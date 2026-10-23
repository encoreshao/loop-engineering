"""Templates the Runs hub pages need (Loop Runs, Run History, Logs, run detail)."""
TEMPLATES = [
    ("dashboard_server", "_history_entry_html", "runs_history_entry"),
    ("dashboard_server", "_history_body", "runs_history_body"),
    ("dashboard_server", "_loop_runs_body", "runs_loop_runs_empty", {"contains": '<div class="page-title">', "pick": "shortest"}),
    ("dashboard_server", "_loop_runs_body", "runs_loop_runs_body", '<div class="page-title">'),
    ("dashboard_server", "_loop_runs_body", "runs_loop_run_row", "/loop-runs/"),
    ("dashboard_server", "_loop_runs_overview_html", "runs_overview", '<div class="grid">'),
    ("dashboard_server", "_loop_runs_overview_html", "runs_overview_tile", "dash-stat-tile"),
    ("dashboard_server", "render_loop_run_detail_page", "runs_detail_iteration", '<section class="card">'),
    ("dashboard_server", "render_loop_run_detail_page", "runs_detail_body", {"var": "body"}),
    ("dashboard_server", "_logs_body", "runs_logs_body"),
]
