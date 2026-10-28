"""
Historical LLM-decision replay for the Main bot (DeepSeekStrategy, spot-long).

What this is and is not:

- This calls the REAL DeepSeek/OpenRouter API with the REAL prompt-building
  and decision-parsing logic (imported directly from
  freqtrade/user_data/strategies/DeepSeekStrategy.py, not reimplemented --
  see _load_strategy_module below), against REAL historical price data
  pulled from Bybit's public market-data API (no key needed for that part).
  This is a genuine test of whether the LLM's price-action judgment has
  any edge, not a coin-flip placeholder.
- It is NOT a faithful reproduction of what the bot would have said live,
  for two reasons the strategy file's own docstring already flags:
    1. "Recent news headlines" and "broader market context" cannot be
       reconstructed for a past date -- there is no free historical news
       archive available (see AGENTS.md's x402-payment lesson). The prompt
       honestly tells the model this context is unavailable for a backtest
       rather than feeding it today's news dressed up as historical.
    2. Live evaluates every new 1h candle (~24x/day/pair). Doing that here
       would mean tens of thousands of paid API calls for a multi-week,
       multi-pair window. This replay subsamples to one decision per pair
       per 24 candles (roughly daily) to keep cost and runtime sane. Intra-
       window stoploss/minimal_roi hits are still checked on every hourly
       candle, not just at decision points, so exits are realistic even
       though entries/rotation checks are subsampled.
  Treat the results as "does price-action-only judgment beat noise over
  this window", not "this is exactly what live would have earned".
- The pair list is a single fixed snapshot (today's top-N Bybit USDT spot
  pairs by volume), not a day-by-day VolumePairList recomputation. Real
  live trading rotates the whitelist daily; this doesn't. Noted in the
  report.

Usage:
    1. Put your key(s) in backtest/.env.local (gitignored, same pattern as
       data/.env -- never committed):
           DEEPSEEK_API_KEY=sk-...
           OPENROUTER_API_KEY=sk-or-...   # optional fallback
    2. python backtest/llm_replay.py --days 30 --pairs 10
       (add --dry-run to sanity-check the simulation mechanics with a fake
       deterministic decision function and zero API calls/cost)

Output: a summary printed to stdout, plus a full JSON report and a CSV of
every simulated trade under backtest/results/.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import types
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
STRATEGY_PATH = REPO_ROOT / "freqtrade" / "user_data" / "strategies" / "DeepSeekStrategy.py"
RESULTS_DIR = SCRIPT_DIR / "results"

STABLE_QUOTE_RE = re.compile(r"^(USDC|USDE|TUSD|FDUSD|DAI|EUR|PYUSD)USDT$")
LEVERAGED_RE = re.compile(r"(2L|2S|3L|3S|4L|4S|5L|5S)USDT$")

HISTORICAL_CONTEXT_NOTE = (
    "historical news/market context is not available for this backtest replay "
    "-- decide from price action, risk tier, and recent simulated performance only"
)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file(SCRIPT_DIR / ".env.local")


def _load_strategy_module():
    """Import the real strategy file standalone, stubbing the freqtrade
    package it imports from -- same trick used by the Wave 1 test scripts,
    so decision parsing here can never silently drift from production."""
    ft = types.ModuleType("freqtrade")
    ft_persistence = types.ModuleType("freqtrade.persistence")
    ft_persistence.Trade = type("Trade", (), {})
    ft_strategy = types.ModuleType("freqtrade.strategy")
    ft_strategy.IStrategy = type("IStrategy", (), {})
    sys.modules.setdefault("freqtrade", ft)
    sys.modules["freqtrade.persistence"] = ft_persistence
    sys.modules["freqtrade.strategy"] = ft_strategy
    # P5: DeepSeekStrategy.py now does `import _second_opinion` as a bare,
    # same-directory import -- real Freqtrade puts user_data/strategies/ on
    # sys.path automatically, so this needs the same here or the import fails.
    strategy_dir = str(STRATEGY_PATH.parent)
    if strategy_dir not in sys.path:
        sys.path.insert(0, strategy_dir)
    spec = importlib.util.spec_from_file_location("deepseek_strategy_module", STRATEGY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_strat = _load_strategy_module()
parse_llm_decision = _strat._parse_llm_decision
SYSTEM_PROMPT = _strat.SYSTEM_PROMPT  # same constant _ask_deepseek uses -- no second hand-copy to drift
CORE_VOLUME_RANK = _strat.DeepSeekStrategy.CORE_VOLUME_RANK
CORE_MAX_VOLATILITY_PCT = _strat.DeepSeekStrategy.CORE_MAX_VOLATILITY_PCT
MINIMAL_ROI = 0.10
STOPLOSS = -0.05
MIN_HOLD_HOURS = _strat.DeepSeekStrategy.MIN_HOLD_HOURS
MAX_HOLD_DECISION_POINTS = 14  # replay-only safety valve, not in live code

# ---------------------------------------------------------------------------
# LLM calls -- intentionally duplicated (not imported) from DeepSeekStrategy's
# _call_deepseek/_call_openrouter_fallback, same reasoning the strategy file
# itself gives for duplicating from brain.py: this script has no Freqtrade
# `self` to hang off of, and staying self-contained means it can't break if
# the strategy file's instance-method shape changes. Keep model names/limits
# in sync with DeepSeekStrategy.py if those ever change there.
# ---------------------------------------------------------------------------
_openrouter_free_models_cache = {"list": None, "fetched_at": 0}
_OPENROUTER_FREE_LIST_TTL_SECONDS = 6 * 60 * 60


def call_deepseek(system: str, user: str) -> str | None:
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-flash",
                "thinking": {"type": "disabled"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            timeout=20,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content or None
    except Exception as e:
        print(f"  [warn] DeepSeek call failed: {e}")
        return None


def _get_openrouter_free_models() -> list[str]:
    now = time.time()
    cache = _openrouter_free_models_cache
    if cache["list"] is not None and (now - cache["fetched_at"]) < _OPENROUTER_FREE_LIST_TTL_SECONDS:
        return cache["list"]
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        entries = r.json().get("data", [])
        free = []
        for m in entries:
            model_id = m.get("id", "")
            pricing = m.get("pricing", {})
            try:
                is_free = float(pricing.get("prompt", "1") or "1") == 0 and float(pricing.get("completion", "1") or "1") == 0
            except (TypeError, ValueError):
                is_free = False
            if model_id.endswith(":free") and is_free:
                free.append(model_id)
        cache["list"], cache["fetched_at"] = free, now
        return free
    except Exception as e:
        print(f"  [warn] Could not fetch OpenRouter model list: {e}")
        return cache["list"] or []


def call_openrouter_fallback(system: str, user: str) -> str | None:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    candidates = _get_openrouter_free_models()[:2]
    models_chain = candidates + ["openai/gpt-4o-mini"]
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "models": models_chain,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 300,
                "temperature": 0.1,
            },
            timeout=25,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return content or None
    except Exception as e:
        print(f"  [warn] OpenRouter fallback failed: {e}")
        return None


def ask_llm(system: str, user: str, dry_run: bool, fake_decision: str, is_core: bool = False) -> tuple[str, str, bool]:
    """`is_core` is unused by the default implementation -- it exists so
    backtest/second_opinion_ab_test.py can replace this function
    (`lr.ask_llm = ...`) with one that also runs the P5 ensemble vote,
    without needing run_replay() itself to know anything about P5."""
    if dry_run:
        return parse_llm_decision(f"{fake_decision}: dry-run fake decision", ("buy", "hold"))
    answer = call_deepseek(system, user) or call_openrouter_fallback(system, user)
    if not answer:
        return "hold", "no LLM response (both calls failed)", False
    return parse_llm_decision(answer, ("buy", "hold"))


# ---------------------------------------------------------------------------
# Historical data (Bybit public REST, no key needed)
# ---------------------------------------------------------------------------
def fetch_top_pairs(n: int) -> list[str]:
    r = requests.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"}, timeout=15)
    r.raise_for_status()
    rows = r.json()["result"]["list"]
    candidates = []
    for row in rows:
        sym = row["symbol"]
        if not sym.endswith("USDT"):
            continue
        if STABLE_QUOTE_RE.match(sym) or LEVERAGED_RE.search(sym):
            continue
        try:
            turnover = float(row.get("turnover24h") or 0)
        except ValueError:
            turnover = 0
        candidates.append((sym, turnover))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [sym for sym, _ in candidates[: n * 2]]  # extra headroom in case some lack enough history


def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame | None:
    r = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={"category": "spot", "symbol": symbol, "interval": "60", "start": start_ms, "end": end_ms, "limit": 1000},
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()["result"]["list"]
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"ts": "int64", "open": "float64", "high": "float64", "low": "float64", "close": "float64"})
    df = df.sort_values("ts").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
@dataclass
class OpenTrade:
    pair: str
    entry_idx: int
    entry_price: float
    entry_time: pd.Timestamp
    is_core: bool
    checkpoints_held: int = 0


@dataclass
class ClosedTrade:
    pair: str
    is_core: bool
    entry_time: str
    close_time: str
    entry_price: float
    close_price: float
    profit_pct: float
    reason: str


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["pct_change_24"] = df["close"].pct_change(24) * 100
    df["volatility_24"] = (
        (df["high"].rolling(24).max() - df["low"].rolling(24).min()) / df["low"].rolling(24).min()
    ) * 100
    return df


MIN_RELIABLE_SAMPLE = _strat.DeepSeekStrategy.MIN_RELIABLE_SAMPLE  # P2.1: mirror production, don't hand-copy the threshold


def performance_summary(closed: list[ClosedTrade], is_core: bool, as_of: pd.Timestamp) -> str:
    visible = [t for t in closed if pd.Timestamp(t.close_time) <= as_of]
    if not visible:
        return "no closed trades yet"
    same_tier = [t for t in visible if t.is_core == is_core]
    sample = same_tier if len(same_tier) >= 3 else visible
    recent = sorted(sample, key=lambda t: t.close_time, reverse=True)[:10]
    total = len(recent)
    wins = sum(1 for t in recent if t.profit_pct > 0)
    avg_pct = sum(t.profit_pct for t in recent) / total
    scope = "same tier" if same_tier else "all pairs, tier history too short yet"
    if total < MIN_RELIABLE_SAMPLE:
        return (
            f"only {total} closed trades ({scope}) -- too few to be a reliable "
            f"record, do not treat as a real track record: {wins}/{total} profitable"
        )
    return f"last {total} closed trades ({scope}): {wins}/{total} profitable, avg {avg_pct:.2f}% per trade"


def run_replay(days: int, num_pairs: int, dry_run: bool, sleep_s: float, max_calls: int) -> dict:
    end = pd.Timestamp.now(tz="UTC").floor("h")
    start = end - pd.Timedelta(days=days + 2)  # +2 buffer for the 24-candle warmup
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    print(f"Fetching top {num_pairs} Bybit USDT spot pairs by volume...")
    candidate_symbols = fetch_top_pairs(num_pairs)

    frames: dict[str, pd.DataFrame] = {}
    for sym in candidate_symbols:
        if len(frames) >= num_pairs:
            break
        df = fetch_klines(sym, start_ms, end_ms)
        min_needed = 24 + days * 24 - 12  # tolerate minor gaps
        if df is None or len(df) < min_needed:
            print(f"  skip {sym}: insufficient history ({0 if df is None else len(df)} candles)")
            continue
        frames[sym] = build_indicators(df)
        print(f"  using {sym}: {len(df)} candles")

    if not frames:
        raise SystemExit("No pairs had enough historical data -- aborting.")

    pairs = list(frames.keys())
    min_len = min(len(df) for df in frames.values())
    num_checkpoints = (min_len - 24) // 24
    print(f"\n{len(pairs)} pairs, {num_checkpoints} daily decision points each, "
          f"up to {len(pairs) * num_checkpoints} LLM calls.\n")

    open_trades: dict[str, OpenTrade] = {}
    core_cache: dict[str, bool] = {}
    closed: list[ClosedTrade] = []
    decisions_log: list[dict] = []
    call_count = 0

    ranked_pairs = pairs  # fixed snapshot rank, already volume-sorted from fetch_top_pairs

    for cp in range(1, num_checkpoints + 1):
        idx = 24 + cp * 24 - 1  # candle index of this checkpoint (0-based)
        prev_idx = idx - 24

        for pair in pairs:
            df = frames[pair]
            row = df.iloc[idx]
            checkpoint_time = row["time"]

            # 1) Check intra-window stoploss/ROI hits on any open trade, hour by hour.
            trade = open_trades.get(pair)
            if trade is not None:
                window = df.iloc[trade.entry_idx + 1: idx + 1]
                hit = None
                for _, hr in window.iterrows():
                    if hr["low"] <= trade.entry_price * (1 + STOPLOSS):
                        hit = ("stoploss", trade.entry_price * (1 + STOPLOSS), hr["time"])
                        break
                    if hr["high"] >= trade.entry_price * (1 + MINIMAL_ROI):
                        hit = ("roi", trade.entry_price * (1 + MINIMAL_ROI), hr["time"])
                        break
                if hit:
                    reason, price, when = hit
                    profit_pct = (price / trade.entry_price - 1) * 100
                    closed.append(ClosedTrade(pair, trade.is_core, str(trade.entry_time), str(when),
                                               trade.entry_price, price, profit_pct, reason))
                    del open_trades[pair]
                    trade = None

            # 2) Volatility / core-satellite classification for this checkpoint.
            volatility = row["volatility_24"]
            rank = ranked_pairs.index(pair)
            is_core = bool(rank < CORE_VOLUME_RANK and pd.notna(volatility) and volatility < CORE_MAX_VOLATILITY_PCT)
            core_cache[pair] = is_core

            if pd.isna(row["pct_change_24"]) or pd.isna(volatility):
                continue

            # 3) Ask the LLM (real call, or a deterministic fake in --dry-run).
            if call_count >= max_calls:
                continue
            tier = "core (currently top-ranked by volume + low volatility)" if is_core else "satellite (smaller-cap and/or higher volatility right now)"
            perf = performance_summary(closed, is_core, checkpoint_time)
            system = SYSTEM_PROMPT
            user = (
                f"Pair: {pair}\n"
                f"Risk tier: {tier}\n"
                f"24-candle % change: {row['pct_change_24']:.2f}\n"
                f"24-candle volatility: {volatility:.2f}\n"
                f"Current close: {row['close']}\n"
                f"Broader market context: {HISTORICAL_CONTEXT_NOTE}\n"
                f"Recent headlines: {HISTORICAL_CONTEXT_NOTE}\n"
                f"Recent performance: {perf}"
            )
            fake_decision = "buy" if row["pct_change_24"] > 2 and volatility < 5 else "hold"
            decision, reason, malformed = ask_llm(system, user, dry_run, fake_decision, is_core=is_core)
            call_count += 1
            if not dry_run:
                time.sleep(sleep_s)
            decisions_log.append({
                "checkpoint": cp, "pair": pair, "time": str(checkpoint_time),
                "decision": decision, "reason": reason, "malformed": malformed, "is_core": is_core,
            })

            # 4) Act on the decision: open new, rotate out of stale, or do nothing.
            trade = open_trades.get(pair)
            if trade is None:
                if decision == "buy":
                    open_trades[pair] = OpenTrade(pair, idx, row["close"], checkpoint_time, is_core)
            else:
                hours_open = (checkpoint_time - trade.entry_time).total_seconds() / 3600
                trade.checkpoints_held += 1
                if hours_open >= MIN_HOLD_HOURS and decision != "buy":
                    profit_pct = (row["close"] / trade.entry_price - 1) * 100
                    closed.append(ClosedTrade(pair, trade.is_core, str(trade.entry_time), str(checkpoint_time),
                                               trade.entry_price, row["close"], profit_pct, "rotate_stale_signal"))
                    del open_trades[pair]
                elif trade.checkpoints_held >= MAX_HOLD_DECISION_POINTS:
                    profit_pct = (row["close"] / trade.entry_price - 1) * 100
                    closed.append(ClosedTrade(pair, trade.is_core, str(trade.entry_time), str(checkpoint_time),
                                               trade.entry_price, row["close"], profit_pct, "max_hold_cap"))
                    del open_trades[pair]

        if cp % 5 == 0 or cp == num_checkpoints:
            print(f"  checkpoint {cp}/{num_checkpoints} -- {call_count} LLM calls so far, "
                  f"{len(closed)} closed, {len(open_trades)} still open")

    # Force-close anything still open at the last available candle, mark to market.
    for pair, trade in open_trades.items():
        df = frames[pair]
        last = df.iloc[min_len - 1]
        profit_pct = (last["close"] / trade.entry_price - 1) * 100
        closed.append(ClosedTrade(pair, trade.is_core, str(trade.entry_time), str(last["time"]),
                                   trade.entry_price, last["close"], profit_pct, "window_end_mark_to_market"))

    # Buy-and-hold benchmark: equal-weight basket of the same pairs over the same window.
    hold_returns = []
    for pair, df in frames.items():
        first, last = df.iloc[24]["close"], df.iloc[min_len - 1]["close"]
        hold_returns.append((last / first - 1) * 100)
    buy_and_hold_pct = sum(hold_returns) / len(hold_returns)

    total = len(closed)
    wins = sum(1 for t in closed if t.profit_pct > 0)
    avg_pct = (sum(t.profit_pct for t in closed) / total) if total else 0.0
    total_return_equal_weight = sum(t.profit_pct for t in closed)  # naive sum, not compounded

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"days": days, "num_pairs": num_pairs, "pairs": pairs, "dry_run": dry_run,
                   "minimal_roi": MINIMAL_ROI, "stoploss": STOPLOSS, "min_hold_hours": MIN_HOLD_HOURS},
        "llm_calls": call_count,
        "malformed_responses": sum(1 for d in decisions_log if d["malformed"]),
        "trades": {
            "total": total, "wins": wins,
            "win_rate_pct": round(100 * wins / total, 1) if total else None,
            "avg_profit_pct_per_trade": round(avg_pct, 3),
            "naive_summed_return_pct": round(total_return_equal_weight, 2),
        },
        "buy_and_hold_benchmark_pct": round(buy_and_hold_pct, 2),
        "reason_breakdown": {r: sum(1 for t in closed if t.reason == r) for r in set(t.reason for t in closed)},
        "closed_trades": [vars(t) for t in closed],
        "decisions": decisions_log,
    }
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pairs", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=0.4, help="seconds between real LLM calls")
    ap.add_argument("--max-calls", type=int, default=1000, help="hard cap on real API calls, cost safety valve")
    ap.add_argument("--dry-run", action="store_true", help="skip real API calls, verify simulation mechanics only")
    args = ap.parse_args()

    if not args.dry_run and not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
        raise SystemExit(
            "No DEEPSEEK_API_KEY or OPENROUTER_API_KEY found (checked env and backtest/.env.local).\n"
            "Add one to backtest/.env.local (gitignored, same pattern as data/.env) or run with --dry-run."
        )

    report = run_replay(args.days, args.pairs, args.dry_run, args.sleep, args.max_calls)

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = RESULTS_DIR / f"replay_{stamp}.json"
    csv_path = RESULTS_DIR / f"replay_{stamp}_trades.csv"
    def _json_default(o):
        if isinstance(o, (pd.Timestamp,)):
            return str(o)
        if hasattr(o, "item"):  # numpy scalar (bool_, float64, int64, ...)
            return o.item()
        raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

    json_path.write_text(json.dumps(report, indent=2, default=_json_default))
    pd.DataFrame(report["closed_trades"]).to_csv(csv_path, index=False)

    t = report["trades"]
    print("\n" + "=" * 60)
    print(f"LLM calls made: {report['llm_calls']}  (malformed responses: {report['malformed_responses']})")
    print(f"Pairs used: {', '.join(report['params']['pairs'])}")
    print(f"Trades: {t['total']}  |  Win rate: {t['win_rate_pct']}%  |  "
          f"Avg profit/trade: {t['avg_profit_pct_per_trade']}%")
    print(f"Naive summed return across all trades: {t['naive_summed_return_pct']}%")
    print(f"Buy-and-hold benchmark (equal-weight basket, same window): {report['buy_and_hold_benchmark_pct']}%")
    print(f"Exit reasons: {report['reason_breakdown']}")
    print(f"\nFull report: {json_path}")
    print(f"Trade log:   {csv_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
