"""
P20.4 part A: retired bots, stale/unreachable bots, partial data, dry_run scope, stuck-pause reset, counterfactual cut-off.
The 02.10 incident is replayed with fakes: Dip stopped, listing_fade silent once, trend_v2 left paused.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ALLOCATOR_DIR"] = tempfile.mkdtemp()
for k in ("PORTFOLIO_BRAIN_MODE", "ALLOCATOR_RETIRED_BOTS", "ALLOCATOR_RETIRED_FROZEN", "ALLOCATOR_EXCLUDED_BOTS"):
    os.environ.pop(k, None)

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import allocator as al  # noqa: E402
from backend.engine import bot_state as bs  # noqa: E402
from backend.engine import portfolio_brain as pb  # noqa: E402
from backend.engine import runtime_settings as rs  # noqa: E402
from backend.engine import signal_audit as sa  # noqa: E402

NOW = datetime(2026, 10, 4, 0, 40, tzinfo=timezone.utc)
EQUITY = {"main": 500.0, "shorts": 480.0, "moonshot": 90.0, "listing_fade": 98.0,
          "trend_ensemble": 105.0, "trend_v2": 520.0, "control_long": 500.0, "control_short": 500.0}
ENV_RET = {"PORTFOLIO_BRAIN_MODE": "enforce", "ALLOCATOR_RETIRED_BOTS": "moonshot"}


class Fleet:
    def __init__(self, down=(), paused=(), dry=None):
        self.down, self.calls, self.cfg_calls = set(down), [], []
        self.state = {b: "running" for b in EQUITY}
        for b in paused:
            self.state[b] = "paused"
        self.dry = {b: True for b in EQUITY}
        self.dry.update(dry or {})

    def reachable(self, b):
        self.calls.append(("reachable", b))
        return b not in self.down

    def balance(self, b):
        self.calls.append(("balance", b))
        return {"total": EQUITY[b], "starting_capital": pb.DEFAULT_START_CAPITAL[b]}

    def status(self, b):
        self.calls.append(("status", b))
        return []

    def trades(self, b, **k):
        self.calls.append(("trades", b))
        return []

    def _json(self, b, p):
        self.calls.append(("json", b, p))
        if b in self.down:
            return None
        return {"dry_run": self.dry[b], "state": self.state[b], "max_open_trades": 5} if p.endswith("show_config") else None


class Ctl:
    def __init__(self, fleet):
        self.f, self.calls = fleet, []

    def pause(self, b):
        al.assert_manageable(b)
        self.calls.append(("stopentry", b))
        self.f.state[b] = "paused"
        return True, "ok"

    def resume(self, b):
        al.assert_manageable(b)
        self.calls.append(("start", b))
        self.f.state[b] = "running"
        return True, "ok"

    def reload(self, b):
        al.assert_manageable(b)
        self.calls.append(("reload_config", b))
        return True, "ok"


def llm_all(w=0.8):
    return lambda s, u: json.dumps({b: {"weight": w, "reason": "r"} for b in al.MANAGED_BOTS})


def seeded(dip_alive_days_ago=2):
    """A data dir shaped like production on 02.10: brain snapshots and a cache from days when every bot answered."""
    d = Path(tempfile.mkdtemp())
    pb_full = pb.run_daily(d, now=NOW - timedelta(days=dip_alive_days_ago), fetcher=Fleet(), llm=lambda s, u: None,
                           env={"PORTFOLIO_BRAIN_MODE": "shadow"}, http_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    return d, pb_full


# ---------------------------------------------------------------- the incident, replayed
d, full = seeded()
eq_full, budget_full = full["capital"]["equity"], full["capital"]["cppi_budget"]
check("baseline: with every bot answering, equity ~ $1.8k and the budget is positive", eq_full > 1700 and budget_full > 0)
# Dip is gone, trend_v2 was left paused on 02.10; every active bot answers
fleet = Fleet(down=["moonshot"], paused=["trend_v2"])
ctl = Ctl(fleet)
(d / al.STATE_FILE).write_text(json.dumps({"weights": {b: 0.0 for b in al.MANAGED_BOTS}, "paused_by_allocator": ["trend_v2"], "last_effective_mode": "enforce"}))
out = al.run_allocation(d, NOW, fleet, llm_all(), ctl, env=ENV_RET)
rec = json.loads((d / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])
check("ACCEPTANCE: partial=false and missing_bots=[] with Dip retired and listing_fade silent", rec["portfolio"]["partial"] is False and rec["portfolio"]["missing_bots"] == [])
check("ACCEPTANCE: equity stays around the pre-incident value (Dip frozen) and the budget is NOT 0",
      abs(rec["portfolio"]["equity"] - eq_full) < 1.0 and rec["portfolio"]["budget"] > 0 and abs(rec["portfolio"]["budget"] - budget_full) < 5)
check("ACCEPTANCE: effective_mode=enforce with no downgrade reasons (a retired bot's dry_run is not asked)", out["effective_mode"] == "enforce" and out["mode_reasons"] == [])
check("no false floor breach and no zeroed weights: the rules that fired are not cppi_floor_breached", "cppi_floor_breached" not in rec["rules_fired"]
      and all(w > 0 for b, w in rec["final"].items() if b not in ("moonshot",)))
check("RETIRED: no REST call of any kind went to moonshot (reachable, balance, status, trades, show_config)", not any(c[1] == "moonshot" for c in fleet.calls))
check("RETIRED: the retired bot is not in the dry_run check and does not get a pause/resume", "moonshot" not in (rec["dry_run_check"] or {}).get("dry_run", {})
      and all(b != "moonshot" for _, b in ctl.calls))
# one active bot silent for a run: stale, not missing; and since its dry_run cannot be read this run, enforce degrades to shadow naming IT
fl_s = Fleet(down=["moonshot", "listing_fade"])
os_ = al.run_allocation(d, NOW + timedelta(hours=1), fl_s, llm_all(), Ctl(fl_s), env=ENV_RET)
check("STALE: listing_fade silent for one run -> stale_bots, not missing, not partial; the budget stays", os_["stale_bots"] == ["listing_fade"] and os_["portfolio"]["partial"] is False
      and os_["portfolio"]["missing_bots"] == [] and os_["portfolio"]["budget"] > 0)
check("STALE: its unverifiable dry_run keeps that run in shadow and the reason names listing_fade only",
      os_["effective_mode"] == "shadow" and os_["mode_reasons"] == ["dry_run not verifiable on ['listing_fade'] (active, not retired)"])
check("RETIRED is recorded: the decision names it with how it was frozen", rec["retired"] == ["moonshot"] and rec["retired_bots"]["moonshot"]["method"] == "frozen_constant")
check("STUCK PAUSE RESET: trend_v2 (paused_by_allocator, weight now > 0) is resumed, with verified_running in the log",
      ("start", "trend_v2") in ctl.calls and fleet.state["trend_v2"] == "running"
      and any(a["bot"] == "trend_v2" and a["action"] == "start" and a.get("verified_running") is True for a in rec["actions"])
      and json.loads((d / al.STATE_FILE).read_text())["paused_by_allocator"] == [])
llm_in = al.build_llm_input({"bots": {}, "equity": 1, "hwm": 1, "floor": 1, "budget": 1, "gross": 0, "drawdown": {}, "targets": []}, {}, {b: 1.0 for b in al.MANAGED_BOTS},
                            al.active_bots({"retired": ["moonshot"]}))
check("LLM input: managed_bots excludes moonshot, retired list names it, no weight for it", "moonshot" not in llm_in["managed_bots"] and llm_in["retired_bots_not_to_be_weighted"] == ["moonshot"]
      and "moonshot" not in llm_in["current_weights"])
v = al.validate_llm(json.dumps({"moonshot": {"weight": 0.0, "reason": "x"}, "main": {"weight": 0.5, "reason": "y"}}), {b: 1.0 for b in al.MANAGED_BOTS}, ["moonshot"])
check("a retired bot named in a reply is ignored (noted), the rest is used", v["ok"] and "moonshot" not in v["reasons"] and v["weights"]["moonshot"] == 1.0 and any("retired bot" in n for n in v["notes"]))
w = json.loads((Path(os.environ["ALLOCATOR_DIR"]) / al.WEIGHTS_FILE).read_text())
check("the weights file holds the retired bot at 1.0 and no control bot or kronos", w["weights"]["moonshot"]["weight"] == 1.0 and set(w["weights"]) == set(al.MANAGED_BOTS))

# ---------------------------------------------------------------- freeze mechanics
frozen = json.loads((d / bs.RETIRED_FILE).read_text())
check("FREEZE: taken from the last brain snapshot that still had the bot, source recorded", frozen["moonshot"]["frozen_equity"] == EQUITY["moonshot"] and "snapshot" in frozen["moonshot"]["source"])
snaps = pb.load_snapshots(d)
check("FREEZE: equity / reference are comparable with the pre-incident snapshots (same level, floor not hit)", abs(snaps[-1]["equity"] - snaps[0]["equity"]) < 1.0)
ov = bs.freeze_retired(d, ["moonshot"], NOW, {"ALLOCATOR_RETIRED_FROZEN": "moonshot:431.5"})
check("FREEZE: an operator override (the final balance) replaces the value and is recorded as such", ov["moonshot"]["frozen_equity"] == 431.5 and "override" in ov["moonshot"]["source"])
check("FREEZE: a retired bot with nothing to freeze is dropped from the reference instead", bs.contribution({"x": {"frozen_equity": None, "start_capital": 500.0}}) == (0.0, 500.0)
      and bs.contribution({"x": {"frozen_equity": 420.0, "start_capital": 500.0}}) == (420.0, 0.0))
check("retired list parsing: only managed bots count, control bots cannot be retired", bs.retired_from_env({"ALLOCATOR_RETIRED_BOTS": "moonshot, control_long ,main,zzz"}) == ["moonshot", "main"])

# ---------------------------------------------------------------- one silent bot never zeroes the budget
d2, full2 = seeded(0)
r_stale = pb.run_daily(d2, now=NOW + timedelta(hours=2), fetcher=Fleet(down=["listing_fade"]), llm=lambda s, u: None, env={"PORTFOLIO_BRAIN_MODE": "shadow"},
                       http_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
check("BRAIN: one silent bot -> stale, not partial, budget intact", r_stale["stale_bots"] == ["listing_fade"] and not r_stale["partial"]
      and r_stale["capital"]["cppi_budget"] == full2["capital"]["cppi_budget"])
r_un = pb.run_daily(d2, now=NOW + timedelta(days=2, hours=2), fetcher=Fleet(down=["listing_fade"]), llm=lambda s, u: None, env={"PORTFOLIO_BRAIN_MODE": "shadow"},
                    http_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
check("BRAIN: silent for more than 24h -> unreachable (not a drawdown: equity unchanged)", r_un["unreachable_bots"] == ["listing_fade"]
      and r_un["capital"]["equity"] == full2["capital"]["equity"] and r_un["drawdown"]["drawdown_from_hwm_pct"] is not None)

# ---------------------------------------------------------------- partial data: warnings only
FACT = {"drawdown": {"highest_rung": {"level": "L3 (-8% from HWM)"}, "weekly_limit_breached": True, "monthly_limit_breached": True}, "gross": 900.0,
        "budget": 0.0, "equity": 100.0, "open_trades": {}, "macro_soon": False, "coin_limit": 0.25}
prev1 = {b: 1.0 for b in al.MANAGED_BOTS}
rp = al.apply_hard_rules({b: 0.9 for b in al.MANAGED_BOTS}, prev1, {**FACT, "partial": True})
check("PARTIAL: ladder L3, weekly, monthly and a zero budget (the floor-breach case) do NOT cut any weight", set(rp["final"].values()) == {0.9}
      and "cppi_floor_breached" not in rp["rules_fired"] and not any(r.startswith("ladder") for r in rp["rules_fired"]))
check("PARTIAL: the report carries warnings saying which rules would have fired", any("ladder/loss" in w for w in rp["warnings"]) and any("CPPI" in w for w in rp["warnings"]))
rf = al.apply_hard_rules({b: 0.9 for b in al.MANAGED_BOTS}, prev1, {**FACT, "partial": False})
check("COMPLETE data: the same facts DO zero everything (floor breach is real when every active bot is accounted for)", set(rf["final"].values()) == {0.0} and rf["warnings"] == [])
check("PARTIAL: the non-equity rules still work (macro cap 0.5)", max(al.apply_hard_rules({b: 1.0 for b in al.MANAGED_BOTS}, prev1, {**FACT, "partial": True, "macro_soon": True})["final"].values()) == 0.5)
check("PARTIAL: the daily step limit still works", min(al.apply_hard_rules({b: 0.0 for b in al.MANAGED_BOTS}, prev1, {**FACT, "partial": True})["final"].values()) == 0.75)
dn = Path(tempfile.mkdtemp())          # a never-seen active bot makes the data partial
(dn / pb.LATEST_FILE).write_text(json.dumps({"evidence": {}}))
on = al.run_allocation(dn, NOW, Fleet(down=["main"]), llm_all(0.9), Ctl(Fleet()), env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("PARTIAL end to end: a never-seen bot -> partial=true, missing named, weights NOT zeroed, warning in the record",
      on["portfolio"]["partial"] is True and on["portfolio"]["missing_bots"] == ["main"] and min(on["final"].values()) > 0)

# ---------------------------------------------------------------- dry_run scope and reason
fl = Fleet(down=["trend_v2"])
dn2 = Path(tempfile.mkdtemp())
(dn2 / pb.LATEST_FILE).write_text(json.dumps({"evidence": {}}))
o2 = al.run_allocation(dn2, NOW, fl, llm_all(), Ctl(fl), env={"PORTFOLIO_BRAIN_MODE": "enforce", "ALLOCATOR_RETIRED_BOTS": "moonshot"})
check("DRY_RUN: an unverifiable ACTIVE bot keeps the run in shadow and the reason names exactly that bot (not the retired one)",
      o2["effective_mode"] == "shadow" and o2["mode_reasons"] == ["dry_run not verifiable on ['trend_v2'] (active, not retired)"])
fl = Fleet(dry={"moonshot": False})
o3 = al.run_allocation(dn2, NOW + timedelta(days=1), fl, llm_all(), Ctl(fl), env=ENV_RET)
check("DRY_RUN: dry_run=false on a RETIRED bot does not block enforce", o3["effective_mode"] == "enforce" and o3["mode_reasons"] == [])
fl = Fleet(dry={"shorts": False})
o4 = al.run_allocation(dn2, NOW + timedelta(days=2), fl, llm_all(), Ctl(fl), env=ENV_RET)
check("DRY_RUN: dry_run=false on an active bot still blocks, naming it", o4["effective_mode"] == "shadow" and "shorts" in o4["mode_reasons"][0])
refused = False
try:
    al.startup_guard(Fleet(dry={"moonshot": False}), ENV_RET)
except al.EnforceRefused:
    refused = True
check("STARTUP GUARD: a retired bot with dry_run=false does not stop the service from starting", not refused)
refused = False
try:
    al.startup_guard(Fleet(dry={"main": False}), ENV_RET)
except al.EnforceRefused:
    refused = True
check("STARTUP GUARD: an active bot with dry_run=false still does", refused)

# ---------------------------------------------------------------- counterfactual cut-off
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def arec(ts, w):
    return {"ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "effective_mode": "shadow", "final": {b: w for b in al.MANAGED_BOTS}}


def trade(open_dt, pnl):
    return {"is_open": False, "open_date": open_dt.isoformat(), "close_date": (open_dt + timedelta(hours=3)).isoformat(), "close_profit_abs": pnl}


allocs = [arec(T0, 0.0), arec(T0 + timedelta(days=1), 0.0), arec(T0 + timedelta(days=2), 0.5)]
trades = {"main": [trade(T0 + timedelta(hours=5), 10.0), trade(T0 + timedelta(days=2, hours=1), 10.0)]}
cf_all = al.counterfactual(trades, allocs, T0 + timedelta(days=3))
cf_cut = al.counterfactual(trades, allocs, T0 + timedelta(days=3), since=T0 + timedelta(days=2))
check("COUNTERFACTUAL cut-off: the incident days are marked excluded: retired_bot_incident (listed, not deleted)",
      cf_cut["excluded"]["reason"] == "retired_bot_incident" and cf_cut["excluded"]["decisions"] == 2 and cf_cut["counted_since"] == "2026-10-03T00:00:00Z")
check("COUNTERFACTUAL cut-off: only the interval after the cut-off is counted (the zero-weight days no longer distort it)",
      cf_cut["with_allocator"]["net_pnl_usd"] == 5.0 and cf_cut["without_allocator"]["net_pnl_usd"] == 10.0 and cf_cut["trades_used"] == 1
      and cf_all["with_allocator"]["net_pnl_usd"] == 5.0 and cf_all["trades_used"] == 2)
check("COUNTERFACTUAL: without a cut-off nothing is excluded", "excluded" not in cf_all)
st_ = json.loads((d / al.STATE_FILE).read_text())
check("the first run that knows about the retirement sets cf_since; the file keeps the old decisions", st_["cf_since"] == "2026-10-04T00:40:00Z"
      and len((d / al.ALLOCATIONS_FILE).read_text().splitlines()) >= 1)
cf_file = json.loads((d / al.COUNTERFACTUAL_FILE).read_text())
check("allocator_counterfactual.json starts at the cut-off", cf_file.get("counted_since") in (None, "2026-10-04T00:40:00Z"))

# ---------------------------------------------------------------- kill switch and the retired bot
dk = Path(tempfile.mkdtemp())
fk = Fleet(paused=["main"])
ck = Ctl(fk)
(dk / al.STATE_FILE).write_text(json.dumps({"weights": {}, "paused_by_allocator": ["main", "moonshot"]}))
os.environ["ALLOCATOR_RETIRED_BOTS"] = "moonshot"
al.kill(dk, "x", "ui", ck, fk, NOW)
check("KILL SWITCH never contacts a retired bot (and drops it from the tracked pauses)", all(b != "moonshot" for _, b in ck.calls) and ("start", "main") in ck.calls
      and "moonshot" not in json.loads((dk / al.STATE_FILE).read_text())["paused_by_allocator"])
os.environ.pop("ALLOCATOR_RETIRED_BOTS")

# ---------------------------------------------------------------- settings / audit / never-managed
check("runtime switches exist for retired bots and the frozen-balance override (UI + env)", "ALLOCATOR_RETIRED_BOTS" in rs.SPEC and "ALLOCATOR_RETIRED_FROZEN" in rs.SPEC)
dd = Path(tempfile.mkdtemp())
check("retired switch accepts managed bots only and the override format moonshot:412.35",
      rs.save({"ALLOCATOR_RETIRED_BOTS": "moonshot"}, dd)["ALLOCATOR_RETIRED_BOTS"]["value"] == "moonshot"
      and rs.save({"ALLOCATOR_RETIRED_FROZEN": "moonshot:412.35"}, dd)["ALLOCATOR_RETIRED_FROZEN"]["value"] == "moonshot:412.35")
for bad in ("control_long", "moonshot:abc", "zzz:10", "moonshot:-5"):
    key = "ALLOCATOR_RETIRED_BOTS" if ":" not in bad else "ALLOCATOR_RETIRED_FROZEN"
    raised = False
    try:
        rs.save({key: bad}, dd)
    except ValueError:
        raised = True
    check(f"retired switch rejects {bad!r}", raised)
check("kronos is never managed (control bots and trend_ensemble too)", set(al.NEVER_MANAGED) == {"control_long", "control_short", "trend_ensemble", "kronos"} and "kronos" not in al.MANAGED_BOTS)
check("the signal audit does not poll a retired bot for candles (source check)", "if src in retired" in (REPO / "backend/engine/signal_audit.py").read_text(encoding="utf-8"))
html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("UI: retired bots, frozen override, stale/unreachable/partial lines are shown", all(s in html for s in ("allocRet", "set_ALLOCATOR_RETIRED_FROZEN", "Stale (no answer", "Unreachable &gt;24h", "Incomplete data")))
import yaml  # noqa: E402

env_lines = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["survival-agent"]["environment"]
check("COMPOSE: no bot is retired by default (ALLOCATOR_RETIRED_BOTS=${...:-}); the UI or Portainer value wins", "ALLOCATOR_RETIRED_BOTS=${ALLOCATOR_RETIRED_BOTS:-}" in env_lines)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P20.4 ROSTER CHECKS PASSED")
