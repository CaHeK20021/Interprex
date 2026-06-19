"""Multimedia Fusion 2 localization parser (Data/Languages/lang_*.txt).

Some MMF2 games (e.g. Baba Is You) don't pack their UI text into the binary
engine chunks at all — they ship plain INI-style language files, one per
language, under `Data/Languages/`:

    Data/Languages/lang_en.txt   English (the source we translate FROM)
    Data/Languages/lang_de.txt   German    lang_fr.txt   French    …

Each file is UTF-8 (no BOM), CRLF line endings, and dead-simple INI:

    [general]
    name=English
    customfont=0
    [texts]
    main_continue=Continue playing
    settings=Settings

We translate ONLY the `[texts]` section — `[general]` holds runtime metadata
(language name, font flag) that must stay as-is. Per the user's decision we
overwrite `lang_en.txt` in place (backed up first), so the English slot the game
loads by default now carries the translation.

WRITE-BACK is in-place and surgical, like the Ren'Py parser: extract() and
inject() share `_scan()`, which yields each translatable value together with the
byte span of its value inside the raw line. inject() rewrites only that span, so
CRLF, key spelling, section headers, blank lines and every other byte are
preserved exactly.

PATH (write-back address + part of the id, BEDROCK #2). The key name is a stable,
structural address — reordering or inserting lines never shifts it — so
`path = ["texts", <key>]`. A line number would not be stable and is never used.
"""

from __future__ import annotations

import os

from .base import BaseParser, TranslationString, make_id

# Where the language files live, relative to the game root.
_LANG_DIR = os.path.join("Data", "Languages")
# The source-language file we read and (per user decision) overwrite.
_SOURCE_FILE = "lang_en.txt"
# Only this section holds player-facing text; [general] is metadata.
_TEXTS_SECTION = "texts"


def _scan(text: str):
    """Yield one record per translatable value in the `[texts]` section, in file
    order:

        {path, original, line: <idx>, start: <col>, end: <col>}

    `line` is the index into text.split("\\n"); `start`/`end` bound the value
    (everything after the first '=') within that line so inject() can splice a
    replacement in place. extract() keeps only path+original.

    A value may legitimately be empty (e.g. `museum_86level_1c=`); we still yield
    it so its id is stable, and extract() filters empties out (nothing to
    translate) while inject() can still target it if a translation is provided."""
    section = ""
    lines = text.split("\n")
    for li, raw in enumerate(lines):
        # Work on the line without its trailing CR so column math lands on the
        # real value bytes; the CR is restored implicitly by slicing the
        # original line in inject() (it lives past `end`).
        line = raw.rstrip("\r")

        stripped = line.strip()
        if not stripped:
            continue

        # Section header: [name]
        if stripped[0] == "[" and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            continue

        if section != _TEXTS_SECTION:
            continue

        eq = line.find("=")
        if eq < 0:
            continue  # not a key=value line; leave untouched

        key = line[:eq].strip()
        if not key:
            continue

        # Value span: immediately after '=' to end of the (CR-stripped) line.
        start = eq + 1
        end = len(line)
        yield {
            "path": [_TEXTS_SECTION, key],
            "original": line[start:end],
            "context": key,
            "line": li,
            "start": start,
            "end": end,
        }


class Mmf2Parser(BaseParser):
    engine = "mmf2"

    # --- detection --------------------------------------------------------
    @staticmethod
    def detect(root: str) -> bool:
        return Mmf2Parser._source_path(root) is not None

    @staticmethod
    def _source_path(base: str) -> str | None:
        """Path to lang_en.txt under `base` (root or a sub_path) if it exists and
        looks like the expected INI (has a [texts] section), else None."""
        candidate = os.path.join(base, _LANG_DIR, _SOURCE_FILE)
        try:
            with open(candidate, encoding="utf-8") as f:
                head = f.read(4096)
        except OSError:
            return None
        return candidate if "[texts]" in head else None

    # --- extract ----------------------------------------------------------
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        bases = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        results: list[TranslationString] = []
        for base in bases:
            src = self._source_path(base)
            if not src:
                continue
            file_rel = os.path.relpath(src, root).replace("\\", "/")
            # newline="" keeps CRLF in the text so the splice round-trips the
            # exact bytes; without it Python translates \r\n -> \n on read.
            with open(src, encoding="utf-8", newline="") as f:
                text = f.read()
            for rec in _scan(text):
                if rec["original"].strip():
                    results.append(self._mk(file_rel, rec["path"], rec["original"],
                                            rec.get("context", "")))
        return results

    # --- inject -----------------------------------------------------------
    def inject(self, root: str, translations: dict[str, str],
               target_lang: str | None = None,
               sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        bases = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        written = 0
        for base in bases:
            src = self._source_path(base)
            if not src:
                continue
            file_rel = os.path.relpath(src, root).replace("\\", "/")
            # newline="" keeps CRLF in the text so the splice round-trips the
            # exact bytes; without it Python translates \r\n -> \n on read.
            with open(src, encoding="utf-8", newline="") as f:
                text = f.read()
            lines = text.split("\n")
            dirty = False

            for rec in _scan(text):
                sid = make_id(self.engine, file_rel, rec["path"], rec["original"])
                if sid not in translations:
                    continue
                # Strip any stray CR/LF from the translation; the line's own CR
                # (if present) lives past `end` and is preserved by the splice.
                value = translations[sid].replace("\r", "").replace("\n", " ")
                line = lines[rec["line"]]
                lines[rec["line"]] = line[: rec["start"]] + value + line[rec["end"]:]
                dirty = True
                written += 1

            if dirty:
                self.backup_file(root, src)
                with open(src, "w", encoding="utf-8", newline="") as f:
                    f.write("\n".join(lines))

        return written
