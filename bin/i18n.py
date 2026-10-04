#!/usr/bin/env python3
"""Dashboard UI translations.

The English source string itself is the lookup key (gettext-style), so
untranslated call sites and missing catalog entries both fall back to
readable English instead of a raw key. Catalogs live next to this file in
locales/<lang>.json as flat {"English": "translation"} objects; English has
no catalog at all.

The active language is per-thread: the dashboard server is a
ThreadingMixIn server, so each request's handler sets it once up front
(see DashboardHandler._apply_language) and every render_* function it
calls reads it implicitly via t(), with no `lang` argument threaded
through dozens of signatures. A fresh thread (and every unit test that
never calls set_language) sees English.
"""

import json
import threading
from pathlib import Path

SUPPORTED_LANGUAGES = ("en", "ja", "zh", "fr")
DEFAULT_LANGUAGE = "en"
LANGUAGE_NAMES = {"en": "English", "ja": "日本語", "zh": "中文", "fr": "Français"}
COOKIE_NAME = "loop_lang"
LOCALES_DIR = Path(__file__).resolve().parent / "locales"

_HTML_LANG = {"zh": "zh-CN"}
_state = threading.local()
_catalogs = {}
_catalogs_lock = threading.Lock()


def _catalog(lang, locales_dir=None):
    if locales_dir is None:
        locales_dir = LOCALES_DIR
    key = (lang, str(locales_dir))
    with _catalogs_lock:
        if key not in _catalogs:
            try:
                _catalogs[key] = json.loads((Path(locales_dir) / f"{lang}.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _catalogs[key] = {}
        return _catalogs[key]


def set_language(lang):
    _state.lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def get_language():
    return getattr(_state, "lang", DEFAULT_LANGUAGE)


def html_lang(lang=None):
    if lang is None:
        lang = get_language()
    return _HTML_LANG.get(lang, lang)


def t(text, **kwargs):
    """Translate `text` into the current thread's language, then apply
    str.format(**kwargs) if any were given - formatting after lookup keeps
    the catalog key a stable template like "{count} runs"."""
    lang = get_language()
    if lang != DEFAULT_LANGUAGE:
        text = _catalog(lang).get(text) or text
    return text.format(**kwargs) if kwargs else text


def _cookie_language(cookie_header):
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value in SUPPORTED_LANGUAGES:
            return value
    return None


def _accept_language(accept_header):
    """First supported primary tag in Accept-Language, honoring q-values."""
    candidates = []
    for index, part in enumerate((accept_header or "").split(",")):
        tag, _, params = part.strip().partition(";")
        quality = 1.0
        params = params.strip()
        if params.startswith("q="):
            try:
                quality = float(params[2:])
            except ValueError:
                quality = 0.0
        primary = tag.strip().lower().split("-")[0]
        if primary in SUPPORTED_LANGUAGES and quality > 0:
            candidates.append((-quality, index, primary))
    return min(candidates)[2] if candidates else None


def resolve_language(cookie_header, accept_header):
    """An explicit choice (the loop_lang cookie the topbar's language menu
    sets) wins; otherwise the browser's Accept-Language; otherwise English."""
    return _cookie_language(cookie_header) or _accept_language(accept_header) or DEFAULT_LANGUAGE
