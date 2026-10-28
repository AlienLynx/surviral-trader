"""
P22 K2 / K3 -- Kronos as a MEASURED signal, replay side. survival-agent prepares a job (pairs, window, closed hourly candles up to the end of the window ONLY) in
REPLAY_DIR/kronos/ (a volume shared with the `kronos-replay` container, or a plain folder when the worker runs on a PC), the worker writes signals_<shard>.jsonl and
progress_<shard>.json there. This module never imports torch and never writes outside REPLAY_DIR/kronos. The few helpers that the worker also has
(read_json / read_signals / summarize_progress) are COPIES (one copy per directory, like the bots); a test keeps the two in step.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import data as rdata, paths

SAMPLES = 3
THRESHOLD = 0.005                     # fixed in advance: |predicted 4h return| >= 0.5% is a signal, else neutral (gross of fees; the evaluation charges fees and slippage)
LOOKBACK = 480
MAX_HOURS_OK = 12.0                   # the spec: above this, say so and propose a smaller pair set
SEC_PER_INFERENCE = 9.6               # K1: 72 inferences measured on the PC (2 threads, 3 samples); the server's real cycle was 11.4 s per pair
MIN_DAYS, MAX_DAYS = 7, 60
HOUR_MS = 3_600_000


class KronosError(ValueError):
    pass


def kdir() -> Path:
    """REPLAY_DIR/kronos -- the ONLY place this module writes (never the live /kronos volume of kronos-signal)."""
    base = paths.root()
    p = (base / "kronos").resolve()
    p.relative_to(base)
    return p


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


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


def read_signals(d: Path | None = None) -> list[dict]:
    recs: dict[str, dict] = {}
    for f in sorted(Path(d or kdir()).glob("signals_*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            recs[r["sid"]] = r
    return sorted(recs.values(), key=lambda r: (r["t0"], r["pair"]))


def summarize_progress(d: Path | None = None) -> dict:
    shards = [s for s in (read_json(f) for f in sorted(Path(d or kdir()).glob("progress_*.json"))) if s]
    if not shards:
        return {"state": "none", "done": 0, "total": 0}
    done, total = sum(s["done"] for s in shards), sum(s["total"] for s in shards)
    rates = [s["seconds_per_inference"] for s in shards if s.get("seconds_per_inference")]
    running = [s for s in shards if s["state"] == "running"]
    eta = max((s["eta_seconds"] or 0) for s in running) if running and all(s.get("eta_seconds") is not None for s in running) else None
    state = "running" if running else ("stopped" if any(s["state"] == "stopped" for s in shards) else "done" if all(s["state"] == "done" for s in shards) else "idle")
    return {"state": state, "done": done, "total": total, "shards": len(shards), "seconds_per_inference_per_shard": (sum(rates) / len(rates)) if rates else None, "eta_seconds": eta,
            "skipped": {k: sum(s.get("skipped", {}).get(k, 0) for s in shards) for k in {k for s in shards for k in s.get("skipped", {})}}}


# ------------------------------------------------------------------ job
def normalize(raw: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    days = int(raw.get("days", 30))
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise KronosError(f"days must be {MIN_DAYS}..{MAX_DAYS}")
    latest_end = (now - timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    end = _parse(raw["end"]) if raw.get("end") else latest_end
    if end > latest_end:
        raise KronosError(f"the window must end at least 48h before now (latest {_iso(latest_end)}) so that 24h returns exist")
    start = end - timedelta(days=days)
    min_start = datetime.fromisoformat(os.environ.get("REPLAY_MIN_START") or "2026-08-01").replace(tzinfo=timezone.utc)
    if start < min_start:
        raise KronosError(f"the window may not start before {min_start:%Y-%m-%d} (REPLAY_MIN_START: the model's training cut-off is unknown)")
    pairs_n = max(3, min(40, int(raw.get("pairs", 20))))
    return {"days": days, "start": start, "end": end, "pairs": pairs_n, "shards": max(1, min(8, int(raw.get("shards", 1)))),
            "sec_per_inference": float(raw.get("sec_per_inference") or SEC_PER_INFERENCE)}


def estimate(p: dict) -> dict:
    hours = int((p["end"] - p["start"]).total_seconds() // 3600)
    n = hours * p["pairs"]
    seconds = n * p["sec_per_inference"] / p["shards"]
    out = {"hours": hours, "pairs": p["pairs"], "inferences": n, "seconds_per_inference": p["sec_per_inference"], "shards": p["shards"], "eta_hours": round(seconds / 3600, 2),
           "cost_usd": 0.0, "note": "no paid API: CPU time only"}
    if out["eta_hours"] > MAX_HOURS_OK:
        fit = max(3, int(MAX_HOURS_OK * 3600 * p["shards"] / (hours * p["sec_per_inference"])))
        out["warning"] = f"estimated {out['eta_hours']} h is above {MAX_HOURS_OK:g} h: a smaller set of {fit} pairs would fit ({hours} hourly steps x {fit} pairs x {p['sec_per_inference']} s / {p['shards']} worker(s)). Nothing is reduced unless you choose it."
        out["suggested_pairs"] = fit
    return out


def prepare(raw: dict, get, now: datetime | None = None, pairs: list[str] | None = None, d: Path | None = None, log=lambda s: None) -> dict:
    """Writes job.json and the candle files (closed hourly candles up to the END of the window only: no later candle is ever handed to the worker). Resumes the same job
    when the window and pair set are the same (its finished rows are kept); a different job id starts a new signals set."""
    p = normalize(raw, now)
    d = Path(d or kdir())
    coins = pairs or rdata.top_coins("spot", p["pairs"], get)
    if not pairs and "BTC" not in coins:
        coins = ["BTC"] + coins[:-1]
    names = [rdata.pair_name(c, "spot") for c in coins]
    job_id = f"k{p['start']:%Y%m%d}-{p['end']:%Y%m%d}-{len(names)}p-" + "".join(sorted(c[0] for c in coins))[:6].lower()
    warm = p["start"] - timedelta(hours=LOOKBACK + 24)
    entries = []
    for pair in names:
        sym = rdata.bybit_symbol(pair)
        df = rdata.load_candles("spot", sym, warm, p["end"], get)
        rows = [[int(t.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume] for t, r in zip(df["date"].dt.to_pydatetime(), df.itertuples())]
        rows = [r for r in rows if r[0] <= int(p["end"].timestamp() * 1000) - HOUR_MS]      # nothing at or after the last as-of time's own candle
        write_json(d / "candles" / f"{sym}.json", {"symbol": sym, "rows": rows})
        entries.append({"pair": pair, "symbol": sym})
        log(f"candles {sym}: {len(rows)}")
    old = read_json(d / "job.json")
    job = {"id": job_id, "pairs": entries, "start": _iso(p["start"]), "end": _iso(p["end"]), "samples": SAMPLES, "threshold": THRESHOLD, "lookback": LOOKBACK, "created": _iso(now or datetime.now(timezone.utc))}
    if old and old.get("id") != job_id:                                                    # a different job: its rows must not mix with the new ones
        for f in list(d.glob("signals_*.jsonl")) + list(d.glob("progress_*.json")):
            f.unlink()
    write_json(d / "job.json", job)
    stop = d / "STOP"
    if stop.exists():
        stop.unlink()
    return {"job": job, "estimate": estimate(p)}


def request_bench(get, now: datetime | None = None, d: Path | None = None, n: int = 12, symbol: str = "BTCUSDT") -> dict:
    """K1 benchmark request: the candles of one pair up to the last closed hour, plus bench_request.json. The worker answers with bench_result.json (it runs it when it is idle)."""
    now = now or datetime.now(timezone.utc)
    d = Path(d or kdir())
    end = now.replace(minute=0, second=0, microsecond=0)
    df = rdata.load_candles("spot", symbol, end - timedelta(hours=LOOKBACK + n + 24), end, get)
    rows = [[int(t.timestamp() * 1000), r.open, r.high, r.low, r.close, r.volume] for t, r in zip(df["date"].dt.to_pydatetime(), df.itertuples())]
    rows = [r for r in rows if r[0] <= int(end.timestamp() * 1000) - HOUR_MS]
    write_json(d / "candles" / f"{symbol}.json", {"symbol": symbol, "rows": rows})
    req = {"id": "b" + now.strftime("%Y%m%d%H%M%S"), "symbol": symbol, "n": int(n), "samples": SAMPLES, "end": _iso(end)}
    write_json(d / "bench_request.json", req)
    return req


def request_stop(d: Path | None = None) -> None:
    (Path(d or kdir())).mkdir(parents=True, exist_ok=True)
    (Path(d or kdir()) / "STOP").write_text("stop", encoding="utf-8")


def status(d: Path | None = None) -> dict:
    d = Path(d or kdir())
    job = read_json(d / "job.json")
    prog = summarize_progress(d)
    recs = read_signals(d)
    counts = {"up": 0, "down": 0, "neutral": 0, "skipped": 0}
    for r in recs:
        counts[r["signal"]] = counts.get(r["signal"], 0) + 1
    if prog["state"] == "running" and (d / "STOP").exists():
        prog["state"] = "stopping"
    req, res = read_json(d / "bench_request.json"), read_json(d / "bench_result.json")
    if not req:
        bench = {"state": "none"}
    elif res and res.get("id") == req["id"]:
        bench = {"state": "failed", "error": res["error"]} if "error" in res else {"state": "done", **res}
    else:
        bench = {"state": "waiting for the worker (it runs the benchmark when it is idle)", "request": req}
    return {"job": job, "progress": prog, "signals": counts, "n_records": len(recs), "model": read_json(d / "model.json"), "stop_flag": (d / "STOP").exists(), "benchmark": bench}
