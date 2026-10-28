"""
P24 fix: the portfolio brain's high-water mark and weekly/monthly change only count the bots that are in the book today.
Real case (2026-10-08): two removed bots took ~$785 out of a $2,550 book, the brain read it as a -31% drawdown, ladder L3 fired and the allocator (enforce) paused every bot.
Offline: fake bots, temp data dir.
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import portfolio_brain as pb  # noqa: E402

ROSTER = ["listing_fade", "main", "moonshot", "shorts", "trend_ensemble", "trend_v2"]
NOW = datetime(2026, 10, 11, 0, 10, tzinfo=timezone.utc)
EQ = {"main": 478.76, "shorts": 495.36, "moonshot": 97.76, "listing_fade": 115.07, "trend_ensemble": 91.49, "trend_v2": 487.89, "control_long": 476.0, "control_short": 501.0}


class Fake:
    def reachable(self, bot):
        return True

    def balance(self, bot):
        return {"total": EQ[bot], "starting_capital": pb.DEFAULT_START_CAPITAL.get(bot, 500)}

    def status(self, bot):
        return []

    def trades(self, bot):
        return []


LLM = lambda system, user: json.dumps({"regime_note": "x", "flags": [], "proposals": []})  # noqa: E731
HTTP = type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"result": {"list": [["1", "100", "0", "0", "110"], ["0", "100", "0", "0", "100"]]}}})()

# snapshots as on the server: 9 bots until 2026-10-07, then 6 (dip and two survival bots removed); hwm_at / state written by the OLD code (no hwm_roster)
OLD = {"dip": 481.56, "survival": 200.0, "survival_shorts": 103.0}
six = {"2026-09-30": 1785.62, "2026-10-04": 1777.95, "2026-10-05": 1776.28, "2026-10-07": 1773.34, "2026-10-08": 1762.36, "2026-10-09": 1753.94, "2026-10-10": 1758.02}


def snap(date, total, with_old):
    per = {b: round(total / 6, 2) for b in ROSTER}
    bots = {**per, **(OLD if with_old else {})}
    return {"ts": f"{date}T00:10:00+00:00", "date": date, "equity": round(sum(bots.values()), 2), "hwm": 2573.88, "partial": False, "bots": bots, "retired_frozen": {}}


with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    snaps = [snap(day, tot, day <= "2026-10-07") for day, tot in six.items()]
    (d / pb.SNAPSHOTS_FILE).write_text("\n".join(json.dumps(s) for s in snaps) + "\n", encoding="utf-8")
    (d / pb.STATE_FILE).write_text(json.dumps({"hwm": 2573.88, "hwm_at": "2026-09-30T00:10:00+00:00", "control_first_seen": "2026-09-30T00:10:00+00:00"}), encoding="utf-8")
    check("SETUP: the old snapshots really are 9-bot (equity ~2550) before the removal", snaps[0]["equity"] > 2500 and snaps[-1]["equity"] < 1800)

    rest = pb.restate_snapshots(snaps, ROSTER)
    check("RESTATE: every old snapshot is re-expressed for today's six bots (~1,780, not ~2,570)", len(rest) == len(snaps) and all(1700 < s["equity"] < 1800 for s in rest))
    check("RESTATE: a snapshot that lacks one of today's bots is dropped, not guessed", pb.restate_snapshots([{"bots": {"main": 1.0}, "retired_frozen": {}}], ROSTER) == [])

    rep = pb.run_daily(d, now=NOW, fetcher=Fake(), llm=LLM, env={}, http_get=lambda *a, **k: HTTP)
    cap, dd = rep["capital"], rep["drawdown"]
    check("HWM: rebased to today's bots only (about 1,786, not 2,573.88), with a note saying so", 1780 < cap["hwm"] < 1800 and cap["hwm_rebased"] and "roster changed" in cap["hwm_rebased"])
    check("DRAWDOWN: about -1%, no ladder rung (it was -31% and L3)", -3 < dd["drawdown_from_hwm_pct"] < 0 and dd["highest_rung"] is None and not dd["rungs_triggered"])
    check("WEEKLY: compared with the same six bots a week ago (about -0.6%), not with the old book", dd["weekly_change_pct"] is not None and -3 < dd["weekly_change_pct"] < 1 and not dd["weekly_limit_breached"])
    st = json.loads((d / pb.STATE_FILE).read_text(encoding="utf-8"))
    check("STATE: the roster is stored with the mark", st["hwm_roster"] == ROSTER and st["hwm"] < 1800)
    saved = [json.loads(line) for line in (d / pb.SNAPSHOTS_FILE).read_text(encoding="utf-8").splitlines()]
    check("HISTORY: the stored snapshots stay raw (the old 9-bot rows are kept as they were) and today's is appended", len(saved) == len(snaps) + 1 and saved[0]["equity"] == snaps[0]["equity"])

    rep2 = pb.run_daily(d, now=NOW + timedelta(days=1), fetcher=Fake(), llm=LLM, env={}, http_get=lambda *a, **k: HTTP)
    check("NEXT DAY: same roster -> no rebase note, the mark is kept", rep2["capital"]["hwm_rebased"] is None and 1780 < rep2["capital"]["hwm"] < 1800)

    EQ["main"] = 300.0                      # a REAL loss of the same roster must still show as a drawdown
    rep3 = pb.run_daily(d, now=NOW + timedelta(days=2), fetcher=Fake(), llm=LLM, env={}, http_get=lambda *a, **k: HTTP)
    check("REAL LOSS: a genuine drop with an unchanged roster still triggers the ladder", rep3["drawdown"]["drawdown_from_hwm_pct"] < -8 and rep3["drawdown"]["highest_rung"] is not None)

# ---- the allocator has its own copy of the state (gather_state): it must use the same roster-aware history
from backend.engine import allocator as al  # noqa: E402

with tempfile.TemporaryDirectory() as td2:
    d2 = Path(td2)
    snaps2 = [snap(day, tot, day <= "2026-10-07") for day, tot in six.items()]
    (d2 / pb.SNAPSHOTS_FILE).write_text("\n".join(json.dumps(s) for s in snaps2) + "\n", encoding="utf-8")
    (d2 / pb.STATE_FILE).write_text(json.dumps({"hwm": 1790.32, "hwm_roster": ROSTER}), encoding="utf-8")     # state already fixed by the brain
    EQ["main"] = 478.76
    st2 = al.gather_state(Fake(), d2, datetime(2026, 10, 12, 9, 35, tzinfo=timezone.utc), al.load_settings({}), with_trades=False)
    dd2 = st2["drawdown"]
    check("ALLOCATOR: weekly change is measured against the same six bots (not the old 9-bot 2,550) -> no weekly-loss breach", dd2["weekly_change_pct"] is not None and dd2["weekly_change_pct"] > -3 and not dd2["weekly_limit_breached"])
    check("ALLOCATOR: no ladder rung from the roster change", dd2["highest_rung"] is None)
    (d2 / pb.STATE_FILE).write_text(json.dumps({"hwm": 2573.88}), encoding="utf-8")                            # old-code state without a roster
    st3 = al.gather_state(Fake(), d2, datetime(2026, 10, 12, 9, 35, tzinfo=timezone.utc), al.load_settings({}), with_trades=False)
    check("ALLOCATOR: an old state with the 9-bot mark is rebased on its own (about 1,790, not 2,573.88)", 1700 < st3["hwm"] < 1800 and st3["drawdown"]["highest_rung"] is None)

print("\nALL P24 HWM CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
