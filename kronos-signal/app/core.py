"""
kronos-signal -- pure logic (no torch, no network, no clock of its own) so it can be tested offline.

Once an hour, right after the 1h candle closes, forecast the next 24 candles of the most liquid pairs with the Kronos foundation model and write one
SIGNAL per pair into an append-only JSONL file in the survival-agent ledger format (bot = "kronos"). It never trades and nothing reads it for control:
the P19 audit scores it against the realised forward return like any other signal source.

Look-ahead rules (identical to the P19 ledger):
  * t0 = the hour boundary the run belongs to; the model sees ONLY candles whose open time is <= t0 - 1h (i.e. closed by t0). A candle opened at t0 or
    later is dropped even if the bot's REST already returns it (it is still forming).
  * the decision price is the close of the candle opened at t0 - 1h.
  * the last closed candle must be exactly the one opened at t0 - 1h, otherwise the pair is skipped ("stale_candles") -- never forecast from old data.
Action: net = expected 4h return minus the round-trip fee; buy when net_long > threshold, sell_short when net_short > threshold, else hold.
confidence = |expected 4h return|. The raw expected returns at 1/4/24h are stored in the record (field `kronos`).
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

LOOKBACK = 480                    # candles fed to the model (model context 512; the spec asks for >= 400)
MIN_ROWS = 400
PRED_LEN = 24
HORIZONS = (1, 4, 24)
SEED = 12345
NEWEST_FIRST_GRACE = 0
RANK_FILE = "kronos_rank.json"

SPOT_BOT_ORDER = ["main", "moonshot"]                       # whose whitelist (and candles) represent a spot pair
FUTURES_BOT_ORDER = ["shorts", "listing_fade", "trend_v2"]  # ... and a futures pair
BOT_URLS = {
    "main": "http://freqtrade:8080", "shorts": "http://freqtrade-shorts:8080", "moonshot": "http://freqtrade-moonshot:8080",
    "listing_fade": "http://freqtrade-listing-fade:8080", "trend_v2": "http://freqtrade-trend-v2:8080",
}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def floor_hour(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def config_from_env(env: dict | None = None) -> dict:
    env = os.environ if env is None else env

    def f(key, default):
        try:
            return float(env.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    return {
        "mode": str(env.get("KRONOS_MODE", "on")).strip().lower(),
        "max_pairs": int(f("KRONOS_MAX_PAIRS", 45)),
        "threshold": f("KRONOS_THRESHOLD", 0.005),           # 0.5% AFTER the round-trip fee
        "fee_round_trip": f("KRONOS_FEE_ROUND_TRIP", 0.002),  # 2 x 0.1% taker
        "samples": int(f("KRONOS_SAMPLES", 3)),
        "cycle_budget_s": f("KRONOS_CYCLE_BUDGET_S", 900),
        "rank_min_n": int(f("KRONOS_RANK_MIN_N", 8)),            # a bot needs this many scored pairs for a cross-sectional rank to mean anything
        "rank_min_score": f("KRONOS_RANK_MIN_SCORE", 0.003),     # a pair must also be at least 0.3% above / below the bot's average forecast (fixed, not tuned)
        "skip_bots": [b.strip() for b in str(env.get("KRONOS_SKIP_BOTS", "")).split(",") if b.strip()],
        "out_dir": Path(env.get("KRONOS_OUT_DIR", "/kronos")),
        "run_minute": int(f("KRONOS_RUN_MINUTE", 2)),
    }


# ------------------------------------------------------------------ candles
def parse_candles(payload) -> list[dict]:
    """Freqtrade /pair_candles -> rows [{open_dt, open, high, low, close, volume}] sorted by time, bad rows dropped."""
    if not isinstance(payload, dict):
        return []
    cols, data = payload.get("columns"), payload.get("data")
    if not isinstance(cols, list) or not isinstance(data, list) or "date" not in cols:
        return []
    ix = {c: i for i, c in enumerate(cols)}
    need = ("open", "high", "low", "close", "volume")
    if not all(c in ix for c in need):
        return []
    rows = []
    for r in data:
        try:
            d = r[ix["date"]]
            dt = datetime.fromtimestamp(d / 1000, tz=timezone.utc) if isinstance(d, (int, float)) else parse_iso(d)
            vals = [float(r[ix[c]]) for c in need]
        except (TypeError, ValueError, IndexError):
            continue
        if dt is None or any(v != v for v in vals):         # NaN check
            continue
        rows.append({"open_dt": dt, **dict(zip(need, vals))})
    rows.sort(key=lambda x: x["open_dt"])
    return rows


def closed_rows(rows: list[dict], t0: datetime) -> list[dict]:
    """Only candles closed by t0, i.e. opened at or before t0 - 1h. Anything newer (the forming candle) is dropped."""
    cutoff = t0 - timedelta(hours=1)
    return [r for r in rows if r["open_dt"] <= cutoff]


def usable_window(rows: list[dict], t0: datetime) -> tuple[list[dict] | None, str | None]:
    """The model input, or (None, reason). The newest closed candle must be the one opened at t0 - 1h and the window must be gap-free."""
    cl = closed_rows(rows, t0)
    if len(cl) < MIN_ROWS:
        return None, f"only {len(cl)} closed candles (< {MIN_ROWS})"
    win = cl[-LOOKBACK:]
    if win[-1]["open_dt"] != t0 - timedelta(hours=1):
        return None, f"stale_candles: newest closed candle opened {iso(win[-1]['open_dt'])}, expected {iso(t0 - timedelta(hours=1))}"
    for a, b in zip(win, win[1:]):
        if b["open_dt"] - a["open_dt"] != timedelta(hours=1):
            return None, f"gap in candles after {iso(a['open_dt'])}"
    return win, None


# ----------------------------------------------------------------- universe
def coin_of(pair: str) -> str:
    return pair.split("/")[0].upper()


def pick_sources(whitelists: dict[str, list[str]], skip_bots: list[str]) -> dict[str, str]:
    """pair -> bot whose REST serves its candles. One pair per coin: the spot pair (first spot bot that lists it) wins; a coin that only exists
    on the futures bots uses the futures pair. Skipped (retired) bots are never used."""
    chosen: dict[str, tuple[str, str]] = {}
    for bots, spot in ((SPOT_BOT_ORDER, True), (FUTURES_BOT_ORDER, False)):
        for bot in bots:
            if bot in skip_bots:
                continue
            for pair in whitelists.get(bot, []):
                coin = coin_of(pair)
                if coin not in chosen:
                    chosen[coin] = (pair, bot)
    return {pair: bot for pair, bot in chosen.values()}


def turnover_24h(rows: list[dict], t0: datetime) -> float:
    cl = closed_rows(rows, t0)[-24:]
    return sum(r["close"] * r["volume"] for r in cl)


def rank_universe(turnover: dict[str, float], n: int) -> list[str]:
    """The n most liquid pairs by 24h quote turnover (pairs with no traded volume are never ranked)."""
    live = {p: v for p, v in turnover.items() if v == v and v > 0}
    return [p for p, _ in sorted(live.items(), key=lambda kv: (-kv[1], kv[0]))][:n]


# ----------------------------------------------------------------- forecast
def expected_returns(pred_closes: list[float], last_close: float) -> dict[str, float]:
    """Expected return at 1/4/24h from the forecast closes (index h-1 is the candle h hours after t0)."""
    out = {}
    for h in HORIZONS:
        if len(pred_closes) < h or last_close <= 0:
            raise ValueError("forecast too short or invalid last close")
        out[f"ret_{h}h"] = pred_closes[h - 1] / last_close - 1.0
        if out[f"ret_{h}h"] != out[f"ret_{h}h"] or abs(out[f"ret_{h}h"]) == float("inf"):
            raise ValueError("non-finite forecast")
    return out


def decide(ret_4h: float, threshold: float, fee_round_trip: float) -> str:
    """buy / sell_short when the expected 4h move beats the threshold AFTER the round-trip fee, else hold."""
    if ret_4h - fee_round_trip > threshold:
        return "buy"
    if -ret_4h - fee_round_trip > threshold:
        return "sell_short"
    return "hold"


def make_record(pair: str, src_bot: str, t0: datetime, now: datetime, last_close: float, rets: dict, cfg: dict, meta: dict) -> dict:
    ret4 = rets["ret_4h"]
    return {
        "sid": f"kronos|{pair}|{iso(t0)}", "ts": iso(now), "t0": iso(t0), "bot": "kronos", "pair": pair,
        "action": decide(ret4, cfg["threshold"], cfg["fee_round_trip"]), "confidence": round(abs(ret4), 6), "price": last_close,
        "source": "kronos-signal", "src_bot": src_bot,
        "kronos": {**{k: round(v, 6) for k, v in rets.items()}, "threshold": cfg["threshold"], "fee_round_trip": cfg["fee_round_trip"],
                   "samples": cfg["samples"], "seed": SEED, "lookback": LOOKBACK, "pred_len": PRED_LEN, **meta},
    }


# ------------------------------------------------------------ cross-sectional view per bot (P23)
RANK_DESIGN = ("cross-sectional relative strength of the 24h forecast, as in the Kronos docs' top-K demo: score = forecast ret_24h minus the mean forecast of the bot's own scored pairs; "
               "the top third (score >= min_score) is `buy`, the bottom third (score <= -min_score) is `sell_short`, the middle and any pair below min_score `hold`; fewer than min_n scored pairs -> all hold")


def rank_bot_view(bot_pairs: list[str], by_coin: dict[str, dict], min_n: int, min_score: float) -> dict[str, dict]:
    """bot_pairs: the bot's whitelist. by_coin: coin -> the Kronos record scored for that coin (spot or futures series of the same coin).
    Returns pair -> {score, rank, n, action, ret_1h, ret_4h, ret_24h, reason}. Pairs the model did not score are absent (the bot holds)."""
    scored = [(p, by_coin[coin_of(p)]) for p in bot_pairs if coin_of(p) in by_coin]
    n = len(scored)
    if n == 0:
        return {}
    rets = [r["kronos"]["ret_24h"] for _, r in scored]
    mean = sum(rets) / n
    order = sorted(range(n), key=lambda i: (-(rets[i] - mean), scored[i][0]))
    k = max(1, math.ceil(n / 3))
    out: dict[str, dict] = {}
    for rank0, i in enumerate(order):
        pair, rec = scored[i]
        score = rets[i] - mean
        if n < min_n:
            action, why = "hold", f"only {n} scored pairs (< {min_n}): no cross-sectional rank"
        elif rank0 < k and score >= min_score:
            action, why = "buy", None
        elif rank0 >= n - k and score <= -min_score:
            action, why = "sell_short", None
        else:
            action, why = "hold", None
        out[pair] = {"score": round(score, 6), "rank": rank0 + 1, "n": n, "action": action, "ret_1h": rec["kronos"]["ret_1h"], "ret_4h": rec["kronos"]["ret_4h"], "ret_24h": rec["kronos"]["ret_24h"],
                     "reason": why or f"24h relative strength {score:+.2%}, rank {rank0 + 1} of {n}"}
    return out


def build_rank_file(t0: datetime, now: datetime, whitelists: dict[str, list[str]], records: list[dict], cfg: dict) -> dict:
    by_coin = {coin_of(r["pair"]): r for r in records}
    return {"t0": iso(t0), "written_at": iso(now), "design": RANK_DESIGN, "min_n": cfg["rank_min_n"], "min_score": cfg["rank_min_score"], "n_scored": len(records),
            "bots": {bot: rank_bot_view(wl, by_coin, cfg["rank_min_n"], cfg["rank_min_score"]) for bot, wl in whitelists.items()}}


def write_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------- output
def read_sids(path: Path) -> set[str]:
    sids: set[str] = set()
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                sids.add(json.loads(line)["sid"])
            except (ValueError, KeyError):
                continue
    return sids


def hour_done(path: Path, t0: datetime) -> bool:
    """True when a signal for this t0 is already in the file (the container restarted inside the hour)."""
    if not path.is_file():
        return False
    want = iso(t0)
    for line in path.read_text(encoding="utf-8").splitlines()[-400:]:
        try:
            if json.loads(line)["t0"] == want:
                return True
        except (ValueError, KeyError):
            continue
    return False


def append_records(path: Path, records: list[dict]) -> int:
    """Append-only; a (pair, t0) that is already there is never written twice (a restart inside the same hour is harmless)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = read_sids(path)
    new = [r for r in records if r["sid"] not in seen]
    if new:
        with open(path, "a", encoding="utf-8") as f:
            for r in new:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return len(new)


# -------------------------------------------------------------------- cycle
def candles_path(pair: str) -> str:
    return f"/api/v1/pair_candles?pair={quote(pair, safe='')}&timeframe=1h&limit=600"


def run_cycle(now: datetime, fetch_json, predict, cfg: dict, out_path: Path, clock=time.monotonic) -> dict:
    """fetch_json(bot, path) -> payload|None ; predict(window_rows, t0) -> list of 24 forecast closes (the model; injected so tests can fake it).
    Never raises for a single pair: failures are recorded in `skipped`. Stops starting new pairs once the time budget is used up."""
    started = clock()
    t0 = floor_hour(now)
    summary: dict = {"t0": iso(t0), "started_at": iso(now), "pairs_scored": 0, "records_written": 0, "skipped": {}, "truncated": False}
    whitelists: dict[str, list[str]] = {}
    for bot in SPOT_BOT_ORDER + FUTURES_BOT_ORDER:
        if bot in cfg["skip_bots"]:
            continue
        wl = fetch_json(bot, "/api/v1/whitelist")
        whitelists[bot] = list((wl or {}).get("whitelist", [])) if isinstance(wl, dict) else []
    sources = pick_sources(whitelists, cfg["skip_bots"])
    summary["candidates"] = len(sources)
    rows_by_pair: dict[str, list[dict]] = {}
    turnover: dict[str, float] = {}
    for pair, bot in sources.items():
        rows = parse_candles(fetch_json(bot, candles_path(pair)))
        if not rows:
            summary["skipped"][pair] = "no candles from the bot's REST"
            continue
        rows_by_pair[pair] = rows
        turnover[pair] = turnover_24h(rows, t0)
    ranked = rank_universe(turnover, cfg["max_pairs"])
    summary["universe"] = ranked
    records = []
    for pair in ranked:
        if clock() - started > cfg["cycle_budget_s"]:
            summary["truncated"] = True
            for rest in ranked[ranked.index(pair):]:
                summary["skipped"][rest] = "cycle time budget exhausted"
            break
        win, why = usable_window(rows_by_pair[pair], t0)
        if win is None:
            summary["skipped"][pair] = why
            continue
        try:
            closes = predict(win, t0)
            rets = expected_returns(list(closes), win[-1]["close"])
        except Exception as e:      # noqa: BLE001
            summary["skipped"][pair] = f"forecast failed: {type(e).__name__}: {e}"
            continue
        records.append(make_record(pair, sources[pair], t0, now, win[-1]["close"], rets, cfg, {"n_candles": len(win)}))
    summary["pairs_scored"] = len(records)
    summary["records_written"] = append_records(out_path, records)
    if records:                                      # the per-bot cross-sectional view that the bots read (published to them by survival-agent)
        rank = build_rank_file(t0, now, whitelists, records, cfg)
        write_json_atomic(out_path.parent / RANK_FILE, rank)
        summary["rank_actions"] = {b: {a: sum(1 for v in view.values() if v["action"] == a) for a in ("buy", "sell_short", "hold")} for b, view in rank["bots"].items() if view}
    summary["actions"] = {a: sum(1 for r in records if r["action"] == a) for a in ("buy", "sell_short", "hold")}
    summary["cycle_seconds"] = round(clock() - started, 1)
    summary["finished_at"] = iso(datetime.now(timezone.utc))
    return summary


def seconds_until(now: datetime, minute: int) -> float:
    nxt = now.replace(minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(hours=1)
    return (nxt - now).total_seconds()
