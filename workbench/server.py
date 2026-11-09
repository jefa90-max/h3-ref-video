"""Loopback-only Flask endpoints with optional private, local .env persistence."""
import json
import os
import platform
import shutil
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file, render_template
from werkzeug.exceptions import HTTPException

from h3ref import config
from .i18n import InputError, message
from .inputs import array, invalid, text, verify_image
from .jobs import Jobs
from .settings import save_env
from .references import bind_references, describe_references
from .directories import choose_output_folder, saved_output_root, save_output_root

KEYS = ("RUNNINGHUB_API_KEY", "APIMART_API_KEY", "LLM_API_KEY")
FIELDS = (*KEYS, "LLM_BASE_URL", "LLM_MODEL")


def create_app(storage=None, env_path=None):
    root = config.ROOT
    storage = Path(storage or root / ".workbench").resolve()
    app = Flask(__name__, static_folder="static")
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024 * 1024, TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"])
    jobs = Jobs(root, storage)
    folder_picker_lock = threading.Lock()
    env_path = Path(env_path or root / ".env")
    app.extensions["jobs"] = jobs
    env = dict(os.environ)
    env["H3_REWRITER"] = "llm"
    env.setdefault("LLM_BASE_URL", config.LLM_BASE)
    env.setdefault("LLM_MODEL", config.LLM_MODEL)
    jobs.remember_secrets(env)

    def language():
        return "en" if request.headers.get("Accept-Language", "").lower().startswith("en") else "zh-CN"

    def settings():
        return {"keys": {key: bool(env.get(key, "").strip()) for key in KEYS}, "LLM_BASE_URL": env["LLM_BASE_URL"],
                "LLM_MODEL": env["LLM_MODEL"], "rewriter": "llm", "python": platform.python_version(),
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
        response.headers["Cache-Control"] = "no-store"
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
        if request.endpoint == 'regenerate_segment':
            field = exc.values.get('field') or {'segment_changed': 'version', 'segment_number': 'number'}.get(exc.key)
            return segment_problem(exc.key, exc.localized(language()), status, field=field)
        return jsonify(error=jobs.redact(exc.localized(language()))), status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=message("http", language(), code=exc.code, detail=exc.description)), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        if request.endpoint in ('regenerate_segment', 'get_job'):
            import traceback
            instance = uuid.uuid4().hex
            number = (request.view_args or {}).get('number', '?')
            identifier = (request.view_args or {}).get('identifier', '?')
            trace = '; '.join(f'{Path(frame.filename).name}:{frame.lineno}:{frame.name}' for frame in traceback.extract_tb(exc.__traceback__)[-5:])
            # No exception messages/URLs or user script contents enter this trace.
            storage_path = storage / 'jobs' / f'{identifier}.log'
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
        return jsonify(config=settings(), example=json.loads((root / "examples" / "film.example.json").read_text(encoding="utf-8")),
                       example_base_dir="examples", jobs=jobs.summaries())

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
            if not isinstance(value, str) or "\n" in value or "\r" in value or "\0" in value:
                invalid(key, "[REDACTED]" if key in KEYS else value, "text")
            if key in KEYS and not value.strip():
                continue
            if key == "LLM_BASE_URL":
                url = urlsplit(value)
                if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
                    invalid(key, "[URL]", "choice")
            updates[key] = text(value, key, empty=key in KEYS)
        with jobs.lock:
            if persist:
                save_env(env_path, updates)
            env.update(updates)
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
            saved = json.loads((Path(record["output_dir"]) / "inputs" / "request.json").read_text(encoding="utf-8"))
            references = character_references(saved, identifier)
            if index >= len(references) or not references[index]["url"]:
                raise InputError("not_found", value=index)
            path = Path(references[index]["path"])
            verify_image(path)
            return send_file(path)

    @app.get("/api/jobs/<identifier>/frames/<key>/references/<int:index>")
    def frame_reference(identifier, key, index):
        from .storyboard import plan
        with jobs.lock:
            record = jobs.find(identifier)
            if record["kind"] != "film":
                raise InputError("not_found", value=key)
            saved = json.loads((Path(record["output_dir"]) / "inputs" / "request.json").read_text(encoding="utf-8"))
            description = next((item for item in plan(saved, check_images=False)[3] if item["key"] == key), None)
            if not description or index >= len(description["reference_paths"]):
                raise InputError("not_found", value=f"{key}/{index}")
            path = Path(description["reference_paths"][index])
            if not path.is_file():
                raise InputError("not_found", value=f"{key}/{index}")
            verify_image(path)
            return send_file(path)

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
        # The worker is a separate process. Pin an opened file, then validate that
        # descriptor; a path/version check alone races with atomic replacement.
        for _ in range(2):
            with jobs.lock:
                record = jobs.find(identifier)
                out = Path(record['output_dir'])
                context = segments.read_context({'output_dir': str(out)})
                matches = [a for a in jobs.artifacts(record, context) if a['name'] == name]
                mapped = name in context['pending'].get('files', {})
                source = segments.visible_file(out, name, context, request.args.get('v')) if matches or mapped else None
            if not source:
                break
            try:
                stream = source.open('rb')
            except FileNotFoundError:
                continue  # Publication removed/replaced this path; resolve once more.
            stat = os.fstat(stream.fileno())
            token = f'{stat.st_mtime_ns}:{stat.st_size}'
            expected = request.args.get('v') or segments.file_version(source, out, context)
            if expected not in (token, token.split(':')[0]):
                stream.close()
                continue
            try:
                if (matches and matches[0]['kind'] in ('report', 'file')) or (not matches and source.suffix == '.txt'):
                    with stream:
                        content = jobs.redact(stream.read().decode('utf-8'))
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
