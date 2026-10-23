//! `loopx update` (replaces `bin/update_check.py` and `bin/update_runner.py`):
//! the self-update of this checkout and the macOS app.
//!
//! State lives in `$LOOP_UPDATE_DIR` (else `<repo root>/outputs/update`):
//! `status.json`, `progress.json`, `notified.json`, `app_path.txt`,
//! `upgrade.lock` (see docs/contract/json-files.md).

use loopx_core::pyjson;
use loopx_core::{update_check, update_runner};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::process::ExitCode;

const USAGE: &str = "Usage: loopx update run [--repo DIR] [--state-dir DIR] [--install-target APP]\n       loopx update check [--repo DIR] [--state-dir DIR] [--no-fetch]\n       loopx update status [--state-dir DIR]\n       loopx update start [--repo DIR] [--state-dir DIR]\n       loopx update notification [--state-dir DIR]";

/// `--name VALUE` or `--name=VALUE` anywhere in `args`.
fn flag(args: &[String], name: &str) -> Option<String> {
    let prefix = format!("{name}=");
    let mut it = args.iter();
    while let Some(a) = it.next() {
        if a == name {
            return it.next().cloned();
        }
        if let Some(v) = a.strip_prefix(&prefix) {
            return Some(v.to_string());
        }
    }
    None
}

fn print_json(v: &Value) {
    println!("{}", pyjson::to_string_pretty(v));
}

pub fn run(args: &[String]) -> ExitCode {
    let Some((cmd, rest)) = args.split_first() else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let repo = flag(rest, "--repo").map(PathBuf::from);
    let state = flag(rest, "--state-dir").map(PathBuf::from);
    match cmd.as_str() {
        "run" => {
            let target = flag(rest, "--install-target").map(PathBuf::from);
            let progress =
                update_runner::run(repo.as_deref(), state.as_deref(), target.as_deref(), None);
            ExitCode::from(u8::from(!update_runner::is_done(&progress)))
        }
        "check" => {
            let fetch = !rest.iter().any(|a| a == "--no-fetch");
            let status = update_check::check(repo.as_deref(), state.as_deref(), fetch);
            print_json(&Value::Object(status));
            ExitCode::SUCCESS
        }
        "status" => {
            let status = update_check::read_status(state.as_deref());
            let progress = update_runner::read_progress(state.as_deref());
            print_json(&json!({"status": status, "progress": progress}));
            ExitCode::SUCCESS
        }
        "start" => {
            let progress = update_runner::start_detached(repo.as_deref(), state.as_deref(), None);
            let refused = progress.get("state") == Some(&json!("refused"));
            print_json(&Value::Object(progress));
            ExitCode::from(u8::from(refused))
        }
        "notification" => {
            if let Some(text) = update_check::take_notification(state.as_deref()) {
                println!("{text}");
            }
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}
