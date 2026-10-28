"""
P20.1 -- enforce watch: run every 6 hours for the first 48 hours after PORTFOLIO_BRAIN_MODE=enforce (and on demand).

    docker exec survival-agent python -m backend.ops.p20_enforce_check [--hours 6]
    docker exec survival-agent python -m backend.ops.p20_enforce_check --kill-drill     # day-one kill-switch check

Checks (read-only apart from --kill-drill):
  * allocations.jsonl: every run in the window with its trigger, effective mode, weights that changed, rules fired and the
    actions it took; any action with ok=false or verified_running=false is a PROBLEM
  * the allocator really ran in enforce (effective_mode == enforce), and why not if it did not (mode_reasons)
  * stuck bots: a managed bot that is paused (state or max_open_trades == 0) although its weight is > 0, or paused but not
    tracked in paused_by_allocator, or still paused after a kill switch
  * the daily run happened inside the last 26 hours
  * control bots / trend_ensemble never appear in actions or weights
Exit code 0 = clean, 1 = at least one PROBLEM (printed). --kill-drill: POST /api/portfolio/allocator/kill, verifies the allocator
says killed, every managed bot is running, weights are 1 and mode drops to shadow, then POST .../resume; the next daily run (or the
hourly event check) re-asserts the weights if enforce is still set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..engine import allocator as al
from ..engine import market_context as mctx
from ..engine import portfolio_brain as pb


def check(data_dir: Path, fetcher, now: datetime, hours: float = 6.0, env: dict | None = None) -> dict:
    data_dir = Path(data_dir)
    problems: list[str] = []
    notes: list[str] = []
    settings = al.load_settings(env)
    allocs = al.load_allocations(data_dir)
    since = now - timedelta(hours=hours)
    window = [a for a in allocs if (pb._parse_dt(a.get("ts")) or now) >= since]
    st = al.load_json(data_dir / al.STATE_FILE, {})
    rows = []
    for a in window:
        changed = {b: [a["previous"].get(b), w] for b, w in a["final"].items() if a["previous"].get(b) != w}
        bad = [x for x in a.get("actions", []) if x.get("ok") is False or x.get("verified_running") is False]
        rows.append({"ts": a["ts"], "trigger": a["trigger"], "effective_mode": a["effective_mode"], "changed": changed,
                     "rules": a.get("rules_fired", []), "actions": a.get("actions", []), "probe": a.get("probe", []),
                     "llm_valid": a.get("llm", {}).get("valid")})
        for x in bad:
            problems.append(f"{a['ts']}: action failed or not verified: {x}")
        for x in a.get("actions", []):
            if x["bot"] in al.NEVER_MANAGED:
                problems.append(f"{a['ts']}: action on a never-managed bot {x['bot']}")
        if settings["mode"] == "enforce" and a["effective_mode"] != "enforce":
            problems.append(f"{a['ts']}: ran as {a['effective_mode']} although enforce is set: {a.get('mode_reasons')}")
    last_daily = max((pb._parse_dt(a["ts"]) for a in allocs if a.get("trigger") == "daily"), default=None)
    if settings["mode"] != "off" and (last_daily is None or now - last_daily > timedelta(hours=26)):
        problems.append(f"no daily allocator run in the last 26h (last: {last_daily})")
    live_state = {}
    for bot in al.MANAGED_BOTS:
        cfg = fetcher._json(bot, "/api/v1/show_config")
        if not isinstance(cfg, dict):
            problems.append(f"{bot}: cannot read /show_config")
            continue
        paused = al.is_paused(cfg)
        w = st.get("weights", {}).get(bot, 1.0)
        tracked = bot in st.get("paused_by_allocator", [])
        live_state[bot] = {"paused": paused, "weight": w, "tracked": tracked, "state": cfg.get("state"), "max_open_trades": cfg.get("max_open_trades"),
                           "dry_run": cfg.get("dry_run")}
        if cfg.get("dry_run") is not True:
            problems.append(f"{bot}: dry_run is {cfg.get('dry_run')} -- enforce must not be on")
        if paused and w > 0:
            problems.append(f"{bot}: STUCK on pause although weight is {w}")
        if paused and w == 0 and not tracked:
            problems.append(f"{bot}: paused at weight 0 but not tracked in paused_by_allocator (paused by someone else?)")
        if not paused and w == 0 and settings["mode"] == "enforce" and al.kill_state(data_dir) is None:
            problems.append(f"{bot}: weight 0 but running (the hourly reconcile should have re-paused it)")
    k = al.kill_state(data_dir)
    if k:
        notes.append(f"kill switch active since {k.get('killed_at')} by {k.get('by')}: {k.get('reason')}")
        stuck = [b for b, s in live_state.items() if s["paused"]]
        if stuck:
            problems.append(f"kill switch active but bots still paused: {stuck}")
    for b in st.get("weights", {}):
        if b in al.NEVER_MANAGED:
            problems.append(f"{b} has a weight entry")
    return {"generated_at": mctx.iso(now), "window_hours": hours, "mode": settings["mode"], "runs_in_window": len(window), "runs": rows,
            "bots": live_state, "probe_state": st.get("probe", {}), "zero_since": st.get("zero_since", {}), "notes": notes,
            "problems": problems, "clean": not problems}


def kill_drill(base_url: str, fetcher, http_get, http_post) -> dict:
    out: dict = {"steps": [], "passed": False}

    def step(name, ok, **d):
        out["steps"].append({"step": name, "ok": ok, **d})
        print(f"[{'OK' if ok else 'FAIL'}] {name} {json.dumps(d, default=str) if d else ''}")
        return ok

    r = http_post(f"{base_url}/api/portfolio/allocator/kill")
    step("POST kill answered 200", r.status_code == 200)
    a = http_get(f"{base_url}/api/portfolio/allocator").json()
    step("allocator reports the kill flag", bool(a.get("kill")), kill=a.get("kill"))
    running = {}
    for bot in al.MANAGED_BOTS:
        running[bot] = not al.is_paused(fetcher._json(bot, "/api/v1/show_config"))
    step("every managed bot is running again", all(running.values()), bots=running)
    step("effective mode dropped to shadow", a.get("effective_mode") in (None, "shadow") or a.get("mode") == "enforce" and bool(a.get("kill")))
    r = http_post(f"{base_url}/api/portfolio/allocator/resume")
    step("POST resume cleared the flag", r.status_code == 200 and r.json().get("cleared") is True)
    out["passed"] = all(s["ok"] for s in out["steps"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--kill-drill", action="store_true")
    ap.add_argument("--base-url", default="http://127.0.0.1:8088")
    args = ap.parse_args(argv)
    fetcher = pb.default_fetcher()
    if args.kill_drill:
        import requests
        res = kill_drill(args.base_url, fetcher, requests.get, requests.post)
        return 0 if res["passed"] else 1
    rep = check(Path(os.getenv("DATA_DIR", "/data")), fetcher, datetime.now(timezone.utc), args.hours)
    print(json.dumps(rep, indent=1, default=str))
    return 0 if rep["clean"] else 1


if __name__ == "__main__":
    sys.exit(main())
