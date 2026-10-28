"""
P20.4 part B: kronos-signal. Offline: the pure logic with a FAKE forecaster (no torch, no network), the ledger/audit integration, the compose wiring and the
image pins. The model itself was measured separately on a dev machine (see AGENTS.md); it is not run here.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
KDATA, SDATA = tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"DATA_DIR": SDATA, "KRONOS_DIR": KDATA, "BOTS_DIR": tempfile.mkdtemp()})
for k in ("SIGNAL_AUDIT_MODE", "ALLOCATOR_RETIRED_BOTS"):
    os.environ.pop(k, None)

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


spec = importlib.util.spec_from_file_location("kronos_core", REPO / "kronos-signal/app/core.py")
kc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kc)

from backend.engine import allocator as al  # noqa: E402
from backend.engine import signal_audit as sa  # noqa: E402

NOW = datetime(2026, 10, 5, 14, 2, 30, tzinfo=timezone.utc)
T0 = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)


def payload(pair_seed: int, t0: datetime = T0, n: int = 520, drop_last_closed: bool = False, forming: bool = True, gap_at: int | None = None):
    """A Freqtrade /pair_candles payload ending at the candle opened at t0-1h (+ the forming candle opened at t0 if `forming`)."""
    rows = []
    last_closed_open = t0 - timedelta(hours=1)
    for i in range(n):
        if gap_at is not None and i == gap_at:
            continue
        o = last_closed_open - timedelta(hours=n - 1 - i)
        px = 100.0 + pair_seed + i * 0.01
        rows.append([o.isoformat().replace("+00:00", "Z"), px, px * 1.01, px * 0.99, px * 1.001, 1000.0 + pair_seed * 10, "indicator"])
    if drop_last_closed:
        rows.pop()
    if forming:
        px = 9999.0                       # the still-forming candle must never reach the model
        rows.append([t0.isoformat().replace("+00:00", "Z"), px, px, px, px, 1.0, "indicator"])
    return {"columns": ["date", "open", "high", "low", "close", "volume", "rsi"], "data": [[r[0], r[1], r[2], r[3], r[4], r[5], 50.0] for r in rows]}


# ---------------------------------------------------------------- candles and look-ahead
rows = kc.parse_candles(payload(1))
check("parse_candles: sorted rows with open time, OHLCV; extra columns ignored", len(rows) == 521 and rows[0]["open_dt"] < rows[-1]["open_dt"] and {"open", "high", "low", "close", "volume"} <= set(rows[0]))
check("parse_candles: ms timestamps and bad rows are handled", len(kc.parse_candles({"columns": ["date", "open", "high", "low", "close", "volume"],
      "data": [[1790000000000, 1, 2, 0.5, 1.5, 10], ["garbage", 1, 1, 1, 1, 1], [1790003600000, None, 1, 1, 1, 1]]})) == 1)
check("parse_candles: junk payloads -> []", kc.parse_candles(None) == [] and kc.parse_candles({"columns": ["x"], "data": []}) == [])
cl = kc.closed_rows(rows, T0)
check("NO LOOK-AHEAD: the forming candle (opened at t0) is dropped; the newest closed candle opened at t0-1h", len(cl) == 520 and cl[-1]["open_dt"] == T0 - timedelta(hours=1)
      and all(r["close"] != 9999.0 for r in cl))
win, why = kc.usable_window(rows, T0)
check("usable_window: exactly LOOKBACK (480) closed, gap-free candles, newest = t0-1h, the 9999 forming price is not in it",
      win is not None and why is None and len(win) == kc.LOOKBACK == 480 and win[-1]["open_dt"] == T0 - timedelta(hours=1) and 9999.0 not in [r["close"] for r in win])
check("spec: the lookback is at least 400 candles", kc.LOOKBACK >= 400 and kc.MIN_ROWS >= 400)
old = kc.parse_candles(payload(1, t0=T0 - timedelta(hours=2)))
w2, why2 = kc.usable_window(old, T0)
check("STALE: the newest closed candle is older than t0-1h -> the pair is skipped, never forecast from old data", w2 is None and "stale_candles" in why2)
w3, why3 = kc.usable_window(kc.parse_candles(payload(1, n=300)), T0)
check("too few closed candles (< 400) -> skipped with the count", w3 is None and "only 300" in why3)
w4, why4 = kc.usable_window(kc.parse_candles(payload(1, gap_at=300)), T0)
check("a gap inside the window -> skipped", w4 is None and "gap" in why4)

# ---------------------------------------------------------------- decisions and returns
fee, thr = 0.002, 0.005
check("expected returns at 1/4/24h from the forecast closes", kc.expected_returns([101.0 + 0 * i for i in range(24)], 100.0) == {"ret_1h": 0.01, "ret_4h": 0.01, "ret_24h": 0.01}
      or all(abs(v - 0.01) < 1e-12 for v in kc.expected_returns([101.0] * 24, 100.0).values()))
r = kc.expected_returns([100 + h for h in range(1, 25)], 100.0)
check("returns index the candle h hours after t0 (h-1)", abs(r["ret_1h"] - 0.01) < 1e-12 and abs(r["ret_4h"] - 0.04) < 1e-12 and abs(r["ret_24h"] - 0.24) < 1e-12)
for bad in ([100.0] * 10,):
    try:
        kc.expected_returns(bad, 100.0)
        okv = False
    except ValueError:
        okv = True
    check("a forecast shorter than 24 candles is rejected", okv)
check("DECIDE buy: +0.8% expected 4h return minus 0.2% fee = 0.6% > 0.5% threshold", kc.decide(0.008, thr, fee) == "buy")
check("DECIDE hold: +0.7% - fee = 0.5% is NOT above the threshold (strict)", kc.decide(0.007, thr, fee) == "hold")
check("DECIDE sell_short: -0.8% -> short net 0.6% > threshold", kc.decide(-0.008, thr, fee) == "sell_short")
check("DECIDE hold inside the band, both signs", kc.decide(0.004, thr, fee) == "hold" and kc.decide(-0.004, thr, fee) == "hold" and kc.decide(0.0, thr, fee) == "hold")
check("the threshold is a variable (KRONOS_THRESHOLD) and the default is 0.5%", kc.config_from_env({})["threshold"] == 0.005 and kc.config_from_env({"KRONOS_THRESHOLD": "0.01"})["threshold"] == 0.01)
cfg = kc.config_from_env({"KRONOS_OUT_DIR": KDATA})
rec = kc.make_record("BTC/USDT", "main", T0, NOW, 100.0, {"ret_1h": 0.001, "ret_4h": -0.012, "ret_24h": -0.03}, cfg, {"n_candles": 480})
check("RECORD (ledger format): sid, ts, t0, bot=kronos, pair, action, confidence=|ret4h|, price=last close, src_bot, raw forecasts and parameters",
      rec["sid"] == "kronos|BTC/USDT|2026-10-05T14:00:00Z" and rec["bot"] == "kronos" and rec["t0"] == "2026-10-05T14:00:00Z" and rec["action"] == "sell_short"
      and rec["confidence"] == 0.012 and rec["price"] == 100.0 and rec["src_bot"] == "main"
      and rec["kronos"]["ret_1h"] == 0.001 and rec["kronos"]["ret_4h"] == -0.012 and rec["kronos"]["ret_24h"] == -0.03
      and rec["kronos"]["seed"] == 12345 and rec["kronos"]["samples"] == 3 and rec["kronos"]["threshold"] == 0.005)

# ---------------------------------------------------------------- universe
WL = {"main": ["BTC/USDT", "ETH/USDT", "HBAR/USDT"], "moonshot": ["BTC/USDT"],
      "shorts": ["BTC/USDT:USDT", "ZEC/USDT:USDT", "SOL/USDT:USDT", "DOGE/USDT:USDT"], "listing_fade": ["PUMP/USDT:USDT"], "trend_v2": []}
WL_SKIP = {**WL, "moonshot": ["XRP/USDT", "BTC/USDT"]}
src = kc.pick_sources(WL_SKIP, skip_bots=["moonshot"])
check("UNIVERSE: one pair per coin, spot preferred (BTC/USDT from main, not the futures pair), futures only for coins with no spot pair",
      src["BTC/USDT"] == "main" and "BTC/USDT:USDT" not in src and src["SOL/USDT:USDT"] == "shorts" and src["ZEC/USDT:USDT"] == "shorts" and src["DOGE/USDT:USDT"] == "shorts")
check("UNIVERSE: a skipped bot (moonshot) is never a source and its exclusive pairs vanish", "XRP/USDT" not in src and "moonshot" not in src.values())
check("UNIVERSE: the union covers all active whitelists", set(src) == {"BTC/USDT", "ETH/USDT", "HBAR/USDT", "SOL/USDT:USDT", "ZEC/USDT:USDT", "DOGE/USDT:USDT", "PUMP/USDT:USDT"})
tv = {"A": 5.0, "B": 50.0, "C": 0.0, "D": float("nan"), "E": 20.0}
check("RANKING by 24h turnover: most liquid first, zero/NaN never ranked, capped at n", kc.rank_universe(tv, 2) == ["B", "E"] and kc.rank_universe(tv, 10) == ["B", "E", "A"])
check("spec (P23): default universe 45 pairs (the bots' whole whitelists), cycle budget 15 minutes -- 45 x ~11.4 s fits well inside the hour", kc.config_from_env({})["max_pairs"] == 45 and kc.config_from_env({})["cycle_budget_s"] <= 900)
check("turnover uses only closed candles (24h of close x volume)", abs(kc.turnover_24h(rows, T0) - sum(r["close"] * r["volume"] for r in kc.closed_rows(rows, T0)[-24:])) < 1e-6)


# ---------------------------------------------------------------- a full cycle with a fake model
class Rest:
    def __init__(self, fail=(), forming=True):
        self.calls, self.fail, self.forming = [], set(fail), forming

    def __call__(self, bot, path):
        self.calls.append((bot, path))
        if bot in self.fail:
            return None
        if path == "/api/v1/whitelist":
            return {"whitelist": WL.get(bot, [])}
        if "/pair_candles" in path:
            from urllib.parse import parse_qs, unquote, urlparse
            pair = unquote(parse_qs(urlparse(path).query)["pair"][0])
            seed = {"BTC/USDT": 90, "ETH/USDT": 60, "SOL/USDT:USDT": 30, "HBAR/USDT": 1, "ZEC/USDT:USDT": 10, "DOGE/USDT:USDT": 5, "PUMP/USDT:USDT": 0}[pair]
            return payload(seed, forming=self.forming)
        return None


forecasts = {}


def fake_predict(window, t0):
    forecasts[window[-1]["close"]] = (len(window), window[-1]["open_dt"])
    last = window[-1]["close"]
    return [last * (1 + 0.003 * h) for h in range(1, 25)]       # +0.3% per hour -> +1.2% at 4h -> buy


out = Path(KDATA) / "kronos_signals.jsonl"
rest = Rest()
clk = iter(range(0, 10_000, 2))
s = kc.run_cycle(NOW, rest, fake_predict, {**cfg, "max_pairs": 25}, out, clock=lambda: next(clk))
recs = [json.loads(line) for line in out.read_text().splitlines()]
check("CYCLE: all candidate pairs scored, one record each, t0 is the hour boundary", s["pairs_scored"] == 7 == len(recs) and s["t0"] == "2026-10-05T14:00:00Z"
      and {r["t0"] for r in recs} == {"2026-10-05T14:00:00Z"} and s["truncated"] is False)
check("CYCLE: the model always got exactly 480 closed candles ending at t0-1h (never the forming one)",
      all(n == 480 and dt == T0 - timedelta(hours=1) for n, dt in forecasts.values()))
check("CYCLE: ranked by turnover -- BTC first", s["universe"][0] == "BTC/USDT" and s["universe"][-1] in ("PUMP/USDT:USDT",))
check("CYCLE: each record has action buy (+1.2% at 4h > 0.5% after fee), confidence 0.012, raw 1/4/24h returns",
      all(r["action"] == "buy" and abs(r["confidence"] - 0.012) < 1e-9 and abs(r["kronos"]["ret_24h"] - 0.072) < 1e-9 for r in recs) and s["actions"] == {"buy": 7, "sell_short": 0, "hold": 0})
check("CYCLE: read-only towards the bots -- only GET /whitelist and /pair_candles", {p.split("?")[0] for _, p in rest.calls} == {"/api/v1/whitelist", "/api/v1/pair_candles"})
s2 = kc.run_cycle(NOW + timedelta(minutes=3), Rest(), fake_predict, {**cfg}, out, clock=lambda: 0)
check("APPEND-ONLY / dedupe: the same hour again writes nothing new (a restart inside the hour is harmless)", s2["records_written"] == 0 and len(out.read_text().splitlines()) == 7)
s3 = kc.run_cycle(NOW + timedelta(hours=1), Rest(forming=False), fake_predict, {**cfg}, out, clock=lambda: 0)    # the bots' REST has not produced the new candle yet
check("the next hour with the old candles: every pair is 'stale_candles' -> nothing written, nothing guessed", s3["pairs_scored"] == 0 and all("stale_candles" in v for v in s3["skipped"].values())
      and len(out.read_text().splitlines()) == 7)
check("hour_done sees the written hour (restart guard)", kc.hour_done(out, T0) and not kc.hour_done(out, T0 + timedelta(hours=1)))
bad_out = Path(KDATA) / "bad.jsonl"
s4 = kc.run_cycle(NOW, Rest(fail=["main"]), fake_predict, {**cfg}, bad_out, clock=lambda: 0)
got = {r["pair"]: r["src_bot"] for r in map(json.loads, bad_out.read_text().splitlines())}
check("a bot that does not answer: BTC fails over to the next spot bot that lists it (moonshot); coins only that bot lists are simply absent; the rest is scored",
      got.get("BTC/USDT") == "moonshot" and "ETH/USDT" not in got and "HBAR/USDT" not in got and s4["pairs_scored"] > 0)


def boom(window, t0):
    raise RuntimeError("model blew up")


s5 = kc.run_cycle(NOW, Rest(), boom, {**cfg}, Path(KDATA) / "boom.jsonl", clock=lambda: 0)
check("a forecast that raises is recorded per pair and never kills the cycle", s5["pairs_scored"] == 0 and len(s5["skipped"]) == 7 and all("forecast failed" in v for v in s5["skipped"].values()))
tick = iter([0, 0, 0, 100, 700, 700, 700, 700, 700, 700, 700, 700, 700])
s6 = kc.run_cycle(NOW, Rest(), fake_predict, {**cfg, "cycle_budget_s": 540}, Path(KDATA) / "budget.jsonl", clock=lambda: next(tick, 999))
check("TIME BUDGET: once 540s are used no new pair is started; the rest is listed as skipped and the cycle says truncated", s6["truncated"] is True
      and any("budget" in v for v in s6["skipped"].values()) and s6["pairs_scored"] < 7)
nan_closes = lambda w, t: [float("nan")] * 24  # noqa: E731
check("a NaN forecast is rejected per pair (a NaN would otherwise become an invalid JSON record)", kc.run_cycle(NOW, Rest(), nan_closes, {**cfg}, Path(KDATA) / "nan.jsonl", clock=lambda: 0)["pairs_scored"] == 0)

# ---------------------------------------------------------------- ledger / audit integration
Path(SDATA).mkdir(exist_ok=True)
seen: set = set()
ing = sa.ingest_kronos(out, seen)
check("INGEST: kronos signals enter the ledger with their raw forecast fields; each (pair, t0) once", len(ing) == 7 and all(i["bot"] == "kronos" and i["kronos"]["ret_4h"] for i in ing)
      and sa.ingest_kronos(out, seen) == [])
junk = Path(KDATA) / "junk.jsonl"
junk.write_text("not json\n" + json.dumps({"sid": "x", "bot": "kronos", "t0": "2026-10-05T14:00:00Z", "ts": "2026-10-05T14:02:00Z", "pair": "A/USDT", "action": "yolo"}) + "\n"
                + json.dumps({"sid": "y", "bot": "main", "t0": "2026-10-05T14:00:00Z", "ts": "2026-10-05T14:02:00Z", "pair": "A/USDT", "action": "buy"}) + "\n")
check("INGEST: malformed lines, unknown actions and other bots' records are dropped", sa.ingest_kronos(junk, set()) == [])
check("kronos is an audited source but its signals are never parsed from a log", "kronos" in sa.AUDITED_BOTS and "kronos" not in sa.LLM_BOTS)


def synth(n_hours=24 * 20, pairs=6, edge=0.01, bot="kronos"):
    """kronos signals: buys beat the cross-sectional mean by `edge` (when edge != 0), shorts by the same; forward returns supplied directly."""
    sigs, fwd = [], []
    t = datetime(2026, 9, 1, tzinfo=timezone.utc)
    state = [4242]

    def rnd():
        state[0] = (state[0] * 1103515245 + 12345) % 2 ** 31
        return state[0] / 2 ** 31 - 0.5
    for i in range(n_hours):
        t0 = t + timedelta(hours=i)
        market = rnd() * 0.01
        for p in range(pairs):
            kind = ("buy", "sell_short", "hold")[p % 3]
            conf = abs(rnd()) * 0.02 + 0.006
            ret = market + rnd() * 0.002 + (edge if kind == "buy" else -edge if kind == "sell_short" else 0.0)
            sid = f"{bot}|P{p}/USDT|{sa._iso(t0)}"
            sigs.append({"sid": sid, "ts": sa._iso(t0 + timedelta(minutes=3)), "t0": sa._iso(t0), "bot": bot, "pair": f"P{p}/USDT", "action": kind, "confidence": conf,
                         "price": 100.0, "src_bot": "main"})
            for h in sa.HORIZONS:
                fwd.append({"sid": sid, "h": h, "price0": 100.0, "price_h": 100 * (1 + ret), "ret": ret})
    return sigs, fwd


NOWA = datetime(2026, 11, 20, tzinfo=timezone.utc)
sigs, fwd = synth()                                    # 20 days, 160 buys + 160 shorts per ... see counts below
aud = sa.build_audit(sigs, fwd, NOWA)
kb = aud["bots"]["kronos"]
check("AUDIT: kronos appears as a bot next to the six LLM bots, with per-direction n, hit rate, excess, CI and rank correlation",
      "kronos" in aud["bots"] and kb["by_direction"]["buy"]["status"] == "ok" and {"n", "hit_rate", "mean_excess_pct", "excess_ci95_pct", "conf_rank_corr"} <= set(kb["by_direction"]["buy"]["h4"]))
check("AUDIT: the six LLM bots are still there (and still 'not enough data' without signals)", set(aud["bots"]) == set(sa.AUDITED_BOTS) and aud["bots"]["main"]["status"] == "not enough data")
check("AUDIT: excess is measured against the cross-sectional mean of the pairs scored at the same t0 (a +1% built-in edge shows as ~+1%)",
      0.5 < kb["by_direction"]["buy"]["h4"]["mean_excess_pct"] < 1.5 and 0.5 < kb["by_direction"]["sell_short"]["h4"]["mean_excess_pct"] < 1.5)
check("AUDIT: the rank correlation uses |forecast| (confidence) vs the realised directional return", kb["by_direction"]["buy"]["h4"]["conf_n"] > 50)
v = kb["verdict"]["directions"]
check("VERDICT edge: >= 200 actions per direction, >= 14 days, CI lower bound > 0 at 4h/24h and in BOTH halves", v["buy"]["status"] == "edge" and v["sell_short"]["status"] == "edge")
sig_few, fwd_few = synth(n_hours=24 * 10)
vf = sa.build_audit(sig_few, fwd_few, NOWA)["bots"]["kronos"]["verdict"]["directions"]
check("VERDICT 'not enough data': 10 days (< 14) -> no conclusion even with 240 actions", vf["buy"]["status"] == "not enough data" and vf["buy"]["needed_days"] == 14)
sig_n, fwd_n = synth(n_hours=24 * 14 + 1, pairs=3)      # 1 buy + 1 short per hour: 337 actions but check the 200 gate with fewer
sig_small, fwd_small = [s for s in sigs if s["t0"] < "2026-09-05"], [f for f in fwd if f["sid"].split("|")[2] < "2026-09-05"]
vs = sa.build_audit(sig_small, fwd_small, NOWA)["bots"]["kronos"]["verdict"]["directions"]
check("VERDICT 'not enough data': fewer than 200 actions per direction -> no conclusion", vs["buy"]["status"] == "not enough data" and vs["buy"]["actions"] < 200)
sig_ne, fwd_ne = synth(edge=0.0)
vn = sa.build_audit(sig_ne, fwd_ne, NOWA)["bots"]["kronos"]["verdict"]["directions"]
check("VERDICT: no built-in edge -> never 'edge' (unproven)", vn["buy"]["status"] == "unproven" and vn["sell_short"]["status"] == "unproven")
sig_neg, fwd_neg = synth(edge=-0.01)
vng = sa.build_audit(sig_neg, fwd_neg, NOWA)["bots"]["kronos"]["verdict"]["directions"]
check("VERDICT: every upper bound below zero -> no_edge", vng["buy"]["status"] == "no_edge")


def synth_split():
    """edge only in the FIRST half of the period: the full-period CI is positive but the second half is not -> must not be 'edge'."""
    sigs_, fwd_ = synth(edge=0.0)
    first_cut = sorted({s["t0"] for s in sigs_})[len({s["t0"] for s in sigs_}) // 2]
    bump = {}
    for s in sigs_:
        if s["t0"] < first_cut and s["action"] in ("buy", "sell_short"):
            bump[s["sid"]] = 0.03 if s["action"] == "buy" else -0.03
    for f in fwd_:
        if f["sid"] in bump:
            f["ret"] += bump[f["sid"]]
    return sigs_, fwd_


sig_sp, fwd_sp = synth_split()
vsp = sa.build_audit(sig_sp, fwd_sp, NOWA)["bots"]["kronos"]["verdict"]["directions"]
check("VERDICT: an edge that exists in only one half of the period is NOT 'edge'", vsp["buy"]["status"] != "edge" and vsp["buy"]["checks"]["h4"]["ci95_pct"][0] > 0
      and (vsp["buy"]["checks"]["h4"]["half2_lo_pct"] is None or vsp["buy"]["checks"]["h4"]["half2_lo_pct"] <= 0))
check("the verdict states its criteria and that using kronos as a filter comes only after", "both halves" in aud["bots"]["kronos"]["verdict"]["criteria"] and "filter" in aud["bots"]["kronos"]["verdict"]["criteria"])

# nightly run end to end with a fake bot REST
class Fetcher:
    def __init__(self):
        self.calls = []

    def _json(self, bot, path):
        self.calls.append((bot, path))
        if "/pair_candles" in path:
            rows_ = []
            base = datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc)
            for h in range(0, 24 * 5):
                o = base + timedelta(hours=h)
                rows_.append([o.isoformat().replace("+00:00", "Z"), 0, 0, 0, 100.0 + h * 0.1, 1.0])
            return {"columns": ["date", "open", "high", "low", "close", "volume"], "data": rows_}
        return None


fx = Fetcher()
sdata = Path(tempfile.mkdtemp())
res = sa.run_nightly(sdata, now=datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc), fetcher=fx, log_files_fn=lambda bot: [])
led = [json.loads(line) for line in (sdata / "signals.jsonl").read_text().splitlines()]
check("NIGHTLY: kronos signals land in signals.jsonl with their forecast fields; candles are fetched through the SOURCE bot's REST (main), not a 'kronos' bot",
      len(led) == 7 and all(s["bot"] == "kronos" for s in led) and all(c[0] in ("main", "moonshot", "shorts", "listing_fade") for c in fx.calls)
      and not any(c[0] == "kronos" for c in fx.calls))
fwd_led = [json.loads(line) for line in (sdata / "signals_forward.jsonl").read_text().splitlines()]
check("NIGHTLY: forward returns are resolved from candles after t0 only (1h and 4h matured at 20:00; 24h not yet)", {f["h"] for f in fwd_led} == {1, 4}
      and all(abs(f["ret"] - (f["price_h"] / f["price0"] - 1)) < 1e-12 for f in fwd_led))
check("NIGHTLY: the audit file carries a kronos entry", "kronos" in json.loads((sdata / "signals_audit.json").read_text())["bots"])
os.environ["ALLOCATOR_RETIRED_BOTS"] = "main"
fx2 = Fetcher()
sa.run_nightly(Path(tempfile.mkdtemp()), now=datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc), fetcher=fx2, log_files_fn=lambda bot: [])
check("a retired bot is not polled for candles by the audit (even as the source of kronos candles)", not any(c[0] == "main" for c in fx2.calls))
os.environ.pop("ALLOCATOR_RETIRED_BOTS")

# ---------------------------------------------------------------- never managed, no trading
check("kronos is in NEVER_MANAGED and cannot be given a weight or paused", "kronos" in al.NEVER_MANAGED and "kronos" not in al.MANAGED_BOTS)
raised = False
try:
    al.assert_manageable("kronos")
except al.ControlBotError:
    raised = True
check("assert_manageable('kronos') raises", raised)
v = al.validate_llm(json.dumps({"kronos": {"weight": 0.0, "reason": "x"}, "main": {"weight": 0.5, "reason": "y"}}), {b: 1.0 for b in al.MANAGED_BOTS})
check("an allocator reply naming kronos is ignored", v["ok"] and "kronos" not in v["weights"] and any("kronos" in n for n in v["notes"]))
code = "\n".join((REPO / "kronos-signal/app" / f).read_text(encoding="utf-8") for f in ("core.py", "runner.py", "predictor.py"))
check("the service has no code path that trades or controls a bot: no POST/PUT/DELETE, no order/stopentry/start/force calls, no keys of any exchange",
      not any(t in code for t in (".post(", ".put(", ".delete(", "forceenter", "forceexit", "stopentry", "/api/v1/start", "BYBIT_API", "create_order")))
check("it only reads two bot endpoints: whitelist and pair_candles", code.count("/api/v1/") == 2 and "/api/v1/whitelist" in code and "pair_candles" in code)

# ---------------------------------------------------------------- compose / image pins
cy = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
ks = cy["services"].get("kronos-signal")
check("COMPOSE: a separate kronos-signal service built from ./kronos-signal", ks is not None and ks["build"]["context"] == "./kronos-signal")
check("COMPOSE: CPU and memory limited so the bots are not disturbed (cpus <= 2, mem_limit <= 1g, lowered cpu_shares, pids limit)",
      float(ks["cpus"]) <= 2 and ks["mem_limit"] in ("1g", "1024m") and int(ks["cpu_shares"]) < 1024 and int(ks["pids_limit"]) > 0)
check("COMPOSE: no published ports (nothing listens), restart policy, own data volume", "ports" not in ks and ks["restart"] == "unless-stopped" and "kronos-data:/kronos" in ks["volumes"])
check("COMPOSE: the container gets the bots' REST credentials from the same variables and nothing else secret",
      "FREQTRADE_API_USERNAME=${FREQTRADE_API_USERNAME:-}" in ks["environment"] and not any("KEY" in e and "FREQTRADE_API" not in e for e in ks["environment"]))
check("COMPOSE: survival-agent reads the signals read-only", "kronos-data:/kronos:ro" in cy["services"]["survival-agent"]["volumes"] and "kronos-data" in cy["volumes"])
check("COMPOSE: no bot container got the kronos volume (P22-K2's kronos-replay has its OWN volume kronos-replay-data and never the live kronos-data)", not any("kronos" in v for name, s in cy["services"].items() if name not in ("kronos-signal", "survival-agent", "kronos-replay") for v in s.get("volumes", []))
      and cy["services"]["kronos-replay"]["volumes"] == ["kronos-replay-data:/kronos-replay"])
check("COMPOSE: threshold, samples, max pairs, mode and skip list are variables", all(any(e.startswith(k + "=") for e in ks["environment"])
      for k in ("KRONOS_MODE", "KRONOS_THRESHOLD", "KRONOS_SAMPLES", "KRONOS_MAX_PAIRS", "KRONOS_SKIP_BOTS")))
df = (REPO / "kronos-signal/Dockerfile").read_text(encoding="utf-8")
check("IMAGE: CPU-only torch from the PyTorch CPU index, pinned", "torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu" in df and "+cu" not in df and "nvidia" not in df.lower())
check("IMAGE: model code pinned to commit 67b630e..., weights pinned to the Hugging Face revision hashes", "67b630e67f6a18c9e9be918d9b4337c960db1e9a" in df
      and "901c26c1332695a2a8f243eb2f37243a37bea320" in df and "0e0117387f39004a9016484a186a908917e22426" in df)
check("IMAGE: weights are verified by sha256 and the runtime is offline", "sha256sum -c" in df and "HF_HUB_OFFLINE=1" in df)
check("IMAGE: a disk guard runs inside the build (df / in a RUN step), reports the free GB and fails only below KRONOS_MIN_FREE_GB, before anything is downloaded",
      "df -Pk /" in df and "KRONOS_MIN_FREE_GB" in df and df.index("df -Pk /") < df.index("pip install torch") < df.index("snapshot_download"))
check("IMAGE: the models are the small variant (CPU), not base or large", "Kronos-small" in df and "Kronos-base" not in df and "Kronos-large" not in df)
check("IMAGE: runs as a non-root user, no heavy libraries in the bot images (the bots' Dockerfiles do not mention torch)",
      "USER kronos" in df and not any("torch" in (REPO / d / "Dockerfile").read_text(encoding="utf-8") for d in ("freqtrade", "freqtrade-shorts", "freqtrade-moonshot")))
check("requirements pin the Kronos dependencies (pandas 2.2.3 / numpy 2.1.3: pandas 2.2.x crashes with numpy 2.5)", "pandas==2.2.3" in (REPO / "kronos-signal/requirements.txt").read_text(encoding="utf-8")
      and "numpy==2.1.3" in (REPO / "kronos-signal/requirements.txt").read_text(encoding="utf-8"))
check("a disk check script exists for the host (before the image build)", "docker system df" in (REPO / "kronos-signal/scripts/check_disk.sh").read_text(encoding="utf-8"))
check("licence notice names the MIT licences and the pins", "MIT" in (REPO / "kronos-signal/NOTICE.md").read_text(encoding="utf-8") and "901c26c" in (REPO / "kronos-signal/NOTICE.md").read_text(encoding="utf-8"))
# ---------------------------------------------------------------- the cycle time is recorded by the system itself
(Path(KDATA) / "kronos_health.json").write_text(json.dumps({**s, "model": {"model": "NeoQuasar/Kronos-small", "model_revision": "901c26c"}}, default=str))
hh = sa.read_kronos_health()
check("HEALTH: the audit reads the container's own health file -- cycle seconds, pairs scored of candidates, truncated flag, model revision",
      hh["cycle_seconds"] == s["cycle_seconds"] and hh["pairs_scored"] == 7 and hh["candidates"] == 7 and hh["truncated"] is False and hh["model"]["model_revision"] == "901c26c")
res_h = sa.run_nightly(Path(tempfile.mkdtemp()), now=datetime(2026, 10, 5, 20, 0, tzinfo=timezone.utc), fetcher=Fetcher(), log_files_fn=lambda bot: [])
check("HEALTH: it is embedded in signals_audit.json (so also in the ZIP) without anyone copying numbers by hand", res_h["kronos_health"]["pairs_scored"] == 7)
(Path(KDATA) / "kronos_health.json").write_text("not json")
check("HEALTH: a corrupt or missing health file gives None, never an exception", sa.read_kronos_health() is None)
(Path(KDATA) / "kronos_health.json").unlink()
check("HEALTH: missing file -> None", sa.read_kronos_health() is None)
runner_src = (REPO / "kronos-signal/app/runner.py").read_text(encoding="utf-8")
check("the container lowers its own priority (os.nice) and writes a health file right after the model loads", "os.nice(10)" in runner_src and "model loaded, waiting for the first cycle" in runner_src)
Path(SDATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402
from backend import export as ex  # noqa: E402

cli = TestClient(app_module.app)
r0 = cli.get("/api/kronos/health").json()
check("GET /api/kronos/health: available=false with a reason before the first cycle", r0["available"] is False and "first hourly cycle" in r0["reason"])
(Path(KDATA) / "kronos_health.json").write_text(json.dumps({**s, "model": {"model": "x"}}, default=str))
r1 = cli.get("/api/kronos/health").json()
check("GET /api/kronos/health: live values straight from the container's file", r1["available"] and r1["cycle_seconds"] == s["cycle_seconds"] and r1["pairs_scored"] == 7)
check("the health route is GET-only", set(app_module.app.openapi()["paths"]["/api/kronos/health"]) == {"get"})
import io, zipfile  # noqa: E402,E401
from fastapi import FastAPI  # noqa: E402

app2 = FastAPI()
app2.include_router(ex.router)
ex._fetcher = lambda: type("F", (), {"_json": lambda s_, b, p_: None, "trades": lambda s_, b: []})()
zb = zipfile.ZipFile(io.BytesIO(TestClient(app2).get("/api/export/bundle?bots=main&include=signals").content))
check("the ZIP export carries signals/kronos_health.json", "signals/kronos_health.json" in zb.namelist()
      and json.loads(zb.read("MANIFEST.json"))["signals"]["kronos_health.json"]["included"] is True)

html = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("UI: the audit card shows the kronos verdict line and the last-cycle time", "kronos verdict" in html and "d.bots.kronos.verdict" in html and "kronos last cycle" in html)
import py_compile  # noqa: E402

for f in ("core", "runner", "predictor"):
    try:
        py_compile.compile(str(REPO / f"kronos-signal/app/{f}.py"), doraise=True)
        okc = True
    except py_compile.PyCompileError:
        okc = False
    check(f"kronos-signal/app/{f}.py compiles", okc)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P20.4 KRONOS CHECKS PASSED")
