"""
P5 ensemble second-opinion, Shorts bot's copy. 3-way decision set
(buy/sell_short/hold) -- run_shadow's default `allowed` below is adjusted
to match; the strategy file's own call site also passes it explicitly.

Duplicated per strategy file on purpose -- same reasoning as
get_market_context()/get_news_headlines() in DeepSeekStrategy.py itself:
each bot's container is self-contained, and a shared module across four
independent Freqtrade instances would mean one file's shape change could
silently break all four. If you fix a real bug here, check whether
Shorts/Moonshot/Dip's copies need the same fix.

WHAT THIS IS, RIGHT NOW: Phase 1 (shadow mode) ONLY. When enabled via
FREQTRADE_SECOND_OPINION_ENABLED=true, this computes a second/third-opinion
ensemble vote on every "borderline" DeepSeek decision, logs everything
(who voted, what they said, which tier of providers was used, what the
ensemble's verdict would have been), and returns that verdict to the
caller for logging ONLY -- DeepSeekStrategy.py's actual entry decision is
governed exclusively by DeepSeek's own vote, unchanged, regardless of
what this module computes. This does not affect live/dry execution at
all until Phase 2 (backtest/second_opinion_ab_test.py, not written to run
automatically) shows the ensemble is >= baseline on profit_factor with no
revenge-trading pattern -- the same gate discipline already used for the
Survival Bot (P... "Survival rollout") and P4.

"Borderline", defined operationally, not left vague: DeepSeek's own
decision was NOT "hold". There is no numeric confidence score anywhere in
this pipeline -- DeepSeek returns a bare decision token, not a probability
-- so "how sure was the model" cannot be measured directly. Every
actionable decision (anything that would actually open or flip a
position) is treated as the thing worth a second look; every "hold" is
treated as low-stakes (nothing happens either way) and never triggers the
ensemble at all. This is a real design simplification, stated plainly
here rather than dressed up as something more precise than it is.

"Critical" (gates access to the Tier 2 pool): pair.tier == "core" AND
borderline. Satellite-tier pairs only ever get the Tier 1 pool, even when
borderline -- per the brief, satellite already carries more risk for a
smaller position size and doesn't need the tighter-rate-limited providers
spent on it.

Provider coverage, updated per REAL calls against real accounts during
P5.1/P5.2/P5.3 (not guesses -- these were run with real keys and the
response BODIES read, not just status codes):
- Grok: CONFIRMED not a code or key-delivery bug at all. Repo-side wiring
  (XAI_API_KEY in docker-compose.yml's environment: block) was already
  correct on all four bots. A real call with a real key returned 403 with
  body: "Your newly created team doesn't have any credits or licenses
  yet" -- an xAI account/billing issue, nothing this code can fix. If
  Grok still 403s after credits are added to the console team, THEN
  reconsider the model slug -- not before.
- NVIDIA: prod 410 Gone was a stale/invalid model slug in the request
  body, not the base URL (already correct). Fixed to try a current model
  first with one fallback model on failure -- confirmed working end to
  end with a real key: the primary model still 410s, the fallback model
  succeeds and returns real content.
- Kilo: the endpoint IS correct (`/api/gateway/chat/completions` returns
  a real, structured JSON error, not a routing 404 page) -- but it
  requires an explicit `model` field even though the account routes
  across a pool automatically; omitting it gets "Model not found".
  UNRESOLVED: don't know what value that field should be (e.g. "auto", or
  one of the six pool model names) -- needs the operator's own Kilo docs/
  dashboard, not another guess.
- Aion Labs: endpoint and auth are fine -- a real call returned a clean
  400 "Unknown model: Aion-2.0". UNRESOLVED: the exact model string is
  wrong (casing or slug), needs the operator's own Aion console/docs to
  get the real value -- not guessed further here.
- Mistral: prod 429s are a real account limit (1 req/sec), not a bug --
  reproduced live in this session on the very first real call. See
  MIN_CALL_INTERVAL_SECONDS below for the per-provider self-throttle this
  led to (also confirmed working live: the second call within 6s was
  correctly skipped).
- OpenRouter_free: the actual silent-failure spot was an empty free-model
  list falling through with no log line -- fixed to log a warning there
  too, not just around the HTTP call itself.
- llm7.io: their own docs warn against hardcoding a specific model
  release (their free catalog is live/rotating) -- switched to their
  documented "fast" selector with a live-discovered fallback model,
  cached 6h, same pattern as OpenRouter's own free-model discovery.
- Every provider call now logs the real HTTP response BODY on failure
  (`_http_error_detail`), not just requests' default "404 Client Error:
  Not Found" -- that default hid the actual, actionable reason for every
  single one of the real failures above (Kilo's "Model not found", Grok's
  "no credits", Aion's "Unknown model") until they were dug out with an
  out-of-band curl. They should now be visible directly in this
  container's own logs going forward.
- Agnes AI (new Tier 2) is added but hard-disabled (AGNES_ENABLED=False)
  and genuinely untested -- do not enable until the operator confirms
  agnes-2.5-flash is free on their plan.
- Gemini's REST shape remains the most stable of the non-DeepSeek/
  OpenRouter providers (Google's documented v1beta API) but returned a
  404 on the same real test session -- not investigated further here,
  out of this patch's scope; its free-tier numbers are still a
  third-party-tracker claim -- verify at aistudio.google.com/rate-limits.
Check this container's logs after any deploy for
"second opinion: <provider> call failed: ... | body: ..." lines.
"""
import concurrent.futures
import json
import logging
import os
import re
import time
from collections import Counter

import requests

log = logging.getLogger(__name__)

ENABLED_ENV_VAR = "FREQTRADE_SHORTS_SECOND_OPINION_ENABLED"

TIER1_PROVIDERS = ["grok", "nvidia", "openrouter_free", "mistral", "kilo"]  # Mistral: free tier (1 req/s, 500k tok/min, 1B tok/mo per help.mistral.ai) beats llm7/Gemini, so Tier 1 not Tier 2. Kilo confirmed real (kilo-auto/free, no credits required). Aion deliberately excluded -- confirmed no free model exists, see AION_ENABLED below.
TIER2_PROVIDERS = ["llm7", "gemini"]  # agnes deliberately NOT listed here -- see AGNES_ENABLED below
MAX_VOTES = 2  # first 2 successful responses from the pool, per the brief

# P5.3: per-provider self-throttle. Not a shared/global limiter -- each of
# the 4 bot containers has no shared memory with the others, so this is a
# purely local, per-bot rate limit on how often THIS bot calls a given
# provider. Only Mistral has a real, confirmed prod 429 behind it (1
# req/sec account-wide limit) -- every other provider defaults to 0 (no
# self-throttle) until similarly evidenced; don't add a blanket number for
# providers that haven't actually shown a rate-limit problem.
# P5.5: raised 6s -> 22s. Mistral's real rate limit is per-WORKSPACE
# (account-wide), not per-bot -- this throttle only governs how often THIS
# one bot calls Mistral, with zero coordination across the other three
# containers. Four bots starting/cycling close together can still collide
# and 429 even at 22s; this reduces that probability, it does not
# eliminate it. A real fix would need shared state across containers
# (Redis, a shared file, etc) -- deliberately not built for a shadow-mode-
# only feature where the stakes of an occasional missed vote are low;
# don't read this number as a guarantee.
MIN_CALL_INTERVAL_SECONDS = {"mistral": 22}
_last_call_at: dict[str, float] = {}

# P5.3: rotates which provider in the pool goes first on each call, so the
# first entry in TIER1_PROVIDERS doesn't always eat the bulk of the load.
# Process-memory only -- resets on a bot restart, which is fine; this is
# about spreading load across a session, not a durable fairness guarantee.
_rotation_state = {"index": 0}

_openrouter_free_models_cache = {"list": None, "fetched_at": 0}
_OPENROUTER_FREE_LIST_TTL_SECONDS = 6 * 60 * 60

_llm7_free_models_cache = {"list": None, "fetched_at": 0}
_LLM7_FREE_LIST_TTL_SECONDS = 6 * 60 * 60

# P5.3 follow-up: confirmed via aionlabs.ai/docs/models/ -- every listed
# Aion model has real per-token pricing (from $0.70/1M in on the cheapest,
# Aion 3.0 Mini/3.5 Mini) and none are free. There is no free tier to
# route to, so this stays permanently disabled, same pattern as Agnes
# below -- not a "not verified yet", a confirmed "no free option exists".
AION_ENABLED = False

# P5.3: added but deliberately NOT wired into TIER2_PROVIDERS above. Do not
# flip this to True until the operator has confirmed in Agnes AI's own
# console that agnes-2.5-flash is actually free on their plan -- if it
# turns out to be paid, this must stay False permanently, not just until
# checked once.
AGNES_ENABLED = False


def is_enabled() -> bool:
    return os.getenv(ENABLED_ENV_VAR, "false").strip().lower() == "true"


def is_borderline(decision: str) -> bool:
    """See module docstring -- 'borderline' == not a hold, since there is
    no numeric confidence signal to threshold on instead."""
    return decision != "hold"


def pick_pool(is_core: bool, borderline: bool) -> list[str]:
    if not borderline:
        return []
    if is_core:
        return TIER1_PROVIDERS + TIER2_PROVIDERS
    return list(TIER1_PROVIDERS)


def _rotate_pool(pool: list[str]) -> list[str]:
    """P5.3: rotate the pool so the same provider doesn't always go first.
    Pure list rotation -- doesn't change WHICH providers are in the pool,
    only the order gather_votes() dispatches/prioritizes them in."""
    if not pool:
        return pool
    start = _rotation_state["index"] % len(pool)
    _rotation_state["index"] += 1
    return pool[start:] + pool[:start]


def _parse_vote(answer: str, allowed: tuple[str, ...]) -> tuple[str, bool]:
    """Same strict first-token parsing as _parse_llm_decision in the main
    strategy file -- duplicated, not imported, so this module stays
    self-contained even if the main file's parser ever changes shape.
    Returns (token, malformed)."""
    text = (answer or "").strip()
    head = text.partition(":")[0] if ":" in text else text
    head_norm = head.strip().lower()
    match = re.search(r"[a-z0-9_]+", head_norm)
    token = match.group(0) if match else ""
    if token and token in allowed:
        return token, False
    return "hold", True


def _http_error_detail(e: Exception) -> str:
    """Same reasoning as _call_deepseek's own error logging in the main
    strategy file (DeepSeekStrategy.py): requests' default str(e) on an
    HTTPError only has status+reason, never the response body -- and the
    body is exactly where a provider's real reason lives (invalid model,
    no credits, etc). Real, confirmed examples that motivated this during
    P5.2/P5.3 troubleshooting: a Kilo 404 logged as bare "Not Found" hid a
    real, actionable "Model not found" body (the model field was simply
    missing from the request); a Grok 403 logged as bare "Forbidden" hid
    "Your newly created team doesn't have any credits or licenses yet" --
    an account/billing issue, not a key-delivery or model-name bug at all.
    Without this, every one of those took a manual out-of-band curl to
    diagnose instead of being obvious from the bot's own logs."""
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"{e} | body: {e.response.text[:300]}"
    return str(e)


# ---------------------------------------------------------------------------
# Provider calls. Each returns the raw answer text or None on any failure --
# never raises, never blocks the caller. See module docstring for the
# "not verified against a live account" caveat on grok/nvidia/llm7/kilo/aion.
# ---------------------------------------------------------------------------
def _call_grok(system: str, user: str, timeout: float = 15.0) -> str | None:
    api_key = os.getenv("XAI_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                # xAI's model slugs churn like every other provider in this
                # project (see DeepSeekStrategy.py's own _call_deepseek
                # comment) -- check https://docs.x.ai for the current fast/
                # cheap model name if this starts failing with a 404/400.
                "model": "grok-4-fast-non-reasoning",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: grok call failed: %s", _http_error_detail(e))
        return None


_NVIDIA_CHAT_EXCLUDE_MARKERS = ("embed", "parse", "safety", "reward", "guard",
                                 "jailbreak", "rerank", "translate", "usdcode")
_nvidia_model_cache = {"list": None, "fetched_at": 0}
_NVIDIA_MODEL_LIST_TTL_SECONDS = 6 * 60 * 60


def _get_nvidia_candidate_models(api_key: str) -> list[str]:
    """P5.6: NVIDIA's own /v1/models has NO pricing/free-tier field at all
    (unlike OpenRouter/llm7's `pricing.prompt == 0` check) -- there is no
    API-verifiable way to filter for "free" the way the other two dynamic-
    discovery functions in this file do. The closest available proxy,
    confirmed via a third party (Kilo's own "Using Kilo for Free" docs,
    which describe routing to "NVIDIA's free endpoints (Super/Ultra/etc)"
    specifically): nvidia/-branded Nemotron chat models. Non-chat NVIDIA
    models (embeddings, rerankers, safety/guard classifiers, parsers,
    translation) are excluded by name -- confirmed by hand from a real
    account's own /v1/models response, not guessed. Cached 6h, same
    pattern as the OpenRouter/llm7 discovery functions."""
    now = time.time()
    cache = _nvidia_model_cache
    if cache["list"] is not None and (now - cache["fetched_at"]) < _NVIDIA_MODEL_LIST_TTL_SECONDS:
        return cache["list"]
    try:
        r = requests.get("https://integrate.api.nvidia.com/v1/models",
                          headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", [])]
        candidates = [
            i for i in ids
            if i.startswith("nvidia/") and "nemotron" in i
            and not any(marker in i for marker in _NVIDIA_CHAT_EXCLUDE_MARKERS)
        ]
        cache["list"], cache["fetched_at"] = candidates, now
        return candidates
    except Exception as e:
        log.warning("second opinion: could not fetch nvidia model list: %s", _http_error_detail(e))
        return cache["list"] or []


def _call_nvidia(system: str, user: str, timeout: float = 15.0) -> str | None:
    """P5.2 fix: prod logs showed a 410 Gone -- root cause was an invalid/
    stale model slug in the request body, NOT the base URL (already
    correct). P5.6 follow-up, confirmed against a real account, not
    guessed: `nvidia/llama-3.3-nemotron-super-49b-v1` (P5.2's "current"
    pick) has ACTUALLY reached end-of-life and no longer appears in
    /v1/models at all -- replaced with `nvidia/nemotron-3-super-120b-a12b`
    (confirmed 200 OK, real content, still live) as primary, plus one
    more confirmed-live model as a fixed fallback, plus a THIRD attempt
    from a live-discovered candidate list (see _get_nvidia_candidate_models)
    so a future EOL on both hardcoded slugs doesn't repeat this exact
    failure mode -- confirmed necessary: two other real, currently-listed
    Nemotron models (nano-3-30b-a3b, llama-3.1-nemotron-70b-instruct)
    returned a live 404 on this exact account despite appearing in
    /v1/models, so "still listed" alone doesn't mean "still invokable" --
    trying several real candidates in order is the honest answer, not a
    single perfect pick.

    P5.5/P5.6 on "detailed thinking off": kept as a leading system
    message (NVIDIA's documented convention for some Nemotron variants),
    but confirmed live that `nemotron-3-super-120b-a12b` does NOT honor it
    -- it reasons regardless. The actual, confirmed-working fix (same
    root cause as DeepSeek's own reasoning-model issue, see
    DeepSeekStrategy.py's _call_deepseek): `max_tokens` raised 50 -> 300 so
    the model's reasoning phase (real measured usage: ~215-220 completion
    tokens) completes before the API returns, leaving a clean final
    `content` field instead of a truncated ramble. Verified directly: at
    max_tokens=50 the response has finish_reason="length" and `content`
    IS the unfinished reasoning text; at 300, finish_reason="stop" and
    `content` is a clean one-line "buy: ..."/"hold: ..." answer, with
    reasoning correctly separated into its own `reasoning_content` field
    that this function never reads.

    P5.7: added four more fallback models to this SAME "nvidia" vote slot
    -- nvidia/nemotron-3.5-lightning-30b-a3b, deepseek-ai/deepseek-v4.1-flash,
    openai/gpt-oss-20b, meta/muse-glimmer-30b. These are NOT independent
    providers: they're all hosted on this same NVIDIA endpoint/account and
    share its rate limit, so unlike Kilo/Mistral/Aion they add zero
    cross-provider vote diversity -- their only purpose is resilience
    within this one slot, so a future EOL/hang on the primary+ultra
    Nemotron models (as already happened once with
    llama-3.3-nemotron-super-49b-v1 in P5.2) doesn't repeat before falling
    back to live discovery. All four confirmed live with one real call
    each, same session, not guessed: nemotron-3.5-lightning-30b-a3b and
    deepseek-v4.1-flash return clean content immediately; gpt-oss-20b and
    muse-glimmer-30b reason by default too (same class of issue as
    nemotron-3-super-120b-a12b above) and needed the max_tokens=300
    already in place here to return clean, non-truncated content.

    Explicitly tried and rejected: google/gemma-4-31b-it. It IS listed in
    a real account's /v1/models, but two separate live chat/completions
    calls to it (15s and 20s timeouts) returned zero bytes and no error --
    not a 4xx, just a hang. Another confirmed case of "listed" != "usable
    right now" (see the nano-3-30b-a3b/70b-instruct case above); left out
    of the fallback chain rather than added and silently stalling
    gather_votes() in production."""
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key:
        return None

    tried = set()
    models = [
        "nvidia/nemotron-3-super-120b-a12b",
        "nvidia/nemotron-3-ultra-550b-a55b",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        "deepseek-ai/deepseek-v4.1-flash",
        "openai/gpt-oss-20b",
        "meta/muse-glimmer-30b",
    ]
    models += [m for m in _get_nvidia_candidate_models(api_key) if m not in models][:1]

    for model in models:
        if model in tried:
            continue
        tried.add(model)
        try:
            r = requests.post(
                "https://integrate.api.nvidia.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "detailed thinking off"},
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            if content:
                return content
            log.warning("second opinion: nvidia model %s returned 200 but empty content", model)
        except Exception as e:
            log.warning("second opinion: nvidia call failed on model %s: %s", model, _http_error_detail(e))
    return None


def _get_openrouter_free_models() -> list[str]:
    now = time.time()
    cache = _openrouter_free_models_cache
    if cache["list"] is not None and (now - cache["fetched_at"]) < _OPENROUTER_FREE_LIST_TTL_SECONDS:
        return cache["list"]
    try:
        r = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        r.raise_for_status()
        entries = r.json().get("data", [])
        free = []
        for m in entries:
            model_id = m.get("id", "")
            pricing = m.get("pricing", {})
            try:
                is_free = float(pricing.get("prompt", "1") or "1") == 0 and float(pricing.get("completion", "1") or "1") == 0
            except (TypeError, ValueError):
                is_free = False
            if model_id.endswith(":free") and is_free:
                free.append(model_id)
        cache["list"], cache["fetched_at"] = free, now
        return free
    except Exception as e:
        log.warning("second opinion: could not fetch OpenRouter free model list: %s", _http_error_detail(e))
        return cache["list"] or []


def _call_openrouter_free(system: str, user: str, timeout: float = 15.0) -> str | None:
    """Deliberately a single free-model attempt, not the multi-candidate
    fallback CHAIN DeepSeekStrategy.py's own _call_openrouter_fallback
    uses for reliability -- this is meant to be ONE independent voice in
    the ensemble, not another safety net. Note this shares OPENROUTER_API_KEY
    with the primary DeepSeek fallback path, so it is not fully statistically
    independent of it -- stated plainly, not glossed over; that's what the
    brief specified (reuse OPENROUTER_API_KEY, don't add a second key)."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    candidates = _get_openrouter_free_models()
    if not candidates:
        # P5.1 fix: this was the actual silent-failure spot -- the HTTP
        # call below already had a try/except+log.warning, but an empty
        # free-model list (fetch succeeded, nothing usable came back, or a
        # prior fetch failure left the cache empty) returned None here
        # with zero log output. That's why "no votes, no explanation" was
        # showing up in prod for this provider specifically.
        log.warning("second opinion: openrouter_free skipped -- no free OpenRouter models available "
                    "(list fetch failed and nothing cached)")
        return None
    try:
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "models": candidates[:3],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: openrouter_free call failed: %s", _http_error_detail(e))
        return None


def _call_mistral(system: str, user: str, timeout: float = 15.0) -> str | None:
    """Tier 1, not Tier 2 -- Mistral's free tier (1 req/s / ~60 RPM, 500k
    tokens/min, 1B tokens/month per help.mistral.ai) is more generous than
    both llm7.io and Gemini's free tiers, so it earns a Tier 1 slot instead.
    Model slug best-effort like the other three non-Deepseek/OpenRouter
    providers here -- not verified against a live account in this session
    (no MISTRAL_API_KEY available); check https://docs.mistral.ai if this
    404s."""
    api_key = os.getenv("MISTRAL_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.mistral.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "mistral-small-latest",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: mistral call failed: %s", _http_error_detail(e))
        return None


def _get_llm7_free_models() -> list[str]:
    """P5.2: llm7.io's own docs explicitly warn against hardcoding a
    specific model release -- their free catalog is live/rotating, same
    class of churn OpenRouter's free tier already has (see
    _get_openrouter_free_models above). Only called when the "fast"
    selector itself fails, not on every call -- no need to pay this HTTP
    round-trip on the happy path."""
    now = time.time()
    cache = _llm7_free_models_cache
    if cache["list"] is not None and (now - cache["fetched_at"]) < _LLM7_FREE_LIST_TTL_SECONDS:
        return cache["list"]
    try:
        r = requests.get("https://api.llm7.io/v1/models", timeout=10)
        r.raise_for_status()
        entries = r.json().get("data", [])
        ids = [m.get("id") for m in entries if m.get("id")]
        cache["list"], cache["fetched_at"] = ids, now
        return ids
    except Exception as e:
        log.warning("second opinion: could not fetch llm7 model list: %s", _http_error_detail(e))
        return cache["list"] or []


def _call_llm7(system: str, user: str, timeout: float = 15.0) -> str | None:
    """P5.2 fix: no longer hardcodes a specific model release. Primary is
    llm7.io's own documented "fast" selector (their official free-tier
    entry point, not tied to one release). If that specific call fails,
    falls back to a live-discovered model from their own /v1/models
    endpoint, cached 6h. Rate limit per the brief: 40 req/min, 1M
    tokens/24h."""
    api_key = os.getenv("LLM7_API_KEY")
    if not api_key:
        return None

    def _attempt(model: str) -> str | None:
        try:
            r = requests.post(
                "https://api.llm7.io/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    "max_tokens": 50,
                    "temperature": 0.1,
                },
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.warning("second opinion: llm7 call failed on model %s: %s", model, _http_error_detail(e))
            return None

    result = _attempt("fast")
    if result is not None:
        return result
    fallback_models = _get_llm7_free_models()
    if not fallback_models:
        return None
    return _attempt(fallback_models[0])


def _call_gemini(system: str, user: str, timeout: float = 15.0) -> str | None:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent",
            params={"key": api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": 50, "temperature": 0.1},
            },
            timeout=timeout,
        )
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except Exception as e:
        log.warning("second opinion: gemini call failed: %s", _http_error_detail(e))
        return None


def _call_kilo(system: str, user: str, timeout: float = 15.0) -> str | None:
    """P5.3, new Tier 1 candidate. Kilo's gateway auto-routes across a
    pre-configured pool of 6 models on the operator's account (poolside/
    nvidia-nemotron/nex-agi/dots-studio/inclusionai/cohere) -- this call
    deliberately does NOT specify a model, Kilo picks one itself. The most
    speculative integration in this file alongside Aion/Agnes: zero
    verification against a live account in this session (no KILO_API_KEY
    available). If this fails, check Kilo's own current API docs before
    assuming a Freqtrade-side bug."""
    api_key = os.getenv("KILO_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.kilo.ai/api/gateway/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                # Confirmed via kilo.ai/docs/gateway/models-and-providers:
                # "kilo-auto/free" is Kilo's own documented auto-routing,
                # no-credits-required tier -- "resolved model is selected
                # dynamically per session from a curated set of available
                # free models". The endpoint itself was already correct;
                # omitting `model` entirely (the original assumption) is
                # what produced the real "Model not found" 404 seen in
                # P5.3's live test -- this field is required even though
                # routing is automatic.
                "model": "kilo-auto/free",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: kilo call failed: %s", _http_error_detail(e))
        return None


def _call_aion(system: str, user: str, timeout: float = 15.0) -> str | None:
    """P5.3 follow-up -- HARD-DISABLED, confirmed no free tier. Real model
    slug (verified via aionlabs.ai/docs/quickstart/) is "aion-labs/aion-2.0"
    -- the earlier "Aion-2.0" slug was simply wrong, missing the provider
    prefix and wrong casing, which is why it 400'd "Unknown model" in
    P5.3's live test. But aionlabs.ai/docs/models/ shows every model on
    real per-token pricing with none free, so fixing the slug wouldn't
    make this provider usable for a free-tier ensemble anyway -- disabled
    the same way as Agnes, not left broken on a wrong model name."""
    if not AION_ENABLED:
        return None
    api_key = os.getenv("AION_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://api.aionlabs.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "aion-labs/aion-2.0",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: aion call failed: %s", _http_error_detail(e))
        return None


def _call_agnes(system: str, user: str, timeout: float = 15.0) -> str | None:
    """P5.3, new Tier 2 candidate -- ADDED BUT HARD-DISABLED. Returns None
    immediately, before even checking for a key, unless AGNES_ENABLED is
    manually flipped to True at the top of this file. Do not flip it until
    the operator has confirmed in Agnes AI's own console that
    agnes-2.5-flash is genuinely free on their plan; if it's paid, this
    function must stay a permanent no-op, not a temporary one."""
    if not AGNES_ENABLED:
        return None
    api_key = os.getenv("AGNES_API_KEY")
    if not api_key:
        return None
    try:
        r = requests.post(
            "https://apihub.agnes-ai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "agnes-2.5-flash",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": 50,
                "temperature": 0.1,
            },
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("second opinion: agnes call failed: %s", _http_error_detail(e))
        return None


_PROVIDER_CALLS = {
    "grok": _call_grok,
    "nvidia": _call_nvidia,
    "openrouter_free": _call_openrouter_free,
    "mistral": _call_mistral,
    "kilo": _call_kilo,
    "aion": _call_aion,  # dispatchable, but never in TIER1/TIER2 -- see AION_ENABLED (confirmed no free tier)
    "llm7": _call_llm7,
    "gemini": _call_gemini,
    "agnes": _call_agnes,  # dispatchable, but never in TIER1/TIER2 -- see AGNES_ENABLED
}


def gather_votes(pool: list[str], system: str, user: str, allowed: tuple[str, ...],
                  max_votes: int = MAX_VOTES) -> list[dict]:
    """Calls providers in `pool` in parallel, returns the first
    `max_votes` that responded successfully, in completion order (not pool
    order) -- 'the first 2 successful answers from the pool, in parallel' per the
    brief. Providers with no configured key return None immediately
    (near-zero cost) and never occupy a vote slot.

    P5.3 additions, both applied before dispatch: the pool is rotated
    (_rotate_pool) so the same provider doesn't always sit first, and any
    provider still inside its own MIN_CALL_INTERVAL_SECONDS window (this
    bot's own last call to it, not a global/shared limiter) is skipped for
    this cycle entirely rather than called and likely 429'd."""
    if not pool:
        return []
    pool = _rotate_pool(pool)

    now = time.time()
    eligible = []
    for p in pool:
        if p not in _PROVIDER_CALLS:
            continue
        min_interval = MIN_CALL_INTERVAL_SECONDS.get(p, 0)
        elapsed = now - _last_call_at.get(p, 0)
        if min_interval and elapsed < min_interval:
            log.info("second opinion: %s throttled this cycle (last call %.1fs ago, min interval %ss)",
                      p, elapsed, min_interval)
            continue
        eligible.append(p)
    if not eligible:
        return []

    votes: list[dict] = []
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(eligible))
    try:
        future_to_provider = {}
        for p in eligible:
            _last_call_at[p] = now  # mark at submission time, not completion, so spacing is measured correctly
            future_to_provider[ex.submit(_PROVIDER_CALLS[p], system, user)] = p
        for future in concurrent.futures.as_completed(future_to_provider):
            provider = future_to_provider[future]
            try:
                answer = future.result()
            except Exception as e:
                log.warning("second opinion: %s raised unexpectedly: %s", provider, e)
                answer = None
            if answer is None:
                continue
            token, malformed = _parse_vote(answer, allowed)
            if malformed:
                # P5.5: a malformed/unparseable answer used to fall through
                # to _parse_vote's own "hold" default and get counted as a
                # REAL vote -- meaning a provider that said nothing
                # coherent could still outvote a real decision (e.g. 2/2
                # "disagree" -> hold, when one of those two "votes" was
                # actually garbage, not a considered "hold"). Treated the
                # same as a failed call now: logged, never added to votes.
                log.warning("second opinion: %s returned an unparseable/malformed answer, "
                            "not counted as a vote: %r", provider, answer[:200])
                continue
            votes.append({"provider": provider, "raw": answer[:120], "vote": token, "malformed": malformed})
            if len(votes) >= max_votes:
                break
    finally:
        # P5.4: once quorum (max_votes) is reached, don't block this cycle
        # on stragglers still in flight (e.g. a slow Kilo call holding the
        # whole vote up to its own HTTP timeout). Cancel any provider call
        # that hasn't started yet and stop waiting on ones already running
        # -- they finish in the background on their own thread and are
        # simply discarded; they never get a vote slot after this point.
        ex.shutdown(wait=False, cancel_futures=True)
    return votes


def vote_result(deepseek_decision: str, other_votes: list[dict]) -> tuple[str, str]:
    """Voting rule, exactly per the brief:
    - 3 votes total (DeepSeek + 2): 2-of-3 agree -> majority. All three
      distinct -> DeepSeek's own vote wins (tie-break).
    - 2 votes total (DeepSeek + 1): agree -> that decision. Disagree -> hold.
    - 1 vote (DeepSeek only, both providers failed/unreachable): fail-open,
      DeepSeek's decision unchanged.
    """
    tokens = [deepseek_decision] + [v["vote"] for v in other_votes]
    if len(tokens) == 1:
        return deepseek_decision, "fail-open: no second opinion available, DeepSeek only"
    if len(tokens) == 2:
        if tokens[0] == tokens[1]:
            return tokens[0], f"2/2 agree ({tokens[0]})"
        return "hold", f"2/2 disagree (deepseek={tokens[0]}, other={tokens[1]}) -> hold"
    counts = Counter(tokens)
    winner, freq = counts.most_common(1)[0]
    if freq >= 2:
        return winner, f"{freq}/3 agree ({winner}): {tokens}"
    return deepseek_decision, f"3-way split {tokens} -> DeepSeek tie-break ({deepseek_decision})"


def run_shadow(pair: str, is_core: bool, deepseek_decision: str, system: str, user: str,
                allowed: tuple[str, ...] = ("buy", "sell_short", "hold")) -> None:
    """Phase 1 entry point. Computes and logs the full ensemble picture --
    pool used, who voted, what they said, the hypothetical verdict -- and
    returns nothing: the caller's own `decision` is never touched. Call
    this AFTER DeepSeek's own decision is already final for this cycle."""
    if not is_enabled():
        return
    borderline = is_borderline(deepseek_decision)
    if not borderline:
        log.info("second_opinion[%s]: not borderline (deepseek=hold) -- skipped", pair)
        return

    pool = pick_pool(is_core, borderline)
    tier_label = "core (Tier1+Tier2)" if is_core else "satellite (Tier1 only)"
    votes = gather_votes(pool, system, user, allowed)
    verdict, explanation = vote_result(deepseek_decision, votes)

    log.info(
        "second_opinion[%s]: tier=%s pool=%s votes=%s deepseek=%s verdict=%s (%s) "
        "[SHADOW ONLY -- does not affect the real decision]",
        pair, tier_label, pool, votes, deepseek_decision, verdict, explanation,
    )
