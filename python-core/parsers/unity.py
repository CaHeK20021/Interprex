"""Parser for Unity.

Multi-source registry (see ``unity_sources.py``): detect capabilities → run
only matching sources → one ``TranslationString`` contract. Sources:

  dll · typetree_text · content_blob · ui_raw · yaml_prefab · string_table
  (+ font_gap on inject)

Games differ by *which* sources fire, not by per-title forks. Adding a layout
= new source id + detect + extract/inject + selftest.

Inject: typetree save when possible; otherwise rebuild MonoBehaviour raw with
length-changing string slots + UnityPy set_raw_data (no truncate-to-EN-length).
"""

from __future__ import annotations

import os
import sys
import re
import json
import struct
import tempfile
import subprocess
from typing import Any
from collections.abc import Generator
from .base import BaseParser, TranslationString, make_id
from .unity_sources import (
    SOURCE_CONTENT,
    SOURCE_DLL,
    SOURCE_FONT_GAP,
    SOURCE_STRING_TABLE,
    SOURCE_TYPETREE,
    SOURCE_UI_RAW,
    SOURCE_YAML,
    UnityPipelineReport,
    detect_unity_capabilities,
    plan_sources,
)

# DEBUG: Test UnityPy import at startup and log traceback on failure
try:
    import UnityPy
except Exception as e:
    import traceback
    print(f"DEBUG: UnityPy global import test failed: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)

# Skip namespaces to avoid library string noise in assets
SKIP_NAMESPACES = {
    "UnityStandardAssets",
    "ProBuilder",
    "UnityEngine.ProBuilder",
    "UnityEngine.Timeline",
    "UnityEngine.Playables",
    "Unity.Collections",
    "Unity.TextMeshPro",
    "TMPro",
    "Unity.Analytics",
    "Unity.Services",
    "Newtonsoft.Json",
    "AstarPathfindingProject",
    "Pathfinding",
    "Naninovel",
    "Elringus",
}

IGNORE_DIRS = {
    "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups", "__macosx"
}

# Markers: MonoBehaviour raw that holds STORY/LOCALIZATION text (not engine UI chrome).
# Universal — any middleware that bakes dialogue into serialized script assets.
_CONTENT_MARKERS: tuple[bytes, ...] = (
    b"GenericTextScriptLine",   # Naninovel Script
    b"LabelScriptLine",
    b"CommandScriptLine",
    b"CommentScriptLine",
    b"mTerms",                  # I2 Localization
    b"mTermData",
    b"TermData",
    b"DialogueEntry",           # Dialogue System / similar
    b"Yarn.Unity",
    b"YarnProgram",
    b"InkList",
    b"LocalizedStringTable",
    b"StringTableEntry",
    b"ScriptableDialogue",
)

# Config/locale blobs that look "texty" but must not enter the table.
_SKIP_BLOB_MARKERS: tuple[bytes, ...] = (
    b"CultureInfo",
    b"ManagedTextConfiguration",
    b"ResourceProviderConfiguration",
)

# Soft size gate for "UI chrome" raw pass (prefab text, menus). Content blobs ignore this.
_UI_RAW_MAX_SIZE = 48_000

def should_skip_type(type_full_name: str) -> bool:
    if not type_full_name:
        return False
    for ns in SKIP_NAMESPACES:
        if type_full_name.startswith(ns + ".") or type_full_name == ns:
            return True
    return False

def is_custom_dll(filename: str) -> bool:
    """True if the DLL is likely game/mod code (not engine, middleware, or runtime).

    Always keeps Assembly-CSharp*. Rejects known Unity/middleware stacks so
    Naninovel/TMP/Mathematics error strings never pollute the extract table.
    Unknown third-party mod DLLs still pass (deny-list, not allow-list).
    """
    fn = filename.lower()
    if not fn.endswith(".dll"):
        return False

    # Game scripts — always
    if fn.startswith("assembly-csharp"):
        return True

    # Exact runtime / player binaries that used to slip through
    if fn in (
        "unityplayer.dll", "baselib.dll", "gameassembly.dll",
        "unitycrashhandler64.dll", "unitycrashhandler32.dll",
    ):
        return False
    if fn.startswith("mono"):
        return False

    deny_prefixes = (
        "system.", "microsoft.", "unityengine.", "unityeditor.", "unity.",
        "mscorlib", "netstandard", "mono.", "newtonsoft", "fastjson", "nlog", "log4net",
        "protobuf", "steamworks", "epoxy", "i2local", "customui", "harmony",
        "bepinex", "accessibility", "webconnection", "sqlite", "mysql", "audiotoolbox",
        "qsp", "fmod", "sdl", "openal", "openvr", "softpcg",
        # VN / middleware engines (strings are framework errors, not game text)
        "elringus.", "naninovel.", "naninovel",
        "spine.", "DOTween", "dotween", "rewired", "photon", "mirror.",
        "facepunch", "com.unity.", "unityengine",
    )
    for pref in deny_prefixes:
        if fn.startswith(pref.lower()):
            return False
    # Unity.* modules ship as Unity.Mathematics.dll etc.
    if fn.startswith("unity"):
        return False
    return True

# ── Compiled regexes ────────────────────────────────────────────────────────
_GUID_RE          = re.compile(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$', re.I)
_VERSION_RE       = re.compile(r'^v?\d+(\.\d+){2,}([.\-+]\w+)?$')
_HEX_HASH_RE      = re.compile(r'^[0-9a-fA-F]{12,}$')
_URL_RE           = re.compile(r'https?://|www\.')
_RICH_TAG_RE      = re.compile(r'</?[a-zA-Z#/][^<>]*>')  # TMP/Naninovel rich text
_EXT_RE           = re.compile(
    r'\.(png|jpg|jpeg|gif|bmp|tga|wav|mp3|ogg|mp4|avi'
    r'|prefab|unity|asset|shader|mat|anim|controller'
    r'|cs|dll|exe|json|xml|yaml|csv|meta)$', re.I
)
_PLACEHOLDER_RE   = re.compile(r'^[\s{}\d,:|%\-]+$')   # pure {0} {1} etc.
_LOG_TAG_RE       = re.compile(r'^\[(DEBUG|INFO|WARNING|ERROR|WARN|FATAL|VERBOSE|TRACE)\]', re.I)
_CODE_WORD_RE     = re.compile(
    r'^[a-z][a-z0-9]*$'                          # lowercase: enabled, name
    r'|^_+\w+$'                                   # _privateField
    r'|^[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+$'     # camelCase
    r'|^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+$'     # PascalCase (2+ части)
    r'|^\w+(?:\.\w+)+$'                           # dotted.namespace
    r'|^[A-Z0-9]{2,}(?:_[A-Z0-9]+)+$'           # SCREAMING_SNAKE
    r'|^(?:\w+_)+\w+$'                           # any_underscore_word
)
_NANO_ID_RE       = re.compile(r'^~[0-9a-f]{6,}$', re.I)
_ASSEMBLY_NAME_RE = re.compile(
    r'Version=\d+\.\d+\.\d+\.\d+|Culture=neutral|PublicKeyToken=|Elringus\.|Naninovel\.',
    re.I,
)
_TYPE_NAME_RE     = re.compile(
    r'^(?:[A-Z][a-zA-Z0-9]+)+(?:ScriptLine|Configuration|Manager|Behaviour|'
    r'Provider|Controller|Handler|Component|Attribute|Exception|Serializer|'
    r'Localizer|Parser|Player|Navigator)$'
)


def _strip_rich_tags(t: str) -> str:
    return _RICH_TAG_RE.sub("", t)


def _looks_like_filesystem_path(t: str) -> bool:
    """True for real asset/FS paths — NOT rich-text closing tags like </i>."""
    plain = _strip_rich_tags(t)
    if "\\" in plain:
        return True
    if re.match(r"^[A-Za-z]:/", plain):
        return True
    if re.search(r"(?:^|[\s\"'])(?:Assets|Resources|StreamingAssets|Packages)/", plain):
        return True
    # multi-segment path ending in a file-like token
    if re.search(r"(?:^|/)\w+(?:/[\w.-]+){2,}", plain) and _EXT_RE.search(plain):
        return True
    return False

# ── Whitelist: однословные UI-метки которые точно нужны ─────────────────────
_KNOWN_UI = frozenset({
    "play", "start", "quit", "exit", "back", "next", "menu", "save",
    "load", "options", "settings", "continue", "resume", "credits",
    "yes", "no", "ok", "cancel", "confirm", "close", "help", "return",
    "pause", "inventory", "map", "journal", "quest", "tutorial",
    "volume", "audio", "graphics", "controls", "language", "new",
    "delete", "accept", "apply", "reset", "buy", "sell", "equip",
    "use", "drop", "craft", "upgrade", "unlock", "replay", "restart",
    "achievements", "leaderboard", "profile", "skip", "score", "level",
    "fullscreen", "windowed", "easy", "normal", "hard", "extreme",
    "collectibles", "tips", "tutorials",
})

# ── Blacklist: однословные слова которые выглядят как текст, но это код ─────
_KNOWN_CODE = frozenset({
    "false", "true", "null", "none", "void", "string", "integer", "boolean",
    "float", "double", "int", "char", "byte",
    "object", "component", "gameobject", "transform",
    "update", "awake", "fixedupdate", "lateupdate",
    "enable", "disable", "active", "enabled", "disabled",
    "manager", "controller", "handler", "provider", "factory", "service",
    "event", "action", "delegate", "callback", "listener", "observer",
    "default", "override", "virtual", "abstract", "static",
    "public", "private", "protected", "internal", "readonly",
    "linear", "easing", "bounce", "elastic",
    "discord", "steam", "firebase", "analytics",
    "debug", "error", "warning", "exception", "log",
    "shader", "material", "texture", "renderer", "collider",
    "rigidbody", "animator", "audiosource", "canvas",
})

# Naninovel command type names as serialized next to "Naninovel.Commands".
# Translating renames the opcode → ScriptPlayer dies (Touchstarved: Goto→RU
# ⇒ Title_Script not found, black title screen). Prefer the sequential
# lookahead rule in `_iter_raw_translatable_slots`; this set is the belt.
_NANINOVEL_COMMANDS = frozenset({
    # flow
    "goto", "gosub", "return", "stop", "wait", "break", "continue",
    "if", "else", "elseif", "endif", "while", "endwhile", "set",
    "beginif", "endif", "processinput", "skipinput", "lockinput", "unlockinput",
    "random", "randomset", "randomstop",
    # text / printer
    "print", "printtext", "resettext", "append", "clearbacklog", "style",
    # actors / scene
    "char", "back", "hide", "show", "arrange", "look", "move", "slide",
    "scale", "rotate", "tint", "animate", "shake", "spawn", "despawn",
    "hideactors", "showactors", "hideall", "showall", "modifycharacter",
    "hideprinter", "showprinter", "hideui", "showui",
    "camera", "shakecamera", "zoom", "ortho", "rollup",
    # audio / video
    "sfx", "bgm", "voice", "playsfx", "stopsfx", "playbgm", "stopbgm",
    "playvoice", "stopvoice", "playmovie", "stopmovie", "movie",
    # fx / choice / vars
    "choice", "addchoice", "overlay", "blur", "glitch", "rain", "snow",
    "sun", "toast", "addtoast", "removealltoast", "waitinput",
    "complete", "title", "mainmenu", "loadgame", "savegame",
    "openurl", "debug", "lipmap", "lipsync", "setcustomvariable",
})

# AudioMixer.SetFloat keys + bare bus names (LLM once mapped Master→«Общая громкость»).
_VOLUME_PARAM_RE = re.compile(
    r"^(?:Master|Music|BGM|SFX|Voice|Voices|Effects|Effect|Ambient|UI|"
    r"Dialog|Dialogue|System|Movie|Video|Foley|Env|Environment)"
    r"\s+[Vv]olume$",
)
_VOLUME_PARAM_LOOSE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9/&+.-]*\s+[Vv]olume$")
_MIXER_BUS_BARE = frozenset({
    "master", "bgm", "sfx", "voice", "voices", "music", "effects", "ambient",
})
# Naninovel script/label/var ids: Vere_Choice, Title_Script, V_patience, EXT_Foo
_NANO_IDENT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+$")
_NANO_VAR_EXPR_RE = re.compile(
    r"[=;]|\+\+|--|^\.|^~"  # V_x=1, a;b, ++, .V_nose, ~hex
)
_NANO_ASSEMBLY_RE = re.compile(
    r"^(?:Naninovel|Elringus)(?:\.|$)|ScriptLine$",
    re.I,
)


def _is_engine_identifier(plain: str) -> bool:
    """True for opcodes / mixer keys / script ids — NEVER translate.

    Wrong positive → string stays English. Wrong negative → bricked game.
    """
    p = (plain or "").strip()
    if not p:
        return False
    low = p.lower()
    compact = low.replace(" ", "")
    if low in _NANINOVEL_COMMANDS or compact in _NANINOVEL_COMMANDS:
        return True
    if low in _MIXER_BUS_BARE:
        return True
    if _VOLUME_PARAM_RE.match(p) or _VOLUME_PARAM_LOOSE_RE.match(p):
        return True
    if _NANO_ASSEMBLY_RE.search(p):
        return True
    if "ScriptLine" in p:
        return True
    # CultureInfo table: "Spanish (El Salvador)"
    if "(" in p and ")" in p and re.match(
        r"^[A-Za-z][A-Za-z\s.'-]*\([A-Za-z][A-Za-z\s.'-]*\)$", p
    ):
        return True
    # Script / label / flag ids (Vere_Choice, Title_Script, EXT_LowtownNight)
    if _NANO_IDENT_RE.match(p) and " " not in p:
        return True
    # Variable expressions / nano-ids baked into command args (no spaces)
    if " " not in p and (
        "=" in p or "++" in p or p.startswith(".") or p.startswith("~")
        or ";" in p
    ):
        return True
    if p in ("true", "false", "True", "False"):
        return True
    return False


def _is_naninovel_script_blob(raw: bytes) -> bool:
    """Naninovel Script MonoBehaviour (story lines), not generic UI."""
    return (
        b"GenericTextScriptLine" in raw
        or b"CommandScriptLine" in raw
        or b"LabelScriptLine" in raw
    )


def _is_valid_unity_text_string(s: str) -> bool:
    """Accept human text stored as Unity AlignedString UTF-8.

    ⚠️ ``str.isprintable()`` is WRONG here: it rejects ``\\n`` / ``\\r`` / ``\\t``,
    so multi-paragraph dialogue never entered the slot walk. Real Touchstarved
    miss (2026-08): character-creation backstories like
    ``\"You were raised as an oracle…\\n\\nYou regularly…\"`` sat in
    ``resources.assets`` but extract returned 0 hits → stayed English after a
    full translate. Allow common whitespace; still reject NULs and other
    control bytes (binary false-positives).
    """
    if not s or "\x00" in s or not s.strip():
        return False
    for ch in s:
        o = ord(ch)
        if ch in "\n\r\t":
            continue
        # Same idea as str.isprintable, without killing paragraph breaks.
        if o < 32 or o == 0x7F:
            return False
    return True


def _sequential_aligned_strings(raw: bytes) -> list[tuple[int, str]]:
    """Non-overlapping left-to-right Unity AlignedString walk (path-stable)."""
    out: list[tuple[int, str]] = []
    i = 0
    n = len(raw)
    while i + 4 < n:
        slen = struct.unpack_from("<I", raw, i)[0]
        if 1 <= slen <= 8000 and i + 4 + slen <= n:
            chunk = raw[i + 4 : i + 4 + slen]
            try:
                s = chunk.decode("utf-8")
                if _is_valid_unity_text_string(s):
                    out.append((i, s))
                    i = _aligned_string_end(i, slen)
                    continue
            except (UnicodeDecodeError, ValueError):
                pass
        i += 1
    return out


def _is_player_facing_raw(text: str, *, naninovel: bool) -> bool:
    """Strict player-facing gate. Under-extract > brick.

    Naninovel: multi-word / rich-text / ALL-CAPS UI only — never bare Title-case
    actor names (those double as command targets). Other content blobs: same
    multi-word bias; ALL-CAPS UI still ok.
    """
    t = text.strip()
    if not t or not _is_game_text_raw(t):
        return False
    if _is_engine_identifier(t):
        return False
    plain = _strip_rich_tags(t).strip()
    if not plain:
        return False
    # Multi-word or multi-line = dialogue / sentence UI
    if " " in plain or "\n" in plain:
        return True
    # ALL-CAPS button chrome (SAVE, LOAD) — display only
    if plain.isupper() and 2 <= len(plain) <= 20 and "_" not in plain:
        return True
    if plain.lower() in _KNOWN_UI:
        return True
    # Punctuated short choice / interjection ("About...?", "Shame.")
    if any(c in plain for c in "?!…") and len(plain) >= 3:
        return True
    # Naninovel: refuse bare single tokens (Vere, MC, Goto-lookalikes)
    if naninovel:
        return False
    # Non-naninovel content: still refuse short Title-case (was sucking Master/Goto)
    if plain[0].isupper() and plain[1:].islower() and len(plain) <= 24:
        return False
    return False


def _iter_raw_translatable_slots(raw: bytes) -> list[tuple[int, str]]:
    """Production raw-slot picker: same walk for extract AND inject.

    Naninovel Script blobs: sequential strings; skip any token whose *next*
    string is ``Naninovel.Commands`` (that token IS the command type name —
    ground truth from Touchstarved dumps). Also skip assembly / ScriptLine /
    ids / var exprs.

    Other content: sequential slots that pass the player-facing gate.
    """
    nano = _is_naninovel_script_blob(raw)
    seq = _sequential_aligned_strings(raw)
    out: list[tuple[int, str]] = []
    for i, (offset, s) in enumerate(seq):
        t = s.strip()
        if not t:
            continue
        nxt = seq[i + 1][1].strip() if i + 1 < len(seq) else ""
        # Opcode sits immediately before the Commands assembly marker.
        if nxt == "Naninovel.Commands" or nxt.startswith("Naninovel.Commands"):
            continue
        if nxt.startswith("Naninovel.") and "Command" in nxt:
            continue
        if not _is_player_facing_raw(t, naninovel=nano):
            continue
        out.append((offset, s))
    return out


def _is_game_text(text: str) -> bool:
    t = text.strip()

    # ── Базовые проверки ────────────────────────────────────────────────────
    if len(t) < 2:
        return False
    if not any(c.isalpha() for c in t):
        return False

    plain = _strip_rich_tags(t).strip()
    if not plain or not any(c.isalpha() for c in plain):
        return False

    # ── Жесткие исключения ──────────────────────────────────────────────────
    if _GUID_RE.match(t):           return False
    if _VERSION_RE.match(t):        return False
    if _HEX_HASH_RE.match(t):       return False
    if _URL_RE.search(t):           return False
    if _looks_like_filesystem_path(t): return False
    if _EXT_RE.search(plain) and "/" not in t and "\\" not in t and "<" not in t:
        # bare "foo.png" — skip; rich-text with tags already stripped above
        if " " not in plain:
            return False
    if _PLACEHOLDER_RE.match(t):    return False
    if _LOG_TAG_RE.match(t):        return False
    if _ASSEMBLY_NAME_RE.search(t): return False
    if _NANO_ID_RE.match(t):        return False
    if _TYPE_NAME_RE.match(t):      return False
    if "lorem ipsum" in t.lower():  return False
    # BEFORE the multi-word / Title-case accept rules — "Goto", "Master Volume"
    # look like normal text but are engine identifiers.
    if _is_engine_identifier(plain): return False

    # ── Быстрый пропуск: очевидно человеческий текст ───────────────────────
    if " " in plain or "\n" in plain: return True   # многословный / диалог
    if plain.lower() in _KNOWN_UI:  return True   # известная UI-метка

    # ── Одно слово: усиленный фильтр ────────────────────────────────────────
    if _CODE_WORD_RE.match(plain):  return False
    if plain.lower() in _KNOWN_CODE: return False

    # Все-капсовое короткое слово без подчеркиваний -> кнопка UI (PLAY, EXIT)
    if plain.isupper() and len(plain) <= 20 and "_" not in plain:
        return True

    # Title-case singles: no auto-accept (Goto / Master / actor ids).
    return False


# ── Length-prefix fallback: stricter filter for raw MonoBehaviour bytes ──────

# Compiled patterns for raw extraction
_REPEATED_CHAR_RE  = re.compile(r'^(.)\1{4,}')        # 5+ same char at start: "aaaaa", "DESCDES"
_REPEATED_WORD_RE  = re.compile(r'(\b\w+\b)(\s+\1){2,}') # word repeated 3+ times
_CREDIT_LIST_RE    = re.compile(
    r'^[A-Za-z0-9_.]{2,}(?:\s*,\s*[A-Za-z0-9_.]{2,}){3,}$'  # 4+ comma-separated tokens
)
_ASSET_SUFFIX_RE   = re.compile(
    r'\b(SDF|_cl|_op|_default|Profile|Track|Clip|Behaviour|Component'
    r'|Controller|Renderer|Filter|Canvas|Mesh|Sprite|Asset'
    r'|Font|Material|Shader|Animation|Animator|AudioSource'
    r'|TMP_|TextMesh)\b', re.I
)
_PLACEHOLDER_WORD_RE = re.compile(
    r'^(New Text|Option [A-C]|Test\d*|test|PLACEHOLDER|TODO|FIXME'
    r'|asdasd|descdesc|lorem|ipsum|dummy|sample|foo|bar|baz'
    r'|qwe|zxc|asd|fff|xxx|zzz|aaa|bbb|ccc|ddd|eee|ggg|hhh|iii|jjj|kkk'
    r'|lll|mmm|nnn|ooo|ppp|qqq|rrr|sss|ttt|uuu|vvv|www|yyy)+$', re.I
)
_GIBBERISH_RE       = re.compile(
    r'(?:asd|qwe|zxc|ghj|foo|bar|baz|fff|xxx|zzz|test)\w{0,5}', re.I
)
_REPEATED_SUBSTR_RE = re.compile(r'(\w{3,})\1{2,}')  # "DESCDESCDESC", "asdasdasd"
_VERSION_RAW_RE    = re.compile(
    r'^v\d+(\.\d+){1,3}(\s*\(.*\))?$'
)
_INTERNAL_ID_RE    = re.compile(
    r'^\d+[.,]\s*\w+$'        # "1, LetsGo" type
    r'|^\w+(?:Morph|State|Node|Event|Step|Phase)\s*\d*$'  # "GamePlayerLostMorph 1"
)
_FONT_NAME_RE = re.compile(
    r'^(Roboto|Lato|Kinkie|Liberation\s+Sans|Open\s+Sans|Montserrat'
    r'|Poppins|Oswald|Anton|Bangers|Electronic\s+Highway\s+Sign'
    r'|Noto\s+Sans|Droid\s+Sans|Source\s+Sans|Fira\s+Sans'
    r'|Raleway|Merriweather|Ubuntu|PT\s+Serif|PT\s+Sans'
    r'|Play\s+Display|Playfair|Nunito|Quicksand|Work\s+Sans'
    r'|Barlow|Inter|DM\s+Sans|Manrope|Lexend|Outfit|Space\s+Grotesk'
    r'|Redacted|Comic\s+Neue|Grandstander|Fugaz|Bungee|Rubik'
    r'|Comfortaa|Righteous|Volkhov|Vollkorn|Alegreya|Gentium'
    r'|Cormorant|Crimson|EB\s+Garamond|Libre\s+Baskerville'
    r'|Spectral|Bitter|Zilla|IBM\s+Plex|Fira|Inconsolata'
    r'|JetBrains|Source\s+Code|Hack|Consolas|Courier|Courier\s+New'
    r'|Times|Georgia|Garamond|Palatino|Book\s+Antiqua'
    r'|Calibri|Cambria|Candara|Corbel|Segoe)\b', re.I
)
_UNITY_INTERNAL_RE = re.compile(
    r'^(UnityEngine|UnityEditor|Unity\.|Unity\w+\.Runtime'
    r'|MonoBehaviour|GameObject|Transform|Canvas|CanvasGroup'
    r'|RectTransform|MeshRenderer|MeshFilter|Collider'
    r'|Rigidbody|AudioSource|AudioListener|Camera'
    r'|Light|ParticleSystem|Animator|Animation|SpriteRenderer'
    r'|Debug|EventSystem|EventTrigger|GraphicRaycaster'
    r'|ScrollRect|GridLayout|HorizontalLayout|VerticalLayout'
    r'|LayoutElement|ContentSizeFitter|Image|RawImage|Button'
    r'|Toggle|Slider|Scrollbar|InputField|Dropdown'
    r'|TMP_|TextMeshPro|TextMesh)\b'
)
_PRIMITIVE_RE = re.compile(
    r'^(Cube|Sphere|Capsule|Cylinder|Plane|Quad|Terrain)$', re.I
)
_GLYPH_NAME_RE = re.compile(
    r'^(Zero|One|Two|Three|Four|Five|Six|Seven|Eight|Nine|Ten'
    r'|Exclamation|Question|Period|Comma|Colon|Semicolon'
    r'|Apostrophe|Quote|Hyphen|Underscore|Slash|Backslash'
    r'|Space|At|Hash|Dollar|Percent|Ampersand|Asterisk'
    r'|Plus|Equal|Less|Greater|Pipe|Tilde|Caret)$', re.I
)


def _is_game_text_raw(text: str) -> bool:
    """Stricter filter for strings extracted via length-prefix from raw bytes.

    Builds on _is_game_text but adds guards against common Unity asset junk
    that only shows up in raw extraction (no typetree context).
    Rich-text tags (<i>, </color>, …) are allowed — slash-in-tag is NOT a path.
    """
    t = text.strip()

    # ── Base checks ────────────────────────────────────────────────────────
    if len(t) < 3:
        return False
    if not any(c.isalpha() for c in t):
        return False

    plain = _strip_rich_tags(t).strip()
    if not plain or not any(c.isalpha() for c in plain):
        return False

    if _GUID_RE.match(t):              return False
    if _VERSION_RE.match(t):           return False
    if _VERSION_RAW_RE.match(t):       return False
    if _HEX_HASH_RE.match(t):          return False
    if _URL_RE.search(t):              return False
    if _looks_like_filesystem_path(t): return False
    if _EXT_RE.search(plain) and " " not in plain and "<" not in t:
        return False
    if _LOG_TAG_RE.match(t):           return False
    if _ASSEMBLY_NAME_RE.search(t):    return False
    if _NANO_ID_RE.match(t):           return False
    if _TYPE_NAME_RE.match(t):         return False
    # Naninovel opcodes / AudioMixer params — must run before multi-word accept.
    # (Goto / Master Volume look human but brick ScriptPlayer / mixer.)
    if _is_engine_identifier(plain):   return False

    low = t.lower()
    plain_low = plain.lower()

    # ── Hard rules: underscore = internal identifier, never dialogue ────────
    # Apply on tag-stripped text so "<color=…>foo_bar</color>" still rejected,
    # but dialogue with no underscore passes even if tags contain '='.
    if "_" in plain:
        return False

    # ── Hard rule: "Unity" as engine token (not the word inside a sentence) ──
    if plain_low == "unity" or plain_low.startswith("unityengine") or plain_low.startswith("unity."):
        return False

    # ── Hard rule: assembly references ("UnityEngine...", "Version=0.0.0.0") ─
    if ", assembly-" in low or ", unity." in low or ", unityengine" in low:
        return False
    if "version=0.0.0.0" in low or "culture=neutral" in low:
        return False

    # ── Framework / placeholder chrome (Naninovel defaults, TMP samples) ───
    if "lorem ipsum" in plain_low:
        return False
    if plain_low in (
        "naninovel", "choice text", "actor name", "tip title", "tip category",
        "memory usage text", "confirmation message.", "new text",
        "select a script to play", "panel title", "dialogue options",
        "1. lorem ipsum",
    ):
        return False

    # ── Placeholder / gibberish ─────────────────────────────────────────────
    if _PLACEHOLDER_WORD_RE.match(low):    return False
    if _REPEATED_CHAR_RE.match(low):       return False
    if _REPEATED_WORD_RE.search(low):      return False
    if _REPEATED_SUBSTR_RE.search(low):    return False  # "DESCDESCDESC"

    # Gibberish prefixes: "ASD HELLO", "ghj ghj", "qwe asd"
    if _GIBBERISH_RE.match(low):           return False

    # Repeated words (3+ same word): "BREAKING NEWS BREAKING NEWS BREAKING NEWS"
    words = low.split()
    if len(words) >= 6:
        unique = set(words)
        if len(unique) < len(words) * 0.4:
            return False

    # ── Asset / engine names ────────────────────────────────────────────────
    if _ASSET_SUFFIX_RE.search(t):         return False
    if _CODE_WORD_RE.match(t):             return False
    if t.lower() in _KNOWN_CODE:           return False

    # ── Font names ──────────────────────────────────────────────────────────
    if _FONT_NAME_RE.search(t):            return False

    # ── Unity internal types / primitives / glyph names ─────────────────────
    if _UNITY_INTERNAL_RE.match(t):        return False
    if _PRIMITIVE_RE.match(t):             return False
    if _GLYPH_NAME_RE.match(t):            return False

    # ── TMP / UI state single-word blacklist ────────────────────────────────
    if low in frozenset({
        "normal", "highlighted", "pressed", "selected", "disabled",
        "foldout", "button", "toggle", "slider", "scrollbar",
        "header", "message", "text", "name", "stage", "stage:",
        "continue", "reset", "enum", "leftclick", "rightclick",
        "bold", "italic", "regular", "light", "thin", "medium",
        "extra", "black", "white", "empty",
        "bloom", "vignette", "tonemapping", "depthoffield",
        "slot", "dialogue", "panel",
        "dropcap", "numbers",
        "style", "sheet", "settings",
        "alt", "ctrl", "shift", "tab", "escape", "return", "delete",
        "vertical", "horizontal", "submit", "cancel",
        "beer", "wine", "gin", "milk", "bread", "eggs", "chips",
        "oranges", "tomatoes", "laptop", "naked", "out", "party",
        "pushed", "toilet", "work", "university", "groceries",
        "position", "link", "quote", "title",
        "smiley", "wink", "whaaat!",
        "new text", "option a", "option b", "option c",
        "tmp settings", "default style sheet", "default sprite asset",
        "panel title", "dialogue options", "drinking hint",
        "scene name", "slot 1", "char name", "message text",
        "storage room bj", "start massage", "continue massage",
        "next foot",
        "blue to purple - vertical", "dark to light green - vertical",
        "light to dark green - vertical", "yellow to orange - vertical",
        "red:", "yellow:", "blue:", "green:", "white:", "black:",
        "automatic control", "manual control",
        "break", "test2",
    }):
        return False

    # ── Multi-line blacklist (long junk that's not dialogue) ────────────────
    if low in frozenset({
        "i can not decline this call",
        "hold and move in circular motion",
        "your phone is ringing. close the quest window and press 'o' to open your phone",
        "when drinking alone and finishing a drink, the other character (including the player) finishes theirs as well but only gains one drunk point",
        "continue the story (indicates important decision or skip the side dialogues)",
        "unlocked dialogue through interactions in the world and other dialogues",
        "regular dialogue (side dialogue)",
        "starts sexual scene",
        "ghj ghj", "test 1 test 1", "asd hello",
    }):
        return False

    # ── Emoji descriptions from TMP ─────────────────────────────────────────
    if re.match(r'^(smiling|grinning|face with|winking|pouting|anguished|'
                r'confounded|disappointed|fearful|joy|sad|thinking|'
                r'neutral|expressionless|unamused|sweat|weary|'
                r'clock face|skull|pile of poo|clapping|heart eyes|'
                r'raised hand|ok hand|thumbs|folded|waving|'
                r'muscle|sparkles|fire|star|rainbow|sun|moon|'
                r'check mark|cross mark|warning|question|exclamation|'
                r'multiplication|bangbang|heart|broken|two hearts|'
                r'black|white|red|blue|green|yellow|purple|orange)\b', low):
        return False

    # ── Font character range like "20-7E,A0,2026" ──────────────────────────
    if re.match(r'^[0-9A-Fa-f]{2,4}(?:-[0-9A-Fa-f]{2,4})?(?:,[0-9A-Fa-f]{2,4}(?:-[0-9A-Fa-f]{2,4})?)*$', t):
        return False

    # ── Credit lists ────────────────────────────────────────────────────────
    if _CREDIT_LIST_RE.match(t):           return False

    # ── Internal IDs ────────────────────────────────────────────────────────
    if _INTERNAL_ID_RE.match(t):           return False

    # ── Comma-separated short tokens (credit lists, asset lists) ────────────
    if ',' in t:
        parts = [p.strip() for p in t.split(',')]
        if len(parts) >= 4 and all(len(p) <= 20 for p in parts):
            return False

    # ── Long all-same-case string with no real words ────────────────────────
    alpha_only = ''.join(c for c in t if c.isalpha())
    if len(alpha_only) > 10:
        # Count transitions between upper/lower
        transitions = sum(1 for i in range(1, len(alpha_only))
                         if alpha_only[i].isupper() != alpha_only[i-1].isupper())
        if transitions < 2 and not t.isupper():
            # Mostly one case with no word boundaries → likely junk
            pass  # keep it — could be all-caps UI like "SPACE"

    # ── Standard pass-through ───────────────────────────────────────────────
    if " " in plain or "\n" in plain:      return True   # multi-word → likely dialogue/UI
    if plain_low in _KNOWN_UI:             return True

    # ALL CAPS button label
    if plain.isupper() and len(plain) <= 20 and "_" not in plain:
        return True

    # Title-case single word: DO NOT auto-accept here. That rule sucked in
    # Naninovel opcodes (Goto) and mixer bus names (Master) on Touchstarved.
    # Player-facing singles go through `_is_player_facing_raw` / UI whitelist.
    return False


def _extract_length_prefixed(raw: bytes) -> list[str]:
    """Extract all length-prefixed UTF-8 strings from Unity serialized bytes."""
    return [s for _, s in _extract_length_prefixed_at(raw)]


def _extract_length_prefixed_at(raw: bytes) -> list[tuple[int, str]]:
    """Like _extract_length_prefixed but returns (byte_offset, string) pairs.

    Offset points at the int32 length prefix — used as the stable inject address.
    Scans every byte (Unity blobs interleave strings with binary); overlaps are
    possible in theory, so callers should prefer non-overlapping left-to-right.
    """
    out: list[tuple[int, str]] = []
    i = 0
    n = len(raw)
    while i + 4 < n:
        slen = struct.unpack_from("<I", raw, i)[0]
        if 2 <= slen <= 5000 and i + 4 + slen <= n:
            chunk = raw[i + 4 : i + 4 + slen]
            try:
                s = chunk.decode("utf-8")
                if _is_valid_unity_text_string(s):
                    out.append((i, s))
            except (UnicodeDecodeError, ValueError):
                pass
        i += 1
    return out


def _aligned_string_end(offset: int, slen: int) -> int:
    """Byte offset after a Unity AlignedString (length + data + pad to 4)."""
    end = offset + 4 + slen
    return (end + 3) & ~3


def _pack_aligned_string(text: str) -> bytes:
    data = text.encode("utf-8")
    blob = struct.pack("<I", len(data)) + data
    pad = (4 - (len(blob) % 4)) % 4
    if pad:
        blob += b"\x00" * pad
    return blob


def _blob_is_content(raw: bytes) -> bool:
    """True if this MonoBehaviour looks like story/localization payload."""
    if any(m in raw for m in _CONTENT_MARKERS):
        return True
    # Density heuristic: many multi-word prose strings → dialogue-ish asset
    if len(raw) < 8_000:
        return False
    hits = 0
    for _, s in _extract_length_prefixed_at(raw):
        t = s.strip()
        if len(t) >= 40 and " " in t and _is_game_text_raw(t):
            hits += 1
            if hits >= 12:
                return True
    return False


def _blob_is_skip_config(raw: bytes) -> bool:
    if any(m in raw for m in _CONTENT_MARKERS):
        return False
    return any(m in raw for m in _SKIP_BLOB_MARKERS)


def find_aa_dir(root: str) -> str | None:
    """Find the StreamingAssets/aa directory in the project root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORE_DIRS and not d.startswith(".")]
        if os.path.basename(dirpath) == "StreamingAssets":
            aa_path = os.path.join(dirpath, "aa")
            if os.path.isdir(aa_path):
                return aa_path
    return None

def find_aa_bundles(root: str) -> list[str]:
    """Find all bundle files inside StreamingAssets/aa."""
    bundles = []
    aa_dir = find_aa_dir(root)
    if not aa_dir:
        return bundles
    for dirpath, dirnames, filenames in os.walk(aa_dir):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.endswith(".bundle"):
                bundles.append(os.path.join(dirpath, f))
    return bundles

def find_managed_dir(root: str) -> str | None:
    """Find the Managed directory containing Assembly-CSharp.dll."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORE_DIRS and not d.startswith(".")]
        if os.path.basename(dirpath) == "Managed":
            if any(f.lower() == "assembly-csharp.dll" for f in filenames):
                return dirpath
    return None

def _parse_multiline_yaml_val(lines: list[str], start_line_idx: int, initial_raw_val: str) -> tuple[str | None, int]:
    line_idx = start_line_idx
    raw_val = initial_raw_val
    # Handle multiline double-quoted values
    if raw_val.startswith('"') and not (raw_val.endswith('"') and len(raw_val) > 1 and raw_val[-2] != '\\'):
        accumulated = [raw_val]
        line_idx += 1
        while line_idx < len(lines):
            next_line = lines[line_idx]
            accumulated.append(next_line)
            if next_line.endswith('"') and (len(next_line) == 1 or next_line[-2] != '\\'):
                break
            line_idx += 1
        raw_val = "\n".join(accumulated)
    # Handle multiline single-quoted values
    elif raw_val.startswith("'") and not (raw_val.endswith("'") and len(raw_val) > 1):
        accumulated = [raw_val]
        line_idx += 1
        while line_idx < len(lines):
            next_line = lines[line_idx]
            accumulated.append(next_line)
            if next_line.endswith("'"):
                break
            line_idx += 1
        raw_val = "\n".join(accumulated)

    if not raw_val:
        return None, line_idx

    val = None
    if raw_val.startswith('"') and raw_val.endswith('"'):
        try:
            val = json.loads(raw_val)
        except Exception:
            val = raw_val[1:-1]
    elif raw_val.startswith("'") and raw_val.endswith("'"):
        val = raw_val[1:-1].replace("''", "'")
    else:
        val = raw_val
    return val, line_idx

def iter_files(root: str, sub_paths: list[str] | None = None) -> Generator[str, None, None]:
    paths_to_walk = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
    for start_path in paths_to_walk:
        for dirpath, dirnames, filenames in os.walk(start_path):
            dirnames[:] = [d for d in dirnames if d.lower() not in IGNORE_DIRS and not d.startswith(".")]
            for f in filenames:
                if f.startswith(".") or f.startswith("._"):
                    continue
                yield os.path.join(dirpath, f)


# ── Font script coverage (TMP SDF + Unity Font) ─────────────────────────────
# Touchstarved-class bug: text is injected as RU, but TMP static SDF atlases only
# baked Latin (Baskervald "32-126") → in-game only punctuation renders.
# Fix = size-preserving PPtr redirect from fonts that LACK the target script to
# an in-file donor that HAS it. Conservative by design so other Unity games that
# already ship proper glyphs are never touched:
#   • Latin / unknown targets → no-op
#   • no in-file donor with positive coverage → no-op (never invent a font)
#   • fonts whose coverage we can't prove → leave alone
#   • only PPtrs whose path_id is a proven-missing victim are rewritten

# Sample codepoints we require for "covers this script" (not full blocks).
_SCRIPT_SAMPLE: dict[str, str] = {
    "cyrillic": "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯабвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    "cjk": "的一是不了人我在有他这中大来上国",  # common CJK ideographs
    "japanese": "あいうえおかきくけこアイウエオ日本語",
    "korean": "가나다라마바사아자차카타파하한글",
    "arabic": "ابجدهوزحطيكلمنسعفصقرشتثخذضظغ",
    "hebrew": "אבגדהוזחטיכלמנסעפצקרשת",
    "thai": "กขคงจฉชซญดตถทนบปผพฟมยรลวสหอ",
    "greek": "ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩαβγδεζηθικλμνξοπρστυφχψω",
}

# Display names from the UI ("Russian") + short codes + aliases → script key.
_LANG_TO_SCRIPT: dict[str, str] = {
    "ru": "cyrillic", "russian": "cyrillic", "uk": "cyrillic", "ukrainian": "cyrillic",
    "be": "cyrillic", "belarusian": "cyrillic", "bg": "cyrillic", "bulgarian": "cyrillic",
    "sr": "cyrillic", "serbian": "cyrillic", "mk": "cyrillic", "macedonian": "cyrillic",
    "zh": "cjk", "chinese": "cjk", "chinese (simplified)": "cjk",
    "chinese (traditional)": "cjk", "cn": "cjk",
    "ja": "japanese", "japanese": "japanese",
    "ko": "korean", "korean": "korean",
    "ar": "arabic", "arabic": "arabic",
    "he": "hebrew", "hebrew": "hebrew",
    "th": "thai", "thai": "thai",
    "el": "greek", "greek": "greek",
}


def _target_script(target_lang: str | None) -> str | None:
    """Map UI/sidecar language string → script key, or None if no font swap needed."""
    if not target_lang:
        return None
    raw = target_lang.strip().lower()
    if not raw:
        return None
    if raw in _LANG_TO_SCRIPT:
        return _LANG_TO_SCRIPT[raw]
    # "Portuguese (Brazil)" → try full string then the part before "("
    if raw in _LANG_TO_SCRIPT:
        return _LANG_TO_SCRIPT[raw]
    base = raw.split("(", 1)[0].strip()
    if base in _LANG_TO_SCRIPT:
        return _LANG_TO_SCRIPT[base]
    # last resort: first token ("chinese simplified" style)
    first = raw.split()[0] if raw.split() else raw
    return _LANG_TO_SCRIPT.get(first)


def _parse_charset_codepoints(charset: str) -> set[int] | None:
    """Parse TMP character-set strings into a set of covered codepoints.

    Accepts both dialects seen in real assets:
      hex: ``0D,20-7E,A0-FF,400-4FF,2000-200F,...``
      dec: ``32 - 126, 160, 8203, 8230`` / ``160 - 255``
    Returns None if the string doesn't look like a charset (don't guess).

    Rule: any A–F letter in the whole string → parse every token as hex;
    otherwise every token is decimal. That matches the two real dialects
    without misreading ``32 - 126`` as hex.
    """
    if not charset or not re.search(r"\d", charset):
        return None
    if not re.fullmatch(r"[\d\s,A-Fa-f\-–]+", charset.strip()):
        return None
    if not re.search(r"[-–,]", charset):
        return None

    as_hex = bool(re.search(r"[A-Fa-f]", charset))
    covered: set[int] = set()
    parsed_any = False
    for part in re.split(r"\s*,\s*", charset.strip()):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"([0-9A-Fa-f]+)\s*[-–]\s*([0-9A-Fa-f]+)", part)
        if m:
            a_s, b_s = m.group(1), m.group(2)
            try:
                a = int(a_s, 16 if as_hex else 10)
                b = int(b_s, 16 if as_hex else 10)
            except ValueError:
                continue
            if 0 <= a <= b <= 0x10FFFF and (b - a) <= 0x10000:
                covered.update(range(a, b + 1))
                parsed_any = True
            continue
        if re.fullmatch(r"[0-9A-Fa-f]+", part):
            try:
                cp = int(part, 16 if as_hex else 10)
            except ValueError:
                continue
            if 0 <= cp <= 0x10FFFF:
                covered.add(cp)
                parsed_any = True
    return covered if parsed_any else None


def _charset_covers_script(charset: str, script: str) -> bool | None:
    """True/False if we can tell; None if unparseable (caller must not assume)."""
    sample = _SCRIPT_SAMPLE.get(script)
    if not sample:
        return None
    covered = _parse_charset_codepoints(charset)
    if covered is None:
        return None
    # Require a solid majority of the sample so a stray single glyph isn't enough.
    hits = sum(1 for ch in sample if ord(ch) in covered)
    return hits >= max(8, len(sample) // 4)


def _ttf_covers_script(font_data: bytes, script: str) -> bool | None:
    """True/False via fontTools cmap; None if unreadable."""
    sample = _SCRIPT_SAMPLE.get(script)
    if not sample or not font_data or len(font_data) < 100:
        return None
    try:
        from fontTools.ttLib import TTFont  # type: ignore
        from io import BytesIO
        cmap = TTFont(BytesIO(font_data)).getBestCmap() or {}
    except Exception:
        return None
    hits = sum(1 for ch in sample if ord(ch) in cmap)
    return hits >= max(8, len(sample) // 4)


def _mb_length_prefixed_strings(raw: bytes, limit: int = 64) -> list[str]:
    """Extract short ASCII length-prefixed strings from a MonoBehaviour blob."""
    out: list[str] = []
    i = 0
    n = len(raw)
    while i + 4 <= n and len(out) < limit:
        ln = int.from_bytes(raw[i : i + 4], "little")
        if 2 <= ln <= 240 and i + 4 + ln <= n:
            chunk = raw[i + 4 : i + 4 + ln]
            if all(32 <= b < 127 for b in chunk):
                out.append(chunk.decode("ascii"))
                i = i + 4 + ln
                i = (i + 3) // 4 * 4
                continue
        i += 1
    return out


def _looks_like_tmp_font_asset(strings: list[str]) -> bool:
    """Heuristic: TMP FontAsset blobs expose a name + version (e.g. '1.1.0')."""
    if not strings:
        return False
    has_ver = any(re.fullmatch(r"\d+\.\d+\.\d+", s) for s in strings[:12])
    has_name = any(
        "SDF" in s or "Font Asset" in s or s.endswith(" Atlas") for s in strings[:12]
    )
    return has_ver and has_name


def _tmp_font_display_name(strings: list[str]) -> str:
    for s in strings[:8]:
        if "SDF" in s or "Font Asset" in s:
            return s
    return strings[0] if strings else ""


def _tmp_charset_from_strings(strings: list[str]) -> str | None:
    """Pick the character-set descriptor string from a TMP FontAsset blob."""
    for s in strings:
        # Real examples: "32 - 126, 160, 8203" / "0D,20-7E,A0-FF,400-4FF,..."
        if not any(c.isdigit() for c in s):
            continue
        if len(s) < 8:
            continue
        if re.search(r"\d\s*-\s*\d", s) or re.search(
            r"[0-9A-Fa-f]{2,4}-[0-9A-Fa-f]{2,4}", s
        ):
            if re.fullmatch(r"[\d\s,A-Fa-f\-–]+", s.strip()):
                return s
    return None


def _font_base_name(name: str) -> str:
    """Normalize 'BaskervaldADFStd SDF' / 'Lato-Regular' → comparable base."""
    n = name.strip()
    for suf in (
        " SDF - Fallback",
        " SDF - Drop Shadow",
        " SDF - Outline",
        " SDF ASCII Extended",
        " SDF",
        " Atlas Material",
        " Atlas",
        " Material",
    ):
        if n.endswith(suf):
            n = n[: -len(suf)]
            break
    return n.strip().lower().replace(" ", "")


class UnityParser(BaseParser):
    engine = "unity"

    def __init__(self) -> None:
        super().__init__()
        self._dll_extract_cache: dict[str, list[dict]] = {}
        self._managed_dir_cache: dict[str, str | None] = {}
        self._generator_cache: dict[str, Any] = {}
        self._font_bytes_cache: dict[str, bytes | None] = {}
        # Last extract/inject multi-source report (API / logs / UI honesty).
        self.last_report: UnityPipelineReport | None = None

    def engine_prompt_addon(self) -> str:
        return (
            "TECHNICAL STRINGS (UI / GAME INTERFACE): these strings come from a Unity "
            "game and are used in menus, HUD, and system messages.\n"
            "FORMAT SPECIFIERS: preserve {0}, {1}, {UserName}, %s, %d and similar "
            "patterns EXACTLY — they are filled in at runtime.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is inside strings.\n"
            "TONE: use a neutral, professional register. Avoid overly literary style."
        )

    def _run_editor(self, args: list[str]) -> str:
        editor_path = self._get_editor_path()
        if not os.path.exists(editor_path):
            raise FileNotFoundError(f"DllEditor helper executable not found at: {editor_path}")

        startupinfo = None
        if sys.platform == 'win32':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0 # SW_HIDE

        proc = subprocess.run(
            [editor_path] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            startupinfo=startupinfo,
            check=True
        )
        return proc.stdout

    def _get_generator(self, root: str, unity_version: str | None) -> Any:
        if not unity_version:
            return None

        cache_key = f"{root}_{unity_version}"
        if cache_key in self._generator_cache:
            return self._generator_cache[cache_key]

        generator = None
        if root not in self._managed_dir_cache:
            self._managed_dir_cache[root] = find_managed_dir(root)
        managed_dir = self._managed_dir_cache[root]

        if managed_dir:
            try:
                from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
                try:
                    generator = TypeTreeGenerator(unity_version)
                    generator.load_local_dll_folder(managed_dir)
                except Exception as e:
                    print(f"Failed to initialize TypeTreeGenerator: {e}", file=sys.stderr)
            except ImportError:
                pass

        self._generator_cache[cache_key] = generator
        return generator

    def _scan_asset_files(self, root: str, sub_paths: list[str] | None = None) -> tuple[list[str], list[str]]:
        compiled_files = []
        source_files = []
        for fpath in iter_files(root, sub_paths):
            f = os.path.basename(fpath)
            f_lower = f.lower()
            if f_lower.endswith(".assets") or (f_lower.startswith("level") and "." not in f):
                if not f_lower.endswith(".manifest") and not f_lower.endswith(".resS") and not f_lower.endswith(".resource") and f_lower != "level":
                    compiled_files.append(fpath)
            elif f_lower.endswith(".unity") or f_lower.endswith(".prefab") or f_lower.endswith(".asset"):
                source_files.append(fpath)
        return compiled_files, source_files

    def _get_editor_path(self) -> str:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            # Running inside PyInstaller single-file bundle
            return os.path.join(sys._MEIPASS, "bin", "DllEditor.exe")
        else:
            # Dev environment path
            return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin", "DllEditor.exe"))

    def _get_font_bytes(self, root: str) -> bytes | None:
        """Return the bytes of NotoSans font to be used for replacements."""
        if root in self._font_bytes_cache:
            return self._font_bytes_cache[root]

        font_bytes = None
        font_path = os.path.join(root, "python-core", "assets", "fonts", "NotoSans-Regular.ttf")
        if os.path.exists(font_path):
            with open(font_path, "rb") as f:
                font_bytes = f.read()
        else:
            # Fallback
            font_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "assets", "fonts", "NotoSans-Regular.ttf"))
            if os.path.exists(font_path):
                with open(font_path, "rb") as f:
                    font_bytes = f.read()

        self._font_bytes_cache[root] = font_bytes
        return font_bytes

    @staticmethod
    def detect(root: str) -> bool:
        """True if there are any non-system .dll files, level* / *.assets / *.prefab, or Addressables localization bundles."""
        # RimWorld mods (About/About.xml) are i18n, even when they ship .dll
        # assemblies and no Languages/ folder yet — must not detect as Unity.
        from .i18n import is_rimworld_mod
        if is_rimworld_mod(root):
            return False
        # Skip mods with i18n/default.json (Stardew Valley) or Languages/ (RimWorld)
        if os.path.isfile(os.path.join(root, "i18n", "default.json")):
            return False
        if os.path.isdir(os.path.join(root, "Languages")):
            return False
        for sub in os.listdir(root):
            sub_path = os.path.join(root, sub)
            if os.path.isdir(sub_path):
                if os.path.isdir(os.path.join(sub_path, "Languages")):
                    return False
        # Unreal signature protection: if it's an Unreal mod/plugin, do not detect as Unity.
        from pathlib import Path
        try:
            for f in Path(root).rglob("*"):
                if f.is_file() and f.suffix.lower() in (".uplugin", ".pak", ".uasset"):
                    return False
        except Exception:
            pass

        # 1. Check for StreamingAssets/aa
        aa_dir = find_aa_dir(root)
        if aa_dir:
            for dirpath, dirnames, filenames in os.walk(aa_dir):
                for f in filenames:
                    if f.endswith(".bundle") or f.startswith("catalog"):
                        return True

        # 2. Existing detection logic
        for fpath in iter_files(root):
            f = os.path.basename(fpath)
            f_lower = f.lower()
            if is_custom_dll(f):
                return True
            if f_lower.endswith(".assets") or f_lower.startswith("level"):
                if not f_lower.endswith(".manifest") and not f_lower.endswith(".resS") and not f_lower.endswith(".resource"):
                    return True
            if f_lower.endswith(".unity") or f_lower.endswith(".prefab"):
                return True
        return False

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        """Run enabled Unity sources; stash ``last_report`` with per-source counts."""
        caps = detect_unity_capabilities(root)
        report = plan_sources(caps, phase="extract")
        self.last_report = report
        results: list[TranslationString] = []

        # ── dll ────────────────────────────────────────────────────────────
        if any(s.id == SOURCE_DLL and s.enabled for s in report.sources):
            before = len(results)
            results.extend(self._extract_dlls(root, sub_paths))
            report.record(SOURCE_DLL, len(results) - before, enabled=True)
        else:
            report.record(SOURCE_DLL, 0, enabled=False, reason="skipped by plan")

        # ── compiled assets: typetree + content_blob + conditional ui_raw ──
        # YAML is scanned in the same helper (separate source id in report).
        asset_counts = self._extract_assets(root, sub_paths, report=report)
        results.extend(asset_counts["strings"])

        # ── string_table (Addressables) ────────────────────────────────────
        if any(s.id == SOURCE_STRING_TABLE and s.enabled for s in report.sources):
            before = len(results)
            results.extend(self._extract_localization(root, sub_paths))
            report.record(SOURCE_STRING_TABLE, len(results) - before, enabled=True)
        else:
            report.record(
                SOURCE_STRING_TABLE,
                0,
                enabled=False,
                reason="no Addressables/Localization signal",
            )

        report.finalize(len(results))
        print(f"[unity] {report.summary_line()}", file=sys.stderr)
        for note in caps.notes:
            print(f"[unity] note: {note}", file=sys.stderr)
        return results

    def _extract_dlls(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        editor_path = self._get_editor_path()
        if not os.path.exists(editor_path):
            print(f"DllEditor helper executable not found at: {editor_path}, skipping DLLs.", file=sys.stderr)
            return []

        results = []
        for fpath in iter_files(root, sub_paths):
            f = os.path.basename(fpath)
            if is_custom_dll(f):
                rel_path = os.path.relpath(fpath, root).replace("\\", "/")

                try:
                    if fpath in self._dll_extract_cache:
                        extracted = self._dll_extract_cache[fpath]
                    else:
                        stdout = self._run_editor(["extract", fpath])
                        extracted = json.loads(stdout)
                        self._dll_extract_cache[fpath] = extracted

                    for item in extracted:
                        original = item.get("original", "")
                        if not _is_game_text(original):
                            continue
                        path = item.get("path", [])
                        context = item.get("context", "")

                        results.append(self._mk(rel_path, path, original, context))
                except Exception as ex:
                    print(f"Error extracting from DLL {f}: {ex}", file=sys.stderr)
                    continue

        return results

    def _extract_assets(
        self,
        root: str,
        sub_paths: list[str] | None = None,
        report: UnityPipelineReport | None = None,
    ) -> dict[str, Any]:
        """Extract typetree + content_blob + ui_raw + yaml; optional report counts.

        Returns ``{"strings": list[TranslationString]}``. Per-source counts go
        into ``report`` when provided (registry path). Legacy callers that
        ignored the return shape should use ``.extract()`` only.
        """
        self._current_root = root
        results: list[TranslationString] = []
        compiled_files, source_files = self._scan_asset_files(root, sub_paths)

        n_typetree = 0
        n_content = 0
        n_ui_raw = 0
        n_yaml = 0
        ui_raw_ran = False

        if compiled_files:
            try:
                import UnityPy
            except ImportError as e:
                import traceback
                print(f"UnityPy import failed: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                compiled_files = []

        for fpath in compiled_files:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            if "globalgamemanagers" in fpath.lower():
                continue

            failed_count = 0
            typetree_found = 0
            env = None
            try:
                env = UnityPy.load(fpath)
                # Resolve and set generator using cached helper
                unity_version = None
                for obj in env.objects:
                    unity_version = obj.assets_file.unity_version
                    break
                generator = self._get_generator(root, unity_version)
                if generator:
                    env.typetree_generator = generator

                for obj in env.objects:
                    if obj.type.name == "MonoBehaviour":
                        try:
                            # Skip library classes, handle external script load failures gracefully
                            ns = ""
                            try:
                                data = obj.read()
                                script_data = data.m_Script.read()
                                ns = getattr(script_data, "namespace", "")
                            except Exception:
                                pass

                            if should_skip_type(ns):
                                continue

                            tree = obj.read_typetree()
                            text = None
                            field_name = None
                            if "m_Text" in tree and isinstance(tree["m_Text"], str):
                                text = tree["m_Text"]
                                field_name = "m_Text"
                            elif "m_text" in tree and isinstance(tree["m_text"], str):
                                text = tree["m_text"]
                                field_name = "m_text"

                            if text and not text.isspace() and _is_game_text(text):
                                path = ["Asset", obj.type.name, str(obj.path_id), field_name]
                                
                                # Resolve GameObject name from tree
                                go_name = ""
                                go_ptr = tree.get("m_GameObject")
                                if isinstance(go_ptr, dict):
                                    go_path_id = go_ptr.get("m_PathID")
                                    if go_path_id and go_path_id in obj.assets_file.objects:
                                        go_obj = obj.assets_file.objects[go_path_id]
                                        try:
                                            go_data = go_obj.read()
                                            go_name = getattr(go_data, "m_Name", None) or getattr(go_data, "name", "")
                                        except Exception:
                                            pass
                                
                                # Resolve Script class name
                                script_name = ""
                                try:
                                    mb_head = obj.parse_monobehaviour_head()
                                    script = mb_head.m_Script.deref_parse_as_object()
                                    script_name = f"{script.m_Namespace}.{script.m_ClassName}" if script.m_Namespace else script.m_ClassName
                                except Exception:
                                    pass

                                parts = []
                                if go_name:
                                    parts.append(f"GameObject: {go_name}")
                                if script_name:
                                    parts.append(f"Script: {script_name}")
                                parts.append(f"File: {os.path.basename(fpath)}")
                                parts.append(f"PathID: {obj.path_id}")
                                
                                context = ", ".join(parts)
                                results.append(self._mk(rel_path, path, text, context))
                                typetree_found += 1
                                n_typetree += 1
                        except Exception:
                            failed_count += 1
                    elif obj.type.name == "Text":
                        try:
                            data = obj.read()
                            text = getattr(data, "m_Text", None)
                            if text and not text.isspace() and _is_game_text(text):
                                path = ["Asset", obj.type.name, str(obj.path_id), "m_Text"]
                                
                                go_name = ""
                                if hasattr(data, "m_GameObject") and data.m_GameObject:
                                    try:
                                        go_data = data.m_GameObject.read()
                                        go_name = getattr(go_data, "m_Name", None) or getattr(go_data, "name", "")
                                    except Exception:
                                        pass

                                parts = []
                                if go_name:
                                    parts.append(f"GameObject: {go_name}")
                                parts.append("Type: Text")
                                parts.append(f"File: {os.path.basename(fpath)}")
                                parts.append(f"PathID: {obj.path_id}")
                                
                                context = ", ".join(parts)
                                results.append(self._mk(rel_path, path, text, context))
                                typetree_found += 1
                                n_typetree += 1
                        except Exception:
                            failed_count += 1
            except Exception as e:
                print(f"Error parsing assets file {fpath}: {e}", file=sys.stderr)
                env = None

            # Always try content blobs (Naninovel Script, I2, Yarn, …) — they are
            # almost never m_Text fields, so typetree success on UI does not cover them.
            if env is not None:
                before_c = len(results)
                content_n = self._extract_raw_from_env(
                    env, fpath, root, results, mode="content"
                )
                n_content += len(results) - before_c
                # content_n is added count; keep using len(results) for accuracy
                if typetree_found == 0 and failed_count > 0:
                    print(
                        f"Typetree failed for {failed_count} objects in "
                        f"{os.path.basename(fpath)}; content-blob hit {content_n}, "
                        f"running UI raw pass.",
                        file=sys.stderr,
                    )
                    # Small UI chrome only when typetree is dead; skip if content
                    # already gave us a full script game (avoids lorem placeholders
                    # from the same Naninovel default UI when scripts dominate).
                    ui_raw_ran = True
                    before_u = len(results)
                    self._extract_raw_from_env(
                        env, fpath, root, results, mode="ui"
                    )
                    n_ui_raw += len(results) - before_u
                elif failed_count > 0:
                    print(
                        f"Warning: Failed to extract {failed_count} objects in "
                        f"compiled asset {fpath}.",
                        file=sys.stderr,
                    )

        for fpath in source_files:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                
                lines = content.splitlines()
                line_idx = 0
                while line_idx < len(lines):
                    line = lines[line_idx]
                    match = re.search(r'm_[Tt]ext(?:UGUI)?:\s*(.*)', line)
                    if match:
                        raw_val = match.group(1).strip()
                        start_line_idx = line_idx
                        
                        val, line_idx = _parse_multiline_yaml_val(lines, line_idx, raw_val)
                        if val and not val.isspace() and _is_game_text(val):
                            path = ["AssetYAML", "m_Text", str(start_line_idx)]
                            context = f"File: {os.path.basename(fpath)}, Line: {start_line_idx + 1}"
                            results.append(self._mk(rel_path, path, val, context))
                            n_yaml += 1
                    line_idx += 1
            except Exception as e:
                print(f"Error parsing source file {fpath}: {e}", file=sys.stderr)

        if report is not None:
            if compiled_files:
                report.record(SOURCE_TYPETREE, n_typetree, enabled=True)
                report.record(SOURCE_CONTENT, n_content, enabled=True)
                if ui_raw_ran:
                    report.record(
                        SOURCE_UI_RAW,
                        n_ui_raw,
                        enabled=True,
                        detail="ran after typetree miss on at least one file",
                    )
                else:
                    report.record(
                        SOURCE_UI_RAW,
                        0,
                        enabled=True,
                        detail="not needed (typetree ok or no failures)",
                    )
            else:
                report.record(SOURCE_TYPETREE, 0, enabled=False, reason="no compiled assets")
                report.record(SOURCE_CONTENT, 0, enabled=False, reason="no compiled assets")
                report.record(SOURCE_UI_RAW, 0, enabled=False, reason="no compiled assets")
            if source_files:
                report.record(SOURCE_YAML, n_yaml, enabled=True)
            else:
                report.record(SOURCE_YAML, 0, enabled=False, reason="no YAML assets")

        return {"strings": results}

    def _extract_raw_from_env(
        self,
        env: Any,
        fpath: str,
        root: str,
        results: list[TranslationString],
        mode: str = "content",
    ) -> int:
        """Length-prefix extract from MonoBehaviours already loaded in ``env``.

        mode:
          - ``content``: script/localization blobs (markers or dialogue density)
          - ``ui``: small chrome MonoBehaviours only (menus, buttons)
          - ``all``: every MonoBehaviour (legacy full scan)

        Paths are ``["AssetRaw", type, path_id, byte_offset]`` so inject can
        rewrite the exact slot and grow/shrink the string.
        Appends into ``results``; returns how many strings were added.
        """
        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
        # Dedup by stable id key so content+ui passes don't double-emit
        existing = {(s.file, tuple(s.path), s.original) for s in results}
        added = 0
        base = os.path.basename(fpath)

        _UI_NAMES = frozenset({
            "play", "quit", "load", "save", "back", "next", "yes", "no",
            "ok", "all", "none", "video", "audio", "game", "settings",
            "gallery", "music", "loading", "credits", "options", "resume",
            "start", "new", "continue", "delete", "accept", "cancel",
            "confirm", "close", "help", "return", "pause", "skip",
            "adult", "content", "disclaimer",
        })

        try:
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    raw = obj.get_raw_data()
                except Exception:
                    continue
                if len(raw) < 20:
                    continue

                is_content = _blob_is_content(raw)
                if mode == "content":
                    if not is_content:
                        continue
                elif mode == "ui":
                    if is_content or _blob_is_skip_config(raw):
                        continue
                    if len(raw) > _UI_RAW_MAX_SIZE:
                        continue
                elif mode == "all":
                    if _blob_is_skip_config(raw) and not is_content:
                        continue
                else:
                    continue

                # Production slot walk (shared with inject). Naninovel scripts
                # skip opcodes via Naninovel.Commands lookahead; never vacuum
                # every length-prefix hit.
                candidates = _iter_raw_translatable_slots(raw)
                if not candidates:
                    continue

                texts = [s.strip() for _, s in candidates]
                possible_names: list[str] = []
                for s in texts:
                    words = s.split()
                    if (
                        1 <= len(words) <= 2
                        and len(s) <= 20
                        and s[0].isupper()
                        and s.lower() not in _UI_NAMES
                        and not any(c in s for c in "?!.:;,()[]{}")
                        and not s.isupper()
                        and not re.match(r"^[A-Z][a-z]+$", s)
                    ):
                        possible_names.append(s)

                for offset, s in candidates:
                    t = s.strip()
                    path = ["AssetRaw", obj.type.name, str(obj.path_id), str(offset)]
                    key = (rel_path, tuple(path), t)
                    if key in existing:
                        continue
                    existing.add(key)

                    ctx_parts = [f"File: {base}", f"PathID: {obj.path_id}"]
                    if is_content:
                        ctx_parts.append("Kind: content")
                    if possible_names and len(possible_names) <= 3 and " " in t and len(t) > 10:
                        ctx_parts.append(f"Speakers in this block: {', '.join(possible_names)}")
                    siblings = [x for x in texts if x != t]
                    if siblings and " " in t and len(t) > 10:
                        capped: list[str] = []
                        total = 0
                        for sib in siblings:
                            if len(capped) >= 12:
                                break
                            addition = len(sib) + 2
                            if total + addition > 600:
                                break
                            capped.append(sib)
                            total += addition
                        if capped:
                            ctx_parts.append(f"Other strings in this block: {'; '.join(capped)}")

                    results.append(self._mk(rel_path, path, t, ", ".join(ctx_parts)))
                    added += 1
        except Exception as e:
            print(f"Error in raw extract ({mode}) for {fpath}: {e}", file=sys.stderr)

        return added

    def _extract_raw_fallback(self, fpath: str, root: str) -> list[TranslationString]:
        """Legacy entry: full raw scan (content + ui). Prefer _extract_raw_from_env."""
        results: list[TranslationString] = []
        try:
            import UnityPy
            env = UnityPy.load(fpath)
            self._extract_raw_from_env(env, fpath, root, results, mode="content")
            self._extract_raw_from_env(env, fpath, root, results, mode="ui")
        except Exception as e:
            print(f"Error in raw fallback for {fpath}: {e}", file=sys.stderr)
        return results

    def _inject_raw_fallback(self, fpath: str, root: str, translations: dict[str, str],
                             env: Any = None, font_bytes: bytes | None = None,
                             target_lang: str | None = None) -> int:
        """Inject into MonoBehaviour raw via length-changing string rebuild.

        Prefers path-addressed slots ``AssetRaw / type / path_id / offset``.
        Falls back to text-match for legacy ``RawFallback`` paths.
        Uses UnityPy ``set_raw_data`` + file save so RU can be longer than EN.
        """
        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
        written = 0

        try:
            if env is None:
                import UnityPy
                env = UnityPy.load(fpath)

            # path forms:
            #   AssetRaw | MonoBehaviour | <path_id> | <offset>
            #   RawFallback | MonoBehaviour | <path_id> | length_prefix  (legacy)
            # Scan blobs and make_id each candidate (same walk as extract).
            objects_changed = 0
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    raw = bytes(obj.get_raw_data())
                except Exception:
                    continue
                if len(raw) < 20:
                    continue

                # Same slot set as extract. Extra belt: never write engine ids
                # even if a stale project.json still maps them to RU.
                replacements: dict[int, str] = {}
                for offset, s in _iter_raw_translatable_slots(raw):
                    t = s.strip()
                    if not t or _is_engine_identifier(t):
                        continue
                    path_new = ["AssetRaw", obj.type.name, str(obj.path_id), str(offset)]
                    path_old = ["RawFallback", obj.type.name, str(obj.path_id), "length_prefix"]
                    sid_new = make_id(self.engine, rel_path, path_new, t)
                    sid_old = make_id(self.engine, rel_path, path_old, t)
                    new_text = None
                    if sid_new in translations:
                        new_text = translations[sid_new]
                    elif sid_old in translations:
                        new_text = translations[sid_old]
                    if new_text is None or new_text == s:
                        continue
                    # Refuse to inject empty / pure-control garbage
                    if not str(new_text).strip():
                        continue
                    replacements[offset] = new_text

                if not replacements:
                    continue

                # Rebuild blob: copy non-string regions, repack strings (may resize)
                out = bytearray()
                pos = 0
                for offset in sorted(replacements.keys()):
                    slen = struct.unpack_from("<I", raw, offset)[0]
                    aligned_end = _aligned_string_end(offset, slen)
                    out.extend(raw[pos:offset])
                    out.extend(_pack_aligned_string(replacements[offset]))
                    pos = aligned_end
                out.extend(raw[pos:])

                try:
                    obj.set_raw_data(bytes(out))
                    written += len(replacements)
                    objects_changed += 1
                except Exception as e:
                    print(
                        f"set_raw_data failed path_id={obj.path_id} in "
                        f"{os.path.basename(fpath)}: {e}",
                        file=sys.stderr,
                    )

            if objects_changed == 0:
                # No string edits — still try a conservative font-gap patch if
                # the target script needs glyphs the game's fonts don't have.
                if _target_script(target_lang):
                    self._replace_font_pptrs(env, fpath, target_lang)
                return written

            self.backup_file(root, fpath)
            # UnityPy Environment.save writes into out_path dir; use file.save bytes.
            try:
                data = env.file.save()
                if isinstance(data, (bytes, bytearray)) and len(data) > 0:
                    with open(fpath, "wb") as f:
                        f.write(data)
                else:
                    # Multi-cab / container: fall back to env.save into temp dir
                    import shutil
                    tmp = tempfile.mkdtemp(prefix="interprex_unity_")
                    try:
                        env.save(pack="none", out_path=tmp)
                        # Find the rewritten asset by basename
                        base = os.path.basename(fpath)
                        candidate = os.path.join(tmp, base)
                        if not os.path.isfile(candidate):
                            for dirpath, _, filenames in os.walk(tmp):
                                if base in filenames:
                                    candidate = os.path.join(dirpath, base)
                                    break
                        if os.path.isfile(candidate):
                            shutil.copy2(candidate, fpath)
                        else:
                            print(
                                f"UnityPy save produced no {base}; inject may be incomplete",
                                file=sys.stderr,
                            )
                    finally:
                        shutil.rmtree(tmp, ignore_errors=True)
            except Exception as e:
                print(f"Error saving injected assets {fpath}: {e}", file=sys.stderr)

            # Font-gap patch AFTER save so we rewrite the final on-disk bytes
            # (UnityPy save would otherwise clobber a pre-save PPtr edit).
            if _target_script(target_lang):
                try:
                    import UnityPy
                    env2 = UnityPy.load(fpath)
                    self._replace_font_pptrs(env2, fpath, target_lang)
                except Exception:
                    pass

        except Exception as e:
            print(f"Error in raw inject for {fpath}: {e}", file=sys.stderr)

        return written

    def _extract_localization(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        try:
            import UnityPy
        except ImportError:
            print("UnityPy is not installed. Skipping Addressables bundles.", file=sys.stderr)
            return []

        results = []
        for bundle_path in find_aa_bundles(root):
            rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
            try:
                env = UnityPy.load(bundle_path)

                # Phase 1: collect SharedTableData (id -> keyName)
                shared_tables: dict[int, dict] = {}
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_Entries" in tree and "m_TableCollectionName" in tree:
                            shared_tables[obj.path_id] = {
                                "name": tree["m_TableCollectionName"],
                                "id_to_key": {e["m_Id"]: e["m_Key"] for e in tree["m_Entries"]},
                            }
                    except Exception:
                        pass

                # Phase 2: extract StringTable
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_TableData" not in tree or "m_LocaleIdentifier" not in tree:
                            continue

                        locale = tree["m_LocaleIdentifier"].get("m_Code", "")
                        shared_path_id = tree.get("m_SharedData", {}).get("m_PathID")
                        shared = shared_tables.get(shared_path_id, {})
                        id_to_key = shared.get("id_to_key", {})
                        collection_name = shared.get("name") or obj.name or "Unknown"

                        for entry in tree["m_TableData"]:
                            value = entry.get("m_Localized", "")
                            if not value or value.isspace():
                                continue
                            entry_id = entry["m_Id"]
                            key_name = id_to_key.get(entry_id, str(entry_id))
                            path = ["StringTable", collection_name, locale, key_name]
                            context = f"Collection: {collection_name}, Locale: {locale}, Key: {key_name}"
                            results.append(self._mk(rel_path, path, value, context))
                    except Exception:
                        pass

            except Exception as e:
                print(f"Error parsing bundle {bundle_path}: {e}", file=sys.stderr)

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        """Run enabled Unity inject sources; stash ``last_report`` with per-source counts."""
        caps = detect_unity_capabilities(root)
        report = plan_sources(caps, phase="inject")
        self.last_report = report
        written = 0

        if any(s.id == SOURCE_DLL and s.enabled for s in report.sources):
            n = self._inject_dlls(root, translations, target_lang, sub_paths)
            written += n
            report.record(SOURCE_DLL, n, enabled=True)
        else:
            report.record(SOURCE_DLL, 0, enabled=False, reason="skipped by plan")

        # Assets: typetree + raw (content/ui slots) + yaml + font_gap inside
        n_assets = self._inject_assets(
            root, translations, target_lang, sub_paths, report=report
        )
        written += n_assets

        if any(s.id == SOURCE_STRING_TABLE and s.enabled for s in report.sources):
            n = self._inject_localization(root, translations, target_lang, sub_paths)
            written += n
            report.record(SOURCE_STRING_TABLE, n, enabled=True)
        else:
            report.record(
                SOURCE_STRING_TABLE,
                0,
                enabled=False,
                reason="no Addressables/Localization signal",
            )

        report.finalize(written)
        print(f"[unity] {report.summary_line()}", file=sys.stderr)
        return written

    def _inject_dlls(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        editor_path = self._get_editor_path()
        if not os.path.exists(editor_path):
            print(f"DllEditor helper executable not found at: {editor_path}, skipping DLLs.", file=sys.stderr)
            return 0

        written = 0
        for fpath in iter_files(root, sub_paths):
            f = os.path.basename(fpath)
            if is_custom_dll(f):
                rel_path = os.path.relpath(fpath, root).replace("\\", "/")

                try:
                    if fpath in self._dll_extract_cache:
                        extracted = self._dll_extract_cache[fpath]
                    else:
                        stdout = self._run_editor(["extract", fpath])
                        extracted = json.loads(stdout)
                        self._dll_extract_cache[fpath] = extracted

                    dll_patch_map = {}
                    for item in extracted:
                        original = item.get("original", "")
                        if not _is_game_text(original):
                            continue
                        path = item.get("path", [])
                        sid = make_id(self.engine, rel_path, path, original)

                        if sid in translations:
                            path_key = "\x01".join(path)
                            dll_patch_map[path_key] = translations[sid]

                    if dll_patch_map:
                        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as temp_f:
                            json.dump(dll_patch_map, temp_f)
                            temp_json_path = temp_f.name

                        try:
                            self.backup_file(root, fpath)
                            stdout = self._run_editor(["inject", fpath, temp_json_path])
                            output = stdout.strip()
                            if output.startswith("SUCCESS:"):
                                replaced = int(output.split(":")[1])
                                written += replaced
                                if fpath in self._dll_extract_cache:
                                    del self._dll_extract_cache[fpath]
                        finally:
                            if os.path.exists(temp_json_path):
                                os.remove(temp_json_path)

                except Exception as ex:
                    print(f"Error injecting into DLL {f}: {ex}", file=sys.stderr)
                    continue

        return written

    def _inject_assets(
        self,
        root: str,
        translations: dict[str, str],
        target_lang: str | None = None,
        sub_paths: list[str] | None = None,
        report: UnityPipelineReport | None = None,
    ) -> int:
        self._current_root = root
        written = 0
        n_typetree = 0
        n_raw = 0
        n_yaml = 0
        compiled_files, source_files = self._scan_asset_files(root, sub_paths)

        if compiled_files:
            try:
                import UnityPy
            except ImportError:
                print("UnityPy is not installed. Skipping compiled assets injection.", file=sys.stderr)
                compiled_files = []

        font_bytes = self._get_font_bytes(root)

        for fpath in compiled_files:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            if "globalgamemanagers" in fpath.lower():
                continue

            failed_count = 0
            typetree_found = 0
            try:
                env = UnityPy.load(fpath)
                unity_version = None
                for obj in env.objects:
                    unity_version = obj.assets_file.unity_version
                    break
                generator = self._get_generator(root, unity_version)
                if generator:
                    env.typetree_generator = generator

                changed = False
                fonts_to_replace = set()

                for obj in env.objects:
                    if obj.type.name in ("MonoBehaviour", "Text"):
                        try:
                            if obj.type.name == "MonoBehaviour":
                                ns = ""
                                try:
                                    data = obj.read()
                                    script_data = data.m_Script.read()
                                    ns = getattr(script_data, "namespace", "")
                                except Exception:
                                    pass

                                if should_skip_type(ns):
                                    continue

                                tree = obj.read_typetree()
                            else:
                                data = obj.read()
                                tree = data.read_typetree()

                            field_name = None
                            if "m_Text" in tree and isinstance(tree["m_Text"], str):
                                field_name = "m_Text"
                            elif "m_text" in tree and isinstance(tree["m_text"], str):
                                field_name = "m_text"

                            if field_name:
                                original = tree[field_name]
                                if original and not original.isspace() and _is_game_text(original):
                                    path = ["Asset", obj.type.name, str(obj.path_id), field_name]
                                    sid = make_id(self.engine, rel_path, path, original)

                                    if sid in translations:
                                        translated = translations[sid]
                                        tree[field_name] = translated
                                        if obj.type.name == "MonoBehaviour":
                                            obj.save_typetree(tree)
                                        else:
                                            data.save_typetree(tree)
                                        changed = True
                                        written += 1
                                        typetree_found += 1
                                        n_typetree += 1
                        except Exception:
                            failed_count += 1

                    if obj.type.name == "TMP_FontAsset" and font_bytes:
                        try:
                            data = obj.read()
                            tree = data.read_typetree()
                            pop_mode = tree.get("m_AtlasPopulationMode", 0)
                            
                            if pop_mode == 1:
                                src_font = tree.get("m_SourceFontFile")
                                if src_font and isinstance(src_font, dict):
                                    path_id = src_font.get("m_PathID")
                                    if path_id:
                                        fonts_to_replace.add(path_id)
                            else:
                                print(f"[WARNING] Static TMP_FontAsset '{data.name}' detected in {fpath}. Fallback requires atlas rebuild.", file=sys.stderr)
                        except Exception:
                            failed_count += 1

                # Always attempt raw inject: content blobs (Naninovel/I2/…) never use m_Text.
                # Font-gap PPtr rewrite runs INSIDE raw inject, AFTER any UnityPy
                # save — a pre-save disk patch would be clobbered by env.file.save().
                raw_written = self._inject_raw_fallback(
                    fpath, root, translations,
                    env=env, font_bytes=font_bytes,
                    target_lang=target_lang,
                )
                if raw_written > 0:
                    written += raw_written
                    n_raw += raw_written
                elif failed_count > 0 and typetree_found == 0:
                    print(
                        f"Warning: no typetree and no raw inject hits in {os.path.basename(fpath)} "
                        f"({failed_count} typetree failures).",
                        file=sys.stderr,
                    )

            except Exception as e:
                print(f"Error injecting into assets file {fpath}: {e}", file=sys.stderr)

        for fpath in source_files:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()

                lines = content.splitlines()
                changed = False
                line_idx = 0
                while line_idx < len(lines):
                    line = lines[line_idx]
                    match = re.search(r'm_[Tt]ext(?:UGUI)?:\s*(.*)', line)
                    if match:
                        raw_val = match.group(1).strip()
                        start_line_idx = line_idx
                        
                        val, line_idx = _parse_multiline_yaml_val(lines, line_idx, raw_val)
                        if val and not val.isspace() and _is_game_text(val):
                            path = ["AssetYAML", "m_Text", str(start_line_idx)]
                            sid = make_id(self.engine, rel_path, path, val)

                            if sid in translations:
                                translated = translations[sid]
                                escaped_trans = json.dumps(translated, ensure_ascii=False)
                                
                                key_prefix = line[:match.start(1)]
                                lines[start_line_idx] = f"{key_prefix}{escaped_trans}"
                                for i in range(start_line_idx + 1, line_idx + 1):
                                    lines[i] = None
                                changed = True
                                written += 1
                                n_yaml += 1
                    line_idx += 1

                if changed:
                    lines = [ln for ln in lines if ln is not None]
                    self.backup_file(root, fpath)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
            except Exception as e:
                print(f"Error injecting into source file {fpath}: {e}", file=sys.stderr)

        if report is not None:
            if compiled_files:
                report.record(SOURCE_TYPETREE, n_typetree, enabled=True)
                # Raw inject covers content_blob + ui_raw slots (same walk).
                report.record(
                    SOURCE_CONTENT,
                    n_raw,
                    enabled=True,
                    detail="raw AssetRaw slots (content + ui paths share inject walk)",
                )
                report.record(
                    SOURCE_UI_RAW,
                    0,
                    enabled=True,
                    detail="merged into content_blob count on inject (shared raw walk)",
                )
                report.record(
                    SOURCE_FONT_GAP,
                    0,
                    enabled=True,
                    detail="runs inside raw inject for non-Latin targets (not a string count)",
                )
            else:
                report.record(SOURCE_TYPETREE, 0, enabled=False, reason="no compiled assets")
                report.record(SOURCE_CONTENT, 0, enabled=False, reason="no compiled assets")
                report.record(SOURCE_UI_RAW, 0, enabled=False, reason="no compiled assets")
                report.record(SOURCE_FONT_GAP, 0, enabled=False, reason="no compiled assets")
            if source_files:
                report.record(SOURCE_YAML, n_yaml, enabled=True)
            else:
                report.record(SOURCE_YAML, 0, enabled=False, reason="no YAML assets")

        return written

    def _replace_font_pptrs(
        self, env: Any, fpath: str, target_lang: str | None = None
    ) -> None:
        """Fill script gaps by repointing UI font PPtrs to an in-file donor.

        Conservative rules (must not break other Unity games):
          1. Only for non-Latin target scripts (``Russian`` / ``ru`` / CJK / …).
          2. Only rewrite PPtrs of TMP FontAssets we can PROVE lack the script.
          3. Only when the same asset file already has a TMP donor that PROVES
             it covers the script (never invent / inject a foreign font asset).
          4. Fonts with unknown coverage are left alone.
          5. Size-preserving: only the 12-byte ``(fileID=0, pathID)`` PPtr is
             patched — no atlas rebuild, no structural rewrite.

        ⚠️ LOAD-BEARING (Touchstarved black-screen, 2026-08):
        Never do a whole-file find/replace of Font (TTF) path_ids, and never
        rewrite hits that land *inside* Font / TMP FontAsset object bodies.

        Font path_ids on real games sit in the low thousands (e.g. Inter TTF
        ``1686``). TMP character tables store ``glyphIndex`` as a uint in the
        *same numeric range*, often adjacent to a zero word — the 12-byte
        pattern ``fileID=0 + pathID`` is then an exact false match for a PPtr.
        Global rewrite flipped Inter-Regular SDF glyph indices →
        ``KeyNotFoundException`` in ``InitializeCharacterLookupDictionary`` →
        broken TMP → black screen after the menu. The old "pathIDs are sparse
        int64s — accepted risk" comment was wrong for this range.

        So: TMP victim → TMP donor only, and only in byte ranges of objects
        that are neither a Font nor a TMP FontAsset (UI TextMeshProUGUI etc.).
        Legacy ``UnityEngine.UI.Text`` Font PPtrs are left alone — Naninovel /
        modern UI is TMP, and wrong-type Font remaps are how we ate glyphs.
        """
        script = _target_script(target_lang)
        if not script:
            return
        try:
            # ── 1. Catalog Unity Font assets (TTF coverage, negative signal) ─
            # path_id → (name, covers: True/False/None). Used ONLY to mark a
            # TMP atlas as missing when its matching TTF has no script glyphs.
            # We never rewrite Font path_ids (glyph-index collision hazard).
            font_cov: dict[int, tuple[str, bool | None]] = {}
            font_by_base: dict[str, tuple[int, bool | None]] = {}
            # Absolute [start, end) ranges that must NEVER be PPtr-patched.
            forbidden_ranges: list[tuple[int, int]] = []
            for obj in env.objects:
                if obj.type.name != "Font":
                    continue
                try:
                    bs = int(getattr(obj, "byte_start", -1))
                    bsz = int(getattr(obj, "byte_size", 0))
                    if bs >= 0 and bsz > 0:
                        forbidden_ranges.append((bs, bs + bsz))
                    data = obj.read()
                    name = getattr(data, "m_Name", None) or ""
                    blob = bytes(getattr(data, "m_FontData", b"") or b"")
                    cov = _ttf_covers_script(blob, script) if blob else None
                    font_cov[obj.path_id] = (name, cov)
                    if name:
                        font_by_base[_font_base_name(name)] = (obj.path_id, cov)
                except Exception:
                    continue

            # ── 2. Catalog TMP FontAsset MonoBehaviours ────────────────────
            # path_id → (name, covers)
            tmp_cov: dict[int, tuple[str, bool | None]] = {}
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                try:
                    raw = bytes(obj.get_raw_data())
                except Exception:
                    continue
                if len(raw) < 40:
                    continue
                strings = _mb_length_prefixed_strings(raw)
                if not _looks_like_tmp_font_asset(strings):
                    continue
                try:
                    bs = int(getattr(obj, "byte_start", -1))
                    bsz = int(getattr(obj, "byte_size", 0))
                    if bs >= 0 and bsz > 0:
                        forbidden_ranges.append((bs, bs + bsz))
                except Exception:
                    pass
                name = _tmp_font_display_name(strings)
                charset = _tmp_charset_from_strings(strings)
                cov: bool | None = None
                if charset:
                    cov = _charset_covers_script(charset, script)
                if cov is None:
                    # No charset (or unparseable): only use the matching TTF as
                    # a NEGATIVE signal. A TTF without the script cannot back a
                    # static atlas that has it. A TTF WITH the script still
                    # doesn't prove the atlas baked those glyphs (LiberationSans
                    # SDF on Touchstarved is Latin-only despite a full TTF) —
                    # so leave coverage unknown rather than risk a bad donor.
                    base = _font_base_name(name)
                    ttf = font_by_base.get(base)
                    if ttf is not None and ttf[1] is False:
                        cov = False
                tmp_cov[obj.path_id] = (name, cov)

            if not tmp_cov:
                return

            # ── 3. Pick TMP donor (positive coverage only) ─────────────────
            tmp_donors = [pid for pid, (_, c) in tmp_cov.items() if c is True]

            # Prefer a "regular"/default-looking donor over a specialized one
            # (e.g. Inter-Regular over a random bold). Stable: lowest path_id
            # among names containing "regular", else lowest path_id overall.
            def _pick_donor(cands: list[int], table: dict[int, tuple[str, bool | None]]) -> int | None:
                if not cands:
                    return None
                regular = [
                    pid for pid in cands
                    if "regular" in (table[pid][0] or "").lower()
                    and "fallback" not in (table[pid][0] or "").lower()
                ]
                pool = regular or [
                    pid for pid in cands
                    if "fallback" not in (table[pid][0] or "").lower()
                ] or cands
                return min(pool)

            tmp_donor = _pick_donor(tmp_donors, tmp_cov)

            # Victims = proven missing coverage. Unknown → leave alone.
            tmp_victims = {
                pid for pid, (_, c) in tmp_cov.items()
                if c is False and pid != tmp_donor
            }

            if not tmp_victims:
                return
            if tmp_donor is None:
                # TMP gaps but no TMP donor — refuse to point UI at a Font asset.
                print(
                    f"[font] {os.path.basename(fpath)}: {len(tmp_victims)} TMP font(s) "
                    f"lack {script}, but no in-file TMP donor covers it — left untouched",
                    file=sys.stderr,
                )
                return

            # Merge forbidden ranges so hit tests are O(log n) via bisect.
            forbidden_ranges.sort()
            merged: list[tuple[int, int]] = []
            for start, end in forbidden_ranges:
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))

            def _in_forbidden(offset: int) -> bool:
                # True if [offset, offset+12) overlaps any Font/TMP FontAsset body.
                if not merged:
                    return False
                lo, hi = 0, len(merged)
                while lo < hi:
                    mid = (lo + hi) // 2
                    if merged[mid][1] <= offset:
                        lo = mid + 1
                    else:
                        hi = mid
                if lo < len(merged) and merged[lo][0] < offset + 12:
                    return True
                return False

            # ── 4. Size-preserving PPtr rewrite on disk ────────────────────
            # Unity PPtr (same file): int32 fileID=0 + int64 pathID.
            # Only rewrite hits OUTSIDE Font / TMP FontAsset object bodies so
            # glyphIndex / internal source-font fields stay byte-verbatim.
            with open(fpath, "rb") as f:
                file_bytes = bytearray(f.read())

            replaced = 0
            skipped_forbidden = 0
            for vid in tmp_victims:
                if vid == tmp_donor:
                    continue
                old_pptr = struct.pack("<Iq", 0, vid)
                new_pptr = struct.pack("<Iq", 0, tmp_donor)
                idx = 0
                while True:
                    idx = file_bytes.find(old_pptr, idx)
                    if idx < 0:
                        break
                    if _in_forbidden(idx):
                        skipped_forbidden += 1
                        idx += 12
                        continue
                    file_bytes[idx : idx + 12] = new_pptr
                    replaced += 1
                    idx += 12

            if replaced > 0:
                # Backup once per file via the parser's backup helper if root known
                root = getattr(self, "_current_root", None)
                if root:
                    try:
                        self.backup_file(root, fpath)
                    except Exception:
                        pass
                with open(fpath, "wb") as f:
                    f.write(file_bytes)
                print(
                    f"[font] {os.path.basename(fpath)}: rewrote {replaced} PPtr(s) "
                    f"for missing {script} glyphs (TMP→{tmp_cov[tmp_donor][0]!r}; "
                    f"victims TMP={len(tmp_victims)}"
                    f"{f', skipped_in_font_bodies={skipped_forbidden}' if skipped_forbidden else ''})",
                    file=sys.stderr,
                )
            elif skipped_forbidden:
                print(
                    f"[font] {os.path.basename(fpath)}: all {skipped_forbidden} "
                    f"PPtr hit(s) were inside Font/TMP FontAsset bodies — left untouched",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"Error replacing font PPtrs: {e}", file=sys.stderr)

    def _inject_localization(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        try:
            import UnityPy
        except ImportError:
            return 0

        written = 0
        for bundle_path in find_aa_bundles(root):
            rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
            try:
                env = UnityPy.load(bundle_path)
                changed = False

                # Collect SharedTableData
                shared_tables: dict[int, dict] = {}
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_Entries" in tree and "m_TableCollectionName" in tree:
                            shared_tables[obj.path_id] = {
                                "name": tree["m_TableCollectionName"],
                                "id_to_key": {e["m_Id"]: e["m_Key"] for e in tree["m_Entries"]},
                            }
                    except Exception:
                        pass

                # Inject into StringTable
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_TableData" not in tree or "m_LocaleIdentifier" not in tree:
                            continue

                        locale = tree["m_LocaleIdentifier"].get("m_Code", "")
                        shared_path_id = tree.get("m_SharedData", {}).get("m_PathID")
                        shared = shared_tables.get(shared_path_id, {})
                        id_to_key = shared.get("id_to_key", {})
                        collection_name = shared.get("name") or obj.name or "Unknown"

                        entry_modified = False
                        for entry in tree["m_TableData"]:
                            value = entry.get("m_Localized", "")
                            if not value:
                                continue
                            entry_id = entry["m_Id"]
                            key_name = id_to_key.get(entry_id, str(entry_id))
                            path = ["StringTable", collection_name, locale, key_name]
                            sid = make_id(self.engine, rel_path, path, value)
                            if sid in translations:
                                entry["m_Localized"] = translations[sid]
                                entry_modified = True
                                written += 1

                        if entry_modified:
                            data.save_typetree(tree)
                            changed = True
                    except Exception:
                        pass

                if changed:
                    self.backup_file(root, bundle_path)
                    with open(bundle_path, "wb") as f:
                        f.write(env.file.save(packer="none"))

            except Exception as e:
                print(f"Error injecting into bundle {bundle_path}: {e}", file=sys.stderr)

        return written
