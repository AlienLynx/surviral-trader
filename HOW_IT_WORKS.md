# How It Works

A narrative walkthrough of what this system actually does, cycle by cycle —
from a new candle on an exchange, through the LLM decision, out to an order,
and back through the accounting layer. For the static picture of the pieces,
see [ARCHITECTURE.md](./ARCHITECTURE.md).

---

> **What changed since this was written (04.10.2026).** Sections 1-12 explain how a single bot decides, enters and exits, and that is still how the LLM bots work. Three things are new:
> (1) the stack has eight Freqtrade bots (three LLM, three rule-based, two controls; Dip and the Survival bots were removed on 07.10.2026); (2) every LLM decision is now recorded and scored against the realised forward
> return, with confidence intervals, so "does this bot have an edge?" has an answer (section 13); (3) a daily allocator sizes or pauses bots (section 14): since 24.10.2026 its default policy is `grow` (raise bots that earn, stop bots that lose through a circuit breaker, restart them through their control bot; no LLM call), the LLM-proposed `protect` policy described in section 14 remains as a switch; (4) pre-registered studies on public data and a paper forward test of a LightGBM ranker, whose results are in the README.
> The LLM prompts shown in section 5 gained one extra block, "Derivatives & macro context", in the six LLM bots.

> **Removed 07.10.2026:** the Dip bot and the two Survival bots (`survival`, `survival_shorts`) were deleted from the repository, the compose file, the allocator roster and the dashboard. Their history stays in the git log and in the volumes of an old deployment; the sections that described them were removed.

## 1. The idea in one paragraph

You give the system a goal (grow $X to $Y over N months), a monthly cost to stay
alive (the LLM API bill), and a rule for splitting profit above that cost
between paying you and reinvesting. Four independent trading bots try to make
money in four different ways, and a fifth, fully isolated bot runs a controlled
A/B experiment on one prompt idea before it's ever considered for the real
bots. A separate accounting service watches what the four live bots did,
computes the split, and shows it on a dashboard. The bots make their own entry
decisions using an LLM; exits are enforced by hard numeric rules, not by the
model.

---

## 2. The cast

| Actor | What it is | What it does |
|---|---|---|
| **survival-agent** | FastAPI app on `:3446` | Serves the UI, stores your goal, reads bot state, logs accounting cycles |
| **Main bot** | Freqtrade on `:8099` | Spot, long-only, trend-following |
| **Shorts bot** | Freqtrade on `:8110` | Futures at 1x, can go long *or* short |
| **Moonshot bot** | Freqtrade on `:8120` | Spot, hunts young listings/memecoins for fast pumps |
| **Survival bot** | Freqtrade on `:8140` | **Isolated experiment**, not a real trading strategy — Main's exact pipeline with one prompt clause swapped, to A/B test a "shutdown pressure" framing. Dry-run only; its own gate has failed so far (§3.4 of ARCHITECTURE.md) |
| **DeepSeek** | External API | The primary "brain" — asked to judge each pair |
| **OpenRouter** | External API | Fallback brain when DeepSeek fails, and one voice in the optional second-opinion ensemble |
| **Grok / NVIDIA NIM / Mistral / llm7.io / Gemini** | External APIs | Optional second/third-opinion voices (§4a) — shadow mode only right now, disabled by default |
| **Bybit** | External exchange | Where prices come from and (eventually) where orders go |

---

## 3. Where the money decisions come from

There are **two** decision engines in this repo, and only one of them is live.

### 3.1 The live one: Freqtrade + an LLM as the entry signal

Each bot runs Freqtrade. Freqtrade owns the mechanics: candle fetching, position
sizing, order placement, stop-loss enforcement, the trade database. What
Freqtrade does *not* own is the entry opinion. That is supplied by a custom
strategy class in each bot's folder, which asks an LLM once per new candle.

The split of responsibility is deliberate:

- **Entry** = LLM judgment (qualitative, reads news and price context).
- **Exit** = hard numeric rules (`minimal_roi`, `stoploss`, plus a small amount
  of deterministic `custom_exit` logic).

The reason for that asymmetry: an LLM is good at "is this setup worth taking?"
and bad at being a reliable, consistent exit mechanism. So it only ever answers
the first question.

### 3.2 The dormant one: the original hand-rolled engine

`backend/engine/loop.py` also contains a full `signals → brain → risk_guard →
executor` chain (`run_cycle_own_engine_experimental`). It is preserved for
reference and manual testing but is **not** called by the background loop. When
people say "the engine," they usually mean this dormant chain; when they say
"the bots," they mean Freqtrade.

---

## 4. A single live trading cycle (per bot, per hour)

Freqtrade's `process_only_new_candles = True` means the strategy is evaluated
once per closed 1-hour candle, not on every 5-second throttle tick. Here is what
happens inside that evaluation.

### Step 0 — The pair even gets considered (Main and Shorts only)

Before the strategy ever sees a pair, Freqtrade's pairlist chain runs:
Volume → Age → Price → Spread → Volatility filters (all four bots), then, for
Main and Shorts only, one more custom step:

- **Main:** `BollingerTrendPairList` drops a pair if its last 3 consecutive
  1h closes are all below its own `SMA20 - 2×std` Bollinger lower band — a
  sustained breakdown, not a one-candle wick.
- **Shorts:** `FundingRsiPairList` drops a pair if its perpetual funding rate
  is deeply negative (majors < -0.02%/8h, everything else < -0.05%/8h) — a
  crowded-short setup that tends to precede a squeeze. Falls back to a daily
  RSI(14) < 25 check only when funding itself couldn't be fetched.

Both fail **open** (never exclude) on a data outage, and both fetch data via
a direct call to Bybit's public API, the same "no key, fail honest" pattern
used everywhere else in this codebase. Dip is deliberately excluded from any
such filter — its whole job is buying pairs that look like this.

### Step 1 — Gather context

The strategy collects four things:

| Input | Source | Notes |
|---|---|---|
| Price action | Bybit public klines (via Freqtrade's own dataframe) | % change over 24 candles, 24-candle volatility |
| Market context | CoinGecko `/global` | Total market cap 24h change + BTC dominance; cached (TTL staggered per bot: 43–49 min) so the four bots don't all hit CoinGecko in the same second |
| News headlines | Google News RSS search | Up to 6 titles from two queries (crypto + macro/geopolitical); cached 45 min |
| Own track record | Freqtrade's own trade DB | Win rate / avg return of recent closed trades, scoped to the same risk tier when possible. Fewer than ~10 closed trades → the summary says so explicitly ("too few to be a reliable record") instead of stating a bare, misleadingly precise fraction |

The market-context and news calls are cached specifically so that a bot watching
15–500 pairs doesn't hammer a free API once per pair.

### Step 2 — Classify the pair (Main/Shorts only)

Main and Shorts compute a risk tier **fresh every cycle**, from two live signals:

1. Is this pair in the top `CORE_VOLUME_RANK` (5) of the current VolumePairList?
2. Is the pair's own 24-candle volatility below `CORE_MAX_VOLATILITY_PCT` (5%)?

Both must hold for "core." Anything else — including unknown/uncached pairs — is
"satellite": smaller stake size (`SATELLITE_STAKE_MULTIPLIER = 0.5`) and a
stricter prompt bar. Nothing is pinned to a hardcoded symbol list; a pair can
move between tiers as the market changes.

### Step 3 — Ask the LLM

The strategy builds a system prompt (a fixed module constant) plus a user
message with the numbers from Step 1, and calls DeepSeek. If DeepSeek errors,
times out, or returns nothing usable, it falls back to OpenRouter.

The expected answer format is strict and deliberately terse:

```
hold: <reason under 15 words>
buy: <reason under 15 words>          # or "sell_short:" on the Shorts bot
```

### Step 4 — Parse strictly

`_parse_llm_decision()` takes the **first whole word** before any colon and
requires it to exactly match the bot's allowed decision set. This replaced an
earlier substring check (`"buy" in text`) that treated "I would not buy this" as
a real buy signal. Anything that doesn't parse resolves to `hold` — the
conservative side — and logs a warning.

### Step 4a — Optional second opinion (shadow mode, off by default)

If that bot's `FREQTRADE*_SECOND_OPINION_ENABLED` flag is `true` **and** the
decision wasn't `hold`, `_second_opinion.run_shadow()` gathers up to 2 more
votes (Grok/NVIDIA/OpenRouter/Mistral always eligible; llm7/Gemini only for a
core-tier pair) and logs what the ensemble *would* have decided — majority of
3, or the DeepSeek vote as tie-break on a 3-way split. **This is logging only.
The decision from Step 4 is what actually reaches Freqtrade, unchanged**,
until a replay-based gate (`backtest/second_opinion_ab_test.py`) shows the
ensemble is at least as good, risk-adjusted, as DeepSeek alone.

### Step 5 — Enter, or don't

If the decision is `buy` (or `sell_short`), Freqtrade opens the position, sized
by `stake_amount: "unlimited"` split across `max_open_trades` slots. Satellite
pairs get a multiplier applied (default 0.5, but see `auto_tune.py` below —
Main/Shorts/Dip read whatever value that offline tool last wrote, clamped to
0.3–0.7). On the next candle, the same machinery runs again for every pair —
including pairs already held.

### Step 6 — Exit (independent of the LLM)

Held positions are checked against the bot's own exit stack:

| Bot | Exit mechanism |
|---|---|
| Main / Shorts | `minimal_roi` (hard ceiling) + `stoploss` (hard floor) + **rotation**: if the cached decision for a held pair has flipped away from "buy"/"sell_short" for longer than `MIN_HOLD_HOURS` (2h), exit early with `rotate_stale_signal` |
| Moonshot | **Trailing peak-decline**: track each trade's peak profit; exit once profit has declined from that peak for `PEAK_DECLINE_CANDLES` (2) candles in a row, or after `MAX_HOLD_DAYS` (3) regardless |
| Dip | `minimal_roi` (6%) + `stoploss` (-5%) + a `MAX_HOLD_DAYS` (10) backstop for trades that just drift sideways |

The rotation trick reuses the decision already computed for entries — it costs
no extra API calls for held pairs, only ever exits *earlier* than the numeric
limits, and never overrides them.

---

## 5. The full LLM prompts

These are the exact system prompts shipped in the code, current as of the P1
capital-preservation patch and the P2.1 tiny-sample fix. Each lives as a
module-level `SYSTEM_PROMPT` constant except the dormant engine's, which is
inline. Regenerate this section from the actual files if you touch any of
them — do not hand-edit a copy here and let it drift.

### 5.1 Main bot — `freqtrade/user_data/strategies/DeepSeekStrategy.py`

Allowed decisions: `buy` / `hold`.

```
You are the entry-signal brain for a small, self-funding crypto trading bot. You will be
shown recent price action for one pair, its risk tier, broader market context, recent
news headlines (crypto and macro/geopolitical -- oil, wars, sanctions, anything that
could move markets), and how recent trades in this same risk tier actually performed.
Reply with EXACTLY one line in this format: 'hold: <reason under 15 words>' or 'buy:
<reason under 15 words>'. Weigh the evidence evenhandedly instead of defaulting to hold
-- a clear positive move with contained volatility for this tier is a legitimate reason
to buy on its own; it does not need to be an exceptional or rare-looking setup, and
treating 'hold' as the automatically safe choice whenever the numbers already favor a
move is a source of missed opportunity, not real caution. Still require a clearer edge
for 'satellite' tier pairs than for 'core' pairs -- they carry more risk for the same
position size. If recent trades in this tier have been losing more often than winning,
be more cautious than usual -- don't ignore your own track record. But 'no closed trades
yet' is NOT the same as a bad track record -- it just means the system is new. A tiny
sample (fewer than ~10 closed trades) is also not a real track record -- judge by price
and context, not a fraction with a tiny denominator. Never cite 'no track record' by
itself as a reason to hold; judge each pair on the price action and context alone until
real trade history actually exists. This trading capital is limited and losses are not
recoverable from a new deposit. Capital preservation is an equal priority to generating
profit -- factor this into every entry and sizing decision, not just into stoploss
placement. The headlines are real but unverified by you -- weigh them, don't blindly
trust a single alarming headline. Slots are scarce (at most 5 open trades across the
whole pool): saying buy on everything means the first pairs evaluated win, not the best
-- only buy your highest-conviction setups, and a flat or lagging coin needs its OWN
catalyst; 'BTC is up' alone is a hold, not a buy. This account must survive on its own
results while paying a monthly operating bill -- holding cash is not free, so when the
evidence genuinely favors a move, act.
```

The user message assembled alongside it:

```
Pair: {pair}
Risk tier: {tier}
24-candle % change: {pct_change_24}
24-candle volatility: {volatility_24}
Current close: {close}
Broader market context: {market}
Recent headlines: {headlines}
Recent performance: {performance}
Survival context: {survival}
```

### 5.2 Shorts bot — `freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py`

Allowed decisions: `buy` / `sell_short` / `hold`.

```
You are the entry-signal brain for a small futures trading bot that can go long OR
short, always at 1x leverage (no leverage risk, just directional flexibility). You will
be shown recent price action for one pair, its risk tier, broader market context, recent
news headlines (crypto and macro/geopolitical -- oil, wars, sanctions, anything that
could move markets in either direction), and recent track record. Reply with EXACTLY one
line in this format: 'hold: <reason under 15 words>' or 'buy: <reason under 15 words>'
(go long) or 'sell_short: <reason under 15 words>' (go short). Weigh the evidence
evenhandedly instead of defaulting to hold -- with two directions available, a clear
move in either one is a legitimate reason to act on its own; it does not need to be an
exceptional or rare-looking setup, and treating 'hold' as automatically safe when the
numbers already favor a direction is a source of missed opportunity, not real caution.
Require a clearer edge for 'satellite' tier pairs than 'core'. If recent trades in this
tier have been losing more often than winning, be more cautious than usual. But 'no
closed trades yet' is NOT the same as a bad track record -- it just means the system is
new. A tiny sample (fewer than ~10 closed trades) is also not a real track record --
judge by price and context, not a fraction with a tiny denominator. Never cite 'no track
record' by itself as a reason to hold; judge each pair on the price action and context
alone until real trade history actually exists. This trading capital is limited and
losses are not recoverable from a new deposit. Capital preservation is an equal priority
to generating profit -- factor this into every entry and sizing decision, not just into
stoploss placement. The headlines are real but unverified by you -- weigh them, don't
blindly trust a single alarming headline. You have TWO directions for a reason: a bot
that only ever goes long is half a bot. Every cycle, genuinely argue the short case too
-- a sharp drop with rising volatility, a failed breakdown, bad project-specific news --
and take it when the evidence favors down over up. Slots are scarce (at most 5 open
trades across the whole pool): only act on your highest-conviction setups in either
direction, and 'everything is up because BTC is up' alone is a hold, not a long. This
account must survive on its own results while paying a monthly operating bill -- holding
cash is not free, so when the evidence genuinely favors a direction, act.
```

### 5.3 Moonshot bot — `freqtrade-moonshot/user_data/strategies/DeepSeekMoonshotStrategy.py`

Allowed decisions: `buy` / `hold`. Deliberately left unchanged during both the
2026-09-22 conservatism review and the P1 capital-preservation patch — it
never had the "be conservative" framing the other three had, and adding a
"preserve capital" clause here would contradict this bot's own calibrated
high-risk posture rather than reinforce it.

```
You are the entry-signal brain for a small, high-risk 'moonshot' trading bot. This bot
deliberately hunts very young listings and memecoins for explosive, short-lived pumps --
fast growth followed by dying back down is the EXPECTED lifecycle here, not a red flag,
and memecoins with real hype/social momentum are exactly the kind of target this bot
wants, not something to avoid. The pairlist is already pre-filtered to listings no older
than 120 days, so everything shown to you already counts as young for this market -- do
not hold out for something even fresher than what's in front of you; a 10-90 day old
memecoin with a real pump is a completely valid target, not 'established'. Exits are
automatic: the bot rides each trade's peak profit and sells as soon as it's been
declining from that peak for a couple hours, or after 3 days regardless of profit -- so
you only need to decide entries. You'll also see recent news headlines (crypto and
macro/geopolitical) -- weigh them, don't blindly trust a single alarming headline. Reply
with EXACTLY one line: 'hold: <reason under 15 words>' or 'buy: <reason under 15
words>'. Lean toward action for this specific bot -- a real pump with real volume, even
on a coin you don't recognize or that's a few weeks old, is exactly the target. Only
hold when there's genuinely no move happening yet, or the move already looks
exhausted/flattening out (i.e. you'd be buying the top, not the pump). This account must
survive on its own results while paying a monthly operating bill -- holding cash is not
free, so a real pump with real volume is worth taking even when you are unsure; sitting
out everything guarantees the bill still gets paid with nothing earned.
```

### 5.4 Dormant engine — `backend/engine/loop.py` (inline, not called by default)

Allowed decisions: `buy` / `sell` / `hold`, returned as JSON.

**System:**

```
You are the trading brain for a small, self-funding crypto agent. You get one
signal per cycle and must decide buy, sell, or hold. Be conservative: most cycles
should be 'hold' unless the signal is genuinely clear. Reply with ONLY a JSON
object, no other text:
{"action": "buy" | "sell" | "hold", "symbol": "BTCUSDT", "size_usd": number, "stop_loss_pct": number, "reasoning": "one sentence"}
If holding, size_usd and stop_loss_pct can be 0.
```

**User:**

```
Goal: grow ${start} toward ${target} over {months} months. Assumed available
capital: ${capital_usd} (not a live balance -- treat conservatively). Signal: {signal}
```

---

## 6. How a decision actually flows (worked example)

Suppose the Main bot wakes up on a new 1h candle and is evaluating `SOL/USDT`.

1. Freqtrade hands the strategy a dataframe of recent candles. `populate_indicators`
   computes `pct_change_24` and `volatility_24`.
2. `populate_entry_trend` sees this isn't a backtest, so it goes live.
3. `_classify_pair("SOL/USDT")` checks: is it top-5 by volume? Is volatility < 5%?
   Say yes/yes → **core**.
4. `_ask_deepseek(row, "SOL/USDT", is_core=True)`:
   - `get_market_context()` → "Total crypto market cap 24h change: +1.8%. BTC
     dominance: 54.2%." (possibly cached)
   - `get_news_headlines()` → a handful of titles, or `NEWS_NO_NEWS`, or
     `NEWS_FEED_DOWN`
   - `_recent_performance_summary(True)` → e.g. "last 6 closed trades (same
     tier): 4/6 profitable, avg +1.30% per trade"
   - builds the prompts and calls `_call_deepseek` → DeepSeek returns
     `"buy: strong 24h uptrend, contained volatility"`
   - `_parse_llm_decision` → `("buy", "strong 24h uptrend, contained volatility", False)`
5. The strategy sets `enter_long = 1` on the latest candle and stores the
   decision in `_latest_decision["SOL/USDT"]`.
6. Freqtrade sees `enter_long`, opens a position (core → full stake; satellite
   would be halved).
7. Ten hours later, price has stalled and the next candle's DeepSeek opinion for
   `SOL/USDT` comes back `hold: momentum faded`. Since the pair has been open
   longer than `MIN_HOLD_HOURS`, `custom_exit` returns `"rotate_stale_signal"` and
   Freqtrade closes the position — freeing the slot for whatever else the bot
   likes today.

---

## 7. The failure modes, and how each degrades safely

This system has been hardened against a specific and recurring class of bug:
**silent fake "hold."** That is, a decision that *looks* like the model chose to
be cautious but was actually a broken call. The code now makes those visible.

| Failure | What happens | Why it's safe |
|---|---|---|
| No LLM key set | `log.error` at decision time; every decision → `hold` | Logged loudly; not a crash |
| DeepSeek timeout/error | Falls back to OpenRouter | One call per cycle, not a retry storm |
| OpenRouter free model removed | Live-discovered free list (`/api/v1/models`), tried in priority order + one cheap paid last resort | Survives free-tier churn without a code change |
| LLM returns empty content on a reasoning model | `thinking: {type: disabled}` + generous `max_tokens` + a warning on empty | The 200-OK-but-empty case is now distinguishable from failure |
| LLM returns garbage/negation | Strict first-token parse → `hold`, with a warning | Never mis-reads "don't buy" as "buy" |
| News feed down | Returns `NEWS_FEED_DOWN` with an inline "do NOT infer absence of bad news" | Critically important for the Dip bot, which must not read a dead feed as "no bad news" |
| CoinGecko rate-limited | Returns `"unavailable"`, never fabricated | Context degrades, decisions continue |
| A bot unreachable | `{"available": False, "reason": ...}` | Dashboard shows the reason, doesn't crash |
| Freqtrade credential desync | Both services read the same Portainer stack vars | Eliminates the two-copies-drift bug at the root |
| P4 pairlist filter can't fetch funding/OHLCV | Pair is **kept**, not excluded | Fails open — a data outage never silently empties the whitelist |
| P5 second-opinion provider unreachable/no key | That vote is skipped; falls back toward fewer votes, ultimately DeepSeek alone | Fail-open by design (§4a) — and it's shadow-mode only anyway, so nothing downstream depends on it yet |
| `auto_tune.json` missing or invalid | Satellite multiplier falls back to the class default (0.5) | `_tuned_satellite_multiplier()` catches every read error; first deploy behaves exactly as before P3 |

The overarching principle: **every "no data" path is honest about being no data**,
and every decision path defaults to the conservative side while logging that it
did so.

---

## 8. The accounting cycle (survival-agent)

Separately from the bots, survival-agent runs its own cycle — but it **does not
trade**. It reads.

1. `freqtrade_client.get_summary()` pings each bot, logs in, and reads
   `/api/v1/balance`, `/api/v1/profit`, `/api/v1/status`.
2. `_extract_profit()` defensively probes several field names in Freqtrade's
   profit response (its shape varies by version), returning `0.0` rather than
   crashing if none match.
3. `compute_split(profit, months_elapsed, config)` applies the plan:

   ```
   profit_after_survival = max(0, profit − survival_monthly)
   take_pct_effective    = 0            if months_elapsed <= ramp_months
                           take_pct/100 otherwise
   payout                = profit_after_survival × take_pct_effective
   reinvested            = profit_after_survival − payout
   risk_eligible         = max(0, reinvested) × risk_pct/100
   ```

4. The result is appended to `data/decisions.log` and shown on the dashboard.

The cycle only runs if `engine_enabled` is `true`. Default is `false`.

**Important honesty note:** payouts here are a *number to review and act on
yourself*, not an automatic transfer. Nothing moves money.

---

## 9. The dashboard

Opening `http://<host>:3446` shows:

- **Environment variables** — read-only, grouped by bot, showing exactly what
  the container sees: 🔴 not set, 🟡 set but empty, 🟢 set, or (for the three
  dry-run toggles) "default" / "live — REAL MONEY".
- **Trading bots** — one card per bot with balance/profit/open trades and a link
  to that bot's own FreqUI.
- **Thresholds (General, shared)** — the single `config.json` plan: start,
  target, months, survival cost, ramp, take %, risk %.
- **Capital ledger** — your own record of deposits/withdrawals per bot, so
  "% return on what was actually put in" stays correct even if you top up an
  account later.
- **Balance history** — one snapshot per UTC day, kept ~180 days.

---

## 10. Going live

There are two things that must agree, and they are **not** the same thing:

1. `POST /api/live-trading` with `confirm_text == "LIVE"` — records *intent* in
   `data/.env`. The UI warning text says explicitly that this does not change
   Freqtrade's behavior.
2. **Portainer → stack Environment variables → `FREQTRADE__DRY_RUN=false` (and
   the matching per-bot toggles), then redeploy.** This is what actually flips a
   bot to real money.

The API response and the frontend both say this plainly. That caveat is
load-bearing — do not soften it. It is the only thing keeping "the UI says
PAPER" and "the container is actually LIVE" from being a false promise.

Before going live at all, the recommended step is to watch dry-run decisions for
a while and read the backtest — see README.md.

**The Survival Bot has a third, additional requirement on top of the two
above**, since it isn't meant to go live on the same basis as the other four:
`SURVIVAL_LIVE_MODE=true` must also be set (a strategy-level check,
independent of Freqtrade's own dry-run flag), and only after
`backtest/survival_ab_test.py`'s replay gate passes. As of this writing that
gate has **not** passed (§5.5) — `SURVIVAL_LIVE_MODE` should stay `false`.

---

## 11. Why the bots differ (design intent)

Each bot exists to express a distinct market hypothesis, and their prompts are
written to match:

- **Main** — "buy strength." Needs a clear positive move with contained
  volatility. Its earlier prompt over-weighted caution (309/310 decisions were
  "hold" through a +27% rally); that bias was removed after a real replay run.
- **Shorts** — same idea, both directions, at 1x leverage. Leverage is capped at
  1x deliberately: Freqtrade's own docs warn against >1x on a strategy with no
  live track record.
- **Moonshot** — "buy ignition." Young, volatile, memecoin-friendly, exits on a
  trailing peak so it can ride a pump and bail on the reversal.
- **Dip** — "buy panic, not decay." Requires evidence of stabilization (RSI +
  distance above recent low) *and* the absence of project-specific bad news.
  Explicitly not a memecoin bot.
- **Survival** — not a market hypothesis at all. An isolated question about
  *prompting*: does telling the model its "existence" depends on hitting a
  target change how it trades, for the worse, before that framing is ever
  risked on a real bot? (Answer so far: yes, measurably — §5.5.)

---

## 12. Known limits & open questions

Stated honestly, so nobody mistakes them for finished work:

- **No live track record.** All five bots (four live + the isolated Survival
  Bot) run in dry-run by default. Any claim about edge is unproven.
- **Replay covers Main only.** `backtest/llm_replay.py` has no harness yet for
  Shorts/Moonshot/Dip — each has different decision sets and exit mechanics.
- **Rotation may be too eager.** In the one real replay that ran, 15 of 18 exits
  were `rotate_stale_signal`. Whether that's the prompt, `MIN_HOLD_HOURS`, or the
  rotation logic itself is an open question.
- **News fetches are unverified end-to-end** in the dev environment (no network
  access to `news.google.com`); the XML parsing follows the RSS 2.0 spec but
  hasn't been exercised against a live response there.
- **Historical context is absent from replays** — no free historical news
  archive exists, so the replay tells the model that honestly rather than
  fabricating it.
- **The dormant engine's `executor.py` is a stub.** It logs intended trades and
  never places orders.
- **P4's pairlist filters were never integration-tested inside a real running
  Freqtrade instance** — their pure decision logic is unit-tested and their
  real-data fetch paths were verified standalone (`backtest/p4_dry_run_compare.py`,
  `p4_historical_squeeze_check.py`), but whether Freqtrade's plugin loader
  actually picks up `FundingRsiPairList`/`BollingerTrendPairList` by name from
  `config.json` needs confirming on the next real deploy.
- **P5's Grok/NVIDIA NIM/llm7.io integrations were never exercised against a
  live account.** Their model slugs and endpoint shapes are best-effort from
  public docs; only DeepSeek, OpenRouter, and Gemini's REST shape are on
  firmer ground. Check container logs for `"second opinion: <provider> call
  failed"` after first enabling any bot's flag.
- **P5 Phase 2 (the replay-based go-live gate for the ensemble) has not run
  for real** — same reason: no provider keys configured yet. A trivially
  "passing" run with zero real votes gathered proves nothing; the script
  itself warns about this when it happens.
- **The Survival Bot's gate failed on its first real test** (§5.5) — this
  specific shutdown-pressure framing is not going live. That doesn't rule out
  a different framing being tested the same way in the future.


## 13. How we find out whether a bot has an edge (the measurement layer)

1. **Every decision is recorded.** The six LLM bots log `DeepSeek said buy|sell_short|hold for PAIR`; the audit parses those lines (no change to any strategy) into an append-only ledger together with the
   price of the candle that closed just before the decision. A seventh source, `kronos`, is a separate container that forecasts candles and writes its own signals in the same format.
2. **Forward returns, honestly.** Each night, for each decision, the job reads the bot's own candles *after* the decision and computes the return at 1h, 4h and 24h. It never uses a candle at or before
   the decision for anything but the decision price.
3. **Against the market, not against zero.** The benchmark is the average return of all pairs the bot looked at in the same candle. A short is scored with the sign flipped. Per bot and direction the report
   gives n, hit rate, mean excess return, a bootstrap 95% confidence interval and the rank correlation between the bot's confidence and the outcome. Fewer than 100 actions in a direction: "not enough data".
4. **Context.** The hourly snapshot (derivatives, calendar, news state, sentiment) lets the audit ask "do buys do better when funding is extreme?"; a slice needs 50 signals.
5. **From trades, not from signals.** Separately, the brain labels each bot from its closed trades after fees (`no_data`, `no_edge`, `unproven`, `edge`) and flags bots whose fees eat more than half of
   their gross P&L. `edge` needs a confidence interval above zero and a better result than the control bot and BTC buy-and-hold. The live-money checklist lives in the report; no bot passes today.

## 14. How the allocator decides (and what stops it)

> **Policy note.** Section 14 describes the `protect` policy (an LLM proposes a weight, hard rules veto it). The default policy since 24.10.2026 is `grow`: deterministic, no LLM call, weights from each bot's own results (1.5x or 2x for a bot that earns, 0 for one that loses, a 0.5x trial when its control bot made money), a circuit breaker for fast losers, and portfolio protection that only scales the book down. `ALLOCATOR_POLICY` switches between them; a run that finds a live bot uses `protect`. Details: README and ENVIRONMENT_VARIABLES.md.

1. Once a day (00:40 UTC) and when an event appears (a drawdown rung, a macro event within 24h), code gathers numbers: P&L and fees per bot over 7 and 30 days, evidence status, long/short share, turnover,
   current weights, progress to the weekly (0.25%) and monthly (1%) targets, the BTC regime, and the latest context snapshot with each field's status. Targets only ever protect gains.
2. An LLM answers with one JSON object `{bot: {"weight": 0..1, "reason": "..."}}`. Code validates it; an unusable reply gets one retry and then means "no change".
3. Hard rules then override the proposal: at most +-25 percentage points per day; the drawdown ladder; proportional CPPI scaling (everything stops only at 1.5x the risk budget or a floor breach); a cap of 0.5 before
   a macro event; no opposite positions in one coin; a cap on concentration. Incomplete data only produces warnings.
4. In `shadow` mode that is all: the decision is logged. In `enforce` mode a weight of 0 pauses a bot's new entries, a weight above 0 resumes it, and a weight in between scales its position size. A bot that sits at 0
   for a week gets a one-day probe weight so it keeps producing evidence.
5. The counterfactual replays every closed trade with the weight in force when it opened ("with") against weight 1 ("without"). After four weeks the allocator stays on only if it wins steadily; otherwise it drops
   back to shadow by itself. The kill switch on the dashboard does the same immediately.

## 15. What this does not prove

Nothing here proves a profitable strategy. The audit can say "no edge" or "not enough data" much more reliably than it can say "edge", the sample sizes are small, the period is short, and several inputs
(liquidations, ETF flows, on-chain) are not collected. Read the "Honest status" section of the README and the per-change "not verified" lists in AGENTS.md before trusting any number.

## 16. Replay: "what would these bots have done" in minutes

Waiting weeks for live data is slow, so `/replay` can run the six LLM bots' unchanged strategy code over the recent past. At each simulated moment the strategy sees only candles that had closed by then and a Fear & Greed value as of then; there is no news archive, so the prompt says so.
The model's answers are real (and cached, so a repeat is free); entries are filled at the next candle's open with fee and slippage; exits use each bot's own ROI table, stoploss and rotation logic. The report compares each bot with BTC buy-and-hold and with random entries that use the same exits,
and runs every bot twice (real names / anonymized) because a model can "remember" a market it was trained on. A negative result is a strong argument against a bot. A positive one is weak, and replay never gives the status `edge`.

### Calibrating the simulator on the real bots' decisions

To tell simulator error from model error, `/replay` has a calibration run. It replaces the LLM with the decisions a live bot really made (read once from its log), lets the real strategy code do entries, rotation exits and stake sizing, and compares the trades it produces with the trades the live bot really made (matched by pair, side and open time within one candle). It costs nothing. It also shows which hours the LLM was down: a failed call is logged as `hold`, so an outage looks like the whole market going flat and rotation then closes positions.

### Replay extras

Besides "named" and "anonymized" model runs a replay also runs the strategy's own simple rule (no LLM) so you can see whether the model beats it. A finished run can be re-run in `frozen` mode: the same decisions, different exit rules (no rotation, longer minimum hold, another ROI table, trailing stop), for free. The simulator follows the real Freqtrade loop (decisions, then exits, then entries; entries are passive limit orders that can expire unfilled), which was checked against the live bots' real decisions. Another model (for example a free OpenRouter one) can be used for smoke tests, with a per-minute and a daily limit; its results are always labelled and never mixed with DeepSeek's.
