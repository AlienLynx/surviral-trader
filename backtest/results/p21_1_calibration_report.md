# Calibration cal-local

Simulator fed with the live bots' real decisions (no LLM, $0). before = legacy mechanics, after = live-like mechanics.

## Coverage of the live decisions

| bot | first full hour | last hour | decisions | hours covered / span | pairs | restarts | outage hours |
|---|---|---|---|---|---|---|---|
| main | 2026-09-29T11:00:00Z | 2026-10-04T14:00:00Z | 1682 | 124/124 | 26 | 12 | 12 |
| shorts | 2026-09-29T11:00:00Z | 2026-10-04T14:00:00Z | 1786 | 124/124 | 29 | 12 | 12 |
| moonshot | 2026-09-29T11:00:00Z | 2026-10-04T14:00:00Z | 1351 | 124/124 | 10 | 12 | 12 |
| survival | 2026-09-29T11:00:00Z | 2026-10-04T14:00:00Z | 1690 | 124/124 | 26 | 12 | 12 |
| survival_shorts | 2026-09-29T11:00:00Z | 2026-10-04T14:00:00Z | 1782 | 124/124 | 29 | 12 | 12 |

## after (live-like mechanics)

| bot | live entries | replay entries | ratio | matched | entry dt min (med abs) | entry px diff % | exit px diff % | exit reason agree | return diff pp (med abs) | mean/trade live -> replay | accepted |
|---|---|---|---|---|---|---|---|---|---|---|---|
| main | 24 | 24 | 1.0 | 23 (0.958) | 0.0167 | 0.0629 | 0.1802 | 0.92 | 0.1728 | -2.4869 -> -2.6801 | True |
| shorts | 73 | 75 | 1.027 | 67 (0.918) | 0.0167 | 0.1108 | 0.1753 | 0.918 | 0.2716 | 0.3333 -> -0.0392 | True |
| moonshot | 9 | 15 | 1.667 | 7 (0.778) | 10.05 | 0.0 | 0.1382 | 1.0 | 0.1533 | -0.941 -> -0.8563 | False |
| survival | 31 | 32 | 1.032 | 30 (0.968) | 0.0333 | 0.0614 | 0.1803 | 0.914 | 0.1986 | -1.0225 -> -1.3901 | True |
| survival_shorts | 46 | 48 | 1.043 | 37 (0.804) | 0.0167 | 0.0486 | 0.0896 | 0.923 | 0.279 | 0.2876 -> 1.0087 | True |

## before (legacy mechanics)

| bot | live entries | replay entries | ratio | matched | entry dt min (med abs) | entry px diff % | exit px diff % | exit reason agree | return diff pp (med abs) | mean/trade live -> replay | accepted |
|---|---|---|---|---|---|---|---|---|---|---|---|
| main | 24 | 24 | 1.0 | 23 (0.958) | 3.4667 | 0.3089 | 0.2872 | 0.92 | 0.2507 | -2.4869 -> -1.9562 | True |
| shorts | 73 | 80 | 1.096 | 69 (0.945) | 0.8667 | 0.1462 | 0.3724 | 0.947 | 0.4132 | 0.3333 -> -0.121 | False |
| moonshot | 9 | 16 | 1.778 | 7 (0.778) | 10.6333 | 0.0 | 0.3155 | 1.0 | 0.5199 | -0.941 -> -0.0228 | False |
| survival | 31 | 33 | 1.065 | 29 (0.935) | 0.6667 | 0.0634 | 0.4169 | 0.882 | 0.2551 | -1.0225 -> -1.1002 | True |
| survival_shorts | 46 | 52 | 1.13 | 39 (0.848) | 0.4667 | 0.0588 | 0.1502 | 0.927 | 0.3252 | 0.2876 -> 1.2044 | False |

## Ablation: each mechanics fix alone

| bot | variant | count ratio | matched share | median abs return diff pp | exit agreement | exit-mix TV | accepted |
|---|---|---|---|---|---|---|---|
| main | legacy | 1.0 | 0.958 | 0.2507 | 0.92 | 0.0417 | True |
| main | decide_first | 1.042 | 0.958 | 0.2057 | 0.92 | 0.0583 | True |
| main | entry_delay | 1.0 | 0.958 | 0.2507 | 0.92 | 0.0417 | True |
| main | ft_stake | 1.0 | 0.958 | 0.2507 | 0.92 | 0.0417 | True |
| main | restart_reset | 1.0 | 0.958 | 0.2656 | 0.88 | 0.0 | True |
| main | fine_prices | 1.0 | 0.958 | 0.2507 | 0.92 | 0.0417 | True |
| main | fine_exits | 1.0 | 0.958 | 0.2507 | 0.92 | 0.0417 | True |
| main | hour_latency | 1.0 | 0.958 | 0.2873 | 0.96 | 0.0417 | True |
| main | exit_extra | 1.0 | 0.958 | 0.2873 | 0.96 | 0.0417 | True |
| main | limit_fill | 0.958 | 0.917 | 0.2443 | 0.958 | 0.0254 | True |
| main | poll_exits | 1.0 | 0.958 | 0.2507 | 0.96 | 0.0417 | True |
| main | live_like | 1.0 | 0.958 | 0.1728 | 0.92 | 0.0417 | True |
| shorts | legacy | 1.096 | 0.945 | 0.4132 | 0.947 | 0.0624 | False |
| shorts | decide_first | 1.082 | 0.973 | 0.275 | 0.935 | 0.0527 | True |
| shorts | entry_delay | 1.096 | 0.945 | 0.4132 | 0.947 | 0.0624 | False |
| shorts | ft_stake | 1.096 | 0.945 | 0.4132 | 0.947 | 0.0624 | False |
| shorts | restart_reset | 1.014 | 0.89 | 0.4963 | 0.901 | 0.1233 | False |
| shorts | fine_prices | 1.096 | 0.945 | 0.4132 | 0.947 | 0.0624 | False |
| shorts | fine_exits | 1.096 | 0.945 | 0.4142 | 0.933 | 0.0541 | False |
| shorts | hour_latency | 1.096 | 0.945 | 0.4132 | 0.933 | 0.0751 | False |
| shorts | exit_extra | 1.096 | 0.945 | 0.4132 | 0.933 | 0.0751 | False |
| shorts | limit_fill | 1.082 | 0.932 | 0.3818 | 0.946 | 0.0655 | False |
| shorts | poll_exits | 1.096 | 0.945 | 0.4132 | 0.947 | 0.0624 | False |
| shorts | live_like | 1.027 | 0.918 | 0.2716 | 0.918 | 0.0772 | True |
| moonshot | legacy | 1.778 | 0.778 | 0.5199 | 1.0 | 0.0808 | False |
| moonshot | decide_first | 1.778 | 0.778 | 0.5199 | 1.0 | 0.0808 | False |
| moonshot | entry_delay | 1.778 | 0.778 | 0.3887 | 1.0 | 0.0808 | False |
| moonshot | ft_stake | 1.778 | 0.778 | 0.5199 | 1.0 | 0.0808 | False |
| moonshot | restart_reset | 1.889 | 0.889 | 0.5073 | 1.0 | 0.0278 | False |
| moonshot | fine_prices | 1.778 | 0.778 | 0.5199 | 1.0 | 0.0808 | False |
| moonshot | fine_exits | 1.778 | 0.778 | 0.5199 | 1.0 | 0.0808 | False |
| moonshot | hour_latency | 1.778 | 0.778 | 0.4068 | 1.0 | 0.0808 | False |
| moonshot | exit_extra | 1.778 | 0.778 | 0.4068 | 1.0 | 0.0808 | False |
| moonshot | limit_fill | 1.778 | 0.778 | 0.4037 | 1.0 | 0.0808 | False |
| moonshot | poll_exits | 1.778 | 0.778 | 0.2093 | 1.0 | 0.0808 | False |
| moonshot | live_like | 1.667 | 0.778 | 0.1533 | 1.0 | 0.0444 | False |
| survival | legacy | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | decide_first | 1.032 | 0.935 | 0.2107 | 0.882 | 0.0565 | True |
| survival | entry_delay | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | ft_stake | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | restart_reset | 1.0 | 0.903 | 0.2415 | 0.879 | 0.129 | True |
| survival | fine_prices | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | fine_exits | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | hour_latency | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | exit_extra | 1.065 | 0.935 | 0.2551 | 0.882 | 0.1095 | True |
| survival | limit_fill | 1.032 | 0.935 | 0.258 | 0.941 | 0.0877 | True |
| survival | poll_exits | 1.065 | 0.935 | 0.2729 | 0.853 | 0.1095 | True |
| survival | live_like | 1.032 | 0.968 | 0.1986 | 0.914 | 0.0877 | True |
| survival_shorts | legacy | 1.13 | 0.848 | 0.3252 | 0.927 | 0.067 | False |
| survival_shorts | decide_first | 1.109 | 0.848 | 0.2867 | 0.927 | 0.0665 | True |
| survival_shorts | entry_delay | 1.13 | 0.848 | 0.3149 | 0.927 | 0.067 | False |
| survival_shorts | ft_stake | 1.13 | 0.848 | 0.3252 | 0.927 | 0.067 | False |
| survival_shorts | restart_reset | 1.065 | 0.783 | 0.4442 | 0.921 | 0.0657 | False |
| survival_shorts | fine_prices | 1.13 | 0.848 | 0.3252 | 0.927 | 0.067 | False |
| survival_shorts | fine_exits | 1.13 | 0.848 | 0.3252 | 0.927 | 0.067 | False |
| survival_shorts | hour_latency | 1.13 | 0.848 | 0.3309 | 0.927 | 0.067 | False |
| survival_shorts | exit_extra | 1.13 | 0.848 | 0.3309 | 0.927 | 0.067 | False |
| survival_shorts | limit_fill | 1.13 | 0.848 | 0.3255 | 0.927 | 0.067 | False |
| survival_shorts | poll_exits | 1.13 | 0.848 | 0.3149 | 0.927 | 0.067 | False |
| survival_shorts | live_like | 1.043 | 0.804 | 0.279 | 0.923 | 0.0652 | True |

## Divergence causes (after)

- main: only in live {'replay_pair_already_open': 1}; only in replay {'live_pair_already_open': 1}
- shorts: only in live {'replay_pair_already_open': 3, 'replay_opened_but_not_matched': 1, 'replay_slots_full': 2}; only in replay {'live_pair_already_open': 5, 'live_no_entry_unexplained': 2, 'live_slots_full': 1}
- moonshot: only in live {'replay_pair_already_open': 2}; only in replay {'live_pair_already_open': 3, 'live_entry_order_timed_out': 2, 'live_no_entry_unexplained': 3}
- survival: only in live {'replay_slots_full': 1}; only in replay {'live_pair_already_open': 2}
- survival_shorts: only in live {'replay_pair_already_open': 5, 'replay_slots_full': 4}; only in replay {'live_no_entry_unexplained': 10, 'live_slots_full': 1}

## Limits

- decisions come from the bots' own log files: they cover only the period since the logs were last rotated / the containers recreated; a bot restart can log one hour twice
- a failed LLM call is logged as a 'hold' ("no LLM response"): during an outage the live bots only held, and rotation closed their positions -- the report shows the outage hours and a second set of numbers with trades opened or closed in them excluded
- the live stake was scaled by the allocator's weight (enforce); the simulator uses weight 1.0, so stake sizes (not entries or exits) differ -- see stake_ratio_median
- ROI / stoploss are checked on hourly candle high/low in the simulator, continuously (ticker) in the live bot; custom_exit once per candle in both
- positions open when the window starts are carried over with their real open time, rate and stake; the strategy's in-memory stale-signal streak is not recoverable
- the pair list per hour is the set of pairs the live bot logged a decision for in that hour (log order = entry order)
- no strategy parameter is changed anywhere in this run; mechanics fixes are listed in `variants` with before / after numbers
