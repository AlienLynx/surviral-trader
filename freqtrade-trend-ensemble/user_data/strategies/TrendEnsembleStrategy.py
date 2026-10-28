"""
P11: eighth bot, isolated on purpose -- a RULE-BASED strategy, no LLM
call at all (same reasoning as ListingFadeStrategy.py's own docstring:
faster, cheaper, never blocked by a provider's rate limit). Thesis:
classic multi-period Donchian-channel breakout trend-following on the
top-20-by-volume spot pairs, long-only.

Daily timeframe on purpose -- a 5-60 day Donchian ensemble is a
low-frequency signal; checking it once per day is honest to what it
actually is, not an attempt to trade it intraday.

Entry: an ENSEMBLE vote across 5 Donchian periods (5/10/20/40/60 days).
For each period P, the pair "breaks out" on this candle if today's
close is above the highest `high` of the prior P days (rolling max,
shifted by 1 so today's own candle is never included in its own
breakout level). Enters long only when at least ENSEMBLE_MIN_VOTES of
the 5 periods agree -- a single short-period breakout alone is noise,
agreement across periods of very different length is a much stronger
trend signal.

Exit: Freqtrade's native trailing stop from the trade's own peak profit
(the spec's "trailing-stop from peak"), with a hard stoploss as the
backstop Freqtrade requires regardless.

Position sizing is volatility-scaled, not a flat stake: a pair with
LOWER recent volatility than TARGET_VOLATILITY_PCT gets a BIGGER stake
(up to MAX_SIZE_MULTIPLIER), a more volatile one gets a SMALLER stake
(down to MIN_SIZE_MULTIPLIER) -- same $ risk exposure across very
different pairs instead of the same $ stake regardless of how much a
pair actually moves.

TREND_LIVE_MODE is a second, independent live gate on top of
FREQTRADE_TREND__DRY_RUN -- both must be true for a real order to ever
be possible. Same belt-and-suspenders pattern as Survival Shorts' and
ListingFadeStrategy's own gates.
"""
import logging
import os

import pandas as pd
from freqtrade.strategy import IStrategy

log = logging.getLogger(__name__)

DONCHIAN_PERIODS = [5, 10, 20, 40, 60]


class TrendEnsembleStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"

    # Trailing stop is the real exit mechanism here (see module
    # docstring) -- minimal_roi set high enough to never fire on its own.
    minimal_roi = {"0": 10.0}
    stoploss = -0.15
    trailing_stop = True
    trailing_stop_positive = 0.05
    trailing_stop_positive_offset = 0.10
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count: int = max(DONCHIAN_PERIODS) + 5

    ENSEMBLE_MIN_VOTES = 3  # of len(DONCHIAN_PERIODS)=5 -- majority, not unanimous

    # Volatility-based position sizing. TARGET_VOLATILITY_PCT is the
    # "normal" 20-day high-low range a pair should have to get exactly
    # proposed_stake; more volatile than that shrinks the stake, less
    # volatile grows it, clamped to [MIN_SIZE_MULTIPLIER, MAX_SIZE_MULTIPLIER]
    # so no single pair ever gets a wildly outsized or tiny position.
    VOLATILITY_LOOKBACK_DAYS = 20
    TARGET_VOLATILITY_PCT = 5.0
    MIN_SIZE_MULTIPLIER = 0.25
    MAX_SIZE_MULTIPLIER = 1.5

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._volatility_cache: dict[str, float] = {}

    def _live_mode_enabled(self) -> bool:
        """Second, independent live gate for THIS bot only -- both this
        AND FREQTRADE_TREND__DRY_RUN=false are required before
        confirm_trade_entry() below allows a real order."""
        return os.getenv("TREND_LIVE_MODE", "false").strip().lower() == "true"

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        for period in DONCHIAN_PERIODS:
            dataframe[f"donchian_high_{period}"] = dataframe["high"].rolling(period).max().shift(1)

        dataframe["volatility_pct"] = (
            (dataframe["high"].rolling(self.VOLATILITY_LOOKBACK_DAYS).max()
             - dataframe["low"].rolling(self.VOLATILITY_LOOKBACK_DAYS).min())
            / dataframe["low"].rolling(self.VOLATILITY_LOOKBACK_DAYS).min()
        ) * 100

        pair = metadata.get("pair")
        if pair and len(dataframe) and pd.notna(dataframe["volatility_pct"].iloc[-1]):
            self._volatility_cache[pair] = float(dataframe["volatility_pct"].iloc[-1])

        return dataframe

    def _breakout_vote_count(self, dataframe: pd.DataFrame) -> pd.Series:
        votes = pd.Series(0, index=dataframe.index)
        for period in DONCHIAN_PERIODS:
            votes += (dataframe["close"] > dataframe[f"donchian_high_{period}"]).astype(int)
        return votes

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_tag"] = ""

        if len(dataframe) < max(DONCHIAN_PERIODS) + 1:
            return dataframe

        votes = self._breakout_vote_count(dataframe)
        condition = votes >= self.ENSEMBLE_MIN_VOTES
        dataframe.loc[condition, "enter_long"] = 1
        dataframe.loc[condition, "enter_tag"] = "donchian_ensemble"

        pair = metadata.get("pair", "?")
        if len(votes) and condition.iloc[-1]:
            log.info("TrendEnsemble: rule-based breakout signal for %s (%d/%d periods agree)",
                      pair, int(votes.iloc[-1]), len(DONCHIAN_PERIODS))

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Exits handled by stoploss/trailing_stop -- nothing here.
        dataframe["exit_long"] = 0
        return dataframe

    def custom_stake_amount(
        self, pair: str, current_time, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs
    ) -> float:
        vol = self._volatility_cache.get(pair)
        if not vol or vol <= 0:
            return proposed_stake
        multiplier = self.TARGET_VOLATILITY_PCT / vol
        multiplier = min(self.MAX_SIZE_MULTIPLIER, max(self.MIN_SIZE_MULTIPLIER, multiplier))
        sized = proposed_stake * multiplier
        if min_stake and sized < min_stake:
            return min_stake
        if max_stake and sized > max_stake:
            return max_stake
        return sized

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time, entry_tag, side: str, **kwargs) -> bool:
        """This bot had no confirm_trade_entry before -- added solely
        for the second live-mode gate (see module docstring)."""
        if not self.config.get("dry_run", True) and not self._live_mode_enabled():
            log.warning(
                "confirm_trade_entry: rejecting %s -- dry_run is False but "
                "TREND_LIVE_MODE is not 'true' (second gate)", pair
            )
            return False
        return True
