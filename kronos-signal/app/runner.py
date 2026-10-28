"""
kronos-signal -- the hourly loop. Wakes at minute KRONOS_RUN_MINUTE (default 2: the 1h candle has closed and the bots' REST has it), runs one cycle,
writes the signals to $KRONOS_OUT_DIR/kronos_signals.jsonl (append-only) and a health file. KRONOS_MODE=off keeps the container idle.
Read-only towards the bots: it only GETs /whitelist and /pair_candles.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

from . import core

log = logging.getLogger("kronos-signal")


def make_fetcher():
    import requests
    user, pw = os.environ.get("FREQTRADE_API_USERNAME"), os.environ.get("FREQTRADE_API_PASSWORD")

    def fetch_json(bot: str, path: str):
        if bot not in core.BOT_URLS or not (user and pw):
            return None
        try:
            r = requests.get(core.BOT_URLS[bot] + path, auth=(user, pw), timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:      # noqa: BLE001
            log.warning("GET %s%s failed: %s", bot, path.split("?")[0], e)
            return None
    return fetch_json


def free_disk_mb(path) -> int:
    import shutil
    return shutil.disk_usage(path).free // (1024 * 1024)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        os.nice(10)         # lowest practical priority: when the host is busy, the bots win the CPU, not this container
    except (AttributeError, OSError):
        pass
    cfg = core.config_from_env()
    out_dir = cfg["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path, health_path = out_dir / "kronos_signals.jsonl", out_dir / "kronos_health.json"
    if cfg["mode"] == "off":
        log.warning("KRONOS_MODE=off: idle (no model loaded, nothing written)")
        while True:
            time.sleep(3600)
    if free_disk_mb(out_dir) < 200:
        raise SystemExit(f"refusing to start: only {free_disk_mb(out_dir)} MB free on the output volume")
    from . import predictor
    threads = int(os.environ.get("KRONOS_THREADS", "2"))
    predict, meta = predictor.load(threads, cfg["samples"])
    log.info("Kronos loaded: %s", meta)
    health_path.write_text(json.dumps({"started_at": core.iso(datetime.now(timezone.utc)), "status": "model loaded, waiting for the first cycle", "model": meta},
                                      indent=1), encoding="utf-8")
    fetch_json = make_fetcher()

    def predict_with_meta(window, t0):
        return predict(window, t0)

    while True:
        now = datetime.now(timezone.utc)
        if now.minute < cfg["run_minute"]:
            time.sleep(core.seconds_until(now, cfg["run_minute"]))
            now = datetime.now(timezone.utc)
        if core.hour_done(out_path, core.floor_hour(now)):
            time.sleep(core.seconds_until(datetime.now(timezone.utc), cfg["run_minute"]))      # this hour is already done (restart)
            continue
            continue
        try:
            summary = core.run_cycle(now, fetch_json, predict_with_meta, cfg, out_path)
            summary["model"] = meta
            health_path.write_text(json.dumps(summary, indent=1, default=str), encoding="utf-8")
            log.info("cycle t0=%s scored=%d written=%d skipped=%d truncated=%s in %ss", summary["t0"], summary["pairs_scored"],
                     summary["records_written"], len(summary["skipped"]), summary["truncated"], summary["cycle_seconds"])
        except Exception as e:      # noqa: BLE001
            log.exception("cycle failed: %s", e)
        time.sleep(max(5.0, core.seconds_until(datetime.now(timezone.utc), cfg["run_minute"])))


if __name__ == "__main__":
    main()
