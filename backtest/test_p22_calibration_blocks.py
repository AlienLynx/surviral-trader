"""
P22 follow-up -- why a replay-only entry has no live twin: entry blockers read from the live log (strategy veto, exchange minimum order size, Freqtrade pair lock),
the outlier-trimmed row and the thin-pair flag of the calibration report. Pure-Python fixtures; nothing needs Docker, the network or a key.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.update({"REPLAY_DIR": tempfile.mkdtemp(), "DATA_DIR": tempfile.mkdtemp(), "REPLAY_STRATEGIES_DIR": str(REPO)})
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import calibration as cal, data as rd, live_data as ld  # noqa: E402

UTC = timezone.utc
LOG = """2026-10-01 04:00:11,415 - DeepSeekSurvivalShortsStrategy - INFO - DeepSeek said buy (long) for BTC/USDT:USDT -- BTC grinding up
2026-10-01 04:00:27,645 - freqtrade.wallets - WARNING - Minimum stake amount > available balance. 87.91902 > 73.39447628
2026-10-01 04:00:29,495 - freqtrade.wallets - WARNING - Minimum stake amount > available balance. 87.91902 > 61.24799174
2026-10-01 04:00:34,495 - freqtrade.wallets - WARNING - Minimum stake amount > available balance. 87.95 > 61.24799174
2026-10-03 14:00:23,753 - freqtrade.wallets - INFO - Stake amount for pair ETH/USDT:USDT is too small (12.410769855 < 28.14714), adjusting to 28.14714.
2026-10-03 14:00:23,754 - freqtrade.wallets - INFO - Adjusted stake amount for pair ETH/USDT:USDT is more than 30% bigger than the desired stake amount of (12.41 * 1.3 = 16.13) < 28.14714), ignoring trade.
2026-10-03 14:00:24,774 - freqtrade.wallets - INFO - Stake amount for pair ETH/USDT:USDT is too small (12.410769855 < 28.14714), adjusting to 28.14714.
2026-10-04 16:41:59,447 - freqtrade.freqtradebot - INFO - Pair BTW/USDT:USDT short is locked until 2026-10-04 17:00:00 due to Auto lock.
2026-10-05 05:00:11,001 - DeepSeekSurvivalShortsStrategy - INFO - confirm_trade_entry: rejecting QNT/USDT:USDT -- funding -0.0123%/8h < -0.0050% threshold
2026-10-05 05:00:16,001 - DeepSeekSurvivalShortsStrategy - INFO - confirm_trade_entry: rejecting QNT/USDT:USDT -- funding -0.0123%/8h < -0.0050% threshold
2026-10-05 05:00:21,001 - DeepSeekSurvivalShortsStrategy - INFO - confirm_trade_entry: rejecting QNT/USDT:USDT -- funding -0.0123%/8h < -0.0050% threshold
""".splitlines()
p = ld.parse_log(LOG)
B = p["entry_blocks"]
kinds = [b["kind"] for b in B]
check("LOG: the repeated lines of one loop iteration collapse to ONE record per kind, pair and hour (3 vetoes -> 1, 3 identical minimum-stake warnings -> 1, 2 too-small lines -> 1)",
      kinds.count("veto") == 1 and kinds.count("min_order") == 1 and kinds.count("locked") == 1 and kinds.count("min_balance") == 1)
mo = next(b for b in B if b["kind"] == "min_order")
check("LOG: a named 'too small' line gives the pair and the exchange minimum stake", mo["pair"] == "ETH/USDT:USDT" and abs(mo["min"] - 28.14714) < 1e-9 and abs(mo["wanted"] - 12.410769855) < 1e-9)
mb = next(b for b in B if b["kind"] == "min_balance")
check("LOG: an unnamed 'minimum stake > available balance' keeps the figure and the free balance (no pair)", mb["pair"] is None and abs(mb["min"] - 87.91902) < 1e-6 and abs(mb["avail"] - 73.39447628) < 1e-6)
lk = next(b for b in B if b["kind"] == "locked")
check("LOG: Freqtrade's pair lock gives the pair, the unlock time and the reason", lk["pair"] == "BTW/USDT:USDT" and lk["until"] == "2026-10-04 17:00:00" and "Auto lock" in lk["why"])
vt = next(b for b in B if b["kind"] == "veto")
check("LOG: the strategy's confirm_trade_entry veto gives the pair and the funding reason", vt["pair"] == "QNT/USDT:USDT" and "funding" in vt["why"])
check("LOG: decisions are still parsed from the same lines (the blocker branch does not eat them)", len(p["decisions"]) == 1 and p["decisions"][0]["pair"] == "BTC/USDT:USDT")
check("LOG: a log without any of these lines gives an empty list (old snapshots without the field are tolerated)", ld.parse_log(["2026-10-01 04:00:11,415 - x - INFO - hello"])["entry_blocks"] == [])

# ------------------------------------------------------------------ labels
H = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)
mm = {"BTC/USDT:USDT": [(H("2026-10-01T18:00:00"), 88.97)], "ETH/USDT:USDT": [(H("2026-10-03T14:00:00"), 28.15)]}
lab = lambda pair, h, blocks=B, m=mm: cal.entry_block_label(pair, H(h), blocks, m)
check("LABEL: a funding veto in the entry's hour -> live_strategy_veto", lab("QNT/USDT:USDT", "2026-10-05T05:00:00") == "live_strategy_veto")
check("LABEL: Freqtrade's pair lock -> live_pair_locked", lab("BTW/USDT:USDT", "2026-10-04T16:00:00") == "live_pair_locked")
check("LABEL: the exchange minimum order size named for the pair -> live_min_order_size", lab("ETH/USDT:USDT", "2026-10-03T14:00:00") == "live_min_order_size")
check("LABEL: an unnamed minimum-stake-above-balance figure that matches the pair's known minimum (87.9 vs 88.97, the price moved) -> live_min_order_size", lab("BTC/USDT:USDT", "2026-10-01T04:00:00") == "live_min_order_size")
check("LABEL: the same unnamed figure does NOT match a pair whose minimum is far away (a different pair, e.g. 12.3)", lab("SUI/USDT:USDT", "2026-10-01T04:00:00", B, {"SUI/USDT:USDT": [(H("2026-10-01T04:00:00"), 12.3)]}) is None)
check("LABEL: a pair with NO known minimum but a minimum-stake warning in that hour is `probable`, not proven", lab("SUI/USDT:USDT", "2026-10-01T04:00:00") == "live_min_order_size_probable")
check("LABEL: nothing in the log -> None (the entry stays live_no_entry_unexplained)", lab("ZEC/USDT:USDT", "2026-10-02T10:00:00") is None)
check("LABEL: only the hour of the entry counts (a veto 3 hours later explains nothing)", lab("QNT/USDT:USDT", "2026-10-05T01:00:00") is None)
only = [{"open_date": "2026-10-05T05:00:20Z", "pair": "QNT/USDT:USDT", "side": "short"}, {"open_date": "2026-10-02T10:00:20Z", "pair": "ZEC/USDT:USDT", "side": "short"}]
cs = cal.causes([], only, [], [], 8, [], B, mm)
check("CAUSES: replay-only entries get the new labels, the unexplained rest keeps its label; a blocker never overrides an open pair / full slots / a timed-out order",
      cs["only_replay"] == {"live_strategy_veto": 1, "live_no_entry_unexplained": 1})
twin = {"open_date": "2026-10-05T04:00:00Z", "close_date": None, "pair": "QNT/USDT:USDT", "is_open": True, "side": "short", "id": 1}
check("CAUSES: ... the pair already open in live wins over a veto record", cal.causes([], only[:1], [], [twin], 8, [], B, mm)["only_replay"] == {"live_pair_already_open": 1})
check("CAUSES: old callers without blocks still work", cal.causes([], only, [], [], 8)["only_replay"] == {"live_no_entry_unexplained": 2})
check("MIN STAKES: collected from the named lines of every bot", cal.min_stakes_of({"a": {"entry_blocks": B}, "b": {}}) == {"ETH/USDT:USDT": [(H("2026-10-03T14:00:23"), 28.14714)]})

# ------------------------------------------------------------------ trimmed row
check("TRIMMED MEAN: 5% from each tail (20 values: the extremes go)", abs(cal.trimmed_mean([100.0] + [1.0] * 18 + [-100.0]) - 1.0) < 1e-12 and cal.trimmed_mean([]) is None)
diffs = [0.1] * 38 + [9.0, -9.0]
lr = [{"profit_ratio": 0.01} for _ in range(38)] + [{"profit_ratio": 0.5}, {"profit_ratio": -0.5}]
rr_ = [{"profit_ratio": 0.012} for _ in range(38)] + [{"profit_ratio": 0.0}, {"profit_ratio": -0.1}]
tb = cal.trimmed_block(lr, rr_, diffs)
check("TRIMMED: the 5% (ceil) matched pairs with the largest |difference| are dropped (2 of 40), the median and mean abs difference are recomputed without them",
      tb["matched_pairs_dropped"] == 2 and tb["matched_pairs_kept"] == 38 and tb["return_diff_pp"]["median_abs"] == 0.1 and tb["return_diff_pp"]["mean_abs"] == 0.1 and tb["accepted_median_abs_return_diff"] is True)
check("TRIMMED: the trimmed mean per trade of each side (one huge stop on one side no longer moves it)", abs(tb["mean_trade_pct"]["live"] - 1.0) < 1e-9 and abs(tb["mean_trade_pct"]["replay"] - 1.2) < 1e-9)
check("TRIMMED: no matched pairs -> an empty, honest block", cal.trimmed_block([], [], [])["matched_pairs_kept"] == 0 and cal.trimmed_block([], [], [])["accepted_median_abs_return_diff"] is None)

# ------------------------------------------------------------------ thin pairs
store = rd.SeriesStore()
store.spreads[("linear", "BTCUSDT")] = 0.00005
store.spreads[("linear", "BTWUSDT")] = 0.0031
store.spreads[("linear", "ZECUSDT")] = 0.0004
entry = {"trades": [{"pair": "BTC/USDT:USDT"}, {"pair": "BTW/USDT:USDT"}], "order_cancels": [{"pair": "ZEC/USDT:USDT", "kind": "entry_cancel", "ts": "x"}] * 2 + [{"pair": "BTC/USDT:USDT", "kind": "entry_cancel", "ts": "x"}],
         "decisions": [{"pair": "ZEC/USDT:USDT"}]}
th = cal.thin_pairs(entry, store, "shorts")
check("THIN: a wide spread (0.31%) or two cancelled orders flags a pair; a major with a tiny spread and one cancel is not flagged",
      set(th) == {"BTW/USDT:USDT", "ZEC/USDT:USDT"} and "spread" in th["BTW/USDT:USDT"]["why"] and "cancelled" in th["ZEC/USDT:USDT"]["why"] and th["BTW/USDT:USDT"]["spread_pct"] == 0.31)

# ------------------------------------------------------------------ the report shows both
met = cal.metrics([], [], [], [], [], 8, set())
rep = {"run_id": "t", "bots": ["shorts"], "coverage": {"shorts": {"first_full_hour": "a", "last_hour": "b", "n_decisions": 1, "hours_with_decisions": 1, "hours_in_span": 1, "n_pairs": 1, "restarts": [], "outage_hours": []}},
       "variants": {"live_like": {"shorts": {"full": {**met, "trimmed": tb}, "thin_pairs": {"pairs": th, "live_entries_on_thin_pairs": 1, "replay_entries_on_thin_pairs": 2, "metrics_excluding_thin_pairs": met}}}},
       "ablation": {}, "limits": cal.LIMITS}
md = cal.to_markdown(rep)
check("REPORT: the markdown has the outlier-trimmed table and the thin-pair table, and the limits explain the new labels and the missing allocator veto",
      "outlier-trimmed" in md and "thin pairs flagged" in md and "BTW/USDT:USDT" in md and "live_strategy_veto" in md and "no evidence was found for an allocator veto" in md)
check("REPORT: metrics() always carries the trimmed block", "trimmed" in met and met["trimmed"]["share"] == 0.05)

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
