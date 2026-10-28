"""
P22-B: Listing Fade validation -- the live rule-based `listing_fade` strategy replayed on EVERY Bybit USDT perpetual listed since 2024-01-01 (delisted ones included),
with real fees, funding, stop gaps and slippage scenarios. Rule based, no LLM, $0.

Rules are NOT copied by hand: the live strategy file is loaded (freqtrade stubbed, exactly like the LLM bots' replay sandbox) and its own `populate_indicators` /
`populate_entry_trend` produce the signals; its class constants (stoploss, trailing, max hold, funding threshold, EMA, drop from high) and the bot's config.json
(max_open_trades, stake, wallet, AgeFilter / PriceFilter) are read from the files, and the SHA-256 of both is printed in every report. Nothing is re-tuned.

No look-ahead, enforced three ways: (1) every indicator the strategy uses (cummax, ewm) is causal and the signal at candle i is computed from candles <= i; (2) the entry is
filled at the OPEN of candle i+1; (3) the funding veto and the price filter use only values published at or before the signal's close. A fixture test shuffles all future
candles and asserts that no earlier signal changes.

Known limits (printed in the report): VolumePairList(top 200) and SpreadFilter of the live pair list cannot be rebuilt from public history and are NOT applied; the live entry is a
passive limit order that may expire (covered only by the slippage scenarios); Bybit lists delisted instruments with status `Closed`, but any symbol Bybit removed from the API
altogether is invisible (residual survivorship bias, size unknown).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backend.engine import evidence as ev

from . import analysis, data as rdata, paths, sandbox

BYBIT = rdata.BYBIT
SINCE = "2024-01-01"
HOURLY_DAYS = 31                 # hourly history from the listing: the strategy needs the whole listing history for its running high; entries happen on days 2..30
FUNDING_DAYS = 46
HOLD_DAYS_FETCH = 15             # 5-minute candles after the last possible entry (14 days max hold + 1)
FEE_TAKER_PCT = 0.055            # Bybit USDT perpetual taker fee, non-VIP, per side
SLIPPAGES_PCT = (0.0, 0.1, 0.3, 0.5)
PRIMARY_SLIPPAGE = 0.3           # "main conclusion is drawn on 0.3%"
LIQUIDITY_STAKE_SHARE = 0.01     # flag: stake >= 1% of the first hour's USDT volume (a stake below 1% of it is fine)
VERDICT_MIN_N = 100
STUDY_ID = "listing_fade"        # directory name under REPLAY_DIR for the raw cache


# ------------------------------------------------------------------ paths / cache
def lf_dir(*parts: str) -> Path:
    name = "/".join(parts)
    p = paths.replay_path(STUDY_ID, name) if name else paths.replay_path(STUDY_ID)
    return p


def _read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    paths.ensure_parent(p).write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


# ------------------------------------------------------------------ the live rules, read from the files
def rules_dir(base=None) -> Path:
    base = Path(base) if base else sandbox.default_strategies_dir()
    baked = base / "listing_fade"
    return baked if (baked / "ListingFadeStrategy.py").is_file() else base / "freqtrade-listing-fade" / "user_data"


def _strategy_file(base=None) -> Path:
    d = rules_dir(base)
    return d / "ListingFadeStrategy.py" if (d / "ListingFadeStrategy.py").is_file() else d / "strategies" / "ListingFadeStrategy.py"


def _config_file(base=None) -> Path:
    d = rules_dir(base)
    return d / "config.json"


def load_rules(base=None) -> dict:
    """The live strategy object (real code) + the numbers the simulation needs + the hashes of the two files."""
    sp, cp = _strategy_file(base), _config_file(base)
    cfg = json.loads(cp.read_text(encoding="utf-8"))
    with sandbox._LOAD_LOCK:
        sandbox._install_stubs()
        import importlib.util
        import sys
        sys.path.insert(0, str(sp.parent))
        try:
            spec = importlib.util.spec_from_file_location("replay_listing_fade_strategy", sp)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(str(sp.parent))
    cls = mod.ListingFadeStrategy
    obj = cls(cfg)
    pl = {x["method"]: x for x in cfg.get("pairlists", [])}
    return {
        "obj": obj, "strategy_file": str(sp), "strategy_sha256": hashlib.sha256(sp.read_bytes()).hexdigest(), "config_sha256": hashlib.sha256(cp.read_bytes()).hexdigest(),
        "stoploss": float(cls.stoploss), "trailing_stop": bool(cls.trailing_stop), "trailing_positive": float(cls.trailing_stop_positive), "trailing_offset": float(cls.trailing_stop_positive_offset),
        "trailing_only_offset": bool(cls.trailing_only_offset_is_reached), "minimal_roi": dict(cls.minimal_roi), "max_hold_days": int(cls.MAX_HOLD_DAYS),
        "pct_from_high": float(cls.PCT_FROM_HIGH_THRESHOLD), "ema_period": int(cls.EMA_PERIOD), "funding_threshold": float(cls.FUNDING_NOT_TOO_NEGATIVE_THRESHOLD), "timeframe": cls.timeframe,
        "max_open_trades": int(cfg["max_open_trades"]), "stake": float(cfg["stake_amount"]), "wallet": float(cfg["dry_run_wallet"]),
        "age_min_days": int((pl.get("AgeFilter") or {}).get("min_days_listed", 0)), "age_max_days": int((pl.get("AgeFilter") or {}).get("max_days_listed", 10 ** 6)),
        "price_low_ratio": float((pl.get("PriceFilter") or {}).get("low_price_ratio", 0)), "blacklist": list(cfg.get("exchange", {}).get("pair_blacklist", [])),
        "not_applied": ["VolumePairList (top 200 by quote volume): the historical ranking of all pairs is not public", "SpreadFilter (max_spread_ratio 0.005): the historical order book is not public"],
    }


def rules_summary(r: dict) -> dict:
    keys = ("strategy_sha256", "config_sha256", "stoploss", "trailing_stop", "trailing_positive", "trailing_offset", "trailing_only_offset", "minimal_roi", "max_hold_days", "pct_from_high",
            "ema_period", "funding_threshold", "timeframe", "max_open_trades", "stake", "wallet", "age_min_days", "age_max_days", "price_low_ratio", "not_applied")
    return {k: r[k] for k in keys} | {"strategy_file": Path(r["strategy_file"]).name}


# ------------------------------------------------------------------ fetching (public Bybit API, no keys; cached, resumable, polite)
class Paced:
    """Wraps requests.get: at least `pace` seconds between requests; counts calls and errors."""

    def __init__(self, get, pace: float = 0.12, sleep=time.sleep):
        self.get, self.pace, self.sleep = get, pace, sleep
        self.calls = 0
        self.errors = 0
        self._last = 0.0
        self.lock = threading.Lock()

    def __call__(self, url, params=None, timeout=None, **kw):
        with self.lock:
            wait = self.pace - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
        try:
            return self.get(url, params=params, timeout=timeout, **kw)
        except Exception:
            with self.lock:
                self.errors += 1
            raise


def blacklisted(symbol: str, patterns: list[str]) -> bool:
    pair = (symbol[:-4] if symbol.endswith("USDT") else symbol) + "/USDT:USDT"
    return any(re.fullmatch(p, pair) for p in patterns)


def fetch_instruments(get, since: datetime, now: datetime, patterns: list[str]) -> dict:
    """Trading + Closed (delisted) linear USDT perpetuals launched in [since, now]. The API lists current instruments only, EXCEPT that status=Closed returns the delisted ones: both are used."""
    found: dict[str, dict] = {}
    raw_counts = {"Trading": 0, "Closed": 0}
    for status in ("Trading", "Closed"):
        cursor = ""
        for _ in range(60):
            params = {"category": "linear", "status": status, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = rdata._get_json(get, f"{BYBIT}/v5/market/instruments-info", params)
            res = data.get("result") or {}
            for it in res.get("list") or []:
                raw_counts[status] += 1
                if it.get("quoteCoin") != "USDT" or it.get("contractType") != "LinearPerpetual":
                    continue
                lt = int(it.get("launchTime") or 0)
                if not lt or lt < _ms(since) or lt > _ms(now) or blacklisted(it["symbol"], patterns):
                    continue
                found[it["symbol"]] = {"symbol": it["symbol"], "status": it.get("status", status), "launch_ms": lt, "delivery_ms": int(it.get("deliveryTime") or 0),
                                       "tick": float((it.get("priceFilter") or {}).get("tickSize") or 0)}
            cursor = res.get("nextPageCursor") or ""
            if not cursor:
                break
    items = sorted(found.values(), key=lambda x: x["launch_ms"])
    return {"fetched_at": _ms(now), "since": since.strftime("%Y-%m-%d"), "raw_counts": raw_counts, "n": len(items), "n_trading": sum(1 for x in items if x["status"] == "Trading"),
            "n_closed": sum(1 for x in items if x["status"] != "Trading"), "items": items}


def fetch_funding(get, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """[[timestamp_ms, rate], ...] ascending."""
    out: dict[int, float] = {}
    end = end_ms
    for _ in range(40):
        data = rdata._get_json(get, f"{BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": symbol, "startTime": start_ms, "endTime": end, "limit": 200})
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            break
        for r in rows:
            out[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest <= start_ms or oldest >= end:                  # only an empty page or reaching the start ends the paging (a short page is not the end)
            break
        end = oldest - 1
    return [[k, out[k]] for k in sorted(out)]


def _cached_series(path: Path, fetch, start_ms: int, end_ms: int, step_ms: int):
    """File: {"rows", "vfrom", "vto"}. vfrom / vto = the request bounds whose paging COMPLETED (an empty page or the bound reached). Only what lies outside [vfrom, vto] is fetched. A file written
    by an older version has no marks: it is trusted only from its first to its last row, so a head that an early-ended paging lost is fetched on the next run. Returns rows in range."""
    state = _read_json(path) or {"rows": [], "vfrom": None, "vto": None}
    rows = state["rows"]
    vfrom = state.get("vfrom") if state.get("vfrom") is not None else (int(rows[0][0]) if rows else None)
    vto = state.get("vto") if state.get("vto") is not None else (int(rows[-1][0]) if rows else None)
    extra, changed = [], False
    if vfrom is None:
        extra += fetch(start_ms, end_ms)
        vfrom, vto, changed = start_ms, end_ms, True
    else:
        if start_ms < vfrom:
            extra += fetch(start_ms, vfrom - 1)
            vfrom, changed = start_ms, True
        if end_ms > vto + step_ms:
            extra += fetch(vto + 1, end_ms)
            vto, changed = end_ms, True
    if extra:
        merged = {int(r[0]): r for r in rows + extra}
        rows = [merged[k] for k in sorted(merged)]
        changed = True
    if changed or state.get("vfrom") is None:
        _write_json(path, {"rows": rows, "vfrom": vfrom, "vto": vto})
    return [r for r in rows if start_ms <= int(r[0]) <= end_ms]


def hourly_rows(get, symbol: str, start_ms: int, end_ms: int, sleep=time.sleep):
    return _cached_series(lf_dir("hourly", f"{symbol}.json"), lambda a, b: rdata.fetch_klines("linear", symbol, a, b, get, sleep, "60"), start_ms, end_ms, 3_600_000)


def m5_rows(get, symbol: str, start_ms: int, end_ms: int, sleep=time.sleep):
    return _cached_series(lf_dir("m5", f"{symbol}.json"), lambda a, b: rdata.fetch_klines("linear", symbol, a, b, get, sleep, "5"), start_ms, end_ms, 300_000)


def funding_rows(get, symbol: str, start_ms: int, end_ms: int, sleep=time.sleep):
    return _cached_series(lf_dir("funding", f"{symbol}.json"), lambda a, b: fetch_funding(get, symbol, a, b), start_ms, end_ms, 8 * 3_600_000)


# ------------------------------------------------------------------ signals (the strategy's own code)
def to_df(rows: list[list]) -> pd.DataFrame:
    return rdata.to_frame([[int(r[0])] + [float(x) for x in r[1:7]] for r in rows])


def raw_signals(rules: dict, df: pd.DataFrame, pair: str) -> np.ndarray:
    """enter_short flag of the live strategy for every hourly candle. It uses the candle itself and earlier ones only (cummax, ewm)."""
    out = rules["obj"].populate_entry_trend(rules["obj"].populate_indicators(df.copy(), {"pair": pair}), {"pair": pair})
    return out["enter_short"].to_numpy(dtype=int) == 1


def funding_asof(frows: list[list], t_ms: int) -> float | None:
    best = None
    for ts, rate in frows:
        if ts <= t_ms:
            best = rate
        else:
            break
    return best


def valid_signals(rules: dict, inst: dict, df: pd.DataFrame, frows: list[list]) -> list[dict]:
    """Every hourly signal that passes the live filters, in time order: age window (AgeFilter counts daily candles since the listing), PriceFilter, funding veto (confirm_trade_entry).
    A signal at candle i is known at its CLOSE; the entry is the open of candle i+1."""
    if len(df) < rules["ema_period"] + 1:
        return []
    pair = inst["symbol"][:-4] + "/USDT:USDT"
    sig = raw_signals(rules, df, pair)
    start = df["date"].iloc[0]
    out = []
    for i in range(len(df) - 1):
        if not sig[i]:
            continue
        close_t = df["date"].iloc[i] + pd.Timedelta(hours=1)
        n_daily = (close_t.date() - start.date()).days + 1
        if not (rules["age_min_days"] <= n_daily <= rules["age_max_days"]):
            continue
        price = float(df["close"].iloc[i])
        if rules["price_low_ratio"] and inst["tick"] and inst["tick"] / price > rules["price_low_ratio"]:
            continue
        fr = funding_asof(frows, int(close_t.timestamp() * 1000))
        if fr is not None and fr < rules["funding_threshold"]:
            continue
        out.append({"i": i, "entry_time": close_t.to_pydatetime(), "age_days": (close_t - start).total_seconds() / 86400, "funding": fr})
    return out


# ------------------------------------------------------------------ trade simulation
def profit_short(entry: float, rate: float, fee: float) -> float:
    return (entry * (1 - fee)) / (rate * (1 + fee)) - 1.0


def simulate_trade(rules: dict, entry_time: datetime, m5: pd.DataFrame, frows: list[list], fee_pct: float, slip_pct: float, stake: float, entry_mult: float | None = None,
                   entry_slip_pct: float | None = None) -> dict | None:
    """One short on 5-minute candles: enter at the first candle's open (the hourly open), static -12% stop, Freqtrade-style trailing (armed at +3.5% profit, stop 3% above the lowest rate),
    14-day maximum hold. A stop / trailing trigger fills at the trigger level, or at the candle's OPEN when it gaps through it (the next available price); slippage is added on top.
    Funding is credited / charged at every funding timestamp inside the hold (a short receives a positive rate). entry_mult / entry_slip_pct (P26, default None = unchanged) model a passive entry:
    the fill price is the open times entry_mult and the entry slippage is entry_slip_pct instead of slip_pct."""
    if m5 is None or m5.empty:
        return None
    t = m5["date"]
    start_idx = int(np.searchsorted(t.to_numpy(dtype="datetime64[ns]"), np.datetime64(entry_time.replace(tzinfo=None), "ns")))
    if start_idx >= len(m5):
        return {"skipped": "no_data_after_entry"}
    if (t.iloc[start_idx].to_pydatetime() - entry_time) > timedelta(minutes=15):
        return {"skipped": "no_data_at_entry"}                      # the series starts later than the signal: a trade simulated on it would use the wrong prices
    o, h, l = m5["open"].to_numpy(float), m5["high"].to_numpy(float), m5["low"].to_numpy(float)
    times = [x.to_pydatetime() for x in t]
    f, slip = fee_pct / 100.0, slip_pct / 100.0
    entry = float(o[start_idx]) * (entry_mult or 1.0) * (1 - (slip if entry_slip_pct is None else entry_slip_pct / 100.0))      # P26: a passive entry (limit order above the open, no slippage)
    stop = entry * (1 - f) / ((1 + rules["stoploss"]) * (1 + f))
    positive, offset = rules["trailing_positive"], rules["trailing_offset"]
    best_low, armed = entry, False
    max_hold = timedelta(days=rules["max_hold_days"])
    exit_rate = exit_time = reason = None
    gap = 0.0
    for j in range(start_idx, len(m5)):
        tj = times[j]
        if j > start_idx and (tj - times[j - 1]) > timedelta(minutes=60):
            return {"skipped": "data_gap_in_hold"}                    # candles are missing inside the hold: the path is unknown, so the trade is not simulated (counted in the report)
        trigger, why = stop, "stop_loss"
        if armed and rules["trailing_stop"]:
            trail = best_low * (1 + positive)
            if trail < trigger:
                trigger, why = trail, "trailing_stop_loss"
        if h[j] >= trigger:                                           # a stop that the price reached comes before the time-based exit of the same candle
            raw = float(o[j]) if o[j] >= trigger else trigger
            gap = (raw - trigger) / trigger
            exit_rate, exit_time, reason = raw * (1 + slip), tj, why
            break
        if tj - entry_time >= max_hold:
            exit_rate, exit_time, reason = float(o[j]) * (1 + slip), tj, "max_hold_days_reached"
            break
        best_low = min(best_low, float(l[j]))
        if rules["trailing_stop"] and not armed and profit_short(entry, best_low, f) >= offset:
            armed = True
    open_at_end = exit_rate is None
    if open_at_end:
        exit_rate, exit_time, reason = float(m5["close"].iloc[-1]) * (1 + slip), times[-1] + timedelta(minutes=5), "open_at_data_end"
    funding = 0.0
    for ts, rate in frows:
        ft = _dt(ts)
        if entry_time < ft <= exit_time:
            funding += rate                                  # notional = stake (1x): a positive rate is paid to the short
    price_ratio = profit_short(entry, exit_rate, f)
    ratio = price_ratio + funding
    return {"open_date": entry_time, "close_date": exit_time, "open_rate": entry, "close_rate": exit_rate, "exit_reason": reason, "price_ratio": price_ratio, "funding_ratio": funding,
            "profit_ratio": ratio, "profit_abs": stake * ratio, "fees_abs": stake * f + stake * (exit_rate / entry) * f, "gap_pct": gap * 100, "open_at_end": open_at_end, "side": "short"}


# ------------------------------------------------------------------ the study
def liquidity_flag(df: pd.DataFrame, stake: float) -> tuple[bool, float | None]:
    if df.empty:
        return False, None
    q = float(df["quote"].iloc[0])
    return stake >= LIQUIDITY_STAKE_SHARE * q, q          # flagged: the live stake would be >= 1% of the first hour's volume -- the book was too thin for it


def load_universe(rules: dict, until: datetime, base_get=None) -> tuple[list[dict], dict]:
    """Events from the cache only (no network): [{inst, df, funding, signals, flagged, first_hour_quote}], and a summary of what was found."""
    inst_doc = _read_json(lf_dir("instruments.json"))
    if not inst_doc:
        raise FileNotFoundError("no cached instruments: run 'Fetch data' first")
    events, no_data = [], 0
    for inst in inst_doc["items"]:
        h = _read_json(lf_dir("hourly", f"{inst['symbol']}.json"))
        if not h or not h["rows"]:
            no_data += 1
            continue
        df = to_df(h["rows"])
        f = _read_json(lf_dir("funding", f"{inst['symbol']}.json")) or {"rows": []}
        frows = [[int(a), float(b)] for a, b in f["rows"]]
        flagged, q = liquidity_flag(df, rules["stake"])
        events.append({"inst": inst, "df": df, "funding": frows, "signals": valid_signals(rules, inst, df, frows), "flagged": flagged, "first_hour_quote": q})
    return events, {"instruments": inst_doc, "events_with_data": len(events), "events_without_data": no_data}


def entry_windows(events: list[dict]) -> list[tuple[str, int, int]]:
    """5-minute data needed: from the first valid signal to the last valid signal + 15 days, per event with at least one signal."""
    out = []
    for e in events:
        if e["signals"]:
            a, b = e["signals"][0]["entry_time"], e["signals"][-1]["entry_time"]
            out.append((e["inst"]["symbol"], _ms(a), _ms(b + timedelta(days=HOLD_DAYS_FETCH))))
    return out


def run_scenario(rules: dict, events: list[dict], slip_pct: float, slots: int | None, exclude_flagged: bool, fee_pct: float, until: datetime, m5_cache: dict, diag: dict | None = None,
                 reentry: bool = False, window: tuple[datetime, datetime] | None = None, cooldown_h: float = 0.0) -> list[dict]:
    """Chronological simulation. slots=None -> unlimited (every event trades at its first valid signal); otherwise a signal that finds no free slot is skipped and the same event tries
    again at its next valid signal (the strategy re-evaluates every candle). One trade per listing event, unless reentry=True (the live bot trades a pair again after it exited: used only
    for the like-for-like comparison with the live dry-run). window limits the entry times. cooldown_h (reentry only): after a stop_loss exit the pair is blocked for that many hours (the
    live re-entry guard). diag collects what could not be simulated and why."""
    diag = diag if diag is not None else {}
    cands = []
    for e in events:
        if exclude_flagged and e["flagged"]:
            continue
        for s in e["signals"]:
            if window and not (window[0] <= s["entry_time"] <= window[1]):
                continue
            cands.append((s["entry_time"], e["inst"]["symbol"], e, s))
    cands.sort(key=lambda x: (x[0], x[1]))
    traded: set[str] = set()
    busy_until: dict[str, datetime] = {}
    blocked_until: dict[str, datetime] = {}
    bad: set[tuple[str, datetime]] = set()
    open_until: list[datetime] = []
    trades: list[dict] = []
    for t0, sym, e, s in cands:
        if not reentry and sym in traded:
            continue
        # A signal stays valid for the whole hour of its candle (Freqtrade acts on the last candle's signal at every loop pass), so when no slot is free at the candle close the bot
        # enters the moment a slot frees up INSIDE that hour (the live log shows entries one second after another trade's exit). Several pending signals: earliest candle first.
        t = t0
        win_end = t0 + timedelta(hours=1)
        gate = max([x for x in (busy_until.get(sym), blocked_until.get(sym)) if x] or [t])
        if reentry and gate > t:
            if gate < win_end:
                t = gate
            else:
                continue
        open_until = [x for x in open_until if x > t]
        if slots is not None:
            while len(open_until) >= slots:
                nxt = min(open_until)
                if nxt < win_end:
                    t = max(t, nxt)
                    open_until = [x for x in open_until if x > t]
                else:
                    break
            if len(open_until) >= slots:
                continue
        m5 = m5_cache.get(sym)
        if m5 is None:
            if (sym, "no_5m_file") not in bad:                 # counted once per listing event, not once per hourly signal
                bad.add((sym, "no_5m_file"))
                diag["events_without_5m_data"] = diag.get("events_without_5m_data", 0) + 1
            continue
        tr = simulate_trade(rules, t, m5, e["funding"], fee_pct, slip_pct, rules["stake"])
        if tr is None or "skipped" in tr:
            why = "empty" if tr is None else tr["skipped"]
            if (sym, why) not in bad:
                bad.add((sym, why))
                diag[why] = diag.get(why, 0) + 1
            continue
        traded.add(sym)
        busy_until[sym] = tr["close_date"]
        if cooldown_h and tr["exit_reason"] == "stop_loss":
            blocked_until[sym] = tr["close_date"] + timedelta(hours=cooldown_h)
        open_until.append(tr["close_date"])
        tr.update({"pair": sym[:-4] + "/USDT:USDT", "symbol": sym, "age_days": s["age_days"], "flagged": e["flagged"], "listing": _dt(e["inst"]["launch_ms"]), "status": e["inst"]["status"]})
        trades.append(tr)
    return trades


def equity_curve(trades: list[dict], wallet: float) -> list[tuple[str, float]]:
    eq, out = wallet, []
    for tr in sorted((x for x in trades if not x["open_at_end"]), key=lambda x: x["close_date"]):
        eq += tr["profit_abs"]
        out.append((tr["close_date"].strftime("%Y-%m-%dT%H:%M:%SZ"), eq))
    return out


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def row_of(tr: dict) -> dict:
    return {k: (_iso(v) if isinstance(v, datetime) else v) for k, v in tr.items()}


def summarize(trades: list[dict], wallet: float) -> dict:
    closed = [t for t in trades if not t["open_at_end"]]
    rows = [{"profit_ratio": t["profit_ratio"], "profit_abs": t["profit_abs"], "fees_abs": t["fees_abs"], "open_date": _iso(t["open_date"]), "close_date": _iso(t["close_date"])} for t in closed]
    eq = equity_curve(closed, wallet)
    st = analysis.trade_stats(rows, wallet, eq)
    rets = sorted(t["profit_ratio"] for t in closed)
    st["median_trade_pct"] = round(rets[len(rets) // 2] * 100, 4) if rets else None
    st["n_open_at_data_end"] = len(trades) - len(closed)
    st["funding_total_usd"] = round(sum(t["funding_ratio"] * (t["profit_abs"] / t["profit_ratio"] if t["profit_ratio"] else 0) for t in closed), 3) if closed else 0.0
    return st


def breakdowns(trades: list[dict], t_lo: datetime, t_hi: datetime) -> dict:
    closed = [t for t in trades if not t["open_at_end"]]
    mid = t_lo + (t_hi - t_lo) / 2

    def agg(sel):
        r = [t["profit_ratio"] for t in sel]
        return {"n": len(r), "mean_trade_pct": round(sum(r) / len(r) * 100, 4) if r else None, "net_usd": round(sum(t["profit_abs"] for t in sel), 3), "win_pct": round(sum(1 for x in r if x > 0) / len(r) * 100, 1) if r else None}
    months: dict[str, list] = {}
    for t in closed:
        months.setdefault(t["open_date"].strftime("%Y-%m"), []).append(t)
    buckets = {"2-5d": (2, 5), "5-10d": (5, 10), "10-20d": (10, 20), "20-30d": (20, 31)}
    return {"by_month": {k: agg(v) for k, v in sorted(months.items())}, "first_half": agg([t for t in closed if t["open_date"] < mid]), "second_half": agg([t for t in closed if t["open_date"] >= mid]),
            "midpoint": _iso(mid), "by_age_bucket": {k: agg([t for t in closed if lo <= t["age_days"] < hi]) for k, (lo, hi) in buckets.items()}}


def verdict_of(n: int, ci: list | None, mean_pct: float | None, first_mean: float | None, second_mean: float | None) -> dict:
    """Computed, never written by hand. edge candidate: n >= 100, CI lower bound > 0 and mean > 0 in BOTH halves. unproven: mean > 0 but one of those fails. no edge: otherwise."""
    fails = []
    if n < VERDICT_MIN_N:
        fails.append(f"n={n} < {VERDICT_MIN_N}")
    if not ci or ci[0] <= 0:
        fails.append("CI lower bound <= 0")
    if first_mean is None or first_mean <= 0:
        fails.append("first half mean <= 0")
    if second_mean is None or second_mean <= 0:
        fails.append("second half mean <= 0")
    if not fails:
        v = "edge candidate"
    elif mean_pct is not None and mean_pct > 0:
        v = "unproven"
    else:
        v = "no edge"
    return {"verdict": v, "failed_conditions": fails}


def scenario_report(rules: dict, trades: list[dict], t_lo: datetime, t_hi: datetime) -> dict:
    st = summarize(trades, rules["wallet"])
    bd = breakdowns(trades, t_lo, t_hi)
    v = verdict_of(st["n_trades"], st["ci95_pct"], st["mean_trade_pct"], bd["first_half"]["mean_trade_pct"], bd["second_half"]["mean_trade_pct"])
    closed = [t for t in trades if not t["open_at_end"]]
    worst = sorted(closed, key=lambda t: t["profit_ratio"])[:5]
    stops = [t["gap_pct"] for t in closed if t["exit_reason"] in ("stop_loss", "trailing_stop_loss")]
    eq = equity_curve(closed, rules["wallet"])
    step = max(1, len(eq) // 250)
    return {**st, **bd, **v, "worst_5": [{"pair": t["pair"], "open": _iso(t["open_date"]), "close": _iso(t["close_date"]), "ratio_pct": round(t["profit_ratio"] * 100, 3), "reason": t["exit_reason"],
                                        "gap_pct": round(t["gap_pct"], 3)} for t in worst], "worst_stop_gap_pct": round(max(stops), 3) if stops else 0.0,
            "exit_reasons": {r: sum(1 for t in closed if t["exit_reason"] == r) for r in sorted({t["exit_reason"] for t in closed})}, "equity_curve": eq[::step]}


def compare_live(replay: list[dict], live: list[dict]) -> dict:
    """Same rules, same dates: the live dry-run trades against the replay trades (matched by symbol and entry within 2 h)."""
    live = [t for t in live if t.get("open_date") and not t.get("is_open")]
    if not live:
        return {"available": False, "note": "no live listing_fade trades in the snapshot (press 'Update live reference' first)"}

    def p(x):
        return pd.Timestamp(x).tz_convert("UTC") if pd.Timestamp(x).tzinfo else pd.Timestamp(x).tz_localize("UTC")
    lo, hi = min(p(t["open_date"]) for t in live), max(p(t["open_date"]) for t in live)
    rep = [t for t in replay if not t["open_at_end"] and lo - pd.Timedelta(hours=2) <= p(t["open_date"]) <= hi + pd.Timedelta(hours=2)]
    used, pairs = set(), []
    for lt in sorted(live, key=lambda x: x["open_date"]):
        sym = lt["pair"].split("/")[0] + "USDT"
        lo_t = p(lt["open_date"])
        best, bd = None, None
        for k, r in enumerate(rep):
            if k in used or r["symbol"] != sym:
                continue
            d = abs((p(r["open_date"]) - lo_t).total_seconds())
            if d <= 7200 and (bd is None or d < bd):
                best, bd = k, d
        if best is not None:
            used.add(best)
            pairs.append((lt, rep[best]))
    diffs = [(r["profit_ratio"] - lt["profit_ratio"]) * 100 for lt, r in pairs if lt.get("profit_ratio") is not None]
    live_mean = sum(t["profit_ratio"] for t in live if t.get("profit_ratio") is not None) / len(live) * 100
    rep_mean = sum(t["profit_ratio"] for t in rep) / len(rep) * 100 if rep else None
    return {"available": True, "period": [_iso(lo.to_pydatetime()), _iso(hi.to_pydatetime())], "n_live": len(live), "n_replay": len(rep), "matched": len(pairs), "only_live": len(live) - len(pairs),
            "only_replay": len(rep) - len(pairs), "live_mean_trade_pct": round(live_mean, 4), "replay_mean_trade_pct": None if rep_mean is None else round(rep_mean, 4),
            "per_trade_diff_pp": {"median": round(float(np.median(diffs)), 4) if diffs else None, "median_abs": round(float(np.median(np.abs(diffs))), 4) if diffs else None,
                                  "mean": round(float(np.mean(diffs)), 4) if diffs else None},
            "exit_reason_agreement": round(sum(1 for lt, r in pairs if (lt.get("exit_reason") or "") == r["exit_reason"]) / len(pairs), 3) if pairs else None,
            "live_exit_reasons": {k: sum(1 for t in live if t.get("exit_reason") == k) for k in sorted({t.get("exit_reason") for t in live if t.get("exit_reason")})},
            "replay_exit_reasons": {k: sum(1 for t in rep if t["exit_reason"] == k) for k in sorted({t["exit_reason"] for t in rep})},
            "note": "replay uses taker fees 0.055% / side and the slippage scenario of the primary line; the live dry-run uses the exchange fee of the dry-run wallet (0.01%) and fills at book prices"}


# ------------------------------------------------------------------ fetch job
def lf_state() -> dict:
    """What the raw cache holds (no network): the page shows it next to the 'Fetch data' button."""
    root = lf_dir()
    inst = _read_json(root / "instruments.json") if root.is_dir() else None
    out = {"instruments": None, "hourly_files": 0, "hourly_with_data": 0, "funding_files": 0, "m5_files": 0, "last_fetch": _read_json(root / "fetch_state.json") if root.is_dir() else None}
    if inst:
        out["instruments"] = {k: inst[k] for k in ("fetched_at", "since", "n", "n_trading", "n_closed", "raw_counts")}
    for sub, key in (("hourly", "hourly_files"), ("funding", "funding_files"), ("m5", "m5_files")):
        d = root / sub
        if d.is_dir():
            out[key] = sum(1 for _ in d.glob("*.json"))
    d = root / "hourly"
    if d.is_dir():
        out["hourly_with_data"] = sum(1 for f in d.glob("*.json") if (_read_json(f) or {}).get("rows"))
    out["data_ready"] = bool(inst and out["hourly_with_data"])
    return out


def fetch_all(get, now: datetime, base=None, log=lambda s: None, progress=lambda **k: None, stop: threading.Event | None = None, pace: float = 0.12, sleep=time.sleep,
              since: str = SINCE) -> dict:
    """Instruments -> hourly candles + funding for every listing -> 5-minute candles for the trade windows. Idempotent and resumable: whatever is cached is not requested again; a
    symbol that fails is counted and skipped (the next fetch retries it)."""
    stop = stop or threading.Event()
    rules = load_rules(base)
    paced = Paced(get, pace, sleep)
    errors: list[dict] = []
    state = {"phase": "instruments", "listings_found": 0, "hourly_done": 0, "funding_done": 0, "m5_done": 0, "m5_total": 0, "api_calls": 0, "api_errors": 0, "symbol_errors": 0}

    def push(**k):
        state.update(k)
        state["api_calls"], state["api_errors"] = paced.calls, paced.errors
        progress(**state)
    since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    doc = fetch_instruments(paced, since_dt, now, rules["blacklist"])
    _write_json(lf_dir("instruments.json"), doc)
    items = doc["items"]
    push(listings_found=len(items), phase="hourly candles + funding")
    log(f"instruments: {doc['n']} listings since {since} ({doc['n_trading']} trading, {doc['n_closed']} delisted/closed), raw {doc['raw_counts']}")
    for k, inst in enumerate(items):
        if stop.is_set():
            break
        launch = inst["launch_ms"]
        try:
            hourly_rows(paced, inst["symbol"], launch, min(launch + HOURLY_DAYS * 86_400_000, _ms(now)), sleep)
            push(hourly_done=k + 1)
            funding_rows(paced, inst["symbol"], launch, min(launch + FUNDING_DAYS * 86_400_000, _ms(now)), sleep)
            push(funding_done=k + 1)
        except Exception as e:      # noqa: BLE001
            errors.append({"symbol": inst["symbol"], "stage": "hourly/funding", "error": f"{type(e).__name__}: {e}"[:160]})
            push(symbol_errors=len(errors), hourly_done=k + 1, funding_done=k + 1)
        if (k + 1) % 50 == 0:
            log(f"hourly + funding {k + 1}/{len(items)} ({paced.calls} API calls, {len(errors)} symbol errors)")
    events, uni = load_universe(rules, now)
    wins = entry_windows(events)
    push(phase="5-minute candles for the trade windows", m5_total=len(wins))
    for k, (sym, a, b) in enumerate(wins):
        if stop.is_set():
            break
        try:
            m5_rows(paced, sym, a, min(b, _ms(now)), sleep)
        except Exception as e:      # noqa: BLE001
            errors.append({"symbol": sym, "stage": "5m", "error": f"{type(e).__name__}: {e}"[:160]})
        push(m5_done=k + 1, symbol_errors=len(errors))
        if (k + 1) % 25 == 0:
            log(f"5-minute windows {k + 1}/{len(wins)}")
    result = {"finished_at": _iso(now), "stopped": stop.is_set(), "listings": len(items), "events_with_data": uni["events_with_data"], "events_without_data": uni["events_without_data"],
              "events_with_signal": len(wins), "api_calls": paced.calls, "api_errors": paced.errors, "symbol_errors": len(errors), "errors": errors[:40]}
    _write_json(lf_dir("fetch_state.json"), result)
    push(phase="stopped" if stop.is_set() else "done")
    return result


# ------------------------------------------------------------------ the study run
def survivorship(uni: dict, events: list[dict]) -> dict:
    doc = uni["instruments"]
    return {"listings_found": doc["n"], "currently_trading": doc["n_trading"], "delisted_closed_included": doc["n_closed"], "with_candle_data": uni["events_with_data"], "without_candle_data": uni["events_without_data"],
            "events_with_a_valid_signal": sum(1 for e in events if e["signals"]),
            "warning": "Survivorship bias is REDUCED, not removed: Bybit's API lists delisted perpetuals under status=Closed and they are included, but a symbol Bybit removed from the API "
                       "altogether (or listed and delisted without a Closed record) is invisible; the number of such symbols is unknown. Listings without candle data are counted above, not silently dropped."}


def run_study(run_id: str, params: dict, base=None, until: datetime | None = None, log=lambda s: None) -> dict:
    """Everything from the cache (no network). params: slippages_pct (default all four), fee_pct (default 0.055), live_trades (optional list of normalised live trades for the comparison)."""
    until = until or datetime.now(timezone.utc)
    rules = load_rules(base)
    fee = float(params.get("fee_pct", FEE_TAKER_PCT))
    slips = [float(s) for s in (params.get("slippages_pct") or SLIPPAGES_PCT)]
    if PRIMARY_SLIPPAGE not in slips:
        slips.append(PRIMARY_SLIPPAGE)
    events, uni = load_universe(rules, until)
    m5_cache: dict[str, pd.DataFrame] = {}
    missing = []
    for e in events:
        if e["signals"]:
            d = _read_json(lf_dir("m5", f"{e['inst']['symbol']}.json"))
            if d and d["rows"]:
                m5_cache[e["inst"]["symbol"]] = to_df(d["rows"])
            else:
                missing.append(e["inst"]["symbol"])
    if missing:
        log(f"5-minute data missing for {len(missing)} events with a signal (run 'Fetch data' again): they cannot be simulated")
    entries = [s["entry_time"] for e in events for s in e["signals"]]
    t_lo = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc)
    t_hi = max(entries) if entries else until
    scenarios, trades_by_key = {}, {}
    for slip in slips:
        for slots_name, slots in (("live", rules["max_open_trades"]), ("unlimited", None)):
            for excl in (True, False):
                key = f"slip{slip:g}_slots_{slots_name}_flagged_{'excluded' if excl else 'included'}"
                dg: dict = {}
                tr = run_scenario(rules, events, slip, slots, excl, fee, until, m5_cache, dg)
                trades_by_key[key] = tr
                scenarios[key] = {"slippage_pct": slip, "slots": slots_name, "flagged_events": "excluded" if excl else "included", "not_simulated": dg, **scenario_report(rules, tr, t_lo, t_hi)}
    primary = f"slip{PRIMARY_SLIPPAGE:g}_slots_live_flagged_excluded"
    opp = f"slip{PRIMARY_SLIPPAGE:g}_slots_unlimited_flagged_excluded"
    matrix = {k: {"verdict": v["verdict"], "n": v["n_trades"], "mean_trade_pct": v["mean_trade_pct"], "ci95_pct": v["ci95_pct"], "failed": v["failed_conditions"]} for k, v in scenarios.items()}
    live_cmp = compare_live(trades_by_key[f"slip{PRIMARY_SLIPPAGE:g}_slots_live_flagged_included"], params.get("live_trades") or [])
    if live_cmp.get("available"):                                          # like for like: the live bot re-enters a pair after an exit and pays the dry-run fee (0.01%), no slippage
        lo_t, hi_t = datetime.fromisoformat(live_cmp["period"][0].replace("Z", "+00:00")), datetime.fromisoformat(live_cmp["period"][1].replace("Z", "+00:00"))
        tr_like = run_scenario(rules, events, 0.0, rules["max_open_trades"], False, 0.01, until, m5_cache, {}, reentry=True, window=(lo_t - timedelta(hours=2), hi_t + timedelta(hours=2)))
        live_cmp["like_for_like"] = {**compare_live(tr_like, params.get("live_trades") or []), "setup": "same rules and dates, re-entry after an exit allowed, 3 slots, dry-run fee 0.01%, slippage 0 (the live dry-run fills at book prices)"}
        live_cmp["like_for_like"].pop("note", None)
        live_syms = {t["pair"].split("/")[0] + "USDT" for t in params.get("live_trades") or []}
        ev_live = [e for e in events if e["inst"]["symbol"] in live_syms]
        tr_pairs = run_scenario(rules, ev_live, 0.0, rules["max_open_trades"], False, 0.01, until, m5_cache, {}, reentry=True, window=(lo_t - timedelta(hours=2), hi_t + timedelta(hours=2)))
        live_cmp["like_for_like_live_pairs"] = {**compare_live(tr_pairs, params.get("live_trades") or []),
                                                "setup": "as like_for_like, restricted to the pairs the live bot actually traded: this removes the effect of the live pair list (VolumePairList top 200, SpreadFilter), which the "
                                                         "replay cannot rebuild and which decides which pairs compete for the 3 slots"}
        live_cmp["like_for_like_live_pairs"].pop("note", None)
    n_flagged = sum(1 for e in events if e["flagged"])
    p = scenarios[primary]
    line = (f"Listing Fade study {run_id}: verdict {p['verdict']} (0.3% slippage, live limits {rules['max_open_trades']} slots, liquidity-flagged excluded): n={p['n_trades']}, "
            f"mean {p['mean_trade_pct']}%, CI95 {p['ci95_pct']}, halves {p['first_half']['mean_trade_pct']}% / {p['second_half']['mean_trade_pct']}%; "
            f"opportunity set (unlimited slots): {scenarios[opp]['verdict']}, n={scenarios[opp]['n_trades']}, mean {scenarios[opp]['mean_trade_pct']}%. "
            f"{uni['instruments']['n']} listings ({uni['instruments']['n_closed']} delisted included). Survivorship bias reduced, not removed.")
    fixed = {"fee_taker_pct_per_side": fee, "slippages_pct": slips, "primary": primary, "opportunity_set": opp,
             "liquidity_flag": f"flagged when the live stake is >= {LIQUIDITY_STAKE_SHARE * 100:g}% of the first hour's USDT volume (volume too thin for the stake; fine when stake < 1% of it)",
             "verdict": f"edge candidate: n >= {VERDICT_MIN_N}, bootstrap CI lower bound > 0 and mean > 0 in BOTH halves of the date range (basis: primary line); unproven: mean > 0 but a condition fails; no edge: otherwise",
             "stop_fill": "a stop / trailing trigger fills at its level, or at the candle's open when the 5-minute candle gaps through it; slippage on top",
             "funding": "credited / charged at every funding timestamp inside the hold, notional = stake (a short receives a positive rate)", "entry": "open of the candle after the signal candle"}
    report = {"run_id": run_id, "type": "listing_fade_study", "banner": "Rule-based historical study, $0, no LLM. A positive result here is a hypothesis for the live dry-run, not proof; `edge` is never given by replay code.",
              "rules": rules_summary(rules), "fixed_parameters": fixed, "universe": survivorship(uni, events), "liquidity_flagged_events": n_flagged, "events_missing_5m": missing[:20], "not_simulated_primary": scenarios[primary]["not_simulated"],
              "date_range": {"from": _iso(t_lo), "to": _iso(t_hi)}, "primary": primary, "verdict": p["verdict"], "verdict_failed_conditions": p["failed_conditions"],
              "opportunity_set_verdict": scenarios[opp]["verdict"], "scenarios": scenarios, "verdict_matrix": matrix, "live_comparison": live_cmp, "summary_line": line,
              "no_lookahead": "signals use candles up to and including the signal candle (cummax / ewm are causal), entries fill at the next candle's open, funding and price filter use values at or before the signal's close; "
                              "fixture-tested by shuffling all future candles",
              "limits": rules["not_applied"] + ["the live entry is a passive limit order that can expire unfilled (not modelled; the slippage scenarios stand in for it)",
                                                 "funding history granularity is whatever Bybit returns (8 h, some symbols 4 h / 1 h)", "survivorship bias reduced, not removed (see universe.warning)"]}
    return {"report": report, "trades": trades_by_key}


def to_markdown(rep: dict) -> str:
    L = [f"# Listing Fade study {rep['run_id']}", "", f"**{rep['banner']}**", "", f"## Verdict: `{rep['verdict']}`", "",
         f"Basis: {rep['primary']}. Failed conditions: {', '.join(rep['verdict_failed_conditions']) or 'none'}. Opportunity set (unlimited slots): `{rep['opportunity_set_verdict']}`.", "", f"`{rep['summary_line']}`", "",
         "## Rules (read from the live files, nothing re-tuned)", "", "```", json.dumps(rep["rules"], indent=1), "```", "", "## Universe and survivorship", "", json.dumps(rep["universe"], indent=1), "",
         f"Liquidity-flagged events (stake >= 1% of first-hour volume): {rep['liquidity_flagged_events']}", "", "## Scenarios", "",
         "| scenario | trades | mean % | median % | win % | CI95 % | net $ | max DD % | first half % | second half % | worst stop gap % | verdict |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for k, s in rep["scenarios"].items():
        L.append(f"| {k} | {s['n_trades']} | {s['mean_trade_pct']} | {s['median_trade_pct']} | {s['win_pct']} | {s['ci95_pct']} | {s['net_usd']} | {s['max_drawdown_pct']} | {s['first_half']['mean_trade_pct']} | "
                 f"{s['second_half']['mean_trade_pct']} | {s['worst_stop_gap_pct']} | {s['verdict']} |")
    p = rep["scenarios"][rep["primary"]]
    L += ["", f"Signals that could NOT be simulated in the primary line (missing 5-minute data): {rep['not_simulated_primary'] or 'none'}"]
    L += ["", "## Primary line: by month", "", "| month | n | mean % | net $ | win % |", "|---|---|---|---|---|"] + [f"| {m} | {v['n']} | {v['mean_trade_pct']} | {v['net_usd']} | {v['win_pct']} |" for m, v in p["by_month"].items()]
    L += ["", "## Primary line: by listing age at entry", "", "| age | n | mean % | net $ |", "|---|---|---|---|"] + [f"| {m} | {v['n']} | {v['mean_trade_pct']} | {v['net_usd']} |" for m, v in p["by_age_bucket"].items()]
    L += ["", "## Primary line: worst 5 trades", ""] + [f"- {w['pair']} {w['open']} -> {w['close']}: {w['ratio_pct']}% ({w['reason']}, gap {w['gap_pct']}%)" for w in p["worst_5"]]
    L += ["", "## Against the live dry-run trades", "", json.dumps(rep["live_comparison"], indent=1), "", "## Limits", ""] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, params: dict, out: dict) -> None:
    import csv
    import io
    rep, trades = out["report"], out["trades"]

    def w(name: str, text: str):
        paths.ensure_parent(paths.replay_path(run_id, name)).write_text(text, encoding="utf-8")
    w("params.json", json.dumps({**{k: v for k, v in params.items() if k != "live_trades"}, "type": "listing_fade_study", "n_live_trades_compared": len(params.get("live_trades") or [])}, indent=1, default=str))
    w("report.json", json.dumps(rep, indent=1, default=str))
    w("report.md", to_markdown(rep))
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["scenario", "pair", "status", "open_date", "close_date", "open_rate", "close_rate", "exit_reason", "price_ratio", "funding_ratio", "profit_ratio", "profit_abs", "gap_pct", "age_days", "flagged"])
    for key, tr in trades.items():
        for t in tr:
            cw.writerow([key, t["pair"], t["status"], _iso(t["open_date"]), _iso(t["close_date"]), t["open_rate"], t["close_rate"], t["exit_reason"], round(t["price_ratio"], 6), round(t["funding_ratio"], 6),
                         round(t["profit_ratio"], 6), round(t["profit_abs"], 4), round(t["gap_pct"], 4), round(t["age_days"], 2), t["flagged"]])
    w("trades.csv", buf.getvalue())
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["scenario", "ts", "equity"])
    for key, s in rep["scenarios"].items():
        for ts, e in s["equity_curve"]:
            cw.writerow([key, ts, round(e, 4)])
    w("equity.csv", buf.getvalue())
