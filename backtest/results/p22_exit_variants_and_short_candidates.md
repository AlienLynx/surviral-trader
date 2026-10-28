# P22 follow-up: exit variants (C) and the no-LLM rule (D) on run 20261005-084124, and the short-candidate check behind A2

Source run: window 2026-09-03 08:00 .. 2026-10-03 08:00 UTC, 13 pairs, step 2 candles, DeepSeek flash (named mode) + the strategy's own no-LLM rule, fee 0.1% + slippage 0.05% per side, no funding served (the run predates that).
Exit variants re-run the SAME recorded decisions with other exits (frozen decisions, $0, no model call). Run on the operator's PC (`fz-20261006-132120`). Nothing was tuned and no variant is adopted: this is an exploration with 9 variants x 4 rows = 36 looks, so one
positive CI among them is expected by chance. The frozen note applies: the effect of results on the prompt (`recent performance`) is not recomputed.

The `baseline` row reproduces the source run (main named 140 trades -0.29%, shorts named 159 trades +0.58%), which is the check that the frozen re-run is faithful.

| variant | bot | decisions | trades | mean/trade % | 95% CI % | net $ | max DD % | pct vs random | exits |
|---|---|---|---|---|---|---|---|---|---|
| baseline | main | LLM | 140 | -0.2946 | [-0.9484, 0.3829] | -14.958 | 6.036 | 9.0 | {'roi': 10, 'stop_loss': 27, 'rotate_stale_signal': 103} |
| baseline | main | rule (no LLM) | 75 | 0.6988 | [0.1341, 1.3156] | 17.335 | 1.985 | 61.5 | {'rotate_stale_signal': 74, 'roi': 1} |
| baseline | shorts | LLM | 159 | 0.5793 | [-0.1024, 1.303] | 25.189 | 4.297 | 70.5 | {'roi': 18, 'rotate_stale_signal': 116, 'stop_loss': 25} |
| baseline | shorts | rule (no LLM) | 140 | -0.2484 | [-0.6224, 0.1202] | -12.487 | 4.912 | 60.0 | {'rotate_stale_signal': 136, 'stop_loss': 4} |
| no_rotation | main | LLM | 51 | 0.8203 | [-1.2651, 2.8896] | 13.37 | 4.26 | 64.0 | {'roi': 20, 'stop_loss': 31} |
| no_rotation | main | rule (no LLM) | 31 | 0.7304 | [-1.7303, 3.2599] | 8.315 | 6.406 | 56.5 | {'roi': 12, 'stop_loss': 19} |
| no_rotation | shorts | LLM | 74 | 2.0117 | [0.2024, 3.8624] | 39.334 | 5.025 | 98.5 | {'roi': 35, 'stop_loss': 39} |
| no_rotation | shorts | rule (no LLM) | 30 | -0.635 | [-2.8923, 1.9112] | -8.725 | 5.074 | 39.5 | {'roi': 9, 'stop_loss': 21} |
| hold0 | main | LLM | 146 | -0.2143 | [-0.8395, 0.4026] | -11.067 | 5.282 | 10.5 | {'roi': 10, 'stop_loss': 27, 'rotate_stale_signal': 109} |
| hold0 | main | rule (no LLM) | 76 | 0.7315 | [0.1916, 1.2939] | 18.268 | 1.729 | 60.5 | {'rotate_stale_signal': 76} |
| hold0 | shorts | LLM | 166 | 0.5467 | [-0.0647, 1.1928] | 25.518 | 4.178 | 72.5 | {'roi': 17, 'rotate_stale_signal': 127, 'stop_loss': 22} |
| hold0 | shorts | rule (no LLM) | 145 | -0.2477 | [-0.5867, 0.0959] | -12.457 | 4.699 | 65.0 | {'rotate_stale_signal': 142, 'stop_loss': 3} |
| hold12 | main | LLM | 123 | 0.1134 | [-0.6995, 0.9353] | 0.602 | 4.777 | 31.0 | {'roi': 12, 'stop_loss': 27, 'rotate_stale_signal': 84} |
| hold12 | main | rule (no LLM) | 70 | 0.4826 | [-0.2537, 1.2689] | 8.787 | 3.763 | 54.0 | {'rotate_stale_signal': 65, 'roi': 1, 'stop_loss': 4} |
| hold12 | shorts | LLM | 138 | 0.7795 | [-0.004, 1.582] | 29.656 | 4.866 | 82.0 | {'roi': 21, 'rotate_stale_signal': 86, 'stop_loss': 31} |
| hold12 | shorts | rule (no LLM) | 121 | -0.3765 | [-0.8909, 0.108] | -18.397 | 6.21 | 54.5 | {'rotate_stale_signal': 109, 'stop_loss': 11, 'roi': 1} |
| hold24 | main | LLM | 110 | 0.2248 | [-0.7047, 1.1655] | 3.434 | 5.454 | 37.0 | {'roi': 15, 'stop_loss': 34, 'rotate_stale_signal': 61} |
| hold24 | main | rule (no LLM) | 61 | 0.5541 | [-0.4263, 1.5286] | 10.112 | 2.346 | 55.0 | {'rotate_stale_signal': 51, 'roi': 3, 'stop_loss': 7} |
| hold24 | shorts | LLM | 125 | 0.8013 | [-0.1813, 1.7545] | 19.37 | 4.864 | 80.0 | {'roi': 23, 'rotate_stale_signal': 63, 'stop_loss': 39} |
| hold24 | shorts | rule (no LLM) | 97 | -0.3927 | [-1.1159, 0.358] | -20.429 | 6.606 | 54.5 | {'rotate_stale_signal': 79, 'stop_loss': 15, 'roi': 3} |
| stop3 | main | LLM | 168 | -0.3346 | [-0.8443, 0.1961] | -18.008 | 6.61 | 11.0 | {'roi': 10, 'stop_loss': 65, 'rotate_stale_signal': 93} |
| stop3 | main | rule (no LLM) | 75 | 0.6786 | [0.098, 1.2989] | 17.009 | 1.96 | 81.5 | {'rotate_stale_signal': 71, 'roi': 1, 'stop_loss': 3} |
| stop3 | shorts | LLM | 180 | 0.5938 | [-0.0041, 1.2216] | 29.49 | 3.624 | 93.5 | {'roi': 20, 'stop_loss': 56, 'rotate_stale_signal': 104} |
| stop3 | shorts | rule (no LLM) | 140 | -0.204 | [-0.551, 0.14] | -10.726 | 4.569 | 71.0 | {'rotate_stale_signal': 125, 'stop_loss': 15} |
| stop8 | main | LLM | 124 | -0.3045 | [-1.0359, 0.4552] | -13.98 | 5.846 | 0.0 | {'roi': 9, 'stop_loss': 8, 'rotate_stale_signal': 107} |
| stop8 | main | rule (no LLM) | 75 | 0.6988 | [0.1341, 1.3156] | 17.335 | 1.985 | 9.0 | {'rotate_stale_signal': 74, 'roi': 1} |
| stop8 | shorts | LLM | 150 | 0.6694 | [-0.0582, 1.4227] | 27.505 | 4.251 | 20.0 | {'roi': 18, 'rotate_stale_signal': 124, 'stop_loss': 8} |
| stop8 | shorts | rule (no LLM) | 140 | -0.2127 | [-0.5725, 0.1471] | -10.813 | 4.586 | 32.0 | {'rotate_stale_signal': 140} |
| roi5 | main | LLM | 173 | -0.201 | [-0.7857, 0.333] | -11.473 | 5.385 | 10.0 | {'roi': 42, 'stop_loss': 35, 'rotate_stale_signal': 96} |
| roi5 | main | rule (no LLM) | 75 | 0.6413 | [0.1293, 1.1856] | 15.778 | 1.975 | 73.0 | {'roi': 9, 'rotate_stale_signal': 66} |
| roi5 | shorts | LLM | 206 | 0.4514 | [-0.0621, 0.9114] | 25.237 | 3.661 | 87.0 | {'roi': 61, 'rotate_stale_signal': 113, 'stop_loss': 32} |
| roi5 | shorts | rule (no LLM) | 140 | -0.2186 | [-0.5957, 0.1504] | -10.221 | 4.302 | 38.5 | {'roi': 8, 'rotate_stale_signal': 128, 'stop_loss': 4} |
| trail | main | LLM | 197 | -0.3782 | [-0.8616, 0.0962] | -19.68 | 6.738 | 4.5 | {'trailing_stop_loss': 66, 'roi': 4, 'stop_loss': 35, 'rotate_stale_signal': 92} |
| trail | main | rule (no LLM) | 75 | 0.7246 | [0.1896, 1.2917] | 16.917 | 1.956 | 61.5 | {'rotate_stale_signal': 68, 'trailing_stop_loss': 7} |
| trail | shorts | LLM | 241 | 0.3131 | [-0.1042, 0.769] | 19.918 | 4.131 | 53.0 | {'roi': 6, 'trailing_stop_loss': 97, 'rotate_stale_signal': 106, 'stop_loss': 32} |
| trail | shorts | rule (no LLM) | 140 | -0.1868 | [-0.5665, 0.1954] | -8.841 | 4.295 | 63.0 | {'rotate_stale_signal': 128, 'trailing_stop_loss': 8, 'stop_loss': 4} |

## What it says

- **Rotation (`rotate_stale_signal`) is the dominant exit** (103 of 140 main trades, 116 of 159 shorts trades). Without it, shorts + LLM: +2.01% per trade, CI [+0.20, +3.86], n=74, 98.5th percentile vs random -- the only row of 36 whose CI lower bound is above 0. With 36 looks that is not evidence; it is a hypothesis for a live dry-run variant. main + LLM without rotation: +0.82%, CI [-1.27, +2.89], n=51: nothing.
- **Tighter stop (-3%) hurts the LLM bots** (main: 65 stops, -0.33%); a looser one (-8%) changes little. A 5% ROI and the trailing stop do not help.
- **The no-LLM rule, run through the real exits** (rule decisions fed to the strategy, rotation on): main rule +0.70% per trade, CI [+0.13, +1.32], n=75; shorts rule -0.25%, CI [-0.62, +0.12], n=140. Different from the source run's rule rows (31 / 30 trades) because that run used the strategy's placeholder branch without rotation.
- The window is a strong up-trend for BTC (+8.9%), so long rules look better and short rules worse than they would in another regime.

## A2 proxy: what the bare short rule is worth on this window (old bidirectional prompt, same decisions)

Rule candidates for a short (24-candle change < -2% and volatility < 5%): **153 of 4680** decisions. The old prompt (buy / sell_short / hold) said hold on 149, buy on 3 and sell_short on 1: it vetoed 99.3% of them.
Forward return of a SHORT opened on every rule candidate (bare rule, no model): **1h -0.10%, 4h -0.32%, 24h -1.33%** (24h hit rate 23%). After a -2% day the pairs on average bounced, so the bare short rule loses; the old model's near-total veto was the right call in this window.
This is a proxy, not A2: the new prompt (confirm / veto, with funding) has not been run. With only 1 confirmed candidate there is no confirmed-vs-vetoed comparison (needs >= 20 vetoes AND confirms to read). The real A2 (shorts_v2 with the veto, ~153 model calls for this window and 13 pairs) is built and tested but needs the server's DeepSeek key.
