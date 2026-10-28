"""
P26: grow-policy circuit breaker, control bot by side, Listing Fade re-entry guard (strategy + shared parameter file + dashboard), Listing Fade exit-mechanics study.
Offline: hand-built trades, stubbed freqtrade, synthetic 5-minute candles, temp directories.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(tmp)
os.environ["REPLAY_DIR"] = str(tmp / "replay")
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)

from backend.engine import allocator as al, bot_params as bp, portfolio_brain as pb, runtime_settings as rs  # noqa: E402
from backend.replay import fade_exit_study as fx, listing_fade as lf  # noqa: E402

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def trade(days_ago, pnl, stake=25.0, short=False):
    cd = NOW - timedelta(days=days_ago)
    return {"is_open": False, "close_date": cd.isoformat(), "open_date": (cd - timedelta(hours=3)).isoformat(), "close_profit_abs": pnl, "close_profit": pnl / stake, "stake_amount": stake, "pair": "BTC/USDT:USDT", "is_short": short}


def seq(pnls, start=0.2, step=0.3, stake=25.0):
    return [trade(start + i * step, p, stake) for i, p in enumerate(reversed(pnls))]       # the list is in time order: the last pnl is the most recent


CFG = {"on": True, "stakes": 1.25, "streak": 8}
ACTIVE = {"status": "active"}
# ======================================================================= the breaker
rec = dict(ACTIVE)
cls, w, why = al.grow_bot("trend_v2", rec, seq([-4.0] * 8), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER (money): 8 trades lost 32 USD on 25 USD stakes (> 1.25 stakes) -> class breaker, weight 0, status stopped, the stop time is remembered",
      cls == "breaker" and w == 0.0 and rec["status"] == "stopped" and rec["breaker_since"] and "1.25 x the average stake" in why)
cls, w, _ = al.grow_bot("trend_v2", dict(ACTIVE), seq([-4.0] * 5), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER: 5 trades with a 20 USD loss are below 1.25 x 25 = 31.25 -> keeps measuring at full size", cls == "neutral" and w == 1.0)
cls, w, _ = al.grow_bot("trend_v2", dict(ACTIVE), seq([-40.0] * 4), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER: never judged on fewer than 5 closed trades, however big the loss", cls == "neutral" and w == 1.0)
rec = dict(ACTIVE)
cls, w, why = al.grow_bot("x", rec, seq([-1.0] * 8, stake=500.0), "no_data", 0.0, NOW, "control_long", CFG) if False else al.grow_bot("main", rec, seq([-1.0] * 8, stake=500.0), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER (streak): 8 losing trades in a row trip it even when the money loss is small against the stake", cls == "breaker" and "all losses" in why)
cls, w, _ = al.grow_bot("main", dict(ACTIVE), seq([-1.0] * 7 + [0.5] + [-1.0] * 0, stake=500.0), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER (streak): one win among the last 8 breaks the streak", cls == "neutral")
check("BREAKER: switched off (or no config) it never trips", al.grow_bot("main", dict(ACTIVE), seq([-4.0] * 8), "no_data", 0.0, NOW, "control_long", {**CFG, "on": False})[0] == "neutral"
      and al.grow_bot("main", dict(ACTIVE), seq([-4.0] * 8), "no_data", 0.0, NOW, "control_long")[0] == "neutral")
check("BREAKER: a bot that earns is never stopped by it", al.grow_bot("main", dict(ACTIVE), seq([1.0] * 12), "no_data", 0.0, NOW, "control_long", CFG)[0] == "neutral")
# after the stop the old losses no longer count
stopped_at = (NOW - timedelta(days=10)).isoformat().replace("+00:00", "Z")
old = seq([-4.0] * 8, start=14.0)
rec = {"status": "trial", "trial_start": (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z"), "breaker_since": stopped_at}
new = seq([1.0, 1.0, 1.0, 1.0, 1.0, 1.0], start=0.2, step=0.5)
cls, w, _ = al.grow_bot("trend_v2", rec, old + new, "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER: a trial that earns restarts the bot clean (status active), the losses before the stop do not trip it again", rec["status"] == "active" and w == 1.0 and cls == "neutral")
cls, w, _ = al.grow_bot("trend_v2", {"status": "active", "breaker_since": stopped_at}, old + new, "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER: an active bot with an old breaker time only counts trades after it", cls == "neutral" and w == 1.0)
# early failure of a trial
rec = {"status": "trial", "trial_start": (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")}
cls, w, why = al.grow_bot("trend_v2", rec, seq([-8.0] * 5, start=0.2, step=0.3), "no_data", 0.0, NOW, "control_long", CFG)
check("BREAKER (trial): a trial that loses more than 1.25 stakes is stopped before its 7 days are over", cls == "breaker" and w == 0.0 and rec["status"] == "stopped" and why.startswith("trial "))
# the old rule keeps its class
cls, w, _ = al.grow_bot("main", dict(ACTIVE), [trade(0.1 + i * 0.2, -1.0) for i in range(30)], "no_edge", 0.0, NOW, "control_long", CFG)
check("ORDER: a loser with 30+ trades is still reported with the old class `stopped`, the breaker covers everything faster", cls == "stopped")

# ======================================================================= control bot by side
check("CONTROL: spot bots always use control_long", al.control_name("main", []) == "control_long" and al.control_name("moonshot", [trade(1, 1, short=True)] * 9) == "control_long")
check("CONTROL: a futures bot that trades long (trend_v2) follows control_long", al.control_name("trend_v2", [trade(i, 1.0, short=False) for i in range(1, 10)]) == "control_long")
check("CONTROL: a futures bot that shorts (listing_fade) keeps control_short", al.control_name("listing_fade", [trade(i, 1.0, short=True) for i in range(1, 10)]) == "control_short")
check("CONTROL: too little history or a mixed bot keeps the mode's default (control_short for futures)", al.control_name("trend_v2", [trade(1, 1.0)]) == "control_short"
      and al.control_name("shorts", [trade(i, 1.0, short=(i % 2 == 0)) for i in range(1, 11)]) == "control_short")

# ======================================================================= settings and the whole policy
S0 = al.load_settings({})
check("SETTINGS: breaker on by default (1.25 stakes, 8 in a row); env and dashboard override", S0["breaker"] == {"on": True, "stakes": 1.25, "streak": 8}
      and al.load_settings({"ALLOCATOR_BREAKER": "off"})["breaker"]["on"] is False and al.load_settings({"ALLOCATOR_BREAKER_STAKES": "2", "ALLOCATOR_BREAKER_STREAK": "0"})["breaker"] == {"on": True, "stakes": 2.0, "streak": 0})
for k in ("ALLOCATOR_BREAKER", "ALLOCATOR_BREAKER_STAKES", "ALLOCATOR_BREAKER_STREAK", "FADE_REENTRY_COOLDOWN_H"):
    check(f"SETTINGS: {k} is a dashboard switch with a validator and a default", k in rs.SPEC and rs.SPEC[k]["default"] in rs.SPEC[k]["options"])
check("SETTINGS: junk values are refused by the dashboard validator", rs.SPEC["ALLOCATOR_BREAKER_STAKES"]["validate"]("0.1") is None and rs.SPEC["FADE_REENTRY_COOLDOWN_H"]["validate"]("7") is None and rs.SPEC["ALLOCATOR_BREAKER"]["validate"]("maybe") is None)


def mk_state(trades):
    bots = {b: {"reachable": True, "evidence_status": "unproven", "d7": {}, "d30": {}} for b in al.MANAGED_BOTS}
    return {"bots": bots, "trades_by_bot": {b: trades.get(b, []) for b in al.MANAGED_BOTS}, "drawdown": {"drawdown_from_hwm_pct": 0.0, "highest_rung": None}, "partial": False, "open_trades": {},
            "equity": 1700.0, "controls": {"control_long": {"d7": {"net_pnl_usd": 5.0}}, "control_short": {"d7": {"net_pnl_usd": -1.0}}}}


prev = {b: 1.0 for b in al.MANAGED_BOTS}
trend = [{**trade(1 + i * 2, -4.0), "is_short": False} for i in range(10)]
sf: dict = {}
g = al.grow_policy(mk_state({"trend_v2": trend}), prev, sf, NOW, S0, None)
check("POLICY: ten straight losses on trend_v2 (what the live bot did) -> weight 0 at once, rule grow_breaker fired, the reason says why", g["final"]["trend_v2"] == 0.0 and "grow_breaker" in g["rules_fired"]
      and "circuit breaker" in g["reasons"]["trend_v2"] and g["detail"]["trend_v2"]["class"] == "breaker")
check("POLICY: the other bots are untouched", all(g["final"][b] == 1.0 for b in al.MANAGED_BOTS if b != "trend_v2"))
check("POLICY: trend_v2 trades long, so its restart is judged by control_long, not control_short", g["detail"]["trend_v2"]["control"] == "control_long")
g2 = al.grow_policy(mk_state({"trend_v2": trend}), g["final"], sf, NOW + timedelta(days=3, hours=1), S0, None)
check("POLICY: after 3 days control_long (earning) restarts it as a 0.5x trial", g2["final"]["trend_v2"] == 0.5 and g2["detail"]["trend_v2"]["class"] == "trial")
sf_off: dict = {}
g3 = al.grow_policy(mk_state({"trend_v2": trend}), prev, sf_off, NOW, al.load_settings({"ALLOCATOR_BREAKER": "off"}), None)
check("POLICY: with the breaker off the bot is left 'measuring' as before", g3["final"]["trend_v2"] == 1.0)

# ======================================================================= Listing Fade strategy: re-entry guard
SP = REPO / "freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py"
from backend.replay import sandbox  # noqa: E402
with sandbox._LOAD_LOCK:
    sandbox._install_stubs()
    sys.path.insert(0, str(SP.parent))
    try:
        spec = importlib.util.spec_from_file_location("fade_guard_under_test", SP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(SP.parent))
T = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
check("GUARD: a stop-loss 2 hours ago blocks a 24 h cooldown", mod.reentry_blocked([("stop_loss", T - timedelta(hours=2))], T, 24) is True)
check("GUARD: a stop-loss 25 hours ago does not", mod.reentry_blocked([("stop_loss", T - timedelta(hours=25))], T, 24) is False)
check("GUARD: a trailing-stop exit (a win) never blocks", mod.reentry_blocked([("trailing_stop_loss", T - timedelta(minutes=5))], T, 24) is False)
check("GUARD: cooldown 0 / no trades / unknown close time never block", mod.reentry_blocked([("stop_loss", T)], T, 0) is False and mod.reentry_blocked([], T, 24) is False and mod.reentry_blocked([("stop_loss", None)], T, 24) is False)
check("GUARD: the exact boundary (24 h) is open, one minute earlier is blocked", mod.reentry_blocked([("stop_loss", T - timedelta(hours=24))], T, 24) is False and mod.reentry_blocked([("stop_loss", T - timedelta(hours=23, minutes=59))], T, 24) is True)

cfg = json.loads((REPO / "freqtrade-listing-fade/user_data/config.json").read_text(encoding="utf-8"))
# the replay sandbox stubs _allocator_ctx; the guard test needs the real helper (the one the bot imports)
_spec = importlib.util.spec_from_file_location("real_allocator_ctx", SP.parent / "_allocator_ctx.py")
real_ctx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_ctx)
mod._allocator_ctx = real_ctx
obj = mod.ListingFadeStrategy(cfg)


class FakeTrade:
    rows: list = []

    @staticmethod
    def get_trades_proxy(pair=None, is_open=None, **kw):
        return [types.SimpleNamespace(exit_reason=r, close_date_utc=c) for r, c in FakeTrade.rows]


persist = types.ModuleType("freqtrade.persistence")
persist.Trade = FakeTrade
sys.modules["freqtrade.persistence"] = persist


class OkResp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"result": {"list": [{"fundingRate": "0.0001"}]}}


mod.requests = types.SimpleNamespace(get=lambda *a, **k: OkResp())
os.environ.pop("FADE_REENTRY_COOLDOWN_H", None)
mod._allocator_ctx.ALLOCATOR_DIR = str(tmp / "no_such_dir")
mod._allocator_ctx._cache.clear()
FakeTrade.rows = [("stop_loss", T - timedelta(hours=3))]
check("ENTRY: confirm_trade_entry refuses a pair stopped out 3 hours ago (default cooldown 24 h from the environment default)", obj.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is False)
FakeTrade.rows = [("trailing_stop_loss", T - timedelta(hours=3))]
check("ENTRY: a recent WIN does not block the pair", obj.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is True)
FakeTrade.rows = [("stop_loss", T - timedelta(hours=3))]
os.environ["FADE_REENTRY_COOLDOWN_H"] = "0"
check("ENTRY: FADE_REENTRY_COOLDOWN_H=0 switches the guard off", obj.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is True)
os.environ["FADE_REENTRY_COOLDOWN_H"] = "banana"
check("ENTRY: a junk environment value falls back to 24 h", obj.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is False)
os.environ.pop("FADE_REENTRY_COOLDOWN_H")


class Boom:
    @staticmethod
    def get_trades_proxy(**kw):
        raise RuntimeError("db gone")


persist.Trade = Boom
check("ENTRY: if the guard cannot read the trade history it fails OPEN (trading is never stopped by the guard itself)", obj.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is True)

# ======================================================================= shared parameter file: backend -> bot
shared = tmp / "allocator"
shared.mkdir()
res = bp.publish(shared, NOW, {"FADE_REENTRY_COOLDOWN_H": "12"})
check("PARAMS: the backend publishes the wanted cooldown to the shared volume", res["ok"] and json.loads((shared / "bot_params.json").read_text(encoding="utf-8"))["listing_fade"]["reentry_cooldown_h"] == 12.0)
check("PARAMS: junk or out-of-range values fall back to the 24 h default", bp.values({"FADE_REENTRY_COOLDOWN_H": "x"})["listing_fade"]["reentry_cooldown_h"] == 24.0 and bp.values({"FADE_REENTRY_COOLDOWN_H": "500"})["listing_fade"]["reentry_cooldown_h"] == 24.0)
check("PARAMS: no shared volume is reported, never raised", bp.publish(None, NOW)["ok"] is False and bp.publish(tmp / "nope", NOW)["ok"] is False)
st = bp.status(shared, NOW + timedelta(minutes=5), {"FADE_REENTRY_COOLDOWN_H": "48"})
check("PARAMS: the dashboard status shows wanted vs published and the file age", st["wanted"]["listing_fade"]["reentry_cooldown_h"] == 48.0 and st["file"]["listing_fade"]["reentry_cooldown_h"] == 12.0 and st["age_s"] == 300 and st["fresh"] is True)
check("PARAMS: a file older than 6 hours is reported stale", bp.status(shared, NOW + timedelta(hours=7))["fresh"] is False)
# the bot side reads it
ctx = mod._allocator_ctx
ctx.ALLOCATOR_DIR = str(shared)
(shared / "bot_params.json").write_text(json.dumps({"schema": "p26-bot-params", "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "listing_fade": {"reentry_cooldown_h": 6.0}}), encoding="utf-8")
ctx._cache.clear()
check("CTX: the bot reads the published value", ctx.bot_param("listing_fade", "reentry_cooldown_h", 24.0) == 6.0)
(shared / "bot_params.json").write_text(json.dumps({"schema": "p26-bot-params", "updated": (datetime.now(UTC) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"), "listing_fade": {"reentry_cooldown_h": 6.0}}), encoding="utf-8")
ctx._cache.clear()
check("CTX: a stale file, a bad value, a missing bot or no file all give the caller's default", ctx.bot_param("listing_fade", "reentry_cooldown_h", 24.0) == 24.0)
(shared / "bot_params.json").write_text(json.dumps({"updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "listing_fade": {"reentry_cooldown_h": "twelve", "other": True}}), encoding="utf-8")
ctx._cache.clear()
check("CTX: non-numeric / boolean values are ignored", ctx.bot_param("listing_fade", "reentry_cooldown_h", 24.0) == 24.0 and ctx.bot_param("listing_fade", "other", 3.0) == 3.0 and ctx.bot_param("main", "x", 5.0) == 5.0)
(shared / "bot_params.json").write_text("{not json", encoding="utf-8")
ctx._cache.clear()
check("CTX: a corrupt file gives the default", ctx.bot_param("listing_fade", "reentry_cooldown_h", 24.0) == 24.0)
(shared / "bot_params.json").write_text(json.dumps({"updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "listing_fade": {"reentry_cooldown_h": 6.0}}), encoding="utf-8")
ctx._cache.clear()
obj_cfg = mod.ListingFadeStrategy(cfg)
FakeTrade2 = types.SimpleNamespace(get_trades_proxy=lambda **k: [types.SimpleNamespace(exit_reason="stop_loss", close_date_utc=T - timedelta(hours=8))])
persist.Trade = FakeTrade2
check("ENTRY: the dashboard value wins over the environment: 8 h after a stop, cooldown 6 h (file) allows the entry", obj_cfg.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is True)
(shared / "bot_params.json").write_text(json.dumps({"updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "listing_fade": {"reentry_cooldown_h": 12.0}}), encoding="utf-8")
ctx._cache.clear()
check("ENTRY: ... and with 12 h in the file the same pair is blocked", obj_cfg.confirm_trade_entry("DNA/USDT:USDT", "limit", 1, 1, "GTC", T, "listing_fade", "short") is False)

# the six copies of the helper stay identical
dirs = ["freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-v2", "freqtrade-shorts-v2"]
texts = {(REPO / d / "user_data/strategies/_allocator_ctx.py").read_text(encoding="utf-8") for d in dirs}
check("CTX: the six copies of _allocator_ctx.py are identical and all carry bot_param", len(texts) == 1 and "def bot_param" in next(iter(texts)))

# ======================================================================= exit study
rules = lf.load_rules(REPO)


def m5_frame(prices, start):
    p = np.asarray(prices, float)
    o = np.concatenate([[p[0]], p[:-1]])
    return pd.DataFrame({"date": pd.date_range(start, periods=len(p), freq="5min", tz="UTC"), "open": o, "high": np.maximum(o, p) * 1.0004, "low": np.minimum(o, p) * 0.9996, "close": p, "volume": 1.0, "quote": 1.0})


def ev(sym, entry_times):
    return {"inst": {"symbol": sym, "launch_ms": 0, "status": "Trading", "tick": 0.0001}, "df": pd.DataFrame(), "funding": [], "flagged": False, "first_hour_quote": 50000.0,
            "signals": [{"i": k, "entry_time": t, "age_days": 3.0, "funding": None} for k, t in enumerate(entry_times)]}


def squeeze_path():
    """flat 1 h, a +9% squeeze over 1 h (inside the live -12% stop, beyond a -8% stop), then a fall to 0.85 over 5 h, a bounce to 0.90 (the trailing stop fires), then flat for 16 days."""
    return np.concatenate([np.full(288, 1.0), np.full(12, 1.0), np.linspace(1.0, 1.09, 12), np.linspace(1.09, 0.85, 60), np.linspace(0.85, 0.90, 12), np.full(288 * 16, 0.90)])


def tests_events(t0):
    start = t0 - timedelta(days=1)
    return ev("AAAUSDT", [t0, t0 + timedelta(hours=4)]), {"AAAUSDT": m5_frame(squeeze_path(), start)}


T0 = datetime(2025, 9, 1, 10, 0, tzinfo=UTC)
e1, cache1 = tests_events(T0)
fee = 0.055
out = {}
for name, stop, cd in fx.VARIANTS:
    out[name] = lf.run_scenario({**rules, "stoploss": stop}, [e1], 0.0, 3, True, fee, NOW, cache1, {}, reentry=True, cooldown_h=cd)
check("VARIANTS: the grid is 4 stops x 2 cooldowns with the live rules first and the candidate named stop8_cd24", [v[0] for v in fx.VARIANTS][:3] == ["live", "cd24", "stop10"] and len(fx.VARIANTS) == 8 and fx.CANDIDATE in [v[0] for v in fx.VARIANTS])
live_tr = out["live"]
check("LIVE rules ride out the +9% squeeze (stop -12%) and exit on the trailing stop in profit; the second signal finds the pair busy -> one trade", len(live_tr) == 1 and live_tr[0]["exit_reason"] == "trailing_stop_loss" and live_tr[0]["profit_ratio"] > 0.05)
s8 = out["stop8"]
check("STOP -8%: the squeeze stops the first trade out, the second signal (4 h later) is traded again -> two trades", len(s8) == 2 and s8[0]["exit_reason"] == "stop_loss" and s8[0]["profit_ratio"] < -0.07)
s8c = out["stop8_cd24"]
check("COOLDOWN: with 24 h after the stop-loss the second signal is blocked -> one trade, the stopped one", len(s8c) == 1 and s8c[0]["exit_reason"] == "stop_loss")
check("COOLDOWN off by default: run_scenario without cooldown_h behaves exactly as before", len(lf.run_scenario(rules, [e1], 0.0, 3, True, fee, NOW, cache1, {}, reentry=True)) == len(live_tr))
check("COOLDOWN: only a stop_loss starts it (the live variant with cd24 and no stop in the path trades as usual)", len(out["cd24"]) == 1 and out["cd24"][0]["exit_reason"] == "trailing_stop_loss")

# numbers
tr = [{"open_at_end": False, "profit_abs": 2.0, "profit_ratio": 0.08, "exit_reason": "trailing_stop_loss", "open_date": T0, "close_date": T0 + timedelta(hours=5), "gap_pct": 0.0},
      {"open_at_end": False, "profit_abs": -3.0, "profit_ratio": -0.12, "exit_reason": "stop_loss", "open_date": T0 + timedelta(days=1), "close_date": T0 + timedelta(days=1, hours=3), "gap_pct": 1.0},
      {"open_at_end": True, "profit_abs": 9.0, "profit_ratio": 0.3, "exit_reason": "open_at_data_end", "open_date": T0, "close_date": T0 + timedelta(days=3), "gap_pct": 0.0}]
ps = fx.period_stats(tr, 100.0)
check("STATS: open-at-end trades are excluded; net, win rate, stops and drawdown are right", ps["n"] == 2 and ps["net_usd"] == -1.0 and ps["win_pct"] == 50.0 and ps["stops"] == 1 and ps["stop_mean_pct"] == -12.0 and abs(ps["max_dd_pct"] - 3 / 102 * 100) < 0.01)
d = fx.daily_pnl(tr, T0.replace(hour=0), T0.replace(hour=0) + timedelta(days=3))
check("STATS: daily P&L is bucketed by the day of exit, zeros included", list(d) == [2.0, -3.0, 0.0])
pdiff = fx.paired_diff(tr[:1], tr[1:2], T0.replace(hour=0), T0.replace(hour=0) + timedelta(days=3))
check("STATS: paired difference (candidate - live) sums per day", pdiff["total_usd"] == 5.0 and pdiff["days"] == 3)

# verdict
live_o = {"n": 150, "net_usd": 10.0, "max_dd_pct": 12.0}
good_diff = {"mean_usd_per_day": 0.05, "ci95_usd_per_day": [0.01, 0.09]}
check("VERDICT: all five conditions met -> better candidate", fx.verdict(live_o, good_diff, {"net_usd": 5.0}, {"net_usd": 6.0}, {"max_dd_pct": 10.0, "net_usd": 25.0}, {"net_usd": 4.0}, {"net_usd": 9.0})["verdict"] == "better candidate")
v = fx.verdict(live_o, {"mean_usd_per_day": 0.05, "ci95_usd_per_day": [-0.02, 0.1]}, {"net_usd": 5.0}, {"net_usd": 6.0}, {"max_dd_pct": 10.0, "net_usd": 25.0}, {"net_usd": 4.0}, {"net_usd": 9.0})
check("VERDICT: a CI that includes 0 -> unproven", v["verdict"] == "unproven" and any("does not exclude 0" in x for x in v["failed"]))
check("VERDICT: worse in the looking period, deeper drawdown or no better at 0.50% slippage each block it", len(fx.verdict(live_o, good_diff, {"net_usd": 5.0}, {"net_usd": 4.0}, {"max_dd_pct": 20.0, "net_usd": 25.0}, {"net_usd": 4.0}, {"net_usd": 3.0})["failed"]) == 3)
check("VERDICT: too few out-of-sample trades with the live rules blocks it", any("only 40" in x for x in fx.verdict({**live_o, "n": 40}, good_diff, {"net_usd": 5.0}, {"net_usd": 6.0}, {"max_dd_pct": 10.0, "net_usd": 25.0}, {"net_usd": 4.0}, {"net_usd": 9.0})["failed"]))
check("VERDICT: a negative difference -> not better", fx.verdict(live_o, {"mean_usd_per_day": -0.1, "ci95_usd_per_day": [-0.2, 0.0]}, {"net_usd": 5.0}, {"net_usd": 6.0}, {"max_dd_pct": 10.0, "net_usd": 25.0}, {"net_usd": 4.0}, {"net_usd": 9.0})["verdict"] == "not better")

# the whole study on two synthetic listings, one in each period
T_LOCK = datetime(2025, 5, 1, 10, 0, tzinfo=UTC)
ea, ca = tests_events(T_LOCK)
ea["inst"]["symbol"] = "LOCKUSDT"
ca = {"LOCKUSDT": ca["AAAUSDT"]}
eb, cb = tests_events(T0)
res = fx.evaluate(rules, [ea, eb], {**ca, **cb}, NOW, fee, datetime(2025, 7, 1, tzinfo=UTC))
check("STUDY: eight variants, two slippage lines, three periods each, a paired difference to the live rules, a verdict computed by code",
      len(res["variants"]) == 8 and set(res["variants"]["live"]) >= {"slip0.3", "slip0.5", "oos_diff_vs_live", "locked_diff_vs_live"} and res["verdict"] in ("better candidate", "unproven", "not better") and res["candidate"] == "stop8_cd24")
check("STUDY: on two listings the candidate cannot reach 100 out-of-sample trades, so it cannot be a `better candidate` (the sample-size condition is enforced)", res["verdict"] != "better candidate" and any("out-of-sample trades" in x for x in res["verdict_failed_conditions"]))
check("STUDY: the live variant's own difference to itself is zero", res["variants"]["live"]["oos_diff_vs_live"]["total_usd"] == 0.0)
lv, c8 = res["variants"]["live"]["slip0.3"], res["variants"]["stop8_cd24"]["slip0.3"]
check("STUDY: the squeeze listing makes the -8% stop lose in both periods and the live rules win, as built", lv["oos"]["net_usd"] > c8["oos"]["net_usd"] and lv["locked"]["net_usd"] > c8["locked"]["net_usd"])

# report files
rep = {"run_id": "fx-test", "type": "fade_exit_study", "banner": "b", "rules": lf.rules_summary(rules), "universe": {}, "basis": {}, "min_oos_trades": 100, **{k: v for k, v in res.items() if k != "_trades"},
       "limits": fx.LIMITS, "_primary_trades": {k: res["_trades"][k][fx.SLIP] for k in (fx.LIVE, fx.CANDIDATE)}}
fx.write_outputs("fx-test", {}, rep)
d_ = Path(os.environ["REPLAY_DIR"]) / "fx-test"
md = (d_ / "report.md").read_text(encoding="utf-8")
check("OUTPUT: report.json, report.md and trades.csv are written, the markdown carries verdict, variants, paired differences, stress and limits", all((d_ / f).is_file() for f in ("report.json", "report.md", "trades.csv"))
      and all(x in md for x in ("Verdict for `stop8_cd24`", "Variants at 0.3% slippage", "Out-of-sample difference", "Stress", "Limits")) and "_primary_trades" not in json.loads((d_ / "report.json").read_text(encoding="utf-8")))

# ======================================================================= wiring: runner, route, interface, docs
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
check("WIRING: runner method, route, download label and the Replay card exist", "start_fade_exit_study" in src("backend/replay/runner.py") and "/api/replay/fade-exit/study" in src("backend/app.py")
      and "fade_exit_study" in src("backend/downloads.py") and "xRun" in src("frontend/replay.html") and "renderFadeExit" in src("frontend/replay.html"))
check("WIRING: the bot-params publisher runs in the 10-minute loop, a dashboard change publishes at once, and /api/bot-params exists", "bot_params.publish" in src("backend/app.py") and "/api/bot-params" in src("backend/app.py")
      and src("backend/app.py").count("bot_params.publish") >= 2)
html = src("frontend/index.html")
check("INTERFACE: the allocator card has the breaker switches, the Fade cooldown switch with what the bot sees, and shows the breaker class in red",
      all(x in html for x in ("sel('ALLOCATOR_BREAKER')", "sel('ALLOCATOR_BREAKER_STAKES')", "sel('ALLOCATOR_BREAKER_STREAK')", "sel('FADE_REENTRY_COOLDOWN_H')", "fadeGuardLine", "/api/bot-params", "circuit breaker"))
      and all(f"'{k}'" in html for k in ("ALLOCATOR_BREAKER", "ALLOCATOR_BREAKER_STAKES", "ALLOCATOR_BREAKER_STREAK", "FADE_REENTRY_COOLDOWN_H")) and "=== 'breaker'" in html)
compose = src("docker-compose.yml")
check("COMPOSE / ENV: the four new variables are passed (breaker three to survival-agent, the cooldown to the backend and to the Fade bot) and are in the import file and the docs",
      all(f"{k}=${{{k}:-" in compose for k in ("ALLOCATOR_BREAKER", "ALLOCATOR_BREAKER_STAKES", "ALLOCATOR_BREAKER_STREAK", "FADE_REENTRY_COOLDOWN_H")) and compose.count("FADE_REENTRY_COOLDOWN_H=") == 2
      and all(k in src(".env.example") and k in src("ENVIRONMENT_VARIABLES.md") for k in ("ALLOCATOR_BREAKER", "ALLOCATOR_BREAKER_STAKES", "ALLOCATOR_BREAKER_STREAK", "FADE_REENTRY_COOLDOWN_H")))

print("\nALL P26 CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
