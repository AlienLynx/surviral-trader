"""
LLM call wrapper. DeepSeek is the primary brain (cheap enough that the
agent's own API bill is a rounding error). OpenRouter's free-tier
DeepSeek model is the fallback if the primary key is missing, errors,
or times out — not for every call, just as a backstop.
"""
import logging
import os

import requests

log = logging.getLogger("survival-agent.brain")

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _call_deepseek(system: str, user: str, api_key: str, max_tokens: int = 1500, json_mode: bool = False) -> str | None:
    try:
        body = {
                "model": "deepseek-flash",
                # V4 models think by default (returns reasoning_content before
                # content), which was consuming max_tokens before reaching the
                # actual answer -- see AGENTS.md. Explicitly disabling it per
                # DeepSeek's own docs is the real fix, not just a bigger budget.
                "thinking": {"type": "disabled"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}      # DeepSeek's JSON mode (the prompt must contain the word "json")
        r = requests.post(
            DEEPSEEK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=90 if max_tokens > 1500 else 30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.warning("DeepSeek call failed: %s", e)
        return None


def _call_openrouter_fallback(system: str, user: str, api_key: str, max_tokens: int = 1500) -> str | None:
    try:
        r = requests.post(
            OPENROUTER_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                # See DeepSeekStrategy.py for the multi-candidate fallback
                # list -- this engine is unused by default (see AGENTS.md).
                "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            },
            timeout=90 if max_tokens > 1500 else 30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("OpenRouter fallback also failed: %s", e)
        return None


def ask(system: str, user: str, max_tokens: int = 1500, json_mode: bool = False) -> str | None:
    """Try DeepSeek first, fall back to OpenRouter's free tier. Returns
    None if both fail — callers must treat None as "no decision this
    cycle", never as "proceed anyway"."""
    deepseek_key = os.getenv("DEEPSEEK_API_KEY")
    if deepseek_key:
        result = _call_deepseek(system, user, deepseek_key, max_tokens, json_mode)
        if result is not None:
            return result

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if openrouter_key:
        log.info("Falling back to OpenRouter")
        return _call_openrouter_fallback(system, user, openrouter_key, max_tokens)     # no JSON mode here: free models vary, the prompt carries the instruction

    log.error("No working LLM backend — DeepSeek failed and no OpenRouter key set")
    return None
