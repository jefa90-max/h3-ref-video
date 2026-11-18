"""Remote Colab A100 bootstrap for ComfyUI + authenticated ngrok tunnel + idle auto-shutdown.

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
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(os.environ.get("COMFY_DRIVE_BASE", "/content/drive/MyDrive/MiniMax_ComfyUI"))
LOCAL = Path(os.environ.get("COMFY_LOCAL_DIR", "/content/ComfyUI_local"))
COMFY_HOST = "127.0.0.1"
COMFY_PORT = 8188
GATEWAY_PORT = 8189
SUPERVISOR_PID_FILE = Path("/tmp/comfy_supervisor.pid")
SUPERVISOR_LOG_FILE = Path("/tmp/comfy_supervisor.log")
COMFY_LOG_FILE = Path("/tmp/comfyui.log")

MODEL_FILES = [
    "diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "text_encoders/qwen3vl_32b_minimax_h3_bf16.safetensors",
    "vae/minimax_h3_video_vae_fp16.safetensors",
    "vae/minimax_h3_audio_vae_fp32.safetensors",
    "loras/minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors",
    "loras/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors",
]

PASSIVE_PROBE_PATHS = {"/system_stats", "/queue"}


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


def _idle_timeout_minutes() -> float:
    raw = _read_secret(".idle_timeout_min", "COLAB_IDLE_TIMEOUT_MIN", required=False) or "60"
    minutes = float(raw)
    if minutes <= 0:
        raise ValueError(f"COLAB_IDLE_TIMEOUT_MIN={raw!r} must be > 0")
    return minutes


def _http_status(url: str, *, headers: dict[str, str] | None = None, timeout: float = 4.0) -> int | None:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(256)
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
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
        return hmac.compare_digest(credential, expected_key)
    if scheme_low == "basic":
        try:
            decoded = base64.b64decode(credential, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        _, sep, password = decoded.partition(":")
        candidate = password if sep else decoded
        return hmac.compare_digest(candidate, expected_key)
    return False


def _queue_busy() -> bool:
    req = urllib.request.Request(f"http://{COMFY_HOST}:{COMFY_PORT}/queue", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        return bool(payload.get("queue_running") or payload.get("queue_pending"))
    except Exception:
        return False


def _unassign_colab_runtime(log) -> None:
    runtime_addr = os.environ.get("TBE_RUNTIME_ADDR", "").strip()
    if runtime_addr:
        req = urllib.request.Request(f"http://{runtime_addr}/unassign", data=b"", method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                log(f"[watchdog] POST /unassign status={resp.status}")
                if resp.status == 200:
                    return
        except Exception as exc:
            log(f"[watchdog] Direct /unassign failed: {type(exc).__name__}: {exc}")
    try:
        from google.colab import runtime  # type: ignore
        runtime.unassign()
        log("[watchdog] google.colab.runtime.unassign() invoked")
    except Exception as exc:
        log(f"[watchdog] runtime.unassign() fallback failed: {type(exc).__name__}: {exc}")


def run_supervisor(*, skip_comfy_check: bool = False) -> None:
    api_key = _read_secret(".comfyui_api_key", "COMFYUI_API_KEY")
    ngrok_token = _read_secret(".ngrok_token", "NGROK_AUTHTOKEN")
    public_url = _normalize_public_url(_read_secret(".ngrok_domain", "NGROK_URL"))
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

    HopByHop = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade",
    }

    class AuthReverseProxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _reject(self) -> None:
            body = b'{"error":"Unauthorized"}\n'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("WWW-Authenticate", 'Bearer realm="ComfyUI"')
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def _proxy(self) -> None:
            if not _check_authorization(self.headers.get("Authorization"), api_key):
                self._reject()
                return
            path_only = self.path.split("?", 1)[0]
            is_work = not (self.command == "GET" and path_only in PASSIVE_PROBE_PATHS)
            if is_work:
                touch_activity()
            content_len = int(self.headers.get("Content-Length") or 0)
            forward_headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in HopByHop and k.lower() not in ("host", "authorization")
            }
            forward_headers["Host"] = f"{COMFY_HOST}:{COMFY_PORT}"
            forward_headers["Connection"] = "close"
            conn = http.client.HTTPConnection(COMFY_HOST, COMFY_PORT, timeout=600)
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for k, v in forward_headers.items():
                    conn.putheader(k, v)
                conn.endheaders()
                remaining = content_len
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    conn.send(chunk)
                    remaining -= len(chunk)
                    if is_work:
                        touch_activity()
                upstream = conn.getresponse()
                resp_headers = [
                    (k, v) for k, v in upstream.getheaders()
                    if k.lower() not in HopByHop
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
                err = json.dumps({"error": f"Upstream error: {type(exc).__name__}"}).encode("utf-8")
                try:
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(err)
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

    server = ThreadingHTTPServer((COMFY_HOST, GATEWAY_PORT), AuthReverseProxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"[supervisor] Auth gateway listening on {COMFY_HOST}:{GATEWAY_PORT} -> {COMFY_HOST}:{COMFY_PORT}")

    from pyngrok import conf, installer  # type: ignore
    ngrok_bin = conf.get_default().ngrok_path
    if not os.path.isfile(ngrok_bin):
        installer.install_ngrok(ngrok_bin)

    def spawn_ngrok() -> subprocess.Popen:
        child_env = {**os.environ, "NGROK_AUTHTOKEN": ngrok_token}
        return subprocess.Popen(
            [ngrok_bin, "http", f"{COMFY_HOST}:{GATEWAY_PORT}", f"--url={public_url}", "--authtoken-from-env", "--log=stdout"],
            env=child_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )

    ngrok_proc = spawn_ngrok()
    log(f"[supervisor] Spawned ngrok pid={ngrok_proc.pid} for {public_url} (idle_timeout={idle_minutes:g}m)")

    def handle_term(signum, frame):
        ngrok_proc.terminate()
        server.shutdown()
        SUPERVISOR_PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_term)
    signal.signal(signal.SIGINT, handle_term)

    check_interval = min(30.0, max(1.0, idle_seconds / 4.0))
    while True:
        time.sleep(check_interval)
        if ngrok_proc.poll() is not None:
            if skip_comfy_check or _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
                log(f"[supervisor] ngrok exited with {ngrok_proc.returncode}; restarting...")
                ngrok_proc = spawn_ngrok()
        if not skip_comfy_check and _queue_busy():
            touch_activity()
            continue
        if idle_elapsed() >= idle_seconds:
            if not skip_comfy_check and _queue_busy():
                touch_activity()
                continue
            log(f"[watchdog] Idle for >= {idle_minutes:g} min with empty queue. Unassigning runtime...")
            ngrok_proc.terminate()
            _unassign_colab_runtime(log)
            break


def ensure_comfyui_code() -> None:
    if not (LOCAL / "main.py").exists():
        print("[start_colab] Cloning ComfyUI to local SSD...", flush=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/Comfy-Org/ComfyUI.git", str(LOCAL)],
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-r", str(LOCAL / "requirements.txt")],
            check=True,
        )
    ensure_pyngrok()


def ensure_pyngrok() -> None:
    try:
        import pyngrok  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pyngrok"], check=True)


def stage_models_to_local_ssd() -> None:
    src_root = BASE / "models"
    dst_root = LOCAL / "models"
    if dst_root.is_symlink():
        dst_root.unlink()
    dst_root.mkdir(parents=True, exist_ok=True)
    for rel in MODEL_FILES:
        src = src_root / rel
        dst = dst_root / rel
        if not src.exists():
            raise FileNotFoundError(f"Missing model in Google Drive: {src}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_size = src.stat().st_size
        if dst.exists() and dst.stat().st_size == src_size:
            print(f"[start_colab] Cached on local SSD: {rel}", flush=True)
            continue
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        print(f"[start_colab] Copying to local SSD ({src_size / (1024**3):.2f} GB): {rel}", flush=True)
        t0 = time.time()
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        print(f"[start_colab] Copied {rel} in {time.time() - t0:.1f}s", flush=True)


def link_persistent_dirs() -> None:
    for sub in ("user", "output", "input"):
        target = BASE / sub
        target.mkdir(parents=True, exist_ok=True)
        dst = LOCAL / sub
        if dst.is_symlink():
            dst.unlink()
        elif dst.exists():
            shutil.rmtree(dst)
        dst.symlink_to(target)


def start_comfyui() -> None:
    if _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
        print(f"[start_colab] ComfyUI already running on {COMFY_HOST}:{COMFY_PORT}.", flush=True)
        return
    subprocess.run(["pkill", "-f", f"main.py.*{COMFY_PORT}"], check=False)
    time.sleep(1)
    with COMFY_LOG_FILE.open("w", encoding="utf-8") as log_f:
        subprocess.Popen(
            [sys.executable, "main.py", "--listen", COMFY_HOST, "--port", str(COMFY_PORT)],
            cwd=str(LOCAL),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    for i in range(90):
        if _http_status(f"http://{COMFY_HOST}:{COMFY_PORT}/system_stats") == 200:
            print(f"[start_colab] ComfyUI UP in {i * 2}s", flush=True)
            return
        time.sleep(2)
    tail = COMFY_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-2500:] if COMFY_LOG_FILE.exists() else ""
    raise RuntimeError(f"ComfyUI failed to start:\n{tail}")


def ensure_supervisor_and_tunnel(*, skip_comfy_check: bool = False) -> str:
    api_key = _read_secret(".comfyui_api_key", "COMFYUI_API_KEY")
    _read_secret(".ngrok_token", "NGROK_AUTHTOKEN")
    public_url = _normalize_public_url(_read_secret(".ngrok_domain", "NGROK_URL"))
    auth_headers = {
        "Authorization": f"Bearer {api_key}",
        "ngrok-skip-browser-warning": "1",
        "User-Agent": "h3-ref-video-colab-bootstrap/1.0",
    }
    probe_target = f"{public_url}/system_stats"
    if (
        _pid_alive(SUPERVISOR_PID_FILE)
        and _http_status(f"http://{COMFY_HOST}:{GATEWAY_PORT}/system_stats") == 401
        and (skip_comfy_check or _http_status(probe_target, headers=auth_headers, timeout=6.0) == 200)
    ):
        print(f"[start_colab] Already online and auth-protected: {public_url}", flush=True)
        return public_url

    old_pid = _pid_alive(SUPERVISOR_PID_FILE)
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
        except OSError:
            pass
    subprocess.run(["pkill", "-f", f"ngrok http {COMFY_HOST}:{GATEWAY_PORT}"], check=False)
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

    expected_status = 502 if skip_comfy_check else 200
    for _ in range(30):
        unauth = _http_status(f"http://{COMFY_HOST}:{GATEWAY_PORT}/system_stats")
        authed = _http_status(probe_target, headers=auth_headers, timeout=6.0)
        if unauth == 401 and authed == expected_status:
            print(f"[start_colab] TUNNEL READY (AUTH PROTECTED): {public_url}", flush=True)
            return public_url
        time.sleep(2)
    tail = SUPERVISOR_LOG_FILE.read_text(encoding="utf-8", errors="replace")[-2000:] if SUPERVISOR_LOG_FILE.exists() else ""
    raise RuntimeError(f"Authenticated ngrok tunnel failed readiness check:\n{tail}")


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
        ensure_pyngrok()
        ensure_supervisor_and_tunnel(skip_comfy_check=args.skip_comfy_check)
        return

    ensure_comfyui_code()
    stage_models_to_local_ssd()
    link_persistent_dirs()
    start_comfyui()
    ensure_supervisor_and_tunnel()


if __name__ == "__main__":
    main()
