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
      value: "", name: "", dataset: {}, events: {}, children: [], attributes: {},
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

