# Kronos evaluation keval-20260919T090000Z-7220

**Verdict: no edge** (net of 0.8% round-trip cost, 4h horizon, per rule fixed in advance). Signal skill before costs: **unproven**.

Model: NeoQuasar/Kronos-small (24.7M, context 512, 3 samples, seed 12345). Window 2026-09-04T08:00:00Z .. 2026-09-19T09:00:00Z (15.0 of 30 days, PARTIAL), 20 pairs.
Signals: {'up': 1488, 'down': 2234, 'neutral': 3498, 'skipped': 0}. Threshold |ret_4h| >= 0.5%.

## Signal quality (independent of exits)

Forward return after the signal, direction-adjusted; excess = minus the mean of all pairs at the same hour; CI = bootstrap over calendar days.

| direction | horizon | n | forward mean % | hit % | gross excess % [95% CI] | net excess % [95% CI] | half 1 net % (n) | half 2 net % (n) |
|---|---|---|---|---|---|---|---|---|
| up | 1h | 1488 | -0.0186 | 49.5 | -0.0283 [-0.0542, -0.0037] | -0.8283 [-0.8542, -0.8037] | -0.8275 (731) | -0.829 (757) |
| up | 4h | 1488 | -0.0133 | 48.5 | -0.0894 [-0.1837, 0.0024] | -0.8894 [-0.9837, -0.7976] | -0.8678 (731) | -0.9104 (757) |
| up | 24h | 1488 | 0.0933 | 52.0 | -0.4596 [-0.8336, -0.0615] | -1.2596 [-1.6336, -0.8615] | -1.1083 (731) | -1.4057 (757) |
| **up** | verdict | | | | gross: unproven | net: **no edge** | | |
| down | 1h | 2234 | -0.0457 | 50.4 | -0.0106 [-0.0397, 0.0186] | -0.8106 [-0.8397, -0.7814] | -0.7884 (1197) | -0.8362 (1037) |
| down | 4h | 2234 | -0.1627 | 47.5 | -0.0618 [-0.1663, 0.0519] | -0.8618 [-0.9663, -0.7481] | -0.7833 (1197) | -0.9524 (1037) |
| down | 24h | 2234 | -1.3419 | 47.1 | -0.4367 [-1.0707, 0.1458] | -1.2367 [-1.8707, -0.6542] | -0.8727 (1197) | -1.6569 (1037) |
| **down** | verdict | | | | gross: unproven | net: **no edge** | | |

## Replay engine: Kronos as the decision source (same exits, fees, slippage)

| bot | decisions | trades | win % | mean/trade % | 95% CI % | net $ | max DD % | vs random (pct) |
|---|---|---|---|---|---|---|---|---|
| main / kronos | 7200 | 296 | 29.7 | -0.9518 | [-1.2129, -0.6938] | -74.373 | 15.685 | 20.5 |
| main / rule | 7220 | 29 | 34.5 | -0.1399 | [-2.7116, 2.4565] | -6.262 | 7.457 | 66.0 |
| shorts / kronos | 6840 | 305 | 32.5 | -1.222 | [-1.5928, -0.854] | -93.419 | 20.233 | 91.0 |
| shorts / rule | 6859 | 30 | 30.0 | -0.876 | [-3.3837, 1.6467] | -14.853 | 3.932 | 58.5 |

BTC buy-and-hold over the window: 0.8%

LLM earlier runs: none in this REPLAY_DIR.

## Notes

- signals are made from candles up to t0 - 1h only; forward returns start at the close of that candle
- the live `shorts` strategy is the stand-in for shorts_v2 here
- verdict is a label of the signal study: `edge candidate` means worth a live dry-run, never proven; replay never gives `edge`
- the Kronos training data are not documented: a positive result on recent history can still be partly memory
- PARTIAL WINDOW: the generator has not finished
