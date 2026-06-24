"""Provider registry. Add a backend by importing it and listing it here; the
frontend dropdown is driven by list_providers()."""

from __future__ import annotations

import httpx

# Globally override httpx.Client and AsyncClient constructors to force trust_env=False.
# This ensures that all HTTP requests (including helper functions like httpx.get/post
# and manual Client instances) bypass registry/environment proxy settings, matching curl.exe.
_orig_client_init = httpx.Client.__init__
def _patched_client_init(self, *args, **kwargs):
    kwargs["trust_env"] = False
    _orig_client_init(self, *args, **kwargs)
httpx.Client.__init__ = _patched_client_init

_orig_async_client_init = httpx.AsyncClient.__init__
def _patched_async_client_init(self, *args, **kwargs):
    kwargs["trust_env"] = False
    _orig_async_client_init(self, *args, **kwargs)
httpx.AsyncClient.__init__ = _patched_async_client_init

from .base import (
    BaseProvider,
    Calibrator,
    ProviderConfig,
    TranslateItem,
    TranslateResult,
    Usage,
)
from .openai_compat import (
    OllamaProvider,
    LMStudioProvider,
    KaggleProvider,
    CustomProvider,
    OpenRouterProvider,
)
from .gemini import GeminiProvider

REGISTRY: list[type[BaseProvider]] = [
    OllamaProvider,
    LMStudioProvider,
    KaggleProvider,
    GeminiProvider,
    OpenRouterProvider,
    CustomProvider,
]


def get_provider(name: str) -> BaseProvider:
    for cls in REGISTRY:
        if cls.name == name:
            return cls()
    raise ValueError(f"unknown provider {name!r}")


def list_providers() -> list[str]:
    return [cls.name for cls in REGISTRY]


__all__ = [
    "BaseProvider",
    "Calibrator",
    "ProviderConfig",
    "TranslateItem",
    "TranslateResult",
    "Usage",
    "REGISTRY",
    "get_provider",
    "list_providers",
]
