use std::collections::HashMap;
use std::io::{BufRead, Read, Write};
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

// Open a folder in Cursor. Prefer the `cursor` CLI; fall back to `open -a Cursor` if it's not on PATH.
#[tauri::command]
fn open_editor(path: String) -> Result<(), String> {
    if std::process::Command::new("cursor").arg(&path).spawn().is_ok() { return Ok(()); }
    std::process::Command::new("open").args(["-a", "Cursor", &path]).spawn()
        .map(|_| ()).map_err(|e| format!("не открыл Cursor: {}", e))
}

// Run an arbitrary cc engine command (action buttons: task mr/merge, epic ops/mr/merge).
#[tauri::command]
fn run_cc(args: Vec<String>) -> Result<String, String> {
    let engine = std::env::var("CC_ENGINE").unwrap_or_else(|_| "cc".to_string());
    let out = std::process::Command::new(&engine).args(&args).output().map_err(|e| e.to_string())?;
    let s = format!("{}{}", String::from_utf8_lossy(&out.stdout), String::from_utf8_lossy(&out.stderr));
    if out.status.success() { Ok(s) } else { Err(if s.trim().is_empty() { "(no output)".into() } else { s }) }
}

// --- embedded terminal via PTY: an interactive shell in the task folder (repos are subfolders) ---
#[tauri::command]
fn pty_spawn(app: AppHandle, state: State<Ptys>, id: String, cwd: String, program: String) -> Result<(), String> {
    let pty = native_pty_system();
    let pair = pty.openpty(PtySize { rows: 30, cols: 100, pixel_width: 0, pixel_height: 0 })
        .map_err(|e| e.to_string())?;
    // empty program → the user's login shell (so aliases/PATH load); set TERM for xterm.js
    let prog = if program.trim().is_empty() {
        std::env::var("SHELL").unwrap_or_else(|_| "/bin/zsh".into())
    } else { program };
    let mut cmd = CommandBuilder::new(&prog);
    cmd.cwd(&cwd);
    cmd.env("TERM", "xterm-256color");
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

// --- headless chat: drive claude/codex in print/stream mode, stream JSON lines to the UI ---
#[derive(Clone, serde::Serialize)]
struct ChatLine { id: String, line: String }
#[derive(Clone, serde::Serialize)]
struct ChatDone { id: String, code: i32, err: String }

// Build the headless+streaming argv per engine; resume the given session when one is supplied.
// `dirs` are extra repos the agent may read/edit beyond cwd (--add-dir) — used so a RESUMED chat
// (pinned to its session's repo cwd) can still touch the task's sibling repos.
fn chat_argv(engine: &str, prompt: &str, session: &str, dirs: &[String]) -> (String, Vec<String>) {
    if engine == "codex" {
        let mut a: Vec<String> = vec!["exec".into()];
        if !session.is_empty() { a.push("resume".into()); a.push(session.into()); }
        a.push("--json".into());
        a.push(prompt.into());
        ("codex".into(), a)
    } else {
        let mut a: Vec<String> = vec![
            "-p".into(), prompt.into(),
            "--output-format".into(), "stream-json".into(), "--verbose".into(),
            "--include-partial-messages".into(),   // emit token deltas (else stream-json sends whole msgs only)
            "--permission-mode".into(), "bypassPermissions".into(),
        ];
        for d in dirs { if !d.is_empty() { a.push("--add-dir".into()); a.push(d.clone()); } }
        if !session.is_empty() { a.push("--resume".into()); a.push(session.into()); }
        ("claude".into(), a)
    }
}

fn chat_run(app: AppHandle, id: String, cwd: String, engine: String, prompt: String, session: String, dirs: Vec<String>) -> Result<(), String> {
    use std::process::{Command, Stdio};
    let (bin, args) = chat_argv(&engine, &prompt, &session, &dirs);
    let mut child = Command::new(&bin).args(&args).current_dir(&cwd)
        .stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null())   // keep stderr — it's the only clue when the engine fails
        .spawn().map_err(|e| format!("не запустил {}: {}", bin, e))?;
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let stderr = child.stderr.take();
    // drain stderr concurrently into a buffer (avoid pipe-buffer deadlock) — surfaced on a bad exit
    let err_buf = std::sync::Arc::new(Mutex::new(String::new()));
    let eb = err_buf.clone();
    let err_handle = std::thread::spawn(move || {
        if let Some(mut se) = stderr { let mut s = String::new(); let _ = se.read_to_string(&mut s); *eb.lock().unwrap() = s; }
    });
    std::thread::spawn(move || {
        let mut child = child;   // owned + mutable inside the thread (for wait())
        let reader = std::io::BufReader::new(stdout);
        for line in reader.lines() {
            match line {
                Ok(l) => { if !l.trim().is_empty() { let _ = app.emit("chat-event", ChatLine { id: id.clone(), line: l }); } }
                Err(_) => break,
            }
        }
        let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
        let _ = err_handle.join();
        let err: String = err_buf.lock().unwrap().chars().take(1200).collect();
        let _ = app.emit("chat-done", ChatDone { id, code, err });
    });
    Ok(())
}

#[tauri::command]
fn chat_spawn(app: AppHandle, id: String, cwd: String, engine: String, prompt: String, session: String, dirs: Vec<String>) -> Result<(), String> {
    chat_run(app, id, cwd, engine, prompt, session, dirs)
}

#[tauri::command]
fn chat_followup(app: AppHandle, id: String, cwd: String, engine: String, prompt: String, session: String, dirs: Vec<String>) -> Result<(), String> {
    chat_run(app, id, cwd, engine, prompt, session, dirs)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .manage(Ptys::default())
        .invoke_handler(tauri::generate_handler![get_state, open_external, open_editor, run_cc, pty_spawn, pty_write, pty_resize, pty_kill, chat_spawn, chat_followup])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
