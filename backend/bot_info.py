"""
Per-bot parameters for the Bots page: what the running Freqtrade bot reports about itself (`/api/v1/show_config`, read-only) cut down to the fields an operator reads, plus the
static facts the repository knows (strategy file, decision-source switch). If the bot is not reachable the static part is still returned.
"""
from __future__ import annotations

import os

from .engine import freqtrade_client as ftc

# what each bot is, in plain words; the decision env var is empty for rule bots
STATIC = {
    "main": {"strategy": "DeepSeekStrategy", "kind": "spot, long-only", "decision_env": "MAIN_DECISION_SOURCE", "provider_env": "MAIN_LLM_PROVIDER", "decider": "Kronos rank + LLM"},
    "shorts": {"strategy": "DeepSeekShortsStrategy", "kind": "futures, short and long, 1x", "decision_env": "SHORTS_DECISION_SOURCE", "provider_env": "SHORTS_LLM_PROVIDER", "decider": "Kronos rank + LLM"},
    "moonshot": {"strategy": "DeepSeekMoonshotStrategy", "kind": "spot, small and new listings, long-only", "decision_env": "MOONSHOT_DECISION_SOURCE", "provider_env": "MOONSHOT_LLM_PROVIDER", "decider": "Kronos rank + LLM"},
    "listing_fade": {"strategy": "ListingFadeStrategy", "kind": "futures, short only (fizzled pumps)", "decision_env": "", "provider_env": "", "decider": "rules, no LLM"},
    "trend_ensemble": {"strategy": "TrendEnsembleStrategy", "kind": "spot, Donchian breakout", "decision_env": "", "provider_env": "", "decider": "rules, no LLM"},
    "trend_v2": {"strategy": "TrendV2Strategy", "kind": "futures, Donchian long and short, 1x, BTC regime filter", "decision_env": "", "provider_env": "", "decider": "rules, no LLM"},
    "control_long": {"strategy": "ControlLongStrategy", "kind": "spot long-only, Freqtrade sample (baseline)", "decision_env": "", "provider_env": "", "decider": "rules, baseline"},
    "control_short": {"strategy": "ControlShortStrategy", "kind": "futures short-only, Freqtrade sample (baseline)", "decision_env": "", "provider_env": "", "decider": "rules, baseline"},
}
KEEP = ("strategy", "timeframe", "trading_mode", "margin_mode", "dry_run", "state", "runmode", "max_open_trades", "stake_currency", "stake_amount", "minimal_roi", "stoploss", "trailing_stop",
        "trailing_stop_positive", "trailing_stop_positive_offset", "trailing_only_offset_is_reached", "use_exit_signal", "exit_profit_only", "position_adjustment_enable", "exchange", "strategy_version", "bot_name")


def params(bot_id: str) -> dict | None:
    if bot_id not in ftc.BOTS:
        return None
    st = dict(STATIC.get(bot_id, {}))
    if st.get("decision_env"):
        st["decision_source"] = os.getenv(st["decision_env"], "kronos_llm")
        st["llm_provider"] = os.getenv(st["provider_env"], "deepseek") if st.get("provider_env") else None
    raw = None
    try:
        raw = ftc._authed_get(bot_id, "/api/v1/show_config")
    except Exception:       # noqa: BLE001
        raw = None
    live = {k: raw[k] for k in KEEP if k in raw} if isinstance(raw, dict) else None
    if live and isinstance(live.get("exchange"), str) is False and live.get("exchange") is not None:
        live["exchange"] = str(live["exchange"])
    return {"bot": bot_id, "label": ftc.BOTS[bot_id]["label"], "static": st, "live": live, "reachable": live is not None}
