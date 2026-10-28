# Listing Fade study lf-real-150641

**Rule-based historical study, $0, no LLM. A positive result here is a hypothesis for the live dry-run, not proof; `edge` is never given by replay code.**

## Verdict: `no edge`

Basis: slip0.3_slots_live_flagged_excluded. Failed conditions: CI lower bound <= 0, first half mean <= 0, second half mean <= 0. Opportunity set (unlimited slots): `no edge`.

`Listing Fade study lf-real-150641: verdict no edge (0.3% slippage, live limits 3 slots, liquidity-flagged excluded): n=557, mean -0.1731%, CI95 [-0.7316, 0.3417], halves -0.0238% / -0.3167%; opportunity set (unlimited slots): no edge, n=563, mean -0.148%. 779 listings (183 delisted included). Survivorship bias reduced, not removed.`

## Rules (read from the live files, nothing re-tuned)

```
{
 "strategy_sha256": "c49e352c3cf3eedddeda5d9df34a4480e395314b515ffdcf1b76cdeb123ce1b1",
 "config_sha256": "f6ecd6de84bc5d084789d86bfb2aadd6a62ee2e5b7df8917666cb43b0dd7dff6",
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

## Universe and survivorship

{
 "listings_found": 779,
 "currently_trading": 596,
 "delisted_closed_included": 183,
 "with_candle_data": 775,
 "without_candle_data": 4,
 "events_with_a_valid_signal": 586,
 "warning": "Survivorship bias is REDUCED, not removed: Bybit's API lists delisted perpetuals under status=Closed and they are included, but a symbol Bybit removed from the API altogether (or listed and delisted without a Closed record) is invisible; the number of such symbols is unknown. Listings without candle data are counted above, not silently dropped."
}

Liquidity-flagged events (stake >= 1% of first-hour volume): 34

## Scenarios

| scenario | trades | mean % | median % | win % | CI95 % | net $ | max DD % | first half % | second half % | worst stop gap % | verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| slip0_slots_live_flagged_excluded | 557 | 0.467 | 1.9562 | 80.4 | [-0.0882, 0.9716] | 65.025 | 18.03 | 0.5166 | 0.4192 | 16.018 | unproven |
| slip0_slots_live_flagged_included | 577 | 0.4815 | 1.9731 | 80.6 | [-0.0167, 0.9891] | 69.457 | 15.805 | 0.569 | 0.3937 | 16.018 | unproven |
| slip0_slots_unlimited_flagged_excluded | 563 | 0.4842 | 1.9729 | 80.5 | [-0.0474, 1.0054] | 68.147 | 17.974 | 0.5222 | 0.4484 | 16.018 | unproven |
| slip0_slots_unlimited_flagged_included | 583 | 0.522 | 1.9731 | 80.8 | [0.0101, 1.043] | 76.076 | 15.759 | 0.574 | 0.4708 | 16.018 | edge candidate |
| slip0.1_slots_live_flagged_excluded | 557 | 0.2485 | 1.7964 | 79.2 | [-0.3091, 0.7677] | 34.61 | 20.063 | 0.3338 | 0.1666 | 16.018 | unproven |
| slip0.1_slots_live_flagged_included | 577 | 0.2641 | 1.8399 | 79.4 | [-0.2427, 0.7857] | 38.1 | 20.399 | 0.3856 | 0.1422 | 16.018 | unproven |
| slip0.1_slots_unlimited_flagged_excluded | 563 | 0.2787 | 1.8142 | 79.2 | [-0.251, 0.8012] | 39.224 | 20.446 | 0.3393 | 0.2216 | 16.018 | unproven |
| slip0.1_slots_unlimited_flagged_included | 583 | 0.3169 | 1.8399 | 79.6 | [-0.1936, 0.8311] | 46.183 | 19.646 | 0.3906 | 0.2444 | 16.018 | unproven |
| slip0.3_slots_live_flagged_excluded | 557 | -0.1731 | 1.5307 | 77.7 | [-0.7316, 0.3417] | -24.11 | 37.392 | -0.0238 | -0.3167 | 16.018 | no edge |
| slip0.3_slots_live_flagged_included | 577 | -0.1518 | 1.5322 | 78.0 | [-0.6729, 0.3689] | -21.895 | 37.399 | 0.0267 | -0.3308 | 16.018 | no edge |
| slip0.3_slots_unlimited_flagged_excluded | 563 | -0.148 | 1.5419 | 77.8 | [-0.7115, 0.3699] | -20.834 | 35.434 | -0.0294 | -0.2597 | 16.018 | no edge |
| slip0.3_slots_unlimited_flagged_included | 583 | -0.1003 | 1.585 | 78.2 | [-0.6285, 0.4082] | -14.617 | 33.663 | 0.0211 | -0.2196 | 16.018 | no edge |
| slip0.5_slots_live_flagged_excluded | 557 | -0.48 | 1.2794 | 76.5 | [-1.053, 0.0411] | -66.846 | 69.775 | -0.4268 | -0.5313 | 16.018 | no edge |
| slip0.5_slots_live_flagged_included | 577 | -0.4275 | 1.2916 | 76.8 | [-0.9571, 0.0928] | -61.666 | 65.766 | -0.3751 | -0.4801 | 16.018 | no edge |
| slip0.5_slots_unlimited_flagged_excluded | 563 | -0.4676 | 1.2803 | 76.4 | [-1.0303, 0.0436] | -65.813 | 68.755 | -0.4324 | -0.5008 | 16.018 | no edge |
| slip0.5_slots_unlimited_flagged_included | 583 | -0.4221 | 1.2823 | 76.7 | [-0.958, 0.1054] | -61.528 | 65.376 | -0.3806 | -0.463 | 16.018 | no edge |

Signals that could NOT be simulated in the primary line (missing 5-minute data): {'no_data_at_entry': 1, 'events_without_5m_data': 1}

## Primary line: by month

| month | n | mean % | net $ | win % |
|---|---|---|---|---|
| 2024-01 | 6 | 0.2235 | 0.335 | 66.7 |
| 2024-02 | 9 | -2.119 | -4.768 | 66.7 |
| 2024-03 | 17 | 0.7398 | 3.144 | 82.4 |
| 2024-04 | 14 | 1.0598 | 3.709 | 85.7 |
| 2024-05 | 11 | -2.7525 | -7.569 | 63.6 |
| 2024-06 | 18 | 1.4328 | 6.448 | 83.3 |
| 2024-07 | 15 | -0.3935 | -1.476 | 80.0 |
| 2024-08 | 13 | 1.0745 | 3.492 | 84.6 |
| 2024-09 | 11 | -3.0985 | -8.521 | 63.6 |
| 2024-10 | 16 | 2.277 | 9.108 | 87.5 |
| 2024-11 | 28 | -1.7337 | -12.136 | 60.7 |
| 2024-12 | 23 | -1.9978 | -11.487 | 69.6 |
| 2025-01 | 32 | 0.9943 | 7.955 | 87.5 |
| 2025-02 | 14 | 0.5705 | 1.997 | 78.6 |
| 2025-03 | 17 | 2.633 | 11.19 | 94.1 |
| 2025-04 | 15 | 0.2366 | 0.887 | 86.7 |
| 2025-05 | 20 | -1.0582 | -5.291 | 75.0 |
| 2025-06 | 15 | -1.0685 | -4.007 | 73.3 |
| 2025-07 | 20 | 1.0975 | 5.487 | 90.0 |
| 2025-08 | 22 | -0.4753 | -2.614 | 81.8 |
| 2025-09 | 28 | 0.5529 | 3.87 | 85.7 |
| 2025-10 | 29 | -1.5438 | -11.192 | 65.5 |
| 2025-11 | 13 | -0.1429 | -0.464 | 84.6 |
| 2025-12 | 9 | 0.9441 | 2.124 | 77.8 |
| 2026-01 | 16 | 2.0676 | 8.271 | 87.5 |
| 2026-02 | 4 | 0.9656 | 0.966 | 75.0 |
| 2026-03 | 8 | 0.0954 | 0.191 | 75.0 |
| 2026-04 | 5 | 2.6146 | 3.268 | 100.0 |
| 2026-05 | 9 | 3.8655 | 8.697 | 100.0 |
| 2026-06 | 25 | -1.2497 | -7.811 | 76.0 |
| 2026-07 | 33 | -0.0831 | -0.686 | 81.8 |
| 2026-08 | 23 | -2.3164 | -13.319 | 56.5 |
| 2026-09 | 15 | -3.6621 | -13.733 | 53.3 |
| 2026-10 | 4 | -0.1766 | -0.177 | 75.0 |

## Primary line: by listing age at entry

| age | n | mean % | net $ |
|---|---|---|---|
| 2-5d | 118 | -0.0197 | -0.581 |
| 5-10d | 89 | -0.9925 | -22.083 |
| 10-20d | 62 | -1.1628 | -18.023 |
| 20-30d | 40 | -1.6824 | -16.824 |

## Primary line: worst 5 trades

- CFG/USDT:USDT 2026-03-19T01:00:00Z -> 2026-03-20T04:25:00Z: -13.432% (stop_loss, gap 0.0%)
- ZBCN/USDT:USDT 2024-04-19T03:00:00Z -> 2024-04-19T21:25:00Z: -13.351% (stop_loss, gap 0.0%)
- AEVO/USDT:USDT 2024-03-24T08:00:00Z -> 2024-03-27T03:25:00Z: -12.651% (stop_loss, gap 0.0%)
- ORDER/USDT:USDT 2024-09-02T19:00:00Z -> 2024-09-04T15:45:00Z: -12.488% (stop_loss, gap 0.0%)
- AWE/USDT:USDT 2025-05-31T04:00:00Z -> 2025-06-01T16:35:00Z: -12.417% (stop_loss, gap 0.0%)

## Against the live dry-run trades

{
 "available": true,
 "period": [
  "2026-09-25T15:30:38Z",
  "2026-10-05T03:10:08Z"
 ],
 "n_live": 73,
 "n_replay": 7,
 "matched": 1,
 "only_live": 72,
 "only_replay": 6,
 "live_mean_trade_pct": 0.6209,
 "replay_mean_trade_pct": -1.3332,
 "per_trade_diff_pp": {
  "median": -0.9904,
  "median_abs": 0.9904,
  "mean": -0.9904
 },
 "exit_reason_agreement": 1.0,
 "live_exit_reasons": {
  "stop_loss": 17,
  "trailing_stop_loss": 56
 },
 "replay_exit_reasons": {
  "stop_loss": 2,
  "trailing_stop_loss": 5
 },
 "note": "replay uses taker fees 0.055% / side and the slippage scenario of the primary line; the live dry-run uses the exchange fee of the dry-run wallet (0.01%) and fills at book prices",
 "like_for_like": {
  "available": true,
  "period": [
   "2026-09-25T15:30:38Z",
   "2026-10-05T03:10:08Z"
  ],
  "n_live": 73,
  "n_replay": 26,
  "matched": 7,
  "only_live": 66,
  "only_replay": 19,
  "live_mean_trade_pct": 0.6209,
  "replay_mean_trade_pct": 1.1492,
  "per_trade_diff_pp": {
   "median": -0.0562,
   "median_abs": 1.0655,
   "mean": -0.388
  },
  "exit_reason_agreement": 1.0,
  "live_exit_reasons": {
   "stop_loss": 17,
   "trailing_stop_loss": 56
  },
  "replay_exit_reasons": {
   "stop_loss": 3,
   "trailing_stop_loss": 23
  },
  "setup": "same rules and dates, re-entry after an exit allowed, 3 slots, dry-run fee 0.01%, slippage 0 (the live dry-run fills at book prices)"
 },
 "like_for_like_live_pairs": {
  "available": true,
  "period": [
   "2026-09-25T15:30:38Z",
   "2026-10-05T03:10:08Z"
  ],
  "n_live": 73,
  "n_replay": 81,
  "matched": 40,
  "only_live": 33,
  "only_replay": 41,
  "live_mean_trade_pct": 0.6209,
  "replay_mean_trade_pct": 0.4418,
  "per_trade_diff_pp": {
   "median": -0.1158,
   "median_abs": 1.2457,
   "mean": -1.1048
  },
  "exit_reason_agreement": 1.0,
  "live_exit_reasons": {
   "stop_loss": 17,
   "trailing_stop_loss": 56
  },
  "replay_exit_reasons": {
   "stop_loss": 16,
   "trailing_stop_loss": 65
  },
  "setup": "as like_for_like, restricted to the pairs the live bot actually traded: this removes the effect of the live pair list (VolumePairList top 200, SpreadFilter), which the replay cannot rebuild and which decides which pairs compete for the 3 slots"
 }
}

## Limits

- VolumePairList (top 200 by quote volume): the historical ranking of all pairs is not public
- SpreadFilter (max_spread_ratio 0.005): the historical order book is not public
- the live entry is a passive limit order that can expire unfilled (not modelled; the slippage scenarios stand in for it)
- funding history granularity is whatever Bybit returns (8 h, some symbols 4 h / 1 h)
- survivorship bias reduced, not removed (see universe.warning)
