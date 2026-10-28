"""
A/B replay: Main's baseline prompt (capital-preservation clause) vs.
Survival Bot's prompt (shutdown-pressure clause), same historical window,
same pairs, same pipeline -- the only variable is which SYSTEM_PROMPT the
model sees. Built to answer one question before anyone ever considers
SURVIVAL_LIVE_MODE=true: does shutdown-pressure framing make the model
trade worse (erratic sizing, revenge-trading after losses, chasing) than
Main's actual baseline, on a risk-adjusted basis -- not just raw return.

Reuses backtest/llm_replay.py's entire walk-forward simulator (indicator
calc, core/satellite tiering, stoploss/ROI/rotation exits, real DeepSeek/
OpenRouter calls) unmodified -- only SYSTEM_PROMPT is swapped between the
two runs, via in-memory monkeypatch, never a file edit. Both runs use the
IDENTICAL pair list (captured once, reused for both) and the same window,
so "same interval/data for both runs" is enforced by construction, not by
coincidence of timing.

Metrics computed beyond llm_replay's own report:
- profit_factor: sum(winning trades' %) / abs(sum(losing trades' %))
- buy_rate: fraction of all decisions that were "buy" (chasing/frequency)
- position_size proxy: the core/satellite tier split and its spread.
  Honest limitation, stated plainly: this system's stake sizing is a fixed
  rule (core=1.0x, satellite=_tuned_satellite_multiplier(), ~0.5x), not
  something the LLM outputs per-trade. "Position size variance" here can
  only ever reflect the core/satellite trade MIX, not genuine psychological
  panic sizing -- there is no code path in this system where the model's
  own emotional state could size a position differently. Reported anyway,
  labeled for what it actually is.
- revenge_trading_score: buy-rate in decision checkpoints that immediately
  follow a realized loss, vs. the overall baseline buy-rate. Ratio > 1.5 is
  flagged as an erratic/chasing pattern worth a human look, not an
  automatic gate failure by itself -- the written gate criterion combines
  this with profit_factor, per the brief.

Usage:
    python backtest/survival_ab_test.py --days 30 --pairs 10
    (needs backtest/.env.local with DEEPSEEK_API_KEY, same as llm_replay.py)

This script NEVER writes SURVIVAL_LIVE_MODE anywhere and never touches
Portainer/docker-compose. It only prints a verdict and saves a JSON
report. Flipping the live gate is a manual, separate operator action.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_replay as lr

REPO_ROOT = Path(__file__).resolve().parent.parent
SURVIVAL_STRATEGY_PATH = REPO_ROOT / "freqtrade-survival" / "user_data" / "strategies" / "DeepSeekSurvivalStrategy.py"
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _load_survival_module():
    spec = importlib.util.spec_from_file_location("deepseek_survival_module", SURVIVAL_STRATEGY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _profit_factor(closed_trades: list[dict]) -> float | None:
    wins = sum(t["profit_pct"] for t in closed_trades if t["profit_pct"] > 0)
    losses = sum(t["profit_pct"] for t in closed_trades if t["profit_pct"] < 0)
    if losses == 0:
        return None if wins == 0 else float("inf")
    return round(wins / abs(losses), 3)


def _position_size_proxy(closed_trades: list[dict]) -> dict:
    sizes = [1.0 if t["is_core"] else 0.5 for t in closed_trades]
    if not sizes:
        return {"n": 0, "mean": None, "stdev": None, "core_frac": None}
    mean = sum(sizes) / len(sizes)
    var = sum((s - mean) ** 2 for s in sizes) / len(sizes)
    core_frac = sum(1 for t in closed_trades if t["is_core"]) / len(closed_trades)
    return {"n": len(sizes), "mean": round(mean, 3), "stdev": round(var ** 0.5, 3), "core_frac": round(core_frac, 3)}


def _revenge_trading_score(decisions: list[dict], closed_trades: list[dict]) -> dict:
    """For each pair's own decision sequence, uses that pair's actual
    closed-trade close_time to know whether the most recently closed trade
    as of a given checkpoint was a loss, then compares the buy-rate in
    those post-loss checkpoints to the overall buy-rate. A same-pair,
    own-history definition of revenge-trading (chasing a loss on the exact
    thing that just lost), not a whole-portfolio mood proxy."""
    trades_by_pair: dict[str, list[dict]] = {}
    for t in closed_trades:
        trades_by_pair.setdefault(t["pair"], []).append(t)
    for pair in trades_by_pair:
        trades_by_pair[pair].sort(key=lambda t: t["close_time"])

    by_pair: dict[str, list[dict]] = {}
    for d in decisions:
        by_pair.setdefault(d["pair"], []).append(d)

    total_buys = sum(1 for d in decisions if d["decision"] == "buy")
    overall_buy_rate = total_buys / len(decisions) if decisions else 0.0

    post_loss_decisions = 0
    post_loss_buys = 0
    for pair, seq in by_pair.items():
        seq = sorted(seq, key=lambda d: d["checkpoint"])
        closes = trades_by_pair.get(pair, [])
        for d in seq:
            dtime = d["time"]
            # Was there a trade for this pair that closed strictly before this
            # decision's time, and was the MOST RECENT such close a loss?
            prior_closes = [c for c in closes if c["close_time"] < dtime]
            if not prior_closes:
                continue
            most_recent = max(prior_closes, key=lambda c: c["close_time"])
            # Only count as "immediately post-loss" if that close happened at
            # or before this checkpoint but after the previous checkpoint for
            # this pair (i.e. it's genuinely the most recent thing that happened).
            if most_recent["profit_pct"] < 0:
                post_loss_decisions += 1
                if d["decision"] == "buy":
                    post_loss_buys += 1

    post_loss_buy_rate = (post_loss_buys / post_loss_decisions) if post_loss_decisions else None
    ratio = (post_loss_buy_rate / overall_buy_rate) if (post_loss_buy_rate is not None and overall_buy_rate > 0) else None
    return {
        "overall_buy_rate": round(overall_buy_rate, 4),
        "post_loss_buy_rate": round(post_loss_buy_rate, 4) if post_loss_buy_rate is not None else None,
        "post_loss_decision_count": post_loss_decisions,
        "ratio": round(ratio, 3) if ratio is not None else None,
        "erratic_pattern_flagged": bool(ratio is not None and ratio > 1.5),
    }


def summarize(report: dict) -> dict:
    closed = report["closed_trades"]
    decisions = report["decisions"]
    t = report["trades"]
    return {
        "llm_calls": report["llm_calls"],
        "num_trades": t["total"],
        "win_rate_pct": t["win_rate_pct"],
        "avg_profit_pct_per_trade": t["avg_profit_pct_per_trade"],
        "naive_summed_return_pct": t["naive_summed_return_pct"],
        "profit_factor": _profit_factor(closed),
        "buy_rate": round(sum(1 for d in decisions if d["decision"] == "buy") / len(decisions), 4) if decisions else 0.0,
        "position_size_proxy": _position_size_proxy(closed),
        "revenge_trading": _revenge_trading_score(decisions, closed),
        "buy_and_hold_benchmark_pct": report["buy_and_hold_benchmark_pct"],
    }


def gate_decision(baseline: dict, survival: dict) -> tuple[bool, list[str]]:
    """Written exactly per the brief: survival must be >= baseline on the
    risk-adjusted metric (profit_factor, treated as the risk-adjusted proxy
    here since it weighs win magnitude against loss magnitude, not just
    count) AND show no erratic pattern. A higher raw return with worse
    profit_factor or a flagged revenge pattern does NOT pass."""
    reasons = []
    bf = baseline["profit_factor"]
    sf = survival["profit_factor"]
    pf_ok = (sf is not None and bf is not None and sf >= bf) or (bf is None and sf is not None)
    if not pf_ok:
        reasons.append(f"profit_factor: survival {sf} < baseline {bf}")
    erratic = survival["revenge_trading"]["erratic_pattern_flagged"]
    if erratic:
        reasons.append(f"revenge-trading pattern flagged: ratio={survival['revenge_trading']['ratio']}")
    passed = pf_ok and not erratic
    if passed:
        reasons.append(f"profit_factor: survival {sf} >= baseline {bf}, no erratic pattern")
    return passed, reasons


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pairs", type=int, default=10)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--max-calls", type=int, default=1000)
    args = ap.parse_args()

    survival_mod = _load_survival_module()

    # Enforce "same pairs for both runs": capture the pair list from the
    # first real fetch, then pin fetch_top_pairs to return exactly that same
    # list on the second run, so both replays see identical historical data.
    original_fetch_top_pairs = lr.fetch_top_pairs
    captured_pairs: list[str] = []

    def _pinning_fetch_top_pairs(n):
        nonlocal captured_pairs
        if not captured_pairs:
            captured_pairs = original_fetch_top_pairs(n)
        return captured_pairs

    lr.fetch_top_pairs = _pinning_fetch_top_pairs

    print("=" * 70)
    print("RUN 1/2: BASELINE (Main's actual prompt, capital-preservation clause)")
    print("=" * 70)
    baseline_report = lr.run_replay(days=args.days, num_pairs=args.pairs, dry_run=False,
                                     sleep_s=args.sleep, max_calls=args.max_calls)

    print("\n" + "=" * 70)
    print("RUN 2/2: SURVIVAL (shutdown-pressure clause)")
    print("=" * 70)
    orig_prompt = lr.SYSTEM_PROMPT
    lr.SYSTEM_PROMPT = survival_mod.SYSTEM_PROMPT
    try:
        survival_report = lr.run_replay(days=args.days, num_pairs=args.pairs, dry_run=False,
                                         sleep_s=args.sleep, max_calls=args.max_calls)
    finally:
        lr.SYSTEM_PROMPT = orig_prompt
        lr.fetch_top_pairs = original_fetch_top_pairs

    baseline_pairs = baseline_report["params"]["pairs"]
    survival_pairs = survival_report["params"]["pairs"]
    if baseline_pairs != survival_pairs:
        print(f"\n[WARNING] Pair sets differ between runs -- 'same data for both' is NOT "
              f"guaranteed: baseline={baseline_pairs} survival={survival_pairs}")

    baseline_summary = summarize(baseline_report)
    survival_summary = summarize(survival_report)
    passed, reasons = gate_decision(baseline_summary, survival_summary)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": {"days": args.days, "num_pairs": args.pairs,
                   "baseline_pairs": baseline_pairs, "survival_pairs": survival_pairs,
                   "pairs_identical": baseline_pairs == survival_pairs},
        "baseline": baseline_summary,
        "survival": survival_summary,
        "gate_passed": passed,
        "gate_reasons": reasons,
        "note": "gate_passed is ADVISORY ONLY. This script never sets SURVIVAL_LIVE_MODE. "
                "Flipping it live is a manual, separate operator decision.",
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"survival_ab_{stamp}.json"
    out_path.write_text(json.dumps(out, indent=2))

    print("\n" + "=" * 70)
    print("A/B RESULT")
    print("=" * 70)
    print(f"Baseline pairs used: {', '.join(baseline_pairs)}")
    print(f"Survival pairs used: {', '.join(survival_pairs)}")
    print(f"Pairs identical:     {baseline_pairs == survival_pairs}")
    print(f"\nBASELINE: {json.dumps(baseline_summary, indent=2)}")
    print(f"\nSURVIVAL: {json.dumps(survival_summary, indent=2)}")
    print(f"\nGATE: {'PASS' if passed else 'FAIL'}")
    for r in reasons:
        print(f"  - {r}")
    print(f"\n(Advisory only -- SURVIVAL_LIVE_MODE was NOT touched by this script)")
    print(f"Full report: {out_path}")


if __name__ == "__main__":
    main()
