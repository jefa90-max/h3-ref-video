"""Storyboard frames: one GPT Image picture for the first instant of a shot, drawn on APIMart.

H3 follows a picture far more closely than words. A frame per shot keeps what the model otherwise "normalizes" — a
miniature prop stays miniature, a prop stays where the script put it. Each frame uses the scene board and character
references without using other generated frames.
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


def without_anchor_reference(text: str) -> str:
    """Remove the automatic first-frame instruction saved by older versions."""
    return re.sub(r"参考图片\d+是同一部片子另一个镜头的画面：人物的长相、妆容、服装和场景的样子与它保持一致，构图按上面的画面描述来。(?:\r?\n)?", "", text)


def describe_composition(film: dict, shot: dict) -> dict:
    """Describe only this shot's opening image, using earlier shots for continuity."""
    shots = film.get("shots") or [shot]
    number = shots.index(shot) + 1
    visible = lambda item: {key: item.get(key, "") for key in ("camera", "action", "line")}
    context = {"shot_number": number, "current_shot": visible(shot),
               "previous_shots": [visible(item) for item in shots[:number - 1]], "key_scenes": film.get("setting") or ""}
    rules = ("你是分镜美术指导，为当前分镜刚开始的一瞬间编写图片生成描述。只输出JSON对象，三个非空中文字符串字段："
             "scene（当前可见的场景和必要道具）、frame（符合当前机位的具体画面和动作起始状态）、"
             "orientation（逐一明确当前人物或物体相对镜头的朝向）。"
             "当前镜头的机位和动作优先，前文仅用于理解场景和道具状态的连续性。关键场景参考只提供外观线索，"
             "按当前镜头需要选择，不能整段照抄或把不同地点的道具都放进画面；没有换场依据时沿用当前场景。"
             "不提前展示尚未发生的动作结果、未打开容器内的物品或此刻不可见的道具，不添加剧情。"
             "按机位与动作推断朝向，不默认人物或物体正对镜头。背后跟拍、从身后看或背影要明确写主体背对镜头，"
             "展示后背、后脑勺；物体背向镜头时写明其背面朝向镜头。不能把背对其他人物或窗户等同于背对镜头。"
             "角色参考图只用于外观，不能覆盖镜头朝向。分镜号仅用于查找参考图对应的关键场景，不画进图片。"
             "不要引用首镜参考图，不输出参考图片编号，不输出说明或Markdown。")
    out = http.request(config.LLM_BASE + "/chat/completions", config.llm_key(),
                       {"model": config.LLM_MODEL, "messages": [{"role": "system", "content": rules},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)}], "max_tokens": 8000, "stream": False},
                       timeout=600, tries=1)
    try:
        composition = json.loads(out["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300])) from exc
    if not isinstance(composition, dict) or any(not isinstance(composition.get(key), str) or not composition[key].strip()
                                              for key in ("scene", "frame", "orientation")):
        raise RuntimeError(message("frame_composition_invalid", detail=json.dumps(composition, ensure_ascii=False)[:300]))
    return {key: value.strip() for key, value in composition.items() if key in ("scene", "frame", "orientation")}


def prompt(film: dict, shot: dict, who: list[dict], with_board: bool, composition: dict | None = None) -> str:
    """Chinese prompt for one frame. Pictures in order: board (optional), each character in it."""
    aspect = film["aspect"]
    lines = [f"一张{ORIENT.get(aspect, '竖屏')}电影剧照，画幅 {aspect}，它是一段视频里一个镜头的第一帧。画风：{(film.get('look') or '').strip()}"]
    number = (film.get("shots") or [shot]).index(shot) + 1
    lines.append(f"当前分镜号：第{number}镜（仅用于查找参考图对应的关键场景，不在画面中显示编号）。")
    k = 1
    if with_board:
        lines.append(f"参考图片1是本片的角色、道具和场景参考总表，优先查找第{number}镜对应的关键场景，只采用当前镜头需要的外观线索。不要把其他场景、总表、白底和文字标注画进画面。")
        k = 2
    cam = (shot.get("camera") or "").strip().strip("【】")
    if composition:
        lines.append("场景与关键道具：" + composition["scene"])
        lines.append("主体朝向（相对镜头）：" + composition["orientation"])
    lines.append("画面：" + (f"【{cam}】" if cam else "") + (composition["frame"] if composition else (shot.get("action") or "").strip()).rstrip("。")
                 + "。画的是这个镜头刚开始的一瞬间，动作还没做完。")
    for c in who:
        look = (c.get("look") or "").strip().rstrip("。")
        lines.append(f"参考图片{k}是{c['name']}" + (f"：{look}" if look else "") + "。长相、发型、妆容和服装严格照参考图，朝向按当前镜头描述，不沿用参考图中的正面姿态。")
        k += 1
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
