"""
P26: Listing Fade exit-mechanics study -- would a tighter stop and/or a re-entry cooldown after a stop-loss have made the live Listing Fade rules earn more?

The question comes from four weeks of live dry-run (23 Oct 2026): 77% winners but break-even, because a few -12% stops (some gapping to -14% .. -31%) cancel many +1..5% trailing exits,
and the bot re-shorts the same pumping coin an hour after being stopped out. This study answers it on data the live trades are NOT part of: the same Bybit listing history as the Listing Fade
study (every USDT perpetual listed since 2024-01-01, delisted ones included, 5-minute candles, real fees, funding, stop gaps and slippage), never on the four weeks that suggested the idea.

Pre-registered before the run (nothing is tuned on the deciding period):
  base        the live rules are read from the live strategy file and config (listing_fade.load_rules); the only things changed are the numbers below
  scenario    like the live bot: a pair may be traded again after an exit, 3 slots, liquidity-flagged events excluded, taker fee 0.055% per side
  slippage    0.30% per side is the basis; 0.50% is the stress line
  variants    hard stop in {-12% (live), -10%, -8%, -6%} x cooldown after a stop-loss in {none, 24 h}; trailing, max hold and entry untouched
  candidate   ONE variant is the question that decides: stop -8% with a 24 h cooldown (`stop8_cd24`). The other seven are context; a grid of eight is a multiple-testing trap, so none of them
              can produce the verdict
  periods     entries before SPLIT are for looking (locked period), entries from SPLIT decide (out-of-sample)
  verdict     `better candidate` only if ALL hold, comparing the candidate with the live rules on the SAME listings (basis line):
                1. at least MIN_OOS_TRADES out-of-sample trades with the live rules
                2. paired daily P&L difference (candidate - live) in the out-of-sample period: block-bootstrap 95% CI lower bound > 0
                3. locked-period net P&L of the candidate >= that of the live rules
                4. out-of-sample max drawdown of the candidate <= that of the live rules
                5. at 0.50% slippage the candidate's out-of-sample net P&L is still above the live rules'
              `unproven` if the out-of-sample difference is positive but a condition fails; `not better` otherwise. Replay never changes a live setting: a pass is a hypothesis for a live dry-run.
Known limits (in every report): the live pair list (VolumePairList top 200, SpreadFilter) cannot be rebuilt, the live entry is a passive limit order that can expire, daily-style gaps are only
as good as 5-minute candles, survivorship bias is reduced not removed (see the Listing Fade study), and one regime (2024-2026 listings).
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import listing_fade as lf, momentum as mo, paths

STUDY_ID = "fade_exit"
SINCE = lf.SINCE
SPLIT = "2025-07-01"
SLIP = 0.30
SLIP_STRESS = 0.50
FEE = lf.FEE_TAKER_PCT
MIN_OOS_TRADES = 100
STOPS = (-0.12, -0.10, -0.08, -0.06)
COOLDOWNS = (0.0, 24.0)
CANDIDATE = "stop8_cd24"
LIVE = "live"


def variant_name(stop: float, cd: float) -> str:
    if stop == -0.12:
        return LIVE if cd == 0.0 else f"cd{int(cd)}"
    return f"stop{int(round(-stop * 100))}" + (f"_cd{int(cd)}" if cd else "")


VARIANTS = [(variant_name(s, c), s, c) for s in STOPS for c in COOLDOWNS]


# ------------------------------------------------------------------ numbers
def _closed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if not t["open_at_end"]]


def daily_pnl(trades: list[dict], lo: datetime, hi: datetime) -> np.ndarray:
    """Net P&L (USD) per calendar day of exit, for every day in [lo, hi) (zeros included)."""
    n = max(0, (hi.date() - lo.date()).days)
    out = np.zeros(n)
    for t in _closed(trades):
        k = (t["close_date"].date() - lo.date()).days
        if 0 <= k < n:
            out[k] += t["profit_abs"]
    return out


def max_dd_pct(trades: list[dict], wallet: float) -> float:
    eq, peak, dd = wallet, wallet, 0.0
    for t in sorted(_closed(trades), key=lambda x: x["close_date"]):
        eq += t["profit_abs"]
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100.0)
    return round(dd, 2)


def period_stats(trades: list[dict], wallet: float) -> dict:
    c = _closed(trades)
    if not c:
        return {"n": 0, "net_usd": 0.0, "mean_trade_pct": None, "win_pct": None, "stops": 0, "stop_mean_pct": None, "max_dd_pct": 0.0}
    r = [t["profit_ratio"] for t in c]
    stops = [t["profit_ratio"] for t in c if t["exit_reason"] == "stop_loss"]
    return {"n": len(c), "net_usd": round(sum(t["profit_abs"] for t in c), 2), "mean_trade_pct": round(float(np.mean(r)) * 100, 3), "win_pct": round(sum(1 for x in r if x > 0) / len(r) * 100, 1),
            "stops": len(stops), "stop_mean_pct": round(float(np.mean(stops)) * 100, 2) if stops else None, "max_dd_pct": max_dd_pct(c, wallet)}


def split_trades(trades: list[dict], split: datetime) -> tuple[list[dict], list[dict]]:
    return [t for t in trades if t["open_date"] < split], [t for t in trades if t["open_date"] >= split]


def paired_diff(var: list[dict], live: list[dict], lo: datetime, hi: datetime) -> dict:
    """Candidate minus live, daily USD over [lo, hi): mean per day and a block-bootstrap (7-day blocks) 95% CI."""
    d = daily_pnl(var, lo, hi) - daily_pnl(live, lo, hi)
    ci = mo.block_ci(d, block=7)
    return {"days": int(len(d)), "mean_usd_per_day": round(float(d.mean()), 4) if len(d) else None, "total_usd": round(float(d.sum()), 2), "ci95_usd_per_day": None if ci is None else [round(ci[0], 4), round(ci[1], 4)]}


def verdict(live_oos: dict, diff_oos: dict, live_locked: dict, cand_locked: dict, cand_oos: dict, live_stress_oos: dict, cand_stress_oos: dict) -> dict:
    fails = []
    if live_oos["n"] < MIN_OOS_TRADES:
        fails.append(f"only {live_oos['n']} out-of-sample trades with the live rules (< {MIN_OOS_TRADES})")
    ci = diff_oos.get("ci95_usd_per_day")
    if ci is None or ci[0] <= 0:
        fails.append("the paired out-of-sample daily P&L difference does not exclude 0")
    if cand_locked["net_usd"] < live_locked["net_usd"]:
        fails.append(f"locked period: candidate ${cand_locked['net_usd']} < live ${live_locked['net_usd']}")
    if cand_oos["max_dd_pct"] > live_oos["max_dd_pct"]:
        fails.append(f"out-of-sample max drawdown {cand_oos['max_dd_pct']}% > live {live_oos['max_dd_pct']}%")
    if cand_stress_oos["net_usd"] <= live_stress_oos["net_usd"]:
        fails.append(f"at {SLIP_STRESS}% slippage the candidate (${cand_stress_oos['net_usd']}) is not above live (${live_stress_oos['net_usd']})")
    if not fails:
        return {"verdict": "better candidate", "failed": []}
    mean = diff_oos.get("mean_usd_per_day")
    return {"verdict": "unproven" if (mean is not None and mean > 0) else "not better", "failed": fails}


# ------------------------------------------------------------------ study
def evaluate(rules: dict, events: list[dict], m5_cache: dict, until: datetime, fee: float = FEE, split: datetime | None = None, log=lambda s: None) -> dict:
    """The study on loaded inputs (events with signals, 5-minute frames). No network, no file access."""
    split = split or datetime.fromisoformat(SPLIT).replace(tzinfo=timezone.utc)
    wallet = rules["wallet"]
    runs: dict[str, dict[float, list[dict]]] = {}
    for name, stop, cd in VARIANTS:
        r = {**rules, "stoploss": stop}
        runs[name] = {}
        for slip in (SLIP, SLIP_STRESS):
            runs[name][slip] = lf.run_scenario(r, events, slip, rules["max_open_trades"], True, fee, until, m5_cache, {}, reentry=True, cooldown_h=cd)
        log(f"{name}: {len(_closed(runs[name][SLIP]))} trades at {SLIP}% slippage")
    t_lo = datetime.fromisoformat(SINCE).replace(tzinfo=timezone.utc)
    t_hi = max([t["close_date"] for name in runs for t in _closed(runs[name][SLIP])] or [until]) + timedelta(days=1)
    table = {}
    for name, stop, cd in VARIANTS:
        row = {"stoploss_pct": round(stop * 100), "cooldown_h": cd}
        for slip in (SLIP, SLIP_STRESS):
            lock, oos = split_trades(runs[name][slip], split)
            row[f"slip{slip:g}"] = {"all": period_stats(runs[name][slip], wallet), "locked": period_stats(lock, wallet), "oos": period_stats(oos, wallet)}
        base_l, base_o = split_trades(runs[LIVE][SLIP], split)
        row["oos_diff_vs_live"] = paired_diff(split_trades(runs[name][SLIP], split)[1], base_o, split, t_hi)
        row["locked_diff_vs_live"] = paired_diff(split_trades(runs[name][SLIP], split)[0], base_l, t_lo, split)
        table[name] = row
    c, lv = table[CANDIDATE], table[LIVE]
    v = verdict(lv[f"slip{SLIP:g}"]["oos"], c["oos_diff_vs_live"], lv[f"slip{SLIP:g}"]["locked"], c[f"slip{SLIP:g}"]["locked"], c[f"slip{SLIP:g}"]["oos"],
                lv[f"slip{SLIP_STRESS:g}"]["oos"], c[f"slip{SLIP_STRESS:g}"]["oos"])
    gaps = [t["gap_pct"] for t in _closed(runs[LIVE][SLIP]) if t["exit_reason"] == "stop_loss"]
    return {"verdict": v["verdict"], "verdict_failed_conditions": v["failed"], "candidate": CANDIDATE, "split": split.date().isoformat(), "variants": table,
            "live_stop_gaps_pct": {"n": len(gaps), "mean": round(float(np.mean(gaps)), 2) if gaps else None, "max": round(float(np.max(gaps)), 2) if gaps else None}, "_trades": runs}


LIMITS = ["The live pair list (VolumePairList top 200, SpreadFilter) cannot be rebuilt from public history: the replay trades every listing that passes the age, price and funding rules.",
          "The live entry is a passive limit order that can expire unfilled; the slippage lines stand in for it.",
          "Stop and trailing fills use 5-minute candles: a stop gaps to the next candle's open, but a spike inside one 5-minute candle is only as visible as that candle.",
          "Survivorship bias is reduced, not removed (see the Listing Fade study). One regime: listings from 2024 to 2026.",
          "The idea came from four live weeks (not part of this data). The deciding period is the listings from the split date on; the other seven variants are context and cannot give the verdict."]


def run_study(run_id: str, params: dict, base=None, until: datetime | None = None, log=lambda s: None) -> dict:
    until = until or datetime.now(timezone.utc)
    rules = lf.load_rules(base)
    events, uni = lf.load_universe(rules, until)
    m5_cache: dict[str, pd.DataFrame] = {}
    for e in events:
        if e["signals"]:
            d = lf._read_json(lf.lf_dir("m5", f"{e['inst']['symbol']}.json"))
            if d and d["rows"]:
                m5_cache[e["inst"]["symbol"]] = lf.to_df(d["rows"])
    log(f"{len(events)} listings, {sum(1 for e in events if e['signals'])} with a signal, {len(m5_cache)} with 5-minute data")
    split = datetime.fromisoformat(params.get("split", SPLIT)).replace(tzinfo=timezone.utc)
    res = evaluate(rules, events, m5_cache, until, float(params.get("fee_pct", FEE)), split, log)
    trades = res.pop("_trades")
    rep = {"run_id": run_id, "type": "fade_exit_study", "banner": "Rule-based historical study, $0, no LLM. A pass is a hypothesis for a live dry-run, never a change of a live setting.",
           "rules": lf.rules_summary(rules), "universe": lf.survivorship(uni, events), "basis": {"fee_pct": float(params.get("fee_pct", FEE)), "slippage_pct": [SLIP, SLIP_STRESS], "slots": rules["max_open_trades"], "reentry": True},
           "min_oos_trades": MIN_OOS_TRADES, **res, "limits": LIMITS + rules["not_applied"]}
    rep["_primary_trades"] = {k: trades[k][SLIP] for k in (LIVE, CANDIDATE)}
    return rep


def to_markdown(rep: dict) -> str:
    s_key = f"slip{SLIP:g}"
    L = [f"# Listing Fade exit-mechanics study {rep['run_id']}", "", f"**{rep['banner']}**", "", f"## Verdict for `{rep['candidate']}`: `{rep['verdict']}`", "",
         ("Not met: " + "; ".join(rep["verdict_failed_conditions"])) if rep["verdict_failed_conditions"] else "All pre-registered conditions met.", "",
         f"Live rules: stop -12%, no cooldown. Candidate: stop -8%, 24 h cooldown after a stop-loss. Split date {rep['split']} (before: looking, from: deciding). Basis {rep['basis']}.", "",
         f"Stop-loss fills in the live-rules run gap {rep['live_stop_gaps_pct']} (percent beyond the stop level).", "",
         f"## Variants at {SLIP}% slippage", "", "| variant | stop % | cooldown h | period | n | net $ | mean % | win % | stops | mean stop % | max DD % |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, row in rep["variants"].items():
        for per in ("all", "locked", "oos"):
            p = row[s_key][per]
            L.append(f"| {name} | {row['stoploss_pct']} | {row['cooldown_h']:g} | {per} | {p['n']} | {p['net_usd']} | {p['mean_trade_pct']} | {p['win_pct']} | {p['stops']} | {p['stop_mean_pct']} | {p['max_dd_pct']} |")
    L += ["", "## Out-of-sample difference to the live rules (paired daily P&L, USD)", "", "| variant | days | total $ | mean $/day | 95% CI $/day |", "|---|---|---|---|---|"]
    for name, row in rep["variants"].items():
        d = row["oos_diff_vs_live"]
        L.append(f"| {name} | {d['days']} | {d['total_usd']} | {d['mean_usd_per_day']} | {d['ci95_usd_per_day']} |")
    L += ["", f"## Stress: {SLIP_STRESS}% slippage, out-of-sample net $", "", "| variant | net $ | n |", "|---|---|---|"]
    for name, row in rep["variants"].items():
        p = row[f"slip{SLIP_STRESS:g}"]["oos"]
        L.append(f"| {name} | {p['net_usd']} | {p['n']} |")
    L += ["", "## Rules read from the live files", "", "```", json.dumps(rep["rules"], indent=1), "```", "", "## Limits", ""] + [f"- {x}" for x in rep["limits"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, params: dict, rep: dict) -> None:
    prim = rep.pop("_primary_trades", {})
    base = paths.replay_path(run_id)
    paths.ensure_parent(base / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(rep), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["variant", "pair", "open_date", "close_date", "exit_reason", "profit_ratio", "profit_abs", "gap_pct"])
    for name, trs in prim.items():
        for t in trs:
            if not t["open_at_end"]:
                cw.writerow([name, t["pair"], lf._iso(t["open_date"]), lf._iso(t["close_date"]), t["exit_reason"], round(t["profit_ratio"], 6), round(t["profit_abs"], 4), round(t["gap_pct"], 4)])
    (base / "trades.csv").write_text(buf.getvalue(), encoding="utf-8")
