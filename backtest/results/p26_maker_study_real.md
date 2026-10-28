# Maker-fill cost model mk-real-20261026-143900

## Ranker verdict: `passive pays`

All pre-registered conditions met.

Maker fee 0.02%, taker fee 0.055% + slippage 0.1% (stress 0.3%) per side.

## Part 1: LightGBM ranker book (primary: limit 0.10% better than the open, 2 h to fill, then chase)

| execution | OOS APR % | OOS Sharpe | OOS max DD % | locked APR % | fill rate | chased | skipped |
|---|---|---|---|---|---|---|---|
| taker_slip0.1 | 97.64 | 1.69 | 54.2 | 52.01 | - | - | - |
| taker_slip0.3 | 67.88 | 1.17 | 65.3 | 21.32 | - | - | - |
| skip 0.05% / 1 h | 93.65 | 1.64 | 54.7 | 64.05 | 0.839 | 0 | 1816 |
| skip 0.05% / 2 h | 96.48 | 1.69 | 54.36 | 60.09 | 0.854 | 0 | 1636 |
| skip 0.05% / 4 h | 96.83 | 1.68 | 55.08 | 62.65 | 0.863 | 0 | 1535 |
| skip 0.10% / 1 h | 85.37 | 1.48 | 67.63 | 55.4 | 0.804 | 0 | 2238 |
| skip 0.10% / 2 h | 82.37 | 1.43 | 66.59 | 53.85 | 0.828 | 0 | 1942 |
| skip 0.10% / 4 h | 84.76 | 1.46 | 64.66 | 56.91 | 0.843 | 0 | 1771 |
| skip 0.20% / 1 h | 84.34 | 1.46 | 52.33 | 53.06 | 0.737 | 0 | 3030 |
| skip 0.20% / 2 h | 90.44 | 1.57 | 51.78 | 50.77 | 0.779 | 0 | 2520 |
| skip 0.20% / 4 h | 93.39 | 1.6 | 53.31 | 54.89 | 0.806 | 0 | 2204 |
| chase 0.05% / 1 h | 109.56 | 1.89 | 49.25 | 68.07 | 0.936 | 623 | 0 |
| chase 0.05% / 2 h | 110.93 | 1.91 | 48.74 | 67.92 | 0.954 | 438 | 0 |
| chase 0.05% / 4 h | 111.89 | 1.93 | 47.98 | 68.99 | 0.963 | 336 | 0 |
| chase 0.10% / 1 h | 108.99 | 1.88 | 48.71 | 66.04 | 0.896 | 1032 | 0 |
| chase 0.10% / 2 h (primary) | 110.36 | 1.9 | 48.68 | 65.29 | 0.924 | 742 | 0 |
| chase 0.10% / 4 h | 110.8 | 1.91 | 47.99 | 67.19 | 0.941 | 573 | 0 |
| chase 0.20% / 1 h | 108.98 | 1.88 | 48.3 | 65.61 | 0.825 | 1770 | 0 |
| chase 0.20% / 2 h | 111.87 | 1.93 | 48.71 | 65.32 | 0.87 | 1305 | 0 |
| chase 0.20% / 4 h | 113.92 | 1.97 | 47.75 | 66.86 | 0.9 | 989 | 0 |

## Part 2: Listing Fade entries with a limit order (4713 replayed trades; live rules, re-entry, 3 slots, 0.30% taker slippage on the exit and on the taker baseline)

| limit above open | wait | fill rate | taker P&L per trade if it WOULD fill ($) | ... if it would NOT fill ($) | net $ taker (all) | net $ skip unfilled | net $ chase unfilled |
|---|---|---|---|---|---|---|---|
| 0.00% | 15 min | 1.0 | -0.088 | None | -413.18 | 35.88 | 35.88 |
| 0.00% | 60 min | 1.0 | -0.088 | None | -413.18 | 35.88 | 35.88 |
| 0.10% | 15 min | 0.947 | -0.11 | 0.319 | -413.18 | 18.59 | 14.74 |
| 0.10% | 60 min | 0.973 | -0.105 | 0.55 | -413.18 | 14.15 | 29.77 |
| 0.20% | 15 min | 0.912 | -0.12 | 0.243 | -413.18 | 45.18 | 27.72 |
| 0.20% | 60 min | 0.954 | -0.113 | 0.439 | -413.18 | 52.8 | 51.84 |

If the trades that would NOT fill earn more per trade than the ones that would, passive entry removes the winners (adverse selection).

- No order-book data: queue position is not modelled (delta 0 is an optimistic bound).
- The fill rule is a touch of the hourly / 5-minute high or low, with no knowledge of the order inside the bar.
- The maker fee is the public tier (0.02%). A chase pays the price move since the open, but the position is not marked for the hours before it is filled.
- Fade part: slot interactions of skipped trades are not re-simulated; it is a diagnostic without a verdict.
