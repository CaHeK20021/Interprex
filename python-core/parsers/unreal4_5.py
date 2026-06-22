"""Unreal Engine 4/5 `.locres` parser — a SEPARATE engine from UE3 (`unreal`).

UE3 ships plain-text `.INT` INI files (handled by `unreal.py`). UE4/UE5 compile
localization into a *binary* `TextLocalizationResource`:
  <Game>/Content/Localization/<Target>/<lang>/<Target>.locres

Format (verified against akintos/UnrealLocres, which mirrors Epic's
TextLocalizationResource.cpp):

  Magic (16 bytes):  0E 14 74 75 67 4A 03 FC 4A 15 90 9D C3 37 7F 1B
                     (== FGuid(0x7574140E,0xFC034A67,0x9D90154A,0x1B7F37C3))
  Absent  -> Legacy (v0) file, no header.
  Version byte (ELocResVersion): 0 Legacy · 1 Compact · 2 Optimized (CRC32) ·
                                 3 Optimized_CityHash64_UTF16. Real games ~v3.

  Layout (v1+):
    magic, version,
    int64 offset to string table,
    [v>=2] int32 entry count,
    int32 namespace count
    per namespace: [v>=2] uint32 hash, FString name, int32 key count
      per key: [v>=2] uint32 hash, FString key, uint32 source-string hash,
               int32 index into string table
    string table @offset: int32 count, then each = FString value [+ [v>=2] int32 refcount]
  Legacy (v0): int32 namespace count; per key the value is stored inline as an
    FString instead of an index — no string table, no hashes.

  FString: int32 length. POSITIVE = ASCII/UTF-8 (1 byte/char); NEGATIVE = UTF-16LE,
    abs(len) = char count. Length INCLUDES the trailing null. Empty = length 0.

Load-bearing design decision (see CLAUDE.md): translating only ever changes string
VALUES — namespaces, keys, and every hash are immutable. So we carry the entire
namespace/key tree AND all hashes over BYTE-VERBATIM from the source file, and only
swap localized values in the string table. This sidesteps reimplementing
`FCrc::StrCrc32` and `CityHash64` in pure Python (the load-bearing reason a `.locres`
rewriter is otherwise hard) — we re-emit the same version we read.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path
import subprocess
import shutil
import os

def _run_cmd(args, **kwargs):
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000
    return subprocess.run(args, **kwargs)
import tempfile
import re
import functools

from .base import BaseParser, TranslationString

logger = logging.getLogger(__name__)

# FGuid(0x7574140E, 0xFC034A67, 0x9D90154A, 0x1B7F37C3) as the on-disk 16 bytes.
LOCRES_MAGIC = bytes([
    0x0E, 0x14, 0x74, 0x75, 0x67, 0x4A, 0x03, 0xFC,
    0x4A, 0x15, 0x90, 0x9D, 0xC3, 0x37, 0x7F, 0x1B,
])

# ELocResVersion
V_LEGACY = 0
V_COMPACT = 1       # string table (LUT), no hashes/refcounts
V_OPTIMIZED = 2     # + CRC32 hashes, entry count, per-string refcount
V_CITYHASH = 3      # + CityHash64/UTF-16 hashes (same byte layout as v2)

# UE4/5 culture codes (the <lang> folder). Fallback is a lowercased 2-letter code.
UE4_LANG_MAP = {
    "English": "en",
    "French": "fr",
    "German": "de",
    "Italian": "it",
    "Spanish": "es",
    "Spanish-LA": "es-419",
    "Portuguese": "pt",
    "Portuguese-BR": "pt-BR",
    "Russian": "ru",
    "Polish": "pl",
    "Czech": "cs",
    "Hungarian": "hu",
    "Turkish": "tr",
    "Japanese": "ja",
    "Korean": "ko",
    "Chinese": "zh-Hans",
    "Chinese-TW": "zh-Hant",
    "Arabic": "ar",
    "Dutch": "nl",
    "Danish": "da",
    "Finnish": "fi",
    "Norwegian": "no",
    "Swedish": "sv",
    "Ukrainian": "uk",
}


# ---------------------------------------------------------------------------
# FString codec
# ---------------------------------------------------------------------------

class FStr:
    """A decoded FString plus the exact bytes it occupied on disk (length prefix
    included). `raw` is what we re-emit verbatim for any value we don't change,
    which is what keeps untouched data byte-for-byte identical. `new_value`, when
    set, replaces it on serialize (only ever used for v0 inline values)."""
    __slots__ = ("text", "raw", "new_value")

    def __init__(self, text: str, raw: bytes):
        self.text = text
        self.raw = raw
        self.new_value: str | None = None


def read_fstring(buf: bytes, pos: int) -> tuple[FStr, int]:
    (length,) = struct.unpack_from("<i", buf, pos)
    p = pos + 4
    if length > 0:
        data = buf[p:p + length]
        p += length
        text = data.decode("utf-8", "replace")
        if text.endswith("\x00"):
            text = text[:-1]
    elif length < 0:
        n = -length
        data = buf[p:p + n * 2]
        p += n * 2
        text = data.decode("utf-16-le", "replace")
        if text.endswith("\x00"):
            text = text[:-1]
    else:
        text = ""
    return FStr(text, buf[pos:p]), p


def encode_fstring(s: str) -> bytes:
    """Encode a (new) string the way UE does: ASCII when every char fits in 7
    bits, else UTF-16LE; the trailing null is part of the payload and the count."""
    t = s + "\x00"
    if all(ord(c) < 128 for c in t):
        data = t.encode("ascii")
        return struct.pack("<i", len(data)) + data
    data = t.encode("utf-16-le")
    return struct.pack("<i", -(len(data) // 2)) + data


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

class STEntry:
    """One string-table slot. Existing slots carry `value`+`refcount_raw` verbatim;
    `new_value` (when set) means re-encode this slot with a translation. Appended
    slots have value=None and carry an integer `refcount`."""
    __slots__ = ("value", "refcount_raw", "new_value", "refcount")

    def __init__(self, value: FStr | None, refcount_raw: bytes | None,
                 new_value: str | None = None, refcount: int = 0):
        self.value = value
        self.refcount_raw = refcount_raw
        self.new_value = new_value
        self.refcount = refcount


class Key:
    __slots__ = ("hash_raw", "key", "source_hash_raw", "string_index", "value_inline")

    def __init__(self, hash_raw, key, source_hash_raw, string_index, value_inline):
        self.hash_raw = hash_raw            # bytes|None (v>=2)
        self.key = key                      # FStr
        self.source_hash_raw = source_hash_raw  # bytes (4)
        self.string_index = string_index    # int|None (v>=1)
        self.value_inline = value_inline    # FStr|None (v0)


class Namespace:
    __slots__ = ("hash_raw", "name", "keys")

    def __init__(self, hash_raw, name, keys):
        self.hash_raw = hash_raw            # bytes|None (v>=2)
        self.name = name                    # FStr
        self.keys = keys                    # list[Key]


class LocresModel:
    __slots__ = ("version", "entry_count_raw", "namespaces", "string_table")

    def __init__(self, version, entry_count_raw, namespaces, string_table):
        self.version = version
        self.entry_count_raw = entry_count_raw  # bytes|None (v>=2)
        self.namespaces = namespaces            # list[Namespace]
        self.string_table = string_table        # list[STEntry]|None (v>=1)


class LocresParseError(ValueError):
    """Exception raised when .locres parsing fails."""
    pass


def parse_locres(buf: bytes) -> LocresModel:
    try:
        pos = 0
        if buf[:16] == LOCRES_MAGIC:
            version = buf[16]
            pos = 17
        else:
            version = V_LEGACY

        entry_count_raw = None
        string_table: list[STEntry] | None = None

        if version >= V_COMPACT:
            (st_offset,) = struct.unpack_from("<q", buf, pos)
            pos += 8

            # Read the string table out-of-line, then return to the index.
            sp = st_offset
            (st_count,) = struct.unpack_from("<i", buf, sp)
            sp += 4
            string_table = []
            for _ in range(st_count):
                val, sp = read_fstring(buf, sp)
                refcount_raw = None
                if version >= V_OPTIMIZED:
                    refcount_raw = buf[sp:sp + 4]
                    sp += 4
                string_table.append(STEntry(val, refcount_raw))

            if version >= V_OPTIMIZED:
                entry_count_raw = buf[pos:pos + 4]
                pos += 4

        (ns_count,) = struct.unpack_from("<i", buf, pos)
        pos += 4

        namespaces: list[Namespace] = []
        for _ in range(ns_count):
            ns_hash = None
            if version >= V_OPTIMIZED:
                ns_hash = buf[pos:pos + 4]
                pos += 4
            name, pos = read_fstring(buf, pos)
            (key_count,) = struct.unpack_from("<i", buf, pos)
            pos += 4

            keys: list[Key] = []
            for _ in range(key_count):
                k_hash = None
                if version >= V_OPTIMIZED:
                    k_hash = buf[pos:pos + 4]
                    pos += 4
                key, pos = read_fstring(buf, pos)
                src_hash = buf[pos:pos + 4]
                pos += 4
                if version >= V_COMPACT:
                    (str_index,) = struct.unpack_from("<i", buf, pos)
                    pos += 4
                    keys.append(Key(k_hash, key, src_hash, str_index, None))
                else:
                    val, pos = read_fstring(buf, pos)
                    keys.append(Key(k_hash, key, src_hash, None, val))
            namespaces.append(Namespace(ns_hash, name, keys))

        return LocresModel(version, entry_count_raw, namespaces, string_table)
    except (struct.error, IndexError, UnicodeDecodeError, ValueError) as e:
        raise LocresParseError(f"Failed to parse locres: {e}") from e


def serialize_locres(m: LocresModel) -> bytes:
    out = bytearray()
    offset_pos = -1

    if m.version >= V_COMPACT:
        out += LOCRES_MAGIC
        out.append(m.version)
        offset_pos = len(out)
        out += b"\x00" * 8                  # placeholder int64 string-table offset
        if m.version >= V_OPTIMIZED:
            total_keys = sum(len(ns.keys) for ns in m.namespaces)
            out += struct.pack("<i", total_keys)

    out += struct.pack("<i", len(m.namespaces))
    for ns in m.namespaces:
        if m.version >= V_OPTIMIZED:
            out += ns.hash_raw
        out += ns.name.raw
        out += struct.pack("<i", len(ns.keys))
        for k in ns.keys:
            if m.version >= V_OPTIMIZED:
                out += k.hash_raw
            out += k.key.raw
            out += k.source_hash_raw
            if m.version >= V_COMPACT:
                out += struct.pack("<i", k.string_index)
            else:
                out += (encode_fstring(k.value_inline.new_value)
                        if k.value_inline.new_value is not None
                        else k.value_inline.raw)

    if m.version >= V_COMPACT:
        st_offset = len(out)
        out += struct.pack("<i", len(m.string_table))
        for e in m.string_table:
            if e.new_value is not None:
                out += encode_fstring(e.new_value)
            else:
                out += e.value.raw
            if m.version >= V_OPTIMIZED:
                out += (e.refcount_raw if e.refcount_raw is not None
                        else struct.pack("<i", e.refcount))
        struct.pack_into("<q", out, offset_pos, st_offset)

    return bytes(out)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def iter_locres_files(root: str):
    """Yield every `.locres` under root, skipping our own backup tree."""
    for f in Path(root).rglob("*.locres"):
        if ".interprex_backups" in f.parts:
            continue
        yield f


def iter_pak_files(root: str):
    """Yield `.pak` archives under root, skipping our own mod-paks and backups."""
    for f in Path(root).rglob("*.pak"):
        if ".interprex_backups" in f.parts:
            continue
        if f.stem.endswith("_P"):   # our own (or other) mod-paks — never a source
            continue
        yield f


# Source culture inside a .pak: the language we read English text FROM. UE stores
# many cultures per target; we only extract one so the table isn't 50x duplicated.
SOURCE_CULTURES = ("en-us", "en", "en-gb")

# Separator in `file` for pak-sourced strings: "<pak rel path>!<inner locres path>".
# Keeps the stable id unique per (pak, inner file) without colliding with loose mode.
PAK_SEP = "!"


def _inner_culture(inner_path: str) -> str | None:
    """Culture folder of an inner `.locres` path: .../Localization/<Target>/<cul>/x.locres."""
    parts = [p.lower() for p in inner_path.split("/")]
    if "localization" in parts:
        i = parts.index("localization")
        if i + 2 < len(parts):
            return parts[i + 2]
    # Fallback: check if any segment is a known culture code
    known = {c.lower() for c in list(UE4_LANG_MAP.values()) + list(SOURCE_CULTURES)}
    for part in parts:
        if part in known:
            return part
    return None


def _retarget_inner(inner_path: str, lang_code: str) -> str:
    """Swap the culture folder in an inner path to the target language."""
    parts = inner_path.split("/")
    if "Localization" in parts:
        i = parts.index("Localization")
        if i + 2 < len(parts):
            parts[i + 2] = lang_code
    return "/".join(parts)


def _is_translatable(value: str) -> bool:
    v = value.strip()
    if not v:
        return False
    # Must contain at least one letter
    if not any(c.isalpha() for c in v):
        return False
    # Exclude technical strings like {0}, [ITEM_001], etc.
    import re
    if re.match(r'^[\{\[\(][A-Z0-9_\-\s]+[\}\]\)]$', v):
        return False
    return True


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _find_retoc() -> str:
    """Find the retoc executable.
    Checks (in order):
      1. sys._MEIPASS/bin/  — PyInstaller ONEFILE unpacks datas/ here; __file__
         resolves into the same temp tree but its .parent.parent path is
         unreliable across Python versions, so we prefer the explicit _MEIPASS.
      2. python-core/bin/ relative to this source file — dev / editable installs.
      3. System PATH.
    Also verifies the found binary supports the 'to-zen' command."""
    import sys
    ext = ".exe" if sys.platform.startswith("win") else ""
    binary_name = f"retoc{ext}"

    candidates: list[Path] = []

    # 1. Frozen ONEFILE: _MEIPASS is the unpacked temp directory
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "bin" / binary_name)

    # 2. Dev mode: bin/ sits next to the python-core/ package root
    parser_dir = Path(__file__).resolve().parent
    core_dir = parser_dir.parent
    candidates.append(core_dir / "bin" / binary_name)

    path_to_try: str | None = None
    for c in candidates:
        if c.is_file():
            path_to_try = str(c)
            break

    if not path_to_try:
        # 3. System PATH
        path_to_try = shutil.which("retoc") or shutil.which(binary_name)

    if not path_to_try:
        raise RuntimeError(
            "The 'retoc' executable is required for Unreal Engine 5 IoStore support but was not found. "
            "Please ensure it is installed in your PATH or placed in 'python-core/bin/'."
        )
        
    # Verify the executable runs and supports 'to-zen'
    try:
        res = _run_cmd([path_to_try, "--help"], capture_output=True, text=True, check=False)
        help_text = res.stdout + res.stderr
        if "to-zen" not in help_text:
            raise RuntimeError(
                f"The found 'retoc' executable at '{path_to_try}' does not support the 'to-zen' command. "
                "Please update retoc to a newer version."
            )
    except Exception as e:
        if isinstance(e, RuntimeError):
            raise e
        raise RuntimeError(
            f"Failed to execute 'retoc' binary at '{path_to_try}': {e}. "
            "Please verify it is a valid executable."
        ) from e
        
    return path_to_try


@functools.lru_cache(maxsize=32)
def _detect_ue_version(utoc_path: str, retoc_bin: str) -> str:
    """Run 'retoc info' to detect the UE version by reading Toc version.
    Returns string like 'UE5_4', 'UE5_5', 'UE5_6'. Defaults to 'UE5_4'."""
    try:
        res = _run_cmd([retoc_bin, "info", "--path", utoc_path], capture_output=True, text=True, check=True)
        info_text = res.stdout
        if "ReplaceIoChunkHashWithIoHash" in info_text:
            return "UE5_6"
        elif "PartitionedToc" in info_text:
            return "UE5_5"
        return "UE5_4"
    except Exception as e:
        logger.warning(f"Failed to detect UE version for {utoc_path}: {e}. Defaulting to UE5_4.")
        return "UE5_4"


def iter_utoc_files(root: str):
    """Yield every `.utoc` container under root, skipping backups and patches."""
    for f in Path(root).rglob("*.utoc"):
        if ".interprex_backups" in f.parts:
            continue
        stem_lower = f.stem.lower()
        if stem_lower.endswith("_p"):  # skip custom patch/mod containers
            continue
        if stem_lower == "global":     # skip global engine/shader containers
            continue
        yield f


def _is_satisfactory_base_game_file(root: str, file_path: Path) -> bool:
    """Check if this is a Satisfactory base game file (resides in FactoryGame/Content/Paks but not in FactoryGame/Mods)"""
    root_path = Path(root).resolve()
    abs_file = file_path.resolve()
    
    # Check if FactoryGame is in the game root
    factory_game_dir = root_path / "FactoryGame"
    if not factory_game_dir.is_dir():
        return False
        
    # Check if the file is in FactoryGame/Content/Paks
    base_paks_dir = factory_game_dir / "Content" / "Paks"
    
    # Check if it's inside mods_dir
    mods_dir = factory_game_dir / "Mods"
    
    try:
        is_sub_of_base = abs_file.is_relative_to(base_paks_dir)
    except AttributeError:
        try:
            abs_file.relative_to(base_paks_dir)
            is_sub_of_base = True
        except ValueError:
            is_sub_of_base = False
            
    try:
        is_sub_of_mods = abs_file.is_relative_to(mods_dir)
    except AttributeError:
        try:
            abs_file.relative_to(mods_dir)
            is_sub_of_mods = True
        except ValueError:
            is_sub_of_mods = False
            
    path_parts_lower = [p.lower() for p in abs_file.parts]
    in_mods_folder = "mods" in path_parts_lower
    
    return is_sub_of_base and not is_sub_of_mods and not in_mods_folder


def _is_descendant_of_any(path_str: str, parent_set: set[str]) -> bool:
    """Check if path_str (e.g. 'FactoryGame/Mods/GameFeatures/ModA/...') starts with
    or equals any path in parent_set."""
    return any(path_str == p or path_str.startswith(p + "/") for p in parent_set)


def _is_translatable_uasset(inner_path: str) -> bool:
    """Check if this uasset is likely a recipe, item or buildable description."""
    path_lower = inner_path.lower()
    name = path_lower.split("/")[-1]
    
    # Check prefixes
    if name.startswith(("recipe_", "desc_", "build_", "schem_", "rec_")):
        return True
        
    # Check folder segments
    segments = set(path_lower.split("/"))
    if segments.intersection({"recipes", "items", "buildable", "schematics"}):
        return True
        
    return False


def _run_uasset_extractor(uasset_path: str) -> list[dict]:
    """Run UAssetExtractor.exe on a single file or directory and return JSON list."""
    import sys
    import json
    
    ext = ".exe" if sys.platform.startswith("win") else ""
    parser_dir = Path(__file__).resolve().parent
    core_dir = parser_dir.parent
    extractor_bin = core_dir / "bin" / f"UAssetExtractor{ext}"
    
    if not extractor_bin.is_file():
        logger.warning(f"UAssetExtractor not found at {extractor_bin}")
        return []
        
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_json = Path(tmp_dir) / "extracted.json"
        try:
            _run_cmd([
                str(extractor_bin),
                "--input", uasset_path,
                "--output", str(out_json),
                "--engine", "VER_UE5_4"
            ], check=True, capture_output=True, timeout=10)
            
            if out_json.is_file():
                with open(out_json, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"UAssetExtractor failed on {uasset_path}: {e}")
            
    return []


class UnrealEngine4_5Parser(BaseParser):
    engine = "unreal4_5"

    def engine_prompt_addon(self) -> str:
        return (
            "TECHNICAL STRINGS (UI / SUBTITLES): these strings come from a game's "
            "localization files and may contain format specifiers or markup.\n"
            "FORMAT SPECIFIERS: preserve %s, %d, %f, %i, {0}, {1}, {UserName}, "
            "{value} and similar patterns EXACTLY — they are filled in at runtime.\n"
            "ESCAPE SEQUENCES: keep literal \\n and \\t as-is; do NOT convert them "
            "into real newlines or tabs inside the JSON string.\n"
            "TONE: use a neutral, professional register suitable for UI labels, "
            "subtitles, and system messages. Avoid overly literary or conversational style."
        )

    @staticmethod
    def detect(root: str) -> bool:
        # Unreal signatures: if it contains any .uplugin, .pak, or .uasset, it's Unreal.
        try:
            for f in Path(root).rglob("*"):
                if f.is_file() and f.suffix.lower() in (".uplugin", ".pak", ".uasset"):
                    return True
        except Exception:
            pass

        # Loose .locres on disk.
        for f in iter_locres_files(root):
            try:
                content = f.read_bytes()
            except Exception:
                continue
            if content[:16] == LOCRES_MAGIC:
                return True
            # Legacy (v0) files have no magic; accept if it parses cleanly.
            try:
                parse_locres(content)
                return True
            except Exception:
                continue
        # Packed: a .pak containing .locres (shipped UE4/5 games like Satisfactory).
        from . import pak as pakmod
        for pf in iter_pak_files(root):
            try:
                if pakmod.read_pak(str(pf)):
                    return True
            except Exception:
                continue
        # Packed .utoc containing .locres
        try:
            retoc_bin = _find_retoc()
            utocs = list(iter_utoc_files(root))
            list_re = re.compile(r"^(?P<chunk_id>[0-9a-fA-F]+)\s+.*?\s+(?P<inner_path>.*\.locres)$")
            for uf in utocs[:5]:
                try:
                    res = _run_cmd([retoc_bin, "list", "--path", str(uf)], capture_output=True, text=True, timeout=5)
                    for line in res.stdout.splitlines():
                        if list_re.match(line.strip()):
                            return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _entry_value(self, m: LocresModel, k: Key) -> str:
        if m.version >= V_COMPACT:
            return m.string_table[k.string_index].value.text
        return k.value_inline.text

    def _extract_model(self, m: LocresModel, file_label: str,
                       out: list[TranslationString]) -> None:
        for ns in m.namespaces:
            for k in ns.keys:
                value = self._entry_value(m, k)
                if not _is_translatable(value):
                    continue
                out.append(self._mk(
                    file=file_label,
                    path=[ns.name.text, k.key.text],
                    original=value,
                    context=f"Namespace: {ns.name.text} | Key: {k.key.text}",
                ))

    def extract(self, root: str, sub_paths: list[str] | None = None) -> list[TranslationString]:
        # Loose .locres on disk.
        files = list(iter_locres_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            files = [f for f in files if _is_descendant_of_any(f.relative_to(root).as_posix(), wanted)]

        strings: list[TranslationString] = []
        for f in files:
            rel = f.relative_to(root).as_posix()
            try:
                m = parse_locres(f.read_bytes())
            except Exception as e:
                logger.error(f"Failed to parse {rel}: {e}")
                continue
            self._extract_model(m, rel, strings)

        # Packed .pak & .utoc (only when there were no loose .locres — a shipped game).
        if not files:
            self._extract_from_paks(root, sub_paths, strings)
            self._extract_from_utocs(root, sub_paths, strings)

        # uassets are ALWAYS extracted — mods without locres (pure TextProperty uassets)
        # need this path even when other mods contributed locres strings above.
        self._extract_from_uassets(root, sub_paths, strings)
        return strings

    def _extract_from_utocs(self, root: str, sub_paths, out: list[TranslationString]) -> None:
        """Extract .locres files from .utoc containers under root using retoc."""
        try:
            retoc_bin = _find_retoc()
        except RuntimeError as e:
            logger.warning(f"Skipping IoStore extraction: {e}")
            return
            
        utocs = list(iter_utoc_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            utocs = [u for u in utocs if _is_descendant_of_any(u.relative_to(root).as_posix(), wanted)]
            
        list_re = re.compile(r"^(?:\S+\s+)?(?P<chunk_id>[0-9a-fA-F]{16,32})\s+.*?\s+(?P<inner_path>.*\.locres)$")
        
        for uf in utocs:
            utoc_rel = uf.relative_to(root).as_posix()
            try:
                res = _run_cmd([retoc_bin, "list", "--path", str(uf)], capture_output=True, text=True, check=True)
            except Exception as e:
                logger.error(f"Failed to list utoc {utoc_rel}: {e}")
                continue
                
            entries = []
            for line in res.stdout.splitlines():
                m = list_re.match(line.strip())
                if m:
                    entries.append(m.groupdict())
                    
            if not entries:
                continue
                
            with tempfile.TemporaryDirectory() as tmp_dir:
                for entry in entries:
                    chunk_id = entry["chunk_id"]
                    inner_path = entry["inner_path"].replace("\\", "/")
                    
                    cul = _inner_culture(inner_path)
                    if cul is not None and cul not in SOURCE_CULTURES:
                        continue
                        
                    temp_file = Path(tmp_dir) / f"{chunk_id}.locres"
                    try:
                        _run_cmd([retoc_bin, "get", str(uf), chunk_id, str(temp_file)], check=True, capture_output=True)
                        if temp_file.is_file():
                            locres_data = temp_file.read_bytes()
                            m = parse_locres(locres_data)
                            self._extract_model(m, f"{utoc_rel}{PAK_SEP}{inner_path}", out)
                    except Exception as e:
                        logger.error(f"Failed to get chunk {chunk_id} from {utoc_rel}: {e}")

    def _extract_from_paks(self, root: str, sub_paths, out: list[TranslationString]) -> None:
        from . import pak as pakmod
        paks = list(iter_pak_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            paks = [p for p in paks if _is_descendant_of_any(p.relative_to(root).as_posix(), wanted)]
        for pf in paks:
            pak_rel = pf.relative_to(root).as_posix()
            try:
                inner_files = pakmod.read_pak(str(pf))
            except Exception as e:
                logger.error(f"Failed to read pak {pak_rel}: {e}")
                continue
            for inf in inner_files:
                cul = _inner_culture(inf.path)
                # Only read the source culture (else 50x duplicate strings).
                if cul is not None and cul not in SOURCE_CULTURES:
                    continue
                try:
                    m = parse_locres(inf.data)
                except Exception as e:
                    logger.error(f"Failed to parse {inf.path} in {pak_rel}: {e}")
                    continue
                self._extract_model(m, f"{pak_rel}{PAK_SEP}{inf.path}", out)

    def _extract_from_uassets(self, root: str, sub_paths, out: list[TranslationString]) -> None:
        """Extract translatable strings from .uasset files inside .pak or .utoc containers."""
        from . import pak as pakmod
        import tempfile
        import re
        
        paks = list(iter_pak_files(root))
        utocs = list(iter_utoc_files(root))
        
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            paks = [p for p in paks if _is_descendant_of_any(p.relative_to(root).as_posix(), wanted)]
            utocs = [u for u in utocs if _is_descendant_of_any(u.relative_to(root).as_posix(), wanted)]

        def get_mod_rel_path(fpath: Path) -> str:
            # Check if this is a nested game feature or general mod
            for parent in fpath.parents:
                parent_name = parent.name.lower()
                if parent_name == "mods" or parent_name == "gamefeatures":
                    child = fpath
                    for p in fpath.parents:
                        if p == parent:
                            return child.relative_to(root).as_posix()
                        child = p
            return fpath.parent.relative_to(root).as_posix()

        # 1. Extract from .pak files
        for pf in paks:
            pak_rel = pf.relative_to(root).as_posix()
            mod_rel = get_mod_rel_path(pf)
            try:
                inner_files = pakmod.read_pak(str(pf), want_suffix=".uasset")
            except Exception as e:
                logger.error(f"Failed to read pak {pak_rel} for uassets: {e}")
                continue
                
            for inf in inner_files:
                if not _is_translatable_uasset(inf.path):
                    continue
                with tempfile.TemporaryDirectory() as tmp_dir:
                    temp_uasset = Path(tmp_dir) / Path(inf.path).name
                    temp_uasset.write_bytes(inf.data)
                    
                    extracted = _run_uasset_extractor(str(temp_uasset))
                    for item in extracted:
                        out.append(self._mk(
                            file=f"uasset://{mod_rel}{PAK_SEP}{inf.path}",
                            path=[item["InternalPath"], item["PropName"]],
                            original=item["Value"],
                            context=f"Class: {item['AssetClass']} | Property: {item['PropName']}"
                        ))

        # 2. Extract from .utoc/ucas files via retoc to-legacy (assembles proper .uasset files)
        try:
            retoc_bin = _find_retoc()
        except RuntimeError:
            return

        root_path = Path(root)
        global_utoc = root_path / "FactoryGame" / "Content" / "Paks" / "global.utoc"
        global_ucas = root_path / "FactoryGame" / "Content" / "Paks" / "global.ucas"

        # Batch ALL mods into ONE retoc to-legacy call — avoids N slow copies of global containers.
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_input = Path(tmp_dir) / "input"
            tmp_output = Path(tmp_dir) / "output"
            tmp_input.mkdir()
            tmp_output.mkdir()

            # Symlink or copy global containers once
            for name, src in [("global.utoc", global_utoc), ("global.ucas", global_ucas)]:
                dst = tmp_input / name
                if src.is_file():
                    try:
                        os.symlink(str(src), str(dst))
                    except OSError:
                        shutil.copy2(str(src), str(dst))

            # Collect all mod .utoc/.ucas into one input dir
            utoc_map: dict[str, Path] = {}  # utoc filename -> original path (for relative lookup)
            for uf in utocs:
                shutil.copy2(str(uf), str(tmp_input / uf.name))
                utoc_map[uf.name] = uf
                ucas = uf.with_suffix(".ucas")
                if ucas.is_file():
                    shutil.copy2(str(ucas), str(tmp_input / ucas.name))

            if not utoc_map:
                pass  # no utocs, skip
            else:
                # Detect UE version from first utoc
                first_utoc = next(iter(utoc_map.values()))
                ue_ver = _detect_ue_version(str(first_utoc), retoc_bin)

                try:
                    _run_cmd([
                        retoc_bin, "to-legacy", str(tmp_input), str(tmp_output),
                        "--version", ue_ver, "--verbose"
                    ], check=True, capture_output=True)
                except Exception as e:
                    logger.error(f"retoc to-legacy failed for batch: {e}")
                    return

                # Map extracted uassets back to their source mod
                for extracted_uasset in tmp_output.rglob("*.uasset"):
                    inner_path = extracted_uasset.relative_to(tmp_output).as_posix()
                    if not _is_translatable_uasset(inner_path):
                        continue
                    # Compute mod_rel from the inner_path (retoc preserves directory structure)
                    # e.g. "FactoryGame/Mods/ModName/Content/File.uasset" -> "FactoryGame/Mods/ModName"
                    mod_rel = inner_path.rsplit("/", 1)[0] if "/" in inner_path else inner_path
                    # Walk up to find the mod root (parent of "Content" or similar)
                    parts = inner_path.split("/")
                    for i, part in enumerate(parts):
                        if part.lower() == "content" and i > 0:
                            mod_rel = "/".join(parts[:i])
                            break
                    try:
                        extracted = _run_uasset_extractor(str(extracted_uasset))
                        for item in extracted:
                            out.append(self._mk(
                                file=f"uasset://{mod_rel}{PAK_SEP}{inner_path}",
                                path=[item["InternalPath"], item["PropName"]],
                                original=item["Value"],
                                context=f"Class: {item['AssetClass']} | Property: {item['PropName']}"
                            ))
                    except Exception as e:
                        logger.error(f"Failed to run UAssetExtractor on {inner_path}: {e}")

    def _target_path(self, source: Path, lang_code: str) -> Path:
        """Sibling language folder: .../Localization/<Target>/<lang>/<file>.locres.
        Only the language directory (the file's parent) changes."""
        return source.parent.parent / lang_code / source.name

    def inject(self, root: str, translations: dict[str, str],
               target_lang: str | None = None,
               sub_paths: list[str] | None = None) -> int:
        if not target_lang:
            return 0
        lang_code = UE4_LANG_MAP.get(target_lang) or target_lang[:2].lower()

        files = list(iter_locres_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            files = [f for f in files if _is_descendant_of_any(f.relative_to(root).as_posix(), wanted)]

        if not files:
            sml_files: dict[str, bytes] = {}
            paks_written = self._inject_into_paks(root, translations, lang_code, sub_paths, sml_files)
            utocs_written = self._inject_into_utocs(root, translations, lang_code, sub_paths, sml_files)
            
            if sml_files:
                self._write_sml_plugin(root, sml_files)
                
            written = paks_written + utocs_written
            written += self._inject_into_uassets(root, translations, sub_paths)
            return written

        written = 0
        for f in files:
            rel = f.relative_to(root).as_posix()
            try:
                m = parse_locres(f.read_bytes())
                written += self._apply(m, rel, translations)
                target = self._target_path(f, lang_code)
                if target.exists():
                    self.backup_file(root, str(target))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(serialize_locres(m))
            except Exception as e:
                logger.error(f"Failed to inject into {rel}: {e}")
        return written

    def _inject_into_uassets(self, root: str, translations: dict[str, str], sub_paths) -> int:
        """Generate ContentLib JSON patch files based on translated uasset strings."""
        import json
        import hashlib
        from .base import update_metadata, make_id
        
        # 1. Re-extract strings to know their IDs and metadata.
        # Use a cache to avoid running retoc to-legacy a second time.
        cache_key = (root, tuple(sub_paths) if sub_paths else None)
        if not hasattr(self, '_uasset_cache') or self._uasset_cache_key != cache_key:
            self._uasset_cache = []
            self._extract_from_uassets(root, sub_paths, self._uasset_cache)
            self._uasset_cache_key = cache_key
        uasset_strings = self._uasset_cache
        
        # Group by mod and asset target path
        # mod_name -> { "recipes": { internal_path -> patch_dict }, "items": { internal_path -> patch_dict } }
        patches_by_mod = {}
        
        written_count = 0
        for s in uasset_strings:
            string_id = make_id(self.engine, s.file, s.path, s.original)
            if string_id in translations:
                translated_text = translations[string_id]
                if s.file.startswith("uasset://"):
                    parts = s.file[9:].split(PAK_SEP)
                    mod_rel = parts[0]
                    mod_name = mod_rel.split("/")[-1]
                    
                    if mod_name not in patches_by_mod:
                        patches_by_mod[mod_name] = {
                            "recipes": {},
                            "items": {}
                        }
                        
                    internal_path = s.path[0]
                    prop_name = s.path[1]
                    
                    # Decide folder category (RecipePatches vs ItemPatches)
                    is_recipe = "recipe" in internal_path.lower()
                    category = "recipes" if is_recipe else "items"
                    
                    target_map = patches_by_mod[mod_name][category]
                    if internal_path not in target_map:
                        # ContentLib full class path: /Game/.../AssetName.AssetName_C
                        asset_name = internal_path.split("/")[-1]
                        target_map[internal_path] = {
                            "_target_comment": f"//{internal_path}.{asset_name}_C"
                        }
                        
                    # Map to ContentLib schema keys
                    prop_lower = prop_name.lower()
                    if "displayname" in prop_lower or prop_lower == "name":
                        target_map[internal_path]["Name"] = translated_text
                    elif "description" in prop_lower:
                        target_map[internal_path]["Description"] = translated_text
                    elif "tooltip" in prop_lower:
                        target_map[internal_path]["Tooltip"] = translated_text
                    elif "flavor" in prop_lower or "longdescription" in prop_lower:
                        target_map[internal_path]["LongDescription"] = translated_text
                    elif "preunlock" in prop_lower and "name" in prop_lower:
                        target_map[internal_path]["PreUnlockDisplayName"] = translated_text
                    elif "preunlock" in prop_lower and "desc" in prop_lower:
                        target_map[internal_path]["PreUnlockDescription"] = translated_text
                    elif "postunlock" in prop_lower and "desc" in prop_lower:
                        target_map[internal_path]["PostUnlockDescription"] = translated_text
                    # else: skip unknown props (enum values, struct fields like DoorMode/Frame/Sound)
                        
                    written_count += 1
                    
        # 2. Write patch files and register in backups as 'created'
        for mod_name, categories in patches_by_mod.items():
            for category, target_map in categories.items():
                folder_name = "RecipePatches" if category == "recipes" else "ItemPatches"
                dest_dir = os.path.join(root, "FactoryGame", "Configs", "ContentLib", folder_name)
                os.makedirs(dest_dir, exist_ok=True)
                
                for internal_path, patch_data in target_map.items():
                    # Generate safe file name from internal path
                    asset_name = internal_path.split("/")[-1]
                    file_name = f"Patch_{mod_name}_{asset_name}.json"
                    file_path = os.path.join(dest_dir, file_name)
                    rel_to_root = os.path.relpath(file_path, root).replace("\\", "/")
                    
                    try:
                        comment = patch_data.pop("_target_comment", "")
                        with open(file_path, "w", encoding="utf-8") as f:
                            if comment:
                                f.write(comment + "\n")
                            json.dump(patch_data, f, indent=4, ensure_ascii=False)
                                
                        # Register in backup system as 'created' (to support Restore Backup)
                        mod_bytes = (comment + "\n" + json.dumps(patch_data, ensure_ascii=False)).encode("utf-8")
                        mod_sha = hashlib.sha256(mod_bytes).hexdigest()
                        update_metadata(root, rel_to_root, "", mod_sha, "created")
                    except Exception as e:
                        logger.error(f"Failed to write ContentLib patch {file_name}: {e}")
                        
        return written_count

    def _inject_into_paks(self, root: str, translations: dict[str, str],
                          lang_code: str, sub_paths, sml_files: dict[str, bytes] | None = None) -> int:
        """Write translated .locres into a NEW uncompressed mod-pak per source pak
        (`<name>_<lang>_P.pak`), leaving the original untouched. Inner files are
        retargeted to the chosen culture so the game loads them as that language."""
        from . import pak as pakmod
        paks = list(iter_pak_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            paks = [p for p in paks if _is_descendant_of_any(p.relative_to(root).as_posix(), wanted)]

        written = 0
        for pf in paks:
            pak_rel = pf.relative_to(root).as_posix()
            try:
                inner_files = pakmod.read_pak(str(pf))
            except Exception as e:
                logger.error(f"Failed to read pak {pak_rel}: {e}")
                continue

            out_files: dict[str, bytes] = {}
            for inf in inner_files:
                cul = _inner_culture(inf.path)
                if cul is not None and cul not in SOURCE_CULTURES:
                    continue
                try:
                    m = parse_locres(inf.data)
                except Exception:
                    continue
                n = self._apply(m, f"{pak_rel}{PAK_SEP}{inf.path}", translations)
                if n == 0:
                    continue
                written += n
                out_files[_retarget_inner(inf.path, lang_code)] = serialize_locres(m)

            if not out_files:
                continue

            if _is_satisfactory_base_game_file(root, pf):
                if sml_files is not None:
                    sml_files.update(out_files)
            else:
                mod_pak = pf.with_name(f"{pf.stem}_{lang_code}_P.pak")
                if mod_pak.exists():
                    self.backup_file(root, str(mod_pak))
                pakmod.write_pak(str(mod_pak), out_files)
                logger.info("Wrote mod-pak %s (%d files)", mod_pak.name, len(out_files))
        return written

    def _inject_into_utocs(self, root: str, translations: dict[str, str],
                           lang_code: str, sub_paths, sml_files: dict[str, bytes] | None = None) -> int:
        """Inject translations into .utoc containers by extracting, modifying, and rebuilding
        them as a Zen patch container next to the original files using retoc to-zen."""
        try:
            retoc_bin = _find_retoc()
        except RuntimeError as e:
            logger.warning(f"Skipping IoStore injection: {e}")
            return 0
            
        utocs = list(iter_utoc_files(root))
        if sub_paths:
            wanted = {Path(p).as_posix() for p in sub_paths}
            utocs = [u for u in utocs if _is_descendant_of_any(u.relative_to(root).as_posix(), wanted)]
            
        list_re = re.compile(r"^(?P<chunk_id>[0-9a-fA-F]+)\s+.*?\s+(?P<inner_path>.*\.locres)$")
        written = 0
        
        for uf in utocs:
            utoc_rel = uf.relative_to(root).as_posix()
            try:
                res = _run_cmd([retoc_bin, "list", "--path", str(uf)], capture_output=True, text=True, check=True)
            except Exception as e:
                logger.error(f"Failed to list utoc {utoc_rel}: {e}")
                continue
                
            entries = []
            for line in res.stdout.splitlines():
                m = list_re.match(line.strip())
                if m:
                    entries.append(m.groupdict())
                    
            if not entries:
                continue
                
            out_files: dict[str, bytes] = {}
            with tempfile.TemporaryDirectory() as tmp_extract_dir:
                for entry in entries:
                    chunk_id = entry["chunk_id"]
                    inner_path = entry["inner_path"].replace("\\", "/")
                    
                    cul = _inner_culture(inner_path)
                    if cul is not None and cul not in SOURCE_CULTURES:
                        continue
                        
                    temp_file = Path(tmp_extract_dir) / f"{chunk_id}.locres"
                    try:
                        _run_cmd([retoc_bin, "get", str(uf), chunk_id, str(temp_file)], check=True, capture_output=True)
                        if temp_file.is_file():
                            locres_data = temp_file.read_bytes()
                            m = parse_locres(locres_data)
                            n = self._apply(m, f"{utoc_rel}{PAK_SEP}{inner_path}", translations)
                            if n > 0:
                                written += n
                                target_inner = _retarget_inner(inner_path, lang_code)
                                out_files[target_inner] = serialize_locres(m)
                    except Exception as e:
                        logger.error(f"Failed to process chunk {chunk_id} in {utoc_rel}: {e}")
                        
            if not out_files:
                continue
                
            if _is_satisfactory_base_game_file(root, uf):
                if sml_files is not None:
                    sml_files.update(out_files)
            else:
                ue_ver = _detect_ue_version(str(uf), retoc_bin)
                
                with tempfile.TemporaryDirectory() as tmp_zen_dir:
                    for inner_path, data in out_files.items():
                        target_file = Path(tmp_zen_dir) / inner_path.lstrip("/")
                        target_file.parent.mkdir(parents=True, exist_ok=True)
                        target_file.write_bytes(data)
                        
                    patch_base = uf.parent / f"{uf.stem}_P"
                    patch_utoc = uf.parent / f"{uf.stem}_P.utoc"
                    patch_ucas = uf.parent / f"{uf.stem}_P.ucas"
                    patch_pak = uf.parent / f"{uf.stem}_P.pak"
                    
                    for p_file in (patch_utoc, patch_ucas, patch_pak):
                        if p_file.exists():
                            self.backup_file(root, str(p_file))
                            
                    try:
                        _run_cmd([
                            retoc_bin, "to-zen",
                            "--version", ue_ver,
                            str(tmp_zen_dir),
                            str(patch_base)
                        ], check=True, capture_output=True)
                        
                        missing = []
                        for p_file in (patch_utoc, patch_ucas, patch_pak):
                            if not p_file.exists() or p_file.stat().st_size == 0:
                                missing.append(p_file.name)
                        if missing:
                            raise RuntimeError(
                                f"retoc to-zen did not generate valid output files: {', '.join(missing)}"
                            )
                        logger.info(f"Wrote Zen patch container {patch_base.name} (files: {list(out_files.keys())})")
                    except Exception as e:
                        logger.error(f"Failed to compile Zen patch container for {utoc_rel}: {e}")
                        raise
        return written

    def _write_sml_plugin(self, root: str, out_files: dict[str, bytes]) -> None:
        """Helper to write a Satisfactory Mod Loader (SML) plugin under FactoryGame/Mods/InterprexTranslation/"""
        from . import pak as pakmod
        
        mods_dir = Path(root) / "FactoryGame" / "Mods"
        plugin_dir = mods_dir / "InterprexTranslation"
        paks_dir = plugin_dir / "Content" / "Paks"
        
        pak_path = paks_dir / "InterprexTranslation.pak"
        if pak_path.exists():
            self.backup_file(root, str(pak_path))
            
        if plugin_dir.exists():
            shutil.rmtree(plugin_dir)
            
        paks_dir.mkdir(parents=True, exist_ok=True)
        
        manifest_path = plugin_dir / "InterprexTranslation.uplugin"
        manifest_data = {
            "FileVersion": 3,
            "Version": 1,
            "VersionName": "1.0.0",
            "SemVersion": "1.0.0",
            "FriendlyName": "Interprex Translation",
            "Description": "Injected localization translations generated by Interprex",
            "Category": "Localization",
            "CreatedBy": "Interprex",
            "CreatedByURL": "",
            "DocsURL": "",
            "MarketplaceURL": "",
            "SupportURL": "",
            "CanContainContent": True,
            "IsBetaVersion": False,
            "IsExperimentalVersion": False,
            "Installed": False
        }
        import json
        manifest_path.write_text(json.dumps(manifest_data, indent=4), encoding="utf-8")
        
        pakmod.write_pak(str(pak_path), out_files)
        
        sig_path = paks_dir / "InterprexTranslation.sig"
        sig_path.write_bytes(b"")
        
        logger.info("Wrote SML plugin at %s (%d files)", plugin_dir.name, len(out_files))

    def _apply(self, m: LocresModel, rel: str, translations: dict[str, str]) -> int:
        """Swap in translated values. Returns the number of keys changed.

        v0 has no shared table — each key owns its value, so a translation is a
        direct in-place swap. v1+ deduplicates values into a string table, so a
        single slot can back several keys: if they all want the same text (or only
        one wants a change) we edit the slot in place; if keys sharing a slot want
        DIFFERENT translations we split — append new slots and repoint just those
        keys, leaving the original slot (and every untouched slot) byte-verbatim."""
        # Legacy: inline values, no dedup.
        if m.version < V_COMPACT:
            n = 0
            for ns in m.namespaces:
                for k in ns.keys:
                    sid = self._mk(file=rel, path=[ns.name.text, k.key.text],
                                   original=k.value_inline.text).id
                    if sid in translations:
                        k.value_inline.new_value = translations[sid]
                        n += 1
            return n

        # v1+: group keys by the table slot they reference.
        users: dict[int, list[tuple[Key, str | None]]] = {}
        for ns in m.namespaces:
            for k in ns.keys:
                value = m.string_table[k.string_index].value.text
                sid = self._mk(file=rel, path=[ns.name.text, k.key.text],
                               original=value).id
                users.setdefault(k.string_index, []).append((k, translations.get(sid)))

        base = len(m.string_table)
        appended: list[STEntry] = []
        append_by_value: dict[str, int] = {}  # value -> absolute index in (table+appended)
        written = 0

        for idx, group in users.items():
            translated = [(k, t) for (k, t) in group if t is not None]
            keepers = [k for (k, t) in group if t is None]
            if not translated:
                continue

            distinct = {t for (_k, t) in translated}
            if not keepers and len(distinct) == 1:
                # Whole slot becomes the single translation — edit in place.
                m.string_table[idx].new_value = next(iter(distinct))
                written += len(translated)
                continue

            # Split: keepers stay on `idx` (its original value is preserved
            # verbatim); each translated key points at a new/append slot.
            for k, t in translated:
                ni = append_by_value.get(t)
                if ni is None:
                    ni = base + len(appended)
                    append_by_value[t] = ni
                    appended.append(STEntry(value=None, refcount_raw=None,
                                            new_value=t, refcount=1))
                else:
                    appended[ni - base].refcount += 1
                k.string_index = ni
                written += 1

        m.string_table.extend(appended)
        return written
