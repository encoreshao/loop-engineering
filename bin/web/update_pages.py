"""HTML for the self-update UI: the topbar chip, the banner shown above any
page when origin has something new, and the /update progress page. Pure
functions of the status/progress dicts (bin/update_check.py,
bin/update_runner.py) - dashboard_server.py wires the routes and passes in
the CSRF hidden input, since this module can't import it."""
import html
import json

import i18n

_t = i18n.t

_STEP_ORDER = ["fetch", "pull", "build", "install", "restart_dashboard", "relaunch"]
_STATUS_ICON = {"pending": "circle", "running": "autorenew", "done": "check_circle", "failed": "error"}
_IN_FLIGHT = ("starting", "running")
_TERMINAL = ("done", "failed", "refused")


def _step_label(name):
    if name == "fetch":
        return _t("Fetch changes")
    if name == "pull":
        return _t("Pull latest code")
    if name == "build":
        return _t("Build app and DMG")
    if name == "install":
        return _t("Install app")
    if name == "restart_dashboard":
        return _t("Restart dashboard")
    if name == "relaunch":
        return _t("Relaunch Loop X")
    return name


def _summary(status):
    if status.get("new_release") and status.get("latest_tag"):
        return _t("New release {tag} is available", tag=html.escape(status["latest_tag"]))
    return _t("{count} new change(s) on {branch}", count=int(status.get("behind", 0)),
              branch=html.escape(status.get("branch", "")))


def _start_form(csrf_input, label):
    return (f"<form method='post' action='/update/start'>{csrf_input}"
            f"<button type='submit' class='btn'>{html.escape(label)}</button></form>")


def render_chip(status):
    if not status.get("behind"):
        return ""
    return (f"<a class='pill' href='/update'><span class='material-symbols-outlined' aria-hidden='true'>"
            f"system_update</span>{html.escape(_t('Update available'))}</a>")


def _blocker(status):
    if not status.get("clean", True):
        return _t("You have local changes to tracked files; commit or stash them first.")
    if not status.get("can_fast_forward", True):
        return _t("This checkout has diverged from origin; resolve it with git first.")
    return None


def render_banner(status, progress, csrf_input):
    if progress.get("state") in _IN_FLIGHT:
        return (f"<div class='card'><p>{html.escape(_t('An upgrade is in progress.'))} "
                f"<a href='/update'>{html.escape(_t('View progress'))}</a></p></div>")
    if not status.get("behind"):
        return ""
    commits = "".join(f"<li><code>{html.escape(c.get('sha', ''))}</code> {html.escape(c.get('subject', ''))}</li>"
                      for c in status.get("commits", [])[:5])
    blocker = _blocker(status)
    action = (f"<p>{html.escape(blocker)}</p>" if blocker else _start_form(csrf_input, _t("Upgrade now")))
    check = (f"<form method='post' action='/update/check'>{csrf_input}"
             f"<button type='submit' class='btn btn-neutral'>{html.escape(_t('Check now'))}</button></form>")
    return (f"<div class='card'><h2>{html.escape(_t('Update available'))}</h2><p>{_summary(status)}</p>"
            f"<ul>{commits}</ul>{action}{check}</div>")


def _steps_html(progress):
    steps = progress.get("steps") or [{"name": n, "status": "pending", "log": ""} for n in _STEP_ORDER]
    items = []
    for step in steps:
        status = step.get("status", "pending")
        items.append(
            f"<li data-step='{html.escape(step.get('name', ''))}' class='step-{html.escape(status)}'>"
            f"<span class='material-symbols-outlined' aria-hidden='true'>"
            f"{_STATUS_ICON.get(status, 'circle')}</span> {html.escape(_step_label(step.get('name', '')))}"
            f"<pre>{html.escape(step.get('log', '') or '')}</pre></li>")
    return f"<ol id='update-steps'>{''.join(items)}</ol>"


def _poll_script():
    return (
        "<script>(function(){"
        "var terminal=" + json.dumps(list(_TERMINAL)) + ";"
        "function render(d){var steps=(d.progress&&d.progress.steps)||[];"
        "steps.forEach(function(s){var li=document.querySelector(\"[data-step='\"+s.name+\"']\");"
        "if(!li)return;li.className='step-'+s.status;var pre=li.querySelector('pre');"
        "if(pre)pre.textContent=s.log||'';});"
        "if(d.progress&&terminal.indexOf(d.progress.state)>=0){location.reload();return false;}return true;}"
        "function poll(){fetch('/update/progress',{cache:'no-store'})"
        ".then(function(r){return r.json();})"
        ".then(function(d){if(render(d))setTimeout(poll,1000);})"
        # The dashboard restarts mid-upgrade: keep polling through errors.
        ".catch(function(){setTimeout(poll,1500);});}"
        "poll();})();</script>"
    )


def render_update_body(status, progress, csrf_input):
    state = progress.get("state")
    parts = [f"<div class='card'><h1>{html.escape(_t('Update'))}</h1>"]
    if state == "done" and not status.get("behind"):
        parts.append(f"<p>{html.escape(_t('Updated to {version}', version=progress.get('to', '')))}</p>")
    elif state in ("failed", "refused"):
        parts.append(f"<p class='flash flash-danger'>{html.escape(progress.get('error') or '')}</p>")
        parts.append(_start_form(csrf_input, _t("Retry")))
    elif state in _IN_FLIGHT:
        parts.append(f"<p>{html.escape(_t('Upgrading Loop X - this window will relaunch when it is done.'))}</p>")
    elif status.get("behind"):
        parts.append(f"<p>{_summary(status)}</p>")
        blocker = _blocker(status)
        parts.append(f"<p>{html.escape(blocker)}</p>" if blocker else _start_form(csrf_input, _t("Upgrade now")))
    else:
        parts.append(f"<p>{html.escape(_t('Loop X is up to date.'))}</p>")
    parts.append("</div>")
    if progress.get("steps") or state in _IN_FLIGHT or state in _TERMINAL:
        parts.append(f"<div class='card'>{_steps_html(progress)}</div>")
    if state in _IN_FLIGHT:
        parts.append(_poll_script())
    return "".join(parts)
