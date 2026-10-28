# kronos-replay

Generates HISTORICAL Kronos signals for the `/replay` page (P22 K2). Same model, pins, weights and adapter as `kronos-signal`, but it forecasts every hour of a past window instead of the present hour, and it talks to nothing:
survival-agent writes `job.json` and the candle files into the shared volume, this worker writes the signals back.

- **Idle by default:** no job = no model loaded, no CPU. A job runs until it is done or until the flag file `STOP` appears; writing the same job again (the card's Generate button) resumes it.
- **Weights are baked in** at build time at pinned revisions with a sha256 check (see `Dockerfile`, same pins as `kronos-signal`); nothing is downloaded at run time. The build stops if the Docker host has less than 3 GB free.
- **No look-ahead:** for as-of t0 the model sees only candles opened at or before t0 - 1h, the newest exactly t0 - 1h, no gap (`app/gen.py: window_for`).
- **Signal, fixed in advance:** up if predicted 4h return >= +0.5%, down if <= -0.5%, else neutral.
- **Cost:** about 10 s per inference on one CPU worker. 30 days x 24 h x 20 pairs = 14,400 inferences = about 38 h on this container; the page shows the estimate and a warning above 12 h.
- **Run by hand on a PC** (what was done first): `python -m app.worker --dir <out dir> --shard I --of N --threads 1 --once` with `KRONOS_MODEL_DIR`, `KRONOS_TOKENIZER_DIR`, `KRONOS_CODE_DIR` set; several shards write side by side.

Files: `app/gen.py` (pure logic), `app/worker.py` (loop, benchmark), `app/predictor.py` (model adapter, a copy of kronos-signal's), `Dockerfile`, `requirements.txt`, `NOTICE.md`. Tests: `backtest/test_p22_kronos_replay.py`.

Not verified: the image was not built here and the container was not run.
