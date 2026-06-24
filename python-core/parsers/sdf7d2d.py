"""Parser for 7 Days to Die mod Localization.txt files.

Format is CSV with header row. English column found by name in header.
Variants:
  - Key,Source,Context,Changes,English                      (5 columns)
  - Key,English                                             (2 columns)
  - Key,Source,Context,Changes,English,French,German,...    (10+ columns)

Color tags like [f44336] and [-] must be preserved.
"""

from __future__ import annotations

import csv
import io
import os
from .base import BaseParser, TranslationString, make_id


class Sdf7d2dParser(BaseParser):
    engine = "sdf7d2d"

    def engine_prompt_addon(self) -> str:
        return (
            "7 DAYS TO DIE LOCALIZATION: these strings appear in-game as item "
            "names, descriptions, quest text, and UI labels.\n"
            "COLOR TAGS: preserve [RRGGBB] and [-] tags EXACTLY — they control "
            "in-game text coloring.\n"
            "NEWLINES: keep \\n as-is — they create line breaks in descriptions.\n"
            "FORMAT: the output must be a comma-separated line with the same "
            "column count as the source."
        )

    @staticmethod
    def detect(root: str) -> bool:
        """True if Config/Localization.txt exists (7 Days to Die mod)."""
        return os.path.isfile(os.path.join(root, "Config", "Localization.txt"))

    def _parse_row(self, row: list[str]) -> tuple[str, str] | None:
        """Extract (key, english_text) from a CSV row. Returns None if invalid."""
        if len(row) < 2:
            return None
        key = row[0].strip()
        if not key:
            return None
        # English text is always the last column
        english = row[-1].strip()
        if not english:
            return None
        return key, english

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        self._current_root = root
        results: list[TranslationString] = []
        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            loc_file = os.path.join(base_path, "Config", "Localization.txt")
            if not os.path.isfile(loc_file):
                continue
            rel_path = os.path.relpath(loc_file, root).replace("\\", "/")
            try:
                with open(loc_file, "r", encoding="utf-8-sig") as f:
                    content = f.read()
                reader = csv.reader(io.StringIO(content))
                header = next(reader, None)
                if not header:
                    continue
                header_lower = [h.strip().lower() for h in header]
                english_col = header_lower.index("english") if "english" in header_lower else len(header) - 1
                for row in reader:
                    if len(row) <= english_col:
                        continue
                    key = row[0].strip()
                    english = row[english_col].strip()
                    if key and english:
                        results.append(self._mk(rel_path, [key], english, f"7D2D | {key}"))
            except Exception as e:
                print(f"Error reading 7D2D Localization.txt {loc_file}: {e}")

        return results

    def inject(self, root: str, translations: dict[str, str], target_lang: str | None = None, sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0
        paths_to_check = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]

        for base_path in paths_to_check:
            loc_file = os.path.join(base_path, "Config", "Localization.txt")
            if not os.path.isfile(loc_file):
                continue
            rel_path = os.path.relpath(loc_file, root).replace("\\", "/")

            try:
                with open(loc_file, "r", encoding="utf-8-sig") as f:
                    content = f.read()

                lines = content.splitlines(keepends=True)
                if not lines:
                    continue

                # Parse header to determine column count
                header_line = lines[0].rstrip("\r\n")
                header_reader = csv.reader(io.StringIO(header_line))
                header = next(header_reader, None)
                if not header:
                    continue
                header_lower = [h.strip().lower() for h in header]
                english_col = header_lower.index("english") if "english" in header_lower else len(header) - 1

                new_lines = [lines[0]]  # keep header
                for line in lines[1:]:
                    stripped = line.strip()
                    if not stripped:
                        new_lines.append(line)
                        continue

                    row_reader = csv.reader(io.StringIO(stripped))
                    try:
                        row = next(row_reader)
                    except StopIteration:
                        new_lines.append(line)
                        continue

                    if len(row) < 2:
                        new_lines.append(line)
                        continue

                    key = row[0].strip()
                    if not key:
                        new_lines.append(line)
                        continue

                    sid = make_id(self.engine, rel_path, [key], row[english_col].strip())
                    if sid in translations and translations[sid]:
                        row[english_col] = translations[sid]
                        written += 1

                    # Re-serialize the row
                    buf = io.StringIO()
                    writer = csv.writer(buf, lineterminator="")
                    writer.writerow(row)
                    new_line = buf.getvalue()
                    # Preserve line ending
                    if line.endswith("\r\n"):
                        new_line += "\r\n"
                    elif line.endswith("\n"):
                        new_line += "\n"
                    new_lines.append(new_line)

                with open(loc_file, "w", encoding="utf-8-sig", newline="") as f:
                    f.writelines(new_lines)

            except Exception as e:
                print(f"Error injecting 7D2D Localization.txt {loc_file}: {e}")

        return written
