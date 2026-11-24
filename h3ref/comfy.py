"""ComfyUI remote backend: MiniMax H3 reference-to-video workflow (workflows/comfy-h3-ref2va.json).

Uploads reference images with pure ASCII content-hash filenames (<sha256[:16]><ext>), builds a dynamic 1..9 image
API prompt graph with selectable 768p/544p native dimensions, optional 2x RealESRGAN_x2plus upscaling, and preset-locked
step/LoRA pairs, submits to /prompt, polls /history/{prompt_id} + /queue (failing fast if missing twice
consecutively after an instance restart), and downloads completed videos via /view with tunnel authentication headers.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from . import config, http, rh
from .i18n import message

SAVE, PROMPT, DURATION, LENGTH, SIZE, SAMPLER, STEPS, SEED, CLIP = "92", "138", "132", "131", "136", "123", "124", "129", "128"
LORA, ENABLE_LORA, FULL_STEPS, TURBO_STEPS, SWITCH_MODEL, SWITCH_STEPS = "145", "146", "143", "144", "141", "142"
VAE_DECODE, CREATE_VIDEO, UPSCALE_LOADER, UPSCALE_IMAGE, EASYCACHE, SPARSE_ATTN = "122", "130", "154", "155", "156", "157"
UPSCALE_MODEL_NAME = "RealESRGAN_x2plus.pth"
IMAGES = ["137", "139", "147", "148", "149", "150", "151", "152", "153"]
NODE_TYPES = {
    SAVE: "SaveVideo",
    PROMPT: "PrimitiveStringMultiline",
    DURATION: "PrimitiveFloat",
    LENGTH: "ComfyMathExpression",
    SIZE: "MiniMaxH3ReferenceToVideo",
    SAMPLER: "KSamplerSelect",
    STEPS: "BasicScheduler",
    SEED: "RandomNoise",
    CLIP: "CLIPLoader",
    LORA: "LoraLoaderModelOnly",
    ENABLE_LORA: "PrimitiveBoolean",
    FULL_STEPS: "PrimitiveInt",
    TURBO_STEPS: "PrimitiveInt",
    SWITCH_MODEL: "ComfySwitchNode",
    SWITCH_STEPS: "ComfySwitchNode",
    VAE_DECODE: "VAEDecode",
    CREATE_VIDEO: "CreateVideo",
    UPSCALE_LOADER: "UpscaleModelLoader",
    UPSCALE_IMAGE: "ImageUpscaleWithModel",
    EASYCACHE: "EasyCache",
    SPARSE_ATTN: "BlockSparseAttention",
    **{n: "LoadImage" for n in IMAGES},
}
NODE_STAGE_KEYS = {
    "128": "load_clip",
    "119": "load_vae",
    "120": "load_vae",
    "127": "load_model",
    "145": "load_lora",
    "136": "encode_prompt",
    "125": "sampling",
    "122": "decode_video",
    "121": "decode_audio",
    "154": "load_upscaler",
    "155": "upscale_video",
    "130": "create_video",
    "92": "save_video",
    **{n: "load_images" for n in IMAGES},
}

SubmissionRejected = rh.SubmissionRejected
NON_RETRYABLE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}
_ASCII_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
_MISSING_LOCK = threading.Lock()
_MISSING_COUNTS: dict[tuple[str, str], int] = {}
_TRANSIENT_COUNTS: dict[tuple[str, str], int] = {}
_UPLOAD_LOCK = threading.Lock()
_UPLOADED_CACHE: dict[tuple[str, str], str] = {}


class NoHealthyNodeError(RuntimeError):
    """Raised when no healthy ComfyUI endpoints remain available in the node pool."""


class DispatchAbortedError(RuntimeError):
    """Raised by ComfyNodePool.acquire() when another segment already triggered abort."""


def _headers() -> dict[str, str]:
    return {"ngrok-skip-browser-warning": "1", "User-Agent": "h3-ref-video/1.0"}


def endpoint_label(base_url: str) -> str:
    """Compact human-readable node label (host[:port]) for logs and UI."""
    cleaned = str(base_url or "").strip()
    if not cleaned:
        return ""
    parts = urlsplit(cleaned)
    host = parts.hostname or cleaned
    try:
        port = parts.port
    except ValueError:
        port = None
    default_port = 443 if parts.scheme.lower() == "https" else 80 if parts.scheme.lower() == "http" else None
    if port and port != default_port:
        return f"{host}:{port}"
    return host


def key_fingerprint(api_key: str) -> str:
    cleaned = str(api_key or "").strip()
    if not cleaned:
        return ""
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]


def is_plaintext_remote_http(url: str) -> bool:
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() != "http":
        return False
    host = (parts.hostname or "").lower()
    return host not in ("127.0.0.1", "localhost", "::1")


def key_for_endpoint(base_url: str, api_key: str) -> str:
    """Never send Bearer secrets over plaintext non-loopback HTTP."""
    if not api_key or is_plaintext_remote_http(base_url):
        return ""
    return api_key


def assert_resume_endpoint_key_allowed(
    endpoint_url: str | None,
    *,
    expected_key_fp: str | None = None,
    environ=None,
) -> None:
    """Block sending a rotated/new COMFYUI_API_KEY to a previously removed remote endpoint."""
    if not endpoint_url:
        return
    vcfg = config.video_settings(environ)
    key = key_for_endpoint(endpoint_url, vcfg["comfyui_api_key"])
    if not key:
        return
    configured_origins = {config.url_origin(u) for u in vcfg["comfyui_urls"]}
    if config.url_origin(endpoint_url) in configured_origins:
        return
    if expected_key_fp and expected_key_fp == key_fingerprint(key):
        return
    raise RuntimeError(
        f"Refusing to send current COMFYUI_API_KEY to removed ComfyUI endpoint {endpoint_label(endpoint_url)}"
    )


def _resolve_endpoint(base_url: str | None = None, api_key: str | None = None) -> tuple[str, str]:
    base = config.validate_endpoint_url(base_url, "COMFYUI_URL") if base_url else config.comfy_url()
    raw_key = api_key.strip() if api_key is not None else config.comfy_key()
    return base, key_for_endpoint(base, raw_key)


def base_graph() -> dict:
    g = json.loads(config.COMFY_GRAPH.read_text(encoding="utf-8"))
    bad = [n for n, t in NODE_TYPES.items() if g.get(n, {}).get("class_type") != t]
    if bad:
        raise SystemExit(f"{config.COMFY_GRAPH.name} 的节点对不上（{', '.join(bad)}），换过工作流就要改 comfy.py 里的节点号")
    return g


def _ascii_hash_filename(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    ext = path.suffix.lower()
    if ext not in _ASCII_IMAGE_EXTS:
        ext = ".png"
    return f"{digest}{ext}"


def clear_upload_cache(base_url: str | None = None) -> None:
    """Clear cached (endpoint, content-hash) upload mappings."""
    with _UPLOAD_LOCK:
        if base_url is None:
            _UPLOADED_CACHE.clear()
        else:
            for cache_key in [k for k in _UPLOADED_CACHE if k[0] == base_url]:
                _UPLOADED_CACHE.pop(cache_key, None)


def _is_ngrok_offline_error(exc: Exception) -> bool:
    if isinstance(exc, http.HTTPResponseError) and exc.status_code != 404:
        return False
    text = str(exc)
    return "ERR_NGROK_3200" in text or ("ERR_NGROK_" in text and "offline" in text.lower())


def _is_tunnel_offline_error(exc: Exception) -> bool:
    if _is_ngrok_offline_error(exc):
        return True
    text = str(exc)
    if isinstance(exc, http.HTTPResponseError) and exc.status_code == 530:
        return True
    return ": 530:" in text or "error 1033" in text.lower() or "argo tunnel error" in text.lower()


def upload(path: Path, *, force: bool = False, base_url: str | None = None, api_key: str | None = None) -> str:
    """Upload an input image to ComfyUI /upload/image under an ASCII content-hash filename."""
    base, key = _resolve_endpoint(base_url, api_key)
    ascii_name = _ascii_hash_filename(path)
    cache_key = (base, ascii_name)
    if not force:
        with _UPLOAD_LOCK:
            cached = _UPLOADED_CACHE.get(cache_key)
        if cached:
            return cached
    try:
        out = http.upload(
            f"{base}/upload/image",
            key,
            path,
            {"type": "input", "overwrite": "true"},
            file_field_name="image",
            filename=ascii_name,
            extra_headers=_headers(),
        )
    except http.HTTPResponseError as exc:
        if _is_ngrok_offline_error(exc):
            raise http.HTTPResponseError(
                exc.status_code,
                message("comfy_tunnel_offline", node=endpoint_label(base), detail=str(exc)),
            ) from exc
        raise
    status = out.get("_http_status")
    name = out.get("name") if isinstance(out, dict) else None
    if (isinstance(status, int) and status >= 400) or not isinstance(name, str) or not name.strip():
        detail = json.dumps(out, ensure_ascii=False)[:240]
        if key:
            detail = detail.replace(key, "[key]")
        raise RuntimeError(f"upload to ComfyUI failed: {detail}")
    subfolder = str(out.get("subfolder") or "").strip("/")
    remote_name = f"{subfolder}/{name.strip()}" if subfolder else name.strip()
    with _UPLOAD_LOCK:
        _UPLOADED_CACHE[cache_key] = remote_name
    return remote_name


def upload_many(
    paths: list[Path],
    *,
    max_workers: int = 4,
    base_url: str | None = None,
    api_key: str | None = None,
) -> list[str]:
    """Upload reference images concurrently with content-hash deduplication while preserving order."""
    if not paths:
        return []
    keys: list[str] = []
    unique_paths: dict[str, Path] = {}
    for p in paths:
        file_key = _ascii_hash_filename(p) if p.is_file() else str(p)
        keys.append(file_key)
        unique_paths.setdefault(file_key, p)
    call_kwargs = {}
    if base_url is not None:
        call_kwargs["base_url"] = base_url
    if api_key is not None:
        call_kwargs["api_key"] = api_key
    if len(unique_paths) == 1:
        only_key, only_path = next(iter(unique_paths.items()))
        uploaded = {only_key: upload(only_path, **call_kwargs)}
    else:
        workers = max(1, min(int(max_workers), len(unique_paths)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {k: pool.submit(upload, p, **call_kwargs) for k, p in unique_paths.items()}
            uploaded = {k: fut.result() for k, fut in futures.items()}
    return [uploaded[k] for k in keys]


def graph(
    prompt: str,
    image_names: list[str],
    seconds: float,
    aspect: str,
    seed: int,
    *,
    preset: str | None = None,
    upscale: bool | None = None,
    resolution: str | None = None,
) -> dict:
    """Build a ComfyUI API prompt graph with dynamic 1..9 LoadImage inputs, selectable resolution, preset-locked LoRA/steps, and optional 2x upscaling."""
    if not 1 <= len(image_names) <= len(IMAGES):
        raise ValueError(f"H3 需要 1 到 {len(IMAGES)} 张参考图，这一段有 {len(image_names)} 张")
    vcfg = config.video_settings() if (preset is None or upscale is None or resolution is None) else None
    resolution_name = (resolution if resolution is not None else vcfg["comfyui_resolution"]).strip().lower()
    if resolution_name not in config.COMFY_RESOLUTIONS:
        raise ValueError(f"COMFYUI_RESOLUTION={resolution_name!r} (768p | 544p)")
    sizes = config.COMFY_RESOLUTIONS[resolution_name]
    if aspect not in sizes:
        raise ValueError(f"画幅只支持 {', '.join(sizes)}")
    preset_name = (preset if preset is not None else vcfg["comfyui_preset"]).strip().lower()
    if preset_name not in config.COMFY_PRESETS:
        raise ValueError(f"COMFYUI_PRESET={preset_name!r} (4step | 8step | full20)")
    preset_cfg = config.COMFY_PRESETS[preset_name]
    use_upscale = vcfg["comfyui_upscale"] if upscale is None else bool(upscale)

    g = base_graph()
    g[SAVE]["inputs"]["filename_prefix"] = f"video/MiniMax_H3_{uuid.uuid4().hex[:12]}"
    g[PROMPT]["inputs"]["value"] = prompt
    g[DURATION]["inputs"]["value"] = max(float(config.MIN_SECONDS), min(float(config.MAX_SECONDS), float(seconds)))
    g[SIZE]["inputs"]["width"], g[SIZE]["inputs"]["height"] = sizes[aspect]
    g[SEED]["inputs"]["noise_seed"] = int(seed)
    g[SAMPLER]["inputs"]["sampler_name"] = preset_cfg["sampler"]
    g[STEPS]["inputs"]["scheduler"] = preset_cfg["scheduler"]
    g[ENABLE_LORA]["inputs"]["value"] = bool(preset_cfg["enable_lora"])
    g[TURBO_STEPS]["inputs"]["value"] = int(preset_cfg["steps"])
    g[FULL_STEPS]["inputs"]["value"] = 20
    if preset_cfg["enable_lora"]:
        g[LORA]["inputs"]["lora_name"] = preset_cfg["lora_name"]
        g["126"]["inputs"]["model"] = [SWITCH_MODEL, 0]
        g[STEPS]["inputs"]["model"] = [SWITCH_MODEL, 0]
        g.pop(EASYCACHE, None)
        g.pop(SPARSE_ATTN, None)
    else:
        model_ref = ["127", 0]
        if preset_cfg.get("enable_sparse_attention"):
            g[SPARSE_ATTN]["inputs"]["model"] = model_ref
            model_ref = [SPARSE_ATTN, 0]
        else:
            g.pop(SPARSE_ATTN, None)
        if preset_cfg.get("enable_easycache"):
            g[EASYCACHE]["inputs"]["model"] = model_ref
            model_ref = [EASYCACHE, 0]
        else:
            g.pop(EASYCACHE, None)
        g["126"]["inputs"]["model"] = model_ref
        g[STEPS]["inputs"]["model"] = model_ref
        g[SWITCH_MODEL]["inputs"]["on_true"] = model_ref
        g[SWITCH_MODEL]["inputs"]["on_false"] = model_ref
        g.pop(LORA, None)

    if use_upscale:
        g[UPSCALE_LOADER]["inputs"]["model_name"] = UPSCALE_MODEL_NAME
        g[UPSCALE_IMAGE]["inputs"]["upscale_model"] = [UPSCALE_LOADER, 0]
        g[UPSCALE_IMAGE]["inputs"]["image"] = [VAE_DECODE, 0]
        g[CREATE_VIDEO]["inputs"]["images"] = [UPSCALE_IMAGE, 0]
    else:
        g[CREATE_VIDEO]["inputs"]["images"] = [VAE_DECODE, 0]
        g.pop(UPSCALE_LOADER, None)
        g.pop(UPSCALE_IMAGE, None)

    for idx, node_id in enumerate(IMAGES):
        ref_key = f"ref_images.ref_image_{idx}"
        if idx < len(image_names):
            g[node_id]["inputs"]["image"] = image_names[idx]
            g[SIZE]["inputs"][ref_key] = [node_id, 0]
        else:
            g[SIZE]["inputs"].pop(ref_key, None)
            g.pop(node_id, None)
    return g


def estimate_cny(seconds: float) -> float:
    return 0.0


def submit(g: dict, *, base_url: str | None = None, api_key: str | None = None) -> str:
    base, key = _resolve_endpoint(base_url, api_key)
    try:
        out = http.request(
            f"{base}/prompt",
            key,
            {"prompt": g, "client_id": "h3-ref-video"},
            timeout=90,
            tries=1,
            extra_headers=_headers(),
        )
    except http.HTTPResponseError as exc:
        if _is_ngrok_offline_error(exc):
            raise http.HTTPResponseError(
                exc.status_code,
                message("comfy_tunnel_offline", node=endpoint_label(base), detail=str(exc)),
            ) from exc
        if exc.status_code in NON_RETRYABLE_STATUSES:
            if any(tok in str(exc) for tok in ("LoadImage", "invalid_image", "image_not_found")):
                clear_upload_cache(base)
            raise SubmissionRejected(message("comfy_submission_rejected", reason=str(exc))) from exc
        raise
    if not isinstance(out, dict):
        raise RuntimeError(f"ComfyUI returned non-object response: {str(out)[:200]}")
    status = out.get("_http_status")
    node_errors = out.get("node_errors")
    if (isinstance(status, int) and status in NON_RETRYABLE_STATUSES) or out.get("error") or (isinstance(node_errors, dict) and node_errors):
        if (isinstance(node_errors, dict) and (set(node_errors.keys()) & set(IMAGES))) or any(
            tok in json.dumps(out, ensure_ascii=False) for tok in ("LoadImage", "invalid_image", "image_not_found")
        ):
            clear_upload_cache(base)
        detail = json.dumps(out, ensure_ascii=False)[:300]
        raise SubmissionRejected(message("comfy_submission_rejected", reason=detail))
    prompt_id = out.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise RuntimeError(message("comfy_submission_failed", reason=json.dumps(out, ensure_ascii=False)[:240]))
    return prompt_id.strip()


def submit_with_retry(
    g: dict,
    log,
    *,
    record_submission=None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Submit prompt to ComfyUI; never auto-retry ambiguous network failures."""
    if record_submission:
        record_submission(True)
    try:
        if base_url is None and api_key is None:
            return submit(g)
        return submit(g, base_url=base_url, api_key=api_key)
    except RuntimeError as exc:
        if (isinstance(exc, SubmissionRejected) or _is_tunnel_offline_error(exc)) and record_submission:
            record_submission(False)
        log(message("comfy_submission_failed", reason=exc))
        raise


def _extract_run_seconds(status_obj: dict) -> float | None:
    messages = status_obj.get("messages")
    if not isinstance(messages, list):
        return None
    start_ts = end_ts = None
    for item in messages:
        if not isinstance(item, (list, tuple)) or len(item) < 2 or not isinstance(item[1], dict):
            continue
        ts = item[1].get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        if item[0] == "execution_start" and start_ts is None:
            start_ts = float(ts)
        elif item[0] in ("execution_success", "execution_error"):
            end_ts = float(ts)
    if start_ts is not None and end_ts is not None and end_ts >= start_ts:
        return round((end_ts - start_ts) / 1000.0, 2)
    return None


def _extract_error(status_obj: dict) -> str:
    messages = status_obj.get("messages")
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and item[0] == "execution_error" and isinstance(item[1], dict):
                info = item[1]
                node_type = info.get("node_type") or info.get("node_id") or ""
                exc_type = info.get("exception_type") or "Error"
                exc_msg = str(info.get("exception_message") or "").strip()
                return f"{node_type} {exc_type}: {exc_msg}"[:300]
    return str(status_obj.get("status_str") or "ComfyUI execution error")[:300]


def _extract_media_item(outputs: dict) -> dict | None:
    ordered_nodes = []
    if SAVE in outputs and isinstance(outputs[SAVE], dict):
        ordered_nodes.append(outputs[SAVE])
    for nid, node_out in outputs.items():
        if nid != SAVE and isinstance(node_out, dict):
            ordered_nodes.append(node_out)
    fallback = None
    for node_out in ordered_nodes:
        for field in ("videos", "images", "gifs"):
            items = node_out.get(field)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or not item.get("filename"):
                    continue
                fname = str(item["filename"])
                if fname.lower().endswith(_VIDEO_EXTS):
                    return item
                if fallback is None:
                    fallback = item
    return fallback


def _task_in_queue_list(items, task: str) -> bool:
    return _queue_position(items, task) is not None


def _queue_position(items, task: str) -> int | None:
    if not isinstance(items, list):
        return None
    for idx, entry in enumerate(items, 1):
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and str(entry[1]) == task:
            return idx
        if isinstance(entry, dict) and str(entry.get("prompt_id") or "") == task:
            return idx
    return None


def live_progress(
    task: str,
    *,
    queued: bool = False,
    queue_position: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict:
    """Best-effort live ComfyUI stage/step/VRAM snapshot; never raises on older proxies or 404."""
    base, key = _resolve_endpoint(base_url, api_key)
    hdrs = _headers()
    info: dict = {
        "queued": bool(queued),
        "queue_position": queue_position if queued else None,
        "stage": "queued" if queued else "running",
        "node": None,
        "step": None,
        "max_steps": None,
        "vram_used_gb": None,
        "vram_total_gb": None,
        "endpoint": base,
        "node_label": endpoint_label(base),
    }
    if queued and queue_position is None:
        try:
            q = http.request(f"{base}/queue", key, timeout=6, tries=1, extra_headers=hdrs)
            if isinstance(q, dict):
                info["queue_position"] = _queue_position(q.get("queue_pending"), task)
        except Exception:
            pass
    if not queued:
        try:
            prog = http.request(f"{base}/h3_progress", key, timeout=6, tries=1, extra_headers=hdrs)
            if isinstance(prog, dict) and not (isinstance(prog.get("_http_status"), int) and prog["_http_status"] >= 400):
                if prog.get("prompt_id") == task and prog.get("status") == "running":
                    node = prog.get("node")
                    if node is not None:
                        node_str = str(node)
                        info["node"] = node_str
                        info["stage"] = NODE_STAGE_KEYS.get(node_str, "running")
                    val = prog.get("value")
                    mx = prog.get("max")
                    if isinstance(val, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
                        info["step"] = int(val)
                        info["max_steps"] = int(mx)
        except Exception:
            pass
    try:
        stats = http.request(f"{base}/system_stats", key, timeout=6, tries=1, extra_headers=hdrs)
        if isinstance(stats, dict) and isinstance(stats.get("devices"), list) and stats["devices"]:
            dev = stats["devices"][0]
            if isinstance(dev, dict):
                vt = dev.get("vram_total")
                vf = dev.get("vram_free")
                if isinstance(vt, (int, float)) and vt > 0 and isinstance(vf, (int, float)) and vf >= 0:
                    gib = float(1024 ** 3)
                    info["vram_total_gb"] = round(vt / gib, 1)
                    info["vram_used_gb"] = round(max(0.0, vt - vf) / gib, 1)
    except Exception:
        pass
    return info


def progress_payload(status: dict, *, elapsed_s: float | None = None, endpoint: str | None = None) -> dict:
    ep = str(endpoint or "").strip()
    payload = {
        "stage": status.get("stage") or ("queued" if status.get("queued") else "running"),
        "queued": bool(status.get("queued")),
        "queue_position": status.get("queue_position"),
        "node": status.get("node"),
        "step": status.get("step"),
        "max_steps": status.get("max_steps"),
        "vram_used_gb": status.get("vram_used_gb"),
        "vram_total_gb": status.get("vram_total_gb"),
        "updated_at": round(time.time(), 1),
    }
    if ep:
        payload["endpoint"] = ep
        payload["node_label"] = status.get("node_label") or endpoint_label(ep)
    if elapsed_s is not None:
        payload["elapsed_s"] = max(0, int(round(elapsed_s)))
    return payload


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def format_progress(status: dict, *, elapsed_s: float | None = None, include_node: bool = False) -> str:
    stage = status.get("stage") or ("queued" if status.get("queued") else "running")
    if stage == "queued" and status.get("queue_position"):
        label = message("comfy_progress_queued_pos", pos=status["queue_position"])
    else:
        key = f"comfy_stage_{stage}"
        known_stages = {"queued", "running", *NODE_STAGE_KEYS.values()}
        label = message(key) if stage in known_stages else message("comfy_stage_running")
    step = status.get("step")
    max_steps = status.get("max_steps")
    if isinstance(step, int) and isinstance(max_steps, int) and max_steps > 0:
        pct = min(100, max(0, int(round(step * 100 / max_steps))))
        label = f"{label} {step}/{max_steps} ({pct}%)"
    parts = [label]
    if include_node:
        node_label = status.get("node_label") or endpoint_label(status.get("endpoint") or "")
        if node_label:
            parts.append(message("comfy_progress_node", node=node_label))
    vu = status.get("vram_used_gb")
    vt = status.get("vram_total_gb")
    if isinstance(vu, (int, float)) and isinstance(vt, (int, float)) and vt > 0:
        parts.append(message("comfy_progress_vram", used=f"{vu:.1f}", total=f"{vt:.1f}"))
    if elapsed_s is not None:
        parts.append(message("comfy_progress_elapsed", elapsed=_format_elapsed(elapsed_s)))
    return " · ".join(parts)


_TRANSIENT_NETWORK_MARKERS = (
    "ERR_NGROK_",
    "ConnectionResetError",
    "Connection reset by peer",
    "RemoteDisconnected",
    "ConnectionAbortedError",
    "BrokenPipeError",
    "TimeoutError",
    "timed out",
    "URLError",
    "EOFError",
    ": 502:",
    ": 503:",
    ": 504:",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
)


def _is_transient_tunnel_error(exc: Exception) -> bool:
    if isinstance(exc, http.HTTPResponseError) and exc.status_code in (502, 503, 504):
        return True
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_NETWORK_MARKERS)


def has_task(task: str, *, base_url: str, api_key: str | None = None) -> bool:
    """Single-shot probe checking whether prompt_id exists in /history or /queue on a specific node."""
    base, key = _resolve_endpoint(base_url, api_key)
    hdrs = _headers()
    try:
        hist = http.request(f"{base}/history/{quote(task, safe='')}", key, timeout=10, tries=1, extra_headers=hdrs)
        if isinstance(hist, dict) and isinstance(hist.get(task), dict):
            return True
    except Exception:
        return False
    try:
        q = http.request(f"{base}/queue", key, timeout=10, tries=1, extra_headers=hdrs)
        if isinstance(q, dict) and (
            _task_in_queue_list(q.get("queue_running"), task)
            or _task_in_queue_list(q.get("queue_pending"), task)
        ):
            return True
    except Exception:
        return False
    return False


def poll(task: str, *, base_url: str | None = None, api_key: str | None = None) -> dict:
    """{state: running|done|failed, url, cny: 0.0, run_seconds, error}"""
    base, key = _resolve_endpoint(base_url, api_key)
    hdrs = _headers()
    try:
        hist = http.request(f"{base}/history/{quote(task, safe='')}", key, extra_headers=hdrs)
    except RuntimeError as exc:
        if _is_transient_tunnel_error(exc):
            with _MISSING_LOCK:
                blips = _TRANSIENT_COUNTS.get((base, task), 0) + 1
                if blips <= 3:
                    _TRANSIENT_COUNTS[(base, task)] = blips
                    return {"state": "running", "queued": False}
                _TRANSIENT_COUNTS.pop((base, task), None)
        raise
    if not isinstance(hist, dict):
        raise RuntimeError(f"ComfyUI /history returned invalid payload: {str(hist)[:200]}")
    http_status = hist.get("_http_status")
    if isinstance(http_status, int) and http_status >= 400 and http_status != 404:
        raise RuntimeError(f"ComfyUI poll HTTP {http_status}: {json.dumps(hist, ensure_ascii=False)[:240]}")

    entry = hist.get(task)
    if isinstance(entry, dict):
        with _MISSING_LOCK:
            _TRANSIENT_COUNTS.pop((base, task), None)
            _MISSING_COUNTS.pop((base, task), None)
        status_obj = entry.get("status") if isinstance(entry.get("status"), dict) else {}
        if status_obj.get("status_str") == "error":
            return {"state": "failed", "error": _extract_error(status_obj)}
        outputs = entry.get("outputs") if isinstance(entry.get("outputs"), dict) else {}
        media_item = _extract_media_item(outputs)
        if media_item is None:
            if status_obj.get("completed") is False:
                return {"state": "running", "queued": False}
            return {"state": "failed", "error": "ComfyUI completed without output video"}
        params = {"filename": str(media_item["filename"]), "type": str(media_item.get("type") or "output")}
        subfolder = str(media_item.get("subfolder") or "").strip("/")
        if subfolder:
            params["subfolder"] = subfolder
        return {
            "state": "done",
            "url": f"{base}/view?{urlencode(params)}",
            "cny": 0.0,
            "coins": 0.0,
            "money": 0.0,
            "run_seconds": _extract_run_seconds(status_obj),
        }

    try:
        q = http.request(f"{base}/queue", key, extra_headers=hdrs)
    except RuntimeError as exc:
        if _is_transient_tunnel_error(exc):
            with _MISSING_LOCK:
                blips = _TRANSIENT_COUNTS.get((base, task), 0) + 1
                if blips <= 3:
                    _TRANSIENT_COUNTS[(base, task)] = blips
                    return {"state": "running", "queued": False}
                _TRANSIENT_COUNTS.pop((base, task), None)
        raise
    if not isinstance(q, dict):
        raise RuntimeError(f"ComfyUI /queue returned invalid payload: {str(q)[:200]}")
    q_status = q.get("_http_status")
    if isinstance(q_status, int) and q_status >= 400:
        raise RuntimeError(f"ComfyUI queue HTTP {q_status}: {json.dumps(q, ensure_ascii=False)[:240]}")

    with _MISSING_LOCK:
        _TRANSIENT_COUNTS.pop((base, task), None)

    if _task_in_queue_list(q.get("queue_running"), task):
        with _MISSING_LOCK:
            _MISSING_COUNTS.pop((base, task), None)
        return {"state": "running", "queued": False}
    if _task_in_queue_list(q.get("queue_pending"), task):
        with _MISSING_LOCK:
            _MISSING_COUNTS.pop((base, task), None)
        return {"state": "running", "queued": True}

    with _MISSING_LOCK:
        misses = _MISSING_COUNTS.get((base, task), 0) + 1
        if misses >= 2:
            _MISSING_COUNTS.pop((base, task), None)
            return {"state": "failed", "error": message("comfy_prompt_lost", task=task), "prompt_lost": True}
        _MISSING_COUNTS[(base, task)] = misses
    return {"state": "running", "queued": False}


def download(url: str, out: Path, *, api_key: str | None = None) -> Path:
    raw_key = api_key.strip() if api_key is not None else config.comfy_key()
    return http.download(url, out, key=key_for_endpoint(url, raw_key), extra_headers=_headers())


def probe_endpoint(base_url: str, api_key: str | None = None, *, timeout: float = 12.0) -> dict:
    """Probe a single ComfyUI endpoint for health, GPU devices, and queue depth."""
    base, key = _resolve_endpoint(base_url, api_key)
    hdrs = _headers()
    label = endpoint_label(base)
    try:
        stats = http.request(f"{base}/system_stats", key, timeout=timeout, tries=1, extra_headers=hdrs)
    except http.HTTPResponseError as exc:
        if exc.status_code in (401, 403):
            return {
                "url": base,
                "label": label,
                "online": False,
                "auth_failed": True,
                "status_code": exc.status_code,
                "devices": [],
                "comfyui_version": None,
                "queue_running": 0,
                "queue_pending": 0,
            }
        return {
            "url": base,
            "label": label,
            "online": False,
            "auth_failed": False,
            "status_code": exc.status_code,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    except Exception:
        return {
            "url": base,
            "label": label,
            "online": False,
            "auth_failed": False,
            "status_code": None,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    if not isinstance(stats, dict):
        return {
            "url": base,
            "label": label,
            "online": False,
            "auth_failed": False,
            "status_code": None,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    st_code = stats.get("_http_status")
    if isinstance(st_code, int) and st_code in (401, 403):
        return {
            "url": base,
            "label": label,
            "online": False,
            "auth_failed": True,
            "status_code": st_code,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    if isinstance(st_code, int) and st_code >= 400:
        return {
            "url": base,
            "label": label,
            "online": False,
            "auth_failed": False,
            "status_code": st_code,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    queue_running = 0
    queue_pending = 0
    try:
        q = http.request(f"{base}/queue", key, timeout=min(timeout, 8.0), tries=1, extra_headers=hdrs)
        if isinstance(q, dict) and not (isinstance(q.get("_http_status"), int) and q["_http_status"] >= 400):
            queue_running = len(q.get("queue_running") or [])
            queue_pending = len(q.get("queue_pending") or [])
    except Exception:
        pass
    devices = [
        {"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
        for d in (stats.get("devices") or []) if isinstance(d, dict)
    ]
    return {
        "url": base,
        "label": label,
        "online": True,
        "auth_failed": False,
        "status_code": 200,
        "devices": devices,
        "comfyui_version": (stats.get("system") or {}).get("comfyui_version") if isinstance(stats.get("system"), dict) else None,
        "queue_running": queue_running,
        "queue_pending": queue_pending,
    }


class ComfyNodePool:
    """Thread-safe pool scheduling independent segments across idle healthy ComfyUI endpoints."""

    def __init__(self, urls: list[str], *, api_key: str | None = None, probe_fn=None, log=None):
        deduped = list(dict.fromkeys(config.validate_endpoint_url(u, "COMFYUI_URL") for u in urls if str(u).strip()))
        if not deduped:
            raise ValueError("ComfyNodePool requires at least one COMFYUI_URL")
        self.urls: list[str] = deduped
        self._order: list[str] = list(deduped)
        self._api_key = api_key
        self._probe_fn = probe_fn or (lambda u: probe_endpoint(u, api_key))
        self._log = log or (lambda _msg: None)
        self._cond = threading.Condition()
        self._busy: set[str] = set()
        self._inflight: dict[str, int] = {}
        self._unhealthy: dict[str, str] = {}
        self._probes: dict[str, dict] = {}
        self._rr_index = 0

    def probe_all(self, *, fail_if_all_offline: bool = True) -> list[dict]:
        """Probe all endpoints concurrently, prioritize idle online nodes, and isolate offline ones."""
        workers = max(1, min(8, len(self.urls)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {u: pool.submit(self._probe_fn, u) for u in self.urls}
            results = [futures[u].result() for u in self.urls]
        online_nodes: list[dict] = []
        with self._cond:
            for item in results:
                u = item["url"]
                self._probes[u] = item
                if item.get("online"):
                    self._unhealthy.pop(u, None)
                    online_nodes.append(item)
                else:
                    reason = (
                        message("comfy_node_reason_auth", code=item.get("status_code") or 401)
                        if item.get("auth_failed")
                        else message("comfy_node_reason_offline")
                    )
                    self._unhealthy[u] = reason
                    self._log(message("comfy_node_skipped", node=endpoint_label(u), reason=reason))
            # Prefer idle online nodes first while keeping configured order as tiebreaker.
            online_sorted = sorted(
                online_nodes,
                key=lambda item: (
                    int(item.get("queue_running") or 0) + int(item.get("queue_pending") or 0),
                    self.urls.index(item["url"]),
                ),
            )
            offline_urls = [u for u in self.urls if u in self._unhealthy]
            self._order = [item["url"] for item in online_sorted] + offline_urls
            self._cond.notify_all()
        if online_nodes:
            labels = ", ".join(endpoint_label(item["url"]) for item in online_sorted)
            self._log(message("comfy_pool_ready", online=len(online_nodes), total=len(self.urls), nodes=labels))
        elif fail_if_all_offline:
            raise NoHealthyNodeError(message("comfy_pool_all_offline", count=len(self.urls)))
        return results

    def healthy_urls(self) -> list[str]:
        with self._cond:
            return [u for u in self._order if u not in self._unhealthy]

    def healthy_count(self) -> int:
        return len(self.healthy_urls())

    def reserve_inflight(self, base_url: str) -> None:
        if not base_url:
            return
        with self._cond:
            self._inflight[base_url] = self._inflight.get(base_url, 0) + 1

    def release_inflight(self, base_url: str) -> None:
        if not base_url:
            return
        with self._cond:
            cur = self._inflight.get(base_url, 0)
            if cur <= 1:
                self._inflight.pop(base_url, None)
            else:
                self._inflight[base_url] = cur - 1
            self._cond.notify_all()

    def notify_all(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def acquire(self, *, exclude: set[str] | None = None, abort: threading.Event | None = None) -> str:
        excluded = set(exclude or ())
        with self._cond:
            while True:
                if abort is not None and abort.is_set():
                    raise DispatchAbortedError("Segment dispatch aborted")
                candidates = [u for u in self._order if u not in self._unhealthy and u not in excluded]
                if not candidates:
                    raise NoHealthyNodeError(message("comfy_pool_all_offline", count=len(self.urls)))
                n = len(candidates)
                for offset in range(n):
                    cand = candidates[(self._rr_index + offset) % n]
                    if cand not in self._busy and self._inflight.get(cand, 0) == 0:
                        self._busy.add(cand)
                        self._rr_index = (self._rr_index + offset + 1) % max(1, n)
                        return cand
                self._cond.wait(timeout=0.5 if abort is not None else None)

    def release(self, base_url: str) -> None:
        if not base_url:
            return
        with self._cond:
            self._busy.discard(base_url)
            self._cond.notify_all()

    def mark_unhealthy(self, base_url: str, reason: str = "") -> None:
        if not base_url:
            return
        should_log = False
        with self._cond:
            if base_url not in self._unhealthy:
                self._unhealthy[base_url] = reason or message("comfy_node_reason_offline")
                should_log = True
            self._busy.discard(base_url)
            self._cond.notify_all()
        if should_log:
            self._log(message("comfy_node_isolated", node=endpoint_label(base_url), reason=reason or message("comfy_node_reason_offline")))


def account() -> dict:
    urls = config.comfy_urls()
    if len(urls) == 1:
        base, key = _resolve_endpoint(urls[0])
        hdrs = _headers()
        stats = http.request(f"{base}/system_stats", key, timeout=20, tries=3, extra_headers=hdrs)
        q = http.request(f"{base}/queue", key, timeout=20, tries=3, extra_headers=hdrs)
        if any(isinstance(d.get("_http_status"), int) and d["_http_status"] >= 400 for d in (stats, q) if isinstance(d, dict)):
            raise RuntimeError(f"ComfyUI status check failed: {json.dumps(stats, ensure_ascii=False)[:200]}")
        devices = [
            {"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
            for d in (stats.get("devices") or []) if isinstance(d, dict)
        ]
        return {
            "provider": "comfyui",
            "url": base,
            "comfyui_version": (stats.get("system") or {}).get("comfyui_version") if isinstance(stats.get("system"), dict) else None,
            "devices": devices,
            "queue_running": len(q.get("queue_running") or []),
            "queue_pending": len(q.get("queue_pending") or []),
        }
    key = config.comfy_key()
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        futures = [pool.submit(probe_endpoint, u, key) for u in urls]
        nodes = [fut.result() for fut in futures]
    online_nodes = [n for n in nodes if n["online"]]
    if not online_nodes:
        raise RuntimeError(message("comfy_pool_all_offline", count=len(urls)))
    primary = online_nodes[0]
    return {
        "provider": "comfyui",
        "url": primary["url"],
        "urls": urls,
        "online_nodes": len(online_nodes),
        "total_nodes": len(nodes),
        "comfyui_version": primary.get("comfyui_version"),
        "devices": [d for n in online_nodes for d in (n.get("devices") or [])],
        "queue_running": sum(int(n.get("queue_running") or 0) for n in online_nodes),
        "queue_pending": sum(int(n.get("queue_pending") or 0) for n in online_nodes),
        "nodes": nodes,
    }
