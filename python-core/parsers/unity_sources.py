"""Unity multi-source registry — one parser, many structural backends.

Unity games store text in different *layouts*. We do NOT special-case game
titles. We detect **capabilities** (cheap filesystem / Managed markers) and
run only the **sources** that make sense. Every source emits the same
``TranslationString`` contract (stable path + original); extract and inject
must stay paired.

Adding a new layout = one source id + detect rule + extract/inject impl +
selftest. Do not fork ``UnityParser`` per title.

Source ids (stable API / report keys — keep names forever):
  dll            — game Assembly-CSharp* via DllEditor
  typetree_text  — m_Text / m_text via UnityPy typetree
  content_blob   — Naninovel / I2 / Yarn / dialogue-density MonoBehaviour raw
  ui_raw         — small UI MonoBehaviour raw (typetree dead / chrome)
  yaml_prefab    — loose .prefab / .unity / .asset YAML m_Text
  string_table   — Addressables / Unity Localization StringTables
  font_gap       — inject-only conservative TMP PPtr remap (not extract)
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from typing import Any

# ── Source catalog (documentation + enablement policy) ──────────────────────

SOURCE_DLL = "dll"
SOURCE_TYPETREE = "typetree_text"
SOURCE_CONTENT = "content_blob"
SOURCE_UI_RAW = "ui_raw"
SOURCE_YAML = "yaml_prefab"
SOURCE_STRING_TABLE = "string_table"
SOURCE_FONT_GAP = "font_gap"

# Ordered for reports (extract order where it matters).
SOURCE_ORDER: tuple[str, ...] = (
    SOURCE_DLL,
    SOURCE_TYPETREE,
    SOURCE_CONTENT,
    SOURCE_UI_RAW,
    SOURCE_YAML,
    SOURCE_STRING_TABLE,
    SOURCE_FONT_GAP,
)

SOURCE_META: dict[str, dict[str, str]] = {
    SOURCE_DLL: {
        "title": "Game DLLs",
        "summary": "Assembly-CSharp* string literals via DllEditor (middleware denied).",
    },
    SOURCE_TYPETREE: {
        "title": "Typetree UI text",
        "summary": "MonoBehaviour/Text m_Text fields when TypeTreeGenerator works.",
    },
    SOURCE_CONTENT: {
        "title": "Content blobs",
        "summary": "Naninovel / I2 / Yarn / dialogue-density MonoBehaviour raw slots.",
    },
    SOURCE_UI_RAW: {
        "title": "UI raw fallback",
        "summary": "Small menu/chrome MonoBehaviours when typetree is weak.",
    },
    SOURCE_YAML: {
        "title": "YAML prefabs",
        "summary": "Loose .prefab/.unity/.asset m_Text lines (dev layouts).",
    },
    SOURCE_STRING_TABLE: {
        "title": "Localization tables",
        "summary": "Addressables StringTable / Unity Localization entries.",
    },
    SOURCE_FONT_GAP: {
        "title": "Font script gap",
        "summary": "Inject-only: TMP victim→donor PPtr remap outside font bodies.",
    },
}


@dataclass
class UnityCapabilities:
    """Cheap structural facts about a Unity game root (no full asset parse)."""

    has_game_dlls: bool = False
    has_compiled_assets: bool = False
    has_yaml_assets: bool = False
    has_addressables: bool = False
    has_il2cpp: bool = False
    has_mono_managed: bool = False
    # Middleware hints from Managed DLL *filenames* (not content).
    marker_naninovel: bool = False
    marker_i2: bool = False
    marker_yarn: bool = False
    marker_localization: bool = False  # Unity.Localization* dll
    game_dll_count: int = 0
    managed_dir: str | None = None
    aa_dir: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRun:
    """One source's plan + outcome for a single extract/inject pass."""

    id: str
    title: str = ""
    enabled: bool = False
    reason: str = ""
    count: int = 0  # extracted or injected strings
    # Optional extra (e.g. "fallback after typetree miss")
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UnityPipelineReport:
    """What detect saw and which sources fired — for logs, API, UI honesty."""

    phase: str  # "extract" | "inject"
    caps: UnityCapabilities
    sources: list[SourceRun] = field(default_factory=list)
    total: int = 0

    def record(
        self,
        source_id: str,
        count: int,
        *,
        enabled: bool = True,
        reason: str = "",
        detail: str = "",
    ) -> None:
        meta = SOURCE_META.get(source_id, {})
        # Update existing row if we pre-planned enablement.
        for s in self.sources:
            if s.id == source_id:
                s.count = count
                s.enabled = enabled
                if reason:
                    s.reason = reason
                if detail:
                    s.detail = detail
                if not s.title:
                    s.title = meta.get("title", source_id)
                return
        self.sources.append(
            SourceRun(
                id=source_id,
                title=meta.get("title", source_id),
                enabled=enabled,
                reason=reason,
                count=count,
                detail=detail,
            )
        )

    def plan_disabled(self, source_id: str, reason: str) -> None:
        self.record(source_id, 0, enabled=False, reason=reason)

    def plan_enabled(self, source_id: str, reason: str = "capability match") -> None:
        self.record(source_id, 0, enabled=True, reason=reason)

    def finalize(self, total: int) -> None:
        self.total = total
        # Stable order for consumers.
        order = {sid: i for i, sid in enumerate(SOURCE_ORDER)}
        self.sources.sort(key=lambda s: order.get(s.id, 99))

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "engine": "unity",
            "total": self.total,
            "capabilities": self.caps.to_dict(),
            "sources": [s.to_dict() for s in self.sources],
            "active_sources": [s.id for s in self.sources if s.enabled and s.count > 0],
            "enabled_sources": [s.id for s in self.sources if s.enabled],
            "summary": self.summary_line(),
        }

    def summary_line(self) -> str:
        parts: list[str] = []
        for s in self.sources:
            if not s.enabled:
                continue
            if s.count > 0:
                parts.append(f"{s.id}={s.count}")
            elif s.id != SOURCE_FONT_GAP:
                # font_gap is inject-only and may run with 0 string writes
                parts.append(f"{s.id}=0")
        return (
            f"unity {self.phase}: total={self.total} "
            f"[{', '.join(parts) if parts else 'no sources'}]"
        )


def _scan_managed_markers(managed_dir: str | None) -> dict[str, bool]:
    out = {
        "naninovel": False,
        "i2": False,
        "yarn": False,
        "localization": False,
    }
    if not managed_dir or not os.path.isdir(managed_dir):
        return out
    try:
        names = [f.lower() for f in os.listdir(managed_dir) if f.lower().endswith(".dll")]
    except OSError:
        return out
    for n in names:
        if "naninovel" in n or n.startswith("elringus."):
            out["naninovel"] = True
        if n.startswith("i2") or "i2loc" in n or n == "i2.dll":
            out["i2"] = True
        if "yarn" in n:
            out["yarn"] = True
        # Unity Localization *package* (not UnityEngine.LocalizationModule engine stub).
        if n.startswith("unity.localization") or n == "unity.localization.dll":
            out["localization"] = True
    return out


def detect_unity_capabilities(
    root: str,
    *,
    # Late-bound callables avoid circular imports with unity.py helpers.
    find_managed_dir_fn: Any = None,
    find_aa_dir_fn: Any = None,
    iter_files_fn: Any = None,
    is_custom_dll_fn: Any = None,
) -> UnityCapabilities:
    """Filesystem-level capability probe (no UnityPy — safe on any machine)."""
    caps = UnityCapabilities()
    if not root or not os.path.isdir(root):
        caps.notes.append("root missing")
        return caps

    if find_managed_dir_fn is None or find_aa_dir_fn is None:
        # Local import: unity.py imports this module for the registry.
        from . import unity as unity_mod

        find_managed_dir_fn = find_managed_dir_fn or unity_mod.find_managed_dir
        find_aa_dir_fn = find_aa_dir_fn or unity_mod.find_aa_dir
        iter_files_fn = iter_files_fn or unity_mod.iter_files
        is_custom_dll_fn = is_custom_dll_fn or unity_mod.is_custom_dll

    managed = find_managed_dir_fn(root)
    caps.managed_dir = managed
    caps.has_mono_managed = bool(managed)

    markers = _scan_managed_markers(managed)
    caps.marker_naninovel = markers["naninovel"]
    caps.marker_i2 = markers["i2"]
    caps.marker_yarn = markers["yarn"]
    caps.marker_localization = markers["localization"]

    # IL2CPP: GameAssembly next to *_Data, or il2cpp_data / global-metadata.
    try:
        for name in os.listdir(root):
            low = name.lower()
            path = os.path.join(root, name)
            if low in ("gameassembly.dll", "gameassembly.so"):
                caps.has_il2cpp = True
            if low.endswith("_data") and os.path.isdir(path):
                try:
                    data_names = {f.lower() for f in os.listdir(path)}
                except OSError:
                    data_names = set()
                if "il2cpp_data" in data_names or "globalgamemanagers" in data_names:
                    il2 = os.path.join(path, "il2cpp_data")
                    if os.path.isdir(il2) or "gameassembly.dll" in {
                        f.lower() for f in os.listdir(root)
                    }:
                        if os.path.isdir(il2):
                            caps.has_il2cpp = True
    except OSError:
        pass

    aa = find_aa_dir_fn(root)
    caps.aa_dir = aa
    if aa and os.path.isdir(aa):
        for _dp, _dn, filenames in os.walk(aa):
            for f in filenames:
                fl = f.lower()
                if fl.endswith(".bundle") or fl.startswith("catalog"):
                    caps.has_addressables = True
                    break
            if caps.has_addressables:
                break

    dll_n = 0
    for fpath in iter_files_fn(root):
        f = os.path.basename(fpath)
        fl = f.lower()
        if is_custom_dll_fn(f):
            dll_n += 1
        if fl.endswith(".assets"):
            if not fl.endswith((".manifest", ".ress", ".resource")):
                caps.has_compiled_assets = True
        elif fl.startswith("level") and "." not in fl:
            caps.has_compiled_assets = True
        if fl.endswith((".unity", ".prefab", ".asset")):
            caps.has_yaml_assets = True

    caps.game_dll_count = dll_n
    caps.has_game_dlls = dll_n > 0

    if caps.has_il2cpp and not caps.has_mono_managed:
        caps.notes.append(
            "IL2CPP build: DLL string extract may be empty; prefer assets/localization sources."
        )
    if caps.marker_naninovel:
        caps.notes.append(
            "Naninovel runtime DLL present — content_blob is primary dialogue source."
        )
    if not caps.has_game_dlls and not caps.has_compiled_assets and not caps.has_addressables:
        caps.notes.append("No DLL/assets/addressables signals — limited Unity extract.")

    return caps


def plan_sources(caps: UnityCapabilities, *, phase: str) -> UnityPipelineReport:
    """Enable sources from capabilities (policy table). Count filled later."""
    report = UnityPipelineReport(phase=phase, caps=caps)

    # dll
    if caps.has_game_dlls:
        report.plan_enabled(SOURCE_DLL, f"{caps.game_dll_count} game DLL(s)")
    else:
        why = "IL2CPP / no Assembly-CSharp*" if caps.has_il2cpp else "no game DLLs"
        report.plan_disabled(SOURCE_DLL, why)

    # compiled asset sources share has_compiled_assets
    if caps.has_compiled_assets:
        report.plan_enabled(SOURCE_TYPETREE, "compiled .assets/level* present")
        # content always on when assets exist — markers only *hint* density,
        # dialogue-density heuristic still finds non-Naninovel blobs.
        reason = "compiled assets"
        hints = []
        if caps.marker_naninovel:
            hints.append("naninovel")
        if caps.marker_i2:
            hints.append("i2")
        if caps.marker_yarn:
            hints.append("yarn")
        if hints:
            reason = f"compiled assets + markers ({', '.join(hints)})"
        report.plan_enabled(SOURCE_CONTENT, reason)
        # ui_raw is conditional at runtime (typetree miss); mark enabled as "may run"
        report.plan_enabled(
            SOURCE_UI_RAW,
            "available if typetree weak (decided per-file at runtime)",
        )
    else:
        report.plan_disabled(SOURCE_TYPETREE, "no compiled assets")
        report.plan_disabled(SOURCE_CONTENT, "no compiled assets")
        report.plan_disabled(SOURCE_UI_RAW, "no compiled assets")

    if caps.has_yaml_assets:
        report.plan_enabled(SOURCE_YAML, "loose .prefab/.unity/.asset")
    else:
        report.plan_disabled(SOURCE_YAML, "no YAML assets")

    if caps.has_addressables or caps.marker_localization:
        report.plan_enabled(
            SOURCE_STRING_TABLE,
            "Addressables aa/ or Unity.Localization DLL",
        )
    else:
        report.plan_disabled(SOURCE_STRING_TABLE, "no Addressables/Localization signal")

    if phase == "inject":
        # Font gap only when we might write assets and target script needs it —
        # actual script check is in inject; here we only gate on assets.
        if caps.has_compiled_assets:
            report.plan_enabled(
                SOURCE_FONT_GAP,
                "inject-only; runs for non-Latin targets with in-file TMP donor",
            )
        else:
            report.plan_disabled(SOURCE_FONT_GAP, "no compiled assets")
    else:
        report.plan_disabled(SOURCE_FONT_GAP, "inject-only")

    return report
