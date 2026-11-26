"""Remote Colab GPU bootstrap for ComfyUI + authenticated ngrok tunnel + idle auto-shutdown.

Secrets and domain configuration are read from private dotfiles in Google Drive
(/content/drive/MyDrive/MiniMax_ComfyUI/) so they never enter local CLI argv or
colab-cli execution history:
  - .ngrok_token       : ngrok authtoken
  - .comfyui_api_key   : Bearer / Basic secret required on all tunnel requests
  - .ngrok_domain      : static ngrok domain or https URL
  - .idle_timeout_min  : optional idle auto-unassign threshold in minutes (default: 60)
"""
from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import http.client
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

BASE = Path(os.environ.get("COMFY_DRIVE_BASE", "/content/drive/MyDrive/MiniMax_ComfyUI"))
LOCAL = Path(os.environ.get("COMFY_LOCAL_DIR", "/content/ComfyUI_local"))
COMFY_HOST = "127.0.0.1"
COMFY_PORT = 8188
COMFY_LAUNCH_FLAGS = ("--disable-dynamic-vram", "--disable-pinned-memory")
GATEWAY_PORT = 8189
SUPERVISOR_PID_FILE = Path("/tmp/comfy_supervisor.pid")
COMFY_PID_FILE = Path("/tmp/comfyui.pid")
SUPERVISOR_LOG_FILE = Path("/tmp/comfy_supervisor.log")
NGROK_LOG_FILE = Path("/tmp/ngrok.log")
CLOUDFLARED_LOG_FILE = Path("/tmp/cloudflared.log")
CLOUDFLARED_URL_FILE = Path("/tmp/cloudflared_url.txt")
CLOUDFLARED_BIN_LOCAL = Path("/tmp/cloudflared")
CLOUDFLARED_DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
TRYCLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com", re.IGNORECASE)
COMFY_LOG_FILE = Path("/tmp/comfyui.log")

LATENT_UPSCALER_MODEL_REL = "latent_upscale_models/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"
LATENT_UPSCALER_REPO_URL = "https://github.com/xmarre/Comfyui_Minimax_h3_latent_Upscaler-Plus.git"
LATENT_UPSCALER_NODE_DIR_NAME = "Comfyui_Minimax_h3_latent_Upscaler"

MODEL_FILES = [
    "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "vae/minimax_h3_video_vae_fp16.safetensors",
    "vae/minimax_h3_audio_vae_fp32.safetensors",
    "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
    "upscale_models/RealESRGAN_x2plus.pth",
    LATENT_UPSCALER_MODEL_REL,
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
]
AUTO_DOWNLOAD_MODELS = {
    "upscale_models/RealESRGAN_x2plus.pth": "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth",
    LATENT_UPSCALER_MODEL_REL: (
        "https://huggingface.co/LBH-123-AI/Minimax_h3_latent_Upscaler/resolve/main/"
        "minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"
    ),
    "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors": (
        "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/"
        "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors"
    ),
}
SAGE_REPO_URL = "https://github.com/thu-ml/SageAttention.git"
SAGE_TRITON_FALLBACK_SPEC = "sageattention==1.0.6"
SSD_HEADROOM_BYTES = 12 * (1024 ** 3)

PASSIVE_PROBE_PATHS = {"/system_stats", "/queue", "/h3_progress"}
COMFY_WS_CLIENT_ID = "h3-ref-video"


def _apply_comfy_ws_event(state: dict, msg: dict, *, now: float | None = None) -> None:
    """Fold a ComfyUI /ws JSON event into the latest execution progress snapshot."""
    if not isinstance(msg, dict):
        return
    event_type = msg.get("type")
    data = msg.get("data")
    if not isinstance(data, dict):
        return
    ts = time.time() if now is None else float(now)
    prompt_id = data.get("prompt_id")
    if event_type == "execution_start":
        state.clear()
        state.update(prompt_id=prompt_id, status="running", node=None, value=None, max=None, updated_at=ts)
    elif event_type == "executing":
        node = data.get("node")
        if node is None:
            if not prompt_id or state.get("prompt_id") == prompt_id:
                state.update(status="finished", node=None, updated_at=ts)
        else:
            prev_node = state.get("node")
            state.update(
                prompt_id=prompt_id or state.get("prompt_id"),
                status="running",
                node=str(node),
                updated_at=ts,
            )
            if prev_node != str(node):
                state["value"] = None
                state["max"] = None
    elif event_type == "progress":
        node = data.get("node")
        state.update(
            prompt_id=prompt_id or state.get("prompt_id"),
            status="running",
            value=data.get("value"),
            max=data.get("max"),
            updated_at=ts,
        )
        if node is not None:
            state["node"] = str(node)
    elif event_type == "progress_state":
        nodes = data.get("nodes")
        if isinstance(nodes, dict):
            running_item = None
            for nid, info in nodes.items():
                if isinstance(info, dict) and info.get("state") == "running":
                    running_item = (str(nid), info)
                    break
            if running_item is not None:
                nid, info = running_item
                state.update(
                    prompt_id=prompt_id or info.get("prompt_id") or state.get("prompt_id"),
                    status="running",
                    node=nid,
                    value=info.get("value"),
                    max=info.get("max"),
                    updated_at=ts,
                )
    elif event_type in ("execution_success", "execution_error", "execution_interrupted"):
        if not prompt_id or state.get("prompt_id") == prompt_id:
            state.update(status=event_type.removeprefix("execution_"), node=None, updated_at=ts)
    elif event_type == "status":
        status_info = data.get("status")
        exec_info = status_info.get("exec_info") if isinstance(status_info, dict) else None
        if isinstance(exec_info, dict) and exec_info.get("queue_remaining") == 0 and state.get("status") == "running":
            state.update(status="finished", node=None, updated_at=ts)


def _recv_exact(sock: socket.socket, rx_buf: bytearray, n: int) -> bytes:
    while len(rx_buf) < n:
        part = sock.recv(max(4096, n - len(rx_buf)))
        if not part:
            raise EOFError("WebSocket closed")
        rx_buf.extend(part)
    out = bytes(rx_buf[:n])
    del rx_buf[:n]
    return out


def _send_ws_frame(sock: socket.socket, opcode: int, payload: bytes = b"") -> None:
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(header) + masked)


def _start_comfy_progress_tracker(log):
    lock = threading.Lock()
    state: dict = {}

    def snapshot() -> dict:
        with lock:
            return dict(state)

    def run_loop() -> None:
        while True:
            sock = None
            try:
                sock = socket.create_connection((COMFY_HOST, COMFY_PORT), timeout=10.0)
                key = base64.b64encode(os.urandom(16)).decode("ascii")
                req = (
                    f"GET /ws?clientId={COMFY_WS_CLIENT_ID} HTTP/1.1\r\n"
                    f"Host: {COMFY_HOST}:{COMFY_PORT}\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode("ascii")
                sock.sendall(req)
                buf = b""
                while b"\r\n\r\n" not in buf and len(buf) < 16384:
                    part = sock.recv(4096)
                    if not part:
                        raise EOFError("Handshake closed")
                    buf += part
                head_bytes, rest = buf.split(b"\r\n\r\n", 1)
                head = head_bytes.decode("latin-1", errors="replace")
                if " 101 " not in head.splitlines()[0]:
                    raise RuntimeError(f"WS upgrade rejected: {head.splitlines()[0]}")
                rx_buf = bytearray(rest)
                sock.settimeout(30.0)
                frag_opcode = None
                frag_buf = bytearray()
                while True:
                    try:
                        hdr = _recv_exact(sock, rx_buf, 2)
                    except socket.timeout:
                        _send_ws_frame(sock, 0x9, b"hb")
                        continue
                    b1, b2 = hdr[0], hdr[1]
                    fin = (b1 & 0x80) != 0
                    opcode = b1 & 0x0F
                    masked = (b2 & 0x80) != 0
                    length = b2 & 0x7F
                    if length == 126:
                        length = struct.unpack("!H", _recv_exact(sock, rx_buf, 2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", _recv_exact(sock, rx_buf, 8))[0]
                    mask_key = _recv_exact(sock, rx_buf, 4) if masked else b""
                    payload = _recv_exact(sock, rx_buf, length) if length else b""
                    if masked:
                        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
                    if opcode == 0x8:
                        break
                    if opcode == 0x9:
                        _send_ws_frame(sock, 0xA, payload[:125])
                        continue
                    if opcode == 0xA:
                        continue
                    if opcode in (0x1, 0x2):
                        if fin:
                            if opcode == 0x1:
                                try:
                                    msg = json.loads(payload.decode("utf-8", errors="replace"))
                                except ValueError:
                                    continue
                                with lock:
                                    _apply_comfy_ws_event(state, msg)
                        else:
                            frag_opcode = opcode
                            frag_buf = bytearray(payload)
                    elif opcode == 0x0 and frag_opcode is not None:
                        frag_buf.extend(payload)
                        if fin:
                            if frag_opcode == 0x1:
                                try:
                                    msg = json.loads(frag_buf.decode("utf-8", errors="replace"))
                                except ValueError:
                                    pass
                                else:
                                    with lock:
                                        _apply_comfy_ws_event(state, msg)
                            frag_opcode = None
                            frag_buf.clear()
            except Exception:
                # Always drop stale running state on WS disconnect so a missed terminal event
                # during the reconnect window can never keep the idle watchdog busy forever.
                with lock:
                    if state.get("status") == "running":
                        state.clear()
                time.sleep(3.0)
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    threading.Thread(target=run_loop, daemon=True).start()
    return snapshot


def _read_secret(dotfile_name: str, env_name: str, *, required: bool = True) -> str:
    dotfile = BASE / dotfile_name
    value = ""
    if dotfile.is_file():
        value = dotfile.read_text(encoding="utf-8").strip()
    if not value:
        value = os.environ.get(env_name, "").strip()
    if required and not value:
        raise RuntimeError(f"Missing {dotfile} (or {env_name} env var)")
    return value


def _normalize_public_url(raw: str) -> str:
    cleaned = raw.strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned
    return cleaned


def _resolve_ngrok_domain() -> str:
    """Resolve optional static ngrok URL; 'ephemeral'/'auto' or blank requests an ephemeral tunnel."""
    override = os.environ.get("H3_NGROK_DOMAIN", "").strip()
    if override:
        if override.lower() in ("ephemeral", "auto", "random", "none"):
            return ""
        return _normalize_public_url(override)
    raw = _read_secret(".ngrok_domain", "NGROK_URL", required=False)
    if not raw or raw.lower() in ("ephemeral", "auto", "random"):
        return ""
    return _normalize_public_url(raw)


def _tunnel_provider() -> str:
    raw = os.environ.get("H3_TUNNEL_PROVIDER", "").strip().lower() or "ngrok"
    if raw not in ("ngrok", "cloudflared"):
        raise ValueError(f"H3_TUNNEL_PROVIDER={raw!r} (ngrok | cloudflared)")
    return raw


def _discover_cloudflared_public_url(
    *,
    log_file: Path | None = None,
    url_file: Path | None = None,
) -> str:
    log_path = CLOUDFLARED_LOG_FILE if log_file is None else log_file
    url_path = CLOUDFLARED_URL_FILE if url_file is None else url_file
    if url_path.is_file():
        saved = url_path.read_text(encoding="utf-8", errors="replace").strip().rstrip("/")
        if TRYCLOUDFLARE_URL_RE.fullmatch(saved):
            return saved
    if not log_path.is_file():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    matches = TRYCLOUDFLARE_URL_RE.findall(text)
    if not matches:
        return ""
    found = matches[-1].rstrip("/")
    try:
        url_path.write_text(found + "\n", encoding="utf-8")
    except OSError:
        pass
    return found


def _is_valid_elf_binary(path: Path, *, min_bytes: int = 1024 * 1024) -> bool:
    try:
        if not path.is_file() or path.stat().st_size < min_bytes:
            return False
        with path.open("rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def ensure_cloudflared() -> str:
    existing = shutil.which("cloudflared")
    if existing:
        return existing
    if _is_valid_elf_binary(CLOUDFLARED_BIN_LOCAL) and os.access(CLOUDFLARED_BIN_LOCAL, os.X_OK):
        return str(CLOUDFLARED_BIN_LOCAL)
    drive_cached = BASE / "bin" / "cloudflared"
    if _is_valid_elf_binary(drive_cached):
        tmp = CLOUDFLARED_BIN_LOCAL.with_suffix(".tmp")
        shutil.copyfile(drive_cached, tmp)
        tmp.chmod(0o755)
        tmp.replace(CLOUDFLARED_BIN_LOCAL)
        return str(CLOUDFLARED_BIN_LOCAL)
    print("[start_colab] Downloading cloudflared binary for ephemeral HTTPS tunnel...", flush=True)
    _download_file_atomic(CLOUDFLARED_DOWNLOAD_URL, CLOUDFLARED_BIN_LOCAL, "bin/cloudflared", description="cloudflared binary")
    if not _is_valid_elf_binary(CLOUDFLARED_BIN_LOCAL):
        CLOUDFLARED_BIN_LOCAL.unlink(missing_ok=True)
        raise RuntimeError("Downloaded cloudflared binary is invalid (not a Linux ELF executable)")
    CLOUDFLARED_BIN_LOCAL.chmod(0o755)
    if BASE.exists():
        tmp_drive = _unique_tmp_path(drive_cached)
        try:
            drive_cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(CLOUDFLARED_BIN_LOCAL, tmp_drive)
            tmp_drive.chmod(0o755)
            tmp_drive.replace(drive_cached)
        except OSError:
            tmp_drive.unlink(missing_ok=True)
    return str(CLOUDFLARED_BIN_LOCAL)


def _discover_ngrok_public_url(*, timeout: float = 3.0) -> str:
    """Read the active HTTPS public_url from ngrok's local inspection API (127.0.0.1:4040)."""
    req = urllib.request.Request("http://127.0.0.1:4040/api/tunnels", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return ""
    for item in payload.get("tunnels") or []:
        if isinstance(item, dict):
            pub = str(item.get("public_url") or "").strip().rstrip("/")
            if pub.startswith("https://"):
                return pub
    return ""


def _idle_timeout_minutes() -> float:
    raw = _read_secret(".idle_timeout_min", "COLAB_IDLE_TIMEOUT_MIN", required=False) or "60"
    minutes = float(raw)
    if minutes <= 0:
        raise ValueError(f"COLAB_IDLE_TIMEOUT_MIN={raw!r} must be > 0")
    return minutes


def _unique_tmp_path(dst: Path) -> Path:
    return dst.with_name(f"{dst.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def _url_origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    try:
        port = parts.port or default_port
    except ValueError:
        port = None
    return (scheme, (parts.hostname or "").lower(), port)


class _StrictProbeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_origin = _url_origin(req.full_url)
        new_origin = _url_origin(newurl)
        if new_origin[0] not in ("http", "https") or (old_origin[0] == "https" and new_origin[0] != "https"):
            raise urllib.error.URLError(f"Blocked unsafe redirect to {new_origin[0] or 'unknown'}")
        has_auth = any(k.lower() == "authorization" for k in (*req.headers, *req.unredirected_hdrs))
        if has_auth and new_origin != old_origin:
            raise urllib.error.URLError("Blocked cross-origin redirect on authenticated probe")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_ORIG_URLOPEN = urllib.request.urlopen
_PROBE_OPENER = urllib.request.build_opener(_StrictProbeRedirectHandler())


def _http_status(url: str, *, headers: dict[str, str] | None = None, timeout: float = 4.0) -> int | None:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    open_fn = _PROBE_OPENER.open if urllib.request.urlopen is _ORIG_URLOPEN else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            resp.read(256)
            return resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code
    except Exception:
        return None


def _pid_alive(pid_file: Path) -> int | None:
    if not pid_file.is_file():
        return None
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError):
        return None


def _check_authorization(header_value: str | None, expected_key: str) -> bool:
    if not header_value or not expected_key:
        return False
    scheme, _, credential = header_value.strip().partition(" ")
    scheme_low = scheme.lower()
    credential = credential.strip()
    if scheme_low == "bearer":
        return hmac.compare_digest(credential.encode("utf-8"), expected_key.encode("utf-8"))
    if scheme_low == "basic":
        try:
            decoded = base64.b64decode(credential, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        _, sep, password = decoded.partition(":")
        candidate = password if sep else decoded
        return hmac.compare_digest(candidate.encode("utf-8"), expected_key.encode("utf-8"))
    return False


def _comfy_process_alive() -> bool:
    if _pid_alive(COMFY_PID_FILE) is not None:
        return True
    try:
        res = subprocess.run(
            ["pgrep", "-f", f"main.py.*{COMFY_PORT}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return res.returncode == 0
    except OSError:
        return False


def _is_passive_probe(method: str, path: str) -> bool:
    if method.upper() not in ("GET", "HEAD"):
        return False
    path_only = path.split("?", 1)[0].rstrip("/") or "/"
    return path_only in PASSIVE_PROBE_PATHS


QUEUE_UNREACHABLE_GRACE_SEC = 10 * 60.0


def _queue_busy_on_error(progress_snapshot=None, *, queue_error_elapsed: float = 0.0) -> bool:
    snap = progress_snapshot() if callable(progress_snapshot) else progress_snapshot
    if isinstance(snap, dict) and snap.get("status") == "running":
        updated_at = snap.get("updated_at")
        if not isinstance(updated_at, (int, float)) or (time.time() - float(updated_at)) <= 120.0:
            return True
    if queue_error_elapsed <= QUEUE_UNREACHABLE_GRACE_SEC:
        return _comfy_process_alive()
    return False


def _queue_busy(progress_snapshot=None, *, queue_error_elapsed: float = 0.0) -> bool:
    req = urllib.request.Request(f"http://{COMFY_HOST}:{COMFY_PORT}/queue", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if isinstance(payload, dict):
            return bool(payload.get("queue_running") or payload.get("queue_pending"))
    except Exception:
        return _queue_busy_on_error(progress_snapshot, queue_error_elapsed=queue_error_elapsed)
    return False


def _unassign_colab_runtime(log) -> bool:
    runtime_addr = os.environ.get("TBE_RUNTIME_ADDR", "").strip()
    if runtime_addr:
        req = urllib.request.Request(
            f"http://{runtime_addr}/unassign",
            data=b"",
            headers={"X-Colab-Tunnel": "Google"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                log(f"[watchdog] POST /unassign status={resp.status}")
                if resp.status == 200:
                    return True
        except Exception as exc:
            log(f"[watchdog] Direct /unassign failed: {type(exc).__name__}: {exc}")
    try:
        from google.colab import runtime  # type: ignore
        runtime.unassign()
        log("[watchdog] google.colab.runtime.unassign() invoked")
        return True
    except Exception as exc:
        log(f"[watchdog] runtime.unassign() fallback failed: {type(exc).__name__}: {exc}")
    return False


_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def _make_auth_proxy_handler(
    api_key: str,
    progress_snapshot,
    touch_activity,
    *,
    comfy_host: str | None = None,
    comfy_port: int | None = None,
):
    target_host = COMFY_HOST if comfy_host is None else comfy_host
    target_port = COMFY_PORT if comfy_port is None else comfy_port

    class AuthReverseProxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send_json_error(self, status: int, message: str) -> None:
            self.close_connection = True
            body = (json.dumps({"error": message}) + "\n").encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _reject(self) -> None:
            self.close_connection = True
            body = b'{"error":"Unauthorized"}\n'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", 'Bearer realm="ComfyUI"')
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _read_chunked_body(self) -> bytes:
            buf = bytearray()
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    raise EOFError("Unexpected EOF reading chunk size")
                size_token = line.split(b";", 1)[0].strip()
                chunk_size = int(size_token, 16)
                if chunk_size < 0:
                    raise ValueError("Negative chunk size")
                if chunk_size == 0:
                    while True:
                        trailer = self.rfile.readline(65536)
                        if not trailer or trailer in (b"\r\n", b"\n"):
                            break
                    break
                remaining = chunk_size
                while remaining > 0:
                    part = self.rfile.read(min(65536, remaining))
                    if not part:
                        raise EOFError("Unexpected EOF reading chunk payload")
                    buf.extend(part)
                    remaining -= len(part)
                crlf = self.rfile.readline(16)
                if crlf not in (b"\r\n", b"\n"):
                    raise ValueError("Invalid chunk terminator")
            return bytes(buf)

        def _proxy(self) -> None:
            self.close_connection = True
            if not _check_authorization(self.headers.get("Authorization"), api_key):
                self._reject()
                return
            path_only = self.path.split("?", 1)[0].rstrip("/") or "/"
            if self.command == "GET" and path_only == "/h3_progress":
                body = json.dumps(progress_snapshot()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            is_work = not _is_passive_probe(self.command, self.path)
            if is_work:
                touch_activity()
            te_header = (self.headers.get("Transfer-Encoding") or "").lower()
            is_chunked = "chunked" in te_header
            raw_cl = self.headers.get("Content-Length")
            content_len = 0
            chunked_body: bytes | None = None
            if is_chunked:
                try:
                    chunked_body = self._read_chunked_body()
                except (ValueError, EOFError):
                    self._send_json_error(400, "Invalid chunked request body")
                    return
                content_len = len(chunked_body)
            elif raw_cl is not None:
                try:
                    content_len = int(raw_cl.strip())
                except ValueError:
                    self._send_json_error(400, "Invalid Content-Length")
                    return
                if content_len < 0:
                    self._send_json_error(400, "Invalid Content-Length")
                    return
            forward_headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in _HOP_BY_HOP_HEADERS and k.lower() not in ("host", "authorization", "content-length", "expect")
            }
            if chunked_body is not None or raw_cl is not None:
                forward_headers["Content-Length"] = str(content_len)
            forward_headers["Host"] = f"{target_host}:{target_port}"
            forward_headers["Connection"] = "close"
            conn = http.client.HTTPConnection(target_host, target_port, timeout=600)
            headers_sent = False
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in forward_headers.items():
                    conn.putheader(k, v)
                conn.endheaders()
                if chunked_body is not None:
                    if chunked_body:
                        conn.send(chunked_body)
                        if is_work:
                            touch_activity()
                else:
                    remaining = content_len
                    while remaining > 0:
                        chunk = self.rfile.read(min(65536, remaining))
                        if not chunk:
                            raise EOFError(f"Truncated request body ({remaining} bytes missing)")
                        conn.send(chunk)
                        remaining -= len(chunk)
                        if is_work:
                            touch_activity()
                upstream = conn.getresponse()
                resp_headers = [
                    (k, v) for k, v in upstream.getheaders()
                    if k.lower() not in _HOP_BY_HOP_HEADERS
                ]
                has_len = any(k.lower() == "content-length" for k, _ in resp_headers)
                buffered = None if has_len or self.command == "HEAD" else upstream.read()
                self.send_response(upstream.status, upstream.reason)
                for k, v in resp_headers:
                    self.send_header(k, v)
                if buffered is not None:
                    self.send_header("Content-Length", str(len(buffered)))
                self.send_header("Connection", "close")
                self.end_headers()
                headers_sent = True
                if self.command != "HEAD":
                    if buffered is not None:
                        if buffered:
                            self.wfile.write(buffered)
                    else:
                        while True:
                            data = upstream.read(65536)
                            if not data:
                                break
                            self.wfile.write(data)
                            if is_work:
                                touch_activity()
            except Exception as exc:
                if not headers_sent:
                    try:
                        self._send_json_error(502, f"Upstream error: {type(exc).__name__}")
                    except Exception:
                        pass
            finally:
                conn.close()

        do_GET = _proxy
        do_POST = _proxy
        do_PUT = _proxy
        do_DELETE = _proxy
        do_HEAD = _proxy
        do_OPTIONS = _proxy

    return AuthReverseProxy


def run_supervisor(*, skip_comfy_check: bool = False) -> None:
    provider = _tunnel_provider()
    api_key = _read_secret(".comfyui_api_key", "COMFYUI_API_KEY")
    ngrok_token = _read_secret(".ngrok_token", "NGROK_AUTHTOKEN") if provider == "ngrok" else ""
    public_url = _resolve_ngrok_domain() if provider == "ngrok" else ""
    idle_minutes = _idle_timeout_minutes()
    idle_seconds = idle_minutes * 60.0

    SUPERVISOR_PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    activity_lock = threading.Lock()
    last_active = time.monotonic()

    def touch_activity() -> None:
        nonlocal last_active
        with activity_lock:
            last_active = time.monotonic()

    def idle_elapsed() -> float:
        with activity_lock:
            return time.monotonic() - last_active

    def log(msg: str) -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {msg}", flush=True)

    progress_snapshot = _start_comfy_progress_tracker(log)
    AuthReverseProxy = _make_auth_proxy_handler(api_key, progress_snapshot, touch_activity)

    server = ThreadingHTTPServer((COMFY_HOST, GATEWAY_PORT), AuthReverseProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"[supervisor] Auth gateway listening on {COMFY_HOST}:{GATEWAY_PORT} -> {COMFY_HOST}:{COMFY_PORT}")

    if provider == "cloudflared":
        cloudflared_bin = ensure_cloudflared()

        def spawn_tunnel() -> subprocess.Popen:
            CLOUDFLARED_URL_FILE.unlink(missing_ok=True)
            with CLOUDFLARED_LOG_FILE.open("w", encoding="utf-8") as cf_log:
                return subprocess.Popen(
                    [
                        cloudflared_bin,
                        "tunnel",
                        "--no-autoupdate",
                        "--url",
                        f"http://{COMFY_HOST}:{GATEWAY_PORT}",
                    ],
                    env=dict(os.environ),
                    stdout=cf_log,
                    stderr=subprocess.STDOUT,
                )

        tunnel_proc = spawn_tunnel()
        log(f"[supervisor] Spawned cloudflared pid={tunnel_proc.pid} for ephemeral trycloudflare tunnel (idle_timeout={idle_minutes:g}m)")
    else:
        from pyngrok import conf, installer  # type: ignore
        ngrok_bin = conf.get_default().ngrok_path
        if not os.path.isfile(ngrok_bin):
            installer.install_ngrok(ngrok_bin)

        def spawn_tunnel() -> subprocess.Popen:
            child_env = {**os.environ, "NGROK_AUTHTOKEN": ngrok_token}
            cmd = [ngrok_bin, "http", f"{COMFY_HOST}:{GATEWAY_PORT}", "--log=stdout"]
            if public_url:
                cmd.insert(3, f"--url={public_url}")
            with NGROK_LOG_FILE.open("a", encoding="utf-8") as ngrok_log:
                return subprocess.Popen(
                    cmd,
                    env=child_env,
                    stdout=ngrok_log,
                    stderr=subprocess.STDOUT,
                )

        tunnel_proc = spawn_tunnel()
        log(f"[supervisor] Spawned ngrok pid={tunnel_proc.pid} for {public_url or 'ephemeral-domain'} (idle_timeout={idle_minutes:g}m)")

    my_pid = str(os.getpid())
    stop_requested = False

    def _cleanup_pid_file() -> None:
        try:
            if SUPERVISOR_PID_FILE.is_file() and SUPERVISOR_PID_FILE.read_text(encoding="utf-8").strip() == my_pid:
                SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        except OSError:
            pass

    def handle_term(signum, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    check_interval = min(30.0, max(1.0, idle_seconds / 4.0))
    queue_err_since: float | None = None

    def _check_queue_busy() -> bool:
        nonlocal queue_err_since
        req = urllib.request.Request(f"http://{COMFY_HOST}:{COMFY_PORT}/queue", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            queue_err_since = None
            if isinstance(payload, dict):
                return bool(payload.get("queue_running") or payload.get("queue_pending"))
            return False
        except Exception:
            now_m = time.monotonic()
            if queue_err_since is None:
                queue_err_since = now_m
            return _queue_busy_on_error(progress_snapshot, queue_error_elapsed=now_m - queue_err_since)

    def _sleep_interruptible(seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.25, remaining))
        return True

    try:
        while not _sleep_interruptible(check_interval):
            if tunnel_proc.poll() is not None:
                if skip_comfy_check or _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
                    log(f"[supervisor] {provider} exited with {tunnel_proc.returncode}; restarting...")
                    tunnel_proc = spawn_tunnel()
            if not skip_comfy_check and _check_queue_busy():
                touch_activity()
                continue
            if idle_elapsed() >= idle_seconds:
                if not skip_comfy_check and _check_queue_busy():
                    touch_activity()
                    continue
                log(f"[watchdog] Idle for >= {idle_minutes:g} min with empty queue. Unassigning runtime...")
                if _unassign_colab_runtime(log):
                    tunnel_proc.terminate()
                    break
                log("[watchdog] Unassign did not succeed yet; keeping supervisor alive to retry.")
    finally:
        try:
            tunnel_proc.terminate()
        except OSError:
            pass
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        _cleanup_pid_file()


def ensure_latent_upscaler_node() -> None:
    node_dir = LOCAL / "custom_nodes" / LATENT_UPSCALER_NODE_DIR_NAME
    if (node_dir / "__init__.py").is_file():
        return
    node_dir.parent.mkdir(parents=True, exist_ok=True)
    if node_dir.exists():
        shutil.rmtree(node_dir)
    print(f"[start_colab] Cloning {LATENT_UPSCALER_NODE_DIR_NAME} custom node...", flush=True)
    subprocess.run(
        ["git", "clone", "--depth", "1", LATENT_UPSCALER_REPO_URL, str(node_dir)],
        check=True,
    )


def ensure_comfyui_code() -> None:
    reqs_marker = LOCAL / ".h3_reqs_installed"
    if not (LOCAL / "main.py").exists():
        print("[start_colab] Cloning ComfyUI to local SSD...", flush=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/Comfy-Org/ComfyUI.git", str(LOCAL)],
            check=True,
        )
    ensure_latent_upscaler_node()
    if not reqs_marker.exists() and (LOCAL / "requirements.txt").is_file():
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(LOCAL / "requirements.txt")],
            check=True,
        )
        reqs_marker.write_text("ok\n", encoding="utf-8")
    if _tunnel_provider() == "cloudflared":
        ensure_cloudflared()
    else:
        ensure_pyngrok()


def ensure_pyngrok() -> None:
    try:
        import pyngrok  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyngrok"], check=True)


def _detect_torch_cuda_info() -> tuple[str, str, str] | None:
    """Return (torch_version, arch_dot, sm_arch) for GPU 0, or None if CUDA is unavailable."""
    try:
        import torch  # type: ignore

        if not torch.cuda.is_available() or torch.cuda.device_count() < 1:
            return None
        major, minor = torch.cuda.get_device_capability(0)
        return (str(getattr(torch, "__version__", "unknown")), f"{major}.{minor}", f"sm{major}{minor}")
    except Exception:
        return None


def _sage_wheel_cache_dir(torch_version: str, sm_arch: str) -> Path:
    py_tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    safe_torch = re.sub(r"[^A-Za-z0-9._+-]", "_", torch_version)
    return BASE / "wheels" / f"sageattention-{py_tag}-torch{safe_torch}-{sm_arch}"


def _purge_sageattention_modules() -> None:
    for name in [k for k in sys.modules if k == "sageattention" or k.startswith("sageattention.")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()


def _sageattention_usable(sm_arch: str | None) -> tuple[bool, str]:
    importlib.invalidate_caches()
    try:
        mod = importlib.import_module("sageattention")
    except Exception as exc:
        return False, f"import failed: {type(exc).__name__}: {exc}"
    if not callable(getattr(mod, "sageattn", None)):
        return False, "sageattention.sageattn not found"
    try:
        ver = importlib.metadata.version("sageattention")
    except Exception:
        ver = str(getattr(mod, "__version__", "") or "unknown")
    if sm_arch in ("sm120", "sm121"):
        if not callable(getattr(mod, "sageattn_qk_int8_pv_fp8_cuda", None)):
            return False, f"sageattention {ver} lacks {sm_arch} CUDA FP8 kernels (Triton V1 unsupported on Blackwell)"
        core_mod = sys.modules.get("sageattention.core")
        if core_mod is not None and hasattr(core_mod, "SM89_ENABLED"):
            if not core_mod.SM89_ENABLED:
                return False, f"sageattention {ver} compiled {sm_arch} CUDA extension is not enabled"
        else:
            try:
                importlib.import_module("sageattention._qattn_sm89")
            except Exception as exc:
                return False, f"sageattention {ver} missing compiled {sm_arch} CUDA extension ({type(exc).__name__}: {exc})"
    return True, f"sageattention {ver} ({sm_arch or 'default'})"


def _find_nvcc() -> str | None:
    found = shutil.which("nvcc")
    if found:
        return found
    for cand in ("/usr/local/cuda/bin/nvcc", "/usr/bin/nvcc"):
        if Path(cand).is_file():
            return cand
    return None


def ensure_sageattention() -> tuple[bool, str]:
    """Ensure a compatible SageAttention package is available; never raises on failure."""
    try:
        cuda_info = _detect_torch_cuda_info()
        if cuda_info is None:
            reason = "CUDA GPU unavailable"
            print(f"[start_colab] SageAttention skipped: {reason}", flush=True)
            return False, reason
        torch_ver, arch_dot, sm_arch = cuda_info
        major = int(arch_dot.split(".", 1)[0])
        if major < 8:
            reason = f"GPU {sm_arch} below compute capability 8.0"
            print(f"[start_colab] SageAttention skipped: {reason}", flush=True)
            return False, reason

        ok, detail = _sageattention_usable(sm_arch)
        if ok:
            print(f"[start_colab] SageAttention ready: {detail}", flush=True)
            return True, detail

        cache_dir = _sage_wheel_cache_dir(torch_ver, sm_arch)
        cached_wheels = sorted(cache_dir.glob("sageattention-*.whl"), reverse=True) if cache_dir.is_dir() else []
        for whl in cached_wheels:
            print(f"[start_colab] Installing cached SageAttention wheel: {whl.name}", flush=True)
            res = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", str(whl)],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if res.returncode == 0:
                _purge_sageattention_modules()
                ok, detail = _sageattention_usable(sm_arch)
                if ok:
                    print(f"[start_colab] SageAttention ready from Drive wheel cache: {detail}", flush=True)
                    return True, f"{detail} [cached wheel]"

        last_err = detail
        nvcc_path = _find_nvcc()
        if nvcc_path:
            if not shutil.which("ninja"):
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "ninja"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
            print(
                f"[start_colab] Building single-arch SageAttention wheel for {sm_arch} (torch {torch_ver}); "
                "will cache to Drive for instant future startups...",
                flush=True,
            )
            t0 = time.time()
            with tempfile.TemporaryDirectory(prefix="sage_wheel_") as tmp_dir:
                src_dir = Path(tmp_dir) / "src"
                whl_out_dir = Path(tmp_dir) / "wheels"
                whl_out_dir.mkdir(parents=True, exist_ok=True)
                clone_res = subprocess.run(
                    ["git", "clone", "--depth", "1", SAGE_REPO_URL, str(src_dir)],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if clone_res.returncode != 0:
                    res = clone_res
                    built_wheels = []
                else:
                    setup_py = src_dir / "setup.py"
                    if setup_py.is_file():
                        setup_text = setup_py.read_text(encoding="utf-8")
                        if "-std=c++17" in setup_text:
                            setup_py.write_text(setup_text.replace("-std=c++17", "-std=c++20"), encoding="utf-8")
                    build_env = {
                        **os.environ,
                        "TORCH_CUDA_ARCH_LIST": arch_dot,
                        "EXT_PARALLEL": "4",
                        "MAX_JOBS": "4",
                    }
                    if "CUDA_HOME" not in build_env and nvcc_path.endswith("/bin/nvcc"):
                        build_env["CUDA_HOME"] = str(Path(nvcc_path).parent.parent)
                    res = subprocess.run(
                        [
                            sys.executable, "-m", "pip", "wheel",
                            "--no-build-isolation", "--no-deps",
                            "-w", str(whl_out_dir), str(src_dir),
                        ],
                        env=build_env,
                        capture_output=True,
                        text=True,
                        timeout=900,
                        check=False,
                    )
                    built_wheels = sorted(whl_out_dir.glob("sageattention-*.whl")) if res.returncode == 0 else []
                if built_wheels:
                    built_whl = built_wheels[0]
                    if BASE.exists():
                        dst_whl = cache_dir / built_whl.name
                        tmp_dst = _unique_tmp_path(dst_whl)
                        try:
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(built_whl, tmp_dst)
                            tmp_dst.replace(dst_whl)
                            print(f"[start_colab] Cached SageAttention wheel to Drive: {dst_whl.name}", flush=True)
                        except OSError as cache_exc:
                            tmp_dst.unlink(missing_ok=True)
                            print(f"[start_colab] Warning: failed to cache SageAttention wheel: {cache_exc}", flush=True)
                    inst = subprocess.run(
                        [sys.executable, "-m", "pip", "install", "-q", "--no-deps", "--force-reinstall", str(built_whl)],
                        capture_output=True,
                        text=True,
                        timeout=180,
                        check=False,
                    )
                    if inst.returncode == 0:
                        _purge_sageattention_modules()
                        ok, detail = _sageattention_usable(sm_arch)
                        if ok:
                            print(f"[start_colab] SageAttention built & ready in {time.time() - t0:.1f}s: {detail}", flush=True)
                            return True, f"{detail} [built]"
                        last_err = detail
                    else:
                        last_err = (inst.stderr or inst.stdout or f"pip install exit {inst.returncode}").strip()[-240:]
                else:
                    last_err = (res.stderr or res.stdout or f"pip wheel exit {res.returncode}").strip()[-240:]
        else:
            last_err = "nvcc not found for CUDA build"

        if sm_arch not in ("sm120", "sm121"):
            print(f"[start_colab] Falling back to pure Triton {SAGE_TRITON_FALLBACK_SPEC} on {sm_arch}...", flush=True)
            fb = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "--no-deps", SAGE_TRITON_FALLBACK_SPEC],
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if fb.returncode == 0:
                _purge_sageattention_modules()
                ok, detail = _sageattention_usable(sm_arch)
                if ok:
                    print(f"[start_colab] SageAttention ready via Triton fallback: {detail}", flush=True)
                    return True, f"{detail} [triton fallback]"
                last_err = detail
            else:
                last_err = (fb.stderr or fb.stdout or f"pip fallback exit {fb.returncode}").strip()[-240:]

        print(f"[start_colab] SageAttention unavailable ({last_err}); continuing with default attention.", flush=True)
        return False, last_err
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        print(f"[start_colab] SageAttention setup error ({reason}); continuing with default attention.", flush=True)
        return False, reason


def _parallel_copy_file(src: Path, dst: Path, src_size: int, rel: str) -> None:
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.unlink(missing_ok=True)
    total_gb = src_size / (1024 ** 3)
    print(f"[start_colab] Copying to local SSD ({total_gb:.2f} GB): {rel}", flush=True)
    t0 = time.time()
    if src_size == 0 or not hasattr(os, "pread") or not hasattr(os, "pwrite"):
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        print(f"[start_colab] Copied {rel} in {time.time() - t0:.1f}s", flush=True)
        return

    stripe = 32 * 1024 * 1024
    offsets = list(range(0, src_size, stripe))
    progress_lock = threading.Lock()
    copied = 0
    next_report = 8 * (1024 ** 3)
    src_fd = os.open(str(src), os.O_RDONLY)
    dst_fd = os.open(str(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o644)
    try:
        os.ftruncate(dst_fd, src_size)

        def copy_stripe(offset: int) -> None:
            nonlocal copied, next_report
            end = min(offset + stripe, src_size)
            pos = offset
            while pos < end:
                want = min(8 * 1024 * 1024, end - pos)
                chunk = b""
                for attempt in range(3):
                    try:
                        chunk = os.pread(src_fd, want, pos)
                        if chunk:
                            break
                        if attempt < 2:
                            time.sleep(2.0)
                    except OSError:
                        if attempt >= 2:
                            raise
                        time.sleep(2.0)
                if not chunk:
                    raise IOError(f"Unexpected EOF reading {src} at {pos}")
                written = 0
                while written < len(chunk):
                    written += os.pwrite(dst_fd, memoryview(chunk)[written:], pos + written)
                pos += len(chunk)
                with progress_lock:
                    copied += len(chunk)
                    if copied >= next_report and copied < src_size:
                        print(f"[start_colab]   progress {rel}: {copied / (1024 ** 3):.1f}/{total_gb:.1f} GB", flush=True)
                        next_report += 8 * (1024 ** 3)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for fut in [pool.submit(copy_stripe, off) for off in offsets]:
                fut.result()
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        os.close(src_fd)
        os.close(dst_fd)
    tmp.replace(dst)
    print(f"[start_colab] Copied {rel} in {time.time() - t0:.1f}s", flush=True)


def _download_file_atomic(url: str, dst: Path, rel: str, *, description: str = "missing model to Drive") -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp_path(dst)
    print(f"[start_colab] Downloading {description}: {rel}", flush=True)
    t0 = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "h3-ref-video-colab-bootstrap/1.0"}, method="GET")
    total_bytes = 0
    expected_bytes: int | None = None
    next_report = 1024 ** 3
    try:
        with urllib.request.urlopen(req, timeout=180.0) as resp, tmp.open("wb") as out_f:
            raw_len = resp.headers.get("Content-Length") if getattr(resp, "headers", None) else None
            if raw_len and str(raw_len).strip().isdigit():
                expected_bytes = int(str(raw_len).strip())
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out_f.write(chunk)
                total_bytes += len(chunk)
                if total_bytes >= next_report:
                    if expected_bytes:
                        print(
                            f"[start_colab]   downloading {rel}: "
                            f"{total_bytes / (1024 ** 3):.1f}/{expected_bytes / (1024 ** 3):.1f} GB",
                            flush=True,
                        )
                    else:
                        print(
                            f"[start_colab]   downloading {rel}: {total_bytes / (1024 ** 3):.1f} GB",
                            flush=True,
                        )
                    next_report += 1024 ** 3
        if total_bytes == 0:
            raise IOError(f"Downloaded 0 bytes for {rel} from {url}")
        if expected_bytes is not None and total_bytes != expected_bytes:
            raise IOError(f"Incomplete download for {rel}: got {total_bytes} of {expected_bytes} bytes")
        tmp.replace(dst)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    print(f"[start_colab] Downloaded {rel} ({total_bytes / (1024 ** 2):.1f} MB in {time.time() - t0:.1f}s)", flush=True)


def stage_models_to_local_ssd() -> None:
    src_root = BASE / "models"
    dst_root = LOCAL / "models"
    if dst_root.is_symlink():
        dst_root.unlink()
    dst_root.mkdir(parents=True, exist_ok=True)
    max_stage_gb = float(os.environ.get("COLAB_MAX_STAGE_GB", "80").strip() or "80")
    max_stage_bytes = int(max_stage_gb * (1024 ** 3))
    for rel in MODEL_FILES:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists() or src.stat().st_size == 0:
            download_url = AUTO_DOWNLOAD_MODELS.get(rel)
            if download_url:
                _download_file_atomic(download_url, src, rel)
            else:
                raise FileNotFoundError(f"Missing model in Google Drive: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.with_suffix(dst.suffix + ".tmp").unlink(missing_ok=True)
        src_size = src.stat().st_size
        if dst.exists() and not dst.is_symlink() and dst.stat().st_size == src_size:
            print(f"[start_colab] Cached on local SSD: {rel}", flush=True)
            continue
        free_bytes = shutil.disk_usage(str(dst.parent)).free
        if src_size > max_stage_bytes or free_bytes < src_size + SSD_HEADROOM_BYTES:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(src)
            reason = (
                f"size {src_size / (1024 ** 3):.1f} GB > {max_stage_gb:g} GB cap"
                if src_size > max_stage_bytes
                else f"low SSD headroom ({free_bytes / (1024 ** 3):.1f} GB free)"
            )
            print(f"[start_colab] Symlinked to Drive ({reason}): {rel}", flush=True)
            continue
        if dst.is_symlink():
            dst.unlink()
        _parallel_copy_file(src, dst, src_size, rel)


def _merge_missing_tree(src_dir: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in src_dir.iterdir():
        dest = dst_dir / item.name
        if dest.is_symlink():
            continue
        if item.is_dir() and not item.is_symlink():
            if not dest.exists() or dest.is_dir():
                _merge_missing_tree(item, dest)
        else:
            if not dest.exists():
                try:
                    shutil.move(str(item), str(dest))
                except (FileExistsError, shutil.Error):
                    pass


def link_persistent_dirs() -> None:
    # Keep user settings on Drive, but isolate per-instance input/output on local SSD so
    # concurrent Colab runtimes never collide on shared Drive filenames or FUSE sync lag.
    user_target = BASE / "user"
    user_target.mkdir(parents=True, exist_ok=True)
    user_dst = LOCAL / "user"
    if user_dst.is_symlink():
        user_dst.unlink()
    elif user_dst.exists():
        if user_dst.is_dir():
            _merge_missing_tree(user_dst, user_target)
            shutil.rmtree(user_dst)
        else:
            user_dst.unlink()
    user_dst.symlink_to(user_target)
    for sub in ("input", "output"):
        local_dir = LOCAL / sub
        if local_dir.is_symlink():
            local_dir.unlink()
        local_dir.mkdir(parents=True, exist_ok=True)


def start_comfyui(*, use_sage_attention: bool | None = None, sage_detail: str = "") -> None:
    if _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
        print(f"[start_colab] ComfyUI already running on {COMFY_HOST}:{COMFY_PORT}.", flush=True)
        return
    if use_sage_attention is None:
        cuda_info = _detect_torch_cuda_info()
        sm_arch = cuda_info[2] if cuda_info else None
        use_sage_attention, sage_detail = _sageattention_usable(sm_arch)
    launch_flags = list(COMFY_LAUNCH_FLAGS)
    if use_sage_attention:
        launch_flags.append("--use-sage-attention")
        print(f"[start_colab] SageAttention enabled (--use-sage-attention): {sage_detail or 'available'}", flush=True)
    else:
        print(f"[start_colab] SageAttention disabled (default attention): {sage_detail or 'not installed'}", flush=True)
    subprocess.run(["pkill", "-f", f"main.py.*{COMFY_PORT}"], check=False)
    time.sleep(1)
    with COMFY_LOG_FILE.open("w", encoding="utf-8") as log_f:
        proc = subprocess.Popen(
            [sys.executable, "main.py", "--listen", COMFY_HOST, "--port", str(COMFY_PORT), *launch_flags],
            cwd=str(LOCAL),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    if getattr(proc, "pid", None):
        COMFY_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    for i in range(90):
        if _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
            print(f"[start_colab] ComfyUI UP in {i * 2}s", flush=True)
            return
        exit_code = proc.poll() if hasattr(proc, "poll") else None
        if exit_code is not None:
            tail = COMFY_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-2500:] if COMFY_LOG_FILE.exists() else ""
            raise RuntimeError(f"ComfyUI exited early (code {exit_code}):\n{tail}")
        time.sleep(2)
    tail = COMFY_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-2500:] if COMFY_LOG_FILE.exists() else ""
    raise RuntimeError(f"ComfyUI failed to start:\n{tail}")


def ensure_supervisor_and_tunnel(*, skip_comfy_check: bool = False) -> str:
    provider = _tunnel_provider()
    api_key = _read_secret(".comfyui_api_key", "COMFYUI_API_KEY")
    ngrok_token = _read_secret(".ngrok_token", "NGROK_AUTHTOKEN") if provider == "ngrok" else ""
    public_url = _resolve_ngrok_domain() if provider == "ngrok" else ""
    unauth_headers = {
        "ngrok-skip-browser-warning": "1",
        "User-Agent": "h3-ref-video-colab-bootstrap/1.0",
    }
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        **unauth_headers,
    }
    expected_status = 502 if skip_comfy_check else 200
    if provider == "cloudflared":
        ensure_cloudflared()
        active_url = _discover_cloudflared_public_url()
    else:
        active_url = public_url or _discover_ngrok_public_url()
    if (
        active_url
        and _pid_alive(SUPERVISOR_PID_FILE)
        and _http_status(f"http://{COMFY_HOST}:{GATEWAY_PORT}/system_stats") == 401
        and _http_status(f"{active_url}/system_stats", headers=unauth_headers, timeout=6.0) == 401
        and _http_status(f"{active_url}/system_stats", headers=auth_headers, timeout=6.0) == expected_status
    ):
        print(f"[start_colab] Already online and auth-protected: {active_url}", flush=True)
        return active_url

    old_pid = _pid_alive(SUPERVISOR_PID_FILE)
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and _pid_alive(SUPERVISOR_PID_FILE) == old_pid:
            time.sleep(0.2)
        if _pid_alive(SUPERVISOR_PID_FILE) == old_pid:
            try:
                os.kill(old_pid, signal.SIGKILL)
            except OSError:
                pass
    if provider == "cloudflared":
        subprocess.run(["pkill", "-f", "cloudflared tunnel"], check=False)
        CLOUDFLARED_URL_FILE.unlink(missing_ok=True)
        CLOUDFLARED_LOG_FILE.unlink(missing_ok=True)
    else:
        subprocess.run(["pkill", "-f", "ngrok http "], check=False)
    time.sleep(0.5)

    cmd = [sys.executable, str(Path(__file__).resolve()), "--daemon-supervisor"]
    if skip_comfy_check:
        cmd.append("--skip-comfy-check")
    with SUPERVISOR_LOG_FILE.open("a", encoding="utf-8") as sup_log:
        subprocess.Popen(
            cmd,
            env=dict(os.environ),
            stdout=sup_log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    for _ in range(35 if provider == "cloudflared" else 30):
        unauth_local = _http_status(f"http://{COMFY_HOST}:{GATEWAY_PORT}/system_stats")
        resolved_url = (
            _discover_cloudflared_public_url()
            if provider == "cloudflared"
            else (public_url or _discover_ngrok_public_url())
        )
        unauth_public = (
            _http_status(f"{resolved_url}/system_stats", headers=unauth_headers, timeout=6.0)
            if resolved_url
            else None
        )
        authed = (
            _http_status(f"{resolved_url}/system_stats", headers=auth_headers, timeout=6.0)
            if resolved_url
            else None
        )
        if unauth_local == 401 and unauth_public == 401 and authed == expected_status:
            print(f"[start_colab] TUNNEL READY (AUTH PROTECTED): {resolved_url}", flush=True)
            return resolved_url
        time.sleep(2)
    sup_tail = SUPERVISOR_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-1200:] if SUPERVISOR_LOG_FILE.exists() else ""
    tun_log = CLOUDFLARED_LOG_FILE if provider == "cloudflared" else NGROK_LOG_FILE
    tun_tail = tun_log.read_text(encoding="utf-8", errors="replace")[-1200:] if tun_log.exists() else ""
    combined = f"{sup_tail}\n{tun_tail}".strip()
    for secret in (ngrok_token, api_key):
        if secret:
            combined = combined.replace(secret, "[REDACTED]")
    raise RuntimeError(f"Authenticated {provider} tunnel failed readiness check:\n{combined}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon-supervisor", action="store_true")
    parser.add_argument("--supervisor-only", action="store_true")
    parser.add_argument("--skip-comfy-check", action="store_true")
    args = parser.parse_args()

    if args.daemon_supervisor:
        run_supervisor(skip_comfy_check=args.skip_comfy_check)
        return

    if args.supervisor_only:
        if _tunnel_provider() == "cloudflared":
            ensure_cloudflared()
        else:
            ensure_pyngrok()
        ensure_supervisor_and_tunnel(skip_comfy_check=args.skip_comfy_check)
        return

    ensure_comfyui_code()
    sage_ok, sage_detail = ensure_sageattention()
    stage_models_to_local_ssd()
    link_persistent_dirs()
    start_comfyui(use_sage_attention=sage_ok, sage_detail=sage_detail)
    ensure_supervisor_and_tunnel()


if __name__ == "__main__":
    main()
