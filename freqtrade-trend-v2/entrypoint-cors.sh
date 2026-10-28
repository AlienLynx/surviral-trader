#!/bin/sh
# Builds the CORS origin list of Freqtrade's REST API / FreqUI from FREQTRADE_UI_HOST (the host name or IP you open the bot UIs with) and starts freqtrade.
# Why a script: config.json is baked into the image and Freqtrade's FREQTRADE__API_SERVER__CORS_ORIGINS env var cannot reach the `CORS_origins` key (env keys are lower-cased),
# so the list is written to a second config file that is merged over config.json (a later --config wins). Identical copy in every bot directory.
host="${FREQTRADE_UI_HOST:-localhost}"
case "$host" in *[!A-Za-z0-9._-]*) echo "FREQTRADE_UI_HOST=$host has characters that are not allowed in a host name; using localhost" >&2; host=localhost ;; esac
origins=""
for port in 8099 8110 8120 8160 8170 8180 8190 8200; do
  origins="${origins}${origins:+,}\"http://${host}:${port}\""
done
printf '{"api_server":{"CORS_origins":[%s]}}\n' "$origins" > /tmp/cors_origins.json
exec freqtrade "$@" --config /tmp/cors_origins.json
