"""
P4 pre-deploy check, second half: since neither bot has enough live
trading history yet to have a real squeeze/pump in its own logs, this
scans REAL historical Bybit data instead, looking for an actual episode
where each filter's condition would have fired shortly before a sharp
reversal -- a genuine historical case, not a synthetic one.

Main / BollingerTrendPairList: scans a basket of pairs' last ~60 days of
1h closes for a point where the filter's own is_persistently_below_band()
condition was true, followed by a strong rebound (>= REBOUND_PCT) within
the next REBOUND_WINDOW_HOURS -- i.e. "the filter would have excluded this
pair right before it violently reverted, showing why a caught pair here
isn't just correctly-avoided continued weakness."

Shorts / FundingRsiPairList: fetches real historical funding rate history
per pair (Bybit's public /v5/market/funding/history) and looks for an
episode where funding was below that pair's own threshold, followed by a
sharp price rally (a real short squeeze) within the following hours.

Read-only, makes no changes.
"""
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "freqtrade-shorts" / "user_data" / "pairlists"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "freqtrade" / "user_data" / "pairlists"))
import FundingRsiPairList as fr  # noqa: E402
import BollingerTrendPairList as bt  # noqa: E402

REBOUND_PCT = 0.10
REBOUND_WINDOW_HOURS = 72


def fetch_hourly_closes(symbol: str, category: str, days: int = 60) -> pd.DataFrame | None:
    """Paginated -- Bybit's kline endpoint caps at 1000 candles/call, and
    180 days of 1h candles (~4320) needs several. Walks backward in time
    from now until either `days` is covered or the exchange has no more
    history for this symbol."""
    import time
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 24 * 3600 * 1000
    all_rows: list[list] = []
    cursor_end = end_ms
    for _ in range(15):  # hard cap on pagination round-trips, safety valve
        r = requests.get(
            "https://api.bybit.com/v5/market/kline",
            params={"category": category, "symbol": symbol, "interval": "60",
                    "start": start_ms, "end": cursor_end, "limit": 1000},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()["result"]["list"]
        if not rows:
            break
        all_rows.extend(rows)
        oldest_ts = min(int(row[0]) for row in rows)
        if oldest_ts <= start_ms or len(rows) < 1000:
            break
        cursor_end = oldest_ts - 1
    if not all_rows:
        return None
    df = pd.DataFrame(all_rows, columns=["ts", "open", "high", "low", "close", "volume", "turnover"])
    df = df.astype({"ts": "int64", "close": "float64"}).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df


def check_main_squeeze(symbols: list[str]) -> None:
    print("=" * 70)
    print("MAIN / BollingerTrendPairList -- scanning real history for a caught breakdown->rebound")
    print("=" * 70)
    found_any = False
    for sym in symbols:
        try:
            df = fetch_hourly_closes(sym, "spot", days=180)
        except Exception as e:
            print(f"  {sym}: fetch failed, skipping: {e}")
            continue
        if df is None or len(df) < 30:
            continue
        closes = df["close"]
        for i in range(bt.BOLLINGER_WINDOW + bt.CONSECUTIVE_CANDLES_BELOW, len(closes) - REBOUND_WINDOW_HOURS):
            window = closes.iloc[: i + 1]
            excluded, _ = bt.is_persistently_below_band(window)
            if not excluded:
                continue
            price_at_flag = closes.iloc[i]
            future_max = closes.iloc[i: i + REBOUND_WINDOW_HOURS].max()
            rebound = (future_max / price_at_flag) - 1
            if rebound >= REBOUND_PCT:
                found_any = True
                when = pd.to_datetime(df["ts"].iloc[i], unit="ms", utc=True)
                print(f"  {sym}: flagged at {when} (price {price_at_flag:.4f}), "
                      f"rebounded +{rebound*100:.1f}% within {REBOUND_WINDOW_HOURS}h -- "
                      f"filter would have excluded it right before the reversal")
                break
    if not found_any:
        print(f"  No qualifying case found in the scanned symbols/window "
              f"(>= {REBOUND_PCT*100:.0f}% rebound within {REBOUND_WINDOW_HOURS}h after a flag). "
              f"Not a failure of the filter -- just means none of these specific pairs had that exact "
              f"pattern in the last 60 days; widen the symbol list or window to keep looking.")
    print()


def check_shorts_squeeze(symbols: list[str]) -> None:
    print("=" * 70)
    print("SHORTS / FundingRsiPairList -- scanning real funding history for a caught squeeze")
    print("=" * 70)
    found_any = False
    for sym in symbols:
        try:
            rows = []
            cursor_end = None
            for _ in range(6):  # up to ~1200 funding periods back, safety-capped
                params = {"category": "linear", "symbol": sym, "limit": 200}
                if cursor_end is not None:
                    params["endTime"] = cursor_end
                r = requests.get("https://api.bybit.com/v5/market/funding/history", params=params, timeout=15)
                r.raise_for_status()
                page = r.json()["result"]["list"]
                if not page:
                    break
                rows.extend(page)
                oldest_ts = min(int(x["fundingRateTimestamp"]) for x in page)
                if len(page) < 200:
                    break
                cursor_end = oldest_ts - 1
        except Exception as e:
            print(f"  {sym}: funding history fetch failed: {e}")
            continue
        if not rows:
            continue
        rows = sorted({x["fundingRateTimestamp"]: x for x in rows}.values(), key=lambda x: int(x["fundingRateTimestamp"]))
        pair = f"{sym[:-4]}/USDT:USDT"  # 'DOGEUSDT' -> 'DOGE/USDT:USDT'
        threshold = fr.funding_threshold_for(pair)
        try:
            df = fetch_hourly_closes(sym, "linear", days=400)
        except Exception as e:
            print(f"  {sym}: kline fetch failed, skipping: {e}")
            continue
        if df is None:
            continue
        for row in rows:
            rate = float(row["fundingRate"])
            if rate >= threshold:
                continue
            ts = int(row["fundingRateTimestamp"])
            after = df[df["ts"] >= ts]
            before = df[df["ts"] <= ts]
            if before.empty or after.empty:
                continue
            price_at = before["close"].iloc[-1]
            future_window = after[after["ts"] <= ts + REBOUND_WINDOW_HOURS * 3600 * 1000]
            if future_window.empty:
                continue
            future_max = future_window["close"].max()
            squeeze = (future_max / price_at) - 1
            if squeeze >= REBOUND_PCT:
                found_any = True
                when = pd.to_datetime(ts, unit="ms", utc=True)
                print(f"  {pair}: funding {rate*100:.4f}%/8h (< {threshold*100:.4f}% threshold) at {when}, "
                      f"price then rallied +{squeeze*100:.1f}% within {REBOUND_WINDOW_HOURS}h -- "
                      f"a real short squeeze the filter would have flagged in advance")
                break
    if not found_any:
        print(f"  No qualifying case found in the scanned symbols/window. Not a failure of the filter --"
              f" deeply negative funding followed by a >= {REBOUND_PCT*100:.0f}% squeeze within "
              f"{REBOUND_WINDOW_HOURS}h is a real but not-guaranteed-to-occur pattern; widen the symbol "
              f"list or window to keep looking.")
    print()


if __name__ == "__main__":
    main_symbols = ["DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "XRPUSDT", "SOLUSDT", "SUIUSDT", "AVAXUSDT",
                     "NEARUSDT", "WIFUSDT", "BONKUSDT", "FLOKIUSDT", "1000SATSUSDT", "PENGUUSDT"]
    shorts_symbols = ["DOGEUSDT", "1000PEPEUSDT", "XRPUSDT", "SOLUSDT", "SUIUSDT", "AVAXUSDT",
                       "NEARUSDT", "WIFUSDT", "1000BONKUSDT", "1000FLOKIUSDT"]
    check_main_squeeze(main_symbols)
    check_shorts_squeeze(shorts_symbols)
