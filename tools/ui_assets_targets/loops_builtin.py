"""Templates of the three built-in loop pages (gitlab-loop, topic-loop, inbox-triage-loop)."""
TEMPLATES = [
    ("dashboard_server", "_gitlab_body", "gitlab_tasks_body"),
    ("dashboard_server", "_gitlab_projects_body", "gitlab_projects_body"),
    ("dashboard_server", "render_settings_fragment", "gitlab_settings_fragment", '\n<div class="grid">'),
    ("dashboard_server", "_topic_monitor_body", "topic_monitor_body", '\n<div class="page-title">'),
    ("dashboard_server", "_topic_monitor_body", "topic_latest_section", '\n<div class="grid">'),
    ("dashboard_server", "_topic_settings_body", "topic_settings_row", "\n<div class='project-block topic-settings-row"),
    ("dashboard_server", "_topic_settings_body", "topic_add_form", "\n<div class='project-block topic-settings-row'>"),
    ("dashboard_server", "_topic_settings_body", "topic_settings_body", '\n<div class="page-title">'),
]
