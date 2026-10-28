"""
P25: funding carry study. Offline: synthetic Bybit-shaped market, fake HTTP, temp REPLAY_DIR. Checks the arithmetic (funding, basis, costs), the entry / exit rules, no look-ahead,
as-of universe, delisting, the verdict, fetching (paging, resume) and the report files.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = tempfile.mkdtemp()
os.environ["REPLAY_DIR"] = tmp
from backend.replay import carry  # noqa: E402

D = carry.DAY_MS
T0 = int(datetime(2023, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)
N_DAYS = 700


def series(sym, perp_fn, idx_fn, fund_fn, turnover, days=N_DAYS, start=T0, delivery=0, status="Trading"):
    perp = [[start + i * D, perp_fn(i), perp_fn(i), perp_fn(i), perp_fn(i), 1.0, turnover] for i in range(days)]
    index = [[start + i * D, idx_fn(i), idx_fn(i), idx_fn(i), idx_fn(i)] for i in range(days)]
    funding = []
    for i in range(days):
        for h in (0, 8, 16):
            funding.append([start + i * D + h * 3_600_000, fund_fn(i, h)])
    return carry.Sym({"symbol": sym, "status": status, "launch_ms": start, "delivery_ms": delivery}, perp, index, funding)


flat = lambda i: 100.0  # noqa: E731
START, END = datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2025, 3, 1, tzinfo=timezone.utc)
cb = carry.cost_pct("BTCUSDT", 0.3)
check("COST: majors round trip = 2x0.10 + 2x0.055 + 0.10 = 0.41%, alts 0.61%", abs(cb - 0.41) < 1e-9 and abs(carry.cost_pct("XYZUSDT", 0.3) - 0.61) < 1e-9)
check("HURDLE: majors 2 x 0.41% x 365 / 60 = 4.99% a year, alts 2 x 0.61% x 365 / 14 = 31.8%", abs(carry.hurdle_ann_pct("BTCUSDT", 0.3) - 4.988) < 0.01 and abs(carry.hurdle_ann_pct("XYZUSDT", 0.3) - 31.81) < 0.05)

# --- 1. BTC pays 0.01% / 8h (10.95% a year), perfect hedge: enters once, earns funding minus the round trip
btc = series("BTCUSDT", flat, flat, lambda i, h: 0.0001, 1e9)
sim = carry.simulate({"BTCUSDT": btc}, START, END)
days = (carry.day_start(END) - carry.day_start(START)) / D
ep = sim["episodes"]
check("PAY: one episode that is still open at the end (funding stays above the hurdle)", len(ep) == 1 and ep[0]["reason"] == "open_at_end")
exp_net = 0.0001 * 3 * (ep[0]["days"]) * 100 - cb                       # funding for the days held minus the full round trip, in %
check("PAY: episode net = funding of the days held - 0.41% (within 0.15 pp)", abs(ep[0]["net_pct"] - exp_net) < 0.15)
st = carry.stats(sim)
check("PAY: net APR on the deployed notional is about 10.5% (funding 10.95% minus costs spread over the hold)", 9.0 < st["apr_on_deployed_pct"] < 11.0)
check("PAY: one of five slots is used -> about 20% of the sleeve deployed", 17 < st["avg_deployed_pct"] < 21)

# --- 2. funding below the hurdle (BTC 3% a year) -> no entry; negative funding -> no entry
low = series("BTCUSDT", flat, flat, lambda i, h: 0.0000274, 1e9)
check("HURDLE: 3% a year does not cover the round trip -> no episode", carry.simulate({"BTCUSDT": low}, START, END)["episodes"] == [])
neg = series("BTCUSDT", flat, flat, lambda i, h: -0.0001, 1e9)
check("NEGATIVE: funding below 0 -> no episode", carry.simulate({"BTCUSDT": neg}, START, END)["episodes"] == [])

# --- 3. exit when the 3-day mean funding turns negative; the rate flips on day 400 of the data
flip_day = 400
flip = series("BTCUSDT", flat, flat, lambda i, h: 0.0001 if i < flip_day else -0.0002, 1e9)
e3 = carry.simulate({"BTCUSDT": flip}, START, END)["episodes"]
exit_idx = (e3[0]["exit"] - T0) // D
check("EXIT: closes within 3 days of the flip (3-day mean < 0), reason funding<0", len(e3) == 1 and e3[0]["reason"] == "funding<0" and flip_day <= exit_idx <= flip_day + 3)
check("EXIT: the loss while funding was negative is limited (the position leaves, it does not ride the negative funding)", e3[0]["funding_pct"] > 0)

# --- 4. basis: perp +1% while the index is flat on one day -> the hedge loses about 1% (short perp, long spot)
jump_day = 450
perp_j = lambda i: 100.0 if i < jump_day else 101.0  # noqa: E731
bj = series("BTCUSDT", perp_j, flat, lambda i, h: 0.0001, 1e9)
eb = carry.simulate({"BTCUSDT": bj}, START, END)["episodes"][0]
check("BASIS: a 1% perp jump costs the hedge about 1% (basis_pct about -1)", -1.1 < eb["basis_pct"] < -0.9)
check("BASIS: net = funding + basis - cost", abs(eb["net_pct"] - (eb["funding_pct"] + eb["basis_pct"] - eb["cost_pct"])) < 0.01)

# --- 5. delisting: an alt with 54% a year funding until delivery day -> episode ends 'delisted' with the extra cost
deliv = T0 + 450 * D                       # 2025-01-25, inside the window (ends 2025-03-01)
alt = series("DEADUSDT", flat, flat, lambda i, h: 0.0005, 5e8, days=450, delivery=deliv, status="Closed")
ed = carry.simulate({"BTCUSDT": btc, "DEADUSDT": alt}, START, END)["episodes"]
dead = [e for e in ed if e["symbol"] == "DEADUSDT"]
check("DELIST: the alt is entered (54% a year clears the 31.8% hurdle) and left at delivery with reason 'delisted'", len(dead) >= 1 and dead[-1]["reason"] == "delisted")
check("DELIST: the extra delisting cost (1%) is charged", dead[-1]["cost_pct"] >= 0.61 + 1.0 - 1e-9)

# --- 6. as-of universe: only the top-N alts by 30-day turnover as of that day are candidates
alts = {f"A{i}USDT": series(f"A{i}USDT", flat, flat, lambda i_, h: 0.0006, 1e6 * (i + 1)) for i in range(6)}
data6 = {"BTCUSDT": series("BTCUSDT", flat, flat, lambda i, h: 0.0, 1e9), **alts}
top2 = {e["symbol"] for e in carry.simulate(data6, START, END, top_n=2)["episodes"]}
check("UNIVERSE: with top_n=2 only the two most liquid alts (A5, A4) are ever held", top2 <= {"A5USDT", "A4USDT"} and top2)
check("MAJORS ONLY: alts are ignored", carry.simulate(data6, START, END, majors_only=True)["episodes"] == [])

# --- 6b. the spot leg is required: a perpetual without a spot market is never carried
nos = carry.simulate(data6, START, END, top_n=6, allowed={"A5USDT"})
check("SPOT: with a spot market only on A5 the study holds A5 and nothing else", {e["symbol"] for e in nos["episodes"]} == {"A5USDT"})
check("SPOT: with no spot market on any alt only the majors are possible", carry.simulate(data6, START, END, top_n=6, allowed=set())["episodes"] == [])

# --- 7. no look-ahead: changing everything after day X must not change any decision before X
cut = carry.day_start(datetime(2024, 8, 1, tzinfo=timezone.utc))
base = series("BTCUSDT", flat, flat, lambda i, h: 0.0001 if i % 40 < 30 else -0.0003, 1e9)
shuf = series("BTCUSDT", flat, flat, lambda i, h: (0.0001 if i % 40 < 30 else -0.0003) if (T0 + i * D) < cut else 0.0009, 1e9)
a = [e for e in carry.simulate({"BTCUSDT": base}, START, END)["episodes"] if e["exit"] < cut - 4 * D]
b = [e for e in carry.simulate({"BTCUSDT": shuf}, START, END)["episodes"] if e["exit"] < cut - 4 * D]
check("NO LOOK-AHEAD: episodes that closed well before the change are identical", a == b and len(a) >= 3)

# --- 8. verdict
good = {"episodes": 80, "mean_episode_pct": 0.5, "ci95_pct": [0.2, 0.8], "worst_episode_pct": -0.5, "apr_on_deployed_pct": 7.0, "max_dd_pct_of_sleeve": 1.0}
check("VERDICT: all conditions met -> edge candidate", carry.verdict(good, 2.0)["verdict"] == "edge candidate")
check("VERDICT: too few episodes -> insufficient n", carry.verdict({**good, "episodes": 20}, 2.0)["verdict"] == "insufficient n")
check("VERDICT: APR below 5% -> unproven, with the reason", carry.verdict({**good, "apr_on_deployed_pct": 3.0}, 2.0)["verdict"] == "unproven" and carry.verdict({**good, "apr_on_deployed_pct": 3.0}, 2.0)["failed"])
check("VERDICT: does not beat majors-only by 1 pp -> not a candidate", carry.verdict(good, 0.3)["verdict"] != "edge candidate")
check("VERDICT: every CI above the mean below zero -> no edge", carry.verdict({**good, "mean_episode_pct": -1.0, "ci95_pct": [-1.5, -0.5], "apr_on_deployed_pct": -2.0}, 2.0)["verdict"] == "no edge")

# --- 9. fetching with a fake exchange: paging, resume, delisted instruments, report files
SYMS = {"BTCUSDT": ("Trading", 0), "ETHUSDT": ("Trading", 0), "ALTUSDT": ("Trading", 0), "OLDUSDT": ("Closed", T0 + 600 * D)}


class R:
    def __init__(self, obj):
        self.obj, self.status_code = obj, 200

    def json(self):
        return self.obj

    def raise_for_status(self):
        pass


calls = []


def fake_get(url, params=None, timeout=None, **kw):
    params = params or {}
    calls.append(url.rsplit("/", 1)[-1])
    ep = url.split("/v5/")[1]
    if ep == "market/instruments-info" and params.get("category") == "spot":
        return R({"result": {"list": [{"symbol": x, "quoteCoin": "USDT", "status": "Trading"} for x in ("BTCUSDT", "ETHUSDT", "ALTUSDT")], "nextPageCursor": ""}})
    if ep == "market/instruments-info":
        st = params["status"]
        return R({"result": {"list": [{"symbol": s, "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": v[0], "launchTime": str(T0), "deliveryTime": str(v[1])} for s, v in SYMS.items() if v[0] == st], "nextPageCursor": ""}})
    if ep == "market/tickers":
        return R({"result": {"list": [{"symbol": s, "turnover24h": str(1e9 - 1e6 * i)} for i, s in enumerate(SYMS)]}})
    sym = params.get("symbol")
    lo, hi = int(params.get("start") or params.get("startTime")), int(params.get("end") or params.get("endTime"))
    if ep in ("market/kline", "market/index-price-kline"):
        rows = []
        for i in range(N_DAYS):
            ts = T0 + i * D
            if lo <= ts <= hi:
                rows.append([str(ts), "100", "100", "100", "100", "1", "5000000"] if ep == "market/kline" else [str(ts), "100", "100", "100", "100"])
        rows.sort(key=lambda r: -int(r[0]))
        return R({"result": {"list": rows[:int(params["limit"])]}})
    if ep == "market/funding/history":
        rows = []
        for i in range(N_DAYS):
            for h in (0, 8, 16):
                ts = T0 + i * D + h * 3_600_000
                if lo <= ts <= hi:
                    rows.append({"symbol": sym, "fundingRate": "0.0001", "fundingRateTimestamp": str(ts)})
        rows.sort(key=lambda r: -int(r["fundingRateTimestamp"]))
        return R({"result": {"list": rows[:int(params["limit"])]}})
    raise AssertionError(url)


now = datetime(2025, 9, 1, tzinfo=timezone.utc)
res = carry.fetch_all(fake_get, now, pace=0, sleep=lambda s: None)
check("FETCH: all four symbols fetched (incl. the Closed one), no errors", res["pool"] == 4 and res["errors"] == 0 and not res["stopped"])
stt = carry.state()
check("FETCH: state is ready and counts the pool", stt["data_ready"] and stt["pool"] == 4)
rows = json.loads((Path(tmp) / "carry" / "funding" / "BTCUSDT.json").read_text(encoding="utf-8"))["rows"]
check("FETCH: funding paged past the 200-row limit (3 settlements a day over hundreds of days)", len(rows) > 600 and len({r[0] for r in rows}) == len(rows))
n1 = len(calls)
carry.fetch_all(fake_get, now, pace=0, sleep=lambda s: None)
check("RESUME: a second run fetches almost nothing (cached ranges are not asked again)", len(calls) - n1 < 25)
data = carry.load_data()
check("LOAD: every symbol loads with perp, index and funding", set(data) == set(SYMS))

rep = carry.run_study("carry-test", {}, until=now)
check("STUDY: report has the four scenarios x (all, locked, oos), a verdict and the pass thresholds", set(rep["scenarios"]) == {"spot_alts_slip0.3", "spot_alts_slip0.6", "majors_only", "no_spot_constraint_NOT_REALISABLE"} and rep["verdict"] and rep["pass_thresholds"])
check("STUDY: more slippage never earns more, and dropping the spot requirement can only add (the unconstrained run is the optimistic bound)", rep["scenarios"]["spot_alts_slip0.3"]["all"]["net_usd"] >= rep["scenarios"]["spot_alts_slip0.6"]["all"]["net_usd"] and rep["scenarios"]["no_spot_constraint_NOT_REALISABLE"]["all"]["net_usd"] >= rep["scenarios"]["spot_alts_slip0.3"]["all"]["net_usd"])
carry.write_outputs("carry-test", {}, rep)
d = Path(tmp) / "carry-test"
check("OUTPUT: report.json, report.md, episodes.csv, equity.csv are written", all((d / f).is_file() for f in ("report.json", "report.md", "episodes.csv", "equity.csv")))
check("OUTPUT: the markdown carries the verdict, the rules, the thresholds and the limits", all(x in (d / "report.md").read_text(encoding="utf-8") for x in ("Verdict", "Pass thresholds", "Limits", "Rules")))
check("OUTPUT: the saved JSON has no internal series left in it", "_sims" not in json.loads((d / "report.json").read_text(encoding="utf-8")))

# --- 10. the fetch only needs index + funding for symbols that are EVER in the as-of top-N: that set must equal what the simulation would pick
pool6 = [{"symbol": f"A{i}USDT", "status": "Trading", "launch_ms": T0, "delivery_ms": 0} for i in range(6)] + [{"symbol": "BTCUSDT", "status": "Trading", "launch_ms": T0, "delivery_ms": 0}]
for it in pool6:
    i = int(it["symbol"][1]) if it["symbol"].startswith("A") else 9
    rows_ = [[T0 + k * D, 100.0, 100.0, 100.0, 100.0, 1.0, 1e6 * (i + 1)] for k in range(N_DAYS)]
    carry.lf._write_json(carry.cdir("perp", f"{it['symbol']}.json"), {"rows": rows_, "vfrom": T0, "vto": T0 + N_DAYS * D})
keep2 = carry.ever_in_universe(pool6, START, END, top_n=2)
check("UNIVERSE (fetch side): top 2 by turnover + the majors, the rest is never needed", keep2 == {"BTCUSDT", "ETHUSDT", "A5USDT", "A4USDT"})
sim_syms = {e["symbol"] for e in carry.simulate({it["symbol"]: series(it["symbol"], flat, flat, lambda i_, h: 0.0006, 1e6 * (int(it["symbol"][1]) + 1 if it["symbol"].startswith("A") else 10)) for it in pool6}, START, END, top_n=2)["episodes"]}
check("UNIVERSE: the simulation never holds a symbol the fetch side left out", sim_syms <= keep2)

print("\nALL P25 CARRY CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
