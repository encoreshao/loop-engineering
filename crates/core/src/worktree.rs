//! Per-issue git worktrees for the loop. Mirrors `bin/scripts/new_worktree.sh`:
//! branch `loop/issue-<iid>`, path `<worktree_root>/<repo>-issue-<iid>`.

use std::path::Path;
use std::process::Command;

/// A failure with the message to show and the exit code to end with.
#[derive(Debug)]
pub struct Failure {
    pub message: String,
    pub code: i32,
}

impl Failure {
    fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
            code: 1,
        }
    }
}

pub fn branch_name(issue_iid: &str) -> String {
    format!("loop/issue-{issue_iid}")
}

/// The worktree path exactly as the shell script built it (plain concatenation).
pub fn worktree_path(repo_path: &str, issue_iid: &str, worktree_root: &str) -> String {
    let name = Path::new(repo_path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    format!("{worktree_root}/{name}-issue-{issue_iid}")
}

/// Run `git -C <dir> <args>` with all of git's output sent to stderr, so
/// stdout carries only the final worktree path. Returns whether it succeeded.
fn git(dir: &str, args: &[&str]) -> Result<bool, Failure> {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .map_err(|e| Failure::new(format!("failed to run git: {e}")))?;
    eprint!("{}", String::from_utf8_lossy(&out.stdout));
    eprint!("{}", String::from_utf8_lossy(&out.stderr));
    Ok(out.status.success())
}

fn git_ok(dir: &str, args: &[&str]) -> Result<(), Failure> {
    if git(dir, args)? {
        Ok(())
    } else {
        Err(Failure::new(format!("git {} failed", args.join(" "))))
    }
}

fn git_stdout(dir: &str, args: &[&str]) -> Result<String, Failure> {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .output()
        .map_err(|e| Failure::new(format!("failed to run git: {e}")))?;
    if !out.status.success() {
        eprint!("{}", String::from_utf8_lossy(&out.stderr));
        return Err(Failure::new(format!("git {} failed", args.join(" "))));
    }
    Ok(String::from_utf8_lossy(&out.stdout).into_owned())
}

/// Create (or bring up to date) the worktree for an issue and return its path.
pub fn new_worktree(
    repo_path: &str,
    default_branch: &str,
    issue_iid: &str,
    worktree_root: &str,
) -> Result<String, Failure> {
    let branch = branch_name(issue_iid);
    let wt = worktree_path(repo_path, issue_iid, worktree_root);
    std::fs::create_dir_all(worktree_root)
        .map_err(|e| Failure::new(format!("cannot create {worktree_root}: {e}")))?;

    // Always start from the latest default branch, never stale code.
    git_ok(repo_path, &["fetch", "origin", default_branch])?;

    let ref_name = format!("refs/heads/{branch}");
    let origin_ref = format!("origin/{default_branch}");
    if git(repo_path, &["show-ref", "--verify", "--quiet", &ref_name])? {
        if !Path::new(&wt).is_dir() {
            git_ok(repo_path, &["worktree", "add", &wt, &branch])?;
        }
        // Merging into a dirty worktree would mix leftover edits into the sync.
        if !git_stdout(&wt, &["status", "--porcelain"])?
            .trim()
            .is_empty()
        {
            return Err(Failure::new(format!(
                "Worktree has uncommitted changes, refusing to sync: {wt}"
            )));
        }
        // On conflict abort the merge: a dangling MERGE_HEAD would wedge every later run.
        if !git(&wt, &["merge", "--no-edit", &origin_ref])? {
            git_ok(&wt, &["merge", "--abort"])?;
            return Err(Failure::new(format!(
                "Merge conflict pulling in {default_branch}, aborted: {wt}"
            )));
        }
    } else {
        git_ok(
            repo_path,
            &["worktree", "add", "-b", &branch, &wt, &origin_ref],
        )?;
    }

    // Gitignored per-developer files `git worktree add` never brings along;
    // copied on every run so later changes are picked up too.
    for rel in [".ruby-version", "config/database.yml"] {
        let src = Path::new(repo_path).join(rel);
        if src.is_file() {
            std::fs::copy(&src, Path::new(&wt).join(rel))
                .map_err(|e| Failure::new(format!("cannot copy {rel} into {wt}: {e}")))?;
        }
    }

    // Submodule checkouts are per-worktree; init them on every run.
    git_ok(&wt, &["submodule", "update", "--init", "--recursive"])?;
    Ok(wt)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn path_is_plain_concatenation_of_basename() {
        assert_eq!(
            worktree_path("/src/kurrant.web", "42", "/wt"),
            "/wt/kurrant.web-issue-42"
        );
        assert_eq!(worktree_path("/src/app/", "7", "/wt/"), "/wt//app-issue-7");
    }

    #[test]
    fn branch_naming() {
        assert_eq!(branch_name("9"), "loop/issue-9");
    }
}
