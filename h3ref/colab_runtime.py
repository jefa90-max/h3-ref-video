"""Cross-platform Colab GPU lifecycle controller for remote ComfyUI.

Decoupled from h3ref/comfy.py: machines without google-colab-cli still probe /system_stats
and run full video generation whenever the remote ComfyUI endpoint is online.
"""
from __future__ import annotations

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
import urllib.request
from pathlib import Path

from . import config, http
from .i18n import message, use_lang

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
DRIVE_AUTH_TIMEOUT_SEC = 600.0
SESSION_CACHE_TTL_SEC = 5.0
CRASH_GRACE_SEC = 1.5
DEFAULT_REQUEST_TIMEOUT_SEC = "90"
# Colab reclaims headless/disconnected runtimes after ~20 min without a kernel client;
# keep active-job heartbeats well below that threshold.
KERNEL_KEEPALIVE_INTERVAL_SEC = 8 * 60.0
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
_SESSION_LINE_RE = re.compile(r"^\[(?P<name>[^\]]+)\]\s+(?P<endpoint>\S+)\s+\|\s+Hardware:\s*(?P<hardware>[^|]+)")


class RuntimeBusyError(RuntimeError):
    """Another Colab lifecycle operation holds the runtime lock."""


class RuntimeUnavailableError(RuntimeError):
    """Colab CLI is unavailable or called from an disallowed worker context."""


@contextmanager
def runtime_file_lock(storage: Path):
    storage.mkdir(parents=True, exist_ok=True)
    with (storage / "colab-runtime.lock").open("a+b") as lock:
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


def _is_transient_ws_disconnect(output: str) -> bool:
    return any(marker in output for marker in _TRANSIENT_WS_MARKERS)


def _is_terminal_cli_crash_line(line: str, saw_rich_box_end: bool) -> tuple[bool, bool]:
    stripped = line.strip()
    if "╰─" in stripped and "╯" in stripped:
        return False, True
    if "Connection was lost." in stripped or "WebSocketConnectionClosedException" in stripped:
        return True, saw_rich_box_end
    if saw_rich_box_end and stripped:
        return True, False
    return False, saw_rich_box_end


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
    if os.name != "nt":
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


def probe_comfyui(environ=None, *, timeout: float = 12.0) -> dict:
    settings = config.video_settings(environ)
    url = settings["comfyui_url"]
    if not url:
        return {"online": False, "configured": False, "devices": []}
    status_code, payload = _probe_json(f"{url}/system_stats", settings["comfyui_api_key"], timeout=timeout)
    if status_code == 200 and isinstance(payload, dict):
        devices = [
            {"name": d.get("name"), "vram_total": d.get("vram_total"), "vram_free": d.get("vram_free")}
            for d in (payload.get("devices") or []) if isinstance(d, dict)
        ]
        version = (payload.get("system") or {}).get("comfyui_version") if isinstance(payload.get("system"), dict) else None
        return {"online": True, "configured": True, "devices": devices, "comfyui_version": version}
    if status_code in (401, 403):
        return {"online": False, "configured": True, "auth_failed": True, "status_code": status_code, "devices": []}
    return {"online": False, "configured": True, "devices": []}


def probe_remote_queue_count(environ=None, *, timeout: float = 12.0) -> int:
    settings = config.video_settings(environ)
    url = settings["comfyui_url"]
    if not url:
        return 0
    status_code, payload = _probe_json(f"{url}/queue", settings["comfyui_api_key"], timeout=timeout)
    if status_code == 200 and isinstance(payload, dict):
        running = payload.get("queue_running") or []
        pending = payload.get("queue_pending") or []
        return (len(running) if isinstance(running, list) else 0) + (len(pending) if isinstance(pending, list) else 0)
    return 0


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


class ColabRuntimeManager:
    def __init__(
        self,
        storage: Path,
        *,
        drive_auth_timeout: float = DRIVE_AUTH_TIMEOUT_SEC,
        crash_grace_sec: float = CRASH_GRACE_SEC,
        retry_delay_sec: float = 4.0,
        keepalive_interval_sec: float = KERNEL_KEEPALIVE_INTERVAL_SEC,
        keepalive_poll_sec: float = KERNEL_KEEPALIVE_POLL_SEC,
        keepalive_retry_cooldown_sec: float = KERNEL_KEEPALIVE_RETRY_COOLDOWN_SEC,
        clock=time.monotonic,
    ):
        self.storage = Path(storage)
        self.drive_auth_timeout = float(drive_auth_timeout)
        self.crash_grace_sec = float(crash_grace_sec)
        self.retry_delay_sec = float(retry_delay_sec)
        self.keepalive_interval_sec = float(keepalive_interval_sec)
        self.keepalive_poll_sec = float(keepalive_poll_sec)
        self.keepalive_retry_cooldown_sec = float(keepalive_retry_cooldown_sec)
        self._clock = clock
        self._lock = threading.RLock()
        self._active_state: str | None = None
        self._auth_url: str | None = None
        self._last_error: str | None = None
        self._logs: list[str] = []
        self._drive_ack_event = threading.Event()
        self._cancel_event = threading.Event()
        self._drive_proc = None
        self._active_proc = None
        self._worker_thread: threading.Thread | None = None
        self._sessions_cache: tuple[float, dict] | None = None
        self._last_keepalive_at = float(self._clock())
        self._last_keepalive_fail_at: float | None = None
        self._keepalive_in_flight = False
        self._keepalive_stop = threading.Event()
        self._keepalive_env_provider = None
        self._keepalive_thread: threading.Thread | None = None

    def _logs_tail(self) -> list[str]:
        with self._lock:
            return list(self._logs[-15:])

    def _append_log(self, line: str) -> None:
        cleaned = line.rstrip("\r\n")
        if not cleaned:
            return
        with self._lock:
            self._logs.append(cleaned)
            if len(self._logs) > 80:
                self._logs = self._logs[-80:]

    def _run_command(self, colab_bin: str, auth_mode: str, sub_args: list[str], *,
                     environ=None, stdin_text: str | None = None, timeout: float = 60.0) -> tuple[int, str]:
        argv = _build_argv(colab_bin, auth_mode, sub_args)
        kwargs = _popen_kwargs(environ)
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        with self._lock:
            self._active_proc = proc
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
            try:
                for line in proc.stdout:
                    collected.append(line)
                    self._append_log(line)
                    crashed, saw_rich_box_end = _is_terminal_cli_crash_line(line, saw_rich_box_end)
                    if crashed:
                        cli_crashed.set()
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        crash_deadline: float | None = None
        try:
            deadline = time.monotonic() + timeout
            while proc.poll() is None:
                if self._cancel_event.is_set():
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
                    _kill_process_tree(proc)
                    break
                time.sleep(0.02)
            rc = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            rc = -9
        finally:
            with self._lock:
                if self._active_proc is proc:
                    self._active_proc = None
            t.join(timeout=2.0)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        return rc, "".join(collected)

    def _query_sessions(self, colab_bin: str, auth_mode: str, environ=None, *, force: bool = False) -> dict:
        now = time.monotonic()
        with self._lock:
            if not force and self._sessions_cache and (now - self._sessions_cache[0]) < SESSION_CACHE_TTL_SEC:
                return self._sessions_cache[1]
        rc, out = self._run_command(colab_bin, auth_mode, ["sessions"], environ=environ, timeout=25.0)
        parsed = parse_sessions_output(out) if rc == 0 else {"named": [], "orphans": []}
        with self._lock:
            self._sessions_cache = (time.monotonic(), parsed)
        return parsed

    def status(self, environ=None) -> dict:
        settings = config.video_settings(environ)
        colab_bin = find_colab_bin(environ)
        cli_available = bool(colab_bin)
        probe = probe_comfyui(environ)
        if probe["online"]:
            with self._lock:
                self._last_error = None
            return {
                "state": "online",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "devices": probe.get("devices", []),
                "comfyui_version": probe.get("comfyui_version"),
                "auth_url": None,
                "error": None,
                "orphan_sessions": [],
                "logs": self._logs_tail(),
            }
        if probe.get("auth_failed"):
            err_msg = message("colab_auth_rejected", code=probe.get("status_code", 401))
            return {
                "state": "error",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "devices": [],
                "auth_url": None,
                "error": err_msg,
                "orphan_sessions": [],
                "logs": self._logs_tail(),
            }

        with self._lock:
            active = self._active_state
            auth_url = self._auth_url
            last_error = self._last_error
            logs_tail = list(self._logs[-15:])

        if active in ("provisioning", "awaiting_drive_auth", "starting_comfy", "stopping"):
            return {
                "state": active,
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": cli_available,
                "devices": [],
                "auth_url": auth_url if active == "awaiting_drive_auth" else None,
                "error": None,
                "orphan_sessions": [],
                "logs": logs_tail,
            }

        if not cli_available:
            return {
                "state": "cli_unavailable",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": False,
                "devices": [],
                "auth_url": None,
                "error": last_error,
                "orphan_sessions": [],
                "logs": logs_tail,
            }

        sessions = self._query_sessions(colab_bin, settings["colab_auth"], environ=environ)
        has_named = any(s["name"] == settings["colab_session"] for s in sessions["named"])
        if sessions["orphans"]:
            return {
                "state": "orphan_warning",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": has_named,
                "devices": [],
                "auth_url": None,
                "error": last_error,
                "orphan_sessions": sessions["orphans"],
                "logs": logs_tail,
            }
        if last_error:
            return {
                "state": "error",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": has_named,
                "devices": [],
                "auth_url": None,
                "error": last_error,
                "orphan_sessions": [],
                "logs": logs_tail,
            }
        return {
            "state": "offline",
            "provider": settings["provider"],
            "comfyui_url": settings["comfyui_url"],
            "session": settings["colab_session"],
            "gpu": settings["colab_gpu"],
            "cli_available": True,
            "has_session": has_named,
            "devices": [],
            "auth_url": None,
            "error": None,
            "orphan_sessions": [],
            "logs": logs_tail,
        }

    def submit_drive_authorized(self) -> dict:
        with self._lock:
            proc = self._drive_proc
            if self._active_state != "awaiting_drive_auth" or proc is None or proc.stdin is None:
                return {"accepted": False, "state": self._active_state or "offline"}
            try:
                proc.stdin.write("\n")
                proc.stdin.flush()
            except OSError:
                return {"accepted": False, "state": self._active_state}
            self._active_state = "starting_comfy"
            self._auth_url = None
            self._drive_ack_event.set()
            return {"accepted": True, "state": "starting_comfy"}

    def _run_drivemount_once(self, colab_bin: str, auth_mode: str, session_name: str, environ) -> tuple[int, str, bool]:
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
            self._drive_proc = proc
            self._active_proc = proc
            self._drive_ack_event.clear()

        lines: list[str] = []
        prompted = threading.Event()
        cli_crashed = threading.Event()

        def read_output():
            buf = ""
            saw_rich_box_end = False
            try:
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        if buf:
                            lines.append(buf)
                            self._append_log(buf)
                            crashed, saw_rich_box_end = _is_terminal_cli_crash_line(buf, saw_rich_box_end)
                            if crashed:
                                cli_crashed.set()
                        break
                    buf += ch
                    if ch == "\n" or "Press Enter after you have granted access" in buf:
                        lines.append(buf)
                        self._append_log(buf)
                        crashed, saw_rich_box_end = _is_terminal_cli_crash_line(buf, saw_rich_box_end)
                        if crashed:
                            cli_crashed.set()
                        m = _URL_RE.search(buf)
                        if m:
                            with self._lock:
                                self._auth_url = m.group(1).strip()
                                self._active_state = "awaiting_drive_auth"
                            prompted.set()
                        elif "Press Enter after you have granted access" in buf:
                            with self._lock:
                                if self._active_state != "awaiting_drive_auth":
                                    self._active_state = "awaiting_drive_auth"
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
                if self._cancel_event.is_set():
                    _kill_process_tree(proc)
                    break
                if prompted.is_set() and not self._drive_ack_event.is_set():
                    if not self._drive_ack_event.wait(timeout=self.drive_auth_timeout):
                        timed_out_auth = True
                        _kill_process_tree(proc)
                        break
                    if self._cancel_event.is_set():
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
            reader.join(timeout=2.0)
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            with self._lock:
                self._drive_proc = None
                if self._active_proc is proc:
                    self._active_proc = None
                self._auth_url = None
        return rc, "".join(lines), timed_out_auth

    def _startup_worker(self, env_copy: dict, colab_bin: str, lock_ctx) -> None:
        settings = config.video_settings(env_copy)
        session_name = settings["colab_session"]
        auth_mode = settings["colab_auth"]
        try:
            with use_lang(env_copy.get("H3_LANG")):
                sessions = self._query_sessions(colab_bin, auth_mode, environ=env_copy, force=True)
                if self._cancel_event.is_set():
                    return
                has_target = any(s["name"] == session_name for s in sessions["named"])
                if not has_target and sessions["orphans"]:
                    endpoints = ", ".join(s["endpoint"] for s in sessions["orphans"])
                    raise RuntimeError(message("colab_orphan_blocked", sessions=endpoints))

                if not has_target:
                    with self._lock:
                        self._active_state = "provisioning"
                    rc, out = self._run_command(
                        colab_bin, auth_mode, _new_session_args(session_name, settings["colab_gpu"]),
                        environ=env_copy, timeout=180.0,
                    )
                    if self._cancel_event.is_set():
                        return
                    if rc != 0 or "Session READY" not in out:
                        raise RuntimeError(message("colab_start_failed", reason=out.strip()[-300:] or f"colab new exit {rc}"))
                    with self._lock:
                        self._sessions_cache = None

                with self._lock:
                    self._active_state = "starting_comfy"

                drive_already_mounted = False
                if has_target:
                    ls_rc, ls_out = self._run_command(
                        colab_bin, auth_mode, ["ls", "/content/drive", "-s", session_name],
                        environ=env_copy, timeout=20.0,
                    )
                    drive_already_mounted = ls_rc == 0 and "MyDrive/" in ls_out

                if not drive_already_mounted:
                    mount_out = ""
                    rc = 1
                    timed_out_auth = False
                    for attempt in range(8):
                        rc, mount_out, timed_out_auth = self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy)
                        if self._cancel_event.is_set():
                            return
                        if timed_out_auth:
                            raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                        if _is_transient_ws_disconnect(mount_out) and "Mounted at" not in mount_out and "already mounted" not in mount_out.lower() and attempt < 7:
                            time.sleep(5.0)
                            continue
                        break
                    if "Credentials propagated" in mount_out and "Mounted at" not in mount_out and "already mounted" not in mount_out.lower():
                        rc, mount_out, timed_out_auth = self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy)
                        if self._cancel_event.is_set():
                            return
                        if timed_out_auth:
                            raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                    if rc != 0 or ("Mounted at" not in mount_out and "already mounted" not in mount_out.lower()):
                        raise RuntimeError(message("colab_start_failed", reason=mount_out.strip()[-300:] or f"drivemount exit {rc}"))

                with self._lock:
                    self._active_state = "starting_comfy"
                local_start_script = config.ROOT / "scripts" / "start_colab.py"
                if local_start_script.is_file():
                    for _ in range(2):
                        up_rc, _ = self._run_command(
                            colab_bin,
                            auth_mode,
                            ["upload", "-s", session_name, str(local_start_script), "content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py"],
                            environ=env_copy,
                            timeout=60.0,
                        )
                        if self._cancel_event.is_set():
                            return
                        if up_rc == 0:
                            break
                        time.sleep(2.0)
                exec_out = ""
                rc = 1
                for attempt in range(6):
                    if self._cancel_event.is_set():
                        return
                    if attempt > 0 and probe_comfyui(env_copy)["online"]:
                        rc = 0
                        exec_out = ""
                        break
                    rc, exec_out = self._run_command(
                        colab_bin,
                        auth_mode,
                        ["exec", "-s", session_name, "--timeout", "1800"],
                        environ=env_copy,
                        stdin_text=REMOTE_START_CODE,
                        timeout=2400.0,
                    )
                    if self._cancel_event.is_set():
                        return
                    if rc == 0 and "Traceback (most recent call last)" not in exec_out:
                        break
                    if _is_transient_ws_disconnect(exec_out) and attempt < 5:
                        time.sleep(self.retry_delay_sec)
                        continue
                    break
                if rc != 0 or "Traceback (most recent call last)" in exec_out:
                    raise RuntimeError(message("colab_start_failed", reason=exec_out.strip()[-300:] or f"colab exec exit {rc}"))

                for _ in range(10):
                    if self._cancel_event.is_set():
                        return
                    if probe_comfyui(env_copy)["online"]:
                        with self._lock:
                            self._last_error = None
                            self._active_state = None
                            self._sessions_cache = None
                            self._last_keepalive_at = float(self._clock())
                            self._last_keepalive_fail_at = None
                        return
                    time.sleep(1.5)
                raise RuntimeError(message("colab_start_failed", reason=message("colab_tunnel_unready")))
        except Exception as exc:
            with self._lock:
                if not self._cancel_event.is_set():
                    self._last_error = str(exc)
                self._active_state = None
                self._auth_url = None
                self._sessions_cache = None
        finally:
            with self._lock:
                if self._cancel_event.is_set():
                    self._active_state = None
                    self._auth_url = None
            lock_ctx.__exit__(None, None, None)

    def start_async(self, environ=None) -> dict:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        probe = probe_comfyui(env_copy)
        if probe["online"]:
            return self.status(env_copy)
        if not settings["comfyui_url"]:
            raise ValueError(message("colab_url_missing"))

        with self._lock:
            if self._active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy"):
                return self.status(env_copy)
            if self._active_state == "stopping":
                raise RuntimeBusyError(message("colab_busy"))

        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            raise RuntimeUnavailableError(message("colab_cli_missing"))

        lock_ctx = runtime_file_lock(self.storage)
        lock_ctx.__enter__()
        try:
            with self._lock:
                self._cancel_event.clear()
                self._last_error = None
                self._auth_url = None
                self._logs.clear()
                self._active_state = "provisioning"
            t = threading.Thread(
                target=self._startup_worker,
                args=(env_copy, colab_bin, lock_ctx),
                daemon=True,
            )
            with self._lock:
                self._worker_thread = t
            t.start()
        except BaseException:
            lock_ctx.__exit__(None, None, None)
            raise
        return self.status(env_copy)

    def ensure_started(self, environ=None, *, timeout: float = 2700.0) -> dict:
        cur = self.start_async(environ)
        if cur["state"] == "online":
            return cur
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            cur = self.status(environ)
            if cur["state"] == "online":
                return cur
            if cur["state"] in ("error", "orphan_warning", "offline", "cli_unavailable"):
                raise RuntimeError(cur.get("error") or f"Colab startup ended in state {cur['state']}")
            time.sleep(1.0)
        raise TimeoutError(f"Timed out after {timeout}s waiting for Colab ComfyUI runtime")

    def maybe_send_kernel_keepalive(
        self,
        environ=None,
        *,
        now: float | None = None,
        queue_count: int | None = None,
        force_busy: bool = False,
    ) -> bool:
        """Send a lightweight Colab kernel heartbeat only while ComfyUI work is active."""
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return False
        env_copy = dict(os.environ if environ is None else environ)
        try:
            settings = config.video_settings(env_copy)
        except ValueError:
            return False
        if settings["provider"] != "comfyui" or not settings["comfyui_url"]:
            return False
        current = float(self._clock()) if now is None else float(now)
        with self._lock:
            if self._active_state is not None or self._active_proc is not None or self._keepalive_in_flight:
                return False
            if (current - self._last_keepalive_at) < self.keepalive_interval_sec:
                return False
            if (
                self._last_keepalive_fail_at is not None
                and (current - self._last_keepalive_fail_at) < self.keepalive_retry_cooldown_sec
            ):
                return False
        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            return False

        if force_busy:
            active_queue = 1 if queue_count is None else max(1, int(queue_count))
        else:
            active_queue = probe_remote_queue_count(env_copy, timeout=6.0) if queue_count is None else int(queue_count)
            if active_queue <= 0:
                return False

        try:
            with runtime_file_lock(self.storage):
                with self._lock:
                    if self._active_state is not None or self._active_proc is not None or self._keepalive_in_flight:
                        return False
                    self._keepalive_in_flight = True
                try:
                    rc, out = self._run_command(
                        colab_bin,
                        settings["colab_auth"],
                        ["exec", "-s", settings["colab_session"], "--timeout", str(KERNEL_KEEPALIVE_TIMEOUT_SEC)],
                        environ=env_copy,
                        stdin_text=KERNEL_KEEPALIVE_CODE,
                        timeout=float(KERNEL_KEEPALIVE_TIMEOUT_SEC) + 15.0,
                    )
                    if rc == 0 and "Traceback (most recent call last)" not in out:
                        with self._lock:
                            self._last_keepalive_at = current
                            self._last_keepalive_fail_at = None
                        self._append_log(
                            f"[keepalive] Kernel keepalive sent (session={settings['colab_session']}, queue={active_queue})"
                        )
                        return True
                    detail = (out.strip() or f"exit {rc}")[-200:]
                    with self._lock:
                        self._last_keepalive_fail_at = current
                    self._append_log(f"[keepalive] Kernel keepalive failed (rc={rc}): {detail}")
                    return False
                finally:
                    with self._lock:
                        self._keepalive_in_flight = False
        except RuntimeBusyError:
            return False
        except Exception as exc:
            with self._lock:
                self._last_keepalive_fail_at = current
            self._append_log(f"[keepalive] Kernel keepalive error: {type(exc).__name__}: {exc}")
            return False

    def trigger_keepalive_async(self, environ=None, *, force_busy: bool = False) -> None:
        """Non-blocking keepalive check when launching a real ComfyUI video job."""
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return
        env_copy = dict(os.environ if environ is None else environ)
        current = float(self._clock())
        with self._lock:
            if self._active_state is not None or self._active_proc is not None or self._keepalive_in_flight:
                return
            if (current - self._last_keepalive_at) < self.keepalive_interval_sec:
                return
        threading.Thread(
            target=self.maybe_send_kernel_keepalive,
            args=(env_copy,),
            kwargs={"force_busy": force_busy},
            name="colab-kernel-keepalive-once",
            daemon=True,
        ).start()

    def start_keepalive_monitor(self, env_provider, *, poll_sec: float | None = None) -> None:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            return
        interval = max(0.05, float(self.keepalive_poll_sec if poll_sec is None else poll_sec))
        with self._lock:
            self._keepalive_env_provider = env_provider
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return
            self._keepalive_stop.clear()

            def monitor_loop() -> None:
                while not self._keepalive_stop.wait(timeout=interval):
                    with self._lock:
                        provider = self._keepalive_env_provider
                    if provider is None:
                        continue
                    try:
                        current_env = provider()
                        self.maybe_send_kernel_keepalive(current_env)
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

    def stop(self, *, environ=None, force: bool = False, local_busy: bool = False) -> dict:
        if os.environ.get("H3_WORKBENCH_WORKER") == "1":
            raise RuntimeUnavailableError(message("colab_worker_forbidden"))
        env_copy = dict(os.environ if environ is None else environ)
        settings = config.video_settings(env_copy)
        colab_bin = find_colab_bin(env_copy)
        if not colab_bin:
            raise RuntimeUnavailableError(message("colab_cli_missing"))

        remote_queue = probe_remote_queue_count(env_copy)
        if not force and (local_busy or remote_queue > 0):
            return {
                "stopped": False,
                "confirmation_required": True,
                "local_busy": bool(local_busy),
                "remote_queue": remote_queue,
                "status": self.status(env_copy),
            }

        with self._lock:
            active_worker = self._worker_thread if self._active_state in ("provisioning", "awaiting_drive_auth", "starting_comfy") else None
            keepalive_running = self._keepalive_in_flight
            proc_to_cancel = self._active_proc
            if active_worker is not None or keepalive_running:
                self._cancel_event.set()
                self._drive_ack_event.set()
        if proc_to_cancel is not None:
            _kill_process_tree(proc_to_cancel)
        if active_worker is not None and active_worker.is_alive():
            active_worker.join(timeout=5.0)
        if keepalive_running:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with self._lock:
                    if not self._keepalive_in_flight:
                        break
                time.sleep(0.02)

        with runtime_file_lock(self.storage):
            with self._lock:
                self._cancel_event.clear()
                self._active_state = "stopping"
                self._last_error = None
            try:
                rc, out = self._run_command(
                    colab_bin,
                    settings["colab_auth"],
                    ["stop", "-s", settings["colab_session"]],
                    environ=env_copy,
                    timeout=45.0,
                )
                if rc != 0:
                    raise RuntimeError(out.strip()[-240:] or f"colab stop exit {rc}")
            finally:
                with self._lock:
                    self._cancel_event.clear()
                    self._active_state = None
                    self._sessions_cache = None
        self.stop_keepalive_monitor()
        return {
            "stopped": True,
            "confirmation_required": False,
            "local_busy": bool(local_busy),
            "remote_queue": 0,
            "status": {
                "state": "offline",
                "provider": settings["provider"],
                "comfyui_url": settings["comfyui_url"],
                "session": settings["colab_session"],
                "gpu": settings["colab_gpu"],
                "cli_available": True,
                "has_session": False,
                "devices": [],
                "auth_url": None,
                "error": None,
                "orphan_sessions": [],
                "logs": self._logs_tail(),
            },
        }
