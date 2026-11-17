"""Pure v2/v3 staging validation and text rendering; expected ends are plans, never images."""
import copy
import json
import re
from pathlib import Path

from .i18n import message

CAMERA_FIELDS = ('position', 'view_direction', 'framing', 'axis_side')
CHARACTER_FIELDS = ('name', 'appearance', 'position', 'body_facing', 'gaze', 'pose', 'expression', 'screen_position', 'visibility')
V3_CHARACTER_FIELDS = ('name', 'locked_look', 'placement_and_pose')
PROP_FIELDS = ('name', 'holder', 'contact', 'position', 'orientation', 'state', 'opening_direction', 'base_direction', 'knot_position', 'screen_position', 'visibility')
PARTS = ('opening_direction', 'base_direction', 'knot_position')
VISIBILITY = ('visible', 'cropped', 'occluded', 'offscreen')

_V3_LOC_PREFIX = (
    r'(?:在|于)?(?:画面|镜头|前景|中景|背景|后景|远景|近景)?'
    r'(?:正中|中央|中间|左侧|右侧|左边|右边|左方|右方|左前|右前|左后|右后|前方|后方|深处|一侧|角落)'
    r'(?:偏左|偏右|偏前|偏后|处)?'
)
_V3_PLACEMENT_VERB = re.compile(
    r'^(' + _V3_LOC_PREFIX + r'\s*(?:(?:正|半|侧身|微微)?(?:站着|坐着|跪着|蹲着|躺着|趴着|靠着|倚着|立着))?)'
    r'\s*(?:[，,]\s*(.*)|$)'
)
_V3_LOCATION_ONLY = re.compile(r'^(' + _V3_LOC_PREFIX + r')\s*(.+)$')


def is_structured(composition: object) -> bool:
    return isinstance(composition, dict) and composition.get('version') in (2, 3)


def deduplicate_paths(paths) -> list[str]:
    seen = set()
    out = []
    for item in paths:
        raw = str(item)
        key = str(Path(raw)) if raw else ''
        if key not in seen:
            seen.add(key)
            out.append(raw)
    return out


def group_cast_by_picture(active_cast: list[dict], all_cast: list[dict] | None = None) -> list[dict]:
    """Group active characters by shared picture path in first-appearance order, tracking absent sheet-mates."""
    full_cast = list(active_cast if all_cast is None else all_cast)

    def key_of(person: dict, fallback_idx: int) -> str:
        pic = person.get('picture')
        return str(Path(str(pic))) if pic else f'__unpictured_{fallback_idx}_{person.get("name", "")}'

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
            'active_name': ' / '.join(c['name'] for c in active_members if c.get('name')),
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


def _render_v3_character(character: dict) -> str:
    name = character['name'].strip()
    look = character['locked_look'].strip().rstrip('。；;，, ')
    pose = character['placement_and_pose'].strip().rstrip('。；; ')
    tagged = f'{name}（{look}）'
    if name in pose:
        sentence = re.sub(re.escape(name) + r'\s*(?:（[^）]*）|\([^)]*\))?', lambda _: tagged, pose, count=1)
    else:
        matched = _V3_PLACEMENT_VERB.match(pose)
        if matched:
            head = matched.group(1).strip()
            rest = (matched.group(2) or '').strip()
            sentence = f'{head}{tagged}，{rest}' if rest else f'{head}{tagged}'
        else:
            loc_matched = _V3_LOCATION_ONLY.match(pose)
            if loc_matched:
                sentence = f'{loc_matched.group(1).strip()}{tagged}{loc_matched.group(2).strip()}'
            else:
                sentence = f'{tagged}{pose}'
    return sentence.rstrip('。；; ') + '。'


def render_scene_and_characters(composition: dict) -> str:
    scene_body = re.sub(r'^场景(?:为|是|：|:)\s*', '', composition['scene'].strip()).rstrip('。；; ')
    char_line = ''.join(_render_v3_character(c) for c in composition.get('characters') or [])
    return f'场景为{scene_body}。' + char_line


def render_opening(composition: dict) -> str:
    c = composition
    if c.get('version') == 3:
        cam_body = re.sub(r'^机位(?:与构图)?(?:：|:)\s*', '', c['camera'].strip()).rstrip('。；; ')
        scene_body = re.sub(r'^场景(?:为|是|：|:)\s*', '', c['scene'].strip()).rstrip('。；; ')
        lines = [f'机位与构图：{cam_body}。', f'场景为{scene_body}。']
        char_line = ''.join(_render_v3_character(ch) for ch in c.get('characters') or [])
        if char_line:
            lines.append(char_line)
        return '\n'.join(lines)
    return '\n'.join((f"{message('staging_scene')}: {c['scene']}", f"{message('staging_frame')}: {c['frame']}",
                      f"{message('staging_world')}: {c['world_frame']}",
                      f"{message('staging_fixed')}: " + _states(c['fixed_objects'], ('name', 'position')),
                      f"{message('staging_camera')}: " + '; '.join(f"{message('staging_' + k)}: {c['camera'][k]}" for k in CAMERA_FIELDS),
                      f"{message('staging_characters')}: " + _states(c['characters'], CHARACTER_FIELDS),
                      f"{message('staging_props')}: " + _states(c['props'], PROP_FIELDS)))


def render_motion(composition: dict, action: str = '') -> str:
    if composition.get('version') == 3:
        cam_body = composition['camera'].strip().strip('【】').rstrip('。；; ')
        act = action.strip().rstrip('。；; ')
        return f'【{cam_body}】{render_scene_and_characters(composition)}' + (f'动作过程：{act}' if act else '')
    end = composition['expected_end']
    return '\n'.join((render_opening(composition), f"{message('staging_action')}: {composition['action']}",
                      message('staging_expected_end') + ':\n' + _states(end['characters'], CHARACTER_FIELDS)
                      + '\n' + _states(end['props'], PROP_FIELDS)))
