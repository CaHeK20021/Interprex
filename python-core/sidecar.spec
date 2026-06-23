# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec — ONEFILE mode.
# Builds a single self-contained sidecar.exe that unpacks to %TEMP% on first
# run. Startup adds ~2 s on the very first launch (subsequent runs reuse the
# cache), but avoids the DLL-not-found problem that onedir has when Tauri
# copies only the launcher without its _internal/ folder.

import os
from pathlib import Path

HERE = Path(SPECPATH)
ASSETS = HERE / "assets"

import UnityPy
UNITYPY_RESOURCES = os.path.join(os.path.dirname(UnityPy.__file__), "resources")

a = Analysis(
    [str(HERE / "main.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=([
        (str(ASSETS / "fonts"), "assets/fonts"),
    ] if (ASSETS / "fonts").exists() else []) + [
        (str(HERE / "bin"), "bin"),
        (str(HERE / "tools/unrpyc"), "tools/unrpyc"),
        (UNITYPY_RESOURCES, "UnityPy/resources"),
    ] + (
        # Oodle DLL for reading compressed UE4/5 .pak files (unreal4 engine).
        # Lives at the project root in dev; bundle it at the _MEIPASS root so
        # oodle.py finds it via sys._MEIPASS. Optional — packed-pak support is
        # simply unavailable if it's absent at build time.
        [(str(HERE.parent / "oo2core_9_win64.dll"), ".")]
        if (HERE.parent / "oo2core_9_win64.dll").exists() else []
    ),
    hiddenimports=[
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "anyio",
        "anyio._backends._asyncio",
        "starlette.routing",
        "parsers.rpgmaker",
        "parsers.renpy",
        "providers.gemini",
        "providers.openai_compat",
        # Parallel translation scheduler — imported lazily inside main.py's
        # _translate_stream, so name it explicitly to guarantee it lands in the
        # onefile bundle.
        "scheduler",
        "pickletools",
        "UnityPy",
        "UnityPy.helpers.TypeTreeGenerator",
        "UnityPy.resources",
        "brotli",
        "lz4",
        "fsspec",
        "texture2ddecoder",
        "etcpak",
        "astc_encoder",
        "fmod_toolkit",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # NOTE: PIL/Pillow must NOT be excluded — the Ren'Py text-fitting
        # (auto-shrink {size=*}, line-count/width measurement) uses it
        # (parsers/renpy.py: _line_height, _wrapped_line_count, fit_scale_*).
        # Excluding it made every measurement silently degrade to "don't shrink",
        # so box-fit never fired in the BUILT app (only in dev, where PIL is
        # present in the venv). This was the "autosize works in dev, not in the
        # build" bug. numpy/pandas/matplotlib stay excluded (PIL doesn't need them).
        "tkinter", "matplotlib", "numpy", "pandas",
        "pytest", "IPython", "jupyter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# ONEFILE: all binaries + data packed into the exe itself.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,   # <-- included directly (not collected separately)
    a.datas,      # <-- included directly
    name="sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,  # use default %TEMP%\_{MEIXXXXXX}
    console=False,        # no black window — background service
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
# No COLLECT step — onefile bundles everything into the exe.
