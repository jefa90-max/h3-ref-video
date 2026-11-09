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
import hashlib
import json
import math
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import check, config, frames, http, media, rewrite, rh, staging
from .i18n import message

GROUP_WORDS = ("两人", "二人", "三人", "四人", "俩", "大家", "众人", "所有人", "他们", "她们", "全员", "everyone", "both")
LANG = {"zh": "中文", "en": "英文", "ja": "日文", "ko": "韩文", "es": "西班牙文", "pt": "葡萄牙文"}
ORIENT = {"9:16": "竖屏", "16:9": "横屏", "1:1": "方形"}
RULES = ("完整表现动作的原因、接触和结果，动作真实连续，不要静态立绘晃动；角色不互换外观，道具不凭空出现或消失，不增加额外人物。"
         "画风全片统一为上面写的风格；每个角色全片是同一位演员，长相、发型、妆容和服装前后一致；"
         "画面里不出现字幕和文字，包装和道具上不印可读的字，只用图案和颜色区分；不出现任何真实品牌的标志。")


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
        c["picture"] = str(p if p.is_absolute() else (base / p).resolve())
        if check_images and not Path(c["picture"]).is_file():
            raise SystemExit(f"找不到 {c['name']} 的参考图：{c['picture']}")
    if film.get("board"):
        p = Path(film["board"]).expanduser()
        film["board"] = str(p if p.is_absolute() else (base / p).resolve())
    shots = [s for s in film.get("shots") or [] if (s.get("action") or "").strip()]
    if not shots:
        raise SystemExit("film.json 里没有镜头（shots）")
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
    return out


def in_shots(c: dict, shots: list[dict]) -> bool:
    """Does this character appear in these shots? Generous on purpose: an extra picture costs little, a missing one lets
    the model draw the character from words alone."""
    text = " ".join(f"{s.get('action') or ''} {s.get('line') or ''}" for s in shots)
    if any(g in text for g in GROUP_WORDS):
        return True
    return any(n and n in text for n in [c["name"], *(c.get("aliases") or [])])


def refs(film: dict, seg: dict, mode: str) -> list[dict]:
    """The segment's pictures in <Picture N> order: board, each character in it, then the frames or the previous last frame.
    Nine at most: when they run short the board goes first, then the later shots' frames."""
    cast = [c for c in film.get("cast") or [] if in_shots(c, seg["shots"])] or list(film.get("cast") or [])
    board = [{"kind": "board", "path": film["board"]}] if film.get("board") else []
    people = [{"kind": "cast", "path": c["picture"], "cast": c} for c in cast]
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
        raise SystemExit(f"第 {seg['idx'] + 1} 段要 {len(out)} 张参考图，H3 最多 {config.MAX_IMAGES} 张；减少这一段的角色")
    return out


def compile_prompt(film: dict, seg: dict, pics: list[dict], count: int, *, compositions: dict[str, dict] | None = None) -> str:
    """The segment's prompt in Chinese, written the way you would for Seedance; rewrite.py turns it into H3's format."""
    compositions = {key: value for key, value in (compositions or {}).items()
                    if key in {_frame_key(seg['idx'], p['shot']) for p in pics if p['kind'] == 'frame'}}
    covered = {c['name'] for comp in compositions.values() for c in comp['characters']}
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
            look = "" if p["cast"]["name"] in covered else (p["cast"].get("look") or "").strip().rstrip("。")
            lines.append(f"参考图片{k}只约束{p['cast']['name']}身份" + (f"：{look}。" if look else "。"))
        elif p["kind"] == "frame":
            s = seg["shots"][p["shot"]]
            span = f"{_num(float(s['start']) - offset)}–{_num(float(s['end']) - offset)}秒"
            lines.append(f"参考图片{k}是第{p['shot'] + 1}个镜头（{span}）开始那一瞬的画面，也就是 [Shot {p['shot'] + 1}] 的首帧："
                         "这个镜头从这张图开始演，人物位置、服装、光线和构图照它，之后按分镜接着演。")
        else:
            first = any(x["kind"] == "frame" for x in pics)
            lines.append(f"参考图片{k}是上一段的最后一个画面：" + ("只用来保持画风、光线、场景和角色状态一致，这一段的第一个镜头从它的首帧开始。"
                                                       if first else "这一段从这个画面直接接着演，画风、光线、场景和角色状态保持一致。"))
    n = sum(1 for p in pics if p["kind"] == "cast")
    if n:
        lines.append((f"只有这{n}名角色" if n > 1 else "只有这一名角色") + "，保持各自发型、五官、服饰纹样与比例。")
    out = [head, "".join(lines)]
    if not compositions and (film.get("setting") or "").strip():
        out.append("场景与关键道具（全片统一）：" + film["setting"].strip())
    speech = film.get("speech") or ""
    shot_lines = []
    for j, s in enumerate(seg["shots"]):
        composition = compositions.get(_frame_key(seg["idx"], j))
        cam = (s.get("camera") or "").strip().strip("【】")
        text = f"{_num(float(s['start']) - offset)}–{_num(float(s['end']) - offset)}秒：" + (f"【{cam}】" if cam else "") \
            + (staging.render_motion(composition) if composition else (s.get("action") or "").strip().rstrip("。")) + "。"
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
            for attempt in range(10):
                try:
                    tmp.replace(self.file)
                    break
                except PermissionError as exc:
                    # Windows readers can briefly block replacement of the state file.
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 9:
                        raise
                    time.sleep(min(0.05 * 2 ** attempt, 0.5))

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


def frame_reference_paths(film: dict, shot: dict, record: dict) -> list[str]:
    characters = record.get("character_reference_paths", [c["picture"] for c in film.get("cast") or [] if in_shots(c, [shot])])
    return ([film["board"]] if film.get("board") else []) + characters


def reference_version(paths, *, metadata=None) -> str:
    sources = []
    metadata = {} if metadata is None else metadata
    for source in paths:
        key = str(source)
        if key not in metadata:
            path = Path(source)
            stat = path.stat() if path.is_file() else None
            metadata[key] = (str(path.resolve()), (stat.st_mtime_ns, stat.st_size) if stat else None)
        sources.append(metadata[key])
    return hashlib.sha256(json.dumps(sources, ensure_ascii=False).encode()).hexdigest()


def frame_reference_versions(film: dict, segs: list[dict], records: dict) -> dict[str, str]:
    metadata = {}
    return {_frame_key(s['idx'], j): reference_version(frame_reference_paths(film, shot, records.get(_frame_key(s['idx'], j), {})), metadata=metadata)
            for s in segs for j, shot in enumerate(s['shots'])}


def saved_compositions(records: dict, *, reference_versions: dict[str, str]) -> dict[str, dict]:
    return {key: rec['composition'] for key, rec in records.items()
            if isinstance(rec.get('composition'), dict) and rec['composition'].get('version') == 2
            and isinstance(rec.get('prompt'), str) and rec['prompt'].strip()
            and rec.get('composition_prompt') == rec['prompt']
            and rec.get('composition_reference_version')
            and rec['composition_reference_version'] == reference_versions.get(key)}


def stage_frames(job: Job, film: dict, segs: list[dict], pics: dict, *, resume_tasks: bool = False):
    """Plan in shot order, save before dispatch, and collect every independent drawing."""
    (job.dir / "frames").mkdir(exist_ok=True)
    wanted = [(i, p['shot']) for i, seg in enumerate(segs) for p in pics[i] if p['kind'] == 'frame']
    versions = frame_reference_versions(film, segs, job.state['frames'])
    effective = saved_compositions(job.state['frames'], reference_versions=versions)
    known_scenes, previous, previous_key = {}, None, None

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
                composition = effective.get(key)
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
                            text = rec.get('prompt') or frames.prompt(film, shot, who, bool(film.get('board')), rec.get('composition'))
                            job.log('staging cache', key, 'source', previous_key)
                        else:
                            job.log('staging plan', key, 'source', previous_key)
                            planned = frames.describe_composition(film, shot, previous_composition=copy.deepcopy(previous), known_scenes=copy.deepcopy(known_scenes))
                            composition = staging.validate_composition(planned, film, shot, known_scenes)
                            text = frames.prompt(film, shot, who, bool(film.get('board')), composition)
                            job.put('frames', key, composition=composition, prompt=text, composition_prompt=text,
                                    composition_reference_version=versions[key], reference_paths=references)
                        futures.append(pool.submit(draw, key, text, references, rec.get('task')))
                    except Exception as exc:
                        if not rec.get('prompt'):
                            job.put('frames', key, prompt=frames.prompt(film, shot, who, bool(film.get('board'))))
                        contextual = RuntimeError(message('staging_plan_failed', frame_key=key, model=config.LLM_MODEL,
                                                          path=job.file, detail=str(exc))) if not rec.get('task') and not rec.get('submission_unknown') else exc
                        failure(key, contextual)
                        composition = None
                if composition:
                    known_scenes[composition['scene_id']] = {k: copy.deepcopy(composition[k]) for k in ('world_frame','fixed_objects')}
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
            return
        zh = segment_prompt_text(rec, compile_prompt(film, seg, pics[i], len(segs), compositions=compositions))
        reference = reference_version(_picture_paths(job, seg, pics[i]))
        if rec.get('h3') and rec.get('zh') == zh and (not rec.get('h3_reference_version') or rec['h3_reference_version'] == reference):
            return
        h3 = rewrite.rewrite(zh, len(pics[i]), seg['seconds'], film['aspect'])
        (job.dir / 'prompts' / f'seg{i + 1}.zh.txt').write_text(zh, encoding='utf-8')
        (job.dir / 'prompts' / f'seg{i + 1}.h3.txt').write_text(h3, encoding='utf-8')
        job.put('segs', str(i), zh=zh, h3=h3, h3_source=zh, h3_reference_version=reference)
        job.log('prompt', f'seg{i + 1}', f'{len(pics[i])} pictures')
    with ThreadPoolExecutor(3) as pool:
        list(pool.map(one, segs))


def _picture_paths(job: Job, seg: dict, pics: list[dict]) -> list[Path]:
    out = []
    for p in pics:
        if p["kind"] in ("board", "cast"):
            out.append(Path(p["path"]))
        elif p["kind"] == "frame":
            out.append(job.dir / "frames" / f"{_frame_key(seg['idx'], p['shot'])}.png")
        else:
            out.append(job.dir / "clips" / f"seg{seg['idx']}-last.jpg")
    return out


def gen_segment(job: Job, film: dict, seg: dict, pics: list[dict], *, reference_paths: list[Path] | None = None,
                guard_submission: bool = True) -> dict:
    i = seg["idx"]
    out = job.dir / "clips" / f"seg{i + 1}.mp4"
    rec = job.state["segs"].get(str(i)) or {}
    if out.exists() and rec.get("cny") is not None:
        return rec
    if rec.get("terminal_failed"):
        raise RuntimeError(message("segment_terminal_failed", number=i + 1, path=job.file))
    if not rec.get("task"):
        if rec.get("submission_unknown"):
            raise RuntimeError(message("submission_unknown", number=i + 1, path=str(job.file)))
        paths = _picture_paths(job, seg, pics) if reference_paths is None else reference_paths
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            raise SystemExit(f"第 {i + 1} 段缺参考图：{missing}")
        job.put("segs", str(i), phase="upload")
        names = [rh.upload(p) for p in paths]
        seed = int(rec.get("seed") or random.randrange(10 ** 14, 10 ** 15))
        g = rh.graph(rec["h3"], names, seg["seconds"], film["aspect"], seed)
        record_submission = (lambda unknown: job.put("segs", str(i), submission_unknown=unknown, seed=seed)) if guard_submission else None
        job.put("segs", str(i), phase="submit")
        task = rh.submit_with_retry(g, log=lambda detail: job.log(f"seg{i + 1}", detail),
                                    record_submission=record_submission)
        job.put("segs", str(i), task=task, seed=seed, submitted=time.time(),
                **({"submission_unknown": False} if guard_submission else {}))
        job.log("submitted", f"seg{i + 1}", f"seed {seed}", f"≈¥{rh.estimate_cny(seg['seconds'])}")
        rec = job.state["segs"][str(i)]
    job.put("segs", str(i), phase="poll")
    while True:
        st = rh.poll(rec["task"])
        if st["state"] != "running":
            break
        time.sleep(20)
    if st["state"] == "failed":
        job.put("segs", str(i), terminal_failed=True, error=st.get("error"))
        raise RuntimeError(message("segment_terminal_failed", number=i + 1, path=job.file))
    job.put("segs", str(i), phase="download")
    http.download(st["url"], out)
    job.put("segs", str(i), phase="tail")
    media.last_frame(out, job.dir / "clips" / f"seg{i + 1}-last.jpg")
    job.put("segs", str(i), file=str(out), cny=st["cny"], coins=st.get("coins"), run_s=st.get("run_seconds"),
            wall_s=round(time.time() - float(rec.get("submitted") or time.time())), error=None)
    job.log("done", f"seg{i + 1}", f"¥{st['cny']:.3f}", f"{st.get('run_seconds')} s on the GPU")
    return job.state["segs"][str(i)]


def stage_generate(job: Job, film: dict, segs: list[dict], pics: dict, mode: str):
    (job.dir / "clips").mkdir(exist_ok=True)
    chained = any(p["kind"] == "prev" for ps in pics.values() for p in ps)
    if chained:                                         # each segment starts from the one before
        for seg in segs:
            gen_segment(job, film, seg, pics[seg["idx"]])
    else:
        with ThreadPoolExecutor(max(1, config.RH_CONCURRENCY)) as pool:
            list(pool.map(lambda s: gen_segment(job, film, s, pics[s["idx"]]), segs))


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


def invalidate_segments(job: Job, mode: str, redo: list[int] | None):
    """The --redo invalidation rule, shared with the workbench's reviewed-frame continuation."""
    for n in redo or []:                                # a new seed for these segments (and, chained, everything after)
        for k in [k for k in job.state["segs"] if int(k) == n - 1 or (mode != "shot" and int(k) > n - 1)]:
            rec = job.state["segs"][k]
            job.state["segs"][k] = {key: rec[key] for key in ("zh", "h3", "h3_source", "h3_reference_version", "prompt_override") if key in rec}
            (job.dir / "clips" / f"seg{int(k) + 1}.mp4").unlink(missing_ok=True)
    job.save()


def run(job_dir: Path, film_path: Path | None, mode: str = "shot", redo: list[int] | None = None) -> Path:
    job = Job(job_dir)
    if film_path:
        job.state["film_path"] = str(Path(film_path).resolve())
        job.state["mode"] = mode
    if not job.state.get("film_path"):
        raise SystemExit("新任务要给 --film")
    mode = job.state.get("mode", mode)
    pending_path = job.dir / "inputs" / "segment-edit.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8")) if pending_path.exists() else {}
    if pending.get("publishing") and not pending.get("applied"):
        raise RuntimeError(message("segment_publication_pending", path=pending_path))
    film = load(Path(job.state["film_path"]))
    segs = plan(film["shots"])
    pics = {s["idx"]: refs(film, s, mode) for s in segs}
    invalidate_segments(job, mode, redo)
    job.state["t_start"] = time.time()
    job.save()
    job.log("plan", f"{len(segs)} segments", " / ".join(f"{s['seconds']}s·{len(s['shots'])} shots·{len(pics[s['idx']])} pictures" for s in segs))
    for s in segs:
        job.put("segs", str(s["idx"]), pics=[p["kind"] for p in pics[s["idx"]]])
    failures = stage_frames(job, film, segs, pics)
    if failures:
        job.log("frames", message("frames_partial", count=len(failures)))
        return job.dir
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
