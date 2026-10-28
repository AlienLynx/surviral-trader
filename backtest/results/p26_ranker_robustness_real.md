# Ranker robustness grid (hold x universe)

Generated 2026-10-26. Same walk-forward, features, fixed model and costs as the LightGBM v2 study; only hold and universe change. Primary = hold 3 / universe 40. Nothing here was tuned.

Out-of-sample (from 2024-07-01) APR positive in **12 of 12** settings at 0.10% slippage, **9 of 12** at 0.30%; mean rank IC positive in **12 of 12**.

| universe | hold d | quintile | OOS APR % (0.10%) | OOS Sharpe | OOS max DD % | locked APR % | OOS APR % (0.30%) | turnover / rebalance | OOS rank IC |
|---|---|---|---|---|---|---|---|---|---|
| 20 | 2 | 4 | 88.58 | 1.04 | 58.87 | 14.36 | 47.12 | 1.2 | 0.0678 |
| 20 | 3 | 4 | 102.91 | 1.29 | 93.88 | 26.07 | 73.53 | 1.26 | 0.0853 |
| 20 | 5 | 4 | 49.51 | 0.57 | 139.81 | 37.39 | 30.33 | 1.34 | 0.0928 |
| 20 | 7 | 4 | 14.33 | 0.18 | 158.58 | 1.9 | 0.24 | 1.35 | 0.0881 |
| 40 | 2 | 8 | 54.81 | 0.86 | 55.3 | 46.52 | 12.2 | 1.22 | 0.0486 |
| 40 | 3 | 8 | 97.64 | 1.69 | 54.2 | 52.01 | 67.88 | 1.24 | 0.069 |
| 40 | 5 | 8 | 55.24 | 0.97 | 65.51 | 24.13 | 36.61 | 1.3 | 0.0785 |
| 40 | 7 | 8 | 23.34 | 0.41 | 87.55 | 15.51 | 9.29 | 1.38 | 0.0734 |
| 60 | 2 | 12 | 23.92 | 0.49 | 44.23 | 8.95 | -18.39 | 1.2 | 0.0508 |
| 60 | 3 | 12 | 23.2 | 0.49 | 52.65 | 34.42 | -6.28 | 1.24 | 0.0726 |
| 60 | 5 | 12 | 26.92 | 0.62 | 41.58 | 30.06 | 8.38 | 1.28 | 0.07 |
| 60 | 7 | 12 | 13.7 | 0.31 | 80.19 | 22.49 | -0.44 | 1.38 | 0.0741 |
