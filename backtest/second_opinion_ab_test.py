"""
P5 Phase 2: baseline (DeepSeek only) vs. baseline+ensemble-vote, same
historical window and pinned pair list, using llm_replay.py's simulator --
same protocol as backtest/survival_ab_test.py (and P4's verification),
reusing its metric functions (profit_factor, revenge-trading score, gate
logic) rather than re-implementing them.

**This cannot be run for real from a dev sandbox with no XAI_API_KEY /
NVIDIA_API_KEY / LLM7_API_KEY / GEMINI_API_KEY -- none of the four new P5
providers were available in the session that wrote this file.** With none
configured, `_second_opinion.gather_votes()` returns zero votes for every
borderline decision (every provider call returns None immediately, near-
zero cost), so the "ensemble" run degenerates to fail-open == baseline on
every single decision, and the gate will trivially "pass" by tying, not by
demonstrating anything. Do not read a passing gate from a run with no
provider keys configured as real evidence the ensemble helps -- check the
printed vote counts (`votes_gathered_total` in the output) first; zero
means the run proved nothing.

Usage (once real keys exist in backtest/.env.local):
    python backtest/second_opinion_ab_test.py --days 30 --pairs 10 --bot main
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm_replay as lr  # noqa: E402
from survival_ab_test import summarize, gate_decision  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STRATEGY_DIRS = {
    "main": REPO_ROOT / "freqtrade" / "user_data" / "strategies",
    "shorts": REPO_ROOT / "freqtrade-shorts" / "user_data" / "strategies",
    "moonshot": REPO_ROOT / "freqtrade-moonshot" / "user_data" / "strategies",
    "dip": REPO_ROOT / "freqtrade-dip" / "user_data" / "strategies",
}
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _load_second_opinion(bot: str):
    strat_dir = STRATEGY_DIRS[bot]
    sys.path.insert(0, str(strat_dir))
    spec = importlib.util.spec_from_file_location(f"second_opinion_{bot}", strat_dir / "_second_opinion.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_ensemble_ask_llm(so_mod, vote_counter: dict):
    """Wraps llm_replay's real ask_llm: gets DeepSeek's own decision first
    (unchanged), then -- ONLY on a borderline decision -- runs the same
    ensemble vote_shadow would compute live, and returns the ENSEMBLE's
    verdict instead of DeepSeek's raw one. This simulates 'what if the
    live decision used the ensemble outcome', which is exactly what Phase
    2 needs to compare against baseline. Never used in Phase 1 (shadow
    mode) itself -- that never overrides anything."""
    original_ask_llm = lr.ask_llm

    def wrapped(system, user, dry_run, fake_decision, is_core=False):
        decision, reason, malformed = original_ask_llm(system, user, dry_run, fake_decision, is_core=is_core)
        if dry_run or not so_mod.is_borderline(decision):
            return decision, reason, malformed
        pool = so_mod.pick_pool(is_core, True)
        votes = so_mod.gather_votes(pool, system, user, ("buy", "hold"))
        vote_counter["total_votes_gathered"] += len(votes)
        vote_counter["borderline_decisions"] += 1
        verdict, explanation = so_mod.vote_result(decision, votes)
        return verdict, f"{reason} | ensemble: {explanation}", malformed

    return wrapped


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--pairs", type=int, default=10)
    ap.add_argument("--bot", default="main", choices=["main"],
                     help="Only 'main' is wired up -- llm_replay.py itself only supports Main's pipeline (see its own docstring); Shorts/Moonshot/Dip would each need their own replay harness first, same limitation P4 and the Survival Bot A/B test already documented.")
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--max-calls", type=int, default=1000)
    args = ap.parse_args()

    so_mod = _load_second_opinion(args.bot)

    original_fetch_top_pairs = lr.fetch_top_pairs
    captured_pairs: list[str] = []

    def _pinning_fetch_top_pairs(n):
        nonlocal captured_pairs
        if not captured_pairs:
            captured_pairs = original_fetch_top_pairs(n)
        return captured_pairs

    lr.fetch_top_pairs = _pinning_fetch_top_pairs

    print("=" * 70)
    print("RUN 1/2: BASELINE (DeepSeek only, ensemble never consulted)")
    print("=" * 70)
    baseline_report = lr.run_replay(days=args.days, num_pairs=args.pairs, dry_run=False,
                                     sleep_s=args.sleep, max_calls=args.max_calls)

    print("\n" + "=" * 70)
    print("RUN 2/2: BASELINE + ENSEMBLE VOTE (on every borderline decision)")
    print("=" * 70)
    vote_counter = {"total_votes_gathered": 0, "borderline_decisions": 0}
    original_ask_llm = lr.ask_llm
    lr.ask_llm = make_ensemble_ask_llm(so_mod, vote_counter)
    try:
        ensemble_report = lr.run_replay(days=args.days, num_pairs=args.pairs, dry_run=False,
                                         sleep_s=args.sleep, max_calls=args.max_calls)
    finally:
        lr.ask_llm = original_ask_llm
        lr.fetch_top_pairs = original_fetch_top_pairs

    if vote_counter["borderline_decisions"] and vote_counter["total_votes_gathered"] == 0:
        print("\n[WARNING] Every borderline decision failed open (0 real votes gathered across "
              "all of them) -- this almost certainly means no XAI_API_KEY/NVIDIA_API_KEY/"
              "LLM7_API_KEY/GEMINI_API_KEY are configured. The 'ensemble' run below is "
              "identical to baseline by construction, not because the ensemble was tested "
              "and found equivalent. Do not treat the gate result below as evidence.")

    baseline_summary = summarize(baseline_report)
    ensemble_summary = summarize(ensemble_report)
    passed, reasons = gate_decision(baseline_summary, ensemble_summary)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bot": args.bot,
        "params": {"days": args.days, "num_pairs": args.pairs,
                   "baseline_pairs": baseline_report["params"]["pairs"],
                   "ensemble_pairs": ensemble_report["params"]["pairs"]},
        "vote_stats": vote_counter,
        "baseline": baseline_summary,
        "ensemble": ensemble_summary,
        "gate_passed": passed,
        "gate_reasons": reasons,
        "note": "gate_passed is ADVISORY ONLY and MEANINGLESS if vote_stats.total_votes_gathered "
                "is 0 -- see the WARNING printed above if that happened. This script never "
                "writes any FREQTRADE*_SECOND_OPINION_ENABLED flag itself.",
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"second_opinion_ab_{stamp}.json"
    out_path.write_text(json.dumps(out, indent=2))

    print(f"\nBorderline decisions: {vote_counter['borderline_decisions']}, "
          f"total real votes gathered: {vote_counter['total_votes_gathered']}")
    print(f"\nBASELINE: {json.dumps(baseline_summary, indent=2)}")
    print(f"\nENSEMBLE: {json.dumps(ensemble_summary, indent=2)}")
    print(f"\nGATE: {'PASS' if passed else 'FAIL'}")
    for r in reasons:
        print(f"  - {r}")
    print(f"\nFull report: {out_path}")


if __name__ == "__main__":
    main()
