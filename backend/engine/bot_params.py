"""
P26 -- bot parameters that can be changed from the dashboard without restarting a bot.

Today one: the Listing Fade re-entry cooldown (hours a pair stays blocked after a stop-loss; 0 = off). The value comes from the same place as every other switch (dashboard value >
environment variable > built-in default, see runtime_settings.py) and is published as `bot_params.json` on the shared allocator volume. The bots read it through
`_allocator_ctx.bot_param(...)`; a missing, stale (older than 6 hours) or malformed file means the bot uses its own environment default. Nothing here touches stakes, stops or ROI.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import runtime_settings as rs

FILE = "bot_params.json"
SCHEMA = "p26-bot-params"
MAX_AGE_S = 6 * 3600
FADE_DEFAULT_H = 24.0


def _hours(raw, default: float = FADE_DEFAULT_H) -> float:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if 0.0 <= v <= 168.0 else default


def values(env: dict | None = None) -> dict:
    env = rs.merged_env() if env is None else env
    return {"listing_fade": {"reentry_cooldown_h": _hours(env.get("FADE_REENTRY_COOLDOWN_H", FADE_DEFAULT_H))}}


def publish(shared: Path | None, now: datetime | None = None, env: dict | None = None) -> dict:
    """Writes bot_params.json (atomic). Never raises: a failure just leaves the old file to go stale, and the bots fall back to their environment default."""
    vals = values(env)
    if shared is None or not Path(shared).is_dir():
        return {"ok": False, "reason": "no shared allocator volume", "values": vals}
    now = now or datetime.now(timezone.utc)
    body = {"schema": SCHEMA, "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), **vals}
    tmp = Path(shared) / (FILE + f".{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(body, indent=1), encoding="utf-8")
        os.replace(tmp, Path(shared) / FILE)
    except OSError as e:
        return {"ok": False, "reason": f"{type(e).__name__}: {e}", "values": vals}
    return {"ok": True, "values": vals}


def status(shared: Path | None, now: datetime | None = None, env: dict | None = None) -> dict:
    """What the dashboard shows: the wanted values, what the file says and how old it is."""
    now = now or datetime.now(timezone.utc)
    out = {"wanted": values(env), "file": None, "age_s": None, "fresh": False}
    try:
        d = json.loads((Path(shared) / FILE).read_text(encoding="utf-8"))
        upd = datetime.fromisoformat(str(d.get("updated")).replace("Z", "+00:00"))
        age = (now - upd).total_seconds()
        out.update({"file": {k: v for k, v in d.items() if k not in ("schema", "updated")}, "age_s": int(age), "fresh": age <= MAX_AGE_S})
    except (OSError, ValueError, TypeError):
        pass
    return out
