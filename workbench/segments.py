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
    pending_stamp = clip_version(out / 'inputs' / 'segment-edit.json', out)
    state = read_json(out / 'state.json', {'frames': {}, 'segs': {}})
    pending = read_json(out / 'inputs' / 'segment-edit.json', {})
    records = dict(state.get('segs', {}))
    current = records.get(str(pending.get('number', 0) - 1), {})
    publication_current = (not pending.get('applied') or not pending.get('active')
                           or current.get('revision') == pending.get('revision') or current == pending.get('previous'))
    if pending.get('publishing') and not pending.get('applied'):
        records[str(pending['number'] - 1)] = pending['previous']
    elif pending.get('applied') and pending.get('active') and publication_current:
        records[str(pending['number'] - 1)] = pending['active']
    return {'pending_stamp': pending_stamp, 'state': state, 'records': records, 'pending': pending, 'publication_current': publication_current, 'automatic': {}, 'path_versions': {}, 'file_versions': {}}


def file_version(path, out, context):
    key = str(path)
    if key not in context['file_versions']:
        context['file_versions'][key] = clip_version(path, out)
    return context['file_versions'][key]


def visible_file(out, name, context, version=None):
    target = out / name
    entry = context['pending'].get('files', {}).get(name)
    pending = context['pending']
    source = target
    if entry and pending.get('publishing') and context['publication_current']:
        selected = entry.get('next') if pending.get('applied') else entry.get('previous')
        source = Path(selected) if selected else target
    if entry and pending.get('publishing') and not pending.get('applied') and not entry.get('previous'):
        return None
    def matches(path):
        token = file_version(path, out, context)
        return token and (version is None or str(version) in (token, token.split(':')[0]))
    if matches(source):
        return source
    if version is not None and entry:
        for saved in ('previous', 'next'):
            if entry.get(saved) and matches(Path(entry[saved])):
                return Path(entry[saved])
    return None


def clip_paths(request, context=None):
    out = Path(request['output_dir'])
    context = context or read_context(request)
    return [visible_file(out, f"clips/seg{s['idx'] + 1}.mp4", context) or out / f"clips/seg{s['idx'] + 1}.mp4"
            for s in film.plan(request['film']['shots'])]


def assembly_status(request, context=None):
    out = Path(request['output_dir'])
    context = context or read_context(request)
    paths = clip_paths(request, context)
    versions = [file_version(path, out, context) for path in paths]
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


def prompt_pair(record, out, number, context):
    """Resolve saved source/H3 locally; a direct H3 override has no paired source."""
    def saved(suffix):
        path = visible_file(out, f'prompts/seg{number}.{suffix}.txt', context)
        return path.read_text(encoding='utf-8') if path else ''
    override = record.get('prompt_override') or {}
    h3 = record.get('h3') or saved('h3')
    if override.get('format') == 'h3':
        return {'source': '', 'h3': h3 or override['text'], 'paired': False}
    source = record.get('zh') or saved('zh')
    paired = bool(source and h3 and record.get('h3_source', source) == source)
    return {'source': source, 'h3': h3, 'paired': paired}


def segment_prompt_version(record: dict, version: str | None, pending: dict | None = None, *, reference_version=None) -> str:
    attempt = {key: (pending or {}).get(key) for key in ('revision', 'prompt', 'prompt_format', 'applied')}
    payload = [version, record.get('zh'), record.get('h3'), record.get('prompt_override'), record.get('revision'), attempt, reference_version]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_rewrite_required(record, pair, reference_version):
    return not (pair['paired'] and (not record.get('h3_reference_version') or record['h3_reference_version'] == reference_version))


def automatic_inputs(request, number, context):
    if number not in context['automatic']:
        from types import SimpleNamespace
        if 'planned' not in context:
            context['planned'] = film.plan(request['film']['shots'])
        planned = context['planned']
        segment = planned[number - 1]
        records = context['state'].get('frames', {})
        if 'compositions' not in context and not records:
            context['compositions'] = {}
        if 'compositions' not in context:
            versions = film.frame_reference_versions(request['film'], planned, records)
            context['compositions'] = film.saved_compositions(records, reference_versions=versions)
        pictures = film.refs(request['film'], segment, request['frames'])
        paths = film._picture_paths(SimpleNamespace(dir=Path(request['output_dir'])), segment, pictures)
        source = film.compile_prompt(request['film'], segment, pictures, len(planned), compositions=context['compositions'])
        reference_version = film.reference_version(paths, metadata=context['path_versions'])
        source_version = hashlib.sha256(json.dumps([source, reference_version], ensure_ascii=False).encode()).hexdigest()
        context['automatic'][number] = {'source': source, 'source_version': source_version,
                                        'reference_version': reference_version, 'pictures': pictures, 'paths': paths}
    return context['automatic'][number]


def segment_source_version(request: dict, number: int) -> str:
    selected_segment(request, number)
    return automatic_inputs(request, number, read_context(request))['source_version']


def attempt_record(request, context, number):
    pending = context['pending']
    if pending.get('number') != number or pending.get('applied'):
        return {}, {}
    if 'staged' not in context:
        path = Path(request['output_dir']) / 'inputs' / f"segment-{pending['revision']}" / 'state.json'
        context['staged'] = read_json(path, {}).get('segs', {}).get(str(number - 1), {})
    return pending, context['staged']


def task_unresolved(record):
    return bool(record.get('task') and not record.get('terminal_failed')
                and not (record.get('file') and record.get('cny') is not None))


def can_edit_segments(request, status, context=None):
    if status == 'succeeded':
        return True
    if status not in ('failed', 'interrupted'):
        return False
    context = context or read_context(request)
    # Local and terminal failures may be corrected; accepted/unknown attempts are locked per card.
    return True


def view(request, artifacts, editable, *, context=None):
    out = Path(request['output_dir'])
    context = context or read_context(request)
    if 'planned' not in context:
        context['planned'] = film.plan(request['film']['shots'])
    planned = context['planned']
    urls = {a['path']: a['url'] for a in artifacts if a['kind'] == 'video'}
    cards = []
    for segment in planned:
        number = segment['idx'] + 1
        record = context['records'].get(str(number - 1), {})
        path = visible_file(out, f'clips/seg{number}.mp4', context)
        version = file_version(path, out, context) if path else None
        pair = prompt_pair(record, out, number, context)
        pending, staged = attempt_record(request, context, number)
        attempted = staged or record
        unknown = bool(attempted.get('submission_unknown') and not attempted.get('task'))
        task_pending = task_unresolved(attempted)
        error = pending.get('error') or attempted.get('error')
        terminal = bool(attempted.get('terminal_failed'))
        automatic = automatic_inputs(request, number, context)
        fmt = pending.get('prompt_format') or ('source' if pair['source'] else 'h3')
        prompt = pending.get('prompt') if pending else (pair['source'] if fmt == 'source' else pair['h3'])
        recoverable = bool(prompt and (version or error or terminal))
        publishing = bool(pending.get('publishing') or pending.get('generated'))
        can_generate = bool(editable and recoverable and not task_pending and not unknown and not publishing)
        references = []
        for i, (picture, source) in enumerate(zip(automatic['pictures'], automatic['paths']), 1):
            reference = {'number': i, 'kind': picture['kind'], 'available': source.is_file()}
            if picture['kind'] == 'cast':
                reference['name'] = picture['cast']['name']
            if picture['kind'] == 'frame':
                reference['shot'] = request['film']['shots'].index(segment['shots'][picture['shot']]) + 1
            references.append(reference)
        override = record.get('prompt_override') or {}
        cards.append({'number': number, 'start': segment['start'], 'end': segment['end'], 'seconds': segment['seconds'],
                      'shots': len(segment['shots']), 'url': urls.get(str(out / f'clips/seg{number}.mp4')) if version else None,
                      'version': version, 'can_regenerate': can_generate, 'editable': bool(editable and recoverable and not task_pending and not unknown and not publishing),
                      'chained_next': request['frames'] != 'shot' and number < len(planned),
                      'prompt': prompt or '', 'prompt_format': fmt, 'prompt_version': segment_prompt_version({**record, 'zh': pair['source'], 'h3': pair['h3']}, version, pending, reference_version=automatic['reference_version']),
                      'source_prompt': pair['source'] if version else '', 'h3_prompt': pair['h3'] if version else '',
                      'source_rewrite_required': source_rewrite_required(record, pair, automatic['reference_version']),
                      'automatic_source_prompt': automatic['source'], 'source_changed': bool(override.get('source_version') and override['source_version'] != automatic['source_version']),
                      'error': error, 'attempt_error': pending.get('attempt_error'), 'task_pending': task_pending,
                      'submission_unknown': unknown, 'terminal_failed': terminal, 'attempt_revision': pending.get('revision') or record.get('revision'),
                      'applied_revision': record.get('revision'), 'publishing': publishing, 'reference_images': references,
                      'reference_history_known': 'reference_images' in record, 'references_for_regeneration': True,
                      'board_omitted': bool(request['film'].get('board') and not any(p['kind'] == 'board' for p in automatic['pictures'])),
                      'omitted_frames': max(0, len(segment['shots']) - sum(p['kind'] == 'frame' for p in automatic['pictures'])) if request['frames'] == 'shot' else 0})
    return cards


def selected_segment(request, number):
    planned = film.plan(request["film"]["shots"])
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(planned):
        raise InputError("segment_number", number=number, count=len(planned))
    return planned[number - 1]


def preflight(request, env):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise InputError('segment_ffmpeg')
    if request['operation'] == 'assemble':
        require_committed_publication(request)
        return
    segment = selected_segment(request, request.get('segment'))
    if request['demo']:
        return
    out = Path(request['output_dir'])
    pending = read_json(out / 'inputs' / 'segment-edit.json', {})
    if pending.get('revision') != request.get('segment_revision'):
        pending = request.get('_segment_preparation') or {}
    if pending.get('applied') or pending.get('generated'):
        return
    staged = read_json(out / 'inputs' / f"segment-{pending.get('revision', '')}" / 'state.json', {}).get('segs', {}).get(str(segment['idx']), {})
    if staged.get('terminal_failed'):
        raise InputError('segment_terminal', number=request['segment'])
    if staged.get('submission_unknown') and not staged.get('task'):
        raise InputError('segment_locked', number=request['segment'])
    if not env.get('RUNNINGHUB_API_KEY', '').strip():
        raise InputError('keys', names='RUNNINGHUB_API_KEY')
    if staged.get('task'):
        return
    if pending.get('prompt_format') == 'source' and not pending.get('reuse_h3') and not staged.get('h3'):
        if not (env.get('LLM_API_KEY', '').strip() or env.get('APIMART_API_KEY', '').strip()):
            raise InputError('keys', names='LLM_API_KEY / APIMART_API_KEY')
    from types import SimpleNamespace
    pics = film.refs(request['film'], segment, request['frames'])
    for path in film._picture_paths(SimpleNamespace(dir=out), segment, pics):
        if not path.is_file():
            raise InputError('missing_image', path=str(path))


def regeneration_plan(request, number, version, *, prompt=None, prompt_format=None, prompt_version=None):
    selected_segment(request, number)
    out = Path(request['output_dir'])
    context = read_context(request)
    previous = context['records'].get(str(number - 1), {})
    other = context['pending']
    if other and not other.get('applied') and other.get('number') != number:
        _, other_record = attempt_record(request, context, other['number'])
        if other.get('publishing') or other.get('generated') or task_unresolved(other_record) or other_record.get('submission_unknown'):
            raise InputError('segment_locked', number=other['number'])
    pending, staged = attempt_record(request, context, number)
    attempted = staged or previous
    if (attempted.get('submission_unknown') and not attempted.get('task')) or task_unresolved(attempted):
        raise InputError('segment_locked', number=number)
    if pending.get('generated') or pending.get('publishing'):
        raise InputError('segment_publishing', number=number)
    path = visible_file(out, f'clips/seg{number}.mp4', context)
    current = clip_version(path, out) if path else None
    pair = prompt_pair(previous, out, number, context)
    # Persist the actual pair even when it was recovered from legacy prompt files.
    previous = {**previous, 'zh': pair['source'], 'h3': pair['h3']}
    if current != version:
        raise InputError('segment_changed', number=number)
    automatic = automatic_inputs(request, number, context)
    edited = any(value is not None for value in (prompt, prompt_format, prompt_version))
    if edited:
        for field, value, valid, expected in (
                ('prompt', prompt, isinstance(prompt, str) and bool(prompt.strip()), 'nonempty string'),
                ('prompt_format', prompt_format, prompt_format in ('source', 'h3'), 'source|h3'),
                ('prompt_version', prompt_version, isinstance(prompt_version, str) and bool(prompt_version), 'current prompt version')):
            if not valid:
                raise InputError('segment_value', field=field, value=json.dumps(value, ensure_ascii=False)[:100], expected=message("segment_expected_" + field, request["lang"]))
        if prompt_version != segment_prompt_version(previous, current, pending, reference_version=automatic['reference_version']):
            raise InputError('segment_prompt_changed', field='prompt_version', number=number)
        if not current and not (attempted.get('terminal_failed') or attempted.get('error') or pending.get('error')):
            raise InputError('segment_prompt', number=number)
    else:
        if pending or not current or (not request['demo'] and not pair['h3']):
            raise InputError('segment_prompt', number=number)
        prompt_format = 'source' if pair['source'] else 'h3'
        prompt = pair['source'] if pair['source'] else pair['h3']
    if prompt_format == 'h3' and edited:
        issues = film.rewrite.problems(prompt, len(automatic['pictures']))
        if issues:
            raise InputError('segment_h3_invalid', field='prompt', detail='; '.join(issues)[:250])
    reuse = bool(prompt_format == 'source' and previous.get('h3')
                 and (not edited or (prompt == pair['source']
                 and not source_rewrite_required(previous, pair, automatic['reference_version']))))
    return {'number': number, 'revision': uuid.uuid4().hex, 'version': version, 'prompt_version': prompt_version,
            'prompt': prompt, 'prompt_format': prompt_format, 'previous': previous, 'applied': False,
            'source_version': automatic['source_version'], 'reference_version': automatic['reference_version'],
            'reuse_h3': reuse, 'explicit_prompt': edited}


def prepare_regeneration(request, number, version, *, prompt=None, prompt_format=None, prompt_version=None):
    preparation = regeneration_plan(request, number, version, prompt=prompt, prompt_format=prompt_format, prompt_version=prompt_version)
    out = Path(request['output_dir'])
    if not (out / 'inputs' / 'assembly.json').exists() and not assembly_status(request)['pending']:
        record_assembly(request)
    write_json(out / 'inputs' / 'segment-edit.json', preparation)
    return preparation['revision']


def prepare_prompt(staged, request, pending, segment):
    number, index = pending['number'], str(segment['idx'])
    prompt_format = pending.get('prompt_format')
    previous = pending['previous']
    legacy = pending.get('explicit_prompt') is False
    rec = staged.state['segs'].get(index, {})
    if not rec:
        if not prompt_format or legacy:  # Existing version-only requests reuse their saved H3.

            rec = {key: previous[key] for key in ('zh', 'h3', 'pics', 'prompt_override', 'h3_source', 'h3_reference_version') if key in previous}
        elif prompt_format == 'h3':
            rec = {'zh': '', 'h3': pending['prompt'], 'h3_source': None}
        else:
            rec = {'zh': pending['prompt']}
            if pending.get('reuse_h3'):
                rec.update(h3=previous['h3'], h3_source=pending['prompt'])
        staged.put('segs', index, **rec)
    if prompt_format == 'source' and not rec.get('h3') and not legacy:
        pictures = film.refs(request['film'], segment, request['frames'])
        if request['demo']:
            h3 = ''
        else:
            staged.log('segment rewrite', pending['revision'], 'format', prompt_format, 'cache miss')
            h3 = film.rewrite.rewrite(pending['prompt'], len(pictures), segment['seconds'], request['film']['aspect'])
        staged.put('segs', index, h3=h3, h3_source=pending['prompt'])
    if prompt_format and not legacy:
        if prompt_format == 'h3' or pending['prompt'] != previous.get('zh') or previous.get('prompt_override'):
            staged.put('segs', index, prompt_override={'format': prompt_format, 'text': pending['prompt'], 'source_version': pending['source_version']})
        staged.put('segs', index, h3_reference_version=pending['reference_version'])
    (staged.dir / 'prompts').mkdir(exist_ok=True)
    current = staged.state['segs'][index]
    for suffix, key in (('zh', 'zh'), ('h3', 'h3')):
        (staged.dir / 'prompts' / f'seg{number}.{suffix}.txt').write_text(current.get(key) or '', encoding='utf-8')
    staged.log('segment prompt', pending['revision'], 'format', prompt_format or 'legacy', 'cache', bool(rec.get('h3')))


def publish_file(source, target):
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temporary)
    temporary.replace(target)


def _regenerate(request):
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
    if not pending.get("generated"):
        pending.update(phase="rewrite", error=None)
        write_json(pending_path, pending)
        prepare_prompt(staged, request, pending, segment)
        pending["phase"] = "generation"
        write_json(pending_path, pending)
        if request["demo"]:
            from .demo import render_clip
            render_clip(candidate, segment["seconds"], request["film"]["aspect"], f"seg{number} / {pending['revision'][:6]}")
            media.last_frame(candidate, candidate_tail)
            staged.put("segs", index, file=str(candidate), cny=0, run_s=0)
        else:
            pics = film.refs(request["film"], segment, request["frames"])
            from types import SimpleNamespace
            paths = film._picture_paths(SimpleNamespace(dir=out), segment, pics)
            if not staged.state['segs'][index].get('task'):
                staged.put('segs', index, reference_images=[{'number': n, 'kind': picture['kind'], 'name': picture.get('cast', {}).get('name')} for n, picture in enumerate(pics, 1)])
            film.gen_segment(staged, request["film"], segment, pics, reference_paths=paths, guard_submission=True)
            film.stage_check(staged, request["film"], [segment])
        if not clip_version(candidate, staged.dir) or media.duration(candidate) <= 0 or not candidate_tail.is_file():
            raise InputError("segment_media", path=str(candidate))
        pending.update(generated=True, phase="publish")
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
    pending.update(applied=True, active=active.state["segs"][index])
    write_json(pending_path, pending)
    print(message("segment_done", request["lang"], number=number), flush=True)


def regenerate(request):
    try:
        _regenerate(request)
    except (Exception, SystemExit) as exc:
        out = Path(request['output_dir'])
        path = out / 'inputs' / 'segment-edit.json'
        pending = read_json(path, {})
        if pending.get('revision') == request.get('segment_revision') and not pending.get('applied'):
            staged = read_json(out / 'inputs' / f"segment-{pending['revision']}" / 'state.json', {}).get('segs', {}).get(str(pending['number'] - 1), {})
            unknown = bool(staged.get('submission_unknown') and not staged.get('task'))
            action = 'manual_check' if unknown else 'new_revision' if staged.get('terminal_failed') else 'resume' if staged.get('task') or pending.get('generated') else 'edit_prompt'
            phase = staged.get('phase') if pending.get('phase') == 'generation' else pending.get('phase', 'prepare')
            pending['error'] = message('segment_attempt_failed', request['lang'], phase=phase, number=pending['number'], revision=pending['revision'])
            pending['attempt_error'] = {'code': 'SEGMENT_ATTEMPT_FAILED', 'phase': phase, 'revision': pending['revision'],
                                        'prompt_format': pending.get('prompt_format'), 'task_present': bool(staged.get('task')),
                                        'side_effects': 'unknown' if unknown else 'partial' if staged.get('task') or staged.get('h3') else 'none',
                                        'recovery': {'action': action, 'next_step': message('segment_recovery_' + action, request['lang']), 'retry_bound': 0 if action in ('manual_check', 'new_revision') else 1, 'attempts_used': 1}}
            write_json(path, pending)
        raise


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
