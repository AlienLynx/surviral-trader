# Funding carry study carry-real-20261012-153803

**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small real-money test, not proof.**

## Verdict: `insufficient n`

Not met: episodes 27 < 60

Window 2024-01-01 .. 2026-10-11, locked period before 2025-07-01, out-of-sample after it. Universe: {'pool': 251, 'delisted': 179}

Funding now (3-day mean, annualised %): {'BTCUSDT': 1.69, 'ETHUSDT': -0.37}

## Scenarios

| scenario | period | episodes | mean episode % | CI95 % | worst % | net $ | APR sleeve % | APR deployed % | max DD % sleeve | deployed % |
|---|---|---|---|---|---|---|---|---|---|---|
| spot_alts_slip0.3 | all | 52 | 1.777 | [0.721, 3.0889] | -0.5968 | 571.48 | 6.857 | 11.623 | 0.955 | 59.0 |
| spot_alts_slip0.3 | locked | 25 | 3.0154 | [1.293, 5.2487] | -0.4499 | 532.11 | 11.836 | 20.062 | 0.305 | 59.0 |
| spot_alts_slip0.3 | oos | 27 | 0.6304 | [-0.0906, 1.8465] | -0.5968 | 40.28 | 1.05 | 1.779 | 0.955 | 59.0 |
| spot_alts_slip0.6 | all | 49 | 1.7632 | [0.7202, 3.0247] | -0.4525 | 538.98 | 6.467 | 11.578 | 0.948 | 55.9 |
| spot_alts_slip0.6 | locked | 25 | 2.9043 | [1.2404, 5.0744] | -0.4499 | 519.04 | 11.545 | 20.668 | 0.455 | 55.9 |
| spot_alts_slip0.6 | oos | 24 | 0.5745 | [-0.1759, 1.9151] | -0.4525 | 20.86 | 0.543 | 0.973 | 0.948 | 55.9 |
| majors_only | all | 41 | 0.4665 | [0.0061, 1.0638] | -0.4525 | 118.44 | 1.421 | 4.75 | 0.988 | 29.9 |
| majors_only | locked | 19 | 1.0667 | [0.1019, 2.2102] | -0.4499 | 124.07 | 2.76 | 9.223 | 0.373 | 29.9 |
| majors_only | oos | 22 | -0.052 | [-0.2321, 0.1921] | -0.4525 | -4.4 | -0.115 | -0.383 | 0.988 | 29.9 |
| no_spot_constraint_NOT_REALISABLE | all | 69 | 2.909 | [1.3699, 4.8159] | -12.0567 | 1232.19 | 14.785 | 17.763 | 3.455 | 83.2 |
| no_spot_constraint_NOT_REALISABLE | locked | 27 | 2.8626 | [1.3926, 4.5461] | -0.4499 | 741.35 | 16.489 | 19.811 | 1.589 | 83.2 |
| no_spot_constraint_NOT_REALISABLE | oos | 42 | 2.9388 | [0.443, 6.1085] | -12.0567 | 489.95 | 12.765 | 15.336 | 3.455 | 83.2 |

## Rules

```
{
 "entry": "trailing 3d funding (annualised) >= 2.0 x round-trip cost x 365 / expected hold (majors 60 d, alts 14 d)",
 "exit": "trailing 3d funding < 0, or delivery (delisting)",
 "fees_pct": {
  "spot_per_side": 0.1,
  "perp_per_side": 0.055
 },
 "slippage_round_trip_pct": {
  "majors": 0.1,
  "alts": 0.3
 },
 "max_positions": 5,
 "sleeve_usd": 3000.0,
 "universe": "BTC, ETH + top 30 by trailing 30d turnover as of each day, among perpetuals that have a Bybit spot market today"
}
```

## Pass thresholds (fixed before the run)

```
{
 "min_episodes": 60,
 "min_apr_deployed_pct": 5.0,
 "max_dd_pct_of_sleeve": 3.0,
 "min_worst_episode_pct": -1.5,
 "min_edge_over_majors_pp": 1.0
}
```

## Limits

- The spot leg is required: only perpetuals with a Bybit spot market TODAY are candidates (the list of past spot markets is not public). That drops delisted symbols, so the delisting tail is under-represented here; the carry on coins without spot (the biggest earners of the unconstrained run) is impossible and excluded.
- Daily bars: intraday basis spikes and liquidation risk are not modelled; no collateral haircuts or margin calls.
- Funding is taken from the history at each symbol's own settlements; the entry fills at the day's open (a passive order may not fill).
- Exchange, custody and legal risk are not numbers here (Bybit lost 1.46B in Feb 2025; access from Belarus has been cut by the regulator before).
- A positive result is a hypothesis for a small real-money test, never `edge` by itself.
