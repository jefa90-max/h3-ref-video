"""Localized CLI messages; the workbench passes its task language through H3_LANG or use_lang()."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path

MESSAGES = {lang: json.loads((Path(__file__).parent / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("zh-CN", "en")}
_LANG: ContextVar[str | None] = ContextVar("h3_lang", default=None)


@contextmanager
def use_lang(lang: str | None):
    token = _LANG.set(lang)
    try:
        yield
    finally:
        _LANG.reset(token)


def message(key, **values):
    raw = _LANG.get() or os.environ.get("H3_LANG", "").strip()
    lang = "en" if raw.lower().startswith("en") else "zh-CN"
    return MESSAGES[lang][key].format(**values)
