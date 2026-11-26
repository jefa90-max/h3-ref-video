"""Turn the CLI's cast/board reference slots into a path-free upload checklist."""
import copy
import os
from pathlib import Path

from h3ref import config, preflight
from .i18n import InputError, message
from .inputs import array, invalid, text, verify_image


def describe_references(script, *, lang="zh-CN", include_preflight=False, environ=None):
    if not isinstance(script, dict):
        if include_preflight:
            raise InputError("film_object_invalid", field="film")
        invalid("film", script, "object")
    if include_preflight:
        shots = script.get("shots")
        if shots is not None and not isinstance(shots, list):
            raise InputError("dialogue_shots_invalid", field="film.shots")
        limit = max(2000, config.max_shots() * 2)
        if isinstance(shots, list) and len(shots) > limit:
            raise InputError("dialogue_references_limit", field="film.shots", actual=len(shots), limit=limit)
    script = copy.deepcopy(script)
    slots = []
    cast = script.get("cast") if script.get("cast") is not None else []
    for index, person in enumerate(array(cast, "film.cast")):
        if not isinstance(person, dict):
            invalid(f"cast[{index}]", person, "object")
        name = text(person.get("name"), f"cast[{index}].name")
        description = text(person.get("look") or "", f"cast[{index}].look", empty=True)
        person.pop("picture", None)
        slots.append({"id": f"cast:{index}", "kind": "cast", "name": name,
                      "description": description, "required": True})
    board = script.get("board")
    if board is not None and not isinstance(board, (str, bool)):
        invalid("film.board", board, "choice")
    script["board"] = True if board else None
    slots.append({"id": "board", "kind": "board", "name": "",
                  "description": text(script.get("setting") or "", "film.setting", empty=True),
                  "required": bool(board)})
    response = {"film": script, "slots": slots}
    if include_preflight:
        result = preflight.inspect_automatic(script, os.environ if environ is None else environ)
        if result["meta"].get("rate_config_fallback"):
            result["warnings"].append({"code": "dialogue_rate_config_fallback", "severity": "warn", "shot": None})
        warnings = [{**warning, "message": preflight.warning_message(warning, lambda key, **values: message(key, lang, **values))}
                    for warning in result["warnings"]]
        response.update(warnings=warnings, preflight_status=result["status"], preflight_meta=result["meta"])
    return response


def bind_references(payload, upload_dir: Path, lang="zh-CN"):
    result = copy.deepcopy(payload)
    described = describe_references(result.get("film"))
    script, slots = described["film"], described["slots"]
    bindings = result.pop("reference_bindings")
    raw_groups = result.pop("reference_groups", None)
    placeholders = result.pop("demo_placeholders", False)
    if not isinstance(bindings, dict):
        invalid("reference_bindings", bindings, "object")
    if not isinstance(placeholders, bool):
        invalid("demo_placeholders", placeholders, "choice")
    known = {slot["id"] for slot in slots}
    cast_ids = {slot["id"] for slot in slots if slot["kind"] == "cast"}
    if any(key not in known for key in bindings):
        invalid("reference_bindings", sorted(set(bindings) - known), "choice")
    slot_to_group_leader: dict[str, str] = {}
    if raw_groups is not None:
        groups = array(raw_groups, "reference_groups")
        seen_members: set[str] = set()
        for g_idx, group in enumerate(groups):
            members = array(group, f"reference_groups[{g_idx}]")
            if len(members) < 2:
                invalid(f"reference_groups[{g_idx}]", members, "choice")
            normalized_members = []
            for m_idx, member in enumerate(members):
                slot_id = text(member, f"reference_groups[{g_idx}][{m_idx}]")
                if slot_id not in cast_ids or slot_id in seen_members:
                    invalid(f"reference_groups[{g_idx}][{m_idx}]", slot_id, "choice")
                seen_members.add(slot_id)
                normalized_members.append(slot_id)
            bound_values = set()
            for m in normalized_members:
                if bindings.get(m) is not None:
                    bindings[m] = text(bindings[m], f"reference_bindings.{m}")
                    bound_values.add(bindings[m])
            if len(bound_values) > 1:
                invalid(f"reference_groups[{g_idx}]", normalized_members, "choice")
            if len(bound_values) == 1:
                shared_id = next(iter(bound_values))
                for m in normalized_members:
                    bindings[m] = shared_id
            leader = normalized_members[0]
            for m in normalized_members:
                slot_to_group_leader[m] = leader
    missing = []
    verified_candidates: set[Path] = set()
    for slot in slots:
        identifier = bindings.get(slot["id"])
        path = None
        if identifier is not None:
            identifier = text(identifier, f"reference_bindings.{slot['id']}")
            candidate = (upload_dir / identifier).resolve()
            if Path(identifier).name != identifier or not candidate.is_relative_to(upload_dir.resolve()) or not candidate.is_file():
                raise InputError("reference_upload_missing", slot=slot["name"] or message("reference_board", lang))
            if candidate not in verified_candidates:
                verify_image(candidate)
                verified_candidates.add(candidate)
            path = str(candidate)
        elif slot["required"]:
            if result.get("demo") is True and placeholders:
                placeholder_slot = slot_to_group_leader.get(slot["id"], slot["id"])
                path = str(upload_dir.parent / "demo-placeholders" / f"{placeholder_slot.replace(':', '-')}.png")
            else:
                missing.append(slot["name"] or message("reference_board", lang))
        if slot["kind"] == "cast":
            script["cast"][int(slot["id"].split(":")[1])]["picture"] = path
        else:
            script["board"] = path
    if missing:
        raise InputError("reference_required", names=", ".join(missing))
    result["film"] = script
    result.pop("film_base_dir", None)
    return result
