# LightGBM walk-forward study lgbm-real-20261013-132524

**Rule-based historical study, $0, no LLM. A pass here is a hypothesis for a small test, not proof.**

## Verdict: `unproven`

Not met: block-bootstrap CI of the out-of-sample mean does not exclude 0; max drawdown 30.84% of the sleeve > 20.0%; percentile vs shuffled-label placebo 70.0 < 95.0

Primary: lgbm_slip0.1. Window {'start': '2024-01-01', 'first_prediction': '2024-12-31', 'end': '2026-10-12', 'lock_end': '2025-07-01'}.

Rank IC (score vs realised 7-day relative return): out-of-sample {'n': 66, 'mean_ic': 0.0328, 'ic_hit_pct': 51.5, 'ci95': [-0.0214, 0.0979]}, locked {'n': 26, 'mean_ic': 0.0267, 'ic_hit_pct': 53.8, 'ci95': [-0.1223, 0.173]}.

Placebo (same pipeline, labels shuffled inside each day, 20 seeds): out-of-sample APR p5 / p50 / p95 = [-29.14, -8.0, 44.35]; the model ranks at the 70.0th percentile.

Models trained: 23. Top features by gain: [{'feature': 'vol14', 'gain_share_pct': 15.3}, {'feature': 'dist_high28', 'gain_share_pct': 10.7}, {'feature': 'turn_ratio', 'gain_share_pct': 9.4}, {'feature': 'ret28', 'gain_share_pct': 9.0}, {'feature': 'funding7', 'gain_share_pct': 7.7}, {'feature': 'btc_vol28', 'gain_share_pct': 5.4}, {'feature': 'ret14', 'gain_share_pct': 5.4}, {'feature': 'ret7', 'gain_share_pct': 4.8}]

## Results

| config | period | days | net % of sleeve | APR % | Sharpe | max DD % | CI95 of APR % | winning weeks % |
|---|---|---|---|---|---|---|---|---|
| lgbm_slip0.1 | all | 650 | 21.41 | 12.02 | 0.32 | 31.2 | [-40.35, 63.39] | 52.2 |
| lgbm_slip0.1 | locked | 182 | 7.72 | 15.49 | 0.35 | 31.2 | [-83.85, 109.77] | 65.4 |
| lgbm_slip0.1 | oos | 468 | 13.68 | 10.67 | 0.32 | 30.84 | [-46.51, 75.64] | 47.0 |
| lgbm_slip0.3 | all | 650 | -2.04 | -1.15 | -0.03 | 43.19 | [-53.57, 50.36] | 47.8 |
| lgbm_slip0.3 | locked | 182 | 1.67 | 3.35 | 0.07 | 32.3 | [-94.77, 97.65] | 61.5 |
| lgbm_slip0.3 | oos | 468 | -3.72 | -2.9 | -0.09 | 43.19 | [-60.45, 61.78] | 42.4 |

## Benchmarks

```
{
 "momentum_14d_same_days": {
  "all": {
   "days": 650,
   "net_pct_of_sleeve": 53.68,
   "apr_pct": 30.14,
   "sharpe": 0.78,
   "max_dd_pct": 45.32,
   "ci95_daily_apr_pct": [
    -21.45,
    79.31
   ],
   "win_week_pct": 53.3
  },
  "locked": {
   "days": 182,
   "net_pct_of_sleeve": 44.41,
   "apr_pct": 89.07,
   "sharpe": 1.94,
   "max_dd_pct": 13.18,
   "ci95_daily_apr_pct": [
    16.82,
    165.04
   ],
   "win_week_pct": 65.4
  },
  "oos": {
   "days": 468,
   "net_pct_of_sleeve": 9.27,
   "apr_pct": 7.23,
   "sharpe": 0.2,
   "max_dd_pct": 45.32,
   "ci95_daily_apr_pct": [
    -61.51,
    72.39
   ],
   "win_week_pct": 48.5
  }
 },
 "btc_hold": {
  "oos": {
   "days": 468,
   "net_pct_of_sleeve": -18.13,
   "apr_pct": -14.14,
   "sharpe": -0.34,
   "max_dd_pct": 68.77,
   "ci95_daily_apr_pct": [
    -84.39,
    60.91
   ],
   "win_week_pct": 47.0
  }
 }
}
```

## Rules

```
{
 "horizon_days": 7,
 "retrain_days": 28,
 "min_train_days": 365,
 "model": {
  "objective": "regression",
  "num_leaves": 7,
  "n_estimators": 200,
  "learning_rate": 0.03,
  "min_child_samples": 100,
  "subsample": 0.7,
  "subsample_freq": 1,
  "colsample_bytree": 0.7,
  "reg_lambda": 5.0,
  "verbose": -1,
  "n_jobs": 2,
  "random_state": 7
 },
 "features": [
  "ret1",
  "ret3",
  "ret7",
  "ret14",
  "ret28",
  "vol14",
  "turn_ratio",
  "funding7",
  "dist_high28",
  "btc_ret7",
  "btc_vol28",
  "rk_ret1",
  "rk_ret3",
  "rk_ret7",
  "rk_ret14",
  "rk_ret28",
  "rk_vol14",
  "rk_turn_ratio",
  "rk_funding7",
  "rk_dist_high28"
 ],
 "book": "long top 4, short bottom 4 of the prediction, weekly, dollar neutral",
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
 "min_beats_momentum_apr_pct": 0.0
}
```

## Limits

- Daily open-to-open returns; no intraday risk, liquidation or borrow model; a coin that vanishes mid-week is held at its last price.
- The universe is ranked from the contracts Bybit's API still knows (delisted ones included); a contract removed from the API altogether is invisible.
- 2.7 years is one regime (mostly falling alts); the first year is training only, so the walk-forward trades roughly 1.7 years, of which the locked part is for looking.
- One fixed parameter set, no search: a search over parameters on this little data would only fit noise. Only the primary configuration counts for the verdict.
- Placebo = 20 pipelines with shuffled labels (a coarse estimate; the percentile has steps of 5). A pass is a hypothesis for a small paper test, never `edge` by itself.
