use std::sync::Mutex;
use std::process::{Child, Command};
use tauri::{AppHandle, Manager, RunEvent};

#[cfg(target_os = "windows")]
use windows::Win32::Foundation::HANDLE;
#[cfg(target_os = "windows")]
use windows::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
    JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};

// The Python sidecar binary is embedded at compile time.
// Build order: run build-sidecar.bat first, then tauri build.
const SIDECAR_BYTES: &[u8] = include_bytes!("../../python-core/dist/sidecar.exe");

struct SidecarState(Mutex<Option<Child>>);

// Holds the Windows Job Object handle for the whole app lifetime. The sidecar is
// assigned to this job with KILL_ON_JOB_CLOSE, so the OS terminates the sidecar's
// ENTIRE process tree the instant the last handle to the job closes — which the
// kernel does automatically when the Tauri process exits by ANY means (clean
// quit, panic, crash, or "End task" in Task Manager). This is what kills the
// orphaned-sidecar / "port 8723 already in use" / "Failed to fetch on next
// launch" class of bugs: we no longer depend on a Python-side watchdog guessing
// whether its parent is alive, nor on child.kill() reaching a grandchild process
// (PyInstaller onefile runs sidecar.exe as bootloader -> real python, and
// child.kill() only hits the bootloader). We never close this handle explicitly;
// process exit does, and that is precisely the trigger we want.
// The HANDLE is held purely to keep the job open for the app's lifetime; it is
// never read back, only dropped at process exit. That drop is the whole point.
#[cfg(target_os = "windows")]
struct JobHandle(#[allow(dead_code)] HANDLE);
// HANDLE is a raw pointer; it is safe to move across threads here because we only
// ever hold it (to keep the job open) and never dereference it concurrently.
#[cfg(target_os = "windows")]
unsafe impl Send for JobHandle {}
#[cfg(target_os = "windows")]
struct JobState(Mutex<Option<JobHandle>>);

/// Create a Job Object with KILL_ON_JOB_CLOSE and assign the sidecar process to
/// it. Returns the job handle to keep alive for the app's lifetime. On any
/// failure we return None and fall back to best-effort child.kill() — the app
/// still works, it just loses the OS-level cleanup guarantee.
#[cfg(target_os = "windows")]
fn assign_to_job(child: &Child) -> Option<JobHandle> {
    use std::os::windows::io::AsRawHandle;
    unsafe {
        let job = CreateJobObjectW(None, windows::core::PCWSTR::null()).ok()?;

        let mut info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &info as *const _ as *const core::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .ok()?;

        let child_handle = HANDLE(child.as_raw_handle() as _);
        AssignProcessToJobObject(job, child_handle).ok()?;
        Some(JobHandle(job))
    }
}

/// Extract sidecar.exe to %TEMP%\interprex\ (only if changed), then launch it.
fn start_sidecar(app: &AppHandle) {
    let dir = std::env::temp_dir().join("interprex");
    std::fs::create_dir_all(&dir).ok();
    let path = dir.join("sidecar.exe");

    // Kill any existing sidecar.exe processes to release file locks and free socket port 8723.
    // With the Job Object below this should rarely matter (the OS already reaps a
    // dead app's sidecar), but a previous run from BEFORE this build, or a crash
    // mid-extraction, can still leave one — so we keep this as a cheap safety net.
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        let _ = Command::new("taskkill")
            .args(&["/F", "/IM", "sidecar.exe"])
            .creation_flags(0x08000000) // CREATE_NO_WINDOW
            .status();
        // Give OS a tiny moment to release socket and file locks
        std::thread::sleep(std::time::Duration::from_millis(150));
    }

    // Always overwrite the sidecar to ensure the latest compiled python code is executed.
    let _ = std::fs::remove_file(&path);
    if let Err(e) = std::fs::write(&path, SIDECAR_BYTES) {
        eprintln!("[sidecar] failed to extract sidecar: {e}");
    }

    match Command::new(&path).spawn() {
        Ok(child) => {
            // Bind the sidecar to a kill-on-close Job Object BEFORE we lose the
            // child handle to state. If this fails we still store the child so
            // ExitRequested's child.kill() can do its best.
            #[cfg(target_os = "windows")]
            {
                match assign_to_job(&child) {
                    Some(job) => {
                        *app.state::<JobState>().0.lock().unwrap() = Some(job);
                        println!("[sidecar] bound to job object (kill-on-close)");
                    }
                    None => eprintln!("[sidecar] WARNING: could not bind to job object; relying on child.kill() only"),
                }
            }
            *app.state::<SidecarState>().0.lock().unwrap() = Some(child);
            println!("[sidecar] started from {:?}", path);
        }
        Err(e) => eprintln!("[sidecar] failed to start: {e}"),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_updater::init())
        .plugin(tauri_plugin_process::init())
        .manage(SidecarState(Mutex::new(None)));

    #[cfg(target_os = "windows")]
    let builder = builder.manage(JobState(Mutex::new(None)));

    builder
        .setup(|app| {
            start_sidecar(app.handle());
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let RunEvent::ExitRequested { .. } = event {
                // Graceful path: ask the child to stop. The Job Object is the hard
                // guarantee — when this process exits, the OS closes the job handle
                // and reaps the whole sidecar tree even if this kill missed.
                let child = app.state::<SidecarState>().0.lock().unwrap().take();
                if let Some(mut child) = child {
                    let _ = child.kill();
                    println!("[sidecar] stopped");
                }
            }
        });
}
