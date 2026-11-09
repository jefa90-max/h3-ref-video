"""Validate web inputs before starting the existing, potentially paid CLI."""
import copy
import json
import math
import tempfile
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from h3ref import config, film
from .i18n import InputError


def invalid(field, value, reason, **limits):
    raise InputError("invalid", field=field, value=repr(value)[:160], reason=reason, **limits)


def text(value, field, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        invalid(field, value, "text")
    return value.strip()


def choice(value, field, options):
    if not isinstance(value, str) or value not in options:
        invalid(field, value, "choice")
    return value


def number(value, field, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        invalid(field, value, "number", low=low, high=high)
    return value


def array(value, field):
    if not isinstance(value, list):
        invalid(field, value, "list")
    return value


def image_path(value, root, demo=False):
    path = Path(text(value, "picture")).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_file():
        if demo:
            return str(path)
        raise InputError("missing_image", path=str(path))
    verify_image(path)
    return str(path)


def verify_image(path):
    try:
        with Image.open(path) as image:
            if image.format not in ("PNG", "JPEG", "WEBP"):
                raise InputError("image", path=str(path))
            image.verify()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise InputError("image", path=str(path)) from exc


def validate(payload, root):
    if not isinstance(payload, dict):
        invalid("request", payload, "object")
    job = copy.deepcopy(payload)
    choice(job.get("kind"), "kind", ("one", "film"))
    if not isinstance(job.get("demo"), bool):
        invalid("demo", job.get("demo"), "choice")
    job["output_dir"] = text(job.get("output_dir", ""), "output_dir", empty=True)
    if "output_root" in job:
        job["output_root"] = text(job["output_root"], "output_root")
    job["operation"] = choice(job.get("operation", "pipeline"), "operation", ("pipeline", "frames", "frame", "segment", "assemble"))
    if job["kind"] == "one":
        choice(job["operation"], "operation", ("pipeline",))
        job["prompt"] = text(job.get("prompt"), "prompt")
        seconds = number(job.get("seconds", 12), "seconds", 5, 15)
        if int(seconds) != seconds:
            invalid("seconds", seconds, "choice")
        job["seconds"] = int(seconds)
        job["aspect"] = choice(job.get("aspect", "9:16"), "aspect", config.SIZES)
        images = array(job.get("images", []), "images")
        number(len(images), "images.length", 0 if job["demo"] else 1, 9)
        job["images"] = [image_path(p, root) for p in images]
        if job.get("seed") is not None:
            number(job["seed"], "seed", 0, 2 ** 53 - 1)
            if not isinstance(job["seed"], int):
                invalid("seed", job["seed"], "choice")
        if not isinstance(job.get("check", False), bool):
            invalid("check", job["check"], "choice")
    else:
        job["frames"] = choice(job.get("frames", "shot"), "frames", ("shot", "first", "none"))
        base = Path(text(job.get("film_base_dir", "."), "film_base_dir")).expanduser()
        base = (base if base.is_absolute() else root / base).resolve()
        job["film_base_dir"] = str(base)
        script = job.get("film")
        if not isinstance(script, dict):
            invalid("film", script, "object")
        script["aspect"] = choice(script.get("aspect", "9:16"), "aspect", config.SIZES)
        for field in ("title", "look", "setting", "speech"):
            if script.get(field) is not None:
                text(script[field], f"film.{field}", empty=True)
        cast = array(script.get("cast") if script.get("cast") is not None else [], "film.cast")
        for index, person in enumerate(cast):
            if not isinstance(person, dict):
                invalid(f"cast[{index}]", person, "object")
            text(person.get("name"), f"cast[{index}].name")
            text(person.get("look") or "", f"cast[{index}].look", empty=True)
            for alias in array(person.get("aliases") or [], f"cast[{index}].aliases"):
                text(alias, f"cast[{index}].aliases")
            person["picture"] = image_path(person.get("picture"), base, job["demo"])
        script["cast"] = cast
        if script.get("board"):
            script["board"] = image_path(script["board"], base, job["demo"])
        shots = array(script.get("shots"), "film.shots")
        for index, shot in enumerate(shots):
            prefix = f"shots[{index}]"
            if not isinstance(shot, dict):
                invalid(prefix, shot, "object")
            for field in ("action", "camera", "line"):
                text(shot.get(field) or "", prefix + "." + field, empty=True)
            if shot.get("start") is not None and shot.get("end") is not None:
                for field in ("start", "end"):
                    try:
                        value = float(shot[field])
                    except (ValueError, TypeError):
                        invalid(prefix + "." + field, shot[field], "number", low="-∞", high="∞")
                    if not math.isfinite(value):
                        invalid(prefix + "." + field, shot[field], "number", low="-∞", high="∞")
        # Let the CLI own all filtering, rounding and duration rules. Temporary
        # demo references only satisfy load()'s file check; they never reach a model.
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "film.json"
            candidate = copy.deepcopy(script)
            for index, person in enumerate(candidate["cast"]):
                if job["demo"] and not Path(person["picture"]).is_file():
                    placeholder = Path(temporary) / f"cast-{index}.png"
                    placeholder.touch()
                    person["picture"] = str(placeholder)
            source.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
            try:
                loaded = film.load(source)
            except (SystemExit, ValueError, TypeError, KeyError) as exc:
                raise InputError("film_invalid", detail=str(exc)) from exc
        loaded["cast"] = script["cast"]
        job["film"] = loaded
        for segment in film.plan(loaded["shots"]):
            try:
                film.refs(loaded, segment, job["frames"])
            except SystemExit as exc:
                raise InputError("film_invalid", detail=str(exc)) from exc
    return job


def preflight(job, env):
    if job.get("operation") in ("segment", "assemble"):
        from .segments import preflight as segment_preflight
        segment_preflight(job, env)
        return
    if job["demo"]:
        return
    import shutil
    from h3ref.rewrite import is_rewritten
    required = []
    out = Path(job["output_dir"]) if job.get("output_dir") else None
    state_file = out / "state.json" if out else None
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file and state_file.exists() else {}
    operation = job.get("operation", "pipeline")
    needs_rh, needs_rewrite, needs_frames, needs_vision = False, False, False, False
    if job["kind"] == "one":
        needs_rh = True
        h3 = state.get("h3") or (job["prompt"] if is_rewritten(job["prompt"]) else "")
        needs_rewrite = not h3
        needs_vision = config.SUBTITLE_CHECKS_ENABLED and (job.get("check", False) or any(ord(ch) > 0x2E80 for ch in h3))
    else:
        segments = film.plan(job["film"]["shots"])
        compositions = film.saved_compositions(state.get("frames", {}), reference_versions=film.frame_reference_versions(job["film"], segments, state.get("frames", {})))
        for segment in segments:
            pics = film.refs(job["film"], segment, job["frames"])
            needs_frames |= any(p["kind"] == "frame" and (not out or not (out / "frames" / f"s{segment['idx'] + 1}-{p['shot'] + 1}.png").exists()) for p in pics)
            if operation == "pipeline":
                rec = state.get("segs", {}).get(str(segment["idx"]), {})
                if not film.frozen_segment(rec) and (rec.get("prompt_override") or {}).get("format") != "h3":
                    zh = film.segment_prompt_text(rec, film.compile_prompt(job["film"], segment, pics, len(segments), compositions=compositions))
                    needs_rewrite |= not (rec.get("h3") and rec.get("zh") == zh)
                needs_rh |= not (out and (out / "clips" / f"seg{segment['idx'] + 1}.mp4").exists() and rec.get("cny") is not None)
        if operation == "frame":
            needs_frames = True
        if job.get("redo") and not job.get("reset_applied"):
            needs_rh = True
        # film.stage_check catches missing vision configuration and records it,
        # so the UI must not turn that optional check into a blocking key requirement.
    if needs_rh and not env.get("RUNNINGHUB_API_KEY", "").strip():
        required.append("RUNNINGHUB_API_KEY")
    if needs_rewrite and not (env.get("LLM_API_KEY", "").strip() or env.get("APIMART_API_KEY", "").strip()):
        required.append("LLM_API_KEY / APIMART_API_KEY")
    if needs_frames and not env.get("APIMART_API_KEY", "").strip():
        required.append("APIMART_API_KEY")
    if needs_vision and not (env.get("VISION_API_KEY", "").strip() or env.get("APIMART_API_KEY", "").strip()):
        required.append("VISION_API_KEY / APIMART_API_KEY")
    if required:
        raise InputError("keys", names=", ".join(dict.fromkeys(required)))
    if operation == "pipeline" and (not shutil.which("ffmpeg") or not shutil.which("ffprobe")):
        raise InputError("ffmpeg")
