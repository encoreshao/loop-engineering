#!/usr/bin/env python3
"""Pipeline Doctor loop on LoopKit: finds failed CI pipelines (default branch
of tracked projects, plus the user's own open MRs), has the model diagnose
each from the failed jobs' log tails, and reports the diagnosis through the
loop's notifier. Notify only: it never writes to GitLab. Failures whose
fingerprint keeps recurring within 7 days are flagged. Run by
run-loop-now.sh with the run id as argv[1] (--force re-diagnoses)."""
import hashlib
import json
import os
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import loopkit  # noqa: E402

TRACE_TAIL_LINES = 300
MAX_JOBS = 3
MAX_TRACE_CHARS = 20_000
RECURRING_WINDOW_DAYS = 7
RECURRING_THRESHOLD = 3
LOOKBACK_HOURS = 26
CATEGORIES = ("flaky", "infra", "test_failure", "lint", "build", "dependency", "config", "unknown")
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SECTION = re.compile(r"section_(?:start|end):\d+:[^\r\n]*\r?")


def _err(exc):
    # Class name only: str(exc) can carry URLs or tokens.
    return type(exc).__name__


def trace_tail(text, n=TRACE_TAIL_LINES):
    """Last n lines of a CI log with ANSI escapes and GitLab section markers removed."""
    cleaned = _ANSI.sub("", _SECTION.sub("", str(text or "")))
    cleaned = _ANSI.sub("", cleaned.replace("\r\n", "\n").replace("\r", "\n"))
    lines = cleaned.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[-n:] if n > 0 else [])


def fingerprint(job_name, explanation_first_line):
    raw = f"{job_name}\n{explanation_first_line}".encode()
    return hashlib.sha1(raw).hexdigest()[:12]


class FingerprintStore:
    """{fingerprint: [iso timestamps]} kept for 7 days, written atomically."""

    def __init__(self, path, now_fn=None):
        self.path = Path(path)
        self._now_fn = now_fn

    def _now(self):
        return self._now_fn() if self._now_fn else datetime.now(timezone.utc)

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record(self, fp):
        now = self._now()
        cutoff = now - timedelta(days=RECURRING_WINDOW_DAYS)
        fresh = {}
        for key, stamps in self._load().items():
            kept = []
            for stamp in stamps if isinstance(stamps, list) else []:
                try:
                    when = datetime.fromisoformat(stamp)
                except (TypeError, ValueError):
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when >= cutoff:
                    kept.append(when.isoformat())
            if kept:
                fresh[key] = kept
        fresh.setdefault(fp, []).append(now.isoformat())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(fresh, indent=2))
        os.replace(tmp, self.path)
        return len(fresh[fp]) >= RECURRING_THRESHOLD


def default_projects():
    """Tracked projects from loop_config, as {alias, gitlab_instance, project_id
    (id or path), default_branch (or None)}."""
    import loop_config
    config = loop_config.load_config()
    rows = []
    for alias, project in (config.get("projects") or {}).items():
        rows.append({"alias": alias,
                     "gitlab_instance": project.get("instance") or config.get("gitlab_instance"),
                     "project_id": project.get("project_id"),
                     "default_branch": project.get("default_branch")})
    return rows


def _quote(value):
    return urllib.parse.quote(str(value), safe="")


class PipelineDoctor(loopkit.LoopPlugin):
    loop_name = "pipeline-doctor-loop"
    definition_dir = "pipeline-doctor"
    output_keys = ("category", "culprit", "explanation", "suggested_fix", "confidence")
    max_items_per_run = 10

    def __init__(self, projects_fn=None, accounts_fn=None, loader=None, state_dir=None):
        self._projects_fn = projects_fn
        self._accounts_fn = accounts_fn
        self._loader = loader
        self._state_dir = state_dir

    def _resolve(self):
        import connectors_config
        return (self._projects_fn or default_projects,
                self._accounts_fn or connectors_config.accounts_with_capability,
                self._loader or connectors_config.load_connector)

    def discover(self, ctx):
        projects_fn, accounts_fn, loader = self._resolve()
        try:
            projects = projects_fn()
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"pipeline-doctor: project config failed: {_err(exc)}")
            return []
        accounts = [a for a in accounts_fn("pipelines") if a.get("type") == "gitlab"]
        since = (ctx.now - timedelta(hours=LOOKBACK_HOURS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        items, seen = [], set()
        for account in accounts:
            aid = account["id"]
            mine = [p for p in projects if p.get("gitlab_instance") == aid]
            if not mine:
                continue
            try:
                conn = loader(aid)
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"pipeline-doctor: account {aid} failed: {_err(exc)}")
                continue
            names = {}
            found = []  # (project_id, project label, pipeline dict)
            for project in mine:
                try:
                    pid, branch = self._project_ref(conn, project)
                    names[str(pid)] = project["alias"]
                    rows = conn.api("GET", f"/projects/{pid}/pipelines?status=failed&ref={_quote(branch)}"
                                           f"&updated_after={since}&per_page=20")
                    found += [(pid, project["alias"], r) for r in rows or [] if isinstance(r, dict)]
                except Exception as exc:  # noqa: BLE001 - one project failing never stops the rest
                    ctx.log(f"pipeline-doctor: project {project.get('alias')} failed: {_err(exc)}")
            try:
                found += self._my_mr_pipelines(conn, names)
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"pipeline-doctor: account {aid} MRs failed: {_err(exc)}")
            for pid, label, pipe in found:
                key = f"pipe:{aid}:{pid}#{pipe.get('id')}"
                if key in seen or pipe.get("id") is None:
                    continue
                seen.add(key)
                try:
                    items.append(self._item(conn, key, aid, pid, label, pipe))
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"pipeline-doctor: pipeline #{pipe.get('id')} failed to load: {_err(exc)}")
        return items

    def _project_ref(self, conn, project):
        pid, branch = project["project_id"], project.get("default_branch")
        if not str(pid).isdigit() or not branch:
            info = conn.api("GET", f"/projects/{_quote(pid)}")
            pid = info["id"] if not str(pid).isdigit() else pid
            branch = branch or info.get("default_branch") or "main"
        return pid, branch

    def _my_mr_pipelines(self, conn, names):
        me = conn.api("GET", "/user")
        rows = conn.api("GET", f"/merge_requests?author_username={_quote(me['username'])}"
                               "&state=opened&scope=all&per_page=50")
        out = []
        for mr in rows or []:
            pipe = mr.get("head_pipeline")
            if isinstance(pipe, dict) and pipe.get("status") == "failed":
                pid = mr["project_id"]
                out.append((pid, names.get(str(pid)) or f"project {pid}", pipe))
        return out

    def _item(self, conn, key, aid, pid, label, pipe):
        jobs = conn.api("GET", f"/projects/{pid}/pipelines/{pipe['id']}/jobs?scope[]=failed") or []
        out = []
        for job in jobs[:MAX_JOBS]:
            try:
                trace = trace_tail(conn.api_text(f"/projects/{pid}/jobs/{job['id']}/trace"))[-MAX_TRACE_CHARS:]
            except Exception:  # noqa: BLE001 - a missing trace still leaves the job name
                trace = ""
            out.append({"name": str(job.get("name") or ""), "stage": str(job.get("stage") or ""),
                        "web_url": str(job.get("web_url") or ""), "trace_tail": trace})
        url = str(pipe.get("web_url") or "")
        return loopkit.WorkItem(
            key=key, title=f"{label} #{pipe['id']}", url=url,
            payload={"account": aid, "project": label, "project_id": pid, "pipeline_id": pipe["id"],
                     "web_url": url, "jobs": out})

    def _store(self, ctx):
        base = Path(self._state_dir) if self._state_dir else (
            Path(ctx.repo_root) / "outputs" / "loops" / self.loop_name)
        return FingerprintStore(base / "fingerprints.json", now_fn=lambda: ctx.now)

    def after_item(self, item, answer, ctx):
        payload = item.payload
        category = answer.get("category")
        category = category if category in CATEGORIES else "unknown"
        culprit = answer.get("culprit")
        culprit = culprit.strip()[:200] if isinstance(culprit, str) else ""
        explanation = str(answer.get("explanation") or "").strip()[:1000]
        fix = str(answer.get("suggested_fix") or "").strip()[:200]
        try:
            confidence = min(1.0, max(0.0, float(answer.get("confidence"))))
        except (TypeError, ValueError):
            confidence = 0.0
        jobs = payload.get("jobs") or []
        first_job = jobs[0]["name"] if jobs else ""
        first_line = (explanation.splitlines() or [""])[0]
        recurring = self._store(ctx).record(fingerprint(first_job, first_line))
        summary = f"[{category}] {payload.get('project')} #{payload.get('pipeline_id')}: {culprit}"
        return loopkit.Outcome(
            item.key, "done", summary, url=item.url or payload.get("web_url", ""),
            data={"category": category, "culprit": culprit, "explanation": explanation,
                  "suggested_fix": fix, "confidence": confidence, "recurring": recurring})

    def digest(self, outcomes, ctx):
        done = [o for o in outcomes if o.status == "done"]
        if not done:
            return None
        recurring = [o for o in done if o.data.get("recurring")]
        lines = [f"Pipeline Doctor: {len(done)} failed pipelines diagnosed ({len(recurring)} recurring)."]

        def line(o, mark):
            text = f"{mark}{loopkit.chat_link(o.summary, o.url)}"
            fix = loopkit.chat_text(o.data.get("suggested_fix") or "", 200)
            return text + (f" - fix: {fix}" if fix else "")

        if recurring:
            lines.append("Recurring:")
            lines += ["- " + line(o, "\U0001F501 ") for o in recurring]
        rest = [o for o in done if not o.data.get("recurring")]
        for cat in sorted({str(o.data.get("category") or "unknown") for o in rest}):
            lines.append(f"{loopkit.chat_text(cat)}:")
            lines += ["- " + line(o, "") for o in rest if str(o.data.get("category") or "unknown") == cat]
        return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(loopkit.main(PipelineDoctor()))
