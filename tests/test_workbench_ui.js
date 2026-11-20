"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticDirectory = path.join(__dirname, "../workbench/static");
const enMessages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/en.json"), "utf8"));

function makeDom() {
  const ids = new Map();
  const all = [];
  function element(tag, className = "", textContent = "") {
    const classes = new Set(className.split(/\s+/).filter(Boolean));
    const node = {
      tag, className, textContent, hidden: false, disabled: false, checked: false,
      value: "", name: "", dataset: {}, events: {}, children: [], attributes: {}, style: {},
      scrollHeight: 0, scrollTop: 0, clientHeight: 0,
      classList: {
        toggle(name, force) {
          const on = force === undefined ? !classes.has(name) : Boolean(force);
          if (on) classes.add(name); else classes.delete(name);
          node.className = [...classes].join(" ");
        },
        add(name) { classes.add(name); node.className = [...classes].join(" "); },
        remove(name) { classes.delete(name); node.className = [...classes].join(" "); },
        contains(name) { return classes.has(name); },
      },
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      addEventListener(type, fn) { this.events[type] = fn; },
      setAttribute(k, v) { this.attributes[k] = v; },
      removeAttribute(k) { delete this.attributes[k]; },
      scrollIntoView() {},
      focus() {},
    };
    all.push(node);
    return node;
  }
  function $(id) {
    if (!ids.has(id)) ids.set(id, element("div"));
    return ids.get(id);
  }
  return { ids, all, element, $ };
}

test("common.js switches dual-LLM providers, preserves drafts, and clears gemini key on save", async () => {
  const { $, ids } = makeDom();
  $("persist-config").checked = true;
  let postedPayload = null;
  class FakeFormData {
    constructor() {
      const entries = [];
      for (const id of ["rh-key", "apimart-key", "llm-provider", "llm-key", "gemini-key", "llm-composition-model", "llm-rewrite-model", "llm-base"]) {
        const el = ids.get(id);
        if (el && !el.disabled && el.name) entries.push([el.name, el.value]);
      }
      this._entries = entries;
    }
    [Symbol.iterator]() { return this._entries[Symbol.iterator](); }
  }
  $("rh-key").name = "RUNNINGHUB_API_KEY";
  $("apimart-key").name = "APIMART_API_KEY";
  $("llm-provider").name = "LLM_PROVIDER";
  $("llm-key").name = "LLM_API_KEY";
  $("gemini-key").name = "GEMINI_API_KEY";
  $("llm-composition-model").name = "LLM_COMPOSITION_MODEL";
  $("llm-rewrite-model").name = "LLM_REWRITE_MODEL";
  $("llm-base").name = "LLM_BASE_URL";

  const baseConfig = {
    keys: { RUNNINGHUB_API_KEY: true, APIMART_API_KEY: true, LLM_API_KEY: false, GEMINI_API_KEY: false },
    LLM_PROVIDER: "apimart",
    LLM_BASE_URL: "https://api.apimart.ai/v1",
    LLM_MODEL: "deepseek-v4-pro",
    llm_providers: {
      apimart: { provider: "apimart", base_url: "https://api.apimart.ai/v1", composition_model: "deepseek-v4-pro", rewrite_model: "deepseek-v4-pro" },
      gemini: { provider: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", composition_model: "gemini-flash-latest", rewrite_model: "gemini-flash-latest" },
    },
    ffmpeg: true, ffprobe: true, python: "3.12.0", saved_to_env: true,
  };

  const context = vm.createContext({
    document: {
      getElementById: $,
      createElement: (tag) => $(`anon-${Math.random()}-${tag}`),
      querySelectorAll: () => [],
      documentElement: { lang: "en" },
      title: "",
    },
    localStorage: { getItem: () => "en", setItem() {} },
    FormData: FakeFormData,
    fetch: async (url, opts) => {
      if (url === "/api/config") {
        postedPayload = JSON.parse(opts.body);
        return {
          ok: true,
          json: async () => ({
            ...baseConfig,
            LLM_PROVIDER: "gemini",
            keys: { ...baseConfig.keys, GEMINI_API_KEY: true },
            llm_providers: {
              ...baseConfig.llm_providers,
              gemini: { ...baseConfig.llm_providers.gemini, composition_model: postedPayload.GEMINI_COMPOSITION_MODEL, rewrite_model: postedPayload.GEMINI_REWRITE_MODEL },
            },
          }),
        };
      }
      return { ok: true, json: async () => enMessages };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "common.js"), "utf8"), context);
  vm.runInContext(`state.messages = ${JSON.stringify(enMessages)}; state.config = ${JSON.stringify(baseConfig)}; renderConfig(true);`, context);

  assert.equal($("llm-provider").value, "apimart");
  $("llm-composition-model").value = "unsaved-deepseek-draft";
  $("llm-provider").value = "gemini";
  await $("llm-provider").events.change();

  assert.equal($("llm-key").disabled, true);
  assert.equal($("llm-base").disabled, true);
  assert.equal($("gemini-key").disabled, false);
  assert.equal($("llm-composition-model").name, "GEMINI_COMPOSITION_MODEL");
  assert.equal($("llm-composition-model").value, "gemini-flash-latest");

  // Switch back to apimart: unsaved draft is preserved
  $("llm-provider").value = "apimart";
  await $("llm-provider").events.change();
  assert.equal($("llm-composition-model").value, "unsaved-deepseek-draft");

  // Switch to gemini and submit
  $("llm-provider").value = "gemini";
  await $("llm-provider").events.change();
  $("gemini-key").value = "gk-new-secret";
  $("llm-composition-model").value = "gemini-2.5-pro";
  $("llm-rewrite-model").value = "gemini-2.5-flash";
  await $("config-form").events.submit({ preventDefault() {}, target: $("config-form") });

  assert.equal(postedPayload.LLM_PROVIDER, "gemini");
  assert.equal(postedPayload.GEMINI_API_KEY, "gk-new-secret");
  assert.equal(postedPayload.GEMINI_COMPOSITION_MODEL, "gemini-2.5-pro");
  assert.equal(postedPayload.LLM_BASE_URL, undefined);
  assert.equal($("gemini-key").value, "");
  assert.equal($("config-summary").textContent, enMessages.configReady);
});

test("initializeResults starts language, bootstrap, and job requests concurrently", async () => {
  const { $, element } = makeDom();
  $("results-page").dataset.jobId = "job-42";
  const started = [];
  let releaseLang;
  const langGate = new Promise((resolve) => { releaseLang = resolve; });

  const context = vm.createContext({
    state: { language: "en", config: null, jobs: [] },
    $, element, renderPage: null,
    document: { body: { dataset: { jobId: "job-42" } }, addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener() {} },
    t: (k) => enMessages[k] || k,
    running: () => false,
    renderConfig() {},
    showError(e) { throw e; },
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    initializePagination() {},
    renderPagination() {},
    initializeVideoSegments() {},
    renderResultTabs() {},
    renderVideoSegments() {},
    pauseSegmentPlayers() {},
    destroySegmentPlayers() {},
    loadLanguage: async () => { started.push("lang"); await langGate; },
    request: async (url) => {
      started.push(url);
      if (url === "/api/bootstrap") return { config: {}, jobs: [] };
      return { id: "job-42", kind: "one", status: "succeeded", demo: true, artifacts: [], logs: "", report: "", progress: { frames: 0, clips: 1 } };
    },
    post: async () => ({}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(started, ["lang", "/api/bootstrap", "/api/jobs/job-42"]);
  releaseLang();
  await new Promise((r) => setImmediate(r));
});

test("create.js parses UTF-8 BOM JSON, debounces #film-json input, and drops stale responses", async () => {
  const { $, element } = makeDom();
  let refPosts = 0;
  const resolvers = [];
  const timers = [];
  const context = vm.createContext({
    state: { language: "en", config: { output_root: "/tmp/jobs", keys: {} }, jobs: [], ready: true },
    $, element, renderPage: null,
    document: { addEventListener() {}, querySelector: () => element("div"), querySelectorAll: () => [] },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
    running: () => false,
    busy: () => false,
    renderConfig() {},
    showError() {},
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    loadLanguage: async () => {},
    request: async () => ({ config: { output_root: "/tmp/jobs", keys: {} }, jobs: [] }),
    post: (url) => {
      if (url === "/api/film/references") {
        refPosts += 1;
        return new Promise((resolve) => resolvers.push(resolve));
      }
      return Promise.resolve({});
    },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "create.js"), "utf8"), context);

  // UTF-8 BOM JSON parsing
  $("film-json").value = '\uFEFF{"shots":[{"action":"walk"}]}';
  const parsed = context.parseFilm();
  assert.equal(parsed.shots[0].action, "walk");

  // Rapid input events on #film-json must debounce before posting /api/film/references
  $("film-json").value = '{"shots":[{"action":"one"}]}';
  $("film-json").events.input();
  $("film-json").value = '{"shots":[{"action":"two"}]}';
  $("film-json").events.input();
  $("film-json").value = '{"shots":[{"action":"three"}]}';
  $("film-json").events.input();

  assert.equal(refPosts, 0, "Input handler should debounce before posting");
  const activeTimers = timers.filter((item) => !item.cleared);
  assert.equal(activeTimers.length, 1);
  const firstRefresh = activeTimers[0].fn();
  assert.equal(refPosts, 1);

  // Trigger a second revision while the first request is still in flight
  $("film-json").value = '{"shots":[{"action":"newest"}]}';
  $("film-json").events.input();
  const secondTimers = timers.filter((item) => !item.cleared);
  const secondRefresh = secondTimers[secondTimers.length - 1].fn();
  assert.equal(refPosts, 2);

  // Resolve newer response first, then stale older response out of order
  resolvers[1]({ film: { shots: [{ action: "newest" }] }, slots: [{ id: "cast:0", kind: "cast", name: "Newest", description: "", required: true }] });
  await secondRefresh;
  resolvers[0]({ film: { shots: [{ action: "stale" }] }, slots: [{ id: "cast:0", kind: "cast", name: "Stale", description: "", required: true }] });
  await firstRefresh;
  assert.equal(context.state.film.shots[0].action, "newest");
  assert.equal(context.state.filmSlots[0].name, "Newest");
});

test("initializeResults renders config and schedules poll even when initial job snapshot transiently fails", async () => {
  const { $, element } = makeDom();
  $("results-page").dataset.jobId = "job-transient";
  let configRendered = false;
  const pollTimers = [];
  const context = vm.createContext({
    state: { language: "en", config: null, jobs: [] },
    $, element, renderPage: null,
    document: { addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener() {} },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
    running: () => false,
    renderConfig() { configRendered = true; },
    showError() {},
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    initializePagination() {},
    renderPagination() {},
    initializeVideoSegments() {},
    renderResultTabs() {},
    renderVideoSegments() {},
    pauseSegmentPlayers() {},
    destroySegmentPlayers() {},
    setTimeout: (fn, ms) => { pollTimers.push({ fn, ms }); return pollTimers.length; },
    clearTimeout() {},
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/bootstrap") return { config: { LLM_PROVIDER: "apimart" }, jobs: [] };
      throw new Error("segment_snapshot_changed");
    },
    post: async () => ({}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal(configRendered, true, "Config must still render when job snapshot transiently fails");
  assert.equal(pollTimers.length, 1, "Polling must be scheduled after transient snapshot failure");
});

test("results.js renders blocked resume hint instead of resumeHint when kind=one has can_resume_video=false", async () => {
  const { $, element } = makeDom();
  $("results-page").dataset.jobId = "job-one-blocked";
  const context = vm.createContext({
    state: { language: "en", config: {}, jobs: [] },
    $, element, renderPage: null,
    document: { addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener() {} },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
    running: () => false,
    renderConfig() {},
    showError() {},
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    initializePagination() {},
    renderPagination() {},
    initializeVideoSegments() {},
    renderResultTabs() {},
    renderVideoSegments() {},
    pauseSegmentPlayers() {},
    destroySegmentPlayers() {},
    setTimeout: () => 1,
    clearTimeout() {},
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/bootstrap") return { config: { LLM_PROVIDER: "apimart" }, jobs: [] };
      return {
        id: "job-one-blocked", kind: "one", status: "failed", demo: false, operation: "pipeline",
        can_resume_video: false, error: "Terminal failure", artifacts: [], logs: "", report: "",
        progress: { frames: 0, clips: 0 }, output_dir: "/tmp/one-blocked",
      };
    },
    post: async () => ({}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal($("resume-hint").textContent, enMessages.resumeBlockedHint);
  assert.equal($("resume-job").disabled, true);
});

test("results.js guards blockedSegment to video operations and stops polling on HTTP 404", async () => {
  const { $, element } = makeDom();
  $("results-page").dataset.jobId = "job-frame-blocked";
  const pollTimers = [];
  let mode = "frame-blocked";
  const context = vm.createContext({
    state: { language: "en", config: {}, jobs: [] },
    $, element, renderPage: null,
    document: { addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener() {} },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
    running: () => false,
    renderConfig() {},
    showError() {},
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    initializePagination() {},
    renderPagination() {},
    initializeVideoSegments() {},
    renderResultTabs() {},
    renderVideoSegments() {},
    pauseSegmentPlayers() {},
    destroySegmentPlayers() {},
    setTimeout: (fn, ms) => { pollTimers.push({ fn, ms }); return pollTimers.length; },
    clearTimeout() {},
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/bootstrap") return { config: { LLM_PROVIDER: "apimart" }, jobs: [] };
      if (mode === "not-found") {
        const err = new Error("Job not found");
        err.status = 404;
        throw err;
      }
      return {
        id: "job-frame-blocked", kind: "film", status: "failed", demo: false, operation: "frame",
        can_resume_video: false, error: "Moderation rejected", artifacts: [], logs: "", report: "",
        review: { kind: "frames", total: 1, ready: false, complete: false, version: "v1" },
        storyboard: [], segments: [{ number: 1, terminal_failed: true, submission_unknown: false }],
        progress: { frames: 1, clips: 0 }, output_dir: "/tmp/frame-blocked",
      };
    },
    post: async () => ({}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));
  assert.equal($("resume-hint").textContent, enMessages.resumeBlockedHint, "Frame failure must not show stale segmentTerminalFailed hint");

  mode = "not-found";
  pollTimers.length = 0;
  await context.refreshJob();
  assert.equal(pollTimers.length, 0, "HTTP 404 must stop rescheduling background polls");
});

test("style.css 68ch hint measure rule excludes frame-placeholder and segment-placeholder boxes", () => {
  const css = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");
  const measureLine = css.split("\n").find((line) => line.includes("max-width:68ch"));
  assert.ok(measureLine, "Expected a max-width:68ch rule in style.css");
  assert.match(measureLine, /\.frame-placeholder/, "68ch rule must exclude .frame-placeholder");
  assert.match(measureLine, /\.segment-placeholder/, "68ch rule must exclude .segment-placeholder");
});

// Generated by /ship coverage audit
// Value: protects=create.js validates one/film job-form submission, retains matching cast bindings, and enforces upload caps; fails_when=invalid seconds/seed or >9 images/2MB JSON submit, renamed cast keeps stale upload, or double-submit fires; why_new=test_workbench_ui.js covered parseFilm BOM and input debounce only, not job-form submit or binding retention; seam=none; no_claim=real browser FormData multipart encoding or window navigation (node:vm DOM harness only)
test("create.js validates job-form submission, retains matching cast bindings, and enforces upload caps", async () => {
  const { $, element } = makeDom();
  const errors = [];
  const posted = [];
  const navigated = [];
  let uploadCalls = 0;
  let releaseSubmit = null;
  $("output-dir").value = "/tmp/out-root";
  $("frames").value = "shot";
  $("aspect").value = "9:16";

  const context = vm.createContext({
    state: { language: "en", config: { output_root: "/tmp/out-root", keys: {} }, jobs: [], ready: true },
    $, element, renderPage: null,
    FormData: class { append() {} },
    window: { location: { assign: (url) => navigated.push(url) } },
    document: { addEventListener() {}, querySelector: () => element("div"), querySelectorAll: () => [] },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => String(v[n] ?? "")),
    running: () => false,
    busy: () => false,
    renderConfig() {},
    showError: (e) => errors.push(typeof e?.message === "string" ? e.message : String(e)),
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/uploads") {
        uploadCalls += 1;
        return { files: [{ id: "u1.png", name: "a.png", path: "/tmp/u1.png", url: "/api/uploads/u1.png" }] };
      }
      return { config: { output_root: "/tmp/out-root", keys: {} }, jobs: [] };
    },
    post: async (url, body) => {
      posted.push({ url, body: structuredClone(body) });
      if (url === "/api/film/references") {
        const cast = body.film.cast || [];
        return {
          film: body.film,
          slots: cast.map((c, idx) => ({ id: `cast:${idx}`, kind: "cast", name: c.name, description: "", required: true })),
        };
      }
      if (url === "/api/jobs") {
        if (releaseSubmit) await releaseSubmit;
        return { id: "job/new 1" };
      }
      return {};
    },
    setTimeout: () => 1,
    clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "create.js"), "utf8"), context);
  await new Promise((r) => setImmediate(r));

  // 1. Film reference slot retention drops renamed cast slots and blocks unbound submit
  $("film-json").value = JSON.stringify({ cast: [{ name: "Alice" }, { name: "Bob" }], shots: [{ action: "walk" }] });
  await context.refreshFilmReferences();
  context.state.filmBindings.set("cast:0", { name: "Alice", file: { id: "alice.png", name: "a.png", url: "/u/a" } });
  context.state.filmBindings.set("cast:1", { name: "Bob", file: { id: "bob.png", name: "b.png", url: "/u/b" } });
  $("film-json").value = JSON.stringify({ cast: [{ name: "Alice" }, { name: "Charlie" }], shots: [{ action: "walk" }] });
  await context.refreshFilmReferences();
  assert.equal(context.state.filmBindings.has("cast:0"), true, "Unchanged cast slot binding must be retained");
  assert.equal(context.state.filmBindings.has("cast:1"), false, "Renamed cast slot binding must be invalidated");

  await $("job-form").events.submit({ preventDefault() {} });
  assert.equal(posted.filter((p) => p.url === "/api/jobs").length, 0, "Missing required film binding must block /api/jobs");

  // 2. Oversized >2MB film JSON import and >9 one-mode image uploads are rejected client-side
  errors.length = 0;
  await $("film-import").events.change({ target: { files: [{ name: "huge.json", size: 2 * 1024 * 1024 + 1, text: async () => "{}" }], value: "huge.json" } });
  assert.equal(errors.at(-1), enMessages.jsonTooLarge);

  context.switchWorkflow("one");
  context.state.images = Array.from({ length: 8 }, (_, idx) => ({ id: `${idx}.png`, name: `${idx}.png`, path: `/p/${idx}.png`, url: `/u/${idx}` }));
  await $("one-upload").events.change({ target: { files: [{ name: "9.png" }, { name: "10.png" }], value: "x" } });
  assert.equal(uploadCalls, 0, ">9 reference images must be rejected before calling /api/uploads");

  // 3. One-mode seconds/seed validation, seed=0 preservation, and double-submit prevention
  $("prompt").value = "A cat walks";
  $("seconds").value = "4";
  errors.length = 0;
  await $("job-form").events.submit({ preventDefault() {} });
  assert.equal(posted.filter((p) => p.url === "/api/jobs").length, 0);

  $("seconds").value = "10";
  $("seed").value = String(Number.MAX_SAFE_INTEGER + 1);
  await $("job-form").events.submit({ preventDefault() {} });
  assert.equal(posted.filter((p) => p.url === "/api/jobs").length, 0);

  $("seed").value = "0";
  let unblock;
  releaseSubmit = new Promise((r) => { unblock = r; });
  const firstSubmit = $("job-form").events.submit({ preventDefault() {} });
  await $("job-form").events.submit({ preventDefault() {} });
  unblock();
  await firstSubmit;
  const jobPosts = posted.filter((p) => p.url === "/api/jobs");
  assert.equal(jobPosts.length, 1, "Rapid double-submit must send only one POST /api/jobs");
  assert.equal(jobPosts[0].body.seed, 0, "Explicit seed=0 must not coerce to null");
  assert.equal(jobPosts[0].body.operation, "pipeline");
  assert.equal(navigated[0], "/jobs/job%2Fnew%201");

  // 4. Film-mode bound submit forces operation="frames" and maps reference_bindings
  releaseSubmit = null;
  context.switchWorkflow("film");
  $("film-json").value = JSON.stringify({ cast: [{ name: "Alice" }, { name: "Charlie" }], shots: [{ action: "walk" }] });
  await context.refreshFilmReferences();
  context.state.filmBindings.set("cast:0", { name: "Alice", file: { id: "alice.png", name: "a.png", url: "/u/a" } });
  context.state.filmBindings.set("cast:1", { name: "Charlie", file: { id: "charlie.png", name: "c.png", url: "/u/c" } });
  await $("job-form").events.submit({ preventDefault() {} });
  const filmPost = posted.filter((p) => p.url === "/api/jobs").at(-1);
  assert.equal(filmPost.body.operation, "frames");
  assert.deepEqual(filmPost.body.reference_bindings, { "cast:0": "alice.png", "cast:1": "charlie.png" });
});

test("create.js supports optimize_storyboard toggle and merging/splitting multi-character reference groups", async () => {
  const { $, element } = makeDom();
  const posted = [];
  let uploadCounter = 0;
  $("output-dir").value = "/tmp/out-root";
  $("frames").value = "shot";

  const context = vm.createContext({
    state: { language: "en", config: { output_root: "/tmp/out-root", keys: {} }, jobs: [], ready: true },
    $, element, renderPage: null,
    FormData: class { append() {} },
    window: { location: { assign() {} } },
    document: { addEventListener() {}, querySelector: () => element("div"), querySelectorAll: () => [] },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => String(v[n] ?? "")),
    running: () => false,
    busy: () => false,
    renderConfig() {},
    showError(e) { throw e; },
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    loadLanguage: async () => {},
    request: async (url) => {
      if (url === "/api/uploads") {
        uploadCounter += 1;
        return { files: [{ id: `group-${uploadCounter}.png`, name: `g${uploadCounter}.png`, path: `/tmp/g${uploadCounter}.png`, url: `/u/g${uploadCounter}` }] };
      }
      return { config: { output_root: "/tmp/out-root", keys: {} }, jobs: [] };
    },
    post: async (url, body) => {
      posted.push({ url, body: structuredClone(body) });
      if (url === "/api/film/references") {
        const cast = body.film.cast || [];
        return {
          film: body.film,
          slots: cast.map((c, idx) => ({ id: `cast:${idx}`, kind: "cast", name: c.name, description: c.look || "", required: true })),
        };
      }
      return { id: "job-grouped" };
    },
    setTimeout: () => 1,
    clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "create.js"), "utf8"), context);
  await new Promise((r) => setImmediate(r));

  // Default optimize_storyboard is checked; switching frames to "none" disables it
  assert.equal($("optimize-storyboard").checked, true);
  $("frames").value = "none";
  $("frames").events.change();
  assert.equal($("optimize-storyboard").disabled, true);
  $("frames").value = "shot";
  $("frames").events.change();
  assert.equal($("optimize-storyboard").disabled, false);
  $("optimize-storyboard").checked = false;
  $("optimize-storyboard").events.change();

  // Load 3 characters, merge cast:0 and cast:2 into one group card, upload once for both
  $("film-json").value = JSON.stringify({
    cast: [{ name: "Queen", look: "gold crown" }, { name: "Monk", look: "red robe" }, { name: "Monkey", look: "golden armor" }],
    shots: [{ action: "Queen and Monkey talk" }],
  });
  await context.refreshFilmReferences();

  function allDescendants(node) {
    return [node, ...(node.children || []).flatMap(allDescendants)];
  }
  let tree = allDescendants($("film-reference-list"));
  const checkboxes = tree.filter((n) => n.className.includes("film-cast-select"));
  assert.equal(checkboxes.length, 3);
  checkboxes[0].checked = true;
  checkboxes[0].events.change();
  checkboxes[2].checked = true;
  checkboxes[2].events.change();

  tree = allDescendants($("film-reference-list"));
  const mergeBtn = tree.find((n) => n.className.includes("merge-cast-button"));
  assert.ok(mergeBtn && !mergeBtn.disabled);
  mergeBtn.events.click();

  // Now there is 1 grouped card ("Queen / Monkey") + 1 solo card ("Monk") = 2 upload inputs
  tree = allDescendants($("film-reference-list"));
  assert.equal(tree.filter((n) => n.className.includes("film-cast-select")).length, 0, "Lone remaining ungrouped character must not render dead-end merge checkbox");
  assert.equal(tree.find((n) => n.className.includes("merge-cast-button")), undefined, "Merge button must hide when <2 ungrouped characters remain");
  const uploads = tree.filter((n) => n.className.includes("film-reference-upload"));
  assert.equal(uploads.length, 2);
  uploads[0].files = [{ name: "queen-monkey.png" }];
  await uploads[0].events.change();
  uploads[1].files = [{ name: "monk.png" }];
  await uploads[1].events.change();

  await $("job-form").events.submit({ preventDefault() {} });
  const jobPost = posted.filter((p) => p.url === "/api/jobs").at(-1);
  assert.equal(jobPost.body.optimize_storyboard, false);
  assert.deepEqual(jobPost.body.reference_bindings, {
    "cast:0": "group-1.png",
    "cast:2": "group-1.png",
    "cast:1": "group-2.png",
  });
  assert.deepEqual(jobPost.body.reference_groups, [["cast:0", "cast:2"]]);

  // Splitting the group restores 3 individual cards
  tree = allDescendants($("film-reference-list"));
  const splitBtn = tree.find((n) => n.className.includes("film-group-split"));
  assert.ok(splitBtn);
  splitBtn.events.click();
  tree = allDescendants($("film-reference-list"));
  assert.equal(tree.filter((n) => n.className.includes("film-reference-upload")).length, 3);

  // Partial upload on only 1 of 2 selected roles must NOT silently bind that solo image as a combined group sheet
  context.state.filmBindings.clear();
  context.renderFilmReferences();
  tree = allDescendants($("film-reference-list"));
  const soloUploads = tree.filter((n) => n.className.includes("film-reference-upload"));
  soloUploads[0].files = [{ name: "queen-solo.png" }];
  await soloUploads[0].events.change();
  assert.equal(context.state.filmBindings.has("cast:0"), true);
  assert.equal(context.state.filmBindings.has("cast:1"), false);

  tree = allDescendants($("film-reference-list"));
  const soloChecks = tree.filter((n) => n.className.includes("film-cast-select"));
  soloChecks[0].checked = true;
  soloChecks[0].events.change();
  soloChecks[1].checked = true;
  soloChecks[1].events.change();
  tree = allDescendants($("film-reference-list"));
  tree.find((n) => n.className.includes("merge-cast-button")).events.click();
  assert.equal(context.state.filmBindings.has("cast:0"), false, "Solo image must not carry over to newly merged multi-role group");
  assert.equal(context.state.filmBindings.has("cast:1"), false);

  // Upload a 2-role group sheet for cast:0 + cast:1, then rename cast:1 in JSON -> dissolved group must clear multi-role sheet from cast:0
  tree = allDescendants($("film-reference-list"));
  const newGroupUploads = tree.filter((n) => n.className.includes("film-reference-upload"));
  newGroupUploads[0].files = [{ name: "queen-monk-sheet.png" }];
  await newGroupUploads[0].events.change();
  assert.equal(context.state.filmBindings.has("cast:0"), true);
  $("film-json").value = JSON.stringify({
    cast: [{ name: "Queen", look: "gold crown" }, { name: "NewMonk", look: "red robe" }, { name: "Monkey", look: "golden armor" }],
    shots: [{ action: "Queen talks" }],
  });
  await context.refreshFilmReferences();
  assert.equal(context.state.filmGroups.length, 0);
  assert.equal(context.state.filmBindings.has("cast:0"), false, "Dissolved 2-role group sheet must not stay bound to the surviving solo role");

  // Toggling a cast merge checkbox updates merge button in place without destroying/recreating card DOM
  tree = allDescendants($("film-reference-list"));
  const firstCheck = tree.find((n) => n.className.includes("film-cast-select"));
  firstCheck.checked = true;
  firstCheck.events.change();
  assert.equal(allDescendants($("film-reference-list")).includes(firstCheck), true, "Checkbox toggle must not tear down card DOM");

  // 9-image limit hint accounts for frames="first" (+2 frame + prev on seg 2+) and per-segment cast subsets in "shot" mode
  const sevenCast = Array.from({ length: 7 }, (_, i) => ({ name: `Role${i + 1}`, look: "look" }));
  $("film-json").value = JSON.stringify({
    board: "board.png",
    cast: sevenCast,
    shots: [
      { start: 0, end: 10, action: "全员站立" },
      { start: 10, end: 20, action: "全员交谈" },
    ],
  });
  context.post = async (url, body) => {
    if (url === "/api/film/references") {
      return {
        film: { ...body.film, board: true },
        slots: [
          ...body.film.cast.map((c, idx) => ({ id: `cast:${idx}`, kind: "cast", name: c.name, description: "", required: true })),
          { id: "board", kind: "board", name: "", description: "", required: true },
        ],
      };
    }
    return {};
  };
  $("frames").value = "shot";
  await context.refreshFilmReferences();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    false,
    "7 cast + board + 1 shot in shot mode (9 refs) must not warn",
  );
  $("frames").value = "first";
  $("frames").events.change();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    true,
    "7 cast + board + frame + prev in first mode on seg 2 (10 refs) must warn before submit",
  );
});

test("create.js does not fall back to all cast on characterless single-shot scenery frames", async () => {
  const { $, element } = makeDom();
  $("output-dir").value = "/tmp/out-root";
  $("frames").value = "shot";

  const nineCast = Array.from({ length: 9 }, (_, i) => ({ name: `Role${i + 1}`, look: "look" }));
  const context = vm.createContext({
    state: { language: "en", config: { output_root: "/tmp/out-root", keys: {} }, jobs: [], ready: true },
    $, element, renderPage: null,
    FormData: class { append() {} },
    window: { location: { assign() {} } },
    document: { addEventListener() {}, querySelector: () => element("div"), querySelectorAll: () => [] },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => String(v[n] ?? "")),
    running: () => false,
    busy: () => false,
    renderConfig() {},
    showError(e) { throw e; },
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    loadLanguage: async () => {},
    request: async () => ({ config: { output_root: "/tmp/out-root", keys: {} }, jobs: [] }),
    post: async (url, body) => {
      if (url === "/api/film/references") {
        return {
          film: { ...body.film, board: true },
          slots: [
            ...body.film.cast.map((c, idx) => ({ id: `cast:${idx}`, kind: "cast", name: c.name, description: "", required: true })),
            { id: "board", kind: "board", name: "", description: "", required: true },
          ],
        };
      }
      return {};
    },
    setTimeout: () => 1,
    clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "create.js"), "utf8"), context);
  await new Promise((r) => setImmediate(r));

  function allDescendants(node) {
    return [node, ...(node.children || []).flatMap(allDescendants)];
  }

  // Seg 1 starts with a characterless scenery shot, followed by a 3-role shot; Seg 2 has the remaining 6 roles.
  // Per-shot frame references never exceed 1 (board) + 6 (roles) = 7, and segment refs never exceed 1 + 6 + 2 = 9.
  $("film-json").value = JSON.stringify({
    board: "board.png",
    cast: nineCast,
    shots: [
      { start: 0, end: 5, action: "空山新雨后，远景云雾缭绕" },
      { start: 5, end: 12, action: "Role1、Role2 与 Role3 沿着石阶上山" },
      { start: 12, end: 24, action: "Role4、Role5、Role6、Role7、Role8、Role9 在山门前等候" },
    ],
  });
  $("frames").value = "shot";
  await context.refreshFilmReferences();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    false,
    "Characterless scenery shot in shot mode must use [] (board only = 1 ref), not fall back to all 9 cast (10 refs)",
  );

  $("frames").value = "first";
  $("frames").events.change();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    false,
    "Characterless opening shot (segShots[0]) in first mode must not fall back to all 9 cast",
  );

  // Conversely, when an entire segment is characterless, segment-level refs still fall back to all cast (matching film.refs)
  $("film-json").value = JSON.stringify({
    board: "board.png",
    cast: nineCast,
    shots: [{ start: 0, end: 8, action: "空山新雨后，远景云雾缭绕" }],
  });
  await context.refreshFilmReferences();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    true,
    "Whole-segment characterless fallbackAll=true must still count all 9 cast + board + first frame (11 > 9)",
  );

  // red-team#1: In shot mode, 9 cast sheets + board + group-word shot ('众人') drops board and omits frame slots (total 9 <= 9),
  // matching film.refs() so create.js must NOT show a false over-limit error.
  $("frames").value = "shot";
  $("film-json").value = JSON.stringify({
    board: "board.png",
    cast: nineCast,
    shots: [{ start: 0, end: 6, action: "众人在议事厅集合商议" }],
  });
  await context.refreshFilmReferences();
  assert.equal(
    allDescendants($("film-reference-list")).some((n) => n.className.includes("field-error")),
    false,
    "9 cast sheets + board in shot mode drops board and omits frames (9 <= 9), matching film.refs",
  );

  // specialist-batched#1: Malformed non-array shots ({}) and non-array aliases ({} / string) during import/typing must NOT throw TypeError or lock #film-json
  await $("film-import").events.change({
    target: {
      files: [{
        name: "partial.json",
        size: 120,
        text: async () => JSON.stringify({
          cast: [{ name: "Role1", aliases: { bad: 1 } }, { name: "Role2", aliases: "not-array" }],
          shots: { not: "an-array" },
        }),
      }],
      value: "partial.json",
    },
  });
  assert.equal($("film-json").readOnly, false, "#film-json must unlock cleanly even when shots/aliases are malformed");
  assert.equal($("film-import").disabled, false, "#film-import must unlock cleanly even when shots/aliases are malformed");
  assert.ok($("film-reference-list").children.length >= 2, "Cast upload cards must still render when shots/aliases are malformed");
});

test("common.js switches VIDEO_PROVIDER to comfyui, computes configReady, and serializes COLAB_AUTO_START", async () => {
  const { $, ids } = makeDom();
  $("persist-config").checked = true;
  let postedPayload = null;
  const fieldIds = [
    "video-provider", "rh-key", "comfy-url", "comfy-key", "comfy-preset", "comfy-resolution", "comfy-upscale", "colab-gpu", "colab-auto-start",
    "apimart-key", "llm-provider", "llm-key", "gemini-key", "llm-composition-model", "llm-rewrite-model", "llm-base",
  ];
  class FakeFormData {
    constructor() {
      const entries = [];
      for (const id of fieldIds) {
        const el = ids.get(id);
        if (el && !el.disabled && el.name) entries.push([el.name, el.value]);
      }
      this._entries = entries;
    }
    [Symbol.iterator]() { return this._entries[Symbol.iterator](); }
  }
  $("video-provider").name = "VIDEO_PROVIDER";
  $("rh-key").name = "RUNNINGHUB_API_KEY";
  $("comfy-url").name = "COMFYUI_URL";
  $("comfy-key").name = "COMFYUI_API_KEY";
  $("comfy-preset").name = "COMFYUI_PRESET";
  $("comfy-resolution").name = "COMFYUI_RESOLUTION";
  $("comfy-upscale").name = "COMFYUI_UPSCALE";
  $("colab-gpu").name = "COLAB_GPU";
  $("colab-auto-start").name = "COLAB_AUTO_START";
  $("apimart-key").name = "APIMART_API_KEY";
  $("llm-provider").name = "LLM_PROVIDER";
  $("llm-key").name = "LLM_API_KEY";
  $("gemini-key").name = "GEMINI_API_KEY";
  $("llm-composition-model").name = "LLM_COMPOSITION_MODEL";
  $("llm-rewrite-model").name = "LLM_REWRITE_MODEL";
  $("llm-base").name = "LLM_BASE_URL";

  const comfyConfig = {
    keys: { RUNNINGHUB_API_KEY: false, COMFYUI_API_KEY: false, APIMART_API_KEY: true, LLM_API_KEY: false, GEMINI_API_KEY: false },
    VIDEO_PROVIDER: "comfyui",
    COMFYUI_URL: "https://demo.ngrok-free.dev",
    COMFYUI_PRESET: "4step",
    COMFYUI_RESOLUTION: "768p",
    COMFYUI_UPSCALE: "0",
    COLAB_GPU: "G4",
    COLAB_AUTO_START: "0",
    comfy_requires_key: true,
    LLM_PROVIDER: "apimart",
    LLM_BASE_URL: "https://api.apimart.ai/v1",
    LLM_MODEL: "deepseek-v4-pro",
    llm_providers: {
      apimart: { provider: "apimart", base_url: "https://api.apimart.ai/v1", composition_model: "deepseek-v4-pro", rewrite_model: "deepseek-v4-pro", key_names: ["LLM_API_KEY", "APIMART_API_KEY"] },
      gemini: { provider: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", composition_model: "gemini-flash-latest", rewrite_model: "gemini-flash-latest", key_names: ["GEMINI_API_KEY"] },
    },
    ffmpeg: true, ffprobe: true, python: "3.12.0", saved_to_env: true,
  };

  const context = vm.createContext({
    document: {
      getElementById: $,
      createElement: (tag) => $(`anon-${Math.random()}-${tag}`),
      querySelectorAll: () => [],
      documentElement: { lang: "en" },
      title: "",
    },
    localStorage: { getItem: () => "en", setItem() {} },
    FormData: FakeFormData,
    fetch: async (url, opts) => {
      if (url === "/api/runtime/status") {
        return { ok: true, json: async () => ({ state: "offline", gpu: comfyConfig.COLAB_GPU, cli_available: true, logs: [] }) };
      }
      if (url === "/api/config") {
        postedPayload = JSON.parse(opts.body);
        return {
          ok: true,
          json: async () => ({
            ...comfyConfig,
            COMFYUI_PRESET: postedPayload.COMFYUI_PRESET,
            COMFYUI_RESOLUTION: postedPayload.COMFYUI_RESOLUTION,
            COMFYUI_UPSCALE: postedPayload.COMFYUI_UPSCALE,
            COLAB_GPU: postedPayload.COLAB_GPU,
            COLAB_AUTO_START: postedPayload.COLAB_AUTO_START,
            keys: { ...comfyConfig.keys, COMFYUI_API_KEY: true },
          }),
        };
      }
      return { ok: true, json: async () => enMessages };
    },
  });
  const freshMessages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/en.json"), "utf8"));
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "common.js"), "utf8"), context);
  vm.runInContext(`state.messages = ${JSON.stringify(freshMessages)}; state.config = ${JSON.stringify(comfyConfig)}; renderConfig(true);`, context);
  await new Promise((r) => setImmediate(r));

  // Missing COMFYUI_API_KEY on remote HTTPS tunnel -> configIncomplete
  assert.equal($("config-summary").textContent, freshMessages.configIncomplete);
  assert.equal($("rh-key").disabled, true);
  assert.equal($("comfy-url").disabled, false);
  assert.equal($("comfy-resolution").disabled, false);
  assert.equal($("comfy-resolution").value, "768p");
  assert.equal($("comfy-upscale").disabled, false);
  assert.equal($("comfy-upscale").value, "0");
  assert.equal($("colab-gpu").disabled, false);
  assert.equal($("colab-gpu").value, "G4");
  assert.equal($("runtime-card").hidden, false);
  assert.ok($("runtime-start").textContent.includes("G4"));

  // Preset copy reflects EasyCache default without hardcoding a single resolution, and resolution/GPU options exist in both locales
  const zhFresh = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/zh-CN.json"), "utf8"));
  assert.ok(freshMessages.comfyPresetFull20.includes("EasyCache") && !freshMessages.comfyPresetFull20.includes("544p"));
  assert.ok(zhFresh.comfyPresetFull20.includes("EasyCache") && !zhFresh.comfyPresetFull20.includes("544p"));
  assert.ok(freshMessages.comfyResolution768p.includes("768×1344") && zhFresh.comfyResolution768p.includes("768×1344"));
  assert.ok(freshMessages.comfyResolution544p.includes("544×960") && zhFresh.comfyResolution544p.includes("544×960"));

  // Submit COMFYUI_API_KEY, 8step preset, 544p resolution, COMFYUI_UPSCALE = "1", COLAB_GPU = "H100", and COLAB_AUTO_START = "0"
  $("comfy-key").value = "ck-secret-42";
  $("comfy-preset").value = "8step";
  $("comfy-resolution").value = "544p";
  $("comfy-upscale").value = "1";
  $("colab-gpu").value = "H100";
  $("colab-auto-start").value = "0";
  await $("config-form").events.submit({ preventDefault() {}, target: $("config-form") });
  await new Promise((r) => setImmediate(r));

  assert.equal(postedPayload.VIDEO_PROVIDER, "comfyui");
  assert.equal(postedPayload.COMFYUI_API_KEY, "ck-secret-42");
  assert.equal(postedPayload.COMFYUI_PRESET, "8step");
  assert.equal(postedPayload.COMFYUI_RESOLUTION, "544p");
  assert.equal(postedPayload.COMFYUI_UPSCALE, "1");
  assert.equal(postedPayload.COLAB_GPU, "H100");
  assert.equal(postedPayload.COLAB_AUTO_START, "0");
  assert.equal($("comfy-key").value, "");
  assert.equal($("config-summary").textContent, freshMessages.configReady);
});

test("common.js renders ComfyUI/Colab runtime card states, Drive auth ack, and two-step stop confirmation", async () => {
  const { $ } = makeDom();
  const calls = [];
  let currentStatus = {
    state: "awaiting_drive_auth",
    cli_available: true,
    auth_url: "https://accounts.google.com/o/oauth2/auth?client_id=test",
    comfyui_url: "https://demo.ngrok-free.dev",
    session: "comfy-a100",
    devices: [],
    logs: ["Waiting for Google Drive consent"],
  };

  const context = vm.createContext({
    document: {
      getElementById: $,
      createElement: (tag) => $(`anon-${Math.random()}-${tag}`),
      querySelectorAll: () => [],
      documentElement: { lang: "en" },
      title: "",
    },
    localStorage: { getItem: () => "en", setItem() {} },
    FormData: class { *[Symbol.iterator]() {} },
    fetch: async (url, opts) => {
      calls.push({ url, body: opts?.body ? JSON.parse(opts.body) : null });
      if (url === "/api/runtime/status") {
        return { ok: true, json: async () => currentStatus };
      }
      if (url === "/api/runtime/drive-ack") {
        currentStatus = {
          state: "online",
          cli_available: true,
          comfyui_url: "https://demo.ngrok-free.dev",
          session: "comfy-a100",
          devices: [{ name: "NVIDIA A100-SXM4-80GB" }],
          logs: ["ComfyUI online"],
        };
        return { ok: true, json: async () => ({ accepted: true, status: currentStatus }) };
      }
      if (url === "/api/runtime/stop") {
        const req = JSON.parse(opts.body);
        if (!req.force) {
          return {
            ok: true,
            json: async () => ({
              stopped: false,
              confirmation_required: true,
              local_busy: true,
              remote_queue: 1,
              status: currentStatus,
            }),
          };
        }
        currentStatus = { state: "offline", cli_available: true, comfyui_url: "https://demo.ngrok-free.dev", logs: [] };
        return { ok: true, json: async () => ({ stopped: true, confirmation_required: false, status: currentStatus }) };
      }
      return { ok: true, json: async () => enMessages };
    },
  });
  const freshMessages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/en.json"), "utf8"));
  const comfyConfig = {
    keys: { RUNNINGHUB_API_KEY: false, COMFYUI_API_KEY: true, APIMART_API_KEY: true, LLM_API_KEY: false, GEMINI_API_KEY: false },
    VIDEO_PROVIDER: "comfyui",
    COMFYUI_URL: "https://demo.ngrok-free.dev",
    COMFYUI_PRESET: "4step",
    COMFYUI_UPSCALE: "1",
    COLAB_AUTO_START: "0",
    comfy_requires_key: true,
    LLM_PROVIDER: "apimart",
    LLM_BASE_URL: "https://api.apimart.ai/v1",
    LLM_MODEL: "deepseek-v4-pro",
    llm_providers: {
      apimart: { provider: "apimart", base_url: "https://api.apimart.ai/v1", composition_model: "deepseek-v4-pro", rewrite_model: "deepseek-v4-pro", key_names: ["LLM_API_KEY", "APIMART_API_KEY"] },
      gemini: { provider: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", composition_model: "gemini-flash-latest", rewrite_model: "gemini-flash-latest", key_names: ["GEMINI_API_KEY"] },
    },
    ffmpeg: true, ffprobe: true, python: "3.12.0", saved_to_env: true,
  };
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "common.js"), "utf8"), context);
  vm.runInContext(`state.messages = ${JSON.stringify(freshMessages)}; state.config = ${JSON.stringify(comfyConfig)}; renderConfig(true);`, context);
  await new Promise((r) => setImmediate(r));

  assert.equal($("runtime-drive-box").hidden, false);
  assert.equal($("runtime-drive-link").href, "https://accounts.google.com/o/oauth2/auth?client_id=test");

  await $("runtime-drive-ack").events.click();
  assert.equal($("runtime-drive-box").hidden, true);
  assert.ok($("runtime-detail").textContent.includes("A100"));

  // First stop click requires secondary confirmation
  await $("runtime-stop").events.click();
  assert.equal($("runtime-confirm").hidden, false);

  // Confirm force stop
  await $("runtime-confirm-stop").events.click();
  assert.equal($("runtime-confirm").hidden, true);
  const stopCalls = calls.filter((c) => c.url === "/api/runtime/stop");
  assert.deepEqual(stopCalls.map((c) => c.body.force), [false, true]);
});

test("common.js refreshes runtime card when video submission triggers auto-start HTTP 400 and submits cleared COMFYUI_URL", async () => {
  const { $, ids } = makeDom();
  $("persist-config").checked = true;
  let postedConfig = null;
  let runtimePhase = "offline";

  class FakeFormData {
    constructor() {
      const entries = [];
      for (const id of ["video-provider", "comfy-url", "comfy-preset", "colab-auto-start", "llm-provider", "llm-composition-model", "llm-rewrite-model", "llm-base"]) {
        const el = ids.get(id);
        if (el && !el.disabled && el.name) entries.push([el.name, el.value]);
      }
      this._entries = entries;
    }
    [Symbol.iterator]() { return this._entries[Symbol.iterator](); }
  }
  $("video-provider").name = "VIDEO_PROVIDER";
  $("comfy-url").name = "COMFYUI_URL";
  $("comfy-preset").name = "COMFYUI_PRESET";
  $("colab-auto-start").name = "COLAB_AUTO_START";
  $("llm-provider").name = "LLM_PROVIDER";
  $("llm-composition-model").name = "LLM_COMPOSITION_MODEL";
  $("llm-rewrite-model").name = "LLM_REWRITE_MODEL";
  $("llm-base").name = "LLM_BASE_URL";

  const comfyConfig = {
    keys: { RUNNINGHUB_API_KEY: false, COMFYUI_API_KEY: true, APIMART_API_KEY: true, LLM_API_KEY: false, GEMINI_API_KEY: false },
    VIDEO_PROVIDER: "comfyui",
    COMFYUI_URL: "https://demo.ngrok-free.dev",
    COMFYUI_PRESET: "4step",
    COLAB_AUTO_START: "1",
    comfy_requires_key: true,
    LLM_PROVIDER: "apimart",
    LLM_BASE_URL: "https://api.apimart.ai/v1",
    LLM_MODEL: "deepseek-v4-pro",
    llm_providers: {
      apimart: { provider: "apimart", base_url: "https://api.apimart.ai/v1", composition_model: "deepseek-v4-pro", rewrite_model: "deepseek-v4-pro", key_names: ["LLM_API_KEY", "APIMART_API_KEY"] },
      gemini: { provider: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", composition_model: "gemini-flash-latest", rewrite_model: "gemini-flash-latest", key_names: ["GEMINI_API_KEY"] },
    },
    ffmpeg: true, ffprobe: true, python: "3.12.0", saved_to_env: true,
  };

  const context = vm.createContext({
    document: {
      getElementById: $,
      createElement: (tag) => $(`anon-${Math.random()}-${tag}`),
      querySelectorAll: () => [],
      documentElement: { lang: "en" },
      title: "",
    },
    localStorage: { getItem: () => "en", setItem() {} },
    FormData: FakeFormData,
    fetch: async (url, opts) => {
      if (url === "/api/runtime/status") {
        if (runtimePhase === "awaiting_drive_auth") {
          return {
            ok: true,
            json: async () => ({
              state: "awaiting_drive_auth",
              cli_available: true,
              auth_url: "https://accounts.google.com/o/oauth2/auth?auto=1",
              comfyui_url: "https://demo.ngrok-free.dev",
              logs: [],
            }),
          };
        }
        return { ok: true, json: async () => ({ state: "offline", cli_available: true, comfyui_url: "https://demo.ngrok-free.dev", logs: [] }) };
      }
      if (url === "/api/jobs") {
        runtimePhase = "awaiting_drive_auth";
        return { ok: false, status: 400, json: async () => ({ error: "Colab A100 startup triggered" }) };
      }
      if (url === "/api/config") {
        postedConfig = JSON.parse(opts.body);
        return { ok: true, json: async () => ({ ...comfyConfig, COMFYUI_URL: postedConfig.COMFYUI_URL ?? comfyConfig.COMFYUI_URL }) };
      }
      return { ok: true, json: async () => enMessages };
    },
  });
  const freshMessages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/en.json"), "utf8"));
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "common.js"), "utf8"), context);
  vm.runInContext(`state.messages = ${JSON.stringify(freshMessages)}; state.config = ${JSON.stringify(comfyConfig)}; renderConfig(true);`, context);
  await new Promise((r) => setImmediate(r));
  assert.equal($("runtime-drive-box").hidden, true);

  // Trigger POST /api/jobs returning 400 -> runtime card must auto-refresh and reveal Drive OAuth box
  await assert.rejects(() => context.post("/api/jobs", { kind: "one" }));
  await new Promise((r) => setImmediate(r));
  assert.equal($("runtime-drive-box").hidden, false);
  assert.equal($("runtime-drive-link").href, "https://accounts.google.com/o/oauth2/auth?auto=1");

  // Clearing #comfy-url and saving submits COMFYUI_URL: ""
  $("comfy-url").value = "";
  await $("config-form").events.submit({ preventDefault() {}, target: $("config-form") });
  assert.equal(postedConfig.COMFYUI_URL, "");
});

test("style.css normalizes all form selects to custom appearance and exact height parity with text inputs", () => {
  const rawCss = fs.readFileSync(path.join(staticDirectory, "style.css"), "utf8");
  const topLevelCss = rawCss.replace(/@media[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}/g, "");
  const rules = [];
  for (const match of topLevelCss.matchAll(/([^{}]+)\{([^{}]+)\}/g)) {
    const selectors = match[1].trim();
    const decls = {};
    for (const part of match[2].split(";")) {
      const colon = part.indexOf(":");
      if (colon === -1) continue;
      decls[part.slice(0, colon).trim()] = part.slice(colon + 1).trim();
    }
    rules.push({ selectors, decls });
  }

  const collectFor = (predicate) => {
    const merged = {};
    for (const rule of rules) {
      if (predicate(rule.selectors)) {
        if ("background" in rule.decls) {
          delete merged["background-image"];
        }
        Object.assign(merged, rule.decls);
      }
    }
    return merged;
  };

  const splitTopLevelCommas = (sel) => {
    const parts = [];
    let depth = 0, cur = "";
    for (const ch of sel) {
      if (ch === "(" || ch === "[") depth += 1;
      else if (ch === ")" || ch === "]") depth -= 1;
      if (ch === "," && depth === 0) {
        parts.push(cur.trim());
        cur = "";
      } else {
        cur += ch;
      }
    }
    if (cur.trim()) parts.push(cur.trim());
    return parts;
  };

  const formSelect = collectFor((s) => splitTopLevelCommas(s).includes("select"));
  const textInput = collectFor((s) => splitTopLevelCommas(s).some((p) => p === "input" || p.startsWith("input:where(:not(")));
  assert.equal(formSelect.appearance, "none", "select must disable native OS popup appearance");
  assert.equal(formSelect["-webkit-appearance"], "none", "select must disable WebKit native popup appearance on macOS");
  assert.ok(formSelect["background-image"]?.startsWith("url("), "select must render custom dropdown chevron");
  assert.equal(formSelect.height, "44px", "form select must set explicit 44px height");
  assert.equal(textInput.height, formSelect.height, "single-line text input height must match form select height");

  const segmentSelect = { ...formSelect, ...collectFor((s) => splitTopLevelCommas(s).includes(".segment-editor select")) };
  assert.equal(segmentSelect.height, formSelect.height, "segment editor select must match form input height");

  const headerSelect = collectFor((s) => {
    const parts = splitTopLevelCommas(s);
    return parts.includes("select") || parts.includes(".header-tools select");
  });
  assert.equal(headerSelect.height, "auto", "header language select must stay compact (height: auto)");
  assert.ok(headerSelect["background-image"]?.startsWith("url("), "header language select must retain custom chevron background-image");

  const pageJumpInput = { ...textInput, ...collectFor((s) => splitTopLevelCommas(s).includes(".page-jump input")) };
  const plyrInput = { ...textInput, ...collectFor((s) => splitTopLevelCommas(s).includes(".artifact .plyr input")) };
  const checkboxInput = { ...textInput, ...collectFor((s) => splitTopLevelCommas(s).includes(".checkbox-field input")) };
  assert.equal(pageJumpInput.height, "auto", ".page-jump input must preserve compact height");
  assert.equal(plyrInput.height, "auto", ".artifact .plyr input must not inherit 44px textbox height");
  assert.equal(checkboxInput.height, "16px", ".checkbox-field input must remain 16px");
});

test("results.js renders live ComfyUI stage, step bar, VRAM, and elapsed progress and hides when finished", async () => {
  const { $, element } = makeDom();
  $("results-page").dataset.jobId = "job-live-comfy";
  let currentJob = {
    id: "job-live-comfy", kind: "film", demo: false, status: "running", operation: "pipeline",
    created_at: "2026-11-18T20:00:00", output_dir: "/tmp/out", logs: "submitted seg1", report: "",
    progress: { frames: 4, clips: 0 }, artifacts: [], review: { kind: "frames", total: 4, complete: true },
    storyboard: [],
    segments: [{
      number: 1, start: 0, end: 13, seconds: 13, shots: 4, url: null, task_pending: true,
      live_progress: { stage: "sampling", node: "125", step: 2, max_steps: 4, vram_used_gb: 31.4, vram_total_gb: 40.0, elapsed_s: 135, queued: false },
    }],
  };

  const context = vm.createContext({
    state: { language: "en", config: {}, jobs: [] },
    $, element, renderPage: null,
    document: { addEventListener() {}, querySelectorAll: () => [] },
    window: { addEventListener() {} },
    t: (k, v = {}) => (enMessages[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
    running: (job) => Boolean(job && ["queued", "running"].includes(job.status)),
    renderConfig() {},
    showError() {},
    showMessage() {},
    clearNotices() {},
    dateLabel: () => "",
    initializePagination() {},
    renderPagination() {},
    initializeVideoSegments() {},
    renderResultTabs() {},
    renderVideoSegments() {},
    pauseSegmentPlayers() {},
    destroySegmentPlayers() {},
    setTimeout: () => 1,
    clearTimeout() {},
    loadLanguage: async () => {},
    request: async (url) => (url === "/api/bootstrap" ? { config: {}, jobs: [] } : currentJob),
    post: async () => ({}),
  });
  for (const script of ["frame-references.js", "results.js"]) {
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, script), "utf8"), context);
  }
  await new Promise((r) => setImmediate(r));
  await new Promise((r) => setImmediate(r));

  assert.equal($("live-progress").hidden, false, "#live-progress must be visible while ComfyUI segment reports live_progress");
  assert.match($("live-progress-label").textContent, /2\/4/, "Progress label must show live step ratio");
  assert.match($("live-progress-meta").textContent, /31\.4\/40(\.0)? GB/, "Progress meta must show VRAM usage");
  assert.match($("live-progress-meta").textContent, /2:15|02:15/, "Progress meta must show elapsed time");
  assert.equal($("live-progress-fill").style.width, "50%", "Progress fill width must reflect 2/4 = 50%");

  currentJob = {
    ...currentJob,
    segments: [{
      ...currentJob.segments[0],
      live_progress: { stage: "upscale_video", node: "155", step: 60, max_steps: 120, vram_used_gb: 28.0, vram_total_gb: 40.0, elapsed_s: 190, queued: false },
    }],
  };
  await context.refreshJob();
  assert.match($("live-progress-label").textContent, /Upscaling video/, "Progress label must render upscale_video stage copy");
  assert.equal($("live-progress-fill").style.width, "50%");

  currentJob = { ...currentJob, status: "succeeded", segments: [{ ...currentJob.segments[0], url: "/clip.mp4", task_pending: false, live_progress: null }] };
  await context.refreshJob();
  assert.equal($("live-progress").hidden, true, "#live-progress must hide once job finishes");
});

test("common.js renders multi-node ComfyUI pool status list, COMFYUI_CONCURRENCY, and results.js shows node_label", async () => {
  const { ids, $ } = makeDom();
  $("persist-config").checked = true;
  let postedConfig = null;
  class FakeFormData {
    constructor() {
      const entries = [];
      for (const id of ["video-provider", "comfy-url", "comfy-concurrency", "comfy-preset", "comfy-resolution", "comfy-upscale", "colab-gpu", "colab-auto-start", "llm-provider", "llm-composition-model", "llm-rewrite-model", "llm-base"]) {
        const el = ids.get(id);
        if (el && !el.disabled && el.name) entries.push([el.name, el.value]);
      }
      this._entries = entries;
    }
    [Symbol.iterator]() { return this._entries[Symbol.iterator](); }
  }
  $("video-provider").name = "VIDEO_PROVIDER";
  $("comfy-url").name = "COMFYUI_URL";
  $("comfy-concurrency").name = "COMFYUI_CONCURRENCY";
  $("comfy-preset").name = "COMFYUI_PRESET";
  $("comfy-resolution").name = "COMFYUI_RESOLUTION";
  $("comfy-upscale").name = "COMFYUI_UPSCALE";
  $("colab-gpu").name = "COLAB_GPU";
  $("colab-auto-start").name = "COLAB_AUTO_START";
  $("llm-provider").name = "LLM_PROVIDER";
  $("llm-composition-model").name = "LLM_COMPOSITION_MODEL";
  $("llm-rewrite-model").name = "LLM_REWRITE_MODEL";
  $("llm-base").name = "LLM_BASE_URL";

  const multiConfig = {
    keys: { RUNNINGHUB_API_KEY: false, COMFYUI_API_KEY: true, APIMART_API_KEY: true, LLM_API_KEY: true, GEMINI_API_KEY: false },
    VIDEO_PROVIDER: "comfyui",
    COMFYUI_URL: "https://node-a.example.com,https://node-b.example.com",
    COMFYUI_URLS: ["https://node-a.example.com", "https://node-b.example.com"],
    COMFYUI_CONCURRENCY: "",
    effective_comfyui_concurrency: 2,
    COMFYUI_PRESET: "full20",
    COMFYUI_RESOLUTION: "768p",
    COMFYUI_UPSCALE: "0",
    COLAB_GPU: "G4",
    COLAB_AUTO_START: "0",
    comfy_requires_key: true,
    LLM_PROVIDER: "apimart",
    LLM_BASE_URL: "https://api.apimart.ai/v1",
    LLM_MODEL: "deepseek-v4-pro",
    llm_providers: {
      apimart: { provider: "apimart", base_url: "https://api.apimart.ai/v1", composition_model: "deepseek-v4-pro", rewrite_model: "deepseek-v4-pro", key_names: ["LLM_API_KEY", "APIMART_API_KEY"] },
      gemini: { provider: "gemini", base_url: "https://generativelanguage.googleapis.com/v1beta/openai", composition_model: "gemini-flash-latest", rewrite_model: "gemini-flash-latest", key_names: ["GEMINI_API_KEY"] },
    },
    ffmpeg: true, ffprobe: true, python: "3.12.0", saved_to_env: true,
  };

  let statusPayload = {
    state: "online",
    gpu: "G4",
    cli_available: true,
    comfyui_url: "https://node-a.example.com",
    online_count: 1,
    total_count: 2,
    nodes: [
      { url: "https://node-a.example.com", label: "node-a.example.com", online: true, auth_failed: false, devices: [{ name: "RTX PRO 6000" }], queue_running: 1, queue_pending: 0 },
      { url: "https://node-b.example.com", label: "node-b.example.com", online: false, auth_failed: false, devices: [], queue_running: 0, queue_pending: 0 },
    ],
    logs: [],
  };

  const context = vm.createContext({
    document: {
      getElementById: $,
      createElement: (tag) => $(`anon-${Math.random()}-${tag}`),
      querySelectorAll: () => [],
      documentElement: { lang: "zh-CN" },
      title: "",
    },
    localStorage: { getItem: () => "zh-CN", setItem() {} },
    FormData: FakeFormData,
    fetch: async (url, opts) => {
      if (url === "/api/runtime/status") return { ok: true, json: async () => statusPayload };
      if (url === "/api/config") {
        postedConfig = JSON.parse(opts.body);
        return { ok: true, json: async () => ({ ...multiConfig, COMFYUI_CONCURRENCY: postedConfig.COMFYUI_CONCURRENCY ?? "" }) };
      }
      return { ok: true, json: async () => zhMessages };
    },
  });
  const zhFresh = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/zh-CN.json"), "utf8"));
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "common.js"), "utf8"), context);
  vm.runInContext(`state.messages = ${JSON.stringify(zhFresh)}; state.config = ${JSON.stringify(multiConfig)}; renderConfig(true);`, context);
  await new Promise((r) => setImmediate(r));

  assert.equal($("comfy-concurrency").disabled, false);
  assert.equal($("runtime-nodes").hidden, false, "Multi-node status must reveal #runtime-nodes list");
  assert.equal($("runtime-nodes").children.length, 2);
  assert.match($("runtime-detail").textContent, /1\/2/);

  // Submitting blank COMFYUI_CONCURRENCY sends "" so server reverts to auto concurrency
  $("comfy-concurrency").value = "";
  await $("config-form").events.submit({ preventDefault() {}, target: $("config-form") });
  assert.equal(postedConfig.COMFYUI_CONCURRENCY, "");

  // Single-node status hides #runtime-nodes
  statusPayload = { state: "online", gpu: "G4", cli_available: true, comfyui_url: "https://node-a.example.com", online_count: 1, total_count: 1, nodes: [statusPayload.nodes[0]], devices: [{ name: "RTX PRO 6000" }], logs: [] };
  await context.refreshRuntimeStatus();
  assert.equal($("runtime-nodes").hidden, true, "Single-node status must keep #runtime-nodes hidden");

  // Verify results.js formatLiveProgressParts renders node_label in both locales
  for (const [lang, dict, expected] of [
    ["zh-CN", zhFresh, "节点 node-b.example.com"],
    ["en", JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages/en.json"), "utf8")), "Node node-b.example.com"],
  ]) {
    const rDom = makeDom();
    rDom.$("results-page").dataset = { jobId: "job-multi" };
    const rCtx = vm.createContext({
      state: { language: lang, config: {}, jobs: [] },
      $: rDom.$,
      element: () => ({}),
      renderPage: null,
      document: { addEventListener() {}, querySelectorAll: () => [] },
      window: { addEventListener() {} },
      t: (k, v = {}) => (dict[k] || k).replace(/\{(\w+)\}/g, (_, n) => v[n] ?? ""),
      running: () => true,
      renderConfig() {}, showError() {}, showMessage() {}, clearNotices() {}, dateLabel: () => "",
      initializePagination() {}, renderPagination() {}, initializeVideoSegments() {}, renderResultTabs() {},
      renderVideoSegments() {}, pauseSegmentPlayers() {}, destroySegmentPlayers() {},
      setTimeout: () => 1, clearTimeout() {}, loadLanguage: async () => {},
      request: async () => ({}), post: async () => ({}),
    });
    vm.runInContext(fs.readFileSync(path.join(staticDirectory, "results.js"), "utf8"), rCtx);
    const parts = rCtx.formatLiveProgressParts({
      stage: "sampling", step: 10, max_steps: 20, vram_used_gb: 41.6, vram_total_gb: 95.0,
      elapsed_s: 125, node_label: "node-b.example.com",
    }, 2);
    assert.ok(parts.meta.includes(expected), `${lang} live progress meta must include node label: ${parts.meta}`);
  }
});


