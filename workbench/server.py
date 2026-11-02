"""Loopback-only Flask endpoints with optional private, local .env persistence."""
import json
import os
import platform
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

from h3ref import config
from .i18n import InputError, message
from .inputs import invalid, text, verify_image
from .jobs import Jobs
from .settings import save_env
from .references import bind_references, describe_references

KEYS = ("RUNNINGHUB_API_KEY", "APIMART_API_KEY", "LLM_API_KEY")
FIELDS = (*KEYS, "LLM_BASE_URL", "LLM_MODEL")


def create_app(storage=None, env_path=None):
    root = config.ROOT
    storage = Path(storage or root / ".workbench").resolve()
    app = Flask(__name__, static_folder="static")
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024 * 1024, TRUSTED_HOSTS=["127.0.0.1", "localhost", "[::1]"])
    jobs = Jobs(root, storage)
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
                "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe")), "saved_to_env": env_path.is_file()}

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

    @app.errorhandler(InputError)
    def input_error(exc):
        return jsonify(error=jobs.redact(exc.localized(language()))), 404 if exc.key == "not_found" else 400

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=message("http", language(), code=exc.code, detail=exc.description)), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        return jsonify(error=message("internal", language(), detail=jobs.redact(f"{type(exc).__name__}: {exc}"))), 500

    @app.get("/")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/bootstrap")
    def bootstrap():
        return jsonify(config=settings(), example=json.loads((root / "examples" / "film.example.json").read_text(encoding="utf-8")),
                       example_base_dir="examples", jobs=jobs.summaries())

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
        with jobs.lock:
            return jsonify(jobs.edit_frame(identifier, key, values.get("prompt"), dict(env)))

    @app.post("/api/jobs/<identifier>/continue")
    def continue_film(identifier):
        with jobs.lock:
            return jsonify(jobs.continue_film(identifier, dict(env)))

    @app.get("/api/jobs/<identifier>/files/<path:name>")
    def artifact(identifier, name):
        with jobs.lock:
            matches = [a for a in jobs.artifacts(jobs.find(identifier)) if a["name"] == name]
        if not matches:
            raise InputError("not_found", value=name)
        artifact = matches[0]
        if artifact["kind"] in ("report", "file"):
            return app.response_class(jobs.redact(Path(artifact["path"]).read_text(encoding="utf-8")), mimetype="text/plain")
        return send_file(artifact["path"], conditional=True)

    return app
