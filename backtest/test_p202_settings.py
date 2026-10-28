"""
P20.2: dashboard switches (runtime settings) -- precedence, validation, audit log, wiring into the features, endpoints,
enforce guard, per-bot exclusion, UI.
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
DATA = tempfile.mkdtemp()
SHARED = tempfile.mkdtemp()
os.environ.update({"DATA_DIR": DATA, "BOTS_DIR": tempfile.mkdtemp(), "ALLOCATOR_DIR": SHARED})
for k in ("PORTFOLIO_BRAIN_MODE", "MARKET_CONTEXT_MODE", "SIGNAL_AUDIT_MODE", "ALLOCATOR_PROBE", "ALLOCATOR_PROBE_DAYS", "ALLOCATOR_EXCLUDED_BOTS"):
    os.environ.pop(k, None)

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import allocator as al  # noqa: E402

# P25: the default policy is now `grow`; this suite pins the original `protect` rules (the grow policy has its own suite, test_p25_grow_policy.py)
_orig_load_settings = al.load_settings


def _protect_settings(env=None):
    e = dict(al.rs.merged_env() if env is None else env)
    e.setdefault("ALLOCATOR_POLICY", "protect")
    return _orig_load_settings(e)


al.load_settings = _protect_settings
from backend.engine import market_context as mc  # noqa: E402
from backend.engine import portfolio_brain as pb  # noqa: E402
from backend.engine import runtime_settings as rs  # noqa: E402
from backend.engine import signal_audit as sa  # noqa: E402

D = Path(DATA)
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------- precedence and validation
d0 = rs.describe(D)
check("defaults when nothing is set: shadow / on / on / on / 7 / none", d0["PORTFOLIO_BRAIN_MODE"]["value"] == "shadow" and d0["PORTFOLIO_BRAIN_MODE"]["source"] == "default"
      and d0["MARKET_CONTEXT_MODE"]["value"] == "on" and d0["ALLOCATOR_PROBE_DAYS"]["value"] == "7" and d0["ALLOCATOR_EXCLUDED_BOTS"]["value"] == "")
os.environ["PORTFOLIO_BRAIN_MODE"] = "enforce"
d1 = rs.describe(D)
check("the environment beats the default and is labelled env", d1["PORTFOLIO_BRAIN_MODE"]["value"] == "enforce" and d1["PORTFOLIO_BRAIN_MODE"]["source"] == "env")
rs.save({"PORTFOLIO_BRAIN_MODE": "shadow"}, D, now=NOW)
d2 = rs.describe(D)
check("a UI value beats the environment and is labelled ui", d2["PORTFOLIO_BRAIN_MODE"]["value"] == "shadow" and d2["PORTFOLIO_BRAIN_MODE"]["source"] == "ui")
check("merged_env: the feature modules see the UI value", rs.merged_env(D)["PORTFOLIO_BRAIN_MODE"] == "shadow")
rs.save({"PORTFOLIO_BRAIN_MODE": None}, D, now=NOW)
check("value null resets to the environment", rs.describe(D)["PORTFOLIO_BRAIN_MODE"]["source"] == "env" and rs.describe(D)["PORTFOLIO_BRAIN_MODE"]["value"] == "enforce")
os.environ.pop("PORTFOLIO_BRAIN_MODE")
for key, bad in {"PORTFOLIO_BRAIN_MODE": "yolo", "MARKET_CONTEXT_MODE": "maybe", "ALLOCATOR_PROBE_DAYS": "0", "ALLOCATOR_EXCLUDED_BOTS": "main,control_long"}.items():
    raised = False
    try:
        rs.save({key: bad}, D)
    except ValueError:
        raised = True
    check(f"validation: {key}={bad!r} is rejected", raised)
for key in ("API_KEY", "DEEPSEEK_API_KEY", "FREQTRADE__DRY_RUN", "dry_run", "ALLOCATOR_DIR"):
    raised = False
    try:
        rs.save({key: "x"}, D)
    except ValueError:
        raised = True
    check(f"only whitelisted switches can be set: {key} rejected (no credentials, no dry_run)", raised)
check("ALLOCATOR_PROBE_DAYS 90 ok / 91 rejected", rs.save({"ALLOCATOR_PROBE_DAYS": "90"}, D)["ALLOCATOR_PROBE_DAYS"]["value"] == "90.0"
      and rs.SPEC["ALLOCATOR_PROBE_DAYS"]["validate"]("91") is None)
rs.save({"ALLOCATOR_PROBE_DAYS": None}, D)
check("excluded bots: managed only, sorted, de-duplicated", rs.save({"ALLOCATOR_EXCLUDED_BOTS": "listing_fade,main,listing_fade"}, D)["ALLOCATOR_EXCLUDED_BOTS"]["value"] == "listing_fade,main")
rs.save({"ALLOCATOR_EXCLUDED_BOTS": ""}, D)

(D / rs.SETTINGS_FILE).write_text("not json")
check("a corrupt settings file is ignored (falls back to env/default), no exception", rs.load_overrides(D) == {} and rs.describe(D)["PORTFOLIO_BRAIN_MODE"]["source"] == "default")
(D / rs.SETTINGS_FILE).write_text(json.dumps({"PORTFOLIO_BRAIN_MODE": "yolo", "EVIL": "x", "MARKET_CONTEXT_MODE": "off"}))
check("a hand-edited file with an invalid value or unknown key: those entries are dropped, valid ones kept", rs.load_overrides(D) == {"MARKET_CONTEXT_MODE": "off"})
(D / rs.SETTINGS_FILE).unlink()

log_before = (D / rs.LOG_FILE).read_text().splitlines()
rs.save({"MARKET_CONTEXT_MODE": "off"}, D, now=NOW)
log_after = (D / rs.LOG_FILE).read_text().splitlines()
rec = json.loads(log_after[-1])
check("AUDIT LOG: append-only, one line per change with key / old / new / source / time", log_after[:len(log_before)] == log_before and len(log_after) == len(log_before) + 1
      and rec["key"] == "MARKET_CONTEXT_MODE" and rec["old"] == "on" and rec["new"] == "off" and rec["source"] == "ui" and rec["ts"] == "2026-10-02T12:00:00Z")
rs.save({"MARKET_CONTEXT_MODE": None}, D)

# ---------------------------------------------------------------- wiring into the features
rs.save({"PORTFOLIO_BRAIN_MODE": "enforce"}, D)
check("allocator.load_settings reads the UI value (enforce) with no env at all", al.load_settings()["mode"] == "enforce")
check("the brain stays read-only: enforce from the UI gives it shadow", pb.load_settings()["mode"] == "shadow")
rs.save({"PORTFOLIO_BRAIN_MODE": "off"}, D)
check("UI 'off' turns the allocator and the brain off", al.load_settings()["mode"] == "off" and pb.load_settings()["mode"] == "off")
rs.save({"PORTFOLIO_BRAIN_MODE": None, "MARKET_CONTEXT_MODE": "off"}, D)
check("market_context.enabled follows the UI switch", mc.enabled() is False)
check("run_hourly is skipped when switched off in the UI", mc.run_hourly(D, now=NOW, fetcher=object()) == {"mode": "off", "skipped": True})
rs.save({"MARKET_CONTEXT_MODE": "on", "SIGNAL_AUDIT_MODE": "off"}, D)
check("market_context back on", mc.enabled() is True)
check("signal audit follows the UI switch (off -> skipped, nothing written)", sa.run_nightly(D, now=NOW, fetcher=object()) == {"mode": "off", "skipped": True})
rs.save({"SIGNAL_AUDIT_MODE": None, "ALLOCATOR_PROBE": "off", "ALLOCATOR_PROBE_DAYS": "3", "ALLOCATOR_EXCLUDED_BOTS": "listing_fade"}, D)
st = al.load_settings()
check("probe off / days / excluded bots reach allocator.load_settings", st["probe"] is False and st["probe_days"] == 3.0 and st["excluded"] == ["listing_fade"])
check("an explicit env dict still wins over everything (tests and callers can pin settings)", al.load_settings({"PORTFOLIO_BRAIN_MODE": "shadow"})["mode"] == "shadow")
rs.save({"ALLOCATOR_PROBE": None, "ALLOCATOR_PROBE_DAYS": None, "ALLOCATOR_EXCLUDED_BOTS": None}, D)


# ---------------------------------------------------------------- per-bot exclusion in a run
class Fleet:
    def __init__(self):
        self.state = {b: "running" for b in pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS}
        self.dry = {b: True for b in self.state}

    def reachable(self, b): return True
    def balance(self, b): return {"total": 500.0, "starting_capital": 500.0}
    def status(self, b): return []
    def trades(self, b, **k): return []

    def _json(self, b, p):
        return {"dry_run": self.dry[b], "state": self.state[b], "max_open_trades": 5} if p.endswith("show_config") else None


class Ctl:
    def __init__(self, f):
        self.f, self.calls = f, []

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
        self.calls.append(("reload_config", b))
        return True, "ok"


def llm_zero(s, u):
    return json.dumps({b: {"weight": 0.0, "reason": "r"} for b in al.MANAGED_BOTS})


dd = Path(tempfile.mkdtemp())
(dd / pb.LATEST_FILE).write_text(json.dumps({"evidence": {}}))
fl = Fleet()
ctl = Ctl(fl)
fl.state["listing_fade"] = "paused"
fl.state["main"] = "paused"
(dd / al.STATE_FILE).write_text(json.dumps({"weights": {b: 0.0 for b in al.MANAGED_BOTS}, "paused_by_allocator": ["listing_fade", "main"], "last_effective_mode": "enforce"}))
env = {"PORTFOLIO_BRAIN_MODE": "enforce", "ALLOCATOR_EXCLUDED_BOTS": "listing_fade"}
out = al.run_allocation(dd, NOW, fl, llm_zero, ctl, env=env)
check("EXCLUDED bot: forced to full size (1.0) whatever the LLM says, and the allocator resumes it if it had paused it",
      out["final"]["listing_fade"] == 1.0 and fl.state["listing_fade"] == "running" and ("start", "listing_fade") in ctl.calls and out["excluded"] == ["listing_fade"])
check("non-excluded bots keep following the allocator (main stays at 0 and paused)", out["final"]["main"] == 0.0 and fl.state["main"] == "paused")
rec = json.loads((dd / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])
check("the decision record names the excluded bots", rec["excluded"] == ["listing_fade"])
check("an excluded bot never gets a zero-clock or probe state", "listing_fade" not in json.loads((dd / al.STATE_FILE).read_text()).get("zero_since", {}))
check("control bots / trend_ensemble in the exclusion list are ignored (they are never managed anyway)",
      al.load_settings({"ALLOCATOR_EXCLUDED_BOTS": "control_long,trend_ensemble,listing_fade"})["excluded"] == ["listing_fade"])

# ---------------------------------------------------------------- endpoints
os.environ["DATA_DIR"] = DATA
Path(DATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402

app_module.DATA_DIR = D
fleet = Fleet()
pb.default_fetcher = lambda: fleet            # the endpoints call portfolio_brain.default_fetcher()
al.default_control = lambda: Ctl(fleet)
client = TestClient(app_module.app)

g = client.get("/api/settings/p20").json()
check("GET /api/settings/p20: every switch with value, source, options; managed and never-managed lists", set(g["settings"]) == set(rs.SPEC)
      and g["settings"]["PORTFOLIO_BRAIN_MODE"]["options"] == ["off", "shadow", "enforce"] and set(g["never_managed"]) == set(al.NEVER_MANAGED))
check("GET: readiness shows dry_run per bot, the shared-volume state and the live-test warning",
      g["readiness"]["dry_run_ok"] is True and g["readiness"]["shared_volume"] is True and any("live pause/resume test" in w for w in g["readiness"]["warnings"]))
r = client.post("/api/settings/p20", json={"key": "MARKET_CONTEXT_MODE", "value": "off"})
check("POST a plain switch: saved, source ui", r.status_code == 200 and r.json()["settings"]["MARKET_CONTEXT_MODE"]["value"] == "off"
      and r.json()["settings"]["MARKET_CONTEXT_MODE"]["source"] == "ui")
check("the change is in the audit log", json.loads((D / rs.LOG_FILE).read_text().splitlines()[-1])["key"] == "MARKET_CONTEXT_MODE")
check("POST an unknown key -> 400", client.post("/api/settings/p20", json={"key": "DEEPSEEK_API_KEY", "value": "x"}).status_code == 400)
check("POST an invalid value -> 400", client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "yolo"}).status_code == 400)
check("POST reset (null) -> back to env/default", client.post("/api/settings/p20", json={"key": "MARKET_CONTEXT_MODE", "value": None}).json()["settings"]["MARKET_CONTEXT_MODE"]["source"] == "default")
r = client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "enforce"})
check("ENFORCE without confirm=true -> 400 and nothing saved", r.status_code == 400 and rs.load_overrides(D).get("PORTFOLIO_BRAIN_MODE") is None)
fleet.dry["moonshot"] = False
r = client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "enforce", "confirm": True})
check("ENFORCE refused (409) while any managed bot has dry_run=false, naming the bot; nothing saved",
      r.status_code == 409 and "moonshot" in r.json()["detail"] and rs.load_overrides(D).get("PORTFOLIO_BRAIN_MODE") is None)
fleet.dry["moonshot"] = True
fleet.dry["control_long"] = False
r = client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "enforce", "confirm": True})
check("a live control bot does not block enforce (it is not managed) -- and enforce is accepted", r.status_code == 200
      and r.json()["settings"]["PORTFOLIO_BRAIN_MODE"]["value"] == "enforce" and r.json()["settings"]["PORTFOLIO_BRAIN_MODE"]["source"] == "ui")
check("accepting enforce returns the warnings (live test not run) so the UI can show them", any("live pause/resume test" in w for w in r.json()["warnings"]))
rs.save({"PORTFOLIO_BRAIN_MODE": "shadow"}, D)
orig = fleet._json
fleet._json = lambda b, p: None if b == "listing_fade" else orig(b, p)
r = client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "enforce", "confirm": True})
check("ENFORCE refused (409) when a bot's dry_run cannot be verified", r.status_code == 409 and "listing_fade" in r.json()["detail"])
fleet._json = orig
check("switching to off never needs confirm or a guard", client.post("/api/settings/p20", json={"key": "PORTFOLIO_BRAIN_MODE", "value": "off"}).status_code == 200)

(D / "p20_live_test_moonshot.json").write_text(json.dumps({"bot": "moonshot", "passed": True, "finished_at": "x", "entries_return": "inconclusive"}))
rd = al.readiness(D, fleet)
check("readiness: a passed live-test report removes that warning and is listed", not any("live pause/resume" in w for w in rd["warnings"]) and rd["live_test_reports"][0]["passed"] is True)
os.environ["ALLOCATOR_DIR"] = str(Path(tempfile.mkdtemp()) / "missing")
check("readiness: no shared volume -> a warning that bots cannot read weights/context", any("shared /allocator volume" in w for w in al.readiness(D, fleet)["warnings"]))
os.environ["ALLOCATOR_DIR"] = SHARED

# run now endpoint
rs.save({"PORTFOLIO_BRAIN_MODE": "shadow"}, D)
(D / pb.LATEST_FILE).write_text(json.dumps({"evidence": {}}))
al.default_llm = lambda: (lambda s, u: json.dumps({b: {"weight": 0.9, "reason": "r"} for b in al.MANAGED_BOTS}))
r = client.post("/api/portfolio/allocator/run")
check("POST /api/portfolio/allocator/run: one manual run through the same path (trigger 'manual', hard rules apply)",
      r.status_code == 200 and r.json()["ran"] and r.json()["effective_mode"] == "shadow" and r.json()["final"]["main"] == 0.9
      and json.loads((D / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])["trigger"] == "manual")
paths = app_module.app.openapi()["paths"]
check("routes: settings GET+POST, run POST", set(paths["/api/settings/p20"]) == {"get", "post"} and set(paths["/api/portfolio/allocator/run"]) == {"post"})

# export
from backend import export as ex  # noqa: E402

check("the ZIP carries the runtime settings and their change log", "allocator/runtime_settings.json" in ex.ALLOCATOR_FILES and "allocator/runtime_settings_log.jsonl" in ex.ALLOCATOR_FILES)

# ---------------------------------------------------------------- UI
html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("UI: mode selector with a confirm dialog for enforce, collector / audit / probe switches, per-bot exclusion, run-now button",
      all(s in html for s in ("setMode", "ENFORCE", "confirm(", "set_ALLOCATOR_PROBE_DAYS", "allocRun", "allocExcl", "/api/settings/p20", "Run allocator now"))
      and all(k in html for k in ("MARKET_CONTEXT_MODE", "SIGNAL_AUDIT_MODE", "ALLOCATOR_PROBE", "PORTFOLIO_BRAIN_MODE", "ALLOCATOR_EXCLUDED_BOTS")))
check("UI: the kill switch and clear-kill buttons are still there", "allocKill" in html and "allocResume" in html)
check("UI: shows where each value comes from and the readiness warnings", "S[k].source" in html and "rd.warnings" in html and "dry_run=false on" in html)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P20.2 SETTINGS CHECKS PASSED")
