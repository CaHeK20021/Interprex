"""Parser for Unity.

Extracts/injects code strings from C# DLL files (using DllEditor Mono.Cecil helper)
and UI-text strings from scenes, prefabs, assets, and Addressables localization StringTables.
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
    "Pathfinding"
}

IGNORE_DIRS = {
    "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups", "__macosx"
}

def should_skip_type(type_full_name: str) -> bool:
    if not type_full_name:
        return False
    for ns in SKIP_NAMESPACES:
        if type_full_name.startswith(ns + ".") or type_full_name == ns:
            return True
    return False

def is_custom_dll(filename: str) -> bool:
    """True if the DLL is likely game code or a custom mod (not system/engine library)."""
    fn = filename.lower()
    if not fn.endswith(".dll"):
        return False
    system_prefixes = (
        "system.", "microsoft.", "unityengine.", "unityeditor.", "mscorlib",
        "netstandard", "mono.", "newtonsoft", "fastjson", "nlog", "log4net",
        "protobuf", "steamworks", "epoxy", "i2local", "customui", "harmony",
        "bepinex", "accessibility", "webconnection", "sqlite", "mysql", "audiotoolbox",
        "qsp", "fmod", "sdl", "openal", "openvr", "softpcg"
    )
    for pref in system_prefixes:
        if fn.startswith(pref):
            return False
    return True

# ── Compiled regexes ────────────────────────────────────────────────────────
_GUID_RE          = re.compile(r'^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$', re.I)
_VERSION_RE       = re.compile(r'^v?\d+(\.\d+){2,}([.\-+]\w+)?$')
_HEX_HASH_RE      = re.compile(r'^[0-9a-fA-F]{12,}$')
_URL_RE           = re.compile(r'https?://|www\.')
_PATH_RE          = re.compile(r'[/\\]')
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


def _is_game_text(text: str) -> bool:
    t = text.strip()

    # ── Базовые проверки ────────────────────────────────────────────────────
    if len(t) < 2:
        return False
    if not any(c.isalpha() for c in t):
        return False

    # ── Жесткие исключения ──────────────────────────────────────────────────
    if _GUID_RE.match(t):           return False
    if _VERSION_RE.match(t):        return False
    if _HEX_HASH_RE.match(t):       return False
    if _URL_RE.search(t):           return False
    if _PATH_RE.search(t):          return False
    if _EXT_RE.search(t):           return False
    if _PLACEHOLDER_RE.match(t):    return False
    if _LOG_TAG_RE.match(t):        return False

    # ── Быстрый пропуск: очевидно человеческий текст ───────────────────────
    if ' ' in t or '\n' in t:       return True   # многословный / диалог
    if t.lower() in _KNOWN_UI:      return True   # известная UI-метка

    # ── Одно слово: усиленный фильтр ────────────────────────────────────────
    if _CODE_WORD_RE.match(t):      return False
    if t.lower() in _KNOWN_CODE:    return False

    # Все-капсовое короткое слово без подчеркиваний -> кнопка UI (PLAY, EXIT)
    if t.isupper() and len(t) <= 20 and '_' not in t:
        return True

    # Title-case слово нормальной длины -> название предмета/локации
    if t[0].isupper() and t[1:].islower() and 4 <= len(t) <= 30:
        return True

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
    """
    t = text.strip()

    # ── Base checks ────────────────────────────────────────────────────────
    if len(t) < 3:
        return False
    if not any(c.isalpha() for c in t):
        return False

    if _GUID_RE.match(t):              return False
    if _VERSION_RE.match(t):           return False
    if _VERSION_RAW_RE.match(t):       return False
    if _HEX_HASH_RE.match(t):          return False
    if _URL_RE.search(t):              return False
    if _PATH_RE.search(t):             return False
    if _EXT_RE.search(t):              return False
    if _LOG_TAG_RE.match(t):           return False

    low = t.lower()

    # ── Hard rules: underscore = internal identifier, never dialogue ────────
    if '_' in t:
        return False

    # ── Hard rule: "Unity" anywhere = engine internal ───────────────────────
    if 'unity' in low:
        return False

    # ── Hard rule: assembly references ("UnityEngine...", "Version=0.0.0.0") ─
    if ', assembly-' in low or ', unity.' in low or ', unityengine' in low:
        return False
    if 'version=0.0.0.0' in low or 'culture=neutral' in low:
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
    if ' ' in t or '\n' in t:              return True   # multi-word → likely dialogue/UI
    if t.lower() in _KNOWN_UI:             return True

    # ALL CAPS button label
    if t.isupper() and len(t) <= 20 and '_' not in t:
        return True

    # Title-case normal word
    if t[0].isupper() and t[1:].islower() and 3 <= len(t) <= 30:
        return True

    return False


def _extract_length_prefixed(raw: bytes) -> list[str]:
    """Extract all length-prefixed UTF-8 strings from Unity serialized bytes."""
    strings = []
    i = 0
    while i + 4 < len(raw):
        slen = struct.unpack_from('<I', raw, i)[0]
        if 2 <= slen <= 5000 and i + 4 + slen <= len(raw):
            chunk = raw[i+4:i+4+slen]
            try:
                s = chunk.decode('utf-8')
                if s.isprintable() and len(s) > 0:
                    strings.append(s)
            except (UnicodeDecodeError, ValueError):
                pass
        i += 1
    return strings


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


class UnityParser(BaseParser):
    engine = "unity"

    def __init__(self) -> None:
        super().__init__()
        self._dll_extract_cache: dict[str, list[dict]] = {}
        self._managed_dir_cache: dict[str, str | None] = {}
        self._generator_cache: dict[str, Any] = {}
        self._font_bytes_cache: dict[str, bytes | None] = {}

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
        results = []
        # 1. Extract DLLs
        results.extend(self._extract_dlls(root, sub_paths))
        # 2. Extract assets/YAML UI-text
        results.extend(self._extract_assets(root, sub_paths))
        # 3. Extract Addressables localization StringTables
        results.extend(self._extract_localization(root, sub_paths))
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

    def _extract_assets(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results = []
        compiled_files, source_files = self._scan_asset_files(root, sub_paths)

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
                        except Exception:
                            failed_count += 1
            except Exception as e:
                print(f"Error parsing assets file {fpath}: {e}", file=sys.stderr)

            if typetree_found == 0 and failed_count > 0:
                print(f"Typetree failed for all {failed_count} objects in {os.path.basename(fpath)}, trying length-prefix fallback.", file=sys.stderr)
                results.extend(self._extract_raw_fallback(fpath, root))
            elif failed_count > 0:
                print(f"Warning: Failed to extract {failed_count} objects in compiled asset {fpath}.", file=sys.stderr)

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
                    line_idx += 1
            except Exception as e:
                print(f"Error parsing source file {fpath}: {e}", file=sys.stderr)

        return results

    def _extract_raw_fallback(self, fpath: str, root: str) -> list[TranslationString]:
        """Fallback extraction via length-prefix scanning of raw MonoBehaviour bytes.

        Used when typetree fails for ALL objects in an asset file (missing DLL,
        unknown type tree, etc.). Scans raw bytes for Unity's int32-length-prefixed
        UTF-8 strings and applies a stricter filter than the typetree path.
        Provides context from sibling strings in the same blob (neighboring
        strings from the same MonoBehaviour are usually related — a character
        name near its dialogue, a quest title near its description).
        """
        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
        results = []
        seen_texts: set[str] = set()

        try:
            import UnityPy
            env = UnityPy.load(fpath)
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                raw = obj.get_raw_data()
                if len(raw) < 20:
                    continue

                # Phase 1: collect all valid strings from this blob
                blob_strings: list[str] = []
                for s in _extract_length_prefixed(raw):
                    t = s.strip()
                    if t and t not in seen_texts and _is_game_text_raw(t):
                        blob_strings.append(t)

                if not blob_strings:
                    continue

                # Phase 2: detect possible speaker names in this blob
                # Names: 1-2 words, ≤20 chars, starts uppercase, not a common UI word
                _UI_NAMES = frozenset({
                    "play", "quit", "load", "save", "back", "next", "yes", "no",
                    "ok", "all", "none", "video", "audio", "game", "settings",
                    "gallery", "music", "loading", "credits", "options", "resume",
                    "start", "new", "continue", "delete", "accept", "cancel",
                    "confirm", "close", "help", "return", "pause", "skip",
                    "adult", "content", "disclaimer",
                })
                possible_names: list[str] = []
                for s in blob_strings:
                    words = s.split()
                    if (1 <= len(words) <= 2 and
                            len(s) <= 20 and
                            s[0].isupper() and
                            s.lower() not in _UI_NAMES and
                            not any(c in s for c in '?!.:;,()[]{}') and
                            not s.isupper() and
                            not re.match(r'^[A-Z][a-z]+$', s)):  # single common word like "Video"
                        possible_names.append(s)

                # Phase 3: emit each string with full context
                for idx, t in enumerate(blob_strings):
                    seen_texts.add(t)

                    ctx_parts = [f"File: {os.path.basename(fpath)}"]

                    # Speaker hint (only for dialogue blobs)
                    if possible_names and len(possible_names) <= 3:
                        ctx_parts.append(f"Speakers in this block: {', '.join(possible_names)}")

                    # Sibling context: only for dialogue-like strings
                    # (multi-word, >10 chars). UI buttons like "Play", "SOLD OUT"
                    # don't need context — LLM knows what they are.
                    siblings = [s for s in blob_strings if s != t]
                    if siblings and ' ' in t and len(t) > 10:
                        capped = []
                        total = 0
                        for s in siblings:
                            if len(capped) >= 15:
                                break
                            addition = len(s) + 2
                            if total + addition > 800:
                                break
                            capped.append(s)
                            total += addition
                        ctx_parts.append(f"Other strings in this block: {'; '.join(capped)}")

                    path = ["RawFallback", obj.type.name, str(obj.path_id), "length_prefix"]
                    results.append(self._mk(rel_path, path, t, ", ".join(ctx_parts)))
        except Exception as e:
            print(f"Error in raw fallback for {fpath}: {e}", file=sys.stderr)

        return results

    def _inject_raw_fallback(self, fpath: str, root: str, translations: dict[str, str],
                             env: Any = None, font_bytes: bytes | None = None,
                             target_lang: str | None = None) -> int:
        """Inject translations AND fonts via pure binary patching — no UnityPy.save().

        Patches both strings and fonts in the same pass, writing the file
        once at the end. This avoids UnityPy's re-serialization.
        """
        rel_path = os.path.relpath(fpath, root).replace("\\", "/")
        written = 0

        try:
            with open(fpath, "rb") as f:
                file_bytes = bytearray(f.read())

            patches: list[tuple[int, int, bytes]] = []

            if env is None:
                import UnityPy
                env = UnityPy.load(fpath)

            # Font patching via raw bytes — replace PPtr references to non-Cyrillic fonts
            # with LiberationSans (which supports Cyrillic). This is a SAFE size-preserving
            # change: we only swap 8-byte path_ids in PPtrs, no file size change.
            is_non_latin = target_lang and target_lang.lower() in ("ru", "zh", "ja", "ko", "ar", "he", "el", "th", "uk", "be", "bg", "sr")
            if is_non_latin:
                # Find LiberationSans path_id (our target)
                liberation_pid = None
                for obj in env.objects:
                    if obj.type.name == "Font":
                        try:
                            data = obj.read()
                            if data.m_Name == "LiberationSans":
                                liberation_pid = obj.path_id
                                break
                        except:
                            pass

                if liberation_pid:
                    # Find all Font objects that DON'T support Cyrillic
                    # and build a map: old_pid -> liberation_pid
                    non_cyrillic_fonts = set()
                    for obj in env.objects:
                        if obj.type.name == "Font":
                            try:
                                data = obj.read()
                                if data.m_Name != "LiberationSans":
                                    non_cyrillic_fonts.add(obj.path_id)
                            except:
                                pass

                    if non_cyrillic_fonts:
                        # Scan entire file for PPtr references to non-Cyrillic fonts
                        # PPtr = int32 FileID (0) + int64 PathID
                        for old_pid in non_cyrillic_fonts:
                            old_pptr = struct.pack('<Iq', 0, old_pid)
                            new_pptr = struct.pack('<Iq', 0, liberation_pid)
                            idx = 0
                            while True:
                                idx = file_bytes.find(old_pptr, idx)
                                if idx < 0:
                                    break
                                patches.append((idx, idx + 12, new_pptr))
                                idx += 12

            # String patching via raw bytes
            for obj in env.objects:
                if obj.type.name != "MonoBehaviour":
                    continue
                raw = obj.get_raw_data()
                if len(raw) < 20:
                    continue

                # Use the object's EXACT byte offset in the file — no search
                raw_offset = obj.byte_start

                # Scan raw blob for strings
                j = 0
                while j + 4 < len(raw):
                    slen = struct.unpack_from('<I', raw, j)[0]
                    if 2 <= slen <= 5000 and j + 4 + slen <= len(raw):
                        chunk = raw[j+4:j+4+slen]
                        try:
                            s = chunk.decode('utf-8')
                            if s.isprintable() and len(s) > 0:
                                t = s.strip()
                                if t and '_' not in t and _is_game_text_raw(t):
                                    path = ["RawFallback", obj.type.name, str(obj.path_id), "length_prefix"]
                                    sid = make_id(self.engine, rel_path, path, t)
                                    if sid in translations:
                                        new_text = translations[sid]
                                        new_bytes = new_text.encode('utf-8')

                                        # Only replace string bytes, keep original length prefix.
                                        # Truncate translation to original slen.
                                        max_str_len = min(len(new_bytes), slen)
                                        new_str = new_bytes[:max_str_len]

                                        file_start = raw_offset + j
                                        file_end = raw_offset + j + 4 + slen
                                        new_blob = struct.pack('<I', slen) + new_str
                                        if len(new_blob) < (4 + slen):
                                            new_blob += b'\x00' * ((4 + slen) - len(new_blob))

                                        patches.append((file_start, file_end, new_blob))
                        except (UnicodeDecodeError, ValueError):
                            pass
                    j += 1

            if not patches:
                return 0

            # Phase 2: apply patches in reverse order
            patches.sort(key=lambda p: p[0], reverse=True)
            for start, end, new_blob in patches:
                file_bytes[start:end] = new_blob

            # Phase 3: write patched file
            self.backup_file(root, fpath)
            with open(fpath, "wb") as f:
                f.write(file_bytes)

            written = len(patches)

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
        written = 0
        # 1. Inject DLLs
        written += self._inject_dlls(root, translations, target_lang, sub_paths)
        # 2. Inject Assets & YAML
        written += self._inject_assets(root, translations, target_lang, sub_paths)
        # 3. Inject Addressables localization StringTables
        written += self._inject_localization(root, translations, target_lang, sub_paths)
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

    def _inject_assets(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0
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

                is_non_latin = target_lang and target_lang.lower() in ("ru", "zh", "ja", "ko", "ar", "he", "el", "th", "uk", "be", "bg", "sr")

                # Font PPtr replacement: swap non-Cyrillic font references to LiberationSans
                # This is a SAFE size-preserving change (only 12 bytes per PPtr).
                if is_non_latin:
                    self._replace_font_pptrs(env, fpath)

                if typetree_found == 0 and failed_count > 0:
                    raw_written = self._inject_raw_fallback(fpath, root, translations,
                                                            env=env, font_bytes=font_bytes,
                                                            target_lang=target_lang)
                    if raw_written > 0:
                        written += raw_written

            except Exception as e:
                print(f"Error injecting into assets file {fpath}: {e}", file=sys.stderr)

            if typetree_found == 0 and failed_count > 0:
                pass  # already handled above
            elif failed_count > 0:
                print(f"Warning: Failed to inject {failed_count} objects in compiled asset {fpath}.", file=sys.stderr)

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
                    line_idx += 1

                if changed:
                    lines = [ln for ln in lines if ln is not None]
                    self.backup_file(root, fpath)
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write("\n".join(lines))
            except Exception as e:
                print(f"Error injecting into source file {fpath}: {e}", file=sys.stderr)

        return written

    def _replace_font_pptrs(self, env: Any, fpath: str) -> None:
        """Replace PPtr references to non-Cyrillic fonts with LiberationSans.

        This is a SAFE size-preserving change: we only swap 8-byte path_ids
        in PPtrs. No file size change, no structure corruption.
        """
        try:
            # Find LiberationSans path_id
            liberation_pid = None
            for obj in env.objects:
                if obj.type.name == "Font":
                    try:
                        data = obj.read()
                        if data.m_Name == "LiberationSans":
                            liberation_pid = obj.path_id
                            break
                    except:
                        pass

            if not liberation_pid:
                return

            # Find all Font path_ids that aren't LiberationSans
            non_cyrillic_pids = set()
            for obj in env.objects:
                if obj.type.name == "Font":
                    try:
                        data = obj.read()
                        if data.m_Name != "LiberationSans":
                            non_cyrillic_pids.add(obj.path_id)
                    except:
                        pass

            if not non_cyrillic_pids:
                return

            # Read the file and replace all PPtr references
            with open(fpath, "rb") as f:
                file_bytes = bytearray(f.read())

            replaced = 0
            for old_pid in non_cyrillic_pids:
                old_pptr = struct.pack("<Iq", 0, old_pid)
                new_pptr = struct.pack("<Iq", 0, liberation_pid)
                idx = 0
                while True:
                    idx = file_bytes.find(old_pptr, idx)
                    if idx < 0:
                        break
                    file_bytes[idx:idx + 12] = new_pptr
                    replaced += 1
                    idx += 12

            if replaced > 0:
                with open(fpath, "wb") as f:
                    f.write(file_bytes)
                print(f"Replaced {replaced} font PPtrs in {os.path.basename(fpath)}", file=sys.stderr)
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
