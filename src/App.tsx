import { useEffect, useState, useRef, useCallback, useMemo, memo, startTransition } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import {
  ping,
  detectEngine,
  extractStrings,
  extractStreaming,
  injectStringsStream,
  listModels,
  getBackupStatus,
  restoreBackup,
  discardBackup,
  exportTranslationZip,
  createBackup,
  pauseTranslation,
  resumeTranslation,
  detectMods,
  keyLimits,
  translateRenpyPython,
  autofixTranslation,
  proxyAutocheck,
  renpyLint,
  type ModInfo,
  type ProxyAutocheckResult,
  type RenpyLintResult,
} from "./lib/ipc";
import {
  translateBatch,
  PROVIDERS,
  type ProviderId,
  type TranslateProgress,
} from "./lib/llm";
import { loadProject, saveProject, mergeStrings, isProjectSaving, resetSavingState } from "./lib/project";
import {
  loadSetting,
  saveSetting,
  loadProviderSetting,
  saveProviderSetting,
  loadProviderKeys,
  saveProviderKeys,
} from "./lib/settings";
import type { Engine, FontStyle, ProjectFile, TranslationString } from "./lib/types";
import { engineLabel } from "./lib/types";
import FolderPicker from "./FolderPicker";
import UpdateOverlay from "./UpdateOverlay";
import {
  useT,
  setUiLang,
  UI_LANGUAGES,
  TARGET_LANGS,
  type TargetLang,
  type StringKey,
} from "./i18n";
import "./App.css";

type Phase = "idle" | "detecting" | "extracting" | "translating" | "saving" | "backing_up" | "injecting" | "autofixing" | "restoring" | "deleting_backup";

// --- OpenRouter daily free-request counter (local, UTC-reset) ---------------
// OpenRouter exposes the daily cap but not how many free requests you've spent
// today, so we count it ourselves. Stored in localStorage; the date stamp is the
// UTC day (YYYY-MM-DD) so the count resets at midnight UTC, matching OpenRouter's
// reset. A request counts the moment it reaches the server — including a 429/503
// error reply, which still burns the quota — so we bump per attempt, not only on
// a successful translation.
function utcDay(): string {
  return new Date().toISOString().slice(0, 10);
}
function readOrUsageCount(): number {
  try {
    if (loadSetting("openrouterUsageDate", "") !== utcDay()) return 0;
    return Number(loadSetting("openrouterUsageCount", "0")) || 0;
  } catch {
    return 0;
  }
}
// Convert a per-key requests-per-minute cap into the minimum seconds each
// request must occupy. The cap is shared by the `threads` workers on one key, so
// together they may fire at most `rpm` requests/min; each worker therefore paces
// to threads×60/rpm seconds. rpm<=0 means "no limit" → no pacing.
function rpmToDelay(rpm: number, threads: number): number {
  if (!rpm || rpm <= 0) return 0;
  return (threads * 60) / rpm;
}

function writeOrUsageCount(total: number): number {
  const safe = Math.max(0, Math.floor(total));
  saveSetting("openrouterUsageDate", utcDay());
  saveSetting("openrouterUsageCount", String(safe));
  return safe;
}

// Rows per page. A big game has thousands of strings; putting them all in the
// DOM in one synchronous render freezes the webview, so the table paginates.
// Translation/write-back still run over EVERY string — this only limits what's
// painted per page.
const PAGE_SIZE = 500;

// Merge id->translation into a project immutably, preserving each entry's
// original text and approved flag. Used both for live per-batch updates and the
// final authoritative merge, so they can't drift apart.
// `sourceStrings` is the extracted TranslationString[] — used as the source of
// truth for `original` so the field is never silently left empty if an entry
// didn't exist yet in project.strings.
function mergeTranslations(
  proj: ProjectFile,
  translations: Record<string, string>,
  sourceStrings?: TranslationString[],
): ProjectFile {
  const byId = sourceStrings
    ? Object.fromEntries(sourceStrings.map((s) => [s.id, s.original]))
    : {};
  const strings = { ...proj.strings };
  for (const [id, translated] of Object.entries(translations)) {
    strings[id] = {
      original: strings[id]?.original || byId[id] || "",
      translated,
      approved: strings[id]?.approved ?? false,
    };
  }
  return { ...proj, strings };
}

// Get regex for foreign words based on the target language of translation
function getForeignWordRegex(targetLang: TargetLang) {
  if (targetLang === "Russian") {
    return /\b[a-zA-Z]+\b/g;
  }
  if (["English", "German", "French", "Spanish", "Portuguese (Brazil)"].includes(targetLang)) {
    return /\b[а-яА-ЯёЁ]+\b|[\u4E00-\u9FAF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]+/g;
  }
  return /\b[a-zA-Z]+\b|\b[а-яА-ЯёЁ]+\b/g;
}

// Check if a word in the text context is a technical token (variable, function call, path, arg, etc.)
function isTechnicalWord(word: string, partText: string, wordIndex: number): boolean {
  // 1. Programming and syntax keywords
  const programmingKeywords = ["true", "false", "none", "self", "import", "def", "config", "renpy", "init", "python", "pass", "return", "eval", "exec", "is", "not", "and", "or", "for", "in", "if", "else", "elif", "try", "except"];
  if (programmingKeywords.includes(word.toLowerCase())) {
    return true;
  }

  // Find boundaries of the full surrounding non-space token (e.g. "img=Transform")
  let tokenStart = wordIndex;
  while (tokenStart > 0 && !/\s/.test(partText[tokenStart - 1])) {
    tokenStart--;
  }
  let tokenEnd = wordIndex + word.length;
  while (tokenEnd < partText.length && !/\s/.test(partText[tokenEnd])) {
    tokenEnd++;
  }
  const token = partText.slice(tokenStart, tokenEnd);

  // 2. File paths or extensions
  if (token.includes("/") || token.includes("\\") || /\.(webp|png|jpg|jpeg|ogg|mp3|wav|rpy|rpyc|json|txt|py|dll|exe)$/i.test(token)) {
    return true;
  }

  // 3. Assignment / arguments (e.g. zoom=0.3)
  if (token.includes("=")) {
    return true;
  }

  // 4. Variables snake_case or containing numbers (e.g. mc_bodyacc, acc4)
  if (token.includes("_") || /[0-9]/.test(token)) {
    return true;
  }

  // 5. Method calls (e.g. .format)
  let beforeIdx = wordIndex - 1;
  while (beforeIdx >= 0 && /\s/.test(partText[beforeIdx])) {
    beforeIdx--;
  }
  if (beforeIdx >= 0 && partText[beforeIdx] === ".") {
    return true;
  }

  // 6. Function calls (e.g. Transform( )
  let afterIdx = wordIndex + word.length;
  while (afterIdx < partText.length && /\s/.test(partText[afterIdx])) {
    afterIdx++;
  }
  if (afterIdx < partText.length && partText[afterIdx] === "(") {
    return true;
  }

  return false;
}

// Get filter button display name based on target language
function getForeignFilterLabel(targetLang: TargetLang) {
  if (targetLang === "Russian") return "С латиницей";
  if (["English", "German", "French", "Spanish", "Portuguese (Brazil)"].includes(targetLang)) return "С кириллицей";
  return "Чужой алфавит";
}

// Get string type display label
function getStringType(s: TranslationString) {
  if (s.path.includes("say")) return "Диалог";
  if (s.path.includes("menu_choice")) return "Выбор в меню";
  if (s.path.includes("uscore") || s.path.includes("inline_python")) return "Python-код";
  if (s.path[0] === "screen") return "Интерфейс";
  if (s.path[0] === "define") return "Имя персонажа";
  return "Обычная";
}

// Helper to get the mod name containing this TranslationString
function getModNameForString(
  s: TranslationString,
  detectedMods: any[],
  gameRoot: string | null,
  modsDir: string | null,
): string {
  if (!modsDir || !gameRoot) return "—";
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();

  // uasset:// path — mod path is the segment before "!"
  if (s.file.startsWith("uasset://")) {
    const inner = s.file.slice("uasset://".length);
    const sepIdx = inner.indexOf("!");
    const modPath = sepIdx >= 0 ? inner.slice(0, sepIdx) : inner;
    const modPathNorm = norm(modPath);
    const matchingMod = detectedMods.find((mod) => {
      const mp = norm(mod.path);
      return modPathNorm === mp || modPathNorm.startsWith(mp + "/");
    });
    if (matchingMod) return matchingMod.name;
    // fallback — leaf directory name
    return modPath.split("/").pop() || "—";
  }

  // Regular path — try matching against mod filesystem paths
  const strAbs = norm(`${gameRoot}/${s.file}`);
  const matchingMod = detectedMods.find((mod) => {
    const modAbs = norm(`${modsDir}/${mod.path}`);
    return strAbs === modAbs || strAbs.startsWith(modAbs + "/");
  });
  if (matchingMod) return matchingMod.name;

  // Fallback — check if any mod name appears in the file path
  const fileLower = s.file.toLowerCase();
  const byName = detectedMods.find((mod) => fileLower.includes(norm(mod.path)));
  return byName ? byName.name : "—";
}

// Helper to get the mod path containing this TranslationString
function getModPathForString(
  s: TranslationString,
  detectedMods: any[],
  gameRoot: string | null,
  modsDir: string | null,
): string | null {
  if (!modsDir || !gameRoot) return null;
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();

  // uasset:// path — mod path is the segment before "!"
  if (s.file.startsWith("uasset://")) {
    const inner = s.file.slice("uasset://".length);
    const sepIdx = inner.indexOf("!");
    const modPath = sepIdx >= 0 ? inner.slice(0, sepIdx) : inner;
    const modPathNorm = norm(modPath);
    const matchingMod = detectedMods.find((mod) => {
      const mp = norm(mod.path);
      return modPathNorm === mp || modPathNorm.startsWith(mp + "/");
    });
    if (matchingMod) return matchingMod.path;
    return modPath;
  }

  // Regular path — try matching against mod filesystem paths
  const strAbs = norm(`${gameRoot}/${s.file}`);
  const matchingMod = detectedMods.find((mod) => {
    const modAbs = norm(`${modsDir}/${mod.path}`);
    return strAbs === modAbs || strAbs.startsWith(modAbs + "/");
  });
  if (matchingMod) return matchingMod.path;

  // Fallback — check if any mod name appears in the file path
  const fileLower = s.file.toLowerCase();
  const byName = detectedMods.find((mod) => fileLower.includes(norm(mod.path)));
  return byName ? byName.path : null;
}

// Highlight foreign words inside translations, ignoring Ren'Py formatting tags and technical tokens.
function highlightLatin(text: string, targetLang: TargetLang) {
  if (!text) return "";
  
  // Split by Ren'Py format tags {...} and variables [...]
  const parts = text.split(/(\{.*?\}|\[.*?\])/g);
  const foreignRegex = getForeignWordRegex(targetLang);
  
  // We need to match keeping track of absolute character indices to verify isTechnicalWord
  return parts.map((part, index) => {
    // If it's a tag or variable interpolation, render it raw
    if (part.startsWith('{') || part.startsWith('[')) {
      return <span key={index} className="tag-node">{part}</span>;
    }
    
    // Split the text part by foreign words
    const foreignRegexCapture = new RegExp(`(${foreignRegex.source})`, 'g');
    const subParts = part.split(foreignRegexCapture);
    
    // Accumulate characters offset to find exact word index
    let currentOffset = 0;
    return (
      <span key={index}>
        {subParts.map((subPart, subIndex) => {
          const isForeign = new RegExp(`^(${foreignRegex.source})$`).test(subPart);
          const startIdx = currentOffset;
          currentOffset += subPart.length;

          if (isForeign && !isTechnicalWord(subPart, part, startIdx)) {
            return (
              <span key={subIndex} className="latin-highlight" title="Инородный символ в переводе">
                {subPart}
              </span>
            );
          }
          return subPart;
        })}
      </span>
    );
  });
}

interface SearchBoxProps {
  onChange: (value: string) => void;
  placeholder?: string;
}

const SearchBox = memo(({ onChange, placeholder }: SearchBoxProps) => {
  const [value, setValue] = useState("");

  useEffect(() => {
    if (value === "") {
      onChange("");
      return;
    }
    const h = setTimeout(() => {
      onChange(value);
    }, 500);
    return () => clearTimeout(h);
  }, [value, onChange]);

  return (
    <div className="search-wrapper">
      <span className="search-icon">🔍</span>
      <input
        type="text"
        className="search-input"
        placeholder={placeholder}
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />
      {value && (
        <button className="search-clear" onClick={() => setValue("")}>
          ✕
        </button>
      )}
    </div>
  );
});

interface TableRowProps {
  s: TranslationString;
  translated?: string;
  isSelected: boolean;
  isJustTranslated: boolean;
  isEditing: boolean;
  isBulkEdit: boolean;
  editingVal: string;
  metadata: string;
  target: TargetLang;
  startEdit: (id: string, current: string, bulk?: boolean) => void;
  commitEdit: (id: string, original: string) => void;
  setEditingVal: (val: string) => void;
  setEditingId: (id: string | null) => void;
  onDragSelectStart: (id: string, isSelected: boolean) => void;
  onDragSelectEnter: (id: string) => void;
  onShiftSelectRange: (id: string) => void;
}

const TableRow = memo(({
  s,
  translated,
  isSelected,
  isJustTranslated,
  isEditing,
  isBulkEdit,
  editingVal,
  metadata,
  target,
  startEdit,
  commitEdit,
  setEditingVal,
  setEditingId,
  onDragSelectStart,
  onDragSelectEnter,
  onShiftSelectRange
}: TableRowProps) => {
  return (
    <tr className={isJustTranslated ? "just-translated-row" : ""}>
      <td style={{ textAlign: "center" }}>
        <input
          type="checkbox"
          checked={isSelected}
          onMouseDown={(e) => {
            e.preventDefault();
            if (e.shiftKey) {
              onShiftSelectRange(s.id);
            } else {
              onDragSelectStart(s.id, isSelected);
            }
          }}
          onMouseEnter={() => onDragSelectEnter(s.id)}
        />
      </td>
      <td>{s.original}</td>
      <td
        className={
          isEditing
            ? (isBulkEdit ? "tr-edit tr-bulk" : "tr-edit")
            : translated
              ? "tr-cell"
              : "tr-cell empty"
        }
        onClick={isEditing ? undefined : (e) => startEdit(s.id, translated ?? "", e.shiftKey)}
        title={!isEditing ? ("Клик — редактировать | Shift+клик — применить ко всем одинаковым") : undefined}
      >
        {isEditing ? (
          <textarea
            autoFocus
            value={editingVal}
            onChange={(e) => setEditingVal(e.target.value)}
            onBlur={() => commitEdit(s.id, s.original)}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                setEditingId(null);
              } else if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.blur();
              }
            }}
          />
        ) : translated ? (
          highlightLatin(translated, target)
        ) : (
          "—"
        )}
      </td>
      <td className="where" title={`${s.file}\n${s.path.join(" › ")}`}>
        {metadata}
      </td>
    </tr>
  );
}, (prevProps, nextProps) => {
  return prevProps.isSelected === nextProps.isSelected &&
         prevProps.isJustTranslated === nextProps.isJustTranslated &&
         prevProps.isEditing === nextProps.isEditing &&
         prevProps.isBulkEdit === nextProps.isBulkEdit &&
         (!nextProps.isEditing || prevProps.editingVal === nextProps.editingVal) &&
         prevProps.metadata === nextProps.metadata &&
         prevProps.target === nextProps.target &&
         prevProps.translated === nextProps.translated &&
         prevProps.s === nextProps.s;
});

export default function App() {
  const { t, lang } = useT();

  function getTargetLangLabel(l: TargetLang): string {
    switch (l) {
      case "Russian": return t("lang_Russian") as string;
      case "English": return t("lang_English") as string;
      case "Spanish": return t("lang_Spanish") as string;
      case "German": return t("lang_German") as string;
      case "French": return t("lang_French") as string;
      case "Japanese": return t("lang_Japanese") as string;
      case "Chinese (Simplified)": return t("lang_Chinese_Simplified") as string;
      case "Korean": return t("lang_Korean") as string;
      case "Portuguese (Brazil)": return t("lang_Portuguese_Brazil") as string;
      default: return l;
    }
  }
  const [sidecarUp, setSidecarUp] = useState<boolean | null>(null);
  const [root, setRoot] = useState<string | null>(null);
  const [engine, setEngine] = useState<Engine | null>(null);
  const [strings, setStrings] = useState<TranslationString[]>([]);
  // Mod sub_paths whose streaming extract has completed this run. Used to clear
  // the per-mod "calculating…" state as each mod's strings land (live counter).
  const [extractedModPaths, setExtractedModPaths] = useState<Set<string>>(new Set());
  const [project, setProject] = useState<ProjectFile | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [hasBackup, setHasBackup] = useState<boolean>(false);
  // Engine-lint result after a translate (real hazards our injection may cause).
  const [lintResult, setLintResult] = useState<RenpyLintResult | null>(null);
  const [target, setTarget] = useState<TargetLang>(
    () => loadSetting("targetLang", "Russian") as TargetLang,
  );
  const [provider, setProvider] = useState<ProviderId>(
    () => loadSetting("provider", "ollama") as ProviderId,
  );
  // Connection settings are stored PER PROVIDER, so switching backend restores
  // that backend's own URL/key/model instead of carrying the previous one's.
  const [baseUrl, setBaseUrl] = useState(() =>
    loadProviderSetting("providerBaseUrl", provider, ""),
  );
  // API keys as a LIST: cloud backends can rotate across several (each key gets
  // its own worker group). Always at least one entry so a field always renders.
  const [apiKeys, setApiKeys] = useState<string[]>(() => loadProviderKeys(provider));
  // Which key rows are revealed (index set). Empty = all masked.
  const [shownKeys, setShownKeys] = useState<Set<number>>(new Set());
  // First non-empty key — used for model discovery / OpenRouter limits.
  const primaryKey = apiKeys.find(Boolean) ?? "";
  // Dedupe to match the scheduler (it dedupes keys server-side): otherwise the
  // worker grid would render threads×duplicateCount cards while the scheduler
  // spawns fewer real workers, leaving phantom "resting" cards forever.
  const nonEmptyKeys = Array.from(new Set(apiKeys.filter(Boolean)));
  // Font style is PER game folder (lives in .interprex.json), not a global pref:
  // a new folder defaults to "smooth", but once switched to "pixel" here it
  // sticks for THIS folder. Reads the project; writing persists + auto-saves.
  const fontStyle: FontStyle = project?.fontStyle ?? "smooth";
  const setFontStyle = (next: FontStyle) => {
    setProject((prev) => {
      if (!prev) return prev;
      const updated = { ...prev, fontStyle: next };
      saveProject(updated).catch((e) => console.error("Save fontStyle failed:", e));
      return updated;
    });
  };
  const [model, setModel] = useState(() =>
    loadProviderSetting("providerModel", provider, ""),
  );
  const [freeOnly, setFreeOnly] = useState<boolean>(
    () => loadSetting("openrouterFreeOnly", "false") === "true",
  );
  const [maxBatchSize, setMaxBatchSize] = useState(() =>
    Number(loadSetting("maxBatchSize", "30")),
  );
  // Parallelism + rate limit, stored PER PROVIDER. threads: workers per key
  // (1..10). rpmLimit: the model's requests-per-minute cap PER KEY that the user
  // reads off their provider dashboard; the per-request pacing delay is DERIVED
  // from it (see rpmToDelay), so the user never thinks in seconds. 0 = no limit.
  const [threads, setThreads] = useState(() =>
    Math.min(10, Math.max(1, Number(loadProviderSetting("providerThreads", provider, "1")) || 1)),
  );
  const [rpmLimit, setRpmLimit] = useState(() =>
    Math.max(0, Number(loadProviderSetting("providerRpm", provider, "0")) || 0),
  );
  // Live per-worker phase (for the status grid card colours), keyed by worker_idx.
  const [workerPhases, setWorkerPhases] = useState<Record<number, string>>({});
  // Is the worker-status panel expanded? Only meaningful when threads*keys > 2.
  const [workersPanelOpen, setWorkersPanelOpen] = useState(false);
  // OpenRouter daily free-request budget badge: {used, cap} or null if N/A.
  const [orUsage, setOrUsage] = useState<{ used: number; cap: number } | null>(null);
  // Today's count BEFORE this run started: the run reports requests_sent as an
  // absolute per-run count, so today's total = baseline + requests_sent.
  const orUsageBaseRef = useRef(0);
  // Ren'Py font-shrink factors from the last translate (id -> factor <1.0), for
  // captions that still overflowed after re-asking. Carried from translateAll to
  // the separate writeBack step that calls inject. A ref so it survives without a
  // re-render; reset at the start of each run.
  const sizeFixesRef = useRef<Record<string, number>>({});
  // True once the proxy autocheck got a decisive verdict. False at launch and
  // after an offline start, so a translate run knows to re-check first.
  const proxyResolvedRef = useRef(false);
  const runSettingsRef = useRef<{
    provider: string;
    model: string;
    threads: number;
    baseUrl: string;
    apiKeys: string[];
  } | null>(null);
  const shouldRestartRef = useRef(false);
  const [error, setError] = useState<string | null>(null);
  // Search box: `searchQuery` is the DEBOUNCED value that the heavy filter
  // (filteredStrings, a pass over all 7000+ rows) actually reads.
  // The instant input state is isolated inside the SearchBox component so that
  // typing does not trigger parent re-renders, preventing input lag on big projects.
  const [searchQuery, setSearchQuery] = useState("");
  // Search mode: all fields, original only, translation only, or none
  const [searchMode, setSearchMode] = useState<"all" | "original" | "translation" | "none">("all");
  // Filter only items containing Latin words in translations
  const [onlyLatinInTranslation, setOnlyLatinInTranslation] = useState(false);
  // Selected translation string IDs for batch operations
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  // Drag-select: mousedown on a checkbox, drag over others to select/deselect
  const dragRef = useRef<{ active: boolean; selecting: boolean; ids: string[] } | null>(null);
  // IDs of strings that were just translated in the current/last batch run
  const [justTranslatedIds, setJustTranslatedIds] = useState<Set<string>>(new Set());
  // Filter strings by source type: all, regular strings (say, screen, define), python (uscore), or none
  const [stringTypeFilter, setStringTypeFilter] = useState<"all" | "regular" | "python" | "none">("all");
  // Filter strings by translation status: all, translated, or untranslated
  const [translationStatusFilter, setTranslationStatusFilter] = useState<"all" | "translated" | "untranslated">("all");
  // In mods mode: set of mod paths to hide from the table list
  const [filterHiddenModPaths, setFilterHiddenModPaths] = useState<Set<string>>(new Set());
  // Zero-based index of the visible table page (PAGE_SIZE rows each).
  const [page, setPage] = useState(0);

  // Filter dropdown state and ref
  const [isFilterMenuOpen, setIsFilterMenuOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsFilterMenuOpen(false);
      }
    };
    const handleMouseUp = () => { dragRef.current = null; };
    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("mouseup", handleMouseUp);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("mouseup", handleMouseUp);
    };
  }, []);

  const handleSearchQueryChange = useCallback((val: string) => {
    setSearchQuery(val);
    setPage(0);
    setJustTranslatedIds(new Set());
  }, []);

  const handleSearchModeCheckbox = (field: "original" | "translation", checked: boolean) => {
    setPage(0);
    if (checked) {
      if (searchMode === "none") {
        setSearchMode(field);
      } else if (searchMode === "original" && field === "translation") {
        setSearchMode("all");
      } else if (searchMode === "translation" && field === "original") {
        setSearchMode("all");
      }
    } else {
      if (searchMode === "all") {
        setSearchMode(field === "original" ? "translation" : "original");
      } else if (searchMode === field) {
        setSearchMode("none");
      }
    }
  };

  const handleStringTypeCheckbox = (type: "regular" | "python", checked: boolean) => {
    setPage(0);
    if (checked) {
      if (stringTypeFilter === "none") {
        setStringTypeFilter(type);
      } else if (stringTypeFilter === "regular" && type === "python") {
        setStringTypeFilter("all");
      } else if (stringTypeFilter === "python" && type === "regular") {
        setStringTypeFilter("all");
      }
    } else {
      if (stringTypeFilter === "all") {
        setStringTypeFilter(type === "regular" ? "python" : "regular");
      } else if (stringTypeFilter === type) {
        setStringTypeFilter("none");
      }
    }
  };

  const handleOnlyLatinToggle = () => {
    setOnlyLatinInTranslation(prev => !prev);
    setPage(0);
  };

  const resetAllFilters = () => {
    setSearchMode("all");
    setStringTypeFilter("all");
    setOnlyLatinInTranslation(false);
    setFilterHiddenModPaths(new Set());
    setJustTranslatedIds(new Set()); // Clear pinned/highlighted translations
    setTranslationStatusFilter("all");
    setPage(0);
  };

  const getActiveFiltersCount = () => {
    let count = 0;
    if (searchMode !== "all") count++;
    if (stringTypeFilter !== "all") count++;
    if (onlyLatinInTranslation) count++;
    if (translationMode === "mods" && filterHiddenModPaths.size > 0) count++;
    if (translationStatusFilter !== "all") count++;
    return count;
  };

  // Drag-select: mousedown on one checkbox, drag to select/deselect a range
  // lastClickedIdRef: anchor for Shift+click range selection (updated on every plain click).
  const lastClickedIdRef = useRef<string | null>(null);

  const onDragSelectStart = (id: string, isCurrentlySelected: boolean) => {
    lastClickedIdRef.current = id; // update anchor on plain click
    dragRef.current = { active: true, selecting: !isCurrentlySelected, ids: [id] };
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (isCurrentlySelected) next.delete(id); else next.add(id);
      return next;
    });
  };

  const onDragSelectEnter = (id: string) => {
    if (!dragRef.current?.active) return;
    dragRef.current.ids.push(id);
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (dragRef.current!.selecting) next.add(id); else next.delete(id);
      return next;
    });
  };

  // Shift+click: select/deselect everything between lastClickedIdRef and `id`
  // using the current filteredStrings order (all pages, not just current page).
  const onShiftSelectRange = (id: string) => {
    const anchor = lastClickedIdRef.current;
    // Update anchor so chaining Shift+clicks extends from the latest click
    lastClickedIdRef.current = id;
    if (!anchor || anchor === id) {
      // No anchor yet — treat as a plain toggle
      setSelectedIds((prev) => {
        const next = new Set(prev);
        if (next.has(id)) next.delete(id); else next.add(id);
        return next;
      });
      return;
    }
    // Find the two endpoints in the filtered list
    const ids = filteredStrings.map((s) => s.id);
    const anchorIdx = ids.indexOf(anchor);
    const targetIdx = ids.indexOf(id);
    if (anchorIdx === -1 || targetIdx === -1) return;
    const lo = Math.min(anchorIdx, targetIdx);
    const hi = Math.max(anchorIdx, targetIdx);
    const rangeIds = ids.slice(lo, hi + 1);
    // Determine intent: if the anchor is currently selected, we select the range;
    // if it's deselected, we deselect the range.
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const shouldSelect = next.has(anchor);
      rangeIds.forEach((rid) => {
        if (shouldSelect) next.add(rid); else next.delete(rid);
      });
      return next;
    });
  };

  const selectAllVisible = (visibleStrings: TranslationString[]) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      const allSelected = visibleStrings.every((s) => next.has(s.id));
      if (allSelected) {
        visibleStrings.forEach((s) => next.delete(s.id));
      } else {
        visibleStrings.forEach((s) => next.add(s.id));
      }
      return next;
    });
  };
  // The table container — used to scroll its headers to the top of the viewport
  // when the user navigates pages (the whole document scrolls, so a fresh page
  // would otherwise stay parked at the old scroll offset, mid-table).
  const tableWrapRef = useRef<HTMLDivElement | null>(null);
  // Jump to a page AND bring the table headers back into view.
  const goToPage = (p: number) => {
    setPage(p);
    tableWrapRef.current?.scrollIntoView({ block: "start" });
  };
  // Inline-edit state: id of the cell currently open, and its live value.
  // null = nothing is being edited.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingVal, setEditingVal] = useState("");
  // true when the user opened the cell with Shift+click → save applies to all duplicates
  const [isBulkEdit, setIsBulkEdit] = useState(false);
  // Toast notification (e.g. "Применено к N ячейкам")
  const [toast, setToast] = useState<string | null>(null);
  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  function showToast(msg: string) {
    setToast(msg);
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current);
    toastTimerRef.current = setTimeout(() => setToast(null), 3000);
  }
  // Modal for Shift+Переперевести: lets the user type a one-time prompt rule.
  const [extraInstructionModal, setExtraInstructionModal] = useState<{ ids: string[] } | null>(null);
  const [extraInstructionText, setExtraInstructionText] = useState("");

  // Stable wrappers to prevent TableRow from re-rendering due to changing callbacks
  const handlersRef = useRef({
    startEdit,
    commitEdit,
    setEditingVal,
    setEditingId,
    onDragSelectStart,
    onDragSelectEnter,
    onShiftSelectRange
  });
  handlersRef.current = {
    startEdit,
    commitEdit,
    setEditingVal,
    setEditingId,
    onDragSelectStart,
    onDragSelectEnter,
    onShiftSelectRange
  };

  const stableStartEdit = useCallback((id: string, current: string, bulk?: boolean) => {
    handlersRef.current.startEdit(id, current, bulk);
  }, []);
  const stableCommitEdit = useCallback((id: string, original: string) => {
    handlersRef.current.commitEdit(id, original);
  }, []);
  const stableSetEditingVal = useCallback((val: string) => {
    handlersRef.current.setEditingVal(val);
  }, []);
  const stableSetEditingId = useCallback((id: string | null) => {
    handlersRef.current.setEditingId(id);
  }, []);
  const stableOnDragSelectStart = useCallback((id: string, isSelected: boolean) => {
    handlersRef.current.onDragSelectStart(id, isSelected);
  }, []);
  const stableOnDragSelectEnter = useCallback((id: string) => {
    handlersRef.current.onDragSelectEnter(id);
  }, []);
  const stableOnShiftSelectRange = useCallback((id: string) => {
    handlersRef.current.onShiftSelectRange(id);
  }, []);
  // Live translation progress (strings done / total), null when not translating.
  const [progress, setProgress] = useState<TranslateProgress | null>(null);
  const [keyStatuses, setKeyStatuses] = useState<Record<number, string>>({});
  // Models the chosen backend can serve, and which one it has loaded right now.
  // Populated from the sidecar so the user picks instead of typing the name.
  const [models, setModels] = useState<string[]>([]);
  const [activeModel, setActiveModel] = useState("");
  const [modelsLoading, setModelsLoading] = useState(false);
  const [isPaused, setIsPaused] = useState(false);
  const isFullyPaused = useMemo(() => {
    if (!isPaused) return false;
    const phases = Object.values(workerPhases);
    if (phases.length === 0) return false;
    return phases.every((p) => p === "paused");
  }, [isPaused, workerPhases]);
  const [abortController, _setAbortController] = useState<AbortController | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);
  const setAbortController = (controller: AbortController | null) => {
    _setAbortController(controller);
    abortControllerRef.current = controller;
  };
  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false);
  // Themed in-app folder browser: which picker is open (null = closed). The kind
  // decides whether the chosen path feeds the game or the mods flow.
  const [folderPickerKind, setFolderPickerKind] = useState<null | "game" | "mods">(null);
  const [translationMode, setTranslationMode] = useState<"game" | "mods">(() => loadSetting("lastFolderMode", "game") as "game" | "mods");
  const [saveLastGameFolder, setSaveLastGameFolder] = useState<boolean>(() => loadSetting("saveLastGameFolder", "true") === "true");
  const [saveLastModsFolder, setSaveLastModsFolder] = useState<boolean>(() => loadSetting("saveLastModsFolder", "true") === "true");
  const [modsDir, setModsDir] = useState<string | null>(null);
  const [gameRoot, setGameRoot] = useState<string | null>(null);
  const [detectedMods, setDetectedMods] = useState<ModInfo[]>([]);
  const [selectedModPaths, setSelectedModPaths] = useState<string[]>([]);
  const [showProxyPanel, setShowProxyPanel] = useState(false);
  const [proxyInfoOpen, setProxyInfoOpen] = useState(false);
  // The proxy URL is stored GLOBALLY (not per-provider): after an autocheck a
  // provider that works direct has its base_url cleared, so the URL itself must
  // live somewhere that survives — else it'd be lost on the next open.
  const [proxyUrlDraft, setProxyUrlDraft] = useState(() => loadSetting("proxyUrl", ""));
  const [proxyChecking, setProxyChecking] = useState(false);
  const [proxyCheckResults, setProxyCheckResults] =
    useState<Record<string, ProxyAutocheckResult> | null>(null);
  const changeTranslationMode = (mode: "game" | "mods") => {
    if (busy) return;
    setTranslationMode(mode);
    saveSetting("lastFolderMode", mode);
    
    // Clear all current states first
    setRoot(null);
    setModsDir(null);
    setGameRoot(null);
    setEngine(null);
    setStrings([]);
    setProject(null);
    setDetectedMods([]);
    setSelectedModPaths([]);
    setHasBackup(false);
    setPage(0);
    setError(null);
  };

  const [pythonLogs, setPythonLogs] = useState<string[]>([]);
  const [pythonTranslating, setPythonTranslating] = useState(false);
  const [pythonLogsOpen, setPythonLogsOpen] = useState(false);
  const [pyLogsMouseDown, setPyLogsMouseDown] = useState(false);
  const updateBusyRef = useRef(false);
  // Stable identity (useCallback, no deps) so UpdateOverlay's effect — which
  // depends on this via `run` — doesn't re-fire on every unrelated App
  // re-render. The actual duplicate-download bug is now guarded in
  // UpdateOverlay itself, but keeping this stable avoids the pointless
  // clear/reschedule churn too.
  const setUpdateBusyTracked = useCallback((v: boolean) => {
    updateBusyRef.current = v;
  }, []);
  // Python-string progress. `stage` distinguishes the two passes (classify =
  // deciding what to translate, translate = the actual work) so the bar resetting
  // to 0 between them reads as a NEW stage, not a regress. Each stage's total is
  // exact (candidate count, then confirmed-string count), so no estimate flag is
  // needed — the stage label tells the user what the number means.
  const [pyProgress, setPyProgress] = useState<
    { done: number; total: number; stage: "classify" | "translate" } | null
  >(null);
  // Streaming inject progress: which mod is being written + count.
  const [injectProgress, setInjectProgress] = useState<
    { doneMod: number; totalMods: number; modName: string } | null
  >(null);
  // Track mods edited during the current session
  const [editedModPaths, setEditedModPaths] = useState<Set<string>>(new Set());

  const providerInfo = PROVIDERS.find((p) => p.id === provider)!;
  // How many API keys feed the worker pool. Total workers = threads * keyCount
  // (mirrors the sidecar). At least 1 so the math never zeroes out.
  const keyCount = Math.max(1, nonEmptyKeys.length);

  // Mutate the key list + persist (per provider).
  function updateKeys(next: string[]) {
    const arr = next.length ? next : [""];
    setApiKeys(arr);
    saveProviderKeys(provider, arr);
  }

  // Clear recently translated highlight when changing active directory/engine
  useEffect(() => {
    setJustTranslatedIds(new Set());
    setEditedModPaths(new Set());
  }, [root, modsDir, translationMode]);

  useEffect(() => {
    ping().then((up) => {
      setSidecarUp(up);
      if (up) {
        // Re-check the proxy on startup if a URL is already saved: provider
        // reachability (geo-blocks, proxy uptime) can change between sessions, so
        // re-decide direct-vs-proxy silently and re-apply per provider.
        const savedProxy = loadSetting("proxyUrl", "");
        if (savedProxy) void runProxyAutocheck(savedProxy, true);
      }
    });
  }, []);

  useEffect(() => {
    const up = () => setPyLogsMouseDown(false);
    window.addEventListener("mouseup", up);
    return () => window.removeEventListener("mouseup", up);
  }, []);

  // Guard F5 / Ctrl+R in dev mode while a save is still in flight.
  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (isProjectSaving()) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
    };
  }, []);

  // Wait for any in-flight disk write before the window closes.
  useEffect(() => {
    let unlistenFn: (() => void) | null = null;
    let isClosingInProgress = false;

    import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
      const w = getCurrentWindow();
      w.onCloseRequested((event) => {
        event.preventDefault();
        if (isClosingInProgress) return;
        isClosingInProgress = true;

        // Block close during auto-update download.
        if (updateBusyRef.current) {
          isClosingInProgress = false;
          return;
        }

        // If a translation is running, abort it immediately.
        if (abortControllerRef.current) {
          abortControllerRef.current.abort();
        }

        // Wait for saves to finish, but limit to 3 seconds failsafe
        if (isProjectSaving()) {
          let attempts = 0;
          const interval = setInterval(() => {
            attempts++;
            if (!isProjectSaving() || attempts >= 150) { // 150 * 20ms = 3000ms
              clearInterval(interval);
              resetSavingState();
              w.destroy();
            }
          }, 20);
        } else {
          w.destroy();
        }
      }).then((unlisten) => {
        unlistenFn = unlisten;
      });
    });

    return () => {
      if (unlistenFn) unlistenFn();
    };
  }, []);

  // Dynamically update Tauri window title to match active engine/project.
  useEffect(() => {
    import("@tauri-apps/api/window").then(({ getCurrentWindow }) => {
      const w = getCurrentWindow();
      let title = "Interprex";
      if (engine) {
        const engineNameMap: Record<string, string> = {
          rpgmaker: "RPG Maker",
          renpy: "Ren'Py",
          godot: "Godot",
          gamemaker: "GameMaker",
          unity: "Unity",
          unreal: "Unreal Engine",
          unreal4_5: translationMode === "mods" ? "Satisfactory Mods" : "Unreal Engine 4/5",
          csharp: "C# / Unity",
          fusion: "Clickteam Fusion",
          mmf2: "Multimedia Fusion 2",
          qsp: "QSP",
          twine: "Twine",
          i18n: "I18n",
        };
        const mapped = engineNameMap[engine] || engine;
        title += ` - ${mapped}`;
      }
      w.setTitle(title).catch(console.error);
    });
  }, [engine, translationMode]);

  // Discover the backend's models whenever the connection details change.
  // Debounced so typing a base URL / API key doesn't hammer the sidecar. The
  // call never throws (sidecar returns empty on a down server); an empty list
  // just leaves the UI on its free-text fallback.
  useEffect(() => {
    let cancelled = false;
    // ~1s after the last keystroke (so it validates the key once you've stopped
    // typing, not on every character). The loading flag flips INSIDE the timer,
    // so "finding models…" never flickers while you're still typing the key.
    const handle = setTimeout(async () => {
      setModelsLoading(true);
      const { models: found, active } = await listModels({
        provider,
        base_url: baseUrl,
        api_key: primaryKey,
        free_only: provider === "openrouter" ? freeOnly : false,
      }).catch(() => ({ models: [] as string[], active: "" }));
      if (cancelled) return;
      setModels(found);
      setActiveModel(active);
      setModelsLoading(false);
      // Auto-pick a model so a local user usually never has to choose: prefer
      // the loaded (active) one, else the first discovered. Only when the user
      // hasn't already settled on a model the backend still offers.
      setModel((cur) => {
        if (cur && found.includes(cur)) return cur;
        const pick = active || found[0] || "";
        if (pick !== cur) saveProviderSetting("providerModel", provider, pick);
        return pick;
      });
    }, 1000);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [provider, baseUrl, primaryKey, freeOnly]);

  // OpenRouter daily free-request budget. The API gives the cap (50 free tier /
  // 1000 once ≥$10 was bought) but NOT how many we've spent today, so we count
  // locally — incremented per request that reached the server (errors included,
  // since a 429/503 still burns the daily quota), reset on UTC date rollover.
  useEffect(() => {
    if (provider !== "openrouter" || !primaryKey) {
      setOrUsage(null);
      return;
    }
    let cancelled = false;
    const handle = setTimeout(async () => {
      const lim = await keyLimits({ provider, base_url: baseUrl, api_key: primaryKey }).catch(
        () => ({}) as Awaited<ReturnType<typeof keyLimits>>,
      );
      if (cancelled) return;
      const cap = lim.daily_cap;
      if (!cap) {
        setOrUsage(null);
        return;
      }
      setOrUsage({ used: readOrUsageCount(), cap });
    }, 1000);
    return () => {
      cancelled = true;
      clearTimeout(handle);
    };
  }, [provider, baseUrl, primaryKey]);

  function fail(e: unknown) {
    setError(e instanceof Error ? e.message : String(e));
    setPhase("idle");
    setProgress(null);
    setInjectProgress(null);
    setKeyStatuses({});
    setWorkerPhases({});
    setIsPaused(false);
  }

  /** Open a cell for inline editing. No-op while a bulk operation is running. */
  function startEdit(id: string, current: string, bulk?: boolean) {
    if (busy && !isPaused) return;
    if (closeTimeoutRef.current) {
      clearTimeout(closeTimeoutRef.current);
      closeTimeoutRef.current = null;
    }
    setEditingId(id);
    setEditingVal(current);
    setIsBulkEdit(bulk ?? false);
  }

  /**
   * Commit the current inline edit: write the new value into the project and
   * save to disk. Called on textarea blur (including when the user clicks away
   * to a different cell or a pagination button).
   * In bulk mode (Shift+click), applies the translation to ALL strings with
   * the same original text and shows a toast with the count.
   */
  function commitEdit(id: string, original: string) {
    // Close the editor with a minor delay. If the user clicked another cell,
    // startEdit will run instantly and cancel this timeout, preventing a flash of null editingId.
    if (closeTimeoutRef.current) clearTimeout(closeTimeoutRef.current);
    closeTimeoutRef.current = setTimeout(() => {
      setEditingId((prev) => (prev === id ? null : prev));
      closeTimeoutRef.current = null;
    }, 50);

    const wasBulk = isBulkEdit;
    setIsBulkEdit(false);
    if (!project) return;
    const entry = project.strings[id];

    if (wasBulk) {
      // Bulk mode: apply editingVal to every string that shares this original text
      const matches = strings.filter((s) => s.original === original);
      const updatedStrings = { ...project.strings };
      const newJustTranslated = new Set(justTranslatedIds);

      for (const strObj of matches) {
        const prev = updatedStrings[strObj.id];
        const prevTrans = prev?.translated ?? "";
        updatedStrings[strObj.id] = {
          original: strObj.original,
          translated: editingVal,
          approved: prev?.approved ?? false,
        };
        if (editingVal.trim() !== "") {
          newJustTranslated.add(strObj.id);
        } else {
          newJustTranslated.delete(strObj.id);
        }
        if (editingVal !== prevTrans && translationMode === "mods") {
          const mPath = getModPathForString(strObj, detectedMods, gameRoot, modsDir);
          if (mPath) {
            setEditedModPaths((prev) => {
              const next = new Set(prev);
              next.add(mPath);
              return next;
            });
          }
        }
      }

      const updated = { ...project, strings: updatedStrings };
      saveProject(updated).catch(fail);
      // Defer the heavy table re-render so the next cell opens without waiting
      startTransition(() => {
        setJustTranslatedIds(newJustTranslated);
        setProject(updated);
      });
      if (matches.length > 1) {
        showToast(`Применено к ${matches.length} ячейкам`);
      }
      return;
    }

    // Single-cell mode: compute updated project synchronously (cheap),
    // but defer the state update so the next cell opens immediately.
    const prevVal = entry?.translated ?? "";
    const updated: ProjectFile = {
      ...project,
      strings: {
        ...project.strings,
        [id]: {
          original,
          translated: editingVal,
          approved: entry?.approved ?? false,
        },
      },
    };
    saveProject(updated).catch(fail);

    startTransition(() => {
      // Heavy updates: re-rendering the table row. Deferred so clicking the
      // next cell is instant — React prioritises startEdit over this render.
      if (editingVal.trim() !== "") {
        setJustTranslatedIds((prev) => {
          const next = new Set(prev);
          next.add(id);
          return next;
        });
      } else {
        setJustTranslatedIds((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
      }

      if (editingVal !== prevVal) {
        const strObj = strings.find((s) => s.id === id);
        if (strObj && translationMode === "mods") {
          const mPath = getModPathForString(strObj, detectedMods, gameRoot, modsDir);
          if (mPath) {
            setEditedModPaths((prev) => {
              const next = new Set(prev);
              next.add(mPath);
              return next;
            });
          }
        }
      }

      setProject(updated);
    });
  }

  async function scanSelectedMods(paths: string[], currentMods?: ModInfo[], currentDir?: string) {
    const activeDir = (currentDir || gameRoot) ?? modsDir;
    const activeMods = currentMods || detectedMods;
    if (!activeDir) return;
    if (paths.length === 0) {
      setStrings([]);
      setExtractedModPaths(new Set());
      setProject(null);
      setEngine(null);
      setFilterHiddenModPaths(new Set());
      return;
    }

    // Identify the engine
    const selectedModsInfo = activeMods.filter((m) => paths.includes(m.path));
    const engines = Array.from(new Set(selectedModsInfo.map((m) => m.engine).filter(Boolean)));
    if (engines.length === 0) {
      setError(t("errNoEngine") as string);
      setEngine(null);
      setStrings([]);
      setProject(null);
      return;
    }
    if (engines.length > 1) {
      setError(t("errMixedEngines") as string);
      setEngine(null);
      setStrings([]);
      setProject(null);
      return;
    }

    const detected = engines[0] as Engine;
    setEngine(detected);
    setError(null);

    try {
      setPhase("extracting");
      // Stream extraction per mod so each mod's counter lights up as it finishes,
      // instead of all of them waiting for the whole run. Strings accumulate
      // incrementally; the project merge happens once on the final full array.
      setStrings([]);
      setExtractedModPaths(new Set());
      const { strings: extracted } = await extractStreaming(
        activeDir, detected, paths,
        (ev) => {
          setStrings((prev) => [...prev, ...ev.strings]);
          setExtractedModPaths((prev) => {
            const next = new Set(prev);
            next.add(ev.path);
            return next;
          });
        },
      );
      const proj = mergeStrings(await loadProject(activeDir, detected), extracted);
      // Replace the accumulated array with the authoritative final one (dedup-safe).
      setStrings(extracted);
      setProject(proj);
      const { has_backup } = await getBackupStatus(activeDir);
      setHasBackup(has_backup);
      setPhase("idle");
    } catch (e) {
      fail(e);
    }
  }

  async function pickModsFolder(picked: string) {
    setError(null);
    setEditingId(null);
    if (typeof picked !== "string" || !picked) return;

    setStrings([]);
    setProject(null);
    setEngine(null);
    setPage(0);
    setDetectedMods([]);
    setSelectedModPaths([]);
    setFilterHiddenModPaths(new Set());

    try {
      setPhase("detecting");
      const res = await detectMods(picked, target);
      setModsDir(res.mods_dir);
      setGameRoot(res.game_root);
      const sortedMods = [...res.mods].sort((a, b) => {
        const aCount = a.total_count ?? 0;
        const bCount = b.total_count ?? 0;
        if (aCount === 0 && bCount > 0) return 1;
        if (bCount === 0 && aCount > 0) return -1;
        if (aCount > 0 && bCount > 0) {
          if (bCount !== aCount) {
            return bCount - aCount;
          }
        }
        return a.name.localeCompare(b.name);
      });
      setDetectedMods(sortedMods);
      
      if (sortedMods.length === 0) {
        setPhase("idle");
        setError(t("noModsDetected") as string);
        return;
      }
      saveSetting("lastFolderMode", "mods");
      saveSetting("lastFolder", picked);
      if (saveLastModsFolder) {
        saveSetting("lastModsFolder", picked);
      } else {
        saveSetting("lastModsFolder", "");
      }

      // select only mods that have strings by default
      const defaultPaths = sortedMods
        .filter(m => m.total_count !== undefined && m.total_count > 0)
        .map(m => m.path);
      setSelectedModPaths(defaultPaths);

      // Check engines
      const engines = Array.from(new Set(sortedMods.map(m => m.engine).filter(Boolean)));
      if (engines.length > 1) {
        setPhase("idle");
        setError(t("errMixedEngines") as string);
        return;
      }

      await scanSelectedMods(defaultPaths, sortedMods, res.game_root);
    } catch (e) {
      fail(e);
    }
  }

  async function handleToggleMod(path: string) {
    if (busy && !isPaused) return;
    const isSelected = selectedModPaths.includes(path);
    const nextPaths = isSelected
      ? selectedModPaths.filter((p) => p !== path)
      : [...selectedModPaths, path];
    setSelectedModPaths(nextPaths);
  }

  async function handleSelectAllMods() {
    if (busy && !isPaused) return;
    const allPaths = detectedMods
      .filter((m) => m.total_count !== undefined && m.total_count > 0)
      .map((m) => m.path);
    setSelectedModPaths(allPaths);
  }

  async function handleDeselectAllMods() {
    if (busy && !isPaused) return;
    setSelectedModPaths([]);
  }

  async function pickFolder(picked: string) {
    setError(null);
    setEditingId(null);
    if (typeof picked !== "string" || !picked) return;
    setRoot(picked);
    setStrings([]);
    setProject(null);
    setEngine(null);
    setPage(0);
    try {
      setPhase("detecting");
      const { engine: detected } = await detectEngine(picked);
      if (!detected) {
        setPhase("idle");
        setError(t("errNoEngine"));
        return;
      }
      saveSetting("lastFolderMode", "game");
      saveSetting("lastFolder", picked);
      if (saveLastGameFolder) {
        saveSetting("lastGameFolder", picked);
      } else {
        saveSetting("lastGameFolder", "");
      }
      setEngine(detected);
      setPhase("extracting");
      const { strings: extracted } = await extractStrings(picked, detected);
      const proj = mergeStrings(await loadProject(picked, detected), extracted);
      setStrings(extracted);
      setProject(proj);
      const { has_backup } = await getBackupStatus(picked);
      setHasBackup(has_backup);
      setLintResult(null);
      setPhase("idle");
    } catch (e) {
      fail(e);
    }
  }

  async function togglePause() {
    try {
      if (isPaused) {
        const settingsChanged = runSettingsRef.current && (
          runSettingsRef.current.provider !== provider ||
          runSettingsRef.current.model !== model ||
          runSettingsRef.current.threads !== threads ||
          runSettingsRef.current.baseUrl !== baseUrl ||
          JSON.stringify(runSettingsRef.current.apiKeys) !== JSON.stringify(apiKeys)
        );
        if (settingsChanged) {
          shouldRestartRef.current = true;
          if (abortController) {
            abortController.abort();
            setAbortController(null);
          }
          setIsPaused(false);
        } else {
          await resumeTranslation();
          setIsPaused(false);
        }
      } else {
        await pauseTranslation();
        setIsPaused(true);
      }
    } catch (e) {
      console.error("Failed to toggle pause:", e);
    }
  }


  async function translateAll(targetIds?: string[], extraInstruction?: string): Promise<{ ok: boolean; project?: ProjectFile }> {
    if (!project || !engine) return { ok: false };
    runSettingsRef.current = {
      provider,
      model,
      threads,
      baseUrl,
      apiKeys: [...apiKeys],
    };
    shouldRestartRef.current = false;
    let ok = true;
    let currentProject = project;
    try {
      setPhase("translating");
      setIsPaused(false);
      setPyProgress(null);

      // In mods mode `strings` holds EVERY extracted mod, but the user may have
      // unchecked some — translate (and size the progress bar to) ONLY the
      // selected mods. Same mod-path resolver the table filter uses. Outside
      // mods mode there's no selection, so the scope is all strings.
      // EXCEPTION: when the user explicitly selected specific cells (targetIds),
      // respect that selection unconditionally — don't silently drop cells that
      // belong to a mod whose checkbox happens to be unchecked.
      const selectedSet = new Set(selectedModPaths);
      const inScope = (s: TranslationString): boolean => {
        if (targetIds) return true; // explicit selection — always in scope
        if (translationMode !== "mods") return true;
        const mPath = getModPathForString(s, detectedMods, gameRoot, modsDir);
        return mPath !== null && selectedSet.has(mPath);
      };
      const scopedStrings = strings.filter(inScope);

      const todo = scopedStrings.filter((s) => {
        const entry = project.strings[s.id];
        if (targetIds) {
          return targetIds.includes(s.id);
        }
        // entry absent = never translated; entry present but translated empty = also needs translation.
        // approved strings are always skipped regardless.
        return !(entry?.approved) && !(entry?.translated);
      });

      // Record translating IDs so they are pinned and highlighted
      if (targetIds) {
        setJustTranslatedIds(new Set(todo.map((s) => s.id)));
      } else {
        setJustTranslatedIds(new Set());
      }

      // Calculate unique representatives matching the backend de-duplication,
      // scoped to the selected mods so the progress total matches the table.
      const uniqueKeys = new Set(scopedStrings.map((s) => `${s.original}\x00${s.context}`));
      const totalUnique = uniqueKeys.size;

      // initialDone must count only FULLY-done unique keys (no untranslated id),
      // i.e. keys NOT present in `todo`. The scheduler dedups `todo` the same way
      // and reports p.done = reps it translated this run, so done = initialDone +
      // p.done lands exactly on totalUnique. Counting keys with merely ≥1
      // translated id would double-count a mixed key (one id done, one pending):
      // it'd sit in initialDone AND get re-counted when the scheduler translates
      // its rep — the cause of the count overshooting total then snapping back.
      const todoKeys = new Set(todo.map((s) => `${s.original}\x00${s.context}`));
      const initialDone = totalUnique - todoKeys.size;

      setProgress({
        done: initialDone,
        total: totalUnique,
        batches: 0,
        translations: {},
        status: "Initializing...",
      });
      setKeyStatuses({ 0: "Initializing..." });
      setWorkerPhases({ 0: "initializing" });

      // Threads/pacing apply to cloud backends only; local servers have one model
      // in VRAM, so parallel requests there just contend. Send 1 / 0 for them.
      // The pacing delay is derived from the user's RPM cap and the thread count.
      const effThreads = providerInfo.needsKey ? threads : 1;
      const effDelay = providerInfo.needsKey ? rpmToDelay(rpmLimit, effThreads) : 0;
      // Snapshot today's OpenRouter usage so the run's absolute request count adds
      // onto it (and resets cleanly if midnight UTC passed since the last run).
      orUsageBaseRef.current = readOrUsageCount();

      const controller = new AbortController();
      setAbortController(controller);

      try {
        const result = await translateBatch(
          todo,
          {
            provider,
            targetLang: target,
            glossary: project?.glossary ?? {},
            // Read base_url fresh: a just-run autocheck may have updated it this
            // same tick, after the `baseUrl` state closure was captured.
            config: { baseUrl: loadProviderSetting("providerBaseUrl", provider, ""), apiKeys: nonEmptyKeys, model },
            maxContextTokens: 0,
            maxBatchSize: maxBatchSize,
            threads: effThreads,
            delaySeconds: effDelay,
            root: activeRoot ?? undefined,
            fontStyle,
            extraInstruction: extraInstruction ?? "",
          },
          (p) => {
            setProgress({
              ...p,
              done: initialDone + p.done,
              total: totalUnique,
            });
            const wi = p.worker_idx ?? p.key_idx;
            if (wi !== undefined) {
              setKeyStatuses((prev) => ({
                ...prev,
                [wi]: getProgressStatusText(p),
              }));
              if (p.phase) {
                setWorkerPhases((prev) => ({ ...prev, [wi]: p.phase! }));
              }
            } else if (p.status) {
              setKeyStatuses({ 0: p.status });
            }
            // OpenRouter daily budget: today's total = pre-run baseline + this
            // run's server-side request count (absolute, so no double counting).
            if (provider === "openrouter" && p.requests_sent !== undefined) {
              const used = writeOrUsageCount(orUsageBaseRef.current + p.requests_sent);
              setOrUsage((prev) => (prev ? { ...prev, used } : prev));
            }
            // Fill rows live: merge each batch's translations into the project as
            // they land, so the table updates as the model works instead of all at
            // the end.
            if (Object.keys(p.translations).length) {
              currentProject = mergeTranslations(currentProject, p.translations, strings);
              setProject(currentProject);
              if (translationMode === "mods" && targetIds) {
                setEditedModPaths((prev) => {
                  const next = new Set(prev);
                  let changed = false;
                  for (const id of Object.keys(p.translations)) {
                    const strObj = strings.find((s) => s.id === id);
                    if (strObj) {
                      const mPath = getModPathForString(strObj, detectedMods, gameRoot, modsDir);
                      if (mPath && !next.has(mPath)) {
                        next.add(mPath);
                        changed = true;
                      }
                    }
                  }
                  return changed ? next : prev;
                });
              }
              saveProject(currentProject).catch((e) => {
                console.error("Auto-save batch failed:", e);
                setError(`Auto-save batch failed: ${e instanceof Error ? e.message : String(e)}`);
              });
            }
          },
          controller.signal,
        );
        setProgress(null);
        setKeyStatuses({});
        setWorkerPhases({});
        setIsPaused(false);
        // Authoritative final merge: built on top of whatever live-merges already
        // applied during the run, not the stale `project` captured at call time.
        currentProject = mergeTranslations(currentProject, result.translations, strings);
        // Carry measured font-shrink factors to the writeBack/inject step.
        sizeFixesRef.current = result.sizeFixes ?? {};
        setProject(currentProject);
        if (translationMode === "mods" && targetIds) {
          setEditedModPaths((prev) => {
            const next = new Set(prev);
            let changed = false;
            for (const id of Object.keys(result.translations)) {
              const strObj = strings.find((s) => s.id === id);
              if (strObj) {
                const mPath = getModPathForString(strObj, detectedMods, gameRoot, modsDir);
                if (mPath && !next.has(mPath)) {
                  next.add(mPath);
                  changed = true;
                }
              }
            }
            return changed ? next : prev;
          });
        }
        setPhase("saving");
        await saveProject(currentProject);
        setPhase("idle");
        if (result.aborted) {
          ok = false;
          const done = Object.keys(result.translations).length;
          const details = result.errors.length
            ? "\n\n" + result.errors.map((err) => `• ${err}`).join("\n")
            : "";
          setError((t("translateAborted")(done, todo.length) as string) + details);
        } else if (result.errors.length) {
          const details = result.errors.map((err) => `• ${err}`).join("\n");
          setError(`${t("translateErrors")(result.errors.length) as string}\n\n${details}`);
        } else {
          const uniqueCount = Object.keys(result.translations).length;
          if (uniqueCount > 0) {
            const totalCount = targetIds ? targetIds.length : strings.length;
            showToast((t("translateSuccess") as any)(uniqueCount, totalCount));
          }
        }
      } finally {
        setAbortController(null);
      }
    } catch (e) {
      ok = false;
      if (e instanceof Error && e.name === "AbortError" || (e instanceof DOMException && e.name === "AbortError")) {
        setPhase((curr) => curr === "injecting" ? "injecting" : "idle");
        setProgress(null);
        setInjectProgress(null);
        setKeyStatuses({});
        setWorkerPhases({});
        setIsPaused(false);
      } else {
        fail(e);
      }
    }
    if (shouldRestartRef.current) {
      shouldRestartRef.current = false;
      setTimeout(() => {
        translateAll(targetIds);
      }, 50);
    }
    return { ok, project: ok ? currentProject : undefined };
  }

  async function writeBack(proj?: ProjectFile, forceModPaths?: string[]): Promise<boolean> {
    const p = proj ?? project;
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    if (!p || !engine || !activeRoot) return false;
    if (abortController) {
      abortController.abort();
      setAbortController(null);
    }
    if (isPaused) {
      await resumeTranslation().catch(() => {});
      setIsPaused(false);
    }

    try {
      // 1. Identify which files will be modified
      const filesToBackup = new Set<string>();
      for (const s of strings) {
        const entry = p.strings[s.id];
        if (entry?.translated) {
          if (forceModPaths) {
            const mPath = getModPathForString(s, detectedMods, gameRoot, modsDir);
            if (mPath && forceModPaths.includes(mPath)) {
              filesToBackup.add(s.file);
            }
          } else {
            filesToBackup.add(s.file);
          }
        }
      }

      // 2. Perform the backup phase
      setPhase("backing_up");
      await createBackup(activeRoot, Array.from(filesToBackup));

      // 3. Perform the injection phase (streaming: one event per mod)
      setPhase("injecting");
      const translations: Record<string, string> = {};
      for (const [id, entry] of Object.entries(p.strings)) {
        if (entry.translated) translations[id] = entry.translated;
      }
      const modsForInject = translationMode === "mods" ? (forceModPaths ?? selectedModPaths) : undefined;
      // Build a path→name map for showing human-readable mod names in progress
      const modNameMap = new Map(detectedMods.map((m) => [m.path, m.name]));
      const { written } = await injectStringsStream(
        activeRoot,
        engine,
        translations,
        target,
        modsForInject,
        fontStyle,
        sizeFixesRef.current,
        (ev) => {
          if (ev.totalMods > 1) {
            setInjectProgress({
              doneMod: ev.doneMods,
              totalMods: ev.totalMods,
              modName: modNameMap.get(ev.path) || ev.path.split("/").pop() || ev.path,
            });
          }
        },
        abortController?.signal,
      );
      // 4. Ren'Py inline-Python (blog, status, search history) — apply from the
      //    translation cache only, NO API. This makes "Write translation" lay
      //    down everything a prior full "Translate" produced, for free. Skipped
      //    in mods mode (it writes a separate pak, not the game's tl/ tree).
      if (engine === "renpy" && translationMode !== "mods") {
        try {
          await handleTranslatePython(false, /* applyCachedOnly */ true);
        } catch (e) {
          console.error("Inline-Python apply (cached) failed:", e);
        }
      }
      const { has_backup } = await getBackupStatus(activeRoot);
      setHasBackup(has_backup);
      setPhase("idle");
      setInjectProgress(null);
      setEditedModPaths(new Set());
      setError(translationMode === "mods" ? (t("wroteBackMods")(written) as string) : (t("wroteBack")(written) as string));
      return true;
    } catch (e) {
      getBackupStatus(activeRoot).then(({ has_backup }) => setHasBackup(has_backup)).catch(() => {});
      fail(e);
      return false;
    }
  }

  async function handleInjectEditedMods() {
    if (editedModPaths.size === 0) return;
    const originalSelected = [...selectedModPaths];
    const editedList = Array.from(editedModPaths);

    setSelectedModPaths(editedList);

    try {
      await writeBack(project ?? undefined, editedList);
    } catch (err) {
      console.error(err);
    } finally {
      setSelectedModPaths(originalSelected);
    }
  }

  // Autocheck after the user pastes a proxy URL: probe each cloud provider that
  // has a key, decide direct-vs-proxy, and APPLY it to that provider's saved
  // base_url. Direct wins where the provider is reachable without the proxy (no
  // geo-block) — proxy is only set where direct is blocked/unreachable. The probe
  // lists models only, so it costs no money and no model quota. The currently
  // selected provider's live baseUrl is updated too so the UI reflects it now.
  // Returns true if the check RESOLVED (network was up and we got a decisive
  // verdict for at least one provider) — the caller uses this to know whether a
  // later lazy re-check is still needed.
  async function runProxyAutocheck(proxyUrl: string, silent = false): Promise<boolean> {
    // Persist the URL globally so it survives even when a provider ends up direct
    // (its own base_url gets cleared). Empty = user removed the proxy.
    saveSetting("proxyUrl", proxyUrl);
    // Cloud providers the proxy can route (mirrors the backend probe specs).
    const cloud: ProviderId[] = ["gemini", "openrouter"];
    const probe: Record<string, string> = {};
    for (const p of cloud) {
      const keys = loadProviderKeys(p).filter(Boolean);
      probe[p] = keys[0] || ""; // probe works without a key too (returns "auth")
    }
    if (!silent) {
      setProxyChecking(true);
      setProxyCheckResults(null);
    }
    try {
      const { results } = await proxyAutocheck(proxyUrl, probe);
      // Apply only DECISIVE verdicts. "unknown" means the provider was reachable
      // neither directly nor via the proxy — typically NO INTERNET (e.g. offline
      // at startup). Don't touch the saved choice in that case, or we'd wipe a
      // needed proxy and silently break the next translate when the net returns.
      let resolved = false;
      for (const [p, r] of Object.entries(results)) {
        if (r.mode === "unknown") continue;
        resolved = true;
        const url = r.mode === "proxy" ? proxyUrl : "";
        saveProviderSetting("providerBaseUrl", p, url);
        if (p === provider) setBaseUrl(url); // reflect the active provider live
      }
      proxyResolvedRef.current = resolved;
      if (!silent) setProxyCheckResults(results);
      return resolved;
    } catch {
      if (!silent) setProxyCheckResults({}); // empty = "check failed", UI shows a hint
      return false;
    } finally {
      if (!silent) setProxyChecking(false);
    }
  }

  // Before a translate run, if a proxy URL is saved but the startup autocheck
  // never resolved (offline at launch), re-run it now — the net is likely up now
  // that the user is actually translating. Cheap (two GET model lists) and only
  // fires until it resolves once.
  async function ensureProxyResolved() {
    const savedProxy = loadSetting("proxyUrl", "");
    if (savedProxy && !proxyResolvedRef.current) {
      await runProxyAutocheck(savedProxy, true);
    }
  }

  async function handleTranslatePython(dryRun: boolean, applyCachedOnly = false) {
    if (!root) return;
    const effThreads = providerInfo.needsKey ? threads : 1;
    // Total workers = threads x keys, matching the backend pool and the main
    // /translate path. The worker-status grid is sized the same (threads*keyCount).
    const workerCount = Math.max(1, effThreads * keyCount);
    const effDelay = providerInfo.needsKey ? rpmToDelay(rpmLimit, effThreads) : 0;
    setPythonLogs([]);
    setPythonTranslating(true);
    setPythonLogsOpen(false);
    setPhase("translating");
    setPyProgress(null);
    let pyTotal = 0;
    let pyBatchTotal = 0;
    let nextBatch = 0;
    // Monotonic count of batches that have REACHED a terminal state this stage
    // (done or failed). Drives the bar — using the just-finished batch's INDEX
    // made the bar jump around, since parallel batches finish out of order.
    let pyDoneBatches = 0;
    let pyStage: "classify" | "translate" = "classify";
    const workerBatch: Record<number, number> = {};
    const initPhases: Record<number, string> = {};
    const initStatuses: Record<number, string> = {};
    for (let i = 0; i < workerCount; i++) {
      initPhases[i] = "initializing";
      initStatuses[i] = t("pyStatusWaiting") as string;
      workerBatch[i] = -1;
    }
    setWorkerPhases(initPhases);
    setKeyStatuses(initStatuses);
    try {
      setPythonLogs(["[System] Starting Ren'Py Python translator..."]);
      await translateRenpyPython({
        root,
        api_key: primaryKey,
        api_keys: nonEmptyKeys,
        model,
        base_url: loadProviderSetting("providerBaseUrl", provider, ""),
        provider,
        target_lang: target.toLowerCase(),
        dry_run: dryRun,
        threads: effThreads,
        delay_seconds: effDelay,
        apply_cached_only: applyCachedOnly,
      }, (line) => {
        setPythonLogs((prev) => [...prev, line]);
        const extractedMatch = line.match(/Extracted (\d+) string literal candidates/);
        if (extractedMatch) {
          pyTotal = Number(extractedMatch[1]);
          pyStage = "classify";
          setPyProgress({ done: 0, total: pyTotal, stage: "classify" });
          return;
        }
        const classifyStart = line.match(/Classifying (\d+) candidates.*in (\d+) parallel batches.*\(threads=(\d+)\)/);
        if (classifyStart) {
          pyTotal = Number(classifyStart[1]);
          pyBatchTotal = Number(classifyStart[2]);
          nextBatch = 0;
          pyDoneBatches = 0;
          pyStage = "classify";
          setPyProgress({ done: 0, total: pyTotal, stage: "classify" });
          // Only as many workers as there are batches can have work — the rest
          // rest immediately (5 batches across 18 workers = 13 resting). Activating
          // all of them and guessing a batch number each is what produced the
          // bogus "thread 18 — batch 18/5".
          const activeClassify = Math.min(workerCount, pyBatchTotal);
          for (let i = 0; i < workerCount; i++) {
            if (i < activeClassify) {
              workerBatch[i] = nextBatch++;
              setWorkerPhases((prev) => ({ ...prev, [i]: "translating_batch" }));
              setKeyStatuses((prev) => ({ ...prev, [i]: t("pyStatusClassifying") as string }));
            } else {
              workerBatch[i] = -1;
              setWorkerPhases((prev) => ({ ...prev, [i]: "resting" }));
              setKeyStatuses((prev) => ({ ...prev, [i]: t("statusResting") as string }));
            }
          }
          return;
        }
        const translateStart = line.match(/Translating (\d+) strings in (\d+) parallel batches/);
        if (translateStart) {
          pyTotal = Number(translateStart[1]);
          pyBatchTotal = Number(translateStart[2]);
          nextBatch = 0;
          pyDoneBatches = 0;
          pyStage = "translate";
          // Now we KNOW the real translate count (confirmed strings, exact).
          setPyProgress({ done: 0, total: pyTotal, stage: "translate" });
          // Same as classify: only min(workers, batches) can work; rest the others
          // so the grid never shows a worker on a batch that doesn't exist.
          const activeTranslate = Math.min(workerCount, pyBatchTotal);
          for (let i = 0; i < workerCount; i++) {
            if (i < activeTranslate) {
              workerBatch[i] = nextBatch++;
              setWorkerPhases((prev) => ({ ...prev, [i]: "translating_batch" }));
              setKeyStatuses((prev) => ({ ...prev, [i]: t("pyStatusBatchDone")(t("pyStatusTranslating") as string, String(workerBatch[i] + 1), String(pyBatchTotal)) as string }));
            } else {
              workerBatch[i] = -1;
              setWorkerPhases((prev) => ({ ...prev, [i]: "resting" }));
              setKeyStatuses((prev) => ({ ...prev, [i]: t("statusResting") as string }));
            }
          }
          return;
        }
        const batchThreadMatch = line.match(/(Classified|Translated) batch (\d+)\/(\d+) \[thread (\d+)\]/);
        if (batchThreadMatch) {
          const curBatch = Number(batchThreadMatch[2]);
          const totalBatches = Number(batchThreadMatch[3]);
          const tidx = Number(batchThreadMatch[4]);
          pyBatchTotal = totalBatches;
          // Advance by COMPLETED-batch count (monotonic), not the finished batch's
          // index — parallel batches finish out of order, so the index made the bar
          // jump backwards.
          pyDoneBatches++;
          const done = pyTotal > 0 ? Math.min(pyTotal, Math.floor(pyDoneBatches * pyTotal / totalBatches)) : 0;
          setPyProgress({ done, total: pyTotal, stage: pyStage });
          if (tidx < workerCount) {
            if (nextBatch < totalBatches) {
              // This worker has another batch to claim — keep it lit.
              workerBatch[tidx] = nextBatch++;
              const isClassify = batchThreadMatch[1] === "Classified";
              setWorkerPhases((prev) => ({ ...prev, [tidx]: isClassify ? "completed_batch" : "translating_batch" }));
              if (isClassify) {
                setKeyStatuses((prev) => ({ ...prev, [tidx]: t("pyStatusClassifying") as string }));
              } else {
                setKeyStatuses((prev) => ({ ...prev, [tidx]: t("pyStatusBatchDone")(t("pyStatusTranslating") as string, String(curBatch), String(totalBatches)) as string }));
              }
            } else {
              // No more NEW batches, but this worker may still finish a requeued
              // one. Show "completed" rather than "resting" — classifyDoneMatch
              // will transition everyone to resting once the phase truly ends.
              workerBatch[tidx] = -1;
              const isClassify = batchThreadMatch[1] === "Classified";
              setWorkerPhases((prev) => ({ ...prev, [tidx]: "completed_batch" }));
              if (isClassify) {
                setKeyStatuses((prev) => ({ ...prev, [tidx]: t("pyStatusClassified") as string }));
              } else {
                setKeyStatuses((prev) => ({ ...prev, [tidx]: t("pyStatusBatchDone")(t("pyTranslated") as string, String(curBatch), String(totalBatches)) as string }));
              }
            }
          }
          return;
        }
        const classifyDoneMatch = line.match(/Classify phase done: (\d+) translate/);
        if (classifyDoneMatch) {
          for (let i = 0; i < workerCount; i++) {
            workerBatch[i] = -1;
            setWorkerPhases((prev) => ({ ...prev, [i]: "resting" }));
            setKeyStatuses((prev) => ({ ...prev, [i]: t("statusResting") as string }));
          }
          return;
        }
        const batchLegacyMatch = line.match(/(Classified|Translated) batch (\d+)\/(\d+)/);
        if (batchLegacyMatch) {
          const totalBatches = Number(batchLegacyMatch[3]);
          pyBatchTotal = totalBatches;
          pyDoneBatches++;
          const done = pyTotal > 0 ? Math.min(pyTotal, Math.floor(pyDoneBatches * pyTotal / totalBatches)) : 0;
          setPyProgress({ done, total: pyTotal, stage: pyStage });
          const isClassify = batchLegacyMatch[1] === "Classified";
          if (isClassify) {
            setKeyStatuses((prev) => {
              const next = { ...prev };
              for (const k of Object.keys(next)) next[Number(k)] = t("pyStatusClassifying") as string;
              return next;
            });
            setWorkerPhases((prev) => {
              const next = { ...prev };
              for (const k of Object.keys(next)) next[Number(k)] = "completed_batch";
              return next;
            });
          } else {
            setKeyStatuses((prev) => {
              const next = { ...prev };
              for (const k of Object.keys(next)) next[Number(k)] = t("pyStatusBatchDone")(t("pyTranslated") as string, batchLegacyMatch[2], batchLegacyMatch[3]) as string;
              return next;
            });
          }
          return;
        }
        // Terminal per-batch failure (key dead with no survivor, or retries
        // exhausted). The pool logs "Failed batch N/M [thread T]" — mirror of the
        // success line — so the worker's card stops showing a frozen "translating
        // batch N" and switches to "error on batch N" instead.
        const failBatchMatch = line.match(/Failed batch (\d+)\/(\d+) \[thread (\d+)\]/);
        if (failBatchMatch) {
          const curBatch = Number(failBatchMatch[1]);
          const totalBatches = Number(failBatchMatch[2]);
          const tidx = Number(failBatchMatch[3]);
          // A failed batch is terminal too — advance the bar so it still reaches
          // 100% even when some batches give up.
          pyDoneBatches++;
          const done = pyTotal > 0 ? Math.min(pyTotal, Math.floor(pyDoneBatches * pyTotal / totalBatches)) : 0;
          setPyProgress({ done, total: pyTotal, stage: pyStage });
          if (tidx < workerCount) {
            // Show the error caption briefly; if no batch remains, the worker will
            // rest (it won't claim again). Keep the error tone so the user sees the
            // batch failed rather than a frozen "translating".
            if (nextBatch < pyBatchTotal) {
              workerBatch[tidx] = nextBatch++;
            } else {
              workerBatch[tidx] = -1;
            }
            setWorkerPhases((prev) => ({ ...prev, [tidx]: "error" }));
            setKeyStatuses((prev) => ({ ...prev, [tidx]: t("pyStatusBatchError")(curBatch) as string }));
          }
          return;
        }
        const failMatch = line.match(/[Bb]atch (classification|translation) thread (\d+) failed/);
        if (failMatch) {
          const idx = Number(failMatch[2]) - 1;
          const failPhase = failMatch[1] === "classification"
            ? (t("pyStatusClassifying") as string)
            : (t("pyStatusTranslating") as string);
          setWorkerPhases((prev) => ({ ...prev, [idx]: "error" }));
          setKeyStatuses((prev) => ({ ...prev, [idx]: t("pyStatusError")(failPhase) as string }));
          return;
        }
        const errLine = line.includes("[ERROR]");
        if (errLine) {
          setWorkerPhases((prev) => {
            const next = { ...prev };
            for (const k of Object.keys(next)) if (next[Number(k)] !== "error") next[Number(k)] = "translating_batch";
            return next;
          });
        }
      });
      if (pyTotal > 0) setPyProgress({ done: pyTotal, total: pyTotal, stage: pyStage });
      const status = await getBackupStatus(root);
      setHasBackup(status.has_backup);
    } catch (err: any) {
      setPythonLogs((prev) => [...prev, `[ERROR] ${err.message || err}`]);
    } finally {
      const donePhases: Record<number, string> = {};
      const doneStatuses: Record<number, string> = {};
      for (let i = 0; i < workerCount; i++) {
        donePhases[i] = "done";
        doneStatuses[i] = t("pyStatusFinished") as string;
      }
      setWorkerPhases(donePhases);
      setKeyStatuses(doneStatuses);
      setPythonTranslating(false);
      setPhase("idle");
    }
  }

  // One "Translate" button = the full pipeline, in order:
  //   1. translateAll  — LLM fills project.strings[*].translated (auto-saved)
  //   2. writeBack      — backup + inject -> game/tl/<lang>/*.rpy
  //   3. (renpy only) Python-block translation — edits source literals in place
  //   4. autofix        — validate the written output, repair broken literals
  // Steps share ONE .interprex_backups store, so the single Restore button undoes
  // everything. Each step short-circuits the chain if the user aborts or it fails;
  // autofix is best-effort (its own try/catch) and never blocks completion.
  async function handleTranslate(targetIds?: string[], extraInstruction?: string) {
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    if (!project || !engine || !activeRoot) return;

    // 0. If a proxy is saved but the startup check never resolved (offline at
    //    launch), re-decide direct-vs-proxy now — the net is up if we're here.
    //    Cheap and only runs until it resolves once.
    await ensureProxyResolved();

    // 1. Translate.
    const { ok: translated, project: updatedProject } = await translateAll(targetIds, extraInstruction);
    if (!translated) return;

    // 2. Write the tl/ files (must precede autofix — it has nothing to check
    //    until the output exists on disk). Pass the updated project from
    //    translateAll's local variable — the React state `project` is stale
    //    (setProject only schedules a re-render, doesn't update the closure).
    const wrote = await writeBack(updatedProject);
    if (!wrote) return;

    // 3. Ren'Py source-literal translation (engine-specific). Same backup store.
    if (engine === "renpy") {
      await handleTranslatePython(false);
    }

    // 4. Autofix — engine-agnostic safety net. Validates the written translation
    //    and repairs anything the translation broke (e.g. a malformed literal in
    //    a Ren'Py python: block). Best-effort: failures here don't fail the run.
    try {
      setPhase("autofixing");
      const res = await autofixTranslation(
        engine, activeRoot, primaryKey, model,
        loadProviderSetting("providerBaseUrl", provider, ""), target,
      );
      if (res.fixed > 0) {
        setError(t("autofixFixed")(res.fixed) as string);
      }
      if (res.log?.length) {
        setPythonLogs((prev) => [...prev, ...res.log.map((l) => `[Autofix] ${l}`)]);
      }
    } catch (e) {
      console.error("Autofix failed:", e);
    } finally {
      const { has_backup } = await getBackupStatus(activeRoot).catch(() => ({ has_backup: false }));
      setHasBackup(has_backup);
      setPhase("idle");
    }

    // 5. Engine-oracle lint (Ren'Py only, non-mods). The game's OWN engine
    //    validates our injected tl/ files and surfaces real hazards our static
    //    validators can't (e.g. a translated "100%" = an unterminated format
    //    code). Best-effort + slow (spawns the engine), so it runs last and never
    //    blocks the pipeline; absent on a machine without the bundled SDK.
    if (engine === "renpy" && translationMode !== "mods") {
      renpyLint(activeRoot, String(target))
        .then((r) => setLintResult(r.available ? r : null))
        .catch(() => setLintResult(null));
    }
  }

  async function handleRestoreBackup() {
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    if (!activeRoot) return;
    try {
      setPhase("restoring");
      setError(null);
      const res = await restoreBackup(activeRoot);
      if (res.success) {
        setHasBackup(false);
        setError(t("restoreSuccess") as string);
      } else {
        setError(res.message || "Failed to restore backup");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      getBackupStatus(activeRoot).then(({ has_backup }) => setHasBackup(has_backup)).catch(() => {});
      setPhase("idle");
    }
  }

  async function handleExportZip() {
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    if (!activeRoot || !engine) return;
    try {
      setError(null);
      const res = await exportTranslationZip(activeRoot, engine, target);
      if (res.success) {
        setError(t("exportZipSuccess")(res.zip_name) as string);
      } else {
        setError(t("exportZipFail")(res.message || "Unknown error") as string);
      }
    } catch (e) {
      setError(t("exportZipFail")(e instanceof Error ? e.message : String(e)) as string);
    }
  }

  function handleDiscardBackup() {
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    if (!activeRoot) return;
    setShowDiscardConfirm(true);
  }

  async function confirmAndDiscardBackup() {
    const activeRoot = translationMode === "mods" ? gameRoot : root;
    setShowDiscardConfirm(false);
    if (!activeRoot) return;
    try {
      setPhase("deleting_backup");
      setError(null);
      const res = await discardBackup(activeRoot);
      if (res.success) {
        setHasBackup(false);
        setError(t("deleteBackupSuccess") as string);
      } else {
        setError(res.message || "Failed to delete backup");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      getBackupStatus(activeRoot).then(({ has_backup }) => setHasBackup(has_backup)).catch(() => {});
      setPhase("idle");
    }
  }

  function getProgressStatusText(p: TranslateProgress): string {
    if (isPaused || p.phase === "paused") {
      return t("statusPaused")(
        p.batch_num ?? 1,
        p.batch_size ?? 0
      ) as string;
    }
    if (p.phase === "initializing") {
      return t("statusInitializing") as string;
    } else if (p.phase === "translating_batch") {
      return t("statusTranslatingBatch")(
        p.batch_num ?? 1,
        p.batch_size ?? 0,
        p.elapsed ?? 0,
        p.try_i !== undefined ? p.try_i + 1 : 1
      ) as string;
    } else if (p.phase === "waiting_retry") {
      return t("statusWaitingRetry")(
        p.batch_num ?? 1,
        p.batch_size ?? 0,
        p.try_i !== undefined ? p.try_i + 1 : 1,
        p.wait_left ?? 0
      ) as string;
    } else if (p.phase === "completed_batch") {
      return t("statusCompletedBatch")(p.batch_num ?? 1) as string;
    } else if (p.phase === "waiting_delay") {
      return t("statusWaitingDelay")(p.wait_left ?? 0) as string;
    } else if (p.phase === "resting") {
      return t("statusResting") as string;
    } else if (p.phase === "error") {
      return t("statusWorkerError") as string;
    } else if (p.phase === "done") {
      return t("statusDone") as string;
    } else if (p.status) {
      return p.status;
    }
    return "";
  }

  const busy = phase !== "idle";

  // Static "translated X / Y" counts for a loaded project, by UNIQUE (text,
  // context) — the same dedup unit the translator works in — so the resting
  // readout matches the live progress bar exactly. Computed whenever a project
  // is open, so the user sees coverage right after picking the folder, not only
  // once a run starts.
  const { doneUnique, totalUnique } = useMemo(() => {
    if (!project || !strings.length) return { doneUnique: 0, totalUnique: 0 };
    const all = new Set<string>();
    const done = new Set<string>();
    for (const s of strings) {
      if (translationMode === "mods" && modsDir && gameRoot) {
        const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();
        const filePath = s.file.startsWith("uasset://") ? s.file.substring(9) : s.file;
        const strAbs = norm(`${gameRoot}/${filePath}`);
        const isExcluded = Array.from(filterHiddenModPaths).some((hiddenPath) => {
          const modAbs = norm(`${modsDir}/${hiddenPath}`);
          return strAbs === modAbs || strAbs.startsWith(modAbs + "/") || strAbs.startsWith(modAbs + "!");
        }) || !selectedModPaths.some((modPath) => {
          const modAbs = norm(`${modsDir}/${modPath}`);
          return strAbs === modAbs || strAbs.startsWith(modAbs + "/") || strAbs.startsWith(modAbs + "!");
        });
        if (isExcluded) continue;
      }
      const key = `${s.original}\x00${s.context}`;
      all.add(key);
      const entry = project.strings[s.id];
      if (entry?.translated || entry?.approved) done.add(key);
    }
    return { doneUnique: done.size, totalUnique: all.size };
  }, [project, strings, translationMode, modsDir, gameRoot, filterHiddenModPaths, selectedModPaths]);

  const selectedModsInfo = detectedMods.filter((m) => selectedModPaths.includes(m.path));
  const selectedEngines = Array.from(new Set(selectedModsInfo.map((m) => m.engine).filter(Boolean)));
  const hasMixedEngines = selectedEngines.length > 1;
  const activeRoot = translationMode === "mods" ? gameRoot : root;


  const translateDisabled =
    busy && !isPaused ||
    !strings.length ||
    !activeRoot ||
    (providerInfo.needsKey && !primaryKey) ||
    (translationMode === "mods" && (selectedModPaths.length === 0 || hasMixedEngines));

  // phase_* keys map to plain strings; cast narrows the t() union for JSX.
  const phaseLabel = busy ? (isFullyPaused ? (t("phase_paused" as StringKey) as string) : (t(`phase_${phase}` as StringKey) as string)) : "";

  // Filter strings by search query (original, translation, path), type and mode
  const filteredStrings = useMemo(() => {
    let result = strings;

    // Filter by mod paths if in mods mode — only show strings from selected mods
    if (translationMode === "mods" && modsDir && gameRoot) {
      const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();
      result = result.filter((s) => {
        if (justTranslatedIds.has(s.id)) return true;
        const filePath = s.file.startsWith("uasset://") ? s.file.substring(9) : s.file;
        const strAbs = norm(`${gameRoot}/${filePath}`);
        // Check if this string belongs to any hidden or unselected mod
        const isExcluded = Array.from(filterHiddenModPaths).some((hiddenPath) => {
          const modAbs = norm(`${modsDir}/${hiddenPath}`);
          return strAbs === modAbs || strAbs.startsWith(modAbs + "/") || strAbs.startsWith(modAbs + "!");
        }) || !selectedModPaths.some((modPath) => {
          const modAbs = norm(`${modsDir}/${modPath}`);
          return strAbs === modAbs || strAbs.startsWith(modAbs + "/") || strAbs.startsWith(modAbs + "!");
        });
        return !isExcluded;
      });
    }

    // Filter by string type (regular vs python/uscore)
    if (stringTypeFilter === "regular") {
      result = result.filter((s) => !s.path.includes("uscore") || justTranslatedIds.has(s.id));
    } else if (stringTypeFilter === "python") {
      result = result.filter((s) => s.path.includes("uscore") || justTranslatedIds.has(s.id));
    } else if (stringTypeFilter === "none") {
      result = result.filter((s) => justTranslatedIds.has(s.id));
    }

    // Filter by translation status
    if (translationStatusFilter === "translated") {
      result = result.filter((s) => {
        if (justTranslatedIds.has(s.id)) return true;
        const entry = project?.strings[s.id];
        return entry && entry.translated && entry.translated.trim() !== "";
      });
    } else if (translationStatusFilter === "untranslated") {
      result = result.filter((s) => {
        if (justTranslatedIds.has(s.id)) return true;
        const entry = project?.strings[s.id];
        return !entry || !entry.translated || entry.translated.trim() === "";
      });
    }

    // Filter only those that contain foreign words in their translation (ignoring Ren'Py tags)
    if (onlyLatinInTranslation) {
      const foreignRegex = getForeignWordRegex(target);
      // Pre-compute latin text length ONCE per string, then filter + sort from the map
      const lenMap = new Map<string, number>();
      for (const s of result) {
        const entry = project?.strings[s.id];
        const trans = entry?.translated || "";
        if (!trans) { lenMap.set(s.id, 0); continue; }
        let len = 0;
        const parts = trans.split(/(\{.*?\}|\[.*?\])/g);
        for (const part of parts) {
          if (part.startsWith('{') || part.startsWith('[')) continue;
          foreignRegex.lastIndex = 0;
          let match;
          while ((match = foreignRegex.exec(part)) !== null) {
            if (!isTechnicalWord(match[0], part, match.index)) {
              len += match[0].length;
            }
          }
        }
        lenMap.set(s.id, len);
      }
      result = result.filter((s) => {
        if (justTranslatedIds.has(s.id)) return true;
        return (lenMap.get(s.id) || 0) > 0;
      });
      result = [...result].sort((a, b) => (lenMap.get(b.id) || 0) - (lenMap.get(a.id) || 0));
    }

    if (searchQuery) {
      const q = searchQuery.toLowerCase();
      result = result.filter((s) => {
        if (justTranslatedIds.has(s.id)) return true;
        
        const entry = project?.strings[s.id];
        const orig = s.original.toLowerCase();
        const trans = (entry?.translated || "").toLowerCase();
        const loc = s.path.join(" ").toLowerCase();

        if (searchMode === "original") {
          return orig.includes(q) || loc.includes(q);
        } else if (searchMode === "translation") {
          return trans.includes(q);
        } else if (searchMode === "none") {
          return false;
        } else {
          return orig.includes(q) || trans.includes(q) || loc.includes(q);
        }
      });
    }

    // Sort recently translated rows to the top
    if (justTranslatedIds.size > 0) {
      result = [...result].sort((a, b) => {
        const aJust = justTranslatedIds.has(a.id) ? 1 : 0;
        const bJust = justTranslatedIds.has(b.id) ? 1 : 0;
        return bJust - aJust;
      });
    }

    return result;
  }, [strings, searchQuery, searchMode, onlyLatinInTranslation, project, stringTypeFilter, target, justTranslatedIds, translationStatusFilter, translationMode, selectedModPaths, modsDir, gameRoot, filterHiddenModPaths]);

  // Memoize mod list statistics to avoid expensive loops on every render (e.g., during checkbox selection).
  const renderModsList = useMemo(() => {
    if (translationMode !== "mods" || detectedMods.length === 0) return [];

    const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+/g, "/").toLowerCase();
    
    // 2. Order mods into three contiguous tiers (the user-requested grouping):
    //    0 = needs translating (top), 1 = "Уже переведено", 2 = "Нет строк" (bottom).
    //    Stable sort preserves the backend order (count desc, then name) within a tier.
    const modHasStrings = (mod: ModInfo): boolean => {
      if (extractedModPaths.has(mod.path)) {
        const mAbs = norm(`${modsDir || ""}/${mod.path}`);
        for (const s of strings) {
          const fp = s.file.startsWith("uasset://") ? s.file.substring(9) : s.file;
          const sAbs = norm(`${gameRoot || ""}/${fp}`);
          if (sAbs === mAbs || sAbs.startsWith(mAbs + "/") || sAbs.startsWith(mAbs + "!")) return true;
        }
        return false;
      }
      return (mod.total_count ?? 0) > 0;
    };
    const tierOf = (mod: ModInfo): number => {
      if (mod.already_translated) return 1;
      return modHasStrings(mod) ? 0 : 2;
    };
    const sortedMods = [...detectedMods].sort((a, b) => tierOf(a) - tierOf(b));

    // 3. Precalculate statistics for each mod
    return sortedMods.map((mod) => {
      let total = mod.total_count ?? 0;
      let translated = mod.translated_count ?? 0;

      const isSelected = selectedModPaths.includes(mod.path);
      const isExtracted = extractedModPaths.has(mod.path);

      if (isExtracted && strings.length > 0) {
        const modAbs = norm(`${modsDir || ""}/${mod.path}`);
        const modStrings = strings.filter((str) => {
          const filePath = str.file.startsWith("uasset://") ? str.file.substring(9) : str.file;
          const strAbs = norm(`${gameRoot || ""}/${filePath}`);
          return strAbs === modAbs || strAbs.startsWith(modAbs + "/") || strAbs.startsWith(modAbs + "!");
        });
        total = modStrings.length;
        translated = modStrings.filter((str) => {
          const entry = project?.strings[str.id];
          return entry && entry.translated && entry.translated.trim() !== "";
        }).length;
      }

      const hasStrings = total > 0;
      const isDisabled = !hasStrings;
      const percent = hasStrings ? Math.round((translated / total) * 100) : 0;
      const finalIsSelected = hasStrings && isSelected;

      const isCurrentlyExtracting =
        phase === "extracting" &&
        finalIsSelected &&
        !extractedModPaths.has(mod.path);

      return {
        mod,
        total,
        translated,
        hasStrings,
        isSelected: finalIsSelected,
        isDisabled,
        percent,
        isCurrentlyExtracting,
      };
    });
  }, [
    translationMode,
    detectedMods,
    extractedModPaths,
    modsDir,
    strings,
    gameRoot,
    selectedModPaths,
    project,
    phase
  ]);

  const selectableMods = useMemo(() => renderModsList.filter((m) => !m.isDisabled), [renderModsList]);
  const isAllModsSelected = selectableMods.length > 0 && selectableMods.every((m) => m.isSelected);
  const isHeaderCheckboxDisabled = (busy && !isPaused) || selectableMods.length === 0;

  // Pagination math. pageCount is at least 1 so "page 1 of 1" reads right on a
  // small project. Clamp the current page in case strings shrank under it (e.g.
  // a re-extract), so we never slice past the end or show a blank page.
  const pageCount = Math.max(1, Math.ceil(filteredStrings.length / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageStart = safePage * PAGE_SIZE;
  const pageRows = filteredStrings.slice(pageStart, pageStart + PAGE_SIZE);
  // True when selected strings have duplicates elsewhere in the table.
  // Shows the "Перевести все копии" button in the floating action bar.
  const hasDuplicatesForSelection = useMemo(() => {
    if (selectedIds.size === 0) return false;
    const selectedOriginals = new Set(
      strings.filter((s) => selectedIds.has(s.id)).map((s) => s.original)
    );
    return strings.some((s) => !selectedIds.has(s.id) && selectedOriginals.has(s.original));
  }, [strings, selectedIds]);

  return (
    <main className="app">
      <UpdateOverlay onStateChange={setUpdateBusyTracked} />

      {selectedIds.size > 0 && (
        <div className="floating-action-bar">
          <span className="selected-count">
            Выбрано строк: <strong>{selectedIds.size}</strong>
          </span>
          <button
            className="retranslate-btn"
            onClick={(e) => {
              if (e.shiftKey) {
                // Shift+клик: открыть modal для ввода кастомного правила
                setExtraInstructionText("");
                setExtraInstructionModal({ ids: Array.from(selectedIds) });
              } else {
                translateAll(Array.from(selectedIds));
                setSelectedIds(new Set());
              }
            }}
            disabled={busy && !isPaused}
            title="Перевести выбранные строки заново (только в кэш, без внедрения в игру) | Shift+клик — открыть окно со своим правилом для модели"
          >
            🔄 Переперевести
          </button>
          {hasDuplicatesForSelection && (
            <button
              className="retranslate-btn translate-copies-btn"
              onClick={() => {
                const selectedOriginals = new Set(
                  strings.filter((s) => selectedIds.has(s.id)).map((s) => s.original)
                );
                const allIds = strings
                  .filter((s) => selectedOriginals.has(s.original))
                  .map((s) => s.id);
                translateAll(allIds);
                setSelectedIds(new Set());
              }}
              disabled={busy && !isPaused}
              title="Перевести ИИ и применить одинаковый результат ко всем строкам с таким же оригинальным текстом (только в кэш, без внедрения в игру)"
            >
              📋 Перевести все копии
            </button>
          )}
          <button
            className="reset-selection-btn"
            onClick={() => setSelectedIds(new Set())}
            title="Сбросить выделение всех строк"
          >
            ❌ Сбросить выбор
          </button>
        </div>
      )}

      <header className="topbar">
        <div className="brand">
          <h1 className={`brand-title engine-${engine || "none"}`}>Interprex</h1>
          <span className="tagline">{t("appTagline")}</span>
        </div>

        <div className={`mode-switcher engine-${engine || "none"}`}>
          <button
            className={`mode-btn ${translationMode === "game" ? "active" : ""}`}
            disabled={busy && !isPaused}
            onClick={() => changeTranslationMode("game")}
          >
            {t("modeGame")}
          </button>
          <button
            className={`mode-btn ${translationMode === "mods" ? "active" : ""}`}
            disabled={busy && !isPaused}
            onClick={() => changeTranslationMode("mods")}
          >
            {t("modeMods")}
          </button>
        </div>

        <div className="topright">
          <label className="field">
            <span>{t("uiLanguage")}</span>
            <select
              value={lang}
              onChange={(e) => setUiLang(e.target.value as typeof lang)}
            >
              {UI_LANGUAGES.map((l) => (
                <option key={l.code} value={l.code}>
                  {l.label}
                </option>
              ))}
            </select>
          </label>
          <span className={`dot ${sidecarUp ? "up" : "down"}`}>
            {sidecarUp == null
              ? "…"
              : sidecarUp
                ? t("sidecarOnline")
                : t("sidecarOffline")}
          </span>
          <button
            id="proxy-settings-btn"
            className="gear-btn"
            title={t("proxySettingsTitle") as string}
            onClick={() => {
              setProxyUrlDraft(loadSetting("proxyUrl", ""));
              setProxyCheckResults(null);
              setProxyInfoOpen(false);
              setShowProxyPanel((v) => !v);
            }}
          >
            ⚙
          </button>
        </div>
      </header>

      {showProxyPanel && (
        <div className="proxy-panel" id="proxy-panel">
          <div className="proxy-panel-header">
            <span className="proxy-panel-title">{t("proxySettingsTitle") as string}</span>
            <button className="proxy-close-btn" onClick={() => setShowProxyPanel(false)}>✕</button>
          </div>

          <label className="proxy-url-row">
            <span className="proxy-url-label">{t("proxyUrlLabel") as string}</span>
            <div className="proxy-url-input-wrap">
              <input
                id="proxy-url-input"
                className="proxy-url-input"
                type="text"
                value={proxyUrlDraft}
                placeholder={t("proxyUrlPlaceholder") as string}
                onChange={(e) => setProxyUrlDraft(e.target.value)}
              />
              <button
                className="proxy-save-btn"
                disabled={proxyChecking}
                onClick={() => {
                  // Autocheck decides direct-vs-proxy PER provider and applies it.
                  // No proxy URL → just clear everything to direct.
                  void runProxyAutocheck(proxyUrlDraft.trim());
                }}
              >
                {proxyChecking ? (t("proxyChecking") as string) : (t("proxySave") as string)}
              </button>
            </div>
            <span className="proxy-url-hint">{t("proxyUrlHint") as string}</span>
          </label>

          {proxyCheckResults && (
            <div className="proxy-check-results">
              {Object.keys(proxyCheckResults).length === 0 ? (
                <span className="proxy-check-fail">{t("proxyCheckFailed") as string}</span>
              ) : (
                <>
                  {Object.entries(proxyCheckResults).map(([p, r]) => {
                    const label = PROVIDERS.find((x) => x.id === p)?.label ?? p;
                    const icon = r.mode === "direct" ? "🟢" : r.mode === "proxy" ? "🟣" : "⚪";
                    const verdict =
                      r.mode === "direct"
                        ? (t("proxyModeDirect") as string)
                        : r.mode === "proxy"
                          ? (t("proxyModeProxy") as string)
                          : (t("proxyModeUnknown") as string);
                    return (
                      <div key={p} className="proxy-check-row">
                        <span className="proxy-check-icon">{icon}</span>
                        <span className="proxy-check-name">{label}</span>
                        <span className="proxy-check-verdict">{verdict}</span>
                      </div>
                    );
                  })}
                  <button
                    className="proxy-save-btn proxy-check-done"
                    onClick={() => setShowProxyPanel(false)}
                  >
                    {t("proxyDone") as string}
                  </button>
                </>
              )}
            </div>
          )}

          <button
            className="proxy-info-toggle"
            onClick={() => setProxyInfoOpen((v) => !v)}
          >
            <span className="proxy-info-icon">ℹ</span>
            {t("proxyInfoTitle") as string}
            <span className="proxy-info-chevron">{proxyInfoOpen ? "▲" : "▼"}</span>
          </button>

          {proxyInfoOpen && (
            <div className="proxy-info-body">
              <p className="proxy-info-step">
                1.{" "}
                <a
                  className="proxy-info-link"
                  href="#"
                  onClick={(e) => { e.preventDefault(); openUrl("https://github.com/CaHeK20021/interprex-proxy"); }}
                >
                  github.com/CaHeK20021/interprex-proxy
                </a>
                {t("proxyInfoStep1Suffix") as string}
              </p>
              <p className="proxy-info-step">{t("proxyInfoStep2") as string}</p>
              <p className="proxy-info-step">{t("proxyInfoStep3") as string}</p>
              <p className="proxy-info-step">{t("proxyInfoStep4") as string}</p>
              <p className="proxy-info-free">{t("proxyInfoFree") as string}</p>
            </div>
          )}
        </div>
      )}

      <div className="controls">
        {translationMode === "game" ? (
          <button onClick={() => setFolderPickerKind("game")} disabled={busy && !isPaused}>
            {t("openFolder")}
          </button>
        ) : (
          <button onClick={() => setFolderPickerKind("mods")} disabled={busy && !isPaused}>
            {t("openModsFolder")}
          </button>
        )}
        <label className="field">
          <span>{t("targetLanguage")}</span>
          <select
            value={target}
            onChange={(e) => {
              const next = e.target.value as TargetLang;
              setTarget(next);
              saveSetting("targetLang", next);
            }}
            disabled={busy && !isPaused}
          >
            {TARGET_LANGS.map((l) => (
              <option key={l} value={l}>
                {getTargetLangLabel(l)}
              </option>
            ))}
          </select>
        </label>
        {engine === "renpy" && (
          <label className="field">
            <span>{t("fontStyle")}</span>
            <div className="font-style-toggle" role="group" aria-label={t("fontStyle") as string}>
              <button
                type="button"
                className={fontStyle === "smooth" ? "active" : ""}
                onClick={() => setFontStyle("smooth")}
                disabled={busy && !isPaused}
                title={t("fontStyleSmoothHint") as string}
              >
                {t("fontStyleSmooth")}
              </button>
              <button
                type="button"
                className={`fs-pixel ${fontStyle === "pixel" ? "active" : ""}`}
                onClick={() => setFontStyle("pixel")}
                disabled={busy && !isPaused}
                title={t("fontStylePixelHint") as string}
              >
                {t("fontStylePixel")}
              </button>
            </div>
          </label>
        )}
        <button onClick={() => handleTranslate()} disabled={translateDisabled}>
          {t("translate")}
        </button>
        {activeRoot && !busy && (
          <button
            onClick={handleExportZip}
            className="btn-secondary"
            title={t("exportZipHint") as string}
          >
            {t("exportZip") as string}
          </button>
        )}
        {phase === "translating" && (
          <button
            onClick={togglePause}
            className="btn-secondary"
            disabled={!progress && !pyProgress}
          >
            {isPaused ? t("btnResume") : t("btnPause")}
          </button>
        )}
        {engine && <span className={`badge engine-${engine}`}>{engineLabel(engine, translationMode === "mods")}</span>}
        {busy && <span className="phase">{phaseLabel}…</span>}
        {hasBackup && (
          <div className="backup-group">
            <span className="backup-label">{t("backupStatusLabel")}</span>
            <button
              className="btn-danger"
              onClick={handleRestoreBackup}
              disabled={busy && !isPaused}
              title={t("restoreOriginalHint") as string}
            >
              {t("restoreOriginal") as string}
            </button>
            <button
              className="btn-secondary"
              onClick={handleDiscardBackup}
              disabled={busy && !isPaused}
              title={t("deleteBackupHint") as string}
            >
              {t("deleteBackup") as string}
            </button>
          </div>
        )}
      </div>

      {/* Engine-oracle lint result after a translate: the game's OWN engine found
          real hazards in our injected files (e.g. a translated "100%"). Only
          actionable findings are shown; benign tl/-lint noise is filtered out. */}
      {engine === "renpy" && lintResult && lintResult.available &&
        lintResult.actionable_count > 0 && (
        <div className="risk-banner risk-high">
          <strong>{t("lintHazardTitle")(lintResult.actionable_count) as string}</strong>
          <ul className="lint-findings">
            {lintResult.ours.filter((f) => f.actionable).slice(0, 6).map((f, i) => (
              <li key={i}>
                <code>{f.file.split("/").pop()}:{f.line}</code> {f.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Resting coverage bar: shown when a project is loaded but no translation
          is running, so "X / Y translated" is visible the moment you pick a
          folder. The live bar below replaces it during a run. */}
      {!progress && !busy && totalUnique > 0 && (
        <div className="progress">
          <div className="progressbar">
            <div
              className={`progressfill engine-${engine || "none"}`}
              style={{ width: `${Math.round((doneUnique / totalUnique) * 100)}%` }}
            />
          </div>
          <span className={`progresslabel engine-${engine || "none"}`}>
            <strong>{t("progressLabel")(doneUnique, totalUnique)}</strong>
          </span>
        </div>
      )}

      {progress && progress.total > 0 && (
        <div className="progress">
          <div className="progressbar">
            <div
              className={`progressfill engine-${engine || "none"}`}
              style={{
                width: `${Math.round((progress.done / progress.total) * 100)}%`,
              }}
            />
          </div>
          <span className={`progresslabel engine-${engine || "none"}`}>
            <strong>{t("progressLabel")(progress.done, progress.total)}</strong>
            {(() => {
              if (providerInfo.needsKey) return null;
              const msg = getProgressStatusText(progress);
              if (!msg) return null;
              return (
                <span className="progress-status-msg" style={{ marginLeft: "8px", opacity: 0.85 }}>
                  ({msg})
                </span>
              );
            })()}
          </span>
        </div>
      )}

      {pyProgress && pyProgress.total > 0 && (
        <div className="progress">
          <div className="progressbar">
            <div
              className="progressfill engine-renpy"
              style={{ width: `${Math.round((pyProgress.done / pyProgress.total) * 100)}%` }}
            />
          </div>
          <span className="progresslabel engine-renpy">
            <strong>
              {pyProgress.stage === "classify"
                ? (t("pyProgressClassify")(pyProgress.done, pyProgress.total) as string)
                : (t("pyProgressTranslate")(pyProgress.done, pyProgress.total) as string)}
            </strong>
            <span style={{ marginLeft: '6px', opacity: 0.7, fontSize: '0.85em' }}>
              ({t("translatePythonBtn") as string})
            </span>
          </span>
        </div>
      )}

      {/* Live per-thread status — a collapsible grid, one card per worker. The
          panel shows 2 cards collapsed; the toggle expands to all N (only
          enabled when there ARE more than 2 to reveal). Height adapts to N so it
          never opens empty space. Cloud only (local runs a single worker). */}
      {phase === "translating" && providerInfo.needsKey && (() => {
        const n = Math.max(1, threads * keyCount);
        const expandable = n > 2;
        const open = expandable && workersPanelOpen;
        const visible = open ? n : Math.min(2, n);
        return (
          <div className={`workers-panel ${open ? "open" : "collapsed"} engine-${engine || "none"}`}>
            <div className="workers-panel-head">
              <span className="workers-panel-title">
                {t("threads")}: {n}
              </span>
              <button
                className="workers-toggle btn-secondary"
                onClick={() => setWorkersPanelOpen((v) => !v)}
                disabled={!expandable}
                title={(open ? t("workersToggleCollapse") : t("workersToggleExpand")) as string}
              >
                {open ? "▴" : "▾"}
              </button>
            </div>
            <div className="workers-grid">
              {Array.from({ length: visible }, (_, i) => {
                const ph = workerPhases[i] ?? "idle";
                const msg = keyStatuses[i] ?? "";
                // Traffic-light tone: request in flight (yellow), answer landed
                // (green), errored (red), or stopped/idle (grey — a thread that
                // finished or went to rest). Waiting/paused keep their own accent.
                const tone =
                  ph === "completed_batch"
                    ? "ok"
                    : ph === "error"
                      ? "err"
                      : ph === "translating_batch" || ph === "initializing"
                        ? "busy"
                        : ph === "done" || ph === "resting" || ph === "idle"
                          ? "stopped"
                          : "idle";
                return (
                  <div key={i} className={`worker-card phase-${ph} tone-${tone}`}>
                    <span className="worker-card-name">
                      <span className={`worker-dot tone-${tone}`} />
                      {t("workerLabel")(i + 1)}
                    </span>
                    <span className="worker-card-status">{msg || t("statusResting")}</span>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })()}

      {/* Indeterminate bar for phases with no countable progress (detect /
          extract / save / write-back). Python translation has its own bar below.
          Regular translation has its own exact bar above. */}
      {busy && phase !== "translating" && (
        <div className="progress">
          <div className="progressbar">
            {injectProgress && injectProgress.totalMods > 1 ? (
              <div
                className={`progressfill engine-${engine || "none"}`}
                style={{
                  width: `${Math.round((injectProgress.doneMod / injectProgress.totalMods) * 100)}%`,
                }}
              />
            ) : (
              <div className={`progressfill indeterminate engine-${engine || "none"}`} />
            )}
          </div>
          <span className={`progresslabel engine-${engine || "none"}`}>
            {phaseLabel}…
            {injectProgress && injectProgress.totalMods > 1 && (
              <> {injectProgress.doneMod}/{injectProgress.totalMods} (<span className="progress-mod-name" title={injectProgress.modName}>{injectProgress.modName}</span>)</>
            )}
          </span>
        </div>
      )}

      <div className="controls provider-row">
        <label className="field">
          <span className={`control-label engine-${engine || "none"}`}>
            <strong>{t("provider")}</strong>
          </span>
          <select
            value={provider}
            onChange={(e) => {
              const next = e.target.value as ProviderId;
              setProvider(next);
              saveSetting("provider", next);
              // Restore this provider's own saved connection settings, so the
              // fields show ITS url/key/model rather than the old provider's.
              setBaseUrl(loadProviderSetting("providerBaseUrl", next, ""));
              setApiKeys(loadProviderKeys(next));
              setShownKeys(new Set());
              setModel(loadProviderSetting("providerModel", next, ""));
              setThreads(Math.min(10, Math.max(1, Number(loadProviderSetting("providerThreads", next, "1")) || 1)));
              setRpmLimit(Math.max(0, Number(loadProviderSetting("providerRpm", next, "0")) || 0));
              
              // Synchronously restore freeOnly for openrouter
              const isOp = next === "openrouter";
              const restoredFreeOnly = isOp ? (loadSetting("openrouterFreeOnly", "false") === "true") : false;
              setFreeOnly(restoredFreeOnly);
            }}
            disabled={busy && !isFullyPaused}
          >
            {PROVIDERS.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className={`control-label engine-${engine || "none"}`}>
            <strong>{t("model")}</strong>
          </span>
          {models.length ? (
            <select
              value={models.includes(model) ? model : ""}
              onChange={(e) => {
                setModel(e.target.value);
                saveProviderSetting("providerModel", provider, e.target.value);
              }}
              disabled={busy && !isFullyPaused}
            >
              {/* Empty option only while the current model isn't one we found,
                  so the box never silently shows the wrong row. */}
              {!models.includes(model) && (
                <option value="">{t("modelTypeManually") as string}</option>
              )}
              {models.map((m) => (
                <option key={m} value={m}>
                  {m}
                  {m === activeModel ? ` · ${t("modelAutoActive")}` : ""}
                </option>
              ))}
            </select>
          ) : provider === "custom" ? (
            // Custom provider: always show a typeable field — the user knows
            // their model name even if /models isn't reachable.
            <input
              value={model}
              placeholder={t("modelPlaceholderLocal")}
              onChange={(e) => {
                setModel(e.target.value);
                saveProviderSetting("providerModel", provider, e.target.value);
              }}
              disabled={busy && !isFullyPaused}
            />
          ) : providerInfo.needsKey ? (
            // Key-needing provider (Gemini) with no models: the field stays
            // LOCKED until a key actually lists models (then the dropdown above
            // replaces this). A non-empty key alone isn't proof — "1" lists
            // nothing, so the field never unlocks for garbage. The placeholder
            // narrates which state we're in.
            <input
              value=""
              readOnly
              placeholder={
                !primaryKey
                  ? t("modelNeedKey")
                  : modelsLoading
                    ? t("modelCheckingKey")
                    : t("modelBadKey")
              }
              disabled
            />
          ) : (
            // Local servers have no key, so the field stays typeable as a manual
            // fallback when the server can't be reached.
            <input
              value={model}
              placeholder={t("modelPlaceholderLocal")}
              onChange={(e) => {
                setModel(e.target.value);
                saveProviderSetting("providerModel", provider, e.target.value);
              }}
              disabled={busy && !isFullyPaused}
            />
          )}
        </label>

        {/* Threads + per-request delay — cloud only (local servers run one model
            in VRAM, so parallel requests just contend; no rate limit to pace). */}
        {providerInfo.needsKey && (
          <>
            <label className="field" title={t("threadsHint") as string}>
              <span className={`control-label engine-${engine || "none"}`}>
                <strong>{t("threads")}</strong>
              </span>
              <select
                value={threads}
                onChange={(e) => {
                  const n = Math.min(10, Math.max(1, Number(e.target.value) || 1));
                  setThreads(n);
                  saveProviderSetting("providerThreads", provider, String(n));
                }}
                disabled={busy && !isFullyPaused}
              >
                {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
            <label className="field" title={t("rpmLimitHint") as string}>
              <span className={`control-label engine-${engine || "none"}`}>
                <strong>{t("rpmLimit")}</strong>
              </span>
              {/* type=text + digit filter, so no ugly native number spinners. */}
              <input
                type="text"
                inputMode="numeric"
                value={rpmLimit || ""}
                placeholder={t("rpmNoLimit") as string}
                className="no-spin"
                style={{ width: 72 }}
                onChange={(e) => {
                  const digits = e.target.value.replace(/[^\d]/g, "");
                  const v = Math.min(100000, Number(digits) || 0);
                  setRpmLimit(v);
                  saveProviderSetting("providerRpm", provider, String(v));
                }}
                disabled={busy && !isFullyPaused}
              />
            </label>
          </>
        )}

        {/* OpenRouter daily free-request budget. Count is local (the API doesn't
            report spent-today); cap comes from the key's tier. */}
        {provider === "openrouter" && orUsage && (
          <span
            className={`or-usage-badge engine-${engine || "none"}`}
            title={t("openrouterDailyUsageHint") as string}
          >
            {t("openrouterDailyUsage")(orUsage.used, orUsage.cap)}
          </span>
        )}

        {provider === "openrouter" && (
          <label className="field">
            <span className={`control-label engine-${engine || "none"}`}>
              <strong>{t("onlyFreeModels")}</strong>
            </span>
            <input
              type="checkbox"
              checked={freeOnly}
              onChange={(e) => {
                const checked = e.target.checked;
                setFreeOnly(checked);
                saveSetting("openrouterFreeOnly", checked ? "true" : "false");
              }}
              disabled={busy && !isFullyPaused}
            />
          </label>
        )}

        <label className="field" title={t("maxBatchSizeHint")}>
          <span className={`control-label engine-${engine || "none"}`}>
            <strong>{t("maxBatchSize")}: <span style={{ display: "inline-block", width: "24px" }}>{maxBatchSize}</span></strong>
          </span>
          {(() => {
            const getEngineColor = (eng: string | null): string => {
              switch (eng) {
                case "rpgmaker": return "#ffa033";
                case "renpy": return "#ff66a0";
                case "godot": return "#3cd4ff";
                case "gamemaker": return "#32e06b";
                case "unity": return "#c084fc";
                case "unreal": return "#eab308";
                case "unreal4_5": return "#f0883e";
                case "csharp": return "#818cf8";
                case "fusion": return "#2dd4bf";
                case "mmf2": return "#fb7185";
                case "qsp": return "#f97316";
                case "twine": return "#d946ef";
                default: return "#5b8cff";
              }
            };
            const color = getEngineColor(engine);
            const pct = ((maxBatchSize - 10) / 90) * 100;
            return (
              <input
                type="range"
                className={`engine-${engine || "none"}`}
                min={10}
                max={100}
                step={1}
                value={maxBatchSize}
                style={{
                  minWidth: 100,
                  verticalAlign: "middle",
                  "--slider-value-percent": `${pct}%`,
                  "--slider-color": color
                } as React.CSSProperties}
                onChange={(e) => {
                  const n = Number(e.target.value);
                  setMaxBatchSize(n);
                  saveSetting("maxBatchSize", String(n));
                }}
                disabled={busy && !isFullyPaused}
              />
            );
          })()}
        </label>



        {/* Server URL: shown for local servers (default-backed) and Kaggle
            (ngrok URL, required, no default). Hidden for pure-cloud (Gemini). */}
        {(!providerInfo.needsKey || providerInfo.needsUrl) && (
          <label className="field">
            <span>{t("baseUrl")}</span>
            <input
              value={baseUrl}
              placeholder={
                provider === "ollama"
                  ? "http://localhost:11434/v1"
                  : provider === "kaggle"
                    ? "https://xxxx.ngrok-free.app/v1"
                    : provider === "custom"
                      ? "http://your-server:8000/v1"
                      : "http://localhost:1234/v1"
              }
              onChange={(e) => {
                setBaseUrl(e.target.value);
                saveProviderSetting("providerBaseUrl", provider, e.target.value);
              }}
              disabled={busy && !isFullyPaused}
            />
          </label>
        )}

        {/* API keys: cloud providers only. A dynamic list — one field per key,
            "+" adds another, "×" removes. Each key spins up its own worker group
            (threads × keys), so adding keys multiplies throughput. */}
        {providerInfo.needsKey && (
          apiKeys.length <= 1 ? (
            <label className="field api-key-field">
              <span className={`control-label engine-${engine || "none"}`}>
                <strong>{t("apiKey")}</strong>
              </span>
              <input
                type={shownKeys.has(0) ? "text" : "password"}
                value={apiKeys[0] ?? ""}
                onChange={(e) => updateKeys([e.target.value])}
                onFocus={() => setShownKeys((s) => new Set(s).add(0))}
                onBlur={() => setShownKeys((s) => { const n = new Set(s); n.delete(0); return n; })}
                disabled={busy && !isFullyPaused}
              />
              <button
                type="button"
                className={`api-key-add btn-secondary engine-${engine || "none"}`}
                onClick={() => updateKeys([...apiKeys, ""])}
                disabled={busy && !isFullyPaused}
                title={t("addKey") as string}
              >
                + {t("addKey")}
              </button>
            </label>
          ) : (
            <div className="api-keys-block">
              <div className="api-keys-head">
                <span className={`control-label engine-${engine || "none"}`}>
                  <strong>{t("apiKey")}</strong>
                </span>
                <button
                  type="button"
                  className={`api-key-add btn-secondary engine-${engine || "none"}`}
                  onClick={() => updateKeys([...apiKeys, ""])}
                  disabled={busy && !isFullyPaused}
                  title={t("addKey") as string}
                >
                  + {t("addKey")}
                </button>
              </div>
              {apiKeys.map((k, i) => (
                <div className="api-key-row" key={i}>
                  <input
                    type={shownKeys.has(i) ? "text" : "password"}
                    value={k}
                    placeholder={`${t("apiKey")} ${i + 1}`}
                    onChange={(e) => {
                      const next = apiKeys.slice();
                      next[i] = e.target.value;
                      updateKeys(next);
                    }}
                    onFocus={() => setShownKeys((s) => new Set(s).add(i))}
                    onBlur={() =>
                      setShownKeys((s) => {
                        const n = new Set(s);
                        n.delete(i);
                        return n;
                      })
                    }
                     disabled={busy && !isFullyPaused}
                  />
                  <button
                    type="button"
                    className="api-key-del"
                    onClick={() => updateKeys(apiKeys.filter((_, j) => j !== i))}
                     disabled={busy && !isFullyPaused}
                    title={t("removeKey") as string}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )
        )}
      </div>

      {translationMode === "mods" && detectedMods.length > 0 && (
        <div className="mods-panel">
          <div className="mods-panel-actions-row">
            <div className="mods-panel-actions">
              <button onClick={handleSelectAllMods} disabled={busy}>
                {t("selectAll")}
              </button>
              <button onClick={handleDeselectAllMods} disabled={busy}>
                {t("deselectAll")}
              </button>
            </div>
          </div>
          <div className="mods-list-header">
            <span className="col-checkbox">
              <input
                type="checkbox"
                checked={isAllModsSelected}
                disabled={isHeaderCheckboxDisabled}
                onChange={() => {
                  if (isAllModsSelected) {
                    void handleDeselectAllMods();
                  } else {
                    void handleSelectAllMods();
                  }
                }}
              />
            </span>
            <span className="col-name">{t("modNameHeader")}</span>
            <span className="col-strings">{t("modStringsHeader")}</span>
            <span className="col-progress">{t("modProgressHeader")}</span>
            <span className="col-status">{t("modStatusHeader")}</span>
          </div>
          <div className="mods-list">
            {renderModsList.map(({
              mod,
              total,
              translated,
              hasStrings,
              isSelected,
              isDisabled,
              percent,
              isCurrentlyExtracting,
            }) => {
              let statusText = t("statusNoStrings");
              let statusClass = "status-empty";
              if (mod.already_translated) {
                statusText = t("statusAlreadyTranslated");
                statusClass = "status-done";
              } else if (isCurrentlyExtracting) {
                statusText = t("statusExtracting");
                statusClass = "status-progress";
              } else if (hasStrings) {
                if (translated === 0) {
                  statusText = t("statusNotStarted");
                  statusClass = "status-todo";
                } else if (translated === total) {
                  statusText = t("statusCompleted");
                  statusClass = "status-done";
                } else {
                  statusText = t("statusInProgress");
                  statusClass = "status-progress";
                }
              }

              return (
                <div
                  key={mod.path}
                  className={`mod-item ${isSelected ? "selected" : ""} ${isDisabled ? "disabled" : ""} ${extractedModPaths.has(mod.path) && total === 0 ? "zero-strings" : ""}`}
                >
                  <span className="col-checkbox">
                    <input
                      type="checkbox"
                      checked={isSelected}
                      readOnly
                      disabled={isDisabled || busy}
                      onClick={(!isDisabled && !busy) ? () => handleToggleMod(mod.path) : undefined}
                    />
                  </span>
                  <span className="mod-name col-name" title={mod.name}>
                    {mod.name}
                  </span>
                  <span className={`col-strings ${hasStrings ? `engine-${mod.engine}` : "status-empty"}`}>
                    {isCurrentlyExtracting ? (
                      t("stringsCalculating")
                    ) : hasStrings ? (
                      `${translated}/${total}`
                    ) : (
                      "—"
                    )}
                  </span>
                  <span className="col-progress">
                    {hasStrings ? (
                      <div className="mod-progress-wrap">
                        <div className="mod-progress-bar">
                          <div
                            className={`mod-progress-fill engine-${mod.engine || "none"}`}
                            style={{ width: `${percent}%` }}
                          />
                        </div>
                        <span className="mod-progress-percent">{percent}%</span>
                      </div>
                    ) : (
                      "—"
                    )}
                  </span>
                  <span className={`col-status ${statusClass}`}>
                    {statusText}
                  </span>
                </div>
              );
            })}
          </div>
          {hasMixedEngines && (
            <div className="mods-warn">
              {t("errMixedEngines")}
            </div>
          )}
          {selectedModPaths.length === 0 && !hasMixedEngines && phase === "idle" && (
            <div className="mods-warn">
              {t("errNoModsSelected")}
            </div>
          )}
        </div>
      )}

      {activeRoot && <div className="path">{activeRoot}</div>}
      {error && <div className="notice">{error}</div>}

      {strings.length > 0 && (
        <div className="search-container">
          <SearchBox
            onChange={handleSearchQueryChange}
            placeholder="Поиск по таблице..."
          />
          
          <div className="filter-dropdown-container" ref={dropdownRef}>
            <button
              className={`filter-dropdown-btn ${getActiveFiltersCount() > 0 ? "active" : ""} engine-${engine || "none"}`}
              onClick={() => setIsFilterMenuOpen(!isFilterMenuOpen)}
              title="Фильтры и настройки поиска"
            >
              ⚙️ Фильтры {getActiveFiltersCount() > 0 ? `(${getActiveFiltersCount()})` : ""} ▾
            </button>
            {isFilterMenuOpen && (
              <div className={`filter-dropdown-menu engine-${engine || "none"}`}>
                <div className="filter-dropdown-item reset-item" onClick={resetAllFilters}>
                  <span>❌ Сбросить все</span>
                </div>
                <div className="filter-dropdown-divider" />
                
                <div className="filter-dropdown-section-title">Искать в:</div>
                <label className="filter-dropdown-item">
                  <input
                    type="checkbox"
                    checked={searchMode === "original" || searchMode === "all"}
                    onChange={(e) => handleSearchModeCheckbox("original", e.target.checked)}
                  />
                  <span>Оригинал</span>
                </label>
                <label className="filter-dropdown-item">
                  <input
                    type="checkbox"
                    checked={searchMode === "translation" || searchMode === "all"}
                    onChange={(e) => handleSearchModeCheckbox("translation", e.target.checked)}
                  />
                  <span>Перевод</span>
                </label>
                
                {engine === "renpy" && (
                  <>
                    <div className="filter-dropdown-divider" />
                    <div className="filter-dropdown-section-title">Тип строк:</div>
                    <label className="filter-dropdown-item">
                      <input
                        type="checkbox"
                        checked={stringTypeFilter === "regular" || stringTypeFilter === "all"}
                        onChange={(e) => handleStringTypeCheckbox("regular", e.target.checked)}
                      />
                      <span>Обычные строки</span>
                    </label>
                    <label className="filter-dropdown-item">
                      <input
                        type="checkbox"
                        checked={stringTypeFilter === "python" || stringTypeFilter === "all"}
                        onChange={(e) => handleStringTypeCheckbox("python", e.target.checked)}
                      />
                      <span>Python-код</span>
                    </label>
                  </>
                )}
                
                {translationMode === "mods" && selectedModsInfo.length > 0 && (
                  <>
                    <div className="filter-dropdown-divider" />
                    <div className="filter-dropdown-section-title">Фильтр модов:</div>
                    <div className="filter-dropdown-select-wrap">
                      <select
                        className="filter-dropdown-select"
                        value={
                          selectedModsInfo.length === 0
                            ? "all"
                            : selectedModsInfo.every(m => !filterHiddenModPaths.has(m.path))
                              ? "all"
                              : selectedModsInfo.find(m => !filterHiddenModPaths.has(m.path))?.path || "all"
                        }
                        onChange={(e) => {
                          const val = e.target.value;
                          setPage(0);
                          if (val === "all") {
                            setFilterHiddenModPaths(new Set());
                          } else {
                            // Hide all mods except the selected one
                            const nextHidden = new Set(selectedModsInfo.map(m => m.path));
                            nextHidden.delete(val);
                            setFilterHiddenModPaths(nextHidden);
                          }
                        }}
                      >
                        <option value="all">{t("allMods") as string}</option>
                        {selectedModsInfo.map((mod) => (
                          <option key={mod.path} value={mod.path}>
                            {mod.name}
                          </option>
                        ))}
                      </select>
                    </div>
                  </>
                )}
                
                <div className="filter-dropdown-divider" />
                
                <div className="filter-dropdown-section-title">Статус перевода:</div>
                <div className="filter-dropdown-select-wrap">
                  <select
                    className="filter-dropdown-select"
                    value={translationStatusFilter}
                    onChange={(e) => {
                      setPage(0);
                      setTranslationStatusFilter(e.target.value as any);
                    }}
                  >
                    <option value="all">Все строки</option>
                    <option value="untranslated">Только непереведенные</option>
                    <option value="translated">Только переведенные</option>
                  </select>
                </div>

                <div className="filter-dropdown-divider" />
                
                <div className="filter-dropdown-section-title">Ошибки / Подсветка:</div>
                <label className="filter-dropdown-item">
                  <input
                    type="checkbox"
                    checked={onlyLatinInTranslation}
                    onChange={handleOnlyLatinToggle}
                  />
                  <span>{getForeignFilterLabel(target)}</span>
                </label>
              </div>
            )}
          </div>

          {translationMode === "mods" && editedModPaths.size > 0 && (
            <button
              className={`btn-inject-dirty engine-${engine || "none"}`}
              onClick={handleInjectEditedMods}
              disabled={busy}
              title={`Внедрить изменения только для измененных модов (${Array.from(editedModPaths).length} шт.)`}
            >
              ⚡ Внедрить измененные ({editedModPaths.size})
            </button>
          )}



          {(searchQuery || onlyLatinInTranslation || stringTypeFilter !== "all" || translationStatusFilter !== "all" || selectedIds.size > 0) && (
            <span className="search-count">
              Найдено: {filteredStrings.length} из {strings.length}
            </span>
          )}
        </div>
      )}

      <div className="tablewrap" ref={tableWrapRef}>
        <table>
          <thead>
            <tr>
              <th style={{ width: "30px", textAlign: "center" }}>
                <input
                  type="checkbox"
                  checked={pageRows.length > 0 && pageRows.every((s) => selectedIds.has(s.id))}
                  onChange={() => selectAllVisible(pageRows)}
                />
              </th>
              <th>{t("colOriginal")}</th>
              <th>{t("colTranslation")}</th>
              <th>{translationMode === "mods" ? "Мод" : "Тип строки"}</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((s) => {
              const entry = project?.strings[s.id];
              const metadata = translationMode === "mods"
                ? getModNameForString(s, detectedMods, gameRoot, modsDir)
                : getStringType(s);
              return (
                <TableRow
                  key={s.id}
                  s={s}
                  translated={entry?.translated}
                  isSelected={selectedIds.has(s.id)}
                  isJustTranslated={justTranslatedIds.has(s.id)}
                  isEditing={editingId === s.id}
                  isBulkEdit={editingId === s.id && isBulkEdit}
                  editingVal={editingVal}
                  metadata={metadata}
                  target={target}
                  startEdit={stableStartEdit}
                  commitEdit={stableCommitEdit}
                  setEditingVal={stableSetEditingVal}
                  setEditingId={stableSetEditingId}
                  onDragSelectStart={stableOnDragSelectStart}
                  onDragSelectEnter={stableOnDragSelectEnter}
                  onShiftSelectRange={stableOnShiftSelectRange}
                />
              );
            })}
          </tbody>
        </table>
        {!strings.length ? (
          <p className="hint">
            {translationMode === "mods" ? (t("hintOpenModsFolder") as string) : (t("hintOpenFolder") as string)}
          </p>
        ) : !filteredStrings.length ? (
          <p className="hint">
            Ничего не найдено по запросу "{searchQuery}"
          </p>
        ) : null}
      </div>

      {filteredStrings.length > PAGE_SIZE && (
        <div className="pager">
          <button onClick={() => goToPage(safePage - 1)} disabled={safePage <= 0}>
            ‹
          </button>
          <span className="pagerlabel">
            {t("pageOf")(safePage + 1, pageCount)}
          </span>
          {/* Type a page and Enter to jump. Clamped to [1, pageCount] so a
              typo never lands on a blank page. value tracks safePage so an
              auto-clamp (shrunk list) shows the real page, not stale input. */}
          <input
            className="pagerinput"
            type="number"
            min={1}
            max={pageCount}
            value={safePage + 1}
            onChange={(e) => {
              const n = Number(e.target.value);
              if (!n) return;
              goToPage(Math.min(pageCount, Math.max(1, n)) - 1);
            }}
          />
          <button
            onClick={() => goToPage(safePage + 1)}
            disabled={safePage >= pageCount - 1}
          >
            ›
          </button>
          <span className="pagerrange">
            {t("showingRows")(
              pageStart + 1,
              Math.min(pageStart + PAGE_SIZE, filteredStrings.length),
              filteredStrings.length,
            )}
          </span>
        </div>
      )}

      {extraInstructionModal && (
        <div className="modal-overlay" onClick={() => setExtraInstructionModal(null)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h2 className="normal-title">Правило для перевода</h2>
            <p>
              Введите дополнительное указание для ИИ. Оно применится только к выбранным ({extraInstructionModal.ids.length}) строкам на этот раз.
            </p>
            <textarea
              className="extra-instruction-textarea"
              value={extraInstructionText}
              onChange={(e) => setExtraInstructionText(e.target.value)}
              placeholder="Например: везде Infinity переводи как бесконечность"
              autoFocus
              onKeyDown={(e) => {
                // Enter = отправка, Shift+Enter = перенос строки.
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  if (!extraInstructionText.trim()) return;
                  translateAll(extraInstructionModal.ids, extraInstructionText);
                  setExtraInstructionModal(null);
                  setExtraInstructionText("");
                  setSelectedIds(new Set());
                } else if (e.key === 'Escape') {
                  setExtraInstructionModal(null);
                }
              }}
            />
            <p className="extra-instruction-hint">
              <kbd>Enter</kbd> — перевести · <kbd>Shift</kbd>+<kbd>Enter</kbd> — новая строка
            </p>
            <div className="modal-actions">
              <button className="btn-secondary" onClick={() => setExtraInstructionModal(null)}>
                Отмена
              </button>
              <button
                className="btn-primary"
                disabled={!extraInstructionText.trim()}
                onClick={() => {
                  if (!extraInstructionText.trim()) return;
                  translateAll(extraInstructionModal.ids, extraInstructionText);
                  setExtraInstructionModal(null);
                  setExtraInstructionText("");
                  setSelectedIds(new Set());
                }}
              >
                Перевести
              </button>
            </div>
          </div>
        </div>
      )}

      {showDiscardConfirm && (
        <div className="modal-overlay" onClick={() => setShowDiscardConfirm(false)}>
          <div className="modal-content" onClick={(e) => e.stopPropagation()}>
            <h2>{t("confirmDiscardBackupTitle") as string}</h2>
            <p>{t("confirmDiscardBackup") as string}</p>
            <div className="modal-actions">
              <button className="btn-secondary" onClick={() => setShowDiscardConfirm(false)}>
                {t("confirmDiscardBackupCancel") as string}
              </button>
              <button className="btn-danger" onClick={confirmAndDiscardBackup}>
                {t("confirmDiscardBackupOk") as string}
              </button>
            </div>
          </div>
        </div>
      )}


      {pythonLogs.length > 0 && (
        <div
          className="python-logs-panel"
          style={{ marginTop: '12px' }}
          onMouseEnter={() => setPythonLogsOpen(true)}
          onMouseLeave={() => { if (!pyLogsMouseDown) setPythonLogsOpen(false); }}
        >
          <div
            className="btn-secondary"
            style={{ display: 'inline-block', cursor: 'default', marginBottom: pythonLogsOpen ? '8px' : 0 }}
          >
            {t("translatePythonTitle") as string} {pythonLogsOpen ? "▴" : "▾"}
            {pythonTranslating && <span style={{ marginLeft: '8px', color: '#888', fontStyle: 'italic' }}>…</span>}
          </div>
          {pythonLogsOpen && (
            <div
              className="python-logs-container"
              onMouseDown={() => setPyLogsMouseDown(true)}
              onMouseUp={() => setPyLogsMouseDown(false)}
              style={{
              background: '#1e1e1e',
              color: '#d4d4d4',
              padding: '12px',
              borderRadius: '4px',
              fontFamily: 'monospace',
              fontSize: '13px',
              lineHeight: '1.4',
              overflowY: 'auto',
              maxHeight: '300px',
              minHeight: '100px',
              textAlign: 'left',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              userSelect: 'text',
              cursor: 'text'
            }}>
              {pythonLogs.map((log, idx) => (
                <div key={idx} className="python-log-line" style={{
                  color: log.includes('[ERROR]') ? '#f44336' : log.includes('[WARNING]') ? '#ffeb3b' : log.includes('[DRY RUN]') ? '#4caf50' : '#d4d4d4',
                  marginBottom: '2px'
                }}>
                  {log}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {folderPickerKind && (
        <FolderPicker
          engineClass={`engine-${engine || "none"}`}
          startPath={
            (folderPickerKind === "mods"
              ? modsDir || (saveLastModsFolder ? loadSetting("lastModsFolder", "") : "")
              : root || (saveLastGameFolder ? loadSetting("lastGameFolder", "") : "")
            ) || ""
          }
          onClose={() => setFolderPickerKind(null)}
          onPick={(p) => {
            const kind = folderPickerKind;
            setFolderPickerKind(null);
            if (kind === "mods") pickModsFolder(p);
            else pickFolder(p);
          }}
          remember={folderPickerKind === "mods" ? saveLastModsFolder : saveLastGameFolder}
          rememberTooltip={
            folderPickerKind === "mods"
              ? (t("fpRememberModsTooltip") as string)
              : (t("fpRememberGameTooltip") as string)
          }
          onRememberChange={(val) => {
            if (folderPickerKind === "mods") {
              setSaveLastModsFolder(val);
              saveSetting("saveLastModsFolder", String(val));
              if (!val) {
                saveSetting("lastModsFolder", "");
              } else if (modsDir) {
                saveSetting("lastModsFolder", modsDir);
              }
            } else {
              setSaveLastGameFolder(val);
              saveSetting("saveLastGameFolder", String(val));
              if (!val) {
                saveSetting("lastGameFolder", "");
              } else if (root) {
                saveSetting("lastGameFolder", root);
              }
            }
          }}
        />
      )}
      {/* Toast notification for bulk-edit confirmation */}
      {toast && (
        <div className="toast-notification" role="status">
          {toast}
        </div>
      )}
    </main>
  );
}
