"""
P22-K3 -- evaluation of the historical Kronos signals: the computed verdict, the clustered bootstrap, forward returns without look-ahead, the decisions the strategies get,
the end-to-end evaluation on a synthetic market. Pure-Python checks on fixtures; nothing needs Docker, the network, torch or a key.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR = tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": tempfile.mkdtemp(), "REPLAY_STRATEGIES_DIR": str(REPO), "BOTS_DIR": tempfile.mkdtemp(), "ALLOCATOR_DIR": tempfile.mkdtemp(), "REPLAY_MIN_START": "2026-08-01"})
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import data as rd, engine as eng, kronos_eval as ke, kronos_replay as kr  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 9, 10, tzinfo=UTC)
H = 3_600_000
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"]


def synth(seed, hours, start):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.006, hours)
    close = 100 * (1 + (seed % 7)) * np.exp(np.cumsum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, hours)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, hours)))
    vol = rng.uniform(1000, 5000, hours)
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": vol, "quote": vol * close})


# ------------------------------------------------------------------ cluster bootstrap
rng = np.random.default_rng(1)
days = [f"2026-09-{1 + i // 24:02d}" for i in range(24 * 30)]
vals = list(rng.normal(0.01, 0.02, len(days)))
c1, c2 = ke.cluster_ci(vals, days), ke.cluster_ci(vals, days)
check("BOOTSTRAP: deterministic (fixed seed) and the mean lies inside its own CI", c1 == c2 and c1["lo"] < c1["mean"] < c1["hi"] and c1["days"] == 30)
iid = ke.cluster_ci(vals, [str(i) for i in range(len(vals))])
day_wide = ke.cluster_ci(list(np.repeat(rng.normal(0, 0.02, 30), 24)), days)                # every hour of a day carries its day's value: strongly clustered
day_wide_iid = ke.cluster_ci(list(np.repeat(rng.normal(0, 0.02, 30), 24)), [str(i) for i in range(720)])
check("BOOTSTRAP: clustering by day gives a WIDER interval than treating hours as independent when the hours of a day move together (the honest one)", (day_wide["hi"] - day_wide["lo"]) > 2 * (day_wide_iid["hi"] - day_wide_iid["lo"]))
check("BOOTSTRAP: fewer than 2 values -> None", ke.cluster_ci([0.1], ["a"]) is None)

# ------------------------------------------------------------------ verdict rule, branch by branch
ci = lambda lo, hi: {"lo": lo, "hi": hi, "mean": (lo + hi) / 2}
check("VERDICT: n below 200 -> insufficient n whatever the numbers are", ke.direction_verdict(199, ci(0.1, 0.2), (0.1, 0.1)) == "insufficient n")
check("VERDICT: n >= 200, CI lower > 0 and both halves > 0 -> edge candidate", ke.direction_verdict(200, ci(0.001, 0.02), (0.01, 0.01)) == "edge candidate")
check("VERDICT: CI lower > 0 but one half <= 0 -> unproven (the two-halves guard)", ke.direction_verdict(500, ci(0.001, 0.02), (0.01, -0.001)) == "unproven" and ke.direction_verdict(500, ci(0.001, 0.02), (0.01, 0.0)) == "unproven")
check("VERDICT: CI including 0 -> unproven; CI upper < 0 -> no edge", ke.direction_verdict(500, ci(-0.01, 0.02), (0.01, 0.01)) == "unproven" and ke.direction_verdict(500, ci(-0.03, -0.001), (-0.01, -0.01)) == "no edge")
check("VERDICT overall: any edge candidate wins; else any unproven; else any insufficient n; else no edge",
      ke.overall_verdict({"up": "edge candidate", "down": "no edge"}) == "edge candidate" and ke.overall_verdict({"up": "unproven", "down": "no edge"}) == "unproven"
      and ke.overall_verdict({"up": "insufficient n", "down": "no edge"}) == "insufficient n" and ke.overall_verdict({"up": "no edge", "down": "no edge"}) == "no edge")

# ------------------------------------------------------------------ forward returns
N_H = 24 * 12
DF = synth(5, N_H, START - timedelta(days=2))
store0 = rd.SeriesStore()
store0.add("spot", "XUSDT", DF)
cl = {"XUSDT": ke.closes_by_ms(store0, "spot", "XUSDT")}
t0 = START + timedelta(days=3)
rec = {"sid": "s", "t0": ke.kr._iso(t0), "pair": "X/USDT", "symbol": "XUSDT", "signal": "up", "ret_4h": 0.01}
row = ke.forward_rows([rec], cl)[0]
cm = lambda t: float(DF.loc[DF["date"] == pd.Timestamp(t), "close"].iloc[0])
p0 = cm(t0 - timedelta(hours=1))
check("FORWARD: price0 = close of the candle opened at t0-1h; the 1h / 4h / 24h forward = close of the candle opened at t0+h-1h over price0 - 1",
      abs(row["fwd"][1] - (cm(t0) / p0 - 1)) < 1e-12 and abs(row["fwd"][4] - (cm(t0 + timedelta(hours=3)) / p0 - 1)) < 1e-12 and abs(row["fwd"][24] - (cm(t0 + timedelta(hours=23)) / p0 - 1)) < 1e-12)
DF2 = DF.copy()
DF2.loc[DF2["date"] >= pd.Timestamp(t0 + timedelta(hours=4)), ["open", "high", "low", "close"]] = 9.9e9
st2 = rd.SeriesStore()
st2.add("spot", "XUSDT", DF2)
row2 = ke.forward_rows([rec], {"XUSDT": ke.closes_by_ms(st2, "spot", "XUSDT")})[0]
check("FORWARD: the 1h and 4h forward return do not depend on any candle opened 4h or more after t0 (no peeking past the horizon)", row2["fwd"][1] == row["fwd"][1] and row2["fwd"][4] == row["fwd"][4] and row2["fwd"][24] != row["fwd"][24])
check("FORWARD: skipped rows and rows without a price are dropped", ke.forward_rows([{**rec, "signal": "skipped"}, {**rec, "t0": "2026-01-01T00:00:00Z"}], cl) == [])


# ------------------------------------------------------------------ signal quality on planted worlds
def world(edge_pct: float, n_days=30, pairs=10, seed=3, up_share=0.2, down_share=0.2):
    """Rows with a known truth: after `up` the forward 4h return is +edge, after `down` it is -edge (direction-adjusted: +edge), noise sd 1%, market-neutral benchmark."""
    r = np.random.default_rng(seed)
    rows = []
    for k in range(n_days * 24):
        t = START + timedelta(hours=k)
        for p in range(pairs):
            u = r.random()
            sig = "up" if u < up_share else "down" if u < up_share + down_share else "neutral"
            drift = edge_pct if sig == "up" else -edge_pct if sig == "down" else 0.0
            f4 = drift + r.normal(0, 0.01)
            rows.append({"t0": ke.kr._iso(t), "pair": f"P{p}/USDT", "signal": sig, "ret_4h_pred": 0.0, "day": ke.kr._iso(t)[:10], "fwd": {1: f4 / 3, 4: f4, 24: f4 * 2}})
    return rows


END = START + timedelta(days=30)
q_edge = ke.signal_quality(world(0.03), START, END, 0.1, 0.3)
check("QUALITY: a planted +3% edge per signal (cost 0.8%) -> edge candidate in both directions, net and gross", q_edge["verdict"] == "edge candidate" and q_edge["signal_skill_gross"] == "edge candidate"
      and q_edge["by_direction"]["up"]["verdict"] == "edge candidate" and q_edge["by_direction"]["down"]["verdict"] == "edge candidate")
q_cost = ke.signal_quality(world(0.005), START, END, 0.1, 0.3)
check("QUALITY: a real but small +0.5% edge is a gross edge candidate but NOT a net one (it does not pay the 0.8% round trip): skill without a tradable edge", q_cost["signal_skill_gross"] == "edge candidate" and q_cost["verdict"] != "edge candidate")
q_none = ke.signal_quality(world(0.0), START, END, 0.1, 0.3)
check("QUALITY: no planted edge -> never an edge candidate", q_none["verdict"] != "edge candidate" and q_none["signal_skill_gross"] != "edge candidate")
q_neg = ke.signal_quality(world(-0.03), START, END, 0.1, 0.3)
check("QUALITY: an INVERTED signal (the model is wrong-way) is `no edge`, in both directions", q_neg["verdict"] == "no edge" and all(d["verdict"] == "no edge" for d in q_neg["by_direction"].values()))
w_small = world(0.03, n_days=3, pairs=3)
q_small = ke.signal_quality(w_small, START, START + timedelta(days=3), 0.1, 0.3)
check("QUALITY: too few signals -> insufficient n, with the shortfall stated per direction", q_small["verdict"] == "insufficient n" and set(q_small["shortfall"]) == {"up", "down"} and all(v > 0 for v in q_small["shortfall"].values()))
# the two-halves guard: an edge that exists only in the first half
rows_h = world(0.03)
mid = START + timedelta(days=15)
for r in rows_h:
    if r["t0"] >= ke.kr._iso(mid):
        r["fwd"] = {k: v - (0.06 if r["signal"] == "up" else -0.06 if r["signal"] == "down" else 0) for k, v in r["fwd"].items()}      # kill the edge in the second half
q_half = ke.signal_quality(rows_h, START, END, 0.1, 0.3)
check("QUALITY: an edge that exists in the first half only is NOT an edge candidate (same sign in both halves is required)", q_half["verdict"] != "edge candidate")
mkt = world(0.0)
shift = {}
for r in mkt:
    shift.setdefault(r["t0"], np.random.default_rng(abs(hash(r["t0"])) % 2**32).normal(0, 0.02))
    r["fwd"] = {k: v + shift[r["t0"]] for k, v in r["fwd"].items()}
check("QUALITY: a common market move in every pair of an hour does not create an edge (benchmark = mean of all pairs at that t0)", ke.signal_quality(mkt, START, END, 0.1, 0.3)["signal_skill_gross"] != "edge candidate")
check("QUALITY: net = gross minus the round-trip cost (2 x (fee + slippage) = 0.8%)", abs(q_edge["by_direction"]["up"]["horizons"]["4"]["excess_gross"]["mean_pct"] - q_edge["by_direction"]["up"]["horizons"]["4"]["excess_net"]["mean_pct"] - 0.8) < 1e-3
      and q_edge["cost_round_trip_pct"] == 0.8)
check("QUALITY: the verdict horizon is 4h and n_min 200 (fixed in advance)", q_edge["verdict_horizon_h"] == 4 and q_edge["n_min"] == 200 and set(q_edge["by_direction"]["up"]["horizons"]) == {"1", "4", "24"})

# ------------------------------------------------------------------ decisions for the strategies
recs = [{"sid": f"s{i}", "t0": ke.kr._iso(START + timedelta(hours=i)), "pair": "ETH/USDT", "symbol": "ETHUSDT", "signal": s, "ret_4h": 0.01, "price": 1.0} for i, s in enumerate(["up", "down", "neutral", "skipped"])]
dm, ds_ = ke.decisions_for("main", recs), ke.decisions_for("shorts", recs)
check("DECISIONS main: up -> buy, everything else hold, on the spot pair; a skipped hour gives no decision", [d["action"] for d in dm] == ["buy", "hold", "hold"] and all(d["pair"] == "ETH/USDT" for d in dm))
check("DECISIONS shorts: down -> sell_short, everything else hold, on the perpetual (COIN/USDT:USDT)", [d["action"] for d in ds_] == ["hold", "sell_short", "hold"] and all(d["pair"] == "ETH/USDT:USDT" for d in ds_))
job_ = {"start": ke.kr._iso(START), "end": ke.kr._iso(START + timedelta(hours=10)), "pairs": [{"pair": "A/USDT"}, {"pair": "B/USDT"}]}
rr_ = [{"t0": ke.kr._iso(START + timedelta(hours=h)), "pair": p} for h in range(10) for p in ("A/USDT", "B/USDT") if not (h >= 6 and p == "B/USDT")]
check("PARTIAL: the evaluated part is the contiguous prefix for which EVERY pair has a record (a stopped generator still gives a clean early window)", ke.covered_end(rr_, job_) == START + timedelta(hours=6))
check("PARTIAL: nothing complete yet -> None", ke.covered_end([], job_) is None)

# ------------------------------------------------------------------ end to end on a synthetic market
E_START = datetime(2026, 9, 10, tzinfo=UTC)
E_END = E_START + timedelta(days=4)
WARM = E_START - timedelta(days=eng.WARMUP_DAYS)
HOURS = int((E_END - WARM).total_seconds() // 3600) + 60
store = rd.SeriesStore()
for i, c in enumerate(COINS):
    df = synth(i + 1, HOURS, WARM)
    store.add("spot", f"{c}USDT", df)
    store.add("linear", f"{c}USDT", df)
by_cat = {"spot": [rd.pair_name(c, "spot") for c in COINS], "linear": [rd.pair_name(c, "linear") for c in COINS]}
d = Path(tempfile.mkdtemp())
job = {"id": "ktest", "pairs": [{"pair": p, "symbol": rd.bybit_symbol(p)} for p in by_cat["spot"]], "start": ke.kr._iso(E_START), "end": ke.kr._iso(E_END), "samples": 3, "threshold": 0.005}
kr.write_json(d / "job.json", job)
r2 = np.random.default_rng(11)
with (d / "signals_0.jsonl").open("w") as fh:
    for k in range(96):
        t = E_START + timedelta(hours=k)
        for p in job["pairs"]:
            u = r2.random()
            sig = "up" if u < 0.3 else "down" if u < 0.6 else "neutral"
            fh.write(json.dumps({"sid": f"kronos_replay|{p['pair']}|{ke.kr._iso(t)}", "t0": ke.kr._iso(t), "pair": p["pair"], "symbol": p["symbol"], "price": 100.0, "ret_1h": 0.0, "ret_4h": {"up": 0.01, "down": -0.01}.get(sig, 0.0),
                                 "ret_24h": 0.0, "signal": sig, "magnitude": 0.01}) + "\n")
out = ke.evaluate({"engine": True, "minute_prices": False}, None, d=d, store_and_pairs=(store, by_cat))
check("E2E: the evaluation runs on the whole planted window and labels the verdict (one of the four labels)", out["verdict"] in ke.VERDICTS and out["window"]["partial"] is False and out["window"]["days_covered"] == 4.0)
check("E2E: the replay engine ran main and shorts with the Kronos decisions AND the strategy's own rule, same window and pairs", set(out["engine"]) == {"main", "shorts"} and all(set(m) >= {"kronos", "rule"} for m in out["engine"].values()))
check("E2E: Kronos decisions are what the strategies saw: actions in the engine's decisions match the signals (main buys only on up, shorts sells only on down)",
      out["engine"]["main"]["kronos"]["actions"]["sell_short"] == 0 and out["engine"]["shorts"]["kronos"]["actions"]["buy"] == 0 and out["engine"]["main"]["kronos"]["actions"]["buy"] > 0 and out["engine"]["shorts"]["kronos"]["actions"]["sell_short"] > 0)
check("E2E: replay never gives the status `edge` on its own trades", all(e["status"] != "edge" for m in out["engine"].values() for e in m.values() if isinstance(e, dict)))
check("E2E: baselines present -- BTC hold, the no-LLM rule (mode rule), random entries with the same trade count (per bot), earlier LLM runs listed (none here)",
      out["baselines"]["btc_buy_and_hold"] is not None and out["engine"]["main"]["kronos"]["random_baseline"] is not None and out["baselines"]["llm_earlier_runs"] == [])
base = d / "eval" / out["eval_id"]
check("E2E: outputs written under <kronos dir>/eval/<id>/: params, report.json/.md, signals, decisions, trades, equity", all((base / f).exists() for f in ("params.json", "report.json", "report.md", "signals.jsonl", "decisions.jsonl", "trades.csv", "equity.csv")))
z = zipfile.ZipFile(io.BytesIO(ke.zip_eval(out["eval_id"], d)))
check("E2E: the ZIP holds all of them", set(z.namelist()) >= {"params.json", "report.json", "report.md", "signals.jsonl", "decisions.jsonl", "trades.csv", "equity.csv"} and ke.zip_eval("../x", d) is None)
md = (base / "report.md").read_text(encoding="utf-8")
check("E2E: the markdown names the model, the verdict rule's cost, the stand-in for shorts_v2 and the memory caveat", "Kronos-small" in md and "round-trip cost" in md and "stand-in for shorts_v2" in md and "memory" in md)
check("E2E: latest_eval returns the report", ke.latest_eval(d)["eval_id"] == out["eval_id"])
before = {p.name for p in Path(RDIR).iterdir()}
check("ISOLATION: the evaluation wrote only below the Kronos directory it was given (nothing else appeared in REPLAY_DIR)", before <= {"cache"} or before == set())
# insufficient history -> a clear error
try:
    ke.evaluate({}, None, d=Path(tempfile.mkdtemp()))
    check("ERROR: no job -> KronosError", False)
except kr.KronosError as e:
    check("ERROR: no job -> a readable instruction to generate first", "generate" in str(e))

# ------------------------------------------------------------------ API and the page
import shutil  # noqa: E402
import time  # noqa: E402

Path(os.environ["DATA_DIR"], "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402
from backend.replay import runner as rr  # noqa: E402

cl = TestClient(app_module.app)
KD = kr.kdir()
shutil.rmtree(KD, ignore_errors=True)
s0 = cl.get("/api/replay/kronos/state").json()
check("API state: with no job it reports no job, benchmark none, the default estimate (20 pairs, 30 days) with the 12-hour warning for ONE worker, and no evaluation",
      s0["job"] is None and s0["benchmark"]["state"] == "none" and s0["estimate"]["inferences"] == 14400 and "warning" in s0["estimate"] and s0["latest_eval"] is None)
check("API estimate: refuses a window that is too short / too recent with the reason", cl.post("/api/replay/kronos/estimate", json={"days": 3}).status_code == 400 and cl.post("/api/replay/kronos/estimate", json={"days": 30, "end": "2099-01-01T00:00:00Z"}).status_code == 400)
check("API generate: without confirm=true nothing is written (409) -- the card shows the estimate first and the server insists on the confirm step", cl.post("/api/replay/kronos/generate", json={"days": 7, "pairs": 4}).status_code == 409 and not (KD / "job.json").exists())
check("API evaluate: with no signals it is a readable 409", cl.post("/api/replay/kronos/evaluate", json={}).status_code == 409)
orig_ = (kr.rdata.load_candles, kr.rdata.top_coins)


def fake_load(cat, sym, start, end, get, sleep=None, interval="60"):
    n = int((end - start).total_seconds() // 3600) + 1
    return pd.DataFrame([[start + timedelta(hours=i), 1.0, 1.0, 1.0, 1.0 + i * 1e-6, 10.0, 10.0] for i in range(n)], columns=["date", "open", "high", "low", "close", "volume", "quote"])


kr.rdata.load_candles, kr.rdata.top_coins = fake_load, lambda cat, n, get, exclude=None: ["ETH", "SOL", "XRP", "ADA"][:n]
g = cl.post("/api/replay/kronos/generate", json={"days": 7, "pairs": 4, "confirm": True})
check("API generate (confirmed): writes job.json + candles under REPLAY_DIR/kronos and returns the estimate", g.status_code == 200 and (KD / "job.json").exists() and (KD / "candles" / "BTCUSDT.json").exists() and g.json()["estimate"]["inferences"] == 7 * 24 * 4)
b = cl.post("/api/replay/kronos/benchmark", json={"n": 6})
st = cl.get("/api/replay/kronos/state").json()
check("API benchmark: writes a request and the state says it waits for the worker", b.status_code == 200 and st["benchmark"]["state"].startswith("waiting"))
kr.write_json(KD / "bench_result.json", {"id": b.json()["id"], "n": 6, "mean_s": 9.6, "median_s": 9.5, "p95_s": 9.9, "peak_rss_mb": 461, "threads": 2})
check("API benchmark: the worker's result is shown with seconds per inference and RAM", cl.get("/api/replay/kronos/state").json()["benchmark"]["state"] == "done" and cl.get("/api/replay/kronos/state").json()["benchmark"]["peak_rss_mb"] == 461)
cl.post("/api/replay/kronos/stop")
check("API stop: sets the STOP flag (the worker stops after its current inference) and the state shows it", (KD / "STOP").exists() and cl.get("/api/replay/kronos/state").json()["stop_flag"] is True)
kr.rdata.load_candles, kr.rdata.top_coins = orig_
# evaluation through the API on the synthetic market of the end-to-end test
shutil.rmtree(KD, ignore_errors=True)
kr.write_json(KD / "job.json", job)
shutil.copy(d / "signals_0.jsonl", KD / "signals_0.jsonl")
ke_orig = ke.load_store
ke.load_store = lambda p, get, spot_pairs, log=lambda s: None: (store, by_cat)
ev = cl.post("/api/replay/kronos/evaluate", json={"minute_prices": False})
for _ in range(600):
    stt = cl.get("/api/replay/status").json()
    if stt.get("phase") in ("done", "failed"):
        break
    time.sleep(0.5)
ke.load_store = ke_orig
check("API evaluate: a background job runs the evaluation to the end without cost", ev.status_code == 200 and stt.get("phase") == "done" and stt.get("kind") == "kronos_eval" and stt["kronos_eval"]["verdict"] in ke.VERDICTS)
s1 = cl.get("/api/replay/kronos/state").json()
check("API state after the evaluation: carries the latest report (verdict, quality, engine) for the card", s1["latest_eval"] and s1["latest_eval"]["verdict"] == stt["kronos_eval"]["verdict"] and "quality" in s1["latest_eval"])
zr = cl.get("/api/replay/kronos/eval/" + s1["latest_eval"]["eval_id"] + "/download")
check("API download: the ZIP of the evaluation; a bad id is a 404", zr.status_code == 200 and zipfile.ZipFile(io.BytesIO(zr.content)).namelist() and cl.get("/api/replay/kronos/eval/..x/download").status_code == 404)
routes = app_module.app.openapi()["paths"]
check("API routes: state (GET), estimate / generate / stop / benchmark / evaluate (POST), eval download (GET)", set(routes["/api/replay/kronos/state"]) == {"get"} and all(set(routes["/api/replay/kronos/" + x]) == {"post"} for x in ("estimate", "generate", "stop", "benchmark", "evaluate"))
      and set(routes["/api/replay/kronos/eval/{eval_id}/download"]) == {"get"})
html = (REPO / "frontend/calibration.html").read_text(encoding="utf-8")
check("PAGE: the Kronos tab of /calibration has the Kronos card with Benchmark, Generate signals (progress + ETA), Stop, Evaluate, the verdict badge, the per-direction table, the engine table, baselines, ZIP and a copyable line",
      all(x in html for x in ("kCard", "kBench", "kGen", "kStop", "kEval", "kVerdict", "kTable", "kEngine", "kBaselines", "kZip", "kCopy", "kBar", "api/replay/kronos/state", "api/replay/kronos/generate", "api/replay/kronos/evaluate")))
check("PAGE: Generate shows the estimate and asks for a confirmation before anything starts", "kEstimate()" in html and "Generate Kronos signals?" in html and "confirm: true" in html)
check("LIVE FILES: the live /kronos files are not referenced by the replay Kronos modules", all("kronos_signals.jsonl" not in (REPO / f).read_text(encoding="utf-8") for f in ("backend/replay/kronos_eval.py", "backend/replay/kronos_replay.py")))

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
