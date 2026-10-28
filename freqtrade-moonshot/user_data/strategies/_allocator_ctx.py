"""
P20 -- bot-side, READ-ONLY access to the allocator's shared volume (mounted at /allocator, :ro).

Two jobs, both fail-safe (any problem = behave exactly as before P20):

1. context_block(pair): the "Derivatives & macro context" block that is appended to the LLM prompt. It is
   built from context_latest.json, written hourly by survival-agent's market_context collector. Every field
   shows its status; a field older than 3 hours is stale, older than 24 hours unavailable, and the block says
   in plain words that stale/unavailable means UNKNOWN, not neutral. No snapshot at all -> a block that says so.
   The bot makes no network call for any of this.

2. stake_multiplier(): the allocator's weight (0..1) for this bot, used by custom_stake_amount. It is applied
   ONLY when the weights file says mode == "enforce" and is younger than 36 hours. In shadow mode, with a
   missing, stale, unparseable or out-of-range file the multiplier is exactly 1.0. Entries and exits are not
   touched by this module; weight 0 is handled by the allocator pausing the bot (POST /stopentry).

Identical copy in every bot directory on purpose (each bot is built from its own directory).
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("allocator_ctx")

ALLOCATOR_DIR = os.environ.get("ALLOCATOR_DIR", "/allocator")
CONTEXT_FILE = "context_latest.json"
WEIGHTS_FILE = "weights.json"
STALE_S = 3 * 3600
DROP_S = 24 * 3600
WEIGHTS_MAX_AGE_S = 36 * 3600
MAX_STAKE_MULT = 2.0
CACHE_S = 60
CONTEXT_BOTS = ("main", "shorts", "moonshot")

_cache = {}


def _load(name):
    now = time.time()
    hit = _cache.get(name)
    if hit and now - hit[0] < CACHE_S:
        return hit[1]
    data = None
    try:
        with open(os.path.join(ALLOCATOR_DIR, name), "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = None
    _cache[name] = (now, data)
    return data


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _status(f, now):
    """ok | stale | unavailable for one context field, re-graded against the clock (like the collector does)."""
    if not isinstance(f, dict) or f.get("value") is None or f.get("status") == "unavailable":
        return "unavailable"
    ts = _ts(f.get("ts"))
    if ts is None:
        return "unavailable"
    age = (now - ts).total_seconds()
    if age > DROP_S:
        return "unavailable"
    if f.get("status") == "stale" or age > STALE_S:
        return "stale"
    return "ok"


def _pct(x, digits=1):
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def _fmt_coin(coin, d, now):
    if not isinstance(d, dict):
        return f"- {coin}: not in the snapshot"
    parts = []
    fr = d.get("funding_rate")
    if fr is not None:
        st = _status(fr, now)
        parts.append(f"funding {fr['value'] * 100:+.4f}% [{st}]" if st != "unavailable" else "funding unavailable")
    fp = d.get("funding_pctile")
    if fp is not None and _status(fp, now) != "unavailable" and fp["value"].get("percentile") is not None:
        parts.append(f"funding percentile {fp['value']['percentile']:.0f} of 30d [{_status(fp, now)}]")
    oi = [(k, d.get(f"oi_change_{k}")) for k in ("1h", "4h", "24h")]
    oi_txt = " ".join(f"{k} {_pct(f['value'])}" for k, f in oi if f is not None and _status(f, now) != "unavailable")
    if oi_txt:
        worst = "stale" if any(_status(f, now) == "stale" for _, f in oi if f is not None) else "ok"
        parts.append(f"OI {oi_txt} [{worst}]")
    else:
        parts.append("OI unavailable")
    ls = d.get("long_short_ratio")
    if ls is not None and _status(ls, now) != "unavailable":
        parts.append(f"long/short accounts {ls['value']['long']:.2f}/{ls['value']['short']:.2f} [{_status(ls, now)}]")
    flags = d.get("flags", {})
    for name in ("funding_extreme", "oi_spike"):
        f = flags.get(name)
        st = _status(f, now) if f is not None else "unavailable"
        parts.append(f"{name}={'yes' if f and f['value'] else 'no'} [{st}]" if st != "unavailable" else f"{name}=unknown")
    ns = d.get("news_status")
    news = d.get("news")
    if ns == "news_found" and news is not None and _status(news, now) != "unavailable":
        parts.append(f"news: news_found, {news['value']['count_24h']} headlines in 24h [{_status(news, now)}]")
    elif ns == "no_news":
        parts.append("news: no_news (the feed answered and found nothing for this coin)")
    elif ns == "feed_down":
        parts.append("news: feed_down (fetch failed -- do NOT read this as 'no news')")
    return f"- {coin}: " + "; ".join(parts)


def _snapshot():
    snap = _load(CONTEXT_FILE)
    return snap if isinstance(snap, dict) and snap.get("schema", "").startswith("p20-context") else None


def context_block(pair, bot_id):
    """'' for any bot that is not an LLM prompt bot (control bots and rule bots get an empty block)."""
    if bot_id not in CONTEXT_BOTS:
        return ""
    snap = _snapshot()
    if snap is None:
        return "Derivatives & macro context: unavailable (no snapshot from the collector) -- unknown, not neutral\n"
    now = datetime.now(timezone.utc)
    g, fl, coins = snap.get("global", {}), snap.get("flags", {}), snap.get("coins", {})
    coin = str(pair).split("/")[0].upper()
    lines = [f"Derivatives & macro context (snapshot {snap.get('ts')}; a field marked stale or unavailable is UNKNOWN, not neutral):"]
    if coin != "BTC":
        lines.append(_fmt_coin("BTC", coins.get("BTC"), now))
    lines.append(_fmt_coin(coin, coins.get(coin), now))
    mf = fl.get("macro_event_within_24h")
    mst = _status(mf, now) if mf is not None else "unavailable"
    if mst == "unavailable":
        lines.append("- macro: calendar unavailable -- macro_event_within_24h unknown")
    else:
        ev = (g.get("macro_events") or {}).get("value") or []
        upcoming = [e for e in ev if _ts(e.get("ts")) and _ts(e["ts"]) >= now]
        nxt = f"; next: {upcoming[0]['name']} in {(_ts(upcoming[0]['ts']) - now).total_seconds() / 3600:.0f}h" if upcoming else ""
        lines.append(f"- macro: macro_event_within_24h={'yes' if mf['value'] else 'no'} [{mst}]{nxt}")
    rg = g.get("btc_regime")
    if rg is not None and _status(rg, now) != "unavailable":
        lines.append(f"- BTC regime: {rg['value']['regime']} [{_status(rg, now)}]")
    lines.append("- liquidations: unavailable; ETF flows and on-chain: not connected")
    return "\n".join(lines) + "\n"


def fear_greed_text():
    """The shared Fear & Greed line, or None when the snapshot's value is not fresh (caller then fetches itself)."""
    snap = _snapshot()
    f = (snap or {}).get("global", {}).get("fear_greed")
    if f is None or _status(f, datetime.now(timezone.utc)) != "ok":
        return None
    return f"Fear & Greed Index: {f['value']['value']} ({f['value']['classification']})"


def market_text():
    """Shared 'Broader market context' line (total cap change + BTC dominance), or None when not fresh."""
    snap = _snapshot()
    g = (snap or {}).get("global", {})
    now = datetime.now(timezone.utc)
    d, c = g.get("btc_dominance"), g.get("total_mcap_change_24h")
    if d is None or c is None or _status(d, now) != "ok" or _status(c, now) != "ok":
        return None
    return f"Total crypto market cap 24h change: {c['value']:.2f}%. BTC dominance: {d['value']:.1f}%."


def stake_multiplier(bot_id):
    """1.0 unless the allocator is in enforce mode with a fresh, valid weight for this bot. 0..1 scales a stake down (0 = paused), 1..2 scales it UP (grow policy: a bot that earns money)."""
    w = _load(WEIGHTS_FILE)
    try:
        if not isinstance(w, dict) or w.get("mode") != "enforce":
            return 1.0
        ts = _ts(w.get("updated_at"))
        if ts is None or (datetime.now(timezone.utc) - ts).total_seconds() > WEIGHTS_MAX_AGE_S:
            return 1.0
        v = w["weights"][bot_id]["weight"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not (0.0 <= v <= MAX_STAKE_MULT):
            return 1.0
        return float(v)
    except (KeyError, TypeError):
        return 1.0


# ---------------------------------------------------------------------------------------------------------------------------
# P23 -- Kronos as a decision source and the LLM provider switch (read-only, fail-safe: no file / stale / bad value = the bot
# behaves exactly as it did before: LLM decides, DeepSeek is the provider).
#
#   <BOT>_DECISION_SOURCE   llm (default) | kronos_llm | kronos
#       llm         the LLM decides, as before
#       kronos_llm  Kronos proposes a direction, the LLM may only CONFIRM it or VETO it (code enforces: any other answer = hold)
#       kronos      Kronos alone decides, no LLM call at all
#   <BOT>_LLM_PROVIDER      deepseek (default) | free | none
#       deepseek    DeepSeek first, OpenRouter as the fallback (as before)
#       free        OpenRouter free models ONLY (never a paid model, never DeepSeek)
#       none        the LLM is never called. With source `llm` the bot is intentionally idle (hold everywhere).
#   kronos_rank.json (published hourly by survival-agent from the kronos-signal container): per bot and pair the cross-sectional
#   relative-strength rank of the 24h forecast. A signal older than KRONOS_MAX_AGE_S, or a pair without one, is "no signal" = hold.
# ---------------------------------------------------------------------------------------------------------------------------
KRONOS_FILE = "kronos_rank.json"
KRONOS_MAX_AGE_S = 2 * 3600 + 900
SOURCES = ("llm", "kronos_llm", "kronos")
PROVIDERS = ("deepseek", "free", "none")
_warned = set()


def _env_choice(name, allowed, default):
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in allowed:
        return raw
    if name not in _warned:
        _warned.add(name)
        log.warning("%s=%r is not one of %s -- using %r", name, raw, allowed, default)
    return default


def decision_config(bot_id):
    prefix = str(bot_id).upper()
    return {"source": _env_choice(f"{prefix}_DECISION_SOURCE", SOURCES, "llm"), "provider": _env_choice(f"{prefix}_LLM_PROVIDER", PROVIDERS, "deepseek")}


def kronos_proposal(pair, bot_id):
    """{'action': buy|sell_short|hold, 'score', 'rank', 'n', 'reason'} for this pair, or None when there is no fresh signal."""
    try:
        data = _load(KRONOS_FILE)
        if not isinstance(data, dict):
            return None
        t0 = _ts(data.get("t0"))
        if t0 is None or (datetime.now(timezone.utc) - t0).total_seconds() > KRONOS_MAX_AGE_S:
            return None
        e = (data.get("bots") or {}).get(bot_id, {}).get(pair)
        if not isinstance(e, dict) or e.get("action") not in ("buy", "sell_short", "hold"):
            return None
        reason = e.get("reason") or f"24h relative strength {float(e['score']):+.2%}, rank {int(e['rank'])} of {int(e['n'])}"
        return {"action": e["action"], "score": e.get("score"), "rank": e.get("rank"), "n": e.get("n"), "reason": str(reason)[:160]}
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------------------
# P25 announcement guard: blocked_pairs.json is published by survival-agent (backend/engine/announcements.py) from the exchange's delisting notices.
# A coin with a notice in force gets no NEW entry from the managed bots; exits, stops and open trades are never touched. A missing or stale file blocks nothing (fail-open: a broken
# feed must not stop trading).
# ---------------------------------------------------------------------------------------------------------------------------
BLOCKED_FILE = "blocked_pairs.json"
BLOCKED_MAX_AGE_S = 6 * 3600


def entry_block(pair):
    """A reason string when new entries on this pair's coin are blocked by an exchange announcement, else None."""
    try:
        data = _load(BLOCKED_FILE)
        if not isinstance(data, dict):
            return None
        now = datetime.now(timezone.utc)
        upd = _ts(data.get("updated"))
        if upd is None or (now - upd).total_seconds() > BLOCKED_MAX_AGE_S:
            return None
        coin = str(pair).split("/")[0].upper()
        e = (data.get("coins") or {}).get(coin)
        if not isinstance(e, dict):
            return None
        t_from, t_until = _ts(e.get("from")), _ts(e.get("until"))
        if t_from is None or t_until is None or not (t_from <= now <= t_until):
            return None
        return (coin + ": " + str(e.get("reason") or "exchange notice"))[:200]
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------------------------------
# P26 bot parameters: bot_params.json is published by survival-agent (backend/engine/bot_params.py) from the dashboard switches / environment. A missing, stale (> 6 h) or malformed file
# or value returns the caller's default, so a broken publisher can never change a bot's behaviour.
# ---------------------------------------------------------------------------------------------------------------------------
BOT_PARAMS_FILE = "bot_params.json"
BOT_PARAMS_MAX_AGE_S = 6 * 3600


def bot_param(bot_id, name, default):
    """A number published for this bot, else `default`."""
    try:
        data = _load(BOT_PARAMS_FILE)
        if not isinstance(data, dict):
            return default
        upd = _ts(data.get("updated"))
        if upd is None or (datetime.now(timezone.utc) - upd).total_seconds() > BOT_PARAMS_MAX_AGE_S:
            return default
        v = (data.get(bot_id) or {}).get(name)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return default
        return float(v)
    except (TypeError, ValueError, AttributeError):
        return default
