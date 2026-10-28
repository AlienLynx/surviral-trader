# kronos-signal

A CPU-only container that, once an hour, forecasts the next 24 hourly candles of the ~25 most liquid pairs with the open [Kronos](https://github.com/shiyu-coder/Kronos)
foundation model for candlestick series and writes one **signal** per pair into an append-only file. It **does not trade, does not control any bot, and is never managed** by the allocator.
The survival-agent's nightly audit reads the file, adds the signals to the P19 ledger as the source `kronos`, and scores them against the realised forward return like every other signal.

The question it answers: *does this model predict the direction of the next hours on our own pairs better than chance, after fees?* Until the audit says so (see "Criteria"), the answer is "unknown".

## What it uses (checked 04.10.2026)

| Item | Value |
|---|---|
| Model | `NeoQuasar/Kronos-small`, 24.7M parameters, context 512, weights file 98.98 MB, MIT license |
| Tokenizer | `NeoQuasar/Kronos-Tokenizer-base`, 15.8 MB, MIT license |
| Model revision (pinned) | `901c26c1332695a2a8f243eb2f37243a37bea320` (weights sha256 `b082dfcb...e020`, verified in the Docker build) |
| Tokenizer revision (pinned) | `0e0117387f39004a9016484a186a908917e22426` (sha256 `59d85f6a...6bee`) |
| Code | `shiyu-coder/Kronos`, MIT, commit `67b630e67f6a18c9e9be918d9b4337c960db1e9a` (downloaded at build time, only `model/` kept) |
| Other variants (not used) | `Kronos-mini` (4.1M, context 2048, tokenizer `Kronos-Tokenizer-2k`), `Kronos-base` (102M, 409 MB) |
| GPU needed? | No. The container installs the CPU wheel of torch (`2.14.1`) |

## Measured cost (on a DEVELOPMENT machine, not on the server)

Kronos-small, 480 input candles, 24 forecast candles, fixed seed, CPU only:

| CPU threads | samples | seconds per pair |
|---|---|---|
| 1 | 3 | 18.4 |
| 2 | 3 | **10.0** (the default) |
| 2 | 5 | 17.4 |
| 4 | 5 | 11.4 |

Resident memory about 420 MB for one pair (about 700 MB when batching 10 series at once, which gives no speed-up on CPU, so the service runs pair by pair). The forecast is deterministic for a fixed seed
(three consecutive runs gave identical numbers). At 25 pairs and the defaults the cycle is about 4-5 minutes on that machine; the server's CPU is unknown, so the cycle has a time budget
(`KRONOS_CYCLE_BUDGET_S`, 540 s): once it is used no new pair is started and the cycle is marked `truncated` (the most liquid pairs are always scored first). If it truncates every hour, lower
`KRONOS_MAX_PAIRS` or `KRONOS_SAMPLES`; do not extend the universe unless a cycle fits well inside 10 minutes.

The compose service is capped at 2 CPUs, 1 GB, a lowered CPU share and a process limit, so it cannot starve the bots. **Disk:** the image is about 1.5-2 GB. The Dockerfile checks the free space inside the build (`df /` in a build step shows the Docker host disk), prints it in the build log and stops the build only below `KRONOS_MIN_FREE_GB` (default 3). A failed build of this service makes the whole `compose up` fail visibly, which is intended (a half-filled disk is worse); free space with `docker image prune -f` and redeploy. `scripts/check_disk.sh` is an optional manual version of the same check.

## How a cycle works

1. At minute 2 of every hour (the 1h candle has closed), for t0 = that hour: ask each active bot's REST for `/whitelist` (the retired Dip bot is skipped), keep one pair per coin (spot preferred).
2. Fetch `/pair_candles` (1h) for each; rank by 24h quote turnover; take the top `KRONOS_MAX_PAIRS` (25).
3. For each pair: the model sees the last 480 **closed** candles (a candle opened at t0 or later is dropped, the newest must be the one opened at t0 - 1h, no gaps, otherwise the pair is skipped as `stale_candles`).
4. Forecast 24 candles (mean over `KRONOS_SAMPLES` parallel samples, `torch.manual_seed(12345)`), turn the forecast closes into expected returns at 1h / 4h / 24h.
5. Action from the 4h expectation: `buy` if `ret_4h - fee > threshold`, `sell_short` if `-ret_4h - fee > threshold`, else `hold` (fee = 0.2% round trip, threshold `KRONOS_THRESHOLD` = 0.5%, i.e. 0.5% **after** fees).
   `confidence = |ret_4h|`.
6. Append one JSON line per pair to `/kronos/kronos_signals.jsonl` (one record per pair and t0; a restart in the same hour writes nothing twice). A health summary (cycle seconds, pairs scored of candidates, skipped with reasons, `truncated`, model and revisions) goes to `/kronos/kronos_health.json`; nobody has to copy it by hand: survival-agent reads it live at `GET /api/kronos/health`, shows it on the signal-audit card, embeds it in `signals_audit.json` and puts it in the ZIP export (`signals/kronos_health.json`). The process also lowers its own priority (`nice 10`) so the bots win the CPU if the host is busy.

Record format (ledger format, `bot = "kronos"`):

```json
{"sid": "kronos|BTC/USDT|2026-10-05T14:00:00Z", "ts": "...", "t0": "2026-10-05T14:00:00Z", "bot": "kronos", "pair": "BTC/USDT",
 "action": "buy", "confidence": 0.012, "price": 100.0, "src_bot": "main",
 "kronos": {"ret_1h": 0.003, "ret_4h": 0.012, "ret_24h": 0.072, "threshold": 0.005, "fee_round_trip": 0.002, "samples": 3, "seed": 12345, "n_candles": 480, "...": "model + revision fields"}}
```

## P23: the per-bot rank file (what Main / Shorts / Moonshot read)

After every cycle the container also writes `kronos_rank.json` into its volume: for each bot, over the bot's OWN whitelist, `score = forecast ret_24h - mean forecast of those pairs`, the top third with score >= +0.3% is `buy`, the bottom third with score <= -0.3% is `sell_short`, anything else (and every pair when fewer than 8 are scored) is `hold`. This is the cross-sectional use of the model that the Kronos docs demonstrate (rank many instruments, take the top-K, neutralise the market). The universe is the union of the bots' whitelists (`KRONOS_MAX_PAIRS` 45, `KRONOS_CYCLE_BUDGET_S` 900). survival-agent copies the file to the allocator volume that the bots mount read-only; the bots decide with it per `<BOT>_DECISION_SOURCE` (see the README). The `kronos` ledger source above is unchanged (absolute threshold). On 15 historical days neither design showed a net edge; the rank design had a 24h rank correlation of +0.10 and a zero top-minus-bottom spread.

## Switching it off

`KRONOS_MODE=off` (compose variable) keeps the container idle: no model is loaded and nothing is written. Stopping the container is equally harmless: the audit simply sees no new signals.

## Criteria for any conclusion (a report label, not code that acts)

At least 200 actions per direction **and** 14 days of data. `edge` only if the lower bound of the 95% bootstrap CI of the excess return over the benchmark (mean of all pairs scored at the same t0)
is above zero at 4h or 24h **and** also in both halves of the period; `no_edge` if every upper bound is below zero; otherwise `unproven`. Only after that is any use as a filter for the bots discussed.
The audit reports this under `bots.kronos.verdict` in `signals_audit.json` (and the dashboard card), also in the ZIP export.

## Files

`app/core.py` (pure logic, tested offline with a fake forecaster), `app/predictor.py` (the only module that imports torch / Kronos), `app/runner.py` (hourly loop), `Dockerfile`, `requirements.txt`,
`scripts/check_disk.sh`, `NOTICE.md` (third-party licenses and pins). Tests: `backtest/test_p204_kronos.py`.

## Not verified here

The Docker image was **not built** and the container was **not run** in the environment where this was written (no Docker there). The model code and weights were run, outside Docker, on a development machine with
Python 3.13 and the pinned library versions (numpy 2.1.3, pandas 2.2.3: pandas 2.2.x crashes with numpy 2.5). The Linux CPU wheel of `torch==2.14.1` for Python 3.13 is assumed to exist on the PyTorch CPU index.
The first real cycle time, memory and the share of pairs whose candles pass the freshness check will be visible in `/kronos/kronos_health.json` after deployment.
