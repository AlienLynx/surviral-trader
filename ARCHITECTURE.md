# Architecture

This document describes the structural layout of the Survival Agent repository:
what the pieces are, how they are wired together, and where state lives.

For a narrative "what actually happens on each cycle" walkthrough, see
[HOW_IT_WORKS.md](./HOW_IT_WORKS.md). For the hard rules and the history of
fixes, read [AGENTS.md](./AGENTS.md).

> **Status note (26.10.2026).** Sections 1-11 describe the original four-bot v0.1 design and are kept as the design record; they remain accurate for what they cover (the per-bot LLM
> strategies, accounting, credentials, persistence) but not for the bot roster. **Today's stack** is `survival-agent`, eight Freqtrade bots (Main, Shorts, Moonshot, Listing Fade, Trend v1 and v2,
> and two control bots), `kronos-signal` and `kronos-replay`: see `docker-compose.yml`, the README, and sections 12 onward (measurement layer, allocator, replay, studies, the LightGBM ranker). The Dip
> and Survival bots were removed on 07.10.2026. Where this file disagrees with `docker-compose.yml` on bot counts or ports, the compose file wins.

---

## 1. High-level shape

The system is two cooperating layers, packaged as one Docker Compose stack
(one Portainer stack, deployed straight from Git):

```
┌───────────────────────────────────────────────────────────────────────────┐
│  Layer A: survival-agent   (FastAPI, container "survival-agent", :3446)   │
│  Role: goal + accounting + dashboard + credentials status                 │
│  It does NOT place trades by default.                                     │
├───────────────────────────────────────────────────────────────────────────┤
│  Layer B: four LIVE Freqtrade bots (:8099/:8110/:8120/:8130)              │
│  Role: the actual market-facing traders. Each is an independent           │
│  Freqtrade instance with its own config, strategy, trade DB, and port.    │
├───────────────────────────────────────────────────────────────────────────┤
│  Layer C: one ISOLATED Freqtrade bot, the Survival Bot (:8140)            │
│  Role: a controlled A/B prompt experiment, not a fifth trading strategy.  │
│  Dry-run only until its own replay gate passes. Zero shared state with    │
│  Layer B. See §3.4.                                                       │
└───────────────────────────────────────────────────────────────────────────┘
```

- **survival-agent is the accounting and goal layer.**
- **Freqtrade is the trader** — five independent instances, four live and
  one isolated experiment.

Both run in the same Compose stack, so they share Compose's automatic
per-stack network and reach each other by service name
(`http://freqtrade:8080`, etc.) with no extra setup.

---

## 2. Repository layout

```
repo/
├── docker-compose.yml         # the single stack: 1 app service + 5 bot services
├── ARCHITECTURE.md            # this file
├── HOW_IT_WORKS.md            # cycle-by-cycle narrative
├── ENVIRONMENT_VARIABLES.md   # every env var, what it does, who reads it
├── AGENTS.md                  # invariants + full bug/fix history (read before editing)
├── README.md                  # operator-facing setup guide
├── .env.example               # documentation of recognized keys (not used at runtime)
│
├── backend/                   # survival-agent (FastAPI app)
│   ├── app.py                 # HTTP API + background loops + UI serving
│   ├── Dockerfile             # builds the survival-agent image
│   ├── requirements.txt
│   └── engine/
│       ├── loop.py            # accounting cycle (primary) + dormant decision chain
│       ├── freqtrade_client.py# read-only client for each bot's REST API
│       ├── split_calc.py      # pure survival/payout/reinvest math
│       ├── risk_guard.py      # hard risk limits (stdlib only) — dormant engine
│       ├── brain.py           # LLM wrapper — dormant engine
│       ├── signals.py         # Bybit public kline summary — dormant engine
│       └── executor.py        # order placement STUB (dry-run only)
│
├── frontend/
│   ├── index.html             # dashboard (tabs: Overview, Bots, Portfolio, Signals & Kronos, Downloads, Settings)
│   ├── calibration.html       # Training & calibration (/calibration)
│   ├── replay.html            # Replay tests (/replay)
│   └── ui.css, nav.js         # shared look and header
│
├── config/
│   └── finance_config.json    # default thresholds, seeded to data/ on first boot
│
├── freqtrade/                 # Bot 1: Main (spot, long-only)         :8099
│   ├── Dockerfile             # bakes config.json + strategies/ + pairlists/ into the image
│   └── user_data/
│       ├── config.json
│       ├── strategies/
│       │   ├── DeepSeekStrategy.py
│       │   ├── _second_opinion.py    # P5 ensemble second-opinion (shadow mode)
│       │   └── auto_tune.json        # P3 baked satellite-stake multiplier
│       └── pairlists/
│           └── BollingerTrendPairList.py  # P4: excludes sustained-breakdown pairs
├── freqtrade-shorts/          # Bot 2: Shorts (futures, 1x, long+short) :8110
│   ├── Dockerfile
│   └── user_data/
│       ├── config.json
│       ├── strategies/
│       │   ├── DeepSeekShortsStrategy.py
│       │   ├── _second_opinion.py
│       │   └── auto_tune.json
│       └── pairlists/
│           └── FundingRsiPairList.py      # P4: excludes crowded-short/squeeze setups
├── freqtrade-moonshot/        # Bot 3: Moonshot (spot, new listings)   :8120
│   ├── Dockerfile
│   └── user_data/
│       └── strategies/
│           ├── DeepSeekMoonshotStrategy.py
│           └── _second_opinion.py
│   ├── Dockerfile
│   └── user_data/
│       ├── config.json
│       └── strategies/
│           ├── _second_opinion.py
│           └── auto_tune.json
│   ├── Dockerfile             # no pairlists/ -- P4 filters are not part of this experiment
│   └── user_data/
│       ├── config.json
│       └── strategies/
│           └── auto_tune.json
│
└── backtest/                  # offline tools -- see ARCHITECTURE.md §9
    ├── llm_replay.py          # replays REAL LLM calls over REAL history (Main only)
    ├── auto_tune.py           # offline satellite-stake tuner (main/shorts/dip)
    ├── survival_ab_test.py    # Main baseline vs. Survival Bot prompt, replay gate
    ├── second_opinion_ab_test.py  # P5 Phase 2 gate (baseline vs. ensemble vote)
    ├── p4_dry_run_compare.py      # live before/after check for the two P4 filters
    ├── p4_historical_squeeze_check.py  # real-history validation for the P4 filters
    └── test_p4_pairlists.py, test_p5_second_opinion.py  # pure-logic unit tests
```

---

## 3. Components

### 3.1 survival-agent (`backend/app.py`)

A FastAPI service. Responsibilities:

| Responsibility | Where |
|---|---|
| Serve the single-page UI | `GET /` → `frontend/index.html`, `/assets` static mount |
| Read/write goal thresholds | `GET/POST /api/config` ↔ `data/config.json` |
| Report credential status (read-only) | `GET /api/env-status` |
| Record live-trading *intent* | `GET/POST /api/live-trading` |
| Read each bot's live state | `GET /api/freqtrade-summary/all` |
| Serve decisions log to UI | `GET /api/decisions` |
| Capital ledger (deposits/withdrawals) | `GET/POST /api/capital` |
| Daily balance snapshots | `GET /api/balance-history` |
| Accounting cycle | background task → `engine/loop.run_cycle()` |
| Daily balance snapshot task | background task, independent of the engine flag |

Two background tasks start on app startup:

1. `_engine_background_loop()` — every `CYCLE_INTERVAL_SECONDS` (default 4h),
   runs the accounting cycle **only if** `engine_enabled` is `true` in config.
2. `_balance_history_loop()` — hourly, writes one balance snapshot per UTC
   calendar day, pruned to a 180-day retention window. This runs
   independently of `engine_enabled` on purpose.

### 3.2 The engine package (`backend/engine/`)

Split into two chains. Only the first is active by default.

**Active chain (accounting):**

```
app.py  ──►  loop.run_cycle(data_dir, config)
                │
                ├─►  freqtrade_client.get_summary()   # read real bot state
                ├─►  _extract_profit(...)             # defensive field probing
                ├─►  split_calc.compute_split(...)    # pure math
                └─►  _log_decision(...)               # append to decisions.log
```

**Dormant chain (own decision engine)** — `run_cycle_own_engine_experimental()`:

```
signals.get_signal()  ──►  brain.ask()  ──►  risk_guard.check_trade()  ──►  executor.execute()
   (Bybit klines)          (DeepSeek/OR)      (hard limits)               (STUB, dry-run)
```

This chain is preserved for manual testing. It is **not** invoked by the
background loop.

| Module | Role | Notes |
|---|---|---|
| `loop.py` | Cycle orchestration + decision logging | Both chains live here |
| `freqtrade_client.py` | Read-only REST client for the bots | Token cache; `BOTS` dict is the single registry |
| `split_calc.py` | Pure survival / payout / reinvest split | No side effects |
| `risk_guard.py` | Hard, non-LLM risk limits | **stdlib only**, load-bearing |
| `brain.py` | LLM call wrapper | DeepSeek primary, OpenRouter fallback |
| `signals.py` | Price + volatility summary from Bybit | Public endpoint, no key |
| `executor.py` | Order placement | **STUB** — logs only, never flips dry-run default |

### 3.3 The Freqtrade bots (original three LLM bots; the full roster is in the README and section 12)

Each bot is a *fully independent* Freqtrade instance: its own image, config,
strategy, trade database, and port. They are deliberately kept in one Compose
file / one Portainer stack, per operator preference.

| Bot | Compose service | Port | Mode | Direction | Entry source | Exits |
|---|---|---|---|---|---|---|
| Main | `freqtrade` | 8099 | spot | long-only | DeepSeek | `minimal_roi`/`stoploss` + rotation |
| Shorts | `freqtrade-shorts` | 8110 | futures (1x) | long + short | DeepSeek | `minimal_roi`/`stoploss` + rotation |
| Moonshot | `freqtrade-moonshot` | 8120 | spot | long-only | DeepSeek | trailing peak-decline + max hold |
| Listing Fade | `freqtrade-listing-fade` | 8160 | futures (1x) | short-only | rules (price drop from the listing high, EMA20, funding veto) | hard stop -12%, native trailing stop, 14-day max hold |
| Trend v1 / v2 | `freqtrade-trend-ensemble` / `-trend-v2` | 8170 / 8200 | spot / futures | long (v2: long + short) | rules (Donchian trend, BTC regime filter in v2) | trailing stop, donchian exit |
| Controls | `freqtrade-control-long` / `-short` | 8180 / 8190 | spot / futures | long / short | Freqtrade's sample strategy | the yardstick; never managed |

Shared between bots: the DeepSeek/OpenRouter API keys, the P5 second-opinion
provider keys, and the FreqUI login. Deliberately **not** shared: each bot has
its own `BYBIT_*_API_KEY`/`_SECRET`, its own `FREQTRADE_*__DRY_RUN` toggle, and
its own `FREQTRADE_*_SECOND_OPINION_ENABLED` flag, so flipping one live (or
enabling one bot's ensemble) never touches another (Freqtrade's own docs
recommend one exchange sub-account per bot).

Each strategy file is self-contained. Helpers such as `get_market_context()`,
`get_news_headlines()`, `_parse_llm_decision()`, and `_second_opinion.py`
are **duplicated per file/directory** on purpose — the containers do not share
a mounted module. If you change one, check whether the others need the same
fix.

**Per-bot auxiliary modules, all optional/fail-open, none affects
entry/exit mechanics by default:**

| Module | Bots | What it does | Status |
|---|---|---|---|
| `pairlists/FundingRsiPairList.py` | Shorts only | Excludes a pair from new shorts when its perpetual funding rate is deeply negative (crowded-short/squeeze setup); RSI(14) daily is a fallback only when funding itself is unavailable | **Active** — wired into `config.json`'s `pairlists` chain |
| `pairlists/BollingerTrendPairList.py` | Main only | Excludes a pair from new longs when 3+ consecutive 1h closes sit below its own Bollinger lower band (sustained breakdown) | **Active** — wired into `config.json`. **Not** applied to Dip: Dip's whole thesis is buying exactly this kind of dip |
| `auto_tune.py` (offline) + `strategies/auto_tune.json` (baked state) | Main, Shorts | Once-a-day, human-run script nudges the satellite-tier stake multiplier ±0.05 (clamped 0.3–0.7) based on real closed-trade win rates by tier | **Active but never automatic** — the strategy reads whatever `auto_tune.json` last said; nothing inside the running bot writes it |
| `_second_opinion.py` | Main, Shorts, Moonshot | Computes a 3-voice ensemble vote (DeepSeek + up to 2 more) on borderline decisions | **Shadow mode only** — logs the vote, never changes the actual decision, disabled by default on all four (see §8.4) |

### 3.5 Frontend (`frontend/`)

P24: three static pages sharing `ui.css` and `nav.js` (served from `/assets`): the dashboard `/` (tabs, one page per bot, `#bots/<id>`), `/calibration` (live reference, calibration, Kronos, reports and logs) and `/replay` (tests only). New read-only endpoints: `GET /api/downloads` (catalog of every file the system writes, built by `backend/downloads.py`: fixed directories, strict names and extensions, no secrets) with `GET /api/downloads/file/{source}/{name}`, `GET /api/bots/{id}/params` (the running bot's `show_config` cut down, plus the repository facts and decision switches, `backend/bot_info.py`), `GET /api/replay/runs/{id}/log`, `GET /api/replay/kronos/files.zip`. The notes below describe the original dashboard sections, now spread over the tabs. Sections:

- **Thresholds (General, shared)** — the one `data/config.json` plan.
- **Environment variables** — read-only mirror of what the container sees.
- **Trading bots** — per-bot summary cards, each linking to that bot's FreqUI.
- **Capital ledger** and **balance history** views.

---

## 4. Data flow

### 4.1 Live trading (per bot, per 1h candle)

```
                   Freqtrade's normal pairlist chain
                (Volume/Age/Price/Spread/Volatility filters)
                               │
                               ▼
        [Main only] BollingerTrendPairList  /  [Shorts only] FundingRsiPairList
              (P4 -- drops sustained-breakdown / crowded-short pairs)
                               │
                               ▼
Bybit public klines ─┐
CoinGecko /global ───┼─►  strategy.populate_entry_trend()
Google News RSS ─────┤         │
own trade history ───┘         ▼
                        _ask_deepseek()  ──►  DeepSeek  ──►  OpenRouter fallback
                               │
                               ▼
                        _parse_llm_decision()  ──►  "buy" / "sell_short" / "hold"
                               │
                               ├──►  [if enabled] _second_opinion.run_shadow()
                               │     (P5 -- logs an ensemble vote, never overrides this)
                               ▼
                     Freqtrade entry + stake sizing
                       (satellite size read from auto_tune.json, P3)
                               │
                               ▼
                        exchange (Bybit) — dry-run or live
```

Exits run on the same cycle for held pairs: `minimal_roi`/`stoploss` (hard
floor/ceiling) plus each bot's own `custom_exit()` logic (rotation, trailing
peak, or max-hold backstop).

### 4.2 Accounting (survival-agent, every 4h if enabled)

```
Freqtrade REST  ──►  get_summary()  ──►  _extract_profit()  ──►  compute_split()
                                                                      │
                                                                      ▼
                                                             data/decisions.log
```

### 4.3 Dashboard reads (on demand)

```
Browser  ──►  survival-agent API  ──►  freqtrade_client  ──►  bot REST APIs
```

---

## 5. Configuration & credentials

There are two distinct configuration surfaces, and they are deliberately
separated.

### 5.1 Thresholds — owned by this app

`data/config.json` (seeded from `config/finance_config.json`):

```json
{
  "start": 500, "target": 2000, "months": 10,
  "survival_monthly": 60, "ramp_months": 6,
  "take_pct": 30, "risk_pct": 50,
  "engine_enabled": false
}
```

Edited live through the UI (`GET/POST /api/config`).

### 5.2 Credentials — owned by Portainer

Every credential is set in **Portainer → Stacks → survival-agent → Editor →
Environment variables**, then piped into the relevant service(s) via
`${VAR}` substitution in `docker-compose.yml`. `data/.env` is *not* the source
of truth for what the bots use.

| Group | Variables |
|---|---|
| `general` | `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `FREQTRADE_API_USERNAME/PASSWORD/JWT_SECRET`, `SURVIVAL_MONTHLY_USD`/`_RUNWAY_MONTHS`/`_MODE`/`_TARGET_MONTHLY_USD`, and P5's shared provider keys: `XAI_API_KEY`, `NVIDIA_API_KEY`, `MISTRAL_API_KEY`, `LLM7_API_KEY`, `GEMINI_API_KEY` |
| `main` | `BYBIT_API_KEY`, `BYBIT_API_SECRET`, `FREQTRADE__DRY_RUN`, `FREQTRADE_SECOND_OPINION_ENABLED` |
| `shorts` | `BYBIT_SHORTS_API_KEY/_SECRET`, `FREQTRADE_SHORTS__DRY_RUN`, `FREQTRADE_SHORTS_SECOND_OPINION_ENABLED` |
| `moonshot` | `BYBIT_MOONSHOT_API_KEY/_SECRET`, `FREQTRADE_MOONSHOT__DRY_RUN`, `FREQTRADE_MOONSHOT_SECOND_OPINION_ENABLED` |

`GET /api/env-status` reports, read-only, exactly what the container sees.
Full description of every variable: [ENVIRONMENT_VARIABLES.md](./ENVIRONMENT_VARIABLES.md).

---

## 6. State & persistence

Every piece of persistent state uses a **named Docker volume** (not a host
bind-mount). This is deliberate and was hard-won — see AGENTS.md.

| Volume | Used by | Holds |
|---|---|---|
| `survival-agent-data` | survival-agent `/data` | `config.json`, `.env`, `decisions.log`, `capital.json`, `balance_history.json` |
| `freqtrade-db` | Main | trade sqlite DB |
| `freqtrade-shorts-db` | Shorts | trade sqlite DB |
| `freqtrade-moonshot-db` | Moonshot | trade sqlite DB |

Freqtrade's `config.json`, `strategies/`, and (Main/Shorts) `pairlists/` are
**baked into the image** at build time via each `Dockerfile` (`COPY`), not
bind-mounted. Reason: Portainer's git-deployed stacks leave bind-mount folders
empty rather than populating them from the repo, so a bind-mounted strategy
silently disappears. **Any new file a bot needs at startup must go through
its Dockerfile as a `COPY`.**

Freqtrade logs go to stdout (visible via Portainer) — no separate log volume.

---

## 7. Networking

```
                    Compose per-stack network (automatic, no config)
        ┌────────────────────────────────────────────────────────────┐
        │                                                            │
  survival-agent ──────► http://freqtrade:8080                      │
   (:3446 host → 8088)   http://freqtrade-shorts:8080                │
                         http://freqtrade-moonshot:8080              │
        └────────────────────────────────────────────────────────────┘
```

Browser access:

- survival-agent dashboard: `http://<host>:3446`
- FreqUI per bot: `:8099` (main), `:8110` (shorts), `:8120` (moonshot),
  `:8130` (dip), `:8140` (survival, isolated experiment)

Cross-bot FreqUI requires per-bot `api_server.CORS_origins` to list every
bot's exact origin (wildcard `*` does not work for credentialed requests).
The list is built at container start from the `FREQTRADE_UI_HOST` environment variable by each bot's `entrypoint-cors.sh` (the `config.json` files carry an empty list): set it to the host name or IP you open the bot UIs with.

---

## 8. LLM prompt architecture

Six prompts exist. Five are live/experimental strategy system prompts; one
belongs to the dormant engine. Each is exposed as a module-level constant
where practical so tooling (e.g. `backtest/llm_replay.py`) can import the
exact same string instead of keeping a drifting copy.

| Prompt | File | Decisions |
|---|---|---|
| Main | `DeepSeekStrategy.py` → `SYSTEM_PROMPT` | `buy` / `hold` |
| Shorts | `DeepSeekShortsStrategy.py` → `SYSTEM_PROMPT` | `buy` / `sell_short` / `hold` |
| Moonshot | `DeepSeekMoonshotStrategy.py` → `SYSTEM_PROMPT` | `buy` / `hold` |
| Experimental (dormant) | `backend/engine/loop.py` (inline) | `buy` / `sell` / `hold` (JSON) |

For the full text of each prompt and the accompanying user message templates,
see [HOW_IT_WORKS.md](./HOW_IT_WORKS.md).

### 8.1 Second-opinion ensemble (P5, shadow mode)

Each of the four live bots' `_second_opinion.py` can gather up to 2 more
opinions (DeepSeek + 2 = 3 total) from a tiered provider pool, when enabled:

| Tier | Providers | Used when |
|---|---|---|
| 1 | Grok (xAI), NVIDIA NIM, OpenRouter `:free`, Mistral | Any "borderline" decision (not `hold`) |
| 2 | llm7.io, Gemini 2.5 Flash-Lite | Only when the pair is also `core` tier ("critical") |

Voting: 3 votes → 2-of-3 majority, all-distinct → DeepSeek tie-break. 2 votes
(one provider didn't answer) → agree = that decision, disagree = `hold`. 1
vote (both unreachable) → fail-open, DeepSeek unchanged. **Currently this only
ever logs its verdict — it never overrides the strategy's actual decision.**
Each bot has its own `FREQTRADE*_SECOND_OPINION_ENABLED` flag, default
`false`. See AGENTS.md's "P5" entries for the full provider-verification
caveats (several of these providers were never exercised against a live
account).

---

## 9. Offline tooling (`backtest/`)

| Script | What it does |
|---|---|
| `llm_replay.py` | Calls the **real** LLM APIs against **real** historical Bybit prices (Main bot's universe only). Imports the production `SYSTEM_PROMPT` and `_parse_llm_decision` directly so parsing can never drift. A sanity check, not a true replay — deliberately does not reconstruct historical news/market context, subsamples to roughly one decision per pair per day. |
| `auto_tune.py` | P3: offline, human-run (or cron), once/day. Reads real closed-trade win rates by tier from a bot's own trade DB and nudges its `auto_tune.json` satellite-stake multiplier ±0.05 (clamped 0.3–0.7). Never touches entry/exit logic. |
| `second_opinion_ab_test.py` | Same protocol for P5: baseline (DeepSeek only) vs. baseline+ensemble-vote. Built and structurally verified, but not run for real — needs the four new provider keys, none configured yet. |
| `p4_dry_run_compare.py` / `p4_historical_squeeze_check.py` | Live and historical validation for the two P4 pairlist filters, against real Bybit data. |
| `test_p4_pairlists.py` / `test_p5_second_opinion.py` | Pure-logic unit tests (mocked HTTP), no real API keys needed. |

All output goes to `backtest/results/` (gitignored). Real-API scripts need
`backtest/.env.local` with real keys (same gitignored pattern as `data/.env`).

---

## 10. Trust boundaries & hard invariants (summary)

Full detail in AGENTS.md. Structurally, the key ones are:

1. **Credentials never enter git.** `data/` is gitignored. Every `set_key()`
   call must use `quote_mode="never"`.
2. **Risk limits live in code, not the prompt.** `risk_guard.py` is the last
   checkpoint; a prompt change must never be the only thing preventing an
   oversized trade.
3. **`executor.py` defaults to dry-run.** Never change that default in code.
4. **The profit split is config, not a constant.**
5. **One active position at a time** unless the operator raises
   `max_concurrent_positions` (default 1). *(Applies to the dormant engine;
   Freqtrade's own `max_open_trades` governs the live bots.)*
6. **Going live is a deliberate, confirmed action.** `POST /api/live-trading`
   requires `confirm_text == "LIVE"` and Bybit keys — and only records intent.
7. **`env_file` uses the plain string form** (Compose long form is silently
   ignored by this Portainer build).
8. **The Survival Bot has its own, additional live gate.** Both
   `FREQTRADE_SURVIVAL__DRY_RUN=false` AND `SURVIVAL_LIVE_MODE=true` are
   required before it can place a real order — set manually, never by a
   script or timer, and only after `backtest/survival_ab_test.py`'s gate
   passes (currently: it has not).
9. **P4/P5 additions never touch entry/exit mechanics.** Pairlist filters
   only ever remove a pair from the candidate pool for new entries; the
   second-opinion ensemble only ever logs, in shadow mode. Neither has
   touched `stoploss`, `minimal_roi`, `max_open_trades`, `risk_guard.py`, or
   `_parse_llm_decision` — verified via `git diff` before each commit.

---

## 11. Extension points

- **Add a bot:** one service block in `docker-compose.yml`, one `BOTS` entry in
  `freqtrade_client.py`, one port in the frontend's `BOT_PORTS` map, new
  `ENV_VAR_DEFS` entries with a new `group`, and add the new origin to every
  other bot's `CORS_origins` (and theirs to the new one).
- **Add a credential:** add an `ENV_VAR_DEFS` entry in `app.py` and the matching
  `${VAR}` line in `docker-compose.yml`.
- **Add a threshold:** add it to `config/finance_config.json`, the
  `FinanceConfig` model in `app.py`, and a form field in `frontend/index.html`.
- **Add a pairlist filter:** a new class in the bot's own `user_data/pairlists/`
  (subclass `IPairList`, see `FundingRsiPairList.py`/`BollingerTrendPairList.py`
  for the pattern), add its `{"method": "ClassName"}` as the last step in that
  bot's `config.json` `pairlists` array, and add `COPY user_data/pairlists/`
  to its Dockerfile if not already present.
- **Add a second-opinion provider:** a `_call_<provider>()` function in each
  bot's `_second_opinion.py` (same signature/fail-open pattern as the
  existing five), register it in `_PROVIDER_CALLS`, and add it to
  `TIER1_PROVIDERS`/`TIER2_PROVIDERS` depending on its real rate limits —
  check the provider's own docs before assuming a tier, per the
  Mistral-vs-llm7/Gemini precedent in AGENTS.md.


---

## 12. Measurement and control plane (P17 - P20.4)

### 12.1 What was added and why

The first version could *run* LLM-driven bots but could not say whether they worked. This layer exists to answer that with statistics and to keep
portfolio risk in code, never in a prompt.

| Component | File | Runs | Writes (under `DATA_DIR`) | Touches a bot? |
|---|---|---|---|---|
| Portfolio brain | `backend/engine/portfolio_brain.py` | daily 00:10 UTC | `portfolio_brain_latest.json`, `..._snapshots.jsonl`, `..._report_<date>.md`, `portfolio_brain_state.json` | no (GET only) |
| Signal audit | `backend/engine/signal_audit.py`, `evidence.py` | nightly 00:30 UTC | `signals.jsonl`, `signals_forward.jsonl` (append-only), `signals_audit.json`, `signals_candles.json` | no |
| Market context | `backend/engine/market_context.py` | hourly :05 | `context_snapshots.jsonl` (append-only), `context_latest.json`; copy on the shared volume | no |
| Allocator | `backend/engine/allocator.py` | daily 00:40 UTC + on events (hourly check) | `allocations.jsonl` (append-only), `allocator_state.json`, `allocator_latest.json`, `allocator_counterfactual.json`, weights file on the shared volume | **yes**: only pause / resume of the 8 managed bots, only in `enforce` |
| Roster state | `backend/engine/bot_state.py` | inside brain and allocator | `retired_bots.json`, `bot_last_known.json` | no |
| Runtime switches | `backend/engine/runtime_settings.py` | on demand | `runtime_settings.json`, `runtime_settings_log.jsonl` (append-only) | no |
| Export centre | `backend/export.py` | on demand | (streams a ZIP) | no |
| Kronos signal | `kronos-signal/` (own container) | hourly :02 | `/kronos/kronos_signals.jsonl` (append-only) on its own volume | no (GET `/whitelist`, `/pair_candles`) |

### 12.2 Data flow

```
bots' logs (named db volumes, :ro) --> signal_audit --> ledger --> forward returns --> signals_audit.json --> dashboard, ZIP
kronos-signal --(kronos-data volume, :ro)---------------+
Bybit public + calendar + news --> market_context --> context_latest.json --+--> /allocator volume --> the 6 LLM bots' prompts (read-only)
                                                                            +--> allocator input, audit slices by flag
bots' REST (balance, status, trades, show_config) --> brain (daily) / allocator (daily + events)
allocator --> weights.json on /allocator (ro in the bots) --> custom_stake_amount multiplier (enforce only)
allocator --> POST /stopentry (weight 0) / POST /start (+ /reload_config fallback) on the managed bots
```

Shared volumes: `allocator-shared` (rw in survival-agent, `:ro` in the 8 managed bots), `kronos-data` (rw in kronos-signal, `:ro` in survival-agent), and each bot's `*-db`
volume mounted `:ro` into survival-agent for log access. The control bots and `trend_ensemble` mount nothing from P20.

### 12.3 Rules that are code, not convention

- **Dry-run only.** The allocator checks every active managed bot's `dry_run` through its own `/show_config`; `enforce` is refused if any is `false` and the service refuses to start in that case. An unverifiable
  bot keeps the run in shadow, and the recorded reason names that bot.
- **Never managed:** `control_long`, `control_short`, `trend_ensemble`, `kronos` (`NEVER_MANAGED`; `BotControl` raises before any request). Retired bots (`ALLOCATOR_RETIRED_BOTS`) are not polled at all.
- **Hard-rule order** in `allocator.apply_hard_rules`: +-25 pp per day on the LLM's proposal; then absolute safety caps (drawdown ladder, weekly/monthly loss, CPPI proportional scaling and the 1.5x / floor stops,
  macro-event cap 0.5, opposite positions, coin concentration); then `< 0.05 -> 0`. On incomplete data (a never-seen bot) the equity-based rules only warn.
- **Roster maths** (`bot_state.py`): retired bot = equity frozen as a constant; silent bot <= 24h = `stale` (last equity and exposure used); > 24h = `unreachable` (last equity kept, exposure unknown); only a never-seen
  bot makes a run `partial`.
- **Append-only everywhere it matters:** ledger, forward returns, context snapshots, allocations, settings log, kronos signals. Tests compare earlier bytes after a second run.
- **No look-ahead:** forward return uses only candles after the decision; decision price is the close of the candle opened at t0 - 1h; context flags come from the last snapshot at or before the decision (<= 3h old);
  the counterfactual weight of a trade is the last allocator decision at or before its open; kronos sees only candles closed by t0.

### 12.4 HTTP surface added to `survival-agent` (:3446)

`GET /api/signals/audit`, `GET /api/context/latest`, `GET /api/portfolio/brain`, `GET /api/portfolio/allocator`, `POST /api/portfolio/allocator/{run,kill,resume}`,
`GET|POST /api/settings/p20`, and the export centre `GET /api/export/{catalog,logs,trades,bundle}`. Only the allocator's `kill` / `resume` / `run` and the settings endpoint change state; all of them are LAN-only
like the rest of :3446 (there is no authentication on the dashboard).

### 12.5 Schedule (UTC)

| Time | Job |
|---|---|
| :02 every hour | kronos-signal cycle (budget 9 min) |
| :05 every hour | market-context snapshot, then the allocator's event check (ladder rung, macro event within 24h) and the re-pause reconcile |
| 00:10 | portfolio brain |
| 00:30 | signal audit (ingests kronos signals, resolves forward returns) |
| 00:40 | allocator daily run |

### 12.6 Extension points

A new signal source = append ledger-format records to a file the audit ingests (see `ingest_kronos`) and add the name to `AUDITED_BOTS`. A new managed bot = add it to
`allocator.MANAGED_BOTS` and `runtime_settings.MANAGED`, give its strategy the `_allocator_ctx` helper (identical copy per bot directory), mount `/allocator` read-only. A new data source for the context = a field with
`value / ts / source / status` in `market_context.build_snapshot`; never default a missing value to a neutral number.

---

## 13. Replay (P21)

`backend/replay/` is a self-contained package that re-runs the six LLM bots' strategy files over cached history. It is a thread inside `survival-agent` (no Docker socket), driven by `/api/replay/*` and the page `/replay`.

| Module | Role |
|---|---|
| `paths.py` | the only way to write: `REPLAY_DIR` sandbox, live file names refused |
| `data.py` | Bybit klines (cached), Fear & Greed history, `SeriesStore.asof()` -- the single look-ahead guard |
| `sandbox.py` | loads a REAL strategy file with swapped globals (requests, datetime/time, Trade, context/news/F&G, helpers) and a fake data provider; anonymiser |
| `llm.py` | the live DeepSeek request + disk cache + bounded parallelism + retries + cost from `usage` + USD/call limits |
| `engine.py` | per bot and mode: decisions at the decision steps, entries/exits with the bot's own constants, equity curve |
| `analysis.py` | statistics, baselines (BTC hold, random entries), the P19 audit on replay decisions, leak flag, divergence from live |
| `runner.py` | parameter validation, estimate, the single background job, status, stop, runs, ZIP |

Data flow: Bybit public (free) -> `/data/replay/cache` -> `SeriesStore` -> strategy sandbox -> replay LLM client -> `/data/replay/<run_id>/`. Nothing flows into `/allocator`, the ledger, the bots' volumes or a bot's REST. Schedule: none -- a job runs only when started from the page.

Reading list for correctness: `backtest/test_p21_replay.py` (isolation by hash and mtime, no-peeking at prompt level, determinism, simulator edge cases, limits). Limits of what a replay proves are printed on the page and in every report.

### 13.1 Live snapshot and calibration (P21.1)

| Module | Role |
|---|---|
| `live_data.py` | parses the bots' `DeepSeek said ...` log lines, restarts, `max_open_trades`, outage hours (LLM calls that failed are logged as `hold`) and the live trades into one snapshot; coverage report |
| `calibration.py` | feeds the simulator the live bots' REAL decisions (no LLM, $0), carries over positions open at the window start, matches live and replay trades 1:1, measures each mechanics fix alone and together |

The button "Update live reference" (`POST /api/replay/live-reference/refresh`, in `app.py`, outside the package) reads trades and logs once with the P18 export's own code and writes `live_reference.json` and `live_snapshot.json` into `REPLAY_DIR`. The replay and calibration read only those files. Calibration run type: `POST /api/replay/calibration/start`, results under `REPLAY_DIR/cal-<time>/`. One-minute candles are used only to price an entry/exit at its exact moment and are never visible to a strategy.

### 13.2 Exit variants, no-LLM rule, providers (P21.1 C-G)

`runner.py` has two more job types besides the replay and the calibration: **exit variants** (`frozen_from` + `variants`, run id `fz-...`) re-run the recorded decisions of an earlier run with different exit rules (`engine.BotRun.apply_overrides`: rotation, minimum hold, ROI, stoploss, trailing) and compare them; a normal run also gets a **rule** mode (the strategy's own placeholder rule via `dp.runmode = backtest`). `llm.py` has `Provider` (deepseek | openai_compatible), a sliding-window `RateLimiter`, a persisted daily call cap and a cache key that includes provider, endpoint and model. `engine.py` carries the simulator mechanics switches (`MECH_REPLAY_DEFAULT` for normal runs, `MECH_LIVE_LIKE` + log-measured latency for calibration).

### 13.3 Listing Fade study (P22-B)

`listing_fade.py` replays the live rule-based `listing_fade` strategy (its own entry code, constants and config read from the files, hashes printed) over every Bybit USDT perpetual listed since 2024-01-01 including delisted ones (`status=Closed`), with taker fees, funding, 5-minute stop gaps and slippage scenarios, and computes a verdict (`edge candidate` / `unproven` / `no edge`) from fixed thresholds. Raw data: `/data/replay/listing_fade/` (instruments, hourly, funding, 5-minute; resumable). Endpoints: `GET /api/replay/lf/state`, `POST /api/replay/lf/fetch`, `POST /api/replay/lf/study`.

### 13.4 Kronos as a measured signal (P22-K)

Three pieces. (1) `kronos-replay/` (new container, CPU only, nice 10, idle until a job exists, weights baked in at the pinned revisions) forecasts hourly as-of times of a window and writes `signals_<shard>.jsonl` + `progress_<shard>.json` into its own volume, which survival-agent mounts at `/data/replay/kronos`. The look-ahead rule lives in one function (`gen.window_for`: candles with open time <= t0 - 1h only, newest exactly t0 - 1h, no gap) and the candle files handed to the worker stop before the window end. (2) `backend/replay/kronos_replay.py` prepares the job (pair set, candles, `job.json`), estimates it (and warns above 12 h), requests the K1 benchmark, sets the STOP flag, reads progress. (3) `backend/replay/kronos_eval.py` evaluates: signal quality independent of exits (cluster bootstrap by day, two halves, computed verdict) and the replay engine with the signals as the decisions of `main` / `shorts`, next to the strategy's own rule, random entries, BTC hold. Endpoints `/api/replay/kronos/*`, card on `/replay`. Nothing here writes to the live `/kronos` volume or to a bot, and the replay never assigns `edge`.

## P23 -- decision source, LLM provider, and the Kronos rank feed (07.10.2026)

- **Removed:** the Dip and the Survival bots (`survival`, `survival_shorts`, and the replay-only `survival_shorts_v2`) are gone from the repository, the compose file, the allocator roster (5 managed bots: main, shorts, moonshot, listing_fade, trend_v2), the export centre (8 bot volumes), the scorecard, the replay engine and the dashboard.
- **Decision source:** Main, Shorts and Moonshot read `<BOT>_DECISION_SOURCE` (`llm` | `kronos_llm` | `kronos`) and `<BOT>_LLM_PROVIDER` (`deepseek` | `free` | `none`) through `_allocator_ctx.decision_config`. The old `_ask_deepseek` body became `_ask_llm`; the new `_ask_deepseek` is a thin wrapper that applies the source (Kronos proposes, the LLM may only confirm or veto, code enforces it) and the provider (`free` = OpenRouter free models only). Exits, stoploss, ROI and leverage never reference Kronos (tested).
- **Feed:** `kronos-signal` writes `kronos_rank.json` (per bot: cross-sectional relative strength of the 24h forecast, top third buy / bottom third sell_short, minimum 8 scored pairs and 0.3% from the bot's mean) next to its ledger file; `survival-agent` (`backend/engine/kronos_feed.py`, a 2-minute loop) validates it and copies it to the allocator volume that the bots already mount read-only. The bots never mount the Kronos volume. A missing / older than ~2 h / malformed signal is hold.
- **Why this design:** the Kronos docs demo ranks many instruments by forecast and goes long the top-K, and warns that raw signals need market neutralisation; the old absolute-threshold signal measured `no edge`. The rank design was fixed in advance and checked once on the 15 historical days: 24h IC +0.10 [+0.02, +0.18] (second half +0.20, first half +0.04 not significant), top-third minus bottom-third 24h spread +0.02% [-0.56, +0.67]: no demonstrated profit. It is wired in so that it is measured live in dry-run, not because it was shown to work.

## P25: announcement guard and funding carry study

`backend/engine/announcements.py` reads Bybit's public delisting notices every 10 minutes (loop in `app.py`), appends them to `DATA_DIR/announcements.jsonl` and writes `blocked_pairs.json` into `ALLOCATOR_DIR`; the managed bots read it through `_allocator_ctx.entry_block` in `confirm_trade_entry` (new entries only, fail-open). `backend/replay/carry.py` is a self-contained study (parallel, cached fetch under `REPLAY_DIR/carry/`, daily simulation, pre-registered verdict) started through `ReplayManager.start_carry_study`; it never writes outside `REPLAY_DIR`. P26: `backend/engine/bot_params.py` publishes `bot_params.json` (today the Listing Fade re-entry cooldown, from the dashboard switch or `FADE_REENTRY_COOLDOWN_H`) to `ALLOCATOR_DIR` in the same 10-minute loop and at once when the switch is saved; the bot reads it through `_allocator_ctx.bot_param` (stale > 6 h or malformed = its own environment default) and `ListingFadeStrategy.confirm_trade_entry` refuses a new entry on a pair stopped out less than that many hours ago (`reentry_blocked`, fail-open). The grow policy's circuit breaker is `allocator.breaker_reason` (checked in `grow_bot`, state `breaker_since` in `allocator_state.json`); `allocator.control_name` picks the control bot by the side a futures bot actually trades. `backend/replay/fade_exit_study.py` replays the live Listing Fade rules on the cached Listing Fade history with eight exit variants (`listing_fade.run_scenario(cooldown_h=...)`) and one pre-registered decision. P26-R: `backend/engine/ranker.py` is the paper forward test of the LightGBM ranker. A 30-minute loop in `app.py` (`_ranker_loop`, only with `RANKER_MODE=shadow`) runs `ranker.run_once` once a day after 00:15 UTC: it keeps the Binance cache of `lgbm_hourly` fresh (exchange API tail for the 120 most liquid cached contracts, `lgbm_hourly.fetch_all` and a retrain every 28 days), rebuilds the last weeks with the study's own matrices and `feature_day`, scores the top-40 universe with the saved model (`DATA_DIR/ranker/model.txt`), and keeps two paper books (A plain, B scaled by `vol_scale`) in `ledger.jsonl` as gross P&L and turnover, so that costs are applied when the ledger is read. `ranker.snapshot` feeds `/api/ranker` and the Portfolio-tab section; `forward_verdict` is the only place a verdict is made. Book C (`passive_outcome`, `evaluate_passive`) prices the same positions with limit orders from the real hourly bars of the rebalance morning, one day after the rebalance. The module imports nothing that trades or talks to a bot.
