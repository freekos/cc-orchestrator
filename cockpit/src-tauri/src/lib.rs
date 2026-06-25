use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::Mutex;
use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize};
use tauri::{AppHandle, Emitter, State};

#[derive(Default)]
struct Ptys {
    writers: Mutex<HashMap<String, Box<dyn Write + Send>>>,
    masters: Mutex<HashMap<String, Box<dyn MasterPty + Send>>>,
}

#[derive(Clone, serde::Serialize)]
struct PtyOut { id: String, data: Vec<u8> }

// --- engine bridge ---
#[tauri::command]
fn get_state() -> Result<String, String> {
    let engine = std::env::var("CC_ENGINE").unwrap_or_else(|_| "cc".to_string());
    let out = std::process::Command::new(&engine).args(["snapshot", "--json"]).output()
        .map_err(|e| format!("cannot run engine '{}': {}", engine, e))?;
    if out.status.success() { Ok(String::from_utf8_lossy(&out.stdout).into_owned()) }
    else { Err(String::from_utf8_lossy(&out.stderr).into_owned()) }
}

#[tauri::command]
fn open_external(target: String) -> Result<(), String> {
    std::process::Command::new("open").arg(&target).spawn().map(|_| ()).map_err(|e| e.to_string())
}

// Run an arbitrary cc engine command (action buttons: task mr/merge, epic ops/mr/merge).
#[tauri::command]
fn run_cc(args: Vec<String>) -> Result<String, String> {
    let engine = std::env::var("CC_ENGINE").unwrap_or_else(|_| "cc".to_string());
    let out = std::process::Command::new(&engine).args(&args).output().map_err(|e| e.to_string())?;
    let s = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    if out.status.success() { Ok(s) } else { Err(if s.trim().is_empty() { "(no output)".into() } else { s }) }
}

// --- embedded terminal (chat) via PTY: runs claude/codex in the task worktree ---
#[tauri::command]
fn pty_spawn(app: AppHandle, state: State<Ptys>, id: String, cwd: String, program: String) -> Result<(), String> {
    let pty = native_pty_system();
    let pair = pty.openpty(PtySize { rows: 30, cols: 100, pixel_width: 0, pixel_height: 0 })
        .map_err(|e| e.to_string())?;
    let mut cmd = CommandBuilder::new(&program);
    cmd.cwd(&cwd);
    pair.slave.spawn_command(cmd).map_err(|e| e.to_string())?;
    let mut reader = pair.master.try_clone_reader().map_err(|e| e.to_string())?;
    let writer = pair.master.take_writer().map_err(|e| e.to_string())?;
    state.writers.lock().unwrap().insert(id.clone(), writer);
    state.masters.lock().unwrap().insert(id.clone(), pair.master);
    // pair.slave drops at end of scope -> parent slave fd closed -> reader sees EOF when child exits
    let id2 = id.clone();
    std::thread::spawn(move || {
        let mut buf = [0u8; 8192];
        loop {
            match reader.read(&mut buf) {
                Ok(0) | Err(_) => { let _ = app.emit("pty-exit", &id2); break; }
                Ok(n) => { let _ = app.emit("pty-output", PtyOut { id: id2.clone(), data: buf[..n].to_vec() }); }
            }
        }
    });
    Ok(())
}

#[tauri::command]
fn pty_write(state: State<Ptys>, id: String, data: Vec<u8>) -> Result<(), String> {
    if let Some(w) = state.writers.lock().unwrap().get_mut(&id) {
        w.write_all(&data).map_err(|e| e.to_string())?;
        let _ = w.flush();
    }
    Ok(())
}

#[tauri::command]
fn pty_resize(state: State<Ptys>, id: String, rows: u16, cols: u16) -> Result<(), String> {
    if let Some(m) = state.masters.lock().unwrap().get(&id) {
        m.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 }).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn pty_kill(state: State<Ptys>, id: String) {
    state.writers.lock().unwrap().remove(&id);
    state.masters.lock().unwrap().remove(&id); // dropping master closes the pty -> child gets SIGHUP
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Ptys::default())
        .invoke_handler(tauri::generate_handler![get_state, open_external, run_cc, pty_spawn, pty_write, pty_resize, pty_kill])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
