"""Localized CLI messages; the workbench passes its task language through H3_LANG."""
import json
from pathlib import Path

from . import config

MESSAGES = {lang: json.loads((Path(__file__).parent / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("zh-CN", "en")}


def message(key, **values):
    lang = "en" if config.env("H3_LANG").lower().startswith("en") else "zh-CN"
    return MESSAGES[lang][key].format(**values)
