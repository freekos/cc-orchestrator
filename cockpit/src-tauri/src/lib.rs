use std::process::Command;

// Bridge to the cc engine: `cc snapshot --json` -> JSON for the frontend. Engine via CC_ENGINE.
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

// Open a URL (MR link) in the browser or a folder (task worktree, for Claude Code/Codex) in Finder.
#[tauri::command]
fn open_external(target: String) -> Result<(), String> {
    Command::new("open").arg(&target).spawn().map(|_| ()).map_err(|e| e.to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![get_state, open_external])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
