"""Parser for Unity.

Extracts/injects code strings from C# DLL files (using DllEditor Mono.Cecil helper)
and UI-text strings from scenes, prefabs, assets, and Addressables localization StringTables.
"""

from __future__ import annotations

import os
import sys
import re
import json
import tempfile
import subprocess
from typing import Any
from collections.abc import Generator
from .base import BaseParser, TranslationString, make_id

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
            except ImportError:
                print("UnityPy is not installed. Skipping compiled assets.", file=sys.stderr)
                compiled_files = []

        for fpath in compiled_files:
            rel_path = os.path.relpath(fpath, root).replace("\\", "/")
            if "globalgamemanagers" in fpath.lower():
                continue

            failed_count = 0
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
                        except Exception:
                            failed_count += 1
            except Exception as e:
                print(f"Error parsing assets file {fpath}: {e}", file=sys.stderr)

            if failed_count > 0:
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
                if font_bytes and (is_non_latin or fonts_to_replace):
                    for obj in env.objects:
                        if obj.type.name == "Font":
                            data = obj.read()
                            should_replace = obj.path_id in fonts_to_replace or is_non_latin
                            if should_replace:
                                try:
                                    tree = data.read_typetree()
                                    if "m_FontData" in tree:
                                        tree["m_FontData"] = font_bytes
                                        data.save_typetree(tree)
                                        changed = True
                                except Exception:
                                    try:
                                        data.m_FontData = font_bytes
                                        data.save()
                                        changed = True
                                    except Exception:
                                        failed_count += 1

                if changed:
                    self.backup_file(root, fpath)
                    with open(fpath, "wb") as f:
                        f.write(env.file.save(packer="none"))

            except Exception as e:
                print(f"Error injecting into assets file {fpath}: {e}", file=sys.stderr)

            if failed_count > 0:
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
