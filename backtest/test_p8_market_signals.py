"""
P8: unit tests for the new market-signal/tuning features -- Fear & Greed
Index, multi-timeframe confirmation, BTC/alt concentration-risk warning,
loss-streak cooldown gate (all duplicated per bot, tested once here
against Main's DeepSeekStrategy.py as the representative copy, plus
Shorts' own funding-rate addition), and auto_tune.py's new second
tunable parameter. Same house style as the other backtest/test_*.py
files -- self-contained, no pytest, OK/FAIL per check.

Items 3 (Coinglass liquidations) and 7 (on-chain exchange netflow) are
NOT implemented -- verified live in this same pass that neither has a
usable free/keyless tier (Coinglass: no free tier at all, cheapest paid
plan $29/mo; Glassnode/CryptoQuant: both return 401 with no key, and
Glassnode's own free tier is Studio-only, not API access). No tests for
what wasn't built.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sqlite3
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_STRATEGY_PATH = REPO_ROOT / "freqtrade" / "user_data" / "strategies" / "DeepSeekStrategy.py"
SHORTS_STRATEGY_PATH = REPO_ROOT / "freqtrade-shorts" / "user_data" / "strategies" / "DeepSeekShortsStrategy.py"
MOONSHOT_STRATEGY_PATH = REPO_ROOT / "freqtrade-moonshot" / "user_data" / "strategies" / "DeepSeekMoonshotStrategy.py"
MOONSHOT_CONFIG_PATH = REPO_ROOT / "freqtrade-moonshot" / "user_data" / "config.json"
AUTO_TUNE_PATH = REPO_ROOT / "backtest" / "auto_tune.py"

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _stub_freqtrade():
    ft = types.ModuleType("freqtrade")
    ft_p = types.ModuleType("freqtrade.persistence")
    ft_p.Trade = type("Trade", (), {"get_trades_proxy": staticmethod(lambda **kw: [])})
    ft_s = types.ModuleType("freqtrade.strategy")

    def _init(self, config):
        self.config = config

    ft_s.IStrategy = type("IStrategy", (), {"__init__": _init})
    sys.modules["freqtrade"] = ft
    sys.modules["freqtrade.persistence"] = ft_p
    sys.modules["freqtrade.strategy"] = ft_s
    return ft_p.Trade


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem + "_p8_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FakeTrade_cls = _stub_freqtrade()
sys.path.insert(0, str(MAIN_STRATEGY_PATH.parent))
main_mod = _load(MAIN_STRATEGY_PATH)
sys.path.insert(0, str(SHORTS_STRATEGY_PATH.parent))
shorts_mod = _load(SHORTS_STRATEGY_PATH)
sys.path.insert(0, str(MOONSHOT_STRATEGY_PATH.parent))
moonshot_mod = _load(MOONSHOT_STRATEGY_PATH)


class FakeTrade:
    def __init__(self, pair, close_profit, close_date):
        self.pair = pair
        self.close_profit = close_profit
        self.close_date = close_date
        self.open_date = close_date


class FakeResp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


# --- Item 1: Fear & Greed Index ---
main_mod._fng_cache["text"] = None
main_mod._fng_cache["fetched_at"] = 0
with mock.patch.object(main_mod.requests, "get", side_effect=Exception("boom")):
    check("get_fear_greed_index: failure -> None (excluded), not a fake neutral",
          main_mod.get_fear_greed_index() is None)

main_mod._fng_cache["text"] = None
main_mod._fng_cache["fetched_at"] = 0
with mock.patch.object(main_mod.requests, "get",
                        return_value=FakeResp({"data": [{"value": "17", "value_classification": "Extreme Fear"}]})):
    result = main_mod.get_fear_greed_index()
    check("get_fear_greed_index: success -> formatted string with value+classification",
          result == "Fear & Greed Index: 17 (Extreme Fear)")

main_mod._fng_cache["text"] = None
main_mod._fng_cache["fetched_at"] = 0
with mock.patch.object(main_mod.requests, "get", return_value=FakeResp({"data": []})):
    check("get_fear_greed_index: unexpected empty response -> None, not a crash",
          main_mod.get_fear_greed_index() is None)


# --- Item 4: multi-timeframe confirmation ---
strat = main_mod.DeepSeekStrategy(config={"dry_run": True})

def _kline_resp(closes):
    rows = [[0, 0, 0, 0, str(c)] for c in reversed(closes)]  # index 4 = close, reversed like real API
    return FakeResp({"result": {"list": rows}})

with mock.patch.object(main_mod.requests, "get", side_effect=Exception("boom")):
    check("_mtf_confirmation: fetch failure -> None, excluded", strat._mtf_confirmation("BTC/USDT") is None)

rising_closes = [100 + i * 0.5 for i in range(55)]  # trending up -> last close above its own SMA50
with mock.patch.object(main_mod.requests, "get", return_value=_kline_resp(rising_closes)):
    check("_mtf_confirmation: uptrend -> reports price ABOVE its 4h SMA50",
          strat._mtf_confirmation("BTC/USDT") == "4h trend: price above its own 4h SMA50")

falling_closes = [200 - i * 0.5 for i in range(55)]
with mock.patch.object(main_mod.requests, "get", return_value=_kline_resp(falling_closes)):
    check("_mtf_confirmation: downtrend -> reports price BELOW its 4h SMA50",
          strat._mtf_confirmation("BTC/USDT") == "4h trend: price below its own 4h SMA50")

with mock.patch.object(main_mod.requests, "get", return_value=_kline_resp([100] * 10)):
    check("_mtf_confirmation: too few candles -> None, excluded (not a guessed default)",
          strat._mtf_confirmation("BTC/USDT") is None)


# --- Item 6: BTC/alt concentration-risk warning ---
with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=[]):
    check("_concentration_risk_warning: no open trades -> None", strat._concentration_risk_warning("ETH/USDT") is None)

open_btc = [FakeTrade("BTC/USDT", 0.01, datetime.now(timezone.utc))]
# Two closely-tracking series -> correlation near 1.0
this_series = [100 + i for i in range(35)]
other_series = [50 + i for i in range(35)]  # perfectly linearly correlated with this_series
with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=open_btc):
    def _get_side_effect(url, params=None, timeout=None):
        return _kline_resp(this_series if params["symbol"] == "ETHUSDT" else other_series)
    with mock.patch.object(main_mod.requests, "get", side_effect=_get_side_effect):
        warning = strat._concentration_risk_warning("ETH/USDT")
        check("_concentration_risk_warning: high correlation with open BTC position -> warns",
              warning is not None and "concentration risk" in warning and "BTC/USDT" in warning)

uncorrelated_other = [50, 80, 40, 90, 30, 100, 20] * 5
with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=open_btc):
    def _get_side_effect2(url, params=None, timeout=None):
        return _kline_resp(this_series if params["symbol"] == "ETHUSDT" else uncorrelated_other)
    with mock.patch.object(main_mod.requests, "get", side_effect=_get_side_effect2):
        check("_concentration_risk_warning: low correlation -> None, no false warning",
              strat._concentration_risk_warning("ETH/USDT") is None)

with mock.patch.object(main_mod.Trade, "get_trades_proxy", side_effect=Exception("db down")):
    check("_concentration_risk_warning: can't read open trades -> None, excluded, never blocks",
          strat._concentration_risk_warning("ETH/USDT") is None)


# --- Item 5: loss-streak cooldown gate (strategy side) ---
check("_tuned_loss_streak_cooldown_hours: default is 0.0 (disabled) with no auto_tune.json",
      strat._tuned_loss_streak_cooldown_hours() == 0.0)
check("_in_loss_streak_cooldown: disabled (0h) -> always False", strat._in_loss_streak_cooldown() is False)

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "auto_tune.json"
    p.write_text(json.dumps({"loss_streak_cooldown_hours": 12}))
    with mock.patch.dict(main_mod.os.environ, {"AUTO_TUNE_JSON": str(p)}):
        check("_tuned_loss_streak_cooldown_hours: reads a tuned value", strat._tuned_loss_streak_cooldown_hours() == 12.0)

        now = datetime.now(timezone.utc)
        recent_losses = [FakeTrade("X/USDT", -1, now - timedelta(hours=1)) for _ in range(3)]
        with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=recent_losses):
            check("_in_loss_streak_cooldown: 3 recent losses inside 12h window -> True",
                  strat._in_loss_streak_cooldown() is True)

        old_losses = [FakeTrade("X/USDT", -1, now - timedelta(hours=20)) for _ in range(3)]
        with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=old_losses):
            check("_in_loss_streak_cooldown: 3 losses outside the window -> False",
                  strat._in_loss_streak_cooldown() is False)

        mixed = [FakeTrade("X/USDT", -1, now - timedelta(hours=1)),
                 FakeTrade("X/USDT", 2, now - timedelta(hours=2)),
                 FakeTrade("X/USDT", -1, now - timedelta(hours=3))]
        with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=mixed):
            check("_in_loss_streak_cooldown: one win breaks the streak -> False",
                  strat._in_loss_streak_cooldown() is False)

with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "auto_tune.json"
    p.write_text(json.dumps({"loss_streak_cooldown_hours": 12}))
    with mock.patch.dict(main_mod.os.environ, {"AUTO_TUNE_JSON": str(p)}):
        with mock.patch.object(main_mod.Trade, "get_trades_proxy", side_effect=Exception("db down")):
            check("_in_loss_streak_cooldown: fails open (False) if trade history unreadable, even when tuned > 0",
                  strat._in_loss_streak_cooldown() is False)


# --- confirm_trade_entry actually rejects during cooldown (Main) ---
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "auto_tune.json"
    p.write_text(json.dumps({"loss_streak_cooldown_hours": 12}))
    with mock.patch.dict(main_mod.os.environ, {"AUTO_TUNE_JSON": str(p)}):
        now = datetime.now(timezone.utc)
        recent_losses = [FakeTrade("X/USDT", -1, now - timedelta(hours=1)) for _ in range(3)]
        with mock.patch.object(main_mod.Trade, "get_trades_proxy", return_value=recent_losses):
            allowed = strat.confirm_trade_entry("BTC/USDT", "market", 1.0, 1.0, "GTC", now, None, "long")
            check("confirm_trade_entry: rejects a new entry during an active loss-streak cooldown",
                  allowed is False)


# --- Item 2: Shorts-only funding-rate prompt line ---
shorts_strat = shorts_mod.DeepSeekShortsStrategy(config={"dry_run": True})
with mock.patch.object(shorts_mod.requests, "get",
                        return_value=FakeResp({"result": {"list": [{"fundingRate": "0.0001"}]}})):
    line = shorts_strat._get_funding_rate_line("BTC/USDT:USDT")
    check("Shorts._get_funding_rate_line: formats the real rate as %/8h",
          line == "Current funding rate: 0.0100%/8h")

with mock.patch.object(shorts_mod.requests, "get", side_effect=Exception("boom")):
    check("Shorts._get_funding_rate_line: fetch failure -> None, excluded",
          shorts_strat._get_funding_rate_line("BTC/USDT:USDT") is None)

with mock.patch.object(shorts_mod.requests, "get", return_value=FakeResp({"result": {"list": []}})):
    check("Shorts._get_funding_rate_line: empty result -> None, not a guessed default",
          shorts_strat._get_funding_rate_line("BTC/USDT:USDT") is None)


# --- auto_tune.py: item 5's offline tuner ---
auto_tune = _load(AUTO_TUNE_PATH)

check("current_loss_streak: 3 losses then a win -> streak of 3",
      auto_tune.current_loss_streak([-1, -2, -3, 5, -1]) == 3)
check("current_loss_streak: win first -> streak of 0",
      auto_tune.current_loss_streak([5, -1, -2]) == 0)
check("current_loss_streak: empty -> 0", auto_tune.current_loss_streak([]) == 0)

new_h, reason = auto_tune.decide_cooldown(0.0, streak=3)
check("decide_cooldown: streak >= LOSS_STREAK_N -> increases by COOLDOWN_STEP_HOURS", new_h == 4.0)
new_h2, _ = auto_tune.decide_cooldown(4.0, streak=1)
check("decide_cooldown: streak < LOSS_STREAK_N -> decreases toward 0 (disabled)", new_h2 == 0.0)
new_h3, _ = auto_tune.decide_cooldown(46.0, streak=5)
check("decide_cooldown: clamps at MAX_COOLDOWN_HOURS (48)", new_h3 == 48.0)

# --- P10: per-bot LOSS_STREAK_N / MAX_COOLDOWN_HOURS ---
check("loss_streak_n_for: moonshot -> 6 (default 3 overridden)", auto_tune.loss_streak_n_for("moonshot") == 6)
check("loss_streak_n_for: main/shorts fall through to the global default (3)",
      auto_tune.loss_streak_n_for("main") == 3
      and auto_tune.loss_streak_n_for("shorts") == 3)
check("max_cooldown_hours_for: moonshot -> 24.0 (default 48.0 overridden)", auto_tune.max_cooldown_hours_for("moonshot") == 24.0)
check("max_cooldown_hours_for: main/shorts fall through to the global default (48.0)",
      auto_tune.max_cooldown_hours_for("main") == 48.0
      and auto_tune.max_cooldown_hours_for("shorts") == 48.0)

new_h4, reason4 = auto_tune.decide_cooldown(
    0.0, streak=3, loss_streak_n=auto_tune.loss_streak_n_for("moonshot"), max_cooldown_hours=auto_tune.max_cooldown_hours_for("moonshot"))
check("decide_cooldown: moonshot with a 3-loss streak does NOT raise the cooldown (needs 6, not 3)", new_h4 == 0.0)
new_h5, _ = auto_tune.decide_cooldown(
    0.0, streak=6, loss_streak_n=auto_tune.loss_streak_n_for("moonshot"), max_cooldown_hours=auto_tune.max_cooldown_hours_for("moonshot"))
check("decide_cooldown: moonshot with a 6-loss streak DOES raise the cooldown", new_h5 == 4.0)
new_h6, _ = auto_tune.decide_cooldown(
    22.0, streak=6, loss_streak_n=auto_tune.loss_streak_n_for("moonshot"), max_cooldown_hours=auto_tune.max_cooldown_hours_for("moonshot"))
check("decide_cooldown: moonshot clamps at its own 24h cap, not the global 48h", new_h6 == 24.0)

with tempfile.TemporaryDirectory() as d:
    bot_dir = Path(d) / "moonshot"
    bot_dir.mkdir(parents=True)
    (bot_dir / "auto_tune.json").write_text(json.dumps({"loss_streak_cooldown_hours": 40.0}))
    check("read_current_cooldown: moonshot's stale 40h value clamped down to its own 24h cap",
          auto_tune.read_current_cooldown(str(d), "moonshot") == 24.0)

with tempfile.TemporaryDirectory() as d:
    db = str(Path(d) / "test.sqlite")
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE trades (enter_tag TEXT, close_profit REAL, is_open INTEGER, close_date TEXT)")
    rows = [("core", 0.05, 0, f"2026-01-{i+1:02d}") for i in range(12)]
    rows += [("sat", -0.02, 0, f"2026-01-{i+1:02d}") for i in range(12)]
    rows += [(None, -0.01, 0, f"2026-02-0{i}") for i in (1, 2, 3)]
    con.executemany("INSERT INTO trades VALUES (?,?,?,?)", rows)
    con.commit()
    con.close()

    state_dir = str(Path(d) / "state")
    log_path = str(Path(d) / "log.jsonl")
    r1 = subprocess.run(
        [sys.executable, str(AUTO_TUNE_PATH), "--db", db, "--bot", "shorts", "--apply",
         "--state-dir", state_dir, "--log", log_path, "--sample", "50"],
        capture_output=True, text=True,
    )
    check("auto_tune.py --apply: exits 0", r1.returncode == 0)
    written = json.loads((Path(state_dir) / "shorts" / "auto_tune.json").read_text())
    check("auto_tune.py --apply: writes BOTH parameters into the same auto_tune.json, neither clobbers the other",
          "satellite_stake_multiplier" in written and "loss_streak_cooldown_hours" in written)
    check("auto_tune.py --apply: satellite multiplier moved down (satellite lost every trade here)",
          written["satellite_stake_multiplier"] == 0.45)
    check("auto_tune.py --apply: cooldown increased (3-loss streak >= LOSS_STREAK_N)",
          written["loss_streak_cooldown_hours"] == 4.0)

    log_lines = [json.loads(l) for l in Path(log_path).read_text().splitlines()]
    check("auto_tune.py --apply: writes exactly 2 log lines, one per param",
          len(log_lines) == 2 and {l["param"] for l in log_lines} == {"satellite_stake_multiplier", "loss_streak_cooldown_hours"})

    r2 = subprocess.run(
        [sys.executable, str(AUTO_TUNE_PATH), "--db", db, "--bot", "shorts", "--apply",
         "--state-dir", state_dir, "--log", log_path, "--sample", "50"],
        capture_output=True, text=True,
    )
    check("auto_tune.py: immediate rerun skips BOTH params (24h per-parameter cooldown)",
          "skipped: changed less than 24h ago" in r2.stdout and r2.stdout.count("skipped: changed less than 24h ago") == 2)

    parsed_moonshot = auto_tune._parse_args(["--db", db, "--bot", "moonshot"])
    check("auto_tune.py: --bot accepts moonshot (P8 widened choices)", parsed_moonshot.bot == "moonshot")

    # P10: the SAME trailing 3-loss streak that raised shorts' cooldown (needs N=3) must NOT raise moonshot's (needs N=6) -- real end-to-end
    # proof the per-bot override actually reaches the CLI, not just the function calls tested directly above.
    state_dir_ms = str(Path(d) / "state_ms")
    log_path_ms = str(Path(d) / "log_ms.jsonl")
    r_ms = subprocess.run(
        [sys.executable, str(AUTO_TUNE_PATH), "--db", db, "--bot", "moonshot", "--apply",
         "--state-dir", state_dir_ms, "--log", log_path_ms, "--sample", "50"],
        capture_output=True, text=True,
    )
    check("auto_tune.py --apply --bot moonshot: exits 0", r_ms.returncode == 0)
    ms_written = json.loads((Path(state_dir_ms) / "moonshot" / "auto_tune.json").read_text())
    check("auto_tune.py --apply --bot moonshot: a 3-loss streak does NOT raise the cooldown (moonshot needs 6)",
          ms_written.get("loss_streak_cooldown_hours", 0.0) == 0.0)

    # A genuine 6-loss trailing streak (moonshot's own threshold) DOES raise it -- but it stays clamped at
    # its own 24h cap, not the global 48h, even after repeated raises.
    db5 = str(Path(d) / "test5.sqlite")
    con5 = sqlite3.connect(db5)
    con5.execute("CREATE TABLE trades (enter_tag TEXT, close_profit REAL, is_open INTEGER, close_date TEXT)")
    rows5 = [("core", 0.05, 0, f"2026-01-{i+1:02d}") for i in range(12)]
    rows5 += [("sat", -0.02, 0, f"2026-01-{i+1:02d}") for i in range(12)]
    rows5 += [(None, -0.01, 0, f"2026-03-{i:02d}") for i in range(1, 7)]  # 6 trailing losses
    con5.executemany("INSERT INTO trades VALUES (?,?,?,?)", rows5)
    con5.commit()
    con5.close()

    state_dir5 = str(Path(d) / "state5")
    log_path5 = str(Path(d) / "log5.jsonl")
    # Pre-seed moonshot near its own cap to prove the CLAMP, not just the step.
    for bot_id in ("moonshot",):
        bot_dir = Path(state_dir5) / bot_id
        bot_dir.mkdir(parents=True)
        (bot_dir / "auto_tune.json").write_text(json.dumps({"loss_streak_cooldown_hours": 22.0}))
    r_moonshot5 = subprocess.run(
        [sys.executable, str(AUTO_TUNE_PATH), "--db", db5, "--bot", "moonshot", "--apply",
         "--state-dir", state_dir5, "--log", log_path5, "--sample", "50"],
        capture_output=True, text=True,
    )
    moonshot5_written = json.loads((Path(state_dir5) / "moonshot" / "auto_tune.json").read_text())
    check("auto_tune.py --apply --bot moonshot: a 6-loss streak (>= moonshot's N=6) raises the cooldown, clamped at 24h",
          moonshot5_written["loss_streak_cooldown_hours"] == 24.0)

# last_change_ts backward-compat: a log line with no "param" field is treated as satellite
with tempfile.TemporaryDirectory() as d:
    log_path = Path(d) / "old_log.jsonl"
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()
    log_path.write_text(json.dumps({"bot": "main", "changed": True, "ts_epoch": old_ts}) + "\n")
    check("last_change_ts: pre-P8 log line (no 'param' field) counts as satellite_stake_multiplier",
          auto_tune.last_change_ts(str(log_path), "main", auto_tune.PARAM_SATELLITE) == old_ts)
    check("last_change_ts: same pre-P8 line does NOT count toward the new cooldown param",
          auto_tune.last_change_ts(str(log_path), "main", auto_tune.PARAM_COOLDOWN) is None)


# --- P10: Moonshot prompt emphasis and config ---
moonshot_prompt = moonshot_mod.SYSTEM_PROMPT
check("Moonshot SYSTEM_PROMPT: emphasizes '<10 days old + double-digit 24h gain' as the strongest setup",
      "under 10 days old" in moonshot_prompt and "double-digit percentage gain" in moonshot_prompt)
check("Moonshot SYSTEM_PROMPT: still keeps the existing 10-90 day / 'not established' guidance (not replaced)",
      "10-90 day old memecoin" in moonshot_prompt)

moonshot_config = json.loads(MOONSHOT_CONFIG_PATH.read_text())
check("Moonshot config.json: max_open_trades == 10 (P12-fix-2 revert, was briefly 20, 6 from P10, 3 originally)", moonshot_config["max_open_trades"] == 10)


if failures:
    print(f"\n{len(failures)} CHECK(S) FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)

print("\nALL P8 MARKET-SIGNAL/TUNING CHECKS PASSED")
