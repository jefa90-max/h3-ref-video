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

from h3ref import config
from .i18n import InputError, message
from .inputs import preflight, validate
from .directories import create_output_directory

ARTIFACT_TYPES = {".mp4": "video", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
                  ".md": "report", ".txt": "file", ".json": "file"}
SECRET_NAMES = ("RUNNINGHUB_API_KEY", "APIMART_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY",
                "VISION_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    config.atomic_replace(temporary, path)


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
    except (ProcessLookupError, PermissionError):
        return False
    return True


def worker_locked(output_dir):
    if not output_dir or os.name == "nt":
        return False
    lock_path = Path(output_dir) / "inputs" / "worker.lock"
    if not lock_path.is_file():
        return False
    import fcntl
    try:
        with lock_path.open("a+b") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        return False
    return False


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
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        child.wait()
    except (ProcessLookupError, PermissionError):
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
    except (ProcessLookupError, PermissionError):
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
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or "id" not in record or "status" not in record:
                continue
            if record["status"] in ("running", "queued"):
                if not worker_locked(record.get("output_dir")):
                    record.pop("pid", None)
                record.update(status="interrupted", error=message("interrupted", record.get("lang", "en")))
                write_json(path, record)
            self.records[record["id"]] = record
        atexit.register(self.close)

    def redact(self, value):
        with self.lock:
            secrets = sorted(self.secrets, key=len, reverse=True)
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def remember_secrets(self, env):
        with self.lock:
            for key, value in env.items():
                if not isinstance(value, str):
                    continue
                cleaned = value.strip()
                if len(cleaned) >= 8 and (key in SECRET_NAMES or key.endswith(("_API_KEY", "_SECRET", "_AUTH_TOKEN", "_OAUTH_TOKEN"))):
                    self.secrets.add(cleaned)

    def save(self, record):
        write_json(self.storage / "jobs" / f"{record['id']}.json", record)

    def find(self, identifier):
        if identifier not in self.records:
            raise InputError("not_found", value=identifier)
        return self.records[identifier]

    def artifacts(self, record, context=None):
        out = Path(record["output_dir"])
        result = []
        from . import segments
        context = context or segments.read_context({"output_dir": str(out)})
        # Deliberately exclude internal state / input copies and arbitrary files in the output folder.
        files = [out / name for name in ("film.mp4", "clip.mp4", "report.md", "plan.json", "prompt.h3.txt")]
        for folder in ("frames", "clips", "prompts"):
            files.extend(sorted((out / folder).glob("*")))
        files.extend(out / name for name in context["pending"].get("files", {}) if out / name not in files)
        for path in files:
            source = segments.visible_file(out, path.relative_to(out).as_posix(), context)
            if not source or path.suffix.lower() not in ARTIFACT_TYPES:
                continue
            name = path.relative_to(out).as_posix()
            st = source.stat()
            result.append({"name": name, "path": str(path), "kind": ARTIFACT_TYPES[path.suffix.lower()],
                           "role": "final" if name in ("film.mp4", "clip.mp4") else None,
                           "url": f"/api/jobs/{record['id']}/files/{quote(name)}?v={st.st_mtime_ns}:{st.st_size}"})
        return result

    def snapshot(self, identifier, full=True):
        for _ in range(3):
            result = self._snapshot(identifier, full)
            if result is not None:
                return result
        raise InputError('segment_snapshot_changed')

    def _snapshot(self, identifier, full=True):
        with self.lock:
            record = dict(self.find(identifier))
            result = {k: v for k, v in record.items() if k not in ("pid", "lang")}
            if not full:
                return result
        from . import segments
        context = segments.read_context({"output_dir": record["output_dir"]})
        artifacts = self.artifacts(record, context)
        log = self.storage / "jobs" / f"{identifier}.log"
        if log.exists():
            with log.open("rb") as stream:
                stream.seek(max(0, log.stat().st_size - 100000))
                result["logs"] = self.redact(stream.read().decode("utf-8", errors="replace"))
        else:
            result["logs"] = ""
        report = next((a for a in artifacts if a["name"] == "report.md"), None)
        try:
            result["report"] = self.redact(Path(report["path"]).read_text(encoding="utf-8", errors="replace")) if report else ""
        except FileNotFoundError:
            result["report"] = ""
        result["artifacts"] = artifacts
        result["progress"] = {"frames": sum(a["name"].startswith("frames/") for a in artifacts),
                              "clips": sum(a["name"].startswith("clips/") and a["kind"] == "video" for a in artifacts)}
        if record["kind"] == "one" and any(a["name"] == "clip.mp4" for a in artifacts):
            result["progress"]["clips"] = 1
        result.update(storyboard=[], character_references=[], pending_video=False, can_continue=False, review=None, review_needed=False,
                      segments=[], assembly=None, pending_assembly=False, can_assemble=False)
        if record["kind"] == "one":
            one_state = context["state"]
            result["can_resume_video"] = not bool(
                (one_state.get("submission_unknown") and not one_state.get("task"))
                or one_state.get("terminal_failed")
            )
        if record["kind"] == "film":
            from .storyboard import can_edit_frames, character_references, frame_edit_task_pending, read_json, review_state, review_status, view
            out = Path(record["output_dir"])
            saved = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            result["character_references"] = character_references(saved, identifier)
            result["storyboard"] = view(saved, artifacts, can_edit_frames(saved, record["status"]), identifier,
                                        failed_only=record["status"] != "succeeded" and saved.get("operation") in ("frames", "frame"),
                                        records=None if saved["demo"] else context["state"].get("frames", {}))
            for frame in result["storyboard"]:
                if frame.get("error"):
                    frame["error"] = self.redact(str(frame["error"]))
            result["review"] = review_status(saved, result["storyboard"])
            result["frame_failures"] = sum(bool(frame.get("error")) for frame in result["storyboard"])
            result["pending_video"] = bool(review_state(out)["dirty_segments"])
            result["can_continue"] = record["status"] == "succeeded" and result["review"]["ready"] and (saved.get("operation") == "frames" or result["pending_video"])
            raw_frame_pending = read_json(out / "inputs" / "frame-edit.json", {}) if saved.get("operation") == "frame" else {}
            active_frame = raw_frame_pending if raw_frame_pending and not raw_frame_pending.get("applied") else {}
            settled_dead_frame = bool(
                record["status"] in ("failed", "interrupted") and saved.get("operation") == "frame"
                and (active_frame.get("task_failed") or active_frame.get("moderation_error")
                     or (not active_frame.get("task") and not active_frame.get("downloaded") and bool(active_frame.get("error"))))
                and not frame_edit_task_pending(active_frame)
                and not (active_frame.get("submission_unknown") and not active_frame.get("task"))
                and any((frame["task_pending"] or (not frame["url"] and not frame.get("error")))
                        for frame in result["storyboard"] if frame["key"] != active_frame.get("key"))
            )
            result["can_resume_frames"] = (record["status"] == "succeeded" and (any(
                frame["task_pending"] for frame in result["storyboard"]) or (saved.get("operation") == "frame" and any(
                not frame["url"] and not frame.get("error") for frame in result["storyboard"])))) or settled_dead_frame
            result["review_needed"] = result["can_continue"]
            editable = record["status"] in ("succeeded", "failed", "interrupted") and result["review"]["complete"] and not result["pending_video"]
            result["segments"] = segments.view(saved, artifacts, editable, context=context)
            for card in result["segments"]:
                if card.get("error"):
                    card["error"] = self.redact(str(card["error"]))
            result["segment_number"] = saved.get("segment") if saved.get("operation") == "segment" else None
            unfinished = [card for card in result['segments'] if not card['url'] or card['task_pending']]
            pending_seg = context['pending']
            _, pending_staged = segments.attempt_record(saved, context, pending_seg.get('number', 0)) if pending_seg else ({}, {})
            pending_card = next((c for c in result['segments'] if c['number'] == pending_seg.get('number')), None) if pending_seg else None
            pending_has_clip = bool(pending_card and pending_card.get('url'))
            sibling_cards = [c for c in result['segments'] if c['number'] != pending_seg.get('number')] if pending_seg else []
            has_inflight_sibling = any(c['task_pending'] for c in sibling_cards)
            has_runnable_clipless_sibling = any(
                not c['url'] and not c['terminal_failed'] and not c['submission_unknown']
                for c in sibling_cards
            )
            pending_settled_dead = bool(
                pending_seg and not pending_seg.get('applied')
                and not pending_seg.get('publishing') and not pending_seg.get('generated')
                and not segments.task_unresolved(pending_staged)
                and not (pending_staged.get('submission_unknown') and not pending_staged.get('task'))
                and (
                    (pending_staged.get('terminal_failed') and (has_inflight_sibling or (pending_has_clip and has_runnable_clipless_sibling)))
                    or (record['status'] in ('failed', 'interrupted') and not pending_staged.get('task')
                        and (pending_seg.get('error') or pending_staged.get('error')) and has_inflight_sibling)
                )
            )
            result['can_resume_pipeline'] = bool(saved.get('operation') == 'segment'
                    and (pending_seg.get('applied') or pending_settled_dead)
                    and unfinished and result['review']['complete'] and not result['pending_video'])
            if result['can_resume_pipeline']:
                result['segment_number'] = None
            if saved.get("operation") in ("frames", "frame", "assemble") and not result["can_resume_pipeline"]:
                if result["can_resume_frames"]:
                    result["can_resume_video"] = not any(frame.get("submission_unknown") for frame in result["storyboard"])
                else:
                    result["can_resume_video"] = not bool(
                        (active_frame.get("submission_unknown") and not active_frame.get("task"))
                        or active_frame.get("task_failed") or active_frame.get("moderation_error")
                    )
            else:
                targets = [card for card in result["segments"] if result["segment_number"] is None or card["number"] == result["segment_number"]]
                has_unknown = any(card["submission_unknown"] for card in targets)
                is_pipeline_target = saved.get("operation") == "pipeline" or result["can_resume_pipeline"]
                has_terminal = any(card["terminal_failed"] and not (is_pipeline_target and card["url"]) for card in targets)
                has_inflight = any(card["task_pending"] for card in targets)
                result["can_resume_video"] = not has_unknown and (
                    not has_terminal or (
                        record["status"] in ("failed", "interrupted")
                        and is_pipeline_target
                        and has_inflight
                    )
                )
            result["assembly"] = segments.assembly_status(saved, context=context)
            result["pending_assembly"] = result["assembly"]["pending"]
            has_locked_segment = any(
                card["task_pending"] or card["submission_unknown"] or card["publishing"]
                for card in result["segments"]
            )
            result["can_assemble"] = editable and result["assembly"]["ready"] and not has_locked_segment
        out = Path(record['output_dir'])
        if segments.clip_version(out / 'inputs' / 'segment-edit.json', out) != context['pending_stamp']:
            return None
        return result

    def summaries(self, limit=50):
        with self.lock:
            newest = sorted(self.records.values(), key=lambda record: (record["status"] in ("running", "queued"), record["created_at"]), reverse=True)
            selected = newest[:limit] if limit else newest
            return [self.snapshot(record["id"], False) for record in selected]

    def available(self):
        if self.closing or self.children or any(r["status"] in ("queued", "running") for r in self.records.values()):
            raise InputError("busy")
        for record in self.records.values():
            if worker_locked(record.get("output_dir")):
                raise InputError("alive", pid=record.get("pid") or record["id"])
            pid = record.get("pid")
            if pid:
                if os.name != "nt" and record["status"] == "interrupted":
                    record.pop("pid", None)
                    self.save(record)
                elif process_alive(pid):
                    raise InputError("alive", pid=pid)
                else:
                    record.pop("pid", None)
                    self.save(record)

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
        from h3ref import frames
        from . import segments
        from .inputs import array, invalid, text, verify_image
        from .storyboard import can_edit_frames, frame_edit_task_pending, frame_records, plan, read_json, unsubmitted_frame_pending
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
                script, planned_segments, _, descriptions = plan(request, check_images=False)
                description = next((item for item in descriptions if item["key"] == key), None)
            except SystemExit as exc:
                raise InputError("film_invalid", detail=str(exc)) from exc
            pending_frame = read_json(out / "inputs" / "frame-edit.json", {})
            active_pending = pending_frame if pending_frame and not pending_frame.get("applied") else {}
            if active_pending.get("submission_unknown") and not active_pending.get("task"):
                raise InputError("frame_submission_unknown", path=str(out / "inputs" / "frame-edit.json"))
            if frame_edit_task_pending(active_pending):
                raise InputError("frame_task_pending", task=active_pending["task"])
            previous = frame_records(request).get(key, {})
            if previous.get("submission_unknown") and not previous.get("task"):
                raise InputError("frame_submission_unknown", path=str(out / "state.json"))
            if frames.task_pending(previous):
                raise InputError("frame_task_pending", task=previous["task"])
            pending_for_key = active_pending if active_pending.get("key") == key else {}
            has_frame_failure = bool(
                previous.get("error") or previous.get("moderation_error")
                or pending_for_key.get("error") or pending_for_key.get("moderation_error")
                or unsubmitted_frame_pending(pending_for_key)
            )
            if record["status"] != "succeeded" and request.get("operation") in ("frames", "frame") and not has_frame_failure:
                raise InputError("frame_operation")
            if not description or not (Path(description["path"]).is_file() or has_frame_failure):
                raise InputError("not_found", value=key)
            seg_context = segments.read_context({"output_dir": str(out)})
            seg_pending = seg_context["pending"]
            if seg_pending and not seg_pending.get("applied"):
                _, pending_staged = segments.attempt_record(request, seg_context, seg_pending["number"])
                if (seg_pending.get("publishing") or seg_pending.get("generated")
                        or segments.task_unresolved(pending_staged)
                        or (pending_staged.get("submission_unknown") and not pending_staged.get("task"))):
                    raise InputError("segment_locked", number=seg_pending["number"])
            affected = ({description["segment"]} if request["frames"] == "shot"
                        else {s["idx"] + 1 for s in planned_segments if s["idx"] + 1 >= description["segment"]})
            for seg_num in sorted(affected):
                _, staged_seg = segments.attempt_record(request, seg_context, seg_num)
                seg_rec = staged_seg or seg_context["records"].get(str(seg_num - 1), {})
                if (seg_rec.get("submission_unknown") and not seg_rec.get("task")) or segments.task_unresolved(seg_rec):
                    raise InputError("segment_locked", number=seg_num)
            prompt = text(frames.without_anchor_reference(text(prompt, "prompt")), "prompt")
            moderation_prompt = (pending_for_key.get("prompt") if pending_for_key.get("moderation_error")
                                 else (previous.get("failed_prompt") or previous.get("prompt", "")) if previous.get("moderation_error") else None)
            if moderation_prompt is not None and prompt == frames.without_anchor_reference(moderation_prompt).strip():
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
            reference_paths = [r["path"] for r in description["references"] if r["kind"] == "board"] + characters
            for path in map(Path, reference_paths):
                if not path.is_file():
                    raise InputError("missing_image", path=str(path))
                verify_image(path)
            request.update(operation="frame")
            preflight(request, env)
            if active_pending.get("key") and active_pending.get("key") != key:
                if active_pending.get("task_failed") or active_pending.get("moderation_error"):
                    from .storyboard import save_frame_failure
                    mod_err = active_pending.get("moderation_error")
                    err_text = active_pending.get("error") or (
                        str(frames.ContentPolicyError(mod_err)) if mod_err
                        else message("frame_task_failed", record.get("lang", "en"), task=active_pending.get("task"), reason="")
                    )
                    save_frame_failure(request, dict(active_pending), err_text, mod_err)
                elif active_pending.get("error") and not active_pending.get("task"):
                    prev_other = active_pending.get("previous") or {}
                    has_valid_other = (
                        (out / "frames" / f"{active_pending['key']}.png").is_file()
                        and not prev_other.get("error") and not prev_other.get("moderation_error")
                    )
                    if not has_valid_other:
                        from h3ref import film
                        film.Job(out).put(
                            "frames", active_pending["key"], prompt=active_pending.get("prompt", ""),
                            error=active_pending["error"], task=None, task_failed=False, moderation_error=None,
                        )
            pending = {"key": key, "prompt": prompt, "revision": uuid.uuid4().hex,
                       "character_reference_paths": characters, "reference_paths": reference_paths,
                       "previous": previous, "task": None, "applied": False}
            write_json(out / "inputs" / "frame-edit.json", pending)
            return self.restart(record, request, env)

    def continue_film(self, identifier, env, confirmation=None):
        from . import segments
        from .storyboard import frame_edit_task_pending, read_json, review_state
        with self.lock:
            self.available()
            record = self.find(identifier)
            snapshot = self.snapshot(identifier)
            if not snapshot["can_continue"]:
                raise InputError("continue_operation")
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            pending_frame = read_json(out / "inputs" / "frame-edit.json", {})
            active_frame = pending_frame if pending_frame and not pending_frame.get("applied") else {}
            if active_frame.get("submission_unknown") and not active_frame.get("task"):
                raise InputError("frame_submission_unknown", path=str(out / "inputs" / "frame-edit.json"))
            if frame_edit_task_pending(active_frame):
                raise InputError("frame_task_pending", task=active_frame["task"])
            seg_context = segments.read_context({"output_dir": str(out)})
            seg_pending = seg_context["pending"]
            if seg_pending and not seg_pending.get("applied"):
                _, pending_staged = segments.attempt_record(request, seg_context, seg_pending["number"])
                if (seg_pending.get("publishing") or seg_pending.get("generated")
                        or segments.task_unresolved(pending_staged)
                        or (pending_staged.get("submission_unknown") and not pending_staged.get("task"))):
                    raise InputError("segment_locked", number=seg_pending["number"])
            dirty = review_state(out).get("dirty_segments", [])
            affected = set()
            if dirty:
                total_segs = len(snapshot["review"]["plan"]["segments"])
                affected = (set(dirty) if request["frames"] == "shot"
                            else {num for num in range(1, total_segs + 1) if any(num >= d for d in dirty)})
                for seg_num in sorted(affected):
                    _, staged_seg = segments.attempt_record(request, seg_context, seg_num)
                    seg_rec = staged_seg or seg_context["records"].get(str(seg_num - 1), {})
                    if (seg_rec.get("submission_unknown") and not seg_rec.get("task")) or segments.task_unresolved(seg_rec):
                        raise InputError("segment_locked", number=seg_num)
            if confirmation:
                if confirmation.get("approved") is not True:
                    raise InputError("review_request")
                if confirmation.get("review_version") != snapshot["review"]["version"] or not snapshot["review"]["ready"]:
                    raise InputError("review_changed")
            elif not snapshot["review"]["complete"]:
                raise InputError("review_required")
            request.update(operation="pipeline", redo=review_state(out).get("dirty_segments", []), reset_applied=False)
            if seg_pending and not seg_pending.get("applied"):
                in_affected = seg_pending.get("number") in affected
                reconciled_segs, _, _ = segments.preview_reconciled_segments(out, keep_pending=not in_affected, supersede=in_affected)
                request["_reconciled_segs"] = reconciled_segs
            try:
                preflight(request, env)
            finally:
                request.pop("_reconciled_segs", None)
            if confirmation:
                review = review_state(out)
                review["confirmed_version"] = snapshot["review"]["version"]
                write_json(out / "inputs" / "review.json", review)
            if seg_pending and not seg_pending.get("applied"):
                in_affected = seg_pending.get("number") in affected
                segments.reconcile_pending_segment(out, keep_pending=not in_affected, supersede=in_affected)
            return self.restart(record, request, env)

    def restart(self, record, request, env):
        if record["kind"] == "film":
            from .storyboard import review_state
            out = Path(record["output_dir"])
            write_json(out / "inputs" / "review.json", review_state(out))
        write_json(Path(record["output_dir"]) / "inputs" / "request.json", request)
        record.update(status="queued", error=None, operation=request["operation"], operation_id=uuid.uuid4().hex)
        self.save(record)
        self.launch(record, request, env)
        return self.snapshot(record["id"])

    def start_video_operation(self, identifier, operation, env, version, number=None, *, prompt=None, prompt_format=None, prompt_version=None):
        from . import segments
        with self.lock:
            self.available()
            record = self.find(identifier)
            if record["kind"] != "film" or record["status"] not in ("succeeded", "failed", "interrupted"):
                raise InputError("segment_operation")
            snapshot = self.snapshot(identifier)
            if not snapshot["review"]["complete"] or snapshot["pending_video"]:
                raise InputError("review_required")
            if operation == "assemble":
                publishing_card = next((c for c in snapshot["segments"] if c["publishing"]), None)
                if publishing_card:
                    raise InputError("segment_publishing", number=publishing_card["number"])
                locked_card = next((c for c in snapshot["segments"] if c["task_pending"] or c["submission_unknown"]), None)
                if locked_card:
                    raise InputError("segment_locked", number=locked_card["number"])
            out = Path(record["output_dir"])
            request = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
            request.update(operation=operation)
            for key in ("redo", "reset_applied", "segment_revision"):
                request.pop(key, None)
            if operation == "segment":
                request["segment"] = number
                request["_segment_preparation"] = segments.regeneration_plan(request, number, version, prompt=prompt, prompt_format=prompt_format, prompt_version=prompt_version)
            preflight(request, env)
            request.pop("_segment_preparation", None)
            if operation == "segment":
                request["segment_revision"] = segments.prepare_regeneration(request, number, version, prompt=prompt, prompt_format=prompt_format, prompt_version=prompt_version)
            else:
                segments.prepare_assembly(request, version)
            return self.restart(record, request, env)

    def resume(self, identifier, env):
        with self.lock:
            record = self.find(identifier)
            snapshot = self.snapshot(identifier)
            remaining_frames = bool(snapshot.get('can_resume_frames'))
            remaining_pipeline = bool(snapshot.get('can_resume_pipeline'))
            if record["status"] not in ("failed", "interrupted") and not remaining_frames and not remaining_pipeline:
                raise InputError("resume")
            self.available()
            out_dir = Path(record["output_dir"])
            request = json.loads((out_dir / "inputs" / "request.json").read_text(encoding="utf-8"))
            if remaining_pipeline:
                request['operation'] = 'pipeline'
                for key in ('segment', 'segment_revision', 'redo', 'reset_applied'):
                    request.pop(key, None)
            elif remaining_frames:
                request["operation"] = "frames"
            if request.get("operation") in ("segment", "assemble") or remaining_pipeline:
                if not snapshot["review"]["complete"] or snapshot["pending_video"]:
                    raise InputError("review_required")
            elif request.get("operation") != "frame":
                one_inflight = False
                if request.get("kind") == "one" and (out_dir / "state.json").is_file():
                    try:
                        saved_one = json.loads((out_dir / "state.json").read_text(encoding="utf-8"))
                        one_inflight = bool(saved_one.get("task") and saved_one.get("h3"))
                    except (OSError, ValueError):
                        one_inflight = False
                if not one_inflight:
                    request = validate(request, self.root)
            if request["kind"] == "film" and request["operation"] == "pipeline" and not snapshot["review"]["complete"]:
                # Old/interrupted jobs cannot bypass the newly required review via resume.
                request["operation"] = "frames"
                request.pop("redo", None)
                request.pop("reset_applied", None)
            if request["kind"] == "one" and snapshot.get("can_resume_video") is False:
                raise InputError("resume")
            can_drain_pipeline = (
                remaining_pipeline
                and not any(c["submission_unknown"] for c in snapshot["segments"])
                and any(c["task_pending"] for c in snapshot["segments"])
            )
            if request["kind"] == "film" and request["operation"] in ("frame", "segment", "pipeline") and snapshot.get("can_resume_video") is False and not can_drain_pipeline:
                raise InputError("resume")
            if request["kind"] == "film" and request["operation"] == "frames":
                request["resume_frame_tasks"] = True
            has_inflight_segments = any(c["task_pending"] for c in snapshot["segments"])
            unblock_pipeline = request["kind"] == "film" and request.get("operation") == "pipeline" and not has_inflight_segments
            if remaining_pipeline or unblock_pipeline:
                from . import segments
                reconciled_segs, _, _ = segments.preview_reconciled_segments(
                    out_dir, keep_pending=False, supersede=False, drain_siblings=has_inflight_segments,
                )
                if unblock_pipeline:
                    reconciled_segs = {
                        k: ({rk: rv for rk, rv in rec.items() if rk not in ("attempt_blocked", "error")}
                            if rec.get("attempt_blocked") else rec)
                        for k, rec in reconciled_segs.items()
                    }
                request["_reconciled_segs"] = reconciled_segs
            try:
                preflight(request, env)
            finally:
                request.pop("_reconciled_segs", None)
            if remaining_pipeline:
                from . import segments
                segments.reconcile_pending_segment(out_dir, keep_pending=False, supersede=False, drain_siblings=has_inflight_segments)
            if unblock_pipeline:
                from h3ref import film
                job = film.Job(out_dir)
                cleared_blocker = False
                for rec in job.state.get("segs", {}).values():
                    if rec.pop("attempt_blocked", None) is not None:
                        rec.pop("error", None)
                        cleared_blocker = True
                if cleared_blocker:
                    job.save()
            elif remaining_frames and (out_dir / "inputs" / "frame-edit.json").exists():
                from h3ref import film, frames
                from .storyboard import read_json, save_frame_failure
                pending_frame = read_json(out_dir / "inputs" / "frame-edit.json", {})
                if pending_frame and not pending_frame.get("applied"):
                    if pending_frame.get("task_failed") or pending_frame.get("moderation_error"):
                        mod_err = pending_frame.get("moderation_error")
                        err_text = pending_frame.get("error") or (
                            str(frames.ContentPolicyError(mod_err)) if mod_err
                            else message("frame_task_failed", record.get("lang", "en"), task=pending_frame.get("task"), reason="")
                        )
                        save_frame_failure(request, dict(pending_frame), err_text, mod_err)
                    elif pending_frame.get("error") and not pending_frame.get("task"):
                        prev_frame = pending_frame.get("previous") or {}
                        has_valid_png = (
                            (out_dir / "frames" / f"{pending_frame['key']}.png").is_file()
                            and not prev_frame.get("error") and not prev_frame.get("moderation_error")
                        )
                        if not has_valid_png:
                            film.Job(out_dir).put(
                                "frames", pending_frame["key"], prompt=pending_frame.get("prompt", ""),
                                error=pending_frame["error"], task=None, task_failed=False, moderation_error=None,
                            )
                (out_dir / "inputs" / "frame-edit.json").unlink(missing_ok=True)
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
                env.update(H3_REWRITER="llm", H3_LANG=lang, H3_SKIP_DOTENV="1",
                           PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
                with self.lock:
                    if self.closing:
                        raise RuntimeError(message("interrupted", lang))
                    start = subprocess.Popen
                    if os.name == "nt":
                        from .processes import start_windows_child
                        start = start_windows_child
                    lock_handle = None
                    popen_extra = {}
                    if os.name != "nt" and record.get("output_dir") and (Path(record["output_dir"]) / "inputs").is_dir():
                        import fcntl
                        lock_handle = (Path(record["output_dir"]) / "inputs" / "worker.lock").open("a+b")
                        try:
                            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError as exc:
                            lock_handle.close()
                            lock_handle = None
                            raise RuntimeError(message("alive", lang, pid=record.get("pid") or identifier)) from exc
                        popen_extra["pass_fds"] = (lock_handle.fileno(),)
                    try:
                        child_env = {**env, "H3_WORKBENCH_WORKER": "1"}
                        child = start(command(request, self.root), cwd=self.root, env=child_env, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                      start_new_session=(os.name != "nt"), **popen_extra)
                    finally:
                        if lock_handle is not None:
                            lock_handle.close()
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
            cleanup_failed = False
            if child is not None:
                try:
                    stop_child(child)
                except Exception:
                    cleanup_failed = True
            with self.lock:
                self.children.pop(identifier, None)
                if not cleanup_failed:
                    record.pop("pid", None)
                self.save(record)

    def close(self):
        with self.lock:
            self.closing = True
            children = list(self.children.values())
        for child in children:
            try:
                stop_child(child)
            except Exception:
                pass
