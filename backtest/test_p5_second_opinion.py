"""
Unit tests for the pure voting/pool-selection logic in _second_opinion.py
(Main's copy, used as the canonical reference -- Shorts/Moonshot/Dip's
copies are near-identical, see AGENTS.md for what differs in each).
Runs standalone, no API keys and no real Freqtrade install needed --
provider HTTP calls are mocked, never hit the network.
"""
import importlib.util
import os
import sys
from unittest import mock

fails = []


def check(label, cond):
    print(("[OK]  " if cond else "[FAIL]") + " " + label)
    if not cond:
        fails.append(label)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


so = load("D:/Trader/repo/freqtrade/user_data/strategies/_second_opinion.py", "so_test")

# ---------------------------------------------------------------------------
# is_enabled / is_borderline
# ---------------------------------------------------------------------------
os.environ.pop("FREQTRADE_SECOND_OPINION_ENABLED", None)
check("is_enabled() defaults to False when unset", so.is_enabled() is False)
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "true"
check("is_enabled() true when env var is 'true'", so.is_enabled() is True)
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "TRUE"
check("is_enabled() is case-insensitive", so.is_enabled() is True)
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "false"
check("is_enabled() false when explicitly 'false'", so.is_enabled() is False)
del os.environ["FREQTRADE_SECOND_OPINION_ENABLED"]

check("is_borderline('hold') is False", so.is_borderline("hold") is False)
check("is_borderline('buy') is True", so.is_borderline("buy") is True)
check("is_borderline('sell_short') is True", so.is_borderline("sell_short") is True)

# ---------------------------------------------------------------------------
# pick_pool
# ---------------------------------------------------------------------------
check("pick_pool: not borderline -> empty pool regardless of tier",
      so.pick_pool(is_core=True, borderline=False) == [])
check("pick_pool: satellite + borderline -> Tier1 only",
      so.pick_pool(is_core=False, borderline=True) == so.TIER1_PROVIDERS)
check("pick_pool: core + borderline -> Tier1 + Tier2",
      so.pick_pool(is_core=True, borderline=True) == so.TIER1_PROVIDERS + so.TIER2_PROVIDERS)
check("Tier1 has at least the original 4 providers (grok, nvidia, openrouter_free, mistral)",
      {"grok", "nvidia", "openrouter_free", "mistral"}.issubset(set(so.TIER1_PROVIDERS)))
check("Tier1 includes mistral", "mistral" in so.TIER1_PROVIDERS)
check("Tier2 has exactly 2 providers", len(so.TIER2_PROVIDERS) == 2)
check("mistral is dispatchable via _PROVIDER_CALLS", "mistral" in so._PROVIDER_CALLS)

# ---------------------------------------------------------------------------
# vote_result -- the actual voting rule
# ---------------------------------------------------------------------------
# 1 vote only (both providers failed) -> fail open, DeepSeek unchanged
verdict, reason = so.vote_result("buy", [])
check("1 vote (deepseek only) -> fail-open, unchanged", verdict == "buy" and "fail-open" in reason)

# 2 votes: agree -> that decision
verdict, reason = so.vote_result("buy", [{"provider": "grok", "vote": "buy"}])
check("2 votes agree (buy, buy) -> buy", verdict == "buy" and "agree" in reason)

# 2 votes: disagree -> hold
verdict, reason = so.vote_result("buy", [{"provider": "grok", "vote": "hold"}])
check("2 votes disagree (buy vs hold) -> hold", verdict == "hold" and "disagree" in reason)

# 3 votes: 2-of-3 majority
verdict, reason = so.vote_result("buy", [{"provider": "grok", "vote": "buy"}, {"provider": "nvidia", "vote": "hold"}])
check("3 votes, 2/3 agree (buy) -> buy", verdict == "buy" and "2/3" in reason)

verdict, reason = so.vote_result("buy", [{"provider": "grok", "vote": "hold"}, {"provider": "nvidia", "vote": "hold"}])
check("3 votes, 2/3 agree (hold, deepseek outvoted) -> hold", verdict == "hold" and "2/3" in reason)

# 3 votes, all distinct (shorts 3-way) -> DeepSeek tie-break
verdict, reason = so.vote_result("buy", [{"provider": "grok", "vote": "sell_short"}, {"provider": "nvidia", "vote": "hold"}])
check("3 votes, all distinct -> DeepSeek tie-break (buy)", verdict == "buy" and "tie-break" in reason)

# ---------------------------------------------------------------------------
# gather_votes -- parallel dispatch, mocked provider calls
# ---------------------------------------------------------------------------
def fake_provider_factory(answer, delay=0.0):
    def _call(system, user, timeout=15.0):
        import time
        time.sleep(delay)
        return answer
    return _call


with mock.patch.dict(so._PROVIDER_CALLS, {
    "grok": fake_provider_factory("buy: looks strong"),
    "nvidia": fake_provider_factory("hold: not convinced", delay=0.05),
    "openrouter_free": fake_provider_factory(None),  # simulates a real failure (key missing etc)
}):
    votes = so.gather_votes(["grok", "nvidia", "openrouter_free"], "sys", "usr", ("buy", "hold"), max_votes=2)
    check("gather_votes: returns exactly 2 votes when 2 succeed", len(votes) == 2)
    check("gather_votes: skips a provider that returned None", all(v["provider"] != "openrouter_free" for v in votes))
    check("gather_votes: parses 'buy: looks strong' -> vote='buy'",
          any(v["provider"] == "grok" and v["vote"] == "buy" for v in votes))
    check("gather_votes: parses 'hold: not convinced' -> vote='hold'",
          any(v["provider"] == "nvidia" and v["vote"] == "hold" for v in votes))

with mock.patch.dict(so._PROVIDER_CALLS, {
    "grok": fake_provider_factory(None),
    "nvidia": fake_provider_factory(None),
    "openrouter_free": fake_provider_factory(None),
}):
    votes = so.gather_votes(["grok", "nvidia", "openrouter_free"], "sys", "usr", ("buy", "hold"), max_votes=2)
    check("gather_votes: all providers fail -> empty list, no exception", votes == [])

check("gather_votes: empty pool -> empty list, no calls attempted", so.gather_votes([], "s", "u", ("buy", "hold")) == [])

# P5.4: a straggler still in flight after quorum (max_votes) is reached
# must not block gather_votes's return -- it should come back as soon as
# max_votes successful answers are in, not wait for the slow provider's
# own HTTP timeout (this was the real ThreadPoolExecutor(...) as ex: bug --
# `with` blocked on ex.__exit__ -> shutdown(wait=True) for ALL futures
# even after the early `break`).
with mock.patch.dict(so._PROVIDER_CALLS, {
    "grok": fake_provider_factory("buy: looks strong", delay=0.05),
    "nvidia": fake_provider_factory("buy: agree", delay=0.05),
    "mistral": fake_provider_factory("hold: too slow", delay=5.0),  # would blow the budget if waited on
}):
    import time as _time
    t0 = _time.monotonic()
    votes = so.gather_votes(["grok", "nvidia", "mistral"], "sys", "usr", ("buy", "hold"), max_votes=2)
    elapsed = _time.monotonic() - t0
    check("gather_votes: returns once quorum (2) reached, doesn't wait for slow straggler",
          elapsed < 1.0)
    check("gather_votes: quorum votes are the 2 fast providers, straggler excluded",
          len(votes) == 2 and all(v["provider"] != "mistral" for v in votes))

# ---------------------------------------------------------------------------
# run_shadow -- end-to-end shadow-mode wiring, mocked, asserts no exception
# and that it never returns anything the caller could accidentally use to
# override its own decision (it returns None by contract).
# ---------------------------------------------------------------------------
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "true"
with mock.patch.object(so, "gather_votes", return_value=[{"provider": "grok", "vote": "buy", "raw": "buy", "malformed": False}]):
    result = so.run_shadow("BTC/USDT", True, "buy", "sys", "usr")
    check("run_shadow returns None (shadow-only contract)", result is None)
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "false"
with mock.patch.object(so, "gather_votes") as gv_mock:
    so.run_shadow("BTC/USDT", True, "buy", "sys", "usr")
    check("run_shadow does nothing at all when disabled (zero API calls)", not gv_mock.called)
os.environ["FREQTRADE_SECOND_OPINION_ENABLED"] = "true"
with mock.patch.object(so, "gather_votes") as gv_mock:
    so.run_shadow("BTC/USDT", True, "hold", "sys", "usr")
    check("run_shadow skips the ensemble entirely on a hold decision (not borderline)", not gv_mock.called)
del os.environ["FREQTRADE_SECOND_OPINION_ENABLED"]

# ---------------------------------------------------------------------------
# P5.2/P5.3: new/fixed provider behavior
# ---------------------------------------------------------------------------
check("Tier1 now has 5 providers (grok, nvidia, openrouter_free, mistral, kilo)",
      len(so.TIER1_PROVIDERS) == 5 and "kilo" in so.TIER1_PROVIDERS and "aion" not in so.TIER1_PROVIDERS)
check("agnes is dispatchable but NOT in TIER1 or TIER2", "agnes" in so._PROVIDER_CALLS
      and "agnes" not in so.TIER1_PROVIDERS and "agnes" not in so.TIER2_PROVIDERS)
check("aion is dispatchable but NOT in TIER1 or TIER2 (confirmed no free tier)", "aion" in so._PROVIDER_CALLS
      and "aion" not in so.TIER1_PROVIDERS and "aion" not in so.TIER2_PROVIDERS)
check("AGNES_ENABLED defaults to False", so.AGNES_ENABLED is False)
check("AION_ENABLED defaults to False", so.AION_ENABLED is False)

# _call_agnes must be a true no-op while disabled, even with a key present
os.environ["AGNES_API_KEY"] = "fake-key-for-test"
with mock.patch.object(so.requests, "post") as post_mock:
    result = so._call_agnes("sys", "usr")
    check("_call_agnes returns None while AGNES_ENABLED=False", result is None)
    check("_call_agnes never even attempts an HTTP call while disabled", not post_mock.called)
del os.environ["AGNES_API_KEY"]

# _call_aion must be a true no-op while disabled, even with a key present
os.environ["AION_API_KEY"] = "fake-key-for-test"
with mock.patch.object(so.requests, "post") as post_mock:
    result = so._call_aion("sys", "usr")
    check("_call_aion returns None while AION_ENABLED=False", result is None)
    check("_call_aion never even attempts an HTTP call while disabled", not post_mock.called)
del os.environ["AION_API_KEY"]

# Kilo: request body must include the confirmed real free-tier model field
os.environ["KILO_API_KEY"] = "fake-key-for-test"
def _kilo_capture(url, headers=None, json=None, timeout=None):
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: kilo ok"}}]})
    _kilo_capture.last_json = json
    return m
with mock.patch.object(so.requests, "post", side_effect=_kilo_capture):
    result = so._call_kilo("sys", "usr")
    check("_call_kilo sends model='kilo-auto/free' (confirmed real, no-credits-required tier)",
          _kilo_capture.last_json["model"] == "kilo-auto/free")
    check("_call_kilo returns the response content", result == "hold: kilo ok")
del os.environ["KILO_API_KEY"]

# NVIDIA tests below pin the dynamic-discovery cache to a known, empty
# list so `_get_nvidia_candidate_models` never makes a real network call
# during these tests (same reasoning as pre-seeding the OpenRouter/llm7
# caches elsewhere in this file).
os.environ["NVIDIA_API_KEY"] = "fake-key"
so._nvidia_model_cache["list"] = []
so._nvidia_model_cache["fetched_at"] = so.time.time()

# NVIDIA: primary model succeeds -> fallback model never attempted
def _nvidia_side_effect(url, headers=None, json=None, timeout=None):
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    if json["model"] == "nvidia/nemotron-3-super-120b-a12b":
        m.json = mock.Mock(return_value={"choices": [{"message": {"content": "buy: primary model ok"}}]})
    else:
        raise AssertionError("fallback model should not have been called when primary succeeded")
    return m

with mock.patch.object(so.requests, "post", side_effect=_nvidia_side_effect):
    result = so._call_nvidia("sys", "usr")
    check("_call_nvidia: primary model success -> returns its content, fallback untouched",
          result == "buy: primary model ok")

# NVIDIA: primary model fails -> fallback model IS attempted and used
call_log = []
def _nvidia_fallback_effect(url, headers=None, json=None, timeout=None):
    call_log.append(json["model"])
    if json["model"] == "nvidia/nemotron-3-super-120b-a12b":
        raise Exception("410 Gone")
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: fallback model"}}]})
    return m

with mock.patch.object(so.requests, "post", side_effect=_nvidia_fallback_effect):
    result2 = so._call_nvidia("sys", "usr")
    check("_call_nvidia: primary fails -> fallback model tried and used",
          result2 == "hold: fallback model" and call_log == [
              "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b"])

# NVIDIA P5.7: all 6 hardcoded models (2 original + 4 added in P5.7, all
# confirmed live in a real session -- see _call_nvidia's docstring) fail
# -> falls through to a live-discovered candidate as the 7th attempt
so._nvidia_model_cache["list"] = ["nvidia/nemotron-3-discovered-live"]
so._nvidia_model_cache["fetched_at"] = so.time.time()
call_log2 = []
def _nvidia_third_effect(url, headers=None, json=None, timeout=None):
    call_log2.append(json["model"])
    if json["model"] == "nvidia/nemotron-3-discovered-live":
        m = mock.Mock()
        m.raise_for_status = mock.Mock()
        m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: from live-discovered model"}}]})
        return m
    raise Exception("Gone")
with mock.patch.object(so.requests, "post", side_effect=_nvidia_third_effect):
    result3 = so._call_nvidia("sys", "usr")
    check("_call_nvidia: all 6 hardcoded models fail -> falls through to a live-discovered candidate",
          result3 == "hold: from live-discovered model" and call_log2 == [
              "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
              "nvidia/nemotron-3.5-lightning-30b-a3b", "deepseek-ai/deepseek-v4.1-flash",
              "openai/gpt-oss-20b", "meta/muse-glimmer-30b",
              "nvidia/nemotron-3-discovered-live"])
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0

# NVIDIA P5.7: the 4 newly added fallback models are all present, in
# order, ahead of the dynamically-discovered candidate
so._nvidia_model_cache["list"] = []
so._nvidia_model_cache["fetched_at"] = so.time.time()
def _nvidia_p57_capture(url, headers=None, json=None, timeout=None):
    _nvidia_p57_capture.seen.append(json["model"])
    raise Exception("simulate all failing, just record the attempted order")
_nvidia_p57_capture.seen = []
with mock.patch.object(so.requests, "post", side_effect=_nvidia_p57_capture):
    so._call_nvidia("sys", "usr")
    check("_call_nvidia P5.7: fallback chain includes all 4 newly added models in order",
          _nvidia_p57_capture.seen == [
              "nvidia/nemotron-3-super-120b-a12b", "nvidia/nemotron-3-ultra-550b-a55b",
              "nvidia/nemotron-3.5-lightning-30b-a3b", "deepseek-ai/deepseek-v4.1-flash",
              "openai/gpt-oss-20b", "meta/muse-glimmer-30b"])
    check("_call_nvidia P5.7: google/gemma-4-31b-it deliberately excluded (confirmed live hang, not a guess)",
          "google/gemma-4-31b-it" not in _nvidia_p57_capture.seen)
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0

# NVIDIA: max_tokens raised to 300 (P5.6 -- the real fix for the
# reasoning-truncation bug; "detailed thinking off" alone did not stop
# nemotron-3-super-120b-a12b from reasoning in real testing)
so._nvidia_model_cache["list"] = []
so._nvidia_model_cache["fetched_at"] = so.time.time()
def _nvidia_capture_tokens(url, headers=None, json=None, timeout=None):
    _nvidia_capture_tokens.last_json = json
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: ok"}}]})
    return m
with mock.patch.object(so.requests, "post", side_effect=_nvidia_capture_tokens):
    so._call_nvidia("sys", "usr")
    check("_call_nvidia: max_tokens is 300, not 50 -- gives the reasoning phase room to finish",
          _nvidia_capture_tokens.last_json["max_tokens"] == 300)
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0

# _get_nvidia_candidate_models: filters to nvidia/-branded Nemotron CHAT
# models only, excluding embed/parse/safety/etc non-chat models by name
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0
fake_models_list = mock.Mock()
fake_models_list.raise_for_status = mock.Mock()
fake_models_list.json = mock.Mock(return_value={"data": [
    {"id": "nvidia/nemotron-3-super-120b-a12b"},
    {"id": "nvidia/nemotron-3-embed-1b"},          # excluded: embedding model
    {"id": "nvidia/nemotron-parse-2.0"},           # excluded: parser
    {"id": "nvidia/llama-3.1-nemotron-safety-guard-8b-v3"},  # excluded: safety
    {"id": "meta/llama-3.3-70b-instruct"},         # excluded: not nvidia/-branded
    {"id": "nvidia/nemotron-3-ultra-550b-a55b"},
]})
with mock.patch.object(so.requests, "get", return_value=fake_models_list):
    candidates = so._get_nvidia_candidate_models("fake-key")
    check("_get_nvidia_candidate_models: keeps nvidia/-branded Nemotron chat models",
          "nvidia/nemotron-3-super-120b-a12b" in candidates and "nvidia/nemotron-3-ultra-550b-a55b" in candidates)
    check("_get_nvidia_candidate_models: excludes embed/parse/safety-guard/non-nvidia models",
          not any("embed" in c or "parse" in c or "safety" in c or c.startswith("meta/") for c in candidates))
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0

del os.environ["NVIDIA_API_KEY"]

# LLM7: "fast" succeeds -> /v1/models never fetched
os.environ["LLM7_API_KEY"] = "fake-key"
def _llm7_fast_ok(url, headers=None, json=None, timeout=None):
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "buy: fast model ok"}}]})
    return m

with mock.patch.object(so.requests, "post", side_effect=_llm7_fast_ok):
    with mock.patch.object(so.requests, "get") as get_mock:
        result3 = so._call_llm7("sys", "usr")
        check("_call_llm7: 'fast' succeeds -> returns content", result3 == "buy: fast model ok")
        check("_call_llm7: 'fast' succeeds -> /v1/models never fetched", not get_mock.called)

# LLM7: "fast" fails -> live model list fetched and first entry used
so._llm7_free_models_cache["list"] = None
so._llm7_free_models_cache["fetched_at"] = 0
llm7_calls = []
def _llm7_fast_fails_then_fallback(url, headers=None, json=None, timeout=None):
    llm7_calls.append(json["model"])
    if json["model"] == "fast":
        raise Exception("fast selector unavailable")
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: fallback model used"}}]})
    return m

fake_models_resp = mock.Mock()
fake_models_resp.raise_for_status = mock.Mock()
fake_models_resp.json = mock.Mock(return_value={"data": [{"id": "some-live-free-model"}]})
with mock.patch.object(so.requests, "post", side_effect=_llm7_fast_fails_then_fallback):
    with mock.patch.object(so.requests, "get", return_value=fake_models_resp) as get_mock:
        result4 = so._call_llm7("sys", "usr")
        check("_call_llm7: 'fast' fails -> live model list WAS fetched", get_mock.called)
        check("_call_llm7: falls back to the first live-discovered model",
              result4 == "hold: fallback model used" and llm7_calls == ["fast", "some-live-free-model"])
del os.environ["LLM7_API_KEY"]

# openrouter_free: empty candidate list now logs a warning (P5.1 silent-failure fix)
os.environ["OPENROUTER_API_KEY"] = "fake-key"
so._openrouter_free_models_cache["list"] = []
so._openrouter_free_models_cache["fetched_at"] = so.time.time()
with mock.patch.object(so.log, "warning") as warn_mock:
    result5 = so._call_openrouter_free("sys", "usr")
    check("_call_openrouter_free: empty candidate list -> returns None", result5 is None)
    check("_call_openrouter_free: empty candidate list -> now logs a warning (used to be silent)",
          warn_mock.called and any("openrouter_free skipped" in str(c) for c in warn_mock.call_args_list))
so._openrouter_free_models_cache["list"] = None
so._openrouter_free_models_cache["fetched_at"] = 0
del os.environ["OPENROUTER_API_KEY"]

# ---------------------------------------------------------------------------
# P5.3: rotation
# ---------------------------------------------------------------------------
so._rotation_state["index"] = 0
pool = ["a", "b", "c"]
r1 = so._rotate_pool(pool)
r2 = so._rotate_pool(pool)
r3 = so._rotate_pool(pool)
r4 = so._rotate_pool(pool)
check("_rotate_pool: first call returns the pool unrotated (index starts at 0)", r1 == ["a", "b", "c"])
check("_rotate_pool: rotates by one position each call", r2 == ["b", "c", "a"] and r3 == ["c", "a", "b"])
check("_rotate_pool: wraps back around after a full cycle", r4 == ["a", "b", "c"])
check("_rotate_pool: never mutates the input pool list", pool == ["a", "b", "c"])
check("_rotate_pool: empty pool stays empty, no crash", so._rotate_pool([]) == [])

# ---------------------------------------------------------------------------
# P5.3/P5.5: per-provider self-throttle
# ---------------------------------------------------------------------------
check("P5.5: Mistral throttle raised to 22s (was 6s -- real limit is per-workspace, not per-bot)",
      so.MIN_CALL_INTERVAL_SECONDS.get("mistral") == 22)

so._last_call_at.clear()
so._rotation_state["index"] = 0
with mock.patch.dict(so._PROVIDER_CALLS, {
    "mistral": lambda system, user, timeout=15.0: "buy: mistral vote",
    "grok": lambda system, user, timeout=15.0: "buy: grok vote",
}):
    # First call: both providers eligible, mistral gets called and its
    # _last_call_at timestamp gets recorded.
    votes_a = so.gather_votes(["mistral", "grok"], "sys", "usr", ("buy", "hold"), max_votes=2)
    check("throttle: first cycle, both providers vote", len(votes_a) == 2)

    # Immediately again: mistral is inside its own 22s window -> must be
    # skipped this cycle, only grok should vote.
    votes_b = so.gather_votes(["mistral", "grok"], "sys", "usr", ("buy", "hold"), max_votes=2)
    check("throttle: mistral skipped on an immediate second call (still inside its 22s window)",
          len(votes_b) == 1 and votes_b[0]["provider"] == "grok")

    # Simulate 23 real seconds having passed for mistral specifically ->
    # eligible again.
    so._last_call_at["mistral"] = so.time.time() - 23
    votes_c = so.gather_votes(["mistral", "grok"], "sys", "usr", ("buy", "hold"), max_votes=2)
    check("throttle: mistral eligible again once its interval has elapsed",
          any(v["provider"] == "mistral" for v in votes_c))
so._last_call_at.clear()

# ---------------------------------------------------------------------------
# P5.5: NVIDIA disables "thinking" via a leading system message
# ---------------------------------------------------------------------------
os.environ["NVIDIA_API_KEY"] = "fake-key"
so._nvidia_model_cache["list"] = []
so._nvidia_model_cache["fetched_at"] = so.time.time()
def _nvidia_capture_payload(url, headers=None, json=None, timeout=None):
    _nvidia_capture_payload.last_json = json
    m = mock.Mock()
    m.raise_for_status = mock.Mock()
    m.json = mock.Mock(return_value={"choices": [{"message": {"content": "hold: ok"}}]})
    return m
with mock.patch.object(so.requests, "post", side_effect=_nvidia_capture_payload):
    so._call_nvidia("real system prompt", "real user message")
    messages = _nvidia_capture_payload.last_json["messages"]
    check("_call_nvidia: 'detailed thinking off' is present as its own system message",
          any(m.get("role") == "system" and m.get("content") == "detailed thinking off" for m in messages))
    check("_call_nvidia: the real system prompt is still sent, unmodified, as its own message",
          any(m.get("role") == "system" and m.get("content") == "real system prompt" for m in messages))
    check("_call_nvidia: 'detailed thinking off' comes before the real system prompt",
          messages.index(next(m for m in messages if m.get("content") == "detailed thinking off"))
          < messages.index(next(m for m in messages if m.get("content") == "real system prompt")))
so._nvidia_model_cache["list"] = None
so._nvidia_model_cache["fetched_at"] = 0
del os.environ["NVIDIA_API_KEY"]

# ---------------------------------------------------------------------------
# P5.5: a malformed answer must not silently become a counted "hold" vote
# ---------------------------------------------------------------------------
so._last_call_at.clear()
so._rotation_state["index"] = 0
with mock.patch.dict(so._PROVIDER_CALLS, {
    "grok": fake_provider_factory("this is not a valid decision line at all"),  # -> malformed, parses to hold/True
    "nvidia": fake_provider_factory("buy: real vote", delay=0.05),
}):
    with mock.patch.object(so.log, "warning") as warn_mock:
        votes = so.gather_votes(["grok", "nvidia"], "sys", "usr", ("buy", "hold"), max_votes=2)
        check("malformed vote: garbage answer is NOT counted in votes",
              all(v["provider"] != "grok" for v in votes))
        check("malformed vote: the real nvidia vote still comes through",
              any(v["provider"] == "nvidia" and v["vote"] == "buy" for v in votes))
        check("malformed vote: a warning is logged for the discarded garbage answer",
              any("unparseable/malformed" in str(c) for c in warn_mock.call_args_list))
so._last_call_at.clear()

print()
if fails:
    print(f"{len(fails)} FAILURES:")
    for f in fails:
        print(" -", f)
    sys.exit(1)
print("ALL P5 SECOND-OPINION LOGIC CHECKS PASSED")
