"""P9: weekly-readable scorecard across all 6 bots.

Architecture note, verified before writing a line of this: this script
reads each bot's sqlite `trades` table DIRECTLY (read-only, same
`sqlite3.connect("file:" + path + "?mode=ro", uri=True)` pattern as
backtest/auto_tune.py) -- NOT via backend/engine/freqtrade_client.py's
REST calls. That means it CANNOT run as live code inside the
survival-agent container: docker-compose.yml only mounts
`survival-agent-data:/data` there, none of the `freqtrade*-db` named
volumes. It runs the same way auto_tune.py already does -- externally
(operator's own machine/host with the volumes reachable, or `docker cp`
a copy of each `tradesv3.sqlite` out first), given explicit `--db
bot_id=/path/to/tradesv3.sqlite` per bot. `GET /api/scorecard` (added to
backend/app.py) does NOT recompute anything live -- it just serves the
JSON this script last wrote to `DATA_DIR/scorecard.json`, the same
"external writer, backend just reads the file" pattern as
`/api/balance-history` and `backend/engine/payout_state.py`.

Never touches an exchange, never places an order, never influences a
live decision -- read-only reporting only.

Verdict thresholds are a plain dict (DEFAULT_THRESHOLDS below),
overridable via `--thresholds path/to.json` (merged over the default,
not replacing it) -- deliberately not hardcoded so the operator can
move them without a code change.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ALL_BOTS = ["main", "shorts", "moonshot",
            "listing_fade", "trend_ensemble", "trend_v2"]

# Same numbers as frontend/index.html's BOT_START_WALLET / backend/app.py's
# DEFAULT_STARTING_CAPITAL -- duplicated here on purpose (this script runs
# outside the backend container/process, same "duplicate rather than share
# across containers" convention as the rest of this codebase) rather than
# imported. Used only to express max_drawdown as a % of a bot's own
# starting capital, not as a claim about its current live balance.
BOT_START_WALLET_USD = {
    "main": 500.0, "shorts": 500.0, "moonshot": 100.0,
    "listing_fade": 100.0, "trend_ensemble": 100.0, "trend_v2": 500.0,
}

# P11: listing_fade and trend_ensemble are isolated rule-based (no LLM)
# experiments -- a dict so adding an isolated bot here
# means one line, not three separate `if bot_id == "..."` checks below.
ISOLATED_EXPERIMENT_NOTES = {
    "listing_fade": "isolated rule-based experiment, not a funding decision yet",
    "trend_ensemble": "isolated rule-based experiment, not a funding decision yet",
    "trend_v2": "isolated rule-based experiment (v1 successor), not a funding decision yet",
}

DEFAULT_THRESHOLDS = {
    "min_trades_for_verdict": 10,       # fewer than this -> WATCH, not enough data for PASS/KILL
    "kill_profit_factor": 1.0,          # profit_factor below this -> KILL (losing more than winning)
    "kill_max_drawdown_pct": 30.0,      # drawdown above this (% of starting wallet) -> KILL
    "pass_profit_factor": 1.3,          # profit_factor at/above this (and dd within bound) -> PASS
    "pass_max_drawdown_pct": 15.0,      # drawdown at/below this (with pass_profit_factor) -> PASS
}


def load_closed_trades_in_window(db_path: str, days: int) -> list[tuple[float, float, str]]:
    """Read-only, same file:...?mode=ro pattern as auto_tune.py. Returns
    (close_profit, close_profit_abs, close_date) for trades closed within
    the last `days`, oldest-to-newest (chronological, needed for the
    drawdown curve below)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT close_profit, close_profit_abs, close_date FROM trades "
            "WHERE is_open = 0 AND close_date IS NOT NULL AND close_date >= ? "
            "ORDER BY close_date ASC",
            (cutoff,),
        ).fetchall()
    finally:
        con.close()
    out = []
    for profit, profit_abs, close_date in rows:
        try:
            out.append((float(profit or 0), float(profit_abs or 0), close_date))
        except (TypeError, ValueError):
            continue
    return out


def compute_profit_factor(profits_abs: list[float]) -> float | None:
    """gross profit / gross loss. None (undefined) if there are no
    losing trades to divide by -- never fabricated as 0 or infinity in
    the JSON output; the caller reports it as null with a note instead."""
    gross_profit = sum(p for p in profits_abs if p > 0)
    gross_loss = -sum(p for p in profits_abs if p < 0)
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


def compute_expectancy_usd(profits_abs: list[float]) -> float | None:
    if not profits_abs:
        return None
    return sum(profits_abs) / len(profits_abs)


def compute_profit_factor_excl_best_trade(profits_abs: list[float]) -> float | None:
    """Same as compute_profit_factor but with the single largest winning
    trade removed -- guards against one lucky outlier making a small
    sample look like a real edge."""
    if not profits_abs:
        return None
    best_idx = max(range(len(profits_abs)), key=lambda i: profits_abs[i])
    remaining = profits_abs[:best_idx] + profits_abs[best_idx + 1:]
    return compute_profit_factor(remaining)


def compute_max_drawdown_pct(profits_abs_chronological: list[float], starting_wallet_usd: float) -> float | None:
    """Cumulative equity curve (running sum of $ profit, starting at 0)
    within the window; max peak-to-trough drop, expressed as a % of the
    bot's starting wallet (not its current live balance -- this script
    has no live balance access, see module docstring). None if the
    window has no trades at all."""
    if not profits_abs_chronological or starting_wallet_usd <= 0:
        return None
    equity = 0.0
    peak = 0.0
    max_dd_usd = 0.0
    for p in profits_abs_chronological:
        equity += p
        peak = max(peak, equity)
        max_dd_usd = max(max_dd_usd, peak - equity)
    return (max_dd_usd / starting_wallet_usd) * 100


def get_btc_return_pct(days: int) -> float | None:
    """CoinGecko's free historical market_chart endpoint, no key needed.
    Returns None (excluded, not a fabricated 0%) on any failure --
    same reliability contract as the P8 market-signal fetches."""
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": days},
            timeout=15,
        )
        r.raise_for_status()
        prices = r.json().get("prices", [])
        if len(prices) < 2:
            return None
        first_price = prices[0][1]
        last_price = prices[-1][1]
        if not first_price:
            return None
        return (last_price - first_price) / first_price * 100
    except Exception as e:
        print(f"WARNING: BTC return fetch failed: {e} -- excluded from report", file=sys.stderr)
        return None


def decide_verdict(trade_count: int, profit_factor: float | None, max_drawdown_pct: float | None,
                    thresholds: dict) -> tuple[str, str]:
    if trade_count < thresholds["min_trades_for_verdict"]:
        return "WATCH", f"only {trade_count} closed trades in window (need >= {thresholds['min_trades_for_verdict']})"
    if profit_factor is not None and profit_factor < thresholds["kill_profit_factor"]:
        return "KILL", f"profit_factor {profit_factor:.2f} < {thresholds['kill_profit_factor']}"
    if max_drawdown_pct is not None and max_drawdown_pct > thresholds["kill_max_drawdown_pct"]:
        return "KILL", f"max_drawdown {max_drawdown_pct:.1f}% > {thresholds['kill_max_drawdown_pct']}%"
    if (profit_factor is not None and profit_factor >= thresholds["pass_profit_factor"]
            and (max_drawdown_pct is None or max_drawdown_pct <= thresholds["pass_max_drawdown_pct"])):
        return "PASS", f"profit_factor {profit_factor:.2f} >= {thresholds['pass_profit_factor']}, drawdown within bound"
    if profit_factor is None:
        return "WATCH", "no losing trades in window yet -- profit_factor undefined, too early to call PASS on zero losses"
    return "WATCH", f"profit_factor {profit_factor:.2f} in between PASS and KILL thresholds"


def build_scorecard_for_bot(bot_id: str, db_path: str, days: int, thresholds: dict) -> dict:
    trades = load_closed_trades_in_window(db_path, days)
    profits_abs = [t[1] for t in trades]
    trade_count = len(trades)
    wins = sum(1 for p in profits_abs if p > 0)
    win_rate = wins / trade_count if trade_count else None
    profit_factor = compute_profit_factor(profits_abs)
    expectancy_usd = compute_expectancy_usd(profits_abs)
    profit_factor_excl_best = compute_profit_factor_excl_best_trade(profits_abs)
    starting_wallet = BOT_START_WALLET_USD.get(bot_id, 0.0)
    max_dd_pct = compute_max_drawdown_pct(profits_abs, starting_wallet)
    verdict, verdict_reason = decide_verdict(trade_count, profit_factor, max_dd_pct, thresholds)

    result = {
        "bot_id": bot_id,
        "trade_count": trade_count,
        "win_rate": round(win_rate, 4) if win_rate is not None else None,
        "profit_factor": round(profit_factor, 3) if profit_factor is not None else None,
        "expectancy_usd": round(expectancy_usd, 2) if expectancy_usd is not None else None,
        "profit_factor_excl_best_trade": round(profit_factor_excl_best, 3) if profit_factor_excl_best is not None else None,
        "max_drawdown_pct": round(max_dd_pct, 2) if max_dd_pct is not None else None,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
    }
    if bot_id in ISOLATED_EXPERIMENT_NOTES:
        result["note"] = ISOLATED_EXPERIMENT_NOTES[bot_id]
    return result


def build_report(db_paths: dict[str, str], days: int, thresholds: dict) -> dict:
    """db_paths: {bot_id: sqlite_path}. Bots not present are skipped
    with an honest 'no db path given' placeholder, never silently
    omitted -- the operator sees every bot in the output, even one they
    forgot to pass a --db for."""
    btc_return_pct = get_btc_return_pct(days)
    bots = {}
    for bot_id in ALL_BOTS:
        if bot_id not in db_paths:
            entry = {"bot_id": bot_id, "verdict": "WATCH", "verdict_reason": "no --db path given for this bot"}
            if bot_id in ISOLATED_EXPERIMENT_NOTES:
                entry["note"] = ISOLATED_EXPERIMENT_NOTES[bot_id]
            bots[bot_id] = entry
            continue
        try:
            bots[bot_id] = build_scorecard_for_bot(bot_id, db_paths[bot_id], days, thresholds)
        except (sqlite3.Error, OSError) as e:
            entry = {"bot_id": bot_id, "verdict": "WATCH", "verdict_reason": f"could not read db: {e}"}
            if bot_id in ISOLATED_EXPERIMENT_NOTES:
                entry["note"] = ISOLATED_EXPERIMENT_NOTES[bot_id]
            bots[bot_id] = entry
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": days,
        "btc_return_pct": round(btc_return_pct, 2) if btc_return_pct is not None else None,
        "thresholds": thresholds,
        "bots": bots,
    }


def render_markdown(report: dict) -> str:
    lines = [
        f"# Bot scorecard -- {report['window_days']}-day window",
        f"Generated: {report['generated_at']}",
        "",
        f"BTC return over the same window: "
        + (f"{report['btc_return_pct']:+.2f}%" if report["btc_return_pct"] is not None else "unavailable"),
        "",
        "| Bot | Trades | Win rate | Profit factor | PF excl. best | Expectancy $ | Max DD % | Verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def _fmt(v, suffix=""):
        return "n/a" if v is None else f"{v}{suffix}"

    for bot_id in ALL_BOTS:
        b = report["bots"].get(bot_id, {})
        win_rate = b.get("win_rate")
        win_rate_str = f"{win_rate * 100:.1f}%" if win_rate is not None else "n/a"
        lines.append(
            "| {bot} | {n} | {wr} | {pf} | {pfx} | {exp} | {dd} | **{v}** |".format(
                bot=bot_id, n=_fmt(b.get("trade_count")), wr=win_rate_str,
                pf=_fmt(b.get("profit_factor")), pfx=_fmt(b.get("profit_factor_excl_best_trade")),
                exp=_fmt(b.get("expectancy_usd"), " $"), dd=_fmt(b.get("max_drawdown_pct"), "%"),
                v=b.get("verdict", "?"),
            )
        )
    lines.append("")
    for bot_id in ALL_BOTS:
        b = report["bots"].get(bot_id, {})
        reason = b.get("verdict_reason", "")
        note = f" ({b['note']})" if "note" in b else ""
        lines.append(f"- **{bot_id}**{note}: {b.get('verdict', '?')} -- {reason}")
    return "\n".join(lines) + "\n"


def _parse_db_arg(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values or []:
        if "=" not in v:
            raise ValueError(f"--db expects bot_id=/path/to/tradesv3.sqlite, got: {v}")
        bot_id, path = v.split("=", 1)
        if bot_id not in ALL_BOTS:
            raise ValueError(f"--db: unknown bot id '{bot_id}', expected one of {ALL_BOTS}")
        out[bot_id] = path
    return out


def _load_thresholds(path: str | None) -> dict:
    thresholds = dict(DEFAULT_THRESHOLDS)
    if path:
        overrides = json.loads(Path(path).read_text(encoding="utf-8"))
        thresholds.update(overrides)
    return thresholds


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="P9 bot scorecard -- read-only sqlite trade analysis, all 6 bots")
    p.add_argument("--db", action="append", metavar="BOT_ID=PATH",
                    help="repeatable, one per bot, e.g. --db main=/path/tradesv3.sqlite")
    p.add_argument("--days", type=int, default=21)
    p.add_argument("--thresholds", default=None, help="JSON file merged over DEFAULT_THRESHOLDS")
    p.add_argument("--out-json", default=None)
    p.add_argument("--out-md", default=None)
    args = p.parse_args(argv)

    try:
        db_paths = _parse_db_arg(args.db)
        thresholds = _load_thresholds(args.thresholds)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    report = build_report(db_paths, args.days, thresholds)
    report_json = json.dumps(report, indent=2)
    report_md = render_markdown(report)

    if args.out_json:
        Path(args.out_json).write_text(report_json + "\n", encoding="utf-8")
    if args.out_md:
        Path(args.out_md).write_text(report_md, encoding="utf-8")
    if not args.out_json and not args.out_md:
        print(report_json)
        print()
        print(report_md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
