"""Review individual frames using the original film/frames APIs, never a second generator."""
import json
import hashlib
import shutil
import sys
from pathlib import Path

from h3ref import film, frames
from .i18n import InputError, message
from .jobs import write_json


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def plan(request, check_images=True):
    out = Path(request["output_dir"])
    source = out / "inputs" / ("demo-film.json" if request["demo"] else "film.json")
    script = film.load(source, check_images=check_images)
    segments = film.plan(script["shots"])
    pictures = {s["idx"]: film.refs(script, s, request["frames"]) for s in segments}
    wanted = [(s["idx"], p["shot"]) for s in segments for p in pictures[s["idx"]] if p["kind"] == "frame"]
    descriptions = []
    records = frame_records(request)
    character_names = {c["picture"]: c["name"] for c in script.get("cast") or []}
    for index, (segment, shot) in enumerate(wanted):
        key = f"s{segment + 1}-{shot + 1}"
        who = [c for c in script.get("cast") or [] if film.in_shots(c, [segments[segment]["shots"][shot]])]
        anchor = out / "frames" / f"s{wanted[0][0] + 1}-{wanted[0][1] + 1}.png" if index else None
        characters = records.get(key, {}).get("character_reference_paths", [c["picture"] for c in who])
        references = ([{"kind": "board", "path": script["board"], "name": ""}] if script.get("board") else [])
        references += [{"kind": "character", "path": p, "name": character_names.get(p, "")} for p in characters]
        if anchor:
            references.append({"kind": "anchor", "path": str(anchor), "name": ""})
        prompt = frames.prompt(script, segments[segment]["shots"][shot], who, bool(script.get("board")), bool(anchor))
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


def view(request, artifacts, editable, identifier=None):
    out = Path(request["output_dir"])
    source = out / "inputs" / ("demo-film.json" if request["demo"] else "film.json")
    if not source.exists():
        return []
    _, _, _, descriptions = plan(request, check_images=False)
    records = frame_records(request)
    urls = {a["path"]: a["url"] for a in artifacts if a["kind"] == "image"}
    for description in descriptions:
        rec = records.get(description["key"], {})
        missing = [p for p in description["reference_paths"] if not Path(p).is_file()]
        description.update(prompt=rec.get("prompt", description["prompt"]), url=urls.get(description["path"]),
                           editable=editable and description["path"] in urls and not missing, missing_references=missing,
                           references_editable=editable and description["path"] in urls)
        for index, reference in enumerate(description["references"]):
            path = Path(reference["path"])
            reference["url"] = (f"/api/jobs/{identifier}/frames/{description['key']}/references/{index}?v={path.stat().st_mtime_ns}"
                                if identifier and path.is_file() else None)
    return descriptions


def review_state(out):
    return read_json(out / "inputs" / "review.json", {"dirty_segments": []})


def review_status(request, descriptions):
    out = Path(request["output_dir"])
    saved = review_state(out)
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
    ready = len(descriptions) == total and all(d["version"] and d["url"] for d in descriptions)
    complete = ready and saved.get("confirmed_version") == version
    return {"kind": "frames" if has_frames else "script", "total": total, "ready": ready, "version": version,
            "complete": complete,
            "plan": {"title": script.get("title", ""), "seconds": sum(s["seconds"] for s in segments), "segments": segments}}


def regenerate(request):
    out = Path(request["output_dir"])
    pending_path = out / "inputs" / "frame-edit.json"
    pending = read_json(pending_path, {})
    if pending.get("applied"):
        return
    script, _, _, descriptions = plan(request, check_images=False)
    description = next((item for item in descriptions if item["key"] == pending.get("key")), None)
    if not description:
        raise InputError("not_found", value=pending.get("key"))
    target = Path(description["path"])
    temporary = out / "inputs" / f"frame-{pending['revision']}.png"

    def record(**values):
        pending.update(values)
        write_json(pending_path, pending)

    if not pending.get("downloaded"):
        if request["demo"]:
            from .demo import placeholder
            placeholder(temporary, f"{pending['key']} / {pending['revision'][:6]}", script["aspect"])
            cost = 0
        else:
            # Resume an uncertain task; only a confirmed terminal failure gets a new submission.
            if pending.get("task") and frames.poll(pending["task"])["state"] == "failed":
                record(task=None)
            cost = frames.draw(pending["prompt"], [Path(p) for p in pending.get("reference_paths", description["reference_paths"])], script["aspect"], temporary,
                               record=record, task=pending.get("task"))
        record(downloaded=True, cny=cost)

    # Persist invalidation before swapping pixels. A crash can resume this transaction
    # without paying again; the old image stays available until download succeeds.
    review = review_state(out)
    review["dirty_segments"] = sorted(set(review["dirty_segments"]) | {description["segment"]})
    review.pop("confirmed_version", None)
    write_json(out / "inputs" / "review.json", review)
    backup = out / "inputs" / f"previous-frame-{pending['revision']}.png"
    if target.exists() and not backup.exists():
        shutil.copy2(target, backup)
    if temporary.exists():
        temporary.replace(target)
    if not target.exists():
        raise RuntimeError(message("frame_missing", request["lang"], path=target))
    previous = pending["previous"]
    updated = {**previous, "prompt": pending["prompt"], "file": str(target), "task": pending.get("task"),
               "cny": float(previous.get("cny") or 0) + float(pending["cny"]), "revision": pending["revision"]}
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
        film.stage_frames(job, script, segments, pictures)
        print(message("frames_ready", request["lang"]), flush=True)
    else:
        # Apply the CLI's --redo rule once, before any new paid generation. Retries
        # use the saved tasks instead of resetting them again with repeated --redo.
        if not request.get("reset_applied"):
            film.invalidate_segments(film.Job(out), request["frames"], request.get("redo", []))
            request["reset_applied"] = True
            write_json(out / "inputs" / "request.json", request)
        film.run(out, out / "inputs" / "film.json", request["frames"])


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
