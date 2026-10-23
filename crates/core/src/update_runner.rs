//! One-click upgrade of the macOS app: fetch, pull (ff-only), rebuild app +
//! DMG into a staging dir, swap the installed app, restart the dashboard
//! daemon (never the scheduler), relaunch. Mirrors `bin/update_runner.py`.
//!
//! Progress goes to `<state>/progress.json` after every change so the
//! `/update` page can show it; the run is detached ([`start_detached`]) so it
//! outlives the app window. The on-disk formats (`progress.json`, `app_path.txt`,
//! `upgrade.lock`) are byte-compatible with the Python's and documented in
//! docs/contract/json-files.md.

use crate::golden_eval::finished_at_now;
use crate::pyjson;
use crate::update_check;
use serde_json::{json, Map, Value};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Duration;

pub const APP_NAME: &str = "Loop X";
pub const STEPS: [&str; 6] = [
    "fetch",
    "pull",
    "build",
    "install",
    "restart_dashboard",
    "relaunch",
];
pub const LOG_TAIL_LINES: usize = 40;
/// The app launcher's own PATH: a launchd-started dashboard has almost none.
const COMMAND_PATH: &str =
    "$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin";

/// A step body: given the line sink, do the work.
type StepFn<'a> = dyn FnMut(&mut dyn FnMut(&str)) -> Result<(), String> + 'a;

pub type Progress = Map<String, Value>;

/// Runs one external command, streaming its lines (stdout and stderr merged)
/// to `on_line`; returns the exit status. Injected so tests can fake it.
pub type RunCmd<'a> = dyn Fn(&[String], &Path, &mut dyn FnMut(&str)) -> i32 + 'a;

fn state_of(state_dir: Option<&Path>) -> PathBuf {
    state_dir.map_or_else(update_check::default_state_dir, Path::to_path_buf)
}

pub fn write_progress(state_dir: &Path, progress: &Progress) -> std::io::Result<()> {
    std::fs::create_dir_all(state_dir)?;
    let tmp = state_dir.join(".progress.tmp");
    std::fs::write(
        &tmp,
        pyjson::to_string_pretty(&Value::Object(progress.clone())),
    )?;
    std::fs::rename(tmp, state_dir.join("progress.json"))
}

/// `os.kill(pid, 0)` succeeding. A permission error (someone else's pid) is
/// "not alive", like the Python's `except OSError`.
pub fn pid_alive(pid: &Value) -> bool {
    let n = match pid {
        Value::Number(n) => n.as_i64(),
        Value::String(s) => s.trim().parse::<i64>().ok(),
        _ => None,
    };
    let Some(n) = n.and_then(|n| i32::try_from(n).ok()) else {
        return false;
    };
    // SAFETY: signal 0 only checks that the process exists.
    unsafe { libc::kill(n, 0) == 0 }
}

fn fresh_progress(state: &str) -> Progress {
    let steps: Vec<Value> = STEPS
        .iter()
        .map(|n| json!({"name": n, "status": "pending", "started": null, "finished": null, "log": ""}))
        .collect();
    let mut p = Map::new();
    p.insert("state".into(), json!(state));
    p.insert("pid".into(), json!(std::process::id()));
    p.insert("started".into(), json!(finished_at_now()));
    p.insert("finished".into(), Value::Null);
    p.insert("error".into(), Value::Null);
    p.insert("from".into(), json!(""));
    p.insert("to".into(), json!(""));
    p.insert("steps".into(), Value::Array(steps));
    p
}

/// `progress.json`, or `{}`. A run that says it is starting/running but whose
/// process is gone reads as failed ("Upgrade was interrupted").
pub fn read_progress(state_dir: Option<&Path>) -> Progress {
    let dir = state_of(state_dir);
    let Some(Value::Object(mut progress)) = std::fs::read_to_string(dir.join("progress.json"))
        .ok()
        .and_then(|t| serde_json::from_str::<Value>(&t).ok())
    else {
        return Map::new();
    };
    let running = matches!(
        progress.get("state").and_then(Value::as_str),
        Some("starting" | "running")
    );
    if running && !pid_alive(progress.get("pid").unwrap_or(&Value::Null)) {
        progress.insert("state".into(), json!("failed"));
        progress.insert("error".into(), json!("Upgrade was interrupted"));
    }
    progress
}

/// Remember where the app is installed (`app_path.txt`).
pub fn record_app_path(path: &Path, state_dir: Option<&Path>) -> std::io::Result<()> {
    let dir = state_of(state_dir);
    std::fs::create_dir_all(&dir)?;
    std::fs::write(dir.join("app_path.txt"), path.display().to_string())
}

/// The recorded `*.app` if it still exists, else `<repo>/dist/Loop X.app`.
pub fn resolve_install_target(repo: Option<&Path>, state_dir: Option<&Path>) -> PathBuf {
    let repo = repo.map_or_else(update_check::default_repo_dir, Path::to_path_buf);
    if let Ok(text) = std::fs::read_to_string(state_of(state_dir).join("app_path.txt")) {
        let recorded = PathBuf::from(text.trim());
        if recorded.extension().is_some_and(|e| e == "app") && recorded.is_dir() {
            return recorded;
        }
    }
    repo.join("dist").join(format!("{APP_NAME}.app"))
}

fn lock_is_live(state: &Path) -> bool {
    std::fs::read_to_string(state.join("upgrade.lock"))
        .map(|t| pid_alive(&json!(t.trim())))
        .unwrap_or(false)
}

fn acquire_lock(state: &Path) -> bool {
    if std::fs::create_dir_all(state).is_err() {
        return false;
    }
    let lock = state.join("upgrade.lock");
    for _ in 0..2 {
        match std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&lock)
        {
            Ok(mut f) => {
                let _ = f.write_all(std::process::id().to_string().as_bytes());
                return true;
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if lock_is_live(state) {
                    return false;
                }
                let _ = std::fs::remove_file(&lock); // stale
            }
            Err(_) => return false,
        }
    }
    false
}

/// `shutil.copytree(src, dst, symlinks=True)`.
pub fn copy_tree(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    std::fs::set_permissions(dst, std::fs::metadata(src)?.permissions())?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let (from, to) = (entry.path(), dst.join(entry.file_name()));
        let kind = entry.file_type()?;
        if kind.is_symlink() {
            std::os::unix::fs::symlink(std::fs::read_link(&from)?, &to)?;
        } else if kind.is_dir() {
            copy_tree(&from, &to)?;
        } else {
            std::fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

fn sibling(target: &Path, name: String) -> PathBuf {
    target.with_file_name(name)
}

fn old_of(target: &Path) -> PathBuf {
    sibling(
        target,
        format!(
            "{}.old",
            target.file_name().unwrap_or_default().to_string_lossy()
        ),
    )
}

/// Replace `target` with `new_app`, crash-safely.
///
/// The new bundle is first copied NEXT TO the target (same volume, so the
/// final step is an atomic rename, never a cross-volume copy that a sleep or
/// power loss can cut in half). The old app is renamed to `<name>.old` and
/// kept: deleting the bundle of the app that is still running is what
/// [`cleanup_old`] does after the relaunch. A leftover `.old` with no target
/// is the last good app of an interrupted earlier swap and is restored, not
/// deleted.
pub fn swap_app(new_app: &Path, target: &Path) -> std::io::Result<()> {
    swap_app_with(new_app, target, &copy_tree, &|a, b| std::fs::rename(a, b))
}

/// [`swap_app`] with the copy and rename steps injectable (for failure tests).
pub fn swap_app_with(
    new_app: &Path,
    target: &Path,
    copytree: &dyn Fn(&Path, &Path) -> std::io::Result<()>,
    rename: &dyn Fn(&Path, &Path) -> std::io::Result<()>,
) -> std::io::Result<()> {
    let name = target
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let old = old_of(target);
    let incoming = sibling(target, format!(".{name}.new"));
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if old.exists() {
        if target.exists() {
            std::fs::remove_dir_all(&old)?;
        } else {
            rename(&old, target)?;
        }
    }
    let _ = std::fs::remove_dir_all(&incoming);
    if let Err(e) = copytree(new_app, &incoming) {
        let _ = std::fs::remove_dir_all(&incoming);
        return Err(e);
    }
    let had_old = target.exists();
    if had_old {
        rename(target, &old)?;
    }
    if let Err(e) = rename(&incoming, target) {
        if had_old {
            rename(&old, target)?;
        }
        let _ = std::fs::remove_dir_all(&incoming);
        return Err(e);
    }
    Ok(())
}

/// Remove the previous app kept by [`swap_app`], only while a current app exists.
pub fn cleanup_old(target: &Path) {
    let old = old_of(target);
    if old.exists() && target.exists() {
        let _ = std::fs::remove_dir_all(old);
    }
}

/// PATH and git prompt settings for the commands the upgrade runs.
pub fn command_env() -> Vec<(String, String)> {
    let home = std::env::var("HOME").unwrap_or_default();
    let mut env = update_check::non_interactive_env();
    env.push(("PATH".into(), COMMAND_PATH.replace("$HOME", &home)));
    env
}

/// The interpreter the build script is told to use (`--python`). The Python
/// build ran the dashboard's own interpreter; there is none here, so this is
/// `$LOOPX_BUILD_PYTHON`, else `python3`.
pub fn build_python() -> String {
    std::env::var("LOOPX_BUILD_PYTHON")
        .ok()
        .filter(|p| !p.is_empty())
        .unwrap_or_else(|| "python3".to_string())
}

/// The real command runner: stdout and stderr merged, line by line.
pub fn real_run_cmd(argv: &[String], cwd: &Path, on_line: &mut dyn FnMut(&str)) -> i32 {
    let Some((program, args)) = argv.split_first() else {
        return 127;
    };
    // `exec "$@" 2>&1` merges the streams into the one pipe we read.
    let mut cmd = Command::new("/bin/sh");
    cmd.arg("-c")
        .arg("exec \"$@\" 2>&1")
        .arg("sh")
        .arg(program)
        .args(args)
        .current_dir(cwd)
        .envs(command_env())
        .stdin(Stdio::null())
        .stdout(Stdio::piped());
    let Ok(mut child) = cmd.spawn() else {
        return 127;
    };
    if let Some(out) = child.stdout.take() {
        for line in BufReader::new(out).split(b'\n').map_while(Result::ok) {
            on_line(&String::from_utf8_lossy(&line));
        }
    }
    child.wait().ok().and_then(|s| s.code()).unwrap_or(-1)
}

fn step_mut<'a>(progress: &'a mut Progress, name: &str) -> &'a mut Map<String, Value> {
    progress
        .get_mut("steps")
        .and_then(Value::as_array_mut)
        .and_then(|steps| {
            steps
                .iter_mut()
                .find(|s| s.get("name").and_then(Value::as_str) == Some(name))
        })
        .and_then(Value::as_object_mut)
        .expect("every step in STEPS exists")
}

fn update_step(progress: &mut Progress, name: &str, fields: &[(&str, Value)]) {
    let step = step_mut(progress, name);
    for (k, v) in fields {
        step.insert((*k).to_string(), v.clone());
    }
}

fn s(parts: &[&str]) -> Vec<String> {
    parts.iter().map(|p| p.to_string()).collect()
}

/// The upgrade, start to finish. Refusals and failures are reported in the
/// returned progress (`state` `refused` / `failed`), never as an error.
pub fn run(
    repo: Option<&Path>,
    state_dir: Option<&Path>,
    install_target: Option<&Path>,
    run_cmd: Option<&RunCmd>,
) -> Progress {
    let repo = repo.map_or_else(update_check::default_repo_dir, Path::to_path_buf);
    let state = state_of(state_dir);
    let run_cmd: &RunCmd = run_cmd.unwrap_or(&real_run_cmd);
    let mut progress = fresh_progress("running");

    if !acquire_lock(&state) {
        // Do not write progress.json: it belongs to the run that holds the lock.
        progress.insert("state".into(), json!("refused"));
        progress.insert("error".into(), json!("An upgrade is already running"));
        return progress;
    }
    let save = |p: &Progress| {
        let _ = write_progress(&state, p);
    };
    let refuse = |p: &mut Progress, message: &str| {
        p.insert("state".into(), json!("refused"));
        p.insert("error".into(), json!(message));
        p.insert("finished".into(), json!(finished_at_now()));
        save(p);
    };

    let outcome = (|| -> Result<PathBuf, ()> {
        let status = update_check::check(Some(&repo), Some(&state), true);
        let text = |k: &str| {
            status
                .get(k)
                .and_then(Value::as_str)
                .unwrap_or("")
                .to_string()
        };
        progress.insert("from".into(), json!(text("current")));
        if status.get("clean") == Some(&json!(false)) {
            refuse(
                &mut progress,
                "You have local changes to tracked files; commit or stash them, then retry",
            );
            return Err(());
        }
        if status.get("can_fast_forward") == Some(&json!(false)) {
            refuse(
                &mut progress,
                "This checkout has diverged from origin and cannot fast-forward; resolve it with git, then retry",
            );
            return Err(());
        }
        let target = install_target
            .map(Path::to_path_buf)
            .unwrap_or_else(|| resolve_install_target(Some(&repo), Some(&state)));
        let staging = state.join("staging");
        save(&progress);

        let repo_s = repo.display().to_string();
        let branch = text("branch");
        let run_step = |progress: &mut Progress, name: &str, f: &mut StepFn| -> bool {
            update_step(
                progress,
                name,
                &[
                    ("status", json!("running")),
                    ("started", json!(finished_at_now())),
                ],
            );
            let mut lines: Vec<String> = Vec::new();
            let result = {
                let progress_cell = std::cell::RefCell::new(&mut *progress);
                let mut on_line = |line: &str| {
                    lines.push(line.to_string());
                    let tail = lines[lines.len().saturating_sub(LOG_TAIL_LINES)..].join("\n");
                    let mut p = progress_cell.borrow_mut();
                    update_step(&mut p, name, &[("log", json!(tail))]);
                    save(&p);
                };
                save(&progress_cell.borrow());
                f(&mut on_line)
            };
            match result {
                Err(e) => {
                    update_step(
                        progress,
                        name,
                        &[
                            ("status", json!("failed")),
                            ("finished", json!(finished_at_now())),
                        ],
                    );
                    progress.insert("state".into(), json!("failed"));
                    progress.insert("error".into(), json!(format!("{name}: {e}")));
                    progress.insert("finished".into(), json!(finished_at_now()));
                    save(progress);
                    false
                }
                Ok(()) => {
                    update_step(
                        progress,
                        name,
                        &[
                            ("status", json!("done")),
                            ("finished", json!(finished_at_now())),
                        ],
                    );
                    save(progress);
                    true
                }
            }
        };
        let cmd = |argv: Vec<String>, on_line: &mut dyn FnMut(&str)| -> Result<(), String> {
            let rc = run_cmd(&argv, &repo, on_line);
            if rc != 0 {
                return Err(format!("exited with status {rc}"));
            }
            Ok(())
        };

        if !run_step(&mut progress, "fetch", &mut |ol| {
            cmd(s(&["git", "-C", &repo_s, "fetch", "--tags", "origin"]), ol)
        }) {
            return Err(());
        }
        let merge_ref = format!("origin/{branch}");
        if !run_step(&mut progress, "pull", &mut |ol| {
            cmd(
                s(&[
                    "git",
                    "-C",
                    &repo_s,
                    "merge",
                    "--ff-only",
                    "--autostash",
                    &merge_ref,
                ]),
                ol,
            )
        }) {
            return Err(());
        }
        if !run_step(&mut progress, "build", &mut |ol| {
            let _ = std::fs::remove_dir_all(&staging);
            std::fs::create_dir_all(&staging).map_err(|e| e.to_string())?;
            let script = repo.join("bin").join("scripts").join("build_macos_app.sh");
            cmd(
                vec![
                    "bash".into(),
                    script.display().to_string(),
                    "--dmg".into(),
                    "--python".into(),
                    build_python(),
                    "--output-dir".into(),
                    staging.display().to_string(),
                ],
                ol,
            )
        }) {
            return Err(());
        }
        if !run_step(&mut progress, "install", &mut |ol| {
            ol(&format!("Installing to {}", target.display()));
            swap_app(&staging.join(format!("{APP_NAME}.app")), &target)
                .map_err(|e| e.to_string())?;
            let dmg = staging.join(format!("{APP_NAME}.dmg"));
            if dmg.exists() {
                let dist = repo.join("dist");
                std::fs::create_dir_all(&dist).map_err(|e| e.to_string())?;
                std::fs::copy(&dmg, dist.join(format!("{APP_NAME}.dmg")))
                    .map_err(|e| e.to_string())?;
            }
            let _ = std::fs::remove_dir_all(&staging);
            Ok(())
        }) {
            return Err(());
        }
        if !run_step(&mut progress, "restart_dashboard", &mut |ol| {
            let script = repo.join("bin").join("scripts").join("restart-daemons.sh");
            cmd(vec!["bash".into(), script.display().to_string()], ol)
        }) {
            return Err(());
        }
        let after = update_check::check(Some(&repo), Some(&state), false);
        progress.insert(
            "to".into(),
            json!(after.get("current").and_then(Value::as_str).unwrap_or("")),
        );
        let now = finished_at_now();
        update_step(
            &mut progress,
            "relaunch",
            &[
                ("status", json!("done")),
                ("started", json!(now)),
                ("finished", json!(now)),
                ("log", json!(format!("Relaunching {APP_NAME}"))),
            ],
        );
        progress.insert("state".into(), json!("done"));
        progress.insert("finished".into(), json!(finished_at_now()));
        save(&progress);
        Ok(target)
    })();
    let _ = std::fs::remove_file(state.join("upgrade.lock"));

    let Ok(target) = outcome else {
        return progress;
    };
    // Last action: the old window goes away, the new app opens on /update.
    let quiet = &mut |_: &str| {};
    run_cmd(
        &s(&[
            "osascript",
            "-e",
            &format!("tell application \"{APP_NAME}\" to quit"),
        ]),
        &repo,
        quiet,
    );
    wait_until_quit(run_cmd, &repo, 30, Duration::from_millis(500));
    run_cmd(
        &["open".to_string(), target.display().to_string()],
        &repo,
        quiet,
    );
    cleanup_old(&target);
    progress
}

/// Poll until the old app has really exited (an `open` while it is still
/// quitting just re-activates it, which then exits and leaves no app running).
pub fn wait_until_quit(run_cmd: &RunCmd, repo: &Path, attempts: usize, pause: Duration) {
    let script = format!("application \"{APP_NAME}\" is running");
    for _ in 0..attempts {
        let mut lines: Vec<String> = Vec::new();
        run_cmd(&s(&["osascript", "-e", &script]), repo, &mut |l: &str| {
            lines.push(l.to_string())
        });
        if lines.iter().any(|l| l.trim() == "false") {
            return;
        }
        std::thread::sleep(pause);
    }
}

/// How [`start_detached`] launches the runner; returns the child's pid.
pub type Spawn<'a> = dyn Fn(&[String], &Path) -> Option<u32> + 'a;

/// `<this binary> update run --repo R --state-dir S` in its own session.
pub fn real_spawn(argv: &[String], cwd: &Path) -> Option<u32> {
    use std::os::unix::process::CommandExt;
    let (program, args) = argv.split_first()?;
    let mut cmd = Command::new(program);
    cmd.args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    // SAFETY: setsid() between fork and exec only detaches the session.
    unsafe {
        cmd.pre_exec(|| {
            libc::setsid();
            Ok(())
        });
    }
    cmd.spawn().ok().map(|c| c.id())
}

/// Start the upgrade in a detached process and return the initial progress
/// right away. Refused while an upgrade is already running.
pub fn start_detached(
    repo: Option<&Path>,
    state_dir: Option<&Path>,
    spawn: Option<&Spawn>,
) -> Progress {
    let repo = repo.map_or_else(update_check::default_repo_dir, Path::to_path_buf);
    let state = state_of(state_dir);
    let spawn: &Spawn = spawn.unwrap_or(&real_spawn);
    if lock_is_live(&state) {
        let mut progress = {
            let p = read_progress(Some(&state));
            if p.is_empty() {
                fresh_progress("starting")
            } else {
                p
            }
        };
        progress.insert("state".into(), json!("refused"));
        progress.insert("error".into(), json!("An upgrade is already running"));
        return progress;
    }
    let mut progress = fresh_progress("starting");
    let _ = write_progress(&state, &progress);
    let exe = std::env::current_exe()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "loopx".into());
    let argv = vec![
        exe,
        "update".into(),
        "run".into(),
        "--repo".into(),
        repo.display().to_string(),
        "--state-dir".into(),
        state.display().to_string(),
    ];
    if let Some(pid) = spawn(&argv, &repo) {
        // So a runner that dies before its first save reads as failed, not "running" forever.
        progress.insert("pid".into(), json!(pid));
        let _ = write_progress(&state, &progress);
    }
    progress
}

/// Used by `loopx update status`: whether `state` is a refusal.
pub fn is_done(progress: &Progress) -> bool {
    progress.get("state").and_then(Value::as_str) == Some("done")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::update_check::testutil::{commit, repos, tgit, Repos};
    use std::cell::RefCell;

    /// Stands in for the command runner: git runs for real (against the temp
    /// repos), everything else is recorded and faked.
    struct FakeCmd {
        calls: RefCell<Vec<Vec<String>>>,
        fail_on: Option<&'static str>,
        quit_after: usize,
        polls: RefCell<usize>,
    }

    impl FakeCmd {
        fn new() -> Self {
            Self {
                calls: RefCell::new(vec![]),
                fail_on: None,
                quit_after: 0,
                polls: RefCell::new(0),
            }
        }
        fn failing(on: &'static str) -> Self {
            Self {
                fail_on: Some(on),
                ..Self::new()
            }
        }
        fn call(&self, argv: &[String], cwd: &Path, on_line: &mut dyn FnMut(&str)) -> i32 {
            on_line("line");
            let joined = argv.join(" ");
            if joined.contains("is running") {
                *self.polls.borrow_mut() += 1;
                on_line(if *self.polls.borrow() > self.quit_after {
                    "false"
                } else {
                    "true"
                });
                self.calls.borrow_mut().push(vec!["is-running-poll".into()]);
                return 0;
            }
            self.calls.borrow_mut().push(argv.to_vec());
            if self.fail_on.is_some_and(|f| joined.contains(f)) {
                on_line("boom");
                return 1;
            }
            if argv[0] == "git" {
                return Command::new(&argv[0])
                    .args(&argv[1..])
                    .current_dir(cwd)
                    .output()
                    .map(|o| o.status.code().unwrap_or(-1))
                    .unwrap_or(127);
            }
            if joined.contains("build_macos_app.sh") {
                let i = argv.iter().position(|a| a == "--output-dir").unwrap();
                let out = PathBuf::from(&argv[i + 1]);
                std::fs::create_dir_all(out.join("Loop X.app/Contents")).unwrap();
                std::fs::write(out.join("Loop X.app/Contents/marker"), "new").unwrap();
                std::fs::write(out.join("Loop X.dmg"), "dmg").unwrap();
            }
            0
        }
        fn flat(&self) -> Vec<String> {
            self.calls.borrow().iter().map(|c| c.join(" ")).collect()
        }
    }

    fn go(r: &Repos, target: &Path, cmd: &FakeCmd) -> Progress {
        let f = |a: &[String], c: &Path, l: &mut dyn FnMut(&str)| cmd.call(a, c, l);
        run(Some(&r.work), Some(&r.state), Some(target), Some(&f))
    }

    fn behind(r: &Repos) {
        commit(&r.other, "b.txt", "x");
        tgit(&r.other, &["push", "origin", "main"]);
    }

    fn app_with_marker(dir: &Path, marker: &str) -> PathBuf {
        let app = dir.join("Loop X.app");
        std::fs::create_dir_all(app.join("Contents")).unwrap();
        std::fs::write(app.join("Contents/marker"), marker).unwrap();
        app
    }

    fn statuses(p: &Progress) -> Vec<String> {
        p["steps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["status"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn happy_path_runs_steps_in_order_and_installs() {
        let r = repos();
        behind(&r);
        let target = app_with_marker(&r.root.join("Apps"), "old");
        let cmd = FakeCmd::new();
        let p = go(&r, &target, &cmd);
        assert_eq!(p["state"], json!("done"));
        assert_eq!(statuses(&p), ["done"; 6]);
        let names: Vec<&str> = p["steps"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["name"].as_str().unwrap())
            .collect();
        assert_eq!(names, STEPS);
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "new"
        );
        assert!(!r.root.join("Apps/Loop X.app.old").exists());
        assert!(r.work.join("b.txt").exists()); // pulled for real
        assert_eq!(
            std::fs::read_to_string(r.work.join("dist/Loop X.dmg")).unwrap(),
            "dmg"
        );
        let flat = cmd.flat();
        assert!(flat.iter().any(|c| c.contains("restart-daemons.sh")));
        assert!(!flat.iter().any(|c| c.contains("--with-scheduler")));
        assert!(flat
            .iter()
            .any(|c| c.starts_with("open") && c.contains("Loop X.app")));
        assert_eq!(
            update_check::read_status(Some(&r.state))["behind"],
            json!(0)
        );
        assert!(!r.state.join("upgrade.lock").exists());
        assert_eq!(p["from"], json!("v0.1.0"));
        assert!(p["to"].as_str().unwrap().starts_with("v0.1.0-1-g"));
        // The saved file matches the returned progress.
        assert_eq!(read_progress(Some(&r.state))["state"], json!("done"));
    }

    #[test]
    fn build_failure_stops_keeps_old_app_and_reports_log() {
        let r = repos();
        behind(&r);
        let target = app_with_marker(&r.root, "old");
        let p = go(&r, &target, &FakeCmd::failing("build_macos_app"));
        assert_eq!(p["state"], json!("failed"));
        assert_eq!(p["error"], json!("build: exited with status 1"));
        let by = |n: &str| {
            p["steps"]
                .as_array()
                .unwrap()
                .iter()
                .find(|s| s["name"] == n)
                .unwrap()
                .clone()
        };
        assert_eq!(by("build")["status"], json!("failed"));
        assert!(by("build")["log"].as_str().unwrap().contains("boom"));
        assert_eq!(by("install")["status"], json!("pending"));
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "old"
        );
        assert!(!r.state.join("upgrade.lock").exists());
    }

    #[test]
    fn dirty_tracked_file_is_refused_before_any_step() {
        let r = repos();
        behind(&r);
        std::fs::write(r.work.join("a.txt"), "edited").unwrap();
        let cmd = FakeCmd::new();
        let p = go(&r, &r.root.join("x.app"), &cmd);
        assert_eq!(p["state"], json!("refused"));
        assert!(p["error"].as_str().unwrap().contains("local changes"));
        assert!(cmd.calls.borrow().is_empty());
        assert_eq!(read_progress(Some(&r.state))["state"], json!("refused"));
    }

    #[test]
    fn modified_progress_md_does_not_block() {
        let r = repos();
        commit(&r.work, "PROGRESS.md", "x");
        tgit(&r.work, &["push", "origin", "main"]);
        tgit(&r.other, &["pull", "--ff-only"]);
        behind(&r);
        std::fs::write(r.work.join("PROGRESS.md"), "loop wrote this").unwrap();
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("done"));
        assert_eq!(
            std::fs::read_to_string(r.work.join("PROGRESS.md")).unwrap(),
            "loop wrote this"
        );
    }

    #[test]
    fn non_fast_forward_is_refused() {
        let r = repos();
        behind(&r);
        commit(&r.work, "local.txt", "x");
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("refused"));
        assert!(p["error"].as_str().unwrap().contains("fast-forward"));
    }

    #[test]
    fn live_lock_refuses_second_run_without_clobbering_progress_and_stale_lock_is_ignored() {
        let r = repos();
        behind(&r);
        std::fs::create_dir_all(&r.state).unwrap();
        let mut running = Map::new();
        running.insert("state".into(), json!("running"));
        running.insert("pid".into(), json!(std::process::id()));
        running.insert("steps".into(), json!([]));
        write_progress(&r.state, &running).unwrap();
        std::fs::write(r.state.join("upgrade.lock"), std::process::id().to_string()).unwrap();
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("refused"));
        assert!(p["error"].as_str().unwrap().contains("already"));
        assert_eq!(read_progress(Some(&r.state))["state"], json!("running")); // live run's file untouched
        assert!(r.state.join("upgrade.lock").exists()); // and its lock too
        std::fs::write(r.state.join("upgrade.lock"), "999999999").unwrap();
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("done"));
    }

    #[test]
    fn swap_replaces_app_and_cleanup_removes_old() {
        let r = repos();
        let target = app_with_marker(&r.root, "old");
        let new = app_with_marker(&r.root.join("stage"), "new");
        swap_app(&new, &target).unwrap();
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "new"
        );
        let old = r.root.join("Loop X.app.old");
        assert!(old.exists()); // the running app's bundle is not deleted under it
        cleanup_old(&target);
        assert!(!old.exists());
        let lone = r.root.join("Other.app.old");
        std::fs::create_dir_all(&lone).unwrap();
        cleanup_old(&r.root.join("Other.app")); // no target: keep the only copy
        assert!(lone.exists());
    }

    #[test]
    fn swap_restores_old_app_when_the_final_rename_fails() {
        let r = repos();
        let target = app_with_marker(&r.root, "old");
        let new = r.root.join("stage/Loop X.app");
        std::fs::create_dir_all(&new).unwrap();
        let rename = |a: &Path, b: &Path| {
            if a.file_name().is_some_and(|n| n == ".Loop X.app.new") {
                return Err(std::io::Error::other("rename failed"));
            }
            std::fs::rename(a, b)
        };
        assert!(swap_app_with(&new, &target, &copy_tree, &rename).is_err());
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "old"
        );
        assert!(!r.root.join("Loop X.app.old").exists());
        assert!(!r.root.join(".Loop X.app.new").exists());
    }

    #[test]
    fn swap_leaves_target_intact_when_copy_is_interrupted_midway() {
        let r = repos();
        let target = app_with_marker(&r.root, "old");
        let new = app_with_marker(&r.root.join("stage"), "new");
        let partial = |_: &Path, dst: &Path| {
            std::fs::create_dir_all(dst)?;
            std::fs::write(dst.join("half"), "x")?;
            Err(std::io::Error::other("interrupted"))
        };
        assert!(swap_app_with(&new, &target, &partial, &|a, b| std::fs::rename(a, b)).is_err());
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "old"
        );
        assert!(!r.root.join(".Loop X.app.new").exists());
    }

    #[test]
    fn swap_recovers_a_lost_target_from_a_leftover_old_instead_of_deleting_it() {
        let r = repos();
        app_with_marker(&r.root, "last good app");
        std::fs::rename(r.root.join("Loop X.app"), r.root.join("Loop X.app.old")).unwrap();
        let target = r.root.join("Loop X.app"); // missing: an earlier swap was interrupted
        let new = app_with_marker(&r.root.join("stage"), "new");
        swap_app(&new, &target).unwrap();
        assert_eq!(
            std::fs::read_to_string(target.join("Contents/marker")).unwrap(),
            "new"
        );
        assert_eq!(
            std::fs::read_to_string(r.root.join("Loop X.app.old/Contents/marker")).unwrap(),
            "last good app"
        );
    }

    #[test]
    fn copy_tree_keeps_symlinks_and_modes() {
        use std::os::unix::fs::PermissionsExt;
        let r = repos();
        let src = r.root.join("src");
        std::fs::create_dir_all(src.join("bin")).unwrap();
        std::fs::write(src.join("bin/tool"), "#!/bin/sh").unwrap();
        std::fs::set_permissions(src.join("bin/tool"), std::fs::Permissions::from_mode(0o755))
            .unwrap();
        std::os::unix::fs::symlink("bin/tool", src.join("link")).unwrap();
        copy_tree(&src, &r.root.join("dst")).unwrap();
        assert_eq!(
            std::fs::read_link(r.root.join("dst/link")).unwrap(),
            PathBuf::from("bin/tool")
        );
        let mode = std::fs::metadata(r.root.join("dst/bin/tool"))
            .unwrap()
            .permissions()
            .mode();
        assert_eq!(mode & 0o777, 0o755);
    }

    #[test]
    fn running_progress_with_dead_pid_reads_as_failed() {
        let r = repos();
        let mut p = Map::new();
        p.insert("state".into(), json!("running"));
        p.insert("pid".into(), json!(999999999));
        p.insert("steps".into(), json!([]));
        write_progress(&r.root, &p).unwrap();
        let got = read_progress(Some(&r.root));
        assert_eq!(got["state"], json!("failed"));
        assert!(got["error"].as_str().unwrap().contains("interrupted"));
        // A finished run is left alone whatever its pid.
        p.insert("state".into(), json!("done"));
        write_progress(&r.root, &p).unwrap();
        assert_eq!(read_progress(Some(&r.root))["state"], json!("done"));
        assert!(read_progress(Some(&r.root.join("missing"))).is_empty());
    }

    #[test]
    fn start_detached_writes_initial_progress_spawns_runner_and_records_pid() {
        let r = repos();
        let spawned = RefCell::new(vec![]);
        let spawn = |argv: &[String], _cwd: &Path| {
            spawned.borrow_mut().push(argv.to_vec());
            Some(4242)
        };
        let p = start_detached(Some(&r.root), Some(&r.root.join("s")), Some(&spawn));
        assert_eq!(p["state"], json!("starting"));
        assert_eq!(p["pid"], json!(4242));
        assert_eq!(statuses(&p), ["pending"; 6]);
        let argv = spawned.borrow()[0].clone();
        assert_eq!(&argv[1..3], ["update", "run"]);
        assert!(argv.contains(&"--repo".to_string()) && argv.contains(&"--state-dir".to_string()));
        let on_disk: Value =
            serde_json::from_str(&std::fs::read_to_string(r.root.join("s/progress.json")).unwrap())
                .unwrap();
        assert_eq!(on_disk["pid"], json!(4242));
    }

    #[test]
    fn start_detached_refuses_when_run_in_flight() {
        let r = repos();
        let state = r.root.join("s");
        std::fs::create_dir_all(&state).unwrap();
        std::fs::write(state.join("upgrade.lock"), std::process::id().to_string()).unwrap();
        let spawned = RefCell::new(0);
        let spawn = |_: &[String], _: &Path| {
            *spawned.borrow_mut() += 1;
            None
        };
        let p = start_detached(Some(&r.root), Some(&state), Some(&spawn));
        assert_eq!(p["state"], json!("refused"));
        assert_eq!(*spawned.borrow(), 0);
    }

    #[test]
    fn resolve_install_target_prefers_the_recorded_app() {
        let r = repos();
        let state = r.root.join("s");
        assert_eq!(
            resolve_install_target(Some(&r.root), Some(&state)),
            r.root.join("dist").join("Loop X.app")
        );
        let app = r.root.join("Apps/Loop X.app");
        std::fs::create_dir_all(&app).unwrap();
        record_app_path(&app, Some(&state)).unwrap();
        assert_eq!(
            std::fs::read_to_string(state.join("app_path.txt")).unwrap(),
            app.display().to_string()
        );
        assert_eq!(resolve_install_target(Some(&r.root), Some(&state)), app);
        // A recorded path that is not an existing .app is ignored.
        std::fs::write(
            state.join("app_path.txt"),
            r.root.join("gone.app").display().to_string(),
        )
        .unwrap();
        assert_eq!(
            resolve_install_target(Some(&r.root), Some(&state)),
            r.root.join("dist").join("Loop X.app")
        );
    }

    #[test]
    fn build_gets_an_explicit_python_and_a_full_path() {
        let r = repos();
        behind(&r);
        let cmd = FakeCmd::new();
        go(&r, &r.root.join("L.app"), &cmd);
        let calls = cmd.calls.borrow();
        let build = calls
            .iter()
            .find(|c| c.iter().any(|a| a.contains("build_macos_app.sh")))
            .unwrap();
        let i = build.iter().position(|a| a == "--python").unwrap();
        assert_eq!(build[i + 1], build_python());
        let path = command_env()
            .into_iter()
            .find(|(k, _)| k == "PATH")
            .unwrap()
            .1;
        assert!(path.split(':').any(|p| p == "/opt/homebrew/bin"));
        assert!(!path.contains("$HOME"));
    }

    #[test]
    fn relaunch_waits_for_the_old_app_to_quit_before_opening() {
        let r = repos();
        behind(&r);
        let mut cmd = FakeCmd::new();
        cmd.quit_after = 2;
        let p = go(&r, &r.root.join("L.app"), &cmd);
        assert_eq!(p["state"], json!("done"));
        let names: Vec<String> = cmd.calls.borrow().iter().map(|c| c[0].clone()).collect();
        let polls: Vec<usize> = names
            .iter()
            .enumerate()
            .filter(|(_, n)| *n == "is-running-poll")
            .map(|(i, _)| i)
            .collect();
        assert_eq!(polls.len(), 3);
        assert!(names.iter().position(|n| n == "open").unwrap() > *polls.iter().max().unwrap());
    }

    #[test]
    fn upgrade_works_on_a_branch_with_no_configured_upstream() {
        let r = repos();
        behind(&r);
        tgit(&r.work, &["branch", "--unset-upstream"]);
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("done"));
        assert!(r.work.join("b.txt").exists());
    }

    #[test]
    fn upgrade_succeeds_with_a_locally_modified_progress_md_that_upstream_did_not_touch() {
        let r = repos();
        commit(&r.work, "PROGRESS.md", "v1");
        tgit(&r.work, &["push", "origin", "main"]);
        tgit(&r.other, &["pull", "--ff-only"]);
        behind(&r);
        std::fs::write(r.work.join("PROGRESS.md"), "loop wrote this").unwrap();
        let p = go(&r, &r.root.join("L.app"), &FakeCmd::new());
        assert_eq!(p["state"], json!("done"));
        assert_eq!(
            std::fs::read_to_string(r.work.join("PROGRESS.md")).unwrap(),
            "loop wrote this"
        );
    }

    #[test]
    fn progress_file_has_the_python_layout() {
        let r = repos();
        write_progress(&r.root, &fresh_progress("starting")).unwrap();
        let text = std::fs::read_to_string(r.root.join("progress.json")).unwrap();
        let keys: Vec<String> = serde_json::from_str::<Map<String, Value>>(&text)
            .unwrap()
            .keys()
            .cloned()
            .collect();
        assert_eq!(
            keys,
            ["state", "pid", "started", "finished", "error", "from", "to", "steps"]
        );
        assert!(text.contains("\"steps\": [\n    {\n      \"name\": \"fetch\",\n      \"status\": \"pending\",\n      \"started\": null,\n      \"finished\": null,\n      \"log\": \"\"\n    },"));
        assert!(!text.ends_with('\n'));
        assert!(!r.root.join(".progress.tmp").exists());
    }

    #[test]
    fn real_run_cmd_merges_stderr_and_reports_the_status() {
        let r = repos();
        let mut lines = vec![];
        let rc = real_run_cmd(
            &s(&["sh", "-c", "echo out; echo err >&2; exit 3"]),
            &r.root,
            &mut |l| lines.push(l.to_string()),
        );
        assert_eq!(rc, 3);
        assert_eq!(lines, ["out", "err"]);
        assert_ne!(
            real_run_cmd(&s(&["/nonexistent/binary"]), &r.root, &mut |_| {}),
            0
        );
    }
}
