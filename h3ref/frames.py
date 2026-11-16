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

from . import config, http, media, staging
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


NON_RETRYABLE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}
MAX_POLL_ATTEMPTS = 600


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


def describe_composition(film: dict, shot: dict, *, previous_composition: dict | None = None,
                         known_scenes: dict | None = None) -> dict:
    """Describe only this shot's opening image, using earlier shots for continuity."""
    shots = film.get("shots") or [shot]
    number = shots.index(shot) + 1
    visible = lambda item: {key: item.get(key, "") for key in ("camera", "action", "line")}
    context = {"shot_number": number, "current_shot": visible(shot),
               "previous_shots": [visible(item) for item in shots[:number - 1]], "key_scenes": film.get("setting") or "",
               "cast": film.get("cast") or [], "previous_composition": previous_composition,
               "known_scenes": known_scenes or {}}
    rules = ("你是分镜美术指导。只输出 composition v2 JSON 对象，不输出 Markdown。必填 version=2；非空字符串 "
             "scene/frame/orientation/scene_id/world_frame/action；fixed_objects 是 {name,position} 数组；"
             "camera 是 {position,view_direction,framing,axis_side}，轴线不适用要明确写不适用；"
             "characters 是人物状态数组，props 是道具状态数组；expected_end={characters:[],props:[]}。"
             "characters 每项必填非空字符串字段：" + ",".join(staging.CHARACTER_FIELDS) + "。props 每项必填字段："
             + ",".join(staging.PROP_FIELDS) + "。除 opening_direction/base_direction/knot_position 在无对应部件时允许 null，"
             "其余为非空字符串；visibility 只能 visible/cropped/occluded/offscreen。所有数组必填，允许无人/无道具。"
             "name 使用 cast 正式姓名，未绑定人物保留原身份，不新增参考图。appearance 只含本镜可见稳定外貌衣着；"
             "cast.look 中旧姿势/持物/毛巾位置不能当作稳定身份，本镜动作、位置、身体朝向、视线优先。"
             "characters/props/frame 只写首帧动作起点，不提前展示交接完成；action 保留原镜头完整多拍动作、"
             "接触者、接触方式、释放和落点，结束状态只放 expected_end。expected_end 是计划而非图片事实。"
             "previous_composition 是最近可信前镜计划，原剧本补充中间缺失动作，不伪造失败镜头的结果。"
             "known_scenes 的 world_frame/fixed_objects 逐字复用；反打只改机位/画面左右，不能移动固定物。"
             "换场新 scene_id，返回已知地点复用其 ID/布局，不能把另一地点的人物持物强加当前场景。"
             "世界位置与 screen_position/visibility 分开；袋口、袋底、结扣各写物体方向，裁切不能清除已知方向。"
             "机位、景别、运镜、台词与原动作不改；背影不要求露脸，转头视线允许与身体不同。"
             "不把参考图的正面姿势当成当前姿势，不引用首镜或其它生成图，不输出参考编号，不画文字。")
    llm = config.llm_settings()
    out = http.request(llm["base_url"] + "/chat/completions", config.llm_key(),
                       {"model": llm["composition_model"], "messages": [{"role": "system", "content": rules},
                        {"role": "user", "content": json.dumps(context, ensure_ascii=False)}], "max_tokens": 8000, "stream": False},
                       timeout=600, tries=1)
    if not isinstance(out, dict):
        raise http.HTTPResponseError(400, message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300]))
    if (out.get("error") or out.get("_http_status", 200) >= 400) and "choices" not in out:
        err_obj = out.get("error")
        detail = json.dumps(out, ensure_ascii=False)
        low = detail.lower()
        http_status = out.get("_http_status")
        raw_code = err_obj.get("code") if isinstance(err_obj, dict) else None
        if http_status in (401, 403) or raw_code in (401, 403, "invalid_api_key", "api_key_invalid", "unauthorized", "authentication_error") or any(
            tok in low for tok in ("invalid_api_key", "api_key_invalid", "api key not valid", "incorrect api key", "unauthorized", "forbidden")
        ):
            code = 403 if http_status == 403 or raw_code == 403 else 401
        elif isinstance(raw_code, int):
            code = raw_code
        elif isinstance(http_status, int) and http_status >= 400:
            code = http_status
        else:
            code = 400
        raise http.HTTPResponseError(code, message("frame_composition_invalid", detail=detail[:300]))
    try:
        raw = str(out["choices"][0]["message"]["content"]).strip()
        raw = re.sub(r"^```\w*\s*|\s*```$", "", raw).strip()
        composition = json.loads(raw)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300])) from exc
    return staging.validate_composition(composition, film, shot, known_scenes)


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
    structured = composition and composition.get("version") == 2
    if structured:
        lines.append(staging.render_opening(composition))
    elif composition:
        lines.append("场景与关键道具：" + composition["scene"])
        lines.append("主体朝向（相对镜头）：" + composition["orientation"])
    if not structured:
        lines.append("画面：" + (f"【{cam}】" if cam else "") + (composition["frame"] if composition else (shot.get("action") or "").strip()).rstrip("。")
                 + "。画的是这个镜头刚开始的一瞬间，动作还没做完。")
    for c in who:
        look = "" if structured else (c.get("look") or "").strip().rstrip("。")
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


def submit(text: str, refs: list[Path], aspect: str, *, before_submit=lambda: None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        urls = [_upload(media.shrink(Path(p), Path(tmp) / f"ref{i}.jpg")) for i, p in enumerate(refs)]
    body = {"model": config.IMAGE_MODEL, "prompt": text, "size": aspect, "resolution": config.IMAGE_RESOLUTION,
            "quality": config.IMAGE_QUALITY, "n": 1}
    if urls:
        body["image_urls"] = urls
    key = config.apimart_key()
    before_submit()
    try:
        # A rejected submission must reach the caller before any HTTP-level retry.
        out = http.request(config.APIMART_BASE + "/images/generations", key, body, tries=1)
    except RuntimeError as exc:
        if is_moderation_error(exc):
            raise ContentPolicyError(str(exc)) from exc
        if isinstance(exc, http.HTTPResponseError) and exc.status_code in NON_RETRYABLE_STATUSES:
            raise SubmissionRejected(str(exc)) from exc
        raise
    if not isinstance(out, dict):
        raise RuntimeError("APIMart gave non-object response: " + json.dumps(out, ensure_ascii=False)[:200])
    if is_moderation_error(out.get("error") or out):
        raise ContentPolicyError(json.dumps(out, ensure_ascii=False))
    data = out.get("data")
    task = (data[0].get("task_id") if isinstance(data, list) and data and isinstance(data[0], dict)
            else data.get("task_id") if isinstance(data, dict) else None)
    if not task:
        rejected = bool(out.get("error") or out.get("_http_status") in NON_RETRYABLE_STATUSES
                        or (isinstance(out.get("code"), int) and out["code"] != 0))
        error = SubmissionRejected if rejected else RuntimeError
        raise error("APIMart gave no task id: " + json.dumps(out, ensure_ascii=False)[:200])
    return task


def poll(task: str) -> dict:
    """{state: running|done|failed, url, cny, error}; APIMart reports cost in US dollars."""
    try:
        out = http.request(config.APIMART_BASE + f"/tasks/{task}", config.apimart_key())
    except http.HTTPResponseError as exc:
        if is_moderation_error(exc):
            raise ContentPolicyError(str(exc)) from exc
        if exc.status_code == 404:
            return {"state": "failed", "error": str(exc), "moderation_failed": False}
        raise
    except RuntimeError as exc:
        if is_moderation_error(exc):
            raise ContentPolicyError(str(exc)) from exc
        raise
    if not isinstance(out, dict):
        return {"state": "failed", "error": json.dumps(out, ensure_ascii=False)[:200], "moderation_failed": False}
    http_status = out.get("_http_status")
    st = out.get("data") if isinstance(out.get("data"), dict) else out
    if not st.get("status"):
        if is_moderation_error(st.get("error") or out):
            raise ContentPolicyError(json.dumps(st, ensure_ascii=False))
        if http_status == 404:
            return {"state": "failed", "error": json.dumps(out, ensure_ascii=False)[:300], "moderation_failed": False}
        if isinstance(http_status, int) and http_status >= 400:
            raise RuntimeError(f"APIMart poll HTTP {http_status}: {json.dumps(out, ensure_ascii=False)[:260]}")
        if st.get("error") or out.get("error") or (isinstance(out.get("code"), int) and out["code"] != 0):
            err_payload = st.get("error") or out.get("error") or out
            err_code = err_payload.get("code") if isinstance(err_payload, dict) else out.get("code")
            detail = json.dumps(err_payload, ensure_ascii=False) if isinstance(err_payload, dict) else str(err_payload)
            low_detail = detail.lower()
            if err_code in (404, "not_found", "task_not_found") or any(
                tok in low_detail for tok in ("not exist", "not found", "cancelled", "canceled", "expired")
            ):
                return {"state": "failed", "error": detail[:300], "moderation_failed": False}
            raise RuntimeError(f"APIMart poll transient/auth error: {detail[:300]}")
    usd = st.get("cost")
    res = {"state": "running", "cny": round(float(usd) * config.USD_CNY, 4) if usd is not None else None}
    if st.get("status") == "completed":
        images = (st.get("result") or {}).get("images") or []
        urls = (images[0].get("url") if images else None) or []
        res.update(state="done", url=urls[0] if isinstance(urls, list) and urls else urls or None)
        if not res["url"]:
            res.update(state="failed", error="completed without an image url")
    elif st.get("status") in ("failed", "cancelled", "canceled", "expired", "error"):
        err = st.get("error") or {}
        detail = json.dumps(err, ensure_ascii=False) if isinstance(err, dict) else str(err or st.get("status"))
        res.update(state="failed", error=detail, moderation_failed=is_moderation_error(err))
    return res


def draw(text: str, refs: list[Path], aspect: str, out: Path, record=lambda **k: None, task: str | None = None) -> float:
    """Draw one frame to `out`; `record(task=…)` is called with the task id before waiting, so a rerun resumes it.
    Returns the cost in CNY."""
    if not task:
        try:
            task = submit(text, refs, aspect, before_submit=lambda: record(submission_unknown=True))
        except SubmissionRejected:
            record(submission_unknown=False)
            raise
    record(task=task, task_failed=False, submission_unknown=False)
    for _ in range(MAX_POLL_ATTEMPTS):
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
    raise RuntimeError(f"frame poll timed out for task {task}")
