"""Google Gemini provider (REST generateContent).

Endpoint:  POST .../v1beta/models/<model>:generateContent
API key:   x-goog-api-key header
Reply text at candidates[0].content.parts[0].text
"""

from __future__ import annotations

import logging
import httpx

from .base import BaseProvider, CompletionResult, ProviderConfig, Usage

logger = logging.getLogger("interprex")

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
_DEFAULT_MODEL = "gemini-2.5-flash"


def _get_proxy_url(url: str, base_url: str) -> str:
    """Replaces https://generativelanguage.googleapis.com with base_url."""
    if not base_url:
        return url
    return url.replace("https://generativelanguage.googleapis.com", base_url.rstrip("/"))


class GeminiProvider(BaseProvider):
    name = "gemini"

    def __init__(self):
        super().__init__()
        self._use_proxy_fallback = True

    def list_models(self, cfg: ProviderConfig) -> list[str]:
        """Models the key can use, via GET v1beta/models, filtered to those that
        support generateContent (excludes embedding/vision-only entries). Returns
        the bare id without the "models/" prefix to match what _complete expects.
        [] on any failure (no key, network) so the UI falls back to free text."""
        api_key = cfg.api_key or cfg.api_key_2
        if not api_key:
            return []

        # Hardcoded list of supported Gemini text models to bypass network-level
        # hangs during start-up key verification.
        models = [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-2.0-flash",
            "gemini-2.0-flash-001",
            "gemini-2.0-flash-lite-001",
            "gemini-2.0-flash-lite",
            "gemini-2.5-flash-preview-tts",
            "gemini-2.5-pro-preview-tts",
            "gemma-4-26b-a4b-it",
            "gemma-4-31b-it",
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
            "gemini-2.5-flash-lite",
            "gemini-3-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
            "gemini-3.1-pro-preview-customtools",
            "gemini-3.1-flash-lite-preview",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "deep-research-max-preview-04-2026",
            "deep-research-preview-04-2026",
            "deep-research-pro-preview-12-2025",
            "antigravity-preview-05-2026",
        ]
        return sorted(models)

    def active_model(self, cfg: ProviderConfig, models: list[str] | None = None) -> str:
        """Return the preferred default model so the UI pre-selects it instead of
        landing on the first alphabetical entry. For cloud providers there's no
        'loaded' model — we just steer toward the recommended one."""
        if models is None:
            models = self.list_models(cfg)
        if not models:
            return ""
        # Prefer the module default if the key can actually use it.
        return _DEFAULT_MODEL if _DEFAULT_MODEL in models else models[0]

    def _complete(self, prompt: str, cfg: ProviderConfig) -> CompletionResult:
        model = cfg.model or _DEFAULT_MODEL
        if cfg.base_url:
            # Explicit proxy base — always route through it (geo-blocked users).
            url = _get_proxy_url(f"{_API_ROOT}/{model}:generateContent", cfg.base_url)
            self._use_proxy_fallback = True
        else:
            url = f"{_API_ROOT}/{model}:generateContent"
        
        key = cfg.api_key
        if not key:
            raise RuntimeError("No Gemini API key provided")
            
        headers = {
            "x-goog-api-key": key,
            "x-provider": "gemini",  # lets the Vercel proxy route to Gemini upstream
            "Content-Type": "application/json",
        }
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": {
                    "type": "OBJECT",
                    "properties": {
                        "items": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "id": { "type": "STRING" },
                                    "translated": { "type": "STRING" }
                                },
                                "required": ["id", "translated"]
                            }
                        }
                    },
                    "required": ["items"]
                }
            },
            "safetySettings": [
                {
                    "category": "HARM_CATEGORY_HARASSMENT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_HATE_SPEECH",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                    "threshold": "BLOCK_NONE"
                },
                {
                    "category": "HARM_CATEGORY_DANGEROUS_CONTENT",
                    "threshold": "BLOCK_NONE"
                }
            ]
        }
        resp = None
        timeout = httpx.Timeout(200.0, connect=20.0)
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
            if resp.is_error:
                try:
                    err_data = resp.json()
                    msg = err_data["error"]["message"]
                except Exception:
                    msg = resp.text
                
                # Check if this error warrants proxy fallback
                is_geoblock = resp.status_code in (403, 451) or any(kw in msg.lower() for kw in ("location", "region", "country", "not supported", "not available", "unsupported", "forbidden", "geo"))
                
                if is_geoblock:
                    if not self._use_proxy_fallback and cfg.base_url:
                        proxy_url = _get_proxy_url(f"{_API_ROOT}/{model}:generateContent", cfg.base_url)
                        logger.info("Direct Gemini API call failed with status %d: %s. Retrying via proxy: %s", resp.status_code, msg, proxy_url)
                        resp = httpx.post(proxy_url, json=body, headers=headers, timeout=timeout)
                        if not resp.is_error:
                            self._use_proxy_fallback = True
                    else:
                        raise RuntimeError("В вашей стране недоступен этот провайдер - нажмите ⚙, чтобы настроить Hugging Face прокси")
                else:
                    raise RuntimeError(f"Gemini API error ({resp.status_code}): {msg}")
        except Exception as e:
            if resp is None:
                if not self._use_proxy_fallback and cfg.base_url:
                    proxy_url = _get_proxy_url(f"{_API_ROOT}/{model}:generateContent", cfg.base_url)
                    logger.info("Direct Gemini API connection failed: %s. Retrying via proxy: %s", e, proxy_url)
                    try:
                        resp = httpx.post(proxy_url, json=body, headers=headers, timeout=timeout)
                        if not resp.is_error:
                            self._use_proxy_fallback = True
                    except Exception as proxy_e:
                        logger.error("Proxy Gemini API connection also failed: %s", proxy_e)
                        raise proxy_e
                else:
                    raise RuntimeError(f"Не удалось подключиться к API: {e}")
            else:
                raise e

        if resp.is_error:
            try:
                err_data = resp.json()
                msg = err_data["error"]["message"]
                raise RuntimeError(f"Gemini API error ({resp.status_code}): {msg}")
            except Exception as e_parse:
                if isinstance(e_parse, RuntimeError):
                    raise e_parse
                resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            candidates = data.get("candidates", [])
            if candidates and candidates[0].get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "OTHER"):
                reason = candidates[0].get("finishReason")
                msg = candidates[0].get("finishMessage") or f"Blocked by Gemini safety policy ({reason})"
                raise RuntimeError(f"GEMINI_SAFETY_BLOCK: {msg}") from e
            raise RuntimeError(f"unexpected Gemini response: {data}") from e
        # Gemini reports exact counts under usageMetadata.
        m = data.get("usageMetadata") or {}
        usage = Usage(
            prompt_tokens=int(m.get("promptTokenCount", 0) or 0),
            completion_tokens=int(m.get("candidatesTokenCount", 0) or 0),
        )
        return CompletionResult(text=text, usage=usage)
