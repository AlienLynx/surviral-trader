# Kronos evaluation keval-20261010T090000Z-14400

**Verdict: no edge** (net of 0.8% round-trip cost, 4h horizon, per rule fixed in advance). Signal skill before costs: **unproven**.

Model: NeoQuasar/Kronos-small (24.7M, context 512, 3 samples, seed 12345). Window 2026-09-10T09:00:00Z .. 2026-10-10T09:00:00Z (30.0 of 30 days), 20 pairs.
Signals: {'up': 2862, 'down': 6673, 'neutral': 4865, 'skipped': 0}. Threshold |ret_4h| >= 0.5%.

## Signal quality (independent of exits)

Forward return after the signal, direction-adjusted; excess = minus the mean of all pairs at the same hour; CI = bootstrap over calendar days.

| direction | horizon | n | forward mean % | hit % | gross excess % [95% CI] | net excess % [95% CI] | half 1 net % (n) | half 2 net % (n) |
|---|---|---|---|---|---|---|---|---|
| up | 1h | 2862 | 0.0329 | 51.1 | -0.0197 [-0.049, 0.0088] | -0.8197 [-0.849, -0.7912] | -0.8255 (1623) | -0.812 (1239) |
| up | 4h | 2862 | 0.1954 | 52.5 | -0.0558 [-0.1553, 0.0359] | -0.8558 [-0.9553, -0.7641] | -0.8934 (1623) | -0.8065 (1239) |
| up | 24h | 2862 | 1.2994 | 59.4 | -0.2579 [-0.5686, 0.0802] | -1.0579 [-1.3686, -0.7198] | -1.0513 (1623) | -1.0666 (1239) |
| **up** | verdict | | | | gross: unproven | net: **no edge** | | |
| down | 1h | 6673 | -0.069 | 49.8 | -0.0161 [-0.0344, 0.0013] | -0.8161 [-0.8344, -0.7987] | -0.812 (3264) | -0.8201 (3409) |
| down | 4h | 6673 | -0.2329 | 49.1 | -0.0583 [-0.1214, -0.0014] | -0.8583 [-0.9214, -0.8014] | -0.8623 (3264) | -0.8545 (3409) |
| down | 24h | 6673 | -1.5451 | 46.6 | -0.3437 [-0.6589, -0.091] | -1.1437 [-1.4589, -0.891] | -1.1019 (3264) | -1.1837 (3409) |
| **down** | verdict | | | | gross: no edge | net: **no edge** | | |

## Replay engine: Kronos as the decision source (same exits, fees, slippage)

| bot | decisions | trades | win % | mean/trade % | 95% CI % | net $ | max DD % | vs random (pct) |
|---|---|---|---|---|---|---|---|---|
| main / kronos | 14380 | 600 | 37.0 | -0.5516 | [-0.7945, -0.2924] | -87.645 | 19.525 | 0.5 |
| main / rule | 14400 | 43 | 30.2 | -0.8125 | [-2.9147, 1.3337] | -15.471 | 6.74 | 14.5 |
| shorts / kronos | 13661 | 673 | 32.1 | -1.5118 | [-1.7949, -1.215] | -211.782 | 42.247 | 21.5 |
| shorts / rule | 13680 | 44 | 22.7 | -1.8966 | [-3.6424, 0.1221] | -32.686 | 6.491 | 8.5 |

BTC buy-and-hold over the window: 6.068%

LLM replay runs found (labelled, other windows/models):
- 20261005-084124 main/named deepseek:deepseek-flash: 140 trades, mean -0.2882% window 2026-09-03..2026-10-03
- 20261005-084124 shorts/named deepseek:deepseek-flash: 159 trades, mean 0.5732% window 2026-09-03..2026-10-03

## Notes

- signals are made from candles up to t0 - 1h only; forward returns start at the close of that candle
- the live `shorts` strategy is the stand-in for shorts_v2 here (same exits; v2 differs only in the model prompt, which Kronos replaces)
- verdict is a label of the signal study: `edge candidate` means worth a live dry-run, never proven; replay never gives `edge`
- the Kronos training data are not documented: a positive result on recent history can still be partly memory
