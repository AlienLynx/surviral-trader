"""
P26: ranker robustness grid -- is the LightGBM v2 lead one lucky setting, or a neighbourhood? Re-runs the SAME walk-forward (same features, same fixed model parameters, same costs, same locked /
out-of-sample split as lgbm_hourly) for neighbouring settings: hold in {2, 3, 5, 7} days x universe in {20, 40, 60} coins (quintile = universe / 5, at least 4). Nothing is tuned: the primary stays hold 3 /
universe 40 and this grid only shows whether the result survives a change of setting. $0, CPU only, no network: it reads the Binance cache of the v2 study.

Verdict, fixed in advance and computed by code (of the cells, N = number of settings):
  neighbourhood holds   rank IC positive in at least 10 of 12 cells AND net APR positive at 0.10% slippage in at least 10 of 12
  fragile               rank IC positive in fewer than 8 of 12 cells
  mixed                 anything else
It says nothing about whether the money survives costs: that is why the 0.30% line and the drawdown are in the table. The primary being the best cell is reported as a warning (selection optimism).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from . import carry, lgbm_hourly as lh, lgbm_study as lg, momentum as mo, paths

SINCE = lh.SINCE
HOLDS = (2, 3, 5, 7)
UNIVERSES = (20, 40, 60)
PRIMARY = (40, 3)
PASS = {"holds_min_cells": 10, "fragile_below_ic_cells": 8}


def fetch_all(get, now, log=lambda s: None, progress=lambda **k: None, stop=None, pace=0.05, sleep=time.sleep) -> dict:
    """Nothing to download: the grid reads the cache of the LightGBM v2 study."""
    return {"stopped": False, "note": "reads the Binance cache of the LightGBM v2 study"}


def verdict(rows: list[dict]) -> dict:
    n = len(rows)
    ic_pos = sum(1 for r in rows if ((r["ic_oos"] or {}).get("mean_ic") or -1) > 0)
    apr_pos = sum(1 for r in rows if (r["slip0.1"]["oos"].get("apr_pct") or -1) > 0)
    if n == 0:
        return {"verdict": "insufficient n", "failed": ["no cells"]}
    need = min(PASS["holds_min_cells"], n)
    if ic_pos >= need and apr_pos >= need:
        return {"verdict": "neighbourhood holds", "failed": []}
    if ic_pos < min(PASS["fragile_below_ic_cells"], n):
        return {"verdict": "fragile", "failed": [f"rank IC positive in only {ic_pos} of {n} cells"]}
    return {"verdict": "mixed", "failed": [f"rank IC positive in {ic_pos} of {n}, net APR positive in {apr_pos} of {n} at 0.10% slippage (needs {need} of each)"]}


def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    lg._lightgbm()
    t0 = time.time()
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    days, syms, M, Od, Td, X, F = lh.build(start, end, max(UNIVERSES), log)
    if len(syms) < 12:
        raise RuntimeError("not enough cached Binance data: run the LightGBM v2 study (or the ranker) first")
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(lh.LOCK_END).replace(tzinfo=timezone.utc)))
    first = k0 + lh.MIN_TRAIN_DAYS
    if kend - first < 300:
        raise RuntimeError(f"not enough history for a walk-forward: {kend - k0} days")
    rows = []
    for n in UNIVERSES:
        q = max(4, n // 5)
        feats = lh.build_features(M, Od, Td, F, 30, kend, syms, n)
        log(f"universe {n}: features built ({round(time.time() - t0)} s)")
        for hold in HOLDS:
            panel = lh.panel_for(feats, X, hold)
            rebal = list(range(first, kend - hold + 1, hold))
            pred = lg.walk_forward(panel, rebal, first, None, lambda s: None, hold, lh.FEATURES, lh.PARAMS, lh.RETRAIN_DAYS, q)
            pred.pop("_importance", None)
            res = {"universe": n, "hold": hold, "quintile": q, "primary": (n, hold) == PRIMARY}
            for slip in (mo.SLIP_PCT, mo.SLIP_STRESS_PCT):
                sim = lg.simulate_scores(days, Od, Td, X, pred, first, kend, slip, hold, q)
                res[f"slip{slip:g}"] = {"locked": mo.stats(sim["pnl"], first, lock_k), "oos": mo.stats(sim["pnl"], lock_k, kend),
                                        "turnover": round(float(np.mean([r["turnover"] for r in sim["rebalances"]])), 2) if sim["rebalances"] else None}
            res["ic_oos"] = lg.rank_ic(pred, panel, lock_k, kend)
            rows.append(res)
            o = res["slip0.1"]["oos"]
            log(f"universe {n} hold {hold} d: OOS APR {o.get('apr_pct')}% IC {(res['ic_oos'] or {}).get('mean_ic')} ({round(time.time() - t0)} s)")
    v = verdict(rows)
    best = max(rows, key=lambda r: r["slip0.1"]["oos"].get("sharpe") or -9)
    warn = []
    if best["primary"]:
        warn.append("the primary (hold 3 / universe 40) is also the best cell by out-of-sample Sharpe: expect forward results below the backtest")
    return {"type": "lgbm_grid_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "pass_thresholds": PASS,
            "window": {"start": start.date().isoformat(), "end": end.date().isoformat(), "lock_end": lh.LOCK_END}, "primary": {"universe": PRIMARY[0], "hold": PRIMARY[1]}, "cells": rows,
            "counts": {"cells": len(rows), "ic_positive": sum(1 for r in rows if ((r["ic_oos"] or {}).get("mean_ic") or -1) > 0), "apr_positive_0.10": sum(1 for r in rows if (r["slip0.1"]["oos"].get("apr_pct") or -1) > 0),
                       "apr_positive_0.30": sum(1 for r in rows if (r["slip0.3"]["oos"].get("apr_pct") or -1) > 0)}, "warnings": warn,
            "limits": ["Same data, same split and same fixed model as the v2 study: the cells are neighbours, not independent tests.", "Max drawdown is large in every cell: a robust IC does not make a tradeable book.",
                       "Costs are the usual killer: read the 0.30% column."]}


def to_markdown(rep: dict) -> str:
    c = rep["counts"]
    L = [f"# Ranker robustness grid {rep['run_id']}", "", f"## Verdict: `{rep['verdict']}`", "", ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "",
         f"Same walk-forward, features, fixed model and costs as the LightGBM v2 study; only hold and universe change. Primary = hold {rep['primary']['hold']} / universe {rep['primary']['universe']}. Out-of-sample from {rep['window']['lock_end']}.", "",
         f"Rank IC positive in **{c['ic_positive']} of {c['cells']}** cells; net APR positive in **{c['apr_positive_0.10']} of {c['cells']}** at 0.10% slippage and **{c['apr_positive_0.30']} of {c['cells']}** at 0.30%.", "",
         "| universe | hold d | quintile | OOS APR % (0.10%) | OOS Sharpe | OOS max DD % | locked APR % | OOS APR % (0.30%) | turnover | OOS rank IC |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["cells"]:
        o, l, s = r["slip0.1"]["oos"], r["slip0.1"]["locked"], r["slip0.3"]["oos"]
        L.append(f"| {r['universe']}{' (primary)' if r['primary'] else ''} | {r['hold']} | {r['quintile']} | {o.get('apr_pct')} | {o.get('sharpe')} | {o.get('max_dd_pct')} | {l.get('apr_pct')} | {s.get('apr_pct')} | {r['slip0.1']['turnover']} | {(r['ic_oos'] or {}).get('mean_ic')} |")
    L += [""] + [f"- WARNING: {w}" for w in rep["warnings"]] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
