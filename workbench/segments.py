"""Review, regenerate one clip, and assemble saved clips without rerunning the film."""
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path

from h3ref import config, film, media
from h3ref.i18n import use_lang
from .i18n import InputError, message
from .jobs import write_json
from .storyboard import read_json


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
    if pending.get('applied'):
        publication_current = bool(pending.get('active') and current.get('revision') == pending.get('revision'))
    else:
        publication_current = (not pending.get('active')
                               or current.get('revision') == pending.get('revision')
                               or current == pending.get('previous'))
    if pending.get('publishing') and not pending.get('applied'):
        records[str(pending['number'] - 1)] = pending['previous']
    elif pending.get('applied') and pending.get('active') and publication_current:
        records[str(pending['number'] - 1)] = pending['active']
    return {'pending_stamp': pending_stamp, 'state': state, 'records': records, 'pending': pending, 'publication_current': publication_current, 'automatic': {}, 'path_versions': {}, 'file_versions': {}}


def artifact_context(out):
    out = Path(out)
    pending = read_json(out / 'inputs' / 'segment-edit.json', {})
    publication_current = True
    if pending.get('publishing'):
        state = read_json(out / 'state.json', {'frames': {}, 'segs': {}})
        current = state.get('segs', {}).get(str(pending.get('number', 0) - 1), {})
        if pending.get('applied'):
            publication_current = bool(pending.get('active') and current.get('revision') == pending.get('revision'))
        else:
            publication_current = (not pending.get('active')
                                   or current.get('revision') == pending.get('revision')
                                   or current == pending.get('previous'))
    return {'pending': pending, 'publication_current': publication_current, 'file_versions': {}}


def preview_reconciled_segments(out, records=None, *, keep_pending=False, supersede=False, drain_siblings=False):
    out = Path(out)
    pending_path = out / 'inputs' / 'segment-edit.json'
    pending = read_json(pending_path, {})
    base_records = dict(read_json(out / 'state.json', {'segs': {}}).get('segs', {}) if records is None else records)
    if not pending or pending.get('applied'):
        return base_records, False, False
    number = pending.get('number')
    if not isinstance(number, int) or number < 1:
        return base_records, False, not keep_pending
    index = str(number - 1)
    staged = read_json(out / 'inputs' / f"segment-{pending.get('revision', '')}" / 'state.json', {}).get('segs', {}).get(index, {})
    has_clip = (out / 'clips' / f'seg{number}.mp4').is_file()
    rec = dict(base_records.get(index, {}))
    changed = False
    should_unlink = not keep_pending

    def apply_prompt_fields():
        nonlocal changed
        prior_zh = rec.get('zh')
        for key in ('zh', 'h3', 'h3_source', 'h3_reference_version', 'prompt_override', 'pics', 'reference_images'):
            if key in staged:
                rec[key] = staged[key]
                changed = True
        if 'prompt_override' not in staged and pending.get('explicit_prompt') and pending.get('prompt_format') in ('source', 'h3'):
            fmt = pending['prompt_format']
            prompt_text = pending.get('prompt', '')
            if fmt == 'h3':
                rec.update(zh='', h3=prompt_text, h3_source=None)
            else:
                if not pending.get('reuse_h3') and prompt_text != prior_zh:
                    rec.update(h3=None, h3_source=None)
                rec['zh'] = prompt_text
            if fmt == 'h3' or prompt_text != (pending.get('previous') or {}).get('zh') or (pending.get('previous') or {}).get('prompt_override'):
                rec['prompt_override'] = {'format': fmt, 'text': prompt_text, 'source_version': pending.get('source_version')}
            changed = True

    if supersede:
        if rec.pop('unapplied_edit', None) is not None or rec.pop('attempt_blocked', None) is not None:
            changed = True
        if staged.get('prompt_override') or (pending.get('explicit_prompt') and pending.get('prompt_format') in ('source', 'h3')):
            apply_prompt_fields()
        should_unlink = True
    elif not has_clip:
        if rec.pop('unapplied_edit', None) is not None:
            changed = True
        apply_prompt_fields()
        if staged.get('terminal_failed'):
            rec.pop('attempt_blocked', None)
            rec.update(terminal_failed=True, task=staged.get('task'),
                       error=staged.get('error') or pending.get('error'), file=None, cny=None)
            changed = True
        elif not staged.get('task') and not staged.get('submission_unknown'):
            for stale_key in ('task', 'terminal_failed', 'submission_unknown', 'submitted',
                              'phase', 'seed', 'file', 'cny', 'coins', 'run_s', 'wall_s',
                              'error_code', 'attempt_blocked'):
                if rec.pop(stale_key, None) is not None:
                    changed = True
            err = staged.get('error') or pending.get('error')
            if err:
                rec['error'] = err
                if drain_siblings:
                    rec['attempt_blocked'] = True
                changed = True
            elif rec.pop('error', None) is not None:
                changed = True
        elif staged.get('error') or pending.get('error'):
            rec['error'] = staged.get('error') or pending.get('error')
            changed = True
        should_unlink = (not keep_pending) if staged.get('terminal_failed') else True
    elif not keep_pending:
        rec['unapplied_edit'] = {
            'revision': pending.get('revision'),
            'prompt': pending.get('prompt'),
            'prompt_format': pending.get('prompt_format'),
            'error': pending.get('error') or staged.get('error'),
            'attempt_error': pending.get('attempt_error'),
        }
        changed = True
        should_unlink = True
    if changed:
        base_records[index] = rec
    return base_records, changed, should_unlink


def reconcile_pending_segment(out, *, keep_pending=False, supersede=False, drain_siblings=False):
    out = Path(out)
    next_records, changed, should_unlink = preview_reconciled_segments(
        out, keep_pending=keep_pending, supersede=supersede, drain_siblings=drain_siblings,
    )
    if changed:
        job = film.Job(out)
        job.state['segs'] = next_records
        job.save()
    if should_unlink:
        (out / 'inputs' / 'segment-edit.json').unlink(missing_ok=True)


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
        if not token:
            return False
        if version is None:
            return True
        ver_str = str(version)
        return ver_str == token if ':' in ver_str else ver_str == token.split(':')[0]
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
    payload = [version, record.get('zh'), record.get('h3'), record.get('prompt_override'), record.get('unapplied_edit'), record.get('revision'), attempt, reference_version]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_rewrite_required(record, pair, reference_version):
    return not (pair['paired'] and (not record.get('h3_reference_version') or record['h3_reference_version'] == reference_version))


def automatic_inputs(request, number, context):
    if number not in context['automatic']:
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
        paths = film.picture_paths(Path(request['output_dir']), segment, pictures)
        with use_lang(request.get('lang')):
            source = film.compile_prompt(request['film'], segment, pictures, len(planned), compositions=context['compositions'])
        reference_version = film.reference_version(paths, metadata=context['path_versions'])
        source_version = hashlib.sha256(json.dumps([source, reference_version], ensure_ascii=False).encode()).hexdigest()
        context['automatic'][number] = {'source': source, 'source_version': source_version,
                                        'reference_version': reference_version, 'pictures': pictures, 'paths': paths}
    return context['automatic'][number]


def attempt_record(request, context, number):
    pending = context['pending']
    if pending.get('number') != number or pending.get('applied'):
        return {}, {}
    active = context['records'].get(str(number - 1), {})
    prev = pending.get('previous') or {}
    if active.get('revision') != prev.get('revision') and not pending.get('publishing'):
        return {}, {}
    if 'staged' not in context:
        path = Path(request['output_dir']) / 'inputs' / f"segment-{pending['revision']}" / 'state.json'
        context['staged'] = read_json(path, {}).get('segs', {}).get(str(number - 1), {})
    return pending, context['staged']


def task_unresolved(record):
    return bool(record.get('task') and not record.get('terminal_failed')
                and not (record.get('file') and record.get('cny') is not None))


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
        unapplied = (record.get('unapplied_edit') or {}) if not pending else {}
        attempted = staged if pending else record
        unknown = bool(attempted.get('submission_unknown') and not attempted.get('task'))
        task_pending = task_unresolved(attempted)
        error = pending.get('error') or attempted.get('error') or unapplied.get('error')
        terminal = bool(attempted.get('terminal_failed'))
        unsubmitted_pending = bool((pending and not pending.get('applied') and not attempted.get('task') and not unknown) or unapplied)
        automatic = automatic_inputs(request, number, context)
        fmt = pending.get('prompt_format') or unapplied.get('prompt_format') or ('source' if pair['source'] else 'h3')
        prompt = pending.get('prompt') if pending else (unapplied.get('prompt') if unapplied.get('prompt') is not None else (pair['source'] if fmt == 'source' else pair['h3']))
        recoverable = bool(prompt and (version or error or terminal or record.get('error') or record.get('terminal_failed') or unsubmitted_pending))
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
                      'error': error, 'attempt_error': pending.get('attempt_error') or unapplied.get('attempt_error'), 'task_pending': task_pending,
                      'submission_unknown': unknown, 'terminal_failed': terminal, 'attempt_revision': pending.get('revision') or unapplied.get('revision') or record.get('revision'),
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
    if '_segment_preparation' in request:
        pending = request['_segment_preparation'] or {}
    else:
        pending = read_json(out / 'inputs' / 'segment-edit.json', {})
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
        names = config.llm_settings(env)['key_names']
        if not any(env.get(name, '').strip() for name in names):
            raise InputError('keys', names=' / '.join(names))
    pics = film.refs(request['film'], segment, request['frames'])
    for path in film.picture_paths(out, segment, pics):
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
        if other.get('publishing') or other.get('generated') or task_unresolved(other_record) or (other_record.get('submission_unknown') and not other_record.get('task')):
            raise InputError('segment_locked', number=other['number'])
    pending, staged = attempt_record(request, context, number)
    attempted = staged if pending else previous
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
        for field, value, valid in (
                ('prompt', prompt, isinstance(prompt, str) and bool(prompt.strip())),
                ('prompt_format', prompt_format, prompt_format in ('source', 'h3')),
                ('prompt_version', prompt_version, isinstance(prompt_version, str) and bool(prompt_version))):
            if not valid:
                raise InputError('segment_value', field=field, value=json.dumps(value, ensure_ascii=False)[:100], expected=message("segment_expected_" + field, request["lang"]))
        if prompt_version != segment_prompt_version(previous, current, pending, reference_version=automatic['reference_version']):
            raise InputError('segment_prompt_changed', field='prompt_version', number=number)
        unsubmitted_pending = bool(pending and not pending.get('applied') and not attempted.get('task') and not attempted.get('submission_unknown'))
        if not current and not (attempted.get('terminal_failed') or previous.get('terminal_failed')
                                or attempted.get('error') or previous.get('error') or pending.get('error') or unsubmitted_pending):
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
    clean_previous = {k: v for k, v in previous.items() if k not in ('unapplied_edit', 'attempt_blocked')}
    reuse = bool(prompt_format == 'source' and clean_previous.get('h3')
                 and (not edited or (prompt == pair['source']
                 and not source_rewrite_required(clean_previous, pair, automatic['reference_version']))))
    return {'number': number, 'revision': uuid.uuid4().hex, 'version': version, 'prompt_version': prompt_version,
            'prompt': prompt, 'prompt_format': prompt_format, 'previous': clean_previous, 'applied': False,
            'source_version': automatic['source_version'], 'reference_version': automatic['reference_version'],
            'reuse_h3': reuse, 'explicit_prompt': edited}


def prepare_regeneration(request, number, version, *, prompt=None, prompt_format=None, prompt_version=None):
    preparation = regeneration_plan(request, number, version, prompt=prompt, prompt_format=prompt_format, prompt_version=prompt_version)
    out = Path(request['output_dir'])
    existing = read_json(out / 'inputs' / 'segment-edit.json', {})
    if existing and not existing.get('applied') and existing.get('number') != number:
        reconcile_pending_segment(out, keep_pending=False, supersede=False)
    elif preparation['previous'] != read_json(out / 'state.json', {'segs': {}}).get('segs', {}).get(str(number - 1), {}):
        job = film.Job(out)
        if str(number - 1) in job.state['segs']:
            job.state['segs'][str(number - 1)].pop('unapplied_edit', None)
            job.state['segs'][str(number - 1)].pop('attempt_blocked', None)
            job.save()
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
        staged.put('segs', index, h3_reference_version=pending['reference_version'],
                   pics=[p['kind'] for p in film.refs(request['film'], segment, request['frames'])])
    (staged.dir / 'prompts').mkdir(exist_ok=True)
    current = staged.state['segs'][index]
    for suffix, key in (('zh', 'zh'), ('h3', 'h3')):
        (staged.dir / 'prompts' / f'seg{number}.{suffix}.txt').write_text(current.get(key) or '', encoding='utf-8')
    staged.log('segment prompt', pending['revision'], 'format', prompt_format or 'legacy', 'cache', bool(rec.get('h3')))


def publish_file(source, target):
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copy2(source, temporary)
    config.atomic_replace(temporary, target)


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
            paths = film.picture_paths(out, segment, pics)
            if not staged.state['segs'][index].get('task'):
                staged.put('segs', index, reference_images=[{'number': n, 'kind': picture['kind'], 'name': picture.get('cast', {}).get('name')} for n, picture in enumerate(pics, 1)])
            film.gen_segment(staged, request["film"], segment, pics, reference_paths=paths)
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
            files[path.relative_to(out).as_posix()] = {'previous': str(backup) if path.is_file() else None, 'next': str(source)}
        write_json(staged.dir / 'previous.json', pending['previous'])
        pending.update(publishing=True, files=files)
        write_json(pending_path, pending)
    for source, path in group:
        path.parent.mkdir(exist_ok=True)
        publish_file(source, path)
    active = film.Job(out)
    active.state["segs"][index] = {**staged.state["segs"][index], "file": str(target), "revision": pending["revision"]}
    active.save()
    pending.update(applied=True, publishing=False, active=active.state["segs"][index])
    write_json(pending_path, pending)
    print(message("segment_done", request["lang"], number=number), flush=True)


def regenerate(request):
    try:
        _regenerate(request)
    except (Exception, SystemExit):
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
    if (pending.get('publishing') or pending.get('generated')) and not pending.get('applied'):
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
