"""
P16: unit tests for the anti-chop stale-signal streak mechanism.

P16b changed the LLM bots (main, shorts) from "exit on the FIRST non-buy candle" to
"exit only after STALE_SIGNAL_STREAK_REQUIRED consecutive non-buy
candles", and raised MIN_HOLD_HOURS 2 -> 4. (The Dip and Survival bots, which shared this
mechanism, were removed from the repository.)

Loads each real strategy file standalone via the same freqtrade-stubbing
technique as the other P-series test files (llm_replay.py, test_p13_*).
"""
from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _install_stubs():
    ft = types.ModuleType("freqtrade")
    ft_persistence = types.ModuleType("freqtrade.persistence")

    class _StubTrade:
        @classmethod
        def get_trades_proxy(cls, is_open=True):
            return []

        @classmethod
        def total_open_trades_stakes(cls):
            return 0.0

    ft_persistence.Trade = _StubTrade

    ft_strategy = types.ModuleType("freqtrade.strategy")

    class _StubIStrategy:
        def __init__(self, config: dict) -> None:
            self.config = config

    ft_strategy.IStrategy = _StubIStrategy

    sys.modules["freqtrade"] = ft
    sys.modules["freqtrade.persistence"] = ft_persistence
    sys.modules["freqtrade.strategy"] = ft_strategy
    return ft_persistence.Trade


def _load_strategy_module(path: Path, module_name: str):
    strategy_dir = str(path.parent)
    if strategy_dir not in sys.path:
        sys.path.insert(0, strategy_dir)
    spec = importlib.util.spec_from_file_location(module_name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeTrade:
    def __init__(self, trade_id=1, open_date_utc=None, is_short=False):
        self.id = trade_id
        self.open_date_utc = open_date_utc or (datetime.now(timezone.utc) - timedelta(hours=10))
        self.is_short = is_short


class FakeDP:
    def __init__(self, candle_dates):
        """candle_dates: list of successive 'latest candle date' values,
        one per call to get_analyzed_dataframe -- lets a test simulate
        the underlying candle advancing across custom_exit calls."""
        self._dates = list(candle_dates)
        self._i = 0

    def get_analyzed_dataframe(self, pair, timeframe):
        date = self._dates[min(self._i, len(self._dates) - 1)]
        self._i += 1
        df = pd.DataFrame({"date": [date]})
        return df, {}


Trade_stub = _install_stubs()
now = datetime.now(timezone.utc)

# ============================================================
# P16b: main bot (long-only)
# ============================================================
main_mod = _load_strategy_module(
    REPO_ROOT / "freqtrade" / "user_data" / "strategies" / "DeepSeekStrategy.py",
    "p16_main_module",
)
MainStrategy = main_mod.DeepSeekStrategy

check("Main: MIN_HOLD_HOURS raised 2 -> 4", MainStrategy.MIN_HOLD_HOURS == 4)
check("Main: STALE_SIGNAL_STREAK_REQUIRED == 2", MainStrategy.STALE_SIGNAL_STREAK_REQUIRED == 2)

main = MainStrategy({})
t_main = FakeTrade(trade_id=10, open_date_utc=now - timedelta(hours=10))
main.dp = FakeDP([now, now + timedelta(hours=1)])
main._latest_decision["BTC/USDT"] = ("hold", "no longer bullish")
r1 = main.custom_exit("BTC/USDT", t_main, now, 100.0, 0.0)
check("Main custom_exit: 1st non-buy candle -> no exit yet (was instant before P16b)", r1 is None)
r2 = main.custom_exit("BTC/USDT", t_main, now, 100.0, 0.0)
check("Main custom_exit: 2nd consecutive non-buy candle -> rotate_stale_signal", r2 == "rotate_stale_signal")

t_main2 = FakeTrade(trade_id=11, open_date_utc=now - timedelta(hours=1))
main.dp = FakeDP([now])
check("Main custom_exit: hours_open < MIN_HOLD_HOURS(4) -> None", main.custom_exit("BTC/USDT", t_main2, now, 100.0, 0.0) is None)

t_main3 = FakeTrade(trade_id=12)
main._stale_streak[12] = {"count": 1, "last_candle_date": now}
main.confirm_trade_exit("BTC/USDT", t_main3, "market", 1.0, 100.0, "GTC", "roi", now)
check("Main confirm_trade_exit: clears streak entry", 12 not in main._stale_streak)


# ============================================================
# P16b: shorts bot (direction-aware)
# ============================================================
shorts_mod = _load_strategy_module(
    REPO_ROOT / "freqtrade-shorts" / "user_data" / "strategies" / "DeepSeekShortsStrategy.py",
    "p16_shorts_module",
)
ShortsStrategy = shorts_mod.DeepSeekShortsStrategy
check("Shorts: MIN_HOLD_HOURS raised 2 -> 4", ShortsStrategy.MIN_HOLD_HOURS == 4)
check("Shorts: STALE_SIGNAL_STREAK_REQUIRED == 2", ShortsStrategy.STALE_SIGNAL_STREAK_REQUIRED == 2)

shorts = ShortsStrategy({})
t_short = FakeTrade(trade_id=20, open_date_utc=now - timedelta(hours=10), is_short=True)
shorts.dp = FakeDP([now, now + timedelta(hours=1)])
shorts._latest_decision["BTC/USDT:USDT"] = ("buy", "flipped bullish")  # no longer "sell_short" -> invalid for a short
r1 = shorts.custom_exit("BTC/USDT:USDT", t_short, now, 100.0, 0.0)
check("Shorts custom_exit: short trade, decision flips off sell_short, 1st candle -> no exit yet", r1 is None)
r2 = shorts.custom_exit("BTC/USDT:USDT", t_short, now, 100.0, 0.0)
check("Shorts custom_exit: 2nd consecutive invalid candle -> rotate_stale_signal", r2 == "rotate_stale_signal")

t_long = FakeTrade(trade_id=21, open_date_utc=now - timedelta(hours=10), is_short=False)
shorts.dp = FakeDP([now, now + timedelta(hours=1)])
shorts._latest_decision["ETH/USDT:USDT"] = ("sell_short", "flipped bearish")  # no longer "buy" -> invalid for a long
shorts.custom_exit("ETH/USDT:USDT", t_long, now, 100.0, 0.0)
r2_long = shorts.custom_exit("ETH/USDT:USDT", t_long, now, 100.0, 0.0)
check("Shorts custom_exit: long trade, direction-aware invalid streak also fires after 2", r2_long == "rotate_stale_signal")


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL P16 STALE-SIGNAL STREAK CHECKS PASSED")
