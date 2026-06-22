"""Interprex Python sidecar.

A tiny localhost HTTP server the TS frontend drives through callPython().
It does ONE job: turn engine files into TranslationString and back. No LLM
logic lives here — translation happens in the frontend behind llm.ts.

Run standalone for dev:  python main.py
Tauri launches the bundled binary of this as a sidecar in production.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path

is_frozen = getattr(sys, "frozen", False)

handlers = [logging.FileHandler("interprex.log", encoding="utf-8")]
if not is_frozen:
    handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=handlers
)
logger = logging.getLogger("interprex")

class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue
    def emit(self, record):
        self.log_queue.put(self.format(record))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from parsers import detect_engine, get_parser
from parsers.renpy import get_char_limit, get_source_font_and_size
from providers import (
    Calibrator,
    ProviderConfig,
    TranslateItem,
    get_provider,
    list_providers,
)
from providers.base import build_prompt  # noqa: F401 — kept for sidecar test tooling

PORT = 8723  # mirror SIDECAR_PORT in src/lib/ipc.ts
_should_pause = False


def start_watchdog():
    import os
    import sys
    import threading
    import time

    # Only run the watchdog when compiled/frozen (production sidecar).
    # In dev mode, the process is managed manually or killed via start.bat,
    # and parent processes can be short-lived launchers (like PowerShell).
    if not getattr(sys, "frozen", False):
        return

    parent_pid = os.getppid()
    if parent_pid <= 1:
        return

    def watch():
        time.sleep(5)
        while True:
            alive = True
            if sys.platform == "win32":
                import ctypes
                # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, parent_pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                else:
                    err = ctypes.windll.kernel32.GetLastError()
                    # If ERROR_ACCESS_DENIED (5), it's alive. If ERROR_INVALID_PARAMETER (87) or others, it's dead.
                    if err != 5:
                        alive = False
            else:
                try:
                    os.kill(parent_pid, 0)
                except ProcessLookupError:
                    alive = False
                except OSError:
                    pass

            if not alive:
                # Parent process is dead! Exit immediately to avoid zombie state.
                os._exit(0)
            time.sleep(2)

    t = threading.Thread(target=watch, daemon=True)
    t.start()


start_watchdog()

app = FastAPI(title="Interprex sidecar")

# The frontend runs on a different origin (http://localhost:1420 in dev, or the
# tauri:// app origin in a build) than this sidecar, so the webview treats every
# call as cross-origin and blocks the response without these headers. curl has
# no such rule, which is why the sidecar can look "up" yet the app shows it
# offline. Localhost-only sidecar, so allowing all origins here is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class DetectReq(BaseModel):
    root: str


class ExtractReq(BaseModel):
    root: str
    engine: str
    sub_paths: list[str] = []


class InjectReq(BaseModel):
    root: str
    engine: str
    translations: dict[str, str]
    target_lang: str | None = None
    sub_paths: list[str] = []
    # Font style for the swapped-in non-Latin font: "smooth" (Noto) or "pixel"
    # (bitmap font matching pixel-art games). Ren'Py only; ignored by others.
    font_style: str = "smooth"
    # id -> font shrink factor (<1.0) for captions that still overflowed after
    # the scheduler's re-ask, from the /translate done event's "size_fixes".
    # Ren'Py only; ignored by other engines.
    size_fixes: dict[str, float] = {}


class DetectModsReq(BaseModel):
    root: str


class BackupStatusReq(BaseModel):
    root: str


class BackupRestoreReq(BaseModel):
    root: str


class BackupDiscardReq(BaseModel):
    root: str


class BackupCreateReq(BaseModel):
    root: str
    files: list[str]


class TItem(BaseModel):
    id: str
    text: str
    context: str = ""
    file: str = ""
    path: list[str] = []


class TranslateReq(BaseModel):
    provider: str
    items: list[TItem]
    target_lang: str
    glossary: dict[str, str] = {}
    base_url: str = ""
    api_key: str = ""
    api_key_2: str = ""
    # Optional list of API keys for providers that rotate across many (3-4+).
    # Supersedes api_key/api_key_2 when present; those stay for compatibility.
    api_keys: list[str] = []
    model: str = ""
    max_context_tokens: int = 0
    max_batch_size: int = 30
    root: str = ""
    engine: str = ""
    # Parallelism: number of concurrent worker threads PER api key (1..10). Total
    # workers = threads * number-of-keys. Cloud only; local providers send 1.
    threads: int = 1
    # Minimum wall-clock duration (seconds) a single request must occupy: if a
    # batch finishes faster, the worker sleeps the remainder before claiming the
    # next one. Lets the user pace requests under a provider's per-minute limit.
    delay_seconds: float = 0.0
    free_only: bool = False
    # Font style for UI-width fitting: the translation is measured against the
    # SAME font inject will write ("smooth" Noto vs "pixel" bitmap) so the budget
    # matches what the player sees. Ren'Py menu choices only.
    font_style: str = "smooth"


# Default window when the UI doesn't constrain it (cloud models, big local ones).
_DEFAULT_CONTEXT_TOKENS = 8192


@app.post("/ping")
def ping() -> dict:
    return {"ok": True}


@app.post("/pause")
def pause() -> dict:
    global _should_pause
    _should_pause = True
    try:
        import renpy_python_translator
        renpy_python_translator.set_paused(True)
    except Exception:
        pass
    logger.info("Translation paused")
    return {"ok": True}


@app.post("/resume")
def resume() -> dict:
    global _should_pause
    _should_pause = False
    try:
        import renpy_python_translator
        renpy_python_translator.set_paused(False)
    except Exception:
        pass
    logger.info("Translation resumed")
    return {"ok": True}


@app.post("/providers")
def providers() -> dict:
    return {"providers": list_providers()}


class ModelsReq(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str = ""
    free_only: bool = False


@app.post("/models")
def models(req: ModelsReq) -> dict:
    """List the chosen backend's models and which one is active right now (the
    loaded local model). Drives the UI model dropdown so the user picks instead
    of typing. Never errors on a down server — returns empty, UI falls back to a
    free-text field."""
    provider = get_provider(req.provider)
    cfg = ProviderConfig(
        base_url=req.base_url,
        api_key=req.api_key,
        free_only=req.free_only,
    )
    models = provider.list_models(cfg)
    return {
        "models": models,
        "active": provider.active_model(cfg, models),
    }


# --- proxy autocheck --------------------------------------------------------
# After the user pastes a proxy URL, probe each cloud provider DIRECTLY first; a
# provider reachable without the proxy should keep going direct (faster, no extra
# hop, doesn't burn the proxy's Vercel quota). Only providers that are geo-blocked
# or network-unreachable get routed through the proxy. The probe lists models
# (a GET) — it never invokes a model, so it spends NO money and no model quota.

# Direct host + how to build the GET-models URL & auth header, per cloud provider.
_PROBE_SPECS: dict[str, dict] = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "header": "x-goog-api-key",
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1/models",
        "header": "authorization",  # Bearer
    },
}

# Same endpoints reached THROUGH the proxy: keep the provider's path, swap host to
# the proxy base, and tag x-provider so the proxy routes to the right upstream.
def _proxy_probe_url(provider: str, proxy_base: str) -> str:
    base = proxy_base.rstrip("/")
    if provider == "gemini":
        # Proxy strips a leading /v1; the gemini path is /v1beta/... so it survives.
        return f"{base}/v1beta/models"
    return f"{base}/models"  # openrouter & other openai-compat → {base}/models


def _classify_probe(status: int, body: str) -> str:
    """ok | geoblock | auth | unreachable, from an HTTP probe response."""
    b = (body or "").lower()
    if 200 <= status < 300:
        return "ok"
    geo_markers = ("location", "region", "country", "not available", "not supported",
                   "unsupported", "geo", "blocked")
    if status in (403, 451) or any(k in b for k in geo_markers):
        return "geoblock"
    if status in (401, 407) or "invalid" in b or "unauthor" in b or "api key" in b:
        # Server reachable, key just wrong — direct still works, proxy won't help.
        return "auth"
    # Any other status means we DID reach the server → direct is usable.
    return "ok"


def _probe(url: str, header: str, api_key: str, x_provider: str | None) -> str:
    import httpx
    headers = {}
    if api_key:
        headers[header] = api_key if header != "authorization" else f"Bearer {api_key}"
    if x_provider:
        headers["x-provider"] = x_provider
    try:
        resp = httpx.get(url, headers=headers, timeout=12)
        return _classify_probe(resp.status_code, resp.text)
    except Exception:
        return "unreachable"  # DNS/TLS/connect failure → network-level block


class ProxyAutocheckReq(BaseModel):
    proxy_url: str
    # provider id -> api key (may be empty; probe still works, returns "auth").
    providers: dict[str, str] = {}


@app.post("/proxy/autocheck")
def proxy_autocheck(req: ProxyAutocheckReq) -> dict:
    """For each cloud provider, decide direct vs proxy and why. Returns
    {results: {provider: {mode, reason}}}. mode is "direct" (use the provider
    server, base_url should be cleared), "proxy" (route via proxy_url), or
    "unknown" (probe inconclusive — left as direct). Never raises."""
    results: dict[str, dict] = {}
    for prov, key in (req.providers or {}).items():
        spec = _PROBE_SPECS.get(prov)
        if not spec:
            continue  # local/unknown provider — proxy doesn't apply
        direct = _probe(spec["url"], spec["header"], key, None)
        if direct in ("ok", "auth"):
            results[prov] = {"mode": "direct", "reason": direct}
            continue
        # Direct geo-blocked or unreachable — try the proxy if one was given.
        if req.proxy_url:
            via = _probe(_proxy_probe_url(prov, req.proxy_url), spec["header"], key, prov)
            if via in ("ok", "auth"):
                results[prov] = {"mode": "proxy", "reason": direct}
            else:
                results[prov] = {"mode": "unknown", "reason": f"direct:{direct},proxy:{via}"}
        else:
            results[prov] = {"mode": "unknown", "reason": direct}
    return {"results": results}


class KeyLimitsReq(BaseModel):
    provider: str
    base_url: str = ""
    api_key: str = ""


@app.post("/key_limits")
def key_limits(req: KeyLimitsReq) -> dict:
    """Per-key rate/usage info for the UI's daily free-request budget badge
    (OpenRouter only today). Returns {} for providers that don't implement it or
    on any failure, so the UI simply hides the badge. Never errors."""
    provider = get_provider(req.provider)
    fn = getattr(provider, "key_limits", None)
    if fn is None:
        return {}
    cfg = ProviderConfig(base_url=req.base_url, api_key=req.api_key)
    try:
        return fn(cfg) or {}
    except Exception:
        return {}


# --- in-app folder browser --------------------------------------------------
# The custom folder picker (themed, vs the un-styleable native OS dialog) needs
# the sidecar to enumerate drives and subfolders. All of this must DEGRADE, never
# raise: an inaccessible/locked folder returns an empty list, not a 500.

class FsListReq(BaseModel):
    path: str = ""  # "" => list drives / roots


def _list_drives() -> list[dict]:
    """Available roots. On Windows, the mounted drive letters; elsewhere, '/'."""
    import os
    import string
    out: list[dict] = []
    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                out.append({"name": f"{letter}:", "path": root, "is_dir": True})
    else:
        out.append({"name": "/", "path": "/", "is_dir": True})
    return out


@app.post("/fs/home")
def fs_home() -> dict:
    """A sensible starting folder for the browser (the user's home)."""
    import os
    try:
        home = os.path.expanduser("~")
        if not os.path.isdir(home):
            home = ""
    except Exception:
        home = ""
    return {"path": home.replace("\\", "/") if home else ""}


def _steam_libraries() -> list[dict]:
    """Every Steam library's `steamapps/common` (games live there), across all
    drives. Steam records extra libraries in `libraryfolders.vdf`; we parse the
    `"path"` entries out of it without a full VDF parser (regex is enough for
    this one field). Returns [] if Steam isn't installed."""
    import os
    import sys
    import re

    if sys.platform == "darwin":
        candidates = [os.path.expanduser("~/Library/Application Support/Steam")]
    elif sys.platform == "linux":
        candidates = [
            os.path.expanduser("~/.local/share/Steam"),
            os.path.expanduser("~/.steam/steam"),
        ]
    else:
        import string
        candidates = []
        for drive in string.ascii_uppercase:
            candidates += [
                f"{drive}:\\Program Files (x86)\\Steam",
                f"{drive}:\\Program Files\\Steam",
                f"{drive}:\\Steam",
            ]
    base = next((c for c in candidates if os.path.isdir(c)), None)
    if not base:
        return []

    libs: list[str] = [base]
    vdf = os.path.join(base, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, "r", encoding="utf-8", errors="ignore") as f:
            for m in re.finditer(r'"path"\s*"([^"]+)"', f.read()):
                p = m.group(1).replace("\\\\", "\\")
                if os.path.isdir(p) and p not in libs:
                    libs.append(p)
    except Exception:
        pass

    out: list[dict] = []
    seen = set()
    for lib in libs:
        common = os.path.join(lib, "steamapps", "common")
        if os.path.isdir(common) and common.lower() not in seen:
            seen.add(common.lower())
            # Label by drive so several Steam libraries are distinguishable.
            drive = os.path.splitdrive(common)[0] or common
            out.append({
                "name": f"Steam ({drive})",
                "path": common.replace("\\", "/"),
            })
    return out


def _get_steam_games(libs: list[dict] = None) -> list[dict]:
    """Scans Steam libraries to return list of installed games."""
    import os
    games = []
    seen_paths = set()
    try:
        libraries = libs if libs is not None else _steam_libraries()
        for lib in libraries:
            common = lib["path"]
            if os.path.isdir(common):
                try:
                    for name in os.listdir(common):
                        path = os.path.join(common, name)
                        if os.path.isdir(path):
                            norm_path = path.replace("\\", "/")
                            norm_lower = norm_path.lower().rstrip("/")
                            if norm_lower not in seen_paths:
                                seen_paths.add(norm_lower)
                                games.append({
                                    "name": name,
                                    "path": norm_path,
                                    "is_dir": True
                                })
                except Exception:
                    continue
    except Exception:
        pass
    
    games.sort(key=lambda g: g["name"].lower())
    return games


def _get_epic_games(manifest_dir: str = None) -> list[dict]:
    """Scans Epic Games manifests to return list of installed games."""
    import os
    import sys
    import json
    if manifest_dir is None:
        if sys.platform == "darwin":
            manifest_dir = os.path.expanduser(
                "~/Library/Application Support/Epic/EpicGamesLauncher/Data/Manifests"
            )
        else:
            manifest_dir = os.path.join(
                os.environ.get("ProgramData", r"C:\ProgramData"),
                "Epic", "EpicGamesLauncher", "Data", "Manifests",
            )
    if not os.path.isdir(manifest_dir):
        return []
    
    games = []
    seen_paths = set()
    try:
        for filename in os.listdir(manifest_dir):
            if filename.endswith(".item"):
                filepath = os.path.join(manifest_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                        data = json.load(f)
                    
                    name = data.get("DisplayName")
                    location = data.get("InstallLocation")
                    if name and location:
                        norm_location = os.path.abspath(location).replace("\\", "/")
                        if os.path.isdir(norm_location):
                            norm_lower = norm_location.lower().rstrip("/")
                            if norm_lower not in seen_paths:
                                seen_paths.add(norm_lower)
                                games.append({
                                    "name": name,
                                    "path": norm_location,
                                    "is_dir": True
                                })
                except Exception:
                    continue  # skip malformed or unreadable file
    except Exception:
        pass
    
    games.sort(key=lambda g: g["name"].lower())
    return games


def _simple_dirs(label: str, paths: list[str]) -> list[dict]:
    import os
    out = []
    for p in paths:
        if os.path.isdir(p):
            out.append({"name": label, "path": p.replace("\\", "/")})
            break  # first existing wins for single-location launchers
    return out


@app.post("/fs/shortcuts")
def fs_shortcuts() -> dict:
    """Quick-jump buttons for installed game launchers — only the ones actually
    present, so the UI shows none if the user has no launchers. Steam can yield
    several (one per library/drive). Never raises."""
    shortcuts: list[dict] = []
    try:
        steam_games = _get_steam_games()
        if steam_games:
            shortcuts.append({"name": "Steam", "path": "steam_games_library"})
    except Exception:
        pass

    try:
        epic_games = _get_epic_games()
        if epic_games:
            shortcuts.append({"name": "Epic Games", "path": "epic_games_library"})
    except Exception:
        pass

    import os
    import sys
    gog = []
    if sys.platform == "darwin":
        gog.append(os.path.expanduser("~/Library/Application Support/GOG Galaxy/Games"))
    elif sys.platform == "linux":
        gog.append(os.path.expanduser("~/GOG Games"))
        gog.append(os.path.expanduser("~/.local/share/GOG Galaxy/Games"))
    else:
        import string
        for drive in string.ascii_uppercase:
            gog.append(f"{drive}:\\GOG Games")
            gog.append(f"{drive}:\\Program Files (x86)\\GOG Galaxy\\Games")
    try:
        shortcuts += _simple_dirs("GOG", gog)
    except Exception:
        pass

    return {"shortcuts": shortcuts}


@app.post("/fs/list")
def fs_list(req: FsListReq) -> dict:
    """List sub-DIRECTORIES of `path` (files are irrelevant to a folder picker).
    Empty path => drive list. Returns the resolved absolute path, its parent (or
    null at a root), and the child folders sorted case-insensitively. Hidden and
    inaccessible entries are skipped; a bad path falls back to the drive list so
    the UI never dead-ends."""
    import os

    raw = (req.path or "").strip()
    if raw == "steam_games_library":
        return {
            "path": "steam_games_library",
            "parent": None,
            "is_root": False,
            "entries": _get_steam_games()
        }
    if raw == "epic_games_library":
        return {
            "path": "epic_games_library",
            "parent": None,
            "is_root": False,
            "entries": _get_epic_games()
        }
    if not raw:
        return {"path": "", "parent": None, "is_root": True, "entries": _list_drives()}

    # Normalise; if it's not a real directory, degrade to drive list.
    try:
        path = os.path.abspath(raw)
    except Exception:
        path = raw
    if not os.path.isdir(path):
        return {"path": "", "parent": None, "is_root": True, "entries": _list_drives()}

    # Parent: None when we're at a filesystem root (drive root / "/"), so the UI
    # shows the drive list when going "up" from there.
    parent = os.path.dirname(path.rstrip("\\/"))
    is_root = (parent == path) or (parent == "" and os.path.dirname(path) == path) \
        or (sys.platform == "win32" and len(path.rstrip("\\/")) == 2 and path.rstrip("\\/")[1] == ":")
    parent_out = None if is_root else parent.replace("\\", "/")

    # Override parent for Steam and Epic game roots so going "Up" returns to the virtual library
    if parent_out:
        try:
            steam_libs = [lib["path"].lower().rstrip("/") for lib in _steam_libraries()]
            if parent_out.lower().rstrip("/") in steam_libs:
                parent_out = "steam_games_library"
            else:
                epic_paths = [g["path"].lower().rstrip("/") for g in _get_epic_games()]
                if path.lower().replace("\\", "/").rstrip("/") in epic_paths:
                    parent_out = "epic_games_library"
        except Exception:
            pass

    entries: list[dict] = []
    try:
        with os.scandir(path) as it:
            for e in it:
                name = e.name
                if name.startswith(".") or name.startswith("$"):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        entries.append({
                            "name": name,
                            "path": e.path.replace("\\", "/"),
                            "is_dir": True,
                        })
                except OSError:
                    continue  # broken symlink / access denied on this entry
    except (PermissionError, OSError):
        entries = []  # locked folder: show it as empty rather than erroring

    entries.sort(key=lambda d: d["name"].lower())
    return {
        "path": path.replace("\\", "/"),
        "parent": parent_out,
        "is_root": False,
        "entries": entries,
    }


def _translate_stream(req: TranslateReq):
    """NDJSON progress stream for one /translate run. The heavy lifting — the
    parallel worker pool, priority ramp-down, key-failover reclaim, pacing and
    pause-correctness — lives in scheduler.TranslationScheduler. This wrapper just
    resets the process-wide pause flag and hands the scheduler a callable to read
    it, keeping the existing /pause and /resume endpoints working unchanged."""
    global _should_pause
    _should_pause = False
    from scheduler import TranslationScheduler

    sched = TranslationScheduler(req, should_pause=lambda: _should_pause)
    return sched.stream()


@app.post("/translate")
def translate(req: TranslateReq) -> StreamingResponse:
    # NDJSON stream: progress events as batches land, then a final "done" event.
    # text/event-stream would also work, but plain NDJSON keeps the client a
    # one-line split — no SSE framing to parse.
    return StreamingResponse(_translate_stream(req),
                             media_type="application/x-ndjson")


@app.post("/detect")
def detect(req: DetectReq) -> dict:
    return {"engine": detect_engine(req.root)}


class RenpyRiskReq(BaseModel):
    root: str


@app.post("/renpy/risk")
def renpy_risk(req: RenpyRiskReq) -> dict:
    """Static text-overflow risk report for a Ren'Py game (READ-ONLY, no engine
    run). Degrades to an 'unknown' verdict rather than raising."""
    try:
        from parsers.renpy_risk import analyze
        return analyze(req.root)
    except Exception as e:  # never 500 a diagnostic
        return {"dialogue_overflow_risk": "unknown",
                "dialogue_reason": f"risk analysis failed: {e}"}


class RenpyLintReq(BaseModel):
    root: str
    lang: str | None = None
    timeout: int = 240


@app.post("/renpy/lint")
def renpy_lint(req: RenpyLintReq) -> dict:
    """Run the game's OWN Ren'Py engine `lint` over the project (including our
    injected tl/<lang>/ files) and report findings split into ours/actionable vs
    pre-existing. Degrades gracefully (available=False) if the engine exe isn't
    present on the machine."""
    try:
        from parsers.renpy import lint_with_engine
        return lint_with_engine(req.root, req.lang, timeout=req.timeout)
    except Exception as e:  # never 500
        return {"available": False, "ours": [], "ours_count": 0,
                "actionable_count": 0, "other_count": 0,
                "reason": f"lint failed: {e}"}


@app.post("/detect_mods")
def detect_mods(req: DetectModsReq) -> dict:
    import os
    import json
    root = req.root
    
    # 1. Locate the mods directory
    mods_dir = root
    common_subdirs = ["Mods", "FactoryGame/Mods", "BepInEx/plugins", "game/mods"]
    for sub in common_subdirs:
        parts = sub.split("/")
        curr = root
        found_sub = True
        for part in parts:
            found_part = None
            if os.path.isdir(curr):
                try:
                    for name in os.listdir(curr):
                        if name.lower() == part.lower():
                            found_part = name
                            break
                except Exception:
                    pass
            if found_part:
                curr = os.path.join(curr, found_part)
            else:
                found_sub = False
                break
        if found_sub and os.path.isdir(curr):
            mods_dir = curr
            break

    # Compute game root: the directory that CONTAINS "FactoryGame" (or equivalent).
    # The parser needs game root to find global.utoc, Paks, etc.
    game_root = root
    # Walk up from mods_dir to find the game root (parent of FactoryGame).
    candidate = mods_dir
    while True:
        parent = os.path.dirname(candidate)
        if parent == candidate:
            break
        # Check if this parent contains "FactoryGame" directory
        if os.path.isdir(os.path.join(parent, "FactoryGame")):
            game_root = parent
            break
        # Also check if THIS dir is FactoryGame
        if os.path.basename(candidate).lower() == "factorygame":
            game_root = parent
            break
        candidate = parent

    # 2. Load project file (if any) to look up translations
    project_path = os.path.join(root, "Interprex", "project.json")
    if not os.path.isfile(project_path):
        project_path = os.path.join(root, ".interprex", "project.json")
    project_data = {}
    if os.path.isfile(project_path):
        try:
            with open(project_path, "r", encoding="utf-8") as f:
                project_data = json.load(f)
        except Exception as e:
            logger.error(f"Failed to read project json for stats: {e}")

    # 3. List direct subdirectories inside mods_dir
    ignore_dirs = {
        "bin", "obj", ".vs", "node_modules", "venv", ".git", ".interprex_backups",
        "appdata", "temp"
    }
    
    mods_list = []
    
    def count_mod_strings(mod_engine: str | None, mod_rel_path: str) -> tuple[int, int]:
        if not mod_engine:
            return 0, 0
        try:
            mod_full = os.path.join(root, *mod_rel_path.split("/")) if os.path.isdir(root) else mod_rel_path
            p = Path(mod_full)
            logger.info(f"count_mod_strings: root={root}, rel={mod_rel_path}, full={mod_full}, exists={p.exists()}")
            uplugins = list(p.glob("*.uplugin"))
            logger.info(f"uplugins found: {uplugins}")
            for uplugin in uplugins:
                return 0, 1
            paks = p / "Content" / "Paks"
            if paks.is_dir():
                for _ in paks.rglob("*.utoc"):
                    return 0, 1
                for _ in paks.rglob("*.pak"):
                    return 0, 1
            for _ in p.rglob("*.uasset"):
                return 0, 1
            for _ in p.rglob("*.locres"):
                return 0, 1
            return 0, 0
        except Exception as e:
            logger.error(f"count_mod_strings error: {e}")
            return 0, 0

    if os.path.isdir(mods_dir):
        try:
            for name in os.listdir(mods_dir):
                full_path = os.path.join(mods_dir, name)
                if not os.path.isdir(full_path) or name.startswith("."):
                    continue
                if name.lower() in ignore_dirs:
                    continue
                    
                # If this is the "GameFeatures" directory, scan its subdirectories instead of the folder itself
                if name.lower() == "gamefeatures":
                    try:
                        for sub_name in os.listdir(full_path):
                            sub_full_path = os.path.join(full_path, sub_name)
                            if os.path.isdir(sub_full_path) and not sub_name.startswith(".") and sub_name.lower() not in ignore_dirs:
                                engine = detect_engine(sub_full_path)
                                rel_path = os.path.relpath(sub_full_path, root).replace("\\", "/")
                                x, n = count_mod_strings(engine, rel_path)
                                mods_list.append({
                                    "name": sub_name,
                                    "path": rel_path,
                                    "engine": engine,
                                    "translated_count": x,
                                    "total_count": n
                                })
                    except Exception as e:
                        logger.error(f"Error scanning GameFeatures directory {full_path}: {e}")
                    continue
                    
                engine = detect_engine(full_path)
                rel_path = os.path.relpath(full_path, root).replace("\\", "/")
                x, n = count_mod_strings(engine, rel_path)
                mods_list.append({
                    "name": name,
                    "path": rel_path,
                    "engine": engine,
                    "translated_count": x,
                    "total_count": n
                })
        except Exception as e:
            logger.error(f"Error scanning mods directory {mods_dir}: {e}")
            
    # 4. Sort: mods with total_count > 0 first (descending by total_count, then name), then total_count == 0
    mods_list.sort(key=lambda m: (m["total_count"] == 0, -m["total_count"], m["name"].lower()))

    return {
        "mods_dir": mods_dir.replace("\\", "/"),
        "game_root": game_root.replace("\\", "/"),
        "mods": mods_list
    }



@app.post("/extract")
def extract(req: ExtractReq) -> dict:
    parser = get_parser(req.engine)
    strings = [s.to_dict() for s in parser.extract(req.root, req.sub_paths)]
    return {"strings": strings}


@app.post("/inject")
def inject(req: InjectReq) -> dict:
    from fastapi import HTTPException
    try:
        parser = get_parser(req.engine)
        # font_style is Ren'Py-specific (font swap); pass only when the parser's
        # inject accepts it so other engines' signatures stay untouched.
        import inspect
        inject_params = inspect.signature(parser.inject).parameters
        if "font_style" in inject_params:
            kwargs = {"font_style": req.font_style}
            # size_fixes is Ren'Py-only too; pass only when supported.
            if "size_fixes" in inject_params and req.size_fixes:
                kwargs["size_fixes"] = req.size_fixes
            written = parser.inject(
                req.root, req.translations, req.target_lang, req.sub_paths,
                **kwargs,
            )
        else:
            written = parser.inject(req.root, req.translations, req.target_lang, req.sub_paths)
        if hasattr(parser, "finalize_backups"):
            parser.finalize_backups(req.root)
        return {"written": written}
    except (PermissionError, OSError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Permission denied. Please make sure the game is closed and try again. Error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _validatable_lang_dir(engine: str, target_lang: str) -> str | None:
    """The on-disk language-directory name an engine uses inside a `tl/` tree, or
    None if the engine has no such convention. Engine-agnostic: we ask the
    engine's own parser how it names the dir (`_lang_dir`) rather than hardcoding
    'renpy' — so a new engine that nests translations under `tl/<lang>/` works
    for free, and engines without `tl/` simply return None."""
    if not target_lang:
        return None
    try:
        parser = get_parser(engine)
    except Exception:
        return None
    lang_dir = getattr(parser, "_lang_dir", None)
    if callable(lang_dir):
        try:
            return lang_dir(target_lang)
        except Exception:
            return None
    return None


def _collect_validatable_files(engine: str, root: str, target_lang: str) -> list[str]:
    """Walk `root` for files the validator should check. Always skips
    `.interprex_backups`.

    For engines that use a `tl/` tree (Ren'Py), we validate ONLY the files we
    actually wrote — i.e. the active target language's `tl/<lang>/` dir. Anything
    OUTSIDE `tl/` is off-limits: that's the engine runtime (`renpy/common/*.rpy`),
    the game's own untouched scripts, and the loose `.rpy` we extracted for
    inline-Python translation. We must not lint/autofix those — they aren't our
    `tl/` output, the line-based validator can't parse their multiline `$` /
    `python:` blocks (false "( was never closed" / "unexpected indent"), and the
    inline-Python path already proves them correct by compiling them with the
    GAME'S OWN Ren'Py runtime. Engines with no `tl/` convention (lang_dir None)
    are walked normally."""
    import os
    lang_dir = _validatable_lang_dir(engine, target_lang)
    files: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        norm = dirpath.replace("\\", "/")
        if ".interprex" in norm:
            continue
        parts = norm.split("/")
        if lang_dir is not None:
            # tl/-using engine: accept ONLY paths inside the active tl/<lang>/.
            if "tl" not in parts:
                continue
            ti = parts.index("tl")
            sub = parts[ti + 1] if ti + 1 < len(parts) else None
            if sub != lang_dir:
                continue
        for fn in filenames:
            if engine == "renpy" and fn.endswith(".rpy"):
                files.append(os.path.join(dirpath, fn))
            elif engine == "i18n" and fn.endswith((".json", ".ini", ".properties")):
                files.append(os.path.join(dirpath, fn))
    return files


@app.post("/validate")
def validate(req: dict) -> dict:
    from validators import get_validator
    engine = req.get("engine", "")
    root = req.get("root", "")
    target_lang = req.get("target_lang", "")
    validator = get_validator(engine)
    if not validator:
        return {"errors": [], "message": f"No validator for engine: {engine}"}
    changed_files = req.get("files", [])
    if not changed_files:
        changed_files = _collect_validatable_files(engine, root, target_lang)
    all_errors = []
    for fp in changed_files:
        all_errors.extend(validator.validate_file(fp))
    return {"errors": [{"file": e.file, "line": e.line, "message": e.message, "severity": e.severity} for e in all_errors], "count": len(all_errors)}


@app.post("/autofix")
def autofix(req: dict) -> dict:
    from validators import get_validator
    engine = req.get("engine", "")
    root = req.get("root", "")
    api_key = req.get("api_key", "")
    model = req.get("model", "gemini-2.0-flash")
    base_url = req.get("base_url", "")
    target_lang = req.get("target_lang", "")
    validator = get_validator(engine)
    if not validator:
        return {"fixed": 0, "message": f"No validator for engine: {engine}"}
    changed_files = _collect_validatable_files(engine, root, target_lang)
    total_fixed = 0
    fix_log = []
    for max_round in range(3):
        all_errors = []
        for fp in changed_files:
            all_errors.extend(validator.validate_file(fp))
        if not all_errors:
            break
        fix_log.append(f"Round {max_round + 1}: {len(all_errors)} errors found")
        for err in all_errors:
            if not err.file:
                continue
            try:
                with open(err.file, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue
            fixed_content = _llm_fix(api_key, model, base_url, err.file, content, err)
            if fixed_content and fixed_content != content:
                try:
                    with open(err.file, "w", encoding="utf-8") as f:
                        f.write(fixed_content)
                    total_fixed += 1
                    fix_log.append(f"Fixed: {err.file}:{err.line} - {err.message}")
                except Exception as e:
                    fix_log.append(f"Failed to write {err.file}: {e}")
    return {"fixed": total_fixed, "log": fix_log, "rounds": max_round + 1}


def _llm_fix(api_key: str, model: str, base_url: str, file_path: str, content: str, error) -> str | None:
    import re
    line_no = error.line or 1
    lines = content.split("\n")
    broken_line = lines[line_no - 1].strip() if line_no <= len(lines) else ""

    is_openai = base_url and "generativelanguage" not in base_url
    if is_openai:
        import httpx, json
        prompt = (
            "Fix this broken Python line in a Ren'Py script.\n"
            f"Line {line_no}: {broken_line}\n"
            f"Error: {error.message}\n\n"
            "Return ONLY a JSON object: {\"line\":N,\"old\":\"<exact broken line>\",\"new\":\"<fixed line>\"}"
        )
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}
        try:
            resp = httpx.post(url, json=body, headers=headers, timeout=60)
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return None
    else:
        try:
            from google import genai
            from google.genai import types
            from pydantic import BaseModel

            class LineFix(BaseModel):
                line: int
                old: str
                new: str

            client = genai.Client(api_key=api_key)
            prompt = (
                "Fix this broken Python line in a Ren'Py script.\n"
                f"Line {line_no}: {broken_line}\n"
                f"Error: {error.message}\n\n"
                "Return the fix with the corrected line."
            )
            response = client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=LineFix,
                ),
            )
            text = response.text
        except Exception:
            return None

    text = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text).strip()

    import json
    try:
        fix = json.loads(text)
        fix_line = fix["line"] - 1
        if 0 <= fix_line < len(lines) and lines[fix_line].strip() == fix["old"].strip():
            orig_indent = len(lines[fix_line]) - len(lines[fix_line].lstrip())
            fixed_line = fix["new"]
            if not fixed_line.startswith(" " * orig_indent) and not fixed_line.startswith("\t"):
                fixed_line = " " * orig_indent + fixed_line.lstrip()
            lines[fix_line] = fixed_line
            return "\n".join(lines)
    except Exception:
        pass
    return None


def get_backup_dir(root: str) -> str:
    import os
    return os.path.join(root, ".interprex_backups")


def has_backup_dir(root: str) -> bool:
    import os
    backup_dir = get_backup_dir(root)
    if not os.path.isdir(backup_dir):
        return False
    # Check if there are other files besides .gitignore
    for dirpath, _, filenames in os.walk(backup_dir):
        for filename in filenames:
            if filename != ".gitignore":
                return True
    return False


@app.post("/backup/status")
def backup_status(req: BackupStatusReq) -> dict:
    try:
        return {"has_backup": has_backup_dir(req.root)}
    except Exception as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=500, detail=str(e))


def run_with_retry(func, *args, **kwargs):
    import time
    delays = [0.1, 0.2, 0.4, 0.8]
    for attempt, delay in enumerate(delays):
        try:
            return func(*args, **kwargs)
        except PermissionError:
            if attempt == len(delays) - 1:
                raise
            time.sleep(delay)

def atomic_write_file(fpath: str, data: bytes) -> None:
    import os
    import tempfile
    import uuid
    dir_name = os.path.dirname(fpath)
    os.makedirs(dir_name, exist_ok=True)
    tmp_name = f"{os.path.basename(fpath)}.tmp.{uuid.uuid4().hex}"
    tmp_path = os.path.join(dir_name, tmp_name)
    
    with open(tmp_path, "wb") as tf:
        tf.write(data)
        
    try:
        run_with_retry(os.replace, tmp_path, fpath)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                run_with_retry(os.remove, tmp_path)
            except Exception:
                pass
        raise

def retry_rmtree(path: str) -> None:
    import shutil
    run_with_retry(shutil.rmtree, path, ignore_errors=False)


@app.post("/backup/restore")
def backup_restore(req: BackupRestoreReq) -> dict:
    import os
    import hashlib
    from fastapi import HTTPException
    from utils.binary_diff import reverse_patch

    backup_dir = get_backup_dir(req.root)
    if not os.path.isdir(backup_dir):
        return {"success": False, "message": "No backup directory found."}

    metadata_path = os.path.join(backup_dir, "metadata.json")
    if not os.path.exists(metadata_path):
        return {"success": False, "message": "No backup metadata found."}

    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            metadata = json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load backup metadata: {e}")

    # Directories that held only Interprex-created files (e.g. game/tl/<lang>/...).
    # After deleting those files we prune any that ended up empty, bottom-up, so a
    # restore doesn't leave behind orphan empty folders (e.g. tl/russian/tl/None/).
    touched_dirs: set[str] = set()

    try:
        for rel_path, info in metadata.items():
            target_file = os.path.join(req.root, rel_path)
            orig_sha = info.get("orig_sha256")

            if info.get("type") == "created" or not orig_sha:
                if os.path.exists(target_file):
                    run_with_retry(os.remove, target_file)
                    touched_dirs.add(os.path.dirname(target_file))
                if target_file.endswith(".rpy"):
                    rpyc_file = target_file + "c"
                    if os.path.exists(rpyc_file):
                        try:
                            run_with_retry(os.remove, rpyc_file)
                        except Exception:
                            pass
                continue

            # Backups are reverse patches. Two on-disk states (mirrors
            # parsers.base.read_backup_original):
            #   staged    <rel>.orig_temp  -> verbatim original (pre-finalize)
            #   finalized <rel>.patch      -> reverse-patch the current file
            orig_temp = os.path.join(backup_dir, rel_path + ".orig_temp")
            patch_file = os.path.join(backup_dir, rel_path + ".patch")

            if os.path.exists(orig_temp):
                with open(orig_temp, "rb") as f:
                    orig_bytes = f.read()
            elif os.path.exists(patch_file):
                if not os.path.exists(target_file):
                    raise FileNotFoundError(f"Modified file not found to revert: {rel_path}")
                with open(patch_file, "rb") as f:
                    patch_bytes = f.read()
                with open(target_file, "rb") as f:
                    mod_bytes = f.read()
                orig_bytes = reverse_patch(mod_bytes, patch_bytes, strict=True)
            else:
                raise FileNotFoundError(f"No backup (patch or staged) found for {rel_path}")

            # Verify SHA256 of restored bytes before writing.
            actual_sha = hashlib.sha256(orig_bytes).hexdigest()
            if actual_sha != orig_sha:
                raise ValueError(f"SHA256 mismatch for restored file {rel_path}: expected {orig_sha}, got {actual_sha}")

            atomic_write_file(target_file, orig_bytes)
            if target_file.endswith(".rpy"):
                rpyc_file = target_file + "c"
                if os.path.exists(rpyc_file):
                    try:
                        run_with_retry(os.remove, rpyc_file)
                    except Exception:
                        pass

        # Prune directories emptied by the restore (deepest first), so orphan
        # empty folders like tl/russian/tl/None/ don't linger. Walk upward from
        # each touched dir toward req.root, stopping at the first non-empty one.
        root_abs = os.path.abspath(req.root)
        for d in sorted(touched_dirs, key=len, reverse=True):
            cur = os.path.abspath(d)
            while cur.startswith(root_abs) and cur != root_abs:
                try:
                    if os.path.isdir(cur) and not os.listdir(cur):
                        os.rmdir(cur)
                        cur = os.path.dirname(cur)
                    else:
                        break
                except OSError:
                    break

        # Delete the backup folder since all files were successfully restored.
        retry_rmtree(backup_dir)
        return {"success": True}
        
    except (PermissionError, OSError) as e:
        raise HTTPException(
            status_code=400,
            detail=(
                "Permission denied during restore. Some files may not have been restored. "
                f"Please ensure the game is closed and click 'Restore' again. Error: {str(e)}"
            )
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/backup/discard")
def backup_discard(req: BackupDiscardReq) -> dict:
    import os
    from fastapi import HTTPException

    backup_dir = get_backup_dir(req.root)
    if not os.path.isdir(backup_dir):
        return {"success": True, "message": "No backup directory existed."}

    try:
        retry_rmtree(backup_dir)
        return {"success": True}
    except (PermissionError, OSError) as e:
        raise HTTPException(
            status_code=400,
            detail=f"Permission denied while deleting backup. Please close the game and try again. Error: {str(e)}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/backup/create")
def backup_create(req: BackupCreateReq) -> dict:
    import os
    from fastapi import HTTPException

    try:
        engine = detect_engine(req.root) or "i18n"
        parser = get_parser(engine)
        
        backed_up = 0
        for rel_file in req.files:
            rel_file = rel_file.replace("\\", "/")
            fpath = os.path.abspath(os.path.join(req.root, rel_file))
            if os.path.isfile(fpath):
                # Call backup_file which handles compression / staging based on file size
                parser.backup_file(req.root, fpath)
                backed_up += 1
                
        # Finalize backups to calculate patch for >= 10 MB staged files
        if hasattr(parser, "finalize_backups"):
            parser.finalize_backups(req.root)
            
        return {"success": True, "backed_up": backed_up}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ExportZipReq(BaseModel):
    root: str
    engine: str
    target_lang: str


def gather_translation_files(root: str, engine: str, target_lang: str) -> list[str]:
    import os
    import json
    files = set()
    
    # 1. Ren'Py specific translation files (written directly to tl/ or fonts/)
    if engine == "renpy":
        # standard tl folder
        tl_dir = os.path.join(root, "game", "tl", target_lang)
        if os.path.isdir(tl_dir):
            for dirpath, _, filenames in os.walk(tl_dir):
                for fname in filenames:
                    abs_path = os.path.join(dirpath, fname)
                    rel = os.path.relpath(abs_path, root).replace("\\", "/")
                    files.add(rel)
        # fonts folder
        fonts_dir = os.path.join(root, "game", "fonts")
        if os.path.isdir(fonts_dir):
            for dirpath, _, filenames in os.walk(fonts_dir):
                for fname in filenames:
                    abs_path = os.path.join(dirpath, fname)
                    rel = os.path.relpath(abs_path, root).replace("\\", "/")
                    files.add(rel)
        # language switcher file
        for switcher in ["game/interprex_language.rpy", "game/interprex_language.rpyc"]:
            if os.path.isfile(os.path.join(root, switcher)):
                files.add(switcher)
                
    # 2. Files modified in-place (tracked via backups)
    backup_dir = os.path.join(root, ".interprex_backups")
    metadata_path = os.path.join(backup_dir, "metadata.json")
    if os.path.isfile(metadata_path):
        try:
            with open(metadata_path, "r", encoding="utf-8") as f:
                metadata = json.load(f)
            for rel_path in metadata:
                # rel_path is already forward slash relative path from root
                if os.path.isfile(os.path.join(root, rel_path.replace("/", os.sep))):
                    files.add(rel_path)
        except Exception:
            pass

    # 3. Add all files from the Interprex directory (project.json, glossary, caches, etc.)
    interprex_dir = os.path.join(root, "Interprex")
    if os.path.isdir(interprex_dir):
        for dirpath, _, filenames in os.walk(interprex_dir):
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, root).replace("\\", "/")
                files.add(rel)

    # 4. Support legacy .interprex directory if present
    dot_interprex_dir = os.path.join(root, ".interprex")
    if os.path.isdir(dot_interprex_dir):
        for dirpath, _, filenames in os.walk(dot_interprex_dir):
            for fname in filenames:
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, root).replace("\\", "/")
                files.add(rel)
            
    return sorted(list(files))


@app.post("/project/export_zip")
def project_export_zip(req: ExportZipReq) -> dict:
    import os
    import zipfile
    import subprocess
    from fastapi import HTTPException
    
    try:
        # Determine the name of the zip file
        folder_name = os.path.basename(os.path.normpath(req.root))
        zip_name = f"{folder_name}_translation_{req.target_lang}.zip"
        zip_path = os.path.join(req.root, zip_name)
        
        # Gather files
        files_to_zip = gather_translation_files(req.root, req.engine, req.target_lang)
        if not files_to_zip:
            return {"success": False, "message": "No translated files found to pack."}
            
        # Write zip
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for rel_file in files_to_zip:
                abs_fpath = os.path.join(req.root, rel_file.replace("/", os.sep))
                if os.path.isfile(abs_fpath):
                    zipf.write(abs_fpath, rel_file)
                    
        # Highlight in Windows Explorer if on Windows
        if sys.platform == "win32":
            try:
                subprocess.Popen(f'explorer /select,"{os.path.abspath(zip_path)}"')
            except Exception:
                pass
                
        return {
            "success": True, 
            "zip_path": zip_path,
            "zip_name": zip_name,
            "file_count": len(files_to_zip)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class RenpyPythonReq(BaseModel):
    root: str
    api_key: str = ""
    # All keys feed the worker pool (threads x keys) with failover, mirroring the
    # main /translate path. Legacy single api_key kept for back-compat.
    api_keys: list[str] = []
    model: str
    base_url: str | None = None
    # Provider id (gemini / openrouter / ollama / ...). Decides the wire format so
    # a proxy URL isn't mistaken for an OpenAI endpoint. Optional for back-compat.
    provider: str | None = None
    target_lang: str = "russian"
    dry_run: bool = True
    threads: int = 4
    # Per-key pacing seconds (derived from RPM in the UI); 0 = no pacing.
    delay_seconds: float = 0.0
    # No-API: apply inline-Python translations from the cache only. Used by the
    # writeBack path so "Write translation" lays down inline-Python (blog, status,
    # search history) from a prior full run without spending any API quota.
    apply_cached_only: bool = False

@app.post("/renpy/translate_python")
def renpy_translate_python(req: RenpyPythonReq) -> StreamingResponse:
    import queue
    import threading
    import renpy_python_translator

    log_queue = queue.Queue()
    
    # Configure custom handler for renpy_python_translator
    translator_logger = logging.getLogger("renpy_python_translator")
    handler = QueueHandler(log_queue)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    translator_logger.addHandler(handler)
    translator_logger.setLevel(logging.INFO)
    
    args_list = [
        "--root", req.root,
        "--model", req.model,
        "--target-lang", req.target_lang,
        "--threads", str(req.threads),
        "--delay-seconds", str(req.delay_seconds),
    ]
    # Pass ALL keys as a JSON array (survives keys containing commas); fall back to
    # the legacy single key for old callers.
    keys = req.api_keys or ([req.api_key] if req.api_key else [])
    if keys:
        args_list += ["--api-keys", json.dumps(keys)]
    elif req.api_key:
        args_list += ["--api-key", req.api_key]
    if req.base_url:
        args_list += ["--base-url", req.base_url]
    if req.provider:
        args_list += ["--provider", req.provider]
    if req.dry_run:
        args_list.append("--dry-run")
    if req.apply_cached_only:
        args_list.append("--apply-cached-only")

    def run_translator():
        try:
            log_queue.put("Starting translation process...")
            renpy_python_translator.main(args_list)
            log_queue.put("Process completed successfully.")
        except SystemExit as e:
            if e.code != 0:
                log_queue.put(f"Process exited with code {e.code}")
            else:
                log_queue.put("Process finished.")
        except Exception as e:
            log_queue.put(f"Error occurred during translation: {e}")
        finally:
            log_queue.put(None)
            translator_logger.removeHandler(handler)
            
    threading.Thread(target=run_translator, daemon=True).start()
    
    def event_generator():
        while True:
            msg = log_queue.get()
            if msg is None:
                break
            yield msg + "\n"
            
    return StreamingResponse(event_generator(), media_type="text/plain")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    import sys
    import uvicorn
    import threading
    import time
    import os

    # NOTE: parent-death cleanup is handled ONLY by start_watchdog() (top of this
    # file), which uses OpenProcess() — the correct Windows liveness check. An
    # earlier monitor_parent() here used os.kill(ppid, 0), which on Windows is NOT
    # a liveness probe but a CTRL_C_EVENT send: it raised OSError against the Tauri
    # parent (different console group) and the sidecar killed itself ~2s after
    # startup, every run. That was the "Failed to fetch" instability. Removed.

    # When bundled with PyInstaller (console=False) sys.stdout/stderr are None,
    # which crashes uvicorn's default color formatter on isatty().
    # Redirect streams to a log file and skip uvicorn's log config entirely.
    if is_frozen:
        import os
        import tempfile
        log_dir = os.path.join(tempfile.gettempdir(), "interprex")
        os.makedirs(log_dir, exist_ok=True)
        _log = open(os.path.join(log_dir, "sidecar.log"), "w", encoding="utf-8", buffering=1)
        sys.stdout = _log
        sys.stderr = _log

    # Pass --reload to auto-restart the sidecar when a .py file changes, so you
    # don't ctrl-C / rerun on every parser edit. Reload needs the import-string
    # form ("main:app") rather than the app object.
    if "--reload" in sys.argv:
        uvicorn.run("main:app", host="127.0.0.1", port=PORT, reload=True, log_level="info")
    else:
        # log_config=None when frozen: skip uvicorn's TTY-checking color formatter.
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info",
                    log_config=None if is_frozen else uvicorn.config.LOGGING_CONFIG)
