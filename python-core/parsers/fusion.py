"""Fusion / Chowdren dialogue parser (data/dia and friends).

Games exported with Chowdren (e.g. Iconoclasts) keep their dialogue NOT inside
the binary engine chunks but in plain serialized-array files under `data/`, one
per language:

    data/dia      English (the source we translate FROM)
    data/diafra   French      data/diager   German    data/diajap   Japanese
    data/diaspa   Spanish     data/diachn   Chinese (簡)  data/diacht   Chinese (繁)

`lang.cfg` at the game root holds a single word ("English") that selects which
file the runtime loads. We translate `data/dia` in place (backed up first) and
leave `lang.cfg` untouched, so the English slot now carries the translation.

──────────────────────────────────────────────────────────────────────────────
TWO LAYERS — keep them separate (this is the extension seam).

  CONTAINER  (shared, game-independent)  — the `ARR1.0` serialized array: how
             cells are framed in the file, and how a cell is rewritten without
             corrupting the file. This is a property of the Fusion "Array
             object" extension, the same for every game that uses it.

  CODEC      (per-game, swappable)        — how readable text is encoded INSIDE
             one cell's payload bytes. Iconoclasts uses a pipe-separated `+31`
             cipher; another game on the same engine could store plain UTF-8, a
             different offset, etc. A new game = a new `DiaCodec` + its file
             name in `VARIANTS`, and NOTHING in the container layer changes.

Do not bake codec assumptions into the container, or game-specifics into the
codec — that wall is what makes the next game a ~10-line addition.
──────────────────────────────────────────────────────────────────────────────

CONTAINER (`ARR1.0`). The file is a serialized n-dim array:

    b"ARR1.0" [w:u32][h:u32] ...cells...

Cells form a nested, typed tree (a string cell is tagged 1,2; sub-arrays carry
other tags), so we do NOT try to parse the whole tree. Instead we TOKENIZE: a
string cell is the byte sequence

    01 00 00 00  02 00 00 00  [len:u32]  [len bytes]

Everything between/around such cells (`gap`) is preserved verbatim. Rebuilding
gap+cell+gap… reproduces the input byte-for-byte (verified on dia/diafra/diager,
12750 cells each). Only string cells are ever rewritten: a translation changes
the payload length, so inject() also rewrites that cell's own `[len:u32]` prefix
to match. This is safe — every cell carries its own length and nothing upstream
stores a total byte size (the per-language files differ in size but not in cell
count), so a longer/shorter translation does not corrupt the container.

CODEC — Iconoclasts. Pipe-separated tokens inside a cell:
    - a run of digits  ->  one character, char = int(token) + 31
                            (so "2"=space, "13"=',', "15"='.')
    - a {token}         ->  an in-game control code (color {dye04}, button glyph
                            {button06}, line break {new}, …) kept LITERAL.
Trailing NUL padding on a cell is preserved. The model already leaves {…}
placeholders untouched (see SYSTEM_INSTRUCTION in providers/base.py), so the
decoded text is handed to the LLM with its control tokens in place.

extract() and inject() share _iter_cells() so the address (cell index) they
compute for a given string is identical by construction.
"""

from __future__ import annotations

import os
import struct
import re
import logging

from .base import BaseParser, TranslationString, make_id, read_backup_original

logger = logging.getLogger("interprex")

# Magic at the start of every Chowdren serialized-array file.
_MAGIC = b"ARR1.0"
# Header after the magic: two u32 dimensions (w, h). Cells follow.
_HEADER_LEN = len(_MAGIC) + 8
# A string cell is introduced by these 8 bytes, then a u32 length, then bytes.
_CELL_MARK = b"\x01\x00\x00\x00\x02\x00\x00\x00"


# ─── CODEC layer (per-game, swappable) ──────────────────────────────────────
# A codec knows ONLY how text is encoded inside one cell's payload bytes. It
# never touches the ARR1.0 framing. To support a new Fusion game, write one
# DiaCodec subclass and register a DiaVariant in VARIANTS below — the container
# layer (cell framing, length-prefix rewrite, splice) is reused untouched.

class DiaCodec:
    """How readable text is encoded inside a single cell's payload bytes.

    A codec must satisfy, for every dialogue cell of its variant:
        encode(decode(cell)) == cell        (byte-exact round-trip)
    so that re-saving an untranslated cell never changes a byte. `decode`
    returns the human/LLM-facing text; `encode` is its exact inverse. Cells the
    codec doesn't recognise as text are filtered by is_dialogue/has_text."""

    def is_dialogue(self, cell: bytes) -> bool:
        """True if this cell holds encoded dialogue (vs. a structural value the
        codec wasn't designed for). Cheap byte-level check, no full decode."""
        raise NotImplementedError

    def decode(self, cell: bytes) -> str:
        """Cell payload bytes -> readable text (with literal control tokens)."""
        raise NotImplementedError

    def encode(self, text: str, cell: bytes) -> bytes:
        """Readable text -> cell payload bytes. `cell` is the ORIGINAL payload,
        passed so the codec can preserve shape that the text alone doesn't carry
        (e.g. trailing NUL padding) and stay byte-exact on an unchanged string."""
        raise NotImplementedError

    def has_text(self, text: str) -> bool:
        """True if decoded text contains anything worth translating. A cell that
        is only control tokens / whitespace carries no words -> skipped."""
        raise NotImplementedError


class PipeOffsetCodec(DiaCodec):
    """Iconoclasts codec: pipe-separated tokens, each char = int(token)+offset,
    `{tokens}` kept literal. Trailing NUL padding preserved verbatim."""

    def __init__(self, offset: int = 31) -> None:
        self.offset = offset
        self.use_cp1251 = False

    def is_dialogue(self, cell: bytes) -> bool:
        # Pipe-separated, every part a digit run or a {token}. Excludes any cell
        # that holds raw non-encoded bytes the codec wouldn't round-trip.
        s = cell.rstrip(b"\x00")
        if b"|" not in s:
            return False
        for part in s.split(b"|"):
            if part == b"" or part.isdigit():
                continue
            if part[:1] == b"{" and part[-1:] == b"}":
                continue
            return False
        return True

    def decode(self, cell: bytes) -> str:
        s = cell.decode("latin1").rstrip("\x00")
        out: list[str] = []
        buf = bytearray()
        
        def flush():
            if buf:
                encoding = "cp1251" if self.use_cp1251 else "latin1"
                out.append(buf.decode(encoding, errors="replace"))
                buf.clear()

        for part in s.split("|"):
            if part == "":
                continue
            if part.isdigit():
                val = int(part) + self.offset
                if val < 256:
                    buf.append(val)
                else:
                    flush()
                    out.append(chr(val))
            else:  # {dye04}, {button06}, {new}, … — kept verbatim
                flush()
                out.append(part)
        flush()
        return "".join(out)

    def encode(self, text: str, cell: bytes) -> bytes:
        trail = len(cell) - len(cell.rstrip(b"\x00"))
        parts: list[str] = []
        i, n = 0, len(text)
        encoding = "cp1251" if self.use_cp1251 else "latin1"
        while i < n:
            if text[i] == "{":
                j = text.find("}", i)
                if j != -1:
                    parts.append(text[i:j + 1])
                    i = j + 1
                    continue
            # Encode character to target encoding bytes, fallback to ord if needed
            try:
                char_byte = text[i].encode(encoding)[0]
            except Exception:
                char_byte = ord(text[i])
            parts.append(str(char_byte - self.offset))
            i += 1
        return ("|".join(parts)).encode("latin1") + b"\x00" * trail

    def has_text(self, text: str) -> bool:
        if text in ("Name", "???"):
            return False
        i, n = 0, len(text)
        while i < n:
            if text[i] == "{":
                j = text.find("}", i)
                if j != -1:
                    i = j + 1
                    continue
            if not text[i].isspace():
                return True
            i += 1
        return False

    @staticmethod
    def extract_tokens(text: str) -> list[str]:
        """Извлекает все {tokens} из текста в порядке их следования."""
        return re.findall(r'\{[^}]+\}', text)

    def restore_tokens(self, translated_text: str, reference_text: str) -> str:
        """Восстанавливает токены в переводе по эталону (оригинальной строке).
        Если количество токенов совпадает — подменяет токены в переводе
        токенами из эталона (чтобы восстановить точные параметры).
        Если количество разное — выбрасывает ValueError, чтобы пропустить
        ячейку и не записывать битый файл."""
        ref_tokens = self.extract_tokens(reference_text)
        trans_tokens = self.extract_tokens(translated_text)
        if len(ref_tokens) != len(trans_tokens):
            raise ValueError(
                f"Token count mismatch: original {len(ref_tokens)}, "
                f"translation {len(trans_tokens)}"
            )
        
        # Разрезаем строку перевода по токенам (сохраняя сами токены в списке)
        parts = re.split(r'(\{[^}]+\})', translated_text)
        # re.split с группирующими скобками возвращает список вида:
        # [текст_до, токен_1, текст_между, токен_2, текст_после...]
        # Управляющие токены всегда находятся на нечетных индексах (1, 3, 5...)
        
        token_idx = 0
        for i in range(1, len(parts), 2):
            parts[i] = ref_tokens[token_idx]
            token_idx += 1
            
        return "".join(parts)


class DiaVariant:
    """One supported game: the dialogue file's name + its codec. Detection and
    dispatch walk VARIANTS, so adding a game is a single entry here."""

    def __init__(self, file_name: str, codec: DiaCodec) -> None:
        self.file_name = file_name
        self.codec = codec


# Known Fusion/Chowdren dialogue variants, tried in order. The first whose file
# exists (with the ARR1.0 magic) under a project wins. Add a new game here.
VARIANTS: list[DiaVariant] = [
    DiaVariant("dia", PipeOffsetCodec(offset=31)),  # Iconoclasts
]


# ─── CONTAINER layer (shared, codec-independent) ────────────────────────────

def _iter_cells(data: bytes):
    """Yield (index, start, end, cell_bytes) for every string cell, in file
    order. `start`/`end` bound the cell's payload bytes (between the u32 length
    and the next gap) so inject() can splice a replacement in place. Index is the
    running position among string cells — stable as long as cells aren't
    inserted/removed, which a translation never does."""
    i = 0
    idx = 0
    n = len(data)
    while i < n:
        if data[i:i + 8] == _CELL_MARK and i + 12 <= n:
            ln = struct.unpack_from("<I", data, i + 8)[0]
            if i + 12 + ln <= n:
                start = i + 12
                end = start + ln
                yield idx, start, end, data[start:end]
                idx += 1
                i = end
                continue
        i += 1


class FusionParser(BaseParser):
    engine = "fusion"

    # --- detection --------------------------------------------------------
    @staticmethod
    def detect(root: str) -> bool:
        return FusionParser._find_variant(root) is not None

    @staticmethod
    def _find_variant(base: str) -> tuple[str, DiaVariant] | None:
        """The (path, variant) for the first known dialogue file present under
        `base` (root or a sub_path) with the ARR1.0 magic, or None."""
        for variant in VARIANTS:
            for candidate in (os.path.join(base, "data", variant.file_name),
                              os.path.join(base, variant.file_name)):
                try:
                    with open(candidate, "rb") as f:
                        if f.read(len(_MAGIC)) == _MAGIC:
                            return candidate, variant
                except OSError:
                    continue
        return None

    # --- extract ----------------------------------------------------------
    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        bases = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        results: list[TranslationString] = []
        for base in bases:
            found = self._find_variant(base)
            if not found:
                continue
            dia, variant = found
            codec = variant.codec

            # Detect if we should decode using CP1251 (Russian)
            use_cp1251 = False
            from .base import project_file_path
            proj_path = project_file_path(root)
            if os.path.exists(proj_path):
                try:
                    import json
                    with open(proj_path, "r", encoding="utf-8") as f:
                        proj = json.load(f)
                    for entry in proj.get("strings", {}).values():
                        tran = entry.get("translated", "")
                        if any('\u0400' <= c <= '\u04FF' for c in tran):
                            use_cp1251 = True
                            break
                except Exception:
                    pass
            if hasattr(codec, "use_cp1251"):
                codec.use_cp1251 = use_cp1251

            file_rel = os.path.relpath(dia, root).replace("\\", "/")
            # Prefer the backed-up ORIGINAL so re-extract after an inject still
            # shows the source English (and stable ids). Backups are stored as
            # reverse patches, so decode through read_backup_original — there's no
            # plain copy on disk to read directly.
            data = read_backup_original(root, file_rel)
            if data is None:
                with open(dia, "rb") as f:
                    data = f.read()
            for idx, _start, _end, cell in _iter_cells(data):
                if not codec.is_dialogue(cell):
                    continue
                text = codec.decode(cell)
                if not codec.has_text(text):
                    continue
                results.append(self._mk(file_rel, ["cell", str(idx)], text))
        return results

    # --- inject -----------------------------------------------------------
    def inject(self, root: str, translations: dict[str, str],
               target_lang: str | None = None,
               sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        bases = [root] if not sub_paths else [os.path.join(root, p) for p in sub_paths]
        written = 0
        for base in bases:
            found = self._find_variant(base)
            if not found:
                continue
            dia, variant = found
            codec = variant.codec

            # Detect if we should encode using CP1251 (Russian)
            use_cp1251 = False
            if target_lang and target_lang.lower() in ("ru", "russian"):
                use_cp1251 = True
            else:
                for tran in translations.values():
                    if any('\u0400' <= c <= '\u04FF' for c in tran):
                        use_cp1251 = True
                        break
            if hasattr(codec, "use_cp1251"):
                codec.use_cp1251 = use_cp1251

            file_rel = os.path.relpath(dia, root).replace("\\", "/")
            with open(dia, "rb") as f:
                data = f.read()

            skipped = 0
            # Collect (start, end, new_bytes) for every translated cell, then
            # splice right-to-left so earlier offsets stay valid.
            edits: list[tuple[int, int, bytes]] = []
            for idx, start, end, cell in _iter_cells(data):
                if not codec.is_dialogue(cell):
                    continue
                text = codec.decode(cell)
                if not codec.has_text(text):
                    continue
                sid = make_id(self.engine, file_rel, ["cell", str(idx)], text)
                if sid not in translations:
                    continue
                
                translated_text = translations[sid]
                if hasattr(codec, "restore_tokens"):
                    try:
                        translated_text = codec.restore_tokens(translated_text, text)
                    except ValueError as e:
                        logger.warning("Skipping cell %d in %s due to token mismatch: %s", idx, file_rel, e)
                        skipped += 1
                        continue

                edits.append((start, end, codec.encode(translated_text, cell)))

            if skipped > 0:
                logger.warning("Skipped %d cells in %s due to token mismatch", skipped, file_rel)

            if not edits:
                continue

            # Splice right-to-left so earlier offsets stay valid. A translation
            # changes the payload length, so the cell's own u32 length prefix (the
            # 4 bytes at start-4) must be rewritten to match — otherwise a re-read
            # truncates the cell. This is safe: each cell carries its own length
            # and nothing upstream stores a byte size (the per-language files have
            # different total sizes but identical cell counts).
            buf = bytearray(data)
            for start, end, new_bytes in sorted(edits, reverse=True):
                buf[start:end] = new_bytes
                buf[start - 4:start] = struct.pack("<I", len(new_bytes))
                written += 1

            self.backup_file(root, dia)
            with open(dia, "wb") as f:
                f.write(buf)

        return written
