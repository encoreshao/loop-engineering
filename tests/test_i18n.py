import ast
import json
import string
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
import i18n


@pytest.fixture(autouse=True)
def _reset_language():
    i18n.set_language("en")
    yield
    i18n.set_language("en")


def test_supported_languages_are_en_ja_zh_fr():
    assert i18n.SUPPORTED_LANGUAGES == ("en", "ja", "zh", "fr")


def test_english_is_identity_even_for_unknown_strings():
    assert i18n.t("Dashboard") == "Dashboard"
    assert i18n.t("Some string nobody translated") == "Some string nobody translated"


def test_translates_into_the_active_language():
    i18n.set_language("ja")
    assert i18n.t("Dashboard") == "ダッシュボード"
    i18n.set_language("zh")
    assert i18n.t("Dashboard") == "仪表盘"
    i18n.set_language("fr")
    assert i18n.t("Dashboard") == "Tableau de bord"


def test_missing_key_falls_back_to_english():
    i18n.set_language("fr")
    assert i18n.t("Some string nobody translated") == "Some string nobody translated"


def test_format_kwargs_apply_after_translation():
    i18n.set_language("fr")
    assert i18n.t("auto-refreshes every {interval}", interval="30s") == "actualisation auto toutes les 30s"
    i18n.set_language("en")
    assert i18n.t("auto-refreshes every {interval}", interval="30s") == "auto-refreshes every 30s"


def test_set_language_rejects_unsupported_by_falling_back_to_english():
    i18n.set_language("de")
    assert i18n.get_language() == "en"


def test_language_is_per_thread():
    i18n.set_language("ja")
    seen = []
    worker = threading.Thread(target=lambda: seen.append(i18n.get_language()))
    worker.start()
    worker.join()
    assert seen == ["en"]
    assert i18n.get_language() == "ja"


@pytest.mark.parametrize(
    "cookie, accept, expected",
    [
        ("loop_lang=fr", None, "fr"),
        ("other=1; loop_lang=zh", "ja", "zh"),
        ("loop_lang=xx", "ja-JP,ja;q=0.9", "ja"),
        (None, "zh-CN,zh;q=0.9,en;q=0.8", "zh"),
        (None, "de-DE,fr;q=0.8", "fr"),
        (None, "de-DE", "en"),
        (None, None, "en"),
        ("", "", "en"),
    ],
)
def test_resolve_language(cookie, accept, expected):
    assert i18n.resolve_language(cookie, accept) == expected


def test_language_names_are_native():
    assert i18n.LANGUAGE_NAMES == {"en": "English", "ja": "日本語", "zh": "中文", "fr": "Français"}


def test_html_lang_attribute():
    assert i18n.html_lang("en") == "en"
    assert i18n.html_lang("ja") == "ja"
    assert i18n.html_lang("zh") == "zh-CN"
    assert i18n.html_lang("fr") == "fr"


def _source_keys():
    """Every literal string passed as the first argument to _t() in the
    dashboard's web modules - the catalogs must cover all of them."""
    keys = set()
    for path in (ROOT / "bin" / "web").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def _catalog(lang):
    return json.loads((ROOT / "bin" / "locales" / f"{lang}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("lang", ["ja", "zh", "fr"])
def test_every_source_string_has_a_translation(lang):
    keys = _source_keys()
    assert keys, "expected the web modules to use _t()"
    catalog = _catalog(lang)
    missing = sorted(k for k in keys if not catalog.get(k))
    assert missing == []


@pytest.mark.parametrize("lang", ["ja", "zh", "fr"])
def test_translations_keep_the_same_format_placeholders(lang):
    formatter = string.Formatter()
    for source, translated in _catalog(lang).items():
        src_fields = {f for _, f, _, _ in formatter.parse(source) if f}
        dst_fields = {f for _, f, _, _ in formatter.parse(translated) if f}
        assert src_fields == dst_fields, (source, translated)


def _connector_message_keys():
    """Literal first args of i18n.t(...) in bin/connectors/*.py and
    bin/connectors_config.py."""
    keys = set()
    paths = list((ROOT / "bin" / "connectors").glob("*.py")) + [ROOT / "bin" / "connectors_config.py"]
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "t"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "i18n"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def _connector_label_keys():
    import connectors
    connectors._load_all()
    keys = set()
    for cls in connectors.CONNECTOR_TYPES.values():
        assert cls.label, cls
        keys.add(cls.label)
        if cls.description:
            keys.add(cls.description)
        for preset in cls.presets:
            keys.add(preset.label)
            if preset.description:
                keys.add(preset.description)
        if cls.secret_label:
            keys.add(cls.secret_label)
        for field in cls.fields:
            assert field.label, (cls.type, field.key)
            keys.add(field.label)
            if field.help:
                keys.add(field.help)
            # Placeholder rule: URLs, ids and example addresses are literal
            # and untranslated; a placeholder is prose (translated) only if
            # it contains a space.
            if " " in field.placeholder:
                keys.add(field.placeholder)
    return keys


@pytest.mark.parametrize("lang", ["ja", "zh", "fr"])
def test_connector_messages_have_translations(lang):
    keys = _connector_message_keys()
    assert keys, "expected the connector modules to use i18n.t()"
    catalog = _catalog(lang)
    assert sorted(k for k in keys if not catalog.get(k)) == []


@pytest.mark.parametrize("lang", ["ja", "zh", "fr"])
def test_connector_labels_have_translations(lang):
    keys = _connector_label_keys()
    assert len(keys) > 8
    catalog = _catalog(lang)
    assert sorted(k for k in keys if not catalog.get(k)) == []


@pytest.mark.parametrize("lang", ["ja", "zh", "fr"])
def test_loop_template_descriptions_have_translations(lang):
    template = json.loads((ROOT / "config" / "loops.json.template").read_text("utf-8"))
    descriptions = [e["description"] for e in template]
    assert len(descriptions) == len(template)  # every shipped loop has one
    catalog = _catalog(lang)
    assert sorted(d for d in descriptions if not catalog.get(d)) == []
