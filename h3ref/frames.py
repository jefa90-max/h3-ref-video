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

from . import config, http, media, rewrite, staging
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
    "杀戮": "冲突", "屠杀": "冲突", "砍杀": "对峙", "虐杀": "冲突", "暴力": "激烈冲突", "殴打": "争执推搡",
    "暴打": "争执冲突", "虐待": "严厉对待", "掐脖": "逼近对峙", "勒脖": "逼近对峙", "捅伤": "挥手逼退", "刺伤": "挥手逼退",
    "自杀": "独自沉思", "自残": "独自沉思",
    "肢解": "画面切换", "断肢": "散落的道具", "内脏": "道具", "裸露": "衣着完整", "裸体": "衣着完整",
}
NEUTRAL_PATTERN = re.compile("|".join(re.escape(word) for word in sorted(NEUTRAL_WORDS, key=len, reverse=True)))
MODERATION = re.compile(r"content[_ -]?(?:policy|filter|moderation)|moderation[_ -]?(?:blocked|failed)|"
                        r"policy[_ -]violation|safety[_ -](?:system|filter|violation)|sensitive[_ -]content|"
                        r"nsfw|内容审核|审核[未不]通过|敏感内容|内容违规|违反.{0,12}(?:政策|安全)", re.I)


class SubmissionRejected(RuntimeError):
    """The image API explicitly rejected creation of a new task."""


class CompositionFormatError(RuntimeError):
    """The composition LLM returned empty, non-JSON, or schema-invalid output across all bounded retries."""


def _extract_composition_json_text(raw_reply: str) -> str:
    cleaned = rewrite.strip_think_blocks(
        raw_reply,
        lambda: ValueError(message("frame_composition_invalid", detail=raw_reply[:300])),
    )
    fence_open = cleaned.find("```")
    if fence_open != -1:
        fence_close = cleaned.find("```", fence_open + 3)
        if fence_close == -1:
            raise ValueError(message("frame_composition_invalid", detail=cleaned[:300]))
        inner = cleaned[fence_open + 3:fence_close]
        if inner[:4].lower() == "json" and (len(inner) == 4 or inner[4].isspace()):
            inner = inner[4:]
        return inner.strip()
    return cleaned


NON_RETRYABLE_STATUSES = {400, 401, 402, 403, 404, 405, 413, 422, 429}
FATAL_COMPOSITION_STATUSES = {401, 402, 403, 404, 429}
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
                         known_scenes: dict | None = None, locked_looks: dict[str, str] | None = None,
                         max_retries: int = 3) -> dict:
    """Describe only this shot's opening image, using earlier shots for continuity."""
    shots = film.get("shots") or [shot]
    number = shots.index(shot) + 1
    visible = lambda item: {key: item.get(key, "") for key in ("camera", "action", "line")}
    context = {"shot_number": number, "current_shot": visible(shot),
               "previous_shots": [visible(item) for item in shots[:number - 1]], "key_scenes": film.get("setting") or "",
               "cast": film.get("cast") or [], "previous_composition": previous_composition,
               "known_scenes": known_scenes or {}, "locked_looks": locked_looks or {}}
    rules = (
        "你是分镜美术指导。只输出精简的 composition v3 JSON 对象，不输出 Markdown、代码块或任何多余解释。"
        "必填字段：{\"version\":3, \"camera\":\"...\", \"scene\":\"...\", \"characters\":[{\"name\":\"...\",\"locked_look\":\"...\",\"placement_and_pose\":\"...\"}]}；无人空镜填 \"characters\":[]。"
        "camera：若 current_shot.camera 已指定景别/视角则忠实展开，未指定时按当前镜头人物关系与空间补足清晰机位与构图（如多人殿堂补高角度斜俯拍广角或对称全景，单人对话补近景/过肩等，不要把单一机位写死为全片固定机位）。"
        "scene：写当前镜头的具体场景环境及前中背景核心陈设与关键道具（key_scenes 仅作参考；与 previous_composition 同场景时保持核心陈设位置一致，反打只改机位和画面左右相对关系）。"
        "characters：name 使用 cast 正式姓名，同一镜内不重复。"
        "locked_look：严格只写跨镜头固定不变的五官长相、发型胡须、固定头饰与固定服装；严禁把单镜临时道具、持物、动作、姿态或朝向（如手持某物、端杯、毛巾挂脖、背对镜头、正面站立）塞进 locked_look；若 locked_looks 已给出某角色锁死词，必须原样逐字复用。"
        "placement_and_pose：只写本镜首帧（动作刚开始那一瞬，不提前画出动作结束结果）的画面左/中/右站位、身体朝向、视线、姿态与当前持物（如“画面左侧站着，侧身面向中央长案，右手轻按婚书”）。"
        "背影镜头不要求露脸，不沿用参考图的正面立绘姿势，不引用参考图编号，不画任何字幕或文字。"
        "内容安全（必须严格遵守，避免触发图片生成审核拦截）：严禁在任何字段中输出暴力、血腥、伤残、流血、伤口、杀戮、殴打、虐待、尸体、自残、色情裸露或“死”字等敏感词与直白伤害画面；"
        "若剧本涉及打斗、冲突、受伤或威胁情节，必须改写为克制、安全的影视戏剧化画面（例如改为紧张对峙、防备架势、气场压迫、衣衫微乱或远景剪影，不描写武器刺伤/击打身体、血迹伤口或残忍细节）。"
    )
    llm = config.llm_settings()
    key = config.llm_key()
    base_messages = [{"role": "system", "content": rules},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
    retry_messages: list[dict] = []
    attempts = max(1, int(max_retries) + 1)
    last_error = ""
    for _ in range(attempts):
        out = http.request(llm["base_url"] + "/chat/completions", key,
                           {"model": llm["composition_model"], "messages": base_messages + retry_messages,
                            "max_tokens": 8000, "stream": False},
                           timeout=600, tries=1)
        if isinstance(out, dict) and (out.get("error") or out.get("_http_status", 200) >= 400) and "choices" not in out:
            err_obj = out.get("error")
            detail = json.dumps(out, ensure_ascii=False)
            low = detail.lower()
            http_status = out.get("_http_status")
            raw_code = err_obj.get("code") if isinstance(err_obj, dict) else None
            if http_status in (401, 403) or raw_code in (401, 403, "invalid_api_key", "api_key_invalid", "unauthorized", "authentication_error") or any(
                tok in low for tok in ("invalid_api_key", "api_key_invalid", "api key not valid", "incorrect api key", "unauthorized", "forbidden")
            ):
                code = 403 if http_status == 403 or raw_code == 403 else 401
            elif http_status in FATAL_COMPOSITION_STATUSES or raw_code in FATAL_COMPOSITION_STATUSES:
                code = int(http_status if http_status in FATAL_COMPOSITION_STATUSES else raw_code)
            elif raw_code in ("insufficient_quota", "billing_hard_limit_reached"):
                code = 402
            elif raw_code in ("model_not_found", "not_found"):
                code = 404
            elif raw_code in ("rate_limit_exceeded", "too_many_requests"):
                code = 429
            elif isinstance(raw_code, int):
                code = raw_code
            elif isinstance(http_status, int) and http_status >= 400:
                code = http_status
            else:
                code = 400
            raise http.HTTPResponseError(code, message("frame_composition_invalid", detail=detail[:300]))
        raw_reply = ""
        try:
            if not isinstance(out, dict):
                raise ValueError(message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300]))
            raw_content = out["choices"][0]["message"]["content"]
            if not isinstance(raw_content, str) or not raw_content.strip():
                raise ValueError(message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300]))
            raw_reply = raw_content.strip()
            cleaned = _extract_composition_json_text(raw_reply)
            composition = json.loads(cleaned)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = str(exc) if isinstance(exc, ValueError) else message("frame_composition_invalid", detail=json.dumps(out, ensure_ascii=False)[:300])
        else:
            try:
                validated = staging.validate_composition(composition, film, shot, known_scenes)
            except ValueError as exc:
                last_error = str(exc)
            else:
                if validated.get("version") == 3 and locked_looks:
                    for ch in validated.get("characters") or []:
                        if ch["name"] in locked_looks:
                            ch["locked_look"] = locked_looks[ch["name"]]
                return validated
        retry_messages.append({"role": "assistant", "content": raw_reply[:2000] if raw_reply else "(empty response)"})
        retry_messages.append({
            "role": "user",
            "content": f"上一轮输出未通过校验：{last_error}。请修正问题，严格只输出合法的 composition v3 JSON 对象，所有必填字段非空，不加代码块。",
        })
    raise CompositionFormatError(last_error)


def prompt(film: dict, shot: dict, who: list[dict], with_board: bool, composition: dict | None = None, *,
           resolve_cache: dict[str, str] | None = None) -> str:
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
    structured = staging.is_structured(composition)
    if structured:
        lines.append(staging.render_opening(composition))
    elif composition:
        lines.append("场景与关键道具：" + composition["scene"])
        lines.append("主体朝向（相对镜头）：" + composition["orientation"])
    if not structured:
        lines.append("画面：" + (f"【{cam}】" if cam else "") + (composition["frame"] if composition else (shot.get("action") or "").strip()).rstrip("。")
                 + "。画的是这个镜头刚开始的一瞬间，动作还没做完。")
    comp_looks = {}
    if structured:
        for ch in composition.get("characters") or []:
            if ch.get("name"):
                comp_looks[ch["name"]] = (ch.get("locked_look") or ch.get("appearance") or "").strip().rstrip("。；; ")
    groups = staging.group_cast_by_picture(who, film.get("cast") or who, resolve_cache=resolve_cache)
    for g in groups:
        if len(g["all"]) <= 1 and len(g["active"]) == 1 and not g["absent"]:
            c = g["active"][0]
            look = "" if structured else (c.get("look") or "").strip().rstrip("。")
            lines.append(f"参考图片{k}是{c['name']}" + (f"：{look}" if look else "") + "。长相、发型、妆容和服装严格照参考图，朝向按当前镜头描述，不沿用参考图中的正面姿态。")
        else:
            parts = []
            for c in g["active"]:
                lk = comp_looks.get(c["name"]) or (c.get("look") or "").strip().rstrip("。；; ")
                parts.append(f"{c['name']}（{lk}）" if lk else c["name"])
            absent_clause = f"该参考图中还包含未出场的{'、'.join(c['name'] for c in g['absent'])}，严禁将未出场角色画进画面。" if g["absent"] else ""
            lines.append(
                f"参考图片{k}是多角色合图，本镜出场角色为{'、'.join(parts)}。请按各自外观特征将长相、发型、妆容和服装严格对号入座，切勿角色串脸；朝向与动作按当前镜头描述，不沿用参考图中的正面姿态。"
                + absent_clause
            )
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
    if len(refs) > config.MAX_IMAGES:
        raise SubmissionRejected(message("frame_too_many_refs", count=len(refs), limit=config.MAX_IMAGES))
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
    has_data = isinstance(out.get("data"), dict)
    st = out["data"] if has_data else out
    raw_status = st.get("status")
    if isinstance(http_status, int) and http_status >= 400:
        if is_moderation_error(st.get("error") or out):
            raise ContentPolicyError(json.dumps(st, ensure_ascii=False))
        if http_status == 404:
            return {"state": "failed", "error": json.dumps(out, ensure_ascii=False)[:300], "moderation_failed": False}
        raise RuntimeError(f"APIMart poll HTTP {http_status}: {json.dumps(out, ensure_ascii=False)[:260]}")
    bad_code = isinstance(out.get("code"), int) and out["code"] not in (0, 200)
    gateway_error = not has_data and not st.get("id") and raw_status == "error"
    non_str_status = raw_status is not None and not isinstance(raw_status, str)
    if not raw_status or non_str_status or bad_code or gateway_error:
        if is_moderation_error(st.get("error") or out):
            raise ContentPolicyError(json.dumps(st, ensure_ascii=False))
        if st.get("error") or out.get("error") or bad_code or gateway_error or non_str_status:
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
