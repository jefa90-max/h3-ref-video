"use strict";

Object.assign(state, {
  kind: "one", demo: false, example: null, images: [], submitting: false,
  uploading: false, pollTimer: null, film: null, filmSlots: [],
  filmBindings: new Map(), filmRevision: 0, filmParsing: false,
  filmImporting: false, filmError: null, choosingOutput: false,
});

function renderMode() {
  $("mode-demo").classList.toggle("active", state.demo);
  $("mode-live").classList.toggle("active", !state.demo);
  $("mode-demo").setAttribute("aria-pressed", String(state.demo));
  $("mode-live").setAttribute("aria-pressed", String(!state.demo));
  document.querySelector(".run-settings").classList.toggle("live", !state.demo);
  $("mode-hint").textContent = t(state.demo ? "demoHint" : "liveHint");
  $("start-label").textContent = t(state.submitting ? "starting" : state.kind === "film" ? ($("frames").value === "none" ? "prepareScriptReview" : "prepareFrameReview") : state.demo ? "startDemo" : "startLive");
  $("submit-hint").textContent = t(busy() ? "submitHintBusy" : state.demo ? "submitHintDemo" : "submitHintLive");
  const filmBlocked = state.kind === "film" && (!state.film || state.filmParsing || state.filmImporting || missingFilmReferences().length > 0);
  $("start-job").disabled = !state.ready || state.submitting || state.uploading || state.choosingOutput || busy() || filmBlocked;
  $("choose-output-folder").disabled = !state.ready || state.submitting || state.choosingOutput;
  $("choose-output-folder").textContent = t(state.choosingOutput ? "choosingOutputFolder" : "chooseOutputFolder");
  const inputsLocked = state.uploading || state.filmImporting || state.submitting;
  $("film-json").readOnly = inputsLocked;
  $("film-import").disabled = inputsLocked;
  $("reset-example").disabled = inputsLocked;
  $("one-upload").disabled = inputsLocked;
  document.querySelectorAll(".film-reference-upload, .film-reference-remove").forEach((input) => {
    input.disabled = inputsLocked || state.filmParsing || !state.film;
  });
  $("demo-placeholders-field").hidden = !state.demo;
  const missing = missingFilmReferences();
  $("film-missing-references").textContent = missing.length ? t("filmMissingReferences", { names: missing.map(filmSlotName).join(t("listSeparator")) }) : "";

}

function switchWorkflow(kind) {
  state.kind = kind;
  for (const name of ["one", "film"]) {
    const active = name === kind;
    $(`tab-${name}`).classList.toggle("active", active);
    $(`tab-${name}`).setAttribute("aria-selected", String(active));
    $(`tab-${name}`).tabIndex = active ? 0 : -1;
    $(`${name}-fields`).hidden = !active;
  }
  // Hidden controls must not block native form validation in the other workflow.
  $("seconds").disabled = kind !== "one";
  $("seed").disabled = kind !== "one";
  clearNotices();
  renderMode();
}

function renderReferences() {
  $("image-count").textContent = t("imageCount", { count: state.images.length });
  const list = $("reference-list");
  list.replaceChildren();
  state.images.forEach((file, index) => {
    const item = element("div", "reference-item");
    const preview = element("img");
    preview.src = file.url;
    preview.alt = file.name;
    const remove = element("button", "", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", t("removeReference", { index: index + 1 }));
    remove.addEventListener("click", () => { state.images.splice(index, 1); renderReferences(); });
    const filename = t("referenceName", { index: index + 1, name: file.name });
    item.append(preview, remove, element("span", "", filename));
    item.title = filename;
    list.append(item);
  });
}

function parseFilm() {
  let film;
  try { film = JSON.parse($("film-json").value); }
  catch (error) { throw new Error(t("jsonInvalid", { error: error.message })); }
  if (!film || typeof film !== "object" || Array.isArray(film)) throw new Error(t("jsonObjectRequired"));
  return film;
}

function filmSlotName(slot) {
  return slot.kind === "board" ? t("filmBoardName") : t("filmCastName", { name: slot.name });
}

function missingFilmReferences() {
  if (state.demo && $("demo-placeholders").checked) return [];
  return state.filmSlots.filter((slot) => slot.required && !state.filmBindings.has(slot.id));
}

function invalidateFilmReferences(clearBindings = false) {
  state.filmRevision += 1;
  state.film = null;
  state.filmSlots = [];
  state.filmError = null;
  if (clearBindings) state.filmBindings.clear();
  renderFilmReferences();
  return state.filmRevision;
}

async function refreshFilmReferences({ reset = false, format = false } = {}) {
  state.filmParsing = true;
  const revision = invalidateFilmReferences(reset);
  try {
    const result = await post("/api/film/references", { film: parseFilm() });
    if (revision !== state.filmRevision) return false;
    state.film = result.film;
    state.filmSlots = result.slots;
    const retained = new Map();
    for (const slot of result.slots) {
      const binding = state.filmBindings.get(slot.id);
      if (binding && binding.name === slot.name) retained.set(slot.id, binding);
    }
    state.filmBindings = retained;
    if (format) $("film-json").value = JSON.stringify(result.film, null, 2);
    return true;
  } catch (error) {
    if (revision !== state.filmRevision) return false;
    state.filmError = error.message;
    return false;
  } finally {
    if (revision === state.filmRevision) {
      state.filmParsing = false;
      renderFilmReferences();
    }
  }
}

function renderFilmReferences() {
  $("film-parse-status").textContent = state.filmImporting ? t("filmImporting") : state.filmParsing ? t("filmParsing") : state.filmError || "";
  $("film-parse-status").classList.toggle("field-error", Boolean(state.filmError));
  $("film-json").setAttribute("aria-invalid", String(Boolean(state.filmError)));
  const list = $("film-reference-list");
  list.replaceChildren();
  for (const slot of state.filmSlots) {
    const binding = state.filmBindings.get(slot.id);
    const name = filmSlotName(slot);
    const card = element("article", "film-reference-card");
    const heading = element("div", "label-heading");
    heading.append(element("strong", "", name), element("span", "subtle", t(slot.required ? "referenceRequired" : "optional")));
    card.append(heading);
    if (slot.description) card.append(element("p", "hint film-reference-description", slot.description));
    card.append(element("p", `film-reference-state${binding ? " uploaded" : ""}`, t(binding ? "referenceUploaded" : "referencePending")));
    if (binding) {
      const preview = element("img", "film-reference-preview");
      preview.src = binding.file.url;
      preview.alt = t("filmReferencePreview", { name });
      card.append(preview, element("p", "hint film-reference-filename", binding.file.name));
    }
    const actions = element("div", "button-row");
    const uploadLabel = element("label", "button secondary file-button");
    uploadLabel.append(element("span", "", t(binding ? "replaceReference" : "uploadReference")));
    const upload = element("input", "film-reference-upload");
    upload.type = "file";
    upload.accept = "image/png,image/jpeg,image/webp";
    upload.setAttribute("aria-label", t(binding ? "replaceNamedReference" : "uploadNamedReference", { name }));
    upload.addEventListener("change", async () => {
      const file = upload.files[0];
      if (!file || state.uploading || state.filmParsing || !state.film) return;
      try {
        const [uploaded] = await uploadFiles([file]);
        state.filmBindings.set(slot.id, { name: slot.name, file: uploaded });
        renderFilmReferences();
        showMessage(t("referenceBound", { name }));
      } catch (error) { showError(error); }
      finally { upload.value = ""; }
    });
    uploadLabel.append(upload);
    actions.append(uploadLabel);
    if (binding) {
      const remove = element("button", "button text-button film-reference-remove", t("removeImage"));
      remove.type = "button";
      remove.setAttribute("aria-label", t("removeNamedReference", { name }));
      remove.addEventListener("click", () => {
        state.filmBindings.delete(slot.id);
        renderFilmReferences();
      });
      actions.append(remove);
    }
    card.append(actions);
    list.append(card);
  }
  renderMode();
}

function updateFrameHint() {
  const hint = { shot: "framesShotHint", first: "framesFirstHint", none: "framesNoneHint" };
  $("frame-mode-hint").textContent = t(hint[$("frames").value]);
  renderMode();
}

async function uploadFiles(files) {
  const form = new FormData();
  files.forEach((file) => form.append("images", file));
  state.uploading = true;
  renderMode();
  showMessage(t("uploading"));
  try { return (await request("/api/uploads", { method: "POST", body: form })).files; }
  finally {
    state.uploading = false;
    renderMode();
  }
}

function renderHistory() {
  const history = $("history-list");
  history.replaceChildren();
  $("history-empty").hidden = state.jobs.length > 0;
  state.jobs.forEach((job) => {
    const row = element("a", "history-row");
    row.href = `/jobs/${encodeURIComponent(job.id)}`;
    const description = element("div");
    description.append(
      element("strong", "", t("historyJob", { kind: t(job.kind === "one" ? "oneTitle" : "filmTitle"), mode: t(job.demo ? "demoMode" : "liveMode"), id: job.id.slice(-8) })),
      element("small", "", dateLabel(job.created_at)),
    );
    row.append(description, element("span", `status-badge ${job.status}`, t(`status_${job.status}`)));
    history.append(row);
  });
}

async function refreshBootstrap(fillInputs = true) {
  const result = await request("/api/bootstrap");
  const firstLoad = !state.example;
  state.config = result.config;
  $("output-dir").value = result.config.output_root;
  state.example = result.example;
  state.jobs = result.jobs;
  state.ready = true;
  if (firstLoad) {
    $("film-json").value = JSON.stringify(state.example, null, 2);
    await refreshFilmReferences({ reset: true, format: true });
  }
  renderConfig(fillInputs);
  renderHistory();
  renderMode();
  if (busy()) scheduleHistoryPoll();
  return result;
}

for (const kind of ["one", "film"]) {
  $(`tab-${kind}`).addEventListener("click", () => switchWorkflow(kind));
  $(`tab-${kind}`).addEventListener("keydown", (event) => {
    if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const next = event.key === "Home" ? "one" : event.key === "End" ? "film" : state.kind === "one" ? "film" : "one";
      switchWorkflow(next);
      $(`tab-${next}`).focus();
    }
  });
}

$("mode-demo").addEventListener("click", () => { state.demo = true; renderMode(); });
$("mode-live").addEventListener("click", () => { state.demo = false; renderMode(); });
$("frames").addEventListener("change", updateFrameHint);
$("film-json").addEventListener("input", () => { refreshFilmReferences(); });
$("demo-placeholders").addEventListener("change", renderMode);

$("choose-output-folder").addEventListener("click", async () => {
  if (state.choosingOutput || state.submitting) return;
  state.choosingOutput = true;
  renderMode();
  clearNotices();
  try {
    const selected = await post("/api/output-folder/pick", {});
    state.config.output_root = selected.output_root;
    $("output-dir").value = selected.output_root;
    if (!selected.cancelled) showMessage(t("outputFolderSaved"));
  } catch (error) { showError(error); }
  finally { state.choosingOutput = false; renderMode(); }
});

$("one-upload").addEventListener("change", async (event) => {
  const files = [...event.target.files];
  if (!files.length) return;
  try {
    if (state.images.length + files.length > 9) throw new Error(t("tooManyImages", { current: state.images.length, added: files.length }));
    const uploads = await uploadFiles(files);
    state.images.push(...uploads);
    renderReferences();
    showMessage(t("uploaded", { count: uploads.length }));
  } catch (error) { showError(error); }
  finally { event.target.value = ""; }
});

$("film-import").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file || state.uploading || state.filmImporting) return;
  state.filmImporting = true;
  state.filmParsing = false;
  const revision = invalidateFilmReferences(true);
  try {
    if (file.size > 2 * 1024 * 1024) throw new Error(t("jsonTooLarge"));
    const text = await file.text();
    if (revision !== state.filmRevision) return;
    $("film-json").value = text;
    if (await refreshFilmReferences({ reset: true, format: true })) showMessage(t("jsonImported", { name: file.name }));
  } catch (error) {
    state.filmError = error.message;
    showError(error);
  } finally {
    event.target.value = "";
    state.filmImporting = false;
    renderFilmReferences();
  }
});

$("reset-example").addEventListener("click", async () => {
  $("film-json").value = JSON.stringify(state.example, null, 2);
  if (await refreshFilmReferences({ reset: true, format: true })) showMessage(t("exampleRestored"));
});

$("job-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.submitting || state.uploading || state.choosingOutput || busy()) return;
  clearNotices();
  try {
    const operation = state.kind === "film" ? "frames" : "pipeline";
    const payload = { kind: state.kind, demo: state.demo, output_root: $("output-dir").value, operation };
    if (state.kind === "one") {
      const seconds = Number($("seconds").value);
      if (!Number.isInteger(seconds) || seconds < 5 || seconds > 15) throw new Error(t("secondsInvalid", { value: $("seconds").value }));
      if (!$("prompt").value.trim()) throw new Error(t("promptRequired"));
      if (!state.demo && !state.images.length) throw new Error(t("imagesRequired"));
      const seedText = $("seed").value.trim();
      const seed = seedText ? Number(seedText) : null;
      if (seed !== null && !Number.isSafeInteger(seed)) throw new Error(t("seedInvalid"));
      Object.assign(payload, { seconds, seed, check: $("check-subtitles").checked, aspect: $("aspect").value, prompt: $("prompt").value.trim(), images: state.images.map((file) => file.path) });
    } else {
      if (!state.film || state.filmParsing || state.filmImporting) throw new Error(state.filmError || t("filmNotReady"));
      const missing = missingFilmReferences();
      if (missing.length) throw new Error(t("filmMissingReferences", { names: missing.map(filmSlotName).join(t("listSeparator")) }));
      Object.assign(payload, {
        film: state.film, frames: $("frames").value,
        reference_bindings: Object.fromEntries([...state.filmBindings].map(([id, binding]) => [id, binding.file.id])),
        demo_placeholders: state.demo && $("demo-placeholders").checked,
      });
    }
    state.submitting = true;
    renderMode();
    const job = await post("/api/jobs", payload);
    window.location.assign(`/jobs/${encodeURIComponent(job.id)}`);
  } catch (error) { showError(error); }
  finally { state.submitting = false; renderMode(); }
});

function scheduleHistoryPoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(async () => {
    try { await refreshBootstrap(false); }
    catch (error) { showError(error); scheduleHistoryPoll(); }
  }, 1800);
}

$("refresh-history").addEventListener("click", async () => {
  $("refresh-history").disabled = true;
  try { await refreshBootstrap(false); clearNotices(); }
  catch (error) { showError(error); }
  finally { $("refresh-history").disabled = false; }
});

renderPage = (previousSample) => {
  if (!$("prompt").value || $("prompt").value === previousSample) $("prompt").value = t("samplePrompt");
  renderMode();
  renderReferences();
  updateFrameHint();
  renderFilmReferences();
  renderHistory();
};

async function initializeCreate() {
  try {
    await loadLanguage(state.language);
    await refreshBootstrap();
  } catch (error) { showError(error); }
}
initializeCreate();
