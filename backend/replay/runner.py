"""
The replay job: validation, cost estimate, the single background run, status, stop, run listing, ZIP download. One run at a time; the run is a THREAD inside
survival-agent (there is no Docker socket); LLM parallelism is bounded by REPLAY_WORKERS through the shared client. Everything is written under REPLAY_DIR
through paths.replay_path(); this module never imports the allocator / BotControl and never calls a live bot.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analysis, calibration, carry, data as rdata, delist_study, engine, kronos_eval, fade_exit_study, kronos_replay, lgbm_grid, lgbm_hourly, lgbm_premium, lgbm_study, listing_fade, maker_study, live_data, momentum, paths, sandbox
from .llm import LLMClient, Provider, ProviderConfigError, prices_from_env, provider_from_env

MAX_DAYS = 60
MIN_DAYS = 7
EVERY_N_CHOICES = (1, 2, 4, 8)
MODES = ("named", "anonymized")
MAX_VARIANTS = 12
MECHANICS_ONLY = ("moonshot",)       # replay trades liquid coins, live Moonshot trades new listings: flagged `universe_mismatch`, kept out of the summary
CHARS_PER_TOKEN = 3.5            # a rough conversion for the estimate; the real cost always comes from the API's own usage field
OUT_TOKENS_PER_CALL = 25
SECONDS_PER_CALL = 2.0           # until a pilot has measured the real latency


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def limits() -> dict:
    return {"max_usd": env_float("REPLAY_MAX_USD", 10.0), "workers": int(env_float("REPLAY_WORKERS", 8)), "min_start": os.environ.get("REPLAY_MIN_START") or "2026-08-01",      # compose passes ${VAR:-}: an empty value means "default"
            "max_calls": int(env_float("REPLAY_MAX_CALLS", 200000)), "fee_pct": env_float("REPLAY_FEE_PCT", 0.1), "slippage_pct": env_float("REPLAY_SLIPPAGE_PCT", 0.05),
            "max_days": MAX_DAYS, "price_in_per_m": prices_from_env()[0], "price_out_per_m": prices_from_env()[1]}


class ParamError(ValueError):
    pass


def _clean_variants(raw) -> list[dict]:
    out, seen = [], set()
    for i, v in enumerate(raw or [{"name": "baseline"}]):
        if not isinstance(v, dict):
            raise ParamError("a variant must be an object")
        name = "".join(ch for ch in str(v.get("name") or f"v{i + 1}") if ch.isalnum() or ch in "_-.+ ")[:40] or f"v{i + 1}"
        if name in seen:
            raise ParamError(f"duplicate variant name {name!r}")
        seen.add(name)
        ex: dict = {}
        if v.get("rotate") is False:
            ex["rotate"] = False
        if v.get("min_hold_hours") not in (None, ""):
            mh = float(v["min_hold_hours"])
            if mh < 0 or mh > 240:
                raise ParamError("min_hold_hours must be 0..240")
            ex["min_hold_hours"] = mh
        if v.get("roi"):
            try:
                ex["roi"] = {str(int(k)): float(x) for k, x in dict(v["roi"]).items()}
            except (TypeError, ValueError):
                raise ParamError("roi must be a {minutes: ratio} table")
            if not ex["roi"] or any(x <= 0 for x in ex["roi"].values()):
                raise ParamError("roi values must be positive ratios")
        if v.get("stoploss") not in (None, ""):
            sl = float(v["stoploss"])
            if not (-0.9 <= sl < 0):
                raise ParamError("stoploss must be a negative ratio between -0.9 and 0")
            ex["stoploss"] = sl
        tr = v.get("trailing") or {}
        if tr.get("enabled"):
            ex["trailing"] = {"enabled": True, "positive": float(tr.get("positive", 0.01)), "offset": float(tr.get("offset", 0.0)), "only_offset_is_reached": bool(tr.get("only_offset_is_reached", False))}
            if not (0 < ex["trailing"]["positive"] < 0.5) or not (0 <= ex["trailing"]["offset"] < 1):
                raise ParamError("trailing: positive must be 0..0.5, offset 0..1")
        out.append({"name": name, "exit": ex})
    if len(out) > MAX_VARIANTS:
        raise ParamError(f"at most {MAX_VARIANTS} variants per run")
    return out


def load_source_run(run_id: str) -> tuple[dict, list[dict]]:
    """The params and the recorded decisions of an earlier run (exit variants re-run these decisions without a model)."""
    try:
        prm = json.loads(paths.replay_path(run_id, "params.json").read_text(encoding="utf-8"))
        decs = [json.loads(x) for x in paths.replay_path(run_id, "decisions.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, ValueError, paths.ReplayPathError):
        raise ParamError(f"source run {run_id!r} has no params.json / decisions.jsonl")
    if prm.get("type") in ("calibration", "exit_variants") or not decs:
        raise ParamError(f"source run {run_id!r} is not a replay run with decisions")
    return prm, decs


def normalize_params(raw: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    lim = limits()
    if raw.get("allocator"):
        raise ParamError("phase 4 (allocator over simulated days) is not implemented yet: it starts only after phases 1-3 are accepted")
    if raw.get("frozen_from"):                         # exit variants on the decisions of an earlier run: window, step, universe come from that run
        prm, decs = load_source_run(str(raw["frozen_from"]))
        modes_in = sorted({d.get("mode") for d in decs if d.get("mode") in MODES + ("rule",)})          # the no-LLM rule's decisions can be re-run with other exits too ($0)
        bots_in = sorted({d["bot"] for d in decs})
        bots = [b for b in (raw.get("bots") or bots_in) if b in bots_in]
        if not bots:
            raise ParamError("none of the chosen bots has decisions in the source run")
        modes_default = [m for m in modes_in if m != "rule"] or modes_in                      # the model's modes by default; the rule's decisions only when asked for ("rule" in modes)
        modes = [m for m in (raw.get("modes") or modes_default) if m in modes_in] or modes_default
        return {"frozen_from": str(raw["frozen_from"]), "bots": bots, "modes": modes, "days": int(prm["days"]), "start": datetime.fromisoformat(prm["start"]), "end": datetime.fromisoformat(prm["end"]),
                "every_n": int(prm["every_n"]), "pairs": int(prm["pairs"]), "fee_pct": float(prm["fee_pct"]), "slippage_pct": float(prm["slippage_pct"]), "seed": int(prm.get("seed", 12345)),
                "variants": _clean_variants(raw.get("variants")), "add_rule": False, "minute_prices": bool(raw.get("minute_prices", True)), "max_usd": 0.0, "max_calls": 0, "workers": lim["workers"],
                "provider": "frozen", "funding": bool(raw.get("funding", prm.get("funding", False)))}          # the source run's own setting: exit variants must differ from it ONLY in the exits
    if "bots" in raw and not raw["bots"]:
        raise ParamError("choose at least one bot (none is ticked)")
    bots = [b for b in (raw.get("bots") or [b for b, s in engine.BOTS.items() if s["default"]]) if b in engine.BOTS]
    if not bots:
        raise ParamError("choose at least one bot")
    days = int(raw.get("days", 30))
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise ParamError(f"days must be {MIN_DAYS}..{MAX_DAYS}")
    latest_end = (now - timedelta(hours=48)).replace(minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(str(raw["end"]).replace("Z", "+00:00")) if raw.get("end") else latest_end
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    if end > latest_end:
        raise ParamError(f"the window must end at least 48h before now (latest {latest_end:%Y-%m-%d %H:%M} UTC) so that 24h returns exist")
    start = end - timedelta(days=days)
    min_start = datetime.fromisoformat(lim["min_start"]).replace(tzinfo=timezone.utc)
    if start < min_start:
        raise ParamError(f"the window may not start before {lim['min_start']} (REPLAY_MIN_START: the model's training cut-off is unknown)")
    modes = [m for m in raw.get("modes", ["named", "anonymized"]) if m in MODES]
    if not modes and not (raw.get("rule_only") and raw.get("add_rule", True)):          # no model mode is valid only as an explicit rule-only run ($0)
        raise ParamError("choose named, anonymized or both")
    every_n = int(raw.get("every_n", 4))
    if every_n not in EVERY_N_CHOICES:
        raise ParamError(f"every_n must be one of {EVERY_N_CHOICES}")
    pairs_n = max(3, min(50, int(raw.get("pairs", 20))))
    provider = str(raw.get("provider") or "deepseek").lower()
    if provider not in ("deepseek", "openai_compatible"):
        raise ParamError("provider must be deepseek or openai_compatible")
    max_usd = min(float(raw.get("max_usd", lim["max_usd"])), lim["max_usd"])
    max_calls = min(int(raw.get("max_calls", lim["max_calls"])), lim["max_calls"])
    return {"bots": bots, "days": days, "start": start, "end": end, "modes": modes, "every_n": every_n, "pairs": pairs_n, "max_usd": max_usd, "max_calls": max_calls,
            "workers": lim["workers"], "fee_pct": float(raw.get("fee_pct", lim["fee_pct"])), "slippage_pct": float(raw.get("slippage_pct", lim["slippage_pct"])),
            "seed": int(raw.get("seed", 12345)), "provider": provider, "add_rule": bool(raw.get("add_rule", True)), "minute_prices": bool(raw.get("minute_prices", True)),
            "funding": bool(raw.get("funding", True))}            # perpetual funding served as-of to the shorts bots (prompt line + the strategy's funding veto); runs made before P22 had none


def n_steps(p: dict) -> int:
    return math.ceil(p["days"] * 24 / p["every_n"])


def estimate(p: dict, sample_chars: float | None, price_in: float | None, price_out: float | None, provider: Provider | None = None, candidates: dict | None = None) -> dict:
    """candidates: {bot: number of rule candidates in the window} for the v2 short bots -- only candidates reach the model, so their calls are candidates x LLM modes."""
    cand = candidates or {}
    calls = 0 if p.get("frozen_from") else sum((cand[b] if b in cand else p["pairs"] * n_steps(p)) * len(p["modes"]) for b in p["bots"])
    tin = (sample_chars / CHARS_PER_TOKEN) if sample_chars else None
    usd = None
    if calls == 0:
        usd = 0.0
    elif tin is not None and price_in is not None and price_out is not None:
        usd = calls * (tin * price_in + OUT_TOKENS_PER_CALL * price_out) / 1e6
    workers = max(1, p["workers"])
    free = price_in == 0 and price_out == 0
    rpm = provider.rpm if provider else None
    secs = calls * SECONDS_PER_CALL / workers
    if rpm:
        secs = max(secs, calls / rpm * 60)
    cap = provider.daily_cap if provider else None
    return {"calls": calls, "avg_input_tokens": None if tin is None else round(tin), "usd": None if usd is None else round(usd, 3), "seconds": round(secs),
            "prices_known": calls == 0 or (price_in is not None and price_out is not None), "usd_limit_applies": not free,
            "within_limits": calls <= p["max_calls"] and (usd is not None and (free or usd <= p["max_usd"])),
            "candidates": cand or None, "model": provider.label if provider else None, "rpm": rpm, "daily_cap": cap, "days_needed_for_daily_cap": None if not cap or not calls else math.ceil(calls / cap),
            "price_note": "the displayed price is conservative (top tier, no cache discount): the real DeepSeek bill per key is 2-3x lower -- check it against the Usage export",
            "note": "the number of calls is exact; tokens come from real prompts built on a few samples, the price from REPLAY_PRICE_*_PER_M, the time is a rough guide; "
                    "already-cached prompts cost nothing; a rule (no-LLM) run and exit variants on frozen decisions make no model call"}


class _Capture:
    """A stand-in LLM that only measures prompt sizes (no API call, no cost)."""

    def __init__(self):
        self.chars: list[int] = []

    def call(self, system, user, temperature=0.1, max_tokens=300):
        self.chars.append(len(system) + len(user))
        return "hold: sample"


def resolve_provider(p: dict) -> tuple[Provider | None, float | None, float | None]:
    """(provider, price_in, price_out) for a run. DeepSeek: prices from REPLAY_PRICE_*_PER_M. A free provider: prices 0 unless set (the USD limit then does not apply; the call limits do)."""
    if p.get("frozen_from"):
        return None, 0.0, 0.0
    prov = provider_from_env(p.get("provider"))
    if prov.kind == "deepseek":
        pin, pout = prices_from_env()
    else:                                              # another model: its own prices (REPLAY_LLM_PRICE_*), default 0 = free; DeepSeek's prices never apply to it
        def f(name):
            try:
                return float((os.environ.get(name) or "").strip() or 0.0)
            except ValueError:
                return 0.0
        pin, pout = f("REPLAY_LLM_PRICE_IN_PER_M"), f("REPLAY_LLM_PRICE_OUT_PER_M")
    return prov, pin, pout


def estimate_for(raw: dict, http_get=None, strategies_dir=None, now: datetime | None = None) -> dict:
    """The 'Estimate cost' button: validates, builds a few REAL prompts per bot (candles come from the free Bybit API, nothing is sent to the LLM),
    and turns the exact call count into tokens and dollars with REPLAY_PRICE_*_PER_M."""
    import requests
    get = http_get or requests.get
    p = normalize_params(raw, now)
    if p.get("frozen_from"):
        est = estimate(p, None, 0.0, 0.0, None)
        est["params"] = {**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}
        est["sampled_bots"] = []
        return est
    if not p["modes"]:                                           # an explicit rule-only run: nothing is sent to a model
        est = estimate(p, None, 0.0, 0.0, None)
        est["params"] = {**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}
        est["sampled_bots"] = []
        return est
    try:
        prov, pin, pout = resolve_provider(p)
    except ProviderConfigError as e:
        raise ParamError(str(e))
    mid = p["start"] + (p["end"] - p["start"]) / 2
    mid = mid.replace(minute=0, second=0, microsecond=0)
    chars: list[float] = []
    fng = rdata.fng_history(get)
    cand: dict[str, int] = {}
    for bot in p["bots"]:
        if bot in analysis.V2_BOTS and p["modes"]:                  # P22-A: count the rule candidates over the real window (cached candles, no model)
            cat_v2 = engine.BOTS[bot]["category"]
            cs = rdata.SeriesStore()
            names = [rdata.pair_name(c, cat_v2) for c in rdata.top_coins(cat_v2, p["pairs"], get)]
            for nm in names:
                sym = rdata.bybit_symbol(nm)
                cs.add(cat_v2, sym, rdata.load_candles(cat_v2, sym, p["start"] - timedelta(days=2), p["end"] + timedelta(hours=1), get))
            cand[bot] = analysis.count_candidates(cs, cat_v2, names, p["start"], p["end"], p["every_n"])
    for bot in p["bots"]:
        spec = engine.BOTS[bot if bot not in analysis.V2_BOTS else analysis.V2_PARENT[bot]]          # a v2 bot's prompt is the parent's plus ~150 characters: sample the parent
        cat = spec["category"]
        store = rdata.SeriesStore()
        coins = rdata.top_coins(cat, 3, get)
        for c in coins:
            store.add(cat, f"{c}USDT", rdata.load_candles(cat, f"{c}USDT", mid - timedelta(days=engine.WARMUP_DAYS), mid + timedelta(hours=2), get))
        cap = _Capture()
        run = engine.BotRun(spec["key"], "named", {"every_n": 1, "workers": 1, "seed": p["seed"]}, store, fng, cap, [rdata.pair_name(c, cat) for c in coins], "estimate", base=strategies_dir)
        run.ctx.clock.now = mid
        run.ctx.df_asof = mid
        for pair in run.pairs:
            run.decide(pair, mid)
        if cap.chars:
            chars.append(sum(cap.chars) / len(cap.chars))
    avg_chars = sum(chars) / len(chars) if chars else None
    est = estimate(p, avg_chars + (150 if any(b in analysis.V2_BOTS for b in p["bots"]) else 0) if avg_chars else avg_chars, pin, pout, prov, cand)
    est["params"] = {**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}
    est["sampled_bots"] = p["bots"]
    return est


class Manager:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.state: dict = {"phase": "idle"}
        self.log = deque(maxlen=50)
        self.progress: dict = {}
        self.client: LLMClient | None = None
        self.t0 = 0.0

    # --------------------------------------------------------------- status
    def _log(self, line: str) -> None:
        self.log.append(line)
        run_id = self.state.get("run_id")
        if run_id:
            try:
                with open(paths.ensure_parent(paths.replay_path(run_id, "log.txt")), "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def status(self) -> dict:
        snap = self.client.snapshot() if self.client else {}
        done = sum(v["step"] for v in self.progress.values())
        total = sum(v["total"] for v in self.progress.values())
        elapsed = time.time() - self.t0 if self.t0 else 0
        rate = (sum(v["decisions"] for v in self.progress.values()) / (elapsed / 60)) if elapsed > 5 else 0
        return {**self.state, "progress": {"steps_done": done, "steps_total": total, "fraction": round(done / total, 4) if total else 0,
                                           "decisions": sum(v["decisions"] for v in self.progress.values()), "trades": sum(v["trades"] for v in self.progress.values()),
                                           "decisions_per_min": round(rate, 1), "per_bot": self.progress},
                "llm": snap, "elapsed_s": round(elapsed), "eta_s": round(elapsed * (total - done) / done) if done and total else None, "log": list(self.log)}

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    # ---------------------------------------------------------------- start
    def start(self, params: dict, http_get=None, post=None, strategies_dir=None) -> str:
        with self.lock:
            if self.running():
                raise ParamError("a replay is already running (one at a time)")
            frozen = bool(params.get("frozen_from"))
            prov = pin = pout = None
            if not frozen and not params["modes"]:                  # an explicit rule-only run: no model, no key, no price needed
                prov, pin, pout = Provider("rule", "-", "rule-only"), 0.0, 0.0
            elif not frozen:
                try:
                    prov, pin, pout = resolve_provider(params)
                except ProviderConfigError as e:
                    raise ParamError(str(e))
                if post is None and not prov.api_key:
                    raise ParamError(("DEEPSEEK_API_KEY" if prov.kind == "deepseek" else "REPLAY_LLM_API_KEY") + " is not set in this container")
                if pin is None or pout is None:
                    raise ParamError("REPLAY_PRICE_IN_PER_M / REPLAY_PRICE_OUT_PER_M are not set: the dollar limit cannot be enforced without prices")
            run_id = ("fz-" if frozen else "") + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            ser = {**params, "start": params["start"].isoformat(), "end": params["end"].isoformat(), "type": "exit_variants" if frozen else "replay",
                   "model": None if frozen else prov.label}
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps(ser, indent=1), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.t0 = time.time()
            self.client = None if frozen else LLMClient(prov.api_key, params["max_usd"], params["max_calls"], pin, pout, workers=params["workers"], post=post, stop_event=self.stop_event,
                                                        provider=prov)
            self.state = {"phase": "starting", "run_id": run_id, "status": "running", "params": ser}
            self.thread = threading.Thread(target=self._execute_frozen if frozen else self._execute, args=(run_id, params, http_get, strategies_dir), daemon=True, name=f"replay-{run_id}")
            self.thread.start()
            return run_id

    def start_calibration(self, bots: list[str] | None, date_from: str | None, date_to: str | None, http_get=None) -> str:
        """The 'calibration' run type: no LLM, no cost. It reads ONLY the live snapshot inside the sandbox (written by 'Update live reference')."""
        with self.lock:
            if self.running():
                raise ParamError("a replay is already running (one at a time)")
            snap = live_data.read_snapshot()
            if snap is None:
                raise ParamError("no live snapshot yet: press 'Update live reference' first (it reads the bots' logs and trades once)")
            chosen = [b for b in (bots or calibration.BOTS) if b in calibration.BOTS and b in snap["bots"] and snap["bots"][b].get("decisions")]
            if not chosen:
                raise ParamError("none of the chosen bots has logged decisions in the snapshot")
            for d in (date_from, date_to):
                if d and live_data.utc(d) is None:
                    raise ParamError("from / to must be dates like 2026-09-29")
            run_id = "cal-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            params = {"type": "calibration", "bots": chosen, "from": date_from, "to": date_to, "cost_usd": 0, "snapshot": snap.get("_meta", {})}
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps(params, indent=1), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "starting", "run_id": run_id, "status": "running", "params": params}
            self.thread = threading.Thread(target=self._execute_cal, args=(run_id, params, snap, http_get), daemon=True, name=f"calibration-{run_id}")
            self.thread.start()
            return run_id

    def _execute_cal(self, run_id: str, params: dict, snap: dict, http_get) -> None:
        try:
            import requests
            self.state["phase"] = "calibrating (candles, then each mechanics variant)"
            self._log(f"calibration {run_id}: {params['bots']} from {params['from'] or 'first full hour'} to {params['to'] or 'last decision hour'}; no LLM, no cost")
            out = calibration.run_calibration(snap["bots"], snap.get("_meta", {}), params["bots"], params["from"], params["to"], http_get or requests.get, run_id,
                                              log=self._log, progress=self._on_progress, stop_event=self.stop_event)
            self.state["phase"] = "writing the report"
            calibration.write_outputs(run_id, out["report"], out["variants"], params)
            self.state.update({"phase": "done", "status": "stopped" if self.stop_event.is_set() else "completed"})
            self._log("done: calibration report written")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------- P22-B: Listing Fade study (rule based, $0)
    def start_lf_fetch(self, http_get=None, now: datetime | None = None, pace: float = 0.12, sleep=time.sleep, base=None) -> str:
        """Fetch (or resume fetching) the public Bybit data of the Listing Fade study into REPLAY_DIR/listing_fade/. No key, no cost."""
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "starting", "run_id": None, "kind": "lf_fetch", "status": "running", "lf": {}}
            self.thread = threading.Thread(target=self._execute_lf_fetch, args=(http_get, now, pace, sleep, base), daemon=True, name="lf-fetch")
            self.thread.start()
            return "lf-fetch"

    def _execute_lf_fetch(self, http_get, now, pace, sleep, base) -> None:
        try:
            import requests

            def prog(**k):
                self.state["lf"] = dict(k)
                self.state["phase"] = k.get("phase", self.state["phase"])
                total = {"instruments": 1, "hourly candles + funding": max(1, k.get("listings_found", 1))}.get(k.get("phase"), max(1, k.get("m5_total", 1)))
                step = k.get("hourly_done", 0) if k.get("phase") == "hourly candles + funding" else k.get("m5_done", 0)
                self.progress = {"lf_fetch/data": {"step": step, "total": total, "decisions": k.get("api_calls", 0), "trades": k.get("symbol_errors", 0)}}
            res = listing_fade.fetch_all(http_get or requests.get, now or datetime.now(timezone.utc), base, self._log, prog, self.stop_event, pace, sleep)
            self.state.update({"phase": "done", "status": "stopped" if res["stopped"] else "completed", "lf_result": res})
            self._log(f"done: {res['listings']} listings, {res['api_calls']} API calls, {res['api_errors']} API errors, {res['symbol_errors']} symbol errors")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    def start_lf_study(self, raw: dict, base=None, until: datetime | None = None) -> str:
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            if not listing_fade.lf_state().get("data_ready"):
                raise ParamError("no Listing Fade data yet: press 'Fetch data' first")
            slips = [float(x) for x in (raw.get("slippages_pct") or listing_fade.SLIPPAGES_PCT)]
            if not slips or any(x < 0 or x > 5 for x in slips):
                raise ParamError("slippage values must be between 0 and 5 percent")
            snap = live_data.read_snapshot()
            live = [t for t in ((snap or {}).get("bots", {}).get("listing_fade", {}) or {}).get("trades", [])]
            params = {"slippages_pct": slips, "fee_pct": float(raw.get("fee_pct", listing_fade.FEE_TAKER_PCT)), "live_trades": live}
            run_id = "lf-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps({**{k: v for k, v in params.items() if k != "live_trades"}, "type": "listing_fade_study", "cost_usd": 0}), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "simulating", "run_id": run_id, "kind": "lf_study", "status": "running", "params": {"type": "listing_fade_study", "slippages_pct": slips}}
            self.thread = threading.Thread(target=self._execute_lf_study, args=(run_id, params, base, until), daemon=True, name=f"lf-study-{run_id}")
            self.thread.start()
            return run_id

    def _execute_lf_study(self, run_id: str, params: dict, base, until) -> None:
        try:
            self._log(f"listing fade study {run_id}: slippages {params['slippages_pct']}%, fee {params['fee_pct']}% per side; from the cache, no network, no model")
            out = listing_fade.run_study(run_id, params, base, until, self._log)
            self.state["phase"] = "writing the report"
            listing_fade.write_outputs(run_id, params, out)
            self.state.update({"phase": "done", "status": "completed"})
            self._log(f"done: verdict {out['report']['verdict']}")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------- P26: Listing Fade exit-mechanics study (from the Listing Fade cache, no network, no model)
    def start_fade_exit_study(self, raw: dict | None = None, base=None, until: datetime | None = None) -> str:
        raw = raw or {}
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            if not listing_fade.lf_state().get("data_ready"):
                raise ParamError("no Listing Fade data yet: press 'Fetch data' in the Listing Fade card first")
            params = {"fee_pct": float(raw.get("fee_pct", fade_exit_study.FEE))}
            if raw.get("split"):
                try:
                    datetime.fromisoformat(str(raw["split"]))
                except ValueError:
                    raise ParamError("split must be a date like 2025-07-01")
                params["split"] = str(raw["split"])
            run_id = "fx-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps({**params, "type": "fade_exit_study", "cost_usd": 0}), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "simulating", "run_id": run_id, "kind": "fade_exit_study", "status": "running", "params": {"type": "fade_exit_study"}}
            self.thread = threading.Thread(target=self._execute_fade_exit, args=(run_id, params, base, until), daemon=True, name=f"fade-exit-{run_id}")
            self.thread.start()
            return run_id

    def _execute_fade_exit(self, run_id: str, params: dict, base, until) -> None:
        try:
            self._log(f"fade exit study {run_id}: 8 exit variants on the Listing Fade history, from the cache, no network, no model")
            rep = fade_exit_study.run_study(run_id, params, base, until, self._log)
            self.state["phase"] = "writing the report"
            fade_exit_study.write_outputs(run_id, params, rep)
            self.state.update({"phase": "done", "status": "completed"})
            self._log(f"done: verdict {rep['verdict']}")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------- P25: funding carry study (rule based, $0): fetch the public history, then simulate
    def start_carry_study(self, raw: dict | None = None, http_get=None, now: datetime | None = None, pace: float = 0.12, sleep=time.sleep) -> str:
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            run_id = "carry-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            params = {"since": (raw or {}).get("since") or carry.SINCE}
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps({**params, "type": "carry_study", "cost_usd": 0}), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "fetching", "run_id": run_id, "kind": "carry_study", "status": "running", "params": {"type": "carry_study"}}
            self.thread = threading.Thread(target=self._execute_carry_study, args=(run_id, params, http_get, now, pace, sleep), daemon=True, name=f"carry-{run_id}")
            self.thread.start()
            return run_id

    def _execute_carry_study(self, run_id, params, http_get, now, pace, sleep) -> None:
        try:
            import requests

            def prog(**k):
                self.progress = {"carry/data": {"step": k.get("step", 0), "total": max(1, k.get("total", 1)), "decisions": k.get("api_calls", 0), "trades": k.get("symbol_errors", 0)}}
            now = now or datetime.now(timezone.utc)
            self._log(f"carry study {run_id}: public Bybit history (perp + index daily bars, funding), cached and resumable, no key, no model")
            res = carry.fetch_all(http_get or requests.get, now, self._log, prog, self.stop_event, pace, sleep)
            if res["stopped"]:
                self.state.update({"phase": "done", "status": "stopped"})
                return
            self.state["phase"] = "simulating"
            rep = carry.run_study(run_id, params, now, self._log)
            self.state["phase"] = "writing the report"
            carry.write_outputs(run_id, params, rep)
            self.state.update({"phase": "done", "status": "completed"})
            self._log(f"done: verdict {rep['verdict']}")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------- P25: delisting short study and momentum study (rule based, $0): fetch the public history, then simulate
    def start_study(self, kind: str, raw: dict | None = None, http_get=None, now: datetime | None = None, pace: float = 0.05, sleep=time.sleep) -> str:
        """kind: 'delist_study' | 'momentum_study' | 'lgbm_study' | 'lgbm_hourly_study' | 'lgbm_grid_study' | 'lgbm_premium_study' | 'maker_study'. Both fetch public Bybit data into REPLAY_DIR (resumable, no key) and then simulate; neither calls a model."""
        mods = {"delist_study": ("delist-", delist_study, delist_study.fetch_all), "momentum_study": ("mom-", momentum, carry.fetch_all), "lgbm_study": ("lgbm-", lgbm_study, carry.fetch_all),
            "lgbm_hourly_study": ("lgbh-", lgbm_hourly, lgbm_hourly.fetch_all),
            "lgbm_grid_study": ("lgg-", lgbm_grid, lgbm_grid.fetch_all), "lgbm_premium_study": ("lgp-", lgbm_premium, lgbm_premium.fetch_all),
            "maker_study": ("mk-", maker_study, maker_study.fetch_all)}
        if kind not in mods:
            raise ParamError(f"unknown study {kind!r}")
        prefix, mod, fetch = mods[kind]
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            run_id = prefix + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            params = {"since": (raw or {}).get("since") or mod.SINCE}
            paths.ensure_parent(paths.replay_path(run_id, "params.json")).write_text(json.dumps({**params, "type": kind, "cost_usd": 0}), encoding="utf-8")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "fetching", "run_id": run_id, "kind": kind, "status": "running", "params": {"type": kind}}
            self.thread = threading.Thread(target=self._execute_study, args=(kind, mod, fetch, run_id, params, http_get, now, pace, sleep), daemon=True, name=f"{kind}-{run_id}")
            self.thread.start()
            return run_id

    def _execute_study(self, kind, mod, fetch, run_id, params, http_get, now, pace, sleep) -> None:
        try:
            import requests

            def prog(**k):
                self.progress = {f"{kind}/data": {"step": k.get("step", 0), "total": max(1, k.get("total", 1)), "decisions": k.get("api_calls", 0), "trades": k.get("symbol_errors", 0)}}
            now = now or datetime.now(timezone.utc)
            self._log(f"{kind} {run_id}: public Bybit history, cached and resumable, no key, no model")
            res = fetch(http_get or requests.get, now, self._log, prog, self.stop_event, pace, sleep)
            if res.get("stopped"):
                self.state.update({"phase": "done", "status": "stopped"})
                return
            self.state["phase"] = "simulating"
            rep = mod.run_study(run_id, params, now, self._log)
            self.state["phase"] = "writing the report"
            mod.write_outputs(run_id, params, rep)
            self.state.update({"phase": "done", "status": "completed"})
            self._log(f"done: verdict {rep['verdict']}")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    # ------------------------------------------------------- P22-K3: evaluation of the historical Kronos signals ($0, CPU only)
    def start_kronos_eval(self, raw: dict, http_get=None, strategies_dir=None, d=None) -> str:
        with self.lock:
            if self.running():
                raise ParamError("a replay job is already running (one at a time)")
            if not kronos_replay.read_json((d or kronos_replay.kdir()) / "job.json"):
                raise ParamError("no Kronos signals yet: press 'Generate signals' first")
            self.stop_event = threading.Event()
            self.log.clear()
            self.progress = {}
            self.client = None
            self.t0 = time.time()
            self.state = {"phase": "evaluating", "run_id": None, "kind": "kronos_eval", "status": "running", "params": {"type": "kronos_eval", "cost_usd": 0}}
            self.thread = threading.Thread(target=self._execute_kronos_eval, args=(raw, http_get, strategies_dir, d), daemon=True, name="kronos-eval")
            self.thread.start()
            return "kronos-eval"

    def _execute_kronos_eval(self, raw: dict, http_get, strategies_dir, d) -> None:
        try:
            import requests
            self._log("kronos evaluation: signal quality + replay engine (main / shorts), no model, no cost")
            out = kronos_eval.evaluate(raw, http_get or requests.get, strategies_dir, d, self.stop_event, self._log)
            self.state.update({"phase": "done", "status": "completed", "kronos_eval": {"eval_id": out["eval_id"], "verdict": out["verdict"]}})
            self._log(f"done: verdict {out['verdict']} (signal skill before costs: {out['signal_skill_gross']})")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    def stop(self) -> None:
        self.stop_event.set()

    # -------------------------------------------------------------- the run
    def _load_market(self, p: dict, get, pairs_by_cat: dict | None = None) -> tuple[rdata.SeriesStore, dict]:
        """Hourly candles (and, unless switched off, minute candles) for the universe; cached under REPLAY_DIR/cache."""
        warm_start = p["start"] - timedelta(days=engine.WARMUP_DAYS)
        store = rdata.SeriesStore()
        cats = sorted({engine.BOTS[b]["category"] for b in p["bots"]})
        by_cat: dict[str, list[str]] = {}
        for cat in cats:
            if pairs_by_cat and pairs_by_cat.get(cat):
                names = list(pairs_by_cat[cat])
            else:
                coins = rdata.top_coins(cat, p["pairs"], get)
                if "BTC" not in coins and cat == "spot":
                    coins = ["BTC"] + coins[:-1]
                names = [rdata.pair_name(c, cat) for c in coins]
            by_cat[cat] = names
            for pair in names:
                sym = rdata.bybit_symbol(pair)
                store.add(cat, sym, rdata.load_candles(cat, sym, warm_start, p["end"] + timedelta(hours=26), get))
                self._log(f"candles {cat} {sym}: {len(store.frames[(cat, sym)])} hourly")
                if cat == "linear" and p.get("funding", True):
                    rdata.add_funding_to(store, sym, p["start"] - timedelta(days=2), p["end"] + timedelta(hours=26), get, self._log)
                if p.get("minute_prices", True):
                    try:
                        store.add_fine(cat, sym, rdata.load_candles(cat, sym, p["start"] - timedelta(hours=1), p["end"] + timedelta(hours=2), get, interval="1"))
                    except Exception as e:      # noqa: BLE001
                        self._log(f"minute candles {cat} {sym}: unavailable ({type(e).__name__}) -- hourly prices are used for that pair")
        if p.get("minute_prices", True):
            for cat in cats:
                try:
                    for sym, sp in rdata.fetch_spreads(cat, get).items():
                        store.spreads[(cat, sym)] = sp
                except Exception as e:      # noqa: BLE001
                    self._log(f"spreads {cat}: unavailable ({type(e).__name__}) -- entries fill at the minute price")
        if not store.has("spot", "BTCUSDT"):
            store.add("spot", "BTCUSDT", rdata.load_candles("spot", "BTCUSDT", warm_start, p["end"] + timedelta(hours=26), get))
        return store, by_cat

    def _execute(self, run_id: str, p: dict, http_get, strategies_dir) -> None:
        try:
            import requests
            get = http_get or requests.get
            self.state["phase"] = "downloading candles"
            modes = list(p["modes"]) + (["rule"] if p.get("add_rule", True) else [])
            self._log(f"replay {run_id}: {p['bots']} x {modes} {p['days']}d every {p['every_n']} candle(s), {p['pairs']} pairs, model {self.client.provider.label}, limit ${p['max_usd']}")
            store, pairs_by_cat = self._load_market(p, get)
            fng = rdata.fng_history(get)
            self.state["phase"] = "replaying"
            runs: list[engine.BotRun] = []
            for bot in p["bots"]:
                for mode in modes:
                    pr = {"every_n": p["every_n"], "workers": p["workers"], "seed": p["seed"], "fee_pct": p["fee_pct"], "slippage_pct": p["slippage_pct"], "rule": mode == "rule"}
                    runs.append(engine.BotRun(bot, mode, pr, store, fng, None if mode == "rule" else self.client, pairs_by_cat[engine.BOTS[bot]["category"]], run_id, log=self._log,
                                              progress=self._on_progress, stop_event=self.stop_event, base=strategies_dir))
            results: dict[tuple, dict] = {}
            threads = []

            def go(r: engine.BotRun):
                results[(r.bot, r.mode)] = r.run(p["start"], p["end"])
            for r in runs:
                th = threading.Thread(target=go, args=(r,), daemon=True)
                th.start()
                threads.append(th)
            for th in threads:
                th.join()
            self.state["phase"] = "analysing"
            report = build_report(run_id, p, results, store, pairs_by_cat, self.client.snapshot(), runs, strategies_dir=strategies_dir)
            write_outputs(run_id, p, results, report)
            statuses = {v["status"] for v in results.values()}
            self.state.update({"phase": "done", "status": "stopped_budget" if "stopped_budget" in statuses else "stopped" if "stopped" in statuses else "completed"})
            self._log(f"done: {self.state['status']}, ${self.client.snapshot()['usd']} spent")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    def _execute_frozen(self, run_id: str, p: dict, http_get, strategies_dir) -> None:
        """Exit variants on the recorded decisions of an earlier run: the same decision steps and pairs, no model, only the exit rules differ."""
        try:
            import requests
            get = http_get or requests.get
            self.state["phase"] = "loading the source run and candles"
            src_prm, decs = load_source_run(p["frozen_from"])
            try:
                universe = json.loads(paths.replay_path(p["frozen_from"], "report.json").read_text(encoding="utf-8")).get("universe") or {}
            except (OSError, ValueError):
                universe = {}
            store, pairs_by_cat = self._load_market(p, get, universe)
            by_key: dict[tuple, list[dict]] = {}
            for d in decs:
                if d["bot"] in p["bots"] and d.get("mode") in p["modes"]:
                    by_key.setdefault((d["bot"], d["mode"]), []).append({**d, "t0": d["ts"], "confidence": None})
            variants_out: dict = {}
            self.state["phase"] = "re-running the exits"
            for v in p["variants"]:
                runs, results = [], {}
                for (bot, mode), ds in sorted(by_key.items()):
                    pr = {"every_n": p["every_n"], "workers": 1, "seed": p["seed"], "fee_pct": p["fee_pct"], "slippage_pct": p["slippage_pct"], "frozen": True, "exit": v["exit"]}
                    r = engine.BotRun(bot, mode, pr, store, None, None, pairs_by_cat[engine.BOTS[bot]["category"]], run_id, log=lambda s: None,
                                      progress=lambda _v=v["name"], **k: self._on_progress(**{**k, "mode": f"{_v}/{k['mode']}"}), stop_event=self.stop_event, base=strategies_dir,
                                      live=engine.LiveDecisions(ds, []))
                    runs.append(r)
                    results[(bot, mode)] = r.run(p["start"], p["end"])
                self._log(f"variant {v['name']}: " + ", ".join(f"{b}/{m} {len(x['trades'])} trades" for (b, m), x in sorted(results.items())))
                variants_out[v["name"]] = {"exit": v["exit"], "results": results,
                                           "report": build_report(run_id, p, results, store, pairs_by_cat, {"calls": 0, "cache_hits": 0, "usd": 0}, runs, strategies_dir=strategies_dir)}
            self.state["phase"] = "analysing"
            report = build_variants_report(run_id, p, variants_out)
            write_variant_outputs(run_id, p, variants_out, report)
            self.state.update({"phase": "done", "status": "stopped" if self.stop_event.is_set() else "completed"})
            self._log("done: exit variants on frozen decisions, no model calls")
        except Exception as e:      # noqa: BLE001
            self._log(f"FAILED: {type(e).__name__}: {e}")
            self.state.update({"phase": "failed", "status": "failed", "error": f"{type(e).__name__}: {e}"})

    def _on_progress(self, **k) -> None:
        self.progress[f"{k['bot']}/{k['mode']}"] = {"step": k["step"], "total": k["total"], "decisions": k["decisions"], "trades": k["trades"]}


MANAGER = Manager()


# ----------------------------------------------------------------- reporting
ALL_MODES = ("named", "anonymized", "rule")
FROZEN_NOTE = ("frozen decisions: the decisions of the source run are re-used as they are; the effect of the trades' results on the prompt (the 'recent performance' block) is NOT recomputed")


def rule_formula(bot: str, base=None) -> list[str]:
    """The exact no-LLM rule of a bot, copied from its strategy file (the `is_backtest` placeholder branch of populate_entry_trend)."""
    try:
        src = sandbox.strategy_path(engine.BOTS[bot], base).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for i, line in enumerate(src):
        if "if is_backtest:" in line:
            out = []
            for l2 in src[i + 1:]:
                if l2.strip() == "return dataframe":
                    break
                if l2.strip() and not l2.strip().startswith("#"):
                    out.append(l2.strip())
            return out
    return []


def build_report(run_id: str, p: dict, results: dict, store: rdata.SeriesStore, pairs_by_cat: dict, llm_snap: dict, runs: list, strategies_dir=None) -> dict:
    btc = analysis.btc_hold(store, p["start"], p["end"])
    ref = analysis.load_live_reference(paths.root())
    by_run = {(r.bot, r.mode): r for r in runs}
    bots_out: dict = {}
    curves: dict = {}
    model = llm_snap.get("label")
    for (bot, mode), res in sorted(results.items()):
        stats = analysis.trade_stats(res["trades"], res["wallet0"], res["equity"])
        spec = engine.BOTS[bot]
        short_share = (sum(1 for t in res["trades"] if t["side"] == "short") / len(res["trades"])) if res["trades"] else 0.0
        rnd = analysis.random_entries(len(res["trades"]), short_share, store, spec["category"], pairs_by_cat[spec["category"]], p["start"], p["end"],
                                      res["constants"]["minimal_roi"], res["constants"]["stoploss"], p["fee_pct"] / 100, p["slippage_pct"] / 100, seed=p["seed"]) if res["trades"] else None
        ledger = analysis.run_ledger_audit(bot, res["decisions"], store, spec["category"], p["end"]) if mode != "rule" else {"entry": {}, "status": "n/a (rule, no model)"}
        entry = {"mode": mode, "model": "rule (no LLM)" if mode == "rule" else (model or "frozen decisions"), "run_status": res["status"], "steps": f"{res['steps_done']}/{res['steps_total']}",
                 "n_decisions": len(res["decisions"]), "actions": {a: sum(1 for d in res["decisions"] if d["action"] == a) for a in ("buy", "sell_short", "hold")}, **stats,
                 "open_at_end": res["open_at_end"], "status": analysis.replay_status(stats), "signal_status": ledger["status"], "ledger_audit": ledger["entry"], "exit_reasons": res["exit_counts"],
                 "constants": res["constants"], "overrides": res.get("overrides") or {}, "max_open_trades": res["max_open_trades"], "wallet0": res["wallet0"], "notes": res["notes"],
                 "blocked_urls": res["blocked_urls"], "universe_mismatch": bot in MECHANICS_ONLY,
                 "random_baseline": None if rnd is None else {k: v for k, v in rnd.items() if k != "_samples"},
                 "vs_random_percentile": None if rnd is None or stats["mean_trade_pct"] is None else analysis.percentile_rank(rnd["_samples"], stats["mean_trade_pct"]),
                 "divergence_from_live": analysis.divergence(bot, res["trades"], ref)}
        assert entry["status"] != analysis.NEVER and entry["signal_status"] != analysis.NEVER
        if bot in analysis.V2_BOTS:
            entry["candidates"] = analysis.candidate_report(res["decisions"], store, spec["category"], p["seed"]) if mode != "rule" else analysis.candidate_report(
                [{**d, "candidate": d["action"] == "sell_short", "vote": "confirm" if d["action"] == "sell_short" else "skip"} for d in res["decisions"]], store, spec["category"], p["seed"])
        bots_out.setdefault(bot, {})[mode] = entry
        eq = res["equity"]
        step = max(1, len(eq) // 300)
        curves.setdefault(bot, {})[mode] = [[ts, round(v / res["wallet0"], 5)] for ts, v in eq[::step]]
    llm_vs_rule: dict = {}
    for bot, modes in bots_out.items():
        named, anon, rule = modes.get("named"), modes.get("anonymized"), modes.get("rule")
        modes["leak_suspect"] = analysis.leak_suspect(named, anon)
        if named and rule and named["mean_trade_pct"] is not None and rule["mean_trade_pct"] is not None:
            llm_vs_rule[bot] = {"named_mean_pct": named["mean_trade_pct"], "rule_mean_pct": rule["mean_trade_pct"], "diff_pp": round(named["mean_trade_pct"] - rule["mean_trade_pct"], 4),
                                    "named_ci95_pct": named["ci95_pct"], "rule_ci95_pct": rule["ci95_pct"],
                                    "llm_ci_above_rule_mean": bool(named["ci95_pct"] and named["ci95_pct"][0] > rule["mean_trade_pct"]),
                                    "question": "does the LLM beat a simple rule on the same signals, window, pairs, exits and fees?"}
    return {"run_id": run_id, "type": "exit_variant_part" if p.get("frozen_from") else "replay", "banner": analysis.BANNER, "banner_en": analysis.BANNER_EN, "model": model,
            "llm_provider": {k: llm_snap.get(k) for k in ("provider", "model", "label", "rpm", "daily_cap", "url_host")},
            "params": {**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}, "llm": llm_snap, "bots": bots_out, "equity_curves": curves,
            "mechanics_check_only": [b for b in bots_out if b in MECHANICS_ONLY], "llm_vs_rule": llm_vs_rule,
            "rule_formulas": {b: rule_formula(b, strategies_dir) for b in bots_out},
            "baselines": {"btc_buy_and_hold": btc, "random_entries": "per bot above (200 repeats, same trade count and exit constants, no rotation exits)",
                          "no_llm_rule": "mode `rule`: the strategy's own placeholder rule (formulas above), same window, step, pairs, exits and fees, no rotation",
                          "controls": "not computed in the app: the control strategies need talib/qtpylib (not in the survival-agent image); compare with the live control bots or a "
                                      "Freqtrade backtest in the backtest/ venv"},
            "universe": pairs_by_cat, "limits_of_replay": [
                "no news archive: the prompt says 'unavailable for a historical replay'; this tests price/indicator/F&G judgement, not what the bot did that day",
                "mechanics follow the live Freqtrade loop (decisions, then exits, then entries; entries ~50 s after the candle open; Freqtrade's stake formula); minute candles price the fills "
                "and order ROI / stoploss inside the hour when downloaded (calibrated on the live bots' real decisions, see the calibration report); no min-notional, partial fills or funding",
                "pair list = today's most liquid coins (a fixed snapshot), not a day-by-day volume list",
                "Moonshot trades new listings live but liquid coins here: its results are flagged `universe_mismatch` and kept out of the summary (mechanics check only)",
                "possible knowledge leakage: compare named vs anonymized; an edge that exists only when named is suspect and invalid",
                "results of different models are never comparable as if equal: every row names its model; the real bots run on DeepSeek",
                "replay never gives the status `edge`"]}


def _row(e: dict) -> list:
    return [e["mode"], e.get("model"), e["n_decisions"], e["n_trades"], e["win_pct"], e["mean_trade_pct"], e["ci95_pct"], e["fees_usd"], e["net_usd"], e["max_drawdown_pct"], e["vs_random_percentile"],
            e["status"], e["signal_status"]]


def to_markdown(rep: dict) -> str:
    L = [f"# Replay {rep['run_id']}", "", f"**{rep['banner_en']}**", "", f"**{rep['banner']}**", "",
         f"Window {rep['params']['start']} .. {rep['params']['end']}, every {rep['params']['every_n']} candle(s), {rep['params']['pairs']} pairs, fee {rep['params']['fee_pct']}% + slippage {rep['params']['slippage_pct']}% per side",
         f"Model: **{rep.get('model')}** (every row names its model; never compare different models as if equal). LLM: {rep['llm'].get('calls')} paid calls, {rep['llm'].get('cache_hits')} cache hits, ${rep['llm'].get('usd')}", ""]
    head = ["| bot | mode | model | decisions | trades | win% | mean/trade % | 95% CI % | fees $ | net $ | max DD % | vs random (pct) | status | signal status |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]

    def table(bots):
        out = list(head)
        for bot in bots:
            modes = rep["bots"][bot]
            for mode in ALL_MODES:
                e = modes.get(mode)
                if e:
                    r = _row(e)
                    out.append(f"| {bot} | {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} | {r[8]} | {r[9]} | {r[10]} | {r[11]} | {r[12]} |")
            if modes.get("leak_suspect"):
                out.append(f"| {bot} | | | | | | | | | | | | **LEAK SUSPECT: positive only when named -- result invalid** | |")
            lr = rep.get("llm_vs_rule", {}).get(bot)
            if lr:
                out.append(f"| {bot} | LLM vs rule | | | | | {lr['diff_pp']} pp | | | | | | named {lr['named_mean_pct']} vs rule {lr['rule_mean_pct']} | {'LLM CI above the rule mean' if lr['llm_ci_above_rule_mean'] else 'LLM not clearly above the rule'} |")
        return out
    main = [b for b in rep["bots"] if b not in rep.get("mechanics_check_only", [])]
    side = [b for b in rep["bots"] if b in rep.get("mechanics_check_only", [])]
    L += table(main)
    if side:
        L += ["", "## Different universe -- mechanics check only (not in the summary, no conclusions)", "", "Live Moonshot trades new listings; replay trades liquid coins (`universe_mismatch`).", ""] + table(side)
    btc = rep["baselines"]["btc_buy_and_hold"]
    v2 = [b for b in rep["bots"] if b in analysis.V2_BOTS]
    for b in v2:
        named = (rep["bots"][b].get("named") or {}).get("candidates")
        rule_c = (rep["bots"][b].get("rule") or {}).get("candidates")
        if named:
            L += ["", f"## {b}: does the model's veto add anything over the bare rule? (P22-A2)", "",
                  f"Rule candidates {named['n_candidates']} of {named['n_decisions']} decisions; votes {named['votes']}; share vetoed {named['share_vetoed']}. **{named['veto_adds']}.**", "",
                  "| horizon | all candidates (bare rule), short return % | confirmed: n, mean %, 95% CI | vetoed: n, mean %, 95% CI | confirmed minus vetoed, pp [95% CI] |", "|---|---|---|---|---|"]
            for h in ("h1", "h4", "h24"):
                f = named["forward_short_return_pct"].get(h) or {}
                c, v, a = f.get("confirmed", {}), f.get("vetoed", {}), f.get("all_candidates (bare rule)", {})
                vv = named["veto_value"].get(h) or {}
                L.append(f"| {h} | {a.get('mean_pct')} (n {a.get('n')}) | {c.get('n')}, {c.get('mean_pct')}, {c.get('ci95_pct')} | {v.get('n')}, {v.get('mean_pct')}, {v.get('ci95_pct')} | {vv.get('confirmed_minus_vetoed_pp')} {vv.get('ci95_pp')} |")
            L += ["", f"Rule: {named['rule']}"]
        elif rule_c:
            L += ["", f"## {b}: rule-only run (no model): {rule_c['n_candidates']} candidates; short forward return of ALL candidates: " + ", ".join(f"{h} {((rule_c['forward_short_return_pct'].get(h) or {}).get('all_candidates (bare rule)') or {}).get('mean_pct')}%" for h in ('h1', 'h4', 'h24'))]
    L += ["", f"BTC buy-and-hold over the window: {btc['return_pct'] if btc else 'n/a'}%", "", "## No-LLM rules (exact formulas from the strategy files)", ""]
    for bot, lines in rep.get("rule_formulas", {}).items():
        L.append(f"- {bot}: `" + " ".join(lines) + "`")
    L += ["", "## Limits", ""] + [f"- {x}" for x in rep["limits_of_replay"]]
    return "\n".join(L) + "\n"


def write_outputs(run_id: str, p: dict, results: dict, report: dict) -> None:
    def w(name: str, text: str):
        paths.ensure_parent(paths.replay_path(run_id, name)).write_text(text, encoding="utf-8")
    w("report.json", json.dumps(report, indent=1, default=str))
    w("report.md", to_markdown(report))
    w("decisions.jsonl", "".join(json.dumps(d) + "\n" for res in results.values() for d in res["decisions"]))
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["bot", "mode", "id", "pair", "side", "open_date", "close_date", "open_rate", "close_rate", "stake", "profit_ratio", "profit_abs", "fees_abs", "exit_reason", "enter_tag"])
    for (bot, mode), res in sorted(results.items()):
        for t in res["trades"]:
            cw.writerow([bot, mode, t["id"], t["pair"], t["side"], t["open_date"], t["close_date"], t["open_rate"], t["close_rate"], t["stake"], t["profit_ratio"], t["profit_abs"], t["fees_abs"],
                         t["exit_reason"], t["enter_tag"]])
    w("trades.csv", buf.getvalue())
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["bot", "mode", "ts", "equity"])
    for (bot, mode), res in sorted(results.items()):
        for ts, e in res["equity"]:
            cw.writerow([bot, mode, ts, round(e, 4)])
    w("equity.csv", buf.getvalue())


# ------------------------------------------------------------- exit variants
def build_variants_report(run_id: str, p: dict, variants_out: dict) -> dict:
    """Bot x variant comparison on the SAME frozen decisions: trades, mean, CI, net, drawdown, percentile vs random entries."""
    rows = []
    for vname, v in variants_out.items():
        for bot, modes in v["report"]["bots"].items():
            for mode in ALL_MODES:
                e = modes.get(mode) if isinstance(modes, dict) else None
                if e:
                    rows.append({"variant": vname, "bot": bot, "mode": mode, "model": e["model"], "n_trades": e["n_trades"], "mean_trade_pct": e["mean_trade_pct"], "ci95_pct": e["ci95_pct"],
                                 "net_usd": e["net_usd"], "net_return_pct": e["net_return_pct"], "max_drawdown_pct": e["max_drawdown_pct"], "vs_random_percentile": e["vs_random_percentile"],
                                 "status": e["status"], "exit_reasons": e["exit_reasons"], "universe_mismatch": e["universe_mismatch"]})
    first = next(iter(variants_out.values()))["report"]
    return {"run_id": run_id, "type": "exit_variants", "source_run": p["frozen_from"], "banner": analysis.BANNER, "banner_en": analysis.BANNER_EN, "note": FROZEN_NOTE,
            "params": {**p, "start": p["start"].isoformat(), "end": p["end"].isoformat()}, "llm": {"calls": 0, "cache_hits": 0, "usd": 0.0, "note": "no model call is made in this run type"},
            "variants": {n: {"overrides": v["exit"], "bots": v["report"]["bots"]} for n, v in variants_out.items()}, "comparison": rows,
            "mechanics_check_only": first["mechanics_check_only"], "only_exits_vary": "entry conditions are never changed: the decisions are those of the source run",
            "baselines": first["baselines"], "universe": first["universe"], "limits_of_replay": first["limits_of_replay"] + [FROZEN_NOTE]}


def variants_markdown(rep: dict) -> str:
    L = [f"# Exit variants {rep['run_id']} (source run {rep['source_run']})", "", f"**{rep['banner_en']}**", "", f"> {rep['note']}", "", "Model calls: 0. Only exits vary; entries are the source run's decisions.", ""]
    for vname, v in rep["variants"].items():
        L.append(f"- **{vname}**: overrides {json.dumps(v['overrides']) if v['overrides'] else 'none (the strategy defaults)'}")
    L += ["", "| bot | mode | variant | trades | mean/trade % | 95% CI % | net $ | max DD % | vs random (pct) | status |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["comparison"]:
        if r["universe_mismatch"]:
            continue
        L.append(f"| {r['bot']} | {r['mode']} | {r['variant']} | {r['n_trades']} | {r['mean_trade_pct']} | {r['ci95_pct']} | {r['net_usd']} | {r['max_drawdown_pct']} | {r['vs_random_percentile']} | {r['status']} |")
    side = [r for r in rep["comparison"] if r["universe_mismatch"]]
    if side:
        L += ["", "## Different universe -- mechanics check only", "", "| bot | mode | variant | trades | mean/trade % | net $ |", "|---|---|---|---|---|---|"]
        L += [f"| {r['bot']} | {r['mode']} | {r['variant']} | {r['n_trades']} | {r['mean_trade_pct']} | {r['net_usd']} |" for r in side]
    L += ["", "## Limits", ""] + [f"- {x}" for x in rep["limits_of_replay"]]
    return "\n".join(L) + "\n"


def write_variant_outputs(run_id: str, p: dict, variants_out: dict, report: dict) -> None:
    def w(name: str, text: str):
        paths.ensure_parent(paths.replay_path(run_id, name)).write_text(text, encoding="utf-8")
    w("report.json", json.dumps(report, indent=1, default=str))
    w("report.md", variants_markdown(report))
    buf = io.StringIO()
    cw = csv.writer(buf, lineterminator="\n")
    cw.writerow(["variant", "bot", "mode", "pair", "side", "open_date", "close_date", "open_rate", "close_rate", "stake", "profit_ratio", "profit_abs", "fees_abs", "exit_reason"])
    for vname, v in variants_out.items():
        for (bot, mode), res in sorted(v["results"].items()):
            for t in res["trades"]:
                cw.writerow([vname, bot, mode, t["pair"], t["side"], t["open_date"], t["close_date"], t["open_rate"], t["close_rate"], t["stake"], t["profit_ratio"], t["profit_abs"], t["fees_abs"], t["exit_reason"]])
    w("trades.csv", buf.getvalue())


# ----------------------------------------------------------------- runs / zip
def list_runs() -> list[dict]:
    out = []
    base = paths.root()
    if not base.is_dir():
        return out
    for d in sorted((x for x in base.iterdir() if x.is_dir() and x.name != "cache"), reverse=True):
        try:
            prm = json.loads((d / "params.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rep = None
        if (d / "report.json").is_file():
            try:
                rep = json.loads((d / "report.json").read_text(encoding="utf-8"))
            except ValueError:
                rep = None
        out.append({"run_id": d.name, "type": prm.get("type", "replay"), "params": prm, "has_report": rep is not None, "usd": (rep or {}).get("llm", {}).get("usd"), "calls": (rep or {}).get("llm", {}).get("calls")})
    return out


def get_run(run_id: str) -> dict | None:
    p = paths.replay_path(run_id, "report.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def zip_run(run_id: str) -> bytes | None:
    d = paths.replay_path(run_id)
    if not d.is_dir():
        return None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ("decisions.jsonl", "trades.csv", "equity.csv", "calibration_trades.csv", "report.json", "report.md", "params.json", "log.txt"):
            f = d / name
            if f.is_file():
                zf.write(f, name)
    return buf.getvalue()
