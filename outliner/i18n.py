# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Julio Angulo
"""Plugin-owned catalogs; never change the host language or document names."""
import json
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QSettings
from core.i18n import current_language

SUPPORTED = ("es", "en", "fr")

# The saved choice ("auto", "es", "en", "fr"), read from QSettings once.
# ``tr`` runs thousands of times per tree refresh, and building a QSettings
# for each call made a refresh of a 300-object model cost ~70 ms (1.4 s with
# 6,000 objects) — every 220 ms while a tool edits the model.
_choice = None


def invalidate():
    """Forget the cached choice (call after writing ``outliner/language``)."""
    global _choice
    _choice = None


def language():
    global _choice
    if _choice is None:
        _choice = QSettings().value("outliner/language", "auto", type=str)
    choice = _choice
    if choice == "auto":
        choice = current_language()
    code = choice.replace("_", "-").split("-")[0].lower()
    return code if code in SUPPORTED else "en"


@lru_cache(maxsize=3)
def catalog(code):
    return json.loads((Path(__file__).parent / "locales" / f"{code}.json").read_text(encoding="utf-8"))


def tr(text, **values):
    translated = catalog(language()).get(text, text)
    return translated.format(**values) if values else translated
