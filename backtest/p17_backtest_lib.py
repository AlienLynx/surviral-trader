"""
P17-B backtest helpers: load Freqtrade backtest exports, compute the metrics
the spec asks for, choose a parameter plateau, break results down by year / BTC
regime / long-vs-short leg, and compute buy-and-hold baselines from the same
candle files. Pure functions over files -- no Freqtrade *runtime* needed except
`freqtrade.data.btanalysis` for reading export zips (run inside the backtest
venv: D:/Trader/ft_backtest/venv).

DECLARED IN ADVANCE (before the grid was looked at; see AGENTS.md P17-B):
  * variants: exit_channel {10, 20} x min_votes {2, 3, 4}  -> exactly 6
  * selection score: Freqtrade's `sharpe` on the TRAIN window (2021-01-01..2023-12-31)
  * plateau rule: a variant's smoothed score = mean of the raw scores of itself and
    its min_votes neighbours WITHIN THE SAME exit_channel; pick the highest smoothed
    score (a plateau, not a peak); tie -> higher min_votes, then exit_channel 20
  * the TEST window (2024-01-01..today) is run ONCE for the chosen variant
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

FT_DIR = Path(os.environ.get("FT_BACKTEST_DIR", "D:/Trader/ft_backtest"))
DATA = FT_DIR / "data"
RESULTS = FT_DIR / "results"

VARIANTS = [(ch, v) for ch in (10, 20) for v in (2, 3, 4)]


# --------------------------------------------------------------------- loading
def load_run(name: str):
    from freqtrade.data.btanalysis import get_latest_backtest_filename, load_backtest_data, load_backtest_stats

    d = RESULTS / name
    fn = get_latest_backtest_filename(d)
    stats = load_backtest_stats(d / fn)
    strat = next(iter(stats["strategy"].values()))
    trades = load_backtest_data(d / fn)
    return strat, trades


# --------------------------------------------------------------------- metrics
def headline(strat: dict) -> dict:
    return {
        "total_return_pct": round(strat["profit_total"] * 100, 2),
        "profit_abs": round(strat["profit_total_abs"], 2),
        "cagr_pct": round(strat["cagr"] * 100, 2),
        "sharpe": round(strat["sharpe"], 3),
        "sortino": round(strat["sortino"], 3),
        "calmar": round(strat["calmar"], 3),
        "max_dd_pct": round(strat["max_drawdown_account"] * 100, 2),
        "trades": strat["total_trades"],
        "long_trades": strat["trade_count_long"],
        "short_trades": strat["trade_count_short"],
        "long_profit_abs": round(strat["profit_total_long_abs"], 2),
        "short_profit_abs": round(strat["profit_total_short_abs"], 2),
        "expectancy_usd": round(strat["expectancy"], 3),
        "expectancy_ratio": round(strat["expectancy_ratio"], 3),
        "profit_factor": round(strat["profit_factor"], 3),
        "win_rate_pct": round(strat["winrate"] * 100, 1),
        "sqn": round(strat["sqn"], 3),
        "fees_note": "fee ratio applied both sides; funding fees included for futures",
    }


def by_year(trades: pd.DataFrame) -> list[dict]:
    if not len(trades):
        return []
    t = trades.copy()
    t["year"] = pd.to_datetime(t["close_date"]).dt.year
    out = []
    for year, g in t.groupby("year"):
        out.append({"year": int(year), "trades": len(g), "profit_abs": round(g["profit_abs"].sum(), 2),
                    "win_rate_pct": round((g["profit_abs"] > 0).mean() * 100, 1)})
    return out


def btc_regime_series() -> pd.Series:
    """date -> 'above' / 'below' (BTC's daily close vs its 200d SMA, both as of that
    day's close)."""
    df = pd.read_feather(DATA / "futures" / "BTC_USDT_USDT-1d-futures.feather")
    df["sma"] = df["close"].rolling(200).mean()
    reg = pd.Series(np.where(df["close"] > df["sma"], "above", "below"), index=df["date"])
    reg[df["sma"].isna().values] = "n/a"
    return reg


def by_regime_and_leg(trades: pd.DataFrame) -> list[dict]:
    """Regime is taken at the day BEFORE the trade opened (what the strategy knew)."""
    if not len(trades):
        return []
    reg = btc_regime_series()
    t = trades.copy()
    prev_day = (pd.to_datetime(t["open_date"]).dt.floor("D") - pd.Timedelta(days=1))
    t["regime"] = [reg.get(d, "n/a") for d in prev_day]
    t["leg"] = np.where(t["is_short"], "short", "long")
    rows = []
    for (regime, leg), g in t.groupby(["regime", "leg"]):
        rows.append({"btc_vs_200d": regime, "leg": leg, "trades": len(g),
                     "profit_abs": round(g["profit_abs"].sum(), 2),
                     "avg_profit_pct": round(g["profit_ratio"].mean() * 100, 2),
                     "win_rate_pct": round((g["profit_abs"] > 0).mean() * 100, 1)})
    return rows


def by_leg(trades: pd.DataFrame) -> list[dict]:
    if not len(trades):
        return []
    rows = []
    for is_short, g in trades.groupby("is_short"):
        rows.append({"leg": "short" if is_short else "long", "trades": len(g),
                     "profit_abs": round(g["profit_abs"].sum(), 2),
                     "avg_profit_pct": round(g["profit_ratio"].mean() * 100, 2),
                     "expectancy_usd": round(g["profit_abs"].mean(), 3),
                     "win_rate_pct": round((g["profit_abs"] > 0).mean() * 100, 1),
                     "funding_fees_total": round(float(g.get("funding_fees", pd.Series(dtype=float)).sum()), 2)})
    return rows


# ------------------------------------------------------- plateau selection
def choose_plateau(scores: dict[tuple[int, int], float]) -> dict:
    """scores: {(exit_channel, min_votes): raw train score}. See module docstring."""
    smoothed = {}
    for (ch, v), s in scores.items():
        neigh = [scores[(ch, v + d)] for d in (-1, 0, 1) if (ch, v + d) in scores]
        smoothed[(ch, v)] = float(np.mean(neigh))
    best_val = max(smoothed.values())
    tied = [k for k, x in smoothed.items() if abs(x - best_val) < 1e-9]
    chosen = sorted(tied, key=lambda k: (-k[1], -k[0]))[0]
    return {"chosen": chosen, "raw": scores, "smoothed": smoothed}


# --------------------------------------------------- buy-and-hold baselines
def _stats_from_equity(eq: pd.Series, label: str) -> dict:
    eq = eq.dropna()
    ret = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    total = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 else float("nan")
    sharpe = ret.mean() / ret.std() * math.sqrt(365) if ret.std() > 0 else float("nan")
    down = ret[ret < 0].std()
    sortino = ret.mean() / down * math.sqrt(365) if down and down > 0 else float("nan")
    dd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(dd) if dd < 0 else float("nan")
    return {"label": label, "total_return_pct": round(total * 100, 2), "cagr_pct": round(cagr * 100, 2),
            "sharpe": round(sharpe, 3), "sortino": round(sortino, 3), "calmar": round(calmar, 3),
            "max_dd_pct": round(abs(dd) * 100, 2), "trades": 1}


def buy_and_hold(pairs: list[str], start: str, end: str | None, fee: float = 0.001) -> dict:
    """BTC buy-and-hold and equal-weight buy-and-hold (no rebalance) on FUTURES 1d closes,
    one entry fee at the start and one exit fee at the end."""
    frames = {}
    for p in pairs:
        f = DATA / "futures" / (p.replace("/", "_").replace(":", "_") + "-1d-futures.feather")
        df = pd.read_feather(f)[["date", "close"]].set_index("date")["close"]
        frames[p] = df
    px = pd.DataFrame(frames)
    px = px[px.index >= pd.Timestamp(start, tz="UTC")]
    if end:
        px = px[px.index <= pd.Timestamp(end, tz="UTC")]
    px = px.dropna(axis=1, how="any")
    norm = px / px.iloc[0]
    btc = norm["BTC/USDT:USDT"] * (1 - fee)
    btc.iloc[-1] *= (1 - fee)
    ew = norm.mean(axis=1) * (1 - fee)
    ew.iloc[-1] *= (1 - fee)
    return {"btc": _stats_from_equity(btc, "BTC buy-and-hold"),
            "equal_weight": _stats_from_equity(ew, f"Equal-weight hold ({norm.shape[1]} pairs)")}


def dump(name: str, obj) -> None:
    (RESULTS / f"{name}.json").write_text(json.dumps(obj, indent=2, default=str))
