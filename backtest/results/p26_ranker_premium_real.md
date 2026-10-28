# Premium-index features vs the v2 feature set lgp-real-20261026-141310

## Verdict: `keep v2 features`

Conditions: {'oos_ic_up_by_0.01': False, 'apr_at_0.30_not_lower': False, 'locked_ic_not_lower': False}. Out-of-sample IC change -0.0007.

Hold 3 d, universe 40, quintile 8; same walk-forward, parameters, split (2024-07-01) and costs. Rule: adopt only if the out-of-sample rank IC rises by at least 0.01, the APR at 0.30% slippage is not lower and the locked-period IC is not lower.

| feature set | OOS IC (CI) | locked IC | OOS APR % 0.10% | OOS Sharpe | OOS max DD % | OOS APR % 0.30% | locked APR % |
|---|---|---|---|---|---|---|---|
| base | 0.069 [0.0437, 0.0922] | 0.0665 | 97.64 | 1.69 | 54.2 | 67.88 | 52.01 |
| +premium | 0.0683 [0.0433, 0.0903] | 0.0534 | 75.91 | 1.38 | 52.27 | 46.03 | 44.63 |

Premium features' share of the model's gain: prem24 3.23%, prem168 2.24%, prem_chg 1.79%, rk_prem24 1.67%, rk_prem168 1.67%, prem_last 1.3%, rk_prem_last 0.95%, rk_prem_chg 0.73%

Premium coverage: 44.6% of the (symbol, hour) cells in the window.

- One comparison, same data and split as the v2 study: a gain here is a hypothesis, not proof.
- Premium files exist only for complete months of the archive: the newest weeks have no premium (left empty).
- A larger IC can still lose the money to costs: the 0.30% line decides.
