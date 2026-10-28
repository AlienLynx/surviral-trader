"""
Primary cycle: reads what Freqtrade actually did, computes the
profit split from config, logs it. Does NOT make its own trading
decisions anymore -- see AGENTS.md for why we pivoted away from that
after comparing against mature open-source bots.

The original signals -> brain -> risk_guard -> executor chain still
exists below as run_cycle_own_engine_experimental(), kept for
reference and manual testing, but the background loop in app.py does
not call it by default.
"""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brain, signals
from .freqtrade_client import get_summary
from .payout_state import SPLIT_BOTS, extract_profit, load_state, months_since
from .split_calc import compute_split
from .risk_guard import ProposedTrade, check_trade
from .executor import execute

log = logging.getLogger("survival-agent.loop")

DECISION_SCHEMA_HINT = """Reply with ONLY a JSON object, no other text:
{"action": "buy" | "sell" | "hold", "symbol": "BTCUSDT", "size_usd": number, "stop_loss_pct": number, "reasoning": "one sentence"}
If holding, size_usd and stop_loss_pct can be 0."""


def _log_decision(data_dir: Path, entry: dict) -> None:
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    log_path = data_dir / "decisions.log"
    with open(log_path, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def run_cycle(data_dir: Path, config: dict) -> dict:
    """
    The accounting cycle. Reads Freqtrade's real (dry-run or live,
    whatever it's actually set to) numbers for every SPLIT_BOTS bot,
    aggregates profit SINCE the last confirmed payout (payout_state.py),
    and computes the survival/payout/reinvest split from config. Never
    moves money -- payouts are a number to review and act on yourself
    via POST /api/payout/confirm, not an automatic transfer.

    A bot that's unreachable this cycle contributes a 0.0 delta (its
    last known baseline, unchanged) rather than crashing the whole
    cycle or silently pretending it earned nothing all-time -- see
    unavailable_bots in the result. If every bot is unreachable, the
    cycle is skipped entirely rather than reporting a meaningless
    all-zero split.
    """
    state = load_state(data_dir)

    per_bot_current_profit: dict[str, float] = {}
    per_bot: dict[str, float] = {}
    unavailable_bots: list[str] = []
    for bot_id in SPLIT_BOTS:
        summary = get_summary(bot_id)
        if not summary.get("available"):
            unavailable_bots.append(bot_id)
            current = state["bots"].get(bot_id, 0.0)
        else:
            current = extract_profit(summary.get("profit"))
        per_bot_current_profit[bot_id] = current
        per_bot[bot_id] = current - state["bots"].get(bot_id, 0.0)

    if len(unavailable_bots) == len(SPLIT_BOTS):
        result = {"stage": "accounting", "outcome": "skipped", "reason": "no SPLIT_BOTS bot reachable this cycle"}
        _log_decision(data_dir, result)
        return result

    # No floor(0) per-bot before summing -- a real drawdown in one bot
    # genuinely reduces the operator's total payout-eligible profit, it
    # is not ignored just because another bot happened to be up. Same
    # money either way; hiding a loss here would overstate the split.
    gross_profit_total = sum(per_bot.values())
    months_elapsed = months_since(state["baseline_set_at"])
    split = compute_split(gross_profit_total, months_elapsed, config)

    result = {
        "stage": "accounting",
        "outcome": "reviewed",
        "months_elapsed": months_elapsed,
        "per_bot": per_bot,
        "per_bot_current_profit": per_bot_current_profit,
        "unavailable_bots": unavailable_bots,
        "gross_profit_total": round(gross_profit_total, 2),
        "split": split,
    }
    _log_decision(data_dir, result)
    return result


# --------------------------------------------------------------------
# Below: the original independent decision-making cycle. NOT called by
# the background loop by default (see module docstring and AGENTS.md).
# Kept for reference and manual testing only.
# --------------------------------------------------------------------

def _parse_decision(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned.strip())
    except Exception as e:
        log.error("Could not parse brain response as JSON: %s | raw=%r", e, raw[:200])
        return None


def run_cycle_own_engine_experimental(data_dir: Path, config: dict) -> dict:
    signal = signals.get_signal(symbol="BTCUSDT", hours=24)

    if not signal.get("available"):
        result = {"stage": "signal", "outcome": "skipped", "reason": signal.get("reason", "unknown")}
        _log_decision(data_dir, result)
        return result

    capital_usd = float(config.get("start", 0))  # not a live balance -- see AGENTS.md

    system = (
        "You are the trading brain for a small, self-funding crypto agent. "
        "You get one signal per cycle and must decide buy, sell, or hold. "
        "Be conservative: most cycles should be 'hold' unless the signal is genuinely clear. "
        + DECISION_SCHEMA_HINT
    )
    user = (
        f"Goal: grow ${config.get('start')} toward ${config.get('target')} over {config.get('months')} months. "
        f"Assumed available capital: ${capital_usd:.2f} (not a live balance -- treat conservatively). "
        f"Signal: {json.dumps(signal)}"
    )

    raw = brain.ask(system, user)
    decision = _parse_decision(raw)

    if decision is None:
        result = {"stage": "brain", "outcome": "skipped", "reason": "no parseable decision", "signal": signal}
        _log_decision(data_dir, result)
        return result

    if decision.get("action") == "hold":
        result = {"stage": "brain", "outcome": "hold", "reasoning": decision.get("reasoning"), "signal": signal}
        _log_decision(data_dir, result)
        return result

    trade = ProposedTrade(
        symbol=decision.get("symbol", "BTCUSDT"),
        side=decision.get("action"),
        size_usd=float(decision.get("size_usd", 0)),
        stop_loss_pct=float(decision.get("stop_loss_pct", 0)),
    )

    risk_result = check_trade(
        trade=trade,
        capital_usd=capital_usd,
        open_positions=0,
        daily_pnl_pct=0.0,
        limits=config,
    )

    if not risk_result.approved:
        result = {
            "stage": "risk_guard",
            "outcome": "rejected",
            "reason": risk_result.reason,
            "proposed_trade": trade.__dict__,
            "reasoning": decision.get("reasoning"),
        }
        _log_decision(data_dir, result)
        return result

    exec_result = execute(trade)
    result = {
        "stage": "executor",
        "outcome": "executed" if exec_result.get("executed") else "logged_dry_run",
        "trade": trade.__dict__,
        "reasoning": decision.get("reasoning"),
        "exec_result": exec_result,
    }
    _log_decision(data_dir, result)
    return result
