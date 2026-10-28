"""
Turns raw profit into the take/reinvest/risk-eligible breakdown, using
the same math as the threshold simulator you used to pick these
numbers in the first place. Pure function, no side effects -- used by
paper_ledger.py for reporting and by loop.run_cycle() for the real
payout accounting.
"""


def compute_split(profit_since_last_payout: float, months_elapsed: int, config: dict) -> dict:
    """
    profit_since_last_payout: raw gain since the last confirmed payout
        (or since plan start if none yet -- see payout_state.py),
        before subtracting survival cost.
    months_elapsed: how many full ~30-day months have passed since that
        same baseline (payout_state.py's months_since()). Used for two
        things: gating take_pct against ramp_months (unchanged), AND
        scaling survival_cost below.

    survival_monthly is a real recurring monthly bill, not a one-time
    barrier -- subtracting it only once regardless of how long
    months_elapsed actually is understated the true cumulative cost
    whenever a payout wasn't confirmed for more than one month. Fixed
    here: survival_cost = survival_monthly * months_elapsed, so a
    3-month-old baseline owes 3 months of survival cost, not 1.
    """
    survival_monthly = float(config.get("survival_monthly", 0))
    survival = survival_monthly * max(1, int(months_elapsed))
    ramp = int(config.get("ramp_months", 0))
    take_pct = float(config.get("take_pct", 0)) / 100
    risk_pct = float(config.get("risk_pct", 0)) / 100

    profit_after_survival = max(0.0, profit_since_last_payout - survival)

    take_pct_effective = 0.0 if months_elapsed <= ramp else take_pct
    payout = profit_after_survival * take_pct_effective
    reinvested = profit_after_survival - payout
    risk_eligible = max(0.0, reinvested) * risk_pct

    return {
        "gross_profit": round(profit_since_last_payout, 2),
        "survival_cost": round(survival, 2),
        "profit_after_survival": round(profit_after_survival, 2),
        "take_pct_effective": take_pct_effective,
        "payout": round(payout, 2),
        "reinvested": round(reinvested, 2),
        "risk_eligible": round(risk_eligible, 2),
    }
