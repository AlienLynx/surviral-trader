FROM freqtradeorg/freqtrade:2026.8

# Same pattern as ../freqtrade/Dockerfile -- config and strategy baked
# into the image at build time, because Portainer's git-deployed
# stacks leave bind-mount host folders empty instead of populating
# them from the repo (see ../AGENTS.md).
COPY user_data/config.json /freqtrade/user_data/config.json
COPY user_data/strategies/ /freqtrade/user_data/strategies/
COPY user_data/pairlists/ /freqtrade/user_data/pairlists/

# Non-root user, named volume needed for the trade db to avoid the
# PermissionError documented in ../AGENTS.md.
# CORS origins from the FREQTRADE_UI_HOST environment variable (no host name in the repository): see entrypoint-cors.sh
COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh
USER root
RUN chmod +x /freqtrade/entrypoint-cors.sh
USER ftuser

RUN mkdir -p /freqtrade/user_data/db && \
    chown -R ftuser:ftuser /freqtrade/user_data/db
