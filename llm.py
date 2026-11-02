"""One tiny chat client that speaks the OpenAI-compatible wire format, so the provider is a config line.
No SDKs: Gemini, OpenAI, Anthropic and any local server all accept this shape."""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error

PRESETS = {
    "gemini":    {"label": "Google Gemini (free tier)", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                  "name": "gemini-2.5-flash", "key_url": "https://aistudio.google.com/apikey"},
    "openai":    {"label": "OpenAI", "base_url": "https://api.openai.com/v1/", "name": "gpt-5-mini",
                  "key_url": "https://platform.openai.com/api-keys"},
    "anthropic": {"label": "Anthropic Claude", "base_url": "https://api.anthropic.com/v1/", "name": "claude-sonnet-5-5",
                  "key_url": "https://console.anthropic.com/settings/keys"},
    "custom":    {"label": "Custom / local server (Ollama, LM Studio)", "base_url": "http://localhost:11434/v1/",
                  "name": "llama3", "key_url": ""},
}


class LLMError(Exception):
    pass


def resolve(model_cfg: dict | None) -> dict:
    """Effective provider, endpoint, model name and key. Keys live per provider under model.keys so switching
    providers in Settings never sends one provider's key to another; a config from an older build that only has
    model.api_key still works."""
    model_cfg = model_cfg or {}
    provider = model_cfg.get("provider") or "gemini"
    preset = PRESETS.get(provider, PRESETS["custom"])
    keys = model_cfg.get("keys") if isinstance(model_cfg.get("keys"), dict) else {}
    api_key = keys.get(provider) if keys else model_cfg.get("api_key")
    return {
        "provider": provider,
        "base_url": (model_cfg.get("base_url") or preset["base_url"]).rstrip("/") + "/",
        "name": model_cfg.get("name") or preset["name"],
        "api_key": (api_key or "").strip(),
    }


def _friendly_http_error(provider: str, code: int, raw: str) -> str:
    detail = raw.strip()
    try:   # most providers wrap the reason as {"error": {"message": ...}}
        err = json.loads(raw).get("error")
        if isinstance(err, dict) and err.get("message"):
            detail = str(err["message"])
        elif isinstance(err, str):
            detail = err
    except Exception:
        pass
    detail = detail[:400]
    hints = {
        401: "The API key was rejected. Open Settings and paste it again.",
        403: "The API key was rejected or lacks permission. Open Settings and check it.",
        404: "The model name was not found. In Settings, clear the model name to use the provider's default.",
        429: "Rate limit or quota reached. Wait a minute and try again, or check the provider's billing page.",
    }
    hint = hints.get(code, "")
    return f"{provider} returned HTTP {code}: {detail}" + (f" {hint}" if hint else "")


def clean_reply(text: str) -> str:
    """Models sometimes wrap the whole answer in a code fence despite being told not to; unwrap that."""
    t = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", t, re.S)
    return m.group(1).strip() if m else t


def _content(data: dict) -> str:
    content = data["choices"][0]["message"]["content"]
    if isinstance(content, list):   # some servers return a list of parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def chat(model_cfg: dict, system: str, user: str, timeout: int = 180) -> str:
    """One round trip. No sampling parameters are sent: the newest OpenAI and Anthropic models reject a
    non-default temperature, and the default is right for drafting anyway."""
    m = resolve(model_cfg)
    if not m["api_key"] and m["provider"] != "custom":
        raise LLMError(f"No API key saved for {m['provider']} yet. In Settings, paste the key and click Save at the bottom.")
    body = {"model": m["name"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {m['api_key']}"}
    req = urllib.request.Request(m["base_url"] + "chat/completions", data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LLMError(_friendly_http_error(m["provider"], e.code, e.read().decode(errors="replace"))) from None
    except urllib.error.URLError as e:
        raise LLMError(f"Could not reach {m['base_url']}: {e.reason}. Check the internet connection"
                       + (" and that the local server is running." if m["provider"] == "custom" else ".")) from None
    except TimeoutError:
        raise LLMError(f"{m['provider']} did not answer within {timeout} seconds. Try again.") from None
    except (ValueError, OSError) as e:
        raise LLMError(f"Bad reply from {m['provider']}: {e}") from None
    try:
        text = _content(data)
    except (KeyError, IndexError, TypeError):
        err = data.get("error") if isinstance(data, dict) else None
        raise LLMError(f"Unexpected reply shape from {m['provider']}: {str(err or data)[:300]}") from None
    if not isinstance(text, str) or not text.strip():
        raise LLMError(f"{m['provider']} returned an empty reply. Try again.")
    return clean_reply(text)
