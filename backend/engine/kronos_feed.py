"""
P23 -- publishes the per-bot Kronos rank file to the bots.

The kronos-signal container writes `kronos_rank.json` into its own volume (/kronos, mounted READ-ONLY here). The bots may not mount that volume (they never
get write or read access to anything of the Kronos container); they already mount the allocator's shared volume read-only, so this module copies the file
there (as `kronos_rank.json`, atomically), after checking that it is a well-formed, recent rank file. Bots read it through `_allocator_ctx.kronos_proposal`.

Nothing here decides anything and nothing here talks to a bot. A missing / old / malformed file simply leaves the previous published copy (the bots treat
anything older than ~2h as "no signal" and hold), it never deletes it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("survival-agent.kronos_feed")

RANK_FILE = "kronos_rank.json"
MAX_SOURCE_AGE_S = 6 * 3600
VALID_ACTIONS = ("buy", "sell_short", "hold")


def _parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def validate(data, now: datetime) -> str | None:
    """None when the rank file is usable, else the reason it is not."""
    if not isinstance(data, dict) or not isinstance(data.get("bots"), dict):
        return "not a rank file (no `bots`)"
    t0 = _parse(data.get("t0"))
    if t0 is None:
        return "no valid t0"
    if (now - t0).total_seconds() > MAX_SOURCE_AGE_S:
        return f"older than {MAX_SOURCE_AGE_S // 3600} h (the Kronos cycle is not producing)"
    for bot, view in data["bots"].items():
        if not isinstance(view, dict):
            return f"bot {bot!r}: view is not an object"
        for pair, e in view.items():
            if not isinstance(e, dict) or e.get("action") not in VALID_ACTIONS:
                return f"bot {bot!r} pair {pair!r}: bad action"
    return None


def publish(kronos_dir: Path | None = None, allocator_dir: Path | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    src = Path(kronos_dir or os.environ.get("KRONOS_DIR") or "/kronos") / RANK_FILE
    dst = Path(allocator_dir or os.environ.get("ALLOCATOR_DIR") or "/allocator") / RANK_FILE
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return {"published": False, "reason": f"no readable rank file yet ({type(e).__name__})"}
    why = validate(data, now)
    if why:
        return {"published": False, "reason": why, "t0": data.get("t0") if isinstance(data, dict) else None}
    try:
        cur = json.loads(dst.read_text(encoding="utf-8"))
        if isinstance(cur, dict) and cur.get("t0") == data["t0"] and cur.get("written_at") == data.get("written_at"):
            return {"published": False, "reason": "already published", "t0": data["t0"], "unchanged": True}
    except (OSError, ValueError):
        pass
    out = {**data, "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, dst)
    return {"published": True, "t0": data["t0"], "bots": {b: {a: sum(1 for e in v.values() if e["action"] == a) for a in VALID_ACTIONS} for b, v in data["bots"].items() if v}}


def status(allocator_dir: Path | None = None, now: datetime | None = None) -> dict:
    """What the bots currently see: the published file's age and how many pairs of each bot are buy / sell_short / hold."""
    now = now or datetime.now(timezone.utc)
    dst = Path(allocator_dir or os.environ.get("ALLOCATOR_DIR") or "/allocator") / RANK_FILE
    try:
        d = json.loads(dst.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "nothing published yet (the kronos-signal container writes the first file after its first hourly cycle)"}
    t0 = _parse(d.get("t0"))
    age = None if t0 is None else int((now - t0).total_seconds())
    return {"available": True, "t0": d.get("t0"), "age_seconds": age, "fresh_for_bots": age is not None and age <= 2 * 3600 + 900, "published_at": d.get("published_at"), "design": d.get("design"),
            "n_scored": d.get("n_scored"), "bots": {b: {a: sum(1 for e in v.values() if e.get("action") == a) for a in VALID_ACTIONS} for b, v in (d.get("bots") or {}).items() if v}}
