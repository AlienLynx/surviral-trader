# Security

Survival Agent is a **paper-trading research system**. It never places a real order, but it controls Freqtrade bots, holds API keys for LLM providers in its environment, and runs a web
dashboard. Read this before you deploy it anywhere other than a private machine.

## The dashboard has no authentication

The survival-agent web app (port **3446**) and every Freqtrade bot UI (ports 8099 to 8200) are meant for a **private network only**. Anyone who can reach port 3446 can:

- change thresholds and switches (allocator mode and policy, the ranker, the Fade re-entry cooldown),
- pause and resume the bots through the allocator,
- start LLM replays that cost money (capped by `REPLAY_MAX_USD`) and long CPU jobs,
- download the export bundle (logs, trades, ledgers). Secrets are masked in it, but treat every bundle as sensitive.

**Never publish these ports to the internet.** Put the host behind a VPN (WireGuard, Tailscale) or an authenticating reverse proxy (Caddy / nginx with basic auth or an identity-aware proxy), and
keep the firewall closed to everything else.

## Secrets

- Secrets live only in the environment of your container platform (Portainer stack variables, or a local `.env` that is git-ignored). No file tracked by git contains a credential; the
  `config.json` files hold placeholders such as `set-via-credentials-panel`.
- Choose a long random `FREQTRADE_API_PASSWORD` and `FREQTRADE_API_JWT_SECRET`. All bots share this login, and so do the allocator and the collectors. A weak JWT secret can be brute-forced from a
  captured token.
- Exchange keys (`BYBIT_*_API_KEY` / `_SECRET`) are only needed if you deliberately take a bot live. Use sub-accounts, disable withdrawals, restrict by IP. Going live is a separate decision;
  nothing in this repository does it as a side effect, and the allocator refuses to run in `enforce` mode while any managed bot reports `dry_run=false`.
- If you ever paste a log, export or screenshot into an issue, check it for tokens first. The export masks bearer tokens, API keys and long hex strings, but a masking rule cannot know every format.

## Running it safely

- Keep `dry_run` on (`FREQTRADE*__DRY_RUN=true`, the default).
- The Docker images pin Freqtrade (`freqtradeorg/freqtrade:2026.8`); its database migrations only go forward, so do not float the tag.
- The replay and study code writes only inside `REPLAY_DIR` (enforced and tested). The downloaders talk only to public exchange endpoints and the public Binance archive, without keys.
- The LLM never places an order, never sets a stop and never changes a threshold.

## Reporting a problem

Please use GitHub's **private vulnerability reporting** on this repository (Security tab), or open an issue that says "security" without exploit details and wait for a reply. There is no bug bounty.
Only the `main` branch is maintained.
