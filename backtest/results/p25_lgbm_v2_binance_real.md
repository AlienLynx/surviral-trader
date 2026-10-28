# LightGBM walk-forward study v2 (Binance, hourly features) lgbh-real-20261013-181544

**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small test, not proof.**

## Verdict: `unproven`

Not met: max drawdown 54.2% of the sleeve > 20.0%

Primary: h3_slip0.1. Window {'start': '2021-01-01', 'first_prediction': '2022-01-01', 'end': '2026-10-12', 'lock_end': '2024-07-01'}.

Rank IC (score vs realised 7-day relative return): out-of-sample {'n': 277, 'mean_ic': 0.069, 'ic_hit_pct': 63.5, 'ci95': [0.0437, 0.0922]}, locked {'n': 304, 'mean_ic': 0.0665, 'ic_hit_pct': 63.5, 'ci95': [0.0446, 0.088]}.

Placebo (same pipeline, labels shuffled inside each day, 30 seeds): out-of-sample APR p5 / p50 / p95 = [-85.01, -34.06, 15.74]; the model ranks at the 100.0th percentile.

Models trained: 32. Top features by gain: [{'feature': 'ret168', 'gain_share_pct': 7.3}, {'feature': 'fund1', 'gain_share_pct': 5.9}, {'feature': 'vol168', 'gain_share_pct': 5.9}, {'feature': 'ret336', 'gain_share_pct': 5.8}, {'feature': 'fund7', 'gain_share_pct': 5.5}, {'feature': 'btc_vol168', 'gain_share_pct': 4.0}, {'feature': 'ret72', 'gain_share_pct': 3.8}, {'feature': 'imb72', 'gain_share_pct': 3.7}, {'feature': 'dist_lo168', 'gain_share_pct': 3.1}, {'feature': 'vol24', 'gain_share_pct': 3.1}]

## Results

| config | period | days | net % of sleeve | APR % | Sharpe | max DD % | CI95 of APR % | winning weeks % |
|---|---|---|---|---|---|---|---|---|
| h3_slip0.1 | all | 1745 | 356.09 | 74.48 | 1.63 | 54.2 | [35.14, 115.45] | 64.3 |
| h3_slip0.1 | locked | 912 | 129.94 | 52.01 | 1.75 | 30.41 | [18.48, 86.31] | 67.7 |
| h3_slip0.1 | oos | 833 | 226.15 | 99.09 | 1.7 | 54.2 | [34.04, 168.62] | 62.2 |
| h3_slip0.3 | all | 1745 | 211.19 | 44.17 | 0.97 | 65.3 | [4.7, 85.4] | 58.2 |
| h3_slip0.3 | locked | 912 | 53.27 | 21.32 | 0.71 | 43.96 | [-13.21, 55.49] | 57.7 |
| h3_slip0.3 | oos | 833 | 157.92 | 69.2 | 1.19 | 65.3 | [4.06, 138.81] | 59.7 |
| h1_slip0.1 | all | 1745 | 38.12 | 7.97 | 0.16 | 90.25 | [-38.42, 54.62] | 52.6 |
| h1_slip0.1 | locked | 912 | 0.21 | 0.08 | 0.0 | 51.23 | [-42.97, 41.06] | 50.8 |
| h1_slip0.1 | oos | 833 | 37.91 | 16.61 | 0.26 | 90.25 | [-56.63, 91.09] | 55.5 |
| h1_slip0.3 | all | 1745 | -375.68 | -78.58 | -1.59 | 394.04 | [-124.98, -31.79] | 41.4 |
| h1_slip0.3 | locked | 912 | -225.37 | -90.2 | -3.01 | 232.46 | [-133.76, -49.09] | 35.4 |
| h1_slip0.3 | oos | 833 | -150.31 | -65.86 | -1.02 | 207.41 | [-138.75, 8.72] | 44.5 |

## Benchmarks

```
{
 "mom14_hold3": {
  "all": {
   "days": 1745,
   "net_pct_of_sleeve": 314.78,
   "apr_pct": 65.84,
   "sharpe": 1.25,
   "max_dd_pct": 34.11,
   "ci95_daily_apr_pct": [
    24.11,
    111.75
   ],
   "win_week_pct": 57.4
  },
  "locked": {
   "days": 912,
   "net_pct_of_sleeve": 103.62,
   "apr_pct": 41.47,
   "sharpe": 1.17,
   "max_dd_pct": 25.65,
   "ci95_daily_apr_pct": [
    -2.51,
    85.55
   ],
   "win_week_pct": 58.5
  },
  "oos": {
   "days": 833,
   "net_pct_of_sleeve": 211.16,
   "apr_pct": 92.53,
   "sharpe": 1.39,
   "max_dd_pct": 26.28,
   "ci95_daily_apr_pct": [
    20.28,
    168.85
   ],
   "win_week_pct": 57.1
  }
 },
 "rev3_hold3": {
  "all": {
   "days": 1745,
   "net_pct_of_sleeve": -167.65,
   "apr_pct": -35.07,
   "sharpe": -0.71,
   "max_dd_pct": 192.63,
   "ci95_daily_apr_pct": [
    -81.66,
    12.03
   ],
   "win_week_pct": 43.0
  },
  "locked": {
   "days": 912,
   "net_pct_of_sleeve": -153.29,
   "apr_pct": -61.35,
   "sharpe": -1.83,
   "max_dd_pct": 162.21,
   "ci95_daily_apr_pct": [
    -102.01,
    -19.78
   ],
   "win_week_pct": 40.0
  },
  "oos": {
   "days": 833,
   "net_pct_of_sleeve": -14.36,
   "apr_pct": -6.29,
   "sharpe": -0.1,
   "max_dd_pct": 126.6,
   "ci95_daily_apr_pct": [
    -91.71,
    76.97
   ],
   "win_week_pct": 49.6
  }
 },
 "btc_hold": {
  "oos": {
   "days": 833,
   "net_pct_of_sleeve": 40.67,
   "apr_pct": 17.82,
   "sharpe": 0.39,
   "max_dd_pct": 68.93,
   "ci95_daily_apr_pct": [
    -38.91,
    75.02
   ],
   "win_week_pct": 52.1
  }
 }
}
```

## Rules

```
{
 "hold_days": 3,
 "retrain_days": 56,
 "min_train_days": 365,
 "model": {
  "objective": "regression",
  "num_leaves": 15,
  "n_estimators": 200,
  "learning_rate": 0.03,
  "min_child_samples": 200,
  "subsample": 0.7,
  "subsample_freq": 1,
  "colsample_bytree": 0.6,
  "reg_lambda": 10.0,
  "verbose": -1,
  "n_jobs": 6,
  "random_state": 7
 },
 "features": [
  "ret6",
  "ret12",
  "ret24",
  "ret72",
  "ret168",
  "ret336",
  "vol24",
  "vol168",
  "imb24",
  "imb72",
  "vol_surge",
  "trade_surge",
  "range24",
  "dist_hi168",
  "dist_lo168",
  "fund1",
  "fund7",
  "btc_ret24",
  "btc_ret168",
  "btc_vol168",
  "mkt_ret24",
  "mkt_disp24",
  "rk_ret6",
  "rk_ret12",
  "rk_ret24",
  "rk_ret72",
  "rk_ret168",
  "rk_ret336",
  "rk_vol24",
  "rk_vol168",
  "rk_imb24",
  "rk_imb72",
  "rk_vol_surge",
  "rk_trade_surge",
  "rk_range24",
  "rk_dist_hi168",
  "rk_dist_lo168",
  "rk_fund1",
  "rk_fund7"
 ],
 "book": "long top 8, short bottom 8 of the prediction, every 3 days, dollar neutral",
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
 "min_oos_rebalances": 40,
 "min_ic": 0.02,
 "min_beats_momentum_apr_pct": 0.0,
 "min_stress_apr_pct": 0.0,
 "min_clipped_oos_apr_pct": 10.0
}
```

## Limits

- Daily marking of a 3-day hold: no intraday stop, liquidation or margin model; a coin that vanishes is held flat at its last price.
- Binance USDT-M perpetuals only; the archive lists delisted contracts, so the universe is not survivor-biased, but a contract missing from the archive is invisible.
- About five years: bull, crash and a long falling-alt period, still few independent regimes. The first year only trains, the 'locked' part (until 2024-07) is for looking, the rest decides.
- One fixed parameter set, no search. Only the primary configuration (3-day hold, 0.10% slippage) counts for the verdict; the 1-day hold and the stress slippage are context (the stress one is a pass condition).
- Placebo = 30 pipelines with shuffled labels (percentile steps of ~3). A pass is a hypothesis for a small paper test, never `edge` by itself.
- The clipped-returns condition and the robustness block were added after the first real run (they can only tighten the verdict).

## Robustness of the primary predictions (diagnostics, nothing is refit)

```
{
 "concentration_pct_of_sleeve": {
  "total": 356.1,
  "best_10_days": 141.8,
  "worst_10_days": -119.9,
  "total_without_best_20_days": 140.0
 },
 "apr_pct_by_year": {
  "2022": 8.8,
  "2023": 98.9,
  "2024": 79.8,
  "2025": 65.3,
  "2026": 132.4
 },
 "daily_coin_return_clipped_at_50pct": {
  "all": {
   "days": 1745,
   "net_pct_of_sleeve": 296.85,
   "apr_pct": 62.09,
   "sharpe": 1.71,
   "max_dd_pct": 35.77,
   "ci95_daily_apr_pct": [
    31.04,
    93.99
   ],
   "win_week_pct": 64.3
  },
  "oos": {
   "days": 833,
   "net_pct_of_sleeve": 172.27,
   "apr_pct": 75.48,
   "sharpe": 1.73,
   "max_dd_pct": 35.77,
   "ci95_daily_apr_pct": [
    23.21,
    126.52
   ],
   "win_week_pct": 63.9
  }
 },
 "without_coins_with_a_100pct_day": {
  "coins_removed": 88,
  "all": {
   "days": 1745,
   "net_pct_of_sleeve": 275.54,
   "apr_pct": 57.63,
   "sharpe": 1.84,
   "max_dd_pct": 48.87,
   "ci95_daily_apr_pct": [
    30.1,
    86.22
   ],
   "win_week_pct": 63.1
  },
  "oos": {
   "days": 833,
   "net_pct_of_sleeve": 163.36,
   "apr_pct": 71.58,
   "sharpe": 2.12,
   "max_dd_pct": 29.37,
   "ci95_daily_apr_pct": [
    27.06,
    117.2
   ],
   "win_week_pct": 58.8
  },
  "note": "chosen with hindsight: a diagnostic, not a trading rule"
 },
 "legs_gross_of_the_other_leg": {
  "long": {
   "apr_pct_all": 31.5,
   "apr_pct_oos": 51.2
  },
  "short": {
   "apr_pct_all": 28.8,
   "apr_pct_oos": 33.4
  }
 },
 "momentum_lookback_sensitivity_hold3": {
  "mom3": {
   "apr_pct_all": -22.39,
   "apr_pct_oos": -50.77
  },
  "mom7": {
   "apr_pct_all": -1.66,
   "apr_pct_oos": -19.82
  },
  "mom14": {
   "apr_pct_all": 65.84,
   "apr_pct_oos": 92.53
  },
  "mom28": {
   "apr_pct_all": 7.1,
   "apr_pct_oos": -8.09
  },
  "mom56": {
   "apr_pct_all": 35.06,
   "apr_pct_oos": 58.07
  }
 }
}
```
