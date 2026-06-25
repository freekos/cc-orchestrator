use std::process::Command;

// Bridge to the cc engine: run `cc snapshot --json` and hand the JSON to the frontend.
// Engine path via CC_ENGINE env (the cc wrapper / cc.py), default "cc" on PATH.
#[tauri::command]
fn get_state() -> Result<String, String> {
    let engine = std::env::var("CC_ENGINE").unwrap_or_else(|_| "cc".to_string());
    let output = Command::new(&engine)
        .args(["snapshot", "--json"])
        .output()
        .map_err(|e| format!("cannot run engine '{}': {}", engine, e))?;
    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).into_owned())
    } else {
        Err(String::from_utf8_lossy(&output.stderr).into_owned())
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![get_state])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
