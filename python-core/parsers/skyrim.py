"""Skyrim Special Edition / Anniversary Edition — Creation Engine plugins.

Reads and writes `.esp` / `.esm` / `.esl` plugins (24-byte record headers, SSE
layout). Text lives either:

  * inline as null-terminated UTF-8 zstrings inside subrecords (most small mods),
    or
  * as uint32 string IDs when TES4 flag 0x80 (Localized) is set — then the real
    text is in sibling `Strings/<Plugin>_<lang>.{strings,dlstrings,ilstrings}`.

Scope:
  * Delocalized plugins (inline zstrings) — extract + inject in-place.
  * Localized plugins — extract via string tables when present; inject into the
    string tables (ESP formIDs left untouched).
  * SkyUI / MCM UI strings in ``Interface/Translations/<mod>_<lang>.txt`` —
    read loose OR from sibling ``.bsa`` (SSE LZ4), write LOOSE overrides
    (engine prefers Data/ over archive; BSA never rewritten).
  * FO4 / ``.ba2`` is a separate future engine.

Architecture notes for a future Fallout 4 parser: share the ESP tree
reader/writer (record/GRUP/subrecord + zlib + XXXX), swap the text-field
dictionary and the archive container (BSA vs BA2).
"""

from __future__ import annotations

import logging
import os
import re
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .base import BaseParser, TranslationString, make_id
from . import bsa as bsa_mod

logger = logging.getLogger("interprex.skyrim")

# ---------------------------------------------------------------------------
# Binary layout constants (Skyrim SE / FO4 64-bit Creation Engine)
# ---------------------------------------------------------------------------

REC_HEADER = 24          # type(4) dataSize(4) flags(4) formId(4) + 8 bytes VC
SUB_HEADER = 6           # type(4) size(2)
FLAG_COMPRESSED = 0x00040000
FLAG_LOCALIZED = 0x00000080  # TES4 header only

PLUGIN_EXTS = (".esp", ".esm", ".esl")

# (record_type, subrecord_type) pairs that hold player-visible text.
# FULL/DESC are accepted on almost every record; more specific pairs catch
# dialogue responses, magic-effect blurbs, message buttons, quest log lines.
# Asset-path fields (HDPT:NAM1 mesh paths, PROJ:NAM1, …) are NOT listed.
_TEXT_FIELDS: frozenset[tuple[bytes, bytes]] = frozenset({
    # Universal display name / description
    (b"*", b"FULL"),
    (b"*", b"DESC"),
    # Dialogue
    (b"INFO", b"NAM1"),   # spoken line
    (b"INFO", b"RNAM"),   # player response prompt
    # Magic effect description (not FULL — FULL is internal effect name)
    (b"MGEF", b"DNAM"),
    # Message box buttons
    (b"MESG", b"ITXT"),
    # NPC short name (what's shown over the head)
    (b"NPC_", b"SHRT"),
    # Quest
    (b"QUST", b"NNAM"),   # objective
    (b"QUST", b"CNAM"),   # journal / log entry
    # Faction rank names
    (b"FACT", b"RNAM"),
})

# Subrecords that go into .dlstrings (long descriptions) when Localized.
# Everything else text goes to .strings; dialogue (INFO) to .ilstrings.
_DLSTRING_SUBS = frozenset({b"DESC", b"CNAM", b"DNAM"})
_ILSTRING_SUBS = frozenset({b"NAM1", b"RNAM"})  # only on INFO in practice

# Book/DESC HTML tags the LLM must preserve.
_ENGINE_PROMPT = (
    "TECHNICAL STRINGS (Skyrim plugin + UI text):\n"
    "These strings come from .esp/.esm/.esl plugin records (item names, spell "
    "descriptions, dialogue, books, quest objectives) AND from SkyUI/MCM "
    "Interface/Translations files ($KEY → caption).\n"
    "MARKUP: preserve Skyrim book/UI tags EXACTLY — <font …>, </font>, "
    "<p align=…>, [pagebreak], […] aliases, and any HTML-like tags. Do NOT "
    "translate tag names or attributes.\n"
    "GAME TOKENS: keep angle-bracket tokens like <mag>, <dur>, <50>, #1, #2 "
    "byte-verbatim — they are filled in at runtime.\n"
    "SKYUI KEYS: never invent or alter $KEY identifiers; only translate the "
    "value text after the tab.\n"
    "TONE: match the source (lore book vs. menu button vs. combat shout). "
    "Fantasy proper names may stay untranslated when they are invented lore."
)

# SkyUI language suffix (filename: <mod>_<lang>.txt). Keys are lowercase.
_SKYUI_LANG_ALIASES: dict[str, str] = {
    "english": "english",
    "russian": "russian",
    "spanish": "spanish",
    "german": "german",
    "french": "french",
    "japanese": "japanese",
    "italian": "italian",
    "polish": "polish",
    "czech": "czech",
    "chinese": "chinese",
    "chinese (simplified)": "chinese",
    "chinese (traditional)": "chinese",
    "korean": "korean",
    "portuguese": "portuguese",
    "portuguese (brazil)": "portuguese",
    "portuguese (brasil)": "portuguese",
}
_SOURCE_LANG_PREFERENCE = ("english", "en")


# ---------------------------------------------------------------------------
# Low-level tree
# ---------------------------------------------------------------------------

@dataclass
class SubRecord:
    type: bytes
    data: bytes
    oversized: bool = False  # was (or must be) preceded by XXXX


@dataclass
class Record:
    type: bytes
    flags: int
    form_id: int
    vc: bytes                # 8 bytes after formId
    subs: list[SubRecord]
    compressed: bool
    raw: bytes               # full on-disk record (header+body), for identity
    dirty: bool = False


@dataclass
class Group:
    label: bytes
    group_type: int
    stamp: bytes             # 8 bytes after groupType
    children: list           # Record | Group
    raw: bytes
    dirty: bool = False


Node = Record | Group


def _parse_subs(body: bytes) -> list[SubRecord]:
    subs: list[SubRecord] = []
    sp = 0
    n = len(body)
    while sp + SUB_HEADER <= n:
        st = body[sp:sp + 4]
        ss = struct.unpack_from("<H", body, sp + 4)[0]
        sp += SUB_HEADER
        oversized = False
        if st == b"XXXX" and ss == 4 and sp + 4 <= n:
            real = struct.unpack_from("<I", body, sp)[0]
            sp += 4
            if sp + SUB_HEADER > n:
                break
            st = body[sp:sp + 4]
            # size field is usually 0; real size is in the preceding XXXX
            sp += SUB_HEADER
            ss = real
            oversized = True
        if sp + ss > n:
            break
        subs.append(SubRecord(st, body[sp:sp + ss], oversized))
        sp += ss
    return subs


def _write_subs(subs: list[SubRecord]) -> bytes:
    out = bytearray()
    for s in subs:
        if s.oversized or len(s.data) > 0xFFFF:
            out += b"XXXX" + struct.pack("<H", 4) + struct.pack("<I", len(s.data))
            out += s.type + struct.pack("<H", 0) + s.data
        else:
            out += s.type + struct.pack("<H", len(s.data)) + s.data
    return bytes(out)


def parse_plugin(data: bytes) -> list[Node]:
    """Parse a whole .esp/.esm/.esl into a tree of Records and Groups."""
    return _parse_nodes(data, 0, len(data))


def _parse_nodes(data: bytes, start: int, end: int) -> list[Node]:
    nodes: list[Node] = []
    pos = start
    while pos < end:
        if pos + 4 > end:
            break
        rtype = data[pos:pos + 4]
        if rtype == b"GRUP":
            if pos + REC_HEADER > end:
                break
            gsize = struct.unpack_from("<I", data, pos + 4)[0]
            if gsize < REC_HEADER or pos + gsize > end:
                break
            label = data[pos + 8:pos + 12]
            gtype = struct.unpack_from("<I", data, pos + 12)[0]
            stamp = data[pos + 16:pos + 24]
            children = _parse_nodes(data, pos + REC_HEADER, pos + gsize)
            raw = data[pos:pos + gsize]
            nodes.append(Group(label, gtype, stamp, children, raw))
            pos += gsize
        else:
            if pos + REC_HEADER > end:
                break
            dsize = struct.unpack_from("<I", data, pos + 4)[0]
            flags = struct.unpack_from("<I", data, pos + 8)[0]
            form_id = struct.unpack_from("<I", data, pos + 12)[0]
            vc = data[pos + 16:pos + 24]
            body_end = pos + REC_HEADER + dsize
            if body_end > end:
                break
            raw = data[pos:body_end]
            body = data[pos + REC_HEADER:body_end]
            compressed = bool(flags & FLAG_COMPRESSED)
            if compressed and len(body) >= 4:
                try:
                    raw_subs = zlib.decompress(body[4:])
                except Exception:
                    raw_subs = b""
                subs = _parse_subs(raw_subs)
            else:
                subs = _parse_subs(body)
            nodes.append(Record(
                type=rtype, flags=flags, form_id=form_id, vc=vc,
                subs=subs, compressed=compressed, raw=raw,
            ))
            pos = body_end
    return nodes


def write_plugin(nodes: list[Node]) -> bytes:
    return _write_nodes(nodes)


def _write_nodes(nodes: list[Node]) -> bytes:
    out = bytearray()
    for n in nodes:
        if isinstance(n, Group):
            if not n.dirty:
                out += n.raw
                continue
            body = _write_nodes(n.children)
            gsize = REC_HEADER + len(body)
            out += (
                b"GRUP"
                + struct.pack("<I", gsize)
                + n.label
                + struct.pack("<I", n.group_type)
                + n.stamp
                + body
            )
        else:
            if not n.dirty:
                out += n.raw
                continue
            body = _write_subs(n.subs)
            if n.compressed:
                body = struct.pack("<I", len(body)) + zlib.compress(body)
            out += (
                n.type
                + struct.pack("<I", len(body))
                + struct.pack("<I", n.flags)
                + struct.pack("<I", n.form_id)
                + n.vc
                + body
            )
    return bytes(out)


def _mark_dirty(rec: Record, ancestors: list[Group]) -> None:
    rec.dirty = True
    for g in ancestors:
        g.dirty = True


# ---------------------------------------------------------------------------
# String helpers
# ---------------------------------------------------------------------------

def _decode_zstring(data: bytes) -> str | None:
    """Decode a null-terminated (or bare) UTF-8/cp1252 string. None if binary."""
    if not data:
        return None
    raw = data[:-1] if data[-1] == 0 else data
    if not raw or 0 in raw:
        return None
    for enc in ("utf-8", "cp1252"):
        try:
            s = raw.decode(enc)
            break
        except UnicodeDecodeError:
            s = None
    else:
        return None
    if not s or not any(c.isalpha() for c in s):
        return None
    # Reject control-char heavy blobs
    if sum(1 for c in s if ord(c) < 9) > 0:
        return None
    return s


def _encode_zstring(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def _looks_like_asset_path(s: str) -> bool:
    sl = s.lower()
    if "\\" in s or s.count("/") >= 2:
        return True
    return any(sl.endswith(ext) for ext in (
        ".nif", ".dds", ".tri", ".hkx", ".wav", ".fuz", ".psc", ".pex",
        ".swf", ".seq", ".lip", ".txt", ".json", ".xml",
    ))


def _is_text_field(rec_type: bytes, sub_type: bytes) -> bool:
    if rec_type == b"TES4":
        return False  # author/desc metadata — not game text
    if (rec_type, sub_type) in _TEXT_FIELDS:
        return True
    if (b"*", sub_type) in _TEXT_FIELDS:
        return True
    return False


def tes4_is_localized(nodes: list[Node]) -> bool:
    for n in nodes:
        if isinstance(n, Record) and n.type == b"TES4":
            return bool(n.flags & FLAG_LOCALIZED)
        break  # TES4 is always the first record
    return False


def iter_records(nodes: list[Node],
                 ancestors: list[Group] | None = None
                 ) -> Iterator[tuple[Record, list[Group]]]:
    anc = ancestors or []
    for n in nodes:
        if isinstance(n, Group):
            yield from iter_records(n.children, anc + [n])
        else:
            yield n, anc


# ---------------------------------------------------------------------------
# String tables (.strings / .dlstrings / .ilstrings)
# ---------------------------------------------------------------------------

@dataclass
class StringTable:
    """Bethesda string table: directory of (id → string)."""
    strings: dict[int, str] = field(default_factory=dict)
    # original file kind for rewrite
    kind: str = "strings"  # strings | dlstrings | ilstrings

    @staticmethod
    def load(path: Path) -> "StringTable":
        data = path.read_bytes()
        if len(data) < 8:
            return StringTable()
        count, data_size = struct.unpack_from("<II", data, 0)
        # directory: count × (id u32, offset u32) starting at 8
        dir_end = 8 + count * 8
        data_start = dir_end
        # Some writers put data_size as size of string data only; data begins at
        # dir_end. Offsets are relative to the start of the data block.
        kind = "strings"
        name = path.name.lower()
        if name.endswith(".dlstrings"):
            kind = "dlstrings"
        elif name.endswith(".ilstrings"):
            kind = "ilstrings"
        out: dict[int, str] = {}
        for i in range(count):
            sid, off = struct.unpack_from("<II", data, 8 + i * 8)
            abs_off = data_start + off
            if abs_off >= len(data):
                continue
            if kind == "strings":
                # zstring at offset
                end = data.find(b"\x00", abs_off)
                if end < 0:
                    end = len(data)
                raw = data[abs_off:end]
            else:
                # dl/il: uint32 length (includes trailing null) then bytes
                if abs_off + 4 > len(data):
                    continue
                (slen,) = struct.unpack_from("<I", data, abs_off)
                raw = data[abs_off + 4:abs_off + 4 + slen]
                if raw.endswith(b"\x00"):
                    raw = raw[:-1]
            try:
                out[sid] = raw.decode("utf-8")
            except UnicodeDecodeError:
                out[sid] = raw.decode("cp1252", errors="replace")
        return StringTable(strings=out, kind=kind)

    def save(self) -> bytes:
        """Serialize back. IDs stable; offsets recomputed."""
        # Build data block
        data_blob = bytearray()
        offsets: dict[int, int] = {}
        for sid in sorted(self.strings.keys()):
            offsets[sid] = len(data_blob)
            text = self.strings[sid]
            raw = text.encode("utf-8")
            if self.kind == "strings":
                data_blob += raw + b"\x00"
            else:
                payload = raw + b"\x00"
                data_blob += struct.pack("<I", len(payload)) + payload
        count = len(self.strings)
        header = struct.pack("<II", count, len(data_blob))
        directory = bytearray()
        for sid in sorted(self.strings.keys()):
            directory += struct.pack("<II", sid, offsets[sid])
        return bytes(header + directory + data_blob)


def _plugin_stem(plugin_path: Path) -> str:
    return plugin_path.stem


def _find_string_tables(plugin_path: Path, lang: str = "english"
                        ) -> dict[str, Path]:
    """Locate Strings/<stem>_<lang>.{strings,dlstrings,ilstrings} next to the
    plugin or under a sibling Strings/ folder (both layouts exist)."""
    stem = _plugin_stem(plugin_path)
    candidates = [
        plugin_path.parent / "Strings",
        plugin_path.parent / "strings",
        plugin_path.parent,
    ]
    found: dict[str, Path] = {}
    for folder in candidates:
        if not folder.is_dir() and folder != plugin_path.parent:
            continue
        for kind in ("strings", "dlstrings", "ilstrings"):
            # Case-insensitive match
            want = f"{stem}_{lang}.{kind}".lower()
            try:
                for p in folder.iterdir():
                    if p.is_file() and p.name.lower() == want:
                        found[kind] = p
            except OSError:
                pass
        if found:
            break
    return found


def _string_table_bucket(sub_type: bytes) -> str:
    if sub_type in _ILSTRING_SUBS:
        return "ilstrings"
    if sub_type in _DLSTRING_SUBS:
        return "dlstrings"
    return "strings"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def iter_plugins(root: str, sub_paths: list[str] | None = None) -> list[Path]:
    """List plugin files under root (or under each sub_path).

    `sub_paths` entries may be directories OR individual .esp/.esm/.esl files
    (the flat-Data layout common when users dump mods into one folder).
    """
    bases: list[Path] = []
    if not sub_paths:
        bases = [Path(root)]
    else:
        for sp in sub_paths:
            bases.append(Path(root, sp) if not os.path.isabs(sp) else Path(sp))

    out: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.exists():
            continue
        if base.is_file() and base.suffix.lower() in PLUGIN_EXTS:
            key = str(base.resolve()).lower()
            if key not in seen:
                seen.add(key)
                out.append(base)
            continue
        if not base.is_dir():
            continue
        # Prefer top-level plugins; also recurse one level for foldered mods
        # (e.g. zFDE Aela/FDE Aela.esp) but skip deep trees (meshes/textures).
        for dirpath, dirnames, filenames in os.walk(base):
            # Don't descend into known non-plugin trees
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in {
                    "meshes", "textures", "sound", "music", "seq", "scripts",
                    "source", "skse", "f4se", "interface", "strings", "video",
                    "grass", "lodsettings", "tools", ".git", "interprex",
                    ".interprex_backups",
                }
            ]
            rel_depth = len(Path(dirpath).relative_to(base).parts)
            if rel_depth > 3:
                dirnames.clear()
                continue
            for fn in filenames:
                if fn.lower().endswith(PLUGIN_EXTS):
                    p = Path(dirpath) / fn
                    key = str(p.resolve()).lower()
                    if key not in seen:
                        seen.add(key)
                        out.append(p)
    return out


def _is_sse_plugin(path: Path) -> bool:
    """True if file looks like a Skyrim SE (24-byte header) plugin."""
    try:
        with open(path, "rb") as f:
            head = f.read(32)
    except OSError:
        return False
    if len(head) < 28 or head[0:4] != b"TES4":
        return False
    # After 24-byte record header the first subrecord should be HEDR
    return head[24:28] == b"HEDR"


# ---------------------------------------------------------------------------
# SkyUI / Interface/Translations
# ---------------------------------------------------------------------------

def _skyui_lang(target_lang: str | None) -> str:
    """Map Interprex display name (\"Russian\") → SkyUI filename suffix."""
    if not target_lang:
        return "english"
    key = target_lang.strip().lower()
    if key in _SKYUI_LANG_ALIASES:
        return _SKYUI_LANG_ALIASES[key]
    # "Chinese (Simplified)" already covered; bare prefix before "(" 
    bare = key.split("(", 1)[0].strip()
    return _SKYUI_LANG_ALIASES.get(bare, bare.replace(" ", ""))


def _parse_translation_filename(name: str) -> tuple[str, str] | None:
    """`racemenu_english.txt` → (`racemenu`, `english`). None if not a match."""
    lower = name.lower()
    if not lower.endswith(".txt"):
        return None
    stem = lower[:-4]
    if "_" not in stem:
        return None
    mod, lang = stem.rsplit("_", 1)
    if not mod or not lang:
        return None
    return mod, lang


def _canonical_translation_rel(rel: str) -> str:
    """Force `…/Interface/Translations/<file>` casing for stable ids."""
    rel = rel.replace("\\", "/")
    lower = rel.lower()
    marker = "interface/translations/"
    idx = lower.rfind(marker)
    if idx < 0:
        return rel
    prefix = rel[:idx]
    name = rel[idx + len(marker):]
    return f"{prefix}Interface/Translations/{name}"


def _decode_translation_bytes(data: bytes) -> tuple[str, str]:
    """Return (text, encoding) for a SkyUI translations file.
    encoding is ``utf-16-le`` (with BOM) or ``utf-8``."""
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be"), "utf-16-be"
    # utf-8 BOM
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "utf-8"


def _parse_translation_text(text: str) -> list[tuple[str, str]]:
    """Parse `$KEY\\tValue` lines. Returns ordered (key, value) pairs.
    Keys keep the leading ``$``. Blank / comment lines skipped."""
    # Drop BOM char if decode left it
    if text.startswith("\ufeff"):
        text = text[1:]
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip("\r\n")
        if not line or line.lstrip().startswith("#"):
            continue
        # Tab is the SkyUI delimiter; some authors use a single space after $KEY
        if "\t" in line:
            key, _, val = line.partition("\t")
        else:
            m = re.match(r"^(\$[^\s]+)\s+(.*)$", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
        key = key.strip()
        if not key.startswith("$"):
            continue
        out.append((key, val))
    return out


def _encode_translation_file(pairs: list[tuple[str, str]],
                             encoding: str = "utf-16-le") -> bytes:
    """Write SkyUI translations file bytes (CRLF, BOM for UTF-16)."""
    body = "\r\n".join(f"{k}\t{v}" for k, v in pairs)
    if body:
        body += "\r\n"
    if encoding.startswith("utf-16"):
        bom = b"\xff\xfe" if encoding == "utf-16-le" else b"\xfe\xff"
        return bom + body.encode(encoding)
    return body.encode("utf-8")


def _mod_bases(root: str, sub_paths: list[str] | None) -> list[Path]:
    if not sub_paths:
        return [Path(root)]
    out: list[Path] = []
    for sp in sub_paths:
        out.append(Path(root, sp) if not os.path.isabs(sp) else Path(sp))
    return out


def _sibling_bsas(plugin_or_dir: Path) -> list[Path]:
    """BSAs that belong with a plugin or sit inside a mod folder."""
    found: list[Path] = []
    if plugin_or_dir.is_file():
        stem = plugin_or_dir.stem.lower()
        parent = plugin_or_dir.parent
        try:
            for p in parent.iterdir():
                if not p.is_file() or p.suffix.lower() != ".bsa":
                    continue
                s = p.stem.lower()
                # RaceMenu.esp ↔ RaceMenu.bsa / RaceMenu - Textures.bsa
                if s == stem or s.startswith(stem + " ") or s.startswith(stem + "-"):
                    found.append(p)
        except OSError:
            pass
        return found
    if not plugin_or_dir.is_dir():
        return []
    try:
        for dirpath, dirnames, filenames in os.walk(plugin_or_dir):
            dirnames[:] = [
                d for d in dirnames
                if d.lower() not in {
                    "meshes", "textures", "sound", "music", "seq", "scripts",
                    "source", "skse", "f4se", "video", "grass", ".git",
                    "interprex", ".interprex_backups",
                }
            ]
            rel_depth = len(Path(dirpath).relative_to(plugin_or_dir).parts)
            if rel_depth > 2:
                dirnames.clear()
                continue
            for fn in filenames:
                if fn.lower().endswith(".bsa"):
                    found.append(Path(dirpath) / fn)
    except OSError:
        pass
    return found


def _loose_translation_files(base: Path) -> list[Path]:
    """Loose Interface/Translations/*.txt under base (or its parent for a plugin)."""
    roots = []
    if base.is_file():
        roots.append(base.parent)
    else:
        roots.append(base)
    out: list[Path] = []
    for r in roots:
        for sub in ("Interface/Translations", "interface/translations"):
            d = r / sub.replace("/", os.sep)
            if not d.is_dir():
                continue
            try:
                for p in d.iterdir():
                    if p.is_file() and p.suffix.lower() == ".txt":
                        out.append(p)
            except OSError:
                pass
    return out


@dataclass
class _UiTranslationFile:
    """One SkyUI translations file ready to extract / inject against."""
    # Stable virtual path relative to root (forward slashes), always the ENGLISH
    # (source) path so ids don't drift when we write a russian override.
    rel_file: str
    mod_stem: str          # "racemenu"
    source_lang: str       # "english"
    pairs: list[tuple[str, str]]
    encoding: str
    # Where to write the target-language override (absolute).
    write_dir: Path


def _collect_ui_translation_files(
    root: str, sub_paths: list[str] | None = None,
) -> list[_UiTranslationFile]:
    """Gather source-language SkyUI translation files from loose paths + BSAs.

    Prefers ``*_english.txt``. Loose files override the same name from a BSA
    (matches the engine: Data/ wins over archive)."""
    root_path = Path(root)
    # key: rel_file lower → _UiTranslationFile
    by_rel: dict[str, _UiTranslationFile] = {}

    def _consider(raw: bytes, rel_file: str, write_dir: Path,
                  *, from_loose: bool) -> None:
        parsed = _parse_translation_filename(Path(rel_file).name)
        if not parsed:
            return
        mod_stem, lang = parsed
        if lang not in _SOURCE_LANG_PREFERENCE and lang != "english":
            # Only ingest source-language files as originals. Other langs are
            # prior translations; we write those on inject.
            return
        text, encoding = _decode_translation_bytes(raw)
        pairs = _parse_translation_text(text)
        if not pairs:
            return
        rel_norm = rel_file.replace("\\", "/")
        key = rel_norm.lower()
        # Loose Data/ files override the same path from a BSA (engine rule).
        if key in by_rel and not from_loose:
            return
        by_rel[key] = _UiTranslationFile(
            rel_file=rel_norm,
            mod_stem=mod_stem,
            source_lang=lang,
            pairs=pairs,
            encoding=encoding,
            write_dir=write_dir,
        )

    bases = _mod_bases(root, sub_paths)
    # Also when a sub_path is a single .esp, consider that plugin's directory.
    scan_targets: list[Path] = []
    for b in bases:
        scan_targets.append(b)
        if b.is_file() and b.suffix.lower() in PLUGIN_EXTS:
            pass  # sibling BSAs handled below
        elif not sub_paths:
            # whole-root: also pick up every plugin's sibling BSA via plugins list
            pass

    # 1) Loose Interface/Translations
    for base in bases:
        write_root = base.parent if base.is_file() else base
        for loose in _loose_translation_files(base):
            try:
                raw = loose.read_bytes()
            except OSError:
                continue
            try:
                rel = os.path.relpath(loose, root).replace("\\", "/")
            except ValueError:
                rel = f"Interface/Translations/{loose.name}"
            # Canonical casing — Windows paths are case-insensitive and iterdir
            # may return lower-case; stable ids need a fixed spelling.
            rel = _canonical_translation_rel(rel)
            _consider(raw, rel, write_root / "Interface" / "Translations",
                      from_loose=True)

    # 2) BSA contents — for each plugin in scope, its sibling BSAs; for dirs, all BSAs
    bsa_paths: list[Path] = []
    seen_bsa: set[str] = set()
    for base in bases:
        for bp in _sibling_bsas(base):
            k = str(bp.resolve()).lower()
            if k not in seen_bsa:
                seen_bsa.add(k)
                bsa_paths.append(bp)
    if not sub_paths:
        # Whole-folder scan: every top-level + one-level BSA
        for bp in Path(root).rglob("*.bsa"):
            # depth guard
            try:
                depth = len(bp.relative_to(root).parts)
            except ValueError:
                continue
            if depth > 3:
                continue
            k = str(bp.resolve()).lower()
            if k not in seen_bsa:
                seen_bsa.add(k)
                bsa_paths.append(bp)

    for bp in bsa_paths:
        arch = bsa_mod.open_bsa(bp)
        if not arch:
            continue
        # write_dir = parent of BSA (flat Data layout) so loose override lands
        # next to the archive as Interface/Translations/
        write_dir = bp.parent / "Interface" / "Translations"
        for entry in arch.entries:
            if "interface/translations/" not in entry.path:
                continue
            if not entry.path.endswith(".txt"):
                continue
            raw = arch.read(entry)
            if not raw:
                continue
            # Virtual path: if BSA lives in a subfolder mod, prefix it
            try:
                bsa_parent_rel = os.path.relpath(bp.parent, root).replace("\\", "/")
            except ValueError:
                bsa_parent_rel = ""
            name = Path(entry.path).name
            if bsa_parent_rel in ("", "."):
                rel = f"Interface/Translations/{name}"
            else:
                rel = f"{bsa_parent_rel}/Interface/Translations/{name}"
            _consider(raw, rel, write_dir, from_loose=False)

    return list(by_rel.values())


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class SkyrimParser(BaseParser):
    engine = "skyrim"

    def engine_prompt_addon(self) -> str:
        return _ENGINE_PROMPT

    @staticmethod
    def detect(root: str) -> bool:
        # A folder (or a single plugin path) is "Skyrim" if it contains at least
        # one SSE-layout plugin. Avoid false positives on FO4 by requiring HEDR
        # at offset 24 (same for FO4 actually — FO4 also uses 24-byte headers).
        # Distinguishing FO4 later: .ba2 presence / different master list.
        # For now any Creation-Engine plugin folder maps here; FO4 gets its own
        # engine when we add BA2 + FO4 record dict.
        p = Path(root)
        if p.is_file():
            return _is_sse_plugin(p)
        # Quick scan: top-level plugins first, then one folder level
        try:
            for entry in p.iterdir():
                if entry.is_file() and entry.suffix.lower() in PLUGIN_EXTS:
                    if _is_sse_plugin(entry):
                        return True
                elif entry.is_dir():
                    try:
                        for sub in entry.iterdir():
                            if sub.is_file() and sub.suffix.lower() in PLUGIN_EXTS:
                                if _is_sse_plugin(sub):
                                    return True
                    except OSError:
                        pass
        except OSError:
            return False
        return False

    # -- extract -----------------------------------------------------------

    def extract(self, root: str,
                sub_paths: list[str] | None = None) -> list[TranslationString]:
        results: list[TranslationString] = []
        for plugin in iter_plugins(root, sub_paths):
            try:
                results.extend(self._extract_plugin(root, plugin))
            except Exception as e:
                logger.error("skyrim extract failed for %s: %s", plugin, e)
        try:
            results.extend(self._extract_ui_translations(root, sub_paths))
        except Exception as e:
            logger.error("skyrim UI translations extract failed: %s", e)
        return results

    def _extract_ui_translations(
        self, root: str, sub_paths: list[str] | None,
    ) -> list[TranslationString]:
        out: list[TranslationString] = []
        for tf in _collect_ui_translation_files(root, sub_paths):
            for key, val in tf.pairs:
                if not val or not any(c.isalpha() for c in val):
                    continue
                path = [key]
                ctx = f"SkyUI {tf.mod_stem}: {key}"
                out.append(self._mk(tf.rel_file, path, val, ctx))
        return out

    def _extract_plugin(self, root: str, plugin: Path) -> list[TranslationString]:
        data = plugin.read_bytes()
        nodes = parse_plugin(data)
        rel = plugin.relative_to(root).as_posix() if Path(root) in plugin.parents or Path(root) == plugin.parent or str(plugin).startswith(str(Path(root))) else plugin.name
        try:
            rel = os.path.relpath(plugin, root).replace("\\", "/")
        except ValueError:
            rel = plugin.name

        localized = tes4_is_localized(nodes)
        tables: dict[str, StringTable] = {}
        if localized:
            for kind, path in _find_string_tables(plugin).items():
                try:
                    tables[kind] = StringTable.load(path)
                except Exception as e:
                    logger.error("string table load %s: %s", path, e)

        out: list[TranslationString] = []
        for rec, _anc in iter_records(nodes):
            if rec.type == b"TES4":
                continue
            counts: dict[bytes, int] = {}
            for sub in rec.subs:
                if not _is_text_field(rec.type, sub.type):
                    continue
                idx = counts.get(sub.type, 0)
                counts[sub.type] = idx + 1

                text: str | None = None
                if localized and len(sub.data) == 4:
                    sid = struct.unpack_from("<I", sub.data, 0)[0]
                    if sid == 0:
                        continue
                    bucket = _string_table_bucket(sub.type)
                    table = tables.get(bucket) or tables.get("strings")
                    if table is None:
                        continue
                    text = table.strings.get(sid)
                else:
                    text = _decode_zstring(sub.data)

                if not text or _looks_like_asset_path(text):
                    continue

                form_hex = f"{rec.form_id:08X}"
                path = [
                    rec.type.decode("ascii", errors="replace"),
                    form_hex,
                    sub.type.decode("ascii", errors="replace"),
                    str(idx),
                ]
                ctx = f"{rec.type.decode(errors='replace')}:{form_hex} {sub.type.decode(errors='replace')}"
                out.append(self._mk(rel, path, text, ctx))
        return out

    # -- inject ------------------------------------------------------------

    def inject(self, root: str, translations: dict[str, str],
               target_lang: str | None = None,
               sub_paths: list[str] | None = None) -> int:
        self._current_root = root
        written = 0
        for plugin in iter_plugins(root, sub_paths):
            try:
                written += self._inject_plugin(root, plugin, translations)
            except Exception as e:
                logger.error("skyrim inject failed for %s: %s", plugin, e)
        try:
            written += self._inject_ui_translations(
                root, translations, target_lang, sub_paths)
        except Exception as e:
            logger.error("skyrim UI translations inject failed: %s", e)
        return written

    def _inject_ui_translations(
        self, root: str, translations: dict[str, str],
        target_lang: str | None, sub_paths: list[str] | None,
    ) -> int:
        """Write loose Interface/Translations/<mod>_<target>.txt overrides.

        BSA is never modified — Skyrim loads loose Data/ files over archives.
        """
        lang = _skyui_lang(target_lang)
        written = 0
        for tf in _collect_ui_translation_files(root, sub_paths):
            # Build new pair list; only count keys we actually changed / filled
            new_pairs: list[tuple[str, str]] = []
            file_hits = 0
            for key, val in tf.pairs:
                path = [key]
                sid = make_id(self.engine, tf.rel_file, path, val)
                if sid in translations:
                    new_val = translations[sid]
                    new_pairs.append((key, new_val))
                    if new_val != val:
                        file_hits += 1
                else:
                    new_pairs.append((key, val))
            if not file_hits:
                continue
            # Even if target is english, write override (user re-translated)
            out_name = f"{tf.mod_stem}_{lang}.txt"
            out_path = tf.write_dir / out_name
            try:
                os.makedirs(out_path.parent, exist_ok=True)
            except OSError as e:
                logger.error("mkdir %s: %s", out_path.parent, e)
                continue
            # Load-bearing for restore:
            #   * file already exists → reverse-patch backup (overwrite)
            #   * brand-new loose override → type=created so restore DELETES it
            #     (backup_file no-ops on missing paths and would leave the
            #     russian.txt orphan after "Restore")
            existed = out_path.is_file()
            if existed:
                self.backup_file(root, str(out_path))
            # Prefer source encoding (almost always utf-16-le for SkyUI)
            enc = tf.encoding if tf.encoding.startswith("utf-16") else "utf-16-le"
            payload = _encode_translation_file(new_pairs, enc)
            out_path.write_bytes(payload)
            written += file_hits
            if not existed:
                try:
                    from .base import update_metadata
                    import hashlib
                    rel = os.path.relpath(out_path, root).replace("\\", "/")
                    rel = _canonical_translation_rel(rel)
                    update_metadata(
                        root, rel,
                        orig_sha="",  # restore: type=created → delete file
                        mod_sha=hashlib.sha256(payload).hexdigest(),
                        backup_type="created",
                    )
                except Exception as e:
                    logger.error("register created UI override %s: %s", out_path, e)
        return written

    def _inject_plugin(self, root: str, plugin: Path,
                       translations: dict[str, str]) -> int:
        try:
            rel = os.path.relpath(plugin, root).replace("\\", "/")
        except ValueError:
            rel = plugin.name

        data = plugin.read_bytes()
        nodes = parse_plugin(data)
        localized = tes4_is_localized(nodes)

        if localized:
            return self._inject_localized(root, plugin, rel, nodes, translations)

        # --- inline zstring path ---
        written = 0
        for rec, ancestors in iter_records(nodes):
            if rec.type == b"TES4":
                continue
            counts: dict[bytes, int] = {}
            changed = False
            for sub in rec.subs:
                if not _is_text_field(rec.type, sub.type):
                    continue
                idx = counts.get(sub.type, 0)
                counts[sub.type] = idx + 1
                text = _decode_zstring(sub.data)
                if not text or _looks_like_asset_path(text):
                    continue
                form_hex = f"{rec.form_id:08X}"
                path = [
                    rec.type.decode("ascii", errors="replace"),
                    form_hex,
                    sub.type.decode("ascii", errors="replace"),
                    str(idx),
                ]
                sid = make_id(self.engine, rel, path, text)
                if sid not in translations:
                    continue
                new_text = translations[sid]
                if new_text == text:
                    continue
                sub.data = _encode_zstring(new_text)
                if len(sub.data) > 0xFFFF:
                    sub.oversized = True
                changed = True
                written += 1
            if changed:
                _mark_dirty(rec, ancestors)

        if written:
            self.backup_file(root, str(plugin))
            new_data = write_plugin(nodes)
            with open(plugin, "wb") as f:
                f.write(new_data)
        return written

    def _inject_localized(self, root: str, plugin: Path, rel: str,
                          nodes: list[Node],
                          translations: dict[str, str]) -> int:
        """Rewrite string tables; ESP subrecords keep their uint32 IDs."""
        table_paths = _find_string_tables(plugin)
        if not table_paths:
            logger.warning("localized plugin %s has no string tables; skip", rel)
            return 0
        tables: dict[str, StringTable] = {}
        for kind, path in table_paths.items():
            tables[kind] = StringTable.load(path)

        # Map id → (bucket, string_id) by re-walking extract logic
        written = 0
        dirty_kinds: set[str] = set()
        for rec, _anc in iter_records(nodes):
            if rec.type == b"TES4":
                continue
            counts: dict[bytes, int] = {}
            for sub in rec.subs:
                if not _is_text_field(rec.type, sub.type):
                    continue
                idx = counts.get(sub.type, 0)
                counts[sub.type] = idx + 1
                if len(sub.data) != 4:
                    continue
                str_id = struct.unpack_from("<I", sub.data, 0)[0]
                if str_id == 0:
                    continue
                bucket = _string_table_bucket(sub.type)
                table = tables.get(bucket) or tables.get("strings")
                if table is None:
                    continue
                text = table.strings.get(str_id)
                if not text or _looks_like_asset_path(text):
                    continue
                form_hex = f"{rec.form_id:08X}"
                path = [
                    rec.type.decode("ascii", errors="replace"),
                    form_hex,
                    sub.type.decode("ascii", errors="replace"),
                    str(idx),
                ]
                sid = make_id(self.engine, rel, path, text)
                if sid not in translations:
                    continue
                new_text = translations[sid]
                if new_text == text:
                    continue
                # Write into the table we actually read from
                for k, t in tables.items():
                    if str_id in t.strings and t.strings[str_id] == text:
                        t.strings[str_id] = new_text
                        dirty_kinds.add(k)
                        written += 1
                        break

        for kind in dirty_kinds:
            path = table_paths.get(kind)
            if path is None:
                continue
            self.backup_file(root, str(path))
            path.write_bytes(tables[kind].save())
        return written
