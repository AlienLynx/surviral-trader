"""
P25: funding carry study -- long spot / short USDT perpetual on Bybit, hedged, replayed on public history. Rule based, no LLM, $0, nothing is traded.

What is simulated (daily bars, decisions at 00:00 UTC from data BEFORE the day, fills at that day's open):
  universe  BTC, ETH + the top N USDT perpetuals by trailing 30-day turnover AS OF each day (delisted, status Closed, instruments included: that is where the tail risk is)
  entry     trailing 3-day mean funding, annualised, >= HURDLE_FACTOR x round-trip cost spread over the expected hold (majors 60 days, alts 14 days)
  exit      trailing 3-day mean funding < 0, or the contract's delivery (delisting) day
  size      the sleeve split in equal parts over MAX_POSITIONS
  P&L       funding received (settlements strictly after the entry open and strictly before the exit open) + basis (index price return minus perp price return: the hedge is not perfect)
            - fees (spot 0.1% x2, perp 0.055% x2) - slippage (alts 0.3% / majors 0.1% per round trip)
The verdict is computed by code against thresholds fixed BEFORE the run (PASS below); the locked period (before LOCK_END) is for looking, the out-of-sample period decides.
Known limits (in every report): daily bars hide intraday basis spikes and liquidation risk; collateral haircuts and margin calls are not modelled; funding settles on the symbol's own
interval; exchange / custody / legal risk is not a number here.
"""
from __future__ import annotations

import bisect
import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.engine import evidence as ev

from . import data as rdata, listing_fade as lf, paths

BYBIT = rdata.BYBIT
STUDY_ID = "carry"
DAY_MS = 86_400_000
SINCE = "2024-01-01"
LOCK_END = "2025-07-01"          # before this date: look; from it: the out-of-sample period that decides
POOL_TURNOVER_TOP = 80           # current top symbols by 24h turnover, plus every Closed (delisted) instrument
MAJORS = ("BTCUSDT", "ETHUSDT")
UNIVERSE_TOP_N = 30
MAX_POSITIONS = 5
SLEEVE_USD = 3000.0
FEE_SPOT_PCT, FEE_PERP_PCT = 0.10, 0.055          # per side, Bybit VIP0 taker (API orders get no MNT discount)
SLIP_MAJOR_PCT, SLIP_ALT_PCT = 0.10, 0.30          # per ROUND TRIP, both legs
HOLD_MAJOR_DAYS, HOLD_ALT_DAYS = 60, 14
HURDLE_FACTOR = 2.0
DELIST_EXTRA_COST_PCT = 1.0
PASS = {"min_episodes": 60, "min_apr_deployed_pct": 5.0, "max_dd_pct_of_sleeve": 3.0, "min_worst_episode_pct": -1.5, "min_edge_over_majors_pp": 1.0}


# ------------------------------------------------------------------ cache
def cdir(*parts: str):
    name = "/".join(parts)
    return paths.replay_path(STUDY_ID, name) if name else paths.replay_path(STUDY_ID)


def _fetch_index_klines(get, symbol: str, start_ms: int, end_ms: int, sleep=time.sleep) -> list[list[float]]:
    out: dict[int, list[float]] = {}
    end = end_ms
    for _ in range(40):
        data = rdata._get_json(get, f"{BYBIT}/v5/market/index-price-kline", {"category": "linear", "symbol": symbol, "interval": "D", "start": start_ms, "end": end, "limit": 1000}, sleep=sleep)
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:5]]
        oldest = min(int(r[0]) for r in rows)
        if oldest <= start_ms or oldest >= end:
            break
        end = oldest - 1
    return [out[k] for k in sorted(out)]


def day_start(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def fetch_pool(get, now: datetime, top_n: int = POOL_TURNOVER_TOP) -> list[dict]:
    """Trading perps with the top 24h turnover + every Closed one. [{symbol, status, launch_ms, delivery_ms}]."""
    items: dict[str, dict] = {}
    for status in ("Trading", "Closed"):
        cursor = ""
        for _ in range(30):
            params = {"category": "linear", "status": status, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            res = (rdata._get_json(get, f"{BYBIT}/v5/market/instruments-info", params).get("result") or {})
            for it in res.get("list") or []:
                if it.get("quoteCoin") != "USDT" or it.get("contractType") != "LinearPerpetual":
                    continue
                items[it["symbol"]] = {"symbol": it["symbol"], "status": it.get("status", status), "launch_ms": int(it.get("launchTime") or 0), "delivery_ms": int(it.get("deliveryTime") or 0)}
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
    tick = (rdata._get_json(get, f"{BYBIT}/v5/market/tickers", {"category": "linear"}).get("result") or {}).get("list") or []
    turn = {t["symbol"]: float(t.get("turnover24h") or 0) for t in tick}
    trading = sorted((s for s, i in items.items() if i["status"] == "Trading"), key=lambda s: -turn.get(s, 0))
    keep = set(trading[:top_n]) | set(MAJORS) | {s for s, i in items.items() if i["status"] != "Trading"}
    return [items[s] for s in sorted(keep) if s in items]


def _perp_turnover(sym: str) -> dict[int, float]:
    rows = (lf._read_json(cdir("perp", f"{sym}.json")) or {}).get("rows") or []
    return {int(r[0]): float(r[6]) for r in rows if len(r) > 6}


def ever_in_universe(pool: list[dict], start: datetime, end: datetime, top_n: int = UNIVERSE_TOP_N) -> set[str]:
    """Symbols that rank in the top N by trailing 30-day turnover on ANY day of the window (+ the majors). Only these need index prices and funding: the others are never candidates, so
    the study's universe is unchanged. Computed from the daily perp bars alone (same as-of rule as the simulation: mean of the 30 days BEFORE the day, at least 20 of them present)."""
    d0, d1 = day_start(start), day_start(end)
    days = list(range(d0, d1 + 1, DAY_MS))
    syms, mat = [], []
    for it in pool:
        sym = it["symbol"]
        if sym in MAJORS:
            continue
        t = _perp_turnover(sym)
        if not t:
            continue
        syms.append(sym)
        mat.append([t.get(d, np.nan) for d in range(d0 - 30 * DAY_MS, d1 + 1, DAY_MS)])
    keep = set(MAJORS)
    if not syms:
        return keep
    a = np.array(mat, dtype=float)                       # symbols x (30 + days)
    for k in range(len(days)):
        win = a[:, k:k + 30]                             # the 30 days before day k
        n = np.sum(~np.isnan(win), axis=1)
        mean = np.where(n >= 20, np.nansum(win, axis=1) / np.maximum(n, 1), -1.0)
        order = np.argsort(-mean)[:top_n]
        keep |= {syms[i] for i in order if mean[i] > 0}
    return keep


def fetch_spot_symbols(get) -> list[str]:
    """Symbols with a Bybit SPOT market today (status Trading). The hedge needs the spot leg: a perpetual without one (BAKE, FARTCOIN, ...) cannot be carried."""
    out, cursor = [], ""
    for _ in range(10):
        params = {"category": "spot", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        res = (rdata._get_json(get, f"{BYBIT}/v5/market/instruments-info", params).get("result") or {})
        out += [i["symbol"] for i in res.get("list") or [] if i.get("status") == "Trading" and i.get("quoteCoin") == "USDT"]
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break
    return sorted(set(out))


def fetch_all(get, now: datetime, log=lambda s: None, progress=lambda **k: None, stop: threading.Event | None = None, pace: float = 0.05, sleep=time.sleep, workers: int = 6) -> dict:
    """Phase 1: daily perp bars of every pool symbol. Phase 2: index bars + funding of the symbols that are ever in the as-of top-N universe. Cached and resumable, polite (shared pacing), parallel."""
    from concurrent.futures import ThreadPoolExecutor

    pc = lf.Paced(get, pace, sleep)
    start = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc) - timedelta(days=40)
    s_ms, e_ms = day_start(start), day_start(now)
    pool = fetch_pool(pc, now)
    lf._write_json(cdir("pool.json"), {"fetched_at": e_ms, "items": pool, "spot": fetch_spot_symbols(pc)})
    log(f"pool: {len(pool)} symbols ({sum(1 for p in pool if p['status'] != 'Trading')} delisted)")
    errors = [0]
    done = [0]
    lock = threading.Lock()

    def bounds(it):
        lo = max(s_ms, day_start(datetime.fromtimestamp(it["launch_ms"] / 1000, tz=timezone.utc))) if it["launch_ms"] else s_ms
        hi = e_ms if not it["delivery_ms"] else min(e_ms, it["delivery_ms"])
        return lo, hi

    def run(items, total, work):
        def one(it):
            if stop is not None and stop.is_set():
                return
            try:
                work(it)
            except Exception as e:      # noqa: BLE001
                with lock:
                    errors[0] += 1
                log(f"{it['symbol']}: {type(e).__name__}: {e}")
            with lock:
                done[0] += 1
                progress(step=done[0], total=total, api_calls=pc.calls, symbol_errors=errors[0])
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))

    def perp_work(it):
        lo, hi = bounds(it)
        lf._cached_series(cdir("perp", f"{it['symbol']}.json"), lambda a, b: rdata.fetch_klines("linear", it["symbol"], a, b, pc, sleep, "D"), lo, hi, DAY_MS)

    total = len(pool) * 2
    run(pool, total, perp_work)
    if stop is not None and stop.is_set():
        log("stopped")
        return {"stopped": True, "pool": len(pool), "errors": errors[0], "calls": pc.calls}
    keep = ever_in_universe(pool, datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc), now) if len(pool) > 3 else {it["symbol"] for it in pool}
    need = [it for it in pool if it["symbol"] in keep]
    log(f"phase 2: index bars and funding for {len(need)} of {len(pool)} symbols (the rest never reach the top {UNIVERSE_TOP_N} by turnover)")
    lf._write_json(cdir("needed.json"), [it["symbol"] for it in need])
    done[0] = len(pool)

    def side_work(it):
        lo, hi = bounds(it)
        sym = it["symbol"]
        lf._cached_series(cdir("index", f"{sym}.json"), lambda a, b: _fetch_index_klines(pc, sym, a, b, sleep), lo, hi, DAY_MS)
        lf._cached_series(cdir("funding", f"{sym}.json"), lambda a, b: lf.fetch_funding(pc, sym, a, b), lo, hi, 8 * 3_600_000)

    total = len(pool) + len(need)
    run(need, total, side_work)
    return {"stopped": bool(stop is not None and stop.is_set()), "pool": len(pool), "needed": len(need), "errors": errors[0], "calls": pc.calls}


def state() -> dict:
    pool = lf._read_json(cdir("pool.json"))
    if not pool:
        return {"data_ready": False, "reason": "no data yet: run the study (it fetches the public Bybit history first, about 600-1000 calls, $0)"}
    n = len(pool["items"])
    have = sum(1 for it in pool["items"] if cdir("funding", f"{it['symbol']}.json").is_file())
    return {"data_ready": have >= max(1, min(10, n // 2)), "pool": n, "with_data": have, "fetched_at": pool["fetched_at"]}


# ------------------------------------------------------------------ data in memory
class Sym:
    """One symbol: daily perp / index opens, daily turnover, funding settlements."""

    def __init__(self, info: dict, perp: list[list], index: list[list], funding: list[list]):
        self.info = info
        self.days = [int(r[0]) for r in perp]
        self.p_open = {int(r[0]): float(r[1]) for r in perp}
        self.turn = {int(r[0]): float(r[6]) for r in perp if len(r) > 6}
        self.i_open = {int(r[0]): float(r[1]) for r in index}
        self.f_ts = [int(r[0]) for r in funding]
        self.f_rate = [float(r[1]) for r in funding]
        self.delivery = info.get("delivery_ms") or 0

    def funding_sum(self, t_lo: int, t_hi: int) -> float:
        """sum of rates of settlements with t_lo <= ts < t_hi (a day bucket: the settlement AT the day's open belongs to that day)"""
        a, b = bisect.bisect_left(self.f_ts, t_lo), bisect.bisect_left(self.f_ts, t_hi)
        return float(sum(self.f_rate[a:b]))

    def funding_ann_3d(self, day: int) -> float | None:
        a, b = bisect.bisect_left(self.f_ts, day - 3 * DAY_MS), bisect.bisect_left(self.f_ts, day)
        if b - a < 2:
            return None
        return float(sum(self.f_rate[a:b])) / 3.0 * 365.0 * 100.0          # percent per year

    def turnover_30d(self, day: int) -> float | None:
        vals = [self.turn[d] for d in range(day - 30 * DAY_MS, day, DAY_MS) if d in self.turn]
        return float(np.mean(vals)) if len(vals) >= 20 else None

    def opens(self, day: int) -> tuple[float, float] | None:
        p, i = self.p_open.get(day), self.i_open.get(day)
        return (p, i) if p and i else None


def load_data(base_dir=None) -> dict[str, Sym]:
    pool = lf._read_json(cdir("pool.json")) or {"items": []}
    out = {}
    for it in pool["items"]:
        sym = it["symbol"]
        perp = (lf._read_json(cdir("perp", f"{sym}.json")) or {}).get("rows") or []
        idx = (lf._read_json(cdir("index", f"{sym}.json")) or {}).get("rows") or []
        fr = (lf._read_json(cdir("funding", f"{sym}.json")) or {}).get("rows") or []
        if perp and idx and fr:
            out[sym] = Sym(it, perp, idx, fr)
    return out


# ------------------------------------------------------------------ simulation
def cost_pct(sym: str, alt_slip_pct: float) -> float:
    major = sym in MAJORS
    return 2 * FEE_SPOT_PCT + 2 * FEE_PERP_PCT + (SLIP_MAJOR_PCT if major else alt_slip_pct)


def hurdle_ann_pct(sym: str, alt_slip_pct: float) -> float:
    hold = HOLD_MAJOR_DAYS if sym in MAJORS else HOLD_ALT_DAYS
    return HURDLE_FACTOR * cost_pct(sym, alt_slip_pct) * 365.0 / hold


def simulate(data: dict[str, Sym], start: datetime, end: datetime, alt_slip_pct: float = SLIP_ALT_PCT, majors_only: bool = False, top_n: int = UNIVERSE_TOP_N,
             max_positions: int = MAX_POSITIONS, sleeve: float = SLEEVE_USD, allowed: set[str] | None = None) -> dict:
    d0, d1 = day_start(start), day_start(end)
    notional = sleeve / max_positions
    open_pos: dict[str, dict] = {}
    episodes: list[dict] = []
    daily = {}
    cum = 0.0
    deployed_days = 0.0
    day = d0
    while day <= d1:
        pnl_day = 0.0
        # ---- mark the day that just ended (previous open -> this open) for positions held across it
        for sym, pos in list(open_pos.items()):
            s = data[sym]
            prev = pos["last_day"]
            o_prev, o_now = s.opens(prev), s.opens(day)
            fday = s.funding_sum(prev, day)
            if prev == pos["entry_day"]:                                    # the settlement at exactly the entry open is not earned (the entry fills after it); the one at the exit open is not either
                fday -= sum(r for t, r in zip(s.f_ts, s.f_rate) if t == prev)
            basis = 0.0
            if o_prev and o_now:
                basis = (o_now[1] / o_prev[1] - 1.0) - (o_now[0] / o_prev[0] - 1.0)
            pos["funding"] += fday
            pos["basis"] += basis
            pos["last_day"] = day
            pnl_day += notional * (fday + basis)
            deployed_days += notional / sleeve
        # ---- exits at this open
        for sym, pos in list(open_pos.items()):
            s = data[sym]
            f3 = s.funding_ann_3d(day)
            delisted = bool(s.delivery) and day >= (s.delivery // DAY_MS) * DAY_MS
            gone = s.opens(day) is None and day - pos["last_day"] >= 2 * DAY_MS
            if delisted or gone or (f3 is not None and f3 < 0.0):
                c = cost_pct(sym, alt_slip_pct) + (DELIST_EXTRA_COST_PCT if (delisted or gone) else 0.0)
                net = pos["funding"] + pos["basis"] - c / 100.0
                pnl_day -= notional * (c / 100.0) / 2.0                     # half of the round trip at the exit (the other half was charged at the entry)
                episodes.append({"symbol": sym, "entry": pos["entry_day"], "exit": day, "days": (day - pos["entry_day"]) // DAY_MS, "funding_pct": round(pos["funding"] * 100, 4), "basis_pct": round(pos["basis"] * 100, 4),
                                 "cost_pct": round(c, 4), "net_pct": round(net * 100, 4), "reason": "delisted" if (delisted or gone) else "funding<0"})
                del open_pos[sym]
        # ---- entries at this open
        if len(open_pos) < max_positions and day < d1:
            cands = []
            uni = list(MAJORS) if majors_only else None
            if uni is None:
                scored = [(s.turnover_30d(day), sym) for sym, s in data.items() if sym not in MAJORS and (allowed is None or sym in allowed)]
                top = [sym for t, sym in sorted(((t, sym) for t, sym in scored if t), reverse=True)[:top_n]]
                uni = list(MAJORS) + top
            for sym in uni:
                if sym in open_pos or sym not in data:
                    continue
                s = data[sym]
                if s.opens(day) is None or (s.delivery and day >= (s.delivery // DAY_MS) * DAY_MS - 7 * DAY_MS):
                    continue
                f3 = s.funding_ann_3d(day)
                if f3 is not None and f3 >= hurdle_ann_pct(sym, alt_slip_pct):
                    cands.append((f3, sym))
            for f3, sym in sorted(cands, reverse=True):
                if len(open_pos) >= max_positions:
                    break
                c = cost_pct(sym, alt_slip_pct)
                pnl_day -= notional * (c / 100.0) / 2.0
                open_pos[sym] = {"entry_day": day, "last_day": day, "funding": 0.0, "basis": 0.0}
        cum += pnl_day
        daily[day] = cum
        day += DAY_MS
    # positions still open at the end are closed at the last open for accounting (no extra exit cost: the second half of the cost was never charged, so charge it)
    for sym, pos in list(open_pos.items()):
        c = cost_pct(sym, alt_slip_pct)
        net = pos["funding"] + pos["basis"] - c / 100.0
        episodes.append({"symbol": sym, "entry": pos["entry_day"], "exit": d1, "days": (d1 - pos["entry_day"]) // DAY_MS, "funding_pct": round(pos["funding"] * 100, 4), "basis_pct": round(pos["basis"] * 100, 4),
                         "cost_pct": round(c, 4), "net_pct": round(net * 100, 4), "reason": "open_at_end"})
    days_total = max(1.0, (d1 - d0) / DAY_MS)
    return {"episodes": episodes, "equity": [(d, round(v, 4)) for d, v in sorted(daily.items())], "avg_deployed_share": deployed_days / days_total, "days": days_total, "sleeve": sleeve}


def max_drawdown(equity: list[tuple[int, float]], sleeve: float) -> float:
    peak, dd = 0.0, 0.0
    for _, v in equity:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd / sleeve * 100.0


def stats(sim: dict, lo: int | None = None, hi: int | None = None) -> dict:
    eps = [e for e in sim["episodes"] if (lo is None or e["exit"] >= lo) and (hi is None or e["exit"] < hi)]
    eq = [(d, v) for d, v in sim["equity"] if (lo is None or d >= lo) and (hi is None or d < hi)]
    n = len(eps)
    nets = [e["net_pct"] / 100.0 for e in eps]
    ci = ev.bootstrap_ci_mean(nets) if n >= 10 else None
    days = max(1.0, ((hi or sim["equity"][-1][0]) - (lo or sim["equity"][0][0])) / DAY_MS) if sim["equity"] else 1.0
    pnl = (eq[-1][1] - eq[0][1]) if len(eq) > 1 else 0.0
    share = max(1e-9, sim["avg_deployed_share"])
    return {"episodes": n, "mean_episode_pct": round(float(np.mean(nets)) * 100, 4) if n else None, "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
            "worst_episode_pct": round(min(e["net_pct"] for e in eps), 4) if n else None, "net_usd": round(pnl, 2), "apr_on_sleeve_pct": round(pnl / sim["sleeve"] / days * 365 * 100, 3),
            "apr_on_deployed_pct": round(pnl / sim["sleeve"] / days * 365 * 100 / share, 3), "max_dd_pct_of_sleeve": round(max_drawdown(eq, sim["sleeve"]), 3) if eq else None,
            "avg_deployed_pct": round(share * 100, 1), "mean_days_held": round(float(np.mean([e["days"] for e in eps])), 1) if n else None,
            "by_exit": {r: sum(1 for e in eps if e["reason"] == r) for r in {e["reason"] for e in eps}}}


def verdict(oos: dict, edge_over_majors_pp: float | None) -> dict:
    failed = []
    n = oos["episodes"]
    if n < PASS["min_episodes"]:
        return {"verdict": "insufficient n", "failed": [f"episodes {n} < {PASS['min_episodes']}"]}
    ci = oos["ci95_pct"]
    if oos["apr_on_deployed_pct"] < PASS["min_apr_deployed_pct"]:
        failed.append(f"net APR on deployed notional {oos['apr_on_deployed_pct']}% < {PASS['min_apr_deployed_pct']}%")
    if oos["max_dd_pct_of_sleeve"] is not None and oos["max_dd_pct_of_sleeve"] > PASS["max_dd_pct_of_sleeve"]:
        failed.append(f"max drawdown {oos['max_dd_pct_of_sleeve']}% of the sleeve > {PASS['max_dd_pct_of_sleeve']}%")
    if oos["worst_episode_pct"] < PASS["min_worst_episode_pct"]:
        failed.append(f"worst episode {oos['worst_episode_pct']}% < {PASS['min_worst_episode_pct']}%")
    if ci is None or ci[0] <= 0:
        failed.append("bootstrap CI of the mean episode does not exclude 0")
    if edge_over_majors_pp is not None and edge_over_majors_pp < PASS["min_edge_over_majors_pp"]:
        failed.append(f"does not beat BTC/ETH-only by {PASS['min_edge_over_majors_pp']} pp (edge {edge_over_majors_pp} pp): run majors only")
    if not failed:
        return {"verdict": "edge candidate", "failed": []}
    all_neg = oos["mean_episode_pct"] is not None and ci is not None and ci[1] < 0
    return {"verdict": "no edge" if all_neg else "unproven", "failed": failed}


# ------------------------------------------------------------------ study
def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    data = load_data()
    if len(data) < 2:
        raise RuntimeError("not enough carry data: fetch first")
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    lock = day_start(datetime.fromisoformat(LOCK_END).replace(tzinfo=timezone.utc))
    spot = (lf._read_json(cdir("pool.json")) or {}).get("spot")
    if not spot:
        raise RuntimeError("no list of spot markets in the cache: run the study again (it fetches it)")
    spot = set(spot)
    scenarios = {}
    sims = {}
    # primary: only perpetuals that have a spot market (the long leg must exist). The last scenario ignores that and is NOT realisable: it is shown so the size of the mistake is visible.
    for name, kw in (("spot_alts_slip0.3", {"allowed": spot}), ("spot_alts_slip0.6", {"allowed": spot, "alt_slip_pct": 0.6}), ("majors_only", {"majors_only": True}), ("no_spot_constraint_NOT_REALISABLE", {})):
        log(f"simulating {name}")
        sim = simulate(data, start, end, **kw)
        sims[name] = sim
        scenarios[name] = {"all": stats(sim), "locked": stats(sim, None, lock), "oos": stats(sim, lock, None)}
    prim, maj = scenarios["spot_alts_slip0.3"], scenarios["majors_only"]
    edge_pp = None if maj["oos"]["apr_on_deployed_pct"] is None else round(prim["oos"]["apr_on_deployed_pct"] - maj["oos"]["apr_on_deployed_pct"], 3)
    v = verdict(prim["oos"], edge_pp)
    funding_now = {}
    for sym in MAJORS:
        if sym in data:
            funding_now[sym] = round(data[sym].funding_ann_3d(day_start(now)) or 0.0, 2)
    return {"type": "carry_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": "spot_alts_slip0.3", "pass_thresholds": PASS,
            "window": {"start": start.date().isoformat(), "end": end.date().isoformat(), "lock_end": LOCK_END}, "universe": {"pool": len(data), "delisted": sum(1 for s in data.values() if s.info["status"] != "Trading")},
            "rules": {"entry": f"trailing 3d funding (annualised) >= {HURDLE_FACTOR} x round-trip cost x 365 / expected hold (majors {HOLD_MAJOR_DAYS} d, alts {HOLD_ALT_DAYS} d)", "exit": "trailing 3d funding < 0, or delivery (delisting)",
                      "fees_pct": {"spot_per_side": FEE_SPOT_PCT, "perp_per_side": FEE_PERP_PCT}, "slippage_round_trip_pct": {"majors": SLIP_MAJOR_PCT, "alts": SLIP_ALT_PCT}, "max_positions": MAX_POSITIONS, "sleeve_usd": SLEEVE_USD,
                      "universe": f"BTC, ETH + top {UNIVERSE_TOP_N} by trailing 30d turnover as of each day, among perpetuals that have a Bybit spot market today"},
            "scenarios": scenarios, "edge_over_majors_oos_pp": edge_pp, "funding_now_ann_pct_3d": funding_now, "limits": LIMITS,
            "_sims": {k: {"episodes": v_["episodes"], "equity": v_["equity"]} for k, v_ in sims.items()}}


LIMITS = ["The spot leg is required: only perpetuals with a Bybit spot market TODAY are candidates (the list of past spot markets is not public). That drops delisted symbols, so the delisting tail is under-represented here; the carry on coins without spot (the biggest earners of the unconstrained run) is impossible and excluded.",
          "Daily bars: intraday basis spikes and liquidation risk are not modelled; no collateral haircuts or margin calls.",
          "Funding is taken from the history at each symbol's own settlements; the entry fills at the day's open (a passive order may not fill).",
          "Exchange, custody and legal risk are not numbers here (Bybit lost 1.46B in Feb 2025; access from Belarus has been cut by the regulator before).",
          "A positive result is a hypothesis for a small real-money test, never `edge` by itself."]


def to_markdown(rep: dict) -> str:
    out = [f"# Funding carry study {rep['run_id']}", "", "**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small real-money test, not proof.**", "",
           f"## Verdict: `{rep['verdict']}`", "", ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "",
           f"Window {rep['window']['start']} .. {rep['window']['end']}, locked period before {rep['window']['lock_end']}, out-of-sample after it. Universe: {rep['universe']}", "",
           f"Funding now (3-day mean, annualised %): {rep['funding_now_ann_pct_3d']}", "", "## Scenarios", "",
           "| scenario | period | episodes | mean episode % | CI95 % | worst % | net $ | APR sleeve % | APR deployed % | max DD % sleeve | deployed % |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, sc in rep["scenarios"].items():
        for per in ("all", "locked", "oos"):
            s = sc[per]
            out.append(f"| {name} | {per} | {s['episodes']} | {s['mean_episode_pct']} | {s['ci95_pct']} | {s['worst_episode_pct']} | {s['net_usd']} | {s['apr_on_sleeve_pct']} | {s['apr_on_deployed_pct']} | {s['max_dd_pct_of_sleeve']} | {s['avg_deployed_pct']} |")
    out += ["", "## Rules", "", "```", json.dumps(rep["rules"], indent=1), "```", "", "## Pass thresholds (fixed before the run)", "", "```", json.dumps(rep["pass_thresholds"], indent=1), "```", "", "## Limits", ""]
    out += [f"- {x}" for x in rep["limits"]]
    return "\n".join(out) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    sims = rep.pop("_sims", {})
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["scenario", "symbol", "entry", "exit", "days", "funding_pct", "basis_pct", "cost_pct", "net_pct", "reason"])
    for name, s in sims.items():
        for e in s["episodes"]:
            cw.writerow([name, e["symbol"], datetime.fromtimestamp(e["entry"] / 1000, tz=timezone.utc).date(), datetime.fromtimestamp(e["exit"] / 1000, tz=timezone.utc).date(), e["days"], e["funding_pct"], e["basis_pct"], e["cost_pct"], e["net_pct"], e["reason"]])
    (base / "episodes.csv").write_text(buf.getvalue(), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["scenario", "date", "cumulative_pnl_usd"])
    for name, s in sims.items():
        for d, v in s["equity"]:
            cw.writerow([name, datetime.fromtimestamp(d / 1000, tz=timezone.utc).date(), v])
    (base / "equity.csv").write_text(buf.getvalue(), encoding="utf-8")
