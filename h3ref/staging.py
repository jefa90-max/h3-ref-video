"""Pure v2/v3 staging validation and text rendering; expected ends are plans, never images."""
import copy
import json
import os
import re

from .i18n import message

CAMERA_FIELDS = ('position', 'view_direction', 'framing', 'axis_side')
CHARACTER_FIELDS = ('name', 'appearance', 'position', 'body_facing', 'gaze', 'pose', 'expression', 'screen_position', 'visibility')
PROP_FIELDS = ('name', 'holder', 'contact', 'position', 'orientation', 'state', 'opening_direction', 'base_direction', 'knot_position', 'screen_position', 'visibility')
PARTS = ('opening_direction', 'base_direction', 'knot_position')
VISIBILITY = ('visible', 'cropped', 'occluded', 'offscreen')

_WEARABLE_NAMES = r'眼镜|墨镜|毛巾|外套|夹克|大衣|围巾|口罩|头盔|帽子'
_WEARABLE_REMOVAL = re.compile(r'(?:摘下|脱下|扯下|取下|解下|拿下|摘掉|脱掉)[^，,。！？；;]{0,24}?('
                               + _WEARABLE_NAMES + r')(?!框|片|盒|架)')
_WEARABLE_AT_OPENING = re.compile(r'(?:戴着|佩戴|穿着|身穿|穿(?!过|越|行|梭)|围着|挂着|系着)[^，,。！？；;]{0,20}?('
                                 + _WEARABLE_NAMES + r')(?!框|片|盒|架)')
_WEARABLE_ON_BODY = re.compile(r'(' + _WEARABLE_NAMES + r')\s*(?:挂|架|戴|系|搭|罩|裹|套|别|扣|遮|压)(?:着|在|于|住)')
_LOOK_WEAR_VERB = re.compile(r'(?:(?:头|脸|肩|颈|脖子|脖颈|鼻梁|身|腰|背|胸|耳)(?:上|前|间))?'
                            r'(?:戴着?|佩戴|外穿|身穿|穿着|穿(?!过|越|行|梭)|围着|挂着|系着)')
_LOOK_WEARABLE = re.compile(r'(' + _WEARABLE_NAMES + r')(?!框|片|盒|架)')
_LOOK_CONNECTOR = re.compile(r'(?:并且|同时|而且|还|也|又|并|和|与)\s*$')
_NEGATED_STATE = ('未', '没', '没有', '不', '别', '不要')
_COMPLETED_STATE = ('已', '已经', '早已', '刚', '刚刚', '先前')
_SUBJECT_PRONOUN = re.compile(r'^\s*(?:[^，,]{1,16}[，,]\s*)?([他她])(?!们|人)')
_ACTION_CONNECTOR = re.compile(r'^(?:随后|然后|接着|此时|这时)\s*')

_V3_LOC_PREFIX = (
    r'(?:在|于)?(?:画面|镜头|前景|中景|背景|后景|远景|近景)?'
    r'(?:正中|中央|中间|左侧|右侧|左边|右边|左方|右方|左前|右前|左后|右后|前方|后方|深处|一侧|角落)'
    r'(?:偏左方|偏右方|偏前方|偏后方|偏左|偏右|偏前|偏后|处)?'
)
_V3_PLACEMENT_VERB = re.compile(
    r'^(' + _V3_LOC_PREFIX + r'(?:\s*(?:正|半|侧身|微微)?(?:站着|坐着|跪着|蹲着|躺着|趴着|靠着|倚着|立着))?)'
    r'\s*(?:[，,]\s*(.*)|$)'
)


def is_structured(composition: object) -> bool:
    return isinstance(composition, dict) and composition.get('version') in (2, 3)


def _actor_sentences(film: dict, action: str):
    names = {alias: actor['name'] for actor in film.get('cast') or []
             for alias in (actor['name'], *(actor.get('aliases') or [])) if alias}
    previous_actor = None
    for sentence in re.split(r'[。！？；;\n]', action):
        if not sentence.strip():
            continue
        pronoun = _SUBJECT_PRONOUN.match(sentence)
        if pronoun:
            # Names after a leading pronoun can be objects of its action.
            # Only a new clause with a named subject can replace that actor.
            actor = previous_actor
            prefix = sentence[:pronoun.start(1)].strip().rstrip('，,')
            named_prefix = {name for alias, name in names.items() if alias in prefix}
            if named_prefix:
                actor = named_prefix.pop() if len(named_prefix) == 1 else None
                if actor:
                    yield actor, prefix
            for clause in re.split(r'[，,]', sentence[pronoun.start(1):]):
                clause = clause.strip()
                beginning = _ACTION_CONNECTOR.sub('', clause)
                if not _SUBJECT_PRONOUN.match(beginning) and any(beginning.startswith(alias) for alias in names):
                    named = {name for alias, name in names.items() if alias in clause}
                    actor = named.pop() if len(named) == 1 else None
                if actor:
                    yield actor, clause
            previous_actor = actor
            continue
        actors = {name for alias, name in names.items() if alias in sentence}
        previous_actor = actors.pop() if len(actors) == 1 else None
        if previous_actor:
            yield previous_actor, sentence


def changing_wearables(film: dict) -> dict[str, set[str]]:
    """Recognize explicit removals in action sentences with one unambiguous cast member."""
    changes: dict[str, set[str]] = {}
    for shot in film.get('shots') or []:
        for name, sentence in _actor_sentences(film, shot.get('action') or ''):
            removed = {match[1] for match in _WEARABLE_REMOVAL.finditer(sentence)
                       if not sentence[:match.start()].endswith(_NEGATED_STATE)}
            if removed:
                changes.setdefault(name, set()).update(removed)
    return changes


def _split_locked_look(look: str, changing: set[str]) -> tuple[str, list[tuple[str, str]]]:
    if not changing:
        return look, []
    fixed, mutable = [], []
    for clause in re.split(r'[，,。；;、\n]+', look):
        clause = clause.strip()
        if not clause:
            continue
        verbs = list(_LOOK_WEAR_VERB.finditer(clause))
        if verbs:
            prefix = _LOOK_CONNECTOR.sub('', clause[:verbs[0].start()]).strip()
            fixed_parts, mutable_parts = ([prefix] if prefix else []), []
            for index, verb in enumerate(verbs):
                end = verbs[index + 1].start() if index + 1 < len(verbs) else len(clause)
                specifier = clause[verb.start():end]
                if end < len(clause):
                    specifier = _LOOK_CONNECTOR.sub('', specifier).strip()
                wearable = _LOOK_WEARABLE.search(clause, verb.end(), end)
                if wearable and wearable[1] in changing:
                    mutable_parts.append((wearable[1], specifier))
                else:
                    fixed_parts.append(specifier)
            if mutable_parts:
                fixed.extend(fixed_parts)
                mutable.extend(mutable_parts)
                continue
        wearable = next((match[1] for match in _WEARABLE_ON_BODY.finditer(clause) if match[1] in changing), None)
        if wearable is None:
            wearable = next((item for item in _WEARABLE_NAMES.split('|') if item in changing and clause.endswith(item)), None)
        if wearable:
            mutable.append((wearable, clause))
        else:
            fixed.append(clause)
    if not mutable:
        return look, mutable
    return '，'.join(fixed) or '保持角色身份外观', mutable


def stable_locked_look(look: str, changing: set[str]) -> str:
    return _split_locked_look(look, changing)[0] if changing else look


def scope_wearable_look(composition: dict | None, film: dict, shot: dict, changes: dict[str, set[str]]) -> dict | None:
    """Keep explicitly changing wearables local to the corresponding opening pose."""
    if not changes or not composition or composition.get('version') != 3:
        return composition
    worn: dict[str, set[str]] = {}
    for name, sentence in _actor_sentences(film, shot.get('action') or ''):
        items = worn.setdefault(name, set())
        items.update(match[1] for match in _WEARABLE_AT_OPENING.finditer(sentence)
                     if not sentence[:match.start()].endswith(_NEGATED_STATE))
        for match in _WEARABLE_REMOVAL.finditer(sentence):
            if sentence[:match.start()].endswith(_COMPLETED_STATE) or sentence[match.end():].startswith(('后', '之后', '以后')):
                items.discard(match[1])
            elif not sentence[:match.start()].endswith(_NEGATED_STATE):
                items.add(match[1])
    characters = []
    for character in composition['characters']:
        look, mutable = _split_locked_look(character['locked_look'], changes.get(character['name'], set()))
        pose = character['placement_and_pose']
        wearing = [clause for item, clause in mutable if item in worn.get(character['name'], ()) and item not in pose]
        if mutable:
            character = {**character, 'locked_look': look, 'placement_and_pose': '，'.join([*wearing, pose])}
        characters.append(character)
    return {**composition, 'characters': characters}


def canonical_path_key(raw: str, resolve_cache: dict[str, str] | None = None) -> str:
    if not raw:
        return ''
    if resolve_cache is not None and raw in resolve_cache:
        return resolve_cache[raw]
    resolved = os.path.normcase(os.path.realpath(os.path.expanduser(raw)))
    if resolve_cache is not None:
        resolve_cache[raw] = resolved
    return resolved


def deduplicate_paths(paths, *, resolve_cache: dict[str, str] | None = None) -> list[str]:
    cache = {} if resolve_cache is None else resolve_cache
    seen = set()
    out = []
    for item in paths:
        raw = str(item)
        key = canonical_path_key(raw, cache)
        if key not in seen:
            seen.add(key)
            out.append(raw)
    return out


def group_cast_by_picture(active_cast: list[dict], all_cast: list[dict] | None = None, *,
                          resolve_cache: dict[str, str] | None = None) -> list[dict]:
    """Group active characters by shared picture path in first-appearance order, tracking absent sheet-mates."""
    cache = {} if resolve_cache is None else resolve_cache
    full_cast = list(active_cast if all_cast is None else all_cast)

    def key_of(person: dict, fallback_idx: int) -> str:
        pic = person.get('picture')
        return canonical_path_key(str(pic), cache) if pic else f'__unpictured_{fallback_idx}_{person.get("name", "")}'

    ordered_keys = []
    active_by_key: dict[str, list[dict]] = {}
    active_seen_names: dict[str, set[str]] = {}
    for idx, person in enumerate(active_cast):
        key = key_of(person, idx)
        if key not in active_by_key:
            ordered_keys.append(key)
            active_by_key[key] = []
            active_seen_names[key] = set()
        name = person.get('name') or ''
        if name not in active_seen_names[key]:
            active_seen_names[key].add(name)
            active_by_key[key].append(person)

    all_by_key: dict[str, list[dict]] = {}
    all_seen_names: dict[str, set[str]] = {}
    for idx, person in enumerate(full_cast):
        key = key_of(person, idx)
        all_by_key.setdefault(key, [])
        all_seen_names.setdefault(key, set())
        name = person.get('name') or ''
        if name not in all_seen_names[key]:
            all_seen_names[key].add(name)
            all_by_key[key].append(person)

    groups = []
    for key in ordered_keys:
        active_members = active_by_key[key]
        all_members = all_by_key.get(key) or list(active_members)
        active_names = {c.get('name') for c in active_members}
        absent_members = [c for c in all_members if c.get('name') not in active_names]
        groups.append({
            'path': str(active_members[0].get('picture') or ''),
            'active': active_members,
            'absent': absent_members,
            'all': all_members,
            'name': ' / '.join(c['name'] for c in all_members if c.get('name')),
        })
    return groups


def validate_composition(value: object, film: dict, shot: dict, known_scenes: dict | None = None) -> dict:
    def invalid(path, actual, expected):
        excerpt = json.dumps(actual, ensure_ascii=False)[:160]
        raise ValueError(message('staging_invalid', path=path, value=excerpt, expected=expected))

    def obj(item, path):
        if not isinstance(item, dict):
            invalid(path, item, 'object')

    def text(item, path):
        if not isinstance(item, str) or not item.strip():
            invalid(path, item, 'nonempty string')
        return item.strip()

    def array(item, path):
        if not isinstance(item, list):
            invalid(path, item, 'array')

    obj(value, 'composition')
    result = copy.deepcopy(value)
    version = result.get('version')
    if type(version) is not int or version not in (2, 3):
        invalid('version', version, '2 or 3')
    aliases = {alias: c['name'] for c in film.get('cast') or [] for alias in [c['name'], *(c.get('aliases') or [])]}
    if version == 3:
        camera = text(result.get('camera'), 'camera')
        scene = text(result.get('scene'), 'scene')
        items = result.get('characters')
        array(items, 'characters')
        names = set()
        characters = []
        for i, item in enumerate(items):
            prefix = f'characters[{i}]'
            obj(item, prefix)
            raw_name = text(item.get('name'), f'{prefix}.name')
            name = aliases.get(raw_name, raw_name)
            if name in names:
                invalid(f'{prefix}.name', name, 'unique name')
            names.add(name)
            characters.append({
                'name': name,
                'locked_look': text(item.get('locked_look'), f'{prefix}.locked_look'),
                'placement_and_pose': text(item.get('placement_and_pose'), f'{prefix}.placement_and_pose'),
            })
        return {'version': 3, 'camera': camera, 'scene': scene, 'characters': characters}

    for field in ('scene', 'frame', 'orientation', 'scene_id', 'world_frame', 'action'):
        result[field] = text(result.get(field), field)

    def states(items, path, fields):
        array(items, path)
        names = set()
        for i, item in enumerate(items):
            prefix = f'{path}[{i}]'
            obj(item, prefix)
            for field in fields:
                if field in PARTS and field in item and item[field] is None:
                    continue
                item[field] = text(item.get(field), f'{prefix}.{field}')
            if fields == CHARACTER_FIELDS:
                item['name'] = aliases.get(item['name'], item['name'])
            if fields == PROP_FIELDS:
                item['holder'] = aliases.get(item['holder'], item['holder'])
            if 'visibility' in fields and item['visibility'] not in VISIBILITY:
                invalid(f'{prefix}.visibility', item['visibility'], '/'.join(VISIBILITY))
            if item['name'] in names:
                invalid(f'{prefix}.name', item['name'], 'unique name')
            names.add(item['name'])

    states(result.get('fixed_objects'), 'fixed_objects', ('name', 'position'))
    obj(result.get('camera'), 'camera')
    for field in CAMERA_FIELDS:
        result['camera'][field] = text(result['camera'].get(field), f'camera.{field}')
    states(result.get('characters'), 'characters', CHARACTER_FIELDS)
    states(result.get('props'), 'props', PROP_FIELDS)
    obj(result.get('expected_end'), 'expected_end')
    for field, fields in (('characters', CHARACTER_FIELDS), ('props', PROP_FIELDS)):
        states(result['expected_end'].get(field), f'expected_end.{field}', fields)
    known = (known_scenes or {}).get(result['scene_id'])
    if known:
        for field in ('world_frame', 'fixed_objects'):
            if result[field] != known[field]:
                invalid(field, result[field], message('staging_known_layout', scene_id=result['scene_id']))
    return result


def _states(items, fields):
    return '\n'.join('; '.join(f"{message('staging_' + field)}: {item[field]}" for field in fields if item.get(field) is not None)
                     for item in items)


def _render_v3_character(character: dict, other_names: set[str] | None = None) -> str:
    name = character['name'].strip()
    look = character['locked_look'].strip().rstrip('。；;，, ')
    pose = character['placement_and_pose'].strip().rstrip('。；; ')
    tagged = f'{name}（{look}）'
    subject_re = re.compile(
        r'^((?:(?:(?:站在|坐在|跪在|位于|立于|处于)\s*)?' + _V3_LOC_PREFIX
        + r'(?:\s*(?:(?:正|半|侧身|微微)?(?:站着|坐着|跪着|蹲着|躺着|趴着|靠着|倚着|立着))\s*|\s*[，,]\s*)?)?'
        + r'(?:[^，,。；;（）()]*的)?)'
        + re.escape(name)
        + r'(\s*(?:（[^）]*）|\([^)]*\))?)'
    )
    subj_matched = subject_re.match(pose)
    from_name = pose[len(subj_matched.group(1)):] if subj_matched else ''
    longer_cast_match = bool(other_names and any(len(other) > len(name) and from_name.startswith(other) for other in other_names))
    if subj_matched and not longer_cast_match:
        note = (subj_matched.group(2) or '').strip()
        kept_note = note if (note and look not in note) else ''
        sentence = f"{subj_matched.group(1)}{tagged}{kept_note}{pose[subj_matched.end():]}"
    else:
        matched = _V3_PLACEMENT_VERB.match(pose)
        if matched:
            head = matched.group(1).strip()
            rest = (matched.group(2) or '').strip()
            sentence = f'{head}{tagged}，{rest}' if rest else f'{head}{tagged}'
        else:
            sentence = f'{tagged}{pose}'
    return sentence.rstrip('。；; ') + '。'


def render_opening(composition: dict) -> str:
    c = composition
    if c.get('version') == 3:
        cam_body = re.sub(r'^机位(?:与构图)?(?:：|:)\s*', '', c['camera'].strip()).rstrip('。；; ')
        scene_body = re.sub(r'^场景(?:为|是|：|:)\s*', '', c['scene'].strip()).rstrip('。；; ')
        lines = [f'机位与构图：{cam_body}。', f'场景为{scene_body}。']
        chars = c.get('characters') or []
        all_names = {ch['name'].strip() for ch in chars if ch.get('name')}
        char_line = ''.join(_render_v3_character(ch, all_names) for ch in chars)
        if char_line:
            lines.append(char_line)
        return '\n'.join(lines)
    return '\n'.join((f"{message('staging_scene')}: {c['scene']}", f"{message('staging_frame')}: {c['frame']}",
                      f"{message('staging_world')}: {c['world_frame']}",
                      f"{message('staging_fixed')}: " + _states(c['fixed_objects'], ('name', 'position')),
                      f"{message('staging_camera')}: " + '; '.join(f"{message('staging_' + k)}: {c['camera'][k]}" for k in CAMERA_FIELDS),
                      f"{message('staging_characters')}: " + _states(c['characters'], CHARACTER_FIELDS),
                      f"{message('staging_props')}: " + _states(c['props'], PROP_FIELDS)))


def render_motion(composition: dict) -> str:
    end = composition['expected_end']
    return '\n'.join((render_opening(composition), f"{message('staging_action')}: {composition['action']}",
                      message('staging_expected_end') + ':\n' + _states(end['characters'], CHARACTER_FIELDS)
                      + '\n' + _states(end['props'], PROP_FIELDS)))
