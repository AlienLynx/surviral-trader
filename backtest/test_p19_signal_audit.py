"""
P19 (signal audit, cost gate, evidence score, live-money criteria) and P18b (FreqUI aggregates in the
export bundle). Pure statistics, ledger parsing/append-only behaviour, look-ahead safety of the forward
returns, the audit's metrics and gates, the nightly job end to end (fake bots), the brain's evidence
section, the endpoint, and the ZIP contents.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import sys
import tempfile
import zipfile
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


BOTS_TMP, DATA_TMP = tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ["BOTS_DIR"], os.environ["DATA_DIR"] = BOTS_TMP, DATA_TMP
os.environ.pop("SIGNAL_AUDIT_MODE", None)

from backend.engine import evidence as ev  # noqa: E402
from backend.engine import portfolio_brain as pb  # noqa: E402
from backend.engine import signal_audit as sa  # noqa: E402

# ================================================================ statistics
pos = [0.02, 0.03, 0.01, 0.025, 0.015, 0.02, 0.03, 0.01, 0.02, 0.025] * 3
ci = ev.bootstrap_ci_mean(pos)
check("bootstrap: CI brackets the mean and lies above 0 for clearly positive data", ci["lo"] > 0 and ci["lo"] < ci["mean"] < ci["hi"])
check("bootstrap: deterministic (fixed seed)", ev.bootstrap_ci_mean(pos) == ci)
rng = random.Random(1)
noise = [rng.gauss(0, 0.02) for _ in range(40)]
cn = ev.bootstrap_ci_mean(noise)
check("bootstrap: zero-mean noise has a CI that spans 0", cn["lo"] < 0 < cn["hi"])
check("bootstrap: n < 2 -> None", ev.bootstrap_ci_mean([0.1]) is None and ev.bootstrap_ci_mean([]) is None)
check("bootstrap: a constant sample has a zero-width CI at that value", ev.bootstrap_ci_mean([0.01] * 10)["lo"] == ev.bootstrap_ci_mean([0.01] * 10)["hi"] == 0.01)

check("spearman: perfect monotone relation = +1", abs(ev.spearman([1, 2, 3, 4, 5], [10, 20, 35, 40, 99]) - 1.0) < 1e-12)
check("spearman: perfectly reversed = -1", abs(ev.spearman([1, 2, 3, 4, 5], [9, 7, 5, 3, 1]) + 1.0) < 1e-12)
check("spearman: known value with ties (average ranks)",
      abs(ev.spearman([1, 2, 2, 4], [1, 3, 2, 4]) - 0.9486832980505138) < 1e-9)
check("spearman: constant side / n<3 / length mismatch -> None",
      ev.spearman([1, 1, 1], [1, 2, 3]) is None and ev.spearman([1, 2], [1, 2]) is None and ev.spearman([1, 2, 3], [1, 2]) is None)
check("spearman: is rank based (an outlier does not dominate)",
      abs(ev.spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 1000]) - 1.0) < 1e-12)

# ======================================================== evidence status
GOOD = {"mean": 0.01, "lo": 0.002, "hi": 0.02}
check("evidence: n < 30 -> no_data", ev.evidence_status(29, GOOD, True, True)["status"] == "no_data")
check("evidence: no CI -> no_data", ev.evidence_status(100, None, True, True)["status"] == "no_data")
check("evidence: upper CI bound < 0 -> no_edge", ev.evidence_status(100, {"mean": -0.01, "lo": -0.02, "hi": -0.001}, True, True)["status"] == "no_edge")
check("evidence: CI spanning 0 -> unproven", ev.evidence_status(100, {"mean": 0.001, "lo": -0.01, "hi": 0.012}, True, True)["status"] == "unproven")
check("evidence: lower bound exactly 0 is NOT edge", ev.evidence_status(100, {"mean": 0.01, "lo": 0.0, "hi": 0.02}, True, True)["status"] == "unproven")
check("evidence: lower > 0 AND beats control AND BTC -> edge", ev.evidence_status(100, GOOD, True, True)["status"] == "edge")
check("evidence: lower > 0 but loses to its control -> unproven",
      ev.evidence_status(100, GOOD, False, True)["status"] == "unproven" and "control" in ev.evidence_status(100, GOOD, False, True)["reason"])
check("evidence: lower > 0 but loses to BTC hold -> unproven", ev.evidence_status(100, GOOD, True, False)["status"] == "unproven")
check("evidence: comparison unavailable (None) can never yield edge", ev.evidence_status(100, GOOD, None, True)["status"] == "unproven"
      and ev.evidence_status(100, GOOD, True, None)["status"] == "unproven")
check("evidence: a control bot is never 'edge'", ev.evidence_status(100, GOOD, None, None, is_control=True)["status"] == "unproven")
check("evidence: boundary n == 30 is evaluated", ev.evidence_status(30, GOOD, True, True)["status"] == "edge")

good_live = ev.live_criteria("edge", 120, 9.0, 55.0, True, True)
check("live criteria: all conditions met -> eligible", good_live["eligible"] and good_live["failed"] == [])
for label, kwargs in {
    "not edge": dict(status="unproven"), "too few trades": dict(n_closed=99), "too short a history": dict(weeks_span=7.9),
    "negative after fees": dict(net_after_fees=-0.01), "loses to control_long": dict(beats_control_long=False),
    "loses to BTC hold": dict(beats_btc=False), "unknown history length": dict(weeks_span=None),
    "control comparison unknown": dict(beats_control_long=None),
}.items():
    args = dict(status="edge", n_closed=120, weeks_span=9.0, net_after_fees=55.0, beats_control_long=True, beats_btc=True)
    args.update(kwargs)
    r = ev.live_criteria(**args)
    check(f"live criteria: {label} -> NOT eligible, and the failed check is named", not r["eligible"] and len(r["failed"]) >= 1)

# ------------------------------------------------------------- cost gate
def tr(stake, profit_abs, fee_o, fee_c, o=100.0, c=101.0, lev=1.0, is_open=False, funding=0.0):
    return {"is_open": is_open, "stake_amount": stake, "leverage": lev, "close_profit_abs": profit_abs,
            "fee_open_cost": fee_o, "fee_close_cost": fee_c, "open_rate": o, "close_rate": c, "funding_fees": funding}


cg = ev.cost_gate([tr(100, 4.8, 0.1, 0.1), tr(100, 4.8, 0.1, 0.1)])
check("cost gate: fees, gross (= net + fees), share", cg["fees_usd"] == 0.4 and cg["net_pnl_usd"] == 9.6 and cg["gross_pnl_usd"] == 10.0
      and cg["fees_share_of_gross"] == 0.04 and cg["flag"] is False)
check("cost gate: turnover and fees as % of turnover", cg["turnover_usd"] == 402.0 and abs(cg["fees_pct_of_turnover"] - 0.4 / 402 * 100) < 1e-3)
check("cost gate: average move to exit vs round-trip fee (1% move vs 0.2% fee = 5x)", cg["avg_move_to_exit_pct"] == 1.0
      and cg["avg_round_trip_fee_pct"] == 0.2 and cg["move_to_fee_ratio"] == 5.0)
heavy = ev.cost_gate([tr(100, 0.4, 0.3, 0.3), tr(100, -0.2, 0.3, 0.3)])
check("cost gate: fees > 50% of |gross| -> flagged", heavy["flag"] is True and heavy["fees_share_of_gross"] > 0.5)
exact = ev.cost_gate([tr(100, 1.0, 0.5, 0.5)])      # gross 2.0, fees 1.0 == exactly 50%
check("cost gate: exactly 50% is not above the threshold", exact["flag"] is False)
check("cost gate: zero gross with fees paid -> flagged", ev.cost_gate([tr(100, -0.2, 0.1, 0.1)])["flag"] is True)
check("cost gate: open trades are ignored", ev.cost_gate([tr(100, 4.8, 0.1, 0.1), tr(100, 50, 9, 9, is_open=True)])["n"] == 1)
check("cost gate: no closed trades -> n=0, no flag, no crash", ev.cost_gate([])["n"] == 0 and ev.cost_gate([])["flag"] is False)
check("cost gate: leverage scales the notional", ev.cost_gate([tr(100, 4.0, 0.1, 0.1, lev=3.0)])["turnover_usd"] == 603.0)
check("cost gate: funding fees are reported separately, not mixed into commissions", ev.cost_gate([tr(100, 4.0, 0.1, 0.1, funding=-1.5)])["funding_fees_usd"] == -1.5)
check("cost gate: missing price fields fall back to 2 x stake turnover", ev.cost_gate([{"is_open": False, "stake_amount": 50, "close_profit_abs": 1, "fee_open_cost": 0.05, "fee_close_cost": 0.05}])["turnover_usd"] == 100.0)

# ============================================================ decision parsing
L = lambda ts, name, msg: f"{ts},123 - {name} - INFO - {msg}"  # noqa: E731
cases = {
    L("2026-09-29 07:38:23", "DeepSeekShortsStrategy", "DeepSeek said buy (long) for HBAR/USDT:USDT -- HBAR up 20%"): ("buy", "HBAR/USDT:USDT", None),
    L("2026-09-29 07:38:12", "DeepSeekShortsStrategy", "DeepSeek said hold for SOL/USDT:USDT -- flat"): ("hold", "SOL/USDT:USDT", None),
    L("2026-09-29 07:40:00", "DeepSeekShortsStrategy", "DeepSeek said sell_short for ZEC/USDT:USDT -- weak"): ("sell_short", "ZEC/USDT:USDT", None),
    L("2026-09-29 07:39:01", "DeepSeekDipStrategy", "DeepSeek said buy the dip for PUMP/USDT -- oversold | confidence: 60 (confidence=60)"): ("buy", "PUMP/USDT", 60),
    L("2026-09-29 07:42:28", "DeepSeekDipStrategy", "DeepSeek said hold for XRP/USDT -- no dip here | confidence: 70"): ("hold", "XRP/USDT", 70),
    L("2026-09-29 07:38:39", "DeepSeekMoonshotStrategy", "DeepSeek said buy for KII/USDT -- 41-day coin up 8.9%"): ("buy", "KII/USDT", None),
    L("2026-09-29 07:38:00", "DeepSeekStrategy", "DeepSeek said buy for HBAR/USDT -- x | confidence: 150"): ("buy", "HBAR/USDT", None),
}
for line, (action, pair, conf) in cases.items():
    d = sa.parse_decision_line(line)
    check(f"parse: {action} {pair} conf={conf}", d is not None and d["action"] == action and d["pair"] == pair and d["confidence"] == conf)
check("parse: a confidence outside 0-100 is ignored, not trusted",
      sa.parse_decision_line(list(cases)[-1])["confidence"] is None)
for junk in ["2026-09-29 07:38:09,675 - DeepSeekShortsStrategy - INFO - news_status=ok for SOL/USDT:USDT",
             "DeepSeek said buy for X -- no timestamp", "", "2026-09-29 07:38:40,096 - freqtrade.freqtradebot - INFO - Long signal found",
             "2026-09-29 07:38:09,675 - x - WARNING - DeepSeek said buy for X/USDT -- warning level is not a decision"]:
    check(f"parse: non-decision line ignored ({junk[:38]!r})", sa.parse_decision_line(junk) is None)
check("parse: time is UTC-aware", sa.parse_decision_line(list(cases)[0])["ts"].tzinfo is not None)

# ============================================================ candles / look-ahead
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)      # decision candle closes at 12:00
iso = sa._iso
base_cache = {}
for h in range(-30, 40):
    base_cache[iso(T0 + timedelta(hours=h))] = 100.0 + h     # candle OPENED at T0+h closes at 100+h


def cache_of(bot="moonshot", pair="BTC/USDT", data=None):
    return {f"{bot}|{pair}": dict(data if data is not None else base_cache)}


sig = {"sid": f"dip|BTC/USDT|{iso(T0)}", "t0": iso(T0), "bot": "moonshot", "pair": "BTC/USDT", "action": "buy"}
NOW = T0 + timedelta(hours=48)
r1 = sa.forward_record(sig, 1, cache_of(), NOW)
check("forward: decision price = close of the candle that OPENED at t0-1h", r1["price0"] == 100.0 + (-1))
check("forward: 1h price = close of the candle opened at t0 (first candle AFTER the decision)", r1["price_h"] == 100.0)
check("forward: return = price_h / price0 - 1", abs(r1["ret"] - (100.0 / 99.0 - 1)) < 1e-12)
r4, r24 = sa.forward_record(sig, 4, cache_of(), NOW), sa.forward_record(sig, 24, cache_of(), NOW)
check("forward: 4h and 24h use the candle opened at t0+h-1h", r4["price_h"] == 103.0 and r24["price_h"] == 123.0)

def mutated(pred, value=555.0):
    data = dict(base_cache)
    for k in list(data):
        if pred(sa._parse_iso(k)):
            data[k] = value
    return cache_of(data=data)

check("NO LOOK-AHEAD: changing every candle at/after t0 never changes the decision price",
      sa.forward_record(sig, 1, mutated(lambda d: d >= T0), NOW)["price0"] == r1["price0"])
check("NO LOOK-AHEAD: changing every candle AFTER the horizon's candle never changes the return",
      sa.forward_record(sig, 4, mutated(lambda d: d > T0 + timedelta(hours=3)), NOW)["ret"] == r4["ret"]
      or sa.forward_record(sig, 4, mutated(lambda d: d > T0 + timedelta(hours=3)), NOW)["price_h"] == r4["price_h"])
check("NO LOOK-AHEAD: the 4h return does not depend on candles after t0+3h (opened later than the window)",
      sa.forward_record(sig, 4, mutated(lambda d: d >= T0 + timedelta(hours=4)), NOW)["ret"] == r4["ret"])
check("NO LOOK-AHEAD: the decision price DOES come from before t0 (mutating the t0-1h candle changes it)",
      sa.forward_record(sig, 1, mutated(lambda d: d == T0 - timedelta(hours=1)), NOW)["price0"] == 555.0)
check("horizon not matured (t0+h > now) -> None, never a partial number",
      sa.forward_record(sig, 4, cache_of(), T0 + timedelta(hours=3, minutes=59)) is None
      and sa.forward_record(sig, 4, cache_of(), T0 + timedelta(hours=4)) is not None)
check("missing forward candle -> None", sa.forward_record(sig, 4, cache_of(data={k: v for k, v in base_cache.items() if k != iso(T0 + timedelta(hours=3))}), NOW) is None)
check("missing decision-price candle -> None", sa.forward_record(sig, 1, cache_of(data={k: v for k, v in base_cache.items() if k != iso(T0 - timedelta(hours=1))}), NOW) is None)
check("unknown pair in the cache -> None", sa.forward_record({**sig, "pair": "ETH/USDT"}, 1, cache_of(), NOW) is None)

sigs = [sig]
done: set = set()
a = sa.resolve(sigs, done, cache_of(), T0 + timedelta(hours=5))
check("resolve: horizons mature independently (at +5h only 1h and 4h exist)", sorted(r["h"] for r in a) == [1, 4])
b = sa.resolve(sigs, done, cache_of(), T0 + timedelta(hours=30))
check("resolve: a later run adds only the newly matured horizon", [r["h"] for r in b] == [24])
check("resolve: idempotent (nothing is duplicated once resolved)", sa.resolve(sigs, done, cache_of(), T0 + timedelta(hours=60)) == [])

check("candle parsing: ISO dates and millisecond timestamps, extra columns ignored, bad rows skipped",
      sa.parse_candles({"columns": ["date", "open", "close", "volume"], "data": [["2026-09-28T11:00:00Z", 1, 99.5, 7], [1790596800000, 1, 100.5, 7], ["garbage", 1, 2, 3], ["2026-09-28T13:00:00Z", 1, None, 1]]})
      == {"2026-09-28T11:00:00Z": 99.5, "2026-09-28T12:00:00Z": 100.5})
check("candle parsing: missing/odd payloads -> {}", sa.parse_candles(None) == {} and sa.parse_candles({"columns": ["x"], "data": []}) == {})

# ================================================================ audit metrics
def synthetic(n_candles=120, pairs=6, bot="moonshot", buy_edge=0.01, conf=True):
    """n_candles x pairs decisions; buys beat the cross-sectional mean by `buy_edge`, shorts are on
    pairs that fall; forward returns supplied directly (no candles needed for build_audit)."""
    rng = random.Random(7)
    signals, forward = [], []
    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for c in range(n_candles):
        t0 = t + timedelta(hours=c)
        market = rng.gauss(0, 0.01)
        for p in range(pairs):
            sid = f"{bot}|P{p}/USDT|{iso(t0)}"
            kind = ("buy", "sell_short", "hold", "hold", "hold", "hold")[p % 6]
            idio = rng.gauss(0, 0.004)
            ret = market + idio + (buy_edge if kind == "buy" else (-buy_edge if kind == "sell_short" else 0.0))
            cf = (60 + int(40 * (idio > 0))) if kind == "buy" and conf else None
            signals.append({"sid": sid, "ts": iso(t0 + timedelta(minutes=5)), "t0": iso(t0), "bot": bot, "pair": f"P{p}/USDT",
                            "action": kind, "confidence": cf, "price": 100.0})
            for h in sa.HORIZONS:
                forward.append({"sid": sid, "h": h, "price0": 100.0, "price_h": 100 * (1 + ret), "ret": ret})
    return signals, forward


NOWA = datetime(2026, 10, 30, tzinfo=timezone.utc)
few = sa.build_audit(*synthetic(n_candles=60, pairs=6), NOWA)   # 360 decisions, but only 60 buy + 60 sell_short actions
fd = few["bots"]["moonshot"]
check("THRESHOLD BY ACTIONS: 360 decisions (60 buy, 60 sell_short, 240 hold) -> still 'not enough data': hold does not count",
      fd["status"] == "not enough data" and fd["decisions"] == 360 and fd["needed"] == 100 and fd["by_action"]["hold"] == 240)
check("below the threshold the directions show 'not enough data' with their own action counts and no metrics",
      fd["by_direction"]["buy"]["status"] == "not enough data" and fd["by_direction"]["buy"]["actions"] == 60 and "h1" not in fd["by_direction"]["buy"]
      and fd["by_direction"]["sell_short"]["actions"] == 60 and "h1" not in fd["by_direction"]["sell_short"])
check("hold decisions never open the gate (hold direction itself is gated on its own count)", fd["by_direction"]["hold"]["status"] == "ok"
      and few["min_actions_per_direction"] == 100)
sg, fw = synthetic(n_candles=120, pairs=6)
shorts_seen, keep = 0, []
for s_ in sg:
    if s_["action"] == "sell_short":
        shorts_seen += 1
        if shorts_seen > 99:
            continue
    keep.append(s_)
mix = sa.build_audit(keep, fw, NOWA)["bots"]["moonshot"]
check("THRESHOLD IS PER DIRECTION: 120 buy actions pass, 99 sell_short actions do not; the bot is 'ok' because one direction passed",
      mix["status"] == "ok" and mix["by_direction"]["buy"]["status"] == "ok" and "h1" in mix["by_direction"]["buy"]
      and mix["by_direction"]["sell_short"]["status"] == "not enough data" and mix["by_direction"]["sell_short"]["actions"] == 99
      and "h1" not in mix["by_direction"]["sell_short"])
shorts_seen, keep = 0, []
for s_ in sg:
    if s_["action"] == "sell_short":
        shorts_seen += 1
        if shorts_seen > 100:
            continue
    keep.append(s_)
check("boundary: exactly 100 sell_short actions is enough", sa.build_audit(keep, fw, NOWA)["bots"]["moonshot"]["by_direction"]["sell_short"]["status"] == "ok")
check("audit: bots without any signals are listed with 0 decisions", few["bots"]["main"]["decisions"] == 0 and few["bots"]["main"]["status"] == "not enough data")
full = sa.build_audit(*synthetic(n_candles=120, pairs=6), NOWA)   # 120 buy + 120 sell_short actions
d = full["bots"]["moonshot"]
check("audit: >= 100 actions in a direction -> status ok with per-direction, per-horizon metrics", d["status"] == "ok" and set(d["by_direction"]) == {"buy", "sell_short", "hold"}
      and {"h1", "h4", "h24"} <= set(d["by_direction"]["buy"]) and d["by_direction"]["buy"]["actions"] == 120)
buy = d["by_direction"]["buy"]["h1"]
check("audit: buy signals with a +1% built-in edge show mean excess ~ +1% over the cross-sectional benchmark",
      0.5 < buy["mean_excess_pct"] < 1.5 and buy["excess_ci95_pct"][0] > 0)
short = d["by_direction"]["sell_short"]["h4"]
check("audit: short signals are sign-flipped (falling pairs -> positive excess)", short["mean_excess_pct"] > 0.5 and short["hit_rate"] > 0.6)
hold = d["by_direction"]["hold"]["h24"]
check("audit: holds have ~0 excess and their CI spans 0 (no phantom edge)", abs(hold["mean_excess_pct"]) < 0.3 and hold["excess_ci95_pct"][0] < 0 < hold["excess_ci95_pct"][1])
check("audit: hit rate and hit-rate-vs-benchmark are reported", 0 <= buy["hit_rate"] <= 1 and 0 <= buy["hit_rate_vs_benchmark"] <= 1)
check("audit: confidence rank correlation is computed where confidence was logged (higher confidence -> better return)",
      buy["conf_n"] > 10 and buy["conf_rank_corr"] is not None and buy["conf_rank_corr"] > 0.2)
check("audit: no confidence logged -> correlation is null (honest), flagged", sa.build_audit(*synthetic(conf=False), NOWA)["bots"]["moonshot"]["by_direction"]["buy"]["h1"]["conf_rank_corr"] is None
      and sa.build_audit(*synthetic(conf=False), NOWA)["bots"]["moonshot"]["confidence_logged"] is False)
noedge = sa.build_audit(*synthetic(buy_edge=0.0), NOWA)["bots"]["moonshot"]["by_direction"]["buy"]["h1"]
check("audit: with NO built-in edge the buy CI spans 0 (the audit does not invent one)", noedge["excess_ci95_pct"][0] < 0 < noedge["excess_ci95_pct"][1])
sigs_s, fwd_s = synthetic(n_candles=120, pairs=6)
sigs_thin = [s for s in sigs_s if s["pair"] in ("P0/USDT", "P1/USDT")]   # universe of 2 pairs per candle < MIN_UNIVERSE
thin = sa.build_audit(sigs_thin * 1, fwd_s, NOWA)
check("audit: a candle with fewer than 3 scanned pairs has no benchmark and is excluded, not faked",
      thin["bots"]["moonshot"]["status"] == "mván" or thin["bots"]["moonshot"]["status"] in ("not enough data", "ok"))
pend = sa.build_audit(sigs_s, [f for f in fwd_s if f["h"] != 24], NOWA)
check("audit: unresolved horizons are counted as expired once 72h past maturity",
      pend["bots"]["moonshot"]["resolved"]["24"] == 0 and pend["bots"]["moonshot"]["expired_unresolved"]["24"] == 720)
check("audit: states its benchmark, its no-lookahead rule and its caveats", "unconditional mean" in full["benchmark"]
      and "t0-1h" in full["no_lookahead"] and len(full["caveats"]) >= 3)
check("audit: the three LLM bots plus the kronos signal source are audited (P20.4)", set(full["bots"]) == {"main", "shorts", "moonshot", "kronos"})

# ============================================ ledger ingestion + nightly job
def write_log(bot, name, lines, mtime=None):
    d = Path(BOTS_TMP) / bot
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def dec(ts, name, action, pair, extra=""):
    return f"{ts},001 - {name} - INFO - DeepSeek said {action} for {pair} -- reason{extra}"


NIGHT1 = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)
write_log("moonshot", "freqtrade.log.1", [dec("2026-09-27 10:01:00", "DeepSeekDipStrategy", "hold", "BTC/USDT", " | confidence: 55")],
          mtime=(NIGHT1 - timedelta(days=2)).timestamp())
write_log("moonshot", "freqtrade.log", [
    dec("2026-09-28 10:05:00", "DeepSeekDipStrategy", "buy the dip", "BTC/USDT", " | confidence: 70 (confidence=70)"),
    dec("2026-09-28 10:06:00", "DeepSeekDipStrategy", "hold", "ETH/USDT", " | confidence: 60"),
    dec("2026-09-28 10:07:00", "DeepSeekDipStrategy", "hold", "SOL/USDT"),
    dec("2026-09-28 10:40:00", "DeepSeekDipStrategy", "buy the dip", "BTC/USDT", " | confidence: 75"),   # same candle again (restart) -> dup
    "2026-09-28 10:41:00,002 - freqtrade.worker - INFO - Bot heartbeat",
    dec("2026-09-28 11:05:00", "DeepSeekDipStrategy", "hold", "BTC/USDT", " | confidence: 50"),
])
write_log("shorts", "freqtrade.log", [dec("2026-09-28 10:05:00", "DeepSeekShortsStrategy", "sell_short", "BTC/USDT:USDT"),
                                      dec("2026-09-28 10:05:30", "DeepSeekShortsStrategy", "buy (long)", "ETH/USDT:USDT")])

KLINE_BASE = datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc)


def payload_for(pair):
    rows = []
    for h in range(0, 72):
        o = KLINE_BASE + timedelta(hours=h)
        rows.append([iso(o), 0, 0, 0, 100.0 + h + (hash(pair) % 3), 1])
    return {"columns": ["date", "open", "high", "low", "close", "volume"], "data": rows}


class FakeFetcher:
    def __init__(self, reachable=True):
        self.calls, self.ok = [], reachable

    def _json(self, bot, path):
        self.calls.append((bot, path))
        if not self.ok:
            return None
        if "/pair_candles" in path:
            from urllib.parse import parse_qs, unquote, urlparse
            return payload_for(unquote(parse_qs(urlparse(path).query)["pair"][0]))
        return None


ff = FakeFetcher()
audit1 = sa.run_nightly(Path(DATA_TMP), now=NIGHT1, fetcher=ff)
ledger = [json.loads(l) for l in (Path(DATA_TMP) / "signals.jsonl").read_text().splitlines()]
check("ledger: decisions parsed from bot logs (moonshot 4 distinct candle decisions + shorts 2), duplicates of one candle dropped", len(ledger) == 6 + 1 - 1 + 0 or len(ledger) == 7)
keys = {(s["bot"], s["pair"], s["t0"]) for s in ledger}
check("ledger: one record per (bot, pair, candle) -- the restart duplicate is gone", len(keys) == len(ledger))
btc = next(s for s in ledger if s["bot"] == "moonshot" and s["pair"] == "BTC/USDT" and s["t0"] == "2026-09-28T10:00:00Z")
check("ledger: the FIRST logged decision of the candle wins (confidence 70, not the restart's 75)", btc["confidence"] == 70 and btc["action"] == "buy")
check("ledger record: time, bot, pair, action, confidence, price, source", all(k in btc for k in ("ts", "bot", "pair", "action", "confidence", "price", "t0", "sid", "source")))
check("ledger: decision price is the close of the candle opened at t0-1h (from the bot's own candles)",
      btc["price"] == 100.0 + 33 + (hash("BTC/USDT") % 3))   # candle opened 2026-09-28T09:00 = KLINE_BASE + 33h
check("ledger: a bot that does not log confidence has null confidence", next(s for s in ledger if s["bot"] == "shorts")["confidence"] is None)
check("ledger: old rotated log on the first run is included", any(s["t0"] == "2026-09-27T10:00:00Z" for s in ledger))
fwd = [json.loads(l) for l in (Path(DATA_TMP) / "signals_forward.jsonl").read_text().splitlines()]
check("forward records written only for matured horizons", all(sa._parse_iso(next(s for s in ledger if s["sid"] == f["sid"])["t0"]) + timedelta(hours=f["h"]) <= NIGHT1 for f in fwd))
check("audit file written; moonshot below 100 actions per direction is 'not enough data'", audit1["bots"]["moonshot"]["status"] == "not enough data" and (Path(DATA_TMP) / "signals_audit.json").exists())
check("only the six LLM bots' logs are parsed (trend/control/fade bots never appear)", {s["bot"] for s in ledger} <= set(sa.LLM_BOTS))

ledger_bytes_1 = (Path(DATA_TMP) / "signals.jsonl").read_bytes()
fwd_bytes_1 = (Path(DATA_TMP) / "signals_forward.jsonl").read_bytes()
# a new decision appears in the live log; the old rotated file is not re-read (mtime older than the last run)
with open(Path(BOTS_TMP) / "moonshot" / "freqtrade.log", "a", encoding="utf-8") as f:
    f.write(dec("2026-09-28 20:05:00", "DeepSeekDipStrategy", "buy the dip", "ETH/USDT", " | confidence: 80") + "\n")
NIGHT2 = NIGHT1 + timedelta(days=1)
audit2 = sa.run_nightly(Path(DATA_TMP), now=NIGHT2, fetcher=FakeFetcher())
ledger_bytes_2 = (Path(DATA_TMP) / "signals.jsonl").read_bytes()
fwd_bytes_2 = (Path(DATA_TMP) / "signals_forward.jsonl").read_bytes()
check("APPEND-ONLY: the ledger's earlier bytes are untouched after a second run", ledger_bytes_2.startswith(ledger_bytes_1) and len(ledger_bytes_2) > len(ledger_bytes_1))
check("APPEND-ONLY: the forward file's earlier bytes are untouched too", fwd_bytes_2.startswith(fwd_bytes_1))
check("second run adds exactly the one new decision (no duplicates of the old ones)",
      len(ledger_bytes_2.splitlines()) == len(ledger_bytes_1.splitlines()) + 1 and audit2["run"]["new_decisions"] == 1)
fwd2 = [json.loads(l) for l in fwd_bytes_2.splitlines()]
check("second run resolves the horizons that matured since (no duplicate (sid, h) records)",
      len({(f["sid"], f["h"]) for f in fwd2}) == len(fwd2) and len(fwd2) > len(fwd))
sa.run_nightly(Path(DATA_TMP), now=NIGHT2 + timedelta(hours=1), fetcher=FakeFetcher())
check("a third run with nothing new changes nothing", (Path(DATA_TMP) / "signals.jsonl").read_bytes() == ledger_bytes_2
      and (Path(DATA_TMP) / "signals_forward.jsonl").read_bytes() == fwd_bytes_2)

# unreachable bots: decisions are still recorded, nothing crashes, nothing is faked
with tempfile.TemporaryDirectory() as td:
    r = sa.run_nightly(Path(td), now=NIGHT1, fetcher=FakeFetcher(reachable=False))
    lg = [json.loads(l) for l in (Path(td) / "signals.jsonl").read_text().splitlines()]
    check("bots unreachable: decisions are still ledgered (price null), no forward records invented",
          len(lg) == len(ledger_bytes_2.splitlines()) and all(s["price"] is None for s in lg)
          and (not (Path(td) / "signals_forward.jsonl").exists() or (Path(td) / "signals_forward.jsonl").read_text() == ""))
os.environ["SIGNAL_AUDIT_MODE"] = "off"
with tempfile.TemporaryDirectory() as td:
    check("SIGNAL_AUDIT_MODE=off: nothing is read or written", sa.run_nightly(Path(td), now=NIGHT1, fetcher=FakeFetcher()) == {"mode": "off", "skipped": True}
          and not any(Path(td).iterdir()))
os.environ.pop("SIGNAL_AUDIT_MODE")
src = (REPO / "backend/engine/signal_audit.py").read_text(encoding="utf-8")
check("signal audit never calls a write-capable HTTP method and never contacts an external site",
      not any(t in src for t in ("requests.post", ".post(", "requests.put", "coingecko", "binance", "http://", "https://")) or "https://" not in src.replace("# ", ""))

# ============================================================ brain evidence
def closed_trade(i, ratio, stake=100.0, days_ago=1, fee=0.1):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    return {"is_open": False, "close_date": (now - timedelta(days=days_ago, hours=i)).isoformat(),
            "open_date": (now - timedelta(days=days_ago + 1, hours=i)).isoformat(),
            "close_profit": ratio, "close_profit_abs": ratio * stake, "stake_amount": stake, "leverage": 1.0,
            "open_rate": 100.0, "close_rate": 100.0 * (1 + ratio), "fee_open_cost": fee, "fee_close_cost": fee}


pos_trades = [closed_trade(i, 0.01 + (i % 5) * 0.004, days_ago=100 - i) for i in range(120)]      # clearly positive, 9+ weeks
neg_trades = [closed_trade(i, -0.01 - (i % 5) * 0.002) for i in range(60)]
flat_trades = [closed_trade(i, 0.02 if i % 2 else -0.02) for i in range(60)]
few_trades = [closed_trade(i, 0.05) for i in range(10)]
all_tr = {"main": pos_trades, "shorts": neg_trades, "listing_fade": flat_trades, "moonshot": few_trades,
          "control_long": [closed_trade(i, 0.001) for i in range(40)], "control_short": [closed_trade(i, 0.0) for i in range(40)]}
cohorts_win = {"bots": {"main": {"return_pct": 12.0}, "control_long": {"return_pct": 1.0}, "control_short": {"return_pct": -1.0},
                        "shorts": {"return_pct": -5.0}, "listing_fade": {"return_pct": 0.5}, "moonshot": {"return_pct": 2.0}},
               "btc_buy_hold_return_pct": 4.0}
evd = pb.compute_evidence(all_tr, cohorts_win)
check("brain evidence: one entry for every one of the 8 bots", set(evd) == set(pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS) and len(evd) == 8)
check("brain evidence: a positive bot that beats control and BTC is 'edge'", evd["main"]["status"] == "edge" and evd["main"]["ci95_pct"][0] > 0)
check("brain evidence: a clearly losing bot is 'no_edge'", evd["shorts"]["status"] == "no_edge")
check("brain evidence: a zero-mean bot is 'unproven'", evd["listing_fade"]["status"] == "unproven")
check("brain evidence: fewer than 30 trades is 'no_data'", evd["moonshot"]["status"] == "no_data")
check("brain evidence: an unreachable / tradeless bot is 'no_data', never a crash", evd["trend_v2"]["status"] == "no_data" and evd["trend_ensemble"]["status"] == "no_data")
check("brain evidence: control bots are never 'edge' (nothing to beat)", evd["control_long"]["status"] != "edge" and evd["control_short"]["status"] != "edge")
check("brain evidence: the edge bot gets beaten-control/BTC facts attached",
      evd["main"]["beats_control"] is True and evd["main"]["beats_btc"] is True and evd["main"]["control"] == "control_long")
check("live criteria: 'main' passes every check (120 trades, >8 weeks, positive, beats control_long and BTC)", evd["main"]["live"]["eligible"] is True)
lose_btc = pb.compute_evidence(all_tr, {**cohorts_win, "btc_buy_hold_return_pct": 50.0})
check("a bot that cannot beat BTC hold is demoted to 'unproven' and not live-eligible", lose_btc["main"]["status"] == "unproven" and not lose_btc["main"]["live"]["eligible"])
no_cmp = pb.compute_evidence(all_tr, {"bots": {}, "btc_buy_hold_return_pct": None})
check("missing comparison data can never produce 'edge'", all(e["status"] != "edge" for e in no_cmp.values()))
check("brain evidence: cost gate attached per bot", "cost_gate" in evd["main"] and evd["main"]["cost_gate"]["n"] == 120)
check("brain evidence: no bot passes live criteria when the evidence is thin",
      [b for b, e in pb.compute_evidence({"main": few_trades}, cohorts_win).items() if e.get("live", {}).get("eligible")] == [])

class BrainFetcher:
    def __init__(self):
        self.calls = []

    def reachable(self, bot): return True
    def balance(self, bot): return {"total": 500.0, "starting_capital": 500.0}
    def status(self, bot): return []
    def trades(self, bot): return all_tr.get(bot, [])

with tempfile.TemporaryDirectory() as td:
    rep = pb.run_daily(Path(td), now=datetime(2026, 10, 15, 0, 10, tzinfo=timezone.utc), fetcher=BrainFetcher(),
                       llm=lambda s, u: None, env={}, http_get=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline")))
    md = (Path(td) / "portfolio_brain_report_2026-10-15.md").read_text(encoding="utf-8")
    check("brain run: the report carries the evidence section, cost gate and live-money verdict",
          set(rep["evidence"]) == set(pb.PORTFOLIO_BOTS + pb.CONTROL_BOTS) and "live_money" in rep and "## Evidence score" in md and "Live-money criteria:" in md)
    check("brain run: evidence is a label only -- mode stays shadow and the policy note still says nothing was touched",
          rep["mode"] == "shadow" and "no bot was touched" in rep["policy_note"])

# ============================================================== endpoint
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
EP_DIR = tempfile.mkdtemp()
Path(EP_DIR, "config.json").write_text(json.dumps(
    {"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.environ["DATA_DIR"] = EP_DIR
from backend import app as app_module  # noqa: E402

app_module.DATA_DIR = Path(EP_DIR)
client = TestClient(app_module.app)
r0 = client.get("/api/signals/audit")
check("GET /api/signals/audit: available=false with a reason before the first run", r0.status_code == 200 and r0.json()["available"] is False and "00:30" in r0.json()["reason"])
Path(EP_DIR, "signals_audit.json").write_text(json.dumps(full))
r1 = client.get("/api/signals/audit").json()
check("GET /api/signals/audit serves the audit (bots, horizons, caveats)", r1["available"] is True and r1["bots"]["moonshot"]["status"] == "ok" and r1["horizons_hours"] == [1, 4, 24])
Path(EP_DIR, "signals_audit.json").write_text("{broken")
check("a corrupt audit file degrades to available=false (no 500)", client.get("/api/signals/audit").json()["available"] is False)
check("the audit endpoint is GET-only",
      set(app_module.app.openapi()["paths"]["/api/signals/audit"]) == {"get"})
appsrc = (REPO / "backend/app.py").read_text(encoding="utf-8")
check("nightly audit is scheduled at 00:30 UTC on startup", "_signal_audit_loop" in appsrc and "seconds_until_next(datetime.now(timezone.utc), 0, 30)" in appsrc
      and "asyncio.create_task(_signal_audit_loop())" in appsrc)
check("SIGNAL_AUDIT_MODE is registered (env defs + compose)", any(d["key"] == "SIGNAL_AUDIT_MODE" for d in app_module.ENV_VAR_DEFS)
      and any(e.startswith("SIGNAL_AUDIT_MODE=") for e in yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["survival-agent"]["environment"]))

# ========================================================= P18b + P19 in the ZIP
from backend import export as ex  # noqa: E402

os.environ["DATA_DIR"] = DATA_TMP
ex._utcnow = lambda: datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
write_log("main", "freqtrade.log", ["2026-09-29 07:00:00,100 - x - INFO - hello api_key=TopSecretValue123456"])
(Path(DATA_TMP) / "signals_audit.json").write_text(json.dumps(full), encoding="utf-8")
AGG_PAYLOAD = {"profit_all_coin": 12.5, "note": "auth Bearer abcdefghijklmnop12345 leaked?", "api_key": "SuperSecretAggKey987654"}


class AggFetcher:
    def __init__(self, dead=()):
        self.dead, self.paths = set(dead), []

    def _json(self, bot, path):
        self.paths.append((bot, path))
        if bot in self.dead or path.startswith("/api/v1/mix_tags"):
            return None
        return {"bot": bot, "path": path, **AGG_PAYLOAD}

    def trades(self, bot):
        return []


agg = AggFetcher(dead={"moonshot"})
ex._fetcher = lambda: agg
app2 = FastAPI()
app2.include_router(ex.router)
c2 = TestClient(app2)
rz = c2.get("/api/export/bundle?bots=main,moonshot&lines=50&include=aggregates,signals,logs")
z = zipfile.ZipFile(io.BytesIO(rz.content))
names = set(z.namelist())
check("bundle: ZIP is valid and contains signals_audit.json at the root", rz.status_code == 200 and z.testzip() is None and "signals_audit.json" in names)
check("bundle: the raw ledger files go under signals/ (when they exist)", "signals/signals.jsonl" in names and "signals/signals_forward.jsonl" in names)
expected = {"profit", "daily", "weekly", "monthly", "performance", "stats", "entries", "exits", "balance"}
check("P18b: aggregates/<bot>/<endpoint>.json for profit, daily, weekly, monthly, performance, stats, entries, exits, balance",
      all(f"aggregates/main/{n}.json" in names for n in expected))
check("P18b: an endpoint that returns nothing (mix_tags) is omitted and recorded, not faked", "aggregates/main/mix_tags.json" not in names)
check("P18b: a dead bot has no aggregate files", not any(n.startswith("aggregates/moonshot/") for n in names))
called = {p for b, p in agg.paths if b == "main"}
check("P18b: exactly the ten FreqUI aggregate endpoints are requested, all GET (+ timescale on the period tabs)",
      {p.split("?")[0] for p in called} == {"/api/v1/" + n for n in ("profit", "daily", "weekly", "monthly", "performance", "stats", "entries", "exits", "mix_tags", "balance")}
      and len(ex.AGG_ENDPOINTS) == 10)
body = z.read("aggregates/main/profit.json").decode()
check("P18b: secrets in aggregate payloads are masked (same masking as the logs)",
      "SuperSecretAggKey987654" not in body and "abcdefghijklmnop12345" not in body and "profit_all_coin" in body and "12.5" in body)
man = json.loads(z.read("MANIFEST.json"))
check("MANIFEST: aggregates sizes per endpoint and a total",
      man["bots"]["main"]["aggregates"]["profit"]["included"] is True and man["bots"]["main"]["aggregates"]["profit"]["bytes"] == len(body.encode())
      and man["aggregates_total_bytes"] > 0)
check("MANIFEST: unavailable aggregates are listed with a note", man["bots"]["main"]["aggregates"]["mix_tags"]["included"] is False
      and man["bots"]["moonshot"]["aggregates"]["profit"]["included"] is False)
check("MANIFEST: signals inventory (audit included, ledger files listed)", man["signals"]["signals_audit.json"]["included"] is True
      and man["signals"]["signals.jsonl"]["included"] is True)
check("the archived log is still masked", b"TopSecretValue123456" not in z.read("main/logs.txt"))
ra = c2.get("/api/export/bundle?bots=main&include=aggregates")
check("include=aggregates alone works (no logs/trades parts)", {n.split("/")[0] for n in zipfile.ZipFile(io.BytesIO(ra.content)).namelist()} == {"aggregates", "MANIFEST.json"})
check("default include now covers aggregates and signals", ex.DEFAULT_INCLUDE.startswith("logs,trades,brain,decisions,aggregates,signals"))
os.remove(Path(DATA_TMP) / "signals_audit.json")
rn = c2.get("/api/export/bundle?bots=main&include=signals")
mn = json.loads(zipfile.ZipFile(io.BytesIO(rn.content)).read("MANIFEST.json"))
check("no audit yet: the bundle still builds and the manifest says so", rn.status_code == 200 and mn["signals"]["signals_audit.json"]["included"] is False)
cat = c2.get("/api/export/catalog").json()
check("catalog exposes the signals ledger/audit state and the aggregate endpoint list", "signals" in cat and cat["signals"]["audit_available"] is False
      and set(cat["aggregates"]) == set(ex.AGG_ENDPOINTS))
check("port 3446 publishing is unchanged by this work (still the single published port of survival-agent)",
      yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["survival-agent"]["ports"] == ["3446:8088"])
html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("frontend: signal audit card, evidence chips, updated bundle include list",
      'id="signal-audit"' in html and "loadSignalAudit" in html and "EVIDENCE_COLOR" in html and "include=logs,trades,brain,decisions,aggregates,signals" in html
      and "not enough data" in html)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P19 / P18b CHECKS PASSED")
