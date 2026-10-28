"""
P26: do the perpetual's PREMIUM features add to the LightGBM v2 ranker? The premium (mark price vs index price, hourly, from Binance's free archive `premiumIndexKlines`) is the market's own crowding
gauge: a perp trading far above its index means longs are paying up, far below means shorts are. Funding already carries a slow version of it.

One pre-registered comparison, nothing else (more variants on the same data would be a multiple-testing trap):
  base      the 39 v2 features, the fixed v2 parameters, hold 3 days, universe 40, quintile 8, same walk-forward / split / costs
  +premium  the same plus 8 features known at 00:00 UTC: mean premium of the last 24 h and 168 h, the change of the 24 h mean against the day before, the last hourly premium, and the daily rank of each
  adopt     only if ALL hold: out-of-sample rank IC higher by at least 0.01, net APR at 0.30% slippage not lower, locked-period IC not lower. Otherwise the v2 features stay.
Needs the Binance cache of the v2 study. Downloads the premium files of the contracts that were ever in the universe (about 15,000 small files, resumable, no key). $0, CPU only.
"""
from __future__ import annotations

import io
import json
import re
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

from . import carry, lgbm_hourly as lh, lgbm_study as lg, listing_fade as lf, momentum as mo, paths

SINCE = lh.SINCE
HOUR = lh.HOUR
PREM_FEATURES = ["prem24", "prem168", "prem_chg", "prem_last"]
FEATURES_P = lh.FEATURES + PREM_FEATURES + [f"rk_{f}" for f in PREM_FEATURES]
URL = lh.VISION + "/data/futures/um/monthly/premiumIndexKlines/{s}/1h/{s}-1h-{m}.zip"
PASS = {"min_ic_gain": 0.01}


def cached_months(sym: str) -> list[str]:
    d = lh.cdir("k", sym)
    return sorted(f.stem for f in d.glob("*.npy") if re.fullmatch(r"\d{4}-\d{2}", f.stem)) if d.is_dir() else []


def universe_symbols(now: datetime, log=lambda s: None) -> list[str]:
    start = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc)
    syms = lh.build(start, now - timedelta(days=1), lh.UNIVERSE_N, log)[1]
    return list(syms)


def fetch_all(get, now: datetime, log=lambda s: None, progress=lambda **k: None, stop: threading.Event | None = None, pace: float = 0.05, sleep=time.sleep, workers: int = 8) -> dict:
    """Premium-index months for every contract that was ever in the top-N universe, in the months the klines cache already has. Cached, resumable."""
    pc = lf.Paced(get, min(pace, 0.03), sleep)
    syms = universe_symbols(now, log)
    jobs = [(s, m) for s in syms for m in cached_months(s)]
    done, errors, lock = [0], [0], threading.Lock()
    log(f"premium index: {len(jobs)} monthly files for {len(syms)} contracts")

    def one(job):
        if stop is not None and stop.is_set():
            return
        s, m = job
        p, none = lh.cdir("p", s, f"{m}.npy"), lh.cdir("p", s, f"{m}.none")
        if not (p.is_file() or none.is_file()):
            for attempt in range(3):            # a reset connection is retried; a file that still fails is counted and picked up by the next run
                try:
                    r = pc(URL.format(s=s, m=m), None, 120)
                    if r.status_code == 404:
                        paths.ensure_parent(none).write_text("", encoding="utf-8")
                    elif r.status_code == 200:
                        rows = lh._csv_rows(r.content)
                        lh._save(p, np.array([[lh._ts_ms(x[0]), float(x[1]), float(x[4])] for x in rows], dtype=np.float64).reshape(-1, 3))
                    else:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    break
                except Exception as e:      # noqa: BLE001
                    if attempt == 2:
                        with lock:
                            errors[0] += 1
                        log(f"{s} {m}: {type(e).__name__}: {e}")
                    else:
                        sleep(1.0 + attempt)
        with lock:
            done[0] += 1
            progress(step=done[0], total=len(jobs), api_calls=pc.calls, symbol_errors=errors[0])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, jobs))
    return {"stopped": bool(stop is not None and stop.is_set()), "jobs": len(jobs), "errors": errors[0], "calls": pc.calls}


def premium_matrix(syms: list[str], d0: int, Hn: int) -> np.ndarray:
    P = np.full((len(syms), Hn), np.nan, dtype=np.float32)
    for j, s in enumerate(syms):
        d = lh.cdir("p", s)
        parts = [np.load(f).reshape(-1, 3) for f in sorted(d.glob("*.npy"))] if d.is_dir() else []
        if not parts:
            continue
        a = np.vstack(parts)
        h = ((a[:, 0] - d0) // HOUR).astype(int)
        ok = (h >= 0) & (h < Hn)
        P[j, h[ok]] = a[ok, 2]            # close of the hourly premium index
    return P


def prem_features(P: np.ndarray, k: int, uni: list[int]) -> np.ndarray:
    """Four premium features and their daily ranks for the universe at day k; only hours before 00:00 of day k are used."""
    h0 = 24 * k
    p = P[np.asarray(uni)].astype(np.float64)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        p24 = np.nanmean(p[:, h0 - 24:h0], axis=1)
        p168 = np.nanmean(p[:, h0 - 168:h0], axis=1)
        pprev = np.nanmean(p[:, h0 - 48:h0 - 24], axis=1)
    base = np.column_stack([p24, p168, p24 - pprev, p[:, h0 - 1]])
    return np.column_stack([base] + [lg.rank01(base[:, j]) for j in range(base.shape[1])])


def verdict(base: dict, plus: dict) -> dict:
    ic_gain = ((plus["ic_oos"] or {}).get("mean_ic") or 0.0) - ((base["ic_oos"] or {}).get("mean_ic") or 0.0)
    conds = {"oos_ic_up_by_0.01": ic_gain >= PASS["min_ic_gain"],
             "apr_at_0.30_not_lower": (plus["slip0.3"]["oos"].get("apr_pct") or -1e9) >= (base["slip0.3"]["oos"].get("apr_pct") or -1e9),
             "locked_ic_not_lower": ((plus["ic_locked"] or {}).get("mean_ic") or 0.0) >= ((base["ic_locked"] or {}).get("mean_ic") or 0.0)}
    failed = [k for k, v in conds.items() if not v]
    return {"verdict": "adopt" if not failed else "keep v2 features", "failed": failed, "conditions": conds, "ic_gain": round(ic_gain, 4)}


def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    lg._lightgbm()
    t0 = time.time()
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    days, syms, M, Od, Td, X, F = lh.build(start, end, lh.UNIVERSE_N, log)
    if len(syms) < 12:
        raise RuntimeError("not enough cached Binance data: run the LightGBM v2 study (or the ranker) first")
    P = premium_matrix(syms, days[0], M["o"].shape[1])
    coverage = float(np.mean(~np.isnan(P))) * 100
    if coverage < 5:
        raise RuntimeError("no premium-index data in the cache: the download step did not run")
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(lh.LOCK_END).replace(tzinfo=timezone.utc)))
    first = k0 + lh.MIN_TRAIN_DAYS
    feats = lh.build_features(M, Od, Td, F, 30, kend, syms, lh.UNIVERSE_N)
    feats_p = {k: (uni, np.column_stack([f, prem_features(P, k, uni)])) for k, (uni, f) in feats.items()}
    del M
    hold, q = lh.HOLD, lh.QUINTILE
    rebal = list(range(first, kend - hold + 1, hold))
    res = {}
    for name, ff, names in (("base", feats, lh.FEATURES), ("+premium", feats_p, FEATURES_P)):
        panel = lh.panel_for(ff, X, hold)
        pred = lg.walk_forward(panel, rebal, first, None, lambda s: None, hold, names, lh.PARAMS, lh.RETRAIN_DAYS, q)
        imp = pred.pop("_importance")
        r = {"ic_oos": lg.rank_ic(pred, panel, lock_k, kend), "ic_locked": lg.rank_ic(pred, panel, first, lock_k), "importance": imp["gain"]}
        for slip in (mo.SLIP_PCT, mo.SLIP_STRESS_PCT):
            sim = lg.simulate_scores(days, Od, Td, X, pred, first, kend, slip, hold, q)
            r[f"slip{slip:g}"] = {"oos": mo.stats(sim["pnl"], lock_k, kend), "locked": mo.stats(sim["pnl"], first, lock_k)}
        res[name] = r
        log(f"{name}: OOS IC {(r['ic_oos'] or {}).get('mean_ic')}, APR at 0.10% {r['slip0.1']['oos'].get('apr_pct')}, at 0.30% {r['slip0.3']['oos'].get('apr_pct')} ({round(time.time() - t0)} s)")
    v = verdict(res["base"], res["+premium"])
    tot = sum(res["+premium"]["importance"].values()) or 1.0
    share = sorted(((n, round(100.0 * g / tot, 2)) for n, g in res["+premium"]["importance"].items() if n in PREM_FEATURES or n.startswith("rk_prem")), key=lambda x: -x[1])
    for r in res.values():
        r.pop("importance", None)
    return {"type": "lgbm_premium_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "conditions": v["conditions"], "ic_gain": v["ic_gain"],
            "pass_thresholds": PASS, "window": {"start": start.date().isoformat(), "end": end.date().isoformat(), "lock_end": lh.LOCK_END}, "setup": {"hold": hold, "universe": lh.UNIVERSE_N, "quintile": q},
            "results": res, "premium_gain_share_pct": share, "coverage_pct": round(coverage, 1), "features_added": PREM_FEATURES,
            "limits": ["One comparison, same data and split as the v2 study: a gain here is a hypothesis, not proof.", "Premium files exist only for complete months of the archive: the newest weeks have no premium (left empty).",
                       "A larger IC can still lose the money to costs: the 0.30% line decides."]}


def to_markdown(rep: dict) -> str:
    L = [f"# Premium-index features vs the v2 feature set {rep['run_id']}", "", f"## Verdict: `{rep['verdict']}`", "", f"Conditions: {rep['conditions']}. Out-of-sample IC change {rep['ic_gain']:+.4f}.", "",
         f"Hold {rep['setup']['hold']} d, universe {rep['setup']['universe']}, quintile {rep['setup']['quintile']}; same walk-forward, parameters, split ({rep['window']['lock_end']}) and costs. Rule: adopt only if the out-of-sample rank IC rises by at least {PASS['min_ic_gain']}, the APR at 0.30% slippage is not lower and the locked-period IC is not lower.", "",
         "| feature set | OOS IC (CI) | locked IC | OOS APR % 0.10% | OOS Sharpe | OOS max DD % | OOS APR % 0.30% | locked APR % |", "|---|---|---|---|---|---|---|---|"]
    for name, r in rep["results"].items():
        o, o3 = r["slip0.1"]["oos"], r["slip0.3"]["oos"]
        L.append(f"| {name} | {(r['ic_oos'] or {}).get('mean_ic')} {(r['ic_oos'] or {}).get('ci95')} | {(r['ic_locked'] or {}).get('mean_ic')} | {o.get('apr_pct')} | {o.get('sharpe')} | {o.get('max_dd_pct')} | {o3.get('apr_pct')} | {r['slip0.1']['locked'].get('apr_pct')} |")
    L += ["", "Premium features' share of the model's gain: " + ", ".join(f"{n} {v}%" for n, v in rep["premium_gain_share_pct"]), "", f"Premium coverage: {rep['coverage_pct']}% of the (symbol, hour) cells in the window.", ""] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
