import { useEffect, useState, useCallback, useRef } from "react";
import { check } from "@tauri-apps/plugin-updater";
import { relaunch } from "@tauri-apps/plugin-process";
import { useT } from "./i18n";

type UpdateState =
  | { kind: "downloading"; version: string; downloaded: number; total: number }
  | { kind: "ready" }
  | null;

export default function UpdateOverlay({
  onStateChange,
}: {
  onStateChange: (busy: boolean) => void;
}) {
  const { t } = useT();
  const [state, setState] = useState<UpdateState>(null);
  // Guards against duplicate downloads: the effect below re-fires whenever
  // `run`'s identity changes (which happens whenever the parent re-renders and
  // hands us a fresh `onStateChange` closure). Without this, a re-render that
  // lands *after* a download has already started would schedule a SECOND
  // parallel check()+downloadAndInstall() — two independent downloads both
  // writing to the same `state`, which is what made the progress bar jump
  // back and forth. This ref makes the actual work run at most once per
  // mount, no matter how many times the effect/timer re-fires.
  const startedRef = useRef(false);

  const run = useCallback(async () => {
    if (startedRef.current) return;
    startedRef.current = true;
    try {
      const update = await check();
      if (!update) return;

      const version = update.version ?? "?";
      setState({ kind: "downloading", version, downloaded: 0, total: 0 });
      onStateChange(true);

      let downloaded = 0;
      let total = 0;

      await update.downloadAndInstall((event) => {
        if (event.event === "Started") {
          total = event.data.contentLength ?? 0;
          setState({ kind: "downloading", version, downloaded: 0, total });
        } else if (event.event === "Progress") {
          downloaded += event.data.chunkLength;
          setState({ kind: "downloading", version, downloaded, total });
        }
      });

      setState({ kind: "ready" });
      await relaunch();
    } catch (err) {
      console.error("[updater] check/download/install failed:", err);
      setState(null);
      onStateChange(false);
    }
  }, [onStateChange]);

  useEffect(() => {
    const timer = setTimeout(run, 500);
    return () => clearTimeout(timer);
  }, [run]);

  if (!state) return null;

  const formatMB = (bytes: number) =>
    state.kind === "downloading" && state.total > 0
      ? (bytes / (1024 * 1024)).toFixed(1)
      : "0";

  return (
    <div className="update-overlay">
      <div className="update-card">
        {state.kind === "downloading" && (
          <>
            <div className="update-spinner" />
            <div className="update-text">
              {(t("updateDownloading") as string).replace("{version}", state.version)}
            </div>
            {state.total > 0 && (
              <>
                <div className="update-progressbar">
                  <div
                    className="update-progressfill"
                    style={{
                      width: `${Math.min(100, Math.round((state.downloaded / state.total) * 100))}%`,
                    }}
                  />
                </div>
                <div className="update-progress-text">
                  {(t("updateProgress") as string)
                    .replace("{downloaded}", formatMB(state.downloaded))
                    .replace("{total}", formatMB(state.total))}
                </div>
              </>
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
