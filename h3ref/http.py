"""Small HTTP helpers on the standard library; every error message has the key replaced by [key]."""
import errno
import functools
import http.client
import io
import ipaddress
import json
import mimetypes
import re
import socket
import ssl
import struct
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from . import config

_NGROK_CODE_RE = re.compile(r"ERR_NGROK_\d+")
_NOSCRIPT_RE = re.compile(r"<noscript[^>]*>(.*?)</noscript>", re.IGNORECASE | re.DOTALL)
_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
}
_MEDIA_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg", ".webp"}
_ERROR_BODY_MAX_BYTES = 65536
_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_SOCKET_TIMEOUT = 60
_DOWNLOAD_TOTAL_TIMEOUT = 900
_DOWNLOAD_ATTEMPTS = 7
_DOWNLOAD_RETRY_DELAY = 5


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "[key]") if key and len(key) >= 6 else text


def _summarize_error_body(raw_body: str, key: str, *, limit: int = 240) -> str:
    safe_text = _scrub(raw_body, key)
    ngrok_match = _NGROK_CODE_RE.search(safe_text)
    if ngrok_match:
        code = ngrok_match.group(0)
        noscript = _NOSCRIPT_RE.search(safe_text)
        if noscript:
            summary = " ".join(noscript.group(1).split())
            if summary:
                clipped = summary[:limit]
                if code not in clipped:
                    return f"{code}: {summary}"[:limit]
                return clipped
        return code
    return safe_text[:limit]


def _scrub_json(value, key: str):
    if not key or len(key) < 6:
        return value
    if isinstance(value, str):
        return value.replace(key, "[key]")
    if isinstance(value, list):
        return [_scrub_json(item, key) for item in value]
    if isinstance(value, dict):
        return {k: _scrub_json(v, key) for k, v in value.items()}
    return value


def _sanitize_disposition_value(value: str) -> str:
    return value.replace("\\", "").replace('"', "").replace("\r", "").replace("\n", "")


def _guess_content_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in _MIME_TYPES:
        return _MIME_TYPES[ext]
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _encode_multipart(
    file_field_name: str,
    upload_name: str,
    payload: bytes,
    fields: dict | None = None,
) -> tuple[str, bytes]:
    boundary = f"----h3refBoundary{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    safe_file_field = _sanitize_disposition_value(file_field_name)
    safe_upload_name = _sanitize_disposition_value(upload_name)
    content_type = _guess_content_type(safe_upload_name)
    for k, v in (fields or {}).items():
        safe_name = _sanitize_disposition_value(str(k))
        chunks.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{safe_name}"\r\n'
            f"\r\n".encode("utf-8")
        )
        chunks.append(str(v).encode("utf-8"))
        chunks.append(b"\r\n")
    chunks.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{safe_file_field}"; filename="{safe_upload_name}"\r\n'
        f"Content-Type: {content_type}\r\n"
        f"\r\n".encode("utf-8")
    )
    chunks.append(payload)
    chunks.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class HTTPResponseError(RuntimeError):
    """An HTTP error response (non-object JSON, unparseable body, or API error payload)."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(detail)


class TransportError(RuntimeError):
    """Connection/TLS/proxy transport failure before an HTTP request was delivered."""


class JSONResponseError(RuntimeError):
    """A successful HTTP response body could not be decoded as JSON."""


class _UnsafeRedirectError(urllib.error.URLError):
    """Redirect blocked because it switches to an unsupported scheme or downgrades HTTPS to HTTP."""


class _UnsafeProxyError(urllib.error.URLError):
    """Proxy rejected because stdlib urllib would send HTTPS proxy CONNECT/credentials in plaintext."""


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise OSError("unexpected EOF from SOCKS5 proxy")
        chunks.extend(chunk)
    return bytes(chunks)


def _open_socks5_socket(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    *,
    timeout: float | None,
    username: str | None = None,
    password: str | None = None,
    remote_dns: bool = True,
) -> socket.socket:
    deadline = (time.monotonic() + float(timeout)) if timeout is not None else None

    def _remaining() -> float | None:
        if deadline is None:
            return None
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError("SOCKS5 proxy handshake timed out")
        return max(0.05, rem)

    sock = socket.create_connection((proxy_host, proxy_port), timeout=_remaining())
    try:
        has_creds = username is not None or password is not None
        if has_creds:
            user_b = (username or "").encode("utf-8")
            pass_b = (password or "").encode("utf-8")
            if len(user_b) > 255 or len(pass_b) > 255:
                raise ValueError("SOCKS5 proxy credentials exceed 255 bytes")
            greeting = b"\x05\x02\x00\x02"
        else:
            user_b = b""
            pass_b = b""
            greeting = b"\x05\x01\x00"

        if deadline is not None:
            sock.settimeout(_remaining())
        sock.sendall(greeting)
        ver, method = _recv_exact(sock, 2)
        if ver != 5:
            raise OSError(f"invalid SOCKS5 version {ver}")
        if method == 0x02:
            if not has_creds:
                raise OSError("SOCKS5 proxy requires authentication")
            if deadline is not None:
                sock.settimeout(_remaining())
            sock.sendall(b"\x01" + bytes([len(user_b)]) + user_b + bytes([len(pass_b)]) + pass_b)
            subver, auth_status = _recv_exact(sock, 2)
            if subver != 1 or auth_status != 0:
                raise OSError("SOCKS5 proxy authentication failed")
        elif method != 0x00:
            raise OSError(f"SOCKS5 proxy rejected authentication method {method:#x}")

        clean_host = target_host.strip("[]")
        try:
            ip = ipaddress.ip_address(clean_host)
            addr_part = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
        except ValueError:
            if remote_dns:
                host_b = clean_host.encode("idna")
                if not host_b or len(host_b) > 255:
                    raise ValueError("invalid target hostname for SOCKS5")
                addr_part = b"\x03" + bytes([len(host_b)]) + host_b
            else:
                infos = socket.getaddrinfo(clean_host, target_port, type=socket.SOCK_STREAM)
                if not infos:
                    raise OSError(f"could not resolve host {clean_host}")
                resolved_ip = ipaddress.ip_address(infos[0][4][0])
                addr_part = (b"\x01" if resolved_ip.version == 4 else b"\x04") + resolved_ip.packed

        if deadline is not None:
            sock.settimeout(_remaining())
        sock.sendall(b"\x05\x01\x00" + addr_part + struct.pack("!H", int(target_port)))
        ver, rep, _rsv, atyp = _recv_exact(sock, 4)
        if ver != 5:
            raise OSError(f"invalid SOCKS5 reply version {ver}")
        if rep != 0:
            raise OSError(f"SOCKS5 proxy CONNECT failed (code {rep:#x})")
        if atyp == 1:
            _recv_exact(sock, 6)
        elif atyp == 4:
            _recv_exact(sock, 18)
        elif atyp == 3:
            alen = _recv_exact(sock, 1)[0]
            _recv_exact(sock, alen + 2)
        else:
            raise OSError(f"unsupported SOCKS5 address type {atyp:#x}")
        sock.settimeout(timeout)
        return sock
    except BaseException:
        sock.close()
        raise


class _Socks5HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *, socks_cfg, socks_deadline=None, **kwargs):
        self._socks_cfg = socks_cfg
        self._socks_deadline = socks_deadline
        super().__init__(host, port=port, timeout=timeout, **kwargs)

    def connect(self):
        proxy_host, proxy_port, username, password, remote_dns = self._socks_cfg
        eff_timeout = None if self.timeout is socket._GLOBAL_DEFAULT_TIMEOUT else float(self.timeout)
        if self._socks_deadline is not None:
            rem = self._socks_deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("request exceeded timeout")
            eff_timeout = rem if eff_timeout is None else min(eff_timeout, rem)
        self.sock = _open_socks5_socket(
            proxy_host,
            proxy_port,
            self.host,
            self.port,
            timeout=eff_timeout,
            username=username,
            password=password,
            remote_dns=remote_dns,
        )


class _Socks5HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, context=None, *, socks_cfg, socks_deadline=None, **kwargs):
        self._socks_cfg = socks_cfg
        self._socks_deadline = socks_deadline
        super().__init__(host, port=port, timeout=timeout, context=context or ssl.create_default_context(), **kwargs)

    def connect(self):
        proxy_host, proxy_port, username, password, remote_dns = self._socks_cfg
        eff_timeout = None if self.timeout is socket._GLOBAL_DEFAULT_TIMEOUT else float(self.timeout)
        if self._socks_deadline is not None:
            rem = self._socks_deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError("request exceeded timeout")
            eff_timeout = rem if eff_timeout is None else min(eff_timeout, rem)
        raw = _open_socks5_socket(
            proxy_host,
            proxy_port,
            self.host,
            self.port,
            timeout=eff_timeout,
            username=username,
            password=password,
            remote_dns=remote_dns,
        )
        try:
            if self._socks_deadline is not None:
                rem_tls = self._socks_deadline - time.monotonic()
                if rem_tls <= 0:
                    raise TimeoutError("request exceeded timeout")
                raw.settimeout(max(0.05, rem_tls if eff_timeout is None else min(eff_timeout, rem_tls)))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


class _DynamicProxyHandler(urllib.request.ProxyHandler):
    """Resolve OS/env proxies per request so runtime proxy toggles apply and loopback bypasses proxies."""

    def __init__(self):
        super().__init__({})

    def _open_with_current_proxy(self, req, scheme: str):
        if req.has_proxy() or getattr(req, "_tunnel_host", None):
            return None
        host = (urlsplit(req.full_url).hostname or "").lower()
        if not host or host in ("localhost", "127.0.0.1", "::1"):
            return None
        if req.host and urllib.request.proxy_bypass(req.host):
            return None
        try:
            proxies = urllib.request.getproxies()
        except OSError:
            return None
        for candidate in (proxies.get(scheme), proxies.get("all"), proxies.get("socks")):
            proxy = (candidate or "").strip()
            if not proxy:
                continue
            has_scheme = "://" in proxy
            if not has_scheme and "/" in proxy:
                continue
            try:
                parsed_proxy = urlsplit(proxy if has_scheme else f"http://{proxy}")
                proxy_port = parsed_proxy.port
            except ValueError:
                continue
            if not parsed_proxy.hostname:
                continue
            if has_scheme:
                proxy_scheme = parsed_proxy.scheme.lower()
                if proxy_scheme in ("socks", "socks5", "socks5h"):
                    socks_cfg = (
                        parsed_proxy.hostname,
                        proxy_port or 1080,
                        unquote(parsed_proxy.username) if parsed_proxy.username is not None else None,
                        unquote(parsed_proxy.password) if parsed_proxy.password is not None else None,
                        proxy_scheme in ("socks", "socks5h"),
                    )
                    socks_deadline = getattr(req, "_deadline", None)
                    if scheme == "https":
                        conn_cls = functools.partial(_Socks5HTTPSConnection, socks_cfg=socks_cfg, socks_deadline=socks_deadline)
                        return urllib.request.HTTPSHandler().do_open(conn_cls, req)
                    conn_cls = functools.partial(_Socks5HTTPConnection, socks_cfg=socks_cfg, socks_deadline=socks_deadline)
                    return urllib.request.HTTPHandler().do_open(conn_cls, req)
                if proxy_scheme not in ("http", "https"):
                    continue
                if proxy_scheme == "https":
                    proxy_host = parsed_proxy.hostname.lower()
                    if parsed_proxy.username or parsed_proxy.password or proxy_host not in ("localhost", "127.0.0.1", "::1"):
                        raise _UnsafeProxyError(
                            "HTTPS proxy with credentials or remote host is unsupported by stdlib urllib "
                            "(would send CONNECT/Proxy-Authorization in plaintext); use an http:// proxy or local tunnel"
                        )
            try:
                return self.proxy_open(req, proxy, scheme)
            except ValueError:
                continue
        return None

    def http_open(self, req):
        return self._open_with_current_proxy(req, "http")

    def https_open(self, req):
        return self._open_with_current_proxy(req, "https")


def _strip_headers_ci(header_map: dict, names: set[str]) -> None:
    for key in [k for k in header_map if k.lower() in names]:
        header_map.pop(key, None)


class _BoundedRedirectFP:
    """Prevent stdlib HTTPRedirectHandler from calling unbounded fp.read() before closing redirect responses."""

    def __init__(self, fp):
        self._fp = fp

    def read(self, amt=None):
        if amt is None:
            return b""
        return self._fp.read(amt)

    def close(self):
        return self._fp.close()

    def __getattr__(self, name):
        return getattr(self._fp, name)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def http_error_302(self, req, fp, code, msg, headers):
        if getattr(req, "_no_redirect", False):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        deadline = getattr(req, "_deadline", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fp.close()
                raise TimeoutError("redirect exceeded timeout")
            cur_timeout = getattr(req, "timeout", None)
            req.timeout = min(float(cur_timeout), remaining) if isinstance(cur_timeout, (int, float)) else remaining
        location = headers.get("location") or headers.get("uri")
        if location:
            try:
                target = urljoin(req.full_url, location.strip())
                old_scheme = urlsplit(req.full_url).scheme.lower()
                new_scheme = urlsplit(target).scheme.lower()
            except ValueError as exc:
                fp.close()
                raise _UnsafeRedirectError("Blocked malformed redirect target") from exc
            if new_scheme not in ("http", "https") or (old_scheme == "https" and new_scheme != "https"):
                fp.close()
                raise _UnsafeRedirectError(f"Blocked unsafe redirect to {new_scheme or 'unknown'} scheme")
        return super().http_error_302(req, _BoundedRedirectFP(fp), code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if getattr(req, "_no_redirect", False):
            raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)
        try:
            old_scheme = urlsplit(req.full_url).scheme.lower()
            new_scheme = urlsplit(newurl).scheme.lower()
        except ValueError as exc:
            raise _UnsafeRedirectError("Blocked malformed redirect target") from exc
        if new_scheme not in ("http", "https") or (old_scheme == "https" and new_scheme != "https"):
            raise _UnsafeRedirectError(f"Blocked unsafe redirect to {new_scheme or 'unknown'} scheme")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            if hasattr(req, "_deadline"):
                redirected._deadline = req._deadline
            _strip_headers_ci(redirected.headers, {"proxy-authorization"})
            _strip_headers_ci(redirected.unredirected_hdrs, {"proxy-authorization"})
            if config.url_origin(newurl) != config.url_origin(req.full_url):
                _strip_headers_ci(redirected.headers, {"authorization", "cookie"})
                _strip_headers_ci(redirected.unredirected_hdrs, {"authorization", "cookie"})
        return redirected


def _read_bounded_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read(_ERROR_BODY_MAX_BYTES).decode("utf-8", errors="replace")
    except Exception:
        return str(exc)
    finally:
        exc.close()


_UPLOAD_RESP_MAX_BYTES = 4 * 1024 * 1024


def _set_response_socket_timeout(resp, seconds: float) -> None:
    fp = getattr(resp, "fp", None)
    raw = getattr(fp, "raw", None)
    sock = getattr(raw, "_sock", None) or getattr(fp, "_sock", None)
    if sock is not None and hasattr(sock, "settimeout"):
        try:
            sock.settimeout(max(0.05, float(seconds)))
        except OSError:
            pass


def _read_response_with_deadline(resp, *, deadline: float, max_bytes: int) -> bytes:
    if isinstance(getattr(resp, "fp", None), io.IOBase):
        read_fn = getattr(resp, "read1", None) or resp.read
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("response exceeded timeout")
            _set_response_socket_timeout(resp, remaining)
            chunk = read_fn(min(_DOWNLOAD_CHUNK_BYTES, max_bytes - total + 1))
            if time.monotonic() > deadline:
                raise TimeoutError("response exceeded timeout")
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise OSError(f"response body exceeded {max_bytes} bytes")
        return b"".join(chunks)
    if time.monotonic() > deadline:
        raise TimeoutError("response exceeded timeout")
    try:
        data = resp.read(max_bytes + 1)
    except TypeError:
        data = resp.read()
    if time.monotonic() > deadline:
        raise TimeoutError("response exceeded timeout")
    if len(data) > max_bytes:
        raise OSError(f"response body exceeded {max_bytes} bytes")
    return data


def _is_non_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, (_UnsafeRedirectError, _UnsafeProxyError, ssl.SSLCertVerificationError, FileNotFoundError, PermissionError, IsADirectoryError)):
        return True
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, ssl.SSLCertVerificationError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == errno.ENOSPC:
        return True
    return False


_ORIG_URLOPEN = urllib.request.urlopen
_OPENER = urllib.request.build_opener(_DynamicProxyHandler(), _SafeRedirectHandler())


def _open_fn():
    return _OPENER.open if urllib.request.urlopen is _ORIG_URLOPEN else urllib.request.urlopen


def _response_status(resp) -> int:
    status = getattr(resp, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(resp, "getcode", None)
    if callable(getcode):
        code = getcode()
        if isinstance(code, int):
            return code
    return 200


def request(url: str, key: str, body: dict | None = None, method: str | None = None, timeout: float = 120, tries: int = 3,
            extra_headers: dict | None = None) -> dict:
    """POST (with body) or GET; network errors and 5xx retry, 4xx answers come back as they are."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": config.UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    if extra_headers:
        headers.update(extra_headers)
    last = None
    error_cls = RuntimeError
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=headers)
        req._deadline = time.monotonic() + float(timeout)
        open_fn = _open_fn()
        try:
            with open_fn(req, timeout=timeout) as r:
                raw_text = r.read().decode(errors="replace")
                try:
                    parsed_ok = json.loads(raw_text)
                except json.JSONDecodeError as exc:
                    error_cls = JSONResponseError
                    last = _scrub(f"{type(exc).__name__}: {exc}", key)
                else:
                    return _scrub_json(parsed_ok, key)
        except urllib.error.HTTPError as exc:
            error_cls = RuntimeError
            raw_text = _read_bounded_error(exc)
            summary = _summarize_error_body(raw_text, key, limit=300)
            if exc.code < 500:
                try:
                    parsed = json.loads(raw_text)
                except ValueError:
                    raise HTTPResponseError(exc.code, f"{url} {exc.code}: {summary}") from None
                if not isinstance(parsed, dict):
                    low = raw_text.lower()
                    auth_code = 401 if exc.code == 400 and ("api_key_invalid" in low or "api key not valid" in low) else exc.code
                    raise HTTPResponseError(auth_code, f"{url} {exc.code}: {summary}") from None
                parsed = _scrub_json(parsed, key)
                parsed["_http_status"] = exc.code
                return parsed
            last = f"{exc.code}: {_summarize_error_body(raw_text, key, limit=200)}"
        except Exception as exc:
            error_cls = TransportError if isinstance(exc, urllib.error.URLError) else RuntimeError
            last = _scrub(f"{type(exc).__name__}: {exc}", key)
            if isinstance(exc, (_UnsafeRedirectError, _UnsafeProxyError)):
                break
        if attempt + 1 < tries:
            time.sleep(4 * (attempt + 1))
    raise error_cls(f"{url}: {_scrub(str(last), key)[:300]}")


_NON_RETRYABLE_UPLOAD_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422}
_NON_RETRYABLE_DOWNLOAD_STATUSES = {400, 401, 402, 403, 405, 410, 422}


def upload(url: str, key: str, path: Path, fields: dict | None = None, timeout: int = 300, *,
           file_field_name: str = "file", filename: str | None = None, extra_headers: dict | None = None) -> dict:
    """Multipart upload via urllib.request so uploads share request()'s OpenSSL and proxy handling without POST redirects."""
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise RuntimeError(f"upload rejected unsafe url: {url[:120]}")
    file_path = Path(path)
    payload = file_path.read_bytes()
    content_type, body = _encode_multipart(file_field_name, filename or file_path.name, payload, fields)
    headers = {"User-Agent": config.UA}
    if key:
        headers["Authorization"] = "Bearer " + key
    if extra_headers:
        headers.update(extra_headers)
    headers["Content-Type"] = content_type
    last = ""
    for attempt in range(4):
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        req._no_redirect = True
        open_fn = _open_fn()
        attempt_deadline = time.monotonic() + timeout
        req._deadline = attempt_deadline
        try:
            with open_fn(req, timeout=timeout) as r:
                status_code = _response_status(r)
                raw_body = _read_response_with_deadline(
                    r, deadline=attempt_deadline, max_bytes=_UPLOAD_RESP_MAX_BYTES
                ).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            raw_body = _read_bounded_error(exc)
        except Exception as exc:
            last = _scrub(f"{type(exc).__name__}: {exc}", key)[:200]
            if isinstance(exc, (_UnsafeRedirectError, _UnsafeProxyError)):
                break
            if attempt + 1 < 4:
                time.sleep(5 * (attempt + 1))
            continue
        safe_summary = _summarize_error_body(raw_body, key, limit=200)
        if status_code >= 500:
            last = f"{status_code}: {safe_summary}"
            if attempt + 1 < 4:
                time.sleep(5 * (attempt + 1))
            continue
        if 300 <= status_code < 400:
            raise HTTPResponseError(status_code, f"{_scrub(url, key)} {status_code}: {safe_summary}")
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                parsed = _scrub_json(parsed, key)
                if status_code >= 400:
                    parsed["_http_status"] = status_code
                else:
                    parsed.pop("_http_status", None)
                return parsed
            last = safe_summary
        except ValueError:
            last = safe_summary
        if status_code in _NON_RETRYABLE_UPLOAD_STATUSES:
            raise HTTPResponseError(status_code, f"{_scrub(url, key)} {status_code}: {last}")
        if attempt + 1 < 4:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"upload {file_path.name} failed: {last}")


def download(url: str, out: Path, *, key: str = "", extra_headers: dict | None = None) -> Path:
    """Streaming download via urllib.request with HTTPS-only redirect enforcement and truncation detection."""
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(f"download rejected unsafe url: {url[:120]}")
    out = Path(out)
    headers = {"User-Agent": config.UA}
    if key:
        headers["Authorization"] = "Bearer " + key
    if extra_headers:
        headers.update(extra_headers)
    deadline = time.monotonic() + _DOWNLOAD_TOTAL_TIMEOUT
    last_detail = ""
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            last_detail = f"timed out after {_DOWNLOAD_TOTAL_TIMEOUT}s"
            break
        req = urllib.request.Request(url, method="GET", headers=headers)
        req._deadline = deadline
        open_fn = _open_fn()
        tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
        try:
            with open_fn(req, timeout=min(_DOWNLOAD_SOCKET_TIMEOUT, remaining)) as resp:
                resp_headers = getattr(resp, "headers", None)
                raw_cl = resp_headers.get("Content-Length") if resp_headers is not None else None
                expected_len: int | None = None
                if raw_cl:
                    try:
                        parsed_cl = int(str(raw_cl).strip())
                        if parsed_cl >= 0:
                            expected_len = parsed_cl
                    except ValueError:
                        expected_len = None
                written = 0
                read_fn = getattr(resp, "read1", None) or resp.read
                with tmp.open("wb") as fh:
                    while True:
                        rem_read = deadline - time.monotonic()
                        if rem_read <= 0:
                            raise TimeoutError(f"download exceeded {_DOWNLOAD_TOTAL_TIMEOUT}s")
                        _set_response_socket_timeout(resp, min(_DOWNLOAD_SOCKET_TIMEOUT, rem_read))
                        chunk = read_fn(_DOWNLOAD_CHUNK_BYTES)
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"download exceeded {_DOWNLOAD_TOTAL_TIMEOUT}s")
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                if expected_len is not None and written != expected_len:
                    raise OSError(f"incomplete download: got {written} of {expected_len} bytes")
        except urllib.error.HTTPError as exc:
            status_code = exc.code
            err_body = _read_bounded_error(exc)
            tmp.unlink(missing_ok=True)
            summary = _summarize_error_body(err_body, key, limit=200) if err_body else _scrub(str(exc), key)[:200]
            last_detail = f"HTTP {status_code}: {summary}" if summary else f"HTTP {status_code}"
            if (300 <= status_code < 400) or status_code in _NON_RETRYABLE_DOWNLOAD_STATUSES:
                break
        except Exception as exc:
            tmp.unlink(missing_ok=True)
            last_detail = _scrub(f"{type(exc).__name__}: {exc}", key)[:200]
            if _is_non_retryable_download_error(exc):
                break
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        else:
            if not tmp.exists() or tmp.stat().st_size < 1000:
                size = tmp.stat().st_size if tmp.exists() else 0
                tmp.unlink(missing_ok=True)
                last_detail = f"response too small ({size} bytes)"
            else:
                try:
                    if out.suffix.lower() in _MEDIA_SUFFIXES:
                        with tmp.open("rb") as fh:
                            head = fh.read(512).lstrip().lower()
                        if head.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"{", b"[")):
                            raise RuntimeError(f"download returned non-media response: {_scrub(url, key)[:120]}")
                    config.atomic_replace(tmp, out)
                    return out
                except BaseException:
                    tmp.unlink(missing_ok=True)
                    raise

        if attempt + 1 < _DOWNLOAD_ATTEMPTS:
            delay = min(_DOWNLOAD_RETRY_DELAY, max(0.0, deadline - time.monotonic()))
            if delay > 0:
                time.sleep(delay)

    suffix = f" ({last_detail[:200]})" if last_detail else ""
    raise RuntimeError(f"download failed: {_scrub(url, key)[:120]}{suffix}")
