"""
P6-addon: unit tests for the new payout/split accounting --
backend/engine/payout_state.py, the survival_cost months-scaling fix in
backend/engine/split_calc.py, and loop.run_cycle()'s per-bot
aggregation. Same house style as backtest/test_p5_second_opinion.py --
a self-contained script, no pytest, prints OK/FAIL per check.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.engine import payout_state as ps  # noqa: E402
from backend.engine import loop as engine_loop  # noqa: E402
from backend.engine.split_calc import compute_split  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- split_calc: survival_cost now scales with months_elapsed ---
config = {"survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}

split_1mo = compute_split(500, 1, config)
check("compute_split: 1 month elapsed -> survival_cost == 1x survival_monthly (60)",
      split_1mo["survival_cost"] == 60)

split_3mo = compute_split(500, 3, config)
check("compute_split: 3 months elapsed -> survival_cost == 3x survival_monthly (180), not 1x (P6-addon fix)",
      split_3mo["survival_cost"] == 180)
check("compute_split: profit_after_survival reflects the scaled cost (500-180=320)",
      split_3mo["profit_after_survival"] == 320)

split_0mo = compute_split(500, 0, config)
check("compute_split: months_elapsed clamped to >=1 even if passed 0",
      split_0mo["survival_cost"] == 60)


# --- payout_state: fresh state, no plan_started_at.txt yet ---
with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    state = ps.load_state(data_dir)
    check("load_state (fresh, no marker): all SPLIT_BOTS baselines are 0.0",
          all(state["bots"][b] == 0.0 for b in ps.SPLIT_BOTS))
    check("load_state (fresh, no marker): history starts empty", state["history"] == [])
    check("load_state (fresh, no marker): writes plan_started_at.txt as the baseline source",
          (data_dir / ps.PLAN_STARTED_AT_FILENAME).exists())
    check("load_state (fresh, no marker): payout_state.json persisted to disk",
          (data_dir / ps.PAYOUT_STATE_FILENAME).exists())

    state2 = ps.load_state(data_dir)
    check("load_state: second call reads the same persisted state back, doesn't reset it",
          state2["baseline_set_at"] == state["baseline_set_at"])

# --- payout_state: reuses an EXISTING plan_started_at.txt marker ---
with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    existing_marker = "2026-01-01T00:00:00+00:00"
    (data_dir / ps.PLAN_STARTED_AT_FILENAME).write_text(existing_marker)
    state = ps.load_state(data_dir)
    check("load_state: reuses an existing plan_started_at.txt marker as baseline_set_at (compat)",
          state["baseline_set_at"] == existing_marker)

# --- months_since ---
old_ts = (datetime.now(timezone.utc) - timedelta(days=95)).isoformat()
check("months_since: ~95 days ago -> 3 months (95//30 + 1)", ps.months_since(old_ts) == 4)
recent_ts = datetime.now(timezone.utc).isoformat()
check("months_since: just now -> floored at 1, never 0", ps.months_since(recent_ts) == 1)

# --- extract_profit ---
check("extract_profit: None -> 0.0", ps.extract_profit(None) == 0.0)
check("extract_profit: prefers profit_closed_coin when present",
      ps.extract_profit({"profit_closed_coin": 12.5, "profit_all_coin": 999}) == 12.5)
check("extract_profit: falls back through the field list", ps.extract_profit({"profit_all_fiat": 7}) == 7.0)
check("extract_profit: unrecognized shape -> 0.0, not a crash", ps.extract_profit({"unrelated_field": 1}) == 0.0)

# --- confirm_payout: snapshots baseline, resets bots, appends history ---
with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    original_baseline = ps.load_state(data_dir)["baseline_set_at"]
    per_bot_now = {"main": 100.0, "shorts": 50.0, "moonshot": -10.0}
    result_state = ps.confirm_payout(data_dir, per_bot_now, amount_usd=33.0)

    check("confirm_payout: bots reset to the snapshotted current profit", result_state["bots"] == per_bot_now)
    check("confirm_payout: baseline_set_at moved forward, no longer the original plan-start marker",
          result_state["baseline_set_at"] != original_baseline)
    check("confirm_payout: exactly one history entry appended", len(result_state["history"]) == 1)
    entry = result_state["history"][0]
    check("confirm_payout: history entry records the confirmed amount_usd", entry["amount_usd"] == 33.0)
    check("confirm_payout: history entry's total_profit_at_confirm == sum of per-bot profits (140.0), "
          "no floor(0) per bot (moonshot's -10 genuinely reduces the total)",
          entry["total_profit_at_confirm"] == 140.0)
    check("confirm_payout: per_bot_profit_at_confirm matches what was passed in",
          entry["per_bot_profit_at_confirm"] == per_bot_now)

    # A cycle right after confirm should see zero delta for every bot.
    reloaded = ps.load_state(data_dir)
    check("confirm_payout: a fresh load_state() after confirm sees the new baseline, not the old one",
          reloaded["bots"] == per_bot_now)


# --- run_cycle: per-bot aggregation, no floor(0), unavailable-bot handling ---
def _fake_summary(profit_by_bot: dict, unavailable: set[str] = frozenset()):
    def _get(bot_id: str = "main"):
        if bot_id in unavailable:
            return {"available": False, "reason": "test-forced unavailable"}
        return {"available": True, "profit": {"profit_closed_coin": profit_by_bot.get(bot_id, 0.0)}}
    return _get


with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    ps.load_state(data_dir)  # baselines all 0.0

    profits = {"main": 100.0, "shorts": -20.0, "moonshot": 30.0}
    with mock.patch.object(engine_loop, "get_summary", side_effect=_fake_summary(profits)):
        result = engine_loop.run_cycle(data_dir, {"survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50})

    check("run_cycle: per_bot deltas match each bot's current profit vs a 0.0 baseline",
          result["per_bot"] == profits)
    check("run_cycle: gross_profit_total sums WITHOUT floor(0) per bot (shorts' -20 counts as a real drag)",
          result["gross_profit_total"] == 110.0)  # 100 - 20 + 30
    check("run_cycle: unavailable_bots empty when every bot responded", result["unavailable_bots"] == [])
    check("run_cycle: outcome is 'reviewed' when at least one bot is reachable", result["outcome"] == "reviewed")

with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    ps.load_state(data_dir)
    with mock.patch.object(engine_loop, "get_summary", side_effect=_fake_summary(profits, unavailable={"main", "shorts", "moonshot"})):
        result = engine_loop.run_cycle(data_dir, {"survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50})
    check("run_cycle: outcome is 'skipped' when every SPLIT_BOTS bot is unreachable",
          result["outcome"] == "skipped")

with tempfile.TemporaryDirectory() as tmp:
    data_dir = Path(tmp)
    ps.load_state(data_dir)
    with mock.patch.object(engine_loop, "get_summary", side_effect=_fake_summary(profits, unavailable={"shorts"})):
        result = engine_loop.run_cycle(data_dir, {"survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50})
    check("run_cycle: one unreachable bot lands in unavailable_bots but the cycle still runs",
          result["unavailable_bots"] == ["shorts"] and result["outcome"] == "reviewed")
    check("run_cycle: unreachable bot's delta is 0.0 (unchanged from baseline), not dropped from the total",
          result["per_bot"]["shorts"] == 0.0)


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL PAYOUT STATE (P6-addon) CHECKS PASSED")
