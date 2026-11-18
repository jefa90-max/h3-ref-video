"""Cross-platform Colab A100 lifecycle controller for remote ComfyUI.

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
REMOTE_START_CODE = (
    "import subprocess, sys\n"
    "p = subprocess.Popen(\n"
    "    [sys.executable, '-u', '/content/drive/MyDrive/MiniMax_ComfyUI/start_colab.py'],\n"
    "    stdout=subprocess.PIPE,\n"
    "    stderr=subprocess.STDOUT,\n"
    "    text=True,\n"
    ")\n"
    "for line in p.stdout:\n"
    "    print(line, end='', flush=True)\n"
    "rc = p.wait()\n"
    "if rc != 0:\n"
    "    raise SystemExit(f'start_colab.py exited with {rc}')\n"
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


def _popen_kwargs(environ=None, *, os_name: str | None = None) -> dict:
    active_os = os.name if os_name is None else os_name
    base_env = dict(os.environ if environ is None else environ)
    for secret_key in _STRIPPED_PROJECT_SECRETS:
        base_env.pop(secret_key, None)
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


def _probe_json(url: str, key: str, *, timeout: float = 4.0) -> tuple[int | None, dict | None]:
    headers = {"User-Agent": config.UA, "ngrok-skip-browser-warning": "1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    open_fn = http._OPENER.open if urllib.request.urlopen is http._ORIG_URLOPEN else urllib.request.urlopen
    try:
        with open_fn(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            return resp.status, payload if isinstance(payload, dict) else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return None, None


def probe_comfyui(environ=None, *, timeout: float = 4.0) -> dict:
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


def probe_remote_queue_count(environ=None, *, timeout: float = 4.0) -> int:
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
    def __init__(self, storage: Path, *, drive_auth_timeout: float = DRIVE_AUTH_TIMEOUT_SEC):
        self.storage = Path(storage)
        self.drive_auth_timeout = float(drive_auth_timeout)
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

        def reader():
            try:
                for line in proc.stdout:
                    collected.append(line)
                    self._append_log(line)
            except Exception:
                pass

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            rc = proc.wait(timeout=timeout)
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

        def read_output():
            buf = ""
            try:
                while True:
                    ch = proc.stdout.read(1)
                    if not ch:
                        if buf:
                            lines.append(buf)
                            self._append_log(buf)
                        break
                    buf += ch
                    if ch == "\n" or "Press Enter after you have granted access" in buf:
                        lines.append(buf)
                        self._append_log(buf)
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
                if time.monotonic() >= deadline:
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
                        colab_bin, auth_mode, ["new", "-s", session_name, "--gpu", "A100"],
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

                rc, mount_out, timed_out_auth = self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy)
                if self._cancel_event.is_set():
                    return
                if timed_out_auth:
                    raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                if "Credentials propagated" in mount_out and "mount failed" in mount_out and "Mounted at" not in mount_out:
                    rc, mount_out, timed_out_auth = self._run_drivemount_once(colab_bin, auth_mode, session_name, env_copy)
                    if self._cancel_event.is_set():
                        return
                    if timed_out_auth:
                        raise RuntimeError(message("colab_drive_timeout", seconds=int(self.drive_auth_timeout)))
                if rc != 0 or ("Mounted at" not in mount_out and "already mounted" not in mount_out.lower()):
                    raise RuntimeError(message("colab_start_failed", reason=mount_out.strip()[-300:] or f"drivemount exit {rc}"))

                with self._lock:
                    self._active_state = "starting_comfy"
                rc, exec_out = self._run_command(
                    colab_bin,
                    auth_mode,
                    ["exec", "-s", session_name, "--timeout", "1200"],
                    environ=env_copy,
                    stdin_text=REMOTE_START_CODE,
                    timeout=1260.0,
                )
                if self._cancel_event.is_set():
                    return
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

    def ensure_started(self, environ=None, *, timeout: float = 1500.0) -> dict:
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
            proc_to_cancel = self._active_proc
            if active_worker is not None:
                self._cancel_event.set()
                self._drive_ack_event.set()
        if proc_to_cancel is not None:
            _kill_process_tree(proc_to_cancel)
        if active_worker is not None and active_worker.is_alive():
            active_worker.join(timeout=5.0)

        with runtime_file_lock(self.storage):
            with self._lock:
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
                    self._active_state = None
                    self._sessions_cache = None
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
                "cli_available": True,
                "has_session": False,
                "devices": [],
                "auth_url": None,
                "error": None,
                "orphan_sessions": [],
                "logs": self._logs_tail(),
            },
        }
