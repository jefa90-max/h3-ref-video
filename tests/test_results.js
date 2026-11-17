"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticDirectory = path.join(__dirname, "../workbench/static");

for (const [language, prefix] of [["en", "Segment"], ["zh-CN", "片段"]]) {
  test(`script review numbers segments from one (${language})`, () => {
    const messages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages", `${language}.json`), "utf8"));
    function element(tag, className = "", textContent = "") {
      return {
        tag, className, textContent, dataset: {}, children: [],
        append(...children) { this.children.push(...children); },
        replaceChildren(...children) { this.children = children; },
        addEventListener() {},
      };
    }
    const nodes = new Map();
    const select = (id) => {
      if (!nodes.has(id)) nodes.set(id, element("div"));
      return nodes.get(id);
    };
    const context = vm.createContext({
      state: { language }, $: select, element, renderPage: null,
      document: { addEventListener() {} },
      t: (key, values = {}) => messages[key].replace(/\{(\w+)\}/g, (_, name) => values[name]),
      initializeVideoSegments() {},
      loadLanguage: () => new Promise(() => {}),
    });
    for (const script of ["pagination.js", "results.js"]) {
      vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
    }
    const segments = [0, 1].map((idx) => ({ idx, start: idx * 5, end: (idx + 1) * 5, seconds: 5, shots: [] }));
    context.renderScriptPlan({ review: { kind: "script", version: "review", plan: { title: "Story", seconds: 10, segments } } });
    const summaries = select("script-plan").children.filter((node) => node.tag === "details").map((node) => node.children[0].textContent);
    assert.equal(summaries.length, 2);
    assert.ok(summaries[0].startsWith(`${prefix} 1`), summaries[0]);
    assert.ok(summaries[1].startsWith(`${prefix} 2`), summaries[1]);
    assert.deepEqual(segments.map((segment) => segment.idx), [0, 1]);
  });
}

for (const prefix of ["page", "segments"]) {
  test(`pending ${prefix} number survives polling after Tab to the jump button`, () => {
    const nodes = new Map();
    function select(id) {
      if (!nodes.has(id)) nodes.set(id, {
        dataset: {}, events: {}, value: "", validationMessage: "",
        addEventListener(name, callback) { this.events[name] = callback; },
        setCustomValidity(message) { this.validationMessage = message; },
        reportValidity() {},
      });
      return nodes.get(id);
    }
    const document = { activeElement: null };
    const destinations = [];
    const context = vm.createContext({ $: select, document, t: (key) => key });
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, "pagination.js"), "utf8"), context);
    context.initializePagination(prefix, (page) => {
      destinations.push(page);
      context.renderPagination(prefix, page, 3);
    });
    context.renderPagination(prefix, 1, 3);
    const input = select(`${prefix}-input`);
    input.value = "2";
    input.events.input();
    document.activeElement = select(`${prefix}-jump`);
    context.renderPagination(prefix, 1, 3);
    select(`${prefix}-jump-form`).events.submit({ preventDefault() {} });
    assert.deepEqual(destinations, [2], "Polling between Tab and Enter must preserve the requested destination");
    assert.equal(input.value, "2");
    select(`${prefix}-last`).events.click();
    assert.deepEqual(destinations, [2, 3]);
    assert.equal(input.value, "3", "An explicit page change updates the input");
  });
}

for (const language of ["en", "zh-CN"]) {
  for (const [url, moderationFailed] of [[null, true], [null, false], ["/previous-frame.png", true], ["/previous-frame.png", false]]) {
    test(`failed frame allows manual regeneration with required edits only for moderation (${language}, ${url}, ${moderationFailed})`, () => {
      const messages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages", `${language}.json`), "utf8"));
      const nodes = [];
      function element(tag, className = "", textContent = "") {
        const node = {
          tag, className, textContent, dataset: {}, children: [], events: {}, value: "",
          append(...children) { this.children.push(...children); },
          replaceChildren(...children) { this.children = children; },
          addEventListener(name, callback) { this.events[name] = callback; },
          setAttribute() {},
        };
        nodes.push(node);
        return node;
      }
      const ids = new Map();
      const select = (id) => {
        if (!ids.has(id)) ids.set(id, element("div"));
        return ids.get(id);
      };
      const state = { language };
      const context = vm.createContext({
        state, $: select, element,
        document: { addEventListener() {}, querySelectorAll: (selector) => nodes.filter((node) => node.className.split(" ").includes(selector.slice(1))) },
        t: (key, values = {}) => messages[key].replace(/\{(\w+)\}/g, (_, name) => values[name]),
        running: (job) => ["queued", "running"].includes(job?.status),
        initializeVideoSegments() {}, initializePagination() {}, loadLanguage: () => new Promise(() => {}),
      });
      for (const script of ["frame-references.js", "results.js"]) {
        vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
      }
      const frame = { key: "s1-1", segment: 1, shot: 1, prompt: "Rejected prompt", error: moderationFailed ? "content_policy_violation" : "upstream unavailable", moderation_failed: moderationFailed,
        references_editable: true, references: [], url };
      state.selectedJob = { id: "job", status: "failed", demo: false };
      const card = context.createStoryboardCard(frame, state.selectedJob, true);
      const editor = nodes.find((node) => node.tag === "textarea");
      const button = nodes.find((node) => node.className.split(" ").includes("regenerate-frame"));
      function descendants(node) { return [node, ...node.children.flatMap(descendants)]; }
      assert.ok(descendants(card).some((node) => node.textContent === messages[moderationFailed ? "frameModerationFailed" : "frameGenerationFailed"]));
      assert.equal(descendants(card).some((node) => node.textContent === messages.frameWaiting), false);
      if (url) assert.equal(descendants(card).find((node) => node.tag === "img").src, url);
      assert.equal(button.disabled, moderationFailed);
      editor.value = "A person stands in a quiet room.";
      editor.events.input();
      assert.equal(button.disabled, false, "An idle failed job must allow the edited frame to be generated");
      editor.value = " Rejected prompt ";
      editor.events.input();
      assert.equal(button.disabled, moderationFailed, "Only a moderation rejection requires a changed prompt");
      editor.value = "Another safe prompt";
      state.selectedJob.status = "running";
      editor.events.input();
      assert.equal(button.disabled, true);
      state.selectedJob.status = "succeeded";
      frame.task_pending = true;
      frame.references_editable = false;
      const pendingCard = context.createStoryboardCard(frame, state.selectedJob, true);
      assert.ok(descendants(pendingCard).some((node) => node.textContent === messages.frameTaskPending),
        "Unknown task status must point to recovery of the saved task");
      assert.equal(descendants(pendingCard).find((node) => node.className.split(" ").includes("regenerate-frame")).disabled, true);
      frame.submission_unknown = true;
      frame.task_pending = false;
      const unknownCard = context.createStoryboardCard(frame, state.selectedJob, true);
      assert.ok(descendants(unknownCard).some((node) => node.textContent === messages.frameSubmissionUnknown));
      assert.equal(descendants(unknownCard).find((node) => node.className.split(" ").includes("regenerate-frame")).disabled, true);
    });
  }
}

test("renderStoryboard preserves unchanged sibling card DOM nodes across poll updates", () => {
  const messages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages", "en.json"), "utf8"));
  function element(tag, className = "", textContent = "") {
    return {
      tag, className, textContent, dataset: {}, children: [], events: {}, value: "",
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      replaceChild(nextNode, oldNode) {
        const idx = this.children.indexOf(oldNode);
        if (idx >= 0) this.children[idx] = nextNode;
      },
      addEventListener(name, callback) { this.events[name] = callback; },
      setAttribute() {},
    };
  }
  const ids = new Map();
  const select = (id) => {
    if (!ids.has(id)) ids.set(id, element("div"));
    return ids.get(id);
  };
  const state = { language: "en" };
  const context = vm.createContext({
    state, $: select, element,
    document: { addEventListener() {}, querySelectorAll: () => [] },
    t: (key, values = {}) => (messages[key] || key).replace(/\{(\w+)\}/g, (_, name) => values[name] ?? ""),
    running: (job) => ["queued", "running"].includes(job?.status),
    initializeVideoSegments() {}, initializePagination() {}, renderPagination() {}, loadLanguage: () => new Promise(() => {}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  const f1 = { key: "s1-1", segment: 1, shot: 1, prompt: "Shot 1", references: [], references_editable: false, url: null };
  const f2 = { key: "s1-2", segment: 1, shot: 2, prompt: "Shot 2", references: [], references_editable: false, url: "/s1-2.png" };
  context.renderStoryboard({ id: "j1", status: "running", demo: false, review: { kind: "frames" }, character_references: [], storyboard: [f1, f2] });
  const initialCard1 = select("storyboard-cards").children[0];
  const initialCard2 = select("storyboard-cards").children[1];
  context.renderStoryboard({
    id: "j1", status: "running", demo: false, review: { kind: "frames" }, character_references: [],
    storyboard: [{ ...f1, url: "/s1-1.png" }, { ...f2 }],
  });
  assert.notEqual(select("storyboard-cards").children[0], initialCard1, "Updated frame card must re-render");
  assert.equal(select("storyboard-cards").children[1], initialCard2, "Unchanged sibling frame card DOM node must be preserved");
});

// Generated by /ship coverage audit
// Value: protects=results.js syncs carousel keyboard navigation, reconciles frame drafts, and recovers runJobAction on error; fails_when=ArrowRight inside textarea flips frames, applied frame edit leaves stale draft, or failed action skips refresh; why_new=test_results.js covered list cards and pagination only, not carousel keys, frameSubmissions, or runJobAction; seam=none; no_claim=real browser layout scrolling or Plyr WebMedia playback (node:vm DOM harness only)
test("carousel keyboard guards, frame draft reconciliation, tab auto-switch, and runJobAction error refresh", async () => {
  const messages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages", "en.json"), "utf8"));
  const nodes = [];
  const docEvents = {};
  function element(tag, className = "", textContent = "") {
    const node = {
      tag, className, textContent, dataset: {}, children: [], events: {}, attributes: {},
      value: "", hidden: false, disabled: false, open: false, scrollHeight: 0, scrollTop: 0, clientHeight: 0,
      append(...children) { for (const c of children) { c.parent = this; this.children.push(c); } },
      replaceChildren(...children) { for (const c of this.children) c.parent = null; this.children = []; this.append(...children); },
      replaceChild(nextNode, oldNode) {
        const idx = this.children.indexOf(oldNode);
        if (idx >= 0) { nextNode.parent = this; this.children[idx] = nextNode; }
      },
      insertBefore(child, ref) {
        const idx = this.children.indexOf(ref);
        child.parent = this;
        this.children.splice(idx < 0 ? this.children.length : idx, 0, child);
      },
      remove() {},
      addEventListener(name, callback) { this.events[name] = callback; },
      setAttribute(k, v) { this.attributes[k] = v; },
      removeAttribute(k) { delete this.attributes[k]; },
      closest(sel) {
        return sel.split(",").map((s) => s.trim()).includes(tag) ? this : this.parent?.closest?.(sel) || null;
      },
      click() { if (!this.disabled) return this.events.click?.(); },
      focus() {},
      scrollIntoView() {},
      setCustomValidity() {},
      reportValidity() {},
    };
    nodes.push(node);
    return node;
  }
  const ids = new Map();
  const select = (id) => {
    if (!ids.has(id)) ids.set(id, element("div"));
    return ids.get(id);
  };
  select("results-page").dataset.jobId = "j1";
  const state = { language: "en", messages };
  const posted = [];
  const errors = [];
  let refreshCount = 0;
  let releaseContinue = null;
  let failAssemble = false;

  let currentServerJob = {
    id: "j1", kind: "film", status: "succeeded", demo: false, operation: "frames", operation_id: "op-1",
    created_at: 1, output_dir: "/tmp/j1", logs: "", report: "", artifacts: [], progress: { frames: 2, clips: 0 },
    can_continue: true, review_needed: true, can_assemble: false, can_resume_video: true,
    review: { kind: "frames", total: 2, ready: true, complete: false, version: "rev-1" },
    character_references: [{ kind: "character", name: "Alice", path: "/a.png", url: "/c/0" }],
    storyboard: [
      { key: "s1-1", segment: 1, shot: 1, version: "v1", prompt: "Shot 1", url: "/s1-1.png", references_editable: true, references: [{ kind: "character", name: "Alice", path: "/a.png", url: "/r/0" }] },
      { key: "s1-2", segment: 1, shot: 2, version: "v1", prompt: "Shot 2", url: "/s1-2.png", references_editable: true, references: [] },
    ],
    segments: [], assembly: { ready: false, version: "asm-1" },
  };

  const context = vm.createContext({
    state, $: select, element, URL,
    window: { location: { href: "http://127.0.0.1:7860/jobs/j1" } },
    document: {
      activeElement: null,
      addEventListener(name, fn) { docEvents[name] = fn; },
      querySelectorAll(sel) {
        return nodes.filter((n) => sel.split(",").some((part) => n.className.split(" ").includes(part.trim().slice(1))));
      },
    },
    t: (key, values = {}) => (messages[key] || key).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? "")),
    running: (job) => ["queued", "running"].includes(job?.status),
    dateLabel: () => "",
    renderConfig() {},
    showError: (e) => errors.push(e instanceof Error ? e.message : String(e)),
    clearNotices() {},
    showMessage() {},
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/bootstrap") return { config: {}, jobs: [] };
      refreshCount += 1;
      return structuredClone(currentServerJob);
    },
    post: async (url, body) => {
      posted.push({ url, body: structuredClone(body) });
      if (url.endsWith("/continue") && releaseContinue) await releaseContinue;
      if (url.endsWith("/assemble") && failAssemble) {
        currentServerJob.assembly = { ready: true, version: "asm-2" };
        throw new Error("assembly_changed");
      }
      currentServerJob = {
        ...currentServerJob,
        status: "running",
        operation: url.endsWith("/continue") ? "pipeline" : "assemble",
        operation_id: "op-2",
      };
      return structuredClone(currentServerJob);
    },
    setTimeout: () => 1,
    clearTimeout() {},
    Plyr: function () { this.destroy = () => {}; this.pause = () => {}; },
  });

  for (const name of ["pagination.js", "frame-references.js", "video-segments.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, name), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  assert.equal(state.resultTab, "storyboard");

  // 1. Switch to carousel: ArrowRight inside textarea or IME composition is ignored; outside textarea advances frame
  select("storyboard-view-carousel").events.click();
  assert.equal(state.storyboardView, "carousel");
  assert.equal(state.frameIndex, 0);

  const textareaTarget = element("textarea");
  docEvents.keydown({ key: "ArrowRight", target: textareaTarget, preventDefault() {} });
  assert.equal(state.frameIndex, 0, "ArrowRight inside prompt textarea must not switch carousel frame");
  docEvents.keydown({ key: "ArrowRight", isComposing: true, target: element("div"), preventDefault() {} });
  assert.equal(state.frameIndex, 0, "ArrowRight during IME composition must not switch carousel frame");

  let prevented = false;
  docEvents.keydown({ key: "ArrowRight", target: element("div"), preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(state.frameIndex, 1);

  // 2. frameSubmissions clears matching applied draft on version bump while keeping newer unsaved draft on sibling
  state.frameDrafts.set("s1-1", "Edited Shot 1");
  state.frameSubmissions.set("s1-1", { prompt: "Edited Shot 1", character_reference_paths: ["/a.png"], version: "v1" });
  state.frameDrafts.set("s1-2", "Newer unsaved Shot 2");
  state.frameSubmissions.set("s1-2", { prompt: "Submitted Shot 2", character_reference_paths: [], version: "v1" });
  currentServerJob.storyboard[0] = { ...currentServerJob.storyboard[0], version: "v2", prompt: "Edited Shot 1" };
  currentServerJob.storyboard[1] = { ...currentServerJob.storyboard[1], version: "v2", prompt: "Submitted Shot 2" };
  await context.refreshJob();
  assert.equal(state.frameDrafts.has("s1-1"), false, "Matching applied frame draft must be cleared");
  assert.equal(state.frameSubmissions.has("s1-1"), false);
  assert.equal(state.frameDrafts.get("s1-2"), "Newer unsaved Shot 2", "Newer unsaved frame edit must survive prior submission");

  // 3. Double-clicking continue-job fires one POST and auto-switches resultTab to "segments" on new video operation_id
  let unblockContinue;
  releaseContinue = new Promise((r) => { unblockContinue = r; });
  const firstContinue = select("continue-job").events.click();
  await select("continue-job").events.click();
  unblockContinue();
  await firstContinue;
  assert.equal(posted.filter((p) => p.url.endsWith("/continue")).length, 1, "Double-click must send only one /continue POST");
  assert.equal(state.resultTab, "segments", "Starting pipeline operation must auto-switch to segments tab");

  // 4. Failed assemble action surfaces error and triggers refreshJob() to pick up updated server assembly version
  currentServerJob = {
    ...currentServerJob,
    status: "succeeded",
    can_assemble: true,
    review: { ...currentServerJob.review, complete: true },
    assembly: { ready: true, version: "asm-1" },
  };
  await context.refreshJob();
  const beforeRefreshes = refreshCount;
  failAssemble = true;
  await select("assemble-job").events.click();
  assert.equal(errors.at(-1), "assembly_changed");
  assert.equal(refreshCount, beforeRefreshes + 1, "Failed runJobAction must refresh server job snapshot");
  assert.equal(state.selectedJob.assembly.version, "asm-2");

  // 5. Prompt-only rewrite on clipless or partially-clipped film does not claim stageFinished or stageAwaitingAssembly
  currentServerJob = {
    ...currentServerJob,
    status: "succeeded",
    operation: "rewrite_prompts",
    review_needed: false,
    can_resume_pipeline: true,
    can_assemble: false,
    pending_assembly: false,
    assembly: { ready: false, pending: false, version: "asm-0" },
    segments: [
      { number: 1, start: 0, end: 8, seconds: 8, shots: 1, url: null, version: null, prompt: "p1", prompt_format: "source", prompt_version: "pv1" },
      { number: 2, start: 8, end: 16, seconds: 8, shots: 1, url: null, version: null, prompt: "p2", prompt_format: "source", prompt_version: "pv2" },
    ],
  };
  await context.refreshJob();
  assert.notEqual(select("stage-title").textContent, messages.stageFinished, "Clipless rewrite must not show Generation complete");

  currentServerJob = {
    ...currentServerJob,
    pending_assembly: true,
    assembly: { ready: false, pending: true, version: "asm-half" },
    segments: [
      { ...currentServerJob.segments[0], url: "/seg1.mp4", version: "v1" },
      currentServerJob.segments[1],
    ],
  };
  await context.refreshJob();
  assert.notEqual(select("stage-title").textContent, messages.stageAwaitingAssembly, "Partial-clip rewrite must not show ready to reassemble");
});

