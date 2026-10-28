"""
P26: maker-fill cost model (limit orders vs taker execution). Offline: hand-built price paths with exact expectations, a synthetic Binance cache, synthetic 5-minute Fade paths.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp())
os.environ["REPLAY_DIR"] = str(tmp / "replay")
os.environ["DATA_DIR"] = str(tmp / "data")
from backend.replay import lgbm_hourly as lh, lgbm_study as lg, listing_fade as lf, maker_study as mk  # noqa: E402

HOUR = 3600 * 1000
# ======================================================================= the fill rule
check("FILL: a buy limit 0.1% below the reference fills only if the low reaches it", mk.fill_decision(True, 100.0, 100.5, 99.89, 0.001) is True and mk.fill_decision(True, 100.0, 100.5, 99.95, 0.001) is False)
check("FILL: a sell limit 0.1% above fills only if the high reaches it", mk.fill_decision(False, 100.0, 100.11, 99.0, 0.001) is True and mk.fill_decision(False, 100.0, 100.05, 99.0, 0.001) is False)
check("FILL: a missing price window never fills", mk.fill_decision(True, 100.0, float("nan"), float("nan"), 0.001) is False)
check("FILL: delta 0 (limit at the open) fills any bar that touches it, the optimistic bound", mk.fill_decision(True, 100.0, 101.0, 99.0, 0.0) is True)

# ======================================================================= the book: exact arithmetic
S, D = 2, 14
H = (D + 1) * 24


def paths(o0=100.0, low=None, high=None, next_open=None):
    """two coins, flat prices; hour-bar highs / lows overridable for the rebalance day (day 3)."""
    o = np.full((S, H), o0, dtype=np.float32)
    hi = np.full((S, H), o0, dtype=np.float32)
    lo = np.full((S, H), o0, dtype=np.float32)
    h0 = 24 * 3
    if low is not None:
        lo[0, h0] = low[0]
        lo[1, h0] = low[1]
    if high is not None:
        hi[0, h0] = high[0]
        hi[1, h0] = high[1]
    if next_open is not None:
        o[0, h0 + 2] = next_open[0]
        o[1, h0 + 2] = next_open[1]
    return {"o": o, "hi": hi, "lo": lo}


days = list(range(D + 2))
X0 = np.zeros((S, D))
scores = {3: {0: 1.0, 1: -1.0}}
kw = dict(k0=3, kend=9, hold=3, q=1)
d, fm, ft, sl = 0.001, mk.FEE_MAKER / 100, mk.FEE_TAKER / 100, mk.SLIP / 100
Mfill = paths(low=(99.8, 100.0), high=(100.0, 100.2))
r = mk.simulate_passive(days, Mfill, X0, scores, mode="chase", delta=d, timeout_h=2, **kw)
check("BOOK (filled): both orders fill; the book earns delta - maker fee on the traded notional (1.0), pays nothing else", r["filled"] == 2 and abs(r["pnl"][3] - (d - fm) * 1.0) < 1e-9)
r = mk.simulate_passive(days, paths(), X0, scores, mode="skip", delta=d, timeout_h=2, **kw)
check("BOOK (not filled, skip): nothing is traded, no cost, no position -> zero P&L everywhere", r["skipped"] == 2 and r["filled"] == 0 and np.all(r["pnl"] == 0.0))
Mchase = paths(next_open=(101.0, 100.0))
r = mk.simulate_passive(days, Mchase, X0, scores, mode="chase", delta=d, timeout_h=2, **kw)
exp_chase = -(0.5 * (0.01 + ft + sl)) - 0.5 * (0.0 + ft + sl)
check("BOOK (not filled, chase): the long chased a coin that rose 1% (pays the move + taker fee + slippage), the short a flat coin (taker fee + slippage)", r["filled"] == 0 and abs(r["pnl"][3] - exp_chase) < 1e-9)
r_short_chase = mk.simulate_passive(days, paths(next_open=(100.0, 99.0)), X0, scores, mode="chase", delta=d, timeout_h=2, **kw)
check("BOOK (chase): a short whose coin FELL 1% before the chase sells lower and is charged for it (price moves against the seller)", abs(r_short_chase["pnl"][3] - (-(0.5 * (0.0 + ft + sl)) - 0.5 * (0.01 + ft + sl))) < 1e-9)

# taker mode reproduces the study's book exactly, on random data with a moving market and several rebalances
rng = np.random.default_rng(5)
S2, D2 = 14, 60
H2 = (D2 + 1) * 24
o2 = (100 * np.cumprod(1 + 0.001 * rng.standard_normal((S2, H2)), axis=1)).astype(np.float32)
u1, u2 = rng.random((S2, H2)), rng.random((S2, H2))
M2 = {"o": o2, "hi": (o2 * (1 + 0.001 * u1)).astype(np.float32), "lo": (o2 * (1 - 0.001 * u2)).astype(np.float32)}           # a delta of 0.05% is reached about half of the time
Od2 = o2[:, ::24].astype(np.float64)
X2 = Od2[:, 1:] / Od2[:, :-1] - 1.0
sc2 = {k: {i: float(rng.standard_normal()) for i in range(S2)} for k in range(5, 50, 3)}
days2 = list(range(D2 + 2))
ref = lg.simulate_scores(days2, Od2, None, X2, sc2, 5, 55, 0.10, 3, 3)
mine = mk.simulate_passive(days2, M2, X2, sc2, 5, 55, 3, 3, "taker", 0.0, 0, 0.10)
check("TAKER MODE: the execution model with taker costs reproduces lg.simulate_scores (the v2 study's book) to the last digit on random data", np.allclose(ref["pnl"], mine["pnl"], atol=1e-12) and mine["orders"] > 20)
pas = mk.simulate_passive(days2, M2, X2, sc2, 5, 55, 3, 3, "chase", 0.0005, 2)
check("CHASE with a tiny delta on a noisy market: some orders fill, some are chased, and the fill rate is a fraction", 0 < pas["filled"] < pas["orders"] and pas["filled"] + pas["chased"] == pas["orders"])
sk = mk.simulate_passive(days2, M2, X2, sc2, 5, 55, 3, 3, "skip", 0.0005, 2)
check("SKIP mode keeps the old weight for an unfilled order, never chases, and ends on a different path than chase", sk["chased"] == 0 and sk["skipped"] > 0 and sk["skipped"] + sk["filled"] == sk["orders"] and not np.allclose(sk["pnl"], pas["pnl"]))

# verdict
def g(apr, fill=0.8):
    return {"oos": {"apr_pct": apr}, "fill_rate": fill}


check("VERDICT: chase as good as the taker book at 0.10%, skip positive, 80% fill -> passive pays", mk.ranker_verdict(g(90.0), g(40.0), g(85.0))["verdict"] == "passive pays")
v = mk.ranker_verdict(g(70.0, 0.3), g(-5.0, 0.3), g(85.0))
check("VERDICT: worse than taker, skip negative and few fills -> no better, all three reasons named", v["verdict"] == "no better" and len(v["failed"]) == 3)

# ======================================================================= the ranker part on a synthetic Binance cache
lh.SINCE = "2022-10-01"
lh.UNIVERSE_N, lh.QUINTILE = 12, 3
lh.PARAMS = {**lh.PARAMS, "n_estimators": 40, "n_jobs": 2, "min_child_samples": 50}
mk.SINCE = lh.SINCE
T0 = datetime(2022, 10, 1, tzinfo=UTC)
T_END = datetime(2025, 3, 20, tzinfo=UTC)
N = int((T_END - T0).total_seconds() // 3600)
TS = np.array([int((T0 + timedelta(hours=h)).timestamp() * 1000) for h in range(N)], dtype=np.int64)
SYMS = [f"S{i}USDT" for i in range(15)] + ["BTCUSDT"]
for i, sym in enumerate(SYMS):
    rng = np.random.default_rng(1100 + i)
    r_ = (i - 8) * 0.00001 + 0.0008 * rng.standard_normal(N)
    px = 100.0 * np.cumprod(1 + r_)
    qv = 1e6 * (len(SYMS) - i % 5) * (1 + 0.1 * rng.random(N))
    y, m = 2022, 10
    while (y, m) <= (2025, 2):
        a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        b = int(datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC).timestamp() * 1000)
        sel = np.flatnonzero((TS >= a) & (TS < b))
        nxt = np.minimum(sel + 1, N - 1)
        lh._save(lh.cdir("k", sym, f"{y}-{m:02d}.npy"), np.column_stack([TS[sel], px[sel], px[sel] * 1.002, px[sel] * 0.998, px[nxt], qv[sel], np.full(len(sel), 1000.0), qv[sel] * 0.5]))
        lh._save(lh.cdir("f", sym, f"{y}-{m:02d}.npy"), np.array([[a + dd * 86_400_000 + hh * HOUR, 0.0001] for dd in range(27) for hh in (0, 8, 16)]))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
lf._write_json(lh.cdir("symbols.json"), {"fetched_at": 0, "symbols": SYMS})
NOW = datetime(2025, 3, 12, tzinfo=UTC)
rp = mk.ranker_part(NOW)
check("RANKER PART: two taker baselines, the full grid (2 modes x 3 deltas x 3 timeouts), a computed verdict", set(rp["baseline"]) == {"taker_slip0.1", "taker_slip0.3"} and len(rp["grid"]) == 18 and rp["verdict"] in ("passive pays", "no better"))
check("RANKER PART: the primary (0.10%, 2 h, chase) is in the grid; fill rates are fractions; skip mode never chases", any(x["mode"] == "chase" and x["delta"] == 0.001 and x["timeout_h"] == 2 for x in rp["grid"])
      and all(0.0 <= x["fill_rate"] <= 1.0 for x in rp["grid"]) and all(x["chased"] == 0 for x in rp["grid"] if x["mode"] == "skip"))
check("RANKER PART: a longer wait never fills less (4 h >= 1 h) for the same delta and mode", all(next(x for x in rp["grid"] if x["mode"] == m_ and x["delta"] == dl and x["timeout_h"] == 4)["fill_rate"] >= next(x for x in rp["grid"] if x["mode"] == m_ and x["delta"] == dl and x["timeout_h"] == 1)["fill_rate"] - 1e-9 for m_ in ("skip", "chase") for dl in mk.DELTAS))
check("RANKER PART: a bigger delta never fills more (0.20% <= 0.05%) for the same wait", all(next(x for x in rp["grid"] if x["mode"] == "chase" and x["delta"] == 0.002 and x["timeout_h"] == t)["fill_rate"] <= next(x for x in rp["grid"] if x["mode"] == "chase" and x["delta"] == 0.0005 and x["timeout_h"] == t)["fill_rate"] + 1e-9 for t in mk.TIMEOUTS))

# ======================================================================= the Fade part on synthetic 5-minute paths
rules = lf.load_rules(REPO)
UT = datetime(2025, 9, 1, 10, 0, tzinfo=UTC)


def m5_frame(prices, start):
    p = np.asarray(prices, float)
    o = np.concatenate([[p[0]], p[:-1]])
    return pd.DataFrame({"date": pd.date_range(start, periods=len(p), freq="5min", tz="UTC"), "open": o, "high": np.maximum(o, p) * 1.0004, "low": np.minimum(o, p) * 0.9996, "close": p, "volume": 1.0, "quote": 1.0})


def ev(sym, times):
    return {"inst": {"symbol": sym, "launch_ms": 0, "status": "Trading", "tick": 0.0001}, "df": pd.DataFrame(), "funding": [], "flagged": False, "first_hour_quote": 50000.0,
            "signals": [{"i": k, "entry_time": t, "age_days": 3.0, "funding": None} for k, t in enumerate(times)]}


# a coin that first bounces +0.3% (a limit 0.1% above fills) and then squeezes against the short; another that falls at once (a limit above never fills, and it is the winner)
bounce = np.concatenate([np.full(288, 1.0), [1.0, 1.003, 1.0], np.linspace(1.0, 1.25, 80), np.full(288 * 16, 1.25)])             # bounces +0.3% in the first candles (a limit 0.1% above fills), then squeezes: the short is stopped out
drop = np.concatenate([np.full(288, 1.0), np.linspace(1.0, 0.85, 80), np.linspace(0.85, 0.90, 12), np.full(288 * 16, 0.90)])
events = [ev("BOUNCEUSDT", [UT]), ev("DROPUSDT", [UT + timedelta(minutes=0)])]
cache = {"BOUNCEUSDT": m5_frame(bounce, UT - timedelta(days=1)), "DROPUSDT": m5_frame(drop, UT - timedelta(days=1))}
mk.FADE_DELTAS, mk.FADE_TIMEOUT_MIN = (0.0, 0.001, 0.05), (15,)
fp = mk.fade_part(rules, events, cache, datetime(2026, 1, 1, tzinfo=UTC))
rows = {x["delta"]: x for x in fp["rows"]}
check("FADE PART: one row per delta x wait, two replayed trades", len(fp["rows"]) == 3 and fp["trades_replayed"] == 2 and all(x["trades"] == 2 for x in fp["rows"]))
check("FADE PART: a limit at the open fills both coins (fill rate 1.0); a limit 0.1% above fills the one that bounces; a limit 5% above fills none", rows[0.0]["fill_rate"] == 1.0 and rows[0.001]["fill_rate"] == 0.5 and rows[0.05]["fill_rate"] == 0.0)
check("FADE PART: adverse selection is measured: the coin that never bounced (the winning short) is the unfilled one at 0.1%, the filled one lost", rows[0.001]["taker_mean_usd_unfilled"] is not None and rows[0.001]["taker_mean_usd_filled"] is not None and rows[0.001]["taker_mean_usd_unfilled"] > 0 > rows[0.001]["taker_mean_usd_filled"])
check("FADE PART: skipping the unfilled winner makes the total worse than the all-fill case", rows[0.001]["net_usd_skip"] < rows[0.0]["net_usd_skip"])
check("FADE PART: with nothing filling and skip, the total is zero; chase still trades them as a taker later", rows[0.05]["net_usd_skip"] == 0.0 and rows[0.05]["net_usd_chase"] != 0.0)

# ======================================================================= the whole study degrades gracefully
rep = mk.run_study("mk-test", {}, NOW)
check("STUDY: the ranker part runs; a missing Listing Fade cache is reported as 'not run', not raised; the verdict comes from the ranker", rep["ranker"] is not None and rep["fade"] is None and any("Fade part not run" in e for e in rep["errors"]) and rep["verdict"] == rep["ranker"]["verdict"])
mk.write_outputs("mk-test", {}, rep)
dd_ = Path(os.environ["REPLAY_DIR"]) / "mk-test"
md = (dd_ / "report.md").read_text(encoding="utf-8")
check("OUTPUT: report.json and report.md written; the markdown has the verdict, the grid, the maker fee and the limits", (dd_ / "report.json").is_file() and all(x in md for x in ("Ranker verdict", "Part 1", "maker 0.02", "NOT RUN", "queue position")) or "Maker fee 0.02" in md)
import shutil
shutil.rmtree(lh.cdir("k"))
try:
    mk.run_study("mk-none", {}, NOW)
    both = False
except RuntimeError as e:
    both = "ranker part not run" in str(e)
check("STUDY: when neither cache exists it refuses with the reasons", both)

# ======================================================================= the Fade strategy hook (passive entry in the simulator) and wiring
tr0 = lf.simulate_trade(rules, UT, cache["DROPUSDT"], [], 0.055, 0.30, 25.0)
tr1 = lf.simulate_trade(rules, UT, cache["DROPUSDT"], [], 0.055, 0.30, 25.0, entry_mult=1.001, entry_slip_pct=0.0)
check("SIMULATOR: a passive entry (fill 0.1% above the open, no entry slippage) changes the entry price and improves the short's result; default arguments are unchanged", tr1["open_rate"] > tr0["open_rate"] and tr1["profit_ratio"] > tr0["profit_ratio"]
      and lf.simulate_trade(rules, UT, cache["DROPUSDT"], [], 0.055, 0.30, 25.0) == tr0)
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
check("WIRING: runner kind, route, download label and the Replay card exist", "maker_study" in src("backend/replay/runner.py") and "/api/replay/maker/study" in src("backend/app.py") and "Maker-fill cost model" in src("backend/downloads.py")
      and all(x in src("frontend/replay.html") for x in ("kRun", "renderMaker", "kTable", "kTable2", "passive pays", "maker fills")))
check("WIRING: the module takes the arguments the study runner passes (run_id, params, until, log)", "def run_study(run_id: str, params: dict, until" in src("backend/replay/maker_study.py"))

print("\nALL P26 MAKER CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
