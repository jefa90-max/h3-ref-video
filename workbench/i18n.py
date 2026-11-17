"""Messages shared by the HTTP boundary and offline worker."""
import json
from pathlib import Path

MESSAGES = {lang: json.loads((Path(__file__).parent / "messages" / f"{lang}.json").read_text(encoding="utf-8"))
            for lang in ("zh-CN", "en")}


def message(key, lang="zh-CN", **values):
    return MESSAGES[lang][key].format(**values)


class InputError(ValueError):
    def __init__(self, key, **values):
        self.key, self.values = key, values
        super().__init__(key)

    def localized(self, lang):
        values = dict(self.values)
        if "reason" in values:
            values["reason"] = message(values["reason"], lang, **values)
        return message(self.key, lang, **values)
