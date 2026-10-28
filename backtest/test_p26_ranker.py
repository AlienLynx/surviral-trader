"""
P26-R: LightGBM ranker, paper forward test. Offline: a synthetic Binance cache in the study's layout, a fake exchange API, simulated days. Nothing is traded; no network.
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
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


tmp = Path(tempfile.mkdtemp())
os.environ["DATA_DIR"] = str(tmp / "data")
os.environ["REPLAY_DIR"] = str(tmp / "replay")
os.environ.pop("RANKER_MODE", None)
(tmp / "data").mkdir()
from backend.engine import ranker as rk, runtime_settings as rs  # noqa: E402
from backend.replay import lgbm_hourly as lh, listing_fade as lf  # noqa: E402

HOUR = 3600 * 1000
# ======================================================================= pure functions
sc = {f"C{i}": float(i) for i in range(12)}
w = rk.target_weights(sc, 3)
check("BOOK: long the 3 highest scores, short the 3 lowest, half of the sleeve per leg", sorted(s for s, v in w.items() if v > 0) == ["C11", "C10", "C9"][::-1] or set(s for s, v in w.items() if v > 0) == {"C9", "C10", "C11"})
check("BOOK: dollar neutral, gross 1.0", abs(sum(w.values())) < 1e-12 and abs(sum(abs(v) for v in w.values()) - 1.0) < 1e-12 and set(s for s, v in w.items() if v < 0) == {"C0", "C1", "C2"})
check("BOOK: too few coins -> no book", rk.target_weights({"A": 1.0, "B": 2.0}, 3) == {})
check("TURNOVER: first rebalance trades the whole gross (1.0); an unchanged book trades 0; swapping one long for another costs 2 x 1/6",
      abs(rk.turnover({}, w) - 1.0) < 1e-12 and rk.turnover(w, w) == 0.0 and abs(rk.turnover(w, {**{k: v for k, v in w.items() if k != "C9"}, "C8": w["C9"]}) - 2 / 6) < 1e-12)
check("MARK: sum of weight x return; a coin with no price (nan / missing) earns 0, a short gains when the coin falls", abs(rk.gross_day({"A": 0.25, "B": -0.25, "C": 0.25}, {"A": 0.04, "B": -0.02, "C": float("nan")}) - 0.015) < 1e-12)
check("SCALE: until 10 days exist B runs at 0.5; calm A -> 1.0 (never above); wild A (2.4%/day) -> target/vol", rk.vol_scale([0.01] * 5) == 0.5 and rk.vol_scale([0.001, -0.001] * 10) == 1.0
      and abs(rk.vol_scale([0.024, -0.024] * 10) - 0.010 / 0.024) < 1e-9)
a = {f"C{i}": float(i) for i in range(10)}
check("IC: perfect ranking = 1, reversed = -1, fewer than 8 coins = None", abs(rk.rank_ic(a, a) - 1) < 1e-12 and abs(rk.rank_ic(a, {k: -v for k, v in a.items()}) + 1) < 1e-12 and rk.rank_ic({"A": 1, "B": 2}, {"A": 1, "B": 2}) is None)

# passive execution outcome (pure) with hand-made bars
HN = 24 * 6
Mp = {"o": np.full((2, HN), 100.0, dtype=np.float32), "hi": np.full((2, HN), 100.0, dtype=np.float32), "lo": np.full((2, HN), 100.0, dtype=np.float32)}
Mp["lo"][0, 48] = 99.8
Mp["hi"][1, 48] = 100.2
orders2 = [{"s": "A", "d": 0.5}, {"s": "B", "d": -0.5}]
po = rk.passive_outcome(orders2, Mp, {"A": 0, "B": 1}, 48, 0.001, 2)
check("PASSIVE: a buy limit 0.1% under the open fills when the low reaches it, a sell limit 0.1% over when the high does -> both fill, gain = notional x (delta - maker fee)", po["filled"] == 2 and po["chased"] == 0 and abs(po["filled_gain"] - 1.0 * (0.001 - 0.0002)) < 1e-12)
Mp2 = {k: v.copy() for k, v in Mp.items()}
Mp2["lo"][0, 48] = 100.0
Mp2["hi"][1, 48] = 100.0
Mp2["o"][0, 50] = 101.0
Mp2["o"][1, 50] = 101.0
po2 = rk.passive_outcome(orders2, Mp2, {"A": 0, "B": 1}, 48, 0.001, 2)
check("PASSIVE: neither fills -> both chased at the next open; the buyer pays the +1% run and the seller sells into a rise and loses the same, so the moves cancel in the sum", po2["chased"] == 2 and abs(po2["chase_move"] - (0.5 * 0.01 + 0.5 * -0.01)) < 1e-12 and abs(po2["chase_amt"] - 1.0) < 1e-12)
check("PASSIVE: a coin that is not in the data is chased with no move (never a free fill)", rk.passive_outcome([{"s": "ZZZ", "d": 0.5}], Mp, {"A": 0}, 48)["chased"] == 1)

# book C arithmetic in the forward statistics
ledC = [{"kind": "rebalance", "date": "2026-11-01", "turnover_A": 1.0, "turnover_B": 0.5, "orders": []}, {"kind": "rebalance", "date": "2026-11-04", "turnover_A": 1.0, "turnover_B": 0.5, "orders": []},
        {"kind": "passive", "date": "2026-11-01", "orders": 6, "filled": 5, "chased": 1, "filled_gain": 0.0008, "chase_move": 0.0003, "chase_amt": 0.1},
        {"kind": "day", "date": "2026-11-02", "gross_A": 0.004, "gross_B": 0.002}, {"kind": "day", "date": "2026-11-05", "gross_A": 0.004, "gross_B": 0.002}]
sc10 = rk.forward_stats(ledC, [], 0.10)
cost10 = (rk.FEE_PCT + 0.10) / 100
exp_c = 0.008 - (0.0003 + 0.1 * cost10 - 0.0008) - 1.0 * cost10          # evaluated rebalance + a pending one that is charged the taker cost
check("BOOK C: evaluated rebalances pay the real outcome (chase move + chased notional x (taker fee + slippage) - fill gain), pending ones the taker cost", abs(sc10["C"]["net_pct_of_sleeve"] - exp_c * 100) < 2e-3)       # the statistics are rounded to 3 decimals of a percent
sc30 = rk.forward_stats(ledC, [], 0.30)
check("BOOK C: at higher slippage only the chased part gets dearer (C degrades less than A)", sc30["A"]["net_pct_of_sleeve"] - sc10["A"]["net_pct_of_sleeve"] < sc30["C"]["net_pct_of_sleeve"] - sc10["C"]["net_pct_of_sleeve"] < 0)
check("BOOK C: the passive summary reports evaluated rebalances, fill rate, chased count, average adverse move and the saving against A", sc10["passive"]["rebalances_evaluated"] == 1 and sc10["passive"]["rebalances"] == 2 and abs(sc10["passive"]["fill_rate"] - 5 / 6) < 1e-3
      and sc10["passive"]["chased"] == 1 and abs(sc10["passive"]["avg_chase_move_pct"] - 0.3) < 1e-6 and sc10["passive"]["saving_vs_A_pct_of_sleeve"] > 0)
led_un = ledC[:1] + [{"kind": "passive", "date": "2026-11-01", "unevaluated": True}] + ledC[3:4]
check("BOOK C: a rebalance recorded as unevaluated (outside the data window) is charged the taker cost, so C equals A there", abs(rk.forward_stats(led_un, [], 0.10)["C"]["net_pct_of_sleeve"] - rk.forward_stats(led_un, [], 0.10)["A"]["net_pct_of_sleeve"]) < 1e-9)

# forward statistics: costs are applied when reading
led = [{"kind": "rebalance", "date": "2026-11-01", "turnover_A": 1.0, "turnover_B": 0.5}]
for i in range(60):
    led.append({"kind": "day", "date": (datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=i)).strftime("%Y-%m-%d"), "gross_A": 0.004, "gross_B": 0.002})
led += [{"kind": "rebalance", "date": (datetime(2026, 11, 4, tzinfo=UTC) + timedelta(days=3 * i)).strftime("%Y-%m-%d"), "turnover_A": 1.0, "turnover_B": 0.5} for i in range(19)]
ics = [{"date": "x", "ic": 0.05}] * 19
b10, b30 = rk.forward_stats(led, ics, 0.10), rk.forward_stats(led, ics, 0.30)
exp_a = 60 * 0.004 - 20 * 1.0 * 0.00155
check("STATS: net = gross - turnover x (0.055% + slippage) per side, read at any slippage (0.10% and 0.30% differ by turnover x 0.2%)", abs(b10["A"]["net_pct_of_sleeve"] - exp_a * 100) < 1e-6 and abs(b30["A"]["net_pct_of_sleeve"] - (60 * 0.004 - 20 * 0.00355) * 100) < 1e-6)
check("STATS: days, rebalances, IC and a drawdown are reported", b10["days"] == 60 and b10["rebalances"] == 20 and b10["ic"]["mean"] == 0.05 and b10["A"]["max_dd_pct"] >= 0 and len(b10["A"]["curve"]) >= 60)
v = rk.forward_verdict(b10, b30)
check("VERDICT: 60 days, 20 rebalances, IC 0.05, B positive at both slippages, calm drawdown -> forward pass", v["verdict"] == "forward pass" and v["failed"] == [])
check("VERDICT: before 56 days / 18 rebalances it is `collecting`, whatever the numbers", rk.forward_verdict({**b10, "days": 30}, b30)["verdict"] == "collecting" and rk.forward_verdict({**b10, "rebalances": 5}, b30)["verdict"] == "collecting")
v2 = rk.forward_verdict(b10, {**b30, "B": {**b30["B"], "net_pct_of_sleeve": -1.0}})
check("VERDICT: B negative at 0.30% slippage -> unproven (costs decide)", v2["verdict"] == "unproven" and any("0.30%" in x for x in v2["failed"]))
v3 = rk.forward_verdict({**b10, "ic": {"n": 19, "mean": 0.0, "hit_pct": 50.0}, "B": {**b10["B"], "net_pct_of_sleeve": -2.0}}, b30)
check("VERDICT: B negative at 0.10% and no positive IC -> failing", v3["verdict"] == "failing")
check("VERDICT: a drawdown over 20% of the sleeve blocks a pass", rk.forward_verdict({**b10, "B": {**b10["B"], "max_dd_pct": 35.0}}, b30)["verdict"] == "unproven")

# ======================================================================= settings
check("SETTINGS: RANKER_MODE is off by default, accepts off / shadow only, junk = off; it is a dashboard switch", rk.mode({}) == "off" and rk.mode({"RANKER_MODE": "shadow"}) == "shadow" and rk.mode({"RANKER_MODE": "enforce"}) == "off"
      and "RANKER_MODE" in rs.SPEC and rs.SPEC["RANKER_MODE"]["default"] == "off" and rs.SPEC["RANKER_MODE"]["validate"]("live") is None)

# ======================================================================= a synthetic Binance cache and a fake exchange
lh.SINCE = "2022-10-01"
lh.UNIVERSE_N, rk.UNIVERSE_N, rk.QUINTILE = 12, 12, 3
rk.TAIL_PACE = 0.0
lh.PARAMS = {**lh.PARAMS, "n_estimators": 60, "n_jobs": 2, "min_child_samples": 50}
T0 = datetime(2022, 10, 1, tzinfo=UTC)
T_END = datetime(2025, 4, 6, tzinfo=UTC)
N = int((T_END - T0).total_seconds() // 3600)
TS = np.array([int((T0 + timedelta(hours=h)).timestamp() * 1000) for h in range(N)], dtype=np.int64)
SYMS = [f"S{i}USDT" for i in range(15)] + ["BTCUSDT"]
SER = {}
for i, sym in enumerate(SYMS):
    rng = np.random.default_rng(900 + i)
    r = (i - 8) * 0.00001 + 0.0008 * rng.standard_normal(N)
    px = 100.0 * np.cumprod(1 + r)
    qv = 1e6 * (len(SYMS) - i % 5) * (1 + 0.1 * rng.random(N))
    SER[sym] = (px, qv, qv * (0.5 + 0.03 * rng.standard_normal(N)))


def rows_of(sym, a, b):
    px, qv, tb = SER[sym]
    sel = np.flatnonzero((TS >= a) & (TS < b))
    nxt = np.minimum(sel + 1, N - 1)
    return np.column_stack([TS[sel], px[sel], px[sel] * 1.002, px[sel] * 0.998, px[nxt], qv[sel], np.full(len(sel), 1000.0), tb[sel]])


months = []
y, m = 2022, 10
while (y, m) <= (2025, 2):
    months.append((y, m))
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
for sym in SYMS:
    for (y, m) in months:
        a = int(datetime(y, m, 1, tzinfo=UTC).timestamp() * 1000)
        b = int(datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=UTC).timestamp() * 1000)
        lh._save(lh.cdir("k", sym, f"{y}-{m:02d}.npy"), rows_of(sym, a, b))
        fts = [a + d * 86_400_000 + h * HOUR for d in range(27) for h in (0, 8, 16)]
        lh._save(lh.cdir("f", sym, f"{y}-{m:02d}.npy"), np.array([[t, 0.0001] for t in fts]))
lf._write_json(lh.cdir("symbols.json"), {"fetched_at": 0, "symbols": SYMS})
check("CACHE: the history is in the study's layout and counts as ready", rk.history_ready() and rk.last_archive_month("S3USDT") == "2025-02")

NOW_MS = [0]
calls = {"klines": 0}


class Resp:
    def __init__(self, js, code=200):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


def fake_get(url, params=None, timeout=None, **kw):
    params = params or {}
    if url.endswith("/fapi/v1/exchangeInfo"):
        return Resp({"symbols": [{"symbol": s, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"} for s in SYMS + ["NOTCACHEDUSDT"]]})
    if url.endswith("/fapi/v1/ticker/24hr"):
        return Resp([{"symbol": s, "quoteVolume": str(1e9 - i)} for i, s in enumerate(SYMS + ["NOTCACHEDUSDT"])])
    if url.endswith("/fapi/v1/klines"):
        calls["klines"] += 1
        sym = params["symbol"]
        if sym not in SER:
            return Resp([])
        rows = rows_of(sym, int(params["startTime"]), NOW_MS[0])[: int(params.get("limit", 1500))]
        return Resp([[int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), "1", int(r[0]) + HOUR - 1, str(r[5]), int(r[6]), "1", str(r[7]), "0"] for r in rows])
    if url.endswith("/fapi/v1/fundingRate"):
        a = int(params["startTime"])
        ev = [{"fundingTime": t, "fundingRate": "0.0001"} for t in range(a - a % (8 * HOUR) + (8 * HOUR if a % (8 * HOUR) else 0), NOW_MS[0], 8 * HOUR)]
        return Resp(ev[: int(params.get("limit", 1000))])
    return Resp(None, 404)


def at(y, m, d, h=0, mi=20):
    t = datetime(y, m, d, h, mi, tzinfo=UTC)
    NOW_MS[0] = int(t.replace(minute=0).timestamp() * 1000) + HOUR       # the API returns the bar of the current hour as well (its open is known)
    return t


# ======================================================================= tail refresh
now1 = at(2025, 3, 20)
res = rk.refresh_tail(fake_get, now1)
tail = lh.cdir("k", "S3USDT", "tail.npy")
check("TAIL: the most liquid cached contracts get their recent hours from the exchange API (from the first day after the last archive month); a contract that is not cached is skipped", res["symbols"] == len(SYMS) and res["errors"] == 0 and tail.is_file()
      and int(np.load(tail)[0, 0]) == int(datetime(2025, 3, 1, tzinfo=UTC).timestamp() * 1000) and not lh.cdir("k", "NOTCACHEDUSDT").exists())
check("TAIL: funding events are stored with it", lh.cdir("f", "S3USDT", "tail.npy").is_file() and np.load(lh.cdir("f", "S3USDT", "tail.npy")).shape[1] == 2)

# ======================================================================= model
d = rk.rdir(tmp / "data")
meta = rk.train_model(now1, tmp / "data")
check("MODEL: trained on the cache's labelled rows, saved with its metadata, loads back", (d / "model.txt").is_file() and meta["rows"] > 500 and rk.load_model(tmp / "data") is not None and meta["features"] == lh.FEATURES)
check("MODEL: not due right after training, due when older than 28 days or missing", not rk.model_due(now1, tmp / "data") and rk.model_due(now1 + timedelta(days=29), tmp / "data") and rk.model_due(now1, tmp / "elsewhere"))

# ======================================================================= the daily job
check("JOB: with RANKER_MODE off nothing runs (and nothing is touched)", rk.run_once(fake_get, now1, tmp / "data")["reason"] == "RANKER_MODE is off")
os.environ["RANKER_MODE"] = "shadow"
r1 = rk.run_once(fake_get, now1, tmp / "data")
st1 = json.loads((d / "state.json").read_text(encoding="utf-8"))
led1 = rk._lines(d / "ledger.jsonl")
check("DAY 1: the job ran, ranked today, opened the paper books (3 long, 3 short), logged one rebalance with the full gross as turnover and the 0.5 start scale for B",
      r1["ran"] and r1["rebalanced"] and sum(1 for v in st1["A"].values() if v > 0) == 3 and sum(1 for v in st1["A"].values() if v < 0) == 3 and len(led1) == 1 and led1[0]["kind"] == "rebalance"
      and abs(led1[0]["turnover_A"] - 1.0) < 1e-9 and abs(led1[0]["turnover_B"] - 0.5) < 1e-9 and st1["scale"] == 0.5)
check("DAY 1: a ranking line with all scored coins was stored, status ok", len(rk._lines(d / "rankings.jsonl")) == 1 and len(rk._lines(d / "rankings.jsonl")[0]["scores"]) >= 6 and json.loads((d / "status.json").read_text(encoding="utf-8"))["state"] == "ok")
check("DAY 1: running again the same day does nothing", rk.run_once(fake_get, now1, tmp / "data")["reason"] == "already ran today")
check("DAY 1: before 00:15 UTC it waits", rk.run_once(fake_get, at(2025, 3, 21, 0, 5), tmp / "data")["reason"] == "before 00:15 UTC")

now2 = at(2025, 3, 21)
r2 = rk.run_once(fake_get, now2, tmp / "data")
led2 = [x for x in rk._lines(d / "ledger.jsonl") if x["kind"] == "day"]
# expected gross of 20 March: weights x (open-to-open return - funding of that day)
idx = lambda day: int(np.flatnonzero(TS == int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).timestamp() * 1000))[0])      # noqa: E731
k20, k21 = idx("2025-03-20"), idx("2025-03-21")
exp = sum(wt * (SER[s][0][k21] / SER[s][0][k20] - 1.0 - 0.0003) for s, wt in st1["A"].items())
check("DAY 2: the first day is marked with the study's arithmetic (weight x (open-to-open return - the day's funding)); no rebalance yet (3-day hold)", r2["ran"] and not r2["rebalanced"] and len(led2) == 1
      and led2[0]["date"] == "2025-03-20" and abs(led2[0]["gross_A"] - exp) < 1e-6 and abs(led2[0]["gross_B"] - 0.5 * exp) < 1e-6)       # prices are float32 in the matrices, as in the study
for day in (22, 23):
    r = rk.run_once(fake_get, at(2025, 3, day), tmp / "data")
check("DAY 4: three days after the first rebalance the book is rebalanced again, the earlier ranking is scored against what really happened (rank IC)", r["rebalanced"] and r["ic"] == 1
      and len(rk._lines(d / "ic.jsonl")) == 1 and rk._lines(d / "ic.jsonl")[0]["date"] == "2025-03-20" and rk._lines(d / "ic.jsonl")[0]["ic"] is not None)
reb = [x for x in rk._lines(d / "ledger.jsonl") if x["kind"] == "rebalance"]
check("DAY 4: the second rebalance has a turnover between 0 and 2 and records the longs and shorts", len(reb) == 2 and 0.0 <= reb[1]["turnover_A"] <= 2.0 and len(reb[1]["longs"]) == 3 and len(reb[1]["shorts"]) == 3)
check("DAY 4: three marked days in the ledger (20, 21, 22 March)", [x["date"] for x in rk._lines(d / "ledger.jsonl") if x["kind"] == "day"] == ["2025-03-20", "2025-03-21", "2025-03-22"])
# book C: limit-order execution evaluated from the real hourly bars of the rebalance morning
ledger_now = rk._lines(d / "ledger.jsonl")
reb_rows = [x for x in ledger_now if x["kind"] == "rebalance"]
pas_rows = [x for x in ledger_now if x["kind"] == "passive"]
amts = [abs(o_["d"]) for o_ in reb_rows[0]["orders"]]
check("BOOK C: every rebalance row carries its orders (symbol and signed weight change); the first one trades the whole book (gross 1.0)", len(reb_rows[0]["orders"]) == 6 and abs(sum(amts) - 1.0) < 1e-5)       # weights are stored with 6 decimals
check("BOOK C: the first rebalance was evaluated a day later (the second rebalance, of the same day as the job, is still pending)", [x["date"] for x in pas_rows] == ["2025-03-20"] and "orders" in reb_rows[1])
check("BOOK C: in the synthetic market the hourly range (+-0.2%) always reaches a limit 0.1% away, so all orders fill and earn delta minus the maker fee on the whole notional", pas_rows[0]["filled"] == pas_rows[0]["orders"] == 6
      and pas_rows[0]["chased"] == 0 and abs(pas_rows[0]["filled_gain"] - sum(amts) * (rk.PASSIVE_DELTA - rk.mk.FEE_MAKER / 100.0)) < 1e-6)

# server was down for four days
r = rk.run_once(fake_get, at(2025, 3, 27), tmp / "data")
days_marked = [x["date"] for x in rk._lines(d / "ledger.jsonl") if x["kind"] == "day"]
check("CATCH-UP: after four days of downtime every missing day is marked from the cache with the positions that were held (23..26 March)", days_marked[-4:] == ["2025-03-23", "2025-03-24", "2025-03-25", "2025-03-26"] and len(days_marked) == 7 and r["rebalanced"])

# ======================================================================= what the dashboard gets
snap = rk.snapshot(tmp / "data")
check("SNAPSHOT: mode, status, model, today's ranking (top / bottom), both books, forward statistics at two slippages and a computed verdict",
      snap["mode"] == "shadow" and snap["status"]["state"] == "ok" and snap["model"]["rows"] > 500 and len(snap["ranking"]["top"]) == 3 and len(snap["ranking"]["bottom"]) == 3
      and snap["book"]["A"] and snap["book"]["B"] and snap["forward"]["base"]["days"] == 7 and snap["forward"]["stress"]["slippage_pct"] == 0.30 and snap["verdict"] == "collecting"
      and snap["thresholds"]["min_days"] == 56 and snap["study"]["oos_max_dd_pct"] == 54.2)
check("SNAPSHOT: book B is book A scaled (same coins, smaller weights)", set(snap["book"]["A"]) == set(snap["book"]["B"]) and all(abs(abs(snap["book"]["B"][s]) - abs(snap["book"]["A"][s]) * snap["book"]["scale"]) < 1e-9 for s in snap["book"]["A"]))
json.dumps(snap)       # serialisable

# a failing run is reported, not raised
os.environ["RANKER_MODE"] = "shadow"
(d / "model.txt").unlink()
(d / "model_meta.json").unlink()
bad = rk.run_once(lambda *a, **k: Resp(None, 500), at(2025, 3, 28), tmp / "data")
check("FAILURE: a job that cannot run returns a reason and sets status failed instead of raising", bad["ran"] is False and json.loads((d / "status.json").read_text(encoding="utf-8"))["state"] in ("failed", "downloading"))

# ======================================================================= wiring
src = lambda p: (REPO / p).read_text(encoding="utf-8")   # noqa: E731
app = src("backend/app.py")
check("WIRING: routes, 30-minute loop and the start-up task exist; the manual run refuses while off, running or a replay job runs", "/api/ranker" in app and "/api/ranker/run" in app and "_ranker_loop" in app
      and "asyncio.create_task(_ranker_loop())" in app and "RANKER_MODE is off" in app and "already running" in app)
html = src("frontend/index.html")
check("INTERFACE: a Ranker section with the mode switch, run button, verdict, books, IC, equity line and the ranking tables", all(x in html for x in ("ranker-shadow", "loadRanker", "rankerMode", "rankerRun", "rankerSpark", "forward verdict", "paper book", "bottom 8", "/api/ranker", "C = A with limit orders", "Book C trades the positions of A")) and "loadRanker();" in html)
check("COMPOSE / ENV / DOCS: RANKER_MODE is passed (default off), in the import file and documented", "RANKER_MODE=${RANKER_MODE:-off}" in src("docker-compose.yml") and "RANKER_MODE=off" in src(".env.example") and "RANKER_MODE" in src("ENVIRONMENT_VARIABLES.md"))
check("SAFETY: the module never imports or calls anything that trades or touches a bot", not any(x in src("backend/engine/ranker.py") for x in ("BotControl", "freqtrade_client", "stopentry", "forcebuy", "forceenter", "/api/v1")))

print("\nALL P26 RANKER CHECKS PASSED" if not failures else f"\n{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
