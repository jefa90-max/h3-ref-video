"""Offline artifacts only. This module never calls a model or cloud API."""
import json
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from h3ref import film, media, rewrite
from .i18n import message


def demo_h3_prompt(n_images: int, seconds: float, aspect: str, number: int) -> str:
    refs_desc = ", ".join(f"<Subject {i}> from <Picture {i}>" for i in range(1, n_images + 1)) or "<Subject 1> Demo character"
    retention = "; ".join(f"<Subject {i}>: fully_preserved" for i in range(1, max(1, n_images) + 1)) + "."
    parts = {
        "subject_definitions": f"{refs_desc} (DEMO offline preview for segment {number}).",
        "summary": f"[reference generation] Offline demo H3 preview for segment {number} ({seconds:g}s, {aspect}).",
        "retention_analysis": retention,
        "detailed_description": (
            f"Demo offline {aspect} clip ({seconds:g}s).\n"
            f"[Shot 1] Offline preview of segment {number}; configure API keys and disable demo mode for real LLM rewriting."
        ),
        "overall_soundscape": "Natural ambient room tone and subtle movement foley.",
        "non_diegetic_music": "N/A",
    }
    return "\n\n".join(f"{name}:\n{parts[name]}" for name in rewrite.SECTIONS)


def placeholder(path, label, aspect):
    size = {"9:16": (270, 480), "16:9": (480, 270), "1:1": (360, 360)}[aspect]
    image = Image.new("RGB", size, "#103c3c")
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, size[0] - 16, size[1] - 16), outline="#74c9ad", width=2)
    draw.text((32, size[1] // 2 - 20), "DEMO / OFFLINE", fill="white")
    draw.text((32, size[1] // 2 + 6), label, fill="#74c9ad")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def render_clip(output, seconds, aspect, label):
    still = placeholder(output.with_suffix(".png"), label, aspect)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still),
                    "-t", str(seconds), "-r", "24", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)], check=True, timeout=120)


def run(request):
    out = Path(request["output_dir"])
    lang = request["lang"]
    log = lambda key, **values: print(message(key, lang, **values), flush=True)
    log("demo_start")
    mode = request.get("frames", "none")
    frames_count = 0
    if request["kind"] == "film":
        script = request["film"]
        demo_cast_map: dict[str, str] = {}
        for index, person in enumerate(script.get("cast", [])):
            orig_pic = str(person.get("picture") or "")
            if not Path(orig_pic).is_file():
                if orig_pic not in demo_cast_map:
                    demo_cast_map[orig_pic] = str(placeholder(out / "inputs" / f"demo-cast-{index}.png", f"CAST {index + 1}", script["aspect"]))
                person["picture"] = demo_cast_map[orig_pic]
        if script.get("board") and not Path(script["board"]).is_file():
            script["board"] = str(placeholder(out / "inputs" / "demo-board.png", "BOARD", script["aspect"]))
        source = out / "inputs" / "demo-film.json"
        source.write_text(json.dumps(script, ensure_ascii=False, indent=2), encoding="utf-8")
        script = film.load(source)
        segments = film.plan(script["shots"])
        aspect = script["aspect"]
        for segment in segments:
            pics = film.refs(script, segment, mode)
            prompt = film.compile_prompt(script, segment, pics, len(segments))
            (out / "prompts").mkdir(exist_ok=True)
            (out / "prompts" / f"seg{segment['idx'] + 1}.zh.txt").write_text(prompt, encoding="utf-8")
            for picture in pics:
                if picture["kind"] == "frame":
                    name = f"s{segment['idx'] + 1}-{picture['shot'] + 1}"
                    path = out / "frames" / f"{name}.png"
                    if not path.exists():
                        placeholder(path, name, aspect)
                    frames_count += 1
                    log("demo_frame", name=name)
    else:
        aspect = request["aspect"]
        segments = [{"idx": 0, "seconds": request["seconds"]}]
    log("demo_plan", segments=len(segments), frames=frames_count)
    (out / "plan.json").write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    if request["kind"] == "film":
        from .storyboard import frame_records, plan
        from .jobs import write_json
        records = frame_records(request)
        for description in plan(request)[3]:
            records.setdefault(description["key"], {"prompt": description["prompt"], "file": description["path"], "cny": 0})
        write_json(out / "inputs" / "demo-frames.json", {"frames": records})
    if request.get("operation") == "frames":
        log("frames_ready")
        return
    has_video = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    if has_video:
        clips = []
        for segment in segments:
            name = f"seg{segment['idx'] + 1}"
            revision = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((out / "frames").glob(f"s{segment['idx'] + 1}-*.png")))).hexdigest()[:6]
            still = placeholder(out / "inputs" / f"demo-{name}.png", f"{name} / {revision}", aspect)
            clip = out / "clips" / f"{name}.mp4"
            clip.parent.mkdir(exist_ok=True)
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(still),
                            "-t", str(segment["seconds"]), "-r", "24", "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)], check=True, timeout=120)
            media.last_frame(clip, clip.with_name(f"{name}-last.jpg"))
            clips.append(clip)
            log("demo_clip", name=name)
        final = out / ("film.mp4" if request["kind"] == "film" else "clip.mp4")
        if request["kind"] == "film" and (out / "state.json").is_file():
            fjob = film.Job(out)
            changed_demo = False
            for segment in segments:
                srec = fjob.state.get("segs", {}).get(str(segment["idx"]))
                if isinstance(srec, dict) and ("manual_rewrite" in srec or "clip_prompts" in srec):
                    srec.pop("manual_rewrite", None)
                    srec.pop("clip_prompts", None)
                    changed_demo = True
            if changed_demo:
                fjob.save()
        if len(clips) == 1:
            shutil.copyfile(clips[0], final)
        else:
            media.join(clips, final)
    else:
        log("demo_no_video")
    report = message("demo_report", lang, kind=request["kind"], mode=mode, segments=len(segments), frames=frames_count,
                     seconds=sum(s["seconds"] for s in segments), path=out,
                     video=message("demo_video" if has_video else "demo_no_video", lang))
    (out / "report.md").write_text(report, encoding="utf-8")
    log("demo_done", path=out / "report.md")


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")))
