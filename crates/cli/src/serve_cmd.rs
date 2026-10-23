//! `loopx serve`: the dashboard backend and the API the Mac app talks to.

use loopx_core::runner_util::repo_root;
use loopx_server::{lock, serve, ServeError, ServeOptions};
use std::process::ExitCode;

const USAGE: &str = "Usage: loopx serve [--port N] [--no-legacy]\n\n  --port N      listen on 127.0.0.1:N (default 8420; 0 picks a free port)\n  --no-legacy   do not start the legacy Python dashboard for unported pages";

const DEFAULT_PORT: u16 = 8420;

pub fn run(args: &[String]) -> ExitCode {
    let mut port = DEFAULT_PORT;
    let mut legacy = true;
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        match arg.as_str() {
            "--port" => match it.next().and_then(|v| v.parse().ok()) {
                Some(p) => port = p,
                None => return fail("--port needs a number"),
            },
            "--no-legacy" => legacy = false,
            "-h" | "--help" => {
                println!("{USAGE}");
                return ExitCode::SUCCESS;
            }
            other => return fail(&format!("unknown argument {other}\n{USAGE}")),
        }
    }
    let runtime = match tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
    {
        Ok(r) => r,
        Err(e) => return fail(&format!("cannot start the async runtime: {e}")),
    };
    let mut opts = ServeOptions::new(repo_root(), port, legacy);
    // The checkout holding the legacy Python dashboard, when it is not the root.
    opts.legacy_root = std::env::var_os("LOOPX_LEGACY_ROOT")
        .filter(|v| !v.is_empty())
        .map(std::path::PathBuf::from);
    let lock_path = opts.lock_path.clone();
    let result = runtime.block_on(serve(opts, |bound| {
        println!(
            "loopx serving at http://127.0.0.1:{bound}/ (lock: {})",
            lock_path.display()
        );
    }));
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(ServeError::Lock(lock::AcquireError::AlreadyRunning(l))) => fail(&format!(
            "another loopx backend is already running: pid {}, http://127.0.0.1:{}/, version {}",
            l.pid, l.port, l.version
        )),
        Err(e) => fail(&e.to_string()),
    }
}

fn fail(msg: &str) -> ExitCode {
    eprintln!("{msg}");
    ExitCode::from(1)
}
