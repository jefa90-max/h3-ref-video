"""Regression tests for pure-Python urllib transport in h3ref.http (upload, download, dynamic proxy)."""
import io
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from h3ref import frames, http, rh


class HttpUrllibTransportRegressionTest(unittest.TestCase):
    def test_upload_multipart_wire_format_and_windows_independent_mime_without_curl(self):
        captured = {}

        class UploadHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                captured["ctype"] = self.headers.get("Content-Type")
                captured["auth"] = self.headers.get("Authorization")
                captured["extra"] = self.headers.get("X-Custom")
                captured["body"] = self.rfile.read(length)
                payload = b'{"url": "https://cdn.example/uploaded.webp"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = HTTPServer(("127.0.0.1", 0), UploadHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "ref0.webp"
            Image.new("RGB", (12, 12), "teal").save(img, format="WEBP")
            raw_img = img.read_bytes()

            # Even when OS mimetypes returns (None, None) (common on Windows for .webp),
            # upload must emit image/webp and never invoke external curl.
            with patch("mimetypes.guess_type", return_value=(None, None)), \
                 patch("subprocess.run", side_effect=AssertionError("curl must not be spawned")):
                out = http.upload(
                    f"http://127.0.0.1:{port}/uploads/images",
                    "sk-secret-key-999",
                    img,
                    fields={"apiKey": "@literal;comma,value=1", "fileType": "input"},
                    extra_headers={"X-Custom": "ok"},
                )

        self.assertEqual(out["url"], "https://cdn.example/uploaded.webp")
        self.assertEqual(captured["auth"], "Bearer sk-secret-key-999")
        self.assertEqual(captured["extra"], "ok")

        raw_msg = f"Content-Type: {captured['ctype']}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii") + captured["body"]
        msg = BytesParser(policy=email_default_policy).parsebytes(raw_msg)
        parts = {part.get_param("name", header="content-disposition"): part for part in msg.iter_parts()}
        self.assertEqual(parts["apiKey"].get_payload(decode=True).decode("utf-8"), "@literal;comma,value=1")
        self.assertEqual(parts["fileType"].get_payload(decode=True).decode("utf-8"), "input")
        self.assertEqual(parts["file"].get_filename(), "ref0.webp")
        self.assertEqual(parts["file"].get_content_type(), "image/webp")
        self.assertEqual(parts["file"].get_payload(decode=True), raw_img)

    def test_download_detects_content_length_truncation_retries_and_recovers(self):
        attempts = {"count": 0}
        full_body = b"M" * 4096

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def serve_two():
            for _ in range(2):
                conn, _ = srv.accept()
                conn.recv(4096)
                attempts["count"] += 1
                if attempts["count"] == 1:
                    # Premature EOF: claims 4096 bytes, sends only 1500 bytes
                    conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(full_body)}\r\n\r\n".encode("ascii") + full_body[:1500])
                else:
                    conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(full_body)}\r\n\r\n".encode("ascii") + full_body)
                conn.close()

        thread = threading.Thread(target=serve_two, daemon=True)
        thread.start()
        self.addCleanup(srv.close)

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.mp4"
            with patch("h3ref.http.time.sleep") as sleep_mock, \
                 patch("subprocess.run", side_effect=AssertionError("curl must not be spawned")):
                http.download(f"http://127.0.0.1:{port}/clip.mp4", dst)
            self.assertEqual(dst.read_bytes(), full_body)
            self.assertFalse(dst.with_suffix(".mp4.part").exists())
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.call_count, 1)
        thread.join(2)

    def test_dynamic_proxy_handles_https_scheme_proxy_url_via_plain_connect_and_runtime_toggle(self):
        received = []
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]

        def fake_proxy():
            conn, _ = srv.accept()
            received.append(conn.recv(2048))
            conn.close()

        thread = threading.Thread(target=fake_proxy, daemon=True)
        thread.start()
        self.addCleanup(srv.close)

        # Even when system/env proxy is configured with https:// scheme (which caused curl Schannel error 35),
        # urllib's CONNECT tunnel sends plain HTTP CONNECT to the local proxy and picks up proxies dynamically.
        with patch("urllib.request.getproxies", return_value={"https": f"https://127.0.0.1:{port}"}), \
             patch("urllib.request.proxy_bypass", return_value=False), \
             patch("h3ref.http.time.sleep"):
            with self.assertRaises(http.TransportError):
                http.request("https://api.apimart.ai/v1/test", "", tries=1, timeout=2)

        thread.join(2)
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].startswith(b"CONNECT api.apimart.ai:443 HTTP/"))

    def test_download_follows_same_origin_redirect_and_strips_auth_cross_origin(self):
        seen_dest = {}

        class DestHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                seen_dest["auth"] = self.headers.get("Authorization")
                seen_dest["cookie"] = self.headers.get("Cookie")
                seen_dest["proxy_auth"] = self.headers.get("Proxy-Authorization")
                payload = b"D" * 2048
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        dest_srv = HTTPServer(("127.0.0.1", 0), DestHandler)
        dest_port = dest_srv.server_address[1]
        threading.Thread(target=dest_srv.serve_forever, daemon=True).start()
        self.addCleanup(dest_srv.shutdown)
        self.addCleanup(dest_srv.server_close)

        class SrcHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/cross":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{dest_port}/final.png")
                    self.end_headers()
                elif self.path == "/unsafe-file":
                    self.send_response(302)
                    self.send_header("Location", "file:///etc/passwd")
                    self.end_headers()

        src_srv = HTTPServer(("127.0.0.1", 0), SrcHandler)
        src_port = src_srv.server_address[1]
        threading.Thread(target=src_srv.serve_forever, daemon=True).start()
        self.addCleanup(src_srv.shutdown)
        self.addCleanup(src_srv.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "final.png"
            dummy_token = "tok-" + "fixture-value"
            http.download(
                f"http://127.0.0.1:{src_port}/cross",
                out,
                key=dummy_token,
                extra_headers={"Cookie": "sid=fixture"},
            )
            self.assertEqual(out.read_bytes(), b"D" * 2048)
            self.assertIsNone(seen_dest["auth"])
            self.assertIsNone(seen_dest["cookie"])

            # 3xx to unsafe schemes (e.g. 302 -> file://) must raise _UnsafeRedirectError and fail fast without retrying
            with patch("h3ref.http.time.sleep") as sleep_mock:
                with self.assertRaises(RuntimeError) as unsafe_err:
                    http.download(f"http://127.0.0.1:{src_port}/unsafe-file", Path(tmp) / "bad.png")
            self.assertIn("Blocked unsafe redirect to file scheme", str(unsafe_err.exception))
            sleep_mock.assert_not_called()

    def test_redirect_strips_proxy_authorization_and_request_short_circuits_unsafe_redirect(self):
        bearer_fixture = "Bearer " + "tok-fixture-value"
        req = urllib.request.Request(
            "https://origin-a.example/file.png",
            headers={"Authorization": bearer_fixture, "Cookie": "session=abc"},
        )
        proxy_userinfo = ":".join(("demo", "fixture"))
        http._DynamicProxyHandler().proxy_open(req, f"http://{proxy_userinfo}@proxy.example:8080", "https")
        self.assertTrue(any(k.lower() == "proxy-authorization" for k in req.headers))

        handler = http._SafeRedirectHandler()
        same_origin = handler.redirect_request(req, None, 302, "Found", {}, "https://origin-a.example/final.png")
        self.assertEqual(same_origin.get_header("Authorization"), bearer_fixture)
        self.assertFalse(any(k.lower() == "proxy-authorization" for k in same_origin.headers))
        self.assertFalse(any(k.lower() == "proxy-authorization" for k in same_origin.unredirected_hdrs))

        cross_origin = handler.redirect_request(req, None, 302, "Found", {}, "https://origin-b.example:8188/final.png")
        self.assertFalse(cross_origin.has_header("Authorization"))
        self.assertFalse(any(k.lower() == "proxy-authorization" for k in cross_origin.headers))
        self.assertFalse(any(k.lower() == "cookie" for k in cross_origin.headers))

        with self.assertRaises(http._UnsafeRedirectError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://origin-a.example/final.png")

        # request() must not retry _UnsafeRedirectError
        with patch.object(http._OPENER, "open", side_effect=http._UnsafeRedirectError("Blocked unsafe redirect")) as m, \
             patch("h3ref.http.time.sleep") as sleep_mock:
            with self.assertRaises(http.TransportError):
                http.request("https://origin-a.example/v1", "", tries=3)
        self.assertEqual(m.call_count, 1)
        sleep_mock.assert_not_called()

    def test_upload_rejects_302_post_to_get_and_overwrites_spoofed_http_status(self):
        seen = {"get_called": False}

        class UploadEdgeHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                seen["get_called"] = True
                body = b'{"url": "https://spoofed.example/get-not-post.png"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/after-redir")
                    self.end_headers()
                else:
                    body = b'{"_http_status": 200, "error": "bad_request"}'
                    self.send_response(400)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

        srv = HTTPServer(("127.0.0.1", 0), UploadEdgeHandler)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)

        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / "ref0.jpg"
            img.write_bytes(b"\xff\xd8\xff\xe0jpeg")

            # 302 on POST upload must NOT follow as GET and succeed with dropped multipart payload
            with patch("h3ref.http.time.sleep") as sleep_mock:
                with self.assertRaises(http.HTTPResponseError) as redir_err:
                    http.upload(f"http://127.0.0.1:{port}/redir", "sk-key-123456", img)
            self.assertEqual(redir_err.exception.status_code, 302)
            self.assertFalse(seen["get_called"])
            sleep_mock.assert_not_called()

            # 400 JSON payload carrying "_http_status": 200 must still report real HTTP status 400
            out = http.upload(f"http://127.0.0.1:{port}/spoof", "sk-key-123456", img)
            self.assertEqual(out["_http_status"], 400)

    def test_download_checks_content_length_even_with_content_encoding_and_uses_unique_part_file(self):
        attempts = {"count": 0}
        full_body = b"G" * 2048

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def serve_encoded():
            for _ in range(2):
                conn, _ = srv.accept()
                conn.recv(4096)
                attempts["count"] += 1
                hdr = f"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: {len(full_body)}\r\n\r\n".encode("ascii")
                if attempts["count"] == 1:
                    conn.sendall(hdr + full_body[:1200])
                else:
                    conn.sendall(hdr + full_body)
                conn.close()

        thread = threading.Thread(target=serve_encoded, daemon=True)
        thread.start()
        self.addCleanup(srv.close)

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.bin"
            # Pre-create legacy shared .part path to verify download uses a unique temp file and does not touch it
            sentinel_part = dst.with_suffix(".bin.part")
            sentinel_part.write_bytes(b"other-concurrent-writer")
            with patch("h3ref.http.time.sleep") as sleep_mock:
                http.download(f"http://127.0.0.1:{port}/clip.bin", dst)
            self.assertEqual(dst.read_bytes(), full_body)
            self.assertEqual(attempts["count"], 2)
            self.assertEqual(sleep_mock.call_count, 1)
            self.assertTrue(sentinel_part.exists(), "download must use per-attempt temp file, not shared out.part")
        thread.join(2)

    def test_download_fails_fast_on_cert_or_disk_errors_and_caps_error_body_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.mp4"
            cert_err = urllib.error.URLError(ssl.SSLCertVerificationError("certificate verify failed"))
            with patch.object(http._OPENER, "open", side_effect=cert_err) as open_mock, \
                 patch("h3ref.http.time.sleep") as sleep_mock:
                with self.assertRaises(RuntimeError):
                    http.download("https://cdn.example/clip.mp4", dst)
            self.assertEqual(open_mock.call_count, 1)
            sleep_mock.assert_not_called()

            missing_dir_dst = Path(tmp) / "does-not-exist" / "clip.mp4"
            ok_resp = io.BytesIO(b"V" * 2048)
            ok_resp.headers = {"Content-Length": "2048"}
            with patch.object(http._OPENER, "open", return_value=ok_resp) as open_mock2, \
                 patch("h3ref.http.time.sleep") as sleep_mock2:
                with self.assertRaises(RuntimeError):
                    http.download("https://cdn.example/clip.mp4", missing_dir_dst)
            self.assertEqual(open_mock2.call_count, 1)
            sleep_mock2.assert_not_called()

            # HTTPError body read must be bounded (not unbounded .read())
            read_args = []
            class TrackingStream(io.BytesIO):
                def read(self, amt=-1):
                    read_args.append(amt)
                    return super().read(amt)

            err_403 = urllib.error.HTTPError("https://cdn.example/clip.mp4", 403, "Forbidden", {}, TrackingStream(b"denied"))
            try:
                with patch.object(http._OPENER, "open", side_effect=err_403):
                    with self.assertRaises(RuntimeError):
                        http.download("https://cdn.example/clip.mp4", dst)
            finally:
                err_403.close()
            self.assertTrue(read_args and all(isinstance(a, int) and 0 < a <= 65536 for a in read_args))

    def test_dynamic_proxy_falls_back_from_unsupported_scheme_to_all_http_and_toggles_at_runtime(self):
        handler = http._DynamicProxyHandler()
        req1 = urllib.request.Request("https://api.apimart.ai/v1/test")
        with patch("urllib.request.getproxies", return_value={"https": "socks4://127.0.0.1:7891", "all": "http://127.0.0.1:7897"}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            handler.https_open(req1)
        self.assertEqual(req1._tunnel_host, "api.apimart.ai")
        self.assertEqual(req1.host, "127.0.0.1:7897")

        # Next request after proxies are cleared must not use the old proxy
        req2 = urllib.request.Request("https://api.apimart.ai/v1/test")
        with patch("urllib.request.getproxies", return_value={}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            handler.https_open(req2)
        self.assertIsNone(req2._tunnel_host)
        self.assertEqual(req2.host, "api.apimart.ai")

    def test_pass2_https_proxy_credentials_blocked_malformed_skipped_deadlines_and_part_cleanup(self):
        # 1) Remote/authenticated https:// proxy must fail fast rather than leaking Proxy-Authorization in plaintext
        handler = http._DynamicProxyHandler()
        proxy_userinfo = ":".join(("demo", "fixture"))
        req_auth_proxy = urllib.request.Request("https://api.apimart.ai/v1/test")
        with patch("urllib.request.getproxies", return_value={"https": f"https://{proxy_userinfo}@proxy.example:443"}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            with self.assertRaises(urllib.error.URLError) as proxy_err:
                handler.https_open(req_auth_proxy)
        self.assertIn("plaintext", str(proxy_err.exception).lower())

        # Local loopback https:// proxy with credentials must also be blocked
        req_local_creds = urllib.request.Request("https://api.apimart.ai/v1/test")
        with patch("urllib.request.getproxies", return_value={"https": f"https://{proxy_userinfo}@127.0.0.1:7897"}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            with self.assertRaises(urllib.error.URLError):
                handler.https_open(req_local_creds)

        # 2) Malformed proxy URL without valid scheme/host must be skipped instead of crashing
        req_bad_proxy = urllib.request.Request("https://api.apimart.ai/v1/test")
        with patch("urllib.request.getproxies", return_value={"https": "http:/bad", "all": "http://127.0.0.1:7897"}), \
             patch("urllib.request.proxy_bypass", return_value=False):
            handler.https_open(req_bad_proxy)
        self.assertEqual(req_bad_proxy._tunnel_host, "api.apimart.ai")
        self.assertEqual(req_bad_proxy.host, "127.0.0.1:7897")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 3) download() must NOT publish file if last read crosses total deadline, and must unlink .part on atomic_replace error
            class SlowFinalResp:
                headers = {"Content-Length": "2048"}
                def __init__(self, clock_state):
                    self.clock_state = clock_state
                    self.done = False
                def read(self, n=-1):
                    if not self.done:
                        self.done = True
                        self.clock_state["t"] += 20.0
                        return b"X" * 2048
                    return b""
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            clock_state = {"t": 100.0}
            dst_late = root / "late.mp4"
            with patch.object(http, "_DOWNLOAD_TOTAL_TIMEOUT", 10.0), \
                 patch.object(http, "_DOWNLOAD_ATTEMPTS", 1), \
                 patch("h3ref.http.time.monotonic", side_effect=lambda: clock_state["t"]), \
                 patch.object(http._OPENER, "open", return_value=SlowFinalResp(clock_state)):
                with self.assertRaises(RuntimeError):
                    http.download("https://cdn.example/late.mp4", dst_late)
            self.assertFalse(dst_late.exists())
            self.assertEqual(list(root.glob("*.part")), [])

            class GoodResp:
                headers = {"Content-Length": "2048"}
                def __init__(self):
                    self.done = False
                def read(self, n=-1):
                    if not self.done:
                        self.done = True
                        return b"\x00\x00\x00\x18ftypmp42" + b"M" * 2036
                    return b""
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            dst_locked = root / "locked.mp4"
            with patch.object(http, "_DOWNLOAD_ATTEMPTS", 1), \
                 patch.object(http._OPENER, "open", return_value=GoodResp()), \
                 patch("h3ref.config.atomic_replace", side_effect=PermissionError("WinError 32 locked")):
                with self.assertRaises(PermissionError):
                    http.download("https://cdn.example/locked.mp4", dst_locked)
            self.assertEqual(list(root.glob("*.part")), [], "Failed atomic_replace must unlink unique .part file")

            # 4) upload() must strip spoofed _http_status on 2xx, and frames._upload / rh.upload must reject HTTP 400
            img = root / "sample.png"
            img.write_bytes(b"pngdata")

            class Spoof200Resp:
                status = 200
                def read(self, n=-1):
                    if getattr(self, "_done", False):
                        return b""
                    self._done = True
                    return b'{"_http_status": 400, "name": "ok.png"}'
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            with patch.object(http._OPENER, "open", return_value=Spoof200Resp()):
                res_200 = http.upload("https://comfy.example/upload/image", "k", img)
            self.assertNotIn("_http_status", res_200)

            with patch("h3ref.config.apimart_key", return_value="sk-apimart"), \
                 patch("h3ref.http.upload", return_value={"_http_status": 400, "url": "https://cdn.example/bad.png"}):
                with self.assertRaises(RuntimeError):
                    frames._upload(img)

            with patch("h3ref.config.rh_key", return_value="sk-rh"), \
                 patch("h3ref.http.upload", return_value={"_http_status": 400, "code": 0, "data": {"fileName": "bad.png"}}):
                with self.assertRaises(RuntimeError):
                    rh.upload(img)

    def test_pass3_no_proxy_bypass_before_https_proxy_check_and_trickle_download_deadline(self):
        # 1) NO_PROXY / proxy_bypass exempt host must bypass BEFORE _UnsafeProxyError fires
        handler = http._DynamicProxyHandler()
        proxy_userinfo = ":".join(("demo", "fixture"))
        req_exempt = urllib.request.Request("https://comfy.example:8188/system_stats")
        with patch("urllib.request.getproxies", return_value={"https": f"https://{proxy_userinfo}@proxy.example:443"}), \
             patch("urllib.request.proxy_bypass", return_value=True):
            res = handler.https_open(req_exempt)
        self.assertIsNone(res)
        self.assertIsNone(req_exempt._tunnel_host)

        # 2) Slow-trickle server sending 1 byte every 40ms must abort near total deadline (~0.25s), NOT wait for 64 KiB buffer
        import time as _time
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        port = srv.getsockname()[1]
        stop_srv = threading.Event()

        def trickle():
            try:
                conn, _ = srv.accept()
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 10000\r\n\r\n")
                for _ in range(30):
                    if stop_srv.is_set():
                        break
                    conn.sendall(b"A")
                    _time.sleep(0.04)
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=trickle, daemon=True)
        t.start()
        self.addCleanup(srv.close)
        self.addCleanup(stop_srv.set)

        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "trickle.mp4"
            t0 = _time.monotonic()
            with patch.object(http, "_DOWNLOAD_TOTAL_TIMEOUT", 0.25), \
                 patch.object(http, "_DOWNLOAD_ATTEMPTS", 1):
                with self.assertRaises(RuntimeError) as dl_err:
                    http.download(f"http://127.0.0.1:{port}/trickle.mp4", dst)
            elapsed = _time.monotonic() - t0
            stop_srv.set()
            t.join(2.0)
            self.assertIn("exceeded", str(dl_err.exception).lower())
            # gstack-timing-ok: verifies slow-trickle download aborts near the 0.25s total deadline
            self.assertLess(elapsed, 0.7, f"Trickle download exceeded deadline budget: {elapsed:.2f}s")
            self.assertFalse(dst.exists())

    def test_ship_pass1_socks5_end_to_end_redirect_bounded_read_404_retry_and_part_cleanup(self):
        import struct

        # 1) Live loopback SOCKS5h proxy with RFC 1929 auth routes download() end-to-end with remote DNS
        payload = b"S" * 2048
        socks_log = {}
        socks_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        socks_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        socks_srv.bind(("127.0.0.1", 0))
        socks_srv.listen(2)
        socks_port = socks_srv.getsockname()[1]
        auth_pair = ("proxyuser", "proxypass")

        def run_socks5_server():
            conn, _ = socks_srv.accept()
            try:
                greet = conn.recv(16)
                socks_log["greet"] = greet
                conn.sendall(b"\x05\x02")
                ver = conn.recv(1)
                ulen = conn.recv(1)[0]
                uname = conn.recv(ulen).decode("utf-8")
                plen = conn.recv(1)[0]
                upass = conn.recv(plen).decode("utf-8")
                socks_log["auth"] = (ver, uname, upass)
                conn.sendall(b"\x01\x00")
                hdr = conn.recv(4)
                socks_log["req_hdr"] = hdr
                alen = conn.recv(1)[0]
                socks_log["dest_host"] = conn.recv(alen).decode("ascii")
                socks_log["dest_port"] = struct.unpack("!H", conn.recv(2))[0]
                conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                http_req = conn.recv(4096)
                socks_log["http_req"] = http_req
                resp = f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("ascii") + payload
                conn.sendall(resp)
            finally:
                conn.close()

        st = threading.Thread(target=run_socks5_server, daemon=True)
        st.start()
        self.addCleanup(socks_srv.close)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            socks_dst = root / "via_socks.bin"
            proxy_cred = ":".join(auth_pair)
            proxy_url = f"socks5h://{proxy_cred}@127.0.0.1:{socks_port}"
            with patch("urllib.request.getproxies", return_value={"all": proxy_url}), \
                 patch("urllib.request.proxy_bypass", return_value=False):
                http.download("http://media.example:8088/video.bin", socks_dst)
            st.join(2.0)
            self.assertEqual(socks_dst.read_bytes(), payload)
            self.assertEqual(socks_log["auth"], (b"\x01", auth_pair[0], auth_pair[1]))
            self.assertEqual(socks_log["req_hdr"], b"\x05\x01\x00\x03")
            self.assertEqual(socks_log["dest_host"], "media.example")
            self.assertEqual(socks_log["dest_port"], 8088)
            self.assertIn(b"GET /video.bin HTTP/1.1", socks_log["http_req"])

            # 2) 302 redirect must NOT drain an unbounded response body before closing fp, and malformed Location fails fast
            unbounded_read_called = []

            class ExplodingBodyFP:
                def __init__(self):
                    self.closed = False
                def read(self, amt=None):
                    if amt is None:
                        unbounded_read_called.append(True)
                        raise AssertionError("unbounded fp.read() must not be called on 3xx redirect")
                    return b"err"
                def close(self):
                    self.closed = True

            dest_srv = HTTPServer(("127.0.0.1", 0), type("Dest", (BaseHTTPRequestHandler,), {
                "log_message": lambda self, fmt, *a: None,
                "do_GET": lambda self: (
                    self.send_response(200),
                    self.send_header("Content-Length", str(len(payload))),
                    self.end_headers(),
                    self.wfile.write(payload),
                ),
            }))
            dest_port = dest_srv.server_address[1]
            threading.Thread(target=dest_srv.serve_forever, daemon=True).start()
            self.addCleanup(dest_srv.shutdown)
            self.addCleanup(dest_srv.server_close)

            redir_handler = http._SafeRedirectHandler()
            redir_handler.add_parent(http._OPENER)
            fp = ExplodingBodyFP()
            req_302 = urllib.request.Request("http://127.0.0.1:9/start")
            req_302.timeout = 5
            import time as _time
            req_302._deadline = _time.monotonic() + 5.0
            with redir_handler.http_error_302(
                req_302, fp, 302, "Found", {"location": f"http://127.0.0.1:{dest_port}/ok.bin"}
            ) as redirected_resp:
                self.assertEqual(redirected_resp.read(), payload)
            self.assertEqual(unbounded_read_called, [])
            self.assertTrue(fp.closed)

            # Expired shared deadline on redirect must fail immediately and close fp
            fp_exp = ExplodingBodyFP()
            req_exp = urllib.request.Request("http://127.0.0.1:9/start")
            req_exp._deadline = _time.monotonic() - 1.0
            with self.assertRaises(TimeoutError):
                redir_handler.http_error_302(
                    req_exp, fp_exp, 302, "Found", {"location": f"http://127.0.0.1:{dest_port}/ok.bin"}
                )
            self.assertTrue(fp_exp.closed)

            # Malformed Location must close fp and raise _UnsafeRedirectError without retrying
            fp_bad = ExplodingBodyFP()
            with self.assertRaises(http._UnsafeRedirectError):
                redir_handler.http_error_302(
                    req_302, fp_bad, 302, "Found", {"location": "http://[::1"}
                )
            self.assertTrue(fp_bad.closed)

            # 3) download() retries transient HTTP 404 (CDN propagation parity with curl --retry-all-errors)
            attempts_404 = {"n": 0}

            def open_404_then_ok(req, timeout=None):
                attempts_404["n"] += 1
                if attempts_404["n"] == 1:
                    raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"not ready"))
                resp = io.BytesIO(payload)
                resp.headers = {"Content-Length": str(len(payload))}
                return resp

            dst_404 = root / "cdn_eventual.bin"
            with patch.object(http._OPENER, "open", side_effect=open_404_then_ok), \
                 patch("h3ref.http.time.sleep") as sleep_404:
                http.download("https://cdn.example/cdn_eventual.bin", dst_404)
            self.assertEqual(attempts_404["n"], 2)
            self.assertEqual(sleep_404.call_count, 1)
            self.assertEqual(dst_404.read_bytes(), payload)

            # 4) download() unlinks unique .part file on KeyboardInterrupt during streaming
            class InterruptingResp:
                headers = {"Content-Length": "4096"}
                def read(self, n=-1):
                    raise KeyboardInterrupt("user cancelled")
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False

            dst_int = root / "interrupted.mp4"
            with patch.object(http._OPENER, "open", return_value=InterruptingResp()):
                with self.assertRaises(KeyboardInterrupt):
                    http.download("https://cdn.example/interrupted.mp4", dst_int)
            self.assertEqual(list(root.glob("*.part")), [])

    def test_ship_pass2_macos_scproxy_http_prefixed_and_schemeless_socks_route_as_socks5(self):
        import struct

        # Both macOS _scproxy._get_proxies() ({'socks': 'http://127.0.0.1:PORT'}) and schemeless
        # SOCKS_PROXY ({'socks': '127.0.0.1:PORT'}) must speak SOCKS5 (ATYP=0x03 remote DNS), NOT HTTP CONNECT.
        for label, proxy_val in (("macos_http_prefix", "http://127.0.0.1:{port}"), ("schemeless", "127.0.0.1:{port}")):
            seen = {}
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(2)
            port = srv.getsockname()[1]

            def serve_one():
                conn, _ = srv.accept()
                try:
                    seen["greet"] = conn.recv(16)
                    if seen["greet"].startswith(b"\x05"):
                        conn.sendall(b"\x05\x00")
                        hdr = conn.recv(4)
                        seen["hdr"] = hdr
                        if len(hdr) == 4 and hdr[3] == 3:
                            alen = conn.recv(1)[0]
                            seen["host"] = conn.recv(alen).decode("ascii")
                            seen["port"] = struct.unpack("!H", conn.recv(2))[0]
                        conn.sendall(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                        http_req = conn.recv(4096)
                        seen["http_req"] = http_req
                        body = b'{"ok": true}'
                        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii") + body)
                finally:
                    conn.close()

            t = threading.Thread(target=serve_one, daemon=True)
            t.start()
            try:
                with patch("urllib.request.getproxies", return_value={"socks": proxy_val.format(port=port)}), \
                     patch("urllib.request.proxy_bypass", return_value=False):
                    res = http.request("http://api.example:8080/ping", "", tries=1, timeout=3)
            finally:
                t.join(2.0)
                srv.close()

            self.assertEqual(res, {"ok": True}, f"Failed for {label}")
            self.assertEqual(seen.get("greet"), b"\x05\x01\x00", f"Expected SOCKS5 greeting for {label}, got {seen.get('greet')!r}")
            self.assertEqual(seen.get("hdr"), b"\x05\x01\x00\x03", f"Expected remote-DNS SOCKS5 CONNECT for {label}")
            self.assertEqual(seen.get("host"), "api.example")
            self.assertEqual(seen.get("port"), 8080)


if __name__ == "__main__":
    unittest.main()
