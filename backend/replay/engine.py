"""
The replay engine for ONE bot in ONE mode: it walks hourly candles, asks the bot's REAL entry logic (via sandbox.py) for decisions at the decision steps,
applies entries / exits with the bot's own constants, and records decisions, trades and the equity curve.

Faithfulness and its limits (all of this is also printed in the report):
  * entries and the LLM prompt/parse/rotation/veto/stake logic are the live strategy code; the exits `minimal_roi`, `stoploss` and `custom_exit` are read from the
    strategy class / called on it; ROI and stoploss are checked on EVERY hourly candle (high / low), `custom_exit` on every hourly close.
  * a decision is made at the close of a candle (as-of t) and filled at the OPEN of the next candle plus slippage; fee per side and slippage are parameters.
  * if ROI and stoploss are both hit inside one candle, the stoploss wins (pessimistic). No partial fills, no min-notional, no funding, 1x leverage.
  * stake = Freqtrade's "unlimited" rule: free cash / remaining slots, then the strategy's own custom_stake_amount.
  * with every_n_candles > 1 the strategy's stale-signal streak counts DECISION steps, not hours (its dataframe snapshot only advances at decision steps).
  * the pair list is a fixed snapshot of today's most liquid coins, not a day-by-day VolumePairList; there are no news or market-context texts.
"""
from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import data as rdata
from . import sandbox
from .llm import BudgetExceeded, Stopped


class LiveDecisions:
    """The live bot's real decisions (snapshot) indexed by (hour, pair) in LOG order. Used by the calibration run instead of an LLM."""

    ENTRY = ("buy", "sell_short")

    def __init__(self, decisions: list[dict], restarts: list[str]):
        self.by_hour: dict[str, list[dict]] = {}
        for d in decisions:
            self.by_hour.setdefault(d["t0"], []).append(d)
        self.restart_hours = {r[:13] + ":00:00Z" for r in restarts}
        self.restarts = sorted(datetime.strptime(r, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for r in restarts)

    @staticmethod
    def key(t: datetime) -> str:
        return t.strftime("%Y-%m-%dT%H:00:00Z")

    def pairs_at(self, t: datetime) -> list[str]:
        return list(dict.fromkeys(d["pair"] for d in self.by_hour.get(self.key(t), [])))

    def _of(self, t: datetime, pair: str) -> list[dict]:
        return [d for d in self.by_hour.get(self.key(t), []) if d["pair"] == pair]

    def effective(self, t: datetime, pair: str) -> dict | None:
        """The decision the entry logic acts on: the first entry-type decision of the hour (an entry fires the moment it is made), else the last one."""
        ds = self._of(t, pair)
        return next((d for d in ds if d["action"] in self.ENTRY), ds[-1] if ds else None)

    def batches(self, t: datetime) -> list[dict]:
        """The hour's log lines split into analysis batches (a gap of more than 2 minutes, e.g. a restart, starts a new one): each batch is a full pass of the strategy over its pairs,
        followed by that iteration's exits and entries. [{'start': ts, 'end': ts, 'end_s': seconds after the hour, 'decisions': [...]}]"""
        ds = self.by_hour.get(self.key(t), [])
        out: list[dict] = []
        for d in sorted(ds, key=lambda d: d["ts"]):
            ts = datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if out and (ts - out[-1]["end"]).total_seconds() <= 120:
                out[-1]["decisions"].append(d)
                out[-1]["end"] = ts
            else:
                out.append({"start": ts, "end": ts, "decisions": [d]})
        for b in out:
            b["end_s"] = (b["end"] - t).total_seconds() + 2.0
        return out

    def latency(self, t: datetime) -> float | None:
        """Seconds after the full hour at which the live bot FINISHED analysing the hour's pairs (the end of the first contiguous batch of its log lines; a gap of more than 2 minutes
        starts a new batch, e.g. a restart). Freqtrade enters / exits right after the analysis, so this plus ~2 s is when the live bot acted in that hour (measured: median 2 s)."""
        ds = self.by_hour.get(self.key(t))
        if not ds:
            return None
        offs = sorted((datetime.strptime(d["ts"], "%Y-%m-%dT%H:%M:%SZ") - datetime.strptime(d["t0"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds() for d in ds)
        end = offs[0]
        for o in offs[1:]:
            if o - end > 120:
                break
            end = o
        return end + 2.0

    def final(self, t: datetime, pair: str) -> dict | None:
        ds = self._of(t, pair)
        return ds[-1] if ds else None

BOTS = {
    "main": {"key": "main", "dir": "freqtrade", "file": "DeepSeekStrategy.py", "category": "spot", "temperature": 0.1, "default": True},
    "shorts": {"key": "shorts", "dir": "freqtrade-shorts", "file": "DeepSeekShortsStrategy.py", "category": "linear", "temperature": 0.1, "default": True},
    "moonshot": {"key": "moonshot", "dir": "freqtrade-moonshot", "file": "DeepSeekMoonshotStrategy.py", "category": "spot", "temperature": 0.2, "default": True},
    "shorts_v2": {"key": "shorts_v2", "dir": "freqtrade-shorts-v2", "file": "DeepSeekShortsV2Strategy.py", "category": "linear", "temperature": 0.1, "default": False},
}
MIN_STAKE = 5.0
# Simulator mechanics. LEGACY is what P21 shipped; LIVE_LIKE is the order and timing the live Freqtrade loop really has (see the calibration report):
#   analyse every pair (decisions) -> exits (custom_exit sees the FRESH decision) -> entries in the pair-list order; entries happen `entry_delay_s` after the candle
#   open (the LLM calls of the whole hour come first); the stake follows Freqtrade's "unlimited" formula; a restart empties the strategy's in-memory state.
MECH_LEGACY = {"name": "legacy", "decide_first": False, "entry_delay_s": 0, "ft_stake": False, "restart_reset": False, "fine_prices": False, "fine_exits": False, "fine_fill": "target", "hour_latency": False, "exit_extra_s": 0.0, "limit_fill": False, "poll_exits": False}
MECH_LIVE_LIKE = {"name": "live_like", "decide_first": True, "entry_delay_s": 0, "ft_stake": True, "restart_reset": True, "fine_prices": True, "fine_exits": True, "fine_fill": "close", "hour_latency": True, "exit_extra_s": 0.0, "limit_fill": True, "poll_exits": True}
# The default of a NORMAL replay (no live log to take the timing from): the live-like order and stake rule, the loop latency the calibration measured on the real bots
# (entries ~50 s and rotation exits ~8 s after the candle open), and -- when minute candles were downloaded -- minute prices and minute-ordered ROI / stoploss checks.
MECH_REPLAY_DEFAULT = {**MECH_LIVE_LIKE, "entry_delay_s": 50.0, "exit_extra_s": 8.0, "hour_latency": False, "restart_reset": False}
MEMORY_ATTRS = ("_stale_streak", "_latest_decision", "_peak_tracker")     # a strategy's in-memory state; a container restart loses it
WARMUP_DAYS = 36            # the strategies read up to 30 daily candles (concentration check) and a 4h SMA50


def bot_config(spec: dict, base=None) -> dict:
    import json
    from pathlib import Path
    base = Path(base) if base else sandbox.default_strategies_dir()
    for p in (base / spec["key"] / "config.json", base / spec["dir"] / "user_data" / "config.json"):
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
    return {}


def roi_at(table: dict, age_minutes: float) -> float | None:
    best = None
    for k, v in sorted(((int(k), float(v)) for k, v in (table or {}).items())):
        if k <= age_minutes:
            best = v
    return best


class BotRun:
    def __init__(self, bot: str, mode: str, params: dict, store: rdata.SeriesStore, fng_hist, llm, pairs: list[str], run_id: str, log=lambda s: None,
                 progress=lambda **k: None, stop_event=None, base=None, live: LiveDecisions | None = None, carry: list[dict] | None = None,
                 max_open: int | None = None, cash0: float | None = None):
        self.bot, self.mode, self.p, self.store, self.llm, self.run_id = bot, mode, params, store, llm, run_id
        self.spec = BOTS[bot]
        self.pairs = pairs
        self.log, self.progress, self.stop_event = log, progress, stop_event
        self.config = bot_config(self.spec, base)
        self.ctx = sandbox.ReplayContext(bot, store, self.spec["category"], pairs, fng_hist, anonymize=(mode == "anonymized"))
        self.anon = sandbox.Anonymizer(pairs) if mode == "anonymized" else None
        self.fee, self.slip = float(params.get("fee_pct", 0.1)) / 100.0, float(params.get("slippage_pct", 0.05)) / 100.0
        self.trailing: dict | None = None
        self.batch: dict[str, list[dict]] | None = None     # calibration with hour_latency: the decisions of the analysis batch being replayed
        self.live = live                                  # calibration: the live bot's real decisions replace the LLM
        self.mech = {**MECH_REPLAY_DEFAULT, **(params.get("mech") or {})}
        self.max_open = int(max_open if max_open is not None else self.config.get("max_open_trades", 5))
        self.wallet0 = float(cash0 if cash0 is not None else self.config.get("dry_run_wallet", 500))
        self.ctx.cash = self.wallet0
        self.decisions: list[dict] = []
        self.attempts: list[dict] = []                    # every entry attempt and what became of it (calibration diagnostics)
        self.equity: list[tuple[str, float]] = []
        self.next_id = 1
        self.exit_counts: dict[str, int] = {}
        temp = self.spec["temperature"]

        def hook(system, user):
            if self.anon is not None:
                system, user = self.anon(system, self.ctx.clock.now), self.anon(user, self.ctx.clock.now)
            return self.llm.call(system, user, temperature=temp)

        self.mod, self.obj = sandbox.load_bot(self.spec, self.ctx, hook, self.config, base)
        self.roi_table = dict(getattr(self.obj, "minimal_roi", {}) or {})
        self.stoploss = float(getattr(self.obj, "stoploss", -0.10))
        self.can_short = bool(getattr(self.obj, "can_short", False))
        self.short_leverage = 1.0
        self.frozen = bool(params.get("frozen"))           # frozen decisions of an earlier run (exit variants): the same decision steps, no model
        self.rule = bool(params.get("rule"))               # the strategy's own no-LLM placeholder rule (its `is_backtest` branch), same exits and fees
        if self.rule:
            self.obj.dp.runmode.value = "backtest"
        if live is not None:
            self.obj._ask_deepseek = self._live_ask
        self.overrides = self.apply_overrides(params.get("exit") or {})
        for c in carry or []:                              # positions that were already open when the window starts (real open time, rate and stake)
            od = datetime.strptime(c["open_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            tr = sandbox.FakeTrade(self.next_id, c["pair"], c["side"] == "short", od, float(c["open_rate"]), float(c["stake"]), self.short_leverage, c.get("enter_tag") or "", self.fee)
            tr.carry_over, tr.live_id = True, c.get("id")
            self.next_id += 1
            self.ctx.trades_open.append(tr)

    def _live_ask(self, row, pair, *a, **k):
        if self.batch is not None:
            ds = self.batch.get(pair) or []
            d = next((x for x in ds if x["action"] in LiveDecisions.ENTRY), ds[-1] if ds else None)
            return ("hold", "no live decision") if d is None else (d["action"], d["reason"])
        d = self.live.effective(self.ctx.clock.now, pair)
        if d is None:
            return ("hold", "no live decision")
        return (d["action"], d["reason"])

    def apply_overrides(self, ov: dict) -> dict:
        """Exit-rule overrides for the exit-variant runs (section C). Only EXITS may vary, never an entry condition. Returns what was really applied (it goes into the report)."""
        applied: dict = {}
        if "rotate" in ov and ov["rotate"] is False:
            orig = self.obj.custom_exit

            def custom_exit(*a, **k):
                tag = orig(*a, **k)
                return None if tag == "rotate_stale_signal" else tag
            self.obj.custom_exit = custom_exit
            applied["rotate_stale_signal"] = "off"
        if ov.get("min_hold_hours") is not None:
            hrs = float(ov["min_hold_hours"])
            for target in (self.obj, self.mod):
                for name in ("MIN_HOLD_HOURS", "STALE_SIGNAL_MIN_HOLD_HOURS"):
                    if hasattr(target, name):
                        setattr(target, name, hrs)
            applied["min_hold_hours"] = hrs
        if ov.get("roi") is not None:
            self.roi_table = {str(int(k)): float(v) for k, v in dict(ov["roi"]).items()}
            applied["minimal_roi"] = self.roi_table
        if ov.get("stoploss") is not None:
            self.stoploss = float(ov["stoploss"])
            applied["stoploss"] = self.stoploss
        tr = ov.get("trailing") or {}
        if tr.get("enabled"):
            self.trailing = {"positive": float(tr.get("positive", 0.01)), "offset": float(tr.get("offset", 0.0)), "only_offset_is_reached": bool(tr.get("only_offset_is_reached", False))}
            applied["trailing"] = self.trailing
        return applied

    def trailing_hit(self, tr: sandbox.FakeTrade, high: float, low: float) -> float | None:
        """Freqtrade-style trailing stop on the candle that just closed: the stop follows the best rate seen by `positive` once the profit reached `offset`. The check uses the stop set by
        the candles BEFORE this one, then the peak is updated (the order inside a candle is unknown: this is the optimistic side, stated in the report)."""
        cfg = getattr(self, "trailing", None)
        if not cfg:
            return None
        peak = getattr(tr, "peak_rate", tr.open_rate)
        armed = getattr(tr, "trail_armed", not cfg["only_offset_is_reached"] and cfg["offset"] == 0)
        hit = None
        if armed:
            stop = peak * (1 + cfg["positive"]) if tr.is_short else peak * (1 - cfg["positive"])
            if (high >= stop) if tr.is_short else (low <= stop):
                hit = stop
        best = low if tr.is_short else high
        tr.peak_rate = min(peak, best) if tr.is_short else max(peak, best)
        if not armed and (tr.calc_profit_ratio(tr.peak_rate) >= cfg["offset"]):
            tr.trail_armed = True
        return hit

    def reset_memory(self) -> None:
        for name in MEMORY_ATTRS:
            v = getattr(self.obj, name, None)
            if isinstance(v, dict):
                v.clear()

    # ------------------------------------------------------------- prices
    def price_now(self, pair: str, fallback: float) -> float:
        """The price at the simulated clock: one-minute data when the mechanics ask for it and the store has it, else the hourly figure `fallback`."""
        if self.mech.get("fine_prices"):
            p = self.store.price_at(self.spec["category"], rdata.bybit_symbol(pair), self.ctx.clock.now)
            if p is not None:
                return p
        return fallback

    LIMIT_TIMEOUT_S = 600          # unfilledtimeout.entry = 10 minutes in every bot's config
    LIMIT_RETRY_S = 603            # the cancelled order is re-placed on the next loop iteration, about 3 s later (measured in the live logs)

    def entry_fill(self, pair: str, is_short: bool, now: datetime) -> tuple[datetime, float] | None:
        """The live entry is a PASSIVE limit order at the order-book top (entry_pricing price_side `same`: the bid for a long, the ask for a short). It fills when the price trades
        through that level; after 10 minutes unfilled it is cancelled and, while the candle's signal still stands (the rest of the hour), placed again at the new level. A trade is
        opened by the order that fills: its open time is that order's creation time, its open rate the limit. The bid is not public history, so it is the minute price moved by half of
        the symbol's CURRENT relative spread (thin new listings have a wide one; majors almost none). Returns (open time, rate), or None when no attempt in the hour filled."""
        cat, sym = self.spec["category"], rdata.bybit_symbol(pair)
        half = self.store.spread(cat, sym) / 2
        hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        for k in range(6):
            t_k = now + timedelta(seconds=self.LIMIT_RETRY_S * k)
            if t_k >= hour_end:
                break
            p = self.store.price_at(cat, sym, t_k)
            if p is None:
                return None
            limit = p * (1 + half) if is_short else p * (1 - half)
            first = t_k.replace(second=0, microsecond=0) + timedelta(minutes=1)          # the order is placed inside a minute: only LATER trades can fill it
            for end_t, high, low, close in self.store.minutes(cat, sym, first, t_k + timedelta(seconds=self.LIMIT_TIMEOUT_S)):
                if (high >= limit) if is_short else (low <= limit):
                    return t_k, limit
        return None

    def candle(self, pair: str, open_dt: datetime):
        df = self.store.frames.get((self.spec["category"], rdata.bybit_symbol(pair)))
        if df is None:
            return None
        row = df[df["date"] == pd.Timestamp(open_dt)]
        return None if row.empty else row.iloc[0]

    # ------------------------------------------------------------ decisions
    def decide(self, pair: str, t: datetime) -> dict | None:
        df = self.ctx.frame_asof(pair, t)
        if len(df) < (24 if self.live is not None else 60):        # calibration: the strategy's own guard (len(dataframe) < 24); the LLM prompt needs 60 for its SMA50
            return None
        ind = self.obj.populate_indicators(df.copy(), {"pair": pair})
        out = self.obj.populate_entry_trend(ind.copy(), {"pair": pair})
        last = out.iloc[-1]
        d = getattr(self.obj, "_latest_decision", {}).get(pair)
        action = d[0] if d else ("buy" if int(last.get("enter_long", 0) or 0) == 1 else "sell_short" if int(last.get("enter_short", 0) or 0) == 1 else "hold")
        if action not in ("buy", "sell_short", "hold"):
            action = "hold"
        v2 = (getattr(self.obj, "_v2_last", None) or {}).get(pair)
        return {"pair": pair, "action": action, "reason": (d[1] if d and len(d) > 1 else "")[:200], "price": float(df.iloc[-1]["close"]),
                "enter_long": int(last.get("enter_long", 0) or 0), "enter_short": int(last.get("enter_short", 0) or 0), "tag": str(last.get("enter_tag", "") or ""),
                **({"candidate": bool(v2["candidate"]), "vote": v2["vote"]} if v2 and self.mode != "rule" else {})}

    def prime_tiers(self, t: datetime) -> None:
        """Classify EVERY pair (core / satellite) before the parallel LLM phase. The strategies read each other pair's tier while writing 'recent performance', so
        without this the prompt of one pair could depend on whether another thread had already classified its pair: results would vary run to run."""
        if not hasattr(self.obj, "_classify_pair"):
            return
        for pair in self.pairs:
            df = self.ctx.frame_asof(pair, t)
            if len(df) >= 60:
                try:
                    self.obj._classify_pair(pair, self.obj.populate_indicators(df.copy(), {"pair": pair}))
                except Exception:      # noqa: BLE001
                    pass

    # --------------------------------------------------------------- trades
    def n_open(self):
        return len(self.ctx.trades_open)

    def attempt(self, t: datetime, sig: dict, result: str, **extra) -> bool:
        self.attempts.append({"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "pair": sig["pair"], "result": result, "open_trades": self.n_open(), **extra})
        return result == "opened"

    def proposed_stake(self) -> float:
        if self.mech["ft_stake"]:                          # Freqtrade's unlimited stake: (available + tied up) / max_open_trades, capped at what is available
            tied = sum(tr.stake_amount for tr in self.ctx.trades_open)
            return min((self.ctx.cash_free() + tied) / max(1, self.max_open), self.ctx.cash_free())
        return self.ctx.cash_free() / max(1, self.max_open - self.n_open())

    def open_trade(self, sig: dict, t: datetime) -> bool:
        pair = sig["pair"]
        is_short = sig["enter_short"] == 1 and sig["enter_long"] != 1
        if is_short and not self.can_short:
            return self.attempt(t, sig, "short_not_allowed")
        if any(tr.pair == pair for tr in self.ctx.trades_open):
            return self.attempt(t, sig, "pair_already_open")
        if self.n_open() >= self.max_open:
            return self.attempt(t, sig, "slots_full")
        c = self.candle(pair, t)
        if c is None:
            return self.attempt(t, sig, "no_candle")
        open_time = self.ctx.clock.now
        rate = self.price_now(pair, float(c["open"])) * (1 - self.slip if is_short else 1 + self.slip)
        if self.mech.get("limit_fill") and self.mech.get("fine_prices") and self.store.price_at(self.spec["category"], rdata.bybit_symbol(pair), open_time) is not None:
            fill = self.entry_fill(pair, is_short, open_time)
            if fill is None:
                return self.attempt(t, sig, "limit_not_filled")
            open_time, rate = fill[0], fill[1] * (1 - self.slip if is_short else 1 + self.slip)
        side = "short" if is_short else "long"
        proposed = self.proposed_stake()
        try:
            if hasattr(self.obj, "confirm_trade_entry") and not self.obj.confirm_trade_entry(pair, "limit", proposed / rate, rate, "GTC", self.ctx.clock.now, sig["tag"] or None, side):
                return self.attempt(t, sig, "confirm_veto")
            stake = float(self.obj.custom_stake_amount(pair, self.ctx.clock.now, rate, proposed, None, self.ctx.cash_free(), 1.0, sig["tag"] or None, side))
        except Exception as e:      # noqa: BLE001
            self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: entry hooks failed for {pair}: {e}")
            return self.attempt(t, sig, "hook_error", error=str(e)[:80])
        stake = min(stake, self.ctx.cash_free())
        if stake < MIN_STAKE:
            return self.attempt(t, sig, "stake_below_min", stake=round(stake, 2))
        tr = sandbox.FakeTrade(self.next_id, pair, is_short, open_time, rate, stake, self.short_leverage, sig["tag"], self.fee)
        self.next_id += 1
        self.ctx.cash -= stake
        self.ctx.trades_open.append(tr)
        self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: ENTER {side} {pair} @ {rate:.6g} stake {stake:.2f} ({sig['tag'] or '-'})")
        return self.attempt(t, sig, "opened")

    def close_trade(self, tr: sandbox.FakeTrade, t: datetime, rate: float, reason: str) -> None:
        rate = rate * (1 + self.slip if tr.is_short else 1 - self.slip)
        ratio = tr.calc_profit_ratio(rate)
        tr.is_open, tr.close_date, tr.close_rate, tr.close_profit, tr.exit_reason = False, t, rate, ratio, reason
        tr.close_profit_abs = tr.stake_amount * ratio
        tr.fees_abs = tr.stake_amount * self.fee + tr.stake_amount * (1 + ratio) * self.fee
        self.ctx.cash += tr.stake_amount + tr.close_profit_abs
        self.ctx.trades_open.remove(tr)
        self.ctx.trades_closed.append(tr)
        self.exit_counts[reason] = self.exit_counts.get(reason, 0) + 1
        try:
            self.obj.confirm_trade_exit(tr.pair, tr, "limit", tr.stake_amount / rate, rate, "GTC", reason, t)
        except Exception:      # noqa: BLE001
            pass
        self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: EXIT {tr.pair} {reason} {ratio * 100:+.2f}%")

    def fine_exit(self, tr: sandbox.FakeTrade, since: datetime, now: datetime) -> tuple[datetime, float, str] | None:
        """ROI / stoploss on ONE-MINUTE candles between the previous check and now, in time order (hourly candles cannot tell which came first inside the hour).
        Returns (time, rate, reason) of the first breach, or None. `fine_fill`: "target" = fill at the stop / ROI rate; "close" = when the minute closed beyond the target
        the fill is the minute's close (a polled market exit gaps through a fast move)."""
        cat, sym = self.spec["category"], rdata.bybit_symbol(tr.pair)
        stop_rate = tr.rate_for_profit(self.stoploss)
        for end_t, high, low, close in self.store.minutes(cat, sym, since.replace(second=0, microsecond=0), now):
            age = (end_t - tr.open_date).total_seconds() / 60
            roi = roi_at(self.roi_table, age)
            roi_rate = tr.rate_for_profit(roi) if roi is not None else None
            stop_hit = (high >= stop_rate) if tr.is_short else (low <= stop_rate)
            roi_hit = roi_rate is not None and ((low <= roi_rate) if tr.is_short else (high >= roi_rate))
            if not (stop_hit or roi_hit):
                continue
            if stop_hit:
                rate = stop_rate
                if self.mech.get("fine_fill") == "close":
                    rate = max(stop_rate, close) if tr.is_short else min(stop_rate, close)
                return end_t, rate, "stop_loss"
            rate = roi_rate
            if self.mech.get("fine_fill") == "close":
                rate = min(roi_rate, close) if tr.is_short else max(roi_rate, close)
            return end_t, rate, "roi"
        return None

    def process_exits(self, t: datetime) -> None:
        for tr in list(self.ctx.trades_open):
            if tr.open_date.replace(minute=0, second=0, microsecond=0) > t - timedelta(hours=1):       # the candle that closed at t must start after the entry hour began
                continue
            c = self.candle(tr.pair, t - timedelta(hours=1))
            if c is None:
                continue
            now = self.ctx.clock.now
            high, low, close = float(c["high"]), float(c["low"]), self.price_now(tr.pair, float(c["close"]))
            age = (now - tr.open_date).total_seconds() / 60
            fine = None
            if self.mech.get("fine_exits"):
                since = max(tr.open_date, getattr(tr, "last_eval", tr.open_date))
                fine = self.fine_exit(tr, since, now)
                tr.last_eval = now
            if fine is not None:
                self.close_trade(tr, fine[0], fine[1], fine[2])
                continue
            stop_rate = tr.rate_for_profit(self.stoploss)
            roi = roi_at(self.roi_table, age)
            roi_rate = tr.rate_for_profit(roi) if roi is not None else None
            stop_hit = (high >= stop_rate) if tr.is_short else (low <= stop_rate)
            roi_hit = roi_rate is not None and ((low <= roi_rate) if tr.is_short else (high >= roi_rate))
            if self.mech.get("fine_exits") and self.store.minutes(self.spec["category"], rdata.bybit_symbol(tr.pair), now - timedelta(hours=1), now):
                stop_hit = roi_hit = False                 # minute data covers this hour and found no breach: the hourly high/low must not invent one
            trail = self.trailing_hit(tr, high, low) if getattr(self, "trailing", None) else None
            if stop_hit:
                self.close_trade(tr, now, stop_rate, "stop_loss")
                continue
            if trail is not None:
                self.close_trade(tr, now, trail, "trailing_stop_loss")
                continue
            if roi_hit:
                self.close_trade(tr, now, roi_rate, "roi")
                continue
            try:
                tag = self.obj.custom_exit(tr.pair, tr, now, close, tr.calc_profit_ratio(close))
            except Exception as e:      # noqa: BLE001
                tag = None
                self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: custom_exit failed for {tr.pair}: {e}")
            if tag:
                self.close_trade(tr, now, close, str(tag))
                continue
            if self.mech.get("poll_exits") and self.mech.get("fine_prices"):
                self.poll_custom_exit(tr, now)

    POLL_STEP_MIN = 5

    def poll_custom_exit(self, tr: sandbox.FakeTrade, now: datetime) -> None:
        """Freqtrade calls custom_exit every few seconds, not once per candle: a time-based rule (Moonshot's 3-day maximum hold) fires the moment its time is reached. Here the rest of the hour
        is polled every few minutes with the minute price. The candle-date guards inside the strategies make these extra calls no-ops for the rotation / peak logic, which already ran at
        `now` exactly as in the live loop (after the hour's analysis)."""
        hour_end = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        cat, sym = self.spec["category"], rdata.bybit_symbol(tr.pair)
        saved = self.ctx.clock.now
        try:
            t_k = now + timedelta(minutes=self.POLL_STEP_MIN)
            while t_k < hour_end and tr in self.ctx.trades_open:
                price = self.store.price_at(cat, sym, t_k)
                if price is None:
                    break
                self.ctx.clock.now = t_k
                try:
                    tag = self.obj.custom_exit(tr.pair, tr, t_k, price, tr.calc_profit_ratio(price))
                except Exception:      # noqa: BLE001
                    tag = None
                if tag:
                    self.close_trade(tr, t_k, price, str(tag))
                    return
                t_k += timedelta(minutes=self.POLL_STEP_MIN)
        finally:
            self.ctx.clock.now = saved

    def mark_equity(self, t: datetime) -> float:
        eq = self.ctx.cash
        for tr in self.ctx.trades_open:
            c = self.candle(tr.pair, t - timedelta(hours=1))
            ratio = tr.calc_profit_ratio(float(c["close"])) if c is not None else 0.0
            eq += tr.stake_amount * (1 + ratio)
        return eq

    # ------------------------------------------------------------------ run
    def decision_phase(self, t: datetime, rng, workers: int) -> list[dict]:
        """Asks the entry logic about every pair and records the decisions. Calibration: the live bot's pairs of that hour, in its log order, one by one."""
        if self.live is not None:
            order = list(self.batch) if self.batch is not None else self.live.pairs_at(t)
            results = []
            for pr in order:
                try:
                    results.append(self.decide(pr, t))
                except Exception as e:      # noqa: BLE001
                    results.append(None)
                    self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: decide failed for {pr}: {type(e).__name__}: {e}")
                fin = (self.batch[pr][-1] if self.batch is not None else self.live.final(t, pr))
                lst = getattr(self.obj, "_latest_decision", None)
                if fin is not None and isinstance(lst, dict) and pr in lst:      # what a LATER decision of the same hour (a restart re-analyses) left for the rotation check
                    lst[pr] = (fin["action"], fin["reason"]) + tuple(lst[pr][2:])
        else:
            order = list(self.pairs)
            rng.shuffle(order)
            self.prime_tiers(t)
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(lambda pr: self.decide(pr, t), order))
        sigs = []
        for sig in results:
            if sig is None:
                continue
            rec = {"ts": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "bot": self.bot, "pair": sig["pair"], "action": sig["action"], "reason": sig["reason"],
                   "price": sig["price"], "mode": self.mode, "run_id": self.run_id, "replay": True}
            if "candidate" in sig:                                     # P22-A: the short_v2 bots log the rule candidate and the model's vote with every decision
                rec["candidate"], rec["vote"] = sig["candidate"], sig["vote"]
            self.decisions.append(rec)
            self.log(f"[{t:%Y-%m-%d %H:%M}] {self.bot}: {sig['pair']} -> {sig['action']} {sig['reason'][:70]}")
            sigs.append(sig)
        return sigs

    def enter_phase(self, sigs: list[dict], t: datetime) -> None:
        for sig in sigs:
            if sig["enter_long"] == 1 or sig["enter_short"] == 1:
                self.open_trade(sig, t)

    def run(self, start: datetime, end: datetime) -> dict:
        every = 1 if (self.live is not None and not self.frozen) else max(1, int(self.p.get("every_n", 4)))
        workers = max(1, int(self.p.get("workers", 8)))
        rng = random.Random(int(self.p.get("seed", 12345)))
        delay = timedelta(seconds=float(self.mech["entry_delay_s"]))
        hours = []
        t = start + timedelta(hours=1)
        while t <= end:
            hours.append(t)
            t += timedelta(hours=1)
        last_decision_t = start
        status = "completed"
        step = 0
        total_steps = len(hours)
        try:
            for i, t in enumerate(hours):
                if self.stop_event is not None and self.stop_event.is_set():
                    status = "stopped"
                    break
                step_delay = delay
                if self.live is not None and self.mech.get("hour_latency"):
                    lat = self.live.latency(t)
                    if lat is not None:
                        step_delay = timedelta(seconds=min(lat, 3000.0))
                self.ctx.clock.now = t + step_delay
                decision_step = (i % every == 0)
                self.ctx.df_asof = t if decision_step else last_decision_t
                if self.live is not None and self.mech["restart_reset"] and not self.mech.get("hour_latency") and LiveDecisions.key(t) in self.live.restart_hours:
                    self.reset_memory()
                if self.live is not None and self.mech.get("hour_latency") and self.mech["decide_first"]:
                    prev_end = None
                    for b in self.live.batches(t):
                        if self.mech["restart_reset"] and any((prev_end or t - timedelta(hours=1)) < r <= b["start"] for r in self.live.restarts):
                            self.reset_memory()               # the container restarted between the previous batch and this one: the strategy's memory is gone
                        prev_end = b["end"]
                        self.batch = {}
                        for d in b["decisions"]:
                            self.batch.setdefault(d["pair"], []).append(d)
                        last_decision_t = t
                        self.ctx.clock.now = t + timedelta(seconds=min(b["end_s"], 3000.0))
                        sigs = self.decision_phase(t, rng, workers)
                        t_decided = self.ctx.clock.now
                        self.ctx.clock.now = t_decided + timedelta(seconds=float(self.mech.get("exit_extra_s") or 0.0))      # custom_exit runs a little after the analysis ends
                        self.process_exits(t)
                        self.ctx.clock.now = t_decided                                                                       # the entry is placed right after the analysis (measured: +2 s)
                        self.enter_phase(sigs, t)
                    self.batch = None
                elif self.mech["decide_first"]:
                    sigs = []
                    if decision_step:
                        last_decision_t = t
                        sigs = self.decision_phase(t, rng, workers)
                    self.ctx.clock.now = t + step_delay + timedelta(seconds=float(self.mech.get("exit_extra_s") or 0.0))      # custom_exit runs a little after the analysis ends, entries after the exits
                    self.process_exits(t)
                    self.ctx.clock.now = t + step_delay
                    self.enter_phase(sigs, t)
                else:
                    self.process_exits(t)
                    if decision_step:
                        last_decision_t = t
                        self.enter_phase(self.decision_phase(t, rng, workers), t)
                self.equity.append((t.strftime("%Y-%m-%dT%H:%M:%SZ"), self.mark_equity(t)))
                step = i + 1
                self.progress(bot=self.bot, mode=self.mode, step=step, total=total_steps, decisions=len(self.decisions), trades=len(self.ctx.trades_closed))
        except BudgetExceeded as e:
            status = "stopped_budget"
            self.log(f"{self.bot}/{self.mode}: STOPPED by budget: {e}")
        except Stopped:
            status = "stopped"
        return {"bot": self.bot, "mode": self.mode, "status": status, "steps_done": step, "steps_total": total_steps, "decisions": self.decisions,
                "trades": [self.trade_row(tr) for tr in self.ctx.trades_closed], "open_trades": [self.trade_row(tr) for tr in self.ctx.trades_open],
                "open_at_end": len(self.ctx.trades_open), "equity": self.equity,
                "wallet0": self.wallet0, "max_open_trades": self.max_open, "exit_counts": self.exit_counts, "blocked_urls": list(dict.fromkeys(self.ctx.blocked))[:20],
                "notes": sorted(self.ctx.notes), "attempts": self.attempts, "mechanics": dict(self.mech),
                "overrides": self.overrides, "constants": {"minimal_roi": self.roi_table, "stoploss": self.stoploss, "trailing": self.trailing, "fee_pct": self.fee * 100, "slippage_pct": self.slip * 100, "every_n": every}}

    @staticmethod
    def trade_row(tr: sandbox.FakeTrade) -> dict:
        def f(d):
            return None if d is None else d.strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"id": tr.id, "pair": tr.pair, "side": "short" if tr.is_short else "long", "open_date": f(tr.open_date), "close_date": f(tr.close_date), "open_rate": tr.open_rate,
                "close_rate": tr.close_rate, "stake": round(tr.stake_amount, 4), "profit_ratio": tr.close_profit,
                "profit_abs": None if tr.close_profit_abs is None else round(tr.close_profit_abs, 4), "fees_abs": round(tr.fees_abs, 4), "exit_reason": tr.exit_reason,
                "enter_tag": tr.enter_tag, "carry_over": bool(getattr(tr, "carry_over", False)), "live_id": getattr(tr, "live_id", None)}
