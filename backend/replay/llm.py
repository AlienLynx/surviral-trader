"""
The replay LLM client. Default: the same DeepSeek request the live strategies send (same model, thinking disabled, same max_tokens and temperature per bot).
Optionally ONE other OpenAI-compatible provider (e.g. an OpenRouter free model) for smoke tests and a separate "other model on the same data" experiment.

What a paid / rate-limited batch job needs: a disk cache whose key includes provider, endpoint and model (results of different models never share an entry), bounded
parallelism, a per-minute request limit and a daily call cap, retries with backoff that honour 429 / Retry-After inside those limits, a `rate_limited` state the page can
show, real cost from the API's own `usage` field, and hard budget limits (USD and number of calls) that stop the run. A stopped run is resumed by starting it again:
everything already answered comes from the cache.

One provider + one key per run. There is no rotation or pooling of several accounts or keys, by design.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from . import paths

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-flash"
PROVIDERS = ("deepseek", "openai_compatible")


class BudgetExceeded(Exception):
    pass


class Stopped(Exception):
    pass


class ProviderConfigError(ValueError):
    pass


def cache_key(system: str, user: str, model: str, temperature: float, provider: str = "deepseek", url: str = DEEPSEEK_URL) -> str:
    """Provider, endpoint and model are part of the key: switching any of them never reuses another model's answer. The DeepSeek key stays what it was before providers existed."""
    parts = [system, user, model, temperature]
    if provider != "deepseek" or url != DEEPSEEK_URL:
        parts += [provider, url]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


class Provider:
    """Which endpoint / model / key / limits this run uses. `label` goes on every report and table row."""

    def __init__(self, kind: str = "deepseek", url: str = DEEPSEEK_URL, model: str = MODEL, api_key: str | None = None, rpm: int | None = None, daily_cap: int | None = None,
                 thinking_disabled: bool = True):
        self.kind, self.url, self.model, self.api_key = kind, url, model, api_key
        self.rpm, self.daily_cap, self.thinking_disabled = rpm, daily_cap, thinking_disabled

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.model}"

    def public(self) -> dict:
        return {"provider": self.kind, "model": self.model, "label": self.label, "rpm": self.rpm, "daily_cap": self.daily_cap, "url_host": self.url.split("/")[2] if "//" in self.url else self.url}


def _int_env(env, name: str) -> int | None:
    v = (env.get(name) or "").strip()
    try:
        return int(v) if v else None
    except ValueError:
        return None


def provider_from_env(kind: str | None = None, env=None) -> Provider:
    """`deepseek` (default; DEEPSEEK_API_KEY, the live bots' model) or `openai_compatible` (REPLAY_LLM_BASE_URL / MODEL / API_KEY; REPLAY_LLM_RPM and REPLAY_LLM_DAILY_CAP are
    mandatory -- a free model without call limits is refused). An empty variable (compose passes `${VAR:-}`) counts as not set."""
    env = os.environ if env is None else env
    kind = (kind or (env.get("REPLAY_LLM_PROVIDER") or "").strip() or "deepseek").lower()
    if kind not in PROVIDERS:
        raise ProviderConfigError(f"unknown provider {kind!r} (use one of {PROVIDERS})")
    if kind == "deepseek":
        return Provider("deepseek", DEEPSEEK_URL, MODEL, (env.get("DEEPSEEK_API_KEY") or "").strip() or None, _int_env(env, "REPLAY_LLM_RPM"), _int_env(env, "REPLAY_LLM_DAILY_CAP"))
    base = (env.get("REPLAY_LLM_BASE_URL") or "").strip().rstrip("/")
    model = (env.get("REPLAY_LLM_MODEL") or "").strip()
    key = (env.get("REPLAY_LLM_API_KEY") or "").strip()
    rpm, cap = _int_env(env, "REPLAY_LLM_RPM"), _int_env(env, "REPLAY_LLM_DAILY_CAP")
    missing = [n for n, v in (("REPLAY_LLM_BASE_URL", base), ("REPLAY_LLM_MODEL", model), ("REPLAY_LLM_API_KEY", key)) if not v]
    if missing:
        raise ProviderConfigError("openai_compatible provider is not configured: " + ", ".join(missing) + " not set")
    if not rpm or rpm < 1 or not cap or cap < 1:
        raise ProviderConfigError("REPLAY_LLM_RPM and REPLAY_LLM_DAILY_CAP are mandatory for a non-DeepSeek provider (call and per-minute limits)")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    return Provider("openai_compatible", url, model, key, rpm, cap, thinking_disabled=False)


class RateLimiter:
    """At most `rpm` request STARTS in any 60 seconds (sliding window). Thread-safe; the clock and sleep can be replaced in tests."""

    def __init__(self, rpm: int | None, clock=time.monotonic, sleep=time.sleep):
        self.rpm, self.clock, self.sleep = rpm, clock, sleep
        self.lock = threading.Lock()
        self.starts: deque[float] = deque()

    def wait(self, stop_event: threading.Event | None = None) -> float:
        """Blocks until a request may start; returns the seconds waited."""
        if not self.rpm:
            return 0.0
        waited = 0.0
        while True:
            with self.lock:
                now = self.clock()
                while self.starts and now - self.starts[0] >= 60.0:
                    self.starts.popleft()
                if len(self.starts) < self.rpm:
                    self.starts.append(now)
                    return waited
                gap = 60.0 - (now - self.starts[0])
            if stop_event is not None and stop_event.is_set():
                raise Stopped()
            step = max(0.05, min(gap, 1.0))
            self.sleep(step)
            waited += step


class LLMClient:
    def __init__(self, api_key: str | None, max_usd: float, max_calls: int, price_in_per_m: float | None, price_out_per_m: float | None, workers: int = 8,
                 post=None, sleep=time.sleep, stop_event: threading.Event | None = None, cache_dir: Path | None = None, retries: int = 5,
                 provider: Provider | None = None, clock=time.monotonic, now_utc=None):
        if post is None:
            import requests
            post = requests.post
        self.provider = provider or Provider("deepseek", DEEPSEEK_URL, MODEL, api_key)
        self.key = api_key if api_key is not None else self.provider.api_key
        self.max_usd, self.max_calls = float(max_usd), int(max_calls)
        self.pin, self.pout = price_in_per_m, price_out_per_m
        self._post, self._sleep, self.stop_event = post, sleep, stop_event or threading.Event()
        self.sem = threading.Semaphore(max(1, int(workers)))
        self.lock = threading.Lock()
        self.limiter = RateLimiter(self.provider.rpm, clock=clock, sleep=sleep)
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        self.cache_dir = cache_dir or paths.cache_path("llm")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retries = retries
        self.calls = 0           # paid API calls actually made
        self.cache_hits = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.usd = 0.0
        self.errors = 0
        self.rate_limited = 0    # HTTP 429 answers seen
        self.waiting = 0         # requests currently waiting for the limiter or a 429 back-off
        self.stopped_budget = False
        self.stop_reason = None
        self.free = self.pin == 0 and self.pout == 0

    # ---- cache
    def _cache_file(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cache_get(self, key: str):
        f = self._cache_file(key)
        if f.is_file():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None

    def _cache_put(self, key: str, payload: dict) -> None:
        f = self._cache_file(key)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    # ---- daily cap (calls per UTC day for this provider + model, persisted so a restart cannot reset it)
    def _daily_file(self) -> Path:
        tag = hashlib.sha256(f"{self.provider.kind}|{self.provider.url}|{self.provider.model}".encode()).hexdigest()[:12]
        return self.cache_dir.parent / f"llm_daily_{tag}.json"

    def _daily_count(self) -> int:
        try:
            d = json.loads(self._daily_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        return int(d.get("n", 0)) if d.get("day") == self._now_utc().strftime("%Y-%m-%d") else 0

    def _daily_add(self) -> None:
        day = self._now_utc().strftime("%Y-%m-%d")
        n = self._daily_count() + 1
        self._daily_file().write_text(json.dumps({"day": day, "n": n}), encoding="utf-8")

    # ---- cost
    def cost_of(self, tokens_in: int, tokens_out: int) -> float:
        if self.pin is None or self.pout is None:
            return 0.0
        return tokens_in * self.pin / 1e6 + tokens_out * self.pout / 1e6

    def snapshot(self) -> dict:
        return {"calls": self.calls, "cache_hits": self.cache_hits, "tokens_in": self.tokens_in, "tokens_out": self.tokens_out, "usd": round(self.usd, 5),
                "errors": self.errors, "stopped_budget": self.stopped_budget, "usd_known": self.pin is not None and self.pout is not None, "usd_limit_applies": not self.free,
                "stop_reason": self.stop_reason, "rate_limited": self.rate_limited, "waiting": self.waiting, "status": "rate_limited" if self.waiting else "ok",
                "calls_today": self._daily_count(), **self.provider.public()}

    def _check_limits(self):
        if self.stop_event.is_set():
            raise Stopped()
        if self.stopped_budget:
            raise BudgetExceeded(self.stop_reason or "budget already exhausted")
        if (not self.free and self.usd > self.max_usd) or self.calls >= self.max_calls:
            self.stopped_budget = True
            self.stop_reason = f"limit reached: ${self.usd:.4f} of ${self.max_usd} / {self.calls} of {self.max_calls} calls"
            raise BudgetExceeded(self.stop_reason)
        cap = self.provider.daily_cap
        if cap and self._daily_count() >= cap:
            self.stopped_budget = True
            self.stop_reason = f"daily call cap reached ({cap} calls today for {self.provider.label}); start the same run again tomorrow -- answered prompts come from the cache"
            raise BudgetExceeded(self.stop_reason)

    @staticmethod
    def _retry_after(r) -> float | None:
        try:
            v = (getattr(r, "headers", None) or {}).get("Retry-After")
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # ---- the call
    def call(self, system: str, user: str, temperature: float = 0.1, max_tokens: int = 300) -> str | None:
        key = cache_key(system, user, self.provider.model, temperature, self.provider.kind, self.provider.url)
        hit = self._cache_get(key)
        if hit is not None:
            with self.lock:
                self.cache_hits += 1
            return hit.get("text")
        with self.lock:
            self._check_limits()
        with self.sem:
            with self.lock:
                self._check_limits()
            body = {"model": self.provider.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "max_tokens": max_tokens, "temperature": temperature}
            if self.provider.thinking_disabled:
                body["thinking"] = {"type": "disabled"}
            last = None
            for attempt in range(self.retries):
                if self.stop_event.is_set():
                    raise Stopped()
                with self.lock:
                    self.waiting += 1
                try:
                    self.limiter.wait(self.stop_event)
                finally:
                    with self.lock:
                        self.waiting -= 1
                with self.lock:
                    self._check_limits()
                try:
                    r = self._post(self.provider.url, headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}, json=body, timeout=30)
                    status = getattr(r, "status_code", 200)
                    if status == 429:
                        with self.lock:
                            self.rate_limited += 1
                        ra = self._retry_after(r)
                        wait = min(max(ra if ra is not None else 2 ** attempt * 2, 1.0), 120.0)
                        with self.lock:
                            self.waiting += 1
                        try:
                            self._sleep(wait)
                        finally:
                            with self.lock:
                                self.waiting -= 1
                        last = RuntimeError("HTTP 429")
                        continue
                    if status in (500, 502, 503, 504):
                        raise RuntimeError(f"HTTP {status}")
                    r.raise_for_status()
                    data = r.json()
                    text = data["choices"][0]["message"]["content"]
                    usage = data.get("usage") or {}
                    tin, tout = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
                    with self.lock:
                        self.calls += 1
                        self.tokens_in += tin
                        self.tokens_out += tout
                        self.usd += self.cost_of(tin, tout)
                        if self.provider.daily_cap:
                            self._daily_add()
                    self._cache_put(key, {"text": text, "tokens_in": tin, "tokens_out": tout, "provider": self.provider.kind, "model": self.provider.model})
                    return text
                except (BudgetExceeded, Stopped):
                    raise
                except Exception as e:      # noqa: BLE001
                    last = e
                    self._sleep(min(2 ** attempt, 30))
            with self.lock:
                self.errors += 1
            return None if last is not None else None


def prices_from_env(env=None) -> tuple[float | None, float | None]:
    env = os.environ if env is None else env

    def f(k):
        try:
            return float(env[k])
        except (KeyError, ValueError):
            return None
    return f("REPLAY_PRICE_IN_PER_M"), f("REPLAY_PRICE_OUT_PER_M")
