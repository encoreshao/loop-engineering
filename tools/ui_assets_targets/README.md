Each `*.py` file here may define `TEMPLATES = [(module, function, template_name[, selector]), ...]`;
tools/extract_ui_assets.py turns the named function's largest f-string into
crates/server/assets/templates/<template_name>.tpl. See that script's docstring.
