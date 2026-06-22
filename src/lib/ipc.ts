// ============================================================================
// The ONE place the frontend talks to the Python sidecar.
//
// Everything goes through callPython(). Today the transport is HTTP to a
// localhost FastAPI sidecar; if that ever becomes Tauri stdio, a Unix socket,
// or anything else, this file changes and nothing else does. Never fetch() the
// sidecar directly from a component.
// ============================================================================

import type { Engine, FontStyle, TranslationString } from "./types";

/** Port the Python sidecar listens on. Mirror in python-core/main.py. */
const SIDECAR_PORT = 8723;
const BASE = `http://127.0.0.1:${SIDECAR_PORT}`;

/** Single transport seam. Swap the body to change how TS reaches Python. */
export async function callPython<T>(
  method: string,
  params: Record<string, unknown> = {},
): Promise<T> {
  const res = await fetch(`${BASE}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`sidecar ${method} failed (${res.status}): ${detail}`);
  }
  return (await res.json()) as T;
}

// --- Typed wrappers over the raw calls. Components use these, not callPython. -

/** Is the sidecar up? */
export async function ping(): Promise<boolean> {
  try {
    await callPython("ping");
    return true;
  } catch {
    return false;
  }
}

/** Detect which engine lives in a folder (null if unknown). */
export function detectEngine(root: string): Promise<{ engine: Engine | null }> {
  return callPython("detect", { root });
}

/** Pull all translatable strings out of a project folder. */
export function extractStrings(
  root: string,
  engine: Engine,
  subPaths?: string[],
): Promise<{ strings: TranslationString[] }> {
  return callPython("extract", { root, engine, sub_paths: subPaths || [] });
}

/** Write translations back into the engine files. */
export function injectStrings(
  root: string,
  engine: Engine,
  translations: Record<string, string>,
  targetLang?: string,
  subPaths?: string[],
  fontStyle?: FontStyle,
  sizeFixes?: Record<string, number>,
): Promise<{ written: number }> {
  return callPython("inject", {
    root,
    engine,
    translations,
    target_lang: targetLang,
    sub_paths: subPaths || [],
    font_style: fontStyle || "smooth",
    // Ren'Py: per-caption font shrink factors for text that still overflowed
    // after the scheduler re-asked the model to shorten (hybrid fit).
    size_fixes: sizeFixes || {},
  });
}

export interface ModInfo {
  name: string;
  path: string;
  engine: Engine | null;
  translated_count?: number;
  total_count?: number;
}

/** Detect and list mods in a game or mods directory. */
export function detectMods(
  root: string,
): Promise<{ mods_dir: string; game_root: string; mods: ModInfo[] }> {
  return callPython("detect_mods", { root });
}

/** Raw shape the /translate endpoint expects (snake_case to match Python). */
export interface SidecarTranslateReq {
  provider: string;
  target_lang: string;
  glossary: Record<string, string>;
  base_url: string;
  api_key: string;
  api_key_2?: string;
  api_keys?: string[];
  model: string;
  max_context_tokens: number;
  max_batch_size: number;
  // Concurrent workers PER api key (1..10). Total workers = threads * #keys.
  // Cloud only; local providers send 1.
  threads?: number;
  // Minimum wall-clock seconds a request must occupy, to pace under a provider's
  // per-minute limit. 0 = no pacing.
  delay_seconds?: number;
  root?: string;
  engine?: string;
  // "smooth" | "pixel" — measure UI-fit against the same font inject will write.
  font_style?: string;
  items: { id: string; text: string; context: string; file: string; path?: string[] }[];
}

/** Per-batch progress from the streaming /translate endpoint. */
export interface TranslateProgress {
  done: number; // strings translated so far
  total: number; // strings in this run
  batches: number; // batches completed so far
  // Translations from THIS batch, already fanned out to every sharing id, so the
  // UI can fill those rows live. Empty if the batch failed.
  translations: Record<string, string>;
  status?: string; // status message from the backend
  phase?:
    | "initializing"
    | "translating_batch"
    | "completed_batch"
    | "paused"
    | "waiting_retry"
    | "waiting_delay" // pacing: holding the request to the min duration
    | "resting" // idle by priority ramp-down (the pool is too shallow)
    | "error" // this worker's key failed
    | "done"; // this worker finished cleanly
  batch_num?: number;
  batch_size?: number;
  try_i?: number;
  elapsed?: number;
  // Per-worker index 0..N-1 (N = threads * #keys). The new field; key_idx is kept
  // as a legacy alias carrying the same value.
  worker_idx?: number;
  key_idx?: number;
  wait_left?: number;
  // Requests that reached the provider this run (success + error responses), for
  // the OpenRouter daily-quota readout. Absolute count, not a delta.
  requests_sent?: number;
}

export interface TranslateResult {
  translations: Record<string, string>;
  errors: string[];
  // True when the sidecar stopped early because the backend kept failing (a
  // killed/unreachable LLM). The translations map then holds only what landed
  // before the breaker tripped — the run did NOT complete.
  aborted: boolean;
  // Ren'Py: id -> font shrink factor (<1.0) for captions that still overflowed
  // their fixed width after re-asking the model to shorten. Forwarded to inject.
  sizeFixes?: Record<string, number>;
}

/**
 * Run a translation through the sidecar, reading its NDJSON progress stream.
 * `onProgress` fires after every batch so the UI can drive a progress bar; the
 * resolved value is the final translations + errors. Still goes over the same
 * HTTP seam — only this endpoint streams, so it reads the body directly rather
 * than via callPython()'s parse-whole-response path.
 */
export async function translateViaSidecar(
  req: SidecarTranslateReq,
  onProgress?: (p: TranslateProgress) => void,
  signal?: AbortSignal,
): Promise<TranslateResult> {
  const res = await fetch(`${BASE}/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(`sidecar translate failed (${res.status}): ${detail}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: TranslateResult = { translations: {}, errors: [], aborted: false };

  // NDJSON: one JSON object per line. A chunk may split a line, so keep the
  // trailing partial in `buffer` until the next newline completes it.
  const handleLine = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const evt = JSON.parse(trimmed);
    if (evt.type === "progress") {
      onProgress?.({
        done: evt.done,
        total: evt.total,
        batches: evt.batches,
        translations: evt.translations ?? {},
        status: evt.status,
        phase: evt.phase,
        batch_num: evt.batch_num,
        batch_size: evt.batch_size,
        try_i: evt.try_i,
        elapsed: evt.elapsed,
        worker_idx: evt.worker_idx ?? evt.key_idx,
        key_idx: evt.key_idx,
        wait_left: evt.wait_left,
        requests_sent: evt.requests_sent,
      });
    } else if (evt.type === "done") {
      result = {
        translations: evt.translations,
        errors: evt.errors ?? [],
        aborted: evt.aborted ?? false,
        sizeFixes: evt.size_fixes ?? {},
      };
    }
  };

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      handleLine(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
    }
  }
  if (buffer.trim()) handleLine(buffer); // last line may arrive without a newline
  return result;
}

/** List provider ids the sidecar supports. */
export function listProviders(): Promise<{ providers: string[] }> {
  return callPython("providers");
}

/** Ask a backend which models it can serve and which is active right now (the
 *  model a local server has loaded). Both may be empty if the server is down or
 *  can't be queried — the UI then falls back to a free-text model field. */
export function listModels(req: {
  provider: string;
  base_url: string;
  api_key: string;
  free_only?: boolean;
}): Promise<{ models: string[]; active: string }> {
  return callPython("models", req);
}

/** Per-key rate/usage info for the daily free-request badge (OpenRouter). Empty
 *  object for providers that don't implement it or on any failure — the UI then
 *  hides the badge. The API does NOT report requests spent today, only the cap;
 *  the frontend counts spent-today locally. */
export interface KeyLimits {
  is_free_tier?: boolean;
  daily_cap?: number;
  rate_requests?: number | null;
  rate_interval?: string | null;
}

export function keyLimits(req: {
  provider: string;
  base_url: string;
  api_key: string;
}): Promise<KeyLimits> {
  return callPython("key_limits", req);
}

/** Per-provider verdict from the proxy autocheck. */
export interface ProxyAutocheckResult {
  /** "direct" = reachable without proxy (clear base_url); "proxy" = route via the
   *  proxy; "unknown" = probe inconclusive, left direct. */
  mode: "direct" | "proxy" | "unknown";
  reason: string;
}

/** Probe each cloud provider directly first, then via the proxy. Decides which
 *  providers need the proxy and which work direct. Free: lists models only, never
 *  invokes a model, so it spends no money/quota. Never throws server-side. */
export function proxyAutocheck(
  proxyUrl: string,
  providers: Record<string, string>,
): Promise<{ results: Record<string, ProxyAutocheckResult> }> {
  return callPython("proxy/autocheck", { proxy_url: proxyUrl, providers });
}

/** One folder entry in the in-app browser (always a directory). */
export interface FsEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

export interface FsListing {
  path: string; // resolved absolute path ("" at the drive list)
  parent: string | null; // null at a filesystem root → go to drive list
  is_root: boolean; // true = this listing IS the drive list
  entries: FsEntry[];
}

/** List sub-folders of `path` for the themed folder picker. Empty path = drives.
 *  Never throws server-side; a bad path returns the drive list. */
export function listDir(path: string): Promise<FsListing> {
  return callPython("fs/list", { path });
}

/** A sensible starting folder (the user's home), or "" if unavailable. */
export function homeDir(): Promise<{ path: string }> {
  return callPython("fs/home");
}

/** Quick-jump shortcut to an installed game launcher's library folder. */
export interface FsShortcut {
  name: string;
  path: string;
}

/** Installed game-launcher library folders (Steam libs across drives, Epic,
 *  GOG…). Only ones that exist; empty if no launcher is found. */
export function fsShortcuts(): Promise<{ shortcuts: FsShortcut[] }> {
  return callPython("fs/shortcuts");
}

/** Check if game backup files exist in the project directory. */
export function getBackupStatus(root: string): Promise<{ has_backup: boolean }> {
  return callPython("backup/status", { root });
}

/** Restore original game files from the backup directory. */
export function restoreBackup(root: string): Promise<{ success: boolean; message?: string }> {
  return callPython("backup/restore", { root });
}

/** Delete the backup directory and keep the translated files. */
export function discardBackup(root: string): Promise<{ success: boolean; message?: string }> {
  return callPython("backup/discard", { root });
}

/** Create backups for the specified relative file paths. */
export function createBackup(root: string, files: string[]): Promise<{ success: boolean; backed_up: number }> {
  return callPython("backup/create", { root, files });
}

/** Export all translation files to a ZIP archive in the game folder. */
export function exportTranslationZip(root: string, engine: string, target_lang: string): Promise<{ success: boolean; zip_path: string; zip_name: string; file_count: number; message?: string }> {
  return callPython("project/export_zip", { root, engine, target_lang });
}

/** Pause the translation loop in the sidecar. */
export function pauseTranslation(): Promise<{ ok: boolean }> {
  return callPython("pause");
}

/** Resume the translation loop in the sidecar. */
export function resumeTranslation(): Promise<{ ok: boolean }> {
  return callPython("resume");
}

export interface TranslatePythonReq {
  root: string;
  api_key: string;
  /** All keys feed the worker pool (threads × keys) with failover. */
  api_keys?: string[];
  model: string;
  base_url?: string | null;
  /** Provider id — decides the wire format (gemini vs OpenAI-compatible), so a
   *  proxy URL isn't mistaken for an OpenAI endpoint. */
  provider?: string;
  target_lang?: string;
  dry_run?: boolean;
  threads?: number;
  /** Per-key pacing seconds (derived from RPM); 0 = none. */
  delay_seconds?: number;
  /** No-API: apply inline-Python translations from the cache only. Used by the
   *  writeBack path so "Write translation" lays down inline-Python from a prior
   *  full run without spending any API quota. */
  apply_cached_only?: boolean;
}

/** Streaming logs from the Ren'Py inline python translation tool. */
export async function translateRenpyPython(
  req: TranslatePythonReq,
  onLog: (line: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/renpy/translate_python`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.text().catch(() => "");
    throw new Error(`Ren'Py python translation failed (${res.status}): ${detail}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      onLog(buffer.slice(0, nl));
      buffer = buffer.slice(nl + 1);
    }
  }
  if (buffer.trim()) {
    onLog(buffer);
  }
}

export async function validateTranslation(engine: string, root: string, targetLang?: string, files?: string[]): Promise<{ errors: { file: string; line: number | null; message: string; severity: string }[]; count: number }> {
  const res = await callPython("validate", { engine, root, target_lang: targetLang ?? "", files: files ?? [] });
  return res as any;
}

export async function autofixTranslation(
  engine: string, root: string, apiKey: string, model: string, baseUrl: string, targetLang?: string
): Promise<{ fixed: number; log: string[]; rounds: number }> {
  const res = await callPython("autofix", { engine, root, api_key: apiKey, model, base_url: baseUrl, target_lang: targetLang ?? "" });
  return res as any;
}

// --- Ren'Py text-overflow risk + engine-lint (measure, don't guess) ----------

export interface RenpyRiskReport {
  dialogue_overflow_risk: "none" | "low" | "high" | "unknown";
  dialogue_reason: string;
  say_lines: number;
  long_say_lines: number;
  longest_say_chars: number;
  textbox_height: string;
  textbox_height_fixed: boolean;
  auto_height_dialogue: boolean;
  has_dialogue_scroll: boolean;
}

/** Static (no engine run) overflow-risk report for a Ren'Py game. */
export function renpyRisk(root: string): Promise<RenpyRiskReport> {
  return callPython("renpy/risk", { root }) as Promise<RenpyRiskReport>;
}

export interface RenpyLintFinding {
  file: string;
  line: number;
  message: string;
  actionable: boolean;
}
export interface RenpyLintResult {
  available: boolean;
  ours: RenpyLintFinding[];
  ours_count: number;
  actionable_count: number;
  other_count: number;
  reason: string;
}

/** Run the game's own Ren'Py engine `lint` over our injected tl/ files. */
export function renpyLint(root: string, lang?: string): Promise<RenpyLintResult> {
  return callPython("renpy/lint", { root, lang: lang ?? null }) as Promise<RenpyLintResult>;
}
