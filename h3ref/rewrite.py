"""Rewrite a Seedance-style prompt (Chinese, storyboard lines, reference pictures named 参考图片N) into MiniMax H3's
official full-reference (Ref2VA) format.

H3 reads Seedance prompts badly: it lays music under most clips, burns subtitles into Chinese dialogue and under-acts,
because the workflow has no negative branch, so "不要背景音乐、字幕" reads as music and subtitles. Rewritten in MiniMax's
own format (English, six sections, <Picture N> by input order, no negative phrasing, music N/A), music dropped from
7 clips in 7 to 1 in 13 and H3 matched Seedance clip for clip in blind pairs. The rewrite is done by a language model
given the official guide (fetched from MiniMax's repository), the rules below and two tested examples.
"""
import glob
import json
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from pathlib import Path

from . import config, http
from .i18n import message

SECTIONS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"]
EXAMPLES = config.ROOT / "prompts/h3_examples.md"
_GUIDE_LOCK = threading.Lock()

RULES = """Rules learned on this workflow (they override nothing in the guide, they make it concrete):
1. Output exactly the six sections, in order, each as `name:` on its own line followed by its text. English only, except
   dialogue inside <d> and visible scene text. No Markdown, no code fences, no comments before or after.
2. <Picture N> is the N-th reference image in input order; the input names each one as 参考图片N. Give every image a role:
   - the reference sheet (参考总表 / 总表) is a design reference: cite it inside the Subject for the sets and props it designs;
   - an image that 只约束某角色身份 defines one <Subject> (carry over every look detail from the input: hair, eyes, outfit, props);
   - a multi-character group sheet (多角色合图) maps a single <Picture N> to multiple distinct <Subject> entries — match each active character by the exact outfit, hair and facial features specified in the input so identities never swap, and if the input mentions absent sheet-mates (未出场角色), omit those absent characters completely from subject_definitions and every shot;
   - a key-frame image (「…」这个画面的样子) is the composition reference for the matching shot when that shot is in this segment,
     otherwise say it maps to no shot in this part;
   - a shot's first frame (第N个镜头…的首帧 / 从参考图片K开始 / 切至参考图片K) is the opening frame of that [Shot N]: explicitly start [Shot N] from `<Picture K>` as its opening composition guidance (not a hard timed keyframe), then move straight into the motion progression;
   - the previous segment's last frame (上一段的最后一个画面) is the first frame of [Shot 1] when [Shot 1] continues the same
     place and moment, otherwise a continuity reference for the set, light and characters' state.
3. Keep every storyboard beat, in order, with its exact camera movement (angle, framing, pan/push-in/tracking/handheld shake) and action. The first shot has no timestamp; each later beat
   starts `[Shot N] At MM:SS.mmm, the camera cuts to …` with two-digit minutes and seconds (e.g. `00:04.000`). Add no beats, drop none, stay inside
   the duration.
4. Spoken lines: exactly the lines the input gives, in the language it names, as `<Subject N> (S1) … says …, <d>[Language] …</d>`.
   Speaker ids count in order of first speech. With no lines in a segment, write no <d> at all.
5. Never write a negative instruction (no / not / without / avoid / never / don't / free of …) about music, subtitles,
   captions, text, watermarks, logos, brands, narration or singing. The workflow has no negative prompt, so naming a thing
   brings it in. Drop the input's 不要背景音乐、字幕、水印、品牌 sentences entirely; describe only what is there.
6. non_diegetic_music is always N/A. overall_soundscape lists the concrete diegetic sounds of these actions and places.
7. Acting is written as concrete faces and bodies, never as "exaggerated" or "comic". When the script says 面无表情 /
   一本正经, say plainly that the face stays blank and serious the whole time. Big reactions (shock, pain, laughing) get
   specific details: eyes snapping wide, jaw dropping, wincing with a hand on the cheek, doubled over laughing.
8. Open detailed_description with one or two sentences of style taken from the input's style sentence, including the
   aspect ratio, then [Shot 1].
9. Define each character's full appearance once in subject_definitions; inside detailed_description, refer to them by `<Subject N>` without re-reciting their full outfit/hair or static background furniture in every shot. When a shot opens from a first-frame `<Picture N>`, spend the detail budget on motion progression (cause -> contact -> release/landing), camera movement, concrete facial/body reactions, gaze shifts, and synchronized diegetic sounds. Keep body facing, gaze, pose and visible appearance separate, and keep prop opening direction/orientation distinct from screen cropping. ReferenceToVideo images are composition references, not guaranteed hard timed keyframes."""

NEGATIVE = re.compile(r"\b(no|not|without|never|avoid|don't|do not|free of)\b[^.\n]{0,40}\b(music|soundtrack|score|subtitles?|captions?|"
                      r"on-screen text|text overlays?|watermarks?|logos?|brands?|narration|voice-?over|singing)\b", re.I)


class RewriteError(RuntimeError):
    pass


class RewriteFatalError(RewriteError):
    pass


class RewriteResponseError(RewriteError):
    """The provider completed the request without usable response text."""


FATAL_REWRITE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}


def guide() -> str:
    """MiniMax's full-reference guide: a local copy when H3_GUIDE_FILE is set, else fetched once and cached."""
    if config.GUIDE_FILE:
        return Path(config.GUIDE_FILE).expanduser().read_text(encoding="utf-8")
    cache = config.CACHE / "ref-en.txt"
    with _GUIDE_LOCK:
        if not cache.is_file() or not cache.stat().st_size:
            req = urllib.request.Request(config.GUIDE_URL, headers={"User-Agent": config.UA})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    text = r.read().decode("utf-8")
            except Exception as exc:
                raise RewriteError(f"取不到 MiniMax 官方提示词指南（{config.GUIDE_URL}）：{type(exc).__name__}。"
                                   "下载 ref-en.txt 后用 H3_GUIDE_FILE 指向它") from None
            if not text.strip():
                raise RewriteError(f"取不到 MiniMax 官方提示词指南（{config.GUIDE_URL}）：内容为空")
            cache.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".ref-en-", suffix=".tmp", dir=cache.parent)
            tmp_path = Path(tmp_name)
            try:
                with open(fd, "w", encoding="utf-8") as handle:
                    handle.write(text)
                config.atomic_replace(tmp_path, cache)
            finally:
                tmp_path.unlink(missing_ok=True)
        return cache.read_text(encoding="utf-8")


def request(prompt_zh: str, n_images: int, seconds: float, aspect: str, feedback: str = "", *,
            rules_override: str | None = None, examples_override: str | None = None,
            guide_override: str | None = None) -> str:
    return "\n\n".join([
        "You rewrite one video-generation prompt for MiniMax H3 in its full-reference (Ref2VA) mode. "
        "Read the official guide, the rules and the two tested examples, then rewrite the input.",
        "=== OFFICIAL GUIDE ===\n" + (guide() if guide_override is None else guide_override),
        "=== RULES ===\n" + (RULES if rules_override is None else rules_override),
        "=== EXAMPLES ===\n" + (EXAMPLES.read_text(encoding="utf-8") if examples_override is None else examples_override),
        f"=== INPUT ===\nThis segment is {seconds:g} seconds, aspect ratio {aspect}, and the request carries {n_images} reference "
        f"images in this order (Picture 1 … Picture {n_images}).\n\n{prompt_zh}",
        (f"=== FIX ===\nYour previous rewrite had these problems; fix them:\n{feedback}" if feedback else ""),
        "Write the rewrite now: the six sections only."]).strip()


_THINK_EXACT_RE = re.compile(r"<think>|</think>", re.IGNORECASE)
_THINK_ANY_RE = re.compile(r"</?think", re.IGNORECASE)


def strip_think_blocks(raw: str, error_factory) -> str:
    pos = 0
    chunks = []
    while True:
        open_match = _THINK_EXACT_RE.search(raw, pos)
        if open_match is None:
            chunks.append(raw[pos:])
            break
        if open_match.group(0)[1] == "/":
            raise error_factory()
        chunks.append(raw[pos:open_match.start()])
        close_match = _THINK_EXACT_RE.search(raw, open_match.end())
        if close_match is None or close_match.group(0)[1] != "/":
            raise error_factory()
        pos = close_match.end()
    rem = "".join(chunks)
    if _THINK_ANY_RE.search(rem):
        raise error_factory()
    return rem.strip()


def _strip_think_blocks_linear(raw: str) -> str:
    return strip_think_blocks(raw, lambda: RewriteError(message("rewrite_empty")))


def _strip_reasoning_and_fences(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    cleaned = _strip_think_blocks_linear(raw)
    if not cleaned:
        return ""
    if cleaned.startswith("```"):
        nl = cleaned.find("\n")
        close_idx = cleaned.find("```", 3)
        if nl == -1 or close_idx == -1 or close_idx != len(cleaned) - 3:
            raise RewriteError("代码围栏未闭合或包含围栏外多余内容")
        header = cleaned[3:nl].strip()
        if header and not header.isalnum():
            raise RewriteError("代码围栏标记无效")
        cleaned = cleaned[nl + 1:close_idx].strip()
    if "```" in cleaned:
        raise RewriteError("提示词包含多余代码围栏")
    return cleaned


def strip_h3_wrappers(raw: str) -> str:
    """Validate and remove <think>...</think> and outer code fences in O(N) time."""
    return _strip_reasoning_and_fences(raw)


def sections(text: str) -> dict:
    """{section name: body}; raises when a section is missing, empty, or out of order."""
    text = _strip_reasoning_and_fences(text)
    if not text:
        raise RewriteError(message("rewrite_empty"))
    found = [(m.start(), m.group(1)) for m in re.finditer(r"^\s*(" + "|".join(SECTIONS) + r")\s*:", text, re.M)]
    names = [n for _, n in found]
    if names != SECTIONS or found[0][0] != 0:
        raise RewriteError("六块不全或顺序不对：" + "、".join(names))
    out = {}
    for k, (pos, name) in enumerate(found):
        end = found[k + 1][0] if k + 1 < len(found) else len(text)
        out[name] = text[pos:end].split(":", 1)[1].strip()
    empty = [name for name in SECTIONS if not out[name]]
    if empty:
        raise RewriteError(message("rewrite_empty_sections", names="、".join(empty)))
    return out


def _picture_problems(parts: dict, n_images: int) -> list[str]:
    out = []
    section_body = "\n".join(parts.values())
    for token in re.findall(r"<Picture([^>]*)>", section_body):
        if not re.fullmatch(r"\s+[0-9]+\s*", token):
            out.append(message('rewrite_picture_token', value=('<Picture' + token + '>')[:100]))
            continue
        number = int(token)
        if not 1 <= number <= n_images:
            out.append(message('rewrite_picture_bound', number=number, count=n_images))
    return out


def cached_problems(text: str, n_images: int) -> list[str]:
    """Structural and <Picture N> bound checks for cached paired H3 prompts."""
    try:
        parts = sections(text)
    except RewriteError as exc:
        return [str(exc)]
    return _picture_problems(parts, n_images)


def problems(text: str, n_images: int) -> list[str]:
    try:
        parts = sections(text)
    except RewriteError as exc:
        return [str(exc)]
    out = []
    for name in ("detailed_description", "overall_soundscape"):
        for m in NEGATIVE.finditer(parts[name]):
            out.append(f"{name} still has a negative instruction: “{m.group(0)}”; remove it and describe only what is there")
    out.extend(_picture_problems(parts, n_images))
    if "[Shot 1]" not in parts["detailed_description"]:
        out.append("detailed_description has no [Shot 1]")
    return out


def assemble(parts: dict) -> str:
    parts = {**parts, "non_diegetic_music": "N/A"}
    return "\n\n".join(f"{name}:\n{parts[name]}" for name in SECTIONS)


def claude_bin() -> str | None:
    """CLAUDE_BIN, `claude` on PATH, or the copy bundled with the Claude desktop app on macOS."""
    if config.env("CLAUDE_BIN"):
        return config.env("CLAUDE_BIN")
    if shutil.which("claude"):
        return shutil.which("claude")
    bundles = []
    for path in glob.glob(str(Path.home() / "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude")):
        version = Path(path).parts[-6]
        if re.fullmatch(r"\d+(\.\d+)*", version):
            bundles.append((tuple(int(x) for x in version.split(".")), path))
    return max(bundles)[1] if bundles else None


def _claude(prompt: str) -> str:
    """The local Claude Code CLI under your own login (or CLAUDE_CODE_OAUTH_TOKEN), headless, all tools denied, run
    from an empty directory so no project files are read."""
    exe = claude_bin()
    if not exe:
        raise RewriteFatalError("没找到 claude 命令行；装上 Claude Code，或者设 H3_REWRITER=llm 用别的模型")
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.run([exe, "-p", "--output-format", "json", "--max-turns", "1", "--model", config.CLAUDE_MODEL,
                               "--tools", "", "--strict-mcp-config",
                               "--disallowedTools", "Bash,Edit,Write,Read,Glob,Grep,WebFetch,WebSearch,NotebookEdit,Task,Agent"],
                              input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900, cwd=cwd)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        err_cls = RewriteFatalError if proc.returncode else RewriteResponseError
        raise err_cls(f"Claude 没有返回结果：{(proc.stderr or proc.stdout)[:300]}") from None
    if not isinstance(out, dict):
        err_cls = RewriteFatalError if proc.returncode else RewriteResponseError
        raise err_cls(message("rewrite_empty"))
    if out.get("is_error"):
        raise RewriteFatalError(f"Claude 调用失败：{str(out.get('result'))[:300]}")
    result = str(out.get("result") or "")
    if not result.strip():
        err_cls = RewriteError if proc.returncode else RewriteResponseError
        raise err_cls("Claude 没有返回内容")
    return result


def _llm(prompt: str, *, tries: int = 3) -> str:
    llm = config.llm_settings()
    try:
        out = http.request(llm["base_url"] + "/chat/completions", config.llm_key(),
                           {"model": llm["rewrite_model"], "messages": [{"role": "user", "content": prompt}], "max_tokens": 8000, "stream": False},
                           timeout=600, tries=tries)
    except http.HTTPResponseError as exc:
        err_cls = RewriteFatalError if exc.status_code in FATAL_REWRITE_STATUSES else RewriteError
        raise err_cls("改写模型请求失败：" + str(exc)[:300]) from None
    if not isinstance(out, dict):
        raise RewriteResponseError(message("rewrite_empty"))
    http_status = out.get("_http_status")
    if (isinstance(http_status, int) and http_status >= 400) or (out.get("error") and "choices" not in out):
        err_obj = out.get("error")
        raw_code = err_obj.get("code") if isinstance(err_obj, dict) else None
        detail = json.dumps(out, ensure_ascii=False)
        low = detail.lower()
        is_transient_5xx = (
            (isinstance(http_status, int) and http_status >= 500 and raw_code not in FATAL_REWRITE_STATUSES)
            or (isinstance(raw_code, int) and raw_code >= 500)
        ) and not any(tok in low for tok in ("invalid_api_key", "api_key_invalid", "unauthorized", "insufficient_quota", "model_not_found"))
        err_cls = RewriteError if is_transient_5xx else RewriteFatalError
        raise err_cls("改写模型请求失败：" + detail[:300])
    choices = out.get("choices")
    first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    msg = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = msg.get("content")
    if not isinstance(text, str) or not text.strip():
        raise RewriteResponseError(message("rewrite_empty"))
    return text


def backend(rewriter_override: str | None = None) -> str:
    selected = config.env("H3_REWRITER", "auto") if rewriter_override is None else rewriter_override
    if selected in ("claude", "llm"):
        return selected
    if rewriter_override not in (None, "auto"):
        raise ValueError(f"Unsupported rewriter: {rewriter_override!r}")
    return "claude" if claude_bin() else "llm"


def rewrite(prompt_zh: str, n_images: int, seconds: float, aspect: str, *, max_retries: int = 6,
            rewriter_override: str | None = None, infer=None, **request_overrides) -> str:
    """The H3 prompt for one clip. Retries up to `max_retries` times with concrete validation feedback."""
    selected = backend() if rewriter_override is None else backend(rewriter_override)
    call = infer if infer is not None else (_claude if selected == "claude" else _llm)
    attempts = max(1, int(max_retries) + 1)
    feedback = ""
    for _ in range(attempts):
        req_text = request(prompt_zh, n_images, seconds, aspect, feedback, **request_overrides)
        try:
            raw = call(req_text)
        except RewriteFatalError:
            raise
        except RewriteError as exc:
            issues = [str(exc)]
        else:
            try:
                cleaned = _strip_reasoning_and_fences(raw or "")
            except RewriteError as exc:
                issues = [str(exc)]
            else:
                issues = [message("rewrite_empty")] if not cleaned else problems(cleaned, n_images)
                if not issues:
                    return assemble(sections(cleaned))
        feedback = "\n".join(f"- {x}" for x in issues)
    raise RewriteError(message("rewrite_exhausted", attempts=attempts, detail=feedback.replace("\n", " ")[:300]))


def is_rewritten(text: str) -> bool:
    """A prompt already in the six-section format goes to H3 as it is."""
    try:
        sections(text)
        return True
    except RewriteError:
        return False
