"""
STUB — order placement not implemented. See AGENTS.md invariant #3:
DRY_RUN defaults to true and this file must never flip that default.

Once implemented, this should call Bybit's authenticated order
endpoint only when os.getenv("DRY_RUN") == "false", and only for a
trade that already passed risk_guard.check_trade().
"""
import logging
import os

from .risk_guard import ProposedTrade

log = logging.getLogger("survival-agent.executor")


def execute(trade: ProposedTrade) -> dict:
    dry_run = os.getenv("DRY_RUN", "true").lower() != "false"
    if dry_run:
        log.info("[DRY_RUN] would place trade: %s", trade)
        return {"executed": False, "dry_run": True, "trade": trade.__dict__}

    log.error("Live trading requested but executor.py has no real Bybit integration yet")
    return {"executed": False, "dry_run": False, "error": "executor not implemented"}
