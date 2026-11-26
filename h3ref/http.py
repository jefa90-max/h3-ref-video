"""Small JSON-over-HTTP helpers on the standard library; every error message has the key replaced by [key]."""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import config

_NGROK_CODE_RE = re.compile(r"ERR_NGROK_\d+")
_NOSCRIPT_RE = re.compile(r"<noscript[^>]*>(.*?)</noscript>", re.IGNORECASE | re.DOTALL)


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


def _curl_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")


class HTTPResponseError(RuntimeError):
    """An HTTP error response (non-object JSON, unparseable body, or API error payload)."""
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(detail)


class TransportError(RuntimeError):
    """Connection/TLS/proxy transport failure before an HTTP request was delivered."""


class JSONResponseError(RuntimeError):
    """A successful HTTP response body could not be decoded as JSON."""


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_scheme = urlsplit(req.full_url).scheme.lower()
        new_scheme = urlsplit(newurl).scheme.lower()
        if new_scheme not in ("http", "https") or (old_scheme == "https" and new_scheme != "https"):
            raise urllib.error.URLError(f"Blocked unsafe redirect to {new_scheme or 'unknown'} scheme")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and config.url_origin(newurl) != config.url_origin(req.full_url):
            redirected.headers.pop("Authorization", None)
            redirected.unredirected_hdrs.pop("Authorization", None)
        return redirected


_ORIG_URLOPEN = urllib.request.urlopen
_OPENER = urllib.request.build_opener(_SafeRedirectHandler())
_ENVIRON = os.environ


def _split_curl_status(stdout: str) -> tuple[str, int | None]:
    if "\n" in stdout:
        head, tail = stdout.rsplit("\n", 1)
        if len(tail.strip()) == 3 and tail.strip().isdigit():
            return head, int(tail.strip())
    return stdout, None


def _normalize_curl_proxy(value: str) -> str:
    raw = value.strip()
    if raw.lower().startswith("socks://"):
        return "socks5h://" + raw[8:]
    return raw


def _curl_proxy_env(url: str) -> dict[str, str] | None:
    """Propagate OS/registry proxies (e.g. Windows Clash system proxy) to curl when env proxies are unset."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return None
    env_keys = {k.lower() for k, v in _ENVIRON.items() if v.strip()}
    if "all_proxy" in env_keys or f"{scheme}_proxy" in env_keys:
        return None
    try:
        if urllib.request.proxy_bypass(parts.netloc or host):
            return None
        proxies = urllib.request.getproxies()
    except Exception:
        return None
    fallback = proxies.get("all") or proxies.get("socks") or ""
    added = {}
    for proto in ("http", "https"):
        if f"{proto}_proxy" in env_keys:
            continue
        val = (proxies.get(proto) or fallback).strip()
        if val:
            added[f"{proto.upper()}_PROXY"] = _normalize_curl_proxy(val)
    if f"{scheme.upper()}_PROXY" not in added:
        return None
    return {**_ENVIRON, **added}


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
        open_fn = _OPENER.open if urllib.request.urlopen is _ORIG_URLOPEN else urllib.request.urlopen
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
            raw_text = exc.read().decode(errors="replace")
            exc.close()
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
                parsed.setdefault("_http_status", exc.code)
                return parsed
            last = f"{exc.code}: {_summarize_error_body(raw_text, key, limit=200)}"
        except Exception as exc:
            error_cls = TransportError if isinstance(exc, urllib.error.URLError) else RuntimeError
            last = _scrub(f"{type(exc).__name__}: {exc}", key)
        if attempt + 1 < tries:
            time.sleep(4 * (attempt + 1))
    raise error_cls(f"{url}: {_scrub(str(last), key)[:300]}")


_NON_RETRYABLE_UPLOAD_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422}


def upload(url: str, key: str, path: Path, fields: dict | None = None, timeout: int = 300, *,
           file_field_name: str = "file", filename: str | None = None, extra_headers: dict | None = None) -> dict:
    """Multipart upload with curl via stdin config so keys never enter argv and paths with commas/semicolons stay intact."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise RuntimeError(f"upload rejected unsafe url: {url[:120]}")
    raw_path = str(path)
    if os.name != "nt":
        raw_path = raw_path.replace("\\", "\\\\")
    form_path = raw_path.replace('"', '\\"')
    file_field = f'{file_field_name}=@"{form_path}"'
    if filename:
        safe_filename = filename.replace("\\", "").replace('"', "")
        file_field += f';filename="{safe_filename}"'
    lines = [
        f'url = "{_curl_escape(url)}"',
        'request = "POST"',
    ]
    if key:
        lines.append(f'header = "{_curl_escape("Authorization: Bearer " + key)}"')
    for hk, hv in (extra_headers or {}).items():
        lines.append(f'header = "{_curl_escape(f"{hk}: {hv}")}"')
    lines.append(f'form = "{_curl_escape(file_field)}"')
    for k, v in (fields or {}).items():
        field_pair = f"{k}={v}"
        lines.append(f'form-string = "{_curl_escape(field_pair)}"')
    cfg = "\n".join(lines) + "\n"
    env = _curl_proxy_env(url)
    run_kwargs = {"env": env} if env is not None else {}
    last = ""
    for attempt in range(4):
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-w", "\n%{http_code}", "--proto", "=http,https", "--proto-redir", "=http,https", "--config", "-"],
                input=cfg, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, **run_kwargs,
            )
        except Exception as exc:
            last = _scrub(f"{type(exc).__name__}: {exc}", key)[:200]
            if attempt + 1 < 4:
                time.sleep(5 * (attempt + 1))
            continue
        if proc.returncode != 0:
            stderr = _scrub((proc.stderr or "").strip(), key)
            body, _ = _split_curl_status(proc.stdout or "")
            safe_body = _scrub(body.strip(), key)
            if stderr and safe_body:
                last = f"{stderr} | {safe_body}"[:200]
            elif stderr:
                last = stderr[:200]
            elif safe_body:
                last = f"curl exit {proc.returncode}: {safe_body}"[:200]
            else:
                last = f"curl exit {proc.returncode}"
            if attempt + 1 < 4:
                time.sleep(5 * (attempt + 1))
            continue
        raw_body, status_code = _split_curl_status(proc.stdout)
        safe_summary = _summarize_error_body(raw_body, key, limit=200)
        if status_code is not None and status_code >= 500:
            last = f"{status_code}: {safe_summary}"
            if attempt + 1 < 4:
                time.sleep(5 * (attempt + 1))
            continue
        try:
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                parsed = _scrub_json(parsed, key)
                if status_code is not None and status_code >= 400:
                    parsed.setdefault("_http_status", status_code)
                return parsed
            last = safe_summary
        except ValueError:
            last = safe_summary or _scrub(proc.stderr, key)[:200]
        if status_code in _NON_RETRYABLE_UPLOAD_STATUSES:
            raise HTTPResponseError(status_code, f"{url} {status_code}: {last}")
        if attempt + 1 < 4:
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"upload {path.name} failed: {last}")


def download(url: str, out: Path, *, key: str = "", extra_headers: dict | None = None) -> Path:
    scheme = urlsplit(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise RuntimeError(f"download rejected unsafe url: {url[:120]}")
    proto_redir = "=https" if scheme == "https" else "=http,https"
    tmp = out.with_suffix(out.suffix + ".part")
    env = _curl_proxy_env(url)
    run_kwargs = {"env": env} if env is not None else {}
    if key or extra_headers:
        cfg_lines = []
        if key:
            cfg_lines.append(f'header = "{_curl_escape("Authorization: Bearer " + key)}"')
        for hk, hv in (extra_headers or {}).items():
            cfg_lines.append(f'header = "{_curl_escape(f"{hk}: {hv}")}"')
        cfg = "\n".join(cfg_lines) + "\n"
        proc = subprocess.run(
            ["curl", "-f", "-sS", "-L", "--proto", "=http,https", "--proto-redir", proto_redir,
             "--retry", "6", "--retry-all-errors", "--retry-delay", "5", "--config", "-", "-o", str(tmp), "--", url],
            input=cfg, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, **run_kwargs,
        )
    else:
        proc = subprocess.run(
            ["curl", "-f", "-sS", "-L", "--proto", "=http,https", "--proto-redir", proto_redir,
             "--retry", "6", "--retry-all-errors", "--retry-delay", "5", "-o", str(tmp), "--", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, **run_kwargs,
        )
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raw_detail = (proc.stderr or "").strip() if isinstance(getattr(proc, "stderr", None), str) else ""
        detail = _scrub(raw_detail, key)
        suffix = f" ({detail[:200]})" if detail else (f" (curl exit {proc.returncode})" if proc.returncode != 0 else "")
        raise RuntimeError(f"download failed: {_scrub(url, key)[:120]}{suffix}")
    if out.suffix.lower() in {".mp4", ".mov", ".webm", ".mkv", ".png", ".jpg", ".jpeg", ".webp"}:
        with tmp.open("rb") as fh:
            head = fh.read(512).lstrip().lower()
        if head.startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml", b"{", b"[")):
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"download returned non-media response: {_scrub(url, key)[:120]}")
    config.atomic_replace(tmp, out)
    return out
