"""Templates the shared page shell needs (sidebar and its links)."""
TEMPLATES = [
    ("dashboard_server", "_sidebar_html", "sidebar"),
    ("dashboard_server", "_nav_link", "nav_link"),
]
TEMPLATES += [
    ("dashboard_server", "_render_shell", "shell_refresh_schedule", {"var": "refresh_schedule_script"}),
    ("dashboard_server", "_render_shell", "shell_refresh_note", {"var": "refresh_note_script"}),
    ("dashboard_server", "_render_shell", "shell_refresh_action_lazy", {"var": "refresh_action", "pick": "longest"}),
    ("dashboard_server", "_render_shell", "shell_refresh_action_reload", {"var": "refresh_action", "pick": "shortest"}),
]
TEMPLATES += [
    ("dashboard_server", "_render_shell", "shell_lang_option", {"var": "lang_options_html"}),
    ("dashboard_server", "_render_shell", "shell_lang_switch", {"var": "lang_switch_html"}),
    ("dashboard_server", "_render_shell", "shell_refresh_html", {"var": "refresh_html"}),
    ("dashboard_server", "_render_shell", "shell_ai_trigger", {"var": "ai_trigger_html"}),
    ("dashboard_server", "_render_shell", "shell_help_link", {"var": "help_link_html"}),
    ("dashboard_server", "_render_shell", "shell_ai_cli_badge", {"var": "ai_cli_badge_html"}),
    ("dashboard_server", "_ai_panel_html", "ai_panel_prompt", {"var": "prompts_html"}),
    ("dashboard_server", "_ai_panel_html", "ai_panel_context", {"var": "context_html"}),
]
