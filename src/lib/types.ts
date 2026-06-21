// ============================================================================
// Interprex — core data contract.
//
// THIS FILE IS THE FOUNDATION. Everything talks through these shapes:
//   parser  ──TranslationString[]──▶  LLM  ──translations──▶  parser writes back
//
// The parser knows nothing about the LLM. The LLM knows nothing about engine
// file formats. They only ever exchange TranslationString. Keep that wall up
// and a new engine never touches translation code; a new LLM never touches
// parsers.
//
// Change `id` generation or the `path` shape and you invalidate every saved
// project. Treat the four "bedrock" pieces below as load-bearing.
// ============================================================================

/** Engines Interprex extracts from. Add new ones here; keep the string stable
 *  once shipped — it is persisted in project files and used in ids. */
export type Engine =
  | "rpgmaker"
  | "renpy"
  | "godot"
  | "gamemaker"
  | "unity"
  | "unreal"
  | "unreal4_5"
  | "csharp"
  | "i18n"
  | "fusion"
  | "mmf2"
  | "qsp"
  | "twine";

/**
 * One translatable unit of text, as produced by a parser's extract().
 *
 * BEDROCK #1 — `path`. The address of this string *inside* its file, deepest
 * last, e.g. ["events", "12", "pages", "0", "list", "3", "parameters", "0"].
 * It is three things at once:
 *   - human context for the LLM ("where in the game is this"),
 *   - the write-back address so inject() can put the translation back exactly,
 *   - part of the stable id.
 * Drop it and you cannot write translations back. Never replace it with a bare
 * line number.
 */
export interface TranslationString {
  /** Stable across runs. See makeId(). Do NOT use array index / line number. */
  id: string;
  /** Source text, verbatim. */
  original: string;
  /** Surrounding context for the LLM: speaker name, file kind, scene, etc. */
  context: string;
  /** Path of the source file relative to the project root (forward slashes). */
  file: string;
  /** Address inside the file. See BEDROCK #1 above. */
  path: string[];
  /** Which engine produced this. */
  engine: Engine;
}

/** A finished translation for one string id. */
export interface TranslationEntry {
  original: string;
  /** Empty until translated. */
  translated: string;
  /** Reviewed/locked by a human — never auto-overwritten once true. */
  approved: boolean;
}

/**
 * BEDROCK #3 — the on-disk project file, saved next to the game so nothing is
 * ever re-translated. `version` exists from day one so a future schema change
 * migrates old projects instead of breaking them. Bump it + add a migration;
 * never read a project whose version you don't recognise.
 */
export interface ProjectFile {
  /** Schema version. Bump on any breaking change to this shape. */
  version: 1;
  engine: Engine;
  /** Absolute path to the game/project root this file describes. */
  root: string;
  /** id -> translation. Keyed by id so reordering source files is harmless. */
  strings: Record<string, TranslationEntry>;
  /** Source term -> preferred translation (names, items). Fed to every LLM call. */
  glossary: Record<string, string>;
  /**
   * Font style for the non-Latin font swapped into the game (Ren'Py): "smooth"
   * (Noto, the default) or "pixel" (bitmap font matching pixel-art games).
   * Stored PER game folder — a new folder defaults to smooth, but once the user
   * picks pixel here it sticks for this folder. Optional/additive: projects
   * saved before this field load fine and read as "smooth".
   */
  fontStyle?: FontStyle;
}

/** Which bundled font family inject swaps in for non-Latin scripts. */
export type FontStyle = "smooth" | "pixel";

export const PROJECT_VERSION = 1 as const;

/** Folder Interprex writes its data into, inside the game folder. Keeps the
 *  game root clean — project + caches all live here instead of as dotfiles. */
export const INTERPREX_DIR = "Interprex";

/** Project filename, now inside INTERPREX_DIR (was a `.interprex.json` dotfile
 *  in the root pre-2026-06; old dotfiles are NOT migrated — re-translate). */
export const PROJECT_FILENAME = "project.json";

// ----------------------------------------------------------------------------
// BEDROCK #2 — stable id.
//
// id = hash(engine + file + path + original).
//
// Why each ingredient:
//   - file+path : the string's true location, so inserting a neighbouring
//                 string does NOT shift this one's id (a line-number id would
//                 shift and destroy your translation memory).
//   - original  : same slot but the source text changed -> new id, so a stale
//                 translation can't silently attach to edited source.
//   - engine    : avoids collisions if one folder is ever read two ways.
//
// Same input -> same id, every run, forever. That is the whole point.
// FNV-1a (32-bit), hex. Tiny, dependency-free, deterministic, good enough for
// content addressing (we are not defending against adversaries here).
// ----------------------------------------------------------------------------
export function makeId(s: {
  engine: Engine;
  file: string;
  path: string[];
  original: string;
}): string {
  const key = s.engine + "\x00" + s.file + "\x00" + s.path.join("\x01") + "\x00" + s.original;
  let h = 0x811c9dc5; // FNV offset basis
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    // 32-bit FNV prime multiply via shifts, kept in unsigned range.
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** Fresh, empty project for a freshly-detected game. */
export function emptyProject(engine: Engine, root: string): ProjectFile {
  return { version: PROJECT_VERSION, engine, root, strings: {}, glossary: {} };
}
