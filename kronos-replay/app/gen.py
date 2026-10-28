"""
kronos-replay -- pure logic of the historical Kronos signal generator (no torch, no network, no clock of its own: tested offline with a fake forecaster).

For every hourly as-of time t0 of a window and every pair, the model sees ONLY the closed candles whose open time is <= t0 - 1h (the newest one must be exactly the
candle opened at t0 - 1h, no gaps, otherwise the pair is skipped at that hour). This is the same convention as the live kronos-signal container and the P19 ledger.
The forecast of the next 24 candles becomes expected returns at 1/4/24h. The SIGNAL is fixed in advance and fitted on nothing:
  up   if ret_4h >= +THRESHOLD, down if ret_4h <= -THRESHOLD, else neutral          (THRESHOLD = 0.5%, gross of fees; the evaluation charges fees and slippage)
Files, all under one output directory (REPLAY_DIR/kronos on the survival-agent side): job.json, candles/<SYMBOL>.json, signals_<shard>.jsonl (append-only, resumable),
progress_<shard>.json, STOP (a flag file: the worker stops after the inference it is running).
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOOKBACK = 480
MIN_ROWS = 400
PRED_LEN = 24
HORIZONS = (1, 4, 24)
SEED = 12345
THRESHOLD = 0.005
SIGNAL_HORIZON = 4
HOUR_MS = 3_600_000
SAFE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def as_of_hours(start: datetime, end: datetime) -> list[datetime]:
    """t0 = start, start+1h, ... < end."""
    out, t = [], start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        out.append(t)
        t += timedelta(hours=1)
    return out


def window_for(candles: list[list[float]], t0: datetime, lookback: int = LOOKBACK) -> tuple[list[dict] | None, str]:
    """candles: rows [open_ms, open, high, low, close, volume] sorted by open_ms. Returns (window, "") or (None, reason).
    LOOK-AHEAD GUARD: only rows with open_ms <= t0 - 1h are even looked at; the newest must be the candle opened at t0 - 1h; the window must have no gap."""
    last_ms = ms(t0) - HOUR_MS
    if not candles or candles[0][0] > last_ms:
        return None, "no_candles_before_t0"
    # binary search for the last row with open_ms <= last_ms
    lo, hi = 0, len(candles)
    while lo < hi:
        mid = (lo + hi) // 2
        if candles[mid][0] <= last_ms:
            lo = mid + 1
        else:
            hi = mid
    rows = candles[max(0, lo - lookback):lo]
    if not rows or rows[-1][0] != last_ms:
        return None, "stale_candles"
    if len(rows) < MIN_ROWS:
        return None, f"only_{len(rows)}_candles"
    if rows[-1][0] - rows[0][0] != (len(rows) - 1) * HOUR_MS:
        return None, "gap_in_window"
    return [{"open_dt": datetime.fromtimestamp(r[0] / 1000, timezone.utc), "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows], ""


def signal_of(ret_4h: float, threshold: float = THRESHOLD) -> str:
    if ret_4h >= threshold:
        return "up"
    if ret_4h <= -threshold:
        return "down"
    return "neutral"


def make_record(pair: str, symbol: str, t0: datetime, window: list[dict], forecast: list[float], samples: int, threshold: float) -> dict:
    price = float(window[-1]["close"])
    rets = {f"ret_{h}h": round(float(forecast[h - 1]) / price - 1.0, 6) for h in HORIZONS}
    return {"sid": f"kronos_replay|{pair}|{iso(t0)}", "t0": iso(t0), "pair": pair, "symbol": symbol, "price": price, **rets, "signal": signal_of(rets["ret_4h"], threshold),
            "magnitude": abs(rets["ret_4h"]), "threshold": threshold, "samples": samples, "seed": SEED, "n_candles": len(window)}


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")           # one name per process: several shards write next to each other
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def read_signals(out_dir: Path) -> list[dict]:
    recs: dict[str, dict] = {}
    for f in sorted(Path(out_dir).glob("signals_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue                      # a half-written last line after a kill
            recs[r["sid"]] = r
    return sorted(recs.values(), key=lambda r: (r["t0"], r["pair"]))


def load_candles(out_dir: Path, job: dict) -> dict[str, list[list[float]]]:
    out = {}
    for p in job["pairs"]:
        d = read_json(Path(out_dir) / "candles" / f"{p['symbol']}.json")
        if d:
            out[p["pair"]] = [[int(r[0])] + [float(x) for x in r[1:6]] for r in d["rows"]]
    return out


def plan(job: dict, shard: int = 0, nshards: int = 1) -> list[tuple[datetime, dict]]:
    """Chronological work list of this shard: the (hour, pair) items in time order, dealt round-robin to the shards (every shard gets the same number of items +-1 and a stop at any
    moment leaves a complete, consistent early part of the window)."""
    items = [(t0, p) for t0 in as_of_hours(parse(job["start"]), parse(job["end"])) for p in job["pairs"]]
    return [it for i, it in enumerate(items) if i % nshards == shard]


def run_shard(job: dict, out_dir: Path, predict, shard: int = 0, nshards: int = 1, stop=lambda: False, clock=time.time, log=print, progress_every: int = 5) -> dict:
    """Resumable: a (pair, t0) already in this shard's file is skipped. Skipped hours (stale / gaps) are recorded as rows with signal 'skipped' so that a resume does not retry them."""
    out_dir = Path(out_dir)
    candles = load_candles(out_dir, job)
    work = plan(job, shard, nshards)
    path = out_dir / f"signals_{shard}.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["sid"])
            except (ValueError, KeyError):
                continue                      # a half-written last line after a kill: that one is redone
    todo = []
    for t0, p in work:
        sid = f"kronos_replay|{p['pair']}|{iso(t0)}"
        if sid not in done:
            todo.append((t0, p, sid))
    total, already = len(work), len(work) - len(todo)
    started, n_new, skipped, state = clock(), 0, {}, "running"
    prog_path = out_dir / f"progress_{shard}.json"

    def progress(st: str) -> None:
        el = clock() - started
        rate = el / n_new if n_new else None
        write_json(prog_path, {"job_id": job.get("id"), "shard": shard, "nshards": nshards, "total": total, "done": already + n_new, "new_this_run": n_new, "state": st, "seconds_per_inference": rate,
                               "eta_seconds": (rate * (total - already - n_new)) if rate else None, "skipped": skipped, "updated": iso(datetime.fromtimestamp(clock(), timezone.utc))})
    progress("running")
    if path.exists() and path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        with path.open("ab") as fh0:
            fh0.write(b"\n")                  # close a line a kill left half-written, so the next record starts on its own line
    with path.open("a", encoding="utf-8") as fh:
        for t0, p, sid in todo:
            if stop() or (out_dir / "STOP").exists():
                state = "stopped"
                break
            rows = candles.get(p["pair"])
            win, why = window_for(rows, t0) if rows else (None, "no_candle_file")
            if win is None:
                rec = {"sid": sid, "t0": iso(t0), "pair": p["pair"], "symbol": p["symbol"], "signal": "skipped", "reason": why}
                skipped[why] = skipped.get(why, 0) + 1
            else:
                rec = make_record(p["pair"], p["symbol"], t0, win, predict(win, t0), job["samples"], job["threshold"])
            rec["_shard"] = shard
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            n_new += 1
            if n_new % progress_every == 0:
                progress("running")
        else:
            state = "done"
    progress(state)
    log(f"shard {shard}/{nshards}: {state}, {already + n_new}/{total} done, {n_new} new, skipped {skipped}")
    return {"state": state, "done": already + n_new, "total": total, "new": n_new, "skipped": skipped}


def summarize_progress(out_dir: Path) -> dict:
    """All shards together: done / total, state, seconds per inference of the whole worker set and the ETA."""
    shards = [read_json(f) for f in sorted(Path(out_dir).glob("progress_*.json"))]
    shards = [s for s in shards if s]
    if not shards:
        return {"state": "none", "done": 0, "total": 0}
    done, total = sum(s["done"] for s in shards), sum(s["total"] for s in shards)
    rates = [s["seconds_per_inference"] for s in shards if s.get("seconds_per_inference")]
    running = [s for s in shards if s["state"] == "running"]
    # wall-clock ETA: the slowest shard decides (they run in parallel)
    eta = max((s["eta_seconds"] or 0) for s in running) if running and all(s.get("eta_seconds") is not None for s in running) else None
    state = "running" if running else ("stopped" if any(s["state"] == "stopped" for s in shards) else "done" if all(s["state"] == "done" for s in shards) else "idle")
    return {"state": state, "done": done, "total": total, "shards": len(shards), "seconds_per_inference_per_shard": (sum(rates) / len(rates)) if rates else None, "eta_seconds": eta,
            "skipped": {k: sum(s.get("skipped", {}).get(k, 0) for s in shards) for k in {k for s in shards for k in s.get("skipped", {})}}}


def peak_rss_mb() -> float | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 2**20, 1)
    except Exception:      # noqa: BLE001
        return None


def run_bench(predict, rows: list[list[float]], end: datetime, n: int, clock=time.time) -> dict:
    """K1 micro-benchmark: n consecutive hourly forecasts of ONE pair (as-of hours end-n .. end-1), seconds per inference and the peak RSS. Same window rule as the real run."""
    times, skipped = [], 0
    for k in range(n):
        t0 = end - timedelta(hours=n - k)
        win, _ = window_for(rows, t0)
        if win is None:
            skipped += 1
            continue
        t = clock()
        predict(win, t0)
        times.append(clock() - t)
    srt = sorted(times)
    return {"n": len(times), "skipped": skipped, "first_s": times[0] if times else None, "mean_s": sum(times) / len(times) if times else None, "median_s": srt[len(srt) // 2] if srt else None,
            "p95_s": srt[int(0.95 * (len(srt) - 1))] if srt else None, "peak_rss_mb": peak_rss_mb()}
