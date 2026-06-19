fn main() {
    println!("cargo:rerun-if-changed=../python-core/dist/sidecar.exe");
    tauri_build::build()
}
