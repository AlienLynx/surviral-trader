FROM freqtradeorg/freqtrade:2026.8

COPY user_data/config.json /freqtrade/user_data/config.json
COPY user_data/strategies/ /freqtrade/user_data/strategies/

# CORS origins from the FREQTRADE_UI_HOST environment variable (no host name in the repository): see entrypoint-cors.sh
COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh
USER root
RUN chmod +x /freqtrade/entrypoint-cors.sh
USER ftuser

RUN mkdir -p /freqtrade/user_data/db && \
    chown -R ftuser:ftuser /freqtrade/user_data/db
