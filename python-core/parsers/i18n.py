"""Parser for i18n formats (Stardew Valley i18n JSON and RimWorld Languages XML)."""

from __future__ import annotations

import os
import json
import xml.etree.ElementTree as ET
from .base import BaseParser, TranslationString, make_id

# Mappings from user-selected languages (codes or full names) to RimWorld
# Languages/ folder names. These MUST match the base game's .tar names
# (Data/Core/Languages/<name>.tar) — the engine loads by exact folder name.
RIMWORLD_LANGS = {
    "russian": "Russian (\u0420\u0443\u0441\u0441\u043a\u0438\u0439)",
    "ru": "Russian (\u0420\u0443\u0441\u0441\u043a\u0438\u0439)",
    "german": "German (Deutsch)",
    "de": "German (Deutsch)",
    "spanish": "Spanish (Espa\u00f1ol(Castellano))",
    "es": "Spanish (Espa\u00f1ol(Castellano))",
    "french": "French (Fran\u00e7ais)",
    "fr": "French (Fran\u00e7ais)",
    "italian": "Italian (Italiano)",
    "it": "Italian (Italiano)",
    "chinese": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinese (simplified)": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinesesimplified": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "zh": "ChineseSimplified (\u7b80\u4f53\u4e2d\u6587)",
    "chinesetraditional": "ChineseTraditional (\u7e41\u9ad4\u4e2d\u6587)",
    "japanese": "Japanese (\u65e5\u672c\u8a9e)",
    "ja": "Japanese (\u65e5\u672c\u8a9e)",
    "korean": "Korean (\ud55c\uad6d\uc5b4)",
    "ko": "Korean (\ud55c\uad6d\uc5b4)",
    "polish": "Polish (Polski)",
    "pl": "Polish (Polski)",
    "portuguese": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "portuguese (brazil)": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "portuguesebrazilian": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "pt": "PortugueseBrazilian (Portugu\u00eas Brasileiro)",
    "turkish": "Turkish (T\u00fcrk\u00e7e)",
    "tr": "Turkish (T\u00fcrk\u00e7e)",
    "ukrainian": "Ukrainian (\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430)",
    "uk": "Ukrainian (\u0423\u043a\u0440\u0430\u0457\u043d\u0441\u044c\u043a\u0430)",
    "czech": "Czech (\u010ce\u0161tina)",
    "cs": "Czech (\u010ce\u0161tina)",
    "dutch": "Dutch (Nederlands)",
    "nl": "Dutch (Nederlands)",
    "danish": "Danish (Dansk)",
    "da": "Danish (Dansk)",
    "finnish": "Finnish (Suomi)",
    "fi": "Finnish (Suomi)",
    "hungarian": "Hungarian (Magyar)",
    "hu": "Hungarian (Magyar)",
    "spanishlatin": "SpanishLatin (Espa\u00f1ol(Latinoam\u00e9rica))",
    "spanish (latin american)": "SpanishLatin (Espa\u00f1ol(Latinoam\u00e9rica))",
}

STARDEW_LANGS = {
    "russian": "ru",
    "ru": "ru",
    "german": "de",
    "de": "de",
    "spanish": "es",
    "es": "es",
    "french": "fr",
    "fr": "fr",
    "italian": "it",
    "it": "it",
    "chinese": "zh",
    "chinese (simplified)": "zh",
    "chinesesimplified": "zh",
    "chinesetraditional": "zh",
    "zh": "zh",
    "japanese": "ja",
    "ja": "ja",
    "korean": "ko",
    "ko": "ko",
    "polish": "pl",
    "pl": "pl",
    "portuguese": "pt",
    "portuguese (brazil)": "pt",
    "portuguesebrazilian": "pt",
    "pt": "pt",
    "turkish": "tr",
    "tr": "tr",
    "ukrainian": "uk",
    "uk": "uk",
}


def get_rimworld_folder(target: str) -> str:
    clean = target.strip().lower()
    return RIMWORLD_LANGS.get(clean, target)


_SKIP_DIRS = frozenset({
    "About", "Source", "Defs", "Assemblies", "Textures",
    "Patches", "Sound", "Meshes", "UI", "bin", "obj",
    ".git", ".vs", "node_modules", "venv", ".interprex_backups",
})


def _find_rimworld_lang_dirs(root: str) -> list[tuple[str, str]]:
    """Find all Languages/<lang> directories under root (including inside
    version subfolders like 1.5/, 1.6/). Returns list of (lang_dir_path, lang_name)
    e.g. (".1.6/Languages/English", "English")."""
    results: list[tuple[str, str]] = []

    def _scan_languages(langs_dir: str) -> None:
        try:
            for entry in os.scandir(langs_dir):
                if entry.is_dir():
                    results.append((entry.path, entry.name))
        except OSError:
            pass

    # 1. Root level
    root_langs = os.path.join(root, "Languages")
    if os.path.isdir(root_langs):
        _scan_languages(root_langs)

    # 2. One level of subdirs (version folders: 1.0, 1.5, 1.6 etc.)
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                langs = os.path.join(entry.path, "Languages")
                if os.path.isdir(langs):
                    _scan_languages(langs)
    except OSError:
        pass

    return results


def _find_rimworld_versions(root: str) -> list[str]:
    """Find available RimWorld version folders (1.0-1.6+). Returns sorted
    list newest-first, e.g. ["1.6", "1.5", "1.4"]. Empty if no versions
    found (mod uses root-level Languages/ only)."""
    import re
    versions: list[tuple[float, str]] = []
    try:
        for entry in os.scandir(root):
            if entry.is_dir() and entry.name not in _SKIP_DIRS:
                langs = os.path.join(entry.path, "Languages")
                if os.path.isdir(langs):
                    m = re.match(r'^(\d+\.\d+)$', entry.name)
                    if m:
                        versions.append((float(m.group(1)), entry.name))
    except OSError:
        pass
    versions.sort(key=lambda v: -v[0])
    return [v[1] for v in versions]


def _rimworld_has_lang(root: str, target_lang: str) -> bool:
    """True if Languages/<target_lang>/ (short or long form) exists in the mod.
    Matches both directions: mod has long form and we check short, or vice versa."""
    target = RIMWORLD_LANGS.get(target_lang.strip().lower(), target_lang)
    # Extract the short code before "(" for bidirectional matching
    target_short = target.split(" (")[0] if " (" in target else target
    for _, name in _find_rimworld_lang_dirs(root):
        if name == target or name == target_short:
            return True
        name_short = name.split(" (")[0] if " (" in name else name
        if name_short == target_short:
            return True
    return False


def _find_rimworld_english_dirs(root: str) -> list[str]:
    """Find Languages/English directories under root. If version subfolders
    exist (1.5/, 1.6/), returns ONLY the latest version's dirs.
    Falls back to root-level Languages/English if no versioned dirs found."""
    all_langs = _find_rimworld_lang_dirs(root)
    english_dirs = [(path, name) for path, name in all_langs if name == "English"]

    # Group by whether they're in a version subfolder
    import re
    versioned: list[tuple[float, str]] = []
    root_dirs: list[str] = []
    for path, _ in english_dirs:
        # Check if path contains a version folder like /1.6/Languages/English
        parts = os.path.normpath(path).split(os.sep)
        found_version = False
        for part in parts:
            m = re.match(r'^(\d+\.\d+)$', part)
            if m:
                versioned.append((float(m.group(1)), path))
                found_version = True
                break
        if not found_version:
            root_dirs.append(path)

    if versioned:
        # Return only the latest version
        versioned.sort(key=lambda v: -v[0])
        latest = versioned[0][0]
        return [p for ver, p in versioned if ver == latest]

    # No versioned dirs — use root-level
    seen: set[str] = set()
    unique: list[str] = []
    for p in root_dirs:
        real = os.path.realpath(p)
        if real not in seen:
            seen.add(real)
            unique.append(p)
    return unique


def get_stardew_code(target: str) -> str:
    clean = target.strip().lower()
    if clean in STARDEW_LANGS:
        return STARDEW_LANGS[clean]
    # Fallback: first two characters
    return clean[:2]


def flatten_json(d: dict, current_path: list[str] = None) -> list[tuple[list[str], str]]:
    if current_path is None:
        current_path = []
    items = []
    for k, v in d.items():
        new_path = current_path + [k]
        if isinstance(v, dict):
            items.extend(flatten_json(v, new_path))
        elif isinstance(v, str):
            items.append((new_path, v))
    return items


def set_by_path(d: dict, path: list[str], value: str) -> None:
    for step in path[:-1]:
        d = d.setdefault(step, {})
    d[path[-1]] = value


def _strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments + trailing commas from JSON text.
    Stardew Valley i18n files commonly use these non-standard features."""
    import re
    # Remove single-line // comments (but not inside strings)
    result = []
    in_string = False
    escape = False
    i = 0
    while i < len(text):
        c = text[i]
        if escape:
            result.append(c)
            escape = False
            i += 1
            continue
        if c == '\\' and in_string:
            result.append(c)
            escape = True
            i += 1
            continue
        if c == '"' and not escape:
            in_string = not in_string
            result.append(c)
            i += 1
            continue
        if not in_string and c == '/' and i + 1 < len(text) and text[i + 1] == '/':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        if not in_string and c == '/' and i + 1 < len(text) and text[i + 1] == '*':
            i += 2
            while i + 1 < len(text) and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        result.append(c)
        i += 1
    text = ''.join(result)
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    return text


class I18nParser(BaseParser):
    engine = "i18n"

    def engine_prompt_addon(self) -> str:
        return (
            "LOCALIZATION FILE STRINGS: these strings come from a JSON/INI locale file "
            "and are displayed in menus, HUD, or system messages.\n"
            "FORMAT SPECIFIERS: preserve {0}, {1}, {player}, %s, %d and similar "
            "patterns EXACTLY — they are filled in at runtime.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is inside strings.\n"
            "TONE: use a neutral, professional register. Avoid overly literary style."
        )

    @staticmethod
    def detect(root: str) -> bool:
        # 1. Stardew Valley i18n
        stardew_default = os.path.join(root, "i18n", "default.json")
        if os.path.isfile(stardew_default):
            return True

        # 2. RimWorld Languages — any language, root or version subfolders
        if _find_rimworld_lang_dirs(root):
            return True

        return False

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results: list[TranslationString] = []

        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            # 1. Extract Stardew JSON
            stardew_default = os.path.join(base_path, "i18n", "default.json")
            if os.path.isfile(stardew_default):
                rel_path = os.path.relpath(stardew_default, root).replace("\\", "/")
                try:
                    with open(stardew_default, "r", encoding="utf-8-sig") as f:
                        raw = f.read()
                    data = json.loads(_strip_json_comments(raw))
                    if isinstance(data, dict):
                        for path, val in flatten_json(data):
                            results.append(self._mk(rel_path, path, val, "Stardew Valley i18n"))
                except Exception as e:
                    print(f"Error reading Stardew i18n default.json: {e}")

            # 2. Extract RimWorld XMLs — root + version subfolders
            rimworld_english_dirs = _find_rimworld_english_dirs(base_path)
            seen_ids: set[str] = set()
            for rimworld_english in rimworld_english_dirs:
                for dirpath, _, filenames in os.walk(rimworld_english):
                    for filename in filenames:
                        if filename.endswith(".xml"):
                            abspath = os.path.join(dirpath, filename)
                            rel_path = os.path.relpath(abspath, root).replace("\\", "/")
                            try:
                                tree = ET.parse(abspath)
                                root_el = tree.getroot()
                                for child in root_el:
                                    if isinstance(child.tag, str) and child.text is not None:
                                        original = child.text
                                        if not original.strip():
                                            continue
                                        sid = make_id(self.engine, rel_path, [child.tag], original)
                                        if sid in seen_ids:
                                            continue
                                        seen_ids.add(sid)
                                        results.append(self._mk(rel_path, [child.tag], original, f"RimWorld | {filename.replace('.xml', '')} | {child.tag}"))
                            except Exception as e:
                                print(f"Error reading RimWorld XML {filename}: {e}")

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0

        if not target_lang:
            target_lang = "Russian"

        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            # 1. Inject Stardew JSON
            stardew_default = os.path.join(base_path, "i18n", "default.json")
            if os.path.isfile(stardew_default):
                lang_code = get_stardew_code(target_lang)
                default_rel_path = os.path.relpath(stardew_default, root).replace("\\", "/")
                target_filepath = os.path.join(base_path, "i18n", f"{lang_code}.json")

                # Load original default JSON
                try:
                    with open(stardew_default, "r", encoding="utf-8-sig") as f:
                        default_data = json.load(f)
                except Exception as e:
                    print(f"Error loading Stardew default.json: {e}")
                    default_data = {}

                if isinstance(default_data, dict):
                    # Load existing target JSON if present
                    existing_target = {}
                    if os.path.isfile(target_filepath):
                        try:
                            with open(target_filepath, "r", encoding="utf-8-sig") as f:
                                existing_target = json.load(f)
                            if not isinstance(existing_target, dict):
                                existing_target = {}
                        except Exception:
                            pass

                    # Back up the target file if it already exists
                    if os.path.isfile(target_filepath):
                        self.backup_file(root, target_filepath)

                    # Merge translations
                    has_updates = False
                    for path, original in flatten_json(default_data):
                        sid = make_id(self.engine, default_rel_path, path, original)
                        if sid in translations:
                            set_by_path(existing_target, path, translations[sid])
                            written += 1
                            has_updates = True

                    if has_updates or os.path.isfile(target_filepath):
                        os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                        with open(target_filepath, "w", encoding="utf-8-sig") as f:
                            json.dump(existing_target, f, ensure_ascii=False, indent=2)

            # 2. Inject RimWorld XMLs — root + version subfolders
            rimworld_english_dirs = _find_rimworld_english_dirs(base_path)
            lang_folder = get_rimworld_folder(target_lang)

            for rimworld_english in rimworld_english_dirs:
                for dirpath, _, filenames in os.walk(rimworld_english):
                    for filename in filenames:
                        if filename.endswith(".xml"):
                            source_abspath = os.path.join(dirpath, filename)
                            source_rel = os.path.relpath(source_abspath, root).replace("\\", "/")

                            # Compute target path: replace Languages/English with Languages/<target>
                            idx = source_rel.find("Languages/English")
                            if idx != -1:
                                target_rel = source_rel[:idx] + "Languages/" + lang_folder + source_rel[idx + len("Languages/English"):]
                            else:
                                target_rel = source_rel.replace("Languages/English", "Languages/" + lang_folder)

                            target_filepath = os.path.join(root, target_rel)

                            try:
                                # Load original XML elements
                                source_tree = ET.parse(source_abspath)
                                source_root = source_tree.getroot()

                                # Check if we have any translations for this file
                                file_translations = {}
                                for child in source_root:
                                    if isinstance(child.tag, str) and child.text is not None:
                                        sid = make_id(self.engine, source_rel, [child.tag], child.text)
                                        if sid in translations:
                                            file_translations[child.tag] = translations[sid]

                                if not file_translations and not os.path.isfile(target_filepath):
                                    # Nothing to write and target doesn't exist, skip
                                    continue

                                # Load or create target XML
                                target_root = None
                                if os.path.isfile(target_filepath):
                                    try:
                                        # Backup before writing
                                        self.backup_file(root, target_filepath)
                                        target_tree = ET.parse(target_filepath)
                                        target_root = target_tree.getroot()
                                    except Exception:
                                        pass

                                if target_root is None:
                                    target_root = ET.Element(source_root.tag)

                                # Build maps of existing elements in target XML to update in place
                                target_elements = {el.tag: el for el in target_root if isinstance(el.tag, str)}

                                # Update or append elements
                                for tag, trans_val in file_translations.items():
                                    if tag in target_elements:
                                        target_elements[tag].text = trans_val
                                    else:
                                        new_el = ET.Element(tag)
                                        new_el.text = trans_val
                                        target_root.append(new_el)
                                        target_elements[tag] = new_el
                                    written += 1

                                # Format and save target XML using utf-8-sig encoding for disk output
                                os.makedirs(os.path.dirname(target_filepath), exist_ok=True)
                                ET.indent(target_root, space="  ")
                                xml_bytes = ET.tostring(target_root, encoding="utf-8", xml_declaration=True)
                                xml_str = xml_bytes.decode("utf-8")
                                with open(target_filepath, "w", encoding="utf-8-sig") as f:
                                    f.write(xml_str)

                            except Exception as e:
                                print(f"Error injecting RimWorld XML {filename}: {e}")

        return written
