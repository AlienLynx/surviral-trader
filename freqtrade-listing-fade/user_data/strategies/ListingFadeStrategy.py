"""
P11: seventh bot, isolated on purpose -- a RULE-BASED strategy, no LLM
call at all. Faster, cheaper (no API bill), and never blocked by a
provider's rate limit or an EOL model, unlike every DeepSeek*Strategy.py
bot in this repo. Thesis: short freshly-listed coins whose initial pump
has already visibly fizzled, not coins still pumping (that's Moonshot's
job, the opposite bet).

Pool: Bybit USDT perpetuals, 2-30 days since listing (config.json's own
AgeFilter -- the strategy does not re-check age itself, it trusts the
pairlist). Futures-only to unlock shorting, leverage always 1.0 (same
"do not add leverage on top of an unproven strategy" reasoning as every
other futures bot here -- see DeepSeekShortsStrategy.py's own
docstring).

Entry (all three, evaluated every new 1h candle):
  1. Price has dropped >=25% from its own high-since-listing (the
     "listing high" is approximated as the running max of `high` over
     however much history Freqtrade has for this pair -- for a coin
     that's only 2-30 days old, that IS its listing history, no
     separate fetch needed).
  2. Latest 1h close is below its own EMA20 (confirms an actual
     downtrend, not just a one-candle dip off the high).
  3. Perpetual funding rate is NOT deeply negative (checked in
     confirm_trade_entry, same direct-Bybit-REST pattern as
     DeepSeekShortsStrategy.py's own funding check) -- a deeply
     negative funding rate means the market is already crowded short,
     which is exactly the setup for a short squeeze against a LATE
     short entry like this one.

Exit: hard stoploss -12%, Freqtrade's native trailing stop once in
profit (not a custom peak-tracker -- this is a rule strategy, the
built-in mechanism is simpler and just as correct here), and a 14-day
max-hold backstop via custom_exit for a trade that neither confirms
nor reverts.

FADE_LIVE_MODE is a second, independent live gate on top of
FREQTRADE_LISTING_FADE__DRY_RUN -- both must be true for a real order
to ever be possible. Same belt-and-suspenders pattern as Survival
Shorts' SURVIVAL_SHORTS_LIVE_MODE.
"""
import logging
import os

import pandas as pd
import requests
from freqtrade.strategy import IStrategy
import _allocator_ctx  # P20: read-only context/weights volume (see that module)

_BOT_ID = "listing_fade"

log = logging.getLogger(__name__)


def reentry_blocked(closed, now, cooldown_h) -> bool:
    """P26 re-entry guard: True when this pair was closed by a stop_loss less than `cooldown_h` hours before `now`. `closed` = iterable of (exit_reason, close_datetime). Pure function;
    cooldown_h <= 0 or no data never blocks. (Trailing-stop exits are wins and never block.)"""
    if not cooldown_h or cooldown_h <= 0:
        return False
    for reason, closed_at in closed:
        if reason == "stop_loss" and closed_at is not None:
            age = (now - closed_at).total_seconds()
            if 0 <= age < cooldown_h * 3600:
                return True
    return False


class ListingFadeStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True

    # Trailing stop is the real take-profit mechanism here (see module
    # docstring) -- minimal_roi is set high enough to never fire on its
    # own, same "disabled in practice" pattern as Moonshot's.
    minimal_roi = {"0": 10.0}
    stoploss = -0.12
    trailing_stop = True
    trailing_stop_positive = 0.03
    # Fix: dead-zone reduction -- was 0.06, cutting the unprotected drawdown
    # window between entry and trailing-stop activation (a trade reversing
    # before +6% had zero protection and could ride all the way to the
    # full -12% stoploss). trailing_stop_positive/stoploss unchanged.
    # Must stay strictly greater than trailing_stop_positive (Freqtrade
    # config validation rejects offset <= positive; 0.03/0.03 was invalid
    # and failed to start -- "trailing_stop_positive_offset needs to be
    # greater than trailing_stop_positive").
    trailing_stop_positive_offset = 0.035
    trailing_only_offset_is_reached = True

    process_only_new_candles = True
    startup_candle_count: int = 24

    # Backstop for a short that neither confirms (stoploss) nor reverts
    # far enough to trail -- covers a coin that just drifts sideways
    # after entry, permanently occupying one of only 4 slots otherwise.
    MAX_HOLD_DAYS = 14

    PCT_FROM_HIGH_THRESHOLD = -25.0  # price must be at/below -25% from its listing high
    EMA_PERIOD = 20
    FUNDING_NOT_TOO_NEGATIVE_THRESHOLD = -0.0003  # below this -> crowded short, squeeze risk, skip

    def __init__(self, config: dict) -> None:
        super().__init__(config)

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        # Always 1.0 -- unlocking shorting, not adding leverage on top of
        # an unproven rule strategy. See module docstring.
        return 1.0

    def _live_mode_enabled(self) -> bool:
        """Second, independent live gate for THIS bot only -- both this
        AND FREQTRADE_LISTING_FADE__DRY_RUN=false are required before
        confirm_trade_entry() below allows a real order. Same
        belt-and-suspenders pattern as Survival Shorts' own gate."""
        return os.getenv("FADE_LIVE_MODE", "false").strip().lower() == "true"

    def custom_stake_amount(
        self, pair: str, current_time, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs
    ) -> float:
        # P20: the allocator's weight scales the position size (enforce mode only; 1.0 otherwise). Entries,
        # exits, stops and ROI are not touched. See _allocator_ctx.stake_multiplier for the fail-safes.
        stake = self._custom_stake_amount_base(
            pair, current_time, current_rate, proposed_stake, min_stake, max_stake, leverage, entry_tag, side, **kwargs)
        mult = _allocator_ctx.stake_multiplier(_BOT_ID)
        if mult == 1.0:
            return stake
        scaled = stake * mult
        if mult > 1.0:                                  # grow policy: a bot that earns money may use a bigger stake, never above what the exchange / wallet allows
            return min(scaled, max_stake) if max_stake else scaled
        if min_stake and scaled < min_stake:
            return min_stake
        return scaled

    def _custom_stake_amount_base(
        self, pair: str, current_time, current_rate: float,
        proposed_stake: float, min_stake: float | None, max_stake: float,
        leverage: float, entry_tag: str | None, side: str, **kwargs
    ) -> float:
        return proposed_stake

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Running max of `high` over all history Freqtrade has for this
        # pair -- for a 2-30-day-old listing this approximates the true
        # since-listing high without a separate fetch. cummax(), not a
        # fixed-window rolling max, so it never resets partway through
        # the available history.
        dataframe["listing_high"] = dataframe["high"].cummax()
        dataframe["pct_from_high"] = (
            (dataframe["close"] - dataframe["listing_high"]) / dataframe["listing_high"]
        ) * 100
        dataframe["ema20"] = dataframe["close"].ewm(span=self.EMA_PERIOD, adjust=False).mean()
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        if len(dataframe) < self.EMA_PERIOD:
            return dataframe

        condition = (
            (dataframe["pct_from_high"] <= self.PCT_FROM_HIGH_THRESHOLD)
            & (dataframe["close"] < dataframe["ema20"])
        )
        dataframe.loc[condition, "enter_short"] = 1
        dataframe.loc[condition, "enter_tag"] = "listing_fade"

        pair = metadata.get("pair", "?")
        if condition.iloc[-1] if len(condition) else False:
            log.info("ListingFade: rule-based short signal for %s (%.1f%% from listing high, close < EMA20)",
                      pair, dataframe["pct_from_high"].iloc[-1])

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Exits handled by stoploss/trailing_stop/custom_exit -- nothing here.
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(self, pair: str, trade, current_time, current_rate: float,
                     current_profit: float, **kwargs) -> str | None:
        days_open = (current_time - trade.open_date_utc).days
        if days_open >= self.MAX_HOLD_DAYS:
            return "max_hold_days_reached"
        return None

    def _reentry_cooldown_h(self) -> float:
        """Hours a pair stays blocked after a stop-loss: the dashboard value (bot_params.json) or, without a fresh file, FADE_REENTRY_COOLDOWN_H (default 24; 0 = off)."""
        try:
            default = float(os.getenv("FADE_REENTRY_COOLDOWN_H", "24") or 24)
        except ValueError:
            default = 24.0
        return _allocator_ctx.bot_param(_BOT_ID, "reentry_cooldown_h", default)

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time, entry_tag, side: str, **kwargs) -> bool:
        """
        Two checks, in order: the live-mode gate (see module docstring),
        then the funding-rate veto. Direct Bybit REST fetch, same
        pattern as DeepSeekShortsStrategy.py's own funding check --
        fails OPEN (allows the trade) if the fetch itself fails, never
        silently blocks on a data outage.
        """
        blocked = _allocator_ctx.entry_block(pair)
        if blocked:
            log.info("confirm_trade_entry: rejecting %s -- announcement guard: %s", pair, blocked)
            return False
        cooldown_h = self._reentry_cooldown_h()
        if cooldown_h > 0:
            try:
                from freqtrade.persistence import Trade
                closed = [(t.exit_reason, t.close_date_utc) for t in Trade.get_trades_proxy(pair=pair, is_open=False)]
                if reentry_blocked(closed, current_time, cooldown_h):
                    log.info("confirm_trade_entry: rejecting %s -- re-entry guard: stopped out less than %gh ago", pair, cooldown_h)
                    return False
            except Exception as e:      # noqa: BLE001 -- fail open: a guard problem must never stop trading
                log.warning("re-entry guard skipped for %s: %s: %s", pair, type(e).__name__, e)
        if not self.config.get("dry_run", True) and not self._live_mode_enabled():
            log.warning(
                "confirm_trade_entry: rejecting %s -- dry_run is False but "
                "FADE_LIVE_MODE is not 'true' (second gate)", pair
            )
            return False

        if side != "short":
            # This strategy never sets enter_long, so this should not
            # happen in practice -- defensive only.
            return False

        symbol = pair.split(":")[0].replace("/", "")
        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if rows:
                raw = rows[0].get("fundingRate")
                funding_rate = float(raw) if raw not in (None, "") else None
                if funding_rate is not None and funding_rate < self.FUNDING_NOT_TOO_NEGATIVE_THRESHOLD:
                    log.info(
                        "confirm_trade_entry: rejecting %s -- funding %.4f%%/8h < %.4f%% "
                        "(crowded short, squeeze risk)",
                        pair, funding_rate * 100, self.FUNDING_NOT_TOO_NEGATIVE_THRESHOLD * 100,
                    )
                    return False
        except Exception as e:
            log.warning("confirm_trade_entry: funding fetch failed for %s: %s -- allowing entry", pair, e)

        return True
