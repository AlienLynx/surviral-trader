"""
P19-A -- signal ledger and forward-return audit (report only; nothing here trades or blocks).

Question answered: "when an LLM bot said buy / sell_short / hold on a pair, did the price then
move the way it said, by more than the market did anyway?"

Ledger (append-only files under DATA_DIR, past lines are never rewritten):
  signals.jsonl          one line per decision: time, bot, pair, action, confidence (only where the
                         bot logs it -- today Dip), price at the decision
  signals_forward.jsonl  one line per (signal, horizon) once that horizon has matured:
                         price0, price_h, return
Source of the decisions: each bot's own `freqtrade.log` (mounted read-only at BOTS_DIR/<bot>), i.e.
the `DeepSeek said <action> for <pair> -- <reason>` lines. `decisions.log` was checked first and does
NOT hold this -- it is the dormant accounting engine's cycle log -- so nothing new is logged and no
strategy is touched. The decision price is not in the log; it is the close of the last COMPLETED 1h
candle before the decision, read from the bot's own REST candles (/api/v1/pair_candles), never from an
external site.

No look-ahead: for a decision whose candle closed at t0, the decision price is the close of the candle
that OPENED at t0-1h, and the forward price for horizon h is the close of the candle that opened at
t0+h-1h. A horizon is only computed once t0+h <= now and that candle exists. Only candles after the
decision ever enter a forward return.

Benchmark: the unconditional mean forward return of ALL pairs that bot decided on at the same candle
(cross-section at t0), so a rising or falling market is subtracted out. Short signals are sign-flipped.
Metrics per bot and direction: n, hit rate, mean excess return with a bootstrap 95% CI, and the rank
correlation of confidence with the directional forward return. First report at n >= 200 decisions per bot.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import evidence

log = logging.getLogger("survival-agent.signal_audit")

LLM_BOTS = ["main", "shorts", "moonshot"]      # their decisions are parsed from the bots' own logs
AUDITED_BOTS = LLM_BOTS + ["kronos"]     # kronos (P20.4 part B) is one more signal SOURCE: its container writes the signals, the audit scores them
KRONOS_FILE = "kronos_signals.jsonl"
KRONOS_MIN_ACTIONS = 200                 # report criteria for the first conclusions on kronos (a label in the audit, never an action)
KRONOS_MIN_DAYS = 14
KRONOS_MIN_HALF = 30
HORIZONS = (1, 4, 24)
MIN_ACTIONS_PER_DIRECTION = 100      # buy and sell_short counted separately; hold never counts
MIN_UNIVERSE = 3
EXPIRE_AFTER_HOURS = 72
CANDLE_KEEP_DAYS = 60

SIGNALS_FILE = "signals.jsonl"
FORWARD_FILE = "signals_forward.jsonl"
STATE_FILE = "signals_state.json"
CANDLES_FILE = "signals_candles.json"
AUDIT_FILE = "signals_audit.json"

DECISION_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} - \S+ - INFO - "
    r"DeepSeek said (buy the dip|buy \(long\)|buy|sell_short|hold) for (\S+) -- (.*)$")
ACTION_MAP = {"buy the dip": "buy", "buy (long)": "buy", "buy": "buy", "sell_short": "sell_short", "hold": "hold"}
CONF_RE = re.compile(r"confidence(?:=|:\s*)(\d{1,3})", re.I)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def parse_decision_line(line: str) -> dict | None:
    m = DECISION_RE.match(line)
    if not m:
        return None
    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    conf = None
    cm = CONF_RE.search(m.group(4))
    if cm:
        value = int(cm.group(1))
        conf = value if 0 <= value <= 100 else None
    return {"ts": ts, "action": ACTION_MAP[m.group(2)], "pair": m.group(3), "confidence": conf}


# ------------------------------------------------------------------ files
def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with open(path, "a", encoding="utf-8") as f:       # append only: earlier lines are never touched
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


# ------------------------------------------------------------- ingestion
def parse_new_signals(data_dir: Path, bot: str, log_files: list[Path], last_run: datetime | None,
                      seen: set[str]) -> list[dict]:
    """Decisions from this bot's logs that are not in the ledger yet (first occurrence per candle wins:
    a restart re-analyses the same candle and would log it twice)."""
    out = []
    for path in log_files:                                   # oldest -> newest
        if last_run and datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < last_run - timedelta(hours=1):
            continue                                         # a rotated file older than the last run holds nothing new
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "DeepSeek said" not in line:
                    continue
                d = parse_decision_line(line.rstrip("\r\n"))
                if not d:
                    continue
                t0 = floor_hour(d["ts"])
                sid = f"{bot}|{d['pair']}|{_iso(t0)}"
                if sid in seen:
                    continue
                seen.add(sid)
                out.append({"sid": sid, "ts": _iso(d["ts"]), "t0": _iso(t0), "bot": bot, "pair": d["pair"],
                            "action": d["action"], "confidence": d["confidence"], "price": None,
                            "source": f"bot_log:{path.name}"})
    return out


def kronos_dir() -> Path:
    return Path(os.environ.get("KRONOS_DIR", "/kronos"))


def read_kronos_health() -> dict | None:
    """The kronos-signal container's own summary of its last cycle (cycle_seconds, pairs scored / skipped, truncated, model and revisions).
    It is written by the container and only READ here, so nobody has to copy numbers by hand."""
    try:
        h = json.loads((kronos_dir() / "kronos_health.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(h, dict):
        return None
    skipped = h.get("skipped") or {}
    return {"t0": h.get("t0"), "finished_at": h.get("finished_at"), "cycle_seconds": h.get("cycle_seconds"), "pairs_scored": h.get("pairs_scored"),
            "candidates": h.get("candidates"), "truncated": h.get("truncated"), "actions": h.get("actions"), "skipped_count": len(skipped),
            "skipped": dict(list(skipped.items())[:30]), "model": h.get("model")}


def ingest_kronos(path: Path, seen: set) -> list[dict]:
    """New kronos signals from the kronos-signal container's append-only file (mounted read-only). One record per (pair, t0); malformed lines and
    unknown actions are dropped; nothing already in the ledger is ever re-added."""
    out: list[dict] = []
    for rec in _read_jsonl(path):
        sid = rec.get("sid")
        if not sid or sid in seen or rec.get("bot") != "kronos":
            continue
        if any(k not in rec for k in ("t0", "ts", "pair", "action")) or rec["action"] not in ("buy", "sell_short", "hold"):
            continue
        seen.add(sid)
        out.append({"sid": sid, "ts": rec["ts"], "t0": rec["t0"], "bot": "kronos", "pair": rec["pair"], "action": rec["action"],
                    "confidence": rec.get("confidence"), "price": rec.get("price"), "source": "kronos-signal",
                    "src_bot": rec.get("src_bot"), "kronos": rec.get("kronos")})
    return out


def parse_candles(payload: dict | None) -> dict[str, float]:
    """/api/v1/pair_candles -> {open_iso: close}. Only the `date` and `close` columns are read."""
    if not payload or "columns" not in payload or "data" not in payload:
        return {}
    cols = payload["columns"]
    try:
        di, ci = cols.index("date"), cols.index("close")
    except ValueError:
        return {}
    out = {}
    for row in payload["data"]:
        try:
            d = row[di]
            dt = datetime.fromtimestamp(d / 1000, timezone.utc) if isinstance(d, (int, float)) \
                else datetime.fromisoformat(str(d).replace("Z", "+00:00"))
            if row[ci] is not None:
                out[_iso(dt)] = float(row[ci])
        except (ValueError, TypeError, IndexError):
            continue
    return out


def fetch_candles(fetcher, bot: str, pair: str) -> dict[str, float]:
    return parse_candles(fetcher._json(bot, f"/api/v1/pair_candles?pair={quote(pair, safe='')}&timeframe=1h&limit=1000"))


def _candle_close(cache: dict, bot: str, pair: str, open_dt: datetime) -> float | None:
    return cache.get(f"{bot}|{pair}", {}).get(_iso(open_dt))


# ---------------------------------------------------------- forward returns
def forward_record(sig: dict, h: int, cache: dict, now: datetime) -> dict | None:
    """Forward return of one signal over h hours, or None if it cannot be computed honestly yet.
    decision price = close of the candle that opened at t0-1h; forward price = close of the candle that
    opened at t0+h-1h. Nothing at or after t0 is used for the decision price."""
    t0 = _parse_iso(sig["t0"])
    if t0 + timedelta(hours=h) > now:
        return None                                   # horizon not matured: its candle is not closed yet
    p0 = _candle_close(cache, sig["bot"], sig["pair"], t0 - timedelta(hours=1))
    ph = _candle_close(cache, sig["bot"], sig["pair"], t0 + timedelta(hours=h - 1))
    if not p0 or not ph:
        return None
    return {"sid": sig["sid"], "h": h, "price0": p0, "price_h": ph, "ret": ph / p0 - 1.0, "resolved_at": _iso(now)}


def resolve(signals: list[dict], done: set[tuple], cache: dict, now: datetime) -> list[dict]:
    out = []
    for s in signals:
        for h in HORIZONS:
            if (s["sid"], h) in done:
                continue
            rec = forward_record(s, h, cache, now)
            if rec:
                out.append(rec)
                done.add((s["sid"], h))
    return out


# ---------------------------------------------------------------- audit
def _direction_stats(items: list[dict]) -> dict:
    """items: [{dir_ret, excess, conf}] for one bot/direction/horizon."""
    n = len(items)
    if n == 0:
        return {"n": 0}
    excess = [i["excess"] for i in items]
    ci = evidence.bootstrap_ci_mean(excess)
    paired = [(i["conf"], i["dir_ret"]) for i in items if i["conf"] is not None]
    rho = evidence.spearman([p[0] for p in paired], [p[1] for p in paired]) if len(paired) >= 10 else None
    return {
        "n": n,
        "hit_rate": round(sum(1 for i in items if i["dir_ret"] > 0) / n, 4),
        "hit_rate_vs_benchmark": round(sum(1 for e in excess if e > 0) / n, 4),
        "mean_fwd_return_pct": round(sum(i["dir_ret"] for i in items) / n * 100, 4),
        "mean_excess_pct": round(sum(excess) / n * 100, 4),
        "excess_ci95_pct": None if ci is None else [round(ci["lo"] * 100, 4), round(ci["hi"] * 100, 4)],
        "conf_rank_corr": None if rho is None else round(rho, 4),
        "conf_n": len(paired),
    }


CONTEXT_FLAGS = ("funding_extreme", "oi_spike", "macro_event_within_24h", "liquidation_cascade")
MIN_N_PER_SLICE = 50
CONTEXT_FILE = "context_snapshots.jsonl"


def compact_context(snapshot: dict) -> dict:
    """Only what the slices need from one hourly snapshot: the global flags and each coin's two flags."""
    def val(f):
        return f.get("value") if isinstance(f, dict) and f.get("status") in ("ok", "stale") else None
    fl = snapshot.get("flags", {})
    return {"ts": snapshot.get("ts"),
            "macro_event_within_24h": val(fl.get("macro_event_within_24h")),
            "liquidation_cascade": val(fl.get("liquidation_cascade")),
            "coins": {c: {"funding_extreme": val(d.get("flags", {}).get("funding_extreme")),
                          "oi_spike": val(d.get("flags", {}).get("oi_spike"))}
                      for c, d in snapshot.get("coins", {}).items()}}


def make_context_lookup(compact: list[dict], max_age_h: float = 3.0):
    """sig -> {flag: True/False/None} from the LAST snapshot taken at or before the decision's own timestamp and
    no older than 3h. A snapshot taken after the decision can never be used (no look-ahead)."""
    import bisect
    rows = sorted(((_parse_iso(c["ts"]), c) for c in compact if c.get("ts")), key=lambda r: r[0])
    times = [r[0] for r in rows]

    def lookup(sig: dict):
        ts = _parse_iso(sig["ts"])
        i = bisect.bisect_right(times, ts) - 1
        if i < 0 or ts - times[i] > timedelta(hours=max_age_h):
            return None
        snap = rows[i][1]
        coin = sig["pair"].split("/")[0]
        cf = snap["coins"].get(coin, {})
        return {"funding_extreme": cf.get("funding_extreme"), "oi_spike": cf.get("oi_spike"),
                "macro_event_within_24h": snap.get("macro_event_within_24h"),
                "liquidation_cascade": snap.get("liquidation_cascade")}
    return lookup


def _slice_stats(items: list[dict]) -> dict:
    if len(items) < MIN_N_PER_SLICE:
        return {"n": len(items), "status": "not enough data", "needed": MIN_N_PER_SLICE}
    return {**_direction_stats(items), "status": "ok"}


def context_slices(slice_items: dict, per_bot_ok: bool = True) -> dict:
    """slice_items[(h, direction)] = [{bot, dir_ret, excess, conf, flags}]. Per flag: signals with the flag true vs
    false (None = flag unknown at that moment: excluded), pooled over the audited bots and per bot; a slice with
    fewer than 50 signals says 'not enough data'."""
    out: dict = {}
    for flag in CONTEXT_FLAGS:
        out[flag] = {}
        for (h, direction), items in sorted(slice_items.items()):
            scopes = {"all": items}
            for b in sorted({i["bot"] for i in items}):
                scopes[b] = [i for i in items if i["bot"] == b]
            for scope, rows in scopes.items():
                yes = [i for i in rows if i["flags"] and i["flags"].get(flag) is True]
                no = [i for i in rows if i["flags"] and i["flags"].get(flag) is False]
                cell = {"flag_true": _slice_stats(yes), "flag_false": _slice_stats(no)}
                if cell["flag_true"].get("status") == "ok" and cell["flag_false"].get("status") == "ok":
                    cell["diff_mean_excess_pct"] = round(cell["flag_true"]["mean_excess_pct"] - cell["flag_false"]["mean_excess_pct"], 4)
                out[flag].setdefault(scope, {}).setdefault(direction, {})[f"h{h}"] = cell
    return out


def _ci_pct(items: list[dict]):
    ci = evidence.bootstrap_ci_mean([i["excess"] for i in items])
    return None if ci is None else (ci["lo"], ci["hi"])


def kronos_verdict(counts: dict, kitems: dict, first_ts: str | None, last_ts: str | None) -> dict:
    """Report label for the kronos source (NOT code that acts). Per direction: 'not enough data' until >= 200 actions and >= 14 days; then 'edge' only if the
    lower bound of the 95% CI of the excess over the benchmark is above zero at 4h or 24h AND also in BOTH halves of the period (split by time); 'no_edge' if
    every computed upper bound is below zero; otherwise 'unproven'."""
    days = 0
    if first_ts and last_ts:
        days = (_parse_iso(last_ts) - _parse_iso(first_ts)).days
    out: dict = {"criteria": f">= {KRONOS_MIN_ACTIONS} actions per direction and >= {KRONOS_MIN_DAYS} days; edge = CI lower bound > 0 at 4h or 24h and in both halves "
                             f"(each half >= {KRONOS_MIN_HALF} signals); no_edge = every upper bound < 0; else unproven. Only after that is use as a bot filter discussed.",
                 "days_of_data": days, "directions": {}}
    for d in ("buy", "sell_short"):
        n = counts.get(d, 0)
        if n < KRONOS_MIN_ACTIONS or days < KRONOS_MIN_DAYS:
            out["directions"][d] = {"status": "not enough data", "actions": n, "needed_actions": KRONOS_MIN_ACTIONS, "days": days, "needed_days": KRONOS_MIN_DAYS}
            continue
        checks, edge, uppers = {}, False, []
        for h in (4, 24):
            items = sorted(kitems.get((d, h), []), key=lambda i: i["t0"])
            half = len(items) // 2
            full, h1, h2 = _ci_pct(items), _ci_pct(items[:half]) if half >= KRONOS_MIN_HALF else None, _ci_pct(items[half:]) if len(items) - half >= KRONOS_MIN_HALF else None
            checks[f"h{h}"] = {"n": len(items), "ci95_pct": None if full is None else [round(full[0] * 100, 4), round(full[1] * 100, 4)],
                               "half1_lo_pct": None if h1 is None else round(h1[0] * 100, 4), "half2_lo_pct": None if h2 is None else round(h2[0] * 100, 4)}
            if full is not None:
                uppers.append(full[1])
                if full[0] > 0 and h1 is not None and h2 is not None and h1[0] > 0 and h2[0] > 0:
                    edge = True
        status = "edge" if edge else ("no_edge" if uppers and all(u < 0 for u in uppers) else "unproven")
        out["directions"][d] = {"status": status, "actions": n, "days": days, "checks": checks}
    return out


def build_audit(signals: list[dict], forward: list[dict], now: datetime, context_lookup=None) -> dict:
    slice_items: dict[tuple, list[dict]] = {}
    sig_by = {s["sid"]: s for s in signals}
    fwd: dict[tuple, float] = {(f["sid"], f["h"]): f["ret"] for f in forward if f["sid"] in sig_by}
    bots_out: dict[str, dict] = {}
    for bot in AUDITED_BOTS:
        kitems: dict[tuple, list[dict]] = {}
        bot_sigs = [s for s in signals if s["bot"] == bot]
        total = len(bot_sigs)
        entry: dict = {
            "decisions": total,
            "by_action": {a: sum(1 for s in bot_sigs if s["action"] == a) for a in ("buy", "sell_short", "hold")},
            "confidence_logged": any(s["confidence"] is not None for s in bot_sigs),
            "first_ts": min((s["ts"] for s in bot_sigs), default=None),
            "last_ts": max((s["ts"] for s in bot_sigs), default=None),
            "resolved": {str(h): sum(1 for s in bot_sigs if (s["sid"], h) in fwd) for h in HORIZONS},
            "pending": {str(h): sum(1 for s in bot_sigs if (s["sid"], h) not in fwd
                                    and _parse_iso(s["t0"]) + timedelta(hours=h + EXPIRE_AFTER_HOURS) >= now)
                        for h in HORIZONS},
            "expired_unresolved": {str(h): sum(1 for s in bot_sigs if (s["sid"], h) not in fwd
                                               and _parse_iso(s["t0"]) + timedelta(hours=h + EXPIRE_AFTER_HOURS) < now)
                                   for h in HORIZONS},
        }
        # the threshold counts ACTIONS per direction (buy, sell_short); hold decisions never count towards it
        counts = {d: entry["by_action"][d] for d in ("buy", "sell_short", "hold")}
        ready = {d: counts[d] >= MIN_ACTIONS_PER_DIRECTION for d in counts}
        entry["status"] = "ok" if (ready["buy"] or ready["sell_short"]) else "not enough data"
        entry["needed"] = MIN_ACTIONS_PER_DIRECTION
        entry["note"] = (f"buy {counts['buy']}/{MIN_ACTIONS_PER_DIRECTION}, sell_short {counts['sell_short']}/{MIN_ACTIONS_PER_DIRECTION} actions "
                         "(hold is not counted); a direction shows metrics only from its own 100 actions")
        by_dir: dict[str, dict] = {d: {"actions": counts[d], "status": "ok" if ready[d] else "not enough data",
                                       "needed": MIN_ACTIONS_PER_DIRECTION} for d in counts}
        for h in HORIZONS:
            # cross-sectional benchmark per candle: mean forward return over every pair the bot decided on at t0
            groups: dict[str, list[float]] = {}
            for s in bot_sigs:
                r = fwd.get((s["sid"], h))
                if r is not None:
                    groups.setdefault(s["t0"], []).append(r)
            bench = {t0: sum(v) / len(v) for t0, v in groups.items() if len(v) >= MIN_UNIVERSE}
            for direction in ("buy", "sell_short", "hold"):
                if not ready[direction]:
                    continue
                sign = -1.0 if direction == "sell_short" else 1.0
                items = []
                for s in bot_sigs:
                    if s["action"] != direction:
                        continue
                    r, b = fwd.get((s["sid"], h)), bench.get(s["t0"])
                    if r is None or b is None:
                        continue
                    items.append({"dir_ret": sign * r, "excess": sign * (r - b), "conf": s["confidence"], "t0": s["t0"]})
                    if context_lookup is not None and direction != "hold":
                        slice_items.setdefault((h, direction), []).append(
                            {**items[-1], "bot": bot, "flags": context_lookup(s)})
                by_dir[direction][f"h{h}"] = _direction_stats(items)
                kitems[(direction, h)] = items
        entry["by_direction"] = by_dir
        if bot == "kronos":
            entry["verdict"] = kronos_verdict(counts, kitems, entry["first_ts"], entry["last_ts"])
            entry["source"] = "kronos-signal container (NeoQuasar/Kronos-small); confidence = |expected 4h return|; raw 1/4/24h forecasts are in each ledger record"
        bots_out[bot] = entry
    ctx_out = ({"status": "no context snapshots yet", "note": "context_snapshots.jsonl is written hourly by P20-A"}
               if context_lookup is None else
               {"status": "ok", "min_n_per_slice": MIN_N_PER_SLICE, "flags": list(CONTEXT_FLAGS),
                "rule": "flag value = last hourly snapshot at or before the decision (<= 3h old); unknown flag -> excluded; "
                        "buy and sell_short only; a bot/direction below 100 actions is not included",
                "slices": context_slices(slice_items)})
    return {
        "context_slices": ctx_out,
        "generated_at": _iso(now), "horizons_hours": list(HORIZONS), "min_actions_per_direction": MIN_ACTIONS_PER_DIRECTION,
        "benchmark": "unconditional mean forward return of all pairs the bot decided on at the same candle; short signals sign-flipped",
        "no_lookahead": "decision price = close of the candle opened at t0-1h; forward price = close of the candle opened at t0+h-1h; only matured horizons are computed",
        "source": "each bot's own log (DeepSeek said ... lines) + its own REST candles; decisions.log does not contain per-pair decisions",
        "caveats": [
            "confidence is logged only by the kronos source (|expected 4h return|); the LLM bots' rank correlation is null",
            "decision logs exist only since the --logfile change (commit 8153a59)",
            "a pair that left a bot's whitelist before its candles were fetched stays unresolved (counted as expired)",
        ],
        "total_decisions": len(signals), "total_forward_records": len(forward), "bots": bots_out,
    }


# -------------------------------------------------------------- nightly run
def run_nightly(data_dir: Path, now: datetime | None = None, fetcher=None, log_files_fn=None) -> dict:
    from . import bot_state as bs
    from . import runtime_settings as rs
    if str(rs.merged_env().get("SIGNAL_AUDIT_MODE", "on")).strip().lower() == "off":
        return {"mode": "off", "skipped": True}
    now = now or datetime.now(timezone.utc)
    data_dir = Path(data_dir)
    if fetcher is None:
        from . import portfolio_brain
        fetcher = portfolio_brain.default_fetcher()
    if log_files_fn is None:
        from .. import export
        log_files_fn = export.log_files

    state = _load_json(data_dir / STATE_FILE, {})
    last_run = _parse_iso(state["last_run"]) if state.get("last_run") else None
    signals = _read_jsonl(data_dir / SIGNALS_FILE)
    seen = {s["sid"] for s in signals}
    new: list[dict] = []
    for bot in LLM_BOTS:
        new += parse_new_signals(data_dir, bot, log_files_fn(bot), last_run, seen)
    new += ingest_kronos(kronos_dir() / KRONOS_FILE, seen)

    # candles for every (bot, pair) that has a new or unresolved signal; cached so a pair that later leaves
    # the whitelist can still be resolved from what was fetched while it was there
    forward = _read_jsonl(data_dir / FORWARD_FILE)
    done = {(f["sid"], f["h"]) for f in forward}
    cache: dict = _load_json(data_dir / CANDLES_FILE, {})
    want = {(s["bot"], s["pair"], s.get("src_bot") or s["bot"]) for s in signals + new
            if any((s["sid"], h) not in done and _parse_iso(s["t0"]) + timedelta(hours=h) <= now
                   for h in HORIZONS) or s in new}
    retired = set(bs.retired_from_env())
    for bot, pair, src in sorted(want):
        if src in retired:           # P20.4: a retired bot is not polled; what was cached stays usable for resolving old signals
            continue
        fresh = fetch_candles(fetcher, src, pair)
        if fresh:
            cache.setdefault(f"{bot}|{pair}", {}).update(fresh)
    horizon_cut = _iso(now - timedelta(days=CANDLE_KEEP_DAYS))
    cache = {k: {t: c for t, c in v.items() if t >= horizon_cut} for k, v in cache.items()}
    (data_dir / CANDLES_FILE).write_text(json.dumps(cache, separators=(",", ":")), encoding="utf-8")

    for s in new:                                          # decision price where the candle is already known
        p = _candle_close(cache, s["bot"], s["pair"], _parse_iso(s["t0"]) - timedelta(hours=1))
        if p is not None or "price" not in s:              # a kronos record already carries the close it forecast from
            s["price"] = p
    _append_jsonl(data_dir / SIGNALS_FILE, new)
    signals += new

    new_forward = resolve(signals, done, cache, now)
    _append_jsonl(data_dir / FORWARD_FILE, new_forward)
    forward += new_forward

    lookup = None
    ctx_path = data_dir / CONTEXT_FILE
    if ctx_path.is_file():
        compact = []
        for line in ctx_path.read_text(encoding="utf-8").splitlines():
            try:
                compact.append(compact_context(json.loads(line)))
            except (ValueError, AttributeError):
                continue
        if compact:
            lookup = make_context_lookup(compact)
    audit = build_audit(signals, forward, now, lookup)
    audit["run"] = {"new_decisions": len(new), "new_forward_records": len(new_forward)}
    audit["kronos_health"] = read_kronos_health()
    (data_dir / AUDIT_FILE).write_text(json.dumps(audit, indent=2), encoding="utf-8")
    (data_dir / STATE_FILE).write_text(json.dumps({"last_run": _iso(now)}), encoding="utf-8")
    log.info("signal_audit: +%d decisions, +%d forward records, %d decisions total", len(new), len(new_forward), len(signals))
    return audit


if __name__ == "__main__":     # manual run: docker exec survival-agent python -m backend.engine.signal_audit
    logging.basicConfig(level=logging.INFO)
    run_nightly(Path(os.getenv("DATA_DIR", "/data")))
