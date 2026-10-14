"""Google Gemini provider (REST generateContent).

Endpoint:  POST .../v1beta/models/<model>:generateContent
API key:   x-goog-api-key header
Reply text at candidates[0].content.parts[0].text
"""

from __future__ import annotations

import logging
import httpx

from .base import (
    BaseProvider,
    CompletionResult,
    ProviderConfig,
    Usage,
    get_http_client,
    http_timeout_s,
)

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
    # Google Gemini enforces TPM on input tokens only: "Tokens per minute (input) (TPM)".
    tpm_counts_output = False

    def __init__(self):
        super().__init__()
        self._use_proxy_fallback = False

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

    def _extract_error(self, resp: httpx.Response) -> RuntimeError:
        err_data = None
        try:
            err_data = resp.json()
            msg = err_data.get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
            err_data = {}

        is_geoblock = resp.status_code in (403, 451) or any(
            kw in msg.lower()
            for kw in (
                "location", "region", "country", "not supported",
                "not available", "unsupported", "forbidden", "geo",
            )
        )
        if is_geoblock:
            return RuntimeError("В вашей стране недоступен этот провайдер - нажмите ⚙, чтобы настроить Hugging Face прокси")

        retry_delay_s = None
        is_daily = False
        error_obj = err_data.get("error", {}) if isinstance(err_data, dict) else {}
        details = error_obj.get("details", [])
        for d in details:
            if isinstance(d, dict):
                rd = d.get("retryDelay")
                if rd is not None:
                    try:
                        s = str(rd).strip().rstrip("s")
                        retry_delay_s = float(s)
                    except ValueError:
                        pass
                for v in d.get("violations", []):
                    desc = str(v.get("description", "")).lower()
                    if any(w in desc for w in ("per day", "per_day", "daily")):
                        is_daily = True
        if any(w in msg.lower() for w in ("per day", "per_day", "daily limit", "requests per day")):
            is_daily = True

        if is_daily:
            return RuntimeError(f"Gemini API daily quota exceeded: {msg} [daily_limit]")
        if retry_delay_s is not None and retry_delay_s > 0:
            return RuntimeError(f"Gemini API rate limit ({resp.status_code}): {msg} [retry_after={retry_delay_s:.1f}s]")
        return RuntimeError(f"Gemini API error ({resp.status_code}): {msg}")

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
        timeout = httpx.Timeout(http_timeout_s(cfg), connect=20.0)
        client = get_http_client()
        try:
            resp = client.post(url, json=body, headers=headers, timeout=timeout)
            if resp.is_error:
                err_inst = self._extract_error(resp)
                # Check if geoblock allows proxy fallback
                is_geoblock = resp.status_code in (403, 451) or "стране недоступен" in str(err_inst)
                if is_geoblock and not self._use_proxy_fallback and cfg.base_url:
                    proxy_url = _get_proxy_url(f"{_API_ROOT}/{model}:generateContent", cfg.base_url)
                    logger.info("Direct Gemini API call failed with status %d. Retrying via proxy: %s", resp.status_code, proxy_url)
                    resp = client.post(proxy_url, json=body, headers=headers, timeout=timeout)
                    if not resp.is_error:
                        self._use_proxy_fallback = True
                    else:
                        raise self._extract_error(resp)
                else:
                    raise err_inst
        except Exception as e:
            if resp is None:
                if not self._use_proxy_fallback and cfg.base_url:
                    proxy_url = _get_proxy_url(f"{_API_ROOT}/{model}:generateContent", cfg.base_url)
                    logger.info("Direct Gemini API connection failed: %s. Retrying via proxy: %s", e, proxy_url)
                    try:
                        resp = client.post(proxy_url, json=body, headers=headers, timeout=timeout)
                        if not resp.is_error:
                            self._use_proxy_fallback = True
                        else:
                            raise self._extract_error(resp)
                    except Exception as proxy_e:
                        logger.error("Proxy Gemini API connection also failed: %s", proxy_e)
                        raise proxy_e
                else:
                    raise RuntimeError(f"Не удалось подключиться к API: {e}")
            else:
                raise e

        if resp.is_error:
            raise self._extract_error(resp)
        resp.raise_for_status()
        data = resp.json()
        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(
                p.get("text", "") for p in parts
                if isinstance(p, dict) and not p.get("thought")
            )
            if not text:
                raise KeyError("empty non-thought parts in Gemini response")
        except (KeyError, IndexError, TypeError) as e:
            candidates = data.get("candidates", [])
            if candidates and candidates[0].get("finishReason") in ("SAFETY", "PROHIBITED_CONTENT", "OTHER"):
                reason = candidates[0].get("finishReason")
                msg = candidates[0].get("finishMessage") or f"Blocked by Gemini safety policy ({reason})"
                raise RuntimeError(f"GEMINI_SAFETY_BLOCK: {msg}") from e
            raise RuntimeError(f"unexpected Gemini response: {data}") from e
        # Gemini usageMetadata: prompt + candidates + (optional) thoughts.
        # totalTokenCount is what AI Studio TPM actually rolls up when present.
        m = data.get("usageMetadata") or {}
        prompt_tok = int(m.get("promptTokenCount", 0) or 0)
        cand_tok = int(m.get("candidatesTokenCount", 0) or 0)
        thoughts_tok = int(m.get("thoughtsTokenCount", 0) or 0)
        total_tok = int(m.get("totalTokenCount", 0) or 0)
        if total_tok <= 0:
            total_tok = prompt_tok + cand_tok + thoughts_tok
        usage = Usage(
            prompt_tokens=prompt_tok,
            completion_tokens=cand_tok,
            total_tokens=total_tok,
        )
        return CompletionResult(text=text, usage=usage)
