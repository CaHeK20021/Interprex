"""Provider registry. Add a backend by importing it and listing it here; the
frontend dropdown is driven by list_providers()."""

from __future__ import annotations

import httpx

def _patched_get(url, *args, **kwargs):
    with httpx.Client(trust_env=False) as client:
        return client.get(url, *args, **kwargs)

def _patched_post(url, *args, **kwargs):
    with httpx.Client(trust_env=False) as client:
        return client.post(url, *args, **kwargs)

httpx.get = _patched_get
httpx.post = _patched_post

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
    OpenRouterProvider,
)
from .gemini import GeminiProvider

REGISTRY: list[type[BaseProvider]] = [
    OllamaProvider,
    LMStudioProvider,
    KaggleProvider,
    GeminiProvider,
    OpenRouterProvider,
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
