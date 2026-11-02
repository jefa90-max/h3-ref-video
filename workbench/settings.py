"""Write explicitly supplied settings to the CLI's local, ignored .env file."""
import os
import tempfile
from pathlib import Path

from .i18n import InputError


def save_env(path: Path, updates: dict):
    if path.is_symlink():
        raise InputError("env_symlink")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    result = []
    for line in lines:
        name = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if name in updates:
            if name in remaining:
                result.append(f"{name}={remaining.pop(name)}")
        else:
            result.append(line)
    result.extend(f"{name}={value}" for name, value in remaining.items())
    # The temporary file has private permissions before any credential is written.
    fd, filename = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(result) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
