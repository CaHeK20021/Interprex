"""Parser for i18n formats (Stardew Valley i18n JSON and RimWorld Languages XML)."""

from __future__ import annotations

import os
import json
import xml.etree.ElementTree as ET
from .base import BaseParser, TranslationString, make_id

# Mappings from user-selected languages (codes or full names) to target structures.
RIMWORLD_LANGS = {
    "russian": "Russian (Русский)",
    "ru": "Russian (Русский)",
    "german": "German (Deutsch)",
    "de": "German (Deutsch)",
    "spanish": "Spanish (Español)",
    "es": "Spanish (Español)",
    "french": "French (Français)",
    "fr": "French (Français)",
    "italian": "Italian (Italiano)",
    "it": "Italian (Italiano)",
    "chinese": "ChineseSimplified (简体中文)",
    "chinese (simplified)": "ChineseSimplified (简体中文)",
    "chinesesimplified": "ChineseSimplified (简体中文)",
    "zh": "ChineseSimplified (简体中文)",
    "chinesetraditional": "ChineseTraditional (繁體中文)",
    "japanese": "Japanese (日本語)",
    "ja": "Japanese (日本語)",
    "korean": "Korean (한국어)",
    "ko": "Korean (한국어)",
    "polish": "Polish (Polski)",
    "pl": "Polish (Polski)",
    "portuguese": "Portuguese (Brazilian) (Português (Brasil))",
    "portuguese (brazil)": "Portuguese (Brazilian) (Português (Brasil))",
    "portuguesebrazilian": "Portuguese (Brazilian) (Português (Brasil))",
    "pt": "Portuguese (Brazilian) (Português (Brasil))",
    "turkish": "Turkish (Türkçe)",
    "tr": "Turkish (Türkçe)",
    "ukrainian": "Ukrainian (Українська)",
    "uk": "Ukrainian (Українська)",
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

        # 2. RimWorld Languages
        rimworld_english = os.path.join(root, "Languages", "English")
        if os.path.isdir(rimworld_english):
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
                        data = json.load(f)
                    if isinstance(data, dict):
                        for path, val in flatten_json(data):
                            results.append(self._mk(rel_path, path, val, "Stardew Valley i18n"))
                except Exception as e:
                    print(f"Error reading Stardew i18n default.json: {e}")

            # 2. Extract RimWorld XMLs
            rimworld_english = os.path.join(base_path, "Languages", "English")
            if os.path.isdir(rimworld_english):
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

            # 2. Inject RimWorld XMLs
            rimworld_english = os.path.join(base_path, "Languages", "English")
            if os.path.isdir(rimworld_english):
                lang_folder = get_rimworld_folder(target_lang)

                for dirpath, _, filenames in os.walk(rimworld_english):
                    for filename in filenames:
                        if filename.endswith(".xml"):
                            source_abspath = os.path.join(dirpath, filename)
                            source_rel = os.path.relpath(source_abspath, root).replace("\\", "/")

                            # Compute target path
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
