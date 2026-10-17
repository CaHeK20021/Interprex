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
import csv
import io
import struct
import tempfile
import shutil
import subprocess
from typing import Any
from collections.abc import Generator
from .base import BaseParser, TranslationString, make_id, read_backup_original
from .unity_sources import (
    SOURCE_CONTENT,
    SOURCE_DLL,
    SOURCE_FONT_GAP,
    SOURCE_IL2CPP_METADATA,
    SOURCE_STREAMING_FILES,
    SOURCE_STRING_TABLE,
    SOURCE_TYPETREE,
    SOURCE_UI_RAW,
    SOURCE_YAML,
    UnityPipelineReport,
    detect_unity_capabilities,
    is_valid_streaming_file,
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
    "Unity.Analytics",
    "Unity.Services",
    "Newtonsoft.Json",
    "AstarPathfindingProject",
    "Pathfinding",
}

_TECHNICAL_TMP_CLASSES = frozenset({
    "TMP_FontAsset", "TMP_Settings", "TMP_StyleSheet", "TMP_SpriteAsset",
    "TMP_ColorGradient", "TextMeshProFont",
})

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
        "cinemachine", "unitask", "unirx", "zenject", "sirenix", "textmeshpro",
        "playfab", "fmodstudio", "icsharpcode", "ionic", "demilib", "ak.wwise",
        "com.rlabrecque", "google.", "firebase", "facebook",
        # Native runtime / C libraries (never .NET managed assemblies)
        "d3dcompiler", "libpython", "librenpy", "libwinpthread", "libffi",
        "libcrypto", "libssl", "libegl", "libgles", "nvdrs", "steam_api",
        "civetweb", "ffmpeg", "avcodec", "avformat", "avutil", "swresample",
        "swscale", "vcruntime", "msvcp", "api-ms-win", "ucrtbase", "concrt",
        "vcomp", "mfc", "atl",
    )
    for pref in deny_prefixes:
        if fn.startswith(pref.lower()):
            return False
    # Unity.* modules ship as Unity.Mathematics.dll etc.
    if fn.startswith("unity"):
        return False
    return True

def is_dotnet_pe(path: str) -> bool:
    """True if the PE binary contains a valid CLR / .NET runtime header (CLI header).

    Prevents invoking Mono.Cecil / DllEditor on unmanaged native C/C++ DLLs
    (Python runtimes, DirectX, graphics drivers, audio wrappers, etc.).
    """
    try:
        with open(path, "rb") as f:
            data = f.read(1024)
            if len(data) < 64 or data[:2] != b"MZ":
                return False
            pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
            f.seek(pe_offset)
            pe_sig = f.read(4)
            if pe_sig != b"PE\x00\x00":
                return False
            f.seek(pe_offset + 24)
            opt_magic_bytes = f.read(2)
            if len(opt_magic_bytes) < 2:
                return False
            opt_magic = struct.unpack("<H", opt_magic_bytes)[0]
            if opt_magic == 0x10B:  # PE32
                f.seek(pe_offset + 24 + 96 + 14 * 8)
            elif opt_magic == 0x20B:  # PE32+
                f.seek(pe_offset + 24 + 112 + 14 * 8)
            else:
                return False
            entry_bytes = f.read(8)
            if len(entry_bytes) < 8:
                return False
            clr_rva, clr_size = struct.unpack("<II", entry_bytes)
            return clr_rva != 0 and clr_size != 0
    except Exception:
        return False


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
    # Game / RPG terms & actions
    "attack", "defense", "defence", "health", "mana", "stamina", "energy",
    "strength", "agility", "dexterity", "intelligence", "wisdom", "luck",
    "charisma", "vitality", "endurance", "perception", "speed", "damage",
    "critical", "armor", "armour", "weapon", "shield", "potion", "victory",
    "defeat", "retry", "chapter", "prologue", "epilogue", "locked", "unlocked",
    "equipped", "completed", "failed", "success", "warning", "resolution",
    "brightness", "sensitivity", "subtitles", "gamepad", "controller",
    "keyboard", "mouse", "gallery", "glossary", "codex", "bestiary", "crafting",
    "skills", "talents", "perks", "stats", "status", "character", "party",
    "guild", "shop", "store", "merchant", "tavern", "dungeon", "enemy",
    "target", "range", "heal", "poison", "bleed", "stun", "freeze", "burn",
    "curse", "rarity", "common", "uncommon", "rare", "epic", "legendary",
    "consumable", "reward", "rewards", "claim", "collect", "loot", "chest",
    "backpack", "stash", "sort", "filter", "search", "clear", "select",
    "inspect", "examine", "read", "talk", "speak", "listen", "look", "open",
    "unlock", "jump", "crouch", "sprint", "walk", "run", "rest", "sleep",
    "wait", "camp", "cook", "repair", "combine", "discard", "destroy", "rename",
    "mute", "unmute", "voice", "music", "sound", "effects", "ambience",
    "accessibility", "autosave", "quicksave", "quickload", "overwrite", "empty",
    "paused", "loading", "saving", "offline", "online", "hello", "goodbye", "thanks",
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

# UnityEvent PersistentCall target type, e.g.
# "Naninovel.PlayScript, Elringus.Naninovel.Runtime" or
# "UnityEngine.Object, UnityEngine". The *next* aligned string is the method
# name (Play / set_text / …) — translating it bricks onClick (Touchstarved:
# Confirm → Play became «Играть» → button no longer invokes PlayScript.Play).
_ASSEMBLY_QUALIFIED_TYPE_RE = re.compile(
    r"^[A-Za-z_][\w.+`\[\]]*,\s*[A-Za-z_][\w.]*"
)


def _looks_like_assembly_qualified_type(s: str) -> bool:
    t = (s or "").strip()
    if not t or "," not in t:
        return False
    return bool(_ASSEMBLY_QUALIFIED_TYPE_RE.match(t))


# Pronoun / pair button labels: "They/Them", "She/Her", "He/Him", "Yes/No".
# Exactly one slash, letters on both sides, no path-like multi-segment junk.
_SLASH_PAIR_LABEL_RE = re.compile(
    r"^[A-Za-z][A-Za-z'.-]*(?:/[A-Za-z][A-Za-z'.-]*)+$"
)


def _is_slash_pair_label(plain: str) -> bool:
    t = (plain or "").strip()
    if not t or "/" not in t or " " in t or "\\" in t:
        return False
    # Exactly one slash: They/Them. Paths like Naninovel/UI/Foo have 2+.
    if t.count("/") != 1 or "." in t:
        return False
    if len(t) > 32:
        return False
    return bool(_SLASH_PAIR_LABEL_RE.match(t))


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
        aligned_end = _aligned_string_end(i, slen)
        if 1 <= slen <= 8000 and aligned_end <= n:
            pad = (4 - (slen % 4)) % 4
            # Reject if padding contains non-zero bytes (prevents false matches on binary floats/arrays)
            if pad > 0 and any(b != 0 for b in raw[i + 4 + slen : aligned_end]):
                i += 1
                continue
            chunk = raw[i + 4 : i + 4 + slen]
            try:
                s = chunk.decode("utf-8")
                if _is_valid_unity_text_string(s):
                    out.append((i, s))
                    i = aligned_end
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
    # Slash-pair captions (They/Them, She/Her) — player-facing UI, not opcodes
    if _is_slash_pair_label(plain):
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

    UnityEvent PersistentCall method names (string after an assembly-qualified
    type) are never translatable — see ``_looks_like_assembly_qualified_type``.

    Other content: sequential slots that pass the player-facing gate.
    """
    if b"SerializationData" in raw or b"SerializedBytes" in raw:
        return []
    nano = _is_naninovel_script_blob(raw)
    seq = _sequential_aligned_strings(raw)
    out: list[tuple[int, str]] = []
    for i, (offset, s) in enumerate(seq):
        t = s.strip()
        if not t:
            continue
        if not nano:
            if offset % 4 != 0 and not (len(s) >= 6 and " " in s):
                continue
        prev = seq[i - 1][1].strip() if i > 0 else ""
        nxt = seq[i + 1][1].strip() if i + 1 < len(seq) else ""
        # Opcode sits immediately before the Commands assembly marker.
        if nxt == "Naninovel.Commands" or nxt.startswith("Naninovel.Commands"):
            continue
        if nxt.startswith("Naninovel.") and "Command" in nxt:
            continue
        # UnityEvent PersistentCall: method name is sandwiched between the
        # target type ("Naninovel.PlayScript, Elringus…") and the Object type
        # ("UnityEngine.Object, UnityEngine"). "Play" is in _KNOWN_UI (valid
        # button *label* elsewhere), but as a method name it must stay English
        # or onClick silently no-ops (Touchstarved Confirm → «Играть»).
        if (
            _looks_like_assembly_qualified_type(prev)
            and nxt.startswith("UnityEngine.Object")
        ):
            continue
        if not _is_player_facing_raw(t, naninovel=nano):
            continue
        out.append((offset, s))
    return out


def _is_debug_or_dev_string(s: str) -> bool:
    """Filter developer logs, C# exceptions, and default Unity engine objects."""
    t = s.strip()
    if not t:
        return False
    # Exceptions and assertions
    if re.search(
        r'\b(NullReferenceException|ArgumentException|ArgumentNullException|InvalidOperationException|'
        r'IndexOutOfRangeException|KeyNotFoundException|NotImplementedException|NotSupportedException|'
        r'FormatException|StackOverflowException|Unhandled exception|Assertion failed)\b',
        t, re.I
    ):
        return True
    # Technical logs
    if re.search(
        r'^(Failed to |Cannot find |Could not find |Unable to |Object reference not set|'
        r'Coroutine couldn\'t|Missing component|Missing prefab|Shader not found|'
        r'Texture .* is not readable|Material .* doesn\'t have|Index out of range|'
        r'Invalid parameter|Error loading |Error parsing |Error initializing )',
        t, re.I
    ):
        return True
    # Default technical GameObject names
    if t.lower() in {
        "main camera", "directional light", "point light", "spot light", "area light",
        "event system", "canvas scaler", "graphic raycaster", "standalone input module",
        "new gameobject", "reflection probe", "post process volume", "global volume", "audio mixer"
    }:
        return True
    return False


def _is_game_text(text: str, *, allow_title_word: bool = False) -> bool:
    t = text.strip()

    # ── Базовые проверки ────────────────────────────────────────────────────
    if len(t) < 2:
        return False
    if not any(c.isalpha() for c in t):
        return False

    if _is_debug_or_dev_string(t):
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
    # Slash-pair UI labels (pronoun buttons): "They/Them", "She/Her", "He/Him".
    # No space → old multi-word gate missed them → buttons stayed English.
    if _is_slash_pair_label(plain): return True
    if plain.lower() in _KNOWN_UI:  return True   # известная UI-метка

    # ── Одно слово: усиленный фильтр ────────────────────────────────────────
    if _CODE_WORD_RE.match(plain):  return False
    if plain.lower() in _KNOWN_CODE: return False

    # Все-капсовое короткое слово без подчеркиваний -> кнопка UI (PLAY, EXIT)
    if plain.isupper() and len(plain) <= 20 and "_" not in plain:
        return True

    # Title-case singles: in structured tables (StringTable, I2, DialogueEntry, CSV)
    # allow words like "Chapter", "Attack", etc. if allow_title_word=True
    if allow_title_word and re.match(r"^[A-Z][a-z]{2,18}$", plain):
        if not _is_engine_identifier(plain) and plain.lower() not in _KNOWN_CODE:
            return True

    # Title-case singles: no auto-accept (Goto / Master / actor ids).
    return False


def _is_il2cpp_safe_literal(s: str) -> bool:
    """Strict filter for string literals in compiled global-metadata.dat.

    Protects against modifying internal C# symbols:
    - Scene names ("Main Menu", "Level 1", "TitleScreen")
    - PlayerPrefs keys, tags, animation states
    - SQL queries, date formats, shader/material properties
    - Single-word UI button labels without punctuation ("Play", "Save", "Load")
    """
    if not _is_game_text(s):
        return False

    t = s.strip()
    words = t.split()
    if len(words) == 1:
        # In IL2CPP code, 95% of single words are method names, scene names, tags, PlayerPrefs
        return False

    low = t.lower()

    # Built-in Unity tags & input axes
    if low in {
        "main menu", "mainmenu", "title menu", "boot", "init",
        "untagged", "respawn", "finish", "editoronly", "maincamera", "gamecontroller",
        "mouse x", "mouse y", "mouse scrollwheel",
    }:
        return False

    # Scene / screen / technical level identifiers (e.g. "Level 1", "Stage 2", "Slot 1", "TitleScreen")
    if re.search(r"\b(Scene|Screen)\b", t, re.I):
        return False
    if re.search(r"^(Level|Stage|Slot)\s+\d+$", t, re.I):
        return False

    # SQL queries
    if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|CREATE TABLE|DROP TABLE|ALTER TABLE|FROM|WHERE)\b", t, re.I):
        return False

    # Date / Time format strings (yyyy-MM-dd, HH:mm:ss, etc.)
    if re.search(r"^(yyyy|MM|dd|HH|mm|ss)[-/ :.A-Za-z0-9%]*$", t):
        return False

    # Code identifiers / Coroutines / Callbacks:
    # camelCase or PascalCase with internal capitals without spaces (e.g. "OnTriggerEnter", "FadeInCoroutine")
    for w in words:
        if re.search(r"^[a-z]+[A-Z]", w) or re.search(r"^[A-Z][a-z]+[A-Z]", w):
            return False

    # Shaders, material props, internal tags (starts with _ or contains / or \)
    if t.startswith("_") or "/" in t or "\\" in t:
        return False

    # URLs, web protocols, asset extensions
    if re.search(r"(http|https|ws|wss|file):|\.(unity|prefab|asset|mat|shader|dll|so|json|xml|txt|csv|png|jpg)$", t, re.I):
        return False

    return True


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
        "new text", "option a", "option b", "option c",
        "tmp settings", "default style sheet", "default sprite asset",
        "panel title", "dialogue options",
        "scene name", "slot 1", "char name", "message text",
        "blue to purple - vertical", "dark to light green - vertical",
        "light to dark green - vertical", "yellow to orange - vertical",
        "red:", "yellow:", "blue:", "green:", "white:", "black:",
        "automatic control", "manual control",
        "break", "test2",
    }):
        return False

    # ── Placeholder / test junk blacklist ───────────────────────────────────
    if low in frozenset({
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
    # "They/Them" / "She/Her" / "He/Him" — real button captions (Touchstarved
    # pronoun picker). Slash is not a space, so the multi-word gate missed them.
    if _is_slash_pair_label(plain):        return True
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


# ── Generic TypeTree and TextAsset Helpers ────────────────────────────────────

_TEXT_FIELD_KEYWORDS = frozenset({
    "text", "texts", "title", "titles", "description", "descriptions",
    "desc", "descs", "caption", "captions", "label", "labels",
    "dialogue", "dialogues", "dialog", "dialogs", "message", "messages",
    "msg", "msgs", "prompt", "prompts", "tooltip", "tooltips",
    "hint", "hints", "bio", "lore", "story", "stories",
    "sentence", "sentences", "phrase", "phrases", "line", "lines",
    "header", "headers", "subtitle", "subtitles", "summary", "summaries",
    "question", "questions", "answer", "answers", "choice", "choices",
    "option", "options", "content", "contents", "bark", "barks",
    "greeting", "greetings", "note", "notes", "quote", "quotes",
    "storytext", "localizedtext", "flavor", "flavortext", "itemname",
    "questname", "skillname", "spellname", "displayname",
    "body", "speech", "narration", "monologue", "objective", "objectives",
    "paragraph", "paragraphs", "tip", "tips", "entry", "entries",
})

_TEXT_FIELD_SUFFIXES = (
    "text", "texts", "title", "titles", "desc", "description", "descriptions",
    "label", "labels", "message", "messages", "tooltip", "tooltips",
    "dialogue", "dialogues", "dialog", "dialogs", "caption", "captions",
    "name", "line", "lines", "choice", "choices", "option", "options",
    "hint", "hints", "note", "notes", "prompt", "prompts",
)

_TECHNICAL_NAME_EXCLUSIONS = frozenset({
    "prefabname", "scenename", "assetname", "filename", "shadername",
    "methodname", "typename", "bonename", "layername", "tagname",
    "statename", "animname", "clipname", "parametername", "variablename",
    "keyname", "idname", "objectname", "scriptname", "gamename",
    "soundname", "audioname", "fontname", "texturename", "materialname",
    "guid", "type", "guidname", "meshname", "skinname", "jointname",
})

_SKIP_TYPETREE_BRANCHES = frozenset({
    "m_GameObject", "m_Script", "m_Enabled", "m_Name", "m_Father",
    "m_Children", "m_Component", "m_Transform", "m_ObjectHideFlags",
    "m_CorrespondingSourceObject", "m_PrefabInstance", "m_PrefabAsset",
    "m_VisualElementAssets",
})


def _is_typetree_text_field(field_name: str) -> bool:
    clean = field_name.lower().removeprefix("m_")
    if clean in _TEXT_FIELD_KEYWORDS:
        return True
    if any(clean.endswith(sfx) for sfx in _TEXT_FIELD_SUFFIXES):
        if clean not in _TECHNICAL_NAME_EXCLUSIONS:
            return True
    return False


def _walk_typetree_generic(node: Any, path_parts: list[str]) -> list[tuple[list[str], str]]:
    """Recursively yield (path_parts, text) for translatable string fields in a typetree."""
    out: list[tuple[list[str], str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _SKIP_TYPETREE_BRANCHES:
                continue
            if isinstance(v, str):
                if _is_typetree_text_field(k) and _is_game_text(v, allow_title_word=True):
                    out.append((path_parts + [k], v))
            elif isinstance(v, (dict, list)):
                out.extend(_walk_typetree_generic(v, path_parts + [k]))
    elif isinstance(node, list):
        parent_field = (
            path_parts[-2]
            if len(path_parts) >= 2 and (path_parts[-1] == "Array" or path_parts[-1].isdigit())
            else (path_parts[-1] if path_parts else "")
        )
        for idx, item in enumerate(node):
            if isinstance(item, (dict, list)):
                out.extend(_walk_typetree_generic(item, path_parts + [str(idx)]))
            elif (
                isinstance(item, str)
                and parent_field
                and _is_typetree_text_field(parent_field)
                and _is_game_text(item, allow_title_word=True)
            ):
                out.append((path_parts + [str(idx)], item))
    return out


def _set_typetree_by_path(tree: Any, path_parts: list[str], value: str) -> bool:
    """Set value at nested path inside a dictionary / list structure."""
    if not path_parts:
        return False
    cur = tree
    for p in path_parts[:-1]:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and p.isdigit() and int(p) < len(cur):
            cur = cur[int(p)]
        else:
            return False
    last = path_parts[-1]
    if isinstance(cur, dict) and last in cur:
        cur[last] = value
        return True
    elif isinstance(cur, list) and last.isdigit() and int(last) < len(cur):
        cur[int(last)] = value
        return True
    return False


_JSON_STRICT_TECHNICAL_KEYS = frozenset({
    "$type", "m_script", "guid", "hash", "version", "color", "shader", "material", "layer", "tag",
})

_JSON_SKIP_KEYS = frozenset({
    "id", "guid", "type", "version", "hash", "key", "$type", "m_script",
    "scene", "scenename", "prefab", "asset", "path", "file", "filename",
    "event", "eventname", "audio", "sound", "sfx", "bgm", "music", "voice",
    "anim", "animation", "animator", "sprite", "image", "icon", "portrait",
    "texture", "material", "shader", "tag", "layer", "command", "cmd",
    "action", "method", "callback", "target", "variable", "var", "flag",
    "condition", "expression", "script", "node", "next", "goto", "branch",
    "speakerid", "actorid", "characterid", "item_id", "quest_id", "color",
})

_JSON_ALLOWED_TEXT_SUFFIXES = frozenset({
    "text", "title", "name", "desc", "caption", "message", "dialogue", "dialog", "content", "string",
})


def _is_technical_id_token(s: str) -> bool:
    """Return True if s looks like a code identifier or technical ID."""
    if not s or " " in s:
        return False
    if s.lower() in _KNOWN_UI:
        return False
    if "_" in s or "/" in s or "\\" in s:
        return True
    if any(c.isdigit() for c in s):
        return True
    if re.search(r"[a-z][A-Z]", s):
        return True
    if s.isupper() and len(s) <= 4:
        return True
    return False


def _walk_json_leaves(node: Any, path: list[str]) -> list[tuple[list[str], str]]:
    """Recursively find string leaves in generic JSON."""
    out: list[tuple[list[str], str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            k_low = k.lower()
            if k_low in _JSON_STRICT_TECHNICAL_KEYS:
                continue

            is_strict_skip_key = (
                k_low in _JSON_SKIP_KEYS
                or k_low.endswith(("path", "file", "prefab", "scene", "sprite", "sound", "sfx", "anim"))
            )
            is_id_or_key = (
                k_low.endswith(("id", "_id", "key", "_key", "term", "_term"))
                and not any(k_low.endswith(suf) for suf in _JSON_ALLOWED_TEXT_SUFFIXES)
            )

            if isinstance(v, str):
                v_str = v.strip()
                if is_strict_skip_key:
                    if "/" in v_str or "\\" in v_str:
                        continue
                    is_sentence = (" " in v_str and len(v_str.split()) >= 3) or any(p in v_str for p in ".?!")
                    if not is_sentence:
                        continue
                elif is_id_or_key:
                    if "/" in v_str or "\\" in v_str:
                        continue
                    if _is_technical_id_token(v_str):
                        continue
                out.append((path + [k], v))
            elif isinstance(v, (dict, list)):
                out.extend(_walk_json_leaves(v, path + [k]))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                out.append((path + [str(i)], item))
            elif isinstance(item, (dict, list)):
                out.extend(_walk_json_leaves(item, path + [str(i)]))
    return out


def _walk_ink_root(root_node: Any, current_idx: list[int]) -> list[tuple[int, str]]:
    """Walk Ink JSON root container to extract strings starting with '^'."""
    out: list[tuple[int, str]] = []
    if isinstance(root_node, list):
        for item in root_node:
            if isinstance(item, str):
                idx = current_idx[0]
                current_idx[0] += 1
                if item.startswith("^"):
                    out.append((idx, item))
            elif isinstance(item, (list, dict)):
                out.extend(_walk_ink_root(item, current_idx))
    elif isinstance(root_node, dict):
        for k, v in root_node.items():
            if isinstance(v, (list, dict)):
                out.extend(_walk_ink_root(v, current_idx))
            elif isinstance(v, str):
                idx = current_idx[0]
                current_idx[0] += 1
                if v.startswith("^"):
                    out.append((idx, v))
    return out


def _replace_ink_root(root_node: Any, current_idx: list[int], replacements: dict[int, str]) -> bool:
    """Walk Ink JSON and replace strings by index."""
    modified = False
    if isinstance(root_node, list):
        for i, item in enumerate(root_node):
            if isinstance(item, str):
                idx = current_idx[0]
                current_idx[0] += 1
                if idx in replacements:
                    root_node[i] = "^" + replacements[idx]
                    modified = True
            elif isinstance(item, (list, dict)):
                if _replace_ink_root(item, current_idx, replacements):
                    modified = True
    elif isinstance(root_node, dict):
        for k, v in root_node.items():
            if isinstance(v, (list, dict)):
                if _replace_ink_root(v, current_idx, replacements):
                    modified = True
            elif isinstance(v, str):
                idx = current_idx[0]
                current_idx[0] += 1
                if idx in replacements:
                    root_node[k] = "^" + replacements[idx]
                    modified = True
    return modified


_XML_TRANSLATABLE_ATTRS = frozenset({
    "text", "value", "label", "title", "desc", "description", "caption", "dialogue",
})


def _extract_xml_strings(xml_text: str) -> list[tuple[str, str]]:
    """Extract translatable strings from XML text using ElementTree."""
    import xml.etree.ElementTree as ET
    out: list[tuple[str, str]] = []
    clean_text = xml_text.lstrip()
    if clean_text.startswith("\ufeff"):
        clean_text = clean_text[1:].lstrip()
    try:
        root_elem = ET.fromstring(clean_text)
    except Exception:
        return []

    def _walk(elem: ET.Element, path: str) -> None:
        if elem.text:
            t = elem.text.strip()
            if t and _is_game_text(t, allow_title_word=True):
                out.append((f"{path}:text", t))
        for attr_name, attr_val in elem.attrib.items():
            if attr_name.lower() in _XML_TRANSLATABLE_ATTRS and isinstance(attr_val, str):
                v = attr_val.strip()
                if v and _is_game_text(v, allow_title_word=True):
                    out.append((f"{path}:attr:{attr_name}", v))
        for idx, child in enumerate(elem):
            _walk(child, f"{path}.{idx}")

    _walk(root_elem, "0")
    return out


def _inject_xml_strings(xml_text: str, replacements: dict[str, str]) -> str:
    """Inject translations into XML text using ElementTree."""
    import xml.etree.ElementTree as ET
    has_bom = xml_text.startswith("\ufeff")
    clean_text = xml_text[1:] if has_bom else xml_text
    has_decl = clean_text.lstrip().startswith("<?xml")
    root_elem = ET.fromstring(clean_text)

    def _inject(elem: ET.Element, path: str) -> None:
        k = f"{path}:text"
        if k in replacements and elem.text is not None:
            lws = elem.text[: len(elem.text) - len(elem.text.lstrip())]
            tws = elem.text[len(elem.text.rstrip()) :]
            elem.text = lws + replacements[k] + tws
        for a in list(elem.attrib.keys()):
            ak = f"{path}:attr:{a}"
            if ak in replacements:
                elem.attrib[a] = replacements[ak]
        for idx, child in enumerate(elem):
            _inject(child, f"{path}.{idx}")

    _inject(root_elem, "0")
    res = ET.tostring(root_elem, encoding="utf-8", xml_declaration=has_decl).decode("utf-8")
    if has_bom:
        res = "\ufeff" + res
    return res


_LANG_TO_ISO_CODES: dict[str, tuple[str, ...]] = {
    "ru": ("ru", "ru-ru", "russian", "русский"),
    "russian": ("ru", "ru-ru", "russian", "русский"),
    "uk": ("uk", "uk-ua", "ukrainian", "українська"),
    "ukrainian": ("uk", "uk-ua", "ukrainian", "українська"),
    "es": ("es", "es-es", "es-mx", "es-419", "spanish", "español"),
    "spanish": ("es", "es-es", "es-mx", "es-419", "spanish", "español"),
    "de": ("de", "de-de", "german", "deutsch"),
    "german": ("de", "de-de", "german", "deutsch"),
    "fr": ("fr", "fr-fr", "fr-ca", "french", "français"),
    "french": ("fr", "fr-fr", "fr-ca", "french", "français"),
    "it": ("it", "it-it", "italian", "italiano"),
    "italian": ("it", "it-it", "italian", "italiano"),
    "pt": ("pt", "pt-br", "pt-pt", "portuguese", "português"),
    "portuguese": ("pt", "pt-br", "pt-pt", "portuguese", "português"),
    "pl": ("pl", "pl-pl", "polish", "polski"),
    "polish": ("pl", "pl-pl", "polish", "polski"),
    "tr": ("tr", "tr-tr", "turkish", "türkçe"),
    "turkish": ("tr", "tr-tr", "turkish", "türkçe"),
    "ja": ("ja", "ja-jp", "jp", "japanese", "日本語"),
    "japanese": ("ja", "ja-jp", "jp", "japanese", "日本語"),
    "zh": ("zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "cn", "chinese", "中文", "简体中文", "繁體中文"),
    "chinese": ("zh", "zh-cn", "zh-hans", "zh-tw", "zh-hant", "cn", "chinese", "中文", "简体中文", "繁體中文"),
    "ko": ("ko", "ko-kr", "kr", "korean", "한국어"),
    "korean": ("ko", "ko-kr", "kr", "korean", "한국어"),
}


def _matches_unity_locale(code: str, name: str, target_lang: str | None) -> bool:
    if not target_lang:
        return False
    t_low = target_lang.strip().lower()
    if not t_low:
        return False
    t_code = re.split(r"[-_\s(]", t_low)[0]
    aliases = _LANG_TO_ISO_CODES.get(t_low) or _LANG_TO_ISO_CODES.get(t_code) or (t_low, t_code)
    c_low = (code or "").strip().lower()
    n_low = (name or "").strip().lower()
    for alias in aliases:
        if not alias:
            continue
        if len(alias) <= 3:
            # Short ISO code: match exact code or prefix with delimiter (es-es, es_mx, es(la))
            if c_low == alias or c_low.startswith((alias + "-", alias + "_", alias + " ", alias + "(")):
                return True
        else:
            # Full language word: safe to match in name or code
            if alias in n_low or alias in c_low:
                return True
    return False


def _resolve_i2_lang_indices(tree: dict, target_lang: str | None = None) -> tuple[int, list[int]]:
    """Find source (English/primary) language index and target language indices in I2 LanguageSource."""
    m_langs = None
    if "mLanguages" in tree and isinstance(tree["mLanguages"], list):
        m_langs = tree["mLanguages"]
    elif "mSource" in tree and isinstance(tree["mSource"], dict):
        ms = tree["mSource"]
        if "mLanguages" in ms and isinstance(ms.get("mLanguages"), list):
            m_langs = ms["mLanguages"]

    if not m_langs:
        return 0, []

    src_idx = 0
    for idx, lang_info in enumerate(m_langs):
        if not isinstance(lang_info, dict):
            continue
        code = str(lang_info.get("Code", "")).strip().lower()
        name = str(lang_info.get("Name", "")).strip().lower()
        if code in ("en", "en-us", "en-gb") or "english" in name:
            src_idx = idx
            break

    target_indices: list[int] = []
    if target_lang:
        for idx, lang_info in enumerate(m_langs):
            if not isinstance(lang_info, dict):
                continue
            code = str(lang_info.get("Code", ""))
            name = str(lang_info.get("Name", ""))
            if _matches_unity_locale(code, name, target_lang):
                if idx not in target_indices:
                    target_indices.append(idx)

    return src_idx, target_indices


def _save_typetree_safe(obj: Any, data: Any, tree: dict) -> None:
    """Save TypeTree safely across different UnityPy ObjectReader / Object versions."""
    if hasattr(obj, "save_typetree"):
        obj.save_typetree(tree)
    elif data is not None and hasattr(data, "save_typetree"):
        data.save_typetree(tree)
    elif hasattr(obj, "save"):
        obj.save()
    elif data is not None and hasattr(data, "save"):
        data.save()


_VTA_TEXT_KEYS = frozenset({"text", "label", "title", "tooltip", "placeholder-text", "placeholder"})


def _extract_from_visual_tree_asset(
    tree: dict,
    path_id: int,
    rel_path: str,
    base_name: str,
    mk_fn: Any,
) -> list[TranslationString]:
    out: list[TranslationString] = []
    ve_assets = tree.get("m_VisualElementAssets")
    if not isinstance(ve_assets, list):
        return out

    for ve_idx, ve in enumerate(ve_assets):
        if not isinstance(ve, dict):
            continue

        ve_text = ve.get("m_Text")
        if isinstance(ve_text, str) and ve_text.strip() and _is_game_text(ve_text):
            path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), "m_Text"]
            ctx = f"VisualTreeAsset: {base_name}, Element: {ve_idx}, Field: m_Text"
            out.append(mk_fn(rel_path, path, ve_text, ctx))

        props = ve.get("m_Properties")
        if not isinstance(props, list):
            props = ve.get("m_SerializedProperties")
        if not isinstance(props, list):
            continue

        is_flat_strings = len(props) > 0 and all(isinstance(x, str) for x in props)
        if is_flat_strings:
            p_idx = 0
            while p_idx < len(props):
                item = props[p_idx]
                k = item.lower().strip()
                if k in _VTA_TEXT_KEYS and p_idx + 1 < len(props):
                    val = props[p_idx + 1]
                    if isinstance(val, str) and val.strip() and _is_game_text(val):
                        prop_idx = p_idx + 1
                        path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                        ctx = f"VisualTreeAsset: {base_name}, Element: {ve_idx}, Prop: {k}"
                        out.append(mk_fn(rel_path, path, val, ctx))
                    p_idx += 2
                elif "=" in item or ":" in item:
                    sep = "=" if "=" in item else ":"
                    k_part, val_part = item.split(sep, 1)
                    if k_part.lower().strip() in _VTA_TEXT_KEYS:
                        val = val_part.strip()
                        if val and _is_game_text(val):
                            path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(p_idx)]
                            ctx = f"VisualTreeAsset: {base_name}, Element: {ve_idx}, Prop: {k_part.strip()}"
                            out.append(mk_fn(rel_path, path, val, ctx))
                    p_idx += 1
                else:
                    p_idx += 1
        else:
            for prop_idx, prop in enumerate(props):
                if isinstance(prop, dict):
                    name = str(prop.get("name") or prop.get("m_Name") or prop.get("key") or prop.get("m_Property") or "").lower().strip()
                    if name in _VTA_TEXT_KEYS:
                        val = prop.get("value") if "value" in prop else (prop.get("m_Value") if "m_Value" in prop else prop.get("val"))
                        if isinstance(val, str) and val.strip() and _is_game_text(val):
                            path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                            ctx = f"VisualTreeAsset: {base_name}, Element: {ve_idx}, Prop: {name}"
                            out.append(mk_fn(rel_path, path, val, ctx))
                    else:
                        for k, v in prop.items():
                            if k.lower().strip() in _VTA_TEXT_KEYS and isinstance(v, str) and v.strip() and _is_game_text(v):
                                path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                                ctx = f"VisualTreeAsset: {base_name}, Element: {ve_idx}, Prop: {k}"
                                out.append(mk_fn(rel_path, path, v, ctx))
                                break
    return out


def _inject_into_visual_tree_asset(
    tree: dict,
    path_id: int,
    rel_path: str,
    translations: dict[str, str],
    engine: str,
) -> int:
    ve_assets = tree.get("m_VisualElementAssets")
    if not isinstance(ve_assets, list):
        return 0

    hits = 0
    for ve_idx, ve in enumerate(ve_assets):
        if not isinstance(ve, dict):
            continue

        ve_text = ve.get("m_Text")
        if isinstance(ve_text, str) and ve_text.strip():
            path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), "m_Text"]
            sid = make_id(engine, rel_path, path, ve_text)
            if sid in translations:
                ve["m_Text"] = translations[sid]
                hits += 1

        props = ve.get("m_Properties")
        if not isinstance(props, list):
            props = ve.get("m_SerializedProperties")
        if not isinstance(props, list):
            continue

        is_flat_strings = len(props) > 0 and all(isinstance(x, str) for x in props)
        if is_flat_strings:
            p_idx = 0
            while p_idx < len(props):
                item = props[p_idx]
                k = item.lower().strip()
                if k in _VTA_TEXT_KEYS and p_idx + 1 < len(props):
                    val = props[p_idx + 1]
                    if isinstance(val, str) and val.strip():
                        prop_idx = p_idx + 1
                        path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                        sid = make_id(engine, rel_path, path, val)
                        if sid in translations:
                            props[prop_idx] = translations[sid]
                            hits += 1
                    p_idx += 2
                elif "=" in item or ":" in item:
                    sep = "=" if "=" in item else ":"
                    k_part, val_part = item.split(sep, 1)
                    if k_part.lower().strip() in _VTA_TEXT_KEYS:
                        val = val_part.strip()
                        if val:
                            path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(p_idx)]
                            sid = make_id(engine, rel_path, path, val)
                            if sid in translations:
                                props[p_idx] = f"{k_part}{sep}{translations[sid]}"
                                hits += 1
                    p_idx += 1
                else:
                    p_idx += 1
        else:
            for prop_idx, prop in enumerate(props):
                if isinstance(prop, dict):
                    name = str(prop.get("name") or prop.get("m_Name") or prop.get("key") or prop.get("m_Property") or "").lower().strip()
                    if name in _VTA_TEXT_KEYS:
                        val_key = "value" if "value" in prop else ("m_Value" if "m_Value" in prop else ("val" if "val" in prop else None))
                        if val_key:
                            orig_val = prop[val_key]
                            if isinstance(orig_val, str) and orig_val.strip():
                                path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                                sid = make_id(engine, rel_path, path, orig_val)
                                if sid in translations:
                                    prop[val_key] = translations[sid]
                                    hits += 1
                    else:
                        for k, v in list(prop.items()):
                            if k.lower().strip() in _VTA_TEXT_KEYS and isinstance(v, str) and v.strip():
                                path = ["Asset", "VisualTreeAsset", str(path_id), str(ve_idx), str(prop_idx)]
                                sid = make_id(engine, rel_path, path, v)
                                if sid in translations:
                                    prop[k] = translations[sid]
                                    hits += 1
                                    break
    return hits


def _resolve_shared_table(
    tree: dict,
    obj: Any,
    data: Any,
    global_shared_tables: dict[int, dict],
    all_shared_list: list[dict],
) -> dict:
    """Resolve matching SharedTableData for a StringTable using path_id, hash overlap, or collection name prefix."""
    shared_path_id = tree.get("m_SharedData", {}).get("m_PathID")
    if shared_path_id in global_shared_tables:
        cand = global_shared_tables[shared_path_id]
        table_ids = {e.get("m_Id") for e in tree.get("m_TableData", []) if isinstance(e, dict) and "m_Id" in e}
        if not table_ids or (table_ids & cand["id_to_key"].keys()):
            return cand

    # Fallback 1: match by 64-bit m_Id key hash intersection
    table_ids = {e.get("m_Id") for e in tree.get("m_TableData", []) if isinstance(e, dict) and "m_Id" in e}
    if table_ids:
        best_match = None
        best_overlap = 0
        for cand in all_shared_list:
            overlap = len(table_ids & cand["id_to_key"].keys())
            if overlap > best_overlap:
                best_overlap = overlap
                best_match = cand
        if best_match is not None and best_overlap > 0:
            return best_match

    # Fallback 2: match by m_Name prefix (e.g. "UI_Strings_en" or "UI_Strings (en)" starts with "UI_Strings")
    obj_name = (
        tree.get("m_Name")
        or getattr(obj, "name", None)
        or getattr(data, "name", "")
        or ""
    )
    if obj_name:
        for cand in all_shared_list:
            cname = cand.get("name") or ""
            if cname and (
                obj_name == cname
                or obj_name.startswith(cname + "_")
                or obj_name.startswith(cname + " ")
                or obj_name.startswith(cname + "-")
                or obj_name.startswith(cname + "(")
            ):
                return cand

    return global_shared_tables.get(shared_path_id, {})


def _detect_csv_delim(text: str) -> str | None:
    """Detect CSV delimiter (comma, tab, semicolon) from first non-empty line."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        counts = {",": line.count(","), "\t": line.count("\t"), ";": line.count(";")}
        best = max(counts, key=counts.get)
        if counts[best] > 0:
            return best
    return None


_NON_EN_LANG_SUFFIXES = (
    "_fr", ".fr", " fr", "-fr",
    "_de", ".de", " de", "-de",
    "_es", ".es", " es", "-es",
    "_ru", ".ru", " ru", "-ru",
    "_ja", ".ja", " ja", "-ja", "_jp", ".jp", " jp", "-jp",
    "_zh", ".zh", " zh", "-zh", "_cn", ".cn", " cn", "-cn",
    "_ko", ".ko", " ko", "-ko", "_kr", ".kr", " kr", "-kr",
    "_pt", ".pt", " pt", "-pt",
    "_it", ".it", " it", "-it",
    "_pl", ".pl", " pl", "-pl",
    "_tr", ".tr", " tr", "-tr",
    "_uk", ".uk", " uk", "-uk",
)


def _select_csv_text_columns(header: list[str]) -> list[int]:
    """Select text columns from CSV header, prioritizing English/source and filtering other languages."""
    if not header:
        return []

    cleaned_header = [h.strip().lower() for h in header]

    # 1. Exact or suffix match for English / source columns
    en_target_names = {
        "en", "english", "en-us", "en_us", "en-gb", "en_gb",
        "source", "original", "default",
        "text_en", "text-en", "dialogue_en", "dialog_en",
    }
    en_cols = []
    for ci, col in enumerate(cleaned_header):
        if col in en_target_names or col.endswith(("_en", ".en", " en", "-en")):
            en_cols.append(ci)
    if en_cols:
        return en_cols

    # 2. Generic text column names, excluding explicit non-English suffixes
    generic_target_names = {
        "text", "value", "translation", "dialogue", "dialog",
        "content", "string", "caption", "message", "desc", "description",
    }
    gen_cols = []
    for ci, col in enumerate(cleaned_header):
        if any(col.endswith(suf) or col == suf.strip(" ._-") for suf in _NON_EN_LANG_SUFFIXES):
            continue
        if col in generic_target_names or any(tn in col for tn in ("text", "dialog", "content", "message")):
            gen_cols.append(ci)
    if gen_cols:
        return gen_cols

    # 3. Fallback: if 2nd column exists and isn't non-English language tag, use column 1
    if len(cleaned_header) >= 2:
        col1 = cleaned_header[1]
        if not any(col1.endswith(suf) or col1 == suf.strip(" ._-") for suf in _NON_EN_LANG_SUFFIXES):
            return [1]

    return []


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

_MANAGED_MARKERS = frozenset({
    "assembly-csharp.dll",
    "unityengine.coremodule.dll",
    "unityengine.dll",
    "mscorlib.dll",
    "netstandard.dll",
})

def find_managed_dir(root: str) -> str | None:
    """Find the Managed directory containing Assembly-CSharp.dll or core runtime assemblies."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in IGNORE_DIRS and not d.startswith(".")]
        if os.path.basename(dirpath) == "Managed":
            if any(f.lower() in _MANAGED_MARKERS for f in filenames):
                return dirpath
    return None

def _safe_yaml_unescape(s: str) -> str:
    def _repl(m: re.Match[str]) -> str:
        seq = m.group(0)
        if seq == r"\n": return "\n"
        if seq == r"\r": return "\r"
        if seq == r"\t": return "\t"
        if seq == r'\"': return '"'
        if seq == r"\\": return "\\"
        try:
            return chr(int(seq[2:], 16))
        except ValueError:
            return seq
    return re.sub(r'\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|[nrt"\\])', _repl, s)

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
            val = _safe_yaml_unescape(raw_val[1:-1])
    elif raw_val.startswith("'") and raw_val.endswith("'"):
        val = raw_val[1:-1].replace("''", "'")
    else:
        if "\\" in raw_val:
            val = _safe_yaml_unescape(raw_val)
        else:
            val = raw_val

    if val is not None:
        try:
            val = val.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        except Exception:
            pass

    return val, line_idx


def iter_files(root: str, sub_paths: list[str] | None = None) -> Generator[str, None, None]:
    paths_to_walk = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
    for start_path in paths_to_walk:
        if os.path.isfile(start_path):
            yield start_path
            continue
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


def _is_yaml_asset(fpath: str) -> bool:
    """Check if a .unity / .prefab / .asset file is text YAML or binary SerializedFile."""
    try:
        with open(fpath, "rb") as f:
            head = f.read(64)
        if head.startswith(b"\xef\xbb\xbf"):
            head = head[3:]
        head = head.lstrip()
        if head.startswith(b"%YAML") or head.startswith(b"--- !u!"):
            return True
        if not fpath.lower().endswith(".asset") and any(head.startswith(kw) for kw in (b"MonoBehaviour:", b"GameObject:", b"%YAML")):
            return True
        return False
    except Exception:
        return False


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
        self._modified_files_in_run: set[str] = set()
        self._discovered_loc_files: set[str] = set()

    def _mark_modified_in_run(self, fpath: str) -> None:
        if not hasattr(self, "_modified_files_in_run"):
            self._modified_files_in_run = set()
        self._modified_files_in_run.add(os.path.normpath(fpath))

    def _is_modified_in_run(self, fpath: str) -> bool:
        if not hasattr(self, "_modified_files_in_run"):
            return False
        return os.path.normpath(fpath) in self._modified_files_in_run

    def _read_asset_backup_original(self, root: str, fpath: str, rel_path: str) -> bytes | None:
        """Read the original un-injected bytes of an asset.

        For split assets (.split0), reconstitutes the full original asset by concatenating
        all backup parts (.split0, .split1, ...) or reading un-injected disk parts.
        Returns None if no backup exists for this file or its parts.
        """
        if fpath.lower().endswith(".split0"):
            base_fpath = fpath[:-7]
            base_rel = rel_path[:-7]
            chunks: list[bytes] = []
            any_backed_up = False
            idx = 0
            while True:
                part_rel = f"{base_rel}.split{idx}"
                part_fpath = f"{base_fpath}.split{idx}"
                part_orig = read_backup_original(root, part_rel)
                if part_orig is not None:
                    any_backed_up = True
                    chunks.append(part_orig)
                elif os.path.isfile(part_fpath):
                    try:
                        with open(part_fpath, "rb") as pf:
                            chunks.append(pf.read())
                    except Exception:
                        break
                else:
                    break
                idx += 1

            if any_backed_up and chunks:
                return b"".join(chunks)
            return None
        return read_backup_original(root, rel_path)

    def _save_or_split_asset(self, root: str, fpath: str, data: bytes) -> None:
        """Save asset data to disk.

        If fpath is a split asset (.split0), slices data into chunks matching the
        original split size, backs up all existing parts, overwrites/creates
        .split0, .split1, ..., and deletes any leftover stale split files.
        Otherwise backs up and writes the single asset file.
        """
        if fpath.lower().endswith(".split0"):
            base_prefix = fpath[:-7]
            existing_parts: list[str] = []
            idx = 0
            while True:
                part_path = f"{base_prefix}.split{idx}"
                if os.path.isfile(part_path):
                    existing_parts.append(part_path)
                    idx += 1
                else:
                    break

            for part in existing_parts:
                self.backup_file(root, part)

            chunk_size = 0
            part_split1 = f"{base_prefix}.split1"
            if os.path.isfile(part_split1):
                try:
                    chunk_size = os.path.getsize(part_split1)
                except Exception:
                    chunk_size = 0
            if chunk_size <= 0 and os.path.isfile(fpath):
                try:
                    chunk_size = os.path.getsize(fpath)
                except Exception:
                    chunk_size = 0
            if chunk_size <= 0:
                chunk_size = max(len(data), 1024 * 1024)

            num_chunks = max(1, (len(data) + chunk_size - 1) // chunk_size) if data else 1
            for k in range(num_chunks):
                part_path = f"{base_prefix}.split{k}"
                chunk = data[k * chunk_size : (k + 1) * chunk_size]
                with open(part_path, "wb") as pf:
                    pf.write(chunk)
                self._mark_modified_in_run(part_path)

            for k in range(num_chunks, len(existing_parts)):
                leftover_part = f"{base_prefix}.split{k}"
                if os.path.isfile(leftover_part):
                    try:
                        os.remove(leftover_part)
                    except Exception:
                        pass
            self._mark_modified_in_run(fpath)
        else:
            self.backup_file(root, fpath)
            with open(fpath, "wb") as f:
                f.write(data)
            self._mark_modified_in_run(fpath)


    def engine_prompt_addon(self) -> str:
        return (
            "UNITY LOCALIZATION RULES:\n"
            "CONTEXT-ADAPTIVE TONE:\n"
            "  • DIALOGUE & STORY (when context mentions 'Kind: content', 'Speaker', 'Actor', 'Conversation', "
            "'DialogueEntry', 'Ink', 'Yarn', or shows 'Prev'/'Next' lines): translate as natural, expressive spoken "
            "dialogue or literary narrative matching character voice. Default to informal address ('ты') among peers/friends "
            "unless hierarchy or strangers dictate formal ('вы').\n"
            "  • UI & MENUS (when context mentions 'GameObject', 'Type: Text', 'VisualTreeAsset', or short UI keys): "
            "use a concise, idiomatic game interface style.\n"
            "FORMAT SPECIFIERS & TAGS: preserve {0}, {1}, {0:0.0}, {UserName}, %s, %d, [var], and rich-text tags "
            "like <color=#...>, </color>, <size=...>, <b>, <i>, <sprite=...>, <link=...> EXACTLY as they appear.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is inside strings."
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
        else:
            # Check for IL2CPP: GameAssembly.dll / GameAssembly.so and global-metadata.dat
            try:
                from UnityPy.helpers.TypeTreeGenerator import TypeTreeGenerator
                ga_path = None
                gm_path = None

                for candidate_ga in ("GameAssembly.dll", "GameAssembly.so"):
                    p = os.path.join(root, candidate_ga)
                    if os.path.isfile(p):
                        ga_path = p
                        break

                if not ga_path:
                    for dirpath, _, filenames in os.walk(root):
                        for fn in filenames:
                            if fn.lower() in ("gameassembly.dll", "gameassembly.so"):
                                ga_path = os.path.join(dirpath, fn)
                                break
                        if ga_path:
                            break

                if ga_path:
                    for dirpath, _, filenames in os.walk(root):
                        for fn in filenames:
                            if fn.lower() == "global-metadata.dat":
                                gm_path = os.path.join(dirpath, fn)
                                break
                        if gm_path:
                            break

                if ga_path and gm_path and os.path.isfile(ga_path) and os.path.isfile(gm_path):
                    try:
                        generator = TypeTreeGenerator(unity_version)
                        with open(ga_path, "rb") as f_ga, open(gm_path, "rb") as f_gm:
                            generator.load_il2cpp(f_ga.read(), f_gm.read())
                    except Exception as e:
                        print(f"Failed to initialize IL2CPP TypeTreeGenerator: {e}", file=sys.stderr)
            except ImportError:
                pass

        self._generator_cache[cache_key] = generator
        return generator

    def _attach_generator(self, root: str, env: Any) -> None:
        unity_version = None
        for obj in env.objects:
            unity_version = getattr(obj.assets_file, "unity_version", None)
            if unity_version:
                break
        generator = self._get_generator(root, unity_version)
        if generator:
            env.typetree_generator = generator

    def _scan_asset_files(self, root: str, sub_paths: list[str] | None = None) -> tuple[list[str], list[str]]:
        compiled_files = []
        source_files = []
        for fpath in iter_files(root, sub_paths):
            f = os.path.basename(fpath)
            f_lower = f.lower()
            if f_lower == "data.unity3d" or f_lower.endswith((".bundle", ".assetbundle")):
                compiled_files.append(fpath)
            elif f_lower.endswith((".assets", ".split0")) or (f_lower.startswith("level") and "." not in f):
                if not f_lower.endswith(".manifest") and not f_lower.endswith(".resS") and not f_lower.endswith(".resource") and f_lower != "level":
                    compiled_files.append(fpath)
            elif f_lower.endswith((".unity", ".prefab", ".asset")):
                if _is_yaml_asset(fpath):
                    source_files.append(fpath)
                else:
                    compiled_files.append(fpath)
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

        # Ren'Py signature protection: if it's a Ren'Py game, do not detect as Unity.
        if os.path.isdir(os.path.join(root, "renpy")) or os.path.isfile(os.path.join(root, "renpy.py")):
            return False
        if os.path.isdir(os.path.join(root, "game")):
            for marker in ("options.rpy", "options.rpyc", "script.rpy", "script.rpyc", "archive.rpa", "data.rpa"):
                if os.path.isfile(os.path.join(root, "game", marker)):
                    return False

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
                if f_lower.startswith("assembly-csharp") or is_dotnet_pe(fpath):
                    return True
            if f_lower in ("data.unity3d", "global-metadata.dat", "gameassembly.dll", "gameassembly.so"):
                return True
            if f_lower.endswith(".assetbundle") or f_lower.endswith(".bundle"):
                return True
            if f_lower.endswith((".assets", ".split0")) or f_lower.startswith("level"):
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
        # ── il2cpp_metadata ───────────────────────────────────────────────
        if any(s.id == SOURCE_IL2CPP_METADATA and s.enabled for s in report.sources):
            before = len(results)
            results.extend(self._extract_il2cpp_metadata(root, sub_paths))
            report.record(SOURCE_IL2CPP_METADATA, len(results) - before, enabled=True)
        else:
            report.record(SOURCE_IL2CPP_METADATA, 0, enabled=False, reason="not an IL2CPP build")

        # ── compiled assets: typetree + content_blob + conditional ui_raw ──
        # YAML is scanned in the same helper (separate source id in report).
        asset_counts = self._extract_assets(root, sub_paths, report=report)
        results.extend(asset_counts["strings"])

        # ── string_table (Addressables / discovered in assets) ─────────────
        loc_enabled = any(s.id == SOURCE_STRING_TABLE and s.enabled for s in report.sources) or bool(getattr(self, "_discovered_loc_files", None))
        if loc_enabled:
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

        # ── streaming_files (External files under StreamingAssets) ─────────
        if any(s.id == SOURCE_STREAMING_FILES and s.enabled for s in report.sources):
            before = len(results)
            results.extend(self._extract_streaming_files(root, sub_paths))
            report.record(SOURCE_STREAMING_FILES, len(results) - before, enabled=True)
        else:
            report.record(
                SOURCE_STREAMING_FILES,
                0,
                enabled=False,
                reason="no external text files in StreamingAssets",
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
            if is_custom_dll(f) and (f.lower().startswith("assembly-csharp") or is_dotnet_pe(fpath)):
                rel_path = os.path.relpath(fpath, root).replace("\\", "/")

                try:
                    if fpath in self._dll_extract_cache:
                        extracted = self._dll_extract_cache[fpath]
                    else:
                        orig_bytes = read_backup_original(root, rel_path)
                        if orig_bytes is not None:
                            import tempfile
                            temp_dll_dir = tempfile.mkdtemp(prefix="interprex_dll_orig_")
                            temp_dll_path = os.path.join(temp_dll_dir, f)
                            try:
                                with open(temp_dll_path, "wb") as tdf:
                                    tdf.write(orig_bytes)
                                stdout = self._run_editor(["extract", temp_dll_path])
                                extracted = json.loads(stdout)
                                self._dll_extract_cache[fpath] = extracted
                            finally:
                                shutil.rmtree(temp_dll_dir, ignore_errors=True)
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
            typetree_handled_pids: set[int] = set()
            env = None
            try:
                orig_bytes = self._read_asset_backup_original(root, fpath, rel_path)
                if orig_bytes is not None:
                    try:
                        bio = io.BytesIO(orig_bytes)
                        bio.name = fpath
                        env = UnityPy.load(bio, path=os.path.dirname(fpath))
                    except Exception:
                        env = UnityPy.load(fpath)
                else:
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
                    if obj.type.name == "VisualTreeAsset":
                        try:
                            tree = obj.read_typetree()
                            vta_res = _extract_from_visual_tree_asset(
                                tree, obj.path_id, rel_path, os.path.basename(fpath), self._mk
                            )
                            if vta_res:
                                results.extend(vta_res)
                                typetree_found += len(vta_res)
                                n_typetree += len(vta_res)
                                typetree_handled_pids.add(obj.path_id)
                        except Exception:
                            failed_count += 1
                        continue

                    if obj.type.name == "MonoBehaviour":
                        try:
                            # Skip library classes, handle external script load failures gracefully
                            ns = ""
                            class_name = ""
                            try:
                                data = obj.read()
                                script_data = data.m_Script.read()
                                ns = getattr(script_data, "namespace", "")
                                class_name = getattr(script_data, "class_name", "") or getattr(script_data, "m_ClassName", "")
                            except Exception:
                                pass

                            if should_skip_type(ns):
                                continue

                            if class_name in _TECHNICAL_TMP_CLASSES:
                                typetree_handled_pids.add(obj.path_id)
                                continue

                            tree = obj.read_typetree()

                            # Unity Localization tables are handled by SOURCE_STRING_TABLE
                            if ("m_TableData" in tree and "m_LocaleIdentifier" in tree) or (
                                "m_Entries" in tree and "m_TableCollectionName" in tree
                            ):
                                self._discovered_loc_files.add(os.path.normpath(fpath))
                                typetree_handled_pids.add(obj.path_id)
                                continue

                            if class_name == "VisualTreeAsset" or "m_VisualElementAssets" in tree:
                                vta_res = _extract_from_visual_tree_asset(
                                    tree, obj.path_id, rel_path, os.path.basename(fpath), self._mk
                                )
                                if vta_res:
                                    results.extend(vta_res)
                                    typetree_found += len(vta_res)
                                    n_typetree += len(vta_res)
                                    typetree_handled_pids.add(obj.path_id)
                                    continue

                            obj_hits = 0

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

                            # 1. Direct text fields (Unity UI, TextMeshPro, Fungus Say/Menu, localizedText)
                            for field_name in ("m_Text", "m_text", "storyText", "description", "localizedText"):
                                if field_name in tree and isinstance(tree[field_name], str):
                                    text = tree[field_name]
                                    if text and not text.isspace() and _is_game_text(text):
                                        path = ["Asset", obj.type.name, str(obj.path_id), field_name]
                                        parts = []
                                        if go_name:
                                            parts.append(f"GameObject: {go_name}")
                                        if script_name:
                                            parts.append(f"Script: {script_name}")
                                        if field_name not in ("m_Text", "m_text"):
                                            parts.append(f"Field: {field_name}")
                                        parts.append(f"File: {os.path.basename(fpath)}")
                                        parts.append(f"PathID: {obj.path_id}")

                                        context = ", ".join(parts)
                                        results.append(self._mk(rel_path, path, text, context))
                                        typetree_found += 1
                                        n_typetree += 1
                                        obj_hits += 1

                            # 2. I2 Localization (mTerms / mSource.mTerms)
                            terms_list = None
                            if "mTerms" in tree and isinstance(tree["mTerms"], list):
                                terms_list = tree["mTerms"]
                            elif "mSource" in tree and isinstance(tree["mSource"], dict):
                                ms = tree["mSource"]
                                if "mTerms" in ms and isinstance(ms.get("mTerms"), list):
                                    terms_list = ms["mTerms"]

                            if terms_list:
                                src_idx, _ = _resolve_i2_lang_indices(tree)
                                for term in terms_list:
                                    if isinstance(term, dict):
                                        term_key = term.get("Term")
                                        langs = term.get("Languages")
                                        if term_key and isinstance(langs, list) and langs:
                                            idx = src_idx if src_idx < len(langs) else 0
                                            val = langs[idx]
                                            if isinstance(val, str) and val and not val.isspace() and _is_game_text(val, allow_title_word=True):
                                                path = ["Asset", "I2Term", str(obj.path_id), str(term_key)]
                                                context = f"I2 Localization: {term_key}, File: {os.path.basename(fpath)}, PathID: {obj.path_id}"
                                                results.append(self._mk(rel_path, path, val, context))
                                                typetree_found += 1
                                                n_typetree += 1
                                                obj_hits += 1

                            # 3. PixelCrushers Dialogue System (conversations -> dialogueEntries -> fields)
                            if "conversations" in tree and isinstance(tree["conversations"], list):
                                for conv in tree["conversations"]:
                                    if isinstance(conv, dict):
                                        conv_title = conv.get("Title", "")
                                        entries = conv.get("dialogueEntries")
                                        if isinstance(entries, list):
                                            for entry in entries:
                                                if isinstance(entry, dict):
                                                    entry_id = entry.get("id", 0)
                                                    fields = entry.get("fields")
                                                    if isinstance(fields, list):
                                                        actor_name = ""
                                                        for fld in fields:
                                                            if isinstance(fld, dict) and fld.get("title") == "Actor":
                                                                actor_name = str(fld.get("value", ""))
                                                        for fld in fields:
                                                            if isinstance(fld, dict):
                                                                title = fld.get("title")
                                                                if title in ("Dialogue Text", "Menu Text"):
                                                                    val = fld.get("value")
                                                                    if isinstance(val, str) and val and not val.isspace() and _is_game_text(val, allow_title_word=True):
                                                                        path = ["Asset", "DialogueEntry", str(obj.path_id), str(entry_id), str(title)]
                                                                        ctx_parts = []
                                                                        if conv_title:
                                                                            ctx_parts.append(f"Conversation: {conv_title}")
                                                                        if actor_name:
                                                                            ctx_parts.append(f"Actor: {actor_name}")
                                                                        ctx_parts.append(f"Entry: {entry_id} ({title})")
                                                                        ctx_parts.append(f"File: {os.path.basename(fpath)}")
                                                                        ctx_parts.append(f"PathID: {obj.path_id}")
                                                                        context = ", ".join(ctx_parts)
                                                                        results.append(self._mk(rel_path, path, val, context))
                                                                        typetree_found += 1
                                                                        n_typetree += 1
                                                                        obj_hits += 1

                            # 4. Generic deep typetree walk for custom ScriptableObjects / MonoBehaviours
                            generic_hits = _walk_typetree_generic(tree, [])
                            for p_parts, val in generic_hits:
                                if p_parts and p_parts[0] in ("m_Text", "m_text", "storyText", "description", "localizedText", "mTerms", "mSource", "conversations", "m_VisualElementAssets"):
                                    continue
                                dot_path = ".".join(p_parts)
                                path = ["AssetTree", str(obj.path_id), dot_path]
                                parts = []
                                if go_name:
                                    parts.append(f"GameObject: {go_name}")
                                if script_name:
                                    parts.append(f"Script: {script_name}")
                                parts.append(f"Field: {dot_path}")
                                parts.append(f"File: {os.path.basename(fpath)}")
                                parts.append(f"PathID: {obj.path_id}")
                                context = ", ".join(parts)
                                results.append(self._mk(rel_path, path, val, context))
                                typetree_found += 1
                                n_typetree += 1
                                obj_hits += 1

                            if obj_hits > 0:
                                typetree_handled_pids.add(obj.path_id)
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
                                typetree_handled_pids.add(obj.path_id)
                        except Exception:
                            failed_count += 1
                    elif obj.type.name == "TextAsset":
                        try:
                            script_content = None
                            asset_name = ""
                            try:
                                tree = obj.read_typetree()
                                script_content = tree.get("m_Script")
                                asset_name = tree.get("m_Name", "")
                            except Exception:
                                pass
                            if not script_content:
                                try:
                                    data = obj.read()
                                    script_content = getattr(data, "m_Script", None) or getattr(data, "script", None)
                                    asset_name = getattr(data, "m_Name", None) or getattr(data, "name", "")
                                except Exception:
                                    pass

                            if isinstance(script_content, bytes):
                                try:
                                    script_content = script_content.decode("utf-8")
                                except Exception:
                                    try:
                                        script_content = script_content.decode("latin-1")
                                    except Exception:
                                        script_content = None

                            if script_content and isinstance(script_content, str):
                                if not asset_name:
                                    asset_name = f"TextAsset_{obj.path_id}"

                                textasset_hits = 0
                                handled = False
                                if "inkVersion" in script_content and "root" in script_content:
                                    try:
                                        ink_data = json.loads(script_content)
                                        if isinstance(ink_data, dict) and "inkVersion" in ink_data and "root" in ink_data:
                                            handled = True
                                            ink_hits = _walk_ink_root(ink_data.get("root"), [0])
                                            for idx, raw_ink_text in ink_hits:
                                                ink_text = raw_ink_text[1:]
                                                if _is_game_text(ink_text):
                                                    path = ["TextAsset", str(obj.path_id), "ink", str(idx)]
                                                    context = f"TextAsset (Ink): {asset_name}, Index: {idx}, File: {os.path.basename(fpath)}"
                                                    results.append(self._mk(rel_path, path, ink_text, context))
                                                    typetree_found += 1
                                                    n_typetree += 1
                                                    textasset_hits += 1
                                    except Exception:
                                        pass

                                if not handled and (script_content.strip().startswith("{") or script_content.strip().startswith("[")):
                                    try:
                                        json_obj = json.loads(script_content)
                                        handled = True
                                        leaves = _walk_json_leaves(json_obj, [])
                                        for jpath, val in leaves:
                                            if _is_game_text(val):
                                                dot_path = ".".join(jpath)
                                                path = ["TextAsset", str(obj.path_id), "json", dot_path]
                                                context = f"TextAsset (JSON): {asset_name}, Path: {dot_path}, File: {os.path.basename(fpath)}"
                                                results.append(self._mk(rel_path, path, val, context))
                                                typetree_found += 1
                                                n_typetree += 1
                                                textasset_hits += 1
                                    except Exception:
                                        pass

                                if not handled and ("\n" in script_content or "\r" in script_content):
                                    delim = _detect_csv_delim(script_content)
                                    if delim:
                                        try:
                                            reader = csv.reader(io.StringIO(script_content), delimiter=delim)
                                            rows = list(reader)
                                            if rows:
                                                header = [h.strip().lower() for h in rows[0]]
                                                text_col_indices = _select_csv_text_columns(header)

                                                for r_idx in range(1, len(rows)):
                                                    row = rows[r_idx]
                                                    key_prefix = row[0] if len(row) > 0 else str(r_idx)
                                                    for ci in text_col_indices:
                                                        if ci < len(row):
                                                            val = row[ci]
                                                            if val and _is_game_text(val, allow_title_word=True):
                                                                col_name = header[ci] if ci < len(header) else str(ci)
                                                                path = ["TextAsset", str(obj.path_id), "csv", str(r_idx), str(ci)]
                                                                context = f"TextAsset (CSV): {asset_name}, Row: {r_idx} (key={key_prefix}), Col: {col_name}, File: {os.path.basename(fpath)}"
                                                                results.append(self._mk(rel_path, path, val, context))
                                                                typetree_found += 1
                                                                n_typetree += 1
                                                                textasset_hits += 1
                                        except Exception:
                                            pass

                                if not handled:
                                    stripped = script_content.lstrip()
                                    if stripped.startswith("<?xml") or (stripped.startswith("<") and ">" in stripped):
                                        xml_hits = _extract_xml_strings(script_content)
                                        for xml_path, val in xml_hits:
                                            path = ["TextAsset", str(obj.path_id), "xml", xml_path]
                                            context = f"TextAsset (XML): {asset_name}, Path: {xml_path}, File: {os.path.basename(fpath)}"
                                            results.append(self._mk(rel_path, path, val, context))
                                            typetree_found += 1
                                            n_typetree += 1
                                            textasset_hits += 1

                                if textasset_hits > 0:
                                    typetree_handled_pids.add(obj.path_id)
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
                    env, fpath, root, results, mode="content", skip_pids=typetree_handled_pids
                )
                n_content += len(results) - before_c
                # content_n is added count; keep using len(results) for accuracy
                if failed_count > 0:
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
                        env, fpath, root, results, mode="ui", skip_pids=typetree_handled_pids
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
                orig_bytes = read_backup_original(root, rel_path)
                if orig_bytes is not None:
                    try:
                        content = orig_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        content = orig_bytes.decode("latin-1", errors="ignore")
                else:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                
                lines = content.splitlines()
                line_idx = 0
                is_asset_file = fpath.lower().endswith(".asset")
                while line_idx < len(lines):
                    line = lines[line_idx]
                    m_field = None
                    raw_val = None
                    start_line_idx = line_idx
                    match = re.match(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$', line)
                    if match:
                        fname = match.group(2)
                        if fname in ("m_Text", "m_text", "m_TextUGUI"):
                            m_field = "m_Text"
                            raw_val = match.group(3).strip()
                        elif is_asset_file and _is_typetree_text_field(fname):
                            m_field = fname
                            raw_val = match.group(3).strip()

                    if m_field is not None and raw_val is not None:
                        val, line_idx = _parse_multiline_yaml_val(lines, line_idx, raw_val)
                        if val and not val.isspace() and _is_game_text(val):
                            path = ["AssetYAML", m_field, str(start_line_idx)]
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
        skip_pids: set[int] | None = None,
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
                if skip_pids and obj.path_id in skip_pids:
                    continue
                try:
                    raw = obj.get_raw_data()
                except Exception:
                    continue
                if len(raw) < 20:
                    continue
                if b"m_TableCollectionName" in raw or b"m_LocaleIdentifier" in raw:
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

                for idx_c, (offset, s) in enumerate(candidates):
                    t = s.strip()
                    path = ["AssetRaw", obj.type.name, str(obj.path_id), str(offset)]
                    key = (rel_path, tuple(path), t)
                    if key in existing:
                        continue
                    existing.add(key)

                    ctx_parts = [f"File: {base}", f"PathID: {obj.path_id}"]
                    if is_content:
                        ctx_parts.append("Kind: content")
                    if possible_names and len(possible_names) <= 3 and (is_content or (" " in t and len(t) > 6)):
                        ctx_parts.append(f"Speakers in this block: {', '.join(possible_names)}")

                    if is_content or (" " in t and len(t) > 6):
                        prev_lines = [texts[j] for j in range(max(0, idx_c - 2), idx_c) if texts[j] and texts[j] != t]
                        next_line = texts[idx_c + 1] if (idx_c + 1 < len(texts) and texts[idx_c + 1] != t) else ""
                        neighbor_parts = []
                        if prev_lines:
                            prev_str = " -> ".join(prev_lines)
                            if len(prev_str) > 160:
                                prev_str = prev_str[:157] + "..."
                            neighbor_parts.append(f"Prev: {prev_str}")
                        if next_line:
                            next_str = next_line
                            if len(next_str) > 160:
                                next_str = next_str[:157] + "..."
                            neighbor_parts.append(f"Next: {next_str}")

                        if neighbor_parts:
                            ctx_parts.append(" | ".join(neighbor_parts))
                        else:
                            siblings = [x for x in texts if x != t]
                            if siblings:
                                capped: list[str] = []
                                total = 0
                                for sib in siblings:
                                    if len(capped) >= 2:
                                        break
                                    addition = len(sib) + 2
                                    if total + addition > 120:
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

    def _extract_raw_fallback(self, fpath: str, root: str, skip_pids: set[int] | None = None) -> list[TranslationString]:
        """Legacy entry: full raw scan (content + ui). Prefer _extract_raw_from_env."""
        results: list[TranslationString] = []
        try:
            import UnityPy
            env = UnityPy.load(fpath)
            self._extract_raw_from_env(env, fpath, root, results, mode="content", skip_pids=skip_pids)
            self._extract_raw_from_env(env, fpath, root, results, mode="ui", skip_pids=skip_pids)
        except Exception as e:
            print(f"Error in raw fallback for {fpath}: {e}", file=sys.stderr)
        return results

    def _inject_raw_fallback(self, fpath: str, root: str, translations: dict[str, str],
                             env: Any = None, font_bytes: bytes | None = None,
                             target_lang: str | None = None,
                             already_changed: bool = False,
                             skip_pids: set[int] | None = None,
                             skip_font_pptr: bool = False) -> int:
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
                if skip_pids and obj.path_id in skip_pids:
                    continue
                try:
                    raw = bytes(obj.get_raw_data())
                except Exception:
                    continue
                if len(raw) < 20:
                    continue
                if b"SerializationData" in raw or b"SerializedBytes" in raw:
                    continue
                if b"m_TableCollectionName" in raw or b"m_LocaleIdentifier" in raw:
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

                if len(out) % 4 != len(raw) % 4:
                    pad_diff = (len(raw) % 4 - len(out) % 4) % 4
                    out.extend(b"\x00" * pad_diff)

                if len(out) % 4 != len(raw) % 4:
                    print(
                        f"Skipping raw injection path_id={obj.path_id} in "
                        f"{os.path.basename(fpath)}: 4-byte alignment mismatch",
                        file=sys.stderr,
                    )
                    continue

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

            if objects_changed == 0 and not already_changed:
                # No string edits — still try a conservative font-gap patch if
                # the target script needs glyphs the game's fonts don't have.
                if not skip_font_pptr and _target_script(target_lang):
                    self._replace_font_pptrs(env, fpath, target_lang)
                return written

            # UnityPy Environment.save writes into out_path dir; use file.save bytes.
            try:
                data = None
                f_lower = os.path.basename(fpath).lower()
                if f_lower.endswith((".bundle", ".assetbundle")) or f_lower == "data.unity3d":
                    try:
                        data = env.file.save(packer="original")
                    except Exception:
                        pass
                if not data:
                    data = env.file.save()
                if isinstance(data, (bytes, bytearray)) and len(data) > 0:
                    self._save_or_split_asset(root, fpath, bytes(data))
                else:
                    # Multi-cab / container: fall back to env.save into temp dir
                    import shutil
                    tmp = tempfile.mkdtemp(prefix="interprex_unity_")
                    try:
                        env.save(pack="none", out_path=tmp)
                        # Find the rewritten asset by basename
                        base = os.path.basename(fpath)
                        candidate = os.path.join(tmp, base)
                        if not os.path.isfile(candidate) and base.lower().endswith(".split0"):
                            candidate = os.path.join(tmp, base[:-7])
                        if not os.path.isfile(candidate):
                            for dirpath, _, filenames in os.walk(tmp):
                                if base in filenames:
                                    candidate = os.path.join(dirpath, base)
                                    break
                                if base.lower().endswith(".split0") and base[:-7] in filenames:
                                    candidate = os.path.join(dirpath, base[:-7])
                                    break
                        if os.path.isfile(candidate):
                            with open(candidate, "rb") as cf:
                                cdata = cf.read()
                            self._save_or_split_asset(root, fpath, cdata)
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
            if not skip_font_pptr and _target_script(target_lang):
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

        aa_bundles = find_aa_bundles(root)
        extra_loc = [p for p in sorted(getattr(self, "_discovered_loc_files", set())) if p not in aa_bundles and os.path.isfile(p)]
        loc_files = aa_bundles + extra_loc
        if not loc_files:
            return []

        # Phase 1: Collect global SharedTableData across ALL bundles, and gather available locales
        global_shared_tables: dict[int, dict] = {}
        all_shared_list: list[dict] = []
        available_locales: set[str] = set()

        for bundle_path in loc_files:
            try:
                rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
                orig_bytes = self._read_asset_backup_original(root, bundle_path, rel_path)
                if orig_bytes is not None:
                    try:
                        bio = io.BytesIO(orig_bytes)
                        bio.name = bundle_path
                        env = UnityPy.load(bio, path=os.path.dirname(bundle_path))
                    except Exception:
                        env = UnityPy.load(bundle_path)
                else:
                    env = UnityPy.load(bundle_path)
                self._attach_generator(root, env)
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_Entries" in tree and "m_TableCollectionName" in tree:
                            col_name = tree.get("m_TableCollectionName") or ""
                            entries = tree.get("m_Entries", [])
                            tbl_info = {
                                "name": col_name,
                                "id_to_key": {e["m_Id"]: e["m_Key"] for e in entries if "m_Id" in e and "m_Key" in e},
                            }
                            global_shared_tables[obj.path_id] = tbl_info
                            all_shared_list.append(tbl_info)
                        elif "m_TableData" in tree and "m_LocaleIdentifier" in tree:
                            loc_code = tree.get("m_LocaleIdentifier", {}).get("m_Code", "")
                            if loc_code:
                                available_locales.add(loc_code.lower())
                    except Exception:
                        pass
            except Exception:
                pass

        has_english = any(loc.startswith("en") for loc in available_locales)

        # Phase 2: Extract StringTables
        results = []
        for bundle_path in loc_files:
            rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
            try:
                orig_bytes = self._read_asset_backup_original(root, bundle_path, rel_path)
                if orig_bytes is not None:
                    try:
                        bio = io.BytesIO(orig_bytes)
                        bio.name = bundle_path
                        env = UnityPy.load(bio, path=os.path.dirname(bundle_path))
                    except Exception:
                        env = UnityPy.load(bundle_path)
                else:
                    env = UnityPy.load(bundle_path)
                self._attach_generator(root, env)
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_TableData" not in tree or "m_LocaleIdentifier" not in tree:
                            continue

                        raw_locale = tree["m_LocaleIdentifier"].get("m_Code", "")
                        locale = raw_locale.lower()

                        # Locale filter: If English exists, only extract English (and blank) tables
                        if has_english and locale and not locale.startswith("en"):
                            continue

                        shared = _resolve_shared_table(tree, obj, data, global_shared_tables, all_shared_list)
                        id_to_key = shared.get("id_to_key", {})
                        collection_name = shared.get("name") or getattr(obj, "name", None) or getattr(data, "name", None) or "Unknown"

                        for entry in tree["m_TableData"]:
                            value = entry.get("m_Localized", "")
                            if not value or value.isspace():
                                continue
                            if not _is_game_text(value, allow_title_word=True):
                                continue
                            entry_id = entry["m_Id"]
                            key_name = id_to_key.get(entry_id, str(entry_id))
                            path = ["StringTable", collection_name, raw_locale, key_name]
                            context = f"Collection: {collection_name}, Locale: {raw_locale}, Key: {key_name}"
                            results.append(self._mk(rel_path, path, value, context))
                    except Exception:
                        pass

            except Exception as e:
                print(f"Error parsing bundle {bundle_path}: {e}", file=sys.stderr)

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        """Run enabled Unity inject sources; stash ``last_report`` with per-source counts."""
        self._modified_files_in_run = set()
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

        if any(s.id == SOURCE_IL2CPP_METADATA and s.enabled for s in report.sources):
            n = self._inject_il2cpp_metadata(root, translations, target_lang, sub_paths)
            written += n
            report.record(SOURCE_IL2CPP_METADATA, n, enabled=True)
        else:
            report.record(SOURCE_IL2CPP_METADATA, 0, enabled=False, reason="not an IL2CPP build")

        # Assets: typetree + raw (content/ui slots) + yaml + font_gap inside
        n_assets = self._inject_assets(
            root, translations, target_lang, sub_paths, report=report
        )
        written += n_assets

        loc_enabled = any(s.id == SOURCE_STRING_TABLE and s.enabled for s in report.sources) or bool(getattr(self, "_discovered_loc_files", None))
        if loc_enabled:
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

        # ── streaming_files (External files under StreamingAssets) ─────────
        if any(s.id == SOURCE_STREAMING_FILES and s.enabled for s in report.sources):
            n = self._inject_streaming_files(root, translations, target_lang, sub_paths)
            written += n
            report.record(SOURCE_STREAMING_FILES, n, enabled=True)
        else:
            report.record(
                SOURCE_STREAMING_FILES,
                0,
                enabled=False,
                reason="no external text files in StreamingAssets",
            )

        self.finalize_backups(root)
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
            if is_custom_dll(f) and (f.lower().startswith("assembly-csharp") or is_dotnet_pe(fpath)):
                rel_path = os.path.relpath(fpath, root).replace("\\", "/")

                try:
                    orig_bytes = read_backup_original(root, rel_path)
                    if fpath in self._dll_extract_cache:
                        extracted = self._dll_extract_cache[fpath]
                    elif orig_bytes is not None:
                        temp_dll_dir = tempfile.mkdtemp(prefix="interprex_dll_orig_")
                        temp_dll_path = os.path.join(temp_dll_dir, f)
                        try:
                            with open(temp_dll_path, "wb") as tdf:
                                tdf.write(orig_bytes)
                            stdout = self._run_editor(["extract", temp_dll_path])
                            extracted = json.loads(stdout)
                            self._dll_extract_cache[fpath] = extracted
                        finally:
                            shutil.rmtree(temp_dll_dir, ignore_errors=True)
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
                            if orig_bytes is not None and not self._is_modified_in_run(fpath):
                                with open(fpath, "wb") as f_out:
                                    f_out.write(orig_bytes)
                            stdout = self._run_editor(["inject", fpath, temp_json_path])
                            output = stdout.strip()
                            if output.startswith("SUCCESS:"):
                                replaced = int(output.split(":")[1])
                                written += replaced
                                self._mark_modified_in_run(fpath)
                        finally:
                            if os.path.exists(temp_json_path):
                                os.remove(temp_json_path)

                except Exception as ex:
                    print(f"Error injecting into DLL {f}: {ex}", file=sys.stderr)
                    continue

        self.finalize_backups(root)
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
                if self._is_modified_in_run(fpath):
                    env = UnityPy.load(fpath)
                else:
                    orig_bytes = self._read_asset_backup_original(root, fpath, rel_path)
                    if orig_bytes is not None:
                        try:
                            bio = io.BytesIO(orig_bytes)
                            bio.name = fpath
                            env = UnityPy.load(bio, path=os.path.dirname(fpath))
                        except Exception:
                            env = UnityPy.load(fpath)
                    else:
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
                typetree_modified_pids: set[int] = set()
                tmp_settings_objs: list[Any] = []
                tmp_font_objs: list[Any] = []

                for obj in env.objects:
                    class_name = ""
                    data = None
                    if obj.type.name in ("MonoBehaviour", "Text", "VisualTreeAsset", "TMP_FontAsset"):
                        try:
                            if obj.type.name == "VisualTreeAsset":
                                data = obj.read()
                                tree = obj.read_typetree()
                                vta_hits = _inject_into_visual_tree_asset(
                                    tree, obj.path_id, rel_path, translations, self.engine
                                )
                                if vta_hits > 0:
                                    _save_typetree_safe(obj, data, tree)
                                    changed = True
                                    written += vta_hits
                                    typetree_found += vta_hits
                                    n_typetree += vta_hits
                                    typetree_modified_pids.add(obj.path_id)
                                continue

                            if obj.type.name == "TMP_FontAsset":
                                tmp_font_objs.append(obj)

                            if obj.type.name == "MonoBehaviour":
                                ns = ""
                                class_name = ""
                                try:
                                    data = obj.read()
                                    script_data = data.m_Script.read()
                                    ns = getattr(script_data, "namespace", "")
                                    class_name = getattr(script_data, "class_name", "") or getattr(script_data, "m_ClassName", "")
                                except Exception:
                                    pass

                                if should_skip_type(ns):
                                    continue

                                if class_name == "TMP_FontAsset":
                                    tmp_font_objs.append(obj)
                                    if font_bytes:
                                        try:
                                            tree = None
                                            if hasattr(obj, "read_typetree"):
                                                try:
                                                    tree = obj.read_typetree()
                                                except Exception:
                                                    pass
                                            if tree is None and data is not None and hasattr(data, "read_typetree"):
                                                tree = data.read_typetree()
                                            if tree:
                                                pop_mode = tree.get("m_AtlasPopulationMode", 0)
                                                src_font = tree.get("m_SourceFontFile")
                                                font_pid = src_font.get("m_PathID") if isinstance(src_font, dict) else None
                                                if font_pid:
                                                    fonts_to_replace.add(font_pid)
                                                elif pop_mode == 0:
                                                    print(
                                                        f"[font] Static TMP_FontAsset '{getattr(data, 'name', obj.path_id)}' "
                                                        f"in {os.path.basename(fpath)}: left intact (relying on PPtr donor remap).",
                                                        file=sys.stderr,
                                                    )
                                        except Exception:
                                            pass
                                    continue

                                if class_name == "TMP_Settings" or getattr(data, "name", "") in ("TMP Settings", "TMP_Settings") or getattr(getattr(obj, "serialized_type", None), "class_name", None) == "TMP_Settings":
                                    tmp_settings_objs.append(obj)
                                    continue

                                if class_name in _TECHNICAL_TMP_CLASSES:
                                    continue

                                tree = obj.read_typetree()

                                # Unity Localization tables are handled by SOURCE_STRING_TABLE
                                if ("m_TableData" in tree and "m_LocaleIdentifier" in tree) or (
                                    "m_Entries" in tree and "m_TableCollectionName" in tree
                                ):
                                    self._discovered_loc_files.add(os.path.normpath(fpath))
                                    typetree_modified_pids.add(obj.path_id)
                                    continue

                                if class_name == "VisualTreeAsset" or "m_VisualElementAssets" in tree:
                                    vta_hits = _inject_into_visual_tree_asset(
                                        tree, obj.path_id, rel_path, translations, self.engine
                                    )
                                    if vta_hits > 0:
                                        _save_typetree_safe(obj, data, tree)
                                        changed = True
                                        written += vta_hits
                                        typetree_found += vta_hits
                                        n_typetree += vta_hits
                                        typetree_modified_pids.add(obj.path_id)
                                    continue
                            else:
                                data = obj.read()
                                tree = data.read_typetree()

                            obj_modified = False

                            # 1. Direct text fields (Unity UI, TextMeshPro, Fungus Say/Menu, localizedText)
                            for field_name in ("m_Text", "m_text", "storyText", "description", "localizedText"):
                                if field_name in tree and isinstance(tree[field_name], str):
                                    original = tree[field_name]
                                    if original and not original.isspace() and _is_game_text(original):
                                        path = ["Asset", obj.type.name, str(obj.path_id), field_name]
                                        sid = make_id(self.engine, rel_path, path, original)
                                        if sid in translations:
                                            tree[field_name] = translations[sid]
                                            obj_modified = True
                                            written += 1
                                            typetree_found += 1
                                            n_typetree += 1

                            # 2. I2 Localization (mTerms / mSource.mTerms)
                            terms_list = None
                            if "mTerms" in tree and isinstance(tree["mTerms"], list):
                                terms_list = tree["mTerms"]
                            elif "mSource" in tree and isinstance(tree["mSource"], dict):
                                ms = tree["mSource"]
                                if "mTerms" in ms and isinstance(ms.get("mTerms"), list):
                                    terms_list = ms["mTerms"]

                            if terms_list:
                                src_idx, tgt_indices = _resolve_i2_lang_indices(tree, target_lang)
                                for term in terms_list:
                                    if isinstance(term, dict):
                                        term_key = term.get("Term")
                                        langs = term.get("Languages")
                                        if term_key and isinstance(langs, list) and langs:
                                            idx = src_idx if src_idx < len(langs) else 0
                                            orig_val = langs[idx]
                                            if isinstance(orig_val, str) and orig_val:
                                                path = ["Asset", "I2Term", str(obj.path_id), str(term_key)]
                                                sid = make_id(self.engine, rel_path, path, orig_val)
                                                if sid in translations:
                                                    translated = translations[sid]
                                                    langs[idx] = translated
                                                    for tgt_idx in tgt_indices:
                                                        if tgt_idx < len(langs) and tgt_idx != idx:
                                                            langs[tgt_idx] = translated
                                                    obj_modified = True
                                                    written += 1
                                                    typetree_found += 1
                                                    n_typetree += 1

                            # 3. PixelCrushers Dialogue System (conversations -> dialogueEntries -> fields)
                            if "conversations" in tree and isinstance(tree["conversations"], list):
                                for conv in tree["conversations"]:
                                    if isinstance(conv, dict):
                                        entries = conv.get("dialogueEntries")
                                        if isinstance(entries, list):
                                            for entry in entries:
                                                if isinstance(entry, dict):
                                                    entry_id = entry.get("id", 0)
                                                    fields = entry.get("fields")
                                                    if isinstance(fields, list):
                                                        for fld in fields:
                                                            if isinstance(fld, dict):
                                                                title = fld.get("title")
                                                                if title in ("Dialogue Text", "Menu Text"):
                                                                    orig_val = fld.get("value")
                                                                    if isinstance(orig_val, str) and orig_val:
                                                                        path = ["Asset", "DialogueEntry", str(obj.path_id), str(entry_id), str(title)]
                                                                        sid = make_id(self.engine, rel_path, path, orig_val)
                                                                        if sid in translations:
                                                                            fld["value"] = translations[sid]
                                                                            obj_modified = True
                                                                            written += 1
                                                                            typetree_found += 1
                                                                            n_typetree += 1

                            # 4. Generic deep typetree injection
                            generic_hits = _walk_typetree_generic(tree, [])
                            for p_parts, orig_val in generic_hits:
                                if p_parts and p_parts[0] in ("m_Text", "m_text", "storyText", "description", "localizedText", "mTerms", "mSource", "conversations", "m_VisualElementAssets"):
                                    continue
                                dot_path = ".".join(p_parts)
                                path = ["AssetTree", str(obj.path_id), dot_path]
                                sid = make_id(self.engine, rel_path, path, orig_val)
                                if sid in translations:
                                    if _set_typetree_by_path(tree, p_parts, translations[sid]):
                                        obj_modified = True
                                        written += 1
                                        typetree_found += 1
                                        n_typetree += 1

                            if obj_modified:
                                _save_typetree_safe(obj, data, tree)
                                changed = True
                                typetree_modified_pids.add(obj.path_id)
                        except Exception:
                            failed_count += 1
                    elif obj.type.name == "TextAsset":
                        try:
                            script_content = None
                            tree = None
                            try:
                                tree = obj.read_typetree()
                                script_content = tree.get("m_Script")
                            except Exception:
                                pass
                            if not script_content:
                                try:
                                    data = obj.read()
                                    script_content = getattr(data, "m_Script", None) or getattr(data, "script", None)
                                except Exception:
                                    pass

                            if isinstance(script_content, bytes):
                                try:
                                    script_content = script_content.decode("utf-8")
                                except Exception:
                                    try:
                                        script_content = script_content.decode("latin-1")
                                    except Exception:
                                        script_content = None

                            if script_content and isinstance(script_content, str):
                                asset_modified = False
                                handled = False

                                # 1. Ink JSON
                                if "inkVersion" in script_content and "root" in script_content:
                                    try:
                                        ink_data = json.loads(script_content)
                                        if isinstance(ink_data, dict) and "inkVersion" in ink_data and "root" in ink_data:
                                            handled = True
                                            ink_hits = _walk_ink_root(ink_data.get("root"), [0])
                                            ink_replacements: dict[int, str] = {}
                                            for idx, raw_ink_text in ink_hits:
                                                ink_text = raw_ink_text[1:]
                                                path = ["TextAsset", str(obj.path_id), "ink", str(idx)]
                                                sid = make_id(self.engine, rel_path, path, ink_text)
                                                if sid in translations:
                                                    ink_replacements[idx] = translations[sid]
                                            if ink_replacements:
                                                if _replace_ink_root(ink_data.get("root"), [0], ink_replacements):
                                                    script_content = json.dumps(ink_data, ensure_ascii=False)
                                                    asset_modified = True
                                                    written += len(ink_replacements)
                                                    typetree_found += len(ink_replacements)
                                                    n_typetree += len(ink_replacements)
                                    except Exception:
                                        pass

                                # 2. Generic JSON
                                if not handled and (script_content.strip().startswith("{") or script_content.strip().startswith("[")):
                                    try:
                                        json_obj = json.loads(script_content)
                                        leaves = _walk_json_leaves(json_obj, [])
                                        j_written = 0
                                        for jpath, orig_val in leaves:
                                            dot_path = ".".join(jpath)
                                            path = ["TextAsset", str(obj.path_id), "json", dot_path]
                                            sid = make_id(self.engine, rel_path, path, orig_val)
                                            if sid in translations:
                                                if _set_typetree_by_path(json_obj, jpath, translations[sid]):
                                                    j_written += 1
                                        if j_written > 0:
                                            script_content = json.dumps(json_obj, ensure_ascii=False, indent=2)
                                            asset_modified = True
                                            written += j_written
                                            typetree_found += j_written
                                            n_typetree += j_written
                                        handled = True
                                    except Exception:
                                        pass

                                # 3. CSV / TSV table
                                if not handled and ("\n" in script_content or "\r" in script_content):
                                    delim = _detect_csv_delim(script_content)
                                    if delim:
                                        try:
                                            reader = csv.reader(io.StringIO(script_content), delimiter=delim)
                                            rows = list(reader)
                                            csv_written = 0
                                            if rows:
                                                header = [h.strip().lower() for h in rows[0]]
                                                text_col_indices = _select_csv_text_columns(header)

                                                for r_idx in range(1, len(rows)):
                                                    row = rows[r_idx]
                                                    for ci in text_col_indices:
                                                        if ci < len(row):
                                                            orig_val = row[ci]
                                                            path = ["TextAsset", str(obj.path_id), "csv", str(r_idx), str(ci)]
                                                            sid = make_id(self.engine, rel_path, path, orig_val)
                                                            if sid in translations:
                                                                row[ci] = translations[sid]
                                                                csv_written += 1

                                            if csv_written > 0:
                                                out_io = io.StringIO()
                                                writer = csv.writer(out_io, delimiter=delim, lineterminator="\n")
                                                writer.writerows(rows)
                                                script_content = out_io.getvalue()
                                                asset_modified = True
                                                written += csv_written
                                                typetree_found += csv_written
                                                n_typetree += csv_written
                                            handled = True
                                        except Exception:
                                            pass

                                # 4. XML
                                if not handled and not asset_modified:
                                    stripped = script_content.lstrip()
                                    if stripped.startswith("<?xml") or (stripped.startswith("<") and ">" in stripped):
                                        xml_hits = _extract_xml_strings(script_content)
                                        xml_replacements: dict[str, str] = {}
                                        for xml_path, orig_val in xml_hits:
                                            path = ["TextAsset", str(obj.path_id), "xml", xml_path]
                                            sid = make_id(self.engine, rel_path, path, orig_val)
                                            if sid in translations:
                                                xml_replacements[xml_path] = translations[sid]
                                        if xml_replacements:
                                            try:
                                                script_content = _inject_xml_strings(script_content, xml_replacements)
                                                asset_modified = True
                                                written += len(xml_replacements)
                                                typetree_found += len(xml_replacements)
                                                n_typetree += len(xml_replacements)
                                            except Exception:
                                                pass

                                if asset_modified:
                                    saved = False
                                    if tree is not None:
                                        try:
                                            tree["m_Script"] = script_content
                                            _save_typetree_safe(obj, data, tree)
                                            saved = True
                                        except Exception:
                                            pass
                                    if not saved:
                                        try:
                                            data = obj.read()
                                            if hasattr(data, "m_Script"):
                                                data.m_Script = script_content
                                            elif hasattr(data, "script"):
                                                data.script = script_content
                                            data.save()
                                            saved = True
                                        except Exception:
                                            pass
                                    if not saved:
                                        try:
                                            orig_raw = bytes(obj.get_raw_data())
                                            if len(orig_raw) >= 4:
                                                name_len = struct.unpack_from("<I", orig_raw, 0)[0]
                                                name_end = _aligned_string_end(0, name_len)
                                                if name_end <= len(orig_raw):
                                                    new_raw = orig_raw[:name_end] + _pack_aligned_string(script_content)
                                                    obj.set_raw_data(new_raw)
                                                    saved = True
                                        except Exception:
                                            pass
                                    changed = True
                                    typetree_modified_pids.add(obj.path_id)
                        except Exception:
                            failed_count += 1

                    if (obj.type.name == "TMP_FontAsset" or class_name == "TMP_FontAsset") and font_bytes:
                        try:
                            data = obj.read()
                            tree = data.read_typetree()
                            pop_mode = tree.get("m_AtlasPopulationMode", 0)
                            
                            src_font = tree.get("m_SourceFontFile")
                            font_pid = src_font.get("m_PathID") if isinstance(src_font, dict) else None

                            if pop_mode == 1:
                                if font_pid:
                                    fonts_to_replace.add(font_pid)
                            elif pop_mode == 0:
                                # For static fonts, do NOT blind-convert m_AtlasPopulationMode to 1,
                                # because uncleaned glyph/character tables and non-readable textures
                                # corrupt runtime rendering. If the static font points to a TTF font,
                                # queue the TTF font for NotoSans replacement without breaking the TMP asset.
                                if font_pid:
                                    fonts_to_replace.add(font_pid)
                                else:
                                    print(
                                        f"[font] Static TMP_FontAsset '{getattr(data, 'name', obj.path_id)}' "
                                        f"in {os.path.basename(fpath)}: left intact (relying on PPtr donor remap).",
                                        file=sys.stderr,
                                    )
                        except Exception:
                            failed_count += 1

                # ── Advanced TMP fallback / donor / drought handling ──────────
                converted_count = 0
                script = _target_script(target_lang)
                tmp_donor = None
                tmp_victims = set()
                tmp_cov = {}
                if script:
                    try:
                        tmp_donor, tmp_victims, tmp_cov, _ = self._find_tmp_donors_and_victims(env, script)
                    except Exception:
                        pass

                # Dynamic TTF font replacement for Font (ClassID 128) - both TMP and UnityEngine.UI.Text
                if font_bytes and (script is not None or target_lang is None):
                    for obj in env.objects:
                        if obj.type.name == "Font":
                            should_replace = obj.path_id in fonts_to_replace
                            data = None
                            try:
                                tree = obj.read_typetree()
                                blob = bytes(tree.get("m_FontData") or b"")
                                if not should_replace and script and len(blob) > 100:
                                    if _ttf_covers_script(blob, script) is False:
                                        should_replace = True
                                if not should_replace:
                                    continue
                                if script and _ttf_covers_script(blob, script) is True:
                                    continue
                                existing_fdata = tree.get("m_FontData")
                                if isinstance(existing_fdata, list):
                                    tree["m_FontData"] = list(font_bytes)
                                else:
                                    tree["m_FontData"] = font_bytes
                                _save_typetree_safe(obj, data, tree)
                                changed = True
                                typetree_modified_pids.add(obj.path_id)
                            except Exception:
                                try:
                                    data = obj.read()
                                    blob = bytes(getattr(data, "m_FontData", b"") or b"")
                                    if not should_replace and script and len(blob) > 100:
                                        if _ttf_covers_script(blob, script) is False:
                                            should_replace = True
                                    if not should_replace:
                                        continue
                                    if script and _ttf_covers_script(blob, script) is True:
                                        continue
                                    if hasattr(data, "m_FontData"):
                                        existing_fdata = getattr(data, "m_FontData")
                                        if isinstance(existing_fdata, list):
                                            data.m_FontData = list(font_bytes)
                                        else:
                                            data.m_FontData = font_bytes
                                        data.save()
                                        changed = True
                                        typetree_modified_pids.add(obj.path_id)
                                except Exception as e_font:
                                    print(f"Failed to replace font in Font path_id={obj.path_id}: {e_font}", file=sys.stderr)

                if script:
                    try:
                        # Fallback for tmp_font_objs / tmp_settings_objs if empty
                        if not tmp_font_objs and tmp_cov:
                            tmp_font_objs = [o for o in env.objects if o.path_id in tmp_cov]
                        if not tmp_settings_objs:
                            for o in env.objects:
                                if o.type.name == "MonoBehaviour":
                                    cn = getattr(getattr(o, "serialized_type", None), "class_name", None)
                                    if cn == "TMP_Settings" or getattr(o, "name", "") in ("TMP Settings", "TMP_Settings"):
                                        tmp_settings_objs.append(o)

                        # Case A: An in-file TMP donor covers the script
                        if tmp_donor is not None:
                            # 1. Inject tmp_donor into global TMP_Settings.m_fallbackFontAssets
                            for obj in tmp_settings_objs:
                                data = None
                                tree = None
                                try:
                                    data = obj.read()
                                    if hasattr(data, "read_typetree"):
                                        tree = data.read_typetree()
                                except Exception:
                                    pass
                                if tree is None and hasattr(obj, "read_typetree"):
                                    try:
                                        tree = obj.read_typetree()
                                    except Exception:
                                        pass
                                if isinstance(tree, dict) and "m_fallbackFontAssets" in tree:
                                    fallbacks = tree.get("m_fallbackFontAssets")
                                    if isinstance(fallbacks, list):
                                        has_donor = any(
                                            isinstance(x, dict) and x.get("m_PathID") == tmp_donor
                                            for x in fallbacks
                                        )
                                        if not has_donor:
                                            fallbacks.insert(0, {"m_FileID": 0, "m_PathID": tmp_donor})
                                            _save_typetree_safe(obj, data, tree)
                                            changed = True
                                            typetree_modified_pids.add(obj.path_id)
                                            print(
                                                f"[font] Injected tmp_donor ({tmp_donor}) into "
                                                f"TMP_Settings.m_fallbackFontAssets in {os.path.basename(fpath)}",
                                                file=sys.stderr,
                                            )

                            # 2. Inject tmp_donor into m_FallbackFontAssetTable of other TMP_FontAssets
                            for obj in tmp_font_objs:
                                if obj.path_id != tmp_donor:
                                    if tmp_cov.get(obj.path_id, (None, None))[1] is not True:
                                        data = None
                                        tree = None
                                        try:
                                            data = obj.read()
                                            if hasattr(data, "read_typetree"):
                                                tree = data.read_typetree()
                                        except Exception:
                                            pass
                                        if tree is None and hasattr(obj, "read_typetree"):
                                            try:
                                                tree = obj.read_typetree()
                                            except Exception:
                                                pass
                                        if isinstance(tree, dict) and "m_FallbackFontAssetTable" in tree:
                                            fallbacks = tree.get("m_FallbackFontAssetTable")
                                            if isinstance(fallbacks, list):
                                                has_donor = any(
                                                    isinstance(x, dict) and x.get("m_PathID") == tmp_donor
                                                    for x in fallbacks
                                                )
                                                if not has_donor:
                                                    fallbacks.insert(0, {"m_FileID": 0, "m_PathID": tmp_donor})
                                                    _save_typetree_safe(obj, data, tree)
                                                    changed = True
                                                    typetree_modified_pids.add(obj.path_id)

                        # Case B: Total Font Drought (TMP victims exist, no donor, font_bytes available)
                        elif tmp_donor is None and tmp_victims and font_bytes:
                            target_font_obj = None
                            # 1. Prefer a Font already referenced by one of the victims
                            for vid in tmp_victims:
                                v_obj = next((o for o in tmp_font_objs if o.path_id == vid), None)
                                if not v_obj:
                                    v_obj = next((o for o in env.objects if o.path_id == vid), None)
                                if v_obj:
                                    try:
                                        v_tree = None
                                        if hasattr(v_obj, "read_typetree"):
                                            v_tree = v_obj.read_typetree()
                                        elif hasattr(v_obj, "read"):
                                            v_data = v_obj.read()
                                            if hasattr(v_data, "read_typetree"):
                                                v_tree = v_data.read_typetree()
                                        src = v_tree.get("m_SourceFontFile") if isinstance(v_tree, dict) else None
                                        if isinstance(src, dict) and src.get("m_FileID") == 0 and src.get("m_PathID"):
                                            f_obj = next(
                                                (o for o in env.objects if o.path_id == src["m_PathID"] and o.type.name == "Font"),
                                                None,
                                            )
                                            if f_obj:
                                                target_font_obj = f_obj
                                                break
                                    except Exception:
                                        pass

                            # 2. Or any Font in the file
                            if target_font_obj is None:
                                for obj in env.objects:
                                    if obj.type.name == "Font":
                                        target_font_obj = obj
                                        break

                            if target_font_obj is not None:
                                ttf_pid = target_font_obj.path_id
                                font_saved = False
                                try:
                                    ftree = None
                                    if hasattr(target_font_obj, "read_typetree"):
                                        ftree = target_font_obj.read_typetree()
                                    elif hasattr(target_font_obj, "read"):
                                        fdata = target_font_obj.read()
                                        if hasattr(fdata, "read_typetree"):
                                            ftree = fdata.read_typetree()
                                    if isinstance(ftree, dict):
                                        existing_fdata = ftree.get("m_FontData")
                                        if isinstance(existing_fdata, list):
                                            ftree["m_FontData"] = list(font_bytes)
                                        else:
                                            ftree["m_FontData"] = font_bytes
                                        _save_typetree_safe(target_font_obj, None, ftree)
                                        font_saved = True
                                        changed = True
                                        typetree_modified_pids.add(ttf_pid)
                                except Exception:
                                    pass

                                if not font_saved:
                                    try:
                                        fdata = target_font_obj.read()
                                        if hasattr(fdata, "m_FontData"):
                                            existing_fdata = getattr(fdata, "m_FontData")
                                            if isinstance(existing_fdata, list):
                                                fdata.m_FontData = list(font_bytes)
                                            else:
                                                fdata.m_FontData = font_bytes
                                            fdata.save()
                                            font_saved = True
                                            changed = True
                                            typetree_modified_pids.add(ttf_pid)
                                    except Exception as e_f:
                                        print(f"Failed to inject font_bytes into Font path_id={ttf_pid}: {e_f}", file=sys.stderr)

                                if font_saved:
                                    converted_vids: list[int] = []
                                    for vid in tmp_victims:
                                        v_obj = next((o for o in tmp_font_objs if o.path_id == vid), None)
                                        if not v_obj:
                                            v_obj = next((o for o in env.objects if o.path_id == vid), None)
                                        if not v_obj:
                                            continue
                                        try:
                                            data = v_obj.read() if hasattr(v_obj, "read") else None
                                            tree = None
                                            if data and hasattr(data, "read_typetree"):
                                                tree = data.read_typetree()
                                            elif hasattr(v_obj, "read_typetree"):
                                                tree = v_obj.read_typetree()
                                            if isinstance(tree, dict):
                                                tree["m_SourceFontFile"] = {"m_FileID": 0, "m_PathID": ttf_pid}
                                                tree["m_AtlasPopulationMode"] = 1
                                                if "m_IsMultiAtlasTexturesEnabled" in tree:
                                                    tree["m_IsMultiAtlasTexturesEnabled"] = 1
                                                _save_typetree_safe(v_obj, data, tree)
                                                changed = True
                                                typetree_modified_pids.add(v_obj.path_id)
                                                converted_count += 1
                                                converted_vids.append(vid)
                                        except Exception as e_v:
                                            print(
                                                f"Failed to convert victim TMP_FontAsset path_id={vid} to dynamic: {e_v}",
                                                file=sys.stderr,
                                            )
                                    if converted_count > 0:
                                        first_vid = converted_vids[0]
                                        # Also inject first converted dynamic font into TMP_Settings.m_fallbackFontAssets
                                        for obj in tmp_settings_objs:
                                            try:
                                                data = obj.read()
                                                tree = obj.read_typetree() if hasattr(obj, "read_typetree") else data.read_typetree()
                                                if isinstance(tree, dict) and "m_fallbackFontAssets" in tree:
                                                    fallbacks = tree.get("m_fallbackFontAssets")
                                                    if isinstance(fallbacks, list):
                                                        if not any(isinstance(x, dict) and x.get("m_PathID") == first_vid for x in fallbacks):
                                                            fallbacks.insert(0, {"m_FileID": 0, "m_PathID": first_vid})
                                                            _save_typetree_safe(obj, data, tree)
                                                            changed = True
                                                            typetree_modified_pids.add(obj.path_id)
                                            except Exception:
                                                pass
                                        print(
                                            f"[font] Converted {converted_count} static TMP_FontAsset(s) in "
                                            f"{os.path.basename(fpath)} to Dynamic Multi-Atlas pointing to Font path_id={ttf_pid}.",
                                            file=sys.stderr,
                                        )
                    except Exception as e_adv_font:
                        print(f"Error in advanced font fallback handling for {fpath}: {e_adv_font}", file=sys.stderr)

                # Always attempt raw inject: content blobs (Naninovel/I2/…) never use m_Text.
                # Font-gap PPtr rewrite runs INSIDE raw inject, AFTER any UnityPy
                # save — a pre-save disk patch would be clobbered by env.file.save().
                raw_written = self._inject_raw_fallback(
                    fpath, root, translations,
                    env=env, font_bytes=font_bytes,
                    target_lang=target_lang,
                    already_changed=changed,
                    skip_pids=typetree_modified_pids,
                    skip_font_pptr=(converted_count > 0),
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
                if self._is_modified_in_run(fpath):
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                else:
                    orig_bytes = read_backup_original(root, rel_path)
                    if orig_bytes is not None:
                        try:
                            content = orig_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            content = orig_bytes.decode("latin-1", errors="ignore")
                    else:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()

                lines = content.splitlines()
                changed = False
                line_idx = 0
                is_asset_file = fpath.lower().endswith(".asset")
                while line_idx < len(lines):
                    line = lines[line_idx]
                    m_field = None
                    raw_val = None
                    start_line_idx = line_idx
                    key_prefix = ""
                    match = re.match(r'^(\s*)([a-zA-Z_][a-zA-Z0-9_]*):\s*(.*)$', line)
                    if match:
                        fname = match.group(2)
                        if fname in ("m_Text", "m_text", "m_TextUGUI"):
                            m_field = "m_Text"
                            raw_val = match.group(3).strip()
                            key_prefix = line[:match.start(3)]
                        elif is_asset_file and _is_typetree_text_field(fname):
                            m_field = fname
                            raw_val = match.group(3).strip()
                            key_prefix = line[:match.start(3)]

                    if m_field is not None and raw_val is not None:
                        val, line_idx = _parse_multiline_yaml_val(lines, line_idx, raw_val)
                        if val and not val.isspace() and _is_game_text(val):
                            path = ["AssetYAML", m_field, str(start_line_idx)]
                            sid = make_id(self.engine, rel_path, path, val)

                            if sid in translations:
                                translated = translations[sid]
                                escaped_trans = json.dumps(translated, ensure_ascii=False)
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
                    self._mark_modified_in_run(fpath)
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

        if written > 0:
            aa_dir = find_aa_dir(root)
            if aa_dir:
                for dirpath, _, filenames in os.walk(aa_dir):
                    for f in filenames:
                        cat_file = os.path.join(dirpath, f)
                        if f.startswith("catalog") and f.endswith(".json"):
                            self._disable_catalog_crc(cat_file)
                        elif f.startswith("catalog") and f.endswith(".bin"):
                            self._disable_catalog_bin_crc(cat_file)

        self.finalize_backups(root)
        return written

    def _find_tmp_donors_and_victims(
        self, env: Any, script: str
    ) -> tuple[int | None, set[int], dict[int, tuple[str, bool | None]], list[tuple[int, int]]]:
        """Catalog Font assets and TMP FontAssets to identify a donor and missing victims for a script.

        Returns:
            (tmp_donor, tmp_victims, tmp_cov, forbidden_ranges)
        """
        font_cov: dict[int, tuple[str, bool | None]] = {}
        font_by_base: dict[str, tuple[int, bool | None]] = {}
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

        tmp_cov: dict[int, tuple[str, bool | None]] = {}
        for obj in env.objects:
            class_name = getattr(getattr(obj, "serialized_type", None), "class_name", None)
            if obj.type.name != "MonoBehaviour" and obj.type.name != "TMP_FontAsset" and class_name != "TMP_FontAsset":
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
                base = _font_base_name(name)
                ttf = font_by_base.get(base)
                if ttf is not None and ttf[1] is False:
                    cov = False
            tmp_cov[obj.path_id] = (name, cov)

        if not tmp_cov:
            return None, set(), {}, forbidden_ranges

        tmp_donors = [pid for pid, (_, c) in tmp_cov.items() if c is True]

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
        tmp_victims = {
            pid for pid, (_, c) in tmp_cov.items()
            if c is False and pid != tmp_donor
        }
        return tmp_donor, tmp_victims, tmp_cov, forbidden_ranges

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
        f_lower = os.path.basename(fpath).lower()
        if f_lower.endswith((".bundle", ".assetbundle")) or f_lower == "data.unity3d":
            return
        script = _target_script(target_lang)
        if not script:
            return
        try:
            tmp_donor, tmp_victims, tmp_cov, forbidden_ranges = self._find_tmp_donors_and_victims(env, script)

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
            if fpath.lower().endswith(".split0"):
                base_prefix = fpath[:-7]
                parts_data = bytearray()
                idx = 0
                while True:
                    part_path = f"{base_prefix}.split{idx}"
                    if os.path.isfile(part_path):
                        with open(part_path, "rb") as pf:
                            parts_data.extend(pf.read())
                        idx += 1
                    else:
                        break
                file_bytes = parts_data
            else:
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
                root = getattr(self, "_current_root", None)
                if root:
                    try:
                        self._save_or_split_asset(root, fpath, bytes(file_bytes))
                    except Exception:
                        with open(fpath, "wb") as f:
                            f.write(file_bytes)
                        self._mark_modified_in_run(fpath)
                else:
                    with open(fpath, "wb") as f:
                        f.write(file_bytes)
                    self._mark_modified_in_run(fpath)
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

        aa_bundles = find_aa_bundles(root)
        extra_loc = [p for p in sorted(getattr(self, "_discovered_loc_files", set())) if p not in aa_bundles and os.path.isfile(p)]
        loc_files = aa_bundles + extra_loc
        if not loc_files:
            return 0

        # Phase 1: Collect global SharedTableData across ALL bundles
        global_shared_tables: dict[int, dict] = {}
        all_shared_list: list[dict] = []

        for bundle_path in loc_files:
            try:
                rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
                if self._is_modified_in_run(bundle_path):
                    env = UnityPy.load(bundle_path)
                else:
                    orig_bytes = self._read_asset_backup_original(root, bundle_path, rel_path)
                    if orig_bytes is not None:
                        try:
                            bio = io.BytesIO(orig_bytes)
                            bio.name = bundle_path
                            env = UnityPy.load(bio, path=os.path.dirname(bundle_path))
                        except Exception:
                            env = UnityPy.load(bundle_path)
                    else:
                        env = UnityPy.load(bundle_path)
                self._attach_generator(root, env)
                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_Entries" in tree and "m_TableCollectionName" in tree:
                            col_name = tree.get("m_TableCollectionName") or ""
                            entries = tree.get("m_Entries", [])
                            tbl_info = {
                                "name": col_name,
                                "id_to_key": {e["m_Id"]: e["m_Key"] for e in entries if "m_Id" in e and "m_Key" in e},
                            }
                            global_shared_tables[obj.path_id] = tbl_info
                            all_shared_list.append(tbl_info)
                    except Exception:
                        pass
            except Exception:
                pass

        # Phase 2: Inject into StringTables
        written = 0

        for bundle_path in loc_files:
            rel_path = os.path.relpath(bundle_path, root).replace("\\", "/")
            try:
                if self._is_modified_in_run(bundle_path):
                    env = UnityPy.load(bundle_path)
                else:
                    orig_bytes = self._read_asset_backup_original(root, bundle_path, rel_path)
                    if orig_bytes is not None:
                        try:
                            bio = io.BytesIO(orig_bytes)
                            bio.name = bundle_path
                            env = UnityPy.load(bio, path=os.path.dirname(bundle_path))
                        except Exception:
                            env = UnityPy.load(bundle_path)
                    else:
                        env = UnityPy.load(bundle_path)
                self._attach_generator(root, env)
                changed = False

                col_entry_translations: dict[tuple[str, int], str] = {}
                target_locale_tables: list[tuple[Any, Any, dict, str, str]] = []

                for obj in env.objects:
                    if obj.type.name != "MonoBehaviour":
                        continue
                    try:
                        data = obj.read()
                        tree = data.read_typetree()
                        if "m_TableData" not in tree or "m_LocaleIdentifier" not in tree:
                            continue

                        raw_locale = tree["m_LocaleIdentifier"].get("m_Code", "")
                        shared = _resolve_shared_table(tree, obj, data, global_shared_tables, all_shared_list)
                        id_to_key = shared.get("id_to_key", {})
                        collection_name = shared.get("name") or getattr(obj, "name", None) or getattr(data, "name", None) or "Unknown"

                        is_target_locale = _matches_unity_locale(raw_locale, "", target_lang)
                        if is_target_locale:
                            target_locale_tables.append((obj, data, tree, collection_name, raw_locale))

                        entry_modified = False
                        for entry in tree["m_TableData"]:
                            value = entry.get("m_Localized", "")
                            if not value:
                                continue
                            entry_id = entry["m_Id"]
                            key_name = id_to_key.get(entry_id, str(entry_id))
                            path = ["StringTable", collection_name, raw_locale, key_name]
                            sid = make_id(self.engine, rel_path, path, value)
                            if sid in translations:
                                entry["m_Localized"] = translations[sid]
                                entry_modified = True
                                written += 1
                                col_entry_translations[(collection_name, entry_id)] = translations[sid]

                        if entry_modified:
                            _save_typetree_safe(obj, data, tree)
                            changed = True
                    except Exception:
                        pass

                # Second pass to sync translations into target locale table (e.g. ru table)
                if col_entry_translations and target_locale_tables:
                    for obj, data, tree, collection_name, raw_locale in target_locale_tables:
                        try:
                            sync_modified = False
                            for entry in tree.get("m_TableData", []):
                                entry_id = entry.get("m_Id")
                                if (collection_name, entry_id) in col_entry_translations:
                                    target_val = col_entry_translations[(collection_name, entry_id)]
                                    if entry.get("m_Localized") != target_val:
                                        entry["m_Localized"] = target_val
                                        sync_modified = True
                            if sync_modified:
                                _save_typetree_safe(obj, data, tree)
                                changed = True
                        except Exception:
                            pass

                if changed:
                    saved_bytes = None
                    try:
                        saved_bytes = env.file.save(packer="original")
                    except Exception:
                        try:
                            saved_bytes = env.file.save(packer="none")
                        except Exception:
                            try:
                                saved_bytes = env.file.save()
                            except Exception:
                                pass
                    if not saved_bytes:
                        try:
                            saved_bytes = env.file.save()
                        except Exception:
                            pass

                    if isinstance(saved_bytes, (bytes, bytearray)) and len(saved_bytes) > 0:
                        self._save_or_split_asset(root, bundle_path, bytes(saved_bytes))
                    else:
                        # Multi-cab / container: fall back to env.save into temp dir
                        tmp = tempfile.mkdtemp(prefix="interprex_unity_loc_")
                        try:
                            env.save(pack="none", out_path=tmp)
                            base = os.path.basename(bundle_path)
                            candidate = os.path.join(tmp, base)
                            if not os.path.isfile(candidate):
                                for dirpath, _, filenames in os.walk(tmp):
                                    if base in filenames:
                                        candidate = os.path.join(dirpath, base)
                                        break
                            if os.path.isfile(candidate):
                                with open(candidate, "rb") as cf:
                                    cdata = cf.read()
                                self._save_or_split_asset(root, bundle_path, cdata)
                            else:
                                print(
                                    f"UnityPy save produced no {base}; loc inject may be incomplete",
                                    file=sys.stderr,
                                )
                        finally:
                            shutil.rmtree(tmp, ignore_errors=True)

            except Exception as e:
                print(f"Error injecting into bundle {bundle_path}: {e}", file=sys.stderr)

        if written > 0:
            aa_dir = find_aa_dir(root)
            if aa_dir:
                for dirpath, _, filenames in os.walk(aa_dir):
                    for f in filenames:
                        cat_file = os.path.join(dirpath, f)
                        if f.startswith("catalog") and f.endswith(".json"):
                            self._disable_catalog_crc(cat_file)
                        elif f.startswith("catalog") and f.endswith(".bin"):
                            self._disable_catalog_bin_crc(cat_file)

        self.finalize_backups(root)
        return written

    def _disable_catalog_crc(self, catalog_path: str) -> bool:
        """Set m_UseCrcForCachedBundles=false and m_Crc=0 in Addressables catalog.json."""
        if not os.path.isfile(catalog_path):
            return False
        try:
            with open(catalog_path, "r", encoding="utf-8") as f:
                content = f.read()
            data = json.loads(content)
            modified = False

            def _disable_crc_recursive(node: Any) -> bool:
                inner_mod = False
                if isinstance(node, dict):
                    if "m_UseCrcForCachedBundles" in node and node["m_UseCrcForCachedBundles"] is True:
                        node["m_UseCrcForCachedBundles"] = False
                        inner_mod = True
                    if "m_Crc" in node and node["m_Crc"] != 0:
                        node["m_Crc"] = 0
                        inner_mod = True
                    if "m_ExtraDataString" in node and isinstance(node["m_ExtraDataString"], str):
                        try:
                            import base64
                            extra_raw = base64.b64decode(node["m_ExtraDataString"])
                            sub_mod = False

                            def _sub_utf8(m: re.Match[bytes]) -> bytes:
                                nonlocal sub_mod
                                sub_mod = True
                                return m.group(1) + b"0" + b" " * (len(m.group(2)) - 1)

                            def _sub_utf16(m: re.Match[bytes]) -> bytes:
                                nonlocal sub_mod
                                sub_mod = True
                                k = len(m.group(2)) // 2
                                return m.group(1) + b"0\x00" + b" \x00" * (k - 1)

                            new_raw = re.sub(rb'("(?:m_Crc|m_CRC)"\s*:\s*)(\d+)', _sub_utf8, extra_raw)
                            new_raw = re.sub(
                                rb'("\x00m\x00_\x00[Cc]\x00[Rr]\x00[Cc]\x00"\x00:\x00(?:\x20\x00|\t\x00|\s\x00)*)((?:[0-9]\x00)+)',
                                _sub_utf16,
                                new_raw,
                            )
                            if sub_mod and new_raw != extra_raw:
                                node["m_ExtraDataString"] = base64.b64encode(new_raw).decode("ascii")
                                inner_mod = True
                        except Exception:
                            pass

                    for v in node.values():
                        if isinstance(v, (dict, list)):
                            if _disable_crc_recursive(v):
                                inner_mod = True
                elif isinstance(node, list):
                    for item in node:
                        if isinstance(item, (dict, list)):
                            if _disable_crc_recursive(item):
                                inner_mod = True
                return inner_mod

            if isinstance(data, (dict, list)):
                if _disable_crc_recursive(data):
                    modified = True

            if modified:
                root = getattr(self, "_current_root", None)
                if root:
                    try:
                        self.backup_file(root, catalog_path)
                    except Exception:
                        pass
                with open(catalog_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                self._mark_modified_in_run(catalog_path)
                return True
        except Exception as e:
            print(f"Error disabling catalog CRC in {catalog_path}: {e}", file=sys.stderr)
        return False

    def _disable_catalog_bin_crc(self, catalog_path: str) -> bool:
        """Neutralize CRC checks directly in binary Addressables catalog (*.bin) without altering file length or offsets."""
        if not os.path.isfile(catalog_path):
            return False
        try:
            with open(catalog_path, "rb") as f:
                raw = f.read()

            sub_mod = False

            def _sub_utf8(m: re.Match[bytes]) -> bytes:
                nonlocal sub_mod
                sub_mod = True
                return m.group(1) + b"0" + b" " * (len(m.group(2)) - 1)

            def _sub_utf16(m: re.Match[bytes]) -> bytes:
                nonlocal sub_mod
                sub_mod = True
                k = len(m.group(2)) // 2
                return m.group(1) + b"0\x00" + b" \x00" * (k - 1)

            new_raw = re.sub(rb'("(?:m_Crc|m_CRC)"\s*:\s*)(\d+)', _sub_utf8, raw)
            new_raw = re.sub(
                rb'("\x00m\x00_\x00[Cc]\x00[Rr]\x00[Cc]\x00"\x00:\x00(?:\x20\x00|\t\x00|\s\x00)*)((?:[0-9]\x00)+)',
                _sub_utf16,
                new_raw,
            )

            if sub_mod and new_raw != raw and len(new_raw) == len(raw):
                root = getattr(self, "_current_root", None)
                if root:
                    try:
                        self.backup_file(root, catalog_path)
                    except Exception:
                        pass
                with open(catalog_path, "wb") as f:
                    f.write(new_raw)
                self._mark_modified_in_run(catalog_path)
                print(
                    f"[catalog] Successfully neutralized CRC check in binary catalog {os.path.basename(catalog_path)}",
                    file=sys.stderr,
                )
                return True
            else:
                fname = os.path.basename(catalog_path)
                print(
                    f"[catalog] Addressables binary catalog detected: {fname}. "
                    "CRC check is compiled into catalog.bin; if the game fails to load modified bundles, "
                    "CRC verification must be disabled in runtime Addressables settings.",
                    file=sys.stderr,
                )
                if hasattr(self, "last_report") and self.last_report and hasattr(self.last_report, "caps"):
                    self.last_report.caps.notes.append(
                        f"Binary Addressables catalog ({fname}) detected: runtime CRC verification might be enforced."
                    )
                return False
        except Exception as e:
            print(f"Error neutralizing binary catalog CRC in {catalog_path}: {e}", file=sys.stderr)
            return False

    def _find_global_metadata(self, root: str) -> str | None:
        """Find global-metadata.dat inside root."""
        std_candidates = [
            os.path.join(root, "il2cpp_data", "Metadata", "global-metadata.dat"),
        ]
        for name in os.listdir(root):
            if name.lower().endswith("_data"):
                std_candidates.append(os.path.join(root, name, "il2cpp_data", "Metadata", "global-metadata.dat"))
        for cand in std_candidates:
            if os.path.isfile(cand):
                return cand

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in IGNORE_DIRS and not d.startswith(".")]
            for f in filenames:
                if f.lower() == "global-metadata.dat":
                    return os.path.join(dirpath, f)
        return None

    def _extract_il2cpp_metadata(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        """Extract hardcoded string literals from global-metadata.dat."""
        self._current_root = root
        gm_path = self._find_global_metadata(root)
        if not gm_path or not os.path.isfile(gm_path):
            return []

        rel_path = os.path.relpath(gm_path, root).replace("\\", "/")
        if sub_paths and not any(rel_path.startswith(sp.replace("\\", "/")) for sp in sub_paths):
            return []

        try:
            orig_bytes = read_backup_original(root, rel_path)
            if orig_bytes is not None:
                raw = orig_bytes
            else:
                with open(gm_path, "rb") as f:
                    raw = f.read()

            if len(raw) < 256:
                return []
            magic = struct.unpack_from("<I", raw, 0)[0]
            if magic != 0xFAB11BAF:
                return []

            lit_offset, lit_size, data_offset, data_size = struct.unpack_from("<IIII", raw, 8)
            if lit_offset + lit_size > len(raw) or data_offset + data_size > len(raw):
                return []

            count = lit_size // 8
            results: list[TranslationString] = []
            for idx in range(count):
                length, data_idx = struct.unpack_from("<II", raw, lit_offset + idx * 8)
                if data_idx + length > data_size:
                    continue
                chunk = raw[data_offset + data_idx : data_offset + data_idx + length]
                try:
                    s = chunk.decode("utf-8")
                    if _is_il2cpp_safe_literal(s):
                        path = ["Il2CppMetadata", str(idx)]
                        ctx = f"IL2CPP StringLiteral #{idx}, File: {os.path.basename(gm_path)}"
                        results.append(self._mk(rel_path, path, s, ctx))
                except UnicodeDecodeError:
                    pass
            return results
        except Exception as e:
            print(f"Error extracting IL2CPP metadata {gm_path}: {e}", file=sys.stderr)
            return []

    def _inject_il2cpp_metadata(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        """Inject translations into global-metadata.dat string literals."""
        self._current_root = root
        gm_path = self._find_global_metadata(root)
        if not gm_path or not os.path.isfile(gm_path):
            return 0

        rel_path = os.path.relpath(gm_path, root).replace("\\", "/")
        if sub_paths and not any(rel_path.startswith(sp.replace("\\", "/")) for sp in sub_paths):
            return 0

        try:
            if self._is_modified_in_run(gm_path):
                with open(gm_path, "rb") as f:
                    raw = bytearray(f.read())
            else:
                orig_bytes = read_backup_original(root, rel_path)
                if orig_bytes is not None:
                    raw = bytearray(orig_bytes)
                else:
                    with open(gm_path, "rb") as f:
                        raw = bytearray(f.read())

            if len(raw) < 256:
                return 0
            magic = struct.unpack_from("<I", raw, 0)[0]
            if magic != 0xFAB11BAF:
                return 0

            lit_offset, lit_size, data_offset, data_size = struct.unpack_from("<IIII", raw, 8)
            if lit_offset + lit_size > len(raw) or data_offset + data_size > len(raw):
                return 0

            count = lit_size // 8
            replacements: dict[int, str] = {}
            for idx in range(count):
                length, data_idx = struct.unpack_from("<II", raw, lit_offset + idx * 8)
                if data_idx + length > data_size:
                    continue
                chunk = raw[data_offset + data_idx : data_offset + data_idx + length]
                try:
                    s = chunk.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                path = ["Il2CppMetadata", str(idx)]
                sid = make_id(self.engine, rel_path, path, s)
                if sid in translations and translations[sid] != s:
                    replacements[idx] = translations[sid]

            if not replacements:
                return 0

            new_data = bytearray()
            new_descriptors = bytearray()
            for idx in range(count):
                length, data_idx = struct.unpack_from("<II", raw, lit_offset + idx * 8)
                if idx in replacements:
                    chunk = replacements[idx].encode("utf-8")
                else:
                    chunk = bytes(raw[data_offset + data_idx : data_offset + data_idx + length])
                new_descriptors.extend(struct.pack("<II", len(chunk), len(new_data)))
                new_data.extend(chunk)

            pad = (4 - (len(new_data) % 4)) % 4
            if pad:
                new_data.extend(b"\x00" * pad)

            new_data_size = len(new_data)
            delta = new_data_size - data_size

            header_end = min(lit_offset, data_offset)
            for p in range(8, min(len(raw), 512), 8):
                if p >= header_end:
                    break
                sec_off, sec_sz = struct.unpack_from("<II", raw, p)
                if sec_sz > 0 and 64 <= sec_off <= len(raw) and sec_off + sec_sz <= len(raw) + 16:
                    if sec_off < header_end:
                        header_end = sec_off
            for p in range(8, header_end, 8):
                sec_off, sec_sz = struct.unpack_from("<II", raw, p)
                if sec_off >= data_offset + data_size and (sec_sz == 0 or sec_off + sec_sz <= len(raw) + 16):
                    struct.pack_into("<I", raw, p, sec_off + delta)

            struct.pack_into("<I", raw, 20, new_data_size)

            actual_lit_offset = lit_offset + delta if lit_offset >= data_offset + data_size else lit_offset
            raw[actual_lit_offset : actual_lit_offset + len(new_descriptors)] = new_descriptors

            new_file = raw[:data_offset] + new_data + raw[data_offset + data_size:]

            self.backup_file(root, gm_path)
            with open(gm_path, "wb") as f:
                f.write(new_file)
            self._mark_modified_in_run(gm_path)

            self.finalize_backups(root)
            return len(replacements)
        except Exception as e:
            print(f"Error injecting IL2CPP metadata {gm_path}: {e}", file=sys.stderr)
            return 0

    def _find_streaming_assets_dirs(self, root: str) -> list[str]:
        sa_dirs: list[str] = []
        try:
            for name in os.listdir(root):
                full = os.path.join(root, name)
                if not os.path.isdir(full):
                    continue
                if name.lower() == "streamingassets":
                    sa_dirs.append(full)
                elif name.lower().endswith("_data") or name.lower() == "data":
                    cand = os.path.join(full, "StreamingAssets")
                    if os.path.isdir(cand):
                        sa_dirs.append(cand)
        except OSError:
            pass
        return sa_dirs

    def _iter_streaming_files(self, root: str, sub_paths: list[str] | None = None) -> list[str]:
        files: list[str] = []
        for sa in self._find_streaming_assets_dirs(root):
            for dp, dn, filenames in os.walk(sa):
                if os.path.basename(dp).lower() == "aa" or "/aa/" in dp.replace("\\", "/").lower():
                    continue
                for f in filenames:
                    fpath = os.path.join(dp, f)
                    rel_path = os.path.relpath(fpath, root).replace("\\", "/")
                    if sub_paths and not any(rel_path.startswith(sp.replace("\\", "/")) for sp in sub_paths):
                        continue
                    if is_valid_streaming_file(fpath, rel_path):
                        files.append(fpath)
        return files

    def _extract_streaming_files(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results: list[TranslationString] = []
        for fpath in self._iter_streaming_files(root, sub_paths):
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            base = os.path.basename(fpath)
            fl = base.lower()

            orig_bytes = read_backup_original(root, rel_path)
            if orig_bytes is not None:
                try:
                    content = orig_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    content = orig_bytes.decode("latin-1", errors="replace")
            else:
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(fpath, "r", encoding="latin-1") as f:
                            content = f.read()
                    except Exception:
                        continue
                except Exception:
                    continue

            if not content.strip():
                continue

            # 1. JSON (Ink or Generic)
            if fl.endswith(".json"):
                is_ink = False
                if "inkVersion" in content and "root" in content:
                    try:
                        ink_data = json.loads(content)
                        if isinstance(ink_data, dict) and "inkVersion" in ink_data and "root" in ink_data:
                            is_ink = True
                            ink_hits = _walk_ink_root(ink_data.get("root"), [0])
                            for idx, raw_ink_text in ink_hits:
                                ink_text = raw_ink_text[1:]
                                if _is_game_text(ink_text):
                                    path = ["StreamingFile", "ink", str(idx)]
                                    ctx = f"StreamingAsset (Ink): {base}, Index: {idx}"
                                    results.append(self._mk(rel_path, path, ink_text, ctx))
                    except Exception:
                        pass

                if not is_ink:
                    try:
                        json_obj = json.loads(content)
                        leaves = _walk_json_leaves(json_obj, [])
                        for jpath, val in leaves:
                            if _is_game_text(val):
                                dot_path = ".".join(jpath)
                                path = ["StreamingFile", "json", dot_path]
                                ctx = f"StreamingAsset (JSON): {base}, Path: {dot_path}"
                                results.append(self._mk(rel_path, path, val, ctx))
                    except Exception:
                        pass

            # 2. CSV / TSV
            elif fl.endswith((".csv", ".tsv")):
                delim = "\t" if fl.endswith(".tsv") else _detect_csv_delim(content)
                if delim:
                    try:
                        reader = csv.reader(io.StringIO(content), delimiter=delim)
                        rows = list(reader)
                        if rows:
                            header = [h.strip().lower() for h in rows[0]]
                            text_col_indices = _select_csv_text_columns(header)

                            for r_idx in range(1, len(rows)):
                                row = rows[r_idx]
                                key_prefix = row[0] if len(row) > 0 else str(r_idx)
                                for ci in text_col_indices:
                                    if ci < len(row):
                                        val = row[ci]
                                        if val and _is_game_text(val, allow_title_word=True):
                                            col_name = header[ci] if ci < len(header) else str(ci)
                                            path = ["StreamingFile", "csv", str(r_idx), str(ci)]
                                            ctx = f"StreamingAsset (CSV): {base}, Row: {r_idx} (key={key_prefix}), Col: {col_name}"
                                            results.append(self._mk(rel_path, path, val, ctx))
                    except Exception:
                        pass

            # 3. Yarn Spinner
            elif fl.endswith(".yarn"):
                lines = content.splitlines()
                for line_idx, line in enumerate(lines):
                    sline = line.strip()
                    if not sline or sline.startswith("//") or sline.startswith("<<") or sline.startswith("===") or sline.startswith("---"):
                        continue
                    if ":" in sline and any(sline.lower().startswith(k) for k in ("title:", "tags:", "position:", "colorid:")):
                        continue

                    # Separate #tag and //comment at end of line so #line:xxx is preserved
                    tag_match = re.search(r"(\s+#[^\s]+.*|\s+//.*)$", sline)
                    clean_line = sline[:tag_match.start()] if tag_match else sline
                    clean_line = clean_line.strip()
                    if not clean_line:
                        continue

                    if clean_line.startswith("->"):
                        choice_text = clean_line[2:].strip()
                        if _is_game_text(choice_text):
                            path = ["StreamingFile", "yarn", str(line_idx)]
                            ctx = f"StreamingAsset (Yarn Choice): {base}, Line: {line_idx + 1}"
                            results.append(self._mk(rel_path, path, choice_text, ctx))
                    elif ":" in clean_line:
                        speaker, text_part = clean_line.split(":", 1)
                        text_part = text_part.strip()
                        if _is_game_text(text_part):
                            path = ["StreamingFile", "yarn", str(line_idx)]
                            ctx = f"StreamingAsset (Yarn): {base}, Speaker: {speaker.strip()}, Line: {line_idx + 1}"
                            results.append(self._mk(rel_path, path, text_part, ctx))
                    elif _is_game_text(clean_line):
                        path = ["StreamingFile", "yarn", str(line_idx)]
                        ctx = f"StreamingAsset (Yarn): {base}, Line: {line_idx + 1}"
                        results.append(self._mk(rel_path, path, clean_line, ctx))

            # 4. Plain text
            elif fl.endswith(".txt"):
                lines = content.splitlines()
                for line_idx, line in enumerate(lines):
                    sline = line.strip()
                    if sline and _is_game_text(sline):
                        path = ["StreamingFile", "txt", str(line_idx)]
                        ctx = f"StreamingAsset (TXT): {base}, Line: {line_idx + 1}"
                        results.append(self._mk(rel_path, path, sline, ctx))

            # 5. XML
            elif fl.endswith(".xml"):
                try:
                    xml_hits = _extract_xml_strings(content)
                    for xml_path, val in xml_hits:
                        path = ["StreamingFile", "xml", xml_path]
                        ctx = f"StreamingAsset (XML): {base}, Path: {xml_path}"
                        results.append(self._mk(rel_path, path, val, ctx))
                except Exception:
                    pass

        return results

    def _inject_streaming_files(
        self,
        root: str,
        translations: dict[str, str],
        target_lang: str | None = None,
        sub_paths: list[str] | None = None,
    ) -> int:
        self._current_root = root
        written = 0
        for fpath in self._iter_streaming_files(root, sub_paths):
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            base = os.path.basename(fpath)
            fl = base.lower()

            if self._is_modified_in_run(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                except UnicodeDecodeError:
                    try:
                        with open(fpath, "r", encoding="latin-1") as f:
                            content = f.read()
                    except Exception:
                        continue
                except Exception:
                    continue
            else:
                orig_bytes = read_backup_original(root, rel_path)
                if orig_bytes is not None:
                    try:
                        content = orig_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        content = orig_bytes.decode("latin-1", errors="replace")
                else:
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                    except UnicodeDecodeError:
                        try:
                            with open(fpath, "r", encoding="latin-1") as f:
                                content = f.read()
                        except Exception:
                            continue
                    except Exception:
                        continue

            if not content.strip():
                continue

            modified = False
            file_written = 0
            new_content = None

            # 1. JSON (Ink or Generic)
            if fl.endswith(".json"):
                is_ink = False
                if "inkVersion" in content and "root" in content:
                    try:
                        ink_data = json.loads(content)
                        if isinstance(ink_data, dict) and "inkVersion" in ink_data and "root" in ink_data:
                            is_ink = True
                            ink_hits = _walk_ink_root(ink_data.get("root"), [0])
                            ink_replacements: dict[int, str] = {}
                            for idx, raw_ink_text in ink_hits:
                                ink_text = raw_ink_text[1:]
                                path = ["StreamingFile", "ink", str(idx)]
                                sid = make_id(self.engine, rel_path, path, ink_text)
                                if sid in translations:
                                    ink_replacements[idx] = translations[sid]
                            if ink_replacements:
                                if _replace_ink_root(ink_data.get("root"), [0], ink_replacements):
                                    new_content = json.dumps(ink_data, ensure_ascii=False)
                                    modified = True
                                    file_written += len(ink_replacements)
                    except Exception:
                        pass

                if not is_ink:
                    try:
                        json_obj = json.loads(content)
                        leaves = _walk_json_leaves(json_obj, [])
                        j_hits = 0
                        for jpath, orig_val in leaves:
                            dot_path = ".".join(jpath)
                            path = ["StreamingFile", "json", dot_path]
                            sid = make_id(self.engine, rel_path, path, orig_val)
                            if sid in translations:
                                if _set_typetree_by_path(json_obj, jpath, translations[sid]):
                                    j_hits += 1
                        if j_hits > 0:
                            new_content = json.dumps(json_obj, ensure_ascii=False, indent=2)
                            modified = True
                            file_written += j_hits
                    except Exception:
                        pass

            # 2. CSV / TSV
            elif fl.endswith((".csv", ".tsv")):
                delim = "\t" if fl.endswith(".tsv") else _detect_csv_delim(content)
                if delim:
                    try:
                        reader = csv.reader(io.StringIO(content), delimiter=delim)
                        rows = list(reader)
                        c_hits = 0
                        if rows:
                            header = [h.strip().lower() for h in rows[0]]
                            text_col_indices = _select_csv_text_columns(header)

                            for r_idx in range(1, len(rows)):
                                row = rows[r_idx]
                                for ci in text_col_indices:
                                    if ci < len(row):
                                        orig_val = row[ci]
                                        path = ["StreamingFile", "csv", str(r_idx), str(ci)]
                                        sid = make_id(self.engine, rel_path, path, orig_val)
                                        if sid in translations:
                                            row[ci] = translations[sid]
                                            c_hits += 1

                        if c_hits > 0:
                            out_io = io.StringIO()
                            writer = csv.writer(out_io, delimiter=delim, lineterminator="\n")
                            writer.writerows(rows)
                            new_content = out_io.getvalue()
                            modified = True
                            file_written += c_hits
                    except Exception:
                        pass

            # 3. Yarn Spinner
            elif fl.endswith(".yarn"):
                lines = content.splitlines()
                y_hits = 0
                for line_idx, line in enumerate(lines):
                    sline = line.strip()
                    if not sline or sline.startswith("//") or sline.startswith("<<") or sline.startswith("===") or sline.startswith("---"):
                        continue
                    if ":" in sline and any(sline.lower().startswith(k) for k in ("title:", "tags:", "position:", "colorid:")):
                        continue

                    # Separate #tag and //comment at end of line so #line:xxx is preserved
                    tag_match = re.search(r"(\s+#[^\s]+.*|\s+//.*)$", sline)
                    yarn_suffix = tag_match.group(1) if tag_match else ""
                    clean_line = sline[:tag_match.start()] if tag_match else sline
                    clean_line = clean_line.strip()
                    if not clean_line:
                        continue

                    if clean_line.startswith("->"):
                        choice_text = clean_line[2:].strip()
                        path = ["StreamingFile", "yarn", str(line_idx)]
                        sid = make_id(self.engine, rel_path, path, choice_text)
                        if sid in translations:
                            prefix = line[:line.index("->") + 2]
                            lines[line_idx] = f"{prefix} {translations[sid]}{yarn_suffix}"
                            y_hits += 1
                    elif ":" in clean_line:
                        speaker, text_part = clean_line.split(":", 1)
                        path = ["StreamingFile", "yarn", str(line_idx)]
                        sid = make_id(self.engine, rel_path, path, text_part.strip())
                        if sid in translations:
                            prefix = line[:line.index(":") + 1]
                            lines[line_idx] = f"{prefix} {translations[sid]}{yarn_suffix}"
                            y_hits += 1
                    else:
                        path = ["StreamingFile", "yarn", str(line_idx)]
                        sid = make_id(self.engine, rel_path, path, clean_line)
                        if sid in translations:
                            indent = line[:len(line) - len(line.lstrip())]
                            lines[line_idx] = f"{indent}{translations[sid]}{yarn_suffix}"
                            y_hits += 1
                if y_hits > 0:
                    new_content = "\n".join(lines)
                    modified = True
                    file_written += y_hits

            # 4. Plain text
            elif fl.endswith(".txt"):
                lines = content.splitlines()
                t_hits = 0
                for line_idx, line in enumerate(lines):
                    sline = line.strip()
                    if sline:
                        path = ["StreamingFile", "txt", str(line_idx)]
                        sid = make_id(self.engine, rel_path, path, sline)
                        if sid in translations:
                            indent = line[:len(line) - len(line.lstrip())]
                            trailing = line[len(line.rstrip()):]
                            lines[line_idx] = f"{indent}{translations[sid]}{trailing}"
                            t_hits += 1
                if t_hits > 0:
                    new_content = "\n".join(lines)
                    modified = True
                    file_written += t_hits

            # 5. XML
            elif fl.endswith(".xml"):
                try:
                    xml_hits = _extract_xml_strings(content)
                    xml_replacements: dict[str, str] = {}
                    for xml_path, orig_val in xml_hits:
                        path = ["StreamingFile", "xml", xml_path]
                        sid = make_id(self.engine, rel_path, path, orig_val)
                        if sid in translations:
                            xml_replacements[xml_path] = translations[sid]
                    if xml_replacements:
                        new_content = _inject_xml_strings(content, xml_replacements)
                        modified = True
                        file_written += len(xml_replacements)
                except Exception:
                    pass

            if modified and new_content is not None:
                self.backup_file(root, fpath)
                with open(fpath, "w", encoding="utf-8") as f:
                    f.write(new_content)
                self._mark_modified_in_run(fpath)
                written += file_written

        self.finalize_backups(root)
        return written
