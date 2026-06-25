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
import hashlib

def _run_cmd(args, **kwargs):
    if os.name == 'nt':
        kwargs['creationflags'] = 0x08000000
    return subprocess.run(args, **kwargs)
import tempfile
import re
import functools

from .base import BaseParser, TranslationString

# Security limits
MAX_UASSET_SIZE = int(1.2 * 1024 * 1024 * 1024)  # 1.2 GB

# Part B (CDO full-array-replace) emit toggle. OFF: verified on a real Satisfactory
# run that the patches apply at the ContentLib layer but the game still renders
# English for these struct-array FText (selector options, MkPlus subsystem build
# descriptions), so they're inert clutter. The build/serialize path is kept (it's
# correct) but no patches are written. Flip to True only if an engine-side path is
# found that actually surfaces these. See the long note in `_inject_into_uassets`.
_ENABLE_CDO_ARRAY_REPLACE = False

# Byte-patch path: rewrite struct-array FText directly in the .uasset bytes and
# repack the mod as a `_P` container (bypasses ContentLib, which can't surface
# these). This is the working replacement for the disabled CDO array-replace.
_ENABLE_ASSET_BYTEPATCH = True


def _sanitize_extracted_path(tmp_output: Path, file_path: Path) -> str | None:
    """Return the relative path of file_path inside tmp_output, or None if it escapes."""
    try:
        resolved = file_path.resolve()
        base = tmp_output.resolve()
        if not str(resolved).startswith(str(base)):
            return None  # path traversal attempt
        return resolved.relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


def _mod_path_from_utoc(uf: Path, root: str) -> str:
    """Derive the mod's relative path (e.g. 'FactoryGame/Mods/ModName') from a .utoc file path."""
    rel = uf.relative_to(root).as_posix()
    parts = rel.split("/")
    for i, part in enumerate(parts):
        if part.lower() == "content" and i > 0:
            return "/".join(parts[:i])
    return str(Path(rel).parent.as_posix())


def _build_uasset_path_key(item: dict) -> list[str]:
    """Build a UNIQUE address inside one .uasset for a translatable string.

    `(InternalPath, PropName)` alone is NOT unique: one asset routinely holds
    dozens of strings under the same prop name — a widget with many separate
    `Text` exports, or a struct-array (MkPlus subsystem: dozens of `Desc` inside
    struct elements of `droneStation`/`generator`/... arrays, PLUS single struct
    props like `drone`). Without a discriminator they all collapse onto one stable
    id and most translations are lost on extract (measured: 557/3859 rows). The
    discriminator combines:
      - ExportName   — the export the value lives in (e.g. ApplyBtnText); this
        separates widget Text exports (case A).
      - ContainerPath — the FULL nesting address inside the export, e.g.
        "droneStation[0]" or "drone" or "x.Ingredients[1]". This is what pins a
        struct-array / nested-struct value (case B); ExportName alone can't —
        every subsystem element shares one export (Default__..._C).

    MUST stay byte-identical to the inject reader's expectation (path[0]=internal,
    path[1]=prop, path[2]=discriminator). Keep the two extract call sites using
    THIS helper so the id never drifts between them.
    """
    seg = item.get("ExportName") or ""
    cont = item.get("ContainerPath") or ""
    base = [item["InternalPath"], item["PropName"]]
    if cont:
        # nested value (struct / array element): export + full container address.
        return base + [f"{seg}|{cont}"]
    if seg:
        return base + [seg]
    return base


# --- ContentLib patch routing ------------------------------------------------
# WHICH ContentLib patch folder an asset goes to is decided by its PARENT UE class
# (the same type ContentLib gates on: "Was not FGRecipe after loading"), NOT by its
# filename. Routing by name over-matched categories/input-actions that merely sit in
# a /Recipes/ folder or contain "Recipe" (CAT_PP, RecipeCatUpgrade, IA_Smart_Recipe*)
# -> they failed as RecipePatches. The parent class (Program.cs SuperClass) is the
# ground truth; HasIngredientsAndProduct is the fallback when retoc erased the parent.
_RECIPE_SUPERS = {"FGRecipe"}
_CATEGORY_SUPERS = {
    "FGItemCategory", "FGCategory", "FGRecipeCategory",
    "FGBuildCategory", "FGBuildableCategory",
}
_ITEM_SUPERS = {"FGItemDescriptor"}
# parents retoc to-legacy erased (direct base-game inheritance) -> use prop fallback
_ERASED_SUPERS = {"", "UnknownExport", "<null>", "None"}


def _route_patch_kind(super_class: str, has_ing_prod: bool, asset_leaf: str) -> str:
    """Decide the ContentLib patch kind ("recipes"|"items"|"cdos") from the asset's
    PARENT UE class (ground truth), with a property-set fallback for assets whose
    parent retoc erased to UnknownExport. asset_leaf (lowercased asset name) is used
    ONLY as a last-resort name heuristic so an old/missing-metadata build degrades to
    the previous behaviour instead of mis-routing."""
    sc = (super_class or "").strip()
    if sc in _RECIPE_SUPERS:
        return "recipes"
    if sc in _CATEGORY_SUPERS:
        # ContentLib has no category patch; CDO patches mDisplayName by raw name with
        # no type gate -> the right home for a category caption.
        return "cdos"
    if sc in _ITEM_SUPERS:
        return "items"
    if sc in _ERASED_SUPERS:
        if has_ing_prod:
            return "recipes"            # Recipe_MK*, Rec_packing* (erased but real recipes)
        if asset_leaf.startswith("desc_"):
            return "items"
        if asset_leaf.startswith("recipe_"):
            return "recipes"
        return "cdos"                   # CAT_PP, IA_Smart_*, and any unknown -> safe CDO
    # Any other concrete parent (FGSchematic, a mod intermediate, a non-FG class) ->
    # CDO is the safe catch-all (patches any class with no type gate).
    return "cdos"


def _export_index_from_name(export_name: str) -> int:
    """ExportName is `ObjectName#<exportTableIndex>` (the index disambiguates
    repeated ObjectNames). Pull the index back out for the byte-patch locator."""
    if export_name and "#" in export_name:
        try:
            return int(export_name.rsplit("#", 1)[1])
        except ValueError:
            return -1
    return -1


def _extract_cdo_meta(item: dict) -> dict | None:
    """Pull array-element metadata out of an extractor item — used BOTH for the
    (disabled) ContentLib CDO array-replace AND the byte-patch path. Returns None
    for items that aren't inside a struct/array container.

    Trigger = the item has a ContainerPath (it lives inside a struct or array).
    The byte-patch locator (export_index, container_path, prop) mirrors exactly
    how `_build_uasset_path_key` built `path[]`, so inject can address the value.
    """
    cont = item.get("ContainerPath") or ""
    if not cont:
        return None
    return {
        # byte-patch locator (always present for a nested item)
        "export_index": _export_index_from_name(item.get("ExportName") or ""),
        "container_path": cont,
        "prop": item.get("PropName") or "",
        # legacy CDO array-replace fields (only when the extractor tagged the array)
        "class": item.get("CdoClass") or "",
        "array_prop": item.get("CdoArrayProp") or "",
        "token": item.get("CdoPlaceholderToken") or "",
        "array_json": item.get("CdoArrayJson") or "",
        "omitted": bool(item.get("CdoArrayOmittedFields")),
    }


def _process_utoc_worker(args: tuple[str, str, str, str, str]) -> list[dict]:
    """Module-level worker for ProcessPoolExecutor. Extracts uassets from one mod's .utoc.
    Returns list of dicts (picklable) instead of TranslationString objects.
    NEVER raises — returns empty list on any error to prevent pool crash."""
    try:
        from .base import make_id
    except ImportError:
        from parsers.base import make_id
    uf_str, root, global_utoc_str, global_ucas_str, retoc_bin = args
    try:
        uf = Path(uf_str)
        global_utoc = Path(global_utoc_str)
        global_ucas = Path(global_ucas_str)
        results: list[dict] = []
        mod_rel = _mod_path_from_utoc(uf, root)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_input = Path(tmp_dir) / "input"
            tmp_output = Path(tmp_dir) / "output"
            tmp_input.mkdir()
            tmp_output.mkdir()

            for name, src in [("global.utoc", global_utoc), ("global.ucas", global_ucas)]:
                if src.is_file():
                    try:
                        os.symlink(str(src), str(tmp_input / name))
                    except OSError:
                        shutil.copy2(str(src), str(tmp_input / name))

            shutil.copy2(str(uf), str(tmp_input / uf.name))
            ucas = uf.with_suffix(".ucas")
            if ucas.is_file():
                shutil.copy2(str(ucas), str(tmp_input / ucas.name))

            ue_ver = _detect_ue_version(str(uf), retoc_bin)
            try:
                # --no-shaders / --no-script-objects: skip GPU shader libraries and
                # script objects (no translatable text in either). ~50% faster
                # to-legacy with ZERO change to the set of .uasset files produced.
                _run_cmd([
                    retoc_bin, "to-legacy", str(tmp_input), str(tmp_output),
                    "--version", ue_ver, "--no-shaders", "--no-script-objects"
                ], check=True, capture_output=True, timeout=60)
            except Exception:
                return results

            # ONE extractor process over the whole assembled mod directory, instead
            # of spawning it per .uasset (hundreds of CLR starts). The C# extractor
            # only emits items for assets that actually carry text, so no separate
            # content-gate is needed here. Each item's AssetPath maps back to the
            # file's path inside tmp_output (the same key the per-file path built).
            extracted = _run_uasset_extractor_dir(str(tmp_output))
            for item in extracted:
                asset_path = item.get("AssetPath")
                if not asset_path:
                    continue
                safe_path = _sanitize_extracted_path(tmp_output, Path(asset_path))
                if safe_path is None:
                    continue
                file_key = f"uasset://{mod_rel}{PAK_SEP}{safe_path}"
                path_key = _build_uasset_path_key(item)
                original_val = item["Value"]
                sid = make_id("unreal4_5", file_key, path_key, original_val)
                rec = {
                    "id": sid,
                    "original": original_val,
                    "context": f"Class: {item['AssetClass']} | Property: {item['PropName']}",
                    "file": file_key,
                    "path": path_key,
                    "engine": "unreal4_5",
                }
                # Part B: carry CDO full-array-replace metadata for inject (stripped
                # before TranslationString construction; stored in _cdo_meta by id).
                cdo = _extract_cdo_meta(item)
                if cdo:
                    rec["_cdo"] = cdo
                # ContentLib routing facts (stripped in the parent, stored in _route_meta).
                rec["_route"] = {
                    "super": item.get("SuperClass") or "",
                    "has_ing_prod": bool(item.get("HasIngredientsAndProduct")),
                }
                results.append(rec)
        return results
    except Exception:
        return []

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
    if pos + 4 > len(buf):
        raise LocresParseError(f"Buffer underflow reading FString length at offset {pos}")
    (length,) = struct.unpack_from("<i", buf, pos)
    p = pos + 4
    if length > 0:
        if p + length > len(buf):
            raise LocresParseError(f"Buffer underflow reading FString data at offset {p}, need {length} bytes")
        data = buf[p:p + length]
        p += length
        text = data.decode("utf-8", "replace")
        if text.endswith("\x00"):
            text = text[:-1]
    elif length < 0:
        n = -length
        if p + n * 2 > len(buf):
            raise LocresParseError(f"Buffer underflow reading FString UTF-16 data at offset {p}, need {n * 2} bytes")
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
    if not s:
        return struct.pack("<i", 0)
    if not s.endswith("\x00"):
        s = s + "\x00"
    if all(ord(c) < 128 for c in s):
        data = s.encode("ascii")
        return struct.pack("<i", len(data)) + data
    data = s.encode("utf-16-le")
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

            # Sanity check: string table offset must be within buffer
            if st_offset < 0 or st_offset + 4 > len(buf):
                raise LocresParseError(f"Invalid string table offset {st_offset} (buffer size {len(buf)})")

            # Read the string table out-of-line, then return to the index.
            sp = st_offset
            (st_count,) = struct.unpack_from("<i", buf, sp)
            sp += 4
            if st_count < 0 or st_count > len(buf):
                raise LocresParseError(f"Invalid string table count {st_count}")
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
            if m.entry_count_raw is not None:
                out += m.entry_count_raw     # preserve original entry count bytes
            else:
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
        # `retoc info` takes the path POSITIONALLY (`info <PATH>`), NOT `--path`
        # (unlike `list`, which accepts `--path`). Passing `--path` errors out and
        # the UE version always fell back to UE5_4 — fine for UE5_4 mods but wrong
        # for UE5_5/5_6.
        res = _run_cmd([retoc_bin, "info", utoc_path], capture_output=True, text=True, check=True)
        info_text = res.stdout
        if "ReplaceIoChunkHashWithIoHash" in info_text:
            return "UE5_6"
        elif "PartitionedToc" in info_text:
            return "UE5_5"
        return "UE5_4"
    except Exception as e:
        logger.warning(f"Failed to detect UE version for {utoc_path}: {e}. Defaulting to UE5_4.")
        return "UE5_4"


def _find_uasset_extractor() -> str:
    """Path to the bundled UAssetExtractor binary (raises if missing)."""
    import sys
    ext = ".exe" if sys.platform.startswith("win") else ""
    core_dir = Path(__file__).resolve().parent.parent
    extractor_bin = core_dir / "bin" / f"UAssetExtractor{ext}"
    if not extractor_bin.is_file():
        raise RuntimeError(f"UAssetExtractor not found at {extractor_bin}")
    return str(extractor_bin)


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
    """Check if path_str (e.g. 'FactoryGame/Mods/GameFeatures/ModA/...') starts with,
    contains as a segment, or equals any path in parent_set."""
    for p in parent_set:
        if path_str == p or path_str.startswith(p + "/"):
            return True
        if f"/{p}/" in f"/{path_str}/":
            return True
    return False


# Property names whose presence in a .uasset's name table means it carries
# user-visible translatable text. Stored as UTF-8 bytes for a cheap substring scan
# of the raw file (the FName strings are ASCII in the asset's name table).
_TEXT_PROP_MARKERS = (
    b"mDisplayName", b"mDescription", b"mTooltip", b"mFlavor",
    b"mLongDescription", b"mPreUnlockDisplayName", b"mPreUnlockDescription",
    b"mPostUnlockDescription", b"mAbbreviatedDisplayName", b"mMenuName",
)


def _uasset_has_text(data: bytes) -> bool:
    """True if the raw .uasset bytes reference any translatable text property.
    The C# extractor is what actually reads the values; this is a cheap pre-gate
    (bytes substring) so we only spawn it for assets that can possibly have text —
    far more reliable than a filename-prefix whitelist (which missed buildables
    named without a `Build_` prefix, e.g. BigStorageTank's `MegaPump`, and item
    buffers like `Glass_Buffer` / `B_Berry`)."""
    return any(m in data for m in _TEXT_PROP_MARKERS)


def _is_translatable_uasset(inner_path: str, data: bytes | None = None) -> bool:
    """Decide whether to run the extractor on this uasset.

    PREFERRED: content-based — if we have the raw bytes, keep it iff it references
    a translatable text property (`_uasset_has_text`). This is the accurate path.

    FALLBACK (data is None): the old filename/folder heuristic, kept only for
    callers that don't have the bytes handy. It UNDER-matches (misses text-bearing
    assets with non-standard names), so prefer passing `data`."""
    if data is not None:
        return _uasset_has_text(data)

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
    """Run UAssetExtractor.exe on a single file and return JSON list."""
    import sys
    import json

    # Memory bomb protection: reject oversized files
    try:
        if os.path.getsize(uasset_path) > MAX_UASSET_SIZE:
            logger.warning(f"Skipping oversized uasset ({os.path.getsize(uasset_path)} bytes): {uasset_path}")
            return []
    except OSError:
        return []

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


def _run_uasset_extractor_dir(dir_path: str) -> list[dict]:
    """Run UAssetExtractor.exe ONCE over a whole directory (`--input-dir`) and
    return the combined JSON list for every `.uasset` under it. Each item carries
    its own `AssetPath`, so the caller maps results back to files. This replaces
    spawning the extractor process once per asset (hundreds of CLR starts per mod)
    with a single process — the dominant cost in extraction/inject."""
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
            # No per-file timeout (a whole mod can have many assets); generous cap.
            _run_cmd([
                str(extractor_bin),
                "--input-dir", dir_path,
                "--output", str(out_json),
                "--engine", "VER_UE5_4"
            ], check=True, capture_output=True, timeout=300)

            if out_json.is_file():
                with open(out_json, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"UAssetExtractor (dir) failed on {dir_path}: {e}")

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
        # Fast path: check common mod locations first (no full rglob)
        r = Path(root)
        for p in r.glob("*.uplugin"):
            return True
        for p in r.glob("*.pak"):
            return True
        for p in (r / "Content" / "Paks").rglob("*.pak"):
            return True
        for p in (r / "Content" / "Paks").rglob("*.utoc"):
            return True
        for p in r.rglob("*.uasset"):
            return True

        # Loose .locres on disk
        for f in iter_locres_files(root):
            try:
                content = f.read_bytes()
            except Exception:
                continue
            if content[:16] == LOCRES_MAGIC:
                return True
            try:
                parse_locres(content)
                return True
            except Exception:
                continue
        # Packed: a .pak containing .locres
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

        # Part B: per-string CDO full-array-replace metadata, keyed by string id.
        # Populated alongside extraction so inject (which re-runs this) can emit
        # array-replace CDO patches without re-extracting structure.
        self._cdo_meta = {}
        # Per-string ContentLib routing facts (super class + recipe-prop signal),
        # keyed by string id. inject reads this to pick recipes/items/cdos.
        self._route_meta = {}

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
                
            # Content-gate in memory, dump the survivors to ONE temp dir, then run
            # the extractor ONCE over the whole dir (not once per file). A map from
            # the on-disk temp path back to the inner pak path recovers the file key.
            with tempfile.TemporaryDirectory() as tmp_dir:
                tmp_root = Path(tmp_dir)
                path_map: dict[str, str] = {}
                for inf in inner_files:
                    if not _is_translatable_uasset(inf.path, inf.data):
                        continue
                    temp_uasset = tmp_root / inf.path.lstrip("/")
                    temp_uasset.parent.mkdir(parents=True, exist_ok=True)
                    temp_uasset.write_bytes(inf.data)
                    path_map[str(temp_uasset.resolve())] = inf.path

                if not path_map:
                    continue

                extracted = _run_uasset_extractor_dir(str(tmp_root))
                for item in extracted:
                    ap = item.get("AssetPath")
                    inner = path_map.get(str(Path(ap).resolve())) if ap else None
                    if inner is None:
                        continue
                    ts = self._mk(
                        file=f"uasset://{mod_rel}{PAK_SEP}{inner}",
                        path=_build_uasset_path_key(item),
                        original=item["Value"],
                        context=f"Class: {item['AssetClass']} | Property: {item['PropName']}"
                    )
                    out.append(ts)
                    cdo = _extract_cdo_meta(item)
                    if cdo:
                        self._cdo_meta[ts.id] = cdo
                    self._route_meta[ts.id] = {
                        "super": item.get("SuperClass") or "",
                        "has_ing_prod": bool(item.get("HasIngredientsAndProduct")),
                    }

        # 2. Extract from .utoc/ucas files via retoc to-legacy (assembles proper .uasset files)
        #    Each mod is processed in a separate process for true parallelism.
        try:
            retoc_bin = _find_retoc()
        except RuntimeError:
            return

        root_path = Path(root)
        global_utoc = root_path / "FactoryGame" / "Content" / "Paks" / "global.utoc"
        global_ucas = root_path / "FactoryGame" / "Content" / "Paks" / "global.ucas"

        if not utocs:
            return

        from concurrent.futures import ProcessPoolExecutor, as_completed

        utoc_args = [
            (str(uf), root, str(global_utoc), str(global_ucas), retoc_bin)
            for uf in utocs
        ]

        max_workers = min(len(utocs), os.cpu_count() or 4)
        with ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_process_utoc_worker, args): args for args in utoc_args}
            for future in as_completed(futures):
                try:
                    dicts = future.result(timeout=120)
                    for d in dicts:
                        cdo = d.pop("_cdo", None)
                        route = d.pop("_route", None)
                        ts = TranslationString(**d)
                        out.append(ts)
                        if cdo:
                            self._cdo_meta[ts.id] = cdo
                        if route:
                            self._route_meta[ts.id] = route
                except Exception as e:
                    logger.error(f"Parallel utoc extraction failed: {e}")

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
            # Byte-patch path: rewrite struct-array FText the ContentLib CDO can't
            # reach (selector options, subsystem build descriptions) directly in the
            # .uasset bytes and repack the mod as a _P container. Runs AFTER the
            # ContentLib pass and reuses its extraction cache + _cdo_meta.
            if _ENABLE_ASSET_BYTEPATCH:
                written += self._inject_into_uassets_bytepatch(root, translations, sub_paths)
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
        
        # Group translations by (mod, asset, ContentLib kind). Kind is decided by the
        # asset's PARENT UE class (see _route_patch_kind), not its filename:
        #   - RecipePatches : parent FGRecipe (or erased + has mIngredients+mProduct)
        #   - ItemPatches   : parent FGItemDescriptor (Desc_*)
        #   - CDOs          : categories, schematics, buildables, input-actions, and
        #                     anything else -> CDO patches any class with no type gate
        # WHY CDO for Build_*: ContentLib's ItemPatch loader rejects any class that
        # is `Was not FGItemDescriptor` — Build_* buildables are NOT descriptors, so
        # 485/550 item patches failed. The CDO feature patches ANY class by raw UE
        # property name (mDisplayName/mDescription), with no type restriction, so it
        # reaches buildables and reuses the SAME translations (no re-translate).
        # mod_name -> kind("recipes"|"items"|"cdos") -> internal_path -> patch_dict
        patches_by_mod = {}

        # ContentLib ItemPatch/RecipePatch schema keys, by UE property name.
        def _schema_key(prop_lower: str) -> str | None:
            if "displayname" in prop_lower or prop_lower == "name":
                return "Name"
            if "tooltip" in prop_lower:
                return "Tooltip"
            if "flavor" in prop_lower or "longdescription" in prop_lower:
                return "LongDescription"
            if "preunlock" in prop_lower and "name" in prop_lower:
                return "PreUnlockDisplayName"
            if "preunlock" in prop_lower and "desc" in prop_lower:
                return "PreUnlockDescription"
            if "postunlock" in prop_lower and "desc" in prop_lower:
                return "PostUnlockDescription"
            if "description" in prop_lower:
                return "Description"
            return None

        cdo_meta = getattr(self, "_cdo_meta", {})
        # Part B: accumulate CDO full-array-replace groups, keyed by
        # (mod_name, CdoClass, array_prop). Each group rebuilds one whole array.
        #   group -> {"class","array_prop","array_json","omitted","mod_name",
        #             "subs": {token: translated_text}}
        array_cdo_groups = {}

        written_count = 0
        for s in uasset_strings:
            string_id = make_id(self.engine, s.file, s.path, s.original)
            if string_id not in translations:
                continue
            translated_text = translations[string_id]
            if not s.file.startswith("uasset://"):
                continue

            parts = s.file[9:].split(PAK_SEP)
            mod_rel = parts[0]
            mod_name = mod_rel.split("/")[-1]
            internal_path = s.path[0]
            prop_name = s.path[1]
            asset_leaf = internal_path.split("/")[-1].lower()

            # Part B (DISABLED — see _ENABLE_CDO_ARRAY_REPLACE): a string inside a
            # struct array can only be applied by replacing the WHOLE array via a
            # CDO. We BUILT this (selectors, subsystem descriptions) and the engine
            # log confirmed the patches apply (`Processed CDOs Successful: N/N`,
            # NewValue shows the translation) — BUT the game still renders English,
            # in a fresh save too. ContentLib's CDO edit does not reach the FText the
            # game actually displays for these DataAsset/subsystem arrays. So the
            # patches are inert clutter (1354 files on a real run). We keep the
            # extraction+serialization code (it's correct and a future engine-side
            # path may use it) but DON'T emit the patches. The string still shows as
            # extracted in the table; it just can't be applied via ContentLib.
            meta = cdo_meta.get(string_id)
            if meta:
                if not _ENABLE_CDO_ARRAY_REPLACE:
                    continue   # array-element string: not applicable via ContentLib
                gkey = (mod_name, meta["class"], meta["array_prop"])
                grp = array_cdo_groups.setdefault(gkey, {
                    "class": meta["class"], "array_prop": meta["array_prop"],
                    "array_json": "", "omitted": meta["omitted"],
                    "mod_name": mod_name, "internal_path": internal_path, "subs": {},
                })
                if meta["array_json"]:
                    grp["array_json"] = meta["array_json"]
                grp["subs"][meta["token"]] = translated_text
                written_count += 1
                continue

            # Recover the real plugin mount path (see notes below) and class path.
            #   1. MOUNT PATH: extractor reports `/Game/Mods/<Mod>/...` but mods are
            #      UE plugins mounted at `/<Mod>/...`; drop `/Game/Mods` or the
            #      package "does not exist".
            #   2. The class path used by BOTH the ItemPatch first-line target AND
            #      the CDO `Class` field is `<mountpath>.<AssetName>_C`.
            mount_path = internal_path
            if mount_path.startswith("/Game/Mods/"):
                mount_path = "/" + mount_path[len("/Game/Mods/"):]
            clean_path = mount_path.lstrip("/")
            asset_name = clean_path.split("/")[-1]
            class_path = f"/{clean_path}.{asset_name}_C"

            patches_by_mod.setdefault(mod_name, {"recipes": {}, "items": {}, "cdos": {}})

            # Route by the asset's PARENT UE class (ground truth ContentLib gates on),
            # not by filename — the old `"recipe" in internal_path` sent categories and
            # input-actions in a /Recipes/ folder to RecipePatches where they were
            # rejected ("Was not FGRecipe"). Falls back to the name heuristic if the
            # extractor predates SuperClass (graceful degradation, no mis-route on a
            # rebuilt exe).
            route = (getattr(self, "_route_meta", {}) or {}).get(string_id, {})
            kind = _route_patch_kind(
                route.get("super", ""),
                bool(route.get("has_ing_prod")),
                asset_leaf,
            )

            target_map = patches_by_mod[mod_name][kind]

            if kind == "cdos":
                # CDO file: { "Class": "<path>_C", "Edits": [ {Property, Value} ] }.
                # Patch by RAW UE property name; no first-line comment, no type gate.
                entry = target_map.setdefault(internal_path, {
                    "Class": class_path, "Edits": [], "_seen": set(),
                })
                if prop_name not in entry["_seen"]:
                    entry["_seen"].add(prop_name)
                    entry["Edits"].append({"Property": prop_name, "Value": translated_text})
                    written_count += 1
            else:
                # ItemPatch / RecipePatch file: first-line `//`+class-path target,
                # body uses ContentLib schema keys. `//`+clean_path (one leading
                # slash) avoids the "double slash in a class path" rejection.
                key = _schema_key(prop_name.lower())
                if key is None:
                    continue   # skip enum/struct fields (DoorMode/Frame/Sound/...)
                entry = target_map.setdefault(internal_path, {
                    "_target_comment": f"//{clean_path}.{asset_name}_C"
                })
                entry[key] = translated_text
                written_count += 1

        # Part B: turn each array-replace group into a CDO patch. The class is the
        # owning object's LoadObject path (a sub-object for selectors, the class
        # default for subsystem arrays); the Edit replaces the WHOLE array property
        # with the rebuilt JSON (placeholders swapped for translations). ContentLib
        # mount-path rule applies to the class too: strip /Game/Mods.
        for gkey, grp in array_cdo_groups.items():
            array_json = grp["array_json"]
            if not array_json:
                # no anchor item carried the full array (shouldn't happen) -> skip
                logger.warning(f"CDO array group {gkey} has no array JSON; skipped")
                continue
            # substitute placeholder tokens with the actual translations
            filled = array_json
            for token, tx in grp["subs"].items():
                # JSON-encode the translation (without the surrounding quotes) so
                # newlines/quotes in the text stay valid inside the JSON string.
                enc = json.dumps(tx, ensure_ascii=False)[1:-1]
                filled = filled.replace(token, enc)
            # any leftover placeholder (untranslated element) -> restore nothing,
            # leave its ORIGINAL text by stripping the marker is impossible here, so
            # we just drop the patch if tokens remain (safer than emitting markers).
            if "@@IPX:" in filled:
                logger.warning(f"CDO array {gkey}: untranslated elements remain; skipped")
                continue
            try:
                array_value = json.loads(filled)
            except Exception as e:
                logger.error(f"CDO array {gkey}: rebuilt JSON invalid: {e}")
                continue

            cls = grp["class"]
            if cls.startswith("/Game/Mods/"):
                cls = "/" + cls[len("/Game/Mods/"):]

            patches_by_mod.setdefault(grp["mod_name"], {"recipes": {}, "items": {}, "cdos": {}})
            cdo_map = patches_by_mod[grp["mod_name"]]["cdos"]
            # one CDO file per (class), key by class so multiple array props on the
            # same object merge into one file.
            entry = cdo_map.setdefault(f"__arr__{cls}", {
                "Class": cls, "Edits": [], "_seen": set(),
            })
            entry["Edits"].append({"Property": grp["array_prop"], "Value": array_value})

            # A full-array-replace where a field had to be dropped (an unresolvable
            # base-game object ref, e.g. Ingredients.ItemClass = UnknownExport) is
            # LOSSY: ContentLib rebuilds each struct from scratch, so the omitted
            # field resets to default. For a buildable subsystem that can blank a
            # recipe's ingredients. We still emit it (the translation is the goal)
            # but log loudly so a live test can confirm the build still works.
            if grp.get("omitted"):
                logger.warning(
                    f"CDO array-replace {cls}.{grp['array_prop']} omitted unresolvable "
                    f"field(s) (e.g. Ingredients) — rebuilt elements lose them; "
                    f"verify the build/recipe in-game."
                )

        # 2. Write patch files and register in backups as 'created'
        FOLDER = {"recipes": "RecipePatches", "items": "ItemPatches", "cdos": "CDOs"}
        for mod_name, kinds in patches_by_mod.items():
            for kind, target_map in kinds.items():
                if not target_map:
                    continue
                folder_name = FOLDER[kind]
                dirs = [
                    os.path.join(root, "Configs", "ContentLib", folder_name),
                    os.path.join(root, "FactoryGame", "Configs", "ContentLib", folder_name)
                ]
                for dest_dir in dirs:
                    os.makedirs(dest_dir, exist_ok=True)

                for internal_path, patch_data in target_map.items():
                    # `__arr__<class>` keys (Part B array-replace) name the file from
                    # the OWNING ASSET, not the sub-object leaf: dozens of selectors
                    # share the sub-object name `FGUserSetting_IntSelector_0`, so
                    # naming by leaf collapses 40 distinct classes into one file
                    # (they overwrite each other). The owning asset = the package
                    # path's last segment (before any `:` sub-object / `.` object).
                    if internal_path.startswith("__arr__"):
                        cls = internal_path[len("__arr__"):]
                        pkg = cls.split(":")[0].split(".")[0]   # /A/B/Asset
                        asset_pkg = pkg.rstrip("/").split("/")[-1]
                        # include the sub-object leaf too so two arrays on different
                        # sub-objects of one asset don't collide.
                        sub = cls.split(":")[-1] if ":" in cls else ""
                        asset_name = "arr_" + asset_pkg + (("_" + sub) if sub else "")
                    else:
                        asset_name = internal_path.split("/")[-1]
                    file_name = f"Patch_{mod_name}_{asset_name}.json"

                    try:
                        # Strip internal bookkeeping and build the comment line.
                        comment = patch_data.pop("_target_comment", "")
                        patch_data.pop("_seen", None)

                        # Build the file body and force CRLF line endings.
                        # ContentLib REQUIRES CRLF: with LF-only files it mis-reads
                        # the first-line patch target and drags the JSON's `{` /
                        # `"Name"` into the class name, so the engine logs
                        # `Failed to find object '<path>{ "Name"'` and the patch is
                        # rejected (docs: Troubleshooting "Always Use CRLF Line
                        # Endings" — telltale sign is curly braces in class names).
                        # CDO files have no comment line (target is the `Class`
                        # field); they still need CRLF for consistency/safety.
                        body = json.dumps(patch_data, indent=4, ensure_ascii=False)
                        if comment:
                            body = comment + "\n" + body
                        body = body.replace("\r\n", "\n").replace("\n", "\r\n")

                        # Write to both configs locations to ensure compatibility with all SML/game versions
                        for dest_dir in dirs:
                            file_path = os.path.join(dest_dir, file_name)
                            rel_to_root = os.path.relpath(file_path, root).replace("\\", "/")

                            with open(file_path, "w", encoding="utf-8", newline="") as f:
                                f.write(body)

                            # Register in backup system as 'created' (to support Restore Backup)
                            mod_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
                            update_metadata(root, rel_to_root, "", mod_sha, "created")
                    except Exception as e:
                        logger.error(f"Failed to write ContentLib patch {file_name}: {e}")

        return written_count

    def _inject_into_uassets_bytepatch(self, root: str, translations: dict[str, str],
                                       sub_paths) -> int:
        """Rewrite struct-array FText (selector options, subsystem descriptions)
        directly in the .uasset bytes and repack each affected mod as a `_P`
        IoStore container — the working path for strings ContentLib CDO can't reach.

        Per mod: re-run `retoc to-legacy` (assemble .uasset), run UAssetExtractor
        `--apply-edits` (write the new FText in place via UAssetAPI), copy only the
        edited assets into a fresh dir, then `retoc to-zen` into `<stem>_P`. Reuses
        the SAME (export_index, container_path, prop) locators the extractor emits
        (stored in self._cdo_meta), so each edit lands on the right value.
        """
        import json as _json
        from .base import update_metadata, make_id

        cdo_meta = getattr(self, "_cdo_meta", {})
        if not cdo_meta:
            return 0

        # Reuse the extraction cache populated by _inject_into_uassets (same run).
        cache_key = (root, tuple(sub_paths) if sub_paths else None)
        if not hasattr(self, "_uasset_cache") or self._uasset_cache_key != cache_key:
            self._uasset_cache = []
            self._extract_from_uassets(root, sub_paths, self._uasset_cache)
            self._uasset_cache_key = cache_key
            cdo_meta = getattr(self, "_cdo_meta", {})

        # Collect edits, grouped by mod_rel -> safe_path(asset) -> list[edit].
        # mod_rel + safe_path come straight from each string's file key.
        edits_by_mod: dict[str, list[dict]] = {}
        for s in self._uasset_cache:
            sid = make_id(self.engine, s.file, s.path, s.original)
            if sid not in translations or not s.file.startswith("uasset://"):
                continue
            meta = cdo_meta.get(sid)
            if not meta or meta.get("export_index", -1) < 0:
                continue  # only nested (array/struct) strings go through byte-patch
            body = s.file[9:]
            mod_rel, _, safe_path = body.partition(PAK_SEP)
            if not safe_path:
                continue
            edits_by_mod.setdefault(mod_rel, []).append({
                "AssetPath": safe_path,            # relative to the to-legacy out dir
                "ExportIndex": meta["export_index"],
                "ContainerPath": meta["container_path"],
                "PropName": meta["prop"],
                "NewValue": translations[sid],
            })

        if not edits_by_mod:
            return 0

        try:
            retoc_bin = _find_retoc()
        except RuntimeError as e:
            logger.warning(f"Byte-patch skipped (no retoc): {e}")
            return 0
        try:
            extractor_bin = _find_uasset_extractor()
        except Exception as e:
            logger.warning(f"Byte-patch skipped (no extractor): {e}")
            return 0

        root_path = Path(root)
        global_utoc = root_path / "FactoryGame" / "Content" / "Paks" / "global.utoc"
        global_ucas = root_path / "FactoryGame" / "Content" / "Paks" / "global.ucas"

        # Map each mod_rel back to its source .utoc on disk.
        utocs = list(iter_utoc_files(root))
        utoc_by_mod = {_mod_path_from_utoc(uf, root): uf for uf in utocs}

        written = 0
        for mod_rel, edits in edits_by_mod.items():
            uf = utoc_by_mod.get(mod_rel)
            if uf is None:
                logger.warning(f"Byte-patch: no source utoc for mod {mod_rel}; skipped")
                continue
            try:
                written += self._bytepatch_one_mod(
                    root, uf, edits, retoc_bin, extractor_bin,
                    global_utoc, global_ucas, update_metadata, _json)
            except Exception as e:
                logger.error(f"Byte-patch failed for mod {mod_rel}: {e}")
        return written

    def _bytepatch_one_mod(self, root, uf, edits, retoc_bin, extractor_bin,
                           global_utoc, global_ucas, update_metadata, _json) -> int:
        """to-legacy → apply-edits → copy edited assets → to-zen `_P`. Returns the
        number of edits applied. Mirrors the locres _inject_into_utocs repack."""
        ue_ver = _detect_ue_version(str(uf), retoc_bin)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_input = Path(tmp_dir) / "input"
            tmp_legacy = Path(tmp_dir) / "legacy"
            tmp_zen = Path(tmp_dir) / "zen"
            tmp_input.mkdir(); tmp_legacy.mkdir(); tmp_zen.mkdir()

            for name, src in [("global.utoc", global_utoc), ("global.ucas", global_ucas)]:
                if src.is_file():
                    try:
                        os.symlink(str(src), str(tmp_input / name))
                    except OSError:
                        shutil.copy2(str(src), str(tmp_input / name))
            shutil.copy2(str(uf), str(tmp_input / uf.name))
            ucas = uf.with_suffix(".ucas")
            if ucas.is_file():
                shutil.copy2(str(ucas), str(tmp_input / ucas.name))

            # 1. assemble legacy .uasset
            _run_cmd([
                retoc_bin, "to-legacy", str(tmp_input), str(tmp_legacy),
                "--version", ue_ver, "--no-shaders", "--no-script-objects"
            ], check=True, capture_output=True, timeout=120)

            # 1b. Filter out assets whose imports contain UnknownExport stubs
            # (retoc replaces base-game refs with UnknownExport). UAssetAPI can
            # rewrite these but the game can't resolve the broken outer-link chain.
            _bad_assets: set[str] = set()
            for e in edits:
                asset_rel = e.get("AssetPath", "")
                if not asset_rel:
                    continue
                asset_path = tmp_legacy / asset_rel
                if not asset_path.is_file():
                    continue
                try:
                    raw = asset_path.read_bytes()
                    if b"UnknownExport" in raw:
                        _bad_assets.add(asset_rel)
                except Exception:
                    pass
            if _bad_assets:
                logger.info(
                    f"Byte-patch: skipping {len(_bad_assets)} asset(s) with "
                    f"UnknownExport imports in {uf.name}")
                edits = [e for e in edits if e.get("AssetPath", "") not in _bad_assets]
                if not edits:
                    return 0

            # 2. write edits in place (AssetPath is relative to tmp_legacy)
            edits_file = Path(tmp_dir) / "edits.json"
            edits_file.write_text(_json.dumps(edits, ensure_ascii=False), encoding="utf-8")
            res = _run_cmd([
                extractor_bin, "--apply-edits", str(edits_file),
                "--base-dir", str(tmp_legacy), "--engine", f"VER_{ue_ver}"
            ], check=True, capture_output=True, text=True, timeout=120)
            applied = 0
            written_abs: list[str] = []
            try:
                result_obj = _json.loads(res.stdout.strip().splitlines()[-1])
                applied = int(result_obj.get("applied", 0))
                # The C# side gates each asset on round-trip fidelity (export type) and
                # returns ONLY the assets it actually wrote — Blueprints whose Kismet
                # bytecode UAssetAPI can't faithfully re-serialize are SKIPPED (writing
                # them would NULL the class → SML crash). We pack ONLY `written` into the
                # _P container, never the requested-but-skipped set.
                written_abs = [str(p).replace("\\", "/") for p in result_obj.get("written", [])]
            except Exception:
                pass
            if applied == 0 or not written_abs:
                logger.warning(f"Byte-patch: 0 round-trip-safe edits applied for {uf.name}")
                # No safe assets to ship. If a PRIOR (buggy) run left a `_P` here that
                # bundled a corrupt Blueprint, it would keep crashing the game — remove
                # any stale `_P` we own so the mod loads vanilla instead.
                for ext in (".utoc", ".ucas", ".pak"):
                    stale = uf.parent / f"{uf.stem}_P{ext}"
                    if stale.exists():
                        try:
                            stale.unlink()  # our own created artifact, not a game original
                            logger.info(f"Removed stale byte-patch container {stale.name}")
                        except Exception as e:
                            logger.warning(f"Could not remove stale {stale.name}: {e}")
                return 0

            # 3. copy ONLY the assets the extractor actually wrote (safe, round-trip
            #    verified) into the zen input, preserving their relative paths so the
            #    package path is unchanged. Skipped (unsafe) assets never enter _P.
            tmp_legacy_abs = str(tmp_legacy).replace("\\", "/").rstrip("/")
            written_rel = set()
            for ab in written_abs:
                ab_norm = ab.replace("\\", "/")
                if ab_norm.startswith(tmp_legacy_abs + "/"):
                    written_rel.add(ab_norm[len(tmp_legacy_abs) + 1:])
                else:
                    # fall back to basename match if path normalization drifts
                    written_rel.add(Path(ab_norm).name)
            copied = 0
            for rel in written_rel:
                base = rel[:-len(".uasset")] if rel.endswith(".uasset") else rel
                for ext in (".uasset", ".uexp", ".ubulk"):
                    src = tmp_legacy / (base + ext)
                    if src.is_file():
                        dst = tmp_zen / src.relative_to(tmp_legacy)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(str(src), str(dst))
                        if ext == ".uasset":
                            copied += 1
            if copied == 0:
                logger.warning(f"Byte-patch: no written assets copied for {uf.name}")
                return 0

            # 4. repack as a `_P` patch container next to the original mod utoc.
            # LIMITATION: the locres inject (`_inject_into_utocs`) writes the SAME
            # `<stem>_P` name for a mod that also ships .locres. A mod with BOTH
            # .locres AND byte-patchable struct-array FText would have its locres `_P`
            # overwritten here (byte-patch runs after locres in inject()). Verified
            # NONE of the real Satisfactory mods overlap (byte-patch mods carry no
            # .locres). If that ever changes, merge both asset sets into one tmp_zen
            # before to-zen instead of overwriting.
            patch_base = uf.parent / f"{uf.stem}_P"
            patch_utoc = uf.parent / f"{uf.stem}_P.utoc"
            patch_ucas = uf.parent / f"{uf.stem}_P.ucas"
            patch_pak = uf.parent / f"{uf.stem}_P.pak"
            for p_file in (patch_utoc, patch_ucas, patch_pak):
                if p_file.exists():
                    self.backup_file(root, str(p_file))

            # to-zen OUTPUT must be the `.utoc` path (it derives .ucas/.pak from it).
            _run_cmd([
                retoc_bin, "to-zen", "--version", ue_ver,
                str(tmp_zen), str(patch_utoc)
            ], check=True, capture_output=True, timeout=120)

            missing = [p.name for p in (patch_utoc, patch_ucas)
                       if not p.exists() or p.stat().st_size == 0]
            if missing:
                raise RuntimeError(f"to-zen produced no output: {', '.join(missing)}")

            for p_file in (patch_utoc, patch_ucas, patch_pak):
                if p_file.exists():
                    rel_to_root = os.path.relpath(str(p_file), root).replace("\\", "/")
                    sha = hashlib.sha256(p_file.read_bytes()).hexdigest()
                    update_metadata(root, rel_to_root, "", sha, "created")

            logger.info(f"Byte-patched {applied} string(s) into {patch_base.name}")
            return applied

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
