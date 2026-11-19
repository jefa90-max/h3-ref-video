"""ComfyUI remote backend: MiniMax H3 reference-to-video workflow (workflows/comfy-h3-ref2va.json).

Uploads reference images with pure ASCII content-hash filenames (<sha256[:16]><ext>), builds a dynamic 1..9 image
API prompt graph with deterministic 768p dimensions and preset-locked step/LoRA pairs, submits to /prompt, polls
/history/{prompt_id} + /queue (failing fast if missing twice consecutively after an instance restart), and downloads
completed videos via /view with tunnel authentication headers.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from . import config, http, rh
from .i18n import message

SAVE, PROMPT, DURATION, LENGTH, SIZE, STEPS, SEED = "92", "138", "132", "131", "136", "124", "129"
LORA, ENABLE_LORA, FULL_STEPS, TURBO_STEPS, SWITCH_MODEL, SWITCH_STEPS = "145", "146", "143", "144", "141", "142"
IMAGES = ["137", "139", "147", "148", "149", "150", "151", "152", "153"]
NODE_TYPES = {
    SAVE: "SaveVideo",
    PROMPT: "PrimitiveStringMultiline",
    DURATION: "PrimitiveFloat",
    LENGTH: "ComfyMathExpression",
    SIZE: "MiniMaxH3ReferenceToVideo",
    STEPS: "BasicScheduler",
    SEED: "RandomNoise",
    LORA: "LoraLoaderModelOnly",
    ENABLE_LORA: "PrimitiveBoolean",
    FULL_STEPS: "PrimitiveInt",
    TURBO_STEPS: "PrimitiveInt",
    SWITCH_MODEL: "ComfySwitchNode",
    SWITCH_STEPS: "ComfySwitchNode",
    **{n: "LoadImage" for n in IMAGES},
}

SubmissionRejected = rh.SubmissionRejected
NON_RETRYABLE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}
_ASCII_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv")
_MISSING_LOCK = threading.Lock()
_MISSING_COUNTS: dict[tuple[str, str], int] = {}
_TRANSIENT_COUNTS: dict[tuple[str, str], int] = {}


def _headers() -> dict[str, str]:
    return {"ngrok-skip-browser-warning": "1", "User-Agent": "h3-ref-video/1.0"}


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


def upload(path: Path) -> str:
    """Upload an input image to ComfyUI /upload/image under an ASCII content-hash filename."""
    base = config.comfy_url()
    key = config.comfy_key()
    ascii_name = _ascii_hash_filename(path)
    out = http.upload(
        f"{base}/upload/image",
        key,
        path,
        {"type": "input", "overwrite": "true"},
        file_field_name="image",
        filename=ascii_name,
        extra_headers=_headers(),
    )
    status = out.get("_http_status")
    name = out.get("name") if isinstance(out, dict) else None
    if (isinstance(status, int) and status >= 400) or not isinstance(name, str) or not name.strip():
        detail = json.dumps(out, ensure_ascii=False)[:240]
        if key:
            detail = detail.replace(key, "[key]")
        raise RuntimeError(f"upload to ComfyUI failed: {detail}")
    subfolder = str(out.get("subfolder") or "").strip("/")
    return f"{subfolder}/{name.strip()}" if subfolder else name.strip()


def graph(prompt: str, image_names: list[str], seconds: float, aspect: str, seed: int, *, preset: str | None = None) -> dict:
    """Build a ComfyUI API prompt graph with dynamic 1..9 LoadImage inputs and preset-locked LoRA/steps."""
    if not 1 <= len(image_names) <= len(IMAGES):
        raise ValueError(f"H3 需要 1 到 {len(IMAGES)} 张参考图，这一段有 {len(image_names)} 张")
    if aspect not in config.COMFY_SIZES:
        raise ValueError(f"画幅只支持 {', '.join(config.COMFY_SIZES)}")
    preset_name = (preset if preset is not None else config.video_settings()["comfyui_preset"]).strip().lower()
    if preset_name not in config.COMFY_PRESETS:
        raise ValueError(f"COMFYUI_PRESET={preset_name!r} (4step | 8step | full20)")
    preset_cfg = config.COMFY_PRESETS[preset_name]

    g = base_graph()
    g[PROMPT]["inputs"]["value"] = prompt
    g[DURATION]["inputs"]["value"] = max(float(config.MIN_SECONDS), min(float(config.MAX_SECONDS), float(seconds)))
    g[SIZE]["inputs"]["width"], g[SIZE]["inputs"]["height"] = config.COMFY_SIZES[aspect]
    g[SEED]["inputs"]["noise_seed"] = int(seed)
    g[ENABLE_LORA]["inputs"]["value"] = bool(preset_cfg["enable_lora"])
    g[TURBO_STEPS]["inputs"]["value"] = int(preset_cfg["steps"])
    g[FULL_STEPS]["inputs"]["value"] = 20
    if preset_cfg["enable_lora"]:
        g[LORA]["inputs"]["lora_name"] = preset_cfg["lora_name"]
    else:
        g["126"]["inputs"]["model"] = ["127", 0]
        g[SWITCH_MODEL]["inputs"]["on_true"] = ["127", 0]
        g.pop(LORA, None)

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


def submit(g: dict) -> str:
    base = config.comfy_url()
    key = config.comfy_key()
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
        if exc.status_code in NON_RETRYABLE_STATUSES:
            raise SubmissionRejected(message("comfy_submission_rejected", reason=str(exc))) from exc
        raise
    if not isinstance(out, dict):
        raise RuntimeError(f"ComfyUI returned non-object response: {str(out)[:200]}")
    status = out.get("_http_status")
    node_errors = out.get("node_errors")
    if (isinstance(status, int) and status in NON_RETRYABLE_STATUSES) or out.get("error") or (isinstance(node_errors, dict) and node_errors):
        detail = json.dumps(out, ensure_ascii=False)[:300]
        raise SubmissionRejected(message("comfy_submission_rejected", reason=detail))
    prompt_id = out.get("prompt_id")
    if not isinstance(prompt_id, str) or not prompt_id.strip():
        raise RuntimeError(message("comfy_submission_failed", reason=json.dumps(out, ensure_ascii=False)[:240]))
    return prompt_id.strip()


def submit_with_retry(g: dict, log, *, record_submission=None) -> str:
    """Submit prompt to ComfyUI; never auto-retry ambiguous network failures."""
    if record_submission:
        record_submission(True)
    try:
        return submit(g)
    except RuntimeError as exc:
        if isinstance(exc, SubmissionRejected) and record_submission:
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
    if not isinstance(items, list):
        return False
    for entry in items:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2 and str(entry[1]) == task:
            return True
        if isinstance(entry, dict) and str(entry.get("prompt_id") or "") == task:
            return True
    return False


def _is_transient_tunnel_error(exc: http.HTTPResponseError) -> bool:
    return exc.status_code in (502, 503, 504) or "ERR_NGROK_" in str(exc)


def poll(task: str) -> dict:
    """{state: running|done|failed, url, cny: 0.0, run_seconds, error}"""
    base = config.comfy_url()
    key = config.comfy_key()
    hdrs = _headers()
    try:
        hist = http.request(f"{base}/history/{quote(task, safe='')}", key, extra_headers=hdrs)
    except http.HTTPResponseError as exc:
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

    with _MISSING_LOCK:
        _TRANSIENT_COUNTS.pop((base, task), None)

    entry = hist.get(task)
    if isinstance(entry, dict):
        with _MISSING_LOCK:
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
    except http.HTTPResponseError as exc:
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
            return {"state": "failed", "error": message("comfy_prompt_lost", task=task)}
        _MISSING_COUNTS[(base, task)] = misses
    return {"state": "running", "queued": False}


def download(url: str, out: Path) -> Path:
    key = config.comfy_key()
    return http.download(url, out, key=key, extra_headers=_headers())


def account() -> dict:
    base = config.comfy_url()
    key = config.comfy_key()
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
