"""
P17-B: Trend Ensemble v2 -- signal logic on synthetic data, gates, wiring.

Runs against REAL Freqtrade when it is importable (e.g. the backtest venv), and
against minimal stubs otherwise (same technique as the other P-series tests) so
it works in the plain project environment too. Logic checks are identical.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
STRATEGY_PATH = REPO / "freqtrade-trend-v2" / "user_data" / "strategies" / "TrendV2Strategy.py"

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


# ------------------------------------------------------------ load strategy
try:
    import freqtrade  # noqa: F401
    from freqtrade.strategy import IStrategy  # noqa: F401
    REAL = True
except Exception:
    REAL = False
    ft = types.ModuleType("freqtrade")
    ft_strategy = types.ModuleType("freqtrade.strategy")

    class _IStrategy:
        def __init__(self, config):
            self.config = config

    def _merge_informative_pair(dataframe, informative, timeframe, timeframe_inf, ffill=True):
        inf = informative.copy()
        inf["date"] = inf["date"] + pd.Timedelta(days=1)  # lag one interval (1d/1d only)
        inf = inf.rename(columns={c: f"{c}_{timeframe_inf}" for c in inf.columns})
        merged = pd.merge_asof(dataframe.sort_values("date"), inf.sort_values(f"date_{timeframe_inf}"),
                               left_on="date", right_on=f"date_{timeframe_inf}")
        return merged

    ft_strategy.IStrategy = _IStrategy
    ft_strategy.merge_informative_pair = _merge_informative_pair
    sys.modules["freqtrade"] = ft
    sys.modules["freqtrade.strategy"] = ft_strategy

sys.path.insert(0, str(Path(STRATEGY_PATH).parent))   # siblings (_allocator_ctx) import from the strategies dir, as in Freqtrade
spec = importlib.util.spec_from_file_location("trend_v2_module", STRATEGY_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
S = mod.TrendV2Strategy
print(f"(running against {'REAL Freqtrade' if REAL else 'stubbed Freqtrade'})")

# ------------------------------------------------------------- synthetic data
N = 420
DATES = pd.date_range("2022-01-01", periods=N, freq="1D", tz="UTC")


def make_ohlc(close: np.ndarray) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame({
        "date": DATES, "open": close, "high": close * 1.004, "low": close * 0.996,
        "close": close, "volume": np.full(N, 1000.0),
    })


def uptrend():   # 250 flat-ish days then a steady climb: fresh breakouts
    return np.concatenate([100 + np.sin(np.arange(250) / 6.0) * 2, 100 * (1.012 ** np.arange(1, N - 249))])


def downtrend():
    return np.concatenate([100 + np.sin(np.arange(250) / 6.0) * 2, 100 * (0.988 ** np.arange(1, N - 249))])


class FakeDP:
    def __init__(self, btc_close, funding_rate=None, whitelist=("SYN/USDT:USDT",)):
        self.btc = make_ohlc(btc_close)
        self.funding_rate = funding_rate
        self.whitelist = list(whitelist)

    def get_pair_dataframe(self, pair, timeframe=None, candle_type=""):
        if pair == mod.BTC_PAIR and (timeframe in (None, "1d")):
            return self.btc.copy()
        if candle_type == "funding_rate":
            if self.funding_rate is None:
                return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
            dates = pd.date_range(DATES[0], periods=N * 3, freq="8h", tz="UTC")
            v = np.full(len(dates), self.funding_rate) if np.isscalar(self.funding_rate) else self.funding_rate
            return pd.DataFrame({"date": dates, "funding_rate": v, "open": v})   # real column layout
        return pd.DataFrame()

    def current_whitelist(self):
        return self.whitelist

    def get_funding_rate_timeframe(self):
        return "1h"   # what Freqtrade reports for Bybit

    def get_analyzed_dataframe(self, pair, timeframe):
        return self._analyzed, None


def run(close, btc_close, funding=None, cfg=None):
    config = {"dry_run": True, "trend_v2": cfg or {}}
    s = S(config)
    s.dp = FakeDP(btc_close, funding)
    df = make_ohlc(close)
    df = s.populate_indicators(df, {"pair": "SYN/USDT:USDT"})
    df = s.populate_entry_trend(df, {"pair": "SYN/USDT:USDT"})
    df = s.populate_exit_trend(df, {"pair": "SYN/USDT:USDT"})
    return s, df


def n_signals(df, col):
    return int(df[col].fillna(0).sum()) if col in df.columns else 0


UP, DOWN = uptrend(), downtrend()
BULL_BTC, BEAR_BTC = uptrend(), downtrend()   # BTC above / below its 200d SMA at the end

# ----------------------------------------------------------------- constants
check("timeframe 1d, can_short, native trailing disabled (v1 dead zone removed)",
      S.timeframe == "1d" and S.can_short is True and S.trailing_stop is False)
check("hard stoploss -15%", S.stoploss == -0.15)
check("hysteresis MIN_HOLD_DAYS == 3", S.MIN_HOLD_DAYS == 3)
check("size multiplier clamp 0.25 / 1.5", S.MIN_SIZE_MULTIPLIER == 0.25 and S.MAX_SIZE_MULTIPLIER == 1.5)
check("funding threshold -10% annualized", S.FUNDING_MIN_ANNUAL_SHORT == -0.10)
check("declared exit-channel default is one of {10, 20}; min_votes default in {2,3,4}",
      S.DEFAULT_EXIT_CHANNEL in (10, 20) and S.DEFAULT_MIN_VOTES in (2, 3, 4))
methods = {p["method"] for p in S.protections}
check("protections: CooldownPeriod + StoplossGuard", methods == {"CooldownPeriod", "StoplossGuard"})

# -------------------------------------------------------------- entry signals
s, df = run(UP, BULL_BTC)
check("uptrend + BTC above 200d SMA -> long entries fire", n_signals(df, "enter_long") > 0)
check("uptrend -> no short entries", n_signals(df, "enter_short") == 0)
check("long entries carry the donchian_long tag",
      set(df.loc[df["enter_long"] == 1, "enter_tag"]) == {"donchian_long"})

s, df = run(UP, BEAR_BTC)
check("regime filter: BTC below 200d SMA blocks longs even on a clean breakout",
      n_signals(df, "enter_long") == 0)

s, df = run(DOWN, BEAR_BTC)
check("downtrend + BTC below 200d SMA -> short entries fire", n_signals(df, "enter_short") > 0)
check("downtrend -> no long entries", n_signals(df, "enter_long") == 0)

s, df = run(DOWN, BULL_BTC)
check("regime filter: BTC above 200d SMA blocks shorts", n_signals(df, "enter_short") == 0)

# ------------------------------------------------------------------- funding
_, df_ok = run(DOWN, BEAR_BTC, funding=0.0001)           # +0.01%/8h = +11% p.a.
_, df_bad = run(DOWN, BEAR_BTC, funding=-0.0003)         # -0.03%/8h = -33% p.a.
_, df_edge_ok = run(DOWN, BEAR_BTC, funding=-0.00008)    # ~ -8.8% p.a. -> above the -10% limit
_, df_none = run(DOWN, BEAR_BTC, funding=None)           # no funding history -> fail open
check("funding: positive funding does not block shorts", n_signals(df_ok, "enter_short") > 0)
check("funding: deeply negative funding (-33% annualized) blocks shorts", n_signals(df_bad, "enter_short") == 0)
check("funding: -8.8% annualized (above the -10% limit) still allows shorts", n_signals(df_edge_ok, "enter_short") > 0)
check("funding: unknown funding fails OPEN (does not block)", n_signals(df_none, "enter_short") > 0)
check("funding_ann column is the daily mean x 3 x 365",
      abs(float(df_ok["funding_ann"].dropna().iloc[-1]) - 0.0001 * 1095) < 1e-9)
_, df_bad_long = run(UP, BULL_BTC, funding=-0.0003)
check("funding filter is short-only: longs unaffected by negative funding", n_signals(df_bad_long, "enter_long") > 0)

# The funding value applied to candle D must come from days strictly BEFORE D
# (lookahead-analysis flagged the same-day version as biased).
fund_step = np.full(N * 3, 0.0001)
fund_step[(N - 60) * 3:] = -0.0004          # funding turns deeply negative on day N-60
_, df_step = run(DOWN, BEAR_BTC, funding=fund_step)
first_neg_day = N - 60
check("funding lag: candle on the day funding turns negative still sees the old (positive) value",
      float(df_step["funding_ann"].iloc[first_neg_day]) > 0)
check("funding lag: the NEXT candle sees the new (negative) value",
      float(df_step["funding_ann"].iloc[first_neg_day + 1]) < -0.10)
check("funding lag: partial-day data cannot change an earlier candle's value (truncation-invariant)",
      float(run(DOWN, BEAR_BTC, funding=fund_step)[1]["funding_ann"].iloc[first_neg_day - 5])
      == float(df_step["funding_ann"].iloc[first_neg_day - 5]))

# ------------------------------------------------------------- min votes cfg
# A drifting random walk (fixed seed): realistic, partial channel agreement.
UP_BTC_FOR_VOTES = UP
BOUNCE = 100 * np.exp(np.cumsum(np.random.default_rng(7).normal(0.001, 0.03, N)))
_, d2 = run(BOUNCE, UP_BTC_FOR_VOTES, cfg={"min_votes": 2})
_, d5 = run(BOUNCE, UP_BTC_FOR_VOTES, cfg={"min_votes": 5})
s_v = S({"dry_run": True})
lv, _sv = s_v._votes(run(BOUNCE, UP_BTC_FOR_VOTES)[1])
check("votes can be partial (some day has 2-4 of the 5 channels agreeing)", bool(((lv >= 2) & (lv <= 4)).any()))
check("min_votes is configurable: 2 votes fires at least as often as 5", n_signals(d2, "enter_long") >= n_signals(d5, "enter_long"))
check("min_votes=5 is strictly stricter than 2 on partial-agreement data", n_signals(d2, "enter_long") > n_signals(d5, "enter_long"))
_, dl = run(UP, BULL_BTC, cfg={"allow_long": False})
_, ds = run(DOWN, BEAR_BTC, cfg={"allow_short": False})
check("allow_long=false disables longs", n_signals(dl, "enter_long") == 0)
check("allow_short=false disables shorts", n_signals(ds, "enter_short") == 0)

# --------------------------------------------------------------------- exits
# uptrend then a sharp drop: the close must break the prior N-day low
drop = np.concatenate([UP[:350], UP[349] * (0.97 ** np.arange(1, N - 349))])
for ch in (10, 20):
    _, dx = run(drop, BULL_BTC, cfg={"exit_channel": ch})
    fired = dx.index[dx["exit_long"] == 1]
    check(f"exit_channel={ch}: long exit fires after the drop breaks the prior {ch}-day low",
          len(fired) > 0 and fired[0] >= 350)
    check(f"exit_channel={ch}: exit tag is donchian_exit_long",
          set(dx.loc[dx["exit_long"] == 1, "exit_tag"]) == {"donchian_exit_long"})
    row = int(fired[0])
    expected_low = dx["low"].iloc[row - ch:row].min()
    check(f"exit_channel={ch}: threshold is the prior {ch}-day low (shifted, excludes today)",
          abs(dx["exit_low"].iloc[row] - expected_low) < 1e-9 and dx["close"].iloc[row] < expected_low)
pump = np.concatenate([DOWN[:350], DOWN[349] * (1.03 ** np.arange(1, N - 349))])
_, dsx = run(pump, BEAR_BTC)
check("short exit fires when the close breaks the prior N-day high", n_signals(dsx, "exit_short") > 0)

# ------------------------------------------------------------- no lookahead
s_full, full = run(UP, BULL_BTC)
k = 330
trunc_close = UP[:k]
_ = None
cfg = {"dry_run": True, "trend_v2": {}}
s_t = S(cfg)
btc_k = BULL_BTC[:k]
dpk = FakeDP(BULL_BTC)
dpk.btc = make_ohlc(BULL_BTC).iloc[:k].reset_index(drop=True)
s_t.dp = dpk
dft = make_ohlc(UP).iloc[:k].reset_index(drop=True)
dft = s_t.populate_indicators(dft, {"pair": "SYN/USDT:USDT"})
dft = s_t.populate_entry_trend(dft, {"pair": "SYN/USDT:USDT"})
same = (full["enter_long"].fillna(0).iloc[:k].values == dft["enter_long"].fillna(0).values).all()
check("no lookahead: entry signals on a truncated series equal the full-series signals up to the cut", bool(same))

# ------------------------------------------------------------------- hysteresis
now = datetime(2025, 1, 10, tzinfo=timezone.utc)


class T:
    def __init__(self, opened):
        self.open_date_utc = opened


s = S({"dry_run": True})
young, old = T(now - timedelta(days=2)), T(now - timedelta(days=3, hours=1))
check("hysteresis: channel exit vetoed inside the first 3 days",
      s.confirm_trade_exit("X", young, "limit", 1, 1, "GTC", "donchian_exit_long", now) is False)
check("hysteresis: channel exit allowed after 3 days",
      s.confirm_trade_exit("X", old, "limit", 1, 1, "GTC", "donchian_exit_long", now) is True)
check("hysteresis: short-side channel exit is vetoed too when young",
      s.confirm_trade_exit("X", young, "limit", 1, 1, "GTC", "donchian_exit_short", now) is False)
for reason in ("stop_loss", "stoploss_on_exchange", "force_exit", "emergency_exit", "liquidation", "roi"):
    check(f"hysteresis: never vetoes '{reason}' even on a 1-day-old trade",
          s.confirm_trade_exit("X", T(now - timedelta(days=1)), "limit", 1, 1, "GTC", reason, now) is True)

# -------------------------------------------------------------------- sizing
def stake_for(vol):
    s = S({"dry_run": True})
    s.dp = types.SimpleNamespace(
        get_analyzed_dataframe=lambda pair, tf: (pd.DataFrame({"realized_vol_pct": [vol]}), None))
    return s.custom_stake_amount("X", now, 1.0, 100.0, 5.0, 1000.0, 1.0, None, "long")


check("sizing: vol == target -> 1.0x", abs(stake_for(4.0) - 100.0) < 1e-9)
check("sizing: very high vol clamps at 0.25x", abs(stake_for(40.0) - 25.0) < 1e-9)
check("sizing: very low vol clamps at 1.5x", abs(stake_for(0.5) - 150.0) < 1e-9)
check("sizing: 8% daily vol -> 0.5x", abs(stake_for(8.0) - 50.0) < 1e-9)
check("sizing: NaN vol falls back to the proposed stake", stake_for(float("nan")) == 100.0)
s = S({"dry_run": True})
s.dp = types.SimpleNamespace(get_analyzed_dataframe=lambda pair, tf: (_ for _ in ()).throw(RuntimeError("boom")))
check("sizing: dataframe error falls back to the proposed stake",
      s.custom_stake_amount("X", now, 1.0, 100.0, 5.0, 1000.0, 1.0, None, "long") == 100.0)
check("sizing never exceeds max_stake", (lambda: (lambda st: st.custom_stake_amount("X", now, 1.0, 100.0, 5.0, 120.0, 1.0, None, "long"))(
    (lambda s: (setattr(s, "dp", types.SimpleNamespace(get_analyzed_dataframe=lambda p, t: (pd.DataFrame({"realized_vol_pct": [0.5]}), None))), s)[1])(S({"dry_run": True}))))() == 120.0)
check("leverage is fixed at 1.0", S({"dry_run": True}).leverage("X", now, 1.0, 5.0, 10.0, None, "long") == 1.0)

# ------------------------------------------------------------------ live gate
os.environ.pop("TREND_V2_LIVE_MODE", None)
check("live gate: dry_run=True always allows entry",
      S({"dry_run": True}).confirm_trade_entry("X", "limit", 1, 1, "GTC", now, None, "long") is True)
check("live gate: dry_run=False without TREND_V2_LIVE_MODE rejects",
      S({"dry_run": False}).confirm_trade_entry("X", "limit", 1, 1, "GTC", now, None, "long") is False)
os.environ["TREND_V2_LIVE_MODE"] = "true"
check("live gate: dry_run=False WITH TREND_V2_LIVE_MODE=true allows",
      S({"dry_run": False}).confirm_trade_entry("X", "limit", 1, 1, "GTC", now, None, "long") is True)
os.environ.pop("TREND_V2_LIVE_MODE", None)

# ------------------------------------------------- informative pairs (live data)
s = S({"dry_run": True})
s.dp = FakeDP(BULL_BTC, whitelist=["ETH/USDT:USDT", "SOL/USDT:USDT"])
ip = s.informative_pairs()
check("informative_pairs: BTC 1d + each whitelisted pair's funding_rate (Freqtrade's funding timeframe)",
      (mod.BTC_PAIR, "1d", "futures") in ip and ("ETH/USDT:USDT", "1h", "funding_rate") in ip and ("SOL/USDT:USDT", "1h", "funding_rate") in ip)

# ---------------------------------------------------------- config / wiring
cfg = json.loads(read(REPO / "freqtrade-trend-v2/user_data/config.json"))
check("config: dry_run true, futures isolated, $500, 10 slots, unlimited stake",
      cfg["dry_run"] is True and cfg["trading_mode"] == "futures" and cfg["margin_mode"] == "isolated"
      and cfg["dry_run_wallet"] == 500 and cfg["max_open_trades"] == 10 and cfg["stake_amount"] == "unlimited")
check("config: no exchange keys committed", cfg["exchange"]["key"] == "" and cfg["exchange"]["secret"] == "")
check("config: top-20 by volume, no VolatilityFilter (it excluded BTC/ETH as 'too calm')",
      cfg["pairlists"][0]["number_assets"] == 20 and all(p["method"] != "VolatilityFilter" for p in cfg["pairlists"]))
check("config: stablecoins incl. RLUSD blacklisted (v1 lacked RLUSD)", "RLUSD/USDT:USDT" in cfg["exchange"]["pair_blacklist"])
check("config: trend_v2 section has the declared parameters",
      cfg["trend_v2"]["min_votes"] in (2, 3, 4) and cfg["trend_v2"]["exit_channel"] in (10, 20))

compose = yaml.safe_load(read(REPO / "docker-compose.yml"))
svc = compose["services"]["freqtrade-trend-v2"]
env = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in svc["environment"]}
check("compose: v2 service on 8200, own db volume, TrendV2Strategy, persistent logfile",
      svc["ports"] == ["8200:8080"] and svc["volumes"] == ["freqtrade-trend-v2-db:/freqtrade/user_data/db", "allocator-shared:/allocator:ro"]
      and "freqtrade-trend-v2-db" in compose["volumes"] and "--strategy TrendV2Strategy" in " ".join(str(svc["command"]).split())
      and "--logfile /freqtrade/user_data/db/freqtrade.log" in " ".join(str(svc["command"]).split()))
check("compose: live gate like v1 (dry_run env defaults true, TREND_V2_LIVE_MODE defaults false)",
      env["FREQTRADE__DRY_RUN"].endswith(":-true}") and env["TREND_V2_LIVE_MODE"].endswith(":-false}"))
check("compose: FREQTRADE__MAX_OPEN_TRADES default equals config.json (no silent override)",
      env["FREQTRADE__MAX_OPEN_TRADES"].endswith(":-10}") and cfg["max_open_trades"] == 10)
host_ports = [p.split(":")[0] for s_ in compose["services"].values() for p in s_.get("ports", [])]
check("compose: all host ports unique", len(host_ports) == len(set(host_ports)))
mirror = {e.split("=", 1)[0] for e in compose["services"]["survival-agent"]["environment"]}
check("compose: survival-agent mirrors v2's env vars for the dashboard",
      {"BYBIT_TREND_V2_API_KEY", "BYBIT_TREND_V2_API_SECRET", "FREQTRADE_TREND_V2__DRY_RUN", "TREND_V2_LIVE_MODE"} <= mirror)

from backend.engine import freqtrade_client, payout_state  # noqa: E402
from backend.reports import bot_scorecard  # noqa: E402

check("BOTS has trend_v2 on the internal port",
      freqtrade_client.BOTS["trend_v2"]["url"] == "http://freqtrade-trend-v2:8080")
check("trend_v2 is NOT in payout SPLIT_BOTS", "trend_v2" not in payout_state.SPLIT_BOTS)
check("scorecard registers trend_v2 ($500 wallet, isolated-experiment note)",
      "trend_v2" in bot_scorecard.ALL_BOTS and bot_scorecard.BOT_START_WALLET_USD["trend_v2"] == 500.0
      and "trend_v2" in bot_scorecard.ISOLATED_EXPERIMENT_NOTES)
os.environ["DATA_DIR"] = __import__("tempfile").mkdtemp()
Path(os.environ["DATA_DIR"], "config.json").write_text(json.dumps(
    {"start": 500, "target": 5000, "months": 12, "survival_monthly": 60, "ramp_months": 0, "take_pct": 20, "risk_pct": 50}))
os.makedirs(os.path.abspath("/app/frontend"), exist_ok=True)
from backend import app as app_module  # noqa: E402

keys = {d["key"] for d in app_module.ENV_VAR_DEFS}
check("ENV_VAR_DEFS registers all four trend_v2 vars",
      {"BYBIT_TREND_V2_API_KEY", "BYBIT_TREND_V2_API_SECRET", "FREQTRADE_TREND_V2__DRY_RUN", "TREND_V2_LIVE_MODE"} <= keys)
check("max-open-trades UI maps know trend_v2 (env key + default 10)",
      app_module.MAX_OPEN_TRADES_ENV_KEYS["trend_v2"] == "TREND_V2_MAX_OPEN_TRADES"
      and app_module.DEFAULT_MAX_OPEN_TRADES["trend_v2"] == 10)

html = read(REPO / "frontend/index.html")
for table in ("BOT_PORTS", "BOT_ACCENT", "BOT_START_WALLET", "BOT_PARAMS", "GROUP_LABEL", "GROUP_ACCENT"):
    line = next(l for l in html.splitlines() if f"const {table} =" in l or (table.startswith("GROUP") and f"const {table}" in l))
    check(f"frontend {table} registers trend_v2", "trend_v2" in line or table == "BOT_PARAMS")
check("frontend BOT_PARAMS + strategy card + GROUP_ORDER register trend_v2",
      "  trend_v2: 'NOT CONFIRMED BY BACKTEST" in html and "'NOT CONFIRMED by backtest'" in html and "trend_v2: {\n    label: 'Trend Ensemble v2'" in html
      and "'trend_ensemble', 'trend_v2'" in next(l for l in html.splitlines() if "const GROUP_ORDER" in l))
check("frontend port + wallet are 8200 / 500", "trend_v2: 8200" in html and "trend_v2: 500" in html)

# ------------------------------------------------------------------ v1 frozen
# 26.10.2026: the digest was re-pinned once after a Russian word in the module docstring was translated to English (no code change).
FROZEN = {
    "freqtrade-trend-ensemble/user_data/strategies/TrendEnsembleStrategy.py":
        "6f0e87a69848f6494625a377bd0da512f037a0ddfcfaf5c85912fc76ed2cc9a2",
}
for rel, digest in FROZEN.items():
    check(f"v1 is frozen: {rel.split('/')[-1]} unchanged (sha256)", hashlib.sha256(read(REPO / rel).encode()).hexdigest() == digest)
# 07.10.2026: the host name was removed from every config.json and a CORS entrypoint was added to every Dockerfile (FREQTRADE_UI_HOST). That touches only the REST/UI origin list, never trading:
# so the freeze now compares the config WITHOUT api_server.CORS_origins and the Dockerfile WITHOUT the entrypoint block, against the same digests as before (config digest of the sorted JSON).
import json as _json  # noqa: E402

_cfg = _json.loads(read(REPO / "freqtrade-trend-ensemble/user_data/config.json"))
_cfg["api_server"]["CORS_origins"] = []
check("v1 is frozen: config.json unchanged except api_server.CORS_origins (sha256 of the sorted JSON)",
      hashlib.sha256(_json.dumps(_cfg, sort_keys=True).encode()).hexdigest() == "afb90440b4e3c0e87176c35818be9f972a9d69088f8d871e436048c78a504a9e")
_blk = "\n".join(["", "# CORS origins from the FREQTRADE_UI_HOST environment variable (no host name in the repository): see entrypoint-cors.sh", "COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh",
                  "USER root", "RUN chmod +x /freqtrade/entrypoint-cors.sh", "USER ftuser", ""])
check("v1 is frozen: Dockerfile unchanged except the CORS entrypoint block (sha256)",
      hashlib.sha256(read(REPO / "freqtrade-trend-ensemble/Dockerfile").replace(_blk, "").encode()).hexdigest() == "c90d172cbc8cbddad61f8fcb43529711bcb08057e07cbda4561dc52bb3175564")

if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nALL P17-B TREND V2 CHECKS PASSED")
