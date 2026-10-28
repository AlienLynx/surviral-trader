"""
P17-B: assemble the backtest report from the Freqtrade exports under
FT_BACKTEST_DIR/results. Run inside the backtest venv:

    D:/Trader/ft_backtest/venv/Scripts/python.exe backtest/p17_make_report.py

Writes backtest/p17_trend_v2_report.md and backtest/p17_results/*.json|txt
(small, tracked). Nothing here changes any parameter: it only reads results and
applies the admission gates that were declared in the spec.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import p17_backtest_lib as L  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT_MD = REPO / "backtest" / "p17_trend_v2_report.md"
OUT_DIR = REPO / "backtest" / "p17_results"
PAIRS = (L.FT_DIR / "pairs_futures.txt").read_text().split()

GATE_MAX_DD_PCT = 30.0
GATE_MIN_TRADES = 100


def tail_profile(trades) -> dict:
    """How much of the profit is a few outliers? (the P9 'excluding best trade' idea)"""
    p = trades["profit_abs"].sort_values(ascending=False)
    total = float(p.sum())
    return {"total": round(total, 2), "best_trade": round(float(p.iloc[0]), 2),
            "total_excl_best": round(total - float(p.iloc[0]), 2),
            "total_excl_top3": round(total - float(p.iloc[:3].sum()), 2),
            "best_pair": str(trades.loc[p.index[0], "pair"])}


def exit_mix(trades) -> list[dict]:
    g = trades.groupby(["is_short", "exit_reason"]).agg(
        n=("profit_abs", "size"), profit=("profit_abs", "sum"),
        avg_pct=("profit_ratio", lambda s: s.mean() * 100), avg_days=("trade_duration", lambda s: s.mean() / 1440))
    return [{"leg": "short" if k[0] else "long", "exit": k[1], "n": int(r.n), "profit": round(float(r.profit), 2),
             "avg_pct": round(float(r.avg_pct), 2), "avg_days": round(float(r.avg_days), 1)} for k, r in g.iterrows()]


def md_table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "_none_\n"
    head = "| " + " | ".join(cols) + " |\n|" + "|".join("---" for _ in cols) + "|\n"
    return head + "\n".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |" for r in rows) + "\n"


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    grid = json.loads((L.RESULTS / "train_grid.json").read_text())
    runs = {}
    for name in ("train_v1", "test_v1", "test_v2_long", "test_v2_ls", "test_control_long", "test_control_short"):
        if (L.RESULTS / name).exists():
            strat, trades = L.load_run(name)
            runs[name] = {"headline": L.headline(strat), "by_year": L.by_year(trades), "by_leg": L.by_leg(trades),
                          "by_regime": L.by_regime_and_leg(trades), "tail": tail_profile(trades) if len(trades) else None,
                          "exit_mix": exit_mix(trades) if len(trades) else [], "window": f"{strat['backtest_start']} -> {strat['backtest_end']}"}
    v2 = runs["test_v2_ls"]
    h = v2["headline"]
    gates = {
        f"max drawdown <= {GATE_MAX_DD_PCT:.0f}% (fees on)": (h["max_dd_pct"] <= GATE_MAX_DD_PCT, f"{h['max_dd_pct']}%"),
        f">= {GATE_MIN_TRADES} trades (long+short together)": (h["trades"] >= GATE_MIN_TRADES, str(h["trades"])),
        "short leg not below zero": (h["short_profit_abs"] >= 0, f"${h['short_profit_abs']}"),
    }
    passed = all(ok for ok, _ in gates.values())
    test_start = runs["test_v2_ls"]["window"].split(" -> ")[0][:10]
    bh = L.buy_and_hold(PAIRS, test_start, None, fee=0.001)

    payload = {"grid": grid, "runs": runs, "gates": {k: {"pass": v[0], "value": v[1]} for k, v in gates.items()},
               "admitted": passed, "buy_and_hold": bh}
    (OUT_DIR / "results.json").write_text(json.dumps(payload, indent=2, default=str))
    for name in ("lookahead", "recursive"):
        src = L.RESULTS / "analysis" / f"{name}.log"
        if src.exists():
            lines = src.read_text(errors="replace").splitlines()
            keep = [l for l in lines if l.startswith(("|", "+")) or "bias" in l.lower() or "exit=" in l or "took" in l]
            (OUT_DIR / f"{name}_analysis.txt").write_text("\n".join(keep) + "\n")

    def row(label, name):
        r = runs[name]["headline"]
        return {"strategy": label, "return %": r["total_return_pct"], "CAGR %": r["cagr_pct"], "Sharpe": r["sharpe"],
                "Sortino": r["sortino"], "max DD %": r["max_dd_pct"], "Calmar": r["calmar"], "trades": r["trades"],
                "expectancy $": r["expectancy_usd"], "long $": r["long_profit_abs"], "short $": r["short_profit_abs"]}

    cmp_rows = [row("v1 (spot long-only, frozen)", "test_v1"), row("v2 long-only", "test_v2_long"), row("v2 long+short (chosen)", "test_v2_ls")]
    for label, name in (("control-long (5m sample strategy, spot)", "test_control_long"), ("control-short (5m sample strategy, futures)", "test_control_short")):
        if name in runs:
            cmp_rows.append(row(label, name))
    for b in (bh["btc"], bh["equal_weight"]):
        cmp_rows.append({"strategy": b["label"], "return %": b["total_return_pct"], "CAGR %": b["cagr_pct"], "Sharpe": b["sharpe"],
                         "Sortino": b["sortino"], "max DD %": b["max_dd_pct"], "Calmar": b["calmar"], "trades": 1,
                         "expectancy $": "-", "long $": "-", "short $": "-"})
    cols = ["strategy", "return %", "CAGR %", "Sharpe", "Sortino", "max DD %", "Calmar", "trades", "expectancy $", "long $", "short $"]

    md = ["# P17-B Trend Ensemble v2 -- backtest report", "",
          f"**Verdict: {'ADMITTED' if passed else 'NOT CONFIRMED (failed the admission gates)'}** -- deployed dry-run only, "
          + ("" if passed else "marked 'not confirmed'. No tuning after the test window was looked at."), "",
          "## Gates (test window, fees on)", "", md_table([{"gate": k, "result": "PASS" if v[0] else "FAIL", "value": v[1]} for k, v in gates.items()], ["gate", "result", "value"]),
          "## Comparison on the TEST window (" + test_start + " -> today)", "", md_table(cmp_rows, cols),
          "Fees: 0.1% per side for perps (0.15% spot = fee + slippage), funding fees included for futures, `--timeframe-detail 1h`, "
          "`--enable-protections`. Wallet $500, 10 slots, 15 fixed long-lived perps (survivorship bias: they are 15 coins that still exist today). "
          "Buy-and-hold rows use daily closes, one entry and one exit fee, no rebalance. Freqtrade's Sharpe/Sortino/Calmar are trade-based; "
          "buy-and-hold ones are daily-equity based -- comparable in sign and order of magnitude, not to the third decimal.", ""]
    md += ["## Training grid (2021-01-01 .. 2023-12-31): the 6 declared variants", "",
           md_table([{"exit channel": r["exit_channel"], "min votes": r["min_votes"], "return %": r["total_return_pct"], "Sharpe": r["sharpe"],
                      "max DD %": r["max_dd_pct"], "trades": r["trades"], "long $": r["long_profit_abs"], "short $": r["short_profit_abs"],
                      "smoothed score": round(grid["smoothed"][f"ch{r['exit_channel']}_v{r['min_votes']}"], 3)} for r in grid["rows"]],
                    ["exit channel", "min votes", "return %", "Sharpe", "max DD %", "trades", "long $", "short $", "smoothed score"]),
           f"Chosen by the pre-declared plateau rule: **exit channel {grid['chosen']['exit_channel']}, min votes {grid['chosen']['min_votes']}**.", ""]
    for name, title in (("test_v2_ls", "v2 long+short (chosen), test window"), ("test_v2_long", "v2 long-only, test window"), ("test_v1", "v1, test window")):
        r = runs[name]
        md += [f"## Breakdown: {title}", "", "By year (close date):", "", md_table(r["by_year"], ["year", "trades", "profit_abs", "win_rate_pct"]),
               "By leg:", "", md_table(r["by_leg"], ["leg", "trades", "profit_abs", "avg_profit_pct", "expectancy_usd", "win_rate_pct", "funding_fees_total"]),
               "By BTC regime (above/below its 200d SMA the day before entry) and leg:", "", md_table(r["by_regime"], ["btc_vs_200d", "leg", "trades", "profit_abs", "avg_profit_pct", "win_rate_pct"]),
               "Exit reasons:", "", md_table(r["exit_mix"], ["leg", "exit", "n", "profit", "avg_pct", "avg_days"]),
               f"Profit concentration: total ${r['tail']['total']}, best trade ${r['tail']['best_trade']} ({r['tail']['best_pair']}), "
               f"excluding the best trade ${r['tail']['total_excl_best']}, excluding the top 3 ${r['tail']['total_excl_top3']}.", ""]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(OUT_MD, "admitted" if passed else "NOT admitted")


if __name__ == "__main__":
    main()
