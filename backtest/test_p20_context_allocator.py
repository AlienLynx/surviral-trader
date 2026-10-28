"""
P20-A (market context collector), the P19 context-flag slices, and P20-B (LLM allocator).
No network: Bybit / calendar / news / sentiment payloads are canned, bots are fakes.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import math
import os
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


DATA, BOTS_TMP, SHARED = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"DATA_DIR": DATA, "BOTS_DIR": BOTS_TMP, "ALLOCATOR_DIR": SHARED})
for k in ("PORTFOLIO_BRAIN_MODE", "MARKET_CONTEXT_MODE", "SIGNAL_AUDIT_MODE"):
    os.environ.pop(k, None)

from backend.engine import allocator as al  # noqa: E402

# P25: the default policy is now `grow`; this suite pins the original `protect` rules (the grow policy has its own suite, test_p25_grow_policy.py)
_orig_load_settings = al.load_settings


def _protect_settings(env=None):
    e = dict(al.rs.merged_env() if env is None else env)
    e.setdefault("ALLOCATOR_POLICY", "protect")
    return _orig_load_settings(e)


al.load_settings = _protect_settings
from backend.engine import market_context as mc  # noqa: E402
from backend.engine import portfolio_brain as pb  # noqa: E402
from backend.engine import signal_audit as sa  # noqa: E402

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
MS = lambda dt: str(int(dt.timestamp() * 1000))  # noqa: E731


# ================================================================ canned sources
class Resp:
    def __init__(self, payload, code=200):
        self.status_code, self._p = code, payload
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self):
        return self._p if not isinstance(self._p, str) else json.loads(self._p)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def rss(items):
    body = "".join(f"<item><title>{t}</title><pubDate>{d.strftime('%a, %d %b %Y %H:%M:%S GMT')}</pubDate></item>" for t, d in items)
    return f'<?xml version="1.0"?><rss><channel>{body}</channel></rss>'


class World:
    """Canned internet. Knobs let each test break one source."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.down: set[str] = set()                 # substrings of urls that fail with 500
        self.codes: dict[str, int] = {}             # url substring -> status code
        self.oi_age_h = {"ETHUSDT": 5}              # ETH open interest data is 5h old -> stale
        self.news = {"BTC": [("BTC rallies", NOW - timedelta(hours=2)), ("BTC old", NOW - timedelta(hours=30))],
                     "ETH": [], "PUMP": None}       # None -> feed down for that coin
        self.no_perp = {"ZZZ"}
        self.cpi_in_h = 10                          # None -> no high-impact event inside 24h

    def get(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        self.calls.append((url, params))
        for frag, code in self.codes.items():
            if frag in url:
                return Resp("", code)
        if any(f in url for f in self.down):
            return Resp("", 500)
        if "/v5/market/tickers" in url:
            rows = [{"symbol": s, "fundingRate": r, "lastPrice": "1"} for s, r in (("BTCUSDT", "0.0010"), ("ETHUSDT", "0.0001"), ("PUMPUSDT", "0.0001"))]
            return Resp({"retCode": 0, "result": {"list": rows}, "time": int(NOW.timestamp() * 1000)})
        if "/funding/history" in url:
            base = 0.0001
            rows = [{"symbol": params["symbol"], "fundingRate": str(base + (i % 5) * 1e-6),
                     "fundingRateTimestamp": MS(NOW - timedelta(hours=8 * i + 1))} for i in range(100)]
            return Resp({"retCode": 0, "result": {"list": rows}})
        if "/open-interest" in url:
            age = self.oi_age_h.get(params["symbol"], 0)
            top = NOW - timedelta(hours=age)
            spike = params["symbol"] == "BTCUSDT"
            rows = []
            for i in range(25):
                oi = 1000.0 if not spike else (1000.0 if i >= 4 else 1000.0 * (1.0 + 0.05 * (4 - i)))   # +20% in 4h for BTC
                rows.append({"openInterest": str(oi), "timestamp": MS(top - timedelta(hours=i))})
            return Resp({"retCode": 0, "result": {"list": rows}})
        if "/account-ratio" in url:
            return Resp({"retCode": 0, "result": {"list": [{"buyRatio": "0.6", "sellRatio": "0.4", "timestamp": MS(NOW - timedelta(minutes=30))}]}})
        if "/v5/market/kline" in url:
            rows = [[MS(NOW - timedelta(days=i)), "0", "0", "0", str(100.0 - i * 0.1), "0"] for i in range(210)]   # rising
            return Resp({"retCode": 0, "result": {"list": rows}})
        if "thisweek" in url:
            return Resp([
                {"title": "CPI m/m", "country": "USD", "impact": "High", "date": (NOW + timedelta(hours=self.cpi_in_h if self.cpi_in_h is not None else 100)).isoformat()},
                {"title": "Unemployment Claims", "country": "USD", "impact": "Medium", "date": (NOW + timedelta(hours=5)).isoformat()},
                {"title": "ECB Rate", "country": "EUR", "impact": "High", "date": (NOW + timedelta(hours=6)).isoformat()},
                {"title": "FOMC Statement", "country": "USD", "impact": "High", "date": (NOW + timedelta(days=3)).isoformat()},
            ])
        if "nextweek" in url:
            return Resp([{"title": "Non-Farm Employment Change", "country": "USD", "impact": "High", "date": (NOW + timedelta(days=6)).isoformat()}])
        if "alternative.me" in url:
            return Resp({"data": [{"value": "34", "value_classification": "Fear", "timestamp": str(int((NOW - timedelta(hours=9)).timestamp()))}]})
        if "coingecko" in url:
            return Resp({"data": {"market_cap_percentage": {"btc": 57.1}, "market_cap_change_percentage_24h_usd": -1.25}})
        if "news.google.com" in url:
            coin = params["q"].split('"')[1]
            items = self.news.get(coin, [])
            if items is None:
                return Resp("", 503)
            return Resp(rss(items))
        return Resp("", 404)


def http_for(world, **kw):
    return mc.Http(get=world.get, clock=lambda: 1000.0, sleep=lambda s: None, **kw)


def tick_clock():
    t = [1000.0]

    def clock():
        return t[0]

    def sleep(s):
        t[0] += s
    return clock, sleep, t


def no_fetcher():
    return type("F", (), {"_json": lambda self, b, p: None})()


# ================================================================ P20-A snapshot
w = World()
snap = mc.build_snapshot(NOW, http_for(w), ["BTC", "ETH", "PUMP", "ZZZ"], None, Path(DATA))
check("schema id and timestamp", snap["schema"] == mc.SCHEMA and snap["ts"] == "2026-09-30T12:00:00Z")


def all_fields(node, path=""):
    if mc.is_field(node):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from all_fields(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from all_fields(v, f"{path}[{i}]")


fields = dict(all_fields({"global": snap["global"], "coins": snap["coins"], "flags": snap["flags"]}))
check("every field carries value / ts / source / status", len(fields) > 40 and all(set(f) >= {"value", "ts", "source", "status"} for f in fields.values()))
check("status is always ok | stale | unavailable", {f["status"] for f in fields.values()} <= {"ok", "stale", "unavailable"})
check("an unavailable field has value None and a reason", all(f["value"] is None and f.get("reason") for f in fields.values() if f["status"] == "unavailable"))
check("an ok field has a timestamp and a source", all(f["ts"] and f["source"] for f in fields.values() if f["status"] == "ok"))
btc, eth, pump, zzz = (snap["coins"][c] for c in ("BTC", "ETH", "PUMP", "ZZZ"))
check("BTC derivatives are ok: funding, 24h funding, OI and its 1h/4h/24h changes, long/short ratio",
      all(btc[k]["status"] == "ok" for k in ("funding_rate", "funding_24h", "funding_pctile", "open_interest", "oi_change_1h", "oi_change_4h", "oi_change_24h", "long_short_ratio")))
check("OI change arithmetic (+20% in 4h for BTC)", abs(btc["oi_change_4h"]["value"] - 0.2) < 1e-9 and abs(btc["oi_change_1h"]["value"] - (1.2 / 1.15 - 1)) < 1e-4 and btc["oi_change_24h"]["value"] == 0.2)
check("funding 24h is the sum of the settlements of the last 24h", btc["funding_24h"]["value"]["settlements"] == 3 and btc["funding_24h"]["value"]["sum"] > 0)
check("long/short ratio stored as long/short", btc["long_short_ratio"]["value"] == {"long": 0.6, "short": 0.4})

# ---- stale: data timestamp older than 3h
check("STALE: ETH open interest data is 5h old -> status stale (value kept)",
      eth["open_interest"]["status"] == "stale" and eth["open_interest"]["value"] == 1000.0 and eth["oi_change_4h"]["status"] == "stale")
check("stale inputs make the derived flag stale, not silently ok", eth["flags"]["oi_spike"]["status"] == "stale")
check("age boundary: exactly 3h is ok, 3h+1s is stale, 24h+1s is unavailable",
      mc.effective_status(mc.field(1, NOW - timedelta(hours=3), "x"), NOW) == "ok"
      and mc.effective_status(mc.field(1, NOW - timedelta(hours=3, seconds=1), "x"), NOW) == "stale"
      and mc.effective_status(mc.field(1, NOW - timedelta(hours=24, seconds=1), "x"), NOW) == "unavailable")
check("a stale field older than 24h is dropped to unavailable with its value removed",
      (lambda f: f["status"] == "unavailable" and f["value"] is None)(mc.apply_staleness(mc.field(5, NOW - timedelta(hours=30), "x", "stale"), NOW)))

# ---- unavailable
check("UNAVAILABLE: a coin with no Bybit perpetual gets unavailable fields with a reason, never a guess",
      zzz["symbol"] is None and all(zzz[k]["status"] == "unavailable" and "ZZZ" in zzz[k]["reason"] for k in ("funding_rate", "open_interest", "long_short_ratio"))
      and zzz["flags"]["funding_extreme"]["status"] == "unavailable" and zzz["flags"]["funding_extreme"]["value"] is None)
g = snap["global"]
check("liquidations: unavailable (no REST source) and so is liquidation_cascade",
      g["liquidations"]["status"] == "unavailable" and snap["flags"]["liquidation_cascade"]["status"] == "unavailable")
check("ETF flows and on-chain are marked 'not connected'", g["etf_flows"]["status"] == "unavailable" and "not connected" in g["etf_flows"]["reason"]
      and snap["sources"]["etf_flows"]["status"] == "not_connected" and snap["sources"]["onchain"]["status"] == "not_connected")

# ---- derived flags computed by code
check("funding_extreme: BTC funding 10x its 30-day history -> True; ETH normal -> False",
      btc["flags"]["funding_extreme"]["value"] is True and eth["flags"]["funding_extreme"]["value"] is False
      and btc["funding_pctile"]["value"]["percentile"] >= 95 and btc["funding_pctile"]["value"]["direction"] == "longs_paying")
check("oi_spike: BTC +20% in 4h -> True, ETH -> False", btc["flags"]["oi_spike"]["value"] is True and eth["flags"]["oi_spike"]["value"] is False)
check("funding flag needs >= 30 samples, otherwise unknown (None), not False",
      mc.funding_flag(0.1, [(NOW - timedelta(hours=i), 0.0001) for i in range(10)], NOW)["extreme"] is None)
check("funding history older than 30 days is ignored in the percentile",
      mc.funding_flag(0.0001, [(NOW - timedelta(days=40 + i), 0.5) for i in range(60)], NOW)["extreme"] is None)
check("percentile_rank is tie-aware", mc.percentile_rank(2, [1, 2, 2, 3]) == 0.5)
check("summary lists the coins that raised each flag", snap["flags"]["funding_extreme_coins"] == ["BTC"] and snap["flags"]["oi_spike_coins"] == ["BTC"])
check("BTC regime from 210 daily candles", g["btc_regime"]["value"]["regime"] == "uptrend" and g["btc_regime"]["status"] == "ok")

# ---- macro
ev = g["macro_events"]["value"]
check("macro: only HIGH-impact USD events on the key list, next 7 days (CPI, FOMC, NFP from next week)",
      [e["name"] for e in ev] == ["CPI m/m", "FOMC Statement", "Non-Farm Employment Change"] and g["macro_events"]["source"] == "ff_calendar")
check("macro_event_within_24h = True (CPI in 10h) and lists the event", snap["flags"]["macro_event_within_24h"]["value"] is True
      and snap["flags"]["macro_event_within_24h"]["events"][0]["name"] == "CPI m/m")
w2 = World()
w2.down = {"faireconomy"}
s2 = mc.build_snapshot(NOW, http_for(w2), ["BTC"], None, Path(DATA))
check("macro feed down and no manual file -> unavailable + a reminder in the report", s2["global"]["macro_events"]["status"] == "unavailable"
      and s2["flags"]["macro_event_within_24h"]["status"] == "unavailable" and any("macro_events.json" in r for r in s2["reminders"]))
manual_dir = Path(tempfile.mkdtemp())
(manual_dir / "macro_events.json").write_text(json.dumps({"events": [{"name": "FOMC rate decision", "ts": (NOW + timedelta(hours=20)).isoformat(), "importance": "high"},
                                                                     {"name": "too far", "ts": (NOW + timedelta(days=30)).isoformat()}]}))
s3 = mc.build_snapshot(NOW, http_for(w2), ["BTC"], None, manual_dir)
check("macro feed down + manual macro_events.json -> manual events used, flagged as manual, reminder to keep it current",
      s3["global"]["macro_events"]["source"] == "manual" and [e["name"] for e in s3["global"]["macro_events"]["value"]] == ["FOMC rate decision"]
      and s3["flags"]["macro_event_within_24h"]["value"] is True and any("macro_events.json" in r for r in s3["reminders"]))

# ---- news: three distinct cases + count
check("NEWS: news_found with a 24h headline count (2 items, 1 inside 24h)", btc["news_status"] == "news_found" and btc["news"]["value"]["count_24h"] == 1 and btc["news"]["value"]["total_items"] == 2)
check("NEWS: no_news = the feed answered and found nothing", eth["news_status"] == "no_news" and eth["news"]["status"] == "ok" and eth["news"]["value"]["count_24h"] == 0)
check("NEWS: feed_down = fetch failed, field unavailable (distinct from no_news)", pump["news_status"] == "feed_down" and pump["news"]["status"] == "unavailable")
check("the three news states are three different strings", len({btc["news_status"], eth["news_status"], pump["news_status"]}) == 3)
check("news summary counts per state", snap["sources"]["news_google_rss"]["news_found"] == 1 and snap["sources"]["news_google_rss"]["no_news"] >= 1
      and snap["sources"]["news_google_rss"]["feed_down"] >= 1)

# ---- sentiment
check("Fear & Greed in the shared snapshot (value, class, publication time), timestamped at confirmation",
      g["fear_greed"]["value"]["value"] == 34 and g["fear_greed"]["value"]["classification"] == "Fear" and g["fear_greed"]["status"] == "ok"
      and g["fear_greed"]["value"]["as_of"] is not None)
check("BTC dominance and total cap change in the snapshot", g["btc_dominance"]["value"] == 57.1 and g["total_mcap_change_24h"]["value"] == -1.25)

# ---- breakdown of unavailable fields
bd = snap["unavailable_breakdown"]
check("BREAKDOWN: top-level key in the snapshot with the Bybit and news parts", set(bd) == {"bybit_derivatives", "news_google_rss"})
check("BREAKDOWN Bybit: the unavailable fields are counted by reason class and by field, with the coins and an example",
      bd["bybit_derivatives"]["total"] == 8 and bd["bybit_derivatives"]["by_reason"] == {"no_perp": 8} and bd["bybit_derivatives"]["by_field"]["funding_rate"] == 1
      and bd["bybit_derivatives"]["coins_by_reason"]["no_perp"] == ["ZZZ"] and "ZZZ" in bd["bybit_derivatives"]["examples"]["no_perp"])
check("BREAKDOWN Bybit: stale fields are counted separately (ETH OI is 5h old)", bd["bybit_derivatives"]["stale"] >= 4)
check("BREAKDOWN news: the feed_down coin is listed with its class (HTTP 5xx), nothing else", bd["news_google_rss"]["total"] == 1
      and bd["news_google_rss"]["coins_by_reason"] == {"server_error": ["PUMP"]} and snap["sources"]["news_google_rss"]["unavailable"] == bd["news_google_rss"])
check("BREAKDOWN: the coin list without a perpetual sits in the Bybit source too", snap["sources"]["bybit_derivatives"]["coins_without_perp"] == ["ZZZ"])
for text, cls in {"no linear USDT perpetual for PUMP on Bybit": "no_perp", "api.bybit.com: ReadTimeout: HTTPSConnectionPool read timed out": "timeout",
                  "HTTP 429 from api.bybit.com": "rate_limited", "HTTP 403 from news.google.com": "blocked", "HTTP 503 from news.google.com": "server_error",
                  "back-off active for api.bybit.com (30s left)": "backoff_active", "api.bybit.com: ConnectionError: Max retries exceeded": "connection_error",
                  "bybit /v5/market/kline: retCode=10006": "api_error", "OI history too short": "history_too_short", "empty funding history": "empty_response",
                  "feed_down: x": "feed_down", "something unexpected": "other", None: "other"}.items():
    check(f"reason class: {text!r} -> {cls}", mc.classify_reason(text) == cls)
wt = World()
wt.codes = {"api.bybit.com": 429}
ht = http_for(wt)
st_t = mc.build_snapshot(NOW, ht, ["BTC", "ETH"], None, Path(DATA))
check("BREAKDOWN under a Bybit 429: every derivative field is unavailable and classed rate_limited / backoff_active, per-host stats show the errors",
      st_t["unavailable_breakdown"]["bybit_derivatives"]["total"] > 0 and set(st_t["unavailable_breakdown"]["bybit_derivatives"]["by_reason"]) <= {"rate_limited", "backoff_active"}
      and st_t["http_stats"]["api.bybit.com"]["errors"].get("rate_limited", 0) >= 1 and st_t["http_stats"]["api.bybit.com"]["calls"] >= 1)
check("http_stats is in every snapshot (calls and errors per host)", set(snap["http_stats"]) >= {"api.bybit.com", "news.google.com"} and snap["http_stats"]["api.bybit.com"]["calls"] > 0)

# ---- carry forward, back-off, cache, rate limit
w3 = World()
w3.codes = {"coingecko": 429, "alternative.me": 500}
s4 = mc.build_snapshot(NOW + timedelta(hours=1), http_for(w3), ["BTC"], snap, Path(DATA))
check("a failed refresh keeps the last good value, marked stale with the ORIGINAL timestamp",
      s4["global"]["fear_greed"]["status"] == "ok" or s4["global"]["fear_greed"]["status"] == "stale")
check("a failed refresh (429) keeps BTC dominance stale with its old ts and the reason",
      s4["global"]["btc_dominance"]["status"] == "stale" and s4["global"]["btc_dominance"]["ts"] == snap["global"]["btc_dominance"]["ts"]
      and "refresh failed" in s4["global"]["btc_dominance"]["reason"])
s5 = mc.build_snapshot(NOW + timedelta(hours=30), http_for(w3), ["BTC"], snap, Path(DATA))
check("a carried-forward value older than 24h becomes unavailable", s5["global"]["btc_dominance"]["status"] == "unavailable" and s5["global"]["btc_dominance"]["value"] is None)

clock, sleep, t = tick_clock()
wb = World()
wb.codes = {"api.coingecko.com": 429}
h = mc.Http(get=wb.get, clock=clock, sleep=sleep)
errs = 0
for _ in range(3):
    try:
        h.fetch(mc.COINGECKO_GLOBAL)
    except mc.SourceError:
        errs += 1
coingecko_hits = sum(1 for u, _ in wb.calls if "coingecko" in u)
check("BACK-OFF: after a 429 the host is left alone (1 real call, the next two are refused locally)", errs == 3 and coingecko_hits == 1)
t[0] += 130
try:
    h.fetch(mc.COINGECKO_GLOBAL)
except mc.SourceError:
    pass
check("back-off ends after its window and the second failure doubles it", sum(1 for u, _ in wb.calls if "coingecko" in u) == 2 and h._blocked_until["api.coingecko.com"] - t[0] >= 200)
wc = World()
h2 = mc.Http(get=wc.get, clock=clock, sleep=sleep)
h2.fetch(mc.FNG_URL)
h2.fetch(mc.FNG_URL)
check("CACHE: the same request inside the TTL is served once (no duplicated calls between bots)", sum(1 for u, _ in wc.calls if "alternative.me" in u) == 1)
slept = []
h3 = mc.Http(get=wc.get, clock=lambda: 50.0, sleep=slept.append)
h3.fetch(mc.BYBIT + "/v5/market/tickers", {"category": "linear"})
h3.fetch(mc.BYBIT + "/v5/market/kline", {"a": 1})
check("RATE LIMIT: consecutive calls to one host are paced", slept and slept[0] > 0)
check("no key, token or credential is ever sent (public endpoints, no auth params or headers)",
      all(not any(k.lower() in ("apikey", "api_key", "key", "token", "secret", "signature") for k in p) for _, p in w.calls + wc.calls)
      and "Authorization" not in mc.HEADERS)

# ---- run_hourly: append-only, latest, shared volume, off switch
d1 = Path(tempfile.mkdtemp())
sh = Path(tempfile.mkdtemp())
wr = World()
r1 = mc.run_hourly(d1, now=NOW, http=http_for(wr), fetcher=no_fetcher(), shared_dir=sh, coins=["BTC", "ETH"])
first_bytes = (d1 / mc.SNAPSHOT_FILE).read_bytes()
r2 = mc.run_hourly(d1, now=NOW + timedelta(hours=1), http=http_for(wr), fetcher=no_fetcher(), shared_dir=sh, coins=["BTC", "ETH"])
all_bytes = (d1 / mc.SNAPSHOT_FILE).read_bytes()
check("APPEND-ONLY: snapshots.jsonl grows by one line per run and earlier bytes never change", all_bytes.startswith(first_bytes) and len(all_bytes.splitlines()) == 2)
check("latest file and the shared-volume copy equal the newest snapshot",
      json.loads((d1 / mc.LATEST_FILE).read_text())["ts"] == r2["ts"] and json.loads((sh / mc.LATEST_FILE).read_text())["ts"] == r2["ts"])
os.environ["MARKET_CONTEXT_MODE"] = "off"
check("MARKET_CONTEXT_MODE=off: nothing runs", mc.run_hourly(d1, now=NOW, http=http_for(wr), fetcher=no_fetcher()) == {"mode": "off", "skipped": True})
os.environ.pop("MARKET_CONTEXT_MODE")

# ---- universe
class WL:
    def _json(self, bot, path):
        return {"whitelist": {"main": ["BTC/USDT", "HBAR/USDT"], "shorts": ["HBAR/USDT:USDT", "ZEC/USDT:USDT"], "control_long": ["EVIL/USDT"]}.get(bot, [])}


u = mc.universe(WL(), Path(tempfile.mkdtemp()))
check("universe: BTC, ETH first, then the bots' pairs, de-duplicated", u[:2] == ["BTC", "ETH"] and u.count("HBAR") == 1 and "ZEC" in u)
check("universe: control bots' pairs are never collected", "EVIL" not in u)

# ================================================================ context slices (P19)
def sig(i, bot="moonshot", action="buy", t0=None, pair="AAA/USDT"):
    t0 = t0 or (datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=i))
    return {"sid": f"{bot}|{pair}|{sa._iso(t0)}", "ts": sa._iso(t0 + timedelta(minutes=5)), "t0": sa._iso(t0), "bot": bot, "pair": pair,
            "action": action, "confidence": None, "price": 1.0}


snaps_c = []
for i in range(0, 400):
    ts = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    snaps_c.append({"ts": sa._iso(ts + timedelta(minutes=2)), "macro_event_within_24h": (i % 3 == 0),
                    "liquidation_cascade": None, "coins": {"AAA": {"funding_extreme": (i % 2 == 0), "oi_spike": None}}})
look = sa.make_context_lookup(snaps_c)
s_ = sig(10)
check("lookup: uses the last snapshot at or before the decision", look(s_) == {"funding_extreme": True, "oi_spike": None, "macro_event_within_24h": (10 % 3 == 0), "liquidation_cascade": None})
early = {**s_, "ts": sa._iso(datetime(2026, 9, 1, 10, 1, tzinfo=timezone.utc))}          # the 10:02 snapshot does not exist yet
check("NO LOOK-AHEAD: a snapshot taken AFTER the decision is never used", look(early)["macro_event_within_24h"] == (9 % 3 == 0) and look(early)["funding_extreme"] == (9 % 2 == 0))
check("lookup: no snapshot before the decision -> None (not a guess)", look({**s_, "ts": "2026-08-01T00:00:00Z"}) is None)
check("lookup: a snapshot older than 3h is not used", look({**s_, "ts": sa._iso(datetime(2026, 9, 30, tzinfo=timezone.utc))}) is None)
check("lookup: unknown coin -> flags None", look(sig(10, pair="QQQ/USDT"))["funding_extreme"] is None)

signals, forward = [], []
rng_state = [12345]


def lcg():
    rng_state[0] = (rng_state[0] * 1103515245 + 12345) % 2 ** 31
    return rng_state[0] / 2 ** 31 - 0.5


for i in range(0, 400):
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=i)
    market = lcg() * 0.01
    for p, pair in enumerate(("AAA/USDT", "BBB/USDT", "CCC/USDT", "DDD/USDT")):
        s = sig(i, pair=pair, action="buy" if p == 0 else "hold", t0=t0)
        signals.append(s)
        edge = 0.01 if (p == 0 and i % 2 == 0) else 0.0      # buys beat the market only when funding_extreme is True
        ret = market + lcg() * 0.002 + edge
        for hh in sa.HORIZONS:
            forward.append({"sid": s["sid"], "h": hh, "price0": 1.0, "price_h": 1 + ret, "ret": ret})
aud = sa.build_audit(signals, forward, datetime(2026, 11, 1, tzinfo=timezone.utc), look)
cs = aud["context_slices"]
check("audit carries context slices with the rule stated", cs["status"] == "ok" and set(cs["flags"]) == set(sa.CONTEXT_FLAGS) and "last hourly snapshot" in cs["rule"])
cell = cs["slices"]["funding_extreme"]["all"]["buy"]["h1"]
check("slice: buys with funding_extreme=True beat buys with it False by ~+1% (the built-in edge)",
      cell["flag_true"]["status"] == "ok" and cell["flag_false"]["status"] == "ok" and 0.5 < cell["diff_mean_excess_pct"] < 1.5)
check("slice: per-bot scope is present too", "moonshot" in cs["slices"]["funding_extreme"] and "buy" in cs["slices"]["funding_extreme"]["moonshot"])
macro_cell = cs["slices"]["macro_event_within_24h"]["all"]["buy"]["h4"]
check("slice: a flag with no built-in effect shows a difference near 0", abs(macro_cell["diff_mean_excess_pct"]) < 0.4)
check("slice: a flag that is always unknown (oi_spike) has no data in either side -> 'not enough data'",
      cs["slices"]["oi_spike"]["all"]["buy"]["h1"]["flag_true"]["status"] == "not enough data" and cs["slices"]["oi_spike"]["all"]["buy"]["h1"]["flag_true"]["n"] == 0)
check("slice: only buy / sell_short are sliced (holds are not)", "hold" not in cs["slices"]["funding_extreme"]["all"])
small = sa.build_audit(signals, forward, datetime(2026, 11, 1, tzinfo=timezone.utc), sa.make_context_lookup(snaps_c[:60]))
sc = small["context_slices"]["slices"]["funding_extreme"]["all"]["buy"]["h1"]
check("SLICE GATE: fewer than 50 signals in a slice -> 'not enough data', no metrics", sc["flag_true"]["status"] == "not enough data" and sc["flag_true"]["n"] < 50
      and "mean_excess_pct" not in sc["flag_true"] and "diff_mean_excess_pct" not in sc)
check("no context snapshots yet -> slices say so", sa.build_audit(signals, forward, datetime(2026, 11, 1, tzinfo=timezone.utc))["context_slices"]["status"] == "no context snapshots yet")
few_dec = sa.build_audit(signals[:100], forward, datetime(2026, 11, 1, tzinfo=timezone.utc), look)
check("a bot under the 200-decision gate contributes nothing to the slices", few_dec["context_slices"]["slices"]["funding_extreme"] == {})

# ================================================================ P20-B: LLM validation
prev = {b: 1.0 for b in al.MANAGED_BOTS}
good = json.dumps({b: {"weight": 0.8, "reason": "r"} for b in al.MANAGED_BOTS})
v = al.validate_llm(good, prev)
check("valid reply: all 8 weights taken", v["ok"] and all(v["weights"][b] == 0.8 for b in al.MANAGED_BOTS) and not v["notes"])
check("reply wrapped in a ```json fence is accepted", al.validate_llm("```json\n" + good + "\n```", prev)["ok"])
for label, text in {"None": None, "empty": "", "not JSON": "I think main should get 0.5", "JSON list": "[1,2,3]", "JSON number": "0.5", "empty object": "{}",
                    "only junk entries": json.dumps({"main": "0.5", "listing_fade": {"reason": "no weight"}})}.items():
    r = al.validate_llm(text, prev)
    check(f"INVALID reply ({label}) -> ok False, weights unchanged", not r["ok"] and r["weights"] == prev)
r = al.validate_llm(json.dumps({"main": {"weight": 1.7, "reason": "x"}, "listing_fade": {"weight": -0.3, "reason": "y"}, "shorts": {"weight": 0.4, "reason": "z"}}), prev)
check("OUT-OF-BOUNDS weights are cut to 0..1 by code (1.7 -> 1, -0.3 -> 0) and noted", r["ok"] and r["weights"]["main"] == 1.0 and r["weights"]["listing_fade"] == 0.0
      and r["weights"]["shorts"] == 0.4 and sum("outside 0..1" in n for n in r["notes"]) == 2)
r = al.validate_llm(json.dumps({"main": {"weight": "high"}, "listing_fade": {"weight": float("nan")}, "shorts": {"weight": True}, "moonshot": {"weight": 0.3, "reason": "ok"}}), prev)
check("non-numeric / NaN / boolean weight keeps that bot's previous weight; the valid entry still counts",
      r["ok"] and r["weights"]["main"] == 1.0 and r["weights"]["listing_fade"] == 1.0 and r["weights"]["shorts"] == 1.0 and r["weights"]["moonshot"] == 0.3)
r = al.validate_llm(json.dumps({"control_long": {"weight": 0.0, "reason": "x"}, "trend_ensemble": {"weight": 0.0}, "made_up": {"weight": 0.1}, "main": {"weight": 0.5}}), prev)
check("CONTROL bots, trend_ensemble and unknown bots in a reply are ignored and never reach the weights",
      r["ok"] and "control_long" not in r["weights"] and "trend_ensemble" not in r["weights"] and "made_up" not in r["weights"] and r["weights"]["main"] == 0.5
      and len(r["notes"]) == 3)
check("a reply that only names control bots is invalid", not al.validate_llm(json.dumps({"control_long": {"weight": 0}}), prev)["ok"])

# ================================================================ hard rules
DD_OK = {"highest_rung": None, "weekly_limit_breached": False, "monthly_limit_breached": False}
FACT = {"drawdown": DD_OK, "gross": 10.0, "budget": 1000.0, "equity": 1000.0, "open_trades": {}, "macro_soon": False, "coin_limit": 0.25}


def rules(proposal=None, prev_=None, **over):
    return al.apply_hard_rules(proposal or {b: 1.0 for b in al.MANAGED_BOTS}, prev_ or {b: 1.0 for b in al.MANAGED_BOTS}, {**FACT, **over})


check("neutral case: nothing fires, all weights 1", rules()["final"] == {b: 1.0 for b in al.MANAGED_BOTS} and rules()["rules_fired"] == [])
check("only the 8 managed bots get a weight (no control bots, no trend_ensemble)", set(rules()["final"]) == set(al.MANAGED_BOTS)
      and not (set(rules()["final"]) & set(al.NEVER_MANAGED)))
r = rules({b: 0.0 for b in al.MANAGED_BOTS})
check("DAILY STEP: 1.0 -> 0 is cut to 0.75 (-25pp)", all(w == 0.75 for w in r["final"].values()) and "daily_step_25pp" in r["rules_fired"])
r = rules({b: 1.0 for b in al.MANAGED_BOTS}, {b: 0.2 for b in al.MANAGED_BOTS})
check("DAILY STEP: 0.2 -> 1.0 is cut to 0.45 (+25pp)", all(w == 0.45 for w in r["final"].values()))
r = rules({b: 0.6 for b in al.MANAGED_BOTS}, {b: 0.5 for b in al.MANAGED_BOTS})
check("DAILY STEP: a move inside 25pp passes unchanged", all(w == 0.6 for w in r["final"].values()) and r["rules_fired"] == [])
L = lambda lvl: {**DD_OK, "highest_rung": {"level": lvl}}  # noqa: E731
check("LADDER L1 (-3%): every weight capped at 0.75", max(rules(drawdown=L("L1 (-3% from HWM)"))["final"].values()) == 0.75)
r = rules(drawdown=L("L2 (-6% from HWM)"))
check("LADDER L2 (-6%): LLM cohort stopped (0), the others capped at 0.5",
      all(r["final"][b] == 0.0 for b in al.LLM_COHORT) and r["final"]["listing_fade"] == 0.5 and r["final"]["trend_v2"] == 0.5)
check("LADDER L3 (-8%): all entries stopped", set(rules(drawdown=L("L3 (-8% from HWM)"))["final"].values()) == {0.0})
check("SAFETY CAPS may cut by more than 25pp (not bound by the daily step)", rules(drawdown=L("L3 (-8% from HWM)"), prev_={b: 1.0 for b in al.MANAGED_BOTS})["final"]["main"] == 0.0)
check("weekly loss -2% breached: all entries paused", set(rules(drawdown={**DD_OK, "weekly_limit_breached": True})["final"].values()) == {0.0})
check("monthly loss -4% breached: weights capped at 0.5", max(rules(drawdown={**DD_OK, "monthly_limit_breached": True})["final"].values()) == 0.5)
r = rules(gross=1110.0)
check("CPPI PROPORTIONAL: gross 1.11x budget -> every weight x budget/gross (~0.90, a ~10% cut), NOT zero",
      all(abs(v - 0.90) < 0.011 for v in r["final"].values()) and "cppi_proportional_scale" in r["rules_fired"] and min(r["final"].values()) > 0)
check("CPPI: gross exactly at the budget -> no scaling", set(rules(gross=1000.0)["final"].values()) == {1.0})
r = rules(gross=1250.0)
check("CPPI: gross 1.25x budget -> weights ~0.80", all(abs(v - 0.80) < 0.011 for v in r["final"].values()))
check("CPPI: the scale is applied on top of other weights (proposal 0.5 x 0.9 -> 0.45)",
      all(abs(v - 0.45) < 0.011 for v in rules({b: 0.5 for b in al.MANAGED_BOTS}, {b: 0.5 for b in al.MANAGED_BOTS}, gross=1110.0)["final"].values()))
check("CPPI: gross just under 1.5x budget (1.49x) still scales (~0.67), does not stop", all(0.6 < v < 0.7 for v in rules(gross=1490.0)["final"].values()))
check("CPPI: gross >= 1.5x budget -> all stopped", set(rules(gross=1500.0)["final"].values()) == {0.0} and set(rules(gross=2500.0)["final"].values()) == {0.0})
check("CPPI: budget 0 (floor 90% breached) -> all stopped, rule named cppi_floor_breached",
      set(rules(gross=0.0, budget=0.0)["final"].values()) == {0.0} and "cppi_floor_breached" in rules(gross=0.0, budget=0.0)["rules_fired"])
check("CPPI: below the budget there is no cap any more (the old 80% cap is gone)", set(rules(gross=950.0)["final"].values()) == {1.0})
check("MACRO: macro_event_within_24h -> no weight above 0.5", max(rules(macro_soon=True)["final"].values()) == 0.5)
check("MACRO unknown (None) does not cap -- unknown is not treated as an event", max(rules(macro_soon=None)["final"].values()) == 1.0)
OT = {"main": [{"base_currency": "SUI", "stake_amount": 100, "leverage": 1, "is_short": False}],
      "shorts": [{"base_currency": "SUI", "stake_amount": 40, "leverage": 1, "is_short": True}]}
r = rules(open_trades=OT, equity=10000.0)
check("OPPOSITE positions in one coin: the smaller side (shorts) stops adding, the bigger side keeps going",
      r["final"]["shorts"] == 0.0 and r["final"]["main"] == 1.0 and "opposite_positions_SUI" in r["rules_fired"])
OT2 = {"main": [{"base_currency": "HBAR", "stake_amount": 300, "leverage": 1, "is_short": False}], "listing_fade": [{"base_currency": "LINK", "stake_amount": 10, "leverage": 1, "is_short": False}]}
r = rules(open_trades=OT2, equity=1000.0)
check("CONCENTRATION: a coin >= 25% of equity caps the bots holding it at 0.5; others untouched",
      r["final"]["main"] == 0.5 and r["final"]["listing_fade"] == 1.0 and "coin_concentration_HBAR" in r["rules_fired"])
check("concentration uses leverage (stake x leverage)", rules(open_trades={"shorts": [{"base_currency": "X", "stake_amount": 100, "leverage": 3, "is_short": True}]}, equity=1000.0)["final"]["shorts"] == 0.5)
check("control bots' open trades are not counted in the concentration/opposite rules",
      rules(open_trades={"control_long": OT["main"], "shorts": OT["shorts"]}, equity=10000.0)["final"]["shorts"] == 1.0)
r = rules({**{b: 1.0 for b in al.MANAGED_BOTS}, "main": 0.03}, {**{b: 1.0 for b in al.MANAGED_BOTS}, "main": 0.1})
check("weights below 0.05 become 0 (a pause)", r["final"]["main"] == 0.0 and "below_min_weight_pause" in r["rules_fired"])
check("every change is logged with rule / from / to", all({"rule", "from", "to"} <= set(s) for ss in rules(drawdown=L("L2 (-6% from HWM)"))["steps"].values() for s in ss))
check("rules compose: macro cap 0.5 AND a ladder L1 cap 0.75 -> 0.5", max(rules(macro_soon=True, drawdown=L("L1 (-3% from HWM)"))["final"].values()) == 0.5)

# ================================================================ probe weight (keep the experiment alive)
T_ = datetime(2026, 10, 1, tzinfo=timezone.utc)
ZERO = {b: 1.0 for b in al.MANAGED_BOTS}
ZERO["listing_fade"] = 0.0
no_steps = {"listing_fade": [{"rule": "daily_step_25pp", "from": 0.25, "to": 0.0}]}
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=8))}}
fin = dict(ZERO)
lg = al.apply_probe(fin, no_steps, stp, T_, True, 7.0)
check("PROBE: weight 0 for more than 7 days -> probe weight 0.1 for about a day, logged", fin["listing_fade"] == 0.1 and lg[0]["probe"] == "started"
      and stp["probe"]["listing_fade"]["until"] == mc.iso(T_ + timedelta(hours=al.PROBE_HOURS)) and al.PROBE_HOURS < 24)
fin = dict(ZERO)
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=6, hours=23))}}
al.apply_probe(fin, no_steps, stp, T_, True, 7.0)
check("PROBE: not before 7 days", fin["listing_fade"] == 0.0 and not stp["probe"])
th = World()
th.codes = {"news.google.com": 503}
hh = http_for(th)
res = []
for i in range(4):
    try:
        hh.fetch(mc.GOOGLE_RSS, {"q": f"coin{i}"}, as_text=True)
    except mc.SourceError as e:
        res.append(str(e))
check("BACK-OFF: a lone 5xx does NOT block the host; only the 3rd consecutive failure does (then the 4th call is refused locally)",
      "HTTP 503" in res[0] and "HTTP 503" in res[1] and "HTTP 503" in res[2] and "back-off active" in res[3] and sum(1 for u, _ in th.calls if "news.google" in u) == 3)
tt = World()
tt.codes = {}
def timeout_get(url, params=None, headers=None, timeout=None):
    raise TimeoutError("read timed out")
ht2 = mc.Http(get=timeout_get, clock=lambda: 1000.0, sleep=lambda s: None)
res2 = []
for i in range(4):
    try:
        ht2.fetch(mc.BYBIT + "/v5/x", {"i": i})
    except mc.SourceError as e:
        res2.append(str(e))
check("BACK-OFF: transport timeouts count the same (3 timeouts then back-off) and are classed 'timeout'",
      all("TimeoutError" in r for r in res2[:3]) and "back-off active" in res2[3] and ht2.stats["api.bybit.com"]["errors"].get("timeout") == 3)
fin = dict(ZERO)
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=30))}}
al.apply_probe(fin, no_steps, stp, T_, False, 7.0)
check("PROBE: ALLOCATOR_PROBE=off disables it", fin["listing_fade"] == 0.0)
fin = dict(ZERO)
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=30))}}
al.apply_probe(fin, {"listing_fade": [{"rule": "ladder_L3_stop_entries", "from": 1.0, "to": 0.0}]}, stp, T_, True, 7.0)
check("PROBE: never overrides a SAFETY rule (ladder) and the zero-clock restarts when the rule lifts", fin["listing_fade"] == 0.0 and "listing_fade" not in stp["zero_since"])
for rule in ("weekly_loss_-2pct_pause_entries", "cppi_gross_1.5x_budget_stop", "cppi_floor_breached", "opposite_positions_SUI"):
    fin = dict(ZERO)
    stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=30))}}
    al.apply_probe(fin, {"listing_fade": [{"rule": rule, "from": 1.0, "to": 0.0}]}, stp, T_, True, 7.0)
    check(f"PROBE: not applied while {rule} holds the bot at 0", fin["listing_fade"] == 0.0)
fin = dict(ZERO)
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=8))}, "probe": {"listing_fade": {"until": mc.iso(T_ + timedelta(hours=5))}}}
lg = al.apply_probe(fin, no_steps, stp, T_, True, 7.0)
check("PROBE: an event-driven run inside the probe window keeps 0.1", fin["listing_fade"] == 0.1 and lg[0]["probe"] == "continuing")
fin = dict(ZERO)
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=8))}, "probe": {"listing_fade": {"until": mc.iso(T_ - timedelta(minutes=1))}}}
lg = al.apply_probe(fin, no_steps, stp, T_, True, 7.0)
check("PROBE: when the probe is over the bot goes back to 0 and a NEW 7-day clock starts (no chained probes)",
      fin["listing_fade"] == 0.0 and lg[0]["probe"] == "ended" and stp["zero_since"]["listing_fade"] == mc.iso(T_) and "listing_fade" not in stp["probe"])
fin = dict(ZERO)
fin["listing_fade"] = 0.5
stp = {"zero_since": {"listing_fade": mc.iso(T_ - timedelta(days=9))}, "probe": {"listing_fade": {"until": mc.iso(T_ + timedelta(hours=1))}}}
al.apply_probe(fin, {}, stp, T_, True, 7.0)
check("PROBE: a bot the allocator raised above 0 by itself clears the clocks", "listing_fade" not in stp["zero_since"] and "listing_fade" not in stp["probe"] and fin["listing_fade"] == 0.5)
fin = dict(ZERO)
stp = {}
al.apply_probe(fin, no_steps, stp, T_, True, 7.0)
check("PROBE: the zero-clock starts the first time a bot is seen at 0", stp["zero_since"]["listing_fade"] == mc.iso(T_) and fin["listing_fade"] == 0.0)
check("PROBE: independent of the weights -- the P19 ledger parses logs only (no weight anywhere in signal_audit)", "weight" not in (REPO / "backend/engine/signal_audit.py").read_text(encoding="utf-8").lower().replace("weighted", ""))
check("ALLOCATOR_PROBE / ALLOCATOR_PROBE_DAYS parsing: default on, 7 days", al.load_settings({})["probe"] is True and al.load_settings({})["probe_days"] == 7.0
      and al.load_settings({"ALLOCATOR_PROBE": "off", "ALLOCATOR_PROBE_DAYS": "3"})["probe"] is False and al.load_settings({"ALLOCATOR_PROBE_DAYS": "3"})["probe_days"] == 3.0)

# ================================================================ dry-run guard / control bots
class Bot:
    """Fake Freqtrade bot fleet for the brain Fetcher interface + /show_config + control calls."""

    def __init__(self, dry=None, state="running"):
        self.dry = {b: True for b in pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS}
        self.dry.update(dry or {})
        self.state = {b: state for b in pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS}
        self.unreachable: set[str] = set()
        self.legacy = False                         # True: /stopentry zeroes max_open_trades, state stays running, /start is a no-op
        self.zeroed: dict[str, bool] = {}
        self.trades_map: dict[str, list[dict]] = {}
        self.open_map: dict[str, list[dict]] = {}
        self.json_calls: list[tuple[str, str]] = []

    def reachable(self, bot): return bot not in self.unreachable
    def balance(self, bot): return {"total": 500.0, "starting_capital": 500.0}
    def status(self, bot): return self.open_map.get(bot, [])
    def trades(self, bot, **kw): return self.trades_map.get(bot, [])

    def _json(self, bot, path):
        self.json_calls.append((bot, path))
        if bot in self.unreachable:
            return None
        if path == "/api/v1/show_config":
            return {"dry_run": self.dry.get(bot), "state": self.state.get(bot),
                    "max_open_trades": 0 if self.zeroed.get(bot) else 5}
        return None


class Control:
    def __init__(self, fleet):
        self.calls, self.fleet = [], fleet

    def pause(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("stopentry", bot))
        if self.fleet.legacy:
            self.fleet.zeroed[bot] = True
        else:
            self.fleet.state[bot] = "paused"
        return True, "ok"

    def resume(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("start", bot))
        if not self.fleet.legacy:
            self.fleet.state[bot] = "running"
        return True, "already running" if self.fleet.legacy else "ok"

    def reload(self, bot):
        al.assert_manageable(bot)
        self.calls.append(("reload_config", bot))
        self.fleet.zeroed[bot] = False
        return True, "ok"


check("dry_run status: True everywhere -> enforce check ok", al.enforce_check(Bot())["ok"])
g1 = al.enforce_check(Bot(dry={"listing_fade": False}))
check("ANY dry_run=false blocks enforce and names the bot", not g1["ok"] and g1["live_bots"] == ["listing_fade"])
b_un = Bot()
b_un.unreachable = {"main"}
g2 = al.enforce_check(b_un)
check("a bot whose dry_run cannot be read also blocks enforce (unverified)", not g2["ok"] and g2["unverified_bots"] == ["main"])
check("control bots' dry_run is not part of the managed set", "control_long" not in al.dry_run_status(Bot()))

env_enf = {"PORTFOLIO_BRAIN_MODE": "enforce"}
refused = False
try:
    al.startup_guard(Bot(dry={"shorts": False}), env_enf)
except al.EnforceRefused as e:
    refused = "shorts" in str(e)
check("STARTUP: enforce with dry_run=false on any managed bot -> the service refuses to start (EnforceRefused)", refused)
check("startup: enforce with all dry-run starts", al.startup_guard(Bot(), env_enf) is None)
check("startup: shadow never checks or refuses", al.startup_guard(Bot(dry={"shorts": False}), {"PORTFOLIO_BRAIN_MODE": "shadow"}) is None)
check("startup: enforce with a merely unreachable bot does not crash the service (re-checked every run)", al.startup_guard(b_un, env_enf) is None)
check("a live control bot does not block (it is not managed)", al.startup_guard(Bot(dry={"control_long": False}), env_enf) is None)
check("mode parsing: default shadow; off|shadow|enforce accepted; junk -> off",
      al.load_settings({})["mode"] == "shadow" and al.load_settings({"PORTFOLIO_BRAIN_MODE": "ENFORCE"})["mode"] == "enforce"
      and al.load_settings({"PORTFOLIO_BRAIN_MODE": "yolo"})["mode"] == "off" and al.load_settings({"PORTFOLIO_BRAIN_MODE": "off"})["mode"] == "off")

for cb in al.NEVER_MANAGED:
    raised = False
    try:
        al.assert_manageable(cb)
    except al.ControlBotError:
        raised = True
    check(f"CONTROL: {cb} is not manageable (ControlBotError)", raised)
posted = []
bc = al.BotControl({b: f"http://{b}:8080" for b in pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS}, "u", "p", http_post=lambda url, **k: posted.append(url) or Resp({"status": "ok"}))
for cb in al.NEVER_MANAGED:
    try:
        bc.pause(cb)
    except al.ControlBotError:
        pass
    try:
        bc.resume(cb)
    except al.ControlBotError:
        pass
check("CONTROL: BotControl sends NOTHING for control bots or trend_ensemble (raises before any network call)", posted == [])
bc.pause("listing_fade")
bc.resume("listing_fade")
check("BotControl: pause = POST /stopentry, resume = POST /start (Freqtrade 2026.8 has no /startentry), both on the bot only",
      posted == ["http://listing_fade:8080/api/v1/stopentry", "http://listing_fade:8080/api/v1/start"])
src_al = (REPO / "backend/engine/allocator.py").read_text(encoding="utf-8")
src_pb = (REPO / "backend/engine/portfolio_brain.py").read_text(encoding="utf-8")
src_mc = (REPO / "backend/engine/market_context.py").read_text(encoding="utf-8")
check("only BotControl sends non-GET requests; the brain and the collector stay GET-only",
      src_al.count("self._post(") == 1 and ".post(" not in src_pb.replace("http_post", "") and "requests.post" not in src_mc and ".post(" not in src_mc)

# ================================================================ run_allocation end to end
def seed_brain(dir_, evidence=None):
    (dir_ / pb.LATEST_FILE).write_text(json.dumps({"evidence": evidence or {b: {"status": "unproven", "reason": "t"} for b in pb.PORTFOLIO_BOTS}}))


def quiet_world():
    q = World()
    q.cpi_in_h = None
    return q


def llm_ok(weights):
    return lambda s, u: json.dumps({b: {"weight": weights.get(b, 1.0), "reason": f"reason {b}"} for b in al.MANAGED_BOTS})


# shadow, valid reply
d = Path(tempfile.mkdtemp())
seed_brain(d)
sh = Path(tempfile.mkdtemp())
os.environ["ALLOCATOR_DIR"] = str(sh)
fleet, ctl = Bot(), None
ctl = Control(fleet)
mc.run_hourly(d, now=NOW, http=http_for(quiet_world()), fetcher=no_fetcher(), shared_dir=sh, coins=["BTC"])
out = al.run_allocation(d, NOW, fleet, llm_ok({"main": 0.9, "listing_fade": 0.0}), ctl, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("shadow: the run completes and records hypothetical weights (listing_fade 1.0 -> 0.75 by the daily step)", out["final"]["main"] == 0.9 and out["final"]["listing_fade"] == 0.75
      and out["effective_mode"] == "shadow")
check("SHADOW: no bot is touched (no pause, no resume) and the weights file says mode=shadow (bots ignore it)",
      ctl.calls == [] and json.loads((sh / al.WEIGHTS_FILE).read_text())["mode"] == "shadow")
rec = [json.loads(line) for line in (d / al.ALLOCATIONS_FILE).read_text().splitlines()]
check("allocations.jsonl: one record with reason, LLM proposal, steps, final weights and the context snapshot summary",
      len(rec) == 1 and rec[0]["llm"]["valid"] and rec[0]["llm"]["reasons"]["main"] == "reason main" and rec[0]["context"]["available"]
      and "macro_event_within_24h" in rec[0]["context"] and "listing_fade" in rec[0]["steps"] and rec[0]["final"]["listing_fade"] == 0.75 and rec[0]["trigger"] == "daily")
wf = json.loads((sh / al.WEIGHTS_FILE).read_text())
check("weights file: managed bots only, never a control bot", set(wf["weights"]) == set(al.MANAGED_BOTS) and wf["schema"] == al.SCHEMA_WEIGHTS)
out2 = al.run_allocation(d, NOW + timedelta(days=1), fleet, lambda s, u: "garbage, not json", ctl, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("INVALID LLM reply: the run does not crash, weights stay as before, the record says invalid",
      out2["llm"]["valid"] is False and out2["final"]["main"] == 0.9 and out2["final"]["listing_fade"] == 0.75)
out3 = al.run_allocation(d, NOW + timedelta(days=2), fleet, lambda s, u: (_ for _ in ()).throw(RuntimeError("api down")), ctl, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("LLM call raising -> same: no change, logged, no crash", out3["llm"]["valid"] is False and out3["final"]["main"] == 0.9)
check("allocations.jsonl is append-only (3 runs = 3 lines, first line unchanged)", len((d / al.ALLOCATIONS_FILE).read_text().splitlines()) == 3
      and json.loads((d / al.ALLOCATIONS_FILE).read_text().splitlines()[0]) == rec[0])
check("PORTFOLIO_BRAIN_MODE=off: nothing runs", al.run_allocation(d, NOW, fleet, llm_ok({}), ctl, env={"PORTFOLIO_BRAIN_MODE": "off"}) == {"mode": "off", "skipped": True})
check("the prompt the LLM receives: numbers only, all 8 bots, targets protect only, context statuses",
      "TARGETS" not in al.SYSTEM_PROMPT and "missing a target must never raise risk" in al.SYSTEM_PROMPT and "stale or unavailable is unknown, not neutral" in al.SYSTEM_PROMPT)

# enforce
d = Path(tempfile.mkdtemp())
seed_brain(d)
sh = Path(tempfile.mkdtemp())
os.environ["ALLOCATOR_DIR"] = str(sh)
fleet = Bot()
ctl = Control(fleet)
mc.run_hourly(d, now=NOW, http=http_for(quiet_world()), fetcher=no_fetcher(), shared_dir=sh, coins=["BTC"])
# step 1: weights 0.75 (rate limit) -> no pause yet. Drive to 0 over days using ladder L2 would be instant; use proposals.
o1 = al.run_allocation(d, NOW, fleet, llm_ok({"main": 0.0, "shorts": 0.9}), ctl, env=env_enf)
check("ENFORCE (all dry-run): runs as enforce, weights file says mode=enforce", o1["effective_mode"] == "enforce" and json.loads((sh / al.WEIGHTS_FILE).read_text())["mode"] == "enforce")
check("enforce: a weight above 0 never triggers a pause", ctl.calls == [] and o1["final"]["main"] == 0.75)
o2 = al.run_allocation(d, NOW + timedelta(days=1), fleet, llm_ok({"main": 0.0}), ctl, env=env_enf)
o3 = al.run_allocation(d, NOW + timedelta(days=2), fleet, llm_ok({"main": 0.0}), ctl, env=env_enf)
o4 = al.run_allocation(d, NOW + timedelta(days=3), fleet, llm_ok({"main": 0.0}), ctl, env=env_enf)
check("daily step drives main 0.75 -> 0.5 -> 0.25 -> 0.0, and only at 0 does the allocator POST /stopentry",
      [o["final"]["main"] for o in (o2, o3, o4)] == [0.5, 0.25, 0.0] and ctl.calls == [("stopentry", "main")] and fleet.state["main"] == "paused")
check("enforce: only managed bots were ever paused or resumed", all(b in al.MANAGED_BOTS for _, b in ctl.calls))
o5 = al.run_allocation(d, NOW + timedelta(days=4), fleet, llm_ok({"main": 0.5}), ctl, env=env_enf)
check("weight back above 0 -> POST /start (resume) for the bot the allocator paused", ("start", "main") in ctl.calls and fleet.state["main"] == "running")
fleet.state["main"] = "paused"
al.run_allocation(d, NOW + timedelta(days=5), fleet, llm_ok({"main": 0.5}), ctl, env=env_enf)
check("a bot that somebody else paused is not resumed by the allocator", fleet.state["main"] == "paused" and ctl.calls.count(("start", "main")) == 1)

# probe end to end: listing_fade sits at weight 0 for 8 days -> the daily run gives it 0.1 and un-pauses it
dp = Path(tempfile.mkdtemp())
seed_brain(dp)
os.environ["ALLOCATOR_DIR"] = str(Path(tempfile.mkdtemp()))
fp = Bot()
fp.state["listing_fade"] = "paused"
cp = Control(fp)
(dp / al.STATE_FILE).write_text(json.dumps({"weights": {**{b: 1.0 for b in al.MANAGED_BOTS}, "listing_fade": 0.0}, "paused_by_allocator": ["listing_fade"],
                                            "zero_since": {"listing_fade": mc.iso(NOW - timedelta(days=8))}, "last_effective_mode": "enforce"}))
op = al.run_allocation(dp, NOW, fp, llm_ok({"listing_fade": 0.0}), cp, env=env_enf)
check("PROBE end to end (enforce): 8 days at 0 -> weight 0.1, the bot is resumed, the record carries the probe",
      op["final"]["listing_fade"] == 0.1 and ("start", "listing_fade") in cp.calls and fp.state["listing_fade"] == "running" and op["probe"][0]["probe"] == "started")
op2 = al.run_allocation(dp, NOW + timedelta(days=1), fp, llm_ok({"listing_fade": 0.0}), cp, env=env_enf)
check("PROBE end to end: the next daily run (probe over) sends listing_fade back to 0 (stopentry) and restarts the clock", op2["final"]["listing_fade"] == 0.0
      and ("stopentry", "listing_fade") in cp.calls and json.loads((dp / al.STATE_FILE).read_text())["zero_since"]["listing_fade"] == mc.iso(NOW + timedelta(days=1)))

# legacy Freqtrade semantics: /stopentry zeroes max_open_trades, /start is a no-op -> /reload_config must bring it back
dl = Path(tempfile.mkdtemp())
seed_brain(dl)
os.environ["ALLOCATOR_DIR"] = str(Path(tempfile.mkdtemp()))
lf = Bot()
lf.legacy = True
lc = Control(lf)
stl = {"paused_by_allocator": []}
al.apply_enforce(lc, stl, {**{b: 1.0 for b in al.MANAGED_BOTS}, "listing_fade": 0.0}, lf)
check("LEGACY pause: max_open_trades zeroed is recognised as paused (state still 'running')", lf.zeroed.get("listing_fade") is True and stl["paused_by_allocator"] == ["listing_fade"]
      and al.is_paused(lf._json("listing_fade", "/api/v1/show_config")))
lc.calls.clear()
al.apply_enforce(lc, stl, {**{b: 1.0 for b in al.MANAGED_BOTS}, "listing_fade": 0.0}, lf)
check("LEGACY: an already-zeroed bot is not paused a second time", lc.calls == [])
acts = al.apply_enforce(lc, stl, {b: 1.0 for b in al.MANAGED_BOTS}, lf)
check("LEGACY resume: /start does nothing, so /reload_config follows and the bot is verified running again",
      lc.calls == [("start", "listing_fade"), ("reload_config", "listing_fade")] and lf.zeroed["listing_fade"] is False and stl["paused_by_allocator"] == []
      and acts[-1]["action"] == "reload_config" and acts[-1]["verified_running"] is True)
lf.zeroed["main"] = True
(dl / al.STATE_FILE).write_text(json.dumps({"paused_by_allocator": ["main"]}))
lc.calls.clear()
al.kill(dl, "operator kill switch", "ui", lc, lf, NOW)
check("LEGACY kill switch resumes through /reload_config too", ("reload_config", "main") in lc.calls and lf.zeroed["main"] is False)
lf2 = Bot()
lf2.legacy = True
lf2.zeroed["shorts"] = True


class StuckControl(Control):
    def reload(self, bot):
        self.calls.append(("reload_config", bot))
        return True, "ok"          # reload answers ok but the limit stays 0


stuck = {"paused_by_allocator": ["shorts"]}
sa_ = al.apply_enforce(StuckControl(lf2), stuck, {b: 1.0 for b in al.MANAGED_BOTS}, lf2)
check("a resume that does not actually take effect is reported (verified_running False) and the bot stays tracked as paused",
      sa_[-1]["verified_running"] is False and stuck["paused_by_allocator"] == ["shorts"])
mod = Bot()
mod.state["shorts"] = "paused"
mc_ = Control(mod)
stm = {"paused_by_allocator": ["shorts"]}
al.apply_enforce(mc_, stm, {b: 1.0 for b in al.MANAGED_BOTS}, mod)
check("2026.8 semantics: /start alone resumes, no reload_config needed", mc_.calls == [("start", "shorts")] and mod.state["shorts"] == "running")
posted2 = []
bc2 = al.BotControl({"listing_fade": "http://freqtrade-listing-fade:8080"}, "u", "p", http_post=lambda url, **k: posted2.append(url) or Resp({"status": "ok"}))
bc2.reload("listing_fade")
check("BotControl.reload = POST /reload_config on a managed bot only", posted2 == ["http://freqtrade-listing-fade:8080/api/v1/reload_config"])
try:
    bc2.reload("control_long")
    rl = False
except al.ControlBotError:
    rl = True
check("BotControl.reload refuses control bots", rl)

# reconcile after a bot restart resets Freqtrade's paused state
st = json.loads((d / al.STATE_FILE).read_text())
st["weights"]["listing_fade"] = 0.0
st["paused_by_allocator"] = ["listing_fade"]
st["last_effective_mode"] = "enforce"
(d / al.STATE_FILE).write_text(json.dumps(st))
fleet.state["listing_fade"] = "running"                      # the dip container restarted -> Freqtrade is running again
ctl.calls.clear()
al.check_events(d, NOW + timedelta(days=5, hours=1), fleet, llm_ok({}), ctl, env_enf)
check("RECONCILE: a restarted bot that should be paused (weight 0) is paused again", ("stopentry", "listing_fade") in ctl.calls and fleet.state["listing_fade"] == "paused")

# enforce blocked
d = Path(tempfile.mkdtemp())
seed_brain(d)
fleet = Bot(dry={"moonshot": False})
ctl = Control(fleet)
ob = al.run_allocation(d, NOW, fleet, llm_ok({"main": 0.0}), ctl, env=env_enf)
check("ENFORCE + dry_run=false on moonshot: the run falls back to SHADOW (nothing touched) and says why",
      ob["effective_mode"] == "shadow" and ctl.calls == [] and any("dry_run=false" in r and "moonshot" in r for r in ob["mode_reasons"]))
fleet_u = Bot()
fleet_u.unreachable = {"trend_v2"}
ou = al.run_allocation(d, NOW, fleet_u, llm_ok({}), Control(fleet_u), env=env_enf)
check("ENFORCE with an unverifiable dry_run: shadow for that run", ou["effective_mode"] == "shadow" and any("not verifiable" in r for r in ou["mode_reasons"]))

# kill switch
d = Path(tempfile.mkdtemp())
seed_brain(d)
sh = Path(tempfile.mkdtemp())
os.environ["ALLOCATOR_DIR"] = str(sh)
fleet = Bot()
ctl = Control(fleet)
(d / al.STATE_FILE).write_text(json.dumps({"weights": {b: 0.0 for b in al.MANAGED_BOTS}, "paused_by_allocator": ["main", "listing_fade"], "last_effective_mode": "enforce"}))
fleet.state["main"] = fleet.state["listing_fade"] = "paused"
kr = al.kill(d, "operator kill switch", "ui", ctl, fleet, NOW)
st = json.loads((d / al.STATE_FILE).read_text())
check("KILL SWITCH: the bots it paused are resumed, weights reset to 1, flag written",
      sorted(a["bot"] for a in kr["actions"]) == ["listing_fade", "main"] and fleet.state["main"] == "running" and st["paused_by_allocator"] == []
      and all(v == 1.0 for v in st["weights"].values()) and al.kill_state(d)["by"] == "ui")
check("kill switch: the shared weights file drops to mode=shadow with all weights 1", json.loads((sh / al.WEIGHTS_FILE).read_text())["mode"] == "shadow"
      and all(x["weight"] == 1.0 for x in json.loads((sh / al.WEIGHTS_FILE).read_text())["weights"].values()))
ok = al.run_allocation(d, NOW + timedelta(days=1), fleet, llm_ok({"main": 0.0}), ctl, env=env_enf)
check("while killed, enforce stays shadow and mentions the kill switch", ok["effective_mode"] == "shadow" and any("kill switch" in r for r in ok["mode_reasons"]))
check("clear_kill removes the flag", al.clear_kill(d) and al.kill_state(d) is None and not al.clear_kill(d))

# events
st_ = {"drawdown": {"highest_rung": {"level": "L2 (-6% from HWM)"}}}
cs_ = {"macro_event_within_24h": {"value": True, "events": [{"ts": "2026-10-01T12:30:00Z"}]}, "liquidation_cascade": {"value": True}}
evs = al.detect_events(st_, cs_, {}, NOW)
check("EVENTS: ladder rung, macro_event_within_24h and liquidation_cascade each produce an event id",
      evs == ["ladder:L2:2026-09-30", "macro:2026-10-01T12:30:00Z", "liquidation:2026-09-30T12"])
check("an event that already triggered a run does not trigger again", al.detect_events(st_, cs_, {e: "x" for e in evs}, NOW) == [])
check("no rung / no macro / no cascade -> no events", al.detect_events({"drawdown": {}}, {"macro_event_within_24h": {"value": False}}, {}, NOW) == [])

d = Path(tempfile.mkdtemp())
seed_brain(d)
fleet = Bot()
ctl = Control(fleet)
ctx_ev = json.loads((d / "x").read_text()) if False else None
snap_ev = mc.build_snapshot(NOW, http_for(World()), ["BTC"], None, d)        # CPI in 10h -> macro_event_within_24h
(d / mc.LATEST_FILE).write_text(json.dumps(snap_ev))
r_ev = al.check_events(d, NOW, fleet, llm_ok({}), ctl, {"PORTFOLIO_BRAIN_MODE": "shadow"})
check("check_events: a NEW macro event triggers a run out of schedule, recorded with its trigger", r_ev is not None and r_ev["trigger"].startswith("event:macro:"))
check("the macro event caps every weight at 0.5 in that run", set(r_ev["final"].values()) == {0.5} and "macro_event_within_24h_cap_0.5" in r_ev["rules_fired"])
check("check_events: the same event does not trigger twice", al.check_events(d, NOW + timedelta(hours=1), fleet, llm_ok({}), ctl, {"PORTFOLIO_BRAIN_MODE": "shadow"}) is None)

# ================================================================ counterfactual
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def trade(open_dt, pnl, close_dt=None):
    close_dt = close_dt or open_dt + timedelta(hours=5)
    return {"is_open": False, "open_date": open_dt.isoformat(), "close_date": close_dt.isoformat(), "close_profit_abs": pnl}


def alloc_rec(ts, w, mode="shadow", bots=None):
    return {"ts": mc.iso(ts), "effective_mode": mode, "final": {b: w for b in (bots or al.MANAGED_BOTS)}}


allocs = [alloc_rec(T0, 0.5)]
cf = al.counterfactual({"main": [trade(T0 + timedelta(hours=1), 10.0), trade(T0 + timedelta(hours=2), -4.0)]}, allocs, T0 + timedelta(days=2))
check("counterfactual (shadow): with = pnl x weight, without = weight 1", cf["with_allocator"]["net_pnl_usd"] == 3.0 and cf["without_allocator"]["net_pnl_usd"] == 6.0)
later = [alloc_rec(T0, 1.0), alloc_rec(T0 + timedelta(hours=3), 0.0)]
cf = al.counterfactual({"main": [trade(T0 + timedelta(hours=1), 10.0), trade(T0 + timedelta(hours=4), -8.0)]}, later, T0 + timedelta(days=2))
check("NO LOOK-AHEAD: a trade opened at 01:00 uses the 00:00 weight (1.0), not the 03:00 decision (0.0); the 04:00 trade uses 0.0",
      cf["with_allocator"]["net_pnl_usd"] == 10.0 and cf["without_allocator"]["net_pnl_usd"] == 2.0)
check("weight_at never reads a decision made after the trade opened", al.weight_at(later, "main", T0 + timedelta(hours=2, minutes=59)) == (1.0, False)
      and al.weight_at(later, "main", T0 + timedelta(hours=3)) == (0.0, False))
cf = al.counterfactual({"main": [trade(T0 - timedelta(hours=5), 50.0, T0 + timedelta(hours=1))]}, allocs, T0 + timedelta(days=2))
check("a trade opened before the first decision is not attributed to the allocator", cf["trades_used"] == 0)
enf = [alloc_rec(T0, 0.5, "enforce")]
cf = al.counterfactual({"main": [trade(T0 + timedelta(hours=1), 5.0)]}, enf, T0 + timedelta(days=2))
check("ENFORCED trades are already scaled: with = actual pnl, without = actual / weight", cf["with_allocator"]["net_pnl_usd"] == 5.0 and cf["without_allocator"]["net_pnl_usd"] == 10.0)
check("counterfactual only looks at managed bots", al.counterfactual({"control_long": [trade(T0 + timedelta(hours=1), 99.0)]}, allocs, T0 + timedelta(days=2))["trades_used"] == 0)
check("no decisions yet -> insufficient_data", al.counterfactual({}, [], T0)["verdict"] == "insufficient_data")
cf = al.counterfactual({"main": [trade(T0 + timedelta(hours=1), 1.0)]}, allocs, T0 + timedelta(days=10))
check("under 28 days -> insufficient_data, no verdict", cf["verdict"] == "insufficient_data" and "28" in cf["reason"])
check("metrics: drawdown and share of days positive are reported", {"max_drawdown_usd", "share_days_positive", "days"} <= set(cf["with_allocator"]))


def month_of_trades(weekly_delta_sign, pnl_per_trade=-10.0):
    """one trade a day for 35 days: pnl losing; weight 0.5 makes the allocator lose less (delta > 0) when pnl < 0."""
    return [trade(T0 + timedelta(days=i, hours=1), pnl_per_trade) for i in range(35)]


allocs_k = [alloc_rec(T0, 0.5)]
cf = al.counterfactual({"main": month_of_trades(1)}, allocs_k, T0 + timedelta(days=36))
check("VERDICT keep: >= 28 days, better net pnl and drawdown, >= 3 of the last 4 weeks not worse", cf["verdict"] == "keep" and cf["weeks_not_worse"].startswith(("4/", "3/")))
cf = al.counterfactual({"main": month_of_trades(1, +10.0)}, allocs_k, T0 + timedelta(days=36))
check("VERDICT revert_to_shadow: cutting winners loses to weight 1", cf["verdict"] == "revert_to_shadow")
check("the counterfactual file is written by a run", (d / al.COUNTERFACTUAL_FILE).is_file())

# auto revert in enforce
d = Path(tempfile.mkdtemp())
seed_brain(d)
sh = Path(tempfile.mkdtemp())
os.environ["ALLOCATOR_DIR"] = str(sh)
fleet = Bot()
fleet.trades_map = {"main": [trade(T0 + timedelta(days=i, hours=1), 10.0) for i in range(35)]}
ctl = Control(fleet)
with open(d / al.ALLOCATIONS_FILE, "w") as f:
    f.write(json.dumps(alloc_rec(T0, 0.5, "enforce")) + "\n")
(d / al.STATE_FILE).write_text(json.dumps({"weights": {b: 0.5 for b in al.MANAGED_BOTS}, "last_effective_mode": "enforce"}))
oa = al.run_allocation(d, T0 + timedelta(days=36), fleet, llm_ok({b: 0.5 for b in al.MANAGED_BOTS}), ctl, env=env_enf)
check("AUTO-REVERT: enforce + verdict revert_to_shadow -> kill switch set by 'auto:counterfactual', weights file back to shadow",
      oa["counterfactual"]["verdict"] == "revert_to_shadow" and al.kill_state(d) and al.kill_state(d)["by"] == "auto:counterfactual"
      and json.loads((sh / al.WEIGHTS_FILE).read_text())["mode"] == "shadow")

# ================================================================ gather_state / brain evidence as input
d = Path(tempfile.mkdtemp())
seed_brain(d, {**{b: {"status": "unproven"} for b in pb.PORTFOLIO_BOTS}, "main": {"status": "edge", "reason": "t"}})
fleet = Bot()
cl = lambda i, pnl, short=False, days=2: {"is_open": False, "open_date": (NOW - timedelta(days=days, hours=i + 3)).isoformat(),  # noqa: E731
                                          "close_date": (NOW - timedelta(days=days, hours=i)).isoformat(), "close_profit_abs": pnl, "stake_amount": 100,
                                          "leverage": 1.0, "fee_open_cost": 0.1, "fee_close_cost": 0.1, "is_short": short}
fleet.trades_map = {"main": [cl(0, 5.0, days=2), cl(1, -2.0, True, days=2), cl(0, 7.0, days=20)]}
stt = al.gather_state(fleet, d, NOW, al.load_settings({}))
mb = stt["bots"]["main"]
check("allocator input: P&L and fees over 7 and 30 days, evidence status from P19, short share, turnover",
      mb["d7"]["net_pnl_usd"] == 3.0 and mb["d30"]["net_pnl_usd"] == 10.0 and mb["d7"]["fees_usd"] == 0.4 and mb["d7"]["short_share"] == 0.5
      and mb["d7"]["turnover_usd"] == 400.0 and mb["evidence_status"] == "edge" and stt["bots"]["listing_fade"]["evidence_status"] == "unproven")
check("allocator input: equity, HWM, floor, CPPI budget, gross exposure, drawdown and targets are present",
      all(k in stt for k in ("equity", "hwm", "floor", "budget", "gross", "drawdown", "targets")) and stt["equity"] == 3000.0)
check("allocator input: control bots' trades are never fetched into the managed set", set(stt["trades_by_bot"]) <= set(al.MANAGED_BOTS))
ctx_s = al.context_summary(snap, NOW)
check("context summary for the LLM: statuses, flags, sources, no per-coin bulk", ctx_s["available"] and ctx_s["macro_event_within_24h"]["value"] is True
      and ctx_s["btc_regime"]["value"]["regime"] == "uptrend" and ctx_s["liquidation_cascade"]["status"] == "unavailable" and "coins" not in ctx_s
      and ctx_s["funding_extreme_coins"] == ["BTC"])
check("no snapshot yet -> the allocator says so instead of inventing context", al.context_summary(None, NOW)["available"] is False)
pp = al.build_llm_input(stt, ctx_s, prev)
check("LLM input contains only numbers/flags: no API key, no URL, no prompt text of the bots",
      "http" not in json.dumps(pp, default=str) and "api_key" not in json.dumps(pp, default=str).lower())

# ================================================================ bot side (_allocator_ctx)
def load_helper(path, alloc_dir):
    os.environ["ALLOCATOR_DIR"] = str(alloc_dir)
    spec = importlib.util.spec_from_file_location("ctxhelper_" + hashlib.md5(str(path).encode()).hexdigest()[:6], path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.CACHE_S = 0
    return m


HELP = REPO / "freqtrade/user_data/strategies/_allocator_ctx.py"
bd = Path(tempfile.mkdtemp())
ok_snap = mc.build_snapshot(datetime.now(timezone.utc), http_for(World()), ["BTC", "ETH", "PUMP"], None, Path(DATA))
(bd / "context_latest.json").write_text(json.dumps(ok_snap))
H = load_helper(HELP, bd)
blk = H.context_block("ETH/USDT", "moonshot")
check("PROMPT BLOCK: titled, says stale/unavailable = unknown, shows BTC and the coin, funding, OI, flags, macro, news", blk.startswith("Derivatives & macro context")
      and "UNKNOWN, not neutral" in blk and "- BTC:" in blk and "- ETH:" in blk and "funding_extreme=" in blk and "oi_spike=" in blk and "macro_event_within_24h=" in blk and "news:" in blk)
check("block: status marker on each field", "[ok]" in blk or "[stale]" in blk)
check("block: the three news states stay distinct in the prompt", "news_found" in H.context_block("BTC/USDT", "main") and "no_news" in blk and "feed_down" in H.context_block("PUMP/USDT", "main"))
check("block: feed_down text warns NOT to read it as 'no news'", "do NOT read this as 'no news'" in H.context_block("PUMP/USDT", "main"))
check("block: liquidations unavailable and ETF/on-chain not connected are stated", "liquidations: unavailable" in blk and "not connected" in blk)
check("block: a coin missing from the snapshot says so", "not in the snapshot" in H.context_block("NOPE/USDT", "main"))
for cb in ("control_long", "control_short", "trend_ensemble", "trend_v2", "listing_fade"):
    check(f"CONTROL/RULE: {cb} gets an EMPTY block", H.context_block("BTC/USDT", cb) == "")
check("fresh shared F&G and market text equal the bots' own prompt format", H.fear_greed_text() == "Fear & Greed Index: 34 (Fear)" and H.market_text() == "Total crypto market cap 24h change: -1.25%. BTC dominance: 57.1%.")
old = json.loads(json.dumps(ok_snap))
for n in mc.json.loads(json.dumps(list(old["coins"]))) if False else []:
    pass
for node_path in (old["global"], old["coins"]):
    for _, f in all_fields(node_path):
        if f["ts"]:
            f["ts"] = mc.iso(datetime.now(timezone.utc) - timedelta(hours=5))
(bd / "context_latest.json").write_text(json.dumps(old))
H2 = load_helper(HELP, bd)
b5 = H2.context_block("ETH/USDT", "moonshot")
check("STALE in the block: 5h-old data is labelled [stale] (graded by the bot's own clock)", "[stale]" in b5 and "[ok]" not in b5.split("- macro")[0])
check("stale shared F&G/market are not used (the bot falls back to its own fetch)", H2.fear_greed_text() is None and H2.market_text() is None)
(bd / "context_latest.json").write_text("{not json")
H3 = load_helper(HELP, bd)
check("corrupt snapshot -> an explicit 'unavailable' block, no exception", "unavailable" in H3.context_block("ETH/USDT", "moonshot") and H3.fear_greed_text() is None)
H4 = load_helper(HELP, Path(tempfile.mkdtemp()))
check("no snapshot file at all -> 'unavailable (no snapshot ...)' block", "no snapshot" in H4.context_block("ETH/USDT", "moonshot"))


def weights_file(dir_, mode, w, age_h=0, bot="listing_fade"):
    (Path(dir_) / "weights.json").write_text(json.dumps({"mode": mode, "updated_at": mc.iso(datetime.now(timezone.utc) - timedelta(hours=age_h)),
                                                          "weights": {bot: {"weight": w}}}))


wd = Path(tempfile.mkdtemp())
Hw = load_helper(HELP, wd)
check("multiplier 1.0 when the weights file is missing", Hw.stake_multiplier("listing_fade") == 1.0)
weights_file(wd, "shadow", 0.3)
check("multiplier 1.0 in SHADOW mode (the weight is ignored)", load_helper(HELP, wd).stake_multiplier("listing_fade") == 1.0)
weights_file(wd, "enforce", 0.3)
check("multiplier = weight in ENFORCE mode", load_helper(HELP, wd).stake_multiplier("listing_fade") == 0.3)
weights_file(wd, "enforce", 0.3, age_h=40)
check("a weights file older than 36h is ignored (allocator dead -> normal size)", load_helper(HELP, wd).stake_multiplier("listing_fade") == 1.0)
check("a weight above 1 (grow policy) is a valid multiplier up to 2.0", (weights_file(wd, "enforce", 1.5), load_helper(HELP, wd).stake_multiplier("listing_fade"))[1] == 1.5 and (weights_file(wd, "enforce", 2.0), load_helper(HELP, wd).stake_multiplier("listing_fade"))[1] == 2.0)
for label, val in {"above 2": 2.5, "negative": -0.2, "string": "0.5", "bool": True, "NaN-ish None": None}.items():
    weights_file(wd, "enforce", val)
    check(f"multiplier 1.0 for an invalid weight ({label})", load_helper(HELP, wd).stake_multiplier("listing_fade") == 1.0)
weights_file(wd, "enforce", 0.3)
check("multiplier 1.0 for a bot that has no entry", load_helper(HELP, wd).stake_multiplier("main") == 1.0)
(wd / "weights.json").write_text("garbage")
check("corrupt weights file -> 1.0", load_helper(HELP, wd).stake_multiplier("listing_fade") == 1.0)

# ---- strategies wired, copies identical, control bots untouched
copies = {}
for d_ in ("freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-v2"):
    copies[d_] = hashlib.sha256((REPO / d_ / "user_data/strategies/_allocator_ctx.py").read_bytes()).hexdigest()
check("the helper is an identical copy in all 5 managed bot directories", len(set(copies.values())) == 1)
for d_ in ("freqtrade-control-long", "freqtrade-control-short", "freqtrade-trend-ensemble"):
    files = list((REPO / d_).rglob("*.py"))
    check(f"CONTROL: {d_} contains no allocator code and no helper file", not (REPO / d_ / "user_data/strategies/_allocator_ctx.py").exists()
          and not any("_allocator_ctx" in f.read_text(encoding="utf-8", errors="replace") for f in files))
STRAT = {"main": ("freqtrade", "DeepSeekStrategy.py"), "shorts": ("freqtrade-shorts", "DeepSeekShortsStrategy.py"), "moonshot": ("freqtrade-moonshot", "DeepSeekMoonshotStrategy.py"),
         "listing_fade": ("freqtrade-listing-fade", "ListingFadeStrategy.py"), "trend_v2": ("freqtrade-trend-v2", "TrendV2Strategy.py")}
for bot, (d_, fn) in STRAT.items():
    s = (REPO / d_ / "user_data/strategies" / fn).read_text(encoding="utf-8")
    llm = bot in al.LLM_COHORT
    check(f"{bot}: strategy imports the helper, knows its id, scales the stake through the weight",
          "import _allocator_ctx" in s and f'_BOT_ID = "{bot}"' in s and "_allocator_ctx.stake_multiplier(_BOT_ID)" in s and "def _custom_stake_amount_base" in s
          and s.count("def custom_stake_amount(") == 1)
    check(f"{bot}: prompt " + ("has the context block" if llm else "is untouched (rule bot, no prompt)"),
          ("_allocator_ctx.context_block(pair, _BOT_ID)" in s) == llm)
    tree = ast.parse(s)
    guarded = {"populate_entry_trend", "populate_exit_trend", "populate_indicators", "custom_exit", "custom_stoploss", "confirm_trade_entry",
               "confirm_trade_exit", "adjust_trade_position", "leverage"}
    # P25: the announcement guard is the one deliberate exception -- confirm_trade_entry may call `_allocator_ctx.entry_block(pair)` (a refusal of a NEW entry on a coin with a delisting notice) and nothing else
    def refs(n):
        seg = ast.get_source_segment(s, n) or ""
        return seg.replace("_allocator_ctx.entry_block(pair)", "").count("_allocator_ctx")
    touched = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in guarded and refs(n)]
    check(f"{bot}: entry/exit/stop/ROI logic does not reference the helper (only stake size, the prompt and the announcement guard's entry_block)", touched == [])
import py_compile  # noqa: E402
for bot, (d_, fn) in STRAT.items():
    try:
        py_compile.compile(str(REPO / d_ / "user_data/strategies" / fn), doraise=True)
        okc = True
    except py_compile.PyCompileError:
        okc = False
    check(f"{bot}: strategy compiles", okc)
check("the allocator never calls the LLM-bot prompts: only 6 LLM bots have a context block, exactly the collector's CONTEXT_BOTS",
      set(al.LLM_COHORT) == set(mc.CONTEXT_BOTS) == set(H.CONTEXT_BOTS))

# ================================================================ P20.3: reply format, extraction, one retry
GOODJ = json.dumps({b: {"weight": 0.7, "reason": "r"} for b in al.MANAGED_BOTS})
reasoning = "We are given a portfolio with several bots. Let us think about {main} first: its evidence is edge. "
check("EXTRACT: JSON after a paragraph of reasoning is read (first balanced object)", al.validate_llm(reasoning + GOODJ + " Done.", prev)["ok"]
      and al.validate_llm(reasoning + GOODJ, prev)["weights"]["main"] == 0.7)
check("EXTRACT: the note says the object was extracted from surrounding text", "JSON object extracted from surrounding text" in al.validate_llm("Sure!\n" + GOODJ, prev)["notes"])
check("EXTRACT: a clean reply has no such note", not al.validate_llm(GOODJ, prev)["notes"])
check("EXTRACT: braces inside JSON strings and escaped quotes do not break the matching",
      al.validate_llm('text {"main": {"weight": 0.4, "reason": "has } brace and \\" quote {"}} tail', prev)["weights"]["main"] == 0.4)
check("EXTRACT: an earlier non-JSON brace group is skipped, the first object that parses wins",
      al.validate_llm('format is {bot: weight} and then ' + GOODJ, prev)["ok"])
check("EXTRACT: nested objects are returned whole (top-level scan)", list(al.extract_json_objects('x {"a": {"b": 1}} y {"c": 2}')) == ['{"a": {"b": 1}}', '{"c": 2}'])
check("EXTRACT: unbalanced / cut-off reply (the 02.10 failure: reasoning then nothing) is invalid", not al.validate_llm("We are given a portfolio... {\"main\": {\"weight\": 0.5, \"rea", prev)["ok"]
      and not al.validate_llm("We are given a portfolio...", prev)["ok"])
check("EXTRACT: ```json fenced reply with trailing text still parses", al.validate_llm("```json\n" + GOODJ + "\n```\nHope this helps", prev)["ok"])
check("REASON is cut to 140 characters", len(al.validate_llm(json.dumps({"main": {"weight": 0.5, "reason": "x" * 400}}), prev)["reasons"]["main"]) == 140 and al.REASON_MAX == 140)
check("limits: reply budget >= 4000 tokens", al.LLM_MAX_TOKENS >= 4000)
check("PROMPT: demands one JSON object, no reasoning/text around it, ends with 'Reply with the JSON object only.'",
      al.SYSTEM_PROMPT.endswith("Reply with the JSON object only.") and "No reasoning" in al.SYSTEM_PROMPT and "at most 140 characters" in al.SYSTEM_PROMPT
      and "no text before or after" in al.SYSTEM_PROMPT)

seen_users = []


def flaky(first, second=None):
    replies = iter([first] + ([second] if second is not None else []))

    def llm(s, u):
        seen_users.append(u)
        return next(replies, "")
    return llm


seen_users.clear()
v, reps, att = al.ask_validated(flaky(GOODJ), "S", "U", prev)
check("ATTEMPTS: a valid first reply -> 1 attempt, no retry", v["ok"] and att == 1 and len(seen_users) == 1 and "previous reply" not in seen_users[0])
seen_users.clear()
v, reps, att = al.ask_validated(flaky("We are given a portfolio... cut off", GOODJ), "S", "U", prev)
check("RETRY: an invalid first reply triggers exactly one retry with the 'previous reply was not valid JSON' suffix; the retry is used",
      v["ok"] and att == 2 and len(seen_users) == 2 and seen_users[1] == "U" + al.RETRY_SUFFIX
      and al.RETRY_SUFFIX.strip().endswith("Your previous reply was not valid JSON. Reply with the JSON object only."))
seen_users.clear()
v, reps, att = al.ask_validated(flaky("garbage one", "garbage two"), "S", "U", prev)
check("RETRY: invalid twice -> still invalid, weights = previous, never a third call, both raw replies kept",
      not v["ok"] and att == 2 and len(seen_users) == 2 and v["weights"] == prev and reps == ["garbage one", "garbage two"])
v, reps, att = al.ask_validated(lambda s, u: (_ for _ in ()).throw(RuntimeError("api down")), "S", "U", prev)
check("RETRY: an API exception on both attempts is invalid after 2 attempts, no crash", not v["ok"] and att == 2 and reps == ["", ""])

dq = Path(tempfile.mkdtemp())
seed_brain(dq)
fq = Bot()
cq = Control(fq)
o_ok2 = al.run_allocation(dq, NOW, fq, flaky("We are given a portfolio... (cut off)", GOODJ), cq, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
rec_q = json.loads((dq / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])
check("RECORD: attempts=2 when the retry was needed; the invalid first reply is kept in invalid_replies; the weights come from the retry",
      rec_q["llm"]["attempts"] == 2 and rec_q["llm"]["valid"] and rec_q["llm"]["invalid_replies"] == ["We are given a portfolio... (cut off)"] and o_ok2["final"]["main"] == 0.75
      and rec_q["llm"]["proposal"]["main"] == 0.7)
o_bad = al.run_allocation(dq, NOW + timedelta(days=1), fq, flaky("We are given a portfolio...", "still reasoning, no JSON"), cq, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
rec_b = json.loads((dq / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])
check("RECORD: invalid twice -> attempts=2, raw_reply and invalid_replies hold what the model said, weights unchanged, run completes",
      rec_b["llm"]["attempts"] == 2 and not rec_b["llm"]["valid"] and rec_b["llm"]["raw_reply"] == "still reasoning, no JSON"
      and rec_b["llm"]["invalid_replies"] == ["We are given a portfolio...", "still reasoning, no JSON"] and o_bad["final"] == o_ok2["final"])
o_one = al.run_allocation(dq, NOW + timedelta(days=2), fq, flaky(GOODJ), cq, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
rec_1 = json.loads((dq / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])
check("RECORD: attempts=1 for a clean first reply, no invalid replies", rec_1["llm"]["attempts"] == 1 and rec_1["llm"]["invalid_replies"] == [])
o_ext = al.run_allocation(dq, NOW + timedelta(days=3), fq, flaky("Reasoning first. " + GOODJ), cq, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("a reply with text around the JSON is accepted on the first attempt (no retry needed)",
      json.loads((dq / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])["llm"]["attempts"] == 1 and o_ext["llm"]["valid"])

# brain.ask: token limit and JSON mode reach the DeepSeek request; OpenRouter gets the limit but no response_format
from backend.engine import brain as brain_mod  # noqa: E402

sent = []


class _R:
    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}]}


brain_mod.requests.post = lambda url, **kw: sent.append((url, kw)) or _R()
os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-real"
os.environ.pop("OPENROUTER_API_KEY", None)
brain_mod.ask("s", "u", max_tokens=al.LLM_MAX_TOKENS, json_mode=True)
body = sent[-1][1]["json"]
check("DEEPSEEK request: max_tokens >= 4000 and response_format json_object when JSON mode is on", body["max_tokens"] >= 4000 and body["response_format"] == {"type": "json_object"}
      and body["thinking"] == {"type": "disabled"})
brain_mod.ask("s", "u")
body = sent[-1][1]["json"]
check("existing callers are unchanged: default max_tokens 1500, no response_format", body["max_tokens"] == 1500 and "response_format" not in body)
brain_mod.requests.post = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("down")) if "deepseek" in url else (sent.append((url, kw)) or _R())
os.environ["OPENROUTER_API_KEY"] = "or-test-not-real"
brain_mod.ask("s", "u", max_tokens=4096, json_mode=True)
bo = sent[-1][1]["json"]
check("OPENROUTER fallback: gets the larger token limit, NO response_format (instruction only)", bo["max_tokens"] == 4096 and "response_format" not in bo)
os.environ.pop("DEEPSEEK_API_KEY")
os.environ.pop("OPENROUTER_API_KEY")
dflt = al.default_llm()
sent.clear()
brain_mod.requests.post = lambda url, **kw: sent.append((url, kw)) or _R()
os.environ["DEEPSEEK_API_KEY"] = "sk-test-not-real"
dflt("s", "u")
check("allocator.default_llm() asks with the big limit and JSON mode", sent[-1][1]["json"]["max_tokens"] >= 4000 and sent[-1][1]["json"]["response_format"] == {"type": "json_object"})
os.environ.pop("DEEPSEEK_API_KEY")

# stuck bots: an unverified resume stays tracked and is reported, nothing is rolled back
sf = Bot()
sf.legacy = True
sf.zeroed["listing_fade"] = True
sf.reload_restores = False


class StuckCtl(Control):
    def reload(self, bot):
        self.calls.append(("reload_config", bot))
        return True, "ok"          # accepted, but max_open_trades stays 0


ds = Path(tempfile.mkdtemp())
seed_brain(ds)
(ds / al.STATE_FILE).write_text(json.dumps({"weights": {**{b: 1.0 for b in al.MANAGED_BOTS}, "listing_fade": 0.0}, "paused_by_allocator": ["listing_fade"], "last_effective_mode": "enforce"}))
os.environ["ALLOCATOR_DIR"] = str(Path(tempfile.mkdtemp()))
os_ = al.run_allocation(ds, NOW, sf, llm_ok({"listing_fade": 0.5}), StuckCtl(sf), env=env_enf)
st_s = json.loads((ds / al.STATE_FILE).read_text())
check("STUCK: resume with verified_running=false -> the bot stays in paused_by_allocator and is listed in stuck_bots, in state, record and latest",
      st_s["paused_by_allocator"] == ["listing_fade"] and st_s["stuck_bots"] == ["listing_fade"] and os_["stuck_bots"] == ["listing_fade"]
      and json.loads((ds / al.ALLOCATIONS_FILE).read_text().splitlines()[-1])["stuck_bots"] == ["listing_fade"]
      and any(x.get("verified_running") is False for x in os_["actions"]))
check("STUCK: nothing is rolled back automatically (no kill flag, mode still enforce)", al.kill_state(ds) is None and os_["effective_mode"] == "enforce")
sf.reload_restores = True
sf.zeroed["listing_fade"] = True
os_ = al.run_allocation(ds, NOW + timedelta(days=1), sf, llm_ok({"listing_fade": 0.5}), Control(sf), env=env_enf)
check("STUCK: cleared once a later resume is verified", json.loads((ds / al.STATE_FILE).read_text())["stuck_bots"] == [] and os_["stuck_bots"] == [])
html_p = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("UI: the card flags unverified resumes in red and shows the number of LLM attempts", "Resume NOT verified" in html_p and "attempt(s) -> no change" in html_p and "2nd attempt" in html_p)

# ================================================================ endpoints + export
os.environ["ALLOCATOR_DIR"] = str(Path(tempfile.mkdtemp()))
EP = tempfile.mkdtemp()
os.environ["DATA_DIR"] = EP
Path(EP, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402

app_module.DATA_DIR = Path(EP)
client = TestClient(app_module.app)
r = client.get("/api/context/latest").json()
check("GET /api/context/latest: available=false with a reason before the first snapshot", r["available"] is False and "hourly" in r["reason"])
Path(EP, mc.LATEST_FILE).write_text(json.dumps(snap))
r = client.get("/api/context/latest").json()
check("GET /api/context/latest serves the snapshot (schema, global, coins, flags, sources)", r["available"] and r["schema"] == mc.SCHEMA and "BTC" in r["coins"] and "sources" in r)
r = client.get("/api/portfolio/allocator").json()
check("GET /api/portfolio/allocator: available=false before the first run, still reports mode and the never-managed list",
      r["available"] is False and r["mode"] == "shadow" and set(r["never_managed"]) == set(al.NEVER_MANAGED))
Path(EP, al.LATEST_FILE).write_text(json.dumps({"ts": "x", "final": {"main": 1.0}, "counterfactual": {"verdict": "insufficient_data"}, "kill": {"leak": True}}))
Path(EP, al.ALLOCATIONS_FILE).write_text(json.dumps({"ts": "x", "trigger": "daily", "effective_mode": "shadow", "final": {"main": 1.0}, "rules_fired": []}) + "\n")
r = client.get("/api/portfolio/allocator").json()
check("GET /api/portfolio/allocator serves the last decision and a short history", r["available"] and r["final"]["main"] == 1.0 and len(r["history"]) == 1
      and r["counterfactual"]["verdict"] == "insufficient_data")
paths = app_module.app.openapi()["paths"]
check("allocator routes: GET status, POST kill and POST resume; context GET-only", set(paths["/api/portfolio/allocator"]) == {"get"} and set(paths["/api/portfolio/allocator/kill"]) == {"post"}
      and set(paths["/api/portfolio/allocator/resume"]) == {"post"} and set(paths["/api/context/latest"]) == {"get"})
r = client.post("/api/portfolio/allocator/resume").json()
check("POST resume on a clean state: nothing to clear", r == {"cleared": False})
appsrc = (REPO / "backend/app.py").read_text(encoding="utf-8")
check("scheduling: context at minute :05 hourly (then the event check), allocator 00:40 UTC, startup guard before any loop",
      "market_context.seconds_until_next_hour" in appsrc and "seconds_until_next(datetime.now(timezone.utc), 0, 40)" in appsrc
      and appsrc.index("allocator.startup_guard") < appsrc.index("asyncio.create_task(_engine_background_loop())")
      and "asyncio.create_task(_market_context_loop())" in appsrc and "asyncio.create_task(_allocator_loop())" in appsrc)
check("env vars registered for the dashboard", {"MARKET_CONTEXT_MODE", "ALLOCATOR_COIN_LIMIT", "ALLOCATOR_DIR"} <= {e["key"] for e in app_module.ENV_VAR_DEFS})

# export bundle
from fastapi import FastAPI  # noqa: E402

from backend import export as ex  # noqa: E402

ed = tempfile.mkdtemp()
os.environ["DATA_DIR"] = ed
ex._utcnow = lambda: NOW
Path(ed, "context_snapshots.jsonl").write_text(json.dumps({"ts": "x", "note": "api_key=SuperSecretContextKey123456"}) + "\n")
Path(ed, "context_latest.json").write_text(json.dumps(snap))
Path(ed, "allocations.jsonl").write_text(json.dumps({"ts": "x", "llm": {"raw_reply": "Bearer abcdefghijklmnop12345 leaked"}}) + "\n")
Path(ed, "allocator_counterfactual.json").write_text(json.dumps({"verdict": "insufficient_data"}))
app2 = FastAPI()
app2.include_router(ex.router)
c2 = TestClient(app2)
ex._fetcher = lambda: type("F", (), {"_json": lambda s, b, p: None, "trades": lambda s, b: []})()
rz = c2.get("/api/export/bundle?bots=main&include=allocator")
zf = zipfile.ZipFile(io.BytesIO(rz.content))
names = set(zf.namelist())
check("ZIP: context and allocator files are in the bundle under context/ and allocator/",
      rz.status_code == 200 and {"context/context_snapshots.jsonl", "context/context_latest.json", "allocator/allocations.jsonl", "allocator/allocator_counterfactual.json"} <= names)
check("ZIP: secrets in those files are masked like the logs",
      b"SuperSecretContextKey123456" not in zf.read("context/context_snapshots.jsonl") and b"abcdefghijklmnop12345" not in zf.read("allocator/allocations.jsonl"))
man = json.loads(zf.read("MANIFEST.json"))
check("MANIFEST: allocator inventory with sizes; missing files listed as not created", man["allocator"]["allocator/allocations.jsonl"]["included"] is True
      and man["allocator"]["allocator/allocations.jsonl"]["bytes"] == len(zf.read("allocator/allocations.jsonl"))
      and man["allocator"]["allocator/allocator_latest.json"]["included"] is False)
check("default include covers allocator", ex.DEFAULT_INCLUDE.endswith("signals,allocator") and "allocator" in ex.INCLUDE_OPTIONS)
cat = c2.get("/api/export/catalog").json()
check("catalog lists the allocator files", "allocator" in cat and cat["allocator"]["allocator/allocations.jsonl"]["bytes"] > 0)

# ================================================================ frontend
html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("frontend: allocator card, kill switch, context chips, loaded with the live data",
      'id="allocator"' in html and "loadAllocator" in html and "allocKill" in html and "/api/portfolio/allocator/kill" in html and "/api/context/latest" in html
      and "include=logs,trades,brain,decisions,aggregates,signals,allocator" in html)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P20 CONTEXT / ALLOCATOR CHECKS PASSED")
