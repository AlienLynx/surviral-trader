"""
Custom Freqtrade pairlist filter for Main (P4): excludes a pair from NEW
long entries if its price has been persistently below its own Bollinger
lower band (SMA20 - 2 std) for several candles in a row -- a sustained
breakdown, not a single-candle wick/false-positive spike below the band.
Applies to whatever ends up in Main's dynamic whitelist, memecoins
included if the volume/age/price/spread/volatility filters upstream ever
let one through -- there's no separate memecoin-specific code path here,
it's the same filter over the same pool.

Design choices, and why:

- Deliberately NOT applied to the Dip bot. Dip's entire thesis is the
  opposite: it exists specifically to buy pairs sitting BELOW their recent
  range on the theory that the drop is an overreaction. Excluding exactly
  the pairs Dip is designed to consider would defeat its purpose outright
  -- this file is only wired into Main's config.json, never Dip's.
- OHLCV is fetched via a direct HTTP call to Bybit's public V5
  `/v5/market/kline` endpoint (no key needed), not through Freqtrade's own
  `self._exchange`/ccxt wrapper -- same self-contained pattern as
  `get_market_context()`/`get_news_headlines()` in DeepSeekStrategy.py,
  and the only way this filter's actual data-fetching path could be
  exercised and verified from a dev sandbox with no live Freqtrade/ccxt
  connection, only public REST access.
- N (consecutive candles required below the band) defaults to 3, matching
  the strategy's own 1h timeframe -- roughly a 3-hour sustained breakdown,
  long enough to filter out a single wick but still be exclude a chair
  candidate before the entry logic would have to catch it on price alone.
  Exposed as a class constant, not hardcoded inline, so it can be tuned
  without touching the exclusion logic itself.
- Fails OPEN, never closed: if there isn't enough OHLCV history yet for a
  pair (fresh listing, fetch error), it is NOT excluded -- consistent with
  every other "fail honest, don't fabricate, don't block on a context
  failure" choice already made elsewhere in this codebase.
- Only ever removes candidates from the pool for NEW entries. Never closes
  an existing position, never touches stoploss/minimal_roi/max_open_trades/
  risk_guard.py/_parse_llm_decision -- a pairlist filter runs upstream of
  populate_entry_trend and has no path to an already-open trade.
"""
import logging
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)

BOLLINGER_WINDOW = 20
BOLLINGER_STD_MULT = 2.0
CONSECUTIVE_CANDLES_BELOW = 3  # N -- tune here, not inline in the logic below
KLINE_LIMIT = 60  # 1h candles: window(20) + N(3) + comfortable warmup margin

_BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"


def to_bybit_spot_symbol(pair: str) -> str:
    """'BTC/USDT' -> 'BTCUSDT' (Main trades spot)."""
    return pair.replace("/", "")


def bollinger_lower_band(closes: pd.Series) -> pd.Series:
    sma = closes.rolling(BOLLINGER_WINDOW).mean()
    std = closes.rolling(BOLLINGER_WINDOW).std()
    return sma - BOLLINGER_STD_MULT * std


def is_persistently_below_band(closes: pd.Series, n: int = CONSECUTIVE_CANDLES_BELOW) -> tuple[bool, str]:
    """The pure decision, independent of how `closes` was fetched -- this is
    the part unit-tested directly (see backtest/test_p4_pairlists.py)."""
    if len(closes) < BOLLINGER_WINDOW + n:
        return False, f"insufficient history ({len(closes)} candles) -- fail open, not excluded"
    lower = bollinger_lower_band(closes)
    recent_closes = closes.iloc[-n:]
    recent_lower = lower.iloc[-n:]
    if recent_lower.isna().any():
        return False, "Bollinger band not yet computable for the recent window -- fail open, not excluded"
    below = bool((recent_closes.values < recent_lower.values).all())
    if below:
        return True, f"price below its own SMA20-2std Bollinger lower band for {n} consecutive 1h candles"
    return False, "not persistently below the Bollinger lower band"


def fetch_recent_closes(pair: str, limit: int = KLINE_LIMIT, timeout: float = 8.0) -> pd.Series | None:
    symbol = to_bybit_spot_symbol(pair)
    try:
        r = requests.get(
            _BYBIT_KLINE_URL,
            params={"category": "spot", "symbol": symbol, "interval": "60", "limit": limit},
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        if not rows:
            return None
        return pd.Series([float(row[4]) for row in reversed(rows)])  # Bybit returns newest-first
    except Exception as e:
        log.warning("BollingerTrendPairList: kline fetch failed for %s: %s", pair, e)
        return None


try:
    from freqtrade.plugins.pairlist.IPairList import IPairList, SupportsBacktesting

    class BollingerTrendPairList(IPairList):
        """Freqtrade custom pairlist plugin -- see module docstring. Wired
        into freqtrade/user_data/config.json's pairlists chain as the last
        step, after Volume/Age/Price/Spread/Volatility. NOT wired into
        Dip's config -- see module docstring for why."""

        is_pairlist_filter = True
        supports_backtesting = SupportsBacktesting.NO  # calls a live external API, not backtest-safe

        @property
        def needstickers(self) -> bool:
            return False

        def short_desc(self) -> str:
            return (f"{self.name} (exclude if {CONSECUTIVE_CANDLES_BELOW} consecutive 1h "
                    f"closes below SMA20-2std)")

        def filter_pairlist(self, pairlist: list[str], tickers: dict[str, Any]) -> list[str]:
            kept = []
            for pair in pairlist:
                closes = fetch_recent_closes(pair)
                if closes is None:
                    kept.append(pair)  # fetch failed -- fail open
                    continue
                excluded, reason = is_persistently_below_band(closes)
                if excluded:
                    log.info("BollingerTrendPairList: excluding %s -- %s", pair, reason)
                else:
                    kept.append(pair)
            return kept

except ImportError:
    log.warning("freqtrade.plugins.pairlist.IPairList not importable -- "
                "BollingerTrendPairList class not defined (logic-only import mode)")
