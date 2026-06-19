"""RPG Maker MV / MZ parser.

Data lives in plain JSON under either `data/` (MZ) or `www/data/` (MV). The
bulk of player-facing text is in map event command lists. This first pass
covers the high-value cases:

  - 401  Show Text (each dialogue line)              parameters[0]  (string)
  - 405  Show Scrolling Text (each line)             parameters[0]  (string)
  - 102  Show Choices                                parameters[0]  (list[str])

`path` records the exact JSON location so inject() writes back to the same slot.
Extracting and injecting are symmetric: same traversal, read vs. write.
"""

from __future__ import annotations

import json
import os

from .base import BaseParser, TranslationString


def _load_json(fpath: str) -> object:
    """Load a JSON file, trying UTF-8 first then Shift-JIS (cp932).

    RPG Maker MV/MZ writes UTF-8, but older engines (XP/VX/VX Ace) use
    Shift-JIS. Falling back silently keeps those projects working without
    requiring the user to re-encode files.
    """
    try:
        with open(fpath, encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        with open(fpath, encoding="cp932") as f:
            return json.load(f)


# Command codes whose text we translate. 102's payload is a list of strings;
# 401/405 carry a single string. Kept together so extract/inject agree.
_LINE_CODES = (401, 405)
_CHOICE_CODE = 102

# Non-map database files that carry player-visible text. Each entry is
# (filename, list of (key_field, text_field)) — key_field is the field used in
# path so ids survive reorders, text_fields are the string columns to extract.
_DB_FILES: list[tuple[str, list[tuple[str, str]]]] = [
    ("Actors.json",       [("id", "name"), ("id", "nickname"), ("id", "profile")]),
    ("Classes.json",      [("id", "name")]),
    ("Skills.json",       [("id", "name"), ("id", "description"), ("id", "message1"), ("id", "message2")]),
    ("Items.json",        [("id", "name"), ("id", "description")]),
    ("Weapons.json",      [("id", "name"), ("id", "description")]),
    ("Armors.json",       [("id", "name"), ("id", "description")]),
    ("Enemies.json",      [("id", "name")]),
    ("States.json",       [("id", "name"), ("id", "message1"), ("id", "message2"),
                           ("id", "message3"), ("id", "message4")]),
    ("CommonEvents.json", []),  # special-cased: contains event command lists
    ("Troops.json",       []),  # special-cased: pages->list like map events
]


class RpgMakerParser(BaseParser):
    engine = "rpgmaker"

    def engine_prompt_addon(self) -> str:
        return (
            "RPG MAKER CONTROL CODES: the following sequences are engine commands — "
            "copy them into the translation EXACTLY, never translate or remove them:\n"
            "  \\V[n]  — variable value (a number supplied at runtime)\n"
            "  \\N[n]  — actor name\n"
            "  \\P[n]  — party member name\n"
            "  \\G     — currency symbol\n"
            "  \\C[n]  — text colour change\n"
            "  \\I[n]  — item icon\n"
            "  \\{    \\}  — font size increase / decrease\n"
            "  \\!  \\>  \\<  \\^  \\|  \\. — wait / instant / pause codes\n"
            "STYLE: item names, skill names, and status labels must be SHORT and punchy "
            "(they render in fixed table cells). Prefer a crisp noun over a verbose phrase."
        )

    # --- detection --------------------------------------------------------
    @staticmethod
    def detect(root: str) -> bool:
        for sub in ("data", os.path.join("www", "data")):
            if os.path.isfile(os.path.join(root, sub, "System.json")):
                return True
        return False

    @staticmethod
    def _data_dir(root: str) -> str:
        mz = os.path.join(root, "data")
        if os.path.isfile(os.path.join(mz, "System.json")):
            return mz
        return os.path.join(root, "www", "data")

    @staticmethod
    def _map_files(data_dir: str) -> list[str]:
        out = []
        for name in os.listdir(data_dir):
            # Map001.json ... MapNNN.json (skip MapInfos.json)
            if name.startswith("Map") and name.endswith(".json") and name != "MapInfos.json":
                stem = name[3:-5]
                if stem.isdigit():
                    out.append(name)
        return sorted(out)

    # --- System.json (game title, currency, UI terms) --------------------
    def _extract_system(self, data_dir: str, rel_base: str) -> list[TranslationString]:
        """Extract player-visible strings from System.json.

        Covers: gameTitle, currencyUnit, terms.basic / commands / params
        (array entries) and terms.messages (dict values).
        """
        fpath = os.path.join(data_dir, "System.json")
        if not os.path.isfile(fpath):
            return []
        data = _load_json(fpath)
        file_rel = f"{rel_base}/System.json"
        results: list[TranslationString] = []

        for field in ("gameTitle", "currencyUnit"):
            val = data.get(field, "")
            if isinstance(val, str) and val.strip():
                results.append(self._mk(file_rel, [field], val))

        # Top-level type-name arrays (shown in equipment/skill menus)
        for arr_field in ("weaponTypes", "armorTypes", "skillTypes", "equipTypes", "elements"):
            for i, item in enumerate(data.get(arr_field) or []):
                if isinstance(item, str) and item.strip():
                    results.append(self._mk(file_rel, [arr_field, str(i)], item))

        terms = data.get("terms") or {}
        for arr_field in ("basic", "commands", "params"):
            for i, item in enumerate(terms.get(arr_field) or []):
                if isinstance(item, str) and item.strip():
                    results.append(self._mk(file_rel, ["terms", arr_field, str(i)], item))
        for key, val in (terms.get("messages") or {}).items():
            if isinstance(val, str) and val.strip():
                results.append(self._mk(file_rel, ["terms", "messages", key], val))

        return results

    def _inject_system(self, data_dir: str, rel_base: str,
                       translations: dict[str, str]) -> int:
        fpath = os.path.join(data_dir, "System.json")
        if not os.path.isfile(fpath):
            return 0
        data = _load_json(fpath)
        file_rel = f"{rel_base}/System.json"
        written = 0
        dirty = False

        for field in ("gameTitle", "currencyUnit"):
            val = data.get(field, "")
            if isinstance(val, str) and val.strip():
                sid = self._id(file_rel, [field], val)
                if sid in translations:
                    data[field] = translations[sid]
                    dirty = True
                    written += 1

        for arr_field in ("weaponTypes", "armorTypes", "skillTypes", "equipTypes", "elements"):
            arr = data.get(arr_field) or []
            for i, item in enumerate(arr):
                if isinstance(item, str) and item.strip():
                    sid = self._id(file_rel, [arr_field, str(i)], item)
                    if sid in translations:
                        arr[i] = translations[sid]
                        dirty = True
                        written += 1

        terms = data.get("terms") or {}
        for arr_field in ("basic", "commands", "params"):
            arr = terms.get(arr_field) or []
            for i, item in enumerate(arr):
                if isinstance(item, str) and item.strip():
                    sid = self._id(file_rel, ["terms", arr_field, str(i)], item)
                    if sid in translations:
                        arr[i] = translations[sid]
                        dirty = True
                        written += 1
        msgs = terms.get("messages") or {}
        for key, val in list(msgs.items()):
            if isinstance(val, str) and val.strip():
                sid = self._id(file_rel, ["terms", "messages", key], val)
                if sid in translations:
                    msgs[key] = translations[sid]
                    dirty = True
                    written += 1

        if dirty:
            self.backup_file(self._current_root, fpath)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return written

    # --- MapInfos.json (location names shown on-screen) ------------------
    def _extract_map_infos(self, data_dir: str, rel_base: str) -> list[TranslationString]:
        """Extract map names from MapInfos.json (shown when entering areas)."""
        fpath = os.path.join(data_dir, "MapInfos.json")
        if not os.path.isfile(fpath):
            return []
        data = _load_json(fpath)
        file_rel = f"{rel_base}/MapInfos.json"
        results: list[TranslationString] = []
        for item in data or []:
            if not item:
                continue
            name = item.get("name", "")
            if isinstance(name, str) and name.strip():
                results.append(
                    self._mk(file_rel, [str(item.get("id", 0)), "name"], name))
        return results

    def _inject_map_infos(self, data_dir: str, rel_base: str,
                          translations: dict[str, str]) -> int:
        fpath = os.path.join(data_dir, "MapInfos.json")
        if not os.path.isfile(fpath):
            return 0
        data = _load_json(fpath)
        file_rel = f"{rel_base}/MapInfos.json"
        written = 0
        dirty = False
        for item in data or []:
            if not item:
                continue
            name = item.get("name", "")
            if isinstance(name, str) and name.strip():
                sid = self._id(file_rel, [str(item.get("id", 0)), "name"], name)
                if sid in translations:
                    item["name"] = translations[sid]
                    dirty = True
                    written += 1
        if dirty:
            self.backup_file(self._current_root, fpath)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return written

    # --- db file helpers --------------------------------------------------
    def _extract_db(self, data_dir: str, fname: str,
                    fields: list[tuple[str, str]], rel_base: str) -> list[TranslationString]:
        """Extract name/description/etc. fields from a flat-array DB JSON file."""
        fpath = os.path.join(data_dir, fname)
        if not os.path.isfile(fpath):
            return []
        data = _load_json(fpath)
        file_rel = f"{rel_base}/{fname}"
        results: list[TranslationString] = []
        for item in data or []:
            if not item:
                continue
            item_id = str(item.get("id", ""))
            for _key_field, text_field in fields:
                text = item.get(text_field, "")
                if isinstance(text, str) and text.strip():
                    results.append(self._mk(file_rel, [item_id, text_field], text))
        return results

    def _inject_db(self, data_dir: str, fname: str,
                   fields: list[tuple[str, str]], rel_base: str,
                   translations: dict[str, str]) -> int:
        """Write translations back into a flat-array DB JSON file."""
        fpath = os.path.join(data_dir, fname)
        if not os.path.isfile(fpath):
            return 0
        data = _load_json(fpath)
        file_rel = f"{rel_base}/{fname}"
        written = 0
        dirty = False
        for item in data or []:
            if not item:
                continue
            item_id = str(item.get("id", ""))
            for _key_field, text_field in fields:
                text = item.get(text_field, "")
                if not isinstance(text, str) or not text.strip():
                    continue
                sid = self._id(file_rel, [item_id, text_field], text)
                if sid in translations:
                    item[text_field] = translations[sid]
                    dirty = True
                    written += 1
        if dirty:
            self.backup_file(self._current_root, fpath)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return written

    def _extract_event_list(self, data_dir: str, fname: str, rel_base: str) -> list[TranslationString]:
        """Extract dialogue from files with top-level event command lists
        (CommonEvents.json, Troops.json)."""
        fpath = os.path.join(data_dir, fname)
        if not os.path.isfile(fpath):
            return []
        data = _load_json(fpath)
        file_rel = f"{rel_base}/{fname}"
        results: list[TranslationString] = []
        for ev_i, event in enumerate(data or []):
            if not event:
                continue
            # CommonEvents: list is direct; Troops: pages[].list
            pages = event.get("pages") or [None]
            for pg_i, page in enumerate(pages):
                cmd_list = (page.get("list") if page else None) or event.get("list") or []
                for cmd_i, cmd in enumerate(cmd_list):
                    code = cmd.get("code")
                    params = cmd.get("parameters") or []
                    base_path = [str(ev_i), str(pg_i), "list", str(cmd_i), "parameters"]
                    if code in _LINE_CODES and params and isinstance(params[0], str):
                        if params[0].strip():
                            results.append(self._mk(file_rel, base_path + ["0"], params[0]))
                    elif code == _CHOICE_CODE and params and isinstance(params[0], list):
                        for ch_i, choice in enumerate(params[0]):
                            if isinstance(choice, str) and choice.strip():
                                results.append(
                                    self._mk(file_rel, base_path + ["0", str(ch_i)], choice))
        return results

    def _inject_event_list(self, data_dir: str, fname: str, rel_base: str,
                           translations: dict[str, str]) -> int:
        """Write translations back into CommonEvents/Troops-style files."""
        fpath = os.path.join(data_dir, fname)
        if not os.path.isfile(fpath):
            return 0
        data = _load_json(fpath)
        file_rel = f"{rel_base}/{fname}"
        written = 0
        dirty = False
        for ev_i, event in enumerate(data or []):
            if not event:
                continue
            pages = event.get("pages") or [None]
            for pg_i, page in enumerate(pages):
                cmd_list = (page.get("list") if page else None) or event.get("list") or []
                for cmd_i, cmd in enumerate(cmd_list):
                    code = cmd.get("code")
                    params = cmd.get("parameters") or []
                    base_path = [str(ev_i), str(pg_i), "list", str(cmd_i), "parameters"]
                    if code in _LINE_CODES and params and isinstance(params[0], str):
                        sid = self._id(file_rel, base_path + ["0"], params[0])
                        if sid in translations:
                            params[0] = translations[sid]
                            dirty = True
                            written += 1
                    elif code == _CHOICE_CODE and params and isinstance(params[0], list):
                        for ch_i, choice in enumerate(params[0]):
                            if not isinstance(choice, str):
                                continue
                            sid = self._id(file_rel, base_path + ["0", str(ch_i)], choice)
                            if sid in translations:
                                params[0][ch_i] = translations[sid]
                                dirty = True
                                written += 1
        if dirty:
            self.backup_file(self._current_root, fpath)
            with open(fpath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        return written

    # --- extract ----------------------------------------------------------
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        results: list[TranslationString] = []

        for base_path in paths_to_check:
            data_dir = self._data_dir(base_path) if sub_paths else self._data_dir(root)
            if not os.path.exists(data_dir):
                if os.path.isfile(os.path.join(base_path, "System.json")):
                    data_dir = base_path
                else:
                    continue
            rel_base = os.path.relpath(data_dir, root).replace("\\", "/")

            # Map events
            for fname in self._map_files(data_dir):
                data = _load_json(os.path.join(data_dir, fname))
                file_rel = f"{rel_base}/{fname}"

                for ev_i, event in enumerate(data.get("events") or []):
                    if not event:
                        continue
                    for pg_i, page in enumerate(event.get("pages") or []):
                        for cmd_i, cmd in enumerate(page.get("list") or []):
                            code = cmd.get("code")
                            params = cmd.get("parameters") or []
                            base_path_item = [
                                "events", str(ev_i), "pages", str(pg_i),
                                "list", str(cmd_i), "parameters",
                            ]
                            if code in _LINE_CODES and params and isinstance(params[0], str):
                                text = params[0]
                                if text.strip():
                                    results.append(self._mk(file_rel, base_path_item + ["0"], text))
                            elif code == _CHOICE_CODE and params and isinstance(params[0], list):
                                for ch_i, choice in enumerate(params[0]):
                                    if isinstance(choice, str) and choice.strip():
                                        results.append(
                                            self._mk(file_rel, base_path_item + ["0", str(ch_i)], choice)
                                        )

            # Database files (actors, items, skills, etc.)
            for fname, fields in _DB_FILES:
                if fields:  # flat-array DB
                    results.extend(self._extract_db(data_dir, fname, fields, rel_base))
                else:       # event-command-list files
                    results.extend(self._extract_event_list(data_dir, fname, rel_base))

            # System.json: game title, currency, UI terms (battle commands, stat names…)
            results.extend(self._extract_system(data_dir, rel_base))

            # MapInfos.json: location names shown on the map display
            results.extend(self._extract_map_infos(data_dir, rel_base))

        return results

    # --- inject -----------------------------------------------------------
    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0

        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            data_dir = self._data_dir(base_path) if sub_paths else self._data_dir(root)
            if not os.path.exists(data_dir):
                if os.path.isfile(os.path.join(base_path, "System.json")):
                    data_dir = base_path
                else:
                    continue
            rel_base = os.path.relpath(data_dir, root).replace("\\", "/")

            # Map events
            for fname in self._map_files(data_dir):
                fpath = os.path.join(data_dir, fname)
                data = _load_json(fpath)
                file_rel = f"{rel_base}/{fname}"
                dirty = False

                for ev_i, event in enumerate(data.get("events") or []):
                    if not event:
                        continue
                    for pg_i, page in enumerate(event.get("pages") or []):
                        for cmd_i, cmd in enumerate(page.get("list") or []):
                            code = cmd.get("code")
                            params = cmd.get("parameters") or []
                            base_path_item = [
                                "events", str(ev_i), "pages", str(pg_i),
                                "list", str(cmd_i), "parameters",
                            ]
                            if code in _LINE_CODES and params and isinstance(params[0], str):
                                sid = self._id(file_rel, base_path_item + ["0"], params[0])
                                if sid in translations:
                                    params[0] = translations[sid]
                                    dirty = True
                                    written += 1
                            elif code == _CHOICE_CODE and params and isinstance(params[0], list):
                                for ch_i, choice in enumerate(params[0]):
                                    if not isinstance(choice, str):
                                        continue
                                    sid = self._id(file_rel, base_path_item + ["0", str(ch_i)], choice)
                                    if sid in translations:
                                        params[0][ch_i] = translations[sid]
                                        dirty = True
                                        written += 1

                if dirty:
                    self.backup_file(self._current_root, fpath)
                    with open(fpath, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False)

            # Database files
            for fname, fields in _DB_FILES:
                if fields:
                    written += self._inject_db(data_dir, fname, fields, rel_base, translations)
                else:
                    written += self._inject_event_list(data_dir, fname, rel_base, translations)

            # System.json
            written += self._inject_system(data_dir, rel_base, translations)

            # MapInfos.json
            written += self._inject_map_infos(data_dir, rel_base, translations)

        return written

    def _id(self, file: str, path: list[str], original: str) -> str:
        # Use the same id the string was extracted with.
        from .base import make_id
        return make_id(self.engine, file, path, original)
