"""UE4/5 `.pak` reader + minimal writer (version 11, unencrypted index).

Reader: walk the pak index, find inner files, decode the bit-packed FPakEntry,
read the on-disk FPakEntry, and Oodle-decompress data blocks. Verified against
Satisfactory's `FactoryGame-Windows.pak` (pak v11, 224 `.locres`).

Writer: emit a NEW *uncompressed* pak (compression method 0) holding a handful of
inner files. We never repack/compress the original — translations ship as a
separate mod-pak (`_P` suffix), so an Oodle encoder is unnecessary. Only the
subset of pak v11 the UE loader needs is produced (FullDirectoryIndex + footer).

References: Epic FPakFile/FPakInfo, akintos/repak, CUE4Parse. Offsets cross-checked
on real files — see the `satisfactory-pak-oodle` memory.
"""

from __future__ import annotations

import hashlib
import logging
import struct
from pathlib import Path

try:
    import oodle  # sidecar runs with python-core/ on sys.path
except ImportError:  # pragma: no cover - fallback if imported as a submodule
    from .. import oodle  # type: ignore

logger = logging.getLogger(__name__)

PAK_MAGIC = 0x5A6F12E1
PAK_V11 = 11
FOOTER_SIZE_V11 = 204            # EncryptionKeyGuid(16)+Encrypted(1)+Magic(4)+Ver(4)
#                                 +IndexOffset(8)+IndexSize(8)+IndexHash(20)
#                                 +CompressionMethods(5*32=160)+Frozen? = 204 from EOF
MOUNT_POINT = "../../../"
COMP_NAME_SLOTS = 5
COMP_NAME_LEN = 32
MAX_BLOCK = 65536


class PakFile:
    __slots__ = ("path", "data")

    def __init__(self, path: str, data: bytes):
        self.path = path        # inner path, e.g. "FactoryGame/Content/.../Game.locres"
        self.data = data        # decompressed bytes


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _read_fstring(b: bytes, pos: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<i", b, pos)
    pos += 4
    if n == 0:
        return "", pos
    if n > 0:
        s = b[pos:pos + n]; pos += n
        return s.rstrip(b"\x00").decode("utf-8", "replace"), pos
    s = b[pos:pos + (-n) * 2]; pos += (-n) * 2
    return s.decode("utf-16-le", "replace").rstrip("\x00"), pos


def _write_fstring(s: str) -> bytes:
    """ASCII when possible, else UTF-16LE; trailing null included in count."""
    t = s + "\x00"
    if all(ord(c) < 128 for c in t):
        data = t.encode("ascii")
        return struct.pack("<i", len(data)) + data
    data = t.encode("utf-16-le")
    return struct.pack("<i", -(len(data) // 2)) + data


def _decode_entry(blob: bytes, off: int) -> dict:
    """Decode a bit-packed FPakEntry from the EncodedPakEntries blob."""
    p = off
    (value,) = struct.unpack_from("<I", blob, p); p += 4
    comp = (value >> 23) & 0x3f
    is_off32 = (value & (1 << 31)) != 0
    is_unc32 = (value & (1 << 30)) != 0
    is_sz32 = (value & (1 << 29)) != 0
    bs6 = value & 0x3f
    offset = struct.unpack_from("<I" if is_off32 else "<Q", blob, p)[0]
    p += 4 if is_off32 else 8
    unc = struct.unpack_from("<I" if is_unc32 else "<Q", blob, p)[0]
    p += 4 if is_unc32 else 8
    if comp != 0:
        size = struct.unpack_from("<I" if is_sz32 else "<Q", blob, p)[0]
        p += 4 if is_sz32 else 8
    else:
        size = unc
    if bs6 == 0x3f:
        (_bsz,) = struct.unpack_from("<I", blob, p); p += 4
    return dict(comp=comp, offset=offset, unc=unc, size=size)


# ---------------------------------------------------------------------------
# reader
# ---------------------------------------------------------------------------

def read_pak(pak_path: str, want_suffix: str = ".locres") -> list[PakFile]:
    """Read inner files whose path ends with `want_suffix`, decompressing data."""
    pak = Path(pak_path)
    size = pak.stat().st_size
    with open(pak, "rb") as f:
        f.seek(size - FOOTER_SIZE_V11)
        footer = f.read(FOOTER_SIZE_V11)
        # locate magic within the footer (guid16 + enc1 then magic)
        magic_off = None
        for cand in (16 + 1,):  # standard v8+ position
            if struct.unpack_from("<I", footer, cand)[0] == PAK_MAGIC:
                magic_off = cand
                break
        if magic_off is None:
            for cand in range(len(footer) - 4):
                if struct.unpack_from("<I", footer, cand)[0] == PAK_MAGIC:
                    magic_off = cand
                    break
        if magic_off is None:
            raise RuntimeError("pak magic not found (unsupported version/encrypted)")
        version = struct.unpack_from("<i", footer, magic_off + 4)[0]
        idx_off, idx_size = struct.unpack_from("<qq", footer, magic_off + 8)
        bencrypted = footer[magic_off - 1]
        if bencrypted:
            raise RuntimeError("encrypted pak index not supported")
        # compression method names
        names_start = magic_off + 4 + 4 + 8 + 8 + 20
        methods = [""]  # index 0 = none
        for i in range(COMP_NAME_SLOTS):
            seg = footer[names_start + i * COMP_NAME_LEN: names_start + (i + 1) * COMP_NAME_LEN]
            if len(seg) < COMP_NAME_LEN:
                break
            nm = seg.split(b"\x00")[0].decode("ascii", "replace")
            if nm:
                methods.append(nm)

        f.seek(idx_off)
        idx = f.read(idx_size)

    # primary index
    pos = 0
    _mount, pos = _read_fstring(idx, pos)
    (num_entries,) = struct.unpack_from("<i", idx, pos); pos += 4
    (_seed,) = struct.unpack_from("<Q", idx, pos); pos += 8
    (has_phi,) = struct.unpack_from("<i", idx, pos); pos += 4
    if has_phi:
        pos += 8 + 8 + 20  # offset, size, hash
    (has_dir,) = struct.unpack_from("<i", idx, pos); pos += 4
    if not has_dir:
        raise RuntimeError("pak lacks FullDirectoryIndex; cannot resolve names")
    dir_off, dir_size = struct.unpack_from("<qq", idx, pos); pos += 16
    pos += 20  # dir hash
    (encoded_size,) = struct.unpack_from("<i", idx, pos); pos += 4
    encoded = idx[pos:pos + encoded_size]

    with open(pak, "rb") as f:
        f.seek(dir_off)
        dirblob = f.read(dir_size)

    # FullDirectoryIndex: TMap<dir FString, TMap<file FString, i32 encodedOffset>>
    dp = 0
    (num_dirs,) = struct.unpack_from("<i", dirblob, dp); dp += 4
    wanted: list[tuple[str, int]] = []
    for _ in range(num_dirs):
        d, dp = _read_fstring(dirblob, dp)
        (nf,) = struct.unpack_from("<i", dirblob, dp); dp += 4
        for _ in range(nf):
            fn, dp = _read_fstring(dirblob, dp)
            (eo,) = struct.unpack_from("<i", dirblob, dp); dp += 4
            full = (d + fn)
            if full.lower().endswith(want_suffix):
                wanted.append((full, eo))

    out: list[PakFile] = []
    with open(pak, "rb") as f:
        for full, eo in wanted:
            e = _decode_entry(encoded, eo)
            data = _read_data(f, e, methods)
            inner = full[len(MOUNT_POINT):] if full.startswith(MOUNT_POINT) else full.lstrip("./")
            out.append(PakFile(inner, data))
    return out


def _read_data(f, e: dict, methods: list[str]) -> bytes:
    """Read+decompress one entry's payload, given its data offset from _decode_entry."""
    f.seek(e["offset"])
    off2, size2, unc2, cm2 = struct.unpack("<qqqi", f.read(28))
    f.read(20)  # hash[20]
    blocks = []
    if cm2 != 0:
        (nblk,) = struct.unpack("<i", f.read(4))
        for _ in range(nblk):
            blocks.append(struct.unpack("<qq", f.read(16)))
    f.read(1)  # flags
    (_blksz,) = struct.unpack("<i", f.read(4))
    # File pointer now sits at the start of the payload.

    if cm2 == 0:
        return f.read(unc2)

    method = methods[cm2] if cm2 < len(methods) else ""
    out = bytearray()
    for (bs, be) in blocks:
        f.seek(e["offset"] + bs)
        comp = f.read(be - bs)
        want = min(MAX_BLOCK, unc2 - len(out))
        if method.lower() == "oodle":
            out += oodle.decompress(comp, want)
        elif method.lower() in ("zlib", "gzip"):
            import zlib
            out += zlib.decompress(comp)
        else:
            raise RuntimeError(f"unsupported pak compression method {method!r}")
    return bytes(out)


# ---------------------------------------------------------------------------
# writer (uncompressed pak v11)
# ---------------------------------------------------------------------------

def _pak_entry_record(offset: int, data: bytes) -> bytes:
    """On-disk FPakEntry for an uncompressed record: offset,size,unc,method=0,
    hash[20], flags(0), blockSize(0). Followed by the raw data."""
    h = hashlib.sha1(data).digest()
    rec = struct.pack("<qqqi", offset, len(data), len(data), 0)
    rec += h
    rec += struct.pack("<B", 0)   # flags
    rec += struct.pack("<i", 0)   # compression block size
    return rec


def _encoded_entry(offset: int, size: int) -> bytes:
    """Bit-packed FPakEntry for the EncodedPakEntries blob (uncompressed).
    Uses 64-bit offset/size fields (flags 0) and compression method 0."""
    value = 0  # comp=0, all is32 flags off, blockCount 0, blockSize bits 0
    return struct.pack("<I", value) + struct.pack("<q", offset) + struct.pack("<q", size)


def write_pak(out_path: str, files: dict[str, bytes]) -> None:
    """Write a minimal uncompressed pak v11 containing `files` (inner_path -> bytes)."""
    buf = bytearray()
    records: list[tuple[str, int, int]] = []  # (inner_path, entry_offset, data_size)

    for inner, data in files.items():
        entry_off = len(buf)
        buf += _pak_entry_record(entry_off, data)
        buf += data
        records.append((inner, entry_off, len(data)))

    # ----- Encoded entries blob -----
    encoded = bytearray()
    enc_offsets: dict[str, int] = {}
    for inner, entry_off, dsize in records:
        enc_offsets[inner] = len(encoded)
        encoded += _encoded_entry(entry_off, dsize)

    # ----- FullDirectoryIndex: group inner paths by directory -----
    from collections import defaultdict
    dirs: dict[str, dict[str, int]] = defaultdict(dict)
    for inner, _eo, _ds in records:
        # mount is "../../../"; dir index keys are paths relative to mount, with
        # a leading "/" on the dir and the bare filename.
        slash = inner.rfind("/")
        d = inner[:slash + 1] if slash != -1 else ""
        fn = inner[slash + 1:] if slash != -1 else inner
        dirs["/" + d]["" + fn] = enc_offsets[inner]

    dirblob = bytearray()
    dirblob += struct.pack("<i", len(dirs))
    for d, filemap in dirs.items():
        dirblob += _write_fstring(d)
        dirblob += struct.pack("<i", len(filemap))
        for fn, eo in filemap.items():
            dirblob += _write_fstring(fn)
            dirblob += struct.pack("<i", eo)

    # PathHashIndex omitted (hasPathHashIndex=0); FullDirIndex is enough for UE.

    # ----- Primary index -----
    data_section_size = len(buf)
    # We will place: [data] [primary index] [full dir index]
    primary = bytearray()
    primary += _write_fstring(MOUNT_POINT)
    primary += struct.pack("<i", len(records))          # NumEntries
    primary += struct.pack("<Q", 0)                     # PathHashSeed
    primary += struct.pack("<i", 0)                     # bReaderHasPathHashIndex
    primary += struct.pack("<i", 1)                     # bReaderHasFullDirectoryIndex
    dir_off_placeholder = len(primary)
    primary += struct.pack("<q", 0)                     # FullDirIndexOffset (fixup)
    primary += struct.pack("<q", len(dirblob))          # FullDirIndexSize
    primary += hashlib.sha1(bytes(dirblob)).digest()    # FullDirIndexHash
    primary += struct.pack("<i", len(encoded))          # EncodedPakEntriesSize
    primary += encoded

    index_offset = data_section_size
    dir_index_offset = index_offset + len(primary)
    struct.pack_into("<q", primary, dir_off_placeholder, dir_index_offset)

    body = bytes(buf) + bytes(primary) + bytes(dirblob)
    index_size = len(primary)
    index_hash = hashlib.sha1(bytes(primary)).digest()

    # ----- Footer -----
    footer = bytearray()
    footer += b"\x00" * 16                              # EncryptionKeyGuid
    footer += struct.pack("<B", 0)                      # bEncryptedIndex
    footer += struct.pack("<I", PAK_MAGIC)
    footer += struct.pack("<i", PAK_V11)
    footer += struct.pack("<q", index_offset)
    footer += struct.pack("<q", index_size)
    footer += index_hash
    # compression methods: 5 zeroed 32-byte slots (none used)
    footer += b"\x00" * (COMP_NAME_SLOTS * COMP_NAME_LEN)

    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(body)
        f.write(footer)
