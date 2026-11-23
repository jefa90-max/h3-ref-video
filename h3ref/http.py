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
        noscript = _NOSCRIPT_RE.search(safe_text)
        if noscript:
            summary = " ".join(noscript.group(1).split())
            if summary:
                return summary[:limit]
        return ngrok_match.group(0)
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
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"), headers=headers)
        open_fn = _OPENER.open if urllib.request.urlopen is _ORIG_URLOPEN else urllib.request.urlopen
        try:
            with open_fn(req, timeout=timeout) as r:
                parsed_ok = json.loads(r.read().decode(errors="replace"))
                return _scrub_json(parsed_ok, key)
        except urllib.error.HTTPError as exc:
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
            last = _scrub(f"{type(exc).__name__}: {exc}", key)
        time.sleep(4 * (attempt + 1))
    raise RuntimeError(f"{url}: {_scrub(str(last), key)[:300]}")


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
    last = ""
    for attempt in range(4):
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-w", "\n%{http_code}", "--proto", "=http,https", "--proto-redir", "=http,https", "--config", "-"],
                input=cfg, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except Exception as exc:
            last = _scrub(f"{type(exc).__name__}: {exc}", key)[:200]
            time.sleep(5 * (attempt + 1))
            continue
        if proc.returncode != 0:
            last = (_scrub(proc.stdout, key) or _scrub(proc.stderr, key) or f"curl exit {proc.returncode}")[:200]
            time.sleep(5 * (attempt + 1))
            continue
        raw_body, status_code = proc.stdout, None
        if "\n" in proc.stdout:
            head, tail = proc.stdout.rsplit("\n", 1)
            if len(tail.strip()) == 3 and tail.strip().isdigit():
                raw_body, status_code = head, int(tail.strip())
        safe_summary = _summarize_error_body(raw_body, key, limit=200)
        if status_code is not None and status_code >= 500:
            last = f"{status_code}: {safe_summary}"
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
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"upload {path.name} failed: {last}")


def download(url: str, out: Path, *, key: str = "", extra_headers: dict | None = None) -> Path:
    if urlsplit(url).scheme not in ("http", "https"):
        raise RuntimeError(f"download rejected unsafe url: {url[:120]}")
    tmp = out.with_suffix(out.suffix + ".part")
    if key or extra_headers:
        cfg_lines = []
        if key:
            cfg_lines.append(f'header = "{_curl_escape("Authorization: Bearer " + key)}"')
        for hk, hv in (extra_headers or {}).items():
            cfg_lines.append(f'header = "{_curl_escape(f"{hk}: {hv}")}"')
        cfg = "\n".join(cfg_lines) + "\n"
        ok = subprocess.run(
            ["curl", "-f", "-sS", "-L", "--proto", "=http,https", "--proto-redir", "=http,https",
             "--retry", "6", "--retry-all-errors", "--retry-delay", "5", "--config", "-", "-o", str(tmp), "--", url],
            input=cfg, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        ).returncode == 0
    else:
        ok = subprocess.run(
            ["curl", "-f", "-sS", "-L", "--proto", "=http,https", "--proto-redir", "=http,https",
             "--retry", "6", "--retry-all-errors", "--retry-delay", "5", "-o", str(tmp), "--", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
        ).returncode == 0
    if not ok or not tmp.exists() or tmp.stat().st_size < 1000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"download failed: {_scrub(url, key)[:120]}")
    config.atomic_replace(tmp, out)
    return out
