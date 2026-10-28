"""
P26: ranker research studies on the Replay page -- robustness grid (hold x universe) and premium-index features. Offline: a synthetic Binance cache in the study's layout, a fake archive for the
premium files, temp REPLAY_DIR. Nothing needs the network, a key or Docker.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

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
from backend.replay import lgbm_grid as gr, lgbm_hourly as lh, lgbm_premium as pm, listing_fade as lf, momentum as mo  # noqa: E402

HOUR = 3600 * 1000
lh.SINCE = "2022-10-01"
gr.SINCE = pm.SINCE = lh.SINCE
lh.UNIVERSE_N, lh.QUINTILE = 12, 3
lh.PARAMS = {**lh.PARAMS, "n_estimators": 40, "n_jobs": 2, "min_child_samples": 50}
gr.HOLDS, gr.UNIVERSES, gr.PRIMARY = (3, 5), (12,), (12, 3)

# ======================================================================= verdicts (pure)
def cell(ic, apr, apr30=10.0):
    return {"ic_oos": {"mean_ic": ic}, "slip0.1": {"oos": {"apr_pct": apr}}, "slip0.3": {"oos": {"apr_pct": apr30}}}


check("GRID VERDICT: IC and APR positive in at least 10 of 12 cells -> neighbourhood holds", gr.verdict([cell(0.05, 20.0)] * 12)["verdict"] == "neighbourhood holds")
check("GRID VERDICT: IC positive in only 7 of 12 -> fragile", gr.verdict([cell(0.05, 20.0)] * 7 + [cell(-0.01, -5.0)] * 5)["verdict"] == "fragile")
check("GRID VERDICT: in between -> mixed (money fails in some cells though the IC is positive)", gr.verdict([cell(0.05, 20.0)] * 9 + [cell(0.04, -3.0)] * 3)["verdict"] == "mixed")
check("GRID VERDICT: no cells -> insufficient n", gr.verdict([])["verdict"] == "insufficient n")


def res(ic, ic_locked, apr30):
    return {"ic_oos": {"mean_ic": ic}, "ic_locked": {"mean_ic": ic_locked}, "slip0.3": {"oos": {"apr_pct": apr30}}}


check("PREMIUM VERDICT: IC +0.012, APR at 0.30% not lower, locked IC not lower -> adopt", pm.verdict(res(0.069, 0.066, 60.0), res(0.081, 0.070, 66.0))["verdict"] == "adopt")
v = pm.verdict(res(0.069, 0.066, 60.0), res(0.075, 0.070, 66.0))
check("PREMIUM VERDICT: IC gain below 0.01 -> keep v2 features, the failed condition is named", v["verdict"] == "keep v2 features" and v["failed"] == ["oos_ic_up_by_0.01"])
check("PREMIUM VERDICT: more IC but worse after costs, or worse locked IC, keeps v2", pm.verdict(res(0.069, 0.066, 60.0), res(0.085, 0.070, 40.0))["verdict"] == "keep v2 features" and pm.verdict(res(0.069, 0.066, 60.0), res(0.085, 0.050, 70.0))["verdict"] == "keep v2 features")

# ======================================================================= a synthetic Binance cache
T0 = datetime(2022, 10, 1, tzinfo=UTC)
T_END = datetime(2025, 3, 20, tzinfo=UTC)
N = int((T_END - T0).total_seconds() // 3600)
TS = np.array([int((T0 + timedelta(hours=h)).timestamp() * 1000) for h in range(N)], dtype=np.int64)
SYMS = [f"S{i}USDT" for i in range(15)] + ["BTCUSDT"]
SER = {}
for i, sym in enumerate(SYMS):
    rng = np.random.default_rng(700 + i)
    r = (i - 8) * 0.00001 + 0.0008 * rng.standard_normal(N)
    px = 100.0 * np.cumprod(1 + r)
    qv = 1e6 * (len(SYMS) - i % 5) * (1 + 0.1 * rng.random(N))
    SER[sym] = (px, qv, qv * (0.5 + 0.03 * rng.standard_normal(N)), 0.0005 * np.sin(np.arange(N) / 50.0 + i) + 0.0002 * rng.standard_normal(N))


def month_bounds(y, m):
    a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
    b = int(datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC).timestamp() * 1000)
    return a, b


months = []
y, m = 2022, 10
while (y, m) <= (2025, 2):
    months.append((y, m))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
for sym in SYMS:
    px, qv, tb, _ = SER[sym]
    for (y, m) in months:
        a, b = month_bounds(y, m)
        sel = np.flatnonzero((TS >= a) & (TS < b))
        nxt = np.minimum(sel + 1, N - 1)
        lh._save(lh.cdir("k", sym, f"{y}-{m:02d}.npy"), np.column_stack([TS[sel], px[sel], px[sel] * 1.002, px[sel] * 0.998, px[nxt], qv[sel], np.full(len(sel), 1000.0), tb[sel]]))
        fts = [a + d * 86_400_000 + h * HOUR for d in range(27) for h in (0, 8, 16)]
        lh._save(lh.cdir("f", sym, f"{y}-{m:02d}.npy"), np.array([[t, 0.0001] for t in fts]))
lf._write_json(lh.cdir("symbols.json"), {"fetched_at": 0, "symbols": SYMS})
NOW = datetime(2025, 3, 12, tzinfo=UTC)

# ======================================================================= the premium download
calls = {"n": 0}


class Resp:
    def __init__(self, code=200, content=b""):
        self.status_code, self.content = code, content


def premium_zip(sym, y, m):
    a, b = month_bounds(y, m)
    sel = np.flatnonzero((TS >= a) & (TS < b))
    prem = SER[sym][3]
    lines = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"]
    for j in sel:
        lines.append(f"{TS[j]},{prem[j]},{prem[j]},{prem[j]},{prem[j]},0,{TS[j] + HOUR - 1},0,720,0,0,0")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "\n".join(lines))
    return buf.getvalue()


def fake_get(url, params=None, timeout=None, **kw):
    calls["n"] += 1
    import re
    mt = re.search(r"/premiumIndexKlines/([A-Z0-9]+)/1h/[A-Z0-9]+-1h-(\d{4})-(\d{2})\.zip$", url)
    if not mt:
        return Resp(404)
    sym, y, m = mt.group(1), int(mt.group(2)), int(mt.group(3))
    if sym == "S2USDT" and (y, m) == (2022, 10):
        return Resp(404)
    return Resp(200, premium_zip(sym, y, m))


res_f = pm.fetch_all(fake_get, NOW, pace=0.0, sleep=lambda s: None)
jobs = sum(len(pm.cached_months(s)) for s in pm.universe_symbols(NOW))
check("DOWNLOAD: the premium files of every contract in the universe, in the months the klines cache has, no errors", res_f["errors"] == 0 and res_f["jobs"] == jobs and lh.cdir("p", "S3USDT", "2023-05.npy").is_file())
check("DOWNLOAD: a month the archive does not have is remembered (a .none marker), not requested again", lh.cdir("p", "S2USDT", "2022-10.none").is_file())
n1 = calls["n"]
res_f2 = pm.fetch_all(fake_get, NOW, pace=0.0, sleep=lambda s: None)
check("DOWNLOAD: a second run requests nothing (resumable)", calls["n"] == n1 and res_f2["errors"] == 0)
stop = threading.Event()
stop.set()
import shutil
shutil.rmtree(lh.cdir("p"))
n2 = calls["n"]
check("DOWNLOAD: a stop request ends it without downloading", pm.fetch_all(fake_get, NOW, stop=stop, pace=0.0, sleep=lambda s: None)["stopped"] and calls["n"] == n2)
pm.fetch_all(fake_get, NOW, pace=0.0, sleep=lambda s: None)

# ======================================================================= premium matrix and features
start = datetime(2023, 1, 1, tzinfo=UTC)
days, syms, M, Od, Td, X, F = lh.build(start, NOW - timedelta(days=1), 12)
P = pm.premium_matrix(syms, days[0], M["o"].shape[1])
j = syms.index("S3USDT")
h = int((TS[5000] - days[0]) // HOUR)
check("MATRIX: premium close lands on the same hourly grid as the prices", abs(float(P[j, h]) - SER["S3USDT"][3][5000]) < 1e-6)
check("MATRIX: coverage is high where the cache has data", np.mean(~np.isnan(P[:, : 24 * 400])) > 0.8)
k = 24 * 0 + len(days) - 60
uni = mo.universe_at(Td, Od, k, 12)
f1 = pm.prem_features(P, k, uni)
P2 = P.copy()
P2[:, 24 * k:] = 1.0
check("FEATURES: 4 premium features + 4 daily ranks per coin", f1.shape == (len(uni), 8) and np.all((f1[:, 4:][~np.isnan(f1[:, 4:])] >= 0) & (f1[:, 4:][~np.isnan(f1[:, 4:])] <= 1)))
check("NO LOOK-AHEAD: changing every premium value from 00:00 of the decision day on does not change the features", np.allclose(f1, pm.prem_features(P2, k, uni), equal_nan=True))
P3 = P.copy()
P3[:, 24 * k - 1] += 0.05
check("FEATURES: the last completed hour does reach them (prem_last moves)", not np.allclose(f1[:, 3], pm.prem_features(P3, k, uni)[:, 3], equal_nan=True))
del M

# ======================================================================= the grid, end to end
rg = gr.run_study("lgg-test", {"since": "2022-10-01"}, NOW)
check("GRID: one cell per hold x universe, each with two slippages, locked and out-of-sample stats, a rank IC; the primary is flagged", len(rg["cells"]) == 2 and rg["cells"][0]["primary"] is True and rg["cells"][1]["primary"] is False
      and all({"slip0.1", "slip0.3", "ic_oos"} <= set(c) for c in rg["cells"]) and rg["counts"]["cells"] == 2)
check("GRID: the verdict is computed (one of the three or insufficient n), with its counts", rg["verdict"] in ("neighbourhood holds", "mixed", "fragile", "insufficient n") and rg["counts"]["ic_positive"] in (0, 1, 2))
gr.write_outputs("lgg-test", {}, rg)
dg = Path(os.environ["REPLAY_DIR"]) / "lgg-test"
check("GRID OUTPUT: report.json and report.md written, the markdown has the verdict, the table and the limits", (dg / "report.json").is_file() and all(x in (dg / "report.md").read_text(encoding="utf-8") for x in ("Verdict", "| universe |", "Costs are the usual killer")))

# ======================================================================= the premium study, end to end
rp = pm.run_study("lgp-test", {"since": "2022-10-01"}, NOW)
check("PREMIUM STUDY: base and +premium, each with out-of-sample and locked IC and stats at two slippages; coverage reported", set(rp["results"]) == {"base", "+premium"} and all({"ic_oos", "ic_locked", "slip0.1", "slip0.3"} <= set(r) for r in rp["results"].values()) and rp["coverage_pct"] > 50)
check("PREMIUM STUDY: the verdict is adopt or keep v2 features, from the three named conditions; the premium features' share of the gain is listed", rp["verdict"] in ("adopt", "keep v2 features") and set(rp["conditions"]) == {"oos_ic_up_by_0.01", "apr_at_0.30_not_lower", "locked_ic_not_lower"} and len(rp["premium_gain_share_pct"]) == 8)
check("PREMIUM STUDY: with no premium data in the cache it refuses instead of comparing identical sets", True)
shutil.rmtree(lh.cdir("p"))
try:
    pm.run_study("lgp-none", {"since": "2022-10-01"}, NOW)
    refused = False
except RuntimeError as e:
    refused = "no premium-index data" in str(e)
check("PREMIUM STUDY: ... refused with a clear message", refused)
pm.write_outputs("lgp-test", {}, rp)
check("PREMIUM OUTPUT: report.json and report.md written", (Path(os.environ["REPLAY_DIR"]) / "lgp-test" / "report.md").is_file() and "Premium coverage" in (Path(os.environ["REPLAY_DIR"]) / "lgp-test" / "report.md").read_text(encoding="utf-8"))

# ======================================================================= wiring: runner, routes, Replay page
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
rr = src("backend/replay/runner.py")
check("WIRING: runner kinds, routes and download labels exist", "lgbm_grid_study" in rr and "lgbm_premium_study" in rr and "/api/replay/lgbm-grid/study" in src("backend/app.py") and "/api/replay/lgbm-premium/study" in src("backend/app.py")
      and "Ranker robustness grid" in src("backend/downloads.py") and "Premium features study" in src("backend/downloads.py"))
check("WIRING: both modules take the arguments the study runner passes (run_id, params, until, log)", "def run_study(run_id: str, params: dict, until" in src("backend/replay/lgbm_grid.py") and "def run_study(run_id: str, params: dict, until" in src("backend/replay/lgbm_premium.py"))
html = src("frontend/replay.html")
check("INTERFACE: two Studies-tab cards with run button, verdict badge, table and download links; render functions; run-list badges; the new verdicts have colours",
      all(x in html for x in ("gRun", "pRun", "renderGrid", "renderPremium", "gTable", "pTable", "'lgbm_grid_study'", "'lgbm_premium_study'", "neighbourhood holds", "keep v2 features", "ranker grid")))
check("INTERFACE: the ranker section on the dashboard points to the research on the Replay page", "/replay#studies" in src("frontend/index.html"))

print("\nALL P26 RESEARCH CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
