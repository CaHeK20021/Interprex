// Themed in-app folder browser. The native OS dialog can't be restyled, so this
// replaces it: it walks the filesystem through the sidecar (listDir) and lets
// the user pick a directory in the app's own look. Built to degrade gracefully —
// a locked/empty/vanished folder shows an empty list, never a dead end.

import { useEffect, useRef, useState } from "react";
import { listDir, homeDir, fsShortcuts, type FsEntry, type FsShortcut } from "./lib/ipc";
import { useT } from "./i18n";

// Launcher logos as inline SVG (no asset files → nothing extra to bundle). Keyed
// by a prefix of the shortcut name the sidecar returns ("Steam (C:)", "Epic
// Games", "GOG"). Falls back to a generic folder glyph for unknown launchers.
function launcherIcon(name: string) {
  const n = name.toLowerCase();
  if (n.startsWith("steam")) {
    return (
      <svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
        <path d="M8 0a8 8 0 0 0-7.9 6.9l4.3 1.8a2.26 2.26 0 0 1 1.3-.4l1.9-2.8v0a3 3 0 1 1 3 3h-.1l-2.7 2a2.27 2.27 0 0 1-4.5.3L.2 9.8A8 8 0 1 0 8 0z"/>
        <circle cx="11" cy="5" r="1.6" fill="var(--panel)"/>
      </svg>
    );
  }
  if (n.startsWith("epic")) {
    return (
      <svg width="13" height="15" viewBox="0 0 14 16" fill="currentColor" aria-hidden>
        <path d="M2 0h10a2 2 0 0 1 2 2v9.5L7 16 0 11.5V2a2 2 0 0 1 2-2z" opacity="0.9"/>
        <path d="M4.7 3.2h4.6v1.3H6.2v1.6h2.6v1.3H6.2v1.8h3.1v1.3H4.7z" fill="var(--panel)"/>
      </svg>
    );
  }
  if (n.startsWith("gog")) {
    return (
      <svg width="15" height="15" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
        <circle cx="8" cy="8" r="7.3" opacity="0.9"/>
        <path d="M8 4.4a3.6 3.6 0 1 0 0 7.2 3.6 3.6 0 0 0 0-7.2zm0 1.6a2 2 0 1 1 0 4 2 2 0 0 1 0-4z" fill="var(--panel)"/>
      </svg>
    );
  }
  return <span className="fp-ico">📁</span>;
}

interface Props {
  /** Engine accent class for theming the modal (e.g. "engine-renpy"). */
  engineClass?: string;
  /** Where to start (last-used path); falls back to home, then drives. */
  startPath?: string;
  onPick: (path: string) => void;
  onClose: () => void;
  remember?: boolean;
  onRememberChange?: (val: boolean) => void;
  rememberTooltip?: string;
}

export default function FolderPicker({ engineClass, startPath, onPick, onClose, remember, onRememberChange, rememberTooltip }: Props) {
  const { t } = useT();
  const [cwd, setCwd] = useState(""); // "" = drive list
  const [parent, setParent] = useState<string | null>(null);
  const [isRoot, setIsRoot] = useState(true);
  const [entries, setEntries] = useState<FsEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [manual, setManual] = useState("");
  const [shortcuts, setShortcuts] = useState<FsShortcut[]>([]);
  const [homePath, setHomePath] = useState("");
  const [drives, setDrives] = useState<FsEntry[]>([]);
  const listRef = useRef<HTMLDivElement>(null);
  const pastedRef = useRef(false);

  // Load a directory. An invalid path makes the sidecar fall back to the drive
  // list, so we just reflect whatever it returns.
  // Esc closes; Backspace navigates up if not typing in an input field; Enter on the manual field navigates to it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
      } else if (e.key === "Backspace") {
        const active = document.activeElement;
        const isTyping = active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA");
        if (!isTyping && parent !== null) {
          go(parent);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, parent]);

  // Load a directory. An invalid path makes the sidecar fall back to the drive
  // list, so we just reflect whatever it returns.
  async function go(path: string) {
    let targetPath = path.trim();
    const lower = targetPath.toLowerCase();
    if (lower === "steam") {
      targetPath = "steam_games_library";
    } else if (lower === "epic games" || lower === "epic") {
      targetPath = "epic_games_library";
    }

    setLoading(true);
    try {
      const r = await listDir(targetPath);
      setCwd(r.path);
      setParent(r.parent);
      setIsRoot(r.is_root);
      setEntries(r.entries);
      
      if (r.path === "steam_games_library") {
        setManual("Steam");
      } else if (r.path === "epic_games_library") {
        setManual("Epic Games");
      } else {
        setManual(r.path);
      }
      
      if (listRef.current) listRef.current.scrollTop = 0;
    } catch {
      // Total failure (sidecar down): show an empty drive list rather than crash.
      setEntries([]);
      setIsRoot(true);
      setCwd("");
      setParent(null);
      setManual("");
    } finally {
      setLoading(false);
    }
  }

  // Checks if the given path is active (case-insensitive and ignore trailing slashes)
  function isPathActive(targetPath: string) {
    if (!cwd && !targetPath) return true;
    if (!cwd || !targetPath) return false;
    const cleanCwd = cwd.replace(/\/$/, "").toLowerCase();
    const cleanTarget = targetPath.replace(/\/$/, "").toLowerCase();
    return cleanCwd === cleanTarget;
  }

  // Initial: start at the provided path, else home, else drives.
  useEffect(() => {
    (async () => {
      let start = startPath || "";
      try {
        const home = await homeDir();
        setHomePath(home.path);
        if (!start) {
          start = home.path;
        }
      } catch {
        if (!start) start = "";
      }
      await go(start);
    })();
    // Fetch drives on mount
    listDir("")
      .then((r) => {
        if (r.is_root) {
          setDrives(r.entries);
        }
      })
      .catch(() => {});
    // Game-launcher quick-jumps (Steam first, then Epic, then others — the
    // sidecar already returns them in that priority order).
    fsShortcuts()
      .then((r) => setShortcuts(r.shortcuts))
      .catch(() => setShortcuts([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const canChoose = !!cwd && !isRoot && cwd !== "epic_games_library" && cwd !== "steam_games_library";

  return (
    <div className="fp-overlay" onClick={onClose}>
      <div
        className={`fp-modal ${engineClass || "engine-none"}`}
        onClick={(e) => e.stopPropagation()}
      >
        {/* launcher quick-jumps — only rendered when something was detected */}
        {shortcuts.length > 0 && (
          <div className="fp-shortcuts">
            {shortcuts.map((s) => (
              <button
                key={s.path}
                className="fp-shortcut"
                onClick={() => go(s.path)}
                title={`${s.name} — ${s.path}`}
              >
                {launcherIcon(s.name)}
                <span className="fp-shortcut-name">{s.name}</span>
              </button>
            ))}
          </div>
        )}

        <div className="fp-head">
          <span className="fp-title">{t("fpTitle")}</span>
          <div className="fp-path-input-wrap">
            <input
              value={manual}
              placeholder={t("fpPathPlaceholder") as string}
              onChange={(e) => {
                setManual(e.target.value);
                if (pastedRef.current) {
                  pastedRef.current = false;
                  setTimeout(() => go(e.target.value.trim()), 0);
                }
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") go(manual.trim());
              }}
              onPaste={() => {
                pastedRef.current = true;
              }}
              onFocus={(e) => e.target.select()}
            />
          </div>
          <button className="fp-x" onClick={onClose} title={t("fpCancel") as string}>
            ×
          </button>
        </div>

        {/* main body flex wrapper */}
        <div className="fp-main-layout">
          {/* sidebar */}
          <div className="fp-sidebar">
            <button
              className={`fp-sidebar-item ${isPathActive("") ? "active" : ""}`}
              onClick={() => go("")}
              disabled={loading}
              title={t("fpSidebarThisPC") as string}
            >
              <span className="fp-sidebar-icon">💻</span>
              <span className="fp-sidebar-text">{t("fpSidebarThisPC") as string}</span>
            </button>
            {homePath && (
              <>
                <button
                  className={`fp-sidebar-item ${isPathActive(`${homePath}/Desktop`) ? "active" : ""}`}
                  onClick={() => go(`${homePath}/Desktop`)}
                  disabled={loading}
                  title={t("fpSidebarDesktop") as string}
                >
                  <span className="fp-sidebar-icon">🖼️</span>
                  <span className="fp-sidebar-text">{t("fpSidebarDesktop") as string}</span>
                </button>
                <button
                  className={`fp-sidebar-item ${isPathActive(`${homePath}/Documents`) ? "active" : ""}`}
                  onClick={() => go(`${homePath}/Documents`)}
                  disabled={loading}
                  title={t("fpSidebarDocuments") as string}
                >
                  <span className="fp-sidebar-icon">📄</span>
                  <span className="fp-sidebar-text">{t("fpSidebarDocuments") as string}</span>
                </button>
                <button
                  className={`fp-sidebar-item ${isPathActive(`${homePath}/Downloads`) ? "active" : ""}`}
                  onClick={() => go(`${homePath}/Downloads`)}
                  disabled={loading}
                  title={t("fpSidebarDownloads") as string}
                >
                  <span className="fp-sidebar-icon">📥</span>
                  <span className="fp-sidebar-text">{t("fpSidebarDownloads") as string}</span>
                </button>
              </>
            )}
            {drives.map((d) => (
              <button
                key={d.path}
                className={`fp-sidebar-item ${isPathActive(d.path) ? "active" : ""}`}
                onClick={() => go(d.path)}
                disabled={loading}
                title={t("fpSidebarLocalDisk")(d.name) as string}
              >
                <span className="fp-sidebar-icon">🖴</span>
                <span className="fp-sidebar-text">{t("fpSidebarLocalDisk")(d.name) as string}</span>
              </button>
            ))}
          </div>

          {/* listing */}
          <div className="fp-list" ref={listRef}>
            {loading ? (
              <div className="fp-empty">{t("fpLoading")}</div>
            ) : entries.length === 0 ? (
              <div className="fp-empty">{isRoot ? t("fpNoDrives") : t("fpEmpty")}</div>
            ) : (
              entries.map((e) => (
                <button
                  key={e.path}
                  className="fp-row"
                  onDoubleClick={() => go(e.path)}
                  onClick={(ev) => {
                    // single click selects (navigates into) — folders only here
                    if (ev.detail === 1) go(e.path);
                  }}
                  title={e.path}
                >
                  <span className="fp-ico">{isRoot ? "🖴" : "📁"}</span>
                  <span className="fp-name">{e.name}</span>
                </button>
              ))
            )}
          </div>
        </div>

        {/* actions */}
        <div className="fp-actions">
          {onRememberChange !== undefined && (
            <label className="fp-remember" title={rememberTooltip}>
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => onRememberChange(e.target.checked)}
              />
              <span>{t("fpRemember") as string}</span>
            </label>
          )}
          <button className="btn-secondary" onClick={onClose}>
            {t("fpCancel")}
          </button>
          <button
            className={`fp-choose ${engineClass || "engine-none"}`}
            onClick={() => canChoose && onPick(cwd)}
            disabled={!canChoose}
            title={canChoose ? cwd : (t("fpChooseHint") as string)}
          >
            {t("fpChoose")}
          </button>
        </div>
      </div>
    </div>
  );
}
