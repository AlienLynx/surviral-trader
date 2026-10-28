"""
STATUS: NOT CONFIRMED BY BACKTEST. On the untouched 2024-01-01..2026-09 test window the chosen
variant lost 13.8% with a 49% max drawdown and a negative short leg (gates: DD <= 30%, >= 100
trades, short leg >= 0 -> 2 of 3 failed). Deployed dry-run only. See backtest/p17_trend_v2_report.md.

P17-B: Trend Ensemble v2 -- rule-based (NO LLM), daily, futures 1x isolated,
long AND short. The frozen v1 (freqtrade-trend-ensemble, spot long-only) stays
untouched as the comparison point; nothing here tunes it.

What v1 got wrong and v2 changes (each item is a declared design decision, not
a tuned parameter):
  1. Both directions. Same Donchian ensemble (5/10/20/40/60 days): a close
     above the prior P-day high votes long, a close below the prior P-day low
     votes short. Enter when >= min_votes periods agree.
  2. BTC regime filter. Longs only while BTC's PREVIOUS daily close is above its
     200-day SMA, shorts only while below (merged with merge_informative_pair,
     which lags the informative candle by one interval -- no lookahead).
  3. Exit = the OPPOSITE Donchian channel (long exits when the close breaks the
     prior `exit_channel`-day low, short when it breaks the prior high) plus a
     hard -15% stoploss. v1's native trailing (activation offset 0.10, i.e. an
     unprotected zone up to +10%) is removed -- the same dead-zone flaw fixed on
     Listing Fade.
  4. Hysteresis: a channel exit is vetoed (confirm_trade_exit) until the trade
     has been open MIN_HOLD_DAYS (3) daily candles. Stops are never vetoed.
  5. Volatility sizing from REALIZED vol read off the analyzed dataframe (v1
     kept a per-pair cache overwritten with the LAST value of the whole frame,
     which in a backtest leaks end-of-data volatility into every trade's size).
     stake = proposed_stake * clamp(TARGET_DAILY_VOL_PCT / realized_vol_pct,
     0.25, 1.5).
  6. Funding: a short is skipped when the PREVIOUS day's average funding rate,
     annualized, is below -10% (a short would pay that). History is available
     for Bybit (verified: `download-data --trading-mode futures` fetches the
     `funding_rate` candle type). Unknown funding fails OPEN (does not block).
  7. Protections: CooldownPeriod and StoplossGuard.

Configurable per config.json -> "trend_v2": {"min_votes": 2, "exit_channel": 10,
"allow_long": true, "allow_short": true} (defaults below; the allow_* switches
exist for the long-only comparison backtest). Live gate: TREND_V2_LIVE_MODE, same belt-and-suspenders as v1
(both it AND dry_run=false are needed for a real order).
"""
import logging
import os

import numpy as np
import pandas as pd
from freqtrade.strategy import IStrategy, merge_informative_pair
import _allocator_ctx  # P20: read-only context/weights volume (see that module)

_BOT_ID = "trend_v2"

log = logging.getLogger(__name__)

DONCHIAN_PERIODS = [5, 10, 20, 40, 60]
BTC_PAIR = "BTC/USDT:USDT"
FUNDING_PAYMENTS_PER_YEAR = 3 * 365  # Bybit settles every 8h


class TrendV2Strategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1d"
    can_short = True

    # Exits are the opposite Donchian channel + this hard stop; ROI disabled.
    minimal_roi = {"0": 100.0}
    stoploss = -0.15
    trailing_stop = False  # v1's trailing (offset 0.10) removed on purpose

    process_only_new_candles = True
    use_exit_signal = True
    startup_candle_count: int = 210  # BTC 200d SMA + margin

    # ---- declared defaults (overridable via config "trend_v2") -------------
    DEFAULT_MIN_VOTES = 2          # of the 5 periods -- chosen on TRAIN data (2021-2023), see AGENTS.md P17-B
    DEFAULT_EXIT_CHANNEL = 10      # days; 10 or 20 were the declared options -- 10 chosen on TRAIN data
    MIN_HOLD_DAYS = 3              # hysteresis: channel exits blocked before this
    BTC_SMA_DAYS = 200
    FUNDING_MIN_ANNUAL_SHORT = -0.10   # skip shorts if annualized funding < -10%
    VOL_LOOKBACK_DAYS = 20
    TARGET_DAILY_VOL_PCT = 4.0     # target daily stdev of returns, in %
    MIN_SIZE_MULTIPLIER = 0.25
    MAX_SIZE_MULTIPLIER = 1.5

    protections = [
        {"method": "CooldownPeriod", "stop_duration_candles": 2},
        {
            "method": "StoplossGuard",
            "lookback_period_candles": 20,
            "trade_limit": 3,
            "stop_duration_candles": 5,
            "only_per_pair": False,
        },
    ]

    # ------------------------------------------------------------------ params
    @property
    def _cfg(self) -> dict:
        return (self.config or {}).get("trend_v2", {}) or {}

    @property
    def min_votes(self) -> int:
        return int(self._cfg.get("min_votes", self.DEFAULT_MIN_VOTES))

    @property
    def exit_channel(self) -> int:
        return int(self._cfg.get("exit_channel", self.DEFAULT_EXIT_CHANNEL))

    @property
    def allow_long(self) -> bool:
        return bool(self._cfg.get("allow_long", True))

    @property
    def allow_short(self) -> bool:
        return bool(self._cfg.get("allow_short", True))

    def _live_mode_enabled(self) -> bool:
        return os.getenv("TREND_V2_LIVE_MODE", "false").strip().lower() == "true"

    # -------------------------------------------------------- data providers
    def _funding_timeframe(self) -> str:
        """Freqtrade's own name for the funding-rate candle timeframe on this
        exchange (Bybit files are stored as '<pair>-1h-funding_rate' even though
        the payments are 8h apart)."""
        try:
            return self.dp.get_funding_rate_timeframe()
        except Exception:
            return "8h"

    def informative_pairs(self):
        pairs = [(BTC_PAIR, "1d", "futures")]
        try:
            tf = self._funding_timeframe()
            for pair in self.dp.current_whitelist():
                pairs.append((pair, tf, "funding_rate"))
        except Exception:
            pass
        return pairs

    # ------------------------------------------------------------ indicators
    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        return 1.0

    @staticmethod
    def _daily_funding_annualized(funding: pd.DataFrame) -> pd.DataFrame:
        """8h funding-rate candles -> one row per UTC day with the day's mean
        rate annualized, APPLIED TO THE FOLLOWING DAY'S CANDLE (one-day lag).
        The first version aligned day D's mean with candle D; `freqtrade
        lookahead-analysis` flagged `funding_ann` as biased because a
        truncated data range gives day D a partial mean (fewer than 3
        payments). Using only fully completed prior days removes that."""
        if funding is None or len(funding) == 0:
            return pd.DataFrame({"date": pd.to_datetime([], utc=True), "funding_ann": []})
        rate_col = "funding_rate" if "funding_rate" in funding.columns else "open"
        f = funding[["date", rate_col]].rename(columns={rate_col: "rate"}).copy()
        f["day"] = f["date"].dt.floor("D")
        daily = f.groupby("day", as_index=False)["rate"].mean()
        daily["funding_ann"] = daily["rate"] * FUNDING_PAYMENTS_PER_YEAR
        daily["day"] = daily["day"] + pd.Timedelta(days=1)   # known only once the day is complete
        return daily.rename(columns={"day": "date"})[["date", "funding_ann"]]

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        for p in DONCHIAN_PERIODS:
            dataframe[f"dc_high_{p}"] = dataframe["high"].rolling(p).max().shift(1)
            dataframe[f"dc_low_{p}"] = dataframe["low"].rolling(p).min().shift(1)

        n = self.exit_channel
        dataframe["exit_low"] = dataframe["low"].rolling(n).min().shift(1)
        dataframe["exit_high"] = dataframe["high"].rolling(n).max().shift(1)

        ret = dataframe["close"].pct_change()
        dataframe["realized_vol_pct"] = ret.rolling(self.VOL_LOOKBACK_DAYS).std() * 100

        # --- BTC regime (previous daily close vs its 200d SMA, lookahead-safe)
        btc = self.dp.get_pair_dataframe(pair=BTC_PAIR, timeframe="1d")
        if len(btc):
            btc = btc.copy()
            btc["sma"] = btc["close"].rolling(self.BTC_SMA_DAYS).mean()
            btc = btc[["date", "close", "sma"]]
            dataframe = merge_informative_pair(dataframe, btc, self.timeframe, "1d", ffill=True)
            dataframe["btc_bull"] = (dataframe["close_1d"] > dataframe["sma_1d"]).astype(int)
            dataframe["btc_bear"] = (dataframe["close_1d"] < dataframe["sma_1d"]).astype(int)
        else:
            dataframe["btc_bull"] = 0
            dataframe["btc_bear"] = 0

        # --- funding (annualized daily mean). NaN = unknown = fail open.
        pair = metadata.get("pair", "")
        try:
            funding = self.dp.get_pair_dataframe(
                pair=pair, timeframe=self._funding_timeframe(), candle_type="funding_rate")
        except Exception:
            funding = None
        daily = self._daily_funding_annualized(funding)
        dataframe = dataframe.drop(columns=["funding_ann"], errors="ignore")
        dataframe = dataframe.merge(daily, on="date", how="left")
        dataframe["funding_ann"] = dataframe["funding_ann"].ffill(limit=3)
        return dataframe

    # --------------------------------------------------------------- signals
    def _votes(self, dataframe: pd.DataFrame):
        long_votes = pd.Series(0, index=dataframe.index)
        short_votes = pd.Series(0, index=dataframe.index)
        for p in DONCHIAN_PERIODS:
            long_votes += (dataframe["close"] > dataframe[f"dc_high_{p}"]).astype(int)
            short_votes += (dataframe["close"] < dataframe[f"dc_low_{p}"]).astype(int)
        return long_votes, short_votes

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        long_votes, short_votes = self._votes(dataframe)
        funding_ok = dataframe["funding_ann"].fillna(0.0) >= self.FUNDING_MIN_ANNUAL_SHORT

        long_cond = (
            (long_votes >= self.min_votes) & (dataframe["btc_bull"] == 1)
            & (dataframe["volume"] > 0) & self.allow_long
        )
        short_cond = (
            (short_votes >= self.min_votes)
            & (dataframe["btc_bear"] == 1)
            & funding_ok
            & (dataframe["volume"] > 0)
            & self.allow_short
        )
        dataframe.loc[long_cond, "enter_long"] = 1
        dataframe.loc[long_cond, "enter_tag"] = "donchian_long"
        dataframe.loc[short_cond, "enter_short"] = 1
        dataframe.loc[short_cond, "enter_tag"] = "donchian_short"
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        long_exit = dataframe["close"] < dataframe["exit_low"]
        short_exit = dataframe["close"] > dataframe["exit_high"]
        dataframe.loc[long_exit, "exit_long"] = 1
        dataframe.loc[long_exit, "exit_tag"] = "donchian_exit_long"
        dataframe.loc[short_exit, "exit_short"] = 1
        dataframe.loc[short_exit, "exit_tag"] = "donchian_exit_short"
        return dataframe

    # ------------------------------------------------------------ sizing/gates
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
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            vol = float(df["realized_vol_pct"].iloc[-1]) if len(df) else float("nan")
        except Exception:
            vol = float("nan")
        if not np.isfinite(vol) or vol <= 0:
            return proposed_stake
        mult = min(self.MAX_SIZE_MULTIPLIER, max(self.MIN_SIZE_MULTIPLIER, self.TARGET_DAILY_VOL_PCT / vol))
        sized = proposed_stake * mult
        if min_stake and sized < min_stake:
            return min_stake
        if max_stake and sized > max_stake:
            return max_stake
        return sized

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time, **kwargs) -> bool:
        """Hysteresis: veto a channel exit inside the first MIN_HOLD_DAYS
        candles. Only the strategy's own channel-exit tags are ever vetoed --
        stoploss, force-exit, liquidation etc. always go through."""
        if exit_reason and exit_reason.startswith("donchian_exit"):
            held_days = (current_time - trade.open_date_utc).total_seconds() / 86400
            if held_days < self.MIN_HOLD_DAYS:
                return False
        return True

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                            time_in_force: str, current_time, entry_tag, side: str, **kwargs) -> bool:
        blocked = _allocator_ctx.entry_block(pair)
        if blocked:
            log.info("confirm_trade_entry: rejecting %s -- announcement guard: %s", pair, blocked)
            return False
        if not self.config.get("dry_run", True) and not self._live_mode_enabled():
            log.warning("confirm_trade_entry: rejecting %s -- dry_run is False but "
                        "TREND_V2_LIVE_MODE is not 'true' (second gate)", pair)
            return False
        return True
