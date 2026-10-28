"""
Custom Freqtrade pairlist filter for the Shorts bot (P4): excludes pairs
from NEW entries when funding rate is deeply negative -- a persistently
negative funding rate on a perpetual future means shorts are crowded and
paying longs to hold, which is exactly the setup for a short squeeze (a
sharp reversal against the crowded short side). Falls back to a daily
RSI(14) oversold check only when funding data itself isn't available for
that pair.

Design choices, and why:

- Funding rate is fetched via a direct HTTP call to Bybit's public V5
  `/v5/market/tickers` endpoint (no key needed), not through Freqtrade's
  own `self._exchange`/ccxt wrapper. Same self-contained pattern as
  `get_market_context()`/`get_news_headlines()` in DeepSeekShortsStrategy.py
  (see that file's own docstring for the reasoning) -- and the only way
  this filter's real data-fetching path could actually be exercised and
  verified from a dev sandbox with no live Freqtrade/ccxt exchange
  connection available, only public REST access.
- Priority is funding-rate-or-nothing, per the brief: if funding is
  available for a pair, RSI is not computed for it at all, regardless of
  what funding says. RSI is ONLY the fallback for pairs where funding
  itself could not be fetched (API error, pair not actually a perpetual on
  Bybit, etc).
- Two different thresholds for majors (BTC, ETH) vs. everything else
  (alts/memecoins) -- majors get a tighter (less negative) bar because
  their funding rates are structurally less extreme in normal conditions;
  the same -0.05%/8h that's unremarkable on a volatile alt would be a
  much rarer, more significant signal on BTC/ETH.
- Fails OPEN, never closed: if BOTH funding and RSI are unavailable for a
  pair (data outage, API error), the pair is NOT excluded. A filter that
  silently excludes everything during an outage would be worse than one
  that does nothing that cycle -- consistent with every other "fail
  honest, never fabricate, never block trading on a context failure"
  choice already made elsewhere in this codebase (get_market_context(),
  get_news_headlines()).
- Only ever removes candidates from the pool for NEW entries. Never closes
  an existing position, never touches stoploss/minimal_roi/max_open_trades/
  risk_guard.py/_parse_llm_decision -- this is a pairlist filter, it runs
  before populate_entry_trend ever sees a pair, and has no path to affect
  an already-open trade at all.
"""
import logging
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)

MAJORS = {"BTC", "ETH"}
MAJOR_FUNDING_THRESHOLD = -0.0002  # -0.02% per 8h
ALT_FUNDING_THRESHOLD = -0.0005  # -0.05% per 8h
RSI_FALLBACK_PERIOD = 14
RSI_FALLBACK_THRESHOLD = 25.0
RSI_KLINE_LIMIT = 40  # daily candles fetched -- comfortably more than needed for RSI(14) + warmup

_BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"
_BYBIT_KLINE_URL = "https://api.bybit.com/v5/market/kline"


def base_currency(pair: str) -> str:
    """'BTC/USDT:USDT' -> 'BTC'. Works for both the futures unified symbol
    Shorts actually uses and a plain 'BTC/USDT' spot pair, in case this is
    ever reused elsewhere."""
    return pair.split("/")[0]


def is_major(pair: str) -> bool:
    return base_currency(pair) in MAJORS


def funding_threshold_for(pair: str) -> float:
    return MAJOR_FUNDING_THRESHOLD if is_major(pair) else ALT_FUNDING_THRESHOLD


def to_bybit_linear_symbol(pair: str) -> str:
    """'BTC/USDT:USDT' -> 'BTCUSDT' (Bybit's linear-perpetual symbol)."""
    return pair.split(":")[0].replace("/", "")


def compute_rsi(closes: pd.Series, period: int = RSI_FALLBACK_PERIOD) -> float | None:
    """Plain-pandas Wilder-style RSI, same approach as DeepSeekDipStrategy.py's
    rsi_14 indicator -- no ta-lib dependency. Returns None if there isn't
    enough data yet."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    last_avg_gain = avg_gain.iloc[-1]
    last_avg_loss = avg_loss.iloc[-1]
    if pd.isna(last_avg_gain) or pd.isna(last_avg_loss):
        return None
    if last_avg_loss == 0:
        return 100.0
    rs = last_avg_gain / last_avg_loss
    return 100 - (100 / (1 + rs))


def should_exclude(pair: str, funding_rate: float | None, rsi_daily: float | None) -> tuple[bool, str]:
    """The pure decision, independent of how funding_rate/rsi_daily were
    fetched -- this is the part unit-tested directly (see
    backtest/test_p4_pairlists.py), so the real HTTP-fetching wrapper below
    can change without risking the actual threshold logic."""
    if funding_rate is not None:
        threshold = funding_threshold_for(pair)
        excluded = funding_rate < threshold
        reason = (f"funding {funding_rate * 100:.4f}%/8h < {threshold * 100:.4f}% threshold"
                  if excluded else f"funding {funding_rate * 100:.4f}%/8h OK")
        return excluded, reason
    if rsi_daily is not None:
        excluded = rsi_daily < RSI_FALLBACK_THRESHOLD
        reason = (f"funding unavailable, RSI(14) daily {rsi_daily:.1f} < {RSI_FALLBACK_THRESHOLD} fallback"
                  if excluded else f"funding unavailable, RSI(14) daily {rsi_daily:.1f} OK")
        return excluded, reason
    return False, "funding and RSI both unavailable -- fail open, not excluded"


def fetch_funding_rate(pair: str, timeout: float = 8.0) -> float | None:
    symbol = to_bybit_linear_symbol(pair)
    try:
        r = requests.get(_BYBIT_TICKERS_URL, params={"category": "linear", "symbol": symbol}, timeout=timeout)
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        if not rows:
            return None
        raw = rows[0].get("fundingRate")
        return float(raw) if raw not in (None, "") else None
    except Exception as e:
        log.warning("FundingRsiPairList: funding rate fetch failed for %s: %s", pair, e)
        return None


def fetch_daily_rsi(pair: str, timeout: float = 8.0) -> float | None:
    symbol = to_bybit_linear_symbol(pair)
    try:
        r = requests.get(
            _BYBIT_KLINE_URL,
            params={"category": "linear", "symbol": symbol, "interval": "D", "limit": RSI_KLINE_LIMIT},
            timeout=timeout,
        )
        r.raise_for_status()
        rows = r.json().get("result", {}).get("list", [])
        if not rows:
            return None
        closes = pd.Series([float(row[4]) for row in reversed(rows)])  # Bybit returns newest-first
        return compute_rsi(closes)
    except Exception as e:
        log.warning("FundingRsiPairList: daily RSI fetch failed for %s: %s", pair, e)
        return None


try:
    from freqtrade.plugins.pairlist.IPairList import IPairList, SupportsBacktesting

    class FundingRsiPairList(IPairList):
        """Freqtrade custom pairlist plugin -- see module docstring. Wired
        into freqtrade-shorts/user_data/config.json's pairlists chain as the
        last step, after Volume/Age/Price/Spread/Volatility."""

        is_pairlist_filter = True
        supports_backtesting = SupportsBacktesting.NO  # calls a live external API, not backtest-safe

        @property
        def needstickers(self) -> bool:
            return False

        def short_desc(self) -> str:
            return (f"{self.name} (funding<{MAJOR_FUNDING_THRESHOLD*100:.2f}%/8h majors, "
                    f"<{ALT_FUNDING_THRESHOLD*100:.2f}%/8h alts; RSI(14)daily<{RSI_FALLBACK_THRESHOLD} fallback)")

        def filter_pairlist(self, pairlist: list[str], tickers: dict[str, Any]) -> list[str]:
            kept = []
            for pair in pairlist:
                funding = fetch_funding_rate(pair)
                rsi = fetch_daily_rsi(pair) if funding is None else None
                excluded, reason = should_exclude(pair, funding, rsi)
                if excluded:
                    log.info("FundingRsiPairList: excluding %s -- %s", pair, reason)
                else:
                    kept.append(pair)
            return kept

except ImportError:
    # Allows this file's pure logic (should_exclude, compute_rsi, fetch_*) to
    # be imported and unit-tested standalone outside a real Freqtrade
    # install -- see backtest/test_p4_pairlists.py. The actual IPairList
    # subclass is simply absent in that case; Freqtrade itself is always
    # present inside the real container.
    log.warning("freqtrade.plugins.pairlist.IPairList not importable -- "
                "FundingRsiPairList class not defined (logic-only import mode)")
