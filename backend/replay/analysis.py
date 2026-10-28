"""
Replay analysis: statistics per bot, the two in-app baselines (BTC buy-and-hold, random entries with the bot's own exit constants), the P19 ledger audit on
the replay decisions (look-ahead-safe forward returns from the LOCAL candles), the named-vs-anonymized comparison and the divergence from the live bots.

Status policy (hard): replay NEVER returns `edge`. The best possible label is "unproven (replay)"; `edge` is reserved for live data.
"""
from __future__ import annotations

import json
import random
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend.engine import evidence as ev
from backend.engine import signal_audit as sa

from . import data as rdata

BANNER = ("Historical replay. A negative result is a strong argument against a bot; a positive one is weak. "
          "The status `edge` is only ever given from live data.")
BANNER_EN = ("Historical replay. A negative result is a strong argument against a bot; a positive one is weak (no slippage or real execution, possible knowledge leakage "
             "of the model). The status `edge` is only ever given from live data.")
NEVER = "edge"


def trade_stats(trades: list[dict], wallet0: float, equity: list[tuple[str, float]]) -> dict:
    rets = [t["profit_ratio"] for t in trades]
    n = len(rets)
    net = sum(t["profit_abs"] for t in trades)
    peak, mdd = wallet0, 0.0
    for _, e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    ci = ev.bootstrap_ci_mean(rets) if n >= 2 else None
    return {"n_trades": n, "win_pct": round(sum(1 for r in rets if r > 0) / n * 100, 1) if n else None,
            "mean_trade_pct": round(sum(rets) / n * 100, 4) if n else None,
            "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
            "fees_usd": round(sum(t["fees_abs"] for t in trades), 3), "net_usd": round(net, 3), "net_return_pct": round(net / wallet0 * 100, 3) if wallet0 else None,
            "max_drawdown_pct": round(mdd * 100, 3), "end_equity": round(equity[-1][1], 2) if equity else wallet0,
            "avg_hold_hours": round(sum((pd.Timestamp(t["close_date"]) - pd.Timestamp(t["open_date"])).total_seconds() for t in trades) / n / 3600, 1) if n else None}


def replay_status(stats: dict) -> str:
    """no_data | no_edge (replay) | unproven (replay). By construction never 'edge': the comparisons that edge needs are passed as None."""
    n = stats["n_trades"]
    ci = None if stats["ci95_pct"] is None else {"mean": (stats["mean_trade_pct"] or 0) / 100, "lo": stats["ci95_pct"][0] / 100, "hi": stats["ci95_pct"][1] / 100}
    st = ev.evidence_status(n, ci, None, None)["status"]
    assert st != NEVER
    return {"no_data": "no_data", "no_edge": "no_edge (replay)", "unproven": "unproven (replay)"}[st]


# ------------------------------------------------------------------ baselines
def btc_hold(store: rdata.SeriesStore, start: datetime, end: datetime) -> dict | None:
    df = store.asof("spot", "BTCUSDT", end)
    df = df[df["date"] >= pd.Timestamp(start)]
    if len(df) < 2:
        return None
    p0, p1 = float(df.iloc[0]["open"]), float(df.iloc[-1]["close"])
    curve = [(r.date.strftime("%Y-%m-%dT%H:%M:%SZ"), float(r.close) / p0) for r in df.itertuples()]
    return {"return_pct": round((p1 / p0 - 1) * 100, 3), "curve": curve[:: max(1, len(curve) // 300)]}


def random_entries(n_trades: int, short_share: float, store: rdata.SeriesStore, category: str, pairs: list[str], start: datetime, end: datetime,
                   roi_table: dict, stoploss: float, fee: float, slip: float, repeats: int = 200, seed: int = 12345, max_hold_h: int = 240) -> dict | None:
    """Same number of trades, same exit constants (ROI table, stoploss, a hold cap), entries at random hours on the same pairs. ROTATION exits are NOT modelled
    (they depend on the LLM), so this is a conservative-in-form reference for the bot's mean net return per trade, not an exact twin."""
    if n_trades < 1:
        return None
    arrs = []
    for p in pairs:
        df = store.asof(category, rdata.bybit_symbol(p), end)
        df = df[df["date"] >= pd.Timestamp(start)].reset_index(drop=True)
        if len(df) > 30:
            arrs.append((df["open"].to_numpy(float), df["high"].to_numpy(float), df["low"].to_numpy(float), df["close"].to_numpy(float)))
    if not arrs:
        return None
    rois = sorted((int(k), float(v)) for k, v in (roi_table or {}).items())

    def roi_at(age_min):
        best = None
        for k, v in rois:
            if k <= age_min:
                best = v
        return best

    def one(rng):
        o, h, l, c = arrs[rng.randrange(len(arrs))]
        i = rng.randrange(0, max(1, len(o) - 26))
        short = rng.random() < short_share
        entry = o[i] * (1 - slip if short else 1 + slip)
        f = fee

        def ratio(rate):
            return (entry * (1 - f)) / (rate * (1 + f)) - 1 if short else (rate * (1 - f)) / (entry * (1 + f)) - 1

        def rate_for(target):
            return entry * (1 - f) / ((1 + target) * (1 + f)) if short else (1 + target) * entry * (1 + f) / (1 - f)
        stop = rate_for(stoploss)
        last = min(len(o) - 1, i + max_hold_h)
        for j in range(i, last + 1):
            age = (j - i + 1) * 60
            roi = roi_at(age)
            stop_hit = h[j] >= stop if short else l[j] <= stop
            if stop_hit:
                return ratio(stop * (1 + slip if short else 1 - slip))
            if roi is not None:
                rr = rate_for(roi)
                if (l[j] <= rr) if short else (h[j] >= rr):
                    return ratio(rr * (1 + slip if short else 1 - slip))
        return ratio(c[last] * (1 + slip if short else 1 - slip))

    rng = random.Random(seed)
    means = []
    for _ in range(repeats):
        rs = [one(rng) for _ in range(n_trades)]
        means.append(sum(rs) / len(rs))
    arr = np.array(means) * 100
    return {"repeats": repeats, "n_trades": n_trades, "mean_trade_pct": {"p5": round(float(np.percentile(arr, 5)), 4), "p50": round(float(np.percentile(arr, 50)), 4),
                                                                         "p95": round(float(np.percentile(arr, 95)), 4)}, "_samples": arr.tolist()}


def percentile_rank(samples: list[float], value_pct: float) -> float:
    s = np.array(samples)
    return round(float((s < value_pct).mean() * 100 + 0.5 * (s == value_pct).mean() * 100), 1)


# -------------------------------------------------------------------- ledger
def decisions_to_ledger(decisions: list[dict], store: rdata.SeriesStore, pair_category: dict[str, str], horizon_end: datetime) -> tuple[list[dict], list[dict]]:
    """Replay decisions -> (signals, forward records) in the exact P19 shapes. Decision price = close of the candle opened at t0-1h; forward price at h = close of
    the candle opened at t0+h-1h; a horizon whose closing time is after the data end is not computed (same maturity rule as the live job)."""
    signals, forward = [], []
    for d in decisions:
        t0 = datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        cat = pair_category.get(d["pair"], rdata.category_of(d["pair"]))
        sym = rdata.bybit_symbol(d["pair"])
        sid = f"{d['bot']}|{d['pair']}|{d['ts']}|{d.get('mode', '')}"
        signals.append({"sid": sid, "ts": d["ts"], "t0": d["ts"], "bot": d["bot"], "pair": d["pair"], "action": d["action"], "confidence": d.get("confidence"),
                        "price": d.get("price")})
        df = store.frames.get((cat, sym))
        if df is None:
            continue
        idx = df.set_index("date")["close"]
        p0 = idx.get(pd.Timestamp(t0 - timedelta(hours=1)))
        for h in sa.HORIZONS:
            if t0 + timedelta(hours=h) > horizon_end:
                continue
            ph = idx.get(pd.Timestamp(t0 + timedelta(hours=h - 1)))
            if p0 and ph:
                forward.append({"sid": sid, "h": h, "price0": float(p0), "price_h": float(ph), "ret": float(ph) / float(p0) - 1.0})
    return signals, forward


def audit_status(entry: dict) -> str:
    """Signal-based label for one bot from the P19 audit entry. Never 'edge'."""
    if entry.get("status") != "ok":
        return "no_data"
    best = "unproven (replay)"
    uppers = []
    for d in ("buy", "sell_short"):
        for h in ("h1", "h4", "h24"):
            s = (entry.get("by_direction", {}).get(d, {}) or {}).get(h)
            if s and s.get("excess_ci95_pct"):
                uppers.append(s["excess_ci95_pct"][1])
    if uppers and all(u < 0 for u in uppers):
        best = "no_edge (replay)"
    return best


def run_ledger_audit(bot: str, decisions: list[dict], store: rdata.SeriesStore, category: str, end: datetime) -> dict:
    sigs, fwd = decisions_to_ledger(decisions, store, {}, end)
    audit = sa.build_audit(sigs, fwd, end)
    entry = audit["bots"].get(bot) or {"status": "not enough data"}
    return {"entry": entry, "status": audit_status(entry), "resolved": {str(h): sum(1 for f in fwd if f["h"] == h) for h in sa.HORIZONS}, "n_signals": len(sigs)}


# --------------------------------------------------- P22-A2: the short candidates and the model's veto
V2_BOTS = ("shorts_v2",)
V2_PARENT = {"shorts_v2": "shorts"}
CAND_PCT_CHANGE, CAND_VOLATILITY = -2.0, 5.0              # the rule of the v2 bots, copied here ONLY to count candidates before a run (the strategy file is the source of truth; a test compares them)


def count_candidates(store: rdata.SeriesStore, category: str, pairs: list[str], start: datetime, end: datetime, every_n: int) -> int:
    """How many (pair, step) the rule would flag as a short candidate in the window = how many LLM calls a veto run makes (non-candidates never reach the model)."""
    n = 0
    for p in pairs:
        df = store.frames.get((category, rdata.bybit_symbol(p)))
        if df is None or len(df) < 25:
            continue
        pct = df["close"].pct_change(24) * 100
        vol = (df["high"].rolling(24).max() - df["low"].rolling(24).min()) / df["low"].rolling(24).min() * 100
        t0 = df["date"] + pd.Timedelta(hours=1)                           # the decision at t sees the candle that closed at t
        step = pd.Series(range(len(df)))
        t_s = pd.Timestamp(start)
        k = ((t0 - t_s) / pd.Timedelta(hours=1))
        on_step = (k >= 1) & (k <= (end - start).total_seconds() / 3600) & ((k.round().astype(int) - 1) % max(1, every_n) == 0)
        n += int(((pct < CAND_PCT_CHANGE) & (vol < CAND_VOLATILITY) & on_step).sum())
    return n


def candidate_report(decisions: list[dict], store: rdata.SeriesStore, category: str, seed: int = 12345) -> dict:
    """The question of A2: does the model's veto add anything over the bare rule? decisions = the veto run's decisions of one v2 bot (each has `candidate` and `vote`).
    Per candidate the SHORT's forward return is -(price return) at 1h / 4h / 24h (decision price = close of the candle opened at t0-1h, forward = close of the candle opened at t0+h-1h).
    Confirmed vs vetoed candidates are compared with a bootstrap CI over calendar days; `veto_adds` is computed from fixed rules (never `edge`)."""
    from . import kronos_eval as ke
    cands = [d for d in decisions if d.get("candidate")]
    votes = Counter(d.get("vote") for d in cands)
    decided = votes.get("confirm", 0) + votes.get("veto", 0)
    out: dict = {"n_decisions": len(decisions), "n_candidates": len(cands), "votes": dict(votes), "share_vetoed": None if not decided else round(votes.get("veto", 0) / decided, 4),
                 "candidate_share_of_decisions": None if not decisions else round(len(cands) / len(decisions), 4), "forward_short_return_pct": {}, "veto_value": {}}
    rows = []
    for d in cands:
        t0 = datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        df = store.frames.get((category, rdata.bybit_symbol(d["pair"])))
        if df is None:
            continue
        idx = df.set_index("date")["close"]
        p0 = idx.get(pd.Timestamp(t0 - timedelta(hours=1)))
        fw = {}
        for h in sa.HORIZONS:
            ph = idx.get(pd.Timestamp(t0 + timedelta(hours=h - 1)))
            fw[h] = None if not p0 or ph is None else -(float(ph) / float(p0) - 1.0)
        rows.append({"vote": d.get("vote"), "day": d["ts"][:10], "fw": fw})

    def grp(votes_in):
        return [r for r in rows if r["vote"] in votes_in]
    for h in sa.HORIZONS:
        allc = [r["fw"][h] for r in rows if r["fw"][h] is not None]
        conf = [r for r in grp(("confirm",)) if r["fw"][h] is not None]
        veto = [r for r in grp(("veto",)) if r["fw"][h] is not None]

        def block(rs):
            xs = [r["fw"][h] for r in rs]
            ci = ke.cluster_ci(xs, [r["day"] for r in rs], seed=seed) if len(xs) >= 2 else None
            return {"n": len(xs), "mean_pct": None if not xs else round(sum(xs) / len(xs) * 100, 4), "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
                    "hit_rate": None if not xs else round(sum(1 for x in xs if x > 0) / len(xs), 3)}
        out["forward_short_return_pct"][f"h{h}"] = {"all_candidates (bare rule)": {"n": len(allc), "mean_pct": None if not allc else round(sum(allc) / len(allc) * 100, 4)}, "confirmed": block(conf), "vetoed": block(veto)}
        # confirmed minus vetoed: > 0 means the model kept the better candidates. CI by resampling days of both groups together.
        if len(conf) >= 2 and len(veto) >= 2:
            days = sorted({r["day"] for r in conf + veto})
            rng = np.random.default_rng(seed)
            cs = {d: [r["fw"][h] for r in conf if r["day"] == d] for d in days}
            vs = {d: [r["fw"][h] for r in veto if r["day"] == d] for d in days}
            diffs = []
            for _ in range(2000):
                pick = rng.integers(0, len(days), len(days))
                c = [x for i in pick for x in cs[days[i]]]
                v = [x for i in pick for x in vs[days[i]]]
                if c and v:
                    diffs.append(sum(c) / len(c) - sum(v) / len(v))
            m = sum(r["fw"][h] for r in conf) / len(conf) - sum(r["fw"][h] for r in veto) / len(veto)
            lo, hi = (float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))) if diffs else (None, None)
            out["veto_value"][f"h{h}"] = {"confirmed_minus_vetoed_pp": round(m * 100, 4), "ci95_pp": None if lo is None else [round(lo * 100, 4), round(hi * 100, 4)]}
    v4 = out["veto_value"].get("h4")
    if not v4 or v4["ci95_pp"] is None or len(grp(("veto",))) < 20:
        out["veto_adds"] = "insufficient n (fewer than 20 vetoed candidates, or none)"
    elif v4["ci95_pp"][0] > 0:
        out["veto_adds"] = "the veto kept better candidates (4h, CI above 0) -- replay evidence only"
    elif v4["ci95_pp"][1] < 0:
        out["veto_adds"] = "the veto removed the BETTER candidates (4h, CI below 0)"
    else:
        out["veto_adds"] = "no measurable difference between confirmed and vetoed candidates (4h, CI includes 0)"
    out["rule"] = ("fixed in advance: judged on the 4h forward return of the short; needs >= 20 vetoed candidates; bootstrap over calendar days; the trade-level comparison against the rule-only run "
                   "(`llm_vs_rule`) is reported next to it")
    return out


# ------------------------------------------------------------------- compare
def leak_suspect(named: dict, anon: dict) -> bool | None:
    """True when the named run shows a positive CI lower bound (trade level) while the anonymized one does not: a sign the model recognised the market."""
    if not named or not anon:
        return None
    def pos(s):
        return bool(s["ci95_pct"] and s["ci95_pct"][0] > 0)
    return pos(named) and not pos(anon)


# --------------------------------------------------------------- live divergence
def load_live_reference(root: Path) -> dict | None:
    try:
        return json.loads((Path(root) / "live_reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


DIV_COUNT = 0.20            # trade count differs from live by more than +-20 %
DIV_MEAN_PP = 0.5           # mean per-trade return differs by more than 0.5 pp
DIV_MIX_TV = 0.20           # exit-reason mix differs: total-variation distance above 0.2


def _tv(a: dict, b: dict) -> float | None:
    na, nb = sum(a.values()), sum(b.values())
    if not na or not nb:
        return None
    return round(0.5 * sum(abs(a.get(k, 0) / na - b.get(k, 0) / nb) for k in set(a) | set(b)), 4)


def divergence(bot: str, trades: list[dict], ref: dict | None) -> dict:
    """Replay vs the live bot on the overlapping period (counts, mean trade return, exit-reason mix). ref comes from live_reference.json (built from the live bots' trades
    with the P18 export code, or offline from a P18 export ZIP). `flags` lists which of the three thresholds is exceeded."""
    if not ref or bot not in ref:
        return {"available": False, "note": "no live reference for this bot (press 'Update live reference' on /replay, or build live_reference.json from the P18 export: python -m backend.replay.live_reference <zip>)"}
    r = ref[bot]
    def utc(x):
        ts = pd.Timestamp(x)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    lo, hi = utc(r["from"]), utc(r["to"]) + pd.Timedelta(days=1)       # `to` is a date: the whole day counts
    rep = [t for t in trades if lo <= utc(t["close_date"]) <= hi]
    n = len(rep)
    mean = sum(t["profit_ratio"] for t in rep) / n * 100 if n else None
    ratio = None if not r["n_trades"] else round(n / r["n_trades"], 2)
    rep_mix: dict = {}
    for t in rep:
        rep_mix[t.get("exit_reason") or "?"] = rep_mix.get(t.get("exit_reason") or "?", 0) + 1
    live_mix = r.get("exit_reasons") or {}
    tv = _tv(live_mix, rep_mix)
    flags = []
    if r["n_trades"] and ratio is not None and abs(n / r["n_trades"] - 1) > DIV_COUNT:
        flags.append("trade_count")
    if mean is not None and r.get("mean_trade_pct") is not None and abs(mean - r["mean_trade_pct"]) > DIV_MEAN_PP:
        flags.append("mean_return")
    if tv is not None and tv > DIV_MIX_TV:
        flags.append("exit_mix")
    return {"available": True, "period": [r["from"], r["to"]], "live_n_trades": r["n_trades"], "live_mean_trade_pct": r["mean_trade_pct"], "replay_n_trades": n,
            "replay_mean_trade_pct": None if mean is None else round(mean, 4), "trade_count_ratio": ratio,
            "live_exit_reasons": live_mix, "replay_exit_reasons": rep_mix, "exit_mix_tv": tv, "flags": flags, "diverges": bool(flags),
            "thresholds": {"trade_count": f"+-{int(DIV_COUNT * 100)}%", "mean_return_pp": DIV_MEAN_PP, "exit_mix_tv": DIV_MIX_TV},
            "verdict": ("simulator disagrees with the live bot (count ratio outside 0.5-2): do not trust the replay numbers for this bot"
                        if r["n_trades"] and not (0.5 <= n / r["n_trades"] <= 2.0) else
                        "same order of magnitude as live, but diverges on " + ", ".join(flags) if flags else "same order of magnitude as live, within the divergence thresholds")}
