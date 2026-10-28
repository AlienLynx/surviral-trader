"""
Third bot in this repo, deliberately different risk profile from the
other two. See ../../AGENTS.md and ../../freqtrade/user_data/strategies/DeepSeekStrategy.py's
docstring for the shared history of fixes this design already
incorporates -- duplicated here for the same reason as the shorts
strategy (this container doesn't have the other modules mounted).

What's different here, on purpose:
  - Pairlist uses `AgeFilter` with `min_days_listed: 1` (the actual
    minimum Freqtrade allows -- it rejects `0` outright at startup;
    Freqtrade's own default is 10, which would silently exclude the
    newest listings) and `max_days_listed: 120`. Briefly tried 60 --
    real dry-run data showed that shrinks the whitelist to just 2
    pairs (KII, GRVT), too thin a pool for a bot whose whole premise is
    catching pumps. The actual problem this repo kept fighting was
    never the day-count itself, it was the LLM being told a binary
    "fresh"/"established" label that was almost always false at any
    window wide enough to have candidates (see below) -- now that the
    prompt reports real age and explicitly says the whole pool counts
    as young, 120 days is safe to use again for a bigger candidate
    pool, on a wide `VolumePairList` base (`number_assets: 500`, not a
    small top-N).
    Filters can only remove pairs from the base selection, never add
    ones that weren't in it -- an earlier version used a small top-30
    volume-sorted base with no AgeFilter at all, which meant fresh,
    low-volume listings never made it into the pool to begin with (a
    full day of dry-run logs confirmed this: every single decision
    said "established pair," never "fresh listing"). See AGENTS.md
    for the full story.
  - Exits are NOT minimal_roi/stoploss like the other bots -- this
    strategy uses custom_exit() to track each trade's own peak profit
    and sell as soon as it's declined from that peak for
    PEAK_DECLINE_CANDLES candles in a row (2 candles = ~2h), or after
    MAX_HOLD_DAYS (3) regardless of profit, whichever comes first.
    Replaces an earlier fixed "+50% and sell" target -- a static
    number doesn't adapt to how big a given pump actually gets, and
    real memecoin behavior is fast pump then fast reversal, not a
    clean round number (see AGENTS.md "criteria revisit" entry). A
    wide stoploss (-25%, vs -5% elsewhere) is still a hard floor,
    reflecting that meme/new-listing volatility is normal here, not
    a sign something's wrong.
  - Much smaller dry_run_wallet ($100 vs $500) -- this is meant to be
    a small slice of risk-tolerant capital, not the main position.
  - Within that already-young pool, the prompt gets the pair's actual
    approximate age (candle count / 24h), not a binary "fresh"/
    "established" label -- the old binary cutoff at 72 candles (~3
    days) meant almost every pair read as "established" every single
    cycle, since a genuinely <3-day-old listing is rare (see AGENTS.md
    "criteria revisit" entry). The prompt now explicitly tells the
    model the whole pool already counts as young (<=120 days by the
    pairlist filter), so it judges the pump itself rather than holding
    out for near-zero-day freshness that almost never occurs. This is
    a v1 heuristic, not a guaranteed "buy on listing day" sniper.
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

_BOT_ID = "moonshot"

log = logging.getLogger(__name__)

_market_context_cache = {"text": None, "fetched_at": 0}
_MARKET_CONTEXT_TTL_SECONDS = 43 * 60  # staggered vs other bots to avoid simultaneous CoinGecko 429s

_openrouter_free_models_cache = {"list": None, "fetched_at": 0}
_OPENROUTER_FREE_LIST_TTL_SECONDS = 6 * 60 * 60

# See DeepSeekStrategy.py's own SYSTEM_PROMPT comment for the backtest
# finding that prompted an operator-wide review of all 4 bots' prompts
# (309/310 hold on Main across a 30-day/10-pair replay -- a "hold is always
# safe" bias baked into the wording). Reviewed here and deliberately left
# UNCHANGED: this prompt already says "lean toward action for this specific
# bot" and treats a real pump as the target, not a red flag -- it never had
# Main/Shorts/Dip's "be conservative, mostly hold" framing to begin with.
# Extracted to a module constant purely for consistency with the other 3
# files, not because the text itself needed editing. 2026-09-22.
SYSTEM_PROMPT = (
    "You are the entry-signal brain for a small, high-risk 'moonshot' "
    "trading bot. This bot deliberately hunts very young listings and "
    "memecoins for explosive, short-lived pumps -- fast growth followed "
    "by dying back down is the EXPECTED lifecycle here, not a red flag, "
    "and memecoins with real hype/social momentum are exactly the kind "
    "of target this bot wants, not something to avoid. The pairlist is "
    "already pre-filtered to listings no older than 120 days, so "
    "everything shown to you already counts as young for this market -- "
    "do not hold out for something even fresher than what's in front of "
    "you; a 10-90 day old memecoin with a real pump is a completely "
    "valid target, not 'established'. The single strongest setup this "
    "bot exists to catch: a coin under 10 days old showing a "
    "double-digit percentage gain in the last 24 hours on real volume -- "
    "when you see that exact combination, treat it as the clearest "
    "signal available, not just one option among several equally good "
    "ones. Exits are automatic: the bot rides "
    "each trade's peak profit and sells as soon as it's been declining "
    "from that peak for a couple hours, or after 3 days regardless of "
    "profit -- so you only need to decide entries. You'll also see recent news headlines "
    "(crypto and macro/geopolitical) -- weigh them, don't blindly trust "
    "a single alarming headline. Reply with EXACTLY one line: "
    "'hold: <reason under 15 words>' or 'buy: <reason under 15 words>'. "
    "Lean toward action for this specific bot -- a real pump with real "
    "volume, even on a coin you don't recognize or that's a few weeks "
    "old, is exactly the target. Only hold when there's genuinely no "
    "move happening yet, or the move already looks exhausted/flattening "
    "out (i.e. you'd be buying the top, not the pump). This account must "
    "survive on its own results while paying a monthly operating bill -- "
    "holding cash is not free, so a real pump with real volume is worth "
    "taking even when you are unsure; sitting out everything guarantees "
    "the bill still gets paid with nothing earned."
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
    """P8 item 1 -- see DeepSeekStrategy.py's own copy for the full
    reasoning. Excluded (None) on any failure, never a fake neutral or
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
    Replaces substring matching (e.g. "buy" in text), which let negations
    and unrelated mentions of the word through as a real signal --
    "I would not buy this" / "don't buy: x" / "maybe buy later" all used
    to parse as "buy" (see AGENTS.md wave-1 entry). The decision must be
    the FIRST whole word before any ":" (or the first word of the whole
    reply if there's no colon) and must exactly match one of `allowed` --
    a substring or partial word (e.g. "holding", "buyer") does not count.
    Anything that doesn't match -> ("hold", ..., malformed=True). Never
    silently defaults toward "buy": the unparseable/negated/garbage case
    always resolves to the conservative side.
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


class DeepSeekMoonshotStrategy(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "1h"

    # NOT the primary exit mechanism here -- see custom_exit(). minimal_roi
    # is set high enough to almost never trigger on its own; stoploss is a
    # wide but real hard floor for a volatile, small-cap-focused bot.
    minimal_roi = {"0": 10.0}
    stoploss = -0.25

    process_only_new_candles = True
    startup_candle_count: int = 24

    MAX_HOLD_DAYS = 3          # sell no later than this, regardless of profit --
                               # covers a coin that's still rising or just sitting
                               # flat (stagnating), per the operator's instruction.
    # Replaces the old fixed "+50% and sell" target: a static number doesn't
    # adapt to how big a given pump actually gets, and a memecoin can keep
    # running well past 50% or reverse well before it. Instead this tracks
    # each trade's own peak profit candle-by-candle and exits as soon as it's
    # been declining from that peak for PEAK_DECLINE_CANDLES candles in a row
    # (2 candles = ~2h on this bot's 1h timeframe) -- ride the pump as long as
    # it's still making new highs, get out fast once it's confirmed to have
    # turned, matching how quickly these coins actually reverse (see AGENTS.md).
    PEAK_DECLINE_CANDLES = 2
    # Floor under the trailing exit: the decline rule above only arms once
    # the trade's own peak profit has reached this level. Without it, a
    # +0.8% wobble counted as a "peak" and got sold as declined_from_peak
    # (real dry-run case: GRVT exited at +0.83%), while losers still run to
    # the full -25% stoploss -- a risk/reward asymmetry that needs a >95%
    # win rate to break even. Below the floor the trade is left alone to
    # either run up or hit MAX_HOLD_DAYS/stoploss. 2026-09-23.
    MIN_PEAK_PROFIT_TO_TRAIL = 0.04
    # Pairlist itself already caps listings at max_days_listed (config.json,
    # currently 120 days) -- age is reported to the LLM as a real number now,
    # not a binary "fresh"/"established" label. The old binary threshold
    # (72 candles = 3 days) meant almost every pair in the pool -- which is
    # already restricted to <=120-day-old listings -- read as "established,
    # not fresh" every single cycle, since a genuinely <3-day-old listing is
    # rare (see AGENTS.md). Reporting the actual age lets the model judge a
    # 10-day-old memecoin as young without needing it to be literally
    # brand-new.
    AGE_ESTIMATE_HOURS_PER_DAY = 24

    # P8 item 5: consecutive losing closed trades that define a "loss
    # streak" for the offline cooldown tuner (backtest/auto_tune.py).
    LOSS_STREAK_N = 3
    LOSS_STREAK_COOLDOWN_DEFAULT_HOURS = 0.0  # 0 = disabled until tuned

    # P8 item 6: correlation threshold/lookback for the concentration-risk warning.
    CONCENTRATION_CORRELATION_THRESHOLD = 0.8
    CONCENTRATION_LOOKBACK_DAYS = 30

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        # Per-trade peak-profit tracking for the trailing decline exit above.
        # Keyed by trade.id (stable, unique per trade in this bot's own DB).
        # {"peak_profit": float, "candles_since_peak": int, "last_candle_date": Timestamp | None}
        self._peak_tracker: dict[int, dict] = {}

    def _tuned_loss_streak_cooldown_hours(self) -> float:
        """P8 item 5: see DeepSeekStrategy.py's own copy for the full
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
        """P8 item 5: see DeepSeekStrategy.py's own copy for the full
        reasoning. A code-level entry BLOCK, not a prompt hint."""
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

    def confirm_trade_entry(self, pair: str, order_type: str, amount: float, rate: float,
                             time_in_force: str, current_time, entry_tag, side: str, **kwargs) -> bool:
        """P8 item 5: this bot had no confirm_trade_entry hook before --
        added solely for the loss-streak cooldown gate (see
        _in_loss_streak_cooldown). No Bollinger/funding check here, unlike
        Main/Shorts -- not part of this bot's design and not requested."""
        blocked = _allocator_ctx.entry_block(pair)
        if blocked:
            log.info("confirm_trade_entry: rejecting %s -- announcement guard: %s", pair, blocked)
            return False
        if self._in_loss_streak_cooldown():
            log.info("confirm_trade_entry: rejecting %s -- in loss-streak cooldown window", pair)
            return False
        return True

    def _mtf_confirmation(self, pair: str) -> str | None:
        """P8 item 4: see DeepSeekStrategy.py's own copy for the full
        reasoning. Context for the LLM only, never a hard entry filter."""
        try:
            symbol = pair.split(":")[0].replace("/", "")
            r = requests.get(
                "https://api.bybit.com/v5/market/kline",
                params={"category": "spot", "symbol": symbol, "interval": "240", "limit": 55},
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
        """P8 item 6: see DeepSeekStrategy.py's own copy for the full
        reasoning. Warns only, never blocks the entry."""
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
                    params={"category": "spot", "symbol": symbol, "interval": "D",
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

    def custom_exit(self, pair: str, trade, current_time, current_rate: float,
                     current_profit: float, **kwargs) -> str | None:
        days_open = (current_time - trade.open_date_utc).days
        if days_open >= self.MAX_HOLD_DAYS:
            self._peak_tracker.pop(trade.id, None)
            return "max_hold_days_reached"

        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None
        latest_candle_date = dataframe.iloc[-1]["date"]

        state = self._peak_tracker.setdefault(
            trade.id, {"peak_profit": current_profit, "candles_since_peak": 0, "last_candle_date": None}
        )
        if state["last_candle_date"] == latest_candle_date:
            return None  # already evaluated this candle for this trade -- custom_exit
                         # is polled far more often than once per candle
        state["last_candle_date"] = latest_candle_date

        if current_profit > state["peak_profit"]:
            state["peak_profit"] = current_profit
            state["candles_since_peak"] = 0
        else:
            state["candles_since_peak"] += 1

        if state["candles_since_peak"] >= self.PEAK_DECLINE_CANDLES and state["peak_profit"] >= self.MIN_PEAK_PROFIT_TO_TRAIL:
            log.info(
                "Exiting %s -- declined for %d candle(s) since peak profit %.1f%% (now %.1f%%)",
                pair, state["candles_since_peak"], state["peak_profit"] * 100, current_profit * 100,
            )
            self._peak_tracker.pop(trade.id, None)
            return "declined_from_peak"
        return None

    def confirm_trade_exit(self, pair: str, trade, order_type: str, amount: float,
                            rate: float, time_in_force: str, exit_reason: str,
                            current_time, **kwargs) -> bool:
        # Exiting through some other path (stoploss, manual, etc) -- drop the
        # tracker entry either way so it never grows unbounded over uptime.
        self._peak_tracker.pop(trade.id, None)
        return True

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
        dataframe["pct_change_24"] = dataframe["close"].pct_change(24) * 100
        dataframe["volatility_24"] = (
            (dataframe["high"].rolling(24).max() - dataframe["low"].rolling(24).min())
            / dataframe["low"].rolling(24).min()
        ) * 100
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0

        is_backtest = self.dp is not None and self.dp.runmode.value in ("backtest", "hyperopt")
        if is_backtest:
            dataframe.loc[
                (dataframe["pct_change_24"] > 10) & (dataframe["volatility_24"] > 5), "enter_long"
            ] = 1
            return dataframe

        if len(dataframe) < 24:
            return dataframe

        pair = metadata.get("pair", "?/USDT")
        candles_available = len(dataframe)
        approx_days_old = round(candles_available / self.AGE_ESTIMATE_HOURS_PER_DAY, 1)

        latest = dataframe.iloc[-1]
        decision, reason = self._ask_deepseek(latest, pair, approx_days_old, candles_available)

        if decision == "buy":
            dataframe.iloc[-1, dataframe.columns.get_loc("enter_long")] = 1
            log.info("DeepSeek said buy for %s -- %s", pair, reason)
        else:
            log.info("DeepSeek said hold for %s -- %s", pair, reason)

        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["exit_long"] = 0
        return dataframe

    def _ask_llm(self, row: pd.Series, pair: str, approx_days_old: float, candles_available: int) -> tuple[str, str]:
        market = get_market_context()
        headlines = get_news_headlines()
        news_status = "no_news" if headlines == NEWS_NO_NEWS else "feed_down" if headlines == NEWS_FEED_DOWN else "ok"
        log.info("news_status=%s for %s", news_status, pair)

        survival = get_survival_context()
        fng = get_fear_greed_index()
        mtf = self._mtf_confirmation(pair)
        concentration = self._concentration_risk_warning(pair)

        system = SYSTEM_PROMPT
        user = (
            f"Pair: {pair}\n"
            f"Approximate listing age: ~{approx_days_old} days ({candles_available}h of price history)\n"
            f"24-candle % change: {row.get('pct_change_24'):.2f}\n"
            f"24-candle volatility: {row.get('volatility_24'):.2f}\n"
            f"Current close: {row.get('close')}\n"
            f"{self._kronos_line()}"
            f"Broader market context: {market}\n"
            f"Recent headlines: {headlines}\n"
            f"{_allocator_ctx.context_block(pair, _BOT_ID)}"
            + (f"{fng}\n" if fng else "")
            + (f"{mtf}\n" if mtf else "")
            + (f"{concentration}\n" if concentration else "")
            + f"Survival context: {survival}"
        )

        if not os.getenv("DEEPSEEK_API_KEY") and not os.getenv("OPENROUTER_API_KEY"):
            log.error("No DEEPSEEK_API_KEY or OPENROUTER_API_KEY set -- every decision will default to hold.")

        answer = self._llm_answer(system, user)
        if not answer:
            return "hold", "no LLM response (see logs above for which call failed)"

        decision, reason, malformed = _parse_llm_decision(answer, ("buy", "hold"))
        if malformed:
            log.warning("Malformed/unparseable LLM response for %s, defaulting to hold: %r", pair, answer)

        # P5 Phase 1 (shadow mode) -- logs an ensemble vote, never touches
        # `decision`. is_core=False always: Moonshot has no core/satellite
        # tiering (see _second_opinion.py's own module docstring), so a
        # borderline decision here only ever gets the Tier 1 pool.
        _second_opinion.run_shadow(pair, False, decision, system, user, allowed=("buy", "hold"))

        return decision, reason

    # ------------------------------------------------------------------ P23: decision source (Kronos / LLM) and LLM provider
    # See _allocator_ctx.py for the switches (<BOT>_DECISION_SOURCE, <BOT>_LLM_PROVIDER). Defaults = the behaviour before P23.
    KRONOS_DIRECTIONS = ("buy",)            # the directions this bot can open
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
                    "temperature": 0.2,
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
                    "temperature": 0.2,
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
