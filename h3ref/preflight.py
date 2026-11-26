"""Deterministic dialogue-budget estimation, independent of generation settings."""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata


MAX_SECONDS = 15
ESTIMATOR_VERSION = "1"
DOC_ANCHOR = "docs/workbench.md#dialogue-preflight"
DIALOGUE_RATE_CONFIG = {
    "chars": {"regular_rate": 6.0, "slow_rate": 3.5, "long_line_limit": 24},
    "words": {"regular_rate": 3.2, "slow_rate": 2.0, "long_line_limit": 18},
}
_FULLWIDTH = {0x3000: " ", **{n: n - 0xFEE0 for n in range(0xFF01, 0xFF5F)}}
_HAN = r"\u3400-\u4dbf\u4e00-\u9fff\u3005"
_KANA_HANGUL = r"\u3040-\u30ff\uac00-\ud7af"
_LATIN = r"A-Za-zÀ-ÖØ-öø-ÿ"
_CHAR_PATTERN = re.compile(rf"[{_HAN}{_KANA_HANGUL}0-9]|[A-Za-z]+")
_WORD_PATTERN = re.compile(rf"[0-9{_LATIN}]+(?:['’][0-9{_LATIN}]+)*|[{_HAN}]")
_COVERED_CHARS = re.compile(rf"[{_HAN}{_KANA_HANGUL}A-Za-z]")
_COVERED_WORDS = re.compile(rf"[{_HAN}{_LATIN}]")
_SLOW_CUE = re.compile(
    r"耳语|低语|低声|轻声|气声|哽咽|一字一顿|一字一句|断续|拖长|放缓|"
    r"\b(?:whisper(?:s|ing|ed)?|murmur(?:s|ing|ed)?|softly|slowly|chok(?:e|es|ing|ed)|sob(?:s|bing|bed)?)\b",
    re.IGNORECASE,
)
_BRACKET_PREFIX = re.compile(r"^(?:\([^()\[\]【】]{1,20}\)|\[[^()\[\]【】]{1,20}\]|【[^()\[\]【】]{1,20}】)\s*")
_ROLE_PREFIX = re.compile(r"^(?:旁白|画外音|路人[甲乙丙0-9]?|众人|Narrator|VO|OS)\s*:\s*", re.IGNORECASE | re.ASCII)
_PAUSES = re.compile(r"(?P<ellipsis>……|\.{3,}|…)|(?P<dash>——|--)|(?P<stop>[。？！.?!]+)|(?P<comma>[，、,]+)")
_PAUSE_MS = {"ellipsis": 800, "dash": 600, "stop": 400, "comma": 200}
_CREDENTIAL = re.compile(
    r"\b(?:bearer\s+|sk-)[\w./+=-]+|"
    r"""\b[A-Z_]*(?:API_KEY|OAUTH_TOKEN|AUTH_TOKEN|AUTHTOKEN|SECRET)\s*[:=]\s*['"]?[^\s'",;]+""",
    re.IGNORECASE,
)


class FilmValidationError(ValueError):
    def __init__(self, code, *, field, actual=None, shot_index=None, **values):
        from .i18n import message

        self.code = code
        self.field = field
        self.actual = actual
        self.shot_index = shot_index
        self.doc_anchor = DOC_ANCHOR
        self.values = {"field": field, "actual": actual, **values}
        self.fix = message(code, **self.values)
        super().__init__(f"{code}: {self.fix}")


def max_shots() -> int:
    return max(1, int(os.environ.get("H3_MAX_SHOTS", "").strip() or "1000"))


def _text(value):
    return value if isinstance(value, str) else ""


def normalize_speech(raw) -> str:
    return re.split(r"[-_]", _text(raw).strip().lower(), maxsplit=1)[0]


def _timestamp(raw, field, index, strict, limit):
    if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
        try:
            value = float(raw)
        except (ValueError, OverflowError):
            value = math.nan
        if 0 <= value <= limit:
            return value
    if strict:
        raise FilmValidationError("film_timestamp_invalid", field=f"shots[{index}].{field}",
                                  actual=repr(raw)[:80], shot_index=index, limit=limit)
    return None


def _normalized_shots(shots, *, strict, normalized_times=False):
    if not isinstance(shots, list):
        if strict:
            raise FilmValidationError("dialogue_shots_invalid", field="shots")
        return []
    normalized = []
    time = 0.0
    # Saved workbench timestamps are cumulative. Only raw script timestamps have the per-film input bound.
    limit = 3600 * max(1, len(shots)) if normalized_times else 3600
    for index, shot in enumerate(shots):
        if not isinstance(shot, dict) or not _text(shot.get("action")).strip():
            continue
        start, end = shot.get("start"), shot.get("end")
        if start is not None and end is not None:
            start = _timestamp(start, "start", index, strict, limit)
            end = _timestamp(end, "end", index, strict, limit)
        default = start is None or end is None
        duration = 2.0 if default else max(0.5, end - start)
        next_time = round(time + duration, 2)
        normalized_shot = {**shot, "start": time, "end": next_time}
        if "line" in normalized_shot:
            normalized_shot["line"] = _text(normalized_shot["line"])
        normalized.append((index, normalized_shot, default))
        time = next_time
    if strict:
        if not normalized:
            raise FilmValidationError("film_shots_empty", field="shots")
        limit = max_shots()
        if len(normalized) > limit:
            raise FilmValidationError("film_shots_limit", field="shots", actual=len(normalized), limit=limit)
    return normalized


def normalize_shots(shots, *, strict=False, normalized_times=False) -> list[dict]:
    return [shot for _, shot, _ in _normalized_shots(shots, strict=strict, normalized_times=normalized_times)]


def _config_error(field):
    return FilmValidationError("dialogue_rate_config_invalid", field=field)


def _read_rate_config(raw):
    try:
        if raw.lstrip().startswith("{"):
            return json.loads(raw)
        path = Path(raw).expanduser()
        if not stat.S_ISREG(path.stat().st_mode):
            raise _config_error("H3_DIALOGUE_RATE_CONFIG")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise _config_error("H3_DIALOGUE_RATE_CONFIG")
            content = stream.read(65537)
        if len(content) > 65536:
            raise _config_error("H3_DIALOGUE_RATE_CONFIG")
        return json.loads(content.decode("utf-8-sig"))
    except (OSError, ValueError, RuntimeError, RecursionError) as exc:
        if isinstance(exc, FilmValidationError):
            raise
        raise _config_error("H3_DIALOGUE_RATE_CONFIG") from exc


def _effective_rate_config(override, environ):
    if override is None:
        raw = environ.get("H3_DIALOGUE_RATE_CONFIG", "").strip()
        if not raw:
            return DIALOGUE_RATE_CONFIG, "provisional"
        override = _read_rate_config(raw)
    if not isinstance(override, dict) or override.keys() - DIALOGUE_RATE_CONFIG.keys():
        raise _config_error("rate_config")
    merged = {unit: dict(settings) for unit, settings in DIALOGUE_RATE_CONFIG.items()}
    for unit, settings in override.items():
        if not isinstance(settings, dict) or settings.keys() - merged[unit].keys():
            raise _config_error(f"rate_config.{unit}")
        for field, value in settings.items():
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            except OverflowError:
                valid = False
            if not valid or (field == "long_line_limit" and not float(value).is_integer()):
                raise _config_error(f"rate_config.{unit}.{field}")
            merged[unit][field] = int(value) if field == "long_line_limit" else float(value)
    return merged, "custom"


def _speaker_prefix(cast):
    names = set()
    for person in cast if isinstance(cast, list) else []:
        if not isinstance(person, dict):
            continue
        aliases = person.get("aliases")
        for label in [person.get("name"), *(aliases if isinstance(aliases, list) else [])]:
            label = _text(label).translate(_FULLWIDTH).strip()
            if 0 < len(label) <= 64:
                names.add(label)
    if names:
        alternatives = "|".join(re.escape(name) for name in sorted(names, key=lambda name: (-len(name), name)))
        return re.compile(rf"^(?:{alternatives})\s*:\s*")
    return None


def _strip_prefixes(line, speaker):
    text = line.translate(_FULLWIDTH).strip()
    for _ in range(5):
        match = _BRACKET_PREFIX.match(text) or (speaker and speaker.match(text)) or _ROLE_PREFIX.match(text)
        if not match:
            break
        text = text[match.end():].lstrip()
    return text


def sanitize_line_preview(text: str) -> str:
    text = "".join(" " if ch.isspace() else ch for ch in text
                   if ch.isspace() or unicodedata.category(ch)[0] != "C")
    text = _CREDENTIAL.sub("[…]", text)
    text = " ".join(text.split())
    return text[:39] + "…" if len(text) > 40 else text


def escape_markdown_cell(text: str) -> str:
    text = " ".join(text.splitlines())
    return re.sub(r"([\\|`*_\[\]<>])", r"\\\1", text).replace("&", "&amp;")


def _warning(code, shot=None, **fields):
    return {"code": code, "severity": "warn", "shot": shot, **fields}


def _estimate_seconds(count, rate, pause_ms):
    raw_min = count / rate + pause_ms / 1000
    if math.isfinite(raw_min * 10):
        return raw_min, math.ceil(raw_min * 10 - 1e-9) / 10
    # Subnormal custom rates can exceed the range of a float. Round exactly.
    numerator, denominator = rate.as_integer_ratio()
    scaled_numerator = count * denominator * 1000 + pause_ms * numerator
    rounded = -(-scaled_numerator // (1000 * numerator))
    return raw_min, rounded


def inspect_dialogue_budget(film: dict, *, rate_config: dict | None = None) -> dict:
    return inspect_with_env(film, os.environ, rate_config=rate_config)


def inspect_with_env(film, environ, *, rate_config=None, normalized_times=False):
    if not isinstance(film, dict):
        raise FilmValidationError("film_object_invalid", field="film")
    rates, calibration_status = _effective_rate_config(rate_config, environ)
    raw_shots = film.get("shots")
    normalized = _normalized_shots(raw_shots, strict=False, normalized_times=normalized_times)
    warnings = []
    total = len(raw_shots) if isinstance(raw_shots, list) else 0
    meta = {
        "estimator_version": ESTIMATOR_VERSION,
        "effective_config_sha256": hashlib.sha256(json.dumps(rates, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "calibration_status": calibration_status,
        "shots_invalid": raw_shots is not None and not isinstance(raw_shots, list),
        "shots_missing": raw_shots is None,
        "total_raw_shots": total,
        "checked_shots": len(normalized),
        "skipped_shots": total - len(normalized),
        "default_duration_shots": sum(default for _, _, default in normalized),
        "unsupported_script_shots": 0,
    }
    if meta["shots_invalid"] or meta["shots_missing"]:
        if meta["shots_invalid"]:
            warnings.append(_warning("dialogue_shots_invalid"))
        return {"status": "invalid", "warnings": warnings, "meta": meta}

    status = "partial" if not normalized or meta["skipped_shots"] or meta["default_duration_shots"] else "complete"
    spoken = [(number, index, shot, _text(shot.get("line"))) for number, (index, shot, _) in enumerate(normalized, 1)
              if _text(shot.get("line")).strip()]
    speech = normalize_speech(film.get("speech"))
    if not speech and spoken:
        warnings.append(_warning("dialogue_speech_unset",
                                 affected_shots=[number for number, _, _, _ in spoken],
                                 affected_raw_shots=[index + 1 for _, index, _, _ in spoken]))
        status = "partial"
    elif spoken:
        unit = "chars" if speech in {"zh", "ja", "ko"} else "words"
        counter, covered = (_CHAR_PATTERN, _COVERED_CHARS) if unit == "chars" else (_WORD_PATTERN, _COVERED_WORDS)
        settings = rates[unit]
        speaker = _speaker_prefix(film.get("cast"))
        checked_lines = 0
        for number, index, shot, line in spoken:
            band = "slow" if _SLOW_CUE.search(shot["action"] + "\n" + line) else "regular"
            text = _strip_prefixes(line, speaker)
            location = {"raw_shot": index + 1, "raw_shot_index": index, "line_preview": sanitize_line_preview(text)}
            if any(ch.isalpha() and not covered.fullmatch(ch) for ch in text):
                meta["unsupported_script_shots"] += 1
                warnings.append(_warning("dialogue_unsupported_script", number, **location))
                continue
            count = sum(1 for _ in counter.finditer(text))
            if not count:
                continue
            checked_lines += 1
            duration = round(shot["end"] - shot["start"], 2)
            effective = min(duration, float(MAX_SECONDS))
            pause_ms = sum(_PAUSE_MS[match.lastgroup] for match in _PAUSES.finditer(text))
            raw_min, min_seconds = _estimate_seconds(count, settings[f"{band}_rate"], pause_ms)
            if raw_min > effective + 1e-9:
                code = "dialogue_slow_over_budget" if band == "slow" else "dialogue_over_budget"
            elif count > settings["long_line_limit"]:
                code = "dialogue_long_line"
            else:
                continue
            budget = {"effective_duration": effective} if effective < duration else {}
            warnings.append(_warning(code, number, **location, **budget, duration=duration, count=count,
                                     unit=unit, min_seconds=min_seconds, rate_band=band))
        if meta["unsupported_script_shots"]:
            status = "partial" if checked_lines else "unsupported"
    return {"status": status, "warnings": warnings, "meta": meta}


def check_dialogue_budget(film, *, rate_config=None) -> list[dict]:
    return inspect_dialogue_budget(film, rate_config=rate_config)["warnings"]


def inspect_automatic(film, environ, *, normalized_times=False):
    try:
        return inspect_with_env(film, environ, normalized_times=normalized_times)
    except FilmValidationError as exc:
        if exc.code != "dialogue_rate_config_invalid":
            raise
        result = inspect_with_env(film, {}, normalized_times=normalized_times)
        result["meta"]["rate_config_fallback"] = True
        return result


def warning_message(warning, translate):
    code = warning["code"]
    values = dict(warning)
    if "unit" in warning:
        values["unit"] = translate(f"dialogue_unit_{warning['unit']}")
    if code.endswith("_over_budget") and "effective_duration" in warning:
        code += "_clamped"
    if code == "dialogue_speech_unset":
        shots = warning["affected_shots"]
        values["shots"] = ", ".join(map(str, shots[:5] if len(shots) > 6 else shots))
        if len(shots) > 6:
            values["shots"] += translate("dialogue_shots_more", count=len(shots))
    return translate(code, **values)


def summary_message(result, translate):
    meta = result["meta"]
    return translate("dialogue_summary", state=translate(f"dialogue_status_{result['status']}"),
                     **meta)


def read_script(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def add_cli_arguments(parser):
    parser.add_argument("--film", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-warn", action="store_true")


def cli(argv):
    import argparse
    import sys

    from .i18n import message

    class PreflightArguments(argparse.ArgumentParser):
        def error(self, _reason):
            self.exit(1, f"dialogue_arguments_invalid: {message('dialogue_arguments_invalid')}\n")

    parser = PreflightArguments(prog="h3ref.py preflight", description=message("dialogue_cli_help"))
    add_cli_arguments(parser)
    args = parser.parse_args(argv)
    try:
        if args.film == "-":
            limit = 32 * 1024 * 1024
            content = sys.stdin.buffer.read(limit + 1)
            if len(content) > limit:
                raise FilmValidationError("dialogue_stdin_limit", field="--film")
            source = json.loads(content.decode("utf-8-sig"))
        else:
            source = read_script(args.film)
        result = inspect_dialogue_budget(source)
    except FilmValidationError as exc:
        print(exc, file=sys.stderr)
        return 1
    except (OSError, ValueError, RecursionError):
        print(f"dialogue_input_invalid: {message('dialogue_input_invalid')}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print(summary_message(result, message))
        if result["meta"]["calibration_status"] == "provisional":
            print(message("dialogue_provisional"))
        speech = normalize_speech(source.get("speech"))
        if speech and speech != "zh":
            print(message("dialogue_non_zh"))
        for warning in result["warnings"]:
            print(warning_message(warning, message))
    return 2 if args.fail_on_warn and (result["warnings"] or result["status"] != "complete") else 0
