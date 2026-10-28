"""
P4 pre-deploy check: fetches the CURRENT real candidate pairlist for Shorts
(linear futures) and Main (spot) from Bybit's public API, runs each bot's
new filter's real fetch functions (real funding rates / real daily RSI /
real 1h klines, right now) against them, and prints before/after so a
human can eyeball whether the filter is cutting too much or too little
before this ever reaches a live deploy. Read-only: makes no config
changes, writes no state.
"""
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "freqtrade-shorts" / "user_data" / "pairlists"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "freqtrade" / "user_data" / "pairlists"))
import FundingRsiPairList as fr  # noqa: E402
import BollingerTrendPairList as bt  # noqa: E402


def fetch_top_linear_pairs(n: int) -> list[str]:
    r = requests.get("https://api.bybit.com/v5/market/tickers", params={"category": "linear"}, timeout=15)
    r.raise_for_status()
    rows = r.json()["result"]["list"]
    usdt_perp = [row for row in rows if row["symbol"].endswith("USDT")]
    usdt_perp.sort(key=lambda row: float(row.get("turnover24h") or 0), reverse=True)
    return [row["symbol"] for row in usdt_perp[:n]]


def fetch_top_spot_pairs(n: int) -> list[str]:
    r = requests.get("https://api.bybit.com/v5/market/tickers", params={"category": "spot"}, timeout=15)
    r.raise_for_status()
    rows = r.json()["result"]["list"]
    usdt = [row for row in rows if row["symbol"].endswith("USDT")]
    usdt.sort(key=lambda row: float(row.get("turnover24h") or 0), reverse=True)
    return [row["symbol"] for row in usdt[:n]]


def symbol_to_shorts_pair(sym: str) -> str:
    base = sym[: -len("USDT")]
    return f"{base}/USDT:USDT"


def symbol_to_main_pair(sym: str) -> str:
    base = sym[: -len("USDT")]
    return f"{base}/USDT"


def run_shorts_check(n: int) -> None:
    print("=" * 70)
    print(f"SHORTS -- FundingRsiPairList, top {n} linear perp candidates by volume")
    print("=" * 70)
    symbols = fetch_top_linear_pairs(n)
    excluded_count = 0
    for sym in symbols:
        pair = symbol_to_shorts_pair(sym)
        funding = fr.fetch_funding_rate(pair)
        rsi = fr.fetch_daily_rsi(pair) if funding is None else None
        excluded, reason = fr.should_exclude(pair, funding, rsi)
        marker = "EXCLUDE" if excluded else "keep   "
        if excluded:
            excluded_count += 1
        print(f"  [{marker}] {pair:20s} funding={funding} rsi={rsi} -- {reason}")
    print(f"\n{excluded_count}/{len(symbols)} would be excluded by FundingRsiPairList right now.\n")


def run_main_check(n: int) -> None:
    print("=" * 70)
    print(f"MAIN -- BollingerTrendPairList, top {n} spot candidates by volume")
    print("=" * 70)
    symbols = fetch_top_spot_pairs(n)
    excluded_count = 0
    for sym in symbols:
        pair = symbol_to_main_pair(sym)
        closes = bt.fetch_recent_closes(pair)
        if closes is None:
            print(f"  [keep   ] {pair:15s} -- kline fetch failed, fail open")
            continue
        excluded, reason = bt.is_persistently_below_band(closes)
        marker = "EXCLUDE" if excluded else "keep   "
        if excluded:
            excluded_count += 1
        print(f"  [{marker}] {pair:15s} -- {reason}")
    print(f"\n{excluded_count}/{len(symbols)} would be excluded by BollingerTrendPairList right now.\n")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    run_shorts_check(n)
    run_main_check(n)
