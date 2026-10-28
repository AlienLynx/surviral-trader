"""
Persistent state for the manual payout/split accounting cycle --
DATA_DIR/payout_state.json. Tracks each SPLIT_BOTS bot's profit
"baseline" (its all-time profit at the moment of the last confirmed
payout, or 0.0 if none yet), so loop.run_cycle() can report profit
SINCE the last payout instead of re-splitting the same all-time total
forever.

Never moves money -- see loop.py's own docstring. POST /api/payout/confirm
(backend/app.py) just means a human already moved money elsewhere and is
telling this system so the next cycle's baseline resets; nothing here
calls an exchange or a payment API.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

# The bots whose profit is summed into the payout split. The isolated experiments (listing_fade, trend_*) are deliberately not in it.
# Change this list if that decision changes later -- nothing else needs to know about it.
SPLIT_BOTS = ["main", "shorts", "moonshot"]

PAYOUT_STATE_FILENAME = "payout_state.json"
PLAN_STARTED_AT_FILENAME = "plan_started_at.txt"


def _state_path(data_dir: Path) -> Path:
    return data_dir / PAYOUT_STATE_FILENAME


def load_state(data_dir: Path) -> dict:
    """
    First-ever call (no payout_state.json yet): baseline_set_at reuses
    the existing plan_started_at.txt marker if one is already there
    (same "months since the plan started" marker loop.py used to keep
    on its own before this module existed) rather than creating a
    second, different start-time source of truth; writes the marker
    fresh only if neither file exists yet. Every bot's baseline starts
    at 0.0 -- equivalent to today's pre-payout-tracking behavior, where
    all-time profit and profit-since-baseline were the same number.
    """
    path = _state_path(data_dir)
    if path.exists():
        return json.loads(path.read_text())

    marker = data_dir / PLAN_STARTED_AT_FILENAME
    if marker.exists():
        baseline_set_at = marker.read_text().strip()
    else:
        baseline_set_at = datetime.now(timezone.utc).isoformat()
        marker.write_text(baseline_set_at)

    state = {
        "baseline_set_at": baseline_set_at,
        "bots": {bot_id: 0.0 for bot_id in SPLIT_BOTS},
        "history": [],
    }
    save_state(data_dir, state)
    return state


def save_state(data_dir: Path, state: dict) -> None:
    _state_path(data_dir).write_text(json.dumps(state, indent=2, default=str))


def months_since(iso_ts: str) -> int:
    """~30-day-month approximation, 1-indexed and floored at 1 -- same
    math loop.py's old _months_elapsed used, now keyed off an arbitrary
    stored timestamp (the payout baseline) instead of a fixed file."""
    started_at = datetime.fromisoformat(iso_ts)
    days = (datetime.now(timezone.utc) - started_at).days
    return max(1, days // 30 + 1)


def extract_profit(freqtrade_profit: dict | None) -> float:
    """
    Freqtrade's /api/v1/profit response shape can vary by version. Try
    the common field names defensively; return 0.0 (not a crash) if
    none match -- the raw response is still logged alongside this by
    the caller, so nothing is hidden if the guess is wrong. Moved here
    from loop.py (P6-addon's payout work) so both loop.run_cycle() and
    app.py's /api/payout/confirm can share one implementation instead
    of two copies drifting apart.
    """
    if not freqtrade_profit:
        return 0.0
    for field in ("profit_closed_coin", "profit_all_coin", "profit_closed_fiat", "profit_all_fiat"):
        value = freqtrade_profit.get(field)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return 0.0


def confirm_payout(data_dir: Path, per_bot_profit_now: dict[str, float], amount_usd: float) -> dict:
    """
    Records a manually-confirmed real-world payout -- the money already
    moved outside this code; this just tells the accounting to stop
    counting it again. Snapshots each SPLIT_BOTS bot's current all-time
    profit as the new baseline (so the next cycle's per-bot deltas start
    from zero again), appends one history entry, and resets
    baseline_set_at to now. One event covers the whole portfolio at
    once, matching how a real payout actually happens (one withdrawal,
    not five separate ones).
    """
    state = load_state(data_dir)
    now = datetime.now(timezone.utc).isoformat()
    total_profit_at_confirm = round(sum(per_bot_profit_now.values()), 2)

    state["history"].append({
        "timestamp": now,
        "amount_usd": amount_usd,
        "per_bot_profit_at_confirm": {k: round(v, 2) for k, v in per_bot_profit_now.items()},
        "total_profit_at_confirm": total_profit_at_confirm,
    })
    state["bots"] = dict(per_bot_profit_now)
    state["baseline_set_at"] = now
    save_state(data_dir, state)
    return state
