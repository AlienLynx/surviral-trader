"""Offline parametric tuner for two independent per-bot parameters (P3, P8).

Not LLM fine-tuning: once a day (manual run or cron -- schedule is the
operator's call, this script never runs inside Freqtrade's live loop) it
looks at real closed-trade outcomes per bot and nudges numbers by a
fixed step:

  1. SATELLITE_STAKE_MULTIPLIER (P3) -- satellite-tier stake size,
     nudged by at most 0.05 based on satellite vs core winrate.
  2. loss_streak_cooldown_hours (P8 item 5) -- hours with no new entries
     after LOSS_STREAK_N consecutive losing closed trades, nudged by
     COOLDOWN_STEP_HOURS based on the bot's CURRENT streak length.

The two run independently -- separate cooldown-on-the-change-itself
timer, separate min-sample gate, separate log "param" tag -- neither
touches the other's state, so adding #2 in P8 doesn't disturb #1's
existing history.

Never touched here (hard invariant): stoploss, minimal_roi,
max_open_trades, risk_guard, entry/exit thresholds, DeepSeek prompts,
decision parsing. Only these two numbers.

Tier split (for #1) comes from the trade enter_tag (core/sat... stamped
by the strategies at entry). Untagged trades are ignored, never guessed.
#2 looks at ALL closed trades regardless of tier or enter_tag -- a loss
streak is tier-agnostic.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MIN_RELIABLE_SAMPLE = 10
WINRATE_GAP_PP = 15.0
STEP = 0.05
MIN_MULT = 0.3
MAX_MULT = 0.7
DEFAULT_MULT = 0.5
COOLDOWN_SECONDS = 24 * 3600  # per-parameter: how often THAT parameter may change

# P8 item 5
LOSS_STREAK_N = 3
COOLDOWN_STEP_HOURS = 4.0
MIN_COOLDOWN_HOURS = 0.0  # 0 == disabled, matches the strategies' own default
MAX_COOLDOWN_HOURS = 48.0
DEFAULT_COOLDOWN_HOURS = 0.0

# P10: Dip (contrarian mean-reversion) and Moonshot (high-variance
# memecoin hunting) both treat a multi-loss streak as a normal, expected
# part of their own approach more than main/shorts/survival's
# trend-following one -- they need a LONGER streak before the tuner
# treats it as a real problem worth raising the cooldown for. Even then,
# the cap on how high that cooldown can grow is HALF the default (24h,
# not 48h) so a rough week doesn't lock either one out for two full
# days. main/shorts/survival are untouched -- they fall through to the
# LOSS_STREAK_N/MAX_COOLDOWN_HOURS defaults above via .get().
PER_BOT_LOSS_STREAK_N = {"moonshot": 6}
PER_BOT_MAX_COOLDOWN_HOURS = {"moonshot": 24.0}


def loss_streak_n_for(bot: str) -> int:
    return PER_BOT_LOSS_STREAK_N.get(bot, LOSS_STREAK_N)


def max_cooldown_hours_for(bot: str) -> float:
    return PER_BOT_MAX_COOLDOWN_HOURS.get(bot, MAX_COOLDOWN_HOURS)


PARAM_SATELLITE = "satellite_stake_multiplier"
PARAM_COOLDOWN = "loss_streak_cooldown_hours"


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="tune satellite stake multiplier + loss-streak cooldown offline")
    p.add_argument("--db", required=True)
    p.add_argument("--bot", required=True, choices=["main", "shorts", "moonshot"])
    p.add_argument("--apply", action="store_true")
    p.add_argument("--state-dir", default="auto_tune_state")
    p.add_argument("--log", default="auto_tune_log.jsonl")
    p.add_argument("--sample", type=int, default=50)
    return p.parse_args(argv)


def _tier_of(enter_tag):
    if not enter_tag:
        return None
    tag = str(enter_tag).strip().lower()
    if tag == "core":
        return "core"
    if tag.startswith("sat"):
        return "satellite"
    return None


def load_tagged_trades(db_path, sample):
    """For SATELLITE_STAKE_MULTIPLIER -- core/satellite split by enter_tag.
    Naturally returns 0/0 for a bot whose strategy never sets enter_tag
    (e.g. Moonshot has no core/satellite tiering) -- that's an honest
    "insufficient data" skip below, not a special case needed here."""
    con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT enter_tag, close_profit FROM trades "
            "WHERE is_open = 0 AND close_date IS NOT NULL "
            "ORDER BY close_date DESC"
        ).fetchall()
    finally:
        con.close()
    sat = []
    core = []
    for tag_raw, profit in rows:
        tier = _tier_of(tag_raw)
        if tier is None:
            continue
        try:
            won = float(profit or 0) > 0
        except (TypeError, ValueError):
            continue
        if tier == "satellite" and len(sat) < sample:
            sat.append(won)
        elif tier == "core" and len(core) < sample:
            core.append(won)
        if len(sat) >= sample and len(core) >= sample:
            break
    return sum(sat), len(sat), sum(core), len(core)


def load_closed_trades_chronological(db_path, sample):
    """For loss_streak_cooldown_hours -- every closed trade, any tier,
    most-recent first, capped at `sample`."""
    con = sqlite3.connect("file:" + db_path + "?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT close_profit FROM trades "
            "WHERE is_open = 0 AND close_date IS NOT NULL "
            "ORDER BY close_date DESC LIMIT ?",
            (sample,),
        ).fetchall()
    finally:
        con.close()
    return [float(p or 0) for (p,) in rows]


def current_loss_streak(closed_profits_desc):
    """Consecutive losers counting back from the most recently closed
    trade, stopping at the first winner (or a non-numeric/missing value,
    treated conservatively as ending the streak)."""
    streak = 0
    for profit in closed_profits_desc:
        if profit > 0:
            break
        streak += 1
    return streak


def decide(current, sat_wr, core_wr):
    gap_pp = (core_wr - sat_wr) * 100
    if gap_pp > WINRATE_GAP_PP:
        new = round(current - STEP, 2)
        if new < MIN_MULT:
            return MIN_MULT, "clamped at bound"
        return new, "satellite %.1fpp worse than core -> -%.2f" % (gap_pp, STEP)
    new = round(current + STEP, 2)
    if new > MAX_MULT:
        return MAX_MULT, "clamped at bound"
    return new, "satellite within/better (%+.1fpp gap) -> +%.2f" % (gap_pp, STEP)


def decide_cooldown(current_hours, streak, loss_streak_n=LOSS_STREAK_N, max_cooldown_hours=MAX_COOLDOWN_HOURS):
    """P10: loss_streak_n/max_cooldown_hours default to the global
    constants (main/shorts/survival's behavior, unchanged) but callers
    pass per-bot values resolved via loss_streak_n_for()/
    max_cooldown_hours_for() for dip/moonshot."""
    if streak >= loss_streak_n:
        new = min(max_cooldown_hours, current_hours + COOLDOWN_STEP_HOURS)
        return new, "loss streak %d >= %d -> +%.1fh" % (streak, loss_streak_n, COOLDOWN_STEP_HOURS)
    new = max(MIN_COOLDOWN_HOURS, current_hours - COOLDOWN_STEP_HOURS)
    return new, "loss streak %d < %d -> -%.1fh" % (streak, loss_streak_n, COOLDOWN_STEP_HOURS)


def read_current(state_dir, bot):
    p = Path(state_dir) / bot / "auto_tune.json"
    try:
        val = float(json.loads(p.read_text(encoding="utf-8")).get(
            "satellite_stake_multiplier", DEFAULT_MULT))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_MULT
    return min(MAX_MULT, max(MIN_MULT, val))


def read_current_cooldown(state_dir, bot):
    """P10: clamps against this bot's own max_cooldown_hours_for(bot),
    not always the global MAX_COOLDOWN_HOURS -- a stale value above
    dip/moonshot's tighter 24h cap (e.g. left over from before P10) gets
    pulled back down on read, not just on the next tuned write."""
    p = Path(state_dir) / bot / "auto_tune.json"
    try:
        val = float(json.loads(p.read_text(encoding="utf-8")).get(
            "loss_streak_cooldown_hours", DEFAULT_COOLDOWN_HOURS))
    except (OSError, ValueError, TypeError, AttributeError):
        return DEFAULT_COOLDOWN_HOURS
    return min(max_cooldown_hours_for(bot), max(MIN_COOLDOWN_HOURS, val))


def last_change_ts(log_path, bot, param):
    """Returns the epoch-seconds float of the last 'changed' entry for
    this bot+param, or None. Reads ts_epoch, not the human-readable ISO
    ts -- comparing an ISO string against datetime.timestamp() in main()
    crashed with TypeError on the very first cooldown check that had a
    real prior entry to compare against (caught by P3-T6, not by
    inspection). A log line with no "param" field predates P8 and is
    treated as PARAM_SATELLITE (the only parameter that existed then)
    for backward compatibility."""
    lp = Path(log_path)
    if not lp.exists():
        return None
    ts = None
    for line in lp.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("bot") == bot and e.get("param", PARAM_SATELLITE) == param and e.get("changed"):
            ts = e.get("ts_epoch")
    return ts


def _write_auto_tune_json(state_dir, bot, key, value, now_iso):
    """Merges one key into this bot's auto_tune.json, preserving whatever
    the OTHER parameter already has -- #1 and #2 must not clobber each
    other's last-written value."""
    bot_dir = Path(state_dir) / bot
    bot_dir.mkdir(parents=True, exist_ok=True)
    path = bot_dir / "auto_tune.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[key] = value
    data["updated_at"] = now_iso
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    args = _parse_args(argv)
    now = datetime.now(timezone.utc).isoformat()
    now_ts = datetime.now(timezone.utc).timestamp()

    # --- Parameter 1: SATELLITE_STAKE_MULTIPLIER (P3, unchanged logic) ---
    try:
        sat_w, sat_n, core_w, core_n = load_tagged_trades(args.db, args.sample)
    except (sqlite3.Error, OSError) as e:
        print("[" + args.bot + "] ERROR reading " + args.db + ": " + str(e))
        return 2
    sat_wr = sat_w / sat_n if sat_n else 0.0
    core_wr = core_w / core_n if core_n else 0.0
    current = read_current(args.state_dir, args.bot)
    entry = {"ts": now, "ts_epoch": now_ts, "bot": args.bot, "param": PARAM_SATELLITE,
             "satellite_n": sat_n, "core_n": core_n,
             "satellite_winrate": round(sat_wr, 4), "core_winrate": round(core_wr, 4),
             "old": current, "new": current, "changed": False, "reason": ""}
    if sat_n < MIN_RELIABLE_SAMPLE or core_n < MIN_RELIABLE_SAMPLE:
        entry["reason"] = "skipped: insufficient data (satellite=%d, core=%d)" % (sat_n, core_n)
        print("[" + args.bot + "/" + PARAM_SATELLITE + "] " + entry["reason"])
    else:
        prev = last_change_ts(args.log, args.bot, PARAM_SATELLITE)
        if prev is not None and (now_ts - prev) < COOLDOWN_SECONDS:
            entry["reason"] = "skipped: changed less than 24h ago"
            print("[" + args.bot + "/" + PARAM_SATELLITE + "] " + entry["reason"])
        else:
            new, reason = decide(current, sat_wr, core_wr)
            entry["new"] = new
            entry["reason"] = reason
            entry["changed"] = (new != current)
            print("[" + args.bot + "/" + PARAM_SATELLITE + "] sat %d/%d vs core %d/%d: %s -> %s (%s)"
                  % (sat_w, sat_n, core_w, core_n, current, new, reason))

    # --- Parameter 2: loss_streak_cooldown_hours (P8 item 5) ---
    closed_profits = load_closed_trades_chronological(args.db, args.sample)
    streak = current_loss_streak(closed_profits)
    total_n = len(closed_profits)
    current_cooldown = read_current_cooldown(args.state_dir, args.bot)
    entry2 = {"ts": now, "ts_epoch": now_ts, "bot": args.bot, "param": PARAM_COOLDOWN,
              "closed_n": total_n, "current_loss_streak": streak,
              "old": current_cooldown, "new": current_cooldown, "changed": False, "reason": ""}
    if total_n < MIN_RELIABLE_SAMPLE:
        entry2["reason"] = "skipped: insufficient data (closed=%d)" % total_n
        print("[" + args.bot + "/" + PARAM_COOLDOWN + "] " + entry2["reason"])
    else:
        prev2 = last_change_ts(args.log, args.bot, PARAM_COOLDOWN)
        if prev2 is not None and (now_ts - prev2) < COOLDOWN_SECONDS:
            entry2["reason"] = "skipped: changed less than 24h ago"
            print("[" + args.bot + "/" + PARAM_COOLDOWN + "] " + entry2["reason"])
        else:
            new2, reason2 = decide_cooldown(
                current_cooldown, streak,
                loss_streak_n=loss_streak_n_for(args.bot),
                max_cooldown_hours=max_cooldown_hours_for(args.bot),
            )
            entry2["new"] = new2
            entry2["reason"] = reason2
            entry2["changed"] = (new2 != current_cooldown)
            print("[" + args.bot + "/" + PARAM_COOLDOWN + "] streak=%d/%d closed: %.1fh -> %.1fh (%s)"
                  % (streak, total_n, current_cooldown, new2, reason2))

    if args.apply:
        if entry["changed"]:
            _write_auto_tune_json(args.state_dir, args.bot, PARAM_SATELLITE, entry["new"], now)
        if entry2["changed"]:
            _write_auto_tune_json(args.state_dir, args.bot, PARAM_COOLDOWN, entry2["new"], now)
        with open(args.log, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
            f.write(json.dumps(entry2) + "\n")
        print("[" + args.bot + "] audit lines appended (2)")
    else:
        print("[" + args.bot + "] DRY-RUN: nothing written (pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
