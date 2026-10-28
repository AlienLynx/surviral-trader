"""
P22 K3 -- evaluation of the historical Kronos signals. Two independent views of the same signals:
  1. SIGNAL QUALITY, independent of any exit: forward returns after up / down signals at 1h / 4h / 24h, the excess over the unconditional mean of the same hour (the mean forward return
     of all pairs scored at that t0, as in the live audit), a bootstrap CI per direction clustered by calendar day (hours of one day are not independent), and the same numbers for both
     halves of the window. THE VERDICT IS COMPUTED HERE, from rules fixed in advance (nothing is fitted on either half):
        statistic  = 4h forward return, direction-adjusted (a down signal earns minus the return), minus the benchmark of the same hour, minus the round-trip cost (2 x (fee + slippage))
        per direction:  n < 200 -> `insufficient n`;  CI lower bound > 0 AND mean > 0 in BOTH halves -> `edge candidate`;  CI upper bound < 0 -> `no edge`;  otherwise `unproven`
        overall: any direction `edge candidate` -> `edge candidate`; else any `unproven` -> `unproven`; else any `insufficient n` -> `insufficient n`; else `no edge`.
     The same rule on the GROSS excess (no cost) is reported as `signal_skill_gross`: it says whether the signal carries any information at all, before the cost of trading it.
  2. REPLAY ENGINE: the signals become the decisions of the real `main` (up -> buy) and `shorts` (down -> sell_short) strategies -- same exits, fees, slippage -- next to the strategy's own no-LLM
     rule on the same window and pairs, random entries with the same trade count and exit constants, and BTC buy-and-hold.
No look-ahead: the signals were made from candles up to t0 - 1h only (kronos-replay); forward returns here start at the close of the candle opened at t0 - 1h.
Replay never gives the status `edge` on its own trades; the verdict above is a label of the SIGNAL study, and `edge candidate` still means "worth a live dry-run", never "proven".
"""
from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from . import analysis, data as rdata, engine, kronos_replay as kr, paths

N_MIN = 200
BOOT_B = 2000
BOOT_SEED = 12345
HOURS = (1, 4, 24)
VERDICT_HORIZON = 4
VERDICTS = ("edge candidate", "unproven", "no edge", "insufficient n")
HOUR_MS = 3_600_000
ENGINE_BOTS = {"main": ("up", "buy"), "shorts": ("down", "sell_short")}       # the live `shorts` strategy stands in for shorts_v2 (same exits; v2 differs only in the prompt, which Kronos replaces) -- stated in the report


# ------------------------------------------------------------------ statistics
def cluster_ci(values: list[float], days: list[str], b: int = BOOT_B, seed: int = BOOT_SEED) -> dict | None:
    """Percentile bootstrap CI of the mean, resampling whole calendar days (ratio of sums, vectorised). Deterministic."""
    if len(values) < 2:
        return None
    uniq = sorted(set(days))
    idx = {d: i for i, d in enumerate(uniq)}
    s, c = np.zeros(len(uniq)), np.zeros(len(uniq))
    for v, d in zip(values, days):
        s[idx[d]] += v
        c[idx[d]] += 1
    rng = np.random.default_rng(seed)
    pick = rng.integers(0, len(uniq), size=(b, len(uniq)))
    means = s[pick].sum(axis=1) / c[pick].sum(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {"mean": float(sum(values) / len(values)), "lo": float(lo), "hi": float(hi), "n": len(values), "days": len(uniq)}


def direction_verdict(n: int, ci: dict | None, half_means: tuple[float | None, float | None]) -> str:
    if n < N_MIN or ci is None:
        return "insufficient n"
    if ci["lo"] > 0 and all(h is not None and h > 0 for h in half_means):
        return "edge candidate"
    if ci["hi"] < 0:
        return "no edge"
    return "unproven"


def overall_verdict(per_dir: dict[str, str]) -> str:
    v = list(per_dir.values())
    if "edge candidate" in v:
        return "edge candidate"
    if "unproven" in v:
        return "unproven"
    if "insufficient n" in v:
        return "insufficient n"
    return "no edge"


def closes_by_ms(store: rdata.SeriesStore, cat: str, sym: str) -> dict[int, float]:
    df = store.frames.get((cat, sym))
    if df is None:
        return {}
    return {int(t.timestamp() * 1000): float(c) for t, c in zip(df["date"].dt.to_pydatetime().tolist(), df["close"].tolist())}


def forward_rows(recs: list[dict], closes: dict[str, dict[int, float]]) -> list[dict]:
    """Non-skipped signals with their forward returns. price0 = close of the candle opened at t0-1h; forward at h = close of the candle opened at t0+h-1h over price0 - 1."""
    rows = []
    for r in recs:
        if r["signal"] == "skipped":
            continue
        cl = closes.get(r["symbol"]) or {}
        t0 = int(datetime.strptime(r["t0"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1000)
        p0 = cl.get(t0 - HOUR_MS)
        if p0 is None:
            continue
        fw = {}
        for h in HOURS:
            ph = cl.get(t0 + (h - 1) * HOUR_MS)
            fw[h] = None if ph is None else ph / p0 - 1.0
        rows.append({"t0": r["t0"], "pair": r["pair"], "signal": r["signal"], "ret_4h_pred": r["ret_4h"], "fwd": fw, "day": r["t0"][:10]})
    return rows


def signal_quality(rows: list[dict], start: datetime, end: datetime, fee_pct: float, slip_pct: float) -> dict:
    cost = 2 * (fee_pct + slip_pct) / 100.0
    mid = start + (end - start) / 2
    mid_s = mid.strftime("%Y-%m-%dT%H:%M:%SZ")
    bench: dict[tuple[str, int], float] = {}
    for h in HOURS:
        by_t: dict[str, list[float]] = {}
        for r in rows:
            if r["fwd"][h] is not None:
                by_t.setdefault(r["t0"], []).append(r["fwd"][h])
        for t, v in by_t.items():
            bench[(t, h)] = sum(v) / len(v)
    out: dict = {"cost_round_trip_pct": round(cost * 100, 3), "midpoint": mid_s, "verdict_horizon_h": VERDICT_HORIZON, "n_min": N_MIN, "by_direction": {}, "horizons": {}}
    n_all = {h: sum(1 for r in rows if r["fwd"][h] is not None) for h in HOURS}
    out["n_scored_rows"] = len(rows)
    out["unconditional_mean_pct"] = {str(h): round(float(np.mean([r["fwd"][h] for r in rows if r["fwd"][h] is not None])) * 100, 4) if n_all[h] else None for h in HOURS}
    verdict_net: dict[str, str] = {}
    verdict_gross: dict[str, str] = {}
    for sig, sign in (("up", 1.0), ("down", -1.0)):
        sel = [r for r in rows if r["signal"] == sig]
        per_h: dict = {}
        for h in HOURS:
            xs = [r for r in sel if r["fwd"][h] is not None and (r["t0"], h) in bench]
            raw = [sign * r["fwd"][h] for r in xs]
            exc = [sign * (r["fwd"][h] - bench[(r["t0"], h)]) for r in xs]
            net = [e - cost for e in exc]
            days = [r["day"] for r in xs]
            entry = {"n": len(xs), "forward_mean_pct": round(float(np.mean(raw)) * 100, 4) if xs else None, "hit_rate_pct": round(sum(1 for x in raw if x > 0) / len(xs) * 100, 1) if xs else None}
            for label, vals in (("excess_gross", exc), ("excess_net", net)):
                ci = cluster_ci(vals, days)
                naive = None
                h1 = [v for v, r in zip(vals, xs) if r["t0"] < mid_s]
                h2 = [v for v, r in zip(vals, xs) if r["t0"] >= mid_s]
                entry[label] = {"mean_pct": round(float(np.mean(vals)) * 100, 4) if vals else None, "ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
                                "days": None if ci is None else ci["days"], "first_half": {"n": len(h1), "mean_pct": round(float(np.mean(h1)) * 100, 4) if h1 else None},
                                "second_half": {"n": len(h2), "mean_pct": round(float(np.mean(h2)) * 100, 4) if h2 else None}}
                if h == VERDICT_HORIZON:
                    halves = (np.mean(h1) if h1 else None, np.mean(h2) if h2 else None)
                    v = direction_verdict(len(xs), ci, halves)
                    (verdict_net if label == "excess_net" else verdict_gross)[sig] = v
            per_h[str(h)] = entry
        out["by_direction"][sig] = {"signals_total": len(sel), "horizons": per_h, "verdict": verdict_net[sig], "signal_skill_gross": verdict_gross[sig]}
    out["verdict"] = overall_verdict(verdict_net)
    out["signal_skill_gross"] = overall_verdict(verdict_gross)
    n_short = {s: out["by_direction"][s]["signals_total"] for s in ("up", "down")}
    out["shortfall"] = {s: max(0, N_MIN - n) for s, n in n_short.items() if n < N_MIN}
    return out


# ------------------------------------------------------------------ the engine run
def decisions_for(bot: str, recs: list[dict]) -> list[dict]:
    """Signals -> the decisions the strategy would have got from its model: up -> buy (main), down -> sell_short (shorts), everything else hold. Skipped rows give no decision."""
    want, action = ENGINE_BOTS[bot]
    cat = engine.BOTS[bot]["category"]
    out = []
    for r in recs:
        if r["signal"] == "skipped":
            continue
        pair = rdata.pair_name(r["pair"].split("/")[0], cat)
        out.append({"bot": bot, "mode": "kronos", "t0": r["t0"], "ts": r["t0"], "pair": pair, "action": action if r["signal"] == want else "hold",
                    "reason": f"kronos {r['signal']} ret_4h {r['ret_4h']:+.4f}", "price": r.get("price")})
    return out


def covered_end(recs: list[dict], job: dict) -> datetime | None:
    """The end of the contiguous early part of the window for which EVERY pair has a record (the generator works in time order, so a stopped job still has a clean prefix)."""
    n_pairs = len(job["pairs"])
    count: dict[str, int] = {}
    for r in recs:
        count[r["t0"]] = count.get(r["t0"], 0) + 1
    t = kr._parse(job["start"])
    end = kr._parse(job["end"])
    last = None
    while t < end and count.get(kr._iso(t), 0) >= n_pairs:
        last = t
        t += timedelta(hours=1)
    return None if last is None else last + timedelta(hours=1)


def load_store(p: dict, get, spot_pairs: list[str], log=lambda s: None) -> tuple[rdata.SeriesStore, dict]:
    warm = p["start"] - timedelta(days=engine.WARMUP_DAYS)
    store = rdata.SeriesStore()
    by_cat: dict[str, list[str]] = {"spot": list(spot_pairs), "linear": []}
    for pair in spot_pairs:
        sym = rdata.bybit_symbol(pair)
        store.add("spot", sym, rdata.load_candles("spot", sym, warm, p["end"] + timedelta(hours=26), get))
        if p.get("minute_prices", True):
            try:
                store.add_fine("spot", sym, rdata.load_candles("spot", sym, p["start"] - timedelta(hours=1), p["end"] + timedelta(hours=2), get, interval="1"))
            except Exception as e:      # noqa: BLE001
                log(f"minute candles spot {sym}: unavailable ({type(e).__name__})")
        lin = rdata.pair_name(pair.split("/")[0], "linear")
        lsym = rdata.bybit_symbol(lin)
        try:
            df = rdata.load_candles("linear", lsym, warm, p["end"] + timedelta(hours=26), get)
            if len(df) < 100:
                raise ValueError("too few candles")
            store.add("linear", lsym, df)
            rdata.add_funding_to(store, lsym, p["start"] - timedelta(days=2), p["end"] + timedelta(hours=26), get, log)
            by_cat["linear"].append(lin)
            if p.get("minute_prices", True):
                try:
                    store.add_fine("linear", lsym, rdata.load_candles("linear", lsym, p["start"] - timedelta(hours=1), p["end"] + timedelta(hours=2), get, interval="1"))
                except Exception as e:      # noqa: BLE001
                    log(f"minute candles linear {lsym}: unavailable ({type(e).__name__})")
        except Exception as e:      # noqa: BLE001
            log(f"no perpetual for {pair}: {type(e).__name__} -- not traded by the shorts run")
    if p.get("minute_prices", True):
        for cat in ("spot", "linear"):
            try:
                for sym, sp in rdata.fetch_spreads(cat, get).items():
                    store.spreads[(cat, sym)] = sp
            except Exception as e:      # noqa: BLE001
                log(f"spreads {cat}: unavailable ({type(e).__name__})")
    if not store.has("spot", "BTCUSDT"):
        store.add("spot", "BTCUSDT", rdata.load_candles("spot", "BTCUSDT", warm, p["end"] + timedelta(hours=26), get))
    return store, by_cat


def run_engine(eval_id: str, p: dict, store: rdata.SeriesStore, by_cat: dict, recs: list[dict], strategies_dir=None, stop_event=None, log=lambda s: None) -> tuple[dict, dict]:
    from . import runner
    results: dict = {}
    runs: list = []
    for bot in ENGINE_BOTS:
        cat = engine.BOTS[bot]["category"]
        if not by_cat.get(cat):
            continue
        for mode in ("kronos", "rule"):
            pr = {"every_n": 1, "workers": 1, "seed": p["seed"], "fee_pct": p["fee_pct"], "slippage_pct": p["slippage_pct"], "rule": mode == "rule", "frozen": mode == "kronos"}
            live = None
            if mode == "kronos":
                ds = [d for d in decisions_for(bot, recs) if d["pair"] in set(by_cat[cat])]
                live = engine.LiveDecisions(ds, [])
            r = engine.BotRun(bot, mode, pr, store, None, None, by_cat[cat], eval_id, log=lambda s: None, stop_event=stop_event, base=strategies_dir, live=live)
            runs.append(r)
            results[(bot, mode)] = r.run(p["start"], p["end"])
            log(f"engine {bot}/{mode}: {len(results[(bot, mode)]['trades'])} trades")
    report = runner.build_report(eval_id, {**p, "frozen_from": None}, results, store, by_cat, {"calls": 0, "cache_hits": 0, "usd": 0, "label": "kronos-small signals"}, runs, strategies_dir=strategies_dir)
    return results, report


# ------------------------------------------------------------------ the whole evaluation
def evaluate(raw: dict, get, strategies_dir=None, d: Path | None = None, stop_event=None, log=lambda s: None, store_and_pairs=None) -> dict:
    d = Path(d or kr.kdir())
    job = kr.read_json(d / "job.json")
    if not job:
        raise kr.KronosError("no Kronos job yet: generate the signals first")
    recs = kr.read_signals(d)
    ce = covered_end(recs, job)
    if ce is None:
        raise kr.KronosError("no complete hour of signals yet (every pair needs a record for an hour to count)")
    start, job_end = kr._parse(job["start"]), kr._parse(job["end"])
    end = min(ce, job_end)
    fee, slip = float(raw.get("fee_pct", 0.1)), float(raw.get("slippage_pct", 0.3))
    recs = [r for r in recs if kr._parse(r["t0"]) < end]
    p = {"start": start, "end": end, "days": max(1, round((end - start).total_seconds() / 86400)), "fee_pct": fee, "slippage_pct": slip, "seed": int(raw.get("seed", 12345)),
         "pairs": len(job["pairs"]), "every_n": 1, "bots": list(ENGINE_BOTS), "modes": ["kronos", "rule"], "minute_prices": bool(raw.get("minute_prices", True))}
    spot_pairs = [x["pair"] for x in job["pairs"]]
    store, by_cat = store_and_pairs if store_and_pairs else load_store(p, get, spot_pairs, log)
    closes = {rdata.bybit_symbol(pr): closes_by_ms(store, "spot", rdata.bybit_symbol(pr)) for pr in spot_pairs}
    rows = forward_rows(recs, closes)
    quality = signal_quality(rows, start, end, fee, slip)
    n_sig = {s: sum(1 for r in recs if r["signal"] == s) for s in ("up", "down", "neutral", "skipped")}
    eval_id = f"keval-{kr._iso(end).replace(':', '').replace('-', '')}-{len(recs)}"
    results, report = ({}, {"bots": {}, "equity_curves": {}, "baselines": {}})
    if raw.get("engine", True):
        results, report = run_engine(eval_id, p, store, by_cat, recs, strategies_dir, stop_event, log)
    full_days = (job_end - start).days
    out = {"eval_id": eval_id, "job_id": job["id"], "model": "NeoQuasar/Kronos-small (24.7M, context 512, 3 samples, seed 12345)", "verdict": quality["verdict"], "signal_skill_gross": quality["signal_skill_gross"],
           "window": {"start": kr._iso(start), "end": kr._iso(end), "days_covered": round((end - start).total_seconds() / 86400, 1), "days_planned": full_days, "partial": end < job_end},
           "params": {"fee_pct_per_side": fee, "slippage_pct_per_side": slip, "signal_threshold_pct": job["threshold"] * 100, "signal_horizon_h": gen_horizon(), "verdict_rule": "see module docstring"},
           "pairs": spot_pairs, "pairs_with_perpetual": by_cat.get("linear", []), "signals": n_sig, "quality": quality, "engine": report.get("bots", {}), "equity_curves": report.get("equity_curves", {}),
           "baselines": {**report.get("baselines", {}), "llm_earlier_runs": earlier_llm_runs(start, end)},
           "notes": ["signals are made from candles up to t0 - 1h only; forward returns start at the close of that candle",
                     "the live `shorts` strategy is the stand-in for shorts_v2 here (same exits; v2 differs only in the model prompt, which Kronos replaces)",
                     "verdict is a label of the signal study: `edge candidate` means worth a live dry-run, never proven; replay never gives `edge`",
                     "the Kronos training data are not documented: a positive result on recent history can still be partly memory"] + (["PARTIAL WINDOW: the generator has not finished"] if end < job_end else [])
                    + ([f"insufficient n: need {N_MIN} signals per direction, short by {quality['shortfall']}; extend the Kronos run to 60 days"] if quality["shortfall"] else [])}
    write_outputs(d, eval_id, p, out, results, recs)
    return out


def gen_horizon() -> int:
    return VERDICT_HORIZON


def earlier_llm_runs(start: datetime, end: datetime) -> list[dict]:
    """Earlier LLM replay runs of main / shorts under REPLAY_DIR, listed WITH their own window and model: they are never the same window, so they are context, not a comparison."""
    out = []
    try:
        for rd in sorted(paths.root().iterdir()):
            f = rd / "report.json"
            if not f.is_file() or rd.name in ("kronos", "cache"):
                continue
            rep = json.loads(f.read_text(encoding="utf-8"))
            if rep.get("type") != "replay":
                continue
            for bot in ("main", "shorts"):
                for mode, e in (rep.get("bots", {}).get(bot) or {}).items():
                    if isinstance(e, dict) and e.get("n_trades") is not None and mode in ("named", "anonymized"):
                        out.append({"run": rd.name, "bot": bot, "mode": mode, "model": e.get("model"), "window": [rep["params"]["start"], rep["params"]["end"]], "n_trades": e["n_trades"],
                                    "mean_trade_pct": e["mean_trade_pct"], "ci95_pct": e["ci95_pct"], "label": "LLM from an earlier run (its own window and model)"})
    except OSError:
        pass
    return out


def to_markdown(out: dict) -> str:
    q = out["quality"]
    L = [f"# Kronos evaluation {out['eval_id']}", "", f"**Verdict: {out['verdict']}** (net of {q['cost_round_trip_pct']}% round-trip cost, {q['verdict_horizon_h']}h horizon, per rule fixed in advance). Signal skill before costs: **{out['signal_skill_gross']}**.", "",
         f"Model: {out['model']}. Window {out['window']['start']} .. {out['window']['end']} ({out['window']['days_covered']} of {out['window']['days_planned']} days{', PARTIAL' if out['window']['partial'] else ''}), {len(out['pairs'])} pairs.",
         f"Signals: {out['signals']}. Threshold |ret_4h| >= {out['params']['signal_threshold_pct']}%.", "", "## Signal quality (independent of exits)", "",
         "Forward return after the signal, direction-adjusted; excess = minus the mean of all pairs at the same hour; CI = bootstrap over calendar days.", "",
         "| direction | horizon | n | forward mean % | hit % | gross excess % [95% CI] | net excess % [95% CI] | half 1 net % (n) | half 2 net % (n) |", "|---|---|---|---|---|---|---|---|---|"]
    for sig, dd in q["by_direction"].items():
        for h, e in dd["horizons"].items():
            g, n = e["excess_gross"], e["excess_net"]
            L.append(f"| {sig} | {h}h | {e['n']} | {e['forward_mean_pct']} | {e['hit_rate_pct']} | {g['mean_pct']} {g['ci95_pct']} | {n['mean_pct']} {n['ci95_pct']} | {n['first_half']['mean_pct']} ({n['first_half']['n']}) | {n['second_half']['mean_pct']} ({n['second_half']['n']}) |")
        L.append(f"| **{sig}** | verdict | | | | gross: {dd['signal_skill_gross']} | net: **{dd['verdict']}** | | |")
    if q["shortfall"]:
        L += ["", f"**insufficient n**: short by {q['shortfall']} signals per direction (need {q['n_min']}); extend the Kronos run to 60 days."]
    L += ["", "## Replay engine: Kronos as the decision source (same exits, fees, slippage)", "", "| bot | decisions | trades | win % | mean/trade % | 95% CI % | net $ | max DD % | vs random (pct) |", "|---|---|---|---|---|---|---|---|---|"]
    for bot, modes in out["engine"].items():
        for mode in ("kronos", "rule"):
            e = modes.get(mode)
            if e:
                L.append(f"| {bot} / {mode} | {e['n_decisions']} | {e['n_trades']} | {e['win_pct']} | {e['mean_trade_pct']} | {e['ci95_pct']} | {e['net_usd']} | {e['max_drawdown_pct']} | {e['vs_random_percentile']} |")
    btc = (out["baselines"].get("btc_buy_and_hold") or {}).get("return_pct")
    L += ["", f"BTC buy-and-hold over the window: {btc}%", ""]
    if out["baselines"].get("llm_earlier_runs"):
        L += ["LLM replay runs found (labelled, other windows/models):"] + [f"- {x['run']} {x['bot']}/{x['mode']} {x['model']}: {x['n_trades']} trades, mean {x['mean_trade_pct']}% window {x['window'][0][:10]}..{x['window'][1][:10]}" for x in out["baselines"]["llm_earlier_runs"]] + [""]
    else:
        L += ["LLM earlier runs: none in this REPLAY_DIR.", ""]
    L += ["## Notes", ""] + [f"- {x}" for x in out["notes"]]
    return "\n".join(L) + "\n"


def write_outputs(d: Path, eval_id: str, p: dict, out: dict, results: dict, recs: list[dict]) -> None:
    base = Path(d) / "eval" / eval_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "params.json").write_text(json.dumps({**p, "start": kr._iso(p["start"]), "end": kr._iso(p["end"])}, indent=1), encoding="utf-8")
    (base / "report.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    (base / "report.md").write_text(to_markdown(out), encoding="utf-8")
    with (base / "signals.jsonl").open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")
    with (base / "decisions.jsonl").open("w", encoding="utf-8") as fh:
        for res in results.values():
            for x in res["decisions"]:
                fh.write(json.dumps(x) + "\n")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["bot", "mode", "id", "pair", "side", "open_date", "close_date", "open_rate", "close_rate", "stake", "profit_ratio", "profit_abs", "fees_abs", "exit_reason"])
    for (bot, mode), res in sorted(results.items()):
        for t in res["trades"]:
            cw.writerow([bot, mode, t["id"], t["pair"], t["side"], t["open_date"], t["close_date"], t["open_rate"], t["close_rate"], t["stake"], t["profit_ratio"], t["profit_abs"], t["fees_abs"], t["exit_reason"]])
    (base / "trades.csv").write_text(buf.getvalue(), encoding="utf-8")
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["bot", "mode", "ts", "equity"])
    for (bot, mode), res in sorted(results.items()):
        for ts, e in res["equity"]:
            cw.writerow([bot, mode, ts, round(e, 4)])
    (base / "equity.csv").write_text(buf.getvalue(), encoding="utf-8")


def zip_eval(eval_id: str, d: Path | None = None) -> bytes | None:
    base = Path(d or kr.kdir()) / "eval" / eval_id
    if not base.is_dir() or not all(c.isalnum() or c in "-_" for c in eval_id):
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(base.iterdir()):
            z.write(f, f.name)
    return buf.getvalue()


def latest_eval(d: Path | None = None) -> dict | None:
    base = Path(d or kr.kdir()) / "eval"
    if not base.is_dir():
        return None
    reps = sorted(base.glob("*/report.json"), key=lambda f: f.stat().st_mtime)
    return json.loads(reps[-1].read_text(encoding="utf-8")) if reps else None
