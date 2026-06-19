"""Oodle decompression via the game's own `oo2core_*.dll` (ctypes).

UE4/5 paks compress data blocks with Oodle — a proprietary RAD/Epic codec with no
pure-Python decoder. The standard approach (FModel, CUE4Parse, repak) is to call
`OodleLZ_Decompress` from `oo2core_9_win64.dll`. The user ships this DLL in the
build; we only ever DECOMPRESS (write-back uses uncompressed paks), so the encoder
isn't needed.

Graceful: if the DLL is absent, `decompress` raises a clear, actionable error.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys

logger = logging.getLogger(__name__)

_DLL_NAMES = ["oo2core_9_win64.dll", "oo2core_8_win64.dll", "oo2core_7_win64.dll"]
_fn = None  # cached OodleLZ_Decompress function pointer


def _candidate_dirs() -> list[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = []
    # PyInstaller bundle (frozen): DLL placed at _MEIPASS root via sidecar.spec.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        dirs.append(sys._MEIPASS)
    dirs.append(here)                       # python-core/
    dirs.append(os.path.dirname(here))      # project root (dev: DLL lives here)
    return dirs


def _load() -> None:
    global _fn
    if _fn is not None:
        return
    last_err = None
    for d in _candidate_dirs():
        for name in _DLL_NAMES:
            p = os.path.join(d, name)
            if not os.path.isfile(p):
                continue
            try:
                dll = ctypes.WinDLL(p)
                fn = dll.OodleLZ_Decompress
                fn.restype = ctypes.c_longlong
                fn.argtypes = [
                    ctypes.c_void_p, ctypes.c_longlong,   # src, srcLen
                    ctypes.c_void_p, ctypes.c_longlong,   # dst, dstLen
                    ctypes.c_int, ctypes.c_int, ctypes.c_int,  # fuzz, check, verbose
                    ctypes.c_void_p, ctypes.c_longlong,   # dictBase, e
                    ctypes.c_void_p, ctypes.c_void_p,     # cb, cbctx
                    ctypes.c_void_p, ctypes.c_longlong,   # scratch, scratchLen
                    ctypes.c_int,                          # threadPhase
                ]
                _fn = fn
                logger.info("Loaded Oodle from %s", p)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
    raise RuntimeError(
        "oo2core Oodle DLL not found. Place oo2core_9_win64.dll next to the app "
        f"(searched: {_candidate_dirs()}). Last error: {last_err}"
    )


def available() -> bool:
    try:
        _load()
        return True
    except Exception:
        return False


def decompress(comp: bytes, raw_size: int) -> bytes:
    """Decompress one Oodle block into exactly `raw_size` bytes."""
    _load()
    dst = ctypes.create_string_buffer(raw_size)
    n = _fn(comp, len(comp), dst, raw_size,
            1, 0, 0, None, 0, None, None, None, 0, 3)
    if n != raw_size:
        raise RuntimeError(f"Oodle decompress returned {n}, expected {raw_size}")
    return dst.raw[:raw_size]
