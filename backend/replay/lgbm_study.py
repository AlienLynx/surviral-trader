"""
P25-D: LightGBM walk-forward study -- can a small gradient-boosted model, trained only on the past, rank liquid USDT perpetuals well enough to earn money AFTER costs? Rule based pipeline, no LLM,
$0, CPU only, nothing is traded. It reuses the momentum study's cached daily Bybit data, universe and cost model, so it adds no download.

Pre-registered design (fixed before the run; nothing is tuned on the out-of-sample period):
  universe   the top N=20 USDT perpetuals by trailing 30-day turnover AS OF each day (delisted ones included while they traded)
  features   known at 00:00 of day k: returns over 1/3/7/14/28 days, 14-day volatility, 7d/30d turnover ratio, 7-day funding paid, distance from the 28-day high, BTC 7-day return and BTC 28-day
             volatility (market regime); each coin feature is also given as its rank inside the universe that day
  label      the coin's return over the next HORIZON=7 days (open to open, minus funding) minus the universe mean for the same days (market neutral), cross-sectionally ranked
  model      LightGBM regressor, FIXED small parameters (no search): 7 leaves, 200 trees, learning rate 0.03, min 100 rows per leaf, 70% row / column sampling
  walk       expanding window; the model is retrained every RETRAIN_DAYS=28 days on rows whose label is already known (purged: label window ends before the first prediction day); the first
             MIN_TRAIN_DAYS=365 days are only training
  book       identical to the momentum study: every 7 days long the top quintile, short the bottom quintile of the PREDICTION, half of the sleeve per leg, fees 0.055% + slippage 0.10% per side
  placebo    the same pipeline trained on SHUFFLED labels (the model cannot know anything), PLACEBO_SEEDS runs: where does the real model's out-of-sample return rank among them?
  baseline   the momentum study's primary book (14-day return) over the same days: the model has to add something to a one-line rule
Verdict thresholds are in PASS. Known limits (in every report): daily bars, no intraday risk or liquidation model, a contract Bybit removed from its API altogether is invisible, 2.7 years is one regime,
and a model that has found nothing is the usual outcome -- this study exists to find that out for $0.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np

from . import carry, momentum as mo, paths

DAY = carry.DAY_MS
SINCE = mo.SINCE
LOCK_END = carry.LOCK_END
HORIZON = 7
RETRAIN_DAYS = 28
MIN_TRAIN_DAYS = 365
UNIVERSE_N = mo.UNIVERSE_N
QUINTILE = mo.QUINTILE
PLACEBO_SEEDS = 20
PARAMS = {"objective": "regression", "num_leaves": 7, "n_estimators": 200, "learning_rate": 0.03, "min_child_samples": 100, "subsample": 0.7, "subsample_freq": 1, "colsample_bytree": 0.7,
          "reg_lambda": 5.0, "verbose": -1, "n_jobs": 2, "random_state": 7}
RET_LAGS = (1, 3, 7, 14, 28)
FEATURES = [f"ret{n}" for n in RET_LAGS] + ["vol14", "turn_ratio", "funding7", "dist_high28", "btc_ret7", "btc_vol28"]
FEATURES += [f"rk_{f}" for f in FEATURES[:9]]
PASS = {**mo.PASS, "min_ic": 0.02, "min_beats_momentum_apr_pct": 0.0}


def _lightgbm():
    try:
        import lightgbm
    except ImportError as e:   # noqa: BLE001
        raise RuntimeError("lightgbm is not installed (pip install lightgbm; it is in backend/requirements.txt)") from e
    return lightgbm


# ------------------------------------------------------------------ features and labels
def rank01(v: np.ndarray) -> np.ndarray:
    """Rank inside the universe scaled to 0..1 (nan stays nan)."""
    out = np.full(len(v), np.nan)
    ok = ~np.isnan(v)
    n = int(ok.sum())
    if n >= 2:
        order = np.argsort(np.argsort(v[ok]))
        out[ok] = order / (n - 1)
    return out


def feature_rows(days, syms, O, T, X, k: int, uni: list[int], btc: int | None):
    """One feature row per coin of the universe at day index k. Uses only data at or before day k (opens up to O[:, k], daily returns X[:, :k] which end at the open of day k)."""
    if k < 30:
        return None
    rows = []
    with np.errstate(invalid="ignore", divide="ignore"):
        for i in uni:
            o = O[i]
            if np.isnan(o[k]):
                rows.append([np.nan] * 9 + [np.nan] * 0)
                continue
            r = [o[k] / o[k - n] - 1.0 if not np.isnan(o[k - n]) else np.nan for n in RET_LAGS]
            daily = o[k - 14:k + 1][1:] / o[k - 14:k + 1][:-1] - 1.0
            vol14 = float(np.nanstd(daily)) if np.sum(~np.isnan(daily)) >= 10 else np.nan
            t7, t30 = np.nanmean(T[i, k - 7:k]) if np.any(~np.isnan(T[i, k - 7:k])) else np.nan, np.nanmean(T[i, k - 30:k]) if np.any(~np.isnan(T[i, k - 30:k])) else np.nan
            ret_d = o[k - 7:k + 1][1:] / o[k - 7:k + 1][:-1] - 1.0
            fund7 = float(np.nansum(ret_d - X[i, k - 7:k])) if np.sum(~np.isnan(X[i, k - 7:k])) >= 5 else np.nan   # ret - (ret - funding) = funding paid by a long
            hi = np.nanmax(o[k - 28:k + 1])
            rows.append(r + [vol14, t7 / t30 if t30 and not np.isnan(t30) else np.nan, fund7, o[k] / hi - 1.0])
    if btc is not None and not np.isnan(O[btc, k]) and not np.isnan(O[btc, k - 7]):
        bd = O[btc, k - 28:k + 1][1:] / O[btc, k - 28:k + 1][:-1] - 1.0
        b = [O[btc, k] / O[btc, k - 7] - 1.0, float(np.nanstd(bd))]
    else:
        b = [np.nan, np.nan]
    base = np.array(rows, dtype=float)
    ranks = np.column_stack([rank01(base[:, j]) for j in range(9)])
    return np.column_stack([base, np.tile(b, (len(uni), 1)), ranks])


def labels(O, X, k: int, uni: list[int], horizon: int = HORIZON) -> np.ndarray | None:
    """Return over the next `horizon` days (minus funding) minus the universe mean; nan when the coin has no bars."""
    seg = X[uni, k:k + horizon]
    raw = np.nansum(seg, axis=1)
    raw[np.all(np.isnan(seg), axis=1)] = np.nan
    m = np.nanmean(raw) if np.any(~np.isnan(raw)) else np.nan
    return raw - m


def build_panel(days, syms, O, T, X, k_first: int, k_last: int):
    """All (day, coin) rows for k in [k_first, k_last]: dict k -> (uni, features, label|None). Labels need k + HORIZON <= last return column."""
    btc = syms.index("BTCUSDT") if "BTCUSDT" in syms else None
    panel = {}
    for k in range(k_first, k_last + 1):
        uni = mo.universe_at(T, O, k)
        if len(uni) < 8:
            continue
        f = feature_rows(days, syms, O, T, X, k, uni, btc)
        if f is None:
            continue
        lab = labels(O, X, k, uni) if k + HORIZON <= X.shape[1] else None
        panel[k] = (uni, f, lab)
    return panel


# ------------------------------------------------------------------ walk-forward
def train_rows(panel: dict, k_max_label: int, shuffle_rng=None, horizon: int = HORIZON):
    """Rows whose label window ends before the first day the model predicts (purged)."""
    Xs, ys = [], []
    for k, (uni, f, lab) in panel.items():
        if lab is None or k + horizon > k_max_label:
            continue
        ok = ~np.isnan(lab) & ~np.isnan(f[:, 0])
        if ok.sum() < 5:
            continue
        y = lab[ok]
        if shuffle_rng is not None:
            y = shuffle_rng.permutation(y)       # shuffled WITHIN the day: the model keeps no information
        Xs.append(f[ok])
        ys.append(y)
    if not Xs:
        return None, None
    return np.vstack(Xs), np.concatenate(ys)


def walk_forward(panel: dict, rebal_ks: list[int], first_pred_k: int, shuffle_seed: int | None = None, log=lambda s: None, horizon: int = HORIZON, features: list[str] | None = None,
                 params: dict | None = None, retrain_days: int = RETRAIN_DAYS, quintile: int = QUINTILE) -> dict[int, dict[int, float]]:
    """Predictions {k: {symbol index: score}} for every rebalance day, each from a model trained only on labels known before the model's block starts."""
    lgb = _lightgbm()
    preds: dict[int, dict[int, float]] = {}
    rng = np.random.default_rng(shuffle_seed) if shuffle_seed is not None else None
    block_start = first_pred_k
    last = max(rebal_ks)
    feats = features or FEATURES
    importances = np.zeros(len(feats))
    n_models = 0
    while block_start <= last:
        block_end = block_start + retrain_days
        Xtr, ytr = train_rows(panel, block_start, rng, horizon)
        if Xtr is not None and len(ytr) >= 500:
            model = lgb.LGBMRegressor(**(params or PARAMS))
            model.fit(Xtr, ytr)
            importances += np.asarray(model.booster_.feature_importance(importance_type="gain"), dtype=float)
            n_models += 1
            for k in rebal_ks:
                if block_start <= k < block_end and k in panel:
                    uni, f, _ = panel[k]
                    ok = ~np.isnan(f[:, 0])
                    if ok.sum() >= 2 * quintile:
                        p = model.predict(f[ok])
                        preds[k] = {uni[j]: float(s) for j, s in zip(np.flatnonzero(ok), p)}
        block_start = block_end
    preds["_importance"] = {"models": n_models, "gain": {n: float(g) for n, g in zip(feats, importances)}}      # type: ignore[assignment]
    return preds


# ------------------------------------------------------------------ book from predictions
def simulate_scores(days, O, T, X, scores: dict, k0: int, kend: int, slip_pct: float, hold: int = mo.REBALANCE_DAYS, quintile: int = QUINTILE) -> dict:
    """Same weekly book as the momentum study but ranked by the model's score. Days without a prediction are not traded (flat)."""
    cost_side = (mo.FEE_PCT + slip_pct) / 100.0
    pnl = np.zeros(len(days) - 1)
    reb, prev = [], {}
    k = k0
    while k + hold <= kend:
        sc = scores.get(k)
        w = {}
        if sc:
            order = sorted(sc, key=lambda i: sc[i])
            q = quintile
            w = {**{i: 0.5 / q for i in order[-q:]}, **{i: -0.5 / q for i in order[:q]}}
        traded = sum(abs(w.get(i, 0.0) - prev.get(i, 0.0)) for i in set(w) | set(prev))
        pnl[k] -= traded * cost_side
        reb.append({"k": k, "n_pos": len(w), "turnover": round(traded, 3)})
        for i, wi in w.items():
            pnl[k:k + hold] += wi * np.nan_to_num(X[i, k:k + hold], nan=0.0)
        prev = w
        k += hold
    return {"pnl": pnl, "rebalances": reb}


def rank_ic(scores: dict, panel: dict, lo_k: int, hi_k: int) -> dict:
    """Spearman correlation between the score and the realised label, one value per rebalance day."""
    ics = []
    for k, sc in scores.items():
        if not (lo_k <= k < hi_k) or k not in panel:
            continue
        uni, _, lab = panel[k]
        if lab is None:
            continue
        pairs = [(sc[i], lab[j]) for j, i in enumerate(uni) if i in sc and not np.isnan(lab[j])]
        if len(pairs) < 8:
            continue
        a, b = np.array(pairs).T
        ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
        c = np.corrcoef(ra, rb)[0, 1]
        if not np.isnan(c):
            ics.append(float(c))
    if len(ics) < 10:
        return {"n": len(ics)}
    x = np.array(ics)
    ci = mo.block_ci(x, block=4)
    return {"n": len(ics), "mean_ic": round(float(x.mean()), 4), "ic_hit_pct": round(float(np.mean(x > 0)) * 100, 1), "ci95": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}


def verdict(oos: dict, locked: dict, percentile: float | None, rebalances_oos: int, ic: dict, baseline_apr: float | None) -> dict:
    if oos.get("days", 0) < 100 or rebalances_oos < PASS["min_oos_rebalances"]:
        return {"verdict": "insufficient n", "failed": [f"out-of-sample rebalances {rebalances_oos} < {PASS['min_oos_rebalances']}"]}
    failed = []
    ci = oos.get("ci95_daily_apr_pct")
    if oos["apr_pct"] < PASS["min_oos_net_apr_pct"]:
        failed.append(f"out-of-sample net APR {oos['apr_pct']}% < {PASS['min_oos_net_apr_pct']}%")
    if ci is None or ci[0] <= 0:
        failed.append("block-bootstrap CI of the out-of-sample mean does not exclude 0")
    if (locked.get("apr_pct") or 0) <= 0:
        failed.append("the locked period is not positive")
    if oos["max_dd_pct"] > PASS["max_dd_pct_of_sleeve"]:
        failed.append(f"max drawdown {oos['max_dd_pct']}% of the sleeve > {PASS['max_dd_pct_of_sleeve']}%")
    if percentile is None or percentile < PASS["min_placebo_percentile"]:
        failed.append(f"percentile vs shuffled-label placebo {percentile} < {PASS['min_placebo_percentile']}")
    if (ic.get("mean_ic") or 0) < PASS["min_ic"]:
        failed.append(f"out-of-sample rank IC {ic.get('mean_ic')} < {PASS['min_ic']}")
    if baseline_apr is not None and oos["apr_pct"] <= baseline_apr + PASS["min_beats_momentum_apr_pct"]:
        failed.append(f"does not beat the one-line 14-day momentum rule ({baseline_apr}% APR)")
    if not failed:
        return {"verdict": "edge candidate", "failed": []}
    return {"verdict": "no edge" if (ci is not None and ci[1] < 0) else "unproven", "failed": failed}


# ------------------------------------------------------------------ study
def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    data = carry.load_data()
    if len(data) < 12:
        raise RuntimeError("not enough cached perpetual data: the carry fetch has to run first")
    _lightgbm()
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    days, syms, O, T, X = mo.build(data, start, end)
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(LOCK_END).replace(tzinfo=timezone.utc)))
    # the data matrices start 40 days before the window: the model may train on them, but it only trades from first_pred_k
    first_pred_k = k0 + MIN_TRAIN_DAYS
    if kend - first_pred_k < 200:
        raise RuntimeError(f"not enough history for a walk-forward: {kend - k0} days, need at least {MIN_TRAIN_DAYS + 200}")
    log(f"building the feature panel for {kend - 30} days")
    panel = build_panel(days, syms, O, T, X, 30, kend)
    rebal = list(range(first_pred_k, kend - mo.REBALANCE_DAYS + 1, mo.REBALANCE_DAYS))
    log(f"walk-forward: {len(rebal)} rebalances, retrain every {RETRAIN_DAYS} days, first prediction {datetime.fromtimestamp(days[first_pred_k] / 1000, tz=timezone.utc).date()}")
    real = walk_forward(panel, rebal, first_pred_k, None, log)
    imp = real.pop("_importance")
    results, sims = {}, {}
    for slip in (mo.SLIP_PCT, mo.SLIP_STRESS_PCT):
        sim = simulate_scores(days, O, T, X, real, first_pred_k, kend, slip)
        key = f"lgbm_slip{slip}"
        sims[key] = sim
        results[key] = {"all": mo.stats(sim["pnl"], first_pred_k, kend), "locked": mo.stats(sim["pnl"], first_pred_k, lock_k), "oos": mo.stats(sim["pnl"], lock_k, kend),
                        "rebalances_oos": sum(1 for r in sim["rebalances"] if r["k"] >= lock_k and r["n_pos"]), "avg_turnover": round(float(np.mean([r["turnover"] for r in sim["rebalances"]])), 3) if sim["rebalances"] else None}
    p_key = f"lgbm_slip{mo.SLIP_PCT}"
    # baseline: the momentum primary over the SAME days
    bsim = mo.simulate(days, O, T, X, first_pred_k, kend, mo.PRIMARY["lookback_days"], mo.PRIMARY["mode"], mo.SLIP_PCT)
    baseline = {"all": mo.stats(bsim["pnl"], first_pred_k, kend), "locked": mo.stats(bsim["pnl"], first_pred_k, lock_k), "oos": mo.stats(bsim["pnl"], lock_k, kend)}
    ib = syms.index("BTCUSDT") if "BTCUSDT" in syms else None
    bench = {"momentum_14d_same_days": baseline}
    if ib is not None:
        btc = np.nan_to_num(X[ib], nan=0.0)
        bench["btc_hold"] = {"oos": mo.stats(btc, lock_k, kend)}
    ic_oos = rank_ic(real, panel, lock_k, kend)
    ic_locked = rank_ic(real, panel, first_pred_k, lock_k)
    log(f"placebo: {PLACEBO_SEEDS} pipelines trained on shuffled labels")
    placebo = []
    for seed in range(PLACEBO_SEEDS):
        sp = walk_forward(panel, rebal, first_pred_k, 500 + seed, log)
        sp.pop("_importance", None)
        sm = simulate_scores(days, O, T, X, sp, first_pred_k, kend, mo.SLIP_PCT)
        placebo.append(float(sm["pnl"][lock_k:kend].mean()) * 365 * 100)
    prim = results[p_key]
    pct = round(100.0 * float(np.mean([prim["oos"].get("apr_pct", -1e9) > a for a in placebo])), 1)
    v = verdict(prim["oos"], prim["locked"], pct, prim["rebalances_oos"], ic_oos, baseline["oos"].get("apr_pct"))
    total = sum(imp["gain"].values()) or 1.0
    top = sorted(imp["gain"].items(), key=lambda kv: -kv[1])[:8]
    return {"type": "lgbm_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": p_key, "pass_thresholds": PASS,
            "window": {"start": start.date().isoformat(), "first_prediction": datetime.fromtimestamp(days[first_pred_k] / 1000, tz=timezone.utc).date().isoformat(), "end": end.date().isoformat(), "lock_end": LOCK_END},
            "universe": {"symbols_in_cache": len(syms), "universe_n": UNIVERSE_N},
            "rules": {"horizon_days": HORIZON, "retrain_days": RETRAIN_DAYS, "min_train_days": MIN_TRAIN_DAYS, "model": PARAMS, "features": FEATURES, "book": f"long top {QUINTILE}, short bottom {QUINTILE} of the prediction, weekly, dollar neutral",
                      "fee_pct": mo.FEE_PCT, "slippage_pct_per_side": [mo.SLIP_PCT, mo.SLIP_STRESS_PCT], "sleeve_usd": mo.SLEEVE_USD},
            "results": results, "rank_ic": {"oos": ic_oos, "locked": ic_locked}, "benchmarks": bench,
            "placebo": {"seeds": PLACEBO_SEEDS, "kind": "same pipeline, labels shuffled inside each day", "oos_apr_pct_p5_p50_p95": [round(float(x), 2) for x in np.percentile(placebo, [5, 50, 95])], "primary_oos_percentile": pct},
            "feature_importance_top": [{"feature": n, "gain_share_pct": round(100.0 * g / total, 1)} for n, g in top], "models_trained": imp["models"],
            "limits": LIMITS, "_sims": {k_: {"pnl": s_["pnl"].tolist(), "rebalances": s_["rebalances"]} for k_, s_ in sims.items() if k_ == p_key}, "_days": days}


LIMITS = ["Daily open-to-open returns; no intraday risk, liquidation or borrow model; a coin that vanishes mid-week is held at its last price.",
          "The universe is ranked from the contracts Bybit's API still knows (delisted ones included); a contract removed from the API altogether is invisible.",
          "2.7 years is one regime (mostly falling alts); the first year is training only, so the walk-forward trades roughly 1.7 years, of which the locked part is for looking.",
          "One fixed parameter set, no search: a search over parameters on this little data would only fit noise. Only the primary configuration counts for the verdict.",
          "Placebo = 20 pipelines with shuffled labels (a coarse estimate; the percentile has steps of 5). A pass is a hypothesis for a small paper test, never `edge` by itself."]


def to_markdown(rep: dict) -> str:
    out = [f"# LightGBM walk-forward study {rep['run_id']}", "", "**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small test, not proof.**", "", f"## Verdict: `{rep['verdict']}`", "",
           ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "", f"Primary: {rep['primary']}. Window {rep['window']}.", "",
           f"Rank IC (score vs realised 7-day relative return): out-of-sample {rep['rank_ic']['oos']}, locked {rep['rank_ic']['locked']}.", "",
           f"Placebo ({rep['placebo']['kind']}, {rep['placebo']['seeds']} seeds): out-of-sample APR p5 / p50 / p95 = {rep['placebo']['oos_apr_pct_p5_p50_p95']}; the model ranks at the {rep['placebo']['primary_oos_percentile']}th percentile.", "",
           f"Models trained: {rep['models_trained']}. Top features by gain: {rep['feature_importance_top']}", "", "## Results", "", "| config | period | days | net % of sleeve | APR % | Sharpe | max DD % | CI95 of APR % | winning weeks % |", "|---|---|---|---|---|---|---|---|---|"]
    for k, r in rep["results"].items():
        for per in ("all", "locked", "oos"):
            s = r[per]
            if s.get("days", 0) < 30:
                continue
            out.append(f"| {k} | {per} | {s['days']} | {s['net_pct_of_sleeve']} | {s['apr_pct']} | {s['sharpe']} | {s['max_dd_pct']} | {s['ci95_daily_apr_pct']} | {s['win_week_pct']} |")
    out += ["", "## Benchmarks", "", "```", json.dumps(rep["benchmarks"], indent=1), "```", "", "## Rules", "", "```", json.dumps(rep["rules"], indent=1), "```", "", "## Pass thresholds (fixed before the run)", "", "```",
            json.dumps(rep["pass_thresholds"], indent=1), "```", "", "## Limits", ""]
    out += [f"- {x}" for x in rep["limits"]]
    return "\n".join(out) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    sims = rep.pop("_sims", {})
    days = rep.pop("_days", [])
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["config", "date", "daily_pnl_pct_of_sleeve", "cumulative_pct"])
    for name, s in sims.items():
        cum = 0.0
        for k, v in enumerate(s["pnl"]):
            cum += v
            if v != 0.0 or k % 7 == 0:
                cw.writerow([name, datetime.fromtimestamp(days[k] / 1000, tz=timezone.utc).date(), round(v * 100, 4), round(cum * 100, 3)])
    (base / "equity.csv").write_text(buf.getvalue(), encoding="utf-8")
