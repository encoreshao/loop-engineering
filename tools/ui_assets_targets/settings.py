"""Templates for the Settings hub (/settings views general/daemons/skills/chat and /settings/fragment)."""
G = "_general_settings_body"
TEMPLATES = [
    ("dashboard_server", G, "settings_notifications_panel", {"var": "notifications_panel"}),
    ("dashboard_server", G, "settings_ai_cli_panel", {"var": "ai_cli_panel"}),
    ("dashboard_server", G, "settings_appearance_panel", {"var": "appearance_panel"}),
    ("dashboard_server", G, "settings_instructions_panel", {"var": "instructions_panel"}),
    ("dashboard_server", G, "settings_general_body", {"var": "body"}),
    ("dashboard_server", "_skills_body", "settings_skills_body", {"var": "body"}),
    ("dashboard_server", "_daemons_body", "settings_daemons_body", {"var": "body"}),
    ("dashboard_server", "_chat_tools_body", "settings_chat_body"),
]
