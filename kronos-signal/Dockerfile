# kronos-signal: CPU-only hourly forecaster. Weights and model code are baked in at PINNED revisions, so the container needs no internet at run time
# (it only talks to the bots' REST inside the compose network). Check the disk first: sh kronos-signal/scripts/check_disk.sh
FROM python:3.13-slim

# Disk guard that runs during `docker compose build` itself (no script to run by hand): `df /` inside a build step reports the filesystem the build writes to,
# i.e. the Docker host's disk. It prints the free space in the build log and stops the build only when there is clearly not enough room (image ~2 GB + cache).
ARG KRONOS_MIN_FREE_GB=3
RUN free_kb=$(df -Pk / | awk 'NR==2 {print $4}') && free_gb=$((free_kb / 1024 / 1024))  && echo "kronos-signal build: ${free_gb} GB free on the build filesystem (needs ${KRONOS_MIN_FREE_GB} GB)"  && if [ "$free_gb" -lt "$KRONOS_MIN_FREE_GB" ]; then echo "NOT ENOUGH DISK for the kronos-signal image: free space with docker image prune -f / docker builder prune -f, then redeploy" >&2; exit 1; fi

ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 HF_HUB_DISABLE_TELEMETRY=1
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*

# CPU wheel of torch only (no CUDA libraries): ~0.8 GB instead of several GB
RUN pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# model code: shiyu-coder/Kronos (MIT), pinned commit; only model/ and LICENSE are kept
ARG KRONOS_COMMIT=67b630e67f6a18c9e9be918d9b4337c960db1e9a
RUN mkdir -p /opt/kronos && curl -fsSL "https://github.com/shiyu-coder/Kronos/archive/${KRONOS_COMMIT}.tar.gz" \
    | tar xz --strip-components=1 -C /opt/kronos --wildcards '*/model/*' '*/LICENSE'

# weights: NeoQuasar/Kronos-small + NeoQuasar/Kronos-Tokenizer-base (MIT), pinned revisions
ARG KRONOS_MODEL_REV=901c26c1332695a2a8f243eb2f37243a37bea320
ARG KRONOS_TOKENIZER_REV=0e0117387f39004a9016484a186a908917e22426
RUN python -c "from huggingface_hub import snapshot_download as s; \
s('NeoQuasar/Kronos-small', revision='${KRONOS_MODEL_REV}', local_dir='/models/small'); \
s('NeoQuasar/Kronos-Tokenizer-base', revision='${KRONOS_TOKENIZER_REV}', local_dir='/models/tokenizer')"

# the downloaded weights must be byte-identical to the ones measured during development
RUN echo "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020  /models/small/model.safetensors" | sha256sum -c -  && echo "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee  /models/tokenizer/model.safetensors" | sha256sum -c -

ENV HF_HUB_OFFLINE=1 KRONOS_MODEL_REV=${KRONOS_MODEL_REV} KRONOS_TOKENIZER_REV=${KRONOS_TOKENIZER_REV} KRONOS_CODE_COMMIT=${KRONOS_COMMIT} \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 KRONOS_OUT_DIR=/kronos

WORKDIR /srv
COPY app/ /srv/app/
COPY NOTICE.md /srv/NOTICE.md
RUN useradd -r -u 10001 kronos && mkdir -p /kronos && chown kronos /kronos
USER kronos
CMD ["python", "-m", "app.runner"]
