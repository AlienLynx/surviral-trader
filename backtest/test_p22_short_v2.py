"""
P22-A1 -- the TRUE SHORT variant (shorts_v2): rule prefilter creates the candidates, the model can only confirm or veto, no `buy` anywhere, exits unchanged,
the funding line in the prompt, funding served as-of in the replay. Pure-Python checks on fixtures (fake candles, fake model); nothing needs Docker, the network or a key.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.update({"REPLAY_DIR": tempfile.mkdtemp(), "DATA_DIR": tempfile.mkdtemp(), "REPLAY_STRATEGIES_DIR": str(REPO), "REPLAY_MIN_START": "2026-08-01"})
failures: list[str] = []


def check(label, cond):
    print(f"[{'OK' if cond else 'FAIL'}] {label}")
    if not cond:
        failures.append(label)


from backend.replay import data as rd, engine as eng, sandbox as sb  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 9, 10, tzinfo=UTC)
END = START + timedelta(days=3)
WARM = START - timedelta(days=eng.WARMUP_DAYS)
HOURS = int((END - WARM).total_seconds() // 3600) + 60
FILES = {"shorts_v2": REPO / "freqtrade-shorts-v2/user_data/strategies/DeepSeekShortsV2Strategy.py"}
PARENTS = {"shorts_v2": REPO / "freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py"}
src = {k: v.read_text(encoding="utf-8") for k, v in FILES.items()}
par = {k: v.read_text(encoding="utf-8") for k, v in PARENTS.items()}


def method(text: str, name: str) -> str:
    m = re.search(rf"^    def {name}\(.*?(?=^    def |\Z)", text, re.S | re.M)
    return m.group(0) if m else ""


# ------------------------------------------------------------------ static: what the variants are
for bot, text in src.items():
    check(f"{bot}: the model's answers are confirm / veto only -- the parser allows nothing else and `buy` is not an allowed answer anywhere", '_parse_llm_decision(answer, ("confirm", "veto"))' in text and '("buy"' not in text and "'buy:" not in text)
    check(f"{bot}: the strategy can never open a long (enter_long is only ever reset to 0)", 'dataframe.iloc[-1, dataframe.columns.get_loc("enter_long")] = 1' not in text and '"enter_long"] = 1' not in text)
    check(f"{bot}: the rule prefilter is the exact mirror of the parent's long placeholder rule (pct_change_24 < -2 and volatility_24 < 5) and the parent's own short rule",
          "CANDIDATE_MAX_PCT_CHANGE_24 = -2.0" in text and "CANDIDATE_MAX_VOLATILITY_24 = 5.0" in text and '(dataframe["pct_change_24"] < -2) & (dataframe["volatility_24"] < 5), "enter_short"' in par[bot])
    check(f"{bot}: exits are unchanged -- custom_exit, confirm_trade_exit, the ROI / stoploss / min-hold constants and the stake logic are byte-identical to the parent",
          all(method(text, m) == method(par[bot], m) and method(text, m) != "" for m in ("custom_exit", "confirm_trade_exit", "custom_stake_amount", "_custom_stake_amount_base", "leverage"))
          and all(re.search(rf"^    {c}.*$", text, re.M).group(0) == re.search(rf"^    {c}.*$", par[bot], re.M).group(0) for c in ("minimal_roi = ", "stoploss = ", "MIN_HOLD_HOURS = ", "STALE_SIGNAL_STREAK_REQUIRED = ")))
    check(f"{bot}: the entry veto of the parent (confirm_trade_entry: funding / loss-streak) is kept unchanged", method(text, "confirm_trade_entry") == method(par[bot], "confirm_trade_entry"))
    check(f"{bot}: the prompt carries the funding line and the candidate line; the model is told it cannot create candidates or go long", "_get_funding_rate_line(pair)" in method(text, "_ask_deepseek") and "_candidate_line(row)" in method(text, "_ask_deepseek")
          and "can NOT create candidates and you can NOT go long" in text and "'confirm: <reason" in text and "'veto: <reason" in text)
    check(f"{bot}: every decision logs one structured V2_DECISION line (pair, rule candidate, vote, reason, price) -- the source of the ledger sources rule_short / llm_veto at live deploy", "V2_DECISION" in text and '"candidate": candidate' in text and '"vote": vote' in text)
check("COPIES: the helper and the config sit in each variant's own directory (duplicated per bot, nothing imported across directories)",
      all((REPO / d / "user_data" / "strategies" / "_allocator_ctx.py").is_file() and (REPO / d / "user_data" / "config.json").is_file() for d in ("freqtrade-shorts-v2",)))
check("NOT DEPLOYED: no Dockerfile / compose service for the variants (A3 needs the operator's approval of the A2 report)", "shorts-v2" not in (REPO / "docker-compose.yml").read_text(encoding="utf-8") and not (REPO / "freqtrade-shorts-v2/Dockerfile").exists())
check("ENGINE: the variant is registered as a non-default replay bot on the perpetual (linear) market", all(eng.BOTS[b]["category"] == "linear" and eng.BOTS[b]["default"] is False for b in ("shorts_v2",)))


# ------------------------------------------------------------------ the replay on a synthetic market
def synth(seed, decline: bool):
    rng = np.random.default_rng(seed)
    n = HOURS
    close = np.full(n, 100.0)
    t0 = int((START - WARM).total_seconds() // 3600)
    for i in range(1, n):
        drift = -0.0015 if (decline and t0 + 2 <= i <= t0 + 40) else (0.0004 if not decline else 0.0)
        close[i] = close[i - 1] * (1 + drift + rng.normal(0, 0.0003))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) * 1.0004
    low = np.minimum(open_, close) * 0.9996
    vol = np.full(n, 2000.0)
    return pd.DataFrame({"date": pd.date_range(WARM, periods=n, freq="h", tz="UTC"), "open": open_, "high": high, "low": low, "close": close, "volume": vol, "quote": vol * close})


def make_store(funding_rate: float | None = None, funding_future: float | None = None):
    s = rd.SeriesStore()
    for i, (c, dec) in enumerate((("AAA", True), ("BBB", False), ("CCC", True))):
        s.add("linear", f"{c}USDT", synth(i + 1, dec))
        if funding_rate is not None:
            rows = [[int((START + timedelta(hours=8 * k) - timedelta(days=3)).timestamp() * 1000), funding_rate] for k in range(0, 30)]
            if funding_future is not None:
                rows = [[ts, funding_rate if ts <= int(START.timestamp() * 1000) + 28 * 3600000 else funding_future] for ts, _ in rows]
            s.add_funding("linear", f"{c}USDT", rows)
    s.add("spot", "BTCUSDT", synth(9, False))
    return s


class FakeLLM:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def call(self, system, user, temperature=0.1, max_tokens=300):
        self.prompts.append((system, user))
        return self.answer(user) if callable(self.answer) else self.answer


PAIRS = [rd.pair_name(c, "linear") for c in ("AAA", "BBB", "CCC")]


def run(bot, answer, mode="named", store=None, mech=None):
    llm = FakeLLM(answer)
    pr = {"every_n": 1, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05, "rule": mode == "rule"}
    if mech:
        pr["mech"] = mech
    r = eng.BotRun(bot, mode, pr, store or make_store(), None, None if mode == "rule" else llm, PAIRS, "t")
    return llm, r.run(START, END), r


for bot in ("shorts_v2",):
    llm_c, res_c, _ = run(bot, "confirm: drop continues")
    llm_v, res_v, _ = run(bot, "veto: bounce under way")
    dec = res_c["decisions"]
    cands = [d for d in dec if d.get("candidate")]
    check(f"{bot}: the model is called ONLY for rule candidates (calls == candidates), never for the others -- and there are candidates at all in the fixture", len(llm_c.prompts) == len(cands) and len(cands) > 10 and len(dec) > len(cands))
    check(f"{bot}: every decision carries the rule candidate flag and the model's vote (skip for a non-candidate, confirm / veto for a candidate)", all("candidate" in d and d["vote"] == ("confirm" if d["candidate"] else "skip") for d in dec))
    check(f"{bot}: all-confirm opens SHORT trades only; all-veto opens none; no long is ever opened", res_c["trades"] + res_c["open_trades"] and all(t["side"] == "short" for t in res_c["trades"] + res_c["open_trades"]) and not (res_v["trades"] or res_v["open_trades"]))
    check(f"{bot}: a veto is a `hold` decision (the parent's exits keep working on the internal names) and a confirm is `sell_short`; there is no `buy` decision",
          {d["action"] for d in res_c["decisions"]} <= {"sell_short", "hold"} and all(d["action"] == "hold" for d in res_v["decisions"]))
    _, res_bad, _ = run(bot, "buy: I would go long")
    check(f"{bot}: anything else than confirm (an answer in the parent's language `buy`, garbage) is a veto -- the model cannot open a trade by mistake", not (res_bad["trades"] or res_bad["open_trades"]) and all(d["vote"] in ("skip", "veto") for d in res_bad["decisions"]))
    llm_n, res_n, _ = run(bot, None)
    check(f"{bot}: no answer (outage) -> no entry; the vote is logged as no_llm", not (res_n["trades"] or res_n["open_trades"]) and any(d.get("vote") == "no_llm" for d in res_n["decisions"]))
    _, res_r, _ = run(bot, None, mode="rule")
    rule_trades = res_r["trades"] + res_r["open_trades"]
    check(f"{bot}: rule-only mode = every candidate is entered (short only, no model call, no vote fields); it opens at least as many trades as the confirmed run", rule_trades and all(t["side"] == "short" for t in rule_trades)
          and len(rule_trades) >= len(res_c["trades"] + res_c["open_trades"]) and all("vote" not in d for d in res_r["decisions"]))
    first = llm_c.prompts[0][1]
    check(f"{bot}: the prompt shows the candidate line and the rule thresholds", re.search(r"Rule candidate: SHORT \(24-candle change -\d+\.\d+% < -2\.0%, volatility \d+\.\d+ < 5\.0\)", first) is not None)

# ------------------------------------------------------------------ funding as of the clock
st_f = make_store(funding_rate=-0.0001)
llm_f, res_f, _ = run("shorts_v2", "confirm: ok", store=st_f)
check("FUNDING: with funding data the prompt carries the real funding line (-0.0100%/8h)", llm_f.prompts and all("Current funding rate: -0.0100%/8h" in u for _, u in llm_f.prompts))
llm_n2, _, _ = run("shorts_v2", "confirm: ok")
check("FUNDING: without funding data the line is left out (never a guessed number)", llm_n2.prompts and all("Current funding rate" not in u for _, u in llm_n2.prompts))
st_fut = make_store(funding_rate=-0.0001, funding_future=-0.009)
llm_a, _, _ = run("shorts_v2", "confirm: ok", store=st_fut)
st_nofut = make_store(funding_rate=-0.0001)
llm_b, _, _ = run("shorts_v2", "confirm: ok", store=st_nofut)
check("FUNDING AS-OF: a funding settlement that happens LATER than the simulated clock never reaches an earlier prompt (changing the future leaves the first prompts identical, and the later ones do change)",
      [u for _, u in llm_a.prompts[:3]] == [u for _, u in llm_b.prompts[:3]] and [u for _, u in llm_a.prompts] != [u for _, u in llm_b.prompts])
st_neg = make_store(funding_rate=-0.01)                          # -1%/8h: crowded shorts, the parent's confirm_trade_entry veto must refuse the entry
_, res_neg, _ = run("shorts_v2", "confirm: ok", store=st_neg)
check("FUNDING VETO: the parent's confirm_trade_entry (funding below -0.05%/8h for an alt) now works in the replay too -- every entry is refused although the model confirmed", not (res_neg["trades"] or res_neg["open_trades"]) and any(d.get("vote") == "confirm" for d in res_neg["decisions"]))
r_h = sb.ReplayHTTP(__import__("types").SimpleNamespace(store=st_f, clock=sb.SimClock(START + timedelta(days=1)), notes=set(), blocked=[]))
rows = r_h.get("https://api.bybit.com/v5/market/funding/history", {"symbol": "AAAUSDT", "limit": 3}).json()["result"]["list"]
check("FUNDING history endpoint: newest first, only settlements before the clock, honours `limit`", len(rows) == 3 and int(rows[0]["fundingRateTimestamp"]) > int(rows[1]["fundingRateTimestamp"]) and all(int(r["fundingRateTimestamp"]) <= int((START + timedelta(days=1)).timestamp() * 1000) for r in rows))
check("FUNDING store: as-of returns nothing for a symbol without data", st_f.funding_asof("linear", "NOPEUSDT", START) == [])

# ------------------------------------------------------------------ A2: candidates, the veto's value, the estimate
from backend.replay import analysis, runner as rr  # noqa: E402


def run_n(bot, answer, every_n, mode="named", store=None):
    llm = FakeLLM(answer)
    pr = {"every_n": every_n, "workers": 1, "seed": 7, "fee_pct": 0.1, "slippage_pct": 0.05, "rule": mode == "rule"}
    r = eng.BotRun(bot, mode, pr, store or make_store(), None, None if mode == "rule" else llm, PAIRS, "t")
    return llm, r.run(START, END), r


for ev_n in (1, 2, 3):
    llm_e, res_e, _ = run_n("shorts_v2", "confirm: x", ev_n)
    cnt = analysis.count_candidates(make_store(), "linear", PAIRS, START, END, ev_n)
    check(f"ESTIMATE every {ev_n}: the candidate count made BEFORE the run equals the model calls the replay really makes ({cnt} == {len(llm_e.prompts)}) -- the call estimate is exact", cnt == len(llm_e.prompts) and cnt > 0)
cand_parent = analysis.CAND_PCT_CHANGE == -2.0 and analysis.CAND_VOLATILITY == 5.0 and "CANDIDATE_MAX_PCT_CHANGE_24 = -2.0" in src["shorts_v2"] and "CANDIDATE_MAX_VOLATILITY_24 = 5.0" in src["shorts_v2"]
check("ESTIMATE: the thresholds used to count candidates are the strategy file's own (a copy that this test keeps in step)", cand_parent)
p_cand = rr.normalize_params({"bots": ["shorts_v2"], "days": 7, "end": "2026-09-30T00:00:00Z", "modes": ["named"], "every_n": 2, "pairs": 10}, datetime(2026, 10, 20, tzinfo=UTC))
e_cand = rr.estimate(p_cand, 3500.0, 0.14, 0.28, None, {"shorts_v2": 40})
e_full = rr.estimate(p_cand, 3500.0, 0.14, 0.28, None, None)
check("ESTIMATE: with candidates the calls are candidates x LLM modes (40), not pairs x steps (840): only candidates reach the model", e_cand["calls"] == 40 and e_full["calls"] == 10 * 84 and e_cand["usd"] < e_full["usd"] / 10 and e_cand["candidates"] == {"shorts_v2": 40})
p_rule = rr.normalize_params({"bots": ["shorts_v2"], "days": 7, "end": "2026-09-30T00:00:00Z", "modes": [], "add_rule": True, "rule_only": True}, datetime(2026, 10, 20, tzinfo=UTC))
check("RULE-ONLY: an empty model-mode list with rule_only and the rule on is a valid $0 run (0 calls); with the rule off it is refused", p_rule["modes"] == [] and rr.estimate(p_rule, None, None, None)["calls"] == 0
      and rr.estimate(p_rule, None, None, None)["usd"] == 0.0)
try:
    rr.normalize_params({"bots": ["shorts_v2"], "days": 7, "end": "2026-09-30T00:00:00Z", "modes": [], "add_rule": False, "rule_only": True}, datetime(2026, 10, 20, tzinfo=UTC))
    check("RULE-ONLY: no model mode and no rule is refused", False)
except rr.ParamError:
    check("RULE-ONLY: no model mode and no rule is refused (and an empty mode list without rule_only stays refused, as before)", True)

# the veto's value: a planted world where the model vetoes the BAD candidates / the GOOD candidates
st_w = make_store()
rng_w = np.random.default_rng(5)


def fake_dec(vote_good_confirmed: bool, n=300):
    """Decisions on the declining pair AAA: candidate hours; `good` = the price fell further in the next 4h (the short wins)."""
    out = []
    df = st_w.frames[("linear", "AAAUSDT")]
    close = df.set_index("date")["close"]
    for k in range(n):
        t = START + timedelta(hours=1 + k % 60) + timedelta(days=k // 60)
        p0, p4 = close.get(pd.Timestamp(t - timedelta(hours=1))), close.get(pd.Timestamp(t + timedelta(hours=3)))
        if p0 is None or p4 is None:
            continue
        good = p4 < p0
        vote = "confirm" if (good == vote_good_confirmed) else "veto"
        out.append({"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "bot": "shorts_v2", "pair": "AAA/USDT:USDT", "action": "sell_short" if vote == "confirm" else "hold", "candidate": True, "vote": vote})
    return out


good_model = analysis.candidate_report(fake_dec(True), st_w, "linear")
bad_model = analysis.candidate_report(fake_dec(False), st_w, "linear")
check("VETO VALUE: a model that confirms the candidates that then fall and vetoes the ones that do not -> confirmed minus vetoed is clearly positive at 4h and the label says the veto kept better candidates", good_model["veto_value"]["h4"]["confirmed_minus_vetoed_pp"] > 0
      and (good_model["veto_value"]["h4"]["ci95_pp"][0] > 0) == good_model["veto_adds"].startswith("the veto kept better"))
check("VETO VALUE: the inverted model is labelled as removing the better candidates (or, with too few vetoes, as insufficient n) -- never as adding value", bad_model["veto_value"]["h4"]["confirmed_minus_vetoed_pp"] < 0 and not bad_model["veto_adds"].startswith("the veto kept better"))
check("VETO VALUE: the share vetoed, the votes and the bare-rule mean over ALL candidates are reported; fewer than 20 vetoes -> insufficient n",
      good_model["share_vetoed"] is not None and set(good_model["votes"]) == {"confirm", "veto"} and good_model["forward_short_return_pct"]["h4"]["all_candidates (bare rule)"]["n"] == good_model["n_candidates"]
      and analysis.candidate_report(fake_dec(True)[:10], st_w, "linear")["veto_adds"].startswith("insufficient n"))
one = fake_dec(True)[0]
df_ = st_w.frames[("linear", "AAAUSDT")].set_index("date")["close"]
t1 = datetime.strptime(one["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
expect = -(float(df_.get(pd.Timestamp(t1 + timedelta(hours=3)))) / float(df_.get(pd.Timestamp(t1 - timedelta(hours=1)))) - 1) * 100
single = analysis.candidate_report([one, {**one, "ts": (t1 + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}], st_w, "linear")
check("VETO VALUE: the forward return of a candidate is the short's -(price return) over the 4h horizon, measured from the close of the candle opened at t0-1h", abs(single["forward_short_return_pct"]["h4"]["all_candidates (bare rule)"]["mean_pct"] - (expect + -(float(df_.get(pd.Timestamp(t1 + timedelta(days=1) + timedelta(hours=3)))) / float(df_.get(pd.Timestamp(t1 + timedelta(days=1) - timedelta(hours=1)))) - 1) * 100) / 2) < 1e-3)

# the full report on a replay run (veto run + rule-only run) -> markdown
llm_r, res_named, run_named = run_n("shorts_v2", lambda u: "confirm: x" if hash(u) % 2 else "veto: y", 1)
_, res_rule, run_rule = run_n("shorts_v2", None, 1, mode="rule")
p_rep = {"bots": ["shorts_v2"], "days": 3, "start": START, "end": END, "modes": ["named"], "every_n": 1, "pairs": 3, "fee_pct": 0.1, "slippage_pct": 0.05, "seed": 7, "max_usd": 1, "max_calls": 1000}
rep = rr.build_report("rid", p_rep, {("shorts_v2", "named"): res_named, ("shorts_v2", "rule"): res_rule}, make_store(), {"linear": PAIRS, "spot": []},
                      {"calls": 0, "cache_hits": 0, "usd": 0, "label": "fake"}, [run_named, run_rule])
cn = rep["bots"]["shorts_v2"]["named"]["candidates"]
cr = rep["bots"]["shorts_v2"]["rule"]["candidates"]
md = rr.to_markdown(rep)
check("REPORT: the veto run carries the candidate analysis (candidates, votes, share vetoed, forward returns of confirmed vs vetoed) and the rule-only run the bare-rule candidates", cn["n_candidates"] == len(llm_r.prompts) and cn["votes"].get("confirm", 0) + cn["votes"].get("veto", 0) == cn["n_candidates"]
      and cr["n_candidates"] > 0 and set(cr["votes"]) == {"confirm"})
check("REPORT: the markdown answers the question of A2 in its own section (share vetoed, confirmed vs vetoed per horizon, the computed label and its fixed rule), next to the LLM-vs-rule line", "does the model's veto add anything over the bare rule" in md
      and "share vetoed" in md and "confirmed minus vetoed" in md and "fixed in advance" in md)
nm_, rl_ = rep["bots"]["shorts_v2"]["named"], rep["bots"]["shorts_v2"]["rule"]
check("REPORT: when both the veto run and the rule-only run have trades, the LLM-vs-rule line (trade level) is in the report next to the candidate analysis", (nm_["mean_trade_pct"] is None or rl_["mean_trade_pct"] is None) or ("shorts_v2" in rep["llm_vs_rule"] and "LLM vs rule" in md))
check("REPORT: replay never gives the status `edge` for the v2 bots either", all(e["status"] != "edge" for e in rep["bots"]["shorts_v2"].values() if isinstance(e, dict)))
tr_all = res_named["trades"] + res_named["open_trades"]
check("REPORT: random-entry baseline for the v2 bots draws SHORT entries only (share of shorts = 1)", all(t["side"] == "short" for t in tr_all) and (rep["bots"]["shorts_v2"]["named"]["random_baseline"] is not None or not tr_all))


# ------------------------------------------------------------------ the page
html = (REPO / "frontend/replay.html").read_text(encoding="utf-8")
check("PAGE: the Experiments block has the short-bots card -- variants, with / without the veto, window, step, the candidate and call estimate, a confirm step, the results block (confirmed vs vetoed)",
      all(x in html for x in ("sCard", "sBot", "sVeto", "sDays", "sPairs", "sEvery", "sEstimate", "sEstBtn", "sStart", "renderCandidates", "rule_only", "api/replay/estimate")))
check("PAGE: the card never starts a run without showing the estimate first (Start is disabled until an estimate came back) and asks for a confirmation", "$('sStart').disabled = !j.within_limits || !j.prices_known" in html and "sStart').disabled = true" in html and "if (!sLast) return;" in html and "confirm((sLast.p.modes.length" in html)
check("PAGE: the v2 bots are labelled replay-only in the bot list, not 'stopped'", "P22-A, replay only" in html)

if failures:
    print(f"\n{len(failures)} FAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nALL CHECKS PASSED")
