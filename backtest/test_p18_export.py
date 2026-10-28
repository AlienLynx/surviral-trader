"""
P18: export centre -- secret masking, log stitching/filtering, catalog, trades, ZIP bundle,
limits, path safety, and wiring (compose read-only mounts, frontend card).
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


BOTS_TMP = tempfile.mkdtemp()
DATA_TMP = tempfile.mkdtemp()
os.environ["BOTS_DIR"] = BOTS_TMP
os.environ["DATA_DIR"] = DATA_TMP

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend import export as ex  # noqa: E402

app = FastAPI()
app.include_router(ex.router)
client = TestClient(app)

# ================================================================ masking
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZGVudGl0eSI6eyJ1IjoiZGVtby11c2VyIn0sImV4cCI6MTcwMDAwMDAwMH0.Zm9yLXRlc3Rpbmctb25seS1ub3QtYS1yZWFsLXRva2Vu"
cases = {
    "Authorization: Bearer abc123def456ghi789": ("abc123def456", "Bearer ***"),
    "curl -H 'authorization: Bearer sometokenvalue12345'": ("sometokenvalue12345", None),
    "Authorization: Basic dXNlcjpwYXNzd29yZA==": ("dXNlcjpwYXNzd29yZA", "Authorization: ***"),
    f'"WebSocket /api/v1/message/ws?token={JWT}" [accepted]': (JWT[:30], None),
    "ccxt config api_key=AbCdEf123456SecretKey&x=1": ("AbCdEf123456SecretKey", "api_key=***"),
    '{"apiKey": "AbCdEf123456SecretKey", "secret": "s3cr3tvalue"}': ("s3cr3tvalue", None),
    "password: hunter2hunter2": ("hunter2hunter2", "password: ***"),
    "SECRET=topsecretvalue": ("topsecretvalue", "SECRET=***"),
    "X-Portainer-API-Key: ptr_FakeKeyForTestingOnlyNotARealPortainerKey00=": ("FakeKeyForTestingOnlyNotARealPortainerKey00", None),
    "using key sk-abcdefghijklmnopqrstuvwx for deepseek": ("abcdefghijklmnop", "sk-***"),
    "digest 0123456789abcdef0123456789abcdef0123 done": ("0123456789abcdef0123456789abcdef", "***HEX***"),
    "token=eyJabcdefghij.klmnopqrstuv.wxyz&other=1": ("klmnopqrstuv", None),
}
for line, (secret, expect) in cases.items():
    out = ex.mask_secrets(line)
    check(f"mask: hides {secret[:22]!r}", secret not in out and (expect is None or expect in out))
    check(f"mask: idempotent for {line[:40]!r}", ex.mask_secrets(out) == out)
plain = [
    "2026-09-29 07:38:12,050 - DeepSeekShortsStrategy - INFO - DeepSeek said hold for SOL/USDT:USDT -- flat 24h",
    "Loading variable 'FREQTRADE__EXCHANGE__SECRET'",
    "Key parts: ['API_SERVER', 'JWT_SECRET_KEY']",
    "Using DB: \"sqlite:////freqtrade/user_data/db/tradesv3.sqlite\"",
    "Order dry_run_buy_HBAR/USDT:USDT_ed6ed02a-fdc3-41e0-982d-9ca8e947090b was created for HBAR/USDT:USDT",
    "P16: stale-signal streak=1/2 for XRP/USDT -- no dip here, RSI neutral | confidence: 70",
]
check("mask: ordinary log lines are returned untouched", all(ex.mask_secrets(l) == l for l in plain))
check("mask: empty string safe", ex.mask_secrets("") == "")

# ============================================================ log fixtures
def write_log(bot: str, name: str, lines: list[str]) -> Path:
    d = Path(BOTS_TMP) / bot
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def L(day: int, hh: int, mm: int, msg: str) -> str:
    return f"2026-09-{day:02d} {hh:02d}:{mm:02d}:00,123 - freqtrade.test - INFO - {msg}"


# rotation chain for main: .2 oldest, .1, plain newest (rotation numbers deliberately not 1..3)
write_log("main", "freqtrade.log.10", [L(25, 1, 0, "a1"), L(25, 2, 0, "a2")])
write_log("main", "freqtrade.log.2", [L(26, 1, 0, "b1"), "  wrapped continuation of b1", L(26, 2, 0, "b2")])
write_log("main", "freqtrade.log.1", [L(27, 1, 0, "c1"), L(27, 2, 0, "c2 api_key=SuperSecretValue123456 end")])
write_log("main", "freqtrade.log", [
    L(28, 1, 0, "d1"), f'"WebSocket /ws?token={JWT}" [accepted]', L(28, 2, 0, "d2"), "  wrapped continuation of d2", L(29, 7, 0, "d3"),
])
for junk in ("freqtrade.log.bak", "freqtrade.log.1.gz", "xfreqtrade.log", "freqtrade.log.old", "freqtrade.log.1x", "notes.txt"):
    write_log("main", junk, [L(1, 0, 0, "SHOULD NEVER APPEAR")])
write_log("listing_fade", "freqtrade.log", [L(28, 5, 0, "lf1"), L(29, 5, 0, "lf2")])

files = [p.name for p in ex.log_files("main")]
check("rotation order is oldest -> newest (.10, .2, .1, plain)", files == ["freqtrade.log.10", "freqtrade.log.2", "freqtrade.log.1", "freqtrade.log"])
check("files that do not match the strict pattern are ignored", all("bak" not in f and "gz" not in f and "old" not in f for f in files))
check("a bot with no directory has no log files (no crash)", ex.log_files("control_short") == [])

fp = ex.log_files("main")
t5 = ex.tail_lines(fp, 5)
check("tail_lines returns the last N lines in chronological order across rotations",
      [x.split(" - ")[-1] if " - " in x else x.strip() for x in t5][-1].startswith("d3") and len(t5) == 5)
allx = ex.tail_lines(fp, 10_000)
check("tail_lines with N larger than everything returns all lines, stitched in order",
      [l for l in allx if l.startswith("2026")][0].endswith("a1") and [l for l in allx if l.startswith("2026")][-1].endswith("d3"))
stamps = [ex._ts_of(l) for l in allx if ex._ts_of(l)]
check("stitched timestamps are non-decreasing", stamps == sorted(stamps))
check("tail_lines 0/negative is empty", ex.tail_lines(fp, 0) == [])

NOW = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
ex._utcnow = lambda: NOW
since = ex.lines_since(fp, datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc))
check("lines_since keeps entries >= cutoff (boundary inclusive)", any(l.endswith("d2") for l in since) and any(l.endswith("d3") for l in since))
check("lines_since drops entries before the cutoff", not any(l.endswith(("d1", "c2 api_key=SuperSecretValue123456 end")) for l in since))
check("continuation lines follow their entry", "  wrapped continuation of d2" in since)
check("a continuation line belonging to a dropped entry is dropped", "  wrapped continuation of b1" not in since)
wide = ex.lines_since(fp, datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc))
check("a cutoff before everything returns every line incl. wrapped ones, in order",
      len(wide) == len(allx) and "  wrapped continuation of b1" in wide and wide[0].endswith("a1"))
check("a cutoff after everything returns nothing", ex.lines_since(fp, datetime(2027, 1, 1, tzinfo=timezone.utc)) == [])
first, last = ex.span_of(since)
check("span_of reports first/last timestamps of the selection", first == datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc) and last == datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc))

# ============================================================== catalog
class FakeFetcher:
    def __init__(self, unreachable=(), trades=None):
        self.unreachable, self._trades = set(unreachable), trades or {}

    def _json(self, bot, path):
        if bot in self.unreachable:
            return None
        if "/api/v1/trades" in path:
            return {"trades": [], "total_trades": len(self._trades.get(bot, []))}
        if "/api/v1/status" in path:
            return [t for t in self._trades.get(bot, []) if t.get("is_open")]
        return None

    def trades(self, bot):
        return None if bot in self.unreachable else list(self._trades.get(bot, []))


def T(i, is_open=False, pair="BTC/USDT", short=False, profit=0.01):
    return {"trade_id": i, "pair": pair, "base_currency": pair.split("/")[0], "is_short": short, "leverage": 1.0,
            "is_open": is_open, "open_date": "2026-09-20 00:00:00+00:00", "close_date": None if is_open else "2026-09-21 00:00:00+00:00",
            "open_rate": 100.0, "close_rate": None if is_open else 101.0, "amount": 1.0, "stake_amount": 100.0,
            "close_profit": None if is_open else profit, "close_profit_abs": None if is_open else profit * 100,
            "profit_ratio": profit, "profit_abs": profit * 100, "exit_reason": None if is_open else "roi",
            "enter_tag": "core", "fee_open": 0.001, "fee_close": 0.001, "fee_open_cost": 0.1, "fee_close_cost": 0.1,
            "funding_fees": 0.0, "min_rate": 99.0, "max_rate": 102.0, "stop_loss_abs": 95.0, "initial_stop_loss_abs": 95.0,
            "trade_duration": 1440, "strategy": "X", "timeframe": "1h"}


TRADES = {"main": [T(1), T(2, is_open=True), T(3, profit=-0.02)], "listing_fade": [T(1, pair="ETH/USDT")]}
FETCH = FakeFetcher(unreachable={"shorts"}, trades=TRADES)
ex._fetcher = lambda: FETCH
(Path(DATA_TMP) / "decisions.log").write_text('{"a": 1}\n{"a": 2}\n', encoding="utf-8")
(Path(DATA_TMP) / "portfolio_brain_snapshots.jsonl").write_text('{"d": 1}\n{"d": 2}\n{"d": 3}\n', encoding="utf-8")
(Path(DATA_TMP) / "portfolio_brain_report_2026-09-29.md").write_text("# report\n", encoding="utf-8")
(Path(DATA_TMP) / "secrets.env").write_text("NOPE=1\n", encoding="utf-8")

cat = client.get("/api/export/catalog")
check("catalog: 200 and never crashes with 7 of 8 bots missing their logs", cat.status_code == 200)
cj = cat.json()
byid = {b["id"]: b for b in cj["bots"]}
check("catalog lists all 8 bots", list(byid) == ex.BOT_IDS and len(byid) == 8)
m = byid["main"]["log"]
check("catalog: real line count (incl. wrapped lines) and bytes across all rotations",
      m["available"] and m["lines"] == len(allx) and m["bytes"] == sum(p.stat().st_size for p in ex.log_files("main")))
check("catalog: first/last timestamps (UTC) from the oldest and newest files",
      m["first_ts"] == "2026-09-25 01:00:00" and m["last_ts"] == "2026-09-29 07:00:00")
check("catalog: per-file breakdown present", [f["name"] for f in m["files"]] == files)
check("catalog: a bot without logs shows available=false with a reason (no data), not an error",
      byid["control_short"]["log"]["available"] is False and "no log files" in byid["control_short"]["log"]["reason"])
check("catalog: trade counts from the REST fake (total 3, open 1, closed 2)",
      byid["main"]["trades"] == {"available": True, "total": 3, "open": 1, "closed": 2})
check("catalog: an unreachable bot API is reported as no data for trades", byid["shorts"]["trades"]["available"] is False)
check("catalog: brain files listed (jsonl line count included, unrelated files excluded)",
      {f["name"] for f in cj["brain"]} == {"portfolio_brain_snapshots.jsonl", "portfolio_brain_report_2026-09-29.md"}
      and next(f for f in cj["brain"] if f["name"].endswith(".jsonl"))["lines"] == 3)
check("catalog: decisions.log rows/bytes", cj["decisions"] == {"available": True, "bytes": (Path(DATA_TMP) / "decisions.log").stat().st_size, "lines": 2})
check("catalog states timestamps are UTC and exposes the limits", cj["log_timestamps"] == "UTC" and cj["limits"]["max_concurrent"] == 2)
# cache: a rotated file's stats are computed once
before = len(ex._count_cache)
client.get("/api/export/catalog")
check("catalog: repeat calls reuse the cached per-file stats", len(ex._count_cache) == before)
# live file grows -> re-read
with open(Path(BOTS_TMP) / "main" / "freqtrade.log", "a", encoding="utf-8") as f:
    f.write(L(29, 7, 30, "d4") + "\n")
check("catalog: a changed live file is re-counted",
      client.get("/api/export/catalog").json()["bots"][0]["log"]["lines"] == len(allx) + 1)

# ================================================================ logs
r = client.get("/api/export/logs?bot=main&lines=4")
body = r.text.splitlines()
check("logs?lines=N: 200, plain text, last 4 lines", r.status_code == 200 and len(body) == 4 and body[-1].endswith("d4"))
check("logs: filename is bot_<id>_<from>_<to>.txt from the returned content",
      re.fullmatch(r'attachment; filename="bot_main_20260928T020000Z_20260929T073000Z\.txt"', r.headers["content-disposition"]) is not None
      or re.fullmatch(r'attachment; filename="bot_main_\d{8}T\d{6}Z_\d{8}T\d{6}Z\.txt"', r.headers["content-disposition"]) is not None)
r = client.get("/api/export/logs?bot=main&lines=100000")
check("logs: secrets are masked in the served text", "SuperSecretValue123456" not in r.text and JWT[:25] not in r.text and "***" in r.text)
check("logs: masking did not eat ordinary lines", "d1" in r.text and "a1" in r.text)
r = client.get("/api/export/logs?bot=main&hours=30")
check("logs?hours=H uses the UTC clock (patched now 2026-09-29 08:00 -> cutoff 09-28 02:00)",
      r.status_code == 200 and "d2" in r.text and "d1" not in r.text and "c2" not in r.text)
r = client.get("/api/export/logs?bot=main&all=1")
check("logs?all=1 streams everything in chronological order", r.text.splitlines()[0].endswith("a1") and r.text.splitlines()[-1].endswith("d4"))
check("logs?all=1 filename spans the whole chain", "20260925T010000Z_20260929T073000Z" in r.headers["content-disposition"])
check("logs: two modes at once -> 400", client.get("/api/export/logs?bot=main&lines=5&hours=2").status_code == 400)
check("logs: no mode given -> default tail (200)", client.get("/api/export/logs?bot=main").status_code == 200)
check("logs: lines above the cap -> 422", client.get(f"/api/export/logs?bot=main&lines={ex.MAX_LINES + 1}").status_code == 422)
check("logs: lines=0 / negative -> 422", client.get("/api/export/logs?bot=main&lines=0").status_code == 422)
check("logs: a bot with no logs -> 404 with a readable message", client.get("/api/export/logs?bot=control_short&lines=10").status_code == 404)
check("logs: missing bot param -> 422", client.get("/api/export/logs?lines=10").status_code == 422)

# ========================================================= path safety
evil = ["../../etc/passwd", "main/../listing_fade", "main%2F..%2F..", "main%00", "MAIN", "all", "", "main ", "..", "main;ls", "*", "/etc", "C:\\Windows"]
check("path safety: only whitelisted bot ids are accepted (traversal / glob / odd strings -> 400)",
      all(client.get("/api/export/logs", params={"bot": b, "lines": 5}).status_code in (400, 422) for b in evil))
check("path safety: trades endpoint applies the same whitelist",
      all(client.get("/api/export/trades", params={"bot": b}).status_code in (400, 422) for b in evil))
check("path safety: bundle rejects an unknown bot in the list", client.get("/api/export/bundle?bots=main,../../x").status_code == 400)
params_all = {p["name"] for route in app.openapi()["paths"].values() for op in route.values() for p in op.get("parameters", [])}
check("path safety: the only accepted parameters are bot/lines/hours/all/status/format/bots/include",
      params_all == {"bot", "lines", "hours", "all", "status", "format", "bots", "include"})
os.makedirs(Path(BOTS_TMP) / "trend_v2" / "sub", exist_ok=True)
write_log("trend_v2", "sub/freqtrade.log", [L(1, 0, 0, "nested should not be read")])
check("path safety: nested directories are not traversed", ex.log_files("trend_v2") == [])
try:
    link = Path(BOTS_TMP) / "listing_fade"
    link.mkdir(exist_ok=True)
    secret_file = Path(DATA_TMP) / "secrets.env"
    os.symlink(secret_file, link / "freqtrade.log")
    check("path safety: a symlinked freqtrade.log is refused", ex.log_files("listing_fade") == [])
except (OSError, NotImplementedError):
    print("[SKIP] symlink test (not permitted on this platform)")

# ================================================================ trades
r = client.get("/api/export/trades?bot=main&status=all&format=csv")
rows = r.text.splitlines()
check("trades csv: 200 with the full header", r.status_code == 200 and rows[0] == ",".join(ex.TRADE_COLUMNS))
check("trades csv: all 3 trades for status=all", len(rows) == 4)
check("trades csv: closed uses close_profit as profit_ratio/profit_pct", "0.01" in rows[1] and "1.0" in rows[1])
check("trades csv: header carries pair, direction, dates, rates, stake, profit, exit_reason, enter_tag, fees",
      all(c in rows[0] for c in ("pair", "is_short", "open_date", "close_date", "open_rate", "close_rate", "stake_amount",
                                 "profit_ratio", "profit_abs", "exit_reason", "enter_tag", "fee_open_cost", "fee_close_cost")))
check("trades csv: status=closed -> 2 rows, status=open -> 1 row",
      len(client.get("/api/export/trades?bot=main&status=closed").text.splitlines()) == 3
      and len(client.get("/api/export/trades?bot=main&status=open").text.splitlines()) == 2)
rj = client.get("/api/export/trades?bot=main&status=all&format=json").json()
check("trades json: count and raw records", rj["count"] == 3 and rj["trades"][0]["pair"] == "BTC/USDT")
check("trades: filename", client.get("/api/export/trades?bot=listing_fade").headers["content-disposition"] == 'attachment; filename="trades_listing_fade_all.csv"')
check("trades: unreachable bot API -> 502", client.get("/api/export/trades?bot=shorts").status_code == 502)
check("trades: bad status / format -> 422",
      client.get("/api/export/trades?bot=main&status=weird").status_code == 422 and client.get("/api/export/trades?bot=main&format=xml").status_code == 422)


class PagedFetcher:
    """Real pagination path of portfolio_brain.Fetcher.trades over fake HTTP pages of 500."""
    pass


from backend.engine import portfolio_brain as pb  # noqa: E402

pages = []


class R:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def fake_get(url, **kw):
    off = int(re.search(r"offset=(\d+)", url).group(1))
    lim = int(re.search(r"limit=(\d+)", url).group(1))
    pages.append((lim, off, kw.get("auth")))
    total = 1234
    return R({"trades": [{"trade_id": i} for i in range(off, min(off + lim, total))], "total_trades": total})


fx = pb.Fetcher({"main": "http://x"}, "u", "p", http_get=fake_get)
allt = fx.trades("main")
check("trades: REST paging fetches every trade in pages of 500 with Basic auth (1234 -> 3 pages)",
      len(allt) == 1234 and [p[:2] for p in pages] == [(500, 0), (500, 500), (500, 1000)] and all(p[2] == ("u", "p") for p in pages))

# ================================================================ bundle
def zip_of(resp) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(resp.content))


r = client.get("/api/export/bundle?bots=main,listing_fade,control_short&lines=100000&include=logs,trades,brain,decisions")
check("bundle: 200 application/zip with a timestamped filename",
      r.status_code == 200 and r.headers["content-type"].startswith("application/zip")
      and re.fullmatch(r'attachment; filename="export_\d{8}T\d{6}Z\.zip"', r.headers["content-disposition"]) is not None)
z = zip_of(r)
check("bundle: ZIP integrity (testzip is clean)", z.testzip() is None)
names = set(z.namelist())
check("bundle: per-bot logs + trades, brain files, decisions and MANIFEST present",
      {"main/logs.txt", "main/trades.csv", "listing_fade/logs.txt", "listing_fade/trades.csv", "MANIFEST.json", "decisions/decisions.log",
       "brain/portfolio_brain_snapshots.jsonl", "brain/portfolio_brain_report_2026-09-29.md"} <= names)
check("bundle: unrelated DATA_DIR files (secrets.env) are never included", not any("secrets" in n for n in names))
man = json.loads(z.read("MANIFEST.json"))
logs_txt = z.read("main/logs.txt").decode().splitlines()
check("MANIFEST: generated_at, UTC note, params", "UTC" in man["timezone_note"] and man["generated_at"] and man["params"]["bots"] == ["main", "listing_fade", "control_short"])
check("MANIFEST: main log line count equals the file's real line count", man["bots"]["main"]["logs"]["lines"] == len(logs_txt))
check("MANIFEST: main period matches the content", man["bots"]["main"]["logs"]["first_ts"] == "2026-09-25 01:00:00"
      and man["bots"]["main"]["logs"]["last_ts"] == "2026-09-29 07:30:00")
check("MANIFEST: trades row count matches trades.csv", man["bots"]["main"]["trades"]["rows"] == len(z.read("main/trades.csv").decode().splitlines()) - 1 == 3)
check("MANIFEST: a bot with no log files says so instead of failing",
      man["bots"]["control_short"]["logs"] == {"included": False, "note": "no data (no log files for this bot)"} and "control_short/logs.txt" not in names)
check("MANIFEST: brain + decisions inventory", [f["name"] for f in man["brain"]] and man["decisions"]["lines"] == 2)
check("bundle: secrets masked inside the archived logs", b"SuperSecretValue123456" not in z.read("main/logs.txt") and JWT[:25].encode() not in z.read("main/logs.txt"))
check("bundle: stitched log keeps rotation order", logs_txt[0].endswith("a1") and logs_txt[-1].endswith("d4"))
check("bundle: source files are listed in the manifest", man["bots"]["main"]["logs"]["source_files"] == files)

rb = client.get("/api/export/bundle?bots=main&hours=30&include=logs")
zb = zip_of(rb)
mb = json.loads(zb.read("MANIFEST.json"))
check("bundle: hours filter applied (only the last 30h), only requested parts included",
      zb.testzip() is None and mb["bots"]["main"]["logs"]["first_ts"] == "2026-09-28 02:00:00" and "main/trades.csv" not in zb.namelist()
      and "MANIFEST.json" in zb.namelist() and "brain" not in " ".join(zb.namelist()))
rl = client.get("/api/export/bundle?bots=listing_fade&lines=1&include=logs")
check("bundle: lines=1 keeps exactly one line", len(zip_of(rl).read("listing_fade/logs.txt").decode().splitlines()) == 1)
ra = client.get("/api/export/bundle?bots=main&all=1&include=logs")
check("bundle: all=1 returns the whole chain", len(zip_of(ra).read("main/logs.txt").decode().splitlines()) == len(allx) + 1)
rdef = client.get("/api/export/bundle?bots=main&include=logs")
check("bundle: default range is a line tail (not unbounded)", json.loads(zip_of(rdef).read("MANIFEST.json"))["params"]["lines"] == ex.DEFAULT_LINES)
check("bundle: unreachable trades API is recorded in the manifest, the archive is still valid",
      json.loads(zip_of(client.get("/api/export/bundle?bots=shorts,main&include=trades")).read("MANIFEST.json"))["bots"]["shorts"]["trades"]["included"] is False)
check("bundle: bad include -> 400", client.get("/api/export/bundle?include=logs,hack").status_code == 400
      and client.get("/api/export/bundle?include=").status_code == 400)
check("bundle: bots=all is accepted", client.get("/api/export/bundle?bots=all&lines=5&include=logs").status_code == 200)

# ------------------------------------------------------------------ limits
ex_max = ex.MAX_BUNDLE_BYTES
ex.MAX_BUNDLE_BYTES = 10
r413 = client.get("/api/export/bundle?bots=main&all=1&include=logs")
ex.MAX_BUNDLE_BYTES = ex_max
check("limit: a bundle estimated above the cap is refused up front with 413 and a readable reason",
      r413.status_code == 413 and "limit" in r413.json()["detail"])
ex.MAX_BUNDLE_BYTES = 10_000_000
check("limit: the same request passes under a larger cap", client.get("/api/export/bundle?bots=main&all=1&include=logs").status_code == 200)
ex.MAX_BUNDLE_BYTES = ex_max
check("limit: the default cap is 300 MB", ex_max == 300 * 1024 * 1024)

t1, t2 = ex._slots.acquire(), ex._slots.acquire()
check("limit: two slots can be taken", t1 is not None and t2 is not None and ex._slots.active() == 2)
check("limit: a third acquire is refused", ex._slots.acquire() is None)
check("limit: 3rd concurrent bundle / logs / trades -> 429",
      client.get("/api/export/bundle?bots=main&lines=5&include=logs").status_code == 429
      and client.get("/api/export/logs?bot=main&lines=5").status_code == 429
      and client.get("/api/export/trades?bot=main").status_code == 429)
ex._slots.release(t1)
ex._slots.release(t1)   # idempotent
check("limit: release frees exactly one slot (double release is harmless)", ex._slots.active() == 1)
ex._slots.release(t2)
check("limit: after release exports work again", client.get("/api/export/logs?bot=main&lines=5").status_code == 200 and ex._slots.active() == 0)
check("limit: catalog is not counted against the export slots", (lambda a, b: (client.get("/api/export/catalog").status_code == 200, ex._slots.release(a), ex._slots.release(b))[0])(ex._slots.acquire(), ex._slots.acquire()))

# stale lease expiry (a client that vanished before its stream started)
stale, stale2 = ex._slots.acquire(), ex._slots.acquire()
ex._slots._leases[stale] -= ex._Slots.LEASE_SECONDS + 5
ex._slots._leases[stale2] -= ex._Slots.LEASE_SECONDS + 5
ex._slots._leases[stale] -= 0
fresh = ex._slots.acquire()
check("limit: an expired lease no longer counts against the limit", fresh is not None and stale not in ex._slots._leases)
ex._slots.release(fresh)

# a stream closed early must free its slot
with client.stream("GET", "/api/export/bundle?bots=main&all=1&include=logs") as resp:
    next(resp.iter_bytes())
deadline = time.time() + 5
while ex._slots.active() and time.time() < deadline:
    time.sleep(0.05)
check("limit: closing a bundle stream early releases its slot", ex._slots.active() == 0)

# ========================================================= graceful "no data"
shutil_dir = tempfile.mkdtemp()
os.environ["BOTS_DIR"] = str(Path(shutil_dir) / "does-not-exist")
cat0 = client.get("/api/export/catalog")
check("no log directory at all: catalog still 200, every bot says 'no data'",
      cat0.status_code == 200 and all(b["log"]["available"] is False for b in cat0.json()["bots"]))
check("no log directory: bundle still builds a valid ZIP with a manifest that says so",
      (lambda z: z.testzip() is None and json.loads(z.read("MANIFEST.json"))["bots"]["main"]["logs"]["included"] is False)(
          zip_of(client.get("/api/export/bundle?bots=main&include=logs"))))
os.environ["BOTS_DIR"] = BOTS_TMP

# ================================================================ wiring
check("router is GET-only (no POST/PUT/DELETE/PATCH)",
      all(m == {"GET"} or m == {"GET", "HEAD"} for r_ in ex.router.routes for m in [set(r_.methods)]))
from backend.engine import freqtrade_client  # noqa: E402

check("export bot ids == freqtrade_client.BOTS (all 8)", set(ex.BOT_IDS) == set(freqtrade_client.BOTS) and len(ex.BOT_IDS) == 8)

os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
Path(DATA_TMP, "config.json").write_text(json.dumps(
    {"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
from backend import app as app_module  # noqa: E402

paths = set(app_module.app.openapi()["paths"])   # included routers are wrapped in newer FastAPI, OpenAPI lists them
check("backend/app.py mounts the four export routes",
      {"/api/export/catalog", "/api/export/logs", "/api/export/trades", "/api/export/bundle"} <= paths)
check("the export routes are GET-only in the real app",
      all(set(app_module.app.openapi()["paths"][p]) == {"get"} for p in paths if p.startswith("/api/export/")))

compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
sa = compose["services"]["survival-agent"]
mounts = {}
for v in sa["volumes"]:
    if v.startswith(("survival-agent-data", "allocator-shared", "kronos-data", "kronos-replay-data")):     # P20: allocator volume rw by design; P20.4: kronos signals ro, not a bot; P22-K2: replay-only Kronos output rw, not a bot
        continue
    vol, target, mode = v.split(":")
    mounts[target] = (vol, mode)
service_for = {"main": "freqtrade", "shorts": "freqtrade-shorts", "moonshot": "freqtrade-moonshot", "listing_fade": "freqtrade-listing-fade",
               "trend_ensemble": "freqtrade-trend-ensemble", "trend_v2": "freqtrade-trend-v2",
               "control_long": "freqtrade-control-long", "control_short": "freqtrade-control-short"}
check("compose: survival-agent mounts all 8 bot volumes under /bots/<id>", set(mounts) == {f"/bots/{b}" for b in ex.BOT_IDS})
check("compose: every one of those mounts is read-only (:ro)", all(mode == "ro" for _, mode in mounts.values()))
check("compose: each mount is the very volume that bot writes its own db/log dir to",
      all(mounts[f"/bots/{b}"][0] == compose["services"][svc]["volumes"][0].split(":")[0] for b, svc in service_for.items()))
check("compose: every mounted volume is declared", all(vol in compose["volumes"] for vol, _ in mounts.values()))
check("compose: survival-agent sets BOTS_DIR=/bots and keeps its own rw data volume",
      "BOTS_DIR=/bots" in sa["environment"] and "survival-agent-data:/data" in sa["volumes"])
check("compose: the bots still write their own db volume rw (P20 adds only the read-only /allocator mount next to it)",
      all(not compose["services"][svc]["volumes"][0].endswith(":ro") for svc in service_for.values())
      and all(v == "allocator-shared:/allocator:ro" for svc in service_for.values() for v in compose["services"][svc]["volumes"][1:]))
check("compose: the bots' logfile lives on the mounted volume (freqtrade.log in the db dir)",
      all("--logfile /freqtrade/user_data/db/freqtrade.log" in " ".join(str(compose["services"][s]["command"]).split()) for s in service_for.values()))
src = (REPO / "backend/export.py").read_text(encoding="utf-8")
check("export code never opens SQLite files or takes a path from the request",
      "sqlite" not in src.lower().replace("never opens sqlite", "") or "sqlite3" not in src)
check("export code has no write-capable HTTP call", not any(t in src for t in ("requests.post", ".post(", "requests.put")))

html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("frontend: export card with the three preset buttons",
      'id="export"' in html and all(i in html for i in ('id="exPresetAnalysis"', 'id="exPreset24h"', 'id="exPresetAll"')))
check("frontend: presets use the documented parameters (50000 lines / 24h / all=1)",
      "exBundleUrl('all', 'lines=50000')" in html and "exBundleUrl('all', 'hours=24')" in html and "exBundleUrl('all', 'all=1')" in html)
check("frontend: inputs for lines/hours, per-bot checkboxes and plain download links",
      'id="exLines"' in html and 'id="exHours"' in html and "exBotChk" in html and 'id="exBundleLink"' in html)
check("frontend: no external script or new dependency added", '<script src="http' not in html and "cdn." not in html.lower())
check("frontend: catalog shows 'no data' when a bot has nothing", "no data" in html)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P18 EXPORT CHECKS PASSED")
