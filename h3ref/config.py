"""Keys, endpoints and defaults. Keys come from the environment or a `.env` file at the repo root (see `.env.example`);
they are read, never written or logged."""
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent


def read_env_file(path: Path = ROOT / ".env") -> dict[str, str]:
    """Parse KEY=value lines from a .env file without mutating os.environ."""
    if not path.is_file():
        return {}
    parsed = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        parsed[k.strip()] = v.strip().strip('"').strip("'")
    return parsed


def _load_env_file(path: Path = ROOT / ".env") -> dict[str, str]:
    """KEY=value lines; values already in the environment win."""
    if os.environ.get("H3_SKIP_DOTENV") == "1":
        return {}
    seeded = {}
    for k, v in read_env_file(path).items():
        if k not in os.environ:
            os.environ[k] = v
            seeded[k] = v
    os.environ.setdefault("_H3_DOTENV_LOADED", "1")
    return seeded


def url_origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    try:
        port = parts.port or default_port
    except ValueError:
        port = None
    return (scheme, (parts.hostname or "").lower(), port)


def atomic_replace(source: Path, target: Path, *, sleep=time.sleep):
    """Replace target with source, retrying transient Windows reader sharing violations."""
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                raise
            sleep(min(0.05 * 2 ** attempt, 0.5))


_SEEDED_DOTENV = {} if "unittest" in sys.modules and os.environ.get("H3_FORCE_DOTENV") != "1" else _load_env_file()


def load_server_env(env_path: Path = ROOT / ".env") -> dict[str, str]:
    file_env = read_env_file(env_path)
    if "_H3_DOTENV_LOADED" not in os.environ:
        base_env = dict(os.environ)
    else:
        base_env = {k: v for k, v in os.environ.items()
                    if k != "_H3_DOTENV_LOADED" and (k not in _SEEDED_DOTENV or v != _SEEDED_DOTENV[k])}
    if env_path.resolve() == (ROOT / ".env").resolve():
        return {**file_env, **base_env}
    return {**base_env, **file_env}


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default.strip()


def need(name: str, what: str) -> str:
    v = env(name)
    if not v:
        raise SystemExit(f"缺少 {name}（{what}）：写进仓库根目录的 .env，或者 export {name}=…；格式见 .env.example")
    return v


# RunningHub: the H3 accelerated workflow (see workflows/README.md), Standard machine, 544 on the short side, 20 steps
RH_BASE = env("RUNNINGHUB_BASE", "https://www.runninghub.cn")
RH_WORKFLOW_ID = "2113525277564424193"
RH_GRAPH = ROOT / "workflows/h3-ref2va-turbo.json"
COMFY_GRAPH = ROOT / "workflows/comfy-h3-ref2va.json"
RH_CONCURRENCY = int(env("RUNNINGHUB_CONCURRENCY", "3"))          # set to the account's API allowance; website tasks share it
RH_COIN_CNY = float(env("RUNNINGHUB_COIN_CNY", str(399 / 180000)))  # what one RH coin cost you (Max monthly: ¥399 for 180k)
H3_STEPS = int(env("H3_STEPS", "20"))
SIZES = {"9:16": (544, 960), "16:9": (960, 544), "1:1": (736, 736)}
COMFY_SIZES = {"9:16": (768, 1344), "16:9": (1344, 768), "1:1": (992, 992)}
VIDEO_PROVIDERS = ("runninghub", "comfyui")
COMFY_PRESETS = {
    "4step": {"steps": 4, "enable_lora": True, "lora_name": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"},
    "8step": {"steps": 8, "enable_lora": True, "lora_name": "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"},
    "full20": {"steps": 20, "enable_lora": False, "lora_name": "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"},
}
COLAB_AUTH_MODES = ("oauth2", "adc")
MAX_IMAGES = 9                                                     # the workflow's nine LoadImage inputs
MIN_SECONDS, MAX_SECONDS = 5, 15


def _parse_positive_int(raw: str, name: str) -> int:
    try:
        parsed = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name}={raw!r} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name}={raw!r} must be >= 1")
    return parsed


def validate_endpoint_url(raw: str, name: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
        _ = parts.port
    except ValueError as exc:
        raise ValueError(f"{name} invalid URL") from exc
    if parts.username or parts.password:
        raise ValueError(f"{name} must not contain embedded credentials")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname or parts.query or parts.fragment:
        raise ValueError(f"{name}={cleaned!r} must be a clean http(s) URL")
    return cleaned.rstrip("/")


def comfy_requires_api_key(comfyui_url: str) -> bool:
    if not comfyui_url:
        return False
    parts = urlsplit(comfyui_url.strip())
    host = (parts.hostname or "").lower()
    if host in ("127.0.0.1", "localhost", "::1"):
        return False
    return parts.scheme.lower() == "https"


def video_settings(environ=None) -> dict:
    values = os.environ if environ is None else environ
    provider = values.get("VIDEO_PROVIDER", "").strip().lower() or "runninghub"
    if provider not in VIDEO_PROVIDERS:
        raise ValueError(f"VIDEO_PROVIDER={provider!r} (runninghub | comfyui)")
    preset = values.get("COMFYUI_PRESET", "").strip().lower() or "4step"
    if preset not in COMFY_PRESETS:
        raise ValueError(f"COMFYUI_PRESET={preset!r} (4step | 8step | full20)")
    comfyui_url = validate_endpoint_url(values.get("COMFYUI_URL", ""), "COMFYUI_URL")
    comfyui_api_key = values.get("COMFYUI_API_KEY", "").strip()
    comfy_concurrency = _parse_positive_int(values.get("COMFYUI_CONCURRENCY", "").strip() or "1", "COMFYUI_CONCURRENCY")
    raw_rh_conc = values.get("RUNNINGHUB_CONCURRENCY", "").strip()
    rh_concurrency = _parse_positive_int(raw_rh_conc, "RUNNINGHUB_CONCURRENCY") if raw_rh_conc else RH_CONCURRENCY
    colab_session = values.get("COLAB_SESSION", "").strip() or "comfy-a100"
    raw_auto = values.get("COLAB_AUTO_START", "").strip().lower() or "0"
    if raw_auto in ("0", "false", "no", "off"):
        colab_auto_start = False
    elif raw_auto in ("1", "true", "yes", "on"):
        colab_auto_start = True
    else:
        raise ValueError(f"COLAB_AUTO_START={raw_auto!r} (0 | 1)")
    colab_bin = values.get("COLAB_BIN", "").strip()
    colab_auth = values.get("COLAB_AUTH", "").strip().lower()
    if colab_auth and colab_auth not in COLAB_AUTH_MODES:
        raise ValueError(f"COLAB_AUTH={colab_auth!r} (oauth2 | adc)")
    return {
        "provider": provider,
        "comfyui_url": comfyui_url,
        "comfyui_api_key": comfyui_api_key,
        "comfy_requires_key": comfy_requires_api_key(comfyui_url),
        "comfyui_preset": preset,
        "comfyui_concurrency": comfy_concurrency,
        "runninghub_concurrency": rh_concurrency,
        "concurrency": comfy_concurrency if provider == "comfyui" else rh_concurrency,
        "colab_session": colab_session,
        "colab_auto_start": colab_auto_start,
        "colab_bin": colab_bin,
        "colab_auth": colab_auth,
    }


def max_shots() -> int:
    return max(1, int(env("H3_MAX_SHOTS", "1000")))


def max_segments() -> int:
    return max(1, int(env("H3_MAX_SEGMENTS", "200")))


MAX_SHOTS = max_shots()
MAX_SEGMENTS = max_segments()

# APIMart: GPT Image for storyboard frames, and by default the vision check and the prompt rewriter too
APIMART_BASE = env("APIMART_BASE_URL", "https://api.apimart.ai/v1").rstrip("/")
IMAGE_MODEL = env("IMAGE_MODEL", "gpt-image-2.6-sunburst")
IMAGE_RESOLUTION = env("IMAGE_RESOLUTION", "1k")                  # 1K looked as good as 2K once H3 redraws it at 544p
IMAGE_QUALITY = env("IMAGE_QUALITY", "medium")
USD_CNY = float(env("USD_CNY", "7.2"))

# the prompt rewrite: "claude" = the local Claude Code CLI, "llm" = any OpenAI-compatible endpoint, "auto" = claude if installed
REWRITER = env("H3_REWRITER", "auto")
CLAUDE_MODEL = env("CLAUDE_MODEL", "opus")
LLM_DEFAULTS = {
    "apimart": {"base_url": APIMART_BASE, "model": "deepseek-v4-pro"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-flash-latest"},
}


def llm_settings(environ=None) -> dict:
    values = os.environ if environ is None else environ
    provider = values.get("LLM_PROVIDER", "").strip().lower() or "apimart"
    if provider not in LLM_DEFAULTS:
        raise ValueError(f"LLM_PROVIDER={provider!r} (apimart | gemini)")
    defaults = LLM_DEFAULTS[provider]
    prefix = "GEMINI" if provider == "gemini" else "LLM"
    model = values.get(f"{prefix}_MODEL", "").strip() or defaults["model"]
    base = (defaults["base_url"] if provider == "gemini" else values.get("LLM_BASE_URL", "").strip() or defaults["base_url"]).rstrip("/")
    same_apimart_origin = url_origin(base) == url_origin(APIMART_BASE)
    key_names = ("GEMINI_API_KEY",) if provider == "gemini" else (("LLM_API_KEY", "APIMART_API_KEY") if same_apimart_origin else ("LLM_API_KEY",))
    return {"provider": provider, "base_url": base,
            "composition_model": values.get(f"{prefix}_COMPOSITION_MODEL", "").strip() or model,
            "rewrite_model": values.get(f"{prefix}_REWRITE_MODEL", "").strip() or model,
            "key_names": key_names}


VISION_BASE = env("VISION_BASE_URL", APIMART_BASE).rstrip("/")
VISION_MODEL = env("VISION_MODEL", "gemini-3.8-flash")
SUBTITLE_CHECKS_ENABLED = False

# the official prompt guide is MiniMax's; it is read from their repository, not copied here (see README)
GUIDE_URL = env("H3_GUIDE_URL", "https://raw.githubusercontent.com/MiniMax-AI/MiniMax-H3/main/skills/h3-prompt-writing/references/ref-en.txt")
GUIDE_FILE = env("H3_GUIDE_FILE")                                  # or a local copy of ref-en.txt
CACHE = Path(env("H3REF_CACHE", str(Path.home() / ".cache/h3-ref-video")))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36"


def rh_key() -> str:
    return need("RUNNINGHUB_API_KEY", "RunningHub 的 API key，控制台「API 调用」里拿")


def comfy_url(environ=None) -> str:
    url = video_settings(environ)["comfyui_url"]
    if url:
        return url
    return validate_endpoint_url(need("COMFYUI_URL", "远端 ComfyUI 隧道地址，例如 https://xxx.ngrok-free.dev"), "COMFYUI_URL")


def comfy_key(environ=None) -> str:
    return video_settings(environ)["comfyui_api_key"]


def apimart_key() -> str:
    return need("APIMART_API_KEY", "APIMart 的 key，画分镜图要用")


def llm_key() -> str:
    names = llm_settings()["key_names"]
    return next((env(name) for name in names if env(name)), "") or need(names[-1], " / ".join(names))


def vision_key() -> str:
    return env("VISION_API_KEY") or need("APIMART_API_KEY", "查烧字幕用的视觉模型 key，也可以单独配 VISION_API_KEY")
