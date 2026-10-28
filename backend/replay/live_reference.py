"""
live_reference.json for the replay's "divergence from the live bots" check: per bot, how many CLOSED trades the live bot closed inside a period and their mean
profit ratio. The replay run itself never reads a live bot; this file is produced either

  * by the button "Refresh live reference" on /replay -- the endpoint (in app.py, outside this package) takes the trades from the SAME code path the P18 export
    uses (backend.export.iter_trades + the export's trade-row normalisation) and hands the rows to build_from_rows(); or
  * offline, from a P18 export ZIP:  python -m backend.replay.live_reference <export.zip> [--from 2026-09-21] [--to 2026-10-04]

Either way the file lands in REPLAY_DIR/live_reference.json (inside the replay sandbox).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from . import live_data, paths

DEFAULT_FROM = "2026-09-21"


def _utc(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def summarize(rows: list[dict], date_from: str, date_to: str | None) -> dict:
    """rows: trade dicts (the export's trade rows, or live_data.norm_trade output). Closed trades whose close time is inside [from, to] (the `to` day counts whole)
    give the count, mean and win rate, the exit-reason mix, the mean hold time and the fees; `open_at_end` counts trades that were open at the end of the window."""
    lo = _utc(date_from)
    hi_day = _utc(date_to) + timedelta(days=1) if date_to else None
    sel, reasons, holds, fees = [], {}, [], 0.0
    closed_dates = []
    for r in rows:
        if r.get("is_open") in (True, "True", "true", 1, "1") or not r.get("close_date") or r.get("profit_ratio") in (None, ""):
            continue
        closed_dates.append(str(r["close_date"]))
        d = _utc(r["close_date"])
        try:
            pr = float(r["profit_ratio"])
        except (TypeError, ValueError):
            continue
        if d is None or d < lo or (hi_day is not None and d > hi_day):
            continue
        sel.append(pr)
        reasons[r.get("exit_reason") or "?"] = reasons.get(r.get("exit_reason") or "?", 0) + 1
        o = _utc(r.get("open_date")) if r.get("open_date") else None
        if o is not None:
            holds.append((d - o).total_seconds() / 3600)
        try:
            fees += float(r.get("fees_abs") or 0.0)
        except (TypeError, ValueError):
            pass
    hi = date_to or max(closed_dates, default=date_from)[:10]
    open_end = 0
    for r in rows:
        o = _utc(r.get("open_date")) if r.get("open_date") else None
        c = _utc(r["close_date"]) if r.get("close_date") else None
        is_open = r.get("is_open") in (True, "True", "true", 1, "1") or c is None
        if hi_day is None:                       # no upper bound given: what is open right now
            open_end += 1 if is_open else 0
        elif o is not None and o <= hi_day and (is_open or c > hi_day):
            open_end += 1
    n = len(sel)
    return {"from": date_from, "to": hi, "n_trades": n, "mean_trade_pct": round(sum(sel) / n * 100, 4) if n else None,
            "win_pct": round(sum(1 for x in sel if x > 0) / n * 100, 1) if n else None, "exit_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
            "avg_hold_hours": round(sum(holds) / len(holds), 2) if holds else None, "open_at_end": open_end, "fees_usd": round(fees, 3)}


def build_from_rows(rows_by_bot: dict[str, list[dict]], date_from: str = DEFAULT_FROM, date_to: str | None = None) -> dict:
    return {bot: summarize(rows, date_from, date_to) for bot, rows in rows_by_bot.items()}


def build(zip_path: str, date_from: str, date_to: str | None) -> tuple[dict, dict]:
    """(reference, snapshot) from a P18 export ZIP."""
    snap = live_data.snapshot_from_zip(zip_path)
    ref = build_from_rows({bot: e["trades"] for bot, e in snap.items() if e.get("trades_available")}, date_from, date_to)
    return ref, snap


def write(ref: dict, meta: dict | None = None):
    f = paths.ensure_parent(paths.root_file("live_reference.json"))
    payload = dict(ref)
    if meta:
        payload["_meta"] = meta
    f.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return f


def read() -> dict | None:
    try:
        return json.loads(paths.root_file("live_reference.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--from", dest="date_from", default=DEFAULT_FROM)
    ap.add_argument("--to", dest="date_to", default=None)
    a = ap.parse_args(argv)
    ref, snap = build(a.zip, a.date_from, a.date_to)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    f = write(ref, {"source": f"export zip {a.zip}", "updated_at": stamp, "from": a.date_from, "to": a.date_to})
    g = live_data.write_snapshot(snap, {"source": f"export zip {a.zip}", "updated_at": stamp})
    print(f"wrote {f}: {sorted(ref)}")
    print(f"wrote {g}: {sorted(snap)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
