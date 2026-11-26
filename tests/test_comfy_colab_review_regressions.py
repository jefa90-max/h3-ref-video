"""Regression tests for Pre-Landing Review findings on ComfyUI / Colab runtime & reverse proxy."""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import socket
import tempfile
import threading
import time
import types
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from PIL import Image

from h3ref import colab_runtime, comfy, config, film, http
from scripts import start_colab
from workbench import segments, server


class _BlockingPipeProc:
    """Fake subprocess that emits lines on demand and blocks until stdin or kill()."""

    def __init__(self, initial_lines: list[str], *, after_ack_lines: list[str] | None = None, exit_code: int = 0):
        self.pid = 99901
        self._after_ack_lines = list(after_ack_lines or [])
        self._exit_code = exit_code
        self._done = threading.Event()
        self._killed = False
        self._r_fd, self._w_fd = os.pipe()
        self.stdout = os.fdopen(self._r_fd, "r", encoding="utf-8", errors="replace", buffering=1)
        self._w = os.fdopen(self._w_fd, "w", encoding="utf-8", buffering=1)
        self.stdin = _AckWriter(self)
        for line in initial_lines:
            self._w.write(line)
        self._w.flush()

    def _on_ack(self) -> None:
        if self._killed or self._done.is_set():
            raise BrokenPipeError("process closed")
        for line in self._after_ack_lines:
            self._w.write(line)
        self._w.flush()
        self._finish(self._exit_code)

    def _finish(self, rc: int) -> None:
        self._exit_code = rc
        self._done.set()
        try:
            self._w.close()
        except OSError:
            pass

    def poll(self):
        return self._exit_code if self._done.is_set() else None

    def wait(self, timeout=None):
        self._done.wait(timeout=timeout)
        return self._exit_code

    def kill(self):
        self._killed = True
        self._finish(-9)


class _AckWriter:
    def __init__(self, proc: _BlockingPipeProc):
        self._proc = proc
        self.closed = False

    def write(self, data: str):
        if self.closed or self._proc.poll() is not None:
            raise BrokenPipeError("broken pipe")
        if "\n" in data:
            self._proc._on_ack()
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


class ComfyColabReviewRegressionsTest(unittest.TestCase):
    def test_query_sessions_failure_never_caches_wipes_secondary_or_double_provisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / ".workbench"
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            synced = []
            mgr = colab_runtime.ColabRuntimeManager(
                storage,
                on_ephemeral_url_change=lambda slot, old, new: synced.append((slot, old, new)),
            )
            sec_url = "https://alive-sec.trycloudflare.com"
            mgr._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_url, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-secret-1234")})
            synced.clear()
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"https://pri.ngrok-free.dev,{sec_url}",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }
            calls = []

            def fail_then_ok(colab_bin, auth_mode, sub_args, **kw):
                calls.append(list(sub_args))
                if len(calls) == 1:
                    return 1, "503 Service Unavailable"
                return 0, "[comfy-a100-2] m-s-2 | Hardware: G4\n"

            with mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "nodes": [], "online_count": 0, "total_count": 2}), \
                 mock.patch.object(mgr, "_run_command", side_effect=fail_then_ok):
                # 1. Transient `colab sessions` failure must NOT wipe remembered secondary or cache empty list
                mgr.status(env)
                self.assertEqual(mgr.remembered_node("secondary")["url"], sec_url)
                self.assertEqual(synced, [])
                # Next status call must re-query sessions (not hit 5s cache of failed result)
                st2 = mgr.status(env)
                self.assertEqual(len(calls), 2)
                sec_entry = next(m for m in st2["managed_nodes"] if m["slot"] == "secondary")
                self.assertTrue(sec_entry["has_session"])

            # 2. _startup_worker must abort instead of calling `colab new` when `colab sessions` fails
            new_calls = []

            def fake_start_cmd(colab_bin, auth_mode, sub_args, **kw):
                if sub_args == ["sessions"]:
                    return 1, "ConnectionError"
                if sub_args and sub_args[0] == "new":
                    new_calls.append(sub_args)
                return 0, ""

            with mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "nodes": [], "online_count": 0, "total_count": 1}), \
                 mock.patch.object(mgr, "_run_command", side_effect=fake_start_cmd):
                mgr.start_async(env, slot="primary")
                mgr._slots["primary"].worker_thread.join(timeout=5.0)
            self.assertEqual(new_calls, [], "Must never run `colab new` when session enumeration failed")
            self.assertIsNotNone(mgr._slots["primary"].last_error)

    def test_stop_primary_preserves_online_secondary_and_rejects_concurrent_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / ".workbench"
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(storage)
            pri_url = "https://pri.ngrok-free.dev"
            sec_url = "https://sec-live.trycloudflare.com"
            mgr._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_url, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-secret-1234")})
            mgr.mark_local_activity(now=500.0)
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"{pri_url},{sec_url}",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }

            def fake_probe(e=None, *, timeout=12.0):
                urls = config.video_settings(e)["comfyui_urls"]
                nodes = []
                for u in urls:
                    is_sec = u == sec_url
                    nodes.append({
                        "url": u,
                        "label": comfy.endpoint_label(u),
                        "online": is_sec,
                        "auth_failed": False,
                        "status_code": 200 if is_sec else None,
                        "devices": [{"name": "NVIDIA G4"}] if is_sec else [],
                        "comfyui_version": "0.3.0" if is_sec else None,
                        "queue_running": 0,
                        "queue_pending": 0,
                    })
                online = [n for n in nodes if n["online"]]
                return {
                    "online": bool(online),
                    "configured": bool(urls),
                    "devices": [d for n in online for d in n["devices"]],
                    "nodes": nodes,
                    "online_count": len(online),
                    "total_count": len(nodes),
                }

            with mock.patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe), \
                 mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr, "_run_cmd_for_slot", return_value=(0, "stopped")):
                res = mgr.stop(environ=env, force=True, local_busy=False, slot="primary")
            st = res["status"]
            self.assertEqual(st["state"], "online")
            self.assertEqual(st["online_count"], 1)
            self.assertEqual(st["total_count"], 2)
            self.assertIn("managed_nodes", st)
            by_slot = {m["slot"]: m for m in st["managed_nodes"]}
            self.assertEqual(by_slot["primary"]["state"], "offline")
            self.assertEqual(by_slot["secondary"]["state"], "online")
            self.assertIsNotNone(mgr._last_activity_at, "Stopping primary must not clear keepalive activity while secondary lives")

            # Concurrent stop while active_state == 'stopping' must raise RuntimeBusyError without killing active_proc
            killed = []
            dummy_proc = object()
            mgr._slots["primary"].active_state = "stopping"
            mgr._slots["primary"].active_proc = dummy_proc
            try:
                with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                     mock.patch.object(colab_runtime, "_kill_process_tree", side_effect=lambda p: killed.append(p)):
                    with self.assertRaises(colab_runtime.RuntimeBusyError):
                        mgr.stop(environ=env, force=True, slot="primary")
                self.assertEqual(killed, [], "Concurrent stop() must not kill the in-flight `colab stop` process")
            finally:
                mgr._slots["primary"].active_state = None
                mgr._slots["primary"].active_proc = None

    def test_primary_secondary_url_isolation_and_ensure_started_primary(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / ".workbench"
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            mgr = colab_runtime.ColabRuntimeManager(storage)
            sec_url = "https://only-sec.trycloudflare.com"
            mgr._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_url, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-secret-1234")})
            env_only_sec = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": sec_url,
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
            }
            probe_payload = {
                "online": True,
                "configured": True,
                "devices": [{"name": "G4"}],
                "nodes": [{"url": sec_url, "label": "only-sec.trycloudflare.com", "online": True, "auth_failed": False, "devices": [{"name": "G4"}], "queue_running": 0, "queue_pending": 0}],
                "online_count": 1,
                "total_count": 1,
            }
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=probe_payload), \
                 mock.patch.object(mgr, "_query_sessions", return_value={"named": [{"name": "comfy-a100-2", "endpoint": "m-2", "hardware": "G4"}], "orphans": [], "ok": True}):
                st = mgr.status(env_only_sec)
            by_slot = {m["slot"]: m for m in st["managed_nodes"]}
            self.assertEqual(by_slot["primary"]["url"], "")
            self.assertFalse(by_slot["primary"]["online"])
            self.assertTrue(by_slot["secondary"]["online"])
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=probe_payload):
                with self.assertRaises(ValueError):
                    mgr.start_async(env_only_sec, slot="primary")

            # ensure_started(slot="primary") must check primary managed_node, not top-level online (from secondary)
            seq = iter([
                {
                    "state": "online",
                    "managed_nodes": [
                        {"slot": "primary", "state": "provisioning", "error": None},
                        {"slot": "secondary", "state": "online", "error": None},
                    ],
                },
                {
                    "state": "online",
                    "managed_nodes": [
                        {"slot": "primary", "state": "error", "error": "primary boot failed"},
                        {"slot": "secondary", "state": "online", "error": None},
                    ],
                },
            ])
            with mock.patch.object(mgr, "start_async", side_effect=lambda *a, **kw: next(seq)), \
                 mock.patch.object(mgr, "status", side_effect=lambda *a, **kw: next(seq)), \
                 mock.patch("h3ref.colab_runtime.time.sleep"):
                with self.assertRaisesRegex(RuntimeError, "primary boot failed"):
                    mgr.ensure_started(env_only_sec, timeout=5.0, slot="primary")

    def test_secondary_drive_ack_and_cancel_during_auth_and_crash_unwedge(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / ".workbench"
            fake_bin = Path(tmp) / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")
            env = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://pri.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
            }

            # 1. State-matrix cell: secondary x drive_ack while primary remains online
            mgr = colab_runtime.ColabRuntimeManager(storage, drive_auth_timeout=10.0)
            sec_proc = _BlockingPipeProc(
                ["Visit https://accounts.google.com/o/oauth2/auth?sec=1\nPress Enter after you have granted access"],
                after_ack_lines=["Mounted at /content/drive\n"],
            )
            sec_ready = {"up": False}

            def fake_cmd(cb, am, args, **kw):
                if args == ["sessions"]:
                    return 0, "[comfy-a100-2] m-2 | Hardware: G4\n"
                if args[:2] == ["ls", "/content/drive"]:
                    return 1, "not mounted"
                if args[:2] == ["exec", "-s"]:
                    sec_ready["up"] = True
                    return 0, "[start_colab] TUNNEL READY (AUTH PROTECTED): https://sec-ack.trycloudflare.com\n"
                return 0, ""

            def fake_probe(e=None, *, timeout=12.0):
                urls = config.video_settings(e)["comfyui_urls"]
                nodes = []
                for u in urls:
                    online = (u == "https://pri.ngrok-free.dev") or (u == "https://sec-ack.trycloudflare.com" and sec_ready["up"])
                    nodes.append({"url": u, "label": comfy.endpoint_label(u), "online": online, "auth_failed": False, "devices": [], "queue_running": 0, "queue_pending": 0})
                on = [n for n in nodes if n["online"]]
                return {"online": bool(on), "configured": bool(urls), "devices": [], "nodes": nodes, "online_count": len(on), "total_count": len(nodes)}

            with mock.patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe), \
                 mock.patch.object(mgr, "_run_command", side_effect=fake_cmd), \
                 mock.patch("subprocess.Popen", return_value=sec_proc):
                mgr.start_async(env, slot="secondary")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    if mgr._slots["secondary"].active_state == "awaiting_drive_auth":
                        break
                    time.sleep(0.02)
                self.assertEqual(mgr._slots["secondary"].active_state, "awaiting_drive_auth")
                ack = mgr.submit_drive_authorized(slot="secondary")
                self.assertTrue(ack["accepted"])
                self.assertEqual(ack["slot"], "secondary")
                mgr._slots["secondary"].worker_thread.join(timeout=5.0)
            self.assertIsNone(mgr._slots["secondary"].last_error)
            self.assertEqual(mgr.remembered_node("secondary")["url"], "https://sec-ack.trycloudflare.com")

            # 2. State-matrix cell: secondary x cancel_during_auth leaves primary untouched
            mgr2 = colab_runtime.ColabRuntimeManager(storage / "m2", drive_auth_timeout=10.0)
            sec_proc2 = _BlockingPipeProc(
                ["Visit https://accounts.google.com/o/oauth2/auth?sec=2\nPress Enter after you have granted access"],
            )
            stopped_sessions = []

            def fake_cmd2(cb, am, args, **kw):
                if args == ["sessions"]:
                    return 0, "[comfy-a100-2] m-2 | Hardware: G4\n"
                if args[:2] == ["ls", "/content/drive"]:
                    return 1, "not mounted"
                if args[:2] == ["stop", "-s"]:
                    stopped_sessions.append(args[2])
                    return 0, "Stopped\n"
                return 0, ""

            with mock.patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe), \
                 mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr2, "_run_command", side_effect=fake_cmd2), \
                 mock.patch("subprocess.Popen", return_value=sec_proc2), \
                 mock.patch.object(colab_runtime, "_kill_process_tree", side_effect=lambda p: p.kill()):
                mgr2.start_async(env, slot="secondary")
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline and mgr2._slots["secondary"].active_state != "awaiting_drive_auth":
                    time.sleep(0.02)
                self.assertEqual(mgr2._slots["secondary"].active_state, "awaiting_drive_auth")
                stop_res = mgr2.stop(environ=env, force=True, slot="secondary")
            self.assertTrue(stop_res["stopped"])
            self.assertEqual(stopped_sessions, ["comfy-a100-2"])
            self.assertIsNone(mgr2._slots["secondary"].active_state)
            self.assertTrue(any(m["slot"] == "primary" and m["online"] for m in stop_res["status"]["managed_nodes"]))

            # 3. drivemount process exiting/crashing after printing URL must exit promptly (not wedge for 600s)
            mgr3 = colab_runtime.ColabRuntimeManager(storage / "m3", drive_auth_timeout=600.0)
            dying_proc = _BlockingPipeProc(
                ["Visit https://accounts.google.com/o/oauth2/auth?dying=1\nPress Enter after you have granted access"],
            )

            def kill_shortly():
                time.sleep(0.15)
                dying_proc._finish(1)

            threading.Thread(target=kill_shortly, daemon=True).start()
            t0 = time.monotonic()
            with mock.patch("subprocess.Popen", return_value=dying_proc):
                rc, _, timed_out = mgr3._run_drivemount_once(str(fake_bin), "", "comfy-a100", env)
            elapsed = time.monotonic() - t0
            self.assertEqual(rc, 1)
            self.assertFalse(timed_out)
            self.assertLess(elapsed, 3.0, "_run_drivemount_once must exit immediately when subprocess dies")

    def test_parse_tunnel_ready_url_whitelist_and_config_forgets_removed_secondary(self):
        # Secondary ephemeral parsing rejects arbitrary domains and paths, accepts only *.trycloudflare.com
        poisoned = (
            "[start_colab] TUNNEL READY (AUTH PROTECTED): https://good-node-42.trycloudflare.com\n"
            "[start_colab] TUNNEL READY (AUTH PROTECTED): https://evil.attacker.example/steal\n"
        )
        self.assertEqual(
            colab_runtime.parse_tunnel_ready_url(poisoned, ephemeral_only=True),
            "https://good-node-42.trycloudflare.com",
        )
        self.assertEqual(
            colab_runtime.parse_tunnel_ready_url(
                "[start_colab] TUNNEL READY (AUTH PROTECTED): https://evil.attacker.example\n",
                ephemeral_only=True,
            ),
            "",
        )

        # Removing secondary URL via /api/config clears colab-nodes.json so status() does not resurrect it
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_path = root / ".env"
            pri = "https://pri.ngrok-free.dev"
            sec = "https://sec-old.trycloudflare.com"
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri},{sec}\nCOMFYUI_API_KEY=old-key-1234\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)
            runtime._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("old-key-1234")})
            client = app.test_client()
            res = client.post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={"COMFYUI_URL": pri, "persist": True},
            )
            self.assertEqual(res.status_code, 200)
            self.assertIsNone(runtime.remembered_node("secondary"))

    def test_start_colab_watchdog_queue_authority_and_ws_status_clear(self):
        # 1. WS status event with queue_remaining=0 clears stale running state
        state = {"prompt_id": "p1", "status": "running", "node": "125"}
        start_colab._apply_comfy_ws_event(state, {"type": "status", "data": {"status": {"exec_info": {"queue_remaining": 0}}}}, now=100.0)
        self.assertEqual(state["status"], "finished")

        # 2. _queue_busy trusts /queue == empty even if progress_snapshot is stuck at running
        class _FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"queue_running": [], "queue_pending": []}'

        stuck_snap = lambda: {"status": "running", "prompt_id": "p-stuck", "updated_at": time.time()}
        with mock.patch("urllib.request.urlopen", return_value=_FakeResp()):
            self.assertFalse(start_colab._queue_busy(stuck_snap))

        # 3. Unused fl2v LoRA is no longer required in MODEL_FILES, while 3D latent upscaler is staged & auto-downloadable
        self.assertFalse(any("fl2v" in m for m in start_colab.MODEL_FILES))
        self.assertIn(start_colab.LATENT_UPSCALER_MODEL_REL, start_colab.MODEL_FILES)
        self.assertIn(start_colab.LATENT_UPSCALER_MODEL_REL, start_colab.AUTO_DOWNLOAD_MODELS)

        with tempfile.TemporaryDirectory() as tmp:
            fake_local = Path(tmp) / "ComfyUI_local"
            with mock.patch.object(start_colab, "LOCAL", fake_local), \
                 mock.patch.object(start_colab.subprocess, "run") as run_mock:
                start_colab.ensure_latent_upscaler_node()
                run_mock.assert_called_once()
                argv = run_mock.call_args.args[0]
                self.assertEqual(argv[:4], ["git", "clone", "--depth", "1"])
                self.assertEqual(argv[4], start_colab.LATENT_UPSCALER_REPO_URL)
                # Idempotent once __init__.py exists
                node_dir = fake_local / "custom_nodes" / start_colab.LATENT_UPSCALER_NODE_DIR_NAME
                node_dir.mkdir(parents=True, exist_ok=True)
                (node_dir / "__init__.py").write_text("", encoding="utf-8")
                run_mock.reset_mock()
                start_colab.ensure_latent_upscaler_node()
                run_mock.assert_not_called()

    def test_start_colab_auth_reverse_proxy_security_chunked_and_stream_error(self):
        # Check non-ASCII auth header never raises TypeError
        self.assertFalse(start_colab._check_authorization("Bearer 密钥", "secret-123"))
        b64_utf8 = base64.b64encode("user:密钥".encode("utf-8")).decode("ascii")
        self.assertFalse(start_colab._check_authorization(f"Basic {b64_utf8}", "secret-123"))

        upstream_seen = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            def log_message(self, *a): return
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                upstream_seen.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                })
                resp = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(resp)

            def do_GET(self):
                if self.path == "/drop-midstream":
                    # Promise 100 bytes but close after 10 bytes to trigger mid-stream error
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", "100")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b"0123456789")
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        up_srv = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        up_port = up_srv.server_address[1]
        threading.Thread(target=up_srv.serve_forever, daemon=True).start()
        self.addCleanup(up_srv.shutdown)
        self.addCleanup(up_srv.server_close)

        proxy_cls = start_colab._make_auth_proxy_handler(
            "secret-xyz",
            lambda: {"status": "idle"},
            lambda: None,
            comfy_host="127.0.0.1",
            comfy_port=up_port,
        )
        px_srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy_cls)
        px_port = px_srv.server_address[1]
        threading.Thread(target=px_srv.serve_forever, daemon=True).start()
        self.addCleanup(px_srv.shutdown)
        self.addCleanup(px_srv.server_close)

        def raw_exchange(raw_req: bytes) -> bytes:
            with socket.create_connection(("127.0.0.1", px_port), timeout=5.0) as s:
                s.sendall(raw_req)
                chunks = []
                while True:
                    d = s.recv(4096)
                    if not d:
                        break
                    chunks.append(d)
                return b"".join(chunks)

        # 1. Missing auth -> 401 and closes connection cleanly
        r_unauth = raw_exchange(b"GET /h3_progress HTTP/1.1\r\nHost: localhost\r\n\r\n")
        self.assertTrue(r_unauth.startswith(b"HTTP/1.1 401"))

        # 2. Invalid/negative Content-Length -> 400 (no crash)
        r_bad_cl = raw_exchange(
            b"POST /prompt HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret-xyz\r\nContent-Length: -5\r\n\r\n"
        )
        self.assertTrue(r_bad_cl.startswith(b"HTTP/1.1 400"))

        # 3. Chunked POST forwards full decoded body and strips Authorization header upstream
        r_chunked = raw_exchange(
            b"POST /prompt HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret-xyz\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        self.assertTrue(r_chunked.startswith(b"HTTP/1.1 200"))
        self.assertEqual(upstream_seen[-1]["body"], b"hello world")
        self.assertIsNone(upstream_seen[-1]["auth"])

        # 4. Mid-stream upstream abort after 200 headers must NOT inject a 502 response into body
        r_drop = raw_exchange(
            b"GET /drop-midstream HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer secret-xyz\r\n\r\n"
        )
        self.assertTrue(r_drop.startswith(b"HTTP/1.1 200"))
        self.assertNotIn(b"502", r_drop)
        self.assertNotIn(b"Upstream error", r_drop)

    def test_start_colab_http_status_blocks_cross_origin_auth_redirect_and_preserves_user_dir(self):
        # Cross-origin redirect with Authorization header must be blocked by _http_status
        seen_auth_on_target = []

        class TargetHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): return
            def do_GET(self):
                seen_auth_on_target.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

        tgt = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        tgt_port = tgt.server_address[1]
        threading.Thread(target=tgt.serve_forever, daemon=True).start()
        self.addCleanup(tgt.shutdown)
        self.addCleanup(tgt.server_close)

        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, *a): return
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{tgt_port}/stolen")
                self.end_headers()

        redir = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        redir_port = redir.server_address[1]
        threading.Thread(target=redir.serve_forever, daemon=True).start()
        self.addCleanup(redir.shutdown)
        self.addCleanup(redir.server_close)

        code = start_colab._http_status(
            f"http://127.0.0.1:{redir_port}/system_stats",
            headers={"Authorization": "Bearer top-secret"},
            timeout=3.0,
        )
        self.assertIsNone(code)
        self.assertEqual(seen_auth_on_target, [])

        # link_persistent_dirs migrates existing LOCAL/user files instead of deleting them
        with tempfile.TemporaryDirectory() as tmp:
            drive_base = Path(tmp) / "drive"
            local_dir = Path(tmp) / "local"
            (local_dir / "user").mkdir(parents=True)
            (local_dir / "user" / "saved.json").write_text('{"keep":true}', encoding="utf-8")
            with mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "LOCAL", local_dir):
                start_colab.link_persistent_dirs()
            self.assertTrue((local_dir / "user").is_symlink())
            self.assertEqual((drive_base / "user" / "saved.json").read_text(encoding="utf-8"), '{"keep":true}')

    def test_comfy_tunnel_offline_clears_submission_unknown_503_transient_and_key_boundaries(self):
        # 1. submit_with_retry clears submission_unknown=False on ngrok 404 and Cloudflare 530
        for exc in (
            http.HTTPResponseError(404, "ERR_NGROK_3200 offline"),
            RuntimeError("https://dead.trycloudflare.com/prompt: 530: error 1033"),
        ):
            recorded = []
            with mock.patch.object(comfy, "submit", side_effect=exc):
                with self.assertRaises(RuntimeError):
                    comfy.submit_with_retry({"1": {}}, log=lambda m: None, record_submission=recorded.append)
            self.assertEqual(recorded, [True, False])

        # 2. _is_transient_tunnel_error recognizes plain RuntimeError 502/503/504 from http.request
        self.assertTrue(comfy._is_transient_tunnel_error(RuntimeError("https://n.example/history/1: 503: Service Unavailable")))
        self.assertTrue(comfy._is_transient_tunnel_error(RuntimeError("https://n.example/queue: 502: Bad Gateway")))
        self.assertTrue(comfy._is_transient_tunnel_error(RuntimeError("https://n.example/queue: 504: Gateway Timeout")))

        # 3. _is_auth_rejection recognizes JSON 401/403 bodies
        self.assertTrue(film._is_auth_rejection(comfy.SubmissionRejected('{"error":"bad key","_http_status": 401}')))

        # 4. Non-loopback plaintext HTTP suppresses Bearer key; loopback retains it
        self.assertEqual(comfy.key_for_endpoint("http://192.168.1.10:8188", "secret"), "")
        self.assertEqual(comfy.key_for_endpoint("http://127.0.0.1:8188", "secret"), "secret")
        self.assertEqual(comfy.key_for_endpoint("https://node.ngrok-free.dev", "secret"), "secret")

        # 5. Resume rejects sending rotated/new COMFYUI_API_KEY to a removed endpoint
        env_rotated = {
            "VIDEO_PROVIDER": "comfyui",
            "COMFYUI_URL": "https://node-b.example.com",
            "COMFYUI_API_KEY": "new-secret-key",
        }
        with self.assertRaises(RuntimeError):
            comfy.assert_resume_endpoint_key_allowed(
                "https://node-a.example.com",
                expected_key_fp=comfy.key_fingerprint("old-secret-key"),
                environ=env_rotated,
            )
        # Same key fingerprint on removed endpoint is allowed
        comfy.assert_resume_endpoint_key_allowed(
            "https://node-a.example.com",
            expected_key_fp=comfy.key_fingerprint("new-secret-key"),
            environ=env_rotated,
        )

        # 6. Single-node progress_payload omits endpoint/node_label when endpoint=None
        payload = comfy.progress_payload({"stage": "sampling", "endpoint": "https://node-a.example.com", "node_label": "node-a.example.com"}, endpoint=None)
        self.assertNotIn("endpoint", payload)
        self.assertNotIn("node_label", payload)

    def test_jobs_before_video_launch_runs_outside_jobs_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pic = root / "pic.png"
            Image.new("RGB", (16, 16), "red").save(pic)
            env_path = root / ".env"
            env_path.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://pri.ngrok-free.dev\n"
                "COMFYUI_API_KEY=k-123456\nAPIMART_API_KEY=a-123456\nLLM_API_KEY=l-123456\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=root / ".workbench", env_path=env_path)
            jobs = app.extensions["jobs"]
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)
            client = app.test_client()

            lock_free_during_probe = []

            def check_lock_probe(env=None, *, timeout=12.0):
                # Attempt acquiring jobs.lock from another thread — must succeed immediately if unlocked!
                acquired = []
                def try_lock():
                    got = jobs.lock.acquire(blocking=False)
                    if got:
                        jobs.lock.release()
                    acquired.append(got)
                t = threading.Thread(target=try_lock)
                t.start()
                t.join(timeout=2.0)
                lock_free_during_probe.append(acquired[0])
                return {"online": True, "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}

            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(colab_runtime, "probe_comfyui", side_effect=check_lock_probe), \
                 mock.patch.object(jobs, "launch"):
                res = client.post(
                    "/api/jobs",
                    headers={"X-Workbench": "1"},
                    json={
                        "kind": "one",
                        "demo": False,
                        "prompt": "Picture 1 moves.",
                        "seconds": 5,
                        "aspect": "9:16",
                        "images": [str(pic)],
                        "output_dir": str(root / "one-out"),
                    },
                )
            self.assertEqual(res.status_code, 201)
            self.assertEqual(lock_free_during_probe, [True], "before_video_launch probe must not hold jobs.lock")

    def test_post_fix_adversarial_regressions_all_six_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = root / ".workbench"
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\n", encoding="utf-8")

            # 1. submit_drive_authorized gracefully handles closed stdin (ValueError) without 500
            mgr = colab_runtime.ColabRuntimeManager(storage / "r1")
            closed_stdin = io.StringIO()
            closed_stdin.close()
            mgr._slots["primary"].active_state = "awaiting_drive_auth"
            mgr._slots["primary"].drive_proc = types.SimpleNamespace(stdin=closed_stdin)
            ack = mgr.submit_drive_authorized(slot="primary")
            self.assertFalse(ack["accepted"])
            self.assertEqual(ack["slot"], "primary")

            # 2. DispatchAbortedError does not pollute state.json or mask the real root-cause error
            job_dir = root / "film-abort"
            job_dir.mkdir()
            job = film.Job(job_dir)
            job.put("segs", "1", h3="Test H3 prompt", seed=7)
            pool = comfy.ComfyNodePool(["https://n1.example.com"], api_key="k-1")
            pool._order = ["https://n1.example.com"]
            pool._busy.add("https://n1.example.com")  # keep node busy so seg 1 blocks in acquire()
            abort_ev = threading.Event()
            abort_ev.set()
            env_single = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://n1.example.com",
                "COMFYUI_API_KEY": "k-1",
            }
            with mock.patch.dict(os.environ, env_single, clear=False):
                with self.assertRaises(comfy.DispatchAbortedError):
                    film.gen_segment(
                        job,
                        {"shots": [], "aspect": "16:9"},
                        {"idx": 1, "shots": [{"action": "walk", "start": 0, "end": 5}], "seconds": 5},
                        [],
                        node_pool=pool,
                        abort=abort_ev,
                    )
            self.assertNotIn("error", job.state["segs"]["1"], "DispatchAbortedError must not write error to state.json")
            self.assertNotIn("error_code", job.state["segs"]["1"], "DispatchAbortedError must not write SEGMENT_GENERATION_FAILED")

            # In stage_generate, seg 0 (aborted while waiting for node) must not mask seg 1's real error
            job2_dir = root / "film-abort-stage"
            job2_dir.mkdir()
            job2 = film.Job(job2_dir)
            seg0 = {"idx": 0, "shots": [{"action": "a", "start": 0, "end": 5}], "seconds": 5}
            seg1 = {"idx": 1, "shots": [{"action": "b", "start": 5, "end": 10}], "seconds": 5}
            waiting_entered = threading.Event()

            def fake_gen_segment(j, f, seg, p, *, reference_paths=None, node_pool=None, abort=None):
                if seg["idx"] == 0:
                    waiting_entered.set()
                    # Block in real acquire() until seg 1 fails and sets abort
                    node_pool.acquire(abort=abort)
                waiting_entered.wait(timeout=2.0)
                raise RuntimeError("REAL_UPSTREAM_OOM")

            pool_stub = comfy.ComfyNodePool(["https://n1.example.com", "https://n2.example.com"], api_key="k-1")
            pool_stub._order = ["https://n1.example.com", "https://n2.example.com"]
            pool_stub._busy.update(pool_stub._order)  # force seg 0 to wait in acquire()
            env_multi = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://n1.example.com,https://n2.example.com",
                "COMFYUI_API_KEY": "k-1",
                "COMFYUI_CONCURRENCY": "2",
            }
            with mock.patch.dict(os.environ, env_multi, clear=False), \
                 mock.patch.object(comfy, "ComfyNodePool", return_value=pool_stub), \
                 mock.patch.object(pool_stub, "probe_all", return_value=[]), \
                 mock.patch.object(film, "gen_segment", side_effect=fake_gen_segment):
                with self.assertRaisesRegex(RuntimeError, "REAL_UPSTREAM_OOM"):
                    film.stage_generate(job2, {"shots": [], "aspect": "16:9"}, [seg0, seg1], {0: [], 1: []}, "shot")

            # 3. Stopping primary while secondary is in awaiting_drive_auth preserves secondary auth box & keepalive
            mgr3 = colab_runtime.ColabRuntimeManager(storage / "r3")
            mgr3.mark_local_activity(now=900.0)
            mgr3._slots["secondary"].active_state = "awaiting_drive_auth"
            mgr3._slots["secondary"].auth_url = "https://accounts.google.com/o/oauth2/auth?sec-live=1"
            env_pri = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://pri.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }
            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": False, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}), \
                 mock.patch.object(mgr3, "_query_sessions", return_value={"named": [], "orphans": [], "ok": True}), \
                 mock.patch.object(mgr3, "_run_cmd_for_slot", return_value=(0, "stopped")), \
                 mock.patch.object(mgr3, "stop_keepalive_monitor") as stop_mon:
                stop_res = mgr3.stop(environ=env_pri, force=True, slot="primary")
            stop_mon.assert_not_called()
            self.assertIsNotNone(mgr3._last_activity_at)
            by_slot = {m["slot"]: m for m in stop_res["status"]["managed_nodes"]}
            self.assertEqual(by_slot["primary"]["state"], "offline")
            self.assertEqual(by_slot["secondary"]["state"], "awaiting_drive_auth")
            self.assertEqual(by_slot["secondary"]["auth_url"], "https://accounts.google.com/o/oauth2/auth?sec-live=1")

            # 4. maybe_send_kernel_keepalive lost primary must NOT clear _last_activity_at while secondary lives
            now_box = {"t": 3000.0}
            mgr4 = colab_runtime.ColabRuntimeManager(storage / "r4", clock=lambda: now_box["t"])
            mgr4._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": "https://sec-alive.trycloudflare.com", "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-secret-1234")})
            mgr4.mark_local_activity(now=2900.0)
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": True, "configured": True, "nodes": [], "online_count": 1, "total_count": 1}), \
                 mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=1), \
                 mock.patch.object(mgr4, "_run_cmd_for_slot", return_value=(1, "Session comfy-a100 appears to be lost")):
                sent = mgr4.maybe_send_kernel_keepalive(env_pri, slot="primary")
            self.assertFalse(sent)
            self.assertEqual(mgr4._last_activity_at, 2900.0, "Primary loss must not wipe keepalive activity while secondary lives")

            # 5. Jobs._probing_launch blocks concurrent job launches and /api/config mid-probe changes are re-validated
            pic = root / "p2.png"
            Image.new("RGB", (16, 16), "blue").save(pic)
            env_path = root / ".env"
            env_path.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://pri.ngrok-free.dev\n"
                "COMFYUI_API_KEY=k-old-1234\nAPIMART_API_KEY=a-123456\nLLM_API_KEY=l-123456\n",
                encoding="utf-8",
            )
            app = server.create_app(storage=storage / "srv-toctou", env_path=env_path)
            jobs = app.extensions["jobs"]
            runtime = app.extensions["colab_runtime"]
            self.addCleanup(runtime.stop_keepalive_monitor)
            client = app.test_client()
            probed_urls = []
            concurrent_status = []

            def racing_probe(e=None, *, timeout=12.0):
                url = config.video_settings(e)["comfyui_url"]
                probed_urls.append(url)
                if len(probed_urls) == 1:
                    # While request 1 is probing, a concurrent /api/jobs MUST be rejected as busy (InputError -> 400)
                    r_busy = client.post(
                        "/api/jobs",
                        headers={"X-Workbench": "1"},
                        json={
                            "kind": "one", "demo": False, "prompt": "Picture 1 jumps.",
                            "seconds": 5, "aspect": "9:16", "images": [str(pic)],
                            "output_dir": str(root / "one-busy"),
                        },
                    )
                    concurrent_status.append((r_busy.status_code, (r_busy.get_json() or {}).get("error", "")))
                    # And a concurrent /api/config rotates COMFYUI_URL -> triggers fresh re-probe with new URL!
                    r_cfg = client.post(
                        "/api/config",
                        headers={"X-Workbench": "1"},
                        json={"COMFYUI_URL": "https://new-node.ngrok-free.dev", "COMFYUI_API_KEY": "k-new-9999"},
                    )
                    self.assertEqual(r_cfg.status_code, 200)
                return {"online": True, "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}

            launched_envs = []
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(colab_runtime, "probe_comfyui", side_effect=racing_probe), \
                 mock.patch.object(jobs, "launch", side_effect=lambda rec, req, e: launched_envs.append(dict(e))):
                r_ok = client.post(
                    "/api/jobs",
                    headers={"X-Workbench": "1"},
                    json={
                        "kind": "one", "demo": False, "prompt": "Picture 1 walks.",
                        "seconds": 5, "aspect": "9:16", "images": [str(pic)],
                        "output_dir": str(root / "one-reprobed"),
                    },
                )
            self.assertEqual(r_ok.status_code, 201)
            self.assertEqual(len(concurrent_status), 1)
            self.assertEqual(concurrent_status[0][0], 400, "Concurrent launch during probe must fail with InputError('busy')")
            self.assertTrue(concurrent_status[0][1], "Busy rejection must include localized error message")
            self.assertEqual(probed_urls, ["https://pri.ngrok-free.dev", "https://new-node.ngrok-free.dev"])
            self.assertEqual(launched_envs[-1]["COMFYUI_URL"], "https://new-node.ngrok-free.dev")
            self.assertEqual(launched_envs[-1]["COMFYUI_API_KEY"], "k-new-9999")

            # 6. Mid-probe non-video config change (e.g. switching LLM_PROVIDER to gemini without key) triggers preflight failure
            env_path_llm = root / ".env.llm"
            env_path_llm.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_URL=https://pri.ngrok-free.dev\n"
                "COMFYUI_API_KEY=k-old-1234\nAPIMART_API_KEY=a-123456\nLLM_API_KEY=l-123456\n",
                encoding="utf-8",
            )
            app_llm = server.create_app(storage=storage / "srv-llm-toctou", env_path=env_path_llm)
            jobs_llm = app_llm.extensions["jobs"]
            self.addCleanup(app_llm.extensions["colab_runtime"].stop_keepalive_monitor)
            client_llm = app_llm.test_client()

            def switch_llm_during_probe(e=None, *, timeout=12.0):
                r_cfg = client_llm.post(
                    "/api/config",
                    headers={"X-Workbench": "1"},
                    json={"LLM_PROVIDER": "gemini"},
                )
                self.assertEqual(r_cfg.status_code, 200)
                return {"online": True, "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}

            launched_llm = []
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(colab_runtime, "probe_comfyui", side_effect=switch_llm_during_probe), \
                 mock.patch.object(jobs_llm, "launch", side_effect=lambda *a: launched_llm.append(a)):
                r_fail = client_llm.post(
                    "/api/jobs",
                    headers={"X-Workbench": "1"},
                    json={
                        "kind": "one", "demo": False, "prompt": "参考图片1在雨中走路。",
                        "seconds": 5, "aspect": "9:16", "images": [str(pic)],
                        "output_dir": str(root / "one-llm-fail"),
                    },
                )
            self.assertEqual(r_fail.status_code, 400, "Switching LLM_PROVIDER to unconfigured gemini mid-probe must fail preflight")
            self.assertEqual(launched_llm, [])

            # 7. stop("primary") drops cleared dead secondary URL from status_payload["comfyui_urls"] and stops monitor on error-only secondary
            mgr5 = colab_runtime.ColabRuntimeManager(storage / "r5")
            dead_sec = "https://dead-sec.trycloudflare.com"
            mgr5._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": dead_sec, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-secret-1234")})
            env_dead = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": f"https://pri.ngrok-free.dev,{dead_sec}",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }
            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}), \
                 mock.patch.object(mgr5, "_query_sessions", return_value={"named": [], "orphans": [], "ok": True}), \
                 mock.patch.object(mgr5, "_run_cmd_for_slot", return_value=(0, "stopped")):
                res_dead = mgr5.stop(environ=env_dead, force=True, slot="primary")
            self.assertEqual(res_dead["status"]["comfyui_urls"], ["https://pri.ngrok-free.dev"])

            mgr6 = colab_runtime.ColabRuntimeManager(storage / "r6")
            mgr6._slots["secondary"].last_error = "stale boot error"
            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": False, "devices": [], "nodes": [], "online_count": 0, "total_count": 0}), \
                 mock.patch.object(mgr6, "_query_sessions", return_value={"named": [], "orphans": [], "ok": True}), \
                 mock.patch.object(mgr6, "_run_cmd_for_slot", return_value=(0, "stopped")), \
                 mock.patch.object(mgr6, "stop_keepalive_monitor") as stop_mon6:
                res_err_only = mgr6.stop(environ=env_pri, force=True, slot="primary")
            stop_mon6.assert_called_once()
            sec6 = next(m for m in res_err_only["status"]["managed_nodes"] if m["slot"] == "secondary")
            self.assertEqual(sec6["state"], "error")

            # 8. Auto-start with only secondary URL configured must return HTTP 400 (not 500 ValueError)
            env_path_sec = root / ".env.sec_only"
            sec_only_url = "https://only-sec-auto.trycloudflare.com"
            env_path_sec.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={sec_only_url}\nCOMFYUI_API_KEY=k-sec-1234\n"
                f"APIMART_API_KEY=a-123456\nLLM_API_KEY=l-123456\nCOLAB_AUTO_START=1\nCOLAB_BIN={fake_bin}\n",
                encoding="utf-8",
            )
            app_sec = server.create_app(storage=storage / "srv-sec-only", env_path=env_path_sec)
            rt_sec = app_sec.extensions["colab_runtime"]
            self.addCleanup(rt_sec.stop_keepalive_monitor)
            rt_sec._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_only_url, "gpu": "G4", "tunnel": "cloudflared", "key_fp": comfy.key_fingerprint("k-sec-1234")})
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                 mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}):
                r_sec = app_sec.test_client().post(
                    "/api/jobs",
                    headers={"X-Workbench": "1"},
                    json={
                        "kind": "one", "demo": False, "prompt": "Picture 1 waves.",
                        "seconds": 5, "aspect": "9:16", "images": [str(pic)],
                        "output_dir": str(root / "one-sec-only"),
                    },
                )
            self.assertEqual(r_sec.status_code, 400, f"Expected 400 not 500, got {r_sec.status_code}: {r_sec.get_data(as_text=True)}")

    def test_ship_review_regressions_public_401_watchdog_grace_inflight_abort_relocate_stop_keepalive_and_elf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # 1. targeted#1: ensure_supervisor_and_tunnel must reject public tunnel returning 200 without Authorization
            drive_base = root / "drive-auth"
            drive_base.mkdir()
            (drive_base / ".comfyui_api_key").write_text("k-secret-1234\n", encoding="utf-8")
            (drive_base / ".ngrok_token").write_text("ng-tok\n", encoding="utf-8")
            sup_pid = root / "sup.pid"
            sup_log = root / "sup.log"
            ngrok_log = root / "ngrok.log"

            probed_urls: list[str] = []

            def unauthed_public_status(url, *, headers=None, timeout=4.0):
                probed_urls.append(url)
                if "127.0.0.1:8189" in url:
                    return 401
                return 200  # Simulates stale unauthenticated tunnel pointing straight to 8188

            with mock.patch.dict(os.environ, {"H3_TUNNEL_PROVIDER": "ngrok", "H3_NGROK_DOMAIN": "demo.ngrok-free.dev"}, clear=False), \
                 mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "SUPERVISOR_PID_FILE", sup_pid), \
                 mock.patch.object(start_colab, "SUPERVISOR_LOG_FILE", sup_log), \
                 mock.patch.object(start_colab, "NGROK_LOG_FILE", ngrok_log), \
                 mock.patch.object(start_colab.subprocess, "run"), \
                 mock.patch.object(start_colab.subprocess, "Popen", return_value=mock.MagicMock(pid=8888)), \
                 mock.patch.object(start_colab, "_http_status", side_effect=unauthed_public_status), \
                 mock.patch.object(start_colab.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    start_colab.ensure_supervisor_and_tunnel()
            self.assertIn("https://demo.ngrok-free.dev/system_stats", probed_urls)

            # 2. red-team#1 + adversarial-native#1: _queue_busy stops trusting _comfy_process_alive past grace window
            with mock.patch("urllib.request.urlopen", side_effect=OSError("hung port 8188")), \
                 mock.patch.object(start_colab, "_comfy_process_alive", return_value=True):
                self.assertTrue(start_colab._queue_busy({"status": "idle"}, queue_error_elapsed=60.0))
                self.assertFalse(
                    start_colab._queue_busy(
                        {"status": "idle"},
                        queue_error_elapsed=start_colab.QUEUE_UNREACHABLE_GRACE_SEC + 1.0,
                    )
                )

            # 3. adversarial-native#4: ensure_cloudflared rejects corrupt non-ELF Drive cache and downloaded HTML
            cf_local = root / "cf-local"
            drive_cf = drive_base / "bin" / "cloudflared"
            drive_cf.parent.mkdir(parents=True, exist_ok=True)
            drive_cf.write_bytes(b"<html>502 Bad Gateway</html>")

            def fake_bad_download(url, dst, rel, description=""):
                dst.write_bytes(b"<html>still bad</html>")

            with mock.patch("shutil.which", return_value=None), \
                 mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "CLOUDFLARED_BIN_LOCAL", cf_local), \
                 mock.patch.object(start_colab, "_download_file_atomic", side_effect=fake_bad_download):
                with self.assertRaises(RuntimeError):
                    start_colab.ensure_cloudflared()
            self.assertFalse(cf_local.exists())

            # 4. adversarial-native#3: AuthReverseProxy strips Expect header and aborts on truncated Content-Length body
            upstream_seen_headers: list[dict] = []
            upstream_body_wait_aborted = threading.Event()

            class UpstreamHandler(BaseHTTPRequestHandler):
                def log_message(self, fmt, *args):
                    return

                def do_POST(self):
                    upstream_seen_headers.append({k.lower(): v for k, v in self.headers.items()})
                    cl = int(self.headers.get("Content-Length", "0"))
                    data = self.rfile.read(cl)
                    if len(data) < cl:
                        upstream_body_wait_aborted.set()
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

            up_srv = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
            up_port = up_srv.server_address[1]
            threading.Thread(target=up_srv.serve_forever, daemon=True).start()
            self.addCleanup(up_srv.shutdown)
            self.addCleanup(up_srv.server_close)

            ProxyCls = start_colab._make_auth_proxy_handler(
                "k-secret-1234",
                lambda: {},
                lambda: None,
                comfy_host="127.0.0.1",
                comfy_port=up_port,
            )
            self.assertEqual(ProxyCls.timeout, 60)
            px_srv = ThreadingHTTPServer(("127.0.0.1", 0), ProxyCls)
            px_port = px_srv.server_address[1]
            threading.Thread(target=px_srv.serve_forever, daemon=True).start()
            self.addCleanup(px_srv.shutdown)
            self.addCleanup(px_srv.server_close)

            # Normal POST with Expect: 100-continue succeeds and strips Expect before forwarding
            req_ok = urllib.request.Request(
                f"http://127.0.0.1:{px_port}/upload/image",
                data=b"hello",
                headers={"Authorization": "Bearer k-secret-1234", "Expect": "100-continue"},
                method="POST",
            )
            with urllib.request.urlopen(req_ok, timeout=5) as resp_ok:
                self.assertEqual(resp_ok.status, 200)
            self.assertNotIn("expect", upstream_seen_headers[0])

            # Truncated Content-Length body closes upstream cleanly without blocking 600s
            sock = socket.create_connection(("127.0.0.1", px_port), timeout=5)
            sock.sendall(
                b"POST /upload/image HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Authorization: Bearer k-secret-1234\r\n"
                b"Content-Length: 100\r\n"
                b"\r\n"
                b"short"
            )
            sock.shutdown(socket.SHUT_WR)
            sock.recv(1024)
            sock.close()
            self.assertTrue(upstream_body_wait_aborted.wait(timeout=3.0))

            # 5. red-team#2: parallel stage_generate aborts & cancels queued other_segs when an inflight resumed segment fails
            job_dir = root / "film-abort"
            job_dir.mkdir()
            job = film.Job(job_dir)
            job.state["segs"] = {
                "0": {"h3": "seg1 prompt", "task": "dead-inflight-task", "provider": "comfyui", "comfyui_url": "https://n1.example.com"},
                "1": {"h3": "seg2 prompt"},
                "2": {"h3": "seg3 prompt"},
            }
            job.save()
            segs = [
                {"idx": 0, "seconds": 5, "start": 0, "end": 5, "shots": []},
                {"idx": 1, "seconds": 5, "start": 5, "end": 10, "shots": []},
                {"idx": 2, "seconds": 5, "start": 10, "end": 15, "shots": []},
            ]
            pics = {0: [], 1: [], 2: []}
            dispatched_other: list[int] = []

            def fake_gen_seg(j, f_obj, seg, p_list, node_pool=None, abort=None):
                if seg["idx"] == 0:
                    raise RuntimeError("inflight seg1 prompt_lost")
                dispatched_other.append(seg["idx"])
                return None

            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://n1.example.com", "COMFYUI_CONCURRENCY": "1"},
                clear=False,
            ), mock.patch.object(film, "gen_segment", side_effect=fake_gen_seg):
                with self.assertRaisesRegex(RuntimeError, "inflight seg1 prompt_lost"):
                    film.stage_generate(job, {"aspect": "9:16", "shots": []}, segs, pics, "shot")
            self.assertEqual(dispatched_other, [])

            # 6. outside-voice#3: rotated *.trycloudflare.com URL relocates via comfy.has_task on resume
            reloc_dir = root / "film-reloc"
            (reloc_dir / "clips").mkdir(parents=True)
            reloc_job = film.Job(reloc_dir)
            old_cf = "https://old-dead-11.trycloudflare.com"
            new_cf = "https://new-live-22.trycloudflare.com"
            key_fp = comfy.key_fingerprint("k-secret-1234")
            reloc_job.state["segs"] = {
                "0": {
                    "h3": "seg1 prompt",
                    "task": "uuid-reloc-99",
                    "provider": "comfyui",
                    "comfyui_url": old_cf,
                    "comfyui_key_fp": key_fp,
                }
            }
            reloc_job.save()
            polled_urls: list[str] = []

            def fake_poll(task_id, *, base_url=None, api_key=None):
                polled_urls.append(base_url)
                return {"state": "done", "url": f"{base_url}/view?filename=out.mp4", "cny": 0.0, "run_seconds": 9.0}

            def fake_download(url, out, **kw):
                out.write_bytes(b"mp4" * 500)
                return out

            with mock.patch.dict(
                os.environ,
                {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": f"https://pri.ngrok-free.dev,{new_cf}", "COMFYUI_API_KEY": "k-secret-1234"},
                clear=False,
            ), mock.patch.object(comfy, "has_task", side_effect=lambda tid, *, base_url, api_key=None: base_url == new_cf and tid == "uuid-reloc-99"), \
                 mock.patch.object(comfy, "poll", side_effect=fake_poll), \
                 mock.patch.object(comfy, "download", side_effect=fake_download), \
                 mock.patch.object(film.media, "last_frame", return_value=None):
                film.gen_segment(reloc_job, {"aspect": "9:16"}, {"idx": 0, "seconds": 5}, [])
            self.assertEqual(polled_urls, [new_cf])
            self.assertEqual(reloc_job.state["segs"]["0"]["comfyui_url"], new_cf)

            # 7. outside-voice#2 + targeted#2 + Pass 2 regressions: real _probe_slot & _query_sessions, 504/530 fail-closed,
            #    404-stopped proceeds, force=True bypasses probes, and cancel_event clears on lock-recheck abort
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            mgr_stop = colab_runtime.ColabRuntimeManager(root / "st-stop")
            env_stop = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://pri.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_SESSION": "comfy-a100",
            }
            busy_flag = {"active": False}

            def queue_probe_flips_busy(*args, **kwargs):
                busy_flag["active"] = True
                return 0

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", side_effect=queue_probe_flips_busy), \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "online", "logs": []}), \
                 mock.patch.object(mgr_stop, "_run_cmd_for_slot") as stop_cmd:
                res_race = mgr_stop.stop(
                    environ=env_stop,
                    force=False,
                    local_busy=False,
                    local_busy_provider=lambda: busy_flag["active"],
                )
            self.assertFalse(res_race["stopped"])
            self.assertTrue(res_race["confirmation_required"])
            self.assertTrue(res_race["local_busy"])
            stop_cmd.assert_not_called()

            # 7b. Lock-time local_busy recheck must clear st.cancel_event even if stop() had set it
            st_pri = mgr_stop._slot("primary")
            st_pri.cancel_event.set()
            calls_busy = {"n": 0}

            def busy_flips_on_second_call():
                calls_busy["n"] += 1
                return calls_busy["n"] >= 2

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "online", "logs": []}), \
                 mock.patch.object(mgr_stop, "_run_cmd_for_slot") as stop_cmd_lock:
                res_lock_race = mgr_stop.stop(
                    environ=env_stop,
                    force=False,
                    local_busy=False,
                    local_busy_provider=busy_flips_on_second_call,
                )
            self.assertFalse(res_lock_race["stopped"])
            self.assertTrue(res_lock_race["confirmation_required"])
            self.assertFalse(st_pri.cancel_event.is_set())
            stop_cmd_lock.assert_not_called()

            # 7c. Real _probe_slot + real _query_sessions (no _query_sessions mock): 504 & 530 with active session fail closed
            for code in (504, 530):
                with mock.patch.object(colab_runtime, "_probe_json", return_value=(code, None)), \
                     mock.patch.object(mgr_stop, "_run_command", return_value=(0, "[comfy-a100] m-1 | Hardware: G4\n")) as run_cmd, \
                     mock.patch.object(mgr_stop, "status", return_value={"state": "offline", "logs": []}):
                    res_code = mgr_stop.stop(environ=env_stop, force=False, local_busy=False)
                self.assertFalse(res_code["stopped"])
                self.assertTrue(res_code["confirmation_required"])
                self.assertTrue(res_code["queue_unknown"])
                self.assertTrue(all("stop" not in c.args[2] for c in run_cmd.call_args_list))

            # 7d. 404 with no active Colab session proceeds to stop cleanly; force=True bypasses queue/session probes
            with mock.patch.object(colab_runtime, "_probe_json", return_value=(404, None)), \
                 mock.patch.object(mgr_stop, "_run_command", side_effect=[(0, ""), (0, "stopped\n")]), \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "offline", "logs": []}):
                res_404_gone = mgr_stop.stop(environ=env_stop, force=False, local_busy=False)
            self.assertTrue(res_404_gone["stopped"])
            self.assertFalse(res_404_gone.get("queue_unknown", False))

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", side_effect=AssertionError("must skip on force")) , \
                 mock.patch.object(mgr_stop, "_run_cmd_for_slot", return_value=(0, "stopped\n")) as force_cmd, \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "offline", "logs": []}):
                res_force = mgr_stop.stop(environ=env_stop, force=True, local_busy=False)
            self.assertTrue(res_force["stopped"])
            self.assertEqual(force_cmd.call_count, 1)

            # 8. outside-voice#4: keepalive still fires for resumed ComfyUI work when default VIDEO_PROVIDER=runninghub
            now_box = {"t": 1000.0}
            mgr_ka = colab_runtime.ColabRuntimeManager(root / "st-ka", clock=lambda: now_box["t"])
            now_box["t"] += 600.0
            env_rh = {
                "VIDEO_PROVIDER": "runninghub",
                "COMFYUI_URL": "https://pri.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_SESSION": "comfy-a100",
            }
            with mock.patch.object(mgr_ka, "_run_cmd_for_slot", return_value=(0, "keepalive\n")) as ka_cmd:
                self.assertTrue(mgr_ka.maybe_send_kernel_keepalive(env_rh, force_busy=True))
                self.assertEqual(ka_cmd.call_count, 1)
                now_box["t"] += 600.0
                # Monitor loop with local_busy=True but remote queue=0 must NOT keep idle Colab warm under runninghub default
                self.assertFalse(mgr_ka.maybe_send_kernel_keepalive(env_rh, local_busy=True, queue_count=0))
                # Monitor loop with local_busy=True and remote queue=1 MUST keep Colab warm
                self.assertTrue(mgr_ka.maybe_send_kernel_keepalive(env_rh, local_busy=True, queue_count=1))
                self.assertEqual(ka_cmd.call_count, 2)

            # 9. Pass 2 direct tests: comfy.has_task() and strict trycloudflare hostname validation
            with mock.patch.object(comfy.http, "request", return_value={"pid-h": {"status": {}}}):
                self.assertTrue(comfy.has_task("pid-h", base_url=new_cf, api_key="k-secret-1234"))
            with mock.patch.object(comfy.http, "request", side_effect=[{}, {"queue_running": [[0, "pid-r"]], "queue_pending": []}]):
                self.assertTrue(comfy.has_task("pid-r", base_url=new_cf, api_key="k-secret-1234"))
            with mock.patch.object(comfy.http, "request", side_effect=[{}, {"queue_running": [], "queue_pending": [[1, "pid-p"]]}]):
                self.assertTrue(comfy.has_task("pid-p", base_url=new_cf, api_key="k-secret-1234"))
            with mock.patch.object(comfy.http, "request", side_effect=[{}, {"queue_running": [], "queue_pending": []}]):
                self.assertFalse(comfy.has_task("pid-missing", base_url=new_cf, api_key="k-secret-1234"))
            with mock.patch.object(comfy.http, "request", side_effect=RuntimeError("502")):
                self.assertFalse(comfy.has_task("pid-err", base_url=new_cf, api_key="k-secret-1234"))
            self.assertFalse(film._is_trycloudflare_url("https://evil.example/foo.trycloudflare.com"))
            self.assertFalse(film._is_trycloudflare_url("http://x.trycloudflare.com"))
            self.assertTrue(film._is_trycloudflare_url(new_cf))

            # 10. Pass 3 regressions:
            # 10a. p3/targeted#1: empty slot URL fails closed on unforced stop when Colab session is active
            self.assertIsNone(colab_runtime.probe_remote_queue_count({"VIDEO_PROVIDER": "comfyui"}, strict=True))
            self.assertEqual(colab_runtime.probe_remote_queue_count({"VIDEO_PROVIDER": "comfyui"}, strict=False), 0)
            with mock.patch.object(mgr_stop, "_run_command", return_value=(0, "[comfy-a100-2] m-2 | Hardware: G4\n")) as run_sec_active, \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "offline", "logs": []}):
                res_sec_missing = mgr_stop.stop(environ=env_stop, force=False, local_busy=False, slot="secondary")
            self.assertFalse(res_sec_missing["stopped"])
            self.assertTrue(res_sec_missing["confirmation_required"])
            self.assertTrue(res_sec_missing["queue_unknown"])
            self.assertTrue(all("stop" not in c.args[2] for c in run_sec_active.call_args_list))

            with mock.patch.object(mgr_stop, "_run_command", side_effect=[(0, ""), (0, "stopped\n")]), \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "offline", "logs": []}):
                res_sec_gone = mgr_stop.stop(environ=env_stop, force=False, local_busy=False, slot="secondary")
            self.assertTrue(res_sec_gone["stopped"])

            # 10b. p3/red-team#2: cancelled startup does not persist secondary URL and _query_sessions binds cancel_event
            mgr_cancel = colab_runtime.ColabRuntimeManager(root / "st-cancel")
            with mock.patch.object(mgr_cancel, "_run_command", return_value=(0, "")) as qs_cmd:
                mgr_cancel._query_sessions(str(fake_bin), "browser", env_stop, force=True, cancel_slot="secondary")
            self.assertFalse(qs_cmd.call_args.kwargs.get("record_slot"))
            self.assertIs(qs_cmd.call_args.kwargs.get("cancel_event"), mgr_cancel._slot("secondary").cancel_event)

            st_sec = mgr_cancel._slot("secondary")
            st_sec.cancel_event.set()
            mgr_cancel._set_remembered_node(
                "secondary",
                {"session": "comfy-a100-2", "url": new_cf, "key_fp": comfy.key_fingerprint("k-secret-1234")},
                abort_if_cancelled=True,
            )
            self.assertIsNone(mgr_cancel.remembered_node("secondary"))

            # 10c. p3/red-team#3: remembered secondary URL is scoped by COMFYUI_API_KEY fingerprint
            st_sec.cancel_event.clear()
            mgr_cancel._set_remembered_node(
                "secondary",
                {"session": "comfy-a100-2", "url": new_cf, "key_fp": comfy.key_fingerprint("k-old-1234")},
            )
            self.assertEqual(mgr_cancel.remembered_urls(api_key="k-old-1234"), [new_cf])
            self.assertEqual(mgr_cancel.remembered_urls(api_key="k-secret-1234"), [])
            self.assertIsNone(mgr_cancel.remembered_node("secondary", api_key="k-secret-1234"))
            # Legacy colab-nodes.json record lacking key_fp must also fail closed when api_key is checked
            mgr_cancel._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": new_cf})
            self.assertEqual(mgr_cancel.remembered_urls(api_key="k-secret-1234"), [])
            self.assertIsNone(mgr_cancel.remembered_node("secondary", api_key="k-secret-1234"))

    def test_pass4_review_regressions_sync_startup_stopping_queue_resume_and_one_relocate(self):
        import importlib.util
        import types
        spec = importlib.util.spec_from_file_location("h3ref_cli_entry", config.ROOT / "h3ref.py")
        cli_entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_entry)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            pri = "https://pri.ngrok-free.dev"
            sec_a = "https://sec-a.trycloudflare.com"
            sec_b = "https://sec-b.trycloudflare.com"

            # 1 & 2. sync_ephemeral_url key_fp gating, disk cleanup without early return, malformed .env guard, and create_app startup strip
            env_path = root / ".env"
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri}\nCOMFYUI_API_KEY=k-old-1234\n",
                encoding="utf-8",
            )
            storage = root / ".wb-sync"
            app = server.create_app(storage=storage, env_path=env_path)
            rt = app.extensions["colab_runtime"]
            self.addCleanup(rt.stop_keepalive_monitor)
            client = app.test_client()

            # 1a) persist=False rotates memory to k-new-9999; secondary starting under k-new-9999 updates memory ONLY (not .env)
            r_mem = client.post("/api/config", headers={"X-Workbench": "1"}, json={"COMFYUI_API_KEY": "k-new-9999", "persist": False})
            self.assertEqual(r_mem.status_code, 200)
            rt._set_remembered_node(
                "secondary",
                {"session": "comfy-a100-2", "url": sec_b, "key_fp": comfy.key_fingerprint("k-new-9999")},
            )
            self.assertEqual(client.get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"], [pri, sec_b])
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], pri)

            # 1b) In-flight stale startup finishing with k-old-1234 while memory + .env are on k-new-9999 must add to NEITHER
            r_pers = client.post("/api/config", headers={"X-Workbench": "1"}, json={"COMFYUI_API_KEY": "k-new-9999", "persist": True})
            self.assertEqual(r_pers.status_code, 200)
            rt._set_remembered_node(
                "secondary",
                {"session": "comfy-a100-2", "url": sec_a, "key_fp": comfy.key_fingerprint("k-old-1234")},
            )
            self.assertEqual(client.get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"], [pri])
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], pri)

            # 1c) Removal (old_url -> None) must still strip old_url from .env even if memory already omitted it
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri},{sec_a}\nCOMFYUI_API_KEY=k-new-9999\n",
                encoding="utf-8",
            )
            rt._set_remembered_node("secondary", None)
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], pri)

            # 1d) Malformed .env COMFYUI_URL must skip disk write instead of wiping valid entries
            malformed_raw = f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri},not-a-valid-url\nCOMFYUI_API_KEY=k-new-9999\n"
            env_path.write_text(malformed_raw, encoding="utf-8")
            rt._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_b, "key_fp": comfy.key_fingerprint("k-new-9999")})
            self.assertEqual(env_path.read_text(encoding="utf-8"), malformed_raw)
            rt._set_remembered_node("secondary", None)

            # 2) create_app() startup must strip key-mismatched (or legacy no-key_fp) secondary URL already written to .env
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri},{sec_a}\nCOMFYUI_API_KEY=k-new-9999\n",
                encoding="utf-8",
            )
            rt._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_a, "key_fp": comfy.key_fingerprint("k-old-1234")})
            app_restart = server.create_app(storage=storage, env_path=env_path)
            self.addCleanup(app_restart.extensions["colab_runtime"].stop_keepalive_monitor)
            self.assertEqual(app_restart.test_client().get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"], [pri])
            # Also verify forget_remembered_node(expected_url=...) won't erase a newer secondary record
            app_restart.extensions["colab_runtime"].forget_remembered_node("secondary", expected_url=sec_b)
            self.assertEqual(app_restart.extensions["colab_runtime"].remembered_node("secondary")["url"], sec_a)

            # 3. stopping precedence over online probe in start_async() and status()
            mgr_stop = colab_runtime.ColabRuntimeManager(root / "st-stopping")
            env_stop = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": pri,
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }
            online_probe = {
                "online": True, "configured": True, "devices": [{"name": "L4"}],
                "nodes": [{"url": pri, "online": True, "devices": [{"name": "L4"}]}],
                "online_count": 1, "total_count": 1,
            }
            mgr_stop._slot("primary").active_state = "stopping"
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=online_probe), \
                 mock.patch.object(mgr_stop, "_query_sessions", return_value={"named": [{"name": "comfy-a100", "endpoint": "m-1", "hardware": "G4"}], "orphans": [], "ok": True}):
                with self.assertRaises(colab_runtime.RuntimeBusyError):
                    mgr_stop.start_async(env_stop, slot="primary")
                st_stopping = mgr_stop.status(env_stop)
            self.assertEqual(st_stopping["state"], "stopping")
            pri_entry = next(m for m in st_stopping["managed_nodes"] if m["slot"] == "primary")
            self.assertEqual(pri_entry["state"], "stopping")
            self.assertFalse(pri_entry["online"])
            mgr_stop._slot("primary").active_state = None

            # 4a. stop(force=False) checks local_busy_provider AFTER reserving stopping and BEFORE killing active_worker
            mgr_race = colab_runtime.ColabRuntimeManager(root / "st-race")
            st_pri = mgr_race._slot("primary")
            worker_stub = mock.MagicMock()
            worker_stub.is_alive.return_value = True
            st_pri.active_state = "starting_comfy"
            st_pri.worker_thread = worker_stub
            busy_calls = {"n": 0}

            def racing_local_busy():
                busy_calls["n"] += 1
                # First call (pre-lock check) returns False; second call (after stopping barrier) returns True!
                return busy_calls["n"] >= 2

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr_race, "status", return_value={"state": "starting_comfy", "logs": []}):
                res_race = mgr_race.stop(environ=env_stop, force=False, local_busy=False, local_busy_provider=racing_local_busy, slot="primary")
            self.assertFalse(res_race["stopped"])
            self.assertTrue(res_race["confirmation_required"])
            self.assertFalse(st_pri.cancel_event.is_set(), "force=False must not cancel startup before local_busy check")
            self.assertEqual(st_pri.active_state, "starting_comfy", "active_state must remain live on confirmation_required")
            self.assertFalse(st_pri.stopping)

            # 4a-ii. Worker finishing during local_busy_provider() must NOT be wedged back to stale starting_comfy,
            #        worker phase transition during stop() must NOT break stopping_urls(), and exception must clear st.stopping
            exit_calls = {"n": 0}
            def worker_exits_during_busy():
                exit_calls["n"] += 1
                if exit_calls["n"] == 1:
                    return False
                # Second call runs inside the Dekker barrier while stop() has st.stopping=True
                self.assertIn(pri, mgr_race.stopping_urls(env_stop))
                st_pri.active_state = "awaiting_drive_auth"
                self.assertIn(pri, mgr_race.stopping_urls(env_stop))
                st_pri.active_state = None
                return True

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr_race, "status", return_value={"state": "online", "logs": []}):
                res_exit = mgr_race.stop(environ=env_stop, force=False, local_busy=False, local_busy_provider=worker_exits_during_busy, slot="primary")
            self.assertFalse(res_exit["stopped"])
            self.assertIsNone(st_pri.active_state, "finished worker must not be wedged back to stale prev_state")
            self.assertFalse(st_pri.stopping)

            def exploding_busy():
                if busy_calls["n"] == 2:
                    busy_calls["n"] += 1
                    return False
                raise RuntimeError("boom")

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0):
                with self.assertRaisesRegex(RuntimeError, "boom"):
                    mgr_race.stop(environ=env_stop, force=False, local_busy=False, local_busy_provider=exploding_busy, slot="primary")
            self.assertFalse(st_pri.stopping, "exception from local_busy_provider must still clear st.stopping")

            # 4a-iii. start_async() racing with stop() inside runtime_file_lock must raise RuntimeBusyError without clearing active_state
            orig_lock_fn = colab_runtime.runtime_file_lock
            def lock_sets_stopping(storage_dir, *, slot="primary"):
                ctx = orig_lock_fn(storage_dir, slot=slot)
                class _Wrap:
                    def __enter__(self_inner):
                        res = ctx.__enter__()
                        st_pri.stopping = True
                        return res
                    def __exit__(self_inner, *a):
                        return ctx.__exit__(*a)
                return _Wrap()

            with mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}), \
                 mock.patch.object(colab_runtime, "runtime_file_lock", side_effect=lock_sets_stopping):
                with self.assertRaises(colab_runtime.RuntimeBusyError):
                    mgr_race.start_async(env_stop, slot="primary")
            self.assertIsNone(st_pri.active_state)
            st_pri.stopping = False
            st_pri.worker_thread = None

            # 4a-iv. maybe_send_kernel_keepalive inner lock recheck honors st.stopping,
            #        early-release stop() finally does not clobber a concurrent stop()'s st.stopping,
            #        and submit_drive_authorized rejects while st.stopping=True
            mgr_ka = colab_runtime.ColabRuntimeManager(root / "st-ka-stop", clock=lambda: 1000.0)
            st_ka = mgr_ka._slot("primary")
            st_ka.last_keepalive_at = 0.0
            def ka_lock_sets_stopping(storage_dir, *, slot="primary"):
                ctx = orig_lock_fn(storage_dir, slot=slot)
                class _WrapKa:
                    def __enter__(self_inner):
                        res = ctx.__enter__()
                        st_ka.stopping = True
                        return res
                    def __exit__(self_inner, *a):
                        return ctx.__exit__(*a)
                return _WrapKa()
            with mock.patch.object(colab_runtime, "runtime_file_lock", side_effect=ka_lock_sets_stopping), \
                 mock.patch.object(mgr_ka, "_run_cmd_for_slot") as ka_exec:
                self.assertFalse(mgr_ka.maybe_send_kernel_keepalive(env_stop, force_busy=True, slot="primary"))
            ka_exec.assert_not_called()
            st_ka.active_state = "awaiting_drive_auth"
            st_ka.drive_proc = mock.MagicMock(stdin=mock.MagicMock())
            ack_stopping = mgr_ka.submit_drive_authorized(slot="primary")
            self.assertFalse(ack_stopping["accepted"])
            self.assertEqual(ack_stopping["state"], "stopping")
            st_ka.stopping = False
            st_ka.active_state = None
            st_ka.drive_proc = None

            # Concurrent stop(force=True) setting st.stopping=True during first stop()'s self.status() must NOT be cleared by first stop()'s finally
            busy_seq = {"n": 0}
            def flip_busy_2():
                busy_seq["n"] += 1
                return busy_seq["n"] >= 2
            def status_simulates_concurrent_stop(_env):
                st_ka.stopping = True
                return {"state": "stopping", "logs": []}
            with mock.patch.object(colab_runtime, "probe_remote_queue_count", return_value=0), \
                 mock.patch.object(mgr_ka, "status", side_effect=status_simulates_concurrent_stop):
                res_early = mgr_ka.stop(environ=env_stop, force=False, local_busy=False, local_busy_provider=flip_busy_2, slot="primary")
            self.assertFalse(res_early["stopped"])
            self.assertTrue(st_ka.stopping, "early-returned stop() must not clear concurrent stop()'s st.stopping")
            st_ka.stopping = False

            # 4b. stop(force=True) retries lock acquisition after cancelling slow-unwinding startup worker and runs colab stop
            mgr_force = colab_runtime.ColabRuntimeManager(root / "st-force")
            lock_held = threading.Event()
            release_worker = threading.Event()

            def slow_worker():
                with colab_runtime.runtime_file_lock(mgr_force.storage, slot="primary"):
                    lock_held.set()
                    mgr_force._slot("primary").cancel_event.wait(timeout=2.0)
                    time.sleep(0.15)  # simulate probe unwinding slightly after cancel
                    release_worker.set()

            wt = threading.Thread(target=slow_worker, daemon=True)
            wt.start()
            self.assertTrue(lock_held.wait(timeout=2.0))
            mgr_force._slot("primary").active_state = "starting_comfy"
            mgr_force._slot("primary").worker_thread = wt
            with mock.patch.object(mgr_force, "_run_cmd_for_slot", return_value=(0, "stopped")) as stop_cmd, \
                 mock.patch.object(mgr_force, "status", return_value={"state": "offline", "logs": []}):
                res_force = mgr_force.stop(environ=env_stop, force=True, slot="primary")
            self.assertTrue(res_force["stopped"])
            self.assertTrue(any(c.args[2] == ["stop", "-s", "comfy-a100"] for c in stop_cmd.call_args_list))

            # 5. probe_remote_queue_count(strict=True) fails closed on HTTP 200 {}, and primary stop checks ALL non-secondary URLs
            with mock.patch.object(colab_runtime, "_probe_json", return_value=(200, {})):
                self.assertIsNone(colab_runtime.probe_remote_queue_count(env_stop, strict=True))
                self.assertEqual(colab_runtime.probe_remote_queue_count(env_stop, strict=False), 0)

            self_hosted = "https://self.example.com"
            env_two_pri = {**env_stop, "COMFYUI_URL": f"{self_hosted},{pri}"}
            def queue_by_url(url, key, timeout=12.0):
                if url.startswith(self_hosted):
                    return 200, {"queue_running": [], "queue_pending": []}
                return 200, {"queue_running": [[0, "busy-job"]], "queue_pending": []}
            with mock.patch.object(colab_runtime, "_probe_json", side_effect=queue_by_url), \
                 mock.patch.object(mgr_force, "status", return_value={"state": "online", "logs": []}):
                res_two = mgr_force.stop(environ=env_two_pri, force=False, local_busy=False, slot="primary")
            self.assertFalse(res_two["stopped"])
            self.assertTrue(res_two["confirmation_required"])
            self.assertEqual(res_two["remote_queue"], 1)

            # 6. Workbench preflight & ensure_video_runtime allow resuming submitted ComfyUI task on saved URL when global pool is empty/offline
            pic = root / "actor.png"
            Image.new("RGB", (16, 16), "green").save(pic)
            resume_out = root / "one-resume-saved"
            resume_out.mkdir()
            (resume_out / "state.json").write_text(
                json.dumps({
                    "h3": "six section h3",
                    "task": "pid-saved-1",
                    "provider": "comfyui",
                    "comfyui_url": sec_a,
                    "comfyui_key_fp": comfy.key_fingerprint("k-secret-1234"),
                }),
                encoding="utf-8",
            )
            env_empty_pool = root / ".env.empty-pool"
            env_empty_pool.write_text(
                "VIDEO_PROVIDER=comfyui\nCOMFYUI_API_KEY=k-secret-1234\nAPIMART_API_KEY=a-123456\nLLM_API_KEY=l-123456\n",
                encoding="utf-8",
            )
            app_res = server.create_app(storage=root / ".wb-resume", env_path=env_empty_pool)
            self.addCleanup(app_res.extensions["colab_runtime"].stop_keepalive_monitor)
            req_resume = {
                "kind": "one", "demo": False, "operation": "pipeline",
                "prompt": "Picture 1 walks.", "seconds": 5, "aspect": "9:16",
                "images": [str(pic)], "output_dir": str(resume_out), "lang": "en",
            }
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                # preflight must NOT fail with missing COMFYUI_URL when resuming a saved ComfyUI task
                from workbench import inputs as wb_inputs
                from workbench.i18n import InputError as WbInputError
                wb_inputs.preflight(req_resume, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_API_KEY": "k-secret-1234"})
                # Malformed COMFYUI_URL in resumable_comfyui_state raises InputError, not raw ValueError
                with self.assertRaises(WbInputError):
                    wb_inputs.resumable_comfyui_state(req_resume, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "ftp://bad"})
                probed_resume = []
                def fake_probe_resume(e=None, *, timeout=12.0):
                    probed_resume.append(config.video_settings(e)["comfyui_urls"])
                    return {"online": sec_a in config.video_settings(e)["comfyui_urls"], "configured": True, "devices": [], "nodes": [], "online_count": 1, "total_count": 1}
                with mock.patch.object(colab_runtime, "probe_comfyui", side_effect=fake_probe_resume):
                    app_res.extensions["jobs"].before_video_launch(
                        req_resume,
                        {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "https://offline-pool.example.com", "COMFYUI_API_KEY": "k-secret-1234"},
                    )
                self.assertIn(sec_a, probed_resume[-1])

            # 7. cmd_one() relocates trycloudflare.com task to alternate configured trycloudflare URL via has_task()
            one_dir = root / "one-relocate"
            one_dir.mkdir()
            prompt_file = root / "p.txt"
            prompt_file.write_text("Picture 1 walks.", encoding="utf-8")
            (one_dir / "state.json").write_text(
                json.dumps({
                    "h3": "six section h3",
                    "task": "pid-one-cf",
                    "provider": "comfyui",
                    "comfyui_url": sec_a,
                    "comfyui_key_fp": comfy.key_fingerprint("k-secret-1234"),
                }),
                encoding="utf-8",
            )
            polled_urls = []
            def fake_poll_one(pid, **kw):
                polled_urls.append(kw.get("base_url"))
                return {"state": "done", "url": f"{kw.get('base_url')}/view?filename=out.mp4", "cny": 0.0, "run_seconds": 5.0}
            args_one = types.SimpleNamespace(
                out=str(one_dir), prompt=str(prompt_file), image=[str(pic)],
                seconds=5, aspect="9:16", seed=None, check=False, no_rewrite=False,
            )
            with mock.patch.dict(os.environ, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": f"{pri},{sec_b}", "COMFYUI_API_KEY": "k-secret-1234"}, clear=False), \
                 mock.patch.object(comfy, "has_task", side_effect=lambda tid, base_url=None: base_url == sec_b), \
                 mock.patch.object(comfy, "poll", side_effect=fake_poll_one), \
                 mock.patch.object(comfy, "download", side_effect=lambda u, dst: Path(dst).write_bytes(b"mp4")), \
                 mock.patch.object(cli_entry.media, "duration", return_value=5.0):
                cli_entry.cmd_one(args_one)
            self.assertEqual(polled_urls, [sec_b])
            self.assertEqual(json.loads((one_dir / "state.json").read_text(encoding="utf-8"))["comfyui_url"], sec_b)

    def test_ship_p1_review_regressions(self):
        """Regression tests for all 15 fixes in /ship Pass 1 review (commit 633e9a6)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("h3ref_cli_p1", config.ROOT / "h3ref.py")
        cli_entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli_entry)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            pri = "https://pri.ngrok-free.dev"
            sec_a = "https://sec-a.trycloudflare.com"
            sec_b = "https://sec-b.trycloudflare.com"
            cf_pri_old = "https://old-pri.trycloudflare.com"
            cf_pri_new = "https://new-pri.trycloudflare.com"

            # 1. p1/outside-voice#1: start_colab.link_persistent_dirs merges nested local-only files into existing Drive dirs
            drive_base = root / "drive"
            comfy_dir = root / "comfy"
            (drive_base / "user" / "default").mkdir(parents=True)
            (drive_base / "user" / "default" / "existing.json").write_text('{"from":"drive"}', encoding="utf-8")
            (comfy_dir / "user" / "default" / "workflows").mkdir(parents=True)
            (comfy_dir / "user" / "default" / "existing.json").write_text('{"from":"local"}', encoding="utf-8")
            (comfy_dir / "user" / "default" / "workflows" / "only-local.json").write_text('{"wf":1}', encoding="utf-8")
            with mock.patch.object(start_colab, "BASE", drive_base), \
                 mock.patch.object(start_colab, "LOCAL", comfy_dir):
                start_colab.link_persistent_dirs()
            self.assertTrue((comfy_dir / "user").is_symlink())
            self.assertEqual((drive_base / "user" / "default" / "existing.json").read_text(encoding="utf-8"), '{"from":"drive"}')
            self.assertEqual((drive_base / "user" / "default" / "workflows" / "only-local.json").read_text(encoding="utf-8"), '{"wf":1}')

            # 2. p1/adversarial-native#8: start_comfyui fails immediately when subprocess exits early (no 180s spin)
            comfy_log = root / "comfy.log"
            dead_proc = mock.MagicMock()
            dead_proc.poll.return_value = 137
            def fake_comfy_popen(cmd, **kwargs):
                if kwargs.get("stdout"):
                    kwargs["stdout"].write("RuntimeError: CUDA OOM\n")
                    kwargs["stdout"].flush()
                return dead_proc
            with mock.patch.object(start_colab, "LOCAL", comfy_dir), \
                 mock.patch.object(start_colab, "COMFY_LOG_FILE", comfy_log), \
                 mock.patch.object(start_colab, "COMFY_PID_FILE", root / "comfy.pid"), \
                 mock.patch.object(start_colab, "_http_status", return_value=0), \
                 mock.patch.object(start_colab.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)), \
                 mock.patch.object(start_colab.subprocess, "Popen", side_effect=fake_comfy_popen), \
                 mock.patch.object(start_colab.time, "sleep") as sleep_mock:
                with self.assertRaisesRegex(RuntimeError, r"exited early \(code 137\):[\s\S]*CUDA OOM"):
                    start_colab.start_comfyui(use_sage_attention=False, sage_detail="disabled")
            self.assertEqual(sleep_mock.call_args_list, [mock.call(1)])

            # 3. p1/outside-voice#2: stop(force=False) re-checks remote queue under runtime_file_lock and aborts if 0 -> 1
            mgr_stop = colab_runtime.ColabRuntimeManager(root / "st-p1-stop")
            env_stop = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": pri,
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_SESSION": "comfy-a100",
            }
            q_calls = {"n": 0}
            def queue_flips_under_lock(*args, **kwargs):
                q_calls["n"] += 1
                return 0 if q_calls["n"] == 1 else 1

            with mock.patch.object(colab_runtime, "probe_remote_queue_count", side_effect=queue_flips_under_lock), \
                 mock.patch.object(mgr_stop, "status", return_value={"state": "online", "logs": []}), \
                 mock.patch.object(mgr_stop, "_run_cmd_for_slot") as stop_cmd:
                res_q_race = mgr_stop.stop(environ=env_stop, force=False, local_busy=False)
            self.assertFalse(res_q_race["stopped"])
            self.assertTrue(res_q_race["confirmation_required"])
            self.assertEqual(res_q_race["remote_queue"], 1)
            self.assertEqual(q_calls["n"], 2)
            stop_cmd.assert_not_called()

            # 4. p1/outside-voice#3: non-530 error containing "error 1033" / "argo tunnel error" does not trigger blind retry
            self.assertFalse(comfy._is_tunnel_offline_error(RuntimeError("upstream error 1033")))
            self.assertFalse(comfy._is_tunnel_offline_error(RuntimeError("argo tunnel error")))
            self.assertTrue(comfy._is_tunnel_offline_error(http.HTTPResponseError(530, "error 1033")))
            self.assertTrue(comfy._is_tunnel_offline_error(http.HTTPResponseError(404, "ERR_NGROK_3200")))
            self.assertFalse(comfy._is_tunnel_offline_error(http.HTTPResponseError(500, "ERR_NGROK_3200")))
            recorded_sub_flags = []
            with mock.patch.object(comfy, "submit", side_effect=RuntimeError("gateway error 1033")) as sub_mock:
                with self.assertRaisesRegex(RuntimeError, "gateway error 1033"):
                    comfy.submit_with_retry(
                        {"1": {}},
                        lambda _msg: None,
                        record_submission=recorded_sub_flags.append,
                        base_url=pri,
                        api_key="k-secret-1234",
                    )
            self.assertEqual(recorded_sub_flags, [True], "Non-530 body text must keep submission_unknown=True")
            self.assertEqual(sub_mock.call_count, 1)

            # 5. p1/outside-voice#4: comfy.poll raises RuntimeError (not prompt_lost) on HTTP 200 {} missing queue lists
            with comfy._MISSING_LOCK:
                comfy._MISSING_COUNTS.clear()
                comfy._TRANSIENT_COUNTS.clear()
            with mock.patch.object(comfy.http, "request", side_effect=[{}, {}]):
                with self.assertRaisesRegex(RuntimeError, "malformed queue lists"):
                    comfy.poll("pid-malformed-q", base_url=pri, api_key="k-secret-1234")
            with comfy._MISSING_LOCK:
                self.assertNotIn((pri, "pid-malformed-q"), comfy._MISSING_COUNTS)

            # 6. p1/outside-voice#5: http.download rejects HTML error pages for media & cmd_one rejects duration=0 clips
            dl_out = root / "bad.mp4"
            def fake_html_open(req, timeout=None):
                resp = io.BytesIO(b"<!DOCTYPE html><html><body>502 Bad Gateway" + b"x" * 2048 + b"</body></html>")
                resp.headers = {}
                return resp
            with mock.patch.object(http._OPENER, "open", side_effect=fake_html_open):
                with self.assertRaisesRegex(RuntimeError, "non-media response"):
                    http.download(f"{pri}/view?filename=out.mp4", dl_out)
            self.assertFalse(dl_out.exists())

            one_zero = root / "one-zero-dur"
            one_zero.mkdir()
            prompt_file = root / "p-zero.txt"
            prompt_file.write_text("Picture 1 walks.", encoding="utf-8")
            pic = root / "actor-p1.png"
            Image.new("RGB", (16, 16), "blue").save(pic)
            (one_zero / "state.json").write_text(
                json.dumps({"h3": "six section h3", "task": "pid-zero", "provider": "comfyui", "comfyui_url": pri}),
                encoding="utf-8",
            )
            args_zero = types.SimpleNamespace(
                out=str(one_zero), prompt=str(prompt_file), image=[str(pic)],
                seconds=5, aspect="9:16", seed=None, check=False, no_rewrite=False,
            )
            with mock.patch.dict(os.environ, {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": pri, "COMFYUI_API_KEY": "k-secret-1234"}, clear=False), \
                 mock.patch.object(comfy, "poll", return_value={"state": "done", "url": f"{pri}/view?filename=z.mp4", "cny": 0.0, "run_seconds": 3.0}), \
                 mock.patch.object(comfy, "download", side_effect=lambda u, dst: Path(dst).write_bytes(b"not-a-real-mp4")), \
                 mock.patch.object(cli_entry.media, "duration", return_value=0.0):
                with self.assertRaisesRegex(RuntimeError, "0\\.0s"):
                    cli_entry.cmd_one(args_zero)
            self.assertFalse((one_zero / "clip.mp4").exists())
            self.assertNotIn("cny", json.loads((one_zero / "state.json").read_text(encoding="utf-8")))

            # 7. p1/adversarial-native#1 & p2/red-team#5: _is_terminal_cli_crash_line ignores non-traceback Rich boxes
            #    and caught standard tracebacks, triggering only on Rich traceback panels
            saw_end, saw_tb = False, False
            for benign in (
                "Traceback (most recent call last):",
                "ValueError: caught pip fallback",
                "╭─ Status ─╮",
                "│ Starting │",
                "╰──────────╯",
                "All systems normal",
            ):
                crashed, saw_end, saw_tb = colab_runtime._is_terminal_cli_crash_line(benign, saw_end, saw_tb)
                self.assertFalse(crashed, f"Benign Rich box or caught traceback triggered crash: {benign}")
            saw_end, saw_tb = False, False
            c1, saw_end, saw_tb = colab_runtime._is_terminal_cli_crash_line("╭──── Traceback (most recent call last) ────╮", saw_end, saw_tb)
            c1b, saw_end, saw_tb = colab_runtime._is_terminal_cli_crash_line("│ frame 1                                   │", saw_end, saw_tb)
            c2, saw_end, saw_tb = colab_runtime._is_terminal_cli_crash_line("╰───────────────────────────────────────────╯", saw_end, saw_tb)
            c3, saw_end, saw_tb = colab_runtime._is_terminal_cli_crash_line("RuntimeError: fatal", saw_end, saw_tb)
            self.assertFalse(c1)
            self.assertFalse(c1b)
            self.assertFalse(c2)
            self.assertTrue(c3)

            # 8. p1/adversarial-native#3: status() prioritizes transitional p_active over probe["auth_failed"]
            mgr_st = colab_runtime.ColabRuntimeManager(root / "st-auth-prio")
            mgr_st._slot("primary").active_state = "stopping"
            auth_fail_probe = {
                "online": False, "configured": True, "auth_failed": True, "devices": [],
                "nodes": [{"url": pri, "online": False, "auth_failed": True}],
                "online_count": 0, "total_count": 1,
            }
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=auth_fail_probe), \
                 mock.patch.object(mgr_st, "_query_sessions", return_value={"named": [], "orphans": [], "ok": True}):
                st_res = mgr_st.status(env_stop)
            self.assertEqual(st_res["state"], "stopping")
            mgr_st._slot("primary").active_state = None

            # 9. p1/adversarial-native#5 & p1/outside-voice#6: secondary uploads to per-VM tmp/h3_start_colab.py;
            #    primary startup probes and persists newly rotated ephemeral discovered_url via replace_url
            synced_changes = []
            mgr_up = colab_runtime.ColabRuntimeManager(
                root / "st-upload",
                on_ephemeral_url_change=lambda slot_n, old_u, new_u, key_fp=None: synced_changes.append((slot_n, old_u, new_u, key_fp)),
            )
            sec_cmds = []
            def fake_sec_cmd(colab_bin, auth_mode, args, *, environ=None, stdin_text=None, timeout=60.0, slot="primary"):
                sec_cmds.append((args, stdin_text))
                if args[:1] == ["ls"]:
                    return 0, "MyDrive/\n"
                if args[:1] == ["exec"]:
                    return 0, f"[start_colab] TUNNEL READY (AUTH PROTECTED): {sec_a}\n"
                return 0, "ok\n"
            env_sec_start = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": pri,
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
            }
            with mock.patch.object(mgr_up, "_query_sessions", return_value={"named": [{"name": "comfy-a100-2", "endpoint": "m-2", "hardware": "G4"}], "orphans": [], "ok": True}), \
                 mock.patch.object(mgr_up, "_run_cmd_for_slot", side_effect=fake_sec_cmd), \
                 mock.patch.object(mgr_up, "_probe_slot", return_value={"online": True, "url": sec_a}):
                lock_sec = colab_runtime.runtime_file_lock(mgr_up.storage, slot="secondary")
                lock_sec.__enter__()
                mgr_up._startup_worker(env_sec_start, str(fake_bin), lock_sec, slot="secondary")
            upload_calls = [a for a, _ in sec_cmds if a[:1] == ["upload"]]
            self.assertTrue(upload_calls)
            self.assertEqual(upload_calls[0][-1], "tmp/h3_start_colab.py")
            exec_stdin = [s for a, s in sec_cmds if a[:1] == ["exec"] and s]
            self.assertTrue(any("H3_START_COLAB_SCRIPT'] = '/tmp/h3_start_colab.py'" in s for s in exec_stdin))
            self.assertEqual(mgr_up.remembered_node("secondary")["url"], sec_a)

            # Primary ephemeral URL rotation (cloudflare tunnel)
            pri_probed = []
            env_pri_cf = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": cf_pri_old,
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
                "COLAB_GPU": "G4",
                "COLAB_TUNNEL_PROVIDER": "cloudflare",
            }
            def fake_pri_cmd(colab_bin, auth_mode, args, *, environ=None, stdin_text=None, timeout=60.0, slot="primary"):
                if args[:1] == ["ls"]:
                    return 0, "MyDrive/\n"
                if args[:1] == ["exec"]:
                    return 0, f"[start_colab] TUNNEL READY (AUTH PROTECTED): {cf_pri_new}\n"
                return 0, "ok\n"
            def fake_pri_probe(env, settings, slot_name, *, target_url=None, timeout=4.0):
                pri_probed.append(target_url)
                return {"online": target_url == cf_pri_new, "url": target_url or cf_pri_old}
            synced_changes.clear()
            with mock.patch.object(mgr_up, "_query_sessions", return_value={"named": [{"name": "comfy-a100", "endpoint": "m-1", "hardware": "G4"}], "orphans": [], "ok": True}), \
                 mock.patch.object(mgr_up, "_run_cmd_for_slot", side_effect=fake_pri_cmd), \
                 mock.patch.object(mgr_up, "_probe_slot", side_effect=fake_pri_probe):
                lock_pri = colab_runtime.runtime_file_lock(mgr_up.storage, slot="primary")
                lock_pri.__enter__()
                mgr_up._startup_worker(env_pri_cf, str(fake_bin), lock_pri, slot="primary")
            self.assertIn(cf_pri_new, pri_probed)
            self.assertEqual(synced_changes[-1][:3], ("primary", cf_pri_old, cf_pri_new))

            # 10. p1/red-team#1: start_async cancels in-flight keepalive before acquiring lock, and workbench surfaces RuntimeBusyError
            mgr_ka_cancel = colab_runtime.ColabRuntimeManager(root / "st-ka-cancel")
            st_kc = mgr_ka_cancel._slot("primary")
            ka_lock_held = threading.Event()
            ka_killed = threading.Event()
            fake_ka_proc = mock.MagicMock()

            def fake_ka_thread():
                with colab_runtime.runtime_file_lock(mgr_ka_cancel.storage, slot="primary"):
                    with mgr_ka_cancel._lock:
                        st_kc.keepalive_in_flight = True
                        st_kc.active_proc = fake_ka_proc
                    ka_lock_held.set()
                    ka_killed.wait(timeout=2.0)
                    with mgr_ka_cancel._lock:
                        st_kc.active_proc = None
                        st_kc.keepalive_in_flight = False

            kt = threading.Thread(target=fake_ka_thread, daemon=True)
            kt.start()
            self.assertTrue(ka_lock_held.wait(timeout=2.0))
            worker_started = threading.Event()
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value={"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}), \
                 mock.patch.object(colab_runtime, "_kill_process_tree", side_effect=lambda p: ka_killed.set()), \
                 mock.patch.object(mgr_ka_cancel, "_startup_worker", side_effect=lambda e, b, lc, slot="primary": (worker_started.set(), lc.__exit__(None, None, None))), \
                 mock.patch.object(mgr_ka_cancel, "status", return_value={"state": "provisioning", "logs": []}):
                st_started = mgr_ka_cancel.start_async(env_stop, slot="primary")
            self.assertEqual(st_started["state"], "provisioning")
            self.assertTrue(ka_killed.is_set())
            self.assertTrue(worker_started.wait(timeout=2.0))
            kt.join(timeout=2.0)

            # 11. p1/red-team#1 & p1/targeted#1 & p1/targeted#2: workbench ensure_video_runtime gates
            env_wb = root / ".env.wb-p1"
            env_wb.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri}\nCOMFYUI_API_KEY=k-secret-1234\nCOLAB_AUTO_START=1\n",
                encoding="utf-8",
            )
            app_wb = server.create_app(storage=root / ".wb-p1", env_path=env_wb)
            rt_wb = app_wb.extensions["colab_runtime"]
            self.addCleanup(rt_wb.stop_keepalive_monitor)
            from workbench.i18n import InputError as WbInputError

            # 11a) RuntimeBusyError raises comfy_runtime_action (NOT comfy_starting)
            req_fresh = {
                "kind": "one", "demo": False, "operation": "pipeline",
                "prompt": "Picture 1 walks.", "seconds": 5, "aspect": "9:16",
                "images": [str(pic)], "output_dir": str(root / "fresh-out"), "lang": "en",
            }
            offline_probe = {"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe), \
                 mock.patch.object(colab_runtime, "find_colab_bin", return_value="/usr/local/bin/colab"), \
                 mock.patch.object(rt_wb, "start_async", side_effect=colab_runtime.RuntimeBusyError("another process holds lock")):
                with self.assertRaises(WbInputError) as ctx_busy:
                    app_wb.extensions["jobs"].before_video_launch(
                        req_fresh,
                        {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": pri, "COMFYUI_API_KEY": "k-secret-1234", "COLAB_AUTO_START": "1"},
                    )
            self.assertEqual(ctx_busy.exception.key, "comfy_runtime_action")

            # 11b) p1/targeted#1: mixed film resume (needs_pool_url=True + saved_resume_urls) still blocks if saved endpoint is stopping
            film_mix = root / "film-mix-stopping"
            film_mix.mkdir()
            (film_mix / "state.json").write_text(
                json.dumps({
                    "segs": {
                        "0": {
                            "task": "pid-inflight",
                            "provider": "comfyui",
                            "comfyui_url": sec_a,
                            "comfyui_key_fp": comfy.key_fingerprint("k-secret-1234"),
                        },
                        "1": {},
                    }
                }),
                encoding="utf-8",
            )
            req_mix = {
                "kind": "film", "demo": False, "operation": "pipeline",
                "film": {
                    "shots": [
                        {"id": "s1", "start": 0, "end": 10, "prompt": "Shot 1"},
                        {"id": "s2", "start": 10, "end": 20, "prompt": "Shot 2"},
                    ]
                },
                "frames": {},
                "output_dir": str(film_mix), "lang": "en",
            }
            with mock.patch.object(rt_wb, "stopping_urls", return_value={sec_a}), \
                 mock.patch.object(colab_runtime, "probe_comfyui", side_effect=AssertionError("must block before probe")):
                with self.assertRaises(WbInputError) as ctx_stop:
                    app_wb.extensions["jobs"].before_video_launch(
                        req_mix,
                        {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": pri, "COMFYUI_API_KEY": "k-secret-1234"},
                    )
            self.assertEqual(ctx_stop.exception.key, "comfy_runtime_action")
            self.assertIn("already in progress", str(ctx_stop.exception.values.get("detail", "")).lower())

            # 11c) p1/targeted#2: non-relocatable saved ngrok URL must NOT be masked by an unrelated online pool node
            one_non_reloc = root / "one-non-reloc"
            one_non_reloc.mkdir()
            (one_non_reloc / "state.json").write_text(
                json.dumps({
                    "h3": "six section h3",
                    "task": "pid-ngrok",
                    "provider": "comfyui",
                    "comfyui_url": pri,
                    "comfyui_key_fp": comfy.key_fingerprint("k-secret-1234"),
                }),
                encoding="utf-8",
            )
            req_non_reloc = {
                "kind": "one", "demo": False, "operation": "pipeline",
                "prompt": "Picture 1 walks.", "seconds": 5, "aspect": "9:16",
                "images": [str(pic)], "output_dir": str(one_non_reloc), "lang": "en",
            }
            seen_probe_urls = []
            def capture_probe_urls(e=None, *, timeout=12.0):
                urls = config.video_settings(e)["comfyui_urls"]
                seen_probe_urls.append(urls)
                return {"online": sec_b in urls, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": len(urls)}
            with mock.patch.object(colab_runtime, "probe_comfyui", side_effect=capture_probe_urls):
                with self.assertRaises(WbInputError) as ctx_nr:
                    app_wb.extensions["jobs"].before_video_launch(
                        req_non_reloc,
                        {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": f"{pri},{sec_b}", "COMFYUI_API_KEY": "k-secret-1234", "COLAB_AUTO_START": "0"},
                    )
            self.assertEqual(ctx_nr.exception.key, "comfy_offline")
            self.assertEqual(seen_probe_urls[-1], [pri])

    def test_ship_p2_review_regressions(self):
        """Regression tests for Pass 2 review findings (primary resurrection, second rotation, resume auto-start, start_async/keepalive race, stop lock release, symlink merge, preflight HTTPS key check)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_bin = root / "colab"
            fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake_bin.chmod(0o755)
            pri_a = "https://pri-a.trycloudflare.com"
            pri_b = "https://pri-b.trycloudflare.com"
            pri_c = "https://pri-c.trycloudflare.com"
            sec_a = "https://sec-a.trycloudflare.com"
            fp = comfy.key_fingerprint("k-secret-1234")
            pic = root / "actor-p2.png"
            Image.new("RGB", (16, 16), "red").save(pic)

            # 1. p2/specialist-batched#1 & p2/targeted#3: removed primary remembered node is forgotten and never resurrected on restart
            env_path = root / ".env.p2"
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri_a}\nCOMFYUI_API_KEY=k-secret-1234\n",
                encoding="utf-8",
            )
            storage = root / ".wb-p2"
            app = server.create_app(storage=storage, env_path=env_path)
            rt = app.extensions["colab_runtime"]
            self.addCleanup(rt.stop_keepalive_monitor)
            client = app.test_client()
            rt._set_remembered_node("primary", {"session": "comfy-a100", "url": pri_a, "key_fp": fp})
            resp_cfg = client.post(
                "/api/config",
                headers={"X-Workbench": "1"},
                json={"COMFYUI_URL": pri_b, "persist": True},
            )
            self.assertEqual(resp_cfg.status_code, 200)
            self.assertIsNone(rt.remembered_node("primary"))
            app_restarted = server.create_app(storage=storage, env_path=env_path)
            self.addCleanup(app_restarted.extensions["colab_runtime"].stop_keepalive_monitor)
            self.assertEqual(
                app_restarted.test_client().get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"],
                [pri_b],
            )
            # Even if primary remained in colab-nodes.json (e.g. hand-edited .env), boot must NOT resurrect it
            app_restarted.extensions["colab_runtime"]._set_remembered_node(
                "primary", {"session": "comfy-a100", "url": pri_a, "key_fp": fp}
            )
            env_path.write_text(
                f"VIDEO_PROVIDER=comfyui\nCOMFYUI_URL={pri_b}\nCOMFYUI_API_KEY=k-secret-1234\n",
                encoding="utf-8",
            )
            app_restarted2 = server.create_app(storage=storage, env_path=env_path)
            self.addCleanup(app_restarted2.extensions["colab_runtime"].stop_keepalive_monitor)
            self.assertEqual(
                app_restarted2.test_client().get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"],
                [pri_b],
            )

            # 2. p2/specialist-batched#2: second primary rotation prefers explicit replace_url over stale prev['url']
            rt2 = app_restarted2.extensions["colab_runtime"]
            old_u, new_u = rt2._set_remembered_node(
                "primary",
                {"session": "comfy-a100", "url": pri_c, "key_fp": fp},
                replace_url=pri_b,
            )
            self.assertEqual((old_u, new_u), (pri_b, pri_c))
            self.assertEqual(
                app_restarted2.test_client().get("/api/bootstrap").get_json()["config"]["COMFYUI_URLS"],
                [pri_c],
            )
            self.assertEqual(config.read_env_file(env_path)["COMFYUI_URL"], pri_c)

            # 3. p2/red-team#1: resume-only job bound to offline non-primary endpoint raises comfy_offline (not comfy_starting loop / unused primary start)
            from workbench.i18n import InputError as WbInputError
            resume_sec_dir = root / "one-resume-offline-sec"
            resume_sec_dir.mkdir()
            (resume_sec_dir / "state.json").write_text(
                json.dumps({
                    "h3": "six section h3",
                    "task": "pid-sec-offline",
                    "provider": "comfyui",
                    "comfyui_url": sec_a,
                    "comfyui_key_fp": fp,
                }),
                encoding="utf-8",
            )
            req_sec_res = {
                "kind": "one", "demo": False, "operation": "pipeline",
                "prompt": "Picture 1 walks.", "seconds": 5, "aspect": "9:16",
                "images": [str(pic)], "output_dir": str(resume_sec_dir), "lang": "en",
            }
            offline_probe = {"online": False, "configured": True, "devices": [], "nodes": [], "online_count": 0, "total_count": 1}
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe), \
                 mock.patch.object(rt2, "start_async", side_effect=AssertionError("must not auto-start primary for non-primary resume")):
                with self.assertRaises(WbInputError) as ctx_sec_off:
                    app_restarted2.extensions["jobs"].before_video_launch(
                        req_sec_res,
                        {
                            "VIDEO_PROVIDER": "comfyui",
                            "COMFYUI_URL": "https://pri.ngrok-free.dev",
                            "COMFYUI_API_KEY": "k-secret-1234",
                            "COLAB_AUTO_START": "1",
                            "COLAB_BIN": str(fake_bin),
                        },
                    )
            self.assertEqual(ctx_sec_off.exception.key, "comfy_offline")

            # 4. p2/red-team#2: start_async() serializes concurrent callers idempotently & preempts keepalive even across lock boundaries, clearing cancel_event on lock failure
            mgr_race = colab_runtime.ColabRuntimeManager(root / "st-p2-race", clock=lambda: 2000.0)
            st_r = mgr_race._slot("primary")
            st_r.last_keepalive_at = 0.0
            env_r = {
                "VIDEO_PROVIDER": "comfyui",
                "COMFYUI_URL": "https://pri.ngrok-free.dev",
                "COMFYUI_API_KEY": "k-secret-1234",
                "COLAB_BIN": str(fake_bin),
            }
            # 4a) Concurrent start_async while first is in starting phase returns idempotent status() instead of RuntimeBusyError
            st_r.starting = True
            with mock.patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe), \
                 mock.patch.object(mgr_race, "status", return_value={"state": "provisioning", "logs": []}):
                res_idem = mgr_race.start_async(env_r, slot="primary")
            self.assertEqual(res_idem["state"], "provisioning")
            st_r.starting = False

            # 4b) Keepalive entering runtime_file_lock already has keepalive_in_flight=True before lock entry and skips if st.starting=True
            st_r.starting = True
            with mock.patch.object(mgr_race, "_run_cmd_for_slot") as ka_skip_cmd:
                self.assertFalse(mgr_race.maybe_send_kernel_keepalive(env_r, force_busy=True, slot="primary"))
            ka_skip_cmd.assert_not_called()
            st_r.starting = False

            # 4c) External process holding lock during start_async clears st.starting and st.cancel_event on RuntimeBusyError
            with colab_runtime.runtime_file_lock(mgr_race.storage, slot="primary"):
                with mock.patch.object(colab_runtime, "probe_comfyui", return_value=offline_probe):
                    with self.assertRaises(colab_runtime.RuntimeBusyError):
                        mgr_race.start_async(env_r, slot="primary")
            self.assertFalse(st_r.starting)
            self.assertFalse(st_r.cancel_event.is_set())

            # 5. p2/red-team#3 & p2/specialist-batched#4:
            #    (a) stop(force=False) releases runtime_file_lock BEFORE calling self.status() on confirmation_required
            #    (b) stop() clears remembered secondary & stops keepalive monitor while st.stopping=True and file lock is held
            mgr_st_lock = colab_runtime.ColabRuntimeManager(root / "st-p2-stop-lock")
            lock_free_during_status = {"ok": False}
            def check_lock_free(_env):
                with colab_runtime.runtime_file_lock(mgr_st_lock.storage, slot="primary"):
                    lock_free_during_status["ok"] = True
                return {"state": "online", "logs": []}
            q_seq = {"n": 0}
            def q_flip(*a, **kw):
                q_seq["n"] += 1
                return 0 if q_seq["n"] == 1 else 1
            with mock.patch.object(colab_runtime, "probe_remote_queue_count", side_effect=q_flip), \
                 mock.patch.object(mgr_st_lock, "status", side_effect=check_lock_free):
                res_conf = mgr_st_lock.stop(environ=env_r, force=False, local_busy=False)
            self.assertTrue(res_conf["confirmation_required"])
            self.assertTrue(lock_free_during_status["ok"], "runtime_file_lock must be released before calling status()")

            # (b) Secondary stop clears remembered node while st.stopping=True and file lock held
            sec_stop_observed = {}
            def on_sec_clear(slot_n, old_u, new_u):
                sec_stop_observed["stopping"] = mgr_st_lock._slot("secondary").stopping
                try:
                    with colab_runtime.runtime_file_lock(mgr_st_lock.storage, slot="secondary"):
                        sec_stop_observed["lock_held"] = False
                except colab_runtime.RuntimeBusyError:
                    sec_stop_observed["lock_held"] = True
            mgr_st_lock.set_url_change_callback(on_sec_clear)
            mgr_st_lock._set_remembered_node("secondary", {"session": "comfy-a100-2", "url": sec_a, "key_fp": fp})
            sec_stop_observed.clear()
            with mock.patch.object(mgr_st_lock, "_run_cmd_for_slot", return_value=(0, "stopped\n")), \
                 mock.patch.object(mgr_st_lock, "status", return_value={"state": "offline", "logs": []}):
                res_sec_stop = mgr_st_lock.stop(environ=env_r, force=True, slot="secondary")
            self.assertTrue(res_sec_stop["stopped"])
            self.assertTrue(sec_stop_observed.get("stopping"))
            self.assertTrue(sec_stop_observed.get("lock_held"))

            # (b-ii) Primary stop calls stop_keepalive_monitor while st.stopping=True and file lock held
            pri_stop_observed = {}
            orig_stop_mon = mgr_st_lock.stop_keepalive_monitor
            def wrapped_stop_mon():
                pri_stop_observed["stopping"] = mgr_st_lock._slot("primary").stopping
                try:
                    with colab_runtime.runtime_file_lock(mgr_st_lock.storage, slot="primary"):
                        pri_stop_observed["lock_held"] = False
                except colab_runtime.RuntimeBusyError:
                    pri_stop_observed["lock_held"] = True
                return orig_stop_mon()
            with mock.patch.object(mgr_st_lock, "stop_keepalive_monitor", side_effect=wrapped_stop_mon), \
                 mock.patch.object(mgr_st_lock, "_run_cmd_for_slot", return_value=(0, "stopped\n")), \
                 mock.patch.object(mgr_st_lock, "status", return_value={"state": "offline", "logs": []}):
                res_pri_stop = mgr_st_lock.stop(environ=env_r, force=True, slot="primary")
            self.assertTrue(res_pri_stop["stopped"])
            self.assertTrue(pri_stop_observed.get("stopping"))
            self.assertTrue(pri_stop_observed.get("lock_held"))

            # 6. p2/specialist-batched#3 & p2/red-team#4: _merge_missing_tree skips dest dir symlinks and merges concurrently created subdirs without nesting
            outside_dir = root / "outside"
            outside_dir.mkdir()
            src_tree = root / "src-user"
            dst_tree = root / "dst-user"
            (src_tree / "sub" / "nested").mkdir(parents=True)
            (src_tree / "sub" / "nested" / "a.json").write_text("1", encoding="utf-8")
            (src_tree / "fresh").mkdir(parents=True)
            (src_tree / "fresh" / "wf.json").write_text("2", encoding="utf-8")
            dst_tree.mkdir()
            (dst_tree / "sub").symlink_to(outside_dir)
            start_colab._merge_missing_tree(src_tree, dst_tree)
            self.assertEqual(list(outside_dir.iterdir()), [], "must never traverse into destination symlink directory")
            self.assertEqual((dst_tree / "fresh" / "wf.json").read_text(encoding="utf-8"), "2")

            # 7. p2/targeted#1 & #2: preflight & segment preflight require COMFYUI_API_KEY for saved HTTPS task even when COMFYUI_URL is local HTTP
            from workbench import inputs as wb_inputs
            local_http_env = {"VIDEO_PROVIDER": "comfyui", "COMFYUI_URL": "http://127.0.0.1:8188", "COMFYUI_API_KEY": ""}
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                with self.assertRaises(WbInputError) as ctx_pf:
                    wb_inputs.preflight(req_sec_res, local_http_env)
            self.assertEqual(ctx_pf.exception.key, "keys")
            self.assertIn("COMFYUI_API_KEY", ctx_pf.exception.values.get("names", ""))

            seg_dir = root / "seg-https-resume"
            (seg_dir / "inputs" / "segment-rev1").mkdir(parents=True)
            (seg_dir / "inputs" / "segment-edit.json").write_text(
                json.dumps({"revision": "rev1", "segment": 1, "applied": False, "generated": False}),
                encoding="utf-8",
            )
            (seg_dir / "inputs" / "segment-rev1" / "state.json").write_text(
                json.dumps({
                    "segs": {
                        "0": {
                            "task": "pid-seg-https",
                            "provider": "comfyui",
                            "comfyui_url": sec_a,
                            "comfyui_key_fp": fp,
                        }
                    }
                }),
                encoding="utf-8",
            )
            seg_req = {
                "kind": "film", "demo": False, "operation": "segment", "segment": 1,
                "film": {"shots": [{"id": "s1", "start": 0, "end": 10, "prompt": "Shot 1"}]},
                "frames": {}, "output_dir": str(seg_dir), "lang": "en",
            }
            with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"):
                with self.assertRaises(WbInputError) as ctx_seg_pf:
                    segments.preflight(seg_req, local_http_env)
            self.assertEqual(ctx_seg_pf.exception.key, "keys")
            self.assertIn("COMFYUI_API_KEY", ctx_seg_pf.exception.values.get("names", ""))


if __name__ == "__main__":
    unittest.main()
