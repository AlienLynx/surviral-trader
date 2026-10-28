"""
P25-C: cross-sectional momentum on liquid USDT perpetuals -- the one directional family with public evidence behind it (Borri et al. 2026: two-week momentum long-short, CTREND), tested the same
pre-registered way as everything else. Rule based, no LLM, $0, nothing is traded. It reuses the carry study's cached Bybit data (daily perp bars incl. delisted contracts, funding), so it adds no download.

Rules (PRIMARY fixed before the run; the other lookbacks and the long-only book are shown for context only and cannot rescue a failed primary):
  universe   the top N=20 USDT perpetuals by trailing 30-day turnover AS OF each rebalance day (delisted ones included while they traded)
  signal     return over the last LOOKBACK days (open to open, both known at 00:00 of the rebalance day)
  book       every REBALANCE_DAYS days: long the top quintile, short the bottom quintile, equal weights, half of the sleeve per leg (dollar neutral); the long-only variant puts the whole sleeve in the top quintile
  P&L        daily open-to-open returns of the held coins minus funding (a long pays positive funding, a short receives it) minus fees and slippage on every traded notional
  costs      taker fee 0.055% + slippage 0.10% per side (0.30% in the stress scenario)
  placebo    the same book with randomly chosen coins (same size, same turnover costs), 300 seeds: where does the strategy's out-of-sample return rank among them?
Verdict thresholds are in PASS. Known limits (in every report): daily bars, no intraday risk or liquidation model, no borrow limits, the universe ranking uses today's list of known contracts (a contract Bybit
removed from its API altogether is invisible), 2.7 years is one regime (a mostly falling alt market).
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.engine import evidence as ev

from . import carry, listing_fade as lf, paths

DAY = carry.DAY_MS
SINCE = "2024-01-01"
LOCK_END = carry.LOCK_END
UNIVERSE_N = 20
QUINTILE = 4
REBALANCE_DAYS = 7
SLEEVE_USD = 3000.0
FEE_PCT = 0.055
SLIP_PCT = 0.10
SLIP_STRESS_PCT = 0.30
PRIMARY = {"lookback_days": 14, "mode": "long_short", "slip_pct": SLIP_PCT}
LOOKBACKS = (7, 14, 28)
MODES = ("long_short", "long_only")
PLACEBO_SEEDS = 300
PASS = {"min_oos_net_apr_pct": 10.0, "max_dd_pct_of_sleeve": 20.0, "min_placebo_percentile": 95.0, "min_oos_rebalances": 40}


# ------------------------------------------------------------------ matrices from the carry cache
def build(data: dict, start: datetime, end: datetime):
    """days (ms), symbols, O opens [S, D+1] (nan when missing), T turnover [S, D+1], X daily 'return minus funding' [S, D] (nan when the symbol has no bar on both days)."""
    d0, d1 = carry.day_start(start) - 40 * DAY, carry.day_start(end)
    days = list(range(d0, d1 + DAY + 1, DAY))
    syms = sorted(data)
    S, D = len(syms), len(days)
    O = np.full((S, D), np.nan)
    T = np.full((S, D), np.nan)
    F = np.zeros((S, D))
    for i, sym in enumerate(syms):
        s = data[sym]
        for k, d in enumerate(days):
            p = s.p_open.get(d)
            if p:
                O[i, k] = p
            t = s.turn.get(d)
            if t is not None:
                T[i, k] = t
        for ts, r in zip(s.f_ts, s.f_rate):
            k = (ts - d0) // DAY
            if 0 <= k < D:
                F[i, k] += r
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = O[:, 1:] / O[:, :-1] - 1.0
    X = ret - F[:, :-1]
    return days, syms, O, T, X


def universe_at(T: np.ndarray, O: np.ndarray, k: int, n: int = UNIVERSE_N) -> list[int]:
    win = T[:, k - 30:k]
    cnt = np.sum(~np.isnan(win), axis=1)
    mean = np.where(cnt >= 20, np.nansum(win, axis=1) / np.maximum(cnt, 1), -1.0)
    ok = ~np.isnan(O[:, k]) & (mean > 0)
    idx = [i for i in np.argsort(-mean) if ok[i]][:n]
    return idx


def weights_for(O: np.ndarray, T: np.ndarray, k: int, lookback: int, mode: str, rng=None) -> dict[int, float]:
    uni = universe_at(T, O, k)
    if len(uni) < 10:
        return {}
    if rng is None:
        sig = {i: O[i, k] / O[i, k - lookback] - 1.0 for i in uni if not np.isnan(O[i, k - lookback])}
        if len(sig) < 10:
            return {}
        order = sorted(sig, key=lambda i: -sig[i])
        longs, shorts = order[:QUINTILE], order[-QUINTILE:]
    else:
        pick = list(rng.permutation(uni))
        longs, shorts = pick[:QUINTILE], pick[QUINTILE:2 * QUINTILE]
    if mode == "long_short":
        return {**{i: 0.5 / QUINTILE for i in longs}, **{i: -0.5 / QUINTILE for i in shorts}}
    return {i: 1.0 / QUINTILE for i in longs}


def simulate(days, O, T, X, start_k: int, end_k: int, lookback: int, mode: str, slip_pct: float, rng=None) -> dict:
    """Weekly book from day index start_k to end_k. Returns the daily P&L as a fraction of the sleeve (index = day index) and the rebalance list."""
    cost_side = (FEE_PCT + slip_pct) / 100.0
    pnl = np.zeros(len(days) - 1)
    reb, prev = [], {}
    k = start_k
    while k + REBALANCE_DAYS <= end_k:
        w = weights_for(O, T, k, lookback, mode, rng)
        traded = sum(abs(w.get(i, 0.0) - prev.get(i, 0.0)) for i in set(w) | set(prev))
        pnl[k] -= traded * cost_side
        reb.append({"k": k, "n_pos": len(w), "turnover": round(traded, 3)})
        for i, wi in w.items():
            seg = X[i, k:k + REBALANCE_DAYS]
            pnl[k:k + REBALANCE_DAYS] += wi * np.nan_to_num(seg, nan=0.0)
        prev = w
        k += REBALANCE_DAYS
    return {"pnl": pnl, "rebalances": reb}


# ------------------------------------------------------------------ statistics
def block_ci(x: np.ndarray, block: int = 7, draws: int = 2000, seed: int = 5):
    n = len(x)
    if n < 2 * block:
        return None
    rng = np.random.default_rng(seed)
    nb = n // block
    blocks = x[:nb * block].reshape(nb, block).mean(axis=1)
    means = [float(rng.choice(blocks, size=nb, replace=True).mean()) for _ in range(draws)]
    lo, hi = np.percentile(means, [2.5, 97.5])
    return float(lo), float(hi)


def stats(pnl: np.ndarray, lo: int, hi: int) -> dict:
    x = pnl[lo:hi]
    n = len(x)
    if n < 30:
        return {"days": n}
    mean, sd = float(x.mean()), float(x.std())
    cum = np.cumsum(x)
    dd = float(np.max(np.maximum.accumulate(cum) - cum))
    ci = block_ci(x)
    return {"days": n, "net_pct_of_sleeve": round(float(cum[-1]) * 100, 2), "apr_pct": round(mean * 365 * 100, 2), "sharpe": round(mean / sd * np.sqrt(365), 2) if sd > 0 else None,
            "max_dd_pct": round(dd * 100, 2), "ci95_daily_apr_pct": None if ci is None else [round(ci[0] * 365 * 100, 2), round(ci[1] * 365 * 100, 2)], "win_week_pct": round(float(np.mean([x[i:i + 7].sum() > 0 for i in range(0, n - 6, 7)])) * 100, 1)}


def verdict(oos: dict, locked: dict, percentile: float | None, rebalances_oos: int) -> dict:
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
        failed.append(f"percentile vs random-coin placebo {percentile} < {PASS['min_placebo_percentile']}")
    if not failed:
        return {"verdict": "edge candidate", "failed": []}
    return {"verdict": "no edge" if (ci is not None and ci[1] < 0) else "unproven", "failed": failed}


# ------------------------------------------------------------------ study
def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    data = carry.load_data()
    if len(data) < 12:
        raise RuntimeError("not enough cached perpetual data: the carry fetch has to run first")
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    days, syms, O, T, X = build(data, start, end)
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(LOCK_END).replace(tzinfo=timezone.utc)))
    results, sims = {}, {}
    for lb in LOOKBACKS:
        for mode in MODES:
            for slip in (SLIP_PCT, SLIP_STRESS_PCT):
                log(f"simulating lookback {lb} d, {mode}, slippage {slip}%")
                sim = simulate(days, O, T, X, k0, kend, lb, mode, slip)
                key = f"L{lb}_{mode}_slip{slip}"
                sims[key] = sim
                results[key] = {"all": stats(sim["pnl"], k0, kend), "locked": stats(sim["pnl"], k0, lock_k), "oos": stats(sim["pnl"], lock_k, kend), "rebalances": len(sim["rebalances"]),
                                "rebalances_oos": sum(1 for r in sim["rebalances"] if r["k"] >= lock_k), "avg_turnover": round(float(np.mean([r["turnover"] for r in sim["rebalances"]])), 3) if sim["rebalances"] else None}
    p_key = f"L{PRIMARY['lookback_days']}_{PRIMARY['mode']}_slip{PRIMARY['slip_pct']}"
    # benchmarks (open to open, same period): BTC buy and hold, equal-weight universe long
    ib = syms.index("BTCUSDT") if "BTCUSDT" in syms else None
    bench = {}
    if ib is not None:
        btc = np.nan_to_num(X[ib], nan=0.0)
        bench["btc_hold"] = {"all": stats(btc, k0, kend), "oos": stats(btc, lock_k, kend)}
    ew = np.zeros(len(days) - 1)
    k = k0
    while k + REBALANCE_DAYS <= kend:
        uni = universe_at(T, O, k)
        for i in uni:
            ew[k:k + REBALANCE_DAYS] += np.nan_to_num(X[i, k:k + REBALANCE_DAYS], nan=0.0) / max(1, len(uni))
        k += REBALANCE_DAYS
    bench["equal_weight_universe"] = {"all": stats(ew, k0, kend), "oos": stats(ew, lock_k, kend)}
    # placebo for the primary: random coins, same size and costs
    log(f"placebo: {PLACEBO_SEEDS} random books")
    oos_aprs = []
    for seed in range(PLACEBO_SEEDS):
        rng = np.random.default_rng(1000 + seed)
        sm = simulate(days, O, T, X, k0, kend, PRIMARY["lookback_days"], PRIMARY["mode"], PRIMARY["slip_pct"], rng=rng)
        oos_aprs.append(float(sm["pnl"][lock_k:kend].mean()) * 365 * 100)
    prim = results[p_key]
    pct = round(100.0 * float(np.mean([prim["oos"].get("apr_pct", -1e9) > a for a in oos_aprs])), 1)
    v = verdict(prim["oos"], prim["locked"], pct, prim["rebalances_oos"])
    top = universe_at(T, O, kend)
    return {"type": "momentum_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": p_key, "pass_thresholds": PASS,
            "window": {"start": start.date().isoformat(), "end": end.date().isoformat(), "lock_end": LOCK_END}, "universe": {"symbols_in_cache": len(syms), "universe_n": UNIVERSE_N, "now": [syms[i] for i in top]},
            "rules": {"signal": f"return over {PRIMARY['lookback_days']} days", "book": f"long top {QUINTILE}, short bottom {QUINTILE} of the top {UNIVERSE_N} by turnover, weekly, dollar neutral, half the sleeve per leg", "fee_pct": FEE_PCT,
                      "slippage_pct_per_side": [SLIP_PCT, SLIP_STRESS_PCT], "sleeve_usd": SLEEVE_USD},
            "results": results, "benchmarks": bench, "placebo": {"seeds": PLACEBO_SEEDS, "oos_apr_pct_p5_p50_p95": [round(float(x), 2) for x in np.percentile(oos_aprs, [5, 50, 95])], "primary_oos_percentile": pct},
            "limits": LIMITS, "_sims": {k_: {"pnl": s_["pnl"].tolist(), "rebalances": s_["rebalances"]} for k_, s_ in sims.items() if k_ == p_key}, "_days": days}


LIMITS = ["Daily open-to-open returns; no intraday risk, liquidation or borrow model; a coin that vanishes mid-week is held at its last price.",
          "The universe is ranked from the contracts Bybit's API still knows (delisted ones included); a contract removed from the API altogether is invisible.",
          "2.7 years is one regime (mostly falling alts). A pass is a hypothesis for a small paper / real-money test, never `edge` by itself.",
          "Only the primary configuration counts for the verdict; the grid of lookbacks and modes is shown for context (a grid of six is a multiple-testing trap)."]


def to_markdown(rep: dict) -> str:
    out = [f"# Momentum study {rep['run_id']}", "", "**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small test, not proof.**", "", f"## Verdict: `{rep['verdict']}`", "",
           ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "", f"Primary: {rep['primary']}. Window {rep['window']}. Universe now: {rep['universe']['now']}", "",
           f"Placebo (random coins, same size and costs, {rep['placebo']['seeds']} seeds): out-of-sample APR p5 / p50 / p95 = {rep['placebo']['oos_apr_pct_p5_p50_p95']}; the primary ranks at the {rep['placebo']['primary_oos_percentile']}th percentile.", "",
           "## Results", "", "| config | period | days | net % of sleeve | APR % | Sharpe | max DD % | CI95 of APR % | winning weeks % |", "|---|---|---|---|---|---|---|---|---|"]
    for k, r in rep["results"].items():
        for per in ("all", "locked", "oos"):
            s = r[per]
            if s.get("days", 0) < 30:
                continue
            out.append(f"| {k} | {per} | {s['days']} | {s['net_pct_of_sleeve']} | {s['apr_pct']} | {s['sharpe']} | {s['max_dd_pct']} | {s['ci95_daily_apr_pct']} | {s['win_week_pct']} |")
    out += ["", "## Benchmarks", "", "```", json.dumps(rep["benchmarks"], indent=1), "```", "", "## Rules", "", "```", json.dumps(rep["rules"], indent=1), "```", "", "## Pass thresholds (fixed before the run)", "", "```", json.dumps(rep["pass_thresholds"], indent=1),
            "```", "", "## Limits", ""]
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
