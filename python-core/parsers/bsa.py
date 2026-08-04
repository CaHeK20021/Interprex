"""Skyrim / Fallout BSA archive reader (read-only).

Supports:
  * v104 (Skyrim LE / FO3 / FNV) — zlib compression
  * v105 (Skyrim SE / AE) — LZ4 compression

We never rewrite a BSA. For translations the game prefers loose files under
`Data/` over archive contents, so inject writes loose `Interface/Translations/`
and leaves the `.bsa` untouched (same stance as `rpa.py` / `pak.py`).

Format reference: UESP `Skyrim_Mod:Archive_File_Format`.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("interprex.bsa")

BSA_MAGIC = b"BSA\x00"
FLAG_INCLUDE_DIR_NAMES = 0x1
FLAG_INCLUDE_FILE_NAMES = 0x2
FLAG_COMPRESSED_DEFAULT = 0x4
FLAG_EMBED_FILE_NAMES = 0x100
SIZE_COMPRESS_TOGGLE = 0x40000000


@dataclass(frozen=True)
class BsaEntry:
    """One file inside a BSA."""
    path: str          # lower-case, forward slashes: "interface/translations/x.txt"
    offset: int
    size_field: int    # raw size field (may have toggle bit)
    compressed: bool


@dataclass
class BsaArchive:
    path: Path
    version: int
    archive_flags: int
    entries: list[BsaEntry]
    _data: bytes | None = None

    def _bytes(self) -> bytes:
        if self._data is None:
            self._data = self.path.read_bytes()
        return self._data

    def read(self, entry: BsaEntry) -> bytes:
        data = self._bytes()
        size = entry.size_field & ~SIZE_COMPRESS_TOGGLE
        blob = data[entry.offset:entry.offset + size]
        if self.archive_flags & FLAG_EMBED_FILE_NAMES:
            # leading bstring: uint8 length + path bytes (no null)
            if not blob:
                return b""
            n = blob[0]
            blob = blob[1 + n:]
        if not entry.compressed:
            return blob
        if len(blob) < 4:
            return b""
        (orig_size,) = struct.unpack_from("<I", blob, 0)
        payload = blob[4:]
        try:
            if self.version >= 105:
                return _lz4_decompress(payload, orig_size)
            return zlib.decompress(payload)
        except Exception as e:
            logger.error("BSA decompress %s in %s: %s", entry.path, self.path.name, e)
            return b""


def _lz4_decompress(payload: bytes, orig_size: int) -> bytes:
    """SSE BSA (v105) stores LZ4 *frame* after the uint32 orig size (magic
    ``04 22 4D 18``). Some community tools emit raw LZ4 *block* instead — try
    frame first, then block with the declared uncompressed size."""
    import lz4.frame
    import lz4.block
    # Frame (RaceMenu / official SSE packs)
    if len(payload) >= 4 and payload[:4] == b"\x04\x22\x4d\x18":
        return lz4.frame.decompress(payload)
    try:
        return lz4.frame.decompress(payload)
    except Exception:
        pass
    return lz4.block.decompress(payload, uncompressed_size=orig_size)

    def find(self, suffix: str | None = None,
             name_contains: str | None = None) -> list[BsaEntry]:
        out = []
        for e in self.entries:
            p = e.path
            if suffix and not p.endswith(suffix):
                continue
            if name_contains and name_contains not in p:
                continue
            out.append(e)
        return out


def open_bsa(path: str | Path) -> BsaArchive | None:
    """Parse a BSA index. Returns None if not a recognised archive."""
    path = Path(path)
    try:
        with open(path, "rb") as f:
            head = f.read(36)
            if len(head) < 36 or head[0:4] != BSA_MAGIC:
                return None
            version, folder_offset, archive_flags, folder_count, file_count, \
                total_folder_name_length, total_file_name_length, file_flags = \
                struct.unpack_from("<IIIIIIII", head, 4)
            # file_flags is actually ushort+ushort padding packed as last I above?
            # Header: after totalFileNameLength (offset 28) is fileFlags ushort + padding ushort
            # We used I for both → fine, we don't need file_flags value for listing.

            if version not in (103, 104, 105):
                logger.warning("BSA %s: unsupported version %s", path.name, version)
                # still try

            # Folder records start at folder_offset (normally 36)
            f.seek(folder_offset)
            folders: list[tuple[int, int]] = []  # (count, offset)
            for _ in range(folder_count):
                if version >= 105:
                    # hash(8) count(4) pad(4) offset(4) pad(4)
                    rec = f.read(24)
                    if len(rec) < 24:
                        return None
                    count = struct.unpack_from("<I", rec, 8)[0]
                    offset = struct.unpack_from("<I", rec, 16)[0]
                else:
                    # hash(8) count(4) offset(4)
                    rec = f.read(16)
                    if len(rec) < 16:
                        return None
                    count = struct.unpack_from("<I", rec, 8)[0]
                    offset = struct.unpack_from("<I", rec, 12)[0]
                # offset points past the file-name block; real offset -= totalFileNameLength
                real_off = offset - total_file_name_length
                folders.append((count, real_off))

            # File record blocks: for each folder, optional bzstring name + file records
            # These are stored sequentially after folder records, NOT necessarily at
            # the offsets in folder records (offsets include name-block skip). We walk
            # sequentially from current position — that is the standard approach.
            file_index_start = f.tell()
            # Re-seek per folder using computed offsets for robustness
            folder_files: list[tuple[str, list[tuple[int, int]]]] = []
            # list of (folder_name, [(size, data_offset), ...])

            # Actually the file record blocks are contiguous after folder records.
            # Folder record "offset" is absolute (minus totalFileNameLength) to that
            # folder's name+files. Sort by offset to walk in order.
            ordered = sorted(enumerate(folders), key=lambda x: x[1][1])
            file_metas: list[tuple[str, int, int]] = []  # (folder, size, data_off)

            for _idx, (count, off) in ordered:
                f.seek(off)
                folder_name = ""
                if archive_flags & FLAG_INCLUDE_DIR_NAMES:
                    # bzstring: length byte includes the trailing null
                    ln = f.read(1)
                    if not ln:
                        break
                    n = ln[0]
                    raw = f.read(n)
                    folder_name = raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
                for _ in range(count):
                    frec = f.read(16)
                    if len(frec) < 16:
                        break
                    size = struct.unpack_from("<I", frec, 8)[0]
                    data_off = struct.unpack_from("<I", frec, 12)[0]
                    file_metas.append((folder_name, size, data_off))

            # File name block (if flag set) — sequential null-terminated names
            # After all file record blocks. Position = end of last file-record walk.
            # UESP: names ordered same as file records generation order = folder
            # order as stored in folder records array (NOT sorted by offset).
            # Rebuild metas in original folder-record order for name pairing.
            file_metas_named: list[tuple[str, int, int]] = []
            for count, off in folders:
                f.seek(off)
                folder_name = ""
                if archive_flags & FLAG_INCLUDE_DIR_NAMES:
                    ln = f.read(1)
                    if not ln:
                        break
                    n = ln[0]
                    raw = f.read(n)
                    folder_name = raw.split(b"\x00", 1)[0].decode("latin-1", errors="replace")
                for _ in range(count):
                    frec = f.read(16)
                    if len(frec) < 16:
                        break
                    size = struct.unpack_from("<I", frec, 8)[0]
                    data_off = struct.unpack_from("<I", frec, 12)[0]
                    file_metas_named.append((folder_name, size, data_off))

            names: list[str] = []
            if archive_flags & FLAG_INCLUDE_FILE_NAMES:
                # File name block sits right after all file record blocks.
                # Its length is totalFileNameLength. Find start: after last
                # folder's file records. Easier: read totalFileNameLength bytes
                # starting where sequential walk of folders in file order ends.
                # After re-walking in folder-record order we're at end of last
                # folder's records — but only if folders were sequential.
                # UESP says file name block is after all fileRecordBlocks.
                # Compute: start of first file-record block + sum of block sizes.
                if folders:
                    first_off = min(o for _, o in folders)
                    f.seek(first_off)
                    # Consume all folder blocks in offset order to land on name block
                    for count, off in sorted(folders, key=lambda x: x[1]):
                        f.seek(off)
                        if archive_flags & FLAG_INCLUDE_DIR_NAMES:
                            ln = f.read(1)
                            if ln:
                                f.read(ln[0])
                        f.read(count * 16)
                    name_blob = f.read(total_file_name_length)
                    names = [
                        n.decode("latin-1", errors="replace")
                        for n in name_blob.split(b"\x00") if n
                    ]

            default_compressed = bool(archive_flags & FLAG_COMPRESSED_DEFAULT)
            entries: list[BsaEntry] = []
            for i, (folder, size_field, data_off) in enumerate(file_metas_named):
                fname = names[i] if i < len(names) else f"file_{i}"
                folder_norm = folder.replace("\\", "/").strip("/")
                full = f"{folder_norm}/{fname}" if folder_norm else fname
                full = full.replace("\\", "/").lower()
                toggled = bool(size_field & SIZE_COMPRESS_TOGGLE)
                compressed = (not default_compressed) if toggled else default_compressed
                entries.append(BsaEntry(
                    path=full,
                    offset=data_off,
                    size_field=size_field,
                    compressed=compressed,
                ))

            return BsaArchive(
                path=path,
                version=version,
                archive_flags=archive_flags,
                entries=entries,
            )
    except Exception as e:
        logger.error("BSA open failed %s: %s", path, e)
        return None


def list_bsa_files(path: str | Path) -> list[str]:
    arch = open_bsa(path)
    if not arch:
        return []
    return [e.path for e in arch.entries]
