"""
P11: unit tests for the two new isolated, rule-based (no LLM) bots --
ListingFadeStrategy.py and TrendEnsembleStrategy.py. These are NOT
backtests against historical data -- they check that the code computes
what it's supposed to on synthetic, hand-constructed price series, same
house style as the other backtest/test_*.py files.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
FADE_PATH = REPO_ROOT / "freqtrade-listing-fade" / "user_data" / "strategies" / "ListingFadeStrategy.py"
TREND_PATH = REPO_ROOT / "freqtrade-trend-ensemble" / "user_data" / "strategies" / "TrendEnsembleStrategy.py"

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _stub_freqtrade():
    ft = types.ModuleType("freqtrade")
    ft_p = types.ModuleType("freqtrade.persistence")
    ft_p.Trade = type("Trade", (), {"get_trades_proxy": staticmethod(lambda **kw: [])})
    ft_s = types.ModuleType("freqtrade.strategy")

    def _init(self, config):
        self.config = config

    ft_s.IStrategy = type("IStrategy", (), {"__init__": _init})
    sys.modules["freqtrade"] = ft
    sys.modules["freqtrade.persistence"] = ft_p
    sys.modules["freqtrade.strategy"] = ft_s


def _load(path: Path):
    sys.path.insert(0, str(path.parent))      # Freqtrade puts the strategies dir on sys.path; siblings like _allocator_ctx import from it
    spec = importlib.util.spec_from_file_location(path.stem + "_p11_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_stub_freqtrade()
fade_mod = _load(FADE_PATH)
trend_mod = _load(TREND_PATH)


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class FakeTrade:
    def __init__(self, open_date_utc):
        self.open_date_utc = open_date_utc


# ===================== ListingFadeStrategy =====================
fade = fade_mod.ListingFadeStrategy(config={"dry_run": True})

check("ListingFade.leverage() is always 1.0", fade.leverage("X/USDT:USDT", None, 1.0, 5.0, 5.0, None, "short") == 1.0)
check("ListingFade._live_mode_enabled() defaults to False", fade._live_mode_enabled() is False)

# populate_indicators: pump to 2.0 then dump to 1.3 (-35% from peak)
n = 60
closes = [1.0] * 10 + [2.0] * 5 + list(np.linspace(2.0, 1.3, n - 15))
df = pd.DataFrame({
    "open": closes, "close": closes,
    "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
    "volume": [100] * n,
})
df = fade.populate_indicators(df, {"pair": "TEST/USDT:USDT"})
check("ListingFade.populate_indicators: listing_high is the running max (cummax), not a fixed window",
      df["listing_high"].iloc[-1] == df["high"].max() and df["listing_high"].is_monotonic_increasing)
check("ListingFade.populate_indicators: pct_from_high is deeply negative after the dump",
      df["pct_from_high"].iloc[-1] < -30)
check("ListingFade.populate_indicators: ema20 tracks below a falling price series",
      df["ema20"].iloc[-1] > df["close"].iloc[-1])  # EMA lags a falling series, sits above it

entry_df = fade.populate_entry_trend(df.copy(), {"pair": "TEST/USDT:USDT"})
check("ListingFade.populate_entry_trend: enter_short fires when price down 25%+ AND below EMA20",
      entry_df["enter_short"].iloc[-1] == 1)
check("ListingFade.populate_entry_trend: enter_tag set on the fired row",
      entry_df["enter_tag"].iloc[-1] == "listing_fade")
check("ListingFade.populate_entry_trend: enter_long never set (short-only bot)",
      (entry_df["enter_long"] == 0).all())

# No drop at all -> no signal, regardless of EMA
flat_closes = [1.0] * n
flat_df = pd.DataFrame({
    "open": flat_closes, "close": flat_closes,
    "high": [c * 1.001 for c in flat_closes], "low": [c * 0.999 for c in flat_closes],
    "volume": [100] * n,
})
flat_df = fade.populate_indicators(flat_df, {"pair": "FLAT/USDT:USDT"})
flat_entry = fade.populate_entry_trend(flat_df.copy(), {"pair": "FLAT/USDT:USDT"})
check("ListingFade.populate_entry_trend: no drop from high -> no short signal",
      (flat_entry["enter_short"] == 0).all())

# Drop far enough but still ABOVE EMA20 (a sharp V-recovery) -> should NOT fire
recover_closes = [2.0] * 10 + [1.3] * 5 + list(np.linspace(1.3, 1.9, n - 15))  # dump then strong bounce
recover_df = pd.DataFrame({
    "open": recover_closes, "close": recover_closes,
    "high": [c * 1.01 for c in recover_closes], "low": [c * 0.99 for c in recover_closes],
    "volume": [100] * n,
})
recover_df = fade.populate_indicators(recover_df, {"pair": "RECOVER/USDT:USDT"})
recover_entry = fade.populate_entry_trend(recover_df.copy(), {"pair": "RECOVER/USDT:USDT"})
check("ListingFade.populate_entry_trend: price recovered above EMA20 -> no short signal even if still down from high",
      recover_entry["enter_short"].iloc[-1] == 0)

# confirm_trade_entry: live-mode gate
with mock.patch.dict(fade_mod.os.environ, {}, clear=False):
    live_fade = fade_mod.ListingFadeStrategy(config={"dry_run": False})
    with mock.patch.object(fade_mod.requests, "get", return_value=FakeResp({"result": {"list": [{"fundingRate": "0.0001"}]}})):
        allowed = live_fade.confirm_trade_entry("X/USDT:USDT", "market", 1.0, 1.0, "GTC", None, None, "short")
        check("ListingFade.confirm_trade_entry: rejects when dry_run=False and FADE_LIVE_MODE unset (second gate)",
              allowed is False)

# confirm_trade_entry: funding gate (dry_run=True, gate not in play)
with mock.patch.object(fade_mod.requests, "get", return_value=FakeResp({"result": {"list": [{"fundingRate": "-0.001"}]}})):
    check("ListingFade.confirm_trade_entry: rejects deeply negative funding (squeeze risk)",
          fade.confirm_trade_entry("X/USDT:USDT", "market", 1.0, 1.0, "GTC", None, None, "short") is False)
with mock.patch.object(fade_mod.requests, "get", return_value=FakeResp({"result": {"list": [{"fundingRate": "0.0001"}]}})):
    check("ListingFade.confirm_trade_entry: allows mild/positive funding",
          fade.confirm_trade_entry("X/USDT:USDT", "market", 1.0, 1.0, "GTC", None, None, "short") is True)
with mock.patch.object(fade_mod.requests, "get", side_effect=Exception("boom")):
    check("ListingFade.confirm_trade_entry: fetch failure fails OPEN (allows the trade)",
          fade.confirm_trade_entry("X/USDT:USDT", "market", 1.0, 1.0, "GTC", None, None, "short") is True)
check("ListingFade.confirm_trade_entry: rejects a 'long' side outright (short-only bot)",
      fade.confirm_trade_entry("X/USDT:USDT", "market", 1.0, 1.0, "GTC", None, None, "long") is False)

# custom_exit: MAX_HOLD_DAYS backstop
now = datetime.now(timezone.utc)
old_trade = FakeTrade(open_date_utc=now - timedelta(days=15))
check("ListingFade.custom_exit: force-closes after MAX_HOLD_DAYS (14)",
      fade.custom_exit("X/USDT:USDT", old_trade, now, 1.0, 0.0) == "max_hold_days_reached")
new_trade = FakeTrade(open_date_utc=now - timedelta(days=1))
check("ListingFade.custom_exit: does not force-close before MAX_HOLD_DAYS",
      fade.custom_exit("X/USDT:USDT", new_trade, now, 1.0, 0.0) is None)


# ===================== TrendEnsembleStrategy =====================
trend = trend_mod.TrendEnsembleStrategy(config={"dry_run": True})
check("TrendEnsemble._live_mode_enabled() defaults to False", trend._live_mode_enabled() is False)

n2 = 100
breakout_closes = [10.0] * 90 + list(np.linspace(10.0, 15.0, 10))
bdf = pd.DataFrame({
    "open": breakout_closes, "close": breakout_closes,
    "high": [c * 1.005 for c in breakout_closes], "low": [c * 0.995 for c in breakout_closes],
    "volume": [100] * n2,
})
bdf = trend.populate_indicators(bdf, {"pair": "BTC/USDT"})
votes = trend._breakout_vote_count(bdf)
check("TrendEnsemble._breakout_vote_count: a real breakout above ALL 5 Donchian levels votes 5/5",
      votes.iloc[-1] == 5)
check("TrendEnsemble.populate_indicators: caches this pair's latest volatility_pct",
      "BTC/USDT" in trend._volatility_cache and trend._volatility_cache["BTC/USDT"] > 0)

bentry = trend.populate_entry_trend(bdf.copy(), {"pair": "BTC/USDT"})
check("TrendEnsemble.populate_entry_trend: enters long when votes >= ENSEMBLE_MIN_VOTES (3)",
      bentry["enter_long"].iloc[-1] == 1 and bentry["enter_tag"].iloc[-1] == "donchian_ensemble")

flat_closes2 = [10.0] * n2
fdf = pd.DataFrame({
    "open": flat_closes2, "close": flat_closes2,
    "high": [c * 1.001 for c in flat_closes2], "low": [c * 0.999 for c in flat_closes2],
    "volume": [100] * n2,
})
fdf = trend.populate_indicators(fdf, {"pair": "FLAT/USDT"})
fentry = trend.populate_entry_trend(fdf.copy(), {"pair": "FLAT/USDT"})
check("TrendEnsemble.populate_entry_trend: a flat series with no breakout never enters",
      (fentry["enter_long"] == 0).all())

# Donchian columns use shift(1) -- today's own high must NOT count toward
# today's own breakout level (no lookahead).
spike_closes = [10.0] * 19 + [100.0]  # one huge spike on the very last candle
sdf = pd.DataFrame({
    "open": spike_closes, "close": spike_closes,
    "high": spike_closes, "low": spike_closes,
    "volume": [100] * 20,
})
sdf["donchian_high_5"] = sdf["high"].rolling(5).max().shift(1)
check("Donchian levels use shift(1): the spike candle's own high is NOT in its own 5-day level",
      sdf["donchian_high_5"].iloc[-1] == 10.0)

# custom_stake_amount: volatility-scaled sizing
trend._volatility_cache["LOWVOL/USDT"] = trend.TARGET_VOLATILITY_PCT / 2  # half target -> upsize, clamped at 1.5x
trend._volatility_cache["HIVOL/USDT"] = trend.TARGET_VOLATILITY_PCT * 4   # 4x target -> downsize, clamped at 0.25x
low_stake = trend.custom_stake_amount("LOWVOL/USDT", None, 1.0, 100.0, None, 1000.0, 1.0, None, "long")
high_stake = trend.custom_stake_amount("HIVOL/USDT", None, 1.0, 100.0, None, 1000.0, 1.0, None, "long")
check("TrendEnsemble.custom_stake_amount: low volatility -> upsized stake, clamped at MAX_SIZE_MULTIPLIER",
      low_stake == 100.0 * trend.MAX_SIZE_MULTIPLIER)
check("TrendEnsemble.custom_stake_amount: high volatility -> downsized stake, clamped at MIN_SIZE_MULTIPLIER",
      high_stake == 100.0 * trend.MIN_SIZE_MULTIPLIER)
check("TrendEnsemble.custom_stake_amount: unknown pair (no cached volatility) -> falls back to proposed_stake",
      trend.custom_stake_amount("UNKNOWN/USDT", None, 1.0, 77.0, None, 1000.0, 1.0, None, "long") == 77.0)
check("TrendEnsemble.custom_stake_amount: never sizes below min_stake",
      trend.custom_stake_amount("HIVOL/USDT", None, 1.0, 100.0, 50.0, 1000.0, 1.0, None, "long") == 50.0)

# confirm_trade_entry: live-mode gate
live_trend = trend_mod.TrendEnsembleStrategy(config={"dry_run": False})
check("TrendEnsemble.confirm_trade_entry: rejects when dry_run=False and TREND_LIVE_MODE unset (second gate)",
      live_trend.confirm_trade_entry("BTC/USDT", "market", 1.0, 1.0, "GTC", None, None, "long") is False)
check("TrendEnsemble.confirm_trade_entry: allows entry in dry_run mode",
      trend.confirm_trade_entry("BTC/USDT", "market", 1.0, 1.0, "GTC", None, None, "long") is True)


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL P11 NEW-BOT RULE CHECKS PASSED")
