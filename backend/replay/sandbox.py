"""
Run a bot's REAL strategy file (prompt building, response parsing, entry logic, rotation exits, entry veto, stake sizing) against replayed history without editing
that file. The trick is the one backtest/llm_replay.py uses -- stub the `freqtrade` package, exec the file as a module -- extended by swapping the module's
GLOBALS after it has loaded:

  requests          -> ReplayHTTP     serves Bybit klines AS OF the replay clock from the local store, refuses every other URL (and records the attempt)
  datetime / time   -> sim clock      so cooldowns, "hours since" and cache TTLs see replay time, not wall time
  Trade             -> ReplayTrade    a per-bot registry of simulated trades behind Trade.get_trades_proxy()
  get_market_context / get_news_headlines   -> a fixed, honest "not available for a historical replay" text (neither NO_NEWS nor FEED_DOWN)
  get_fear_greed_index                      -> the alternative.me value as of the replay clock
  _second_opinion / _allocator_ctx          -> inert stubs (no shadow votes, no context block, stake multiplier 1.0, no announcement guard)
  _call_deepseek / _call_openrouter_fallback (on the instance) -> the replay LLM client

If a hook cannot be replaced without editing the live file the loader raises HookError naming it; nothing is patched silently.
"""
from __future__ import annotations

import importlib.util
import re
import sys
import threading
import time as _time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests as _real_requests

from . import data as rdata

HISTORICAL_CONTEXT_NOTE = "unavailable for a historical replay -- decide from price action, risk tier and recent simulated performance only"
HISTORICAL_NEWS_NOTE = "news headlines are not available for a historical replay (no archive) -- this is neither 'no news' nor a failed feed; do not infer anything from it"
_LOAD_LOCK = threading.Lock()


class ReplayBlocked(Exception):
    """A strategy tried to reach something replay does not serve (a live URL)."""


class HookError(Exception):
    pass


# --------------------------------------------------------------------- clock
class SimClock:
    def __init__(self, now: datetime | None = None):
        self.now = now or datetime(2000, 1, 1, tzinfo=timezone.utc)


def make_datetime(clock: SimClock):
    class SimDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            n = clock.now
            return n.astimezone(tz) if tz else n.replace(tzinfo=None)

        @classmethod
        def utcnow(cls):
            return clock.now.replace(tzinfo=None)
    return SimDatetime


class SimTime(types.SimpleNamespace):
    def __init__(self, clock: SimClock):
        super().__init__(_clock=clock)

    def time(self):
        return self._clock.now.timestamp()

    def monotonic(self):
        return self._clock.now.timestamp()

    def sleep(self, *_a, **_k):
        return None


# ---------------------------------------------------------------------- http
class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.text = str(payload)
        self.content = b""

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _real_requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


class ReplayHTTP:
    """Stands in for the `requests` module inside a strategy. Only Bybit klines and (when the store has them) the perpetual funding rate are answered, both AS OF the simulated clock; without funding data the answer is empty."""
    exceptions = _real_requests.exceptions

    def __init__(self, ctx: "ReplayContext"):
        self.ctx = ctx

    def get(self, url, params=None, timeout=None, **kw):
        p = params or {}
        if "api.bybit.com/v5/market/kline" in url:
            cat, sym = p.get("category", "spot"), p.get("symbol", "")
            if not self.ctx.store.has(cat, sym):
                return _Resp({"retCode": 0, "result": {"list": []}})
            rows = self.ctx.store.klines_asof(cat, sym, str(p.get("interval", "60")), int(p.get("limit", 200)), self.ctx.clock.now)
            return _Resp({"retCode": 0, "result": {"list": rows}})
        if "api.bybit.com/v5/market/funding/history" in url or "api.bybit.com/v5/market/tickers" in url:
            sym = p.get("symbol", "")
            rows = self.ctx.store.funding_asof("linear", sym, self.ctx.clock.now, int(p.get("limit", 200))) if sym else []
            if rows:       # AS OF the simulated clock: only settlements that had happened. The live `fundingRate` of a ticker is the CURRENT rate; the last settled one stands in for it.
                if "funding/history" in url:
                    return _Resp({"retCode": 0, "result": {"list": [{"symbol": sym, "fundingRate": repr(r[1]), "fundingRateTimestamp": str(int(r[0]))} for r in rows]}})
                return _Resp({"retCode": 0, "result": {"list": [{"symbol": sym, "fundingRate": repr(rows[0][1])}]}})
            self.ctx.notes.add("funding/ticker data is not served by replay (the prompt line is left out)")
            return _Resp({"retCode": 0, "result": {"list": []}})
        self.ctx.blocked.append(url)
        raise ReplayBlocked(f"replay refuses to reach {url}")

    def post(self, url, *a, **k):
        self.ctx.blocked.append(url)
        raise ReplayBlocked(f"replay refuses POST {url}")


# ----------------------------------------------------------------- data provider
class _RunMode:
    value = "dry_run"


class FakeDP:
    def __init__(self, ctx: "ReplayContext"):
        self.ctx = ctx
        self.runmode = _RunMode()          # per instance: the no-LLM rule mode flips it to "backtest" for ONE bot

    def current_whitelist(self):
        return list(self.ctx.whitelist_asof())

    def get_analyzed_dataframe(self, pair, timeframe):
        df = self.ctx.frame_asof(pair, self.ctx.df_asof or self.ctx.clock.now)
        return df, (df.iloc[-1]["date"] if len(df) else None)

    def ticker(self, pair):
        df = self.ctx.frame_asof(pair, self.ctx.clock.now, 1)
        return {"last": float(df.iloc[-1]["close"])} if len(df) else None


class FakeWallets:
    def __init__(self, ctx: "ReplayContext"):
        self.ctx = ctx

    def get_available_stake_amount(self):
        return self.ctx.cash_free()

    def get_free(self, *_a):
        return self.ctx.cash_free()


# -------------------------------------------------------------------- context
class ReplayContext:
    """Everything one bot's replay needs, shared by the strategy shims and the engine (one per bot; the clock is the replay time)."""

    def __init__(self, bot: str, store: rdata.SeriesStore, category: str, pairs: list[str], fng_hist, anonymize: bool = False):
        self.bot, self.store, self.category, self.pairs = bot, store, category, pairs
        self.clock = SimClock()
        self.df_asof: datetime | None = None        # what the strategy's dataframe snapshot is "as of" (see engine: stale-signal semantics)
        self.fng_hist = fng_hist
        self.anonymize = anonymize
        self.blocked: list[str] = []
        self.notes: set[str] = set()
        self.cash = 0.0
        self.trades_open: list = []
        self.trades_closed: list = []
        self.current_pair: str | None = None

    def cash_free(self) -> float:
        return float(self.cash)

    def symbol(self, pair: str) -> str:
        return rdata.bybit_symbol(pair)

    def frame_asof(self, pair: str, t: datetime, n: int | None = None) -> pd.DataFrame:
        return self.store.asof(self.category, self.symbol(pair), t, n)

    def whitelist_asof(self) -> list[str]:
        syms = [self.symbol(p) for p in self.pairs]
        order = self.store.turnover_rank(self.category, syms, self.clock.now)
        by_sym = {self.symbol(p): p for p in self.pairs}
        return [by_sym[s] for s in order]

    def fng_text(self):
        v = rdata.fng_asof(self.fng_hist, self.clock.now) if self.fng_hist else None
        return None if v is None else f"Fear & Greed Index: {v[0]} ({v[1]})"


# ------------------------------------------------------------------ fake trades
class FakeTrade:
    """The attributes the strategies read from a Freqtrade Trade. Prices are real replayed prices; profit ratios include fee and slippage."""

    def __init__(self, tid, pair, is_short, open_dt, open_rate, stake, leverage, enter_tag, fee):
        self.id, self.pair, self.is_short = tid, pair, bool(is_short)
        self.open_date = self.open_date_utc = open_dt
        self.open_rate, self.stake_amount, self.leverage, self.enter_tag = open_rate, stake, leverage, enter_tag
        self.fee = fee
        self.is_open = True
        self.close_date = None
        self.close_rate = None
        self.close_profit = None
        self.close_profit_abs = None
        self.exit_reason = None
        self.fees_abs = 0.0
        self.strategy = "replay"
        self.carry_over = False
        self.live_id = None

    def calc_profit_ratio(self, rate: float) -> float:
        f = self.fee
        if self.is_short:
            return (self.open_rate * (1 - f)) / (rate * (1 + f)) - 1.0 if rate > 0 else 0.0
        return (rate * (1 - f)) / (self.open_rate * (1 + f)) - 1.0

    def rate_for_profit(self, target: float) -> float:
        """The price at which calc_profit_ratio == target (for ROI / stoploss exits)."""
        f = self.fee
        if self.is_short:
            return self.open_rate * (1 - f) / ((1 + target) * (1 + f))
        return (1 + target) * self.open_rate * (1 + f) / (1 - f)

    def calc_profit(self, rate: float) -> float:
        return self.stake_amount * self.calc_profit_ratio(rate)

    def __repr__(self):
        return f"FakeTrade({self.id},{self.pair},{'S' if self.is_short else 'L'},{'open' if self.is_open else 'closed'})"


def make_replay_trade_class(ctx: ReplayContext):
    class ReplayTrade:
        @classmethod
        def get_trades_proxy(cls, is_open=None, **kw):
            if is_open is True:
                return list(ctx.trades_open)
            if is_open is False:
                return list(ctx.trades_closed)
            return list(ctx.trades_open) + list(ctx.trades_closed)

        @classmethod
        def get_open_trade_count(cls):
            return len(ctx.trades_open)

        @classmethod
        def total_open_trades_stakes(cls):
            return float(sum(tr.stake_amount for tr in ctx.trades_open))
    return ReplayTrade


# ----------------------------------------------------------------------- loader
def default_strategies_dir() -> Path:
    import os
    env = os.environ.get("REPLAY_STRATEGIES_DIR")
    if env:
        return Path(env)
    baked = Path("/app/strategies_src")
    return baked if baked.is_dir() else Path(__file__).resolve().parents[2]


def strategy_path(spec: dict, base: Path | None = None) -> Path:
    base = Path(base) if base else default_strategies_dir()
    baked = base / spec["key"] / spec["file"]
    return baked if baked.is_file() else base / spec["dir"] / "user_data" / "strategies" / spec["file"]


def _install_stubs():
    ft = types.ModuleType("freqtrade")
    pers = types.ModuleType("freqtrade.persistence")
    pers.Trade = type("Trade", (), {})
    strat = types.ModuleType("freqtrade.strategy")

    class IStrategy:
        timeframe = "1h"
        can_short = False

        def __init__(self, config=None):
            self.config = config or {}
            self.dp = None
            self.wallets = None
    strat.IStrategy = IStrategy
    sys.modules["freqtrade"], sys.modules["freqtrade.persistence"], sys.modules["freqtrade.strategy"] = ft, pers, strat
    so = types.ModuleType("_second_opinion")
    so.run_shadow = lambda *a, **k: None
    ac = types.ModuleType("_allocator_ctx")
    ac.context_block = lambda pair, bot_id: ""
    ac.stake_multiplier = lambda bot_id: 1.0
    ac.decision_config = lambda bot_id: {"source": "llm", "provider": "deepseek"}      # the replay always asks the LLM it is given; Kronos decisions are replayed through the frozen path
    ac.kronos_proposal = lambda pair, bot_id: None
    ac.entry_block = lambda pair: None                                                   # the announcement guard is a live-feed rule: a replay of the past never sees it
    ac.bot_param = lambda bot_id, name, default: default                             # live-feed parameters (P26) are never seen by a replay
    ac.fear_greed_text = lambda: None
    ac.market_text = lambda: None
    sys.modules["_second_opinion"], sys.modules["_allocator_ctx"] = so, ac


def load_bot(spec: dict, ctx: ReplayContext, llm_hook, config: dict, base: Path | None = None):
    """Returns (module, strategy_object). `llm_hook(system, user) -> str|None` replaces the strategy's DeepSeek call."""
    path = strategy_path(spec, base)
    if not path.is_file():
        raise HookError(f"strategy file not found: {path}")
    with _LOAD_LOCK:
        _install_stubs()
        sys.path.insert(0, str(path.parent))
        try:
            m = importlib.util.spec_from_file_location(f"replay_{spec['key']}_{id(ctx)}", path)
            mod = importlib.util.module_from_spec(m)
            m.loader.exec_module(mod)
        finally:
            sys.path.remove(str(path.parent))
    cls = next((v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == mod.__name__ and hasattr(v, "_ask_deepseek")), None)
    if cls is None:
        raise HookError(f"{spec['key']}: no strategy class with _ask_deepseek in {path.name}")
    for name in ("requests", "Trade", "get_market_context", "get_news_headlines"):      # the data sources of the prompt: they MUST be replaceable
        if not hasattr(mod, name):
            raise HookError(f"{spec['key']}: expected module global {name!r} is missing -- the hook cannot be replaced without editing the live file")
    mod.requests = ReplayHTTP(ctx)
    mod.Trade = make_replay_trade_class(ctx)
    mod.get_market_context = lambda: HISTORICAL_CONTEXT_NOTE
    mod.get_news_headlines = lambda: HISTORICAL_NEWS_NOTE
    if hasattr(mod, "datetime"):                 # optional: a bot that never uses the wall clock simply has no such global
        mod.datetime = make_datetime(ctx.clock)
    if hasattr(mod, "time"):
        mod.time = SimTime(ctx.clock)
    if hasattr(mod, "get_fear_greed_index"):
        mod.get_fear_greed_index = ctx.fng_text
    obj = cls(config)
    obj.dp = FakeDP(ctx)
    obj.wallets = FakeWallets(ctx)
    for hook in ("_call_deepseek", "_call_openrouter_fallback"):
        if not hasattr(obj, hook):
            raise HookError(f"{spec['key']}: strategy has no {hook}")
    obj._call_deepseek = llm_hook
    obj._call_openrouter_fallback = lambda system, user: None
    return mod, obj


# --------------------------------------------------------------------- anonymiser
ISO_RE = re.compile(r"\b20\d\d-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d)?)?(?:Z|[+-]\d\d:?\d\d)?\b")


class Anonymizer:
    """named -> anonymized prompt: every pair / coin becomes ASSET_NN (stable per run), absolute dates become 'T-<n>h'. Prices stay (the model needs them)."""

    def __init__(self, pairs: list[str]):
        self.map: dict[str, str] = {}
        coins = sorted({p.split("/")[0] for p in pairs}, key=lambda c: (-len(c), c))
        for i, coin in enumerate(sorted({p.split("/")[0] for p in pairs})):
            self.map[coin] = f"ASSET_{i + 1:02d}"
        self.order = coins

    def __call__(self, text: str, now: datetime | None = None) -> str:
        out = text
        for coin in self.order:
            out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(coin)}(?![a-z0-9])", self.map[coin], out)
        if now is not None:
            def rel(m):
                try:
                    d = datetime.fromisoformat(m.group(0).replace("Z", "+00:00").replace(" ", "T"))
                    d = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
                    return f"T-{int((now - d).total_seconds() // 3600)}h"
                except ValueError:
                    return "T-?"
            out = ISO_RE.sub(rel, out)
        return out
