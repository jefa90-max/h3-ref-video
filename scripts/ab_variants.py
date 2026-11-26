"""Opt-in prompt changes used only by the experiment runner."""
from h3ref import frames, rewrite


ARMS = ("2A", "3A", "3B", "3C-neg", "3C-light", "3D", "4")
IMAGE_ARMS = frozenset(("2A", "3A", "3B", "3C-neg", "3C-light"))
COMPOSITION_ARMS = frozenset(("2A", "3B"))
VIDEO_ARMS = frozenset(("2A", "3D", "4"))
EXAMPLE_ARMS = frozenset(("3D", "4"))

NEUTRAL_VARIANT = {
    "殴打": "激烈交手推搡", "暴打": "重击逼退",
    "鲜血": "暗红痕迹", "伤口": "破损擦痕", "流血": "沾染暗红痕迹",
    "砍杀": "兵刃交锋对峙", "捅伤": "招式逼近格挡", "刺伤": "招式逼近格挡",
    "掐脖": "逼近扣肩制伏", "勒脖": "逼近扣肩制伏",
    "断肢": "散落碎石与道具", "炸飞碎尸": "气浪震散碎石尘土",
}
IMAGE_STATE_NOTE = "首帧穿戴按本镜开场描述。镜内脱摘从原穿戴开始，物品只出现一次。"
VIDEO_STATE_NOTE = (
    "参考图约束身份和默认外观。各镜的穿戴以剧本明确描述的状态为准。镜内摘下或脱下物品时，从首帧穿戴开始，"
    "按动作推进到摘下后的状态，每件道具只出现一次。"
)
COMPOSITION_STATE = (
    "首帧已经脱摘时，在 placement_and_pose 写明变化后的面部或内搭以及物品所在处。"
    "若脱摘发生在本镜内，首帧仍按动作开始时的穿戴。临时变化不写入 locked_look。"
    "前镜显著姿态在本镜结束或已切换时，写明同一人物当前姿态。只有被指定的人完成主动作，"
    "背景人物各自静候。用画面左中右及前中后景写位置，越过人物右肩拍摄时该人物位于画面左前景，"
    "越过左肩时位于右前景。"
)
VISUAL_NEGATION = (
    "For a negative visual cue about expression, luminosity, an empty shot, or visible injury, "
    "write the visible expression, matte material, present subject, or garment condition in positive terms. "
    "Preserve ordinary negative statements in dialogue, plot and action."
)
ACTING = (
    "When the script calls for a big emotional beat in close view, specify the brow, eyelids, cheeks, and mouth or jaw, "
    "plus visible breath or posture. A genuine laugh pulls up both cheeks and creases the eye corners; "
    "a polite smile can move only the mouth. Surprise lifts brows and releases the jaw; fear adds a knitted brow, "
    "a brief sharp inhale and shoulders drawing back. Use a clear onset, peak and recovery for strong reactions. "
    "For restraint keep the requested small movement, such as a settled jaw and measured breathing. Use no AU codes."
)
VIDEO_STATE_RULE = (
    "Distinguish the stable reference identity from explicit per-shot wardrobe and prop states. "
    "Open with the stated starting state, show the scripted removal and its end state, and keep a single instance of each object. "
    "Give the core action only to the named subject; the other people stay in their own positions."
)
DEMONSTRATIONS = {
    "3D": (
        "Input cue: 面无笑意，哑光道具不发光，镜头里不能是空镜。 "
        "Detailed description excerpt: [Shot 1] She stands beside the window with relaxed cheeks and a level mouth, "
        "holding the matte copper token in natural window light. Her spoken words remain exactly as supplied."
    ),
    "4": (
        "Input cue: 近景，突然受到强烈惊吓，几秒后缓过来。 "
        "Detailed description excerpt: [Shot 1] His brows lift and knit at the center. His upper lids snap open, "
        "his cheeks pull taut, his jaw drops, and his shoulders draw back on a sharp inhale before slowly lowering."
    ),
}


def _replace_rule(text, old, new):
    if text.count(old) != 1:
        raise ValueError("An A/B rule anchor changed; review the baseline before running")
    return text.replace(old, new, 1)


def variant_names(arm):
    return ("rules", "examples") if arm in EXAMPLE_ARMS else ("rules",)


def prompt_options(arm, variant, examples):
    if variant == "baseline":
        return {}
    if variant == "examples":
        # Keep both full examples and put the isolated cue alongside the first example.
        marker = "## Example 2"
        return {"examples_override": _replace_rule(examples, marker, DEMONSTRATIONS[arm] + "\n\n" + marker)}
    if arm == "3A":
        return {"neutral_words": NEUTRAL_VARIANT}
    if arm == "3B":
        target = "例如改为紧张对峙、防备架势、气场压迫、衣衫微乱或远景剪影"
        replacement = "例如退半步扶住桌沿，衣摆甩动、杯中水珠飞溅，侧光下只见远景剪影"
        return {"composition_rules": _replace_rule(frames.COMPOSITION_RULES, target, replacement)}
    if arm == "3C-neg":
        return {"tail_override": _replace_rule(
            frames.TAIL, "不增加额外人物。不出现字幕、文字、水印和任何真实品牌的标志。",
            "画面人物仅为剧本出场角色。道具与服装使用纯色及抽象图案，成片为干净电影画面。"
        )}
    if arm == "3C-light":
        return {"tail_override": _replace_rule(frames.TAIL, "电影级布光。", "沿用场景可见主光方向，保留面部细节与自然阴影。")}
    if arm == "3D":
        return {"rules_override": _replace_rule(rewrite.RULES, "\n6. non_diegetic_music",
                                                f"\n   {VISUAL_NEGATION}\n6. non_diegetic_music")}
    if arm == "4":
        start = rewrite.RULES.index("\n7. Acting")
        end = rewrite.RULES.index("\n8. Open")
        return {"rules_override": rewrite.RULES[:start] + f"\n7. {ACTING}" + rewrite.RULES[end:]}
    if arm == "2A":
        rules = _replace_rule(rewrite.RULES, "carry over every look detail from the input: hair, eyes, outfit, props",
                              "carry over stable identity details; explicit per-shot costume or prop states take precedence")
        return {"composition_rules": frames.COMPOSITION_RULES + COMPOSITION_STATE,
                "image_state_note": IMAGE_STATE_NOTE, "video_state_note": VIDEO_STATE_NOTE,
                "rules_override": rules + "\n   " + VIDEO_STATE_RULE}
    raise ValueError(f"Unknown A/B arm: {arm}")


def stub_composition(messages):
    """Return valid v3 structure for the request, with no generated-media claim."""
    import json

    context = json.loads(messages[-1]["content"])
    shot = context["current_shot"]
    characters = [
        {"name": person["name"], "locked_look": person["look"],
         "placement_and_pose": "人物在画面中央，保持镜头开场时的姿态。"}
        for person in context["cast"] if person["name"] in shot.get("action", "")
    ]
    reply = {"version": 3, "camera": shot.get("camera") or "中景",
             "scene": context.get("key_scenes") or "场景按剧本保持一致。", "characters": characters}
    return {"choices": [{"message": {"content": json.dumps(reply, ensure_ascii=False)}}]}


def stub_rewrite(n_images, aspect):
    parts = {
        "subject_definitions": " ".join(f"<Picture {i}> is an input reference." for i in range(1, n_images + 1))
        or "The subject follows the supplied shot.",
        "summary": "Offline structure preview.",
        "retention_analysis": "Keep the visible subject identity consistent.",
        "detailed_description": f"{aspect}. [Shot 1] The scene follows the supplied shot.",
        "overall_soundscape": "Natural room tone.",
        "non_diegetic_music": "N/A",
    }
    return rewrite.assemble(parts)
