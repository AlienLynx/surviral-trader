"""
P18 -- export centre. Read-only downloads for analysis: bot logs, all-time trades,
and a streamed ZIP bundle, plus a catalog that shows the REAL coverage of every log
(lines, bytes, first/last timestamp) so nobody assumes 100k lines and receives 200.

Sources:
  * logs    -- each bot's own `freqtrade.log` (+ rotations) from its `*-db` volume, mounted
               READ-ONLY into this container at BOTS_DIR/<bot_id>/ (default /bots).
  * trades  -- the bot's REST API (`/api/v1/trades`, pages of 500), never its SQLite file.
  * brain   -- DATA_DIR/portfolio_brain_* (P17-A) and DATA_DIR/decisions.log.

Safety: `bot` must be in a fixed whitelist; no user-supplied path is ever opened (file
names are discovered by listing a whitelisted directory and matching a strict regex);
secrets are masked in every log line before it leaves; bundles are capped (300 MB
estimated) and at most 2 exports run at once; heavy work runs off the event loop.
Every route here is GET-only.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import re
import threading
import time as _time
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("survival-agent.export")
router = APIRouter(prefix="/api/export", tags=["export"])

BOT_IDS = [
    "main", "shorts", "moonshot",
    "listing_fade", "trend_ensemble", "trend_v2", "control_long", "control_short",
]
INCLUDE_OPTIONS = ("logs", "trades", "brain", "decisions", "aggregates", "signals", "allocator")
DEFAULT_INCLUDE = ",".join(INCLUDE_OPTIONS)
LOG_RE = re.compile(r"^freqtrade\.log(?:\.(\d+))?$")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3}")
BRAIN_RE = re.compile(r"^portfolio_brain_[A-Za-z0-9_.\-]+$")

MAX_LINES = 500_000
MAX_HOURS = 24 * 365
DEFAULT_LINES = 5_000
MAX_BUNDLE_BYTES = int(os.getenv("EXPORT_MAX_BUNDLE_BYTES", str(300 * 1024 * 1024)))
MAX_CONCURRENT = 2
TRADE_PAGE = 500

TRADE_COLUMNS = [
    "trade_id", "pair", "base_currency", "is_short", "leverage", "is_open",
    "open_date", "close_date", "open_rate", "close_rate", "amount", "stake_amount",
    "profit_ratio", "profit_abs", "profit_pct", "exit_reason", "enter_tag", "strategy", "timeframe",
    "fee_open", "fee_close", "fee_open_cost", "fee_close_cost", "funding_fees",
    "min_rate", "max_rate", "stop_loss_abs", "initial_stop_loss_abs", "trade_duration",
]

class _Slots:
    """At most MAX_CONCURRENT exports at once. Leases expire (a client that disconnects before its
    stream starts never runs the generator's cleanup), and release() is idempotent."""

    LEASE_SECONDS = 1800

    def __init__(self, limit: int):
        self.limit, self._leases, self._lock = limit, {}, threading.Lock()
        self._next = 0

    def acquire(self) -> int | None:
        now = _time.monotonic()
        with self._lock:
            self._leases = {k: v for k, v in self._leases.items() if now - v < self.LEASE_SECONDS}
            if len(self._leases) >= self.limit:
                return None
            self._next += 1
            self._leases[self._next] = now
            return self._next

    def release(self, token: int | None) -> None:
        with self._lock:
            self._leases.pop(token, None)

    def active(self) -> int:
        with self._lock:
            return len(self._leases)


# P18b: the FreqUI aggregate tabs, one JSON per bot and endpoint (GET, read-only, secrets masked).
AGG_ENDPOINTS = {
    "profit": "/api/v1/profit",
    "daily": "/api/v1/daily?timescale=365",
    "weekly": "/api/v1/weekly?timescale=104",
    "monthly": "/api/v1/monthly?timescale=24",
    "performance": "/api/v1/performance",
    "stats": "/api/v1/stats",
    "entries": "/api/v1/entries",
    "exits": "/api/v1/exits",
    "mix_tags": "/api/v1/mix_tags",
    "balance": "/api/v1/balance",
}
SIGNAL_FILES = ("signals.jsonl", "signals_forward.jsonl")
# P20: market context (A) and allocator decisions / counterfactual (B), text files masked like the logs.
ALLOCATOR_FILES = {
    "context/context_snapshots.jsonl": "context_snapshots.jsonl",
    "context/context_latest.json": "context_latest.json",
    "context/macro_events.json": "macro_events.json",
    "allocator/allocations.jsonl": "allocations.jsonl",
    "allocator/allocator_counterfactual.json": "allocator_counterfactual.json",
    "allocator/allocator_latest.json": "allocator_latest.json",
    "allocator/allocator_state.json": "allocator_state.json",
    "allocator/runtime_settings.json": "runtime_settings.json",
    "allocator/runtime_settings_log.jsonl": "runtime_settings_log.jsonl",
}

_slots = _Slots(MAX_CONCURRENT)
_count_cache: dict[tuple, dict] = {}
_count_lock = threading.Lock()


def _bots_dir() -> Path:
    return Path(os.getenv("BOTS_DIR", "/bots"))


def _data_dir() -> Path:
    return Path(os.getenv("DATA_DIR", "/data"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fetcher():
    from .engine import portfolio_brain
    return portfolio_brain.default_fetcher()


# ---------------------------------------------------------------- secret masking
_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]*")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{6,}")
_AUTH_HEADER = re.compile(r"(?i)(authorization\s*[:=]\s*)(?!bearer\b)(?:basic\s+)?[^\s\"',}]+")
_KV = re.compile(
    r"(?i)((?:x-)?(?:[a-z0-9_\-]*api[_\-]?key|[a-z0-9_\-]*secret[a-z0-9_\-]*|[a-z0-9_\-]*token|[a-z0-9_\-]*password|passwd)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',&}\]]+)")
_SK = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")
_PTR = re.compile(r"\bptr_[A-Za-z0-9+/=_\-]{16,}")
_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")


def mask_secrets(line: str) -> str:
    """Mask credentials in one log line. Idempotent; ordinary lines are returned unchanged."""
    if not line:
        return line
    s = _JWT.sub("***JWT***", line)
    s = _BEARER.sub(lambda m: f"{m.group(1)} ***", s)
    s = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}***", s)
    s = _KV.sub(lambda m: f"{m.group(1)}***", s)
    s = _SK.sub("sk-***", s)
    s = _PTR.sub("ptr_***", s)
    s = _HEX.sub("***HEX***", s)
    return s


# --------------------------------------------------------------- path / file helpers
def _validate_bot(bot: str) -> str:
    if bot not in BOT_IDS:      # exact whitelist: no traversal, no globbing, no odd characters
        raise HTTPException(400, f"unknown bot '{bot[:40]}', expected one of {BOT_IDS}")
    return bot


def _parse_bots(raw: str | None) -> list[str]:
    if not raw or raw == "all":
        return list(BOT_IDS)
    bots = [b.strip() for b in raw.split(",") if b.strip()]
    for b in bots:
        _validate_bot(b)
    return list(dict.fromkeys(bots))


def log_files(bot: str) -> list[Path]:
    """The bot's log + rotations, OLDEST FIRST (RotatingFileHandler: .log.10 is the oldest,
    plain .log the newest). Only regular, non-symlink files that match the strict pattern."""
    base = (_bots_dir() / _validate_bot(bot))
    try:
        entries = list(base.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return []
    real_base = base.resolve()
    found = []
    for p in entries:
        m = LOG_RE.match(p.name)
        if not m or p.is_symlink() or not p.is_file():
            continue
        if real_base not in p.resolve().parents:
            continue
        found.append((-(int(m.group(1))) if m.group(1) else 1, p))   # .10 -> -10 ... .log -> 1
    return [p for _, p in sorted(found, key=lambda x: x[0])]


def _read_lines(path: Path) -> list[str]:
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read().splitlines()


def _ts_of(line: str) -> datetime | None:
    m = TS_RE.match(line)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _fmt_ts(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _fname_ts(dt: datetime | None) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ") if dt else "na"


def span_of(lines: list[str]) -> tuple[datetime | None, datetime | None]:
    first = next((t for t in map(_ts_of, lines) if t), None)
    last = next((t for t in map(_ts_of, reversed(lines)) if t), None)
    return first, last


# ------------------------------------------------------------- line selection
def tail_lines(files: list[Path], n: int) -> list[str]:
    """Last n lines across the chain, in chronological order (rotations stitched oldest->newest)."""
    out: list[str] = []
    for path in reversed(files):
        out = _read_lines(path) + out
        if len(out) >= n:
            break
    return out[-n:] if n > 0 else []


def lines_since(files: list[Path], cutoff: datetime) -> list[str]:
    """Every line whose entry timestamp is >= cutoff. Continuation lines (no timestamp, e.g. a
    wrapped message) follow the entry above them."""
    result: list[str] = []
    for path in reversed(files):
        lines = _read_lines(path)
        kept: list[str] = []
        lead: list[str] = []
        include = None
        first_ts: datetime | None = None
        for line in lines:
            ts = _ts_of(line)
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                include = ts >= cutoff
            elif include is None:
                lead.append(line)
                continue
            if include:
                kept.append(line)
        needs_older = first_ts is None or first_ts >= cutoff
        result = (lead + kept if needs_older else kept) + result
        if not needs_older:
            break
    return result


def iter_all(files: list[Path]) -> Iterator[str]:
    for path in files:
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            for line in f:
                yield line.rstrip("\r\n")


# ---------------------------------------------------------------- catalog helpers
def _file_stats(path: Path) -> dict:
    """Lines / first / last timestamp of one log file, cached by (path, mtime, size): rotated
    files never change, so only the live file is ever re-read."""
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _count_lock:
        hit = _count_cache.get(key)
    if hit:
        return hit
    n, first, last = 0, None, None
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        for line in f:
            n += 1
            ts = _ts_of(line)
            if ts is not None:
                last = ts
                if first is None:
                    first = ts
    stats = {"name": path.name, "bytes": st.st_size, "lines": n, "first": first, "last": last}
    with _count_lock:
        _count_cache[key] = stats
    return stats


def bot_log_info(bot: str) -> dict:
    files = log_files(bot)
    if not files:
        return {"available": False, "reason": "no log files found (volume not mounted or nothing logged yet)",
                "files": [], "lines": 0, "bytes": 0, "first_ts": None, "last_ts": None}
    stats = [_file_stats(p) for p in files]
    firsts = [s["first"] for s in stats if s["first"]]
    lasts = [s["last"] for s in stats if s["last"]]
    return {"available": True,
            "files": [{"name": s["name"], "bytes": s["bytes"], "lines": s["lines"]} for s in stats],
            "lines": sum(s["lines"] for s in stats), "bytes": sum(s["bytes"] for s in stats),
            "first_ts": _fmt_ts(min(firsts) if firsts else None), "last_ts": _fmt_ts(max(lasts) if lasts else None),
            "_first": min(firsts) if firsts else None, "_last": max(lasts) if lasts else None}


def _public(info: dict) -> dict:
    return {k: v for k, v in info.items() if not k.startswith("_")}


def trade_counts(bot: str, fetcher) -> dict:
    try:
        page = fetcher._json(bot, "/api/v1/trades?limit=1&offset=0")
        status = fetcher._json(bot, "/api/v1/status")
    except Exception as e:      # noqa: BLE001
        return {"available": False, "reason": str(e)}
    if page is None:
        return {"available": False, "reason": "bot API unreachable or credentials missing"}
    total = int(page.get("total_trades", 0))
    open_n = len(status) if isinstance(status, list) else 0
    return {"available": True, "total": total, "open": open_n, "closed": max(total - open_n, 0)}


def brain_files() -> list[dict]:
    out = []
    d = _data_dir()
    try:
        names = sorted(p for p in d.iterdir() if BRAIN_RE.match(p.name) and p.is_file() and not p.is_symlink())
    except OSError:
        return []
    for p in names:
        entry = {"name": p.name, "bytes": p.stat().st_size}
        if p.suffix == ".jsonl":
            entry["lines"] = sum(1 for _ in open(p, "rb"))
        out.append(entry)
    return out


def signals_info() -> dict:
    d = _data_dir()
    out: dict = {"audit_available": (d / "signals_audit.json").is_file()}
    for name in SIGNAL_FILES:
        p = d / name
        out[name] = {"bytes": p.stat().st_size, "lines": sum(1 for _ in open(p, "rb"))} if p.is_file() else None
    if out["audit_available"]:
        try:
            out["audit_generated_at"] = json.loads((d / "signals_audit.json").read_text(encoding="utf-8")).get("generated_at")
        except (OSError, ValueError):
            out["audit_generated_at"] = None
    return out


def allocator_info() -> dict:
    d = _data_dir()
    out: dict = {}
    for arc, name in ALLOCATOR_FILES.items():
        p = d / name
        out[arc] = {"bytes": p.stat().st_size} if p.is_file() else None
    return out


def decisions_info() -> dict:
    p = _data_dir() / "decisions.log"
    if not p.is_file():
        return {"available": False, "lines": 0, "bytes": 0}
    return {"available": True, "bytes": p.stat().st_size, "lines": sum(1 for _ in open(p, "rb"))}


# ------------------------------------------------------------------- slots
def _take_slot() -> int:
    token = _slots.acquire()
    if token is None:
        raise HTTPException(429, f"at most {MAX_CONCURRENT} exports at once -- try again in a moment")
    return token


# ================================================================= routes
@router.get("/catalog")
def catalog():
    """What exists, right now, per bot. Cheap after the first call (cached line counts)."""
    fetcher = None
    try:
        fetcher = _fetcher()
    except Exception as e:      # noqa: BLE001
        log.warning("export catalog: no REST fetcher (%s) -- trade counts unavailable", e)

    def one(bot: str) -> dict:
        info = _public(bot_log_info(bot))
        trades = trade_counts(bot, fetcher) if fetcher else {"available": False, "reason": "REST client unavailable"}
        return {"id": bot, "log": info, "trades": trades}

    with ThreadPoolExecutor(max_workers=6) as pool:
        bots = list(pool.map(one, BOT_IDS))
    return JSONResponse({
        "generated_at": _utcnow().isoformat(), "log_timestamps": "UTC", "bots": bots,
        "brain": brain_files(), "decisions": decisions_info(), "signals": signals_info(), "allocator": allocator_info(),
        "aggregates": sorted(AGG_ENDPOINTS),
        "limits": {"max_lines": MAX_LINES, "max_hours": MAX_HOURS, "max_bundle_bytes": MAX_BUNDLE_BYTES,
                   "max_concurrent": MAX_CONCURRENT},
    }, headers={"Cache-Control": "no-store"})


def _select_lines(files: list[Path], lines: int | None, hours: int | None, all_: bool) -> list[str] | Iterator[str]:
    if all_:
        return iter_all(files)
    if hours is not None:
        return lines_since(files, _utcnow() - timedelta(hours=hours))
    return tail_lines(files, lines if lines is not None else DEFAULT_LINES)


def _mode(lines: int | None, hours: int | None, all_: int) -> tuple[int | None, int | None, bool]:
    chosen = [x for x in (lines is not None, hours is not None, bool(all_)) if x]
    if len(chosen) > 1:
        raise HTTPException(400, "give exactly one of lines=N, hours=H, all=1")
    return lines, hours, bool(all_)


@router.get("/logs")
def logs(bot: str, lines: int | None = Query(None, ge=1, le=MAX_LINES),
         hours: int | None = Query(None, ge=1, le=MAX_HOURS), all: int = Query(0, ge=0, le=1)):
    bot = _validate_bot(bot)
    lines, hours, all_ = _mode(lines, hours, all)
    files = log_files(bot)
    if not files:
        raise HTTPException(404, f"no logs for {bot} (nothing mounted or nothing logged yet)")
    token = _take_slot()
    try:
        chosen = _select_lines(files, lines, hours, all_)
    except Exception:
        _slots.release(token)
        raise
    if all_:
        info = bot_log_info(bot)
        first, last = info.get("_first"), info.get("_last")
        body: Iterator[str] = chosen  # type: ignore[assignment]
    else:
        first, last = span_of(chosen)          # type: ignore[arg-type]
        body = iter(chosen)                    # type: ignore[arg-type]
    fname = f"bot_{bot}_{_fname_ts(first)}_{_fname_ts(last)}.txt"

    def gen():
        try:
            for line in body:
                yield mask_secrets(line) + "\n"
        finally:
            _slots.release(token)

    return StreamingResponse(gen(), media_type="text/plain; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})


def iter_trades(bot: str, fetcher, status: str) -> list[dict]:
    rows = fetcher.trades(bot)
    if rows is None:
        raise HTTPException(502, f"{bot}: REST API unreachable or credentials missing")
    if status == "closed":
        rows = [t for t in rows if not t.get("is_open")]
    elif status == "open":
        rows = [t for t in rows if t.get("is_open")]
    return rows


def _trade_row(t: dict) -> dict:
    closed = not t.get("is_open")
    row = {c: t.get(c) for c in TRADE_COLUMNS}
    row["profit_ratio"] = t.get("close_profit") if closed and t.get("close_profit") is not None else t.get("profit_ratio")
    row["profit_abs"] = t.get("close_profit_abs") if closed and t.get("close_profit_abs") is not None else t.get("profit_abs")
    row["profit_pct"] = round(row["profit_ratio"] * 100, 4) if isinstance(row["profit_ratio"], (int, float)) else None
    return row


def trades_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TRADE_COLUMNS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for t in rows:
        w.writerow(_trade_row(t))
    return buf.getvalue()


@router.get("/trades")
def trades(bot: str, status: str = Query("all", pattern="^(closed|open|all)$"),
           format: str = Query("csv", pattern="^(csv|json)$")):
    bot = _validate_bot(bot)
    token = _take_slot()
    try:
        rows = iter_trades(bot, _fetcher(), status)
        if format == "json":
            return JSONResponse({"bot": bot, "status": status, "count": len(rows), "trades": rows},
                                headers={"Content-Disposition": f'attachment; filename="trades_{bot}_{status}.json"'})
        return StreamingResponse(iter([trades_csv(rows)]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="trades_{bot}_{status}.csv"'})
    finally:
        _slots.release(token)


# ------------------------------------------------------------------ bundle
def _estimate(bot: str, lines: int | None, hours: int | None, all_: bool) -> int:
    info = bot_log_info(bot)
    if not info["available"]:
        return 0
    size, n = info["bytes"], max(info["lines"], 1)
    if all_:
        return size
    if hours is not None:
        span = ((info["_last"] - info["_first"]).total_seconds() / 3600) if info.get("_first") and info.get("_last") else 0
        return size if span <= 0 else int(size * min(1.0, hours / span))
    return min(size, int((lines if lines is not None else DEFAULT_LINES) * (size / n)))


class _QueueWriter(io.RawIOBase):
    """Non-seekable sink that hands zip bytes to the response generator, with a byte cap and cancel flag."""

    def __init__(self, q: queue.Queue, cancel: threading.Event, cap: int):
        self.q, self.cancel, self.cap, self.written = q, cancel, cap, 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        if self.cancel.is_set():
            raise RuntimeError("export cancelled (client went away)")
        data = bytes(b)
        self.written += len(data)
        if self.written > self.cap:
            raise RuntimeError("bundle exceeded the size cap")
        self.q.put(data, timeout=300)      # a stream nobody reads must not park this thread forever
        return len(data)


def _build_zip(sink: _QueueWriter, bots: list[str], lines, hours, all_, include: set[str], fetcher_factory) -> None:
    manifest = {
        "generated_at": _utcnow().isoformat(),
        "timezone_note": "All log timestamps are UTC (container time). Trades carry the bot's own UTC dates.",
        "params": {"bots": bots, "lines": lines, "hours": hours, "all": all_, "include": sorted(include)},
        "masking": "Secrets (Bearer/Authorization, api_key/secret/token/password values, JWTs, sk-/ptr_ keys, long hex) are masked in logs.",
        "bots": {}, "brain": [], "decisions": None,
    }
    fetcher = None
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for bot in bots:
            entry: dict = {}
            if "logs" in include:
                files = log_files(bot)
                if not files:
                    entry["logs"] = {"included": False, "note": "no data (no log files for this bot)"}
                else:
                    count, first, last = 0, None, None
                    chosen = _select_lines(files, lines, hours, all_)
                    with zf.open(f"{bot}/logs.txt", "w", force_zip64=True) as out:
                        for line in chosen:
                            ts = _ts_of(line)
                            if ts:
                                first = first or ts
                                last = ts
                            out.write((mask_secrets(line) + "\n").encode("utf-8"))
                            count += 1
                    entry["logs"] = {"included": True, "lines": count, "first_ts": _fmt_ts(first), "last_ts": _fmt_ts(last),
                                     "source_files": [p.name for p in files]}
            if "aggregates" in include:
                agg: dict = {}
                try:
                    fetcher = fetcher or fetcher_factory()
                except Exception as e:      # noqa: BLE001
                    fetcher, agg["_error"] = None, f"REST client unavailable: {e}"
                for name, path in AGG_ENDPOINTS.items():
                    data = fetcher._json(bot, path) if fetcher is not None else None
                    if data is None:
                        agg[name] = {"included": False, "note": "endpoint unavailable (bot unreachable or credentials missing)"}
                        continue
                    text = "\n".join(mask_secrets(l) for l in json.dumps(data, indent=2, default=str).splitlines()) + "\n"
                    zf.writestr(f"aggregates/{bot}/{name}.json", text)
                    agg[name] = {"included": True, "bytes": len(text.encode("utf-8"))}
                entry["aggregates"] = agg
            if "trades" in include:
                try:
                    fetcher = fetcher or fetcher_factory()
                    rows = iter_trades(bot, fetcher, "all")
                    data = trades_csv(rows)
                    zf.writestr(f"{bot}/trades.csv", data)
                    entry["trades"] = {"included": True, "rows": len(rows)}
                except HTTPException as e:
                    entry["trades"] = {"included": False, "note": str(e.detail)}
                except Exception as e:      # noqa: BLE001
                    entry["trades"] = {"included": False, "note": f"error: {e}"}
            manifest["bots"][bot] = entry
        if "brain" in include:
            for meta in brain_files():
                zf.write(_data_dir() / meta["name"], f"brain/{meta['name']}")
                manifest["brain"].append(meta)
        if "decisions" in include:
            info = decisions_info()
            if info["available"]:
                zf.write(_data_dir() / "decisions.log", "decisions/decisions.log")
            manifest["decisions"] = info
        if "signals" in include:
            d = _data_dir()
            sig: dict = {}
            if (d / "signals_audit.json").is_file():
                zf.write(d / "signals_audit.json", "signals_audit.json")
                sig["signals_audit.json"] = {"included": True, "bytes": (d / "signals_audit.json").stat().st_size}
            else:
                sig["signals_audit.json"] = {"included": False, "note": "no audit generated yet (first run is at 00:30 UTC)"}
            kh = Path(os.environ.get("KRONOS_DIR", "/kronos")) / "kronos_health.json"
            if kh.is_file():
                zf.write(kh, "signals/kronos_health.json")
                sig["kronos_health.json"] = {"included": True, "bytes": kh.stat().st_size}
            else:
                sig["kronos_health.json"] = {"included": False, "note": "kronos-signal has not completed a cycle yet"}
            for name in SIGNAL_FILES:
                if (d / name).is_file():
                    zf.write(d / name, f"signals/{name}")
                    sig[name] = {"included": True, "bytes": (d / name).stat().st_size}
                else:
                    sig[name] = {"included": False, "note": "not created yet"}
            manifest["signals"] = sig
        if "allocator" in include:
            d = _data_dir()
            alloc: dict = {}
            for arc, name in ALLOCATOR_FILES.items():
                src = d / name
                if not src.is_file():
                    alloc[arc] = {"included": False, "note": "not created yet"}
                    continue
                n = 0
                with zf.open(arc, "w", force_zip64=True) as out:       # streamed, every line masked like the logs
                    with open(src, "r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            data = (mask_secrets(line.rstrip("\r\n")) + "\n").encode("utf-8")
                            out.write(data)
                            n += len(data)
                alloc[arc] = {"included": True, "bytes": n}
            manifest["allocator"] = alloc
        manifest["aggregates_total_bytes"] = sum(
            m.get("bytes", 0) for e in manifest["bots"].values() for m in e.get("aggregates", {}).values() if isinstance(m, dict))
        zf.writestr("MANIFEST.json", json.dumps(manifest, indent=2, default=str))


@router.get("/bundle")
def bundle(bots: str | None = None, lines: int | None = Query(None, ge=1, le=MAX_LINES),
           hours: int | None = Query(None, ge=1, le=MAX_HOURS), all: int = Query(0, ge=0, le=1),
           include: str = DEFAULT_INCLUDE):
    chosen_bots = _parse_bots(bots)
    inc = {x.strip() for x in include.split(",") if x.strip()}
    if not inc or not inc <= set(INCLUDE_OPTIONS):
        raise HTTPException(400, f"include must be a comma list of {INCLUDE_OPTIONS}")
    lines, hours, all_ = _mode(lines, hours, all)
    if lines is None and hours is None and not all_:
        lines = DEFAULT_LINES
    est = sum(_estimate(b, lines, hours, all_) for b in chosen_bots) if "logs" in inc else 0
    if est > MAX_BUNDLE_BYTES:
        raise HTTPException(413, f"bundle would be about {est // (1024 * 1024)} MB (limit {MAX_BUNDLE_BYTES // (1024 * 1024)} MB) "
                                 "-- pick fewer bots or fewer lines/hours")
    token = _take_slot()
    q: queue.Queue = queue.Queue(maxsize=64)
    cancel = threading.Event()
    sink = _QueueWriter(q, cancel, MAX_BUNDLE_BYTES)
    error: list[BaseException] = []

    def worker():
        try:
            _build_zip(sink, chosen_bots, lines, hours, all_, inc, _fetcher)
        except BaseException as e:      # noqa: BLE001
            error.append(e)
            log.error("export bundle failed: %s", e)
        finally:
            try:
                q.put(None, timeout=5)
            except queue.Full:
                pass

    threading.Thread(target=worker, daemon=True, name="export-bundle").start()

    def gen():
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                yield chunk
            if error:
                raise RuntimeError(f"bundle aborted: {error[0]}")
        finally:
            cancel.set()
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            _slots.release(token)

    fname = f"export_{_utcnow().strftime('%Y%m%dT%H%M%SZ')}.zip"
    return StreamingResponse(gen(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"', "Cache-Control": "no-store"})
