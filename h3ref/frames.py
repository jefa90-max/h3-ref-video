"""Storyboard frames: one GPT Image picture for the first instant of a shot, drawn on APIMart.

H3 follows a picture far more closely than words. A frame per shot keeps what the model otherwise "normalizes" — a
miniature prop stays miniature, a prop stays where the script put it — and the first frame of the film goes into every
later frame as a reference, so the actor and the set stay the same from shot to shot.
"""
import json
import re
import tempfile
import time
from pathlib import Path

from . import config, http, media
from .i18n import message

ORIENT = {"9:16": "竖屏", "16:9": "横屏", "1:1": "方形"}
TAIL = "不增加额外人物。不出现字幕、文字、水印和任何真实品牌的标志。电影级布光。"
NEUTRAL_WORDS = {
    "死命": "使劲", "死死": "紧紧", "死盯": "紧盯", "死角": "隐蔽角落", "死板": "僵硬", "死心": "放下执念",
    "死寂": "寂静", "死撑": "勉强支撑", "死守": "坚守", "死抓": "紧抓", "死气沉沉": "沉闷",
    "死缠烂打": "反复纠缠", "死里逃生": "脱离险境", "死胡同": "尽头小巷", "死记硬背": "机械记忆",
    "笑死": "大笑", "吓死": "十分吃惊", "拼死": "奋力", "生死": "艰难抉择",
    "杀死": "制止", "打死": "阻止", "致死": "使人倒地", "死亡": "静止不动", "死去": "静止不动",
    "死了": "静止不动了", "死者": "静卧的人物", "死": "静止", "尸体": "静卧的人物", "尸": "静卧人物", "血腥": "紧张", "鲜血": "深色污渍",
    "流血": "衣物沾有污渍", "血迹": "深色痕迹", "血": "深色痕迹", "伤口": "衣物褶皱",
    "杀戮": "冲突", "屠杀": "冲突", "砍杀": "对峙", "自杀": "独自沉思", "自残": "独自沉思",
    "肢解": "画面切换", "断肢": "散落的道具", "内脏": "道具", "裸露": "衣着完整", "裸体": "衣着完整",
}
NEUTRAL_PATTERN = re.compile("|".join(re.escape(word) for word in sorted(NEUTRAL_WORDS, key=len, reverse=True)))
MODERATION = re.compile(r"content[_ -]?(?:policy|filter|moderation)|moderation[_ -]?(?:blocked|failed)|"
                        r"policy[_ -]violation|safety[_ -](?:system|filter|violation)|sensitive[_ -]content|"
                        r"nsfw|内容审核|审核[未不]通过|敏感内容|内容违规|违反.{0,12}(?:政策|安全)", re.I)


class SubmissionRejected(RuntimeError):
    """The image API explicitly rejected creation of a new task."""


class ContentPolicyError(SubmissionRejected):
    def __init__(self, reason):
        self.reason = str(reason)
        super().__init__(message("frame_moderation_failed", reason=self.reason))


def is_moderation_error(error) -> bool:
    return bool(MODERATION.search(json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else str(error)))


def task_pending(record) -> bool:
    return bool(record.get("task") and record.get("error")
                and not record.get("task_failed") and not record.get("moderation_error"))


def prompt(film: dict, shot: dict, who: list[dict], with_board: bool, with_anchor: bool) -> str:
    """Chinese prompt for one frame. Pictures in order: board (optional), each character in it, the anchor frame."""
    aspect = film["aspect"]
    lines = [f"一张{ORIENT.get(aspect, '竖屏')}电影剧照，画幅 {aspect}，它是一段视频里一个镜头的第一帧。画风：{(film.get('look') or '').strip()}"]
    k = 1
    if with_board:
        lines.append("参考图片1是本片的角色、道具和场景参考总表，只用来统一外观，不要把总表、白底和文字标注画进画面。")
        k = 2
    if (film.get("setting") or "").strip():
        lines.append("场景与关键道具：" + film["setting"].strip())
    cam = (shot.get("camera") or "").strip().strip("【】")
    lines.append("画面：" + (f"【{cam}】" if cam else "") + (shot.get("action") or "").strip().rstrip("。")
                 + "。画的是这个镜头刚开始的一瞬间，动作还没做完。")
    for c in who:
        look = (c.get("look") or "").strip().rstrip("。")
        lines.append(f"参考图片{k}是{c['name']}" + (f"：{look}" if look else "") + "。长相、发型、妆容和服装严格照参考图。")
        k += 1
    if with_anchor:
        lines.append(f"参考图片{k}是同一部片子另一个镜头的画面：人物的长相、妆容、服装和场景的样子与它保持一致，构图按上面的画面描述来。")
    lines.append(TAIL)
    return NEUTRAL_PATTERN.sub(lambda match: NEUTRAL_WORDS[match.group()], "\n".join(lines))


def _upload(path: Path) -> str:
    out = http.upload(config.APIMART_BASE + "/uploads/images", config.apimart_key(), path)
    if is_moderation_error(out.get("error") or out):
        raise ContentPolicyError(json.dumps(out, ensure_ascii=False))
    url = out.get("url") or (out.get("data") or {}).get("url")
    if not url:
        raise RuntimeError("APIMart upload failed: " + json.dumps(out, ensure_ascii=False)[:200])
    return url


def submit(text: str, refs: list[Path], aspect: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        urls = [_upload(media.shrink(Path(p), Path(tmp) / f"ref{i}.jpg")) for i, p in enumerate(refs)]
    body = {"model": config.IMAGE_MODEL, "prompt": text, "size": aspect, "resolution": config.IMAGE_RESOLUTION,
            "quality": config.IMAGE_QUALITY, "n": 1}
    if urls:
        body["image_urls"] = urls
    try:
        # A rejected submission must reach the caller before any HTTP-level retry.
        out = http.request(config.APIMART_BASE + "/images/generations", config.apimart_key(), body, tries=1)
    except RuntimeError as exc:
        if is_moderation_error(exc):
            raise ContentPolicyError(str(exc)) from exc
        raise
    if is_moderation_error(out.get("error") or out):
        raise ContentPolicyError(json.dumps(out, ensure_ascii=False))
    task = ((out.get("data") or [{}])[0] or {}).get("task_id")
    if not task:
        error = SubmissionRejected if out.get("error") or (isinstance(out.get("code"), int) and out["code"] != 0) else RuntimeError
        raise error("APIMart gave no task id: " + json.dumps(out, ensure_ascii=False)[:200])
    return task


def poll(task: str) -> dict:
    """{state: running|done|failed, url, cny, error}; APIMart reports cost in US dollars."""
    try:
        out = http.request(config.APIMART_BASE + f"/tasks/{task}", config.apimart_key())
    except RuntimeError as exc:
        if is_moderation_error(exc):
            raise ContentPolicyError(str(exc)) from exc
        raise
    st = out.get("data") or out
    if not st.get("status") and is_moderation_error(st.get("error") or st):
        raise ContentPolicyError(json.dumps(st, ensure_ascii=False))
    usd = st.get("cost")
    res = {"state": "running", "cny": round(float(usd) * config.USD_CNY, 4) if usd is not None else None}
    if st.get("status") == "completed":
        images = (st.get("result") or {}).get("images") or []
        urls = (images[0].get("url") if images else None) or []
        res.update(state="done", url=urls[0] if isinstance(urls, list) and urls else urls or None)
        if not res["url"]:
            res.update(state="failed", error="completed without an image url")
    elif st.get("status") in ("failed", "cancelled"):
        err = st.get("error") or {}
        detail = json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err or st.get("status"))
        res.update(state="failed", error=detail, moderation_failed=is_moderation_error(err))
    return res


def draw(text: str, refs: list[Path], aspect: str, out: Path, record=lambda **k: None, task: str | None = None) -> float:
    """Draw one frame to `out`; `record(task=…)` is called with the task id before waiting, so a rerun resumes it.
    Returns the cost in CNY."""
    if not task:
        record(submission_unknown=True)
        try:
            task = submit(text, refs, aspect)
        except SubmissionRejected:
            record(submission_unknown=False)
            raise
    record(task=task, task_failed=False, submission_unknown=False)
    while True:
        st = poll(task)
        if st["state"] == "done":
            http.download(st["url"], out)
            return float(st.get("cny") or 0)
        if st["state"] == "failed":
            if st.get("moderation_failed") or is_moderation_error(st.get("error")):
                error = ContentPolicyError(st.get("error"))
                record(task_failed=True, moderation_error=error.reason, error=str(error))
                raise error
            error = "frame failed: " + str(st.get("error"))
            record(task_failed=True, moderation_error=None, error=error)
            raise RuntimeError(error)
        time.sleep(6)
