// Load/save the per-game project file (.interprex.json) that holds translation
// memory + glossary, so nothing is ever re-translated. Uses Tauri's fs plugin.

import { readTextFile, writeTextFile, exists, mkdir } from "@tauri-apps/plugin-fs";
import {
  type Engine,
  type ProjectFile,
  type TranslationString,
  INTERPREX_DIR,
  PROJECT_FILENAME,
  PROJECT_VERSION,
  emptyProject,
} from "./types";

function projectDir(root: string): string {
  return `${root.replace(/\\/g, "/")}/${INTERPREX_DIR}`;
}

function projectPath(root: string): string {
  return `${projectDir(root)}/${PROJECT_FILENAME}`;
}

export async function loadProject(
  root: string,
  engine: Engine,
): Promise<ProjectFile> {
  const path = projectPath(root);
  if (await exists(path)) {
    const raw = JSON.parse(await readTextFile(path)) as ProjectFile;
    if (raw.version !== PROJECT_VERSION) {
      // Future: migrate. For now refuse rather than corrupt.
      throw new Error(
        `project version ${raw.version} unsupported (expected ${PROJECT_VERSION})`,
      );
    }
    // Re-root to the folder we were actually opened from, NOT the absolute path
    // baked in when the file was first saved. This is what makes a project
    // portable: hand someone your .interprex.json (translations are keyed by id,
    // which uses only the file-relative path), they open their copy of the game,
    // and saves/inject land in THEIR folder instead of your old one.
    raw.root = root.replace(/\\/g, "/");
    return raw;
  }
  return emptyProject(engine, root.replace(/\\/g, "/"));
}

let activeSavePromise: Promise<void> | null = null;
let pendingSave: ProjectFile | null = null;

export async function saveProject(project: ProjectFile): Promise<void> {
  // If a save is already running, just queue this one as the next one to save.
  if (activeSavePromise !== null) {
    pendingSave = project;
    return activeSavePromise;
  }

  // Otherwise, start the save chain.
  let currentProject: ProjectFile | null = project;

  activeSavePromise = (async () => {
    try {
      while (currentProject !== null) {
        // Ensure the Interprex/ folder exists (recursive = no-op if present).
        await mkdir(projectDir(currentProject.root), { recursive: true });
        const final = projectPath(currentProject.root);
        await writeTextFile(final, JSON.stringify(currentProject, null, 2));

        // Grab the next pending save, if any.
        currentProject = pendingSave;
        pendingSave = null;
      }
    } finally {
      activeSavePromise = null;
      pendingSave = null;
    }
  })();

  return activeSavePromise;
}

export function isProjectSaving(): boolean {
  return activeSavePromise !== null || pendingSave !== null;
}

/** Force-clear stuck saving state (e.g. when a close attempt interrupted a write). */
export function resetSavingState(): void {
  activeSavePromise = null;
  pendingSave = null;
}


/** Merge freshly-extracted strings into the project, preserving existing
 *  translations + approved flags. New ids get a blank entry; vanished ids are
 *  dropped. This is the translation-memory step. */
export function mergeStrings(
  project: ProjectFile,
  strings: TranslationString[],
): ProjectFile {
  const next: ProjectFile["strings"] = {};
  for (const s of strings) {
    const prev = project.strings[s.id];
    next[s.id] = prev ?? {
      original: s.original,
      translated: "",
      approved: false,
    };
  }
  return { ...project, strings: next };
}
