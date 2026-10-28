"""
What the replay may know about the LIVE bots, in one place: their real decisions (the `DeepSeek said <action> for <pair> -- <reason>` log lines), their restarts,
their real trades. It is turned into a plain snapshot (REPLAY_DIR/live_snapshot.json) by an explicit operator action -- the "Update live reference" button on
/replay (the endpoint in app.py reads logs and trades with the P18 export's own code) or `python -m backend.replay.live_reference <export.zip>`. After that
the replay engine and the calibration read ONLY the snapshot inside the sandbox; they never open a live bot or a live file.

Honest limits of the source (all of them are printed by the calibration report):
  * the log of a bot starts where its log file was rotated or the container was recreated, so the decisions cover only that period;
  * a failed LLM call is logged as a "hold" (reason "no LLM response ...") -- an outage looks like a market-wide hold; `outage_hours` finds those hours;
  * the log holds the FINAL decision (after the strategy's own vetoes) but not the price, confidence (except Dip) or the full model answer.
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.engine import signal_audit as sa

from . import paths

SNAPSHOT_FILE = "live_snapshot.json"
RESTART_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - freqtrade\.worker - INFO - Changing state to: RUNNING")
MAX_OPEN_RE = re.compile(r"Strategy using max_open_trades: (\d+)")
CANCEL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - .*Sending rpc message: \{.*'type': (entry_cancel|exit_cancel),.*?'pair': '([^']+)'")
VETO_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - \S+ - INFO - confirm_trade_entry: rejecting (\S+) -- (.*)$")
SMALL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - freqtrade\.wallets - INFO - Stake amount for pair (\S+) is too small \(([\d.]+) < ([\d.]+)\)")
MINBAL_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - freqtrade\.wallets - WARNING - Minimum stake amount > available balance\. ([\d.]+) > ([\d.]+)")
LOCK_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - freqtrade\.freqtradebot - INFO - Pair (\S+) (?:long|short) is locked until (\S+ \S+) due to (.*?)\.?$")
FAIL_REASON = "no LLM response"
REASON_KEEP = 200
OUTAGE_SHARE = 0.9              # an hour is an outage hour when at least this share of its decisions are "no LLM response"


def utc(s) -> datetime | None:
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_hour(d: datetime) -> datetime:
    return d.replace(minute=0, second=0, microsecond=0)


# ------------------------------------------------------------------------ logs
def parse_log(lines) -> dict:
    """decisions (one per log line, in log order), restarts (the worker went RUNNING again), the max_open_trades the bot started with, the first / last log time."""
    decisions, restarts, orders = [], [], []
    blocks: list[dict] = []
    seen_blocks: set = set()
    max_open = None
    first_ts = last_ts = None
    for line in lines:
        if len(line) > 19 and line[:2] == "20":
            ts = line[:19]
            first_ts = first_ts or ts
            last_ts = ts
        if "DeepSeek said" in line:
            m = sa.DECISION_RE.match(line)
            if m:
                ts = utc(m.group(1))
                d = sa.parse_decision_line(line)
                decisions.append({"ts": iso(ts), "t0": iso(floor_hour(ts)), "pair": m.group(3), "action": d["action"], "reason": m.group(4)[:REASON_KEEP], "confidence": d["confidence"]})
        elif "confirm_trade_entry: rejecting" in line or "is too small (" in line or "Minimum stake amount > available balance" in line or "is locked until" in line:
            _block(line, blocks, seen_blocks)
        elif "_cancel," in line and "Sending rpc message" in line:
            m = CANCEL_RE.match(line)
            if m:
                orders.append({"ts": iso(utc(m.group(1))), "kind": m.group(2), "pair": m.group(3)})
        elif "Changing state to: RUNNING" in line:
            m = RESTART_RE.match(line)
            if m:
                restarts.append(iso(utc(m.group(1))))
        elif max_open is None or "Strategy using max_open_trades" in line:
            m = MAX_OPEN_RE.search(line)
            if m:
                max_open = int(m.group(1))
    return {"decisions": decisions, "restarts": restarts, "order_cancels": orders, "entry_blocks": blocks, "max_open_trades": max_open, "log_first_ts": first_ts, "log_last_ts": last_ts}


def _block(line: str, blocks: list[dict], seen: set) -> None:
    """Why Freqtrade did NOT open a trade that the strategy asked for, as the live log says it (the same line repeats every loop iteration: one record per kind, pair and hour is kept).
      veto       the strategy's own confirm_trade_entry refused (funding below the threshold, loss-streak cooldown)
      min_order  the exchange's minimum order size: the wanted stake is too small and the adjusted one would be more than 30% bigger (named pair, `min` = the minimum stake)
      min_balance  the minimum stake of a pair is more than the free balance (the log line does not name the pair: `min` is the figure, `avail` the free balance)
      locked     Freqtrade's pair lock (Auto lock after an exit, protections): no new entry on that pair until `until`"""
    m = VETO_RE.match(line)
    if m:
        kind, ts, pair, extra = "veto", utc(m.group(1)), m.group(2), {"why": m.group(3)[:80]}
    elif (m := SMALL_RE.match(line)):
        kind, ts, pair, extra = "min_order", utc(m.group(1)), m.group(2), {"min": float(m.group(4)), "wanted": float(m.group(3))}
    elif (m := MINBAL_RE.match(line)):
        kind, ts, pair, extra = "min_balance", utc(m.group(1)), None, {"min": float(m.group(2)), "avail": float(m.group(3))}
    elif (m := LOCK_RE.match(line)):
        kind, ts, pair, extra = "locked", utc(m.group(1)), m.group(2), {"until": m.group(3), "why": m.group(4)[:60]}
    else:
        return
    key = (kind, pair, iso(floor_hour(ts)), round(extra.get("min", 0), -1))
    if key in seen:
        return
    seen.add(key)
    blocks.append({"ts": iso(ts), "kind": kind, "pair": pair, **extra})


def outage_hours(decisions: list[dict]) -> list[str]:
    """Hours (ISO) in which nearly every decision is a failed LLM call logged as hold: the bot did not decide anything, it just held."""
    tot, bad = Counter(), Counter()
    for d in decisions:
        tot[d["t0"]] += 1
        if d["reason"].startswith(FAIL_REASON):
            bad[d["t0"]] += 1
    return sorted(h for h in tot if bad[h] / tot[h] >= OUTAGE_SHARE)


def entry_offsets(trades: list[dict]) -> list[float]:
    """Seconds after the full hour at which the live bot really OPENED each trade (from the trades themselves: the `signal found` log line repeats on every loop
    iteration while an entry is pending, so it is no measure of the entry time). The LLM calls of the whole hour come first, so entries trail the candle open."""
    out = []
    for t in trades:
        d = utc(t["open_date"]) if t.get("open_date") else None
        if d is not None:
            out.append((d - floor_hour(d)).total_seconds())
    return out


def delay_stats(trades: list[dict]) -> dict | None:
    offs = sorted(entry_offsets(trades))
    if not offs:
        return None
    return {"n": len(offs), "median": round(offs[len(offs) // 2], 1), "p25": round(offs[len(offs) // 4], 1), "p75": round(offs[(3 * len(offs)) // 4], 1)}


# ---------------------------------------------------------------------- trades
def _f(x):
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def _b(x) -> bool:
    return x is True or str(x).strip().lower() in ("true", "1")


def norm_trade(row: dict) -> dict:
    """A live trade from either the P18 export's rows (REST dicts or trades.csv strings) in one shape. Dates are ISO UTC."""
    is_open = _b(row.get("is_open"))
    od, cd = utc(row.get("open_date")), utc(row.get("close_date")) if row.get("close_date") else None
    fees = (_f(row.get("fee_open_cost")) or 0.0) + (_f(row.get("fee_close_cost")) or 0.0)
    pr = _f(row.get("profit_ratio"))
    if pr is None:
        pr = _f(row.get("close_profit"))
    return {"id": row.get("trade_id"), "pair": row.get("pair"), "side": "short" if _b(row.get("is_short")) else "long", "is_open": is_open,
            "open_date": iso(od) if od else None, "close_date": iso(cd) if cd else None, "open_rate": _f(row.get("open_rate")), "close_rate": _f(row.get("close_rate")),
            "stake": _f(row.get("stake_amount")), "profit_ratio": pr, "profit_abs": _f(row.get("profit_abs")), "fees_abs": round(fees, 6),
            "exit_reason": row.get("exit_reason") or None, "enter_tag": row.get("enter_tag") or ""}


def norm_trades(rows: list[dict]) -> list[dict]:
    out = [norm_trade(r) for r in rows]
    return sorted((t for t in out if t["open_date"] or t["close_date"]), key=lambda t: (t["open_date"] or "", t["pair"] or ""))


# -------------------------------------------------------------------- snapshot
def build_bot(lines, trade_rows: list[dict] | None) -> dict:
    if lines is not None and not isinstance(lines, list):
        lines = list(lines)
    parsed = parse_log(lines) if lines is not None else {"decisions": [], "restarts": [], "order_cancels": [], "max_open_trades": None, "log_first_ts": None, "log_last_ts": None}
    out = {**parsed, "trades": norm_trades(trade_rows or []), "trades_available": trade_rows is not None, "logs_available": lines is not None,
           "outage_hours": outage_hours(parsed["decisions"])}
    ds = delay_stats(out["trades"])
    if ds:
        out["entry_delay_s"] = ds
    return out


def snapshot_from_zip(zip_path: str, bots: list[str] | None = None) -> dict:
    """A P18 export ZIP (<bot>/logs.txt + <bot>/trades.csv) -> {bot: build_bot(...)}."""
    snap = {}
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        found = sorted({n.split("/")[0] for n in names if n.endswith("/logs.txt") or n.endswith("/trades.csv")})
        for bot in found:
            if bots and bot not in bots:
                continue
            ln, tn = f"{bot}/logs.txt", f"{bot}/trades.csv"
            lines = z.read(ln).decode("utf-8", "replace").splitlines() if ln in names else None
            rows = list(csv.DictReader(io.StringIO(z.read(tn).decode("utf-8")))) if tn in names else None
            snap[bot] = build_bot(lines, rows)
    return snap


def write_snapshot(snap: dict, meta: dict) -> Path:
    f = paths.ensure_parent(paths.root_file(SNAPSHOT_FILE))
    f.write_text(json.dumps({"_meta": meta, "bots": snap}, separators=(",", ":")), encoding="utf-8")
    return f


def read_snapshot() -> dict | None:
    try:
        d = json.loads(paths.root_file(SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and isinstance(d.get("bots"), dict) else None


# -------------------------------------------------------------------- coverage
def coverage(entry: dict) -> dict:
    """Which period the decisions of one bot really cover, how many hours inside it have any decision, the restarts, the outage hours."""
    decs = entry.get("decisions") or []
    if not decs:
        return {"available": False, "note": "no decisions in the logs"}
    hours = sorted({d["t0"] for d in decs})
    first, last = utc(hours[0]), utc(hours[-1])
    # the first hour is complete only when the bot was already running at its start (restarts and log rotation cut the hour)
    first_ts = utc(decs[0]["ts"])
    start = floor_hour(first_ts) if first_ts == floor_hour(first_ts) else floor_hour(first_ts) + timedelta(hours=1)
    span = int((last - start).total_seconds() // 3600) + 1 if last >= start else 0
    have = sum(1 for h in hours if utc(h) >= start)
    pairs = Counter(d["pair"] for d in decs)
    return {"available": True, "first_decision": decs[0]["ts"], "last_decision": decs[-1]["ts"], "n_decisions": len(decs), "first_full_hour": iso(start), "last_hour": hours[-1],
            "hours_in_span": span, "hours_with_decisions": have, "missing_hours": max(0, span - have), "n_pairs": len(pairs), "restarts": entry.get("restarts", []),
            "outage_hours": entry.get("outage_hours", []), "entry_orders_timed_out": sum(1 for o in entry.get("order_cancels", []) if o["kind"] == "entry_cancel"), "exit_orders_timed_out": sum(1 for o in entry.get("order_cancels", []) if o["kind"] == "exit_cancel"), "max_open_trades": entry.get("max_open_trades"), "trades_available": entry.get("trades_available"),
            "entry_delay_s": entry.get("entry_delay_s")}
