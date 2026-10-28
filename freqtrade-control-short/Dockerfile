# P17-C control group (control-short). Image is PINNED to the exact Freqtrade release
# the sample strategy was copied from -- deliberately not ":stable", so a
# Freqtrade upgrade can never silently change the baseline's behavior. See
# user_data/strategies/*.py header and AGENTS.md (P17-C).
FROM freqtradeorg/freqtrade:2026.8

# Config and strategy baked into the image (Portainer git-deployed stacks leave
# bind-mount folders empty -- same pattern as every other bot here).
COPY user_data/config.json /freqtrade/user_data/config.json
COPY user_data/strategies/ /freqtrade/user_data/strategies/

# Non-root ftuser + named volume for the db/log dir (see ../freqtrade/Dockerfile).
# CORS origins from the FREQTRADE_UI_HOST environment variable (no host name in the repository): see entrypoint-cors.sh
COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh
USER root
RUN chmod +x /freqtrade/entrypoint-cors.sh
USER ftuser

RUN mkdir -p /freqtrade/user_data/db &&     chown -R ftuser:ftuser /freqtrade/user_data/db
