"""
P26-R -- LightGBM cross-sectional ranker, SHADOW (paper) forward test.

The LightGBM v2 walk-forward study (Binance perpetuals, hourly features, backtest/results/p25_lgbm_v2_binance_real.md) passed every pre-registered check except one: a 54% drawdown. A backtest is a
hypothesis. This module tests it forward, on data that did not exist when the model was designed, and risks nothing: it never places an order and never talks to a bot.

Every day at about 00:15 UTC (idempotent, catches up after downtime):
  1. keeps the Binance hourly cache fresh (the exchange API tail for the most liquid contracts; the monthly archive and a new model every RETRAIN_DAYS days)
  2. marks the paper book of the previous days (open-to-open return minus funding, exactly as in the study)
  3. every HOLD days ranks the top-40 perpetuals by trailing turnover with the model, goes long the top 8 / short the bottom 8 (half of the sleeve per leg) and records the turnover
  4. later scores the earlier rankings against what really happened (rank IC)
Two paper books (and a third view of A, see below) run side by side from the same ranking: A = plain (as in the study), B = volatility-scaled (exposure x min(1, TARGET_VOL / trailing 20-day volatility of A, which the study measured at
2.4% a day), only ever scaled down). The scale is fixed in advance from that volatility, not fitted to returns. Costs are NOT in the ledger: it stores gross P&L and turnover, and the reader applies
fee 0.055% + slippage 0.10% per side (0.30% as the stress line), so the verdict can be re-read at any cost assumption.
Book C = book A traded with LIMIT orders instead of market orders (the maker-fill study's primary: a limit 0.10% better than the 00:00 open, two hours to fill, then a taker chase). The paper
positions are A's; only the execution cost differs. A day after each rebalance, when the two hours are over, the job looks at the REAL hourly highs and lows of that morning and records which orders
would have filled (price touched the limit) and what the chased ones cost. This tests the maker study's fill assumption on live data; until a rebalance has been evaluated, book C is charged the
taker cost for it (the conservative side). Book C is information, not part of the verdict.

Forward verdict (computed by code, never written by hand): `collecting` until FORWARD_MIN_DAYS days and FORWARD_MIN_REBALANCES rebalances; then `forward pass` only if ALL hold: mean rank IC >= 0.02,
book B net P&L > 0 at 0.10% slippage AND at 0.30%, book B max drawdown <= FORWARD_MAX_DD of the sleeve; `failing` if B is negative at 0.10% and the IC is not positive; otherwise `unproven`. A forward pass is a
hypothesis for a longer test or a very small real-money test, never `edge`.

Files in DATA_DIR/ranker/: model.txt + model_meta.json, state.json, ledger.jsonl, rankings.jsonl, ic.jsonl, status.json. Switch: RANKER_MODE = off (default) | shadow (dashboard or environment).
The first start downloads the Binance history (about 0.5 GB, about 1.5 h) and trains the model (about 2 GB of RAM while it runs).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from ..replay import carry, lgbm_hourly as lh, lgbm_study as lg, listing_fade as lf, maker_study as mk, momentum as mo
from . import runtime_settings as rs

log = logging.getLogger("ranker")

HOLD = lh.HOLD
QUINTILE = lh.QUINTILE
UNIVERSE_N = lh.UNIVERSE_N
FEE_PCT = mo.FEE_PCT
SLIP_PCT = mo.SLIP_PCT
SLIP_STRESS_PCT = mo.SLIP_STRESS_PCT
TARGET_VOL = 0.010                  # daily volatility the scaled book aims at (the study's book had 2.4% a day)
VOL_LOOKBACK = 20
VOL_MIN_DAYS = 10
VOL_START_SCALE = 0.5               # until VOL_MIN_DAYS of history exist
RETRAIN_DAYS = 28
TAIL_SYMBOLS = 120                  # most liquid contracts (24h turnover) whose recent hours are refreshed from the exchange API
RUN_AFTER_UTC = (0, 15)
PASSIVE_DELTA = mk.PRIMARY["delta"]       # book C: limit 0.10% better than the 00:00 open ...
PASSIVE_TIMEOUT_H = mk.PRIMARY["timeout_h"]  # ... two hours to fill, then chase as a taker
TAIL_PACE = 0.1                     # seconds between exchange API calls
FORWARD_MIN_DAYS = 56
FORWARD_MIN_REBALANCES = 18
FORWARD_MIN_IC = 0.02
FORWARD_MAX_DD = 20.0
RUN_LOCK = threading.Lock()


# ------------------------------------------------------------------ files
def rdir(data_dir: Path | str | None = None) -> Path:
    p = Path(data_dir or os.environ.get("DATA_DIR", "/data")) / "ranker"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _read(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(p: Path, obj) -> None:
    p = Path(p)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def _append(p: Path, obj) -> None:
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, default=str) + "\n")


def _lines(p: Path) -> list[dict]:
    out = []
    try:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def mode(env: dict | None = None) -> str:
    env = rs.merged_env() if env is None else env
    m = str(env.get("RANKER_MODE", "off")).strip().lower()
    return m if m in ("off", "shadow") else "off"


def set_status(d: Path, state: str, message: str = "", **extra) -> None:
    cur = _read(d / "status.json", {}) or {}
    cur.update({"state": state, "message": message, "updated": iso(datetime.now(timezone.utc)), **extra})
    _write(d / "status.json", cur)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def day_of(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _dt(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)


# ------------------------------------------------------------------ the paper book (pure)
def target_weights(scores: dict[str, float], quintile: int | None = None) -> dict[str, float]:
    """Long the `quintile` highest scores, short the `quintile` lowest, half of the sleeve per leg."""
    quintile = quintile or QUINTILE
    if len(scores) < 2 * quintile:
        return {}
    order = sorted(scores, key=lambda s: (scores[s], s))
    return {**{s: 0.5 / quintile for s in order[-quintile:]}, **{s: -0.5 / quintile for s in order[:quintile]}}


def turnover(old: dict[str, float], new: dict[str, float]) -> float:
    return float(sum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in set(old) | set(new)))


def gross_day(weights: dict[str, float], rets: dict[str, float]) -> float:
    """P&L of one day as a fraction of the sleeve: sum of weight x (open-to-open return minus funding); a coin with no price that day earns 0 (held flat)."""
    total = 0.0
    for s, w in weights.items():
        r = rets.get(s)
        if r is not None and np.isfinite(r):
            total += w * float(r)
    return total


def vol_scale(history_a: list[float]) -> float:
    """Exposure of book B: min(1, TARGET_VOL / std of A's last VOL_LOOKBACK daily gross returns); VOL_START_SCALE until VOL_MIN_DAYS exist. Only ever scales down."""
    h = history_a[-VOL_LOOKBACK:]
    if len(h) < VOL_MIN_DAYS:
        return VOL_START_SCALE
    sd = float(np.std(h))
    return 1.0 if sd <= 0 else float(min(1.0, TARGET_VOL / sd))


def rank_ic(scores: dict[str, float], realised: dict[str, float]) -> float | None:
    """Spearman correlation between the model's scores and what the coins really did, over the coins that have both (needs 8)."""
    syms = [s for s in scores if s in realised and np.isfinite(realised[s])]
    if len(syms) < 8:
        return None
    a = np.argsort(np.argsort([scores[s] for s in syms])).astype(float)
    b = np.argsort(np.argsort([realised[s] for s in syms])).astype(float)
    if a.std() == 0 or b.std() == 0:
        return None
    return float(np.corrcoef(a, b)[0, 1])


def passive_outcome(orders: list[dict], M: dict, sym_index: dict[str, int], h0: int, delta: float = PASSIVE_DELTA, timeout_h: int = PASSIVE_TIMEOUT_H) -> dict:
    """What a limit-order execution of one rebalance would have done, from the real hourly bars. orders = [{'s': symbol, 'd': signed weight change}]. A buy limit sits `delta` under the 00:00
    open, a sell limit `delta` over it; it fills if the low / high of the next `timeout_h` hours reaches it. Filled orders earn delta minus the maker fee on their notional; the others are chased
    as a taker at the next hour's open (cost = the move since the open against the order + taker fee + slippage, the last two applied when read). Orders in coins missing from the data are
    counted as chased with no move."""
    fm = mk.FEE_MAKER / 100.0
    Hn = M["o"].shape[1]
    out = {"orders": 0, "filled": 0, "chased": 0, "filled_gain": 0.0, "chase_move": 0.0, "chase_amt": 0.0}
    for o in orders:
        amt, buy = abs(float(o["d"])), float(o["d"]) > 0
        if amt < 1e-12:
            continue
        out["orders"] += 1
        i = sym_index.get(o["s"])
        ref = float(M["o"][i, h0]) if i is not None and h0 < Hn else float("nan")
        h1 = min(Hn, h0 + timeout_h)
        hi = lo = float("nan")
        if i is not None and np.isfinite(ref) and h1 > h0:
            with np.errstate(all="ignore"):
                if np.any(~np.isnan(M["hi"][i, h0:h1])):
                    hi = float(np.nanmax(M["hi"][i, h0:h1]))
                if np.any(~np.isnan(M["lo"][i, h0:h1])):
                    lo = float(np.nanmin(M["lo"][i, h0:h1]))
        if mk.fill_decision(buy, ref, hi, lo, delta):
            out["filled"] += 1
            out["filled_gain"] += amt * (delta - fm)
        else:
            out["chased"] += 1
            nxt = float(M["o"][i, h1]) if i is not None and h1 < Hn else float("nan")
            move = (nxt / ref - 1.0) if np.isfinite(nxt) and np.isfinite(ref) else 0.0
            out["chase_move"] += amt * (move if buy else -move)
            out["chase_amt"] += amt
    return out


# ------------------------------------------------------------------ forward statistics (pure, from the ledger)
def forward_stats(ledger: list[dict], ics: list[dict], slip_pct: float = SLIP_PCT) -> dict:
    cost = (FEE_PCT + slip_pct) / 100.0
    days = sorted({r["date"] for r in ledger if r.get("kind") == "day"})
    reb = [r for r in ledger if r.get("kind") == "rebalance"]
    out: dict = {"days": len(days), "rebalances": len(reb), "slippage_pct": slip_pct}
    passive = {r["date"]: r for r in ledger if r.get("kind") == "passive" and not r.get("unevaluated")}
    for book in ("A", "B", "C"):
        gross = {r["date"]: r["gross_A" if book == "C" else f"gross_{book}"] for r in ledger if r.get("kind") == "day"}
        costs: dict[str, float] = {}
        for r in reb:
            if book == "C" and r["date"] in passive:                 # limit orders: the real fills of that morning; chased orders pay the move + taker fee + slippage
                p = passive[r["date"]]
                c = p["chase_move"] + p["chase_amt"] * cost - p["filled_gain"]
            else:                                                    # not evaluated yet (or a plain book): taker cost
                c = r["turnover_A" if book == "C" else f"turnover_{book}"] * cost
            costs[r["date"]] = costs.get(r["date"], 0.0) + c
        dates = sorted(set(gross) | set(costs))
        net = np.array([gross.get(d, 0.0) - costs.get(d, 0.0) for d in dates])
        if len(net) == 0:
            out[book] = {"net_pct_of_sleeve": 0.0, "apr_pct": None, "max_dd_pct": 0.0, "sharpe": None, "curve": []}
            continue
        cum = np.cumsum(net)
        dd = float(np.max(np.maximum.accumulate(cum) - cum))
        sd = float(net.std())
        out[book] = {"net_pct_of_sleeve": round(float(cum[-1]) * 100, 3), "apr_pct": round(float(net.mean()) * 36500, 1), "max_dd_pct": round(dd * 100, 2),
                     "sharpe": round(float(net.mean()) / sd * np.sqrt(365), 2) if sd > 0 else None, "curve": [[d, round(float(c) * 100, 3)] for d, c in zip(dates, cum)]}
    ev = [r for r in ledger if r.get("kind") == "passive" and not r.get("unevaluated")]
    n_or = sum(r["orders"] for r in ev)
    out["passive"] = {"rebalances_evaluated": len(ev), "rebalances": len(reb), "orders": n_or, "fill_rate": round(sum(r["filled"] for r in ev) / n_or, 3) if n_or else None,
                      "chased": sum(r["chased"] for r in ev), "avg_chase_move_pct": round(100.0 * sum(r["chase_move"] for r in ev) / max(1e-12, sum(r["chase_amt"] for r in ev)), 3) if any(r["chase_amt"] for r in ev) else None,
                      "delta_pct": PASSIVE_DELTA * 100, "timeout_h": PASSIVE_TIMEOUT_H, "saving_vs_A_pct_of_sleeve": round(out["C"]["net_pct_of_sleeve"] - out["A"]["net_pct_of_sleeve"], 3)}
    vals = [x["ic"] for x in ics if x.get("ic") is not None]
    out["ic"] = {"n": len(vals), "mean": round(float(np.mean(vals)), 4) if vals else None, "hit_pct": round(100.0 * float(np.mean([v > 0 for v in vals])), 1) if vals else None}
    return out


def forward_verdict(base: dict, stress: dict) -> dict:
    """Computed, never written by hand. `base` = forward_stats at 0.10% slippage, `stress` at 0.30%."""
    if base["days"] < FORWARD_MIN_DAYS or base["rebalances"] < FORWARD_MIN_REBALANCES:
        return {"verdict": "collecting", "failed": [f"{base['days']} of {FORWARD_MIN_DAYS} days, {base['rebalances']} of {FORWARD_MIN_REBALANCES} rebalances"]}
    failed = []
    ic = base["ic"]["mean"]
    if ic is None or ic < FORWARD_MIN_IC:
        failed.append(f"mean rank IC {ic} < {FORWARD_MIN_IC} (n={base['ic']['n']})")
    if base["B"]["net_pct_of_sleeve"] <= 0:
        failed.append(f"scaled book net {base['B']['net_pct_of_sleeve']}% of the sleeve at 0.10% slippage is not positive")
    if stress["B"]["net_pct_of_sleeve"] <= 0:
        failed.append(f"scaled book net {stress['B']['net_pct_of_sleeve']}% at 0.30% slippage is not positive")
    if base["B"]["max_dd_pct"] > FORWARD_MAX_DD:
        failed.append(f"scaled book max drawdown {base['B']['max_dd_pct']}% > {FORWARD_MAX_DD}% of the sleeve")
    if not failed:
        return {"verdict": "forward pass", "failed": []}
    if base["B"]["net_pct_of_sleeve"] < 0 and (ic is None or ic <= 0):
        return {"verdict": "failing", "failed": failed}
    return {"verdict": "unproven", "failed": failed}


# ------------------------------------------------------------------ data
def last_archive_month(sym: str) -> str | None:
    d = lh.cdir("k", sym)
    months = sorted(f.stem for f in d.glob("*.npy") if re.fullmatch(r"\d{4}-\d{2}", f.stem)) if d.is_dir() else []
    return months[-1] if months else None


def _next_month_ms(month: str | None, fallback_ms: int) -> int:
    if not month:
        return fallback_ms
    t = datetime.strptime(month + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int((t + timedelta(days=32)).replace(day=1).timestamp() * 1000)


def refresh_tail(get, now: datetime, logf=log.info, top: int = TAIL_SYMBOLS, pace: float | None = None) -> dict:
    """Recent hours (since the last month in the archive cache) for the most liquid cached contracts, from the exchange API. Overwrites each symbol's tail file."""
    pc = lf.Paced(get, TAIL_PACE if pace is None else pace)
    cached = set((lf._read_json(lh.cdir("symbols.json")) or {}).get("symbols") or [])
    live = lh.trading_symbols(pc)
    ranked = []
    try:
        r = pc(f"{lh.FAPI}/fapi/v1/ticker/24hr", None, 60)
        if r.status_code == 200:
            ranked = [x["symbol"] for x in sorted(r.json(), key=lambda x: -float(x.get("quoteVolume") or 0.0))]
    except Exception as e:      # noqa: BLE001
        logf(f"ticker listing failed: {e}")
    cand = [s for s in ranked if s in live and (not cached or s in cached)][:top] or sorted(live & cached)[:top]
    now_ms = int(now.timestamp() * 1000)
    errors = 0
    for sym in cand:
        try:
            lh.fetch_tail(pc, sym, max(_next_month_ms(last_archive_month(sym), now_ms - 40 * 86_400_000), now_ms - 62 * 86_400_000), now_ms)
        except Exception as e:      # noqa: BLE001
            errors += 1
            logf(f"{sym}: tail failed: {type(e).__name__}: {e}")
    return {"symbols": len(cand), "errors": errors, "calls": pc.calls}


def history_ready() -> bool:
    return bool((lf._read_json(lh.cdir("symbols.json")) or {}).get("symbols"))


# ------------------------------------------------------------------ model
def train_model(now: datetime, data_dir=None, logf=log.info) -> dict:
    """Fits the study's fixed LightGBM on every labelled (day, coin) row of the cache (labels ending before today) and saves it. Heavy: full history in memory."""
    lgb = lg._lightgbm()
    d = rdir(data_dir)
    start = datetime.fromisoformat(lh.SINCE).replace(tzinfo=timezone.utc)
    days, syms, M, Od, Td, X, F = lh.build(start, now - timedelta(days=1), UNIVERSE_N, logf)
    feats = lh.build_features(M, Od, Td, F, 30, len(days) - 2, syms, UNIVERSE_N)
    panel = lh.panel_for(feats, X, HOLD)
    del M
    Xtr, ytr = lg.train_rows(panel, X.shape[1], None, HOLD)
    if Xtr is None or len(ytr) < 500:
        raise RuntimeError("not enough labelled rows in the cache to train the ranker")
    model = lgb.LGBMRegressor(**lh.PARAMS)
    model.fit(Xtr, ytr)
    model.booster_.save_model(str(d / "model.txt"))
    meta = {"trained_at": iso(now), "rows": int(len(ytr)), "features": lh.FEATURES, "params": lh.PARAMS, "last_row_day": datetime.fromtimestamp(days[max(feats)] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "symbols_in_cache": len(syms)}
    _write(d / "model_meta.json", meta)
    logf(f"ranker model trained on {meta['rows']} rows until {meta['last_row_day']}")
    return meta


def load_model(data_dir=None):
    lgb = lg._lightgbm()
    p = rdir(data_dir) / "model.txt"
    return lgb.Booster(model_file=str(p)) if p.is_file() else None


def model_due(now: datetime, data_dir=None) -> bool:
    meta = _read(rdir(data_dir) / "model_meta.json")
    if not meta or not (rdir(data_dir) / "model.txt").is_file():
        return True
    try:
        t = datetime.strptime(meta["trained_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return True
    return (now - t).days >= RETRAIN_DAYS


# ------------------------------------------------------------------ one day
def recent_data(now: datetime, window_days: int = 16, logf=log.info):
    """The last weeks of the cache as the study's matrices. The last day column is today; the last return column is yesterday."""
    start = (now - timedelta(days=window_days)).replace(hour=0, minute=0, second=0, microsecond=0)
    since_month = (now - timedelta(days=window_days + 75)).strftime("%Y-%m")
    return lh.build(start, now - timedelta(days=1), UNIVERSE_N, logf, since_month)


def score_today(booster, data, logf=log.info) -> dict | None:
    days, syms, M, Od, Td, X, F = data
    k = len(days) - 1
    uni = mo.universe_at(Td, Od, k, UNIVERSE_N)
    if len(uni) < 2 * QUINTILE:
        return None
    btc = syms.index("BTCUSDT") if "BTCUSDT" in syms else None
    f = lh.feature_day(M, Od, F, k, uni, btc)
    if f is None:
        return None
    ok = ~np.isnan(f[:, 0])
    if ok.sum() < 2 * QUINTILE:
        return None
    pred = booster.predict(f[ok])
    sel = [uni[j] for j in np.flatnonzero(ok)]
    scores = {syms[i]: float(s) for i, s in zip(sel, pred)}
    return {"date": datetime.fromtimestamp(days[k] / 1000, tz=timezone.utc).strftime("%Y-%m-%d"), "scores": scores, "universe": len(uni)}


def realised(data, day: str, hold: int = HOLD) -> dict[str, float]:
    """What each coin did over `hold` days from `day`: return minus funding, minus the mean of the coins (same label as the study). {} when the days are not complete yet."""
    days, syms, M, Od, Td, X, F = data
    ms = int(_dt(day).timestamp() * 1000)
    if ms not in days:
        return {}
    k = days.index(ms)
    if k + hold > X.shape[1]:
        return {}
    seg = np.nansum(X[:, k:k + hold], axis=1)
    has = ~np.all(np.isnan(X[:, k:k + hold]), axis=1)
    vals = {syms[i]: float(seg[i]) for i in range(len(syms)) if has[i]}
    if not vals:
        return {}
    m = float(np.mean(list(vals.values())))
    return {s: v - m for s, v in vals.items()}


def day_returns(data, day: str) -> dict[str, float]:
    days, syms, M, Od, Td, X, F = data
    ms = int(_dt(day).timestamp() * 1000)
    if ms not in days:
        return {}
    k = days.index(ms)
    if k >= X.shape[1]:
        return {}
    return {syms[i]: float(X[i, k]) for i in range(len(syms)) if np.isfinite(X[i, k])}


def evaluate_passive(d: Path, data, today: str, logf=log.info) -> int:
    """For every rebalance of an earlier day that has orders but no passive record yet, record what limit orders would have done (see passive_outcome). A rebalance whose morning is not in the
    data window any more is recorded as unevaluated (book C then pays the taker cost for it)."""
    days, syms, M, Od, Td, X, F = data
    ledger = _lines(d / "ledger.jsonl")
    have = {r["date"] for r in ledger if r.get("kind") == "passive"}
    idx = {s: i for i, s in enumerate(syms)}
    n = 0
    for r in ledger:
        if r.get("kind") != "rebalance" or r["date"] in have or r["date"] >= today or "orders" not in r:
            continue
        ms = int(_dt(r["date"]).timestamp() * 1000)
        if ms not in days:
            _append(d / "ledger.jsonl", {"kind": "passive", "date": r["date"], "unevaluated": True})
            logf(f"rebalance of {r['date']} is outside the data window: book C pays the taker cost for it")
            continue
        out = passive_outcome(r["orders"], M, idx, 24 * days.index(ms))
        _append(d / "ledger.jsonl", {"kind": "passive", "date": r["date"], **{k: (round(v, 8) if isinstance(v, float) else v) for k, v in out.items()}})
        n += 1
    return n


def process_day(d: Path, data, booster, now: datetime, logf=log.info) -> dict:
    """Marks the missing days, scores earlier rankings, rebalances when due. Pure file/state logic on already loaded data (no network)."""
    today = day_of(now)
    st = _read(d / "state.json") or {"started": today, "last_marked": None, "last_rebalance": None, "A": {}, "B": {}}
    done = {"marked": 0, "rebalanced": False, "ic": 0}
    yesterday = day_of(now - timedelta(days=1))
    # 1. mark every day since the last one held positions
    if st["last_marked"] is None:
        st["last_marked"] = yesterday if not st["A"] else st["last_marked"]
    day = _dt(st["last_marked"]) + timedelta(days=1)
    while day_of(day) <= yesterday and (st["A"] or st["B"]):
        r = day_returns(data, day_of(day))
        if r:
            _append(d / "ledger.jsonl", {"kind": "day", "date": day_of(day), "gross_A": gross_day(st["A"], r), "gross_B": gross_day(st["B"], r)})
            done["marked"] += 1
        else:
            logf(f"no prices for {day_of(day)}: day not marked (older than the cache window?)")
        st["last_marked"] = day_of(day)
        day += timedelta(days=1)
    if not (st["A"] or st["B"]):
        st["last_marked"] = yesterday
    # 2. score earlier rankings that are now complete
    scored = {x["date"] for x in _lines(d / "ic.jsonl")}
    for rk in _lines(d / "rankings.jsonl"):
        if rk.get("rebalance") and rk["date"] not in scored:
            real = realised(data, rk["date"])
            if real:
                ic = rank_ic(rk["scores"], real)
                _append(d / "ic.jsonl", {"date": rk["date"], "ic": ic, "n": len([s for s in rk["scores"] if s in real])})
                done["ic"] += 1
    # 2b. book C: the morning of an earlier rebalance is over, look at the real hourly bars
    done["passive"] = evaluate_passive(d, data, today, logf)
    # 3. rank today; rebalance every HOLD days
    due = st["last_rebalance"] is None or (_dt(today) - _dt(st["last_rebalance"])).days >= HOLD
    if st.get("ranked_on") != today:
        sc = score_today(booster, data, logf)
        if sc:
            w_a = target_weights(sc["scores"])
            hist = [x["gross_A"] for x in _lines(d / "ledger.jsonl") if x.get("kind") == "day"]
            scale = vol_scale(hist)
            w_b = {s: w * scale for s, w in w_a.items()}
            _append(d / "rankings.jsonl", {"date": sc["date"], "rebalance": bool(due), "universe": sc["universe"], "scores": {s: round(v, 6) for s, v in sc["scores"].items()}})
            if due and w_a:
                orders = [{"s": s_, "d": round(w_a.get(s_, 0.0) - st["A"].get(s_, 0.0), 6)} for s_ in sorted(set(w_a) | set(st["A"])) if abs(w_a.get(s_, 0.0) - st["A"].get(s_, 0.0)) > 1e-12]
                _append(d / "ledger.jsonl", {"kind": "rebalance", "date": today, "turnover_A": turnover(st["A"], w_a), "turnover_B": turnover(st["B"], w_b), "scale": round(scale, 4),
                                             "longs": sorted(s for s, w in w_a.items() if w > 0), "shorts": sorted(s for s, w in w_a.items() if w < 0), "orders": orders})
                st.update({"A": w_a, "B": w_b, "last_rebalance": today, "scale": round(scale, 4)})
                done["rebalanced"] = True
            st["ranked_on"] = today
        else:
            logf("no ranking today (universe or features incomplete)")
    _write(d / "state.json", st)
    return done


def run_once(get, now: datetime | None = None, data_dir=None, force: bool = False, logf=log.info) -> dict:
    """The daily job. Safe to call often: it does nothing when today is done. Returns {ran, reason, ...}. Never raises."""
    now = now or datetime.now(timezone.utc)
    d = rdir(data_dir)
    if mode() == "off" and not force:
        return {"ran": False, "reason": "RANKER_MODE is off"}
    if not RUN_LOCK.acquire(blocking=False):
        return {"ran": False, "reason": "already running"}
    try:
        today = day_of(now)
        st = _read(d / "status.json", {}) or {}
        if st.get("last_run_day") == today and not force:
            return {"ran": False, "reason": "already ran today"}
        if not force and (now.hour, now.minute) < RUN_AFTER_UTC:
            return {"ran": False, "reason": "before 00:15 UTC"}
        if not history_ready():
            set_status(d, "downloading", "downloading the Binance history (about 0.5 GB, about 1.5 h)")
            res = lh.fetch_all(get, now, logf)
            if res.get("stopped") or not history_ready():
                set_status(d, "failed", "history download did not finish; it resumes on the next run")
                return {"ran": False, "reason": "history incomplete"}
        if model_due(now, data_dir):
            set_status(d, "training", "refreshing the archive and training the model")
            lh.fetch_all(get, now, logf)
            train_model(now, data_dir, logf)
        set_status(d, "refreshing", "recent hours from the exchange API")
        tail = refresh_tail(get, now, logf)
        booster = load_model(data_dir)
        if booster is None:
            raise RuntimeError("no model file")
        data = recent_data(now, logf=logf)
        res = process_day(d, data, booster, now, logf)
        set_status(d, "ok", f"{res['marked']} day(s) marked, rebalanced={res['rebalanced']}, {res['ic']} IC scored, tail {tail}", last_run_day=today, last_run=iso(now))
        return {"ran": True, **res, "tail": tail}
    except Exception as e:      # noqa: BLE001
        logf(f"ranker run failed: {type(e).__name__}: {e}")
        set_status(d, "failed", f"{type(e).__name__}: {e}")
        return {"ran": False, "reason": f"{type(e).__name__}: {e}"}
    finally:
        RUN_LOCK.release()


# ------------------------------------------------------------------ what the dashboard shows
def snapshot(data_dir=None) -> dict:
    d = rdir(data_dir)
    ledger, ics = _lines(d / "ledger.jsonl"), _lines(d / "ic.jsonl")
    base, stress = forward_stats(ledger, ics, SLIP_PCT), forward_stats(ledger, ics, SLIP_STRESS_PCT)
    st = _read(d / "state.json") or {}
    rks = _lines(d / "rankings.jsonl")
    last = rks[-1] if rks else None
    ranking = None
    if last:
        order = sorted(last["scores"].items(), key=lambda kv: -kv[1])
        ranking = {"date": last["date"], "universe": last.get("universe"), "rebalance": last.get("rebalance"), "top": order[:QUINTILE], "bottom": order[-QUINTILE:][::-1]}
    reb = [r for r in ledger if r.get("kind") == "rebalance"]
    v = forward_verdict(base, stress)
    return {"mode": mode(), "status": _read(d / "status.json", {}), "model": _read(d / "model_meta.json"), "ranking": ranking, "book": {"A": st.get("A", {}), "B": st.get("B", {}), "scale": st.get("scale"),
            "last_rebalance": st.get("last_rebalance"), "started": st.get("started")}, "last_rebalances": reb[-5:], "forward": {"base": base, "stress": stress}, "verdict": v["verdict"],
            "verdict_failed_conditions": v["failed"], "thresholds": {"min_days": FORWARD_MIN_DAYS, "min_rebalances": FORWARD_MIN_REBALANCES, "min_ic": FORWARD_MIN_IC, "max_dd_pct": FORWARD_MAX_DD},
            "study": {"oos_apr_pct": 99.1, "oos_max_dd_pct": 54.2, "oos_rank_ic": 0.069, "note": "backtest, 2024-07..2026-10, plain book; the forward test is the real question"}}
