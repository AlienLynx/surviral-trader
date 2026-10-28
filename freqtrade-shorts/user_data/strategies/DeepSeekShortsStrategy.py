"""
Same DeepSeek-driven approach as ../../freqtrade/user_data/strategies/DeepSeekStrategy.py,
extended for futures + shorting. See that file's docstring and
../../AGENTS.md for the full history of fixes this design already
incorporates (model name churn, empty-content-on-reasoning-models,
OpenRouter free-tier volatility, etc.) -- all of it applies here too,
duplicated rather than imported for the same reason as the original:
this container doesn't have the other strategy's module mounted.

Key differences from the spot/long-only strategy:
  - can_short = True, trading_mode "futures" (set in config.json)
  - leverage() always returns 1.0. Freqtrade's own docs say explicitly:
    "Do not trade with a leverage > 1 using a strategy that hasn't
    shown positive results in a live run using the spot market."
    This strategy has zero live track record. Futures mode is used
    here ONLY to unlock short-selling, not to add leverage on top of
    an unproven strategy.
  - DeepSeek is asked for a 3-way decision: buy (long) / sell_short
    (short) / hold, instead of the 2-way buy/hold.
"""
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote

import pandas as pd
import _second_opinion  # P5: same-directory module, see its own docstring
import requests
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
import _allocator_ctx  # P20: read-only context/weights volume (see that module)

_BOT_ID = "shorts"

log = logging.getLogger(__name__)

_market_context_cache = {"text": None, "fetched_at": 0}
_MARKET_CONTEXT_TTL_SECONDS = 47 * 60  # staggered vs other bots to avoid simultaneous CoinGecko 429s

_openrouter_free_models_cache = {"list": None, "fetched_at": 0}
_OPENROUTER_FREE_LIST_TTL_SECONDS = 6 * 60 * 60

# See DeepSeekStrategy.py's own SYSTEM_PROMPT comment for the backtest finding
# that motivated this edit (309/310 hold across a 30-day/10-pair replay,
# missing a +27% move). Shorts carries the same fix, adapted for two-way
# direction: with both long AND short available every cycle, an equally high
# "mostly hold" bar cuts opportunity even more than on a long-only bot, so
# it needed this at least as much as Main did, not less. 2026-09-22.
SYSTEM_PROMPT = (
    "You are the entry-signal brain for a small futures trading bot that "
    "can go long OR short, always at 1x leverage (no leverage risk, just "
    "directional flexibility). You will be shown recent price action for "
    "one pair, its risk tier, broader market context, recent news headlines "
    "(crypto and macro/geopolitical -- oil, wars, sanctions, anything that "
    "could move markets in either direction), and recent track "
    "record. Reply with EXACTLY one line in this format: "
    "'hold: <reason under 15 words>' or 'buy: <reason under 15 words>' "
    "(go long) or 'sell_short: <reason under 15 words>' (go short). "
    "Weigh the evidence evenhandedly instead of defaulting to hold -- with "
    "two directions available, a clear move in either one is a legitimate "
    "reason to act on its own; it does not need to be an exceptional or "
    "rare-looking setup, and treating 'hold' as automatically safe when the "
    "numbers already favor a direction is a source of missed opportunity, "
    "not real caution. Require a clearer edge for 'satellite' tier pairs "
    "than 'core'. If recent trades in this tier have been losing more often "
    "than winning, be more cautious than usual. But 'no closed trades yet' "
    "is NOT the same as a bad track record -- it just means the system is "
    "new. A tiny sample (fewer than ~10 closed trades) is also not a real track "
    "record -- judge by price and context, not a fraction with a tiny denominator. "
    "Never cite 'no track record' by itself as a reason to hold; judge "
    "each pair on the price action and context alone until real trade "
    "history actually exists. This trading capital is limited and losses are "
    "not recoverable from a new deposit. Capital preservation is an equal "
    "priority to generating profit -- factor this into every entry and sizing "
    "decision, not just into stoploss placement. The headlines are real but unverified by you "
    "-- weigh them, don't blindly trust a single alarming headline. You have "
    "TWO directions for a reason: a bot that only ever goes long is half a "
    "bot. Every cycle, genuinely argue the short case too -- a sharp drop "
    "with rising volatility, a failed breakdown, bad project-specific news -- "
    "and take it when the evidence favors down over up. Slots are scarce (at "
    "most 5 open trades across the whole pool): only act on your highest-"
    "conviction setups in either direction, and 'everything is up because BTC "
    "is up' alone is a hold, not a long. This account must survive on its own "
    "results while paying a monthly operating bill -- holding cash is not "
    "free, so when the evidence genuinely favors a direction, act."
)


def get_market_context() -> str:
    now = time.time()
    if _market_context_cache["text"] and (now - _market_context_cache["fetched_at"]) < _MARKET_CONTEXT_TTL_SECONDS:
        return _market_context_cache["text"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/global", timeout=8)
        r.raise_for_status()
        data = r.json().get("data", {})
        cap_change = data.get("market_cap_change_percentage_24h_usd")
        btc_dominance = data.get("market_cap_percentage", {}).get("btc")
        text = (
            f"Total crypto market cap 24h change: {cap_change:.2f}%. "
            f"BTC dominance: {btc_dominance:.1f}%."
            if cap_change is not None and btc_dominance is not None
            else "unavailable"
        )
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 429:
            log.warning("Market context: CoinGecko 429 rate limit -- backing off until TTL expiry")
        else:
            log.warning("Market context fetch failed: %s", e)
        text = "unavailable"
    except Exception as e:
        log.warning("Market context fetch failed: %s", e)
        text = "unavailable"
    _market_context_cache["text"] = text
    _market_context_cache["fetched_at"] = now
    return text


_fng_cache = {"text": None, "fetched_at": 0}
_FNG_TTL_SECONDS = 45 * 60  # P8: same cadence as news


def get_fear_greed_index() -> str | None:
    """P8 item 1 -- see Main's DeepSeekStrategy.py for the full reasoning.
    Excluded (None) on any failure, never a fake neutral or
    stale-beyond-TTL value."""
    now = time.time()
    if _fng_cache["text"] is not None and (now - _fng_cache["fetched_at"]) < _FNG_TTL_SECONDS:
        return _fng_cache["text"]
    try:
        r = requests.get("https://api.alternative.me/fng/", timeout=8)
        r.raise_for_status()
        entry = (r.json().get("data") or [{}])[0]
        value = entry.get("value")
        classification = entry.get("value_classification")
        if value is None or classification is None:
            raise ValueError("unexpected response shape")
        text = f"Fear & Greed Index: {value} ({classification})"
    except Exception as e:
        log.warning("fear_greed: fetch failed: %s -- excluded from this cycle", e)
        return None
    _fng_cache["text"] = text
    _fng_cache["fetched_at"] = now
    return text


_news_cache = {"text": None, "fetched_at": 0}
_NEWS_CACHE_TTL_SECONDS = 45 * 60  # same cadence as market context, shared across all pairs

# Wave-1 patch 1.2: the two "nothing to show" outcomes used to collapse into
# the same "unavailable" string, so the model (and the logs) couldn't tell
# "genuinely no news on this asset" apart from "the RSS fetch itself failed".
# These are two distinct, fixed strings -- never reuse "unavailable" for
# either case again.
NEWS_NO_NEWS = "no news found for this asset"
NEWS_FEED_DOWN = (
    "news feed unavailable due to fetch error; "
    "do NOT infer absence of bad news from this -- decide on price/"
    "numeric context only or hold"
)


def get_news_headlines() -> str:
    """
    Real headlines, not just a number -- Google News RSS search, genuinely
    free (no key, no signup, no rate-limit tier to hit) and general-purpose
    enough to cover both crypto-specific news and broader macro/geopolitical
    events (oil, wars, sanctions) that could move markets. An earlier
    attempt used a "free" crypto news API found via search that turned out
    to require x402 micropayments in production -- verify a source
    actually works before trusting a search snippet's "no key required"
    claim (see AGENTS.md). Cached like market context. Returns
    NEWS_NO_NEWS when the fetch succeeded but found nothing, or
    NEWS_FEED_DOWN when the fetch itself failed -- deliberately two
    different strings (wave-1 patch 1.2) so the model isn't told the
    same "unavailable" for both and can't read a down feed as a clean
    "no bad news" signal.
    """
    now = time.time()
    if _news_cache["text"] is not None and (now - _news_cache["fetched_at"]) < _NEWS_CACHE_TTL_SECONDS:
        return _news_cache["text"]

    queries = ["bitcoin crypto market", "oil price war sanctions economy"]
    headlines: list[str] = []
    try:
        for q in queries:
            url = f"https://news.google.com/rss/search?q={quote(q)}&hl=en-US&gl=US&ceid=US:en"
            r = requests.get(url, timeout=8)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:3]:
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    headlines.append(title_el.text)
        text = "; ".join(headlines[:6]) if headlines else NEWS_NO_NEWS
    except Exception as e:
        log.warning("News headlines fetch failed: %s", e)
        text = NEWS_FEED_DOWN

    _news_cache["text"] = text
    _news_cache["fetched_at"] = now
    return text


def get_survival_context() -> str:
    """
    Survival framing for the prompt: the account must cover its own monthly
    operating bill and grow toward a payout goal within a fixed runway, with
    zero withdrawals until then. Read from env (Portainer stack vars, same
    pattern as the API keys) so the numbers can change without a code push --
    Freqtrade containers cannot see data/config.json, hence env, not the
    shared config file. Never fabricated: missing vars fall back to the
    documented defaults, and the string always states the numbers it used.
    """
    monthly = os.getenv("SURVIVAL_MONTHLY_USD", "60")
    runway = os.getenv("SURVIVAL_RUNWAY_MONTHS", "4")
    mode = os.getenv("SURVIVAL_MODE", "grow")
    target = os.getenv("SURVIVAL_TARGET_MONTHLY_USD", "1000")
    return (
        f"this account pays ~${monthly}/mo in operating costs and has "
        f"~{runway} months of runway with zero withdrawals (mode={mode}). "
        f"After that the operator needs ~${target}/mo extracted. "
        f"Holding cash while paying the monthly bill is a slow guaranteed loss."
    )


def _parse_llm_decision(answer: str, allowed: tuple[str, ...]) -> tuple[str, str, bool]:
    """
    Strict first-token parse of a "<decision>: <reason>" style LLM reply.
    Replaces substring matching (e.g. "sell_short"/"short"/"buy" in text),
    which let negations and unrelated mentions of the word through as a
    real signal -- "I would not buy this" / "don't buy: x" / "maybe buy
    later" all used to parse as "buy" (see AGENTS.md wave-1 entry). The
    decision must be the FIRST whole word before any ":" (or the first
    word of the whole reply if there's no colon) and must exactly match
    one of `allowed` -- a substring or partial word (e.g. "holding",
    "shorted") does not count. Anything that doesn't match -> ("hold",
    ..., malformed=True). Never silently defaults toward "buy"/
    "sell_short": the unparseable/negated/garbage case always resolves
    to the conservative side.
    """
    text = (answer or "").strip()
    head = text.partition(":")[0] if ":" in text else text
    head_norm = head.strip().lower()
    match = re.search(r"[a-z0-9_]+", head_norm)
    token = match.group(0) if match else ""
    if token and token in allowed:
        reason = text[match.end():].strip(" \t\n.,:;!?-")
        return token, reason[:200], False
    return "hold", ("malformed:" + text[:120])[:200], True


class DeepSeekShortsStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"
    can_short = True

    minimal_roi = {"0": 0.10}
    stoploss = -0.05

    # Rotation: see DeepSeekStrategy.py (main bot) for the full rationale --
    # same mechanism here, direction-aware (long vs short).
    # P16b: raised 2 -> 4 and paired with STALE_SIGNAL_STREAK_REQUIRED below --
    # a single noisy candle no longer force-exits by itself.
    MIN_HOLD_HOURS = 4
    STALE_SIGNAL_STREAK_REQUIRED = 2

    process_only_new_candles = True
    startup_candle_count: int = 24

    CORE_VOLUME_RANK = 5
    CORE_MAX_VOLATILITY_PCT = 5.0
    SATELLITE_STAKE_MULTIPLIER = 0.5
    MIN_RELIABLE_SAMPLE = 10  # fewer closed trades than this is noise, not a track record

    # P8 item 5: consecutive losing closed trades that define a "loss
    # streak" for the offline cooldown tuner (backtest/auto_tune.py).
    LOSS_STREAK_N = 3
    LOSS_STREAK_COOLDOWN_DEFAULT_HOURS = 0.0  # 0 = disabled until tuned

    # P8 item 6: correlation threshold/lookback for the concentration-risk warning.
    CONCENTRATION_CORRELATION_THRESHOLD = 0.8
    CONCENTRATION_LOOKBACK_DAYS = 30

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._core_cache: dict[str, bool] = {}
        self._latest_decision: dict[str, tuple[str, str]] = {}
        # P16b: see DeepSeekStrategy.py (main bot) for the full rationale.
        self._stale_streak: dict[int, dict] = {}

    def leverage(self, pair: str, current_time, current_rate: float,
                 proposed_leverage: float, max_leverage: float,
                 entry_tag: str | None, side: str, **kwargs) -> float:
        # Always 1.0 -- see module docstring. Do not change this without
        # first having a real, positive live/dry-run track record.
        return 1.0

    def _classify_pair(self, pair: str, dataframe: pd.DataFrame) -> bool:
        try:
            whitelist = self.dp.current_whitelist()
            rank = whitelist.index(pair)
        except (ValueError, AttributeError):
            rank = self.CORE_VOLUME_RANK
        top_by_volume = rank < self.CORE_VOLUME_RANK
        volatility = dataframe["volatility_24"].iloc[-1] if len(dataframe) else None
        low_volatility = volatility is not None and volatility < self.CORE_MAX_VOLATILITY_PCT
        is_core = top_by_volume and low_volatility
        self._core_cache[pair] = is_core
        return is_core

    def _recent_performance_summary(self, is_core: bool) -> str:
        try:
            closed = Trade.get_trades_proxy(is_open=False)
        except Exception as e:
            log.warning("Could not read trade history: %s", e)
            return "trade history unavailable"
        if not closed:
            return "no closed trades yet"
        same_tier = [t for t in closed if self._core_cache.get(t.pair) == is_core]
        sample = same_tier if len(same_tier) >= 3 else closed
        recent = sorted(sample, key=lambda t: t.close_date or t.open_date, reverse=True)[:10]
        total = len(recent)
        if total == 0:
            return "no closed trades yet"
        wins = sum(1 for t in recent if (t.close_profit or 0) > 0)
        avg_pct = sum((t.close_profit or 0) for t in recent) / total * 100
        scope = "same tier" if same_tier else "all pairs, tier history too short yet"
        if total < self.MIN_RELIABLE_SAMPLE:
            return (
                f"only {total} closed trades ({scope}) -- too few to be a reliable "
                f"record, do not treat as a real track record: {wins}/{total} profitable"
            )
        return f"last {total} closed trades ({scope}): {wins}/{total} profitable, avg {avg_pct:.2f}% per trade"

    def _tuned_satellite_multiplier(self) -> float:
        """Offline-tuned satellite size, written by backtest/auto_tune.py.

        Reads the per-bot JSON state (baked copy, else the persistent db
        dir). Missing/invalid file -> class default 0.5, so first deploy
        behaves exactly as before. Clamped to [0.3, 0.7] in any case.
        Only stake size -- never entry/exit logic.
        """
        default = float(self.SATELLITE_STAKE_MULTIPLIER)
        path = os.environ.get("AUTO_TUNE_JSON")
        candidates = [path] if path else []
        candidates += [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_tune.json"),
            "/freqtrade/user_data/db/auto_tune.json",
        ]
        for cand in candidates:
            if not cand:
                continue
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                val = float(data.get("satellite_stake_multiplier", default))
                return min(0.7, max(0.3, val))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return default

    def _tuned_loss_streak_cooldown_hours(self) -> float:
        """P8 item 5 -- see Main's DeepSeekStrategy.py for the full
        reasoning. Missing/invalid file -> 0.0 (disabled)."""
        default = self.LOSS_STREAK_COOLDOWN_DEFAULT_HOURS
        path = os.environ.get("AUTO_TUNE_JSON")
        candidates = [path] if path else []
        candidates += [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "auto_tune.json"),
            "/freqtrade/user_data/db/auto_tune.json",
        ]
        for cand in candidates:
            if not cand:
                continue
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return max(0.0, float(data.get("loss_streak_cooldown_hours", default)))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return default

    def _in_loss_streak_cooldown(self) -> bool:
        """P8 item 5 -- see Main's DeepSeekStrategy.py for the full
        reasoning. A code-level entry BLOCK, not a prompt hint. Applies
        across both long and short trades -- a loss is a loss regardless
        of direction."""
        cooldown_hours = self._tuned_loss_streak_cooldown_hours()
        if cooldown_hours <= 0:
            return False
        try:
            closed = sorted(Trade.get_trades_proxy(is_open=False),
                             key=lambda t: t.close_date or t.open_date, reverse=True)
        except Exception as e:
            log.warning("loss_streak_cooldown: could not read trade history: %s -- not enforcing cooldown", e)
            return False
        if len(closed) < self.LOSS_STREAK_N:
            return False
        recent = closed[: self.LOSS_STREAK_N]
        if any((t.close_profit or 0) > 0 for t in recent):
            return False
        last_close = recent[0].close_date
        if last_close is None:
            return False
        if last_close.tzinfo is None:
            last_close = last_close.replace(tzinfo=timezone.utc)
        hours_since = (datetime.now(timezone.utc) - last_close).total_seconds() / 3600
        return hours_since < cooldown_hours

    def _mtf_confirmation(self, pair: str) -> str | None:
        """P8 item 4 -- see Main's DeepSeekStrategy.py for the full
        reasoning. category='linear' here (futures), vs 'spot' on the
        spot bots. Context for the LLM only, never a hard entry filter."""
        try:
            symbol = pair.split(":")[0].replace("/", "")
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": "240", "limit": 55},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if len(rows) < 51:
                return None
            closes = pd.Series([float(row[4]) for row in reversed(rows)])
            sma50 = closes.rolling(50).mean().iloc[-1]
            current = closes.iloc[-1]
            if pd.isna(sma50):
                return None
            trend = "above" if current > sma50 else "below"
            return f"4h trend: price {trend} its own 4h SMA50"
        except Exception as e:
            log.warning("mtf_confirmation: fetch failed for %s: %s -- excluded from this cycle", pair, e)
            return None

    def _concentration_risk_warning(self, pair: str) -> str | None:
        """P8 item 6 -- see Main's DeepSeekStrategy.py for the full
        reasoning. category='linear' here (futures). Warns only, never
        blocks the entry, and applies regardless of the open position's
        or the candidate entry's direction (long/short)."""
        try:
            open_trades = Trade.get_trades_proxy(is_open=True)
        except Exception as e:
            log.warning("concentration_risk: could not read open trades for %s: %s -- excluded", pair, e)
            return None
        if not open_trades:
            return None

        def _daily_closes(sym_pair: str) -> pd.Series | None:
            try:
                symbol = sym_pair.split(":")[0].replace("/", "")
                r = requests.get(
                    "https://api.bybit.com/v5/market/kline",
                    params={"category": "linear", "symbol": symbol, "interval": "D",
                            "limit": self.CONCENTRATION_LOOKBACK_DAYS + 5},
                    timeout=8,
                )
                r.raise_for_status()
                rows = r.json().get("result", {}).get("list", [])
                if len(rows) < self.CONCENTRATION_LOOKBACK_DAYS:
                    return None
                return pd.Series([float(row[4]) for row in reversed(rows)])
            except Exception as e:
                log.warning("concentration_risk: fetch failed for %s: %s -- excluded from this cycle", sym_pair, e)
                return None

        this_closes = _daily_closes(pair)
        if this_closes is None:
            return None

        for t in open_trades:
            if t.pair == pair:
                continue
            other_closes = _daily_closes(t.pair)
            if other_closes is None:
                continue
            n = min(len(this_closes), len(other_closes), self.CONCENTRATION_LOOKBACK_DAYS)
            if n < 10:
                continue
            corr = this_closes.iloc[-n:].reset_index(drop=True).corr(other_closes.iloc[-n:].reset_index(drop=True))
            if pd.notna(corr) and corr >= self.CONCENTRATION_CORRELATION_THRESHOLD:
                return f"concentration risk: {corr:.2f} correlation with already-open {t.pair} over {n}d"
        return None

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
        is_core = self._core_cache.get(pair, False)
        if is_core:
            return proposed_stake
        reduced = proposed_stake * self._tuned_satellite_multiplier()
        if min_stake and reduced < min_stake:
            return min_stake
        return reduced

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["pct_change_24"] = dataframe["close"].pct_change(24) * 100
        dataframe["volatility_24"] = (
            (dataframe["high"].rolling(24).max() - dataframe["low"].rolling(24).min())
            / dataframe["low"].rolling(24).min()
        ) * 100
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        is_backtest = self.dp is not None and self.dp.runmode.value in ("backtest", "hyperopt")
        if is_backtest:
            # Placeholder only -- see the spot strategy's docstring for why
            # backtest never calls the LLM. Symmetric long/short version of
            # that same placeholder rule, for sanity-checking mechanics only.
            dataframe.loc[
                (dataframe["pct_change_24"] > 2) & (dataframe["volatility_24"] < 5), "enter_long"
            ] = 1
            dataframe.loc[
                (dataframe["pct_change_24"] < -2) & (dataframe["volatility_24"] < 5), "enter_short"
            ] = 1
            return dataframe

        if len(dataframe) < 24:
            return dataframe

        pair = metadata.get("pair", "BTC/USDT:USDT")
        is_core = self._classify_pair(pair, dataframe)
        latest = dataframe.iloc[-1]
        decision, reason = self._ask_deepseek(latest, pair, is_core)
        self._latest_decision[pair] = (decision, reason)

        if decision == "buy":
            dataframe.iloc[-1, dataframe.columns.get_loc("enter_long")] = 1
            dataframe.iloc[-1, dataframe.columns.get_loc("enter_tag")] = "core" if is_core else "sat"
            log.info("DeepSeek said buy (long) for %s -- %s", pair, reason)
        elif decision == "sell_short":
            dataframe.iloc[-1, dataframe.columns.get_loc("enter_short")] = 1
            dataframe.iloc[-1, dataframe.columns.get_loc("enter_tag")] = "core" if is_core else "sat"
            log.info("DeepSeek said sell_short for %s -- %s", pair, reason)
        else:
            log.info("DeepSeek said hold for %s -- %s", pair, reason)

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        # Exits handled by minimal_roi/stoploss, same as the spot strategy.
        dataframe["exit_long"] = 0
        dataframe["exit_short"] = 0
        return dataframe

    def custom_exit(self, pair: str, trade: Trade, current_time, current_rate: float,
                     current_profit: float, **kwargs) -> str | None:
        """Rotation on top of minimal_roi/stoploss -- see the main bot's
        DeepSeekStrategy.custom_exit for the full rationale. Direction-aware:
        a long trade rotates out when the latest decision stops being "buy",
        a short trade rotates out when it stops being "sell_short".

        P16b: requires STALE_SIGNAL_STREAK_REQUIRED consecutive invalid
        candles (anti-chop) instead of firing on the first one -- see the
        main bot's own P16b comment for the full rationale."""
        hours_open = (current_time - trade.open_date_utc).total_seconds() / 3600
        if hours_open < self.MIN_HOLD_HOURS:
            return None

        try:
            dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if dataframe is None or dataframe.empty:
                return None
            latest_candle_date = dataframe.iloc[-1]["date"]

            state = self._stale_streak.setdefault(trade.id, {"count": 0, "last_candle_date": None})
            if state["last_candle_date"] == latest_candle_date:
                return None
            state["last_candle_date"] = latest_candle_date

            default = "sell_short" if trade.is_short else "buy"
            decision, reason = self._latest_decision.get(pair, (default, ""))
            still_valid = (decision == "sell_short") if trade.is_short else (decision == "buy")
            if not still_valid:
                state["count"] += 1
                if state["count"] >= self.STALE_SIGNAL_STREAK_REQUIRED:
                    direction = "short" if trade.is_short else "long"
                    log.info(
                        "Rotating out of %s (%s) -- DeepSeek signal changed for %d consecutive candles: %s",
                        pair, direction, state["count"], reason,
                    )
                    self._stale_streak.pop(trade.id, None)
                    return "rotate_stale_signal"
            else:
                state["count"] = 0
        except Exception as e:
            log.warning("Stale-signal streak check failed for %s: %s -- falling back to no rotation this cycle", pair, e)

        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                            rate: float, time_in_force: str, exit_reason: str,
                            current_time, **kwargs) -> bool:
        """P16b: clears the stale-signal streak counter on any close."""
        self._stale_streak.pop(trade.id, None)
        return True

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time, entry_tag, side: str, **kwargs) -> bool:
        """
        P4.1: last-moment entry veto. Originally built as a custom Freqtrade
        pairlist plugin (`user_data/pairlists/FundingRsiPairList.py`), which
        turned out to be a real, maintainer-confirmed dead end -- Freqtrade's
        config schema only accepts a fixed enum of built-in pairlist method
        names; custom pairlist plugins are an explicitly unsupported,
        non-public interface (freqtrade/freqtrade#12445, closed as a
        duplicate of #4477, xmatthias: "Custom Pairlists / plugins are
        currently not supported and it's an (intentional) limitation").
        That's why Shorts never actually started with the old config --
        CRITICAL config validation error on the unrecognized method name,
        not a runtime bug. `confirm_trade_entry` is a real, documented
        Freqtrade strategy hook that runs right before an order is placed,
        so the same check works here instead: excludes a NEW shorts entry
        when the pair's perpetual funding rate is deeply negative (majors
        BTC/ETH < -0.02%/8h, everything else < -0.05%/8h) -- a crowded-short
        setup that tends to precede a squeeze. RSI(14) daily < 25 is a
        fallback ONLY when funding itself couldn't be fetched -- when
        funding is available it is used exclusively, RSI is never computed
        alongside it. Same direct-Bybit-REST fetch pattern as
        get_market_context() (no key needed), fails OPEN (allows the trade)
        if both funding and RSI are unavailable. Never touches an
        already-open position, and never affects the short side's own
        rotation/exit logic above.

        P8 item 5 check runs first: a real code-level block during an
        offline-tuned post-loss-streak cooldown window (see
        _in_loss_streak_cooldown), independent of the funding/RSI check below.
        """
        blocked = _allocator_ctx.entry_block(pair)
        if blocked:
            log.info("confirm_trade_entry: rejecting %s -- announcement guard: %s", pair, blocked)
            return False
        if self._in_loss_streak_cooldown():
            log.info("confirm_trade_entry: rejecting %s -- in loss-streak cooldown window", pair)
            return False

        majors = {"BTC", "ETH"}
        major_funding_threshold = -0.0002
        alt_funding_threshold = -0.0005
        rsi_fallback_threshold = 25.0

        symbol = pair.split(":")[0].replace("/", "")
        base = pair.split("/")[0]

        funding_rate = None
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
        except Exception as e:
            log.warning("confirm_trade_entry: funding fetch failed for %s: %s", pair, e)

        if funding_rate is not None:
            threshold = major_funding_threshold if base in majors else alt_funding_threshold
            if funding_rate < threshold:
                log.info("confirm_trade_entry: rejecting %s -- funding %.4f%%/8h < %.4f%% threshold",
                          pair, funding_rate * 100, threshold * 100)
                return False
            return True

        # Funding unavailable -- fall back to daily RSI(14).
        try:
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": symbol, "interval": "D", "limit": 40},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if not rows:
                return True
            closes = pd.Series([float(row[4]) for row in reversed(rows)])
        except Exception as e:
            log.warning("confirm_trade_entry: RSI fallback fetch failed for %s: %s -- allowing entry", pair, e)
            return True

        if len(closes) < 15:
            return True
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(14).mean().iloc[-1]
        avg_loss = loss.rolling(14).mean().iloc[-1]
        if pd.isna(avg_gain) or pd.isna(avg_loss):
            return True
        rsi = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
        if rsi < rsi_fallback_threshold:
            log.info("confirm_trade_entry: rejecting %s -- funding unavailable, RSI(14) daily "
                      "%.1f < %s fallback", pair, rsi, rsi_fallback_threshold)
            return False
        return True

    def _get_funding_rate_line(self, pair: str) -> str | None:
        """P8 item 2: the perpetual funding rate was already being fetched
        and used SILENTLY inside confirm_trade_entry's own entry veto
        (P4.1) -- the LLM itself never saw the number, only the pass/fail
        outcome. Surfaces the real number in the prompt too. A separate
        fetch on purpose: confirm_trade_entry runs at order-confirm time,
        this runs at decision time -- different cycle, can't share one
        value. Excluded (None) on any failure, never a guessed default."""
        try:
            symbol = pair.split(":")[0].replace("/", "")
            r = requests.get(
                "https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": symbol},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json().get("result", {}).get("list", [])
            if not rows:
                return None
            raw = rows[0].get("fundingRate")
            if raw in (None, ""):
                return None
            rate = float(raw)
            return f"Current funding rate: {rate * 100:.4f}%/8h"
        except Exception as e:
            log.warning("funding_rate_prompt: fetch failed for %s: %s -- excluded from this cycle", pair, e)
            return None

    def _ask_llm(self, row: pd.Series, pair: str, is_core: bool) -> tuple[str, str]:
        tier = "core (currently top-ranked by volume + low volatility)" if is_core else "satellite (smaller-cap and/or higher volatility right now)"
        market = get_market_context()
        headlines = get_news_headlines()
        news_status = "no_news" if headlines == NEWS_NO_NEWS else "feed_down" if headlines == NEWS_FEED_DOWN else "ok"
        log.info("news_status=%s for %s", news_status, pair)
        performance = self._recent_performance_summary(is_core)
        survival = get_survival_context()
        funding_line = self._get_funding_rate_line(pair)
        fng = get_fear_greed_index()
        mtf = self._mtf_confirmation(pair)
        concentration = self._concentration_risk_warning(pair)

        system = SYSTEM_PROMPT
        user = (
            f"Pair: {pair}\n"
            f"Risk tier: {tier}\n"
            f"24-candle % change: {row.get('pct_change_24'):.2f}\n"
            f"24-candle volatility: {row.get('volatility_24'):.2f}\n"
            f"Current close: {row.get('close')}\n"
            f"{self._kronos_line()}"
            + (f"{funding_line}\n" if funding_line else "")
            + f"Broader market context: {market}\n"
            f"Recent headlines: {headlines}\n"
            f"{_allocator_ctx.context_block(pair, _BOT_ID)}"
            + (f"{fng}\n" if fng else "")
            + (f"{mtf}\n" if mtf else "")
            + (f"{concentration}\n" if concentration else "")
            + f"Recent performance: {performance}\n"
            f"Survival context: {survival}"
        )

        if not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
            log.error("No DEEPSEEK_API_KEY or OPENROUTER_API_KEY set -- every decision will default to hold.")

        answer = self._llm_answer(system, user)
        if not answer:
            return "hold", "no LLM response (see logs above for which call failed)"

        decision, reason, malformed = _parse_llm_decision(answer, ("buy", "sell_short", "hold"))
        if malformed:
            log.warning("Malformed/unparseable LLM response for %s, defaulting to hold: %r", pair, answer)

        # P5 Phase 1 (shadow mode) -- logs an ensemble vote, never touches `decision`.
        _second_opinion.run_shadow(pair, is_core, decision, system, user, allowed=("buy", "sell_short", "hold"))

        return decision, reason

    # ------------------------------------------------------------------ P23: decision source (Kronos / LLM) and LLM provider
    # See _allocator_ctx.py for the switches (<BOT>_DECISION_SOURCE, <BOT>_LLM_PROVIDER). Defaults = the behaviour before P23.
    KRONOS_DIRECTIONS = ("buy", "sell_short")            # the directions this bot can open
    _kronos_note = None

    def _ask_deepseek(self, row, pair, *args, **kwargs):
        cfg = _allocator_ctx.decision_config(_BOT_ID)
        proposal = None
        if cfg["source"] in ("kronos", "kronos_llm"):
            proposal = _allocator_ctx.kronos_proposal(pair, _BOT_ID)
            if proposal is None:
                return "hold", "kronos: no fresh signal for this pair (fail-safe: hold)"
            if proposal["action"] not in self.KRONOS_DIRECTIONS:
                return "hold", "kronos: " + proposal["reason"] + (" (no entry)" if proposal["action"] == "hold" else " (direction not traded by this bot)")
            if cfg["source"] == "kronos" or cfg["provider"] == "none":
                return proposal["action"], "kronos " + proposal["reason"]
        elif cfg["provider"] == "none":
            return "hold", "no decision source: LLM provider is none and the decision source is llm (this bot is intentionally idle)"
        self._kronos_note = proposal
        try:
            decision, reason = self._ask_llm(row, pair, *args, **kwargs)
        finally:
            self._kronos_note = None
        if proposal is not None and decision != proposal["action"]:
            if decision != "hold":
                log.warning("%s: the LLM answered %s but Kronos proposed %s -- only confirm or veto is allowed, treated as veto", pair, decision, proposal["action"])
            return "hold", "llm veto of kronos " + proposal["action"] + ": " + str(reason)[:120]
        return decision, reason

    def _kronos_line(self) -> str:
        p = self._kronos_note
        if not p:
            return ""
        return (f"Quantitative proposal (Kronos forecast model, {p['reason']}): {p['action'].upper()}. You may only CONFIRM it (answer {p['action']}) or VETO it (answer hold); "
                f"any other answer counts as a veto. Judge the news and context, not the forecast itself.\n")

    def _llm_answer(self, system: str, user: str) -> str | None:
        if _allocator_ctx.decision_config(_BOT_ID)["provider"] == "free":
            return self._call_openrouter_free_only(system, user)
        return self._call_deepseek(system, user) or self._call_openrouter_fallback(system, user)

    def _call_openrouter_free_only(self, system: str, user: str) -> str | None:
        """Provider `free`: OpenRouter free models only. Never DeepSeek, never a paid model (unlike the fallback chain, which ends on a paid one)."""
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            log.error("LLM provider is 'free' but OPENROUTER_API_KEY is not set -- every decision will default to hold")
            return None
        models = self._get_openrouter_free_models()[:3]
        if not models:
            log.error("LLM provider is 'free' but no OpenRouter free model is available right now -- hold")
            return None
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"models": models, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": 100, "temperature": 0.1},
                timeout=25,
            )
            r.raise_for_status()
            data = r.json()
            log.info("OpenRouter free answered via %s", data.get("model", "unknown"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            log.error("OpenRouter free chain failed: %s", e)
            return None

    def _call_deepseek(self, system: str, user: str) -> str | None:
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            return None
        try:
            r = requests.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": "deepseek-flash",
                    # V4 models think by default (returns reasoning_content before
                    # content), which was consuming max_tokens before reaching the
                    # actual answer -- see AGENTS.md. Explicitly disabling it per
                    # DeepSeek's own docs is the real fix, not just a bigger budget.
                    "thinking": {"type": "disabled"},
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": 300,
                    "temperature": 0.1,
                },
                timeout=20,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if not content:
                log.warning("DeepSeek returned 200 but empty content -- check finish_reason/max_tokens")
            return content
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else "no response body"
            log.warning("DeepSeek call failed: %s | body: %s", e, body)
            return None
        except Exception as e:
            log.warning("DeepSeek call failed: %s", e)
            return None

    def _get_openrouter_free_models(self) -> list[str]:
        now = time.time()
        cache = _openrouter_free_models_cache
        if cache["list"] is not None and (now - cache["fetched_at"]) < _OPENROUTER_FREE_LIST_TTL_SECONDS:
            return cache["list"]
        try:
            r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
            r.raise_for_status()
            entries = r.json().get("data", [])
            free = []
            for m in entries:
                model_id = m.get("id", "")
                pricing = m.get("pricing", {})
                try:
                    is_free = float(pricing.get("prompt", "1") or "1") == 0 and float(pricing.get("completion", "1") or "1") == 0
                except (TypeError, ValueError):
                    is_free = False
                if model_id.endswith(":free") and is_free:
                    free.append(model_id)
            cache["list"] = free
            cache["fetched_at"] = now
            log.info("Refreshed OpenRouter free model list: %d found", len(free))
            return free
        except Exception as e:
            log.warning("Could not fetch OpenRouter model list: %s", e)
            return cache["list"] or []

    def _call_openrouter_fallback(self, system: str, user: str) -> str | None:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return None
        candidates = self._get_openrouter_free_models()[:2]
        models_chain = candidates + ["openai/gpt-4o-mini"]
        try:
            r = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "models": models_chain,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": 100,
                    "temperature": 0.1,
                },
                timeout=25,
            )
            r.raise_for_status()
            data = r.json()
            log.info("OpenRouter fallback succeeded via %s", data.get("model", "unknown"))
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.HTTPError as e:
            body = e.response.text[:300] if e.response is not None else "no response body"
            log.error("OpenRouter fallback chain failed: %s | body: %s", e, body)
            return None
        except Exception as e:
            log.error("OpenRouter fallback chain failed: %s", e)
            return None


# P20: prefer the shared snapshot written by survival-agent's collector (no per-bot fetch while it is fresh);
# fall back to this bot's own fetch exactly as before when the snapshot is missing or stale.
_own_fear_greed_index = get_fear_greed_index


def get_fear_greed_index():
    return _allocator_ctx.fear_greed_text() or _own_fear_greed_index()


_own_market_context = get_market_context


def get_market_context():
    return _allocator_ctx.market_text() or _own_market_context()
