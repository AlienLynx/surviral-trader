"""
P19 -- pure statistics for "does this bot have an edge?" (stdlib only).

Used by the nightly signal audit (backend/engine/signal_audit.py) and by the portfolio brain
(evidence score, cost gate, live-money criteria). Nothing here trades, pauses or blocks
anything: it produces numbers and labels for a human.
"""
from __future__ import annotations

import math
import random
from datetime import datetime

BOOTSTRAP_B = 2000
BOOTSTRAP_SEED = 12345
MIN_TRADES_FOR_STATUS = 30
LIVE_MIN_TRADES = 100
LIVE_MIN_WEEKS = 8
COST_FLAG_SHARE = 0.5      # fees > 50% of |gross P&L|


def bootstrap_ci_mean(values: list[float], b: int = BOOTSTRAP_B, seed: int = BOOTSTRAP_SEED,
                      alpha: float = 0.05) -> dict | None:
    """Percentile bootstrap CI of the mean. Deterministic (fixed seed). None if n < 2."""
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(b):
        means.append(sum(rng.choices(values, k=n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * b)]
    hi = means[min(int((1 - alpha / 2) * b), b - 1)]
    return {"mean": sum(values) / n, "lo": lo, "hi": hi, "n": n}


def _avg_ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Rank correlation with average ranks for ties. None if n < 3 or either side is constant."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx, ry = _avg_ranks(xs), _avg_ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    sy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry)) / (sx * sy)


# ------------------------------------------------------------------ evidence
def evidence_status(n_trades: int, ci: dict | None, beats_control: bool | None,
                    beats_btc: bool | None, is_control: bool = False) -> dict:
    """One status per bot, from CLOSED trades after fees:
        no_data   n < 30
        no_edge   upper bound of the bootstrap CI of the mean trade < 0
        unproven  CI spans 0 (or is positive but the bot does not beat BOTH its control and BTC hold)
        edge      lower bound > 0 AND beats its control analogue AND BTC buy-and-hold
    Labels only -- nothing is blocked or paused by this."""
    if n_trades < MIN_TRADES_FOR_STATUS or ci is None:
        return {"status": "no_data", "reason": f"only {n_trades} closed trades (need {MIN_TRADES_FOR_STATUS})"}
    if ci["hi"] < 0:
        return {"status": "no_edge", "reason": f"mean trade after fees {ci['mean'] * 100:.3f}%, 95% CI upper bound "
                                               f"{ci['hi'] * 100:.3f}% is below 0"}
    if ci["lo"] <= 0:
        return {"status": "unproven", "reason": f"95% CI of the mean trade [{ci['lo'] * 100:.3f}%, {ci['hi'] * 100:.3f}%] spans 0"}
    if is_control:
        return {"status": "unproven", "reason": "control bot: CI above 0 but a control has no reference to beat"}
    if beats_control and beats_btc:
        return {"status": "edge", "reason": f"CI [{ci['lo'] * 100:.3f}%, {ci['hi'] * 100:.3f}%] above 0 and beats control and BTC hold"}
    missing = []
    if not beats_control:
        missing.append("its control" if beats_control is False else "control comparison unavailable")
    if not beats_btc:
        missing.append("BTC hold" if beats_btc is False else "BTC comparison unavailable")
    return {"status": "unproven", "reason": "CI above 0 but does not beat " + " and ".join(missing)}


def live_criteria(status: str, n_closed: int, weeks_span: float | None, net_after_fees: float,
                  beats_control_long: bool | None, beats_btc: bool | None) -> dict:
    """P19-D: a bot may only be considered for real money if ALL hold. Report only."""
    checks = {
        "status is edge": status == "edge",
        f">= {LIVE_MIN_TRADES} closed trades": n_closed >= LIVE_MIN_TRADES,
        f">= {LIVE_MIN_WEEKS} weeks of history": weeks_span is not None and weeks_span >= LIVE_MIN_WEEKS,
        "positive after fees": net_after_fees > 0,
        "beats control_long": beats_control_long is True,
        "beats BTC buy-and-hold": beats_btc is True,
    }
    return {"eligible": all(checks.values()), "checks": checks,
            "failed": [k for k, v in checks.items() if not v]}


# ---------------------------------------------------------------- cost gate
def cost_gate(closed_trades: list[dict]) -> dict:
    """Commissions vs gross P&L and vs the typical move, over a bot's closed trades.
    `close_profit_abs` from Freqtrade is NET of fees, so gross = net + fees."""
    rows = [t for t in closed_trades if not t.get("is_open")]
    n = len(rows)
    if n == 0:
        return {"n": 0, "flag": False, "note": "no closed trades"}
    fees = turnover = net = funding = 0.0
    moves, fee_pcts = [], []
    for t in rows:
        stake = abs(float(t.get("stake_amount") or 0.0)) * float(t.get("leverage") or 1.0)
        fee = float(t.get("fee_open_cost") or 0.0) + float(t.get("fee_close_cost") or 0.0)
        fees += fee
        net += float(t.get("close_profit_abs") or 0.0)
        funding += float(t.get("funding_fees") or 0.0)
        o, c = t.get("open_rate"), t.get("close_rate")
        if stake > 0 and o and c:
            turnover += stake + stake * (float(c) / float(o))
            moves.append(abs(float(c) / float(o) - 1.0) * 100)
            fee_pcts.append(fee / stake * 100)
        elif stake > 0:
            turnover += 2 * stake
    gross = net + fees
    share = (fees / abs(gross)) if abs(gross) > 1e-9 else None
    avg_move = sum(moves) / len(moves) if moves else None
    avg_fee = sum(fee_pcts) / len(fee_pcts) if fee_pcts else None
    flag = (fees > COST_FLAG_SHARE * abs(gross)) if share is not None else fees > 0
    return {
        "n": n, "fees_usd": round(fees, 3), "turnover_usd": round(turnover, 2),
        "fees_pct_of_turnover": round(fees / turnover * 100, 4) if turnover else None,
        "net_pnl_usd": round(net, 3), "gross_pnl_usd": round(gross, 3),
        "fees_share_of_gross": None if share is None else round(share, 3),
        "avg_move_to_exit_pct": None if avg_move is None else round(avg_move, 3),
        "avg_round_trip_fee_pct": None if avg_fee is None else round(avg_fee, 4),
        "move_to_fee_ratio": round(avg_move / avg_fee, 2) if avg_move is not None and avg_fee else None,
        "funding_fees_usd": round(funding, 3),
        "flag": bool(flag),
        "flag_rule": f"fees > {int(COST_FLAG_SHARE * 100)}% of |gross P&L|",
    }


def weeks_between(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return max((b - a).total_seconds(), 0.0) / (7 * 86400)
