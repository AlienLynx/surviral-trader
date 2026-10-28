"""
P9: unit + CLI tests for backend/reports/bot_scorecard.py. Same house
style as the other backtest/test_*.py files -- self-contained, no
pytest, OK/FAIL per check.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.reports import bot_scorecard as sc  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _make_db(rows) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    con = sqlite3.connect(tmp.name)
    con.execute("CREATE TABLE trades (close_profit REAL, close_profit_abs REAL, is_open INTEGER, close_date TEXT)")
    con.executemany("INSERT INTO trades VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()
    return tmp.name


NOW = datetime.now(timezone.utc)


# --- load_closed_trades_in_window ---
db1 = _make_db([
    (0.04, 20.0, 0, (NOW - timedelta(days=1)).isoformat()),
    (-0.03, -15.0, 0, (NOW - timedelta(days=2)).isoformat()),
    (0.5, 500.0, 0, (NOW - timedelta(days=40)).isoformat()),  # outside 21d window
    (0.1, 10.0, 1, (NOW - timedelta(days=1)).isoformat()),    # still open, excluded
])
trades = sc.load_closed_trades_in_window(db1, days=21)
check("load_closed_trades_in_window: excludes trades outside the window", len(trades) == 2)
check("load_closed_trades_in_window: excludes open trades", all(t[1] != 10.0 for t in trades))
check("load_closed_trades_in_window: chronological order (oldest first)",
      trades[0][2] < trades[1][2])


# --- pure math functions ---
check("compute_profit_factor: normal case", sc.compute_profit_factor([20, -10, 30, -5]) == 50 / 15)
check("compute_profit_factor: no losses -> None (undefined, never 0 or inf)",
      sc.compute_profit_factor([20, 30]) is None)
check("compute_profit_factor: no trades -> None", sc.compute_profit_factor([]) is None)

check("compute_expectancy_usd: average $ per trade", sc.compute_expectancy_usd([20, -10, 30]) == 40 / 3)
check("compute_expectancy_usd: empty -> None", sc.compute_expectancy_usd([]) is None)

check("compute_profit_factor_excl_best_trade: removes the single largest win",
      sc.compute_profit_factor_excl_best_trade([100, -10, 5, -5]) == sc.compute_profit_factor([-10, 5, -5]))
check("compute_profit_factor_excl_best_trade: empty -> None",
      sc.compute_profit_factor_excl_best_trade([]) is None)

check("compute_max_drawdown_pct: simple peak-to-trough as % of starting wallet",
      sc.compute_max_drawdown_pct([50, -30, 10], 100.0) == 30.0)
check("compute_max_drawdown_pct: no trades -> None", sc.compute_max_drawdown_pct([], 100.0) is None)
check("compute_max_drawdown_pct: zero/negative wallet -> None, never divide by zero",
      sc.compute_max_drawdown_pct([10], 0.0) is None)


# --- decide_verdict ---
t = sc.DEFAULT_THRESHOLDS
v, r = sc.decide_verdict(trade_count=3, profit_factor=5.0, max_drawdown_pct=1.0, thresholds=t)
check("decide_verdict: too few trades -> WATCH regardless of good numbers", v == "WATCH")

v, r = sc.decide_verdict(trade_count=20, profit_factor=0.5, max_drawdown_pct=5.0, thresholds=t)
check("decide_verdict: profit_factor below kill threshold -> KILL", v == "KILL")

v, r = sc.decide_verdict(trade_count=20, profit_factor=2.0, max_drawdown_pct=50.0, thresholds=t)
check("decide_verdict: drawdown above kill threshold -> KILL even with good profit_factor", v == "KILL")

v, r = sc.decide_verdict(trade_count=20, profit_factor=2.0, max_drawdown_pct=5.0, thresholds=t)
check("decide_verdict: good profit_factor + low drawdown -> PASS", v == "PASS")

v, r = sc.decide_verdict(trade_count=20, profit_factor=None, max_drawdown_pct=0.0, thresholds=t)
check("decide_verdict: undefined profit_factor (no losses yet) -> WATCH, not PASS",
      v == "WATCH" and "undefined" in r)

v, r = sc.decide_verdict(trade_count=20, profit_factor=1.15, max_drawdown_pct=20.0, thresholds=t)
check("decide_verdict: between kill and pass thresholds -> WATCH", v == "WATCH")

# Movable thresholds -- not hardcoded
custom = dict(t)
custom["kill_profit_factor"] = 1.2
v, r = sc.decide_verdict(trade_count=20, profit_factor=1.1, max_drawdown_pct=5.0, thresholds=custom)
check("decide_verdict: custom thresholds actually change the outcome (movable, not hardcoded)", v == "KILL")


# --- build_scorecard_for_bot / build_report ---
report_thresholds = sc.DEFAULT_THRESHOLDS
scorecard = sc.build_scorecard_for_bot("shorts", db1, days=21, thresholds=report_thresholds)
check("build_scorecard_for_bot: trade_count matches the windowed trades", scorecard["trade_count"] == 2)
check("build_scorecard_for_bot: win_rate computed correctly (1 win / 2 trades)", scorecard["win_rate"] == 0.5)
check("build_scorecard_for_bot: the LLM bots do NOT carry an isolated-experiment note", "note" not in scorecard)

# P11: listing_fade/trend_ensemble are isolated experiments with a note
check("ALL_BOTS includes both new P11 bots", "listing_fade" in sc.ALL_BOTS and "trend_ensemble" in sc.ALL_BOTS)
check("build_scorecard_for_bot: listing_fade always carries its isolated-experiment note",
      sc.build_scorecard_for_bot("listing_fade", db1, 21, report_thresholds).get("note")
      == sc.ISOLATED_EXPERIMENT_NOTES["listing_fade"])
check("build_scorecard_for_bot: trend_ensemble always carries its isolated-experiment note",
      sc.build_scorecard_for_bot("trend_ensemble", db1, 21, report_thresholds).get("note")
      == sc.ISOLATED_EXPERIMENT_NOTES["trend_ensemble"])
check("BOT_START_WALLET_USD has entries for both new P11 bots",
      sc.BOT_START_WALLET_USD.get("listing_fade") == 100.0 and sc.BOT_START_WALLET_USD.get("trend_ensemble") == 100.0)

with mock.patch.object(sc, "get_btc_return_pct", return_value=12.3):
    report = sc.build_report({"main": db1}, days=21, thresholds=report_thresholds)
check("build_report: includes btc_return_pct", report["btc_return_pct"] == 12.3)
check("build_report: every ALL_BOTS entry present even without a --db path",
      set(report["bots"].keys()) == set(sc.ALL_BOTS))
check("build_report: bot with no db path given is WATCH, not silently dropped",
      report["bots"]["moonshot"]["verdict"] == "WATCH" and "no --db path" in report["bots"]["moonshot"]["verdict_reason"])

bad_db_report = sc.build_report({"main": "/nonexistent/path.sqlite"}, days=21, thresholds=report_thresholds)
check("build_report: unreadable db -> WATCH with the real error, never a crash",
      bad_db_report["bots"]["main"]["verdict"] == "WATCH" and "could not read db" in bad_db_report["bots"]["main"]["verdict_reason"])


# --- get_btc_return_pct: reliability contract ---
class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


with mock.patch.object(sc.requests, "get", side_effect=Exception("boom")):
    check("get_btc_return_pct: fetch failure -> None, excluded, never a fabricated 0%",
          sc.get_btc_return_pct(21) is None)

with mock.patch.object(sc.requests, "get", return_value=FakeResp({"prices": [[0, 100.0], [1, 110.0]]})):
    check("get_btc_return_pct: computes real % change from first to last price",
          sc.get_btc_return_pct(21) == 10.0)

with mock.patch.object(sc.requests, "get", return_value=FakeResp({"prices": [[0, 100.0]]})):
    check("get_btc_return_pct: fewer than 2 price points -> None, not a crash",
          sc.get_btc_return_pct(21) is None)


# --- render_markdown ---
md = sc.render_markdown(report)
check("render_markdown: includes the window size", "21-day window" in md)
check("render_markdown: includes every bot as its own row", all(b in md for b in sc.ALL_BOTS))
check("render_markdown: the listing_fade note appears in the per-bot detail lines",
      sc.ISOLATED_EXPERIMENT_NOTES["listing_fade"] in md)


# --- CLI end-to-end ---
with tempfile.TemporaryDirectory() as d:
    out_json = str(Path(d) / "scorecard.json")
    out_md = str(Path(d) / "scorecard.md")
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "reports" / "bot_scorecard.py"),
         "--db", f"main={db1}", "--days", "21", "--out-json", out_json, "--out-md", out_md],
        capture_output=True, text=True,
    )
    check("CLI: exits 0", r.returncode == 0)
    check("CLI: writes valid JSON", json.loads(Path(out_json).read_text())["window_days"] == 21)
    check("CLI: writes a non-empty markdown file", len(Path(out_md).read_text()) > 0)

    r2 = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "reports" / "bot_scorecard.py"),
         "--db", "not-a-real-bot=/x/y.sqlite"],
        capture_output=True, text=True,
    )
    check("CLI: rejects an unknown bot id in --db rather than silently accepting it",
          r2.returncode == 2 and "unknown bot id" in r2.stderr)

    thresholds_path = str(Path(d) / "thresholds.json")
    # min_trades_for_verdict also lowered -- db1 only has 2 closed trades in
    # the window, and the sample-size gate runs before the profit_factor
    # check either way.
    Path(thresholds_path).write_text(json.dumps({"kill_profit_factor": 999, "min_trades_for_verdict": 1}))
    r3 = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "reports" / "bot_scorecard.py"),
         "--db", f"main={db1}", "--thresholds", thresholds_path, "--out-json", out_json],
        capture_output=True, text=True,
    )
    check("CLI: --thresholds override actually changes the verdict (KILL, since 999 > any real PF)",
          r3.returncode == 0 and json.loads(Path(out_json).read_text())["bots"]["main"]["verdict"] == "KILL")


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL P9 SCORECARD CHECKS PASSED")
