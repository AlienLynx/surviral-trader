"""
"Why isn't this bot trading?" -- one read-only answer per bot, built from what the bot itself logged in the last hours, the allocator's weight and the bot's own state. Nothing is guessed: every line
is a count from a log or a value from a file / the bot's REST API, and the findings are ordered by how much of the silence each explains.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import export
from .engine import freqtrade_client as ftc

DECISION_RE = re.compile(r"said (buy the dip|buy \(long\)|buy|sell_short|hold) for (\S+)")
VETO_RE = re.compile(r"llm veto of kronos (buy|sell_short)")
NOSIG_RE = re.compile(r"kronos: no fresh signal")
REJECT_RE = re.compile(r"confirm_trade_entry: rejecting (\S+) -- (.*)")
TOO_SMALL_RE = re.compile(r"Stake amount for pair (\S+) is too small \(([\d.]+) < ([\d.]+)\)")
ENTRY_FILL_RE = re.compile(r"'type': entry_fill.*?'pair': '([^']+)'")
SIGNAL_RE = re.compile(r"rule-based (?:short )?signal for (\S+)|signal for (\S+)", re.I)


def _weight(bot: str, allocator_dir: Path) -> dict:
    try:
        w = json.loads((allocator_dir / "weights.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"known": False}
    try:
        upd = datetime.fromisoformat(str(w.get("updated_at")).replace("Z", "+00:00"))
        age = int((datetime.now(timezone.utc) - upd).total_seconds())
    except ValueError:
        age = None
    e = (w.get("weights") or {}).get(bot)
    return {"known": True, "mode": w.get("mode"), "age_seconds": age, "weight": (e or {}).get("weight") if isinstance(e, dict) else None, "fresh": age is not None and age <= 36 * 3600}


def _reject_kind(reason: str) -> str:
    r = reason.lower()
    if "announcement guard" in r:
        return "announcement guard (delisting notice)"
    if "funding" in r:
        return "funding rule (the short would pay funding)"
    if "loss-streak" in r or "cooldown" in r:
        return "loss-streak cooldown"
    if "dry_run" in r or "live_mode" in r.replace("-", "_"):
        return "live-mode gate"
    return reason[:60]


def why(bot: str, hours: int = 24, allocator_dir: Path | None = None) -> dict | None:
    if bot not in ftc.BOTS:
        return None
    allocator_dir = Path(allocator_dir or os.environ.get("ALLOCATOR_DIR") or "/allocator")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    files = export.log_files(bot)
    lines = [x for x in (export.lines_since(files, cutoff) if files else []) if x.strip()]
    decisions, vetoes, nosig, rejects, small, fills, signals = Counter(), Counter(), 0, Counter(), Counter(), Counter(), 0
    small_examples: dict[str, tuple[float, float]] = {}
    for ln in lines:
        m = DECISION_RE.search(ln)
        if m:
            decisions["buy" if m.group(1).startswith("buy") else m.group(1)] += 1
            v = VETO_RE.search(ln)
            if v:
                vetoes[v.group(1)] += 1
            elif NOSIG_RE.search(ln):
                nosig += 1
            continue
        m = REJECT_RE.search(ln)
        if m:
            rejects[_reject_kind(m.group(2))] += 1
            continue
        m = TOO_SMALL_RE.search(ln)
        if m:
            small[m.group(1)] += 1
            small_examples[m.group(1)] = (float(m.group(2)), float(m.group(3)))
            continue
        m = ENTRY_FILL_RE.search(ln)
        if m:
            fills[m.group(1)] += 1
            continue
        if SIGNAL_RE.search(ln) and "DeepSeek said" not in ln:
            signals += 1
    cfg = ftc._authed_get(bot, "/api/v1/show_config")
    state = cfg.get("state") if isinstance(cfg, dict) else None
    max_open = cfg.get("max_open_trades") if isinstance(cfg, dict) else None
    status = ftc._authed_get(bot, "/api/v1/status")
    open_n = len(status) if isinstance(status, list) else None
    tr = ftc._authed_get(bot, "/api/v1/trades?limit=20")
    stakes = [float(t.get("stake_amount") or 0) for t in ((tr or {}).get("trades") or []) if t.get("stake_amount")]
    wt = _weight(bot, allocator_dir)
    n_dec = sum(decisions.values())
    n_veto = sum(vetoes.values())
    findings = []

    def add(severity: str, text: str):
        findings.append({"severity": severity, "text": text})

    if not lines:
        add("warn", f"No log lines in the last {hours} h were readable for this bot (log volume not mounted, or the bot is down).")
    if state and str(state).lower() != "running":
        add("bad", f"The bot's state is {str(state).upper()}: it opens no new trades. If the allocator paused it, its weight is 0 or a rule is firing (see the allocator card on the Portfolio tab); you can also press Start in the bot's own UI.")
    if wt.get("known") and wt.get("mode") == "enforce" and wt.get("fresh"):
        w = wt.get("weight")
        if w is not None and w <= 0:
            add("bad", "The allocator weight is 0: the bot is paused by the allocator.")
        elif w is not None and w < 1:
            typical = statistics.median(stakes) if stakes else None
            add("warn", f"The allocator weight is {w}: every stake is scaled to {int(w * 100)}% of normal" + (f" (recent stakes: median ${typical:.2f})" if typical else "") + ". A scaled stake below the exchange minimum order cannot open (see 'too small' below).")
    elif wt.get("known") and not wt.get("fresh"):
        add("info", "The allocator weights file is older than 36 h, so the bots ignore it (stake multiplier 1.0).")
    if small:
        tot = sum(small.values())
        ex = ", ".join(f"{p} (stake ${small_examples[p][0]:.2f} < minimum ${small_examples[p][1]:.2f})" for p in list(small)[:4])
        add("bad" if tot > 20 else "warn", f"{tot} entries were dropped because the stake was below the exchange's minimum order: {ex}.")
    if n_dec:
        holds = decisions.get("hold", 0)
        acts = n_dec - holds
        add("info", f"{n_dec} decisions in {hours} h: {decisions.get('buy', 0)} buy, {decisions.get('sell_short', 0)} sell_short, {holds} hold.")
        if n_veto:
            add("warn" if acts == 0 else "info", f"The LLM vetoed {n_veto} of the Kronos proposals ({vetoes.get('buy', 0)} buy, {vetoes.get('sell_short', 0)} sell_short). With the default source (kronos_llm) a proposal only becomes a trade when the LLM also agrees; in a falling or choppy market it mostly does not. Set <BOT>_DECISION_SOURCE=kronos to follow Kronos without the LLM.")
        if nosig:
            add("warn", f"{nosig} decisions were 'hold: no fresh Kronos signal' (the pair is not in the rank file, or the file is older than 2h15m). Check the Kronos feed on the Signals tab.")
        if acts == 0 and not n_veto and not nosig:
            add("warn", "The bot decided hold on every pair: there was no setup it liked.")
    elif lines:
        add("info", f"No LLM decisions in {hours} h" + (f"; {signals} rule signals were logged." if signals else "; the rule strategy logged no signals (nothing matched its entry conditions)."))
    for kind, n in rejects.most_common(4):
        add("warn", f"{n} entries were refused by the {kind}.")
    if open_n is not None and max_open is not None and open_n >= max_open:
        add("warn", f"All {max_open} slots are in use ({open_n} open trades): no new entry until one closes.")
    done = sum(fills.values())
    if not findings:
        add("info", "Nothing is blocking it that the logs show.")
    return {"bot": bot, "hours": hours, "state": state, "open_trades": open_n, "max_open_trades": max_open, "entries_filled": done, "allocator": wt, "decisions": dict(decisions), "vetoes": dict(vetoes), "no_signal": nosig,
            "rejections": dict(rejects), "too_small": {k: {"count": v, "stake": small_examples[k][0], "minimum": small_examples[k][1]} for k, v in small.items()}, "findings": findings, "log_lines": len(lines)}
