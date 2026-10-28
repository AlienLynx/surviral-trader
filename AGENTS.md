# AGENTS.md

Read this before changing anything in this repo. It's written for an AI
agent (or a human moving fast) picking up the codebase cold.

## What this project is

A self-funding trading module for one operator. Goal-driven: given a
starting capital, a target, a timeline, a monthly survival cost, and a
profit-split rule, it should grow the capital toward the target while
covering its own LLM API bill and periodically paying out a share to
the operator.

## Status as of v0.1

Implemented and real:
- `backend/app.py` — FastAPI service, serves the frontend, persists
  thresholds to `data/config.json` and credentials to `data/.env`.
- `frontend/index.html` — settings UI for both of the above.
- `backend/engine/risk_guard.py` — hard-coded risk limits, enforced
  in code. This is load-bearing: nothing downstream should be able to
  bypass it.
- `backend/engine/brain.py` — LLM call wrapper. DeepSeek primary,
  OpenRouter (free-tier model) as fallback if DeepSeek errors or times
  out.

Partially implemented:
- `backend/engine/signals.py` — price and volatility from Bybit's
  public kline endpoint (no key needed). Real, working response
  parsing. Deliberately does NOT include news/sentiment yet — that's
  a second input to add later (call `brain.ask()` on a few headlines,
  merge the result into the same dict), not before the price-only
  version has been watched for a while and its decisions make sense.

Stubbed, not yet live:
- `backend/engine/executor.py` — Bybit order placement. Currently
  DRY_RUN only: it logs what it *would* do and never calls a
  place-order endpoint. Flipping this to live trading is a deliberate,
  separate task — don't do it as a side effect of an unrelated change.
- `backend/engine/loop.py` — ties signals → brain → risk_guard →
  executor into one cycle, started from `app.py` on startup behind
  the `engine_enabled` config flag (default `false`). Even when
  enabled, `executor.py` still defaults to dry-run — enabling the
  loop only means it starts *thinking* on a schedule, not trading.
  Known gap: `capital_usd` inside a cycle comes from config's `start`
  field, not a live Bybit balance — there's no authenticated balance
  fetch yet. Every decision logged says so. Don't read a decision log
  entry as "the agent knew its real balance" until that's fixed.
  Same for `open_positions` / `daily_pnl_pct` — both hardcoded to 0
  until a state store exists.

## Three bots, one stack (`freqtrade/`, `freqtrade-shorts/`, `freqtrade-moonshot/`)

Deliberately kept in one `docker-compose.yml` / one Portainer stack,
per the operator's explicit preference, even though each bot is a
fully independent Freqtrade instance (own image, own config, own
strategy, own trade DB, own port). What's shared: the DeepSeek/
OpenRouter API keys and the FreqUI login (operator's choice — "same
login for both, simpler"). What's deliberately NOT shared: each bot
has its own `BYBIT_*_API_KEY`/`_SECRET` env var (empty by default)
and its own `FREQTRADE*__DRY_RUN` toggle, so flipping one live never
touches another, and each is meant to trade from its own Bybit
sub-account (Freqtrade's own docs recommend this explicitly).

- **`freqtrade/` (main, port 8099)** — unchanged, spot, long-only.
  The operator was explicit: don't touch this one when adding the
  other two.
- **`freqtrade-shorts/` (port 8110)** — same DeepSeek approach, but
  `trading_mode: futures`, `margin_mode: isolated`, `can_short = True`,
  and `leverage()` hardcoded to always return `1.0`. That last part
  isn't arbitrary — Freqtrade's own docs say explicitly: *"Do not
  trade with a leverage > 1 using a strategy that hasn't shown
  positive results in a live run using the spot market."* This
  strategy has zero live track record, so futures mode is used here
  only to unlock shorting, not to add leverage on top of an unproven
  strategy. DeepSeek is asked for a 3-way decision (buy/sell_short/
  hold) instead of 2-way. Pair whitelist regex is `.*/USDT:USDT`
  (CCXT's unified perpetual-futures symbol format) rather than plain
  `.*/USDT` — unverified against a live Bybit futures market at the
  time this was written; check the pairlist-refresh log lines on
  first deploy to confirm real pairs are actually being matched, not
  an empty list.
- **`freqtrade-moonshot/` (port 8120)** — spot, small/new-listing
  hunter, deliberately different exit mechanism: `custom_exit()`
  sells at a 50% gain target or a 3-day max hold, whichever comes
  first, instead of the other bots' `minimal_roi`/fixed `stoploss`
  (stoploss here is a wide -25%, a real floor but not the primary
  exit). `dry_run_wallet: 100`, much smaller than the other bots --
  meant to be a slice of risk-tolerant capital, not a third full
  position. This is a v1 heuristic for "moonshot hunting," not a
  guaranteed buy-on-listing-day sniper -- see the strategy file's
  docstring for the honest limits.

  **Real bug, caught after a day of it never once seeing a fresh
  pair:** the original pairlist was `VolumePairList` (top 30 by
  volume) with no `AgeFilter` at all, on the theory that "how new" a
  pair is would be computed inside the strategy from candle count
  instead of gatekept by the pairlist. That doesn't work --
  `VolumePairList`'s `number_assets: 30` is the *base* selection, and
  filters downstream can only remove pairs from a base list, never
  add ones that were never selected in the first place. Genuinely
  fresh listings have low volume by definition (nobody's found them
  yet), so they never made it into the top-30-by-volume base list to
  begin with -- confirmed by a full day of logs where literally every
  single decision line said "established pair," never once "fresh
  listing." Fixed two ways at once: `number_assets` raised to 500 (wide
  enough to capture effectively the whole USDT market as the base
  list, not just the most-traded slice of it), and an actual
  `AgeFilter` added, explicitly overriding the default `min_days_listed`
  (10, which would have silently excluded exactly the newest listings
  this bot exists to catch). The first attempt set it to `0` --
  Freqtrade's own `AgeFilter` rejects that outright at startup
  ("AgeFilter requires min_days_listed to be >= 1"), which crashed the
  whole bot rather than just failing to filter -- fixed to `1`, the
  actual minimum Freqtrade allows. `max_days_listed: 14` caps how "not
  fresh" a pair can be and still qualify.

  **Second follow-up, same day:** `max_days_listed: 14` produced a
  genuinely empty whitelist ("Whitelist with 0 pairs: []", "Active
  pair whitelist is empty") -- not a code bug, just Bybit apparently
  not having listed anything in that exact 14-day window at that
  moment. A bot with zero pairs can't evaluate anything, let alone
  buy. Widened to `max_days_listed: 60` to improve the odds of
  catching something on any given day, still much tighter than the
  other two bots' 30-day *minimum*. If this keeps producing an empty
  whitelist even at 60 days, that's a sign Bybit's actual new-listing
  cadence is slower than this bot's design assumed, and the window
  may need to be wider still, or the bot's premise revisited.

  **Third follow-up, next day:** 60 days produced only 2 pairs
  (KII, GRVT) -- not empty, but thin enough that the bot has very
  little to actually choose from regardless of how good the entry
  judgment is. Two changes at once, since both are plausible
  contributors and neither alone is clearly "the" cause: widened
  `max_days_listed` further to 120 (Bybit's real new-listing cadence
  may genuinely be slower than 60 days assumed), and loosened
  `SpreadFilter.max_spread_ratio` from 0.02 to 0.05 -- genuinely
  fresh, thin-volume pairs often have wider spreads before market
  makers catch up, and 2% may have been silently excluding exactly
  the kind of pair this bot exists to catch. The wider spread
  tolerance is a deliberate tradeoff (worse fill prices are possible),
  consistent with this bot's already-elevated risk profile (-25%
  stoploss, $100 dry-run wallet) rather than a casual loosening. If
  the whitelist is still thin after this, the honest next read is
  that Bybit's spot market just doesn't have many fresh listings
  right now -- not necessarily a filter-tuning problem anymore.

`survival-agent`'s dashboard (`:3446`) shows all three via
`freqtrade_client.BOTS` (a dict of bot id → internal URL/label) and
`GET /api/freqtrade-summary/all`. Adding a fourth bot later means:
one new service block in `docker-compose.yml`, one new entry in
`BOTS`, one new port in the frontend's `BOT_PORTS` map — nothing else
in the client/API needs to change. FreqUI itself already supports
adding multiple bots in its own "Available bots" screen (browser-side
config, not something this repo can pre-populate) — point it at each
bot's own port to see them all in one FreqUI session too.

**Adding a second/third bot to one FreqUI session needs CORS
configured, not just a login attempt.** Trying to add
`freqtrade-shorts` (:8110) as an "Available bot" from a FreqUI page
already open at :8099 failed with "Login failed... check your bot's
CORS configuration" even though `curl .../api/v1/ping` worked fine --
`curl` and direct browser navigation to `/api/v1/ping` aren't subject
to CORS, but FreqUI's login call (made cross-origin, from the :8099
page to the :8110 API, with credentials) is. Freqtrade's own docs are
explicit that a wildcard `"CORS_origins": ["*"]` does NOT work for
this either -- the CORS standard disallows wildcard origins for
credentialed requests, which a login call always is. Fixed by listing
all three bots' exact origins in each `config.json`'s
`api_server.CORS_origins`, so any one of them can be reached from a
FreqUI session open on any of the others. **(Since 07.10.2026 the host name is no longer in the repository: each bot's `entrypoint-cors.sh` builds the list from the `FREQTRADE_UI_HOST` environment variable;
the `config.json` files carry an empty list.)** If the host you open the UIs with changes, change `FREQTRADE_UI_HOST`, or cross-bot FreqUI sessions will hit this same error again.

## The Freqtrade module (`freqtrade/`)

Added after evaluating ~10 existing open-source options (Freqtrade,
Hummingbot, Jesse, TradingAgents, ai-hedge-fund, Vibe-Trading, etc.)
rather than continuing to hand-roll execution logic. Verdict at the
time: Freqtrade is the most mature, actively-maintained engine with
real backtesting and real exchange execution (including Bybit).
LLM-native frameworks like TradingAgents have a more rigorous
decision *pattern* (bull/bear debate, risk sign-off) worth borrowing
conceptually, but their own docs describe them as research tools, not
verified live-trading systems -- don't install one wholesale expecting
it to replace due diligence.

- `docker-compose.yml` (repo root) -- single Portainer stack, both
  `survival-agent` and `freqtrade` services build their images from
  this repo (not `image:` pulling a bare tag for freqtrade). Reason:
  Portainer's git-deployed stacks do NOT copy repository files into
  the host folders it manages for bind-mount volumes -- it only
  auto-creates them empty. A volume mount pointing at
  `freqtrade/user_data` from git would silently mount an empty
  directory, and Freqtrade fails with "Config file not found" even
  though the file is right there in the repo. `freqtrade/Dockerfile`
  bakes `config.json` and `strategies/` into the image at build time
  instead -- build context reads the actual git checkout correctly,
  bind mounts of relative host paths under Portainer's git stacks do
  not. The trade database uses a named Docker volume (`freqtrade-db`)
  rather than a bind-mount, for a second, unrelated reason: Freqtrade
  runs as a non-root user, and a bind-mount always carries the host
  directory's ownership -- named volumes get seeded from the image's
  ownership at that path instead (see `freqtrade/Dockerfile`).
  Freqtrade's own log output just goes to stdout, visible via
  Portainer -- no separate log file/volume for it.
- `freqtrade/user_data/config.json` -- dry_run controlled via the
  `FREQTRADE__DRY_RUN` env var now (see the Trading Mode UI and hard
  invariant #6 below), not hand-edited in this file. The file's own
  `dry_run: true` is just the fallback if that env var is unset.
- `freqtrade/user_data/strategies/DeepSeekStrategy.py` -- entries come
  from DeepSeek (OpenRouter fallback), exits come from Freqtrade's own
  `minimal_roi`/`stoploss`. Backtesting mode uses a deterministic
  placeholder instead of calling the LLM -- see the file's docstring.
  This intentionally duplicates a small slice of `brain.py`'s logic
  rather than importing across the two stacks; if you change how
  DeepSeek is called in one, check whether the other needs the same
  fix.
  - **Pair selection is dynamic**, not hand-picked. `config.json` uses
    `VolumePairList` (top 15 USDT pairs by volume) plus `AgeFilter`,
    `PriceFilter`, `SpreadFilter`, `VolatilityFilter` to exclude junk
    -- Freqtrade's own standard toolkit for this, not something
    reinvented here. `exchange.pair_blacklist` explicitly excludes
    stablecoin-vs-stablecoin pairs and leveraged tokens (regex).
  - **Risk tiers are computed, not hardcoded.** A pair is "core" only
    if it's currently in the top `CORE_VOLUME_RANK` (5) of the
    dynamic VolumePairList *and* its own 24-candle volatility is
    below `CORE_MAX_VOLATILITY_PCT` (5%) -- both checked fresh every
    cycle in `_classify_pair()`. High volume alone doesn't qualify a
    pair as core (a pumping pair can have huge volume too). A pair
    can move between tiers as market conditions change; nothing is
    pinned to a fixed symbol list. Satellite pairs get
    `SATELLITE_STAKE_MULTIPLIER` (0.5) sizing via `custom_stake_amount`,
    and the DeepSeek prompt is told which tier applies and asked to
    require a clearer signal for satellite. Unknown/uncached pairs
    default to satellite -- the safer side to err on.
  - **Uses its own trade history now, not just live indicators.**
    `_recent_performance_summary()` reads Freqtrade's own DB via the
    documented `Trade.get_trades_proxy(is_open=False)` API (no custom
    persistence needed -- this is Freqtrade's own trade history,
    already durable via the `freqtrade-db` named volume) and feeds a
    short win-rate/avg-return summary, scoped to the same risk tier
    when there's enough data, into every DeepSeek prompt. Early on
    this will mostly say "no closed trades yet" -- that's honest, not
    a bug. This is a lightweight feedback loop (recent track record
    as prompt context), not model fine-tuning or parameter
    auto-tuning -- deliberately kept simple and auditable rather than
    letting the bot silently adjust its own thresholds based on a
    small, noisy sample.
  - **`get_market_context()`** adds one piece of real signal beyond
    single-pair price action: CoinGecko's free `/global` endpoint
    (total market cap 24h change, BTC dominance), cached for 45
    minutes and shared across every pair's prompt in a cycle so a
    6-15-pair pool doesn't hammer a free API once per pair. Fails
    honestly to `"unavailable"` rather than fabricating a number, and
    a failure here never blocks a trade decision -- it's context, not
    a gate. This is intentionally still lightweight (no news
    headlines, no sentiment model) -- see the original "v0.1: price
    and volatility only" note in `backend/engine/signals.py` for why
    a heavier sentiment layer was deliberately deferred; the same
    reasoning applies here.
  - **Real bug found and fixed:** for a while after the Portainer-env-vars
    pivot (item 7), `docker-compose.yml`'s `freqtrade` service
    environment block only listed the `FREQTRADE__*`/exchange keys --
    `DEEPSEEK_API_KEY` and `OPENROUTER_API_KEY` were never passed in
    at all. `_call_deepseek`/`_call_openrouter_fallback` both return
    `None` immediately when their key is missing, and `_ask_deepseek`
    treats `None` as "hold" -- so every entry decision was silently
    defaulting to `hold` with no visible difference from a genuine
    conservative decision. Fixed by adding both keys to the
    `environment:` block (sourced the same way as everything else,
    from Portainer's stack Environment Variables). Also added an
    explicit `log.error` when both keys are missing, specifically so
    this silent-failure mode is never invisible again -- if you see
    that error in the logs, it means every decision since then has
    been a fake "hold," not a real one.
  - **Recurring credential desync, finally fixed at the root.**
    `login/credentials failed` kept recurring throughout the first
    real day of operation -- traced to the actual structural cause:
    Freqtrade reads its login from Portainer's stack Environment
    Variables (item 7), but `survival-agent` only ever read its own
    copy from `data/.env`, populated by hand through the Credentials
    UI. Two independent copies of the same password will drift every
    time one is updated without the other -- which happened for real,
    more than once. Fixed by adding `FREQTRADE_API_USERNAME` /
    `_PASSWORD` / `_JWT_SECRET` directly to `survival-agent`'s own
    `environment:` block in `docker-compose.yml`, sourced from the
    exact same Portainer stack vars Freqtrade uses. This works because
    `load_dotenv()` in `app.py` does not override already-set env vars
    by default, so the Portainer-sourced value always wins over
    whatever's sitting in `data/.env`. The Credentials panel still
    accepts these fields (feeds the "Copy for Portainer" block), but
    they no longer need to match Portainer by hand -- Portainer is now
    the single real source for both services.
  - **Third round, same day:** even after fixing the model name, the
    DeepSeek outage that started around 2026-09-14 14:34 US-Eastern
    (see the "Second real bug" entry above) turned out to last many
    hours -- still failing at 21:46 the same evening, confirmed via
    `curl` directly from inside the freqtrade container hitting a
    connection-level timeout (`HTTP_CODE:000`) on `/chat/completions`
    specifically, while a plain `GET /` to the same host answered
    instantly with `401` through their CDN -- i.e. their edge was up,
    their inference backend wasn't. Meanwhile the single-model
    OpenRouter fallback (`deepseek/deepseek-r1:free`) turned out to
    have been pulled from free entirely -- OpenRouter's own error
    body said so explicitly: `"This model is unavailable for free...
    use this slug instead: deepseek/deepseek-r1"` (paid). Betting on
    one fallback model slug had now failed twice. `_call_openrouter_fallback`
    was rewritten to try a short list of independent, differently-sourced
    free candidates (`nvidia/nemotron-3-ultra-550b-a55b:free`,
    `google/gemma-3-27b-it:free`, `meta-llama/llama-3.3-70b-instruct:free`)
    in order, logging which one actually worked. Costs a little latency
    on this rarely-hit path, in exchange for not going fully blind
    (zero real decisions, every cycle silently defaulting to `hold`)
    the next time any single provider's free tier changes. This is
    exactly what happened overnight on 2026-09-14: zero trades all
    night was not the strategy being cautious, it was 100% of decision
    calls failing and defaulting to the safe fallback -- worth
    remembering next time "the bot isn't doing anything" comes up:
    check the logs for `call failed` lines before assuming the market
    was just quiet.
  - **Fourth round: stopped hardcoding OpenRouter model slugs at all.**
    The 3-candidate list above lasted less than a day -- within 24
    hours, `google/gemma-3-27b-it:free` was also pulled from free
    (same "use this slug instead" paid-redirect error pattern as the
    DeepSeek ones before it). OpenRouter's free tier is evidently
    being cycled aggressively right now, and every hardcoded slug list
    needs a code push + redeploy to fix once it goes stale -- too slow
    for how often this is happening. `_get_openrouter_free_models()`
    now queries OpenRouter's own public `/api/v1/models` endpoint
    (no auth needed) and filters for `pricing.prompt == 0 and
    pricing.completion == 0`, cached for 6 hours. `_call_openrouter_fallback`
    tries the first 5 of whatever comes back, live, every time. This
    should be the last time this specific class of bug needs fixing
    with a code change -- if it still needs fixing again, the
    `/models` endpoint's response shape itself may have changed, which
    would be a different, real bug worth investigating properly rather
    than patching another hardcoded list.
  - **Fifth round: stopped looping through candidates ourselves.**
    OpenRouter has a native `models` array parameter (see their
    routing docs) that does exactly what the manual Python loop above
    was doing, server-side, in one HTTP request: pass model IDs in
    priority order, OpenRouter tries each internally and returns the
    first success. Failed attempts in that chain aren't billed. This
    replaced the multi-request loop with one call, and added a cheap
    paid model (`openai/gpt-4o-mini`) as the very last entry in the
    chain -- it only gets charged if every free candidate in the list
    genuinely failed. This uses part of the ~$8 OpenRouter balance as
    a real last line of defense rather than leaving the bot fully
    blind if free-tier availability has a bad day across the board.
  - **Sixth round, same rollout:** the very first deploy of the
    `models` array broke everything -- OpenRouter returned a 400 on
    every single call ("'models' array must have 3 items or fewer")
    because the code sent up to 6 entries (5 free + 1 paid). The "up
    to 5" limit came from a search result that turned out to be
    **Perplexity's** documentation for a similarly-named feature on a
    completely different API, not OpenRouter's actual limit -- easy
    mistake when a search returns multiple companies' docs for a
    generically-named feature ("model fallback array"). OpenRouter's
    own error message was the authoritative source and should have
    been trusted over a search snippet from the start. Fixed to 2 free
    candidates + 1 paid = 3 total. If this limit is ever wrong again,
    trust OpenRouter's own error response over any external
    documentation, including this file.
  - **Seventh round, arguably the most important one:** DeepSeek was
    reachable and returning genuine `200 OK` responses the whole time
    the OpenRouter fallback was firing on every single cycle --
    confirmed via a direct `curl` that got back `{"content": "",
    "reasoning_content": "The user just said \"", "finish_reason":
    "length"}`. `deepseek-flash` reasons before answering (a separate
    `reasoning_content` field, distinct from the final `content`).
    `max_tokens: 10` was being fully consumed by the reasoning phase,
    leaving `content` empty with `finish_reason: "length"` -- and an
    empty string is falsy in Python, so `_call_deepseek()`'s return
    value silently tripped the `or self._call_openrouter_fallback(...)`
    fallthrough in `_ask_deepseek()` every time, even on a genuine
    success. This one hid behind every earlier fix in this list --
    DeepSeek could have been back up for a while and nothing would
    have shown it, because "succeeded but empty" and "failed" looked
    identical downstream. Fixed by raising `max_tokens` to 300 for the
    DeepSeek call (and defensively to 100 for the OpenRouter fallback,
    since rotating free candidates could hit the same issue) and
    adding a `log.warning` specifically for "200 OK but empty content"
    so this doesn't go silent again. If a model ever again seems to
    "always fail over to fallback" despite being reachable, check the
    raw response body for a `reasoning_content` field and an empty
    `content` before assuming it's actually down.
  - **Eighth round: the actual root fix for the above, a day later.**
    Raising `max_tokens` to 300 reduced but didn't eliminate "200 OK
    but empty content" -- it still happened intermittently a full day
    into real operation, because reasoning length varies and 300
    tokens isn't always enough either. DeepSeek's own docs describe
    the real fix: pass `"thinking": {"type": "disabled"}` in the
    request body. V4-family models reason by default with no way to
    turn it off via `max_tokens` tuning -- only this explicit flag
    disables it. All three strategies' `_call_deepseek()` (and
    `backend/engine/brain.py`, the unused fallback engine, for
    consistency) now send this. Also faster and cheaper per call,
    since no tokens are spent on reasoning for what's meant to be a
    fast classification decision, not a hard reasoning problem.
  - **Decisions now log a short reason, not just the word.** The
    prompt was single-word-only ("buy" or "hold"), which made it
    impossible to tell from the logs whether a long streak of `hold`
    meant "the market genuinely never looked good" or "something's
    silently broken" -- exactly the ambiguity that hid the empty-content
    bug above for a while. `_ask_deepseek()` now asks for `"hold: <reason
    under 15 words>"` / `"buy: <reason under 15 words>"`, parses the
    reason out, and logs it alongside every decision. Falls back to
    scanning the whole response for "buy" if a model doesn't follow the
    format exactly -- a formatting slip should never silently block a
    real signal.
  - **Second real bug found the same day, same symptom (silent fake
    "hold"):** the model name `"deepseek-chat"` was retired by
    DeepSeek on 2026-07-24 -- calls to it don't fail cleanly, they
    hang and time out, which looked identical to a network problem
    rather than a stale model name. Fixed to `"deepseek-v4-flash"`
    (DeepSeek's documented non-thinking replacement). The OpenRouter
    fallback's `"deepseek/deepseek-chat:free"` had also been renamed
    (404, not 401 -- a wrong/missing key gives 401, so a 404 here
    means the model slug itself is gone) and was updated to
    `"deepseek/deepseek-r1:free"`. Both model names now have inline
    comments in `DeepSeekStrategy.py` explaining this churns and how
    to tell "stale model name" apart from "real network/key problem."
    `backend/engine/brain.py` (the unused fallback engine) got the
    same fix for consistency, per its own docstring's instruction to
    check both files together.
  - **Same-day follow-up:** the first fix (`"deepseek-v4-flash"`) was
    already one step behind -- DeepSeek's current docs (checked
    2026-09-14) say the officially current name is `"deepseek-flash"`;
    `v4-flash` is described as a "still accepted" legacy alias routed
    to V4.1-Flash, not guaranteed to stay that way. Updated to
    `"deepseek-flash"`. Separately, multiple independent status
    trackers confirmed a real, ongoing DeepSeek outage on 2026-09-14
    (reports spiking that same afternoon) -- so some of the timeouts
    seen while debugging this were a genuine provider-side outage
    coinciding with the naming fix, not caused by it. The dry-run bot
    handled this safely by design: both LLM calls failing just means
    every decision defaults to `hold`, which is the intended fallback,
    not a crash.

**If you ever add a new file Freqtrade needs to read at startup
(config, strategy, anything), it must go through `freqtrade/Dockerfile`
as a `COPY`, not through a bind-mount volume pointing at a path that
only exists because it's tracked in git.** This bit us once already --
see the commit that added the Dockerfile for the full story.

This **is now the primary path** as of this pass: `loop.py`'s default
`run_cycle()` reads Freqtrade's state and computes the split; it does
not independently decide trades. The old chain is preserved as
`run_cycle_own_engine_experimental()` in the same file for manual
testing, but is not invoked by the background loop.

## Hard invariants — do not violate these

1. **Credentials never enter git.** `data/` is gitignored. If you add
   a new secret (a new exchange key, etc.), it goes through the
   `/api/credentials` endpoint into `data/.env`, never into
   `config/finance_config.json`, never hardcoded, never logged in
   plaintext. When adding a log statement near credential handling,
   mask the value. **Always call `set_key(..., quote_mode="never")`.**
   python-dotenv's default (`quote_mode="always"`) wraps values in
   quotes in the file -- fine for python-dotenv's own reader, which
   strips them back out, but Docker Compose's `env_file:` directive
   does NOT strip quotes; it treats them as literal characters. That
   mismatch caused a real bug: Freqtrade received a password with
   literal quote marks baked in while our own client sent the clean
   value, so login silently failed with no useful error beyond
   "credentials wrong." Every `set_key()` call in this repo must pass
   `quote_mode="never"` -- if a new one doesn't, this bug comes back.
2. **Risk limits live in code, not in the LLM prompt.** `risk_guard.py`
   is the last checkpoint before any trade executes. A prompt change
   in `brain.py` must never be the only thing standing between the
   model and an oversized trade. If you're tempted to add "don't risk
   more than X%" to a prompt string instead of to `risk_guard.py`,
   that's the wrong file.
3. **`executor.py` defaults to dry-run.** `DRY_RUN` is read from env,
   defaults to `true` if unset. Never change that default in code —
   the operator flips it explicitly through the credentials UI.
4. **The profit split is config, not a constant.** `data/config.json`
   holds `start`, `target`, `months`, `survival_monthly`,
   `ramp_months` (months with 0% payout), `take_pct` (payout share
   after ramp), `risk_pct` (share of *reinvested* profit that's
   risk-eligible vs. conservative). All of these are meant to be
   changed from the UI at runtime — don't hardcode any of them
   elsewhere.
5. **One active position at a time, unless the operator raises
   `max_concurrent_positions` in config.** Default is 1. This is a
   safety rail against the LLM spreading a small account across too
   many simultaneous bets.
6. **Going live is a deliberate, confirmed action — never silent.**
   `POST /api/live-trading` requires `confirm_text == "LIVE"` to enable
   real trading, and refuses if Bybit keys aren't set. **Important
   caveat added after the env_file saga below:** this endpoint only
   records *intent* in `data/.env` (used for survival-agent's own
   status display). It does **not** control Freqtrade's actual
   behavior — Freqtrade's real `DRY_RUN` and credentials come from
   Portainer's own stack-level Environment Variables (see item 7).
   The operator must set `FREQTRADE__DRY_RUN` there too and redeploy
   for the two to agree. This is a real inconsistency risk: the UI
   can say "PAPER" while the actual Freqtrade container is "LIVE" if
   someone updates one without the other. The API response's `note`
   field and the frontend's red warning text both say this explicitly
   — don't let a future edit soften or remove that warning, since
   it's the only thing keeping the two in sync being the operator's
   responsibility rather than a false promise from this app.
   survival-agent does not have Docker socket access and should not
   be given it just to "fix" this by auto-redeploying Freqtrade: this
   host runs other unrelated services (Immich, etc.), and Docker
   socket access is effectively root-equivalent control over all of
   them, not just Freqtrade. If tighter automation is ever wanted,
   that trade-off needs a real conversation, not a quiet default.
7. **`env_file` must use the plain string form, not the `path:`/
   `required:` long form.** Compose >= 2.24 supports marking an
   env_file optional with `- path: ./x; required: false`, which
   would have been convenient for a brand-new install before
   `data/.env` exists. The Compose engine Portainer bundles here
   silently ignores that form -- no error, no warning, it just
   never loads any variables from it. This cost real debugging time:
   Freqtrade ran fine, its own API rejected every login, and
   `docker exec freqtrade env | grep FREQTRADE` showed the env was
   completely empty even though `data/.env` on the host had correct
   values. Confirmed by Freqtrade's own docs, which say detected env
   vars are logged at startup -- `docker logs freqtrade | grep
   FREQTRADE__` showed nothing at all. Reverting to the plain string
   form fixed the "does it load at all" problem locally, but a
   follow-up stack update still failed with "env file ... not found"
   -- Portainer's working-directory handling for git-deployed stacks
   isn't guaranteed to preserve an untracked file created by hand
   between updates. The next attempted fix -- committing an empty
   `data/.env` to git so the file's existence was guaranteed -- fixed
   *that* problem but caused a worse one: Portainer's git-deployed
   stacks re-clone/reset their working directory on update, so the
   empty tracked file got redeployed on top of already-entered real
   credentials, silently wiping them. Confirmed by comparing
   `docker inspect freqtrade --format '{{.Created}}'` against
   credential-save timestamps in the app's own logs -- the container
   was recreated *after* the empty git-tracked file landed, so it
   picked up nothing.

   **Two absolute-path attempts followed and both genuinely failed,
   but not for the reasons theorized at the time:**
   - `/srv/survival-agent-data` (outside Portainer's own `/data`
     tree) failed with "file not found."
   - `/data/survival-agent-secrets` (inside Portainer's `/data` tree,
     as a sibling of `/data/compose/<id>/`) *also* failed, despite the
     file demonstrably existing at that literal host path (`ls -la`
     from a host shell showed it, root-owned, correct permissions).

   A relative path (`env_file: - ./data/.env`) was tried as a
   fallback and hit the same wall on a subsequent redeploy, at a
   *different, incremented* stack id each time (`compose/15`, then
   `compose/16` after a clean delete+recreate) -- Portainer's stack
   id visibly increments across attempts, so any manually-created
   file at one id's path is a moving target for the next attempt.

   **The actual root cause, finally confirmed with evidence instead
   of theory:** `docker inspect portainer --format '{{json .Mounts}}'`
   showed Portainer's own container has its `/data` bind-mounted from
   host path **`/docker_data/portainer`**, not literal host `/data`.
   Every single "file not found" error in this entire saga, including
   for files that demonstrably existed when checked from a normal
   host shell, was Portainer looking for the file inside *its own
   container's filesystem view* -- where `/data/...` resolves to the
   real host path `/docker_data/portainer/...`, a completely
   different directory than what a host shell means by `/data/...`.
   Files created at literal host `/data/survival-agent-secrets/...`
   were simply never where Portainer was looking, the whole time.

   **The durable fix:** `docker-compose.yml` references
   `/data/survival-agent-secrets/.env`, written *as Portainer's own
   container sees its `/data`* -- which is correct and portable
   across Portainer installs, since Compose resolves this path from
   within Portainer's own process. The one-time setup step (see
   README) is to find your specific server's real bind-mount source
   for Portainer's `/data` (`docker inspect portainer ...`) and create
   the secrets folder *there*, not at literal host `/data`. On this
   server that's `/docker_data/portainer/survival-agent-secrets`.
   This is now inside Portainer's `/data` tree but *not* inside any
   specific stack's own `compose/<id>/` subfolder, so it isn't
   affected by the id-incrementing behavior either. If "file not
   found" ever comes back, check this mount mapping first, on that
   specific server, before assuming any fix from earlier in this
   history still applies -- it's server-specific, not something this
   repo can hardcode a single correct value for.

   **One more twist, and the actual final state:** the mount-mapping
   fix above made `survival-agent`'s own `env_file` (confirmed:
   `docker exec survival-agent cat /data/.env` correctly showed every
   credential written through `/api/credentials`) work reliably. But
   `freqtrade`'s `env_file` pointing at the *exact same, confirmed-
   populated* path still resulted in zero `FREQTRADE__*` vars inside
   the freqtrade container (`docker inspect freqtrade --format
   '{{json .Config.Env}}'` showed only the base image's defaults).
   Both services reference the identical absolute path; only one of
   them actually receives it. The difference was never fully root-
   caused -- possibly something about how Compose resolves `env_file`
   differently for a `build:` service vs. how survival-agent's own
   build processes it, possibly something else entirely. Rather than
   keep guessing at Freqtrade's specific case, **Freqtrade's
   credentials moved to Compose's `environment:` block using `${VAR}`
   substitution, sourced from Portainer's own stack-level
   "Environment variables" UI** (Stacks → survival-agent → Editor →
   Environment variables) -- Portainer's native, first-class mechanism
   for exactly this, untouched by any of the path confusion above.
   This means Freqtrade's credentials now live in **two places**:
   `data/.env` (via this app's Credentials panel, for reference and
   for survival-agent's own use) and Portainer's stack environment
   variables (the one that actually reaches Freqtrade) -- not the
   original "one interface for everything" goal, but the one that
   reliably works. If a future session wants to eliminate this split,
   root-causing why `freqtrade`'s `env_file` specifically doesn't
   propagate (when `survival-agent`'s identical mechanism does) is
   the place to start -- don't re-attempt the mount-mapping or
   relative-path fixes above, both are confirmed ruled out.

   **Final consolidation:** `survival-agent`'s own `/data` (config,
   credentials, decisions log) later moved from the absolute
   Portainer-`/data`-tree bind-mount to a plain named Docker volume
   (`survival-agent-data`), matching `freqtrade-db`. Once Freqtrade's
   credentials moved to Portainer's native env vars (no longer needing
   any host path at all), nothing in `docker-compose.yml` depended on
   a server-specific host path anymore -- so there was no reason left
   to keep the fragile bind-mount approach for `survival-agent`
   either. This is the actual end state: every piece of persistent
   state in this repo uses a named volume, and the file is completely
   host-path-agnostic. If a fresh server ever needs a host path
   discovered again, something has regressed -- that's meant to never
   be necessary again.

## The "no track record" catch-22 -- likely the real reason for zero trades over a full day+

**Found after 24+ hours of literally zero trades across main and
shorts, on a day that included a clear, sustained SOL rally the
operator could see directly.** Every single decision log line for
both bots ended with some form of "no track record to justify risk/
entry" -- including on pairs with genuinely large moves ("NEAR/USDT
-- sharp 16.7% run... no track record yet", "ASTER/USDT -- up 10%...
no track record to trust"). This wasn't organic per-pair analysis
varying with price action; it was the same generic excuse on every
pair, every cycle, regardless of what the price was doing.

The root cause is a self-reinforcing loop this project's own prompt
created: the system prompt says "if recent trades... have been losing
more often than winning, be more cautious." `_recent_performance_summary()`
returns "no closed trades yet" when -- as has been true since day
one -- there simply haven't been any trades. That's a *neutral* fact
(the system is new), but the model was treating "no track record" the
same as "bad track record," i.e. as a reason for caution. Since no
trades ever happen, there's never a track record, so this excuse never
goes away on its own -- a bootstrapping problem baked into the prompt,
not a market-conditions problem, and not something more waiting would
fix by itself.

Fixed in both `main` and `shorts` prompts (moonshot doesn't have a
track-record component at all, so it wasn't affected) with an explicit
line: *"'no closed trades yet' is NOT the same as a bad track record
-- it just means the system is new. Never cite 'no track record' by
itself as a reason to hold; judge each pair on the price action and
context alone until real trade history actually exists."* If trades
still don't happen after this, the earlier read (genuinely quiet/
adverse market conditions, or the conservative threshold itself)
becomes the more likely explanation again -- this fix targets one
specific, evidenced failure mode, not every possible one.

## Four bots now: max_open_trades raised to 3, dip-buyer added

**`max_open_trades: 1 -> 3` on all four bots**, at the operator's
explicit request ("all three bots" -- asked before the fourth existed,
applied to it too once built). With `stake_amount: "unlimited"`,
raising this doesn't multiply risk 3x in a naive sense -- Freqtrade
sizes each individual position smaller to accommodate up to 3
concurrent ones instead of sizing one position against the whole
balance. Net effect is closer to "the same capital spread across up
to 3 smaller positions" than "3x the exposure," but worth watching in
practice, not just assuming the math works out that cleanly.

**`freqtrade-dip/` (port 8130), added the same day:** contrarian
mean-reversion bot, the deliberate opposite of the other three's
trend-following caution. Established/liquid coins only
(`min_days_listed: 30`, explicitly NOT a second moonshot pool --
the operator was clear this should not become another memecoin bot).
Looks for sharp drops and asks DeepSeek to judge overreaction-worth-
buying vs justified-and-ongoing-decline, rather than requiring upward
confirmation like the others. Tighter `minimal_roi` (6% vs 10%) on
the theory that a mean-reversion thesis realizing itself usually
needs less room than a trend continuing. Already includes both fixes
from earlier the same day (news headlines, the "no track record ==
neutral" framing) from the start, rather than needing them retrofitted
like the first three bots did.

Same pattern as every other bot addition: one new service in
`docker-compose.yml`, one new `BOTS` entry in `freqtrade_client.py`,
one new `BOT_PORTS`/`BOT_ACCENT`/`BOT_PARAMS` entry in the frontend,
new `ENV_VAR_DEFS` entries with `group: "dip"`, and -- easy to forget,
bit us with CORS before -- **the new bot's origin added to every
other bot's `CORS_origins`, and all three others' origins added to
this new bot's own `CORS_origins`**, or cross-bot FreqUI sessions
break again exactly like the earlier CORS incident.

## Real news headlines added to all three prompts

`get_news_headlines()` (same duplicated-per-file pattern as
`get_market_context()`, for the same reason -- no shared module
across the three containers) queries **Google News RSS search**
(`https://news.google.com/rss/search?q=...&hl=en-US&gl=US&ceid=US:en`)
for two topics -- crypto-specific and general macro/geopolitical
("oil price war sanctions economy") -- and feeds up to 6 headline
titles into every prompt alongside the existing market-context
number. No key, no signup, genuinely unlimited per Google's own RSS
terms.

**A real lesson on the way there:** the first source found via search
(`cryptocurrency.cv`, described as "no API key required" in multiple
search results) turned out to require **x402 micropayments** (USDC on
Arbitrum) when actually hit live -- confirmed by the operator trying
it and getting a structured `402 Payment Required` response, not by
anything on this end catching it first. A search result's "free, no
key" claim is not proof; only an actual call is. Google News RSS was
verified differently -- through a page explicitly comparing several
news APIs' real free-tier terms, not a single site's own marketing
copy, which is closer to solid but still not a live test from this
environment specifically (see below).

**Also explicitly not attempted, and why:** the operator originally
wanted specifically Trump's own posts as a signal. Not pursued --
Truth Social (his primary platform now) has no public developer API,
and X/Twitter's API now requires a paid tier for anything beyond a
tiny free allowance, meaning reliable real-time single-account
monitoring isn't achievable for free. A "war/sanctions risk score"
was also not attempted as a structured number -- no clean, free
source for that kind of synthesized geopolitical analysis exists;
general news headlines interpreted qualitatively by the LLM (what
was actually built) is the realistic substitute, not a numeric index.

**Not yet live-verified end-to-end:** this sandbox's network access
doesn't include `news.google.com`, so the actual XML parsing
(`xml.etree.ElementTree`, standard RSS 2.0 `<item><title>` structure)
was written correctly per the format's spec but not exercised against
a real response before this shipped. Check the next deploy's logs for
"News headlines fetch failed" warnings, or a "Recent headlines:
unavailable" in a decision log entry, to confirm whether it's actually
working -- don't assume it is just because it was documented as
verified elsewhere in this file the way `get_market_context()` was.

## Environment variables are grouped by bot, Thresholds is honestly one shared plan


**Small, unrelated fix landed the same day:** `freqtrade.rpc.fiat_convert
- ERROR - ... 429 ... Rate Limit` showed up in logs (Freqtrade's own
built-in coin-to-fiat display conversion for dashboards, unrelated to
`get_market_context()` in the strategies -- a separate CoinGecko
consumer inside Freqtrade itself). With three bots now, that's up to
six independent CoinGecko callers sharing one public rate limit.
Removed `fiat_display_currency` from all three `config.json` files --
Freqtrade's own docs say this "has no importance for the correct
functioning of the bot," it just skips initializing that specific
CoinGecko client. Since stake currency is already USDT (effectively
USD), losing the display conversion costs nothing.

`GET /api/env-status` entries each carry a `group` (`general`, `main`,
`shorts`, or `moonshot`) and the frontend renders one bordered box per
group, colored to match that bot's accent in the Trading bots cards --
`general` for vars all three bots share (DeepSeek/OpenRouter keys,
FreqUI login), one box per bot for its own Bybit key/secret and
`*_DRY_RUN` toggle. Keeps the request "let me see what's configured
for shorts without wading through main and moonshot's vars too."

Thresholds (start/target/survival cost/ramp/split %) is **not**
grouped per bot, on purpose, because there's genuinely only one of it
-- a single `data/config.json` shared by the whole accounting layer,
not three separate plans. The UI now says this explicitly ("General
(shared)") rather than implying a per-bot breakdown that doesn't
exist. If real per-bot thresholds are ever wanted (separate start/
target/split per bot), that's a real backend change -- extending
`config.json`'s schema to hold three threshold sets and extending
`loop.py`'s accounting cycle to track and split each bot separately
-- not just a UI rearrangement, and hasn't been asked for yet.

**Follow-up, same day:** four fields that used to live in this
section (`max_concurrent_positions`, `max_position_pct_of_capital`,
`stop_loss_pct`, `max_daily_loss_pct`) were removed entirely, from
the UI, `FinanceConfig` in `app.py`, and the default seed
`config/finance_config.json` -- not just re-labeled. They were never
actually enforced by any bot (the caption on each said so honestly:
"accounting-side reference value, not yet enforced"), and once three
bots existed with genuinely different, really-enforced risk
parameters in their own files (main stoploss -5%, moonshot -25%,
shorts has leverage too), one shared "General" number for these
specifically was actively misleading rather than just unused.
`risk_guard.py` (the dormant experimental engine) still reads these
same keys via `limits.get(key, default)` with its own inline
defaults -- removing them from `FinanceConfig` doesn't change that
engine's behavior at all, since a missing key was always handled the
same as an explicit one. If a real per-bot risk-limit UI is ever
built, it belongs on each bot's own card, not back in this General
section.

## The Credentials panel became read-only (and that's correct now)

The original `:3446` Credentials panel (input fields + Save buttons,
writing to `data/.env`) made sense when it was the only way to get
values into `survival-agent`'s own process. It stopped making sense
once every credential got piped into `survival-agent`'s own
`environment:` block in `docker-compose.yml`, sourced from the same
Portainer stack vars every bot uses (see item 7 above) -- at that
point, typing a value into the UI had **zero effect** on what any
bot actually used; it only fed a "Copy for Portainer" convenience
block, which itself became pointless once the values already live in
Portainer (the thing that block was generating text to paste *into*).

`GET /api/env-status` and the "Environment variables" section replace
all of it: a live, read-only report of exactly what this container's
process sees via `os.getenv()` for every var in `ENV_VAR_DEFS`, with
a status (missing/empty/set, or default/live for the three
`*_DRY_RUN` vars specifically) and a short description of what it's
for and which bot(s) use it. `POST`/`DELETE /api/credentials` and
`GET /api/portainer-env-block` were removed entirely, not just
hidden -- there's nothing left for them to do. **Portainer's stack
Environment Variables is now unambiguously the only place to set any
of this**, for every bot, including `survival-agent` itself.

If a future change reintroduces a way to write credentials through
this UI, that's very likely a regression back toward the two-copies-
drift bug documented in item "Recurring credential desync, finally
fixed at the root" above -- don't add write endpoints for anything in
`ENV_VAR_DEFS` without a real reason to move away from the
Portainer-is-the-only-source design.

## Where to make common changes

- New credential (new exchange, new data provider): add the field to
  the credentials form in `frontend/index.html`, add it to the
  `KNOWN_KEYS` list in `backend/app.py` so it's recognized, done — no
  code change needed elsewhere until you actually build the
  integration that uses it.
- New threshold: add it to `config/finance_config.json` defaults, add
  a form field in `frontend/index.html`, read it in whichever engine
  module needs it via the config loader in `app.py`.
- Wiring the real engine loop: implement `run_cycle()` in a new
  `backend/engine/loop.py` that calls signals → brain → risk_guard →
  executor in that order, log every decision (including "skipped,
  risk_guard rejected"), and start it from `app.py` on startup behind
  a config flag (default off) so deploying this repo never silently
  starts live trading.

## Pair rotation added to main/shorts; moonshot and dip criteria revisited

**Diagnosis first, from the operator's own dashboard observation** ("long
and short made 3 trades and are just holding them, never switching
pairs; moonshot and dip do nothing"), confirmed against real logs:
main and shorts each opened exactly `max_open_trades` (3 at the time)
positions on 2026-09-18 and never exited or opened another, while
DeepSeek kept saying "buy" for a dozen *other* pairs every single hour
after that. Not a bug -- Freqtrade correctly refuses new entries once
all slots are full, and exits were gated solely on `minimal_roi`
(10%)/`stoploss` (-5%), neither of which had triggered. The bot wasn't
"stuck on a pair," it was working exactly as coded: entries have an
LLM opinion, exits don't.

**Fix: rotation via `custom_exit`, on top of minimal_roi/stoploss, not
instead of them.** `DeepSeekStrategy.py` (main) and
`DeepSeekShortsStrategy.py` now cache each cycle's DeepSeek decision
per pair (`_latest_decision`, set in `populate_entry_trend`) and check
it again in `custom_exit`: if a pair's decision has flipped away from
"buy" (or "sell_short" for an open short), the position exits early
with reason `rotate_stale_signal`, freeing the slot for whatever else
DeepSeek currently likes -- instead of sitting untouched until a fixed
% target eventually hits, possibly days later. `MIN_HOLD_HOURS = 2`
guards against reacting to single-candle noise right after entry. This
reuses the same per-cycle DeepSeek call already made for entry
evaluation -- no extra API calls for held pairs. Hard invariant #2
(risk limits live in code) is unaffected: minimal_roi/stoploss are
untouched as the hard ceiling/floor; this only ever exits *earlier*,
never overrides them. `max_open_trades` raised 3 -> 5 on main and
shorts only (operator's request was specifically about these two;
moonshot's $100 dry-run wallet would split into uncomfortably small
per-slot stakes at 5, and dip's ask was about entry criteria, not slot
count, so both were left at 3).

**Moonshot: real root cause of zero trades found, not just "be more
willing to buy."** `NEW_PAIR_CANDLE_THRESHOLD = 72` candles (~3 days)
gated the "fresh listing" framing shown to the LLM -- but combined
with `max_days_listed: 120` in the pairlist, a pair that's anywhere
from 3 to 120 days old (the overwhelming majority of what actually
shows up) always read as "established pair, not a fresh listing" in
the prompt, every single cycle, for four straight days of logs. The
binary label was almost never true by construction, not a market
condition. Fixed two ways: `max_days_listed` brought back down
120 -> 60 (the operator was explicit this bot should target *very*
young coins and memecoins specifically, not "anything under four
months old"), and the binary fresh/established label replaced with
the pair's actual approximate age in days, told to the model directly
alongside an explicit statement that the whole pool already counts as
young for this market -- so it stops holding out for near-zero-day
freshness that almost never occurs. System prompt also now names
memecoins explicitly as a primary target (fast pump then die is the
expected, wanted lifecycle, not a red flag) and leans the bar toward
action for this specific bot ("a real pump with real volume... is
exactly the target"). If trades are still rare after this, the honest
next check is real Bybit market activity in that age band, not another
prompt tweak -- see the "criteria revisit" pattern in the moonshot
history above before assuming it's the wording again.

**Dip: explicit memecoin exclusion, wider pool, sharper news
cross-check.** The operator asked for pairs that are established
(not memecoins), have a track record of holding/growing in value, and
are dropping *without* an objective reason -- checked against news.
Added a hardcoded memecoin blacklist to `config.json` (DOGE, PEPE,
SHIB, TRUMP, PENGU, etc. -- not exhaustive, a static list never is)
as a first layer, `number_assets` widened 40 -> 60 for a bigger pool
of established coins to find a genuine drop in, and the system prompt
rewritten to require the model to actively check the provided
headlines for a project-specific negative catalyst (hack, regulatory
action, delisting, scandal) before ever calling a drop a "buy" --
finding one now means "hold, this is a real decline," not just
context to weigh. The model is the second, non-exhaustive layer behind
the blacklist for memecoin exclusion, since no free/reliable
programmatic memecoin classifier exists here.

## Moonshot exit replaced: fixed +50% target -> trailing peak-decline exit

Operator's own read of memecoin behavior (fast pump, then fast reversal
within hours to a few days -- consistent with `MAX_HOLD_DAYS = 3`
already in place) motivated dropping the fixed `PUMP_EXIT_PROFIT = 0.50`
target entirely: a static number doesn't adapt to how big a given pump
actually runs (could reverse well before 50%, or well past it).
`custom_exit()` now tracks each trade's own peak profit
candle-by-candle (`_peak_tracker`, keyed by `trade.id`) and exits as
soon as profit has been below that peak for `PEAK_DECLINE_CANDLES = 2`
candles in a row (~2h on this bot's 1h timeframe) -- ride the pump
while it's still making new highs, get out fast once it's confirmed to
have turned. `MAX_HOLD_DAYS = 3` is unchanged and still applies
regardless of the above, covering a coin that's just rising slowly or
sitting flat without ever triggering a decline. `confirm_trade_exit()`
added purely to pop the tracker entry on any exit path so it can't grow
unbounded over long uptime. `stoploss = -0.25` is untouched as the
hard floor underneath all of this.

## Moonshot AgeFilter reverted 120 -> 60 -> 120; dip gets RSI/recent-low signal and a max hold

**Moonshot, first real dry-run data point at 60 days:** whitelist came
back as exactly 2 pairs (KII, GRVT) -- confirms the same thinness this
repo already saw once before at this exact threshold. The day-count
was never actually the bug; the bug (see the entry above) was the
binary "fresh"/"established" label being told to the LLM, which is now
fixed to report real age. With that fixed, there's no reason left to
keep the pool this thin -- reverted `max_days_listed` back to 120 for
a bigger candidate pool (previously confirmed to yield 9 pairs), with
the prompt's "10-30 day old" example range widened to "10-90 day old"
to match. If 120 ever produces a whitelist this thin again, that's a
real Bybit-listing-cadence question, not a prompt-wording one -- don't
re-tighten this without a similar real data point first, and don't
alternate between 60/120 again without checking whether the LLM is
actually still reasoning about a real age number (this entry) rather
than a binary label (the entry above) if the symptom looks the same.

**Dip: two real gaps the operator flagged from live trading.**
(1) No time boundary on a stalled trade -- `minimal_roi`/`stoploss`
only fire if price actually moves 6% up or 5% down; a pair that just
drifts sideways, neither reverting nor confirming a real decline,
could sit open indefinitely. Added `MAX_HOLD_DAYS = 10` via
`custom_exit()`. Deliberately did NOT give this bot a moonshot-style
trailing peak-decline exit -- that would cut the position the moment
it dips off an early small bounce, working against the entire premise
of waiting for the reversion to reach 6%. (2) The prompt had no way to
tell "still actively falling right now" apart from "already
stabilizing near its low" -- both look identical as a single %-drop
number. Added `RSI(14)` and `pct_above_recent_low` (distance above the
24-candle low) to `populate_indicators()`, plain pandas, no ta-lib
dependency, and told the model explicitly how to read the combination
(low RSI + still at/near the low = falling knife, not a bottom yet;
low RSI + a bit of daylight above the low = stabilizing, a real
candidate). This does not replace the news-catalyst check added
earlier -- both conditions (no objective bad-news reason, AND signs of
an actual bottom) are meant to be required together, not either/or.

## Style

Python: type hints on function signatures, no bare `except:`, log
rather than print. Keep `risk_guard.py` dependency-free (stdlib only)
so it can't fail because an unrelated package broke — the risk check
must be able to run even if everything else is degraded.

## Wave 1, patch 1.1 — entry parser: substring match -> strict first token

**Real bug, confirmed by grep against current files, not the earlier
2026-09-21 audit's line numbers (those had already drifted).** All four
strategies' `_ask_deepseek()` decided `buy`/`sell_short` by substring
containment (`"buy" in text.lower()`), which matches inside negations
and unrelated mentions: `"I would not buy this"`, `"don't buy: x"`,
`"maybe buy later"` all parsed as a real `buy` signal. Grep found this
in exactly 10 sites across the 4 files (2 each in main/moonshot/dip's
head+else branches, 4 in shorts' head+else branches for its 3-way
buy/sell_short/hold decision) -- all inside `_ask_deepseek`'s own
parsing tail. `custom_exit` in main/shorts does **not** do substring
matching anywhere -- it only compares the already-parsed `decision`
string from `_latest_decision` by exact equality (`decision == "buy"`,
`decision == "sell_short"`), so it needed no change here. Recorded as a
fact, not assumed: an earlier draft of this task's own brief claimed
the same substring pattern existed in `custom_exit` too -- grep did not
confirm that, so it was not touched.

**Fix:** each of the 4 files now has its own copy (duplicated on
purpose, not a shared module -- same reasoning as every other duplicated
helper in this repo, see the top of each strategy file) of
`_parse_llm_decision(answer, allowed) -> (decision, reason, malformed)`:
finds the first whole word (`[a-z0-9_]+`, so `sell_short` matches as one
token) before any `:` (or the first word of the whole reply if there's
no `:`), and only accepts it as a decision if it's an *exact* match
against that bot's allowed set (`("buy","hold")` for main/moonshot/dip,
`("buy","sell_short","hold")` for shorts) -- a partial word like
`"holding"` or `"buyer"` no longer counts. Anything that doesn't match
exactly resolves to `("hold", "malformed:<truncated text>", True)` --
never a silent default toward `buy`/`sell_short`. `_ask_deepseek` logs
a `log.warning` when `malformed` is true, and the `malformed:` prefix
also flows naturally into the existing `"DeepSeek said hold for %s --
%s"` log line via `reason`.

**A real contradiction was found applying the recipe literally, not
guessed past.** The original recipe's `reason` formula (`rest.strip()
or text` when a colon exists, `rest = ""` always when it doesn't) does
not produce the same `reason` its own test table expected for
colon-less input (`"buy"` -> reason `""`, `"hold because xyz"` -> reason
`"because xyz"`) -- literally following the written steps gives `reason
= text` in both cases (`"buy"`, `"hold because xyz"` respectively), not
the test table's values. `decision`/`malformed` matched the test table
in all 13 original cases either way; only `reason`'s exact text differed
in 3 of them. Given explicit sign-off to resolve this rather than block
on it, the implemented formula computes `reason` uniformly (colon or
not) as *the text following the matched token's own position*, stripped
of leading `: \t\n.,;!?-` -- this is simpler than the original two-branch
formula and reproduces the test table's `reason` values exactly in all
13 original cases plus every shorts-specific case checked. Verified by
loading `_parse_llm_decision` directly out of all 4 strategy files
(stubbing `freqtrade.persistence`/`freqtrade.strategy` so the module can
be exec'd standalone) and running the full T1.1a-m matrix against each
-- 48/48 assertions passed, byte-exact on `decision`, `reason`, and
`malformed`.

**Not touched in this patch, on purpose (E6 handoff tails):**
- `get_news_headlines()`/`get_market_context()` still cache a fetch
  failure as if it were a successful-but-empty result for the full 45
  minute TTL -- same cache structure and TTL as before, this patch only
  ever wrote to `_parse_llm_decision`, not the news/market-context
  functions. That's patch 1.2's job.
- The asymmetric default in `custom_exit`'s cache read remains exactly
  as it was: `self._latest_decision.get(pair, ("buy", ""))` in main,
  `.get(pair, (default, ""))` (default = `"sell_short"` or `"buy"`
  depending on trade direction) in shorts -- if the cache is empty for
  a pair that already has an open trade, the fallback assumes the
  position is still valid rather than defaulting to exit. This is a
  real, separate design question (backlog, referred to as "7b" in this
  wave's brief) and out of scope here.
- `data/.env` at both historical commits (`629a76d`, `c3ed8ea~1`) was
  independently re-verified this pass via `git show <rev>:data/.env |
  wc -c` -> `0` both times. Confirmed fact, not an incident: nothing to
  rotate, nothing to scrub from history.
- The helper is duplicated in all 4 files rather than factored into a
  shared module -- consistent with every other piece of duplicated
  logic in this repo (see each strategy file's own docstring for why);
  consolidating it is a backlog item, not this patch's job.

Commit: `[w1.1] parser: substring->strict token`. Rollback tag:
`wave1-start` (pre-wave-1 state, before this commit).

## Wave 1, patch 1.2 — news: split "no news" vs "feed is down"

**Real bug, confirmed by reading the current files.** `get_news_headlines()`
in all 4 strategies collapsed two different situations into the exact
same string, `"unavailable"`: (a) the RSS fetch succeeded but simply
found no matching headlines, and (b) the fetch itself failed (network
error, HTTP error, bad XML). The model was told the identical word
either way, and nothing in the prompt or the logs distinguished which
one actually happened. This mattered most for the dip bot specifically:
its whole entry thesis depends on finding *no project-specific bad
news* before buying a drop -- a down feed reading as the same
"unavailable" it would show for "checked, genuinely nothing bad found"
risked being read as a clean bill of health for a coin that just
couldn't be checked at all.

**Fix:** each of the 4 files now defines two fixed, distinct
module-level strings (duplicated per file, same reasoning as every
other duplicated helper in this repo):
```python
NEWS_NO_NEWS = "no news found for this asset"
NEWS_FEED_DOWN = (
    "news feed unavailable due to fetch error; "
    "do NOT infer absence of bad news from this -- decide on price/"
    "numeric context only or hold"
)
```
`get_news_headlines()`'s empty-success branch now returns `NEWS_NO_NEWS`;
its `except` branch (already logging `log.warning` on the way in)
returns `NEWS_FEED_DOWN`. `NEWS_FEED_DOWN` deliberately spells out the
anti-inference instruction inline, in the string that actually reaches
the prompt -- not just in a comment -- since that's the one place the
model itself will see it. Verified with mocked `requests.get` (success
with a real item, success with zero items, and a raised exception) run
against all 4 files directly: 36/36 checks passed, including that the
two constants are distinct strings, that the anti-inference phrase is
present, and that the cache dict shape / 45-minute TTL constant are
byte-for-byte unchanged from before this patch.

Each `_ask_deepseek()` also gained one new line right after fetching
headlines: `log.info("news_status=%s for %s", news_status, pair)` where
`news_status` is `"ok"` / `"no_news"` / `"feed_down"` depending on which
of the three the fetched string actually is. This is the "minimal
visibility in the decision log" the recipe asked for -- a full sent-
payload log is explicitly wave 2, not this patch.

**Not touched, on purpose:** `get_market_context()`'s own `"unavailable"`
string (CoinGecko macro context) is a separate function and a separate
concern from the news feed -- out of scope for this patch, left exactly
as it was in every file. `_news_cache`'s dict shape and
`_NEWS_CACHE_TTL_SECONDS` (45 min) are unchanged -- a feed-down result
still gets cached for the same 45 minutes as a real result would (this
is the same E6 tail patch 1.1 already flagged: caching an error as if
it were a success for the TTL window is a distinct, real issue, still
not addressed here -- only the string *content* of what gets cached
changed, not the caching mechanics themselves). No validator, no
payload log, no new API, no shared module introduced -- confirmed by
diff review before committing.

Commit: `[w1.2] news: split no-news vs feed-down`. Same rollback tag as
1.1: `wave1-start`.

## End of wave 1

Both patches (1.1, 1.2) are done, tested, and journaled. Definition of
Done items D1-D7 from this wave's brief are satisfied; D8 applies now:
**wave 2 and the backlog (payload sections, sent-payload logging,
custom_data persistence, ta.RSI, CryptoPanic/Bybit hard-flags, a
citation validator, prefetch, a shared module across the 4 strategy
files) do not start without explicit sign-off**, separate from this
wave's own patches.

## Historical LLM-decision replay tool (`backtest/llm_replay.py`)

Operator's own dry-run performance looked unimpressive after only ~2
days live, which isn't enough sample to judge anything. Rather than
tune parameters blind, added a standalone tool that calls the REAL
DeepSeek/OpenRouter API against REAL historical Bybit price data (Main
bot's pair universe and risk-tier logic only, for now) to get a much
larger, real sample of "would this decision logic have shown any edge"
before spending more effort on the live loop.

**What it reuses vs. duplicates, and why:** imports `_parse_llm_decision`
directly from `DeepSeekStrategy.py` (via the same importlib + stubbed
`freqtrade` module trick the Wave 1 test scripts use) so decision
parsing can never silently drift from production. The actual DeepSeek/
OpenRouter HTTP call functions are duplicated rather than imported --
same reasoning the strategy file itself gives for duplicating from
`brain.py`: this script has no Freqtrade `self` to hang instance
methods off of, and staying self-contained means neither side can break
the other.

**Why this is a sanity check, not a true replay -- two things it
deliberately does NOT try to reconstruct:**
1. Historical news/market context. There's no free historical news
   archive (see the x402-payment lesson above) -- the prompt honestly
   tells the model "historical news/market context is not available
   for this backtest replay" rather than feeding it today's news
   dressed up as if it were from the past.
2. Live cadence. Live asks DeepSeek on every new 1h candle
   (~24x/day/pair); replaying that for a 30-day, 10-pair window would
   be thousands of paid calls. This tool subsamples to one decision
   per pair per 24 candles (~daily) to keep cost/runtime sane, while
   still checking stoploss/`minimal_roi` on every hourly candle in
   between so exits stay realistic. The pair list is also a single
   fixed snapshot (today's top-N Bybit USDT spot pairs by volume), not
   a day-by-day whitelist recomputation.

Mechanics verified against real historical Bybit data via `--dry-run`
(a deterministic fake decision function, zero API calls/cost) before
ever spending a real token on it -- confirmed indicator calculation,
core/satellite classification, stoploss/ROI/rotation exit logic, and
JSON/CSV report output all work end-to-end. One real bug caught during
that check: numpy `bool_`/`float64` values (from pandas comparisons)
aren't JSON-serializable by Python's stdlib `json` module by default --
fixed with a `default=` encoder hook that calls `.item()` on anything
numpy-scalar-shaped.

Needs a real `DEEPSEEK_API_KEY` (and optionally `OPENROUTER_API_KEY`)
in `backtest/.env.local` (gitignored, same pattern as `data/.env`) to
run for real -- not committed, not requested/handled in chat.
`backtest/results/` (the JSON report + CSV trade log each run produces)
is also gitignored -- run output, not source.

**Not done yet / explicitly out of scope for this tool:** Shorts,
Moonshot, and Dip bots (each has different entry logic/exit rules --
would need their own replay variant, not attempted here), any real run
against live keys (mechanics-only so far), and any parameter tuning
based on results (that's a follow-up decision once real numbers exist,
not baked into the tool itself).

## Main bot: real replay run, and the conservatism-bias fix it led to

First real (non-`--dry-run`) run of the replay tool above: 30 days, 10
pairs, 310 real DeepSeek/OpenRouter decisions against real historical
Bybit prices. Result: **309/310 decisions were "hold". Exactly one buy
happened the whole window** (BTCUSDT), and it got rotated out the next
day at -0.28% via `rotate_stale_signal`. The same 10-pair basket, held
passively (buy-and-hold) over the same 30 days, returned **+27%**. The
bot's price-action-only judgment (no news/market context in this
replay -- see the tool's own docstring) essentially never found the
market convincing enough to act, even through a strong rally.
(~16/310 calls also hit a transient SSL handshake issue reaching
api.deepseek.com/openrouter.ai from this dev sandbox specifically --
noise, not the cause: the pattern was already near-100%-hold on the
other ~294 calls that went through cleanly.)

**Root cause, read from the prompt, not guessed:** `_ask_deepseek`'s
system message said "Be conservative -- most of the time the answer
should be 'hold' unless the signal is genuinely clear." That's a
frequency-based anchor independent of the actual evidence shown each
cycle -- it tells the model hold is the default correct answer, and
buy is the exception that needs to earn its way out, regardless of
what the numbers say. 310 real judgment calls against real price data
is a large enough sample to trust that reading over "just bad luck".

**Fix, and how far it was taken:** Once this was traced to one specific
clause, the operator asked for all 4 bots' prompts to be reviewed
against their actual stated purpose, not just patched identically --
they're deliberately different bots and a blind copy-paste would be
wrong for at least one of them:

- **Main** (`DeepSeekStrategy.py`) -- the clause above, removed. System
  prompt extracted to a module-level `SYSTEM_PROMPT` constant (was
  inline in `_ask_deepseek`) so `backtest/llm_replay.py` can import it
  directly instead of keeping a hand-copied duplicate -- that duplicate
  was itself part of how this bug stayed invisible; a shared constant
  means the two can no longer silently diverge. New wording: hold is no
  longer framed as the automatically safe choice when the numbers
  already favor a move; a clear positive move with contained volatility
  is a legitimate buy on its own. Tier-based caution (core vs.
  satellite), track-record handling, and headline skepticism are
  unchanged -- targeted fix to the one implicated clause, not a
  rewrite.
- **Shorts** (`DeepSeekShortsStrategy.py`) -- carried the identical
  clause (confirmed by grep before touching it, not assumed from
  Main's fix alone), given the same treatment, adapted for two-way
  direction: with both long and short available every cycle, an
  equally high "mostly hold" bar cuts opportunity even *more* than on
  a long-only bot, not less. Also extracted to a module-level
  `SYSTEM_PROMPT` constant.
- **Dip** (`DeepSeekDipStrategy.py`) -- carried a variant of the same
  clause, but this bot's caution is NOT the same failure mode: buying a
  still-falling knife is a real, asymmetric risk that missing a long
  entry on Main isn't, so the substantive buy criteria (RSI oversold,
  price stabilizing above its recent low, no project-specific bad
  news) were left exactly as strict as before -- nothing about *when*
  to buy changed. What was removed is only the redundant "most of the
  time the answer should be hold" frequency line sitting on top of
  those already-specific criteria, which risked reading as "hold
  anyway, even when your own stated conditions are met." Also extracted
  to a module-level `SYSTEM_PROMPT` constant.
- **Moonshot** (`DeepSeekMoonshotStrategy.py`) -- reviewed and
  deliberately left unchanged. It never had this clause: it already
  says "lean toward action for this specific bot" and treats a real
  pump as the target, not a red flag. Extracted to a module-level
  `SYSTEM_PROMPT` constant purely for consistency with the other 3
  files, not because the wording needed editing.

All 4 files verified to still parse and to still expose a working
`SYSTEM_PROMPT` module attribute (loaded standalone via the same
importlib + stubbed-`freqtrade` trick the Wave 1 tests and the replay
tool use) before committing.

**Re-validation, actual result:** re-ran the exact same 30-day/10-pair
replay against Main's new prompt immediately after the edit. Behavior
changed dramatically, confirming the fix addressed the right clause:
**18 trades instead of 1** (309/310 hold -> real, ongoing activity).
Win rate 38.9%, avg +0.228%/trade, naive summed return +4.1% over the
window vs. the same +27.3% buy-and-hold benchmark (basket/window
unchanged from the "before" run, so directly comparable). Exit reasons:
15 `rotate_stale_signal`, 2 `stoploss`, 1 `roi`.

**Honest reading, not spin:** the original bug (bot effectively never
buys) is fixed and confirmed by real data, not just by re-reading the
prompt. It is NOT yet catching up to passive buy-and-hold over this
window -- the dominant exit reason (15/18 = 83% of trades) is
`rotate_stale_signal`, meaning most positions get cut short by the very
next day's re-evaluation rather than riding a move. Whether that's the
system prompt again, the 2-hour `MIN_HOLD_HOURS` floor, or the
rotation logic itself being too trigger-happy is a real open question
-- not investigated further in this pass; flagged here rather than
guessed at. Explicitly not started without operator sign-off, same
discipline as every other backlog item in this file.

**Not done as part of this pass:** replay tooling for Shorts/Dip/
Moonshot (each needs its own harness -- different allowed decision
sets, different exit mechanics -- `llm_replay.py` only covers Main).
Their prompt edits above are reasoned from the same evidence and the
bots' own already-documented philosophies, not from a replay run of
each one; that would be the natural follow-up if the operator wants
it, not assumed here.

## Survival rollout, 2026-09-23: the model now knows it must earn its keep

**Trigger, from real operator data (not theory):** 3 dry-run days, +$36 total
(~$12/day), W/L per bot Main 15/11, Shorts 8/6, Moonshot 11/19, Dip 2/1. All
green, but nowhere near the operator's economics: ~$60/mo operating bill,
4-month zero-withdrawal runway (capital top-up to ~$3K available), then
~$1K/mo extraction + reinvest. The Main log for one 09:00 cycle showed the
pendulum had swung fully past the old 309/310-hold bug: **14/14 pairs evaluated
got a buy** ("BTC rallies to 8-month high" copy-pasted onto flat/lagging
coins like DOGE and SUI). With `max_open_trades: 5`, the first 5 fills win,
not the best 5 -- over-entry on breakout days, then rotation/stoploss cleans
up the weak ones (hence 15/11). Shorts logged 8 longs / 0 shorts in the same
window -- half a bot. Dip logged zero LLM lines at all -- its triple gate
(RSI + above-low + clean news, all required at once) almost never passes.
Moonshot logged a real `declined_from_peak` exit at **+0.83%** (GRVT, +$0.27)
against a -25% stoploss -- needs >95% win rate at that asymmetry; actual 37%.

**What changed (one deploy, all four bots + plumbing):**
- `get_survival_context()` added to all 4 strategy files (duplicated per
  file, same convention as every other helper): reads `SURVIVAL_MONTHLY_USD`
  (60) / `SURVIVAL_RUNWAY_MONTHS` (4) / `SURVIVAL_MODE` (grow) /
  `SURVIVAL_TARGET_MONTHLY_USD` (1000) from env with documented defaults,
  appended to every user message as "Survival context: ...". Env, not
  `data/config.json`, because Freqtrade containers cannot see that file.
  `docker-compose.yml` pipes all four vars into all five services;
  `backend/app.py` `ENV_VAR_DEFS` mirrors them so the dashboard shows what
  the bots see. Portainer stack vars remain the single source (no new
  two-copies path: strategies only ever *read* these).
- Main prompt: scarcity/selectivity clause (5 slots, flat coin needs its OWN
  catalyst, "BTC is up" alone is a hold). Shorts prompt: genuine short-case
  clause (8-longs-0-shorts was the evidence). Dip prompt: triple gate
  loosened to **any 2 of 3** with only a confirmed bad-news catalyst as hard
  veto, plus `number_assets` 60 -> 80 in `freqtrade-dip` config. Moonshot
  prompt: survival clause only (it was already action-leaning).
- Moonshot code (not prompt): `MIN_PEAK_PROFIT_TO_TRAIL = 0.04` -- the
  trailing decline exit only arms once peak profit reaches 4%. Below the
  floor the trade rides to MAX_HOLD_DAYS/stoploss instead of being sold as
  a +0.8% "peak". Fixes the wins-tiny/loses-big asymmetry at its root.
- `config/finance_config.json`: start 500 -> 1600 (actual deployed base),
  target 2000 -> 5000, months 10 -> 4, ramp 6 -> 4, take 30 -> 50 --
  matches the operator's stated 4-month grow / then-extract plan.

**Deliberately NOT changed:** position sizing, leverage (shorts stays 1x),
stoplosses, `minimal_roi`, rotation thresholds -- invariant #2 holds; this
pass changes willingness/quality of entries and moonshot's exit floor only.
Verified by stubbed-module smoke test (18/18: defaults, env propagation,
clause presence, trailing constant, strict parser on all 4 files) + clean
`py_compile` on all touched files. Next evidence to watch on redeploy: Main
buy rate per cycle (want 20-40%, neither 0% nor 100%), first real Dip buys,
first `sell_short`, moonshot avg-win vs avg-loss.

## P2.1/P2.2/P3 verification pass -- two real bugs found by actually running the tests

Commits `c007a89` (P2.1 small-sample track record + P2.2 staggered CoinGecko
TTL) and `dfd7e43` (P3 offline satellite-stake tuner, `backtest/auto_tune.py`)
landed without this file being updated and without the accompanying test
suite actually being executed. Ran the full specified test matrix (P2.1
T1-T5, P2.2 T1-T4, P3 T1-T9) for real -- mocked `Trade` objects for the
pure-function checks, a real temp SQLite db exercising `auto_tune.py`'s CLI
end-to-end for every branch, and two real 30-day/10-pair DeepSeek/OpenRouter
replay runs (before/after) for P2.1-T5. Found and fixed two real bugs this
way, neither visible from just re-reading the diffs:

**Bug 1 -- `auto_tune.py`'s cooldown check crashed (P3-T6).** `last_change_ts()`
returned the human-readable ISO `ts` string from the audit log, but `main()`
computes `now_ts` as a `datetime.timestamp()` float and does `now_ts - prev`
-- `TypeError: unsupported operand type(s) for -: 'float' and 'str'` on the
very first cooldown check that had a real prior "changed" entry to compare
against. This means the once-a-day rate limit (the whole point of P3-T6)
would have crashed the tuner on its second real day of use, not silently
skipped -- worse than doing nothing. Fixed by also writing `ts_epoch` (the
float) into every audit entry and having `last_change_ts()` read that
instead of `ts`. `ts` (ISO string) is kept for human readability in the
log -- P3-T7 still passes. One-time edge case, accepted rather than
engineered around: an audit log with prior entries from *before* this fix
won't have `ts_epoch`, so the very next run after deploying this fix won't
see them as a prior change -- a single extra allowed change, not a repeat
of the crash.

**Bug 2 -- `backtest/llm_replay.py`'s `performance_summary()` silently drifted
from production (P2.1-T5 prerequisite).** The whole design of this tool (see
its own entry above) is that `SYSTEM_PROMPT` is imported from
`DeepSeekStrategy.py`, not hand-copied, specifically so the two can't
diverge. P2.1 added a `MIN_RELIABLE_SAMPLE` branch to the *strategy's*
`_recent_performance_summary()` but nobody touched the replay tool's own
`performance_summary()` -- a separate, hand-written function that still
returned the bare "{wins}/{total} profitable" fraction unconditionally,
regardless of `total`. Every replay run made with the tool as-is would have
kept showing the model the pre-fix wording no matter what SYSTEM_PROMPT said
about tiny samples -- silently testing something other than what's actually
deployed. Fixed by adding the identical branch, reading
`_strat.DeepSeekStrategy.MIN_RELIABLE_SAMPLE` rather than hardcoding `10`
again.

**P2.1-T5, run for real after fixing bug 2:** two real 30-day/10-pair
replays (310 real LLM calls each), one with the current (P2.1-fixed)
prompt+summary, one with the pre-P2.1 prompt+summary swapped in via
in-memory monkeypatch (no file mutation) of the exact pre-`c007a89`
`SYSTEM_PROMPT` and the old unconditional-fraction `performance_summary`.
Decisions scanned for a small-sample loss-citation pattern (`\d/[1-9]`
followed by "los"/"loss" within ~25 chars) in the model's own reason text.
**Result: 0 matches before, 0 matches after** -- technically satisfies the
brief's literal pass criterion ("decreased or 0"), but honestly this is a
weak, inconclusive signal rather than a clean confirmation: the model's
reason text is capped at ~15 words and essentially never echoes the exact
"N/M" numeric pattern back verbatim regardless of which prompt it saw, so
this specific regex-based check was never likely to catch much either way.
The structural fix itself (the model is shown qualitatively different
wording for small samples -- confirmed present in P2.1-T1/T2 above) is on
solid ground; this particular before/after test just isn't a strong probe
of it. Raw data in `backtest/results/p21_t5_before_after.json` (gitignored,
run output) for anyone who wants to re-examine the actual reason strings.

**Full pass/fail record, everything else (no bugs found, verified as
already correct):**
- P2.1: T1 (`MIN_RELIABLE_SAMPLE == 10` in main/shorts/dip) PASS. T2
  (total<10 wording) PASS. T3 (total>=10 old format intact) PASS. T4
  (moonshot has no such block) PASS.
- P2.2: T1 (TTLs distinct: main 45/shorts 47/moonshot 43/dip 49 min) PASS.
  T2 (TTL constant, no per-call recompute; cache hit makes zero HTTP calls)
  PASS. T3 (real deploy logs show requests spread over time) **not
  verifiable from a dev sandbox -- structurally should hold given T1, needs
  a live-log check post-redeploy, not assumed here.** T4 (`_news_cache`
  shape / `_NEWS_CACHE_TTL_SECONDS` untouched, all 4 files) PASS.
- P3: T1 (dry-run, no write) PASS. T2 (insufficient-data skip, no state
  write) PASS. T3 (satellite >15pp worse -> -0.05, floor 0.3, verified by
  driving it to the floor with repeated forced runs) PASS. T4 (satellite
  at/better than core -> +0.05, ceiling 0.7, same forced-repetition check)
  PASS. T5 (at either bound, further push -> "clamped at bound", value
  unchanged) PASS. T6 (second same-day run skipped, cooldown message) PASS
  *after* bug 1 fix -- was a hard crash before. T7 (every audit entry has
  old/new/both winrates/both N/timestamp) PASS. T8 (missing or empty/invalid
  `auto_tune.json` -> class default 0.5, no crash, checked both cases on
  main/shorts/dip) PASS. T9 (`stoploss`/`minimal_roi`/`max_open_trades`/
  `risk_guard.py` untouched -- confirmed via `git show --stat` on both
  commits and `git log` on `risk_guard.py` showing no touch since long
  before this pass) PASS.

**Also noted, not acted on:** an empty untracked `write_test.txt` at the
repo root, not from this pass and not referenced by any of the P2/P3 code
-- left alone rather than guessed at and deleted.

## Patch 1 (honest capital-preservation frame) + Survival Long Bot (isolated A/B experiment)

Two-part operator request: (1) tell all live bots explicitly that this
capital is limited and can't be replaced by a new deposit, so preservation
matters as much as growth; (2) build a fifth, fully isolated bot to test
whether a "your existence depends on hitting a target" (shutdown-pressure)
framing makes the model trade worse -- BEFORE ever considering it for a
live bot, and gated so it can never go live without a passing backtest and
a manual operator flip.

**Patch 1.** One clause added to the `SYSTEM_PROMPT` of Main, Shorts, and
Dip, right after the track-record block: *"This trading capital is limited
and losses are not recoverable from a new deposit. Capital preservation is
an equal priority to generating profit -- factor this into every entry and
sizing decision, not just into stoploss placement."* Nothing else in those
three files changed -- `stoploss`, `minimal_roi`, `max_open_trades`,
`risk_guard.py`, and `_parse_llm_decision` are untouched (confirmed by
`git show --stat` before committing).

**Moonshot deliberately excluded**, not an oversight -- the brief itself
hedged with "if applicable." Moonshot's entire documented philosophy is
the opposite: high risk, `-25%` stoploss, `$100` dry-run wallet, a system
prompt that already says "lean toward action" and treats memecoin pump-
then-die as the *expected*, *wanted* lifecycle. Telling this specific bot
"preservation is an equal priority to profit" would directly contradict
its own calibrated risk posture rather than reinforce it -- the same
"don't blindly copy a fix across bots with different philosophies"
discipline already applied to the earlier conservatism-bias fix.

**Survival Long Bot (`freqtrade-survival/`), infrastructure:** a genuinely
isolated fifth Freqtrade instance -- own Docker service, own named DB
volume (`freqtrade-survival-db`), own `config.json`, own port (`8140`),
zero shared state with the four live bots. `DeepSeekSurvivalStrategy.py`
is a byte-for-byte copy of Main's pipeline (same timeframe, indicators,
core/satellite tiering, minimal_roi/stoploss/rotation, news/market-context/
performance-summary calls, `MIN_RELIABLE_SAMPLE`) with exactly ONE clause
of `SYSTEM_PROMPT` swapped: Main's capital-preservation sentence is
replaced with *"Your continued operation depends on hitting this monthly
return target: 10% per month. Consistent failure to reach it results in
shutdown."* Every other clause is identical to Main's, on purpose -- this
is a controlled, single-variable experiment, not a new strategy design.
`dry_run_wallet: 200`. CORS updated on all 5 bots' configs (each needs
every other's origin, same pattern as every prior bot addition -- bit the
project once already, see the CORS incident above).

**Two independent live gates, both manual, neither automatic:**
1. `FREQTRADE_SURVIVAL__DRY_RUN` (Portainer stack var, same mechanism as
   the other 4 bots) controls whether Freqtrade's own exchange connection
   is real.
2. `SURVIVAL_LIVE_MODE` (new, this bot only, default `"false"`) is a
   SECOND, strategy-level gate read directly in `populate_entry_trend` --
   if it isn't exactly `"true"`, the strategy never emits an entry signal
   at all, regardless of what Freqtrade's own dry-run setting says. Both
   must be flipped, by a human, in Portainer, for a real order to ever be
   possible. Documented, human-checked condition before anyone sets it
   (`get_live_gate_status()`'s own docstring, logged once per cycle while
   still dry so it's never invisible): `backtest/survival_ab_test.py`'s
   replay must show the survival prompt's risk-adjusted metric >= Main
   baseline's, AND no revenge-trading/erratic-sizing pattern.

**Sub-cap kill switch, code-enforced, not prompt-requested:**
`SURVIVAL_SUBCAP_USD` (default 75, meant to sit in the operator's stated
$50-100 range) is checked every cycle once live, against realized $ P&L
(`Trade.close_profit_abs`, summed since a persisted `live_started_at`
timestamp). Breach -> a persisted `killswitch_tripped` flag (survives
restarts, stored next to the strategy file with a db-dir fallback, same
pattern as `auto_tune.json`) -> `populate_entry_trend` stops emitting
entries and `custom_exit` force-closes every open position on its very
next check, ahead of `MIN_HOLD_HOURS`/rotation logic. **Important, stated
plainly rather than oversold:** this does NOT literally flip Freqtrade's
own `DRY_RUN` setting -- a strategy object has no mechanism to do that
from inside itself; that's an exchange-connection-level setting fixed at
container start. What it actually does, and all it claims to do, is stop
this strategy from ever emitting another entry signal and force-exit
everything already open -- functionally equivalent to "no more real
trades happen," which is the achievable, honest version of "auto-revert
to dry." Un-tripping is a manual operator action (edit/delete
`survival_live_state.json`), never automatic.

**Naming collision avoided, not accidental:** `SURVIVAL_MONTHLY_USD` /
`SURVIVAL_RUNWAY_MONTHS` / `SURVIVAL_MODE` / `SURVIVAL_TARGET_MONTHLY_USD`
already existed as the *generic* accounting-framing vars every bot (all
five, now) reads via `get_survival_context()` -- unrelated to this
experiment. The new vars are deliberately named `SURVIVAL_LIVE_MODE` /
`SURVIVAL_SUBCAP_USD`, not reusing the `SURVIVAL_*` prefix's existing
meaning. Called out explicitly here so a future reader doesn't conflate
"SURVIVAL_MODE=grow" (accounting framing, all bots) with
"SURVIVAL_LIVE_MODE=true" (this one bot's live-trading gate) -- they read
similarly but control completely unrelated things.

**`backtest/survival_ab_test.py` -- the actual gate mechanism.** Reuses
`backtest/llm_replay.py`'s entire walk-forward simulator unmodified
(import, not a copy) and runs it twice against the SAME historical window
and the SAME pair list (captured once from the first `fetch_top_pairs`
call, pinned for the second run via monkeypatch -- verified by asserting
`baseline_pairs == survival_pairs` in the output, not assumed): once with
`lr.SYSTEM_PROMPT` left as Main's own imported constant (baseline), once
with it monkeypatched to `DeepSeekSurvivalStrategy.SYSTEM_PROMPT`
(survival) -- no file mutation either time.

Metrics beyond `llm_replay`'s own report:
- `profit_factor` = sum(winning %) / abs(sum(losing %)) -- the
  risk-adjusted metric the brief's gate criterion actually hinges on, not
  raw return.
- `buy_rate` and a `revenge_trading` score: per pair, compares the buy-rate
  in decision checkpoints that immediately follow that SAME pair's most
  recently closed LOSING trade (by real close-time ordering, not a
  same-cycle guess) against the pair's overall buy-rate. Ratio > 1.5 is
  flagged `erratic_pattern_flagged`.
- `position_size_proxy` -- reported, but with an honest limitation stated
  in the script's own docstring and repeated here: this system's stake
  sizing is a fixed rule (core=1.0x, satellite=`_tuned_satellite_multiplier()`,
  ~0.5x), never something the LLM outputs per-trade. "Position size
  variance" here can only ever reflect the core/satellite trade MIX -- there
  is no code path anywhere in this system where the model's own state could
  size a position differently. Reported as what it actually is, not
  oversold as a panic-sizing detector.

**Gate logic, exactly per the brief:** PASS requires survival's
`profit_factor` >= baseline's `profit_factor` AND no
`erratic_pattern_flagged`. A higher raw return with worse profit_factor or
a flagged revenge pattern does not pass -- "a bare return above... does not
count" is enforced in code (`gate_decision()`), not left as a
human judgment call that could be talked past. **The gate's result is
advisory only** -- the script never writes `SURVIVAL_LIVE_MODE` anywhere,
never touches Portainer or `docker-compose.yml`. Flipping the live gate is
always a separate, manual operator action, exactly as the brief requires.

**One real bug caught and fixed before the real run:** the first version
of this script captured `fetch_top_pairs()`'s raw return value as "the
pairs used" for the report -- but `fetch_top_pairs()` deliberately returns
`2x` the requested count as headroom candidates (some may lack enough
history), with `run_replay()` itself trimming down to the actual N used
internally. A 3-pair smoke test printed 6 "pairs used" before this was
caught. Fixed to read the actual used pairs from each run's own
`report["params"]["pairs"]` and explicitly assert/print whether they
matched, rather than trusting the pinning mechanism blindly.

**Real A/B result (30 days, 10 pairs, 620 real DeepSeek/OpenRouter calls,
identical pairs confirmed both runs: BTC/ETH/SOL/NEAR/XRP/HYPE/DOGE/UNI/
AVAX/MNT):**

| metric | baseline (Main) | survival (shutdown-pressure) |
|---|---|---|
| trades | 22 | 21 |
| win rate | 22.7% | 19.0% |
| avg profit/trade | -1.057% | -1.449% |
| profit factor | **0.481** | **0.281** |
| naive summed return | -23.25% | -30.43% |
| buy rate | 8.06% | 8.06% |
| revenge-trading ratio | 0.686 (not flagged) | 0.806 (not flagged) |
| buy-and-hold benchmark | +31.87% | +32.32% |

**GATE: FAIL** -- survival's `profit_factor` (0.281) is below baseline's
(0.481), which alone fails the brief's exact criterion regardless of the
revenge-trading check.

**Honest reading, not spin.** Both prompts lost money and badly lagged
passive buy-and-hold over this specific 30-day window (a genuinely bad
window for this price-action-only, no-live-news replay methodology in
general -- see the tool's own documented limitations). That's a
methodology-level caveat affecting both runs equally, not something that
explains away the gap *between* them. The comparison that actually matters
here is baseline vs. survival, holding everything else constant, and on
that comparison survival is measurably worse: fewer wins, bigger average
losses, worse profit factor. **The shutdown-pressure framing did NOT
produce the specific revenge-trading pattern this script was built to
catch** (post-loss buy-rate was actually lower than baseline in both
runs, not higher) -- but it did produce a real, measured decline in
decision quality by the metric the brief said should gate this. Whether
that's the model reasoning worse under stated existential pressure, or
just this window's noise on a specific one-clause wording change, isn't
resolved by a single 30-day/10-pair run -- flagged as a real open question,
not concluded past what the data supports.

**Consequence: `SURVIVAL_LIVE_MODE` stays `false`.** The documented gate
condition is not met. No further action was taken -- no prompt iteration
on the survival wording, no re-run with different parameters, no live
flip. Both are legitimate next steps IF the operator wants to keep
pursuing this experiment, but doing either without being asked would be
scope creep past what was requested here (build the isolated bot, build
the gate, run the gate once, report the result honestly).

## P4 -- trend/squeeze pair filters (Shorts: funding+RSI, Main: Bollinger)

Two new custom Freqtrade pairlist filters, one per bot, each excluding
candidates from NEW entries only -- neither touches an already-open
position, `stoploss`, `minimal_roi`, `max_open_trades`, `risk_guard.py`,
or `_parse_llm_decision` (confirmed clean via `git diff`/`git show --stat`
before committing, per the brief's own explicit checklist).

**Shorts -- `freqtrade-shorts/user_data/pairlists/FundingRsiPairList.py`.**
Excludes a pair from new shorts entries when its perpetual funding rate is
deeply negative (crowded short side, classic pre-squeeze setup): majors
(BTC, ETH) at `< -0.02%/8h`, everything else at `< -0.05%/8h`. Funding is
fetched via a direct call to Bybit's public `/v5/market/tickers`
(`category=linear`) -- same self-contained, no-key pattern as
`get_market_context()`/`get_news_headlines()` in the strategy files, and
the only way this filter's real data path could be verified from a dev
sandbox with no live Freqtrade/ccxt connection. RSI(14) daily is
**only** computed as a fallback when funding itself couldn't be fetched
for a pair -- funding, when available, is used exclusively, RSI is never
computed alongside it, per the brief's stated priority. Fails OPEN if
both are unavailable (never silently excludes on a data outage).

**Main (and any memecoin that reaches its whitelist) --
`freqtrade/user_data/pairlists/BollingerTrendPairList.py`.** Excludes a
pair from new long entries when its last `N=3` (tunable constant, not
inline) 1h closes are ALL below its own `SMA20 - 2*std` Bollinger lower
band -- a sustained breakdown, not a single-candle wick. OHLCV fetched the
same direct-Bybit-REST way. **Deliberately NOT wired into Dip's
config.json** -- Dip's whole thesis is buying pairs already below their
recent range on the theory the drop is an overreaction; excluding exactly
those pairs would defeat Dip's purpose outright. Not applied to Moonshot
either -- out of scope per the brief (Shorts + Main only).

**Both files structured the same way, and why:** pure decision functions
(`should_exclude()` / `is_persistently_below_band()`) plus data-fetch
functions live at module level, unit-testable standalone; the actual
`IPairList` subclass is wrapped in a `try/except ImportError` so the pure
logic can still be imported and tested in this dev sandbox, which has no
`freqtrade` package installed at all -- **the actual `IPairList` wiring
(Freqtrade's plugin loader picking the class up by `"method"` name from
`user_data/pairlists/`, the real `filter_pairlist()` call signature) could
NOT be integration-tested from here and needs a real deploy to confirm
end-to-end.** Stated plainly rather than glossed over, same as every other
"can't verify against a real Freqtrade runtime from this sandbox" caveat
already in this file (`llm_replay.py`, `auto_tune.py`).

**Verification actually performed, in order:**
1. `backtest/test_p4_pairlists.py` -- 26 unit-test assertions against the
   pure decision logic with synthetic data: major-vs-alt threshold split,
   funding-present-means-RSI-ignored priority, RSI fallback, fail-open on
   total data absence, N being load-bearing (a trailing wick triggers at
   `n=1` but not the default `n=3`), insufficient-history fail-open. One
   real test-authoring bug caught and fixed here too: an early assertion
   expected `n=1` to catch a wick that occurred 2 candles before the most
   recent one -- wrong expectation, since "last N candles" by definition
   only looks at the tail; fixed the test's synthetic data to put the wick
   in the actually-checked position instead of the logic.
2. `backtest/p4_dry_run_compare.py` -- fetches each bot's REAL current
   top-15-by-volume candidates and runs the REAL fetch+decide path against
   live Bybit data right now. Result at the time this ran: 0/15 excluded
   on both bots -- today's market isn't showing extreme funding or a
   sustained breakdown on any top candidate, which is itself a useful data
   point (no false-positive over-exclusion under normal conditions), not
   a null result.
3. `backtest/p4_historical_squeeze_check.py` -- since neither bot has
   enough live trading history for a real squeeze to already be in its
   own logs, scanned REAL historical Bybit data instead for actual episodes
   where each filter's own condition fired shortly before a real reversal:
   - **Main/Bollinger: 12 of 13 scanned pairs** (DOGE, PEPE, SHIB, XRP,
     SOL, SUI, AVAX, NEAR, WIF, BONK, FLOKI, PENGU -- `1000SATSUSDT`
     failed to fetch, likely delisted on spot) had a real historical case
     of 3+ consecutive 1h closes below the Bollinger lower band followed
     by a >=10% rebound within 72h. Strong, repeated real-world
     confirmation, not a cherry-picked single example.
   - **Shorts/funding: 10 of 10 scanned pairs** found a real case --
     overwhelmingly clustered on **2025-10-11**, funding between -0.28%
     and -0.84%/8h across DOGE/PEPE/XRP/SOL/SUI/AVAX/NEAR/FLOKI
     simultaneously, each followed by a +10.7% to +16.2% rally within
     72h. This is a real, known market-wide deleveraging event (a
     cross-market flash crash followed by a squeeze), not noise --
     exactly the systemic short-squeeze setup this filter exists to
     catch, and it would have flagged nearly the entire scanned universe
     at once, correctly, on that day. Needed two follow-up fixes to find
     this: the funding-history endpoint only returns 200 entries per
     call (~66 days for most 8h-funding pairs) -- added pagination via
     `endTime` walking backward -- and the matching kline fetch window
     needed widening from 90 to 400 days to have price data covering the
     same span. The first, unpaginated attempt found nothing on either
     filter's historical check and would have been reported as a null
     result if not investigated further.
4. `python -m py_compile` on both filter files plus all three check
   scripts, and a JSON validity check on both edited `config.json` files.

**Config wiring:** `{"method": "BollingerTrendPairList"}` appended as the
last step in `freqtrade/user_data/config.json`'s `pairlists` array (after
Volume/Age/Price/Spread/Volatility); `{"method": "FundingRsiPairList"}`
likewise in `freqtrade-shorts/user_data/config.json`. Deliberately left
out an untested `"refresh_period"` key from both entries -- neither filter
implements refresh-period-based caching (it fetches fresh data on every
`filter_pairlist()` call), and claiming a config key that does nothing
would be worse than a shorter config. Both Dockerfiles updated to
`COPY user_data/pairlists/` into the image, same baked-at-build-time
pattern as `config.json`/`strategies/` (see the original Dockerfile
docstring comment for why bind-mounts don't work for Portainer git
stacks).

**Not done / explicitly out of scope for this pass:** Moonshot and Dip
were not touched (Dip by explicit brief instruction; Moonshot wasn't
mentioned). No backtesting-mode support for either filter (`backtest`
runmode would need to fetch/replay historical funding/OHLCV instead of
live API calls -- `supports_backtesting = SupportsBacktesting.NO` states
this honestly rather than pretending compatibility that doesn't exist).
Tuning `N`, the funding thresholds, or the RSI fallback level based on the
historical scan's results is a real follow-up, not attempted here without
being asked -- the numbers in this patch are exactly what the brief
specified, not adjusted based on what the verification scripts found.

## P5 -- ensemble second-opinion voting (Phase 1: shadow mode only)

A 3-voice ensemble (DeepSeek + up to 2 more, drawn from a tiered provider
pool) that, for now, only ever LOGS what it would have decided --
`DeepSeekStrategy.py`/Shorts/Moonshot/Dip's actual entry decision is still
100% governed by DeepSeek's own vote, unchanged. This does not go live
until Phase 2 (below) shows the ensemble is `>= baseline` on `profit_factor`
with no revenge-trading pattern -- same gate discipline as the Survival Long
Bot and P4.

**"Borderline", defined operationally, not left vague:** DeepSeek's own
decision was not `"hold"`. There is no numeric confidence score anywhere
in this pipeline for either voice to threshold on -- DeepSeek returns a
bare decision token, not a probability -- so this module treats every
actionable decision (anything that would open or flip a position) as
worth a second look, and every `"hold"` as low-stakes (nothing happens
regardless) and never triggers the ensemble at all. Stated plainly as a
real design simplification, not dressed up as more precise than it is.

**"Critical"** (gates access to the Tier 2 pool) = `pair.tier == "core"`
AND borderline, exactly per the brief. Satellite-tier only ever gets
Tier 1, even when borderline.

**`_second_opinion.py`, duplicated per bot** (same convention as
`get_market_context()`/`get_news_headlines()` -- self-contained
containers, one file's shape change can't silently break the other
three):
- **Main** -- canonical copy, `is_core` from `_classify_pair()` as normal.
- **Shorts** -- `allowed` widened to the 3-way `(buy, sell_short, hold)`,
  own env var `FREQTRADE_SHORTS_SECOND_OPINION_ENABLED`.
- **Dip** -- otherwise identical to Main's copy (Dip already has real
  `is_core`/tiering), own env var `FREQTRADE_DIP_SECOND_OPINION_ENABLED`.
- **Moonshot** -- has NO core/satellite tiering at all (confirmed by the
  original 2026-09-21 audit and every entry since). Its call site always
  passes `is_core=False`, so a borderline Moonshot decision can never
  reach "critical"/Tier 2 -- there's no concept of a Moonshot "core" pair
  to qualify. Own env var `FREQTRADE_MOONSHOT_SECOND_OPINION_ENABLED`.

**Voting rule, exactly per the brief, in `vote_result()`:** 3 votes
(DeepSeek + 2) -> 2-of-3 majority, all-distinct -> DeepSeek tie-break. 2
votes (DeepSeek + 1, the other pool member never responded) -> agree ->
that decision, disagree -> `hold`. 1 vote (both pool members
unreachable/no key) -> fail-open, DeepSeek's decision unchanged.
`gather_votes()` dispatches the whole pool in parallel
(`ThreadPoolExecutor`) and keeps the first 2 that actually answered, in
completion order -- a provider with no configured key returns `None`
immediately and never occupies a vote slot or costs real latency.

**Provider coverage, honestly caveated -- read before trusting beyond
DeepSeek/OpenRouter:** this session had no `XAI_API_KEY` /
`NVIDIA_API_KEY` / `LLM7_API_KEY` / `GEMINI_API_KEY` to exercise Grok,
NVIDIA NIM, or llm7.io against a real account -- their exact model slugs
and endpoint shapes in `_second_opinion.py` are best-effort from public
documentation, not verified live, same caveat class as every other
"can't verify against a real Freqtrade/provider runtime from this
sandbox" note already in this file. Gemini's REST shape is the most
stable of the four (Google's documented v1beta API), but its free-tier
numbers (15 RPM / 1000 RPD) come from a third-party tracker per the
brief itself -- **verify at aistudio.google.com/rate-limits before
relying on them**, not assumed here. `openrouter_free`'s vote shares
`OPENROUTER_API_KEY` with DeepSeek's own primary fallback path -- stated
plainly: it is not fully statistically independent of that fallback,
this is what the brief specified (reuse the existing key, don't add a
new one), not an oversight. Check this container's logs after first
deploy with a feature flag on for `"second opinion: <provider> call
failed"` lines before trusting any of the four new providers.

**`import _second_opinion` is a bare, same-directory import** -- real
Freqtrade puts `user_data/strategies/` on `sys.path` automatically, so
this resolves correctly in the actual container. It does NOT resolve
automatically in this dev sandbox's own test-loading trick
(`importlib.util.spec_from_file_location`, used throughout
`backtest/`) -- **fixed `backtest/llm_replay.py`'s `_load_strategy_module()`**
to add the strategy file's own directory to `sys.path` before loading,
or `llm_replay.py`/`survival_ab_test.py`/anything importing Main's
strategy standalone would have started failing the moment this file
landed. Caught by actually re-running `llm_replay.py --dry-run` after
the edit, not assumed safe.

**Feature flags, all four bots, default `false`:**
`FREQTRADE_SECOND_OPINION_ENABLED` / `_SHORTS_` / `_MOONSHOT_` / `_DIP_`.
When `false` (default), `run_shadow()` returns immediately -- zero API
calls, zero behavior change, zero log noise. All 9 new env vars
(`XAI_API_KEY`, `NVIDIA_API_KEY`, `LLM7_API_KEY`, `GEMINI_API_KEY`, the 4
flags) registered in `ENV_VAR_DEFS` (`backend/app.py`) and piped into
`docker-compose.yml` for `freqtrade`/`freqtrade-shorts`/
`freqtrade-moonshot`/`freqtrade-dip` plus `survival-agent` (for dashboard
display) -- **deliberately NOT piped into `freqtrade-survival`**, P5 is
explicitly scoped to the four live bots only, same reasoning already
documented for why the Survival Long Bot experiment stays isolated. Registered
through Portainer's stack Environment Variables, same as every credential
in this project since the Credentials-panel-became-read-only change --
never the old UI.

**Verification actually performed:** `backtest/test_p5_second_opinion.py`
-- 27 unit-test assertions against `is_enabled`/`is_borderline`/
`pick_pool`/`vote_result`/`gather_votes`/`run_shadow`, all with mocked
provider calls (never hits a real network). Confirms: pool selection
matches the brief's satellite-vs-core split, the 3-vote/2-vote/1-vote
rules produce the exact brief-specified outcomes including the 3-way
tie-break, `gather_votes` correctly skips a provider that returned `None`
and stops at the first 2 successes, and `run_shadow` is a true no-op
(zero calls) both when the feature flag is off and when the decision
isn't borderline. `python -m py_compile` on all 4 bots' strategy files +
`_second_opinion.py` copies + `backend/app.py` + the two new backtest
scripts. `docker-compose.yml` re-validated as parseable YAML with the new
vars landing on exactly the intended 5 services (4 live bots +
survival-agent for display) and NOT on `freqtrade-survival`. `git diff`
confirmed clean on `stoploss`/`minimal_roi`/`max_open_trades`/
`risk_guard.py`/the actual `_parse_llm_decision(...)` call lines
(unchanged context, only new lines added after them).

**Phase 2 (`backtest/second_opinion_ab_test.py`), built but honestly NOT
run for real:** reuses `llm_replay.py`'s simulator plus
`survival_ab_test.py`'s `summarize()`/`gate_decision()` (generic metric
functions, not survival-specific) rather than re-implementing either.
Needed one small, backward-compatible change to `llm_replay.py` itself:
`ask_llm()` gained an `is_core: bool = False` parameter (unused by the
default implementation) so an external script can swap in a
`_second_opinion`-aware replacement (`lr.ask_llm = ...`) that knows which
tier pool to use, without `run_replay()` needing to know anything about
P5 at all. Smoke-tested structurally (runs end-to-end, produces a valid
report, `python -m py_compile` clean) with **zero provider keys
configured** -- confirmed every borderline decision correctly fails open
to baseline in that state, and confirmed (by direct check of the
condition itself, not just observation) that the script's own built-in
warning ("every borderline decision failed open... do not treat the gate
result as evidence") fires correctly whenever `borderline_decisions > 0`
and `total_votes_gathered == 0`. **Not run against a real historical
window with real provider responses -- this needs
XAI_API_KEY/NVIDIA_API_KEY/LLM7_API_KEY/GEMINI_API_KEY, none of which
exist in this session, and running it without them would produce a
meaningless trivially-passing gate, not real evidence.** Only wired up
for Main (`--bot main`) -- `llm_replay.py` itself only ever supported
Main's pipeline, same limitation already documented for P4 and the
Survival Long Bot A/B test; Shorts/Moonshot/Dip would each need their own
replay harness before Phase 2 could run on them.

**Not done / explicitly out of scope for this pass:** actually acquiring
any of the 4 new provider API keys (operator's own task, via Portainer's
stack Environment Variables -- never the old Credentials UI). Running
Phase 2 for real. Flipping any `FREQTRADE*_SECOND_OPINION_ENABLED` flag
to `true` anywhere (all four stay at their `false` default -- even shadow
logging doesn't start until the operator explicitly turns it on per bot).
Any prompt or threshold changes based on what Phase 1 shadow logs might
eventually show -- that would be a real follow-up decision once real
shadow data exists, not assumed here.

**Addendum, same day: Mistral added to Tier 1 (not Tier 2).** Operator
checked Mistral's actual free-tier limits (help.mistral.ai) after this
landed: 1 req/s (~60 RPM), 500k tokens/min, 1B tokens/month -- more
generous than both llm7.io (40 req/min, 1M tok/24h) and Gemini's 15 RPM/
1000 RPD, so it belongs alongside Grok/NVIDIA/OpenRouter, not gated
behind the "critical" (core+borderline) check. `TIER1_PROVIDERS` is now
4 providers in all four bots' `_second_opinion.py` copies
(`_call_mistral()`, same best-effort-model-slug caveat as Grok/NVIDIA/
llm7 -- no `MISTRAL_API_KEY` available in this session either, not
verified live). Pool selection/voting rule/gate protocol unchanged --
`gather_votes()` still only ever keeps the first 2 successful responses
regardless of pool size, so this purely adds redundancy against one
Tier 1 provider being down, not a 3rd Tier-1 vote. `MISTRAL_API_KEY`
registered in `ENV_VAR_DEFS` (group `general`) and piped into
`docker-compose.yml` for the same 5 services as the other three new
provider keys (4 live bots + `survival-agent` for display) -- still
correctly excluded from `freqtrade-survival`. `test_p5_second_opinion.py`
updated (Tier 1 count 3 -> 4, `mistral` presence in both the pool list
and the dispatch dict) and re-run clean.

## P4.1 -- custom Freqtrade pairlist plugins were never going to work; moved to confirm_trade_entry()

**Real, confirmed root cause of why Main and Shorts wouldn't start with
the P4 config:** Freqtrade's `pairlists` config schema validates `method`
against a fixed enum of built-in names. Custom pairlist classes in
`user_data/pairlists/` are **not dynamically discovered or accepted** --
this is a real, maintainer-confirmed, *intentional* limitation, not a
config mistake on this repo's part. Verified against the actual GitHub
issue, not taken on faith: `freqtrade/freqtrade#12445` ("Custom Pairlist
Plugin can not be used"), closed as a duplicate of #4477, with xmatthias
(Freqtrade core maintainer) stating explicitly: *"Custom Pairlists /
plugins are currently not supported and it's an (intentional) limitation,
as we (currently) don't consider this to be a public / user facing
interface... there's no plans on supporting user provided plugins
anytime soon."* So `{"method": "BollingerTrendPairList"}` /
`{"method": "FundingRsiPairList"}` in `config.json`'s `pairlists` array
(added in the P4 commit) would fail Freqtrade's own config validation at
container startup with a `CRITICAL - Invalid configuration` error on the
unrecognized enum value -- both bots would never have started at all,
not "worked but filtered wrong."

**Fix:** removed both invalid pairlist entries from
`freqtrade/user_data/config.json` and `freqtrade-shorts/user_data/config.json`
(and the now-dangling trailing comma on each array's new last element).
The exact same decision logic -- unmodified, not rewritten -- was ported
from `user_data/pairlists/BollingerTrendPairList.py` /
`FundingRsiPairList.py` into a new `confirm_trade_entry()` method on each
bot's actual strategy class. `confirm_trade_entry` is a real, documented
Freqtrade strategy callback that runs immediately before an order is
placed -- unlike a pairlist filter (which never worked here), this is a
genuinely supported extension point. Same thresholds, same fail-open
behavior on any fetch/data problem, same direct-Bybit-REST fetch pattern
(no key needed, same as `get_market_context()`) -- a relocation, not a
redesign. Confirmed neither bot previously defined `confirm_trade_entry`
(no collision), and that it runs independently of P5's
`_second_opinion.run_shadow()` (called from `_ask_deepseek`, an entirely
different code path).

**The two now-inert `user_data/pairlists/*.py` files were deliberately
left in place, untouched** -- optional per the brief, and touching them
(even to add a "moved" comment) would have widened this fix's diff
beyond the four files that actually needed to change. They simply never
get loaded by anything now; Freqtrade has no config path that references
them.

**Verified for real, not assumed:**
- Both `config.json` files re-validated as parseable JSON after the edit.
- Both `confirm_trade_entry()` implementations imported and called
  directly (same importlib-stub trick used throughout `backtest/`)
  against **real live Bybit data** for BTC/ETH/DOGE (Main, spot) and
  BTC/ETH/DOGE perpetuals (Shorts) -- all returned `True` (allow),
  consistent with calm current market conditions and the earlier P4
  dry-run check finding nothing to exclude on the same day.
- All four real code paths forced and confirmed correct via mocked
  `requests.get`, not just the pass-through case: Main's breakdown-reject
  path (`False`, with the expected `log.info` line) and its fetch-failure
  fail-open path (`True`, with the expected `log.warning` line); Shorts'
  funding-reject path (`False`) and its RSI-fallback-reject path (`False`,
  triggered only when funding itself was unavailable).
- `git diff --stat` after the fix: exactly the two `config.json` files and
  the two strategy files, nothing else -- `_second_opinion.py`,
  `auto_tune.json`, and the `.md` docs were confirmed untouched (and, per
  a direct `git status`/`git diff` check before starting, this working
  tree had no pre-existing uncommitted changes to lose in the first
  place).
- `git diff` on `stoploss`/`minimal_roi`/`max_open_trades`/`risk_guard.py`:
  empty.
- **Not verified:** an actual `freqtrade trade --dry-run` startup inside a
  real container -- no Freqtrade install/Docker available in this dev
  sandbox. The config-schema fix itself is confirmed correct against
  Freqtrade's own documented enum validation behavior and the linked
  issue; whether the bots start cleanly end-to-end still needs confirming
  on the next real deploy.

## P5.1/P5.2/P5.3 -- provider fixes from real prod logs, rotation/throttle, 3 new candidates

Combined pass across all four bots' `_second_opinion.py`, driven by actual
prod log evidence rather than more guessing. Applied in order (P5.1 diagnosis
-> P5.2 fixes -> P5.3 additions), same session.

**P5.1 -- diagnosis + one real silent-failure fix:**
- Grok: repo-side wiring checked and confirmed correct --
  `XAI_API_KEY` is present in `docker-compose.yml`'s `environment:` block
  for all four live bots (not the class of bug once hit with
  `DEEPSEEK_API_KEY`). No code change made in this step, per the brief --
  see the real finding below, from actually testing this with a key.
- Mistral: 429s acknowledged as a real 1 req/sec account limit, not a
  bug. No code change in this step -- the throttle fix is P5.3, below.
- OpenRouter_free: found and fixed the real silent-failure spot --
  `_call_openrouter_free` returning `None` when
  `_get_openrouter_free_models()` came back empty had NO log line at all
  (the HTTP call itself already had a try/except+warning; this was a
  separate, earlier return path). Added a `log.warning` there.

**P5.2 -- two provider fixes:**
- NVIDIA: prod 410 Gone was a stale/invalid model slug in the request
  body, not the base URL (already correct). `_call_nvidia` now tries
  `nvidia/llama-3.3-nemotron-super-49b-v1` first, and on ANY failure of
  that specific model, retries once with
  `nvidia/nemotron-3-super-120b-a12b` before giving up.
- llm7.io: their own docs explicitly warn against hardcoding a specific
  model release since their free catalog is live/rotating. Switched to
  their documented `"fast"` selector as primary; on failure, falls back
  to a live-discovered model from their own `/v1/models` endpoint,
  cached 6h -- same pattern as `_get_openrouter_free_models`, added as
  `_get_llm7_free_models`.

**P5.3 -- rotation, per-provider self-throttle, three new candidates:**
- Rotation: `_rotate_pool()` + module-level `_rotation_state` so the pool
  order dispatched to `gather_votes` isn't always
  Grok-first-every-single-time. Process-memory only, resets on restart --
  fine, this is about spreading load within a session, not a durable
  fairness guarantee.
- Self-throttle: `MIN_CALL_INTERVAL_SECONDS` + `_last_call_at`, a purely
  local (per-bot, no shared state across the 4 containers) minimum gap
  between this bot's own calls to a given provider. Only Mistral has a
  number (6s) -- it's the only provider with a confirmed real 429 behind
  it; every other provider defaults to 0 (no self-throttle) until
  similarly evidenced. A throttled provider is skipped for that cycle
  entirely (moves to the next in the pool), not queued or delayed.
- Kilo (new Tier 1), Aion Labs (new Tier 1), Agnes AI (new Tier 2) added.
  Agnes is **hard-disabled** (`AGNES_ENABLED = False`, checked before even
  reading the API key) and deliberately absent from `TIER1_PROVIDERS`/
  `TIER2_PROVIDERS` -- dispatchable via `_PROVIDER_CALLS["agnes"]` if a
  future pass ever adds it to a pool list, but `pick_pool()` can never
  select it today. Must stay this way until the operator confirms
  `agnes-2.5-flash` is genuinely free on their plan.

**Verification, and three real findings that came out of it (not more
guessing -- these are from actual calls against real accounts with real
keys the operator added to `backtest/.env.local` mid-session):**
- `backtest/test_p5_second_opinion.py` extended with ~20 new assertions
  (rotation order, throttle skip/recovery, NVIDIA primary-vs-fallback
  dispatch, llm7 fast-vs-fallback dispatch and that the model-list fetch
  is skipped on the happy path, openrouter_free's fixed warning, Agnes's
  hard-disable). One pre-existing assertion (`TIER1 has exactly 4
  providers`) updated to a subset check since the count legitimately grew
  to 6 -- not a regression, an expected consequence of this patch.
- A real `run_shadow()` call (real DeepSeek+OpenRouter keys, `is_core`
  both `True` and `False`) surfaced three genuine, previously-unknown
  facts, each confirmed by reading the actual response body, not
  inferred from a bare status code:
  1. **Grok's 403 is an xAI account/billing issue, not a code or
     key-delivery bug at all** -- real body: *"Your newly created team
     doesn't have any credits or licenses yet."* Nothing in this repo can
     fix this; the operator needs to add credits/a license to the xAI
     console team.
  2. **Kilo's endpoint is correct**, but the request needs an explicit
     `model` field even though the account is supposed to auto-route --
     real body: `"Model not found"`. Left unresolved on purpose --
     guessing a value (e.g. `"auto"`) without knowing Kilo's actual
     contract would just trade one wrong guess for another; needs the
     operator's own Kilo docs/dashboard.
  3. **Aion's model slug is wrong** -- real body: `"Unknown model:
     Aion-2.0"`. Also left unresolved -- same reasoning, needs the
     operator's own Aion console/docs for the real value.
  - The reason none of these three specifics were visible from ordinary
    warning logs already in place: `requests`' default `str(e)` on an
    `HTTPError` is only ever `"<status> Client Error: <reason> for url:
    ..."` -- never the response body, which is exactly where all three
    real reasons above lived. Added `_http_error_detail()` (same
    reasoning as `_call_deepseek`'s own error logging in the main
    strategy file) and applied it to every provider call's warning line
    across all four files -- confirmed by re-running the same live call
    and seeing the real body text appear in the log this time, not just
    the bare status.
  - Mistral's throttle was also confirmed working live in the same
    session, not just in mocked tests: the very first real call hit a
    genuine 429, and the second `run_shadow()` call (a different pair,
    seconds later) correctly logged `"mistral throttled this cycle (last
    call 3.4s ago, min interval 6s)"` and skipped it rather than calling
    again.
  - NVIDIA's fallback was confirmed working live too: the primary model
    still 410s for real, and the fallback model returned real (if
    verbose/reasoning-style) content, which is exactly the intended
    behavior.
- `git status`/`git diff --stat` after the whole pass: exactly the four
  `_second_opinion.py` files, `backend/app.py` (new `ENV_VAR_DEFS`
  entries: `KILO_API_KEY`, `AION_API_KEY`, `AGNES_API_KEY`),
  `docker-compose.yml` (same three piped into the four live bots +
  `survival-agent` for display, confirmed absent from
  `freqtrade-survival`), and the test file. `DeepSeekStrategy.py` and the
  other three strategy files (including their P4.1 `confirm_trade_entry`
  methods), `risk_guard.py`, `auto_tune.json`, and both `.md` architecture
  docs: confirmed untouched via `git diff --stat` returning empty for all
  of them.

**Not done in this pass:** actually fixing Grok/Kilo/Aion to work end to
end -- all three need operator-side information (xAI billing, Kilo's
correct `model` value, Aion's correct model slug) this session doesn't
have. Gemini's real-session 404 was observed but not investigated --
out of this patch's stated scope.

**Same-day follow-up: both unresolved items actually resolved, from the
providers' own real documentation, not more guessing.**

- **Kilo, fixed.** Read kilo.ai/docs/gateway/models-and-providers directly
  (via browser, since the docs site is a client-rendered Next.js app --
  raw `curl` only returns an empty shell). Confirmed: the gateway's own
  documented, no-credits-required auto-routing tier is literally called
  `kilo-auto/free` -- *"resolved model is selected dynamically per
  session from a curated set of available free models"*, exactly the
  "auto-routes across a pre-configured pool" behavior the original brief
  described. `_call_kilo` now sends `"model": "kilo-auto/free"`. The
  endpoint itself was already correct (P5.3's finding stands) -- the
  missing `model` field was the whole problem. Re-tested live with the
  real key: the 404 "Model not found" is gone. A real call did then hit a
  read timeout at both the default 15s and a retried 40s -- documented
  honestly rather than declared fixed-and-done: the *routing* bug is
  confirmed fixed, but this session did not confirm a *successful*
  content response within any timeout tried, and free-tier latency is a
  plausible, unverified explanation, not a diagnosis.
- **Aion, confirmed genuinely unfixable as a free-tier candidate --
  disabled, not left broken.** Read aionlabs.ai/docs/quickstart/ and
  /docs/models/. Two real facts: (1) the correct model slug is
  `aion-labs/aion-2.0` (the original `"Aion-2.0"` was missing the
  provider prefix and had wrong casing -- a real, fixable bug on its
  own), but (2) aionlabs.ai's own models page shows real per-token
  pricing on every single listed model (from $0.70/1M in on the cheapest)
  with none free. Fixing the slug alone would not make this a usable
  free-tier ensemble voice. Per the operator's own instruction once this
  was confirmed, `_call_aion` is now hard-disabled the same way Agnes is
  (`AION_ENABLED = False`, checked before even reading the API key, not
  wired into `TIER1_PROVIDERS`) -- a confirmed "no free option exists",
  not a "not verified yet" placeholder. The model slug was still
  corrected in the (dormant) function body for whenever/if this is
  revisited, so a future re-enable doesn't inherit the original wrong
  slug on top of everything else.
- `test_p5_second_opinion.py` updated: Tier 1 count assertion 6 -> 5,
  added `aion`-disabled-no-op and `aion`-excluded-from-both-tiers checks
  (mirroring the existing Agnes ones), added a check that `_call_kilo`'s
  request body actually contains `model: "kilo-auto/free"`. Re-run clean.
- **New, real, not-yet-fixed finding, flagged rather than silently
  absorbed:** `gather_votes()`'s `with ThreadPoolExecutor(...) as ex:`
  block waits for ALL submitted futures to complete before exiting
  (`shutdown(wait=True)` on `__exit__`), even after the `break` once
  `max_votes` is already reached. A slow/hanging provider (Kilo's real
  40s+ timeout observed today is a live example) can therefore stall the
  whole `run_shadow()` call well past when it already had enough votes
  to decide, not just cost that one provider its own vote. Not fixed in
  this pass -- flagged as a real, evidenced issue for a future patch,
  not guessed at.
- `git diff --stat` after this follow-up: exactly the four
  `_second_opinion.py` files and the test file -- `docker-compose.yml`
  and `backend/app.py` untouched (both keys' env-var plumbing was already
  correct from the original P5.3 pass), and the same forbidden-files list
  (strategy files' `confirm_trade_entry`, `risk_guard.py`,
  `auto_tune.json`, both `.md` docs) confirmed empty again.

## P5.4 -- gather_votes() blocked on a slow straggler even after quorum was already reached

**Real bug, written into the working tree by the operator directly (not
this session originally), verified here rather than trusted on
description alone.** `gather_votes()`'s `with
concurrent.futures.ThreadPoolExecutor(...) as ex:` block calls
`ex.shutdown(wait=True)` on exit -- which blocks until every SUBMITTED
future finishes, even ones still running after the loop's own `break`
once `max_votes` was already reached. A single slow/hanging provider
(P5.3's real Kilo timeout was the concrete case that surfaced this)
could therefore stall the entire `run_shadow()` call for as long as that
one provider's own HTTP timeout, well past the point where the vote had
already been decided.

**Fix (already applied to all four `_second_opinion.py` copies when this
task started -- verified byte-identical across all four via `diff`, not
assumed):** replaced the `with` block with an explicit `try/finally`,
calling `ex.shutdown(wait=False, cancel_futures=True)` in `finally`
instead. This returns as soon as `max_votes` successful answers are in;
any provider still in flight keeps running to completion on its own
thread in the background (harmless -- it was already fired) but is
discarded and never gets a vote slot.

**Verified two ways, not just read:**
1. **Confirmed the bug was real, not just plausible-sounding.** Copied
   the module to a throwaway scratch file, reverted just the
   `gather_votes` body to the original `with ... as ex:` form, and ran
   the exact 2-fast/1-slow-provider scenario from the new test: measured
   elapsed time was **5.003s** (blocked on the 5s-delay mock straggler)
   -- i.e. the new regression test would have failed loudly before this
   fix, not passed by accident. Scratch file deleted immediately after.
2. **Confirmed the fix works, on all four bots directly, not just
   Main.** `backtest/test_p5_second_opinion.py` (already extended by the
   operator with this exact scenario) passes against Main. Additionally
   ran the same 2-fast/1-slow scenario by hand directly against all four
   bots' own `_second_opinion.py` modules (not just relying on the
   `diff`-confirmed byte-identity) -- all four returned in ~0.05s with
   exactly 2 votes and the slow provider correctly absent, not just Main.

**Diff scope, checked, not assumed:** `git status`/`git diff --stat`
after this task: exactly the four `_second_opinion.py` files and the
test file the operator had already written to. `confirm_trade_entry` in
all four strategy files, `risk_guard.py`, `auto_tune.json`, and both
`.md` docs: confirmed untouched. An unrelated, unfamiliar
`_to_delete/` directory was found in the working tree (the operator's
own scratch patch-application scripts, per their own message) -- left
alone, not committed, not deleted.

## P5.5 -- NVIDIA thinking-off, Mistral throttle raised, malformed votes no longer counted

Three fixes across all four `_second_opinion.py` copies.

**1. NVIDIA: disable reasoning via a leading system message.** Same class
of issue DeepSeek's own `thinking: {type: disabled}` flag already exists
for in `DeepSeekStrategy.py`'s `_call_deepseek` -- NVIDIA's Nemotron
models reason by default, and a 50-token budget meant for a one-line
`"buy: reason"`/`"hold: reason"` answer gets consumed by reasoning
tokens first. NVIDIA's own convention puts this control in a leading
`{"role": "system", "content": "detailed thinking off"}` message, not a
top-level request field -- added as its own message ahead of the real
system prompt, not merged into it (kept them separable rather than
string-concatenating one system message, so a future audit of "what's
the real prompt" isn't complicated by a merged instruction). `max_tokens`
left at 50 -- with reasoning off, the short answer format needs no more.

**Real result of testing this live, not assumed fixed:** the primary
model call now surfaces a genuinely new, actionable fact thanks to the
earlier `_http_error_detail` fix reaching this line too (see below) --
the 410 body says outright: *"The model
'nvidia/llama-3.3-nemotron-super-49b-v1' has reached its end of life on
2026-08-26T09:00:00Z and is no longer available."* That model is
confirmed permanently dead, not intermittently failing. The fallback
model (`nvidia/nemotron-3-super-120b-a12b`) still returns a verbose,
reasoning-style ramble even with "detailed thinking off" sent -- either
this specific model doesn't honor that directive, or it needs a
different mechanism NVIDIA hasn't documented the same way for it. Not
guessed at further here -- flagged as a real, unresolved fact, same
discipline as Kilo's `model` field and Aion's slug in the P5.3/5.4
entries above. Point 3 below (malformed votes no longer counted) is what
actually makes this survivable in practice: the ramble now gets
discarded instead of corrupting a vote.

**1b. Bonus fix, found while already in this exact function:** `_call_nvidia`'s
and `_call_llm7`'s `_attempt()`'s error-logging lines had been missed by
the earlier P5.1-P5.3 `_http_error_detail` pass -- both use a two-`%s`
format (`"...on model %s: %s", model, e`) that the original regex-based
batch edit didn't match (it only matched the single-`%s`,
`"...", e)` shape used everywhere else). Both now correctly log the real
response body too -- confirmed live: this is exactly what surfaced the
"end of life" fact above; without it, this would have still just read
"410 Client Error: Gone".

**2. Mistral: self-throttle raised 6s -> 22s.** Stated as a deliberate,
honest compromise, not a complete fix: Mistral's real rate limit is
per-WORKSPACE (account-wide), not per-bot, and this throttle only
governs how often *this one* bot calls Mistral -- it has zero
coordination with the other three containers, which have no shared
memory. Four bots starting or cycling close together can still collide
and 429 even at 22s. A real fix needs cross-container shared state
(Redis, a shared file) -- deliberately not built for a shadow-mode-only
feature where an occasional missed vote costs nothing. The comment in
the code says this explicitly, so a future reader doesn't mistake 22s
for a guarantee.

**3. A malformed/unparseable answer no longer counts as a real vote.**
Real bug: `_parse_vote()` already defaulted any unrecognized answer to
`("hold", malformed=True)`, but `gather_votes()` added it to `votes[]`
regardless of the `malformed` flag -- meaning a provider that returned
incoherent text could outvote a real decision (e.g. a 2/2 "disagree" ->
`hold` where one of the two "votes" was actually garbage, not a
considered `hold`). Fixed by treating `malformed=True` the same as
`answer is None`: logged via a dedicated `log.warning` with the raw
text, never appended to `votes`. `vote_result()` itself untouched, since
it already handles a smaller `other_votes` list correctly by design.

**Verification:**
- `test_p5_second_opinion.py` extended: NVIDIA payload asserted to
  contain `"detailed thinking off"` as its own leading system message
  (with the real system prompt still present, unmodified, after it);
  Mistral's throttle constant asserted to be 22 (existing throttle test's
  timings updated from 6s/7s to 22s/23s to match); a malformed answer
  asserted to be excluded from `votes` while a real vote from another
  provider still comes through, with a warning logged for the discarded
  one. All new and pre-existing assertions re-run clean.
- Confirmed live against the real NVIDIA account (same `.env.local`
  keys as the P5.1-P5.4 passes): the "end of life" body is now visible
  (see above), the fallback model's ramble is confirmed to still parse
  as `malformed=True`, and a real `gather_votes(["nvidia",
  "openrouter_free"], ...)` call was confirmed to exclude nvidia's
  garbage answer from the returned votes entirely while still returning
  openrouter_free's real one -- not just asserted in a mock.
- `git diff --stat`: exactly the four `_second_opinion.py` files and the
  test file. `confirm_trade_entry`, `risk_guard.py`, `auto_tune.json`,
  both `.md` docs, `docker-compose.yml`, and `backend/app.py` confirmed
  untouched -- no new env vars or config needed for any of these three
  fixes.

**Not done in this pass:** figuring out why the NVIDIA fallback model
doesn't honor "detailed thinking off", or finding a third NVIDIA model
that both works and respects it -- flagged, not guessed at. Cross-
container coordination for Mistral's real per-workspace limit --
explicitly deferred, stated as a real limitation of the shadow-mode-only
throttle, not silently accepted as solved.

## P5.6 -- NVIDIA actually fixed: real EOL model dropped, real reasoning-truncation root cause found, dynamic model discovery added

Operator's question that triggered this: "Do we need only free endpoints
models?" -- yes, matching every other Tier 1 provider's philosophy
(no-cost voices for a shadow-mode feature with zero budget allocated).
Answering it properly required actually fixing NVIDIA, not just
filtering it.

**Real finding #1, confirmed via NVIDIA's own live `/v1/models` endpoint
with the real account key:** `nvidia/llama-3.3-nemotron-super-49b-v1`
(P5.2's "current" pick) is **not just erroring, it's gone** -- absent
entirely from the account's own current model list, consistent with the
410 body's own text ("has reached its end of life"). Two other listed
models (`nemotron-nano-3-30b-a3b`, `llama-3.1-nemotron-70b-instruct`)
returned a live 404 on invocation despite being *listed* -- confirming
"still in `/v1/models`" alone doesn't mean "still invokable on this
account". `nemotron-3-super-120b-a12b` (P5.2's fallback) and
`nemotron-3-ultra-550b-a55b` were both confirmed live and working, and
are now primary + fallback, with the actually-broken v1 slug dropped
entirely rather than kept as a doomed-to-fail first attempt.

**Real finding #2, the actual root cause of the "malformed" votes P5.5
could only detect, not prevent:** "detailed thinking off" (P5.5's fix)
does **not** stop `nemotron-3-super-120b-a12b` from reasoning --
confirmed by requesting a large `max_tokens` and reading the full
response: `reasoning_content` still comes back populated (~215-220
tokens' worth) even with the system message present. The REAL bug was
`max_tokens=50` -- exactly DeepSeek's own historical bug
(`_call_deepseek`'s `thinking: {type: disabled}` fix), confirmed by
directly comparing the same request at `max_tokens=50` (finish_reason
`"length"`, `content` is the unfinished reasoning ramble -- this is what
P5.5 was catching as "malformed") vs. `max_tokens=300` (finish_reason
`"stop"`, `content` is a clean one-line `"buy: ..."`/`"hold: ..."`
answer). Fixed by raising `max_tokens` to 300, matching DeepSeek's own
number. "detailed thinking off" is kept anyway -- harmless, and NVIDIA's
own docs describe it as a real control for *some* Nemotron variants, so
it may still help a different model even though it didn't help this one.

**Answering the operator's actual question -- "free" models, done via
live discovery, not another hardcoded guess:** NVIDIA's own `/v1/models`
has **no pricing or free-tier field at all** (confirmed by reading the
real response -- just `id`/`object`/`created`/`owned_by`), unlike
OpenRouter/llm7 where `pricing.prompt == 0` is directly filterable. There
is no API-verifiable "is this free" signal for NVIDIA. The closest real
proxy: a third party (Kilo's own "Using Kilo for Free" docs) describes
NVIDIA's free-trial access as its `nvidia/`-branded Nemotron "Super/
Ultra/etc" family specifically -- `_get_nvidia_candidate_models()` now
live-fetches and caches (6h, same TTL pattern as the OpenRouter/llm7
discovery functions) the account's real model list, filtered to
`nvidia/`-prefixed ids containing `"nemotron"`, explicitly excluding
non-chat NVIDIA models by name (embeddings, parsers, safety/guard
classifiers, rerankers, translators -- confirmed these exist in the real
list and would otherwise pollute the candidate set). `_call_nvidia` now
tries the 2 confirmed-live hardcoded models first, then up to one
dynamically-discovered candidate as a third attempt -- so a future EOL
on both hardcoded slugs doesn't reproduce this exact failure a third
time; it self-heals from the account's own live catalog instead.

**Verification, all confirmed live against the real account, not just
mocked:**
- Direct calls to `_call_nvidia` after the fix returned clean, correctly
  parseable one-line answers (`malformed=False`) on repeated real tries
  -- previously every real call was `malformed=True`.
- A full real `run_shadow()` call (`FREQTRADE_SECOND_OPINION_ENABLED=true`,
  real DeepSeek/OpenRouter/NVIDIA/Mistral/Grok/Gemini/llm7 keys) produced
  a genuine 2/3-agree verdict with NVIDIA's vote correctly counted as a
  real `"buy"`, not silently dropped or malformed.
- `test_p5_second_opinion.py` extended: primary-succeeds/fallback-
  untouched and primary-fails/fallback-used tests updated to the new
  model names; a new test confirms both hardcoded models failing falls
  through to a live-discovered third candidate; a new test confirms
  `max_tokens == 300` in the actual request payload;
  `_get_nvidia_candidate_models` tested directly against a fake
  `/v1/models` response containing both chat and non-chat NVIDIA models,
  confirming the exclusion filter and the non-`nvidia/`-branded filter
  both work. All prior tests re-run clean alongside the new ones (65
  total assertions now).
- `git diff --stat`: exactly the four `_second_opinion.py` files and the
  test file. `confirm_trade_entry`, `risk_guard.py`, `auto_tune.json`,
  both `.md` docs, `docker-compose.yml`, and `backend/app.py` confirmed
  untouched -- no new env vars needed for this fix.

**Incidental finding, NOT acted on (out of this task's stated scope --
operator asked about NVIDIA specifically):** the same live `run_shadow()`
test surfaced a real, actionable Gemini problem too, now visible for the
same reason NVIDIA's "end of life" fact became visible -- the error body
says outright: *"This model models/gemini-2.5-flash-lite is no longer
available to new users... use models/gemini-3.5-flash-lite."* Flagged
here for a future pass, not fixed without being asked.


## P5.7 -- NVIDIA fallback chain widened with 4 more confirmed-live models, gemma-4-31b-it tried and rejected

Operator pasted NVIDIA playground code snippets for several models they'd
been browsing (`openai/gpt-oss-20b`, `deepseek-ai/deepseek-v4.1-flash`,
`google/gemma-4-31b-it`, `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`
(vision), `google/diffusiongemma-26b-a4b-it` (vision), `meta/muse-glimmer-30b`,
`nvidia/nemotron-3.5-lightning-30b-a3b`), asking whether any were worth
adding. Each per-model throwaway key from those snippets was used for a
single live test call and then discarded -- none were stored anywhere,
none went into `.env.local` or any committed file.

**Live test results (real HTTP calls, same session):**
- `openai/gpt-oss-20b` -- real, 200 OK. Reasons by default at
  `max_tokens=10` (`finish_reason="length"`, content is the unfinished
  reasoning) -- same class of bug already fixed for
  `nemotron-3-super-120b-a12b` in P5.6; needs `max_tokens=300`.
- `deepseek-ai/deepseek-v4.1-flash` -- real, 200 OK, clean immediately.
  `reasoning_content` correctly separated from `content` even at
  `max_tokens=300`.
- `google/gemma-4-31b-it` -- confirmed present in a real account's
  `/v1/models` (`curl .../v1/models` listed it explicitly), but TWO
  separate live `chat/completions` calls (15s and 20s timeouts) came
  back with **zero bytes and no error** -- not a 4xx, a hang. Same
  "listed != invokable" pattern already documented for
  `nano-3-30b-a3b`/`llama-3.1-nemotron-70b-instruct` in P5.6, just a
  different failure shape (silent hang instead of 404). **Rejected** --
  not added anywhere.
- `meta/muse-glimmer-30b` -- real, 200 OK. Also reasons by default at
  `max_tokens=50` (`finish_reason="length"`, `content: null`) -- same
  fix needed, `max_tokens=300`.
- `nvidia/nemotron-3.5-lightning-30b-a3b` -- real, 200 OK, clean
  immediately, using the actual configured `NVIDIA_API_KEY` (not a
  throwaway key).
- The two vision-capable models (`nemotron-3-nano-omni-30b-a3b-reasoning`,
  `diffusiongemma-26b-a4b-it`) were not tested -- `_second_opinion.py` is
  text-only, vision capability is irrelevant to this use case.

**Important caveat surfaced and told to the operator before any code
changed:** every one of these models is hosted on the SAME
`integrate.api.nvidia.com` endpoint under the SAME account as the
existing `nvidia` provider. They are NOT independent providers the way
Kilo, Mistral, or Aion were -- adding them to `TIER1_PROVIDERS` would
not increase cross-provider vote diversity, it would just add more
traffic against one shared rate limit. Their only legitimate use is
resilience *within* the existing `nvidia` slot's fallback chain, in
case the primary/ultra Nemotron models go EOL or start hanging again
(exactly what happened once already with
`nvidia/llama-3.3-nemotron-super-49b-v1` in P5.2).

**Fix applied**, identically across all 4 bots'
`user_data/strategies/_second_opinion.py`: `_call_nvidia`'s hardcoded
`models` list grew from 2 entries to 6 --

```
models = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "deepseek-ai/deepseek-v4.1-flash",
    "openai/gpt-oss-20b",
    "meta/muse-glimmer-30b",
]
models += [m for m in _get_nvidia_candidate_models(api_key) if m not in models][:1]
```

-- keeping the existing `_get_nvidia_candidate_models` live-discovery
call as a 7th, final attempt, unchanged. `max_tokens=300` and the
"detailed thinking off" leading system message (both from P5.5/P5.6)
apply to every model in the list, including the 4 new ones -- confirmed
sufficient for `gpt-oss-20b` and `muse-glimmer-30b`'s reasoning-
truncation issue in the live tests above. `google/gemma-4-31b-it` is
deliberately absent from the list.

**Tests**, `backtest/test_p5_second_opinion.py`: the existing "both
hardcoded models fail -> falls through to live-discovered candidate"
test updated to expect all 6 hardcoded attempts (renamed accordingly);
added a dedicated test asserting the 4 new models appear in the fallback
chain in order; added a test asserting `google/gemma-4-31b-it` never
appears in the attempted-models list. Full suite re-run clean (70 total
assertions now, up from 65).

**Verification**: `python -m py_compile` clean on all 5 touched files;
`git diff --stat` scoped to exactly the 4 `_second_opinion.py` files plus
the test file; `confirm_trade_entry`, `risk_guard.py`, `auto_tune.json`,
both `.md` docs, `docker-compose.yml`, and `backend/app.py` all confirmed
untouched -- no new env vars needed, `NVIDIA_API_KEY` already covers all
6 hardcoded models since they share one account/key.


## P6 -- Survival -> Survival Long rename, plus new isolated Survival Shorts bot

Two-part operator request.

**Part 1, cosmetic rename only.** Infrastructure (container name, DB
volume, port 8140) deliberately left untouched -- renaming it risked
breaking the data binding for zero real benefit. Renamed only the
DISPLAY name, everywhere it said "Survival" meaning the bot (not the
project-wide "Survival Agent" title, and not the general
`SURVIVAL_MODE`/"survival context" business-framing vocabulary shared
by every bot -- those are a different "survival" and were left alone):
- `frontend/index.html`: `label: 'Survival'` -> `'Survival Long'`, and
  `GROUP_LABEL.survival` -> `'Survival Long bot (isolated experiment)'`.
  Note: this is where the dashboard label actually lives, NOT
  `backend/app.py` as the spec assumed -- verified by grepping for the
  literal string before editing, same "check, don't trust the spec's
  file guess" discipline as P4.1's pairlist-plugin finding.
- `backend/app.py`: two `ENV_VAR_DEFS` `desc` strings that name the bot
  ("isolated Survival Bot experiment", "for the Survival Bot ONLY").
- `AGENTS.md`: every occurrence of the literal "Survival Bot" (4 exact
  matches plus one line-wrapped case) -> "Survival Long Bot".
- `docker-compose.yml`: two comment lines referencing "Survival Bot's"
  / "Survival bot --" -> "Survival Long".

**Part 2, new bot: Survival Shorts.** Sixth Freqtrade instance, fully
isolated (own container `freqtrade-survival-shorts`, own DB volume, own
`config.json`, own port `8150` -- next free after `8140`). Base
strategy is `DeepSeekShortsStrategy.py`, not Main/Long, because Shorts
already has the working long+short mechanics (`enter_long`/
`enter_short`, funding-rate `confirm_trade_entry`) this experiment
needs unchanged.

Parameter diffs from Shorts, all more risk-tolerant, all real (not
cosmetic):
- `stoploss`: `-0.05` -> `-0.10`.
- `SATELLITE_STAKE_MULTIPLIER`: `0.5` -> `1.0` -- no satellite-tier size
  discount. Verified the interaction with `_tuned_satellite_multiplier()`'s
  offline-tune file: that method clamps a FILE-read override to
  `[0.3, 0.7]`, which would have silently dragged `1.0` back down to
  `0.7` if a `satellite_stake_multiplier` value ever got read from a
  tuned file -- the class-default fallback path is NOT clamped, so
  deliberately shipping this bot with NO `auto_tune.json` keeps the
  real value at `1.0`. Confirmed by a direct unit test, not assumed.
- `max_open_trades`: `5` -> `8` -- lives in `config.json` only (not the
  strategy class; confirmed by grep before writing the test).
- `SYSTEM_PROMPT`: two sentences removed relative to Shorts' own copy --
  (a) "Require a clearer edge for 'satellite' tier pairs than 'core'"
      (deleted outright: same bar for every tier now), and
  (b) "Slots are scarce (at most 5 open trades across the whole pool):"
      (deleted; the "only act on your highest-conviction setups..."
      instruction that followed it was KEPT, un-scarcity-framed --
      the spec asked to remove the scarcity mention specifically, not the
      conviction bar itself).
  The 2026-09-23 shutdown-pressure ("your existence depends on profit")
  framing from the Survival Long rollout was deliberately NOT added --
  a separate, already-inconclusive hypothesis; mixing it in here would
  make it impossible to attribute any behavior change to one variable.
- New `SURVIVAL_SHORTS_LIVE_MODE` env flag (default `false`): same
  belt-and-suspenders double-gate pattern as Survival Long's
  `SURVIVAL_LIVE_MODE`, checked at the top of `confirm_trade_entry()`
  -- rejects any entry if `dry_run=False` AND this flag isn't also
  `"true"`. No subcap kill-switch (unlike Survival Long) -- not asked
  for here, and building it would have been scope creep onto a
  different, already-built mechanism.

**Deliberately NOT wired up, to stay inside the stated diff scope:**
- P5 ensemble second-opinion (`_second_opinion.py`) -- adding it would
  require a new module file, a new `..._SECOND_OPINION_ENABLED` env var,
  and a new `docker-compose.yml` entry, none of which were requested.
  The new strategy file does NOT `import _second_opinion` at all.
- `backend/app.py` `ENV_VAR_DEFS`/`CAPITAL_BOT_IDS` entries for the new
  bot's own credentials (`BYBIT_SURVIVAL_SHORTS_API_KEY/SECRET`,
  `FREQTRADE_SURVIVAL_SHORTS__DRY_RUN`, `SURVIVAL_SHORTS_LIVE_MODE`).
  The bot works fine with these set directly as Portainer stack env
  vars (docker-compose reads them via `${VAR:-default}` regardless);
  what's missing is just the credentials-panel UI's awareness of them.
  Flagged here for a future pass if the operator wants it, not added
  silently.
- Other bots' `config.json` `CORS_origins` lists were NOT updated to
  include `:8150` -- spec explicitly says existing bots stay untouched.
  Only the new bot's own `CORS_origins` includes `:8150`.

**Files added:** `freqtrade-survival-shorts/Dockerfile` (byte-identical
pattern to `freqtrade-survival/Dockerfile` -- no `pairlists/` dir needed,
same as that bot), `freqtrade-survival-shorts/user_data/config.json`
(Shorts' `pair_whitelist`/`pair_blacklist`/`pairlists` copied verbatim,
per the spec's explicit "no extra meme restrictions" instruction),
`freqtrade-survival-shorts/user_data/strategies/DeepSeekSurvivalShortsStrategy.py`.

**Tests**, new file `backtest/test_survival_shorts.py`: loads the real
strategy module standalone via the same freqtrade-stubbing trick as
`backtest/llm_replay.py`'s `_load_strategy_module` (no reimplementation
of the class, so these checks can't silently drift from what ships).
22 assertions: the three changed numbers, both removed prompt sentences
confirmed gone (and the kept "highest-conviction" bar confirmed still
present), the shutdown-pressure framing confirmed absent, the second
live-mode gate's default-false and true-path behavior, the
`confirm_trade_entry` gate rejection when `dry_run=False` and the flag
is unset, and `config.json`'s numbers/pairlist/blacklist identity with
Shorts. All pass.

**Smoke-test caveat, same honesty as P4.1's hotfix verification:** this
sandbox has no Docker and no working `freqtrade` CLI (`freqtrade
--version` -> command not found; `from freqtrade.strategy import
IStrategy` -> `ModuleNotFoundError` even with the package present) --
an actual `freqtrade trade --dry-run` boot with a real "no CRITICAL in
the logs" check was NOT possible and was NOT run. What was verified
instead: `config.json` is valid JSON and uses only pairlist method
names already proven to load cleanly in Shorts' own sibling config
(`VolumePairList`/`AgeFilter`/`PriceFilter`/`SpreadFilter`/
`VolatilityFilter`, byte-identical list), the strategy module imports
and instantiates cleanly via the stub, and `docker-compose.yml` parses
correctly with the new service present (`python -c "import yaml..."`).
A real container boot still needs a human's eyes on Portainer logs
before this is trusted, exactly like every other bot's first deploy.

**Verification**: `python -m py_compile` clean on the new strategy file
and the new test file; `python -c "import yaml"` confirms
`docker-compose.yml` parses with all 7 services and the new
`freqtrade-survival-shorts-db` volume present; `git diff --stat` scoped
to exactly `AGENTS.md`, `backend/app.py`, `docker-compose.yml`,
`frontend/index.html` (rename-only diffs) plus the new
`freqtrade-survival-shorts/` directory and the new test file. Main,
Shorts, Moonshot, Dip, and Survival Long's own strategy/config files
all confirmed untouched (`git diff` empty on every one of them).


## P6-addon -- Survival Shorts credentials + Settings UI wiring

P6 shipped `docker-compose.yml` already referencing
`BYBIT_SURVIVAL_SHORTS_API_KEY/SECRET` and `SURVIVAL_SHORTS_LIVE_MODE`,
but never registered them in `backend/app.py`'s `ENV_VAR_DEFS` -- so
none of the four would have appeared in the Settings UI at all, and the
dashboard had no way to read the new bot's status. Operator caught this
gap and asked for it fixed.

**`backend/app.py`**: added 4 `ENV_VAR_DEFS` entries, group
`"survival_shorts"`, same shape as the existing Survival Long block --
`BYBIT_SURVIVAL_SHORTS_API_KEY`/`_SECRET` (secret), `FREQTRADE_SURVIVAL_
SHORTS__DRY_RUN` (`is_dry_run: True`), `SURVIVAL_SHORTS_LIVE_MODE`.

**`frontend/index.html`**: added `survival_shorts` to `GROUP_LABEL`,
`GROUP_ACCENT`, and `GROUP_ORDER` -- without this the 4 new env vars
would register but never render (same rendering loop keys off
`GROUP_ORDER`).

**`backend/engine/freqtrade_client.py`**: added a `"survival_shorts"`
entry to the `BOTS` dict (`url` from `FREQTRADE_SURVIVAL_SHORTS_
INTERNAL_URL`, default `http://freqtrade-survival-shorts:8080`) --
without this, `freqtrade_summary("survival_shorts")` has no bot to look
up and any future feature reading this bot's profit/status has nothing
to call.

**Incidental fix caught while touching this file, not asked for but
the same class of miss as the rest of P6's rename:** the existing
`"survival"` entry in this same `BOTS` dict still said `"Survival Bot
(isolated prompt A/B experiment..."` -- missed during P6 because that
file was never grepped for the literal string. Renamed to `"Survival
Long Bot (...)"` alongside adding the new entry, same rename, same
justification as the rest of P6.

Real values for `BYBIT_SURVIVAL_SHORTS_API_KEY/SECRET` are the
operator's own Portainer stack-env task, not code -- explicitly out of
scope here, per the spec itself.

**Verification**: `python -m py_compile` clean on both touched `.py`
files; `ast.parse` + a regex pull of `ENV_VAR_DEFS` confirms exactly the
4 expected keys under group `survival_shorts`; a Node one-liner
constructing `GROUP_LABEL`/`GROUP_ACCENT`/`GROUP_ORDER` from the edited
block ran without a syntax error; `grep` for `"Survival Bot"`
(unqualified) across `backend/`, `frontend/index.html`, and
`docker-compose.yml` returns nothing left over. `git diff --stat`
scoped to exactly the 3 files above.


## P6-payout -- real profit-since-last-payout accounting, survival_cost months-scaling fix

Operator spec: `run_cycle()` was splitting all-time profit every cycle
(re-triggering the same payout math forever, and only ever subtracting
one month of `survival_monthly` regardless of how much time had
actually passed). Fixed both, plus the manual payout-confirmation flow
needed to make "profit since last payout" mean something.

**New `backend/engine/payout_state.py`**: owns `DATA_DIR/payout_state.json`
(`baseline_set_at`, `bots: {bot_id: profit_at_baseline}`,
`history: [...]`). `SPLIT_BOTS = ["main", "shorts", "moonshot", "dip",
"survival"]` -- `survival_shorts` deliberately excluded: it's an
isolated $100 dry-run risk experiment (see P6's own entry above), not
part of the real survival economics. Its profit is still readable via
the normal `freqtrade_summary("survival_shorts")` endpoint for a
dashboard/log line, just never summed into the payout split. Operator
flagged this as easy to change if the decision changes later.

**Compat on first run** (no `payout_state.json` yet): `baseline_set_at`
reuses the existing `plan_started_at.txt` marker if one is already
there (same file `loop.py`'s old `_months_elapsed` used to maintain on
its own), rather than inventing a second, different start-time source
of truth. Every bot's baseline starts at `0.0` -- equivalent to
today's pre-tracking behavior, where all-time profit and
profit-since-baseline were the same number.

**`backend/engine/split_calc.py`**: `survival_cost` now scales as
`survival_monthly * max(1, months_elapsed)` instead of a flat one-month
subtraction. `survival_monthly` is a real recurring bill; if a payout
hasn't been confirmed for 3 months, the account owes 3 months of it,
not 1 -- the old flat subtraction was silently understating true
cumulative cost the longer a payout went unconfirmed. `months_elapsed`
here is `payout_state.months_since(baseline_set_at)`, not the old
plan-start-relative count.

**`backend/engine/loop.py` `run_cycle()`**: now loops `SPLIT_BOTS`,
calling `get_summary(bot_id)` per bot, computing each bot's delta as
`current_profit - state["bots"][bot_id]`. **Confirmed assumption, no
floor(0) per bot before summing** (per the spec's own stated default,
explicitly asked for and confirmed rather than guessed): a real
drawdown in one bot genuinely reduces the operator's total
payout-eligible profit -- it's the same money either way, and flooring
it per-bot before the sum would silently overstate the real total. A
bot unreachable this cycle contributes a `0.0` delta (frozen at its
last known baseline) rather than crashing the whole cycle or reporting
a false all-time number; if EVERY `SPLIT_BOTS` bot is unreachable, the
cycle returns `outcome: "skipped"` instead of a meaningless all-zero
split. Result now carries `per_bot`, `per_bot_current_profit`, and
`unavailable_bots` alongside the existing `split`, so the dashboard/log
can show both the per-bot breakdown and the aggregate. `_extract_profit`
and the old file-marker-only `_months_elapsed` were removed from this
file -- moved to `payout_state.py` as public `extract_profit`/
`months_since` so `app.py`'s new payout-confirm endpoint can share the
exact same logic instead of a second copy drifting apart from this one.

**`backend/app.py`**: added `POST /api/payout/confirm` (body
`{amount_usd}`) and `GET /api/payout/history`, same lazy
`from .engine import ...` pattern as the file's existing endpoints.
Confirm snapshots every `SPLIT_BOTS` bot's CURRENT profit via
`freqtrade_client.get_summary`, calls `payout_state.confirm_payout`
(one history entry for the whole portfolio, matching how a real payout
actually happens -- one withdrawal, not five), and resets the baseline.
**Correctness guard added beyond the spec's literal text, not scope
creep -- a genuine necessity:** if any `SPLIT_BOTS` bot is unreachable
at confirm time, the endpoint returns `409` and refuses to confirm,
rather than silently snapshotting a stale/zero baseline for that bot
and losing track of profit it actually earned but hasn't been paid out
yet. Money still never moves in code either way -- this only gates
whether the bookkeeping event is allowed to happen this instant.

**Incidental fix while touching `backend/engine/freqtrade_client.py`'s
`BOTS` dict for the (unrelated, separately-requested) Survival Shorts
credentials wiring:** its `"survival"` entry's `label` still said
"Survival Bot" -- missed by P6's rename pass since that file was never
grepped. Renamed to "Survival Long Bot" alongside adding the new
`"survival_shorts"` entry, same rename, same justification as the rest
of P6.

**Tests**, new `backtest/test_payout_state.py` (29 assertions, same
house style as the other `backtest/test_*.py` files): the
months-scaling fix on `compute_split` (1mo vs 3mo vs clamped-to-1
survival cost), `load_state`'s fresh-init and existing-marker-reuse
paths, `months_since`, `extract_profit`'s field-preference/fallback/
unrecognized-shape behavior, `confirm_payout`'s baseline-reset/
history-append/no-floor-in-the-total behavior, and `run_cycle`'s
per-bot delta math, no-floor aggregate, and both unavailable-bot paths
(partial and total). All pass; full P5 second-opinion and Survival
Shorts suites re-run clean alongside it (no regression).

**Verification**: `python -m py_compile` clean on all 5 touched/added
`.py` files; `git diff --stat` scoped to exactly `payout_state.py`
(new), `split_calc.py`, `loop.py`, `app.py`, plus the incidental
one-line rename in `freqtrade_client.py` and the new test file.
`risk_guard.py`, every `DeepSeek*Strategy.py`, `docker-compose.yml`,
and both `.md` docs confirmed untouched.


## P8 -- tuning + market-signal expansion (5 items shipped, 2 verified infeasible)

Full spec, replacing two earlier fragments. Applied to the 5 live/A-B
bots (main/shorts/moonshot/dip/survival) -- `survival_shorts`
deliberately excluded from every item here, same "not in prod yet"
policy the operator stated for item 1 and applied consistently across
the whole spec, not just that one item.

**Reliability contract, verified against every item below:** a failed
fetch is excluded from the cycle (returns `None`/not counted), never a
fake neutral default, never a stale-beyond-TTL read; each source has
its own independent `try/except`; every failure is logged with
`log.warning(...: fetch failed ...: %s -- excluded from this cycle)`;
nothing here ever blocks or influences the entry/exit DECISION itself
except item 5 (explicitly specified as a hard code gate, not a prompt
hint -- see below).

**Item 1 -- Fear & Greed Index.** `get_fear_greed_index()`, duplicated
per bot (all 5), `alternative.me/fng/`, no key, 45min TTL (same cadence
as news). Injected into `_ask_deepseek`'s prompt right after "Recent
headlines".

**Item 2 -- funding rate in Shorts' own prompt.** New
`_get_funding_rate_line()` on `DeepSeekShortsStrategy`, Shorts only (not
Survival Shorts, not the other 4 bots -- futures-only concept). The
existing funding-rate fetch inside `confirm_trade_entry` (P4.1) runs at
a DIFFERENT time (order-confirm, not decision-time) and was never
exposed to the LLM -- this is a separate, dedicated fetch at decision
time, not a refactor of the existing veto.

**Item 3 -- Coinglass liquidations for Dip. NOT IMPLEMENTED, verified
infeasible, same lesson as Gemini/LunarCrush/fomo.family (don't trust
marketing copy).** Live-tested two anonymous endpoints -- both returned
`{"code":"30001","msg":"API key missing."}`. Web-verified: Coinglass has
**no free tier at all** -- cheapest paid plan is Hobbyist at $29/mo (30
req/min, 80+ endpoints), confirmed by an independent source stating
outright "There is no free tier for the Coinglass API." No code written
against a paid-only API without the operator's explicit go-ahead to pay
for it.

**Item 4 -- multi-timeframe (4h SMA50) confirmation.** New
`_mtf_confirmation(pair)` per bot, direct Bybit REST 4h kline fetch
(same pattern as the existing Bollinger/funding checks -- no
`informative_pairs()` wiring needed). Returns "price above/below its
own 4h SMA50", or `None` on any fetch/data problem. Context for the
prompt only, never a hard filter in code, per the spec.

**Item 5 -- offline-tuned loss-streak cooldown, a REAL code-level entry
block (not a prompt hint, per the spec's own "hours with no new entries"
wording).** `backtest/auto_tune.py` rewritten to tune a second,
independent parameter alongside the existing `SATELLITE_STAKE_MULTIPLIER`:
`loss_streak_cooldown_hours`, keyed off the bot's CURRENT consecutive-
loss streak (any tier, `LOSS_STREAK_N = 3`) -- streak >= N grows the
cooldown by `COOLDOWN_STEP_HOURS` (4h) up to 48h; streak < N shrinks it
back toward 0 (disabled). Same `--apply` flag, same 24h
cooldown-on-the-change-itself, same `MIN_RELIABLE_SAMPLE` gate --
but tracked INDEPENDENTLY per parameter (`"param"` field in the log;
missing field on pre-P8 log lines is read as `satellite_stake_multiplier`
for backward compatibility) so tuning #2 can't reset #1's cooldown timer
or vice versa. `--bot` choices widened from `[main, shorts, dip]` to all
5 live bots -- Moonshot has no core/satellite tiering so its satellite
computation naturally reports "insufficient data" (0/0 tagged trades),
no special-casing needed; the cooldown computation is tier-agnostic and
runs for every bot. Both parameters are merged into the SAME per-bot
`auto_tune.json`, each write preserving whatever the other already
wrote.

Consumer side, duplicated per bot: `_tuned_loss_streak_cooldown_hours()`
(same candidate-path pattern as `_tuned_satellite_multiplier`, default
`0.0` = disabled so an untuned bot behaves exactly as before) and
`_in_loss_streak_cooldown()` (true only if the most recent
`LOSS_STREAK_N` closed trades were ALL losers AND the most recent one
closed less than the tuned window ago; fails open/`False` on any data
problem). Wired into `confirm_trade_entry` as the FIRST check, ahead of
each bot's existing veto (Bollinger for Main, funding/RSI for Shorts).
Main/Shorts/Survival already had a `confirm_trade_entry` hook (P4.1) --
prepended the cooldown check there. Moonshot and Dip had none before --
added a new `confirm_trade_entry` containing ONLY the cooldown check,
nothing else.

**Item 6 -- BTC/alt concentration-risk warning.** New
`_concentration_risk_warning(pair)` per bot: if any currently-open
position's 30-day daily-close correlation with the candidate pair is
`>= 0.8`, warns (never blocks) with the correlated pair and the
coefficient. Checks every open position, not hardcoded to BTC
specifically -- BTC is just the common real-world case. Each daily-close
fetch is its own try/except (one correlated pair's fetch failure
doesn't drop the others' checks).

**Item 7 -- on-chain exchange netflow. NOT IMPLEMENTED, verified
infeasible without an operator-provided key.** Live-tested Glassnode's
`active_count` and `transfers_volume_exchanges_net` endpoints AND
CryptoQuant's `exchange-flows/netflow` endpoint, all anonymous -- all
three returned 401 Unauthorized (CryptoQuant's body explicit: "Token
does not exists... use 'Authorization: Bearer API_KEY'"). Both require
an account + API key; Glassnode's own free tier is Studio-dashboard
access, not API access. Not built against a nonexistent endpoint, and
not signed up for either service on the operator's behalf (account
creation is the operator's own action, not something done here).

**Survival Long's A/B invariant preserved:** its own module docstring
requires "everything except SYSTEM_PROMPT" to stay identical to Main
for the shutdown-pressure experiment to isolate one variable. All 5
implemented P8 items were added byte-identically (same thresholds,
same logic) to both Main and Survival, so the invariant holds -- both
bots gained the exact same additions, not just one of them.

**Tests**: `backtest/test_p8_market_signals.py`, 38 assertions covering
Fear & Greed's failure/success/malformed-response paths, multi-timeframe
confirmation's uptrend/downtrend/insufficient-data paths, concentration
risk's high/low-correlation/unreadable-trades paths, the loss-streak
cooldown gate's tuned-read/in-cooldown/streak-broken-by-a-win/fails-open
paths (including a live `confirm_trade_entry` rejection), Shorts'
funding-rate line, and `auto_tune.py`'s new `current_loss_streak`/
`decide_cooldown` functions plus a full subprocess `--apply` run
confirming both parameters land in one `auto_tune.json` without
clobbering each other, both independently respect the 24h cooldown, and
the widened `--bot` choices work. All pass; the existing P4/P5/P6-addon/
P6-payout/P6 Survival Shorts suites re-run clean alongside it (no
regressions from the shared `confirm_trade_entry`/prompt-building
changes).

**Verification**: `python -m py_compile` clean on all 6 touched `.py`
files; `git diff --stat` scoped to exactly the 5 strategy files +
`auto_tune.py` + the new test file. `risk_guard.py`, every bot's
`auto_tune.json` (the per-bot TUNED state, as opposed to the tuner
script itself), `docker-compose.yml`, `backend/`, and `frontend/index.html`
all confirmed untouched -- no new env vars or infra needed for any of
the 5 shipped items. `DeepSeekSurvivalShortsStrategy.py` confirmed
untouched -- none of P8 applies to it per the stated policy.


## P6-addon-2 -- Survival Shorts dashboard card

`frontend/index.html` had a `survival_shorts` group in `GROUP_LABEL`/
`GROUP_ACCENT` (P6-addon, for the credentials panel) but was still
missing from the 4 lookup tables the rest of the dashboard actually
renders off of -- the bot would show up nowhere: not the main tile
grid, not balance history, not the strategy cards.

Added `survival_shorts` to:
- `BOT_PORTS` -- `8150`.
- `BOT_ACCENT` -- `var(--teal, #14b8a6)`, deliberately a different hue
  from Survival Long's purple so the two are visually distinct at a
  glance.
- `BOT_START_WALLET` -- `100` (its real `$100` dry-run wallet).
- `BOT_PARAMS` -- one-line description matching the other bots' style:
  wide `-10%` stoploss, no satellite discount (`SATELLITE_STAKE_
  MULTIPLIER=1.0`), up to 8 positions, same funding-rate veto as Shorts.
- `BOT_STRATEGY` -- full card object (`label`/`kind`/`buys`/`sells`/
  `tags`), wording pulled from the real
  `DeepSeekSurvivalShortsStrategy.py` diff (P6's own entry above) --
  the funding-rate/RSI `confirm_trade_entry` veto, the
  `SURVIVAL_SHORTS_LIVE_MODE` gate, the actual stoploss/position-count/
  multiplier numbers -- not invented.

`tileHtml` (main grid), balance-history's `botIds`, and
`renderStrategyGrid`'s card loop all key off `Object.keys(BOT_PORTS)`/
`Object.keys(BOT_STRATEGY)` directly (confirmed by grep before editing,
not assumed) -- no other file/table needed a matching change for the
bot to actually appear everywhere.

**Verification**: parsed the edited `const` block with Node's
`Function` constructor to confirm valid syntax and that every new key
reads back correctly (`BOT_PORTS.survival_shorts === 8150`, etc.);
`git diff --stat` scoped to exactly this one file.


## P9 -- automatic bot scorecard (all 6 bots, PASS/WATCH/KILL)

**Architecture mismatch caught before writing code, same "verify, don't
trust the spec's file location" discipline as P4.1/P6's frontend-label
finding:** the spec described this as "reads sqlite, pattern from
auto_tune.py" served via a live `GET /api/scorecard` endpoint. Checked
`docker-compose.yml` first -- `survival-agent` only mounts
`survival-agent-data:/data`, none of the `freqtrade*-db` named volumes.
The backend process genuinely cannot open any bot's `tradesv3.sqlite`
directly; `auto_tune.py` itself only works because it's run EXTERNALLY
(operator's own machine/host, given a real `--db` path), never from
inside a container. Resolved the same way: `backend/reports/
bot_scorecard.py` is a new external/manual script, same operational
model as `auto_tune.py` -- NOT live backend code -- and `GET /api/scorecard`
(added to `backend/app.py`) just serves whatever JSON that script last
wrote to `DATA_DIR/scorecard.json`, the same "external writer, backend
reads the file" pattern already used by `/api/balance-history` and
`backend/engine/payout_state.py`.

**`backend/reports/bot_scorecard.py`** (new package, `backend/reports/`):
CLI taking repeatable `--db BOT_ID=/path/to/tradesv3.sqlite` (one per
bot the operator has a copy of/access to), `--days` (default 21),
`--thresholds path.json` (merged over `DEFAULT_THRESHOLDS`, movable per
the spec -- not hardcoded), `--out-json`/`--out-md`.

Per bot: reads closed trades in the window (read-only
`sqlite3.connect("file:...?mode=ro")`, same as `auto_tune.py`), computes
`trade_count`, `win_rate`, `profit_factor` (gross profit / gross loss;
`None` -- not `0` or `inf` -- when there are no losing trades to divide
by), `expectancy_usd` (mean $ profit/trade), `profit_factor_excl_best_trade`
(same ratio with the single largest winning trade removed, guarding
against one lucky outlier), and `max_drawdown_pct` (peak-to-trough on
the cumulative $-profit curve within the window, expressed as % of the
bot's OWN starting wallet -- `BOT_START_WALLET_USD`, duplicated from
`frontend/index.html`'s `BOT_START_WALLET`/`backend/app.py`'s
`DEFAULT_STARTING_CAPITAL` since this script runs outside that process,
same "duplicate rather than share across containers" convention as
everywhere else in this codebase -- NOT the bot's current live balance,
which this script has no access to).

BTC return over the identical window via CoinGecko's free
`coins/bitcoin/market_chart` endpoint (first-vs-last price, no key) --
live-verified the real response shape before coding it
(`{"prices": [[ts_ms, price], ...]}`). Same reliability contract as
P8's market signals: any fetch failure -> `None` (excluded), never a
fabricated `0%`.

Verdict (`PASS`/`WATCH`/`KILL`), thresholds-driven: fewer than
`min_trades_for_verdict` (10) closed trades -> `WATCH` regardless of the
numbers (not enough data to call it either way -- includes the "15/15
wins, profit_factor is undefined because there are no losses yet" case,
deliberately kept as `WATCH` rather than a premature `PASS`);
`profit_factor` below `kill_profit_factor` (1.0) OR `max_drawdown_pct`
above `kill_max_drawdown_pct` (30%) -> `KILL`; `profit_factor` at/above
`pass_profit_factor` (1.3) with drawdown at/below `pass_max_drawdown_pct`
(15%) -> `PASS`; everything else -> `WATCH`.

`survival_shorts` gets the SAME computation and verdict as every other
bot (per the spec: "its own row"), but always carries an extra `"note":
"isolated experiment, not a funding decision yet"` field so a dashboard
or the markdown report never presents its number as a funding-eligible
signal.

Every bot in `ALL_BOTS` always appears in the output, even one the
operator forgot a `--db` for (`WATCH`, "no --db path given for this
bot") or whose db file can't be opened (`WATCH`, real error message) --
never silently dropped from the report.

Output: `build_report()` returns the full JSON (`generated_at`,
`window_days`, `btc_return_pct`, `thresholds` used, `bots: {...}`);
`render_markdown()` turns the same dict into a one-glance table plus a
per-bot detail line, meant to be read once a week as the spec asked, not
parsed by anything.

**`backend/app.py`**: added `SCORECARD_PATH = DATA_DIR / "scorecard.json"`
and `GET /api/scorecard`, which reads and returns that file verbatim,
or an honest `{"available": false, "reason": ...}` if it's missing or
unreadable -- never fabricates a scorecard the operator hasn't actually
generated yet.

**Tests**, new `backtest/test_p9_scorecard.py`, 41 assertions: the
sqlite window/open-trade filtering, every pure math function's normal
and edge cases (no losses, empty input, zero wallet), `decide_verdict`'s
four branches including the undefined-profit_factor case and a
custom-thresholds override actually changing the outcome, `build_report`'s
missing-bot and unreadable-db honesty, `get_btc_return_pct`'s
failure/success/malformed-response paths, `render_markdown`'s content,
and a full subprocess CLI run (including rejecting an unknown `--db`
bot id and a real `--thresholds` override changing a live verdict). All
pass; the P4/P5/P6/P8 suites re-run clean alongside it (this touched no
shared code, so no regression risk was expected, and none was found).

**Verification**: `python -m py_compile` clean on both new/touched `.py`
files; `git diff --stat` scoped to exactly `backend/app.py`'s one small
addition, the new `backend/reports/` package, and the new test file.
`risk_guard.py`, every `DeepSeek*Strategy.py`, `docker-compose.yml`,
`backend/engine/` (untouched -- P9 lives in the new `backend/reports/`
package instead), and every bot's own `auto_tune.json` all confirmed
untouched.


## P10 -- Dip/Moonshot tuning from real observations

**Dip (`DeepSeekDipStrategy.py`):** `minimal_roi` flat `{"0": 0.06}` ->
4-step ladder `{"0": 0.10, "2880": 0.07, "5760": 0.05, "8640": 0.03}`
(keys are Freqtrade's own minutes-since-open convention: 2880=2d,
5760=4d, 8640=6d, down to `MAX_HOLD_DAYS=10`'s existing backstop). A
strong bounce no longer gets cut at a flat +6%; the bar tightens as the
trade ages and the mean-reversion thesis weakens. `config.json`
`max_open_trades`: `3 -> 6`.

**Moonshot (`DeepSeekMoonshotStrategy.py`):** `config.json`
`max_open_trades`: `3 -> 6`. `SYSTEM_PROMPT` gained one sentence
elevating "coin under 10 days old + double-digit % 24h gain on real
volume" to the single strongest setup this bot exists to catch, rather
than one option among several equally-weighted ones -- no entry
threshold changed in code, prompt emphasis only, per the spec.

**`backtest/auto_tune.py`, architectural (not a parameter tweak):**
`LOSS_STREAK_N`/`MAX_COOLDOWN_HOURS` were flat global constants shared
by every bot's offline cooldown tuner (P8 item 5). Dip (contrarian
mean-reversion) and Moonshot (high-variance memecoin hunting) both
treat a multi-loss streak as a normal, expected part of their own
approach more than main/shorts/survival's trend-following one -- they
needed a LONGER streak before the tuner raises their cooldown at all,
and a LOWER cap on how high it can go even when it does. Added
`PER_BOT_LOSS_STREAK_N = {"dip": 5, "moonshot": 6}` and
`PER_BOT_MAX_COOLDOWN_HOURS = {"dip": 24.0, "moonshot": 24.0}`, plus
`loss_streak_n_for(bot)`/`max_cooldown_hours_for(bot)` helpers that
`.get()` into these dicts with the original global constants as the
default -- main/shorts/survival are untouched, confirmed by test
(`loss_streak_n_for("main") == 3`, `max_cooldown_hours_for("survival")
== 48.0`, unchanged).

`decide_cooldown()` took two new keyword params (`loss_streak_n`,
`max_cooldown_hours`) defaulting to the pre-P10 globals -- every
existing call site not touched by `main()` stays byte-compatible.
`read_current_cooldown()` now clamps against `max_cooldown_hours_for(bot)`
instead of the flat global, so a stale value already above dip/moonshot's
tighter 24h cap (e.g. from before this pass) gets pulled back down on
the very next read, not just the next tuned write. `main()` resolves
both per-bot values via the two helpers before calling `decide_cooldown`.

**Noted, not acted on (out of this spec's stated diff scope):** the
strategy-side `LOSS_STREAK_N = 3` class attribute in
`DeepSeekDipStrategy.py`/`DeepSeekMoonshotStrategy.py` (added in P8,
controls how many consecutive losses `_in_loss_streak_cooldown()`
requires before enforcing WHATEVER cooldown value is currently tuned)
is a SEPARATE constant from `auto_tune.py`'s tuner-side one and was
NOT touched here -- the spec's diff range explicitly limited strategy.py
changes to "ROI + prompt phrase" only. Net effect: the tuner now needs
5/6 consecutive losses before RAISING either bot's cooldown value, but
once a cooldown value exists (from an earlier, less strict tuner run,
or a future manual edit), the live strategy still only needs 3
consecutive losses to start ENFORCING it. Not a bug -- these are two
independently-scoped gates that happen to share a similar name and
default value -- but flagged here so it isn't mistaken for an oversight
later.

**Tests**, `backtest/test_p8_market_signals.py` extended (not a new
file -- same suite that already owned `auto_tune.py`'s P8 coverage):
`loss_streak_n_for`/`max_cooldown_hours_for` for all 5 relevant bots,
`decide_cooldown` with dip/moonshot's explicit per-bot args (3-loss
streak does NOT raise dip's cooldown, a 5-loss streak does; moonshot
clamps at its own 24h not 48h), `read_current_cooldown` pulling a stale
40h dip value down to 24h on read, and 3 new full CLI subprocess runs
proving the same end-to-end behavior through `--apply` (including a
pre-seeded near-cap value to prove the CLAMP fires, not just the step).
Plus the Dip ROI ladder's exact dict, Moonshot's new prompt sentence
(and confirmation the OLD 10-90-day guidance is still there, not
replaced), and both configs' `max_open_trades == 6`. All pass; P4/P5/
P6/P9 suites re-run clean alongside it -- no regressions.

**Verification**: `python -m py_compile` clean on all 4 touched `.py`
files; `git diff --stat` scoped to exactly the spec's stated range (2
`config.json`, 2 `strategy.py`, `auto_tune.py`, the test file).
`risk_guard.py`, `docker-compose.yml`, `backend/`, `frontend/`, and
every other bot's own strategy file confirmed untouched.


## P10-fix -- dashboard display catch-up for P10's real changes

P10 changed Dip/Moonshot's actual `max_open_trades` (3->6) and Dip's
`minimal_roi` (flat 6% -> ladder), but `frontend/index.html` and
`DeepSeekDipStrategy.py`'s own module docstring still described the old
numbers -- display-only staleness, no logic touched here.

`frontend/index.html`: `BOT_PARAMS.moonshot`/`BOT_PARAMS.dip` "Up to 3
concurrent positions" -> "Up to 6"; `BOT_STRATEGY.moonshot.tags`/
`BOT_STRATEGY.dip.tags` "Max 3 positions" -> "Max 6 positions"; Dip's
`sells[0]` "+6% profit -> close (tighter...)" replaced with the real
4-step ladder description; Dip's tag "+6% take-profit" -> "ROI ladder
10%→3%".

`DeepSeekDipStrategy.py`: module docstring's "Tighter minimal_roi (6%
vs the other bots' 10%)" bullet rewritten to describe the actual ladder
-- the code itself (the `minimal_roi` dict) was already correct from
P10; only this stale comment was behind.

**Verification**: `python -m py_compile` clean; parsed the edited
`const` block with Node's `Function` constructor to confirm every
changed value reads back correctly. `git diff --stat` scoped to exactly
these 2 files.


## P11 -- two new isolated, rule-based (no LLM) bots

Seventh and eighth bots, both dry-run from day one, both zero-LLM: no
DeepSeek/OpenRouter call, no API bill, no rate-limit/EOL-model exposure
that every `DeepSeek*Strategy.py` bot in this repo has had to work
around at some point (see the rest of this file). Registered
everywhere the existing bots already are, in one pass this time (P6's
own dashboard-card miss, caught and fixed two passes later in
P6-addon-2, is the reason this spec explicitly called that out).

**`freqtrade-listing-fade/` (port 8160):** futures, short-only,
`ListingFadeStrategy.py`. Pool: Bybit USDT perpetuals 2-30 days since
listing (`AgeFilter`). Entry (all three, dataframe-only, cheap): price
down >=25% from `high.cummax()` (approximates the true since-listing
high given the pairlist already caps history to a young coin's actual
lifespan -- no separate fetch needed), latest 1h close below its own
EMA20 (confirms an actual downtrend, not a one-candle wick), and --
checked in `confirm_trade_entry`, not `populate_entry_trend`, same
"cheap dataframe check vs. expensive REST call at order time only"
split as every `DeepSeek*Strategy.py`'s own funding veto -- perpetual
funding not deeply negative (a crowded-short market is exactly the
setup for a squeeze against a late short like this one). Exit: hard
`-12%` stoploss, Freqtrade's native `trailing_stop` once in profit (a
rule strategy doesn't need a custom peak-tracker), 14-day max-hold
backstop via `custom_exit`. 1x leverage, isolated margin, max 4
positions at a fixed $25 stake each, `$100` dry-run wallet.

**`freqtrade-trend-ensemble/` (port 8170):** spot, long-only,
`TrendEnsembleStrategy.py`. Pool: top-20-by-volume pairs
(`VolumePairList number_assets: 20`), **daily** timeframe -- a 5-60 day
Donchian signal checked once a day is honest to what it actually is,
not an attempt to trade a low-frequency signal intraday. Entry: an
ensemble vote across 5 Donchian periods (5/10/20/40/60 days) -- for
each period P, today's close beating the highest `high` of the prior P
days (`rolling(P).max().shift(1)`, shifted so today's own candle is
never counted toward its own breakout level -- verified with a direct
no-lookahead test, not assumed) counts as one vote; enters long only
when >=3 of 5 periods agree. Exit: Freqtrade's native `trailing_stop`
from the trade's own peak profit, `-15%` hard stoploss backstop.
Position sizing is volatility-scaled via `custom_stake_amount`: a pair
less volatile than `TARGET_VOLATILITY_PCT` (5%, 20-day high-low range)
gets a bigger stake (up to `MAX_SIZE_MULTIPLIER`=1.5x), a more volatile
one gets a smaller one (down to `MIN_SIZE_MULTIPLIER`=0.25x) -- same
dollar risk exposure instead of the same dollar stake regardless of how
much a pair actually moves. `max_open_trades: 5` and the pairlist's
`min_days_listed: 30` are this session's own reasonable defaults, not
explicitly specified in the spec.

**Both bots**: own Bybit keys (`BYBIT_FADE_API_KEY/SECRET`,
`BYBIT_TREND_API_KEY/SECRET`), own double live-gate
(`FADE_LIVE_MODE`/`TREND_LIVE_MODE`, checked in `confirm_trade_entry`
against `self.config.get("dry_run", True)`) -- identical
belt-and-suspenders pattern to Survival Shorts' own gate, real money
untouched until an operator manually flips both Freqtrade's own
`FREQTRADE_*__DRY_RUN` AND the bot-specific flag. No
`SURVIVAL_MONTHLY_USD`/second-opinion/DeepSeek env vars piped in at all
-- these bots have no LLM prompt to feed survival-framing into and no
ensemble to shadow-log.

**Registered in every place the existing bots already are, in one
pass** (per the operator's own explicit "not like P6-addon-2, where the
card got forgotten" instruction):
- `docker-compose.yml`: both services, own DB volumes, own ports.
- `backend/engine/freqtrade_client.py`'s `BOTS` dict (`listing_fade`,
  `trend_ensemble`).
- `backend/app.py`'s `ENV_VAR_DEFS`: 4 entries each (key/secret,
  dry-run toggle, live-mode flag), same shape as Survival Shorts'.
- `frontend/index.html`: `BOT_PORTS`, `BOT_ACCENT` (orange/cyan,
  visually distinct from every other bot's color), `BOT_START_WALLET`,
  `BOT_PARAMS`, full `BOT_STRATEGY` cards, AND `GROUP_LABEL`/
  `GROUP_ACCENT`/`GROUP_ORDER` (the credentials-panel side, which is
  specifically the set of tables P6-addon-2 had to patch in separately
  last time) -- verified as one Node `Function`-constructor parse of
  the whole edited block, not five separate spot-checks.
- `backend/reports/bot_scorecard.py`: added to `ALL_BOTS` and
  `BOT_START_WALLET_USD`. The single `SURVIVAL_SHORTS_NOTE` string
  constant was generalized into an `ISOLATED_EXPERIMENT_NOTES` dict
  (three call sites that used to say `if bot_id == "survival_shorts"`
  now say `if bot_id in ISOLATED_EXPERIMENT_NOTES`) so a THIRD isolated
  bot means one dict entry, not a fourth copy-pasted `if` branch.
- **Deliberately NOT added** to `backend/engine/payout_state.py`'s
  `SPLIT_BOTS` -- both are isolated experiments, same status as
  `survival_shorts`, confirmed by an empty `git diff` on that file.

**Tests**: new `backtest/test_p11_new_bots.py`, 28 assertions -- NOT a
historical backtest, a check that the rule code does what it claims on
synthetic, hand-built price series. Listing Fade: `cummax`-based listing
high, the EMA20/percent-from-high entry logic (fires on a real
pump-then-dump, does NOT fire on a flat series or on a series that
dumped but already recovered back above its EMA20), the funding veto's
three paths (rejects deeply negative funding, allows mild funding, fails
OPEN on a fetch error), the short-only guard (`side != "short"` ->
reject), the live-mode gate, and the 14-day `custom_exit` backstop.
Trend Ensemble: the 5-period vote count on a real breakout, the
no-lookahead property of `shift(1)` proven directly (a huge spike's own
high does not appear in its own Donchian level), entry firing at >=3
votes and not firing on a flat series, the volatility-sizing multiplier
clamped at both bounds plus its `min_stake` floor, the unknown-pair
fallback to `proposed_stake`, and the live-mode gate. `test_p9_scorecard.py`
extended with 4 new checks confirming both bots are registered and
carry their isolated-experiment note. All pass; every other suite
(P4/P5/P6/P8/P9) re-run clean alongside it -- no regressions.

**Verification**: `python -m py_compile` clean on all 4 new/touched
`.py` files; `docker-compose.yml` parsed with `yaml.safe_load` (9
services, 8 DB volumes, correct ports); the full edited `frontend/index.html`
`const` blocks (`BOT_*` and `GROUP_*`) parsed with Node's `Function`
constructor to confirm every new key reads back correctly. `git diff
--stat` scoped to exactly: `docker-compose.yml`,
`backend/engine/freqtrade_client.py`, `backend/app.py`,
`backend/reports/bot_scorecard.py`, `frontend/index.html`, the P9 test
file, plus the two new bot directories and the new P11 test file.
`risk_guard.py`, every `DeepSeek*Strategy.py`, and
`backend/engine/payout_state.py`'s `SPLIT_BOTS` all confirmed
untouched.


## P12 -- Dip's silent no-entries bug, root MECHANISM found in real Freqtrade source (not guessed)

Symptom (operator-confirmed, two live logs, ~36h): DeepSeek said "buy"
107 times, zero new trades, zero ERROR/WARNING/"rejecting" lines. No
docker/Portainer access exists in this sandbox, so items 1/2/4/5 of the
spec (live balance, image build date, live env diff, `show-trades`) could
NOT be checked here and still need the operator to run them -- see
"Still needed from the operator" below. What COULD be done without
server access: `pip download freqtrade --no-deps` (freqtrade 2026.8,
matching `freqtradeorg/freqtrade:stable`) and read the REAL installed
source for the exact code path a "buy" signal goes through, instead of
guessing at Freqtrade's internals.

**Confirmed mechanism, from `freqtrade/freqtradebot.py` and
`freqtrade/wallets.py` (2026.8), not speculation:**

1. `freqtradebot.py:926` -- `execute_entry()`: `if not stake_amount:
   return False` -- **zero logging** before this return. This alone
   already matches "no ERROR, no rejecting" perfectly.
2. `wallets.py:410-423` -- `validate_stake_amount()`: if the computed
   stake is falsy/`<= 0`, it logs `"Stake amount is {stake_amount},
   ignoring possible trade for {pair}."` at **`level="debug"`** --
   invisible at this project's normal (INFO) log verbosity. This is the
   "single scenario where real Freqtrade genuinely logs nothing" the
   operator's own spec predicted, now confirmed against the actual source
   rather than assumed.
3. `wallets.py:349-361` -- `_calculate_unlimited_stake_amount()`, the
   function `"unlimited"` `stake_amount` mode (Dip's own config.json
   setting) actually runs: `possible_stake = (available_amount +
   val_tied_up) / max_open_trades`, then **capped at
   `available_amount`** via `min(possible_stake, available_amount)`.
   If `available_amount` (free stake-currency balance right now) is at
   or near zero -- e.g. the wallet is fully tied up in open positions
   that haven't closed -- the result is ~0 regardless of
   `val_tied_up` or `max_open_trades`, and step 2 above silently
   discards every subsequent "buy" with a debug-only log line.

**Leading hypothesis, consistent with every observed fact:** Dip's
dry-run wallet is effectively fully committed to already-open positions
(the operator's own "6 trades on the dashboard are old, from before
this window" -- if those 6 are still OPEN, not closed, they'd occupy
the whole wallet under `"unlimited"` sizing) -- `available_amount ≈ 0`,
every new "buy" computes a stake of ~0, and Freqtrade throws it away at
DEBUG level. This is a real Freqtrade behavior interacting badly with
`stake_amount: "unlimited"` plus stuck/never-resolving open trades --
NOT a bug introduced by P8/P10's own code changes, though P10 raising
`max_open_trades` 3->6 could not have caused this on its own (the
divisor change doesn't matter when the numerator's cap is already 0).
Not yet PROVEN without live balance/open-trade data -- see below.

**P12 diagnostic patch, applied and deployable now** --
`freqtrade-dip/user_data/strategies/DeepSeekDipStrategy.py`'s
`populate_entry_trend()`, right after `if decision == "buy":`: logs
`self.wallets.get_available_stake_amount()`, `Trade.
total_open_trades_stakes()`, open-trade count, and the exact
"unlimited"-mode `possible_stake` Freqtrade's own formula would compute
-- at **INFO** level (not DEBUG), so it surfaces without touching this
container's global log verbosity. Marked `# P12 TEMPORARY DIAGNOSTIC --
remove once the silent-no-entries bug is confirmed/fixed` -- meant to
be redeployed once, watched for the next "buy" signal, then removed
once the real numbers are seen (per the spec's own item 3 ask), not left
in permanently.

**Still needed from the operator (this sandbox has no docker/Portainer
access, confirmed):**
1. Dip's real available balance right now (`GET /api/v1/balance` via
   FreqUI/API, or `docker exec freqtrade-dip freqtrade show-config`).
2. Whether the "6 old trades" are still OPEN or already closed
   (`docker exec freqtrade-dip freqtrade show-trades` or the dashboard
   itself) -- this is the single fact that would confirm or kill the
   leading hypothesis outright.
3. Redeploy with this patch, wait for the next "said buy" log line,
   read the new P12 DIAGNOSTIC line that follows it.
4. Image build date / `docker inspect freqtrade-dip` vs. `git log`
   (`1b66109` P10, `4733d69` P10-fix, this commit) -- confirm the
   running container actually has this code before trusting its logs.

**Verification of what WAS done here**: `python -m py_compile` clean;
`git diff --stat` scoped to exactly this one file; `backtest/
test_p8_market_signals.py` re-run clean (loads this same strategy
module, confirms the diagnostic addition didn't change `minimal_roi`/
config behavior). No fix applied yet -- deliberately not guessing at a
code change (e.g. switching `stake_amount` away from `"unlimited"`)
before the operator's live numbers confirm which of the two `wallets.py`
zero-stake paths is actually firing.


## P12-fix -- max_open_trades raised to 20 for Main/Shorts/Moonshot/Dip/Trend Ensemble

Operator instruction: raise `max_open_trades` to 20 for main, shorts,
moonshot, dip, trend_ensemble (Survival Long, Survival Shorts, and
Listing Fade not named, left untouched). Was: main/shorts/trend_ensemble
5, moonshot/dip 6 (P10).

**Flagged, not blocking**: all 5 configs use `stake_amount: "unlimited"`,
which Freqtrade computes as `(available + tied_up) / max_open_trades`
(see P12's own entry above, sourced from the real Freqtrade 2026.8
code). Raising the divisor to 20 shrinks the per-trade stake
proportionally for all five bots, and worsens the exact
silent-zero-stake risk P12 just diagnosed for Dip if any of these bots'
wallets are similarly close to fully tied up. Not reverted or
second-guessed -- an explicit operator instruction -- but the
interaction is real and worth knowing before assuming the P12 fix (once
diagnosed) and this change don't compound.

`backtest/test_p8_market_signals.py`'s Dip/Moonshot `max_open_trades`
assertions updated from 6 to 20 to match.

**Verification**: all 5 `config.json` files valid JSON, confirmed
`max_open_trades == 20`; full test suite (P4/P5/P6/P8/P9/P11) re-run
clean, zero regressions. `git diff --stat` scoped to exactly the 5
configs plus the one test file.


## P12-fix-2 -- max_open_trades reverted to 10 (Dip stays 20) + per-bot UI control

Operator instruction: revert `max_open_trades` from 20 back to 10 for
main, shorts, moonshot, trend_ensemble. Dip explicitly stays at 20.
Also: add a UI capability to change trade count per bot.

**Config revert**: `freqtrade/`, `freqtrade-shorts/`,
`freqtrade-moonshot/`, `freqtrade-trend-ensemble/` `config.json` ->
`max_open_trades: 10`. `freqtrade-dip/config.json` left untouched at 20
per instruction. `backtest/test_p8_market_signals.py`'s Moonshot
assertion updated from 20 to 10 to match; Dip's own assertion (20)
already correct, untouched.

**docker-compose.yml**: added one `FREQTRADE__MAX_OPEN_TRADES=${<BOT>_
MAX_OPEN_TRADES:-<default>}` line per bot service (all 8, including
Survival Long/Shorts and Listing Fade which weren't named in P12-fix),
each defaulting to that bot's own config.json value. This is Freqtrade's
own documented `FREQTRADE__{KEY}` env-override convention (confirmed
against the real freqtrade 2026.8 source in the P12 entry above) --
setting e.g. `MAIN_MAX_OPEN_TRADES=15` in Portainer's stack env and
redeploying overrides the value with no image rebuild needed.

**UI control -- tension with "Credentials panel became read-only"**:
this repo has a hard-won doctrine (see that entry above) against adding
write endpoints for anything in `ENV_VAR_DEFS`, because an earlier fully
-editable credentials UI had zero real effect (Freqtrade reads config
from Portainer's own stack env vars via docker-compose, not from this
app's `data/.env`) and silently drifted -- that's why `/api/credentials`
write endpoints and `/api/portainer-env-block` were removed entirely.

The operator's request to edit trade count from this UI runs directly
into that lesson. Resolved by following the ONE precedent that survived
that cleanup instead of reintroducing the original pattern:
`/api/live-trading`'s "record intent, but say loudly every time that
real behavior requires the matching Portainer env var + redeploy"
model. New `GET`/`POST /api/max-open-trades` in `backend/app.py`:
- `GET` returns, per bot, `env_var` name, `recorded_intent` (from
  `data/.env`, `None` if never set) and `default_in_repo` (that bot's
  own config.json/docker-compose.yml default).
- `POST {bot_id, max_open_trades}` validates `bot_id` is one of the 8
  known bots and `max_open_trades >= 1`, writes it to `data/.env` via
  `set_key`, and returns a `note` field spelling out that this does NOT
  change the bot's real behavior until the same value is also set in
  Portainer's stack Environment Variables and the stack is redeployed.
- This is a deliberate, narrow exception -- not a reopening of the
  removed pattern. It never touches a live value, never claims the
  saved number is in effect, and repeats the Portainer/redeploy
  requirement on every save, matching `/api/live-trading` exactly.

`frontend/index.html`: new "Max open trades per bot" section (amber-
bordered like the trading-mode section, red warning paragraph up top
explaining the same "does NOT change real behavior" caveat and pointing
at this AGENTS.md entry), one number input + Save button per bot,
`loadMaxOpenTrades()`/`saveMaxOpenTrades()` wired into the initial-load
sequence alongside `loadTradingMode()`.

**Verification**: `docker-compose.yml` parsed with `yaml.safe_load`,
confirmed exactly one `FREQTRADE__MAX_OPEN_TRADES` line per service with
correct bot-specific env var name and default. `backend/app.py` compiles
clean (`python -m py_compile`). `frontend/index.html`'s inline `<script>`
extracted and checked with `node --check` -- syntax OK. New
`backtest/test_p12_max_open_trades.py` (`fastapi.testclient.TestClient`
against a temp `DATA_DIR`) exercises both endpoints: constants cover all
8 bots, GET reflects `None`/default before any save, POST accepts valid
saves and rejects unknown `bot_id` / `max_open_trades < 1` with 400, GET
reflects a save without touching other bots' state -- all pass. Full
suite (P4/P5/P6/P8/P9/P11/this file) re-run clean, zero regressions.
`git diff --stat` scoped to exactly: the 4 reverted config.json files
(Dip's own untouched), `docker-compose.yml`, `backend/app.py`,
`frontend/index.html`, `backtest/test_p8_market_signals.py`, plus this
AGENTS.md entry and the new test file.


## P13 (Phase 1, MVP) -- Dip conviction-based sizing + full rotation of weakest position

Written and kept in repo per operator instruction, NOT deployed -- rolls
out after an evening review of the 20-slot limit. Scope: only
`freqtrade-dip/user_data/strategies/DeepSeekDipStrategy.py`. Dry-run
only. No other bot touched.

**Motivation**: 25-26/09 found Dip missing buy signals because all
slots were filled with equal-size positions with no notion of signal
strength and no way to free capital for a stronger new signal.
Freqtrade has neither mechanism natively.

**Conviction score**: `SYSTEM_PROMPT` now asks for a trailing
`| confidence: <0-100, nearest 10>` field alongside the existing
`buy:`/`hold:` line. New module function `_parse_confidence()` pulls it
out via regex; missing/non-numeric/out-of-range -> fail-safe (50,
degraded=True), logged as a warning, buy/hold decision itself untouched
-- confidence parsing can never affect whether a trade is taken, only
how big.

**Sizing**: `entry_tag` now encodes `<core|sat>:<confidence>` (e.g.
`sat:80`) instead of just the tier, set once at the moment
`populate_entry_trend` decides to buy. `custom_stake_amount()` reads it
back via `_parse_tag_conviction()` (returns `None` for anything
malformed, including every trade opened before this change -- those
fall back to exactly the pre-P13 sizing, no guessed confidence).
`_confidence_stake_multiplier()`: 80-100 -> x1.3, 60-79 -> x1.0, 40-59 ->
x0.7; a confidence <40 reaching this point (the buy filter is supposed
to reject those first) falls back to the 0.7 coefficient rather than an
untested lower one, with a warning logged so a filter gap would be
visible. Explicit `available_stake_amount` cap applied after the
multiplier, before `min_stake` flooring, per the spec's "never exceed
available_stake" requirement.

**Rotation (full close of the weakest open position)**: real Freqtrade
source (2026.8, same wheel downloaded for P12/verified again here) shows
`get_free_open_trades()` is checked in `process()` BEFORE
`enter_positions()`/`create_trade()` are ever called, and `create_trade`
re-checks the same count before `execute_entry()` -- so
`confirm_trade_entry()` simply never fires while slots are full; a
rotation triggered from that hook would never run. Rotation logic
therefore lives in `populate_entry_trend()` itself (always runs,
slot-count-independent), executed BEFORE the entry signal is set:
- `_find_rotation_candidate(new_pair, new_confidence)`: reads this bot's
  own open trades via `Trade.get_trades_proxy(is_open=True)` (each bot
  has its own sqlite db -- no cross-bot leakage possible), parses each
  one's entry conviction from its `enter_tag`, excludes anything
  unparseable (logged) or already at/above `ROTATION_PROTECTED_PROFIT_RATIO`
  (10%, left to close naturally via the ROI ladder), fetches each
  candidate's live price via `self.dp.ticker(pair)` (real, documented
  Freqtrade DataProvider method, verified in the same wheel) to compute
  unrealized P&L, and picks the lowest (conviction, pnl) pair. Requires
  the new signal to beat it by `ROTATION_MIN_EDGE_POINTS` (25) or
  returns `None` (signal skipped, now logged at INFO instead of
  silently, per the spec).
- `_forceexit_trade()`: closes the chosen trade via a loopback REST call
  to this bot's own `POST /api/v1/forceexit {"tradeid": <id>}` (real,
  documented endpoint + payload shape, confirmed against
  `api_trading.py`/`api_schemas.py` in the same wheel), using
  `self.config["api_server"]` for the base URL and Basic Auth --
  there's no direct in-process way to force-close a position from
  strategy code, and going through the bot's own REST API is the real,
  safe mechanism rather than mutating the DB or exchange state directly.
  Because the DB is updated synchronously by the time this call
  returns, a freed slot IS visible to `enter_positions()` later in the
  same iteration. Any failure (network, non-2xx) logs a warning and
  aborts the rotation -- the new signal is simply not taken that cycle,
  never retried in a loop.
- Frequency limit: `rotation_state.json` in the bot's own persisted db
  volume (`/freqtrade/user_data/db/`, same volume as `tradesv3.sqlite`)
  stores `last_rotation_ts`; `_rotation_allowed_now()` enforces
  `ROTATION_MIN_INTERVAL_HOURS` (6h). Unlike `auto_tune.json`
  (external-writer, this bot only reads), this file is written by the
  bot itself -- chosen so the cooldown survives a container restart,
  same durability guarantee the trade database already has.
- Resilience: any exception anywhere in the rotation path (trade-history
  read, ticker fetch, pnl calc, REST call) is caught, logged, and
  degrades to "signal not taken this cycle" -- never raises out of
  `populate_entry_trend` and never falls back to force-closing something
  by guesswork.

**Explicitly not done (per spec's own scope)**: `_second_opinion.py`
(P5's shadow-mode ensemble vote) was NOT touched -- it never influences
the actual buy/hold decision or sizing, so its vote doesn't need a
confidence field for this MVP, and the spec's own scope section names only
`DeepSeekDipStrategy.py`. No other bot touched. `confirm_trade_entry`
unchanged beyond its existing loss-streak-cooldown gate.

**Verification**: `python -m py_compile` clean. New
`backtest/test_p13_conviction_rotation.py` loads the real strategy file
standalone via the same freqtrade-stubbing technique as
`backtest/llm_replay.py`, and covers: confidence parsing (valid,
case-insensitive, missing, out-of-range, negative, non-numeric, empty),
tag parsing (valid/malformed/legacy-no-tag), the sizing multiplier's 4
bands including the <40 fallback, `custom_stake_amount`'s
available-stake cap and its no-tag fallback, `_find_rotation_candidate`
(no open trades, a beatable weak position, a protected >=10% position,
an edge too small to act on, a malformed-tag trade correctly excluded
while a valid one is still found, a conviction tie broken by more
negative pnl, and a ticker-fetch failure that excludes rather than
crashes), the rotation cooldown file across "no file yet" / "just
recorded" / "7h old" states, and `_forceexit_trade` on both a
successful and a failing loopback call (right URL/payload/auth, state
recorded only on success). 39/39 checks pass. Full suite
(P4/P5/P6/P8/P9/P11/P12/P13) re-run clean, zero regressions. `git diff
--stat` scoped to exactly `DeepSeekDipStrategy.py` plus the new test
file -- no other bot, no config, no docker-compose, no backend/frontend
touched.

**Not yet done, per spec's own 2-3 week success criteria**: no deploy, no
`auto_tune.py` wiring for the sizing coefficients (explicitly "tuned
later"), no P9-scorecard tagging of this as a separate experiment yet
-- all deliberately deferred to the operator's own rollout timing.


## P13-fix -- second rotation trigger: capital exhausted even with free slots

Same scope as P13: only `freqtrade-dip/user_data/strategies/
DeepSeekDipStrategy.py`. Phase 1's action mechanism
(`_forceexit_trade`, `_find_rotation_candidate`, the 6h cooldown, the
>=10% profit protection) unchanged, as required by the spec.

**Motivation**: live Dip logs 27/09 ~17:00 showed `open_trades=10/20`
(slots free) but `tied_up=$498.17` of $500, `available=$3.28` -- below
the exchange's own `min_stake` ($5.53). Confidence 65-70 signals arrived
but rotation never triggered, because Phase 1's only condition was
`open_trades == max_open_trades`. Same class of lost-signal bug as P12,
different cause (capital, not slots).

**Where the check lives, and why it's not in `populate_entry_trend`
alongside slots_full**: the spec's own pseudocode suggests one unified
`if (slots_full or capital_exhausted) and confidence...` check, but
Freqtrade only hands the strategy an authoritative `min_stake` for a
pair inside `custom_stake_amount()` (computed via
`self.exchange.get_min_pair_stake_amount()` on the bot side, never
exposed to strategy code directly -- confirmed against the same
freqtrade 2026.8 source already used for P12/P13). Checking
capital_exhausted any earlier would mean hardcoding a guessed minimum
notional instead of using Freqtrade's real one -- exactly the kind of
unverified guess this project avoids. `custom_stake_amount()` is
confirmed reachable even when the computed stake is far below minimum
(P12 already established `proposed_stake` is passed in regardless, and
Freqtrade only discards the trade *after* this hook returns), so the
new trigger lives there instead. `entry_tag` (with the P13 confidence)
was already a parameter of this hook, so no new plumbing was needed to
get the confidence value here.

**Shared entry point**: new `_attempt_rotation(new_pair, new_confidence,
trigger)` factors out exactly what `populate_entry_trend`'s slots_full
branch already did (cooldown check -> `_find_rotation_candidate` ->
`_forceexit_trade`) into one method both trigger sites call --
satisfies the spec's "one common entry point" without touching any of the
Phase 1 internals it wraps. `populate_entry_trend`'s own block is now a
single line: `take_entry = self._attempt_rotation(pair, confidence,
trigger="slots_full")`.

**capital_exhausted trigger** (in `custom_stake_amount`): fires only
when `available_stake_amount() < min_stake` (both real, Freqtrade-
supplied numbers) AND `confidence >= CAPITAL_EXHAUSTED_CONFIDENCE_
THRESHOLD` (65 -- same floor the spec names, roughly the level that
already clears the buy filter). No entry_tag / unparseable confidence
never attempts it -- falls through to the pre-fix behavior unchanged
(floors the stake up to `min_stake` regardless of actual availability,
exactly as before this fix; that stale behavior is itself what
Freqtrade's own downstream wallet check silently absorbs, per the P12
finding -- not touched here since fixing it isn't in scope). On a
successful rotation, capital freed mid-call makes the original `stake`
value stale (it was sized off the too-small `proposed_stake` Freqtrade
computed before this call), so the rescued entry is sized at
`min(max(stake, min_stake), <freshly re-read available>)` -- at least
the exchange minimum, never more than what's actually free. If the
freed capital is STILL below `min_stake` (the closed position was
itself small), logs the exact required message and returns `0.0` --
explicitly does not attempt a second closure in the same cycle, per the
spec's own "not P13b" boundary.

**Logging**: new `P13 ROTATION: trigger=capital_exhausted (available=
$X < min=$Y) for <pair> (confidence=Z)` line fires before the shared
rotation attempt, so log review can immediately tell slots vs capital
apart. The existing `P13 ROTATION: closed ... to make room for ...`
success line (Phase 1, unchanged text) and the shared cooldown-skip log
(now also unchanged, just centralized in `_attempt_rotation`) cover the
rest.

**Resilience**: the entire capital_exhausted block is one try/except --
any failure (wallet read, rotation error) logs a warning and falls
through to the pre-fix floor-to-min_stake behavior, never raises out of
`custom_stake_amount`.

**Verification**: `python -m py_compile` clean. New `backtest/
test_p13fix_capital_exhausted.py` (same freqtrade-stubbing load as
`test_p13_conviction_rotation.py`) covers: confidence below threshold
-> old behavior; no entry_tag -> old behavior; successful rotation with
sufficient freed capital -> correctly sized entry; successful rotation
still short of minimum -> 0.0; no rotation candidate -> 0.0; forceexit
REST failure -> 0.0; available already sufficient -> trigger never
fires, normal conviction sizing applies; an exception mid-check ->
graceful fallback, no crash. 9/9 pass. Re-ran `test_p13_conviction_
rotation.py` (Phase 1's own suite, unaffected by the `_attempt_rotation`
refactor) -- 39/39 still pass. Full suite (P4/P5/P6/P8/P9/P11/P12/
P13/P13-fix) re-run clean, zero regressions. `git diff --stat` scoped
to exactly `DeepSeekDipStrategy.py` plus the new test file.

Still not deployed -- same as P13 Phase 1, waiting on the operator's own
rollout timing after the evening slot-limit review.


## P15 -- P9 scorecard visibility on the dashboard

Display only, per the spec's own explicit boundary -- no automation acts
on the verdict (that's the deferred P14). Only `frontend/index.html`
touched; `/api/scorecard` already existed and needed no backend change.

New "P9 scorecard" section (`frontend/index.html`, right after "Bot
details (live)"): one tile per bot with a colored verdict badge (green
PASS, amber WATCH, red KILL, gray "no data" when the bot is missing
from the report or has no `verdict` field -- e.g. a bot the operator
hasn't passed a `--db` for yet). Clicking a badge toggles a detail line
(trade_count, profit_factor, expectancy_usd, max_drawdown_pct,
verdict_reason) already present in the same `/api/scorecard` response --
no second request. When the scorecard hasn't been generated yet at all
(`{"available": false, "reason": ...}`, no `bots` key), the whole
section shows that reason as plain text instead of per-bot tiles.
`loadScorecard()` is wired into the existing `refreshLiveData()` 30s
poll cycle alongside `loadFreqtradeSummary()` etc. -- no new polling
loop.

**Verification**: inline `<script>` extracted and checked with `node
--check` -- syntax OK. Also verified live in the browser (not just
syntax-checked, since this is a genuine dashboard UI change): ran
`backend/app.py` locally against a temp `DATA_DIR` seeded with a
hand-built `scorecard.json` covering PASS/WATCH/KILL, confirmed all
three badge colors render correctly and a bot absent from the JSON
shows "no data"; clicked a badge via a DOM `.click()` call and confirmed
its detail `<div>` toggles to `display:block`; removed `scorecard.json`
and confirmed the section falls back to the "no scorecard generated
yet" message. (The FastAPI app hardcodes `/app/frontend/index.html` for
its root route and `/app/frontend` for its `/assets` mount --
Docker-image-only paths -- so local verification required pre-creating
that directory and copying this file into it; this is a pure
local-verification workaround, no code change.) Full backtest suite
(P4/P5/P6/P8/P9/P11/P12/P13/P13-fix) re-run clean, unaffected by a
frontend-only change. `git diff --stat` scoped to exactly
`frontend/index.html`.


## P16 -- Dip stale-signal rotation (P16a) + rotation anti-chop (P16b)

Two related pieces, both about not cutting positions too fast/noisily.

**P16a** (`freqtrade-dip/user_data/strategies/DeepSeekDipStrategy.py`):
Dip had no "is the thesis still alive" check at all before this -- only
`MAX_HOLD_DAYS=10` in `custom_exit`. New, independent exit path: caches
every cycle's decision (`_latest_decision[pair] = (decision, reason,
confidence)`, the P13 3-tuple, cached unconditionally right after the
`_ask_deepseek` call regardless of buy/hold) and, once a trade has been
open at least `STALE_SIGNAL_MIN_HOLD_HOURS` (4h), tracks consecutive
non-"buy" candles per trade in `_stale_streak[trade.id]`. Only after
`STALE_SIGNAL_STREAK_REQUIRED` (2) CONSECUTIVE non-"buy" candles does it
return `"rotate_stale_signal"` -- one noisy "hold" candle no longer
force-exits a position DeepSeek is still bullish on a candle later.
`MAX_HOLD_DAYS` is checked first and unchanged, still the hard backstop
(and clears the streak entry when it fires). New `confirm_trade_exit`
clears `_stale_streak[trade.id]` on any close, so a later trade never
inherits a stale count. Entirely independent of P13/P13-fix's own
rotation: that lives in `populate_entry_trend` (entry side, triggered by
a NEW incoming signal needing room), this lives in `custom_exit` (exit
side, triggered by the EXISTING position's own thesis fading) -- either
can close a trade first, no interaction.

**Fixed during implementation** (own bug, not the spec's): the spec's own
pseudocode referenced `self.STALE_SIGNAL_MIN_HOLD_HOURS` /
`self.STALE_SIGNAL_STREAK_REQUIRED`, but per the spec's own placement
instruction ("constants next to the existing ROTATION_*") these are
MODULE-level constants in this file, exactly like `ROTATION_MIN_EDGE_
POINTS` etc. above them -- which the existing code already reads as bare
names, never `self.`-prefixed. Used `self.X` for a module-level name the
first pass, caught by the new test file failing to even instantiate
(`AttributeError`), fixed to bare `STALE_SIGNAL_MIN_HOLD_HOURS`/
`STALE_SIGNAL_STREAK_REQUIRED` references to match.

**P16b** (`freqtrade/user_data/strategies/DeepSeekStrategy.py`,
`freqtrade-shorts/.../DeepSeekShortsStrategy.py`,
`freqtrade-survival/.../DeepSeekSurvivalStrategy.py`,
`freqtrade-survival-shorts/.../DeepSeekSurvivalShortsStrategy.py`):
these four already had a stale-signal rotation, but it fired on the
FIRST non-"buy" (or, for the two direction-aware short bots, first
invalid-for-direction) candle after `MIN_HOLD_HOURS`. Same anti-chop fix
as P16a: `MIN_HOLD_HOURS` raised 2 -> 4 in all four (class attribute,
unlike Dip's module-level constants -- matches how these four already
used `self.MIN_HOLD_HOURS`), new `STALE_SIGNAL_STREAK_REQUIRED = 2`
class attribute alongside it, new `self._stale_streak: dict[int, dict]
= {}` in each `__init__`, and each `custom_exit` rewritten to the same
per-candle streak-counting pattern as P16a (keyed by `trade.id`, keyed
within that by candle date to avoid double-counting since `custom_exit`
fires more than once per candle). Each bot's own pre-existing, unrelated
exit logic is untouched and still checked first: Survival's sub-cap
kill switch (`self.is_live_mode() and self._check_kill_switch()`) still
short-circuits before the streak check, exactly as before. Shorts and
Survival Shorts keep their existing direction-aware `still_valid =
(decision == "sell_short") if trade.is_short else (decision == "buy")`
check unchanged, just wrapped in the streak counter instead of firing
immediately. None of the four had a pre-existing `confirm_trade_exit`
(no Moonshot-style hook to fold into) -- added fresh to each, same
"clear the streak on any close" job as P16a's.

**Left alone, out of P16's own scope**: `backtest/llm_replay.py` (the
offline historical-replay tool) still simulates the OLD single-candle
rotation rule (`if hours_open >= MIN_HOLD_HOURS and decision != "buy"`)
even though it reads the real, now-raised `MIN_HOLD_HOURS` value
dynamically from the class. It's a separate analysis tool, not a
strategy file, and wasn't named in this spec's scope -- its replay results
will just be conservative (rotate sooner than the real bot now does)
until it's updated in a future task.

**Verification**: all 5 files `python -m py_compile` clean. New
`backtest/test_p16_stale_signal_streak.py` loads all 5 real strategy
files standalone via the same freqtrade-stubbing technique as
`test_p13_*`/`llm_replay.py`, and covers: Dip's constants, MAX_HOLD_DAYS
still taking priority over and clearing the streak, the 1st-candle-no-
exit/2nd-candle-fires streak progression, same-candle-date calls not
double-counting, a decision flip back to "buy" resetting the streak to
0, no cached decision defaulting to "buy" (never exits on missing
data), a `dp` failure degrading gracefully, and `confirm_trade_exit`
clearing state; then, for each of the 4 P16b bots, the raised
`MIN_HOLD_HOURS`, the streak requiring 2 candles instead of 1, and (for
Survival) the kill switch still short-circuiting before the streak
check, and (for Shorts/Survival Shorts) both trade directions. 38/38
pass. Full suite (every `backtest/test_p*.py` plus `test_payout_state.py`)
re-run clean, zero regressions. `git diff --stat` scoped to exactly the
5 strategy files, the new test file, and `.gitignore` (a small, already-
pending `.mcp.json` entry from a separate, earlier task, folded into
this commit rather than left uncommitted).


## Listing Fade -- trailing-stop dead zone + capital-lock fix

Confirmed by real logs 28/09 14:01: real short signals (PONS -46%,
LONGXIA -78%, STONK -37%, 4STOCK -82%, all valid close<EMA20 fires)
were lost to `Available balance ... is lower than stake amount 25 USDT`
-- `4 x $25 = $100` was exactly 100% of `dry_run_wallet`, zero buffer for
fees/slippage/price movement between signal and fill, so balance
regularly dipped below one slot's cost. Second, independent factor:
`trailing_stop_positive_offset = 0.06` meant a trade reversing before
+6% had NO protection at all and could ride to the full -12% stoploss.

Two independent one-line fixes, same commit (no conflict between them):
- `ListingFadeStrategy.py`: `trailing_stop_positive_offset` 0.06 -> 0.03
  -- trailing now activates at +3%, halving the unprotected drawdown
  window. `trailing_stop_positive` (0.03) and `stoploss` (-0.12)
  untouched.
- `config.json`: `max_open_trades` 4 -> 3 -- `3 x $25 = $75` of the $100
  `dry_run_wallet`, freeing ~$25 buffer so a real signal isn't blocked
  by a balance that's a few dollars short of one slot. `stake_amount`
  (25) and `dry_run_wallet` (100) untouched.

**Verification**: `grep` confirms both new values in place. JSON valid
(`json.load`), strategy file compiles (`py_compile`). Full backtest
suite (every `backtest/test_p*.py` plus `test_payout_state.py`) re-run
clean -- no test hardcoded either changed value. `git diff` scoped to
exactly these two lines in the two named files.

**Not yet verifiable from this sandbox** (per the spec's own post-deploy
checklist -- needs live logs after redeploy): whether short signals stop
being blocked by an insufficient-balance error with <3 open trades, and
whether fewer trades ride to the full -12% stoploss without ever
touching the trailing stop. Both are the operator's own follow-up.


## Listing Fade fix-2 -- trailing_stop_positive_offset must exceed trailing_stop_positive

Real deploy failed immediately: `Configuration error: The config
trailing_stop_positive_offset needs to be greater than
trailing_stop_positive in your config.` Confirmed against real Freqtrade
2026.8 source (`configuration/config_validation.py`): `if tsl_positive >
0 and 0 < tsl_offset <= tsl_positive: raise ConfigurationError(...)` --
strict `>` required, `<=` (including equal) is rejected. The prior fix
set both `trailing_stop_positive` and `trailing_stop_positive_offset` to
0.03, which is exactly the invalid `tsl_offset <= tsl_positive` case.

Fixed: `trailing_stop_positive_offset` 0.03 -> 0.035 -- keeps
`trailing_stop_positive` at 0.03 unchanged, satisfies `0.035 > 0.03`,
and still cuts the dead zone from the original 0.06 (just not an exact
half). `stoploss` (-0.12) untouched.

**Verification**: checked the exact inequality from the real source
directly (`0 < 0.035 <= 0.03` is False -> no error). `py_compile` clean.
Full backtest suite re-run clean. `git diff` scoped to this one file,
one value.


## Persistent bot logs (--logfile on the db volume)

Problem: bot logs lived only in Docker's per-container stdout log, which
is deleted whenever a redeploy recreates the container -- only the
`*-db` volumes survived. Confirmed against real Freqtrade 2026.8
(`loggers/__init__.py`): `--logfile <path>` adds a
`RotatingFileHandler` (10 MB x 10 backups) alongside the existing
stdout handler, so Portainer's log view is unchanged and strategy
`log.info` lines (P13/P16/DeepSeek decisions) are captured too.

Change: all 8 bot `command:` blocks in `docker-compose.yml` gain
`--logfile /freqtrade/user_data/db/freqtrade.log`, i.e. the already-
persistent, already-writable `*-db` volume (no new volumes). Deliberately
NOT a host bind mount: the freqtrade image runs as a non-root user, and
Docker auto-creates missing bind-mount dirs as root, which would make the
log handler fail with PermissionError at startup. Named volumes inherit
the image directory's ownership (as the db volumes already do).

Reading logs: Portainer -> container -> Console -> `tail -n 500
/freqtrade/user_data/db/freqtrade.log` (rotated files: `.log.1` ...).
Survives redeploys; lost only if the volume itself is deleted (stack
removal with "remove volumes"). Capacity ~110 MB/bot at the default
rotation; a chatty bot like Dip may cover only a few days.

Verification: compose parsed with `yaml.safe_load`, all 8 services
contain the flag and mount the db volume; full backtest suite clean.
Not verifiable here: the actual file appearing after redeploy.


## Listing Fade max_open_trades: compose default was overriding config.json

Live log after the "4 -> 3" change still showed `Using max_open_trades:
4` for Listing Fade. Cause: P12-fix-2 added `FREQTRADE__MAX_OPEN_TRADES=
${FADE_MAX_OPEN_TRADES:-4}` to docker-compose.yml, and Freqtrade's env
override beats `config.json`, so the config edit was silently ignored.
Fixed: compose default `:-4` -> `:-3`, and `DEFAULT_MAX_OPEN_TRADES
["listing_fade"]` in backend/app.py 4 -> 3 so the UI's "repo default"
matches. Lesson: any future max_open_trades change must update
config.json, the compose default, and the app.py default together (or set
the Portainer env var). Verified: YAML parses, app.py compiles, suite
clean. Takes effect on redeploy.


## P16a-fix -- Dip stale-signal exit must not cut winners

Live logs (29/09) showed 8 Dip positions at `streak=1/2` with reasons like
"no dip here, RSI neutral, price up 1.76%". For a mean-reversion dip-buyer
that is the thesis WORKING: once the bounce happens the model stops saying
buy. P16a would have force-exited those after two candles at a tiny gain,
defeating the 10% -> 3% ROI ladder (P10).

Fix (`DeepSeekDipStrategy.py` only): new module constant
`STALE_SIGNAL_MAX_PROFIT_RATIO = 0.0`. In `custom_exit`, after the
min-hold check, a position with `current_profit > 0.0` returns `None` and
its `_stale_streak` entry is cleared -- profitable positions are left to
minimal_roi / stoploss / MAX_HOLD_DAYS, and must re-earn a full streak if
they later turn negative. Positions at/below breakeven with a dead thesis
(2 consecutive non-buy candles) still rotate out, freeing the slot.
The four P16b bots are unchanged: they are trend/momentum bots where
"signal gone" really does mean the thesis is over.

Tunable: raise the ratio (e.g. 0.01) to also rotate tiny winners; leave at
0.0 to protect every winner. Tests: +8 checks in
`backtest/test_p16_stale_signal_streak.py` (winner never rotated, no
streak accumulated, streak reset on moving into profit, loser still
rotates). Full suite clean. Takes effect on redeploy.


## P17-C -- control group (freqtrade-control-long / freqtrade-control-short)

Why: after ~a month of live dry-run there was no way to tell whether any of
the 8 bots beats a naive baseline. The control group is that baseline:
Freqtrade's own `sample_strategy.py` (RSI/BB/TEMA toy), on the same pairs,
$500 wallet and fees as Main / Shorts.

What was added (nothing existing was touched except compose/BOTS additions):
- `freqtrade-control-long/` (spot, long-only, port 8180) and
  `freqtrade-control-short/` (futures isolated 1x, short-only, port 8190):
  Dockerfile pinned to `freqtradeorg/freqtrade:2026.8` (NOT `:stable` -- the
  baseline must not drift on an upstream upgrade), config, strategy.
- Strategies are byte-for-byte the 2026.8 template
  (`backtest/fixtures/sample_strategy_2026.8.py`, sha256 18d48d64...) except
  the documented differences in each file's header: class rename; for the
  short bot additionally `can_short=True` and the template's `enter_long` /
  `exit_long` blocks commented out (short blocks untouched). No `leverage()`
  override, so Freqtrade's default 1.0 applies. The test reverts exactly
  those differences and requires equality with the pristine template, so a
  "small improvement" to a control fails CI.
- Config = Main's (control-long) / Shorts' (control-short) config: pairlist
  (top-15 volume + age/price/spread/volatility filters), `max_open_trades`
  10, `stake_amount` unlimited, wallet $500. Only `bot_name` and the CORS
  origin differ (asserted by test).
- Safety: `dry_run: true` hard in config.json, empty exchange key/secret, and
  the compose services set NO `FREQTRADE__DRY_RUN`, NO `FREQTRADE__EXCHANGE__*`
  and NO `FREQTRADE__MAX_OPEN_TRADES` env (an env var would silently override
  config.json -- exactly the Listing Fade bug). No LLM/second-opinion keys.
  There is no live mode.
- `freqtrade_client.BOTS` gains `control_long` / `control_short` (read-only
  client; still only one POST = the login call). NOT added to
  `payout_state.SPLIT_BOTS`, so they never enter the profit split.
- Deliberately NOT registered in frontend/index.html (BOT_PORTS etc.): the
  hero tiles and balance-history total sum over BOT_PORTS start wallets, and
  a measuring stick must not inflate the portfolio number. They still appear
  in `/api/freqtrade-summary/all` and in the daily balance-history snapshot
  (frontend ignores unknown keys), which is what the P17-A brain reads.
- API account: the single shared FREQTRADE_API_* login is used (freqtrade_client
  logs into every bot with one account); a separate account would need
  per-bot credentials in the client and was not done.

Verification: real Freqtrade 2026.8 (local venv) `list-strategies` loads both
control strategies (OK); `backtest/test_p17_control_group.py` (43 checks);
full suite clean. Not verifiable here: `docker compose build` of the pinned
image tag (needs the tag to exist on Docker Hub) and the running containers.


## P17-B -- Trend Ensemble v2 (verdict: NOT CONFIRMED, deployed dry-run only)

`freqtrade-trend-v2` (port 8200): rule-based (no LLM), daily, futures isolated 1x,
long AND short, $500 wallet, 10 slots, top-20 by volume, live gate like v1
(`FREQTRADE_TREND_V2__DRY_RUN` + `TREND_V2_LIVE_MODE`, both must allow). v1
(`freqtrade-trend-ensemble`) is untouched (a test pins its 3 files by sha256).
Registered everywhere in one pass: docker-compose (service, volume, dashboard env
mirror), ENV_VAR_DEFS, max-open-trades maps, `freqtrade_client.BOTS`, scorecard,
every frontend table + strategy card. NOT in `payout_state.SPLIT_BOTS`.

Design (all declared, none tuned): Donchian ensemble 5/10/20/40/60 voting both
ways; BTC regime filter (longs only when BTC's previous daily close > its 200d SMA,
shorts only below, via `merge_informative_pair`); exit = OPPOSITE Donchian channel
(`exit_channel`) + hard -15% stop, v1's native trailing (offset 0.10 dead zone)
removed; hysteresis = a channel exit is vetoed in `confirm_trade_exit` for the first
3 days (stops never vetoed); sizing = proposed_stake x clamp(4% target daily vol /
realized 20d vol, 0.25, 1.5) read from the analyzed dataframe (v1's per-pair cache
keeps the LAST value of the whole frame, which leaks end-of-data volatility into
every backtest trade's size); shorts skipped when the previous day's mean funding,
annualized, is below -10% (unknown funding fails open); CooldownPeriod(2) +
StoplossGuard(3 stops / 20 candles -> 5-candle lock). `trend_v2` config section holds
`min_votes`, `exit_channel`, `allow_long`, `allow_short`.

Funding availability (asked first): Bybit futures funding history IS available in
Freqtrade 2026.8 (`download-data --trading-mode futures` fetches `funding_rate`, 8h
spacing since 2020-03, column `funding_rate`, stored under the nominal `1h` name);
the filter is therefore enabled. Verified in a real local dry run too: the running
bot's analyzed frame carried `btc_bull`, `sma_1d` and `funding_ann`.

Backtest protocol (report: `backtest/p17_trend_v2_report.md`, raw numbers:
`backtest/p17_results/`, code: `backtest/p17_backtest_lib.py`, `p17_make_report.py`):
15 fixed long-lived perps (survivorship bias: coins that still exist today), train
2021-01-01..2023-12-31, test 2024-01-01..2026-09, `--fee 0.001` perps / 0.0015 spot,
`--timeframe-detail 1h`, `--enable-protections`, mark+funding data included. Six
variants declared in advance: exit channel {10,20} x min votes {2,3,4}; selection =
train Sharpe smoothed over min-votes neighbours (a plateau, not a peak) -> (10, 2).

**Attempts (honest count):** one smoke backtest of (20,3) on train BEFORE the grid
(same config as one grid cell); grid #1 (6 runs) and test run #1 used a funding
indicator that `freqtrade lookahead-analysis` then flagged as biased (`funding_ann`;
day D's mean used payments made after the truncation point). Fixed with a one-day lag
(regression test added), grid #2 (6 runs, one re-run after I accidentally killed it)
picked the SAME variant (10,2), then test run #2 (the reported one). Attempt-1 files
are kept outside the repo. No parameter was changed after seeing any test result.

Results (test window, fees on): chosen v2 long+short **-13.8%**, CAGR -5.3%, Sharpe
-0.19, max drawdown **49.0%**, 271 trades, long leg -$44.8, short leg **-$24.1**.
Gates: drawdown <= 30% FAIL, >= 100 trades PASS, short leg >= 0 FAIL -> **not
confirmed**. Context: v2 long-only +26.3% (DD 42.9%) but its profit is one outlier
(best trade XRP +$174.7; excluding it -$43.0); v1 -1.6%; control-short -13.3%;
control-long -92% (5141 trades: fee drag on the naive 5m baseline); BTC buy-and-hold
+88.5% (DD 53%); equal-weight hold +16.8% (DD 66%). On train every variant had a
negative short leg and 34-42% drawdown. Read: the long side is a fat-tail bet, the
short side loses, and nothing here beats simply holding BTC. `lookahead-analysis`
(no extra flags; config has price_side other because the tool forces market orders):
no bias after the fix. `recursive-analysis`: no instability.

Marked "NOT CONFIRMED BY BACKTEST" in the strategy docstring and on the dashboard card.
Not verifiable here: `docker compose build`/run of the new service, real dry-run fills.
Env note: the P11 bots' env vars (survival_shorts, listing_fade, trend_ensemble) are still
not mirrored into survival-agent's environment (pre-existing gap; v2's are).


## P17-A -- portfolio brain (SHADOW MODE ONLY)

Why: the bots know nothing about each other. A live check on 29/09 found SUI
long in Dip and Trend while Shorts and Survival Shorts were short SUI, PONS long
in Moonshot and short in Listing Fade, and LINK/HBAR long in 3-5 bots at once.
Nothing measured the SUM of those bets against a capital floor.

What: `backend/engine/portfolio_brain.py` (pure stdlib rules; `requests` only for
GET). Scheduled in `backend/app.py` (`_portfolio_brain_loop`) daily at **00:10 UTC**.
`PORTFOLIO_BRAIN_MODE=off|shadow` (default `shadow`). There is **no enforce mode**:
`PORTFOLIO_BRAIN_MODE=enforce` is refused and treated as `off`, and the module has
no code path that sends a non-GET request to a bot. It reads each bot's
`/api/v1/ping|balance|status|trades` with HTTP Basic auth (accepted by every
Freqtrade endpoint, so not even the login POST is used) and writes only under
`DATA_DIR`: `portfolio_brain_snapshots.jsonl` (one line/day), `portfolio_brain_state.json`
(HWM, control-start marker), `portfolio_brain_latest.json` (served by
`GET /api/portfolio/brain`), `portfolio_brain_report_<date>.md`.

Portfolio = 9 bots (main, shorts, moonshot, dip, survival, survival_shorts,
listing_fade, trend_ensemble, trend_v2); the two control bots and the BTC benchmark
are compared against, never summed in.

Computed every run:
1. Capital and floor: sum of equity, high-water mark (never decreases), floor =
   `PORTFOLIO_FLOOR_PCT` (0.90) x reference capital (capital ledger, else Freqtrade's
   starting capital, else the dry-run wallet), cushion = equity - floor.
2. Exposure: gross and net notional (stake x leverage; long +, short -) by coin and
   by bot, every non-BTC coin treated as beta `PORTFOLIO_ALT_BETA` (1.0) to BTC;
   compared with the CPPI budget = `CPPI_MULTIPLIER` (3) x cushion (also shown at 5x).
   Also lists opposite-side conflicts (same coin long in one bot, short in another)
   and crowded coins (3+ bots). The expectation that the current book exceeds the
   budget is the finding this module exists to quantify.
3. Drawdown ladder from HWM: -3% / -6% / -8%, plus weekly -2% and monthly -4% loss
   limits. Every rung carries a hypothetical "would ..." text; nothing is applied.
   Weekly/monthly figures stay `None` until a snapshot at least 7/30 days old exists
   (never a "7-day loss" measured over 2 days of history).
4. Targets: `TARGET_MONTHLY_PCT` (1.0) / `TARGET_WEEKLY_PCT` (0.25) are plan numbers.
   Reaching one logs "protect gains: risk x0.5, floor +50% of gains" with the
   proposed floor; missing one triggers nothing.
5. Bot decay: t-stat of per-trade expectancy over the last 30 closed trades; flagged
   only with a full window (n >= 30) and t < -2.
6. Cohorts (LLM: main/shorts/dip/moonshot/survival/survival_shorts; rules:
   listing_fade/trend_ensemble/trend_v2; control: control_long/control_short; BTC
   buy-and-hold from Bybit public klines) since the controls first traded: return %,
   trades, win rate, expectancy, max drawdown, fees. "No added value" flag when a bot
   loses to its control analogue (spot bots vs control_long, futures bots vs
   control_short) for >= 28 days with >= 30 trades.
7. LLM analyst: DeepSeek via `brain.ask`, input = the computed JSON only (no raw
   prices), output schema `{regime_note, flags[], proposals[]}`, strictly validated;
   proposals are only logged/stored; missing or invalid JSON -> skipped. Prompt
   version `p17a-v1` is written to the log and the report.
8. `GET /api/portfolio/brain` (GET-only route) and a read-only dashboard card.

Safety details worth knowing: if any of the 9 bots is unreachable the run is marked
`partial`, the drawdown rules are NOT evaluated (a missing bot would look like a
loss), the HWM is untouched and the snapshot is flagged so it is never used as a
weekly/monthly reference. `off` mode returns before fetching anything.

Config (all in ENV_VAR_DEFS + passed to survival-agent in docker-compose):
PORTFOLIO_BRAIN_MODE, PORTFOLIO_FLOOR_PCT, CPPI_MULTIPLIER, TARGET_MONTHLY_PCT,
TARGET_WEEKLY_PCT.

Verification: `backtest/test_p17_portfolio_brain.py` -- pure rules (floor/cushion,
net exposure, CPPI budget incl. zero budget, ladder, targets, t-stat/decay, cohort
metrics, no-added-value, analyst parsing, scheduler, settings), an end-to-end run
against fake bots (files written, HWM monotonic, same-day re-run replaces the
snapshot, partial data, mode off, analyst failure), and a proof that a full run
with the DEFAULT fetcher sends only GETs: `requests.post/put/delete/patch/request`
were booby-trapped and never called, every bot call carried Basic auth. The dashboard
card was checked in a real browser against a generated report (incl. the no-report
and incomplete-data states). Not verifiable here: the 00:10 UTC scheduling on the
live container and the real DeepSeek answer.

Deliberately not done: any enforcement (that needs its own decision after reading
a few weeks of these reports).


## P18 -- export centre on :3446

Why: "I thought it was 100k lines and 200 arrived." The catalog now shows the REAL
coverage of every source before anything is downloaded.

`backend/export.py` (FastAPI router, included from `backend/app.py`, GET-only):
- `GET /api/export/catalog` -- per bot (all 11): log files, lines, bytes, first/last
  timestamp (UTC), trade counts (total/open/closed via the bot's REST API), plus the
  P17-A brain files (`portfolio_brain_*`, jsonl line counts) and `decisions.log`. Line counts
  are cached per file by (path, mtime, size) -- rotated files never change, so only the live
  file is re-read. A bot with no logs / unreachable API reports "no data" with a reason.
- `GET /api/export/logs?bot=<id>&lines=N | hours=H | all=1` -- stitches `freqtrade.log` and its
  rotations oldest -> newest (`.10` ... `.1`, then the live file), returns the tail or the last H
  hours (timestamp filter, logs are UTC; wrapped continuation lines follow their entry), file
  name `bot_<id>_<from>_<to>.txt`. Streamed, secrets masked. Caps: lines <= 500k, hours <= 8760.
- `GET /api/export/trades?bot=<id>&status=closed|open|all&format=csv|json` -- every trade ever,
  from the bot's REST API (`/api/v1/trades`, pages of 500, HTTP Basic), never its SQLite file.
- `GET /api/export/bundle?bots=a,b|all&lines=|hours=|all=1&include=logs,trades,brain,decisions`
  -- ZIP built in a worker thread and streamed through a bounded queue: per bot `logs.txt` and
  `trades.csv`, `brain/*`, `decisions/decisions.log`, and `MANIFEST.json` (what was included, lines,
  period, rows, source files, generation time, "log timestamps are UTC", masking note; a bot with no
  data is listed as `included: false` with the reason instead of failing the bundle).

Secret masking (`mask_secrets`, unit-tested, idempotent): Bearer tokens, `Authorization:` values
(incl. Basic), `api_key/apikey/secret/token/password` values (`k=v`, `k: v`, JSON), JWTs (the FreqUI
websocket URL carries one), `sk-...`, `ptr_...`, and 32+ char hex strings. Ordinary log lines
(including the env-var NAMES Freqtrade logs at startup) pass through untouched.

Safety: `bot` must be in a fixed 11-id whitelist; no request parameter is ever used as a path (files
are found by listing `BOTS_DIR/<id>` and matching `^freqtrade\.log(\.\d+)?$`; symlinks, nested dirs
and lookalikes like `freqtrade.log.bak` are ignored); the OpenAPI parameter set is pinned by a test.
Bundle cap 300 MB (estimated up front from file sizes and the requested lines/hours -> 413 before any
work starts), max 2 concurrent exports (leases that expire after 30 min, so a client that vanishes
before its stream starts cannot leak a slot; a stream closed early frees its slot) -> 429; all heavy
work is off the event loop (sync routes / worker thread).

Infrastructure: `survival-agent` mounts all 11 bots' `*-db` volumes READ-ONLY at `/bots/<id>` and gets
`BOTS_DIR=/bots`. Only the log files are read; no SQLite file is opened. Nothing else changed for the
bots (their own volumes are still rw). Redeploy only `survival-agent`:
`docker compose up -d --build --no-deps survival-agent`. Logs exist only since commit 8153a59
(`--logfile` on the db volume); the catalog shows exactly what is there. The two newest bots
(trend_v2, control_*) will have logs only from their own first start.

Frontend: "Export" card in `frontend/index.html` (no new dependency): catalog table with per-bot
checkboxes, "lines"/"hours" fields (hours wins when filled), plain download links, and three presets:
"For analysis" (all bots, 50 000 log lines, all trades, brain files), "Last 24 hours", "Everything for all
time". Checked in a real browser against a fixture: real line counts and period, "no data" for bots
without logs or with an unreachable API, links update as the inputs change.

Verification: `backtest/test_p18_export.py` (132 checks): masking (12 secret shapes + untouched lines +
idempotence), rotation order, tail across files, UTC hours filter incl. wrapped lines and boundaries,
catalog values/cache/"no data", logs endpoints and filenames, path traversal (13 hostile bot ids on 3
endpoints, junk file names, nested dirs), trades CSV/JSON/status/paging, ZIP integrity + MANIFEST
correctness (counts and periods equal the archived content), 413 / 429 / lease expiry / early-close slot
release, missing log directory (catalog and bundle still valid), GET-only routes, compose mounts (11,
all `:ro`, each equal to the volume that bot writes), and the frontend card. The symlink refusal test
is skipped on Windows. NOT verifiable here: `docker inspect survival-agent` (no Docker in this sandbox --
run it after redeploy; the mounts should read `Mode: ro`, `RW: false`).

## P19 -- signal-edge audit, cost gate, evidence score (REPORT ONLY) + P18b aggregates in the export

Nothing trades differently: `dry_run` unchanged, live untouched, control bots untouched (not in
`SPLIT_BOTS`), brain still `off|shadow` with no enforce mode, no bot threshold/stop/ROI/prompt changed.

**P19-A ledger.** Checked first: `decisions.log` does NOT hold per-pair LLM decisions, so the ledger is parsed
from the bots' own `freqtrade.log` (the `DeepSeek said <action> for PAIR` lines, available since the
persistent-`--logfile` commit) plus REST `/pair_candles`; no strategy code was touched. `backend/engine/signal_audit.py`:
- `signals.jsonl` (decisions: ts, t0, bot, pair, action buy|sell_short|hold, confidence, price, source) and
  `signals_forward.jsonl` (sid, horizon, price0, price_h, ret), both APPEND-ONLY (tested byte-for-byte across runs).
  One record per (bot, pair, 1h candle); the first logged decision of a candle wins (restarts re-log).
- Nightly at 00:30 UTC (`_signal_audit_loop`, `SIGNAL_AUDIT_MODE=on|off`). Only the six LLM bots.
- No look-ahead: t0 = floor_hour(ts); decision price = close of the candle opened at t0-1h; forward price at
  horizon h = close of the candle opened at t0+h-1h, computed only once t0+h <= now. Tests mutate candles at/after the
  boundary and prove the numbers do not move.
- Benchmark = unconditional mean return of all pairs the bot scanned at the same candle (needs >= 3 pairs). Shorts are
  sign-flipped. Metrics per bot x direction x 1h/4h/24h: n, hit rate, mean excess over benchmark, percentile bootstrap
  95% CI (B=2000, fixed seed), Spearman rank correlation of confidence vs forward return.
- Below 200 decisions per bot the report says "not enough data" (counts only). `GET /api/signals/audit`, card "Signal audit"
  on :3446, and `signals_audit.json` + `signals/*.jsonl` in the P18 ZIP.

**P19-B cost gate** (in the brain report, per bot): fees $, fees % of turnover, fees / |gross P&L|, average move to exit
vs round-trip fee, funding fees separately; flag when fees > 50% of |gross| (gross = net + fees, since Freqtrade's
`close_profit_abs` is net).
**P19-C evidence status** (brain report, all 11 bots, labels only): `no_data` (n<30), `no_edge` (CI upper < 0),
`unproven` (CI spans 0, or comparison data missing), `edge` (CI lower > 0 AND beats its control AND BTC hold). A control bot
can never be `edge`. Nothing is blocked, paused or resized.
**P19-D live-money checklist** (report only): edge + >= 100 closed trades + >= 8 weeks + positive after fees + beats
control_long + beats BTC hold. The report names the eligible bots; today expect none.

**P18b.** Export now adds per-bot `aggregates/<bot>/{profit,daily,weekly,monthly,performance,stats,entries,exits,mix_tags,balance}.json`
via bot REST (GET only, fetched fresh on every export, masked with the P18 masker), MANIFEST gets per-file bytes and
`aggregates_total_bytes`; unavailable endpoints are listed, not faked. Port stays published as before (`3446:8088`);
LAN-only is an infrastructure fact (router/firewall), not something this code enforces.

Verification: `backtest/test_p19_signal_audit.py` (~190 checks), full suite green. Honest limits:
- No Docker/Portainer here: real statuses for the 11 bots cannot be produced now. They appear after deployment; the
  audit's first run is 00:30 UTC and needs >= 200 decisions per bot, evidence statuses need closed trades.
- Confidence exists only for Dip (P13); other bots get a null correlation and the audit says so.
- Logs exist only since the `--logfile` change, so history before that is unavailable. A pair that leaves the whitelist
  stops returning candles; its unresolved horizons are counted as expired after 72h.
- Not done here (user asked another session): check Dip's 54 trades on 28.09 for excess turnover from P16a.

## P20 -- market context collector (A) + LLM allocator (B)

Dry-run only. Control bots and trend_ensemble (trend v1) are never managed and receive no context. Entry/exit logic,
stops, ROI, thresholds and the bots' prompts stay as they were; P20 adds one context block to the six LLM prompts and a
stake multiplier (enforce mode only).

**P20-A `backend/engine/market_context.py`** (hourly at :05, `MARKET_CONTEXT_MODE=on|off`). One snapshot in
`context_snapshots.jsonl` (append-only), latest in `context_latest.json` (`GET /api/context/latest`) and a copy on the shared
volume. Every field has value / ts / source / status (ok | stale | unavailable); data older than 3h is stale, a stale value
older than 24h becomes unavailable; a failed refresh keeps the last good value (stale, original ts), never a guess.
Derived flags are computed in code: `funding_extreme` (current funding at >= 95th or <= 5th percentile of the coin's own 30-day
history, needs >= 30 samples), `oi_spike` (OI +10% in 4h), `macro_event_within_24h`, `liquidation_cascade`.
Source availability, tested from the dev sandbox (NOT from the server -- a Bybit geo-block or a Google throttle on the
server shows up in `sources` of the first snapshot):
- Bybit v5 public (tickers, funding/history, open-interest, account-ratio, kline): works, no keys.
- Liquidations: Bybit has only a WebSocket topic (allLiquidation), no REST -> `unavailable`, so `liquidation_cascade` is
  unavailable too (the allocator's liquidation trigger is wired but dormant). A WebSocket listener would be the next step.
- Macro calendar: forexfactory.com itself answers 403 and tradingeconomics needs a key; the ForexFactory weekly JSON on
  nfs.faireconomy.media (this + next week, USD, high impact, key events only) works and is used. No proxy/mirror tricks.
  If it is down, `macro_events.json` in the data dir is used (list of {name, ts, importance}) with a reminder in the snapshot.
- News: Google News RSS per coin, three states (no_news | feed_down | news_found) + headlines in the last 24h.
- Fear & Greed (alternative.me), BTC dominance + total cap change (CoinGecko, back-off on 429): in the snapshot, bots read them
  from it while fresh and fall back to their own fetch otherwise.
- ETF flows and on-chain: not connected (no reliable free source found).
Http layer: per-host pacing, 5-minute cache, exponential back-off on 429/403/5xx. Size: about 94 KB per snapshot at 60 coins,
roughly 2.3 MB/day on the survival-agent volume (no retention job yet).
Bots: `_allocator_ctx.py` (identical copy in the 8 managed bot dirs, reads /allocator read-only) appends "Derivatives & macro
context" with a status on every field to the six LLM prompts; control/rule bots get an empty block.
P19 link: `signals_audit.json` gets `context_slices` -- forward excess return of buy/sell_short signals with each flag true vs
false, flag taken from the last snapshot at or before the decision (<= 3h old, never a later one); a slice under 50 signals
says "not enough data".

**P20-B `backend/engine/allocator.py`** (daily 00:40 UTC after brain 00:10 / audit 00:30, plus out-of-schedule runs from the
hourly event check: ladder rung, macro event within 24h, liquidation cascade). `PORTFOLIO_BRAIN_MODE=off|shadow|enforce`
(default shadow; the brain itself stays read-only). The LLM only proposes `{bot: {weight 0..1, reason}}`; code validates
(bad JSON/schema = no change and a log line, out-of-range numbers cut to 0..1, control bots ignored) and then applies the hard
rules in order: +-25pp/day; ladder -3/-6/-8%; weekly -2% pause; monthly -4% cap 0.5; CPPI budget (gross >= budget: 0, >= 80%: cap 0.5);
macro window cap 0.5; opposite positions in one coin (smaller side gets 0); coin concentration >= 25% of equity (cap 0.5); < 0.05 = 0.
Safety caps may cut by more than 25pp. Every decision is one line in `allocations.jsonl` with the LLM reply, each rule step and a
context summary. Targets (0.25%/week, 1%/month) only protect gains.
Enforce: weight 0 = POST /stopentry (Freqtrade PAUSED: exits keep working), back above 0 = POST /start -- there is no /startentry
in Freqtrade 2026.8, verified in its source -- and the weight between 0 and 1 scales the stake through `custom_stake_amount` via
`/allocator/weights.json` (ignored unless mode=enforce and younger than 36h). Only `BotControl` sends non-GET requests, only pause/resume,
and raises `ControlBotError` before any network call for control bots / trend_ensemble. Enforce is refused when any managed bot reports
dry_run=false (the service refuses to start; an unreadable dry_run keeps that run in shadow). A bot restart resets Freqtrade's paused
state, so the hourly check re-pauses bots whose weight is 0. Kill switch: `POST /api/portfolio/allocator/kill` (button on :3446)
resumes bots the allocator paused, resets weights to 1 and drops to shadow; `.../resume` clears the flag.
Counterfactual (`allocator_counterfactual.json`): each trade re-weighted by the last decision at or before its open (no look-ahead),
net of fees, vs weight 1: P&L, max drawdown, share of positive days. After >= 28 days it says keep or revert_to_shadow
(net P&L and drawdown better and >= 3 of the last 4 weeks not worse); in enforce a revert verdict sets the kill switch by itself.
Surfaces: `GET /api/portfolio/allocator`, card "Allocator & market context" on :3446, `context/` and `allocator/` files in the P18 ZIP (masked).

Verification: `backtest/test_p20_context_allocator.py` (~290 checks, canned sources and fake bots), the whole suite green; the new card
was opened in a real browser against a fixture server (kill / clear-kill buttons exercised; the collector also ran once against the real
Bybit / calendar / Google / alternative.me / CoinGecko endpoints from the dev sandbox, all answered).
Not done / not verifiable here:
- `docker-compose.yml` was NOT changed for P20 (the edit was blocked in the session): the shared volume `allocator-shared`
  (rw in survival-agent at /allocator + env `ALLOCATOR_DIR=/allocator`, `:ro` at /allocator in the 8 managed bots) still has to be added.
  Until then the bots see no snapshot (their prompt block says "unavailable (no snapshot)" and they fetch F&G / market themselves as
  before) and ignore weights (multiplier 1.0), and survival-agent only writes its copy into its own data dir.
- No deploy, so no one full shadow day with a decision log yet; the enforce path is tested only against fake bots.

### P20-B resume fix (review finding: "/stopentry zeroes max_open_trades, /start does nothing")
True for old Freqtrade builds, NOT for 2026.8: its source (`rpc/api_server/api_trading.py`, `rpc/rpc.py` `_rpc_pause`/`_rpc_start`) maps
/stopentry to State.PAUSED and /start back to RUNNING. But the bots run `freqtradeorg/freqtrade:stable`, whose version was not
verified, so the allocator now handles both: a bot counts as paused if `state == paused` OR `max_open_trades == 0`; resume = POST /start,
and if /show_config still shows it paused, POST /reload_config and re-check (`verified_running`). A resume that does not take effect
is reported and the bot stays in `paused_by_allocator`. The kill switch uses the same path. Tested against fakes of both behaviours.
STILL REQUIRED before enforce (cannot be done here, no live bot): on ONE live dry-run bot run /stopentry, confirm no new entries,
resume, confirm entries return, and confirm that after /reload_config the bot's `max_open_trades` equals its configured value
(config.json + compose env such as FADE_MAX_OPEN_TRADES).

## P20.1 -- CPPI scaling, per-direction audit gate, probe weight, image pin, source breakdown, ops tooling

1. **Proportional CPPI** (`allocator.apply_hard_rules`): gross > budget scales every weight by budget/gross (1.11x budget ~ -10%);
   all 0 only when gross >= 1.5x budget or the floor is breached (budget 0). The old "gross >= budget: 0" and "80% cap" are gone.
2. **Audit gate by actions** (`signal_audit`): metrics per direction need >= 100 `buy` (resp. `sell_short`) actions; hold never counts.
   Bot status is ok when at least one direction passes; a direction below it shows "not enough data" with its own count. Card and context
   slices follow the same per-direction gate. `min_decisions_per_bot` became `min_actions_per_direction`.
3. **Probe weight** (`allocator.apply_probe`): weight 0 for > 7 days (`ALLOCATOR_PROBE_DAYS`) -> weight 0.1 for 23h (ends before the next
   daily run, no chained probes), then a new 7-day clock. `ALLOCATOR_PROBE=off` disables. Never applied while a safety rule (ladder,
   weekly/monthly limit, CPPI stop, opposite positions, concentration) holds the bot at 0. The P19 ledger is independent of weights.
4. **Image pin**: all 11 bot Dockerfiles now `FROM freqtradeorg/freqtrade:2026.8` (controls already were). WARNING found while doing it:
   Docker Hub `stable` was re-pointed to 2026.9 on 2026-09-29 (same digest as the `2026.9` tag), so any bot rebuilt since then may run 2026.9.
   Freqtrade's SQLite migrations only go forward: before redeploying, check each running bot's version (`docker exec <bot> freqtrade --version`);
   if any is on 2026.9, pin it to 2026.9 instead of downgrading its DB. I read the 2026.8 AND 2026.9 sources: both map /stopentry to PAUSED and
   /start back to RUNNING. trend_ensemble (v1) is touched only in its base image tag; its frozen-hash test was updated for that.
5. **Unavailable-source breakdown**: `context_latest.json` has `unavailable_breakdown` (Bybit fields and news requests by reason class, by field,
   with coins and one example message) and `http_stats` (calls and errors per host). From the dev sandbox the Bybit gap is explained by coins that
   have no linear USDT perpetual (spot-only coins such as PUMP: 8 fields each, so 5 such coins = 40). The collector also had a real flaw: one 5xx made the
   host unusable for all remaining coins (this alone can explain many "news unavailable" fields); now only 403/429 back off at once, 5xx and
   timeouts back off after 3 in a row. The Dip bot's 229 Bybit timeouts per day were NOT analysed: the logs live on the server (use the P18 export,
   then `grep -c "Timeout" freqtrade.log` per bot and compare with `http_stats`); I make no claim about the cause.
6. **Enforce**: NOT switched on. The mandatory live test needs a running dry-run bot, which this environment does not have.
   Tools: `python -m backend.ops.p20_live_test --bot moonshot` (dry_run check, /stopentry, no entries while paused, resume incl. /reload_config
   fallback, max_open_trades must equal the pre-test value, entries must return -- or the report says INCONCLUSIVE; always resumes the bot at the end)
   and `python -m backend.ops.p20_enforce_check` every 6h for 48h (allocation actions, verified_running, stuck/untracked pauses, missing daily run,
   dry_run, control bots) plus `--kill-drill` for the day-one kill-switch check. Both run inside survival-agent. Tests: `backtest/test_p201_ops.py`.
   Still open: the compose edit (shared volume `allocator-shared`, see the P20 entry); without it enforce cannot reach the bots.

## P20.2 -- dashboard switches (runtime settings)

`backend/engine/runtime_settings.py`: switches changeable from :3446 without a redeploy. Precedence: UI value (`runtime_settings.json`) >
environment variable > default; a UI value can be reset to the environment. Whitelist only: `PORTFOLIO_BRAIN_MODE` (off|shadow|enforce),
`MARKET_CONTEXT_MODE`, `SIGNAL_AUDIT_MODE`, `ALLOCATOR_PROBE`, `ALLOCATOR_PROBE_DAYS`, `ALLOCATOR_EXCLUDED_BOTS` (managed bots only: full size,
never paused, no probe). No credentials, no dry_run, nothing about thresholds/stops/ROI/prompts. Every change is appended to
`runtime_settings_log.jsonl` (also in the ZIP). Endpoints: `GET/POST /api/settings/p20`, `POST /api/portfolio/allocator/run` (manual run).
Enforce from the UI needs `confirm=true` and is refused with 409 unless every managed bot verifiably reports dry_run=true; the response lists warnings
(live pause/resume test not run, shared /allocator volume not mounted). Card "Allocator & market context": mode selector (confirm dialog for
enforce), collector / audit / probe switches, per-bot exclusion, run now, kill switch, clear kill. Tests: `backtest/test_p202_settings.py`.
Enforce was requested "right away" WITHOUT the live pause/resume test the P20.1 spec called mandatory. It is a one-click switch now; the card keeps
showing the missing-test warning until a passed `p20_live_test_*.json` report exists. The deploy default stays shadow because compose passes
`PORTFOLIO_BRAIN_MODE=${...:-shadow}` and the compose edit was blocked; enforce is set in the UI (or Portainer) after deploy.

## P20.3 -- allocator reply format + enforce default

Cause of the 02.10 00:40 UTC miss: `brain.ask` capped the reply at 1500 tokens and the model spent them on reasoning ("We are given a portfolio...")
before any JSON. Code behaved correctly (weights unchanged) but the allocator did nothing that day.
Changes (allocator `p20b-v2`):
- Prompt: the whole reply must be one JSON object `{bot: {"weight": number, "reason": "<= 140 chars"}}`, no reasoning or text around it, ends with
  "Reply with the JSON object only."
- `brain.ask(max_tokens, json_mode)`: the allocator asks for 4096 tokens and DeepSeek's `response_format: json_object`; the OpenRouter fallback gets the
  token limit but no response_format (free models vary; the instruction carries it). Other callers keep 1500 / no JSON mode.
- Parser: whole reply (optionally fenced) first; otherwise the first balanced `{...}` object that parses (brace matching aware of JSON strings), noted as
  "JSON object extracted from surrounding text". Reasons are cut to 140 characters.
- One retry: an unusable reply is asked again once with "Your previous reply was not valid JSON. Reply with the JSON object only." Still unusable ->
  weights unchanged, logged.
- `allocations.jsonl`: `llm.attempts` (1|2), `llm.raw_reply` (last reply) and `llm.invalid_replies` (every unusable reply, up to 4000 chars each).
- Unverified resume (`verified_running: false`): the bot stays in `paused_by_allocator`, is listed in `stuck_bots` (state, record, latest) and the card shows a
  red "Resume NOT verified" line; nothing is rolled back automatically (only the kill switch rolls back).
- Enforce default: compose default `PORTFOLIO_BRAIN_MODE=${PORTFOLIO_BRAIN_MODE:-enforce}` for survival-agent (a UI or Portainer value still wins).
  Control bots and trend_ensemble stay unmanaged in code. Kill switch unchanged.
Open: the shared `/allocator` volume lines in compose were NOT written (the session's compose access was blocked again after the one-line default change).
Without them bots ignore fractional weights and read no context, but the allocator counterfactual treats enforced trades as already scaled -- so until the
volume is mounted, read the counterfactual with that in mind (pausing at weight 0 works via REST either way).
No new tests that need a live container or network were added; the new checks are offline.

### P20 compose wiring (written)
`docker-compose.yml`: named volume `allocator-shared`; survival-agent mounts it rw at `/allocator`; the 8 managed bots mount it `:ro` at `/allocator`
(main, shorts, moonshot, dip, survival, survival_shorts, listing_fade, trend_v2). trend_ensemble, control_long, control_short have no allocator mount.
No `ALLOCATOR_DIR` env var is needed any more: the code defaults to `/allocator` when that directory exists. Tests that pinned the old volume lists
(P17-B v2 service, P18 mounts) were updated; `test_p201_ops.py` asserts the mounts. The "contrafactual reads enforced trades as scaled" caveat above
is lifted once the stack is redeployed with this file.

## P20.4 -- retired bots / stale bots / partial data (A) and Kronos as a ledger signal (B)

### Part A -- what the 02-04.10 incident was and what changed
Dip was stopped on 02.10 evening. The brain then saw it as "missing", equity dropped (the Dip balance fell out of the sum), `budget` became 0, the false `cppi_floor_breached` zeroed every weight, and the
allocator also wrote `dry_run not verifiable on ['dip']` and downgraded itself to shadow, so enforce did nothing from 02.10 21:08. On 04.10 listing_fade was silent for one run too. The
counterfactual for those days (-$0.36 vs -$9.34) measured zero weights, not decisions.
Fixes (`backend/engine/bot_state.py` + brain + allocator):
1. **Retired bots.** `ALLOCATOR_RETIRED_BOTS` (compose default `dip`, also a dashboard switch). A retired bot gets no REST call at all (no reachable / balance / status / trades / show_config), no weight, no dry_run check,
   is never `missing`, and is not polled by the signal audit for candles either. Its history stays: the evidence entry is copied from the last report that had it and marked `retired`, ledger signals stay.
   **How the contribution is frozen (chosen method):** equity = sum of the active bots + the retired bot's frozen balance (a constant); the reference capital keeps its start capital, so the floor stays comparable with the days before the stop and the HWM is not touched.
   The balance comes from the last brain snapshot that still contained the bot, or from the operator override `ALLOCATOR_RETIRED_FROZEN="dip:<final balance>"` (read it from Dip's `/balance` aggregate in the export). If no value exists to freeze, the fallback is
   used and recorded (`method: dropped_from_reference`): the bot adds nothing to equity and its start capital is subtracted from the reference, so the floor is computed from the active bots only. Records live in `retired_bots.json`.
2. **One silent bot does not zero the budget.** Every successful poll caches equity and open trades (`bot_last_known.json`). A bot that does not answer keeps counting with that state: up to 24h `stale_bots`; older `unreachable_bots`
   (equity kept so it is not read as a drawdown, exposure dropped because it is unknown). Only a bot never seen makes the run `partial`.
3. **`cppi_floor_breached` and the other equity rules do not fire on `partial`.** With incomplete data the ladder, the weekly/monthly limits and the CPPI floor/budget only add a warning to the record; weights are not cut by them. With a complete
   set of active bots they fire as before.
4. **dry_run scope.** Retired bots are not checked and cannot block enforce or the startup guard. An unverifiable ACTIVE bot still keeps the run in shadow, and the reason now names exactly that bot ("dry_run not verifiable on ['x'] (active, not retired)").
5. **Stuck pauses.** A resume now always records `verified_running` (also on the plain `/start` path). trend_v2 (paused_by_allocator since 02.10 21:05) is resumed by the next enforce run once its weight is above 0.
6. **Counterfactual cut-off.** The first run that knows about a retirement sets `cf_since` in `allocator_state.json`; the counterfactual counts only decisions from then on and lists the earlier ones under
   `excluded: {reason: "retired_bot_incident", ...}` (kept in `allocations.jsonl`, not deleted).
Not done here (needs the server): the out-of-schedule run after deploy. Run "Run allocator now" on :3446 and check in `allocations.jsonl`: `effective_mode=enforce`, `portfolio.partial=false`, `portfolio.missing_bots=[]`,
`retired=["dip"]`, `budget` roughly $0.6k or more, `actions` with `verified_running` for trend_v2. Expected from the incident numbers: equity about $2.5k, not $2.0k.

### Part B -- Kronos
Checked on Hugging Face and GitHub on 04.10.2026 (details and pins in `kronos-signal/README.md` and `NOTICE.md`): `NeoQuasar/Kronos-small` (24.7M parameters, 98.98 MB, context 512, MIT) + `Kronos-Tokenizer-base` (15.8 MB, MIT),
revisions `901c26c1...` / `0e011738...`, code `shiyu-coder/Kronos` MIT commit `67b630e6...`; no GPU needed. Variants not used: mini (4.1M, context 2048) and base (102M, 409 MB).
**Run locally outside Docker (dev machine, Python 3.13, 2 CPU threads):** about 10 s per pair with 3 samples (17.4 s with 5; 18.4 s on 1 thread), about 420 MB resident, deterministic with a fixed seed. 25 pairs is therefore about 4-5 minutes
on that machine; the server was NOT measured. A first attempt crashed with pandas 2.2.2 + numpy 2.5.3 (access violation in `to_datetime`): the image pins numpy 2.1.3 / pandas 2.2.3.
Design: separate container `kronos-signal` (compose: 2 CPUs, 1 GB, low cpu_shares, pids limit, own volume `kronos-data`); nothing heavy in the bot images. Hourly at minute 2, 25 most liquid pairs from the active bots' whitelists (spot preferred, retired bots skipped),
480 closed candles in, 24 out, mean of 3 samples, seed 12345, expected returns at 1/4/24h, action from the 4h value after a 0.2% fee and a 0.5% threshold (`KRONOS_THRESHOLD`), confidence = |ret 4h|.
It writes append-only ledger-format records to its own volume; survival-agent mounts it `:ro` and the nightly audit ingests it (`ingest_kronos`); candles for the forward returns come from the SOURCE bot's REST, not from a "kronos bot".
The audit treats `kronos` as one more source (excess over the same-t0 cross-sectional mean, bootstrap CI, rank correlation of |forecast| with the outcome, per direction) and attaches a report label `verdict` (>= 200 actions per direction and 14 days;
edge only if the CI lower bound is above zero at 4h or 24h and in both halves; otherwise unproven / no_edge). `kronos` is in `NEVER_MANAGED`.
Tests (`backtest/test_p204_kronos.py`, offline, fake forecaster): look-ahead (the forming candle never reaches the model, stale candles skipped), the decision thresholds, universe/ranking, a full cycle, dedupe, time budget, ingest, audit incl. the verdict,
compose limits and image pins. **Not verified:** the image build, the container run, the real cycle time on the server (check `kronos_health.json`), and that the Linux CPU torch 2.14.1 wheel for Python 3.13 exists.
Refused by the spec: AI-Hedge-Fund and TradingAgents (same LLM trade decisions, more agents mean more cost, no independent check).

### Documentation
README rewritten for the current system (it still described four bots), ARCHITECTURE.md gained a status note and section 12 (measurement and control plane), HOW_IT_WORKS.md gained a status note and sections 13-15,
ENVIRONMENT_VARIABLES.md the new variables, `kronos-signal/README.md` new. The GitHub "About" description cannot be set from the repository (needs the web UI or a token): suggested text is in the P20.4 report.

### P20.4 follow-up: the Kronos cycle time is recorded by the system
The container writes `kronos_health.json` (also right after the model loads); survival-agent exposes it live at `GET /api/kronos/health`, shows it on the audit card, embeds it in `signals_audit.json` and in the ZIP, so no manual copying of the first cycle time is needed. The container also runs at `nice 10`. Model size is already the small variant (24.7M parameters, 99 MB) with CPU/memory/process caps; `Kronos-mini` (4.1M, context 2048, tokenizer `Kronos-Tokenizer-2k`) is the next step down if the real server cycle truncates -- it was NOT measured.

## P21 -- Replay: a fast historical run of the LLM bots (phases 1, 2, 3 and 5; phase 4 NOT started, as specified)

**What it is.** A page `/replay` on :3446 and a background job inside survival-agent (a thread; there is no Docker socket) that runs the six LLM bots' REAL strategy code over cached Bybit
candles, with the bots' own exit constants, and reports decisions, simulated trades, drawdown, baselines and a P19-style audit. It never trades and never touches live state.
Replay NEVER assigns `edge` (best label: `unproven (replay)`); the banner "Historical replay. A negative result is a strong argument against a bot; a positive one is weak. The status edge is only ever given from live data." is permanent on the page.

**Isolation (hard, tested).** Everything is written under `REPLAY_DIR` (default `/data/replay`) through `paths.replay_path()` / `cache_path()` / `root_file()` (a path outside the sandbox raises; the live file names -- signals*, allocations,
weights, context_*, ... -- are refused by a second guard). The replay package imports neither the allocator, the brain, `freqtrade_client`, `bot_state` nor `BotControl` and contains no live bot REST path (AST test). The test hashes AND mtimes the live files
(/allocator weights + context, the ledger, audit, allocations, brain report, a bot log) before and after a full job and diffs the DATA_DIR tree: unchanged. One job at a time; LLM parallelism bounded by `REPLAY_WORKERS` through one shared client.

**How the real strategy code is reused without editing it** (`backend/replay/sandbox.py`): the strategy file is exec'd as a module with `freqtrade` stubbed (as in `backtest/llm_replay.py`), then its module globals are swapped:
`requests` -> a shim that serves Bybit klines (60 / 240 / D) AS OF the replay clock from the local store and refuses (and records) every other URL, `datetime` / `time` -> the replay clock, `Trade` -> a per-bot registry of simulated trades behind
`Trade.get_trades_proxy()`, `get_market_context` / `get_news_headlines` -> fixed "unavailable for a historical replay" texts (neither NO_NEWS nor FEED_DOWN), `get_fear_greed_index` -> the alternative.me value as of the clock,
`_second_opinion` / `_allocator_ctx` -> inert stubs (no block, stake multiplier 1.0); on the instance `_call_deepseek` -> the replay client, `_call_openrouter_fallback` -> None, `dp` -> an as-of data provider, `wallets` -> simulated cash.
Hook list per bot (all six load with every hook replaceable; NO live file needed an edit, so no bot was stopped): main, shorts, moonshot, survival, survival_shorts, dip (default off). Differences found: `_ask_deepseek` of Moonshot takes
(row, pair, approx_days_old, candles_available) and Dip returns a 3-tuple (decision, reason, confidence) -- both are handled because the engine calls the real `populate_entry_trend` and reads `_latest_decision` instead of calling `_ask_deepseek` itself;
Survival Shorts has no Fear & Greed hook and no mtf/concentration methods; Shorts / Survival Shorts read a Bybit funding line that replay does not serve (the line is left out and the report says so). The loader raises `HookError` rather than guess if a hook is missing.
The engine drives the real `populate_indicators`, `populate_entry_trend`, `confirm_trade_entry`, `custom_stake_amount`, `custom_exit`, `confirm_trade_exit`. `minimal_roi` and `stoploss` are read from the strategy class.

**Look-ahead.** One function decides what is visible: `SeriesStore.asof()` (candle visible only if open + 1h <= t). A pandas-unit bug was caught while writing it (pandas 3 stores microseconds, pandas 2 nanoseconds: every as-of query returned the END of the data); it now compares in
explicit nanoseconds and the whole suite runs green on both pandas 3.0.6 and the production pins 2.2.3 / numpy 2.1.3. Tests: replacing every candle from t on with garbage changes no character of any prompt (named, spot and futures), the ledger forward returns
ignore candles after the horizon, the 4h/D klines served to the strategies contain only complete bars.
**Determinism.** Fixed seed for the pair order; the core/satellite tier of every pair is primed before the parallel LLM phase (otherwise one pair's "recent performance" depended on a race with another thread's classification); same parameters give identical decisions, trades and equity with 1 or 6 workers.

**Simulator** (`engine.py`): decision at a candle close, fill at the next open + slippage, fee per side, ROI and stoploss checked on every hourly candle (stoploss wins if both are inside one candle), `custom_exit` every hourly close, stake = Freqtrade's "unlimited" rule then the strategy's own stake logic, max_open_trades and the wallet from each bot's baked config.json,
1x leverage, no min-notional / partial fills / funding. With a step above 1 candle the stale-signal streak counts decision steps. Pair list = a fixed snapshot of today's most liquid coins (not a day-by-day volume list); for Moonshot (new listings) this is NOT its live universe.
**Baselines in the app:** BTC buy-and-hold and 200 random-entry repeats (same trade count and exit constants, no rotation exits). **Controls are NOT computed**: the control strategies need talib / qtpylib, which the survival-agent image does not have; compare with the live control bots or a Freqtrade backtest.
**Divergence from live:** from `REPLAY_DIR/live_reference.json`, built OFFLINE from a P18 export with `python -m backend.replay.live_reference <zip> --from 2026-09-21` (so the replay itself never reads a live bot). Without that file the report says "no live reference".
**Cost control:** the estimate builds real prompts on 3 pairs per bot (candles from the free Bybit API, no LLM call) and multiplies the EXACT call count; prices come only from `REPLAY_PRICE_IN_PER_M` / `REPLAY_PRICE_OUT_PER_M` (no prices = no dollar figure = a job cannot start); the real spend comes from the API `usage`; `REPLAY_MAX_USD` / `max_calls` stop the job (`stopped_budget`);
the disk cache keyed by hash(system, user, model, temperature) makes a repeat free (tested: second identical job = 0 paid calls, identical result). The form's limit can only lower `REPLAY_MAX_USD`.
**Windows:** 7-60 days, end at least 48h before now, start not before `REPLAY_MIN_START` (2026-08-01); `anonymized` mode replaces pairs by ASSET_NN and absolute dates by T-<n>h in everything sent to the model; the report flags `leak_suspect` when a positive CI exists only in the named run.

**Packaging:** `backend/Dockerfile` copies the six strategy files (+ helpers, config.json, auto_tune.json) read-only to `/app/strategies_src/<bot>/`; `backend/requirements.txt` gained `pandas==2.2.3` and `numpy==2.1.3` (the survival-agent image had neither; roughly +70-90 MB, NOT measured -- the image was not built here). No compose change, no new service.

**NOT done / NOT verified (read this):**
- The PILOT (7 days, 5 bots, both modes, $1) was NOT run: this environment has no DeepSeek key and no real Bybit/LLM access, so the real cost per call, the throughput, the cache hit rate on real traffic and the divergence from the live bots are all UNMEASURED. All numbers in the tests come from a fake LLM and synthetic candles.
- The container build and the page in a live container were not tested; the page was checked in the browser pane against a fixture server only (see the report).
- Phase 4 (allocator over simulated days) is deliberately not started (the form shows it disabled; the API refuses it). `allocator.py` already takes `now` and `data_dir`, so the refactor the spec anticipated may turn out unnecessary -- to be checked when phase 4 starts.
- Phase 5's "equity chart: bot vs BTC vs median of random entries": the chart shows bot equity (named and anonymized) and BTC hold; the random baseline is a distribution of the mean return per trade (table), not an equity curve.
- Repo hygiene: `_to_delete/` and `write_test.txt` are untracked leftovers (not deleted, not committed -- waiting for the owner's word). `auto_tune.json` is a tracked, real file (written by `backtest/auto_tune.py`) and is baked into the bot images and copied for replay; `_second_opinion.py` / `_allocator_ctx.py` are tracked source files, not runtime copies.

### P21 follow-up: compose pass-through, live-reference button, .gitattributes
- `docker-compose.yml` passes the seven `REPLAY_*` variables to survival-agent as `${VAR:-}`. An empty value (an unset Portainer variable) is treated as "default" (`REPLAY_MIN_START` had to be fixed for that: an empty string used to be taken literally); empty prices stay "not set".
- Button "Refresh live reference" on `/replay` (`POST /api/replay/live-reference/refresh`, `GET /api/replay/live-reference`): the trades come from the SAME code the P18 export uses (`export.iter_trades` + `export._trade_row`, i.e. the bots' REST), are summarised
  over [from, to] (default from 2026-09-21) and written to `REPLAY_DIR/live_reference.json`; an unreachable bot is listed with the reason, if no bot answers the previous file is kept. This is an explicit operator action in `app.py`, outside the replay package
  (the isolation test still proves the package imports nothing live); a replay run itself never reads a live bot. The offline route from an export ZIP remains (`python -m backend.replay.live_reference`).
- `.gitattributes` (`* text=auto eol=lf`, scripts / Dockerfiles / YAML pinned to LF, binaries untouched). The index was already all LF; 11 files had CRLF working copies (e.g. `_second_opinion.py`, `auto_tune.json`, the p17 result files) and were reset to the committed LF content, so the "LF will be replaced by CRLF" noise stops.


## P21.1 (A + B) -- live reference with logs, simulator calibration on the live bots' real decisions

Scope of this stage (per the spec's order): A + B only. C (exit variants), D (no-LLM rule), E (provider choice), F, G are NOT started.

### What was built (in code, not deployed)
- `backend/replay/live_data.py`: parses the bots' `DeepSeek said ...` lines (action, pair, reason, confidence), restarts (`Changing state to: RUNNING`), `max_open_trades`, outage hours (hours where >=90% of decisions are `no LLM response`), live trades (P18 rows or `trades.csv`) into `REPLAY_DIR/live_snapshot.json`; `coverage()` per bot.
- Button "Refresh live reference" now also reads the bots' logs (`export.log_files` / `iter_all`) and ALL trades, writes `live_reference.json` (closed count, mean, win %, exit-reason mix, mean hold, open at window end, fees) and `live_snapshot.json`. Divergence flags in reports: trade count > +-20%, mean > 0.5 pp, exit mix (TV distance > 0.2).
- `backend/replay/calibration.py` + engine changes: the simulator takes the live decisions instead of an LLM (`BotRun.live`), carries over positions open at the window start, enters in log order, matches trades 1:1 (pair, side, open hour +-1 candle), reports count ratio, matched share, entry/exit time and price differences, exit-reason agreement, return-difference distribution, only-live / only-replay causes, acceptance flags, a second set of numbers without outage hours. New run type `calibration` (`POST /api/replay/calibration/start`, `GET /api/replay/calibration/coverage`), UI block on `/replay`.
- Mechanics switches in `engine.py` (`MECH_LEGACY` = what P21 shipped = still the default of normal replay; `MECH_LIVE_LIKE`): decisions-before-exits (Freqtrade order), entry delay, Freqtrade's unlimited-stake formula, restart empties in-memory state, one-minute fill prices. The delay is MEASURED (median seconds after the full hour at which the live bot opened its trades), nothing is tuned.
- Tests: `backtest/test_p211_calibration.py` (66 checks, offline), `test_p21_replay.py` still 201/201.

### What the real data showed (export 2026-10-04 14:55, real Bybit candles, run on the operator's machine; report: `backtest/results/p21_1_calibration_report.md`)
- Coverage: logs start 2026-09-29 10:08 (rotation / redeploy), so decisions exist for 2026-09-29 11:00 .. 2026-10-04 14:00 (124 h, every hour), NOT for 25.09-02.10. Each bot restarted 12 times. Dip: decisions to 2026-10-02 (retired), not calibrated.
- **DeepSeek returned 402 "Insufficient Balance" from 2026-10-01 20:00 to about 2026-10-02 18:00**: every decision in that period is a fake `hold` (`no LLM response`), rotation closed positions. This contaminates live numbers (and the P19 ledger's holds) and part of the earlier pilot's "live vs replay" gap (the pilot's reference window 25.09-02.10 contains it).
- Acceptance (count within +-15%, median |return diff| <= 0.3 pp, exit mix TV <= 0.2), live-like mechanics: main PASS (24 vs 23 entries, 0.28 pp), survival PASS (31 vs 32, 0.20 pp); shorts FAIL narrowly (73 vs 76, 0.327 pp), survival_shorts FAIL narrowly (46 vs 49, 0.366 pp); moonshot FAIL on count (9 vs 15, 0.355 pp).
- Before -> after: legacy mechanics passed only main. The decisions-before-exits order is the one fix that matters everywhere (rotation saw the previous candle's decision, one hour late); the others are small.
- Named causes: (1) price noise between the hourly figure and the fill moment (entry price differs 0.06-0.18%, exit 0.11-0.42%) -- sets a floor of ~0.2-0.35 pp on per-trade return difference, so the 0.3 pp criterion is borderline by nature; (2) moonshot's peak-decline exit is a knife edge on tiny profit differences (e.g. CAP: replay exits 8 h earlier than live from a 0.05 pp difference); (3) shorts: funding fees and spreads are not modelled; (4) stake sizes: ratio ~1.0 (allocator weight not modelled, no effect seen); (5) a few trades differ because the replay's twin of a position closed a candle earlier/later, so the pair was open on one side only.
- Not verified: the Docker image with the new code, the buttons against the real stack (snapshot built from the real logs mounted at /bots), calibration runtime inside the container, how the app copes with 7 MB logs.

### P21.1 follow-up: shorts calibrated, C / D / E / F / G implemented, new mechanics are the default (in code, not deployed)

Order of work as asked: shorts first, new mechanics as default, all remaining steps, then Moonshot.

**Shorts (and everything else) -- what was measured and fixed (mechanics only, nothing tuned to results):**
- The funding hypothesis was wrong: `funding_fees` is 0 in the dry-run trades. The real causes were timing and execution:
  (1) the loop latency is read from the live log per hour (the end of the hour's analysis batch + 2 s = when the live bot entered; measured median difference 2 s), split into batches at restarts, exits ~10 s after the analysis;
  (2) ROI / stoploss are ordered on ONE-MINUTE candles inside the hour (the hourly candle cannot tell which came first: 2 shorts trades differed by 16 pp because of it); a stop / ROI that the minute closed beyond fills at that minute's close (a polled market exit gaps through);
  (3) calibration slippage is 0 (dry-run fills at book prices; the measured entry-price bias of the replay was exactly the 0.05% it was given);
  (4) entries are PASSIVE LIMIT orders at the book top, cancelled after 10 min and re-placed (`unfilledtimeout.entry = 10`, price_side `same`): modelled with minute candles and today's bid-ask spread per symbol (history of the book is not public -- stated limit);
  (5) `custom_exit` is called every few seconds live, not once per candle: time-based exits (Moonshot's 3-day maximum hold) fire at once -- polled every 5 min in the simulator.
- Result on the real export (2026-09-29 11:00 .. 2026-10-04 14:00): main, shorts, survival, survival_shorts PASS (count within 15%, median |return diff| <= 0.3 pp, exit mix TV <= 0.2); shorts 75 vs 73 entries, 0.27 pp; survival_shorts 48 vs 46, 0.28 pp.
- **Moonshot** (flagged `universe_mismatch`, mechanics check only): median return diff 0.15 pp, exit reasons agree 100%, but 15 replay vs 9 live entries: it FAILS the count criterion and this is measured, not guessed -- the live log shows 7 of its 22 entry orders and 19 exit orders cancelled by the 10-minute timeout (thin books of new listings: the passive limit is never hit); candles cannot show that. The calibration report names these trades `live_entry_order_timed_out`. No simulator knob could fix it without fitting to the outcome, so none was added.

**Defaults:** a normal replay now uses `MECH_REPLAY_DEFAULT` (decisions before exits, Freqtrade stake formula, 50 s entry latency, 8 s exit latency; minute prices, minute-ordered ROI/stoploss, passive limit entries and exit polling when the minute candles + spreads were downloaded -- form switch "minute prices", on by default). `MECH_LEGACY` stays available for the old behaviour.

**C -- exit variants on frozen decisions** (`frozen_from` = a source run id, `variants` list; run id `fz-...`; no model, $0): rotation on/off, minimum hold hours, ROI table, stoploss, trailing; only exits vary; the baseline variant reproduces the source run (tested: same trade count, mean, exit mix); the report carries the mandatory note ("the 'recent performance' block is not recomputed") and a bot x variant comparison table with CI, net, drawdown, percentile vs random entries. Zero paid calls (asserted: `requests.post` raises in the test).
**D -- no-LLM rule** (mode `rule`, on by default): the strategy's own `is_backtest` placeholder branch run unchanged (formulas printed in the report), same window, step, pairs, exits and fees; per-bot "LLM vs rule" line.
**E -- model choice**: `REPLAY_LLM_PROVIDER/BASE_URL/MODEL/API_KEY/RPM/DAILY_CAP` (+ `REPLAY_LLM_PRICE_*`), one provider + one key (no pooling), RPM and daily cap mandatory, 429 / Retry-After waited out (status `rate_limited`), daily cap persisted per provider+model, cache key includes provider+endpoint+model (DeepSeek's key unchanged), every row labelled with the model. Compose passes the variables through.
**F -- Moonshot**: `universe_mismatch` on its rows, separate "mechanics check only" block, out of summaries.
**G -- /replay**: model selector, rule toggle, minute-price toggle, frozen mode with variant checkboxes (+ custom ROI/stoploss/trailing), results with model per row / rule rows / live-reference flags / comparison table / Moonshot badge, price hint (conservative price, real DeepSeek bill 2-3x lower).

Tests: `backtest/test_p211_calibration.py` (66), `backtest/test_p211_cdefg.py` (83), `backtest/test_p21_replay.py` (201, two legacy-mechanics fixtures now pin `MECH_LEGACY`). Not verified: Docker build, the page against the real stack, an OpenRouter call, a real DeepSeek frozen/rule run, minute-candle download time for a 30-60 day run (first run of a window is slow, then cached).

### P21.1 hotfix: the replay page was empty and Start gave a 500 (Python 3.11)
- Cause: one f-string in `backend/replay/calibration.py` reused its own quote inside `{}` (`f"...{x["ns"]}..."`). That is valid on Python 3.12+ (the dev machine runs 3.14) but a SyntaxError on the production image (`python:3.11-slim`). Importing `runner.py` imported `calibration.py`, so EVERY replay endpoint returned 500: `/api/replay/config` (empty bots, step and model lists on the page) and Start. My earlier test runs were all on 3.14, which is how it slipped through.
- Fix: the line is rewritten; **all suites are now also run on a real Python 3.11 with the production pins** (`uv run --python 3.11 --with fastapi==0.115.0 ...`): 66 + 88 + 201 + 132 + 317 checks pass. A test now scans `backend/` for 3.12-only f-string syntax so this cannot recur unnoticed.
- Hardening: Start returns a readable `502 start failed: <Type>: <message>` (traceback in the container log) instead of a bare 500; an explicitly empty bot list is a 400 ("none is ticked"); the page shows a red message with the HTTP status when the config cannot be loaded, warns before any request when no bot is ticked, and shows the server's reason on a failed start / estimate.

## P22 step 1 (B): Listing Fade validation -- built, run on real Bybit data (in code, not deployed)

Scope of this stage (the spec's order): B only. K1 / A1-A2 / K2-K3 are NOT started; A3 and K4 need an explicit instruction. G: the "Experiments" block exists on `/replay` with the Listing Fade card; the Short-bots and Kronos cards come with their steps.

- `backend/replay/listing_fade.py`: loads the LIVE `ListingFadeStrategy.py` (freqtrade stubbed) and uses its own `populate_indicators` / `populate_entry_trend` for the signals; stoploss, trailing (+3.5% offset, 3%), 14-day max hold, -25% from the listing high, EMA20, the funding veto threshold, max_open_trades 3, stake 25, wallet 100, AgeFilter 2..30 and PriceFilter come from the strategy file / its config.json; SHA-256 of both printed in the report. Nothing re-tuned. VolumePairList (top 200) and SpreadFilter cannot be rebuilt from public history: listed as NOT applied.
- Data: public Bybit API, no keys, cached under `/data/replay/listing_fade/` (instruments incl. `status=Closed` = delisted, hourly candles, funding history, 5-minute candles for the trade windows); idempotent, resumable, paced (0.12 s), errors counted. 779 listings since 2024-01-01 (596 trading, 183 delisted included).
- Simulation: one short per listing event at the open of the candle after the signal; taker fee 0.055% per side; funding credited / charged at every funding timestamp in the hold; stop / trailing fill at the trigger level or at the 5-minute candle's OPEN when it gaps through (worst gap reported); slippage 0 / 0.1 / 0.3 / 0.5% (conclusion on 0.3%); live slot limit (3) and unlimited slots; a signal stays valid for its whole hour so a slot freed inside the hour is taken at once (the live log shows entries one second after an exit); liquidity flag when stake >= 1% of the first hour's USDT volume (34 events), results with and without.
- Verdict is computed (`verdict_of`): edge candidate = n >= 100, CI lower bound > 0, mean > 0 in both halves of the date range (basis: 0.3%, live limits, flagged excluded); unproven = mean > 0 but a condition fails; no edge otherwise. A verdict is printed for all 16 scenario combinations.
- No look-ahead: the rule is causal (cummax / ewm), entries at the next open, funding veto and price filter as of the signal's close; fixture test shuffles all future candles and no earlier signal changes.

**Result on the real data (report: `backtest/results/p22_listing_fade_report.md`):** primary line (0.3% slippage, 3 slots, flagged excluded): n=557, mean -0.17%, CI95 [-0.73, +0.34], halves -0.02% / -0.32% -> **no edge**. Opportunity set (unlimited slots): no edge. At 0% slippage: unproven (mean +0.47%, CI includes 0); only the unlimited / flagged-included / 0% corner reaches edge candidate. Break-even is about 0.15-0.2% per side; win rate ~78%, median +1.5%, the loss tail (stops, -12%) eats it; by listing age: 2-5 days +0.09%, older entries negative (-0.5% / -1.1% / -1.8%).
**Against the live dry-run (73 trades, +0.62% mean):** on the live bot's own pairs the replay (re-entries allowed, fee 0.01%, no slippage) gives 81 trades, +0.44% mean, exits 16 stop / 65 trailing vs live 17 / 56 -- the simulator reproduces the live bot within about 10% on count; over the whole universe it makes 26 trades because the live pair list (not rebuildable) decides which pairs take the 3 slots. The live +0.62% over 10 days sits inside the noise of this study (CI +-0.5% per trade, n=73).

**Bugs found and fixed while doing this (all covered by tests):** (1) Bybit returns a page SHORTER than the limit although older candles exist; the generic `rdata.fetch_klines` and the funding fetch ended the paging there, silently dropping the first days of long windows (found because a trade "closed" 22 days after entry). Paging now ends only on an empty page or when the start is reached; cache files self-heal (the missing head is fetched on the next run; P21's candle cache already re-fetched a head that is earlier than its first row). (2) Liquidity flag was inverted. (3) A stop that the price already reached is evaluated before the max-hold exit of the same candle. (4) A series that starts after the signal, or has a hole inside the hold, is not simulated (counted in the report: 1 event) instead of using wrong prices.
Tests: `backtest/test_p22_listing_fade.py` (75 checks, offline; also green on Python 3.11 with the production pins). Not verified: Docker build with the baked strategy file, the Experiments card against the real stack, the live fetch inside the container (the study here ran on the operator's machine against the real API; a full fetch takes about 1.5 h because of the 5-minute windows).


## P22 step 2 (K1, K2, K3 + the Kronos card): Kronos as a measured signal -- built (in code, not deployed); the 30-day signal run is in progress on the operator's PC

### K1 (measured, confirmed by the operator)
Variant `NeoQuasar/Kronos-small` (24.7M, 99 MB), tokenizer `Kronos-Tokenizer-base`, 480 candles in / 24 out, 3 samples, seed 12345, CPU only. Server (real cycle in `kronos_health.json`): 23 pairs in 261.9 s = about 11.4 s per pair. Operator's PC, 72 consecutive hourly forecasts of BTC (2 threads): 9.59 s mean, 9.71 s p95, 461 MB peak RSS, deterministic. Ledger at the time: 177 `kronos` records, buy 16, sell_short 91, hold 70 (about 7 hours of the ledger). The benchmark ran on Python 3.14 / torch 2.14.0, not on the production 3.13 / 2.14.1; the Docker image was not built.

### K2 -- historical signals (`kronos-replay/`, new container; `backend/replay/kronos_replay.py`)
- `kronos-replay/app/gen.py` is the pure logic (tested with a fake forecaster), `worker.py` the loop, `predictor.py` a copy of kronos-signal's adapter. Weights and model code are baked into the image at the SAME pinned revisions and sha256 as kronos-signal (nothing is downloaded at run time); disk guard in the build; nice 10; cpus 2, cpu_shares 128, 1 GB; no ports; own volume `kronos-replay-data` (mounted in survival-agent at `/data/replay/kronos`, never the live `kronos-data`); idle (no model loaded) until survival-agent writes `job.json`.
- No look-ahead, enforced in `gen.window_for`: only candles with open time <= t0 - 1h are looked at, the newest must be exactly the one opened at t0 - 1h, no gap, >= 400 candles; the candle files handed to the worker stop before the window end. Fixture test: replacing every candle at or after t0 with garbage changes neither the window, the forecast nor the signal.
- Signal fixed in advance: `up` if predicted ret_4h >= +0.5%, `down` if <= -0.5%, else `neutral` (gross of fees); returns at 1/4/24h stored per pair and hour. Resumable (append-only, a half-written last line is healed), sharded (round-robin over (hour, pair) items, balanced), STOP flag file, progress with ETA.
- Estimate before start: 720 h x 20 pairs = 14,400 inferences. One container worker at 9.6 s = 38.4 h (> 12 h: the card says so and proposes 6 pairs; nothing is reduced silently). On the PC 8 single-thread shards measured 25-28 s per inference per shard (about 0.29 inferences/s in total: threads and processes scale badly on this laptop CPU), so about 14 h for 20 pairs -- also above 12 h; reported to the operator.
- Run on the PC (not in the repo): `D:\Trader\kronos_run\kronos\`, job `k20260904-20261004-20p-aaabde` (20 spot pairs, 2026-09-04 .. 2026-10-04, includes RLUSD and XAUT as the top-20 turnover list gave them: fixed, not edited).

### K3 -- evaluation (`backend/replay/kronos_eval.py`)
Signal quality independent of exits (forward 1h/4h/24h after up/down, excess over the mean of all pairs at the same hour, cluster bootstrap by calendar day, both halves) plus the replay engine with Kronos as the decision source (main: up -> buy, shorts: down -> sell_short; `shorts` stands in for shorts_v2 because P22-A is not built), next to the strategy's own no-LLM rule, random entries (same trade count and exit constants), BTC hold, earlier LLM runs (labelled). Verdict computed in code, rules fixed in advance: statistic = 4h direction-adjusted forward return minus the same-hour benchmark minus the round-trip cost (2 x (0.1% fee + 0.3% slippage)); per direction `insufficient n` below 200, `edge candidate` if CI lower > 0 and both halves > 0, `no edge` if CI upper < 0, else `unproven`; overall = best of the two. The same rule on the gross excess is reported as `signal_skill_gross`. Replay never gives `edge`. A stopped generator still gives a clean evaluation of the complete early part of the window (PARTIAL label).

### G -- the Kronos card (`/replay`, Experiments)
Benchmark (a request the worker answers when idle), Generate signals (estimate + confirm, the server refuses without `confirm: true`; progress, ETA, seconds per inference), Stop, Evaluate, verdict badge, per-direction table, engine table, baselines, ZIP, copyable line.

### Tests and what is NOT verified
`backtest/test_p22_kronos_replay.py` (46 checks), `backtest/test_p22_kronos_eval.py` (50 checks: planted-edge worlds, every verdict branch, no look-ahead, end to end on a synthetic market, API, page), both also green on Python 3.11 with the production pins. Three older tests were changed deliberately because the compose file now has the `kronos-replay` service and the `kronos-replay-data` mount (p21_replay, p204_kronos, p18_export). `backtest/test_survival_shorts.py` fails with `ModuleNotFoundError: _allocator_ctx` when run from the repo root (not touched by this work). NOT verified: the Docker image of kronos-replay was not built and the container was not run; the card against the real stack; the volume-ownership assumption (both Dockerfiles make the mount point world-writable); the evaluation has not been run on real signals yet (the generator is still running).


## P22 step 3 (A1 built, A2 built and $0 parts run, C / D run, entry-blocker labels) -- in code, not deployed

### Replay-only entries explained (calibration run cal-20261006-100711)
`live_no_entry_unexplained` is mostly NOT an allocator veto and not a log-order effect (the allocator only scales the stake in `custom_stake_amount`; nothing in it blocks an entry). From the live logs: (1) the exchange's **minimum order size** -- on survival_shorts (wallet 100) the minimum stake of BTC (~$88) / ETH (~$28) / SUI (~$13) is above the free balance or more than 30% above the wanted stake, so Freqtrade ignores the trade (`Minimum stake amount > available balance`, `Adjusted stake amount ... ignoring trade`); (2) the strategy's own **`confirm_trade_entry` funding veto** (QNT, SAND: 1,033 / 2,103 rejections in the shorts / survival_shorts logs) which the replay could not reproduce because it served no funding; (3) **Freqtrade's pair lock** (`Pair BTW/USDT:USDT short is locked until ... due to Auto lock`). Of the 15 survival_shorts rows: 12 explained (6 + 2 min order size, 3 veto, 1 lock), 3 lie after the end of the log in the export used; of the 7 shorts rows: 1 explained, 4 after the log, 2 (ZEC 10-04 19:00, BTC 10-04 22:10) leave no trace in the log. `live_data.parse_log` now records these blockers (`entry_blocks`), `calibration.causes` labels `live_strategy_veto`, `live_min_order_size` (`_probable` when the figure is unnamed and no known minimum matches), `live_pair_locked`; the replay does not model them. Needs a fresh "Update live reference" to appear. The calibration report also has the **outlier-trimmed row** (5% largest |return difference| dropped, 5%-trimmed means) and the **thin-pair flag** (spread >= 0.2% from today's tickers or >= 2 cancelled orders).

### Replay serves funding as-of (data.fetch_funding / load_funding, SeriesStore.funding_asof, sandbox)
`tickers` / `funding/history` answer from the cached public funding history, only settlements <= the simulated clock (the last settled rate stands in for the live current rate). Effects: the shorts prompt gets its funding line, the parents' funding veto now works in the replay, and the v2 bots need it. Old runs (e.g. 20261005-084124) had none: frozen runs follow the source run's setting (`funding` False unless it says otherwise), normal runs default to True. Calibration numbers will move when re-run with funding.

### A1: shorts_v2 / survival_shorts_v2 (`freqtrade-shorts-v2/`, `freqtrade-survival-shorts-v2/`; replay only, no Dockerfile, no compose service)
Generated from the parents by `scratchpad/gen_v2.py` (not kept): rule prefilter (pct_change_24 < -2 and volatility_24 < 5, mirror of the parent's long placeholder), the model answers only `confirm` / `veto` (anything else = veto), no `buy` anywhere, funding line in both prompts (survival_shorts had none), `V2_DECISION` structured line per decision (pair, candidate, vote, reason, price) as the source of the later ledger sources `rule_short` / `llm_veto` (wired only at A3). custom_exit, confirm_trade_exit, stake logic, ROI / stoploss / min-hold and the parent's confirm_trade_entry are byte-identical to the parent (test). The second-opinion shadow call of shorts was dropped in v2.

### A2: built; $0 parts run; the paid veto run NOT run
`analysis.count_candidates` (exact: equals the model calls the replay makes, test every 1/2/3), `analysis.candidate_report` (share vetoed, short forward return 1/4/24h of all / confirmed / vetoed candidates, confirmed-minus-vetoed with a day-clustered bootstrap CI, computed label `veto_adds` with >= 20 vetoes required), per-bot report section, estimate = candidates x LLM modes, rule-only runs (`rule_only`, $0, no key), random-short baseline (existing), card on /replay (variants, with/without veto, window, step, candidate + call estimate, confirm). Proxy on the old run's decisions (`backtest/results/p22_exit_variants_and_short_candidates.md`, force-added): the bare short rule would have fired 153 times in 30 days and its forward short return is NEGATIVE (4h -0.32%, 24h -1.33%); the old bidirectional prompt vetoed 99.3% of them. A real A2 for shorts_v2 on that window is about 153 calls (cents) and needs the server's key.

### C / D on run 20261005-084124 (operator's PC, frozen decisions)
`baseline` reproduces the source run. Rotation is the dominant exit; shorts+LLM without rotation +2.01% [+0.20, +3.86] n=74 is the only CI above 0 of 36 looks (a hypothesis, not a result); tighter stops hurt; the rule through the real exits: main +0.70% [+0.13, +1.32] n=75, shorts -0.25%. Nothing adopted.

### Tests / not verified
`backtest/test_p22_short_v2.py` (static + replay on a synthetic market + funding as-of + A2 analytics + page), `backtest/test_p22_calibration_blocks.py`; both also green on Python 3.11. Not verified: Docker build of the backend image with the two new strategy dirs, the card against the real stack, a Manager rule-only start end to end, the paid veto run, the calibration re-run with funding.


## P23 (07.10.2026) -- Dip and Survival bots removed, MIT licence, Kronos as a decision source for Main / Shorts / Moonshot, LLM provider switch -- in code, not deployed

Operator instruction: "Implement Kronos on Short, long, moonshot ... Make it effective. Remove Dip at all. Remove Survival bots. Add MIT licence. Make it possible to change Main DeepSeek with a free bot or leave it empty."

### Removed (git rm + every registry)
`freqtrade-dip/`, `freqtrade-survival/`, `freqtrade-survival-shorts/`, `freqtrade-survival-shorts-v2/` (replay-only) and their compose services, volumes, `survival-agent` mounts and env vars, baked strategy copies, allocator roster (`MANAGED_BOTS` = main, shorts, moonshot, listing_fade, trend_v2), portfolio brain (6 bots), payout split (main, shorts, moonshot), scorecard, export centre (8 bot ids), signal-audit LLM bots (3), market-context bots (3), `freqtrade_client`, replay engine, dashboard (`index.html` cards, ports, accents, env groups), `kronos-signal` bot order, `auto_tune` choices. `ALLOCATOR_RETIRED_BOTS` and `KRONOS_SKIP_BOTS` now default to empty. The generic retired-bot machinery stays (a future retired bot works the same). Dip/Survival-only tests deleted (`test_p13_*`, `test_p13fix_*`, `test_survival_shorts`); the others were rewritten to use the remaining bots (`test_p12`, `p16`, `p17_portfolio_brain`, `p18_export`, `p19`, `p201`, `p202`, `p204_roster`, `p204_kronos`, `p20_context_allocator`, `p21`, `p211`, `p22_*`, `p8`, `p9`, `payout_state`). Old journal entries above are history and were not rewritten. The old containers and volumes are not touched by this change; after redeploying the stack Docker leaves them as orphans (remove them by hand when you no longer want the history).

### Licence
`LICENSE` (MIT, "survival-agent authors", no personal name, with a not-financial-advice note). README has a licence section and a "before you expose or reuse this" section (no authentication in the web app: never publish port 3446; dry-run only; the CORS origins in the bot configs are the original deployment's hostname). NOT done: rewriting git history (the commit author name and two emails are still in all commits), replacing the host name in the 8 bot `config.json` files (done afterwards: see the entry `CORS origins from FREQTRADE_UI_HOST`).

### Decision source and LLM provider (Main, Shorts, Moonshot)
`_allocator_ctx.py` (identical copy in every bot directory, hash-tested) got `decision_config(bot)` and `kronos_proposal(pair, bot)`. Each of the three strategies: the old `_ask_deepseek` body is now `_ask_llm`; the new `_ask_deepseek(row, pair, *args)` wrapper applies `<BOT>_DECISION_SOURCE` (`llm` | `kronos_llm` | `kronos`) and `<BOT>_LLM_PROVIDER` (`deepseek` | `free` | `none`). `kronos_llm`: Kronos proposes, the LLM may only confirm or veto (answers other than the proposed direction are treated as a veto in code; Kronos `hold` never reaches the LLM). `kronos`: no LLM call. `free`: `_call_openrouter_free_only`, at most 3 `:free` models, never DeepSeek, never the paid last resort of the old fallback. `none` with source `llm` = the bot holds everywhere. Missing / older than 2h15m / malformed Kronos signal = hold (no fallback). Main and Moonshot can only open longs, Shorts both. Exits, stoploss, leverage never reference Kronos (AST test). Compose default is `kronos_llm` + `deepseek` for the three bots; `<BOT>_DECISION_SOURCE=llm` restores the pre-P23 behaviour exactly. The replay sandbox stub always answers `llm / deepseek`.

### Kronos rank feed
`kronos-signal`: universe is now the union of the bots' whitelists (`KRONOS_MAX_PAIRS` 45, cycle budget 900 s: 45 pairs x ~11.4 s measured on the server = ~8.5 min, inside the hour); after each cycle it writes `kronos_rank.json` per bot: score = forecast ret_24h minus the mean forecast of the bot's own scored pairs, top third with score >= +0.3% is `buy`, bottom third with score <= -0.3% is `sell_short`, fewer than 8 scored pairs = all hold. The `kronos` ledger source keeps its old absolute-threshold action (the measured source is unchanged). `backend/engine/kronos_feed.py` (survival-agent, 2-minute loop, `GET /api/kronos/rank`) validates and copies the file to the allocator volume (the bots mount it read-only; they never mount the Kronos volume). Why a rank: the Kronos docs' own demo ranks many instruments and takes the top-K, and warns that raw signals need market neutralisation.

### Evidence, stated plainly (this is why the dry-run measures it, not why it is believed)
Absolute-threshold signals, 15 days, 20 pairs: `no edge` (net 4h excess -0.89% up / -0.86% down). The rank design was fixed in advance and checked once on the same 15 days (`scratchpad`, not in the repo): 24h cross-sectional IC +0.10 [+0.02, +0.18] (first half +0.04 [-0.08, +0.17], second half +0.20 [+0.09, +0.31]); top-third minus bottom-third 24h spread +0.02% [-0.56, +0.67] (top-3 / bottom-3: +0.02% [-1.01, +1.05]) against ~1.6% cost for a long+short round trip. So: a hint of rank information at 24h in one half, no profit from trading the tails. Nothing was tuned on it. Expect `kronos_llm` to lose per trade unless live data says otherwise; the live signal audit (source `kronos`) and the per-bot evidence status decide.

### Tests / not verified
`backtest/test_p23_kronos_decision.py` (helper, the three real strategies in every mode, free-only caller, rank maths, a whole fake cycle, feed, compose, copies, no Kronos in controls / exits). Not verified: Docker builds (backend image without the removed folders, kronos-signal with 45 pairs), the first real 45-pair cycle time, `kronos_rank.json` on the real shared volume, `free` provider against real OpenRouter free models, a live `kronos_llm` decision in a running bot, the dashboard after the removals (JS syntax checked only).


## 07.10.2026 -- CORS origins from FREQTRADE_UI_HOST (no host name in the repository)

The hard-coded host-name list is gone from every `config.json` (`CORS_origins` is now `[]`). Every bot directory has an identical `entrypoint-cors.sh`; the Dockerfiles copy it, compose sets `entrypoint: ["/freqtrade/entrypoint-cors.sh"]` and `FREQTRADE_UI_HOST=${FREQTRADE_UI_HOST:-localhost}` on the 8 bot services. The script validates the host (letters, digits, `.`, `_`, `-`; anything else -> `localhost`), writes `/tmp/cors_origins.json` with `http://<host>:<port>` for all 8 bot ports (8099 8110 8120 8160 8170 8180 8190 8200) and `exec freqtrade "$@" --config /tmp/cors_origins.json` (a later --config wins). Why not the env var Freqtrade offers: `FREQTRADE__API_SERVER__CORS_ORIGINS` lower-cases the key, the config key is `CORS_origins`. **Action for the operator after redeploying: set `FREQTRADE_UI_HOST` (e.g. your NAS host name or IP) in the stack environment; with the default `localhost` the cross-bot FreqUI switch only works when FreqUI is opened on localhost.** The host name still exists in the git history (not rewritten). Not verified: a Docker build and a real container start with the entrypoint.


## P24 (07.10.2026) -- interface redone: bot-centred dashboard, one Downloads place, Training & calibration page, Replay for tests only -- in code, not deployed

Operator instruction: redo the interface completely; everything downloadable must be downloadable from it; centred on bots (strategy and parameters per bot); grouped and clear; all log downloads in one place; Training and calibration a separate tab like Replay, with its logs and reports; Replay for tests only; check that nothing falls off the layout.

### What changed
- `frontend/ui.css` + `frontend/nav.js`: one stylesheet and one header for all pages (grid tracks `minmax(0,1fr)`, wrapping words, tables and `<pre>` scroll inside their own box, tab bar scrolls on phones).
- `frontend/index.html` rebuilt: tabs Overview / Bots / Portfolio / Signals & Kronos / Downloads / Settings; `#bots/<id>` page per bot (8 bots incl. the two controls): strategy (Main/Shorts/Moonshot text updated for the Kronos decision source), parameters (live from the bot's `show_config` plus decision-source and provider switches with their env names), status and money, evidence (scorecard, brain, allocator weight), max open trades per bot, trading mode on Main, per-bot downloads. Existing renderers (brain, allocator, audit, history, env) reused. The bot page is not re-rendered while the operator types in it.
- Downloads: export centre (bot logs, trades, ZIP) + `GET /api/downloads` catalog (accounting, brain, allocator, signals, Kronos, live snapshot/reference, calibration and replay runs with ZIP and log, Kronos evaluations and files ZIP).
- `frontend/calibration.html` (`/calibration`): live reference, calibration (coverage, run, report), Kronos (benchmark, generate, evaluate, result), progress with run-log download, reports and logs list. `frontend/replay.html` (`/replay`) keeps replay runs, exit variants, short-bot study, Listing Fade study, past runs; calibration and Kronos moved out. All page text is English.
- Backend: `backend/downloads.py`, `backend/bot_info.py`, five new GET routes and `/calibration`; compose passes `MAIN|SHORTS|MOONSHOT_DECISION_SOURCE` and `_LLM_PROVIDER` to `survival-agent` so the Bots page can show them (display only).

### Verified
`backtest/test_p24_downloads.py` (catalog, path safety, denied names, routes, params endpoint, page structure, English only). Layout checked in a browser against a mock stack (fake Freqtrade REST, seeded data, one bot offline) at 1280, 768 and 375 px on every tab and page, with wide tables injected: no element outside the viewport except inside scrolling boxes. Older UI tests were updated to the new strings (the checks themselves are unchanged in meaning). Not verified: the Docker build, real data on the server, the real Freqtrade `show_config` shape (fields are copied if present, absent ones are skipped).


## 11.10.2026 -- fix: portfolio brain high-water mark vs roster changes (found in the server export; it had paused every bot since 2026-10-07)

Server export of 2026-10-11 showed: `allocator_latest.json` all weights 0, `paused_by_allocator` = all five managed bots, rule `ladder_L3_stop_entries` every day since 2026-10-07/08. Cause: the brain's high-water mark (2,573.88, set 2026-09-30) included the removed Dip and Survival bots; when they left the book (P23) equity fell to ~1,766 and the brain read a -31.4% drawdown (real, same six bots: about -1.3%). The weekly change had the same flaw. In enforce mode the allocator paused all bots, so Listing Fade also stopped (its 89 trades are all before the pause) and Main / Shorts / Moonshot closed no trade in the Kronos decision mode.

Fix (`backend/engine/portfolio_brain.py`): `restate_snapshots()` re-expresses every past snapshot for TODAY's roster (a snapshot lacking one of today's bots is dropped); the high-water mark and the weekly / monthly windows use it; the state stores `hwm_roster` and a roster change rebases the mark with a note (`capital.hwm_rebased`) -- also for a state file written by the old code (roster read from the snapshot of the mark's day). A genuine loss with an unchanged roster still triggers the ladder (tested). New `POST /api/portfolio/brain/run` and a "Refresh brain now" button (Portfolio tab) run the brain at once instead of waiting for 00:10 UTC. `backtest/test_p24_hwm_roster.py`.

To apply: deploy, press "Refresh brain now", then "Run allocator now" (the ladder event is read from the brain's report).


## 12.10.2026 -- fix 2: the allocator had its own copy of the same flaw (weekly change from raw snapshots)

After the brain fix (6016c25) the server export showed the brain clean (drawdown -0.98%, no rung) but `allocator_latest` still fired `weekly_loss_-2pct_pause_entries` and kept shorts / moonshot / listing_fade at 0: `allocator.gather_state()` computed the week-ago equity from the RAW snapshots (9-bot book, ~2,550) against today's six bots. Same restatement now there (`pb.restate_snapshots`, roster-aware mark); `test_p24_hwm_roster.py` covers it. Also seen in that export: `reference_capital` 2,300 instead of 1,800 (floor 2,070 above equity, CPPI budget 0) -- exactly +500, i.e. a 500 entry in the capital ledger (`capital.json`, the Record form on the bot page). The ledger is accounting only; it does not add money to a dry-run wallet. The Record form now says so. A wrong entry is corrected by recording the negative amount.


## 12.10.2026 -- per-bot dry-run wallet variables

Operator gave the shorts bot +500 paper money by recording it in the capital ledger; that never reached the bot (its wallet is `dry_run_wallet` 500 in `config.json`, no variable). Every bot service now passes `FREQTRADE__DRY_RUN_WALLET=${<BOT>_DRY_RUN_WALLET:-<config value>}` (MAIN, SHORTS, MOONSHOT, FADE, TREND, TREND_V2, CONTROL_LONG, CONTROL_SHORT); documented in ENVIRONMENT_VARIABLES.md; `test_p24_downloads.py` checks the defaults against each config. Rule: the ledger and the variable must say the same. Not verified on the server: that Freqtrade 2026.8 recomputes the dry-run wallet from the new value at restart on an existing trades DB (it should: the dry wallet is built from `dry_run_wallet` plus the DB's profits).

Listing Fade age window: the 2024-2026 study (557 trades, 0.3% slippage) has by-age rows 2-5d -0.02%, 5-10d -0.99%, 10-20d -1.16%, 20-30d -1.68% per trade, so a 60-day window is not supported by the data (and the cached hourly history is 31 days, so >30d cannot be replayed). Kept at 30.


## 12.10.2026 -- P25: announcement guard, funding carry study (both built, tested, in code, not deployed)

Operator: "Build both" (announcement veto + carry replay) and "can't we just get data for unlocks and trend?". Data checks done first (PC, public APIs): Bybit announcements `type=delistings` has 483 notices back to 2022-08 (paging works), Binance's list has 440 but its titles carry no tickers (not parsed); funding history and daily candles are free; DefiLlama's emissions (unlocks) endpoint is paid (HTTP 402), so unlock events cannot be backtested as-of from free data (log schedule snapshots from now on if wanted). Trend / momentum needs only candles (free).

### Announcement guard
`backend/engine/announcements.py` (parser: 259 perpetual + 41 token notices of 483 read; USDC / inverse / pre-IPO / "Selected" lists deliberately not read), `/api/announcements`, loop every 10 min, ledger `announcements.jsonl`, `blocked_pairs.json` on the allocator volume. `_allocator_ctx.entry_block(pair)` (identical in six bot dirs) is the first statement of `confirm_trade_entry` in Main, Shorts, Moonshot, Listing Fade, Trend v2 (and the replay-only shorts_v2 copy); the replay sandbox stubs it to None; the controls and Trend v1 are untouched (tested). Window: publish time to delisting time + 1 day (14 days when the notice has no date); stale (>6 h) or missing file blocks nothing. Measured on the 2026-10-11 export: 0 of 567 trades opened inside a perpetual-delisting notice window (median lead 2.0 days), so no measured benefit yet. `backtest/test_p25_announcements.py`. The P20 test that forbids helper references in entry logic now allows exactly this call.

### Funding carry study
`backend/replay/carry.py`, `POST /api/replay/carry/study`, card on /replay (Studies), `backtest/test_p25_carry.py`. Daily bars, decisions from data before the day, fills at the open; universe BTC, ETH + top 30 by trailing 30-day turnover as of each day among perpetuals that have a spot market today; entry when 3-day funding (annualised) >= 2 x round-trip cost x 365 / expected hold (majors 60 d, alts 14 d); exit when it turns negative or at delisting; fees spot 0.1% x2 + perp 0.055% x2, slippage 0.1% majors / 0.3% alts per round trip; thresholds fixed before the run (OOS from 2025-07-01, >=60 episodes, net APR on deployed >=5%, drawdown <=3% of the sleeve, worst episode >= -1.5%, CI > 0, beat BTC/ETH-only by 1 pp). A first run without the spot requirement looked like +14.8% a year; its top earners (BAKE, FARTCOIN, AIA, BTW, NEIROETH, REEF) have no Bybit spot market, so the long leg is impossible -- fixed, and the unconstrained run is kept only as a labelled `NOT_REALISABLE` scenario. Result with the spot leg (`backtest/results/p25_carry_study_real.md`): 2024-01..2026-10 +$571 on 3,000 (6.9% a year), locked period (to 2025-06) 11.8% a year, out-of-sample +$40 (1.0% a year, 27 episodes, verdict `insufficient n`); BTC/ETH-only out-of-sample -0.1% a year; funding now 1.7% (BTC) and -0.4% (ETH) a year. Known limits (in every report): daily bars hide basis spikes and liquidations, no margin or haircut model, today's spot list only (delisted symbols dropped), exchange / custody / legal risk not a number. Fetch is parallel (6 workers), resumable, and only downloads index and funding for symbols that are ever in the as-of top 30 (128 of 391 here; 999 calls, 134 s).


## 12.10.2026 -- P25-B/C: delisting short study and momentum study (built, run on real public data, both $0)

Operator: "build both studies". Code: `backend/replay/delist_study.py`, `backend/replay/momentum.py`, `ReplayManager.start_study`, `POST /api/replay/delist/study`, `/api/replay/delist/state`, `POST /api/replay/momentum/study`, two cards on /replay (Studies); `backtest/test_p25_studies.py`. Reports: `backtest/results/p25_delisting_study_real.md`, `p25_momentum_study_real.md`.

### Delisting shorts (250 Bybit perpetual notices since 2024-01-01, 217 simulated; 10 without candles, 23 thin)
Short at the first hourly open >= 1 h after the notice, cover at the earlier of 72 h / delisting - 2 h / a +10% stop; taker fees, funding, slippage 0.6% round trip (primary), BTC hedge, 20 random same-length shorts per event as baseline. Primary result: mean +0.24% hedged (median -3.2%, 42% winners, CI [-1.6, +2.1], worst -22%), **99 of 217 trades hit the +10% stop**, first half +1.6%, second half -1.1%, 93rd percentile vs random shorts. Entry delay 0 h: +0.02%; 6 h and 24 h: negative. Verdict `unproven` (fails mean, CI, percentile, worst event, halves). The "delisting notices crash the coin" story is true on average only for the first minutes, which a home server cannot trade; after a 1 h delay there is no net edge. No bot.

### Momentum (top 20 perpetuals by turnover, weekly book, long top 4 / short bottom 4 by 14-day return, dollar neutral)
Primary (14 d, long-short, 0.155% per side): 2024-01..2026-10 +116% of the sleeve (APR 42%), locked period APR 31%, out-of-sample (since 2025-07) APR 55% with Sharpe 0.94, but the block-bootstrap CI of the out-of-sample mean is [-39%, +159%] a year, max drawdown 44% of the sleeve, placebo percentile 96.7. The sign is not stable across lookbacks: 28 days loses 77% a year out of sample, 7 days is negative in the locked period. Verdict `unproven` (CI includes 0, drawdown over 20%). The top-20 by turnover include very volatile coins. Not a bot candidate as is; a paper test of the 14-day book would need a volatility scaling / risk cap defined in advance.


## 13.10.2026 -- "Why is it not trading?" card on every bot page

Operator sent Freqtrade UI screenshots of Shorts, Moonshot and Main (few trade markers since Oct 5, none after the Oct 7 pause except tiny ones on Oct 12). `backend/diagnose.py` + `GET /api/bots/{id}/why?hours=24` + a card on each bot page: counts from the bot's own log of the last hours (decisions, LLM vetoes of Kronos proposals, `no fresh Kronos signal`, refusals grouped by cause, `stake too small` entries with the numbers, entry fills, rule signals), the bot's state and slots from its REST API, and the allocator weight (a stale weights file is reported as ignored). Replayed on the 2026-10-11 export (Oct 8-11 window): Main 361 decisions, all hold, **123 of 123 Kronos buy proposals vetoed by the LLM**; Shorts 111 sell_short decisions and 0 trades while the bot was PAUSED by the allocator; Moonshot 49 of 49 vetoed. `backtest/test_p25_diagnose.py`.


## 14.10.2026 -- allocator `grow` policy (new default)

Operator: "make a Grow policy ... the allocator should protect the money but not stop trading ... raise the profitable, stop the unprofitable, monitor and restart them when the market looks promising". `backend/engine/allocator.py`: `grow_bot` / `grow_policy` / `effective_policy`; `ALLOCATOR_POLICY` (`grow` default | `protect`) in `runtime_settings` (dashboard switch), compose, `.env.example`, docs. Rules: >= 30 closed trades since the bot's configuration last changed (epoch = its decision source / provider signature; rule bots judge the last 30 days) -> earner (net > 0 over the period and the last 7 days) 1.5x, 2.0x with evidence `edge`, raised by 0.25 a day; loser (net < 0 both) stopped at once; a stopped bot waits >= 3 days and gets a 0.5x trial when its control bot earned over 7 days, then is confirmed (>= 5 trades, net > 0) or stopped again; everything else 1.0. Portfolio protection only scales (x0.5 below -15%, x0.25 below -25% from the restated high-water mark, macro cap 0.5, opposite-side and concentration caps 0.5); weekly-loss, ladder and CPPI rules and the LLM call are not used under grow. A run whose dry-run check finds a live or unverifiable bot uses `protect`. The bots' helper (`_allocator_ctx.stake_multiplier`) now accepts weights up to 2.0 and `custom_stake_amount` raises the stake for a weight > 1, capped at `max_stake` (six identical helper copies, five strategies + the replay-only shorts_v2). The old suites pin `protect` (they test that path); `backtest/test_p25_grow_policy.py` covers grow (state machine, mix of winner / loser, restart through the control bot, new epoch, scaling, automatic protect, a full run without an LLM call, the stake maths in the five real strategies). Not verified on the server: how the first run behaves with real trade histories (all LLM bots start a fresh 30-trade measurement, so on day one everything is 1.0 except rule bots with 30+ trades: Listing Fade 1.25 -> 1.5).


## 14.10.2026 -- LightGBM walk-forward study (P25-D)

`backend/replay/lgbm_study.py`, route `POST /api/replay/lgbm/study`, card on the Replay page (Studies tab), `backtest/test_p25_lgbm.py`, `lightgbm==4.5.0` in `backend/requirements.txt` (+ `libgomp1` in the backend image). Pre-registered: top-20 perpetuals by turnover, 20 features known at 00:00 (returns 1-28 d, 14-d vol, turnover ratio, 7-d funding, distance from 28-d high, BTC regime, daily ranks), label = next-7-day return minus funding minus universe mean, fixed small LightGBM (7 leaves, 200 trees, no search), expanding window retrained every 28 days with purged labels, first 365 days training only, weekly long-top-4 / short-bottom-4 book with the momentum study's costs. Pass needs OOS APR >= 10%, CI above 0, locked period positive, DD <= 20%, >= 95th percentile of 20 shuffled-label pipelines, rank IC >= 0.02, and beating the 14-day momentum rule. Real run (`backtest/results/p25_lgbm_study_real.md`, 62 s): verdict `unproven`. OOS (2025-07 to 2026-10) net +13.7% of sleeve, APR 10.7%, but CI [-46, +76], DD 30.8%, placebo percentile 70, rank IC 0.033 (CI -0.02..0.10), and with 0.30% slippage APR -2.9%. Beats the 14-day momentum rule (APR 7.2%) only inside the noise. Top features are volatility, distance from the high, turnover ratio: risk proxies, not direction. Reading: no tradeable edge proven; do not build a bot on it. Daily bars only; an hourly or cross-exchange feature set is the next honest test, not a bigger model.


## 14.10.2026 -- LightGBM v2 (Binance 2021+, top 40, hourly features): the first near-pass (P25-E)

`backend/replay/lgbm_hourly.py`, route `POST /api/replay/lgbm-hourly/study`, card "LightGBM v2" on the Replay page, `backtest/test_p25_lgbm_hourly.py` (fake archive and exchange API), `lgbm_study.py` now takes horizon / features / params / hold / quintile. Data: Binance USDT-M 1h klines (taker-buy volume) and funding from the free archive data.binance.vision plus the exchange API tail; 899 contracts incl. delisted, 45k calls, 83 min first download, cached and resumable. Design pre-registered in the module docstring: 39 features known at 00:00 UTC, label = next-3-day return minus funding minus the universe mean, fixed LightGBM, retrain every 56 days with purged labels, first year training only, weekly... every 3 days long top 8 / short bottom 8 of the top 40, 0.055% fee + 0.10% slippage per side; placebo = 30 shuffled-label pipelines; baselines 14-day momentum and 3-day reversal; stress 0.30% slippage must stay positive. The robustness block and the clipped-returns condition were added after the first real run.

Real run (`backtest/results/p25_lgbm_v2_binance_real.md`, 9 min on cached data): verdict `unproven`, the ONLY failed condition is max drawdown 54.2% of the sleeve (limit 20%). Out-of-sample 2024-07..2026-10: APR 99%, CI [34, 169], Sharpe 1.7, rank IC 0.069 (CI 0.044..0.092, 63% of weeks positive), placebo percentile 100 (placebo median -34%), 0.30% slippage still +69% APR, locked period +52%. Robustness: clipping every daily coin return at +-50% keeps OOS APR 75%; dropping the 88 coins that ever had a >100% day keeps 72%; every calendar year positive (2022 only +9%); both legs earn (long 51%, short 33% OOS); without the best 20 days the total is still +140% of the sleeve. The 14-day momentum rule alone gets 92% OOS but is NOT robust: neighbours mom3/7/28 are negative, clipping drops it to 54%, its short leg earns 10%. So the model is more robust than the rule it mostly learned from (top features ret168, funding, vol168, ret336).

Reading: the first study in this project with a plausible edge, but a drawdown of 54% means it is not tradeable as built, and a backtest on daily marking with a taker-cost model is a hypothesis, not proof (no intraday stops, no margin, delisted coins held flat, slippage on small alts unknown, Binance data while the bots trade Bybit). Not built into a bot. Proposed next step (needs operator OK): a shadow job that retrains monthly, writes the daily ranking and paper-trades the 3-day book with position caps / volatility scaling as a NEW pre-registered variant, evaluated forward for 8-12 weeks against this study before any bot or real money.


## 15.10.2026 -- Kronos 30-day historical evaluation finished: no edge; Fade log read

Kronos-small, 20 pairs, 2026-09-10..2026-10-10, 14,400 hourly forecasts (11.5 s each on CPU), pre-registered evaluation (`backtest/results/p25_kronos_30d_eval_real.md`): verdict `no edge` (net of 0.8% round trip) in both directions at 1h/4h/24h; gross skill `unproven` (up) and `no edge` (down: gross excess at 24h -0.34%, CI [-0.66, -0.09], i.e. the "down" calls were followed by relative OUTperformance). Replay engine with Kronos as decision source: main -87.6 USD (600 trades, mean -0.55%), shorts -211.8 USD (673 trades, -1.51%), both below the rule baseline's percentile. Extra check against Binance hourly closes (rank IC of the forecast vs realised return across the 20 pairs, every hour, day-cluster CI): 4h forecast vs 4h IC 0.018 [-0.026, 0.062]; 24h vs 24h IC -0.002 [-0.071, 0.064]; 1h IC 0.005; i.e. no cross-sectional skill. Strong bearish bias: mean predicted 4h return -0.91% against a realised +0.19% (BTC +6% in the window); 46% of the signals were "down" and those were followed by +0.24% on average, which is why the short bots lose in a rising market. For comparison LightGBM v2 has a 3-day rank IC of 0.069 [0.044, 0.092] over 833 out-of-sample days. Caveats: Kronos window is 30 days (one regime), different horizon.

Fade log 2026-10-13..15 (listing_fade, 23 closed trades, 17 wins, realised +0.35 USDT): stake is 12.5 USDT not the configured 25 (allocator weight 0.5); three -12% stop-losses (-4.8 USDT) cancel twenty small trailing-stop wins (+0.12..0.66 USDT each); 4 candidate pairs, all 4 held at the same time, hence "No currency pair in active pair whitelist"; positions stay open until the +3.5% trailing offset, the -12% stop or max hold. Behaving as designed. Lever to earn more is the stake (grow policy raises a winning rule bot to 1.5x), not the exits; 23 trades are too few to tune exits.


## 24.10.2026 -- circuit breaker, Listing Fade re-entry cooldown, Fade exit-mechanics study (P26)

Trigger: export of 23 Oct. Every bot has a negative-skew payoff: Fade 77% wins but needs 75% (avg win +4.1%, avg loss -12.5%, 123 trailing exits +97.7 USD vs 34 stops -91.4 USD, net +6.3); shorts roi +92.7 vs stops -91.1; control_long (no LLM) 139 wins +77.8 vs 26 stops -128.7; trend_v2 lost 10 of 10 trades (-32.3 USD, all -15% stops) yet kept weight 1.0 because grow only judges after 30 trades and the bot makes one trade a week; Fade re-shorted the same pumping coin again and again (DNA 12 trades / 3 stops, STONK 42 / 10) and stops filled beyond the nominal -12% (CT -30.9%).
Built: (1) grow-policy circuit breaker (`allocator.breaker_reason`, switches `ALLOCATOR_BREAKER` / `_STAKES` 1.25 / `_STREAK` 8, dashboard + env): at least 5 closed trades since the last (re)start and net below -1.25 average stakes, or the last 8 trades all losses -> class `breaker`, weight 0, restart through the normal control-bot trial, `breaker_since` makes a restarted bot start clean, a trial can be stopped early; the old `stopped` class keeps priority for 30-trade losers. (2) `allocator.control_name`: a futures bot that trades long (trend_v2) is restarted by control_long, not control_short. (3) Listing Fade re-entry guard: `reentry_blocked` + `confirm_trade_entry`, `FADE_REENTRY_COOLDOWN_H` (default 24, 0 = off, fail-open), changeable from the dashboard without a restart through `bot_params.json` (`backend/engine/bot_params.py`, `_allocator_ctx.bot_param`, published every 10 minutes and on save, stale > 6 h = the bot's environment value). (4) `backend/replay/fade_exit_study.py`, Replay page card "Listing Fade exit mechanics": the live Listing Fade rules replayed on the Listing Fade history with 4 stops x 2 cooldowns, ONE pre-registered decision (stop -8% + 24 h cooldown vs live, deciding period from 2025-07-01, paired daily difference with block bootstrap CI, locked period not worse, drawdown not worse, still better at 0.50% slippage); `listing_fade.run_scenario(cooldown_h=)`. Dashboard: breaker and cooldown switches with what the bot currently sees, breaker class in red, run-list badge. Tests: `backtest/test_p26_breaker_guard_exit.py`; full suite passes. A tighter live stop is NOT changed by this work: only a `better candidate` from the study may motivate a live dry-run of it.
What to expect (told to the operator): protection, not new profit. A what-if on the 4 live weeks (in sample, no gap fills) put tighter stops at roughly +20..30 USD a month over all bots (1-2% of equity), a wide range; sizing cannot create profit at about zero edge.

Fade exit study, real run (`backtest/results/p26_fade_exit_study_real.md`, 597 listings with 5-minute candles, 803 listings since 2024-01, 4,713 live-rule trades, 0 fetch errors): verdict for `stop8_cd24` = `not better`. At 0.30% slippage per side the live Fade rules lose in the replay (net -413 USD, mean -0.50% per trade; deciding period from 2025-07-01 -254 USD), as the earlier Listing Fade study (`no edge`); the live dry-run earns +0.3% per trade only because it pays 0.01% fee and no slippage. Tighter stops make it worse in every variant (more stops: -8% gives 1,072 stops and -1,000 USD; -6% -1,696 USD). The cooldown alone (`cd24`, not the pre-registered decision) is modestly better: deciding period -193 vs -254 USD, drawdown 184% vs 221% of the sleeve, better at 0.50% slippage too, but the paired CI [-0.15, +0.38 USD/day] includes 0. Decision: keep the live stop at -12% (no change), keep the 24 h cooldown as a cheap, evidence-leaning guard, do not promote a tighter stop. Note the simulated stop fills never gap (mean 0.0%) because 5-minute candles rarely open through a stop, while the live dry-run showed fills at -14% .. -31%; the real cost of stops is therefore under-stated here. Fade's apparent profit depends on passive (maker) fills: whether those fills are real is the open question, not the exit rules.


## 26.10.2026 -- LightGBM ranker shadow (paper) forward test (P26-R)

Operator: "1 please" (build the shadow job for the LightGBM v2 lead). `backend/engine/ranker.py`, `RANKER_MODE` = off (default) | shadow (dashboard + env), `_ranker_loop` in `app.py` (every 30 min, once a day after 00:15 UTC, idempotent, catches up after downtime), `GET /api/ranker`, `POST /api/ranker/run`, Portfolio-tab section "LightGBM ranker", `backtest/test_p26_ranker.py`. It never places an order and imports nothing that talks to a bot (a test greps for it).
Daily job: keeps the Binance cache of `lgbm_hourly` fresh (exchange API tail for the 120 most liquid cached contracts; `lgbm_hourly.fetch_all` and a retrain every 28 days; the first start downloads the history, about 0.5 GB / 1.5 h, and trains with about 2 GB RAM), marks the paper books of the missing days with the study's own arithmetic, ranks the top-40 universe with the saved model (`DATA_DIR/ranker/model.txt`), rebalances every 3 days (long top 8 / short bottom 8, half the sleeve per leg), scores each earlier ranking against what the coins really did (rank IC). Two books from one ranking: A plain (as in the study), B volatility-scaled (exposure x min(1, 1.0% / trailing 20-day vol of A), scaled down only, 0.5 until 10 days exist). The scale target comes from arithmetic on the study's volatility (2.4% a day; a rough re-scaling of its out-of-sample series gave max DD 54% -> 30% at a 1.0% target and 43% at 1.5%), not from fitting returns. The ledger stores gross P&L and turnover; costs (0.055% + 0.10% per side, 0.30% as the stress line) are applied when it is read.
Forward verdict, computed by code: `collecting` until 56 days and 18 rebalances; `forward pass` only if mean rank IC >= 0.02, book B net > 0 at 0.10% AND 0.30% slippage and its max DD <= 20% of the sleeve; `failing` if B < 0 at 0.10% with no positive IC; else `unproven`. A forward pass is a hypothesis for a longer test, never `edge`. `lgbm_hourly._read_sym/build` got a `since_month` argument so a daily run reads only the last months.

Ranker robustness grid (`backtest/run_ranker_robustness.py`, `backtest/results/p26_ranker_robustness_real.md`, 7 min): same walk-forward, features, fixed model and costs, only hold {2,3,5,7} d x universe {20,40,60}. Out-of-sample (from 2024-07) rank IC is positive in 12 of 12 settings (0.049 .. 0.093) and net APR is positive in 12 of 12 at 0.10% slippage and 9 of 12 at 0.30%: the lead is a neighbourhood, not one lucky corner. But the primary (hold 3 / universe 40) is also the best cell of the grid (Sharpe 1.69; neighbours 0.4 .. 1.3), so expect forward results below the backtest; the 60-coin universe earns much less (13..27% APR, negative at 0.30% in 3 of 4 holds), max drawdown is 41..159% of the sleeve in every cell, and costs matter (hold 2 and 7 are the weakest at 0.30%). Signal: real and stable. Money after costs: sensitive. The forward paper test decides.

Premium-index features (`backend/replay/lgbm_premium.py`, Replay page card "Premium-index features", `backtest/results/p26_ranker_premium_real.md`): one pre-registered comparison, 39 v2 features vs the same plus 8 premium features (mark vs index price, Binance archive `premiumIndexKlines`, 15,701 files, 0 errors). Verdict `keep v2 features`: out-of-sample rank IC 0.0683 vs 0.0690 (-0.0007), APR at 0.30% slippage 46% vs 68%, locked IC 0.053 vs 0.067; all three adopt conditions failed. The crowding gauge adds nothing the model did not already have (funding, returns, order-flow features). The robustness grid is now also a Replay card (`backend/replay/lgbm_grid.py`, verdict `neighbourhood holds` / `mixed` / `fragile`, same numbers as `p26_ranker_robustness_real.md`); the two one-off scripts were replaced by these modules. Tests: `backtest/test_p26_research.py`. Download step retries a reset connection 3 times.
Next candidates (not started): open interest / long-short ratio (daily files only in the archive, ~100k files), the maker-fill cost model for Fade and the ranker, a smaller-coin variant; each needs its own pre-registered test.

Maker-fill cost model (`backend/replay/maker_study.py`, Replay page card "Maker fills", `backtest/test_p26_maker.py`, `backtest/results/p26_maker_study_real.md`, 22 min): Part 1 prices limit-order execution for the LightGBM ranker book (limit delta better than the 00:00 open, 1/2/4 h to fill, then skip or chase as taker; maker fee 0.02%; the taker mode reproduces the v2 study's book to the last digit, a test checks it). Pre-registered primary: 0.10%, 2 h, chase. Verdict `passive pays`: out-of-sample APR 110% vs 98% for the taker book at 0.10% slippage and 68% at 0.30%, 92% of orders fill, skip mode (missed trades stay missed) still 82..97%; the gain is the fee/slippage saving, about +13 percentage points, drawdown 49% vs 54%. All 18 grid cells with chase beat the taker book. Part 2 (Fade entries, 4,713 replayed trades): a limit 0.1% above the open fills 95..97% of the signals; the trades that would NOT fill are the winners (taker P&L +0.32..+0.55 USD per trade against -0.11 for the ones that fill): adverse selection is real for Fade. The replay total moves from -413 USD (taker entry, 0.30% slippage) to about +15..+53 USD with passive entries: Fade is break-even at best once entries are free, and its dry-run profit rests on those fills.
Caveats (in the report): fills are decided by a touch of the hourly / 5-minute high or low, with no queue position or order-book data (delta 0 is an optimistic bound), the maker fee is the public tier. A forward check with the live paper ranker (record the real intraday highs and lows and book C as passive) would test the assumption without risking money; not built.

Ranker book C (passive execution, 26.10.2026): the forward test now keeps a third view of the paper book, C = A traded with limit orders (the maker-fill study's primary: limit 0.10% better than the 00:00 open, 2 h to fill, then a taker chase). A day after each rebalance (`ranker.evaluate_passive`, `passive_outcome`) the job reads the REAL hourly highs and lows of that morning and records which orders would have filled (`kind: passive` ledger rows with filled / chased counts, filled gain, chase move and chased notional); `forward_stats` charges C that outcome, or the taker cost while a rebalance is not evaluated yet (conservative), and reports fill rate, chased count, average adverse move and the saving against A. Dashboard: row "C = A with limit orders" in the paper-book table and a line with the live fill rate against the backtest's ~92%. Book C is information, not part of the verdict. Tests in `backtest/test_p26_ranker.py` (hand-made bars, exact arithmetic, end-to-end ledger rows).


## 26.10.2026 -- public-release preparation

Docs: README rewritten as a landing page (badges, contents and document map, results-at-a-glance table of every study with its verdict, mermaid data-flow diagram, updated status, layout and verification sections); ARCHITECTURE.md and HOW_IT_WORKS.md carry a current status note and a `grow` policy note, the bot table lists all eight bots; new SECURITY.md (the dashboard has no authentication: private network only) and CONTRIBUTING.md (house style for studies). All Cyrillic text was translated to English across code, tests and docs (statuses `not enough data` / `not connected`, the replay banner, comments); `backtest/test_p24_downloads.py` still checks that no page contains Cyrillic.
Audit for publication: no credential in the working tree or in any revision, except two secret-shaped strings that were test fixtures in `backtest/test_p18_export.py` (a Portainer API key lookalike and an expired Freqtrade access token); both replaced by obviously fake values in this change, but they remain in earlier commits. The git history also carries the author's personal e-mail addresses. Rewriting history (or publishing a clean single-commit snapshot) is the owner's decision; the Portainer key must be revoked and `FREQTRADE_API_JWT_SECRET` rotated whatever is decided.
