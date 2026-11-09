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
from .directories import create_output_directory

ARTIFACT_TYPES = {".mp4": "video", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
                  ".md": "report", ".txt": "file", ".json": "file"}


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def command(request, root):
    out = Path(request["output_dir"])
    if request.get("operation") in ("segment", "assemble"):
        return [sys.executable, "-u", "-m", "workbench.segments", str(out / "inputs" / "request.json")]
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
    if os.name == "nt":
        from .processes import windows_process_alive

        return windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def stop_child(child):
    if os.name == "nt":
        child.windows_job.stop(child)
        return
    # All cleanup callers share this lock, even when the leader already exited.
    lock = vars(child).setdefault("_workbench_group_lock", threading.Lock())
    with lock:
        if vars(child).get("_workbench_group_stopped", False):
            return
        stop_posix_group(child)
        child._workbench_group_stopped = True


def stop_posix_group(child):
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
    except ProcessLookupError:
        child.wait()
        return
    # The group can outlive its leader, including descendants that ignore TERM.
    try:
        os.killpg(child.pid, signal.SIGKILL)
        deadline = time.monotonic() + 5
        while True:
            os.killpg(child.pid, 0)
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Task process group={child.pid} has not exited")
            time.sleep(0.01)
    except ProcessLookupError:
        return


def stop_posix_descendants_on_exit(child):
    child.wait()
    # Descendants can retain stdout after the leader dies; do not wait for EOF.
    stop_child(child)


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
            result.update(storyboard=[], character_references=[], pending_video=False, can_continue=False, review=None, review_needed=False,
                          segments=[], assembly=None, pending_assembly=False, can_assemble=False)
            if record["kind"] == "film":
                from .storyboard import can_edit_frames, character_references, review_state, review_status, view
                out = Path(record["output_dir"])
                saved = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
                result["character_references"] = character_references(saved, identifier)
                result["storyboard"] = view(saved, artifacts, can_edit_frames(saved, record["status"]), identifier,
                                            failed_only=record["status"] != "succeeded")
                result["review"] = review_status(saved, result["storyboard"])
                result["frame_failures"] = sum(bool(frame.get("error")) for frame in result["storyboard"])
                result["pending_video"] = bool(review_state(out)["dirty_segments"])
                result["can_continue"] = record["status"] == "succeeded" and result["review"]["ready"] and (saved.get("operation") == "frames" or result["pending_video"])
                result["can_resume_frames"] = record["status"] == "succeeded" and (any(
                    frame["task_pending"] for frame in result["storyboard"]) or (saved.get("operation") == "frame" and any(
                    not frame["url"] and not frame.get("error") for frame in result["storyboard"])))
                result["review_needed"] = result["can_continue"]
                from . import segments
                editable = record["status"] == "succeeded" and result["review"]["complete"] and not result["pending_video"]
                result["segments"] = segments.view(saved, artifacts, editable)
                result["assembly"] = segments.assembly_status(saved)
                result["pending_assembly"] = result["assembly"]["pending"]
                result["can_assemble"] = editable and result["assembly"]["ready"]
            return result

    def summaries(self):
        with self.lock:
            newest = sorted(self.records.values(), key=lambda record: record["created_at"], reverse=True)
            return [self.snapshot(record["id"], False) for record in newest]

    def available(self):
        if self.closing or self.children or any(r["status"] in ("queued", "running") for r in self.records.values()):
            raise InputError("busy")
        for record in self.records.values():
            if process_alive(record.get("pid")):
                raise InputError("alive", pid=record["pid"])

    def create(self, payload, env, lang):
        request = validate(payload, self.root)
        if request["operation"] == "frame":
            raise InputError("frame_operation")
        if request["operation"] in ("segment", "assemble"):
            raise InputError("segment_operation")
        if request["kind"] == "film":
            # Creation always stops after frames. Only the reviewed continuation can generate a film.
            request["operation"] = "frames"
        request.pop("redo", None)
        request.pop("reset_applied", None)
        request.pop("resume_frame_tasks", None)
        preflight(request, env)
        with self.lock:
            self.available()
            identifier = uuid.uuid4().hex[:12]
            if request.get("output_root") and not request["output_dir"]:
                output = create_output_directory(request["output_root"], self.root, self.storage)
            else:
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

    def edit_frame(self, identifier, key, prompt, env, character_reference_paths=None):
        from .inputs import array, invalid, text, verify_image
        from .storyboard import can_edit_frames, frame_records, plan
        with self.lock:
            self.available()
            record = self.find(identifier)
            if record["kind"] != "film" or record["status"] not in ("succeeded", "failed", "interrupted"):
                raise InputError("frame_operation")
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            if not can_edit_frames(request, record["status"]):
                raise InputError("frame_operation")
            try:
                script, _, _, descriptions = plan(request, check_images=False)
                description = next((item for item in descriptions if item["key"] == key), None)
            except SystemExit as exc:
                raise InputError("film_invalid", detail=str(exc)) from exc
            previous = frame_records(request).get(key, {})
            if previous.get("submission_unknown") and not previous.get("task"):
                raise InputError("frame_submission_unknown", path=str(out / "state.json"))
            from h3ref.frames import task_pending
            if task_pending(previous):
                raise InputError("frame_task_pending", task=previous["task"])
            if record["status"] != "succeeded" and not (previous.get("error") or previous.get("moderation_error")):
                raise InputError("frame_operation")
            if not description or not (Path(description["path"]).is_file() or previous.get("error") or previous.get("moderation_error")):
                raise InputError("not_found", value=key)
            prompt = text(prompt, "prompt")
            if previous.get("moderation_error") and prompt == (previous.get("failed_prompt") or previous.get("prompt", "")).strip():
                raise InputError("frame_prompt_unchanged")
            current_characters = [r["path"] for r in description["references"] if r["kind"] == "character"]
            characters = current_characters if character_reference_paths is None else array(character_reference_paths, "character_reference_paths")
            allowed = {Path(c["picture"]).resolve() for c in script.get("cast") or []}
            characters = [str(Path(text(p, "character_reference_paths")).resolve()) for p in characters]
            if len(characters) != len(set(characters)):
                invalid("character_reference_paths", characters, "choice")
            for path in map(Path, characters):
                if path not in allowed:
                    invalid("character_reference_paths", str(path), "choice")
            reference_paths = ([r["path"] for r in description["references"] if r["kind"] == "board"]
                               + characters + [r["path"] for r in description["references"] if r["kind"] == "anchor"])
            for path in map(Path, reference_paths):
                if not path.is_file():
                    raise InputError("missing_image", path=str(path))
                verify_image(path)
            request.update(operation="frame")
            preflight(request, env)
            pending = {"key": key, "prompt": prompt, "revision": uuid.uuid4().hex,
                       "character_reference_paths": characters, "reference_paths": reference_paths,
                       "previous": previous, "task": None, "applied": False}
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
        record.update(status="queued", error=None, operation=request["operation"], operation_id=uuid.uuid4().hex)
        self.save(record)
        self.launch(record, request, env)
        return self.snapshot(record["id"])

    def start_video_operation(self, identifier, operation, env, version, number=None):
        from . import segments
        with self.lock:
            self.available()
            record = self.find(identifier)
            if record["kind"] != "film" or record["status"] != "succeeded":
                raise InputError("segment_operation")
            snapshot = self.snapshot(identifier)
            if not snapshot["review"]["complete"] or snapshot["pending_video"]:
                raise InputError("review_required")
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            request.update(operation=operation)
            for key in ("redo", "reset_applied", "segment_revision"):
                request.pop(key, None)
            if operation == "segment":
                request["segment"] = number
            preflight(request, env)
            if operation == "segment":
                request["segment_revision"] = segments.prepare_regeneration(request, number, version)
            else:
                segments.prepare_assembly(request, version)
            return self.restart(record, request, env)

    def resume(self, identifier, env):
        with self.lock:
            record = self.find(identifier)
            remaining_frames = record["status"] == "succeeded" and self.snapshot(identifier).get("can_resume_frames")
            if record["status"] not in ("failed", "interrupted") and not remaining_frames:
                raise InputError("resume")
            self.available()
            request = json.loads((Path(record["output_dir"]) / "inputs" / "request.json").read_text(encoding="utf-8"))
            if remaining_frames:
                request["operation"] = "frames"
            if request.get("operation") in ("segment", "assemble"):
                snapshot = self.snapshot(identifier)
                if not snapshot["review"]["complete"] or snapshot["pending_video"]:
                    raise InputError("review_required")
            elif request.get("operation") != "frame":
                request = validate(request, self.root)
            if request["kind"] == "film" and request["operation"] == "pipeline" and not self.snapshot(identifier)["review"]["complete"]:
                # Old/interrupted jobs cannot bypass the newly required review via resume.
                request["operation"] = "frames"
                request.pop("redo", None)
                request.pop("reset_applied", None)
            if request["kind"] == "film" and request["operation"] == "frames":
                request["resume_frame_tasks"] = True
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
                env.update(H3_REWRITER="llm", H3_LANG=lang, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                with self.lock:
                    if self.closing:
                        raise RuntimeError(message("interrupted", lang))
                    start = subprocess.Popen
                    if os.name == "nt":
                        from .processes import start_windows_child
                        start = start_windows_child
                    child = start(command(request, self.root), cwd=self.root, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                  start_new_session=(os.name != "nt"))
                    self.children[identifier] = child
                    record.update(status="running", pid=child.pid)
                    self.save(record)
                    if os.name != "nt":
                        threading.Thread(target=stop_posix_descendants_on_exit, args=(child,), daemon=True).start()
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
                    from .storyboard import read_json, review_state
                    out = Path(record["output_dir"])
                    if read_json(out / "state.json", {}).get("frame_failures"):
                        request["operation"] = "frames"
                        write_json(out / "inputs" / "request.json", request)
                        with self.lock:
                            record["operation"] = "frames"
                    else:
                        review = review_state(out)
                        review["dirty_segments"] = []
                        write_json(out / "inputs" / "review.json", review)
                        from .segments import record_assembly
                        record_assembly(request)
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
