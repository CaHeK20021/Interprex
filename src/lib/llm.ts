// ============================================================================
// The ONE place the frontend asks for a translation. It no longer calls a model
// directly — the Python sidecar owns provider HTTP (keeps API keys out of the
// browser and dodges cloud CORS). This file just shapes the request and routes
// it through ipc. Swap providers, batching, prompts -> all server-side now;
// the rest of the app still only knows "give me strings, get back translations".
// ============================================================================

import type { TranslationString } from "./types";
import {
  translateViaSidecar,
  type TranslateProgress,
  type TranslateResult,
} from "./ipc";

/** Which backends the user can pick. Mirrors the Python provider registry. */
export type ProviderId = "ollama" | "lmstudio" | "kaggle" | "gemini" | "openrouter";

// needsKey: an API key is required (cloud). needsUrl: a server URL is required
// (no usable default — local servers have a default, Kaggle's ngrok URL does
// not). Kaggle needs a URL AND optionally a key (vLLM started with --api-key).
export const PROVIDERS: {
  id: ProviderId;
  label: string;
  needsKey: boolean;
  needsUrl: boolean;
}[] = [
  { id: "ollama", label: "Ollama (local)", needsKey: false, needsUrl: false },
  { id: "lmstudio", label: "LM Studio (local)", needsKey: false, needsUrl: false },
  { id: "kaggle", label: "Kaggle (ngrok)", needsKey: false, needsUrl: true },
  { id: "gemini", label: "Google Gemini", needsKey: true, needsUrl: false },
  { id: "openrouter", label: "OpenRouter", needsKey: true, needsUrl: false },
];

/** Provider connection settings, supplied per call from the UI. */
export interface ProviderConfig {
  baseUrl?: string; // local servers; blank = provider default
  apiKey?: string; // cloud (legacy single key — kept for compatibility)
  apiKey2?: string; // second api key (legacy)
  apiKeys?: string[]; // cloud: any number of keys to rotate across
  model?: string;
}

export interface TranslateOptions {
  provider: ProviderId;
  config: ProviderConfig;
  targetLang: string;
  glossary: Record<string, string>;
  /** Model context window in tokens. Batches are packed to fit it so small-VRAM
   *  local models aren't silently truncated. 0 = sidecar default. */
  maxContextTokens: number;
  /** Maximum number of strings to send in a single API request batch. */
  maxBatchSize: number;
  /** Concurrent workers per API key (1..10). Total = threads * #keys. Cloud
   *  only; pass 1 for local providers. Default 1. */
  threads?: number;
  /** Minimum wall-clock seconds each request must occupy, to pace under a
   *  provider's per-minute limit. 0 = no pacing. */
  delaySeconds?: number;
  root?: string;
  /** Font style for UI-width fitting — measure against the same font inject will
   *  write ("smooth" Noto vs "pixel" bitmap). Ren'Py menu choices only. */
  fontStyle?: string;
}

/**
 * Translate a batch of strings. Returns the full sidecar result — the
 * id -> translation map PLUS any errors and whether the run aborted early (a
 * dead/killed LLM trips a circuit breaker server-side). Callers must check
 * `aborted`/`errors`, not assume the map is complete. Batching, prompt, and
 * glossary handling happen in the sidecar; this is a thin pass-through.
 * `onProgress`, if given, fires after each batch so the caller can show a bar.
 */
export async function translateBatch(
  strings: TranslationString[],
  opts: TranslateOptions,
  onProgress?: (p: TranslateProgress) => void,
  signal?: AbortSignal,
): Promise<TranslateResult> {
  if (!strings.length) return { translations: {}, errors: [], aborted: false };
  return translateViaSidecar(
    {
      provider: opts.provider,
      target_lang: opts.targetLang,
      glossary: opts.glossary,
      base_url: opts.config.baseUrl ?? "",
      api_key: opts.config.apiKey ?? "",
      api_key_2: opts.config.apiKey2 ?? "",
      api_keys: opts.config.apiKeys ?? [],
      model: opts.config.model ?? "",
      max_context_tokens: opts.maxContextTokens,
      max_batch_size: opts.maxBatchSize,
      threads: opts.threads ?? 1,
      delay_seconds: opts.delaySeconds ?? 0,
      root: opts.root ?? "",
      engine: strings[0]?.engine ?? "",
      font_style: opts.fontStyle ?? "smooth",
      items: strings.map((s) => ({
        id: s.id,
        text: s.original,
        context: s.context,
        file: s.file,
        path: s.path,
      })),
    },
    onProgress,
    signal,
  );
}

export type { TranslateProgress };
