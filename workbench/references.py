"""Turn the CLI's cast/board reference slots into a path-free upload checklist."""
import copy
from pathlib import Path

from .i18n import InputError
from .inputs import array, invalid, text, verify_image


def describe_references(script):
    if not isinstance(script, dict):
        invalid("film", script, "object")
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
    return {"film": script, "slots": slots}


def bind_references(payload, upload_dir: Path, lang="zh-CN"):
    from .i18n import message

    result = copy.deepcopy(payload)
    described = describe_references(result.get("film"))
    script, slots = described["film"], described["slots"]
    bindings = result.pop("reference_bindings")
    placeholders = result.pop("demo_placeholders", False)
    if not isinstance(bindings, dict):
        invalid("reference_bindings", bindings, "object")
    if not isinstance(placeholders, bool):
        invalid("demo_placeholders", placeholders, "choice")
    known = {slot["id"] for slot in slots}
    if any(key not in known for key in bindings):
        invalid("reference_bindings", sorted(set(bindings) - known), "choice")
    missing = []
    for slot in slots:
        identifier = bindings.get(slot["id"])
        path = None
        if identifier is not None:
            identifier = text(identifier, f"reference_bindings.{slot['id']}")
            candidate = (upload_dir / identifier).resolve()
            if Path(identifier).name != identifier or not candidate.is_relative_to(upload_dir.resolve()) or not candidate.is_file():
                raise InputError("reference_upload_missing", slot=slot["name"] or message("reference_board", lang))
            verify_image(candidate)
            path = str(candidate)
        elif slot["required"]:
            if result.get("demo") is True and placeholders:
                # Missing paths are replaced by the existing offline demo runner.
                path = str(upload_dir.parent / "demo-placeholders" / f"{slot['id'].replace(':', '-')}.png")
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
