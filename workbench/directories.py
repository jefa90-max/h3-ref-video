"""Choose a local output root and allocate a separate dated directory for each new task."""
import itertools
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .i18n import InputError, message
from .inputs import text


def saved_output_root(root, storage):
    preferences = storage / "preferences.json"
    if not preferences.exists():
        return root / "jobs"
    saved = json.loads(preferences.read_text(encoding="utf-8"))
    return Path(text(saved.get("output_root"), "output_root"))


def validate_output_root(value, root, storage):
    value = text(value, "output_root")
    folder = Path(value).expanduser()
    folder = (folder if folder.is_absolute() else root / folder).resolve()
    if folder.is_relative_to(storage):
        raise InputError("output_reserved", path=str(folder))
    if not folder.is_dir():
        raise InputError("output_root_missing", path=str(folder))
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError as exc:
        raise InputError("output_root_unwritable", path=str(folder), detail=str(exc)) from exc
    return folder


def save_output_root(value, root, storage):
    folder = validate_output_root(value, root, storage)
    fd, filename = tempfile.mkstemp(prefix="preferences-", suffix=".tmp", dir=storage)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"output_root": str(folder)}, stream, ensure_ascii=False, indent=2)
        temporary.replace(storage / "preferences.json")
    finally:
        temporary.unlink(missing_ok=True)
    return folder


def create_output_directory(value, root, storage):
    default = root / "jobs"
    if Path(value) == default and not (storage / "preferences.json").exists():
        default.mkdir(parents=True, exist_ok=True)
    folder = validate_output_root(value, root, storage)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    for number in itertools.count():
        destination = folder / (stamp if number == 0 else f"{stamp}-{number:03d}")
        try:
            destination.mkdir()
        except FileExistsError:
            continue
        except OSError as exc:
            raise InputError("output_root_unwritable", path=str(destination), detail=str(exc)) from exc
        return destination


def choose_output_folder(initial, lang):
    initial = initial if initial.is_dir() else Path.home()
    title = message("output_picker_title", lang)
    if sys.platform == "win32":
        command = ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File",
                   str(Path(__file__).with_name("choose-folder.ps1"))]
    elif sys.platform == "darwin":
        script = '''on run argv
try
    set selected to choose folder with prompt (item 2 of argv) default location (POSIX file (item 1 of argv))
    return POSIX path of selected
on error detail number code
    if code is -128 then return ""
    error detail number code
end try
end run'''
        command = ["osascript", "-e", script, str(initial), title]
    else:
        raise InputError("output_picker_unsupported")
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=300,
                                env={**os.environ, "H3_OUTPUT_ROOT": str(initial), "H3_FOLDER_TITLE": title})
    except subprocess.TimeoutExpired as exc:
        raise InputError("output_picker_timeout") from exc
    if result.returncode:
        raise InputError("output_picker_failed", detail=result.stderr.strip()[:400])
    if sys.platform == "win32":
        return json.loads(result.stdout)["path"]
    return result.stdout.strip() or None
