"""
P25: allocator `grow` policy. Offline: hand-built states and trades, a fake fleet for one end-to-end run, stubbed freqtrade for the stake multiplier.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
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


tmp = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(tmp)
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)

from backend.engine import allocator as al, portfolio_brain as pb, runtime_settings as rs  # noqa: E402

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def trade(days_ago, pnl, bot_open=False):
    cd = NOW - timedelta(days=days_ago)
    return {"is_open": bot_open, "close_date": cd.isoformat(), "open_date": (cd - timedelta(hours=3)).isoformat(), "close_profit_abs": pnl, "close_profit": pnl / 100.0, "stake_amount": 50.0, "pair": "BTC/USDT"}


def many(n, pnl, start=1, step=0.2):
    return [trade(start + i * step, pnl) for i in range(n)]


ACTIVE = {"status": "active"}
# ======================================================================= one bot at a time
cls, w, why = al.grow_bot("main", dict(ACTIVE), many(29, 1.0), "unproven", 5.0, NOW, "control_long")
check("MEASURING: under 30 trades a bot trades at full size, whatever its results", cls == "neutral" and w == 1.0 and "29 of 30" in why)
cls, w, _ = al.grow_bot("main", dict(ACTIVE), many(29, -3.0), "no_edge", 5.0, NOW, "control_long")
check("MEASURING: 29 losing trades are not enough to stop a bot", w == 1.0)
cls, w, why = al.grow_bot("main", dict(ACTIVE), many(30, 1.0), "unproven", 0.0, NOW, "control_long")
check("WINNER: 30 trades, net > 0 over the period and the last 7 days -> 1.5x", cls == "winner" and w == 1.5)
cls, w, _ = al.grow_bot("main", dict(ACTIVE), many(30, 1.0), "edge", 0.0, NOW, "control_long")
check("EDGE: the same with evidence status `edge` -> 2.0x", cls == "edge" and w == 2.0)
recent_loss = many(30, 1.0, start=8) + [trade(1, -5.0)]
cls, w, _ = al.grow_bot("main", dict(ACTIVE), recent_loss, "unproven", 0.0, NOW, "control_long")
check("NOT YET A WINNER: net > 0 over the period but a loss in the last 7 days -> full size, not a raise", cls == "neutral" and w == 1.0)
rec = dict(ACTIVE)
cls, w, why = al.grow_bot("main", rec, many(30, -1.0), "no_edge", 0.0, NOW, "control_long")
check("LOSER: 30 trades, net < 0 over the period and the last 7 days -> stopped (weight 0), status remembered", cls == "stopped" and w == 0.0 and rec["status"] == "stopped" and rec["status_since"])
check("LOSER: a long-ago loss streak that has recovered in the last 7 days is not stopped", al.grow_bot("main", dict(ACTIVE), many(30, -1.0, start=8) + many(5, 2.0, start=0.5), "unproven", 0.0, NOW, "control_long")[1] == 1.0)
# stopped -> trial
st = {"status": "stopped", "status_since": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")}
cls, w, why = al.grow_bot("main", dict(st), [], "no_edge", 9.0, NOW, "control_long")
check("RESTART: stopped for 2 days -> still stopped even if the control earns (a minimum wait)", cls == "stopped" and w == 0.0)
st3 = {"status": "stopped", "status_since": (NOW - timedelta(days=3, hours=1)).isoformat().replace("+00:00", "Z")}
rec = dict(st3)
cls, w, why = al.grow_bot("main", rec, [], "no_edge", 9.0, NOW, "control_long")
check("RESTART: stopped >= 3 days and the control bot earned over the last 7 days -> a trial at 0.5x", cls == "trial" and w == 0.5 and rec["status"] == "trial" and rec["trial_start"] and "control_long" in why)
check("RESTART: the control earned nothing (or is unknown) -> stays stopped", al.grow_bot("main", dict(st3), [], "no_edge", 0.0, NOW, "control_long")[0] == "stopped" and al.grow_bot("main", dict(st3), [], "no_edge", None, NOW, "control_long")[0] == "stopped")
# trial outcomes
trial_mid = {"status": "trial", "trial_start": (NOW - timedelta(days=3)).isoformat().replace("+00:00", "Z")}
cls, w, why = al.grow_bot("main", dict(trial_mid), [trade(1, -1.0)], "no_edge", 0.0, NOW, "control_long")
check("TRIAL: inside the 7 days it stays at 0.5x whatever happens (it is a trial)", cls == "trial" and w == 0.5 and "day 4 of 7" in why)
trial_done = {"status": "trial", "trial_start": (NOW - timedelta(days=8)).isoformat().replace("+00:00", "Z")}
rec = dict(trial_done)
cls, w, _ = al.grow_bot("main", rec, many(6, 1.0, start=0.5), "no_edge", 0.0, NOW, "control_long")
check("TRIAL: >= 5 trades and net > 0 after 7 days -> the bot is active again (full size, judged normally)", rec["status"] == "active" and w == 1.0 and cls == "neutral")
rec = dict(trial_done)
cls, w, _ = al.grow_bot("main", rec, many(6, -1.0, start=0.5), "no_edge", 0.0, NOW, "control_long")
check("TRIAL: >= 5 trades and net <= 0 -> stopped again, the wait restarts", cls == "stopped" and w == 0.0 and rec["status"] == "stopped")
cls, w, why = al.grow_bot("main", dict(trial_done), many(2, 1.0, start=0.5), "no_edge", 0.0, NOW, "control_long")
check("TRIAL: fewer than 5 trades after 7 days -> the trial is extended, not judged on noise", cls == "trial" and w == 0.5 and "extended" in why)
ep = {"status": "active", "epoch_since": (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z")}
cls, w, why = al.grow_bot("main", ep, many(40, -2.0, start=3) + many(5, 1.0, start=0.2), "no_edge", 0.0, NOW, "control_long")
check("EPOCH: results from before the configuration changed are ignored (only the 5 new trades count)", w == 1.0 and "5 of 30" in why)

# ======================================================================= the whole policy
def bots_state(**kw):
    out = {}
    for b in al.MANAGED_BOTS:
        out[b] = {"reachable": True, "evidence_status": kw.get("ev", {}).get(b, "unproven"), "d7": {}, "d30": {}}
    return out


def mk_state(trades=None, dd=None, partial=False, open_trades=None, controls=None, equity=1800.0, ev=None):
    return {"bots": bots_state(ev=ev or {}), "trades_by_bot": {b: (trades or {}).get(b, []) for b in al.MANAGED_BOTS}, "drawdown": {"drawdown_from_hwm_pct": dd, "highest_rung": None},
            "partial": partial, "open_trades": open_trades or {}, "equity": equity, "controls": controls or {"control_long": {"d7": {"net_pnl_usd": 0.0}}, "control_short": {"d7": {"net_pnl_usd": 0.0}}}}


SET = al.load_settings({})
check("SETTINGS: the default policy is grow; junk falls back to protect (safe); protect is accepted", SET["policy"] == "grow" and al.load_settings({"ALLOCATOR_POLICY": "yolo"})["policy"] == "protect" and al.load_settings({"ALLOCATOR_POLICY": "PROTECT"})["policy"] == "protect")
check("SETTINGS: a bot's signature is its decision source / provider (rule bots: 'rules')", SET["bot_signature"]["main"] == "kronos_llm/deepseek" and SET["bot_signature"]["listing_fade"] == "rules"
      and al.load_settings({"MAIN_DECISION_SOURCE": "kronos"})["bot_signature"]["main"] == "kronos/deepseek")
prev1 = {b: 1.0 for b in al.MANAGED_BOTS}
st_file: dict = {}
g = al.grow_policy(mk_state(), prev1, st_file, NOW, SET, None)
check("FIRST RUN: LLM bots start a new epoch now (everything neutral, full size); rule bots judge their last 30 days", all(w == 1.0 for w in g["final"].values()) and st_file["grow"]["main"]["epoch_since"] and st_file["grow"]["listing_fade"]["epoch_since"] is None)
# the real case of 2026-10-12: every weight 0 from the old rules, nothing to judge -> grow does not idle anybody
zero = {b: 0.0 for b in al.MANAGED_BOTS}
bad_state = mk_state(dd=-1.2)
bad_state["drawdown"] = {"drawdown_from_hwm_pct": -1.2, "highest_rung": {"level": "L3 (-8% from HWM)"}, "weekly_limit_breached": True, "monthly_limit_breached": True}
bad_state.update({"budget": 0.0, "gross": 268.0, "floor": 2070.0})
g = al.grow_policy(bad_state, zero, {}, NOW, SET, None)
check("NO IDLING: weekly-loss, ladder and CPPI flags (budget 0) that froze every bot under `protect` change nothing under grow: every bot is back at full size", all(w == 1.0 for w in g["final"].values()) and not any("cppi" in r or "weekly" in r or "ladder" in r for r in g["rules_fired"]))
# a winner among losers
tr = {"listing_fade": many(40, 0.5), "main": many(40, -0.5), "shorts": many(10, 1.0)}
sf = {"grow": {b: {"status": "active", "sig": SET["bot_signature"][b], "epoch_since": None} for b in al.MANAGED_BOTS}}
g = al.grow_policy(mk_state(trades=tr), prev1, sf, NOW, SET, None)
check("MIX: the earner goes up (stepwise: +0.25 a day), the loser is stopped at once, the one with too few trades stays at 1.0",
      g["final"]["listing_fade"] == 1.25 and g["final"]["main"] == 0.0 and g["final"]["shorts"] == 1.0 and g["detail"]["listing_fade"]["class"] == "winner" and g["detail"]["main"]["class"] == "stopped")
g2 = al.grow_policy(mk_state(trades=tr), g["final"], sf, NOW + timedelta(days=1), SET, None)
g3 = al.grow_policy(mk_state(trades=tr), g2["final"], sf, NOW + timedelta(days=2), SET, None)
check("MIX: the raise reaches 1.5 on day 2 and stays there; the stopped bot stays stopped", g2["final"]["listing_fade"] == 1.5 and g3["final"]["listing_fade"] == 1.5 and g3["final"]["main"] == 0.0)
sf2 = {"grow": {"listing_fade": {"status": "active", "sig": "rules", "epoch_since": None}}}
g = al.grow_policy(mk_state(trades={"listing_fade": many(40, 0.5)}), {b: 0.0 for b in al.MANAGED_BOTS}, sf2, NOW, SET, None)
check("WINNER FROM ZERO: a winner is never below 1.0 on the day it is classified", g["final"]["listing_fade"] >= 1.0)
# restart through the control bot, end to end
sf3 = {"grow": {"main": {"status": "stopped", "status_since": (NOW - timedelta(days=4)).isoformat().replace("+00:00", "Z"), "sig": SET["bot_signature"]["main"], "epoch_since": None}}}
g = al.grow_policy(mk_state(controls={"control_long": {"d7": {"net_pnl_usd": 7.5}}, "control_short": {"d7": {"net_pnl_usd": -1.0}}}), {**prev1, "main": 0.0}, sf3, NOW, SET, None)
check("RESTART end to end: main (spot, long) is judged by control_long: it earned $7.50 in 7 days -> trial at 0.5x, reason names it", g["final"]["main"] == 0.5 and "control_long" in g["reasons"]["main"] and g["detail"]["main"]["class"] == "trial")
sf4 = {"grow": {"shorts": {"status": "stopped", "status_since": (NOW - timedelta(days=4)).isoformat().replace("+00:00", "Z"), "sig": SET["bot_signature"]["shorts"], "epoch_since": None}}}
g = al.grow_policy(mk_state(controls={"control_long": {"d7": {"net_pnl_usd": 7.5}}, "control_short": {"d7": {"net_pnl_usd": -1.0}}}), {**prev1, "shorts": 0.0}, sf4, NOW, SET, None)
check("RESTART end to end: shorts (futures) is judged by control_short: it lost money -> stays stopped even though control_long earned", g["final"]["shorts"] == 0.0)
# configuration change = new epoch
sf5 = {"grow": {"shorts": {"status": "stopped", "status_since": (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"), "sig": "kronos_llm/deepseek", "epoch_since": None}}}
st5 = mk_state(trades={"shorts": many(40, -1.0)})
g = al.grow_policy(st5, {**prev1, "shorts": 0.0}, sf5, NOW, al.load_settings({"SHORTS_DECISION_SOURCE": "kronos"}), None)
check("NEW CONFIGURATION: changing SHORTS_DECISION_SOURCE ends the old verdict: the stopped bot is active again at full size and its old losses do not count", g["final"]["shorts"] == 1.0 and sf5["grow"]["shorts"]["sig"] == "kronos/deepseek" and sf5["grow"]["shorts"]["status"] == "active")
# protection only scales
base = {"grow": {b: {"status": "active", "sig": SET["bot_signature"][b], "epoch_since": None} for b in al.MANAGED_BOTS}}
g = al.grow_policy(mk_state(dd=-16.0), prev1, json.loads(json.dumps(base)), NOW, SET, None)
check("DRAWDOWN: -16% from the high-water mark scales every weight x0.5 (never 0)", all(w == 0.5 for w in g["final"].values()) and g["drawdown_factor"] == 0.5)
g = al.grow_policy(mk_state(dd=-30.0), prev1, json.loads(json.dumps(base)), NOW, SET, None)
check("DRAWDOWN: -30% scales x0.25 and still no bot is at 0 (the book is never idled)", all(w == 0.25 for w in g["final"].values()))
g = al.grow_policy(mk_state(dd=-30.0, partial=True), prev1, json.loads(json.dumps(base)), NOW, SET, None)
check("DRAWDOWN: incomplete data (a bot missing) -> no scaling on a number that is not reliable", all(w == 1.0 for w in g["final"].values()))
g = al.grow_policy(mk_state(), prev1, json.loads(json.dumps(base)), NOW, SET, True)
check("MACRO: a high-impact event within 24 h caps every weight at 0.5", all(w == 0.5 for w in g["final"].values()) and "macro_event_within_24h_cap_0.5" in g["rules_fired"])
opp = {"shorts": [{"pair": "NEAR/USDT:USDT", "stake_amount": 10.0, "is_short": True, "leverage": 1.0}], "trend_ensemble": [{"pair": "NEAR/USDT", "stake_amount": 30.0, "is_short": False, "leverage": 1.0}],
       "trend_v2": [{"pair": "NEAR/USDT:USDT", "stake_amount": 30.0, "is_short": False, "leverage": 1.0}]}
g = al.grow_policy(mk_state(open_trades=opp), prev1, json.loads(json.dumps(base)), NOW, SET, None)
check("OPPOSITE SIDES: the smaller side is capped at 0.5, not stopped", g["final"]["shorts"] == 0.5 and g["final"]["trend_v2"] == 1.0)
g = al.grow_policy(mk_state(open_trades={"moonshot": [{"pair": "PONS/USDT", "stake_amount": 500.0, "is_short": False, "leverage": 1.0}]}, equity=1800.0), prev1, json.loads(json.dumps(base)), NOW, SET, None)
check("CONCENTRATION: a coin at >= 25% of equity caps the bots holding it at 0.5", g["final"]["moonshot"] == 0.5)
unreach = mk_state()
unreach["bots"]["main"] = {"reachable": False}
g = al.grow_policy(unreach, {**prev1, "main": 0.7}, json.loads(json.dumps(base)), NOW, SET, None)
check("UNREACHABLE: a bot that does not answer keeps its weight", g["final"]["main"] == 0.7 and g["detail"]["main"]["class"] == "unreachable")
check("RETIRED: a retired bot gets no verdict and no state", "main" not in al.grow_policy(mk_state(), prev1, {}, NOW, al.load_settings({"ALLOCATOR_RETIRED_BOTS": "main"}), None)["detail"])

# ======================================================================= policy choice and automatic switch
check("POLICY: grow when configured and nothing is live", al.effective_policy(SET, None) == ("grow", None) and al.effective_policy(SET, {"live_bots": [], "unverified_bots": []})[0] == "grow")
pol, note = al.effective_policy(SET, {"live_bots": ["shorts"], "unverified_bots": []})
check("AUTOMATIC SWITCH: a bot that is live (dry_run=false) forces protect for that run, with the reason", pol == "protect" and "shorts" in note)
check("AUTOMATIC SWITCH: a bot whose dry_run cannot be verified forces protect too", al.effective_policy(SET, {"live_bots": [], "unverified_bots": ["main"]})[0] == "protect")
check("MANUAL SWITCH: protect configured stays protect", al.effective_policy(al.load_settings({"ALLOCATOR_POLICY": "protect"}), None) == ("protect", None))

# ======================================================================= end to end through run_allocation (shadow): no LLM call under grow
class Fake:
    def reachable(self, bot):
        return True

    def balance(self, bot):
        return {"total": 300.0, "starting_capital": pb.DEFAULT_START_CAPITAL.get(bot, 300.0)}

    def status(self, bot):
        return []

    def trades(self, bot):
        return {"listing_fade": many(40, 0.5), "main": many(40, -0.5)}.get(bot, [])

    def _json(self, bot, path):
        return {"state": "running", "dry_run": True, "max_open_trades": 3}


calls = []


def llm(system, user):
    calls.append(1)
    return json.dumps({b: {"weight": 0.5, "reason": "x"} for b in al.MANAGED_BOTS})


d = tmp / "alloc"
d.mkdir()
(d / al.STATE_FILE).write_text(json.dumps({"grow": {"main": {"status": "active", "sig": "kronos_llm/deepseek", "epoch_since": None}}}))      # main has been on this configuration for long
out = al.run_allocation(d, NOW, Fake(), llm, None, env={"PORTFOLIO_BRAIN_MODE": "shadow"})
check("RUN (grow): no LLM call is made", calls == [] and out["policy"] == "grow")
check("RUN (grow): the earner is raised (step-limited to 1.25), the loser is 0, the record says why and carries the classes", out["final"]["listing_fade"] == 1.25 and out["final"]["main"] == 0.0 and out["grow"]["listing_fade"]["class"] == "winner"
      and out["llm"]["reasons"]["main"].startswith("loses") and out["llm"]["attempts"] == 0)
rec = json.loads((d / al.ALLOCATIONS_FILE).read_text(encoding="utf-8").splitlines()[-1])
check("RUN (grow): allocations.jsonl keeps the policy and the per-bot classes (audit trail)", rec["policy"] == "grow" and rec["grow"]["main"]["class"] == "stopped")
st = json.loads((d / al.STATE_FILE).read_text(encoding="utf-8"))
check("RUN (grow): the state file keeps each bot's status / epoch for the next day", st["grow"]["main"]["status"] == "stopped" and st["grow"]["listing_fade"]["sig"] == "rules")
calls.clear()
out2 = al.run_allocation(d, NOW + timedelta(days=1), Fake(), llm, None, env={"PORTFOLIO_BRAIN_MODE": "shadow", "ALLOCATOR_POLICY": "protect"})
check("RUN (protect): the original path still asks the LLM once and reports policy protect", len(calls) == 1 and out2["policy"] == "protect")

# ======================================================================= stake multiplier above 1 in the real strategies
DIRS = ["freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-v2", "freqtrade-shorts-v2"]
hashes = {hashlib.sha256((REPO / x / "user_data" / "strategies" / "_allocator_ctx.py").read_bytes()).hexdigest() for x in DIRS}
check("HELPER: the multiplier helper is byte-identical in all six bot directories and allows up to 2.0", len(hashes) == 1 and "MAX_STAKE_MULT = 2.0" in (REPO / "freqtrade" / "user_data" / "strategies" / "_allocator_ctx.py").read_text(encoding="utf-8"))


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
shared = tmp / "shared"
shared.mkdir()
os.environ["ALLOCATOR_DIR"] = str(shared)


def weights(bot, w):
    (shared / "weights.json").write_text(json.dumps({"mode": "enforce", "updated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "weights": {bot: {"weight": w}}}))


SPECS = {"main": ("freqtrade/user_data/strategies/DeepSeekStrategy.py", "DeepSeekStrategy"), "shorts": ("freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py", "DeepSeekShortsStrategy"),
         "moonshot": ("freqtrade-moonshot/user_data/strategies/DeepSeekMoonshotStrategy.py", "DeepSeekMoonshotStrategy"), "listing_fade": ("freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py", "ListingFadeStrategy"),
         "trend_v2": ("freqtrade-trend-v2/user_data/strategies/TrendV2Strategy.py", "TrendV2Strategy")}
for bot, (rel, cls_name) in SPECS.items():
    mod = load(rel, f"p25g_{bot}")
    obj = getattr(mod, cls_name)({"dry_run": True})
    obj._custom_stake_amount_base = lambda *a, **k: 50.0
    call = lambda mn, mx: obj.custom_stake_amount("X/USDT", NOW, 1.0, 50.0, mn, mx, 1.0, None, "long")  # noqa: E731
    mod._allocator_ctx._cache.clear()
    weights(bot, 1.5)
    r15 = call(5.0, 1000.0)
    mod._allocator_ctx._cache.clear()
    weights(bot, 2.0)
    r2 = call(5.0, 1000.0)
    r2_cap = call(5.0, 80.0)
    mod._allocator_ctx._cache.clear()
    weights(bot, 1.0)
    r1 = call(5.0, 1000.0)
    mod._allocator_ctx._cache.clear()
    weights(bot, 0.5)
    r05 = call(5.0, 1000.0)
    mod._allocator_ctx._cache.clear()
    weights(bot, 0.05)
    rmin = call(5.0, 1000.0)
    check(f"STAKE {bot}: weight 1.5 -> 75, 2.0 -> 100 (capped at the exchange / wallet maximum 80), 1.0 -> 50, 0.5 -> 25, tiny weight -> the minimum order", (r15, r2, r2_cap, r1, r05, rmin) == (75.0, 100.0, 80.0, 50.0, 25.0, 5.0))

# ======================================================================= the switch
check("SWITCH: ALLOCATOR_POLICY is a dashboard setting (grow | protect), invalid values are refused", rs.SPEC["ALLOCATOR_POLICY"]["options"] == ["grow", "protect"] and rs.SPEC["ALLOCATOR_POLICY"]["default"] == "grow"
      and rs.SPEC["ALLOCATOR_POLICY"]["validate"]("yolo") is None and rs.SPEC["ALLOCATOR_POLICY"]["validate"]("protect") == "protect")
rs.save({"ALLOCATOR_POLICY": "protect"}, tmp)
check("SWITCH: a saved dashboard value overrides the environment and the allocator reads it", rs.describe(tmp)["ALLOCATOR_POLICY"]["source"] == "ui" and al.load_settings(rs.merged_env(tmp))["policy"] == "protect")
html = (REPO / "frontend" / "index.html").read_text(encoding="utf-8")
check("UI: the allocator card has the policy switch, shows the policy in force and a forced switch", "set_ALLOCATOR_POLICY" in html or "sel('ALLOCATOR_POLICY')" in html and "policy_note" in html and "drawdown scaling" in html)
compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
check("COMPOSE / ENV: ALLOCATOR_POLICY is passed to survival-agent (default grow) and is in the import file", "ALLOCATOR_POLICY=${ALLOCATOR_POLICY:-grow}" in compose and "ALLOCATOR_POLICY=grow" in (REPO / ".env.example").read_text(encoding="utf-8"))

print("\nALL P25 GROW POLICY CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
