// ============================================================================
// Tiny i18n layer for Interprex's OWN interface. No external library — for a
// desktop tool with a fixed string set that's all overhead.
//
// Two language axes exist in this app, kept deliberately separate:
//   1. UI language   — the language of Interprex itself (this file).
//   2. target language — the language games get translated INTO (TARGET_LANGS
//      below + passed to the LLM). They share nothing on purpose.
//
// Adding a UI language = one new locale file + one line in `locales`. The
// `Strings` type forces it to have every key, so you can't ship a half-locale.
// ============================================================================

import { useSyncExternalStore } from "react";
import en from "./en";
import ru from "./ru";
import { loadSetting, saveSetting } from "../lib/settings";

/** The shape every locale must satisfy — derived from English, the source of
 *  truth. Miss a key in another locale and TypeScript fails the build. */
export type Strings = typeof en;
export type StringKey = keyof Strings;

export type UiLang = "en" | "ru";

/** Registered UI locales. Add a language here and in UI_LANGUAGES — done. */
export const locales: Record<UiLang, Strings> = {
  en,
  ru,
};

export const UI_LANGUAGES: { code: UiLang; label: string }[] = [
  { code: "en", label: "English" },
  { code: "ru", label: "Русский" },
];

/** Languages games can be translated INTO. Separate axis from UI language. */
export const TARGET_LANGS = [
  "Russian",
  "English",
  "Spanish",
  "German",
  "French",
  "Japanese",
  "Chinese (Simplified)",
  "Korean",
  "Portuguese (Brazil)",
] as const;
export type TargetLang = (typeof TARGET_LANGS)[number];

// --- reactive current-locale store -----------------------------------------

/** Restore the saved UI language, falling back to en for unknown/absent.
 *  Checks own locale keys (not `in`, which would accept Object.prototype keys
 *  like "toString"). */
function initialLang(): UiLang {
  const saved = loadSetting("uiLang", "en");
  return Object.prototype.hasOwnProperty.call(locales, saved)
    ? (saved as UiLang)
    : "en";
}

let current: UiLang = initialLang();
const subscribers = new Set<() => void>();

function notify() {
  for (const fn of subscribers) fn();
}

export function setUiLang(lang: UiLang) {
  if (lang === current) return;
  current = lang;
  saveSetting("uiLang", lang);
  notify();
}

export function getUiLang(): UiLang {
  return current;
}

/** Translate a key. Values are either strings or functions (for interpolation,
 *  e.g. wroteBack(n)). */
export function t<K extends StringKey>(key: K): Strings[K] {
  return locales[current][key];
}

/** React hook: re-renders the component when the UI language changes, and
 *  returns a `t` bound to the current locale. */
export function useT(): { t: typeof t; lang: UiLang } {
  const lang = useSyncExternalStore(
    (cb) => {
      subscribers.add(cb);
      return () => subscribers.delete(cb);
    },
    getUiLang,
    getUiLang,
  );
  return { t, lang };
}
