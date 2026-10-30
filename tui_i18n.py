"""Translation catalogs and display-language preferences for the terminal UI."""

import json
import os
from pathlib import Path
import tempfile


LANGUAGES = {"ru": "Русский", "en": "English"}
DEFAULT_LANGUAGE = "ru"
CATALOG_DIR = Path(__file__).parent / "locales" / "tui"
PREFERENCES_FILE = Path.home() / ".cc" / "tui.json"
_language = DEFAULT_LANGUAGE
_catalogs = {}


def set_language(language):
    """`set_language(language)` selects a supported TUI language, defaulting to Russian."""
    global _language
    _language = language if language in LANGUAGES else DEFAULT_LANGUAGE


def get_language():
    """`get_language()` is the active TUI language code."""
    return _language


def tr(message, *, language=None):
    """`tr(message, language=None)` is MESSAGE in LANGUAGE, with the English source as fallback.

    Omitted LANGUAGE uses the active TUI language; an explicit language does not
    change it. Translate literal templates before formatting them with user content.
    """
    language = _language if language is None else language
    if language not in LANGUAGES:
        language = DEFAULT_LANGUAGE
    if language not in _catalogs:
        try:
            catalog = json.loads((CATALOG_DIR / f"{language}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            catalog = {}
        _catalogs[language] = catalog if isinstance(catalog, dict) else {}
    translated = _catalogs[language].get(message)
    return translated if isinstance(translated, str) and translated else message


def load_language(path=None):
    """`load_language(path)` is the saved TUI language, or Russian for absent or invalid preferences."""
    path = Path(path) if path is not None else PREFERENCES_FILE
    try:
        preferences = json.loads(path.read_text(encoding="utf-8"))
        language = preferences.get("language") if isinstance(preferences, dict) else None
    except (OSError, ValueError):
        return DEFAULT_LANGUAGE
    return language if isinstance(language, str) and language in LANGUAGES else DEFAULT_LANGUAGE


def save_language(language, path=None):
    """`save_language(language, path)` atomically persists a supported TUI language.

    Invalid languages raise ValueError; filesystem errors propagate to the caller.
    The orchestrator's state file is never modified.
    """
    if language not in LANGUAGES:
        raise ValueError(f"Unsupported TUI language: {language}")
    path = Path(path) if path is not None else PREFERENCES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".tui-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"language": language}, stream)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
