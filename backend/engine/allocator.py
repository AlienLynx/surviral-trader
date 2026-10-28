"""
P20-B -- LLM allocator.

Decides a weight 0..1 per managed bot once a day (00:40 UTC, after the brain) and on events (drawdown ladder
rung, macro event within 24h, liquidation cascade). The LLM only PROPOSES. Hard rules written in code have
the last word, every step is logged, and an invalid reply means "no change".

What a weight does (enforce mode only):
  weight 0      POST /stopentry on the bot (Freqtrade pauses new entries; exits and stops keep working)
  back above 0  POST /start (Freqtrade 2026.8: /stopentry pauses, /start resumes; there is no /startentry). If the bot still
                looks paused afterwards (older builds zero max_open_trades and answer 'already running')
                POST /reload_config and verify via /show_config
  0 < weight<1  position-size multiplier, read by custom_stake_amount from the shared weights file
Entry/exit logic, stops, ROI, thresholds and prompts of the bots are NOT touched. Signals keep being written
to the P19 ledger at weight 0.

Modes (PORTFOLIO_BRAIN_MODE): off | shadow (default) | enforce.
  shadow   decisions, hypothetical weights and the counterfactual are written; no bot is touched and the
           shared weights file carries mode=shadow, which the bots ignore.
  enforce  refuses to run (and the service refuses to start) if any managed bot reports dry_run=false; if a
           bot's dry_run cannot be verified the run stays in shadow. A kill switch (POST
           /api/portfolio/allocator/kill) drops back to shadow, resumes paused bots and resets weights to 1.
Policies (ALLOCATOR_POLICY, dashboard switch):
  grow     (default) weights from the bots' own results, never from a portfolio-level rule that idles the whole book. A bot that earns money (>= 30 closed trades since its
           configuration last changed, net > 0 now and over the last 7 days) is raised to 1.5x (2.0x when its evidence status is `edge`); a bot that loses (>= 30 trades,
           net < 0 over the period and over the last 7 days) is stopped; a stopped bot gets a trial at 0.5x after >= 3 days when its control bot made money over the last 7
           days (the market suits that style), and is confirmed or stopped again after the trial; every other bot trades at full size (1.0). Portfolio protection only SCALES:
           x0.5 from -15% below the (roster-restated) high-water mark, x0.25 from -25%, never 0; macro window cap 0.5; opposite positions / coin concentration cap 0.5. No LLM call.
  protect  the original rule set above (LLM proposal + hard rules; weekly loss, ladder and CPPI can pause every bot).
  grow is forced back to protect for a run whose dry-run check finds a bot that is live or cannot be verified (real money is never governed by the grow rules).
Control bots (control_long, control_short) and trend_ensemble (trend v1) are never managed: every control
call raises ControlBotError, they are not in the weights file and receive no context.

Hard rules (veto over the LLM), applied in this order:
  1. weight change per day <= +-25 pp vs the previous final weight (LLM proposals only)
  2. absolute safety caps, which may cut by more than 25 pp:
       drawdown ladder  -3% HWM: cap 0.75 | -6%: LLM cohort 0, others cap 0.5 | -8%: all 0
       weekly -2%: all 0 (pause entries) | monthly -4%: cap 0.5
       CPPI budget (floor 90%): gross > budget scales every weight by budget/gross; all 0 only when gross >= 1.5x budget
                or the floor is breached (budget 0)
       macro_event_within_24h: cap 0.5
       opposite positions in one coin: the bots on the smaller side get 0
       coin concentration >= COIN_LIMIT of equity: bots holding it cap 0.5 (bot-level: a weight cannot block one coin)
  3. weights below 0.05 become 0 (a pause), values rounded to 0.01
Targets (0.25%/week, 1%/month) only ever protect gains; missing a target never raises risk.

Usefulness test (counterfactual): every managed bot's closed trades re-weighted by the weight in force when it
opened ("with allocator") versus weight 1 ("without"), net of fees, no look-ahead (the weight of a trade is the
last decision made at or before its open time). Keep enforcing only if it wins steadily over >= 4 weeks, else
the allocator returns to shadow by itself.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import market_context as mctx
from . import portfolio_brain as pb
from . import runtime_settings as rs

log = logging.getLogger("survival-agent.allocator")

PROMPT_VERSION = "p20b-v2"
MANAGED_BOTS = ["main", "shorts", "moonshot", "listing_fade", "trend_v2"]
NEVER_MANAGED = ["control_long", "control_short", "trend_ensemble", "kronos"]
LLM_COHORT = pb.COHORTS["llm"]

MAX_DAILY_STEP = 0.25
MIN_WEIGHT = 0.05
MACRO_CAP = 0.5
BUDGET_ZERO_RATIO = 1.5           # gross >= 1.5 x budget: everything stops (below that the weights are scaled)
PROBE_WEIGHT = 0.1
# grow policy
GROW_MAX = 1.5                    # a bot that earns money
EDGE_MAX = 2.0                    # ... and has evidence status `edge`
GROW_MIN_TRADES = 30              # closed trades since the epoch before a bot is judged either way
TRIAL_WEIGHT = 0.5
BREAKER_MIN_TRADES = 5            # P26 circuit breaker: never judged on fewer closed trades
BREAKER_STAKES = 1.25             # ... stop a bot whose trades since the last (re)start lost more than this many average stakes
BREAKER_STREAK = 8                # ... or whose last N closed trades are all losses (0 = off)
STOP_MIN_DAYS = 3                 # a stopped bot waits at least this long before a trial
TRIAL_DAYS = 7
TRIAL_MIN_TRADES = 5
DD_HALF, DD_QUARTER = -15.0, -25.0
POLICIES = ("grow", "protect")
PROBE_HOURS = 23                  # "one day", ending before the next daily run so it cannot chain into a second day
NON_SAFETY_RULES = ("daily_step_25pp", "below_min_weight_pause")
WEIGHTS_MAX_AGE_H = 36            # bots ignore a weights file older than this

ALLOCATIONS_FILE = "allocations.jsonl"
STATE_FILE = "allocator_state.json"
LATEST_FILE = "allocator_latest.json"
COUNTERFACTUAL_FILE = "allocator_counterfactual.json"
KILL_FILE = "allocator_kill.json"
WEIGHTS_FILE = "weights.json"
SCHEMA_WEIGHTS = "p20-weights-v1"

LLM_MAX_TOKENS = 4096             # the 02.10 reply was cut off mid-reasoning at 1500
REASON_MAX = 140
RETRY_SUFFIX = "\n\nYour previous reply was not valid JSON. Reply with the JSON object only."
CF_MIN_DAYS = 28
CF_WEEKS = 4


class ControlBotError(Exception):
    """Raised for any attempt to manage a control bot (or trend v1)."""


class EnforceRefused(RuntimeError):
    """Enforce requested while a managed bot is not verifiably dry-run."""


def assert_manageable(bot: str) -> str:
    if bot in NEVER_MANAGED or bot not in MANAGED_BOTS:
        raise ControlBotError(f"{bot!r} is not a managed bot")
    return bot


# ---------------------------------------------------------------- settings
def load_settings(env: dict | None = None) -> dict:
    env = rs.merged_env() if env is None else env      # P20.2: dashboard switches override the environment
    mode = str(env.get("PORTFOLIO_BRAIN_MODE", "shadow")).strip().lower() or "shadow"
    if mode not in ("off", "shadow", "enforce"):
        log.error("PORTFOLIO_BRAIN_MODE=%r is not one of off|shadow|enforce -- treating as off", mode)
        mode = "off"
    base = pb.load_settings({**dict(env), "PORTFOLIO_BRAIN_MODE": "shadow"})
    policy = str(env.get("ALLOCATOR_POLICY", "grow")).strip().lower() or "grow"
    if policy not in POLICIES:
        log.error("ALLOCATOR_POLICY=%r is not one of grow|protect -- treating as protect", policy)
        policy = "protect"
    sig = {}
    for b in MANAGED_BOTS:
        pre = {"main": "MAIN", "shorts": "SHORTS", "moonshot": "MOONSHOT"}.get(b)
        if pre:
            src = str(env.get(pre + "_DECISION_SOURCE", "") or "kronos_llm").strip().lower()
            prov = str(env.get(pre + "_LLM_PROVIDER", "") or "deepseek").strip().lower()
            sig[b] = f"{src}/{prov}"
        else:
            sig[b] = "rules"
    return {**base, "mode": mode, "policy": policy, "bot_signature": sig, "coin_limit": pb._f(env, "ALLOCATOR_COIN_LIMIT", 0.25),
            "breaker": {"on": str(env.get("ALLOCATOR_BREAKER", "on")).strip().lower() != "off", "stakes": pb._f(env, "ALLOCATOR_BREAKER_STAKES", BREAKER_STAKES),
                        "streak": int(pb._f(env, "ALLOCATOR_BREAKER_STREAK", float(BREAKER_STREAK)))},
            "probe": str(env.get("ALLOCATOR_PROBE", "on")).strip().lower() != "off",
            "probe_days": pb._f(env, "ALLOCATOR_PROBE_DAYS", 7.0),
            "excluded": [b for b in str(env.get("ALLOCATOR_EXCLUDED_BOTS", "")).split(",") if b.strip() in MANAGED_BOTS],
            "retired": [b for b in str(env.get("ALLOCATOR_RETIRED_BOTS", "")).split(",") if b.strip() in MANAGED_BOTS]}


def active_bots(settings: dict) -> list[str]:
    """Managed bots minus the retired ones: the only bots that get a weight, a dry_run check or a REST call."""
    return [b for b in MANAGED_BOTS if b not in settings.get("retired", [])]


# ------------------------------------------------------------ bot control
class BotControl:
    """The ONLY code in the backend that sends a non-GET request to a bot, and only pause/resume, and only
    for managed bots (control bots raise before any network call)."""

    def __init__(self, urls: dict[str, str], username: str | None, password: str | None, http_post=None, timeout: int = 10):
        import requests
        self._post = http_post or requests.post
        self.urls, self.auth, self.timeout = urls, (username, password), timeout

    def _call(self, bot: str, path: str) -> tuple[bool, str]:
        assert_manageable(bot)
        if bot not in self.urls or not all(self.auth):
            return False, "no URL or credentials"
        try:
            r = self._post(f"{self.urls[bot]}{path}", auth=self.auth, timeout=self.timeout)
            r.raise_for_status()
            return True, str(getattr(r, "text", ""))[:120]
        except Exception as e:      # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    def pause(self, bot: str) -> tuple[bool, str]:
        return self._call(bot, "/api/v1/stopentry")

    def resume(self, bot: str) -> tuple[bool, str]:
        return self._call(bot, "/api/v1/start")

    def reload(self, bot: str) -> tuple[bool, str]:
        """Fallback resume for Freqtrade builds where /stopentry zeroes max_open_trades instead of pausing."""
        return self._call(bot, "/api/v1/reload_config")


def default_control() -> BotControl:
    from . import freqtrade_client
    urls = {b: cfg["url"] for b, cfg in freqtrade_client.BOTS.items()}
    return BotControl(urls, os.getenv("FREQTRADE_API_USERNAME"), os.getenv("FREQTRADE_API_PASSWORD"))


# ---------------------------------------------------------------- kill/guard
def kill_state(data_dir: Path) -> dict | None:
    try:
        return json.loads((Path(data_dir) / KILL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def dry_run_status(fetcher, bots: list[str] | None = None) -> dict[str, bool | None]:      # bots = the ACTIVE ones; retired are never asked
    """True/False from the bot's own /show_config; None when it cannot be verified."""
    out: dict[str, bool | None] = {}
    for bot in bots or MANAGED_BOTS:
        cfg = fetcher._json(bot, "/api/v1/show_config")
        v = cfg.get("dry_run") if isinstance(cfg, dict) else None
        out[bot] = v if isinstance(v, bool) else None
    return out


def enforce_check(fetcher, bots: list[str] | None = None) -> dict:
    st = dry_run_status(fetcher, bots)
    live = [b for b, v in st.items() if v is False]
    unknown = [b for b, v in st.items() if v is None]
    return {"ok": not live and not unknown, "live_bots": live, "unverified_bots": unknown, "dry_run": st}


def startup_guard(fetcher, env: dict | None = None) -> None:
    """Called when the service starts. enforce + any bot confirmed dry_run=false -> refuse to start.
    A bot that is merely unreachable (containers still booting) is re-checked on every run instead."""
    if load_settings(env)["mode"] != "enforce":
        return
    live = [b for b, v in dry_run_status(fetcher, active_bots(load_settings(env))).items() if v is False]
    if live:
        raise EnforceRefused(f"PORTFOLIO_BRAIN_MODE=enforce refused: dry_run=false on {live}")


def readiness(data_dir: Path, fetcher) -> dict:
    """What the dashboard shows next to the enforce switch: can it act, and what has not been verified."""
    data_dir = Path(data_dir)
    chk = enforce_check(fetcher, active_bots(load_settings()))
    reports = []
    for p in sorted(data_dir.glob("p20_live_test_*.json")):
        r = load_json(p, {})
        reports.append({"bot": r.get("bot"), "passed": bool(r.get("passed")), "finished_at": r.get("finished_at"),
                        "entries_return": r.get("entries_return")})
    warnings = []
    if not any(r["passed"] for r in reports):
        warnings.append("the live pause/resume test (python -m backend.ops.p20_live_test) has not been run or has not passed")
    if shared_dir() is None:
        warnings.append("the shared /allocator volume is not mounted: bots cannot read weights or context (pausing via REST still works)")
    return {"dry_run": chk["dry_run"], "dry_run_ok": chk["ok"], "live_bots": chk["live_bots"], "unverified_bots": chk["unverified_bots"],
            "shared_volume": shared_dir() is not None, "live_test_reports": reports, "kill": kill_state(data_dir), "warnings": warnings}


def effective_mode(data_dir: Path, settings: dict, fetcher) -> tuple[str, list[str], dict | None]:
    """The mode this run may really use. enforce degrades to shadow (with reasons) instead of acting unsafely."""
    mode, reasons, check = settings["mode"], [], None
    if mode == "enforce":
        k = kill_state(data_dir)
        if k:
            reasons.append(f"kill switch active since {k.get('killed_at')} ({k.get('reason')})")
        check = enforce_check(fetcher, active_bots(settings))
        if check["live_bots"]:
            reasons.append(f"dry_run=false on {check['live_bots']}")
        if check["unverified_bots"]:
            reasons.append(f"dry_run not verifiable on {check['unverified_bots']} (active, not retired)")
        if reasons:
            mode = "shadow"
    return mode, reasons, check


def is_paused(cfg) -> bool:
    """Paused in either Freqtrade semantics: state == 'paused' (2026.8: /stopentry = pause), or the legacy
    behaviour where /stopentry zeroes max_open_trades and the state stays 'running'."""
    if not isinstance(cfg, dict):
        return False
    mot = cfg.get("max_open_trades")
    return cfg.get("state") == "paused" or (isinstance(mot, (int, float)) and mot == 0)


def resume_bot(control, fetcher, bot: str) -> list[dict]:
    """POST /start; if the bot still looks paused afterwards (legacy builds: /start answers 'already running' and
    max_open_trades stays 0) POST /reload_config, which re-reads the configured max_open_trades."""
    actions = []
    ok, msg = control.resume(bot)
    actions.append({"bot": bot, "action": "start", "ok": ok, "detail": msg})
    still_paused = fetcher is not None and ok and is_paused(fetcher._json(bot, "/api/v1/show_config"))
    if fetcher is not None and ok:
        actions[-1]["verified_running"] = not still_paused       # every resume says whether the bot really runs again
    if still_paused:
        ok2, msg2 = control.reload(bot)
        actions.append({"bot": bot, "action": "reload_config", "ok": ok2, "detail": msg2})
        ok = ok2 and not is_paused(fetcher._json(bot, "/api/v1/show_config"))
        actions[-1]["verified_running"] = ok
        actions[-1]["ok"] = ok
    return actions


# ------------------------------------------------------------ pure rules
def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def extract_json_objects(text: str):
    """Yield every balanced top-level {...} substring of `text` in order (brace matching that respects JSON strings and
    escapes), so a reply with reasoning before or after the JSON can still be read."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[i:j + 1]
                    break
            j += 1
        else:
            return          # unbalanced from here on: nothing more to extract
        i = j + 1


def _load_reply(text: str):
    """(data, how): parse the whole reply (optionally ```-fenced); otherwise the FIRST balanced {...} object that parses."""
    body = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.S)
    if fence:
        body = fence.group(1)
    try:
        return json.loads(body), "direct"
    except ValueError:
        pass
    for cand in extract_json_objects(text):
        try:
            data = json.loads(cand)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data, "extracted"
    return None, None


def validate_llm(text: str | None, prev: dict[str, float], retired: list[str] | None = None) -> dict:
    """Strict schema check. Returns {ok, weights, reasons, notes}. Whole reply invalid -> ok False (caller keeps
    the previous weights). Per entry: a non-numeric weight keeps that bot's previous weight; a number outside
    0..1 is cut to the bound; unknown/control bots are ignored (and noted), never applied."""
    notes: list[str] = []
    if not text or not isinstance(text, str):
        return {"ok": False, "weights": dict(prev), "reasons": {}, "notes": ["empty reply"]}
    data, how = _load_reply(text)
    if how is None:
        try:
            json.loads(text.strip())
            return {"ok": False, "weights": dict(prev), "reasons": {}, "notes": ["reply is not a JSON object"]}
        except ValueError:
            return {"ok": False, "weights": dict(prev), "reasons": {}, "notes": ["reply is not JSON"]}
    if how == "extracted":
        notes.append("JSON object extracted from surrounding text")
    if not isinstance(data, dict):
        return {"ok": False, "weights": dict(prev), "reasons": {}, "notes": ["reply is not a JSON object"]}
    weights, reasons, good = dict(prev), {}, 0
    for bot, entry in data.items():
        if bot in (retired or []):
            notes.append(f"{bot}: retired bot -- ignored")
            continue
        if bot in NEVER_MANAGED or bot not in MANAGED_BOTS:
            notes.append(f"{bot}: not a managed bot -- ignored")
            continue
        if not isinstance(entry, dict) or "weight" not in entry:
            notes.append(f"{bot}: entry is not {{weight, reason}} -- kept previous")
            continue
        w = entry["weight"]
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w):
            notes.append(f"{bot}: weight {w!r} is not a finite number -- kept previous")
            continue
        if w < 0 or w > 1:
            notes.append(f"{bot}: weight {w} outside 0..1 -- cut to bound")
        weights[bot] = clamp(float(w))
        reasons[bot] = str(entry.get("reason", ""))[:REASON_MAX]
        good += 1
    if good == 0:
        return {"ok": False, "weights": dict(prev), "reasons": {}, "notes": notes + ["no valid bot entry"]}
    return {"ok": True, "weights": weights, "reasons": reasons, "notes": notes}


def side_exposure(open_trades: dict[str, list[dict]]) -> dict[str, dict]:
    """coin -> {'long': {bot: $}, 'short': {bot: $}} notionals from the managed bots' open trades."""
    out: dict[str, dict] = {}
    for bot, trades in open_trades.items():
        if bot not in MANAGED_BOTS:
            continue
        for t in trades:
            try:
                n = abs(float(t.get("stake_amount", 0.0))) * float(t.get("leverage") or 1.0)
            except (TypeError, ValueError):
                continue
            side = "short" if t.get("is_short") else "long"
            c = out.setdefault(pb.coin_of(t), {"long": {}, "short": {}})
            c[side][bot] = c[side].get(bot, 0.0) + n
    return out


def apply_hard_rules(proposal: dict[str, float], prev: dict[str, float], facts: dict) -> dict:
    """facts: drawdown (brain drawdown_state dict), gross, budget, equity, open_trades, macro_soon (True/False/None),
    coin_limit. Returns {final, steps, rules_fired}. Every change of a weight is recorded with its rule."""
    steps: dict[str, list[dict]] = {b: [] for b in MANAGED_BOTS}
    w = {b: clamp(float(proposal.get(b, prev.get(b, 1.0)))) for b in MANAGED_BOTS}
    fired: list[str] = []

    def setw(bot: str, new: float, rule: str):
        new = clamp(new)
        if abs(new - w[bot]) > 1e-9:
            steps[bot].append({"rule": rule, "from": round(w[bot], 4), "to": round(new, 4)})
            w[bot] = new
            if rule not in fired:
                fired.append(rule)

    for b in MANAGED_BOTS:          # 1. rate limit on LLM-driven moves
        p = prev.get(b, 1.0)
        setw(b, clamp(w[b], p - MAX_DAILY_STEP, p + MAX_DAILY_STEP), "daily_step_25pp")

    def cap_all(limit: float, rule: str, bots=None):
        for b in bots or MANAGED_BOTS:
            if w[b] > limit:
                setw(b, limit, rule)

    warnings: list[str] = []
    partial = bool(facts.get("partial"))
    dd = facts.get("drawdown") or {}
    rung = (dd.get("highest_rung") or {}).get("level", "")
    if partial:
        # incomplete data (a never-seen active bot): ladder, weekly/monthly loss and the CPPI floor/budget are WARNINGS only --
        # they never cut a weight, so a missing bot cannot zero the book
        if rung or dd.get("weekly_limit_breached") or dd.get("monthly_limit_breached"):
            warnings.append(f"partial data: ladder/loss rules would fire ({rung or 'loss limit'}) -- not applied")
        rung = ""
        dd = {}
    if rung.startswith("L3"):
        cap_all(0.0, "ladder_L3_stop_entries")
    elif rung.startswith("L2"):
        cap_all(0.0, "ladder_L2_halt_llm_cohort", [b for b in MANAGED_BOTS if b in LLM_COHORT])
        cap_all(0.5, "ladder_L2_cap_0.5", [b for b in MANAGED_BOTS if b not in LLM_COHORT])
    elif rung.startswith("L1"):
        cap_all(0.75, "ladder_L1_cap_0.75")
    if dd.get("weekly_limit_breached"):
        cap_all(0.0, "weekly_loss_-2pct_pause_entries")
    if dd.get("monthly_limit_breached"):
        cap_all(0.5, "monthly_loss_-4pct_cap_0.5")

    budget, gross = facts.get("budget"), facts.get("gross")
    if partial:
        if isinstance(budget, (int, float)) and isinstance(gross, (int, float)) and (budget <= 0 or gross > budget):
            warnings.append("partial data: CPPI budget/floor rule would fire -- not applied")
    elif isinstance(budget, (int, float)) and isinstance(gross, (int, float)):
        if budget <= 0:
            cap_all(0.0, "cppi_floor_breached")                       # equity at/below the 90% floor: no risk budget at all
        elif gross >= BUDGET_ZERO_RATIO * budget:
            cap_all(0.0, "cppi_gross_1.5x_budget_stop")
        elif gross > budget:                                          # proportional: every weight x budget/gross
            scale = budget / gross
            for b in MANAGED_BOTS:
                setw(b, w[b] * scale, "cppi_proportional_scale")

    if facts.get("macro_soon") is True:
        cap_all(MACRO_CAP, "macro_event_within_24h_cap_0.5")

    exposure = side_exposure(facts.get("open_trades", {}))
    for coin, c in exposure.items():
        if c["long"] and c["short"]:
            lg, sh = sum(c["long"].values()), sum(c["short"].values())
            loser = c["short"] if sh < lg else c["long"]         # the smaller side stops adding
            for bot in loser:
                setw(bot, 0.0, f"opposite_positions_{coin}")
    equity, limit = facts.get("equity"), facts.get("coin_limit", 0.25)
    if equity and equity > 0:
        for coin, c in exposure.items():
            total = sum(c["long"].values()) + sum(c["short"].values())
            if total / equity >= limit:
                for bot in set(c["long"]) | set(c["short"]):
                    if w[bot] > 0.5:
                        setw(bot, 0.5, f"coin_concentration_{coin}")

    for b in MANAGED_BOTS:
        if 0 < w[b] < MIN_WEIGHT:
            setw(b, 0.0, "below_min_weight_pause")
        w[b] = round(w[b], 2)
    return {"final": w, "steps": {b: s for b, s in steps.items() if s}, "rules_fired": fired, "warnings": warnings}


def apply_probe(final: dict[str, float], steps: dict, st_file: dict, now: datetime, enabled: bool, days: float) -> list[dict]:
    """Keep the experiment alive: a bot that has sat at weight 0 for more than `days` days gets a probe weight of 0.1
    for about a day, so real trades (and statistics) keep coming. Never overrides a SAFETY rule (ladder, weekly/monthly
    limit, CPPI, opposite positions, concentration): a bot those rules hold at 0 is not probed, and its zero-clock restarts.
    Mutates `final` and st_file['zero_since'] / st_file['probe']; returns a log of what it did. The P19 ledger is
    independent of all this: signals are written whatever the weight."""
    zs, pr = st_file.setdefault("zero_since", {}), st_file.setdefault("probe", {})
    log: list[dict] = []
    for b in MANAGED_BOTS:
        safety = final[b] == 0 and any(s["rule"] not in NON_SAFETY_RULES for s in steps.get(b, []))
        if final[b] > 0 or safety:
            zs.pop(b, None)
            pr.pop(b, None)
            continue
        since = pb._parse_dt(zs.get(b))
        if since is None:
            zs[b] = mctx.iso(now)
            continue
        until = pb._parse_dt((pr.get(b) or {}).get("until"))
        if until is not None and until > now:                          # probe still running (e.g. an event-driven run)
            final[b] = PROBE_WEIGHT
            log.append({"bot": b, "probe": "continuing", "until": mctx.iso(until)})
        elif until is not None:                                        # probe over: back to 0, a new 7-day clock starts
            pr.pop(b, None)
            zs[b] = mctx.iso(now)
            log.append({"bot": b, "probe": "ended"})
        elif enabled and (now - since) >= timedelta(days=days):
            final[b] = PROBE_WEIGHT
            pr[b] = {"started": mctx.iso(now), "until": mctx.iso(now + timedelta(hours=PROBE_HOURS))}
            zs[b] = mctx.iso(now)
            log.append({"bot": b, "probe": "started", "weight": PROBE_WEIGHT, "until": pr[b]["until"],
                        "reason": f"weight 0 for >= {days:g} days"})
    return log


# ------------------------------------------------------------ grow policy
def effective_policy(settings: dict, check: dict | None) -> tuple[str, str | None]:
    """grow unless the dry-run check of this run found a live (or unverifiable) bot: real money is governed by the protect rules."""
    if settings.get("policy") == "protect":
        return "protect", None
    if check and (check.get("live_bots") or check.get("unverified_bots")):
        return "protect", f"forced: dry_run is false or unverifiable on {sorted(set(check.get('live_bots', [])) | set(check.get('unverified_bots', [])))}"
    return "grow", None


def _sum_pnl(trades: list[dict]) -> float:
    return sum(float(t.get("close_profit_abs") or 0.0) for t in trades)


def breaker_reason(cl: list[dict], cfg: dict | None) -> str | None:
    """P26 circuit breaker for a bot that is losing money fast, long before the 30-trade verdict (a slow rule bot needs months for 30 trades). `cl` are the closed trades since the bot's
    last (re)start. Trips on (a) net loss of more than `stakes` average stakes, or (b) the last `streak` trades all losses. Returns the reason, or None. cfg None / off = never trips."""
    if not cfg or not cfg.get("on", True):
        return None
    n = len(cl)
    if n < BREAKER_MIN_TRADES:
        return None
    net = _sum_pnl(cl)
    stakes = [float(t.get("stake_amount") or 0.0) for t in cl if float(t.get("stake_amount") or 0.0) > 0]
    avg = sum(stakes) / len(stakes) if stakes else 0.0
    k = float(cfg.get("stakes") or 0.0)
    if k > 0 and avg > 0 and net <= -k * avg:
        return f"circuit breaker: {n} trades lost ${-net:.2f}, more than {k:g} x the average stake (${avg:.2f})"
    s = int(cfg.get("streak") or 0)
    if s > 0 and n >= s and net < 0 and all(float(t.get("close_profit_abs") or 0.0) <= 0 for t in cl[-s:]):
        return f"circuit breaker: the last {s} trades were all losses (net ${net:+.2f})"
    return None


def control_name(bot: str, trades: list[dict]) -> str:
    """The control bot whose last-7-days result decides a restart. Spot bots are long (control_long). A futures bot follows its recent side: mostly longs -> control_long, else control_short."""
    mode = pb.BOT_MODE[bot]
    if mode == "spot":
        return "control_long"
    recent = [t for t in trades if not t.get("is_open")][-30:]
    if len(recent) >= 5 and sum(1 for t in recent if t.get("is_short") is False) / len(recent) >= 0.6:
        return "control_long"
    return pb.CONTROL_FOR_MODE[mode]


def grow_bot(bot: str, rec: dict, trades: list[dict], ev_status: str, ctrl_d7: float | None, now: datetime, ctrl_name: str, breaker: dict | None = None) -> tuple[str, float, str]:
    """One bot's class, weight and reason. Mutates `rec` (status, status_since, trial_start). Deterministic, no LLM."""
    since = pb._parse_dt(rec.get("epoch_since")) or (now - timedelta(days=30))
    cl = pb.closed_since(trades, since)
    n, net = len(cl), _sum_pnl(cl)
    net7 = _sum_pnl(pb.closed_since(trades, max(since, now - timedelta(days=7))))
    status = rec.get("status", "active")
    ctl = f"{ctrl_name} {'n/a' if ctrl_d7 is None else '$%+.2f' % ctrl_d7} over 7 d"
    if status == "stopped":
        st_since = pb._parse_dt(rec.get("status_since")) or now
        days = (now - st_since).total_seconds() / 86400
        if days >= STOP_MIN_DAYS and ctrl_d7 is not None and ctrl_d7 > 0:
            rec.update({"status": "trial", "trial_start": mctx.iso(now)})
            return "trial", TRIAL_WEIGHT, f"restart trial: stopped {days:.1f} d, {ctl}"
        return "stopped", 0.0, f"stopped {days:.1f} d; a trial starts after {STOP_MIN_DAYS} d once {ctrl_name} earns > 0 over 7 d ({ctl})"
    if status == "trial":
        ts = pb._parse_dt(rec.get("trial_start")) or now
        tr = pb.closed_since(trades, ts)
        n_t, net_t = len(tr), _sum_pnl(tr)
        why_b = breaker_reason(tr, breaker)
        if why_b:
            rec.update({"status": "stopped", "status_since": mctx.iso(now), "breaker_since": mctx.iso(now)})
            return "breaker", 0.0, "trial " + why_b
        if (now - ts) >= timedelta(days=TRIAL_DAYS):
            if n_t >= TRIAL_MIN_TRADES and net_t > 0:
                rec["status"] = "active"
                status = "active"
            elif n_t >= TRIAL_MIN_TRADES:
                rec.update({"status": "stopped", "status_since": mctx.iso(now)})
                return "stopped", 0.0, f"trial failed: {n_t} trades, net ${net_t:+.2f}"
            else:
                return "trial", TRIAL_WEIGHT, f"trial extended: only {n_t} trades in {TRIAL_DAYS}+ d (needs {TRIAL_MIN_TRADES})"
        else:
            return "trial", TRIAL_WEIGHT, f"trial day {int((now - ts).total_seconds() / 86400) + 1} of {TRIAL_DAYS}: {n_t} trades, net ${net_t:+.2f}"
    if n >= GROW_MIN_TRADES and net < 0 and net7 < 0:
        rec.update({"status": "stopped", "status_since": mctx.iso(now)})
        return "stopped", 0.0, f"loses: {n} trades net ${net:+.2f}, last 7 d ${net7:+.2f}"
    why_b = breaker_reason(pb.closed_since(trades, max(since, pb._parse_dt(rec.get("breaker_since")) or since)), breaker)
    if why_b:
        rec.update({"status": "stopped", "status_since": mctx.iso(now), "breaker_since": mctx.iso(now)})
        return "breaker", 0.0, why_b
    if n >= GROW_MIN_TRADES and net > 0 and net7 >= 0:
        if ev_status == "edge":
            return "edge", EDGE_MAX, f"evidence edge, {n} trades net ${net:+.2f}, last 7 d ${net7:+.2f}"
        return "winner", GROW_MAX, f"earns: {n} trades net ${net:+.2f}, last 7 d ${net7:+.2f}"
    return "neutral", 1.0, f"measuring: {n} of {GROW_MIN_TRADES} trades since {mctx.iso(since)[:10]}, net ${net:+.2f}"


def grow_policy(state: dict, prev: dict[str, float], st_file: dict, now: datetime, settings: dict, macro_soon) -> dict:
    """Weights from the bots' own results (see the module docstring). Returns {final, steps, rules_fired, warnings, detail, reasons}."""
    active = active_bots(settings)
    gstate = st_file.setdefault("grow", {})
    final: dict[str, float] = {b: float(prev.get(b, 1.0)) for b in MANAGED_BOTS}
    steps: dict[str, list[dict]] = {b: [] for b in MANAGED_BOTS}
    reasons, detail, fired = {}, {}, []

    def setw(bot: str, new: float, rule: str):
        new = round(max(0.0, min(EDGE_MAX, new)), 2)
        if abs(new - final[bot]) > 1e-9:
            steps[bot].append({"rule": rule, "from": round(final[bot], 4), "to": new})
            final[bot] = new
            if rule not in fired:
                fired.append(rule)

    ctrl = state.get("controls") or {}
    for b in active:
        e = (state.get("bots") or {}).get(b) or {}
        rec = gstate.setdefault(b, {"status": "active"})
        sig = settings.get("bot_signature", {}).get(b, "rules")
        if rec.get("sig") != sig:                              # first run, or the decision source / provider changed: a new epoch starts
            first = "sig" not in rec
            rec.update({"sig": sig, "epoch_since": None if (first and sig == "rules") else mctx.iso(now)})
            if not first:
                rec.update({"status": "active", "status_since": mctx.iso(now)})
        if not e.get("reachable"):
            reasons[b] = "bot not reachable: weight unchanged"
            detail[b] = {"class": "unreachable"}
            continue
        ctl_name = control_name(b, state["trades_by_bot"].get(b, []))
        c7 = ((ctrl.get(ctl_name) or {}).get("d7") or {}).get("net_pnl_usd")
        cls, w, why = grow_bot(b, rec, state["trades_by_bot"].get(b, []), e.get("evidence_status", "no_data"), c7, now, ctl_name, settings.get("breaker"))
        if w > final[b] and cls in ("winner", "edge"):         # raises are gradual, cuts are immediate
            w = max(1.0, min(w, final[b] + MAX_DAILY_STEP))
        setw(b, w, f"grow_{cls}")
        reasons[b] = why[:REASON_MAX]
        detail[b] = {"class": cls, "status": rec.get("status"), "epoch_since": rec.get("epoch_since"), "control": ctl_name, "control_d7_usd": c7}
    warnings: list[str] = []
    dd = None if state.get("partial") else (state.get("drawdown") or {}).get("drawdown_from_hwm_pct")
    factor = 1.0
    if isinstance(dd, (int, float)):
        factor = 0.25 if dd <= DD_QUARTER else 0.5 if dd <= DD_HALF else 1.0
    if factor < 1.0:
        for b in active:
            if final[b] > 0:
                setw(b, final[b] * factor, f"drawdown_scale_x{factor:g}")
    if macro_soon is True:
        for b in active:
            if final[b] > 0.5:
                setw(b, MACRO_CAP, "macro_event_within_24h_cap_0.5")
    exposure = side_exposure(state.get("open_trades", {}))
    for coin, c in exposure.items():
        if c["long"] and c["short"]:
            lg, sh = sum(c["long"].values()), sum(c["short"].values())
            for bot in (c["short"] if sh < lg else c["long"]):
                if bot in active and final[bot] > 0.5:
                    setw(bot, 0.5, f"opposite_positions_{coin}_cap_0.5")
    equity, limit = state.get("equity"), settings.get("coin_limit", 0.25)
    if equity and equity > 0:
        for coin, c in exposure.items():
            if (sum(c["long"].values()) + sum(c["short"].values())) / equity >= limit:
                for bot in set(c["long"]) | set(c["short"]):
                    if bot in active and final[bot] > 0.5:
                        setw(bot, 0.5, f"coin_concentration_{coin}")
    for b in MANAGED_BOTS:
        if 0 < final[b] < MIN_WEIGHT:
            setw(b, 0.0, "below_min_weight_pause")
        final[b] = round(final[b], 2)
    return {"final": final, "steps": {b: s_ for b, s_ in steps.items() if s_}, "rules_fired": fired, "warnings": warnings, "reasons": reasons, "detail": detail, "drawdown_factor": factor}


# -------------------------------------------------------------- LLM input
SYSTEM_PROMPT = (
    "You are the capital allocator for a small multi-bot crypto stack that trades in DRY-RUN. You receive only "
    "numbers computed by code. For every managed bot choose a weight between 0 and 1: the share of its normal "
    "position size it may use (0 pauses new entries; exits keep working). Favor bots with evidence_status 'edge', "
    "cut bots with 'no_edge', stay cautious with 'unproven' and 'no_data'. Weekly/monthly targets only protect "
    "profit: if a target is reached you may cut and lock gains; missing a target must never raise risk. Respect "
    "the context status: a field marked stale or unavailable is unknown, not neutral. Hard rules in code will "
    "override you (drawdown ladder, CPPI budget, macro window, opposite positions, +-25pp/day). "
    "A bot left at weight 0 for more than 7 days is given a one-day probe weight of 0.1 by code so that real trades keep "
    "producing evidence; do not rely on weight 0 being permanent. "
    'Your whole reply must be EXACTLY one JSON object and nothing else: {"<bot>": {"weight": <number 0..1>, "reason": '
    '"<one sentence, at most 140 characters, tied to a number>"}, ...} covering every managed bot in the input. '
    "No reasoning, no explanation, no text before or after the JSON, no code fence. Reply with the JSON object only."
)


def _trade_fees(t: dict) -> float:
    return float(t.get("fee_open_cost") or 0.0) + float(t.get("fee_close_cost") or 0.0)


def bot_window(trades: list[dict], now: datetime, days: int) -> dict:
    since = now - timedelta(days=days)
    closed = pb.closed_since(trades, since)
    pnl = sum(float(t.get("close_profit_abs") or 0.0) for t in closed)
    fees = sum(_trade_fees(t) for t in closed)
    turnover = sum(abs(float(t.get("stake_amount") or 0.0)) * float(t.get("leverage") or 1.0) * 2 for t in closed)
    shorts = sum(1 for t in closed if t.get("is_short"))
    return {"trades": len(closed), "net_pnl_usd": round(pnl, 2), "fees_usd": round(fees, 3), "turnover_usd": round(turnover, 2),
            "short_share": round(shorts / len(closed), 3) if closed else None}


def context_summary(ctx: dict | None, now: datetime) -> dict:
    """What the allocator (and the audit trail) sees of the snapshot: flags + statuses, no per-coin bulk."""
    if not ctx:
        return {"available": False, "reason": "no context snapshot yet"}
    g, fl = ctx.get("global", {}), ctx.get("flags", {})

    def st(f):
        return mctx.effective_status(f, now) if mctx.is_field(f) else "unavailable"

    def val(f):
        return f.get("value") if mctx.is_field(f) and st(f) != "unavailable" else None

    return {
        "available": True, "snapshot_ts": ctx.get("ts"),
        "btc_regime": {"value": val(g.get("btc_regime")), "status": st(g.get("btc_regime", {}))},
        "fear_greed": {"value": val(g.get("fear_greed")), "status": st(g.get("fear_greed", {}))},
        "btc_dominance": {"value": val(g.get("btc_dominance")), "status": st(g.get("btc_dominance", {}))},
        "macro_event_within_24h": {"value": val(fl.get("macro_event_within_24h")), "status": st(fl.get("macro_event_within_24h", {})),
                                   "events": (fl.get("macro_event_within_24h") or {}).get("events")},
        "liquidation_cascade": {"value": val(fl.get("liquidation_cascade")), "status": st(fl.get("liquidation_cascade", {}))},
        "funding_extreme_coins": fl.get("funding_extreme_coins", []), "oi_spike_coins": fl.get("oi_spike_coins", []),
        "sources": {k: v.get("status") for k, v in (ctx.get("sources") or {}).items()},
    }


def build_llm_input(state: dict, ctx_sum: dict, prev: dict[str, float], active: list[str] | None = None) -> dict:
    active = active or MANAGED_BOTS
    return {
        "managed_bots": active,
        "retired_bots_not_to_be_weighted": [b for b in MANAGED_BOTS if b not in active],
        "current_weights": {b: prev.get(b, 1.0) for b in active},
        "portfolio": {k: state.get(k) for k in ("equity", "hwm", "floor", "budget", "gross", "drawdown", "targets")},
        "bots": state["bots"],
        "context": ctx_sum,
        "note": "targets only protect profit; unavailable/stale context fields are unknown, not neutral",
    }


# ---------------------------------------------------------------- state
def gather_state(fetcher, data_dir: Path, now: datetime, settings: dict, with_trades: bool = True) -> dict:
    data_dir = Path(data_dir)
    ledger: dict = {}
    try:
        ledger = json.loads((data_dir / "capital.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    from . import bot_state as bs
    retired = settings.get("retired", [])
    frozen = bs.freeze_retired(data_dir, retired, now)
    col = bs.collect(fetcher, data_dir, now, pb.PORTFOLIO_BOTS, retired, trade_bots=(MANAGED_BOTS if with_trades else []))
    equities, open_trades, trades_by_bot, balances, missing = col["equities"], col["open_trades"], col["trades"], col["balances"], col["missing"]
    partial = bool(missing)
    frozen_eq, dropped_ref = bs.contribution(frozen)
    equity = round(sum(equities.values()) + frozen_eq, 2)
    reference = sum(pb._starting_capital(b, balances.get(b), ledger) for b in pb.PORTFOLIO_BOTS) - dropped_ref
    st = pb.load_state(data_dir)
    snaps_raw = pb.load_snapshots(data_dir)
    roster = sorted(set(equities) | set(frozen))
    # same rule as the brain (see pb.restate_snapshots): history and the high-water mark only count the bots in the book today
    snaps = pb.restate_snapshots(snaps_raw, roster) if not partial else snaps_raw
    hwm = st.get("hwm")
    if not partial:
        hist_max = max([x["equity"] for x in snaps if not x.get("partial")] or [equity])
        if hwm is None or st.get("hwm_roster") != roster:
            hwm = max(hist_max, equity)
        else:
            hwm = max(float(hwm), hist_max, equity)
    elif hwm is None:
        hwm = equity
    fc = pb.floor_and_cushion(equity, reference, settings["floor_pct"], settings["cppi_multiplier"])
    exp = pb.exposure({b: t for b, t in open_trades.items() if b in pb.PORTFOLIO_BOTS}, settings["alt_beta"])
    week_ago = pb.equity_at_or_before(snaps, now - timedelta(days=7), fallback_first=False)
    month_ago = pb.equity_at_or_before(snaps, now - timedelta(days=30), fallback_first=False)
    dd = ({"drawdown_from_hwm_pct": None, "highest_rung": None, "weekly_limit_breached": False,
           "monthly_limit_breached": False, "note": f"not evaluated: bots unreachable {missing}"}
          if partial else pb.drawdown_state(equity, float(hwm), week_ago, month_ago))
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    targets = [] if partial else [
        pb.target_progress("week", equity, pb.equity_at_or_before(snaps, week_start) if snaps else None, settings["target_weekly_pct"], fc["floor"]),
        pb.target_progress("month", equity, pb.equity_at_or_before(snaps, month_start) if snaps else None, settings["target_monthly_pct"], fc["floor"]),
    ]
    try:
        brain = json.loads((data_dir / pb.LATEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        brain = {}
    evid = brain.get("evidence", {})
    bots = {}
    for b in active_bots(settings):
        if not with_trades:
            break
        if b not in trades_by_bot:
            bots[b] = {"reachable": False, "stale": b in col["stale"], "unreachable_over_24h": b in col["unreachable"]}
            continue
        e = evid.get(b, {})
        bots[b] = {"reachable": True, "d7": bot_window(trades_by_bot[b], now, 7), "d30": bot_window(trades_by_bot[b], now, 30),
                   "evidence_status": e.get("status", "no_data"), "evidence_reason": e.get("reason"),
                   "open_gross": exp["by_bot"].get(b, {}).get("gross", 0.0)}
    controls = {}
    if with_trades:
        for c in ("control_long", "control_short"):
            try:
                ct = fetcher.trades(c)
            except Exception:      # noqa: BLE001
                ct = None
            if ct is not None:
                controls[c] = {"d7": bot_window(ct, now, 7), "d30": bot_window(ct, now, 30)}
    return {"equity": equity, "hwm": round(float(hwm), 2), "floor": fc["floor"], "budget": fc["cppi_budget"], "gross": exp["gross"], "controls": controls,
            "partial": partial, "missing_bots": missing, "stale_bots": col["stale"], "unreachable_bots": col["unreachable"],
            "retired_bots": {b: {"frozen_equity": r.get("frozen_equity"), "method": r.get("method"), "source": r.get("source")} for b, r in frozen.items()},
            "drawdown": dd, "targets": targets, "bots": bots,
            "open_trades": open_trades, "trades_by_bot": trades_by_bot, "exposure": exp}


# ------------------------------------------------------ files / persistence
def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def load_allocations(data_dir: Path) -> list[dict]:
    p = Path(data_dir) / ALLOCATIONS_FILE
    out = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def shared_dir() -> Path | None:
    v = os.environ.get("ALLOCATOR_DIR") or "/allocator"        # the compose mount point; no env var needed
    return Path(v) if Path(v).is_dir() else None


def write_weights(shared: Path | None, mode: str, weights: dict[str, float], now: datetime) -> bool:
    """Shared weights file for the bots. Only managed bots, never controls. mode != enforce is ignored by bots."""
    if shared is None:
        return False
    body = {"schema": SCHEMA_WEIGHTS, "mode": mode, "updated_at": mctx.iso(now),
            "weights": {b: {"weight": float(weights.get(b, 1.0))} for b in MANAGED_BOTS}}
    try:
        _atomic(Path(shared) / WEIGHTS_FILE, json.dumps(body, indent=1))
        return True
    except OSError as e:
        log.warning("allocator: could not write the weights file: %s", e)
        return False


# ------------------------------------------------------------ counterfactual
def weight_at(allocs: list[dict], bot: str, when: datetime) -> tuple[float, bool]:
    """(weight, applied) of the last decision at or before `when` -- never a later one (no look-ahead)."""
    w, applied = 1.0, False
    for a in allocs:
        ts = pb._parse_dt(a.get("ts"))
        if ts is None or ts > when:
            break
        if bot in a.get("final", {}):
            w, applied = float(a["final"][bot]), a.get("effective_mode") == "enforce"
    return w, applied


def _curve_stats(daily: dict[str, float]) -> dict:
    days = sorted(daily)
    cum = peak = dd = 0.0
    for d in days:
        cum += daily[d]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    pos = sum(1 for d in days if daily[d] > 0)
    return {"net_pnl_usd": round(sum(daily.values()), 2), "max_drawdown_usd": round(dd, 2),
            "days": len(days), "days_positive": pos, "share_days_positive": round(pos / len(days), 3) if days else None}


def counterfactual(trades_by_bot: dict[str, list[dict]], allocs: list[dict], now: datetime, since: datetime | None = None) -> dict:
    """`since`: only decisions made at/after it count (P20.4: the days around the Dip stop had zeroed weights that say nothing about the
    allocator's judgement). Earlier decisions are not deleted -- they are listed under `excluded` with the reason."""
    allocs = sorted(allocs, key=lambda a: a.get("ts", ""))
    excluded = None
    if since is not None:
        before = [a for a in allocs if (pb._parse_dt(a.get("ts")) or since) < since]
        allocs = [a for a in allocs if (pb._parse_dt(a.get("ts")) or since) >= since]
        if before:
            excluded = {"reason": "retired_bot_incident", "decisions": len(before), "from": before[0]["ts"], "to": before[-1]["ts"],
                        "note": "kept in allocations.jsonl, left out of this counterfactual"}
    first = pb._parse_dt(allocs[0]["ts"]) if allocs else None
    out: dict = {"generated_at": mctx.iso(now), "decisions": len(allocs), "first_decision": mctx.iso(first) if first else None,
                 "rule": "trade weight = last decision at or before its open time; with = pnl x weight (shadow) or actual pnl (enforced "
                         "trades are already scaled); without = weight 1; net of fees; grouped by close day"}
    if excluded:
        out["excluded"] = excluded
        out["counted_since"] = mctx.iso(since)
    if not first:
        out.update({"verdict": "insufficient_data", "reason": "no allocator decision yet" + (" since the cut-off" if excluded else "")})
        return out
    with_d: dict[str, float] = {}
    without_d: dict[str, float] = {}
    week_delta: dict[str, float] = {}
    n_used = 0
    for bot, trades in trades_by_bot.items():
        if bot not in MANAGED_BOTS:
            continue
        for t in pb.closed_since(trades, first):
            op = pb._parse_dt(t.get("open_date"))
            if op is None or op < first:         # trades that opened before the first decision are not affected by it
                continue
            pnl = float(t.get("close_profit_abs") or 0.0)
            w, applied = weight_at(allocs, bot, op)
            if applied:
                actual, base = pnl, (pnl / w if w > 0 else pnl)
            else:
                actual, base = pnl * w, pnl
            day = t["_close"].strftime("%Y-%m-%d")
            with_d[day] = with_d.get(day, 0.0) + actual
            without_d[day] = without_d.get(day, 0.0) + base
            wk = (t["_close"] - timedelta(days=t["_close"].weekday())).strftime("%Y-%m-%d")
            week_delta[wk] = week_delta.get(wk, 0.0) + (actual - base)
            n_used += 1
    out["trades_used"] = n_used
    out["with_allocator"] = _curve_stats(with_d)
    out["without_allocator"] = _curve_stats(without_d)
    days_cov = (now - first).total_seconds() / 86400
    out["days_covered"] = round(days_cov, 1)
    last_weeks = sorted(week_delta)[-CF_WEEKS:]
    out["weekly_delta_usd"] = {w: round(week_delta[w], 2) for w in last_weeks}
    if days_cov < CF_MIN_DAYS:
        out.update({"verdict": "insufficient_data", "reason": f"{days_cov:.1f} of {CF_MIN_DAYS} days"})
        return out
    w_pnl, wo_pnl = out["with_allocator"]["net_pnl_usd"], out["without_allocator"]["net_pnl_usd"]
    wins = sum(1 for w in last_weeks if week_delta[w] >= 0)
    better = w_pnl > wo_pnl and out["with_allocator"]["max_drawdown_usd"] <= out["without_allocator"]["max_drawdown_usd"] \
        and len(last_weeks) >= CF_WEEKS and wins >= CF_WEEKS - 1
    out["weeks_not_worse"] = f"{wins}/{len(last_weeks)}"
    out["verdict"] = "keep" if better else "revert_to_shadow"
    out["reason"] = ("net pnl and drawdown better than the counterfactual, >= 3 of the last 4 weeks not worse" if better
                     else "does not win steadily against weight=1 (pnl, drawdown and >= 3/4 weeks are all required)")
    return out


# ------------------------------------------------------------- operations
def apply_enforce(control, state_file: dict, final: dict[str, float], fetcher, bots: list[str] | None = None) -> list[dict]:
    """Bring each bot's paused/running state in line with its weight. Returns the log of actions. `bots` = the ACTIVE bots:
    a retired bot is never touched (and silently dropped from the tracked sets)."""
    actions = []
    bots = bots or MANAGED_BOTS
    paused = set(state_file.get("paused_by_allocator", [])) & set(bots)
    stuck = set(state_file.get("stuck_bots", [])) & set(bots)
    for bot in bots:
        cfg = fetcher._json(bot, "/api/v1/show_config")
        paused_now = is_paused(cfg)
        if final.get(bot, 1.0) <= 0.0:
            if not paused_now:
                ok, msg = control.pause(bot)
                actions.append({"bot": bot, "action": "stopentry", "ok": ok, "detail": msg})
                if ok:
                    paused.add(bot)
            else:
                paused.add(bot)
        elif bot in paused:
            if paused_now:
                acts = resume_bot(control, fetcher, bot)
                actions += acts
                if acts[-1]["ok"]:
                    paused.discard(bot)
                    stuck.discard(bot)
                else:
                    stuck.add(bot)         # verified_running was false: stays tracked as paused and is reported, nothing is rolled back
            else:
                paused.discard(bot)
                stuck.discard(bot)
    state_file["paused_by_allocator"] = sorted(paused)
    state_file["stuck_bots"] = sorted(stuck & paused)
    return actions


def kill(data_dir: Path, reason: str, by: str, control=None, fetcher=None, now: datetime | None = None) -> dict:
    """Kill switch: enforcement off, bots the allocator paused are resumed, weights reset to 1, file flagged."""
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    _atomic(data_dir / KILL_FILE, json.dumps({"killed_at": mctx.iso(now), "reason": reason, "by": by}, indent=1))
    st = load_json(data_dir / STATE_FILE, {})
    actions = []
    retired_now = load_settings()["retired"]
    st["paused_by_allocator"] = [b for b in st.get("paused_by_allocator", []) if b not in retired_now]    # a retired bot is never contacted
    if control is not None:
        for bot in list(st.get("paused_by_allocator", [])):
            acts = resume_bot(control, fetcher, bot)
            actions += acts
            if acts[-1]["ok"]:
                st["paused_by_allocator"].remove(bot)
    st["weights"] = {b: 1.0 for b in MANAGED_BOTS}
    _atomic(data_dir / STATE_FILE, json.dumps(st, indent=1))
    write_weights(shared_dir(), "shadow", {b: 1.0 for b in MANAGED_BOTS}, now)
    log.warning("allocator: KILL SWITCH (%s, by %s) -- enforcement off, weights reset to 1, resumed=%s", reason, by, actions)
    return {"killed": True, "actions": actions}


def clear_kill(data_dir: Path) -> bool:
    p = Path(data_dir) / KILL_FILE
    if p.is_file():
        p.unlink()
        return True
    return False


def detect_events(state: dict, ctx_sum: dict, seen: dict[str, str], now: datetime) -> list[str]:
    """Event ids that have not triggered a run yet. The id is stable for as long as the event lasts."""
    ev: list[str] = []
    rung = ((state.get("drawdown") or {}).get("highest_rung") or {}).get("level")
    if rung:
        ev.append(f"ladder:{rung.split(' ')[0]}:{now.strftime('%Y-%m-%d')}")
    m = ctx_sum.get("macro_event_within_24h", {})
    if m.get("value") is True and m.get("events"):
        ev.append(f"macro:{m['events'][0]['ts']}")
    if ctx_sum.get("liquidation_cascade", {}).get("value") is True:
        ev.append(f"liquidation:{now.strftime('%Y-%m-%dT%H')}")
    return [e for e in ev if e not in seen]


def default_llm():
    from . import brain
    return lambda system, user: brain.ask(system, user, max_tokens=LLM_MAX_TOKENS, json_mode=True)


def ask_validated(llm, system: str, user: str, prev: dict[str, float], retired: list[str] | None = None) -> tuple[dict, list[str], int]:
    """Ask once; if the reply is not usable JSON ask ONE more time with the retry suffix. Returns (verdict, replies, attempts).
    Still invalid after the retry -> the verdict is invalid and the caller keeps the previous weights."""
    replies: list[str] = []
    verdict: dict = {"ok": False, "weights": dict(prev), "reasons": {}, "notes": ["no reply"]}
    for attempt, suffix in enumerate(("", RETRY_SUFFIX), start=1):
        try:
            reply = llm(system, user + suffix)
        except Exception as e:      # noqa: BLE001
            log.warning("allocator: LLM call failed (attempt %d): %s", attempt, e)
            reply = None
        replies.append(reply or "")
        verdict = validate_llm(reply, prev, retired)
        if verdict["ok"]:
            return verdict, replies, attempt
        log.warning("allocator[%s]: attempt %d gave no usable JSON (%s)", PROMPT_VERSION, attempt, "; ".join(verdict["notes"]))
    return verdict, replies, 2


def run_allocation(data_dir: Path, now: datetime | None = None, fetcher=None, llm=None, control=None,
                   env: dict | None = None, trigger: str = "daily") -> dict:
    settings = load_settings(env)
    now = now or datetime.now(timezone.utc)
    if settings["mode"] == "off":
        return {"mode": "off", "skipped": True}
    data_dir = Path(data_dir)
    fetcher = fetcher or pb.default_fetcher()
    llm = llm or default_llm()
    eff, eff_reasons, check = effective_mode(data_dir, settings, fetcher)

    st_file = load_json(data_dir / STATE_FILE, {})
    prev = {b: float(st_file.get("weights", {}).get(b, 1.0)) for b in MANAGED_BOTS}
    state = gather_state(fetcher, data_dir, now, settings)
    ctx = mctx.load_latest(data_dir)
    ctx_sum = context_summary(ctx, now)

    active = active_bots(settings)
    policy, policy_note = effective_policy(settings, check)
    macro_v = ctx_sum.get("macro_event_within_24h", {})
    macro_soon = macro_v.get("value") if macro_v.get("status") in ("ok", "stale") else None
    if policy == "grow":
        rules = grow_policy(state, prev, st_file, now, settings, macro_soon)
        verdict = {"ok": True, "weights": dict(rules["final"]), "reasons": rules["reasons"], "notes": ["grow policy: weights from the bots' own results, no LLM call"]}
        replies, attempts, reply = [], 0, ""
        probe_log = []
        final = rules["final"]
    else:
        llm_in = build_llm_input(state, ctx_sum, prev, active)
        verdict, replies, attempts = ask_validated(llm, SYSTEM_PROMPT, json.dumps(llm_in, indent=1, default=str), prev, settings["retired"])
        reply = replies[-1] if replies else ""
        if not verdict["ok"]:
            log.warning("allocator[%s]: no usable JSON after %d attempts (%s) -- weights unchanged before hard rules",
                        PROMPT_VERSION, attempts, "; ".join(verdict["notes"]))
        rules = apply_hard_rules(verdict["weights"], prev, {
            "drawdown": state["drawdown"], "gross": state["gross"], "budget": state["budget"], "equity": state["equity"],
            "open_trades": state["open_trades"], "macro_soon": macro_soon, "coin_limit": settings["coin_limit"], "partial": state["partial"]})
        final = rules["final"]
        probe_log = apply_probe(final, rules["steps"], st_file, now, settings["probe"], settings["probe_days"])
    for b in settings["excluded"] + settings["retired"]:   # left alone (dashboard) or retired: full size, never paused, no probe clock
        final[b] = 1.0
        st_file.get("zero_since", {}).pop(b, None)
        st_file.get("probe", {}).pop(b, None)

    actions: list[dict] = []
    if eff == "enforce":
        actions = apply_enforce(control or default_control(), st_file, final, fetcher, active)
    write_weights(shared_dir(), eff, final, now)
    if settings["retired"] and not st_file.get("cf_since"):
        st_file["cf_since"] = mctx.iso(now)         # the counterfactual starts with the first run that knows about the retirement

    seen = {k: v for k, v in st_file.get("seen_events", {}).items()
            if (pb._parse_dt(v) or now) > now - timedelta(days=7)}
    for e in detect_events(state, ctx_sum, seen, now):
        seen[e] = mctx.iso(now)

    record = {
        "ts": mctx.iso(now), "trigger": trigger, "prompt_version": PROMPT_VERSION, "mode": settings["mode"], "policy": policy, "policy_configured": settings["policy"], "policy_note": policy_note,
        "grow": rules.get("detail") if policy == "grow" else None, "drawdown_factor": rules.get("drawdown_factor") if policy == "grow" else None,
        "effective_mode": eff, "mode_reasons": eff_reasons, "dry_run_check": check,
        "llm": {"valid": verdict["ok"], "notes": verdict["notes"], "proposal": verdict["weights"] if verdict["ok"] else None,
                "reasons": verdict["reasons"], "raw_reply": (reply or "")[:4000], "attempts": attempts,
                "invalid_replies": [r[:4000] for r in (replies if not verdict["ok"] else replies[:-1])]},
        "previous": prev, "final": final, "steps": rules["steps"], "rules_fired": rules["rules_fired"], "actions": actions,
        "probe": probe_log, "excluded": settings["excluded"], "stuck_bots": st_file.get("stuck_bots", []),
        "retired": settings["retired"], "warnings": rules["warnings"], "stale_bots": state["stale_bots"], "unreachable_bots": state["unreachable_bots"],
        "retired_bots": state["retired_bots"],
        "portfolio": {k: state[k] for k in ("equity", "hwm", "floor", "budget", "gross", "partial", "missing_bots")},
        "drawdown": state["drawdown"], "context": ctx_sum,
    }
    with open(data_dir / ALLOCATIONS_FILE, "a", encoding="utf-8") as f:       # append-only
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
    st_file.update({"weights": final, "last_run": mctx.iso(now), "seen_events": seen,
                    "last_effective_mode": eff})
    _atomic(data_dir / STATE_FILE, json.dumps(st_file, indent=1))

    cf = counterfactual(state["trades_by_bot"], load_allocations(data_dir), now, pb._parse_dt(st_file.get("cf_since")))
    _atomic(data_dir / COUNTERFACTUAL_FILE, json.dumps(cf, indent=1))
    if eff == "enforce" and cf.get("verdict") == "revert_to_shadow":
        kill(data_dir, f"counterfactual verdict: {cf['reason']}", "auto:counterfactual", control or default_control(), fetcher, now)
        record["auto_revert"] = True
    latest = {**{k: record[k] for k in ("ts", "trigger", "mode", "policy", "policy_configured", "policy_note", "grow", "drawdown_factor", "effective_mode", "mode_reasons", "final", "previous",
                                        "rules_fired", "steps", "actions", "llm", "probe", "excluded", "stuck_bots", "retired", "warnings",
                                        "stale_bots", "unreachable_bots", "retired_bots", "portfolio")},
              "context": ctx_sum, "counterfactual": cf, "kill": kill_state(data_dir), "managed_bots": MANAGED_BOTS, "active_bots": active,
              "never_managed": NEVER_MANAGED, "paused_by_allocator": st_file.get("paused_by_allocator", []),
              "bots": {b: {k: v for k, v in s.items()} for b, s in state["bots"].items()},
              "note": ("SHADOW: hypothetical weights, no bot was touched" if eff != "enforce" else "ENFORCE: weights applied")}
    _atomic(data_dir / LATEST_FILE, json.dumps(latest, indent=1, default=str))
    log.info("allocator: trigger=%s mode=%s->%s policy=%s llm_valid=%s rules=%s final=%s", trigger, settings["mode"], eff,
             policy, verdict["ok"], rules["rules_fired"], final)
    return latest


def check_events(data_dir: Path, now: datetime | None = None, fetcher=None, llm=None, control=None, env: dict | None = None) -> dict | None:
    """Hourly, after the context snapshot: run the allocator only when a NEW event appeared; in enforce also
    re-pause bots that came back running (a bot restart resets Freqtrade's paused state)."""
    settings = load_settings(env)
    if settings["mode"] == "off":
        return None
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    fetcher = fetcher or pb.default_fetcher()
    st_file = load_json(data_dir / STATE_FILE, {})
    if st_file.get("last_effective_mode") == "enforce" and not kill_state(data_dir):
        eff, _, _ = effective_mode(data_dir, settings, fetcher)
        if eff == "enforce":
            w = st_file.get("weights", {})
            actions = apply_enforce(control or default_control(), st_file, {b: w.get(b, 1.0) for b in MANAGED_BOTS}, fetcher, active_bots(settings))
            if actions:
                log.warning("allocator: reconcile actions %s", actions)
                _atomic(data_dir / STATE_FILE, json.dumps(st_file, indent=1))
    state = gather_state(fetcher, data_dir, now, settings, with_trades=False)
    ctx_sum = context_summary(mctx.load_latest(data_dir), now)
    new = detect_events(state, ctx_sum, st_file.get("seen_events", {}), now)
    if not new:
        return None
    log.warning("allocator: new event(s) %s -- running out of schedule", new)
    return run_allocation(data_dir, now, fetcher, llm, control, env, trigger="event:" + ",".join(new))


if __name__ == "__main__":      # manual: docker exec survival-agent python -m backend.engine.allocator
    logging.basicConfig(level=logging.INFO)
    run_allocation(Path(os.getenv("DATA_DIR", "/data")))
