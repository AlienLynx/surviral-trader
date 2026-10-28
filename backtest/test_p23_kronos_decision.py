"""
P23 -- Kronos as a decision source for Main / Shorts / Moonshot, and the LLM provider switch (deepseek | free | none).
The real strategy files are loaded with stubbed freqtrade (same technique as the other P-series tests); the Kronos rank file, the model answers and OpenRouter are faked.
Nothing here needs Docker, a bot, the network or a key.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
failures: list[str] = []
UTC = timezone.utc


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


# ------------------------------------------------------------------ stubs + loading
def install_stubs():
    ft, pers, strat = types.ModuleType("freqtrade"), types.ModuleType("freqtrade.persistence"), types.ModuleType("freqtrade.strategy")

    class _Trade:
        @classmethod
        def get_trades_proxy(cls, is_open=True):
            return []

        @classmethod
        def total_open_trades_stakes(cls):
            return 0.0

    class _IStrategy:
        def __init__(self, config=None):
            self.config = config or {}
    pers.Trade, strat.IStrategy = _Trade, _IStrategy
    sys.modules.update({"freqtrade": ft, "freqtrade.persistence": pers, "freqtrade.strategy": strat})


def load(rel: str, name: str):
    path = REPO / rel
    d = str(path.parent)
    sys.path.insert(0, d)
    for m in ("_allocator_ctx", "_second_opinion"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.remove(d)
    return mod


install_stubs()
ALLOC = tempfile.mkdtemp()
os.environ["ALLOCATOR_DIR"] = ALLOC
CTX = load("freqtrade/user_data/strategies/_allocator_ctx.py", "ctx_main")


def write_rank(bots: dict, t0: datetime | None = None):
    t0 = t0 or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    Path(ALLOC, "kronos_rank.json").write_text(json.dumps({"t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"), "bots": bots}))
    CTX._cache.clear()
    for m in globals().get("MODS", {}).values():          # every strategy module has its own copy of the helper and its own 60 s cache
        m._allocator_ctx._cache.clear()


def entry(action, score=0.02, rank=1, n=12):
    return {"action": action, "score": score, "rank": rank, "n": n, "reason": f"24h relative strength {score:+.2%}, rank {rank} of {n}"}


def env(**kw):
    for k in ("MAIN_DECISION_SOURCE", "MAIN_LLM_PROVIDER", "SHORTS_DECISION_SOURCE", "SHORTS_LLM_PROVIDER", "MOONSHOT_DECISION_SOURCE", "MOONSHOT_LLM_PROVIDER"):
        os.environ.pop(k, None)
    os.environ.update(kw)


# ------------------------------------------------------------------ the helper
env()
check("DEFAULTS: no variable set -> source llm, provider deepseek (the behaviour before P23)", CTX.decision_config("main") == {"source": "llm", "provider": "deepseek"})
env(MAIN_DECISION_SOURCE="Kronos_LLM", MAIN_LLM_PROVIDER=" FREE ")
check("CONFIG: values are case-insensitive and trimmed", CTX.decision_config("main") == {"source": "kronos_llm", "provider": "free"})
env(MAIN_DECISION_SOURCE="kronos-ish", MAIN_LLM_PROVIDER="gpt")
check("CONFIG: an invalid value falls back to the safe default (never an unknown mode)", CTX.decision_config("main") == {"source": "llm", "provider": "deepseek"})
env(SHORTS_LLM_PROVIDER="none")
check("CONFIG: per bot (SHORTS_ does not leak into MAIN_)", CTX.decision_config("shorts")["provider"] == "none" and CTX.decision_config("main")["provider"] == "deepseek")
check("PROPOSAL: no file -> None", CTX.kronos_proposal("BTC/USDT", "main") is None)
write_rank({"main": {"BTC/USDT": entry("buy"), "ETH/USDT": entry("hold", 0.0, 6)}})
p = CTX.kronos_proposal("BTC/USDT", "main")
check("PROPOSAL: a fresh file gives the action, score, rank and a readable reason", p["action"] == "buy" and p["rank"] == 1 and "relative strength" in p["reason"])
check("PROPOSAL: a pair the file does not list, or another bot's pair, is None (fail-safe: hold)", CTX.kronos_proposal("XRP/USDT", "main") is None and CTX.kronos_proposal("BTC/USDT", "shorts") is None)
write_rank({"main": {"BTC/USDT": entry("buy")}}, t0=datetime.now(UTC) - timedelta(hours=3))
check("PROPOSAL: a file older than ~2h is no signal (the Kronos cycle stopped: never trade on old forecasts)", CTX.kronos_proposal("BTC/USDT", "main") is None)
write_rank({"main": {"BTC/USDT": {"action": "yolo"}}})
check("PROPOSAL: a malformed entry is no signal", CTX.kronos_proposal("BTC/USDT", "main") is None)
Path(ALLOC, "kronos_rank.json").write_text("{not json")
CTX._cache.clear()
check("PROPOSAL: a corrupt file is no signal, no exception", CTX.kronos_proposal("BTC/USDT", "main") is None)

# ------------------------------------------------------------------ the three real strategies
SPECS = {"main": ("freqtrade/user_data/strategies/DeepSeekStrategy.py", "DeepSeekStrategy", ("buy",)),
         "shorts": ("freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py", "DeepSeekShortsStrategy", ("buy", "sell_short")),
         "moonshot": ("freqtrade-moonshot/user_data/strategies/DeepSeekMoonshotStrategy.py", "DeepSeekMoonshotStrategy", ("buy",))}
MODS = {b: load(rel, f"p23_{b}") for b, (rel, _, _) in SPECS.items()}


def make(bot):
    mod = MODS[bot]
    st = getattr(mod, SPECS[bot][1])({})
    calls = {"ds": 0, "or": 0, "free": 0, "prompts": []}
    st.answer = "hold: x"

    def ds(system, user):
        calls["ds"] += 1
        calls["prompts"].append(user)
        return st.answer

    def orr(system, user):
        calls["or"] += 1
        return None
    st._call_deepseek, st._call_openrouter_fallback = ds, orr
    st._call_openrouter_free_only = lambda system, user: (calls.__setitem__("free", calls["free"] + 1) or calls["prompts"].append(user) or st.answer)
    # the data sources of the prompt: no network
    mod.get_market_context = lambda: "ctx"
    mod.get_news_headlines = lambda: "headlines"
    mod.get_fear_greed_index = lambda: None
    mod.get_survival_context = lambda: "surv"
    st._mtf_confirmation = lambda pair: None
    st._concentration_risk_warning = lambda pair: None
    st._recent_performance_summary = lambda is_core: "perf"
    if hasattr(st, "_get_funding_rate_line"):
        st._get_funding_rate_line = lambda pair: None            # no network in a test
    if hasattr(mod, "_second_opinion"):
        mod._second_opinion.run_shadow = lambda *a, **k: None
    return st, calls


ROW = pd.Series({"pct_change_24": 1.0, "volatility_24": 2.0, "close": 100.0})


def ask(st, pair, bot):
    if bot == "moonshot":
        return st._ask_deepseek(ROW, pair, 30.0, 700)
    return st._ask_deepseek(ROW, pair, True)


for bot, (rel, cls, dirs) in SPECS.items():
    pair = "BTC/USDT:USDT" if bot == "shorts" else "BTC/USDT"
    src = (REPO / rel).read_text(encoding="utf-8")
    check(f"{bot}: the file parses, keeps `_ask_deepseek` as the entry point and the old body as `_ask_llm` (the replay hooks the name)", ast.parse(src) and "def _ask_deepseek(self, row, pair, *args, **kwargs)" in src and "def _ask_llm(" in src)
    check(f"{bot}: it can open only {dirs}", getattr(MODS[bot], cls).KRONOS_DIRECTIONS == dirs)

    # --- unchanged default
    env()
    write_rank({bot: {pair: entry("buy")}})
    st, c = make(bot)
    st.answer = "buy: go"
    d, r = ask(st, pair, bot)
    check(f"{bot} default (llm + deepseek): the LLM decides, DeepSeek is called, Kronos is not consulted -- exactly as before", d == "buy" and c["ds"] == 1 and "Kronos" not in c["prompts"][0] and c["free"] == 0)

    # --- provider none, source llm = idle
    env(**{f"{bot.upper()}_LLM_PROVIDER": "none"})
    st, c = make(bot)
    d, r = ask(st, pair, bot)
    check(f"{bot}: provider none with source llm -> hold, no LLM call at all (the bot is intentionally idle)", d == "hold" and c["ds"] == c["or"] == c["free"] == 0 and "idle" in r)

    # --- provider free
    env(**{f"{bot.upper()}_LLM_PROVIDER": "free"})
    st, c = make(bot)
    st.answer = "buy: ok"
    d, r = ask(st, pair, bot)
    check(f"{bot}: provider free -> only the free OpenRouter path is used (DeepSeek and the paid fallback are never called)", d == "buy" and c["free"] == 1 and c["ds"] == 0 and c["or"] == 0)

    # --- kronos alone
    env(**{f"{bot.upper()}_DECISION_SOURCE": "kronos"})
    st, c = make(bot)
    d, r = ask(st, pair, bot)
    check(f"{bot}: source kronos -> the Kronos buy is the decision and no LLM is called", d == "buy" and c["ds"] == c["free"] == c["or"] == 0 and r.startswith("kronos"))
    write_rank({bot: {pair: entry("hold", 0.0, 6)}})
    check(f"{bot}: source kronos, Kronos says hold -> hold", ask(st, pair, bot)[0] == "hold")
    write_rank({bot: {pair: entry("sell_short", -0.03, 12)}})
    d, r = ask(st, pair, bot)
    check(f"{bot}: source kronos, Kronos says sell_short -> " + ("sell_short" if "sell_short" in dirs else "hold (this bot does not trade shorts)"), d == ("sell_short" if "sell_short" in dirs else "hold"))
    write_rank({bot: {}})
    d, r = ask(st, pair, bot)
    check(f"{bot}: source kronos, no signal for the pair -> hold (fail-safe, never falls back to the LLM)", d == "hold" and "no fresh signal" in r and c["ds"] == 0)

    # --- kronos proposes, LLM may only confirm or veto
    env(**{f"{bot.upper()}_DECISION_SOURCE": "kronos_llm"})
    write_rank({bot: {pair: entry("buy")}})
    st, c = make(bot)
    st.answer = "buy: news fine"
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm, LLM confirms -> buy; the prompt carries the Kronos proposal and the confirm-or-veto rule", d == "buy" and c["ds"] == 1 and "Quantitative proposal" in c["prompts"][0] and "CONFIRM it" in c["prompts"][0] and "VETO it" in c["prompts"][0])
    st.answer = "hold: bad news"
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm, LLM vetoes -> hold ('llm veto of kronos buy' in the reason)", d == "hold" and "llm veto of kronos buy" in r)
    st.answer = "sell_short: reverse"
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm, the LLM answers the OTHER direction -> hold (code enforces confirm-or-veto: the LLM can never create or reverse a trade)", d == "hold")
    write_rank({bot: {pair: entry("hold", 0.0, 6)}})
    st, c = make(bot)
    st.answer = "buy: go"
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm, Kronos says hold -> hold WITHOUT calling the LLM (the LLM cannot create a candidate)", d == "hold" and c["ds"] == 0)
    write_rank({bot: {}})
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm, no Kronos signal -> hold, LLM not called", d == "hold" and c["ds"] == 0)
    write_rank({bot: {pair: entry("buy")}})
    env(**{f"{bot.upper()}_DECISION_SOURCE": "kronos_llm", f"{bot.upper()}_LLM_PROVIDER": "none"})
    st, c = make(bot)
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm with provider none -> behaves as kronos alone (no LLM), the Kronos direction is the decision", d == "buy" and c["ds"] == c["free"] == 0)
    env(**{f"{bot.upper()}_DECISION_SOURCE": "kronos_llm", f"{bot.upper()}_LLM_PROVIDER": "free"})
    st, c = make(bot)
    st.answer = "buy: ok"
    d, r = ask(st, pair, bot)
    check(f"{bot}: kronos_llm + free provider -> confirm through the FREE path only", d == "buy" and c["free"] == 1 and c["ds"] == 0 and "Quantitative proposal" in c["prompts"][0])
    check(f"{bot}: the Kronos note does not leak into the next decision (note cleared after each call)", st._kronos_note is None)

    # --- the free-only caller never uses a paid model
    env()
    st, _ = make(bot)
    st2 = getattr(MODS[bot], cls)({})
    posted = {}

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"model": "x:free", "choices": [{"message": {"content": "buy: ok"}}]}
    MODS[bot].requests = types.SimpleNamespace(post=lambda url, **k: posted.update({"url": url, **k}) or R(), get=lambda *a, **k: None, exceptions=types.SimpleNamespace(HTTPError=Exception))
    st2._get_openrouter_free_models = lambda: ["a/one:free", "b/two:free", "c/three:free", "d/four:free"]
    os.environ["OPENROUTER_API_KEY"] = "k-test"
    out = st2._call_openrouter_free_only("s", "u")
    check(f"{bot}: the free-only call sends at most 3 models, ALL of them free, no paid last resort, to OpenRouter only", out == "buy: ok" and posted["json"]["models"] == ["a/one:free", "b/two:free", "c/three:free"]
          and "openrouter.ai" in posted["url"] and all(m.endswith(":free") for m in posted["json"]["models"]) and "deepseek" not in posted["url"])
    st2._get_openrouter_free_models = lambda: []
    check(f"{bot}: no free model available -> None (hold), nothing is sent", st2._call_openrouter_free_only("s", "u") is None)
    os.environ.pop("OPENROUTER_API_KEY", None)
    st2._get_openrouter_free_models = lambda: ["a/one:free"]
    check(f"{bot}: no OpenRouter key -> None (hold)", st2._call_openrouter_free_only("s", "u") is None)

# ------------------------------------------------------------------ copies of the helper stay identical
import hashlib  # noqa: E402

hs = {d: hashlib.sha256((REPO / d / "user_data/strategies/_allocator_ctx.py").read_bytes()).hexdigest() for d in ("freqtrade", "freqtrade-shorts", "freqtrade-moonshot", "freqtrade-listing-fade", "freqtrade-trend-v2", "freqtrade-shorts-v2")}
check("COPIES: _allocator_ctx.py is byte-identical in every bot directory that has it", len(set(hs.values())) == 1)
for d in ("freqtrade-control-long", "freqtrade-control-short", "freqtrade-trend-ensemble"):
    check(f"CONTROL: {d} has no Kronos / decision code at all", not any("kronos" in f.read_text(encoding="utf-8", errors="replace").lower() for f in (REPO / d).rglob("*.py")))
for bot, (rel, _, _) in {k: v for k, v in SPECS.items()}.items():
    s = (REPO / rel).read_text(encoding="utf-8")
    tree = ast.parse(s)
    touched = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in {"custom_exit", "custom_stoploss", "populate_exit_trend", "confirm_trade_exit", "leverage"} and "kronos" in (ast.get_source_segment(s, n) or "").lower()]
    check(f"{bot}: exits, stoploss and leverage never reference Kronos (only the entry decision does)", touched == [])

# ------------------------------------------------------------------ kronos-signal: the cross-sectional rank
spec = importlib.util.spec_from_file_location("kcore", REPO / "kronos-signal/app/core.py")
kc = importlib.util.module_from_spec(spec)
sys.modules["kcore"] = kc
spec.loader.exec_module(kc)


def rec(pair, r24, r4=0.0, r1=0.0):
    return {"pair": pair, "kronos": {"ret_1h": r1, "ret_4h": r4, "ret_24h": r24}}


pairs = [f"C{i:02d}/USDT" for i in range(12)]
rets = [0.06, 0.04, 0.03, 0.01, 0.005, 0.0, 0.0, -0.002, -0.01, -0.03, -0.04, -0.06]
by_coin = {kc.coin_of(p): rec(p, r) for p, r in zip(pairs, rets)}
v = kc.rank_bot_view(pairs, by_coin, 8, 0.003)
acts = {p: v[p]["action"] for p in pairs}
check("RANK: of 12 pairs the top third (4) are buy and the bottom third (4) are sell_short, the middle hold", [p for p in pairs if acts[p] == "buy"] == pairs[:4] and [p for p in pairs if acts[p] == "sell_short"] == pairs[-4:] and sum(1 for a in acts.values() if a == "hold") == 4)
mean = sum(rets) / 12
check("RANK: the score is the forecast minus the mean forecast of the bot's own pairs (market-neutral), rank 1 = strongest", abs(v[pairs[0]]["score"] - (0.06 - mean)) < 1e-6 and v[pairs[0]]["rank"] == 1 and v[pairs[-1]]["rank"] == 12 and v[pairs[0]]["n"] == 12)
shift = {kc.coin_of(p): rec(p, r + 0.05) for p, r in zip(pairs, rets)}
check("RANK: adding the same number to every forecast (a market-wide move) changes nothing", {p: x["action"] for p, x in kc.rank_bot_view(pairs, shift, 8, 0.003).items()} == acts)
flat = {kc.coin_of(p): rec(p, 0.0001 * i) for i, p in enumerate(pairs)}
check("RANK: tiny differences (< 0.3% from the mean) are never a trade, however the pairs rank", all(x["action"] == "hold" for x in kc.rank_bot_view(pairs, flat, 8, 0.003).values()))
few = kc.rank_bot_view(pairs[:5], by_coin, 8, 0.003)
check("RANK: fewer than 8 scored pairs -> everything holds, with the reason", all(x["action"] == "hold" and "only 5 scored pairs" in x["reason"] for x in few.values()))
check("RANK: a pair the model did not score is absent (the bot holds it); the ranking only uses the bot's own whitelist", set(kc.rank_bot_view(pairs + ["ZZZ/USDT"], by_coin, 8, 0.003)) == set(pairs)
      and len(kc.rank_bot_view(pairs[:9], by_coin, 8, 0.003)) == 9 and kc.rank_bot_view(pairs[:9], by_coin, 8, 0.003)[pairs[0]]["n"] == 9)
fut = {kc.coin_of(p): rec(p, r) for p, r in zip([x + ":USDT" for x in pairs], rets)}
check("RANK: a futures bot's pair (COIN/USDT:USDT) gets the forecast made for the coin (spot or futures series)", kc.rank_bot_view([p + ":USDT" for p in pairs], by_coin, 8, 0.003)[pairs[0] + ":USDT"]["action"] == "buy")
check("RANK: deterministic", kc.rank_bot_view(pairs, by_coin, 8, 0.003) == kc.rank_bot_view(pairs, by_coin, 8, 0.003))
T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
cfg = {"rank_min_n": 8, "rank_min_score": 0.003}
rf = kc.build_rank_file(T0, T0 + timedelta(minutes=9), {"main": pairs, "shorts": [p + ":USDT" for p in pairs[:6]]}, list(by_coin.values()), cfg)
check("RANK FILE: t0, design text, per-bot views; the shorts bot with only 6 pairs holds everything", rf["t0"] == "2026-10-07T12:00:00Z" and "relative strength" in rf["design"] and rf["bots"]["main"][pairs[0]]["action"] == "buy"
      and all(x["action"] == "hold" for x in rf["bots"]["shorts"].values()))

# the whole cycle writes the file
cdir = Path(tempfile.mkdtemp())


class Rest:
    def __call__(self, bot, path):
        if path == "/api/v1/whitelist":
            return {"whitelist": pairs if bot == "main" else []}
        from urllib.parse import parse_qs, unquote, urlparse
        pair = unquote(parse_qs(urlparse(path).query)["pair"][0])
        base = datetime(2026, 10, 7, 12, 0, tzinfo=UTC) - timedelta(hours=600)
        i0 = pairs.index(pair)
        cols = ["date", "open", "high", "low", "close", "volume"]
        return {"columns": cols, "data": [[int((base + timedelta(hours=h)).timestamp() * 1000), 1.0 + i0, 1.0 + i0, 1.0 + i0, 1.0 + i0, 100.0] for h in range(600)]}


def fake_predict(window, t0):
    i = int(round(window[-1]["close"] - 1.0))
    last = window[-1]["close"]
    return [last * (1 + rets[i] * (k + 1) / 24) for k in range(24)]


ccfg = {**kc.config_from_env({"KRONOS_OUT_DIR": str(cdir)}), "skip_bots": []}
summ = kc.run_cycle(T0 + timedelta(minutes=2), Rest(), fake_predict, ccfg, cdir / "kronos_signals.jsonl", clock=lambda: 0)
rank = json.loads((cdir / "kronos_rank.json").read_text())
check("CYCLE: one cycle scores the pairs, appends the ledger records (unchanged format) AND writes kronos_rank.json for the bots", summ["pairs_scored"] == 12 and (cdir / "kronos_signals.jsonl").exists()
      and rank["bots"]["main"][pairs[0]]["action"] == "buy" and rank["bots"]["main"][pairs[-1]]["action"] == "sell_short" and summ["rank_actions"]["main"] == {"buy": 4, "sell_short": 4, "hold": 4})
check("CYCLE: the ledger record of the `kronos` source keeps its own absolute-threshold action (the measured source is unchanged by P23)", json.loads((cdir / "kronos_signals.jsonl").read_text().splitlines()[0])["bot"] == "kronos")
check("CONFIG: defaults -- 45 pairs, 900 s budget, min 8 pairs, min score 0.3%", kc.config_from_env({})["max_pairs"] == 45 and kc.config_from_env({})["cycle_budget_s"] == 900 and kc.config_from_env({})["rank_min_n"] == 8
      and kc.config_from_env({})["rank_min_score"] == 0.003)

# ------------------------------------------------------------------ survival-agent: the publisher
from backend.engine import kronos_feed as kf  # noqa: E402

src_dir, dst_dir = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
NOW = datetime(2026, 10, 7, 12, 30, tzinfo=UTC)
check("FEED: no source file yet -> nothing published, a readable reason", kf.publish(src_dir, dst_dir, NOW)["published"] is False and not (dst_dir / "kronos_rank.json").exists())
(src_dir / "kronos_rank.json").write_text(json.dumps(rank))
r1 = kf.publish(src_dir, dst_dir, NOW)
pub = json.loads((dst_dir / "kronos_rank.json").read_text())
check("FEED: a valid recent rank file is copied to the allocator volume with a published_at stamp", r1["published"] and pub["t0"] == rank["t0"] and pub["published_at"] and pub["bots"] == rank["bots"] and r1["bots"]["main"]["buy"] == 4)
check("FEED: publishing the same file again is a no-op", kf.publish(src_dir, dst_dir, NOW).get("unchanged") is True)
(src_dir / "kronos_rank.json").write_text(json.dumps({**rank, "t0": "2026-10-07T00:00:00Z", "written_at": "x"}))
r_old = kf.publish(src_dir, dst_dir, NOW)
check("FEED: a source older than 6 h is refused and the previously published file is left untouched (the bots will treat it as stale by themselves)", r_old["published"] is False and "older than" in r_old["reason"]
      and json.loads((dst_dir / "kronos_rank.json").read_text())["t0"] == rank["t0"])
(src_dir / "kronos_rank.json").write_text(json.dumps({**rank, "bots": {"main": {pairs[0]: {"action": "all-in"}}}, "written_at": "y"}))
check("FEED: a malformed file is refused", kf.publish(src_dir, dst_dir, NOW)["published"] is False)
(src_dir / "kronos_rank.json").write_text("{broken")
check("FEED: a corrupt file is refused without an exception", kf.publish(src_dir, dst_dir, NOW)["published"] is False)
st = kf.status(dst_dir, NOW)
check("FEED status: what the bots see -- age, freshness for the bots, counts of buy / sell_short / hold per bot", st["available"] and st["fresh_for_bots"] is True and st["bots"]["main"] == {"buy": 4, "sell_short": 4, "hold": 4} and st["age_seconds"] == 1800)
check("FEED status: nothing published -> says so", kf.status(Path(tempfile.mkdtemp()), NOW)["available"] is False)
check("FEED: the module never imports anything that talks to a bot or the network", not any(x in (REPO / "backend/engine/kronos_feed.py").read_text(encoding="utf-8") for x in ("requests", "urllib", "freqtrade_client", "http")))

# ------------------------------------------------------------------ wiring
import yaml  # noqa: E402

cy = yaml.safe_load((REPO / "docker-compose.yml").read_text(encoding="utf-8"))
for svc, pre in (("freqtrade", "MAIN"), ("freqtrade-shorts", "SHORTS"), ("freqtrade-moonshot", "MOONSHOT")):
    e = cy["services"][svc]["environment"]
    check(f"COMPOSE: {svc} passes {pre}_DECISION_SOURCE (default kronos_llm) and {pre}_LLM_PROVIDER (default deepseek) as ${{VAR:-default}}; it reads the rank file from the read-only allocator volume",
          f"{pre}_DECISION_SOURCE=${{{pre}_DECISION_SOURCE:-kronos_llm}}" in e and f"{pre}_LLM_PROVIDER=${{{pre}_LLM_PROVIDER:-deepseek}}" in e and "allocator-shared:/allocator:ro" in cy["services"][svc]["volumes"])
ke = cy["services"]["kronos-signal"]["environment"]
check("COMPOSE: kronos-signal covers the bots' whole whitelists (45 pairs, 900 s budget) and passes the rank settings through", "KRONOS_MAX_PAIRS=${KRONOS_MAX_PAIRS:-45}" in ke and "KRONOS_CYCLE_BUDGET_S=${KRONOS_CYCLE_BUDGET_S:-900}" in ke
      and "KRONOS_RANK_MIN_N=${KRONOS_RANK_MIN_N:-8}" in ke)
check("COMPOSE: no bot container mounts the kronos volume (they only see the published copy on the allocator volume)", not any("kronos" in v for n, s in cy["services"].items() if n not in ("kronos-signal", "survival-agent", "kronos-replay") for v in s.get("volumes", [])))
check("COMPOSE: survival-agent reads /kronos read-only and writes /allocator (where it publishes)", "kronos-data:/kronos:ro" in cy["services"]["survival-agent"]["volumes"] and "allocator-shared:/allocator" in cy["services"]["survival-agent"]["volumes"])
app_src = (REPO / "backend/app.py").read_text(encoding="utf-8")
check("APP: the feed loop is started with the other background tasks and /api/kronos/rank is a GET", "asyncio.create_task(_kronos_feed_loop())" in app_src and '@app.get("/api/kronos/rank")' in app_src)
check("REPLAY: the sandbox stub always answers `llm / deepseek` and no Kronos proposal, so the replay is unchanged", "ac.decision_config = lambda bot_id" in (REPO / "backend/replay/sandbox.py").read_text(encoding="utf-8"))

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL P23 CHECKS PASSED")
