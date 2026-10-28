"""
Talks to each Freqtrade bot's own REST API to read (never write) its
state: balance, open trades, profit. This is how survival-agent finds
out what each bot actually did, instead of guessing or duplicating
its decision logic.

Reachable at http://<service-name>:8080 -- all Freqtrade services and
survival-agent share the same per-stack Docker network automatically
(single docker-compose.yml, see its top-of-file comments).
"""
import logging
import os
import time

import requests

log = logging.getLogger("survival-agent.freqtrade_client")

# Every Freqtrade bot in this stack, keyed by a short id used
# throughout the API and UI. Adding a fourth bot later means adding
# one line here (and the matching service in docker-compose.yml) --
# nothing else in this file needs to change.
BOTS = {
    "main": {
        "label": "Main (spot, long-only)",
        "url": os.getenv("FREQTRADE_MAIN_INTERNAL_URL", "http://freqtrade:8080"),
    },
    "shorts": {
        "label": "Shorts (futures, 1x leverage)",
        "url": os.getenv("FREQTRADE_SHORTS_INTERNAL_URL", "http://freqtrade-shorts:8080"),
    },
    "moonshot": {
        "label": "Moonshot (small/new listings)",
        "url": os.getenv("FREQTRADE_MOONSHOT_INTERNAL_URL", "http://freqtrade-moonshot:8080"),
    },
    "listing_fade": {
        "label": "Listing Fade (rule-based, no LLM -- shorts fizzled pumps, dry until manually gated live)",
        "url": os.getenv("FREQTRADE_LISTING_FADE_INTERNAL_URL", "http://freqtrade-listing-fade:8080"),
    },
    "trend_ensemble": {
        "label": "Trend Ensemble (rule-based, no LLM -- Donchian breakout, dry until manually gated live)",
        "url": os.getenv("FREQTRADE_TREND_ENSEMBLE_INTERNAL_URL", "http://freqtrade-trend-ensemble:8080"),
    },
    "trend_v2": {
        "label": "Trend Ensemble v2 (rule-based, no LLM -- Donchian long+short futures 1x, BTC regime filter, dry until manually gated live)",
        "url": os.getenv("FREQTRADE_TREND_V2_INTERNAL_URL", "http://freqtrade-trend-v2:8080"),
    },
    # P17-C control group: read-only baselines (Freqtrade's own sample strategy,
    # hard dry-run). Deliberately NOT in payout_state.SPLIT_BOTS -- they are a
    # measuring stick, never part of the profit split.
    "control_long": {
        "label": "Control Long (Freqtrade sample strategy, spot long-only -- baseline, hard dry-run)",
        "url": os.getenv("FREQTRADE_CONTROL_LONG_INTERNAL_URL", "http://freqtrade-control-long:8080"),
    },
    "control_short": {
        "label": "Control Short (Freqtrade sample strategy, futures short-only 1x -- baseline, hard dry-run)",
        "url": os.getenv("FREQTRADE_CONTROL_SHORT_INTERNAL_URL", "http://freqtrade-control-short:8080"),
    },
}

_token_caches: dict[str, dict] = {bot_id: {"access_token": None, "expires_at": 0} for bot_id in BOTS}


def _get_token(bot_id: str) -> str | None:
    cache = _token_caches[bot_id]
    now = time.time()
    if cache["access_token"] and now < cache["expires_at"]:
        return cache["access_token"]

    username = os.getenv("FREQTRADE_API_USERNAME")
    password = os.getenv("FREQTRADE_API_PASSWORD")
    if not username or not password:
        log.warning("Freqtrade API credentials not set -- add them in the Credentials panel")
        return None

    try:
        r = requests.post(f"{BOTS[bot_id]['url']}/api/v1/token/login", auth=(username, password), timeout=10)
        r.raise_for_status()
        data = r.json()
        cache["access_token"] = data["access_token"]
        cache["expires_at"] = now + 12 * 60  # tokens last ~15min, refresh a bit early
        return cache["access_token"]
    except Exception as e:
        log.warning("Freqtrade login failed (%s): %s", bot_id, e)
        return None


def _authed_get(bot_id: str, path: str) -> dict | None:
    token = _get_token(bot_id)
    if not token:
        return None
    try:
        r = requests.get(f"{BOTS[bot_id]['url']}{path}", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("Freqtrade request failed (%s, %s): %s", bot_id, path, e)
        return None


def get_summary(bot_id: str = "main") -> dict:
    """
    Returns {"available": False, "reason": ...} if that bot isn't
    reachable, isn't configured, or credentials are missing --
    callers must handle that case, never assume it succeeded.
    """
    if bot_id not in BOTS:
        return {"available": False, "reason": f"unknown bot id '{bot_id}'"}

    base_url = BOTS[bot_id]["url"]
    try:
        ping = requests.get(f"{base_url}/api/v1/ping", timeout=5)
        if ping.status_code != 200:
            return {"available": False, "reason": f"{bot_id} not responding"}
    except Exception as e:
        return {"available": False, "reason": f"{bot_id} unreachable: {e}"}

    balance = _authed_get(bot_id, "/api/v1/balance")
    profit = _authed_get(bot_id, "/api/v1/profit")
    status = _authed_get(bot_id, "/api/v1/status")

    if balance is None and profit is None:
        return {"available": False, "reason": "reachable but login/credentials failed"}

    return {
        "available": True,
        "label": BOTS[bot_id]["label"],
        "dry_run": True,  # config.json default; see AGENTS.md for the "don't flip in code" invariant
        "balance": balance,
        "profit": profit,
        "open_trades": status if status is not None else [],
    }


def get_all_summaries() -> dict:
    """Convenience for the dashboard -- every bot's summary in one call."""
    return {bot_id: get_summary(bot_id) for bot_id in BOTS}
