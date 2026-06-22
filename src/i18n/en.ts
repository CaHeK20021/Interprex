// English UI strings for Interprex itself (NOT the target language games are
// translated into — that's a separate axis, see TARGET_LANGS in i18n/index.ts).
//
// This object's keys are the source of truth: every other locale must provide
// the same keys, enforced by the `Strings` type in index.ts. Add a key here
// first, then the compiler tells you which locales are missing it.

const en = {
  // header
  appTagline: "Game localization, end to end",
  sidecarOnline: "sidecar online",
  sidecarOffline: "sidecar offline",
  uiLanguage: "Interface",
  targetLanguage: "Translate into",
  lang_Russian: "Russian",
  lang_English: "English",
  lang_Spanish: "Spanish",
  lang_German: "German",
  lang_French: "French",
  lang_Japanese: "Japanese",
  lang_Chinese_Simplified: "Chinese (Simplified)",
  lang_Korean: "Korean",
  lang_Portuguese_Brazil: "Portuguese (Brazil)",
  fontStyle: "Font",
  fontStyleSmooth: "Smooth",
  fontStylePixel: "Pixel",
  fontStyleSmoothHint:
    "Smooth font (Noto) — clean and highly readable. Best for most games.",
  fontStylePixelHint:
    "Pixel font (bitmap) — matches the look of pixel-art games. Latin/Cyrillic " +
    "use PixelOperator, Chinese/Japanese use Zpix. Korean and scripts without a " +
    "pixel font fall back to the smooth font.",
  provider: "Engine",
  model: "Model",
  apiKey: "API key",
  apiKey2: "Second API key",
  optional: "optional",
  addKey: "add key",
  removeKey: "remove key",
  // themed folder browser
  fpTitle: "Open game folder",
  fpDrives: "Drives",
  fpUp: "Up one level",
  fpLoading: "Loading…",
  fpEmpty: "No subfolders here",
  fpNoDrives: "No drives found",
  fpPathPlaceholder: "Paste or type a folder path…",
  fpGo: "Go",
  fpCancel: "Cancel",
  fpChoose: "Select this folder",
  fpChooseHint: "Open a folder first, then select it",
  fpRemember: "Remember path",
  fpRememberGameTooltip: "Save the selected game path to open it automatically",
  fpRememberModsTooltip: "Save the selected mods path to open it automatically",
  fpSidebarThisPC: "This PC",
  fpSidebarHome: "User folder",
  fpSidebarLibraries: "Game libraries",
  fpSidebarDownloads: "Downloads",
  fpSidebarDesktop: "Desktop",
  fpSidebarDocuments: "Documents",
  fpSidebarLocalDisk: (drive: string) => `Local Disk ${drive.replace(":", "")}`,
  baseUrl: "Server URL",
  modelPlaceholderLocal: "e.g. llama3.1",
  modelPlaceholderGemini: "gemini-2.5-flash",
  modelLoading: "finding models…",
  modelAutoActive: "active (auto)",
  modelTypeManually: "Type model name…",
  modelNeedKey: "enter an API key to load the model list",
  modelCheckingKey: "checking API key…",
  modelBadKey: "key rejected — no models for this key",
  maxBatchSize: "Batch size",
  maxBatchSizeHint: "Maximum strings to send in a single API request",
  onlyFreeModels: "Only free models",
  threads: "Threads",
  threadsHint:
    "Parallel requests per API key. With 2 keys, each key gets this many. " +
    "Higher = faster, but watch the provider's per-minute limit.",
  rpmLimit: "Limit, req/min",
  rpmNoLimit: "none",
  rpmLimitHint:
    "Your model's requests-per-minute cap PER KEY (from the provider's " +
    "dashboard). The app paces itself to stay under it automatically — it splits " +
    "the limit across the threads on each key, so you never set seconds by hand. " +
    "Leave empty for no limit. (On every cloud API a 429/503 error also spends " +
    "your quota, so retries respect the same pace.)",
  workerLabel: (n: number) => `Thread ${n}`,
  workersToggleExpand: "Show all threads",
  workersToggleCollapse: "Collapse threads",
  openrouterDailyUsage: (used: number, cap: number) =>
    `Free requests today: ${used} / ${cap}`,
  openrouterDailyUsageHint:
    "Free-model requests used today vs your daily cap (50, or 1000 with $10+ " +
    "credit). Counted locally — every request that reaches the server counts, " +
    "even errors. Resets at midnight UTC.",

  // buttons
  openFolder: "Open game folder…",
  translate: "Translate",
  writeBack: "Write back to game",
  restoreOriginal: "Restore Original",
  restoreOriginalHint: "Restore original game files from backup",
  deleteBackup: "Discard Backup",
  deleteBackupHint: "Confirm translation and delete backup files",
  exportZip: "Export Translation (ZIP)",
  exportZipHint: "Pack all translated files into a ZIP archive for sharing",
  confirmDiscardBackupTitle: "Discard Backup",
  confirmDiscardBackup: "Are you sure you want to discard the backup? You will not be able to restore the original files.",
  confirmDiscardBackupOk: "Yes, discard",
  confirmDiscardBackupCancel: "Cancel",
  btnPause: "Pause",
  btnResume: "Resume",
  translatePythonBtn: "Translate Python strings",
  translatePythonBtnHint: "Translate inline Python code strings ($ blocks, init python)",
  translatePythonTitle: "Ren'Py Python String Translation",
  btnDryRun: "Simulation (Dry Run)",
  btnRealRun: "Apply Translation",

  // phases (shown next to the spinner)
  phase_detecting: "detecting",
  phase_extracting: "extracting",
  phase_translating: "translating",
  phase_saving: "saving",
  phase_backing_up: "backing up",
  phase_injecting: "writing back",
  phase_autofixing: "validating & fixing translation",
  phase_restoring: "restoring",
  phase_deleting_backup: "deleting backup",
  autofixFixed: (n: number) => `Autofix repaired ${n} line(s) after translation.`,

  // overflow risk + engine-lint
  riskDialogueTitle: "Dialogue overflow risk:",
  lintHazardTitle: (n: number) =>
    `The game engine's own check found ${n} real issue(s) in the translation:`,

  // progress
  progressLabel: (done: number, total: number) =>
    `${done} / ${total} strings`,
  statusInitializing: "Initializing translator...",
  statusTranslatingBatch: (num: number, size: number, elapsed: number, retry?: number) =>
    `Translating batch ${num} (${size} strings) — ${elapsed}s elapsed` +
    (retry && retry > 1 ? ` (retry ${retry - 1}/100)` : "") +
    (elapsed > 15 ? " (waiting for model response)" : ""),
  statusPaused: (num: number, size: number) =>
    `Translation paused (batch ${num}, ${size} strings)`,
  statusWaitingRetry: (num: number, size: number, retry: number, waitLeft: number) =>
    `Translating batch ${num} (${size} strings) — Waiting before retry ${waitLeft}s` +
    (retry && retry > 0 ? ` (retry ${retry}/100)` : ""),
  statusCompletedBatch: (num: number) => `Completed batch ${num}!`,
  statusWaitingDelay: (waitLeft: number) => `Pacing — ${waitLeft}s left`,
  statusResting: "Resting (waiting for work)",
  statusWorkerError: "Key failed",
  pyStatusWaiting: "Waiting...",
  pyStatusClassifying: "Evaluating translation need...",
  pyStatusClassified: "Evaluation complete",
  pyStatusTranslating: "Translating...",
  pyStatusFinished: "Finished",
  pyStatusBatchDone: (phase: string, cur: string, total: string) => `${phase} batch ${cur}/${total}`,
  pyStatusError: (phase: string) => `Error: ${phase} failed`,
  pyStatusBatchError: (num: number) => `Error translating batch ${num}`,
  // Two-stage Python progress: stage 1 evaluates which candidates need translating
  // (the count is exact, but it's NOT how many will be translated — that's only
  // known after); stage 2 translates the confirmed strings.
  pyProgressClassify: (done: number, total: number) =>
    `Evaluating ${done} / ${total} candidates`,
  pyProgressTranslate: (done: number, total: number) =>
    `Translating ${done} / ${total} strings`,
  pyClassified: "Classified",
  pyTranslated: "Translated",
  statusDone: "Done",
  // pager: which rows this page shows, and which page of how many
  showingRows: (from: number, to: number, total: number) =>
    `${from}–${to} of ${total}`,
  pageOf: (page: number, pages: number) => `Page ${page} of ${pages}`,

  // table
  colOriginal: "Original",
  colTranslation: "Translation",
  colWhere: "Where",

  // messages
  hintOpenFolder: "Open a game folder to extract its strings.",
  hintReadyToTranslatePython: "Ready to translate. Select an action below. Simulation will show what strings will be translated without modifying game files.",
  errNoEngine: "No supported engine detected in that folder.",
  wroteBack: (n: number) => `Wrote ${n} unique strings into the game (duplicates merged — no translation lost).`,
  translateAborted: (done: number, total: number) =>
    `Translation stopped: the model stopped responding after retries. ` +
    `${done} of ${total} strings were translated — fix the backend and run Translate again to finish the rest.`,
  translateErrors: (n: number) =>
    `Finished, but ${n} batch(es) failed and were left untranslated. Run Translate again to retry them.`,
  backupStatusLabel: "Backup active:",
  restoreSuccess: "Original files restored successfully!",
  deleteBackupSuccess: "Backup deleted.",
  exportZipSuccess: (name: string) => `Translation successfully packed to archive:\n${name}\n(file selected in explorer)`,
  exportZipFail: (err: string) => `Failed to export archive: ${err}`,

  // mods mode
  modeGame: "Game Translation",
  modeMods: "Mod Translation",
  openModsFolder: "Open mods folder…",
  detectedModsLabel: "Detected Mods",
  modNameHeader: "Mod Name",
  modStatsHeader: "Strings",
  modStringsHeader: "Strings",
  modProgressHeader: "Progress",
  modStatusHeader: "Status",
  statusNotStarted: "Not started",
  statusInProgress: "In progress",
  statusCompleted: "Done",
  statusNoStrings: "No strings",
  statusExtracting: "Extracting",
  stringsCalculating: "counting",
  noModsDetected: "No mods detected in this folder.",
  allMods: "All mods",
  selectAll: "Select All",
  deselectAll: "Deselect All",
  errNoModsSelected: "Please select at least one mod.",
  errMixedEngines: "Mixed engines detected. Please select only mods of the same engine type.",
  phase_detecting_mods: "detecting mods",
  hintOpenModsFolder: "Open a mods folder to extract strings from mods.",
  wroteBackMods: (n: number) => `Wrote ${n} unique strings into the mods (duplicates merged — no translation lost).`,
  writeBackBtnMods: "Write back to mods",

  // proxy settings panel
  proxySettingsTitle: "Proxy / Custom Server",
  proxyUrlLabel: "Proxy URL",
  proxyUrlPlaceholder: "https://username-space-name.hf.space/v1",
  proxyUrlHint: "Leave blank to use the provider's default server",
  proxyInfoTitle: "How to set up a free proxy (for users in restricted regions)",
  proxyInfoStep1Suffix: " — follow the instructions to create a Space",
  proxyInfoStep2: "2. Log in to Hugging Face (free) and click \"Duplicate Space\" (this deploys a persistent container without timeout limits)",
  proxyInfoStep3: "3. Copy your direct Space URL (e.g. https://username-space-name.hf.space/v1) and paste it above",
  proxyInfoStep4: "4. Choose a provider (e.g. Google Gemini) and translate — if a geo-block occurs, request will route via your Hugging Face Space automatically.",
  proxyInfoFree: "Completely free · Your own container on Hugging Face · Works with Gemini, OpenAI, Groq and more",
  proxySave: "Save & check",
  proxyChecking: "Checking…",
  proxyDone: "Done",
  proxyCheckFailed: "Check failed — proxy unreachable. Verify the URL.",
  proxyModeDirect: "direct (no proxy needed)",
  proxyModeProxy: "via proxy",
  proxyModeUnknown: "unreachable both ways",

  // auto-update overlay
  updateChecking: "Checking for updates…",
  updateDownloading: "Downloading update {version}…",
  updateReady: "Update ready. Restarting…",
  updateLatest: "You're up to date",
  updateError: "Update check failed",
  updateProgress: "{downloaded} / {total} MB",
};

export default en;
