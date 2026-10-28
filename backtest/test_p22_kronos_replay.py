"""
P22-K2 -- historical Kronos signals: the look-ahead guard, the fixed signal rule, resumable sharded generation, the job/estimate on the survival-agent side, the container files.
Pure-Python checks with a fake forecaster (no torch, no weights, no network, no Docker).
"""
from __future__ import annotations

import importlib.util
import json
import os
import random
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR = tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": tempfile.mkdtemp(), "REPLAY_STRATEGIES_DIR": str(REPO)})
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


spec = importlib.util.spec_from_file_location("kronos_replay_gen", REPO / "kronos-replay/app/gen.py")
gen = importlib.util.module_from_spec(spec)
sys.modules["kronos_replay_gen"] = gen
spec.loader.exec_module(gen)
from backend.replay import kronos_replay as kr, paths as rp  # noqa: E402

UTC = timezone.utc
H = 3_600_000
T_START = datetime(2026, 9, 1, tzinfo=UTC)


def series(n_hours: int, start=T_START, seed=1) -> list[list[float]]:
    rnd, px, out = random.Random(seed), 100.0, []
    for i in range(n_hours):
        o = px
        px *= 1 + rnd.uniform(-0.01, 0.01)
        out.append([int(start.timestamp() * 1000) + i * H, o, max(o, px) * 1.001, min(o, px) * 0.999, px, 1000 + i])
    return out


def fake_predict(window, t0):
    """A forecast that is a pure function of the window it is shown (so any look-ahead would change it)."""
    last, first = window[-1]["close"], window[-16]["close"]
    drift = (last / first - 1) * 0.5
    return [last * (1 + drift * (k + 1) / 24) for k in range(24)]


# ------------------------------------------------------------------ look-ahead
C = series(24 * 40)
T0 = T_START + timedelta(days=30)
w, why = gen.window_for(C, T0)
check("WINDOW: the newest candle is the one opened at t0 - 1h, and the window holds exactly 480 closed candles", w is not None and w[-1]["open_dt"] == T0 - timedelta(hours=1) and len(w) == 480)
check("WINDOW: no candle opened at t0 or later is in the window", all(r["open_dt"] < T0 for r in w))
rec_a = gen.make_record("X/USDT", "XUSDT", T0, w, fake_predict(w, T0), 3, 0.005)
rnd = random.Random(7)
C2 = [list(r) for r in C]
for r in C2:
    if r[0] >= int(T0.timestamp() * 1000):                 # shuffle / replace EVERY future candle (open time >= t0)
        r[1:5] = [rnd.uniform(1, 1000) for _ in range(4)]
        r[5] = rnd.uniform(1, 9)
w2, _ = gen.window_for(C2, T0)
rec_b = gen.make_record("X/USDT", "XUSDT", T0, w2, fake_predict(w2, T0), 3, 0.005)
check("NO LOOK-AHEAD (fixture): replacing every candle at or after t0 with garbage does not change the window, the forecast or the signal at t0", w == w2 and rec_a == rec_b)
C3 = [list(r) for r in C]
C3[-1][4] = 5.0e6
fut = [r for r in C3 if r[0] >= int(T0.timestamp() * 1000)]
random.Random(3).shuffle(fut)
C3 = [r for r in C3 if r[0] < int(T0.timestamp() * 1000)] + sorted(fut, key=lambda r: random.random())
C3.sort(key=lambda r: r[0])
w3, _ = gen.window_for(C3, T0)
check("NO LOOK-AHEAD: a huge future close and a shuffled future do not change the past signal", w3 == w)
ret_a = gen.make_record("X/USDT", "XUSDT", T0 + timedelta(hours=1), gen.window_for(C, T0 + timedelta(hours=1))[0], [1.0] * 24, 3, 0.005)
check("WINDOW: the next as-of hour sees exactly one more candle (the one opened at t0) and slides by one", gen.window_for(C, T0 + timedelta(hours=1))[0][-1]["open_dt"] == T0 and ret_a["t0"] == gen.iso(T0 + timedelta(hours=1)))
check("WINDOW: the candle opened at t0 - 1h missing (stale) -> the pair is skipped, never forecast from older data", gen.window_for([r for r in C if r[0] != int(T0.timestamp() * 1000) - H], T0)[1] == "stale_candles")
holey = [r for i, r in enumerate(C) if not (int(T0.timestamp() * 1000) // H - 100 == r[0] // H)]
check("WINDOW: a hole inside the window -> skipped as gap_in_window", gen.window_for(holey, T0)[1] == "gap_in_window")
check("WINDOW: fewer than 400 candles of history -> skipped", gen.window_for(series(300), T_START + timedelta(hours=300))[1].startswith("only_"))
check("WINDOW: as-of before the first candle -> skipped", gen.window_for(C, T_START - timedelta(days=1))[1] == "no_candles_before_t0")

# ------------------------------------------------------------------ the fixed signal rule
check("SIGNAL: |ret_4h| >= 0.5% is up / down, anything smaller is neutral (boundaries inclusive, no fee subtracted, no fitting)",
      gen.signal_of(0.005) == "up" and gen.signal_of(-0.005) == "down" and gen.signal_of(0.00499) == "neutral" and gen.signal_of(-0.00499) == "neutral" and gen.signal_of(0.0) == "neutral")
check("SIGNAL: constants are the ones fixed in the spec (threshold 0.5%, 4h horizon, seed 12345, 3 samples in the job)", gen.THRESHOLD == 0.005 and gen.SIGNAL_HORIZON == 4 and gen.SEED == 12345 and kr.SAMPLES == 3 and kr.THRESHOLD == gen.THRESHOLD)
close = w[-1]["close"]
r_ = gen.make_record("X/USDT", "XUSDT", T0, w, [close * 1.02] * 24, 3, 0.005)
check("RECORD: expected returns at 1/4/24h are taken from forecast closes 1, 4 and 24 against the last closed price; signal up; magnitude = |ret_4h|",
      abs(r_["ret_1h"] - 0.02) < 1e-9 and abs(r_["ret_4h"] - 0.02) < 1e-9 and abs(r_["ret_24h"] - 0.02) < 1e-9 and r_["signal"] == "up" and r_["magnitude"] == 0.02 and r_["price"] == close)

# ------------------------------------------------------------------ job, resumable and sharded
def make_job(d: Path, pairs=("AAA", "BBB", "CCC", "DDD"), days=4):
    start = T_START + timedelta(days=21)
    job = {"id": "ktest", "pairs": [{"pair": f"{p}/USDT", "symbol": f"{p}USDT"} for p in pairs], "start": gen.iso(start), "end": gen.iso(start + timedelta(days=days)), "samples": 3, "threshold": 0.005}
    gen.write_json(d / "job.json", job)
    for i, p in enumerate(pairs):
        gen.write_json(d / "candles" / f"{p}USDT.json", {"rows": series(24 * 40, seed=10 + i)})
    return job


calls = []


def counting_predict(window, t0):
    calls.append(t0)
    return fake_predict(window, t0)


d1 = Path(tempfile.mkdtemp())
job1 = make_job(d1)
res = gen.run_shard(job1, d1, counting_predict, log=lambda s: None)
recs = gen.read_signals(d1)
check("RUN: every (pair, hour) of the window gets exactly one record (4 pairs x 96 hours), none twice", res["state"] == "done" and len(recs) == 4 * 96 == len({r["sid"] for r in recs}) and len(calls) == 384)
check("RUN: as-of hours are the hourly grid from start (inclusive) to end (exclusive)", min(r["t0"] for r in recs) == job1["start"] and max(r["t0"] for r in recs) == gen.iso(gen.parse(job1["end"]) - timedelta(hours=1)))
ref_ = {r["sid"]: r for r in recs}
# resume after a stop
d2 = Path(tempfile.mkdtemp())
make_job(d2)
n_ = {"k": 0}


def stopper():
    n_["k"] += 1
    return n_["k"] > 100


gen.run_shard(job1, d2, fake_predict, stop=stopper, log=lambda s: None)
part = len(gen.read_signals(d2))
check("STOP: the worker stops when asked and the progress file says stopped", part == 100 and gen.read_json(d2 / "progress_0.json")["state"] == "stopped")
calls.clear()
gen.run_shard(job1, d2, counting_predict, log=lambda s: None)
rec2 = {r["sid"]: r for r in gen.read_signals(d2)}
check("RESUME: a second run does only the missing 284 inferences and the final file equals an uninterrupted run", len(calls) == 284 and {k: {a: b for a, b in v.items() if a != "_shard"} for k, v in rec2.items()} == {k: {a: b for a, b in v.items() if a != "_shard"} for k, v in ref_.items()})
calls.clear()
gen.run_shard(job1, d2, counting_predict, log=lambda s: None)
check("RESUME: a finished job re-run does nothing (0 inferences)", not calls)
# a kill that left a half-written last line
f_ = d2 / "signals_0.jsonl"
lines = f_.read_text(encoding="utf-8").splitlines()
f_.write_text("\n".join(lines[:-3]) + "\n" + lines[-3][:40], encoding="utf-8")
calls.clear()
gen.run_shard(job1, d2, counting_predict, log=lambda s: None)
rec3 = gen.read_signals(d2)
check("RESUME: a half-written last line (killed mid-write) is redone and does not corrupt the next record", len(calls) == 3 and len(rec3) == 384)
# STOP flag file
d3 = Path(tempfile.mkdtemp())
make_job(d3)
(d3 / "STOP").write_text("x")
res3 = gen.run_shard(job1, d3, fake_predict, log=lambda s: None)
check("STOP FLAG: with the file STOP present the worker computes nothing", res3["state"] == "stopped" and res3["new"] == 0)
# shards
d4 = Path(tempfile.mkdtemp())
make_job(d4)
for s in range(3):
    gen.run_shard(job1, d4, fake_predict, shard=s, nshards=3, log=lambda s_: None)
rec4 = {r["sid"]: {a: b for a, b in r.items() if a != "_shard"} for r in gen.read_signals(d4)}
check("SHARDS: 3 shards together produce exactly the records of one process (the (hour, pair) items are dealt round-robin, nothing is done twice or missed)", rec4 == {k: {a: b for a, b in v.items() if a != "_shard"} for k, v in ref_.items()})
sizes_ = [len(gen.plan(job1, i, 3)) for i in range(3)]
check("SHARDS: the work is balanced (every shard has the same number of items +-1, even when the pairs do not divide evenly)", max(sizes_) - min(sizes_) <= 1 and sum(sizes_) == 384)
sizes8 = [len(gen.plan({**job1, "pairs": [{"pair": f"P{i}/USDT", "symbol": f"P{i}USDT"} for i in range(20)]}, i, 8)) for i in range(8)]
check("SHARDS: 20 pairs over 8 shards -> equal shares (the earlier pair-wise split gave 3 vs 2 pairs)", max(sizes8) - min(sizes8) <= 1)
pr_ = gen.summarize_progress(d4)
check("PROGRESS: shards are summed (done = total), state done", pr_["done"] == pr_["total"] == 384 and pr_["state"] == "done" and pr_["shards"] == 3)
# skipped rows are recorded (so a resume does not retry them)
d5 = Path(tempfile.mkdtemp())
job5 = make_job(d5, pairs=("AAA",))
gen.write_json(d5 / "candles" / "AAAUSDT.json", {"rows": [r for r in series(24 * 40, seed=10) if not (r[0] // H) % 50 == 7]})
gen.run_shard(job5, d5, fake_predict, log=lambda s: None)
rs = gen.read_signals(d5)
check("GAPS: hours whose window has a hole are recorded as signal 'skipped' with the reason (not forecast, not retried on resume)", any(r["signal"] == "skipped" and r["reason"] == "gap_in_window" for r in rs) and len(rs) == 96)
check("PROGRESS: it reports the skipped reasons", gen.summarize_progress(d5)["skipped"].get("gap_in_window", 0) > 0)
prog = gen.read_json(d1 / "progress_0.json")
check("PROGRESS: seconds per inference and an ETA are published while running", "seconds_per_inference" in prog and "eta_seconds" in prog and prog["job_id"] == "ktest")

# ------------------------------------------------------------------ survival-agent side
check("COPIES: the survival-agent copies of read_signals / summarize_progress give the same answers as the worker's", kr.read_signals(d4) == gen.read_signals(d4) and kr.summarize_progress(d4) == gen.summarize_progress(d4) and kr.summarize_progress(Path(tempfile.mkdtemp()))["state"] == "none")
check("PATH: the Kronos replay directory is REPLAY_DIR/kronos (inside the replay sandbox, never the live /kronos volume)", kr.kdir() == (Path(RDIR) / "kronos").resolve() and str(kr.kdir()) != "/kronos")
NOW = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)
p30 = kr.normalize({"days": 30, "pairs": 20, "shards": 1}, NOW)
check("WINDOW: ends at the latest 48h before now (so 24h returns exist) and is 30 days long", p30["end"] == datetime(2026, 10, 4, 9, 0, tzinfo=UTC) and (p30["end"] - p30["start"]).days == 30)
for bad, msg in (({"days": 3}, "days"), ({"days": 30, "end": "2026-10-06T00:00:00Z"}, "48h"), ({"days": 60, "end": "2026-09-01T00:00:00Z"}, "may not start")):
    try:
        kr.normalize(bad, NOW)
        check(f"VALIDATION: {bad} is refused", False)
    except kr.KronosError as e:
        check(f"VALIDATION: {bad} is refused ({msg})", msg in str(e))
e1 = kr.estimate({**p30, "shards": 1})
check("ESTIMATE: 720 h x 20 pairs = 14,400 inferences; one worker at 9.6 s = 38.4 h > 12 h -> a warning that proposes a smaller pair set (and reduces nothing by itself)",
      e1["inferences"] == 14400 and e1["eta_hours"] == 38.4 and "warning" in e1 and e1["suggested_pairs"] == 6 and e1["cost_usd"] == 0.0)
check("ESTIMATE: a plan that fits in 12 h carries no warning", "warning" not in kr.estimate({**kr.normalize({"days": 30, "pairs": 6, "shards": 1}, NOW)}))


def fake_load(cat, sym, start, end, get, sleep=None, interval="60"):
    import pandas as pd
    n = int((end - start).total_seconds() // 3600) + 1
    rows = [[start + timedelta(hours=i), 1.0, 1.0, 1.0, 1.0 + i * 1e-6, 10.0, 10.0] for i in range(n)]
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "quote"])


rd = kr.rdata
orig = (rd.load_candles, rd.top_coins)
rd.load_candles, rd.top_coins = fake_load, lambda cat, n, get, exclude=None: ["ETH", "SOL", "XRP", "ADA"][:n]
dp = Path(tempfile.mkdtemp())
out = kr.prepare({"days": 7, "pairs": 4, "shards": 2}, None, NOW, d=dp)
cf = gen.read_json(dp / "candles" / "BTCUSDT.json")
last = out["job"]["end"]
check("PREPARE: BTC is always in the pair set; job.json and one candle file per pair are written", out["job"]["pairs"][0]["pair"] == "BTC/USDT" and len(out["job"]["pairs"]) == 4 and (dp / "job.json").exists())
check("PREPARE: the candle files stop BEFORE the end of the window -- the worker is never handed a candle from the future of its last as-of time",
      cf["rows"][-1][0] <= int(gen.parse(last).timestamp() * 1000) - H and len(cf["rows"]) > 480)
(dp / "signals_0.jsonl").write_text('{"sid": "a|b|c", "t0": "x", "pair": "p", "signal": "up"}\n')
(dp / "STOP").write_text("x")
out2 = kr.prepare({"days": 7, "pairs": 4, "shards": 2}, None, NOW, d=dp)
check("PREPARE: the same job again keeps its finished rows and clears the STOP flag (this is 'resume')", (dp / "signals_0.jsonl").exists() and not (dp / "STOP").exists() and out2["job"]["id"] == out["job"]["id"])
out3 = kr.prepare({"days": 8, "pairs": 4, "shards": 2}, None, NOW, d=dp)
check("PREPARE: a DIFFERENT job (other window) starts a clean signals set -- rows of two windows never mix", out3["job"]["id"] != out["job"]["id"] and not (dp / "signals_0.jsonl").exists())
kr.request_stop(dp)
st = kr.status(dp)
check("STATUS: shows the job, the stop flag and per-signal counts", st["job"]["id"] == out3["job"]["id"] and st["stop_flag"] is True and set(st["signals"]) >= {"up", "down", "neutral", "skipped"})
rd.load_candles, rd.top_coins = orig

# ------------------------------------------------------------------ container files
dk = (REPO / "kronos-replay/Dockerfile").read_text(encoding="utf-8")
sig = (REPO / "kronos-signal/Dockerfile").read_text(encoding="utf-8")
pins = lambda t: sorted(re.findall(r"(?:KRONOS_COMMIT|KRONOS_MODEL_REV|KRONOS_TOKENIZER_REV)=([0-9a-f]{40})|sha256sum -c|([0-9a-f]{64})  /models", t))
check("CONTAINER: the weights and the model code are baked into the image at the SAME pinned revisions and sha256 as kronos-signal (nothing is downloaded at run time: HF_HUB_OFFLINE=1)",
      pins(dk) == pins(sig) and "HF_HUB_OFFLINE=1" in dk and "snapshot_download" in dk and dk.count("sha256sum -c") == 2 and "KRONOS_MIN_FREE_GB" in dk)
check("CONTAINER: it starts the idle worker, as a non-root user, with no network client needed", 'CMD ["python", "-m", "app.worker"]' in dk and "USER kronos" in dk)
dc = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
blk = dc[dc.index("  kronos-replay:"):dc.index("\nvolumes:\n", dc.index("  kronos-replay:"))]
check("COMPOSE: kronos-replay is CPU-capped (cpus 2, low cpu_shares, 1 GB, pids), uses its own volume, mounts neither the bots' volumes nor survival-agent-data (no secrets), and has no ports",
      "cpus: 2.0" in blk and "cpu_shares:" in blk and "mem_limit: 1g" in blk and "kronos-replay-data:/kronos-replay" in blk and "survival-agent-data" not in blk and "ports:" not in blk and "-db:" not in blk and "FREQTRADE_API" not in blk)
check("COMPOSE: survival-agent mounts the same volume at /data/replay/kronos (replay output only) and the live kronos-data volume is still read-only there", "kronos-replay-data:/data/replay/kronos" in dc and "kronos-data:/kronos:ro" in dc and "kronos-replay-data:" in dc.split("volumes:\n  freqtrade-db:")[1])
wk = (REPO / "kronos-replay/app/worker.py").read_text(encoding="utf-8")
check("WORKER: nice 10 and the disk guard like kronos-signal, idle until a job exists (the model is loaded only when there is one)", "os.nice(10)" in wk and "200 * 1024 * 1024" in wk and "no model is loaded until a job exists" in wk)
bd = (REPO / "backend/Dockerfile").read_text(encoding="utf-8")
check("BACKEND IMAGE: the mount point /data/replay/kronos exists and is world-writable, so that either container may create the volume first", "mkdir -p /data/replay/kronos && chmod 777 /data/replay/kronos" in bd)
check("COPIES: the model adapter of kronos-replay is a copy of kronos-signal's (only the import of the constants differs)",
      (REPO / "kronos-signal/app/predictor.py").read_text(encoding="utf-8").split("from .core import")[1].split("\n", 1)[1].split("meta =")[0]
      == (REPO / "kronos-replay/app/predictor.py").read_text(encoding="utf-8").split("from .gen import")[1].split("\n", 1)[1].split("meta =")[0])

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
