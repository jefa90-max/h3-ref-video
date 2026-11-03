"""Persist task metadata and run the existing CLI in an isolated child process."""
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from .i18n import InputError, message
from .inputs import preflight, validate

ARTIFACT_TYPES = {".mp4": "video", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
                  ".md": "report", ".txt": "file", ".json": "file"}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def command(request, root):
    out = Path(request["output_dir"])
    if request.get("operation", "pipeline") != "pipeline" or request.get("redo"):
        return [sys.executable, "-u", "-m", "workbench.storyboard", str(out / "inputs" / "request.json")]
    if request["demo"]:
        return [sys.executable, "-u", "-m", "workbench.demo", str(out / "inputs" / "request.json")]
    argv = [sys.executable, "-u", str(root / "h3ref.py"), request["kind"]]
    if request["kind"] == "one":
        argv += ["--prompt", str(out / "inputs" / "prompt.txt"), "--seconds", str(request["seconds"]),
                 "--aspect", request["aspect"], "--out", str(out)]
        for path in request["images"]:
            argv += ["--image", path]
        if request.get("seed") is not None:
            argv += ["--seed", str(request["seed"])]
        if request.get("check"):
            argv += ["--check"]
    else:
        argv += [str(out), "--film", str(out / "inputs" / "film.json"), "--frames", request["frames"]]
    return argv


def process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_child(child):
    if child.poll() is not None:
        return
    try:
        if os.name == "nt":
            child.terminate()
        else:
            os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            child.kill()
        else:
            os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    except ProcessLookupError:
        child.wait()


class Jobs:
    def __init__(self, root, storage):
        self.root, self.storage = root, storage
        self.lock = threading.RLock()
        self.records = {}
        self.children = {}
        self.secrets = set()
        self.closing = False
        (storage / "jobs").mkdir(parents=True, exist_ok=True)
        for path in sorted((storage / "jobs").glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["status"] in ("running", "queued"):
                record.update(status="interrupted", error=message("interrupted", record["lang"]))
                write_json(path, record)
            self.records[record["id"]] = record
        atexit.register(self.close)

    def redact(self, value):
        for secret in sorted(self.secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    def remember_secrets(self, env):
        with self.lock:
            self.secrets.update(value for key, value in env.items() if value and ("KEY" in key or "TOKEN" in key))

    def save(self, record):
        write_json(self.storage / "jobs" / f"{record['id']}.json", record)

    def find(self, identifier):
        if identifier not in self.records:
            raise InputError("not_found", value=identifier)
        return self.records[identifier]

    def artifacts(self, record):
        out = Path(record["output_dir"])
        result = []
        # Deliberately exclude internal state / input copies and arbitrary files in the output folder.
        files = [out / name for name in ("film.mp4", "clip.mp4", "report.md", "plan.json", "prompt.h3.txt")]
        for folder in ("frames", "clips", "prompts"):
            files.extend(sorted((out / folder).glob("*")))
        for path in files:
            if not path.is_file() or not path.resolve().is_relative_to(out) or path.suffix.lower() not in ARTIFACT_TYPES:
                continue
            name = path.relative_to(out).as_posix()
            result.append({"name": name, "path": str(path), "kind": ARTIFACT_TYPES[path.suffix.lower()],
                           "role": "final" if name in ("film.mp4", "clip.mp4") else None,
                           "url": f"/api/jobs/{record['id']}/files/{quote(name)}?v={path.stat().st_mtime_ns}"})
        return result

    def snapshot(self, identifier, full=True):
        with self.lock:
            record = self.find(identifier)
            result = {k: v for k, v in record.items() if k not in ("pid", "lang")}
            if not full:
                return result
            artifacts = self.artifacts(record)
            log = self.storage / "jobs" / f"{identifier}.log"
            if log.exists():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size - 100000))
                    result["logs"] = self.redact(stream.read().decode("utf-8", errors="replace"))
            else:
                result["logs"] = ""
            report = next((a for a in artifacts if a["name"] == "report.md"), None)
            result["report"] = self.redact(Path(report["path"]).read_text(encoding="utf-8")) if report else ""
            result["artifacts"] = artifacts
            result["progress"] = {"frames": sum(a["name"].startswith("frames/") for a in artifacts),
                                  "clips": sum(a["name"].startswith("clips/") and a["kind"] == "video" for a in artifacts)}
            if record["kind"] == "one" and any(a["name"] == "clip.mp4" for a in artifacts):
                result["progress"]["clips"] = 1
            result.update(storyboard=[], pending_video=False, can_continue=False, review=None, review_needed=False)
            if record["kind"] == "film":
                from .storyboard import review_state, review_status, view
                out = Path(record["output_dir"])
                saved = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
                result["storyboard"] = view(saved, artifacts, record["status"] == "succeeded")
                result["review"] = review_status(saved, result["storyboard"])
                result["pending_video"] = bool(review_state(out)["dirty_segments"])
                result["can_continue"] = record["status"] == "succeeded" and (saved.get("operation") == "frames" or result["pending_video"])
                result["review_needed"] = result["can_continue"]
            return result

    def summaries(self):
        with self.lock:
            newest = sorted(self.records.values(), key=lambda record: record["created_at"], reverse=True)
            return [self.snapshot(record["id"], False) for record in newest]

    def available(self):
        if self.closing or any(r["status"] in ("queued", "running") for r in self.records.values()):
            raise InputError("busy")
        for record in self.records.values():
            if process_alive(record.get("pid")):
                raise InputError("alive", pid=record["pid"])

    def create(self, payload, env, lang):
        request = validate(payload, self.root)
        if request["operation"] == "frame":
            raise InputError("frame_operation")
        if request["kind"] == "film":
            # Creation always stops after frames. Only the reviewed continuation can generate a film.
            request["operation"] = "frames"
        request.pop("redo", None)
        request.pop("reset_applied", None)
        preflight(request, env)
        with self.lock:
            self.available()
            identifier = uuid.uuid4().hex[:12]
            output = Path(request["output_dir"]).expanduser() if request["output_dir"] else self.root / "jobs" / ("demo" if request["demo"] else "web") / identifier
            output = (output if output.is_absolute() else self.root / output).resolve()
            if output.is_relative_to(self.storage) or self.storage.is_relative_to(output):
                raise InputError("output_reserved", path=str(output))
            if output.exists() and (not output.is_dir() or any(output.iterdir())):
                raise InputError("output", path=str(output))
            output.mkdir(parents=True, exist_ok=True)
            (output / "inputs").mkdir()
            request.update(output_dir=str(output), lang=lang)
            write_json(output / "inputs" / "request.json", request)
            if request["kind"] == "film":
                write_json(output / "inputs" / "film.json", request["film"])
            else:
                (output / "inputs" / "prompt.txt").write_text(request["prompt"], encoding="utf-8")
            record = {"id": identifier, "kind": request["kind"], "demo": request["demo"], "output_dir": str(output),
                      "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": "queued", "error": None, "lang": lang,
                      "operation": request["operation"]}
            self.records[identifier] = record
            self.save(record)
            self.launch(record, request, env)
            return self.snapshot(identifier)

    def edit_frame(self, identifier, key, prompt, env):
        from .inputs import text
        from .storyboard import frame_records, plan
        with self.lock:
            self.available()
            record = self.find(identifier)
            if record["kind"] != "film" or record["status"] != "succeeded":
                raise InputError("frame_operation")
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            try:
                description = next((item for item in plan(request)[3] if item["key"] == key), None)
            except SystemExit as exc:
                raise InputError("film_invalid", detail=str(exc)) from exc
            if not description or not Path(description["path"]).is_file():
                raise InputError("not_found", value=key)
            prompt = text(prompt, "prompt")
            request.update(operation="frame")
            preflight(request, env)
            pending = {"key": key, "prompt": prompt, "revision": uuid.uuid4().hex,
                       "previous": frame_records(request).get(key, {}), "task": None, "applied": False}
            write_json(out / "inputs" / "frame-edit.json", pending)
            return self.restart(record, request, env)

    def continue_film(self, identifier, env, confirmation=None):
        from .storyboard import review_state
        with self.lock:
            self.available()
            record = self.find(identifier)
            snapshot = self.snapshot(identifier)
            if not snapshot["can_continue"]:
                raise InputError("continue_operation")
            if confirmation:
                if confirmation.get("approved") is not True:
                    raise InputError("review_request")
                if confirmation.get("review_version") != snapshot["review"]["version"] or not snapshot["review"]["ready"]:
                    raise InputError("review_changed")
                review = review_state(Path(record["output_dir"]))
                review["confirmed_version"] = snapshot["review"]["version"]
                write_json(Path(record["output_dir"]) / "inputs" / "review.json", review)
                snapshot = self.snapshot(identifier)
            if not snapshot["review"]["complete"]:
                raise InputError("review_required")
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            request.update(operation="pipeline", redo=review_state(out).get("dirty_segments", []), reset_applied=False)
            preflight(request, env)
            return self.restart(record, request, env)

    def restart(self, record, request, env):
        write_json(Path(record["output_dir"]) / "inputs" / "request.json", request)
        record.update(status="queued", error=None, operation=request["operation"])
        self.save(record)
        self.launch(record, request, env)
        return self.snapshot(record["id"])

    def resume(self, identifier, env):
        with self.lock:
            record = self.find(identifier)
            if record["status"] not in ("failed", "interrupted"):
                raise InputError("resume")
            self.available()
            request = json.loads((Path(record["output_dir"]) / "inputs" / "request.json").read_text(encoding="utf-8"))
            request = validate(request, self.root)
            if request["kind"] == "film" and request["operation"] == "pipeline" and not self.snapshot(identifier)["review"]["complete"]:
                # Old/interrupted jobs cannot bypass the newly required review via resume.
                request["operation"] = "frames"
                request.pop("redo", None)
                request.pop("reset_applied", None)
            preflight(request, env)
            return self.restart(record, request, env)

    def launch(self, record, request, env):
        self.remember_secrets(env)
        thread = threading.Thread(target=self.run, args=(record, request, dict(env)), daemon=True)
        thread.start()

    def run(self, record, request, env):
        identifier, lang = record["id"], record["lang"]
        log_path = self.storage / "jobs" / f"{identifier}.log"
        child = None
        try:
            with log_path.open("a", encoding="utf-8") as log:
                log.write(message("started", lang, kind=record["kind"], mode=message("demo" if record["demo"] else "live", lang), path=record["output_dir"]) + "\n")
                log.flush()
                env.update(H3_REWRITER="llm", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
                with self.lock:
                    if self.closing:
                        raise RuntimeError(message("interrupted", lang))
                    child = subprocess.Popen(command(request, self.root), cwd=self.root, env=env, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                             start_new_session=(os.name != "nt"))
                    self.children[identifier] = child
                    record.update(status="running", pid=child.pid)
                    self.save(record)
                for line in child.stdout:
                    with self.lock:
                        log.write(self.redact(line))
                    log.flush()
                code = child.wait()
                if code:
                    raise RuntimeError(message("exit", lang, code=code))
                if not request["demo"] and request["kind"] == "one":
                    out = Path(record["output_dir"])
                    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
                    report = message("one_report", lang, path=out / "clip.mp4", cost=state.get("cny"), gpu=state.get("run_s"),
                                     checks=json.dumps(state.get("subtitles", message("unchecked", lang)), ensure_ascii=False, indent=2))
                    (out / "report.md").write_text(self.redact(report), encoding="utf-8")
                if request["kind"] == "film" and request.get("operation", "pipeline") == "pipeline":
                    from .storyboard import review_state
                    out = Path(record["output_dir"])
                    review = review_state(out)
                    review["dirty_segments"] = []
                    write_json(out / "inputs" / "review.json", review)
                with self.lock:
                    record.update(status="succeeded", error=None)
        except Exception as exc:
            # Worker boundary: preserve the failure as a visible task status, never a silent success.
            with self.lock:
                detail = self.redact(f"{type(exc).__name__}: {exc}")
                record.update(status="interrupted" if self.closing else "failed", error=detail)
            with log_path.open("a", encoding="utf-8") as log:
                log.write(detail + "\n")
        finally:
            # Logging/pipe failures must not leave a paid CLI running while a retry is enabled.
            if child is not None:
                stop_child(child)
            with self.lock:
                self.children.pop(identifier, None)
                record.pop("pid", None)
                self.save(record)

    def close(self):
        with self.lock:
            self.closing = True
            for child in self.children.values():
                stop_child(child)
