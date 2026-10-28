"""
P20.1 ops tooling: the live pause/resume test driver and the 6-hourly enforce watch, against fakes of a Freqtrade bot
(both pause semantics), plus the Dockerfile pin and the control-bot guarantees.
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

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import allocator as al  # noqa: E402
from backend.engine import market_context as mc  # noqa: E402
from backend.engine import portfolio_brain as pb  # noqa: E402
from backend.ops import p20_enforce_check as chk  # noqa: E402
from backend.ops import p20_live_test as lt  # noqa: E402

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
QUIET = lambda *a, **k: None  # noqa: E731


class Fleet:
    """One or many bots with Freqtrade's REST behaviour: 2026.8 (paused state) or legacy (max_open_trades zeroed)."""

    def __init__(self, legacy=False, dry=True, mot=7, bots=None):
        self.legacy, self.dry, self.mot0 = legacy, dry, mot
        self.bots = bots or ["moonshot"]
        self.state = {b: "running" for b in self.bots}
        self.mot = {b: mot for b in self.bots}
        self.trades = {b: 10 for b in self.bots}
        self.reload_restores = True
        self.log = []

    def _json(self, bot, path):
        if bot not in self.state:
            return None
        if path == "/api/v1/show_config":
            return {"dry_run": self.dry, "state": self.state[bot], "max_open_trades": self.mot[bot]}
        if path.startswith("/api/v1/trades"):
            return {"total_trades": self.trades[bot], "trades": []}
        return None


class Ctl:
    def __init__(self, fleet):
        self.f, self.calls = fleet, []

    def pause(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("stopentry", bot))
        if self.f.legacy:
            self.f.mot[bot] = 0
        else:
            self.f.state[bot] = "paused"
        return True, "ok"

    def resume(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("start", bot))
        if not self.f.legacy:
            self.f.state[bot] = "running"
        return True, "already running" if self.f.legacy else "ok"

    def reload(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("reload_config", bot))
        if self.f.reload_restores:
            self.f.mot[bot] = self.f.mot0
        return True, "ok"


def run_lt(fleet, ctl=None, script=None, **kw):
    ctl = ctl or Ctl(fleet)
    t = [0]
    script = script or {}

    def sleep(s):
        t[0] += s
        for at, fn in list(script.items()):
            if t[0] >= at:
                fn()
                del script[at]
    rep = lt.run("moonshot", fleet, ctl, sleep=sleep, watch1_s=600, watch2_s=600, poll_s=60, now=lambda: NOW, say=QUIET, **kw)
    return rep, ctl


# ---------------------------------------------------------------- live test, 2026.8 semantics
f = Fleet()
rep, ctl = run_lt(f, script={900: lambda: f.trades.__setitem__("moonshot", 11)})        # an entry arrives 5 min into phase 2
check("LIVE TEST (2026.8): passes; pause semantics recorded as state_paused; /start alone resumed it (no reload)",
      rep["passed"] and rep["exit"] == 0 and rep["pause_semantics"] == "state_paused" and rep["resume_used_reload_config"] is False
      and ctl.calls == [("stopentry", "moonshot"), ("start", "moonshot")])
check("LIVE TEST: max_open_trades after resume equals the recorded value, state running", f.mot["moonshot"] == 7 and f.state["moonshot"] == "running"
      and any(s["step"].startswith("3b") and s["ok"] for s in rep["steps"]))
check("LIVE TEST: entries returned after resume -> 'proven'", rep["entries_return"] == "proven")
f = Fleet()
rep, ctl = run_lt(f)
check("LIVE TEST: no entry in either phase -> passes state-level checks but says INCONCLUSIVE (does not claim proof)",
      rep["passed"] and rep["entries_return"] == "inconclusive" and "INCONCLUSIVE" in json.dumps(rep["steps"]))

# ---------------------------------------------------------------- legacy semantics
f = Fleet(legacy=True)
rep, ctl = run_lt(f)
check("LIVE TEST (legacy): max_open_trades zeroed is detected as the pause semantics; /start is a no-op so /reload_config follows and restores 7",
      rep["passed"] and rep["pause_semantics"] == "max_open_trades_zeroed" and rep["resume_used_reload_config"] is True
      and ctl.calls == [("stopentry", "moonshot"), ("start", "moonshot"), ("reload_config", "moonshot")] and f.mot["moonshot"] == 7)
f = Fleet(legacy=True)
f.reload_restores = False
rep, ctl = run_lt(f)
check("LIVE TEST: a resume that does not restore max_open_trades FAILS the test (exit 1)", not rep["passed"] and rep["exit"] == 1)
f = Fleet(legacy=True)
f.reload_restores = False
rep, ctl = run_lt(f)
check("LIVE TEST: it never leaves the bot paused even when the test failed (cleanup resume attempted)", len([c for c in ctl.calls if c[0] in ("start", "reload_config")]) >= 3)

# ---------------------------------------------------------------- safety stops
f = Fleet(dry=False)
rep, ctl = run_lt(f)
check("LIVE TEST: dry_run=false -> refused before any POST", rep["exit"] == 2 and ctl.calls == [] and not rep["passed"])
f = Fleet()
f.dry = None
rep, ctl = run_lt(f)
check("LIVE TEST: dry_run not readable -> refused, no POST", rep["exit"] == 2 and ctl.calls == [])
f = Fleet()
rep, ctl = run_lt(f, script={120: lambda: f.trades.__setitem__("moonshot", 12)})
check("LIVE TEST: a new trade WHILE paused fails the test (the pause does not stop entries) and the bot is resumed afterwards",
      not rep["passed"] and rep["exit"] == 1 and ("start", "moonshot") in ctl.calls and f.state["moonshot"] == "running")
f = Fleet()
rep, ctl = run_lt(f, script={120: lambda: f.state.__setitem__("moonshot", "running")})
check("LIVE TEST: the bot leaving the paused state by itself during the watch (restart) fails the test", not rep["passed"] and rep["exit"] == 1)
f = Fleet()
f.state["moonshot"] = "paused"
rep, ctl = run_lt(f)
check("LIVE TEST: refuses to start on a bot that is already paused (would hide a pre-existing pause)", rep["exit"] == 2 and ctl.calls == [])
for cb in al.NEVER_MANAGED:
    raised = False
    try:
        lt.run(cb, Fleet(bots=[cb]), Ctl(Fleet(bots=[cb])), sleep=QUIET, now=lambda: NOW, say=QUIET)
    except al.ControlBotError:
        raised = True
    check(f"LIVE TEST: {cb} is refused (ControlBotError, no request sent)", raised)
check("LIVE TEST CLI: --bot control_long is refused with exit 2", lt.main(["--bot", "control_long"]) == 2)
fb = Fleet()


class Boom(Ctl):
    def pause(self, bot):
        super().pause(bot)
        raise KeyboardInterrupt


boomc = Boom(fb)
try:
    lt.run("moonshot", fb, boomc, sleep=QUIET, watch1_s=60, watch2_s=60, poll_s=60, now=lambda: NOW, say=QUIET)
except KeyboardInterrupt:
    pass
check("LIVE TEST: Ctrl-C right after the pause still resumes the bot (try/finally)", fb.state["moonshot"] == "running" and ("start", "moonshot") in boomc.calls)
tmp = Path(tempfile.mkdtemp())
f = Fleet()
lt.run("moonshot", f, Ctl(f), sleep=QUIET, watch1_s=60, watch2_s=60, poll_s=60, now=lambda: NOW, out_dir=tmp, say=QUIET)
saved = json.loads((tmp / "p20_live_test_moonshot.json").read_text())
check("LIVE TEST: the report file is written and holds no credentials", saved["bot"] == "moonshot" and "password" not in json.dumps(saved).lower())

# ---------------------------------------------------------------- enforce watch
ALL = al.MANAGED_BOTS


def make_state(weights=None, paused=(), tracked=(), kill=False, daily_age_h=3, actions=None, mode_ok=True):
    d = Path(tempfile.mkdtemp())
    w = {b: 1.0 for b in ALL}
    w.update(weights or {})
    st = {"weights": w, "paused_by_allocator": list(tracked), "last_effective_mode": "enforce"}
    (d / al.STATE_FILE).write_text(json.dumps(st))
    rec = {"ts": mc.iso(NOW - timedelta(hours=daily_age_h)), "trigger": "daily", "effective_mode": "enforce" if mode_ok else "shadow",
           "mode_reasons": [] if mode_ok else ["dry_run=false on ['listing_fade']"], "previous": {b: 1.0 for b in ALL}, "final": w, "rules_fired": [],
           "actions": actions or [], "probe": [], "llm": {"valid": True}}
    (d / al.ALLOCATIONS_FILE).write_text(json.dumps(rec) + "\n")
    if kill:
        (d / al.KILL_FILE).write_text(json.dumps({"killed_at": mc.iso(NOW), "reason": "x", "by": "ui"}))
    fleet = Fleet(bots=list(ALL))
    for b in paused:
        fleet.state[b] = "paused"
    return d, fleet


ENF = {"PORTFOLIO_BRAIN_MODE": "enforce"}
d, fl = make_state()
r = chk.check(d, fl, NOW, 6, ENF)
check("ENFORCE WATCH: a clean state is clean (exit-0 condition), 1 run in the window", r["clean"] and r["runs_in_window"] == 1 and r["problems"] == [])
d, fl = make_state(weights={"listing_fade": 0.5}, paused=["listing_fade"])
check("ENFORCE WATCH: a bot paused although its weight > 0 is reported as STUCK", any("STUCK" in p and "listing_fade" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(weights={"listing_fade": 0.0}, paused=["listing_fade"], tracked=["listing_fade"])
check("ENFORCE WATCH: a tracked pause at weight 0 is fine", chk.check(d, fl, NOW, 6, ENF)["clean"])
d, fl = make_state(weights={"listing_fade": 0.0}, paused=["listing_fade"], tracked=[])
check("ENFORCE WATCH: a pause at weight 0 that the allocator does not track is flagged", any("not tracked" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(weights={"listing_fade": 0.0}, tracked=["listing_fade"])
check("ENFORCE WATCH: weight 0 but the bot is running (reconcile missed) is flagged", any("weight 0 but running" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(actions=[{"bot": "listing_fade", "action": "reload_config", "ok": False, "verified_running": False}])
check("ENFORCE WATCH: an action with verified_running=false is a problem", any("not verified" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(actions=[{"bot": "listing_fade", "action": "start", "ok": False, "detail": "boom"}])
check("ENFORCE WATCH: an action with ok=false is a problem", any("failed" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(actions=[{"bot": "control_long", "action": "stopentry", "ok": True}])
check("ENFORCE WATCH: an action on a control bot is a problem", any("never-managed" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(daily_age_h=30)
check("ENFORCE WATCH: no daily run in 26h is a problem", any("no daily allocator run" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(mode_ok=False)
check("ENFORCE WATCH: enforce set but the run degraded to shadow is a problem, with the reason", any("although enforce is set" in p and "dry_run=false" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state()
fl.dry = False
check("ENFORCE WATCH: dry_run=false on any bot is a problem", any("dry_run is False" in p for p in chk.check(d, fl, NOW, 6, ENF)["problems"]))
d, fl = make_state(kill=True, paused=["main"])
rk = chk.check(d, fl, NOW, 6, ENF)
check("ENFORCE WATCH: kill switch active with a bot still paused is flagged; the kill itself is noted", any("kill switch active but bots still paused" in p for p in rk["problems"]) and rk["notes"])
d, fl = make_state()
check("ENFORCE WATCH: the report lists every managed bot's live state and never a control bot", set(chk.check(d, fl, NOW, 6, ENF)["bots"]) == set(ALL)
      and not set(chk.check(d, fl, NOW, 6, ENF)["bots"]) & set(al.NEVER_MANAGED))
d, fl = make_state(daily_age_h=20)
check("ENFORCE WATCH: the window filters runs by time (a 20h-old run is outside a 6h window)", chk.check(d, fl, NOW, 6, ENF)["runs_in_window"] == 0
      and chk.check(d, fl, NOW, 24, ENF)["runs_in_window"] == 1)


# ---------------------------------------------------------------- kill drill
class R:
    def __init__(self, code=200, payload=None):
        self.status_code, self._p = code, payload or {}

    def json(self):
        return self._p


def drill(killed_flag=True, bots_paused=(), cleared=True):
    fl = Fleet(bots=list(ALL))
    for b in bots_paused:
        fl.state[b] = "paused"
    posts = []

    def post(url):
        posts.append(url)
        return R(200, {"cleared": cleared} if url.endswith("resume") else {"killed": True})

    def get(url):
        return R(200, {"kill": {"by": "ui"} if killed_flag else None, "effective_mode": "shadow", "mode": "enforce"})
    return chk.kill_drill("http://x", fl, get, post), posts


res, posts = drill()
check("KILL DRILL: passes when killed, every bot running, then resume clears the flag", res["passed"] and posts == ["http://x/api/portfolio/allocator/kill", "http://x/api/portfolio/allocator/resume"])
res, _ = drill(bots_paused=["listing_fade"])
check("KILL DRILL: fails when a bot is still paused after the kill", not res["passed"])
res, _ = drill(killed_flag=False)
check("KILL DRILL: fails when the allocator does not report the kill flag", not res["passed"])

# ---------------------------------------------------------------- Dockerfile pin
for d_ in ("freqtrade", "freqtrade-shorts", "freqtrade-moonshot",
           "freqtrade-listing-fade", "freqtrade-trend-ensemble", "freqtrade-trend-v2", "freqtrade-control-long", "freqtrade-control-short"):
    first = [ln for ln in (REPO / d_ / "Dockerfile").read_text(encoding="utf-8").splitlines() if ln.startswith("FROM ")]
    check(f"IMAGE PIN: {d_} builds FROM freqtradeorg/freqtrade:2026.8 (no :stable)", first == ["FROM freqtradeorg/freqtrade:2026.8"])
compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
check("IMAGE PIN: no service pulls a floating freqtrade tag in the compose file", "freqtradeorg/freqtrade:stable" not in compose and "freqtradeorg/freqtrade:latest" not in compose)

# ---------------------------------------------------------------- compose: shared allocator volume
import yaml  # noqa: E402

cy = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
vols = lambda svc: cy["services"][svc].get("volumes", [])  # noqa: E731
MANAGED_SVC = ["freqtrade", "freqtrade-shorts", "freqtrade-moonshot",
               "freqtrade-listing-fade", "freqtrade-trend-v2"]
check("COMPOSE: survival-agent mounts allocator-shared read-write at /allocator", "allocator-shared:/allocator" in vols("survival-agent"))
for svc in MANAGED_SVC:
    check(f"COMPOSE: {svc} mounts allocator-shared READ-ONLY at /allocator", "allocator-shared:/allocator:ro" in vols(svc))
for svc in ("freqtrade-trend-ensemble", "freqtrade-control-long", "freqtrade-control-short"):
    check(f"COMPOSE: {svc} (control / trend v1) has NO allocator mount", not any("allocator" in v for v in vols(svc)))
check("COMPOSE: the named volume is declared", "allocator-shared" in cy["volumes"])
check("COMPOSE: survival-agent still defaults the allocator mode to enforce (UI/Portainer value wins)",
      any(e.startswith("PORTFOLIO_BRAIN_MODE=") and e.endswith(":-enforce}") for e in cy["services"]["survival-agent"]["environment"]))

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for x in failures:
        print("  -", x)
    sys.exit(1)
print("\nALL P20.1 OPS CHECKS PASSED")
