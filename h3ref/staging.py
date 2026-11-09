"""Pure v2 staging validation and text rendering; expected ends are plans, never images."""
import copy
import json

from .i18n import message

CHARACTER_FIELDS = ('name', 'appearance', 'position', 'body_facing', 'gaze', 'pose', 'expression', 'screen_position', 'visibility')
PROP_FIELDS = ('name', 'holder', 'contact', 'position', 'orientation', 'state', 'opening_direction', 'base_direction', 'knot_position', 'screen_position', 'visibility')
PARTS = ('opening_direction', 'base_direction', 'knot_position')
VISIBILITY = ('visible', 'cropped', 'occluded', 'offscreen')


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
    if type(result.get('version')) is not int or result['version'] != 2:
        invalid('version', result.get('version'), '2')
    for field in ('scene', 'frame', 'orientation', 'scene_id', 'world_frame', 'action'):
        result[field] = text(result.get(field), field)
    aliases = {alias: c['name'] for c in film.get('cast', []) for alias in [c['name'], *(c.get('aliases') or [])]}

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
    for field in ('position', 'view_direction', 'framing', 'axis_side'):
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


def render_opening(composition: dict) -> str:
    c = composition
    return '\n'.join((f"{message('staging_scene')}: {c['scene']}", f"{message('staging_frame')}: {c['frame']}",
                      f"{message('staging_world')}: {c['world_frame']}",
                      f"{message('staging_fixed')}: " + _states(c['fixed_objects'], ('name', 'position')),
                      f"{message('staging_camera')}: " + '; '.join(f"{message('staging_' + k)}: {v}" for k, v in c['camera'].items()),
                      f"{message('staging_characters')}: " + _states(c['characters'], CHARACTER_FIELDS),
                      f"{message('staging_props')}: " + _states(c['props'], PROP_FIELDS)))


def render_motion(composition: dict) -> str:
    end = composition['expected_end']
    return '\n'.join((render_opening(composition), f"{message('staging_action')}: {composition['action']}",
                      message('staging_expected_end') + ':\n' + _states(end['characters'], CHARACTER_FIELDS)
                      + '\n' + _states(end['props'], PROP_FIELDS)))
