"""
P20.2 -- runtime switches for the P19/P20 features, changeable from the dashboard without a redeploy.

Precedence: value saved from the UI (`runtime_settings.json` in DATA_DIR)  >  environment variable (Portainer/compose)  >  built-in
default. A UI value can be reset (value null) to fall back to the environment. Every change is appended to
`runtime_settings_log.jsonl` (when, key, old, new, source); nothing here is ever deleted.

Only the keys listed in SPEC can be set, each with a strict validator. Bots, thresholds, stops, ROI and prompts are not
configurable from here, and neither are credentials. The allocator can never manage a control bot or trend_ensemble: the
per-bot exclusion list only accepts managed bots.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

SETTINGS_FILE = "runtime_settings.json"
LOG_FILE = "runtime_settings_log.jsonl"

MANAGED = ["main", "shorts", "moonshot", "listing_fade", "trend_v2"]


def _choice(*opts):
    return lambda v: str(v).strip().lower() if str(v).strip().lower() in opts else None


def _days(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return str(f) if 1 <= f <= 90 else None


def _bots(v):
    items = [x.strip() for x in str(v).split(",") if x.strip()] if not isinstance(v, list) else [str(x).strip() for x in v if str(x).strip()]
    if any(b not in MANAGED for b in items):
        return None
    return ",".join(sorted(set(items)))


def _frozen(v):
    parts = [p.strip() for p in str(v).split(",") if p.strip()]
    out = []
    for p in parts:
        bot, _, amount = p.partition(":")
        try:
            val = float(amount)
        except ValueError:
            return None
        if bot.strip() not in MANAGED or val < 0:
            return None
        out.append(f"{bot.strip()}:{val:g}")
    return ",".join(out)


SPEC = {
    "PORTFOLIO_BRAIN_MODE": {"default": "shadow", "options": ["off", "shadow", "enforce"], "validate": _choice("off", "shadow", "enforce"),
                             "label": "Allocator mode (off / shadow / enforce)"},
    "ALLOCATOR_POLICY": {"default": "grow", "options": ["grow", "protect"], "validate": _choice("grow", "protect"),
                         "label": "Allocator policy (grow = raise winners, stop losers, never idle the book; protect = the original loss rules)"},
    "RANKER_MODE": {"default": "off", "options": ["off", "shadow"], "validate": _choice("off", "shadow"),
                    "label": "LightGBM ranker forward test (paper only; the first start downloads the Binance history, about 0.5 GB and 1.5 h)"},
    "ALLOCATOR_BREAKER": {"default": "on", "options": ["on", "off"], "validate": _choice("on", "off"),
                          "label": "Circuit breaker: stop a bot that loses fast, before its 30-trade verdict (grow policy)"},
    "ALLOCATOR_BREAKER_STAKES": {"default": "1.25", "options": ["0.75", "1", "1.25", "1.5", "2", "3"], "validate": _choice("0.75", "1", "1.25", "1.5", "2", "3"),
                                 "label": "Breaker: stop when the bot lost more than this many average stakes"},
    "ALLOCATOR_BREAKER_STREAK": {"default": "8", "options": ["0", "6", "8", "10", "12"], "validate": _choice("0", "6", "8", "10", "12"),
                                 "label": "Breaker: stop after this many losing trades in a row (0 = off)"},
    "FADE_REENTRY_COOLDOWN_H": {"default": "24", "options": ["0", "6", "12", "24", "48"], "validate": _choice("0", "6", "12", "24", "48"),
                                "label": "Listing Fade: hours a pair stays blocked after a stop-loss (0 = off); reaches the bot within ~10 minutes"},
    "MARKET_CONTEXT_MODE": {"default": "on", "options": ["on", "off"], "validate": _choice("on", "off"), "label": "Market-context collector"},
    "SIGNAL_AUDIT_MODE": {"default": "on", "options": ["on", "off"], "validate": _choice("on", "off"), "label": "Signal audit (nightly)"},
    "ALLOCATOR_PROBE": {"default": "on", "options": ["on", "off"], "validate": _choice("on", "off"), "label": "Probe weight 0.1 after 7 days at 0"},
    "ALLOCATOR_PROBE_DAYS": {"default": "7", "options": None, "validate": _days, "label": "Days at weight 0 before the probe"},
    "ALLOCATOR_RETIRED_BOTS": {"default": "", "options": None, "validate": _bots,
                               "label": "Retired bots (stopped for good: not polled, equity frozen, no weight)"},
    "ALLOCATOR_RETIRED_FROZEN": {"default": "", "options": None, "validate": _frozen,
                                 "label": "Frozen balance override for retired bots, e.g. botname:412.35"},
    "ALLOCATOR_EXCLUDED_BOTS": {"default": "", "options": None, "validate": _bots, "label": "Bots the allocator must leave alone"},
}


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def load_overrides(d: Path | None = None) -> dict:
    try:
        raw = json.loads(((d or data_dir()) / SETTINGS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: str(v) for k, v in raw.items() if k in SPEC and isinstance(v, (str, int, float)) and SPEC[k]["validate"](v) is not None} \
        if isinstance(raw, dict) else {}


def merged_env(d: Path | None = None) -> dict:
    """os.environ with the UI overrides applied -- what the feature modules should read."""
    env = dict(os.environ)
    env.update(load_overrides(d))
    return env


def describe(d: Path | None = None) -> dict:
    ov = load_overrides(d)
    out = {}
    for k, s in SPEC.items():
        if k in ov:
            val, src = ov[k], "ui"
        elif k in os.environ and os.environ[k] != "":
            val, src = os.environ[k], "env"
        else:
            val, src = s["default"], "default"
        out[k] = {"value": val, "source": src, "default": s["default"], "options": s["options"], "label": s["label"]}
    return out


def save(patch: dict, d: Path | None = None, source: str = "ui", now: datetime | None = None) -> dict:
    """patch: {key: value | None}. None resets the key to the environment. Raises ValueError on an unknown key/invalid value."""
    d = d or data_dir()
    now = now or datetime.now(timezone.utc)
    cur = load_overrides(d)
    new = dict(cur)
    changes = []
    for k, v in patch.items():
        if k not in SPEC:
            raise ValueError(f"unknown setting {k!r}")
        old = describe(d)[k]["value"]
        if v is None:
            new.pop(k, None)
        else:
            ok = SPEC[k]["validate"](v)
            if ok is None:
                raise ValueError(f"invalid value {v!r} for {k}")
            new[k] = ok
        changes.append({"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "key": k, "old": old, "new": new.get(k, "(reset to env/default)"), "source": source})
    tmp = d / (SETTINGS_FILE + ".tmp")
    tmp.write_text(json.dumps(new, indent=1), encoding="utf-8")
    os.replace(tmp, d / SETTINGS_FILE)
    with open(d / LOG_FILE, "a", encoding="utf-8") as f:
        for c in changes:
            f.write(json.dumps(c) + "\n")
    return describe(d)
