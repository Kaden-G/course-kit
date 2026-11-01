"""One tiny chat client that speaks the OpenAI-compatible wire format, so the provider is a config line.
No SDKs: Gemini, OpenAI, Anthropic and any local server all accept this shape."""
from __future__ import annotations

import json
import urllib.request
import urllib.error

PRESETS = {
    "gemini":    {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "name": "gemini-2.5-flash",
                  "key_url": "https://aistudio.google.com/apikey"},
    "openai":    {"base_url": "https://api.openai.com/v1/", "name": "gpt-5-mini",
                  "key_url": "https://platform.openai.com/api-keys"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1/", "name": "claude-sonnet-5-5",
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "custom":    {"base_url": "http://localhost:11434/v1/", "name": "llama3", "key_url": ""},
}


class LLMError(Exception):
    pass


def resolve(model_cfg: dict) -> dict:
    provider = (model_cfg or {}).get("provider", "gemini")
    preset = PRESETS.get(provider, PRESETS["custom"])
    return {
        "provider": provider,
        "base_url": (model_cfg.get("base_url") or preset["base_url"]).rstrip("/") + "/",
        "name": model_cfg.get("name") or preset["name"],
        "api_key": model_cfg.get("api_key") or "",
    }


def chat(model_cfg: dict, system: str, user: str, temperature: float = 0.7, timeout: int = 180) -> str:
    m = resolve(model_cfg)
    if not m["api_key"] and m["provider"] != "custom":
        raise LLMError("No API key saved yet. In Settings, paste the key and click Save at the bottom.")
    body = {"model": m["name"], "temperature": temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(m["base_url"] + "chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {m['api_key']}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise LLMError(f"{m['provider']} returned HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise LLMError(f"Could not reach {m['base_url']}: {e.reason}") from None
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"Unexpected reply shape: {str(data)[:300]}")
