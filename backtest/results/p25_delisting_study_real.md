# Delisting short study delist-real-20261012-185613

**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small real-money test, not proof.**

## Verdict: `unproven`

Not met: mean net hedged 0.238% < 1.0%; bootstrap CI of the mean does not exclude 0; percentile vs random shorts 93.2 < 95.0; worst event -22.152% < -8.0%; a half of the period is not positive; top 5 events are 496% of the total (> 50%)

Events: {'notices': 250, 'simulated': 217, 'skipped': {'no_candles': 10, 'thin': 23, 'too_late': 0}, 'since': '2024-01-01'}

Primary: delay1h_slip0.6 {'delay_h': 1, 'slip_pct': 0.6, 'hold_max_h': 72, 'exit_buffer_h': 2}

## Grid (entry delay x round-trip slippage)

| scenario | basis | events | mean % | median % | win % | CI95 % | worst % | excess vs random % | percentile | 1st half | 2nd half | top5 share |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| delay0h_slip0.3 | hedged | 217 | 0.322 | -3.394 | 42.9 | [np.float64(-1.532), np.float64(2.191)] | -22.389 | 0.364 | 76.2 | 1.979 | -1.32 | 3.62 |
| delay0h_slip0.3 | raw | 217 | 0.699 | -4.718 | 42.4 | [np.float64(-1.17), np.float64(2.547)] | -16.888 | 0.495 | None | 1.228 | 0.175 | 1.635 |
| delay0h_slip0.6 | hedged | 217 | 0.022 | -3.694 | 41.5 | [np.float64(-1.832), np.float64(1.891)] | -22.689 | 0.364 | 76.2 | 1.679 | -1.62 | 53.349 |
| delay0h_slip0.6 | raw | 217 | 0.399 | -5.018 | 41.9 | [np.float64(-1.47), np.float64(2.247)] | -17.188 | 0.495 | None | 0.928 | -0.125 | 2.847 |
| delay0h_slip1.0 | hedged | 217 | -0.378 | -4.094 | 39.6 | [np.float64(-2.232), np.float64(1.491)] | -23.089 | 0.364 | 76.2 | 1.279 | -2.02 | None |
| delay0h_slip1.0 | raw | 217 | -0.001 | -5.418 | 40.1 | [np.float64(-1.87), np.float64(1.847)] | -17.588 | 0.495 | None | 0.528 | -0.525 | None |
| delay1h_slip0.3 | hedged | 217 | 0.538 | -2.924 | 42.4 | [np.float64(-1.336), np.float64(2.381)] | -21.852 | 0.736 | 93.2 | 1.908 | -0.819 | 2.208 |
| delay1h_slip0.3 | raw | 217 | 0.994 | -3.534 | 43.8 | [np.float64(-0.886), np.float64(2.858)] | -16.888 | 0.967 | None | 1.243 | 0.748 | 1.185 |
| delay1h_slip0.6 | hedged | 217 | 0.238 | -3.224 | 42.4 | [np.float64(-1.636), np.float64(2.081)] | -22.152 | 0.736 | 93.2 | 1.608 | -1.119 | 4.964 |
| delay1h_slip0.6 | raw | 217 | 0.694 | -3.834 | 42.4 | [np.float64(-1.186), np.float64(2.558)] | -17.188 | 0.967 | None | 0.943 | 0.448 | 1.687 |
| delay1h_slip1.0 | hedged | 217 | -0.162 | -3.624 | 41.5 | [np.float64(-2.036), np.float64(1.681)] | -22.552 | 0.736 | 93.2 | 1.208 | -1.519 | None |
| delay1h_slip1.0 | raw | 217 | 0.294 | -4.234 | 42.4 | [np.float64(-1.586), np.float64(2.158)] | -17.588 | 0.967 | None | 0.543 | 0.048 | 3.948 |
| delay6h_slip0.3 | hedged | 216 | -0.298 | -2.712 | 44.4 | [np.float64(-2.006), np.float64(1.407)] | -23.338 | -0.184 | 34.0 | 1.688 | -2.284 | None |
| delay6h_slip0.3 | raw | 216 | 0.141 | -3.058 | 43.1 | [np.float64(-1.555), np.float64(1.863)] | -23.989 | 0.001 | None | 1.39 | -1.108 | 7.366 |
| delay6h_slip0.6 | hedged | 216 | -0.598 | -3.012 | 43.5 | [np.float64(-2.306), np.float64(1.107)] | -23.638 | -0.184 | 34.0 | 1.388 | -2.584 | None |
| delay6h_slip0.6 | raw | 216 | -0.159 | -3.358 | 42.6 | [np.float64(-1.855), np.float64(1.563)] | -24.289 | 0.001 | None | 1.09 | -1.408 | None |
| delay6h_slip1.0 | hedged | 216 | -0.998 | -3.412 | 42.1 | [np.float64(-2.706), np.float64(0.707)] | -24.038 | -0.184 | 34.0 | 0.988 | -2.984 | None |
| delay6h_slip1.0 | raw | 216 | -0.559 | -3.758 | 40.7 | [np.float64(-2.255), np.float64(1.163)] | -24.689 | 0.001 | None | 0.69 | -1.808 | None |
| delay24h_slip0.3 | hedged | 213 | -0.492 | -1.252 | 44.6 | [np.float64(-1.843), np.float64(0.888)] | -15.89 | -0.325 | 21.4 | 0.792 | -1.764 | None |
| delay24h_slip0.3 | raw | 213 | -0.059 | -1.432 | 44.1 | [np.float64(-1.441), np.float64(1.342)] | -13.961 | -0.1 | None | 0.577 | -0.688 | None |
| delay24h_slip0.6 | hedged | 213 | -0.792 | -1.552 | 44.1 | [np.float64(-2.143), np.float64(0.588)] | -16.19 | -0.325 | 21.4 | 0.492 | -2.064 | None |
| delay24h_slip0.6 | raw | 213 | -0.359 | -1.732 | 42.3 | [np.float64(-1.741), np.float64(1.042)] | -14.261 | -0.1 | None | 0.277 | -0.988 | None |
| delay24h_slip1.0 | hedged | 213 | -1.192 | -1.952 | 42.7 | [np.float64(-2.543), np.float64(0.188)] | -16.59 | -0.325 | 21.4 | 0.092 | -2.464 | None |
| delay24h_slip1.0 | raw | 213 | -0.759 | -2.132 | 42.3 | [np.float64(-2.141), np.float64(0.642)] | -14.661 | -0.1 | None | -0.123 | -1.388 | None |

## By lead time (primary, hedged)

```
{
 "lead 0-1d": {
  "events": 3,
  "mean_pct": 15.198,
  "median_pct": 3.378,
  "win_pct": 66.7,
  "ci95_pct": null,
  "worst_pct": -10.71,
  "best_pct": 52.926,
  "baseline_mean_pct": -1.175,
  "excess_mean_pct": 16.373,
  "excess_ci95_pct": null,
  "percentile_vs_random": null,
  "first_half_mean_pct": -10.71,
  "second_half_mean_pct": 28.152,
  "top5_share_of_total": 1.0,
  "stops": 1,
  "mean_hours": 13.3
 },
 "lead 1-2d": {
  "events": 88,
  "mean_pct": -1.694,
  "median_pct": -3.473,
  "win_pct": 38.6,
  "ci95_pct": [
   -4.104,
   0.96
  ],
  "worst_pct": -17.946,
  "best_pct": 50.391,
  "baseline_mean_pct": -0.532,
  "excess_mean_pct": -1.162,
  "excess_ci95_pct": [
   -3.564,
   1.418
  ],
  "percentile_vs_random": 3.2,
  "first_half_mean_pct": 0.037,
  "second_half_mean_pct": -3.425,
  "top5_share_of_total": null,
  "stops": 39,
  "mean_hours": 31.6
 },
 "lead 2-4d": {
  "events": 63,
  "mean_pct": 0.033,
  "median_pct": -4.993,
  "win_pct": 39.7,
  "ci95_pct": [
   -3.37,
   3.816
  ],
  "worst_pct": -22.152,
  "best_pct": 61.612,
  "baseline_mean_pct": -0.906,
  "excess_mean_pct": 0.938,
  "excess_ci95_pct": [
   -2.632,
   4.828
  ],
  "percentile_vs_random": 86.6,
  "first_half_mean_pct": 0.494,
  "second_half_mean_pct": -0.414,
  "top5_share_of_total": 80.602,
  "stops": 30,
  "mean_hours": 43.9
 },
 "lead 4-30d": {
  "events": 63,
  "mean_pct": 2.429,
  "median_pct": -0.381,
  "win_pct": 49.2,
  "ci95_pct": [
   -1.246,
   6.046
  ],
  "worst_pct": -16.789,
  "best_pct": 45.574,
  "baseline_mean_pct": -0.009,
  "excess_mean_pct": 2.438,
  "excess_ci95_pct": [
   -1.161,
   6.104
  ],
  "percentile_vs_random": 99.0,
  "first_half_mean_pct": 4.101,
  "second_half_mean_pct": 0.81,
  "top5_share_of_total": 1.197,
  "stops": 29,
  "mean_hours": 50.3
 }
}
```

## By month (primary, hedged)

```
{
 "2024-07": {
  "events": 8,
  "mean_hedged_pct": -1.711
 },
 "2024-08": {
  "events": 2,
  "mean_hedged_pct": 1.535
 },
 "2024-09": {
  "events": 1,
  "mean_hedged_pct": 6.588
 },
 "2024-10": {
  "events": 6,
  "mean_hedged_pct": 4.109
 },
 "2024-11": {
  "events": 5,
  "mean_hedged_pct": 14.193
 },
 "2024-12": {
  "events": 4,
  "mean_hedged_pct": 4.281
 },
 "2025-01": {
  "events": 10,
  "mean_hedged_pct": 7.043
 },
 "2025-02": {
  "events": 8,
  "mean_hedged_pct": -3.733
 },
 "2025-03": {
  "events": 8,
  "mean_hedged_pct": 0.385
 },
 "2025-04": {
  "events": 13,
  "mean_hedged_pct": 5.137
 },
 "2025-05": {
  "events": 5,
  "mean_hedged_pct": -3.671
 },
 "2025-06": {
  "events": 6,
  "mean_hedged_pct": -4.028
 },
 "2025-07": {
  "events": 8,
  "mean_hedged_pct": 1.184
 },
 "2025-08": {
  "events": 4,
  "mean_hedged_pct": -3.29
 },
 "2025-09": {
  "events": 6,
  "mean_hedged_pct": 1.064
 },
 "2025-10": {
  "events": 35,
  "mean_hedged_pct": -0.955
 },
 "2025-11": {
  "events": 8,
  "mean_hedged_pct": -2.503
 },
 "2025-12": {
  "events": 7,
  "mean_hedged_pct": 5.494
 },
 "2026-01": {
  "events": 7,
  "mean_hedged_pct": 8.076
 },
 "2026-02": {
  "events": 11,
  "mean_hedged_pct": -6.235
 },
 "2026-03": {
  "events": 8,
  "mean_hedged_pct": -0.56
 },
 "2026-04": {
  "events": 16,
  "mean_hedged_pct": -2.808
 },
 "2026-05": {
  "events": 6,
  "mean_hedged_pct": -4.837
 },
 "2026-06": {
  "events": 10,
  "mean_hedged_pct": -6.094
 },
 "2026-07": {
  "events": 8,
  "mean_hedged_pct": -0.195
 },
 "2026-08": {
  "events": 3,
  "mean_hedged_pct": 3.93
 },
 "2026-09": {
  "events": 2,
  "mean_hedged_pct": -7.027
 },
 "2026-10": {
  "events": 2,
  "mean_hedged_pct": 21.287
 }
}
```

## Rules

```
{
 "entry": "open of the first hourly bar >= publish + 1 h",
 "exit": "earlier of entry + 72 h, delisting - 2 h, stop +10.0%",
 "fees_taker_pct": 0.055,
 "slippage_round_trip_pct": 0.6,
 "hedge": "long BTC, same notional and window",
 "baseline": "20 random shorts of the same length in the 30 days before the notice"
}
```

## Pass thresholds (fixed before the run)

```
{
 "min_events": 100,
 "min_mean_hedged_pct": 1.0,
 "max_worst_pct": -8.0,
 "max_top5_share": 0.5,
 "min_baseline_percentile": 95.0
}
```

## Limits

- Hourly bars hide intraday squeezes; the stop fills at its price or the bar's open (a gap is charged).
- Delisting coins are thin: the real slippage can exceed the 1.0% scenario; events with under 100k USDT traded in the 24h before the notice are skipped and counted.
- Bybit can halt trading or change margin on a delisting contract; borrow, margin calls and liquidation are not modelled.
- A notice published inside our entry delay, or with no room before the delisting, is skipped.
- A positive result is a hypothesis for a small real-money test, never `edge` by itself.
