// Persisted user preferences (UI language, target language). Stored in the
// webview's localStorage — survives restarts, no fs round-trip needed for a
// couple of small keys. One module so every persisted setting has a single
// home and a typed accessor.

const KEYS = {
  uiLang: "interprex.uiLang",
  targetLang: "interprex.targetLang",
  provider: "interprex.provider",
  providerBaseUrl: "interprex.providerBaseUrl",
  providerApiKey: "interprex.providerApiKey",
  providerApiKey2: "interprex.providerApiKey2",
  providerModel: "interprex.providerModel",
  providerThreads: "interprex.providerThreads",
  providerRpm: "interprex.providerRpm",
  maxBatchSize: "interprex.maxBatchSize",
  openrouterFreeOnly: "interprex.openrouterFreeOnly",
  // OpenRouter daily free-request counter. The API doesn't report requests
  // spent today, so we count locally: usageCount ticks per request that reached
  // the server (errors included — they still burn the daily quota), reset when
  // usageDate (UTC YYYY-MM-DD) rolls over.
  openrouterUsageDate: "interprex.openrouterUsageDate",
  openrouterUsageCount: "interprex.openrouterUsageCount",
  lastFolderMode: "interprex.lastFolderMode",
  lastFolder: "interprex.lastFolder",
  // Proxy URL, stored globally (not per-provider): after an autocheck a direct
  // provider's base_url is cleared, so the URL itself must persist here.
  proxyUrl: "interprex.proxyUrl",
} as const;

/** Read a stored string, or `fallback` if absent / storage unavailable. */
export function loadSetting(key: keyof typeof KEYS, fallback: string): string {
  try {
    return localStorage.getItem(KEYS[key]) ?? fallback;
  } catch {
    return fallback; // storage can be disabled; never crash over a preference
  }
}

export function saveSetting(key: keyof typeof KEYS, value: string): void {
  try {
    localStorage.setItem(KEYS[key], value);
  } catch {
    /* ignore — a lost preference is not worth surfacing */
  }
}

// Connection settings (server URL, API key, model) are PER PROVIDER: Ollama's
// URL, Kaggle's ngrok URL, and Gemini's key are different things, so storing
// them in one slot would mean switching provider wipes the other's value. These
// helpers namespace the key by provider id so each backend remembers its own,
// across restarts.
type ProviderKey =
  | "providerBaseUrl"
  | "providerApiKey"
  | "providerApiKey2"
  | "providerModel"
  | "providerThreads"
  | "providerRpm";

export function loadProviderSetting(
  key: ProviderKey,
  provider: string,
  fallback: string,
): string {
  try {
    return localStorage.getItem(`${KEYS[key]}.${provider}`) ?? fallback;
  } catch {
    return fallback;
  }
}

export function saveProviderSetting(
  key: ProviderKey,
  provider: string,
  value: string,
): void {
  try {
    localStorage.setItem(`${KEYS[key]}.${provider}`, value);
  } catch {
    /* ignore */
  }
}

// API keys are a LIST per provider now (cloud backends can rotate across many:
// Gemini, OpenRouter, …). Stored as a JSON array under providerApiKey. Reading
// migrates the old single providerApiKey (plain string) + providerApiKey2 so a
// user's existing keys aren't lost when they upgrade. Always returns at least
// one entry (possibly "") so the UI renders one field.
export function loadProviderKeys(provider: string): string[] {
  try {
    const raw = localStorage.getItem(`${KEYS.providerApiKey}.${provider}`);
    if (raw && raw.trim().startsWith("[")) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        const keys = arr.map((k) => String(k));
        return keys.length ? keys : [""];
      }
    }
    // Legacy migration: plain-string providerApiKey (+ providerApiKey2).
    const legacy1 = raw ?? "";
    const legacy2 =
      localStorage.getItem(`${KEYS.providerApiKey2}.${provider}`) ?? "";
    const migrated = [legacy1, legacy2].filter(Boolean);
    return migrated.length ? migrated : [""];
  } catch {
    return [""];
  }
}

export function saveProviderKeys(provider: string, keys: string[]): void {
  try {
    localStorage.setItem(
      `${KEYS.providerApiKey}.${provider}`,
      JSON.stringify(keys),
    );
  } catch {
    /* ignore */
  }
}
