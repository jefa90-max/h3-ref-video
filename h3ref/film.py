"""A storyboard (film.json) in, a finished film out: segments of up to 15 s cut at shot boundaries, a storyboard frame
for every shot, each segment's prompt written the way you'd write it for Seedance and rewritten into H3's official
format, generated on RunningHub, checked for burned subtitles, joined. Every paid step is recorded in state.json before
it is waited on, so `run` again resumes instead of paying twice.

Frame modes (--frames):
  shot   a frame for every shot, each the first frame of its [Shot N] (default; segments are independent and run at once)
  first  one frame per segment for its first shot; later segments also get the previous segment's last frame
  none   no frames: characters and setting from words and pictures alone, segments chained on the last frame
"""
import copy
import errno
import hashlib
import json
import math
import os
import random
import re
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

from . import check, comfy, config, frames, http, media, rewrite, rh, staging
from .i18n import message
from .staging import deduplicate_paths, group_cast_by_picture

GROUP_WORDS = ("两人", "二人", "三人", "四人", "俩", "大家", "众人", "所有人", "他们", "她们", "全员", "everyone", "both")
LANG = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文", "es": "西班牙文", "pt": "葡萄牙文"}
ORIENT = {"9:16": "竖屏", "16:9": "横屏", "1:1": "方形"}
RULES = ("完整表现动作的原因、接触和结果，动作真实连续，不要静态立绘晃动；角色不互换外观，道具不凭空出现或消失，不增加额外人物。"
         "画风全片统一为上面写的风格；每个角色全片是同一位演员，长相、发型、妆容和服装前后一致；"
         "画面里不出现字幕和文字，包装和道具上不印可读的字，只用图案和颜色区分；不出现任何真实品牌的标志。")


def _is_trycloudflare_url(url: str | None) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    return parts.scheme.lower() == "https" and host.endswith(".trycloudflare.com") and len(host) > len(".trycloudflare.com")


def _relocate_trycloudflare_endpoint(job, seg_idx: int, rec: dict, endpoint_url: str | None) -> str | None:
    if not endpoint_url or not _is_trycloudflare_url(endpoint_url):
        return endpoint_url
    task_id = str(rec.get("task") or "").strip()
    if not task_id:
        return endpoint_url
    cur_urls = config.video_settings()["comfyui_urls"]
    if endpoint_url in cur_urls:
        return endpoint_url
    for cand in cur_urls:
        if cand == endpoint_url or not _is_trycloudflare_url(cand):
            continue
        try:
            comfy.assert_resume_endpoint_key_allowed(cand, expected_key_fp=rec.get("comfyui_key_fp"))
        except RuntimeError:
            continue
        if comfy.has_task(task_id, base_url=cand):
            job.log("relocate", f"seg{seg_idx + 1}", f"{comfy.endpoint_label(endpoint_url)} -> {comfy.endpoint_label(cand)}")
            job.put("segs", str(seg_idx), comfyui_url=cand)
            return cand
    return endpoint_url


def _num(x) -> str:
    x = float(x)
    return str(int(x)) if x.is_integer() else f"{x:g}"


# ------------------------------------------------------------------ the storyboard

def load(path: Path, check_images: bool = True) -> dict:
    film = json.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    if film.get("aspect", "9:16") not in config.SIZES:
        raise SystemExit(f"aspect 只支持 {', '.join(config.SIZES)}")
    film.setdefault("aspect", "9:16")
    for c in film.get("cast") or []:
        p = Path(c["picture"]).expanduser()
        c["picture"] = str((p if p.is_absolute() else (base / p)).resolve())
        if check_images and not Path(c["picture"]).is_file():
            raise SystemExit(f"找不到 {c['name']} 的参考图：{c['picture']}")
    if film.get("board"):
        p = Path(film["board"]).expanduser()
        film["board"] = str((p if p.is_absolute() else (base / p)).resolve())
    shots = [s for s in film.get("shots") or [] if (s.get("action") or "").strip()]
    if not shots:
        raise SystemExit("film.json 里没有镜头（shots）")
    limit_shots = config.max_shots()
    if len(shots) > limit_shots:
        raise SystemExit(f"镜头数不能超过 {limit_shots}（当前 {len(shots)}）")
    t = 0.0
    for s in shots:                                    # times made continuous; a shot without times lasts 2 s
        length = float(s["end"]) - float(s["start"]) if s.get("start") is not None and s.get("end") is not None else 2.0
        s["start"], s["end"] = round(t, 2), round(t + max(0.5, length), 2)
        t = s["end"]
    film["shots"] = shots
    return film


def plan(shots: list[dict], max_len: int = config.MAX_SECONDS) -> list[dict]:
    """Segments of at most `max_len` seconds, cut at shot boundaries, about the same length each."""
    total = float(shots[-1]["end"])
    n = max(1, math.ceil(total / max_len))
    target = total / n
    segs: list[list[dict]] = [[]]
    for s in shots:
        cur = segs[-1]
        if cur:
            start = float(cur[0]["start"])
            if float(s["end"]) - start > max_len or (float(s["start"]) - start >= target - 0.01 and len(segs) < n):
                segs.append([])
        segs[-1].append(s)
    out = []
    for i, group in enumerate(segs):
        a, b = float(group[0]["start"]), float(group[-1]["end"])
        out.append({"idx": i, "start": a, "end": b, "shots": group,
                    "seconds": max(config.MIN_SECONDS, min(max_len, int(math.ceil(b - a - 0.25))))})
    limit_segs = config.max_segments()
    if len(out) > limit_segs:
        raise SystemExit(f"分段数不能超过 {limit_segs}（当前 {len(out)}）")
    return out


def in_shots(c: dict, shots: list[dict]) -> bool:
    """Does this character appear in these shots? Generous on purpose: an extra picture costs little, a missing one lets
    the model draw the character from words alone."""
    text = " ".join(f"{s.get('action') or ''} {s.get('line') or ''}" for s in shots)
    if any(g in text for g in GROUP_WORDS):
        return True
    return any(n and n in text for n in [c["name"], *(c.get("aliases") or [])])


def refs(film: dict, seg: dict, mode: str, *, resolve_cache: dict[str, str] | None = None) -> list[dict]:
    """The segment's pictures in <Picture N> order: board, each character sheet in it, then the frames or the previous last frame.
    Nine at most: when they run short the board goes first, then the later shots' frames."""
    all_cast = list(film.get("cast") or [])
    active_cast = [c for c in all_cast if in_shots(c, seg["shots"])] or all_cast
    groups = group_cast_by_picture(active_cast, all_cast, resolve_cache=resolve_cache)
    board = [{"kind": "board", "path": film["board"]}] if film.get("board") else []
    people = [{"kind": "cast", "path": g["path"], "cast": {**g["active"][0], "name": g["name"]},
               "active_cast": g["active"], "absent_cast": g["absent"], "all_cast": g["all"]}
              for g in groups]
    tail = []
    if mode == "shot":
        room = config.MAX_IMAGES - len(people) - len(board)
        if room < len(seg["shots"]) and board:
            board, room = [], room + 1
        tail = [{"kind": "frame", "shot": j} for j in range(min(len(seg["shots"]), max(0, room)))]
    else:
        if mode == "first":
            tail.append({"kind": "frame", "shot": 0})
        if seg["idx"] > 0:
            tail.append({"kind": "prev"})
    out = board + people + tail
    if len(out) > config.MAX_IMAGES:
        raise SystemExit(f"第 {seg['idx'] + 1} 段要 {len(out)} 张参考图，H3 最多 {config.MAX_IMAGES} 张；减少这一段的角色或将多个角色合并为一张参考图")
    return out


def _scene_tokens(scene: str) -> set[str]:
    head = re.split(r"[，,。；;\n]", scene.strip(), maxsplit=1)[0].strip().lower()
    words = set(re.findall(r"[a-z0-9]{2,}", head))
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", head))
    bigrams = {cjk[i:i + 2] for i in range(len(cjk) - 1)}
    return words | bigrams


def _is_same_scene(prev_scene: str, cur_scene: str) -> bool:
    if not prev_scene or not cur_scene:
        return False
    if prev_scene.strip() == cur_scene.strip():
        return True
    a, b = _scene_tokens(prev_scene), _scene_tokens(cur_scene)
    if not a or not b:
        return False
    overlap = len(a & b)
    return overlap >= 2 or overlap / min(len(a), len(b)) >= 0.4


def _clean_scene_text(scene: str) -> str:
    return re.sub(r"^场景(?:为|是|：|:)\s*", "", scene.strip()).rstrip("。；; ")


def compile_prompt(film: dict, seg: dict, pics: list[dict], count: int, *, compositions: dict[str, dict] | None = None) -> str:
    """The segment's prompt in Chinese, written the way you would for Seedance; rewrite.py turns it into H3's format."""
    frame_pic_numbers = {p["shot"]: idx for idx, p in enumerate(pics, 1) if p["kind"] == "frame"}
    compositions = {key: value for key, value in (compositions or {}).items()
                    if key in {_frame_key(seg['idx'], p['shot']) for p in pics if p['kind'] == 'frame'}}
    v2_covered = {c['name'] for comp in compositions.values() if comp.get('version') == 2 for c in comp['characters']}
    changing = staging.changing_wearables(film)
    v3_looks: dict[str, str] = {}
    comp_looks: dict[str, str] = {}
    for comp in compositions.values():
        is_v3 = comp.get('version') == 3
        for ch in comp.get('characters') or []:
            if ch.get('name'):
                lk = (ch.get('locked_look') or ch.get('appearance') or '').strip().rstrip('。；; ')
                if is_v3:
                    lk = staging.stable_locked_look(lk, changing.get(ch['name'], set()))
                if lk:
                    comp_looks.setdefault(ch['name'], lk)
                    if is_v3:
                        v3_looks.setdefault(ch['name'], lk)
    aspect = film["aspect"]
    head = (f"根据下面文字分镜生成一段{seg['seconds']}秒{ORIENT[aspect]}视频，{aspect}。"
            + (f"这是一条连续短片的第{seg['idx'] + 1}段。" if count > 1 else "")
            + "没有原视频、截图或原音频，画面从这些文字和参考图来。" + (film.get("look") or "").strip())
    offset = float(seg["start"])
    lines, k = [], 0
    for p in pics:
        k += 1
        if p["kind"] == "board":
            lines.append(f"参考图片{k}是本片的角色、道具和场景参考总表，只用来统一外观，不要把总表、白底、文字标注画进画面。")
        elif p["kind"] == "cast":
            active_list = p.get("active_cast") or [p["cast"]]
            absent_list = p.get("absent_cast") or []
            all_list = p.get("all_cast") or active_list
            if len(all_list) <= 1 and len(active_list) == 1 and not absent_list:
                c = active_list[0]
                if c["name"] in v3_looks:
                    look = v3_looks[c["name"]]
                elif c["name"] in v2_covered:
                    look = ""
                else:
                    look = (c.get("look") or "").strip().rstrip("。")
                    look = staging.stable_locked_look(look, changing.get(c['name'], set()))
                lines.append(f"参考图片{k}只约束{c['name']}身份" + (f"：{look}。" if look else "。"))
            else:
                parts = []
                for c in active_list:
                    lk = comp_looks.get(c["name"]) or (c.get("look") or "").strip().rstrip("。；; ")
                    lk = staging.stable_locked_look(lk, changing.get(c['name'], set()))
                    parts.append(f"{c['name']}（{lk}）" if lk else c["name"])
                absent_clause = f"该参考图中还包含未出场的{'、'.join(c['name'] for c in absent_list)}，严禁将未出场角色画进画面。" if absent_list else ""
                lines.append(
                    f"参考图片{k}是多角色合图，本段仅约束出场角色{'、'.join(parts)}的身份与外貌，请按各自外观特征严格对号入座，切勿混淆或串脸。"
                    + absent_clause
                )
        elif p["kind"] == "frame":
            s = seg["shots"][p["shot"]]
            span = f"{_num(float(s['start']) - offset)}–{_num(float(s['end']) - offset)}秒"
            lines.append(f"参考图片{k}是第{p['shot'] + 1}个镜头（{span}）开始那一瞬的画面，也就是 [Shot {p['shot'] + 1}] 的首帧："
                         "这个镜头从这张图开始演，人物位置、服装、光线和构图照它，之后按分镜接着演。")
        else:
            first = any(x["kind"] == "frame" for x in pics)
            lines.append(f"参考图片{k}是上一段的最后一个画面：" + ("只用来保持画风、光线、场景和角色状态一致，这一段的第一个镜头从它的首帧开始。"
                                                       if first else "这一段从这个画面直接接着演，画风、光线、场景和角色状态保持一致。"))
    n = sum(len(p.get("active_cast") or [p["cast"]]) for p in pics if p["kind"] == "cast")
    if n:
        lines.append((f"只有这{n}名角色" if n > 1 else "只有这一名角色") + "，保持各自发型、五官、服饰纹样与比例。")
    out = [head, "\n".join(lines)]
    first_v3_scene = next(
        (_clean_scene_text(compositions[_frame_key(seg["idx"], j)]["scene"])
         for j in range(len(seg["shots"]))
         if (compositions.get(_frame_key(seg["idx"], j)) or {}).get("version") == 3
         and compositions[_frame_key(seg["idx"], j)].get("scene")),
        "",
    )
    if not compositions and (film.get("setting") or "").strip():
        out.append("场景与关键道具（全片统一）：" + film["setting"].strip())
    elif first_v3_scene:
        out.append(f"场景基调：{first_v3_scene}。")
    speech = film.get("speech") or ""
    shot_lines = []
    last_v3_scene = first_v3_scene
    for j, s in enumerate(seg["shots"]):
        composition = compositions.get(_frame_key(seg["idx"], j))
        cam = (s.get("camera") or "").strip().strip("【】")
        frame_num = frame_pic_numbers.get(j)
        frame_anchor = (f"从参考图片{frame_num}开始。" if j == 0 else f"切至参考图片{frame_num}。") if frame_num else ""
        if composition and composition.get("version") == 3:
            comp_cam = composition["camera"].strip().strip("【】").rstrip("。；; ")
            cam_label = cam or comp_cam
            cur_scene = _clean_scene_text(composition.get("scene") or "")
            scene_cut = ""
            if cur_scene and last_v3_scene and not _is_same_scene(last_v3_scene, cur_scene):
                scene_cut = f"场景切换为：{cur_scene}。"
            if cur_scene:
                last_v3_scene = cur_scene
            act = (s.get("action") or "").strip().rstrip("。；; ")
            text = (f"{_num(float(s['start']) - offset)}–{_num(float(s['end']) - offset)}秒："
                    + (f"【{cam_label}】" if cam_label else "")
                    + frame_anchor + scene_cut + f"动作过程：{act}。")
        else:
            text = (f"{_num(float(s['start']) - offset)}–{_num(float(s['end']) - offset)}秒："
                    + (f"【{cam}】" if cam else "")
                    + (staging.render_motion(composition) if composition
                       else frame_anchor + (s.get("action") or "").strip().rstrip("。"))
                    + "。")
        if compositions and not composition and (film.get("setting") or "").strip():
            text += "场景参考：" + film["setting"].strip()
        if speech and (s.get("line") or "").strip():
            text += "台词：" + s["line"].strip()
        shot_lines.append(text)
    out.append("分镜：\n" + "\n".join(shot_lines))
    out.append(RULES)
    if speech and any((s.get("line") or "").strip() for s in seg["shots"]):
        out.append(f"声音：角色只说上面写出的{LANG.get(speech, speech)}台词，口型对上；其余只有动作音效和环境声；不要背景音乐、旁白、唱歌、字幕和水印。")
    else:
        out.append("声音：这一段没有台词，角色不说话；只有动作音效和环境声；不要背景音乐、旁白、唱歌、字幕和水印。情绪只用表情和肢体表现，无说话口型。")
    return "\n".join(out)


# ------------------------------------------------------------------ the run

class Job:
    def __init__(self, path: Path):
        self.dir = path.resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "state.json"
        self.state = json.loads(self.file.read_text(encoding="utf-8")) if self.file.exists() else {"frames": {}, "segs": {}}
        self.lock = threading.RLock()

    def save(self):
        with self.lock:
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
            config.atomic_replace(tmp, self.file, sleep=time.sleep)

    def put(self, kind: str, key: str, **rec):
        with self.lock:
            self.state[kind][key] = {**self.state[kind].get(key, {}), **rec}
            self.save()

    def log(self, *parts):
        line = time.strftime("%H:%M:%S") + " " + " ".join(str(p) for p in parts)
        print(line, flush=True)
        with (self.dir / "run.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _frame_key(i: int, j: int) -> str:
    return f"s{i + 1}-{j + 1}"


def frame_reference_paths(film: dict, shot: dict, record: dict, *, resolve_cache: dict[str, str] | None = None) -> list[str]:
    if "character_reference_paths" in record:
        characters = deduplicate_paths(record["character_reference_paths"], resolve_cache=resolve_cache)
    else:
        active = [c for c in film.get("cast") or [] if in_shots(c, [shot])]
        characters = [g["path"] for g in group_cast_by_picture(active, film.get("cast") or [], resolve_cache=resolve_cache)]
    return ([film["board"]] if film.get("board") else []) + characters


def reference_version(paths, *, metadata=None) -> str:
    sources = []
    metadata = {} if metadata is None else metadata
    for source in paths:
        key = str(source)
        if key not in metadata:
            path = Path(source)
            try:
                st = path.stat()
            except OSError:
                st = None
            regular = st if st is not None and stat.S_ISREG(st.st_mode) else None
            metadata[key] = (str(path.resolve()), (regular.st_mtime_ns, regular.st_size) if regular else None)
        sources.append(metadata[key])
    return hashlib.sha256(json.dumps(sources, ensure_ascii=False).encode()).hexdigest()


def frame_reference_versions(film: dict, segs: list[dict], records: dict, *, resolve_cache: dict[str, str] | None = None) -> dict[str, str]:
    metadata = {}
    cache = {} if resolve_cache is None else resolve_cache
    return {_frame_key(s['idx'], j): reference_version(frame_reference_paths(film, shot, records.get(_frame_key(s['idx'], j), {}), resolve_cache=cache), metadata=metadata)
            for s in segs for j, shot in enumerate(s['shots'])}


def saved_compositions(records: dict, *, reference_versions: dict[str, str]) -> dict[str, dict]:
    return {key: rec['composition'] for key, rec in records.items()
            if staging.is_structured(rec.get('composition'))
            and isinstance(rec.get('prompt'), str) and rec['prompt'].strip()
            and rec.get('composition_prompt') == rec['prompt']
            and rec.get('composition_reference_version')
            and rec['composition_reference_version'] == reference_versions.get(key)}


def stage_frames(job: Job, film: dict, segs: list[dict], pics: dict, *, resume_tasks: bool = False,
                 optimize_storyboard: bool = True):
    """Plan in shot order, save before dispatch, and collect every independent drawing."""
    (job.dir / "frames").mkdir(exist_ok=True)
    wanted = [(i, p['shot']) for i, seg in enumerate(segs) for p in pics[i] if p['kind'] == 'frame']
    versions = frame_reference_versions(film, segs, job.state['frames'])
    effective = saved_compositions(job.state['frames'], reference_versions=versions)
    changing = staging.changing_wearables(film)
    known_scenes, locked_looks, previous, previous_key = {}, {}, None, None

    def failure(key, exc):
        reason = f"{type(exc).__name__}: {exc}"
        job.put('frames', key, error=reason, moderation_error=exc.reason if isinstance(exc, frames.ContentPolicyError) else None)
        job.log('frame failed', key, reason)

    def draw(key, text, references, task):
        out = job.dir / 'frames' / f'{key}.png'
        try:
            cny = frames.draw(text, references, film['aspect'], out, record=lambda **k: job.put('frames', key, **k), task=task)
            if not out.is_file():
                raise RuntimeError(message('frame_output_missing', path=out))
            job.put('frames', key, file=str(out), cny=cny, error=None, moderation_error=None)
            job.log('frame', key, f'¥{cny:.3f}')
        except Exception as exc:
            failure(key, exc)

    futures = []
    with ThreadPoolExecutor(4) as pool:
        for i, segment in enumerate(segs):
            for j, shot in enumerate(segment['shots']):
                key = _frame_key(i, j)
                rec = job.state['frames'].get(key) or {}
                saved = effective.get(key)
                composition = staging.scope_wearable_look(saved, film, shot, changing)
                out = job.dir / 'frames' / f'{key}.png'
                skip = ((i, j) not in wanted or (out.exists() and not frames.task_pending(rec))
                        or ((rec.get('error') or rec.get('moderation_error')) and not (resume_tasks and frames.task_pending(rec))))
                if not skip:
                    who = [c for c in film.get('cast') or [] if in_shots(c, [shot])]
                    references = frame_reference_paths(film, shot, rec)
                    try:
                        if rec.get('submission_unknown') and not rec.get('task'):
                            raise RuntimeError(message('frame_submission_unknown', path=job.file))
                        if rec.get('task'):
                            text = rec.get('prompt') or frames.prompt(film, shot, who, bool(film.get('board')), rec.get('composition'))
                            references = rec.get('reference_paths', references)
                        elif rec.get('prompt') or rec.get('composition'):
                            references = rec.get('reference_paths', references)
                            if composition != saved:
                                text = frames.prompt(film, shot, who, bool(film.get('board')), composition)
                                job.put('frames', key, composition=composition, prompt=text, composition_prompt=text)
                            else:
                                text = rec.get('prompt') or frames.prompt(film, shot, who, bool(film.get('board')), rec.get('composition'))
                            job.log('staging cache', key, 'source', previous_key)
                        elif not optimize_storyboard:
                            composition = None
                            text = frames.prompt(film, shot, who, bool(film.get('board')), None)
                            job.put('frames', key, composition=None, prompt=text, reference_paths=references)
                        else:
                            job.log('staging plan', key, 'source', previous_key)
                            try:
                                planned = frames.describe_composition(
                                    film, shot,
                                    previous_composition=copy.deepcopy(previous),
                                    known_scenes=copy.deepcopy(known_scenes),
                                    locked_looks=dict(locked_looks),
                                )
                                composition = staging.validate_composition(planned, film, shot, known_scenes)
                                composition = staging.scope_wearable_look(composition, film, shot, changing)
                                if composition.get('version') == 3:
                                    for ch in composition.get('characters') or []:
                                        if ch['name'] in locked_looks:
                                            ch['locked_look'] = locked_looks[ch['name']]
                                        else:
                                            locked_looks[ch['name']] = ch['locked_look']
                                text = frames.prompt(film, shot, who, bool(film.get('board')), composition)
                                job.put('frames', key, composition=composition, prompt=text, composition_prompt=text,
                                        composition_reference_version=versions[key], reference_paths=references)
                            except frames.CompositionFormatError as exc:
                                composition = None
                                job.log('staging fallback', key, message('frame_composition_fallback', frame_key=key, detail=str(exc)))
                                text = frames.prompt(film, shot, who, bool(film.get('board')), None)
                                job.put('frames', key, composition=None, prompt=text, reference_paths=references)
                        futures.append(pool.submit(draw, key, text, references, rec.get('task')))
                    except Exception as exc:
                        if isinstance(exc, http.HTTPResponseError) and exc.status_code in frames.FATAL_COMPOSITION_STATUSES and not rec.get('task') and not rec.get('submission_unknown'):
                            for future in futures:
                                future.result()
                            raise
                        if isinstance(exc, http.TransportError) and not rec.get('task') and not rec.get('submission_unknown'):
                            for future in futures:
                                future.result()
                            try:
                                model_name = config.llm_settings()['composition_model']
                            except ValueError:
                                model_name = 'invalid-provider'
                            err = http.TransportError(message('staging_plan_network_failed', frame_key=key,
                                                              model=model_name, detail=str(exc)))
                            job.log('staging abort', key, str(err))
                            raise err from exc
                        try:
                            model_name = config.llm_settings()['composition_model']
                        except ValueError:
                            model_name = 'invalid-provider'
                        is_policy = isinstance(exc, frames.ContentPolicyError)
                        contextual = (exc if is_policy or rec.get('task') or rec.get('submission_unknown')
                                      else RuntimeError(message('staging_plan_failed', frame_key=key, model=model_name,
                                                                path=job.file, detail=str(exc))))
                        extra = {} if rec.get('prompt') else {'prompt': frames.prompt(film, shot, who, bool(film.get('board')))}
                        if is_policy:
                            job.put('frames', key, **extra, error=str(contextual), moderation_error=exc.reason)
                        else:
                            job.put('frames', key, **extra, error=f"{type(contextual).__name__}: {contextual}")
                        job.log('frame failed', key, f"{type(contextual).__name__}: {contextual}")
                        composition = None
                if composition:
                    if composition.get('version') == 2:
                        known_scenes[composition['scene_id']] = {k: copy.deepcopy(composition[k]) for k in ('world_frame', 'fixed_objects')}
                    elif composition.get('version') == 3:
                        for ch in composition.get('characters') or []:
                            if ch.get('name') and ch.get('locked_look'):
                                locked_looks.setdefault(ch['name'], ch['locked_look'])
                    previous, previous_key = composition, key
        for future in futures:
            future.result()
    failures = [_frame_key(i, j) for i, j in wanted
                if job.state['frames'].get(_frame_key(i,j), {}).get('error')
                or job.state['frames'].get(_frame_key(i,j), {}).get('moderation_error')
                or not (job.dir / 'frames' / f'{_frame_key(i,j)}.png').is_file()]
    job.state['frame_failures'] = failures
    job.save()
    return failures


def segment_prompt_text(record: dict, automatic: str) -> str:
    override = record.get('prompt_override') or {}
    return override.get('text', automatic) if override.get('format') == 'source' else automatic


def frozen_segment(record: dict) -> bool:
    return bool(record.get('task') or record.get('submission_unknown') or record.get('terminal_failed')
                or record.get('attempt_blocked')
                or (record.get('file') and record.get('cny') is not None))


def stage_prompts(job: Job, film: dict, segs: list[dict], pics: dict):
    (job.dir / 'prompts').mkdir(exist_ok=True)
    compositions = saved_compositions(job.state['frames'], reference_versions=frame_reference_versions(film, segs, job.state['frames']))

    def one(seg):
        i = seg['idx']
        rec = job.state['segs'].get(str(i)) or {}
        if frozen_segment(rec):
            return
        if (rec.get('prompt_override') or {}).get('format') == 'h3':
            issues = rewrite.problems(rec.get('h3') or '', len(pics[i]))
            if issues:
                detail = '; '.join(issues)
                job.put('segs', str(i), error=f"RewriteError: {detail}")
                raise rewrite.RewriteError(detail)
            (job.dir / 'prompts' / f'seg{i + 1}.zh.txt').write_text(rec.get('zh') or '', encoding='utf-8')
            (job.dir / 'prompts' / f'seg{i + 1}.h3.txt').write_text(rec.get('h3') or '', encoding='utf-8')
            return
        zh = segment_prompt_text(rec, compile_prompt(film, seg, pics[i], len(segs), compositions=compositions))
        paths = _picture_paths(job, seg, pics[i])
        reference = reference_version(paths) if all(path.is_file() for path in paths) else None
        if (rec.get('h3') and rec.get('zh') == zh and rec.get('h3_source', rec.get('zh')) == zh
                and (not rec.get('h3_reference_version') or rec['h3_reference_version'] == reference)
                and ('subject_definitions:' not in rec['h3'] or not rewrite.cached_problems(rec['h3'], len(pics[i])))):
            (job.dir / 'prompts' / f'seg{i + 1}.zh.txt').write_text(zh, encoding='utf-8')
            (job.dir / 'prompts' / f'seg{i + 1}.h3.txt').write_text(rec['h3'], encoding='utf-8')
            if 'unapplied_edit' in rec:
                with job.lock:
                    job.state['segs'][str(i)].pop('unapplied_edit', None)
                    job.save()
            return
        try:
            h3 = rewrite.rewrite(zh, len(pics[i]), seg['seconds'], film['aspect'])
        except Exception as exc:
            (job.dir / 'prompts' / f'seg{i + 1}.zh.txt').write_text(zh, encoding='utf-8')
            (job.dir / 'prompts' / f'seg{i + 1}.h3.txt').unlink(missing_ok=True)
            job.put('segs', str(i), zh=zh, h3=None, h3_source=None, error=f"{type(exc).__name__}: {exc}")
            raise
        (job.dir / 'prompts' / f'seg{i + 1}.zh.txt').write_text(zh, encoding='utf-8')
        (job.dir / 'prompts' / f'seg{i + 1}.h3.txt').write_text(h3, encoding='utf-8')
        with job.lock:
            cur = job.state['segs'].setdefault(str(i), {})
            cur.pop('unapplied_edit', None)
            cur.update(zh=zh, h3=h3, h3_source=zh, h3_reference_version=reference, error=None)
            job.save()
        job.log('prompt', f'seg{i + 1}', f'{len(pics[i])} pictures')
    abort = threading.Event()
    def run_one(seg):
        if abort.is_set():
            return
        try:
            one(seg)
        except BaseException:
            abort.set()
            raise
    pool = ThreadPoolExecutor(3)
    try:
        futures = [pool.submit(run_one, seg) for seg in segs]
        for future in futures:
            future.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def picture_paths(output_dir: Path, seg: dict, pics: list[dict]) -> list[Path]:
    out = []
    for p in pics:
        if p["kind"] in ("board", "cast"):
            out.append(Path(p["path"]))
        elif p["kind"] == "frame":
            out.append(output_dir / "frames" / f"{_frame_key(seg['idx'], p['shot'])}.png")
        else:
            out.append(output_dir / "clips" / f"seg{seg['idx']}-last.jpg")
    return out


def _picture_paths(job: Job, seg: dict, pics: list[dict]) -> list[Path]:
    return picture_paths(job.dir, seg, pics)


def gen_segment(
    job: Job,
    film: dict,
    seg: dict,
    pics: list[dict],
    *,
    reference_paths: list[Path] | None = None,
    node_pool: comfy.ComfyNodePool | None = None,
    abort: threading.Event | None = None,
) -> dict:
    try:
        return _generate_segment(job, film, seg, pics, reference_paths=reference_paths, node_pool=node_pool, abort=abort)
    except comfy.DispatchAbortedError:
        raise
    except (Exception, SystemExit) as exc:
        rec = job.state['segs'].get(str(seg['idx']), {})
        if not rec.get('terminal_failed'):
            job.put('segs', str(seg['idx']), error=message('segment_generation_failed', number=seg['idx'] + 1,
                    phase=rec.get('phase', 'prepare'), error_type=type(exc).__name__), error_code='SEGMENT_GENERATION_FAILED')
        raise


def _video_provider_name(rec: dict | None = None) -> str:
    if rec and (rec.get("task") or rec.get("submission_unknown")):
        return str(rec.get("provider") or "runninghub").strip().lower()
    return config.video_settings()["provider"]


def _comfy_endpoint_kwargs(endpoint_url: str | None) -> dict:
    """Pass base_url explicitly when routing across multiple nodes or to a non-default saved endpoint."""
    if not endpoint_url:
        return {}
    vcfg = config.video_settings()
    urls = vcfg["comfyui_urls"]
    if len(urls) > 1 or (urls and endpoint_url != urls[0]) or not urls:
        return {"base_url": endpoint_url}
    return {}


def _select_single_comfy_endpoint(log=None) -> str | None:
    vcfg = config.video_settings()
    urls = vcfg["comfyui_urls"]
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]
    pool = comfy.ComfyNodePool(urls, api_key=vcfg["comfyui_api_key"], log=log)
    pool.probe_all(fail_if_all_offline=True)
    return pool.healthy_urls()[0]


def _is_auth_rejection(exc: Exception) -> bool:
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, http.HTTPResponseError) and cause.status_code in (401, 403):
        return True
    text = str(exc)
    return any(
        tok in text
        for tok in ("HTTP 401", "HTTP 403", "Unauthorized", "Forbidden", '"_http_status": 401', '"_http_status": 403')
    )


def _terminal_failed_message(number: int, path: Path, error: object = None) -> str:
    reason = str(error or "").strip()
    if reason:
        return message("segment_terminal_failed_with_reason", number=number, reason=reason, path=path)
    return message("segment_terminal_failed", number=number, path=path)


def _generate_segment(
    job: Job,
    film: dict,
    seg: dict,
    pics: list[dict],
    *,
    reference_paths: list[Path] | None = None,
    node_pool: comfy.ComfyNodePool | None = None,
    abort: threading.Event | None = None,
) -> dict:
    i = seg["idx"]
    out = job.dir / "clips" / f"seg{i + 1}.mp4"
    rec = job.state["segs"].get(str(i)) or {}
    if out.exists() and rec.get("cny") is not None:
        return rec
    if rec.get("terminal_failed"):
        raise RuntimeError(_terminal_failed_message(i + 1, job.file, rec.get("error")))
    provider_name = _video_provider_name(rec)
    backend = comfy if provider_name == "comfyui" else rh
    extra_provider = {"provider": provider_name}
    leased_endpoint: str | None = None
    reserved_inflight_endpoint: str | None = None
    endpoint_url: str | None = str(rec.get("comfyui_url") or "").strip() or None
    multi_node = bool(node_pool is not None and len(node_pool.urls) > 1)
    try:
        if not rec.get("task"):
            if rec.get("submission_unknown"):
                unknown_key = "comfy_submission_unknown" if provider_name == "comfyui" else "submission_unknown"
                raise RuntimeError(message(unknown_key, number=i + 1, path=str(job.file)))
            paths = _picture_paths(job, seg, pics) if reference_paths is None else reference_paths
            missing = [str(p) for p in paths if not p.exists()]
            if missing:
                raise SystemExit(f"第 {i + 1} 段缺参考图：{missing}")
            seed = int(rec["seed"] if rec.get("seed") is not None else random.randrange(10 ** 14, 10 ** 15))
            if provider_name == "comfyui" and node_pool is not None:
                tried: set[str] = set()
                while True:
                    cand = node_pool.acquire(exclude=tried, abort=abort)
                    tried.add(cand)
                    leased_endpoint = cand
                    endpoint_url = cand
                    ep_kw = _comfy_endpoint_kwargs(endpoint_url)
                    job.put("segs", str(i), phase="upload", h3_reference_version=reference_version(paths))
                    try:
                        names = comfy.upload_many(paths, **ep_kw)
                    except Exception as exc:
                        node_pool.mark_unhealthy(cand, f"{type(exc).__name__}: {str(exc)[:120]}")
                        leased_endpoint = None
                        if node_pool.healthy_count() > 0 and any(u not in tried for u in node_pool.healthy_urls()):
                            job.log("pool", message("comfy_node_failover", number=i + 1, node=comfy.endpoint_label(cand)))
                            continue
                        raise
                    g = comfy.graph(rec["h3"], names, seg["seconds"], film["aspect"], seed)
                    job.put("segs", str(i), phase="submit")
                    key_fp = comfy.key_fingerprint(comfy.key_for_endpoint(endpoint_url, config.comfy_key()))
                    extra_with_ep = {
                        **extra_provider,
                        "comfyui_url": endpoint_url,
                        **({"comfyui_key_fp": key_fp} if key_fp else {}),
                    }
                    try:
                        task = comfy.submit_with_retry(
                            g,
                            log=lambda detail: job.log(f"seg{i + 1}", detail),
                            record_submission=lambda unknown, _ep=extra_with_ep: job.put(
                                "segs", str(i), submission_unknown=unknown, seed=seed, **_ep
                            ),
                            **ep_kw,
                        )
                    except Exception as exc:
                        can_failover = (
                            (isinstance(exc, comfy.SubmissionRejected) and _is_auth_rejection(exc))
                            or comfy._is_tunnel_offline_error(exc)
                        )
                        if can_failover or not isinstance(exc, comfy.SubmissionRejected):
                            node_pool.mark_unhealthy(cand, f"{type(exc).__name__}: {str(exc)[:120]}")
                            leased_endpoint = None
                        if can_failover and node_pool.healthy_count() > 0 and any(u not in tried for u in node_pool.healthy_urls()):
                            job.log("pool", message("comfy_node_failover", number=i + 1, node=comfy.endpoint_label(cand)))
                            continue
                        raise
                    job.put(
                        "segs", str(i), task=task, seed=seed, submitted=time.time(),
                        submission_unknown=False, **extra_with_ep,
                    )
                    job.log(
                        "submitted", f"seg{i + 1}", f"node {comfy.endpoint_label(endpoint_url)}",
                        f"seed {seed}", f"≈¥{backend.estimate_cny(seg['seconds'])}",
                    )
                    rec = job.state["segs"][str(i)]
                    break
            else:
                if provider_name == "comfyui":
                    endpoint_url = _select_single_comfy_endpoint(log=lambda detail: job.log("pool", detail))
                ep_kw = _comfy_endpoint_kwargs(endpoint_url) if provider_name == "comfyui" else {}
                key_fp = (
                    comfy.key_fingerprint(comfy.key_for_endpoint(endpoint_url, config.comfy_key()))
                    if provider_name == "comfyui" and endpoint_url
                    else ""
                )
                extra_with_ep = {
                    **extra_provider,
                    **({"comfyui_url": endpoint_url} if provider_name == "comfyui" and endpoint_url else {}),
                    **({"comfyui_key_fp": key_fp} if key_fp else {}),
                }
                job.put("segs", str(i), phase="upload", h3_reference_version=reference_version(paths))
                if provider_name == "comfyui":
                    names = comfy.upload_many(paths, **ep_kw)
                else:
                    names = backend.upload_many(paths) if hasattr(backend, "upload_many") else [backend.upload(p) for p in paths]
                g = backend.graph(rec["h3"], names, seg["seconds"], film["aspect"], seed)
                job.put("segs", str(i), phase="submit")
                task = backend.submit_with_retry(
                    g,
                    log=lambda detail: job.log(f"seg{i + 1}", detail),
                    record_submission=lambda unknown: job.put("segs", str(i), submission_unknown=unknown, seed=seed, **extra_with_ep),
                    **ep_kw,
                )
                job.put("segs", str(i), task=task, seed=seed, submitted=time.time(), submission_unknown=False, **extra_with_ep)
                job.log("submitted", f"seg{i + 1}", f"seed {seed}", f"≈¥{backend.estimate_cny(seg['seconds'])}")
                rec = job.state["segs"][str(i)]
        elif provider_name == "comfyui":
            endpoint_url = str(rec.get("comfyui_url") or "").strip() or (config.video_settings()["comfyui_url"] or None)
            endpoint_url = _relocate_trycloudflare_endpoint(job, i, rec, endpoint_url)
            comfy.assert_resume_endpoint_key_allowed(endpoint_url, expected_key_fp=rec.get("comfyui_key_fp"))
            if node_pool is not None and endpoint_url:
                node_pool.reserve_inflight(endpoint_url)
                reserved_inflight_endpoint = endpoint_url

        provider_name = _video_provider_name(rec)
        backend = comfy if provider_name == "comfyui" else rh
        if provider_name == "comfyui" and not endpoint_url:
            endpoint_url = str(rec.get("comfyui_url") or "").strip() or (config.video_settings()["comfyui_url"] or None)
        ep_kw = _comfy_endpoint_kwargs(endpoint_url) if provider_name == "comfyui" else {}
        extra_provider = {"provider": provider_name, **({"comfyui_url": endpoint_url} if provider_name == "comfyui" and endpoint_url else {})}
        job.put("segs", str(i), phase="poll")
        poll_interval = 5 if provider_name == "comfyui" else 20
        poll_limit = 1440 if provider_name == "comfyui" else 360
        started_at = float(rec.get("submitted") or time.time())
        last_progress_sig = None
        last_progress_log = 0.0
        last_progress_save = 0.0
        for _ in range(poll_limit):
            try:
                st = backend.poll(rec["task"], **ep_kw) if provider_name == "comfyui" else backend.poll(rec["task"])
            except Exception as exc:
                if provider_name == "comfyui" and node_pool is not None and endpoint_url:
                    node_pool.mark_unhealthy(endpoint_url, f"{type(exc).__name__}: {str(exc)[:120]}")
                    if leased_endpoint == endpoint_url:
                        leased_endpoint = None
                raise
            if st["state"] != "running":
                break
            if provider_name == "comfyui":
                now = time.time()
                elapsed = max(0.0, now - started_at)
                live_info = st if "stage" in st else comfy.live_progress(rec["task"], queued=bool(st.get("queued")), **ep_kw)
                prog = comfy.progress_payload(live_info, elapsed_s=elapsed, endpoint=endpoint_url if multi_node else None)
                sig = (prog["stage"], prog["step"], prog["max_steps"], prog["queue_position"])
                if sig != last_progress_sig or (now - last_progress_save) >= 15.0:
                    job.put("segs", str(i), live_progress=prog)
                    last_progress_save = now
                if sig != last_progress_sig or (now - last_progress_log) >= 20.0:
                    job.log(
                        "progress", f"seg{i + 1}",
                        comfy.format_progress(live_info, elapsed_s=elapsed, include_node=multi_node),
                    )
                    last_progress_sig = sig
                    last_progress_log = now
            time.sleep(poll_interval)
        else:
            label = "ComfyUI" if provider_name == "comfyui" else "RunningHub"
            raise RuntimeError(f"{label} poll timed out for seg{i + 1} task {rec['task']}")
        with job.lock:
            cur_seg = job.state["segs"].setdefault(str(i), {})
            if "live_progress" in cur_seg:
                cur_seg.pop("live_progress", None)
                job.save()
        if st["state"] == "failed":
            err = st.get("error")
            if provider_name == "comfyui" and st.get("prompt_lost") and node_pool is not None and endpoint_url:
                node_pool.mark_unhealthy(endpoint_url, str(err or "prompt lost")[:120])
                if leased_endpoint == endpoint_url:
                    leased_endpoint = None
            job.put("segs", str(i), terminal_failed=True, error=err, **extra_provider)
            if err:
                job.log("failed", f"seg{i + 1}", str(err))
            raise RuntimeError(_terminal_failed_message(i + 1, job.file, err))
        job.put("segs", str(i), phase="download")
        if provider_name == "comfyui":
            comfy.download(st["url"], out)
        else:
            http.download(st["url"], out)
        job.put("segs", str(i), phase="tail")
        media.last_frame(out, job.dir / "clips" / f"seg{i + 1}-last.jpg")
        with job.lock:
            cur = job.state["segs"].setdefault(str(i), {})
            cur.pop("clip_prompts", None)
            cur.pop("manual_rewrite", None)
            cur.update(file=str(out), cny=st["cny"], coins=st.get("coins"), run_s=st.get("run_seconds"),
                       wall_s=round(time.time() - float(rec.get("submitted") or time.time())), error=None, **extra_provider)
            job.save()
        job.log("done", f"seg{i + 1}", f"¥{st['cny']:.3f}", f"{st.get('run_seconds')} s on the GPU")
        return job.state["segs"][str(i)]
    finally:
        if node_pool is not None:
            if leased_endpoint is not None:
                node_pool.release(leased_endpoint)
            if reserved_inflight_endpoint is not None:
                node_pool.release_inflight(reserved_inflight_endpoint)


def _has_inflight_task(job: Job, seg: dict) -> bool:
    rec = job.state["segs"].get(str(seg["idx"])) or {}
    clip = job.dir / "clips" / f"seg{seg['idx'] + 1}.mp4"
    return bool(rec.get("task") and not rec.get("terminal_failed") and not (clip.exists() and rec.get("cny") is not None))


def preexisting_segment_blocker(job: Job, segs: list[dict]) -> dict | None:
    for seg in segs:
        rec = job.state["segs"].get(str(seg["idx"])) or {}
        clip = job.dir / "clips" / f"seg{seg['idx'] + 1}.mp4"
        if clip.exists() and rec.get("cny") is not None:
            continue
        if rec.get("terminal_failed") or (rec.get("submission_unknown") and not rec.get("task")) or rec.get("attempt_blocked"):
            return seg
    return None


def stage_generate(job: Job, film: dict, segs: list[dict], pics: dict, mode: str):
    (job.dir / "clips").mkdir(exist_ok=True)
    chained = any(p["kind"] == "prev" for ps in pics.values() for p in ps)
    blocker = preexisting_segment_blocker(job, segs)
    if blocker is not None:
        inflight_segs = [s for s in segs if _has_inflight_task(job, s)]
        first_error = None
        if chained:
            for s in inflight_segs:
                try:
                    gen_segment(job, film, s, pics[s["idx"]])
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
        elif inflight_segs:
            pool = ThreadPoolExecutor(max(1, config.video_settings()["concurrency"]))
            try:
                futures = [pool.submit(gen_segment, job, film, s, pics[s["idx"]]) for s in inflight_segs]
                for future in futures:
                    try:
                        future.result()
                    except BaseException as exc:
                        if first_error is None:
                            first_error = exc
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        if first_error is not None:
            raise first_error
        blocker_rec = job.state["segs"].get(str(blocker["idx"])) or {}
        if blocker_rec.get("attempt_blocked") and not blocker_rec.get("terminal_failed"):
            raise RuntimeError(blocker_rec.get("error") or message(
                "segment_generation_failed", number=blocker["idx"] + 1, phase="prepare", error_type="RuntimeError"))
        gen_segment(job, film, blocker, pics[blocker["idx"]])
        return
    if chained:                                         # each segment starts from the one before
        first_error = None
        for seg in segs:
            if first_error is not None and not _has_inflight_task(job, seg):
                continue
            try:
                gen_segment(job, film, seg, pics[seg["idx"]])
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error
    else:
        inflight_segs = [s for s in segs if _has_inflight_task(job, s)]
        other_segs = [s for s in segs if not _has_inflight_task(job, s)]
        vcfg = config.video_settings()
        node_pool: comfy.ComfyNodePool | None = None
        if vcfg["provider"] == "comfyui" and len(vcfg["comfyui_urls"]) > 1:
            pending_new = [
                s for s in other_segs
                if not ((job.dir / "clips" / f"seg{s['idx'] + 1}.mp4").exists()
                        and (job.state["segs"].get(str(s["idx"])) or {}).get("cny") is not None)
            ]
            if pending_new or inflight_segs:
                node_pool = comfy.ComfyNodePool(
                    vcfg["comfyui_urls"],
                    api_key=vcfg["comfyui_api_key"],
                    log=lambda detail: job.log("pool", detail),
                )
                if pending_new:
                    node_pool.probe_all(fail_if_all_offline=not inflight_segs)
        abort = threading.Event()
        configured_concurrency = max(1, vcfg["concurrency"])
        new_seg_sem = threading.Semaphore(configured_concurrency)

        def run_inflight(seg):
            try:
                if node_pool is not None:
                    return gen_segment(job, film, seg, pics[seg["idx"]], node_pool=node_pool)
                return gen_segment(job, film, seg, pics[seg["idx"]])
            except BaseException:
                abort.set()
                if node_pool is not None:
                    node_pool.notify_all()
                raise

        def run_other(seg):
            if abort.is_set():
                return
            with new_seg_sem:
                if abort.is_set():
                    return
                try:
                    if node_pool is not None:
                        gen_segment(job, film, seg, pics[seg["idx"]], node_pool=node_pool, abort=abort)
                    else:
                        gen_segment(job, film, seg, pics[seg["idx"]])
                except comfy.DispatchAbortedError:
                    return
                except BaseException:
                    abort.set()
                    if node_pool is not None:
                        node_pool.notify_all()
                    raise

        effective_workers = configured_concurrency
        if node_pool is not None:
            effective_workers = max(
                len(inflight_segs),
                min(effective_workers, max(1, node_pool.healthy_count() or len(node_pool.urls))),
            )
        pool = ThreadPoolExecutor(effective_workers)
        first_error = None
        try:
            # Pre-reserve ComfyUI inflight endpoints before dispatching new work so new segments never crowd inflight nodes.
            inflight_pairs: list[tuple[dict, str | None]] = []
            unsubmitted_reserved: list[str] = []
            for s in inflight_segs:
                res_ep: str | None = None
                if node_pool is not None:
                    rec_s = job.state["segs"].get(str(s["idx"])) or {}
                    if _video_provider_name(rec_s) == "comfyui":
                        ep_s = str(rec_s.get("comfyui_url") or "").strip() or (vcfg["comfyui_url"] or "")
                        ep_s = _relocate_trycloudflare_endpoint(job, s["idx"], rec_s, ep_s) or ""
                        if ep_s:
                            node_pool.reserve_inflight(ep_s)
                            res_ep = ep_s
                            unsubmitted_reserved.append(ep_s)
                inflight_pairs.append((s, res_ep))
            try:
                def run_inflight_releasing(seg, reserved_ep: str | None):
                    try:
                        return run_inflight(seg)
                    finally:
                        if node_pool is not None and reserved_ep:
                            node_pool.release_inflight(reserved_ep)

                inflight_futures = []
                for s, res_ep in inflight_pairs:
                    fut = pool.submit(run_inflight_releasing, s, res_ep)
                    if res_ep and res_ep in unsubmitted_reserved:
                        unsubmitted_reserved.remove(res_ep)
                    inflight_futures.append(fut)
                other_futures = [pool.submit(run_other, s) for s in other_segs]
            finally:
                if node_pool is not None:
                    for leftover_ep in unsubmitted_reserved:
                        node_pool.release_inflight(leftover_ep)
            for future in inflight_futures:
                try:
                    future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    abort.set()
                    if node_pool is not None:
                        node_pool.notify_all()
                    for pending_future in other_futures:
                        pending_future.cancel()
            for future in other_futures:
                try:
                    future.result()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    abort.set()
                    if node_pool is not None:
                        node_pool.notify_all()
                    for pending_future in other_futures:
                        pending_future.cancel()
            if first_error is not None:
                raise first_error
        finally:
            pool.shutdown(wait=True, cancel_futures=True)


def stage_check(job: Job, film: dict, segs: list[dict]):
    """Burned-subtitle check on every segment with lines (and every segment when the film has any lines)."""
    if not config.SUBTITLE_CHECKS_ENABLED:
        return
    if not film.get("speech") or not any((s.get("line") or "").strip() for s in film["shots"]):
        return
    total_segments = len(plan(film["shots"]))
    for seg in segs:
        rec = job.state["segs"].get(str(seg["idx"])) or {}
        if "subtitles" in rec or not rec.get("file"):
            continue
        job.log("check", message("subtitle_check_start", number=seg["idx"] + 1, total=total_segments))
        try:
            res = check.subtitles(Path(rec["file"]))
        except Exception as exc:                        # a check that can't run is reported, not fatal
            res = {"checked": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
        job.put("segs", str(seg["idx"]), subtitles=res)
        job.log("check", message("subtitle_check_failed", number=seg["idx"] + 1, error=res["error"]) if res.get("error")
                else message("subtitle_check_done", number=seg["idx"] + 1))
        if res.get("burned_text"):
            job.log("SUBTITLES", f"seg{seg['idx'] + 1}", res.get("text"))


def report(job: Job, film: dict, segs: list[dict], mode: str, final: Path) -> Path:
    fr = sum(float(r.get("cny") or 0) for r in job.state["frames"].values())
    gen = sum(float(r.get("cny") or 0) for r in job.state["segs"].values())
    gpu = sum(float(r.get("run_s") or 0) for r in job.state["segs"].values())
    wall = time.time() - float(job.state.get("t_start") or time.time())
    lines = [f"# {film.get('title') or job.dir.name}", "",
             f"成片 `{final.name}`，{media.duration(final):.1f} 秒，{len(segs)} 段，分镜图模式 {mode}。", "",
             f"花费：分镜图 {len(job.state['frames'])} 张 ¥{fr:.2f}，H3 生成 ¥{gen:.2f}，合计 ¥{fr + gen:.2f}；"
             f"GPU {gpu / 60:.1f} 分钟，这次运行墙钟 {wall / 60:.0f} 分钟（续跑只算这一次）。", ""]
    subs = [s for s in segs if (job.state["segs"].get(str(s["idx"])) or {}).get("subtitles", {}).get("burned_text")]
    if subs:
        lines += ["**烧字幕了**：" + "、".join(f"第 {s['idx'] + 1} 段" for s in subs)
                  + "。用 `--redo N` 换种子重跑这一段，或者删掉那句台词。", ""]
    lines += ["| 段 | 秒 | 参考图 | 费用 | GPU 秒 | 字幕检查 |", "|---|---|---|---|---|---|"]
    for s in segs:
        r = job.state["segs"].get(str(s["idx"])) or {}
        sub = r.get("subtitles") or {}
        lines.append(f"| {s['idx'] + 1} | {_num(s['start'])}–{_num(s['end'])} | {len(r.get('pics') or [])} | ¥{float(r.get('cny') or 0):.3f} | "
                     f"{r.get('run_s') or ''} | {'烧字幕：' + sub.get('text', '') if sub.get('burned_text') else ('干净' if sub.get('checked') else '—')} |")
    p = job.dir / "report.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _segment_task_unresolved(rec: dict) -> bool:
    if rec.get("submission_unknown") and not rec.get("task"):
        return True
    if not rec.get("task") or rec.get("terminal_failed") or (rec.get("file") and rec.get("cny") is not None) or "subtitles" in rec:
        return False
    return bool(rec.get("submitted") or rec.get("phase") in ("submit", "poll", "download", "tail") or ("file" in rec and not rec.get("file")))


def invalidated_segment_records(records: dict, mode: str, redo: list[int] | None) -> dict:
    return {k: (dict(rec) if _segment_task_unresolved(rec) else
                {key: rec[key] for key in ("zh", "h3", "h3_source", "h3_reference_version", "prompt_override", "manual_rewrite") if key in rec})
            for k, rec in records.items()
            if any(int(k) == n - 1 or (mode != "shot" and int(k) > n - 1) for n in redo or [])}


def unaffected_segment_blocker(job: Job, mode: str, redo: list[int] | None, *, planned_count: int | None = None) -> dict | None:
    raw_inv = invalidated_segment_records(job.state["segs"], mode, redo)
    actually_invalidated = {
        k for k in raw_inv
        if not _segment_task_unresolved(job.state["segs"].get(k, {}))
    }
    indices = (
        range(planned_count)
        if planned_count is not None
        else [int(k) for k in sorted(job.state["segs"], key=int)]
    )
    unaffected = [{"idx": idx} for idx in indices if str(idx) not in actually_invalidated]
    return preexisting_segment_blocker(job, unaffected)


def invalidate_segments(job: Job, mode: str, redo: list[int] | None):
    """The --redo invalidation rule, shared with the workbench's reviewed-frame continuation."""
    for k, rec in invalidated_segment_records(job.state["segs"], mode, redo).items():
        if _segment_task_unresolved(job.state["segs"].get(k, {})):
            continue
        job.state["segs"][k] = rec
        (job.dir / "clips" / f"seg{int(k) + 1}.mp4").unlink(missing_ok=True)
        (job.dir / "clips" / f"seg{int(k) + 1}-last.jpg").unlink(missing_ok=True)
    job.save()


_IN_WORKBENCH_RUNNER = threading.local()


def _workbench_activity_locked(job_dir: Path) -> Path | None:
    in_wb = getattr(_IN_WORKBENCH_RUNNER, "active", False) or os.environ.get("H3_WORKBENCH_WORKER") == "1"
    if not in_wb and os.name != "nt":
        lock_path = job_dir / "inputs" / "worker.lock"
        if lock_path.exists():
            import fcntl
            try:
                with lock_path.open("a+b") as handle:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        return lock_path
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                if exc.errno != errno.ENOENT:
                    return lock_path
    pending_path = job_dir / "inputs" / "segment-edit.json"
    if pending_path.exists():
        try:
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return pending_path
        if pending and not pending.get("applied"):
            if pending.get("publishing") or pending.get("generated"):
                return pending_path
            revision = str(pending.get("revision") or "").strip()
            number = pending.get("number")
            if revision and isinstance(number, int) and number >= 1:
                staged_file = job_dir / "inputs" / f"segment-{revision}" / "state.json"
                if staged_file.is_file():
                    try:
                        staged = json.loads(staged_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        return pending_path
                    staged_rec = (staged.get("segs") or {}).get(str(number - 1)) or {}
                    if _segment_task_unresolved(staged_rec):
                        return pending_path
    frame_pending_path = job_dir / "inputs" / "frame-edit.json"
    if frame_pending_path.exists():
        try:
            frame_pending = json.loads(frame_pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return frame_pending_path
        if frame_pending and not frame_pending.get("applied"):
            if (frame_pending.get("downloaded")
                    or (frame_pending.get("submission_unknown") and not frame_pending.get("task"))
                    or (frame_pending.get("task") and not frame_pending.get("task_failed") and not frame_pending.get("moderation_error"))):
                return frame_pending_path
    return None


def run(job_dir: Path, film_path: Path | None, mode: str = "shot", redo: list[int] | None = None,
        *, resume_frame_tasks: bool = False, optimize_storyboard: bool = True) -> Path:
    job = Job(job_dir)
    if film_path:
        job.state["film_path"] = str(Path(film_path).resolve())
        job.state["mode"] = mode
        job.state["optimize_storyboard"] = bool(optimize_storyboard)
    elif not optimize_storyboard:
        job.state["optimize_storyboard"] = False
    if not job.state.get("film_path"):
        raise SystemExit("新任务要给 --film")
    mode = job.state.get("mode", mode)
    optimize = bool(job.state.get("optimize_storyboard", optimize_storyboard))
    locked_artifact = _workbench_activity_locked(job.dir)
    if locked_artifact is not None:
        raise RuntimeError(message("segment_publication_pending", path=locked_artifact))
    film = load(Path(job.state["film_path"]))
    segs = plan(film["shots"])
    pics = {s["idx"]: refs(film, s, mode) for s in segs}
    if redo and unaffected_segment_blocker(job, mode, redo, planned_count=len(segs)) is None:
        invalidate_segments(job, mode, redo)
    job.state["t_start"] = time.time()
    job.save()
    job.log("plan", f"{len(segs)} segments", " / ".join(f"{s['seconds']}s·{len(s['shots'])} shots·{len(pics[s['idx']])} pictures" for s in segs))
    for s in segs:
        job.put("segs", str(s["idx"]), pics=[p["kind"] for p in pics[s["idx"]]])
    failures = stage_frames(job, film, segs, pics, resume_tasks=resume_frame_tasks, optimize_storyboard=optimize)
    if failures:
        job.log("frames", message("frames_partial", count=len(failures)))
        return job.dir
    if preexisting_segment_blocker(job, segs) is None:
        stage_prompts(job, film, segs, pics)
    stage_generate(job, film, segs, pics, mode)
    final = job.dir / "film.mp4"
    job.log("assemble", message("film_assembly_start", count=len(segs)))
    media.join([Path(job.state["segs"][str(s["idx"])]["file"]) for s in segs], final)
    job.log("assembled", message("film_assembly_done", path=final))
    stage_check(job, film, segs)
    rep = report(job, film, segs, mode, final)
    job.log("film", final, "report", rep)
    return final
