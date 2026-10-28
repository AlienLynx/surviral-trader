"""
P17-A -- Portfolio brain, SHADOW MODE ONLY.

Runs once a day at 00:10 UTC (scheduled in backend/app.py). Reads every bot's
state through Freqtrade's GET endpoints, computes portfolio-level risk numbers
with hard rules (stdlib only), asks DeepSeek for a commentary on the already-
computed numbers, and WRITES FILES. It never places, closes, pauses, resizes or
reconfigures anything: there is no `enforce` mode in P17, and this module has no
code path that sends a non-GET request to a bot. Set PORTFOLIO_BRAIN_MODE=off to
disable it entirely.

Why it exists: the eight running bots have no idea about each other. A live
check on 29/09 found SUI long in Dip and Trend while Shorts and Survival Shorts
were short SUI, PONS long in Moonshot and short in Listing Fade, and LINK/HBAR
long in 3-5 bots at once. Nothing measured the sum of those bets against a
capital floor. This module measures it (and what a CPPI/drawdown policy WOULD
have done) so the operator can decide whether a real enforcement layer is worth
building -- from data, not from a guess.

Outputs (all under DATA_DIR):
  portfolio_brain_snapshots.jsonl   one line per day (equity, HWM, exposure, ...)
  portfolio_brain_state.json        HWM, control-start marker
  portfolio_brain_latest.json       the last full report (served by GET /api/portfolio/brain)
  portfolio_brain_report_<date>.md  the same report, human readable

Portfolio = 6 bots (main, shorts, moonshot,
listing_fade, trend_ensemble, trend_v2). The two control bots and the BTC
benchmark are compared against, never summed in.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import evidence as ev
from . import runtime_settings as rs

log = logging.getLogger("survival-agent.portfolio_brain")

PROMPT_VERSION = "p17a-v1"

PORTFOLIO_BOTS = [
    "main", "shorts", "moonshot",
    "listing_fade", "trend_ensemble", "trend_v2",
]
CONTROL_BOTS = ["control_long", "control_short"]
COHORTS = {
    "llm": ["main", "shorts", "moonshot"],
    "rules": ["listing_fade", "trend_ensemble", "trend_v2"],
    "control": list(CONTROL_BOTS),
}
# A bot is judged against the control that trades the same market type.
BOT_MODE = {
    "main": "spot", "moonshot": "spot", "trend_ensemble": "spot",
    "shorts": "futures", "listing_fade": "futures", "trend_v2": "futures",
}
CONTROL_FOR_MODE = {"spot": "control_long", "futures": "control_short"}
DEFAULT_START_CAPITAL = {
    "main": 500.0, "shorts": 500.0, "moonshot": 100.0,
    "listing_fade": 100.0, "trend_ensemble": 100.0, "trend_v2": 500.0,
    "control_long": 500.0, "control_short": 500.0,
}

# Hypothetical actions for the drawdown ladder. NOTHING here is applied.
LADDER = [
    (-0.03, "L1 (-3% from HWM)", "would cut new-entry risk to x0.75"),
    (-0.06, "L2 (-6% from HWM)", "would halt new entries on the LLM cohort and cut risk to x0.5"),
    (-0.08, "L3 (-8% from HWM)", "would go defensive: stop all new entries and cut risk to x0.25"),
]
WEEKLY_LOSS_LIMIT = -0.02
MONTHLY_LOSS_LIMIT = -0.04
WEEKLY_WOULD = "would pause new entries until the next week"
MONTHLY_WOULD = "would halve risk until the end of the month"

DECAY_WINDOW = 30
DECAY_T_THRESHOLD = -2.0
NO_VALUE_MIN_DAYS = 28
NO_VALUE_MIN_TRADES = 30

SNAPSHOTS_FILE = "portfolio_brain_snapshots.jsonl"
STATE_FILE = "portfolio_brain_state.json"
LATEST_FILE = "portfolio_brain_latest.json"


# ----------------------------------------------------------------- settings
def _f(env: dict, key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except (TypeError, ValueError):
        return default


def load_settings(env: dict | None = None) -> dict:
    env = rs.merged_env() if env is None else env      # P20.2: dashboard switches override the environment
    mode = str(env.get("PORTFOLIO_BRAIN_MODE", "shadow")).strip().lower() or "shadow"
    if mode == "enforce":
        # P20: the variable now also selects the allocator's enforce mode. The brain itself stays read-only.
        mode = "shadow"
    elif mode not in ("off", "shadow"):
        log.error("PORTFOLIO_BRAIN_MODE=%r is not supported (off|shadow|enforce) -- treating as off", mode)
        mode = "off"
    return {
        "mode": mode,
        "floor_pct": _f(env, "PORTFOLIO_FLOOR_PCT", 0.90),
        "cppi_multiplier": _f(env, "CPPI_MULTIPLIER", 3.0),
        "target_monthly_pct": _f(env, "TARGET_MONTHLY_PCT", 1.0),
        "target_weekly_pct": _f(env, "TARGET_WEEKLY_PCT", 0.25),
        "alt_beta": _f(env, "PORTFOLIO_ALT_BETA", 1.0),
    }


# --------------------------------------------------------------- pure rules
def floor_and_cushion(equity: float, reference_capital: float, floor_pct: float,
                      multiplier: float) -> dict:
    """Floor = floor_pct x reference capital; cushion = equity - floor (never negative
    for budgeting); CPPI risky-exposure budget = multiplier x cushion. Also reported at 5x."""
    floor = floor_pct * reference_capital
    cushion = equity - floor
    usable = max(cushion, 0.0)
    return {
        "reference_capital": round(reference_capital, 2),
        "floor": round(floor, 2),
        "cushion": round(cushion, 2),
        "cppi_multiplier": multiplier,
        "cppi_budget": round(multiplier * usable, 2),
        "cppi_budget_5x": round(5.0 * usable, 2),
    }


def coin_of(trade: dict) -> str:
    base = trade.get("base_currency")
    if base:
        return str(base)
    return str(trade.get("pair", "?")).split("/")[0]


def exposure(open_trades: dict[str, list[dict]], alt_beta: float = 1.0) -> dict:
    """Gross and net notional (stake x leverage; long +, short -), by coin and by bot.
    Every non-BTC coin is treated as `alt_beta` x BTC for the net BTC-equivalent."""
    by_coin: dict[str, dict] = {}
    by_bot: dict[str, dict] = {}
    for bot, trades in open_trades.items():
        for t in trades:
            try:
                notional = abs(float(t.get("stake_amount", 0.0))) * float(t.get("leverage") or 1.0)
            except (TypeError, ValueError):
                continue
            sign = -1.0 if t.get("is_short") else 1.0
            c = by_coin.setdefault(coin_of(t), {"long": 0.0, "short": 0.0, "bots_long": [], "bots_short": []})
            if sign > 0:
                c["long"] += notional
                c["bots_long"].append(bot)
            else:
                c["short"] += notional
                c["bots_short"].append(bot)
            b = by_bot.setdefault(bot, {"gross": 0.0, "net": 0.0})
            b["gross"] += notional
            b["net"] += sign * notional
    gross = sum(c["long"] + c["short"] for c in by_coin.values())
    net = sum(c["long"] - c["short"] for c in by_coin.values())
    btc_net = by_coin.get("BTC", {}).get("long", 0.0) - by_coin.get("BTC", {}).get("short", 0.0)
    alt_net = net - btc_net
    conflicts = []
    for coin, c in sorted(by_coin.items()):
        c["net"] = round(c["long"] - c["short"], 2)
        c["gross"] = round(c["long"] + c["short"], 2)
        c["long"], c["short"] = round(c["long"], 2), round(c["short"], 2)
        c["bots_long"], c["bots_short"] = sorted(set(c["bots_long"])), sorted(set(c["bots_short"]))
        if c["bots_long"] and c["bots_short"]:
            conflicts.append({"coin": coin, "long_bots": c["bots_long"], "short_bots": c["bots_short"]})
    crowded = [{"coin": coin, "bots": len(set(c["bots_long"] + c["bots_short"]))}
               for coin, c in by_coin.items() if len(set(c["bots_long"] + c["bots_short"])) >= 3]
    return {
        "gross": round(gross, 2),
        "net": round(net, 2),
        "net_btc_equivalent": round(btc_net + alt_beta * alt_net, 2),
        "alt_beta": alt_beta,
        "by_coin": by_coin,
        "by_bot": {k: {"gross": round(v["gross"], 2), "net": round(v["net"], 2)} for k, v in by_bot.items()},
        "opposite_side_conflicts": conflicts,
        "crowded_coins": sorted(crowded, key=lambda x: -x["bots"]),
    }


def exposure_vs_budget(exp: dict, fc: dict) -> dict:
    budget, budget5 = fc["cppi_budget"], fc["cppi_budget_5x"]
    gross = exp["gross"]
    return {
        "gross_exposure": gross,
        "cppi_budget": budget,
        "cppi_budget_5x": budget5,
        "exceeds_budget": gross > budget,
        "exceeds_budget_5x": gross > budget5,
        "gross_over_budget_ratio": (round(gross / budget, 2) if budget > 0 else None),
    }


def drawdown_state(equity: float, hwm: float, week_ago: float | None, month_ago: float | None) -> dict:
    dd = equity / hwm - 1.0 if hwm > 0 else 0.0
    rungs = [{"level": name, "threshold_pct": thr * 100, "would": act} for thr, name, act in LADDER if dd <= thr]
    weekly = (equity / week_ago - 1.0) if week_ago else None
    monthly = (equity / month_ago - 1.0) if month_ago else None
    return {
        "drawdown_from_hwm_pct": round(dd * 100, 3),
        "highest_rung": rungs[-1] if rungs else None,
        "rungs_triggered": rungs,
        "weekly_change_pct": None if weekly is None else round(weekly * 100, 3),
        "weekly_limit_breached": weekly is not None and weekly <= WEEKLY_LOSS_LIMIT,
        "weekly_would": WEEKLY_WOULD if (weekly is not None and weekly <= WEEKLY_LOSS_LIMIT) else None,
        "monthly_change_pct": None if monthly is None else round(monthly * 100, 3),
        "monthly_limit_breached": monthly is not None and monthly <= MONTHLY_LOSS_LIMIT,
        "monthly_would": MONTHLY_WOULD if (monthly is not None and monthly <= MONTHLY_LOSS_LIMIT) else None,
        "note": "shadow: nothing above is applied to any bot",
    }


def target_progress(period: str, equity: float, period_start_equity: float | None,
                    target_pct: float, floor: float) -> dict:
    if not period_start_equity:
        return {"period": period, "target_pct": target_pct, "gain_pct": None, "reached": False,
                "note": "no snapshot at the period start yet"}
    gain = equity / period_start_equity - 1.0
    reached = target_pct > 0 and gain * 100 >= target_pct
    out = {"period": period, "target_pct": target_pct, "gain_pct": round(gain * 100, 3), "reached": reached}
    if reached:
        out["protect_gains"] = "protect gains: risk x0.5, floor +50% of gains"
        out["proposed_floor"] = round(floor + 0.5 * max(equity - period_start_equity, 0.0), 2)
    return out   # missing the target triggers nothing, by design


def tstat(values: list[float]) -> float | None:
    n = len(values)
    if n < 2:
        return None
    sd = statistics.stdev(values)
    if sd == 0:
        return None
    return statistics.fmean(values) / (sd / math.sqrt(n))


def decay_flag(closed_profit_ratios: list[float]) -> dict:
    """Rolling t-stat of per-trade expectancy over the last DECAY_WINDOW closed trades.
    Flag only with a full window (n >= 30) and t < -2."""
    window = closed_profit_ratios[-DECAY_WINDOW:]
    n = len(window)
    t = tstat(window) if n >= DECAY_WINDOW else None
    return {"n": n, "t_stat": None if t is None else round(t, 3),
            "mean_profit_pct": round(statistics.fmean(window) * 100, 3) if window else None,
            "flag": bool(n >= DECAY_WINDOW and t is not None and t < DECAY_T_THRESHOLD)}


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def closed_since(trades: list[dict], since: datetime | None) -> list[dict]:
    out = []
    for t in trades:
        if t.get("is_open"):
            continue
        cd = _parse_dt(t.get("close_date"))
        if cd is None:
            continue
        if since is None or cd >= since:
            out.append({**t, "_close": cd})
    out.sort(key=lambda t: t["_close"])
    return out


def bot_metrics(trades: list[dict], since: datetime | None, start_capital: float) -> dict:
    ts = closed_since(trades, since)
    n = len(ts)
    profits = [float(t.get("close_profit_abs") or 0.0) for t in ts]
    equity, peak, max_dd = start_capital, start_capital, 0.0
    for p in profits:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    fees = sum(float(t.get("fee_open_cost") or 0.0) + float(t.get("fee_close_cost") or 0.0) for t in ts)
    return {
        "trades": n,
        "return_pct": round(sum(profits) / start_capital * 100, 3) if start_capital else None,
        "profit_abs": round(sum(profits), 2),
        "win_rate_pct": round(sum(1 for p in profits if p > 0) / n * 100, 1) if n else None,
        "expectancy_usd": round(sum(profits) / n, 3) if n else None,
        "max_drawdown_pct": round(max_dd * 100, 3),
        "fees_usd": round(fees, 3),
    }


def no_added_value(bot_m: dict, control_m: dict, days_since_start: float) -> dict:
    """Flag when a bot has been losing to its control analogue for >= 4 weeks with >= 30 trades."""
    enough = days_since_start >= NO_VALUE_MIN_DAYS and (bot_m["trades"] or 0) >= NO_VALUE_MIN_TRADES
    losing = (bot_m["return_pct"] is not None and control_m["return_pct"] is not None
              and bot_m["return_pct"] < control_m["return_pct"])
    return {"flag": bool(enough and losing), "enough_data": bool(enough), "losing_to_control": bool(losing),
            "days": round(days_since_start, 1), "bot_trades": bot_m["trades"]}


def compute_evidence(all_trades: dict[str, list[dict]], cohorts: dict, retired: list[str] | None = None, frozen: dict | None = None) -> dict:
    """P19-B/C/D. One status per bot (all 11) from CLOSED trades after fees, plus the cost gate and the
    live-money checklist. REPORT ONLY: nothing is blocked, paused or resized because of any of this."""
    cb = cohorts.get("bots", {})
    btc = cohorts.get("btc_buy_hold_return_pct")
    ctl_long = cb.get("control_long", {}).get("return_pct")
    out: dict[str, dict] = {}
    for bot in PORTFOLIO_BOTS + CONTROL_BOTS:
        if bot in (retired or []):          # P20.4: history stays, marked retired; the bot is not polled any more
            rec = ((frozen or {}).get(bot) or {}).get("evidence") or {"status": "no_data"}
            out[bot] = {**rec, "retired": True, "retired_note": "bot retired -- last known evidence, not refreshed"}
            continue
        trades = all_trades.get(bot)
        if trades is None:
            out[bot] = {"status": "no_data", "reason": "bot unreachable in this run", "n_closed": 0}
            continue
        closed = closed_since(trades, None)
        vals = [float(t.get("close_profit") or 0.0) for t in closed]
        ci = ev.bootstrap_ci_mean(vals)
        is_control = bot in CONTROL_BOTS
        bot_ret = cb.get(bot, {}).get("return_pct")
        analog = None if is_control else CONTROL_FOR_MODE[BOT_MODE[bot]]
        analog_ret = cb.get(analog, {}).get("return_pct") if analog else None
        beats_control = None if (bot_ret is None or analog_ret is None) else bot_ret > analog_ret
        beats_btc = None if (bot_ret is None or btc is None) else bot_ret > btc
        st = ev.evidence_status(len(vals), ci, beats_control, beats_btc, is_control)
        net = sum(float(t.get("close_profit_abs") or 0.0) for t in closed)
        weeks = ev.weeks_between(closed[0]["_close"], closed[-1]["_close"]) if closed else None
        beats_cl = None if (bot_ret is None or ctl_long is None) else bot_ret > ctl_long
        out[bot] = {
            **st,
            "n_closed": len(vals),
            "mean_trade_pct": None if ci is None else round(ci["mean"] * 100, 4),
            "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
            "net_after_fees_usd": round(net, 2),
            "weeks_of_history": None if weeks is None else round(weeks, 1),
            "return_pct_since_control_start": bot_ret, "control": analog, "control_return_pct": analog_ret,
            "btc_hold_return_pct": btc, "beats_control": beats_control, "beats_btc": beats_btc,
            "cost_gate": ev.cost_gate(closed),
            "live": ev.live_criteria(st["status"], len(vals), weeks, net, beats_cl, beats_btc),
        }
    return out


# -------------------------------------------------------- snapshot history
def load_snapshots(data_dir: Path) -> list[dict]:
    p = data_dir / SNAPSHOTS_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def save_snapshots(data_dir: Path, snaps: list[dict]) -> None:
    (data_dir / SNAPSHOTS_FILE).write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")


def equity_at_or_before(snaps: list[dict], when: datetime, fallback_first: bool = True) -> float | None:
    """Latest complete snapshot at/before `when`. With fallback_first (period-start targets)
    a period that began before the first snapshot uses the first snapshot; the rolling
    weekly/monthly LOSS windows pass fallback_first=False so they stay None until there is
    genuinely a snapshot that old (no '7-day loss' measured over 2 days of history)."""
    complete = [s for s in snaps if not s.get("partial")]
    before = [s for s in complete if (_parse_dt(s["ts"]) or when) <= when]
    if before:
        return float(before[-1]["equity"])
    return float(complete[0]["equity"]) if (complete and fallback_first) else None


def restate_snapshots(snaps: list[dict], roster: list[str]) -> list[dict]:
    """Past snapshots re-expressed for TODAY's roster: equity = the sum of exactly the bots that are in the book now (a retired bot counts at its frozen value).
    A snapshot that lacks one of today's bots is dropped. Without this, removing a bot makes the book look like a crash against a high-water mark and a weekly
    change that included the removed bot's money (on 2026-10-08 two removed bots made a -31% 'drawdown' out of a -1% one and the ladder paused every bot)."""
    out = []
    for sn in snaps:
        bots, frozen = sn.get("bots") or {}, sn.get("retired_frozen") or {}
        vals = [bots[b] if b in bots else frozen.get(b) for b in roster]
        if not roster or any(v is None for v in vals):
            continue
        out.append({**sn, "equity": round(sum(float(v) for v in vals), 2)})
    return out


def load_state(data_dir: Path) -> dict:
    p = data_dir / STATE_FILE
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"hwm": None, "hwm_at": None, "control_first_seen": None}


# ------------------------------------------------------------ bot fetching
class Fetcher:
    """GET-only client for the bots' Freqtrade REST API. HTTP Basic auth is accepted
    by every Freqtrade endpoint, so no login POST is needed: this class cannot write."""

    def __init__(self, urls: dict[str, str], username: str | None, password: str | None,
                 http_get=None, timeout: int = 10):
        import requests  # local import: keeps the pure functions above importable without it
        self._get = http_get or requests.get
        self.urls, self.auth, self.timeout = urls, (username, password), timeout

    def _json(self, bot: str, path: str):
        if bot not in self.urls or not all(self.auth):
            return None
        try:
            r = self._get(f"{self.urls[bot]}{path}", auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning("portfolio_brain: GET %s%s failed: %s", bot, path, e)
            return None

    def reachable(self, bot: str) -> bool:
        if bot not in self.urls:
            return False
        try:
            return self._get(f"{self.urls[bot]}/api/v1/ping", timeout=5).status_code == 200
        except Exception:
            return False

    def balance(self, bot: str):
        return self._json(bot, "/api/v1/balance")

    def status(self, bot: str):
        return self._json(bot, "/api/v1/status")

    def trades(self, bot: str, page: int = 500, max_pages: int = 40) -> list[dict] | None:
        out: list[dict] = []
        offset = 0
        for _ in range(max_pages):
            data = self._json(bot, f"/api/v1/trades?limit={page}&offset={offset}")
            if data is None:
                return out if out else None
            batch = data.get("trades", [])
            out.extend(batch)
            total = data.get("total_trades", len(out))
            offset += page
            if len(batch) < page or offset >= total:
                break
        return out


def default_fetcher() -> Fetcher:
    from . import freqtrade_client
    urls = {bot: cfg["url"] for bot, cfg in freqtrade_client.BOTS.items()}
    return Fetcher(urls, os.getenv("FREQTRADE_API_USERNAME"), os.getenv("FREQTRADE_API_PASSWORD"))


def _equity_of(balance: dict | None) -> float | None:
    if not balance:
        return None
    for key in ("total", "value"):
        v = balance.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _starting_capital(bot: str, balance: dict | None, ledger: dict) -> float:
    entry = ledger.get(bot)
    if isinstance(entry, dict):
        contributions = entry.get("contributions", [])
        if contributions:
            return float(sum(c.get("amount", 0.0) for c in contributions))
    if balance and isinstance(balance.get("starting_capital"), (int, float)):
        return float(balance["starting_capital"])
    return DEFAULT_START_CAPITAL.get(bot, 0.0)


def btc_return_pct_since(start: datetime, http_get=None) -> float | None:
    """BTC buy-and-hold return from `start` to now, from Bybit's public daily klines.
    Any failure -> None (the benchmark is simply omitted)."""
    try:
        import requests
        get = http_get or requests.get
        r = get("https://api.bybit.com/v5/market/kline",
                params={"category": "spot", "symbol": "BTCUSDT", "interval": "D",
                        "start": int(start.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000),
                        "limit": 400}, timeout=10)
        r.raise_for_status()
        rows = r.json()["result"]["list"]          # newest first: [ts, open, high, low, close, ...]
        if not rows:
            return None
        oldest, newest = rows[-1], rows[0]
        return round((float(newest[4]) / float(oldest[1]) - 1.0) * 100, 3)
    except Exception as e:
        log.warning("portfolio_brain: BTC benchmark unavailable: %s", e)
        return None


# ------------------------------------------------------------- LLM analyst
ANALYST_SYSTEM = (
    "You are a portfolio risk analyst for a small multi-bot crypto trading stack. You receive ONLY "
    "pre-computed numbers (no prices). Write a short, sober assessment. Reply with EXACTLY one JSON "
    'object and nothing else: {"regime_note": "<=60 words on portfolio-level risk, in plain words>", '
    '"flags": ["<short factual observations, max 8>"], "proposals": [{"action": "<what a human could '
    'consider>", "target": "<bot or portfolio>", "reason": "<one sentence tied to a number>"}]}. '
    "Proposals are advisory text for a human; nothing you write is executed. Do not invent numbers "
    "that are not in the input. If the data is thin or incomplete, say so instead of guessing."
)


def parse_analyst(text: str | None) -> dict | None:
    """Strict validation. Anything off -> None (skip), never a partial guess."""
    if not text:
        return None
    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    note, flags, proposals = data.get("regime_note"), data.get("flags"), data.get("proposals")
    if not isinstance(note, str) or not isinstance(flags, list) or not isinstance(proposals, list):
        return None
    if not all(isinstance(f, str) for f in flags) or not all(isinstance(p, (dict, str)) for p in proposals):
        return None
    return {"regime_note": note[:600], "flags": [f[:300] for f in flags[:10]], "proposals": proposals[:10]}


def run_analyst(compact: dict, llm) -> dict | None:
    try:
        answer = llm(ANALYST_SYSTEM, json.dumps(compact, indent=1))
    except Exception as e:
        log.warning("portfolio_brain: analyst call failed: %s -- skipped", e)
        return None
    parsed = parse_analyst(answer)
    if parsed is None:
        log.warning("portfolio_brain[%s]: analyst reply was missing/invalid JSON -- skipped", PROMPT_VERSION)
        return None
    log.info("portfolio_brain[%s]: analyst regime_note=%r flags=%s proposals=%s (LOG ONLY, nothing applied)",
             PROMPT_VERSION, parsed["regime_note"], parsed["flags"], parsed["proposals"])
    return {**parsed, "prompt_version": PROMPT_VERSION}


# ------------------------------------------------------------------ report
def render_markdown(r: dict) -> str:
    cap, ex, dd = r["capital"], r["exposure_vs_budget"], r["drawdown"]
    lines = [
        f"# Portfolio brain -- {r['date']} (shadow, nothing applied)",
        "",
        f"- Equity ${cap['equity']} | HWM ${cap['hwm']} | drawdown {dd['drawdown_from_hwm_pct']}%",
        f"- Floor ${cap['floor']} ({cap['floor_pct']*100:.0f}% of ${cap['reference_capital']}) | cushion ${cap['cushion']}",
        f"- CPPI budget {cap['cppi_multiplier']}x = ${cap['cppi_budget']} (5x = ${cap['cppi_budget_5x']}) | "
        f"gross exposure ${ex['gross_exposure']} -> "
        f"{'EXCEEDS' if ex['exceeds_budget'] else 'within'} budget"
        + (f" ({ex['gross_over_budget_ratio']}x)" if ex['gross_over_budget_ratio'] else ""),
        f"- Net exposure ${r['exposure']['net']} | BTC-equivalent ${r['exposure']['net_btc_equivalent']}",
    ]
    if r.get("partial"):
        lines.append(f"- DATA INCOMPLETE: unreachable bots {r['missing_bots']} -- drawdown rules not evaluated")
    if r["exposure"]["opposite_side_conflicts"]:
        lines.append("- Opposite-side conflicts: " + "; ".join(
            f"{c['coin']} long[{','.join(c['long_bots'])}] vs short[{','.join(c['short_bots'])}]"
            for c in r["exposure"]["opposite_side_conflicts"]))
    if r["exposure"]["crowded_coins"]:
        lines.append("- Crowded coins (3+ bots): " + ", ".join(f"{c['coin']} x{c['bots']}" for c in r["exposure"]["crowded_coins"]))
    if dd["rungs_triggered"]:
        lines.append(f"- Ladder: {dd['highest_rung']['level']} {dd['highest_rung']['would']}")
    for k in ("weekly", "monthly"):
        if dd.get(f"{k}_would"):
            lines.append(f"- {k.capitalize()} loss limit breached ({dd[k + '_change_pct']}%): {dd[k + '_would']}")
    for p in r["targets"]:
        if p.get("reached"):
            lines.append(f"- {p['period']} target reached ({p['gain_pct']}%): {p['protect_gains']} (floor -> ${p['proposed_floor']})")
    if r["decay"]:
        flagged = [b for b, d in r["decay"].items() if d["flag"]]
        lines.append("- Decay flags (n>=30, t<-2): " + (", ".join(flagged) if flagged else "none"))
    nv = [b for b, d in r["cohorts"]["no_added_value"].items() if d["flag"]]
    lines.append("- No added value vs control: " + (", ".join(nv) if nv else "none"))
    lines += ["", "## Evidence score (closed trades after fees; report only, nothing is blocked)", "",
              "| bot | status | closed | mean trade % | 95% CI % | fees $ | fees/gross | move/fee | cost flag | live-eligible |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for bot, e in r.get("evidence", {}).items():
        cg = e.get("cost_gate", {})
        lines.append(f"| {bot} | {e['status']} | {e.get('n_closed', 0)} | {e.get('mean_trade_pct')} | {e.get('ci95_pct')} | "
                     f"{cg.get('fees_usd')} | {cg.get('fees_share_of_gross')} | {cg.get('move_to_fee_ratio')} | "
                     f"{'FLAG' if cg.get('flag') else '-'} | {'YES' if e.get('live', {}).get('eligible') else 'no'} |")
    lines.append("")
    lines.append("Live-money criteria: " + (", ".join(r["live_money"]["eligible_bots"]) or "no bot passes") + " -- " + r["live_money"]["rule"])
    lines += ["", "## Cohorts since control start " + str(r["cohorts"].get("since")), "",
              "| bot | cohort | trades | return % | win % | expectancy $ | max DD % | fees $ |", "|---|---|---|---|---|---|---|---|"]
    for bot, m in r["cohorts"]["bots"].items():
        lines.append(f"| {bot} | {m['cohort']} | {m['trades']} | {m['return_pct']} | {m['win_rate_pct']} | "
                     f"{m['expectancy_usd']} | {m['max_drawdown_pct']} | {m['fees_usd']} |")
    if r["cohorts"].get("btc_buy_hold_return_pct") is not None:
        lines.append(f"\nBTC buy-and-hold over the same window: {r['cohorts']['btc_buy_hold_return_pct']}%")
    if r.get("analyst"):
        lines += ["", f"## Analyst ({r['analyst']['prompt_version']}, log only)", "", r["analyst"]["regime_note"], ""]
        lines += [f"- {f}" for f in r["analyst"]["flags"]]
        lines += [f"- PROPOSAL: {p}" for p in r["analyst"]["proposals"]]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ the run
def run_daily(data_dir: Path, now: datetime | None = None, fetcher: Fetcher | None = None,
              llm=None, env: dict | None = None, http_get=None) -> dict:
    settings = load_settings(env)
    now = now or datetime.now(timezone.utc)
    if settings["mode"] == "off":
        return {"mode": "off", "skipped": True}
    data_dir = Path(data_dir)
    fetcher = fetcher or default_fetcher()
    if llm is None:
        from . import brain
        llm = brain.ask

    ledger: dict = {}
    cap_path = data_dir / "capital.json"
    if cap_path.exists():
        try:
            ledger = json.loads(cap_path.read_text(encoding="utf-8"))
        except ValueError:
            ledger = {}

    from . import bot_state as bs      # local import: bot_state imports this module
    retired = bs.retired_from_env(env)
    frozen = bs.freeze_retired(data_dir, retired, now, env)
    col = bs.collect(fetcher, data_dir, now, PORTFOLIO_BOTS, retired)
    balances, open_trades, all_trades = col["balances"], col["open_trades"], col["trades"]
    equities, missing = col["equities"], col["missing"]
    stale_bots, unreachable_bots = col["stale"], col["unreachable"]
    start_caps: dict[str, float] = {b: _starting_capital(b, bal, ledger) for b, bal in balances.items()}
    for bot in CONTROL_BOTS:               # the controls are only compared against, never summed in
        if not fetcher.reachable(bot):
            continue
        bal = fetcher.balance(bot)
        balances[bot] = bal
        start_caps[bot] = _starting_capital(bot, bal, ledger)
        open_trades[bot] = fetcher.status(bot) or []
        all_trades[bot] = fetcher.trades(bot) or []

    partial = bool(missing)
    frozen_eq, dropped_ref = bs.contribution(frozen)
    equity = round(sum(equities.values()) + frozen_eq, 2)
    reference = sum(_starting_capital(b, balances.get(b), ledger) for b in PORTFOLIO_BOTS) - dropped_ref

    state = load_state(data_dir)
    roster = sorted(set(equities) | set(frozen))
    all_snaps = [s for s in load_snapshots(data_dir) if s.get("date") != now.date().isoformat()]
    comparable = restate_snapshots(all_snaps, roster) if not partial else []
    hwm = state.get("hwm")
    hwm_rebased = None
    if not partial:
        # the high-water mark only counts the bots that are in the book now: the state's value is kept only when the roster did not change
        same_roster = state.get("hwm_roster") == roster or (state.get("hwm_roster") is None and hwm is not None and
                       all(sorted(set(s.get("bots") or {}) | set(s.get("retired_frozen") or {})) == roster for s in all_snaps if s.get("date") == str(state.get("hwm_at") or "")[:10]) and
                       any(s.get("date") == str(state.get("hwm_at") or "")[:10] for s in all_snaps))
        hist_max = max([s["equity"] for s in comparable if not s.get("partial")] or [equity])
        if hwm is not None and same_roster:
            hwm = max(float(hwm), hist_max, equity)
        else:
            if hwm is not None:
                hwm_rebased = f"roster changed: high-water mark {round(float(hwm), 2)} (old roster) replaced by {round(max(hist_max, equity), 2)} (today's bots only)"
                log.warning("portfolio_brain: %s", hwm_rebased)
            hwm = max(hist_max, equity)
    elif hwm is None:
        hwm = equity
    fc = floor_and_cushion(equity, reference, settings["floor_pct"], settings["cppi_multiplier"])
    exp = exposure({b: t for b, t in open_trades.items() if b in PORTFOLIO_BOTS}, settings["alt_beta"])
    exp_budget = exposure_vs_budget(exp, fc)

    snaps = comparable if not partial else [s for s in all_snaps]
    week_ago = equity_at_or_before(snaps, now - timedelta(days=7), fallback_first=False)
    month_ago = equity_at_or_before(snaps, now - timedelta(days=30), fallback_first=False)
    if partial:
        dd = {"drawdown_from_hwm_pct": None, "rungs_triggered": [], "highest_rung": None,
              "weekly_limit_breached": False, "monthly_limit_breached": False,
              "note": f"not evaluated: bots unreachable {missing}"}
    else:
        dd = drawdown_state(equity, float(hwm), week_ago, month_ago)

    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    targets = [] if partial else [
        target_progress("week", equity, equity_at_or_before(snaps, week_start) if snaps else None,
                        settings["target_weekly_pct"], fc["floor"]),
        target_progress("month", equity, equity_at_or_before(snaps, month_start) if snaps else None,
                        settings["target_monthly_pct"], fc["floor"]),
    ]

    decay = {b: decay_flag([float(t.get("close_profit") or 0.0) for t in closed_since(all_trades.get(b, []), None)])
             for b in PORTFOLIO_BOTS if b in all_trades}

    # ---- cohorts / control comparison, from the moment the controls first traded
    ctl_seen = state.get("control_first_seen")
    ctl_trade_dates = [d for b in CONTROL_BOTS for t in all_trades.get(b, [])
                       if (d := _parse_dt(t.get("open_date"))) is not None]
    if not ctl_seen and any(b in balances for b in CONTROL_BOTS):
        ctl_seen = now.isoformat()
    since_candidates = [d for d in ctl_trade_dates] + ([_parse_dt(ctl_seen)] if ctl_seen else [])
    since = min(since_candidates) if since_candidates else None
    cohorts: dict = {"since": since.isoformat() if since else None, "bots": {}, "no_added_value": {}}
    if since:
        days = (now - since).total_seconds() / 86400
        for cohort, bots in COHORTS.items():
            for bot in bots:
                if bot in all_trades:
                    cohorts["bots"][bot] = {"cohort": cohort, **bot_metrics(
                        all_trades[bot], since, start_caps.get(bot) or DEFAULT_START_CAPITAL.get(bot, 0.0))}
        for bot in PORTFOLIO_BOTS:
            ctl = CONTROL_FOR_MODE[BOT_MODE[bot]]
            if bot in cohorts["bots"] and ctl in cohorts["bots"]:
                cohorts["no_added_value"][bot] = {**no_added_value(cohorts["bots"][bot], cohorts["bots"][ctl], days),
                                                  "control": ctl}
        cohorts["btc_buy_hold_return_pct"] = btc_return_pct_since(since, http_get)
        cohorts["days_since_start"] = round(days, 1)
    else:
        cohorts["note"] = "control bots not seen yet"

    evidence_out = compute_evidence(all_trades, cohorts, retired, frozen)

    report = {
        "date": now.date().isoformat(), "generated_at": now.isoformat(), "mode": settings["mode"],
        "prompt_version": PROMPT_VERSION, "partial": partial, "missing_bots": missing,
        "stale_bots": stale_bots, "unreachable_bots": unreachable_bots, "retired_bots": frozen,
        "roster_note": ("stale = no answer this run, last known equity/exposure (<=24h) used; unreachable = silent >24h, last equity kept, exposure unknown; "
                        "retired = not polled, equity frozen as a constant (or dropped from the reference when it could not be frozen)"),
        "capital": {"equity": equity, "hwm": round(float(hwm), 2), "floor_pct": settings["floor_pct"],
                    "floor": fc["floor"], "cushion": fc["cushion"], "reference_capital": fc["reference_capital"],
                    "cppi_multiplier": fc["cppi_multiplier"], "cppi_budget": fc["cppi_budget"],
                    "cppi_budget_5x": fc["cppi_budget_5x"], "per_bot_equity": {k: round(v, 2) for k, v in equities.items()},
                    "retired_frozen_equity": {b: r.get("frozen_equity") for b, r in frozen.items()}, "hwm_rebased": hwm_rebased},
        "exposure": exp, "exposure_vs_budget": exp_budget, "drawdown": dd, "targets": targets,
        "decay": decay, "cohorts": cohorts, "evidence": evidence_out,
        "live_money": {"eligible_bots": [b for b, e in evidence_out.items() if e.get("live", {}).get("eligible")],
                       "rule": "edge status AND >= 100 closed trades over >= 8 weeks AND positive after fees AND "
                               "better than control_long AND BTC hold (report only, not code)"},
        "policy_note": "SHADOW MODE: every 'would' is hypothetical; no bot was touched.",
    }
    compact = {k: report[k] for k in ("capital", "exposure_vs_budget", "drawdown", "targets", "partial", "missing_bots")}
    compact["exposure"] = {k: exp[k] for k in ("gross", "net", "net_btc_equivalent", "opposite_side_conflicts", "crowded_coins")}
    compact["decay_flags"] = [b for b, d in decay.items() if d["flag"]]
    compact["evidence_status"] = {b: e["status"] for b, e in evidence_out.items()}
    compact["cost_gate_flags"] = [b for b, e in evidence_out.items() if e.get("cost_gate", {}).get("flag")]
    compact["cohorts"] = {"since": cohorts.get("since"), "bots": cohorts["bots"], "no_added_value": cohorts["no_added_value"],
                          "btc_buy_hold_return_pct": cohorts.get("btc_buy_hold_return_pct")}
    report["analyst"] = run_analyst(compact, llm)

    snap = {"ts": now.isoformat(), "date": now.date().isoformat(), "equity": equity, "hwm": round(float(hwm), 2),
            "floor": fc["floor"], "cushion": fc["cushion"], "gross_exposure": exp["gross"], "net_exposure": exp["net"],
            "partial": partial, "bots": {k: round(v, 2) for k, v in equities.items()}, "mode": settings["mode"],
            "stale_bots": stale_bots, "unreachable_bots": unreachable_bots, "retired_frozen": {b: r.get("frozen_equity") for b, r in frozen.items()}}
    all_snaps.append(snap)
    save_snapshots(data_dir, all_snaps)
    (data_dir / STATE_FILE).write_text(json.dumps({"hwm": hwm, "hwm_at": state.get("hwm_at") if (hwm == state.get("hwm") and not hwm_rebased) else now.isoformat(),
                                                   "hwm_roster": roster if not partial else state.get("hwm_roster"),
                                                   "control_first_seen": ctl_seen}, indent=2), encoding="utf-8")
    (data_dir / LATEST_FILE).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (data_dir / f"portfolio_brain_report_{report['date']}.md").write_text(render_markdown(report), encoding="utf-8")
    log.info("portfolio_brain: %s equity=$%s hwm=$%s gross=$%s budget=$%s partial=%s",
             report["date"], equity, round(float(hwm), 2), exp["gross"], fc["cppi_budget"], partial)
    return report


def seconds_until_next(now: datetime, hour: int = 0, minute: int = 10) -> float:
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


if __name__ == "__main__":   # manual run: docker exec survival-agent python -m backend.engine.portfolio_brain
    logging.basicConfig(level=logging.INFO)
    run_daily(Path(os.getenv("DATA_DIR", "/data")))
