"""
Survival Agent backend.

Serves the settings UI and small APIs:
  - /api/config       thresholds (start, target, survival cost, split rules)
  - /api/credentials  API keys, written to data/.env, never into config.json
  - /api/decisions    recent engine decisions, for the UI to display

Everything under DATA_DIR is the only persistent, non-git state.
See AGENTS.md before changing invariants around credential handling.
"""
import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values, set_key, load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .export import router as export_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("survival-agent")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
ENV_PATH = DATA_DIR / ".env"
DECISIONS_PATH = DATA_DIR / "decisions.log"
CAPITAL_PATH = DATA_DIR / "capital.json"
BALANCE_HISTORY_PATH = DATA_DIR / "balance_history.json"
SCORECARD_PATH = DATA_DIR / "scorecard.json"
DEFAULT_CONFIG_PATH = Path("/app/config_defaults/finance_config.json")

CYCLE_INTERVAL_SECONDS = int(os.getenv("CYCLE_INTERVAL_SECONDS", str(4 * 3600)))
# One snapshot per calendar day (UTC) is enough for a daily balance history --
# the check loop runs hourly (see _balance_history_loop) but writes are a
# no-op once today's date is already recorded, so this is cheap and safe to
# poll more often than it actually needs to write.
BALANCE_HISTORY_CHECK_SECONDS = 3600
BALANCE_HISTORY_RETENTION_DAYS = 180  # ~6 months, per operator request -- don't keep this forever

if not CONFIG_PATH.exists():
    shutil.copy(DEFAULT_CONFIG_PATH, CONFIG_PATH)
    log.info("No config.json found, seeded from defaults")

if not ENV_PATH.exists():
    ENV_PATH.touch()
    log.info("No .env found, created empty one at %s", ENV_PATH)

load_dotenv(ENV_PATH)

# Every credential/config var any of the bots (or this app)
# read from Portainer's stack Environment Variables -- see
# docker-compose.yml, where each is piped into whichever
# service(s) actually need it. This app no longer writes any of
# these anywhere; Portainer is the single place to set them, and
# GET /api/env-status below just reports what this container
# actually sees, live, via os.getenv(). Adding a new bot or a new
# credential means adding one entry here (and the matching env: line
# in docker-compose.yml) -- see AGENTS.md.
ENV_VAR_DEFS = [
    {"key": "DEEPSEEK_API_KEY", "desc": "Primary decision brain for all three LLM bots.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "OPENROUTER_API_KEY", "desc": "Fallback brain for all three LLM bots when DeepSeek is unavailable.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "FREQTRADE_API_USERNAME", "desc": "Shared FreqUI login across all three LLM bots.", "used_by": "main, shorts, moonshot", "group": "general", "secret": False},
    {"key": "FREQTRADE_API_PASSWORD", "desc": "Shared FreqUI login password.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "FREQTRADE_API_JWT_SECRET", "desc": "Signs Freqtrade's own API session tokens -- not from any external service. Any long (32+ char) random string.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "BYBIT_API_KEY", "desc": "Bybit key, spot, long-only. Dedicated sub-account recommended, no withdraw permission.", "used_by": "main", "group": "main", "secret": True},
    {"key": "BYBIT_API_SECRET", "desc": "Secret for the key above.", "used_by": "main", "group": "main", "secret": True},
    {"key": "FREQTRADE__DRY_RUN", "desc": "Paper/live toggle. Empty or 'true' = safe paper trading (Freqtrade's own default). 'false' = real orders.", "used_by": "main", "group": "main", "secret": False, "is_dry_run": True},
    {"key": "BYBIT_SHORTS_API_KEY", "desc": "Bybit key, futures, 1x leverage. Separate sub-account from the other bots.", "used_by": "shorts", "group": "shorts", "secret": True},
    {"key": "BYBIT_SHORTS_API_SECRET", "desc": "Secret for the key above.", "used_by": "shorts", "group": "shorts", "secret": True},
    {"key": "FREQTRADE_SHORTS__DRY_RUN", "desc": "This bot's own paper/live toggle, fully independent from the others.", "used_by": "shorts", "group": "shorts", "secret": False, "is_dry_run": True},
    {"key": "BYBIT_MOONSHOT_API_KEY", "desc": "Bybit key, small/new listings, spot. Separate sub-account recommended.", "used_by": "moonshot", "group": "moonshot", "secret": True},
    {"key": "BYBIT_MOONSHOT_API_SECRET", "desc": "Secret for the key above.", "used_by": "moonshot", "group": "moonshot", "secret": True},
    {"key": "FREQTRADE_MOONSHOT__DRY_RUN", "desc": "This bot's own paper/live toggle, fully independent from the others.", "used_by": "moonshot", "group": "moonshot", "secret": False, "is_dry_run": True},
    {"key": "SURVIVAL_MONTHLY_USD", "desc": "Monthly operating bill (LLM APIs + infra) the bots must earn back. Fed into every LLM prompt.", "used_by": "main, shorts, moonshot", "group": "general", "secret": False},
    {"key": "SURVIVAL_RUNWAY_MONTHS", "desc": "Months of zero-withdrawal runway left. Fed into every LLM prompt.", "used_by": "main, shorts, moonshot", "group": "general", "secret": False},
    {"key": "SURVIVAL_MODE", "desc": "'grow' = zero withdrawals, compound everything. 'extract' = take payouts per the split.", "used_by": "main, shorts, moonshot", "group": "general", "secret": False},
    {"key": "SURVIVAL_TARGET_MONTHLY_USD", "desc": "Monthly payout the operator needs once runway ends. Fed into every LLM prompt.", "used_by": "main, shorts, moonshot", "group": "general", "secret": False},
    {"key": "BYBIT_FADE_API_KEY", "desc": "Bybit key, isolated Listing Fade Bot experiment (rule-based, no LLM). Separate sub-account recommended.", "used_by": "listing_fade", "group": "listing_fade", "secret": True},
    {"key": "BYBIT_FADE_API_SECRET", "desc": "Secret for the key above.", "used_by": "listing_fade", "group": "listing_fade", "secret": True},
    {"key": "FREQTRADE_LISTING_FADE__DRY_RUN", "desc": "This bot's own paper/live toggle, fully independent from the others.", "used_by": "listing_fade", "group": "listing_fade", "secret": False, "is_dry_run": True},
    {"key": "FADE_LIVE_MODE", "desc": "Second, independent live gate for the Listing Fade Bot ONLY -- both this AND FREQTRADE_LISTING_FADE__DRY_RUN=false are required before it can place a real order. Manual only.", "used_by": "listing_fade", "group": "listing_fade", "secret": False},
    {"key": "BYBIT_TREND_API_KEY", "desc": "Bybit key, isolated Trend Ensemble Bot experiment (rule-based, no LLM). Separate sub-account recommended.", "used_by": "trend_ensemble", "group": "trend_ensemble", "secret": True},
    {"key": "BYBIT_TREND_API_SECRET", "desc": "Secret for the key above.", "used_by": "trend_ensemble", "group": "trend_ensemble", "secret": True},
    {"key": "FREQTRADE_TREND__DRY_RUN", "desc": "This bot's own paper/live toggle, fully independent from the others.", "used_by": "trend_ensemble", "group": "trend_ensemble", "secret": False, "is_dry_run": True},
    {"key": "TREND_LIVE_MODE", "desc": "Second, independent live gate for the Trend Ensemble Bot ONLY -- both this AND FREQTRADE_TREND__DRY_RUN=false are required before it can place a real order. Manual only.", "used_by": "trend_ensemble", "group": "trend_ensemble", "secret": False},
    {"key": "BYBIT_TREND_V2_API_KEY", "desc": "Bybit key, isolated Trend Ensemble v2 Bot (rule-based, no LLM, long+short futures). Separate sub-account recommended.", "used_by": "trend_v2", "group": "trend_v2", "secret": True},
    {"key": "BYBIT_TREND_V2_API_SECRET", "desc": "Secret for the key above.", "used_by": "trend_v2", "group": "trend_v2", "secret": True},
    {"key": "FREQTRADE_TREND_V2__DRY_RUN", "desc": "This bot's own paper/live toggle, fully independent from the others.", "used_by": "trend_v2", "group": "trend_v2", "secret": False, "is_dry_run": True},
    {"key": "TREND_V2_LIVE_MODE", "desc": "Second, independent live gate for the Trend Ensemble v2 Bot ONLY -- both this AND FREQTRADE_TREND_V2__DRY_RUN=false are required before it can place a real order. Manual only.", "used_by": "trend_v2", "group": "trend_v2", "secret": False},
    {"key": "SIGNAL_AUDIT_MODE", "desc": "P19 signal audit: 'on' (default) parses the LLM bots' decisions nightly at 00:30 UTC and scores their forward returns; 'off' disables it. Report only -- it never touches a bot.", "used_by": "signal_audit", "group": "portfolio_brain", "secret": False},
    {"key": "PORTFOLIO_BRAIN_MODE", "desc": "Brain + P20 allocator: 'shadow' (default) computes and logs, never touches any bot; 'off' disables; 'enforce' lets the allocator pause/resize the 8 managed bots (refused while any bot has dry_run=false; kill switch in the UI). The brain itself stays read-only.", "used_by": "portfolio_brain, allocator", "group": "portfolio_brain", "secret": False},
    {"key": "MARKET_CONTEXT_MODE", "desc": "P20-A hourly market-context collector (Bybit derivatives, macro calendar, news counts, sentiment): 'on' (default) or 'off'.", "used_by": "market_context", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_COIN_LIMIT", "desc": "P20-B: a coin whose open notional is >= this share of equity (default 0.25) caps the weight of the bots holding it at 0.5.", "used_by": "allocator", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_PROBE", "desc": "P20.1: 'on' (default) gives a bot that sat at weight 0 for more than ALLOCATOR_PROBE_DAYS a probe weight of 0.1 for about a day, so real trades and statistics keep coming. Never overrides a safety rule. 'off' disables it.", "used_by": "allocator", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_POLICY", "desc": "`grow` (default): weights from each bot's own results -- a bot that earns (>= 30 trades, net > 0 now and over 7 days) is raised to 1.5x (2x with evidence `edge`), a bot that loses is stopped and gets a 0.5x trial when its control bot made money over the last 7 days; portfolio protection only scales (x0.5 below -15%, x0.25 below -25% from the high-water mark), never idles the book; no LLM call. `protect`: the original rules where weekly loss / ladder / CPPI can pause every bot. Forced to `protect` for a run that finds a live (or unverifiable) bot. Also a dashboard switch.", "used_by": "allocator", "group": "portfolio_brain", "secret": False},
    {"key": "RANKER_MODE", "desc": "`off` (default) / `shadow`. LightGBM ranker forward test (paper only, it never places an order or touches a bot): once a day after 00:15 UTC it ranks the top-40 Binance perpetuals with the walk-forward study's model, keeps two paper books (plain and volatility-scaled) and scores itself with a pre-registered forward verdict. The first start downloads the Binance history (about 0.5 GB, about 1.5 h) and trains the model (about 2 GB RAM while it runs). Also a dashboard switch (Portfolio tab -> Ranker)."},
    {"key": "ALLOCATOR_BREAKER", "desc": "`on` (default): grow-policy circuit breaker. A bot whose trades since its last (re)start lost more than ALLOCATOR_BREAKER_STAKES average stakes, or whose last ALLOCATOR_BREAKER_STREAK trades were all losses, is stopped at once (needs at least 5 trades) instead of waiting for 30 trades; it can restart through the normal control-bot trial. `off` disables it. Also a dashboard switch."},
    {"key": "ALLOCATOR_BREAKER_STAKES", "desc": "Breaker threshold in average stakes (default 1.25): net loss since the last (re)start of more than this many average stakes stops the bot. Also a dashboard switch."},
    {"key": "ALLOCATOR_BREAKER_STREAK", "desc": "Breaker threshold in trades (default 8): this many losing trades in a row stops the bot; 0 turns this half off. Also a dashboard switch."},
    {"key": "FADE_REENTRY_COOLDOWN_H", "desc": "Listing Fade re-entry guard (default 24): hours a pair stays blocked after a stop-loss exit (0 = off). Trailing-stop exits never block. A dashboard value reaches the bot within about 10 minutes through bot_params.json; without a fresh file the bot uses this variable."},
    {"key": "ALLOCATOR_RETIRED_BOTS", "desc": "P20.4: comma list of bots stopped for good (default in compose: none). A retired bot is not polled, has no weight or dry_run check, is never 'missing'; its equity is frozen as a constant. Settable in the dashboard.", "used_by": "allocator, portfolio_brain", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_RETIRED_FROZEN", "desc": "P20.4: optional frozen-balance override for retired bots, e.g. botname:412.35 (default: last known balance from the brain snapshots).", "used_by": "allocator, portfolio_brain", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_EXCLUDED_BOTS", "desc": "P20.2: comma list of managed bots the allocator leaves alone (full size, never paused). Settable in the dashboard too.", "used_by": "allocator", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_PROBE_DAYS", "desc": "P20.1: days at weight 0 before the probe (default 7).", "used_by": "allocator", "group": "portfolio_brain", "secret": False},
    {"key": "ALLOCATOR_DIR", "desc": "P20: shared volume (rw here, ro in the managed bots) carrying context_latest.json and weights.json.", "used_by": "allocator, market_context", "group": "portfolio_brain", "secret": False},
    {"key": "PORTFOLIO_FLOOR_PCT", "desc": "Capital floor as a fraction of the reference capital (default 0.90).", "used_by": "portfolio_brain", "group": "portfolio_brain", "secret": False},
    {"key": "CPPI_MULTIPLIER", "desc": "CPPI multiplier: risky-exposure budget = multiplier x (equity - floor). Default 3; the log also shows 5.", "used_by": "portfolio_brain", "group": "portfolio_brain", "secret": False},
    {"key": "TARGET_MONTHLY_PCT", "desc": "Planned monthly return in % (default 1.0). Reaching it logs 'protect gains'; missing it triggers nothing.", "used_by": "portfolio_brain", "group": "portfolio_brain", "secret": False},
    {"key": "TARGET_WEEKLY_PCT", "desc": "Planned weekly return in % (default 0.25). Same behaviour as the monthly target.", "used_by": "portfolio_brain", "group": "portfolio_brain", "secret": False},
    # P5 ensemble second-opinion (Phase 1, shadow mode only -- see AGENTS.md
    # and each bot's _second_opinion.py). Provider keys are shared by all
    # four live bots' ensembles; OPENROUTER_API_KEY already exists above
    # and is reused, not duplicated.
    {"key": "XAI_API_KEY", "desc": "Grok (xAI) -- Tier 1 second-opinion provider, all three LLM bots.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "MISTRAL_API_KEY", "desc": "Mistral -- Tier 1 second-opinion provider, all three LLM bots. Free tier (1 req/s, 500k tok/min, 1B tok/mo) is more generous than llm7/Gemini, hence Tier 1 not Tier 2. Self-throttled to 1 call/6s per bot after a real prod 429.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "KILO_API_KEY", "desc": "Kilo -- Tier 1 second-opinion provider, all three LLM bots. Auto-routes across a pre-configured model pool on the operator's Kilo account. Unverified against a live account.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "AION_API_KEY", "desc": "Aion Labs -- Tier 1 second-opinion provider, all three LLM bots. Unverified against a live account.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "AGNES_API_KEY", "desc": "Agnes AI -- Tier 2 second-opinion provider, all three LLM bots. CODE-DISABLED (AGNES_ENABLED=False in _second_opinion.py) until the operator confirms agnes-2.5-flash is genuinely free on their plan -- setting this key alone does not activate it.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "NVIDIA_API_KEY", "desc": "NVIDIA NIM -- Tier 1 second-opinion provider, all three LLM bots.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "LLM7_API_KEY", "desc": "llm7.io -- Tier 2 second-opinion provider (core-tier/critical decisions only). 40 req/min, 1M tok/24h.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "GEMINI_API_KEY", "desc": "Gemini 2.5 Flash-Lite -- Tier 2 second-opinion provider (core-tier/critical decisions only). Free-tier limits (15 RPM/1000 RPD) are from a third-party tracker -- verify at aistudio.google.com/rate-limits before relying on them.", "used_by": "main, shorts, moonshot", "group": "general", "secret": True},
    {"key": "FREQTRADE_SECOND_OPINION_ENABLED", "desc": "Main bot's P5 ensemble second-opinion feature flag. Phase 1 = shadow mode only (logs the ensemble vote, never overrides DeepSeek's own decision) until Phase 2's replay gate passes -- see backtest/second_opinion_ab_test.py and AGENTS.md.", "used_by": "main", "group": "main", "secret": False},
    {"key": "FREQTRADE_SHORTS_SECOND_OPINION_ENABLED", "desc": "Shorts bot's P5 ensemble second-opinion feature flag. Same shadow-mode-only status as Main's.", "used_by": "shorts", "group": "shorts", "secret": False},
    {"key": "FREQTRADE_MOONSHOT_SECOND_OPINION_ENABLED", "desc": "Moonshot bot's P5 ensemble second-opinion feature flag. Same shadow-mode-only status as Main's. Moonshot has no core/satellite tiering, so its ensemble only ever uses the Tier 1 pool.", "used_by": "moonshot", "group": "moonshot", "secret": False},
]

app = FastAPI(title="Survival Agent")


class FinanceConfig(BaseModel):
    start: float
    target: float
    months: int
    survival_monthly: float
    ramp_months: int
    take_pct: float
    risk_pct: float
    engine_enabled: bool = False


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "•" * (len(value) - 8) + value[-4:]


@app.get("/api/config")
def get_config():
    data = json.loads(CONFIG_PATH.read_text())
    data.setdefault("engine_enabled", False)
    return data


@app.post("/api/config")
def save_config(cfg: FinanceConfig):
    CONFIG_PATH.write_text(json.dumps(cfg.model_dump(), indent=2))
    log.info("Config updated: start=%s target=%s months=%s", cfg.start, cfg.target, cfg.months)
    return {"ok": True}


class LiveTradingRequest(BaseModel):
    enable: bool
    confirm_text: Optional[str] = None


@app.get("/api/live-trading")
def get_live_trading():
    # NOTE: this reads survival-agent's own record of the intended
    # mode, written below. It is NOT necessarily what Freqtrade's own
    # container is actually running with -- Freqtrade's env comes
    # from Portainer's own stack-level environment variables (see
    # docker-compose.yml comments and AGENTS.md), which this app
    # cannot read or write. This value is a statement of *intent*,
    # not a live-verified fact. Cross-check against Freqtrade's own
    # dashboard (:8099) if in doubt.
    values = dotenv_values(ENV_PATH)
    # Safe default: if unset, treat as paper mode.
    dry_run = values.get("FREQTRADE__DRY_RUN", "true").lower() != "false"
    return {"live": not dry_run}


@app.post("/api/live-trading")
def set_live_trading(req: LiveTradingRequest):
    if req.enable:
        if req.confirm_text != "LIVE":
            raise HTTPException(400, "Type LIVE exactly to confirm switching to real money.")
        if not os.getenv("BYBIT_API_KEY") or not os.getenv("BYBIT_API_SECRET"):
            raise HTTPException(400, "Set BYBIT_API_KEY and BYBIT_API_SECRET in Portainer's stack Environment Variables first -- Freqtrade can't place real orders without them.")
        set_key(str(ENV_PATH), "FREQTRADE__DRY_RUN", "false", quote_mode="never")
        os.environ["FREQTRADE__DRY_RUN"] = "false"
        log.warning("LIVE TRADING ENABLED (intent recorded) -- Freqtrade will NOT actually go live until FREQTRADE__DRY_RUN=false is also set in Portainer's own stack Environment Variables and the stack is redeployed")
    else:
        set_key(str(ENV_PATH), "FREQTRADE__DRY_RUN", "true", quote_mode="never")
        os.environ["FREQTRADE__DRY_RUN"] = "true"
        log.info("Live trading disabled (intent recorded)")

    return {
        "ok": True,
        "live": req.enable,
        "note": (
            "Recorded here, but this does NOT change Freqtrade's actual behavior. "
            "Go to Portainer -> Stacks -> survival-agent -> Editor -> Environment variables, "
            f"set FREQTRADE__DRY_RUN={'false' if req.enable else 'true'}, then redeploy the stack. "
            "Freqtrade's real credentials/mode come from there, not from this app -- see AGENTS.md."
        ),
    }


# P12-fix follow-up: per-bot max_open_trades, editable from this UI.
# This is a DELIBERATE, narrow exception to "The Credentials panel
# became read-only" (see AGENTS.md) -- that entry warns against
# reintroducing write endpoints for anything in ENV_VAR_DEFS, because
# an earlier version of this UI let you type a value that had zero
# effect on any bot and silently drifted from Portainer's real one.
# This follows the ONE precedent that already survived that cleanup --
# /api/live-trading above -- exactly: record intent in data/.env,
# return it, and say loudly, every time, that it does NOT change any
# bot's actual behavior until the same value is also set in Portainer's
# stack Environment Variables and the stack is redeployed. Do not make
# this editable-and-silent; that is the exact bug this pattern exists
# to avoid repeating.
MAX_OPEN_TRADES_ENV_KEYS = {
    "main": "MAIN_MAX_OPEN_TRADES",
    "shorts": "SHORTS_MAX_OPEN_TRADES",
    "moonshot": "MOONSHOT_MAX_OPEN_TRADES",
    "listing_fade": "FADE_MAX_OPEN_TRADES",
    "trend_ensemble": "TREND_MAX_OPEN_TRADES",
    "trend_v2": "TREND_V2_MAX_OPEN_TRADES",
}
# Same numbers as each bot's own config.json / docker-compose.yml default.
DEFAULT_MAX_OPEN_TRADES = {
    "main": 10, "shorts": 10, "moonshot": 10, "listing_fade": 3, "trend_ensemble": 10,
    "trend_v2": 10,
}


class MaxOpenTradesUpdate(BaseModel):
    bot_id: str
    max_open_trades: int


@app.get("/api/max-open-trades")
def get_max_open_trades():
    """Read-only-in-spirit: returns this app's RECORDED INTENT per bot
    (data/.env), not a live-verified fact -- see module note above.
    `default_in_repo` is what the bot actually runs with if Portainer
    has never set the var at all."""
    values = dotenv_values(ENV_PATH)
    result = {}
    for bot_id, env_key in MAX_OPEN_TRADES_ENV_KEYS.items():
        raw = values.get(env_key)
        try:
            recorded_intent = int(raw) if raw else None
        except ValueError:
            recorded_intent = None
        result[bot_id] = {
            "env_var": env_key,
            "recorded_intent": recorded_intent,
            "default_in_repo": DEFAULT_MAX_OPEN_TRADES[bot_id],
        }
    return result


@app.post("/api/max-open-trades")
def set_max_open_trades(req: MaxOpenTradesUpdate):
    if req.bot_id not in MAX_OPEN_TRADES_ENV_KEYS:
        raise HTTPException(400, f"Unknown bot id '{req.bot_id}', expected one of {list(MAX_OPEN_TRADES_ENV_KEYS)}")
    if req.max_open_trades < 1:
        raise HTTPException(400, "max_open_trades must be at least 1")

    env_key = MAX_OPEN_TRADES_ENV_KEYS[req.bot_id]
    set_key(str(ENV_PATH), env_key, str(req.max_open_trades), quote_mode="never")
    os.environ[env_key] = str(req.max_open_trades)
    log.info("Max open trades intent recorded for %s: %d (env var %s)", req.bot_id, req.max_open_trades, env_key)

    return {
        "ok": True,
        "bot_id": req.bot_id,
        "max_open_trades": req.max_open_trades,
        "note": (
            f"Recorded here, but this does NOT change {req.bot_id}'s actual behavior. "
            f"Go to Portainer -> Stacks -> survival-agent -> Editor -> Environment variables, "
            f"set {env_key}={req.max_open_trades}, then redeploy the stack. "
            "Freqtrade's real config comes from there, not from this app -- see AGENTS.md."
        ),
    }


@app.get("/api/env-status")
def env_status():
    """
    Read-only. Every var in ENV_VAR_DEFS as this container actually
    sees it right now, live, via os.getenv() -- nothing here is
    written by this app; Portainer's stack Environment Variables is
    the only place to set any of these (see docker-compose.yml and
    AGENTS.md for why an earlier version of this app tried to also
    accept values through this UI, and why that was removed).
    status is "missing" (not set at all), "empty" (set to ""), or
    "set". For the three *_DRY_RUN vars specifically, missing/empty
    is treated as "default" (Freqtrade's own safe default of paper
    trading), not a warning -- only an explicit "false" is flagged.
    """
    result = []
    for d in ENV_VAR_DEFS:
        raw = os.getenv(d["key"])
        if d.get("is_dry_run"):
            if raw is None or raw == "":
                status, display = "default", "true (default)"
            elif raw.lower() == "false":
                status, display = "live", "false — REAL MONEY"
            else:
                status, display = "default", raw
        elif raw is None:
            status, display = "missing", None
        elif raw == "":
            status, display = "empty", None
        else:
            status, display = "set", (_mask(raw) if d["secret"] else raw)
        result.append({
            "key": d["key"], "desc": d["desc"], "used_by": d["used_by"], "group": d["group"],
            "status": status, "display": display,
        })
    return result


# Our own ledger of real money put into (or taken out of) each bot's
# trading account. Deliberately separate from Freqtrade's own
# starting_capital (from /api/v1/balance): that value is inferred as
# current balance minus realized trading profit, which is not
# guaranteed to tell a manual deposit/withdrawal apart from actual
# trading gains/losses -- a deposit could silently show up as "profit".
# This ledger is the authoritative source for "% return on what was
# actually put in" once any deposit/withdrawal happens after first
# launch. Seeded once from each bot's own dry_run_wallet (config.json)
# as the initial entry, then only ever appended to, never overwritten
# -- a full history, not just a running total, so a past adjustment is
# never silently lost.
CAPITAL_BOT_IDS = ["main", "shorts", "moonshot"]
DEFAULT_STARTING_CAPITAL = {"main": 500.0, "shorts": 500.0, "moonshot": 100.0}


def _load_capital() -> dict:
    if not CAPITAL_PATH.exists():
        now = datetime.now(timezone.utc).isoformat()
        seeded = {
            bot_id: {"contributions": [{"date": now, "amount": amount, "note": "initial (dry_run_wallet)"}]}
            for bot_id, amount in DEFAULT_STARTING_CAPITAL.items()
        }
        CAPITAL_PATH.write_text(json.dumps(seeded, indent=2))
        return seeded
    return json.loads(CAPITAL_PATH.read_text())


class CapitalContribution(BaseModel):
    bot_id: str
    amount: float  # positive = deposit, negative = withdrawal
    note: Optional[str] = None


@app.get("/api/capital")
def get_capital():
    data = _load_capital()
    return {
        bot_id: {
            "total_contributed": sum(c["amount"] for c in bot["contributions"]),
            "contributions": bot["contributions"],
        }
        for bot_id, bot in data.items()
    }


@app.post("/api/capital")
def add_capital_contribution(c: CapitalContribution):
    if c.bot_id not in CAPITAL_BOT_IDS:
        raise HTTPException(400, f"Unknown bot id '{c.bot_id}', expected one of {CAPITAL_BOT_IDS}")
    data = _load_capital()
    data.setdefault(c.bot_id, {"contributions": []})
    data[c.bot_id]["contributions"].append({
        "date": datetime.now(timezone.utc).isoformat(),
        "amount": c.amount,
        "note": c.note or "",
    })
    CAPITAL_PATH.write_text(json.dumps(data, indent=2))
    total = sum(x["amount"] for x in data[c.bot_id]["contributions"])
    log.info("Capital contribution recorded: bot=%s amount=%+g new_total=%g note=%r", c.bot_id, c.amount, total, c.note)
    return {"ok": True, "bot_id": c.bot_id, "total_contributed": total}


# Daily balance history -- one snapshot per calendar day (UTC), per bot,
# so the dashboard can show "vs N days ago" instead of only "vs whenever
# this bot's wallet started". Kept for BALANCE_HISTORY_RETENTION_DAYS (~6
# months) and pruned automatically on every write -- the operator was
# explicit this doesn't need to be kept forever.
def _load_balance_history() -> dict:
    if not BALANCE_HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(BALANCE_HISTORY_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Could not read balance history, treating as empty: %s", e)
        return {}


def _prune_balance_history(history: dict) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=BALANCE_HISTORY_RETENTION_DAYS)).date().isoformat()
    return {day: snap for day, snap in history.items() if day >= cutoff}


def _snapshot_balances_if_needed() -> None:
    """
    Writes one entry to balance_history.json for today (UTC), if one isn't
    there yet. Only ever reads each bot's already-fetched /api/v1/balance
    (via freqtrade_client, same call the dashboard itself makes) -- no new
    external calls. Skips writing entirely if every bot is unreachable
    right now, rather than recording an all-empty day: the hourly check
    loop will just try again next hour, same calendar day.
    """
    from .engine import freqtrade_client

    today = datetime.now(timezone.utc).date().isoformat()
    history = _load_balance_history()
    if today in history:
        return

    snapshot = {}
    for bot_id, s in freqtrade_client.get_all_summaries().items():
        if not s.get("available"):
            continue
        bal = s.get("balance") or {}
        total = bal.get("total") if isinstance(bal.get("total"), (int, float)) else bal.get("value")
        if isinstance(total, (int, float)):
            snapshot[bot_id] = total

    if not snapshot:
        log.info("Balance snapshot for %s skipped -- no bot reachable yet, will retry next hour", today)
        return

    history[today] = snapshot
    history = _prune_balance_history(history)
    BALANCE_HISTORY_PATH.write_text(json.dumps(history, indent=2))
    log.info("Balance snapshot recorded for %s: %s", today, snapshot)


@app.get("/api/balance-history")
def get_balance_history():
    """Read-only, pruned to the retention window on every read too (in case
    the write-time prune was skipped, e.g. right after a fresh deploy)."""
    return _prune_balance_history(_load_balance_history())


# P9: this endpoint does NOT compute anything -- backend/reports/bot_scorecard.py
# reads each bot's sqlite trades DIRECTLY (read-only, same pattern as
# backtest/auto_tune.py), which this container has no filesystem access to
# (only survival-agent-data:/data is mounted here, none of the freqtrade*-db
# volumes -- see that script's own module docstring). Run it externally,
# point --out-json at a copy of this file (or paste one in), and this
# endpoint just serves whatever was last written -- same "external writer,
# backend reads the file" pattern as /api/balance-history above.
@app.get("/api/scorecard")
def get_scorecard():
    if not SCORECARD_PATH.exists():
        return {"available": False, "reason": "no scorecard generated yet -- run backend/reports/bot_scorecard.py"}
    try:
        return json.loads(SCORECARD_PATH.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read scorecard.json: %s", e)
        return {"available": False, "reason": f"scorecard.json unreadable: {e}"}


_engine_state = {"last_cycle_at": None, "last_outcome": None, "cycles_run": 0}


@app.get("/api/status")
def status():
    cfg = json.loads(CONFIG_PATH.read_text())
    values = dotenv_values(ENV_PATH)
    freqtrade_dry_run = values.get("FREQTRADE__DRY_RUN", "true").lower() != "false"
    return {
        "engine_enabled": cfg.get("engine_enabled", False),
        "dry_run": os.getenv("DRY_RUN", "true").lower() != "false",
        "freqtrade_live": not freqtrade_dry_run,
        "last_cycle_at": _engine_state["last_cycle_at"],
        "last_outcome": _engine_state["last_outcome"],
        "cycles_run": _engine_state["cycles_run"],
        "cycle_interval_seconds": CYCLE_INTERVAL_SECONDS,
    }


@app.get("/api/freqtrade-summary")
def freqtrade_summary(bot: str = "main"):
    from .engine import freqtrade_client
    return freqtrade_client.get_summary(bot)


@app.get("/api/freqtrade-summary/all")
def freqtrade_summary_all():
    from .engine import freqtrade_client
    return freqtrade_client.get_all_summaries()


# Manual payout confirmation -- see backend/engine/payout_state.py's own
# docstring. Money already moved outside this code; this just resets the
# accounting baseline so run_cycle() stops re-splitting the same profit.
# The isolated experiments are intentionally excluded from SPLIT_BOTS.
class PayoutConfirm(BaseModel):
    amount_usd: float


@app.post("/api/payout/confirm")
def confirm_payout(p: PayoutConfirm):
    from .engine import freqtrade_client, payout_state

    per_bot_profit_now = {}
    for bot_id in payout_state.SPLIT_BOTS:
        summary = freqtrade_client.get_summary(bot_id)
        if not summary.get("available"):
            raise HTTPException(
                409, f"Cannot confirm payout -- bot '{bot_id}' is unreachable right now, "
                     "its current profit can't be snapshotted as the new baseline. Retry once it's up."
            )
        per_bot_profit_now[bot_id] = payout_state.extract_profit(summary.get("profit"))

    state = payout_state.confirm_payout(DATA_DIR, per_bot_profit_now, p.amount_usd)
    log.info("Payout confirmed: amount_usd=%g per_bot=%s", p.amount_usd, per_bot_profit_now)
    return {"ok": True, "state": state}


@app.get("/api/payout/history")
def payout_history():
    from .engine import payout_state
    state = payout_state.load_state(DATA_DIR)
    return {"baseline_set_at": state["baseline_set_at"], "bots": state["bots"], "history": state["history"]}


@app.get("/api/decisions")
def get_decisions(limit: int = 20):
    if not DECISIONS_PATH.exists():
        return []
    lines = DECISIONS_PATH.read_text().strip().splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.reverse()
    return out


async def _engine_background_loop():
    from .engine import loop as engine_loop

    while True:
        cfg = json.loads(CONFIG_PATH.read_text())
        if cfg.get("engine_enabled", False):
            try:
                result = await asyncio.to_thread(engine_loop.run_cycle, DATA_DIR, cfg)
                _engine_state["last_outcome"] = result.get("outcome", result.get("stage"))
                _engine_state["cycles_run"] += 1
                log.info("Cycle result: %s", result)
            except Exception as e:
                log.error("Engine cycle failed: %s", e)
                _engine_state["last_outcome"] = f"error: {e}"
            _engine_state["last_cycle_at"] = datetime.now(timezone.utc).isoformat()
        await asyncio.sleep(CYCLE_INTERVAL_SECONDS)


async def _balance_history_loop():
    # Deliberately independent of engine_enabled -- the operator wants
    # daily balance history regardless of whether the (mostly dormant,
    # see AGENTS.md) accounting engine loop is on.
    while True:
        try:
            await asyncio.to_thread(_snapshot_balances_if_needed)
        except Exception as e:
            log.error("Balance history snapshot check failed: %s", e)
        await asyncio.sleep(BALANCE_HISTORY_CHECK_SECONDS)


async def _portfolio_brain_loop():
    """P17-A: run the (shadow-only) portfolio brain once a day at 00:10 UTC. It only
    reads the bots' GET endpoints and writes files under DATA_DIR."""
    from .engine import portfolio_brain

    while True:
        await asyncio.sleep(portfolio_brain.seconds_until_next(datetime.now(timezone.utc), 0, 10))
        try:
            await asyncio.to_thread(portfolio_brain.run_daily, DATA_DIR)
        except Exception as e:
            log.error("Portfolio brain run failed: %s", e)
        await asyncio.sleep(90)   # never fire twice inside the same minute


async def _signal_audit_loop():
    """P19-A: nightly ledger + forward-return audit at 00:30 UTC (after the brain's 00:10 run)."""
    from .engine import portfolio_brain, signal_audit

    while True:
        await asyncio.sleep(portfolio_brain.seconds_until_next(datetime.now(timezone.utc), 0, 30))
        try:
            await asyncio.to_thread(signal_audit.run_nightly, DATA_DIR)
        except Exception as e:
            log.error("Signal audit run failed: %s", e)
        await asyncio.sleep(90)


# ----------------------------------------------------------------------- P21 replay (separate page /replay)
@app.get("/replay")
def replay_page():
    return FileResponse("/app/frontend/replay.html")


@app.get("/calibration")
def calibration_page():
    return FileResponse("/app/frontend/calibration.html")


@app.get("/api/replay/config")
def replay_config():
    from .replay import engine as rengine, llm as rllm, runner as rrunner

    providers = {}
    for kind in rllm.PROVIDERS:
        try:
            pr = rllm.provider_from_env(kind)
            providers[kind] = {"ready": bool(pr.api_key), "label": pr.label, "rpm": pr.rpm, "daily_cap": pr.daily_cap, "problem": None if pr.api_key else "API key is not set"}
        except rllm.ProviderConfigError as e:
            providers[kind] = {"ready": False, "label": kind, "rpm": None, "daily_cap": None, "problem": str(e)}
    return {"bots": [{"key": k, "default": s["default"], "category": s["category"], "universe_mismatch": k in rrunner.MECHANICS_ONLY} for k, s in rengine.BOTS.items()], "limits": rrunner.limits(),
            "providers": providers, "default_provider": "deepseek", "max_variants": rrunner.MAX_VARIANTS,
            "price_note": "the displayed price is conservative (top tier, no cache discount): the real DeepSeek bill per key is 2-3x lower -- check it against the Usage export",
            "defaults": {"days": 30, "pairs": 20, "every_n": 4, "modes": ["named", "anonymized"], "add_rule": True, "minute_prices": True,
                         "pilot": {"days": 7, "every_n": 4, "pairs": 15, "max_usd": 1.0, "modes": ["named", "anonymized"], "bots": [k for k, s in rengine.BOTS.items() if s["default"]]}},
            "every_n_choices": list(rrunner.EVERY_N_CHOICES), "running": rrunner.MANAGER.running()}


@app.post("/api/replay/estimate")
async def replay_estimate(raw: dict):
    from .replay import runner as rrunner

    try:
        return await asyncio.to_thread(rrunner.estimate_for, raw)
    except rrunner.ParamError as e:
        raise HTTPException(400, str(e))
    except Exception as e:      # noqa: BLE001
        raise HTTPException(502, f"estimate failed: {type(e).__name__}: {e}")


@app.post("/api/replay/start")
async def replay_start(raw: dict):
    from .replay import runner as rrunner

    try:
        est = await asyncio.to_thread(rrunner.estimate_for, raw)
        if not est["prices_known"]:
            raise HTTPException(409, "REPLAY_PRICE_IN_PER_M / REPLAY_PRICE_OUT_PER_M are not set: the dollar limit cannot be enforced")
        if not est["within_limits"]:
            raise HTTPException(409, f"estimate ${est['usd']} / {est['calls']} calls exceeds the limits (max ${est['params']['max_usd']}, {est['params']['max_calls']} calls)")
        params = rrunner.normalize_params(raw)
        run_id = rrunner.MANAGER.start(params)
        return {"run_id": run_id, "estimate": est}
    except rrunner.ParamError as e:
        raise HTTPException(409 if "already running" in str(e) else 400, str(e))
    except HTTPException:
        raise
    except Exception as e:      # noqa: BLE001 -- the page must show WHY (a bare 500 hides it); the traceback goes to the container log
        log.exception("replay start failed")
        raise HTTPException(502, f"start failed: {type(e).__name__}: {e}")


@app.get("/api/replay/lf/state")
def replay_lf_state():
    """What the Listing Fade study has cached (instruments, hourly / funding / 5-minute files, the last fetch and its errors) and what a fetch would cost in API calls."""
    from .replay import listing_fade

    st = listing_fade.lf_state()
    n = (st["instruments"] or {}).get("n") or 0
    st["estimate"] = {"api_calls_if_nothing_cached": (n * 3 + 400) if not st["data_ready"] else max(0, n - st["hourly_with_data"]) * 3,
                      "note": "public Bybit endpoints, no key, no cost; about 0.12 s per call (rate-limit friendly); resumable -- cached files are never requested again"}
    return st


@app.post("/api/replay/lf/fetch")
async def replay_lf_fetch():
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_lf_fetch)
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"started": rid}


@app.post("/api/replay/lf/study")
async def replay_lf_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_lf_study, raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409 if "already running" in str(e) else 400, str(e))
    except Exception as e:      # noqa: BLE001
        log.exception("listing fade study failed to start")
        raise HTTPException(502, f"study failed to start: {type(e).__name__}: {e}")
    return {"run_id": rid, "cost_usd": 0}


@app.get("/api/replay/kronos/state")
def replay_kronos_state():
    """The Kronos experiment: the worker's job / progress / signal counts / benchmark, the default estimate and the last evaluation."""
    from .replay import kronos_eval, kronos_replay as kr

    st = kr.status()
    defaults = {"days": 30, "pairs": 20, "shards": 1}
    try:
        est = kr.estimate(kr.normalize(defaults))
    except kr.KronosError as e:
        est = {"error": str(e)}
    return {**st, "defaults": defaults, "estimate": est, "latest_eval": kronos_eval.latest_eval(),
            "note": "the kronos-replay container does the forecasting (CPU only, nice 10, idle until a job exists); this page only prepares the job and reads the result"}


@app.post("/api/replay/kronos/estimate")
async def replay_kronos_estimate(raw: dict):
    from .replay import kronos_replay as kr

    try:
        return kr.estimate(kr.normalize(raw))
    except kr.KronosError as e:
        raise HTTPException(400, str(e))


@app.post("/api/replay/kronos/generate")
async def replay_kronos_generate(raw: dict):
    """Writes the job (candles up to the window end only) and clears STOP: the worker picks it up (or resumes it). Requires confirm=true (the card shows the estimate first)."""
    import requests
    from .replay import kronos_replay as kr

    if not raw.get("confirm"):
        raise HTTPException(409, "confirm is required: the card shows the estimate (inferences, hours) first")
    try:
        return await asyncio.to_thread(kr.prepare, raw, requests.get)
    except kr.KronosError as e:
        raise HTTPException(400, str(e))
    except Exception as e:      # noqa: BLE001
        log.exception("kronos generate failed")
        raise HTTPException(502, f"could not prepare the job: {type(e).__name__}: {e}")


@app.post("/api/replay/kronos/stop")
def replay_kronos_stop():
    from .replay import kronos_replay as kr

    kr.request_stop()
    return {"stopping": True}


@app.post("/api/replay/kronos/benchmark")
async def replay_kronos_benchmark(raw: dict | None = None):
    import requests
    from .replay import kronos_replay as kr

    try:
        return await asyncio.to_thread(kr.request_bench, requests.get, None, None, int((raw or {}).get("n", 12)))
    except Exception as e:      # noqa: BLE001
        raise HTTPException(502, f"could not prepare the benchmark: {type(e).__name__}: {e}")


@app.post("/api/replay/kronos/evaluate")
async def replay_kronos_evaluate(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_kronos_eval, raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    except Exception as e:      # noqa: BLE001
        log.exception("kronos evaluation failed to start")
        raise HTTPException(502, f"evaluation failed to start: {type(e).__name__}: {e}")
    return {"run_id": rid, "cost_usd": 0}


@app.get("/api/replay/kronos/eval/{eval_id}/download")
def replay_kronos_download(eval_id: str):
    from fastapi.responses import Response
    from .replay import kronos_eval

    data = kronos_eval.zip_eval(eval_id)
    if data is None:
        raise HTTPException(404, "no such evaluation")
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="kronos_{eval_id}.zip"'})


@app.get("/api/replay/carry/state")
def replay_carry_state():
    from .replay import carry

    return carry.state()


@app.post("/api/replay/carry/study")
async def replay_carry_study(raw: dict | None = None):
    """Funding carry study: fetch the public Bybit history (resumable) and simulate. $0, no model, no key."""
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_carry_study, raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.get("/api/replay/delist/state")
def replay_delist_state():
    from .replay import delist_study

    return delist_study.state()


@app.post("/api/replay/delist/study")
async def replay_delist_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "delist_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/momentum/study")
async def replay_momentum_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "momentum_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/lgbm/study")
async def replay_lgbm_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "lgbm_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/lgbm-hourly/study")
async def replay_lgbm_hourly_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "lgbm_hourly_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/fade-exit/study")
async def replay_fade_exit_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_fade_exit_study, raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/lgbm-grid/study")
async def replay_lgbm_grid_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "lgbm_grid_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/lgbm-premium/study")
async def replay_lgbm_premium_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "lgbm_premium_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.post("/api/replay/maker/study")
async def replay_maker_study(raw: dict | None = None):
    from .replay import runner as rrunner

    try:
        rid = await asyncio.to_thread(rrunner.MANAGER.start_study, "maker_study", raw or {})
    except rrunner.ParamError as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid, "cost_usd": 0}


@app.get("/api/replay/live-reference")
def replay_live_reference():
    from .replay import live_reference as lref

    ref = lref.read()
    if ref is None:
        return {"available": False, "reason": "no live_reference.json yet -- press the refresh button on /replay (or build it from a P18 export ZIP)"}
    meta = ref.pop("_meta", {})
    return {"available": True, "meta": meta, "bots": ref}


@app.post("/api/replay/live-reference/refresh")
async def replay_live_reference_refresh(raw: dict | None = None):
    """Refresh live_reference.json AND live_snapshot.json from the live bots, through the SAME code the P18 export uses (export.iter_trades + its trade-row
    normalisation for the trades, export.log_files / iter_all for the logs). This is an explicit operator action, separate from any replay run: after it the replay
    and the calibration read only the two files inside the sandbox and never open a live bot."""
    from . import export as export_mod
    from .replay import engine as rengine, live_data, live_reference as lref

    raw = raw or {}
    date_from = str(raw.get("from") or lref.DEFAULT_FROM)
    date_to = raw.get("to") or None
    if lref._utc(date_from) is None or (date_to and lref._utc(date_to) is None):
        raise HTTPException(400, "from / to must be dates like 2026-09-21")

    def decision_lines(files):
        for line in export_mod.iter_all(files):
            yield export_mod.mask_secrets(line) if "DeepSeek said" in line else line

    def collect():
        fetcher = export_mod._fetcher()
        rows_by_bot, snap, status = {}, {}, {}
        for bot in list(rengine.BOTS) + ["listing_fade"]:       # listing_fade: its trades feed the Listing Fade study's comparison with the live dry-run
            st: dict = {}
            rows = None
            try:
                rows = [export_mod._trade_row(t) for t in export_mod.iter_trades(bot, fetcher, "all")]
                rows_by_bot[bot] = rows
                st.update({"ok": True, "trades_in_export": len(rows)})
            except HTTPException as e:
                st.update({"ok": False, "note": str(e.detail)})
            except Exception as e:      # noqa: BLE001
                st.update({"ok": False, "note": f"{type(e).__name__}: {e}"})
            lines = None
            try:
                files = export_mod.log_files(bot)
                if files:
                    lines = decision_lines(files)
                    entry = live_data.build_bot(lines, rows)
                else:
                    entry = live_data.build_bot(None, rows)
                st["decisions_in_logs"] = len(entry["decisions"])
                if lines is None:
                    st["logs"] = "no log files mounted for this bot"
            except Exception as e:      # noqa: BLE001
                entry = live_data.build_bot(None, rows)
                st["logs"] = f"unreadable: {type(e).__name__}: {e}"
            if rows is not None or entry["decisions"]:
                snap[bot] = entry
            status[bot] = st
        return rows_by_bot, snap, status

    rows_by_bot, snap, status = await asyncio.to_thread(collect)
    if not rows_by_bot:
        raise HTTPException(502, "no bot answered (REST unreachable or credentials missing): the previous live_reference.json and live_snapshot.json, if any, were kept")
    ref = lref.build_from_rows({b: snap[b]["trades"] for b in rows_by_bot}, date_from, date_to)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    meta = {"source": "P18 export code path (live bots' REST via export.iter_trades, logs via export.log_files)", "updated_at": stamp, "from": date_from, "to": date_to, "per_bot_status": status}
    lref.write(ref, meta)
    live_data.write_snapshot(snap, {"source": meta["source"], "updated_at": stamp, "per_bot_status": status})
    coverage = {b: live_data.coverage(e) for b, e in snap.items() if e.get("decisions")}
    return {"available": True, "meta": {"per_bot_status": status, "from": date_from, "to": date_to}, "bots": ref, "coverage": coverage}


@app.get("/api/replay/calibration/coverage")
def replay_calibration_coverage():
    """What the last snapshot can calibrate: per bot the period the live decisions really cover (read from the sandbox, not from a live bot)."""
    from .replay import calibration, live_data

    snap = live_data.read_snapshot()
    if snap is None:
        return {"available": False, "reason": "no live snapshot yet -- press 'Update live reference' (it also reads the logs for the calibration)"}
    return {"available": True, "meta": snap.get("_meta", {}), "bots": {b: live_data.coverage(e) for b, e in snap["bots"].items() if b in calibration.BOTS}}


@app.post("/api/replay/calibration/start")
async def replay_calibration_start(raw: dict | None = None):
    """Start a calibration run: the simulator replays the live bots' real decisions (no LLM, no cost). Status / stop / results use the same /api/replay/* endpoints."""
    from .replay import runner as rrunner

    raw = raw or {}
    try:
        run_id = await asyncio.to_thread(rrunner.MANAGER.start_calibration, raw.get("bots"), raw.get("from") or None, raw.get("to") or None)
    except rrunner.ParamError as e:
        raise HTTPException(409 if "already running" in str(e) else 400, str(e))
    return {"run_id": run_id, "cost_usd": 0}


@app.get("/api/replay/status")
def replay_status():
    from .replay import runner as rrunner

    return rrunner.MANAGER.status()


@app.post("/api/replay/stop")
def replay_stop():
    from .replay import runner as rrunner

    rrunner.MANAGER.stop()
    return {"stopping": rrunner.MANAGER.running()}


@app.get("/api/replay/runs")
def replay_runs():
    from .replay import runner as rrunner

    return {"runs": rrunner.list_runs()}


@app.get("/api/replay/runs/{run_id}")
def replay_run(run_id: str):
    from .replay import paths as rpaths, runner as rrunner

    try:
        rep = rrunner.get_run(run_id)
    except rpaths.ReplayPathError:
        raise HTTPException(400, "bad run id")
    if rep is None:
        raise HTTPException(404, "no report for this run (still running, or it failed before the report)")
    return rep


@app.get("/api/replay/runs/{run_id}/download")
def replay_download(run_id: str):
    from fastapi.responses import Response
    from .replay import paths as rpaths, runner as rrunner

    try:
        data = rrunner.zip_run(run_id)
    except rpaths.ReplayPathError:
        raise HTTPException(400, "bad run id")
    if data is None:
        raise HTTPException(404, "no such run")
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="replay_{run_id}.zip"'})


@app.get("/api/replay/runs/{run_id}/log")
def replay_run_log(run_id: str):
    from fastapi.responses import PlainTextResponse
    from . import downloads
    from .replay import paths as rpaths

    try:
        d = rpaths.replay_path(run_id)
    except rpaths.ReplayPathError:
        raise HTTPException(400, "bad run id")
    text = downloads.run_log(d)
    if text is None:
        raise HTTPException(404, "no log for this run")
    return PlainTextResponse(text, headers={"Content-Disposition": f'attachment; filename="replay_{run_id}_log.txt"'})


@app.get("/api/replay/kronos/files.zip")
def replay_kronos_files():
    from fastapi.responses import Response
    from . import downloads
    from .replay import paths as rpaths

    data = downloads.kronos_files_zip(rpaths.root() / "kronos")
    if data is None:
        raise HTTPException(404, "no Kronos historical job yet")
    return Response(content=data, media_type="application/zip", headers={"Content-Disposition": 'attachment; filename="kronos_historical.zip"'})


@app.get("/api/downloads")
def downloads_catalog():
    """One list of every file the system writes (the bot logs/trades are in /api/export/catalog)."""
    from . import downloads
    from .replay import paths as rpaths, runner as rrunner

    evals = []
    ed = rpaths.root() / "kronos" / "eval"
    if ed.is_dir():
        evals = sorted((x.name for x in ed.iterdir() if x.is_dir()), reverse=True)
    return downloads.catalog(rrunner.list_runs(), evals, DATA_DIR)


@app.get("/api/downloads/file/{source}/{name}")
def downloads_file(source: str, name: str):
    from . import downloads

    p = downloads.resolve(source, name, DATA_DIR)
    if p is None:
        raise HTTPException(404, "no such file")
    return FileResponse(str(p), filename=name, media_type="application/octet-stream")


@app.get("/api/bots/{bot_id}/why")
async def bot_why(bot_id: str, hours: int = 24):
    """Why is this bot not trading? Counts from its own log, the allocator weight and its state. Read-only."""
    from . import diagnose

    res = await asyncio.to_thread(diagnose.why, bot_id, max(1, min(hours, 168)))
    if res is None:
        raise HTTPException(404, "unknown bot")
    return res


@app.get("/api/bots/{bot_id}/params")
async def bot_params(bot_id: str):
    from . import bot_info

    res = await asyncio.to_thread(bot_info.params, bot_id)
    if res is None:
        raise HTTPException(404, "unknown bot")
    return res


@app.get("/api/kronos/health")
def get_kronos_health():
    """Live, read-only: what the kronos-signal container reports about its last cycle (time, pairs, truncation, model revisions)."""
    from .engine import signal_audit

    h = signal_audit.read_kronos_health()
    if h is None:
        return {"available": False, "reason": "no kronos_health.json yet -- the container writes it after its first hourly cycle (minute :02)"}
    return {"available": True, **h}


@app.get("/api/signals/audit")
def get_signal_audit():
    """Read-only: the last audit written by backend/engine/signal_audit.py."""
    path = DATA_DIR / "signals_audit.json"
    if not path.exists():
        return {"available": False, "reason": "no audit yet -- the first run is at 00:30 UTC (metrics need >= 200 decisions per bot)"}
    try:
        return {"available": True, **json.loads(path.read_text())}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read signals_audit.json: %s", e)
        return {"available": False, "reason": f"audit unreadable: {e}"}


async def _kronos_feed_loop():
    """P23: copy the kronos-signal container's per-bot rank file to the shared allocator volume where the bots read it (read-only for them)."""
    from .engine import kronos_feed

    while True:
        try:
            res = await asyncio.to_thread(kronos_feed.publish)
            if res.get("published"):
                log.info("Kronos rank file published for t0=%s: %s", res.get("t0"), res.get("bots"))
        except Exception as e:      # noqa: BLE001
            log.warning("Kronos feed publish failed: %s", e)
        await asyncio.sleep(120)


def _allocator_dir() -> Path:
    return Path(os.environ.get("ALLOCATOR_DIR") or "/allocator")


async def _ranker_loop():
    """P26-R: once a day after 00:15 UTC (checked every 30 minutes) run the ranker's paper forward test, when RANKER_MODE=shadow. Never touches a bot."""
    import requests

    from .engine import ranker
    from .replay import runner as rrunner

    await asyncio.sleep(90)
    while True:
        try:
            if ranker.mode() != "off" and not rrunner.MANAGER.running():
                res = await asyncio.to_thread(ranker.run_once, requests.get, None, DATA_DIR)
                if res.get("ran"):
                    log.info("Ranker: %s", {k: v for k, v in res.items() if k != "tail"})
        except Exception as e:      # noqa: BLE001
            log.warning("Ranker loop failed: %s", e)
        await asyncio.sleep(1800)


async def _announcements_loop():
    """P25: every 10 minutes read the exchange's delisting notices and publish blocked_pairs.json on the shared volume (the bots refuse NEW entries on a coin with a notice in force)."""
    from .engine import announcements

    await asyncio.sleep(45)
    while True:
        try:
            from .engine import bot_params
            await asyncio.to_thread(bot_params.publish, _allocator_dir())
            res = await asyncio.to_thread(announcements.refresh, DATA_DIR, _allocator_dir())
            if res.get("ok") and res.get("new"):
                log.info("Announcements: %s new notice(s); blocked now: %s", res["new"], res["blocked"])
            elif not res.get("ok"):
                log.warning("Announcements refresh failed: %s", res.get("error"))
        except Exception as e:      # noqa: BLE001
            log.warning("Announcements loop failed: %s", e)
        await asyncio.sleep(600)


@app.get("/api/ranker")
def get_ranker():
    """P26-R: the LightGBM ranker's paper (shadow) forward test: mode, status, today's ranking, the two paper books, forward statistics at 0.10% and 0.30% slippage and the computed verdict. Read-only."""
    from .engine import ranker

    return ranker.snapshot(DATA_DIR)


@app.post("/api/ranker/run")
def post_ranker_run():
    """Run the daily ranker job now (in a background thread). Needs RANKER_MODE=shadow. 409 while it is already running."""
    import threading

    import requests

    from .engine import ranker
    from .replay import runner as rrunner

    if ranker.mode() == "off":
        raise HTTPException(409, "RANKER_MODE is off: switch it to shadow first")
    if ranker.RUN_LOCK.locked():
        raise HTTPException(409, "the ranker job is already running")
    if rrunner.MANAGER.running():
        raise HTTPException(409, "a replay job is running (one data job at a time)")
    threading.Thread(target=lambda: ranker.run_once(requests.get, None, DATA_DIR, True), daemon=True, name="ranker-manual").start()
    return {"started": True}


@app.get("/api/bot-params")
def get_bot_params():
    """The bot parameters the dashboard publishes to the bots (Listing Fade re-entry cooldown): wanted value, file content and age. Read-only."""
    from .engine import bot_params

    return bot_params.status(_allocator_dir())


@app.get("/api/announcements")
def get_announcements():
    """The coins the bots currently may not open new trades on (exchange delisting notices), file age and ledger size. Read-only."""
    from .engine import announcements

    return announcements.status(DATA_DIR, _allocator_dir())


@app.get("/api/kronos/rank")
def get_kronos_rank():
    """What the bots currently see from Kronos (age, how many pairs per bot are buy / sell_short / hold). Read-only."""
    from .engine import kronos_feed

    return kronos_feed.status()


async def _market_context_loop():
    """P20-A: hourly (minute :05) market-context snapshot, then the allocator's event check."""
    from .engine import allocator, market_context

    await asyncio.sleep(30)
    while True:
        try:
            await asyncio.to_thread(market_context.run_hourly, DATA_DIR)
        except Exception as e:
            log.error("Market context run failed: %s", e)
        try:
            await asyncio.to_thread(allocator.check_events, DATA_DIR)
        except Exception as e:
            log.error("Allocator event check failed: %s", e)
        await asyncio.sleep(market_context.seconds_until_next_hour(datetime.now(timezone.utc)))


async def _allocator_loop():
    """P20-B: daily allocator run at 00:40 UTC (after the brain 00:10 and the signal audit 00:30)."""
    from .engine import allocator, portfolio_brain

    while True:
        await asyncio.sleep(portfolio_brain.seconds_until_next(datetime.now(timezone.utc), 0, 40))
        try:
            await asyncio.to_thread(allocator.run_allocation, DATA_DIR)
        except Exception as e:
            log.error("Allocator run failed: %s", e)
        await asyncio.sleep(90)


@app.get("/api/context/latest")
def get_context_latest():
    """Read-only: the last hourly market-context snapshot (P20-A)."""
    from .engine import market_context

    snap = market_context.load_latest(DATA_DIR)
    if snap is None:
        return {"available": False, "reason": "no snapshot yet -- the collector runs hourly at minute :05"}
    return {"available": True, **snap}


@app.get("/api/portfolio/allocator")
def get_allocator():
    """Read-only: the allocator's last decision, counterfactual, mode and kill-switch state (P20-B)."""
    from .engine import allocator

    settings = allocator.load_settings()
    out = {"mode": settings["mode"], "kill": allocator.kill_state(DATA_DIR), "managed_bots": allocator.MANAGED_BOTS,
           "never_managed": allocator.NEVER_MANAGED}
    latest = allocator.load_json(DATA_DIR / allocator.LATEST_FILE, None)
    if latest is None:
        return {**out, "available": False, "reason": "no allocator run yet -- the first run is at 00:40 UTC"}
    history = [{k: a.get(k) for k in ("ts", "trigger", "effective_mode", "final", "rules_fired")}
               for a in allocator.load_allocations(DATA_DIR)[-15:]]
    return {**out, "available": True, **{k: v for k, v in latest.items() if k != "kill"}, "history": history}


class SettingChange(BaseModel):
    key: str
    value: str | None = None       # None = reset to the environment/default
    confirm: bool = False


@app.get("/api/settings/p20")
def get_p20_settings():
    """The dashboard switches for P19/P20, where each value comes from (ui / env / default), and enforce readiness."""
    from .engine import allocator, portfolio_brain, runtime_settings

    out = {"settings": runtime_settings.describe(DATA_DIR), "managed_bots": allocator.MANAGED_BOTS, "never_managed": allocator.NEVER_MANAGED}
    try:
        out["readiness"] = allocator.readiness(DATA_DIR, portfolio_brain.default_fetcher())
    except Exception as e:      # noqa: BLE001
        out["readiness"] = {"error": str(e)}
    return out


@app.post("/api/settings/p20")
def post_p20_setting(change: SettingChange):
    """Change one switch. enforce needs confirm=true and is refused (409) unless every managed bot is verifiably dry-run."""
    from .engine import allocator, portfolio_brain, runtime_settings

    warnings: list[str] = []
    if change.key == "PORTFOLIO_BRAIN_MODE" and (change.value or "").strip().lower() == "enforce":
        if not change.confirm:
            raise HTTPException(400, "enforce needs confirm=true")
        ready = allocator.readiness(DATA_DIR, portfolio_brain.default_fetcher())
        if ready["live_bots"]:
            raise HTTPException(409, f"refused: dry_run=false on {ready['live_bots']}")
        if ready["unverified_bots"]:
            raise HTTPException(409, f"refused: cannot verify dry_run on {ready['unverified_bots']} (bot unreachable?) -- try again when they are up")
        warnings = ready["warnings"]
    try:
        settings = runtime_settings.save({change.key: change.value}, DATA_DIR)
    except ValueError as e:
        raise HTTPException(400, str(e))
    log.warning("P20 setting changed from the UI: %s=%s", change.key, change.value)
    if change.key == "FADE_REENTRY_COOLDOWN_H":
        from .engine import bot_params
        bot_params.publish(_allocator_dir())            # reaches the bot now instead of at the next 10-minute loop
    return {"settings": settings, "warnings": warnings}


@app.post("/api/portfolio/allocator/run")
async def post_allocator_run():
    """Run the allocator once now (same code path as the daily run; mode and hard rules apply)."""
    from .engine import allocator

    latest = await asyncio.to_thread(allocator.run_allocation, DATA_DIR, None, None, None, None, None, "manual")
    return {"ran": not latest.get("skipped"), "effective_mode": latest.get("effective_mode"), "final": latest.get("final"),
            "mode_reasons": latest.get("mode_reasons")}


@app.post("/api/portfolio/allocator/kill")
def post_allocator_kill():
    """Kill switch: enforcement off, bots the allocator paused are resumed, weights back to 1. LAN-only like the rest."""
    from .engine import allocator, portfolio_brain

    return allocator.kill(DATA_DIR, "operator kill switch", "ui", allocator.default_control(), portfolio_brain.default_fetcher())


@app.post("/api/portfolio/allocator/resume")
def post_allocator_resume():
    """Clears the kill flag; enforcement still needs PORTFOLIO_BRAIN_MODE=enforce and verified dry_run on every bot."""
    from .engine import allocator

    return {"cleared": allocator.clear_kill(DATA_DIR)}


@app.post("/api/portfolio/brain/run")
async def post_portfolio_brain_run():
    """Run the portfolio brain once now (same code as the daily 00:10 run; read-only towards the bots). Use it after a roster change instead of waiting for the next night."""
    from .engine import portfolio_brain

    rep = await asyncio.to_thread(portfolio_brain.run_daily, DATA_DIR)
    if rep.get("skipped"):
        return {"ran": False, "reason": "PORTFOLIO_BRAIN_MODE is off"}
    cap, dd = rep.get("capital", {}), rep.get("drawdown", {})
    return {"ran": True, "equity": cap.get("equity"), "hwm": cap.get("hwm"), "hwm_rebased": cap.get("hwm_rebased"), "drawdown_from_hwm_pct": dd.get("drawdown_from_hwm_pct"),
            "highest_rung": (dd.get("highest_rung") or {}).get("level")}


@app.get("/api/portfolio/brain")
def get_portfolio_brain():
    """Read-only: the last report written by backend/engine/portfolio_brain.py (shadow mode)."""
    path = DATA_DIR / "portfolio_brain_latest.json"
    from .engine import runtime_settings
    mode = runtime_settings.merged_env(DATA_DIR).get("PORTFOLIO_BRAIN_MODE", "shadow")
    if not path.exists():
        return {"available": False, "mode": mode, "reason": "no report yet -- the first run is at 00:10 UTC"}
    try:
        return {"available": True, **json.loads(path.read_text())}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read portfolio_brain_latest.json: %s", e)
        return {"available": False, "mode": mode, "reason": f"report unreadable: {e}"}


@app.on_event("startup")
async def start_engine_loop():
    from .engine import allocator, portfolio_brain

    try:     # P20: enforce is refused while any managed bot is confirmed dry_run=false
        await asyncio.to_thread(allocator.startup_guard, portfolio_brain.default_fetcher())
    except allocator.EnforceRefused as e:
        log.critical("%s", e)
        raise
    asyncio.create_task(_engine_background_loop())
    asyncio.create_task(_balance_history_loop())
    asyncio.create_task(_portfolio_brain_loop())
    asyncio.create_task(_signal_audit_loop())
    asyncio.create_task(_market_context_loop())
    asyncio.create_task(_kronos_feed_loop())
    asyncio.create_task(_announcements_loop())
    asyncio.create_task(_ranker_loop())
    asyncio.create_task(_allocator_loop())
    log.info("Engine background loop started (checks engine_enabled every cycle, interval=%ss)", CYCLE_INTERVAL_SECONDS)
    log.info("Balance history loop started (checks hourly, one snapshot/day, %sd retention)", BALANCE_HISTORY_RETENTION_DAYS)


# P18: read-only export centre (logs / trades / bundle), see backend/export.py.
app.include_router(export_router)

app.mount("/assets", StaticFiles(directory="/app/frontend"), name="assets")


@app.get("/")
def index():
    return FileResponse("/app/frontend/index.html")
