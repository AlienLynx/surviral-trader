"""
P26: maker-fill cost model -- can passive (limit-order) execution rescue the costs that decide every result here?

Every strategy in this project is judged after taker costs (fee 0.055% + slippage 0.10% per side, 0.30% as stress). A limit order pays a lower fee (maker 0.02%) and no slippage, and can even
earn a little (it fills at a better price), BUT it is not always filled, and the orders that do NOT fill are exactly the ones where the price ran away from you (adverse selection). This study
prices both effects from real hourly / 5-minute price paths, in two parts. $0, CPU only, no network: it reads the caches of the LightGBM v2 study and of the Listing Fade study.

PART 1 -- the LightGBM ranker book (hold 3 d, universe 40, long / short 8 each, the v2 walk-forward). At every rebalance each changed position is traded with a limit order placed `delta` better
than the 00:00 open (a buy at open x (1 - delta), a sell at open x (1 + delta)). It fills if the price trades through it within `timeout_h` hours (the hourly low / high). Not filled:
  skip   the trade is not done (the old weight stays) -- the missed winners show up as lost P&L
  chase  after the timeout the trade is done as a taker at the next hour's open: the price move since the open is paid, plus taker fee and slippage
  taker  baseline: everything at the open with taker fee + slippage (this reproduces the v2 study's book exactly)
Filled: maker fee 0.02% is paid and the price improvement `delta` is earned. Daily marks, funding and everything else are the v2 study's.
PRIMARY (fixed in advance, nothing is tuned): delta 0.10%, timeout 2 h, chase. The rest of the grid (delta 0.05 / 0.10 / 0.20%, timeout 1 / 2 / 4 h, skip / chase) is context.
Verdict `passive pays` only if ALL hold: (1) the primary's out-of-sample net APR is at least that of the taker book at 0.10% slippage (passive is no worse than the optimistic taker assumption),
(2) the skip-mode APR with the same delta and timeout is positive (the book survives missing trades), (3) at least 50% of the orders fill. Otherwise `no better`.

PART 2 -- Listing Fade entries (the live bot already enters with a limit order: does the dry-run's instant fill hold?). For the trades of the live-rules replay (re-entry like the live bot, 3 slots,
0.30% slippage) a short is entered with a limit order `delta` above the first candle's open; it fills if a 5-minute candle within `timeout_min` reaches it. The report shows the fill rate, the
taker-replay P&L of the trades that would NOT have filled against those that would (adverse selection: if the unfilled trades are the better ones, passive entry hurts), and the total with skip /
chase. A diagnostic, no verdict: slot interactions of skipped trades are not re-simulated.

Known limits (in every report): no order-book data, so queue position is not modelled (delta 0 is an optimistic bound); the fill rule is a touch of the hourly / 5-minute high or low, with no
knowledge of the order inside the bar; the maker fee is the public tier 0.02%; a chase pays the price move since the open but the position is not marked for the hours before it is filled.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import carry, lgbm_hourly as lh, lgbm_study as lg, listing_fade as lf, momentum as mo, paths

SINCE = lh.SINCE
FEE_MAKER = 0.020
FEE_TAKER = mo.FEE_PCT
SLIP = mo.SLIP_PCT
SLIP_STRESS = mo.SLIP_STRESS_PCT
PRIMARY = {"delta": 0.0010, "timeout_h": 2, "mode": "chase"}
DELTAS = (0.0005, 0.0010, 0.0020)
TIMEOUTS = (1, 2, 4)
MODES = ("skip", "chase")
MIN_FILL_RATE = 0.50
FADE_DELTAS = (0.0, 0.0010, 0.0020)
FADE_TIMEOUT_MIN = (15, 60)


def fetch_all(get, now, log=lambda s: None, progress=lambda **k: None, stop=None, pace=0.05, sleep=time.sleep) -> dict:
    """Nothing to download: this study reads the caches of the LightGBM v2 study and of the Listing Fade study."""
    return {"stopped": False, "note": "reads existing caches"}


# ------------------------------------------------------------------ part 1: the ranker book with limit orders
def fill_decision(buy: bool, ref: float, hi: float, lo: float, delta: float) -> bool:
    """A limit order `delta` better than the reference price fills if the price traded through it in the window: a buy needs the low at or below it, a sell the high at or above it."""
    if not (np.isfinite(ref) and np.isfinite(hi) and np.isfinite(lo)):
        return False
    return lo <= ref * (1.0 - delta) if buy else hi >= ref * (1.0 + delta)


def simulate_passive(days, M, X, scores: dict, k0: int, kend: int, hold: int, q: int, mode: str, delta: float, timeout_h: int, slip_pct: float = SLIP,
                     fee_maker_pct: float = FEE_MAKER, fee_taker_pct: float = FEE_TAKER) -> dict:
    """The weekly-style book of lg.simulate_scores with the execution model above. mode: taker | skip | chase. Returns {pnl, orders, filled, chased, skipped}."""
    fm, ft, sl = fee_maker_pct / 100.0, fee_taker_pct / 100.0, slip_pct / 100.0
    pnl = np.zeros(len(days) - 1)
    prev: dict[int, float] = {}
    st = {"orders": 0, "filled": 0, "chased": 0, "skipped": 0}
    Hn = M["o"].shape[1]
    k = k0
    while k + hold <= kend:
        sc = scores.get(k)
        target: dict[int, float] = {}
        if sc and len(sc) >= 2 * q:
            order = sorted(sc, key=lambda i: sc[i])
            target = {**{i: 0.5 / q for i in order[-q:]}, **{i: -0.5 / q for i in order[:q]}}
        cur: dict[int, float] = {}
        one_off = 0.0
        h0 = 24 * k
        for i in set(prev) | set(target):
            d = target.get(i, 0.0) - prev.get(i, 0.0)
            if abs(d) < 1e-12:
                cur[i] = prev.get(i, 0.0)
                continue
            amt = abs(d)
            st["orders"] += 1
            if mode == "taker":
                one_off -= amt * (ft + sl)
                cur[i] = target.get(i, 0.0)
                continue
            ref = float(M["o"][i, h0])
            if not np.isfinite(ref):
                cur[i] = target.get(i, 0.0)           # no price that day: nothing to model
                continue
            buy = d > 0
            h1 = min(Hn, h0 + timeout_h)
            with np.errstate(all="ignore"):
                hi = float(np.nanmax(M["hi"][i, h0:h1])) if h1 > h0 and np.any(~np.isnan(M["hi"][i, h0:h1])) else float("nan")
                lo = float(np.nanmin(M["lo"][i, h0:h1])) if h1 > h0 and np.any(~np.isnan(M["lo"][i, h0:h1])) else float("nan")
            if fill_decision(buy, ref, hi, lo, delta):
                st["filled"] += 1
                one_off += amt * (delta - fm)
                cur[i] = target.get(i, 0.0)
            elif mode == "chase":
                nxt = float(M["o"][i, h1]) if h1 < Hn else float("nan")
                move = (nxt / ref - 1.0) if np.isfinite(nxt) else 0.0
                one_off -= amt * ((move if buy else -move) + ft + sl)
                st["chased"] += 1
                cur[i] = target.get(i, 0.0)
            else:
                st["skipped"] += 1
                cur[i] = prev.get(i, 0.0)
        pnl[k] += one_off
        for i, w in cur.items():
            if w:
                pnl[k:k + hold] += w * np.nan_to_num(X[i, k:k + hold], nan=0.0)
        prev = {i: w for i, w in cur.items() if w}
        k += hold
    return {"pnl": pnl, **st}


def ranker_verdict(primary: dict, skip: dict, taker01: dict) -> dict:
    fails = []
    if (primary["oos"].get("apr_pct") or -1e9) < (taker01["oos"].get("apr_pct") or -1e9):
        fails.append(f"passive (chase) out-of-sample APR {primary['oos'].get('apr_pct')}% is below the taker book at 0.10% slippage ({taker01['oos'].get('apr_pct')}%)")
    if (skip["oos"].get("apr_pct") or -1e9) <= 0:
        fails.append(f"with the unfilled trades skipped the out-of-sample APR is {skip['oos'].get('apr_pct')}% (not positive)")
    if primary["fill_rate"] < MIN_FILL_RATE:
        fails.append(f"only {primary['fill_rate'] * 100:.0f}% of the orders fill (< {MIN_FILL_RATE * 100:.0f}%)")
    return {"verdict": "passive pays" if not fails else "no better", "failed": fails}


def ranker_part(now: datetime, log=lambda s: None) -> dict:
    lg._lightgbm()
    start = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc)
    days, syms, M, Od, Td, X, F = lh.build(start, now - timedelta(days=1), lh.UNIVERSE_N, log)
    if len(syms) < 12:
        raise RuntimeError("not enough cached Binance data: run the LightGBM v2 study (or the ranker) first")
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(lh.LOCK_END).replace(tzinfo=timezone.utc)))
    first = k0 + lh.MIN_TRAIN_DAYS
    hold, q = lh.HOLD, lh.QUINTILE
    feats = lh.build_features(M, Od, Td, F, 30, kend, syms, lh.UNIVERSE_N)
    panel = lh.panel_for(feats, X, hold)
    rebal = list(range(first, kend - hold + 1, hold))
    pred = lg.walk_forward(panel, rebal, first, None, lambda s: None, hold, lh.FEATURES, lh.PARAMS, lh.RETRAIN_DAYS, q)
    pred.pop("_importance", None)
    log("walk-forward done; pricing the executions")

    def pack(sim):
        return {"oos": mo.stats(sim["pnl"], lock_k, kend), "locked": mo.stats(sim["pnl"], first, lock_k), "orders": sim["orders"], "filled": sim.get("filled", 0), "chased": sim.get("chased", 0),
                "skipped": sim.get("skipped", 0), "fill_rate": round(sim["filled"] / sim["orders"], 3) if sim["orders"] else 1.0}
    base = {}
    for slip in (SLIP, SLIP_STRESS):
        sim = simulate_passive(days, M, X, pred, first, kend, hold, q, "taker", 0.0, 0, slip)
        base[f"taker_slip{slip:g}"] = {**pack(sim), "fill_rate": 1.0}
    grid = []
    for mode in MODES:
        for delta in DELTAS:
            for to in TIMEOUTS:
                sim = simulate_passive(days, M, X, pred, first, kend, hold, q, mode, delta, to)
                grid.append({"mode": mode, "delta": delta, "timeout_h": to, **pack(sim)})
    prim = next(g for g in grid if g["mode"] == PRIMARY["mode"] and g["delta"] == PRIMARY["delta"] and g["timeout_h"] == PRIMARY["timeout_h"])
    skip = next(g for g in grid if g["mode"] == "skip" and g["delta"] == PRIMARY["delta"] and g["timeout_h"] == PRIMARY["timeout_h"])
    v = ranker_verdict(prim, skip, base[f"taker_slip{SLIP:g}"])
    return {"verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": PRIMARY, "baseline": base, "grid": grid, "window": {"start": start.date().isoformat(), "end": (now - timedelta(days=1)).date().isoformat(), "lock_end": lh.LOCK_END}}


# ------------------------------------------------------------------ part 2: Listing Fade entries with limit orders
def _fade_trade(rules, ev_funding, m5, entry_time, fee, slip, mode, delta, timeout_min):
    """(taker trade, passive outcome) for one signal. passive = {'filled': bool, 'trade': dict|None}."""
    taker = lf.simulate_trade(rules, entry_time, m5, ev_funding, fee, slip, rules["stake"])
    if taker is None or "skipped" in taker:
        return None, None
    t = m5["date"].to_numpy(dtype="datetime64[ns]")
    s = int(np.searchsorted(t, np.datetime64(entry_time.replace(tzinfo=None), "ns")))
    o, h = m5["open"].to_numpy(float), m5["high"].to_numpy(float)
    n = max(1, timeout_min // 5)
    ref = float(o[s])
    limit = ref * (1.0 + delta)
    j = next((x for x in range(s, min(len(m5), s + n)) if h[x] >= limit), None)
    if j is not None:
        px = max(limit, float(o[j]))                                  # a candle that opens above the limit fills at its open
        tr = lf.simulate_trade(rules, m5["date"].iloc[j].to_pydatetime(), m5, ev_funding, fee, slip, rules["stake"], entry_mult=px / float(o[j]), entry_slip_pct=0.0)
        if tr is None or "skipped" in tr:
            return taker, {"filled": True, "trade": None}
        tr["profit_ratio"] += (fee - FEE_MAKER) / 100.0                # the entry leg pays the maker fee instead of the taker fee
        tr["profit_abs"] = rules["stake"] * tr["profit_ratio"]
        return taker, {"filled": True, "trade": tr}
    if mode == "chase" and s + n < len(m5):
        tr = lf.simulate_trade(rules, m5["date"].iloc[s + n].to_pydatetime(), m5, ev_funding, fee, slip, rules["stake"])
        return taker, {"filled": False, "trade": None if (tr is None or "skipped" in tr) else tr}
    return taker, {"filled": False, "trade": None}


def fade_part(rules: dict, events: list[dict], m5_cache: dict, until: datetime, fee: float = lf.FEE_TAKER_PCT, log=lambda s: None) -> dict:
    base = lf.run_scenario(rules, events, 0.30, rules["max_open_trades"], True, fee, until, m5_cache, {}, reentry=True)
    funding = {e["inst"]["symbol"]: e["funding"] for e in events}
    out = []
    for delta in FADE_DELTAS:
        for tmo in FADE_TIMEOUT_MIN:
            filled, unfilled, skip_net, chase_net = [], [], 0.0, 0.0
            for b in base:
                if b["open_at_end"]:
                    continue
                sym = b["symbol"]
                taker, pas = _fade_trade(rules, funding.get(sym, []), m5_cache[sym], b["open_date"], fee, 0.30, "chase", delta, tmo)
                if taker is None:
                    continue
                if pas["filled"]:
                    filled.append(taker["profit_abs"])
                    pnl = pas["trade"]["profit_abs"] if pas["trade"] else taker["profit_abs"]
                    skip_net += pnl
                    chase_net += pnl
                else:
                    unfilled.append(taker["profit_abs"])
                    chase_net += pas["trade"]["profit_abs"] if pas["trade"] else 0.0
            n = len(filled) + len(unfilled)
            out.append({"delta": delta, "timeout_min": tmo, "trades": n, "fill_rate": round(len(filled) / n, 3) if n else None,
                        "taker_mean_usd_filled": round(float(np.mean(filled)), 3) if filled else None, "taker_mean_usd_unfilled": round(float(np.mean(unfilled)), 3) if unfilled else None,
                        "net_usd_taker_all": round(sum(filled) + sum(unfilled), 2), "net_usd_skip": round(skip_net, 2), "net_usd_chase": round(chase_net, 2)})
            log(f"fade delta {delta * 100:.2f}% timeout {tmo} min: fill rate {out[-1]['fill_rate']}")
    return {"rows": out, "trades_replayed": len([b for b in base if not b["open_at_end"]]), "basis": "live rules, re-entry, 3 slots, 0.30% taker slippage on the exit and on the taker baseline"}


# ------------------------------------------------------------------ study
def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    now = until or datetime.now(timezone.utc)
    rep: dict = {"type": "maker_study", "run_id": run_id, "generated_at": now.isoformat(), "maker_fee_pct": FEE_MAKER, "taker_fee_pct": FEE_TAKER, "slippage_pct": [SLIP, SLIP_STRESS]}
    errors = []
    try:
        rep["ranker"] = ranker_part(now, log)
    except Exception as e:      # noqa: BLE001
        rep["ranker"] = None
        errors.append(f"ranker part not run: {type(e).__name__}: {e}")
    try:
        rules = lf.load_rules(None)
        events, uni = lf.load_universe(rules, now)
        m5_cache = {}
        for e in events:
            if e["signals"]:
                d = lf._read_json(lf.lf_dir("m5", f"{e['inst']['symbol']}.json"))
                if d and d["rows"]:
                    m5_cache[e["inst"]["symbol"]] = lf.to_df(d["rows"])
        rep["fade"] = fade_part(rules, events, m5_cache, now, lf.FEE_TAKER_PCT, log)
    except Exception as e:      # noqa: BLE001
        rep["fade"] = None
        errors.append(f"Fade part not run: {type(e).__name__}: {e}")
    rep["errors"] = errors
    if rep["ranker"] is None and rep["fade"] is None:
        raise RuntimeError("; ".join(errors) or "nothing to run")
    rep["verdict"] = rep["ranker"]["verdict"] if rep["ranker"] else "insufficient n"
    rep["verdict_failed_conditions"] = rep["ranker"]["verdict_failed_conditions"] if rep["ranker"] else errors
    rep["limits"] = ["No order-book data: queue position is not modelled (delta 0 is an optimistic bound).", "The fill rule is a touch of the hourly / 5-minute high or low, with no knowledge of the order inside the bar.",
                     "The maker fee is the public tier (0.02%). A chase pays the price move since the open, but the position is not marked for the hours before it is filled.",
                     "Fade part: slot interactions of skipped trades are not re-simulated; it is a diagnostic without a verdict."]
    return rep


def to_markdown(rep: dict) -> str:
    L = [f"# Maker-fill cost model {rep['run_id']}", "", f"## Ranker verdict: `{rep['verdict']}`", "", ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "",
         f"Maker fee {rep['maker_fee_pct']}%, taker fee {rep['taker_fee_pct']}% + slippage {rep['slippage_pct'][0]}% (stress {rep['slippage_pct'][1]}%) per side."]
    r = rep.get("ranker")
    if r:
        p = r["primary"]
        L += ["", f"## Part 1: LightGBM ranker book (primary: limit {p['delta'] * 100:.2f}% better than the open, {p['timeout_h']} h to fill, then {p['mode']})", "",
              "| execution | OOS APR % | OOS Sharpe | OOS max DD % | locked APR % | fill rate | chased | skipped |", "|---|---|---|---|---|---|---|---|"]
        for k, b in r["baseline"].items():
            L.append(f"| {k} | {b['oos'].get('apr_pct')} | {b['oos'].get('sharpe')} | {b['oos'].get('max_dd_pct')} | {b['locked'].get('apr_pct')} | - | - | - |")
        for g in r["grid"]:
            mark = " (primary)" if (g["mode"], g["delta"], g["timeout_h"]) == (p["mode"], p["delta"], p["timeout_h"]) else ""
            L.append(f"| {g['mode']} {g['delta'] * 100:.2f}% / {g['timeout_h']} h{mark} | {g['oos'].get('apr_pct')} | {g['oos'].get('sharpe')} | {g['oos'].get('max_dd_pct')} | {g['locked'].get('apr_pct')} | {g['fill_rate']} | {g['chased']} | {g['skipped']} |")
    f = rep.get("fade")
    if f:
        L += ["", f"## Part 2: Listing Fade entries with a limit order ({f['trades_replayed']} replayed trades; {f['basis']})", "",
              "| limit above open | wait | fill rate | taker P&L per trade if it WOULD fill ($) | ... if it would NOT fill ($) | net $ taker (all) | net $ skip unfilled | net $ chase unfilled |", "|---|---|---|---|---|---|---|---|"]
        for x in f["rows"]:
            L.append(f"| {x['delta'] * 100:.2f}% | {x['timeout_min']} min | {x['fill_rate']} | {x['taker_mean_usd_filled']} | {x['taker_mean_usd_unfilled']} | {x['net_usd_taker_all']} | {x['net_usd_skip']} | {x['net_usd_chase']} |")
        L += ["", "If the trades that would NOT fill earn more per trade than the ones that would, passive entry removes the winners (adverse selection)."]
    L += [""] + [f"- NOT RUN: {e}" for e in rep.get("errors", [])] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
