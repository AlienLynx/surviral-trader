"""
P25-E: LightGBM walk-forward study v2 -- the same question as lgbm_study (can a small model rank liquid USDT perpetuals well enough to earn money AFTER costs?) with the three things the first run lacked:
more history (Binance USDT-M perpetuals since 2021, free public archive data.binance.vision, delisted contracts included), a wider universe (top 40 instead of top 20) and hourly features (order flow, volume and
trade-count surges, intraday range, volatility, funding, BTC regime). $0, CPU only, no key, no LLM, nothing is traded.

Pre-registered design (fixed before the run; nothing is tuned on the out-of-sample period):
  data       Binance USDT-M 1h klines (with taker-buy volume) and funding, monthly archive files, plus the exchange API for the months the archive has not published yet; cached and resumable
  universe   the top N=40 perpetuals by trailing 30-day turnover AS OF each day (delisted contracts included while they traded)
  features   known at 00:00 UTC of day k (bars up to 23:00 of the day before): returns over 6/12/24/72/168/336 hours, hourly volatility over 24/168 h, taker buy/sell imbalance (24 h, 72 h), volume and
             trade-count surge against the last week, 24 h high-low range, distance from the 168 h high and low, funding (last day, last 7 days), BTC 24 h / 168 h return and 168 h volatility, market
             return and dispersion of the universe; each coin feature also as its rank inside the universe that day
  label      the coin's return over the next HOLD days (minus funding) minus the universe mean for the same days (market neutral)
  model      LightGBM regressor, FIXED parameters (no search); expanding window retrained every RETRAIN_DAYS=56 days on rows whose label is already known (purged); the first MIN_TRAIN_DAYS=365 days only train
  book       every HOLD days long the top quintile (8 coins), short the bottom quintile of the PREDICTION, half of the sleeve per leg, taker fee 0.055% + slippage 0.10% per side (0.30% stress)
  primary    HOLD = 3 days (the cost of a daily turnover is too high for a taker book); HOLD = 1 day is shown for context and cannot rescue a failed primary
  placebo    the same pipeline trained on labels shuffled inside each day, PLACEBO_SEEDS runs
  baselines  the one-line rules 14-day momentum and 3-day reversal, same book, same days: the model has to beat both
  extra      the stress scenario (0.30% slippage) has to stay positive: costs are the usual killer
Known limits (in every report): daily marking of a 3-day hold (no intraday stop or liquidation model), a coin that vanishes is held flat, Binance perpetuals only, ~5 years are a few regimes, not all.
"""
from __future__ import annotations

import bisect
import csv
import io
import json
import re
import threading
import time
import warnings
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np

from . import carry, lgbm_study as lg, listing_fade as lf, momentum as mo, paths

STUDY_ID = "binance"
VISION = "https://data.binance.vision"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
FAPI = "https://fapi.binance.com"
KLINE_DIR = "data/futures/um/monthly/klines/"
FUND_DIR = "data/futures/um/monthly/fundingRate/"
SINCE = "2021-01-01"
LOCK_END = "2024-07-01"
DAY = carry.DAY_MS
HOUR = 3_600_000
UNIVERSE_N = 40
QUINTILE = 8
HOLD = 3
RETRAIN_DAYS = 56
MIN_TRAIN_DAYS = 365
PLACEBO_SEEDS = 30
PARAMS = {"objective": "regression", "num_leaves": 15, "n_estimators": 200, "learning_rate": 0.03, "min_child_samples": 200, "subsample": 0.7, "subsample_freq": 1, "colsample_bytree": 0.6,
          "reg_lambda": 10.0, "verbose": -1, "n_jobs": 6, "random_state": 7}
BASE_FEATURES = ["ret6", "ret12", "ret24", "ret72", "ret168", "ret336", "vol24", "vol168", "imb24", "imb72", "vol_surge", "trade_surge", "range24", "dist_hi168", "dist_lo168", "fund1", "fund7"]
FEATURES = BASE_FEATURES + ["btc_ret24", "btc_ret168", "btc_vol168", "mkt_ret24", "mkt_disp24"] + [f"rk_{f}" for f in BASE_FEATURES]
PASS = {**lg.PASS, "min_stress_apr_pct": 0.0, "min_clipped_oos_apr_pct": 10.0}      # the clipped-returns condition was added after the first real run showed how fragile the baseline is; it can only tighten the verdict


# ------------------------------------------------------------------ cache
def cdir(*parts: str):
    name = "/".join(parts)
    return paths.replay_path(STUDY_ID, name) if name else paths.replay_path(STUDY_ID)


def months_between(start: datetime, end: datetime) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _ts_ms(v: float) -> int:
    v = int(float(v))
    return v // 1000 if v > 10 ** 14 else v          # some archive files use microseconds


def _csv_rows(raw: bytes) -> list[list[str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        text = z.read(z.namelist()[0]).decode("utf-8")
    return [r for r in csv.reader(io.StringIO(text)) if r and r[0].strip().lstrip("-").isdigit()]       # drops the header row some files have


def _s3_list(get, prefix: str, delimiter: str | None = "/") -> tuple[list[str], list[str]]:
    """All common prefixes and keys under a prefix of the public archive bucket."""
    prefixes, keys, marker = [], [], ""
    for _ in range(50):
        params = {"prefix": prefix, "max-keys": 1000}
        if delimiter:
            params["delimiter"] = delimiter
        if marker:
            params["marker"] = marker
        r = get(S3, params=params, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"archive listing HTTP {r.status_code}")
        xml = r.text
        prefixes += re.findall(r"<CommonPrefixes><Prefix>([^<]+)</Prefix></CommonPrefixes>", xml)
        keys += re.findall(r"<Key>([^<]+)</Key>", xml)
        if "<IsTruncated>true</IsTruncated>" not in xml:
            break
        nxt = re.search(r"<NextMarker>([^<]+)</NextMarker>", xml)
        marker = nxt.group(1) if nxt else (keys[-1] if keys else (prefixes[-1] if prefixes else ""))
    return prefixes, keys


def list_symbols(get) -> list[str]:
    prefixes, _ = _s3_list(get, KLINE_DIR)
    names = [p[len(KLINE_DIR):].strip("/") for p in prefixes]
    return sorted(n for n in names if n.endswith("USDT") and "_" not in n)


def list_months(get, sym: str) -> list[str]:
    _, keys = _s3_list(get, f"{KLINE_DIR}{sym}/1h/", None)
    return sorted(m.group(1) for k in keys if (m := re.search(r"-1h-(\d{4}-\d{2})\.zip$", k)))


def trading_symbols(get) -> set[str]:
    try:
        r = get(f"{FAPI}/fapi/v1/exchangeInfo", params=None, timeout=60)
        if r.status_code != 200:
            return set()
        return {s["symbol"] for s in r.json().get("symbols", []) if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT"}
    except Exception:      # noqa: BLE001
        return set()


def _save(p, arr: np.ndarray) -> None:
    paths.ensure_parent(p)
    with open(p, "wb") as fh:
        np.save(fh, arr)


def fetch_kline_month(get, sym: str, ym: str) -> bool:
    """One month of 1h klines into the cache (ts, open, high, low, close, quote volume, trades, taker-buy quote volume). False when the archive has no file."""
    p = cdir("k", sym, f"{ym}.npy")
    if p.is_file():
        return True
    if cdir("k", sym, f"{ym}.none").is_file():
        return False
    r = get(f"{VISION}/{KLINE_DIR}{sym}/1h/{sym}-1h-{ym}.zip", params=None, timeout=120)
    if r.status_code == 404:
        paths.ensure_parent(cdir("k", sym, f"{ym}.none")).write_text("", encoding="utf-8")
        return False
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {sym} {ym}")
    rows = _csv_rows(r.content)
    arr = np.array([[_ts_ms(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[7]), float(x[8]), float(x[10])] for x in rows], dtype=np.float64).reshape(-1, 8)
    _save(p, arr)
    return True


def fetch_funding_month(get, sym: str, ym: str) -> bool:
    p = cdir("f", sym, f"{ym}.npy")
    if p.is_file():
        return True
    if cdir("f", sym, f"{ym}.none").is_file():
        return False
    r = get(f"{VISION}/{FUND_DIR}{sym}/{sym}-fundingRate-{ym}.zip", params=None, timeout=120)
    if r.status_code == 404:
        paths.ensure_parent(cdir("f", sym, f"{ym}.none")).write_text("", encoding="utf-8")
        return False
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} funding {sym} {ym}")
    arr = np.array([[_ts_ms(x[0]), float(x[2])] for x in _csv_rows(r.content)], dtype=np.float64).reshape(-1, 2)
    _save(p, arr)
    return True


def fetch_tail(get, sym: str, from_ms: int, now_ms: int) -> None:
    """The months the archive has not published yet, from the exchange API (overwritten on every run). Only for contracts that still trade."""
    rows, t = [], from_ms
    for _ in range(12):
        r = get(f"{FAPI}/fapi/v1/klines", params={"symbol": sym, "interval": "1h", "startTime": t, "limit": 1500}, timeout=60)
        if r.status_code != 200:
            break
        part = r.json()
        if not part:
            break
        rows += [[x[0], x[1], x[2], x[3], x[4], x[7], x[8], x[10]] for x in part]
        t = int(part[-1][0]) + HOUR
        if t >= now_ms or len(part) < 1500:
            break
    _save(cdir("k", sym, "tail.npy"), np.array(rows, dtype=np.float64).reshape(-1, 8))
    frows, t = [], from_ms
    for _ in range(6):
        r = get(f"{FAPI}/fapi/v1/fundingRate", params={"symbol": sym, "startTime": t, "limit": 1000}, timeout=60)
        if r.status_code != 200:
            break
        part = r.json()
        if not part:
            break
        frows += [[x["fundingTime"], x["fundingRate"]] for x in part]
        t = int(part[-1]["fundingTime"]) + 1
        if len(part) < 1000:
            break
    _save(cdir("f", sym, "tail.npy"), np.array(frows, dtype=np.float64).reshape(-1, 2))


def fetch_all(get, now: datetime, log=lambda s: None, progress=lambda **k: None, stop: threading.Event | None = None, pace: float = 0.05, sleep=time.sleep, workers: int = 6) -> dict:
    """Downloads the monthly archive files of every USDT perpetual (delisted ones too) that traded after SINCE, plus the exchange API tail. Cached, resumable, polite."""
    pc = lf.Paced(get, min(pace, 0.03), sleep)
    since = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    syms = list_symbols(pc)
    live = trading_symbols(pc)
    log(f"archive: {len(syms)} USDT perpetuals listed, {len(live)} trade now")
    errors, done, lock = [0], [0], threading.Lock()
    kept: list[str] = []

    def one(sym):
        if stop is not None and stop.is_set():
            return
        try:
            avail = [m for m in list_months(pc, sym) if m >= since.strftime("%Y-%m")]
            for ym in avail:
                if stop is not None and stop.is_set():
                    return
                fetch_kline_month(pc, sym, ym)
                fetch_funding_month(pc, sym, ym)
            if avail or sym in live:
                last = avail[-1] if avail else None
                nxt = (datetime.strptime(last + "-01", "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=32)).replace(day=1) if last else since
                if sym in live and int(nxt.timestamp() * 1000) < now_ms:
                    fetch_tail(pc, sym, int(nxt.timestamp() * 1000), now_ms)
                with lock:
                    kept.append(sym)
        except Exception as e:      # noqa: BLE001
            with lock:
                errors[0] += 1
            log(f"{sym}: {type(e).__name__}: {e}")
        with lock:
            done[0] += 1
            progress(step=done[0], total=len(syms), api_calls=pc.calls, symbol_errors=errors[0])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, syms))
    stopped = bool(stop is not None and stop.is_set())
    if not stopped:
        lf._write_json(cdir("symbols.json"), {"fetched_at": now_ms, "symbols": sorted(kept)})
    return {"stopped": stopped, "symbols": len(syms), "kept": len(kept), "errors": errors[0], "calls": pc.calls}


# ------------------------------------------------------------------ cache -> dense matrices
def _read_sym(sym: str, since_month: str | None = None):
    """Hourly klines [n, 8] sorted and de-duplicated, and funding [m, 2], from the cache (monthly files, then the API tail). since_month ('YYYY-MM') skips older monthly files (a daily run
    only needs the last weeks); the API tail is always read."""
    def cat(kind, width):
        parts = []
        d = cdir(kind, sym)
        for f in sorted(d.glob("*.npy")) if d.is_dir() else []:
            if since_month and f.stem != "tail" and f.stem < since_month:
                continue
            a = np.load(f)
            if a.size:
                parts.append(a.reshape(-1, width))
        if not parts:
            return np.zeros((0, width))
        a = np.vstack(parts)
        a = a[np.argsort(a[:, 0], kind="stable")]
        keep = np.r_[a[1:, 0] != a[:-1, 0], True]
        return a[keep]
    return cat("k", 8), cat("f", 2)


def build(start: datetime, end: datetime, universe_n: int = UNIVERSE_N, log=lambda s: None, since_month: str | None = None):
    """days (ms), symbols, hourly matrices {o,hi,lo,qv,n,tb} [S, H], daily O [S, D+1] / T [S, D+1] / X [S, D] / F [S, D]. Only symbols that are ever in the as-of top-N universe are kept."""
    names = (lf._read_json(cdir("symbols.json")) or {}).get("symbols") or sorted(p.name for p in cdir("k").iterdir() if p.is_dir())
    d0, d1 = carry.day_start(start) - 40 * DAY, carry.day_start(end)
    days = list(range(d0, d1 + DAY + 1, DAY))
    D = len(days) - 1
    Hn = (D + 1) * 24
    # pass 1: daily turnover of every symbol -> who is ever in the universe
    raw = {}
    Tall = np.full((len(names), D + 1), np.nan)
    Oall = np.full((len(names), D + 1), np.nan)
    for i, sym in enumerate(names):
        k, f = _read_sym(sym, since_month)
        if len(k) == 0:
            continue
        raw[sym] = (k, f)
        di = ((k[:, 0] - d0) // DAY).astype(int)
        ok = (di >= 0) & (di <= D)
        if not ok.any():
            continue
        tt = np.zeros(D + 1)
        np.add.at(tt, di[ok], k[ok, 5])
        has = np.zeros(D + 1, bool)
        has[di[ok]] = True
        Tall[i, has] = tt[has]
        hh = ((k[:, 0] - d0) % DAY == 0) & ok
        Oall[i, di[hh]] = k[hh, 1]
    keep = set()
    for kk in range(30, D + 1):
        keep.update(mo.universe_at(Tall, Oall, kk, universe_n))
    if "BTCUSDT" in names:
        keep.add(names.index("BTCUSDT"))
    sel = sorted(keep)
    syms = [names[i] for i in sel]
    log(f"{len(names)} symbols in the cache, {len(syms)} ever in the top {universe_n} by turnover")
    S = len(syms)
    M = {n: np.full((S, Hn), np.nan, dtype=np.float32) for n in ("o", "hi", "lo", "qv", "n", "tb")}
    F = np.zeros((S, D))
    for j, sym in enumerate(syms):
        k, f = raw[sym]
        hi_ = ((k[:, 0] - d0) // HOUR).astype(int)
        ok = (hi_ >= 0) & (hi_ < Hn)
        h = hi_[ok]
        for name, col in (("o", 1), ("hi", 2), ("lo", 3), ("qv", 5), ("n", 6), ("tb", 7)):
            M[name][j, h] = k[ok, col]
        if len(f):
            fd = ((f[:, 0] - d0) // DAY).astype(int)
            fo = (fd >= 0) & (fd < D)
            np.add.at(F[j], fd[fo], f[fo, 1])
    Od = M["o"][:, ::24].astype(np.float64)
    qv = M["qv"].reshape(S, D + 1, 24)
    Td = np.where(np.all(np.isnan(qv), axis=2), np.nan, np.nansum(qv, axis=2))
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = Od[:, 1:] / Od[:, :-1] - 1.0
    X = ret - F
    return days, syms, M, Od, Td, X, F


# ------------------------------------------------------------------ features
def _nan_reduce(fn, a, axis=1):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return fn(a, axis=axis)


def feature_day(M: dict, Od: np.ndarray, F: np.ndarray, k: int, uni: list[int], btc: int | None):
    """Feature matrix [len(uni), len(FEATURES)] for day index k (hour h0 = 24 k). Uses bars before h0 and the open price at h0 only."""
    h0 = 24 * k
    if k < 30 or h0 - 336 < 0:
        return None
    u = np.asarray(uni)
    o = M["o"][u].astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        ret = [o[:, h0] / o[:, h0 - n] - 1.0 for n in (6, 12, 24, 72, 168, 336)]
        rh = o[:, h0 - 168 + 1:h0 + 1] / o[:, h0 - 168:h0] - 1.0                  # hourly returns, the last one ends at the open of h0
        vol24 = _nan_reduce(np.nanstd, rh[:, -24:])
        vol168 = _nan_reduce(np.nanstd, rh)
        cnt24 = np.sum(~np.isnan(rh[:, -24:]), axis=1)
        vol24 = np.where(cnt24 >= 18, vol24, np.nan)
        vol168 = np.where(np.sum(~np.isnan(rh), axis=1) >= 120, vol168, np.nan)
        qv, tb, nt = M["qv"][u].astype(np.float64), M["tb"][u].astype(np.float64), M["n"][u].astype(np.float64)
        q24, q72, q168 = (_nan_reduce(np.nansum, qv[:, h0 - w:h0]) for w in (24, 72, 168))
        imb24 = _nan_reduce(np.nansum, tb[:, h0 - 24:h0]) / q24 - 0.5
        imb72 = _nan_reduce(np.nansum, tb[:, h0 - 72:h0]) / q72 - 0.5
        vol_surge = (q24 / 24.0) / (q168 / 168.0)
        trade_surge = (_nan_reduce(np.nansum, nt[:, h0 - 24:h0]) / 24.0) / (_nan_reduce(np.nansum, nt[:, h0 - 168:h0]) / 168.0)
        hi, lo = M["hi"][u].astype(np.float64), M["lo"][u].astype(np.float64)
        range24 = (_nan_reduce(np.nanmax, hi[:, h0 - 24:h0]) - _nan_reduce(np.nanmin, lo[:, h0 - 24:h0])) / o[:, h0]
        dist_hi = o[:, h0] / _nan_reduce(np.nanmax, hi[:, h0 - 168:h0]) - 1.0
        dist_lo = o[:, h0] / _nan_reduce(np.nanmin, lo[:, h0 - 168:h0]) - 1.0
    fund1 = F[u, k - 1]
    fund7 = F[u, k - 7:k].sum(axis=1)
    base = np.column_stack(ret + [vol24, vol168, imb24, imb72, vol_surge, trade_surge, range24, dist_hi, dist_lo, fund1, fund7])
    base[np.isnan(o[:, h0])] = np.nan
    base[~np.isfinite(base)] = np.nan
    if btc is not None and not np.isnan(Od[btc, k]) and h0 - 336 >= 0:
        ob = M["o"][btc].astype(np.float64)
        rb = ob[h0 - 168 + 1:h0 + 1] / ob[h0 - 168:h0] - 1.0
        reg_b = [ob[h0] / ob[h0 - 24] - 1.0, ob[h0] / ob[h0 - 168] - 1.0, float(np.nanstd(rb))]
    else:
        reg_b = [np.nan] * 3
    r24 = base[:, 2]
    reg_m = [float(np.nanmean(r24)) if np.any(~np.isnan(r24)) else np.nan, float(np.nanstd(r24)) if np.sum(~np.isnan(r24)) > 2 else np.nan]
    regime = np.tile(reg_b + reg_m, (len(uni), 1))
    ranks = np.column_stack([lg.rank01(base[:, j]) for j in range(base.shape[1])])
    return np.column_stack([base, regime, ranks])


def build_features(M, Od, Td, F, k_first: int, k_last: int, syms: list[str], universe_n: int) -> dict:
    btc = syms.index("BTCUSDT") if "BTCUSDT" in syms else None
    out = {}
    for k in range(k_first, k_last + 1):
        uni = mo.universe_at(Td, Od, k, universe_n)
        if len(uni) < 8:
            continue
        f = feature_day(M, Od, F, k, uni, btc)
        if f is not None:
            out[k] = (uni, f)
    return out


def panel_for(feats: dict, X: np.ndarray, hold: int) -> dict:
    return {k: (uni, f, lg.labels(None, X, k, uni, hold) if k + hold <= X.shape[1] else None) for k, (uni, f) in feats.items()}


# ------------------------------------------------------------------ baselines and verdict
def rule_scores(Od: np.ndarray, Td: np.ndarray, ks: list[int], kind: str, universe_n: int) -> dict:
    out = {}
    for k in ks:
        uni = mo.universe_at(Td, Od, k, universe_n)
        sc = {}
        for i in uni:
            if kind == "mom14" and k >= 14 and not np.isnan(Od[i, k]) and not np.isnan(Od[i, k - 14]):
                sc[i] = Od[i, k] / Od[i, k - 14] - 1.0
            elif kind == "rev3" and k >= 3 and not np.isnan(Od[i, k]) and not np.isnan(Od[i, k - 3]):
                sc[i] = -(Od[i, k] / Od[i, k - 3] - 1.0)
        if len(sc) >= 8:
            out[k] = sc
    return out


def robustness(days, Od, Td, X, syms, pred: dict, rebal: list[int], first: int, kend: int, lock_k: int) -> dict:
    """Is the primary result a broad effect or a few lucky days / bad prints / one lookback? Everything here is a diagnostic of the SAME predictions, nothing is refit."""
    base = lg.simulate_scores(days, Od, Td, X, pred, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
    pnl = base["pnl"]
    x = np.sort(pnl[first:kend])
    out = {"concentration_pct_of_sleeve": {"total": round(float(x.sum()) * 100, 1), "best_10_days": round(float(x[-10:].sum()) * 100, 1), "worst_10_days": round(float(x[:10].sum()) * 100, 1),
                                           "total_without_best_20_days": round(float(x[:-20].sum()) * 100, 1)}}
    yrs: dict[int, list[float]] = {}
    for k in range(first, kend):
        yrs.setdefault(datetime.fromtimestamp(days[k] / 1000, tz=timezone.utc).year, []).append(pnl[k])
    out["apr_pct_by_year"] = {str(y): round(float(np.mean(v)) * 36500, 1) for y, v in sorted(yrs.items())}
    clipped = lg.simulate_scores(days, Od, Td, np.clip(X, -0.5, 0.5), pred, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
    out["daily_coin_return_clipped_at_50pct"] = {"all": mo.stats(clipped["pnl"], first, kend), "oos": mo.stats(clipped["pnl"], lock_k, kend)}
    breaks = {i for i, _ in np.argwhere(np.abs(np.nan_to_num(X, nan=0.0)) > 1.0)}
    ex = {k: {i: v for i, v in sc.items() if i not in breaks} for k, sc in pred.items()}
    exs = lg.simulate_scores(days, Od, Td, X, ex, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
    out["without_coins_with_a_100pct_day"] = {"coins_removed": len(breaks), "all": mo.stats(exs["pnl"], first, kend), "oos": mo.stats(exs["pnl"], lock_k, kend), "note": "chosen with hindsight: a diagnostic, not a trading rule"}
    legs = {}
    for leg in ("long", "short"):
        pl = np.zeros(len(days) - 1)
        for k in rebal:
            sc = pred.get(k)
            if not sc or k + HOLD > kend:
                continue
            order = sorted(sc, key=lambda i: sc[i])
            sel, sign = (order[-QUINTILE:], 1.0) if leg == "long" else (order[:QUINTILE], -1.0)
            for i in sel:
                pl[k:k + HOLD] += sign * 0.5 / QUINTILE * np.nan_to_num(X[i, k:k + HOLD], nan=0.0)
            pl[k] -= 0.5 * 2 * (mo.FEE_PCT + mo.SLIP_PCT) / 100.0
        legs[leg] = {"apr_pct_all": round(float(pl[first:kend].mean()) * 36500, 1), "apr_pct_oos": round(float(pl[lock_k:kend].mean()) * 36500, 1)}
    out["legs_gross_of_the_other_leg"] = legs
    lbs = {}
    for lb in (3, 7, 14, 28, 56):
        sc = {}
        for k in rebal:
            uni = mo.universe_at(Td, Od, k, UNIVERSE_N)
            d = {i: Od[i, k] / Od[i, k - lb] - 1.0 for i in uni if k >= lb and not np.isnan(Od[i, k]) and not np.isnan(Od[i, k - lb])}
            if len(d) >= 8:
                sc[k] = d
        sm = lg.simulate_scores(days, Od, Td, X, sc, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
        lbs[f"mom{lb}"] = {"apr_pct_all": mo.stats(sm["pnl"], first, kend).get("apr_pct"), "apr_pct_oos": mo.stats(sm["pnl"], lock_k, kend).get("apr_pct")}
    out["momentum_lookback_sensitivity_hold3"] = lbs
    return out


def verdict(oos: dict, locked: dict, percentile: float | None, rebalances_oos: int, ic: dict, baseline_apr: float | None, stress_apr: float | None, clipped_apr: float | None = None) -> dict:
    v = lg.verdict(oos, locked, percentile, rebalances_oos, ic, baseline_apr)
    if v["verdict"] == "insufficient n":
        return v
    failed = list(v["failed"])
    if stress_apr is None or stress_apr <= PASS["min_stress_apr_pct"]:
        failed.append(f"with 0.30% slippage the out-of-sample APR is {stress_apr}% (must stay above {PASS['min_stress_apr_pct']}%)")
    if clipped_apr is not None and clipped_apr < PASS["min_clipped_oos_apr_pct"]:
        failed.append(f"with every daily coin return clipped at +-50% the out-of-sample APR is {clipped_apr}% (< {PASS['min_clipped_oos_apr_pct']}%): the result lives on a few extreme moves")
    if not failed:
        return {"verdict": "edge candidate", "failed": []}
    return {"verdict": v["verdict"] if v["verdict"] != "edge candidate" else "unproven", "failed": failed}


LIMITS = ["Daily marking of a 3-day hold: no intraday stop, liquidation or margin model; a coin that vanishes is held flat at its last price.",
          "Binance USDT-M perpetuals only; the archive lists delisted contracts, so the universe is not survivor-biased, but a contract missing from the archive is invisible.",
          "About five years: bull, crash and a long falling-alt period, still few independent regimes. The first year only trains, the 'locked' part (until 2024-07) is for looking, the rest decides.",
          "One fixed parameter set, no search. Only the primary configuration (3-day hold, 0.10% slippage) counts for the verdict; the 1-day hold and the stress slippage are context (the stress one is a pass condition).",
          "Placebo = 30 pipelines with shuffled labels (percentile steps of ~3). A pass is a hypothesis for a small paper test, never `edge` by itself.",
          "The clipped-returns condition and the robustness block were added after the first real run (they can only tighten the verdict)."]


# ------------------------------------------------------------------ study
def run_study(run_id: str, params: dict, until: datetime | None = None, log=lambda s: None) -> dict:
    lg._lightgbm()
    now = until or datetime.now(timezone.utc)
    start = datetime.fromisoformat(params.get("since", SINCE)).replace(tzinfo=timezone.utc)
    end = now - timedelta(days=1)
    days, syms, M, Od, Td, X, F = build(start, end, UNIVERSE_N, log)
    if len(syms) < 12:
        raise RuntimeError("not enough cached Binance data: the fetch has to run first")
    k0 = next(k for k, d in enumerate(days) if d >= carry.day_start(start))
    kend = len(days) - 2
    lock_k = next(k for k, d in enumerate(days) if d >= carry.day_start(datetime.fromisoformat(LOCK_END).replace(tzinfo=timezone.utc)))
    first = k0 + MIN_TRAIN_DAYS
    if kend - first < 300:
        raise RuntimeError(f"not enough history for a walk-forward: {kend - k0} days, need at least {MIN_TRAIN_DAYS + 300}")
    log(f"features for {kend - k0 + 40} days, {len(syms)} symbols")
    feats = build_features(M, Od, Td, F, 30, kend, syms, UNIVERSE_N)
    first_pred = datetime.fromtimestamp(days[first] / 1000, tz=timezone.utc).date().isoformat()
    results, sims, pred_h = {}, {}, {}
    for hold in (HOLD, 1):
        panel = panel_for(feats, X, hold)
        rebal = list(range(first, kend - hold + 1, hold))
        log(f"walk-forward hold {hold} d: {len(rebal)} rebalances, retrain every {RETRAIN_DAYS} days, first prediction {first_pred}")
        pr = lg.walk_forward(panel, rebal, first, None, log, hold, FEATURES, PARAMS, RETRAIN_DAYS, QUINTILE)
        imp = pr.pop("_importance")
        pred_h[hold] = (pr, imp, panel, rebal)
        for slip in (mo.SLIP_PCT, mo.SLIP_STRESS_PCT):
            sim = lg.simulate_scores(days, Od, Td, X, pr, first, kend, slip, hold, QUINTILE)
            key = f"h{hold}_slip{slip}"
            sims[key] = sim
            results[key] = {"all": mo.stats(sim["pnl"], first, kend), "locked": mo.stats(sim["pnl"], first, lock_k), "oos": mo.stats(sim["pnl"], lock_k, kend),
                            "rebalances_oos": sum(1 for r in sim["rebalances"] if r["k"] >= lock_k and r["n_pos"]), "avg_turnover": round(float(np.mean([r["turnover"] for r in sim["rebalances"]])), 3) if sim["rebalances"] else None}
    p_key = f"h{HOLD}_slip{mo.SLIP_PCT}"
    pr, imp, panel, rebal = pred_h[HOLD]
    bench = {}
    for kind in ("mom14", "rev3"):
        sc = rule_scores(Od, Td, rebal, kind, UNIVERSE_N)
        bs = lg.simulate_scores(days, Od, Td, X, sc, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
        bench[f"{kind}_hold{HOLD}"] = {"all": mo.stats(bs["pnl"], first, kend), "locked": mo.stats(bs["pnl"], first, lock_k), "oos": mo.stats(bs["pnl"], lock_k, kend)}
    if "BTCUSDT" in syms:
        bench["btc_hold"] = {"oos": mo.stats(np.nan_to_num(X[syms.index("BTCUSDT")], nan=0.0), lock_k, kend)}
    ic_oos, ic_locked = lg.rank_ic(pr, panel, lock_k, kend), lg.rank_ic(pr, panel, first, lock_k)
    log(f"placebo: {PLACEBO_SEEDS} pipelines trained on shuffled labels")
    placebo = []
    for seed in range(PLACEBO_SEEDS):
        sp = lg.walk_forward(panel, rebal, first, 500 + seed, log, HOLD, FEATURES, PARAMS, RETRAIN_DAYS, QUINTILE)
        sp.pop("_importance", None)
        sm = lg.simulate_scores(days, Od, Td, X, sp, first, kend, mo.SLIP_PCT, HOLD, QUINTILE)
        placebo.append(float(sm["pnl"][lock_k:kend].mean()) * 365 * 100)
    prim = results[p_key]
    pct = round(100.0 * float(np.mean([prim["oos"].get("apr_pct", -1e9) > a for a in placebo])), 1)
    base_apr = max((b["oos"].get("apr_pct", -1e9) for k_, b in bench.items() if k_.endswith(f"hold{HOLD}")), default=None)
    rob = robustness(days, Od, Td, X, syms, pr, rebal, first, kend, lock_k)
    v = verdict(prim["oos"], prim["locked"], pct, prim["rebalances_oos"], ic_oos, base_apr, results[f"h{HOLD}_slip{mo.SLIP_STRESS_PCT}"]["oos"].get("apr_pct"), rob["daily_coin_return_clipped_at_50pct"]["oos"].get("apr_pct"))
    total = sum(imp["gain"].values()) or 1.0
    top = sorted(imp["gain"].items(), key=lambda kv: -kv[1])[:10]
    return {"type": "lgbm_hourly_study", "run_id": run_id, "generated_at": now.isoformat(), "verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "primary": p_key, "pass_thresholds": PASS,
            "window": {"start": start.date().isoformat(), "first_prediction": first_pred, "end": end.date().isoformat(), "lock_end": LOCK_END},
            "universe": {"symbols_used": len(syms), "universe_n": UNIVERSE_N, "source": "Binance USDT-M perpetuals, data.binance.vision + fapi"},
            "rules": {"hold_days": HOLD, "retrain_days": RETRAIN_DAYS, "min_train_days": MIN_TRAIN_DAYS, "model": PARAMS, "features": FEATURES, "book": f"long top {QUINTILE}, short bottom {QUINTILE} of the prediction, every {HOLD} days, dollar neutral",
                      "fee_pct": mo.FEE_PCT, "slippage_pct_per_side": [mo.SLIP_PCT, mo.SLIP_STRESS_PCT], "sleeve_usd": mo.SLEEVE_USD},
            "results": results, "rank_ic": {"oos": ic_oos, "locked": ic_locked}, "benchmarks": bench, "robustness": rob,
            "placebo": {"seeds": PLACEBO_SEEDS, "kind": "same pipeline, labels shuffled inside each day", "oos_apr_pct_p5_p50_p95": [round(float(x), 2) for x in np.percentile(placebo, [5, 50, 95])], "primary_oos_percentile": pct},
            "feature_importance_top": [{"feature": n, "gain_share_pct": round(100.0 * g / total, 1)} for n, g in top], "models_trained": imp["models"],
            "limits": LIMITS, "_sims": {k_: {"pnl": s_["pnl"].tolist(), "rebalances": s_["rebalances"]} for k_, s_ in sims.items() if k_ == p_key}, "_days": days}


def to_markdown(rep: dict) -> str:
    md = lg.to_markdown(rep).replace("# LightGBM walk-forward study", "# LightGBM walk-forward study v2 (Binance, hourly features)", 1)
    if rep.get("robustness"):
        md += "\n## Robustness of the primary predictions (diagnostics, nothing is refit)\n\n```\n" + json.dumps(rep["robustness"], indent=1) + "\n```\n"
    return md


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    sims, days = rep.pop("_sims", {}), rep.pop("_days", [])
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
