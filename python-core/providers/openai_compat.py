"""OpenAI-compatible chat provider — covers Ollama AND LM Studio (and any other
local server exposing /v1/chat/completions). They differ only in default port,
so they're two thin subclasses over one implementation.

  Ollama    default  http://localhost:11434/v1
  LM Studio default  http://localhost:1234/v1

Local servers ignore the api key, so we send a dummy one to satisfy clients
that require the header.
"""

from __future__ import annotations

import logging
import httpx

logger = logging.getLogger("interprex")

from .base import BaseProvider, CompletionResult, ProviderConfig, Usage


class _OpenAICompat(BaseProvider):
    default_base_url: str = ""
    # Extra HTTP headers sent on every request. Empty for local servers; remote
    # backends (Kaggle behind ngrok) override this to bypass ngrok's interstitial.
    extra_headers: dict[str, str] = {}
    # Whether to send Ollama's num_ctx field. True for Ollama (it sizes the
    # context window / KV-cache VRAM); subclasses on servers that reject unknown
    # fields (vLLM/TGI) set this False — their window is fixed at server start.
    sends_num_ctx: bool = True

    def _base(self, cfg: ProviderConfig) -> str:
        return (cfg.base_url or self.default_base_url).rstrip("/")

    def _headers(self, cfg: ProviderConfig) -> dict[str, str]:
        # Local servers ignore the key but some clients require the header, so
        # send a dummy. extra_headers carries remote-only needs (ngrok bypass).
        # x-provider lets the Vercel proxy route to the right upstream (a proxy
        # URL otherwise looks like a generic OpenAI endpoint); real APIs ignore it.
        return {"Authorization": f"Bearer {cfg.api_key or 'local'}",
                "x-provider": self.name,
                **self.extra_headers}

    def list_models(self, cfg: ProviderConfig) -> list[str]:
        """Every model the server can serve, via the OpenAI-standard GET /models.
        Returns [] (UI falls back to free text) if the server is down or the
        route is missing."""
        try:
            resp = httpx.get(
                f"{self._base(cfg)}/models",
                headers=self._headers(cfg),
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or []
            ids = [m.get("id") for m in data if isinstance(m, dict) and m.get("id")]
            return sorted(ids)
        except Exception:
            return []  # never let model discovery break the app

    def _complete(self, prompt: str, cfg: ProviderConfig) -> CompletionResult:
        url = f"{self._base(cfg)}/chat/completions"
        body: dict = {
            "model": cfg.model or "",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        if cfg.num_ctx and self.sends_num_ctx:
            # Ollama reads num_ctx to size its context window (and thus KV-cache
            # VRAM); a smaller window means less video memory used. LM Studio
            # ignores unknown fields, so this is harmless there. Sent both at top
            # level and under options to cover both Ollama API shapes.
            body["num_ctx"] = cfg.num_ctx
            body["options"] = {"num_ctx": cfg.num_ctx}
        # Local models can be slow on first load; give them room.
        resp = httpx.post(url, json=body, headers=self._headers(cfg), timeout=200)
        if resp.is_error:
            try:
                err_data = resp.json()
                msg = err_data["error"]["message"]
                raise RuntimeError(f"API error ({resp.status_code}): {msg}")
            except Exception as e_parse:
                if isinstance(e_parse, RuntimeError):
                    raise e_parse
                resp.raise_for_status()
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        # OpenAI-compatible servers (incl. LM Studio and Ollama) return exact
        # token counts here, from the loaded model's own tokenizer.
        u = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(u.get("prompt_tokens", 0) or 0),
            completion_tokens=int(u.get("completion_tokens", 0) or 0),
        )
        return CompletionResult(text=text, usage=usage)


class OllamaProvider(_OpenAICompat):
    name = "ollama"
    default_base_url = "http://localhost:11434/v1"

    def _native_base(self, cfg: ProviderConfig) -> str:
        """Ollama's native API root (/api/...), which lives one level up from the
        OpenAI-compat /v1 path."""
        base = self._base(cfg)
        return base[:-3] if base.endswith("/v1") else base

    def active_model(self, cfg: ProviderConfig) -> str:
        """The model Ollama currently has loaded in VRAM, via native /api/ps. If
        several are loaded, the first is returned; "" if none (or server down) so
        the UI falls back to the full model list."""
        try:
            resp = httpx.get(f"{self._native_base(cfg)}/api/ps", timeout=10)
            resp.raise_for_status()
            running = resp.json().get("models") or []
            for m in running:
                name = m.get("name") or m.get("model")
                if name:
                    return name
            return ""
        except Exception:
            return ""

    def count_tokens(self, text: str, cfg: ProviderConfig) -> int | None:
        """Exact count via Ollama's native /api/tokenize, which uses the model's
        own vocabulary WITHOUT loading the weights into VRAM. The tokenize route
        lives on the native API root, not the OpenAI-compat /v1 path, so strip
        /v1. Returns None on any failure (older Ollama lacking the route, model
        not pulled, server down) so the caller falls back to the estimate."""
        if not cfg.model:
            return None
        base = self._native_base(cfg)
        try:
            resp = httpx.post(
                f"{base}/api/tokenize",
                json={"model": cfg.model, "prompt": text},
                timeout=15,
            )
            resp.raise_for_status()
            tokens = resp.json().get("tokens")
            return len(tokens) if isinstance(tokens, list) else None
        except Exception:
            return None  # never let token counting break translation


class LMStudioProvider(_OpenAICompat):
    name = "lmstudio"
    default_base_url = "http://localhost:1234/v1"

    def active_model(self, cfg: ProviderConfig) -> str:
        """LM Studio's /v1/models lists the models it has loaded, so the first
        entry is effectively the active one. "" if none loaded / server down."""
        models = self.list_models(cfg)
        return models[0] if models else ""


class KaggleProvider(_OpenAICompat):
    """A big model run on Kaggle's GPUs (e.g. 2×T4 16 GB) behind an ngrok tunnel,
    exposed as an OpenAI-compatible server (vLLM / llama.cpp / TGI). Same wire
    protocol as the local backends — it just lives at a remote https URL that
    changes each Kaggle session, so there's no default base URL: the user pastes
    the current ngrok URL (…/v1) and, if the server was started with --api-key,
    the key.

    Two remote-only quirks handled here:
      * ngrok's free tier serves an HTML interstitial until you send the
        `ngrok-skip-browser-warning` header — without it the reply is HTML, not
        JSON, and parsing silently yields nothing.
      * num_ctx is an Ollama-ism; vLLM/TGI reject unknown body fields, and their
        context window is fixed at server launch anyway, so we don't send it.
    """
    name = "kaggle"
    default_base_url = ""  # remote, per-session: user supplies the ngrok URL
    extra_headers = {"ngrok-skip-browser-warning": "true"}
    sends_num_ctx = False

    def active_model(self, cfg: ProviderConfig) -> str:
        """Whatever the server has loaded — typically one model per Kaggle
        session, so the first listed id is the active one. "" if unreachable."""
        models = self.list_models(cfg)
        return models[0] if models else ""


class OpenRouterProvider(_OpenAICompat):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"
    sends_num_ctx = False

    def list_models(self, cfg: ProviderConfig) -> list[str]:
        import re
        url = f"{self._base(cfg)}/models"
        headers = self._headers(cfg)
        params = {"limit": 200}
        models_data = []

        while url:
            try:
                resp = httpx.get(url, headers=headers, params=params, timeout=30.0)
                params = None  # Clear for subsequent pages if URL has them
                resp.raise_for_status()
                data = resp.json().get("data") or []
                for m in data:
                    if isinstance(m, dict) and m.get("id"):
                        models_data.append(m)

                # Check pagination Link header
                link_hdr = resp.headers.get("link")
                next_url = None
                if link_hdr:
                    match = re.search(r'<([^>]+)>;\s*rel="next"', link_hdr)
                    if match:
                        next_url = match.group(1)
                url = next_url
            except Exception as e:
                logger.warning("Error fetching OpenRouter models from %s: %s", url, e)
                break

        # Filter models
        if cfg.free_only:
            filtered_ids = []
            for m in models_data:
                m_id = m.get("id") or ""
                pricing = m.get("pricing") or {}
                try:
                    is_prompt_free = float(pricing.get("prompt", "1") or "1") == 0
                    is_completion_free = float(pricing.get("completion", "1") or "1") == 0
                except ValueError:
                    is_prompt_free = False
                    is_completion_free = False

                is_free = m_id.endswith(":free") or (is_prompt_free and is_completion_free)
                if is_free:
                    filtered_ids.append(m_id)
            return sorted(filtered_ids)
        else:
            # Return all models (both free and paid)
            ids = [m.get("id") for m in models_data if m.get("id")]
            return sorted(ids)

    def key_limits(self, cfg: ProviderConfig) -> dict:
        """Rate/usage info for the key via GET /auth/key, for the UI's daily
        free-request budget readout. OpenRouter does NOT expose how many free
        requests you've spent today — only the cap and the per-interval rate
        limit — so the frontend keeps the spent-today count locally (it ticks on
        every request that reached the server, since an errored response still
        burns the daily free quota). We surface:

          is_free_tier  bool   — False once ≥$10 of credit was ever purchased
          daily_cap     int    — free-model requests/day: 1000 if is_free_tier
                                  has unlocked it, else 50 (OpenRouter's published
                                  tiers; the API gives the flag, not the number)
          rate_requests int    — requests allowed per rate_interval
          rate_interval str    — e.g. "10s"

        Returns {} on any failure (no key, network) so the UI just hides the
        badge. Never raises."""
        if not cfg.api_key:
            return {}
        try:
            resp = httpx.get(
                f"{self._base(cfg)}/auth/key",
                headers=self._headers(cfg),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
        except Exception:
            return {}
        is_free_tier = bool(data.get("is_free_tier", True))
        rl = data.get("rate_limit") or {}
        # ≥$10 credit unlocks the 1000/day free-model tier; otherwise it's 50/day.
        daily_cap = 50 if is_free_tier else 1000
        return {
            "is_free_tier": is_free_tier,
            "daily_cap": daily_cap,
            "rate_requests": rl.get("requests"),
            "rate_interval": rl.get("interval"),
        }
