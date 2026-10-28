"""
P25: exchange announcement guard. Bybit publishes delisting notices (median 2 days before a perpetual contract is delisted; 255 perpetual notices since 2022). A bot that opens a trade on a coin
that is about to be delisted takes a settlement / squeeze risk it cannot be paid for. This module reads the public announcement feed, keeps every notice in a ledger (audit + history),
and publishes one small file, `blocked_pairs.json`, on the shared allocator volume. The bots read it (`_allocator_ctx.entry_block`) in `confirm_trade_entry` and refuse NEW entries on a blocked coin.
Exits, stops and open trades are never touched.

Rules, deliberately plain (a keyword rule, not an LLM: a delisting notice has a fixed form, and a regex that is right cannot be talked out of it):
  perpetual notice  "Delisting of XYZUSDT Perpetual Contract" (one or several symbols) + "... at Oct 10, 2026, 9:00AM UTC" in the description -> coin XYZ blocked from the publish time to the delisting time
  list notice       "Delisting of AAA,BBB,CCC" / "Delisting of AAA on <date>" -> those coins blocked from the publish time until the stated date (14 days when none)
  anything the parser cannot read is ignored (and counted), never guessed.
Fail-safe: a missing or stale file blocks nothing (a broken feed must not stop trading), and the status says so.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("survival-agent.announcements")

URL = "https://api.bybit.com/v5/announcements/index"
LEDGER = "announcements.jsonl"
BLOCKED = "blocked_pairs.json"
DEFAULT_WINDOW_DAYS = 14
AFTER_DAYS = 1                    # keep a coin blocked a day after the stated delisting time (a delayed delisting must not reopen the door)
SYMBOL_RE = re.compile(r"\b([A-Z0-9]{1,20}?)USDT\b")
WHEN_RE = re.compile(r"at\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[AP]M)\s+UTC")
LIST_RE = re.compile(r"^\s*Delisting\s+of\s+([A-Z0-9][A-Z0-9,\s]{1,200}?)(?:\s+on\s+(.+))?\s*$")
COIN_RE = re.compile(r"^[A-Z0-9]{2,20}$")
STOP_WORDS = {"SELECTED", "PERPETUAL", "CONTRACT", "CONTRACTS", "SPOT", "TRADING", "PAIRS", "TOKENS", "TOKEN", "ON", "AND", "OF", "USDC", "USDT", "USD"}


def _ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_when(text: str) -> int | None:
    m = WHEN_RE.search(text or "")
    if not m:
        return None
    try:
        return _ms(datetime.strptime(re.sub(r"\s+", " ", m.group(1)).replace(" AM", "AM").replace(" PM", "PM"), "%b %d, %Y, %I:%M%p").replace(tzinfo=timezone.utc))
    except ValueError:
        return None


def _parse_date(text: str | None) -> int | None:
    if not text:
        return None
    for fmt in ("%b %d, %Y", "%Y-%m-%d"):
        m = re.search(r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}" if fmt == "%b %d, %Y" else r"\d{4}-\d{2}-\d{2}", text)
        if m:
            try:
                return _ms(datetime.strptime(re.sub(r"\s+", " ", m.group(0)), fmt).replace(tzinfo=timezone.utc))
            except ValueError:
                continue
    return None


def parse_item(item: dict) -> dict | None:
    """One announcement -> {coins, published_ms, effective_ms, kind, title}; None when it is not a delisting notice we can read."""
    title = " ".join(str(item.get("title") or "").split())
    pub = int(item.get("publishTime") or item.get("dateTimestamp") or 0)
    if not pub:
        return None
    if re.search(r"Delisting\s+of\b", title, re.I) and re.search(r"Perpetual\s+Contracts?", title, re.I) and not re.search(r"Relisting|Inverse|Pre-IPO", title, re.I):
        coins = [c.upper() for c in SYMBOL_RE.findall(title) if c.upper() not in STOP_WORDS]
        if coins:
            return {"coins": sorted(set(coins)), "published_ms": pub, "effective_ms": _parse_when(str(item.get("description") or "")), "kind": "perp_delisting", "title": title[:160]}
    m = LIST_RE.match(title)
    if m and "Perpetual" not in title and "Collateral" not in title:
        coins = [c for c in (x.strip().upper() for x in m.group(1).replace(" ", ",").split(",")) if COIN_RE.match(c) and c not in STOP_WORDS]
        if coins:
            return {"coins": coins, "published_ms": pub, "effective_ms": _parse_date(m.group(2)), "kind": "token_delisting", "title": title[:160]}
    return None


def fetch(get, pages: int = 2, types=("delistings",), sleep=time.sleep) -> list[dict]:
    out = []
    for t in types:
        for page in range(1, pages + 1):
            last = None
            for attempt in range(3):
                try:
                    r = get(URL, params={"locale": "en-US", "type": t, "limit": 50, "page": page}, timeout=20)
                    r.raise_for_status()
                    data = r.json()
                    if data.get("retCode") != 0:
                        raise RuntimeError(f"retCode {data.get('retCode')}")
                    last = (data.get("result") or {}).get("list") or []
                    break
                except Exception as e:      # noqa: BLE001
                    last = None
                    sleep(1 + attempt)
                    err = e
            if last is None:
                raise RuntimeError(f"announcements feed failed: {err}")
            out += last
            if len(last) < 50:
                break
    return out


def read_ledger(data_dir: Path) -> dict[str, dict]:
    out = {}
    p = Path(data_dir) / LEDGER
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                out[e["id"]] = e
            except (ValueError, KeyError):
                continue
    return out


def build_blocked(entries: list[dict], now: datetime) -> dict:
    coins: dict[str, dict] = {}
    now_ms = _ms(now)
    for e in entries:
        until = (e["effective_ms"] + AFTER_DAYS * 86_400_000) if e.get("effective_ms") else e["published_ms"] + DEFAULT_WINDOW_DAYS * 86_400_000
        if not (e["published_ms"] <= now_ms <= until):
            continue
        for c in e["coins"]:
            old = coins.get(c)
            if old is None or until > old["_until"]:
                coins[c] = {"from": _iso(e["published_ms"]), "until": _iso(until), "reason": f"{e['kind'].replace('_', ' ')}: {e['title']}" + (f" (effective {_iso(e['effective_ms'])})" if e.get("effective_ms") else " (no date in the notice)"), "_until": until}
    return {"updated": _iso(now_ms), "coins": {c: {k: v for k, v in d.items() if k != "_until"} for c, d in sorted(coins.items())}}


def refresh(data_dir: Path, allocator_dir: Path, get=None, now: datetime | None = None, pages: int = 2) -> dict:
    """Fetch the newest notices, append unseen ones to the ledger, republish blocked_pairs.json. Returns a status dict (never raises on a feed failure: the old file simply goes stale)."""
    import requests

    get = get or requests.get
    now = now or datetime.now(timezone.utc)
    data_dir, allocator_dir = Path(data_dir), Path(allocator_dir)
    ledger = read_ledger(data_dir)
    first = not ledger
    try:
        items = fetch(get, pages=11 if first else pages)
    except Exception as e:      # noqa: BLE001
        log.warning("announcements: %s", e)
        return {"ok": False, "error": str(e), "ledger": len(ledger)}
    new, unreadable = [], 0
    for it in items:
        key = str(it.get("url") or it.get("title") or "")[-120:]
        if not key or key in ledger:
            continue
        parsed = parse_item(it)
        if parsed is None:
            unreadable += 1
            continue
        parsed["id"] = key
        ledger[key] = parsed
        new.append(parsed)
    if new:
        with open(data_dir / LEDGER, "a", encoding="utf-8") as f:
            for e in new:
                f.write(json.dumps(e, sort_keys=True) + "\n")
    blocked = build_blocked(list(ledger.values()), now)
    allocator_dir.mkdir(parents=True, exist_ok=True)
    tmp = allocator_dir / (BLOCKED + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(blocked, indent=1), encoding="utf-8")
    os.replace(tmp, allocator_dir / BLOCKED)
    if new:
        log.info("announcements: %d new notice(s), %d coin(s) blocked now", len(new), len(blocked["coins"]))
    return {"ok": True, "new": len(new), "unreadable_new": unreadable, "ledger": len(ledger), "blocked": sorted(blocked["coins"])}


def status(data_dir: Path, allocator_dir: Path, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    try:
        d = json.loads((Path(allocator_dir) / BLOCKED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False, "reason": "no blocked_pairs.json yet (the first refresh runs a few minutes after start)"}
    try:
        age = int((now - datetime.fromisoformat(str(d.get("updated")).replace("Z", "+00:00"))).total_seconds())
    except ValueError:
        age = None
    return {"available": True, "updated": d.get("updated"), "age_seconds": age, "fresh_for_bots": age is not None and age <= 6 * 3600, "coins": d.get("coins", {}), "ledger": len(read_ledger(data_dir))}
