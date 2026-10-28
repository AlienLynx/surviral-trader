"""
P17-A: portfolio brain (shadow mode). Pure rules, an end-to-end run against fake
bots, proof that the shadow run sends only GETs to bots, and the endpoint/wiring.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.engine import portfolio_brain as pb  # noqa: E402

# =========================================================== pure rules
fc = pb.floor_and_cushion(2600.0, 2600.0, 0.90, 3.0)
check("floor = 90% of reference, cushion = equity - floor",
      fc["floor"] == 2340.0 and fc["cushion"] == 260.0)
check("CPPI budget = 3x cushion, also reports 5x", fc["cppi_budget"] == 780.0 and fc["cppi_budget_5x"] == 1300.0)
neg = pb.floor_and_cushion(2300.0, 2600.0, 0.90, 3.0)
check("equity below the floor -> negative cushion but budget clamps to 0",
      neg["cushion"] == -40.0 and neg["cppi_budget"] == 0.0 and neg["cppi_budget_5x"] == 0.0)

T = lambda pair, stake, short=False, lev=1.0: {"pair": pair, "stake_amount": stake, "is_short": short, "leverage": lev}  # noqa: E731
opens = {
    "moonshot": [T("SUI/USDT", 25), T("LINK/USDT", 25)],
    "trend_ensemble": [T("SUI/USDT", 10), T("BTC/USDT", 50), T("LINK/USDT", 10)],
    "shorts": [T("SUI/USDT:USDT", 30, short=True), T("LINK/USDT:USDT", 20)],
    "listing_fade": [T("SUI/USDT:USDT", 10, short=True), T("ETH/USDT:USDT", 40, short=True, lev=2.0)],
}
ex = pb.exposure(opens, alt_beta=1.0)
check("gross exposure = sum of stake x leverage", ex["gross"] == 25 + 25 + 10 + 50 + 10 + 30 + 20 + 10 + 80)
check("net exposure: longs +, shorts -", ex["net"] == (25 + 25 + 10 + 50 + 10 + 20) - (30 + 10 + 80))
check("leverage multiplies the notional (ETH short 40 x2 = 80)", ex["by_coin"]["ETH"]["short"] == 80.0)
check("per-coin net for SUI = 35 long - 40 short", ex["by_coin"]["SUI"]["net"] == -5.0)
check("opposite-side conflict detected on SUI (long moonshot/trend vs short shorts/listing_fade)",
      any(c["coin"] == "SUI" and "moonshot" in c["long_bots"] and "shorts" in c["short_bots"] for c in ex["opposite_side_conflicts"]))
check("crowded coin detected (LINK in 3 bots)", any(c["coin"] == "LINK" and c["bots"] == 3 for c in ex["crowded_coins"]))
check("BTC-equivalent net treats every alt as beta 1", ex["net_btc_equivalent"] == ex["net"])
ex_half = pb.exposure(opens, alt_beta=0.5)
btc_net = 50.0
check("alt_beta scales only the alts: net_btc_eq = btc_net + beta x alt_net",
      abs(ex_half["net_btc_equivalent"] - (btc_net + 0.5 * (ex["net"] - btc_net))) < 1e-9)
budget = pb.exposure_vs_budget(ex, fc)
check("exposure_vs_budget flags a book bigger than the CPPI budget (the expected finding)",
      budget["exceeds_budget"] is False and budget["gross_exposure"] == 260.0)   # 260 < 780: within
big = pb.exposure({"a": [T("BTC/USDT", 1500)]}, 1.0)
check("a 1500 gross book exceeds a 780 budget and the 5x budget is 1300", pb.exposure_vs_budget(big, fc)["exceeds_budget"]
      and pb.exposure_vs_budget(big, fc)["exceeds_budget_5x"] and pb.exposure_vs_budget(big, fc)["gross_over_budget_ratio"] == 1.92)
zero_budget = pb.exposure_vs_budget(big, neg)
check("zero budget -> exceeds, ratio undefined (None), no ZeroDivisionError",
      zero_budget["exceeds_budget"] and zero_budget["gross_over_budget_ratio"] is None)

d = pb.drawdown_state(1000.0, 1000.0, 1000.0, 1000.0)
check("no drawdown -> no rung", d["highest_rung"] is None and d["rungs_triggered"] == [])
check("-3.1% -> L1 only", [r["level"][:2] for r in pb.drawdown_state(969.0, 1000.0, None, None)["rungs_triggered"]] == ["L1"])
check("-6.1% -> L1+L2, highest L2", pb.drawdown_state(939.0, 1000.0, None, None)["highest_rung"]["level"].startswith("L2"))
d3 = pb.drawdown_state(919.0, 1000.0, None, None)
check("-8.1% -> L3 with a hypothetical 'would' text, all three rungs listed",
      d3["highest_rung"]["level"].startswith("L3") and len(d3["rungs_triggered"]) == 3 and "would" in d3["highest_rung"]["would"])
check("drawdown note states nothing is applied", "not applied" in d3["note"] or "nothing above is applied" in d3["note"])
dw = pb.drawdown_state(979.0, 1000.0, 1000.0, None)
check("weekly -2.1% breaches the weekly limit", dw["weekly_limit_breached"] and dw["weekly_would"] is not None)
check("weekly -1.5% does not", not pb.drawdown_state(985.0, 1000.0, 1000.0, None)["weekly_limit_breached"])
dm = pb.drawdown_state(959.0, 1000.0, None, 1000.0)
check("monthly -4.1% breaches the monthly limit", dm["monthly_limit_breached"] and dm["monthly_would"] is not None)
check("missing history -> weekly/monthly are None, not breached",
      dw["monthly_change_pct"] is None and not dw["monthly_limit_breached"])

tp = pb.target_progress("month", 1012.0, 1000.0, 1.0, 900.0)
check("target reached -> 'protect gains: risk x0.5, floor +50% of gains' with floor + half the gain",
      tp["reached"] and tp["protect_gains"] == "protect gains: risk x0.5, floor +50% of gains" and tp["proposed_floor"] == 906.0)
miss = pb.target_progress("month", 1005.0, 1000.0, 1.0, 900.0)
check("missing the target triggers nothing", not miss["reached"] and "protect_gains" not in miss)
check("no period-start snapshot -> explicit note, not a crash", pb.target_progress("week", 1000.0, None, 0.25, 900.0)["gain_pct"] is None)

# ---- decay
bad = [(-0.02 if i % 2 else -0.005) for i in range(30)]
dc = pb.decay_flag(bad)
check("30 trades with a clearly negative mean -> t < -2 -> flagged", dc["flag"] and dc["t_stat"] < -2 and dc["n"] == 30)
check("29 trades is never enough to flag", not pb.decay_flag(bad[:29])["flag"])
check("30 winning trades -> not flagged", not pb.decay_flag([0.01 + (i % 3) * 0.001 for i in range(30)])["flag"])
check("zero-variance window -> t undefined, not flagged, no crash", pb.decay_flag([-0.01] * 30)["t_stat"] is None and not pb.decay_flag([-0.01] * 30)["flag"])
check("only the LAST 30 trades count (old losses forgotten)",
      not pb.decay_flag(bad + [0.02 + (i % 3) * 0.001 for i in range(30)])["flag"])
check("tstat of a single value is None", pb.tstat([0.1]) is None)

# ---- cohort metrics / no added value
now = datetime(2026, 10, 1, 0, 10, tzinfo=timezone.utc)
since = now - timedelta(days=30)
mk = lambda days_ago, p, fee=0.1: {"is_open": False, "close_date": (now - timedelta(days=days_ago)).isoformat(),  # noqa: E731
                                   "close_profit_abs": p, "fee_open_cost": fee, "fee_close_cost": fee}
tr = [mk(40, 99.0), mk(20, 10.0), mk(15, -30.0), mk(5, 5.0), {"is_open": True, "close_date": None}]
m = pb.bot_metrics(tr, since, 500.0)
check("metrics ignore trades closed before the control start and still-open trades", m["trades"] == 3)
check("return % is profit over the bot's own start capital", m["profit_abs"] == -15.0 and m["return_pct"] == -3.0)
check("win rate / expectancy", m["win_rate_pct"] == 66.7 and m["expectancy_usd"] == -5.0)
check("max drawdown from the closed-trade equity curve", abs(m["max_drawdown_pct"] - (30.0 / 510.0 * 100)) < 0.01)
check("fees are summed (open+close)", m["fees_usd"] == 0.6)
b_lose, c_win = {"trades": 35, "return_pct": -2.0}, {"trades": 50, "return_pct": 1.0}
check("no-added-value: losing to control for >=28 days with >=30 trades -> flagged", pb.no_added_value(b_lose, c_win, 30)["flag"])
check("no-added-value: only 20 days -> not flagged", not pb.no_added_value(b_lose, c_win, 20)["flag"])
check("no-added-value: 29 trades -> not flagged", not pb.no_added_value({"trades": 29, "return_pct": -2.0}, c_win, 40)["flag"])
check("no-added-value: beating the control -> not flagged", not pb.no_added_value({"trades": 40, "return_pct": 3.0}, c_win, 40)["flag"])

# ---- analyst parsing
good = {"regime_note": "book is crowded", "flags": ["a"], "proposals": [{"action": "x", "target": "portfolio", "reason": "y"}]}
check("analyst: valid JSON accepted", pb.parse_analyst(json.dumps(good)) is not None)
check("analyst: ```json fenced``` reply accepted", pb.parse_analyst("```json\n" + json.dumps(good) + "\n```") is not None)
check("analyst: not JSON -> skipped", pb.parse_analyst("I think the portfolio is fine") is None)
check("analyst: missing key -> skipped", pb.parse_analyst(json.dumps({"regime_note": "x", "flags": []})) is None)
check("analyst: wrong types -> skipped", pb.parse_analyst(json.dumps({"regime_note": 5, "flags": [], "proposals": []})) is None)
check("analyst: empty/None -> skipped", pb.parse_analyst(None) is None and pb.parse_analyst("") is None)
check("analyst prompt forbids invented numbers and says nothing is executed",
      "Do not invent numbers" in pb.ANALYST_SYSTEM and "nothing you write is executed" in pb.ANALYST_SYSTEM)

# ---- schedule / settings
check("scheduler: 00:05 -> 5 minutes", pb.seconds_until_next(datetime(2026, 9, 29, 0, 5, tzinfo=timezone.utc)) == 300)
check("scheduler: exactly 00:10 -> next day", pb.seconds_until_next(datetime(2026, 9, 29, 0, 10, tzinfo=timezone.utc)) == 86400)
check("scheduler: 23:59 -> 11 minutes", pb.seconds_until_next(datetime(2026, 9, 29, 23, 59, tzinfo=timezone.utc)) == 660)
s0 = pb.load_settings({})
check("defaults: shadow, floor 0.90, CPPI 3, monthly 1.0, weekly 0.25",
      (s0["mode"], s0["floor_pct"], s0["cppi_multiplier"], s0["target_monthly_pct"], s0["target_weekly_pct"]) == ("shadow", 0.9, 3.0, 1.0, 0.25))
check("P20: the brain itself never acts -- PORTFOLIO_BRAIN_MODE=enforce (the allocator's switch) leaves the brain read-only (shadow)",
      pb.load_settings({"PORTFOLIO_BRAIN_MODE": "enforce"})["mode"] == "shadow"
      and pb.load_settings({"PORTFOLIO_BRAIN_MODE": "bogus"})["mode"] == "off")
check("PORTFOLIO_BRAIN_MODE=off is honoured", pb.load_settings({"PORTFOLIO_BRAIN_MODE": "off"})["mode"] == "off")
check("garbage numeric env falls back to the default", pb.load_settings({"CPPI_MULTIPLIER": "abc"})["cppi_multiplier"] == 3.0)


# ===================================================== end-to-end (fakes)
EQUITY = {"main": 510.0, "shorts": 495.0, "moonshot": 95.0,
          "listing_fade": 90.0, "trend_ensemble": 101.0, "trend_v2": 500.0,
          "control_long": 505.0, "control_short": 498.0}


def closed(days_ago, profit, ratio=None):
    return {"is_open": False, "close_date": (NOW - timedelta(days=days_ago)).isoformat(),
            "open_date": (NOW - timedelta(days=days_ago + 1)).isoformat(),
            "close_profit_abs": profit, "close_profit": ratio if ratio is not None else profit / 100,
            "fee_open_cost": 0.05, "fee_close_cost": 0.05}


NOW = datetime(2026, 10, 15, 0, 10, tzinfo=timezone.utc)


class FakeFetcher:
    def __init__(self, unreachable=(), trades=None, opens=None):
        self.unreachable, self.calls = set(unreachable), []
        self._trades = trades or {}
        self._opens = opens or {}

    def reachable(self, bot):
        self.calls.append(("reachable", bot))
        return bot not in self.unreachable

    def balance(self, bot):
        self.calls.append(("balance", bot))
        return {"total": EQUITY[bot], "starting_capital": pb.DEFAULT_START_CAPITAL[bot]}

    def status(self, bot):
        self.calls.append(("status", bot))
        return self._opens.get(bot, [])

    def trades(self, bot):
        self.calls.append(("trades", bot))
        return self._trades.get(bot, [])


OPENS = {
    "moonshot": [T("SUI/USDT", 400), T("LINK/USDT", 300)],
    "trend_v2": [T("SUI/USDT:USDT", 500, short=True), T("BTC/USDT:USDT", 300)],
    "main": [T("LINK/USDT", 400)],
}
TRADES = {"main": [closed(20, -8.0), closed(10, 2.0)], "control_long": [closed(20, 4.0), closed(5, 3.0)],
          "trend_v2": [closed(12, 6.0)], "control_short": [closed(9, -2.0)]}
LLM_GOOD = lambda system, user: json.dumps({"regime_note": "book is crowded on LINK", "flags": ["gross above budget"],  # noqa: E731
                                            "proposals": [{"action": "cap LINK", "target": "portfolio", "reason": "3 bots long"}]})
BTC_HTTP = type("R", (), {"raise_for_status": lambda s: None,
                          "json": lambda s: {"result": {"list": [["1", "100", "0", "0", "110"], ["0", "100", "0", "0", "100"]]}}})()

with tempfile.TemporaryDirectory() as td:
    data_dir = Path(td)
    (data_dir / "capital.json").write_text(json.dumps({"main": {"contributions": [{"amount": 500.0}]}}))
    fake = FakeFetcher(trades=TRADES, opens=OPENS)
    rep = pb.run_daily(data_dir, now=NOW, fetcher=fake, llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)

    check("mode is shadow by default", rep["mode"] == "shadow" and rep["policy_note"].startswith("SHADOW MODE"))
    portfolio_sum = sum(EQUITY[b] for b in pb.PORTFOLIO_BOTS)
    check("equity = sum of the 6 portfolio bots only (controls excluded)", rep["capital"]["equity"] == round(portfolio_sum, 2))
    check("controls are read but never summed into equity",
          "control_long" not in rep["capital"]["per_bot_equity"] and any(c[1] == "control_long" for c in fake.calls))
    check("reference capital = sum of the 6 bots' starting capital", rep["capital"]["reference_capital"] == 1800.0)
    check("HWM initialised to the first equity, floor 1620", rep["capital"]["hwm"] == round(portfolio_sum, 2) and rep["capital"]["floor"] == 1620.0)
    check("exposure counted only for portfolio bots", rep["exposure"]["gross"] == 400 + 300 + 500 + 300 + 400)
    check("CPPI comparison present and the 1900 gross book exceeds the budget",
          rep["exposure_vs_budget"]["exceeds_budget"] is True and rep["exposure_vs_budget"]["cppi_budget"] == round(3 * (portfolio_sum - 1620), 2))
    check("conflicts: SUI long in moonshot vs short in trend_v2", any(c["coin"] == "SUI" for c in rep["exposure"]["opposite_side_conflicts"]))
    check("cohorts: since = earliest control trade open date", rep["cohorts"]["since"] is not None)
    check("cohorts: every reachable bot is tagged with its cohort",
          rep["cohorts"]["bots"]["main"]["cohort"] == "llm" and rep["cohorts"]["bots"]["trend_v2"]["cohort"] == "rules"
          and rep["cohorts"]["bots"]["control_long"]["cohort"] == "control")
    check("BTC buy-and-hold benchmark computed from the (mocked) klines", rep["cohorts"]["btc_buy_hold_return_pct"] == 10.0)
    check("analyst output stored with the prompt version", rep["analyst"]["prompt_version"] == pb.PROMPT_VERSION
          and rep["analyst"]["proposals"][0]["action"] == "cap LINK")
    snaps = [json.loads(l) for l in (data_dir / pb.SNAPSHOTS_FILE).read_text().splitlines()]
    check("one jsonl snapshot written for the day", len(snaps) == 1 and snaps[0]["date"] == "2026-10-15" and not snaps[0]["partial"])
    check("latest.json and the markdown report are written",
          (data_dir / pb.LATEST_FILE).exists() and (data_dir / "portfolio_brain_report_2026-10-15.md").exists())
    md = (data_dir / "portfolio_brain_report_2026-10-15.md").read_text()
    check("markdown says shadow / nothing applied and flags the budget breach", "nothing applied" in md and "EXCEEDS" in md)

    # second run the same day: snapshot replaced, not duplicated; HWM never falls
    EQUITY_BACKUP = dict(EQUITY)
    EQUITY["main"] = 432.0
    rep2 = pb.run_daily(data_dir, now=NOW + timedelta(hours=3), fetcher=FakeFetcher(trades=TRADES, opens=OPENS),
                        llm=lambda s, u: None, env={}, http_get=lambda *a, **k: BTC_HTTP)
    snaps2 = [json.loads(l) for l in (data_dir / pb.SNAPSHOTS_FILE).read_text().splitlines()]
    check("re-running the same day replaces that day's snapshot (no duplicates)", len(snaps2) == 1)
    check("HWM never decreases", rep2["capital"]["hwm"] == rep["capital"]["hwm"])
    check("drawdown is measured from the HWM (-4.4% -> L1 hypothetical rung)",
          rep2["drawdown"]["highest_rung"] is not None and rep2["drawdown"]["highest_rung"]["level"].startswith("L1"))
    check("analyst failure (None reply) is skipped, the run still completes", rep2["analyst"] is None)
    EQUITY.update(EQUITY_BACKUP)

    # next day: weekly/monthly windows have no snapshot old enough yet -> None
    rep3 = pb.run_daily(data_dir, now=NOW + timedelta(days=1), fetcher=FakeFetcher(trades=TRADES, opens=OPENS),
                        llm=lambda s, u: "not json", env={}, http_get=lambda *a, **k: BTC_HTTP)
    check("a 1-day-old history gives no weekly/monthly loss figure (never computed over <7 days)",
          rep3["drawdown"]["weekly_change_pct"] is None and rep3["drawdown"]["monthly_change_pct"] is None)
    check("invalid analyst JSON is skipped", rep3["analyst"] is None)
    # eight days later a weekly comparison exists
    rep4 = pb.run_daily(data_dir, now=NOW + timedelta(days=9), fetcher=FakeFetcher(trades=TRADES, opens=OPENS),
                        llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    check("after >7 days a weekly change is reported", rep4["drawdown"]["weekly_change_pct"] is not None)

with tempfile.TemporaryDirectory() as td:
    # partial data: one bot unreachable -> drawdown NOT evaluated, snapshot flagged partial, HWM untouched
    d2 = Path(td)
    pb.run_daily(d2, now=NOW, fetcher=FakeFetcher(trades=TRADES, opens=OPENS), llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    hwm_before = json.loads((d2 / pb.STATE_FILE).read_text())["hwm"]
    (d2 / "bot_last_known.json").unlink()        # P20.4: a bot that was never seen cannot be replaced by its last known state
    repp = pb.run_daily(d2, now=NOW + timedelta(days=1), fetcher=FakeFetcher(unreachable=["moonshot"], trades=TRADES, opens=OPENS),
                        llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    check("unreachable bot -> report is partial and names it", repp["partial"] and repp["missing_bots"] == ["moonshot"])
    check("partial data -> drawdown rules are NOT evaluated (no false ladder from a missing bot)",
          repp["drawdown"]["rungs_triggered"] == [] and repp["drawdown"]["drawdown_from_hwm_pct"] is None)
    check("partial data -> HWM untouched", json.loads((d2 / pb.STATE_FILE).read_text())["hwm"] == hwm_before)
    snap_last = [json.loads(l) for l in (d2 / pb.SNAPSHOTS_FILE).read_text().splitlines()][-1]
    check("partial snapshot is flagged so it is never used as a weekly/monthly reference", snap_last["partial"] is True)
    check("partial markdown warns the data is incomplete", "DATA INCOMPLETE" in (d2 / f"portfolio_brain_report_{repp['date']}.md").read_text())

# ---------------------------------------------------------------- P20.4: stale / unreachable / retired bots
with tempfile.TemporaryDirectory() as td:
    d3 = Path(td)
    full = pb.run_daily(d3, now=NOW, fetcher=FakeFetcher(trades=TRADES, opens=OPENS), llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    eq_full = full["capital"]["equity"]
    st1 = pb.run_daily(d3, now=NOW + timedelta(hours=20), fetcher=FakeFetcher(unreachable=["listing_fade"], trades=TRADES, opens=OPENS),
                       llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    check("STALE: a bot silent for one run keeps its last known equity -- the run is NOT partial, equity unchanged, the bot is named stale_bots",
          not st1["partial"] and st1["missing_bots"] == [] and st1["stale_bots"] == ["listing_fade"] and st1["capital"]["equity"] == eq_full)
    check("STALE: the budget and the drawdown are still evaluated (no false floor breach, no zeroed budget)",
          st1["capital"]["cppi_budget"] == full["capital"]["cppi_budget"] and st1["drawdown"]["drawdown_from_hwm_pct"] is not None)
    un = pb.run_daily(d3, now=NOW + timedelta(hours=60), fetcher=FakeFetcher(unreachable=["listing_fade"], trades=TRADES, opens=OPENS),
                      llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    check("UNREACHABLE: silent for more than 24h -> flagged unreachable (not stale), last equity still kept so it is not read as a drawdown",
          un["unreachable_bots"] == ["listing_fade"] and un["stale_bots"] == [] and not un["partial"] and un["capital"]["equity"] == eq_full)
    check("the roster note explains stale / unreachable / retired", "stale" in un["roster_note"] and "retired" in un["roster_note"])

with tempfile.TemporaryDirectory() as td:
    d4 = Path(td)
    pb.run_daily(d4, now=NOW, fetcher=FakeFetcher(trades=TRADES, opens=OPENS), llm=LLM_GOOD, env={}, http_get=lambda *a, **k: BTC_HTTP)
    base = json.loads((d4 / pb.LATEST_FILE).read_text())
    hwm0 = json.loads((d4 / pb.STATE_FILE).read_text())["hwm"]
    ENV = {"ALLOCATOR_RETIRED_BOTS": "moonshot"}
    ff = FakeFetcher(unreachable=["moonshot"], trades=TRADES, opens=OPENS)
    ret = pb.run_daily(d4, now=NOW + timedelta(days=3), fetcher=ff, llm=LLM_GOOD, env=ENV, http_get=lambda *a, **k: BTC_HTTP)
    check("RETIRED: not partial, not missing, no REST call at all to the retired bot",
          not ret["partial"] and ret["missing_bots"] == [] and not any(c[1] == "moonshot" for c in ff.calls))
    check("RETIRED: equity frozen at the last known balance (a constant) -- equity equals the day before", ret["capital"]["equity"] == base["capital"]["equity"]
          and ret["capital"]["retired_frozen_equity"] == {"moonshot": EQUITY["moonshot"]})
    check("RETIRED: reference and HWM stay comparable, the budget is not zeroed", ret["capital"]["reference_capital"] == base["capital"]["reference_capital"]
          and ret["capital"]["cppi_budget"] == base["capital"]["cppi_budget"] and ret["capital"]["hwm"] == round(hwm0, 2))
    check("RETIRED: no ladder / floor false alarm", ret["drawdown"]["rungs_triggered"] == [] and ret["drawdown"]["drawdown_from_hwm_pct"] is not None)
    check("RETIRED: its evidence entry stays in the report, marked retired", ret["evidence"]["moonshot"]["retired"] is True and "status" in ret["evidence"]["moonshot"])
    check("RETIRED: the freeze record names its source and method", ret["retired_bots"]["moonshot"]["method"] == "frozen_constant"
          and "snapshot" in ret["retired_bots"]["moonshot"]["source"])
    rec_file = json.loads((d4 / "retired_bots.json").read_text())
    check("RETIRED: freeze record persisted", rec_file["moonshot"]["frozen_equity"] == EQUITY["moonshot"])
    ov = pb.run_daily(d4, now=NOW + timedelta(days=4), fetcher=FakeFetcher(unreachable=["moonshot"], trades=TRADES, opens=OPENS), llm=LLM_GOOD,
                      env={**ENV, "ALLOCATOR_RETIRED_FROZEN": "moonshot:100.0"}, http_get=lambda *a, **k: BTC_HTTP)
    check("RETIRED: an operator override (final balance from Dip's aggregates) replaces the frozen value",
          ov["capital"]["retired_frozen_equity"] == {"moonshot": 100.0} and ov["capital"]["equity"] == round(base["capital"]["equity"] - EQUITY["moonshot"] + 100.0, 2))
with tempfile.TemporaryDirectory() as td:
    d5 = Path(td)         # a retired bot with NO snapshot to freeze from: dropped from the reference instead (fallback)
    ff = FakeFetcher(unreachable=["moonshot"], trades=TRADES, opens=OPENS)
    r5 = pb.run_daily(d5, now=NOW, fetcher=ff, llm=LLM_GOOD, env={"ALLOCATOR_RETIRED_BOTS": "moonshot"}, http_get=lambda *a, **k: BTC_HTTP)
    check("RETIRED fallback: no value to freeze -> contributes nothing and is dropped from the reference capital; the record says so",
          r5["retired_bots"]["moonshot"]["method"] == "dropped_from_reference" and r5["capital"]["reference_capital"] == sum(
              pb.DEFAULT_START_CAPITAL[b] for b in pb.PORTFOLIO_BOTS if b != "moonshot") and not r5["partial"])

with tempfile.TemporaryDirectory() as td:
    # mode off: nothing fetched, nothing written
    fake_off = FakeFetcher()
    r_off = pb.run_daily(Path(td), now=NOW, fetcher=fake_off, llm=LLM_GOOD, env={"PORTFOLIO_BRAIN_MODE": "off"})
    check("mode off: returns immediately, fetches nothing, writes nothing",
          r_off == {"mode": "off", "skipped": True} and not fake_off.calls and not list(Path(td).iterdir()))

# ============================================== shadow run sends only GETs
import requests  # noqa: E402

calls: list[tuple] = []


def fake_get(url, *a, **k):
    calls.append(("GET", url, k.get("auth")))

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self_):
            if "/ping" in url:
                return {"status": "pong"}
            if "/balance" in url:
                bot = next((b for b, u in urls.items() if url.startswith(u)), "main")
                return {"total": EQUITY.get(bot, 100.0)}
            if "/status" in url:
                return []
            if "/trades" in url:
                return {"trades": [], "total_trades": 0}
            if "kline" in url:
                return {"result": {"list": [["1", "100", "0", "0", "105"], ["0", "100", "0", "0", "100"]]}}
            return {}
    return R()


def forbidden(*a, **k):
    calls.append(("WRITE", a, k))
    raise AssertionError("shadow mode attempted a non-GET request")


from backend.engine import freqtrade_client  # noqa: E402

urls = {b: c["url"] for b, c in freqtrade_client.BOTS.items()}
orig = {n: getattr(requests, n) for n in ("get", "post", "put", "delete", "patch", "request")}
os.environ["FREQTRADE_API_USERNAME"], os.environ["FREQTRADE_API_PASSWORD"] = "user", "pass"
requests.get = fake_get
for n in ("post", "put", "delete", "patch", "request"):
    setattr(requests, n, forbidden)
try:
    with tempfile.TemporaryDirectory() as td:
        llm_calls: list = []
        pb.run_daily(Path(td), now=NOW, fetcher=pb.default_fetcher(), llm=lambda s, u: llm_calls.append(u) or None, env={})
finally:
    for n, f in orig.items():
        setattr(requests, n, f)
bot_calls = [c for c in calls if c[0] == "GET" and any(c[1].startswith(u) for u in urls.values())]
check("shadow run really talked to the bots", len(bot_calls) > 20)
check("shadow run made ZERO non-GET requests (post/put/delete/patch/request were booby-trapped)",
      not [c for c in calls if c[0] == "WRITE"])
check("bot requests use HTTP Basic auth (no login POST needed)", all(c[2] == ("user", "pass") for c in bot_calls if "/ping" not in c[1]))
check("no bot endpoint outside the GET set was requested",
      all(any(seg in c[1] for seg in ("/api/v1/ping", "/api/v1/balance", "/api/v1/status", "/api/v1/trades")) for c in bot_calls))
check("the analyst prompt received computed JSON (no raw price series)", llm_calls and "capital" in llm_calls[0] and "ohlcv" not in llm_calls[0].lower())
src = (REPO / "backend/engine/portfolio_brain.py").read_text(encoding="utf-8")
check("portfolio_brain.py contains no write-capable HTTP call to bots (no requests.post/put/delete/patch)",
      not any(tok in src for tok in ("requests.post", "requests.put", "requests.delete", "requests.patch", ".post(", ".put(", ".delete(")))
check("all 6 portfolio bots, no controls, are in PORTFOLIO_BOTS",
      len(pb.PORTFOLIO_BOTS) == 6 and not set(pb.PORTFOLIO_BOTS) & set(pb.CONTROL_BOTS) and "trend_v2" in pb.PORTFOLIO_BOTS)

# ============================================================= wiring
os.environ["DATA_DIR"] = tempfile.mkdtemp()
Path(os.environ["DATA_DIR"], "config.json").write_text(json.dumps(
    {"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
from backend import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app_module.app)
r0 = client.get("/api/portfolio/brain")
check("GET /api/portfolio/brain: 200 and available=false before any run", r0.status_code == 200 and r0.json()["available"] is False)
Path(os.environ["DATA_DIR"], "portfolio_brain_latest.json").write_text(json.dumps({"date": "2026-10-15", "mode": "shadow", "capital": {"equity": 1}}))
r1 = client.get("/api/portfolio/brain").json()
check("GET /api/portfolio/brain serves the latest report", r1["available"] is True and r1["date"] == "2026-10-15")
check("the endpoint is GET-only (no POST route registered)",
      not [r for r in app_module.app.routes if getattr(r, "path", "") == "/api/portfolio/brain" and "POST" in getattr(r, "methods", set())])
Path(os.environ["DATA_DIR"], "portfolio_brain_latest.json").write_text("{broken")
check("a corrupt report degrades to available=false, no 500", client.get("/api/portfolio/brain").json()["available"] is False)
check("the daily loop is registered at startup and scheduled for 00:10 UTC",
      "_portfolio_brain_loop" in (REPO / "backend/app.py").read_text(encoding="utf-8")
      and "seconds_until_next(datetime.now(timezone.utc), 0, 10)" in (REPO / "backend/app.py").read_text(encoding="utf-8"))
keys = {d["key"] for d in app_module.ENV_VAR_DEFS}
check("ENV_VAR_DEFS registers the five brain settings",
      {"PORTFOLIO_BRAIN_MODE", "PORTFOLIO_FLOOR_PCT", "CPPI_MULTIPLIER", "TARGET_MONTHLY_PCT", "TARGET_WEEKLY_PCT"} <= keys)
compose = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in compose["services"]["survival-agent"]["environment"]}
check("compose passes the brain settings to survival-agent with the documented defaults (P20.3: the allocator mode defaults to enforce)",
      env["PORTFOLIO_BRAIN_MODE"].endswith(":-enforce}") and env["PORTFOLIO_FLOOR_PCT"].endswith(":-0.90}")
      and env["CPPI_MULTIPLIER"].endswith(":-3}") and env["TARGET_MONTHLY_PCT"].endswith(":-1.0}") and env["TARGET_WEEKLY_PCT"].endswith(":-0.25}"))

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P17-A PORTFOLIO BRAIN CHECKS PASSED")
