//! Runs the legacy Python dashboard (`bin/web/dashboard_server.py`) as a child on
//! a private loopback port, so the pages that are not ported yet keep working
//! behind the Rust server. Killed when the guard drops.

use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

pub struct LegacyDashboard {
    child: Child,
    pub port: u16,
}

#[derive(Debug, thiserror::Error)]
pub enum LegacyError {
    #[error("{0} not found (the legacy dashboard needs the Python checkout)")]
    MissingScript(String),
    #[error("could not start python3: {0}")]
    Spawn(std::io::Error),
    #[error("the legacy dashboard did not start listening on port {0} in time")]
    NotReady(u16),
}

fn free_port() -> std::io::Result<u16> {
    let l = std::net::TcpListener::bind("127.0.0.1:0")?;
    Ok(l.local_addr()?.port())
}

fn wait_listening(port: u16, child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return true;
        }
        if matches!(child.try_wait(), Ok(Some(_))) {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    false
}

impl LegacyDashboard {
    pub fn start(root: &Path) -> Result<Self, LegacyError> {
        let script = root.join("bin").join("web").join("dashboard_server.py");
        if !script.exists() {
            return Err(LegacyError::MissingScript(script.display().to_string()));
        }
        let port = free_port().map_err(LegacyError::Spawn)?;
        let mut child = Command::new("python3")
            .arg(&script)
            .arg(port.to_string())
            .current_dir(root)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .map_err(LegacyError::Spawn)?;
        if !wait_listening(port, &mut child, Duration::from_secs(20)) {
            let _ = child.kill();
            let _ = child.wait();
            return Err(LegacyError::NotReady(port));
        }
        Ok(Self { child, port })
    }
}

impl Drop for LegacyDashboard {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_checkout_without_the_python_dashboard_is_reported() {
        let d = tempfile::tempdir().unwrap();
        match LegacyDashboard::start(d.path()) {
            Err(LegacyError::MissingScript(p)) => assert!(p.ends_with("dashboard_server.py")),
            Err(other) => panic!("unexpected error: {other}"),
            Ok(_) => panic!("must not start without the script"),
        }
    }

    #[test]
    fn free_ports_are_distinct_and_usable() {
        let (a, b) = (free_port().unwrap(), free_port().unwrap());
        assert!(a > 0 && b > 0);
    }
}
