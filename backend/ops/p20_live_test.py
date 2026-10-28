"""
P20.1 -- live pause/resume test on ONE dry-run bot (default: moonshot). Run it on the server, inside survival-agent:

    docker exec survival-agent python -m backend.ops.p20_live_test --bot moonshot

What it proves, in order (every step is printed and written to $DATA_DIR/p20_live_test_<bot>.json):
  0. the bot reports dry_run=true (anything else aborts before a single POST), and its max_open_trades / state are recorded
  1. POST /stopentry -> the bot shows as paused (state 'paused', or the legacy max_open_trades == 0) and which of the two it was
  2. watch phase 1: no new trade is opened while paused
  3. resume (POST /start, then POST /reload_config if it still looks paused) -> state running AND max_open_trades equals the
     value recorded in step 0 (so a compose/env-driven value such as FADE_MAX_OPEN_TRADES really came back)
  4. watch phase 2: entries return (a new trade appears) -- or the result says INCONCLUSIVE when the market simply produced no
     signal in either phase (then only the state-level checks are proven, and the report says so)
The bot is ALWAYS resumed at the end, even on failure or Ctrl-C. Only managed bots are accepted (control bots and
trend_ensemble raise before any request). Nothing secret is printed: credentials come from the environment and are never echoed.

Exit code 0 = every state-level check passed (entries_return may still be 'inconclusive'); 1 = a check failed; 2 = refused to start.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ..engine import allocator as al


def _paused(cfg) -> bool:
    return al.is_paused(cfg)


def _trade_total(fetcher, bot) -> int | None:
    data = fetcher._json(bot, "/api/v1/trades?limit=1&offset=0")
    if not isinstance(data, dict):
        return None
    return data.get("total_trades")


def run(bot: str, fetcher, control, sleep=time.sleep, watch1_s: int = 3900, watch2_s: int = 3900, poll_s: int = 60,
        now=lambda: datetime.now(timezone.utc), out_dir: Path | None = None, say=print) -> dict:
    """Pure driver (injectable fetcher/control/sleep) so the logic is tested without a live bot."""
    al.assert_manageable(bot)
    rep: dict = {"bot": bot, "started_at": now().isoformat(), "steps": [], "passed": False}

    def step(name, ok, **detail):
        rep["steps"].append({"step": name, "ok": ok, **detail})
        say(f"[{'OK' if ok else 'FAIL'}] {name} {json.dumps(detail, default=str) if detail else ''}")
        return ok

    cfg0 = fetcher._json(bot, "/api/v1/show_config")
    if not isinstance(cfg0, dict):
        step("0 read /show_config", False, reason="bot unreachable or credentials missing")
        rep["exit"] = 2
        return rep
    if cfg0.get("dry_run") is not True:
        step("0 dry_run is true", False, dry_run=cfg0.get("dry_run"))
        rep["exit"] = 2
        return rep
    mot0, state0 = cfg0.get("max_open_trades"), cfg0.get("state")
    step("0 dry_run=true; recorded state and max_open_trades", state0 == "running" and not _paused(cfg0), state=state0, max_open_trades=mot0)
    if _paused(cfg0):
        rep["exit"] = 2
        return rep
    t0 = _trade_total(fetcher, bot)
    paused_by_us = False
    try:
        paused_by_us = True          # set BEFORE the call: an interrupt right after the POST must still trigger the cleanup resume
        ok, msg = control.pause(bot)
        cfg1 = None
        for _ in range(15):
            cfg1 = fetcher._json(bot, "/api/v1/show_config")
            if _paused(cfg1):
                break
            sleep(2)
        semantics = ("state_paused" if isinstance(cfg1, dict) and cfg1.get("state") == "paused"
                     else "max_open_trades_zeroed" if isinstance(cfg1, dict) and cfg1.get("max_open_trades") == 0 else "unknown")
        rep["pause_semantics"] = semantics
        step("1 POST /stopentry -> bot is paused", ok and _paused(cfg1), semantics=semantics, response=msg,
             state=(cfg1 or {}).get("state"), max_open_trades=(cfg1 or {}).get("max_open_trades"))
        if not _paused(cfg1):
            rep["exit"] = 1
            return rep

        # ---- phase 1: nothing may open while paused
        opened1, waited = 0, 0
        while waited < watch1_s:
            sleep(poll_s)
            waited += poll_s
            tn = _trade_total(fetcher, bot)
            if t0 is not None and tn is not None and tn > t0:
                opened1 = tn - t0
                break
            if not _paused(fetcher._json(bot, "/api/v1/show_config")):
                step("2 bot stayed paused", False, reason="bot became un-paused during the watch (restart?)")
                rep["exit"] = 1
                return rep
        step("2 no new entries while paused", opened1 == 0, watched_seconds=waited, new_trades=opened1)
        if opened1:
            rep["exit"] = 1
            return rep
        t1 = _trade_total(fetcher, bot)

        # ---- resume
        acts = al.resume_bot(control, fetcher, bot)
        paused_by_us = False if acts and acts[-1]["ok"] else paused_by_us
        cfg2 = fetcher._json(bot, "/api/v1/show_config")
        ok_state = isinstance(cfg2, dict) and cfg2.get("state") == "running" and not _paused(cfg2)
        ok_mot = isinstance(cfg2, dict) and cfg2.get("max_open_trades") == mot0
        rep["resume_used_reload_config"] = any(a["action"] == "reload_config" for a in acts)
        step("3 resume -> state running", ok_state, actions=acts, state=(cfg2 or {}).get("state"))
        step("3b max_open_trades after resume equals the value before the test", ok_mot, before=mot0, after=(cfg2 or {}).get("max_open_trades"))
        if not (ok_state and ok_mot):
            rep["exit"] = 1
            return rep

        # ---- phase 2: entries return
        opened2, waited = 0, 0
        while waited < watch2_s:
            sleep(poll_s)
            waited += poll_s
            tn = _trade_total(fetcher, bot)
            if t1 is not None and tn is not None and tn > t1:
                opened2 = tn - t1
                break
        if opened2:
            step("4 entries returned after resume", True, new_trades=opened2, watched_seconds=waited)
            rep["entries_return"] = "proven"
        else:
            step("4 entries returned after resume", True, watched_seconds=waited, new_trades=0,
                 verdict="INCONCLUSIVE: no signal produced an entry in this window; only the state-level checks are proven")
            rep["entries_return"] = "inconclusive"
        rep["passed"] = True
        rep["exit"] = 0
        return rep
    finally:
        if paused_by_us:       # never leave the bot paused
            acts = al.resume_bot(control, fetcher, bot)
            rep["final_resume"] = acts
            say(f"[cleanup] resumed {bot}: {json.dumps(acts, default=str)}")
        rep["finished_at"] = now().isoformat()
        if out_dir is not None:
            try:
                (Path(out_dir) / f"p20_live_test_{bot}.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
            except OSError:
                pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bot", default="moonshot")
    ap.add_argument("--watch-minutes", type=int, default=65, help="per phase; must span at least one 1h candle close (default 65)")
    ap.add_argument("--poll-seconds", type=int, default=60)
    args = ap.parse_args(argv)
    try:
        al.assert_manageable(args.bot)
    except al.ControlBotError as e:
        print(f"refused: {e}")
        return 2
    from ..engine import portfolio_brain as pb
    rep = run(args.bot, pb.default_fetcher(), al.default_control(), watch1_s=args.watch_minutes * 60, watch2_s=args.watch_minutes * 60,
              poll_s=args.poll_seconds, out_dir=Path(os.getenv("DATA_DIR", "/data")))
    print(json.dumps({k: rep.get(k) for k in ("bot", "passed", "pause_semantics", "resume_used_reload_config", "entries_return", "exit")}, indent=1))
    return int(rep.get("exit", 1))


if __name__ == "__main__":
    sys.exit(main())
