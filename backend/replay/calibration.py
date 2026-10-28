"""
Simulator calibration on the live bots' REAL decisions (no LLM, $0).

Goal: separate simulator error from model error. The simulator is fed the decisions a live bot really made (the `DeepSeek said ...` log lines, from the snapshot the
"Update live reference" button writes) and must reproduce the trades that bot really made. The strategy's own entry code, rotation exit (custom_exit), stake
sizing, ROI and stoploss run unchanged; only the model answer is replaced by the logged one.

What is compared: live trades that OPENED inside the window against replay trades that opened inside it (matched 1:1 by pair, side and open time, tolerance one
candle), plus the positions that were already open when the window starts (carried over with their real open time, rate and stake). Mechanics fixes are measured
one by one (ablation) and together; nothing here tunes a strategy parameter.
"""
from __future__ import annotations

import csv
import math
import io
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import median

from . import data as rdata
from . import engine, live_data, paths

ACCEPT_COUNT = 0.15              # trade count within +-15 %
ACCEPT_MEDIAN_ABS_PP = 0.3       # median |per-trade return difference| <= 0.3 pp
ACCEPT_MIX_TV = 0.20             # exit-reason mix: total-variation distance <= 0.2 (half the sum of share differences)
TOL_HOURS = 1                    # open-time tolerance of the 1:1 match: one candle
BOTS = [b for b, s in engine.BOTS.items() if s["default"]]       # the bots the replay engine knows by default
FIXES = ("decide_first", "entry_delay", "ft_stake", "restart_reset", "fine_prices", "fine_exits", "hour_latency", "exit_extra", "limit_fill", "poll_exits")


def _t(s):
    return live_data.utc(s)


def _hour(s):
    return live_data.floor_hour(_t(s))


# ------------------------------------------------------------------- windows
def window_for(entry: dict, date_from: str | None = None, date_to: str | None = None) -> dict | None:
    cov = live_data.coverage(entry)
    if not cov.get("available"):
        return None
    start, end = _t(cov["first_full_hour"]), _t(cov["last_hour"])
    if date_from:
        start = max(start, _t(date_from))
        start = live_data.floor_hour(start) + (timedelta(hours=1) if start != live_data.floor_hour(start) else timedelta(0))
    if date_to:
        end = min(end, _t(date_to) + timedelta(days=1) - timedelta(hours=1))
    if end < start:
        return None
    return {"start": start, "end": end, "coverage": cov}


def split_live_trades(trades: list[dict], start: datetime, end: datetime) -> tuple[list[dict], list[dict], list[dict], float]:
    """(carry-over: open at `start`, entries: opened inside the window, closed before the window, realised profit of those) -- from the live trades."""
    carry, entries, before, realised = [], [], [], 0.0
    stop = end + timedelta(hours=1)
    for tr in trades:
        if not tr.get("open_date") or not tr.get("pair"):
            continue                                           # a row without an open time or pair cannot be placed on the timeline
        od = _t(tr["open_date"])
        cd = _t(tr["close_date"]) if tr.get("close_date") else None
        if od < start:
            if tr.get("is_open") or cd is None or cd > start:
                carry.append(tr)
            else:
                before.append(tr)
                realised += float(tr.get("profit_abs") or 0.0)
        elif od < stop:
            entries.append(tr)
    return carry, entries, before, realised


# ----------------------------------------------------------------------- data
def load_store(snap: dict, bots: list[str], windows: dict, get, log=lambda s: None, fine: bool = True, funding: bool = True) -> tuple[rdata.SeriesStore, dict]:
    store = rdata.SeriesStore()
    pairs_by_bot: dict[str, list[str]] = {}
    need: dict[tuple[str, str], tuple[datetime, datetime]] = {}
    need_fine: dict[tuple[str, str], tuple[datetime, datetime]] = {}
    for bot in bots:
        w = windows[bot]
        cat = engine.BOTS[bot]["category"]
        entry = snap[bot]
        names = list(dict.fromkeys([d["pair"] for d in entry["decisions"]] + [t["pair"] for t in entry["trades"]]))
        pairs_by_bot[bot] = names
        lo, hi = w["start"] - timedelta(days=engine.WARMUP_DAYS), w["end"] + timedelta(hours=26)
        for pair in names:
            k = (cat, rdata.bybit_symbol(pair))
            a, b = need.get(k, (lo, hi))
            need[k] = (min(a, lo), max(b, hi))
            fa, fb = need_fine.get(k, (w["start"] - timedelta(hours=1), w["end"] + timedelta(hours=2)))
            need_fine[k] = (min(fa, w["start"] - timedelta(hours=1)), max(fb, w["end"] + timedelta(hours=2)))
    for i, ((cat, sym), (lo, hi)) in enumerate(sorted(need.items())):
        try:
            store.add(cat, sym, rdata.load_candles(cat, sym, lo, hi, get))
        except Exception as e:      # noqa: BLE001
            log(f"candles {cat} {sym}: FAILED ({type(e).__name__}: {e}) -- the pair is skipped")
            store.add(cat, sym, rdata.to_frame([]))
            continue
        log(f"candles {cat} {sym}: {len(store.frames[(cat, sym)])} hourly ({i + 1}/{len(need)})")
    if funding:                   # the live shorts bots read the perpetual funding rate (prompt line, confirm_trade_entry veto): serve it AS OF the clock so the replay sees the same veto
        for (cat, sym), (lo, hi) in sorted(need.items()):
            if cat == "linear" and store.has(cat, sym):
                rdata.add_funding_to(store, sym, lo, hi, get, log)
    for cat in sorted({engine.BOTS[b]["category"] for b in bots}):
        try:
            for sym, sp in rdata.fetch_spreads(cat, get).items():
                store.spreads[(cat, sym)] = sp
        except Exception as e:      # noqa: BLE001
            log(f"spreads {cat}: unavailable ({type(e).__name__}) -- entries fill at the minute price")
    if fine:                      # one-minute candles: the price at the exact moment of an entry / exit (the entry trails the candle open by minutes, hourly figures cannot see that)
        for i, ((cat, sym), (lo, hi)) in enumerate(sorted(need_fine.items())):
            try:
                store.add_fine(cat, sym, rdata.load_candles(cat, sym, lo, hi, get, interval="1"))
            except Exception as e:      # noqa: BLE001
                log(f"1m candles {cat} {sym}: FAILED ({type(e).__name__}: {e}) -- that pair falls back to hourly prices")
                continue
            n_min = len(store.fine[(cat, sym)]["ns"]) if (cat, sym) in store.fine else 0
            log(f"candles1m {cat} {sym}: {n_min} ({i + 1}/{len(need_fine)})")
    return store, pairs_by_bot


# ------------------------------------------------------------------- matching
def tv_distance(a: dict, b: dict) -> float | None:
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return None
    keys = set(a) | set(b)
    return round(0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in keys), 4)


def match_trades(live: list[dict], rep: list[dict]) -> dict:
    """Greedy 1:1 match of live trades to replay trades: same pair and side, open hour within one candle, nearest open time first. A carried-over position
    matches its own replay twin (live_id)."""
    used: set[int] = set()
    pairs: list[tuple[dict, dict]] = []
    only_live: list[dict] = []
    by_live_id = {r["live_id"]: i for i, r in enumerate(rep) if r.get("carry_over") and r.get("live_id") is not None}
    for lt in sorted(live, key=lambda x: x["open_date"]):
        j = by_live_id.get(lt.get("id")) if lt.get("id") is not None else None
        if j is not None and j not in used:
            used.add(j)
            pairs.append((lt, rep[j]))
            continue
        lo = _t(lt["open_date"])
        best, best_d = None, None
        for k, r in enumerate(rep):
            if k in used or r.get("carry_over") or r["pair"] != lt["pair"] or r["side"] != lt["side"]:
                continue
            d = abs((_t(r["open_date"]) - lo).total_seconds())
            if abs((_hour(r["open_date"]) - _hour(lt["open_date"])).total_seconds()) <= TOL_HOURS * 3600 and (best_d is None or d < best_d):
                best, best_d = k, d
        if best is None:
            only_live.append(lt)
        else:
            used.add(best)
            pairs.append((lt, rep[best]))
    only_rep = [r for k, r in enumerate(rep) if k not in used]
    return {"pairs": pairs, "only_live": only_live, "only_replay": only_rep}


def _pct(x):
    return None if x is None else round(x * 100, 4)


def _med(xs):
    return round(float(median(xs)), 4) if xs else None


def _closed(t):
    return not t.get("is_open") and t.get("close_date") and t.get("profit_ratio") is not None


def exclude_hours(trades: list[dict], hours: set[str]) -> list[dict]:
    if not hours:
        return trades
    out = []
    for t in trades:
        oh = live_data.iso(_hour(t["open_date"]))
        ch = live_data.iso(_hour(t["close_date"])) if t.get("close_date") else None
        if oh in hours or ch in hours:
            continue
        out.append(t)
    return out


TRIM_SHARE = 0.05                 # the outlier-trimmed row drops the 5% most extreme per-trade differences (and the 5% tails of the mean): fixed in advance
THIN_SPREAD_PCT = 0.2             # a pair is THIN when its relative bid-ask spread is at least 0.2% (today's tickers), or when the live log shows >= 2 cancelled orders on it
THIN_CANCELS = 2
MIN_STAKE_TOL = 0.06              # an unnamed "minimum stake > balance" figure belongs to a pair when it is within 6% of that pair's minimum stake seen in any bot's log within 36 hours (it follows the price)


def trimmed_mean(xs: list[float], share: float = TRIM_SHARE) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = int(len(s) * share)
    core = s[k:len(s) - k] if len(s) - 2 * k > 0 else s
    return sum(core) / len(core)


def trimmed_block(live_closed: list[dict], rep_closed: list[dict], diffs: list[float]) -> dict:
    """The same comparison without the outliers: drop the ceil(5%) matched pairs with the largest |return difference| (a stop that filled at a gap on one side only moves the whole row),
    and compare the 5%-trimmed mean per trade of each side."""
    keep = sorted(diffs, key=abs)
    keep = keep[:len(keep) - math.ceil(len(keep) * TRIM_SHARE)] if keep else []
    ab = [abs(x) for x in keep]
    return {"share": TRIM_SHARE, "matched_pairs_kept": len(keep), "matched_pairs_dropped": len(diffs) - len(keep),
            "return_diff_pp": {"median_abs": _med(ab), "mean_abs": None if not ab else round(sum(ab) / len(ab), 4)},
            "mean_trade_pct": {"live": _pct(trimmed_mean([t["profit_ratio"] for t in live_closed])), "replay": _pct(trimmed_mean([t["profit_ratio"] for t in rep_closed]))},
            "accepted_median_abs_return_diff": None if not ab else _med(ab) <= ACCEPT_MEDIAN_ABS_PP}


def thin_pairs(entry: dict, store, bot: str) -> dict[str, dict]:
    """Pairs on which a passive limit order can fail: wide spread, or cancelled orders in the live log. {pair: {spread_pct, cancels, why}}."""
    cat = engine.BOTS[bot]["category"]
    cancels = Counter(o["pair"] for o in (entry.get("order_cancels") or []))
    pairs = {t["pair"] for t in entry["trades"]} | set(cancels) | {d["pair"] for d in entry["decisions"]}
    out = {}
    for p in sorted(pairs):
        sp = store.spread(cat, rdata.bybit_symbol(p)) * 100
        why = [x for x, ok in ((f"spread {sp:.2f}% >= {THIN_SPREAD_PCT}%", sp >= THIN_SPREAD_PCT), (f"{cancels[p]} cancelled orders", cancels[p] >= THIN_CANCELS)) if ok]
        if why:
            out[p] = {"spread_pct": round(sp, 3), "cancels": cancels[p], "why": "; ".join(why)}
    return out


def min_stakes_of(snap: dict) -> dict[str, list[tuple[datetime, float]]]:
    """pair -> [(time, minimum stake)] from every bot's `too small` log lines: the exchange minimum is the same for every bot, so another bot's line names the pair for an unnamed figure."""
    out: dict[str, list] = {}
    for entry in snap.values():
        for b in entry.get("entry_blocks") or []:
            if b["kind"] == "min_order":
                out.setdefault(b["pair"], []).append((_t(b["ts"]), float(b["min"])))
    return out


def metrics(live_entries: list[dict], rep_entries: list[dict], carry_pairs: list[tuple[dict, dict]], attempts: list[dict], live_all: list[dict], max_open: int,
            outage: set[str], cancels: list[dict] | None = None, blocks: list[dict] | None = None, min_stakes: dict | None = None) -> dict:
    """Everything the spec's section B asks for, for one bot and one variant."""
    m = match_trades(live_entries, rep_entries)
    pairs = m["pairs"] + carry_pairs
    n_live, n_rep = len(live_entries), len(rep_entries)
    entry_diff = [(_t(r["open_date"]) - _t(lt["open_date"])).total_seconds() / 60 for lt, r in m["pairs"]]
    price_in = [abs(r["open_rate"] / lt["open_rate"] - 1) * 100 for lt, r in pairs if lt.get("open_rate") and r.get("open_rate")]
    both = [(lt, r) for lt, r in pairs if _closed(lt) and _closed(r)]
    price_out = [abs(r["close_rate"] / lt["close_rate"] - 1) * 100 for lt, r in both if lt.get("close_rate") and r.get("close_rate")]
    diffs = [(r["profit_ratio"] - lt["profit_ratio"]) * 100 for lt, r in both]
    agree = sum(1 for lt, r in both if lt.get("exit_reason") == r.get("exit_reason"))
    live_closed = [t for t in live_entries if _closed(t)]
    rep_closed = [t for t in rep_entries if _closed(t)]
    mix_live = dict(Counter(t.get("exit_reason") or "?" for t in live_closed))
    mix_rep = dict(Counter(t.get("exit_reason") or "?" for t in rep_closed))
    tv = tv_distance(mix_live, mix_rep)
    stake_ratio = [r["stake"] / lt["stake"] for lt, r in pairs if lt.get("stake") and r.get("stake")]
    hold_diff = [((_t(r["close_date"]) - _t(r["open_date"])) - (_t(lt["close_date"]) - _t(lt["open_date"]))).total_seconds() / 3600 for lt, r in both]
    ratio = None if not n_live else round(n_rep / n_live, 3)
    mad = _med([abs(x) for x in diffs])
    out = {"n_live_entries": n_live, "n_replay_entries": n_rep, "count_ratio": ratio, "matched": len(m["pairs"]), "matched_share_of_live": None if not n_live else round(len(m["pairs"]) / n_live, 3),
           "carry_over_matched": len(carry_pairs), "only_live": len(m["only_live"]), "only_replay": len(m["only_replay"]),
           "entry_time_diff_min": {"median": _med(entry_diff), "median_abs": _med([abs(x) for x in entry_diff]), "p90_abs": None if not entry_diff else round(float(sorted(abs(x) for x in entry_diff)[int(len(entry_diff) * 0.9)]), 2)},
           "entry_price_diff_pct": {"median_abs": _med(price_in)}, "exit_price_diff_pct": {"median_abs": _med(price_out)},
           "both_closed": len(both), "exit_reason_agreement": None if not both else round(agree / len(both), 3),
           "return_diff_pp": {"median_signed": _med(diffs), "median_abs": mad, "p10": None if len(diffs) < 5 else round(float(sorted(diffs)[int(len(diffs) * 0.1)]), 3),
                              "p90": None if len(diffs) < 5 else round(float(sorted(diffs)[int(len(diffs) * 0.9)]), 3)},
           "hold_hours_diff_median": _med(hold_diff), "stake_ratio_median": _med(stake_ratio),
           "mean_trade_pct": {"live": _pct(sum(t["profit_ratio"] for t in live_closed) / len(live_closed)) if live_closed else None,
                              "replay": _pct(sum(t["profit_ratio"] for t in rep_closed) / len(rep_closed)) if rep_closed else None},
           "exit_mix": {"live": mix_live, "replay": mix_rep, "tv_distance": tv}}
    out["acceptance"] = {"count_within_15pct": None if ratio is None else abs(ratio - 1) <= ACCEPT_COUNT,
                         "median_abs_return_diff_le_0_3pp": None if mad is None else mad <= ACCEPT_MEDIAN_ABS_PP,
                         "exit_mix_not_materially_different": None if tv is None else tv <= ACCEPT_MIX_TV}
    out["accepted"] = all(v is True for v in out["acceptance"].values())
    out["causes"] = causes(m["only_live"], m["only_replay"], attempts, live_all, max_open, cancels, blocks, min_stakes)
    out["trimmed"] = trimmed_block(live_closed, rep_closed, diffs)
    return out


def causes(only_live: list[dict], only_replay: list[dict], attempts: list[dict], live_all: list[dict], max_open: int, cancels: list[dict] | None = None, blocks: list[dict] | None = None,
           min_stakes: dict | None = None) -> dict:
    """Why a trade exists on one side only. Live-only: what the replay did with that pair around the live entry. Replay-only: the live bot's own state then."""
    by_pair: dict[str, list[dict]] = {}
    for a in attempts:
        by_pair.setdefault(a["pair"], []).append(a)
    lo_c: Counter = Counter()
    lo_examples = []
    for lt in only_live:
        h = _hour(lt["open_date"])
        near = [a for a in by_pair.get(lt["pair"], []) if abs((_t(a["t"]) - h).total_seconds()) <= TOL_HOURS * 3600]
        label = "replay_no_entry_signal" if not near else "replay_" + sorted({a["result"] for a in near})[0] if all(a["result"] != "opened" for a in near) else "replay_opened_but_not_matched"
        lo_c[label] += 1
        if len(lo_examples) < 6:
            lo_examples.append({"pair": lt["pair"], "open": lt["open_date"], "why": label, "attempts": near[:2]})

    def live_open_at(t: datetime) -> list[dict]:
        out = []
        for x in live_all:
            xo = _t(x["open_date"])
            xc = _t(x["close_date"]) if x.get("close_date") else None
            if xo <= t and (x.get("is_open") or xc is None or xc > t):
                out.append(x)
        return out

    ro_c: Counter = Counter()
    ro_examples = []
    for r in only_replay:
        ot = _t(r["open_date"])
        open_live = live_open_at(ot)
        h0 = _hour(r["open_date"])
        if any(o["kind"] == "entry_cancel" and o["pair"] == r["pair"] and h0 <= _t(o["ts"]) < h0 + timedelta(hours=1, minutes=11) for o in (cancels or [])):
            label = "live_entry_order_timed_out"             # measured in the live log: the passive limit order was cancelled after 10 minutes unfilled
        elif any(x["pair"] == r["pair"] for x in open_live):
            label = "live_pair_already_open"
        elif len(open_live) >= max_open:
            label = "live_slots_full"
        else:
            label = entry_block_label(r["pair"], h0, blocks or [], min_stakes or {}) or "live_no_entry_unexplained"
        ro_c[label] += 1
        if len(ro_examples) < 6:
            ro_examples.append({"pair": r["pair"], "open": r["open_date"], "why": label, "live_open_then": len(open_live)})
    return {"only_live": dict(lo_c), "only_replay": dict(ro_c), "examples": {"only_live": lo_examples, "only_replay": ro_examples}}


def entry_block_label(pair: str, h0: datetime, blocks: list[dict], min_stakes: dict) -> str | None:
    """The reason the LIVE log gives for not opening an entry the replay opened: a strategy veto, the exchange's minimum order size, Freqtrade's pair lock. None when the log says nothing
    (the entry stays `live_no_entry_unexplained`)."""
    win = [b for b in blocks if h0 <= _t(b["ts"]) < h0 + timedelta(hours=1, minutes=11)]
    if any(b["kind"] == "veto" and b["pair"] == pair for b in win):
        return "live_strategy_veto"                       # confirm_trade_entry (funding below the threshold / loss-streak cooldown): replay cannot see funding offline
    if any(b["kind"] == "locked" and b["pair"] == pair for b in win):
        return "live_pair_locked"                         # Freqtrade's Auto lock after an exit in the same candle
    if any(b["kind"] == "min_order" and b["pair"] == pair for b in win):
        return "live_min_order_size"
    known = [m for t, m in min_stakes.get(pair, []) if abs((t - h0).total_seconds()) <= 36 * 3600]
    if known and any(b["kind"] == "min_balance" and any(abs(b["min"] / m - 1) <= MIN_STAKE_TOL for m in known) for b in win):
        return "live_min_order_size"
    if not known and any(b["kind"] == "min_balance" for b in win):
        return "live_min_order_size_probable"             # the log shows a minimum-stake-above-balance warning in that hour but no figure names this pair: probable, not proven
    return None


# ------------------------------------------------------------------- the run
def measured_delay(entry: dict, w: dict) -> float:
    """The loop latency the simulator uses: the MEDIAN seconds after the full hour at which the live bot opened trades and made its custom (rotation / trailing) exits inside
    the window, offsets above 10 minutes (restart-hour oddities) dropped. A measured fact, not a tuned number."""
    carry, entries, _, _ = split_live_trades(entry["trades"], w["start"], w["end"])
    offs = []
    for t in entries + carry:
        for key in ("open_date", "close_date"):
            if key == "open_date" and t in carry:
                continue
            if key == "close_date" and (not t.get("close_date") or (t.get("exit_reason") or "") in ("stop_loss", "roi", "stoploss_on_exchange", "trailing_stop_loss")):
                continue
            d = _t(t[key])
            if d is None or not (w["start"] <= d <= w["end"] + timedelta(hours=1)):
                continue
            s = (d - live_data.floor_hour(d)).total_seconds()
            if s <= 600:
                offs.append(s)
    if offs:
        offs.sort()
        return float(offs[len(offs) // 2])
    return max(0.0, min(float((entry.get("entry_delay_s") or {}).get("median") or 0.0), 600.0))


def measured_exit_extra(entry: dict, w: dict) -> float:
    """Seconds after the end of the hour's analysis at which the live bot's custom (rotation / trailing) exits happened: the median over the window, capped at 5 minutes."""
    live = engine.LiveDecisions(entry["decisions"], entry["restarts"])
    xs = []
    for t in entry["trades"]:
        if not t.get("close_date") or (t.get("exit_reason") or "") in ("stop_loss", "roi", "stoploss_on_exchange", "trailing_stop_loss"):
            continue
        d = _t(t["close_date"])
        if not (w["start"] <= d <= w["end"] + timedelta(hours=1)):
            continue
        lat = live.latency(live_data.floor_hour(d))
        s = (d - live_data.floor_hour(d)).total_seconds()
        if lat is not None and 0 <= s - lat <= 300:
            xs.append(s - lat)
    xs.sort()
    return float(xs[len(xs) // 2]) if xs else 0.0


def variant_mech(name: str, measured_delay: float, exit_extra: float = 0.0) -> dict:
    """legacy | each single fix on top of legacy | all fixes (live_like)."""
    base = dict(engine.MECH_LEGACY)
    if name == "legacy":
        return {**base, "name": "legacy"}
    full = {**engine.MECH_LIVE_LIKE, "entry_delay_s": measured_delay, "exit_extra_s": exit_extra}
    if name == "live_like":
        return {**full, "name": "live_like"}
    if name == "live_like_close":
        return {**full, "fine_fill": "close", "name": "live_like_close"}
    one = {"decide_first": {"decide_first": True}, "entry_delay": {"entry_delay_s": measured_delay}, "ft_stake": {"ft_stake": True}, "restart_reset": {"restart_reset": True},
           "fine_prices": {"fine_prices": True}, "fine_exits": {"fine_exits": True}, "hour_latency": {"hour_latency": True}, "limit_fill": {"fine_prices": True, "limit_fill": True}, "poll_exits": {"fine_prices": True, "poll_exits": True}, "exit_extra": {"hour_latency": True, "exit_extra_s": exit_extra}, "fine_exits_close": {"fine_exits": True, "fine_fill": "close"}}[name]
    return {**base, **one, "name": f"legacy+{name}"}


CAL_SLIPPAGE_PCT = 0.0           # live dry-run fills at order-book prices with no extra slippage; the measured entry-price bias of the replay is the slippage it was given


def run_bot(bot: str, entry: dict, w: dict, store: rdata.SeriesStore, names: list[str], mech: dict, run_id: str, log=lambda s: None, stop_event=None,
            slippage_pct: float = CAL_SLIPPAGE_PCT) -> dict:
    carry, entries, before, realised = split_live_trades(entry["trades"], w["start"], w["end"])
    cfg = engine.bot_config(engine.BOTS[bot])
    wallet = float(cfg.get("dry_run_wallet", 500)) + realised - sum(float(c["stake"] or 0.0) for c in carry)
    live = engine.LiveDecisions(entry["decisions"], entry["restarts"])
    run = engine.BotRun(bot, "calibration", {"mech": mech, "fee_pct": 0.1, "slippage_pct": slippage_pct}, store, None, None, names, run_id, log=log, stop_event=stop_event,
                        live=live, carry=carry, max_open=entry.get("max_open_trades"), cash0=wallet)
    res = run.run(w["start"] - timedelta(hours=1), w["end"])
    res["carry_in"] = len(carry)
    res["live_entries"], res["live_carry"], res["wallet_at_start"] = entries, carry, round(wallet, 2)
    return res


def evaluate(bot: str, entry: dict, w: dict, res: dict, outage: set[str], min_stakes: dict | None = None, thin: dict | None = None) -> dict:
    live_all = entry["trades"]
    carry_by_id = {c.get("id"): c for c in res["live_carry"]}
    rep_all = res["trades"] + res["open_trades"]
    rep_carry = [r for r in rep_all if r.get("carry_over")]
    rep_entries = [r for r in rep_all if not r.get("carry_over")]
    carry_pairs = [(carry_by_id[r["live_id"]], r) for r in rep_carry if r.get("live_id") in carry_by_id]
    cancels = entry.get("order_cancels") or []
    blocks = entry.get("entry_blocks") or []
    full = metrics(res["live_entries"], rep_entries, carry_pairs, res["attempts"], live_all, res["max_open_trades"], outage, cancels, blocks, min_stakes)
    clean_live = exclude_hours(res["live_entries"], outage)
    clean_rep = exclude_hours(rep_entries, outage)
    clean = metrics(clean_live, clean_rep, [], res["attempts"], live_all, res["max_open_trades"], outage, cancels, blocks, min_stakes) if outage else None
    thin_out = None
    if thin:
        tl = [t for t in res["live_entries"] if t["pair"] not in thin]
        tr_ = [t for t in rep_entries if t["pair"] not in thin]
        thin_out = {"pairs": thin, "live_entries_on_thin_pairs": len(res["live_entries"]) - len(tl), "replay_entries_on_thin_pairs": len(rep_entries) - len(tr_),
                    "rule": f"spread >= {THIN_SPREAD_PCT}% (today's tickers) or >= {THIN_CANCELS} cancelled orders in the live log",
                    "metrics_excluding_thin_pairs": metrics(tl, tr_, [], res["attempts"], live_all, res["max_open_trades"], outage, cancels, blocks, min_stakes)}
    return {"window": {"start": live_data.iso(w["start"]), "end": live_data.iso(w["end"])}, "max_open_trades": res["max_open_trades"], "wallet_at_start": res["wallet_at_start"],
            "carry_in": res["carry_in"], "mechanics": res["mechanics"], "full": full, "excluding_outage_hours": clean, "thin_pairs": thin_out,
            "replay_exit_reasons": res["exit_counts"], "open_at_end": res["open_at_end"], "n_decisions": len(res["decisions"]), "_rep_entries": rep_entries}


def build_report(run_id: str, snap: dict, bots: list[str], windows: dict, variants: dict, snapshot_meta: dict, limits: list[str]) -> dict:
    """variants: name -> {bot: evaluate(...)}. `live_like` is the 'after', `legacy` the 'before'."""
    cov = {b: windows[b]["coverage"] for b in bots}
    out_v: dict = {}
    for name, per_bot in variants.items():
        out_v[name] = {b: {k: v for k, v in ev.items() if not k.startswith("_")} for b, ev in per_bot.items()}

    def pick(name, bot, key="full"):
        return ((variants.get(name) or {}).get(bot) or {}).get(key)

    ablation = {}
    for bot in bots:
        row = {}
        for name in variants:
            f = pick(name, bot)
            if f:
                row[name] = {"count_ratio": f["count_ratio"], "matched_share_of_live": f["matched_share_of_live"], "median_abs_return_diff_pp": f["return_diff_pp"]["median_abs"],
                             "exit_reason_agreement": f["exit_reason_agreement"], "exit_mix_tv": f["exit_mix"]["tv_distance"], "accepted": f["accepted"]}
        ablation[bot] = row
    return {"run_id": run_id, "type": "calibration", "snapshot": snapshot_meta, "bots": bots, "coverage": cov, "variants": out_v, "ablation": ablation,
            "before": "legacy", "after": "live_like", "acceptance_rules": {"trade_count": f"within +-{int(ACCEPT_COUNT * 100)}% of live", "median_abs_return_diff_pp": ACCEPT_MEDIAN_ABS_PP,
                                                                       "exit_mix_tv_distance": ACCEPT_MIX_TV, "match": f"1:1 by pair + side, open time within {TOL_HOURS} candle"},
            "limits": limits}


LIMITS = [
    "decisions come from the bots' own log files: they cover only the period since the logs were last rotated / the containers recreated; a bot restart can log one hour twice",
    "a failed LLM call is logged as a 'hold' (\"no LLM response\"): during an outage the live bots only held, and rotation closed their positions -- the report shows the outage hours and "
    "a second set of numbers with trades opened or closed in them excluded",
    "the live stake was scaled by the allocator's weight (enforce); the simulator uses weight 1.0, so stake sizes (not entries or exits) differ -- see stake_ratio_median",
    "ROI / stoploss are checked on hourly candle high/low in the simulator, continuously (ticker) in the live bot; custom_exit once per candle in both",
    "positions open when the window starts are carried over with their real open time, rate and stake; the strategy's in-memory stale-signal streak is not recoverable",
    "the pair list per hour is the set of pairs the live bot logged a decision for in that hour (log order = entry order)",
    "no strategy parameter is changed anywhere in this run; mechanics fixes are listed in `variants` with before / after numbers",
    "replay-only entries are labelled from the live log: `live_strategy_veto` (confirm_trade_entry: funding veto / loss-streak cooldown -- the replay cannot fetch funding), `live_min_order_size` "
    "(the exchange's minimum order size is larger than the stake the small wallet allows), `live_pair_locked` (Freqtrade's Auto lock after an exit); the replay does NOT model these, they are only explained",
    "no evidence was found for an allocator veto: the allocator only scales the stake (custom_stake_amount), it never blocks an entry",
    "the spread used for the thin-pair flag is today's, not the spread of the day the trade happened",
]


def to_markdown(rep: dict) -> str:
    L = [f"# Calibration {rep['run_id']}", "", "Simulator fed with the live bots' real decisions (no LLM, $0). before = legacy mechanics, after = live-like mechanics.", ""]
    L += ["## Coverage of the live decisions", "", "| bot | first full hour | last hour | decisions | hours covered / span | pairs | restarts | outage hours |", "|---|---|---|---|---|---|---|---|"]
    for b in rep["bots"]:
        c = rep["coverage"][b]
        L.append(f"| {b} | {c['first_full_hour']} | {c['last_hour']} | {c['n_decisions']} | {c['hours_with_decisions']}/{c['hours_in_span']} | {c['n_pairs']} | {len(c['restarts'])} | {len(c['outage_hours'])} |")
    for label, key in (("after (live-like mechanics)", "live_like"), ("before (legacy mechanics)", "legacy")):
        v = rep["variants"].get(key)
        if not v:
            continue
        L += ["", f"## {label}", "", "| bot | live entries | replay entries | ratio | matched | entry dt min (med abs) | entry px diff % | exit px diff % | exit reason agree | return diff pp (med abs) | mean/trade live -> replay | accepted |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for b in rep["bots"]:
            f = v[b]["full"]
            L.append(f"| {b} | {f['n_live_entries']} | {f['n_replay_entries']} | {f['count_ratio']} | {f['matched']} ({f['matched_share_of_live']}) | {f['entry_time_diff_min']['median_abs']} | "
                     f"{f['entry_price_diff_pct']['median_abs']} | {f['exit_price_diff_pct']['median_abs']} | {f['exit_reason_agreement']} | {f['return_diff_pp']['median_abs']} | "
                     f"{f['mean_trade_pct']['live']} -> {f['mean_trade_pct']['replay']} | {f['accepted']} |")
    L += ["", "## Ablation: each mechanics fix alone", "", "| bot | variant | count ratio | matched share | median abs return diff pp | exit agreement | exit-mix TV | accepted |", "|---|---|---|---|---|---|---|---|"]
    for b, row in rep["ablation"].items():
        for name, x in row.items():
            L.append(f"| {b} | {name} | {x['count_ratio']} | {x['matched_share_of_live']} | {x['median_abs_return_diff_pp']} | {x['exit_reason_agreement']} | {x['exit_mix_tv']} | {x['accepted']} |")
    v = rep["variants"].get("live_like")
    if v:
        L += ["", "## after, outlier-trimmed (the 5% matched pairs with the largest |return difference| dropped; 5%-trimmed mean per trade)", "",
              "| bot | pairs kept / dropped | return diff pp (med abs) | mean abs diff pp | trimmed mean/trade live -> replay | accepted on the trimmed median |", "|---|---|---|---|---|---|"]
        for b in rep["bots"]:
            t = v[b]["full"].get("trimmed")
            if t:
                L.append(f"| {b} | {t['matched_pairs_kept']} / {t['matched_pairs_dropped']} | {t['return_diff_pp']['median_abs']} | {t['return_diff_pp']['mean_abs']} | {t['mean_trade_pct']['live']} -> {t['mean_trade_pct']['replay']} | {t['accepted_median_abs_return_diff']} |")
        L += ["", "## after, thin pairs flagged", "", f"Thin = relative spread >= {THIN_SPREAD_PCT}% (today's tickers) or >= {THIN_CANCELS} cancelled orders in the live log. The row compares live and replay WITHOUT trades on thin pairs.", "",
              "| bot | thin pairs | live entries on them | replay entries on them | ratio without them | median abs return diff pp without them | accepted without them |", "|---|---|---|---|---|---|---|"]
        for b in rep["bots"]:
            tp = v[b].get("thin_pairs")
            if tp:
                f = tp["metrics_excluding_thin_pairs"]
                L.append(f"| {b} | {', '.join(sorted(tp['pairs'])) or '-'} | {tp['live_entries_on_thin_pairs']} | {tp['replay_entries_on_thin_pairs']} | {f['count_ratio']} | {f['return_diff_pp']['median_abs']} | {f['accepted']} |")
            else:
                L.append(f"| {b} | none | 0 | 0 | | | |")
        L += ["", "## Divergence causes (after)", ""]
        for b in rep["bots"]:
            c = v[b]["full"]["causes"]
            L.append(f"- {b}: only in live {c['only_live']}; only in replay {c['only_replay']}")
    L += ["", "## Limits", ""] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, rep: dict, variants: dict, params: dict) -> None:
    def w(name, text):
        paths.ensure_parent(paths.replay_path(run_id, name)).write_text(text, encoding="utf-8")
    w("params.json", json.dumps(params, indent=1, default=str))
    w("report.json", json.dumps(rep, indent=1, default=str))
    w("report.md", to_markdown(rep))
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["variant", "bot", "kind", "pair", "side", "live_open", "replay_open", "live_close", "replay_close", "live_exit", "replay_exit", "live_ratio", "replay_ratio"])
    for name, per_bot in variants.items():
        for bot, ev in per_bot.items():
            m = match_trades(ev.get("_live_entries", []), ev["_rep_entries"])
            for lt, r in m["pairs"]:
                cw.writerow([name, bot, "matched", lt["pair"], lt["side"], lt["open_date"], r["open_date"], lt.get("close_date"), r.get("close_date"), lt.get("exit_reason"), r.get("exit_reason"),
                             lt.get("profit_ratio"), r.get("profit_ratio")])
            for lt in m["only_live"]:
                cw.writerow([name, bot, "only_live", lt["pair"], lt["side"], lt["open_date"], "", lt.get("close_date"), "", lt.get("exit_reason"), "", lt.get("profit_ratio"), ""])
            for r in m["only_replay"]:
                cw.writerow([name, bot, "only_replay", r["pair"], r["side"], "", r["open_date"], "", r.get("close_date"), "", r.get("exit_reason"), "", r.get("profit_ratio")])
    w("calibration_trades.csv", buf.getvalue())


def run_calibration(snap: dict, snapshot_meta: dict, bots: list[str], date_from, date_to, get, run_id: str, mechs: list[str] | None = None, log=lambda s: None,
                    progress=lambda **k: None, stop_event=None) -> dict:
    """The whole calibration: candles -> every variant on every bot -> report. `get` is the HTTP getter for Bybit klines (the only network use)."""
    bots = [b for b in bots if b in snap and b in BOTS]
    windows = {}
    for b in bots:
        w = window_for(snap[b], date_from, date_to)
        if w:
            windows[b] = w
    bots = [b for b in bots if b in windows]
    if not bots:
        raise ValueError("no bot has decisions in the snapshot (press 'Update live reference' first, or the window is empty)")
    store, pairs_by_bot = load_store(snap, bots, windows, get, log)
    min_map = min_stakes_of(snap)
    thin_by_bot = {b: thin_pairs(snap[b], store, b) for b in bots}
    variants: dict = {}
    names = mechs or ["legacy", "decide_first", "entry_delay", "ft_stake", "restart_reset", "fine_prices", "fine_exits", "hour_latency", "exit_extra", "limit_fill", "poll_exits", "live_like"]
    total = len(names) * len(bots)
    done = 0
    for name in names:
        variants[name] = {}
        for bot in bots:
            entry = snap[bot]
            delay = measured_delay(entry, windows[bot])
            res = run_bot(bot, entry, windows[bot], store, pairs_by_bot[bot], variant_mech(name, delay, measured_exit_extra(entry, windows[bot])), run_id, log=lambda s: None, stop_event=stop_event)
            ev = evaluate(bot, entry, windows[bot], res, set(entry.get("outage_hours") or []), min_map, thin_by_bot[bot])
            ev["_live_entries"] = res["live_entries"]
            variants[name][bot] = ev
            done += 1
            log(f"calibration {name}/{bot}: live {ev['full']['n_live_entries']} replay {ev['full']['n_replay_entries']} matched {ev['full']['matched']} accepted={ev['full']['accepted']}")
            progress(bot=bot, mode=name, step=done, total=total, decisions=ev["n_decisions"], trades=ev["full"]["n_replay_entries"])
    rep = build_report(run_id, snap, bots, windows, variants, snapshot_meta, LIMITS)
    return {"report": rep, "variants": variants}
