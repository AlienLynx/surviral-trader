"""
Unit tests for the pure decision logic in the two P4 custom pairlist
filters (FundingRsiPairList for Shorts, BollingerTrendPairList for Main).
Runs standalone, no real Freqtrade install needed -- imports the filter
files directly (they degrade to logic-only modules outside a real
Freqtrade env, see each file's own try/except ImportError block) and
exercises should_exclude()/is_persistently_below_band() with synthetic
inputs. This is what CAN be verified from a dev sandbox with no live
Freqtrade/ccxt exchange connection; the actual IPairList wiring (fetching
real tickers/klines inside a running bot, config.json's pairlists chain
picking the class up by name) needs a real deploy to confirm end-to-end --
flagged honestly, not glossed over.
"""
import importlib.util
import sys

import pandas as pd

fails = []


def check(label, cond):
    print(("[OK]  " if cond else "[FAIL]") + " " + label)
    if not cond:
        fails.append(label)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = load("D:/Trader/repo/freqtrade-shorts/user_data/pairlists/FundingRsiPairList.py", "fr_pl_test")
bt = load("D:/Trader/repo/freqtrade/user_data/pairlists/BollingerTrendPairList.py", "bt_pl_test")

# ---------------------------------------------------------------------------
# FundingRsiPairList
# ---------------------------------------------------------------------------
check("major detection: BTC/USDT:USDT is major", fr.is_major("BTC/USDT:USDT"))
check("major detection: ETH/USDT:USDT is major", fr.is_major("ETH/USDT:USDT"))
check("major detection: SOL/USDT:USDT is NOT major", not fr.is_major("SOL/USDT:USDT"))
check("symbol conversion: BTC/USDT:USDT -> BTCUSDT", fr.to_bybit_linear_symbol("BTC/USDT:USDT") == "BTCUSDT")

# Majors: exclude if funding < -0.02%/8h == -0.0002
excluded, reason = fr.should_exclude("BTC/USDT:USDT", -0.0003, None)
check("major funding -0.03% (< -0.02%) -> excluded", excluded)
excluded, reason = fr.should_exclude("BTC/USDT:USDT", -0.0001, None)
check("major funding -0.01% (> -0.02%) -> NOT excluded", not excluded)
excluded, reason = fr.should_exclude("BTC/USDT:USDT", -0.0002, None)
check("major funding exactly -0.02% -> NOT excluded (strict <, not <=)", not excluded)

# Alts: exclude if funding < -0.05%/8h == -0.0005
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", -0.0006, None)
check("alt funding -0.06% (< -0.05%) -> excluded", excluded)
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", -0.0003, None)
check("alt funding -0.03% (> -0.05%, would exclude a major but not an alt) -> NOT excluded", not excluded)

# Priority: funding present -> RSI ignored even if RSI would also say exclude
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", -0.0001, 10.0)
check("funding present (even if fine) -> RSI ignored entirely", excluded is False and "RSI" not in reason)

# RSI fallback only when funding is None
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", None, 20.0)
check("funding unavailable, RSI 20 (< 25) -> excluded via fallback", excluded and "RSI" in reason)
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", None, 30.0)
check("funding unavailable, RSI 30 (>= 25) -> NOT excluded", not excluded)

# Fail-open: both unavailable
excluded, reason = fr.should_exclude("PEPE/USDT:USDT", None, None)
check("funding AND RSI both unavailable -> fail open, NOT excluded", not excluded)
check("fail-open reason says so explicitly", "fail open" in reason)

# RSI computation sanity: monotonically falling prices -> low RSI
falling = pd.Series([100 - i for i in range(30)])
rsi_falling = fr.compute_rsi(falling)
check("RSI of a steadily falling series is low (<30)", rsi_falling is not None and rsi_falling < 30)
rising = pd.Series([100 + i for i in range(30)])
rsi_rising = fr.compute_rsi(rising)
check("RSI of a steadily rising series is high (>70)", rsi_rising is not None and rsi_rising > 70)
check("RSI returns None with too little data", fr.compute_rsi(pd.Series([100, 101, 102])) is None)

# ---------------------------------------------------------------------------
# BollingerTrendPairList
# ---------------------------------------------------------------------------
check("symbol conversion: BTC/USDT -> BTCUSDT", bt.to_bybit_spot_symbol("BTC/USDT") == "BTCUSDT")

# Flat-then-crashed series: last 3 candles clearly below a band computed on
# the flat history before the crash.
flat = [100.0] * 25
crash = [80.0, 78.0, 76.0]
closes_crash = pd.Series(flat + crash)
excluded, reason = bt.is_persistently_below_band(closes_crash)
check("sustained breakdown (3 candles well below flat-history band) -> excluded", excluded)

# Single-candle wick then recovery: should NOT trigger (only 1 candle below,
# not N=3 in a row).
wick = [100.0] * 24 + [80.0, 100.0, 101.0]
closes_wick = pd.Series(wick)
excluded, reason = bt.is_persistently_below_band(closes_wick)
check("single-candle wick (not persistent) -> NOT excluded", not excluded)

# Not enough history at all -> fail open
short_series = pd.Series([100.0, 99.0, 98.0])
excluded, reason = bt.is_persistently_below_band(short_series)
check("insufficient history -> fail open, NOT excluded", not excluded)
check("insufficient-history reason says so", "insufficient history" in reason)

# Steady uptrend -- never below its own band
uptrend = pd.Series([100 + i * 0.5 for i in range(30)])
excluded, reason = bt.is_persistently_below_band(uptrend)
check("steady uptrend -> NOT excluded", not excluded)

# N is configurable and actually used: a wick that IS the most recent candle
# triggers with n=1 but not with the default n=3 (not persistent enough).
wick_is_last = pd.Series([100.0] * 24 + [100.0, 100.0, 80.0])
excluded_n1, _ = bt.is_persistently_below_band(wick_is_last, n=1)
check("with n=1, a wick that IS the latest candle DOES trigger (N is load-bearing)", excluded_n1)
excluded_n3, _ = bt.is_persistently_below_band(wick_is_last, n=3)
check("with default n=3, that same single trailing wick does NOT trigger", not excluded_n3)

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL P4 PAIRLIST LOGIC CHECKS PASSED")
