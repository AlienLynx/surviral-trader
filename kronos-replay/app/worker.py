"""
kronos-replay -- the worker. Two ways to run the same code:
  container (default): idle loop. It polls $KRONOS_REPLAY_DIR/job.json (written by survival-agent through the shared volume); no job = no model loaded and no CPU used.
                       A job runs until it is done or until the flag file STOP appears; removing STOP and writing job.json again resumes it (finished (pair, hour) rows are skipped).
  PC / one-off:        python -m app.worker --dir <out dir> [--shard I --of N]    runs shard I of N of the job in <out dir> once and exits (several processes = several shards).
It never talks to a bot and never writes outside its output directory. nice 10, like kronos-signal.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from . import gen

log = logging.getLogger("kronos-replay")


def run_job(out_dir: Path, shard: int, nshards: int, threads: int) -> dict:
    job = gen.read_json(out_dir / "job.json")
    if not job:
        raise SystemExit(f"no job.json in {out_dir}")
    if shutil.disk_usage(out_dir).free < 200 * 1024 * 1024:
        raise SystemExit(f"refusing to start: less than 200 MB free on {out_dir}")
    from . import predictor
    predict, meta = predictor.load(threads, job["samples"])
    log.info("Kronos loaded: %s", meta)
    gen.write_json(out_dir / "model.json", meta)
    return gen.run_shard(job, out_dir, predict, shard, nshards, log=log.info)


def run_bench_request(out_dir: Path, threads: int) -> None:
    """A benchmark request (bench_request.json, written by survival-agent next to the candle file of one pair): n forecasts, the result goes to bench_result.json."""
    req = gen.read_json(out_dir / "bench_request.json")
    res = gen.read_json(out_dir / "bench_result.json")
    if not req or (res and res.get("id") == req.get("id")):
        return
    from . import predictor
    samples = int(req.get("samples", 3))
    t = time.time()
    predict, meta = predictor.load(threads, samples)
    load_s = time.time() - t
    cd = gen.read_json(out_dir / "candles" / f"{req['symbol']}.json")
    rows = [[int(r[0])] + [float(x) for x in r[1:6]] for r in (cd or {}).get("rows", [])]
    out = gen.run_bench(predict, rows, gen.parse(req["end"]), int(req.get("n", 12)))
    gen.write_json(out_dir / "bench_result.json", {"id": req["id"], "symbol": req["symbol"], "threads": threads, "samples": samples, "load_s": round(load_s, 1), "model": meta, **out,
                                                    "finished": gen.iso(datetime.now(timezone.utc))})


def finished(out_dir: Path, job: dict) -> bool:
    p = gen.read_json(out_dir / "progress_0.json")
    return bool(p and p.get("state") == "done" and p.get("job_id") == job.get("id"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.environ.get("KRONOS_REPLAY_DIR", "/kronos-replay"))
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--of", type=int, default=1)
    ap.add_argument("--threads", type=int, default=int(os.environ.get("KRONOS_THREADS", "2")))
    ap.add_argument("--once", action="store_true", help="run the job once and exit instead of idling (PC mode)")
    a = ap.parse_args()
    out_dir = Path(a.dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.once:
        print(run_job(out_dir, a.shard, a.of, a.threads))
        return
    log.info("idle: waiting for %s/job.json (no model is loaded until a job exists)", out_dir)
    while True:
        try:
            run_bench_request(out_dir, a.threads)
        except Exception as e:      # noqa: BLE001
            log.exception("benchmark failed: %s", e)
            gen.write_json(out_dir / "bench_result.json", {"id": (gen.read_json(out_dir / "bench_request.json") or {}).get("id"), "error": f"{type(e).__name__}: {e}"})
        job = gen.read_json(out_dir / "job.json")
        if job and not (out_dir / "STOP").exists() and not finished(out_dir, job):
            try:
                log.info("job %s: %d pairs, %s -> %s", job.get("id"), len(job["pairs"]), job["start"], job["end"])
                run_job(out_dir, 0, 1, a.threads)
            except SystemExit as e:
                log.error("%s", e)
            except Exception as e:      # noqa: BLE001
                log.exception("job failed: %s", e)
                time.sleep(60)
        time.sleep(20)


if __name__ == "__main__":
    main()
