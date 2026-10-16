import { useEffect, useState, useCallback, useRef } from "react";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { useT } from "./i18n";

export type UpdateState =
  | { kind: "downloading"; version: string; downloaded: number; total: number }
  | { kind: "ready" }
  | null;

export type AvailableUpdateInfo = {
  version: string;
  run: () => void;
};

export default function UpdateOverlay({
  onStateChange,
  isAppBusy,
  onUpdateAvailableChange,
}: {
  onStateChange: (busy: boolean) => void;
  isAppBusy?: boolean;
  onUpdateAvailableChange?: (update: AvailableUpdateInfo | null) => void;
}) {
  const { t } = useT();
  const [state, setState] = useState<UpdateState>(null);

  const isAppBusyRef = useRef(isAppBusy ?? false);
  isAppBusyRef.current = isAppBusy ?? false;

  const updateHandleRef = useRef<any>(null);
  const isCheckingRef = useRef(false);
  const isDownloadingRef = useRef(false);

  const onUpdateAvailableChangeRef = useRef(onUpdateAvailableChange);
  onUpdateAvailableChangeRef.current = onUpdateAvailableChange;

  const startDownload = useCallback(async () => {
    if (isDownloadingRef.current) return;
    if (isAppBusyRef.current) return;
    const update = updateHandleRef.current;
    if (!update) return;

    isDownloadingRef.current = true;
    onStateChange(true);
    try {
      const version = update.version ?? "?";
      setState({ kind: "downloading", version, downloaded: 0, total: 0 });

      let downloaded = 0;
      let total = 0;

      await update.downloadAndInstall((event: any) => {
        if (event.event === "Started") {
          total = event.data?.contentLength ?? 0;
          setState({ kind: "downloading", version, downloaded: 0, total });
        } else if (event.event === "Progress") {
          downloaded += event.data?.chunkLength ?? 0;
          setState({ kind: "downloading", version, downloaded, total });
        }
      });

      setState({ kind: "ready" });
      await relaunch();
    } catch (err) {
      console.error("[updater] check/download/install failed:", err);
      setState(null);
      onStateChange(false);
      isDownloadingRef.current = false;
    }
  }, [onStateChange]);

  const checkUpdate = useCallback(async (isLaunch: boolean = false) => {
    if (isCheckingRef.current || isDownloadingRef.current) return;
    isCheckingRef.current = true;
    try {
      const update = await check();
      if (update) {
        updateHandleRef.current = update;
        if (onUpdateAvailableChangeRef.current) {
          onUpdateAvailableChangeRef.current({
            version: update.version ?? "?",
            run: startDownload,
          });
        }
        // Auto-update on startup if app is not busy
        if (isLaunch && !isAppBusyRef.current) {
          startDownload();
        }
      } else {
        updateHandleRef.current = null;
        if (onUpdateAvailableChangeRef.current) {
          onUpdateAvailableChangeRef.current(null);
        }
      }
    } catch (err) {
      console.warn("[updater] check failed:", err);
    } finally {
      isCheckingRef.current = false;
    }
  }, [startDownload]);

  // Initial check on mount: auto-update on launch! Periodic check every 15 minutes.
  const hasMountedRef = useRef(false);
  useEffect(() => {
    if (!hasMountedRef.current) {
      hasMountedRef.current = true;
      checkUpdate(true);
    }
    const interval = setInterval(() => checkUpdate(false), 15 * 60 * 1000);
    return () => clearInterval(interval);
  }, [checkUpdate]);

  // When work in progress stops (busy transitions from true to false), re-check immediately.
  const prevBusyRef = useRef(isAppBusy ?? false);
  useEffect(() => {
    if (prevBusyRef.current && !isAppBusy) {
      checkUpdate(false);
    }
    prevBusyRef.current = isAppBusy ?? false;
  }, [isAppBusy, checkUpdate]);

  if (!state) return null;

  const formatMB = (bytes: number) =>
    state.kind === "downloading" && state.total > 0
      ? (bytes / (1024 * 1024)).toFixed(1)
      : "0";

  // Circular progress ring geometry. The arc length is controlled by
  // stroke-dashoffset: full circumference = empty, 0 = full.
  const RADIUS = 52;
  const STROKE = 6;
  const CIRC = 2 * Math.PI * RADIUS;
  const pct =
    state.kind === "downloading" && state.total > 0
      ? Math.min(1, state.downloaded / state.total)
      : 0;
  // When the total size isn't known yet (still connecting / first bytes), the
  // ring spins as an indeterminate loader instead of sitting empty at 0%.
  const indeterminate = state.kind === "downloading" && state.total === 0;

  return (
    <div className="update-overlay">
      <div className="update-card">
        {state.kind === "downloading" && (
          <>
            <div className={`update-ring${indeterminate ? " is-indeterminate" : ""}`}>
              <svg viewBox="0 0 120 120" className="update-ring-svg">
                <defs>
                  <linearGradient id="update-ring-grad" x1="0" y1="0" x2="1" y2="1">
                    <stop offset="0%" className="update-ring-grad-a" />
                    <stop offset="100%" className="update-ring-grad-b" />
                  </linearGradient>
                </defs>
                <circle
                  className="update-ring-track"
                  cx="60"
                  cy="60"
                  r={RADIUS}
                  strokeWidth={STROKE}
                  fill="none"
                />
                <circle
                  className="update-ring-arc"
                  cx="60"
                  cy="60"
                  r={RADIUS}
                  strokeWidth={STROKE}
                  fill="none"
                  strokeLinecap="round"
                  strokeDasharray={CIRC}
                  strokeDashoffset={
                    indeterminate ? CIRC * 0.7 : CIRC * (1 - pct)
                  }
                  transform="rotate(-90 60 60)"
                />
              </svg>
              <div className="update-ring-label">
                {indeterminate ? "" : `${Math.round(pct * 100)}%`}
              </div>
            </div>
            <div className="update-text">
              {(t("updateDownloading") as string).replace("{version}", state.version)}
            </div>
            {state.total > 0 && (
              <div className="update-progress-text">
                {(t("updateProgress") as string)
                  .replace("{downloaded}", formatMB(state.downloaded))
                  .replace("{total}", formatMB(state.total))}
              </div>
            )}
          </>
        )}
        {state.kind === "ready" && (
          <>
            <div className="update-checkmark" />
            <div className="update-text">{t("updateReady")}</div>
          </>
        )}
      </div>
    </div>
  );
}
