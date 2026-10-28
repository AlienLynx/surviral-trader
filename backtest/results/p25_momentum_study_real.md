# Momentum study mom-real-20261012-185716

**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small test, not proof.**

## Verdict: `unproven`

Not met: block-bootstrap CI of the out-of-sample mean does not exclude 0; max drawdown 44.04% of the sleeve > 20.0%

Primary: L14_long_short_slip0.1. Window {'start': '2024-01-01', 'end': '2026-10-11', 'lock_end': '2025-07-01'}. Universe now: ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XRPUSDT', 'ZECUSDT', 'NEARUSDT', 'HYPEUSDT', 'SOXLUSDT', 'DOGEUSDT', 'ENAUSDT', 'SUIUSDT', 'CLUSDT', 'XAUUSDT', 'ONDOUSDT', 'PUMPFUNUSDT', 'ADAUSDT', '1000PEPEUSDT', 'QNTUSDT', 'WLDUSDT', 'UNIUSDT']

Placebo (random coins, same size and costs, 300 seeds): out-of-sample APR p5 / p50 / p95 = [-75.57, -9.18, 47.99]; the primary ranks at the 96.7th percentile.

## Results

| config | period | days | net % of sleeve | APR % | Sharpe | max DD % | CI95 of APR % | winning weeks % |
|---|---|---|---|---|---|---|---|---|
| L7_long_short_slip0.1 | all | 1014 | 60.02 | 21.61 | 0.43 | 63.4 | [-40.83, 91.76] | 47.2 |
| L7_long_short_slip0.1 | locked | 547 | -13.36 | -8.91 | -0.21 | 34.87 | [-64.63, 48.35] | 51.3 |
| L7_long_short_slip0.1 | oos | 467 | 73.38 | 57.35 | 0.99 | 63.4 | [-41.86, 168.56] | 45.5 |
| L7_long_short_slip0.3 | all | 1014 | 15.77 | 5.68 | 0.11 | 68.95 | [-56.76, 76.1] | 45.1 |
| L7_long_short_slip0.3 | locked | 547 | -37.71 | -25.16 | -0.6 | 42.64 | [-80.63, 32.38] | 48.7 |
| L7_long_short_slip0.3 | oos | 467 | 53.48 | 41.8 | 0.72 | 68.95 | [-58.54, 153.57] | 45.5 |
| L7_long_only_slip0.1 | all | 1014 | 138.47 | 49.84 | 0.48 | 114.14 | [-82.52, 206.79] | 53.5 |
| L7_long_only_slip0.1 | locked | 547 | 34.48 | 23.01 | 0.24 | 114.14 | [-126.11, 183.69] | 53.8 |
| L7_long_only_slip0.1 | oos | 467 | 103.99 | 81.28 | 0.71 | 69.83 | [-110.26, 326.85] | 40.9 |
| L7_long_only_slip0.3 | all | 1014 | 94.97 | 34.18 | 0.33 | 118.04 | [-98.35, 191.32] | 52.1 |
| L7_long_only_slip0.3 | locked | 547 | 10.38 | 6.92 | 0.07 | 118.04 | [-141.9, 167.55] | 52.6 |
| L7_long_only_slip0.3 | oos | 467 | 84.59 | 66.11 | 0.58 | 83.33 | [-126.25, 311.89] | 39.4 |
| L14_long_short_slip0.1 | all | 1014 | 116.04 | 41.77 | 0.84 | 44.04 | [-15.24, 110.06] | 50.7 |
| L14_long_short_slip0.1 | locked | 547 | 46.27 | 30.88 | 0.74 | 24.51 | [-24.99, 91.33] | 53.8 |
| L14_long_short_slip0.1 | oos | 467 | 69.77 | 54.53 | 0.94 | 44.04 | [-38.61, 158.62] | 48.5 |
| L14_long_short_slip0.3 | all | 1014 | 82.04 | 29.53 | 0.59 | 47.3 | [-27.81, 97.53] | 47.2 |
| L14_long_short_slip0.3 | locked | 547 | 27.07 | 18.07 | 0.43 | 28.04 | [-37.89, 78.88] | 50.0 |
| L14_long_short_slip0.3 | oos | 467 | 54.97 | 42.96 | 0.74 | 47.3 | [-50.55, 146.56] | 48.5 |
| L14_long_only_slip0.1 | all | 1014 | 141.54 | 50.95 | 0.47 | 114.62 | [-83.0, 208.05] | 49.3 |
| L14_long_only_slip0.1 | locked | 547 | 10.28 | 6.86 | 0.07 | 114.62 | [-150.88, 160.82] | 56.4 |
| L14_long_only_slip0.1 | oos | 467 | 131.26 | 102.59 | 0.88 | 88.28 | [-88.33, 351.27] | 47.0 |
| L14_long_only_slip0.3 | all | 1014 | 108.54 | 39.07 | 0.36 | 119.82 | [-95.14, 195.79] | 49.3 |
| L14_long_only_slip0.3 | locked | 547 | -8.52 | -5.69 | -0.06 | 119.82 | [-163.94, 148.49] | 56.4 |
| L14_long_only_slip0.3 | oos | 467 | 117.06 | 91.49 | 0.79 | 94.08 | [-99.15, 340.28] | 45.5 |
| L28_long_short_slip0.1 | all | 1014 | -51.13 | -18.4 | -0.39 | 141.27 | [-83.84, 39.46] | 52.1 |
| L28_long_short_slip0.1 | locked | 547 | 47.69 | 31.82 | 0.79 | 34.67 | [-27.75, 93.02] | 59.0 |
| L28_long_short_slip0.1 | oos | 467 | -98.82 | -77.24 | -1.46 | 125.51 | [-224.98, 26.94] | 45.5 |
| L28_long_short_slip0.3 | all | 1014 | -77.33 | -27.84 | -0.6 | 158.22 | [-93.61, 30.33] | 50.0 |
| L28_long_short_slip0.3 | locked | 547 | 32.94 | 21.98 | 0.55 | 39.37 | [-37.47, 83.07] | 57.7 |
| L28_long_short_slip0.3 | oos | 467 | -110.27 | -86.19 | -1.62 | 133.31 | [-233.68, 18.26] | 40.9 |
| L28_long_only_slip0.1 | all | 1014 | -5.4 | -1.94 | -0.02 | 190.42 | [-108.39, 108.82] | 47.2 |
| L28_long_only_slip0.1 | locked | 547 | 34.35 | 22.92 | 0.24 | 109.69 | [-120.55, 178.76] | 52.6 |
| L28_long_only_slip0.1 | oos | 467 | -39.75 | -31.07 | -0.36 | 166.3 | [-165.2, 117.38] | 47.0 |
| L28_long_only_slip0.3 | all | 1014 | -29.6 | -10.66 | -0.12 | 200.82 | [-116.77, 99.92] | 46.5 |
| L28_long_only_slip0.3 | locked | 547 | 20.95 | 13.98 | 0.15 | 112.09 | [-129.44, 170.45] | 52.6 |
| L28_long_only_slip0.3 | oos | 467 | -50.55 | -39.51 | -0.45 | 170.1 | [-173.8, 109.32] | 45.5 |

## Benchmarks

```
{
 "btc_hold": {
  "all": {
   "days": 1014,
   "net_pct_of_sleeve": 79.06,
   "apr_pct": 28.46,
   "sharpe": 0.6,
   "max_dd_pct": 68.77,
   "ci95_daily_apr_pct": [
    -21.46,
    81.95
   ],
   "win_week_pct": 51.4
  },
  "oos": {
   "days": 467,
   "net_pct_of_sleeve": -19.07,
   "apr_pct": -14.91,
   "sharpe": -0.36,
   "max_dd_pct": 68.77,
   "ci95_daily_apr_pct": [
    -84.39,
    60.91
   ],
   "win_week_pct": 47.0
  }
 },
 "equal_weight_universe": {
  "all": {
   "days": 1014,
   "net_pct_of_sleeve": 33.19,
   "apr_pct": 11.95,
   "sharpe": 0.16,
   "max_dd_pct": 92.35,
   "ci95_daily_apr_pct": [
    -73.7,
    102.03
   ],
   "win_week_pct": 50.0
  },
  "oos": {
   "days": 467,
   "net_pct_of_sleeve": 4.28,
   "apr_pct": 3.35,
   "sharpe": 0.05,
   "max_dd_pct": 90.08,
   "ci95_daily_apr_pct": [
    -116.04,
    128.48
   ],
   "win_week_pct": 47.0
  }
 }
}
```

## Rules

```
{
 "signal": "return over 14 days",
 "book": "long top 4, short bottom 4 of the top 20 by turnover, weekly, dollar neutral, half the sleeve per leg",
 "fee_pct": 0.055,
 "slippage_pct_per_side": [
  0.1,
  0.3
 ],
 "sleeve_usd": 3000.0
}
```

## Pass thresholds (fixed before the run)

```
{
 "min_oos_net_apr_pct": 10.0,
 "max_dd_pct_of_sleeve": 20.0,
 "min_placebo_percentile": 95.0,
 "min_oos_rebalances": 40
}
```

## Limits

- Daily open-to-open returns; no intraday risk, liquidation or borrow model; a coin that vanishes mid-week is held at its last price.
- The universe is ranked from the contracts Bybit's API still knows (delisted ones included); a contract removed from the API altogether is invisible.
- 2.7 years is one regime (mostly falling alts). A pass is a hypothesis for a small paper / real-money test, never `edge` by itself.
- Only the primary configuration counts for the verdict; the grid of lookbacks and modes is shown for context (a grid of six is a multiple-testing trap).
