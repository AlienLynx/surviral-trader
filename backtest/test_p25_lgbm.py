"""
P25-D: LightGBM walk-forward study. Offline: synthetic Bybit-shaped data in the carry cache layout, temp REPLAY_DIR.
Covers: no look-ahead in features and in the training rows (purge), shuffled-label placebo, book maths, verdict thresholds, end-to-end report and the wiring (route, runner, UI card, requirements).
"""
import json
import os
import sys
import tempfile
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


tmp = tempfile.mkdtemp()
os.environ["REPLAY_DIR"] = tmp
from backend.replay import carry, lgbm_study as lg, momentum as mo  # noqa: E402

D = carry.DAY_MS
H = 3600 * 1000
N_SYM, N_DAYS = 26, 760
START = datetime(2024, 3, 1, tzinfo=UTC)
M0 = carry.day_start(datetime(2023, 11, 1, tzinfo=UTC))


def mk_sym(i, drift, vol, turnover, noise_seed=0):
    rng = np.random.default_rng(100 + i + noise_seed)
    px = [100.0]
    for k in range(1, N_DAYS):
        px.append(px[-1] * (1.0 + drift + vol * rng.standard_normal()))
    perp = [[M0 + k * D, px[k], px[k], px[k], px[k], 1.0, turnover * (1.0 + 0.001 * k)] for k in range(N_DAYS)]
    idx = [[M0 + k * D, px[k], px[k], px[k], px[k]] for k in range(N_DAYS)]
    fund = [[M0 + k * D + h * H, 0.0] for k in range(N_DAYS) for h in (0, 8, 16)]
    return carry.Sym({"symbol": f"S{i}USDT", "status": "Trading", "launch_ms": M0, "delivery_ms": 0}, perp, idx, fund)


def write_cache(data, end):
    pool = [{"symbol": s.info["symbol"], "status": "Trading", "launch_ms": M0, "delivery_ms": 0} for s in data.values()]
    carry.lf._write_json(carry.cdir("pool.json"), {"fetched_at": end, "items": pool, "spot": [p["symbol"] for p in pool]})
    for s in data.values():
        sym = s.info["symbol"]
        carry.lf._write_json(carry.cdir("perp", f"{sym}.json"), {"rows": [[d, s.p_open[d], s.p_open[d], s.p_open[d], s.p_open[d], 1.0, s.turn[d]] for d in s.days], "vfrom": M0, "vto": end})
        carry.lf._write_json(carry.cdir("index", f"{sym}.json"), {"rows": [[d, v, v, v, v] for d, v in s.i_open.items()], "vfrom": M0, "vto": end})
        carry.lf._write_json(carry.cdir("funding", f"{sym}.json"), {"rows": [[t_, r_] for t_, r_ in zip(s.f_ts, s.f_rate)], "vfrom": M0, "vto": end})


# persistent drifts: the past predicts the future, so a model has something to learn
data = {f"S{i}USDT": mk_sym(i, drift=(i - N_SYM / 2) * 0.0004, vol=0.002, turnover=1e7 * (N_SYM - i % 5)) for i in range(N_SYM)}
end = M0 + (N_DAYS - 3) * D
days, syms, O, T, X = mo.build(data, START, datetime.fromtimestamp(end / 1000, tz=UTC))
k0 = next(i for i, d in enumerate(days) if d >= carry.day_start(START))
kend = len(days) - 2
btc = syms.index("BTCUSDT") if "BTCUSDT" in syms else None

# ------------------------------------------------------------ features
k = k0 + 200
uni = mo.universe_at(T, O, k)
f = lg.feature_rows(days, syms, O, T, X, k, uni, btc)
check("FEATURES: one row per universe coin and one column per named feature", f.shape == (len(uni), len(lg.FEATURES)))
j = lg.FEATURES.index("ret7")
check("FEATURES: ret7 is the open-to-open return over the last 7 days", abs(f[0, j] - (O[uni[0], k] / O[uni[0], k - 7] - 1.0)) < 1e-12)
r = lg.rank01(np.array([3.0, 1.0, 2.0, np.nan]))
check("FEATURES: ranks are 0..1 inside the universe, nan stays nan", r[1] == 0.0 and r[0] == 1.0 and r[2] == 0.5 and np.isnan(r[3]))
O2, X2, T2 = O.copy(), X.copy(), T.copy()
O2[:, k + 1:] *= 1.9
X2[:, k:] *= 3.0
T2[:, k + 1:] *= 5.0
f2 = lg.feature_rows(days, syms, O2, T2, X2, k, uni, btc)
check("NO LOOK-AHEAD: changing every price, return and turnover after the day does not change that day's features", np.allclose(f, f2, equal_nan=True))
lab = lg.labels(O, X, k, uni)
check("LABEL: next 7 days' return minus funding, market neutral (mean 0)", abs(np.nanmean(lab)) < 1e-12 and abs(lab[0] - (np.nansum(X[uni[0], k:k + 7]) - np.mean([np.nansum(X[i, k:k + 7]) for i in uni]))) < 1e-12)

# ------------------------------------------------------------ purge
panel = lg.build_panel(days, syms, O, T, X, 30, kend)
block = k0 + 300
Xtr, ytr = lg.train_rows(panel, block)
n_expected = sum(len(v[0]) for kk, v in panel.items() if v[2] is not None and kk + lg.HORIZON <= block)
check("PURGE: the training rows of a model starting at day b only use days whose label ends by b", Xtr is not None and len(ytr) <= n_expected and len(ytr) > 500)
panel_b = {kk: (u, ff, (ll if kk + lg.HORIZON <= block else np.full(len(ll), 9.0) if ll is not None else None)) for kk, (u, ff, ll) in panel.items()}
Xb, yb = lg.train_rows(panel_b, block)
check("PURGE: labels of days that end after the block start are never read", np.allclose(np.sort(ytr), np.sort(yb)))
_, ys = lg.train_rows(panel, block, np.random.default_rng(1))
check("PLACEBO: shuffling keeps the same label values but breaks the link to the features", np.allclose(np.sort(ys), np.sort(ytr)) and not np.allclose(ys, ytr))

# ------------------------------------------------------------ walk-forward on drifting data
first = k0 + lg.MIN_TRAIN_DAYS
rebal = list(range(first, kend - 7 + 1, 7))
pred = lg.walk_forward(panel, rebal, first)
imp = pred.pop("_importance")
check("WALK: a prediction for (almost) every rebalance day, a model per 28-day block", len(pred) >= len(rebal) - 2 and imp["models"] >= 5)
check("WALK: only rebalance days from the first prediction day on are scored", min(pred) >= first)
sim = lg.simulate_scores(days, O, T, X, pred, first, kend, 0.10)
st = mo.stats(sim["pnl"], first, kend)
check("BOOK: on persistent drifts the model book earns net of costs", st["net_pct_of_sleeve"] > 0)
ic = lg.rank_ic(pred, panel, first, kend)
check("IC: rank information coefficient is clearly positive on persistent drifts", ic.get("mean_ic", 0) > 0.1)
hi = lg.simulate_scores(days, O, T, X, pred, first, kend, 0.30)
check("BOOK: higher slippage never earns more", mo.stats(hi["pnl"], first, kend)["net_pct_of_sleeve"] < st["net_pct_of_sleeve"])
sc = {first: {i: float(n) for n, i in enumerate(uni)}}
one = lg.simulate_scores(days, O, T, X, sc, first, first + 7, 0.10)
w_longs = sorted(sc[first], key=lambda i: sc[first][i])[-4:]
check("BOOK: long the 4 highest scores, short the 4 lowest, first week pays the entry cost", one["rebalances"][0]["n_pos"] == 8 and abs(one["rebalances"][0]["turnover"] - 1.0) < 1e-9 and w_longs[-1] == uni[-1])
# placebo on the same data must not look like the real model
shuf = lg.walk_forward(panel, rebal, first, 500)
shuf.pop("_importance")
sp = lg.simulate_scores(days, O, T, X, shuf, first, kend, 0.10)
check("PLACEBO: the shuffled-label pipeline earns less than the real one on learnable data", mo.stats(sp["pnl"], first, kend)["net_pct_of_sleeve"] < st["net_pct_of_sleeve"])

# ------------------------------------------------------------ verdict
ok = {"days": 300, "apr_pct": 25.0, "max_dd_pct": 12.0, "ci95_daily_apr_pct": [5.0, 45.0]}
icg = {"mean_ic": 0.05}
check("VERDICT: all met -> edge candidate", lg.verdict(ok, {"apr_pct": 5.0}, 99.0, 45, icg, 10.0)["verdict"] == "edge candidate")
check("VERDICT: IC under 0.02 fails", any("IC" in x for x in lg.verdict(ok, {"apr_pct": 5.0}, 99.0, 45, {"mean_ic": 0.0}, 10.0)["failed"]))
check("VERDICT: not beating the 14-day momentum rule fails", any("momentum" in x for x in lg.verdict(ok, {"apr_pct": 5.0}, 99.0, 45, icg, 40.0)["failed"]))
check("VERDICT: placebo percentile under 95 fails", any("placebo" in x for x in lg.verdict(ok, {"apr_pct": 5.0}, 80.0, 45, icg, 10.0)["failed"]))
check("VERDICT: negative with CI under 0 -> no edge", lg.verdict({**ok, "apr_pct": -30.0, "ci95_daily_apr_pct": [-60.0, -5.0]}, {"apr_pct": 5.0}, 10.0, 45, icg, 10.0)["verdict"] == "no edge")
check("VERDICT: too few rebalances -> insufficient n", lg.verdict(ok, {"apr_pct": 5.0}, 99.0, 5, icg, 10.0)["verdict"] == "insufficient n")

# ------------------------------------------------------------ end to end
lg.PLACEBO_SEEDS = 3
write_cache(data, end)
rep = lg.run_study("lgbm-test", {"since": "2024-03-01"}, datetime.fromtimestamp(end / 1000, tz=UTC) + timedelta(days=1))
check("STUDY: report with verdict, rank IC, placebo, baseline momentum, feature importance", rep["verdict"] and rep["rank_ic"]["oos"] and rep["placebo"]["seeds"] == 3 and "momentum_14d_same_days" in rep["benchmarks"] and rep["feature_importance_top"])
lg.write_outputs("lgbm-test", {}, rep)
d_ = Path(tmp) / "lgbm-test"
check("OUTPUT: report.json, report.md, equity.csv written; no internal series in the JSON", all((d_ / f_).is_file() for f_ in ("report.json", "report.md", "equity.csv")) and "_sims" not in json.loads((d_ / "report.json").read_text(encoding="utf-8")))
check("OUTPUT: the markdown carries verdict, IC, placebo, benchmarks, rules, thresholds and limits", all(x in (d_ / "report.md").read_text(encoding="utf-8") for x in ("Verdict", "Rank IC", "Placebo", "Benchmarks", "Rules", "Pass thresholds", "Limits")))

# ------------------------------------------------------------ wiring
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
check("WIRING: route, runner study kind, UI card and requirement exist",
      "/api/replay/lgbm/study" in src("backend/app.py") and "lgbm_study" in src("backend/replay/runner.py") and "lRun" in src("frontend/replay.html") and "lightgbm" in src("backend/requirements.txt"))
check("WIRING: the image has the OpenMP runtime LightGBM needs", "libgomp1" in src("backend/Dockerfile"))

print("\nALL P25 LGBM CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
