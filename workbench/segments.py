"""Review, regenerate one clip, and assemble saved clips without rerunning the film."""
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

from h3ref import film, media
from .i18n import InputError, message
from .jobs import write_json
from .storyboard import read_json, review_state


def clip_version(path, out):
    if not path.is_file() or not path.resolve().is_relative_to(out.resolve()):
        return None
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}" if stat.st_size else None


def read_context(request):
    """One immutable read context per snapshot, including an interrupted publication."""
    out = Path(request['output_dir'])
    state = read_json(out / 'state.json', {'frames': {}, 'segs': {}})
    pending = read_json(out / 'inputs' / 'segment-edit.json', {})
    records = dict(state.get('segs', {}))
    if pending.get('publishing') and not pending.get('applied'):
        records[str(pending['number'] - 1)] = pending['previous']
    return {'state': state, 'records': records, 'pending': pending, 'automatic': {}, 'path_versions': {}}


def visible_file(out, name, context, version=None):
    target = out / name
    entry = context['pending'].get('files', {}).get(name)
    pending = context['pending']
    source = Path(entry['previous']) if entry and entry.get('previous') and pending.get('publishing') and not pending.get('applied') else target
    if entry and pending.get('publishing') and not pending.get('applied') and not entry.get('previous'):
        return None
    def matches(path):
        token = clip_version(path, out)
        return token and (version is None or str(version) in (token, token.split(':')[0]))
    if matches(source):
        return source
    if version is not None and entry and entry.get('previous'):
        backup = Path(entry['previous'])
        if matches(backup):
            return backup
    return None


def clip_paths(request, context=None):
    out = Path(request['output_dir'])
    context = context or read_context(request)
    return [visible_file(out, f"clips/seg{s['idx'] + 1}.mp4", context) or out / f"clips/seg{s['idx'] + 1}.mp4"
            for s in film.plan(request['film']['shots'])]


def assembly_status(request, context=None):
    out = Path(request['output_dir'])
    paths = clip_paths(request, context)
    versions = [clip_version(path, out) for path in paths]
    version = hashlib.sha256(json.dumps(versions).encode()).hexdigest()
    ready = bool(paths) and all(versions)
    saved = read_json(out / 'inputs' / 'assembly.json', {})
    final = out / 'film.mp4'
    if saved:
        pending = saved.get('version') != version or not clip_version(final, out)
    else:
        pending = any(versions) and (not clip_version(final, out) or any(
            path.stat().st_mtime_ns > final.stat().st_mtime_ns for path in paths if clip_version(path, out)))
    return {'version': version, 'ready': bool(ready), 'pending': bool(pending)}


def record_assembly(request):
    out = Path(request["output_dir"])
    summary = assembly_status(request)
    if summary["ready"] and clip_version(out / "film.mp4", out):
        write_json(out / "inputs" / "assembly.json", {"version": summary["version"]})


def view(request, artifacts, editable, *, context=None):
    out = Path(request["output_dir"])
    urls = {a["path"]: a["url"] for a in artifacts if a["kind"] == "video"}
    planned = film.plan(request["film"]["shots"])
    context = context or read_context(request)
    records = context["records"]
    cards = []
    for segment in planned:
        number = segment["idx"] + 1
        path = out / "clips" / f"seg{number}.mp4"
        version = clip_version(visible_file(out, f"clips/seg{number}.mp4", context) or path, out)
        cards.append({"number": number, "start": segment["start"], "end": segment["end"],
                      "seconds": segment["seconds"], "shots": len(segment["shots"]),
                      "url": urls.get(str(path)) if version else None, "version": version,
                      "can_regenerate": bool(editable and version and (request["demo"] or records.get(str(number - 1), {}).get("h3"))),
                      "chained_next": request["frames"] != "shot" and number < len(planned)})
    return cards


def selected_segment(request, number):
    planned = film.plan(request["film"]["shots"])
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(planned):
        raise InputError("segment_number", number=number, count=len(planned))
    return planned[number - 1]


def preflight(request, env):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise InputError("segment_ffmpeg")
    if request["operation"] == "assemble":
        return
    segment = selected_segment(request, request.get("segment"))
    if request["demo"]:
        return
    out = Path(request["output_dir"])
    pending = read_json(out / "inputs" / "segment-edit.json", {})
    if pending.get("revision") != request.get("segment_revision"):
        pending = {}
    if pending.get("applied") and pending.get("number") == request["segment"]:
        return
    if pending.get("generated"):
        return
    if not env.get("RUNNINGHUB_API_KEY", "").strip():
        raise InputError("keys", names="RUNNINGHUB_API_KEY")
    # Once submitted, polling/downloading does not need the source images again.
    staged = read_json(out / "inputs" / f"segment-{pending.get('revision', '')}" / "state.json", {})
    if staged.get("segs", {}).get(str(segment["idx"]), {}).get("task"):
        return
    pics = film.refs(request["film"], segment, request["frames"])
    for path in film._picture_paths(film.Job(out), segment, pics):
        if not path.is_file():
            raise InputError("missing_image", path=str(path))


def prepare_regeneration(request, number, version):
    out = Path(request["output_dir"])
    selected_segment(request, number)
    current = clip_version(out / "clips" / f"seg{number}.mp4", out)
    if not current or current != version:
        raise InputError("segment_changed", number=number)
    previous = read_json(out / "state.json", {}).get("segs", {}).get(str(number - 1), {})
    if not request["demo"] and not previous.get("h3"):
        raise InputError("segment_prompt", number=number)
    if not (out / "inputs" / "assembly.json").exists() and not assembly_status(request)["pending"]:
        # Remember the old film's inputs before a replacement can change them.
        record_assembly(request)
    revision = uuid.uuid4().hex
    write_json(out / "inputs" / "segment-edit.json", {
        "number": number, "revision": revision, "version": version, "previous": previous, "applied": False})
    return revision


def publish_file(source, target):
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(target)


def regenerate(request):
    out = Path(request["output_dir"])
    pending_path = out / "inputs" / "segment-edit.json"
    pending = read_json(pending_path, {})
    if pending.get("revision") != request.get("segment_revision"):
        raise InputError("segment_changed", number=request.get("segment"))
    segment = selected_segment(request, pending.get("number"))
    if pending.get("applied"):
        return
    number, index = pending["number"], str(segment["idx"])
    target = out / "clips" / f"seg{number}.mp4"
    tail = target.with_name(f"seg{number}-last.jpg")
    if not pending.get("publishing") and clip_version(target, out) != pending["version"]:
        raise InputError("segment_changed", number=number)
    staged = film.Job(out / "inputs" / f"segment-{pending['revision']}")
    (staged.dir / "clips").mkdir(exist_ok=True)
    candidate = staged.dir / "clips" / target.name
    candidate_tail = candidate.with_name(tail.name)
    if index not in staged.state["segs"]:
        staged.put("segs", index, **{key: value for key, value in pending["previous"].items() if key in ("h3", "zh", "pics")})
    if not pending.get("generated"):
        if request["demo"]:
            from .demo import render_clip
            render_clip(candidate, segment["seconds"], request["film"]["aspect"], f"seg{number} / {pending['revision'][:6]}")
            media.last_frame(candidate, candidate_tail)
            staged.put("segs", index, file=str(candidate), cny=0, run_s=0)
        else:
            pics = film.refs(request["film"], segment, request["frames"])
            paths = film._picture_paths(film.Job(out), segment, pics)
            film.gen_segment(staged, request["film"], segment, pics, reference_paths=paths, guard_submission=True)
            film.stage_check(staged, request["film"], [segment])
        if not clip_version(candidate, staged.dir) or media.duration(candidate) <= 0 or not candidate_tail.is_file():
            raise InputError("segment_media", path=str(candidate))
        pending["generated"] = True
        write_json(pending_path, pending)
    group = [(candidate, target), (candidate_tail, tail)]
    for suffix in ('zh', 'h3'):
        prompt = staged.dir / 'prompts' / f'seg{number}.{suffix}.txt'
        if prompt.is_file():
            group.append((prompt, out / 'prompts' / prompt.name))
    if not pending.get('publishing'):
        files = {}
        for source, path in group:
            backup = staged.dir / f'previous-{path.name}'
            if path.is_file():
                publish_file(path, backup)
            files[path.relative_to(out).as_posix()] = {'previous': str(backup) if path.is_file() else None,
                                                       'previous_version': clip_version(backup, out),
                                                       'next': str(source), 'next_version': clip_version(source, out)}
        write_json(staged.dir / 'previous.json', pending['previous'])
        pending.update(publishing=True, files=files)
        write_json(pending_path, pending)
    for source, path in group:
        path.parent.mkdir(exist_ok=True)
        publish_file(source, path)
    active = film.Job(out)
    active.state["segs"][index] = {**staged.state["segs"][index], "file": str(target), "revision": pending["revision"]}
    active.save()
    pending["applied"] = True
    write_json(pending_path, pending)
    print(message("segment_done", request["lang"], number=number), flush=True)


def require_committed_publication(request):
    pending = read_context(request)['pending']
    if pending.get('publishing') and not pending.get('applied'):
        raise InputError('segment_publishing', number=pending['number'])


def prepare_assembly(request, version):
    require_committed_publication(request)
    summary = assembly_status(request)
    if not summary["ready"]:
        raise InputError("assembly_missing")
    if not version or version != summary["version"]:
        raise InputError("assembly_changed")
    write_json(Path(request["output_dir"]) / "inputs" / "assembly-edit.json", {
        "version": version, "revision": uuid.uuid4().hex, "applied": False})


def assemble(request):
    require_committed_publication(request)
    out = Path(request["output_dir"])
    pending_path = out / "inputs" / "assembly-edit.json"
    pending = read_json(pending_path, {})
    if pending.get("applied"):
        return
    summary = assembly_status(request)
    if not summary["ready"] or summary["version"] != pending.get("version"):
        raise InputError("assembly_changed")
    candidate = out / "inputs" / f"assembled-{pending['revision']}.mp4"
    if not pending.get("generated"):
        media.join(clip_paths(request), candidate)
        if media.duration(candidate) <= 0:
            raise InputError("segment_media", path=str(candidate))
        pending["generated"] = True
        write_json(pending_path, pending)
    final = out / "film.mp4"
    backup = out / "inputs" / f"previous-film-{pending['revision']}.mp4"
    if final.is_file() and not backup.exists():
        publish_file(final, backup)
    publish_file(candidate, final)
    if request["demo"]:
        planned = film.plan(request["film"]["shots"])
        report = message("demo_report", request["lang"], kind="film", mode=request["frames"], segments=len(planned),
                         frames=len(list((out / "frames").glob("*.png"))), seconds=sum(s["seconds"] for s in planned),
                         path=out, video=message("demo_video", request["lang"]))
        (out / "report.md").write_text(report, encoding="utf-8")
    else:
        film.report(film.Job(out), request["film"], film.plan(request["film"]["shots"]), request["frames"], final)
    record_assembly(request)
    pending["applied"] = True
    write_json(pending_path, pending)
    print(message("assembly_done", request["lang"], path=str(final)), flush=True)


if __name__ == "__main__":
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    {"segment": regenerate, "assemble": assemble}[request["operation"]](request)
