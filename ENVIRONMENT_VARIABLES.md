# Environment Variables

Every environment variable this stack recognizes, what it does, which
service(s) read it, and its default. **All of them are set in one place:
Portainer → Stacks → survival-agent → Editor → Environment variables**, then
redeploy. This is the only place — the dashboard's Credentials panel is
read-only (it mirrors `GET /api/env-status`, it does not accept input), and
`data/.env` is not the source of truth for what any bot actually uses. See
[ARCHITECTURE.md](./ARCHITECTURE.md) §5.2 for why.

A missing variable is not an error — every one of these has a safe default
(shown below) baked into `docker-compose.yml` via `${VAR:-default}`
substitution. Nothing requires every variable to be set; set only what the
feature you want actually needs.

---

## 1. Shared credentials (`general` group)

Read by the three LLM bots (Main, Shorts, Moonshot). Not read by the
isolated Survival Bot.

| Variable | Purpose | Default | Secret |
|---|---|---|---|
| `DEEPSEEK_API_KEY` | Primary entry-decision brain for all four bots. | *(empty)* | Yes |
| `OPENROUTER_API_KEY` | Fallback brain when DeepSeek errors/times out; also one voice in the P5 second-opinion ensemble. | *(empty)* | Yes |
| `FREQTRADE_API_USERNAME` | Shared FreqUI login username across all four bots. | *(empty)* | No |
| `FREQTRADE_API_PASSWORD` | Shared FreqUI login password. | *(empty)* | Yes |
| `FREQTRADE_API_JWT_SECRET` | Signs Freqtrade's own API session tokens. Any long (32+ char) random string — not issued by an external service. | *(empty)* | Yes |

## 2. Survival/accounting framing (`general` group)

Fed into every live bot's LLM prompt as plain context ("this account pays
~$X/mo and has ~Y months of runway..."). Read by all four live bots. **Not**
related to the isolated Survival Bot below despite the similar name — see the
warning in §5.

| Variable | Purpose | Default |
|---|---|---|
| `SURVIVAL_MONTHLY_USD` | Monthly operating bill (LLM API costs + infra) the bots are told they must earn back. | `60` |
| `SURVIVAL_RUNWAY_MONTHS` | Months of zero-withdrawal runway left, told to the model. | `4` |
| `SURVIVAL_MODE` | `grow` = zero withdrawals, compound everything. `extract` = take payouts per the configured split. | `grow` |
| `SURVIVAL_TARGET_MONTHLY_USD` | Monthly payout the operator needs once the runway ends. | `1000` |

## 3. P5 second-opinion ensemble — provider keys (`general` group)

Optional. Shared by all four live bots' ensembles (Phase 1, shadow mode only
— see [HOW_IT_WORKS.md](./HOW_IT_WORKS.md) §4a). None of these are required;
the ensemble simply gathers fewer votes (down to zero, i.e. a no-op) if a key
is missing.

| Variable | Provider | Tier | Free-tier limits (verify before relying on them) |
|---|---|---|---|
| `XAI_API_KEY` | Grok (xAI) | 1 | Not independently verified this session |
| `NVIDIA_API_KEY` | NVIDIA NIM | 1 | Not independently verified this session |
| `MISTRAL_API_KEY` | Mistral | 1 | 1 req/s (~60 RPM), 500k tokens/min, 1B tokens/month — per help.mistral.ai |
| `LLM7_API_KEY` | llm7.io | 2 (core-tier decisions only) | 40 req/min, 1M tokens/24h |
| `GEMINI_API_KEY` | Gemini 2.5 Flash-Lite | 2 (core-tier decisions only) | 15 RPM / 1000 RPD per a third-party tracker — **verify at aistudio.google.com/rate-limits before relying on this number** |

Tier 1 is used on any "borderline" (non-`hold`) decision. Tier 2 is added
only when the pair is also `core` tier. See ARCHITECTURE.md §8.1 for the
voting rule.

## 4. Per-bot: Main (`main` group)

| Variable | Purpose | Default |
|---|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | Bybit key/secret, spot, long-only. Use a dedicated sub-account with no withdraw permission. | *(empty)* |
| `FREQTRADE__DRY_RUN` | Paper/live toggle. Empty or `true` = safe paper trading. `false` = real orders. | *(empty, treated as `true`)* |
| `FREQTRADE_SECOND_OPINION_ENABLED` | Turns on Main's P5 ensemble (shadow mode: logs a vote, never overrides the real decision). | `false` |

## 5. Per-bot: Shorts (`shorts` group)

| Variable | Purpose | Default |
|---|---|---|
| `BYBIT_SHORTS_API_KEY` / `_SECRET` | Bybit key/secret, futures, 1x leverage. Use a separate sub-account from the other bots. | *(empty)* |
| `FREQTRADE_SHORTS__DRY_RUN` | This bot's own paper/live toggle, independent of every other bot's. | *(empty, treated as `true`)* |
| `FREQTRADE_SHORTS_SECOND_OPINION_ENABLED` | Turns on Shorts' P5 ensemble (shadow mode). | `false` |

## 6. Per-bot: Moonshot (`moonshot` group)

| Variable | Purpose | Default |
|---|---|---|
| `BYBIT_MOONSHOT_API_KEY` / `_SECRET` | Bybit key/secret, small/new listings, spot. Separate sub-account recommended. | *(empty)* |
| `FREQTRADE_MOONSHOT__DRY_RUN` | This bot's own paper/live toggle. | *(empty, treated as `true`)* |
| `FREQTRADE_MOONSHOT_SECOND_OPINION_ENABLED` | Turns on Moonshot's P5 ensemble (shadow mode). Moonshot has no core/satellite tiering, so its ensemble only ever uses the Tier 1 pool. | `false` |

## 6b. `FREQTRADE_UI_HOST` (all Freqtrade bots)

The host name or IP you open the bot UIs (FreqUI, ports 8099 ... 8200) with, e.g. `my-nas.local` or `192.168.1.20`. Each bot's `entrypoint-cors.sh` turns it into the REST API's `CORS_origins` (`http://<host>:<port>` for all 8 bot ports) so a FreqUI session open on one bot can talk to the others. Default `localhost` (then the cross-bot switch only works when FreqUI is opened on localhost). Only letters, digits, `.`, `_`, `-` are accepted; anything else falls back to `localhost`. A change needs a container restart.

## 7. Per-bot decision source and LLM provider (Main, Shorts, Moonshot) -- P23

| Variable | Values | What it does |
|---|---|---|
| `MAIN_DECISION_SOURCE` / `SHORTS_DECISION_SOURCE` / `MOONSHOT_DECISION_SOURCE` | `llm`, `kronos_llm` (**compose default**), `kronos` | `llm`: the LLM decides, as before P23. `kronos_llm`: Kronos proposes a direction from its cross-sectional 24h forecast, the LLM may only confirm or veto it (any other answer is a veto, enforced in code). `kronos`: Kronos alone decides, no LLM call. A missing / older than ~2 h / malformed Kronos signal for a pair = hold. |
| `MAIN_LLM_PROVIDER` / `SHORTS_LLM_PROVIDER` / `MOONSHOT_LLM_PROVIDER` | `deepseek` (**default**), `free`, `none` | `deepseek`: DeepSeek, OpenRouter fallback (as before). `free`: OpenRouter **free models only** (needs `OPENROUTER_API_KEY`; never DeepSeek, never a paid model). `none`: the LLM is never called; with source `llm` the bot is idle (holds everywhere = "left empty"), with `kronos_llm` it behaves as `kronos`. |

**Choosing the decision source (what each value means in practice).**

| Value | Who decides | When to use it |
|---|---|---|
| `kronos_llm` (default) | Kronos proposes buy / sell_short / hold from its hourly cross-sectional forecast; the LLM may only **confirm or veto** | cautious; in a falling or choppy market the LLM vetoes most proposals (measured 2026-10: Main 123 of 123, Moonshot 49 of 49), so the bot trades rarely |
| `kronos` | Kronos alone; the LLM is never called (no DeepSeek cost) | to measure Kronos as a decision source without the veto; the live audit's 24 h edge is still unproven (6 days of data) |
| `llm` | the LLM alone, as before P23 | to return to the pre-P23 behaviour (it had no measurable edge: Main -0.65% per trade over 145 trades) |

How to change it: Portainer -> Stacks -> your stack -> Environment variables -> set e.g. `SHORTS_DECISION_SOURCE=kronos` -> Update the stack (redeploy). One bot at a time is the safest way to compare. The dashboard shows the value in force on each bot's page (Bots tab, Parameters) and the card "Why is it not trading?" shows what the bot decided in the last 24 hours. Both `MAIN_`, `SHORTS_` and `MOONSHOT_` variables are also passed to survival-agent so the page can display them.

Invalid values fall back to the defaults (`llm` / `deepseek`) and log a warning. To go back to exactly the pre-P23 behaviour set `<BOT>_DECISION_SOURCE=llm`. Kronos reaches the bots through `kronos_rank.json` on the read-only allocator volume (see `GET /api/kronos/rank` for what they currently see).

---

## Quick reference: which bot reads what

| | general | main | shorts | moonshot |
|---|---|---|---|---|
| Main | ✅ | ✅ | | |
| Shorts | ✅ | | ✅ | |
| Moonshot | ✅ | | | ✅ |
| survival-agent (dashboard) | ✅ | ✅ | ✅ | ✅ *(display only: for the env-status page)* |

---

## How to check what's actually set

Open `http://<host>:3446` → **Environment variables** section. It reads
`os.getenv()` directly inside the running `survival-agent` container and
shows, per variable: 🔴 not set, 🟡 set but empty, 🟢 set (or, for the five
`*_DRY_RUN` toggles specifically, "default" vs. "live — REAL MONEY"). This
is a live, read-only mirror of what Portainer actually has — nothing is
entered here, and changing a value here does nothing. To change a value, go
to Portainer and redeploy.

## P20 (market context + allocator)

- `MARKET_CONTEXT_MODE` -- `on` (default) / `off`. Hourly public-data snapshot.
- `PORTFOLIO_BRAIN_MODE` -- now `off` / `shadow` (default) / `enforce`. enforce lets the allocator pause and resize the 8 managed bots; refused while any of them has `dry_run=false`.
- `ALLOCATOR_COIN_LIMIT` -- share of equity (default 0.25) above which a coin caps the holders' weights at 0.5.
- `ALLOCATOR_DIR` -- shared volume path (`/allocator`): `context_latest.json`, `weights.json`.
- `ALLOCATOR_POLICY` -- `grow` (default) / `protect`; see the section "Allocator policy: grow or protect" below. `grow`: weights from each bot's own results, no LLM, a bot that earns is raised to 1.5x (2x with evidence `edge`), a bot that loses is stopped and restarted with a 0.5x trial when its control bot made money, protection only scales the book. `protect`: the original LLM + hard-rule policy, where one portfolio-level rule can pause every bot. Also a dashboard switch.
- `RANKER_MODE` -- `off` (default) / `shadow`. The LightGBM ranker's paper forward test (section below). Also a dashboard switch (Portfolio tab -> LightGBM ranker). The first start downloads the Binance history (about 0.5 GB, about 1.5 h).
- `ALLOCATOR_BREAKER` -- `on` (default) / `off`. Grow-policy circuit breaker: see the section below. Also a dashboard switch.
- `ALLOCATOR_BREAKER_STAKES` -- default 1.25: stop a bot when its net loss since the last (re)start is more than this many average stakes (needs at least 5 closed trades). Also a dashboard switch.
- `ALLOCATOR_BREAKER_STREAK` -- default 8: stop a bot after this many losing trades in a row (0 = off; needs at least 5 trades). Also a dashboard switch.
- `FADE_REENTRY_COOLDOWN_H` -- default 24: hours a Listing Fade pair stays blocked after a stop-loss exit (0 = off; trailing-stop exits never block). Also a dashboard switch; a dashboard value reaches the bot within about 10 minutes through `bot_params.json` on the shared volume, and without a fresh file the bot uses this variable.
- `MAIN_DRY_RUN_WALLET` / `SHORTS_DRY_RUN_WALLET` / `MOONSHOT_DRY_RUN_WALLET` / `FADE_DRY_RUN_WALLET` / `TREND_DRY_RUN_WALLET` / `TREND_V2_DRY_RUN_WALLET` / `CONTROL_LONG_DRY_RUN_WALLET` / `CONTROL_SHORT_DRY_RUN_WALLET` -- paper money of each bot (becomes `FREQTRADE__DRY_RUN_WALLET` of that bot). Defaults are the values in each `config.json` (500 / 500 / 100 / 100 / 100 / 500 / 500 / 500). Raising one needs a redeploy; also record the same amount in the capital ledger of that bot (Bots tab), otherwise the brain's reference capital does not match the real money.
- `ALLOCATOR_PROBE` -- `on` (default) / `off`. A bot at weight 0 for more than `ALLOCATOR_PROBE_DAYS` gets weight 0.1 for ~a day; never overrides a safety rule.
- `ALLOCATOR_PROBE_DAYS` -- days at weight 0 before the probe (default 7).
- `ALLOCATOR_EXCLUDED_BOTS` -- comma list of managed bots the allocator leaves alone (full size, never paused).
- All P19/P20 switches above can also be changed in the dashboard (card "Allocator & market context"); a dashboard value overrides the environment.

## P20.4 additions

- `ALLOCATOR_RETIRED_BOTS` -- comma list of bots stopped for good; compose default none. Not polled, no weight, no dry_run check, equity frozen. Also a dashboard switch.
- `ALLOCATOR_RETIRED_FROZEN` -- optional frozen-balance override, e.g. `botname:412.35` (default: last known balance from the brain snapshots).
- Kronos (read by the `kronos-signal` container only): `KRONOS_MODE` (`on` / `off`, default on), `KRONOS_THRESHOLD` (default `0.005`: expected 4h return after the 0.2% round-trip fee needed for buy / sell_short),
  `KRONOS_FEE_ROUND_TRIP` (`0.002`), `KRONOS_MAX_PAIRS` (`45`), `KRONOS_SAMPLES` (`3`), `KRONOS_SKIP_BOTS` (none), `KRONOS_THREADS` (`2`), `KRONOS_CYCLE_BUDGET_S` (`900`), `KRONOS_RANK_MIN_N` (`8`), `KRONOS_RANK_MIN_SCORE` (`0.003`), `KRONOS_RUN_MINUTE` (`2`), `KRONOS_OUT_DIR` (`/kronos`).
  The survival-agent side reads `KRONOS_DIR` (default `/kronos`, the read-only mount of the kronos-data volume).

## P21 Replay (page `/replay` on :3446; read by survival-agent only)

- `REPLAY_MAX_USD` -- hard dollar limit per job (default `10`); the job form can only lower it. A job stops with status `stopped_budget` when the real spend (from the API `usage`) exceeds it.
- `REPLAY_WORKERS` -- maximum parallel LLM calls (default `8`), so a replay cannot exhaust the rate limit of the live allocator.
- `REPLAY_MIN_START` -- earliest start of a replay window (default `2026-08-01`): the model's training cut-off is unknown, so older periods are refused.
- `REPLAY_PRICE_IN_PER_M` / `REPLAY_PRICE_OUT_PER_M` -- DeepSeek prices in USD per million input / output tokens. Not hard-coded: **without them a job cannot start** (the dollar limit could not be enforced) and the estimate shows no dollars.
- `REPLAY_FEE_PCT` (default `0.1`) and `REPLAY_SLIPPAGE_PCT` (default `0.05`) -- per-side fee and slippage used by the simulator.
- `REPLAY_MAX_CALLS` -- hard cap on paid LLM calls per job (default `200000`).
- `REPLAY_LLM_PROVIDER` (`deepseek` default | `openai_compatible`), `REPLAY_LLM_BASE_URL`, `REPLAY_LLM_MODEL`, `REPLAY_LLM_API_KEY` -- a second model for replay smoke tests (e.g. an OpenRouter free model). ONE provider + ONE key; there is no rotation or pooling of keys. DeepSeek stays the page default and the only model the real bots use.
- `REPLAY_LLM_PRICE_IN_PER_M` / `REPLAY_LLM_PRICE_OUT_PER_M` -- prices of the OTHER model (default `0` = free, the dollar limit then does not apply). DeepSeek's `REPLAY_PRICE_*` never apply to it.
- `REPLAY_LLM_RPM` (requests per minute) and `REPLAY_LLM_DAILY_CAP` (calls per UTC day) -- **mandatory** for a non-DeepSeek provider (a run is refused without them). With prices unset or `0` the dollar limit does not apply and these call limits are what protects the key; a 429 is waited out inside them (page status `rate_limited`), a cap hit stops the run and the same run can be started again later (answered prompts come from the cache, whose key includes provider, endpoint and model).
- `REPLAY_DIR` (default `/data/replay`) and `REPLAY_STRATEGIES_DIR` (default `/app/strategies_src`) -- where replay writes (nothing outside it) and where the baked, read-only strategy files live.
- The job uses the same `DEEPSEEK_API_KEY` as the rest of survival-agent.
- All fifteen replay variables above (`REPLAY_MAX_USD`, `REPLAY_WORKERS`, `REPLAY_MIN_START`, `REPLAY_PRICE_IN_PER_M`, `REPLAY_PRICE_OUT_PER_M`, `REPLAY_FEE_PCT`, `REPLAY_SLIPPAGE_PCT`) are passed through by `docker-compose.yml` as `${VAR:-}`: an unset stack variable arrives as an empty string and means "built-in default" (for the two prices: "not set", so a job cannot start).

## Dry-run paper money per bot (P24)

`MAIN_DRY_RUN_WALLET` (500), `SHORTS_DRY_RUN_WALLET` (500), `MOONSHOT_DRY_RUN_WALLET` (100), `FADE_DRY_RUN_WALLET` (100), `TREND_DRY_RUN_WALLET` (100), `TREND_V2_DRY_RUN_WALLET` (500), `CONTROL_LONG_DRY_RUN_WALLET` (500), `CONTROL_SHORT_DRY_RUN_WALLET` (500). Each is passed to its bot as `FREQTRADE__DRY_RUN_WALLET` (the defaults equal the value in the bot's `config.json`). Freqtrade reads it at start, so redeploy or restart the bot after changing it. **Keep it equal to what the capital ledger says for that bot**: the ledger (Bots page, "Record") is accounting only and gives a bot no money; the brain's reference capital and the CPPI floor come from the ledger, the bot's real paper money from this variable. To give a bot more paper money: set the variable, redeploy, then record the same difference in the ledger.

## Import file for Portainer

`.env.example` in the repository root lists **every** variable `docker-compose.yml` reads (98), grouped, with the default the compose file uses and empty values for secrets. In Portainer: Stacks -> your stack -> Environment variables -> *Load variables from .env file*, pick the file, fill in the keys and passwords, update the stack. `backtest/test_p25_env.py` fails if the compose file gains a variable the example does not list, or the defaults differ.

## Allocator switches (P25)

`ALLOCATOR_EXCLUDED_BOTS` (comma list of bots left at full size and never paused; default none), `ALLOCATOR_RETIRED_FROZEN`, `ALLOCATOR_PROBE` (`on` | `off`), `ALLOCATOR_PROBE_DAYS` (7), `ALLOCATOR_COIN_LIMIT` (0.25) and `MARKET_CONTEXT_MODE` (`on` | `off`) were dashboard-only before; they are now also passed by compose. The dashboard switch (Portfolio tab -> Allocator) still overrides the environment. The probe weight itself (0.1) is a constant in `backend/engine/allocator.py`. To stop the allocator scaling stakes at all while everything is dry-run: `PORTFOLIO_BRAIN_MODE=shadow` (weights are computed and shown, the bots ignore them).

## Allocator policy: grow or protect (P25)

`ALLOCATOR_POLICY` = `grow` (**default**) | `protect`. Also a switch on the dashboard (Portfolio tab -> Allocator -> Switches); a dashboard value overrides the variable.

| | `grow` | `protect` |
|---|---|---|
| Weights come from | each bot's own results (code, **no LLM call**) | an LLM proposal, then hard rules |
| A bot that earns | 1.5x stake (2x with evidence `edge`), raised by 0.25 a day | up to 1.0x |
| A bot that loses | stopped (weight 0) at once by the circuit breaker (below), or after 30+ trades with net < 0 over the period and the last 7 days; a 0.5x trial after 3+ days when its control bot (`control_long` for Main / Moonshot / Trend v1, `control_short` for the futures bots) made money over 7 days; confirmed or stopped again after 7 days | cut by the LLM / probe weight 0.1 after 7 days at 0 |
| Portfolio-level protection | scales only: x0.5 below -15%, x0.25 below -25% from the (roster-restated) high-water mark, macro-window cap 0.5, opposite positions / coin concentration cap 0.5. **Never idles the whole book.** | weekly loss -2% / ladder / CPPI floor and budget can set every weight to 0 |
| Results before a configuration change | ignored: changing `<BOT>_DECISION_SOURCE` or `_LLM_PROVIDER` starts a new measurement for that bot (full size until it has 30 trades) | not applicable |

**Does it switch by itself?** Partly. `grow` is used while every managed bot is a verified dry-run. A run whose dry-run check finds a bot that is live (`dry_run=false`) or cannot be verified is computed with `protect` instead (the record shows `policy_note`), because real money is never governed by the grow rules. Between `grow` and `protect` you switch by hand (dashboard or variable); it does not flip on a drawdown, because in dry-run a drawdown costs nothing and the grow policy already scales down. The weights file the bots read carries weights up to 2.0 (a value above 2.0 is ignored by the bots).

Kill switch, `shadow` mode, `ALLOCATOR_EXCLUDED_BOTS` and the counterfactual (which can return enforcement to shadow after 4 weeks if the allocator does not beat weight 1) work the same under both policies.

### Circuit breaker (grow policy, P26)

The 30-trade verdict is too slow for a bot that makes one trade a week (a trend bot lost 10 of 10 trades, -32 USD, and still traded at full size while it was "measuring"). The breaker looks at the closed trades since the bot's last (re)start and stops it at once, with the class `breaker` in the table, when it has at least 5 trades and either its net result is below minus `ALLOCATOR_BREAKER_STAKES` (default 1.25) average stakes, or its last `ALLOCATOR_BREAKER_STREAK` (default 8) trades were all losses. A stopped bot follows the normal path: after 3 days a 0.5x trial when its control bot earned over the last 7 days; the breaker counts only trades after the stop, so a restarted bot starts clean. The control bot follows the side the bot actually trades (a futures bot whose recent trades are mostly long uses `control_long`). Switches: `ALLOCATOR_BREAKER`, `ALLOCATOR_BREAKER_STAKES`, `ALLOCATOR_BREAKER_STREAK`, all also on the dashboard.

### Listing Fade re-entry cooldown (P26)

After a stop-loss exit the bot may not open a NEW trade on the same pair for `FADE_REENTRY_COOLDOWN_H` hours (default 24). Live log, 4 weeks: DNA was shorted 12 times with 3 stops, STONK 42 times with 10 stops; the bot re-shorted a squeezing coin an hour after being stopped out. Exits and open trades are never touched; if the guard itself fails (no trade history available) it lets the entry through. Whether a tighter stop would also help is a separate pre-registered question: the "Listing Fade exit mechanics" study on the Replay page (Studies tab).

### LightGBM ranker, paper forward test (P26-R, `RANKER_MODE`)

The walk-forward study of the LightGBM model (Binance perpetuals, hourly features) passed every check but a 54% drawdown. `RANKER_MODE=shadow` runs it forward on paper: once a day after 00:15 UTC the backend keeps the Binance cache fresh (exchange API for the 120 most liquid contracts, the monthly archive and a new model every 28 days), marks two paper books (A plain as in the study, B volatility-scaled: exposure x min(1, 1.0% / trailing 20-day volatility of A), only scaled down), rebalances every 3 days (long the top 8, short the bottom 8 of the top-40 perpetuals) and scores each earlier ranking against what the coins really did (rank IC). Nothing is ever traded and no bot is touched. The ledger stores gross P&L and turnover; costs (0.055% fee + 0.10% slippage per side, 0.30% as the stress line) are applied when it is read. Verdict, computed by code: `collecting` until 56 days and 18 rebalances; then `forward pass` only if the mean IC is at least 0.02, book B is net positive at both slippages and its drawdown is at most 20% of the sleeve; `failing` if B is negative at 0.10% with no positive IC; else `unproven`. Book C (information, not part of the verdict) is book A traded with limit orders instead of market orders: a limit 0.10% better than the 00:00 open, two hours to fill, then a taker chase. A day after each rebalance the job looks at the real hourly highs and lows of that morning and records which orders would have filled and what the chased ones cost, so the maker-fill study's fill assumption (about 92%) is tested on live data; until a rebalance is evaluated, C pays the taker cost for it. Files: `DATA_DIR/ranker/` (`model.txt`, `state.json`, `ledger.jsonl`, `rankings.jsonl`, `ic.jsonl`, `status.json`). First start: downloads about 0.5 GB (about 1.5 h) and needs about 2 GB RAM while training.
