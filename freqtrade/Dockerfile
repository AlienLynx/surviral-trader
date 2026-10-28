FROM freqtradeorg/freqtrade:2026.8

# Portainer's git-deployed stacks don't materialize repo files into
# the bind-mount host folders it manages -- it only auto-creates
# empty directories for them. So config.json and the strategy file
# get baked into the image at build time instead of relying on a
# volume mount that would just be empty.
COPY user_data/config.json /freqtrade/user_data/config.json
COPY user_data/strategies/ /freqtrade/user_data/strategies/
COPY user_data/pairlists/ /freqtrade/user_data/pairlists/

# This image runs as the non-root "ftuser". A host bind-mount always
# carries the HOST directory's ownership (root, since Docker/Portainer
# created it) regardless of what's set here -- that caused a
# PermissionError on the trade database. Named Docker volumes behave
# differently: on first use, Docker seeds them from the image's
# existing content *and ownership* at that path. So docker-compose.yml
# uses a named volume for the db directory, and this pre-creates it
# with the right owner so that seeding works correctly.
# CORS origins from the FREQTRADE_UI_HOST environment variable (no host name in the repository): see entrypoint-cors.sh
COPY entrypoint-cors.sh /freqtrade/entrypoint-cors.sh
USER root
RUN chmod +x /freqtrade/entrypoint-cors.sh
USER ftuser

RUN mkdir -p /freqtrade/user_data/db && \
    chown -R ftuser:ftuser /freqtrade/user_data/db
