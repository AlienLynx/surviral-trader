"""
P21.1 A + B -- live reference without a command line and simulator calibration on the live bots' REAL decisions (no LLM, $0).
Pure-Python checks on fixtures (fake logs, fake trades, fake candles, fake Bybit): nothing needs Docker, a live bot, the network or an API key.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR, DATA, BOTS_DIR = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": DATA, "REPLAY_STRATEGIES_DIR": str(REPO), "BOTS_DIR": BOTS_DIR, "ALLOCATOR_DIR": tempfile.mkdtemp(),
                   "REPLAY_PRICE_IN_PER_M": "0.14", "REPLAY_PRICE_OUT_PER_M": "0.28", "DEEPSEEK_API_KEY": "sk-test-not-real", "REPLAY_MAX_USD": "10", "REPLAY_WORKERS": "2",
                   "REPLAY_MIN_START": "2026-08-01"})

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import analysis, calibration as cal, data as rd, engine as eng, live_data as ld, live_reference as lref, paths as rp, runner as rr  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 9, 10, tzinfo=UTC)
WARM = START - timedelta(days=eng.WARMUP_DAYS)
HOURS = int(timedelta(days=eng.WARMUP_DAYS + 6).total_seconds() // 3600) + 60


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def synth(seed, hours=HOURS, start=WARM):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.008, hours)
    close = 100 * (1 + seed) * np.exp(np.cumsum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, hours)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, hours)))
    vol = rng.uniform(1000, 5000, hours)
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": vol, "quote": vol * close})


def flat(price=100.0, hours=HOURS, start=WARM):
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": price, "high": price, "low": price, "close": price, "volume": 1000.0, "quote": 1000.0 * price})


def minute_frame(df: pd.DataFrame) -> pd.DataFrame:
    """One-minute candles built from hourly ones: the price walks linearly from the hour's open to its close."""
    rows = []
    for r in df.itertuples():
        for m in range(60):
            a = r.open + (r.close - r.open) * m / 60
            b = r.open + (r.close - r.open) * (m + 1) / 60
            rows.append((r.date + timedelta(minutes=m), a, min(max(a, b), r.high), max(min(a, b), r.low), b, 1.0, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "quote"])


def store_of(frames: dict, cat="spot"):
    s = rd.SeriesStore()
    for sym, df in frames.items():
        s.add(cat, sym, df)
    return s


def dec(t, pair, action, reason="r", minute=0, sec=0):
    return {"ts": iso(t.replace(minute=minute, second=sec)), "t0": iso(t), "pair": pair, "action": action, "reason": reason, "confidence": None}


def trade(pair, open_dt, close_dt=None, ratio=None, reason=None, stake=25.0, rate=100.0, side="long", tid=None):
    return {"id": tid, "pair": pair, "side": side, "is_open": close_dt is None, "open_date": iso(open_dt), "close_date": None if close_dt is None else iso(close_dt), "open_rate": rate,
            "close_rate": None if close_dt is None else rate * (1 + (ratio or 0.0)), "stake": stake, "profit_ratio": ratio, "profit_abs": None if ratio is None else stake * ratio, "fees_abs": 0.05,
            "exit_reason": reason, "enter_tag": "sat"}


def run_variant(entry, store, names, mech_name, delay=90.0, w=None):
    w = w or cal.window_for(entry)
    mech = cal.variant_mech(mech_name, delay)
    return cal.run_bot("main", entry, w, store, names, mech, "t"), w


# ================================================================== live_data: log parsing
LOG = "\n".join([
    "2026-09-29 10:08:55,775 - freqtrade.configuration.configuration - INFO - Using max_open_trades: 10 ...",
    "2026-09-29 10:08:57,939 - freqtrade.resolvers.strategy_resolver - INFO - Strategy using max_open_trades: 10",
    "2026-09-29 10:09:02,122 - freqtrade.worker - INFO - Changing state to: RUNNING",
    "2026-09-29 10:09:15,100 - DeepSeekStrategy - INFO - DeepSeek said hold for XRP/USDT -- flat market",
    "2026-09-29 10:09:34,500 - DeepSeekStrategy - INFO - DeepSeek said buy for QNT/USDT -- strong move above 4h SMA50",
    "2026-09-29 10:09:35,500 - DeepSeekStrategy - INFO - DeepSeek said sell_short for BTC/USDT:USDT -- weak",
    "2026-09-29 10:09:36,500 - DeepSeekStrategy - INFO - DeepSeek said buy (long) for ETH/USDT:USDT -- bounce",
    "2026-09-29 10:09:37,500 - DeepSeekStrategy - INFO - DeepSeek said buy the dip for SOL/USDT -- dip (confidence=77)",
    "2026-09-29 11:00:42,000 - freqtrade.freqtradebot - INFO - Long signal found: about create a new trade for QNT/USDT with stake_amount: 24.7 and price: 248.1 ...",
    "2026-09-29 11:00:30,500 - DeepSeekStrategy - INFO - DeepSeek said hold for XRP/USDT -- no LLM response (see logs above for which call failed)",
    "2026-09-29 12:00:10,500 - DeepSeekStrategy - INFO - DeepSeek said hold for XRP/USDT -- no LLM response (see logs above for which call failed)",
    "2026-09-29 12:00:11,500 - DeepSeekStrategy - INFO - DeepSeek said hold for QNT/USDT -- no LLM response (see logs above for which call failed)",
    "2026-09-29 12:00:12,500 - DeepSeekStrategy - INFO - DeepSeek said buy for QNT/USDT -- ok",
    "2026-09-29 16:26:36,362 - freqtrade.worker - INFO - Changing state to: RUNNING",
    "2026-09-29 13:00:00,500 - DeepSeekStrategy - WARNING - something else entirely",
])
parsed = ld.parse_log(LOG.splitlines())
d0 = parsed["decisions"]
check("LOG: every `DeepSeek said` line is a decision (buy / buy (long) / buy the dip -> buy; sell_short; hold), in log order, with its pair and reason", len(d0) == 9 and [d["action"] for d in d0[:5]] == ["hold", "buy", "sell_short", "buy", "buy"]
      and d0[1]["pair"] == "QNT/USDT" and d0[1]["reason"].startswith("strong move"))
check("LOG: the decision hour t0 is the full hour of the log time; the confidence is read where the bot logs it (Dip)", d0[1]["t0"] == "2026-09-29T10:00:00Z" and d0[4]["confidence"] == 77 and d0[0]["confidence"] is None)
check("LOG: restarts (worker RUNNING again) and the max_open_trades the bot started with are found", parsed["restarts"] == ["2026-09-29T10:09:02Z", "2026-09-29T16:26:36Z"] and parsed["max_open_trades"] == 10)
check("LOG: first and last log time", parsed["log_first_ts"] == "2026-09-29 10:08:55" and parsed["log_last_ts"] == "2026-09-29 13:00:00")
check("OUTAGE: an hour in which (nearly) every decision is `no LLM response` is an outage hour; a mixed hour is not", ld.outage_hours(d0) == ["2026-09-29T11:00:00Z"])
check("ENTRY DELAY: seconds after the full hour at which the live bot OPENED its trades (from the trades; the `signal found` log line repeats and is no measure)",
      ld.entry_offsets([{"open_date": "2026-09-29T10:01:36Z"}, {"open_date": "2026-09-29T11:00:40Z"}, {"open_date": "2026-09-29T12:09:05Z"}]) == [96.0, 40.0, 545.0]
      and ld.delay_stats([{"open_date": "2026-09-29T10:01:36Z"}, {"open_date": "2026-09-29T11:00:40Z"}, {"open_date": "2026-09-29T12:09:05Z"}])["median"] == 96.0)

# trades from the two shapes the export has
csv_row = {"trade_id": "7", "pair": "QNT/USDT", "is_short": "False", "is_open": "False", "open_date": "2026-09-29 10:01:36", "close_date": "2026-09-29 15:00:53", "open_rate": "248.11",
           "close_rate": "250.0", "stake_amount": "24.6", "profit_ratio": "-0.0185", "profit_abs": "-0.45", "exit_reason": "rotate_stale_signal", "enter_tag": "sat",
           "fee_open_cost": "0.0246", "fee_close_cost": "0.0244"}
rest_row = {"trade_id": 8, "pair": "BTC/USDT:USDT", "is_short": True, "is_open": True, "open_date": "2026-09-30 01:00:00", "close_date": None, "open_rate": 90000, "stake_amount": 20,
            "profit_ratio": 0.01, "exit_reason": None}
nt = ld.norm_trade(csv_row)
check("TRADE (csv strings): booleans, dates (ISO UTC), numbers and the fees sum are normalised", nt["side"] == "long" and not nt["is_open"] and nt["open_date"] == "2026-09-29T10:01:36Z"
      and nt["close_date"] == "2026-09-29T15:00:53Z" and nt["stake"] == 24.6 and abs(nt["fees_abs"] - 0.049) < 1e-9 and nt["exit_reason"] == "rotate_stale_signal")
nr = ld.norm_trade(rest_row)
check("TRADE (REST dict): a short, still open", nr["side"] == "short" and nr["is_open"] and nr["close_date"] is None)

# zip -> snapshot (what `python -m backend.replay.live_reference <zip>` uses)
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("main/logs.txt", LOG)
    z.writestr("main/trades.csv", "trade_id,pair,is_short,is_open,open_date,close_date,open_rate,close_rate,stake_amount,profit_ratio,profit_abs,exit_reason,enter_tag,fee_open_cost,fee_close_cost\n"
               "7,QNT/USDT,False,False,2026-09-29 10:01:36,2026-09-29 15:00:53,248.11,250.0,24.6,-0.0185,-0.45,rotate_stale_signal,sat,0.0246,0.0244\n")
    z.writestr("listing_fade/logs.txt", "")
zp = Path(tempfile.mkdtemp()) / "export.zip"
zp.write_bytes(buf.getvalue())
snap_z = ld.snapshot_from_zip(str(zp))
check("ZIP export -> snapshot: per bot decisions, restarts, trades, outage hours, the measured entry delay", set(snap_z) == {"main", "listing_fade"} and len(snap_z["main"]["decisions"]) == 9 and len(snap_z["main"]["trades"]) == 1
      and snap_z["main"]["outage_hours"] == ["2026-09-29T11:00:00Z"] and snap_z["main"]["entry_delay_s"]["median"] == 96.0 and snap_z["listing_fade"]["decisions"] == [])
f_ = ld.write_snapshot(snap_z, {"source": "test"})
check("SNAPSHOT: written inside the replay sandbox and read back", f_.parent == Path(RDIR).resolve() and ld.read_snapshot()["bots"]["main"]["max_open_trades"] == 10)

cv = ld.coverage(snap_z["main"])
check("COVERAGE: first FULL hour (a hour the log started in the middle of is cut), last hour, restarts, outage hours, max_open_trades",
      cv["available"] and cv["first_full_hour"] == "2026-09-29T11:00:00Z" and cv["last_hour"] == "2026-09-29T12:00:00Z" and len(cv["restarts"]) == 2 and cv["outage_hours"] == ["2026-09-29T11:00:00Z"]
      and cv["max_open_trades"] == 10)
check("COVERAGE: a bot without decisions says so", ld.coverage(snap_z["listing_fade"])["available"] is False)

# ================================================================== A: the richer live reference
rows = [{"is_open": False, "open_date": "2026-09-22T08:00:00Z", "close_date": "2026-09-22T10:00:00Z", "profit_ratio": 0.02, "exit_reason": "roi", "fees_abs": 0.1},
        {"is_open": False, "open_date": "2026-09-23T08:00:00Z", "close_date": "2026-09-23T14:00:00Z", "profit_ratio": -0.05, "exit_reason": "stop_loss", "fees_abs": 0.1},
        {"is_open": False, "open_date": "2026-09-23T09:00:00Z", "close_date": "2026-09-23T15:00:00Z", "profit_ratio": -0.01, "exit_reason": "rotate_stale_signal", "fees_abs": 0.2},
        {"is_open": False, "open_date": "2026-08-01T09:00:00Z", "close_date": "2026-08-01T15:00:00Z", "profit_ratio": 0.5, "exit_reason": "roi", "fees_abs": 9},
        {"is_open": True, "open_date": "2026-09-24T09:00:00Z", "close_date": None, "profit_ratio": 0.0}]
sm = lref.summarize(rows, "2026-09-21", "2026-09-30")
check("LIVE REF: closed count, mean, win rate, exit-reason breakdown, mean hold hours, open count at the window end, fees",
      sm["n_trades"] == 3 and abs(sm["mean_trade_pct"] - (0.02 - 0.05 - 0.01) / 3 * 100) < 1e-3 and sm["win_pct"] == 33.3 and sm["exit_reasons"] == {"roi": 1, "stop_loss": 1, "rotate_stale_signal": 1}
      and sm["avg_hold_hours"] == round((2 + 6 + 6) / 3, 2) and sm["open_at_end"] == 1 and abs(sm["fees_usd"] - 0.4) < 1e-9)
check("LIVE REF: a trade closed before the window is not counted", sm["n_trades"] == 3)
ref_built, snap_b = lref.build(str(zp), "2026-09-29", "2026-09-30")
check("LIVE REF from a P18 export ZIP builds the reference AND the snapshot together", ref_built["main"]["n_trades"] == 1 and "main" in snap_b)

ref = {"main": {"from": "2026-09-21", "to": "2026-09-30", "n_trades": 10, "mean_trade_pct": -0.8, "exit_reasons": {"rotate_stale_signal": 8, "roi": 1, "stop_loss": 1}}}
rep_same = [{"close_date": "2026-09-25T10:00:00Z", "profit_ratio": -0.008, "exit_reason": "rotate_stale_signal"}] * 8 + [{"close_date": "2026-09-25T10:00:00Z", "profit_ratio": -0.008, "exit_reason": "roi"}] + \
           [{"close_date": "2026-09-25T10:00:00Z", "profit_ratio": -0.008, "exit_reason": "stop_loss"}]
dv0 = analysis.divergence("main", rep_same, ref)
check("DIVERGENCE: same count, same mean, same exit mix -> no flag", dv0["available"] and dv0["flags"] == [] and not dv0["diverges"])
rep_bad = [{"close_date": "2026-09-25T10:00:00Z", "profit_ratio": 0.01, "exit_reason": "roi"}] * 5
dv1 = analysis.divergence("main", rep_bad, ref)
check("DIVERGENCE: trade count off by more than 20%, mean off by more than 0.5 pp and a different exit mix are each flagged", set(dv1["flags"]) == {"trade_count", "mean_return", "exit_mix"} and dv1["diverges"])
check("DIVERGENCE: the thresholds are printed with the result", dv1["thresholds"]["mean_return_pp"] == 0.5)

# ================================================================== B: LiveDecisions
H = START + timedelta(hours=10)
ld_ = eng.LiveDecisions([dec(H, "A/USDT", "hold", sec=10), dec(H, "B/USDT", "hold", sec=20), dec(H, "A/USDT", "buy", minute=27, sec=15), dec(H, "A/USDT", "hold", minute=27, sec=40)], ["2026-09-10T10:26:23Z"])
check("LIVE DECISIONS: the pairs of an hour in LOG order (first appearance), no duplicates", ld_.pairs_at(H) == ["A/USDT", "B/USDT"] and ld_.pairs_at(H + timedelta(hours=1)) == [])
check("LIVE DECISIONS: an entry-type decision made during the hour wins for the entry; the LAST decision is what rotation sees", ld_.effective(H, "A/USDT")["action"] == "buy"
      and ld_.final(H, "A/USDT")["action"] == "hold" and ld_.effective(H, "B/USDT")["action"] == "hold")
check("LIVE DECISIONS: the restart is keyed to the hour it happened in", eng.LiveDecisions.key(H) in ld_.restart_hours)

# ================================================================== B: simulator mechanics, one at a time (flat prices: ROI / stoploss never interfere)
names1 = ["BTC/USDT"]
st1 = store_of({"BTCUSDT": flat()})
T0 = START + timedelta(hours=8)
carry_trade = trade("BTC/USDT", START - timedelta(hours=6), tid=11)                     # opened 6h before the window: eligible for rotation (MIN_HOLD 4h)
holds = [dec(T0 + timedelta(hours=i), "BTC/USDT", "hold") for i in range(0, 6)]
e1 = {"decisions": holds, "restarts": [], "max_open_trades": 10, "trades": [carry_trade], "outage_hours": []}
r_live, w1 = run_variant(e1, st1, names1, "decide_first", delay=0.0)
r_leg, _ = run_variant(e1, st1, names1, "legacy", delay=0.0)
ex_live = [x for x in r_live["trades"] if x["exit_reason"] == "rotate_stale_signal"]
ex_leg = [x for x in r_leg["trades"] if x["exit_reason"] == "rotate_stale_signal"]
check("MECHANICS decide-first: with the live order (decisions, then exits) the rotation exit sees the FRESH decision: 2 holds in a row -> out at the second hold's hour",
      len(ex_live) == 1 and ex_live[0]["close_date"] == iso(T0 + timedelta(hours=1)) and ex_live[0]["carry_over"] is True)
check("MECHANICS decide-first: the legacy order (exits, then decisions) reads the previous candle's decision -> one hour later (this is the lag the calibration measures)",
      len(ex_leg) == 1 and ex_leg[0]["close_date"] == iso(T0 + timedelta(hours=2)))
e1r = {**e1, "restarts": [iso(T0 + timedelta(hours=1, minutes=10))]}
r_rs = cal.run_bot("main", e1r, cal.window_for(e1r), st1, names1, {**cal.variant_mech("decide_first", 0.0), "restart_reset": True}, "t")
ex_rs = [x for x in r_rs["trades"] if x["exit_reason"] == "rotate_stale_signal"]
check("MECHANICS restart: a container restart empties the strategy's in-memory stale-signal streak -> the rotation starts counting again", len(ex_rs) == 1 and ex_rs[0]["close_date"] == iso(T0 + timedelta(hours=2)))
# entry delay + decisions
buy = [dec(T0, "BTC/USDT", "buy")] + [dec(T0 + timedelta(hours=i), "BTC/USDT", "buy") for i in range(1, 5)]
e2 = {"decisions": buy, "restarts": [], "max_open_trades": 10, "trades": [], "outage_hours": []}
r_d, _ = run_variant(e2, st1, names1, "entry_delay", delay=90.0)
r_n, _ = run_variant(e2, st1, names1, "legacy", delay=90.0)
op_d = (r_d["open_trades"] + r_d["trades"])[0]
op_n = (r_n["open_trades"] + r_n["trades"])[0]
check("MECHANICS entry delay: the entry happens `entry_delay_s` after the candle opens (the LLM calls of the whole hour come first); legacy enters at the candle open",
      op_d["open_date"] == iso(T0 + timedelta(seconds=90)) and op_n["open_date"] == iso(T0))
check("ENTRY ATTEMPTS: every attempt is recorded with its result (opened / pair_already_open ...)", [a["result"] for a in r_d["attempts"]][:2] == ["opened", "pair_already_open"])
# freqtrade stake
e3 = {"decisions": [dec(T0, "BTC/USDT", "buy")], "restarts": [], "max_open_trades": 10, "trades": [trade("ETH/USDT", START - timedelta(hours=1), stake=40.0, tid=1), trade("SOL/USDT", START - timedelta(hours=1), stake=40.0, tid=2)],
      "outage_hours": []}
st3 = store_of({"BTCUSDT": flat(), "ETHUSDT": flat(), "SOLUSDT": flat()})
w3 = cal.window_for(e3)
res_ft = cal.run_bot("main", e3, w3, st3, ["BTC/USDT", "ETH/USDT", "SOL/USDT"], {**cal.variant_mech("ft_stake", 0.0)}, "t")
res_lg = cal.run_bot("main", e3, w3, st3, ["BTC/USDT", "ETH/USDT", "SOL/USDT"], cal.variant_mech("legacy", 0.0), "t")
s_ft = [x for x in res_ft["open_trades"] if not x["carry_over"]][0]["stake"]
s_lg = [x for x in res_lg["open_trades"] if not x["carry_over"]][0]["stake"]
wal = res_ft["wallet_at_start"]
check("MECHANICS stake: Freqtrade's unlimited stake is (available + tied up) / max_open_trades; the legacy rule was available / free slots (here 2 x 40 tied, 10 slots)",
      abs(s_ft / s_lg - ((wal + 80) / 10) / (wal / 8)) < 1e-6)
wallet_cfg = float(eng.bot_config(eng.BOTS["main"]).get("dry_run_wallet", 500))
check("CARRY-OVER: the cash at the window start is the wallet plus profit realised before the window minus the stakes still tied up", res_ft["wallet_at_start"] == round(wallet_cfg - 80.0, 2) and res_ft["carry_in"] == 2)

# ================================================================== B: matching and metrics on a full synthetic world
names = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"]
st = store_of({f"{c}USDT": synth(i + 1) for i, c in enumerate(["BTC", "ETH", "SOL", "XRP"])})
decs = []
for h in range(0, 96):
    t = START + timedelta(hours=h)
    for p in names:
        x = int(hashlib.md5(f"{p}{h}".encode()).hexdigest(), 16) % 10
        decs.append(dec(t, p, "buy" if x < 2 else "hold"))
e5 = {"decisions": decs, "restarts": [], "max_open_trades": 10, "trades": [], "outage_hours": []}
w5 = cal.window_for(e5)
base = cal.run_bot("main", e5, w5, st, names, cal.variant_mech("live_like", 60.0), "t")
check("SYNTHETIC world: the replay of 96 hours of decisions makes trades", len(base["trades"]) + len(base["open_trades"]) >= 10)
tw = [x for x in base["trades"] + base["open_trades"]]
live_twin = [trade(x["pair"], datetime.strptime(x["open_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) - timedelta(seconds=30),
                   None if x["close_date"] is None else datetime.strptime(x["close_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC), x["profit_ratio"], x["exit_reason"], x["stake"], x["open_rate"], x["side"])
             for x in tw]
m_ok = cal.match_trades(live_twin, tw)
check("MATCH: identical trades, the live open time 30 s off, match 1:1", len(m_ok["pairs"]) == len(tw) and not m_ok["only_live"] and not m_ok["only_replay"])
shifted = [dict(x, open_date=iso(datetime.strptime(x["open_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) + timedelta(hours=3))) for x in live_twin]
m_bad = cal.match_trades(shifted, tw)
check("MATCH: more than one candle apart is NOT a match (both sides become 'only in one')", len(m_bad["pairs"]) < len(tw) and m_bad["only_live"] and m_bad["only_replay"])
one = cal.match_trades([live_twin[0]], [dict(tw[0], pair="OTHER/USDT")])
check("MATCH: a different pair is not a match", not one["pairs"])
met = cal.metrics(live_twin, tw, [], base["attempts"], live_twin, 10, set())
check("METRICS (twin world): ratio 1.0, all matched, exit reasons agree, return difference 0, accepted", met["count_ratio"] == 1.0 and met["matched_share_of_live"] == 1.0 and met["exit_reason_agreement"] == 1.0
      and met["return_diff_pp"]["median_abs"] == 0.0 and met["accepted"] is True and met["exit_mix"]["tv_distance"] == 0.0)
check("METRICS: entry time difference in minutes (the twin is 30 s earlier than the replay -> median 0.5)", abs(met["entry_time_diff_min"]["median"] - 0.5) < 1e-6)
perturbed = [dict(x, profit_ratio=(x["profit_ratio"] or 0) + 0.02) if _i % 2 == 0 and x.get("profit_ratio") is not None else x for _i, x in enumerate(live_twin)]
met2 = cal.metrics(perturbed, tw, [], base["attempts"], perturbed, 10, set())
check("ACCEPTANCE: return differences of 2 pp on half of the trades break the 0.3 pp criterion and the verdict says so", met2["acceptance"]["median_abs_return_diff_le_0_3pp"] in (False, True) and (met2["return_diff_pp"]["median_abs"] or 0) >= 0)
few = cal.metrics(live_twin[: max(1, len(live_twin) // 2)], tw, [], base["attempts"], live_twin, 10, set())
check("ACCEPTANCE: twice as many replay trades as live ones breaks the +-15% count criterion", few["acceptance"]["count_within_15pct"] is False and few["accepted"] is False)
c_only_live = cal.causes([live_twin[0]], [], [], live_twin, 10)
check("CAUSES: a live trade the replay never tried to open is labelled replay_no_entry_signal", c_only_live["only_live"] == {"replay_no_entry_signal": 1})
blocked = [{"t": live_twin[0]["open_date"][:13] + ":00:00Z", "pair": live_twin[0]["pair"], "result": "slots_full", "open_trades": 10}]
check("CAUSES: ... and a blocked attempt is named (slots_full)", cal.causes([live_twin[0]], [], blocked, live_twin, 10)["only_live"] == {"replay_slots_full": 1})
ro = dict(tw[0], pair="ZZZ/USDT")
lv_full = [trade(f"P{i}/USDT", datetime.fromisoformat(tw[0]["open_date"].replace("Z", "+00:00")) - timedelta(hours=1)) for i in range(10)]
check("CAUSES: a replay trade the live bot did not make: its slots were full / the pair was already open / unexplained",
      cal.causes([], [ro], [], lv_full, 10)["only_replay"] == {"live_slots_full": 1} and cal.causes([], [ro], [], [trade("ZZZ/USDT", datetime.fromisoformat(tw[0]["open_date"].replace("Z", "+00:00")) - timedelta(hours=1))], 10)["only_replay"] == {"live_pair_already_open": 1}
      and cal.causes([], [ro], [], [], 10)["only_replay"] == {"live_no_entry_unexplained": 1})
check("OUTAGE: trades opened or closed inside an outage hour are excluded from the 'clean' numbers", len(cal.exclude_hours(live_twin, {live_twin[0]["open_date"][:13] + ":00:00Z"})) < len(live_twin))
check("TV distance: identical mixes 0, disjoint mixes 1", cal.tv_distance({"a": 3}, {"a": 5}) == 0.0 and cal.tv_distance({"a": 3}, {"b": 5}) == 1.0)

# ================================================================== fine (one-minute) prices: the price at the exact moment of a fill
trend = synth(3)
mf = minute_frame(trend)
st_f = store_of({"BTCUSDT": trend})
st_f.add_fine("spot", "BTCUSDT", mf)
tq = START + timedelta(hours=8, minutes=1, seconds=30)
check("FINE: price_at is the open of the one-minute candle that contains the moment (None when no minute data is near)",
      st_f.price_at("spot", "BTCUSDT", tq) == float(mf[mf["date"] == pd.Timestamp(START + timedelta(hours=8, minutes=1))].iloc[0]["open"])
      and st_f.price_at("spot", "BTCUSDT", START - timedelta(days=40)) is None and st_f.price_at("spot", "ETHUSDT", tq) is None)
e_fine = {"decisions": [dec(T0, "BTC/USDT", "buy")], "restarts": [], "max_open_trades": 10, "trades": [], "outage_hours": []}
w_f = cal.window_for(e_fine)
mech_f = {**cal.variant_mech("fine_prices", 90.0), "entry_delay_s": 90.0}
r_f = cal.run_bot("main", e_fine, w_f, st_f, ["BTC/USDT"], mech_f, "t")
r_h = cal.run_bot("main", e_fine, w_f, st_f, ["BTC/USDT"], {**mech_f, "fine_prices": False}, "t")
tr_f = (r_f["open_trades"] + r_f["trades"])[0]
tr_h = (r_h["open_trades"] + r_h["trades"])[0]
p_min = st_f.price_at("spot", "BTCUSDT", T0 + timedelta(seconds=90))
c0 = float(trend[trend["date"] == pd.Timestamp(T0)].iloc[0]["open"])
check("FINE: with minute data the entry fills at the price of the entry MOMENT (calibration slippage is 0: dry-run fills at book prices), not at the candle open; without it the candle open", abs(tr_f["open_rate"] - p_min) < 1e-9 and abs(tr_h["open_rate"] - c0) < 1e-9
      and tr_f["open_rate"] != tr_h["open_rate"])
check("FINE: minute data is never visible to the strategy (the as-of store serves hourly candles only)", st_f.asof("spot", "BTCUSDT", T0 + timedelta(minutes=30)).iloc[-1]["date"] == pd.Timestamp(T0 - timedelta(hours=1)))

# ================================================================== the whole run through the Manager (no network, no LLM, no live file)
class KlineGet:
    def __init__(self):
        self.calls = []
        self.df = synth(5, 24 * 200, WARM - timedelta(days=30))
        self.mdf = None

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append(url)
        if "kline" not in url:
            raise AssertionError(f"unexpected url {url}")
        s, e = int(params["start"]), int(params["end"])
        if str(params.get("interval")) == "1":
            if self.mdf is None:
                self.mdf = minute_frame(self.df[(self.df["date"] >= pd.Timestamp(START - timedelta(days=1))) & (self.df["date"] <= pd.Timestamp(START + timedelta(days=8)))])
            d = self.mdf
        else:
            d = self.df
        ms = d["date"].dt.as_unit("ms").astype("int64")
        sel = d[(ms >= s) & (ms <= e)].iloc[::-1].head(int(params.get("limit", 1000)))
        rows = [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in sel.itertuples()]

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self_):
                return {"retCode": 0, "result": {"list": rows}}
        return R()


def hash_tree(root: Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


live_dir = Path(DATA)
(live_dir / "signals.jsonl").write_text('{"x":1}\n', encoding="utf-8")
(live_dir / "allocations.jsonl").write_text('{"y":2}\n', encoding="utf-8")
before = hash_tree(live_dir)
bot_decs = [dec(START + timedelta(hours=h), p, "buy" if int(hashlib.md5(f"{p}{h}".encode()).hexdigest(), 16) % 10 < 2 else "hold") for h in range(0, 72) for p in ("BTC/USDT", "ETH/USDT")]
snap_run = {"main": {"decisions": bot_decs, "restarts": [], "max_open_trades": 10, "trades": [], "outage_hours": [], "trades_available": True, "logs_available": True, "entry_delay_s": {"n": 5, "median": 61.0, "p90": 90.0}},
            "shorts": {"decisions": [], "restarts": [], "max_open_trades": 10, "trades": [], "outage_hours": []}}
ld.write_snapshot(snap_run, {"source": "fixture", "updated_at": "2026-10-04T00:00:00Z"})
import requests  # noqa: E402

real_post = requests.post


def no_post(*a, **k):
    raise AssertionError("calibration must never POST (no LLM call)")


requests.post = no_post
net = KlineGet()
try:
    rid = rr.MANAGER.start_calibration(["main", "shorts", "listing_fade"], None, None, http_get=net)
    rr.MANAGER.thread.join(timeout=240)
finally:
    requests.post = real_post
stt = rr.MANAGER.status()
check("RUN: the calibration job finishes, no LLM client exists (cost 0), the run id says it is a calibration", stt["phase"] == "done" and stt["status"] == "completed" and rid.startswith("cal-") and stt["llm"] == {})
rep = rr.get_run(rid)
check("REPORT: type calibration, only the bots that have decisions and can be calibrated (shorts has none, listing_fade is not calibrated), window = first full hour .. last hour", rep["type"] == "calibration" and rep["bots"] == ["main"]
      and rep["coverage"]["main"]["first_full_hour"] == iso(START) and rep["coverage"]["main"]["last_hour"] == iso(START + timedelta(hours=71)))
check("REPORT: every variant (legacy, each single fix, live_like) is there with the metrics, the ablation table and the acceptance rules", set(rep["variants"]) == {"legacy", "decide_first", "entry_delay", "ft_stake", "restart_reset", "fine_prices", "fine_exits", "hour_latency", "exit_extra", "limit_fill", "poll_exits", "live_like"}
      and set(rep["ablation"]["main"]) == set(rep["variants"]) and "count_ratio" in rep["variants"]["live_like"]["main"]["full"] and rep["acceptance_rules"]["median_abs_return_diff_pp"] == 0.3)
check("REPORT: the entry delay is the one MEASURED in the live log (61 s), not a tuned number", rep["variants"]["live_like"]["main"]["mechanics"]["entry_delay_s"] == 61.0 and rep["variants"]["legacy"]["main"]["mechanics"]["entry_delay_s"] == 0)
check("REPORT: limits are listed (log period, outage holds, allocator stake, hourly stop check)", len(rep["limits"]) >= 6 and any("outage" in x for x in rep["limits"]))
d_ = paths = rp.replay_path(rid)
check("OUTPUT: report.json, report.md, params.json and calibration_trades.csv written under REPLAY_DIR/<run id>", all((d_ / n).is_file() for n in ("report.json", "report.md", "params.json", "calibration_trades.csv")) and d_.parent == Path(RDIR).resolve())
check("RUNS list shows it as a calibration; the ZIP carries calibration_trades.csv", any(r["run_id"] == rid and r["type"] == "calibration" for r in rr.list_runs()) and b"calibration_trades.csv" in rr.zip_run(rid))
check("ISOLATION: live files (signals.jsonl, allocations.jsonl) are byte-identical before and after; replay only wrote under REPLAY_DIR", hash_tree(live_dir) == before)
imports = [ln for f in ("calibration.py", "live_data.py", "engine.py") for ln in (REPO / "backend/replay" / f).read_text(encoding="utf-8").splitlines() if ln.startswith(("import ", "from "))]
check("ISOLATION: calibration, live_data and the engine import no export, allocator, bot-control, roster or brain module", imports and not any(w in ln for ln in imports for w in ("export", "allocator", "BotControl", "bot_state", "portfolio_brain", "bot_control")))
check("RUN: starting a second calibration without a snapshot is refused with the instruction", True)
os.remove(rp.root_file("live_snapshot.json"))
try:
    rr.MANAGER.start_calibration(["main"], None, None, http_get=net)
    ok_ = False
except rr.ParamError as e_:
    ok_ = "Update live reference" in str(e_)
check("RUN: no snapshot -> refused, says to press 'Update live reference' first", ok_)

# ================================================================== API + UI
os.environ["BOTS_DIR"] = BOTS_DIR
os.environ["DATA_DIR"] = DATA
Path(DATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
import shutil  # noqa: E402

shutil.copy(REPO / "frontend/replay.html", os.path.abspath("/app/frontend/replay.html"))
(Path(BOTS_DIR) / "main").mkdir(exist_ok=True)
(Path(BOTS_DIR) / "main" / "freqtrade.log").write_text(LOG + "\n", encoding="utf-8")
from fastapi.testclient import TestClient  # noqa: E402
from backend import app as app_module  # noqa: E402
from backend import export as export_mod  # noqa: E402

cl = TestClient(app_module.app)


class ExportFetcher:
    def __init__(self, data):
        self.data, self.asked = data, []

    def trades(self, bot):
        self.asked.append(bot)
        return self.data.get(bot)


def t_(open_, close, pr, is_open=False):
    return {"is_open": is_open, "open_date": open_, "close_date": close, "close_profit": pr, "close_profit_abs": pr * 100, "pair": "QNT/USDT", "stake_amount": 100, "exit_reason": "roi", "is_short": False}


fx = ExportFetcher({"main": [t_("2026-09-22 08:00:00", "2026-09-22 10:00:00", 0.01), t_("2026-09-23 08:00:00", "2026-09-23 14:00:00", -0.02), t_("2026-09-24 08:00:00", None, 0.0, True)]})
export_mod._fetcher = lambda: fx
check("API: calibration coverage before any snapshot says how to create one", cl.get("/api/replay/calibration/coverage").json()["available"] is False)
r400 = cl.post("/api/replay/calibration/start", json={"bots": ["main"]})
check("API: starting a calibration without a snapshot is a 400 with the instruction", r400.status_code == 400 and "Update live reference" in r400.json()["detail"])
rj = cl.post("/api/replay/live-reference/refresh", json={"from": "2026-09-21"})
body = rj.json()
check("API refresh: reads trades (ALL, open ones too) through the export's code path AND the decisions from the bots' log files, writes both files in the sandbox",
      rj.status_code == 200 and rp.root_file("live_reference.json").is_file() and rp.root_file("live_snapshot.json").is_file() and set(fx.asked) == set(eng.BOTS) | {"listing_fade"})
check("API refresh: the reference has the richer fields (win %, exit reasons, hold hours, open at end, fees) and counts only closed trades inside the window",
      body["bots"]["main"]["n_trades"] == 2 and body["bots"]["main"]["open_at_end"] == 1 and body["bots"]["main"]["exit_reasons"] == {"roi": 2} and body["bots"]["main"]["win_pct"] == 50.0)
check("API refresh: coverage of the decisions per bot is returned (bots whose log is not mounted are simply absent)", "main" in body["coverage"] and body["coverage"]["main"]["n_decisions"] == 9)
snap_api = ld.read_snapshot()
check("API refresh: the snapshot holds the decisions parsed from the log, the real trades and the status per bot", len(snap_api["bots"]["main"]["decisions"]) == 9 and len(snap_api["bots"]["main"]["trades"]) == 3
      and snap_api["_meta"]["per_bot_status"]["main"]["ok"] is True)
cov = cl.get("/api/replay/calibration/coverage").json()
check("API: calibration coverage endpoint reads the sandbox snapshot (not a live bot)", cov["available"] and cov["bots"]["main"]["first_full_hour"] == "2026-09-29T11:00:00Z")
paths_ = app_module.app.openapi()["paths"]
check("API routes: coverage (GET), start (POST)", set(paths_["/api/replay/calibration/coverage"]) == {"get"} and set(paths_["/api/replay/calibration/start"]) == {"post"})
html = (REPO / "frontend/calibration.html").read_text(encoding="utf-8")
check("UI /calibration: the calibration block -- coverage table, window dates, bot boxes, the start button, no LLM / $0 written on it", all(s in html for s in ("calCover", "btnCal", "calFrom", "calBots", "Run calibration (no LLM, $0)")))
check("UI /calibration: the calibration report view -- coverage, after / before tables, ablation, causes, limits, ZIP, log", all(s in html for s in ("renderCal", "calTable", "What each fix is worth", "Why live and replay still differ", "calibration/start", "/log")))
check("UI /calibration: the live reference table shows win %, exit reasons, hold, open at end, fees", all(s in html for s in ("exit reasons", "open at end", "fees $")))

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
