"""Loopback-only Flask endpoints with optional private, local .env persistence."""
import json
import os
import platform
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, render_template
from werkzeug.exceptions import HTTPException

from h3ref import config, film
from .i18n import InputError, message
from .inputs import array, invalid, text, verify_image
from .jobs import ARTIFACT_TYPES, Jobs
from .settings import save_env
from .references import bind_references, describe_references
from .directories import choose_output_folder, saved_output_root, save_output_root

KEYS = ("RUNNINGHUB_API_KEY", "APIMART_API_KEY", "LLM_API_KEY", "GEMINI_API_KEY")
FIELDS = (*KEYS, "LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_COMPOSITION_MODEL", "LLM_REWRITE_MODEL",
          "GEMINI_MODEL", "GEMINI_COMPOSITION_MODEL", "GEMINI_REWRITE_MODEL")


def create_app(storage=None, env_path=None):
    root = config.ROOT
    storage = Path(storage or root / ".workbench").resolve()
    app = Flask(__name__, static_folder="static")
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024 * 1024, TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"])
    jobs = Jobs(root, storage)
    folder_picker_lock = threading.Lock()
    env_path = Path(env_path or root / ".env")
    app.extensions["jobs"] = jobs
    env = config.load_server_env(env_path)
    env["H3_REWRITER"] = "llm"
    env.setdefault("LLM_BASE_URL", config.LLM_DEFAULTS["apimart"]["base_url"])
    env.setdefault("LLM_MODEL", config.LLM_DEFAULTS["apimart"]["model"])
    if env.get("LLM_PROVIDER", "").strip().lower() not in ("", *config.LLM_DEFAULTS):
        env["LLM_PROVIDER"] = "apimart"
    config.llm_settings(env)
    jobs.remember_secrets(env)

    verified_images = set()

    def send_verified_image(path):
        resolved = path.resolve()
        try:
            st = resolved.stat()
        except FileNotFoundError:
            raise InputError("not_found", value=path.name)
        token = f"{st.st_mtime_ns}:{st.st_size}"
        stamp = (str(resolved), st.st_mtime_ns, st.st_size)
        if stamp not in verified_images:
            verify_image(resolved)
            if len(verified_images) >= 512:
                verified_images.clear()
            verified_images.add(stamp)
        if request.if_none_match and request.if_none_match.contains(token):
            response = app.response_class(status=304)
            response.set_etag(token)
            return response
        response = send_file(resolved, conditional=False)
        response.set_etag(token)
        response.last_modified = st.st_mtime
        response.make_conditional(request.environ)
        return response

    def language():
        return "en" if request.headers.get("Accept-Language", "").lower().startswith("en") else "zh-CN"

    def settings():
        return {"keys": {key: bool(env.get(key, "").strip()) for key in KEYS}, "LLM_BASE_URL": env["LLM_BASE_URL"],
                "LLM_MODEL": env["LLM_MODEL"], "LLM_PROVIDER": config.llm_settings(env)["provider"],
                "llm_providers": {provider: {**config.llm_settings({**env, "LLM_PROVIDER": provider}),
                                             "key_names": list(config.llm_settings({**env, "LLM_PROVIDER": provider})["key_names"])}
                                  for provider in config.LLM_DEFAULTS},
                "rewriter": "llm", "python": platform.python_version(),
                "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")), "saved_to_env": env_path.is_file(),
                "output_root": str(saved_output_root(root, storage))}

    @app.before_request
    def local_requests():
        # A custom header forces a cross-origin preflight, for which we grant no CORS access.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if request.headers.get("X-Workbench") != "1" or (origin and origin != request.host_url.rstrip("/")):
                raise InputError("origin")

    @app.after_request
    def headers(response):
        if "ETag" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers.setdefault("Cache-Control", "private, no-cache")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; style-src-attr 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    def segment_problem(code, detail, status, *, field=None, instance=None):
        body = dict(type='urn:workbench:error:' + code, title=message('segment_problem_title', language()), status=status,
                    detail=jobs.redact(detail), error=jobs.redact(detail), code=code, doc_anchor='docs/workbench.md#video-prompt-errors')
        if field:
            body['field'] = field
        if instance:
            body['instance'] = instance
        response = jsonify(body)
        response.status_code = status
        response.mimetype = 'application/problem+json'
        return response

    @app.errorhandler(InputError)
    def input_error(exc):
        status = 404 if exc.key == 'not_found' else 400
        if request.endpoint in ('regenerate_segment', 'rewrite_segment', 'rewrite_prompts'):
            field = exc.values.get('field') or {'segment_changed': 'version', 'segment_number': 'number'}.get(exc.key)
            return segment_problem(exc.key, exc.localized(language()), status, field=field)
        return jsonify(error=jobs.redact(exc.localized(language()))), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=message("http", language(), code=exc.code, detail=exc.description)), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        if request.endpoint in ('regenerate_segment', 'rewrite_segment', 'rewrite_prompts', 'get_job'):
            import traceback
            instance = uuid.uuid4().hex
            number = (request.view_args or {}).get('number', '?')
            identifier = (request.view_args or {}).get('identifier', '?')
            trace = '; '.join(f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(exc.__traceback__)[-5:])
            # No exception messages/URLs or user script contents enter this trace.
            if isinstance(identifier, str) and identifier in jobs.records:
                storage_path = (storage / 'jobs' / f'{identifier}.log').resolve()
                if storage_path.parent == (storage / 'jobs').resolve():
                    with storage_path.open('a', encoding='utf-8') as log:
                        log.write(f'correlation={instance} segment={number} error={type(exc).__name__} trace={trace}\n')
            return segment_problem('segment_internal', message('segment_internal', language(), number=number, instance=instance), 500, instance=instance)
        return jsonify(error=message("internal", language(), detail=jobs.redact(f"{type(exc).__name__}: {exc}"))), 500

    @app.get("/")
    def index():
        return render_template("index.html", subtitle_checks_enabled=config.SUBTITLE_CHECKS_ENABLED)

    @app.get("/jobs/<identifier>")
    def result_page(identifier):
        return render_template("results.html", job_id=identifier)

    @app.get("/api/bootstrap")
    def bootstrap():
        return jsonify(config=settings(), jobs=jobs.summaries())

    @app.get("/api/film/example")
    def download_film_example():
        source = json.loads((root / "examples" / "film.example.json").read_text(encoding="utf-8"))
        example = describe_references(source)["film"]
        return app.response_class(json.dumps(example, ensure_ascii=False, indent=2) + "\n", mimetype="application/json",
                                  headers={"Content-Disposition": 'attachment; filename="film.example.json"'})

    @app.post("/api/config")
    def configure():
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("config", values, "object")
        persist = values.pop("persist", True)
        if not isinstance(persist, bool):
            invalid("persist", persist, "choice")
        updates = {}
        for key, value in values.items():
            if key not in FIELDS:
                invalid("config", key, "choice")
            # Never quote a key's value in an error response.
            if not isinstance(value, str) or "\0" in value or len(f"{key}={value}".splitlines()) != 1:
                invalid(key, "[REDACTED]" if key in KEYS else value, "text")
            if key in KEYS and not value.strip():
                continue
            if key == "LLM_PROVIDER" and value.strip() not in config.LLM_DEFAULTS:
                invalid(key, value, "choice")
            if key == "LLM_BASE_URL":
                try:
                    url = urlsplit(value.strip())
                    _ = url.port
                except ValueError:
                    invalid(key, "[URL]", "choice")
                if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
                    invalid(key, "[URL]", "choice")
            updates[key] = text(value, key, empty=key in KEYS)
        with jobs.lock:
            disk_updates = dict(updates)
            mem_updates = dict(updates)
            if "LLM_BASE_URL" in updates and "LLM_API_KEY" not in updates:
                new_origin = config.url_origin(updates["LLM_BASE_URL"])
                mem_changed = new_origin != config.url_origin(env.get("LLM_BASE_URL", ""))
                if mem_changed:
                    mem_updates["LLM_API_KEY"] = None
                if persist:
                    disk_file = config.read_env_file(env_path)
                    disk_base = disk_file.get("LLM_BASE_URL") or config.LLM_DEFAULTS["apimart"]["base_url"]
                    disk_changed = new_origin != config.url_origin(disk_base)
                    if disk_changed or mem_changed or (not env.get("LLM_API_KEY", "").strip() and "LLM_API_KEY" in disk_file):
                        disk_updates["LLM_API_KEY"] = None
            if persist:
                save_env(env_path, disk_updates)
            for k, v in mem_updates.items():
                env[k] = "" if v is None else v
            jobs.remember_secrets(env)
        return jsonify(settings())

    @app.post("/api/output-folder/pick")
    def pick_output_folder():
        if not folder_picker_lock.acquire(blocking=False):
            raise InputError("output_picker_busy")
        try:
            selected = choose_output_folder(saved_output_root(root, storage), language())
            if selected is None:
                return jsonify(cancelled=True, output_root=str(saved_output_root(root, storage)))
            with jobs.lock:
                folder = save_output_root(selected, root, storage)
            return jsonify(cancelled=False, output_root=str(folder))
        finally:
            folder_picker_lock.release()

    @app.post("/api/uploads")
    def upload():
        images = request.files.getlist("images")
        if not 1 <= len(images) <= 9:
            raise InputError("upload")
        folder = storage / "uploads"
        folder.mkdir(exist_ok=True)
        saved, result = [], []
        try:
            for uploaded in images:
                suffix = Path(uploaded.filename or "").suffix.lower()
                if suffix not in (".png", ".jpg", ".jpeg", ".webp"):
                    raise InputError("image", path=uploaded.filename)
                path = folder / (uuid.uuid4().hex + suffix)
                saved.append(path)
                uploaded.save(path)
                verify_image(path)
                result.append({"id": path.name, "name": uploaded.filename, "path": str(path), "url": f"/api/uploads/{path.name}"})
        except Exception:
            for path in saved:
                path.unlink(missing_ok=True)
            raise
        return jsonify(files=result)

    @app.get("/api/uploads/<name>")
    def uploaded_image(name):
        folder = storage / "uploads"
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or path.suffix not in (".png", ".jpg", ".jpeg", ".webp"):
            raise InputError("not_found", value=name)
        return send_file(path)

    @app.post("/api/jobs")
    def create_job():
        with jobs.lock:
            payload = request.get_json()
            if isinstance(payload, dict) and not payload.get("output_dir"):
                payload.setdefault("output_root", str(saved_output_root(root, storage)))
            if isinstance(payload, dict) and payload.get("kind") == "film" and "reference_bindings" in payload:
                payload = bind_references(payload, storage / "uploads", language())
            return jsonify(jobs.create(payload, dict(env), language())), 201

    @app.post("/api/film/references")
    def film_references():
        payload = request.get_json()
        if not isinstance(payload, dict):
            invalid("request", payload, "object")
        return jsonify(describe_references(payload.get("film")))

    @app.get("/api/jobs/<identifier>")
    def get_job(identifier):
        return jsonify(jobs.snapshot(identifier))

    @app.post("/api/jobs/<identifier>/resume")
    def resume_job(identifier):
        with jobs.lock:
            return jsonify(jobs.resume(identifier, dict(env)))

    @app.post("/api/jobs/<identifier>/frames/<key>")
    def regenerate_frame(identifier, key):
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("frame", values, "object")
        for field in values:
            if field not in ("prompt", "character_reference_paths"):
                invalid("frame", field, "choice")
        characters = array(values["character_reference_paths"], "character_reference_paths") if "character_reference_paths" in values else None
        with jobs.lock:
            return jsonify(jobs.edit_frame(identifier, key, values.get("prompt"), dict(env), characters))

    @app.get("/api/jobs/<identifier>/characters/<int:index>")
    def character_reference(identifier, index):
        from .storyboard import character_references
        with jobs.lock:
            record = jobs.find(identifier)
            if record["kind"] != "film":
                raise InputError("not_found", value=index)
            out = Path(record["output_dir"])
        saved = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
        references = character_references(saved, identifier)
        if index >= len(references) or not references[index]["url"]:
            raise InputError("not_found", value=index)
        path = Path(references[index]["path"])
        return send_verified_image(path)

    @app.get("/api/jobs/<identifier>/frames/<key>/references/<int:index>")
    def frame_reference(identifier, key, index):
        from .storyboard import frame_records, read_json
        with jobs.lock:
            record = jobs.find(identifier)
            if record["kind"] != "film":
                raise InputError("not_found", value=key)
            out = Path(record["output_dir"])
        match = re.fullmatch(r"s(\d+)-(\d+)", key)
        if not match:
            raise InputError("not_found", value=f"{key}/{index}")
        seg_idx, shot_idx = int(match.group(1)) - 1, int(match.group(2)) - 1
        saved = json.loads((out / "inputs" / "request.json").read_text(encoding="utf-8"))
        source = out / "inputs" / ("demo-film.json" if saved["demo"] and (out / "inputs" / "demo-film.json").exists() else "film.json")
        script = film.load(source, check_images=False)
        planned = film.plan(script["shots"])
        if not (0 <= seg_idx < len(planned)):
            raise InputError("not_found", value=f"{key}/{index}")
        segment = planned[seg_idx]
        if not any(p["kind"] == "frame" and p["shot"] == shot_idx for p in film.refs(script, segment, saved["frames"])):
            raise InputError("not_found", value=f"{key}/{index}")
        allowed = {Path(c["picture"]).resolve() for c in script.get("cast") or [] if c.get("picture")}
        if script.get("board"):
            allowed.add(Path(script["board"]).resolve())
        who = [c for c in script.get("cast") or [] if film.in_shots(c, [segment["shots"][shot_idx]])]
        default_char_paths = [g["path"] for g in film.group_cast_by_picture(who, script.get("cast") or [])]
        pending_frame = read_json(out / "inputs" / "frame-edit.json", {})
        active_pending = pending_frame if pending_frame and not pending_frame.get("applied") and pending_frame.get("key") == key else {}
        raw_characters = active_pending.get("character_reference_paths") if "character_reference_paths" in active_pending else frame_records(saved).get(key, {}).get("character_reference_paths", default_char_paths)
        characters = film.deduplicate_paths(raw_characters)
        ref_paths = ([script["board"]] if script.get("board") else []) + list(characters)
        if not (0 <= index < len(ref_paths)):
            raise InputError("not_found", value=f"{key}/{index}")
        path = Path(ref_paths[index]).resolve()
        if path not in allowed or not path.is_file():
            raise InputError("not_found", value=f"{key}/{index}")
        return send_verified_image(path)

    @app.post("/api/jobs/<identifier>/continue")
    def continue_film(identifier):
        confirmation = request.get_json()
        if not isinstance(confirmation, dict):
            raise InputError("review_request")
        with jobs.lock:
            return jsonify(jobs.continue_film(identifier, dict(env), confirmation))

    @app.post("/api/jobs/<identifier>/segments/<int:number>/regenerate")
    def regenerate_segment(identifier, number):
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("segment", values, "object")
        with jobs.lock:
            edited = any(key in values for key in ('prompt', 'prompt_format', 'prompt_version'))
            return jsonify(jobs.start_video_operation(identifier, 'segment', dict(env), values.get('version'), number,
                prompt=values.get('prompt'), prompt_format=values.get('prompt_format', '') if edited else None,
                prompt_version=values.get('prompt_version', '') if edited else None))

    @app.post("/api/jobs/<identifier>/segments/<int:number>/rewrite")
    def rewrite_segment(identifier, number):
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("segment", values, "object")
        with jobs.lock:
            edited = any(key in values for key in ('prompt', 'prompt_format', 'prompt_version'))
            return jsonify(jobs.start_video_operation(identifier, 'rewrite_segment', dict(env), None, number,
                prompt=values.get('prompt'), prompt_format=values.get('prompt_format', 'source') if edited else None,
                prompt_version=values.get('prompt_version', '') if edited else None))

    @app.post("/api/jobs/<identifier>/segments/rewrite")
    def rewrite_prompts(identifier):
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("segment", values, "object")
        with jobs.lock:
            return jsonify(jobs.start_video_operation(identifier, 'rewrite_prompts', dict(env), None,
                scope=values.get('scope', 'missing')))

    @app.post("/api/jobs/<identifier>/assemble")
    def assemble_film(identifier):
        values = request.get_json()
        if not isinstance(values, dict):
            invalid("assembly", values, "object")
        with jobs.lock:
            return jsonify(jobs.start_video_operation(identifier, "assemble", dict(env), values.get("version")))

    @app.get("/api/jobs/<identifier>/files/<path:name>")
    def artifact(identifier, name):
        from . import segments
        parts = Path(name).parts
        kind = ARTIFACT_TYPES.get(Path(name).suffix.lower())
        whitelisted = bool(kind and (
            name in ("film.mp4", "clip.mp4", "report.md", "plan.json", "prompt.h3.txt")
            or (len(parts) == 2 and parts[0] in ("frames", "clips", "prompts") and parts[1] not in ("", ".", ".."))
        ))
        # The worker is a separate process. Pin an opened file, then validate that
        # descriptor; a path/version check alone races with atomic replacement.
        for _ in range(2):
            with jobs.lock:
                record = jobs.find(identifier)
                out = Path(record['output_dir'])
            context = segments.artifact_context(out)
            mapped = bool(kind and name in context['pending'].get('files', {}))
            source = segments.visible_file(out, name, context, request.args.get('v')) if whitelisted or mapped else None
            if not source:
                break
            try:
                stream = source.open('rb')
            except FileNotFoundError:
                continue  # Publication removed/replaced this path; resolve once more.
            stat = os.fstat(stream.fileno())
            token = f'{stat.st_mtime_ns}:{stat.st_size}'
            expected = request.args.get('v') or segments.file_version(source, out, context)
            if expected and ((expected != token) if ':' in expected else (expected != token.split(':')[0])):
                stream.close()
                continue
            try:
                if kind in ('report', 'file') or (not whitelisted and source.suffix == '.txt'):
                    with stream:
                        content = jobs.redact(stream.read().decode('utf-8', errors='replace'))
                    return app.response_class(content, mimetype='text/plain')
                response = send_file(stream, download_name=Path(name).name, conditional=False,
                                     as_attachment=request.args.get('download') == '1')
                response.content_length = stat.st_size
                response.last_modified = stat.st_mtime
                response.set_etag(token)
                response.make_conditional(request.environ, accept_ranges=True, complete_length=stat.st_size)
                response.call_on_close(stream.close)
                return response
            except BaseException:
                stream.close()
                raise
        raise InputError('not_found', value=name)

    return app
