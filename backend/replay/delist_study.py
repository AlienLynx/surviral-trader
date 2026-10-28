"""
P25-B: delisting event study -- short the perpetual of a coin whose delisting Bybit has just announced, cover before the delisting. Rule based, no LLM, $0, nothing is traded.

Events: every Bybit perpetual-delisting notice since 2024-01-01 that states the delisting time (parsed by backend/engine/announcements.py: exact symbols, publish time, delisting time).
Trade (fixed before the run, see PRIMARY):
  entry   the open of the first hourly bar at least ENTRY_DELAY after the notice was published (the notice is public at its publish time; the delay is our reaction time)
  exit    the earlier of: HOLD_MAX_H after the entry, EXIT_BUFFER before the stated delisting time (a contract is settled at delisting: never be in it), or a stop at +STOP_PCT adverse (hourly highs; a gap fills at the open)
  P&L     short return + funding (a short RECEIVES positive funding, pays negative) - taker fees both sides - slippage (a round-trip percentage: 0.3 / 0.6 / 1.0, delisting coins are thin)
  hedge   the same notional long BTC over the same window (strips the market; fees and 0.05% slippage charged)
Baseline: for every event, 20 random shorts of the same length on the same coin in the 30 days BEFORE the notice (same costs, same stop). The event's excess = its net minus the mean of those.
The verdict is computed by code against thresholds fixed before the run. Known limits (in every report): hourly bars hide intraday squeezes; liquidity is thin and the real slippage on a delisting coin can be
much larger than 1%; Bybit can halt a contract; borrow / margin is not modelled; delisted symbols older than the API's memory are missing (counted); a notice published inside our entry delay is skipped.
"""
from __future__ import annotations

import csv
import io
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.engine import announcements as ann, evidence as ev

from . import data as rdata, listing_fade as lf, paths

BYBIT = rdata.BYBIT
STUDY_ID = "delist"
HOUR = 3_600_000
SINCE = "2024-01-01"
FEE_TAKER_PCT = 0.055
HEDGE_SLIP_PCT = 0.05
STOP_PCT = 10.0
RANDOM_DRAWS = 20
PRE_DAYS = 30
MIN_TURNOVER_24H = 100_000.0       # USDT traded in the 24h before the notice; a thinner coin is skipped (and counted)
PRIMARY = {"delay_h": 1, "slip_pct": 0.6, "hold_max_h": 72, "exit_buffer_h": 2}
GRID_DELAY = (0, 1, 6, 24)
GRID_SLIP = (0.3, 0.6, 1.0)
PASS = {"min_events": 100, "min_mean_hedged_pct": 1.0, "max_worst_pct": -8.0, "max_top5_share": 0.5, "min_baseline_percentile": 95.0}


def cdir(*parts: str):
    name = "/".join(parts)
    return paths.replay_path(STUDY_ID, name) if name else paths.replay_path(STUDY_ID)


# ------------------------------------------------------------------ data
def load_events(get, now: datetime, since: str = SINCE, refresh: bool = True) -> list[dict]:
    """One event per (notice, coin): {symbol, coin, published_ms, effective_ms, title}. Cached in events.json; the feed is re-read when `refresh`."""
    p = cdir("events.json")
    cached = lf._read_json(p)
    if cached and not refresh:
        return cached["events"]
    items = ann.fetch(get, pages=11)
    s_ms = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
    ev_, seen = [], set()
    for it in items:
        e = ann.parse_item(it)
        if not e or e["kind"] != "perp_delisting" or not e.get("effective_ms") or e["published_ms"] < s_ms or e["effective_ms"] > int(now.timestamp() * 1000) + 40 * 86_400_000:
            continue
        for coin in e["coins"]:
            key = (coin, e["published_ms"])
            if key in seen:
                continue
            seen.add(key)
            ev_.append({"symbol": coin + "USDT", "coin": coin, "published_ms": e["published_ms"], "effective_ms": e["effective_ms"], "title": e["title"]})
    ev_.sort(key=lambda x: x["published_ms"])
    lf._write_json(p, {"fetched_at": int(now.timestamp() * 1000), "events": ev_})
    return ev_


def fetch_all(get, now: datetime, log=lambda s: None, progress=lambda **k: None, stop: threading.Event | None = None, pace: float = 0.05, sleep=time.sleep, workers: int = 6) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    pc = lf.Paced(get, pace, sleep)
    events = load_events(pc, now)
    log(f"events: {len(events)} perpetual delisting notices with a stated time since {SINCE}")
    first = events[0]["published_ms"] - (PRE_DAYS + 2) * 24 * HOUR if events else 0
    lf._cached_series(cdir("btc.json"), lambda a, b: rdata.fetch_klines("linear", "BTCUSDT", a, b, pc, sleep, "60"), first, int(now.timestamp() * 1000), HOUR)
    lf._cached_series(cdir("funding", "BTCUSDT.json"), lambda a, b: lf.fetch_funding(pc, "BTCUSDT", a, b), first, int(now.timestamp() * 1000), 8 * HOUR)
    errors, done, lock = [0], [0], threading.Lock()

    def one(e):
        if stop is not None and stop.is_set():
            return
        sym = e["symbol"]
        lo, hi = e["published_ms"] - (PRE_DAYS + 1) * 24 * HOUR, e["effective_ms"] + 3 * HOUR
        try:
            lf._cached_series(cdir("hourly", f"{sym}.json"), lambda a, b: rdata.fetch_klines("linear", sym, a, b, pc, sleep, "60"), lo, hi, HOUR)
            lf._cached_series(cdir("funding", f"{sym}.json"), lambda a, b: lf.fetch_funding(pc, sym, a, b), e["published_ms"] - 24 * HOUR, hi, 8 * HOUR)
        except Exception as ex:      # noqa: BLE001
            with lock:
                errors[0] += 1
            log(f"{sym}: {type(ex).__name__}: {ex}")
        with lock:
            done[0] += 1
            progress(step=done[0], total=len(events), api_calls=pc.calls, symbol_errors=errors[0])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, events))
    return {"stopped": bool(stop is not None and stop.is_set()), "events": len(events), "errors": errors[0], "calls": pc.calls}


def state() -> dict:
    ev_ = lf._read_json(cdir("events.json"))
    if not ev_:
        return {"data_ready": False, "reason": "no data yet: run the study (it reads the notice archive and the hourly candles first, a few hundred calls, $0)"}
    n = len(ev_["events"])
    have = sum(1 for e in ev_["events"] if cdir("hourly", f"{e['symbol']}.json").is_file())
    return {"data_ready": have > 0, "events": n, "with_candles": have, "fetched_at": ev_["fetched_at"]}


# ------------------------------------------------------------------ simulation
class Bars:
    def __init__(self, rows: list[list]):
        self.ts = np.array([int(r[0]) for r in rows], dtype=np.int64)
        self.o = np.array([float(r[1]) for r in rows])
        self.h = np.array([float(r[2]) for r in rows])
        self.c = np.array([float(r[4]) for r in rows])
        self.turn = np.array([float(r[6]) if len(r) > 6 else 0.0 for r in rows])

    def idx_at_or_after(self, t: int) -> int | None:
        i = int(np.searchsorted(self.ts, t, side="left"))
        return i if i < len(self.ts) else None

    def idx_at_or_before(self, t: int) -> int | None:
        i = int(np.searchsorted(self.ts, t, side="right")) - 1
        return i if i >= 0 else None


def funding_between(frows: list[list], t_lo: int, t_hi: int) -> float:
    """sum of rates of settlements with t_lo < ts <= t_hi"""
    return float(sum(r for ts, r in frows if t_lo < ts <= t_hi))


def short_trade(b: Bars, frows: list[list], entry_idx: int, exit_target_ms: int, slip_pct: float, stop_pct: float = STOP_PCT) -> dict | None:
    """One short from the open of bar entry_idx to the open of the last bar at or before exit_target_ms (or the stop). None when there is no room (exit not after entry)."""
    xi = b.idx_at_or_before(exit_target_ms)
    if xi is None or xi <= entry_idx:
        return None
    entry = b.o[entry_idx]
    if entry <= 0:
        return None
    stop_px = entry * (1.0 + stop_pct / 100.0)
    exit_i, exit_px, reason = xi, b.o[xi], "time"
    for i in range(entry_idx, xi):
        if b.h[i] >= stop_px:
            exit_i, exit_px, reason = i, max(stop_px, b.o[i]) if i > entry_idx else stop_px, "stop"
            break
    ret = -(exit_px / entry - 1.0)
    fund = funding_between(frows, int(b.ts[entry_idx]), int(b.ts[exit_i]))
    costs = (2 * FEE_TAKER_PCT + slip_pct) / 100.0
    return {"entry_i": entry_idx, "exit_i": exit_i, "entry_ts": int(b.ts[entry_idx]), "exit_ts": int(b.ts[exit_i]), "price_ret": ret, "funding": fund, "cost": costs, "net": ret + fund - costs, "reason": reason, "hours": (int(b.ts[exit_i]) - int(b.ts[entry_idx])) // HOUR}


def hedge(btc: Bars, t0: int, t1: int) -> float:
    """return of being long BTC over the window, minus the BTC leg's costs; 0 when BTC bars are missing"""
    i0, i1 = btc.idx_at_or_after(t0), btc.idx_at_or_before(t1)
    if i0 is None or i1 is None or i1 <= i0:
        return 0.0
    return float(btc.o[i1] / btc.o[i0] - 1.0) - (2 * FEE_TAKER_PCT + HEDGE_SLIP_PCT) / 100.0


def run_events(events: list[dict], bars: dict[str, Bars], fund: dict[str, list], btc: Bars, delay_h: int, slip_pct: float, hold_max_h: int, exit_buffer_h: int, seed: int = 7) -> dict:
    rng = np.random.default_rng(seed)
    rows, skipped = [], {"no_candles": 0, "thin": 0, "too_late": 0}
    for e in events:
        b = bars.get(e["symbol"])
        if b is None or len(b.ts) < 48:
            skipped["no_candles"] += 1
            continue
        pub_i = b.idx_at_or_before(e["published_ms"])
        if pub_i is None:
            skipped["no_candles"] += 1
            continue
        if float(b.turn[max(0, pub_i - 23):pub_i + 1].sum()) < MIN_TURNOVER_24H:
            skipped["thin"] += 1
            continue
        ei = b.idx_at_or_after(e["published_ms"] + delay_h * HOUR)
        if ei is None:
            skipped["no_candles"] += 1
            continue
        target = min(int(b.ts[ei]) + hold_max_h * HOUR, e["effective_ms"] - exit_buffer_h * HOUR)
        tr = short_trade(b, fund.get(e["symbol"], []), ei, target, slip_pct)
        if tr is None:
            skipped["too_late"] += 1
            continue
        tr["hedged"] = tr["net"] + hedge(btc, tr["entry_ts"], tr["exit_ts"])
        # baseline: random shorts of the same length in the PRE_DAYS before the notice
        hours = max(1, tr["hours"])
        lo_ts, hi_ts = e["published_ms"] - PRE_DAYS * 24 * HOUR, e["published_ms"] - (hours + 2) * HOUR
        i_lo, i_hi = b.idx_at_or_after(lo_ts), b.idx_at_or_before(hi_ts)
        base, base_h = [], []
        if i_lo is not None and i_hi is not None and i_hi > i_lo:
            for _ in range(RANDOM_DRAWS):
                bi = int(rng.integers(i_lo, i_hi + 1))
                bt = short_trade(b, fund.get(e["symbol"], []), bi, int(b.ts[bi]) + hours * HOUR, slip_pct)
                if bt is not None:
                    base.append(bt["net"])
                    base_h.append(bt["net"] + hedge(btc, bt["entry_ts"], bt["exit_ts"]))
        tr["baseline"] = float(np.mean(base)) if base else None
        tr["baseline_hedged"] = float(np.mean(base_h)) if base_h else None
        tr["baseline_draws"] = base_h
        tr.update({"symbol": e["symbol"], "published_ms": e["published_ms"], "effective_ms": e["effective_ms"]})
        rows.append(tr)
    return {"trades": rows, "skipped": skipped}


def summarize(trades: list[dict], key: str = "hedged") -> dict:
    n = len(trades)
    if not n:
        return {"events": 0}
    x = np.array([t[key] for t in trades])
    ci = ev.bootstrap_ci_mean(list(x)) if n >= 10 else None
    base_key = "baseline_hedged" if key == "hedged" else "baseline"
    pairs = [(t[key], t[base_key]) for t in trades if t.get(base_key) is not None]
    ex = [a - b for a, b in pairs]
    ex_ci = ev.bootstrap_ci_mean(ex) if len(ex) >= 10 else None
    order = sorted(trades, key=lambda t: t["published_ms"])
    half = n // 2
    first, second = order[:half], order[half:]
    top5 = sorted(x, reverse=True)[:5]
    total = float(x.sum())
    # percentile of the event mean among means of random samples of baseline draws (one random draw per event, 500 times)
    pct = None
    draws = [t["baseline_draws"] for t in trades if t.get("baseline_draws")]
    if key == "hedged" and len(draws) >= 10:
        rng = np.random.default_rng(11)
        means = [float(np.mean([d[int(rng.integers(0, len(d)))] for d in draws])) for _ in range(500)]
        pct = round(100.0 * float(np.mean([float(x.mean()) > m for m in means])), 1)
    return {"events": n, "mean_pct": round(float(x.mean()) * 100, 3), "median_pct": round(float(np.median(x)) * 100, 3), "win_pct": round(float((x > 0).mean()) * 100, 1),
            "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 3), round(ci["hi"] * 100, 3)], "worst_pct": round(float(x.min()) * 100, 3), "best_pct": round(float(x.max()) * 100, 3),
            "baseline_mean_pct": round(float(np.mean([b for _, b in pairs])) * 100, 3) if pairs else None,
            "excess_mean_pct": round(float(np.mean(ex)) * 100, 3) if ex else None, "excess_ci95_pct": None if ex_ci is None else [round(ex_ci["lo"] * 100, 3), round(ex_ci["hi"] * 100, 3)],
            "percentile_vs_random": pct, "first_half_mean_pct": round(float(np.mean([t[key] for t in first])) * 100, 3) if first else None,
            "second_half_mean_pct": round(float(np.mean([t[key] for t in second])) * 100, 3) if second else None,
            "top5_share_of_total": round(float(sum(top5)) / total, 3) if total > 0 else None, "stops": sum(1 for t in trades if t["reason"] == "stop"), "mean_hours": round(float(np.mean([t["hours"] for t in trades])), 1)}


def verdict(s: dict) -> dict:
    if s.get("events", 0) < PASS["min_events"]:
        return {"verdict": "insufficient n", "failed": [f"events {s.get('events', 0)} < {PASS['min_events']}"]}
    failed = []
    ci = s["ci95_pct"]
    if s["mean_pct"] < PASS["min_mean_hedged_pct"]:
        failed.append(f"mean net hedged {s['mean_pct']}% < {PASS['min_mean_hedged_pct']}%")
    if ci is None or ci[0] <= 0:
        failed.append("bootstrap CI of the mean does not exclude 0")
    if s["percentile_vs_random"] is None or s["percentile_vs_random"] < PASS["min_baseline_percentile"]:
        failed.append(f"percentile vs random shorts {s['percentile_vs_random']} < {PASS['min_baseline_percentile']}")
    if s["worst_pct"] < PASS["max_worst_pct"]:
        failed.append(f"worst event {s['worst_pct']}% < {PASS['max_worst_pct']}%")
    if (s["first_half_mean_pct"] or 0) <= 0 or (s["second_half_mean_pct"] or 0) <= 0:
        failed.append("a half of the period is not positive")
    if s["top5_share_of_total"] is not None and s["top5_share_of_total"] > PASS["max_top5_share"]:
        failed.append(f"top 5 events are {int(s['top5_share_of_total'] * 100)}% of the total (> {int(PASS['max_top5_share'] * 100)}%)")
    if not failed:
        return {"verdict": "edge candidate", "failed": []}
    return {"verdict": "no edge" if (ci is not None and ci[1] < 0) else "unproven", "failed": failed}


def load_bars(events: list[dict]):
    bars, fund = {}, {}
    for e in events:
        sym = e["symbol"]
        if sym in bars:
            continue
        rows = (lf._read_json(cdir("hourly", f"{sym}.json")) or {}).get("rows") or []
        if rows:
            bars[sym] = Bars(rows)
        fund[sym] = (lf._read_json(cdir("funding", f"{sym}.json")) or {}).get("rows") or []
    btc = Bars((lf._read_json(cdir("btc.json")) or {}).get("rows") or [[0, 1, 1, 1, 1, 1, 0]])
    return bars, fund, btc


def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    now = until or datetime.now(timezone.utc)
    events = (lf._read_json(cdir("events.json")) or {}).get("events") or []
    if not events:
        raise RuntimeError("no delisting events cached: fetch first")
    bars, fund, btc = load_bars(events)
    grid = {}
    for d in GRID_DELAY:
        for sl in GRID_SLIP:
            log(f"simulating delay {d} h, slippage {sl}%")
            r = run_events(events, bars, fund, btc, d, sl, PRIMARY["hold_max_h"], PRIMARY["exit_buffer_h"])
            grid[f"delay{d}h_slip{sl}"] = {"hedged": summarize(r["trades"], "hedged"), "raw": summarize(r["trades"], "net"), "skipped": r["skipped"]}
            if d == PRIMARY["delay_h"] and sl == PRIMARY["slip_pct"]:
                primary_trades, primary_skipped = r["trades"], r["skipped"]
    p_key = f"delay{PRIMARY['delay_h']}h_slip{PRIMARY['slip_pct']}"
    prim = grid[p_key]["hedged"]
    v = verdict(prim)
    by_lead = {}
    for lo, hi in ((0, 1), (1, 2), (2, 4), (4, 30)):
        sub = [t for t in primary_trades if lo <= (t["effective_ms"] - t["published_ms"]) / (24 * HOUR) < hi]
        by_lead[f"lead {lo}-{hi}d"] = summarize(sub, "hedged")
    by_year = {}
    for t in primary_trades:
        by_year.setdefault(datetime.fromtimestamp(t["published_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m"), []).append(t)
    monthly = {k: {"events": len(v_), "mean_hedged_pct": round(float(np.mean([t["hedged"] for t in v_])) * 100, 3)} for k, v_ in sorted(by_year.items())}
    return {"type": "delist_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": p_key, "primary_params": PRIMARY, "pass_thresholds": PASS,
            "events": {"notices": len(events), "simulated": prim.get("events", 0), "skipped": primary_skipped, "since": SINCE}, "grid": grid, "by_lead_time": by_lead, "by_month": monthly, "limits": LIMITS,
            "rules": {"entry": f"open of the first hourly bar >= publish + {PRIMARY['delay_h']} h", "exit": f"earlier of entry + {PRIMARY['hold_max_h']} h, delisting - {PRIMARY['exit_buffer_h']} h, stop +{STOP_PCT}%", "fees_taker_pct": FEE_TAKER_PCT,
                      "slippage_round_trip_pct": PRIMARY["slip_pct"], "hedge": "long BTC, same notional and window", "baseline": f"{RANDOM_DRAWS} random shorts of the same length in the {PRE_DAYS} days before the notice"},
            "_trades": primary_trades}


LIMITS = ["Hourly bars hide intraday squeezes; the stop fills at its price or the bar's open (a gap is charged).",
          "Delisting coins are thin: the real slippage can exceed the 1.0% scenario; events with under 100k USDT traded in the 24h before the notice are skipped and counted.",
          "Bybit can halt trading or change margin on a delisting contract; borrow, margin calls and liquidation are not modelled.",
          "A notice published inside our entry delay, or with no room before the delisting, is skipped.",
          "A positive result is a hypothesis for a small real-money test, never `edge` by itself."]


def to_markdown(rep: dict) -> str:
    out = [f"# Delisting short study {rep['run_id']}", "", "**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small real-money test, not proof.**", "", f"## Verdict: `{rep['verdict']}`", "",
           ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "", f"Events: {rep['events']}", "", f"Primary: {rep['primary']} {rep['primary_params']}", "",
           "## Grid (entry delay x round-trip slippage)", "", "| scenario | basis | events | mean % | median % | win % | CI95 % | worst % | excess vs random % | percentile | 1st half | 2nd half | top5 share |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, g in rep["grid"].items():
        for basis in ("hedged", "raw"):
            s = g[basis]
            if not s.get("events"):
                out.append(f"| {k} | {basis} | 0 |  |  |  |  |  |  |  |  |  |  |")
                continue
            out.append(f"| {k} | {basis} | {s['events']} | {s['mean_pct']} | {s['median_pct']} | {s['win_pct']} | {s['ci95_pct']} | {s['worst_pct']} | {s['excess_mean_pct']} | {s['percentile_vs_random']} | {s['first_half_mean_pct']} | {s['second_half_mean_pct']} | {s['top5_share_of_total']} |")
    out += ["", "## By lead time (primary, hedged)", "", "```", json.dumps(rep["by_lead_time"], indent=1), "```", "", "## By month (primary, hedged)", "", "```", json.dumps(rep["by_month"], indent=1), "```", "", "## Rules", "", "```", json.dumps(rep["rules"], indent=1),
            "```", "", "## Pass thresholds (fixed before the run)", "", "```", json.dumps(rep["pass_thresholds"], indent=1), "```", "", "## Limits", ""]
    out += [f"- {x}" for x in rep["limits"]]
    return "\n".join(out) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    trades = rep.pop("_trades", [])
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["symbol", "published", "effective", "entry", "exit", "hours", "price_ret_pct", "funding_pct", "cost_pct", "net_pct", "hedged_pct", "baseline_hedged_pct", "reason"])
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M")  # noqa: E731
    for t in trades:
        cw.writerow([t["symbol"], iso(t["published_ms"]), iso(t["effective_ms"]), iso(t["entry_ts"]), iso(t["exit_ts"]), t["hours"], round(t["price_ret"] * 100, 4), round(t["funding"] * 100, 4), round(t["cost"] * 100, 4),
                     round(t["net"] * 100, 4), round(t["hedged"] * 100, 4), None if t["baseline_hedged"] is None else round(t["baseline_hedged"] * 100, 4), t["reason"]])
    (base / "events.csv").write_text(buf.getvalue(), encoding="utf-8")
