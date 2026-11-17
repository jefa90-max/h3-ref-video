"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const staticDirectory = path.join(__dirname, "../workbench/static");

function descendants(node) {
  return [node, ...node.children.flatMap(descendants)];
}

function fixture(language = "zh-CN") {
  const messages = JSON.parse(fs.readFileSync(path.join(staticDirectory, "messages", `${language}.json`), "utf8"));
  function element(tag, className = "", textContent = "") {
    return {
      tag, className, textContent, dataset: {}, children: [], attributes: {}, events: {}, disabled: false,
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.events[name] = callback; },
      click() { if (!this.disabled) return this.events.click?.(); },
    };
  }
  const alice = { kind: "character", name: "Alice", path: "/initial/alice.png", url: "/characters/0" };
  const bob = { kind: "character", name: "Bob", path: "/initial/bob.png", url: "/characters/1" };
  const missing = { kind: "character", name: "Missing", path: "/initial/missing.png", url: null };
  const frame = { key: "s1-2", references_editable: true, references: [
    { kind: "board", path: "/initial/board.png", url: "/board.png" }, alice,
    { kind: "anchor", path: "/frames/s1-1.png", url: "/anchor.png" },
  ] };
  const state = { language, submitting: false, frameReferenceDrafts: new Map(),
    selectedJob: { status: "succeeded", character_references: [alice, bob, missing] } };
  let section;
  let changes = 0;
  const context = vm.createContext({
    state, element,
    t: (key, values = {}) => (messages[key] || key).replace(/\{(\w+)\}/g, (_, name) => values[name]),
    request() { assert.fail("The frame picker must not upload or request files"); },
    renderResultActions() {
      for (const node of descendants(section)) {
        if (node.className.split(" ").includes("frame-reference-action")) {
          node.disabled = state.submitting || state.selectedJob.status === "running" || node.dataset.enabled !== "true";
        }
      }
    },
  });
  vm.runInContext(fs.readFileSync(path.join(staticDirectory, "frame-references.js"), "utf8"), context);
  function render() {
    section = context.createFrameReferenceEditor(frame, () => { changes += 1; });
    context.renderResultActions();
    return section;
  }
  render();
  return { context, state, frame, alice, bob, missing, messages, render,
    nodes: () => descendants(section), changes: () => changes };
}

for (const language of ["zh-CN", "en"]) {
  test(`selects only imported unselected characters without an upload control (${language})`, () => {
    const ui = fixture(language);
    assert.equal(ui.nodes().filter((node) => node.tag === "input" && node.type === "file").length, 0);
    const toggle = ui.nodes().find((node) => node.className.includes("frame-character-toggle"));
    assert.ok(toggle, "The existing-role chooser must be present");
    toggle.click();
    const choices = ui.nodes().filter((node) => node.className.includes("frame-character-option"));
    assert.equal(choices.length, 2);
    assert.deepEqual(choices.map((node) => node.attributes["aria-label"]), [
      ui.messages.addNamedFrameCharacter.replace("{name}", "Bob"),
      ui.messages.addNamedFrameCharacter.replace("{name}", "Missing"),
    ]);
    assert.equal(choices[1].disabled, true);
    choices[1].click();
    choices[0].click();
    choices[0].click();
    assert.deepEqual(Array.from(ui.context.frameGenerationReferences(ui.frame), (reference) => reference.path), [
      "/initial/board.png", "/initial/alice.png", "/initial/bob.png",
    ]);
    assert.equal(ui.changes(), 1);
    assert.equal(ui.frame.references.length, 3, "Selection must remain a draft for this frame");
  });
}

test("removed imported roles can be selected again and fixed references stay read only", () => {
  const ui = fixture();
  let remove = ui.nodes().find((node) => node.tag === "button" && node.textContent === ui.messages.removeImage);
  assert.ok(remove);
  remove.click();
  assert.deepEqual(Array.from(ui.context.frameGenerationReferences(ui.frame), (reference) => reference.kind), ["board"]);
  assert.equal(ui.nodes().filter((node) => node.textContent === ui.messages.removeImage).length, 0);
  ui.render();
  const toggle = ui.nodes().find((node) => node.className.includes("frame-character-toggle"));
  assert.ok(toggle);
  toggle.click();
  const alice = ui.nodes().find((node) => node.className.includes("frame-character-option") && node.attributes["aria-label"]?.includes("Alice"));
  assert.ok(alice);
  alice.click();
  assert.equal(ui.context.frameReferencesChanged(ui.frame), false);
  assert.deepEqual(Array.from(ui.context.frameCharacterReferences(ui.frame), (reference) => reference.path), ["/initial/alice.png"]);
  assert.equal(ui.state.frameReferenceDrafts.has("another-frame"), false);
});

test("the picker explains an empty catalog or all-selected catalog and locks during generation", () => {
  const ui = fixture();
  ui.state.selectedJob.character_references = [];
  ui.render();
  assert.ok(ui.nodes().some((node) => node.textContent === ui.messages.frameCharacterCatalogEmpty));
  ui.state.selectedJob.character_references = [ui.alice];
  ui.render();
  assert.ok(ui.nodes().some((node) => node.textContent === ui.messages.frameCharactersAllSelected));
  ui.state.selectedJob.character_references = [ui.alice, ui.bob];
  ui.render();
  ui.nodes().find((node) => node.className.includes("frame-character-toggle")).click();
  ui.state.selectedJob.status = "running";
  ui.context.renderResultActions();
  const add = ui.nodes().find((node) => node.className.includes("frame-character-option"));
  assert.equal(add.disabled, true);
  add.click();
  assert.equal(ui.state.frameReferenceDrafts.size, 0);
});
