"""Cross-platform Colab GPU lifecycle controller for remote ComfyUI.

Decoupled from h3ref/comfy.py: machines without google-colab-cli still probe /system_stats
and run full video generation whenever the remote ComfyUI endpoint is online.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import comfy, config, http
from .i18n import message, use_lang

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_ORIG_SLEEP = time.sleep
DRIVE_AUTH_TIMEOUT_SEC = 600.0
CLI_AUTH_TIMEOUT_SEC = 600.0
CLI_AUTH_START_WAIT_SEC = 45.0
CLI_AUTH_SUBMIT_TIMEOUT_SEC = 60.0
SESSION_CACHE_TTL_SEC = 5.0
CRASH_GRACE_SEC = 1.5
DEFAULT_REQUEST_TIMEOUT_SEC = "90"
PROVISION_MAX_RETRIES = 5
PROVISION_RETRY_DELAYS_SEC = (20.0, 30.0, 45.0, 60.0, 60.0)
PROVISION_ATTEMPT_TIMEOUT_SEC = 120.0
# Colab reclaims headless/disconnected runtimes after ~20 min without a kernel client;
# keep heartbeats well below that threshold while remote or local workbench work is active,
# plus a bounded grace window shorter than start_colab.py's 60 min remote idle watchdog.
KERNEL_KEEPALIVE_INTERVAL_SEC = 8 * 60.0
KERNEL_KEEPALIVE_IDLE_GRACE_SEC = 45 * 60.0
KERNEL_KEEPALIVE_POLL_SEC = 30.0
KERNEL_KEEPALIVE_TIMEOUT_SEC = 30
KERNEL_KEEPALIVE_RETRY_COOLDOWN_SEC = 60.0
KERNEL_KEEPALIVE_CODE = 'print("keepalive")\n'
REMOTE_START_CODE = (
    "import os, subprocess, sys, time\n"
    "tmp_d = os.environ.get('H3_BOOT_TMP', '/tmp')\n"
    "script = os.environ.get('H3_START_COLAB_SCRIPT', '/content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py')\n"
    "pid_f = os.path.join(tmp_d, 'h3_boot.pid')\n"
    "log_f = os.path.join(tmp_d, 'h3_boot.log')\n"
    "rc_f = os.path.join(tmp_d, 'h3_boot.rc')\n"
    "def alive(p):\n"
    "    try:\n"
    "        os.kill(p, 0)\n"
    "    except OSError:\n"
    "        return False\n"
    "    if os.path.isdir('/proc'):\n"
    "        try:\n"
    "            raw = open(f'/proc/{p}/cmdline', 'rb').read()\n"
    "            return bool(raw) and b'h3_boot_runner' in raw\n"
    "        except OSError:\n"
    "            return False\n"
    "    return True\n"
    "try:\n"
    "    pid = int(open(pid_f, encoding='utf-8').read().strip()) if os.path.isfile(pid_f) else 0\n"
    "except ValueError:\n"
    "    pid = 0\n"
    "if not (pid and not os.path.isfile(rc_f) and alive(pid)):\n"
    "    for f in (log_f, rc_f):\n"
    "        try:\n"
    "            os.remove(f)\n"
    "        except OSError:\n"
    "            pass\n"
    "    runner = (\n"
    "        '# h3_boot_runner\\n'\n"
    "        'import os, subprocess, sys\\n'\n"
    "        f'with open({log_f!r}, \"a\", encoding=\"utf-8\") as out_f:\\n'\n"
    "        f'    child = subprocess.Popen([sys.executable, \"-u\", {script!r}], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)\\n'\n"
    "        '    for line in child.stdout:\\n'\n"
    "        '        out_f.write(line)\\n'\n"
    "        '        out_f.flush()\\n'\n"
    "        '    rc = child.wait()\\n'\n"
    "        f'tmp_rc = {rc_f!r} + \".tmp\"\\n'\n"
    "        'open(tmp_rc, \"w\", encoding=\"utf-8\").write(str(rc))\\n'\n"
    "        f'os.replace(tmp_rc, {rc_f!r})\\n'\n"
    "        'sys.exit(rc)\\n'\n"
    "    )\n"
    "    open(log_f, 'w', encoding='utf-8').close()\n"
    "    p = subprocess.Popen(\n"
    "        [sys.executable, '-u', '-c', runner],\n"
    "        stdout=subprocess.DEVNULL,\n"
    "        stderr=subprocess.DEVNULL,\n"
    "        stdin=subprocess.DEVNULL,\n"
    "        start_new_session=True,\n"
    "    )\n"
    "    open(pid_f, 'w', encoding='utf-8').write(str(p.pid))\n"
    "    pid = p.pid\n"
    "pos, last_beat = 0, time.monotonic()\n"
    "while True:\n"
    "    if os.path.isfile(log_f):\n"
    "        with open(log_f, 'r', encoding='utf-8', errors='replace') as lf:\n"
    "            lf.seek(pos)\n"
    "            chunk = lf.read()\n"
    "            pos = lf.tell()\n"
    "        if chunk:\n"
    "            print(chunk, end='', flush=True)\n"
    "            last_beat = time.monotonic()\n"
    "    if os.path.isfile(rc_f) or not alive(pid):\n"
    "        break\n"
    "    if time.monotonic() - last_beat >= 20:\n"
    "        print('[start_colab] bootstrap running...', flush=True)\n"
    "        last_beat = time.monotonic()\n"
    "    time.sleep(0.2)\n"
    "if os.path.isfile(log_f):\n"
    "    with open(log_f, 'r', encoding='utf-8', errors='replace') as lf:\n"
    "        lf.seek(pos)\n"
    "        rest = lf.read()\n"
    "    if rest:\n"
    "        print(rest, end='', flush=True)\n"
    "rc = int(open(rc_f, encoding='utf-8').read().strip()) if os.path.isfile(rc_f) else 1\n"
    "if rc != 0:\n"
    "    raise SystemExit(f'start_colab.py exited with {rc}')\n"
)

_TRANSIENT_WS_MARKERS = (
    "Connection was lost.",
    "Connection is already closed.",
    "WebSocketConnectionClosedException",
    "WebSocketTimeoutException",
    "ReadTimeout",
    "ConnectTimeout",
)

_STRIPPED_PROJECT_SECRETS = (
    "COMFYUI_API_KEY",
    "RUNNINGHUB_API_KEY",
    "APIMART_API_KEY",
    "LLM_API_KEY",
    "GEMINI_API_KEY",
    "VISION_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "NGROK_AUTHTOKEN",
)
_URL_RE = re.compile(r"(https://accounts\.google\.com/\S+)")
_OAUTH_TOKEN_RE = re.compile(
    r"(?:ya29\.[^\s\"'&,}\\)]+|1//[^\s\"'&,}\\)]+|(?i:Bearer)\s+[^\s\"',}]+)"
)
_OAUTH_SECRET_FIELD_RE = re.compile(
    r"(?i)(\b(?:access_token|refresh_token|client_secret|id_token)\b[\"']?\s*[:=]\s*[\"']?)[^\s\"',}&]+"
)
_CLI_AUTH_REQUIRED_MARKERS = (
    "To authorize colab-cli",
    "Enter the authorization code:",
)
_KEYRING_SAVE_WARNING_MARKER = "Could not save the OAuth token to the OS keyring"
_SESSION_LINE_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s+(?P<endpoint>\S+)\s+\|\s+Hardware:\s*(?P<hardware>[^|]+)")
_TUNNEL_READY_RE = re.compile(
    r"\[start_colab\]\s+(?:TUNNEL READY \(AUTH PROTECTED\)|Already online and auth-protected):\s*(https://\S+)"
)
_TRYCLOUDFLARE_URL_RE = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$", re.IGNORECASE)
MANAGED_SLOTS = ("primary", "secondary")


def normalize_slot(slot: str | None) -> str:
    cleaned = (slot or "primary").strip().lower()
    if cleaned not in MANAGED_SLOTS:
        raise ValueError(f"Unknown Colab runtime slot {slot!r} (primary | secondary)")
    return cleaned


def secondary_session_name(primary_session: str) -> str:
    base = (primary_session or "comfy-a100").strip() or "comfy-a100"
    return f"{base}-2"


def parse_tunnel_ready_url(output: str, *, ephemeral_only: bool = False) -> str:
    matches = _TUNNEL_READY_RE.findall(output or "")
    for candidate in reversed(matches):
        cleaned = candidate.strip().rstrip(".,;)")
        try:
            url = config.validate_endpoint_url(cleaned, "COMFYUI_URL")
            if url:
                if ephemeral_only and not _TRYCLOUDFLARE_URL_RE.fullmatch(url):
                    continue
                return url
        except ValueError:
            continue
    return ""


def merge_ephemeral_url(urls: list[str], old_url: str | None, new_url: str | None) -> list[str]:
    """Replace an old managed ephemeral URL with a new one (or remove it) while preserving static URLs."""
    old_clean = old_url.strip().rstrip("/") if old_url else ""
    new_clean = new_url.strip().rstrip("/") if new_url else ""
    result: list[str] = []
    seen: set[str] = set()
    replaced = False
    for raw in urls:
        u = raw.strip().rstrip("/")
        if not u:
            continue
        if old_clean and u == old_clean:
            if new_clean and new_clean not in seen:
                result.append(new_clean)
                seen.add(new_clean)
                replaced = True
            continue
        if u not in seen:
            result.append(u)
            seen.add(u)
    if new_clean and not replaced and new_clean not in seen:
        result.append(new_clean)
    return result


def _remote_start_code(tunnel_provider: str = "ngrok", *, script_path: str | None = None) -> str:
    prefix_lines: list[str] = []
    if tunnel_provider != "ngrok":
        prefix_lines.append(f"import os; os.environ['H3_TUNNEL_PROVIDER'] = {tunnel_provider!r}")
    if script_path:
        prefix_lines.append(f"import os; os.environ['H3_START_COLAB_SCRIPT'] = {script_path!r}")
    if not prefix_lines:
        return REMOTE_START_CODE
    return "\n".join(prefix_lines) + "\n" + REMOTE_START_CODE


class RuntimeBusyError(RuntimeError):
    """Another Colab lifecycle operation holds the runtime lock."""


class RuntimeUnavailableError(RuntimeError):
    """Colab CLI is unavailable or called from an disallowed worker context."""


@contextmanager
def runtime_file_lock(storage: Path, slot: str = "primary"):
    slot_name = normalize_slot(slot)
    storage.mkdir(parents=True, exist_ok=True)
    lock_name = "colab-runtime.lock" if slot_name == "primary" else f"colab-runtime-{slot_name}.lock"
    with (storage / lock_name).open("a+b") as lock:
        locked_nt = False
        locked_posix = False
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0, os.SEEK_END)
                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                locked_nt = True
            else:
                import fcntl
                lock.seek(0)
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked_posix = True
        except OSError as exc:
            raise RuntimeBusyError(message("colab_busy")) from exc
        try:
            yield
        finally:
            if locked_nt:
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            elif locked_posix:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def find_colab_bin(environ=None) -> str | None:
    values = os.environ if environ is None else environ
    explicit = values.get("COLAB_BIN", "").strip()
    if explicit:
        cand = Path(explicit).expanduser()
        if cand.is_file():
            return str(cand)
        resolved = shutil.which(explicit, path=values.get("PATH"))
        if resolved:
            return resolved
        return None

    candidates: list[Path] = []
    if os.name == "nt":
        candidates.append(config.ROOT / ".venv" / "Scripts" / "colab.exe")
        userprofile = values.get("USERPROFILE") or os.environ.get("USERPROFILE") or ""
        if userprofile:
            candidates.append(Path(userprofile) / ".local" / "bin" / "colab.exe")
        appdata = values.get("APPDATA") or os.environ.get("APPDATA") or ""
        if appdata:
            appdata_path = Path(appdata)
            candidates.append(appdata_path / "Python" / "Scripts" / "colab.exe")
            py_dir = appdata_path / "Python"
            if py_dir.is_dir():
                candidates.extend(sorted(py_dir.glob("Python*/Scripts/colab.exe"), reverse=True))
    else:
        candidates.append(config.ROOT / ".venv" / "bin" / "colab")
        home = values.get("HOME") or str(Path.home())
        candidates.append(Path(home) / ".local" / "bin" / "colab")

    for cand in candidates:
        if cand.is_file():
            return str(cand)
    return shutil.which("colab", path=values.get("PATH"))


def _build_argv(colab_bin: str, auth_mode: str, sub_args: list[str]) -> list[str]:
    prefix = [colab_bin]
    if auth_mode:
        prefix += ["--auth", auth_mode]
    return [*prefix, *sub_args]


def _new_session_args(session_name: str, gpu: str) -> list[str]:
    args = ["new", "-s", session_name, "--gpu", gpu]
    if gpu == "A100":
        args.append("--high-mem")
    return args


_NON_RETRYABLE_PROVISION_MARKERS = (
    "Backend rejected accelerator",
    "Allocation refused",
    "TooManyAssignmentsError",
    "HTTP 400",
    "HTTP 401",
    "HTTP 403",
    "HTTP 412",
    "400 Client Error",
    "401 Client Error",
    "403 Client Error",
    "412 Client Error",
)
_RETRYABLE_PROVISION_MARKERS = (
    "503",
    "502",
    "504",
    "Service Unavailable",
    "Bad Gateway",
    "Gateway Timeout",
    "ReadTimeout",
    "ConnectTimeout",
    "ConnectionError",
    "Connection was lost.",
    "RemoteDisconnected",
    "timed out",
)


def _strip_rich_traceback_frames(output: str) -> str:
    """Drop Rich traceback source-code box lines so `except ...` snippets never trigger error-marker checks."""
    kept = [
        line for line in (output or "").splitlines()
        if not line.lstrip().startswith(("│", "╭", "╰", "┃", "┏", "┗"))
    ]
    return "\n".join(kept).strip()


def _is_retryable_provision_failure(rc: int, output: str) -> bool:
    text = _strip_rich_traceback_frames(output)
    if any(marker in text for marker in _NON_RETRYABLE_PROVISION_MARKERS):
        return False
    if rc == -9:
        return True
    return any(marker in text for marker in _RETRYABLE_PROVISION_MARKERS)


def _summarize_provision_error(rc: int, output: str) -> str:
    text = _strip_rich_traceback_frames(output)
    if "503" in text or "Service Unavailable" in text:
        return "503 Service Unavailable"
    if "502" in text or "Bad Gateway" in text:
        return "502 Bad Gateway"
    if "504" in text or "Gateway Timeout" in text:
        return "504 Gateway Timeout"
    if "ReadTimeout" in text or "ConnectTimeout" in text or rc == -9:
        return f"timeout ({int(PROVISION_ATTEMPT_TIMEOUT_SEC)}s)"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines:
        return " ".join(lines[-3:])[-240:]
    return f"colab new exit {rc}"


def _is_transient_ws_disconnect(output: str) -> bool:
    return any(marker in output for marker in _TRANSIENT_WS_MARKERS)


def _is_terminal_cli_crash_line(
    line: str,
    saw_rich_box_end: bool,
    saw_traceback: bool = False,
) -> tuple[bool, bool, bool]:
    stripped = line.strip()
    if "Connection was lost." in stripped or "WebSocketConnectionClosedException" in stripped:
        return True, saw_rich_box_end, saw_traceback
    if "╭─" in stripped and "Traceback (most recent call last)" in stripped:
        return False, False, True
    if "╰─" in stripped and "╯" in stripped:
        return False, saw_traceback, saw_traceback
    if saw_rich_box_end and stripped:
        return True, False, False
    if saw_traceback and stripped and not stripped.startswith(("│", "╭", "╰")):
        saw_traceback = False
    return False, saw_rich_box_end, saw_traceback


def _popen_kwargs(environ=None, *, os_name: str | None = None) -> dict:
    active_os = os.name if os_name is None else os_name
    base_env = dict(os.environ if environ is None else environ)
    for secret_key in _STRIPPED_PROJECT_SECRETS:
        base_env.pop(secret_key, None)
    if not str(base_env.get("REQUEST_TIMEOUT", "")).strip():
        base_env["REQUEST_TIMEOUT"] = DEFAULT_REQUEST_TIMEOUT_SEC
    base_env.update(
        PYTHONUNBUFFERED="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    kwargs: dict = {
        "env": base_env,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if active_os == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _kill_process_tree(proc) -> None:
    if proc is None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=5.0,
                check=False,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _probe_json(url: str, key: str, *, timeout: float = 12.0) -> tuple[int | None, dict | None]:
    headers = {"User-Agent": "h3-ref-video/1.0", "ngrok-skip-browser-warning": "1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    open_fn = http._OPENER.open if urllib.request.urlopen is http._ORIG_URLOPEN else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return resp.status, payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        return code, None
    except Exception:
        return None, None


def _probe_single_node(url: str, shared_key: str, *, timeout: float = 12.0, include_queue: bool = False) -> dict:
    key = comfy.key_for_endpoint(url, shared_key)
    label = comfy.endpoint_label(url)
    status_code, payload = _probe_json(f"{url}/system_stats", key, timeout=timeout)
    if status_code == 200 and isinstance(payload, dict):
        devices = [
            {"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
            for d in (payload.get("devices") or []) if isinstance(d, dict)
        ]
        version = (payload.get("system") or {}).get("comfyui_version") if isinstance(payload.get("system"), dict) else None
        queue_running = 0
        queue_pending = 0
        if include_queue:
            q_code, q_payload = _probe_json(f"{url}/queue", key, timeout=min(timeout, 6.0))
            if q_code == 200 and isinstance(q_payload, dict):
                r_list = q_payload.get("queue_running") or []
                p_list = q_payload.get("queue_pending") or []
                queue_running = len(r_list) if isinstance(r_list, list) else 0
                queue_pending = len(p_list) if isinstance(p_list, list) else 0
        return {
            "url": url,
            "label": label,
            "online": True,
            "auth_failed": False,
            "status_code": 200,
            "devices": devices,
            "comfyui_version": version,
            "queue_running": queue_running,
            "queue_pending": queue_pending,
        }
    if status_code in (401, 403):
        return {
            "url": url,
            "label": label,
            "online": False,
            "auth_failed": True,
            "status_code": status_code,
            "devices": [],
            "comfyui_version": None,
            "queue_running": 0,
            "queue_pending": 0,
        }
    return {
        "url": url,
        "label": label,
        "online": False,
        "auth_failed": False,
        "status_code": status_code,
        "devices": [],
        "comfyui_version": None,
        "queue_running": 0,
        "queue_pending": 0,
    }


def probe_comfyui_nodes(environ=None, *, timeout: float = 12.0, include_queue: bool = False) -> list[dict]:
    settings = config.video_settings(environ)
    urls = settings["comfyui_urls"]
    if not urls:
        return []
    key = settings["comfyui_api_key"]
    if len(urls) == 1:
        return [_probe_single_node(urls[0], key, timeout=timeout, include_queue=include_queue)]
    workers = max(1, min(8, len(urls)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_probe_single_node, u, key, timeout=timeout, include_queue=include_queue) for u in urls]
        return [fut.result() for fut in futures]


def probe_comfyui(environ=None, *, timeout: float = 12.0) -> dict:
    settings = config.video_settings(environ)
    urls = settings["comfyui_urls"]
    if not urls:
        return {"online": False, "configured": False, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}
    nodes = probe_comfyui_nodes(environ, timeout=timeout, include_queue=len(urls) > 1)
    online_nodes = [n for n in nodes if n["online"]]
    if online_nodes:
        devices = [d for n in online_nodes for d in (n.get("devices") or [])]
        return {
            "online": True,
            "configured": True,
            "devices": devices,
            "comfyui_version": online_nodes[0].get("comfyui_version"),
            "nodes": nodes,
            "online_count": len(online_nodes),
            "total_count": len(nodes),
        }
    auth_node = next((n for n in nodes if n.get("auth_failed")), None)
    if auth_node is not None:
        return {
            "online": False,
            "configured": True,
            "auth_failed": True,
            "status_code": auth_node.get("status_code", 401),
            "devices": [],
            "nodes": nodes,
            "online_count": 0,
            "total_count": len(nodes),
        }
    return {
        "online": False,
        "configured": True,
        "devices": [],
        "nodes": nodes,
        "online_count": 0,
        "total_count": len(nodes),
    }


def probe_remote_queue_count(environ=None, *, timeout: float = 12.0, strict: bool = False) -> int | None:
    settings = config.video_settings(environ)
    urls = settings["comfyui_urls"]
    if not urls:
        return None if strict else 0
    shared_key = settings["comfyui_api_key"]

    def count_one(url: str) -> int | None:
        key = comfy.key_for_endpoint(url, shared_key)
        status_code, payload = _probe_json(f"{url}/queue", key, timeout=timeout)
        if status_code == 200 and isinstance(payload, dict):
            running = payload.get("queue_running")
            pending = payload.get("queue_pending")
            if strict and (not isinstance(running, list) or not isinstance(pending, list)):
                return None
            return (len(running) if isinstance(running, list) else 0) + (len(pending) if isinstance(pending, list) else 0)
        return None if strict else 0

    if len(urls) == 1:
        return count_one(urls[0])
    with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
        results = list(pool.map(count_one, urls))
    if strict and any(item is None for item in results):
        return None
    return sum(int(item or 0) for item in results)


def parse_sessions_output(output: str) -> dict:
    named: list[dict] = []
    orphans: list[dict] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        m = _SESSION_LINE_RE.match(line)
        if not m:
            continue
        item = {
            "name": m.group("name").strip(),
            "endpoint": m.group("endpoint").strip(),
            "hardware": m.group("hardware").strip(),
            "raw": line,
        }
        if item["name"] == "?":
            orphans.append(item)
        else:
            named.append(item)
    return {"named": named, "orphans": orphans}


def _is_cli_auth_required_output(output: str) -> bool:
    text = output or ""
    return any(marker in text for marker in _CLI_AUTH_REQUIRED_MARKERS)


class _CliAuthState:
    def __init__(self):
        self.auth_id: str = uuid.uuid4().hex[:12]
        self.active_state: str | None = None
        self.auth_url: str | None = None
        self.error: str | None = None
        self.keyring_warning: str | None = None
        self.proc = None
        self.thread: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.url_ready = threading.Event()
        self.code_submitted = threading.Event()
        self.done_event = threading.Event()
        self.authenticated = False
        self.already_authenticated = False
        self.submitted_code: str | None = None
        self.submit_timed_out = False


class _SlotState:
    def __init__(self, now: float):
        self.active_state: str | None = None
        self.auth_url: str | None = None
        self.last_error: str | None = None
        self.auth_required_error = False
        self.logs: list[str] = []
        self.drive_ack_event = threading.Event()
        self.cancel_event = threading.Event()
        self.drive_proc = None
        self.active_proc = None
        self.worker_thread: threading.Thread | None = None
        self.stopping = False
        self.starting = False
        self.last_keepalive_at = float(now)
        self.last_keepalive_fail_at: float | None = None
        self.keepalive_in_flight = False


class ColabRuntimeManager:
    def __init__(
        self,
        storage: Path,
        *,
        drive_auth_timeout: float = DRIVE_AUTH_TIMEOUT_SEC,
        cli_auth_timeout: float = CLI_AUTH_TIMEOUT_SEC,
        crash_grace_sec: float = CRASH_GRACE_SEC,
        retry_delay_sec: float = 4.0,
        provision_retries: int = PROVISION_MAX_RETRIES,
        provision_retry_delays: tuple[float, ...] = PROVISION_RETRY_DELAYS_SEC,
        keepalive_interval_sec: float = KERNEL_KEEPALIVE_INTERVAL_SEC,
        keepalive_idle_grace_sec: float = KERNEL_KEEPALIVE_IDLE_GRACE_SEC,
        keepalive_poll_sec: float = KERNEL_KEEPALIVE_POLL_SEC,
        keepalive_retry_cooldown_sec: float = KERNEL_KEEPALIVE_RETRY_COOLDOWN_SEC,
        clock=time.monotonic,
        on_ephemeral_url_change=None,
    ):
        self.storage = Path(storage)
        self.drive_auth_timeout = float(drive_auth_timeout)
        self.cli_auth_timeout = float(cli_auth_timeout)
        self.crash_grace_sec = float(crash_grace_sec)
        self.retry_delay_sec = float(retry_delay_sec)
        self.provision_retries = max(0, int(provision_retries))
        self.provision_retry_delays = tuple(float(d) for d in provision_retry_delays) or (20.0,)
        self.keepalive_interval_sec = float(keepalive_interval_sec)
        self.keepalive_idle_grace_sec = float(keepalive_idle_grace_sec)
        self.keepalive_poll_sec = float(keepalive_poll_sec)
        self.keepalive_retry_cooldown_sec = float(keepalive_retry_cooldown_sec)
        self._clock = clock
        self._on_ephemeral_url_change = on_ephemeral_url_change
        self._lock = threading.RLock()
        now_init = float(self._clock())
        self._slots: dict[str, _SlotState] = {
            "primary": _SlotState(now_init),
            "secondary": _SlotState(now_init),
        }
        self._cli_auth = _CliAuthState()
        self._cli_auth_starting = False
        self._cli_auth_gen = 0
        self._cli_auth_required_hint = False
        self._sessions_cache: tuple[float, dict] | None = None
        self._last_activity_at: float | None = None
        self._keepalive_stop = threading.Event()
        self._keepalive_env_provider = None
        self._local_busy_provider = None
        self._keepalive_thread: threading.Thread | None = None

    def _sleep_cancelable(self, st: _SlotState, seconds: float) -> bool:
        """Wait up to `seconds`, waking immediately on cancel_event (honors monkeypatched time.sleep in tests)."""
        if time.sleep is not _ORIG_SLEEP:
            time.sleep(seconds)
            return st.cancel_event.is_set()
        return st.cancel_event.wait(timeout=seconds)

    def _slot(self, slot: str = "primary") -> _SlotState:
        return self._slots[normalize_slot(slot)]

    # Primary-slot compatibility properties for callers/tests inspecting primary state directly.
    @property
    def _active_state(self):
        return self._slots["primary"].active_state

    @_active_state.setter
    def _active_state(self, val):
        self._slots["primary"].active_state = val

    @property
    def _auth_url(self):
        return self._slots["primary"].auth_url

    @_auth_url.setter
    def _auth_url(self, val):
        self._slots["primary"].auth_url = val

    @property
    def _last_error(self):
        return self._slots["primary"].last_error

    @_last_error.setter
    def _last_error(self, val):
        self._slots["primary"].last_error = val

    @property
    def _logs(self):
        return self._slots["primary"].logs

    @_logs.setter
    def _logs(self, val):
        self._slots["primary"].logs = val

    @property
    def _drive_ack_event(self):
        return self._slots["primary"].drive_ack_event

    @property
    def _cancel_event(self):
        return self._slots["primary"].cancel_event

    @property
    def _drive_proc(self):
        return self._slots["primary"].drive_proc

    @_drive_proc.setter
    def _drive_proc(self, val):
        self._slots["primary"].drive_proc = val

    @property
    def _active_proc(self):
        return self._slots["primary"].active_proc

    @_active_proc.setter
    def _active_proc(self, val):
        self._slots["primary"].active_proc = val

    @property
    def _worker_thread(self):
        return self._slots["primary"].worker_thread

    @_worker_thread.setter
    def _worker_thread(self, val):
        self._slots["primary"].worker_thread = val

    @property
    def _last_keepalive_at(self):
        return self._slots["primary"].last_keepalive_at

    @_last_keepalive_at.setter
    def _last_keepalive_at(self, val):
        self._slots["primary"].last_keepalive_at = val

    @property
    def _last_keepalive_fail_at(self):
        return self._slots["primary"].last_keepalive_fail_at

    @_last_keepalive_fail_at.setter
    def _last_keepalive_fail_at(self, val):
        self._slots["primary"].last_keepalive_fail_at = val

    @property
    def _keepalive_in_flight(self):
        return self._slots["primary"].keepalive_in_flight

    @_keepalive_in_flight.setter
    def _keepalive_in_flight(self, val):
        self._slots["primary"].keepalive_in_flight = val

    def set_url_change_callback(self, callback) -> None:
        with self._lock:
            self._on_ephemeral_url_change = callback

    def _nodes_state_path(self) -> Path:
        return self.storage / "colab-nodes.json"

    def _load_saved_nodes(self) -> dict:
        path = self._nodes_state_path()
        if not path.is_file():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save_nodes_state(self, data: dict) -> None:
        self.storage.mkdir(parents=True, exist_ok=True)
        path = self._nodes_state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        config.atomic_replace(tmp, path)

    @staticmethod
    def _node_key_matches(item: dict, api_key: str | None) -> bool:
        if api_key is None:
            return True
        saved_fp = str(item.get("key_fp") or "").strip()
        cur_fp = comfy.key_fingerprint(api_key)
        return bool(saved_fp) and cur_fp == saved_fp

    def remembered_node(self, slot: str = "secondary", *, api_key: str | None = None) -> dict | None:
        slot_name = normalize_slot(slot)
        with self._lock:
            item = self._load_saved_nodes().get(slot_name)
        if not (isinstance(item, dict) and item.get("url")):
            return None
        if not self._node_key_matches(item, api_key):
            return None
        return dict(item)

    def remembered_urls(self, *, api_key: str | None = None) -> list[str]:
        with self._lock:
            data = self._load_saved_nodes()
        urls: list[str] = []
        for slot_name in MANAGED_SLOTS:
            item = data.get(slot_name)
            if isinstance(item, dict) and item.get("url") and self._node_key_matches(item, api_key):
                try:
                    cleaned = config.validate_endpoint_url(str(item["url"]), "COMFYUI_URL")
                    if cleaned and cleaned not in urls:
                        urls.append(cleaned)
                except ValueError:
                    continue
        return urls

    @staticmethod
    def _is_stopping(st: _SlotState) -> bool:
        return bool(st.stopping or st.active_state == "stopping")

    def stopping_urls(self, environ=None) -> set[str]:
        """Return normalized ComfyUI endpoint URLs belonging to managed slots currently stopping."""
        env_copy = dict(os.environ if environ is None else environ)
        try:
            settings = config.video_settings(env_copy)
        except ValueError:
            return set()
        with self._lock:
            p_stopping = self._is_stopping(self._slots["primary"])
            s_stopping = self._is_stopping(self._slots["secondary"])
        if not p_stopping and not s_stopping:
            return set()
        saved_sec = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
        sec_url = str((saved_sec or {}).get("url") or "").strip().rstrip("/")
        urls: set[str] = set()
        if s_stopping and sec_url:
            urls.add(sec_url)
        if p_stopping:
            pri_url = self._primary_url(settings, sec_url)
            if pri_url:
                urls.add(pri_url)
        return urls

    def forget_remembered_node(self, slot: str = "secondary", *, expected_url: str | None = None) -> None:
        """Drop a saved node entry without triggering URL sync (used when /api/config removes it)."""
        slot_name = normalize_slot(slot)
        expected = expected_url.strip().rstrip("/") if expected_url is not None else None
        with self._lock:
            data = self._load_saved_nodes()
            prev = data.get(slot_name)
            if prev is not None:
                if expected is not None:
                    prev_url = str(prev.get("url") or "").strip().rstrip("/") if isinstance(prev, dict) else ""
                    if prev_url != expected:
                        return
                data.pop(slot_name, None)
                self._save_nodes_state(data)

    def _clear_remembered_node_if_match(self, slot: str, expected_url: str) -> bool:
        slot_name = normalize_slot(slot)
        expected = expected_url.strip().rstrip("/")
        if not expected:
            return False
        transitional = ("provisioning", "awaiting_drive_auth", "starting_comfy", "stopping")
        callback = None
        with self._lock:
            if self._is_stopping(self._slots[slot_name]) or self._slots[slot_name].active_state in transitional:
                return False
            data = self._load_saved_nodes()
            prev = data.get(slot_name)
            prev_url = str(prev.get("url") or "").strip().rstrip("/") if isinstance(prev, dict) else ""
            if prev_url != expected:
                return False
            data.pop(slot_name, None)
            self._save_nodes_state(data)
            callback = self._on_ephemeral_url_change
        if callback is not None:
            try:
                callback(slot_name, expected, None)
            except Exception as exc:
                self._append_log(f"[nodes] URL sync callback error: {type(exc).__name__}: {exc}", slot=slot_name)
        return True

    def _primary_url(self, settings: dict, sec_url: str | None = None) -> str:
        resolved_sec = (
            sec_url
            if sec_url is not None
            else str((self.remembered_node("secondary", api_key=settings["comfyui_api_key"]) or {}).get("url") or "").strip().rstrip("/")
        )
        for u in settings["comfyui_urls"]:
            if not resolved_sec or u != resolved_sec:
                return u
        return ""

    def _set_remembered_node(
        self,
        slot: str,
        record: dict | None,
        *,
        abort_if_cancelled: bool = False,
        replace_url: str | None = None,
    ) -> tuple[str | None, str | None]:
        slot_name = normalize_slot(slot)
        st = self._slot(slot_name)
        callback = None
        old_url: str | None = None
        new_url: str | None = None
        with self._lock:
            if abort_if_cancelled and st.cancel_event.is_set():
                return None, None
            data = self._load_saved_nodes()
            prev = data.get(slot_name)
            prev_url = str(prev.get("url") or "").strip().rstrip("/") if isinstance(prev, dict) else None
            explicit_replace = str(replace_url or "").strip().rstrip("/") or None
            old_url = explicit_replace or prev_url or None
            if record and record.get("url"):
                new_url = config.validate_endpoint_url(str(record["url"]), "COMFYUI_URL") or None
                data[slot_name] = {**record, "url": new_url}
            else:
                data.pop(slot_name, None)
                new_url = None
            self._save_nodes_state(data)
            callback = self._on_ephemeral_url_change
        if callback is not None and old_url != new_url:
            try:
                callback(slot_name, old_url, new_url)
            except Exception as exc:
                self._append_log(f"[nodes] URL sync callback error: {type(exc).__name__}: {exc}", slot=slot_name)
        return old_url, new_url

    def _logs_tail(self, slot: str = "primary") -> list[str]:
        st = self._slot(slot)
        with self._lock:
            return list(st.logs[-15:])

    def _append_log(self, line: str, *, slot: str = "primary") -> None:
        cleaned = line.rstrip("\r\n")
        if not cleaned:
            return
        st = self._slot(slot)
        with self._lock:
            st.logs.append(cleaned)
            if len(st.logs) > 80:
                st.logs = st.logs[-80:]

    def _run_command(self, colab_bin: str, auth_mode: str, sub_args: list[str], *,
                     environ=None, stdin_text: str | None = None, timeout: float = 60.0,
                     slot: str = "primary", record_slot: bool = True,
                     cancel_event: threading.Event | None = None) -> tuple[int, str]:
        st = self._slot(slot)
        argv = _build_argv(colab_bin, auth_mode, sub_args)
        kwargs = _popen_kwargs(environ)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        if record_slot:
            with self._lock:
                st.active_proc = proc
        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
        collected: list[str] = []
        cli_crashed = threading.Event()

        def reader():
            saw_rich_box_end = False
            saw_traceback = False
            try:
                for line in proc.stdout:
                    collected.append(line)
                    if record_slot:
                        self._append_log(line, slot=slot)
                    crashed, saw_rich_box_end, saw_traceback = _is_terminal_cli_crash_line(
                        line, saw_rich_box_end, saw_traceback
                    )
                    if crashed:
                        cli_crashed.set()
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        crash_deadline: float | None = None
        timed_out = False
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if (record_slot and st.cancel_event.is_set()) or (cancel_event is not None and cancel_event.is_set()):
                    _kill_process_tree(proc)
                    break
                now = time.monotonic()
                if cli_crashed.is_set():
                    if crash_deadline is None:
                        crash_deadline = now + self.crash_grace_sec
                    elif now >= crash_deadline:
                        _kill_process_tree(proc)
                        break
                if now >= deadline:
                    timed_out = True
                    _kill_process_tree(proc)
                    break
                time.sleep(0.02)
            rc = proc.wait(timeout=5.0)
            if timed_out and rc == 0:
                rc = -9
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            rc = -9
        finally:
            if record_slot:
                with self._lock:
                    if st.active_proc is proc:
                        st.active_proc = None
            t.join(timeout=2.0)
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        return rc, "".join(collected)

    def _query_sessions(
        self,
        colab_bin: str,
        auth_mode: str,
        environ=None,
        *,
        force: bool = False,
        cancel_slot: str | None = None,
    ) -> dict:
        now = time.monotonic()
        with self._lock:
            if not force and self._sessions_cache and (now - self._sessions_cache[0]) < SESSION_CACHE_TTL_SEC:
                return self._sessions_cache[1]
        if cancel_slot is not None:
            rc, out = self._run_command(
                colab_bin,
                auth_mode,
                ["sessions"],
                environ=environ,
                timeout=45.0,
                record_slot=False,
                cancel_event=self._slot(normalize_slot(cancel_slot)).cancel_event,
            )
        else:
            rc, out = self._run_command(colab_bin, auth_mode, ["sessions"], environ=environ, timeout=45.0, record_slot=False)
        if rc != 0:
            auth_required = auth_mode != "adc" and _is_cli_auth_required_output(out)
            res = {"named": [], "orphans": [], "ok": False, "auth_required": auth_required}
            if auth_required:
                with self._lock:
                    self._cli_auth_required_hint = True
                    self._sessions_cache = (time.monotonic(), res)
            elif auth_mode == "adc":
                with self._lock:
                    self._cli_auth_required_hint = False
            return res
        parsed = {**parse_sessions_output(out), "ok": True, "auth_required": False}
        with self._lock:
            self._cli_auth_required_hint = False
            self._sessions_cache = (time.monotonic(), parsed)
        return parsed

    def _session_name(self, settings: dict, slot: str = "primary") -> str:
        slot_name = normalize_slot(slot)
        return settings["colab_session"] if slot_name == "primary" else secondary_session_name(settings["colab_session"])

    def _tunnel_provider(self, slot: str = "primary") -> str:
        return "ngrok" if normalize_slot(slot) == "primary" else "cloudflared"

    def _run_cmd_for_slot(self, colab_bin: str, auth_mode: str, sub_args: list[str], *,
                          environ=None, stdin_text: str | None = None, timeout: float = 60.0,
                          slot: str = "primary") -> tuple[int, str]:
        if normalize_slot(slot) == "primary":
            return self._run_command(colab_bin, auth_mode, sub_args, environ=environ, stdin_text=stdin_text, timeout=timeout)
        return self._run_command(colab_bin, auth_mode, sub_args, environ=environ, stdin_text=stdin_text, timeout=timeout, slot=slot)

    def _probe_slot(self, env_copy: dict, settings: dict, slot: str, target_url: str | None = None, *, timeout: float | None = None) -> dict:
        slot_name = normalize_slot(slot)
        probe_kw = {"timeout": timeout} if timeout is not None else {}
        if slot_name == "primary":
            if target_url:
                return probe_comfyui({**env_copy, "COMFYUI_URL": target_url}, **probe_kw)
            primary_url = self._primary_url(settings)
            if not primary_url:
                return {"online": False, "configured": False, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}
            if len(settings["comfyui_urls"]) <= 1 and settings["comfyui_url"] == primary_url:
                return probe_comfyui(env_copy, **probe_kw)
            return probe_comfyui({**env_copy, "COMFYUI_URL": primary_url}, **probe_kw)
        url = target_url or (self.remembered_node("secondary", api_key=settings["comfyui_api_key"]) or {}).get("url") or ""
        if not url:
            return {"online": False, "configured": False, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}
        return probe_comfyui({**env_copy, "COMFYUI_URL": url}, **probe_kw)

    def status(self, environ=None) -> dict:
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        colab_bin = find_colab_bin(env_copy)
        cli_available = bool(colab_bin)
        probe = probe_comfyui(env_copy)
        probed_nodes = [dict(n) for n in (probe.get("nodes") or []) if isinstance(n, dict)]
        probed_by_url = {n["url"]: n for n in probed_nodes if n.get("url")}
        urls = list(settings["comfyui_urls"])

        saved_sec = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
        sec_url = str((saved_sec or {}).get("url") or "").strip().rstrip("/")
        if sec_url and sec_url not in probed_by_url:
            sec_extra = probe_comfyui({**env_copy, "COMFYUI_URL": sec_url})
            extra_nodes = [dict(n) for n in (sec_extra.get("nodes") or []) if isinstance(n, dict)]
            if extra_nodes:
                probed_by_url[sec_url] = extra_nodes[0]
                probed_nodes.append(extra_nodes[0])
            else:
                synthetic = {
                    "url": sec_url,
                    "label": comfy.endpoint_label(sec_url),
                    "online": bool(sec_extra.get("online")),
                    "auth_failed": bool(sec_extra.get("auth_failed")),
                    "status_code": sec_extra.get("status_code"),
                    "devices": sec_extra.get("devices") or [],
                    "comfyui_version": sec_extra.get("comfyui_version"),
                    "queue_running": 0,
                    "queue_pending": 0,
                }
                probed_by_url[sec_url] = synthetic
                probed_nodes.append(synthetic)
            if sec_url not in urls:
                urls.append(sec_url)

        transitional_states = ("provisioning", "awaiting_drive_auth", "starting_comfy", "stopping")
        with self._lock:
            p_slot = self._slots["primary"]
            s_slot = self._slots["secondary"]
            p_active = "stopping" if self._is_stopping(p_slot) else p_slot.active_state
            s_active = "stopping" if self._is_stopping(s_slot) else s_slot.active_state
            p_auth_url, p_error, p_logs = p_slot.auth_url, p_slot.last_error, list(p_slot.logs[-15:])
            s_auth_url, s_error, s_logs = s_slot.auth_url, s_slot.last_error, list(s_slot.logs[-15:])
            cli_auth_id = self._cli_auth.auth_id
            cli_auth_state = self._cli_auth.active_state
            cli_auth_url = self._cli_auth.auth_url
            cli_auth_error = self._cli_auth.error
            cli_auth_keyring_warning = self._cli_auth.keyring_warning
            cli_auth_hint_initial = self._cli_auth_required_hint

        primary_url = self._primary_url(settings, sec_url)
        p_node = probed_by_url.get(primary_url) if primary_url else None
        p_online = bool(p_node["online"]) if p_node is not None else (bool(probe.get("online")) and not sec_url)
        p_auth_failed = bool(p_node.get("auth_failed")) if p_node is not None else (bool(probe.get("auth_failed")) and not sec_url)
        s_node = probed_by_url.get(sec_url) if sec_url else None
        s_online = bool(s_node["online"]) if s_node is not None else False
        s_auth_failed = bool(s_node.get("auth_failed")) if s_node is not None else False

        is_adc = settings["colab_auth"] == "adc"
        if is_adc and cli_auth_hint_initial:
            with self._lock:
                self._cli_auth_required_hint = False
            cli_auth_hint_initial = False

        cli_auth_active = cli_auth_state in ("awaiting_cli_auth", "verifying_cli_auth")
        need_sessions = cli_available and not cli_auth_active and (
            (not probe.get("online") and not probe.get("auth_failed") and p_active not in transitional_states)
            or (bool(sec_url) and not s_online and not s_auth_failed and s_active not in transitional_states)
            or (bool(s_error) and not s_online and s_active not in transitional_states)
            or (bool(primary_url) and not p_online and p_active not in transitional_states and s_online)
            or (cli_auth_hint_initial and not is_adc and p_active not in transitional_states)
        )
        sessions = self._query_sessions(colab_bin, settings["colab_auth"], environ=env_copy) if need_sessions else {"named": [], "orphans": [], "ok": False, "auth_required": False}
        with self._lock:
            cli_auth_hint = self._cli_auth_required_hint
        cli_auth_required = (not is_adc) and (
            bool(sessions.get("auth_required")) or (cli_available and not cli_auth_active and cli_auth_hint)
        )
        cli_auth_info = {
            "state": cli_auth_state or ("required" if cli_auth_required else "idle"),
            "auth_id": cli_auth_id if cli_auth_active else None,
            "auth_url": cli_auth_url if cli_auth_state == "awaiting_cli_auth" else None,
            "error": cli_auth_error,
            "keyring_warning": cli_auth_keyring_warning,
        }
        primary_session = self._session_name(settings, "primary")
        secondary_session = self._session_name(settings, "secondary")
        has_primary_session = any(s["name"] == primary_session for s in sessions["named"])
        has_secondary_session = any(s["name"] == secondary_session for s in sessions["named"])

        # Drop dead remembered ephemeral URL only when CLI confirmed the secondary VM is gone (rc == 0)
        # and the slot is still non-transitional with the same URL (prevents TOCTOU wipe on transient errors).
        if (
            saved_sec
            and sec_url
            and need_sessions
            and s_active not in transitional_states
            and not s_online
            and not s_auth_failed
            and cli_available
            and sessions.get("ok") is True
            and not has_secondary_session
        ):
            if self._clear_remembered_node_if_match("secondary", sec_url):
                probed_by_url.pop(sec_url, None)
                probed_nodes = [n for n in probed_nodes if n.get("url") != sec_url]
                urls = [u for u in urls if u != sec_url]
                sec_url = ""
                s_node = None

        if p_online and p_active != "stopping":
            with self._lock:
                self._slots["primary"].last_error = None
                self._slots["primary"].auth_required_error = False
            p_error = None
        if s_online and s_active != "stopping":
            with self._lock:
                self._slots["secondary"].last_error = None
                self._slots["secondary"].auth_required_error = False
            s_error = None

        def _build_managed_entry(
            slot_name: str,
            session_name: str,
            node_url: str,
            node_probe: dict | None,
            online: bool,
            auth_failed: bool,
            active: str | None,
            auth_url: str | None,
            last_error: str | None,
            logs_tail: list[str],
            has_session: bool,
        ) -> dict:
            if active == "stopping":
                st = "stopping"
                online = False
                err = None
            elif online:
                st = "online"
                err = None
            elif active in transitional_states:
                st = active
                err = None
            elif auth_failed:
                st = "error"
                err = message("colab_auth_rejected", code=(node_probe or {}).get("status_code") or probe.get("status_code", 401))
            elif not cli_available:
                st = "cli_unavailable"
                err = last_error
            elif sessions["orphans"] and not has_session and slot_name == "primary":
                st = "orphan_warning"
                err = last_error
            elif last_error:
                st = "error"
                err = last_error
            else:
                st = "offline"
                err = None
            devices = list((node_probe or {}).get("devices") or [])
            if not devices and online and slot_name == "primary" and not node_probe:
                devices = list(probe.get("devices") or [])
            return {
                "slot": slot_name,
                "session": session_name,
                "tunnel": self._tunnel_provider(slot_name),
                "ephemeral": slot_name == "secondary",
                "url": node_url,
                "label": comfy.endpoint_label(node_url) if node_url else "",
                "state": st,
                "online": online,
                "auth_failed": auth_failed,
                "has_session": has_session,
                "gpu": settings["colab_gpu"],
                "devices": devices,
                "queue_running": int((node_probe or {}).get("queue_running") or 0),
                "queue_pending": int((node_probe or {}).get("queue_pending") or 0),
                "auth_url": auth_url if st == "awaiting_drive_auth" else None,
                "error": err,
                "logs": logs_tail,
            }

        managed_primary = _build_managed_entry(
            "primary", primary_session, primary_url, p_node, p_online, p_auth_failed,
            p_active, p_auth_url, p_error, p_logs, has_primary_session,
        )
        managed_secondary = _build_managed_entry(
            "secondary", secondary_session, sec_url, s_node, s_online, s_auth_failed,
            s_active, s_auth_url, s_error, s_logs, has_secondary_session,
        )
        managed_nodes = [managed_primary, managed_secondary]

        stopping_slot_urls = set()
        if p_active == "stopping" and primary_url:
            stopping_slot_urls.add(primary_url)
        if s_active == "stopping" and sec_url:
            stopping_slot_urls.add(sec_url)
        if stopping_slot_urls:
            for n in probed_nodes:
                if n.get("url") in stopping_slot_urls:
                    n["online"] = False
        online_nodes_list = [n for n in probed_nodes if n.get("online")]
        any_online = bool(online_nodes_list) or (
            bool(probe.get("online")) and not probed_nodes and p_active != "stopping" and s_active != "stopping"
        )
        online_count = len(online_nodes_list) if probed_nodes else (probe.get("online_count", 1 if probe.get("online") else 0))
        total_count = len(probed_nodes) if probed_nodes else (probe.get("total_count", len(urls)))
        node_meta = {
            "comfyui_urls": urls,
            "nodes": probed_nodes,
            "online_count": online_count,
            "total_count": total_count,
            "managed_nodes": managed_nodes,
            "cli_auth": cli_auth_info,
        }
        all_devices = [d for n in online_nodes_list for d in (n.get("devices") or [])] or list(probe.get("devices") or [])

        if any_online:
            with self._lock:
                if p_online:
                    self._last_error = None
            return {
                "state": "online",
                "provider": settings["provider"],
                "comfyui_url": urls[0] if urls else settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "has_session": has_primary_session or p_online,
                "devices": all_devices,
                "comfyui_version": probe.get("comfyui_version"),
                "auth_url": managed_primary["auth_url"] or managed_secondary["auth_url"],
                "error": None,
                "orphan_sessions": [],
                "logs": p_logs or s_logs,
            }
        if p_active in transitional_states:
            return {
                "state": p_active,
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "devices": [],
                "auth_url": p_auth_url if p_active == "awaiting_drive_auth" else None,
                "error": None,
                "orphan_sessions": [],
                "logs": p_logs,
            }

        if probe.get("auth_failed"):
            err_msg = message("colab_auth_rejected", code=probe.get("status_code", 401))
            return {
                "state": "error",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "devices": [],
                "auth_url": None,
                "error": err_msg,
                "orphan_sessions": [],
                "logs": p_logs,
            }

        if not cli_available:
            return {
                "state": "cli_unavailable",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": False,
                "devices": [],
                "auth_url": None,
                "error": p_error,
                "orphan_sessions": [],
                "logs": p_logs,
            }

        if cli_auth_active or cli_auth_required:
            return {
                "state": cli_auth_state if cli_auth_active else "cli_auth_required",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": False,
                "devices": [],
                "auth_url": cli_auth_url if cli_auth_state == "awaiting_cli_auth" else None,
                "error": cli_auth_error,
                "orphan_sessions": [],
                "logs": p_logs,
            }

        if sessions["orphans"]:
            return {
                "state": "orphan_warning",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": has_primary_session,
                "devices": [],
                "auth_url": None,
                "error": p_error,
                "orphan_sessions": sessions["orphans"],
                "logs": p_logs,
            }
        if p_error:
            return {
                "state": "error",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                **node_meta,
                "session": primary_session,
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": has_primary_session,
                "devices": [],
                "auth_url": None,
                "error": p_error,
                "orphan_sessions": [],
                "logs": p_logs,
            }
        return {
            "state": "offline",
            "provider": settings["provider"],
            "comfyui_url": settings["comfyui_url"],
            **node_meta,
            "session": primary_session,
            "gpu": settings["colab_gpu"],
            "cli_available": True,
            "has_session": has_primary_session,
            "devices": [],
            "auth_url": None,
            "error": None,
            "orphan_sessions": [],
            "logs": p_logs or s_logs,
        }

    def _reset_cli_auth(self, *, kill_proc: bool = True, expected: _CliAuthState | None = None) -> None:
        with self._lock:
            ca = self._cli_auth
            if expected is not None and ca is not expected:
                ca = expected
            ca.cancel_event.set()
            proc = ca.proc
            thread = ca.thread
            ca.active_state = None
            ca.auth_url = None
            ca.proc = None
        if kill_proc and proc is not None:
            _kill_process_tree(proc)
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._lock:
            ca.submitted_code = None

    def close(self) -> None:
        self.stop_keepalive_monitor()
        with self._lock:
            self._cli_auth_gen += 1
        self._reset_cli_auth(kill_proc=True)

    def start_cli_auth(self, environ=None, *, restart: bool = False) -> dict:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            raise RuntimeUnavailableError(message("colab_cli_missing"))
        auth_mode = settings["colab_auth"]
        if auth_mode == "adc":
            raise ValueError(message("colab_cli_auth_adc_unsupported"))

        existing_url: str | None = None
        existing_warning: str | None = None
        existing_auth_id: str | None = None
        start_gen = 0
        with self._lock:
            cur = self._cli_auth
            if self._cli_auth_starting or cur.active_state == "verifying_cli_auth":
                raise RuntimeBusyError(message("colab_busy"))
            if (
                not restart
                and cur.active_state == "awaiting_cli_auth"
                and cur.auth_url
                and cur.proc is not None
                and cur.proc.poll() is None
            ):
                existing_url = cur.auth_url
                existing_warning = cur.keyring_warning
                existing_auth_id = cur.auth_id
            else:
                self._cli_auth_starting = True
                start_gen = self._cli_auth_gen
        if existing_url is not None:
            return {
                "started": True,
                "already_authenticated": False,
                "auth_id": existing_auth_id,
                "auth_url": existing_url,
                "keyring_warning": existing_warning,
                "status": self.status(env_copy),
            }

        ca = _CliAuthState()
        try:
            self._reset_cli_auth(kill_proc=True)
            with self._lock:
                if self._cli_auth_gen != start_gen:
                    raise RuntimeError(message("colab_cli_auth_failed", reason="login cancelled"))
            argv = _build_argv(colab_bin, auth_mode, ["sessions"])
            kwargs = _popen_kwargs(env_copy)
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **kwargs,
            )
            ca.proc = proc
            ca.active_state = "awaiting_cli_auth"
            with self._lock:
                cancelled_during_spawn = self._cli_auth_gen != start_gen
                if not cancelled_during_spawn:
                    self._cli_auth = ca
                    self._sessions_cache = None
            if cancelled_during_spawn:
                _kill_process_tree(proc)
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
                raise RuntimeError(message("colab_cli_auth_failed", reason="login cancelled"))

            def _scrub_code(text: str, code_val: str | None) -> str:
                out = text
                if code_val and len(code_val) >= 6:
                    quoted = urllib.parse.quote(code_val, safe="")
                    for variant in (
                        code_val,
                        quoted,
                        quoted.lower(),
                        code_val.replace("/", "\\/"),
                    ):
                        if variant and variant in out:
                            out = out.replace(variant, "[REDACTED]")
                out = _OAUTH_TOKEN_RE.sub("[REDACTED]", out)
                out = _OAUTH_SECRET_FIELD_RE.sub(r"\1[REDACTED]", out)
                return out

            def _watch() -> None:
                collected: list[str] = []
                collected_chars = 0
                saw_keyring_warning = False
                timed_out = False
                code_snapshot: str | None = None

                def _handle_chunk(chunk: str) -> None:
                    nonlocal collected_chars, saw_keyring_warning
                    if _KEYRING_SAVE_WARNING_MARKER in chunk:
                        saw_keyring_warning = True
                    if len(chunk) > 65536:
                        chunk = chunk[-65536:]
                    collected.append(chunk)
                    collected_chars += len(chunk)
                    while collected_chars > 65536 and len(collected) > 1:
                        dropped = collected.pop(0)
                        collected_chars -= len(dropped)
                    m = _URL_RE.search(chunk)
                    if m:
                        should_signal = False
                        with self._lock:
                            if self._cli_auth is ca and not ca.code_submitted.is_set() and not ca.cancel_event.is_set():
                                ca.auth_url = m.group(1).strip()
                                ca.active_state = "awaiting_cli_auth"
                                should_signal = True
                        if should_signal:
                            ca.url_ready.set()

                def _read_stdout() -> None:
                    buf = ""
                    try:
                        while True:
                            ch = proc.stdout.read(1)
                            if not ch:
                                if buf:
                                    _handle_chunk(buf)
                                break
                            buf += ch
                            if ch == "\n" or "Enter the authorization code:" in buf or len(buf) >= 8192:
                                _handle_chunk(buf)
                                buf = ""
                    except Exception:
                        pass

                rt = threading.Thread(target=_read_stdout, daemon=True)
                rt.start()
                try:
                    deadline = time.monotonic() + self.cli_auth_timeout
                    extended_for_submit = False
                    while proc.poll() is None:
                        if ca.submitted_code and code_snapshot is None:
                            code_snapshot = ca.submitted_code
                        if ca.code_submitted.is_set() and not extended_for_submit:
                            extended_for_submit = True
                            submit_window = min(CLI_AUTH_SUBMIT_TIMEOUT_SEC, max(5.0, self.cli_auth_timeout))
                            deadline = max(deadline, time.monotonic() + submit_window + 5.0)
                        if ca.cancel_event.is_set():
                            _kill_process_tree(proc)
                            break
                        if time.monotonic() >= deadline:
                            timed_out = True
                            _kill_process_tree(proc)
                            break
                        time.sleep(0.05)
                    if ca.submitted_code and code_snapshot is None:
                        code_snapshot = ca.submitted_code
                    try:
                        rc = proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        _kill_process_tree(proc)
                        rc = -9
                    rt.join(timeout=2.0)
                    raw_out = _scrub_code("".join(collected), code_snapshot or ca.submitted_code)
                    with use_lang(env_copy.get("H3_LANG")):
                        with self._lock:
                            if self._cli_auth is ca:
                                ca.proc = None
                                ca.active_state = None
                                ca.auth_url = None
                                self._sessions_cache = None
                                if ca.cancel_event.is_set():
                                    if not ca.submit_timed_out:
                                        ca.error = None
                                elif timed_out:
                                    ca.error = message("colab_cli_auth_timeout", seconds=int(self.cli_auth_timeout))
                                elif rc == 0:
                                    ca.authenticated = True
                                    ca.already_authenticated = not ca.code_submitted.is_set()
                                    ca.error = None
                                    self._cli_auth_required_hint = False
                                    for slot_st in self._slots.values():
                                        if slot_st.auth_required_error:
                                            slot_st.last_error = None
                                            slot_st.auth_required_error = False
                                    if saw_keyring_warning or _KEYRING_SAVE_WARNING_MARKER in raw_out:
                                        ca.keyring_warning = message("colab_cli_auth_keyring_warning")
                                    else:
                                        ca.keyring_warning = None
                                else:
                                    lower_out = raw_out.lower()
                                    if "invalid_grant" in lower_out or "malformed auth code" in lower_out:
                                        ca.error = message("colab_cli_auth_invalid_code")
                                    else:
                                        clean_tail = _strip_rich_traceback_frames(raw_out)[-240:] or f"exit {rc}"
                                        ca.error = message("colab_cli_auth_failed", reason=clean_tail)
                finally:
                    try:
                        if proc.poll() is None:
                            _kill_process_tree(proc)
                    except Exception:
                        pass
                    for stream in (proc.stdin, proc.stdout):
                        if stream is not None:
                            try:
                                stream.close()
                            except OSError:
                                pass
                    with self._lock:
                        if self._cli_auth is ca and ca.active_state is not None:
                            ca.proc = None
                            ca.active_state = None
                            ca.auth_url = None
                            self._sessions_cache = None
                            if not ca.error and not ca.cancel_event.is_set():
                                ca.error = message("colab_cli_auth_failed", reason="login watcher aborted")
                        ca.submitted_code = None
                    ca.done_event.set()

            wt = threading.Thread(target=_watch, name="colab-cli-auth", daemon=True)
            ca.thread = wt
            wt.start()

            wait_deadline = time.monotonic() + min(CLI_AUTH_START_WAIT_SEC, max(1.0, self.cli_auth_timeout))
            while time.monotonic() < wait_deadline:
                if ca.url_ready.is_set() or ca.done_event.is_set():
                    break
                time.sleep(0.02)
            if not ca.url_ready.is_set() and not ca.done_event.is_set():
                self._reset_cli_auth(kill_proc=True, expected=ca)
                raise RuntimeError(message("colab_cli_auth_failed", reason="timed out waiting for authorization URL"))
        finally:
            with self._lock:
                self._cli_auth_starting = False

        if ca.url_ready.is_set() and not ca.done_event.is_set():
            with self._lock:
                url = ca.auth_url if self._cli_auth is ca else None
                auth_id = ca.auth_id if self._cli_auth is ca else None
            if url:
                return {
                    "started": True,
                    "already_authenticated": False,
                    "auth_id": auth_id,
                    "auth_url": url,
                    "keyring_warning": None,
                    "status": self.status(env_copy),
                }
        if ca.done_event.is_set():
            if ca.authenticated and ca.already_authenticated:
                return {
                    "started": False,
                    "already_authenticated": True,
                    "auth_id": None,
                    "auth_url": None,
                    "keyring_warning": ca.keyring_warning,
                    "status": self.status(env_copy),
                }
            raise RuntimeError(ca.error or message("colab_cli_auth_failed", reason="login process exited"))
        self._reset_cli_auth(kill_proc=True, expected=ca)
        raise RuntimeError(message("colab_cli_auth_failed", reason="timed out waiting for authorization URL"))

    def submit_cli_auth_code(self, code: str, environ=None, *, auth_id: str | None = None) -> dict:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        if not isinstance(code, str):
            raise ValueError(message("colab_cli_auth_code_invalid"))
        cleaned = code.strip()
        if not cleaned or "\0" in cleaned or "\n" in cleaned or "\r" in cleaned or len(cleaned) > 2048:
            raise ValueError(message("colab_cli_auth_code_invalid"))

        with self._lock:
            if self._cli_auth_starting:
                raise RuntimeBusyError(message("colab_busy"))
            ca = self._cli_auth
            if (
                (auth_id and ca.auth_id != auth_id)
                or ca.active_state != "awaiting_cli_auth"
                or not ca.auth_url
                or ca.proc is None
                or ca.proc.poll() is not None
                or ca.proc.stdin is None
            ):
                raise ValueError(message("colab_cli_auth_not_waiting"))
            ca.active_state = "verifying_cli_auth"
            ca.submitted_code = cleaned
            ca.code_submitted.set()
            stdin_pipe = ca.proc.stdin
        try:
            stdin_pipe.write(cleaned + "\n")
            stdin_pipe.flush()
            stdin_pipe.close()
        except (OSError, ValueError) as exc:
            self._reset_cli_auth(kill_proc=True, expected=ca)
            raise ValueError(message("colab_cli_auth_not_waiting")) from exc

        wait_timeout = min(CLI_AUTH_SUBMIT_TIMEOUT_SEC, max(5.0, self.cli_auth_timeout))
        finished = ca.done_event.wait(timeout=wait_timeout)
        if not finished:
            err = message("colab_cli_auth_submit_timeout", seconds=int(wait_timeout))
            with self._lock:
                if self._cli_auth is ca:
                    ca.submit_timed_out = True
                    ca.error = err
            self._reset_cli_auth(kill_proc=True, expected=ca)
            return {
                "accepted": False,
                "authenticated": False,
                "error": err,
                "keyring_warning": None,
                "status": self.status(env_copy),
            }
        return {
            "accepted": bool(ca.authenticated),
            "authenticated": bool(ca.authenticated),
            "error": ca.error,
            "keyring_warning": ca.keyring_warning,
            "status": self.status(env_copy),
        }

    def cancel_cli_auth(self, environ=None, *, auth_id: str | None = None) -> dict:
        env_copy = dict(os.environ if environ is None else environ)
        with self._lock:
            if auth_id and (self._cli_auth_starting or self._cli_auth.auth_id != auth_id):
                stale = True
            else:
                stale = False
                self._cli_auth_gen += 1
        if stale:
            return {"cancelled": False, "stale": True, "status": self.status(env_copy)}
        self._reset_cli_auth(kill_proc=True)
        with self._lock:
            self._cli_auth.error = None
            self._sessions_cache = None
        return {"cancelled": True, "stale": False, "status": self.status(env_copy)}

    def submit_drive_authorized(self, *, slot: str | None = None) -> dict:
        candidates = [normalize_slot(slot)] if slot is not None else ["primary", "secondary"]
        with self._lock:
            target_slot = None
            for cand in candidates:
                st = self._slots[cand]
                if not self._is_stopping(st) and st.active_state == "awaiting_drive_auth" and st.drive_proc is not None and st.drive_proc.stdin is not None:
                    target_slot = cand
                    break
            if target_slot is None:
                fallback = self._slots[candidates[0]]
                fb_state = "stopping" if self._is_stopping(fallback) else (fallback.active_state or "offline")
                return {"accepted": False, "slot": candidates[0], "state": fb_state}
            st = self._slots[target_slot]
            try:
                st.drive_proc.stdin.write("\n")
                st.drive_proc.stdin.flush()
            except (OSError, ValueError):
                st.drive_ack_event.set()
                return {"accepted": False, "slot": target_slot, "state": st.active_state}
            st.active_state = "starting_comfy"
            st.auth_url = None
            st.drive_ack_event.set()
            return {"accepted": True, "slot": target_slot, "state": "starting_comfy"}

    def _run_drivemount_once(self, colab_bin: str, auth_mode: str, session_name: str, environ, *, slot: str = "primary") -> tuple[int, str, bool]:
        st = self._slot(slot)
        argv = _build_argv(colab_bin, auth_mode, ["drivemount", "-s", session_name])
        kwargs = _popen_kwargs(environ)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        with self._lock:
            st.drive_proc = proc
            st.active_proc = proc
            st.drive_ack_event.clear()

        lines: list[str] = []
        prompted = threading.Event()
        cli_crashed = threading.Event()

        def read_output():
            buf = ""
            saw_rich_box_end = False
            saw_traceback = False
            try:
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        if buf:
                            lines.append(buf)
                            self._append_log(buf, slot=slot)
                            crashed, saw_rich_box_end, saw_traceback = _is_terminal_cli_crash_line(
                                buf, saw_rich_box_end, saw_traceback
                            )
                            if crashed:
                                cli_crashed.set()
                        break
                    buf += ch
                    if ch == "\n" or "Press Enter after you have granted access" in buf:
                        lines.append(buf)
                        self._append_log(buf, slot=slot)
                        crashed, saw_rich_box_end, saw_traceback = _is_terminal_cli_crash_line(
                            buf, saw_rich_box_end, saw_traceback
                        )
                        if crashed:
                            cli_crashed.set()
                        m = _URL_RE.search(buf)
                        if m:
                            with self._lock:
                                if not st.drive_ack_event.is_set():
                                    st.auth_url = m.group(1).strip()
                                    st.active_state = "awaiting_drive_auth"
                            prompted.set()
                        elif "Press Enter after you have granted access" in buf:
                            with self._lock:
                                if not st.drive_ack_event.is_set() and st.active_state != "awaiting_drive_auth":
                                    st.active_state = "awaiting_drive_auth"
                            prompted.set()
                        buf = ""
            except Exception:
                pass

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        timed_out_auth = False
        crash_deadline: float | None = None
        try:
            deadline = time.monotonic() + 180.0
            while proc.poll() is None:
                if st.cancel_event.is_set():
                    _kill_process_tree(proc)
                    break
                if prompted.is_set() and not st.drive_ack_event.is_set():
                    auth_deadline = time.monotonic() + self.drive_auth_timeout
                    while not st.drive_ack_event.is_set():
                        if st.cancel_event.is_set() or proc.poll() is not None:
                            break
                        now_auth = time.monotonic()
                        if cli_crashed.is_set():
                            if crash_deadline is None:
                                crash_deadline = now_auth + self.crash_grace_sec
                            elif now_auth >= crash_deadline:
                                _kill_process_tree(proc)
                                break
                        remaining_auth = auth_deadline - now_auth
                        if remaining_auth <= 0:
                            timed_out_auth = True
                            _kill_process_tree(proc)
                            break
                        st.drive_ack_event.wait(timeout=min(0.2, remaining_auth))
                    if timed_out_auth or proc.poll() is not None:
                        break
                    if st.cancel_event.is_set():
                        _kill_process_tree(proc)
                        break
                    deadline = time.monotonic() + 180.0
                now = time.monotonic()
                if cli_crashed.is_set():
                    if crash_deadline is None:
                        crash_deadline = now + self.crash_grace_sec
                    elif now >= crash_deadline:
                        _kill_process_tree(proc)
                        break
                if now >= deadline:
                    _kill_process_tree(proc)
                    break
                time.sleep(0.05)
            rc = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            rc = -9
        finally:
            with self._lock:
                if st.drive_proc is proc:
                    st.drive_proc = None
            reader.join(timeout=2.0)
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            with self._lock:
                if st.active_proc is proc:
                    st.active_proc = None
                st.auth_url = None
        return rc, "".join(lines), timed_out_auth

    def _startup_worker(self, env_copy: dict, colab_bin: str, lock_ctx, *, slot: str = "primary") -> None:
        slot_name = normalize_slot(slot)
        st = self._slot(slot_name)
        settings = config.video_settings(env_copy)
        session_name = self._session_name(settings, slot_name)
        tunnel_provider = self._tunnel_provider(slot_name)
        auth_mode = settings["colab_auth"]
        try:
            with use_lang(env_copy.get("H3_LANG")):
                sessions = self._query_sessions(colab_bin, auth_mode, environ=env_copy, force=True, cancel_slot=slot_name)
                if st.cancel_event.is_set():
                    return
                if sessions.get("ok") is False:
                    if sessions.get("auth_required"):
                        with self._lock:
                            st.auth_required_error = True
                        raise RuntimeError(message("colab_cli_auth_required"))
                    raise RuntimeError(message("colab_start_failed", reason="colab sessions query failed"))
                has_target = any(s["name"] == session_name for s in sessions["named"])
                if not has_target and sessions["orphans"]:
                    endpoints = ", ".join(s["endpoint"] for s in sessions["orphans"])
                    raise RuntimeError(message("colab_orphan_blocked", sessions=endpoints))

                if not has_target:
                    with self._lock:
                        st.active_state = "provisioning"
                    new_env = {**env_copy, "COLAB_CLI_ASSIGN_RETRIES": "0"}
                    total_attempts = self.provision_retries + 1
                    provisioned = False
                    last_rc = 1
                    last_out = ""
                    for attempt in range(total_attempts):
                        rc, out = self._run_cmd_for_slot(
                            colab_bin, auth_mode, _new_session_args(session_name, settings["colab_gpu"]),
                            environ=new_env, timeout=PROVISION_ATTEMPT_TIMEOUT_SEC, slot=slot_name,
                        )
                        last_rc, last_out = rc, out
                        if st.cancel_event.is_set():
                            return
                        if rc == 0 and "Session READY" in out:
                            provisioned = True
                            break
                        if attempt < self.provision_retries and _is_retryable_provision_failure(rc, out):
                            delay = self.provision_retry_delays[min(attempt, len(self.provision_retry_delays) - 1)]
                            reason = _summarize_provision_error(rc, out)
                            self._append_log(
                                message(
                                    "colab_provision_retry",
                                    gpu=settings["colab_gpu"],
                                    attempt=attempt + 1,
                                    total=total_attempts,
                                    reason=reason,
                                    seconds=int(round(delay)),
                                ),
                                slot=slot_name,
                            )
                            if st.cancel_event.wait(timeout=delay):
                                return
                            if rc == -9 or "ReadTimeout" in out:
                                check_sessions = self._query_sessions(colab_bin, auth_mode, environ=env_copy, force=True, cancel_slot=slot_name)
                                if st.cancel_event.is_set():
                                    return
                                if check_sessions.get("ok") is False:
                                    raise RuntimeError(message("colab_start_failed", reason="colab sessions query failed after timeout"))
                                if any(s["name"] == session_name for s in check_sessions["named"]):
                                    provisioned = True
                                    break
                                if check_sessions["orphans"]:
                                    endpoints = ", ".join(s["endpoint"] for s in check_sessions["orphans"])
                                    raise RuntimeError(message("colab_orphan_blocked", sessions=endpoints))
                            continue
                        break
                    if not provisioned:
                        reason = _summarize_provision_error(last_rc, last_out)
                        if _is_retryable_provision_failure(last_rc, last_out) and self.provision_retries > 0:
                            raise RuntimeError(
                                message(
                                    "colab_provision_exhausted",
                                    gpu=settings["colab_gpu"],
                                    attempts=total_attempts,
                                    reason=reason,
                                )
                            )
                        clean_tail = _strip_rich_traceback_frames(last_out)[-300:]
                        raise RuntimeError(message("colab_start_failed", reason=clean_tail or reason))
                    with self._lock:
                        self._sessions_cache = None

                with self._lock:
                    st.active_state = "starting_comfy"

                drive_already_mounted = False
                if has_target:
                    ls_rc, ls_out = self._run_cmd_for_slot(
                        colab_bin, auth_mode, ["ls", "/content/drive", "-s", session_name],
                        environ=env_copy, timeout=20.0, slot=slot_name,
                    )
                    drive_already_mounted = ls_rc == 0 and "MyDrive/" in ls_out

                if not drive_already_mounted:
                    mount_out = ""
                    rc = 1
                    timed_out_auth = False
                    def _mount_once():
                        if slot_name == "primary":
                            return self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy)
                        return self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy, slot=slot_name)

                    for attempt in range(8):
                        rc, mount_out, timed_out_auth = _mount_once()
                        if st.cancel_event.is_set():
                            return
                        if timed_out_auth:
                            raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                        if _is_transient_ws_disconnect(mount_out) and "Mounted at" not in mount_out and "already mounted" not in mount_out.lower() and attempt < 7:
                            if self._sleep_cancelable(st, 5.0):
                                return
                            continue
                        break
                    if "Credentials propagated" in mount_out and "Mounted at" not in mount_out and "already mounted" not in mount_out.lower():
                        rc, mount_out, timed_out_auth = _mount_once()
                        if st.cancel_event.is_set():
                            return
                        if timed_out_auth:
                            raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                    if rc != 0 or ("Mounted at" not in mount_out and "already mounted" not in mount_out.lower()):
                        raise RuntimeError(message("colab_start_failed", reason=mount_out.strip()[-300:] or f"drivemount exit {rc}"))

                with self._lock:
                    st.active_state = "starting_comfy"
                local_start_script = config.ROOT / "scripts" / "start_colab.py"
                remote_script_override: str | None = None
                if local_start_script.is_file():
                    upload_dst = (
                        "content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py"
                        if slot_name == "primary"
                        else "tmp/h3_start_colab.py"
                    )
                    up_rc = 1
                    up_out = ""
                    for _ in range(2):
                        up_rc, up_out = self._run_cmd_for_slot(
                            colab_bin,
                            auth_mode,
                            ["upload", "-s", session_name, str(local_start_script), upload_dst],
                            environ=env_copy,
                            timeout=60.0,
                            slot=slot_name,
                        )
                        if st.cancel_event.is_set():
                            return
                        if up_rc == 0:
                            break
                        if self._sleep_cancelable(st, 2.0):
                            return
                    if up_rc == 0 and slot_name != "primary":
                        remote_script_override = "/tmp/h3_start_colab.py"
                    elif up_rc != 0:
                        detail = (up_out.strip() or f"exit {up_rc}")[-180:]
                        self._append_log(
                            f"[bootstrap] Warning: failed to upload local start_colab.py ({detail}); continuing with existing Drive copy",
                            slot=slot_name,
                        )
                exec_out = ""
                rc = 1
                discovered_url = ""
                start_code = _remote_start_code(tunnel_provider, script_path=remote_script_override)
                configured_primary = self._primary_url(settings) if slot_name == "primary" else ""
                for attempt in range(6):
                    if st.cancel_event.is_set():
                        return
                    if attempt > 0:
                        cand_url = discovered_url or configured_primary
                        probe_target = cand_url if (slot_name == "secondary" or (discovered_url and discovered_url != configured_primary)) else None
                        if cand_url and self._probe_slot(env_copy, settings, slot_name, target_url=probe_target, timeout=4.0)["online"]:
                            rc = 0
                            exec_out = ""
                            break
                    rc, exec_out = self._run_cmd_for_slot(
                        colab_bin,
                        auth_mode,
                        ["exec", "-s", session_name, "--timeout", "1800"],
                        environ=env_copy,
                        stdin_text=start_code,
                        timeout=2400.0,
                        slot=slot_name,
                    )
                    if st.cancel_event.is_set():
                        return
                    parsed_url = parse_tunnel_ready_url(exec_out, ephemeral_only=(slot_name == "secondary"))
                    if parsed_url:
                        discovered_url = parsed_url
                    if rc == 0 and "Traceback (most recent call last)" not in exec_out:
                        break
                    if _is_transient_ws_disconnect(exec_out) and attempt < 5:
                        if self._sleep_cancelable(st, self.retry_delay_sec):
                            return
                        continue
                    break
                if rc != 0 or "Traceback (most recent call last)" in exec_out:
                    raise RuntimeError(message("colab_start_failed", reason=exec_out.strip()[-300:] or f"colab exec exit {rc}"))

                if slot_name == "secondary":
                    if not discovered_url:
                        raise RuntimeError(message("colab_start_failed", reason=message("colab_tunnel_unready")))
                    probe_limit = 15
                    target_probe_url = discovered_url
                else:
                    probe_limit = 10
                    target_probe_url = (
                        discovered_url
                        if (discovered_url and discovered_url != configured_primary)
                        else None
                    )

                for _ in range(probe_limit):
                    if st.cancel_event.is_set():
                        return
                    if self._probe_slot(env_copy, settings, slot_name, target_url=target_probe_url, timeout=4.0)["online"]:
                        if st.cancel_event.is_set():
                            return
                        now_ts = float(self._clock())
                        if slot_name == "secondary":
                            self._set_remembered_node(
                                "secondary",
                                {
                                    "session": session_name,
                                    "url": discovered_url,
                                    "gpu": settings["colab_gpu"],
                                    "tunnel": tunnel_provider,
                                    "key_fp": comfy.key_fingerprint(settings["comfyui_api_key"]),
                                    "updated_at": round(time.time(), 1),
                                },
                                abort_if_cancelled=True,
                            )
                        elif target_probe_url:
                            self._set_remembered_node(
                                "primary",
                                {
                                    "session": session_name,
                                    "url": target_probe_url,
                                    "gpu": settings["colab_gpu"],
                                    "tunnel": tunnel_provider,
                                    "key_fp": comfy.key_fingerprint(settings["comfyui_api_key"]),
                                    "updated_at": round(time.time(), 1),
                                },
                                abort_if_cancelled=True,
                                replace_url=configured_primary or None,
                            )
                        with self._lock:
                            if st.cancel_event.is_set():
                                return
                            st.last_error = None
                            st.auth_required_error = False
                            st.active_state = None
                            self._sessions_cache = None
                            st.last_keepalive_at = now_ts
                            self._last_activity_at = now_ts
                            st.last_keepalive_fail_at = None
                        return
                    if self._sleep_cancelable(st, 1.5):
                        return
                raise RuntimeError(message("colab_start_failed", reason=message("colab_tunnel_unready")))
        except Exception as exc:
            with self._lock:
                if not st.cancel_event.is_set():
                    st.last_error = str(exc)
                else:
                    st.auth_required_error = False
                st.active_state = None
                st.auth_url = None
                self._sessions_cache = None
        finally:
            with self._lock:
                if st.cancel_event.is_set():
                    st.active_state = None
                    st.auth_url = None
            lock_ctx.__exit__(None, None, None)

    def start_async(self, environ=None, *, slot: str = "primary") -> dict:
        slot_name = normalize_slot(slot)
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        st = self._slot(slot_name)
        already_active = False
        with self._lock:
            if self._is_stopping(st) or self._cli_auth_starting or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth"):
                raise RuntimeBusyError(message("colab_busy"))
            if st.active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy"):
                already_active = True
        if already_active:
            return self.status(env_copy)

        if slot_name == "primary":
            probe = self._probe_slot(env_copy, settings, "primary")
            if probe["online"]:
                with self._lock:
                    if self._is_stopping(st):
                        raise RuntimeBusyError(message("colab_busy"))
                return self.status(env_copy)
            if not self._primary_url(settings):
                raise ValueError(message("colab_url_missing"))
        else:
            if not settings["comfyui_api_key"]:
                raise ValueError(message("colab_key_missing"))
            saved_sec = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
            if saved_sec and saved_sec.get("url"):
                if self._probe_slot(env_copy, settings, "secondary", target_url=saved_sec["url"])["online"]:
                    with self._lock:
                        if self._is_stopping(st):
                            raise RuntimeBusyError(message("colab_busy"))
                    return self.status(env_copy)

        with self._lock:
            if self._is_stopping(st) or self._cli_auth_starting or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth"):
                raise RuntimeBusyError(message("colab_busy"))
            if st.starting or st.active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy"):
                already_active = True
        if already_active:
            return self.status(env_copy)

        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            raise RuntimeUnavailableError(message("colab_cli_missing"))

        owns_starting = False
        with self._lock:
            if self._is_stopping(st) or self._cli_auth_starting or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth"):
                raise RuntimeBusyError(message("colab_busy"))
            if st.starting or st.active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy"):
                already_active = True
            else:
                st.starting = True
                owns_starting = True
                keepalive_running = bool(st.keepalive_in_flight)
                keepalive_proc = st.active_proc if keepalive_running else None
                if keepalive_running:
                    st.cancel_event.set()
        if already_active:
            return self.status(env_copy)

        lock_ctx = runtime_file_lock(self.storage, slot=slot_name)
        entered_lock = False
        set_active = False
        try:
            if keepalive_proc is not None:
                _kill_process_tree(keepalive_proc)
            if keepalive_running:
                ka_deadline = time.monotonic() + 5.0
                while time.monotonic() < ka_deadline:
                    with self._lock:
                        if not st.keepalive_in_flight:
                            break
                        if st.active_proc is not None and st.active_proc is not keepalive_proc:
                            keepalive_proc = st.active_proc
                            _kill_process_tree(keepalive_proc)
                    time.sleep(0.02)

            lock_deadline = time.monotonic() + (3.0 if keepalive_running else 0.0)
            while True:
                try:
                    lock_ctx.__enter__()
                    entered_lock = True
                    break
                except RuntimeBusyError:
                    if time.monotonic() >= lock_deadline:
                        raise
                    time.sleep(0.05)

            t = threading.Thread(
                target=self._startup_worker,
                args=(env_copy, colab_bin, lock_ctx),
                kwargs={"slot": slot_name},
                daemon=True,
            )
            with self._lock:
                if self._is_stopping(st) or self._cli_auth_starting or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth"):
                    raise RuntimeBusyError(message("colab_busy"))
                st.cancel_event.clear()
                st.last_error = None
                st.auth_required_error = False
                st.auth_url = None
                st.logs.clear()
                st.active_state = "provisioning"
                st.worker_thread = t
                set_active = True
                t.start()
        except BaseException:
            with self._lock:
                if set_active:
                    st.active_state = None
                if keepalive_running and not self._is_stopping(st):
                    st.cancel_event.clear()
            if entered_lock:
                lock_ctx.__exit__(None, None, None)
            raise
        finally:
            if owns_starting:
                with self._lock:
                    st.starting = False
        return self.status(env_copy)

    def ensure_started(self, environ=None, *, timeout: float = 2700.0, slot: str = "primary") -> dict:
        slot_name = normalize_slot(slot)
        cur = self.start_async(environ, slot=slot_name)
        def _slot_state(st_dict: dict) -> tuple[str, str | None]:
            entry = next((m for m in (st_dict.get("managed_nodes") or []) if m.get("slot") == slot_name), None)
            if entry:
                return entry["state"], entry.get("error")
            if slot_name == "primary":
                return st_dict["state"], st_dict.get("error")
            return "offline", None
        state_val, err_val = _slot_state(cur)
        if state_val == "online":
            return cur
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur = self.status(environ)
            state_val, err_val = _slot_state(cur)
            if state_val == "online":
                return cur
            if state_val in ("error", "orphan_warning", "offline", "cli_unavailable"):
                raise RuntimeError(err_val or f"Colab startup ended in state {state_val}")
            time.sleep(1.0)
        raise TimeoutError(f"Timed out after {timeout}s waiting for Colab ComfyUI runtime")

    def mark_local_activity(self, *, now: float | None = None) -> None:
        """Record real user/job activity so Colab stays warm during storyboard & review before video launch."""
        current = float(self._clock()) if now is None else float(now)
        with self._lock:
            self._last_activity_at = current

    def maybe_send_kernel_keepalive(
        self,
        environ=None,
        *,
        now: float | None = None,
        queue_count: int | None = None,
        force_busy: bool = False,
        local_busy: bool = False,
        slot: str = "primary",
    ) -> bool:
        """Send a lightweight Colab kernel heartbeat while remote or local workbench work is active."""
        slot_name = normalize_slot(slot)
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return False
        env_copy = dict(os.environ if environ is None else environ)
        try:
            settings = config.video_settings(env_copy)
        except ValueError:
            return False
        is_comfy_default = settings["provider"] == "comfyui"
        if not is_comfy_default and not force_busy and not local_busy:
            return False
        if slot_name == "primary":
            target_url = self._primary_url(settings)
        else:
            saved_sec = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
            target_url = str((saved_sec or {}).get("url") or "").strip()
        if not target_url:
            return False
        st = self._slot(slot_name)
        current = float(self._clock()) if now is None else float(now)
        with self._lock:
            if local_busy:
                self._last_activity_at = current
            if (
                self._is_stopping(st)
                or st.starting
                or st.active_state is not None
                or st.active_proc is not None
                or st.keepalive_in_flight
                or (self._cli_auth_required_hint and settings["colab_auth"] != "adc")
                or self._cli_auth_starting
                or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth")
            ):
                return False
            if (current - st.last_keepalive_at) < self.keepalive_interval_sec:
                return False
            if (
                st.last_keepalive_fail_at is not None
                and (current - st.last_keepalive_fail_at) < self.keepalive_retry_cooldown_sec
            ):
                return False
            last_activity_at = self._last_activity_at
        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            return False

        probe_env = env_copy if (slot_name == "primary" and len(settings["comfyui_urls"]) <= 1) else {**env_copy, "COMFYUI_URL": target_url}
        within_idle_grace = (
            last_activity_at is not None
            and (current - last_activity_at) < self.keepalive_idle_grace_sec
        )
        if force_busy:
            active_queue = 1 if queue_count is None else max(1, int(queue_count))
            reason = f"queue={active_queue}"
        else:
            active_queue = probe_remote_queue_count(probe_env, timeout=6.0) if queue_count is None else int(queue_count)
            if active_queue > 0:
                reason = f"queue={active_queue}"
            elif is_comfy_default and (local_busy or within_idle_grace):
                if not probe_comfyui(probe_env, timeout=6.0)["online"]:
                    return False
                reason = "local_active" if local_busy else "idle_grace"
            else:
                return False

        session_name = self._session_name(settings, slot_name)
        with self._lock:
            if (
                self._is_stopping(st)
                or st.starting
                or st.active_state is not None
                or st.active_proc is not None
                or st.keepalive_in_flight
                or (self._cli_auth_required_hint and settings["colab_auth"] != "adc")
                or self._cli_auth_starting
                or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth")
            ):
                return False
            st.keepalive_in_flight = True
        try:
            with runtime_file_lock(self.storage, slot=slot_name):
                with self._lock:
                    if (
                        self._is_stopping(st)
                        or st.starting
                        or st.active_state is not None
                        or st.active_proc is not None
                        or (self._cli_auth_required_hint and settings["colab_auth"] != "adc")
                        or self._cli_auth_starting
                        or self._cli_auth.active_state in ("awaiting_cli_auth", "verifying_cli_auth")
                    ):
                        return False
                    st.cancel_event.clear()
                rc, out = self._run_cmd_for_slot(
                    colab_bin,
                    settings["colab_auth"],
                    ["exec", "-s", session_name, "--timeout", str(KERNEL_KEEPALIVE_TIMEOUT_SEC)],
                    environ=env_copy,
                    stdin_text=KERNEL_KEEPALIVE_CODE,
                    timeout=float(KERNEL_KEEPALIVE_TIMEOUT_SEC) + 15.0,
                    slot=slot_name,
                )
                if rc == 0 and "Traceback (most recent call last)" not in out:
                    with self._lock:
                        st.last_keepalive_at = current
                        if active_queue > 0 or local_busy:
                            self._last_activity_at = current
                        st.last_keepalive_fail_at = None
                    self._append_log(
                        f"[keepalive] Kernel keepalive sent (session={session_name}, {reason})",
                        slot=slot_name,
                    )
                    return True
                auth_needed = settings["colab_auth"] != "adc" and _is_cli_auth_required_output(out)
                detail = (out.strip() or f"exit {rc}")[-200:]
                with self._lock:
                    st.last_keepalive_fail_at = current
                    if auth_needed:
                        self._cli_auth_required_hint = True
                        self._sessions_cache = None
                    if "appears to be lost" in out:
                        self._sessions_cache = None
                        if (
                            slot_name == "primary"
                            and not self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
                            and not self._slots["secondary"].active_state
                            and not self._is_stopping(self._slots["secondary"])
                        ):
                            self._last_activity_at = None
                if "appears to be lost" in out and slot_name == "secondary":
                    self._set_remembered_node("secondary", None)
                self._append_log(f"[keepalive] Kernel keepalive failed (rc={rc}): {detail}", slot=slot_name)
                return False
        except RuntimeBusyError:
            return False
        except Exception as exc:
            with self._lock:
                st.last_keepalive_fail_at = current
            self._append_log(f"[keepalive] Kernel keepalive error: {type(exc).__name__}: {exc}", slot=slot_name)
            return False
        finally:
            with self._lock:
                st.keepalive_in_flight = False

    def trigger_keepalive_async(self, environ=None, *, force_busy: bool = False) -> None:
        """Non-blocking keepalive check when launching a real ComfyUI video job."""
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return
        env_copy = dict(os.environ if environ is None else environ)
        current = float(self._clock())
        due_slots: list[str] = []
        cur_api_key = str(env_copy.get("COMFYUI_API_KEY") or "").strip()
        slots_to_check = ["primary"] + (["secondary"] if self.remembered_node("secondary", api_key=cur_api_key) else [])
        with self._lock:
            for slot_name in slots_to_check:
                st = self._slots[slot_name]
                if self._is_stopping(st) or st.starting or st.active_state is not None or st.active_proc is not None or st.keepalive_in_flight:
                    continue
                if (current - st.last_keepalive_at) >= self.keepalive_interval_sec:
                    due_slots.append(slot_name)
        for slot_name in due_slots:
            threading.Thread(
                target=self.maybe_send_kernel_keepalive,
                args=(env_copy,),
                kwargs={"force_busy": force_busy, "slot": slot_name},
                name=f"colab-kernel-keepalive-once-{slot_name}",
                daemon=True,
            ).start()

    def start_keepalive_monitor(self, env_provider, *, poll_sec: float | None = None, local_busy_provider=None) -> None:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return
        interval = max(0.05, float(self.keepalive_poll_sec if poll_sec is None else poll_sec))
        with self._lock:
            self._keepalive_env_provider = env_provider
            if local_busy_provider is not None:
                self._local_busy_provider = local_busy_provider
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return
            self._keepalive_stop.clear()

            def monitor_loop() -> None:
                while not self._keepalive_stop.wait(timeout=interval):
                    with self._lock:
                        provider = self._keepalive_env_provider
                        busy_provider = self._local_busy_provider
                    if provider is None:
                        continue
                    try:
                        current_env = provider()
                        local_busy = bool(busy_provider()) if callable(busy_provider) else False
                        self.maybe_send_kernel_keepalive(current_env, local_busy=local_busy, slot="primary")
                        env_copy = os.environ if current_env is None else current_env
                        cur_api_key = str(env_copy.get("COMFYUI_API_KEY") or "").strip()
                        if self.remembered_node("secondary", api_key=cur_api_key):
                            self.maybe_send_kernel_keepalive(current_env, local_busy=local_busy, slot="secondary")
                    except Exception as exc:
                        self._append_log(f"[keepalive] Monitor error: {type(exc).__name__}: {exc}")

            t = threading.Thread(target=monitor_loop, name="colab-kernel-keepalive", daemon=True)
            self._keepalive_thread = t
            t.start()

    def stop_keepalive_monitor(self) -> None:
        self._keepalive_stop.set()
        with self._lock:
            thread = self._keepalive_thread
            self._keepalive_thread = None
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def stop(
        self,
        *,
        environ=None,
        force: bool = False,
        local_busy: bool = False,
        local_busy_provider=None,
        slot: str = "primary",
    ) -> dict:
        slot_name = normalize_slot(slot)
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            raise RuntimeUnavailableError(message("colab_cli_missing"))

        session_name = self._session_name(settings, slot_name)
        remote_queue = 0
        queue_unknown = False
        session_verified_absent = False

        def _check_remote_queue_strict(*, allow_cached_absent: bool = False) -> tuple[int, bool, bool]:
            if slot_name == "secondary":
                saved_sec_q = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
                sec_url_q = str((saved_sec_q or {}).get("url") or "").strip()
                rq = probe_remote_queue_count({**env_copy, "COMFYUI_URL": sec_url_q}, strict=True) if sec_url_q else None
            else:
                saved_sec_q = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
                sec_url_q = str((saved_sec_q or {}).get("url") or "").strip().rstrip("/")
                primary_urls_q = [u for u in settings["comfyui_urls"] if not sec_url_q or u != sec_url_q]
                if not primary_urls_q:
                    rq = None
                else:
                    formatted_pri_q = config.format_endpoint_urls(primary_urls_q)
                    probe_env_q = (
                        env_copy
                        if (len(primary_urls_q) == len(settings["comfyui_urls"]) and settings["comfyui_url"] == formatted_pri_q)
                        else {**env_copy, "COMFYUI_URL": formatted_pri_q}
                    )
                    rq = probe_remote_queue_count(probe_env_q, strict=True)
            if rq is None:
                slot_probe = self._probe_slot(env_copy, settings, slot_name)
                if slot_probe.get("online") or slot_probe.get("auth_failed"):
                    return 0, True, False
                if allow_cached_absent:
                    return 0, False, True
                sess = self._query_sessions(colab_bin, settings["colab_auth"], env_copy, force=True)
                has_active_session = any(item["name"] == session_name for item in sess.get("named", []))
                unknown = (not sess.get("ok")) or has_active_session
                return 0, unknown, (not unknown)
            return int(rq), False, False

        if not force:
            remote_queue, queue_unknown, session_verified_absent = _check_remote_queue_strict()

            if callable(local_busy_provider):
                local_busy = bool(local_busy or local_busy_provider())

            if local_busy or remote_queue > 0 or queue_unknown:
                return {
                    "stopped": False,
                    "slot": slot_name,
                    "confirmation_required": True,
                    "local_busy": bool(local_busy),
                    "remote_queue": remote_queue,
                    "queue_unknown": queue_unknown,
                    "status": self.status(env_copy),
                }

        st = self._slot(slot_name)
        with self._lock:
            if self._is_stopping(st):
                raise RuntimeBusyError(message("colab_busy"))
            st.stopping = True

        owns_stopping = True
        stop_succeeded = False
        stopped_sec_old_url: str | None = None
        try:
            # Dekker barrier: st.stopping is True before re-checking local_busy_provider(),
            # preventing a concurrent job launch or start_async() from racing without
            # clobbering st.active_state or killing an active startup worker on force=False.
            if not force and callable(local_busy_provider) and local_busy_provider():
                with self._lock:
                    st.stopping = False
                    owns_stopping = False
                    if st.active_state is None:
                        st.cancel_event.clear()
                return {
                    "stopped": False,
                    "slot": slot_name,
                    "confirmation_required": True,
                    "local_busy": True,
                    "remote_queue": remote_queue,
                    "queue_unknown": queue_unknown,
                    "status": self.status(env_copy),
                }

            with self._lock:
                active_worker = st.worker_thread if st.active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy") else None
                keepalive_running = st.keepalive_in_flight
                proc_to_cancel = st.active_proc if (active_worker is not None or keepalive_running) else None
                if active_worker is not None or keepalive_running:
                    st.cancel_event.set()
                    st.drive_ack_event.set()
            if proc_to_cancel is not None:
                _kill_process_tree(proc_to_cancel)
            if active_worker is not None and active_worker.is_alive():
                active_worker.join(timeout=6.0)
            if keepalive_running:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with self._lock:
                        if not st.keepalive_in_flight:
                            break
                    time.sleep(0.02)

            lock_ctx = runtime_file_lock(self.storage, slot=slot_name)
            entered = False
            lock_deadline = time.monotonic() + (3.0 if (active_worker is not None or keepalive_running) else 0.0)
            while True:
                try:
                    lock_ctx.__enter__()
                    entered = True
                    break
                except RuntimeBusyError:
                    if time.monotonic() >= lock_deadline:
                        raise
                    time.sleep(0.05)
            confirm_result: dict | None = None
            try:
                if not force:
                    local_busy_now = bool(callable(local_busy_provider) and local_busy_provider())
                    if local_busy_now:
                        rq_now, qu_now = remote_queue, queue_unknown
                    else:
                        rq_now, qu_now, _ = _check_remote_queue_strict(allow_cached_absent=session_verified_absent)
                    if local_busy_now or rq_now > 0 or qu_now:
                        with self._lock:
                            st.cancel_event.clear()
                            st.stopping = False
                            owns_stopping = False
                        confirm_result = {
                            "stopped": False,
                            "slot": slot_name,
                            "confirmation_required": True,
                            "local_busy": local_busy_now,
                            "remote_queue": rq_now,
                            "queue_unknown": qu_now,
                        }
                if confirm_result is None:
                    with self._lock:
                        st.cancel_event.clear()
                        st.last_error = None
                    rc, out = self._run_cmd_for_slot(
                        colab_bin,
                        settings["colab_auth"],
                        ["stop", "-s", session_name],
                        environ=env_copy,
                        timeout=45.0,
                        slot=slot_name,
                    )
                    if rc != 0:
                        if _is_cli_auth_required_output(out):
                            with self._lock:
                                self._cli_auth_required_hint = True
                                self._sessions_cache = None
                            raise RuntimeError(message("colab_cli_auth_required"))
                        raise RuntimeError(out.strip()[-240:] or f"colab stop exit {rc}")
                    stop_succeeded = True
                    if slot_name == "secondary":
                        stopped_sec_old_url, _ = self._set_remembered_node("secondary", None)
                    else:
                        with self._lock:
                            sec_st_chk = self._slots["secondary"]
                            saved_sec_chk = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
                            sec_running_chk = bool(saved_sec_chk or sec_st_chk.active_state or self._is_stopping(sec_st_chk))
                        if not sec_running_chk:
                            self.stop_keepalive_monitor()
            finally:
                if entered:
                    lock_ctx.__exit__(None, None, None)
            if confirm_result is not None:
                confirm_result["status"] = self.status(env_copy)
                return confirm_result
        finally:
            if owns_stopping:
                with self._lock:
                    if stop_succeeded or st.active_state is None:
                        st.cancel_event.clear()
                    st.stopping = False
                    self._sessions_cache = None
                    if (
                        stop_succeeded
                        and slot_name == "primary"
                        and not self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
                        and not self._slots["secondary"].active_state
                        and not self._is_stopping(self._slots["secondary"])
                    ):
                        self._last_activity_at = None

        if slot_name == "secondary":
            if stopped_sec_old_url:
                updated_urls = merge_ephemeral_url(settings["comfyui_urls"], stopped_sec_old_url, None)
                env_copy["COMFYUI_URL"] = config.format_endpoint_urls(updated_urls)
            return {
                "stopped": True,
                "slot": slot_name,
                "confirmation_required": False,
                "local_busy": bool(local_busy),
                "remote_queue": 0,
                "status": self.status(env_copy),
            }

        with self._lock:
            saved_sec = self.remembered_node("secondary", api_key=settings["comfyui_api_key"])
            sec_st = self._slots["secondary"]
            sec_slot_active = bool(sec_st.active_state or sec_st.last_error or self._is_stopping(sec_st))
        primary_url = self._primary_url(settings)
        remaining_urls = [u for u in settings["comfyui_urls"] if not primary_url or u != primary_url]
        if saved_sec or remaining_urls or sec_slot_active:
            probe_other_env = {**env_copy, "COMFYUI_URL": config.format_endpoint_urls(remaining_urls)}
            other_status = self.status(probe_other_env)
            other_urls = list(other_status.get("comfyui_urls") or [])
            other_url_set = set(other_urls)
            merged_urls = [u for u in settings["comfyui_urls"] if u == primary_url or u in other_url_set]
            for u in other_urls:
                if u not in merged_urls:
                    merged_urls.append(u)
            nodes_list = [n for n in (other_status.get("nodes") or []) if n.get("url") != primary_url]
            if primary_url:
                nodes_list.insert(0, {
                    "url": primary_url,
                    "label": comfy.endpoint_label(primary_url),
                    "online": False,
                    "auth_failed": False,
                    "status_code": None,
                    "devices": [],
                    "comfyui_version": None,
                    "queue_running": 0,
                    "queue_pending": 0,
                })
            managed_nodes = []
            for entry in (other_status.get("managed_nodes") or []):
                if isinstance(entry, dict) and entry.get("slot") == "primary":
                    managed_nodes.append({
                        **entry,
                        "url": primary_url,
                        "label": comfy.endpoint_label(primary_url) if primary_url else "",
                        "state": "offline",
                        "online": False,
                        "auth_failed": False,
                        "has_session": False,
                        "devices": [],
                        "queue_running": 0,
                        "queue_pending": 0,
                        "auth_url": None,
                        "error": None,
                        "logs": self._logs_tail("primary"),
                    })
                else:
                    managed_nodes.append(entry)
            online_nodes = [n for n in nodes_list if n.get("online")]
            status_payload = {
                **other_status,
                "state": "online" if online_nodes else "offline",
                "comfyui_url": merged_urls[0] if merged_urls else primary_url,
                "comfyui_urls": merged_urls,
                "nodes": nodes_list,
                "online_count": len(online_nodes),
                "total_count": len(nodes_list),
                "managed_nodes": managed_nodes,
                "has_session": False,
            }
        else:
            primary_node = {
                "url": primary_url,
                "label": comfy.endpoint_label(primary_url) if primary_url else "",
                "online": False,
                "auth_failed": False,
                "status_code": None,
                "devices": [],
                "comfyui_version": None,
                "queue_running": 0,
                "queue_pending": 0,
            }
            status_payload = {
                "state": "offline",
                "provider": settings["provider"],
                "comfyui_url": primary_url,
                "comfyui_urls": [primary_url] if primary_url else [],
                "nodes": [primary_node] if primary_url else [],
                "online_count": 0,
                "total_count": 1 if primary_url else 0,
                "managed_nodes": [
                    {
                        "slot": "primary",
                        "session": session_name,
                        "tunnel": self._tunnel_provider("primary"),
                        "ephemeral": False,
                        "url": primary_url,
                        "label": comfy.endpoint_label(primary_url) if primary_url else "",
                        "state": "offline",
                        "online": False,
                        "auth_failed": False,
                        "has_session": False,
                        "gpu": settings["colab_gpu"],
                        "devices": [],
                        "queue_running": 0,
                        "queue_pending": 0,
                        "auth_url": None,
                        "error": None,
                        "logs": self._logs_tail("primary"),
                    },
                    {
                        "slot": "secondary",
                        "session": self._session_name(settings, "secondary"),
                        "tunnel": self._tunnel_provider("secondary"),
                        "ephemeral": True,
                        "url": "",
                        "label": "",
                        "state": "offline",
                        "online": False,
                        "auth_failed": False,
                        "has_session": False,
                        "gpu": settings["colab_gpu"],
                        "devices": [],
                        "queue_running": 0,
                        "queue_pending": 0,
                        "auth_url": None,
                        "error": None,
                        "logs": self._logs_tail("secondary"),
                    },
                ],
                "session": session_name,
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": False,
                "devices": [],
                "auth_url": None,
                "error": None,
                "orphan_sessions": [],
                "logs": self._logs_tail("primary"),
            }
        return {
            "stopped": True,
            "slot": slot_name,
            "confirmation_required": False,
            "local_busy": bool(local_busy),
            "remote_queue": 0,
            "status": status_payload,
        }
