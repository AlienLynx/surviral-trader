"""
Hard risk limits. Stdlib only — see AGENTS.md invariant #2: this module
is the last checkpoint before any trade executes, and it must keep
working even if everything else (LLM calls, market data) is degraded.

Nothing here is configurable by the LLM. It reads numbers from
data/config.json (loaded by the caller and passed in), never from a
prompt or model output.
"""
from dataclasses import dataclass


@dataclass
class ProposedTrade:
    symbol: str
    side: str  # "buy" or "sell"
    size_usd: float
    stop_loss_pct: float


@dataclass
class RiskCheckResult:
    approved: bool
    reason: str


def check_trade(
    trade: ProposedTrade,
    capital_usd: float,
    open_positions: int,
    daily_pnl_pct: float,
    limits: dict,
) -> RiskCheckResult:
    """
    Returns approved=False with a reason the moment any hard limit is
    crossed. Every rejection reason should be specific enough to log
    and act on — never a bare "no".

    max_concurrent_positions/max_position_pct_of_capital/stop_loss_pct/
    max_daily_loss_pct used to also live in app.py's FinanceConfig
    model and the :3446 Thresholds UI -- removed from both once three
    real bots existed with genuinely different, actually-enforced risk
    parameters in their own config.json/strategy files, which made one
    shared "General" number here actively misleading. The .get(...,
    default) fallbacks below are unchanged and still correct for this
    (dormant, not invoked by default) experimental engine -- absence
    from `limits` behaves identically to before.
    """
    max_positions = int(limits.get("max_concurrent_positions", 1))
    if open_positions >= max_positions:
        return RiskCheckResult(False, f"already at max_concurrent_positions ({max_positions})")

    max_pos_pct = float(limits.get("max_position_pct_of_capital", 20))
    if capital_usd <= 0:
        return RiskCheckResult(False, "capital_usd is zero or negative")
    position_pct = (trade.size_usd / capital_usd) * 100
    if position_pct > max_pos_pct:
        return RiskCheckResult(
            False,
            f"position size {position_pct:.1f}% of capital exceeds max_position_pct_of_capital ({max_pos_pct}%)",
        )

    min_stop = float(limits.get("stop_loss_pct", 5))
    if trade.stop_loss_pct < min_stop:
        return RiskCheckResult(
            False,
            f"stop_loss_pct {trade.stop_loss_pct}% is looser than the required minimum ({min_stop}%)",
        )

    max_daily_loss = float(limits.get("max_daily_loss_pct", 10))
    if daily_pnl_pct <= -abs(max_daily_loss):
        return RiskCheckResult(
            False,
            f"daily loss already at {daily_pnl_pct:.1f}%, at/past max_daily_loss_pct ({max_daily_loss}%) — no new trades today",
        )

    return RiskCheckResult(True, "within limits")
