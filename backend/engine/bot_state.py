"""
P20.4 -- bot roster state shared by the portfolio brain and the allocator.

Three things that used to make the portfolio maths lie when a bot went away (the 02.10 Dip stop):

1. RETIRED bots (`ALLOCATOR_RETIRED_BOTS`, also a dashboard switch). A retired bot is not polled at all (no REST call), is never "missing", has no
   weight and no dry_run check. Its contribution to equity is FROZEN at the last known value (a constant): the balance in the last brain snapshot that
   still contained it, or an operator override (`ALLOCATOR_RETIRED_FROZEN="botname:412.35"`, e.g. the final balance from the bot's /balance aggregate).
   Equity, HWM and the reference capital therefore stay comparable with the days before the stop, and the floor is not hit by a bot that merely left.
   If no value can be frozen, the bot is dropped from the reference capital instead (the fallback) and the report says so.
   Its history (evidence status, ledger signals, trades in earlier reports) stays visible with a `retired` mark: the evidence entry is copied from the
   last report that had it.
2. STALE / UNREACHABLE bots. A bot that does not answer one run keeps counting with its last known equity and open trades (cached on every successful
   poll): up to 24h it is `stale_bots`, older it is `unreachable_bots` (equity still kept so it is not read as a drawdown; its exposure is dropped because
   it is unknown). Only a bot that has never answered makes the picture `partial`.
3. `partial` is therefore rare, and when it happens the equity-based rules (ladder, weekly/monthly loss, CPPI floor) only warn.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import portfolio_brain as pb
from . import runtime_settings as rs

RETIRED_FILE = "retired_bots.json"
CACHE_FILE = "bot_last_known.json"
STALE_AFTER = timedelta(hours=24)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- retired
def retired_from_env(env: dict | None = None) -> list[str]:
    env = rs.merged_env() if env is None else env
    raw = str(env.get("ALLOCATOR_RETIRED_BOTS", ""))
    return [b for b in (x.strip() for x in raw.split(",")) if b in rs.MANAGED]


def frozen_overrides(env: dict | None = None) -> dict[str, float]:
    env = rs.merged_env() if env is None else env
    out: dict[str, float] = {}
    for part in str(env.get("ALLOCATOR_RETIRED_FROZEN", "")).split(","):
        if ":" not in part:
            continue
        bot, _, amount = part.partition(":")
        try:
            out[bot.strip()] = float(amount)
        except ValueError:
            continue
    return out


def freeze_retired(data_dir: Path, retired: list[str], now: datetime, env: dict | None = None) -> dict[str, dict]:
    """Make sure every retired bot has a freeze record (written once, updated only by an explicit override).
    Returns {bot: record} for the retired bots."""
    data_dir = Path(data_dir)
    book = _load(data_dir / RETIRED_FILE, {})
    overrides = frozen_overrides(env)
    snaps = pb.load_snapshots(data_dir)
    brain = _load(data_dir / pb.LATEST_FILE, {})
    changed = False
    for bot in retired:
        rec = book.get(bot)
        want_override = overrides.get(bot)
        if rec is not None and (want_override is None or rec.get("frozen_equity") == want_override):
            continue
        equity, source = None, "unknown"
        if want_override is not None:
            equity, source = want_override, "operator override (ALLOCATOR_RETIRED_FROZEN)"
        else:
            for s in reversed(snaps):
                v = (s.get("bots") or {}).get(bot)
                if isinstance(v, (int, float)):
                    equity, source = float(v), f"last brain snapshot that still had it ({s.get('ts')})"
                    break
        book[bot] = {
            "frozen_equity": equity, "source": source, "frozen_at": _iso(now),
            "start_capital": pb.DEFAULT_START_CAPITAL.get(bot, 0.0),
            "method": "frozen_constant" if equity is not None else "dropped_from_reference",
            "evidence": {**(brain.get("evidence", {}).get(bot) or (rec or {}).get("evidence") or {"status": "no_data"})},
        }
        changed = True
    if changed:
        _save(data_dir / RETIRED_FILE, book)
    return {b: book[b] for b in retired if b in book}


def contribution(frozen: dict[str, dict]) -> tuple[float, float]:
    """(equity added, start capital dropped from the reference). A frozen bot adds its constant to equity and stays in the reference;
    a bot whose value could not be frozen adds nothing and is dropped from the reference (the documented fallback)."""
    eq = sum(float(r["frozen_equity"]) for r in frozen.values() if r.get("frozen_equity") is not None)
    dropped = sum(float(r.get("start_capital") or 0.0) for r in frozen.values() if r.get("frozen_equity") is None)
    return eq, dropped


# ------------------------------------------------------------------ cache
def _trim(trades: list[dict]) -> list[dict]:
    keep = ("pair", "base_currency", "stake_amount", "leverage", "is_short", "open_date", "trade_id")
    return [{k: t.get(k) for k in keep if k in t} for t in trades or []]


def collect(fetcher, data_dir: Path, now: datetime, bots: list[str], retired: list[str], trade_bots: list[str] | None = None) -> dict:
    """Poll `bots` (retired ones are skipped: no REST call). A bot that does not answer falls back to its cached last-known equity / open trades."""
    data_dir = Path(data_dir)
    cache = _load(data_dir / CACHE_FILE, {})
    out = {"equities": {}, "balances": {}, "open_trades": {}, "trades": {}, "missing": [], "stale": [], "unreachable": []}
    for bot in bots:
        if bot in retired:
            continue
        live = False
        if fetcher.reachable(bot):
            bal = fetcher.balance(bot)
            eq = pb._equity_of(bal)
            if eq is not None:
                live = True
                out["balances"][bot], out["equities"][bot] = bal, eq
                out["open_trades"][bot] = fetcher.status(bot) or []
                if trade_bots is None or bot in trade_bots:
                    out["trades"][bot] = fetcher.trades(bot) or []
                cache[bot] = {"equity": eq, "ts": _iso(now), "open_trades": _trim(out["open_trades"][bot])}
        if live:
            continue
        ent = cache.get(bot)
        ts = pb._parse_dt((ent or {}).get("ts"))
        if ent is None or ts is None:
            out["missing"].append(bot)
            continue
        out["equities"][bot] = float(ent["equity"])            # kept either way: a silent bot is not a drawdown
        if now - ts <= STALE_AFTER:
            out["stale"].append(bot)
            out["open_trades"][bot] = ent.get("open_trades", [])
        else:
            out["unreachable"].append(bot)                     # exposure unknown, so it is not counted
    _save(data_dir / CACHE_FILE, cache)
    return out
