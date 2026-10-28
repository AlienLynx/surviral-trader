"""
P20-A -- market context collector.

Once an hour (survival-agent, minute :05) this module builds ONE snapshot of derivatives, macro, news and
sentiment data, appends it to `context_snapshots.jsonl` (append-only), keeps the latest as
`context_latest.json` (GET /api/context/latest) and publishes a copy to the allocator's shared volume so the
LLM bots can read it without making any network call of their own.

Every field carries  value / ts / source / status  with status ok | stale | unavailable:
  * ok           fetched this run, data timestamp younger than 3 hours
  * stale        value kept from an earlier run, or data timestamp older than 3 hours (auto-downgraded)
  * unavailable  no value; `reason` says why. Nothing is ever replaced with a guess or a neutral default.

Derived flags are computed here in code, never by the LLM: funding_extreme (percentile of the current funding
rate in its own 30-day history), oi_spike, macro_event_within_24h, liquidation_cascade.

Sources (public endpoints, no keys, same outbound path the bots use):
  Bybit v5   tickers, funding/history, open-interest, account-ratio, kline        -> derivatives, BTC regime
  Bybit      liquidations: only a WebSocket topic exists, no REST -> "unavailable" (and so is liquidation_cascade)
  Macro      FF weekly calendar JSON (this + next week, high impact, USD) merged with the operator's
             macro_events.json; no feed and no manual file -> unavailable, with a reminder in the report
  News       Google News RSS per coin: no_news | feed_down | news_found + 24h headline count
  Sentiment  alternative.me Fear & Greed, CoinGecko /global (BTC dominance, total cap change)
  ETF flows, on-chain: NOT CONNECTED (no reliable free source was found); listed as such in the report.

Politeness: one shared HTTP layer with a per-host minimum interval, a short response cache and exponential
back-off on 429/403/5xx (a blocked host is left alone until the back-off ends; no proxies, no mirrors).
"""
from __future__ import annotations

import email.utils
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("survival-agent.market_context")

SCHEMA = "p20-context-v1"
STALE_AFTER = timedelta(hours=3)
DROP_AFTER = timedelta(hours=24)        # a carried-forward value older than this becomes unavailable
SNAPSHOT_FILE = "context_snapshots.jsonl"
LATEST_FILE = "context_latest.json"
MANUAL_MACRO_FILE = "macro_events.json"

CONTEXT_BOTS = ["main", "shorts", "moonshot"]     # the LLM prompts
WHITELIST_BOTS = CONTEXT_BOTS + ["listing_fade", "trend_v2"]                           # coins to collect for
MAX_COINS = int(os.getenv("CONTEXT_MAX_COINS", "60"))
NEWS_MAX_COINS = int(os.getenv("CONTEXT_NEWS_MAX_COINS", "40"))

# derived-flag parameters (documented in AGENTS.md)
FUNDING_LOOKBACK = timedelta(days=30)
FUNDING_MIN_SAMPLES = 30
FUNDING_EXTREME_PCTILE = 0.95            # >= 95th or <= 5th percentile of the coin's own 30-day history
OI_SPIKE_4H = 0.10                       # open interest +10% (or more) within 4 hours
MACRO_WINDOW_H = 24
MACRO_HORIZON_DAYS = 7

BYBIT = "https://api.bybit.com"
FF_THIS_WEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_WEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
FNG_URL = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"
GOOGLE_RSS = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "survival-agent/p20 (+context-collector)"}

MACRO_KEYWORDS = ("FOMC", "Federal Funds", "Fed Chair", "CPI", "Non-Farm", "NFP", "PCE", "GDP", "PPI", "Unemployment",
                  "Retail Sales", "Powell", "Interest Rate")


class SourceError(Exception):
    pass


REASON_CLASSES = [
    ("no_perp", r"no linear USDT perpetual"),
    ("timeout", r"(?i)timeout|timed out"),
    ("rate_limited", r"HTTP 429"),
    ("blocked", r"HTTP 40[13]"),
    ("server_error", r"HTTP 5\d\d"),
    ("backoff_active", r"back-off active"),
    ("connection_error", r"(?i)connection|ssl|resolve|name or service|max retries"),
    ("api_error", r"retCode"),
    ("history_too_short", r"too short|need >="),
    ("empty_response", r"empty|no history|no fundingRate|no funding history"),
    ("feed_down", r"feed_down"),
]


def classify_reason(reason: str | None) -> str:
    """Group a free-text failure reason into a stable class so the snapshot can say WHY fields are unavailable."""
    import re as _re
    text = str(reason or "")
    for name, pattern in REASON_CLASSES:
        if _re.search(pattern, text):
            return name
    return "other"


def unavailable_breakdown(entries: list[tuple[str, str, dict]]) -> dict:
    """entries: (coin, field_name, field). Counts the unavailable ones by reason class and by field, with one example
    message per class and the affected coins."""
    by_reason: dict[str, int] = {}
    by_field: dict[str, int] = {}
    examples: dict[str, str] = {}
    coins: dict[str, set] = {}
    stale = 0
    for coin, name, f in entries:
        if f["status"] == "stale":
            stale += 1
        if f["status"] != "unavailable":
            continue
        cls = classify_reason(f.get("reason"))
        by_reason[cls] = by_reason.get(cls, 0) + 1
        by_field[name] = by_field.get(name, 0) + 1
        examples.setdefault(cls, str(f.get("reason"))[:200])
        coins.setdefault(cls, set()).add(coin)
    return {"total": sum(by_reason.values()), "stale": stale, "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])),
            "by_field": dict(sorted(by_field.items(), key=lambda kv: -kv[1])), "examples": examples,
            "coins_by_reason": {k: sorted(v) for k, v in coins.items()}}


# ----------------------------------------------------------------- fields
def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def field(value, ts: datetime | None, source: str, status: str = "ok", reason: str | None = None) -> dict:
    f = {"value": value, "ts": iso(ts), "source": source, "status": status}
    if reason:
        f["reason"] = reason
    return f


def unavailable(source: str, reason: str) -> dict:
    return {"value": None, "ts": None, "source": source, "status": "unavailable", "reason": reason}


def is_field(x) -> bool:
    return isinstance(x, dict) and {"value", "ts", "source", "status"} <= set(x)


def effective_status(f: dict, now: datetime) -> str:
    """Status as of `now`: an 'ok' field whose data timestamp is older than 3 hours is stale; a stale one
    older than 24 hours is unavailable. Used by the collector, the bots' prompt builder and the allocator."""
    st = f.get("status")
    if st == "unavailable" or f.get("value") is None:
        return "unavailable"
    ts = parse_iso(f.get("ts"))
    if ts is None:
        return "unavailable"
    age = now - ts
    if age > DROP_AFTER:
        return "unavailable"
    if st == "stale" or age > STALE_AFTER:
        return "stale"
    return "ok"


def apply_staleness(node, now: datetime):
    """Walk a snapshot and re-grade every field in place."""
    if is_field(node):
        eff = effective_status(node, now)
        if eff != node["status"]:
            node["status"] = eff
            if eff == "unavailable" and node.get("value") is not None:
                node["reason"] = "data older than 24h"
            if eff == "unavailable":
                node["value"] = None
        return node
    if isinstance(node, dict):
        for v in node.values():
            apply_staleness(v, now)
    elif isinstance(node, list):
        for v in node:
            apply_staleness(v, now)
    return node


def carry_forward(prev: dict | None, new: dict) -> dict:
    """A field that failed this run reuses the last good value with its ORIGINAL timestamp, marked stale
    (it degrades to unavailable after 24h via apply_staleness). Fresh fields are untouched."""
    if new.get("status") != "unavailable" or not prev or not is_field(prev) or prev.get("value") is None:
        return new
    kept = dict(prev)
    kept["status"] = "stale"
    kept["reason"] = f"refresh failed ({new.get('reason')}); last good value kept"
    return kept


# ------------------------------------------------------------- HTTP layer
class Http:
    """Shared, polite HTTP. All sources go through here: per-host pacing, short cache, back-off."""

    BACKOFF_AFTER = 3
    MIN_INTERVAL = {"api.bybit.com": 0.12, "news.google.com": 1.0, "api.coingecko.com": 1.5}

    def __init__(self, get=None, clock=time.monotonic, sleep=time.sleep, timeout: int = 10, cache_ttl: float = 300.0):
        if get is None:
            import requests
            get = requests.get
        self._get, self._clock, self._sleep, self.timeout, self.ttl = get, clock, sleep, timeout, cache_ttl
        self._last: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}
        self._fails: dict[str, int] = {}
        self._cache: dict[tuple, tuple[float, object]] = {}
        self.calls = 0
        self.stats: dict[str, dict] = {}        # host -> {"calls": n, "errors": {class: n}}

    @staticmethod
    def _host(url: str) -> str:
        return re.sub(r"^https?://", "", url).split("/")[0]

    def fetch(self, url: str, params: dict | None = None, as_text: bool = False):
        host = self._host(url)
        try:
            return self._fetch(url, params, as_text)
        except SourceError as e:
            st = self.stats.setdefault(host, {"calls": 0, "errors": {}})
            cls = classify_reason(str(e))
            st["errors"][cls] = st["errors"].get(cls, 0) + 1
            raise

    def _fetch(self, url: str, params: dict | None, as_text: bool):
        host = self._host(url)
        key = (url, tuple(sorted((params or {}).items())), as_text)
        now = self._clock()
        if key in self._cache and now - self._cache[key][0] < self.ttl:
            return self._cache[key][1]
        if self._blocked_until.get(host, 0) > now:
            raise SourceError(f"back-off active for {host} ({int(self._blocked_until[host] - now)}s left)")
        gap = self.MIN_INTERVAL.get(host, 0.2) - (now - self._last.get(host, -1e9))
        if gap > 0:
            self._sleep(gap)
        self._last[host] = self._clock()
        self.calls += 1
        self.stats.setdefault(host, {"calls": 0, "errors": {}})["calls"] += 1
        try:
            r = self._get(url, params=params, headers=HEADERS, timeout=self.timeout)
            code = getattr(r, "status_code", 200)
            if code in (403, 429) or code >= 500:
                self._fails[host] = self._fails.get(host, 0) + 1
                # 403/429 mean "stop": back off at once. A lone 5xx only counts as a failure -- the host is left alone after
                # BACKOFF_AFTER consecutive ones, so one bad answer cannot knock out every other coin's request.
                if code in (403, 429) or self._fails[host] >= self.BACKOFF_AFTER:
                    self._blocked_until[host] = self._clock() + min(60 * 2 ** self._fails[host], 3600)
                raise SourceError(f"HTTP {code} from {host}")
            r.raise_for_status()
            data = r.text if as_text else r.json()
        except SourceError:
            raise
        except Exception as e:      # noqa: BLE001
            self._fails[host] = self._fails.get(host, 0) + 1
            if self._fails[host] >= self.BACKOFF_AFTER:
                self._blocked_until[host] = self._clock() + min(60 * 2 ** self._fails[host], 3600)
            raise SourceError(f"{host}: {type(e).__name__}: {e}") from e
        self._fails[host] = 0
        self._cache[key] = (self._clock(), data)
        return data


# ------------------------------------------------------------ pure helpers
def coin_of_pair(pair: str) -> str:
    return pair.split("/")[0].strip().upper()


def percentile_rank(value: float, sample: list[float]) -> float | None:
    if not sample:
        return None
    below = sum(1 for s in sample if s < value)
    equal = sum(1 for s in sample if s == value)
    return (below + 0.5 * equal) / len(sample)


def funding_flag(current: float, history: list[tuple[datetime, float]], now: datetime) -> dict:
    sample = [r for ts, r in history if now - ts <= FUNDING_LOOKBACK]
    if len(sample) < FUNDING_MIN_SAMPLES:
        return {"extreme": None, "percentile": None, "n": len(sample), "note": f"need >= {FUNDING_MIN_SAMPLES} samples"}
    p = percentile_rank(current, sample)
    hi, lo = p >= FUNDING_EXTREME_PCTILE, p <= 1 - FUNDING_EXTREME_PCTILE
    return {"extreme": bool(hi or lo), "percentile": round(p * 100, 1), "n": len(sample),
            "direction": "longs_paying" if hi else ("shorts_paying" if lo else None)}


def oi_changes(rows: list[tuple[datetime, float]]) -> dict:
    """rows newest-first, hourly. Change vs the row 1/4/24 steps back; None where history is short."""
    out = {}
    for label, k in (("1h", 1), ("4h", 4), ("24h", 24)):
        out[label] = None if len(rows) <= k or rows[k][1] <= 0 else round(rows[0][1] / rows[k][1] - 1.0, 5)
    return out


def classify_btc_regime(closes_newest_first: list[float]) -> dict | None:
    c = closes_newest_first
    if len(c) < 200:
        return None
    sma50, sma200 = sum(c[:50]) / 50, sum(c[:200]) / 200
    last = c[0]
    if last > sma50 > sma200:
        regime = "uptrend"
    elif last < sma50 < sma200:
        regime = "downtrend"
    else:
        regime = "mixed"
    return {"regime": regime, "close": last, "sma50": round(sma50, 2), "sma200": round(sma200, 2)}


# ------------------------------------------------------------------ Bybit
def _bybit(http: Http, path: str, params: dict):
    data = http.fetch(BYBIT + path, params)
    if not isinstance(data, dict) or data.get("retCode") != 0:
        raise SourceError(f"bybit {path}: retCode={data.get('retCode') if isinstance(data, dict) else '?'} "
                          f"{data.get('retMsg') if isinstance(data, dict) else ''}")
    return data


def fetch_tickers(http: Http) -> tuple[dict[str, dict], datetime]:
    data = _bybit(http, "/v5/market/tickers", {"category": "linear"})
    ts = datetime.fromtimestamp(int(data.get("time", time.time() * 1000)) / 1000, tz=timezone.utc)
    return {t["symbol"]: t for t in data["result"]["list"]}, ts


def fetch_funding_history(http: Http, symbol: str) -> list[tuple[datetime, float]]:
    data = _bybit(http, "/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": 200})
    return [(datetime.fromtimestamp(int(r["fundingRateTimestamp"]) / 1000, tz=timezone.utc), float(r["fundingRate"]))
            for r in data["result"]["list"]]


def fetch_oi(http: Http, symbol: str) -> list[tuple[datetime, float]]:
    data = _bybit(http, "/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 25})
    return [(datetime.fromtimestamp(int(r["timestamp"]) / 1000, tz=timezone.utc), float(r["openInterest"]))
            for r in data["result"]["list"]]


def fetch_ratio(http: Http, symbol: str) -> tuple[datetime, float, float]:
    data = _bybit(http, "/v5/market/account-ratio", {"category": "linear", "symbol": symbol, "period": "1h", "limit": 1})
    row = data["result"]["list"][0]
    return datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc), float(row["buyRatio"]), float(row["sellRatio"])


def fetch_btc_regime(http: Http) -> tuple[dict | None, datetime]:
    data = _bybit(http, "/v5/market/kline", {"category": "linear", "symbol": "BTCUSDT", "interval": "D", "limit": 210})
    rows = data["result"]["list"]            # newest first
    ts = datetime.fromtimestamp(int(rows[0][0]) / 1000, tz=timezone.utc) if rows else None
    return classify_btc_regime([float(r[4]) for r in rows]), ts


def resolve_symbol(coin: str, tickers: dict[str, dict]) -> str | None:
    for cand in (f"{coin}USDT", f"1000{coin}USDT", f"10000{coin}USDT"):
        if cand in tickers:
            return cand
    return None


def derivatives_for_coin(http: Http, coin: str, tickers: dict, tickers_ts: datetime, now: datetime) -> dict:
    src = "bybit_v5_public"
    symbol = resolve_symbol(coin, tickers)
    if symbol is None:
        no = unavailable(src, f"no linear USDT perpetual for {coin} on Bybit")
        return {k: dict(no) for k in ("funding_rate", "funding_24h", "funding_pctile", "open_interest", "oi_change_1h",
                                      "oi_change_4h", "oi_change_24h", "long_short_ratio")} | {"symbol": None}
    out: dict = {"symbol": symbol}
    t = tickers[symbol]
    try:
        out["funding_rate"] = field(float(t["fundingRate"]), tickers_ts, src)
    except (KeyError, ValueError, TypeError):
        out["funding_rate"] = unavailable(src, "tickers row has no fundingRate")
    try:
        hist = fetch_funding_history(http, symbol)
        last24 = [r for ts, r in hist if now - ts <= timedelta(hours=24)]
        out["funding_24h"] = field({"sum": round(sum(last24), 8), "settlements": len(last24)}, tickers_ts, src) if hist else unavailable(src, "empty funding history")
        if hist and out["funding_rate"]["value"] is not None:
            out["funding_pctile"] = field(funding_flag(out["funding_rate"]["value"], hist, now), tickers_ts, src)
        else:
            out["funding_pctile"] = unavailable(src, "no funding history")
    except SourceError as e:
        out["funding_24h"] = unavailable(src, str(e))
        out["funding_pctile"] = unavailable(src, str(e))
    try:
        rows = fetch_oi(http, symbol)
        ch = oi_changes(rows)
        out["open_interest"] = field(rows[0][1], rows[0][0], src) if rows else unavailable(src, "empty OI history")
        for label in ("1h", "4h", "24h"):
            out[f"oi_change_{label}"] = field(ch[label], rows[0][0], src) if rows and ch[label] is not None \
                else unavailable(src, "OI history too short")
    except SourceError as e:
        for k in ("open_interest", "oi_change_1h", "oi_change_4h", "oi_change_24h"):
            out[k] = unavailable(src, str(e))
    try:
        rts, buy, sell = fetch_ratio(http, symbol)
        out["long_short_ratio"] = field({"long": buy, "short": sell}, rts, src)
    except (SourceError, IndexError, KeyError, ValueError) as e:
        out["long_short_ratio"] = unavailable(src, str(e))
    return out


def coin_flags(d: dict, now: datetime) -> dict:
    """Derived flags for one coin: value True/False, or None (+unavailable status) when the inputs are missing."""
    src = "derived"
    fp = d.get("funding_pctile")
    if fp and effective_status(fp, now) != "unavailable" and fp["value"]["extreme"] is not None:
        fe = field(bool(fp["value"]["extreme"]), parse_iso(fp["ts"]), src)
        if effective_status(fp, now) == "stale":
            fe["status"] = "stale"
    else:
        fe = unavailable(src, "funding history unavailable or too short")
    o4 = d.get("oi_change_4h")
    if o4 and effective_status(o4, now) != "unavailable":
        spike = field(bool(o4["value"] >= OI_SPIKE_4H), parse_iso(o4["ts"]), src)
        if effective_status(o4, now) == "stale":
            spike["status"] = "stale"
    else:
        spike = unavailable(src, "open interest unavailable")
    return {"funding_extreme": fe, "oi_spike": spike}


# ------------------------------------------------------------------ macro
def parse_ff_calendar(rows, now: datetime) -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            ts = datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        if str(r.get("impact", "")).lower() != "high" or str(r.get("country", "")).upper() != "USD":
            continue
        title = str(r.get("title", ""))
        if not any(k.lower() in title.lower() for k in MACRO_KEYWORDS):
            continue
        if now - timedelta(hours=1) <= ts <= now + timedelta(days=MACRO_HORIZON_DAYS):
            out.append({"name": title, "ts": iso(ts), "importance": "high", "country": "USD", "source": "ff_calendar"})
    return out


def load_manual_macro(data_dir: Path, now: datetime) -> tuple[list[dict], dict]:
    path = Path(data_dir) / MANUAL_MACRO_FILE
    info = {"exists": path.is_file(), "path": MANUAL_MACRO_FILE}
    if not path.is_file():
        return [], info
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        info["error"] = str(e)
        return [], info
    events = raw.get("events", raw) if isinstance(raw, dict) else raw
    out = []
    for e in events if isinstance(events, list) else []:
        ts = parse_iso(str(e.get("ts", "")))
        if ts is None or not e.get("name"):
            continue
        if now - timedelta(hours=1) <= ts <= now + timedelta(days=MACRO_HORIZON_DAYS):
            out.append({"name": str(e["name"]), "ts": iso(ts), "importance": str(e.get("importance", "high")),
                        "country": str(e.get("country", "USD")), "source": "manual"})
    info["upcoming"] = len(out)
    return out, info


def collect_macro(http: Http, data_dir: Path, now: datetime) -> tuple[dict, list[str], dict]:
    """Returns (field, reminders, source_info). Feed + manual merged; manual alone if the feed is down."""
    reminders: list[str] = []
    manual, minfo = load_manual_macro(data_dir, now)
    feed_events, feed_err = None, None
    try:
        feed_events = []
        for url in (FF_THIS_WEEK, FF_NEXT_WEEK):
            try:
                feed_events += parse_ff_calendar(http.fetch(url), now)
            except SourceError as e:
                if url == FF_THIS_WEEK:
                    raise
                log.info("market_context: next-week calendar not available yet: %s", e)
    except SourceError as e:
        feed_events, feed_err = None, str(e)
    info = {"primary": "ff_calendar", "primary_ok": feed_events is not None, "manual_file": minfo}
    if feed_err:
        info["primary_error"] = feed_err
    if feed_events is None and not manual:
        if not minfo["exists"]:
            reminders.append("macro calendar feed is down and macro_events.json does not exist: create it "
                             "(list of {name, ts, importance}) or the macro flag stays unavailable")
        else:
            reminders.append("macro calendar feed is down and macro_events.json has no upcoming events: update it")
        return unavailable("macro_calendar", feed_err or "no source"), reminders, info
    merged = {(e["name"], e["ts"]): e for e in (feed_events or []) + manual}
    events = sorted(merged.values(), key=lambda e: e["ts"])
    source = "ff_calendar" if feed_events is not None else "manual"
    if feed_events is not None and manual:
        source = "ff_calendar+manual"
    if feed_events is None:
        reminders.append("macro calendar feed is down: using macro_events.json only -- check it is current")
    return field(events, now, source), reminders, info


def macro_flag(macro_field: dict, now: datetime) -> dict:
    if effective_status(macro_field, now) == "unavailable":
        return unavailable("derived", "macro calendar unavailable")
    soon = [e for e in macro_field["value"] if 0 <= (parse_iso(e["ts"]) - now).total_seconds() <= MACRO_WINDOW_H * 3600]
    f = field(bool(soon), parse_iso(macro_field["ts"]), "derived")
    if soon:
        f["events"] = soon
    if effective_status(macro_field, now) == "stale":
        f["status"] = "stale"
    return f


# ------------------------------------------------------------------- news
def count_recent(items: list[tuple[str, datetime | None]], now: datetime) -> int:
    return sum(1 for _, ts in items if ts is not None and now - ts <= timedelta(hours=24))


def fetch_news(http: Http, coin: str, now: datetime) -> dict:
    """no_news (feed answered, nothing), feed_down (fetch failed), news_found. Three distinct states."""
    src = "google_news_rss"
    q = f'"{coin}" crypto'
    try:
        text = http.fetch(GOOGLE_RSS, {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}, as_text=True)
        root = ET.fromstring(text)
        items = []
        for it in root.findall(".//item"):
            title = (it.findtext("title") or "").strip()
            pub = it.findtext("pubDate")
            try:
                ts = email.utils.parsedate_to_datetime(pub).astimezone(timezone.utc) if pub else None
            except (TypeError, ValueError):
                ts = None
            if title:
                items.append((title, ts))
    except (SourceError, ET.ParseError) as e:
        f = unavailable(src, f"feed_down: {e}")
        f["news_status"] = "feed_down"
        return f
    status = "news_found" if items else "no_news"
    f = field({"count_24h": count_recent(items, now), "total_items": len(items), "headlines": [t for t, _ in items[:3]]},
              now, src)
    f["news_status"] = status
    return f


# ------------------------------------------------------------ sentiment
def fetch_fng(http: Http, now: datetime) -> dict:
    try:
        entry = (http.fetch(FNG_URL).get("data") or [{}])[0]
        v, cls = int(entry["value"]), entry["value_classification"]
        as_of = iso(datetime.fromtimestamp(int(entry["timestamp"]), tz=timezone.utc)) if entry.get("timestamp") else None
        # the index is published once a day, so the field timestamp is the moment we confirmed it (as_of = publication)
        return field({"value": v, "classification": cls, "as_of": as_of}, now, "alternative.me")
    except (SourceError, KeyError, ValueError, TypeError, AttributeError) as e:
        return unavailable("alternative.me", str(e))


def fetch_global(http: Http, now: datetime) -> tuple[dict, dict]:
    try:
        d = http.fetch(COINGECKO_GLOBAL)["data"]
        dom = float(d["market_cap_percentage"]["btc"])
        chg = float(d["market_cap_change_percentage_24h_usd"])
        return field(round(dom, 2), now, "coingecko"), field(round(chg, 3), now, "coingecko")
    except (SourceError, KeyError, ValueError, TypeError) as e:
        return unavailable("coingecko", str(e)), unavailable("coingecko", str(e))


# --------------------------------------------------------------- universe
def universe(fetcher, data_dir: Path, limit: int = MAX_COINS) -> list[str]:
    """BTC, ETH, then the coins the bots trade (their current whitelists, then their recent ledger)."""
    coins: list[str] = ["BTC", "ETH"]
    seen = set(coins)

    def add(c: str):
        if c and c not in seen and re.fullmatch(r"[A-Z0-9]{2,15}", c):
            seen.add(c)
            coins.append(c)

    for bot in WHITELIST_BOTS:
        try:
            wl = fetcher._json(bot, "/api/v1/whitelist") if fetcher is not None else None
        except Exception:       # noqa: BLE001
            wl = None
        for p in (wl or {}).get("whitelist", []) if isinstance(wl, dict) else []:
            add(coin_of_pair(p))
    ledger = Path(data_dir) / "signals.jsonl"
    if ledger.is_file():
        try:
            for line in ledger.read_text(encoding="utf-8").splitlines()[-2000:]:
                try:
                    add(coin_of_pair(json.loads(line)["pair"]))
                except (ValueError, KeyError):
                    continue
        except OSError:
            pass
    return coins[:limit]


# --------------------------------------------------------------- snapshot
def build_snapshot(now: datetime, http: Http, coins: list[str], prev: dict | None, data_dir: Path) -> dict:
    prev = prev or {}
    prev_g, prev_c = prev.get("global", {}), prev.get("coins", {})
    snap: dict = {"schema": SCHEMA, "ts": iso(now), "coins_requested": coins}

    # ---- global, sentiment
    fng = carry_forward(prev_g.get("fear_greed"), fetch_fng(http, now))
    dom, cap = fetch_global(http, now)
    dom, cap = carry_forward(prev_g.get("btc_dominance"), dom), carry_forward(prev_g.get("total_mcap_change_24h"), cap)
    macro, reminders, macro_info = collect_macro(http, data_dir, now)
    macro = carry_forward(prev_g.get("macro_events"), macro) if effective_status(macro, now) == "unavailable" else macro
    try:
        regime, rts = fetch_btc_regime(http)
        btc_regime = field(regime, rts, "bybit_v5_public") if regime else unavailable("bybit_v5_public", "fewer than 200 daily candles")
    except SourceError as e:
        btc_regime = carry_forward(prev_g.get("btc_regime"), unavailable("bybit_v5_public", str(e)))
    snap["global"] = {
        "fear_greed": fng, "btc_dominance": dom, "total_mcap_change_24h": cap, "macro_events": macro,
        "btc_regime": btc_regime,
        "liquidations": unavailable("bybit", "Bybit exposes liquidations only via WebSocket (allLiquidation); no REST endpoint -- not collected"),
        "etf_flows": unavailable("none", "not connected: no reliable free source found"),
        "onchain": unavailable("none", "not connected: no reliable free source found"),
    }

    # ---- per coin
    try:
        tickers, tickers_ts = fetch_tickers(http)
        tickers_err = None
    except SourceError as e:
        tickers, tickers_ts, tickers_err = {}, now, str(e)
    snap["coins"] = {}
    for i, coin in enumerate(coins):
        if tickers_err:
            d = {k: unavailable("bybit_v5_public", tickers_err) for k in
                 ("funding_rate", "funding_24h", "funding_pctile", "open_interest", "oi_change_1h", "oi_change_4h",
                  "oi_change_24h", "long_short_ratio")}
            d["symbol"] = None
        else:
            d = derivatives_for_coin(http, coin, tickers, tickers_ts, now)
        if tickers_err or d.get("symbol"):          # a coin with no Bybit perp has nothing to carry forward
            for k, v in list(d.items()):
                if is_field(v):
                    d[k] = carry_forward(prev_c.get(coin, {}).get(k), v)
        if i < NEWS_MAX_COINS:
            news = fetch_news(http, coin, now)
            d["news_status"] = news.pop("news_status")
        else:
            news, d["news_status"] = unavailable("google_news_rss", "news not collected for this coin (cap)"), "not_collected"
        d["news"] = news
        apply_staleness(d, now)
        d["flags"] = coin_flags(d, now)
        snap["coins"][coin] = d

    # ---- global flags
    mf = macro_flag(snap["global"]["macro_events"], now)
    snap["flags"] = {
        "macro_event_within_24h": mf,
        "liquidation_cascade": unavailable("derived", "liquidations not collected (no REST source)"),
        "funding_extreme_coins": sorted(c for c, d in snap["coins"].items() if d["flags"]["funding_extreme"].get("value") is True),
        "oi_spike_coins": sorted(c for c, d in snap["coins"].items() if d["flags"]["oi_spike"].get("value") is True),
    }
    apply_staleness(snap["global"], now)
    snap["reminders"] = reminders
    snap["sources"] = summarize_sources(snap, macro_info, tickers_err, now)
    snap["unavailable_breakdown"] = {"bybit_derivatives": snap["sources"]["bybit_derivatives"]["unavailable"],
                                     "news_google_rss": snap["sources"]["news_google_rss"]["unavailable"]}
    snap["http_stats"] = {h: {"calls": s["calls"], "errors": dict(s["errors"])} for h, s in http.stats.items()}
    return snap


def _share_ok(fields: list[dict]) -> float | None:
    return None if not fields else round(sum(1 for f in fields if f["status"] == "ok") / len(fields), 3)


def summarize_sources(snap: dict, macro_info: dict, tickers_err: str | None, now: datetime) -> dict:
    coins, g = snap["coins"], snap["global"]
    deriv_keys = ("funding_rate", "funding_24h", "funding_pctile", "open_interest", "oi_change_1h", "oi_change_4h", "oi_change_24h", "long_short_ratio")
    deriv_fields = [d[k] for d in coins.values() for k in deriv_keys if k in d]
    deriv_entries = [(c, k, d[k]) for c, d in coins.items() for k in deriv_keys if k in d]
    news_entries = [(c, "news", d["news"]) for c, d in coins.items() if d.get("news_status") != "not_collected"]
    news_fields = [d["news"] for d in coins.values() if d.get("news_status") != "not_collected"]

    def level(share):
        return "unavailable" if not share else "ok" if share >= 0.9 else "degraded"

    deriv_share = _share_ok(deriv_fields)
    news_share = _share_ok(news_fields)
    out = {
        "bybit_derivatives": {"status": level(deriv_share), "ok_share": deriv_share, "coins": len(coins),
                              "reason": tickers_err, "unavailable": unavailable_breakdown(deriv_entries),
                              "coins_without_perp": sorted(c for c, d in coins.items() if d.get("symbol") is None)},
        "macro_calendar": {"status": g["macro_events"]["status"], "detail": macro_info,
                           "reason": g["macro_events"].get("reason")},
        "news_google_rss": {"status": level(news_share), "ok_share": news_share,
                            "feed_down": sum(1 for d in coins.values() if d.get("news_status") == "feed_down"),
                            "no_news": sum(1 for d in coins.values() if d.get("news_status") == "no_news"),
                            "news_found": sum(1 for d in coins.values() if d.get("news_status") == "news_found"),
                            "not_collected_cap": sum(1 for d in coins.values() if d.get("news_status") == "not_collected"),
                            "unavailable": unavailable_breakdown(news_entries)},
        "fear_greed": {"status": g["fear_greed"]["status"], "reason": g["fear_greed"].get("reason")},
        "btc_dominance": {"status": g["btc_dominance"]["status"], "reason": g["btc_dominance"].get("reason")},
        "liquidations": {"status": "unavailable", "reason": g["liquidations"]["reason"]},
        "etf_flows": {"status": "not_connected", "reason": "not connected"},
        "onchain": {"status": "not_connected", "reason": "not connected"},
    }
    return out


# ------------------------------------------------------------------- run
def enabled(env: dict | None = None) -> bool:
    from . import runtime_settings as rs
    env = rs.merged_env() if env is None else env
    return str(env.get("MARKET_CONTEXT_MODE", "on")).strip().lower() != "off"


def load_latest(data_dir: Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / LATEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def run_hourly(data_dir: Path, now: datetime | None = None, http: Http | None = None, fetcher=None,
               shared_dir: Path | None = None, coins: list[str] | None = None) -> dict:
    if not enabled():
        return {"mode": "off", "skipped": True}
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    http = http or Http()
    if fetcher is None:
        from . import portfolio_brain
        fetcher = portfolio_brain.default_fetcher()
    coins = coins if coins is not None else universe(fetcher, data_dir)
    snap = build_snapshot(now, http, coins, load_latest(data_dir), data_dir)
    line = json.dumps(snap, separators=(",", ":"), default=str)
    with open(data_dir / SNAPSHOT_FILE, "a", encoding="utf-8") as f:      # append-only
        f.write(line + "\n")
    _atomic_write(data_dir / LATEST_FILE, json.dumps(snap, indent=1, default=str))
    shared = shared_dir if shared_dir is not None else Path(os.environ.get("ALLOCATOR_DIR") or "/allocator")
    if shared is not None and Path(shared).is_dir():
        try:
            _atomic_write(Path(shared) / LATEST_FILE, json.dumps(snap, separators=(",", ":"), default=str))
        except OSError as e:
            log.warning("market_context: could not publish to the shared volume: %s", e)
    log.info("market_context: %d coins, derivatives ok_share=%s, news=%s, macro=%s, http_calls=%d", len(coins),
             snap["sources"]["bybit_derivatives"]["ok_share"], snap["sources"]["news_google_rss"].get("ok_share"),
             snap["global"]["macro_events"]["status"], http.calls)
    return snap


def seconds_until_next_hour(now: datetime, minute: int = 5) -> float:
    nxt = now.replace(minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    return (nxt - now).total_seconds()


if __name__ == "__main__":      # manual: docker exec survival-agent python -m backend.engine.market_context
    logging.basicConfig(level=logging.INFO)
    run_hourly(Path(os.getenv("DATA_DIR", "/data")))
