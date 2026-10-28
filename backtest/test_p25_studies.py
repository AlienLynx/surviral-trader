"""
P25: delisting short study and momentum study. Offline: synthetic Bybit-shaped data, fake HTTP, temp REPLAY_DIR. Arithmetic (returns, funding, costs, stop, hedge), timing rules, no look-ahead,
as-of universe, placebo, verdicts, fetching and the report files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = tempfile.mkdtemp()
os.environ["REPLAY_DIR"] = tmp
from backend.replay import carry, delist_study as ds, momentum as mo  # noqa: E402

H = ds.HOUR
D = carry.DAY_MS
UTC = timezone.utc
ms = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731

# ============================================================== delisting study: trade arithmetic
T0 = ms(datetime(2025, 3, 1, tzinfo=UTC))


def bars(prices, start=T0, turnover=1e6, high_mult=1.0):
    rows = []
    for i, p in enumerate(prices):
        rows.append([start + i * H, p, p * high_mult, p, p, 1.0, turnover])
    return ds.Bars(rows)


flat = bars([100.0] * 200)
tr = ds.short_trade(flat, [], 5, T0 + 50 * H, slip_pct=0.6)
check("TRADE: flat price, no funding -> net = -(2 x 0.055% + 0.6%) = -0.71%", tr and abs(tr["net"] + 0.0071) < 1e-9 and tr["reason"] == "time")
fall = bars([100.0] * 10 + [90.0] * 190)
tr = ds.short_trade(fall, [], 5, T0 + 50 * H, 0.6)
check("TRADE: a 10% fall is a +10% short before costs", abs(tr["price_ret"] - 0.10) < 1e-9 and abs(tr["net"] - (0.10 - 0.0071)) < 1e-9)
tr = ds.short_trade(flat, [[T0 + 10 * H, 0.001], [T0 + 20 * H, -0.0004], [T0 + 5 * H, 0.5], [T0 + 60 * H, 0.5]], 5, T0 + 50 * H, 0.6)
check("FUNDING: a short receives positive and pays negative funding for the settlements inside (entry, exit] only", abs(tr["funding"] - 0.0006) < 1e-12)
spike = bars([100.0] * 20 + [100.0] + [100.0] * 179)
spike.h[12] = 115.0
tr = ds.short_trade(spike, [], 5, T0 + 50 * H, 0.6)
check("STOP: a high of +15% inside the hold stops the short at +10% (a loss of 10% before costs)", tr["reason"] == "stop" and abs(tr["price_ret"] + 0.10) < 1e-9 and tr["exit_i"] == 12)
gap = bars([100.0] * 12 + [130.0] * 188)
gap.h[12] = 130.0
tr = ds.short_trade(gap, [], 5, T0 + 50 * H, 0.6)
check("STOP: a gap above the stop fills at the bar's open, not at the stop (the loss is bigger)", tr["reason"] == "stop" and tr["price_ret"] < -0.25)
check("TRADE: no room (exit not after entry) -> None", ds.short_trade(flat, [], 5, T0 + 5 * H, 0.6) is None)
# no look-ahead: bars after the exit do not change the trade
a = ds.short_trade(bars([100.0] * 60 + [100.0] * 140), [], 5, T0 + 50 * H, 0.6)
b = ds.short_trade(bars([100.0] * 60 + [999.0] * 140), [], 5, T0 + 50 * H, 0.6)
check("NO LOOK-AHEAD: bars after the exit time cannot change the trade", a["net"] == b["net"] and a["exit_i"] == b["exit_i"])
btc_up = bars([100.0 + i * 0.05 for i in range(200)])
hv = ds.hedge(btc_up, T0 + 5 * H, T0 + 55 * H)
check("HEDGE: the BTC leg earns its return minus its costs (0.11% fees + 0.05% slippage)", abs(hv - ((100 + 55 * 0.05) / (100 + 5 * 0.05) - 1 - 0.0016)) < 1e-9)

# ============================================================== event rules
PUB = T0 + 150 * H
EFF = T0 + 260 * H
ev1 = {"symbol": "XYZUSDT", "coin": "XYZ", "published_ms": PUB, "effective_ms": EFF, "title": "t"}
drift = bars([100.0 - 0.05 * i for i in range(400)])
r = ds.run_events([ev1], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, delay_h=1, slip_pct=0.6, hold_max_h=72, exit_buffer_h=2)
t = r["trades"][0]
check("RULE: entry is the first hourly open at least 1 h after the notice", t["entry_ts"] == PUB + 1 * H)
check("RULE: exit is entry + 72 h when that comes before delisting - 2 h", t["exit_ts"] == PUB + 1 * H + 72 * H and t["hours"] == 72)
r = ds.run_events([ev1], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, delay_h=1, slip_pct=0.6, hold_max_h=500, exit_buffer_h=2)
check("RULE: with a long hold cap the exit is delisting - 2 h (never inside the delisting)", r["trades"][0]["exit_ts"] == EFF - 2 * H)
late = {**ev1, "effective_ms": PUB + 2 * H}
check("RULE: a notice that leaves no room before the delisting is skipped and counted", ds.run_events([late], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, 1, 0.6, 72, 2)["skipped"]["too_late"] == 1)
thin = bars([100.0] * 400, turnover=1.0)
check("RULE: a coin with under 100k USDT traded in the 24h before the notice is skipped and counted", ds.run_events([ev1], {"XYZUSDT": thin}, {"XYZUSDT": []}, flat, 1, 0.6, 72, 2)["skipped"]["thin"] == 1)
check("RULE: no candles for the symbol -> skipped and counted", ds.run_events([ev1], {}, {}, flat, 1, 0.6, 72, 2)["skipped"]["no_candles"] == 1)
t0_ = ds.run_events([ev1], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, 0, 0.6, 72, 2)["trades"][0]
t24 = ds.run_events([ev1], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, 24, 0.6, 72, 2)["trades"][0]
check("RULE: a later entry delay enters later (and exits later: the hold length is the same)", t24["entry_ts"] == t0_["entry_ts"] + 24 * H and t24["hours"] == t0_["hours"])
check("BASELINE: 20 random shorts of the same length drawn BEFORE the notice (all end before it)", len(t["baseline_draws"]) == 20 and t["baseline"] is not None)
r1 = ds.run_events([ev1], {"XYZUSDT": drift}, {"XYZUSDT": []}, flat, 1, 0.6, 72, 2)["trades"][0]
check("BASELINE: deterministic (fixed seed)", r1["baseline_hedged"] == t["baseline_hedged"])

# ============================================================== stats + verdict
def fake_trades(nets, hedged=None, base=0.0, step=10):
    out = []
    for i, n in enumerate(nets):
        h_ = n if hedged is None else hedged[i]
        out.append({"net": n, "hedged": h_, "baseline": base, "baseline_hedged": base, "baseline_draws": [base - 0.01, base, base + 0.01], "published_ms": T0 + i * step * H, "effective_ms": T0 + (i * step + 48) * H, "reason": "time", "hours": 48})
    return out


good = fake_trades([0.03] * 60 + [0.02] * 60)
s = ds.summarize(good)
check("STATS: mean, median, win rate, halves, worst", abs(s["mean_pct"] - 2.5) < 1e-9 and s["win_pct"] == 100.0 and s["first_half_mean_pct"] == 3.0 and s["second_half_mean_pct"] == 2.0 and s["worst_pct"] == 2.0)
check("STATS: percentile vs random baseline is 100 when every event beats every baseline draw", s["percentile_vs_random"] == 100.0)
check("VERDICT: all thresholds met -> edge candidate", ds.verdict({**s, "top5_share_of_total": 0.1})["verdict"] == "edge candidate")
check("VERDICT: fewer than 100 events -> insufficient n", ds.verdict(ds.summarize(good[:50]))["verdict"] == "insufficient n")
bad_mean = ds.summarize(fake_trades([0.004] * 120))
check("VERDICT: mean below 1% -> not a candidate, reason named", ds.verdict(bad_mean)["verdict"] != "edge candidate" and any("mean net hedged" in x for x in ds.verdict(bad_mean)["failed"]))
conc = ds.summarize(fake_trades([0.5] * 5 + [0.001] * 115))
check("VERDICT: profit concentrated in 5 events fails the concentration rule", any("top 5" in x for x in ds.verdict(conc)["failed"]))
loser = ds.summarize(fake_trades([-0.05] * 60 + [-0.04] * 60))
check("VERDICT: a consistently negative result -> no edge", ds.verdict(loser)["verdict"] == "no edge")
wick = ds.summarize(fake_trades([0.03] * 119 + [-0.2]))
check("VERDICT: one event worse than -8% fails the worst-event rule", any("worst event" in x for x in ds.verdict(wick)["failed"]))

# ============================================================== events + fetch with a fake exchange
NOW = datetime(2026, 1, 15, tzinfo=UTC)
FEED = []
for i in range(6):
    p = ms(datetime(2025, 3, 1, tzinfo=UTC) + timedelta(days=10 * i))
    FEED.append({"title": f"Delisting of C{i}USDT Perpetual Contract", "description": "Bybit will be delisting at " + (datetime(2025, 3, 1, tzinfo=UTC) + timedelta(days=10 * i + 3)).strftime("%b %-d, %Y, %-I:%M%p") if os.name != "nt" else
                 "Bybit will be delisting at " + (datetime(2025, 3, 1, tzinfo=UTC) + timedelta(days=10 * i + 3)).strftime("%b %#d, %Y, %#I:%M%p") + " UTC.", "publishTime": p, "dateTimestamp": p, "url": f"https://x/{i}"})
FEED[0]["description"] = FEED[0]["description"] if FEED[0]["description"].endswith("UTC.") else FEED[0]["description"] + " UTC."
FEED.append({"title": "Delisting of OLDUSDT Perpetual Contract", "description": "at Jan 1, 2023, 9:00AM UTC.", "publishTime": ms(datetime(2023, 1, 1, tzinfo=UTC)), "dateTimestamp": 0, "url": "https://x/old"})


class R:
    def __init__(self, obj):
        self.obj, self.status_code = obj, 200

    def json(self):
        return self.obj

    def raise_for_status(self):
        pass


def fake_get(url, params=None, timeout=None, **kw):
    params = params or {}
    ep = url.split("/v5/")[1]
    if ep == "announcements/index":
        return R({"retCode": 0, "result": {"list": FEED if params["page"] == 1 else []}})
    sym = params.get("symbol")
    lo, hi = int(params.get("start") or params.get("startTime")), int(params.get("end") or params.get("endTime"))
    if ep == "market/kline":
        rows = []
        t = ((lo + H - 1) // H) * H
        while t <= hi:
            px = 100.0 if sym == "BTCUSDT" else 100.0 - ((t - ms(datetime(2025, 3, 1, tzinfo=UTC))) % (40 * 24 * H)) / H * 0.01
            rows.append([str(t), str(px), str(px * 1.001), str(px), str(px), "10", "5000000"])
            t += H
        rows.sort(key=lambda r: -int(r[0]))
        return R({"result": {"list": rows[:int(params["limit"])]}})
    if ep == "market/funding/history":
        rows = []
        t = ((lo + 8 * H - 1) // (8 * H)) * 8 * H
        while t <= hi:
            rows.append({"symbol": sym, "fundingRate": "0.0001", "fundingRateTimestamp": str(t)})
            t += 8 * H
        rows.sort(key=lambda r: -int(r["fundingRateTimestamp"]))
        return R({"result": {"list": rows[:int(params["limit"])]}})
    raise AssertionError(url)


events = ds.load_events(fake_get, NOW)
check("EVENTS: perpetual notices with a stated time since 2024 become one event per coin; the 2023 notice is dropped", len(events) == 6 and events[0]["symbol"] == "C0USDT" and all(e["published_ms"] >= ms(datetime(2024, 1, 1, tzinfo=UTC)) for e in events))
res = ds.fetch_all(fake_get, NOW, pace=0, sleep=lambda s: None)
check("FETCH: candles and funding for every event, BTC bars, no errors", res["events"] == 6 and res["errors"] == 0 and not res["stopped"] and ds.state()["data_ready"] and ds.state()["with_candles"] == 6)
rep = ds.run_study("delist-test", {}, NOW)
check("STUDY: report has the grid (4 delays x 3 slippages), hedged and raw summaries, a verdict, thresholds and limits", len(rep["grid"]) == 12 and rep["verdict"] and rep["pass_thresholds"] and rep["limits"] and rep["events"]["notices"] == 6)
check("STUDY: with 6 events the verdict is insufficient n (never a candidate on a tiny sample)", rep["verdict"] == "insufficient n")
ds.write_outputs("delist-test", {}, rep)
d_ = Path(tmp) / "delist-test"
check("OUTPUT: report.json, report.md, events.csv written; no internal series left in the JSON", all((d_ / f).is_file() for f in ("report.json", "report.md", "events.csv")) and "_trades" not in json.loads((d_ / "report.json").read_text(encoding="utf-8")))
check("OUTPUT: the markdown carries verdict, grid, rules, thresholds and limits", all(x in (d_ / "report.md").read_text(encoding="utf-8") for x in ("Verdict", "Grid", "Rules", "Pass thresholds", "Limits")))

# ============================================================== momentum study
N_SYM, N_DAYS = 26, 760
START = datetime(2024, 3, 1, tzinfo=UTC)
M0 = carry.day_start(datetime(2023, 11, 1, tzinfo=UTC))


def mk_sym(i, drift, vol=0.0, turnover=None, days=N_DAYS, delivery=0, status="Trading"):
    rng = np.random.default_rng(100 + i)
    px = [100.0]
    for k in range(1, days):
        px.append(px[-1] * (1.0 + drift + vol * rng.standard_normal()))
    perp = [[M0 + k * D, px[k], px[k], px[k], px[k], 1.0, (turnover or 1e7) * (1.0 + 0.001 * k)] for k in range(days)]
    idx = [[M0 + k * D, px[k], px[k], px[k], px[k]] for k in range(days)]
    fund = [[M0 + k * D + h * H, 0.0] for k in range(days) for h in (0, 8, 16)]
    return carry.Sym({"symbol": f"S{i}USDT", "status": status, "launch_ms": M0, "delivery_ms": delivery}, perp, idx, fund)


# winners keep winning, losers keep losing: persistent drifts -> momentum must earn
data = {f"S{i}USDT": mk_sym(i, drift=(i - N_SYM / 2) * 0.0004, vol=0.002, turnover=1e7 * (N_SYM - i % 5)) for i in range(N_SYM)}
end = M0 + (N_DAYS - 3) * D
days, syms, O, T, X = mo.build(data, START, datetime.fromtimestamp(end / 1000, tz=UTC))
check("BUILD: matrices have one row per symbol and a column per day, returns are open to open", O.shape[0] == N_SYM and X.shape == (N_SYM, O.shape[1] - 1) and abs(X[0, 50] - (O[0, 51] / O[0, 50] - 1)) < 1e-12)
k = next(i for i, d in enumerate(days) if d >= carry.day_start(START)) + 40
uni = mo.universe_at(T, O, k)
check("UNIVERSE: top 20 by trailing 30-day turnover as of the day", len(uni) == 20)
w = mo.weights_for(O, T, k, 14, "long_short")
check("BOOK: 4 longs and 4 shorts, dollar neutral (half the sleeve per leg)", sum(1 for v in w.values() if v > 0) == 4 and sum(1 for v in w.values() if v < 0) == 4 and abs(sum(w.values())) < 1e-12 and abs(sum(v for v in w.values() if v > 0) - 0.5) < 1e-12)
mom = {i: O[i, k] / O[i, k - 14] - 1 for i in uni}
check("BOOK: the longs are the 4 highest 14-day returns, the shorts the 4 lowest", set(i for i, v in w.items() if v > 0) == set(sorted(mom, key=lambda i: -mom[i])[:4]) and set(i for i, v in w.items() if v < 0) == set(sorted(mom, key=lambda i: mom[i])[:4]))
wl = mo.weights_for(O, T, k, 14, "long_only")
check("BOOK: long-only puts the whole sleeve in the top 4", len(wl) == 4 and abs(sum(wl.values()) - 1.0) < 1e-12)
# no look-ahead: change every price after day k -> the weights at k are identical
O2 = O.copy()
O2[:, k + 1:] *= 1.7
check("NO LOOK-AHEAD: prices after the rebalance day cannot change that day's book", mo.weights_for(O2, T, k, 14, "long_short") == w)
k0 = next(i for i, d in enumerate(days) if d >= carry.day_start(START))
kend = len(days) - 2
sim = mo.simulate(days, O, T, X, k0, kend, 14, "long_short", 0.10)
st = mo.stats(sim["pnl"], k0, kend)
check("SIM: with persistent drifts the momentum book earns (net of costs)", st["net_pct_of_sleeve"] > 0 and st["apr_pct"] > 0)
sim_hi = mo.simulate(days, O, T, X, k0, kend, 14, "long_short", 0.30)
check("SIM: higher slippage never earns more", mo.stats(sim_hi["pnl"], k0, kend)["net_pct_of_sleeve"] < st["net_pct_of_sleeve"])
sim0 = mo.simulate(days, O, T, X, k0, kend, 14, "long_short", 0.0)
check("SIM: costs are charged on traded notional (zero slippage still pays the 0.055% fee)", mo.stats(sim0["pnl"], k0, kend)["net_pct_of_sleeve"] > st["net_pct_of_sleeve"])
plc = [float(mo.simulate(days, O, T, X, k0, kend, 14, "long_short", 0.10, rng=np.random.default_rng(s))["pnl"][k0:kend].mean()) * 36500 for s in range(30)]
check("PLACEBO: random-coin books centre near zero, far below the momentum book on trending data", abs(float(np.median(plc))) < st["apr_pct"] and st["apr_pct"] > float(np.percentile(plc, 90)))
# funding: a long pays positive funding, a short receives it
data_f = {f"S{i}USDT": mk_sym(i, drift=0.0, vol=0.0, turnover=1e7 * (N_SYM - i)) for i in range(N_SYM)}
for s_ in data_f.values():
    s_.f_rate = [0.0003] * len(s_.f_rate)
dF, sF, OF, TF, XF = mo.build(data_f, START, datetime.fromtimestamp(end / 1000, tz=UTC))
check("FUNDING: with flat prices X is minus the day's funding (3 x 0.03% = -0.09% a day)", abs(XF[0, 100] + 0.0009) < 1e-9)
# verdict
ok_oos = {"days": 467, "apr_pct": 25.0, "max_dd_pct": 12.0, "ci95_daily_apr_pct": [5.0, 45.0]}
check("VERDICT: all thresholds met -> edge candidate", mo.verdict(ok_oos, {"apr_pct": 5.0}, 99.0, 65)["verdict"] == "edge candidate")
check("VERDICT: CI that includes 0 -> unproven", mo.verdict({**ok_oos, "ci95_daily_apr_pct": [-10.0, 60.0]}, {"apr_pct": 5.0}, 99.0, 65)["verdict"] == "unproven")
check("VERDICT: drawdown over 20% of the sleeve fails", any("drawdown" in x for x in mo.verdict({**ok_oos, "max_dd_pct": 44.0}, {"apr_pct": 5.0}, 99.0, 65)["failed"]))
check("VERDICT: placebo percentile under 95 fails", any("placebo" in x for x in mo.verdict(ok_oos, {"apr_pct": 5.0}, 80.0, 65)["failed"]))
check("VERDICT: too few out-of-sample rebalances -> insufficient n", mo.verdict(ok_oos, {"apr_pct": 5.0}, 99.0, 10)["verdict"] == "insufficient n")
check("VERDICT: a negative out-of-sample result whose CI is below 0 -> no edge", mo.verdict({**ok_oos, "apr_pct": -30.0, "ci95_daily_apr_pct": [-60.0, -5.0]}, {"apr_pct": 5.0}, 10.0, 65)["verdict"] == "no edge")

# end to end from the carry cache layout
pool = [{"symbol": s.info["symbol"], "status": "Trading", "launch_ms": M0, "delivery_ms": 0} for s in data.values()]
carry.lf._write_json(carry.cdir("pool.json"), {"fetched_at": end, "items": pool, "spot": [p["symbol"] for p in pool]})
for s in data.values():
    sym = s.info["symbol"]
    carry.lf._write_json(carry.cdir("perp", f"{sym}.json"), {"rows": [[d, s.p_open[d], s.p_open[d], s.p_open[d], s.p_open[d], 1.0, s.turn[d]] for d in s.days], "vfrom": M0, "vto": end})
    carry.lf._write_json(carry.cdir("index", f"{sym}.json"), {"rows": [[d, v, v, v, v] for d, v in s.i_open.items()], "vfrom": M0, "vto": end})
    carry.lf._write_json(carry.cdir("funding", f"{sym}.json"), {"rows": [[t_, r_] for t_, r_ in zip(s.f_ts, s.f_rate)], "vfrom": M0, "vto": end})
rep = mo.run_study("mom-test", {"since": "2024-03-01"}, datetime.fromtimestamp(end / 1000, tz=UTC) + timedelta(days=1))
check("STUDY: report has 12 configurations (3 lookbacks x 2 books x 2 slippages), benchmarks, a placebo and a verdict", len(rep["results"]) == 12 and rep["placebo"]["seeds"] == 300 and rep["verdict"] and "equal_weight_universe" in rep["benchmarks"])
mo.write_outputs("mom-test", {}, rep)
d_ = Path(tmp) / "mom-test"
check("OUTPUT: report.json, report.md, equity.csv written; no internal series left in the JSON", all((d_ / f).is_file() for f in ("report.json", "report.md", "equity.csv")) and "_sims" not in json.loads((d_ / "report.json").read_text(encoding="utf-8")))
check("OUTPUT: the markdown carries verdict, placebo, benchmarks, rules, thresholds and limits", all(x in (d_ / "report.md").read_text(encoding="utf-8") for x in ("Verdict", "Placebo", "Benchmarks", "Rules", "Pass thresholds", "Limits")))

print("\nALL P25 STUDIES CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
