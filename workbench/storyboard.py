"""Review individual frames using the original film/frames APIs, never a second generator."""
import json
import hashlib
import shutil
import sys
from pathlib import Path

from h3ref import config, film, frames
from h3ref.i18n import use_lang
from .i18n import InputError, message
from .jobs import write_json


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def plan(request, check_images=True, *, records=None):
    out = Path(request["output_dir"])
    source = out / "inputs" / ("demo-film.json" if request["demo"] else "film.json")
    script = film.load(source, check_images=check_images)
    segments = film.plan(script["shots"])
    pictures = {s["idx"]: film.refs(script, s, request["frames"]) for s in segments}
    wanted = [(s["idx"], p["shot"]) for s in segments for p in pictures[s["idx"]] if p["kind"] == "frame"]
    descriptions = []
    records = frame_records(request) if records is None else records
    pending = read_json(out / "inputs" / "frame-edit.json", {})
    active_pending = pending if pending and not pending.get("applied") else {}
    compositions = film.saved_compositions(records, reference_versions=film.frame_reference_versions(script, segments, records))
    character_names = {c["picture"]: c["name"] for c in script.get("cast") or []}
    for segment, shot in wanted:
        key = f"s{segment + 1}-{shot + 1}"
        pending_for_key = active_pending if active_pending.get("key") == key else {}
        who = [c for c in script.get("cast") or [] if film.in_shots(c, [segments[segment]["shots"][shot]])]
        characters = pending_for_key.get("character_reference_paths",
                                         records.get(key, {}).get("character_reference_paths", [c["picture"] for c in who]))
        references = ([{"kind": "board", "path": script["board"], "name": ""}] if script.get("board") else [])
        references += [{"kind": "character", "path": p, "name": character_names.get(p, "")} for p in characters]
        cached = records.get(key, {})
        composition = compositions.get(key) or (cached.get("composition") if (cached.get("composition") or {}).get("version") != 2 else None)
        with use_lang(request.get("lang")):
            prompt = pending_for_key.get("prompt") or cached.get("prompt") or frames.prompt(script, segments[segment]["shots"][shot], who, bool(script.get("board")), composition)
        descriptions.append({"key": key, "segment": segment + 1, "shot": shot + 1, "prompt": prompt,
                             "references": references, "reference_paths": [r["path"] for r in references],
                             "path": str(out / "frames" / f"{key}.png")})
    return script, segments, pictures, descriptions


def frame_records(request):
    out = Path(request["output_dir"])
    path = out / "inputs" / "demo-frames.json" if request["demo"] else out / "state.json"
    return read_json(path, {"frames": {}}).get("frames", {})


def character_references(request, identifier):
    inputs = Path(request["output_dir"]) / "inputs"
    source = inputs / "demo-film.json" if request["demo"] and (inputs / "demo-film.json").exists() else inputs / "film.json"
    script = film.load(source, check_images=False)
    references = []
    for index, character in enumerate(script.get("cast") or []):
        path = Path(character["picture"])
        url = f"/api/jobs/{identifier}/characters/{index}?v={path.stat().st_mtime_ns}" if path.is_file() else None
        references.append({"kind": "character", "path": str(path), "name": character["name"], "url": url})
    return references


def unsubmitted_frame_pending(pending: dict) -> bool:
    return bool(pending and not pending.get("applied") and not pending.get("downloaded")
                and not pending.get("task") and not pending.get("submission_unknown"))


def frame_edit_task_pending(pending: dict) -> bool:
    return bool(pending and not pending.get("applied") and pending.get("task")
                and not pending.get("task_failed") and not pending.get("moderation_error"))


def can_edit_frames(request, status):
    if status == "succeeded":
        return True
    if status not in ("failed", "interrupted"):
        return False
    pending = read_json(Path(request["output_dir"]) / "inputs" / "frame-edit.json", {})
    return request.get("operation") != "frame" or bool(
        pending.get("moderation_error") or pending.get("task_failed") or unsubmitted_frame_pending(pending)
    )


def _cached_mtime_ns(path_str, cache):
    if path_str not in cache:
        path = Path(path_str)
        try:
            st = path.stat()
            cache[path_str] = st.st_mtime_ns if path.is_file() else None
        except OSError:
            cache[path_str] = None
    return cache[path_str]


def view(request, artifacts, editable, identifier=None, failed_only=False, *, records=None):
    out = Path(request["output_dir"])
    source = out / "inputs" / ("demo-film.json" if request["demo"] else "film.json")
    if not source.exists():
        return []
    records = frame_records(request) if records is None else records
    pending = read_json(out / "inputs" / "frame-edit.json", {})
    active_pending = pending if pending and not pending.get("applied") else {}
    _, _, _, descriptions = plan(request, check_images=False, records=records)
    urls = {a["path"]: a["url"] for a in artifacts if a["kind"] == "image"}
    stat_cache = {}
    for description in descriptions:
        rec = records.get(description["key"], {})
        pending_for_key = active_pending if active_pending.get("key") == description["key"] else {}
        missing = [p for p in description["reference_paths"] if _cached_mtime_ns(p, stat_cache) is None]
        error = rec.get("error") or rec.get("moderation_error") or pending_for_key.get("error") or pending_for_key.get("moderation_error")
        unsubmitted = unsubmitted_frame_pending(pending_for_key)
        available = description["path"] in urls or bool(error) or unsubmitted
        task_pending = frames.task_pending(rec) or frame_edit_task_pending(pending_for_key)
        submission_unknown = bool((rec.get("submission_unknown") and not rec.get("task"))
                                  or (pending_for_key.get("submission_unknown") and not pending_for_key.get("task")))
        can_edit = editable and available and not task_pending and not submission_unknown and (not failed_only or bool(error) or unsubmitted)
        raw_prompt = pending_for_key.get("prompt") or rec.get("failed_prompt") or rec.get("prompt", description["prompt"])
        description.update(prompt=frames.without_anchor_reference(raw_prompt),
                           url=urls.get(description["path"]), error=error,
                           moderation_failed=bool(rec.get("moderation_error") or pending_for_key.get("moderation_error")),
                           task_pending=task_pending, submission_unknown=submission_unknown,
                           editable=can_edit and not missing, missing_references=missing,
                           references_editable=can_edit)
        for index, reference in enumerate(description["references"]):
            mtime_ns = _cached_mtime_ns(reference["path"], stat_cache)
            reference["url"] = (f"/api/jobs/{identifier}/frames/{description['key']}/references/{index}?v={mtime_ns}"
                                if identifier and mtime_ns is not None else None)
    return descriptions


def review_state(out):
    saved = read_json(out / "inputs" / "review.json", {"dirty_segments": []})
    request = read_json(out / "inputs" / "request.json", {})
    if request.get("operation") == "pipeline" and request.get("reset_applied"):
        applied = set(request.get("redo") or [])
        saved["dirty_segments"] = [number for number in saved.get("dirty_segments", []) if number not in applied]
    return saved


def review_status(request, descriptions):
    out = Path(request["output_dir"])
    saved = review_state(out)
    pending = read_json(out / "inputs" / "frame-edit.json", {})
    active_pending = bool(pending and not pending.get("applied"))
    versions = []
    for description in descriptions:
        path = Path(description["path"])
        stat = path.stat() if path.is_file() else None
        version = f"{stat.st_mtime_ns}:{stat.st_size}" if stat else None
        description["version"] = version
        versions.append((description["key"], version))
    script = request["film"]
    signature = json.dumps([script, request["frames"], versions], ensure_ascii=False, sort_keys=True)
    version = hashlib.sha256(signature.encode()).hexdigest()
    segments = film.plan(script["shots"])
    total = sum(p["kind"] == "frame" for segment in segments for p in film.refs(script, segment, request["frames"]))
    has_frames = total > 0
    ready = (not active_pending and len(descriptions) == total
             and all(d["version"] and d["url"] and not d.get("error") and not d.get("task_pending") and not d.get("submission_unknown")
                     for d in descriptions))
    complete = ready and saved.get("confirmed_version") == version
    return {"kind": "frames" if has_frames else "script", "total": total, "ready": ready, "version": version,
            "complete": complete,
            "plan": {"title": script.get("title", ""), "seconds": sum(s["seconds"] for s in segments), "segments": segments}}


def save_frame_failure(request, pending, error, moderation_error):
    out = Path(request["output_dir"])
    pending.update(error=error, task_failed=True, moderation_error=moderation_error)
    write_json(out / "inputs" / "frame-edit.json", pending)
    film.Job(out).put("frames", pending["key"], error=error, task=pending.get("task"),
                      task_failed=True, moderation_error=moderation_error, failed_prompt=pending["prompt"])


def regenerate(request):
    out = Path(request["output_dir"])
    pending_path = out / "inputs" / "frame-edit.json"
    pending = read_json(pending_path, {})
    if pending.get("applied"):
        return
    if pending.get("moderation_error"):
        error = frames.ContentPolicyError(pending["moderation_error"])
        save_frame_failure(request, pending, str(error), pending["moderation_error"])
        raise error
    if pending.get("submission_unknown") and not pending.get("task"):
        raise RuntimeError(message("frame_submission_unknown", request["lang"], path=pending_path))
    if pending.get("task_failed"):
        error = pending.get("error") or message("frame_task_failed", request["lang"], task=pending.get("task"), reason="")
        save_frame_failure(request, pending, error, None)
        raise RuntimeError(error)
    script, _, _, descriptions = plan(request, check_images=False)
    description = next((item for item in descriptions if item["key"] == pending.get("key")), None)
    if not description:
        raise InputError("not_found", value=pending.get("key"))
    target = Path(description["path"])
    temporary = out / "inputs" / f"frame-{pending['revision']}.png"

    def record(**values):
        pending.update(values)
        write_json(pending_path, pending)

    if not pending.get("task") and not pending.get("downloaded"):
        references = description["references"]
        characters = pending.get("character_reference_paths", [r["path"] for r in references if r["kind"] == "character"])
        record(prompt=frames.without_anchor_reference(pending["prompt"]),
               reference_paths=[r["path"] for r in references if r["kind"] == "board"] + characters)

    if not pending.get("downloaded"):
        if request["demo"]:
            from .demo import placeholder
            placeholder(temporary, f"{pending['key']} / {pending['revision'][:6]}", script["aspect"])
            cost = 0
        else:
            # Resume the saved task; a terminal failure waits for a new manual edit.
            try:
                record(error=None)
                if pending.get("task"):
                    status = frames.poll(pending["task"])
                    if status["state"] == "failed":
                        if status.get("moderation_failed") or frames.is_moderation_error(status.get("error")):
                            raise frames.ContentPolicyError(status.get("error"))
                        record(task_failed=True, error=str(status.get("error")))
                        raise RuntimeError(str(status.get("error")))
                cost = frames.draw(pending["prompt"], [Path(p) for p in pending.get("reference_paths", description["reference_paths"])], script["aspect"], temporary,
                                   record=record, task=pending.get("task"))
            except frames.ContentPolicyError as exc:
                save_frame_failure(request, pending, str(exc), exc.reason)
                raise
            except (Exception, SystemExit) as exc:
                if pending.get("task_failed"):
                    save_frame_failure(request, pending, str(exc), None)
                else:
                    record(error=str(exc))
                raise
        record(downloaded=True, cny=cost)

    # Persist invalidation before swapping pixels. A crash can resume this transaction
    # without paying again; the old image stays available until download succeeds.
    review = review_state(out)
    review["dirty_segments"] = sorted(set(review["dirty_segments"]) | {description["segment"]})
    review.pop("confirmed_version", None)
    write_json(out / "inputs" / "review.json", review)
    backup = out / "inputs" / f"previous-frame-{pending['revision']}.png"
    if target.exists() and temporary.exists():
        backup_temporary = backup.with_suffix(".tmp")
        shutil.copy2(target, backup_temporary)
        config.atomic_replace(backup_temporary, backup)
    if temporary.exists():
        target.parent.mkdir(exist_ok=True)
        config.atomic_replace(temporary, target)
    if not target.exists():
        raise RuntimeError(message("frame_missing", request["lang"], path=target))
    previous = pending["previous"]
    updated = {**previous, "prompt": pending["prompt"], "file": str(target), "task": pending.get("task"),
               "cny": float(previous.get("cny") or 0) + float(pending["cny"]), "revision": pending["revision"],
               "error": None, "moderation_error": None, "failed_prompt": None, "task_failed": False}
    if "character_reference_paths" in pending:
        updated["character_reference_paths"] = pending["character_reference_paths"]
    if request["demo"]:
        records = frame_records(request)
        records[pending["key"]] = updated
        write_json(out / "inputs" / "demo-frames.json", {"frames": records})
    else:
        job = film.Job(out)
        job.put("frames", pending["key"], **updated)
    record(applied=True)
    print(message("frame_done", request["lang"], frame=pending["key"]), flush=True)


def run(request):
    operation = request["operation"]
    out = Path(request["output_dir"])
    if operation == "frame":
        regenerate(request)
    elif request["demo"]:
        from .demo import run as demo_run
        demo_run(request)
    elif operation == "frames":
        script, segments, pictures, _ = plan(request)
        job = film.Job(out)
        job.state.update(film_path=str(out / "inputs" / "film.json"), mode=request["frames"])
        job.save()
        failures = film.stage_frames(job, script, segments, pictures, resume_tasks=request.get("resume_frame_tasks", False))
        print(message("frames_partial", request["lang"], count=len(failures)) if failures else message("frames_ready", request["lang"]), flush=True)
    else:
        # Apply the CLI's --redo rule once, before any new paid generation. Retries
        # use the saved tasks instead of resetting them again with repeated --redo.
        if not request.get("reset_applied"):
            fjob = film.Job(out)
            if film.unaffected_segment_blocker(fjob, request["frames"], request.get("redo", [])) is None:
                film.invalidate_segments(fjob, request["frames"], request.get("redo", []))
                request["reset_applied"] = True
                write_json(out / "inputs" / "request.json", request)
        film.run(out, out / "inputs" / "film.json", request["frames"])


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
