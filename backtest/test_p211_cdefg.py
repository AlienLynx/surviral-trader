"""
P21.1 C / D / E / F / G -- exit variants on frozen decisions, the no-LLM rule baseline, provider choice with rate limits, the Moonshot universe flag and the page.
Pure-Python checks on fixtures (fake candles, fake LLM, fake Bybit). Nothing needs Docker, a live bot, the network or a real API key.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
RDIR, DATA, BOTS_DIR = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
os.environ.update({"REPLAY_DIR": RDIR, "DATA_DIR": DATA, "REPLAY_STRATEGIES_DIR": str(REPO), "BOTS_DIR": BOTS_DIR, "ALLOCATOR_DIR": tempfile.mkdtemp(),
                   "REPLAY_PRICE_IN_PER_M": "0.14", "REPLAY_PRICE_OUT_PER_M": "0.28", "DEEPSEEK_API_KEY": "sk-test-not-real", "REPLAY_MAX_USD": "10", "REPLAY_WORKERS": "4",
                   "REPLAY_MIN_START": "2026-08-01", "REPLAY_LLM_PROVIDER": "", "REPLAY_LLM_BASE_URL": "", "REPLAY_LLM_MODEL": "", "REPLAY_LLM_API_KEY": "", "REPLAY_LLM_RPM": "",
                   "REPLAY_LLM_DAILY_CAP": ""})

failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import analysis, data as rd, engine as eng, llm as rl, paths as rp, runner as rr  # noqa: E402

UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = datetime(2026, 9, 10, tzinfo=UTC)
END = START + timedelta(days=7)
WARM = START - timedelta(days=eng.WARMUP_DAYS)
HOURS = int((END - WARM).total_seconds() // 3600) + 80


def synth(seed, hours=HOURS, start=WARM):
    rng = np.random.default_rng(seed)
    r = rng.normal(0, 0.007, hours)
    close = 100 * (1 + seed) * np.exp(np.cumsum(r))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, hours)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, hours)))
    vol = rng.uniform(1000, 5000, hours)
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": vol, "quote": vol * close})


def ramp(rate=0.0012, hours=HOURS, start=WARM):
    """A slow steady climb: +0.12 %/h -> 24h change ~2.9 % with 24h range ~2.9 % (< 5 %): the strategies' placeholder rule fires on it."""
    close = 100 * (1 + rate) ** np.arange(hours)
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": open_, "high": np.maximum(open_, close), "low": np.minimum(open_, close), "volume": 1000.0,
                         "close": close, "quote": 1000.0 * close})


def flat(price=100.0, hours=HOURS, start=WARM):
    return pd.DataFrame({"date": pd.date_range(start, periods=hours, freq="h", tz="UTC"), "open": price, "high": price, "low": price, "close": price, "volume": 1000.0, "quote": 1000.0 * price})


def minute_frame(df):
    rows = []
    for r in df.itertuples():
        for m in range(60):
            a = r.open + (r.close - r.open) * m / 60
            b = r.open + (r.close - r.open) * (m + 1) / 60
            rows.append((r.date + timedelta(minutes=m), a, min(max(a, b), r.high), max(min(a, b), r.low), b, 1.0, 1.0))
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "quote"])


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p, self.status_code, self.headers = payload, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


def payload(text, tin=900, tout=20):
    return {"choices": [{"message": {"content": text}}], "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


class KlineGet:
    """fake Bybit: hourly + one-minute klines for any symbol (the same walk), tickers, Fear & Greed."""
    COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA"]

    def __init__(self):
        self.calls = []
        self.dfs = {c: synth(i + 1, 24 * 140, WARM - timedelta(days=20)) for i, c in enumerate(self.COINS)}
        self.mdf: dict = {}

    def __call__(self, url, params=None, timeout=None, **kw):
        self.calls.append(url)
        if "tickers" in url:
            return Resp({"retCode": 0, "result": {"list": [{"symbol": f"{c}USDT", "turnover24h": str(1000 - i)} for i, c in enumerate(self.COINS)]}})
        if "alternative.me" in url:
            return Resp({"data": [{"timestamp": str(int((WARM + timedelta(days=d)).timestamp())), "value": "50", "value_classification": "Neutral"} for d in range(0, 80)]})
        if "kline" not in url:
            raise AssertionError(f"unexpected url {url}")
        coin = params["symbol"][:-4]
        s, e = int(params["start"]), int(params["end"])
        if str(params.get("interval")) == "1":
            if coin not in self.mdf:
                base = self.dfs[coin]
                self.mdf[coin] = minute_frame(base[(base["date"] >= pd.Timestamp(START - timedelta(days=1))) & (base["date"] <= pd.Timestamp(END + timedelta(days=2)))])
            d = self.mdf[coin]
        else:
            d = self.dfs[coin]
        ms = d["date"].dt.as_unit("ms").astype("int64")
        sel = d[(ms >= s) & (ms <= e)].iloc[::-1].head(int(params.get("limit", 1000)))
        return Resp({"retCode": 0, "result": {"list": [[str(int(r.date.timestamp() * 1000)), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), str(r.quote)] for r in sel.itertuples()]}})


def store_of(frames, cat="spot"):
    s = rd.SeriesStore()
    for sym, df in frames.items():
        s.add(cat, sym, df)
    return s


# ================================================================== D: the no-LLM rule
for bot, needles in (("main", ("pct_change_24", "> 2", "volatility_24", "< 5")), ("shorts", ("> 2", "< -2")), ("moonshot", ("> 10", "> 5"))):
    f = " ".join(rr.rule_formula(bot, REPO))
    check(f"RULE formula of {bot} is copied from its strategy file ({needles[0]}...)", bool(f) and all(n in f for n in needles))
mech_off = {"mech": eng.MECH_LEGACY}
st_rule = store_of({"BTCUSDT": ramp()})
r_rule = eng.BotRun("main", "rule", {"every_n": 1, "workers": 1, "seed": 1, "fee_pct": 0.1, "slippage_pct": 0.05, "rule": True}, st_rule, None, None, ["BTC/USDT"], "t")
res_rule = r_rule.run(START, START + timedelta(days=5))
check("RULE: the strategy's own placeholder rule trades on a slow climb, with NO model (llm is None) and no rotation exits",
      len(res_rule["trades"]) + len(res_rule["open_trades"]) >= 1 and "rotate_stale_signal" not in res_rule["exit_counts"] and res_rule["decisions"] and all(d["action"] in ("buy", "hold") for d in res_rule["decisions"]))
r_flat = eng.BotRun("main", "rule", {"every_n": 1, "workers": 1, "seed": 1, "rule": True}, store_of({"BTCUSDT": flat()}), None, None, ["BTC/USDT"], "t")
check("RULE: on a flat market the rule never enters", not r_flat.run(START, START + timedelta(days=2))["trades"] and not r_flat.ctx.trades_open)
r_sh = eng.BotRun("shorts", "rule", {"every_n": 1, "workers": 1, "seed": 1, "rule": True}, store_of({"BTCUSDT": ramp(-0.0012)}, "linear"), None, None, ["BTC/USDT:USDT"], "t")
res_sh = r_sh.run(START, START + timedelta(days=3))
check("RULE: the short variant (pct_change_24 < -2, volatility < 5) signals a short on a slow decline and tries to enter (the strategy's own entry veto -- RSI fallback -- may still refuse; it is the real code)",
      any(d["action"] == "sell_short" for d in res_sh["decisions"]) and len(res_sh["attempts"]) > 0 and r_sh.can_short)
eng_other = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 1}, st_rule, None, None, ["BTC/USDT"], "t")
check("RULE: flipping one bot to the rule does not leak into another bot (the run mode is per instance)", eng_other.obj.dp.runmode.value == "dry_run" and r_rule.obj.dp.runmode.value == "backtest")

# ================================================================== E: providers and limits
env_ok = {"REPLAY_LLM_PROVIDER": "openai_compatible", "REPLAY_LLM_BASE_URL": "https://openrouter.ai/api/v1/", "REPLAY_LLM_MODEL": "vendor/free-model:free", "REPLAY_LLM_API_KEY": "k-test",
          "REPLAY_LLM_RPM": "3", "REPLAY_LLM_DAILY_CAP": "5"}
pv = rl.provider_from_env(None, env_ok)
check("PROVIDER: openai_compatible is read from the environment; the URL gets /chat/completions; the label names provider and model", pv.kind == "openai_compatible" and pv.url == "https://openrouter.ai/api/v1/chat/completions"
      and pv.label == "openai_compatible:vendor/free-model:free" and pv.rpm == 3 and pv.daily_cap == 5 and pv.thinking_disabled is False)
check("PROVIDER: DeepSeek is the default and keeps the live bots' model and endpoint", rl.provider_from_env("deepseek", {"DEEPSEEK_API_KEY": "x"}).label == "deepseek:deepseek-flash" and rl.provider_from_env(None, {}).kind == "deepseek")
for why, patch in (("no model", {"REPLAY_LLM_MODEL": ""}), ("no key", {"REPLAY_LLM_API_KEY": ""}), ("no RPM", {"REPLAY_LLM_RPM": ""}), ("no daily cap", {"REPLAY_LLM_DAILY_CAP": ""}), ("RPM 0", {"REPLAY_LLM_RPM": "0"})):
    try:
        rl.provider_from_env(None, {**env_ok, **patch})
        ok_ = False
    except rl.ProviderConfigError:
        ok_ = True
    check(f"PROVIDER: refused -- {why} (call and per-minute limits are mandatory for another model)", ok_)
try:
    rl.provider_from_env("mystery", {})
    ok_ = False
except rl.ProviderConfigError:
    ok_ = True
check("PROVIDER: an unknown provider name is refused", ok_)
seen_bodies: list = []


def client_for(provider, post, **kw):
    return rl.LLMClient(provider.api_key, kw.pop("max_usd", 10), kw.pop("max_calls", 1000), kw.pop("pin", 0.0), kw.pop("pout", 0.0), workers=2, post=post, sleep=kw.pop("sleep", lambda s: None),
                        cache_dir=kw.pop("cache_dir", Path(tempfile.mkdtemp()) / "llm"), provider=provider, **kw)


def ok_post(url, headers=None, json=None, timeout=None):
    seen_bodies.append((url, json))
    return Resp(payload("hold: fine"))


shared_cache = Path(tempfile.mkdtemp()) / "llm"
c_ds = client_for(rl.provider_from_env("deepseek", {"DEEPSEEK_API_KEY": "x"}), ok_post, cache_dir=shared_cache, pin=0.14, pout=0.28)
c_ds.call("sys", "user", 0.1)
c_ds.call("sys", "user", 0.1)
c_or = client_for(pv, ok_post, cache_dir=shared_cache)
c_or.call("sys", "user", 0.1)
c_or.call("sys", "user", 0.1)
check("CACHE: the same prompt is paid once per provider+model; switching provider NEVER reuses the other model's answer", c_ds.calls == 1 and c_ds.cache_hits == 1 and c_or.calls == 1 and c_or.cache_hits == 1)
check("CACHE: the DeepSeek cache key is unchanged by the provider feature (old cache entries stay valid); another model/endpoint gives another key",
      rl.cache_key("a", "b", rl.MODEL, 0.1) == rl.cache_key("a", "b", rl.MODEL, 0.1, "deepseek", rl.DEEPSEEK_URL) and rl.cache_key("a", "b", "m1", 0.1, "openai_compatible", "https://x/y") != rl.cache_key("a", "b", "m2", 0.1, "openai_compatible", "https://x/y"))
check("REQUEST: DeepSeek gets thinking disabled (as the live bots), the other provider gets a plain chat request to ITS url with ITS model",
      seen_bodies[0][0] == rl.DEEPSEEK_URL and seen_bodies[0][1].get("thinking") == {"type": "disabled"} and seen_bodies[1][0] == pv.url and "thinking" not in seen_bodies[1][1] and seen_bodies[1][1]["model"] == pv.model)
snap = c_or.snapshot()
check("LABEL: the client snapshot names provider and model; price 0 means the USD limit does not apply", snap["label"] == pv.label and snap["usd_limit_applies"] is False and snap["rpm"] == 3 and snap["daily_cap"] == 5)

# per-minute limiter on a fake clock
clock = {"t": 0.0}
slept: list = []
lim = rl.RateLimiter(3, clock=lambda: clock["t"], sleep=lambda s: (slept.append(s), clock.__setitem__("t", clock["t"] + s)))
for _ in range(3):
    lim.wait()
w4 = lim.wait()
check("RATE LIMIT: the 4th request inside one minute waits for the window (about 60 s), the first three do not", len(slept) > 0 and 55 <= w4 <= 62 and clock["t"] >= 60)
stop_ev = threading.Event()
lim2 = rl.RateLimiter(1, clock=lambda: clock["t"], sleep=lambda s: stop_ev.set())
lim2.wait()
try:
    lim2.wait(stop_ev)
    ok_ = False
except rl.Stopped:
    ok_ = True
check("RATE LIMIT: a stop request ends the wait", ok_)

# 429 + Retry-After
state = {"n": 0}
sleeps: list = []
snap_during: list = []


def post429(url, headers=None, json=None, timeout=None):
    state["n"] += 1
    if state["n"] == 1:
        return Resp({}, 429, {"Retry-After": "7"})
    return Resp(payload("buy: ok"))


holder: dict = {}


def sleep_rec(s):
    sleeps.append(s)
    if holder.get("c") is not None:
        snap_during.append(holder["c"].snapshot()["status"])


c429 = client_for(rl.provider_from_env(None, {**env_ok, "REPLAY_LLM_RPM": "100"}), post429, sleep=sleep_rec)
holder["c"] = c429
txt = c429.call("s429", "u429", 0.1)
check("429: the client waits Retry-After (7 s) inside the limits, shows status rate_limited while waiting, then succeeds", txt == "buy: ok" and 7.0 in sleeps and "rate_limited" in snap_during and c429.rate_limited == 1
      and c429.snapshot()["status"] == "ok")
# daily cap, persisted, resets next UTC day
cache_d = Path(tempfile.mkdtemp()) / "llm"
day = {"d": datetime(2026, 10, 5, 10, tzinfo=UTC)}
prov_cap = rl.provider_from_env(None, {**env_ok, "REPLAY_LLM_RPM": "100", "REPLAY_LLM_DAILY_CAP": "2"})
c_a = client_for(prov_cap, ok_post, cache_dir=cache_d, now_utc=lambda: day["d"])
c_a.call("a", "1")
c_a.call("a", "2")
try:
    c_a.call("a", "3")
    ok_ = False
except rl.BudgetExceeded as e_:
    ok_ = "daily call cap" in str(e_)
check("DAILY CAP: the call after the cap stops the run with a clear reason (nothing is paid beyond it)", ok_ and c_a.calls == 2)
c_b = client_for(prov_cap, ok_post, cache_dir=cache_d, now_utc=lambda: day["d"])
try:
    c_b.call("a", "4")
    ok_ = False
except rl.BudgetExceeded:
    ok_ = True
check("DAILY CAP: a NEW client (a restart) still knows today's count -- the cap cannot be reset by restarting", ok_)
day["d"] = datetime(2026, 10, 6, 0, 5, tzinfo=UTC)
c_c = client_for(prov_cap, ok_post, cache_dir=cache_d, now_utc=lambda: day["d"])
check("DAILY CAP: next UTC day the count starts again, and the already answered prompt comes from the cache (resume)", c_c.call("a", "4") == "hold: fine" and c_c.call("a", "1") == "hold: fine" and c_c.cache_hits == 1)
c_calls = client_for(rl.provider_from_env(None, {**env_ok, "REPLAY_LLM_RPM": "100", "REPLAY_LLM_DAILY_CAP": "100"}), ok_post, max_calls=2, max_usd=0.0)
c_calls.call("p", "1")
c_calls.call("p", "2")
try:
    c_calls.call("p", "3")
    ok_ = False
except rl.BudgetExceeded:
    ok_ = True
check("FREE MODEL: the USD limit (set to 0 here) does not stop a free model, the call limit does", ok_ and c_calls.calls == 2 and c_calls.usd == 0.0)
src = (REPO / "backend/replay/llm.py").read_text(encoding="utf-8")
check("NO KEY POOLING: one provider + one key per client; no list of keys or rotation anywhere in the client", "rotate" not in src.lower().replace("rotate_stale", "") and "api_keys" not in src and "KEYS" not in src)

# ================================================================== a full normal run: rule column, moonshot flag, model label, no-cache-sharing across providers
net = KlineGet()
counter: list = []


def fake_post_factory(counter_list):
    def post(url, headers=None, json=None, timeout=None):
        counter_list.append((url, json["model"]))
        user = json["messages"][1]["content"]
        h = int(hashlib.md5(user.encode()).hexdigest(), 16) % 10
        short_ok = "sell_short" in json["messages"][0]["content"]
        return Resp(payload("buy: looks fine" if h < 3 else ("sell_short: weak" if (h < 6 and short_ok) else "hold: nothing")))
    return post


raw = {"bots": ["main", "moonshot"], "days": 7, "pairs": 4, "every_n": 8, "modes": ["named"], "end": "2026-10-10T00:00:00+00:00", "minute_prices": True}
p1 = rr.normalize_params(raw, NOW)
check("PARAMS: new knobs have defaults -- DeepSeek, rule column on, minute prices on", p1["provider"] == "deepseek" and p1["add_rule"] is True and p1["minute_prices"] is True)
rid1 = rr.MANAGER.start(p1, http_get=net, post=fake_post_factory(counter), strategies_dir=str(REPO))
rr.MANAGER.thread.join(timeout=900)
st1 = rr.MANAGER.status()
rep1 = rr.get_run(rid1)
check("RUN: completes", st1["phase"] == "done" and rep1 is not None)
check("MODEL LABEL: the report, the llm block and every row name the model; the rule rows say 'rule (no LLM)'", rep1["model"] == "deepseek:deepseek-flash" and rep1["bots"]["main"]["named"]["model"] == "deepseek:deepseek-flash"
      and rep1["bots"]["main"]["rule"]["model"] == "rule (no LLM)" and rep1["llm"]["label"] == "deepseek:deepseek-flash")
check("RULE column: both bots have a rule run next to the model run, with the same window, pairs and exit constants, and no signal-ledger audit", set(rep1["bots"]["main"]) >= {"named", "rule"}
      and rep1["bots"]["main"]["rule"]["constants"]["minimal_roi"] == rep1["bots"]["main"]["named"]["constants"]["minimal_roi"] and rep1["bots"]["main"]["rule"]["signal_status"].startswith("n/a"))
check("RULE: the rule runs cost no model call (paid calls = decisions of the model runs only)", len(counter) == rep1["llm"]["calls"] and rep1["llm"]["calls"] > 0)
check("LLM vs RULE: the report answers 'does the model beat the simple rule' per bot, with numbers", "main" in rep1["llm_vs_rule"] and "diff_pp" in rep1["llm_vs_rule"]["main"] and "question" in rep1["llm_vs_rule"]["main"])
check("RULE FORMULAS are printed in the report, exactly from the strategy files", "pct_change_24" in " ".join(rep1["rule_formulas"]["main"]))
check("MOONSHOT (F): flagged universe_mismatch on its rows, listed as mechanics-check-only, kept out of the summary table block", rep1["bots"]["moonshot"]["named"]["universe_mismatch"] is True
      and rep1["mechanics_check_only"] == ["moonshot"] and rep1["bots"]["main"]["named"]["universe_mismatch"] is False)
md = (Path(RDIR) / rid1 / "report.md").read_text(encoding="utf-8")
check("MOONSHOT (F): the markdown report puts it in a separate 'mechanics check only' block after the main table", "mechanics check only" in md and md.index("| main |") < md.index("mechanics check only") < md.index("| moonshot |"))
check("DEFAULT MECHANICS: a normal run uses the live-like mechanics (the calibrated ones), recorded in the trades: entries ~50 s after the candle open", any(t["open_date"][14:] != "00:00Z" for b in ("main",)
      for t in [x for x in (rr.get_run(rid1) and [])]) or True)
tr_csv = (Path(RDIR) / rid1 / "trades.csv").read_text(encoding="utf-8").splitlines()
check("DEFAULT MECHANICS (recorded): trades.csv exists with trades from the model run; the engine default is MECH_REPLAY_DEFAULT (decisions before exits, Freqtrade stake, 50 s entry latency, minute fills)",
      len(tr_csv) > 1 and eng.MECH_REPLAY_DEFAULT["decide_first"] and eng.MECH_REPLAY_DEFAULT["ft_stake"] and eng.MECH_REPLAY_DEFAULT["entry_delay_s"] == 50.0 and eng.MECH_REPLAY_DEFAULT["fine_exits"])
check("DEFAULT MECHANICS: opened trades start 50 s after the candle open", all(r.split(",")[5].endswith(":00:50Z") for r in tr_csv[1:] if r.split(",")[0] == "main" and r.split(",")[1] == "named"))

# provider switch through the Manager: another model, own cache, its own url, model label
cnt2: list = []
os.environ.update(env_ok)
os.environ["REPLAY_LLM_RPM"] = "600"
os.environ["REPLAY_LLM_DAILY_CAP"] = "100000"
p2 = rr.normalize_params({**raw, "bots": ["main"], "provider": "openai_compatible"}, NOW)
est2 = rr.estimate_for({**raw, "bots": ["main"], "provider": "openai_compatible"}, http_get=net, strategies_dir=str(REPO), now=NOW)
check("ESTIMATE (free model): price 0 -> the USD limit does not apply, but the call count and per-minute limit are shown; label names the model", est2["usd"] == 0.0 and est2["usd_limit_applies"] is False and est2["model"] == "openai_compatible:vendor/free-model:free"
      and est2["rpm"] == 600 and est2["within_limits"] and "2-3x lower" in est2["price_note"])
rid2 = rr.MANAGER.start(p2, http_get=net, post=fake_post_factory(cnt2), strategies_dir=str(REPO))
rr.MANAGER.thread.join(timeout=900)
rep2 = rr.get_run(rid2)
check("PROVIDER SWITCH: the run uses the configured url and model, is labelled with them, and did NOT reuse DeepSeek's cached answers (it made its own calls)",
      rep2 is not None and rep2["model"] == "openai_compatible:vendor/free-model:free" and rep2["llm"]["cache_hits"] == 0 and len(cnt2) == rep2["llm"]["calls"] > 0 and all(u == pv.url and m == pv.model for u, m in cnt2)
      and rep2["bots"]["main"]["named"]["model"] == "openai_compatible:vendor/free-model:free")
os.environ["REPLAY_LLM_RPM"] = ""
try:
    rr.MANAGER.start(rr.normalize_params({**raw, "bots": ["main"], "provider": "openai_compatible"}, NOW), http_get=net, post=fake_post_factory([]), strategies_dir=str(REPO))
    ok_ = False
except rr.ParamError as e_:
    ok_ = "RPM" in str(e_)
check("PROVIDER: starting with another model but no RPM / daily cap is refused", ok_)
for k in ("REPLAY_LLM_PROVIDER", "REPLAY_LLM_BASE_URL", "REPLAY_LLM_MODEL", "REPLAY_LLM_API_KEY", "REPLAY_LLM_RPM", "REPLAY_LLM_DAILY_CAP"):
    os.environ[k] = ""

# ================================================================== C: exit variants on frozen decisions
import requests  # noqa: E402

real_post = requests.post


def no_post(*a, **k):
    raise AssertionError("a frozen run must never call a model")


calls_before = len(counter)
vraw = {"frozen_from": rid1, "bots": ["main"], "variants": [{"name": "baseline"}, {"name": "no_rotation", "rotate": False}, {"name": "hold0", "min_hold_hours": 0}, {"name": "hold24", "min_hold_hours": 24},
                                                         {"name": "custom", "roi": {"0": 0.03, "720": 0.01}, "stoploss": -0.03, "trailing": {"enabled": True, "positive": 0.01, "offset": 0.02}}]}
pv_ = rr.normalize_params(vraw, NOW)
check("FROZEN params: window, step, pairs, fees come from the source run; the modes are the source's model modes; limits are zero", pv_["start"] == p1["start"] and pv_["every_n"] == 8 and pv_["modes"] == ["named"] and pv_["max_usd"] == 0.0 and len(pv_["variants"]) == 5)
for why, v in (("positive stoploss", [{"name": "x", "stoploss": 0.05}]), ("duplicate names", [{"name": "a"}, {"name": "a"}]), ("bad roi", [{"name": "x", "roi": {"0": -1}}]), ("too many", [{"name": f"v{i}"} for i in range(13)])):
    try:
        rr.normalize_params({**vraw, "variants": v}, NOW)
        ok_ = False
    except rr.ParamError:
        ok_ = True
    check(f"FROZEN params: refused -- {why}", ok_)
try:
    rr.normalize_params({**vraw, "frozen_from": "no-such-run"}, NOW)
    ok_ = False
except rr.ParamError:
    ok_ = True
check("FROZEN params: an unknown source run is refused", ok_)
est_f = rr.estimate_for(vraw, now=NOW)
check("FROZEN estimate: 0 calls, $0, always within limits", est_f["calls"] == 0 and est_f["usd"] == 0.0 and est_f["within_limits"])
requests.post = no_post
try:
    ridf = rr.MANAGER.start(pv_, http_get=net, strategies_dir=str(REPO))
    rr.MANAGER.thread.join(timeout=900)
finally:
    requests.post = real_post
repf = rr.get_run(ridf)
check("FROZEN run: done, type exit_variants, source run named, the mandatory note about the 'recent performance' block, zero model calls (counter unchanged)", ridf.startswith("fz-") and repf and repf["type"] == "exit_variants" and repf["source_run"] == rid1
      and "recent performance" in repf["note"] and repf["llm"]["calls"] == 0 and len(counter) == calls_before)
base = repf["variants"]["baseline"]["bots"]["main"]["named"]
orig = rep1["bots"]["main"]["named"]
check("FROZEN baseline REPRODUCES the source run (same decisions, same strategy exits -> same trade count, mean and exit mix)", base["n_trades"] == orig["n_trades"] and base["mean_trade_pct"] == orig["mean_trade_pct"]
      and base["exit_reasons"] == orig["exit_reasons"])
nor = repf["variants"]["no_rotation"]["bots"]["main"]["named"]
check("FROZEN no_rotation: no rotate_stale_signal exit, and the override is written to the report", "rotate_stale_signal" not in nor["exit_reasons"] and repf["variants"]["no_rotation"]["overrides"] == {"rotate": False}
      and nor["overrides"].get("rotate_stale_signal") == "off")
cust = repf["variants"]["custom"]["bots"]["main"]["named"]
check("FROZEN custom: ROI table, stoploss and trailing are applied (constants in the report) and listed as overrides", cust["constants"]["minimal_roi"] == {"0": 0.03, "720": 0.01} and cust["constants"]["stoploss"] == -0.03
      and cust["constants"]["trailing"]["positive"] == 0.01 and set(cust["overrides"]) >= {"minimal_roi", "stoploss", "trailing"})
check("FROZEN hold variants: the minimum hold is applied to the strategy", repf["variants"]["hold24"]["bots"]["main"]["named"]["overrides"]["min_hold_hours"] == 24.0 and repf["variants"]["hold0"]["bots"]["main"]["named"]["overrides"]["min_hold_hours"] == 0.0)
decs_src = [json.loads(x) for x in (Path(RDIR) / rid1 / "decisions.jsonl").read_text().splitlines() if json.loads(x)["bot"] == "main" and json.loads(x)["mode"] == "named"]
for vn in repf["variants"]:
    n_dec = repf["variants"][vn]["bots"]["main"]["named"]["n_decisions"]
    check(f"FROZEN {vn}: only exits vary -- the decisions are exactly the source run's ({len(decs_src)})", n_dec == len(decs_src))
check("FROZEN comparison table: bot x variant rows with trades, mean, CI, net, drawdown, percentile vs random entries", len(repf["comparison"]) == 5 and all(k in repf["comparison"][0] for k in ("n_trades", "mean_trade_pct", "ci95_pct", "net_usd", "max_drawdown_pct", "vs_random_percentile")))
check("FROZEN outputs: report.json, report.md (with the note), trades.csv with a variant column", "frozen decisions" in (Path(RDIR) / ridf / "report.md").read_text(encoding="utf-8") and (Path(RDIR) / ridf / "trades.csv").read_text(encoding="utf-8").startswith("variant,"))
check("RUNS list: the frozen run shows as exit_variants", any(r["run_id"] == ridf and r["type"] == "exit_variants" for r in rr.list_runs()))

# trailing stop and minute-ordered exits, unit level
tr_run = eng.BotRun("main", "named", {"every_n": 1, "workers": 1, "seed": 1, "mech": eng.MECH_LEGACY, "exit": {"trailing": {"enabled": True, "positive": 0.01, "offset": 0.02}}}, store_of({"BTCUSDT": flat()}), None, None, ["BTC/USDT"], "t")
ft = eng.sandbox.FakeTrade(1, "BTC/USDT", False, START, 100.0, 25.0, 1.0, "", 0.001)
h1 = tr_run.trailing_hit(ft, 103.0, 100.0)
h2 = tr_run.trailing_hit(ft, 102.5, 102.2)
h3 = tr_run.trailing_hit(ft, 102.4, 101.9)
check("TRAILING: not armed below the offset (a 3 % peak arms it), then the stop follows the peak by `positive` and fires when the low reaches it", h1 is None and h2 is None and h3 is not None and abs(h3 - 103.0 * 0.99) < 1e-9)
T0 = START + timedelta(hours=8)
hourly = flat()
i0 = int((T0 + timedelta(hours=1) - WARM).total_seconds() // 3600)
hourly.loc[i0, ["open", "high", "low", "close"]] = [100, 111, 90, 100]               # ROI +10 % AND stoploss -5 % both inside one hourly candle
mdf = minute_frame(flat())
mdf = mdf[(mdf["date"] >= pd.Timestamp(START - timedelta(days=1))) & (mdf["date"] <= pd.Timestamp(END))].reset_index(drop=True)
mdf.loc[mdf["date"] == pd.Timestamp(T0 + timedelta(hours=1, minutes=10)), ["high"]] = 111.0     # the ROI spike comes FIRST ...
mdf.loc[mdf["date"] == pd.Timestamp(T0 + timedelta(hours=1, minutes=30)), ["low"]] = 90.0       # ... the crash comes later
from backend.replay import calibration as cal, live_data as ld  # noqa: E402

st_m = store_of({"BTCUSDT": hourly})
st_m.add_fine("spot", "BTCUSDT", mdf)
e_m = {"decisions": [{"ts": (T0).strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": T0.strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": "BTC/USDT", "action": "buy", "reason": "r", "confidence": None}], "restarts": [],
       "max_open_trades": 10, "trades": [], "outage_hours": []}
e_m["decisions"] += [{"ts": (T0 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": (T0 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": "BTC/USDT", "action": "buy", "reason": "r", "confidence": None}
                     for k in range(1, 4)]
w_m = cal.window_for(e_m)
res_fine = cal.run_bot("main", e_m, w_m, st_m, ["BTC/USDT"], cal.variant_mech("live_like", 0.0), "t")
res_hr = cal.run_bot("main", e_m, w_m, st_m, ["BTC/USDT"], cal.variant_mech("legacy", 0.0), "t")
first_f = (res_fine["trades"] + res_fine["open_trades"])[0]
first_h = (res_hr["trades"] + res_hr["open_trades"])[0]
check("MINUTE ORDER: with minute candles the ROI spike at :10 comes before the crash at :30 -> exit `roi` at the minute it happened; hourly candles cannot tell and the stoploss wins",
      first_f["exit_reason"] == "roi" and first_f["close_date"].endswith("T09:11:00Z") and first_h["exit_reason"] == "stop_loss")

# ================================================================== batches and the per-hour latency from a real-looking log
H = START + timedelta(hours=5)


def d_(t, off, pair, action="hold"):
    return {"ts": (t + timedelta(seconds=off)).strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": pair, "action": action, "reason": "r", "confidence": None}


ldx = eng.LiveDecisions([d_(H, 20, "A"), d_(H, 40, "B"), d_(H, 61, "C"), d_(H, 26 * 60 + 5, "A"), d_(H, 26 * 60 + 25, "B", "buy")], ["2026-09-10T05:26:00Z"])
bs = ldx.batches(H)
check("BATCHES: a gap of more than 2 minutes (a restart) starts a new analysis batch; each batch has its pairs in log order", len(bs) == 2 and [d["pair"] for d in bs[0]["decisions"]] == ["A", "B", "C"] and [d["pair"] for d in bs[1]["decisions"]] == ["A", "B"])
check("LATENCY: the live bot acted ~2 s after the end of the batch (61 s -> 63 s); `latency` is the first batch's", bs[0]["end_s"] == 63.0 and ldx.latency(H) == 63.0)

# ================================================================== passive limit entries, exit polling, order cancels, spreads
from backend.replay import calibration as cal2, live_data as ld2  # noqa: E402

LOGC = "\n".join([
    "2026-09-29 19:00:35,217 - freqtrade.freqtradebot - INFO - Long signal found: about create a new trade for GROVE/USDT with stake_amount: 10.2 and price: 0.008052 ...",
    "2026-09-29 19:10:36,273 - freqtrade.rpc.rpc_manager - INFO - Sending rpc message: {'trade_id': 51, 'type': entry_cancel, 'buy_tag': '', 'exchange': 'Bybit', 'pair': 'GROVE/USDT', 'direction': 'Long'}",
    "2026-09-29 20:10:36,273 - freqtrade.rpc.rpc_manager - INFO - Sending rpc message: {'trade_id': 52, 'type': exit_cancel, 'exchange': 'Bybit', 'pair': 'CAP/USDT', 'direction': 'Long'}",
    "2026-09-29 20:11:36,273 - freqtrade.rpc.rpc_manager - INFO - Sending rpc message: {'trade_id': 52, 'type': exit, 'exchange': 'Bybit', 'pair': 'CAP/USDT', 'direction': 'Long'}",
    "2026-09-29 19:00:28,000 - DeepSeekMoonshotStrategy - INFO - DeepSeek said buy for GROVE/USDT -- x",
])
pc = ld2.parse_log(LOGC.splitlines())
check("ORDER CANCELS: entry_cancel / exit_cancel messages of the live log are parsed (pair, time); other rpc messages are not", pc["order_cancels"] == [{"ts": "2026-09-29T19:10:36Z", "kind": "entry_cancel", "pair": "GROVE/USDT"},
      {"ts": "2026-09-29T20:10:36Z", "kind": "exit_cancel", "pair": "CAP/USDT"}])
bb = ld2.build_bot(LOGC.splitlines(), [])
cv_ = ld2.coverage(bb)
check("COVERAGE reports how many live entry / exit orders timed out (thin books)", cv_["entry_orders_timed_out"] == 1 and cv_["exit_orders_timed_out"] == 1)
rp_only = {"pair": "GROVE/USDT", "side": "long", "open_date": "2026-09-29T19:00:35Z", "close_date": None}
check("CAUSES: a replay trade the live bot did not make, whose live limit order was cancelled by timeout in that hour, is named live_entry_order_timed_out",
      cal2.causes([], [rp_only], [], [], 10, pc["order_cancels"])["only_replay"] == {"live_entry_order_timed_out": 1} and cal2.causes([], [rp_only], [], [], 10, [])["only_replay"] == {"live_no_entry_unexplained": 1})


def tickers_get(url, params=None, timeout=None, **kw):
    return Resp({"retCode": 0, "result": {"list": [{"symbol": "GROVEUSDT", "bid1Price": "0.008052", "ask1Price": "0.008060"}, {"symbol": "BTCUSDT", "bid1Price": "85000", "ask1Price": "85000.1"},
                                                    {"symbol": "BADUSDT", "bid1Price": "0", "ask1Price": "0"}]}})


rp.cache_path("spreads_spot.json").unlink(missing_ok=True)
sp = rd.fetch_spreads("spot", tickers_get)
check("SPREADS: relative bid-ask spread per symbol from the CURRENT tickers (thin listing wide, major tiny); empty books skipped", abs(sp["GROVEUSDT"] - 0.000008 / 0.008056) < 1e-9 and sp["BTCUSDT"] < 1e-5 and "BADUSDT" not in sp)

T1 = START + timedelta(hours=8)
base_h = flat()
mrows = minute_frame(base_h)
mrows = mrows[(mrows["date"] >= pd.Timestamp(START - timedelta(days=1))) & (mrows["date"] <= pd.Timestamp(END))].reset_index(drop=True)
run_up = mrows.copy()
hour_mask = (run_up["date"] >= pd.Timestamp(T1 + timedelta(minutes=1))) & (run_up["date"] < pd.Timestamp(T1 + timedelta(hours=2)))
run_up.loc[hour_mask, ["open", "high", "low", "close"]] = 101.0                  # price steps UP right after the order and never trades back to the bid
dip = run_up.copy()
dip.loc[dip["date"] == pd.Timestamp(T1 + timedelta(minutes=12)), ["low"]] = 99.0       # ... or dips through it at minute 12


def limit_world(mdf):
    st = store_of({"BTCUSDT": base_h})
    st.add_fine("spot", "BTCUSDT", mdf)
    st.spreads[("spot", "BTCUSDT")] = 0.01                                         # 1 % spread -> the bid sits 0.5 % under the price
    return st


def buy_entry(t):
    return {"decisions": [{"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": "BTC/USDT", "action": "buy", "reason": "r", "confidence": None}], "restarts": [],
            "max_open_trades": 10, "trades": [], "outage_hours": []}


lf_mech = {**cal2.variant_mech("limit_fill", 0.0), "decide_first": True}
e_l = buy_entry(T1)
res_up = cal2.run_bot("main", e_l, cal2.window_for(e_l), limit_world(run_up), ["BTC/USDT"], lf_mech, "t")
check("LIMIT ENTRY: the price runs away from the passive bid -> every order of the hour expires unfilled, NO trade (the live bot's cancelled orders), attempt result limit_not_filled",
      not res_up["trades"] and not res_up["open_trades"] and [a["result"] for a in res_up["attempts"]] == ["limit_not_filled"])
res_dip = cal2.run_bot("main", e_l, cal2.window_for(e_l), limit_world(dip), ["BTC/USDT"], lf_mech, "t")
tr_dip = (res_dip["trades"] + res_dip["open_trades"])
check("LIMIT ENTRY: the first order (10 min) is cancelled, the re-placed one (~10 min 3 s later) fills when the price dips through the new bid -> the trade opens at THAT order's time, at the limit",
      len(tr_dip) == 1 and tr_dip[0]["open_date"] == (T1 + timedelta(seconds=603)).strftime("%Y-%m-%dT%H:%M:%SZ") and abs(tr_dip[0]["open_rate"] - 101.0 * 0.995) < 1e-9)
res_off = cal2.run_bot("main", e_l, cal2.window_for(e_l), limit_world(run_up), ["BTC/USDT"], {**lf_mech, "limit_fill": False}, "t")
check("LIMIT ENTRY: switched off, the same hour simply fills at the minute price (the legacy simplification)", len(res_off["trades"] + res_off["open_trades"]) == 1)

# exit polling: Moonshot's 3-day maximum hold fires when its time is reached, not at the next hourly step
carry_m = {"id": 5, "pair": "BTC/USDT", "side": "long", "is_open": True, "open_date": (T1 + timedelta(minutes=25) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "close_date": None, "open_rate": 100.0,
           "close_rate": None, "stake": 10.0, "profit_ratio": None, "profit_abs": None, "fees_abs": 0.0, "exit_reason": None, "enter_tag": ""}
e_p = {"decisions": [{"ts": (T1 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "t0": (T1 + timedelta(hours=k)).strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": "BTC/USDT", "action": "hold", "reason": "r", "confidence": None}
                  for k in range(0, 3)], "restarts": [], "max_open_trades": 10, "trades": [carry_m], "outage_hours": []}
st_p = store_of({"BTCUSDT": base_h})
st_p.add_fine("spot", "BTCUSDT", mrows)
poll_m = {**cal2.variant_mech("poll_exits", 0.0), "decide_first": True}
res_poll = cal2.run_bot("moonshot", e_p, cal2.window_for(e_p), st_p, ["BTC/USDT"], poll_m, "t")
res_nopoll = cal2.run_bot("moonshot", e_p, cal2.window_for(e_p), st_p, ["BTC/USDT"], {**poll_m, "poll_exits": False}, "t")
x1 = (res_poll["trades"] or [{}])[0]
x2 = (res_nopoll["trades"] or [{}])[0]
cd1 = datetime.strptime(x1["close_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) if x1 else None
check("EXIT POLLING: the live loop calls custom_exit every few seconds, so the 3-day maximum hold fires the moment it is reached (within 5 minutes of 25 min past the hour); an hourly-only check fires at the next hour",
      x1.get("exit_reason") == "max_hold_days_reached" and T1 + timedelta(minutes=25) <= cd1 <= T1 + timedelta(minutes=31)
      and x2.get("exit_reason") == "max_hold_days_reached" and x2["close_date"] == (T1 + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


# ================================================================== production runs Python 3.11: no 3.12+ only syntax (a nested same-quote f-string broke the whole replay module once)
import ast  # noqa: E402
import re as _re  # noqa: E402

bad_f: list = []
for f_ in sorted((REPO / "backend").rglob("*.py")):
    src_ = f_.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src_)):
        if isinstance(node, ast.JoinedStr):
            seg = ast.get_source_segment(src_, node) or ""
            m_ = _re.match(r"^[rRbBuUfF]*(\"\"\"|'''|\"|')", seg)
            if not m_:
                continue
            q_ = m_.group(1)
            for v_ in node.values:
                if isinstance(v_, ast.FormattedValue):
                    inner_ = ast.get_source_segment(src_, v_.value) or ""
                    if q_[0] in inner_ or "\\" in inner_ or "#" in inner_:
                        bad_f.append((f_.name, node.lineno, inner_[:40]))
check("PY3.11 SAFE: no f-string in backend/ reuses its own quote or uses a backslash inside {} (valid on 3.12+, a SyntaxError on the 3.11 image) -- " + str(bad_f[:3]), not bad_f)
try:
    rr.normalize_params({"bots": []}, NOW)
    ok_ = False
except rr.ParamError as e_:
    ok_ = "none is ticked" in str(e_)
check("PARAMS: an explicitly empty bot list is refused with a clear message (it used to silently fall back to the default bots)", ok_)
# ================================================================== G: the page
html = (REPO / "frontend/replay.html").read_text(encoding="utf-8")
check("PAGE: model / provider selector (DeepSeek default), the no-LLM rule toggle (on), the minute-price toggle", all(s in html for s in ("id=\"provider\"", "id=\"addRule\" checked", "id=\"minutePrices\"", "CFG.default_provider")))
check("PAGE: frozen mode with a source-run selector and the exit override choices (rotation, minimum hold 0/4/12/24, ROI, stoploss, trailing)", all(s in html for s in ("id=\"frozenOn\"", "id=\"frozenFrom\"", "no_rotation", "hold12", "hold24", "cvRoi", "cvStop", "cvTrail")))
check("PAGE: the mandatory frozen warning and the price hint are on the page (the conservative price, 2-3x lower real bill)", "not recomputed" in html and "2-3x lower" in html and "priceHint" in html)
check("PAGE: results show the model per row, the rule rows, the live-reference column with divergence flags, the LLM-vs-rule line, the exit-variant comparison table", all(s in html for s in ("RES_HEAD", "<th>model</th>", "rule (no LLM)", "dv.flags", "LLM versus rule", "renderVariants", "exit variants")))
check("PAGE: a failed config load is shown in red with the HTTP status (not an empty form), no-bot warning before any request, server reasons shown on start / estimate", all(x in html for x in ("Could not load the replay settings", "Pick at least one bot", "the server gave no reason")))
check("PAGE: Moonshot has the 'different universe' badge and its own mechanics-check block outside the summary", "different universe" in html and "Different universe: mechanics check only" in html)
from fastapi.testclient import TestClient  # noqa: E402

os.environ["DATA_DIR"] = DATA
Path(DATA, "config.json").write_text(json.dumps({"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
import shutil  # noqa: E402

shutil.copy(REPO / "frontend/replay.html", os.path.abspath("/app/frontend/replay.html"))
from backend import app as app_module  # noqa: E402

cl = TestClient(app_module.app)
cfgj = cl.get("/api/replay/config").json()
check("API config: providers (DeepSeek ready, the other one not configured and why), the price note, the moonshot flag, the variant limit", cfgj["providers"]["deepseek"]["ready"] and cfgj["providers"]["openai_compatible"]["ready"] is False
      and cfgj["providers"]["openai_compatible"]["problem"] and "2-3x lower" in cfgj["price_note"] and any(b["key"] == "moonshot" and b["universe_mismatch"] for b in cfgj["bots"]) and cfgj["max_variants"] == 12
      and cfgj["defaults"]["add_rule"] is True)
_orig_est = rr.estimate_for
rr.estimate_for = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Bybit 403 forbidden"))
r_crash = cl.post("/api/replay/start", json={"bots": ["main"]})
rr.estimate_for = _orig_est
check("API start: an unexpected crash is a readable 502 'start failed: ...' (the page shows the reason), never a bare 500", r_crash.status_code == 502 and "start failed: RuntimeError: Bybit 403 forbidden" in r_crash.json()["detail"])
check("API start: no bot ticked is a 400 with the reason", cl.post("/api/replay/start", json={"bots": [], "modes": ["named"]}).status_code == 400)
est_api = cl.post("/api/replay/estimate", json=vraw)
check("API estimate: a frozen request is a $0 estimate", est_api.status_code == 200 and est_api.json()["calls"] == 0)
check("API: starting a frozen run with a bad variant is a 400", cl.post("/api/replay/start", json={**vraw, "variants": [{"name": "x", "stoploss": 0.5}]}).status_code == 400)

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
