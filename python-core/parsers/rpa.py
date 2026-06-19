"""Ren'Py `.rpa` archive reader.

Ren'Py packs a game's assets (scripts, images, audio) into one or more `.rpa`
archives under `game/`. Commercial / itch games often ship their `.rpy` source
INSIDE the archive with nothing loose on disk — so to translate them we must read
`.rpy` straight out of the `.rpa`. (Killer Chat! is exactly this: 0 loose `.rpy`,
88 `.rpy` packed in `game/archive.rpa`.)

We only ever READ. Translations are written to `game/tl/<lang>/` on disk, which
the engine loads in preference to the archive ("Files on disk should be checked
before archives" — renpy/loader.py), so the `.rpa` is never modified. This mirrors
the read-only stance of `pak.py` for Unreal `.pak`.

FORMAT (RPA-2.0 / RPA-3.0, the two in the wild):
  - First line, ASCII: ``RPA-3.0 <hex_index_offset> <hex_key>\n``
    (RPA-2.0 omits the key; treat key as 0 = XOR identity.)
  - At index_offset: ``pickle.loads(zlib.decompress(<rest of file>))``.
  - Index is ``dict[inner_path] -> list of [offset, length, prefix]`` segments
    (usually one segment per file). For RPA-3.0 each ``offset`` and ``length`` is
    XOR-ed with the key; RPA-2.0 stores them plain.
  - A file's bytes = ``prefix`` (usually empty) + the ``length`` bytes read at
    ``offset``. The prefix lets Ren'Py inline a few leading bytes in the index.

Verified against Killer Chat!'s `archive.rpa` (RPA-3.0, 2456 entries).
"""

from __future__ import annotations

import logging
import os
import pickle
import zlib
from pathlib import Path

logger = logging.getLogger(__name__)


class RpaFile:
    __slots__ = ("path", "data")

    def __init__(self, path: str, data: str):
        self.path = path        # inner path relative to game/, forward slashes
        self.data = data        # decoded source text (utf-8), like open(...).read()


def _parse_header(first_line: bytes) -> tuple[int, int] | None:
    """Return (index_offset, key) for an RPA header line, or None if unrecognised.
    RPA-3.0: `RPA-3.0 <hex_offset> <hex_key>`. RPA-2.0: `RPA-2.0 <hex_offset>`."""
    parts = first_line.split()
    if not parts:
        return None
    magic = parts[0]
    try:
        if magic == b"RPA-3.0" and len(parts) >= 3:
            return int(parts[1], 16), int(parts[2], 16)
        if magic == b"RPA-2.0" and len(parts) >= 2:
            return int(parts[1], 16), 0  # no key -> XOR identity
    except ValueError:
        return None
    return None


# Parsed-archive cache. detect() → extract() → inject() each touch the same
# `.rpa`; without this they'd re-open, re-decompress the index, and re-decode
# every `.rpy` three times. Keyed by (path, mtime, size) so an edited archive is
# never served stale. Values are the finished list[RpaFile] per want_suffix.
_RPA_CACHE: dict[tuple, list[RpaFile]] = {}


def _read_index(f, rpa_path: str) -> tuple[int, dict]:
    """Read only the header + the (small) pickled index. No payloads. Returns
    (key, index). Raises RuntimeError on an unrecognised header."""
    hdr = _parse_header(f.readline())
    if hdr is None:
        raise RuntimeError(f"not an RPA-2.0/3.0 archive: {rpa_path}")
    offset, key = hdr
    f.seek(offset)
    index = pickle.loads(zlib.decompress(f.read()))
    return key, index


def _inner_name(name) -> str:
    """Index keys are normally str; be defensive if pickled as bytes."""
    if isinstance(name, bytes):
        name = name.decode("utf-8", "replace")
    return name.replace("\\", "/")


def archive_has_suffix(rpa_path: str, want_suffix: str = ".rpy") -> bool:
    """True if the archive's index contains ANY entry ending in `want_suffix`.
    Reads only the index (not payloads) and short-circuits on the first match —
    the cheap check `detect()` needs. Returns False on an unreadable archive."""
    try:
        with open(rpa_path, "rb") as f:
            _key, index = _read_index(f, rpa_path)
        return any(_inner_name(n).endswith(want_suffix) for n in index)
    except Exception:
        return False


def read_rpa(rpa_path: str, want_suffix: str = ".rpy") -> list[RpaFile]:
    """Read inner files whose path ends with `want_suffix`, decoded as utf-8 text.

    Filters by suffix BEFORE seeking/reading any payload, so a 300 MB archive of
    media is never loaded just to reach its handful of `.rpy`. Result is cached by
    (path, mtime, size). Raises RuntimeError on an unrecognised header; the caller
    wraps this in try/except so a foreign or corrupt `.rpa` is simply skipped."""
    st = os.stat(rpa_path)
    cache_key = (os.path.abspath(rpa_path), st.st_mtime_ns, st.st_size, want_suffix)
    cached = _RPA_CACHE.get(cache_key)
    if cached is not None:
        return cached

    with open(rpa_path, "rb") as f:
        key, index = _read_index(f, rpa_path)

        out: list[RpaFile] = []
        for name, segments in index.items():
            inner = _inner_name(name)
            if not inner.endswith(want_suffix):
                continue

            # Reassemble the file from its segments. Each segment is
            # [offset, length, prefix]; offset/length are XOR-key obfuscated in
            # 3.0 (key=0 in 2.0 -> no-op). `prefix` is inlined leading bytes
            # (usually empty). Keep this arithmetic verbatim — prefix is empty in
            # the games we've seen, so an off-by-prefix bug would stay latent.
            raw = bytearray()
            for seg in segments:
                seg_off = seg[0] ^ key
                seg_len = seg[1] ^ key
                prefix = seg[2] if len(seg) > 2 else b""
                if isinstance(prefix, str):
                    prefix = prefix.encode("latin1")
                raw += prefix
                f.seek(seg_off)
                raw += f.read(seg_len)

            out.append(RpaFile(inner, bytes(raw).decode("utf-8", "replace")))

    _RPA_CACHE[cache_key] = out
    return out


def iter_rpa_files(game_dir: str) -> list[str]:
    """All `.rpa` archives under `game_dir` (recursive), excluding the tl/ tree.
    Sorted for deterministic ordering. Mirrors pak.py's container discovery."""
    base = Path(game_dir)
    if not base.is_dir():
        return []
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in ("tl", "cache")]
        for name in filenames:
            if name.lower().endswith(".rpa"):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


def list_rpa_contents(rpa_path: str, want_suffix: str = ".rpyc") -> list[str]:
    """Return a list of inner paths in the archive matching want_suffix."""
    try:
        with open(rpa_path, "rb") as f:
            _key, index = _read_index(f, rpa_path)
        return [
            _inner_name(n)
            for n in index
            if _inner_name(n).endswith(want_suffix)
        ]
    except Exception as e:
        logger.warning("failed to read index of %s: %s", rpa_path, e)
        return []


def extract_rpa_file(rpa_path: str, inner_path: str, dst_path: str) -> None:
    """Extract a single file from the RPA archive as raw bytes, writing it to dst_path."""
    with open(rpa_path, "rb") as f:
        key, index = _read_index(f, rpa_path)
        
        # Let's find the matching entry.
        target_norm = inner_path.replace("\\", "/").lower()
        matched_key = None
        for name in index:
            if _inner_name(name).lower() == target_norm:
                matched_key = name
                break
        if matched_key is None:
            raise KeyError(f"File {inner_path} not found in archive {rpa_path}")
            
        segments = index[matched_key]
        raw = bytearray()
        for seg in segments:
            seg_off = seg[0] ^ key
            seg_len = seg[1] ^ key
            prefix = seg[2] if len(seg) > 2 else b""
            if isinstance(prefix, str):
                prefix = prefix.encode("latin1")
            raw += prefix
            f.seek(seg_off)
            raw += f.read(seg_len)
            
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with open(dst_path, "wb") as out_f:
            out_f.write(raw)

