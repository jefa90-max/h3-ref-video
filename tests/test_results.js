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
