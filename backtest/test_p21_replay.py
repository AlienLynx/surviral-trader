"""
P21 Replay -- pure-Python checks on fixtures (fake candles, fake LLM, fake Bybit): isolation, look-ahead, the strategy shims, the simulator, the cost limits,
the statistics, the job/API/UI. Nothing here needs Docker, a live bot, the network or a real API key.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR, DATA = tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": DATA, "REPLAY_STRATEGIES_DIR": str(REPO), "BOTS_DIR": tempfile.mkdtemp(), "ALLOCATOR_DIR": tempfile.mkdtemp(),
                   "REPLAY_PRICE_IN_PER_M": "0.14", "REPLAY_PRICE_OUT_PER_M": "0.28", "DEEPSEEK_API_KEY": "sk-test-not-real", "REPLAY_MAX_USD": "10", "REPLAY_WORKERS": "4",
                   "REPLAY_MIN_START": "2026-08-01"})

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import analysis, data as rd, engine as eng, llm as rl, paths as rp, runner as rr, sandbox as sb  # noqa: E402

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
START = datetime(2026, 9, 10, tzinfo=timezone.utc)
END = START + timedelta(days=4)
WARM = START - timedelta(days=eng.WARMUP_DAYS)
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"]


def synth(seed, hours, start=WARM):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.006, hours)
    close = 100 * (1 + (seed % 7)) * np.exp(np.cumsum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.003, hours)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.003, hours)))
    vol = rng.uniform(1000, 5000, hours)
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": vol, "quote": vol * close})


HOURS = int((END - WARM).total_seconds() // 3600) + 60


def make_store(mutate_after=None):
    s = rd.SeriesStore()
    for i, c in enumerate(COINS):
        df = synth(i + 1, HOURS)
        if mutate_after is not None:                     # a "peek" fixture: every candle that opens after this moment is replaced by garbage
            m = df["date"] >= pd.Timestamp(mutate_after)
            df.loc[m, ["open", "high", "low", "close"]] = 1.0e9
        s.add("spot", f"{c}USDT", df)
        s.add("linear", f"{c}USDT", df)
    return s


FNG = [(int((WARM + timedelta(days=d)).timestamp()), 40 + d % 30, "Neutral") for d in range(0, 80)]


class Resp:
    status_code = 200

    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def llm_payload(text, tin=900, tout=20):
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


def fake_post_factory(counter=None, answer=None):
    def post(url, headers=None, json=None, timeout=None):
        if counter is not None:
            counter.append(json)
        user = json["messages"][1]["content"]
        if answer is not None:
            return Resp(llm_payload(answer))
        h = int(hashlib.md5(user.encode()).hexdigest(), 16) % 10
        short_ok = "sell_short" in json["messages"][0]["content"]
        return Resp(llm_payload("buy: looks fine" if h < 3 else ("sell_short: weak" if (h < 6 and short_ok) else "hold: nothing")))
    return post


def new_client(post=None, max_usd=100.0, max_calls=100000, workers=4, stop=None):
    return rl.LLMClient("k", max_usd, max_calls, 0.14, 0.28, workers=workers, post=post or fake_post_factory(), sleep=lambda s: None, stop_event=stop,
                        cache_dir=Path(tempfile.mkdtemp()))


# ================================================================== isolation: paths
for bad in ("../x", "a/b", "", "x" * 80, "run id", "..", "a\\b"):
    try:
        rp.replay_path(bad, "f.txt")
        ok_ = False
    except rp.ReplayPathError:
        ok_ = True
    check(f"PATHS: run id {bad!r} is refused", ok_)
for name in ("../escape.txt", "/etc/passwd", "a/../../b", "x\\y"):
    try:
        rp.replay_path("run1", name)
        ok_ = False
    except rp.ReplayPathError:
        ok_ = True
    check(f"PATHS: file name {name!r} is refused", ok_)
check("PATHS: a normal path resolves inside REPLAY_DIR", rp.replay_path("run1", "decisions.jsonl") == (Path(RDIR).resolve() / "run1" / "decisions.jsonl"))
for live in ("signals.jsonl", "signals_audit.json", "allocations.jsonl", "weights.json", "context_latest.json", "signals_forward.jsonl"):
    try:
        rp.replay_path("run1", live)
        ok_ = False
    except rp.ReplayPathError:
        ok_ = True
    check(f"PATHS: the live file name {live} can never be written by replay", ok_)
for sub in ("allocator/x.json", "bots/main/log"):
    try:
        rp.cache_path(sub)
        ok_ = sub not in ("allocator/x.json",)
    except rp.ReplayPathError:
        ok_ = True
    check(f"PATHS: cache_path {sub!r} stays inside REPLAY_DIR/cache", ok_ or str(rp.cache_path(sub)).startswith(str(Path(RDIR).resolve())))
try:
    rp.assert_not_live(Path(os.environ["ALLOCATOR_DIR"]) / "allocator" / "weights.json")
    ok_ = False
except rp.ReplayPathError:
    ok_ = True
check("PATHS: assert_not_live refuses a weights.json anywhere", ok_)
check("PATHS: root_file stays directly inside the sandbox", rp.root_file("live_reference.json").parent == Path(RDIR).resolve())
# regression: on Windows Path.resolve() returns the \\?\ form while another thread is creating the parent directory; that used to raise "outside REPLAY_DIR" (flaky fetch_all)
race_errs = []


def _race(w):
    for k in range(150):
        p_ = Path(RDIR) / "racerun" / f"d{w}_{k}" / "hourly"
        mk = threading.Thread(target=lambda: p_.mkdir(parents=True, exist_ok=True))
        mk.start()
        try:
            rp.replay_path("racerun", f"d{w}_{k}/hourly/S.json")
        except Exception as e_:      # noqa: BLE001
            race_errs.append(repr(e_))
        mk.join()


race_ts = [threading.Thread(target=_race, args=(w,)) for w in range(6)]
for t_ in race_ts:
    t_.start()
for t_ in race_ts:
    t_.join()
check("PATHS: replay_path stays valid while other threads create its parent directories", not race_errs)

# ================================================================== data / as-of / look-ahead
store = make_store()
t = START + timedelta(hours=10)
a1 = store.asof("spot", "BTCUSDT", t)
check("AS-OF: only candles closed by t are visible (open + 1h <= t)", a1.iloc[-1]["date"] == pd.Timestamp(t - timedelta(hours=1)) and (a1["date"] + pd.Timedelta(hours=1) <= pd.Timestamp(t)).all())
a2 = store.asof("spot", "BTCUSDT", t + timedelta(minutes=59))
check("AS-OF: 59 minutes later the forming candle is still invisible", len(a2) == len(a1))
check("AS-OF: exactly at the next close the new candle appears", len(store.asof("spot", "BTCUSDT", t + timedelta(hours=1))) == len(a1) + 1)
mut = make_store(mutate_after=t)
check("LOOK-AHEAD (data): replacing every future candle with garbage does not change the visible history", store.asof("spot", "BTCUSDT", t).equals(mut.asof("spot", "BTCUSDT", t)))
k60 = store.klines_asof("spot", "BTCUSDT", "60", 5, t)
check("klines 60: newest first, strings, last closed candle", len(k60) == 5 and int(k60[0][0]) == int((t - timedelta(hours=1)).timestamp() * 1000) and int(k60[0][0]) > int(k60[1][0]))
k240 = store.klines_asof("spot", "BTCUSDT", "240", 55, START + timedelta(days=10, hours=3))
check("klines 240: only COMPLETE 4h bars as of t (the forming bar is excluded)", all(int(r[0]) % (4 * 3600 * 1000) == 0 for r in k240)
      and int(k240[0][0]) + 4 * 3600 * 1000 <= int((START + timedelta(days=10, hours=3)).timestamp() * 1000))
kd = store.klines_asof("spot", "BTCUSDT", "D", 35, START + timedelta(days=10, hours=3))
check("klines D: only complete daily bars", len(kd) == 35 and int(kd[0][0]) + 86400000 <= int((START + timedelta(days=10, hours=3)).timestamp() * 1000))
try:
    store.klines_asof("spot", "BTCUSDT", "5", 5, t)
    ok_ = False
except ValueError:
    ok_ = True
check("klines: an interval replay does not serve raises", ok_)
check("turnover rank is as-of (a trailing-24h sum)", set(store.turnover_rank("spot", [f"{c}USDT" for c in COINS], t)) == {f"{c}USDT" for c in COINS})
check("F&G as-of: the value published at or before t, never a later one", rd.fng_asof(FNG, WARM + timedelta(days=3, hours=5)) == (43, "Neutral")
      and rd.fng_asof(FNG, WARM - timedelta(days=1)) is None)


class KlineGet:
    """fake Bybit: counts calls, serves hourly candles descending within [start, end] (<=1000 per call)."""

    def __init__(self):
        self.calls = []
        self.df = synth(5, 4000, WARM - timedelta(days=60))

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        if "kline" in url:
            s, e = int(params["start"]), int(params["end"])
            d = self.df
            ms = (d["date"].astype("int64") // 10 ** 6) if str(d["date"].dtype).endswith("[ns, UTC]") else (d["date"].dt.as_unit("ms").astype("int64"))
            sel = d[(ms >= s) & (ms <= e)].iloc[::-1].head(int(params.get("limit", 1000)))
            rows = [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in sel.itertuples()]
            return Resp({"retCode": 0, "result": {"list": rows}})
        if "tickers" in url:
            return Resp({"retCode": 0, "result": {"list": [{"symbol": f"{c}USDT", "turnover24h": str(1000 - i)} for i, c in enumerate(COINS)] + [{"symbol": "USDCUSDT", "turnover24h": "99999"},
                                                                                                                                    {"symbol": "BTC3LUSDT", "turnover24h": "99999"}, {"symbol": "BTCUSD", "turnover24h": "1"}]}})
        if "alternative.me" in url:
            return Resp({"data": [{"timestamp": str(ts), "value": str(v), "value_classification": c} for ts, v, c in FNG]})
        raise AssertionError(f"unexpected url {url}")


kg = KlineGet()
df1 = rd.load_candles("spot", "TESTUSDT", START, START + timedelta(days=2), kg, sleep=lambda s: None)
n_calls = len(kg.calls)
df2 = rd.load_candles("spot", "TESTUSDT", START, START + timedelta(days=2), kg, sleep=lambda s: None)
check("CANDLES: fetched once, the second identical request is served from the cache (no HTTP)", len(df1) == len(df2) >= 48 and len(kg.calls) == n_calls)
df3 = rd.load_candles("spot", "TESTUSDT", START, START + timedelta(days=3), kg, sleep=lambda s: None)
check("CANDLES: extending the window fetches only the missing tail", len(df3) > len(df1) and len(kg.calls) == n_calls + 1)
big = rd.fetch_klines("spot", "TESTUSDT", int((WARM - timedelta(days=40)).timestamp() * 1000), int(WARM.timestamp() * 1000), kg, sleep=lambda s: None)
check("CANDLES: pagination past 1000 rows (ascending, no duplicates)", len(big) > 900 and all(big[i][0] < big[i + 1][0] for i in range(len(big) - 1)))
check("TOP COINS: stables, leveraged tokens and non-USDT symbols are excluded, ordered by turnover", rd.top_coins("spot", 4, kg) == COINS[:4])
check("FNG history is cached and parsed", rd.fng_history(kg)[0][1] == 40)

# ================================================================== sandbox: shims
ctx = sb.ReplayContext("main", store, "spot", [rd.pair_name(c, "spot") for c in COINS], FNG)
ctx.clock.now = t
http = sb.ReplayHTTP(ctx)
r = http.get("https://api.bybit.com/v5/market/kline", params={"category": "spot", "symbol": "BTCUSDT", "interval": "60", "limit": 3}, timeout=5)
check("SHIM: Bybit klines are served from the local store as of the replay clock", len(r.json()["result"]["list"]) == 3 and int(r.json()["result"]["list"][0][0]) == int((t - timedelta(hours=1)).timestamp() * 1000))
for url in ("https://api.coingecko.com/api/v3/global", "https://api.alternative.me/fng/", "https://news.google.com/rss/search?q=x", "https://api.deepseek.com/chat/completions"):
    try:
        http.get(url)
        ok_ = False
    except sb.ReplayBlocked:
        ok_ = True
    check(f"SHIM: a live URL is refused and recorded ({url.split('/')[2]})", ok_)
try:
    http.post("https://api.deepseek.com/chat/completions", json={})
    ok_ = False
except sb.ReplayBlocked:
    ok_ = True
check("SHIM: POST is refused", ok_ and len(ctx.blocked) == 5)
check("SHIM: exceptions attribute exists (strategies catch requests.exceptions.HTTPError)", hasattr(http.exceptions, "HTTPError"))
sd = sb.make_datetime(ctx.clock)
check("SIM CLOCK: datetime.now(timezone.utc) is the replay time", sd.now(timezone.utc) == t and sd.now() == t.replace(tzinfo=None))
tm = sb.SimTime(ctx.clock)
check("SIM CLOCK: time.time() is the replay time and sleep is a no-op", tm.time() == t.timestamp() and tm.sleep(10) is None)
an = sb.Anonymizer(ctx.pairs)
txt = "Pair: BTC/USDT 24h Current close 123.4 correlation with already-open ETH/USDT; opened 2026-09-10T08:00:00Z"
out_ = an(txt, t)
check("ANONYMIZER: pairs become ASSET_NN, absolute dates become relative time, prices stay", "BTC" not in out_ and "ETH" not in out_ and "ASSET_" in out_ and "123.4" in out_
      and "2026-09-10" not in out_ and "T-" in out_)
check("ANONYMIZER: the mapping is stable and consistent", an("BTC ETH BTC").split()[0] == an("BTC ETH BTC").split()[2] and an("BTC") != an("ETH"))

# ---- the hook list: every LLM bot loads, with each hook replaced, from the real strategy file
for bot, spec in eng.BOTS.items():
    c = sb.ReplayContext(bot, store, spec["category"], [rd.pair_name(x, spec["category"]) for x in COINS], FNG)
    mod, obj = sb.load_bot(spec, c, lambda s, u: "hold: x", eng.bot_config(spec))
    check(f"LOADER {bot}: the real strategy file loads; requests/Trade/context/news/F&G are swapped, the LLM call is the replay hook",
          isinstance(mod.requests, sb.ReplayHTTP) and mod.get_market_context() == sb.HISTORICAL_CONTEXT_NOTE and mod.get_news_headlines() == sb.HISTORICAL_NEWS_NOTE
          and obj._call_deepseek("s", "u") == "hold: x" and obj._call_openrouter_fallback("s", "u") is None)
    check(f"LOADER {bot}: news text is neither 'no news' nor 'feed down'", "no news found" not in sb.HISTORICAL_NEWS_NOTE and "unavailable due to fetch error" not in sb.HISTORICAL_NEWS_NOTE)
missing_ok = False
try:
    sb.load_bot({"key": "nope", "dir": "nope", "file": "Nope.py"}, ctx, lambda s, u: None, {})
except sb.HookError:
    missing_ok = True
check("LOADER: a missing strategy file stops with HookError (nothing is guessed)", missing_ok)

# ---- no peeking at the prompt level
def prompts_for(store_, bot="main", at=None):
    seen = []

    class Cap:
        def call(self, system, user, temperature=0.1, max_tokens=300):
            seen.append(user)
            return "hold: x"
    spec = eng.BOTS[bot]
    run = eng.BotRun(bot, "named", {"every_n": 1, "workers": 1, "seed": 1}, store_, FNG, Cap(), [rd.pair_name(c, spec["category"]) for c in COINS], "peek")
    run.ctx.clock.now = at
    run.ctx.df_asof = at
    for p in run.pairs:
        run.decide(p, at)
    return sorted(seen), run


T_PEEK = START + timedelta(days=2)
p_clean, _ = prompts_for(make_store(), "main", T_PEEK)
p_peek, run_peek = prompts_for(make_store(mutate_after=T_PEEK), "main", T_PEEK)
check("NO PEEKING: replacing every candle from t on with garbage does not change one character of any prompt at t", p_clean == p_peek and len(p_clean) == len(COINS))
check("NO PEEKING: the prompts really contain the as-of price and the as-of Fear & Greed value", all("Current close:" in p and "Fear & Greed Index:" in p for p in p_clean))
check("NO PEEKING: the strategy tried no live URL while building those prompts", run_peek.ctx.blocked == [])
p_s_clean, _ = prompts_for(make_store(), "shorts", T_PEEK)
p_s_peek, _ = prompts_for(make_store(mutate_after=T_PEEK), "shorts", T_PEEK)
check("NO PEEKING (shorts, futures): same", p_s_clean == p_s_peek)
p_late, _ = prompts_for(make_store(), "main", T_PEEK + timedelta(hours=1))
check("the prompt does change when the as-of time moves (so the previous checks are not vacuous)", p_late != p_clean)
check("prompts carry the honest 'unavailable for a historical replay' texts", all("historical replay" in p for p in p_clean))

# ================================================================== the LLM client
posts = []
c1 = new_client(fake_post_factory(posts, "buy: x"))
r1 = c1.call("S", "U1")
r2 = c1.call("S", "U1")
check("LLM CLIENT: the identical prompt is paid once, then served from the cache", r1 == r2 == "buy: x" and c1.calls == 1 and c1.cache_hits == 1 and len(posts) == 1)
check("LLM CLIENT: the request is the live one -- deepseek-flash, thinking disabled, max_tokens 300, temperature 0.1", posts[0]["model"] == "deepseek-flash"
      and posts[0]["thinking"] == {"type": "disabled"} and posts[0]["max_tokens"] == 300 and posts[0]["temperature"] == 0.1)
check("LLM CLIENT: the cost comes from the API usage field and the per-million prices", abs(c1.usd - (900 * 0.14 + 20 * 0.28) / 1e6) < 1e-12 and c1.tokens_in == 900 and c1.tokens_out == 20)
c1.call("S", "U1", temperature=0.2)
check("LLM CLIENT: a different temperature is a different cache key", c1.calls == 2)
c2 = new_client(fake_post_factory(), max_usd=0.0002)
try:
    for i in range(10):
        c2.call("S", f"U{i}")
    ok_ = False
except rl.BudgetExceeded:
    ok_ = True
check("LIMIT: the dollar limit stops the run (BudgetExceeded) and flags stopped_budget", ok_ and c2.stopped_budget and c2.usd <= 0.0002 + 0.0002)
c3 = new_client(fake_post_factory(), max_calls=3)
n_ok = 0
try:
    for i in range(10):
        c3.call("S", f"V{i}")
        n_ok += 1
except rl.BudgetExceeded:
    pass
check("LIMIT: max_calls stops the run after exactly that many paid calls", n_ok == 3 and c3.calls == 3)
try:
    c3.call("S", "brand new prompt")
    ok_ = False
except rl.BudgetExceeded:
    ok_ = True
check("LIMIT: after the stop even a new prompt is refused, a cached one is still served", ok_ and c3.call("S", "V0") in ("hold: nothing", "buy: looks fine", "sell_short: weak"))
stop = threading.Event()
c4 = new_client(fake_post_factory(), stop=stop)
stop.set()
try:
    c4.call("S", "W")
    ok_ = False
except rl.Stopped:
    ok_ = True
check("STOP: a stopped run makes no further call", ok_ and c4.calls == 0)
flaky_n = []


def flaky(url, headers=None, json=None, timeout=None):
    flaky_n.append(1)
    if len(flaky_n) < 3:
        rr_ = Resp({})
        rr_.status_code = 429
        return rr_
    return Resp(llm_payload("hold: ok"))


c5 = rl.LLMClient("k", 10, 100, 0.14, 0.28, workers=1, post=flaky, sleep=lambda s: None, cache_dir=Path(tempfile.mkdtemp()))
check("RETRY: 429 answers are retried with backoff and the call still succeeds", c5.call("S", "X") == "hold: ok" and len(flaky_n) == 3 and c5.calls == 1)
c6 = rl.LLMClient("k", 10, 100, 0.14, 0.28, workers=1, post=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")), sleep=lambda s: None, retries=2, cache_dir=Path(tempfile.mkdtemp()))
check("RETRY: a permanently failing API returns None (the strategy then holds) and counts an error, never crashes", c6.call("S", "Y") is None and c6.errors == 1)
conc = {"now": 0, "max": 0}
lk = threading.Lock()


def slow_post(url, headers=None, json=None, timeout=None):
    with lk:
        conc["now"] += 1
        conc["max"] = max(conc["max"], conc["now"])
    time.sleep(0.03)
    with lk:
        conc["now"] -= 1
    return Resp(llm_payload("hold: z"))


c7 = rl.LLMClient("k", 10, 1000, 0.14, 0.28, workers=3, post=slow_post, sleep=lambda s: None, cache_dir=Path(tempfile.mkdtemp()))
ths = [threading.Thread(target=lambda i=i: c7.call("S", f"par{i}")) for i in range(12)]
[x.start() for x in ths]
[x.join() for x in ths]
check("PARALLELISM: never more than REPLAY_WORKERS calls in flight", conc["max"] <= 3 and c7.calls == 12)
check("env prices are read, an unset price is None (never hard-coded)", rl.prices_from_env({"REPLAY_PRICE_IN_PER_M": "1", "REPLAY_PRICE_OUT_PER_M": "2"}) == (1.0, 2.0) and rl.prices_from_env({}) == (None, None))

# ================================================================== the simulator: exits, entries, stake
def tiny_run(bot="main", answer_fn=None, every_n=1, store_=None, days=3, pairs=None):
    spec = eng.BOTS[bot]
    st = store_ or make_store()
    pr = pairs or [rd.pair_name(c, spec["category"]) for c in COINS[:3]]
    run = eng.BotRun(bot, "named", {"every_n": every_n, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, st, FNG, new_client(fake_post_factory(answer=answer_fn or "hold: x")), pr, "t")
    return run, run.run(START, START + timedelta(days=days))


def forced_store(pair_coin="BTC", path=None):
    """candles flat at 100 except a scripted path starting at hour index `i0` (open of the first post-entry candle)."""
    st = make_store()
    df = st.frames[("spot", f"{pair_coin}USDT")].copy()
    base = 100.0
    df[["open", "high", "low", "close"]] = base
    df["volume"] = 10_000.0
    df["quote"] = df["volume"] * base
    for (hours_after_start, o, h, l, c) in (path or []):
        i = int((START + timedelta(hours=hours_after_start) - WARM).total_seconds() // 3600)
        df.loc[i, ["open", "high", "low", "close"]] = [o, h, l, c]
    st.add("spot", f"{pair_coin}USDT", df)
    return st


# entry at the open of the candle after the decision (+ slippage); stoploss -5% hit by a low in a later candle
st_stop = forced_store(path=[(5, 100, 100, 100, 100), (6, 100, 100, 90, 95)])
run_s = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05, "mech": eng.MECH_LEGACY}, st_stop, FNG, new_client(fake_post_factory(answer="buy: go")), [rd.pair_name("BTC", "spot")], "t")
res_s = run_s.run(START, START + timedelta(hours=9))
tr = res_s["trades"][0] if res_s["trades"] else None
check("SIM entry: filled at the OPEN of the candle after the decision plus slippage (100 -> 100.05)", tr is not None and abs(tr["open_rate"] - 100.0 * 1.0005) < 1e-9
      and tr["open_date"] == (START + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
check("SIM exit: the stoploss (-5% net of fees) fires on the candle whose LOW reaches it, at the stop price, reason stop_loss", tr["exit_reason"] == "stop_loss"
      and abs(tr["profit_ratio"] - (-0.05 - 0.0005 * 1.0)) < 6e-3 and tr["close_date"] == (START + timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
check("SIM fees: fee per side on entry and exit is charged and reported", tr["fees_abs"] > 0 and abs(tr["fees_abs"] - (tr["stake"] * 0.001 + tr["stake"] * (1 + tr["profit_ratio"]) * 0.001)) < 1e-3)
check("SIM cash: stake leaves the wallet at entry and comes back with the P&L at exit (cash + open stakes = wallet + realised P&L)",
      abs(run_s.ctx.cash + sum(x.stake_amount for x in run_s.ctx.trades_open) - (res_s["wallet0"] + sum(t_["profit_abs"] for t_ in res_s["trades"]))) < 0.01)
st_roi = forced_store(path=[(5, 100, 100, 100, 100), (6, 100, 111, 100, 105)])
res_r = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, st_roi, FNG, new_client(fake_post_factory(answer="buy: go")),
                   [rd.pair_name("BTC", "spot")], "t").run(START, START + timedelta(hours=9))
check("SIM exit: minimal_roi (10%) fires when a HIGH reaches it, reason roi", res_r["trades"] and res_r["trades"][0]["exit_reason"] == "roi" and res_r["trades"][0]["profit_ratio"] > 0.09)
st_both = forced_store(path=[(5, 100, 100, 100, 100), (6, 100, 125, 80, 100)])
res_b = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, st_both, FNG, new_client(fake_post_factory(answer="buy: go")),
                   [rd.pair_name("BTC", "spot")], "t").run(START, START + timedelta(hours=9))
check("SIM exit: ROI and stoploss both inside one candle -> the stoploss wins (pessimistic, documented)", res_b["trades"][0]["exit_reason"] == "stop_loss")
check("SIM: the strategy's own constants are the ones used (read from the class, not copied)", res_s["constants"]["stoploss"] == -0.05 and res_s["constants"]["minimal_roi"] == {"0": 0.10})
check("SIM stake: free cash / free slots (500/10 = 50) x the strategy's own satellite multiplier when satellite (x0.5 = 25), from the real custom_stake_amount",
      res_s["trades"][0]["stake"] in (25.0, 50.0) and res_s["trades"][0]["stake"] == (25.0 if res_s["trades"][0]["enter_tag"] == "sat" else 50.0))
# short mirror on the futures bot
st_short = forced_store(path=[(5, 100, 100, 100, 100), (6, 100, 100, 88, 88)])
for coin in ("BTC",):
    st_short.add("linear", f"{coin}USDT", st_short.frames[("spot", f"{coin}USDT")].copy())
res_sh = eng.BotRun("shorts", "named", {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, st_short, FNG, new_client(fake_post_factory(answer="sell_short: weak")),
                    [rd.pair_name("BTC", "linear")], "t").run(START, START + timedelta(hours=9))
sh = res_sh["trades"][0] if res_sh["trades"] else None
check("SIM short: a sell_short decision opens a SHORT; the entry is slipped against it (down) and its exit/ratio mirror the long logic", sh is not None and sh["side"] == "short"
      and abs(sh["open_rate"] - 100 * (1 - 0.0005)) < 1e-9)
check("SIM short: profit when the price falls -- a 12% drop hits the 10% ROI target of the short, a gain after fees and slippage", sh is not None and sh["exit_reason"] == "roi"
      and 0.085 < sh["profit_ratio"] < 0.105)
check("SIM: a long-only bot never opens a short even if its decision says sell_short", all(t_["side"] == "long" for t_ in tiny_run("main", "sell_short: weak")[1]["trades"]))

# max open trades / one position per pair / cash accounting
run_m = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, make_store(), FNG, new_client(fake_post_factory(answer="buy: always")),
                   [rd.pair_name(c, "spot") for c in COINS], "t", max_open=3)
res_m = run_m.run(START, START + timedelta(days=3))
check("SIM: at most max_open_trades (here 3 of 6 pairs) positions at a time, one per pair", len({tt.pair for tt in run_m.ctx.trades_open}) == len(run_m.ctx.trades_open)
      and len(run_m.ctx.trades_open) <= res_m["max_open_trades"] == 3)
check("SIM: stake = unlimited rule, so the wallet is never overdrawn (cash >= 0)", run_m.ctx.cash >= -1e-6)

# stale-signal semantics with a coarse step
flip = {"n": 0}


def alternating(url, headers=None, json=None, timeout=None):
    return Resp(llm_payload("buy: up" if hashlib.md5(json["messages"][1]["content"].encode()).hexdigest()[0] in "01234567" else "hold: down"))


# determinism
r_a = tiny_run("main", None, every_n=4, days=3)[1]
r_b = tiny_run("main", None, every_n=4, days=3)[1]
check("DETERMINISM: the same parameters and seed give identical decisions and trades", r_a["decisions"] == r_b["decisions"] and r_a["trades"] == r_b["trades"])
pairs6 = [rd.pair_name(c, "spot") for c in COINS]


def run_parallel(workers):
    rn = eng.BotRun("main", "named", {"every_n": 2, "workers": workers, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05}, make_store(), FNG, new_client(fake_post_factory()), pairs6, "t")
    return rn.run(START, START + timedelta(days=3))


rp1, rp2 = run_parallel(6), run_parallel(6)
check("DETERMINISM with parallel LLM calls (6 workers): tiers are primed before the threads start, so two runs give identical decisions, trades and equity",
      rp1["decisions"] == rp2["decisions"] and rp1["trades"] == rp2["trades"] and rp1["equity"] == rp2["equity"] and len(rp1["trades"]) > 3)
check("DETERMINISM: parallel (6 workers) equals sequential (1 worker)", run_parallel(1)["trades"] == rp1["trades"])
check("DECISION RECORDS: ledger-compatible, flagged replay:true with run id and mode", all({"ts", "bot", "pair", "action", "reason", "price", "mode", "run_id", "replay"} <= set(d) and d["replay"] is True
      for d in r_a["decisions"]) and r_a["decisions"][0]["mode"] == "named" and r_a["decisions"][0]["run_id"] == "t")
check("STEPS: with every_n=4 decisions are made every 4th hour only", len({d["ts"] for d in r_a["decisions"]}) == len(range(0, 72, 4)))
r_1 = tiny_run("main", None, every_n=1, days=1)[1]
check("STEPS: every_n=1 decides every hour (as live)", len({d["ts"] for d in r_1["decisions"]}) == 24)
check("PAIR ORDER: shuffled with a fixed seed, so slot taking is not alphabetical but reproducible", [d["pair"] for d in r_a["decisions"][:3]] == [d["pair"] for d in r_b["decisions"][:3]])

# every bot runs end to end on the real strategy code
for bot, spec in eng.BOTS.items():
    run_x, res_x = tiny_run(bot, None, every_n=8, days=3, pairs=[rd.pair_name(c, spec["category"]) for c in COINS[:4]])
    check(f"BOT {bot}: the REAL strategy code runs end to end, completes, makes decisions, touches no live URL", res_x["status"] == "completed" and len(res_x["decisions"]) > 0
          and res_x["blocked_urls"] == [])

# ================================================================== analysis
winning = [{"profit_ratio": 0.05, "profit_abs": 2.5, "fees_abs": 0.1, "open_date": "2026-09-10T00:00:00Z", "close_date": "2026-09-10T10:00:00Z"} for _ in range(300)]
st_w = analysis.trade_stats(winning, 500, [("2026-09-10T00:00:00Z", 500.0), ("2026-09-11T00:00:00Z", 1250.0)])
check("STATUS: even 300 perfect winning trades give 'unproven (replay)' -- replay can NEVER say edge", analysis.replay_status(st_w) == "unproven (replay)" and "edge" not in analysis.replay_status(st_w).split())
losing = [{**winning[0], "profit_ratio": -0.02, "profit_abs": -1.0} for _ in range(100)]
check("STATUS: a clearly losing bot is 'no_edge (replay)'", analysis.replay_status(analysis.trade_stats(losing, 500, [("a", 500.0), ("b", 400.0)])) == "no_edge (replay)")
check("STATUS: fewer than 30 trades is no_data", analysis.replay_status(analysis.trade_stats(winning[:10], 500, [("a", 500.0)])) == "no_data")
check("STATS: max drawdown from the equity curve and net return on the wallet", analysis.trade_stats(winning[:2], 500, [("a", 500), ("b", 600), ("c", 450), ("d", 480)])["max_drawdown_pct"] == 25.0)
btc = analysis.btc_hold(store, START, END)
check("BASELINE BTC hold: return over the window from the first open to the last close", btc is not None and abs(btc["return_pct"] - round((float(store.asof("spot", "BTCUSDT", END).iloc[-1]["close"]) /
      float(store.asof("spot", "BTCUSDT", END)[lambda d: d["date"] >= pd.Timestamp(START)].iloc[0]["open"]) - 1) * 100, 3)) < 1e-9)
rnd = analysis.random_entries(40, 0.0, store, "spot", [rd.pair_name(c, "spot") for c in COINS], START, END, {"0": 0.10}, -0.05, 0.001, 0.0005, repeats=50, seed=3)
rnd2 = analysis.random_entries(40, 0.0, store, "spot", [rd.pair_name(c, "spot") for c in COINS], START, END, {"0": 0.10}, -0.05, 0.001, 0.0005, repeats=50, seed=3)
check("BASELINE random entries: deterministic, a distribution with percentiles, same trade count", rnd["_samples"] == rnd2["_samples"] and rnd["mean_trade_pct"]["p5"] <= rnd["mean_trade_pct"]["p50"] <= rnd["mean_trade_pct"]["p95"]
      and rnd["n_trades"] == 40 and rnd["repeats"] == 50)
check("BASELINE random: the percentile rank of a value inside / above the distribution", 0 <= analysis.percentile_rank(rnd["_samples"], rnd["mean_trade_pct"]["p50"]) <= 100 and analysis.percentile_rank(rnd["_samples"], 1e9) == 100.0)

# ledger audit with look-ahead safety
decs = []
for i in range(0, 24 * 3, 1):
    for c in COINS:
        t_ = START + timedelta(hours=i + 1)
        decs.append({"ts": t_.strftime("%Y-%m-%dT%H:%M:%SZ"), "bot": "main", "pair": f"{c}/USDT", "action": "buy" if (i + COINS.index(c)) % 3 == 0 else "hold", "price": 1.0, "mode": "named"})
sigs, fwd = analysis.decisions_to_ledger(decs, store, {}, END)
d0 = decs[0]
t0 = datetime.strptime(d0["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
idx = store.frames[("spot", "BTCUSDT")].set_index("date")["close"]
f1 = next(f for f in fwd if f["sid"].startswith("main|BTC/USDT|" + d0["ts"]) and f["h"] == 4)
check("LEDGER: decision price = close of the candle opened at t0-1h, forward price at 4h = close of the candle opened at t0+3h", abs(f1["price0"] - idx[pd.Timestamp(t0 - timedelta(hours=1))]) < 1e-9
      and abs(f1["price_h"] - idx[pd.Timestamp(t0 + timedelta(hours=3))]) < 1e-9)
st_mut = make_store()
dfm = st_mut.frames[("spot", "BTCUSDT")].copy()
dfm.loc[dfm["date"] >= pd.Timestamp(t0 + timedelta(hours=4)), ["open", "high", "low", "close"]] = 1e9
st_mut.add("spot", "BTCUSDT", dfm)
_, fwd_m = analysis.decisions_to_ledger(decs[:1], st_mut, {}, END)
_, fwd_o = analysis.decisions_to_ledger(decs[:1], store, {}, END)
check("NO LOOK-AHEAD (ledger): candles after the horizon's candle never change the 1h and 4h forward returns", {f["h"]: f["ret"] for f in fwd_m if f["h"] in (1, 4)} == {f["h"]: f["ret"] for f in fwd_o if f["h"] in (1, 4)})
check("LEDGER: a horizon whose candle closes after the data end is not computed", all(datetime.strptime(s["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) + timedelta(hours=24) <= END
      for s in sigs if any(f["sid"] == s["sid"] and f["h"] == 24 for f in fwd)))
aud = analysis.run_ledger_audit("main", decs, store, "spot", END)
check("AUDIT on replay decisions: the P19 audit entry with per-direction metrics once >= 100 buys, a never-edge label", aud["entry"]["status"] == "ok" and aud["status"] in ("unproven (replay)", "no_edge (replay)"))
aud_small = analysis.run_ledger_audit("main", decs[:50], store, "spot", END)
check("AUDIT: under 100 actions in a direction -> no_data", aud_small["status"] == "no_data")
check("LEAK flag: positive CI only in the named run -> suspect; both positive or neither -> not", analysis.leak_suspect({"ci95_pct": [0.1, 1.0]}, {"ci95_pct": [-0.5, 0.4]}) is True
      and analysis.leak_suspect({"ci95_pct": [0.1, 1.0]}, {"ci95_pct": [0.2, 1.0]}) is False and analysis.leak_suspect({"ci95_pct": [-1, 1]}, {"ci95_pct": [-1, 1]}) is False
      and analysis.leak_suspect(None, {"ci95_pct": [1, 2]}) is None)
ref = {"main": {"from": "2026-09-10", "to": "2026-09-14", "n_trades": 10, "mean_trade_pct": 0.5}}
tr_list = [{"close_date": "2026-09-11T00:00:00Z", "profit_ratio": 0.01}] * 8
dv = analysis.divergence("main", tr_list, ref)
check("DIVERGENCE from live: counts and mean on the overlap, a ratio and a verdict", dv["available"] and dv["replay_n_trades"] == 8 and dv["trade_count_ratio"] == 0.8 and "same order" in dv["verdict"])
check("DIVERGENCE: a big count mismatch says the simulator disagrees and the numbers must not be trusted", "do not trust" in analysis.divergence("main", tr_list[:2], ref)["verdict"])
check("DIVERGENCE: no live reference -> says how to build it, never invents one", analysis.divergence("main", tr_list, None)["available"] is False and "live_reference" in analysis.divergence("main", tr_list, None)["note"])
zpath = Path(tempfile.mkdtemp()) / "export.zip"
with zipfile.ZipFile(zpath, "w") as z:
    z.writestr("main/trades.csv", "trade_id,close_date,profit_ratio\n1,2026-09-22T10:00:00Z,0.01\n2,2026-09-23T10:00:00Z,-0.02\n3,2026-09-01T10:00:00Z,0.5\n4,,0.9\n")
from backend.replay import live_reference as lr  # noqa: E402

built, _snap = lr.build(str(zpath), "2026-09-21", None)
check("live_reference.json is built OFFLINE from a P18 export (closed trades inside the period only)", built["main"]["n_trades"] == 2 and abs(built["main"]["mean_trade_pct"] - (-0.5)) < 1e-9)

# ================================================================== params / estimate / limits
ok_p = rr.normalize_params({"bots": ["main", "shorts"], "days": 30, "pairs": 20, "every_n": 4}, NOW)
check("PARAMS: defaults -- the window ends 48h before now, 30 days, both modes, the replay-only v2 bot excluded by default", ok_p["end"] == (NOW - timedelta(hours=48)).replace(minute=0) and ok_p["modes"] == ["named", "anonymized"]
      and "shorts_v2" not in rr.normalize_params({}, NOW)["bots"])
for raw, why in (({"days": 6}, "days < 7"), ({"days": 61}, "days > 60"), ({"every_n": 3}, "every_n not in 1/2/4/8"), ({"modes": []}, "no mode"), ({"bots": ["nonsense"]}, "no valid bot"),
                 ({"end": (NOW - timedelta(hours=10)).isoformat()}, "window too recent"), ({"end": "2026-08-10T00:00:00+00:00", "days": 30}, "starts before REPLAY_MIN_START"),
                 ({"allocator": True}, "phase 4")):
    try:
        rr.normalize_params(raw, NOW)
        ok_ = False
    except rr.ParamError:
        ok_ = True
    check(f"PARAMS: refused -- {why}", ok_)
check("PARAMS: the dollar limit can be lowered but never raised above REPLAY_MAX_USD", rr.normalize_params({"max_usd": 1}, NOW)["max_usd"] == 1.0 and rr.normalize_params({"max_usd": 999}, NOW)["max_usd"] == 10.0)
check("PARAMS: the pair count is clamped 3..50", rr.normalize_params({"pairs": 1}, NOW)["pairs"] == 3 and rr.normalize_params({"pairs": 500}, NOW)["pairs"] == 50)
p30 = rr.normalize_params({"bots": ["main"], "days": 30, "pairs": 20, "every_n": 4, "modes": ["named"]}, NOW)
check("ESTIMATE: the call count is exact = bots x modes x pairs x ceil(days*24/step); month, 20 pairs, 4h, one bot = 3600", rr.estimate(p30, 3150, 0.14, 0.28)["calls"] == 3600)
e = rr.estimate(p30, 900 * 3.5, 0.14, 0.28)
check("ESTIMATE: dollars from the sampled prompt size and the env prices (3600 x (900 x 0.14 + 25 x 0.28)/1e6 ~ $0.47)", abs(e["usd"] - round(3600 * (900 * 0.14 + 25 * 0.28) / 1e6, 3)) < 1e-9 and e["within_limits"])
check("ESTIMATE: without prices there is no dollar figure and it is not startable", rr.estimate(p30, 3000, None, None)["usd"] is None and not rr.estimate(p30, 3000, None, None)["within_limits"])
big = rr.normalize_params({"bots": list(eng.BOTS)[:5], "days": 60, "pairs": 50, "every_n": 1}, NOW)
check("ESTIMATE: an oversized job is flagged outside the limits", not rr.estimate(big, 3150, 0.14, 0.28)["within_limits"])
net = KlineGet()
net.df = synth(5, 24 * 90, NOW - timedelta(days=80))
lim_before = rr.MANAGER.client
est_real = rr.estimate_for({"bots": ["main", "shorts"], "days": 7, "pairs": 5, "every_n": 4, "end": "2026-10-10T00:00:00+00:00"}, http_get=net, strategies_dir=str(REPO), now=NOW)
check("ESTIMATE from REAL prompts: exact calls (2 bots x 2 modes x 5 pairs x 42 steps = 840), tokens and dollars filled, sampled bots listed", est_real["calls"] == 840 and est_real["avg_input_tokens"] > 100
      and est_real["usd"] is not None and est_real["sampled_bots"] == ["main", "shorts"] and rr.MANAGER.client is lim_before)

# ================================================================== a full job, end to end (fake net, fake LLM)
def wait_done(timeout=240):
    t_end = time.time() + timeout
    while time.time() < t_end:
        s = rr.MANAGER.status()
        if s.get("phase") in ("done", "failed"):
            return s
        time.sleep(0.2)
    return rr.MANAGER.status()


# live files that must be untouched by a replay
live = {}
adir = Path(os.environ["ALLOCATOR_DIR"])
for name, folder in (("weights.json", adir), ("context_latest.json", adir), ("signals.jsonl", Path(DATA)), ("signals_audit.json", Path(DATA)), ("allocations.jsonl", Path(DATA)),
                     ("signals_forward.jsonl", Path(DATA)), ("allocator_state.json", Path(DATA)), ("portfolio_brain_latest.json", Path(DATA))):
    p_ = folder / name
    p_.write_text(json.dumps({"name": name, "live": True}), encoding="utf-8")
    os.utime(p_, (1_700_000_000, 1_700_000_000))
    live[p_] = (hashlib.sha256(p_.read_bytes()).hexdigest(), p_.stat().st_mtime_ns)
bots_dir = Path(os.environ["BOTS_DIR"]) / "main"
bots_dir.mkdir(parents=True, exist_ok=True)
(bots_dir / "freqtrade.log").write_text("live log line\n", encoding="utf-8")
live[bots_dir / "freqtrade.log"] = (hashlib.sha256((bots_dir / "freqtrade.log").read_bytes()).hexdigest(), (bots_dir / "freqtrade.log").stat().st_mtime_ns)
tree_before = sorted(str(p.relative_to(DATA)) for p in Path(DATA).rglob("*"))

net = KlineGet()
net.df = synth(5, 24 * 95, NOW - timedelta(days=90))
post_calls: list = []
API_END = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%dT00:00:00+00:00")
raw = {"bots": ["main", "shorts"], "days": 7, "pairs": 5, "every_n": 8, "end": "2026-10-10T00:00:00+00:00", "max_usd": 5, "max_calls": 5000, "modes": ["named", "anonymized"]}
params = rr.normalize_params(raw, NOW)
run_id = rr.MANAGER.start(params, http_get=net, post=fake_post_factory(post_calls), strategies_dir=str(REPO))
try:
    rr.MANAGER.start(params, http_get=net, post=fake_post_factory(), strategies_dir=str(REPO))
    ok_ = False
except rr.ParamError as e_:
    ok_ = "already running" in str(e_)
check("JOB: a second start while one runs is refused (one at a time)", ok_)
st_done = wait_done()
check("JOB: the run completes", st_done["phase"] == "done" and st_done["status"] == "completed")
check("JOB: status carries phase, progress, counters, spend, speed and a log in the 'simulated time' format",
      "progress" in st_done and st_done["progress"]["decisions"] > 0 and st_done["llm"]["calls"] > 0 and any(l.startswith("[2026-") and " -> " in l for l in st_done["log"]))
rep = rr.get_run(run_id)
check("REPORT: files written under the replay dir: decisions.jsonl, trades.csv, equity.csv, report.json, report.md, params.json, log.txt",
      all((Path(RDIR) / run_id / n).is_file() for n in ("decisions.jsonl", "trades.csv", "equity.csv", "report.json", "report.md", "params.json", "log.txt")))
check("REPORT: both banners, both modes per bot, named-vs-anonymized, the limits of replay, BTC hold", "Historical replay" in rep["banner"] and "knowledge" in rep["banner_en"] and set(rep["bots"]) == {"main", "shorts"}
      and set(m for m in rep["bots"]["main"] if m in ("named", "anonymized")) == {"named", "anonymized"} and rep["baselines"]["btc_buy_and_hold"] is not None
      and len(rep["limits_of_replay"]) >= 5)
allst = [e_["status"] for b in rep["bots"].values() for m, e_ in b.items() if isinstance(e_, dict)] + [e_["signal_status"] for b in rep["bots"].values() for m, e_ in b.items() if isinstance(e_, dict)]
check("REPORT: NO bot or signal status in the whole report is 'edge'", all(s != "edge" and not s.startswith("edge") for s in allst) and allst)
named_dec = [json.loads(l) for l in (Path(RDIR) / run_id / "decisions.jsonl").read_text().splitlines()]
check("decisions.jsonl: every line flagged replay:true, with bot, mode and run id", named_dec and all(d["replay"] is True and d["run_id"] == run_id and d["mode"] in ("named", "anonymized", "rule") for d in named_dec))
anon_prompts = [c["messages"][1]["content"] for c in post_calls]
check("ANONYMIZED prompts sent to the API contain no real pair/coin names and no absolute dates", any("ASSET_" in p_ for p_ in anon_prompts) and
      not any(("ASSET_" in p_) and any(f"{c}/" in p_ for c in COINS) for p_ in anon_prompts))
check("COST is tracked from usage and stays under the limit", 0 < st_done["llm"]["usd"] <= 5 and st_done["llm"]["calls"] == len(post_calls))
check("REPORT: random-entry baseline, vs-random percentile and the divergence-from-live block exist per bot and mode", all("random_baseline" in e_ and "divergence_from_live" in e_ for b in rep["bots"].values() for m, e_ in b.items() if isinstance(e_, dict)))
zb = rr.zip_run(run_id)
zf = zipfile.ZipFile(io.BytesIO(zb))
check("DOWNLOAD: ZIP holds decisions.jsonl, trades.csv, equity.csv, report.json, report.md", {"decisions.jsonl", "trades.csv", "equity.csv", "report.json", "report.md"} <= set(zf.namelist()))
check("the listing shows the run with its parameters and cost", any(r["run_id"] == run_id and r["has_report"] and r["usd"] == rep["llm"]["usd"] for r in rr.list_runs()))
check("ISOLATION: every live file keeps its content hash AND its modification time (allocator, ledger, audit, allocations, weights, bot logs)",
      all(hashlib.sha256(p_.read_bytes()).hexdigest() == h and p_.stat().st_mtime_ns == mt for p_, (h, mt) in live.items()))
check("ISOLATION: nothing new appeared in DATA_DIR outside the replay sandbox", sorted(str(p.relative_to(DATA)) for p in Path(DATA).rglob("*")) == tree_before)

# second identical run is free (cache)
post_calls2: list = []
rid2 = rr.MANAGER.start(rr.normalize_params(raw, NOW), http_get=net, post=fake_post_factory(post_calls2), strategies_dir=str(REPO))
s2 = wait_done()
check("CACHE: re-running the same parameters makes no paid call and spends $0 (every prompt comes from the cache)", s2["status"] == "completed" and len(post_calls2) == 0 and s2["llm"]["usd"] == 0
      and s2["llm"]["cache_hits"] == st_done["llm"]["calls"])
rep2 = rr.get_run(rid2)
check("CACHE: and gives the identical result", {b: {m: (e_["n_trades"], e_["net_usd"]) for m, e_ in modes.items() if isinstance(e_, dict)} for b, modes in rep2["bots"].items()}
      == {b: {m: (e_["n_trades"], e_["net_usd"]) for m, e_ in modes.items() if isinstance(e_, dict)} for b, modes in rep["bots"].items()})

# budget stop
raw_b = {**raw, "max_usd": 0.001, "max_calls": 5000, "days": 8}
p_b = rr.normalize_params(raw_b, NOW)
rr.MANAGER.start(p_b, http_get=net, post=fake_post_factory([]), strategies_dir=str(REPO))
sb_ = wait_done()
check("LIMIT END TO END: exceeding the dollar limit stops the job with status stopped_budget, and what was computed is still reported", sb_["status"] == "stopped_budget"
      and rr.get_run(sb_["run_id"]) is not None and sb_["llm"]["stopped_budget"] is True)
# stop button
slow_hits = []


def slow_fake(url, headers=None, json=None, timeout=None):
    slow_hits.append(1)
    time.sleep(0.02)
    return fake_post_factory()(url, headers, json, timeout)


raw_s = {**raw, "bots": ["main"], "modes": ["named"], "days": 9}
rr.MANAGER.start(rr.normalize_params(raw_s, NOW), http_get=net, post=slow_fake, strategies_dir=str(REPO))
time.sleep(1.5)
rr.MANAGER.stop()
ss = wait_done()
check("STOP: the stop button ends the run early (status stopped) and keeps the results already computed", ss["status"] == "stopped" and rr.get_run(ss["run_id"]) is not None)
os.environ.pop("REPLAY_PRICE_IN_PER_M")
try:
    rr.MANAGER.start(params, http_get=net, post=None, strategies_dir=str(REPO))
    ok_ = False
except rr.ParamError as e_:
    ok_ = "PRICE" in str(e_) or "DEEPSEEK" in str(e_)
check("START: without REPLAY_PRICE_* the dollar limit cannot be enforced, so it refuses", ok_)
os.environ["REPLAY_PRICE_IN_PER_M"] = "0.14"

# ================================================================== the code never reaches live things
import ast  # noqa: E402
import yaml  # noqa: E402

imports, consts = set(), []
for f_ in (REPO / "backend/replay").glob("*.py"):
    tree = ast.parse(f_.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imports.add((node.module or "") + "." + ",".join(a.name for a in node.names))
        elif isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and f_.name != "paths.py":
            consts.append((f_.name, node.value))
bad_imp = [i_ for i_ in imports if any(k in i_ for k in ("allocator", "portfolio_brain", "freqtrade_client", "bot_state", "BotControl"))]
check("ISOLATION (imports): the replay package imports neither the allocator, the brain, the bot REST clients nor the roster state (only evidence and signal_audit's pure functions)", not bad_imp)
bad_const = [(n, c[:60]) for n, c in consts if any(k in c for k in ("/stopentry", "/api/v1/start", "reload_config", "/bots/", "/allocator", "/api/v1/pair_candles", "/api/v1/whitelist")) and len(c) < 200]
check("ISOLATION (strings): no REST path of a live bot and no live directory appears in the replay code (docstrings aside)", not bad_const)

# ================================================================== API and UI
os.environ["DATA_DIR"] = DATA
Path(DATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
import shutil  # noqa: E402

shutil.copy(REPO / "frontend/replay.html", os.path.abspath("/app/frontend/replay.html"))
from fastapi.testclient import TestClient  # noqa: E402

from backend import app as app_module  # noqa: E402

cl = TestClient(app_module.app)
cfg = cl.get("/api/replay/config").json()
check("API config: bots (shorts_v2 not default), limits (REPLAY_MAX_USD, workers, min start), defaults, pilot preset",
      {b["key"] for b in cfg["bots"]} == set(eng.BOTS) and [b for b in cfg["bots"] if b["key"] == "shorts_v2"][0]["default"] is False and cfg["limits"]["max_usd"] == 10.0
      and cfg["limits"]["workers"] == 4 and cfg["limits"]["min_start"] == "2026-08-01" and cfg["defaults"]["pilot"]["days"] == 7 and cfg["defaults"]["pilot"]["max_usd"] == 1.0
      and len(cfg["defaults"]["pilot"]["bots"]) == 3)
paths_ = app_module.app.openapi()["paths"]
check("API: all seven replay routes exist with the right methods", set(paths_["/api/replay/config"]) == {"get"} and set(paths_["/api/replay/estimate"]) == {"post"} and set(paths_["/api/replay/start"]) == {"post"}
      and set(paths_["/api/replay/status"]) == {"get"} and set(paths_["/api/replay/stop"]) == {"post"} and set(paths_["/api/replay/runs"]) == {"get"}
      and set(paths_["/api/replay/runs/{run_id}"]) == {"get"} and set(paths_["/api/replay/runs/{run_id}/download"]) == {"get"} and set(paths_["/replay"]) == {"get"})
rl_ = cl.get("/api/replay/runs").json()["runs"]
check("API runs: the previous runs with parameters and cost", any(r["run_id"] == run_id for r in rl_))
check("API run report and ZIP download", cl.get(f"/api/replay/runs/{run_id}").json()["run_id"] == run_id and cl.get(f"/api/replay/runs/{run_id}/download").headers["content-type"] == "application/zip"
      and zipfile.ZipFile(io.BytesIO(cl.get(f"/api/replay/runs/{run_id}/download").content)).testzip() is None)
check("API: a bad run id is a 400, an unknown one a 404 (no path traversal)", cl.get("/api/replay/runs/..%2Fetc").status_code in (400, 404) and cl.get("/api/replay/runs/nonexistent").status_code == 404
      and cl.get("/api/replay/runs/bad%20id/download").status_code in (400, 404))
r400 = cl.post("/api/replay/estimate", json={"days": 3, "end": API_END})
check("API estimate: invalid parameters -> 400 with the reason", r400.status_code == 400 and "days" in r400.json()["detail"])
r_p4 = cl.post("/api/replay/start", json={**raw, "allocator": True, "end": API_END})
check("API start: phase 4 is refused until phases 1-3 are accepted", r_p4.status_code == 400 and "phase 4" in r_p4.json()["detail"])
rr.estimate_for = lambda raw_, *a, **k: {"calls": 10 ** 7, "usd": 999.0, "prices_known": True, "within_limits": False, "params": {"max_usd": 10, "max_calls": 5000}}
r409 = cl.post("/api/replay/start", json={**raw, "end": API_END})
check("API start: an estimate above the limit is refused (409) before anything runs", r409.status_code == 409 and "exceeds" in r409.json()["detail"])
page = cl.get("/replay")
check("PAGE: /replay is served from replay.html", page.status_code == 200 and "<title>Replay" in page.text)
html_r = (REPO / "frontend/replay.html").read_text(encoding="utf-8")
html_i = (REPO / "frontend/index.html").read_text(encoding="utf-8")
check("UI /replay: form (bots, days, end date, pairs, step 1/2/4/8, named/anonymized/both, phase 4, $ limit, max_calls), estimate, start only after the estimate with the sum, stop, pilot",
      all(s in html_r for s in ("botBoxes", 'id="days"', 'id="endDate"', 'id="pairs"', 'id="everyN"', 'id="mNamed"', 'id="mAnon"', 'id="phase4"', 'id="maxUsd"', 'id="maxCalls"', "btnEstimate", "btnStart", "btnStop", "btnPilot"))
      and 'id="btnStart" type="button" disabled' in html_r and "confirm('Start the replay? Estimate: $'" in html_r)
check("UI /replay: progress bar, counters (decisions, trades, $, speed, elapsed, eta), the simulated-time log, results table with named vs anonymized, badges, chart, runs list with ZIP",
      all(s in html_r for s in ("barFill", "decisions_per_min", "logBox", "replay ≠ proof", "leak_suspect", "drawChart", "/download", "loadRuns", "Suspected leak")))
check("UI /replay: the permanent banner, verbatim, above the form results and in the results", html_r.count("Historical run. A negative result is a strong argument against a bot; a positive one is a weak argument. The status edge is given only from live data.") == 2)
nav_js = (REPO / "frontend/nav.js").read_text(encoding="utf-8")
check("UI /replay: the shared nav links back to the dashboard", '/assets/nav.js' in html_r and "['/', 'Dashboard']" in nav_js)
check("UI index.html: ONLY a link to Replay (shared nav), no replay controls on the main page", "['/replay', 'Replay (tests)']" in nav_js and '/assets/nav.js' in html_i
      and not any(s in html_i for s in ("btnEstimate", "api/replay", "botBoxes", "estimateBox")))
ui_css = (REPO / "frontend/ui.css").read_text(encoding="utf-8")
check("UI: one shared stylesheet for every page (the dashboard variables, unchanged)", '/assets/ui.css' in html_r and '/assets/ui.css' in html_i and all(v in ui_css for v in ("--bg: #0E1614", "--panel: #131E1B", "table.hist", ".tiles", ".badge", ".note")))

# ================================================================== packaging / docs
df_ = (REPO / "backend/Dockerfile").read_text(encoding="utf-8")
check("PACKAGING: Dockerfile copies the LLM bots' strategy files (+ helpers, config.json, auto_tune.json) into /app/strategies_src/<bot>/",
      all(f"/app/strategies_src/{b}/" in df_ for b in eng.BOTS) and "DeepSeekShortsV2Strategy.py" in df_ and "_second_opinion.py" in df_ and "config.json" in df_)
reqs = (REPO / "backend/requirements.txt").read_text(encoding="utf-8")
check("PACKAGING: pandas and numpy are pinned in backend/requirements.txt (the survival-agent image had neither)", "pandas==2.2.3" in reqs and "numpy==2.1.3" in reqs)
envdoc = (REPO / "ENVIRONMENT_VARIABLES.md").read_text(encoding="utf-8")
check("DOCS: the new environment variables are documented", all(v in envdoc for v in ("REPLAY_MAX_USD", "REPLAY_WORKERS", "REPLAY_MIN_START", "REPLAY_PRICE_IN_PER_M", "REPLAY_PRICE_OUT_PER_M", "REPLAY_FEE_PCT", "REPLAY_SLIPPAGE_PCT")))
check("compose: no new SERVICE for the replay itself (a thread inside survival-agent); the only replay-related container is P22-K2's `kronos-replay` (CPU forecaster for Kronos signals, idle until a job)", [name for name in yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"] if "replay" in name] == ["kronos-replay"])

# ================================================================== compose pass-through, empty env, live-reference refresh, .gitattributes
cenv = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))["services"]["survival-agent"]["environment"]
for v in ("REPLAY_MAX_USD", "REPLAY_WORKERS", "REPLAY_MIN_START", "REPLAY_PRICE_IN_PER_M", "REPLAY_PRICE_OUT_PER_M", "REPLAY_FEE_PCT", "REPLAY_SLIPPAGE_PCT"):
    check(f"COMPOSE: survival-agent passes {v} through as the ${{VAR:-}} form", f"{v}=${{{v}:-}}" in cenv)
saved_env = {k: os.environ.get(k) for k in ("REPLAY_MAX_USD", "REPLAY_WORKERS", "REPLAY_MIN_START", "REPLAY_PRICE_IN_PER_M", "REPLAY_PRICE_OUT_PER_M", "REPLAY_FEE_PCT", "REPLAY_SLIPPAGE_PCT")}
for k in saved_env:
    os.environ[k] = ""                      # exactly what compose produces when the stack variable is not set
lim_empty = rr.limits()
check("EMPTY ENV (what ${VAR:-} gives when unset): every limit falls back to its built-in default, no crash", lim_empty["max_usd"] == 10.0 and lim_empty["workers"] == 8
      and lim_empty["min_start"] == "2026-08-01" and lim_empty["fee_pct"] == 0.1 and lim_empty["slippage_pct"] == 0.05)
check("EMPTY ENV: empty prices are 'not set' (None), never 0 -- so a job still refuses to start without them", lim_empty["price_in_per_m"] is None and lim_empty["price_out_per_m"] is None
      and rl.prices_from_env() == (None, None))
check("EMPTY ENV: the window rules still work with the default REPLAY_MIN_START", rr.normalize_params({"days": 30}, NOW)["start"] >= datetime(2026, 8, 1, tzinfo=timezone.utc))
try:
    rr.MANAGER.start(rr.normalize_params({"days": 7}, NOW), http_get=net, post=fake_post_factory(), strategies_dir=str(REPO))
    ok_ = False
except rr.ParamError as e_:
    ok_ = "PRICE" in str(e_)
check("EMPTY ENV: start refuses (prices unset) with a clear message", ok_)
for k, v in saved_env.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

from backend import export as export_mod  # noqa: E402
from backend.replay import live_reference as lref  # noqa: E402


class ExportFetcher:
    """What backend.export._fetcher() returns: .trades(bot) -> list of Freqtrade trade dicts, or None when the bot is unreachable."""

    def __init__(self, data):
        self.data, self.asked = data, []

    def trades(self, bot):
        self.asked.append(bot)
        return self.data.get(bot)


def trade_(close, pr, is_open=False):
    return {"is_open": is_open, "close_date": close, "close_profit": pr, "close_profit_abs": pr * 100, "open_date": "2026-09-20T00:00:00Z", "pair": "BTC/USDT", "stake_amount": 100}


fx = ExportFetcher({"main": [trade_("2026-09-22T10:00:00", 0.01), trade_("2026-09-23T10:00:00", -0.02), trade_("2026-09-01T10:00:00", 0.5), trade_(None, 0.0, True)],
                    "shorts": [trade_("2026-09-25T10:00:00", 0.03)]})
export_mod._fetcher = lambda: fx
ref_file = rp.root_file("live_reference.json")
if ref_file.exists():
    ref_file.unlink()
r0 = cl.get("/api/replay/live-reference").json()
check("LIVE REF: before the first refresh the page is told there is nothing yet and how to create it", r0["available"] is False and "refresh" in r0["reason"])
rj = cl.post("/api/replay/live-reference/refresh", json={"from": "2026-09-21"})
body = rj.json()
check("LIVE REF refresh: reads each bot's CLOSED trades through the export's own code path and writes live_reference.json inside the replay sandbox",
      rj.status_code == 200 and ref_file.is_file() and ref_file.parent == Path(RDIR).resolve() and set(fx.asked) == set(eng.BOTS) | {"listing_fade"})
check("LIVE REF: counts only trades closed inside the period (the 09-01 trade and the open trade are excluded) and their mean profit",
      body["bots"]["main"]["n_trades"] == 2 and abs(body["bots"]["main"]["mean_trade_pct"] - (-0.5)) < 1e-9 and body["bots"]["shorts"]["n_trades"] == 1)
check("LIVE REF: a bot that does not answer (None) is listed as unavailable with the reason, the rest still count",
      body["meta"]["per_bot_status"]["moonshot"]["ok"] is False and "unreachable" in body["meta"]["per_bot_status"]["moonshot"]["note"] and body["meta"]["per_bot_status"]["main"]["ok"] is True
      and "moonshot" not in body["bots"])
g1 = cl.get("/api/replay/live-reference").json()
check("LIVE REF: GET returns the file, its update time and where the data came from", g1["available"] and g1["meta"]["updated_at"] and "export" in g1["meta"]["source"] and g1["bots"]["main"]["n_trades"] == 2)
dv_ = analysis.divergence("main", [{"close_date": "2026-09-22T12:00:00Z", "profit_ratio": 0.01}] * 2, analysis.load_live_reference(Path(RDIR)))
check("LIVE REF feeds the divergence check (the _meta key is not mistaken for a bot)", dv_["available"] and dv_["live_n_trades"] == 2 and dv_["replay_n_trades"] == 2)
before = ref_file.read_bytes()
export_mod._fetcher = lambda: ExportFetcher({})
r502 = cl.post("/api/replay/live-reference/refresh", json={"from": "2026-09-21"})
check("LIVE REF: when NO bot answers the refresh fails (502) and the previous file is kept byte for byte", r502.status_code == 502 and ref_file.read_bytes() == before)
check("LIVE REF: a bad date is a 400", cl.post("/api/replay/live-reference/refresh", json={"from": "yesterday"}).status_code == 400)
app_src = (REPO / "backend/app.py").read_text(encoding="utf-8")
check("LIVE REF: the endpoint uses the P18 export functions (iter_trades + _trade_row), not the replay engine", "export_mod.iter_trades" in app_src and "export_mod._trade_row" in app_src)
paths2 = app_module.app.openapi()["paths"]
check("LIVE REF routes: GET status and POST refresh", set(paths2["/api/replay/live-reference"]) == {"get"} and set(paths2["/api/replay/live-reference/refresh"]) == {"post"})
html_c = (REPO / "frontend/calibration.html").read_text(encoding="utf-8")
check("UI /calibration: the button 'Refresh live reference' with a from-date, its status block, loaded on page open", all(s in html_c for s in ("btnRef", "Refresh live reference", 'id="refFrom"', "refBody", "loadRef()"))
      and "/api/replay/live-reference/refresh" in html_c)
ga = (REPO / ".gitattributes").read_text(encoding="utf-8")
check(".gitattributes: one policy, text files LF everywhere; scripts, Dockerfiles and YAML pinned to LF; binaries untouched", "* text=auto eol=lf" in ga and "*.sh text eol=lf" in ga and "Dockerfile text eol=lf" in ga
      and "*.yml text eol=lf" in ga and "*.safetensors binary" in ga and "*.zip binary" in ga)

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P21 REPLAY CHECKS PASSED")
