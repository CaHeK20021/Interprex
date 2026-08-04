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
from .twine import TwineParser
from .sdf7d2d import Sdf7d2dParser
from .skyrim import SkyrimParser

# Order matters only for detect(): first match wins. RPG Maker and Ren'Py key
# off different marker files/dirs, so the order between them is not significant.
# Skyrim sits before Unity: a Data/ dump of .esp is unambiguous and must not
# be swallowed by a looser detector.
REGISTRY: list[type[BaseParser]] = [
    RpgMakerParser,
    RenPyParser,
    CSharpParser,
    I18nParser,
    FusionParser,
    Mmf2Parser,
    QspParser,
    SkyrimParser,
    UnrealEngine4_5Parser,
    UnrealParser,
    Sdf7d2dParser,
    UnityParser,
    TwineParser,
]


def detect_engine(root: str) -> str | None:
    for cls in REGISTRY:
        try:
            if cls.detect(root):
                return cls().engine
        except Exception:
            # A detector may assume `root` is a directory (most do). Loose
            # plugin files (.esp) are valid roots for Skyrim — skip detectors
            # that choke on a file path rather than aborting the whole scan.
            continue
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
