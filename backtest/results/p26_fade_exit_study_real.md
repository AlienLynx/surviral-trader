# Listing Fade exit-mechanics study fx-real-20261024-141522

**Rule-based historical study, $0, no LLM. A pass is a hypothesis for a live dry-run, never a change of a live setting.**

## Verdict for `stop8_cd24`: `not better`

Not met: the paired out-of-sample daily P&L difference does not exclude 0; locked period: candidate $-427.35 < live $-159.38; out-of-sample max drawdown 273.48% > live 220.81%; at 0.5% slippage the candidate ($-489.02) is not above live ($-429.43)

Live rules: stop -12%, no cooldown. Candidate: stop -8%, 24 h cooldown after a stop-loss. Split date 2025-07-01 (before: looking, from: deciding). Basis {'fee_pct': 0.055, 'slippage_pct': [0.3, 0.5], 'slots': 3, 'reentry': True}.

Stop-loss fills in the live-rules run gap {'n': 1156, 'mean': 0.0, 'max': 0.0} (percent beyond the stop level).

## Variants at 0.3% slippage

| variant | stop % | cooldown h | period | n | net $ | mean % | win % | stops | mean stop % | max DD % |
|---|---|---|---|---|---|---|---|---|---|---|
| live | -12 | 0 | all | 4713 | -413.18 | -0.351 | 74.5 | 1156 | -12.25 | 418.74 |
| live | -12 | 0 | locked | 2675 | -159.38 | -0.238 | 75.3 | 634 | -12.23 | 167.67 |
| live | -12 | 0 | oos | 2038 | -253.8 | -0.498 | 73.4 | 522 | -12.28 | 220.81 |
| cd24 | -12 | 24 | all | 4302 | -343.63 | -0.32 | 74.8 | 1032 | -12.25 | 349.37 |
| cd24 | -12 | 24 | locked | 2486 | -150.81 | -0.243 | 75.1 | 586 | -12.23 | 165.22 |
| cd24 | -12 | 24 | oos | 1816 | -192.82 | -0.425 | 74.3 | 446 | -12.29 | 184.23 |
| stop10 | -10 | 0 | all | 5552 | -749.46 | -0.54 | 69.5 | 1640 | -10.25 | 748.05 |
| stop10 | -10 | 0 | locked | 3117 | -358.89 | -0.461 | 70.4 | 891 | -10.24 | 365.68 |
| stop10 | -10 | 0 | oos | 2435 | -390.57 | -0.642 | 68.3 | 749 | -10.26 | 338.08 |
| stop10_cd24 | -10 | 24 | all | 4872 | -559.21 | -0.459 | 70.1 | 1397 | -10.24 | 558.27 |
| stop10_cd24 | -10 | 24 | locked | 2824 | -277.91 | -0.394 | 70.6 | 795 | -10.24 | 279.95 |
| stop10_cd24 | -10 | 24 | oos | 2048 | -281.3 | -0.549 | 69.3 | 602 | -10.25 | 272.89 |
| stop8 | -8 | 0 | all | 6833 | -999.9 | -0.585 | 64.4 | 2378 | -8.27 | 1002.32 |
| stop8 | -8 | 0 | locked | 3843 | -511.87 | -0.533 | 65.1 | 1306 | -8.26 | 517.28 |
| stop8 | -8 | 0 | oos | 2990 | -488.03 | -0.653 | 63.5 | 1072 | -8.28 | 469.41 |
| stop8_cd24 | -8 | 24 | all | 5773 | -711.01 | -0.493 | 65.2 | 1963 | -8.26 | 711.38 |
| stop8_cd24 | -8 | 24 | locked | 3327 | -427.35 | -0.514 | 65.1 | 1135 | -8.26 | 447.53 |
| stop8_cd24 | -8 | 24 | oos | 2446 | -283.66 | -0.464 | 65.3 | 828 | -8.26 | 273.48 |
| stop6 | -6 | 0 | all | 8858 | -1696.31 | -0.766 | 56.1 | 3843 | -6.27 | 1698.86 |
| stop6 | -6 | 0 | locked | 5107 | -944.91 | -0.74 | 56.5 | 2191 | -6.27 | 948.16 |
| stop6 | -6 | 0 | oos | 3751 | -751.4 | -0.801 | 55.5 | 1652 | -6.28 | 737.28 |
| stop6_cd24 | -6 | 24 | all | 6920 | -967.35 | -0.559 | 58.2 | 2846 | -6.27 | 969.51 |
| stop6_cd24 | -6 | 24 | locked | 3955 | -604.04 | -0.611 | 57.7 | 1650 | -6.27 | 619.55 |
| stop6_cd24 | -6 | 24 | oos | 2965 | -363.31 | -0.49 | 58.9 | 1196 | -6.27 | 365.37 |

## Out-of-sample difference to the live rules (paired daily P&L, USD)

| variant | days | total $ | mean $/day | 95% CI $/day |
|---|---|---|---|---|
| live | 481 | 0.0 | 0.0 | [0.0, 0.0] |
| cd24 | 481 | 60.98 | 0.1268 | [-0.1535, 0.3835] |
| stop10 | 481 | -136.77 | -0.2843 | [-0.5048, -0.0506] |
| stop10_cd24 | 481 | -27.5 | -0.0572 | [-0.3943, 0.2471] |
| stop8 | 481 | -234.23 | -0.487 | [-0.757, -0.2275] |
| stop8_cd24 | 481 | -29.85 | -0.0621 | [-0.3646, 0.2395] |
| stop6 | 481 | -497.6 | -1.0345 | [-1.3537, -0.7397] |
| stop6_cd24 | 481 | -109.51 | -0.2277 | [-0.5627, 0.0916] |

## Stress: 0.5% slippage, out-of-sample net $

| variant | net $ | n |
|---|---|---|
| live | -429.43 | 2015 |
| cd24 | -346.3 | 1755 |
| stop10 | -567.53 | 2352 |
| stop10_cd24 | -405.75 | 2026 |
| stop8 | -771.99 | 2913 |
| stop8_cd24 | -489.02 | 2410 |
| stop6 | -1085.38 | 3715 |
| stop6_cd24 | -629.48 | 2967 |

## Rules read from the live files

```
{
 "strategy_sha256": "cfad8bdb93b097cdadf4fd7b32dc36d4d97209e9b865cb861ebe64fb1c4ba6ec",
 "config_sha256": "23fd76f6629654f4bd8e1aac2c55bfc6a803d8cba4be193ea61cd742a710d5e3",
 "stoploss": -0.12,
 "trailing_stop": true,
 "trailing_positive": 0.03,
 "trailing_offset": 0.035,
 "trailing_only_offset": true,
 "minimal_roi": {
  "0": 10.0
 },
 "max_hold_days": 14,
 "pct_from_high": -25.0,
 "ema_period": 20,
 "funding_threshold": -0.0003,
 "timeframe": "1h",
 "max_open_trades": 3,
 "stake": 25.0,
 "wallet": 100.0,
 "age_min_days": 2,
 "age_max_days": 30,
 "price_low_ratio": 0.01,
 "not_applied": [
  "VolumePairList (top 200 by quote volume): the historical ranking of all pairs is not public",
  "SpreadFilter (max_spread_ratio 0.005): the historical order book is not public"
 ],
 "strategy_file": "ListingFadeStrategy.py"
}
```

## Limits

- The live pair list (VolumePairList top 200, SpreadFilter) cannot be rebuilt from public history: the replay trades every listing that passes the age, price and funding rules.
- The live entry is a passive limit order that can expire unfilled; the slippage lines stand in for it.
- Stop and trailing fills use 5-minute candles: a stop gaps to the next candle's open, but a spike inside one 5-minute candle is only as visible as that candle.
- Survivorship bias is reduced, not removed (see the Listing Fade study). One regime: listings from 2024 to 2026.
- The idea came from four live weeks (not part of this data). The deciding period is the listings from the split date on; the other seven variants are context and cannot give the verdict.
- VolumePairList (top 200 by quote volume): the historical ranking of all pairs is not public
- SpreadFilter (max_spread_ratio 0.005): the historical order book is not public
