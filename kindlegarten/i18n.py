"""Localize interface messages without adding dependencies to the CLI."""

import json
import locale
import os
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

LANGUAGE = ContextVar("language", default="")
SUPPORTED = ("en", "zh", "ja")


def language():
    value = LANGUAGE.get() or os.environ.get("KINDLEGARTEN_LANGUAGE")
    if not value:
        value = next((os.environ[name] for name in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG")
                      if os.environ.get(name)), locale.getlocale()[0] or "")
    code = value.lower().replace("-", "_").split("_")[0].split(".")[0].split(":")[0]
    return code if code in SUPPORTED else "en"


@lru_cache(maxsize=1)
def messages():
    path = Path(__file__).with_name("messages.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def tr(key, **values):
    chosen = language()
    translated = messages().get(key, {}).get(chosen, key)
    return translated.format(**values) if values else translated
