"""
P25: announcement guard. Parsing real-shaped Bybit notices, the block windows, the refresh (ledger, dedupe, atomic publish, fail-open), the bots' helper (identical copies, stale file, case)
and the five real strategies' confirm_trade_entry. Offline: fake HTTP, temp dirs, stubbed freqtrade.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import announcements as an  # noqa: E402

ms = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731
PUB = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def item(title, desc="", pub=PUB):
    return {"title": title, "description": desc, "publishTime": ms(pub), "dateTimestamp": ms(pub), "url": "https://x/" + title.replace(" ", "-")}


# ------------------------------------------------------------------ parsing
p = an.parse_item(item("Delisting of STGUSDT Perpetual Contract", "Bybit will be delisting the STGUSDT Perpetual Contract at Oct 10, 2026, 9:00AM UTC."))
check("PARSE: a perpetual notice -> coin STG, effective 2026-10-10 09:00 UTC", p and p["coins"] == ["STG"] and p["kind"] == "perp_delisting" and an._iso(p["effective_ms"]) == "2026-10-10T09:00:00Z")
p = an.parse_item(item("Delisting  of  1000CATSUSDT  Perpetual Contract", "at Oct 12, 2026, 11:05PM UTC"))
check("PARSE: extra spaces, a 1000x contract and PM time", p and p["coins"] == ["1000CATS"] and an._iso(p["effective_ms"]) == "2026-10-12T23:05:00Z")
p = an.parse_item(item("Delisting of DAOUSDT, VGXUSDT, LOOMUSDT, ZCXUSDT, MAPOUSDT Perpetual Contract"))
check("PARSE: several symbols in one notice -> every coin; no time in the text -> effective None (a 14-day window is used)", p and p["coins"] == ["DAO", "LOOM", "MAPO", "VGX", "ZCX"] and p["effective_ms"] is None)
p = an.parse_item(item("Delisting of FETUSDT, OCEANUSDT, and AGIXUSDT Perpetual Contracts"))
check("PARSE: 'and' / plural wording", p and p["coins"] == ["AGIX", "FET", "OCEAN"])
p = an.parse_item(item("Delisting of GODS,SCRT"))
check("PARSE: a token list notice -> both coins", p and p["coins"] == ["GODS", "SCRT"] and p["kind"] == "token_delisting")
p = an.parse_item(item("Delisting of VANRY on Aug 14, 2026"))
check("PARSE: a token notice with a date -> that date", p and p["coins"] == ["VANRY"] and an._iso(p["effective_ms"]) == "2026-08-14T00:00:00Z")
for t in ("Delisting of USDC Perpetual Contracts (2023-04-04)", "Delisting of WIFUSD Inverse Perpetual Contract", "Delisting of Selected Perpetual Contracts (Nov 24, 2025)", "Notice of Delisting and Relisting of SPCXUSDT Pre-IPO Perpetual Contract",
          "Discontinuation of ICX  as Collateral and Lending Asset", "Bybit to Delist 1 Token(s) on Oct 13, 2026", "Bybit Alpha to Delist 10 Token(s) on Sep 19, 2026"):
    check(f"PARSE: not read, not guessed: {t[:55]}", an.parse_item(item(t)) is None)
check("PARSE: no publish time -> ignored", an.parse_item({"title": "Delisting of STGUSDT Perpetual Contract"}) is None)

# ------------------------------------------------------------------ windows
e1 = {"coins": ["STG"], "published_ms": ms(PUB), "effective_ms": ms(datetime(2026, 10, 10, 9, 0, tzinfo=UTC)), "kind": "perp_delisting", "title": "Delisting of STGUSDT Perpetual Contract"}
e2 = {"coins": ["DAO"], "published_ms": ms(PUB), "effective_ms": None, "kind": "perp_delisting", "title": "Delisting of DAOUSDT Perpetual Contract"}
at = lambda d, h=0: datetime(2026, 10, d, h, 0, tzinfo=UTC)  # noqa: E731
check("WINDOW: before the notice nothing is blocked", an.build_blocked([e1, e2], at(8, 9))["coins"] == {})
b = an.build_blocked([e1, e2], at(9))["coins"]
check("WINDOW: from the notice both coins are blocked, the reason names the notice and the effective time", set(b) == {"STG", "DAO"} and "effective 2026-10-10T09:00:00Z" in b["STG"]["reason"] and "no date" in b["DAO"]["reason"])
check("WINDOW: a day after the stated delisting time the coin is free again, a notice without a date lasts 14 days", "STG" not in an.build_blocked([e1, e2], at(11, 10))["coins"] and "DAO" in an.build_blocked([e1, e2], at(20))["coins"] and "DAO" not in an.build_blocked([e1, e2], at(23))["coins"])
e3 = {**e1, "published_ms": ms(PUB) + 3600_000, "effective_ms": ms(datetime(2026, 10, 15, 9, 0, tzinfo=UTC))}
check("WINDOW: two notices for one coin -> the later end wins", an.build_blocked([e1, e3], at(12))["coins"]["STG"]["until"] == "2026-10-16T09:00:00Z")

# ------------------------------------------------------------------ refresh with a fake feed
FEED = [item("Delisting of STGUSDT Perpetual Contract", "at Oct 10, 2026, 9:00AM UTC."), item("Bybit to Delist 1 Token(s) on Oct 13, 2026"), item("Delisting of GODS,SCRT", "", PUB - timedelta(days=60))]


class R:
    def __init__(self, obj):
        self.obj = obj

    def json(self):
        return self.obj

    def raise_for_status(self):
        pass


calls = []


def get_ok(url, params=None, timeout=None):
    calls.append(params["page"])
    return R({"retCode": 0, "result": {"list": FEED if params["page"] == 1 else []}})


def get_bad(url, params=None, timeout=None):
    raise ConnectionError("down")


with tempfile.TemporaryDirectory() as d_, tempfile.TemporaryDirectory() as a_:
    data, alloc = Path(d_), Path(a_)
    now = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)
    r1 = an.refresh(data, alloc, get=get_ok, now=now)
    check("REFRESH: first run reads the notices, writes the ledger and the blocked file (STG blocked, the 60-day-old token notice is not)", r1["ok"] and r1["new"] == 2 and r1["blocked"] == ["STG"] and r1["unreadable_new"] == 1)
    check("REFRESH: the first run reads the whole history (11 pages asked until a short page ends it)", calls[0] == 1)
    r2 = an.refresh(data, alloc, get=get_ok, now=now)
    check("REFRESH: a second run adds nothing (deduplicated by notice)", r2["ok"] and r2["new"] == 0 and r2["ledger"] == 2)
    led = (data / an.LEDGER).read_text(encoding="utf-8").splitlines()
    check("REFRESH: the ledger has one JSON line per readable notice", len(led) == 2 and all(json.loads(x)["coins"] for x in led))
    before = (alloc / an.BLOCKED).read_text(encoding="utf-8")
    r3 = an.refresh(data, alloc, get=get_bad, now=now, pages=1)
    check("FAIL-OPEN: a feed failure returns ok False and leaves the published file as it was (it just ages)", (not r3["ok"]) and (alloc / an.BLOCKED).read_text(encoding="utf-8") == before)
    check("PUBLISH: no temporary file is left behind", not [f for f in os.listdir(alloc) if f.endswith(".tmp")])
    st = an.status(data, alloc, now=now + timedelta(minutes=5))
    check("STATUS: fresh file, one coin, ledger size", st["available"] and st["fresh_for_bots"] and list(st["coins"]) == ["STG"] and st["ledger"] == 2)
    check("STATUS: a file older than 6 hours is not fresh", not an.status(data, alloc, now=now + timedelta(hours=7))["fresh_for_bots"])
    check("STATUS: no file yet", not an.status(data, Path(d_) / "none")["available"])

    # ------------------------------------------------------------------ the bots' helper
    def stub_freqtrade():
        ft, pers, strat = types.ModuleType("freqtrade"), types.ModuleType("freqtrade.persistence"), types.ModuleType("freqtrade.strategy")

        class _Trade:
            @classmethod
            def get_trades_proxy(cls, is_open=True):
                return []

            @classmethod
            def total_open_trades_stakes(cls):
                return 0.0

        class _I:
            def __init__(self, config=None):
                self.config = config or {}
        pers.Trade, strat.IStrategy = _Trade, _I
        strat.merge_informative_pair = lambda *a, **k: None
        sys.modules.update({"freqtrade": ft, "freqtrade.persistence": pers, "freqtrade.strategy": strat})

    def load(rel, name):
        path = REPO / rel
        sys.path.insert(0, str(path.parent))
        for m in ("_allocator_ctx", "_second_opinion"):
            sys.modules.pop(m, None)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.path.remove(str(path.parent))
        return mod

    stub_freqtrade()
    os.environ["ALLOCATOR_DIR"] = str(alloc)
    DIRS = ["freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-v2", "freqtrade-shorts-v2"]
    hashes = {hashlib.sha256((REPO / d / "user_data" / "strategies" / "_allocator_ctx.py").read_bytes()).hexdigest() for d in DIRS}
    check("CTX: the helper copy is byte-identical in all six bot directories", len(hashes) == 1)
    ctx = load("freqtrade/user_data/strategies/_allocator_ctx.py", "ctx_p25")
    # the published file was written for 2026-10-09 08:00; move the clock by rewriting `updated` to now so the helper sees it fresh
    now_real = datetime.now(UTC)
    pub_real = an.build_blocked([{**e1, "published_ms": ms(now_real - timedelta(hours=2)), "effective_ms": ms(now_real + timedelta(days=1))}], now_real)
    (alloc / an.BLOCKED).write_text(json.dumps(pub_real), encoding="utf-8")
    ctx._cache.clear()
    check("CTX: a blocked coin gives a reason (any quote / futures suffix, any case)", ctx.entry_block("STG/USDT:USDT") and "STG" in ctx.entry_block("stg/USDT") and ctx.entry_block("STG/USDT"))
    check("CTX: another coin is free", ctx.entry_block("BTC/USDT:USDT") is None)
    old = {**pub_real, "updated": an._iso(ms(now_real - timedelta(hours=7)))}
    (alloc / an.BLOCKED).write_text(json.dumps(old), encoding="utf-8")
    ctx._cache.clear()
    check("CTX: a file older than 6 hours blocks nothing (fail-open)", ctx.entry_block("STG/USDT:USDT") is None)
    (alloc / an.BLOCKED).write_text("{broken", encoding="utf-8")
    ctx._cache.clear()
    check("CTX: a corrupt file blocks nothing, no exception", ctx.entry_block("STG/USDT:USDT") is None)
    (alloc / an.BLOCKED).unlink()
    ctx._cache.clear()
    check("CTX: no file blocks nothing", ctx.entry_block("STG/USDT:USDT") is None)
    (alloc / an.BLOCKED).write_text(json.dumps(pub_real), encoding="utf-8")

    # ------------------------------------------------------------------ the five real strategies refuse a NEW entry on a blocked coin
    SPECS = {"main": ("freqtrade/user_data/strategies/DeepSeekStrategy.py", "DeepSeekStrategy"), "shorts": ("freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py", "DeepSeekShortsStrategy"),
             "moonshot": ("freqtrade-moonshot/user_data/strategies/DeepSeekMoonshotStrategy.py", "DeepSeekMoonshotStrategy"), "listing_fade": ("freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py", "ListingFadeStrategy"),
             "trend_v2": ("freqtrade-trend-v2/user_data/strategies/TrendV2Strategy.py", "TrendV2Strategy")}
    logs = []

    class H(logging.Handler):
        def emit(self, rec):
            logs.append(rec.getMessage())
    h = H()
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    for bot, (rel, cls) in SPECS.items():
        mod = load(rel, f"p25_{bot}")
        mod._allocator_ctx._cache.clear()
        st_ = getattr(mod, cls)({"dry_run": True})
        logs.clear()
        for side in ("long", "short"):
            ok = st_.confirm_trade_entry("STG/USDT:USDT", "limit", 1.0, 1.0, "GTC", datetime.now(UTC), None, side)
            check(f"STRATEGY {bot}: a NEW {side} entry on a blocked coin is refused", ok is False)
        check(f"STRATEGY {bot}: the refusal is logged with the notice", any("announcement guard" in m and "STG" in m for m in logs))
        import ast
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "confirm_trade_entry")
        body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) and isinstance(n.value.value, str))]
        first = body[0]
        check(f"STRATEGY {bot}: the guard is the first statement of the hook (before any network call or other rule)", isinstance(first, ast.Assign) and "entry_block" in ast.dump(first))
    logging.getLogger().removeHandler(h)
    tv2 = getattr(load(SPECS["trend_v2"][0], "p25_tv2b"), "TrendV2Strategy")({"dry_run": True})
    check("STRATEGY trend_v2: an unblocked coin still enters (the guard changes nothing else)", tv2.confirm_trade_entry("BTC/USDT:USDT", "limit", 1.0, 1.0, "GTC", datetime.now(UTC), None, "long") is True)

# controls must stay pure
for ctl in ("freqtrade-control-long", "freqtrade-control-short", "freqtrade-trend-ensemble"):
    f = REPO / ctl / "user_data" / "strategies"
    check(f"CONTROL {ctl}: no announcement guard (a baseline stays a baseline)", not any("entry_block" in x.read_text(encoding="utf-8") for x in f.glob("*.py")))

print("\nALL P25 ANNOUNCEMENT CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
