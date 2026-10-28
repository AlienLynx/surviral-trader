"""
Replay data: Bybit public klines (cached under REPLAY_DIR/cache), the Fear & Greed history, and the as-of series store every replayed strategy reads from.

As-of rule (the look-ahead guard): at replay time `t` a candle is visible only if it has CLOSED by t, i.e. open_time + interval <= t. SeriesStore.asof and the
HTTP shim in sandbox.py both go through the same function, so a strategy cannot see a future candle by any route.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import paths

BYBIT = "https://api.bybit.com"
FNG_URL = "https://api.alternative.me/fng/"
STABLE_RE = re.compile(r"^(USDC|USDE|TUSD|FDUSD|DAI|EUR|PYUSD|USD1|USDD)$")
LEVERAGED_RE = re.compile(r"(2L|2S|3L|3S|4L|4S|5L|5S)$")
HOUR_MS = 3_600_000
COLUMNS = ["date", "open", "high", "low", "close", "volume", "quote"]


def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def bybit_symbol(pair: str) -> str:
    return pair.split(":")[0].replace("/", "")


def pair_name(coin: str, category: str) -> str:
    return f"{coin}/USDT:USDT" if category == "linear" else f"{coin}/USDT"


def category_of(pair: str) -> str:
    return "linear" if ":" in pair else "spot"


def _get_json(get, url: str, params: dict | None = None, retries: int = 4, sleep=time.sleep):
    last = None
    for i in range(retries):
        try:
            r = get(url, params=params, timeout=20)
            if getattr(r, "status_code", 200) in (429, 500, 502, 503):
                raise RuntimeError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except Exception as e:      # noqa: BLE001
            last = e
            sleep(min(2 ** i, 20))
    raise RuntimeError(f"GET {url} failed: {last}")


def fetch_klines(category: str, symbol: str, start_ms: int, end_ms: int, get, sleep=time.sleep, interval: str = "60") -> list[list[float]]:
    """Klines (hourly by default, interval "1" = one-minute) in [start_ms, end_ms], ascending: [open_ms, o, h, l, c, volume, quote_turnover]."""
    out: dict[int, list[float]] = {}
    end = end_ms
    for _ in range(200):
        data = _get_json(get, f"{BYBIT}/v5/market/kline", {"category": category, "symbol": symbol, "interval": interval, "start": start_ms, "end": end, "limit": 1000}, sleep=sleep)
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            break
        for r in rows:
            out[int(r[0])] = [int(r[0])] + [float(x) for x in r[1:7]]
        oldest = min(int(r[0]) for r in rows)
        # Bybit sometimes returns a page SHORTER than the limit although older candles exist (a quiet period, a halt): only an EMPTY page or reaching `start` ends the paging
        if oldest <= start_ms or oldest >= end:
            break
        end = oldest - 1
    return [out[k] for k in sorted(out)]


def fetch_funding(symbol: str, start_ms: int, end_ms: int, get, sleep=time.sleep) -> list[list[float]]:
    """Perpetual funding history of one symbol in [start_ms, end_ms], ascending: [settlement_ms, rate]. Public endpoint, no key. Paging ends on an empty page or when the start is reached."""
    out: dict[int, float] = {}
    end = end_ms
    for _ in range(100):
        data = _get_json(get, f"{BYBIT}/v5/market/funding/history", {"category": "linear", "symbol": symbol, "startTime": start_ms, "endTime": end, "limit": 200}, sleep=sleep)
        rows = (data.get("result") or {}).get("list") or []
        if not rows:
            break
        for r in rows:
            out[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest <= start_ms or oldest >= end:
            break
        end = oldest - 1
    return [[k, out[k]] for k in sorted(out)]


def load_funding(symbol: str, start: datetime, end: datetime, get, sleep=time.sleep) -> list[list[float]]:
    """Cached like the candles: the cache file holds the rows and the range whose paging completed; only what lies outside is fetched."""
    p = paths.ensure_parent(paths.cache_path(f"funding/linear_{symbol}.json"))
    st = {"rows": [], "from": None, "to": None}
    if p.is_file():
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            pass
    s_ms, e_ms = to_ms(start), to_ms(end)
    rows = st["rows"]
    extra: list[list[float]] = []
    if st["from"] is None:
        extra += fetch_funding(symbol, s_ms, e_ms, get, sleep)
        st["from"], st["to"] = s_ms, e_ms
    else:
        if s_ms < st["from"]:
            extra += fetch_funding(symbol, s_ms, st["from"] - 1, get, sleep)
            st["from"] = s_ms
        if e_ms > st["to"] + 8 * HOUR_MS:
            extra += fetch_funding(symbol, st["to"] + 1, e_ms, get, sleep)
            st["to"] = e_ms
    if extra:
        merged = {int(r[0]): r for r in rows + extra}
        st["rows"] = [merged[k] for k in sorted(merged)]
        p.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
    return [r for r in st["rows"] if s_ms <= r[0] <= e_ms]


def add_funding_to(store, symbol: str, start: datetime, end: datetime, get, log=lambda s: None, sleep=time.sleep) -> bool:
    """Funding history of a perpetual into the store (as-of readable by the replayed strategy). A failure only costs the funding line / funding veto of that pair: logged, never fatal."""
    try:
        rows = load_funding(symbol, start, end, get, sleep)
    except Exception as e:      # noqa: BLE001
        log(f"funding linear {symbol}: unavailable ({type(e).__name__}) -- no funding line / funding veto for that pair")
        return False
    store.add_funding("linear", symbol, rows)
    log(f"funding linear {symbol}: {len(rows)} settlements")
    return bool(rows)


def to_frame(rows: list[list[float]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(rows, columns=["ms", "open", "high", "low", "close", "volume", "quote"])
    df["date"] = pd.to_datetime(df["ms"], unit="ms", utc=True)
    return df[COLUMNS].drop_duplicates("date").sort_values("date").reset_index(drop=True)


def load_candles(category: str, symbol: str, start: datetime, end: datetime, get, sleep=time.sleep, interval: str = "60") -> pd.DataFrame:
    """Cached: only the missing head/tail of the requested range is fetched. interval "60" (default) or "1" (minute candles, own cache file)."""
    step = HOUR_MS if interval == "60" else 60_000
    folder = "candles" if interval == "60" else "candles1m"
    p = paths.ensure_parent(paths.cache_path(f"{folder}/{category}_{symbol}.json"))
    have: list[list[float]] = []
    if p.is_file():
        try:
            have = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            have = []
    s_ms, e_ms = to_ms(start), to_ms(end)
    lo = have[0][0] if have else None
    hi = have[-1][0] if have else None
    fetched: list[list[float]] = []
    if not have or s_ms < lo:
        fetched += fetch_klines(category, symbol, s_ms, (lo - 1) if have else e_ms, get, sleep, interval)
    if have and e_ms > hi + step:
        fetched += fetch_klines(category, symbol, hi + step, e_ms, get, sleep, interval)
    if fetched:
        merged = {int(r[0]): r for r in have + fetched}
        have = [merged[k] for k in sorted(merged)]
        p.write_text(json.dumps(have, separators=(",", ":")), encoding="utf-8")
    sel = [r for r in have if s_ms <= r[0] <= e_ms]
    return to_frame(sel)


def top_coins(category: str, n: int, get, exclude: set[str] | None = None) -> list[str]:
    """Today's most liquid USDT coins on Bybit (a single fixed snapshot -- NOT a day-by-day volume list; the report says so)."""
    data = _get_json(get, f"{BYBIT}/v5/market/tickers", {"category": category})
    rows = (data.get("result") or {}).get("list") or []
    coins = []
    for r in rows:
        sym = r.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if not coin or STABLE_RE.match(coin) or LEVERAGED_RE.search(coin) or coin in (exclude or set()):
            continue
        try:
            coins.append((float(r.get("turnover24h") or 0.0), coin))
        except ValueError:
            continue
    coins.sort(reverse=True)
    return [c for _, c in coins[:n]]


def fetch_spreads(category: str, get) -> dict[str, float]:
    """Relative bid-ask spread per symbol from Bybit's CURRENT tickers ((ask - bid) / mid). The history of the book is not public, so today's spread stands in for the past one: it tells
    a thin new listing (0.3 %+) from a major (0.001 %). Cached for the day under REPLAY_DIR/cache."""
    p = paths.ensure_parent(paths.cache_path(f"spreads_{category}.json"))
    if p.is_file() and time.time() - p.stat().st_mtime < 24 * 3600:
        try:
            return {k: float(v) for k, v in json.loads(p.read_text(encoding="utf-8")).items()}
        except ValueError:
            pass
    data = _get_json(get, f"{BYBIT}/v5/market/tickers", {"category": category})
    out: dict[str, float] = {}
    for r in (data.get("result") or {}).get("list") or []:
        try:
            bid, ask = float(r.get("bid1Price") or 0), float(r.get("ask1Price") or 0)
        except ValueError:
            continue
        if bid > 0 and ask >= bid:
            out[r["symbol"]] = (ask - bid) / ((ask + bid) / 2)
    p.write_text(json.dumps(out), encoding="utf-8")
    return out


# ------------------------------------------------------------------ Fear & Greed
def fng_history(get) -> list[tuple[int, int, str]]:
    p = paths.ensure_parent(paths.cache_path("fng_history.json"))
    if p.is_file() and time.time() - p.stat().st_mtime < 6 * 3600:
        try:
            return [tuple(x) for x in json.loads(p.read_text(encoding="utf-8"))]
        except ValueError:
            pass
    data = _get_json(get, FNG_URL, {"limit": 0, "format": "json"})
    hist = sorted((int(d["timestamp"]), int(d["value"]), str(d["value_classification"])) for d in data.get("data", []))
    p.write_text(json.dumps(hist), encoding="utf-8")
    return hist


def fng_asof(hist: list[tuple[int, int, str]], t: datetime) -> tuple[int, str] | None:
    """The index published at or before t (its timestamp is the day it applies from)."""
    ts = t.astimezone(timezone.utc).timestamp()
    best = None
    for h in hist:
        if h[0] <= ts:
            best = h
        else:
            break
    return None if best is None else (best[1], best[2])


# ---------------------------------------------------------------- the as-of store
INTERVAL_HOURS = {"60": 1, "240": 4, "D": 24}


def _ns(t) -> int:
    """UTC nanoseconds of a timestamp, independent of the pandas datetime unit (pandas 3 defaults to microseconds, pandas 2 to nanoseconds)."""
    ts = pd.Timestamp(t)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.as_unit("ns").value)


def _ns_array(series: pd.Series) -> np.ndarray:
    s = series.dt.tz_convert("UTC") if series.dt.tz is not None else series.dt.tz_localize("UTC")
    return s.dt.as_unit("ns").astype("int64").to_numpy()


class SeriesStore:
    """(category, symbol) -> hourly DataFrame. Everything a replayed strategy can read goes through asof()/klines_asof()."""

    def __init__(self):
        self.frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._ends: dict[tuple[str, str], np.ndarray] = {}
        self.spreads: dict[tuple[str, str], float] = {}                          # relative bid-ask spread per (category, symbol), see fetch_spreads
        self.fine: dict[tuple[str, str], dict[str, np.ndarray]] = {}       # one-minute candles (calibration only): open times in ns + o/h/l/c
        self.funding: dict[tuple[str, str], list[list[float]]] = {}        # perpetual funding history per (category, symbol): [settlement_ms, rate], ascending

    def add(self, category: str, symbol: str, df: pd.DataFrame) -> None:
        df = df.reset_index(drop=True)
        self.frames[(category, symbol)] = df
        self._ends[(category, symbol)] = _ns_array(df["date"] + pd.Timedelta(hours=1)) if len(df) else np.array([], dtype="int64")

    def add_fine(self, category: str, symbol: str, df: pd.DataFrame) -> None:
        """One-minute candles. Used ONLY for the price at an exact moment (entry / exit fill), never for what a strategy is allowed to see."""
        if len(df):
            self.fine[(category, symbol)] = {"ns": _ns_array(df["date"]), "o": df["open"].to_numpy(float), "h": df["high"].to_numpy(float), "l": df["low"].to_numpy(float),
                                             "c": df["close"].to_numpy(float)}

    def add_funding(self, category: str, symbol: str, rows: list[list[float]]) -> None:
        if rows:
            self.funding[(category, symbol)] = sorted(rows)

    def funding_asof(self, category: str, symbol: str, t: datetime, limit: int = 200) -> list[list[float]]:
        """Funding settlements that had HAPPENED by t, newest first (at most `limit`). Empty when the store has no funding for the symbol."""
        rows = self.funding.get((category, symbol))
        if not rows:
            return []
        t_ms = to_ms(t)
        out = [r for r in rows if r[0] <= t_ms]
        return out[::-1][:limit]

    def price_at(self, category: str, symbol: str, t: datetime, max_gap_s: float = 150.0) -> float | None:
        """The price at moment t: the open of the one-minute candle that contains t (None when there is no minute data within `max_gap_s` seconds)."""
        f = self.fine.get((category, symbol))
        if f is None:
            return None
        ns = f["ns"]
        i = int(np.searchsorted(ns, _ns(t), side="right")) - 1
        if i < 0 or (_ns(t) - int(ns[i])) / 1e9 > max_gap_s:
            return None
        return float(f["o"][i])

    def minutes(self, category: str, symbol: str, t0: datetime, t1: datetime) -> list[tuple[datetime, float, float, float]]:
        """One-minute candles that OPENED in [t0, t1): (close time, high, low, close), ascending. Empty when there is no minute data for the range."""
        f = self.fine.get((category, symbol))
        if f is None:
            return []
        ns = f["ns"]
        a, b = int(np.searchsorted(ns, _ns(t0), side="left")), int(np.searchsorted(ns, _ns(t1), side="left"))
        return [(pd.Timestamp(int(ns[i]) + 60_000_000_000, tz="UTC").to_pydatetime(), float(f["h"][i]), float(f["l"][i]), float(f["c"][i])) for i in range(a, b)]

    def spread(self, category: str, symbol: str) -> float:
        return float(self.spreads.get((category, symbol), 0.0))

    def has(self, category: str, symbol: str) -> bool:
        return (category, symbol) in self.frames and len(self.frames[(category, symbol)]) > 0

    def asof(self, category: str, symbol: str, t: datetime, n: int | None = None) -> pd.DataFrame:
        """Only candles that have CLOSED by t (open + 1h <= t). This is the single look-ahead guard every reader goes through."""
        df = self.frames.get((category, symbol))
        if df is None or df.empty:
            return pd.DataFrame(columns=COLUMNS)
        idx = int(np.searchsorted(self._ends[(category, symbol)], _ns(t), side="right"))
        lo = max(0, idx - n) if n else 0
        return df.iloc[lo:idx].reset_index(drop=True)

    def klines_asof(self, category: str, symbol: str, interval: str, limit: int, t: datetime) -> list[list[str]]:
        """Bybit-shaped response rows (newest first) for interval 60 / 240 / D, complete bars only, as of t."""
        hours = INTERVAL_HOURS.get(str(interval))
        if hours is None:
            raise ValueError(f"interval {interval!r} not served by replay")
        base = self.asof(category, symbol, t, limit * hours + 2 * hours)
        if base.empty:
            return []
        if hours > 1:
            base = base.copy()
            bucket = base["date"].dt.floor(f"{hours}h")
            g = base.groupby(bucket)
            agg = pd.DataFrame({"date": list(g.groups.keys()), "open": g["open"].first().values, "high": g["high"].max().values, "low": g["low"].min().values,
                                "close": g["close"].last().values, "volume": g["volume"].sum().values, "quote": g["quote"].sum().values, "n": g.size().values})
            agg = agg[(agg["n"] == hours) & (agg["date"] + pd.Timedelta(hours=hours) <= pd.Timestamp(t))]
            base = agg.drop(columns=["n"])
        rows = base.tail(limit).iloc[::-1]
        return [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in rows.itertuples()]

    def turnover_rank(self, category: str, symbols: list[str], t: datetime) -> list[str]:
        """Symbols ordered by trailing-24h quote turnover as of t (what a VolumePairList would have ranked)."""
        score = {}
        for s in symbols:
            df = self.asof(category, s, t, 24)
            score[s] = float(df["quote"].sum()) if not df.empty else 0.0
        return sorted(symbols, key=lambda s: (-score[s], s))
