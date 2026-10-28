# P17-B Trend Ensemble v2 -- backtest report

**Verdict: NOT CONFIRMED (failed the admission gates)** -- deployed dry-run only, marked 'not confirmed'. No tuning after the test window was looked at.

## Gates (test window, fees on)

| gate | result | value |
|---|---|---|
| max drawdown <= 30% (fees on) | FAIL | 49.03% |
| >= 100 trades (long+short together) | PASS | 271 |
| short leg not below zero | FAIL | $-24.06 |

## Comparison on the TEST window (2024-01-01 -> today)

| strategy | return % | CAGR % | Sharpe | Sortino | max DD % | Calmar | trades | expectancy $ | long $ | short $ |
|---|---|---|---|---|---|---|---|---|---|---|
| v1 (spot long-only, frozen) | -1.56 | -0.57 | -0.083 | -0.175 | 12.1 | -0.246 | 347 | -0.022 | -7.78 | 0.0 |
| v2 long-only | 26.34 | 8.9 | 0.132 | 0.828 | 42.94 | 1.171 | 190 | 0.693 | 131.68 | 0.0 |
| v2 long+short (chosen) | -13.77 | -5.26 | -0.19 | -0.5 | 49.03 | -0.536 | 271 | -0.254 | -44.78 | -24.06 |
| control-long (5m sample strategy, spot) | -92.1 | -60.33 | -9.95 | -6.423 | 92.21 | -1.904 | 5141 | -0.09 | -460.49 | 0.0 |
| control-short (5m sample strategy, futures) | -13.3 | -5.06 | -0.979 | -0.75 | 13.71 | -1.849 | 440 | -0.151 | 0.0 | -66.48 |
| BTC buy-and-hold | 88.48 | 26.02 | 0.724 | 1.107 | 52.97 | 0.491 | 1 | - | - | - |
| Equal-weight hold (15 pairs) | 16.79 | 5.83 | 0.398 | 0.568 | 65.85 | 0.088 | 1 | - | - | - |

Fees: 0.1% per side for perps (0.15% spot = fee + slippage), funding fees included for futures, `--timeframe-detail 1h`, `--enable-protections`. Wallet $500, 10 slots, 15 fixed long-lived perps (survivorship bias: they are 15 coins that still exist today). Buy-and-hold rows use daily closes, one entry and one exit fee, no rebalance. Freqtrade's Sharpe/Sortino/Calmar are trade-based; buy-and-hold ones are daily-equity based -- comparable in sign and order of magnitude, not to the third decimal.

## Training grid (2021-01-01 .. 2023-12-31): the 6 declared variants

| exit channel | min votes | return % | Sharpe | max DD % | trades | long $ | short $ | smoothed score |
|---|---|---|---|---|---|---|---|---|
| 10 | 2 | 22.27 | 0.174 | 40.98 | 206 | 152.23 | -40.87 | 0.096 |
| 10 | 3 | 2.01 | 0.018 | 36.17 | 180 | 78.0 | -67.93 | 0.079 |
| 10 | 4 | 5.52 | 0.046 | 37.66 | 144 | 72.14 | -44.55 | 0.032 |
| 20 | 2 | -6.87 | -0.076 | 39.32 | 163 | 36.46 | -70.79 | -0.042 |
| 20 | 3 | -0.85 | -0.007 | 42.61 | 148 | 58.4 | -62.66 | -0.026 |
| 20 | 4 | 0.53 | 0.004 | 40.59 | 117 | 27.63 | -24.97 | -0.002 |

Chosen by the pre-declared plateau rule: **exit channel 10, min votes 2**.

## Breakdown: v2 long+short (chosen), test window

By year (close date):

| year | trades | profit_abs | win_rate_pct |
|---|---|---|---|
| 2024 | 99 | -106.99 | 30.3 |
| 2025 | 97 | -100.96 | 29.9 |
| 2026 | 75 | 139.11 | 48.0 |

By leg:

| leg | trades | profit_abs | avg_profit_pct | expectancy_usd | win_rate_pct | funding_fees_total |
|---|---|---|---|---|---|---|
| long | 167 | -44.78 | -0.13 | -0.268 | 35.3 | -58.76 |
| short | 104 | -24.06 | -0.64 | -0.231 | 34.6 | 0.65 |

By BTC regime (above/below its 200d SMA the day before entry) and leg:

| btc_vs_200d | leg | trades | profit_abs | avg_profit_pct | win_rate_pct |
|---|---|---|---|---|---|
| above | long | 167 | -44.78 | -0.13 | 35.3 |
| below | short | 104 | -24.06 | -0.64 | 34.6 |

Exit reasons:

| leg | exit | n | profit | avg_pct | avg_days |
|---|---|---|---|---|---|
| long | donchian_exit_long | 113 | 136.84 | 3.79 | 25.0 |
| long | force_exit | 10 | 74.53 | 22.88 | 15.2 |
| long | stop_loss | 44 | -256.15 | -15.41 | 8.2 |
| short | donchian_exit_short | 85 | 58.34 | 2.62 | 26.5 |
| short | stop_loss | 19 | -82.4 | -15.22 | 9.6 |

Profit concentration: total $-68.84, best trade $38.88 (DOGE/USDT:USDT), excluding the best trade $-107.71, excluding the top 3 $-165.9.

## Breakdown: v2 long-only, test window

By year (close date):

| year | trades | profit_abs | win_rate_pct |
|---|---|---|---|
| 2024 | 88 | 64.96 | 29.5 |
| 2025 | 86 | -58.41 | 24.4 |
| 2026 | 16 | 125.13 | 87.5 |

By leg:

| leg | trades | profit_abs | avg_profit_pct | expectancy_usd | win_rate_pct | funding_fees_total |
|---|---|---|---|---|---|---|
| long | 190 | 131.68 | 1.62 | 0.693 | 32.1 | -75.72 |

By BTC regime (above/below its 200d SMA the day before entry) and leg:

| btc_vs_200d | leg | trades | profit_abs | avg_profit_pct | win_rate_pct |
|---|---|---|---|---|---|
| above | long | 190 | 131.68 | 1.62 | 32.1 |

Exit reasons:

| leg | exit | n | profit | avg_pct | avg_days |
|---|---|---|---|---|---|
| long | donchian_exit_long | 124 | 426.31 | 8.18 | 23.7 |
| long | force_exit | 10 | 90.29 | 15.72 | 14.1 |
| long | stop_loss | 56 | -384.92 | -15.44 | 9.2 |

Profit concentration: total $131.68, best trade $174.66 (XRP/USDT:USDT), excluding the best trade $-42.98, excluding the top 3 $-237.79.

## Breakdown: v1, test window

By year (close date):

| year | trades | profit_abs | win_rate_pct |
|---|---|---|---|
| 2024 | 171 | 14.17 | 65.5 |
| 2025 | 94 | -14.61 | 56.4 |
| 2026 | 82 | -7.34 | 56.1 |

By leg:

| leg | trades | profit_abs | avg_profit_pct | expectancy_usd | win_rate_pct | funding_fees_total |
|---|---|---|---|---|---|---|
| long | 347 | -7.78 | -0.34 | -0.022 | 60.8 | 0.0 |

By BTC regime (above/below its 200d SMA the day before entry) and leg:

| btc_vs_200d | leg | trades | profit_abs | avg_profit_pct | win_rate_pct |
|---|---|---|---|---|---|
| above | long | 281 | -5.82 | -0.07 | 61.9 |
| below | long | 66 | -1.96 | -1.46 | 56.1 |

Exit reasons:

| leg | exit | n | profit | avg_pct | avg_days |
|---|---|---|---|---|---|
| long | force_exit | 9 | -3.35 | -1.83 | 21.0 |
| long | stop_loss | 129 | -267.86 | -15.25 | 14.3 |
| long | trailing_stop_loss | 209 | 263.43 | 8.93 | 9.4 |

Profit concentration: total $-7.78, best trade $4.78 (BTC/USDT), excluding the best trade $-12.57, excluding the top 3 $-21.99.
