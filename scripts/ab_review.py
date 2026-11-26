"""Render a local blind-review packet from paired A/B outputs."""

import hashlib
import json
import random
import shutil
from collections.abc import Callable
from pathlib import Path


_MEDIA_TYPES = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
    ".mp4": "video", ".webm": "video",
}
_HTML_ESCAPE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026",
                              "\u2028": "\\u2028", "\u2029": "\\u2029"})


def _copy_side_media(sources, output: Path, pair_id: str, side: str) -> list[dict]:
    media = []
    for index, source in enumerate(sources, 1):
        source = Path(source)
        extension = source.suffix.lower()
        if extension not in _MEDIA_TYPES:
            raise ValueError("Unsupported blind-review media format")
        if not source.is_file():
            raise ValueError("Blind-review media is missing")
        suffix = f"_{index}" if len(sources) > 1 else ""
        relative = f"blind/{pair_id}_{side}{suffix}{extension}"
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        media.append({"src": relative, "kind": _MEDIA_TYPES[extension]})
    return media


def write_review(output: Path, pairs: list[dict], *, run_id: str, seed: int, dry_run: bool,
                 redact: Callable[[str], str]) -> None:
    """Write a standalone HTML board and its separate, non-public answer key."""
    output.mkdir(parents=True, exist_ok=True)
    review_id = hashlib.sha256(f"{run_id}\0{seed}".encode()).hexdigest()[:20]
    public_pairs = []
    private_pairs = {}
    for pair in pairs:
        randomizer = random.Random(f"{review_id}\0{pair['id']}")
        pair_id = f"{randomizer.getrandbits(64):016x}"
        if pair_id in private_pairs:
            raise ValueError("Duplicate blind-review pair")
        left, right = pair["sides"]
        if randomizer.getrandbits(1):
            left, right = right, left
        public = {"id": pair_id, "arm": redact(pair["arm"]), "description": redact(pair["description"]),
                  "complete": all(side.get("complete", True) for side in (left, right))}
        private = {}
        for position, side, suffix in (("left", left, "L"), ("right", right, "R")):
            public[position] = _copy_side_media(side["media"], output, pair_id, suffix)
            private[position] = {
                "label": redact(side["label"]), "cny": side["cny"],
                "moderation_failed": side["moderation_failed"], "retries": side["retries"],
            }
        public_pairs.append(public)
        private_pairs[pair_id] = private

    messages = Path(__file__).with_name("messages")
    localized = {language: json.loads((messages / f"ab-review-{language}.json").read_text(encoding="utf-8"))
                 for language in ("zh-CN", "en")}
    key = {"review_id": review_id, "run_id": redact(run_id), "dry_run": dry_run, "pairs": private_pairs}
    payload = {"review_id": review_id, "pairs": public_pairs, "dry_run": dry_run, "messages": localized}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False).translate(_HTML_ESCAPE)
    (output / "answer_key.json").write_text(json.dumps(key, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                                          encoding="utf-8")
    (output / "ab_review.html").write_text(_PAGE.replace("__REVIEW_DATA__", data), encoding="utf-8")


_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' file:; media-src 'self' file:; base-uri 'none'; form-action 'none'">
<title></title>
<style>
:root { color-scheme: light; font: 16px/1.5 system-ui, sans-serif; color: #172725; background: #f3f5f1; }
body { margin: 0; }
main { max-width: 75rem; padding: 1.5rem clamp(.75rem, 4vw, 3rem) 3rem; margin: auto; }
h1 { font-size: clamp(1.6rem, 4vw, 2.5rem); margin: 0; }
h2 { font-size: 1.1rem; margin: 0 0 .75rem; overflow-wrap: anywhere; }
header, .toolbar { display: flex; gap: .7rem; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.controls { display: flex; flex-wrap: wrap; align-items: center; gap: .55rem; margin: .75rem 0; }
label, button, select, input, textarea { font: inherit; }
button, select, textarea, input[type=file] { box-sizing: border-box; background: #fff; color: inherit; border: 1px solid #72867e; border-radius: .4rem; padding: .45rem .65rem; }
button { cursor: pointer; min-height: 2.75rem; }
button[aria-pressed="true"] { background: #173a33; color: #fff; border-color: #173a33; }
button:disabled { cursor: default; opacity: .6; }
:focus-visible { outline: 3px solid #267353; outline-offset: 2px; }
article, footer { border: 1px solid #bac9c0; border-radius: .6rem; padding: clamp(.65rem, 2vw, 1.15rem); margin-top: 1rem; background: #fff; }
.visuals, .results { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .55rem; margin-block: .4rem; }
.side-title { font-weight: 650; }
.media { min-height: 7rem; height: min(51vw, 28rem); object-fit: contain; width: 100%; display: grid; place-items: center; background: #18201f; color: #f4f6f4; border-radius: .3rem; }
.placeholder { box-sizing: border-box; padding: .35rem; text-align: center; overflow-wrap: anywhere; }
textarea { width: 100%; resize: vertical; margin-top: .25rem; }
input[type=range] { flex: 1 1 8rem; accent-color: #267353; }
input[type=file] { max-width: 100%; }
.muted { color: #485e55; }
.note { background: #e4ede7; border-radius: .4rem; padding: .65rem .8rem; }
.results p { overflow-wrap: anywhere; }
</style>
</head>
<body>
<main>
  <header><h1 id="title"></h1><label><span id="language-label"></span> <select id="language"></select></label></header>
  <p id="instructions"></p>
  <p id="preview" class="note" hidden></p>
  <div class="toolbar"><div id="tabs" class="controls" role="group"></div><span id="progress" aria-live="polite"></span></div>
  <div id="comparisons"></div>
  <footer>
    <p id="reveal-note"></p>
    <label id="key-label" hidden><span id="key-label-text"></span> <input id="key-file" type="file" accept="application/json,.json"></label>
    <p id="reveal-status" aria-live="polite" hidden></p>
    <button id="export" type="button" disabled></button>
  </footer>
</main>
<script type="application/json" id="review-data">__REVIEW_DATA__</script>
<script>
"use strict";
const byId = (id) => document.getElementById(id);
const review = JSON.parse(byId("review-data").textContent);
let language = "zh-CN";
let selectedArm = review.pairs[0]?.arm;
let answer = null;
const votes = new Map();
const reasons = new Map();
const matching = (pair) => pair.complete && pair.left.length > 0 && pair.left.length === pair.right.length
  && pair.left.every((media, index) => media.kind === pair.right[index].kind);
const eligible = review.pairs.filter(matching).length;

function t(key, fields = {}) {
  return review.messages[language][key].replace(/\{(\w+)\}/g, (_, name) => String(fields[name]));
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action) {
  const node = element("button", text);
  node.type = "button";
  node.addEventListener("click", action);
  return node;
}

function updateProgress() {
  const completed = eligible === votes.size;
  byId("progress").textContent = t("progress", {done: votes.size, total: eligible});
  byId("reveal-note").textContent = t(answer ? "revealed" : completed ? "revealReady" : "revealLocked");
  byId("key-label").hidden = !completed || !!answer;
  byId("export").disabled = !completed || !eligible;
}

function mediaElement(media, side) {
  if (!media) return element("div", t("missingMedia"), "media placeholder");
  const node = element(media.kind === "video" ? "video" : "img", undefined, "media");
  node.src = media.src;
  if (media.kind === "image") {
    node.alt = t(side);
    node.loading = "lazy";
  } else {
    node.preload = "metadata";
    node.playsInline = true;
    node.muted = true;
    node.setAttribute("aria-label", t(side));
  }
  return node;
}

function playback(left, right) {
  const videos = [left, right].filter((node) => node.tagName === "VIDEO");
  const controls = element("div", undefined, "controls");
  const duration = () => videos.every((video) => Number.isFinite(video.duration))
    ? Math.min(...videos.map((video) => video.duration)) : 0;
  const timeline = element("input");
  timeline.type = "range";
  timeline.min = "0";
  timeline.max = "0";
  timeline.value = "0";
  timeline.step = "0.05";
  timeline.disabled = true;
  timeline.setAttribute("aria-label", t("timeline"));
  const status = element("small", "", "muted");
  status.setAttribute("aria-live", "polite");
  const seek = (time) => videos.forEach((video) => { video.currentTime = time; });
  const pause = () => {
    videos.forEach((video) => video.pause());
    toggle.textContent = t("play");
  };
  const toggle = button(t("play"), async () => {
    if (videos.some((video) => !video.paused)) {
      pause();
      return;
    }
    if (videos[0].currentTime >= duration() - 0.05) seek(0);
    const results = await Promise.allSettled(videos.map((video) => video.play()));
    if (results.some((result) => result.status === "rejected")) {
      pause();
      status.textContent = t("playbackUnavailable");
    } else {
      status.textContent = "";
      toggle.textContent = t("pause");
    }
  });
  const updateDuration = () => {
    timeline.max = String(duration());
    toggle.disabled = timeline.disabled = duration() <= 0;
  };
  updateDuration();
  timeline.addEventListener("input", () => seek(Number(timeline.value)));
  videos[0].addEventListener("timeupdate", () => {
    const time = Math.min(videos[0].currentTime, duration());
    timeline.value = String(time);
    if (!videos[0].paused && time >= duration()) pause();
    if (videos[1] && !videos[0].paused && !videos[1].paused && !videos[1].seeking
        && Math.abs(videos[1].currentTime - time) > 0.25) videos[1].currentTime = time;
  });
  for (const video of videos) {
    video.addEventListener("loadedmetadata", updateDuration);
    video.addEventListener("ended", pause);
    video.addEventListener("error", () => {
      pause();
      toggle.disabled = timeline.disabled = true;
      status.textContent = t("playbackUnavailable");
    });
  }
  const audio = element("select");
  audio.setAttribute("aria-label", t("audio"));
  for (const [choice, node] of [["muted", null], ["left", left], ["right", right]]) {
    if (node && node.tagName !== "VIDEO") continue;
    const option = element("option", t(choice));
    option.value = choice;
    audio.append(option);
  }
  audio.addEventListener("change", () => {
    for (const [side, node] of [["left", left], ["right", right]]) {
      if (node.tagName === "VIDEO") node.muted = audio.value !== side;
    }
  });
  const audioLabel = element("label", t("audio") + " ");
  audioLabel.append(audio);
  controls.append(toggle, timeline, audioLabel, status);
  return controls;
}

function ratingControls(pair, article) {
  if (!matching(pair)) {
    article.append(element("p", t("unavailableVote"), "muted"));
    return;
  }
  const rating = element("div", undefined, "controls");
  rating.setAttribute("role", "group");
  rating.setAttribute("aria-label", t("rating"));
  const reason = element("textarea");
  reason.rows = 2;
  reason.maxLength = 600;
  reason.value = reasons.get(pair.id) ?? "";
  reason.disabled = !!answer;
  const choices = Object.entries({left: "preferLeft", tie: "tie", right: "preferRight"});
  const buttons = choices.map(([value, key]) => {
    const option = button(t(key), () => {
      votes.set(pair.id, value);
      buttons.forEach((node, index) => node.setAttribute("aria-pressed", String(choices[index][0] === value)));
      updateProgress();
    });
    option.setAttribute("aria-pressed", String(votes.get(pair.id) === value));
    option.disabled = !!answer;
    return option;
  });
  reason.addEventListener("input", () => reasons.set(pair.id, reason.value));
  rating.append(...buttons);
  const reasonLabel = element("label", t("reason"));
  reasonLabel.append(reason);
  article.append(rating, reasonLabel);
}

function resultBlock(pair) {
  const results = element("section", undefined, "results note");
  results.setAttribute("aria-label", t("results"));
  for (const side of ["left", "right"]) {
    const result = answer.pairs[pair.id][side];
    const details = element("div");
    const cost = result.cny === null ? t("unknown") : `¥${result.cny.toFixed(4)}`;
    const moderation = result.moderation_failed ? "moderationBlocked" : "moderationNoRejection";
    details.append(element("strong", t(side)), element("p", result.label),
      element("p", t("cost", {cost})), element("p", t("retries", {count: result.retries})),
      element("p", t(review.dry_run ? "notRun" : moderation)));
    results.append(details);
  }
  return results;
}

function showArm() {
  byId("comparisons").querySelectorAll("video").forEach((video) => video.pause());
  const panels = [];
  for (const pair of review.pairs.filter((item) => item.arm === selectedArm)) {
    const article = element("article");
    article.append(element("h2", pair.description));
    const columns = element("div", undefined, "visuals");
    columns.append(element("span", t("left"), "side-title"), element("span", t("right"), "side-title"));
    article.append(columns);
    const rows = Math.max(pair.left.length, pair.right.length, 1);
    for (let row = 0; row < rows; row += 1) {
      const visual = element("div", undefined, "visuals");
      const left = mediaElement(pair.left[row], "left");
      const right = mediaElement(pair.right[row], "right");
      visual.append(left, right);
      article.append(visual);
      if (left.tagName === "VIDEO" || right.tagName === "VIDEO") article.append(playback(left, right));
    }
    ratingControls(pair, article);
    if (answer) article.append(resultBlock(pair));
    panels.push(article);
  }
  byId("comparisons").replaceChildren(...panels);
}

function render() {
  document.documentElement.lang = language;
  document.title = t("title");
  for (const [id, key] of Object.entries({title: "title", "language-label": "language", instructions: "instructions",
    "key-label-text": "keyFile", export: "download"})) byId(id).textContent = t(key);
  byId("preview").hidden = !review.dry_run;
  byId("preview").textContent = t("dryRun");
  const tabs = byId("tabs");
  tabs.setAttribute("aria-label", t("tabs"));
  const armTabs = [];
  for (const arm of new Set(review.pairs.map((pair) => pair.arm))) {
    const tab = button(arm, () => {
      selectedArm = arm;
      armTabs.forEach((node) => node.setAttribute("aria-pressed", String(node === tab)));
      showArm();
    });
    tab.setAttribute("aria-pressed", String(arm === selectedArm));
    armTabs.push(tab);
  }
  tabs.replaceChildren(...armTabs);
  showArm();
  updateProgress();
}

function validSide(result) {
  return result && typeof result.label === "string" && (result.cny === null || Number.isFinite(result.cny))
    && typeof result.moderation_failed === "boolean" && Number.isSafeInteger(result.retries) && result.retries >= 0;
}

byId("key-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (votes.size !== eligible || answer || !file) return;
  try {
    const key = JSON.parse(await file.text());
    if (key.review_id !== review.review_id || key.dry_run !== review.dry_run
        || !review.pairs.every(({id}) => validSide(key.pairs?.[id]?.left) && validSide(key.pairs?.[id]?.right))) {
      throw new Error();
    }
    answer = key;
    byId("reveal-status").hidden = true;
    showArm();
    updateProgress();
  } catch {
    byId("reveal-status").hidden = false;
    byId("reveal-status").textContent = t("invalidKey");
  } finally {
    event.target.value = "";
  }
});

byId("export").addEventListener("click", () => {
  if (votes.size !== eligible || !eligible) return;
  const ratings = review.pairs.map(({id, arm}) => ({id, arm, preference: votes.get(id) ?? null, reason: reasons.get(id) ?? null}));
  const record = {review_id: review.review_id, dry_run: review.dry_run, revealed: !!answer, ratings};
  const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], {type: "application/json"}));
  const download = element("a");
  download.href = url;
  download.download = "ab_ratings.json";
  download.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
});

const languageChoice = byId("language");
for (const [code, messages] of Object.entries(review.messages)) {
  const option = element("option", messages.languageName);
  option.value = code;
  languageChoice.append(option);
}
languageChoice.addEventListener("change", () => {
  language = languageChoice.value;
  byId("reveal-status").hidden = true;
  render();
});
render();
</script>
</body>
</html>
"""
