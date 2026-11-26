"""Durable reservations and task identity for a single A/B run."""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


SECRET_NAMES = (
    "RUNNINGHUB_API_KEY", "COMFYUI_API_KEY", "APIMART_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY",
    "VISION_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "NGROK_AUTHTOKEN",
)
_TOKEN = re.compile(
    r"(?i)\bbearer\s+[\w./+=-]+|\bsk-[\w-]+|"
    + rf"""\b(?:{'|'.join(SECRET_NAMES)})\s*[:=]\s*['"]?[^\s'",;]+"""
)


class ExperimentError(ValueError):
    pass


def redactor(environ):
    secrets = sorted({environ.get(name, "").strip() for name in SECRET_NAMES} - {""}, key=len, reverse=True)

    def redact(text):
        clean = str(text)
        for secret in secrets:
            clean = clean.replace(secret, "[REDACTED]")
        return _TOKEN.sub("[REDACTED]", clean)

    return redact


def money(amount):
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise ExperimentError(f"invalid CNY amount: {amount!r}") from None
    if not value.is_finite() or not 0 <= value <= 1_000_000:
        raise ExperimentError("CNY amount must be finite and between 0 and 1000000")
    return value


def text_sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fingerprint(value):
    canonical = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return text_sha256(canonical)


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


@contextmanager
def run_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".ab.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ExperimentError("run_locked: another process is using this A/B output directory") from exc
        # The descriptor releases the OS lock even if the process is interrupted.
        yield


class Journal:
    """Use only while holding the run-directory lock."""

    def __init__(self, path, run_id, run_fingerprint, cap, *, resume):
        self.path = path
        self.cap = cap
        if path.exists():
            if not resume:
                raise ExperimentError("state_exists: use --resume with the original run options")
            self.state = json.loads(path.read_text(encoding="utf-8"))
            if (self.state["fingerprint"] != run_fingerprint or self.state["run_id"] != run_id
                    or money(self.state["max_cny"]) != cap):
                raise ExperimentError("fingerprint_changed: use the original inputs, models, assets and budget")
        else:
            if resume:
                raise ExperimentError("state_missing: --resume needs an existing ab_state.json")
            self.state = {"version": 1, "run_id": run_id, "fingerprint": run_fingerprint,
                          "max_cny": str(cap), "steps": {}}
            self.save()

    @property
    def steps(self):
        return self.state["steps"]

    def save(self):
        write_json(self.path, self.state)

    def committed_cny(self):
        return sum((money(step["estimate_cny"] if step["actual_cny"] is None else step["actual_cny"])
                    for step in self.steps.values()), Decimal(0))

    def reserve(self, key, estimate):
        if key in self.steps:
            if self.steps[key]["status"] == "submission_unknown":
                raise ExperimentError(f"submission_unknown: reconcile the existing provider request for {key}")
            return self.steps[key]
        if self.committed_cny() + estimate > self.cap:
            raise ExperimentError(f"budget_exhausted: {key} would exceed the ¥{self.cap} run limit")
        self.steps[key] = {"status": "reserved", "estimate_cny": str(estimate), "actual_cny": None}
        self.save()
        return self.steps[key]

    def update(self, key, **changes):
        self.steps[key].update(changes)
        self.save()

    def record_charge(self, key, charge):
        # A later poll can omit billing after the original completion response.
        if charge is not None:
            self.update(key, actual_cny=str(money(charge)))
