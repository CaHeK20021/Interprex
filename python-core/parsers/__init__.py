"""Parser registry. Add a new engine by importing its class and listing it in
REGISTRY — autodetect and dispatch pick it up automatically."""

from __future__ import annotations

from .base import BaseParser, TranslationString, make_id
from .renpy import RenPyParser
from .rpgmaker import RpgMakerParser
from .csharp import CSharpParser
from .unity import UnityParser
from .i18n import I18nParser
from .fusion import FusionParser
from .mmf2 import Mmf2Parser
from .qsp import QspParser
from .unreal import UnrealParser
from .unreal4_5 import UnrealEngine4_5Parser

# Order matters only for detect(): first match wins. RPG Maker and Ren'Py key
# off different marker files/dirs, so the order between them is not significant.
REGISTRY: list[type[BaseParser]] = [
    RpgMakerParser,
    RenPyParser,
    CSharpParser,
    I18nParser,
    FusionParser,
    Mmf2Parser,
    QspParser,
    UnrealEngine4_5Parser,
    UnrealParser,
    UnityParser,
]


def detect_engine(root: str) -> str | None:
    for cls in REGISTRY:
        if cls.detect(root):
            return cls().engine
    return None


def get_parser(engine: str) -> BaseParser:
    for cls in REGISTRY:
        inst = cls()
        if inst.engine == engine:
            return inst
    raise ValueError(f"no parser for engine {engine!r}")


__all__ = [
    "BaseParser",
    "TranslationString",
    "make_id",
    "REGISTRY",
    "detect_engine",
    "get_parser",
]
