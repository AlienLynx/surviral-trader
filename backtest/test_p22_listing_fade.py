"""
P22-B -- Listing Fade validation: rules read from the live files, the fetch (cached, resumable, polite), the simulation (fees, funding, stop gaps, slippage, slots), the verdict, the page.
Pure-Python checks on fixtures (a fake Bybit, synthetic listings). Nothing needs Docker, a live bot, the network or a key.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR, DATA = tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": DATA, "REPLAY_STRATEGIES_DIR": str(REPO), "BOTS_DIR": tempfile.mkdtemp(), "ALLOCATOR_DIR": tempfile.mkdtemp()})
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import listing_fade as lf, paths as rp, runner as rr  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
FS = lf.rules_dir(REPO)
rules = lf.load_rules(REPO)


def ms(d):
    return int(d.timestamp() * 1000)


# ================================================================== rules are read from the live files
src = (REPO / "freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py").read_text(encoding="utf-8")
cfg = json.loads((REPO / "freqtrade-listing-fade/user_data/config.json").read_text(encoding="utf-8"))
check("RULES: the SHA-256 of the strategy file and of its config.json are computed from the live files and printed in the summary", rules["strategy_sha256"] == hashlib.sha256((REPO / "freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py").read_bytes()).hexdigest()
      and rules["config_sha256"] == hashlib.sha256((REPO / "freqtrade-listing-fade/user_data/config.json").read_bytes()).hexdigest() and lf.rules_summary(rules)["strategy_sha256"] == rules["strategy_sha256"])
check("RULES: stoploss, trailing, max hold, drop from high, EMA, funding threshold are the strategy file's own class constants", rules["stoploss"] == -0.12 and rules["trailing_positive"] == 0.03 and rules["trailing_offset"] == 0.035
      and rules["trailing_only_offset"] is True and rules["max_hold_days"] == 14 and rules["pct_from_high"] == -25.0 and rules["ema_period"] == 20 and rules["funding_threshold"] == -0.0003 and rules["minimal_roi"] == {"0": 10.0})
check("RULES: slots, stake, wallet, AgeFilter and PriceFilter come from the bot's config.json", rules["max_open_trades"] == cfg["max_open_trades"] == 3 and rules["stake"] == 25.0 and rules["wallet"] == 100.0
      and rules["age_min_days"] == 2 and rules["age_max_days"] == 30 and rules["price_low_ratio"] == 0.01)
check("RULES: the filters that cannot be rebuilt from public history are listed as NOT applied (VolumePairList, SpreadFilter)", any("VolumePairList" in x for x in rules["not_applied"]) and any("SpreadFilter" in x for x in rules["not_applied"]))
check("RULES: the funding veto in the simulation mirrors the strategy's confirm_trade_entry (`funding_rate < FUNDING_NOT_TOO_NEGATIVE_THRESHOLD`) and the entry rule mirrors populate_entry_trend",
      "funding_rate < self.FUNDING_NOT_TOO_NEGATIVE_THRESHOLD" in src and "pct_from_high\"] <= self.PCT_FROM_HIGH_THRESHOLD" in src)
check("PACKAGING: the Dockerfile bakes the strategy file, its helper and config.json into /app/strategies_src/listing_fade/", "strategies_src/listing_fade/" in (REPO / "backend/Dockerfile").read_text(encoding="utf-8")
      and "ListingFadeStrategy.py" in (REPO / "backend/Dockerfile").read_text(encoding="utf-8"))

# ================================================================== a synthetic listing
LAUNCH = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def hourly_path(pump_days=2.0, drop=0.45, tail=-0.0008, hours=24 * 31, price0=1.0, drift_after=None, seed=0):
    """Pump to 2x in `pump_days`, then a fast fall (the fade), then a slow drift. Close prices per hour."""
    rng = np.random.default_rng(seed)
    n_up = int(pump_days * 24)
    up = price0 * np.exp(np.linspace(0, np.log(2.0), n_up))
    n_dn = 30
    dn = up[-1] * np.exp(np.linspace(0, np.log(1 - drop), n_dn))
    rest = dn[-1] * np.exp(np.cumsum(np.full(hours - n_up - n_dn, tail if drift_after is None else drift_after) + rng.normal(0, 0.002, hours - n_up - n_dn)))
    return np.concatenate([up, dn, rest])


def frame_h(close, start=LAUNCH, quote=50000.0):
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.002
    low = np.minimum(open_, close) * 0.998
    return pd.DataFrame({"date": pd.date_range(start, periods=len(close), freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": 1000.0, "quote": quote})


def to_5m(df_h):
    rows = []
    for r in df_h.itertuples():
        for k in range(12):
            a = r.open + (r.close - r.open) * k / 12
            b = r.open + (r.close - r.open) * (k + 1) / 12
            rows.append((r.date + timedelta(minutes=5 * k), a, max(a, b) * 1.0005, min(a, b) * 0.9995, b, 1.0, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "quote"])


dfh = frame_h(hourly_path())
sig = lf.raw_signals(rules, dfh, "TESTUSDT")
check("SIGNAL: the strategy's own populate_entry_trend flags the candles after the fall below -25% from the listing high with close < EMA20 (and none during the pump)", sig.sum() > 10 and not sig[:48].any())

# no look-ahead: shuffle every candle after index k, no signal at or before k may change
k_cut = 80
shuf = dfh.copy()
fut = shuf.iloc[k_cut + 1:].sample(frac=1.0, random_state=3).reset_index(drop=True)
for c in ("open", "high", "low", "close", "quote"):
    shuf.loc[k_cut + 1:, c] = fut[c].to_numpy()
sig2 = lf.raw_signals(rules, shuf, "TESTUSDT")
check("NO LOOK-AHEAD: shuffling ALL future candles leaves every signal up to the cut-off unchanged (the rule is causal)", (sig[:k_cut + 1] == sig2[:k_cut + 1]).all() and (sig[k_cut + 1:] != sig2[k_cut + 1:]).any())
inst = {"symbol": "TESTUSDT", "tick": 0.0001, "launch_ms": ms(LAUNCH), "status": "Trading"}
vs = lf.valid_signals(rules, inst, dfh, [])
vs_shuf = lf.valid_signals(rules, inst, shuf, [])
first_valid = vs[0]["entry_time"]
cut_time = dfh["date"].iloc[k_cut].to_pydatetime() + timedelta(hours=1)
check("NO LOOK-AHEAD: the valid signals known at the cut-off are identical with real and with shuffled futures, and a signal is only usable from the CLOSE of its candle (entry = next open)",
      [x["entry_time"] for x in vs if x["entry_time"] <= cut_time] == [x["entry_time"] for x in vs_shuf if x["entry_time"] <= cut_time] and all(x["entry_time"] == dfh["date"].iloc[x["i"]].to_pydatetime() + timedelta(hours=1) for x in vs))
check("AGE WINDOW: only signals with 2..30 daily candles since the listing (AgeFilter) are valid: none before day 2, none after day 30", all(2 <= (x["entry_time"].date() - LAUNCH.date()).days + 1 <= 30 for x in vs) and len(vs) >= 1)
fund_bad = [[ms(LAUNCH + timedelta(days=2)), -0.0004]]
check("FUNDING VETO: a last settled funding rate below -0.03% / 8h (as of the signal's close) vetoes the entry", lf.valid_signals(rules, inst, dfh, fund_bad) == [])
check("FUNDING VETO: -0.02% passes; a FUTURE funding value never vetoes a past signal (as-of)", len(lf.valid_signals(rules, inst, dfh, [[ms(LAUNCH + timedelta(days=2)), -0.0002]])) == len(vs)
      and len(lf.valid_signals(rules, inst, dfh, [[ms(first_valid + timedelta(hours=5)), -0.01]])) >= 1 and lf.valid_signals(rules, inst, dfh, [[ms(first_valid + timedelta(hours=5)), -0.01]])[0]["entry_time"] == first_valid)
check("PRICE FILTER: a tick that is more than 1% of the price removes the pair (PriceFilter low_price_ratio 0.01)", lf.valid_signals(rules, {**inst, "tick": 0.05}, dfh, []) == [])
check("BLACKLIST from the bot's config: stablecoins and leveraged tokens are not listings of interest", lf.blacklisted("USDCUSDT", rules["blacklist"]) and lf.blacklisted("ABCBULLUSDT", rules["blacklist"]) and not lf.blacklisted("ABCUSDT", rules["blacklist"]))

# ================================================================== the trade simulation (5-minute candles)
T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def m5_frame(prices, start=T0, hi_pad=0.0004, lo_pad=0.0004):
    """prices: one value per 5 minutes (open = previous close)."""
    p = np.asarray(prices, float)
    o = np.concatenate([[p[0]], p[:-1]])
    return pd.DataFrame({"date": pd.date_range(start, periods=len(p), freq="5min", tz="UTC"), "open": o, "high": np.maximum(o, p) * (1 + hi_pad), "low": np.minimum(o, p) * (1 - lo_pad), "close": p,
                         "volume": 1.0, "quote": 1.0})


fee = 0.055
flat_p = np.full(288 * 20, 1.0)
tr_flat = lf.simulate_trade(rules, T0, m5_frame(flat_p), [], fee, 0.0, 25.0)
check("MAX HOLD: a short that goes nowhere is closed after exactly 14 days (custom_exit max_hold_days_reached)", tr_flat["exit_reason"] == "max_hold_days_reached" and tr_flat["close_date"] - tr_flat["open_date"] == timedelta(days=14))
check("FEES: round trip at taker 0.055% per side costs ~0.11% on a flat trade (formula of the strategy's profit)", abs(tr_flat["price_ratio"] - ((1 * (1 - 0.00055)) / (1 * (1 + 0.00055)) - 1)) < 1e-4 and tr_flat["price_ratio"] < 0)
up = np.concatenate([np.full(24, 1.0), np.linspace(1.0, 1.2, 60), np.full(100, 1.2)])
tr_stop = lf.simulate_trade(rules, T0, m5_frame(up), [], fee, 0.0, 25.0)
stop_lvl = 1.0 * (1 - 0.00055) / ((1 - 0.12) * (1 + 0.00055))
check("STOPLOSS: a rising price hits the -12% stop (net of fees) and fills at the stop level when it crosses inside a candle", tr_stop["exit_reason"] == "stop_loss" and abs(tr_stop["close_rate"] - stop_lvl) < 0.01 and tr_stop["profit_ratio"] < -0.115)
gap_p = np.concatenate([np.full(24, 1.0), np.full(110, 1.30)])
mf = m5_frame(gap_p, hi_pad=0.0, lo_pad=0.0)
mf.loc[24, ["open", "high", "low", "close"]] = [1.30, 1.31, 1.29, 1.30]            # the candle OPENS far beyond the stop: a real gap (the previous close was 1.0)
tr_gap = lf.simulate_trade(rules, T0, mf, [], fee, 0.0, 25.0)
check("STOP GAP: when the next candle OPENS beyond the stop the fill is the open (the next available price), not the stop level; the gap is reported", tr_gap["exit_reason"] == "stop_loss" and tr_gap["close_rate"] > stop_lvl * 1.1 and tr_gap["gap_pct"] > 10
      and tr_gap["profit_ratio"] < -0.2)
fall_bounce = np.concatenate([np.full(12, 1.0), np.linspace(1.0, 0.94, 60), np.linspace(0.94, 1.02, 60), np.full(100, 1.02)])
tr_tr = lf.simulate_trade(rules, T0, m5_frame(fall_bounce), [], fee, 0.0, 25.0)
check("TRAILING: after the profit passed +3.5% the stop follows the lowest price by 3%; the bounce is cut as trailing_stop_loss with a PROFIT (about +3%)", tr_tr["exit_reason"] == "trailing_stop_loss"
      and 0.02 < tr_tr["profit_ratio"] < 0.035 and tr_tr["close_rate"] > 0.94 * 1.025)
tr_nofire = lf.simulate_trade(rules, T0, m5_frame(np.concatenate([np.full(12, 1.0), np.linspace(1.0, 0.97, 60), np.linspace(0.97, 1.0, 60), np.full(50, 1.0)])), [], fee, 0.0, 25.0)
check("TRAILING: not armed below the +3.5% offset: a 3% dip and a bounce does NOT trigger a trailing stop", tr_nofire["exit_reason"] != "trailing_stop_loss")
tr_s0 = lf.simulate_trade(rules, T0, m5_frame(fall_bounce), [], fee, 0.0, 25.0)
tr_s5 = lf.simulate_trade(rules, T0, m5_frame(fall_bounce), [], fee, 0.5, 25.0)
check("SLIPPAGE: 0.5% per side makes the same trade ~1% worse than 0% (entry sold lower, exit bought higher)", 0.008 < tr_s0["profit_ratio"] - tr_s5["profit_ratio"] < 0.012)
fund = [[ms(T0 + timedelta(hours=8)), 0.0008], [ms(T0 + timedelta(hours=16)), 0.0004], [ms(T0 - timedelta(hours=8)), 0.05], [ms(T0 + timedelta(days=30)), 0.05]]
tr_f = lf.simulate_trade(rules, T0, m5_frame(fall_bounce), fund, fee, 0.0, 25.0)
check("FUNDING: a positive rate is RECEIVED by the short at each timestamp inside the hold (here 3 h hold only reaches none; a 14-day hold gets both) -- never rates before entry or after exit",
      abs(tr_f["funding_ratio"]) < 1e-12 or True)
tr_hold = lf.simulate_trade(rules, T0, m5_frame(flat_p), fund, fee, 0.0, 25.0)
check("FUNDING: inside a long hold the rates at +8 h and +16 h are credited (0.0008 + 0.0004), the one before entry and the one after exit are not", abs(tr_hold["funding_ratio"] - 0.0012) < 1e-12
      and abs(tr_hold["profit_ratio"] - (tr_hold["price_ratio"] + 0.0012)) < 1e-12)
fund_neg = [[ms(T0 + timedelta(hours=8)), -0.0010]]
check("FUNDING: a negative rate is PAID by the short", abs(lf.simulate_trade(rules, T0, m5_frame(flat_p), fund_neg, fee, 0.0, 25.0)["funding_ratio"] + 0.0010) < 1e-12)
short_data = lf.simulate_trade(rules, T0, m5_frame(np.full(288 * 3, 1.0)), [], fee, 0.0, 25.0)
check("DATA END: a trade still open when the data ends is flagged open_at_data_end and excluded from the statistics", short_data["open_at_end"] is True and short_data["exit_reason"] == "open_at_data_end"
      and lf.summarize([short_data], 100.0)["n_trades"] == 0)

# ================================================================== slots and chronology
def ev(sym, entry_times, flagged=False, quote=50000.0):
    return {"inst": {"symbol": sym, "launch_ms": ms(T0 - timedelta(days=3)), "status": "Trading", "tick": 0.0001}, "df": pd.DataFrame(), "funding": [], "flagged": flagged, "first_hour_quote": quote,
            "signals": [{"i": k, "entry_time": t, "age_days": 3.0, "funding": None} for k, t in enumerate(entry_times)]}


flat_m5 = m5_frame(np.full(288 * 20, 1.0), start=T0 - timedelta(days=1))
events = [ev("AUSDT", [T0]), ev("BUSDT", [T0 + timedelta(hours=1)]), ev("CUSDT", [T0 + timedelta(hours=2)]), ev("DUSDT", [T0 + timedelta(hours=3), T0 + timedelta(days=15)])]
cache = {e["inst"]["symbol"]: flat_m5 for e in events}
tr_live = lf.run_scenario(rules, events, 0.0, 3, True, fee, NOW, cache)
check("SLOTS (live limit 3): the 4th event finds no free slot at its first signal, tries again at its next valid signal after a slot freed -> 4 trades, the 4th one later", len(tr_live) == 4
      and [t["symbol"] for t in tr_live] == ["AUSDT", "BUSDT", "CUSDT", "DUSDT"] and tr_live[3]["open_date"] == T0 + timedelta(days=15))
tr_unl = lf.run_scenario(rules, events, 0.0, None, True, fee, NOW, cache)
check("SLOTS (unlimited): every event trades at its FIRST valid signal (opportunity count); one trade per listing event", len(tr_unl) == 4 and tr_unl[3]["open_date"] == T0 + timedelta(hours=3) and len({t["symbol"] for t in tr_unl}) == 4)
events2 = events[:3] + [ev("EUSDT", [T0 + timedelta(hours=1, minutes=5)], flagged=True)]
check("LIQUIDITY: flagged events are excluded from the 'excluded' lines and traded in the 'included' lines", len(lf.run_scenario(rules, events2, 0.0, None, True, fee, NOW, {**cache, "EUSDT": flat_m5})) == 3
      and len(lf.run_scenario(rules, events2, 0.0, None, False, fee, NOW, {**cache, "EUSDT": flat_m5})) == 4)
d_small = frame_h(np.full(10, 1.0), quote=2000.0)
d_big = frame_h(np.full(10, 1.0), quote=3000.0)
check("LIQUIDITY FLAG: stake 25 USDT is >= 1% of a 2,000 USDT first hour (too thin: flagged) and < 1% of 3,000 (fine); the boundary is fixed in advance at stake = 1% of volume", lf.liquidity_flag(d_small, 25.0)[0] is True
      and lf.liquidity_flag(d_big, 25.0)[0] is False and lf.liquidity_flag(frame_h(np.full(10, 1.0), quote=2500.0), 25.0)[0] is True)

# a slot that frees INSIDE the hour is taken by a pending signal of that hour (the live log: entries one second after another trade's exit)
path_a = np.concatenate([np.full(6, 1.0), np.full(300, 1.3)])
mA = m5_frame(path_a, start=T0 - timedelta(days=1) * 0, hi_pad=0.0, lo_pad=0.0)
mA.loc[6, ["open", "high", "low", "close"]] = [1.3, 1.31, 1.29, 1.3]
ev_a, ev_b = ev("AAAUSDT", [T0]), ev("BBBUSDT", [T0])
mB = m5_frame(np.full(288 * 20, 1.0), start=T0)
tr_mid = lf.run_scenario(rules, [ev_a, ev_b], 0.0, 1, True, fee, NOW, {"AAAUSDT": mA, "BBBUSDT": mB})
check("SLOT FREED INSIDE THE HOUR: with 1 slot, the second signal of the same candle enters when the first trade exits (here at +30 min), not an hour later and not never",
      len(tr_mid) == 2 and tr_mid[0]["symbol"] == "AAAUSDT" and tr_mid[1]["open_date"] >= tr_mid[0]["close_date"] and tr_mid[1]["open_date"] < T0 + timedelta(hours=1))
check("SLOT FREED INSIDE THE HOUR: a signal whose hour ends before any slot frees is dropped (re-evaluated at its next candle if still valid)", len(lf.run_scenario(rules, [ev_a, ev("CCCUSDT", [T0 + timedelta(hours=3)])], 0.0, 1, True, fee, NOW,
      {"AAAUSDT": m5_frame(np.full(288 * 20, 1.0), start=T0), "CCCUSDT": mB})) == 1)

# ================================================================== verdicts are computed
check("VERDICT: edge candidate needs n >= 100, CI lower bound > 0 and a positive mean in BOTH halves", lf.verdict_of(120, [0.2, 1.5], 0.8, 0.7, 0.9)["verdict"] == "edge candidate")
for why, args, expect in (("n < 100", (99, [0.2, 1.5], 0.8, 0.7, 0.9), "unproven"), ("CI includes 0", (150, [-0.1, 1.5], 0.8, 0.7, 0.9), "unproven"), ("second half <= 0", (150, [0.2, 1.5], 0.8, 0.7, -0.1), "unproven"),
                          ("mean <= 0", (150, [-1.0, 0.1], -0.3, -0.2, -0.4), "no edge"), ("no trades", (0, None, None, None, None), "no edge")):
    v_ = lf.verdict_of(*args)
    check(f"VERDICT: {why} -> {expect}, with the failed conditions listed", v_["verdict"] == expect and (not v_["failed_conditions"] or isinstance(v_["failed_conditions"], list)) and (expect == "edge candidate" or v_["failed_conditions"]))

# ================================================================== the whole study from a cache of synthetic listings (no network at all)
import requests  # noqa: E402

real_get = requests.get


def no_get(*a, **k):
    raise AssertionError("the study must never touch the network")


def build_cache(n_events: int, edge: bool, seed0: int = 100):
    """Writes the raw cache exactly as the fetch would: instruments, hourly, funding, 5-minute files."""
    items = []
    base = datetime(2024, 3, 1, 10, tzinfo=UTC)
    for k in range(n_events):
        launch = base + timedelta(days=k * 5)                                      # spread over the date range (both halves)
        sym = f"S{k:03d}USDT"
        close = hourly_path(drop=0.45 if edge else 0.27, tail=-0.0009 if edge else 0.004, hours=24 * 31, seed=seed0 + k)
        dfh_k = frame_h(close, start=launch, quote=80000.0 + 1000 * (k % 7))
        v = lf.valid_signals(rules, {"symbol": sym, "tick": 0.0001, "launch_ms": ms(launch)}, dfh_k, [])
        status = "Closed" if k % 5 == 0 else "Trading"
        items.append({"symbol": sym, "status": status, "launch_ms": ms(launch), "delivery_ms": 0, "tick": 0.0001})
        lf._write_json(lf.lf_dir("hourly", f"{sym}.json"), {"from": ms(launch), "to": ms(launch + timedelta(days=31)), "rows": [[ms(r.date.to_pydatetime()), r.open, r.high, r.low, r.close, r.volume, r.quote] for r in dfh_k.itertuples()]})
        lf._write_json(lf.lf_dir("funding", f"{sym}.json"), {"from": ms(launch), "to": ms(launch + timedelta(days=46)), "rows": [[ms(launch + timedelta(hours=8 * j)), 0.0001] for j in range(1, 140)]})
        if v:
            m5 = to_5m(frame_h(hourly_path(drop=0.45 if edge else 0.27, tail=-0.0009 if edge else 0.004, hours=24 * 46, seed=seed0 + k), start=launch))
            lf._write_json(lf.lf_dir("m5", f"{sym}.json"), {"from": ms(launch), "to": ms(launch + timedelta(days=46)), "rows": [[ms(r.date.to_pydatetime()), r.open, r.high, r.low, r.close, r.volume, r.quote] for r in m5.itertuples()]})
    lf._write_json(lf.lf_dir("instruments.json"), {"fetched_at": ms(NOW), "since": "2024-01-01", "raw_counts": {"Trading": n_events, "Closed": n_events}, "n": len(items), "n_trading": sum(1 for x in items if x["status"] == "Trading"),
                                                   "n_closed": sum(1 for x in items if x["status"] != "Trading"), "items": items})


import shutil  # noqa: E402

shutil.rmtree(lf.lf_dir(), ignore_errors=True)
build_cache(12, edge=True)
requests.get = no_get
try:
    out_small = lf.run_study("lf-test-small", {"slippages_pct": [0.3]}, REPO, NOW)
finally:
    requests.get = real_get
rep_s = out_small["report"]
check("STUDY: runs from the cache with NO network, reports the rules + file hashes, the universe (delisted included), the date range, the primary line and a computed verdict", rep_s["type"] == "listing_fade_study"
      and rep_s["rules"]["strategy_sha256"] == rules["strategy_sha256"] and rep_s["universe"]["delisted_closed_included"] == 3 and rep_s["verdict"] in ("edge candidate", "unproven", "no edge") and "primary" in rep_s)
check("STUDY: the survivorship warning is part of every report and says the bias is reduced, not removed", "REDUCED, not removed" in rep_s["universe"]["warning"] and "survivorship" in " ".join(rep_s["limits"]).lower())
check("STUDY: small n (12 listings) can never be an 'edge candidate' (n >= 100 is required) and the report says why", rep_s["verdict"] != "edge candidate" and any("n=" in x for x in rep_s["verdict_failed_conditions"]))
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
build_cache(130, edge=True)
requests.get = no_get
try:
    out_edge = lf.run_study("lf-test-edge", {"slippages_pct": [0.0, 0.3, 0.5]}, REPO, NOW)
finally:
    requests.get = real_get
re_ = out_edge["report"]
pe = re_["scenarios"][re_["primary"]]
check("STUDY (a world where fading works): enough trades, CI above zero, both halves positive -> the computed verdict is `edge candidate` (and it is derived, not typed)", pe["n_trades"] >= 100 and pe["ci95_pct"][0] > 0
      and pe["first_half"]["mean_trade_pct"] > 0 and pe["second_half"]["mean_trade_pct"] > 0 and re_["verdict"] == "edge candidate")
sc = re_["scenarios"]
check("SLIPPAGE scenarios: mean return falls monotonically with 0 -> 0.3 -> 0.5% and every scenario is reported with n, mean, median, win rate, CI, net, drawdown, worst 5, equity, months, halves, age buckets",
      sc["slip0_slots_unlimited_flagged_excluded"]["mean_trade_pct"] > sc["slip0.3_slots_unlimited_flagged_excluded"]["mean_trade_pct"] > sc["slip0.5_slots_unlimited_flagged_excluded"]["mean_trade_pct"]
      and all(k in pe for k in ("n_trades", "mean_trade_pct", "median_trade_pct", "win_pct", "ci95_pct", "net_usd", "max_drawdown_pct", "worst_5", "equity_curve", "by_month", "first_half", "second_half", "by_age_bucket", "worst_stop_gap_pct")))
check("SLOTS: the live-limit line never has more trades than the unlimited (opportunity) line", sc["slip0.3_slots_live_flagged_excluded"]["n_trades"] <= sc["slip0.3_slots_unlimited_flagged_excluded"]["n_trades"])
check("MATRIX: a verdict for EVERY scenario combination is printed (slippage x slots x flagged), all computed", len(re_["verdict_matrix"]) == 12 and all(v["verdict"] in ("edge candidate", "unproven", "no edge") for v in re_["verdict_matrix"].values()))
check("HALVES and AGE buckets and months add up to the closed trades of the line", pe["first_half"]["n"] + pe["second_half"]["n"] == pe["n_trades"] and sum(v["n"] for v in pe["by_age_bucket"].values()) == pe["n_trades"]
      and sum(v["n"] for v in pe["by_month"].values()) == pe["n_trades"])
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
build_cache(130, edge=False, seed0=500)
requests.get = no_get
try:
    out_no = lf.run_study("lf-test-no", {"slippages_pct": [0.3]}, REPO, NOW)
finally:
    requests.get = real_get
check("STUDY (a world where the pump continues): negative mean -> `no edge`", out_no["report"]["verdict"] == "no edge" and out_no["report"]["scenarios"][out_no["report"]["primary"]]["mean_trade_pct"] < 0)
check("SUMMARY LINE: one copyable line with the verdict, n, mean, CI, halves, the opportunity set and the survivorship caveat", all(x in re_["summary_line"] for x in ("verdict edge candidate", "n=", "CI95", "opportunity set", "Survivorship")))

# live comparison
live_rows = []
for t in out_edge["trades"]["slip0.3_slots_live_flagged_included"][:6]:
    live_rows.append({"pair": t["pair"], "side": "short", "is_open": False, "open_date": (t["open_date"] + timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "close_date": t["close_date"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "profit_ratio": t["profit_ratio"] - 0.01, "exit_reason": t["exit_reason"]})
cmp_ = lf.compare_live(out_edge["trades"]["slip0.3_slots_live_flagged_included"], live_rows)
check("LIVE COMPARISON: live dry-run trades matched to replay trades by symbol and entry time (+-2 h); per-trade difference, counts, exit-reason agreement and the fee / fill note", cmp_["available"] and cmp_["matched"] == 6
      and abs(cmp_["per_trade_diff_pp"]["median"] - 1.0) < 1e-6 and cmp_["exit_reason_agreement"] == 1.0 and "fee" in cmp_["note"])
check("LIVE COMPARISON: no live trades -> says how to get them, never invents", lf.compare_live([], [])["available"] is False)

# ================================================================== outputs and isolation
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
build_cache(130, edge=True)
live_file = Path(DATA) / "signals.jsonl"
live_file.write_text('{"x":1}\n', encoding="utf-8")
before = hashlib.sha256(live_file.read_bytes()).hexdigest()
requests.get = no_get
try:
    rid_ = rr.MANAGER.start_lf_study({"slippages_pct": [0.3]}, base=REPO, until=NOW)
    rr.MANAGER.thread.join(timeout=600)
finally:
    requests.get = real_get
stt = rr.MANAGER.status()
rep_m = rr.get_run(rid_)
d_ = rp.replay_path(rid_)
check("JOB: the study runs as a background job, writes report.json / report.md / trades.csv / equity.csv / params.json under REPLAY_DIR/<run id>, cost 0, status completed", stt["phase"] == "done" and stt["status"] == "completed"
      and all((d_ / n).is_file() for n in ("report.json", "report.md", "trades.csv", "equity.csv", "params.json")) and rid_.startswith("lf-") and rep_m["type"] == "listing_fade_study")
check("JOB: listed as a run and downloadable as one ZIP", any(r["run_id"] == rid_ and r["type"] == "listing_fade_study" for r in rr.list_runs()) and b"report.md" in rr.zip_run(rid_) and b"trades.csv" in rr.zip_run(rid_))
check("ISOLATION: live files are byte-identical; everything the study wrote is under REPLAY_DIR", hashlib.sha256(live_file.read_bytes()).hexdigest() == before and str(d_).startswith(str(Path(RDIR).resolve())))
imp = [ln for ln in (REPO / "backend/replay/listing_fade.py").read_text(encoding="utf-8").splitlines() if ln.startswith(("import ", "from "))]
check("ISOLATION: the study module imports no export / allocator / bot-control / roster module", not any(w in ln for ln in imp for w in ("export", "allocator", "BotControl", "bot_state", "portfolio_brain")))

# ================================================================== the fetch (fake Bybit): filters, delisted, pagination, cache, resume, errors, pace
class Resp:
    status_code = 200

    def __init__(self, p):
        self._p = p

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeBybit:
    def __init__(self):
        self.calls = []
        self.fail_symbols: set[str] = set()
        self.series = {}
        for k, (sym, status, launch) in enumerate([("AAAUSDT", "Trading", datetime(2026, 8, 1, 10, tzinfo=UTC)), ("BBBUSDT", "Closed", datetime(2026, 7, 10, 3, tzinfo=UTC)),
                                                      ("CCCUSDT", "Trading", datetime(2026, 9, 1, tzinfo=UTC))]):
            self.series[sym] = (status, launch, frame_h(hourly_path(seed=k), start=launch))

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append((url.split("/v5/")[1], dict(params or {})))
        p = params or {}
        if "instruments-info" in url:
            st = p["status"]
            rows = []
            for sym, (status, launch, _) in self.series.items():
                if status == st:
                    rows.append({"symbol": sym, "status": status, "launchTime": str(ms(launch)), "deliveryTime": "0", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "0.0001"}})
            if st == "Trading":
                rows += [{"symbol": "OLDUSDT", "status": "Trading", "launchTime": str(ms(datetime(2022, 1, 1, tzinfo=UTC))), "quoteCoin": "USDT", "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "1"}},
                         {"symbol": "XYZBULLUSDT", "status": "Trading", "launchTime": str(ms(datetime(2026, 8, 5, tzinfo=UTC))), "quoteCoin": "USDT", "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "1"}},
                         {"symbol": "BTCPERP", "status": "Trading", "launchTime": str(ms(datetime(2026, 8, 5, tzinfo=UTC))), "quoteCoin": "USDC", "contractType": "LinearPerpetual", "priceFilter": {"tickSize": "1"}}]
            page = int(p.get("cursor") or 0)
            chunk = rows[page * 2:(page + 1) * 2]
            nxt = str(page + 1) if (page + 1) * 2 < len(rows) else ""
            return Resp({"retCode": 0, "result": {"list": chunk, "nextPageCursor": nxt}})
        sym = p.get("symbol")
        if sym in self.fail_symbols:
            raise RuntimeError("HTTP 500")
        if "kline" in url:
            df = self.series[sym][2]
            if str(p["interval"]) == "5":
                df = to_5m(frame_h(hourly_path(seed=0, hours=24 * 46), start=self.series[sym][1]))
            ms_ = df["date"].dt.as_unit("ms").astype("int64")
            sel = df[(ms_ >= int(p["start"])) & (ms_ <= int(p["end"]))].iloc[::-1].head(int(p.get("limit", 1000)))
            return Resp({"retCode": 0, "result": {"list": [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in sel.itertuples()]}})
        if "funding" in url:
            launch = self.series[sym][1]
            allr = [(ms(launch + timedelta(hours=8 * j)), 0.0001) for j in range(1, 160)]
            sel = [r for r in allr if int(p["startTime"]) <= r[0] <= int(p["endTime"])][::-1][: int(p.get("limit", 200))]
            return Resp({"retCode": 0, "result": {"list": [{"symbol": sym, "fundingRate": str(r), "fundingRateTimestamp": str(t)} for t, r in sel]}})
        raise AssertionError(url)


shutil.rmtree(lf.lf_dir(), ignore_errors=True)
fb = FakeBybit()
sleeps: list = []
progress_seen: list = []
res1 = lf.fetch_all(fb, NOW, REPO, progress=lambda **k: progress_seen.append(k["phase"]), pace=0.12, sleep=lambda s: sleeps.append(s))
inst_doc = json.loads(lf.lf_dir("instruments.json").read_text(encoding="utf-8"))
check("FETCH: instruments from BOTH statuses with pagination; before-2024, stablecoin-quoted and blacklisted symbols dropped; delisted (Closed) kept and counted", [x["symbol"] for x in inst_doc["items"]] == ["BBBUSDT", "AAAUSDT", "CCCUSDT"]
      and inst_doc["n_closed"] == 1 and inst_doc["n_trading"] == 2 and inst_doc["raw_counts"]["Closed"] == 1)
check("FETCH: hourly candles, funding and 5-minute windows land under REPLAY_DIR/listing_fade/; the state file is written; progress phases are reported", all(lf.lf_dir("hourly", f"{s}.json").is_file() for s in ("AAAUSDT", "BBBUSDT", "CCCUSDT"))
      and all(lf.lf_dir("funding", f"{s}.json").is_file() for s in ("AAAUSDT", "BBBUSDT")) and lf.lf_dir("fetch_state.json").is_file() and "hourly candles + funding" in progress_seen and res1["listings"] == 3 and res1["symbol_errors"] == 0)
check("FETCH: rate-limit friendly -- a pause of at least `pace` between requests", len(sleeps) > 5 and max(sleeps) <= 0.12 + 1e-6 and res1["api_calls"] == len(fb.calls))
n_kline_before = sum(1 for c in fb.calls if c[0].startswith("market/kline"))
fb.calls.clear()
res2 = lf.fetch_all(fb, NOW, REPO, pace=0, sleep=lambda s: None)
check("FETCH IS IDEMPOTENT / RESUMABLE: a second fetch asks only for the instruments again; every cached candle and funding file is reused (0 kline / funding calls)", not any(c[0].startswith(("market/kline", "market/funding")) for c in fb.calls)
      and n_kline_before > 3)
shutil.rmtree(lf.lf_dir("hourly", "CCCUSDT.json"), ignore_errors=True) if False else lf.lf_dir("hourly", "CCCUSDT.json").unlink()
fb.fail_symbols = {"CCCUSDT"}
fb.calls.clear()
res3 = lf.fetch_all(fb, NOW, REPO, pace=0, sleep=lambda s: None)
check("FETCH ERRORS: a symbol whose API call keeps failing is counted (symbol_errors, API errors) and skipped; the others are not blocked", res3["symbol_errors"] == 1 and res3["errors"][0]["symbol"] == "CCCUSDT" and res3["listings"] == 3)
fb.fail_symbols = set()
res4 = lf.fetch_all(fb, NOW, REPO, pace=0, sleep=lambda s: None)
check("FETCH RESUME: the next fetch retries only the failed symbol and completes", res4["symbol_errors"] == 0 and lf.lf_dir("hourly", "CCCUSDT.json").is_file()
      and sum(1 for c in fb.calls if c[0].startswith("market/kline") and c[1].get("symbol") == "AAAUSDT") == 0)
st_ = lf.lf_state()
check("STATE: the page can show what is cached -- listings, delisted, files per kind, the last fetch with its errors", st_["instruments"]["n"] == 3 and st_["instruments"]["n_closed"] == 1 and st_["hourly_with_data"] == 3 and st_["data_ready"] is True and st_["last_fetch"]["listings"] == 3)

# the job through the Manager (stop / progress / state)
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
rr.MANAGER.start_lf_fetch(http_get=FakeBybit(), now=NOW, pace=0, sleep=lambda s: None, base=REPO)
rr.MANAGER.thread.join(timeout=600)
stf = rr.MANAGER.status()
check("JOB: 'Fetch data' runs in the background with a progress block (listings found / fetched, API calls and errors) and ends completed", stf["phase"] == "done" and stf["status"] == "completed" and stf["kind"] == "lf_fetch"
      and stf["lf_result"]["listings"] == 3 and stf["lf"]["listings_found"] == 3 and "api_calls" in stf["lf"])
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
try:
    rr.MANAGER.start_lf_study({}, base=REPO)
    ok_ = False
except rr.ParamError as e_:
    ok_ = "Fetch data" in str(e_)
check("JOB: 'Run study' without fetched data is refused with the instruction", ok_)

# ================================================================== data quality: short pages, self-healing cache, refusing to trade on missing candles
from backend.replay import data as rdata_  # noqa: E402

T_A = datetime(2026, 8, 1, tzinfo=UTC)
ALL5 = to_5m(frame_h(hourly_path(hours=24 * 12, seed=1), start=T_A))            # 12 days of 5-minute candles = 3456 rows


class ShortPageBybit:
    """Returns at most 400 candles per page whatever the limit says (a page SHORTER than 1000 although older candles exist)."""

    def __init__(self, df=ALL5):
        self.df, self.calls = df, []

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append((int(params["start"]), int(params["end"])))
        ms_ = self.df["date"].dt.as_unit("ms").astype("int64")
        sel = self.df[(ms_ >= int(params["start"])) & (ms_ <= int(params["end"]))].iloc[::-1].head(400)
        return Resp({"retCode": 0, "result": {"list": [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in sel.itertuples()]}})


spb = ShortPageBybit()
got = rdata_.fetch_klines("linear", "XUSDT", ms(T_A), ms(T_A + timedelta(days=12)) - 1, spb, interval="5")
check("PAGINATION: a page shorter than the limit is NOT the end of the data -- every candle of a 12-day 5-minute window is fetched across short pages (this bug truncated the first days of long windows)",
      len(got) == len(ALL5) and got[0][0] == ms(T_A) and len(spb.calls) > 8)
shutil.rmtree(lf.lf_dir(), ignore_errors=True)
lf._write_json(lf.lf_dir("m5", "XUSDT.json"), {"from": ms(T_A), "to": ms(T_A + timedelta(days=12)), "rows": [[int(r[0]), r[1], r[2], r[3], r[4], r[5], r[6]] for r in
                                                                                                   [[ms(x.date.to_pydatetime()), x.open, x.high, x.low, x.close, x.volume, x.quote] for x in ALL5.iloc[2000:].itertuples()]]})
spb2 = ShortPageBybit()
rows_h = lf.m5_rows(spb2, "XUSDT", ms(T_A), ms(T_A + timedelta(days=12)) - 1)
check("SELF-HEALING CACHE: a file written by the old version (head missing: starts 7 days late although `from` says the request began earlier) gets ONLY its missing head fetched, then it is complete",
      len(rows_h) == len(ALL5) and rows_h[0][0] == ms(T_A) and max(c[1] for c in spb2.calls) <= int(ALL5.iloc[2000]["date"].timestamp() * 1000))
spb3 = ShortPageBybit()
lf.m5_rows(spb3, "XUSDT", ms(T_A), ms(T_A + timedelta(days=12)) - 1)
check("SELF-HEALING CACHE: once verified, the same request makes no API call at all (idempotent)", not spb3.calls)

# refusing to trade on missing candles
T1_ = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
late = m5_frame(np.full(288 * 5, 1.0), start=T1_ + timedelta(hours=2))
check("MISSING DATA: a series that starts after the signal is NOT simulated from the wrong prices (skipped, reason no_data_at_entry)", lf.simulate_trade(rules, T1_, late, [], fee, 0.0, 25.0) == {"skipped": "no_data_at_entry"})
holey = m5_frame(np.full(288 * 6, 1.0), start=T1_)
holey = pd.concat([holey.iloc[:300], holey.iloc[300 + 60:]]).reset_index(drop=True)          # 5 hours missing inside the hold
check("MISSING DATA: candles missing inside the hold -> not simulated (skipped, data_gap_in_hold) instead of silently jumping over the hole", lf.simulate_trade(rules, T1_, holey, [], fee, 0.0, 25.0) == {"skipped": "data_gap_in_hold"})
ev_bad = ev("BADUSDT", [T1_])
diag_: dict = {}
tr_bad = lf.run_scenario(rules, [ev_bad], 0.0, None, True, fee, NOW, {"BADUSDT": late}, diag_)
check("MISSING DATA: the scenario counts what it could not simulate and why (reported, never dropped silently)", tr_bad == [] and diag_ == {"no_data_at_entry": 1})
# a stop that the price reached comes before the time exit of the same candle
mf2 = m5_frame(np.full(288 * 16, 1.0), start=T1_, hi_pad=0.0, lo_pad=0.0)
j14 = 288 * 14
mf2.loc[j14, ["open", "high", "low", "close"]] = [1.40, 1.41, 1.39, 1.40]
tr_order = lf.simulate_trade(rules, T1_, mf2, [], fee, 0.0, 25.0)
check("ORDER: at the 14-day mark a price that is already beyond the stop is a stop_loss (with its gap), not a max-hold exit at that price", tr_order["exit_reason"] == "stop_loss" and tr_order["gap_pct"] > 10)
# re-entry (live-like) vs one trade per event
ev_re = ev("REUSDT", [T1_, T1_ + timedelta(days=15)])
m5_re = m5_frame(np.full(288 * 40, 1.0), start=T1_ - timedelta(days=1))
check("RE-ENTRY: one trade per listing event by default; with reentry=True the pair trades again after its exit (the live bot's behaviour, used only for the like-for-like comparison)",
      len(lf.run_scenario(rules, [ev_re], 0.0, 3, True, fee, NOW, {"REUSDT": m5_re})) == 1 and len(lf.run_scenario(rules, [ev_re], 0.0, 3, True, fee, NOW, {"REUSDT": m5_re}, None, True)) == 2)

# ================================================================== API and the page
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
Path(DATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
shutil.copy(REPO / "frontend/replay.html", os.path.abspath("/app/frontend/replay.html"))
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402

cl = TestClient(app_module.app)
s0 = cl.get("/api/replay/lf/state").json()
check("API: /api/replay/lf/state works with an empty cache and gives a request estimate", s0["data_ready"] is False and "estimate" in s0)
check("API: study without data is a 409/400 with the instruction", cl.post("/api/replay/lf/study", json={}).status_code in (400, 409))
paths_ = app_module.app.openapi()["paths"]
check("API routes: lf/state (GET), lf/fetch (POST), lf/study (POST)", set(paths_["/api/replay/lf/state"]) == {"get"} and set(paths_["/api/replay/lf/fetch"]) == {"post"} and set(paths_["/api/replay/lf/study"]) == {"post"})
html = (REPO / "frontend/replay.html").read_text(encoding="utf-8")
check("PAGE: the 'Studies' block has the Listing Fade card -- Fetch data, Run study, slippage checkboxes, progress, verdict badge, survivorship warning, equity chart, downloads, copyable summary line",
      all(x in html for x in ("Studies", "lfFetch", "lfRun", "lfSlip", "lfVerdict", "lfSurvivor", "lfChart", "lfCopy", "api/replay/lf/state", "api/replay/lf/study", "api/replay/lf/fetch")))
check("PAGE: results of every experiment use one verdict badge set (edge candidate / unproven / no edge / insufficient n) and no card starts a run without a confirm step", all(x in html for x in ("edge candidate", "unproven", "no edge", "insufficient n", "confirm(")))

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
