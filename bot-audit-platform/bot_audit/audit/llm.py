"""
LLM API wrapper for the audit platform.

Uses OpenRouter (OpenAI-compatible) by default.
API key resolution order: explicit param → OPENROUTER_API_KEY env var → local config file.

Zero external dependencies — uses Python stdlib urllib only.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Any, Dict, Optional

from . import config as _cfg


def get_api_key(api_key: Optional[str] = None) -> str:
    """Resolve API key: explicit > env var > local config file."""
    if api_key:
        return api_key

    key = os.environ.get(_cfg.LLM_API_KEY_ENV)
    if key:
        return key

    # Try reading from local config file
    # Format: {"env": {"OPENROUTER_API_KEY": "sk-..."}}
    try:
        with open(_cfg.LOCAL_CONFIG_PATH) as f:
            cfg = json.load(f)
        key = (
            cfg.get("env", {}).get("OPENROUTER_API_KEY")
            or cfg.get("models", {})
               .get("providers", {})
               .get("openrouter", {})
               .get("apiKey")
        )
        if key:
            return key
    except Exception:
        pass

    raise ValueError(
        "No API key found. Set OPENROUTER_API_KEY env var or pass api_key param.\n"
        "See QUICKSTART.md for setup instructions."
    )


def call_llm(
    prompt: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    max_tokens: int = 2048,
    temperature: float = 0.2,
    system_prompt: Optional[str] = None,
) -> str:
    """
    Make a single LLM call via OpenRouter. Returns the response text string.

    Raises on HTTP or parsing errors.
    """
    key = get_api_key(api_key)
    model = model or _cfg.LLM_EXTRACTION_MODEL

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{_cfg.LLM_API_BASE}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/your-org/bot-audit-platform",
            "X-Title": "Bot Audit Platform",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return data["choices"][0]["message"]["content"]
