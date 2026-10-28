FROM python:3.11-slim

WORKDIR /app

# LightGBM (P25 walk-forward study) needs the OpenMP runtime, which the slim image lacks
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ /app/backend
COPY frontend/ /app/frontend
COPY config/ /app/config_defaults

# P21 replay: read-only copies of the LLM bots' strategy files (plus the config.json and auto_tune.json each strategy reads), used ONLY by the replay.
# The replay loads the REAL strategy code from here and swaps its data sources (see backend/replay/sandbox.py); nothing in the bots is edited.
COPY freqtrade/user_data/strategies/DeepSeekStrategy.py freqtrade/user_data/strategies/_second_opinion.py freqtrade/user_data/strategies/_allocator_ctx.py freqtrade/user_data/strategies/auto_tune.json freqtrade/user_data/config.json /app/strategies_src/main/
COPY freqtrade-shorts/user_data/strategies/DeepSeekShortsStrategy.py freqtrade-shorts/user_data/strategies/_second_opinion.py freqtrade-shorts/user_data/strategies/_allocator_ctx.py freqtrade-shorts/user_data/strategies/auto_tune.json freqtrade-shorts/user_data/config.json /app/strategies_src/shorts/
COPY freqtrade-moonshot/user_data/strategies/DeepSeekMoonshotStrategy.py freqtrade-moonshot/user_data/strategies/_second_opinion.py freqtrade-moonshot/user_data/strategies/_allocator_ctx.py freqtrade-moonshot/user_data/config.json /app/strategies_src/moonshot/
COPY freqtrade-listing-fade/user_data/strategies/ListingFadeStrategy.py freqtrade-listing-fade/user_data/strategies/_allocator_ctx.py freqtrade-listing-fade/user_data/config.json /app/strategies_src/listing_fade/
# P22-A1: the TRUE SHORT variant (replay only until the operator approves A3): strategy, allocator helper, config, auto_tune
COPY freqtrade-shorts-v2/user_data/strategies/DeepSeekShortsV2Strategy.py freqtrade-shorts-v2/user_data/strategies/_allocator_ctx.py freqtrade-shorts-v2/user_data/strategies/auto_tune.json freqtrade-shorts-v2/user_data/config.json /app/strategies_src/shorts_v2/

# P22 K2: the mount point of the kronos-replay volume (shared with the kronos-replay container, which runs as another user): world-writable so that either container may create the volume first
RUN mkdir -p /data/replay/kronos && chmod 777 /data/replay/kronos

ENV DATA_DIR=/data

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8088"]
