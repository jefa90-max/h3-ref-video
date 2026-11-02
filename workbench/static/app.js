"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  language: "zh-CN", messages: {}, kind: "one", demo: true,
  config: null, example: null, images: [], jobs: [], selectedJob: null,
  submitting: false, uploading: false, ready: false, pollTimer: null,
  artifactSignature: "", pollError: false,
  artifactNodes: new Map(), storyboardSignature: "", frameDrafts: new Map(),
  film: null, filmSlots: [], filmBindings: new Map(), filmRevision: 0,
  filmParsing: false, filmImporting: false, filmError: null,
};

function t(key, values = {}) {
  return (state.messages[key] ?? key).replace(/\{(\w+)\}/g, (_, name) => String(values[name] ?? ""));
}

function showError(error) {
  $("global-error").textContent = error instanceof Error ? error.message : String(error);
  $("global-error").hidden = false;
  $("global-message").hidden = true;
  $("global-error").scrollIntoView({ block: "nearest" });
}

function showMessage(message) {
  $("global-message").textContent = message;
  $("global-message").hidden = false;
  $("global-error").hidden = true;
  state.pollError = false;
}

function clearNotices() {
  $("global-error").hidden = true;
  $("global-message").hidden = true;
  state.pollError = false;
}

async function request(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: { "X-Workbench": "1", "Accept-Language": state.language, ...options.headers },
    });
  } catch (error) {
    throw new Error(t("networkError", { error: error.message }));
  }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || t("httpError", { status: response.status }));
  return body;
}

function post(url, payload) {
  return request(url, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
}

function element(tag, className, content) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== undefined) node.textContent = content;
  return node;
}

function running(job) {
  return job && ["queued", "running"].includes(job.status);
}

function busy() {
  return state.jobs.some(running);
}

function dateLabel(value) {
  const timestamp = typeof value === "number" ? value * 1000 : value;
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime()) ? String(value || "") : date.toLocaleString(state.language, {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

async function loadLanguage(language) {
  const response = await fetch(`/static/messages/${language}.json`);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const previousSample = state.messages.samplePrompt;
  state.messages = await response.json();
  state.language = language;
  clearNotices();
  document.documentElement.lang = language;
  document.title = t("pageTitle");
  $("language").value = language;
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAria));
  });
  if (!$("prompt").value || $("prompt").value === previousSample) $("prompt").value = t("samplePrompt");
  renderConfig();
  renderMode();
  renderReferences();
  updateFrameHint();
  renderFilmReferences();
  renderHistory();
  state.artifactSignature = "";
  renderJob(state.selectedJob);
}

function renderConfig(fillInputs = false) {
  const config = state.config;
  if (!config) return;
  const keys = config.keys || {};
  const complete = keys.RUNNINGHUB_API_KEY && keys.APIMART_API_KEY && config.ffmpeg && config.ffprobe;
  $("config-summary").textContent = t(complete ? "configReady" : "configIncomplete");
  $("rh-key-status").textContent = t(keys.RUNNINGHUB_API_KEY ? "keyPresent" : "keyMissing");
  $("apimart-key-status").textContent = t(keys.APIMART_API_KEY ? "keyPresent" : "keyMissing");
  $("llm-key-status").textContent = t(keys.LLM_API_KEY ? "keyPresent" : "llmFallback");
  $("config-storage").textContent = t(config.saved_to_env ? "configOnDisk" : "configInMemory");
  const environment = $("environment");
  environment.replaceChildren(element("span", "ok", t("pythonVersion", { version: config.python })));
  for (const name of ["ffmpeg", "ffprobe"]) {
    environment.append(element("span", config[name] ? "ok" : "missing", t(config[name] ? "environmentPresent" : "environmentMissing", { name })));
  }
  if (fillInputs) {
    $("llm-model").value = config.LLM_MODEL || "";
    $("llm-base").value = config.LLM_BASE_URL || "";
  }
}

function renderMode() {
  $("mode-demo").classList.toggle("active", state.demo);
  $("mode-live").classList.toggle("active", !state.demo);
  $("mode-demo").setAttribute("aria-pressed", String(state.demo));
  $("mode-live").setAttribute("aria-pressed", String(!state.demo));
  document.querySelector(".run-settings").classList.toggle("live", !state.demo);
  $("mode-hint").textContent = t(state.demo ? "demoHint" : "liveHint");
  $("start-label").textContent = t(state.submitting ? "starting" : state.demo ? "startDemo" : "startLive");
  $("submit-hint").textContent = t(busy() ? "submitHintBusy" : state.demo ? "submitHintDemo" : "submitHintLive");
  const filmBlocked = state.kind === "film" && (!state.film || state.filmParsing || state.filmImporting || missingFilmReferences().length > 0);
  $("start-job").disabled = !state.ready || state.submitting || state.uploading || busy() || filmBlocked;
  $("start-frames").hidden = state.kind !== "film" || $("frames").value === "none";
  $("start-frames").disabled = $("start-job").disabled || $("start-frames").hidden;
  $("start-frames").textContent = t(state.demo ? "startFramesDemo" : "startFramesLive");
  $("resume-job").disabled = state.submitting || busy();
  $("continue-job").disabled = state.submitting || busy();
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
  for (const button of document.querySelectorAll(".regenerate-frame")) {
    button.disabled = state.submitting || busy() || button.dataset.editable !== "true";
  }
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

function upsertJob(job) {
  const index = state.jobs.findIndex((item) => item.id === job.id);
  if (index === -1) state.jobs.unshift(job);
  else state.jobs[index] = { ...state.jobs[index], ...job };
  renderHistory();
  renderMode();
}

function renderHistory() {
  const history = $("history-list");
  history.replaceChildren();
  $("history-empty").hidden = state.jobs.length > 0;
  state.jobs.forEach((job) => {
    const row = element("button", `history-row${state.selectedJob?.id === job.id ? " selected" : ""}`);
    row.type = "button";
    const description = element("div");
    description.append(
      element("strong", "", t("historyJob", { kind: t(job.kind === "one" ? "oneTitle" : "filmTitle"), mode: t(job.demo ? "demoMode" : "liveMode"), id: job.id.slice(-8) })),
      element("small", "", dateLabel(job.created_at)),
    );
    row.append(description, element("span", `status-badge ${job.status}`, t(`status_${job.status}`)));
    row.addEventListener("click", async () => {
      try { await selectJob(job.id); }
      catch (error) { showError(error); }
    });
    history.append(row);
  });
}

function renderArtifacts(job) {
  const framePaths = new Set((job.storyboard || []).map((frame) => frame.path));
  const artifacts = (job.artifacts || []).filter((file) => !framePaths.has(file.path));
  const signature = `${job.id}:${JSON.stringify(artifacts)}:${state.language}`;
  if (state.artifactSignature === signature) return;
  state.artifactSignature = signature;
  $("artifact-section").hidden = !artifacts.length;
  const wanted = new Set(artifacts.map((file) => `${job.id}:${file.name}`));
  for (const [key, entry] of state.artifactNodes) {
    if (!wanted.has(key)) {
      entry.player?.destroy();
      entry.card.remove();
      state.artifactNodes.delete(key);
    }
  }
  artifacts.forEach((file, index) => {
    const key = `${job.id}:${file.name}`;
    const existing = state.artifactNodes.get(key);
    if (existing?.url === file.url && existing.language === state.language) return;
    existing?.player?.destroy();
    existing?.card.remove();
    const card = element("div", `artifact ${file.kind}`);
    let video;
    if (file.kind === "video") {
      video = element("video");
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.src = file.url;
      card.append(video);
    } else if (file.kind === "image") {
      const preview = element("img");
      preview.src = file.url;
      preview.loading = "lazy";
      preview.alt = t("artifactImageAlt", { name: file.name });
      card.append(preview);
    }
    const link = element("a", "", file.name);
    link.href = file.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = file.path;
    card.append(link);
    $("artifacts").insertBefore(card, $("artifacts").children[index] || null);
    const entry = { card, url: file.url, language: state.language, player: null };
    state.artifactNodes.set(key, entry);
    if (video) {
      if (typeof Plyr !== "function") {
        showError(t("playerUnavailable"));
        return;
      }
      entry.player = new Plyr(video, {
        iconUrl: "/static/vendor/plyr/plyr.svg", loadSprite: true,
        blankVideo: "/static/vendor/plyr/blank.mp4",
        storage: { enabled: false }, i18n: state.messages.player,
        controls: ["play-large", "play", "progress", "current-time", "mute", "volume", "settings", "pip", "fullscreen"],
        settings: ["speed"],
      });
    }
  });
}

function renderStoryboard(job) {
  const frames = job.storyboard || [];
  $("storyboard-section").hidden = !frames.length;
  const signature = `${job.id}:${JSON.stringify(frames)}:${job.status}:${state.language}`;
  if (state.storyboardSignature !== signature) {
    state.storyboardSignature = signature;
    $("storyboard-cards").replaceChildren();
    for (const frame of frames) {
      const draftKey = `${job.id}:${frame.key}`;
      const card = element("article", "storyboard-card");
      const heading = element("h4", "", t("frameTitle", { segment: frame.segment, shot: frame.shot }));
      card.append(heading);
      if (frame.url) {
        const link = element("a", "storyboard-preview");
        link.href = frame.url;
        link.target = "_blank";
        link.rel = "noopener";
        const image = element("img");
        image.src = frame.url;
        image.loading = "lazy";
        image.alt = heading.textContent;
        link.append(image);
        card.append(link);
      } else card.append(element("p", "hint", t("frameWaiting")));
      const label = element("label");
      label.append(element("span", "", t("framePromptLabel")));
      const editor = element("textarea", "frame-prompt");
      editor.rows = 7;
      editor.value = state.frameDrafts.get(draftKey) ?? frame.prompt;
      editor.addEventListener("input", () => state.frameDrafts.set(draftKey, editor.value));
      label.append(editor);
      card.append(label);
      const refs = element("details", "frame-references");
      refs.append(element("summary", "", t("frameReferences")));
      for (const path of frame.reference_paths || []) refs.append(element("code", "", path));
      card.append(refs);
      if (frame.missing_references?.length) card.append(element("p", "notice error", t("frameMissingReferences", { paths: frame.missing_references.join(", ") })));
      const button = element("button", "button secondary regenerate-frame", t(job.demo ? "regenerateDemo" : "regenerateLive"));
      button.type = "button";
      button.dataset.editable = String(frame.editable && job.status === "succeeded");
      button.addEventListener("click", async () => {
        if (!editor.value.trim()) { showError(t("framePromptRequired")); return; }
        await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/frames/${encodeURIComponent(frame.key)}`, { prompt: editor.value.trim() });
      });
      card.append(button, element("p", "hint", t(job.demo ? "regenerateDemoHint" : "regenerateLiveHint")));
      $("storyboard-cards").append(card);
    }
  }
  renderMode();
}

function renderJob(job) {
  $("monitor-empty").hidden = Boolean(job);
  $("job-detail").hidden = !job;
  const status = job?.status || "idle";
  $("job-status").textContent = t(`status_${status}`);
  $("job-status").className = `status-badge ${status}`;
  if (!job) return;
  $("job-kind").textContent = t(job.kind === "one" ? "oneTitle" : "filmTitle");
  $("job-mode").textContent = t(job.demo ? "demoMode" : "liveMode");
  $("job-operation").textContent = t({ frames: "operationFrames", frame: "operationFrame" }[job.operation] || "operationPipeline");
  $("job-created").textContent = dateLabel(job.created_at);
  $("job-error").hidden = !job.error;
  $("job-error").textContent = job.error || "";
  $("frames-count").textContent = job.progress?.frames ?? 0;
  $("clips-count").textContent = job.progress?.clips ?? 0;
  $("files-count").textContent = job.artifacts?.length ?? 0;
  $("progress-note").textContent = t(running(job) ? "progressRunning" : job.demo && job.status === "succeeded" ? "progressDemo" : "progressDone");
  $("job-output").textContent = job.output_dir;
  const logs = $("job-logs");
  const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 35;
  const nextLogs = job.logs || t("logsEmpty");
  if (logs.textContent !== nextLogs) {
    logs.textContent = nextLogs;
    if (atBottom) logs.scrollTop = logs.scrollHeight;
  }
  $("log-live").hidden = !running(job);
  const canResume = ["failed", "interrupted"].includes(job.status);
  $("resume-job").hidden = !canResume;
  $("resume-hint").hidden = !canResume;
  $("report-section").hidden = !job.report;
  $("job-report").textContent = job.report || "";
  $("video-stale").hidden = !job.pending_video;
  $("continue-section").hidden = !job.can_continue;
  $("continue-job").textContent = t(job.demo ? "continueDemo" : "continueLive");
  $("continue-hint").textContent = t(job.demo ? "continueDemoHint" : "continueLiveHint");
  renderStoryboard(job);
  renderArtifacts(job);
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(pollJobs, 1400);
}

async function pollJobs() {
  const selectedId = state.selectedJob?.id;
  const ids = new Set(state.jobs.filter(running).map((job) => job.id));
  if (selectedId) ids.add(selectedId);
  try {
    for (const id of ids) {
      const job = await request(`/api/jobs/${encodeURIComponent(id)}`);
      upsertJob(job);
      if (state.selectedJob?.id === id) {
        state.selectedJob = job;
        renderJob(job);
      }
    }
    if (state.pollError) {
      $("global-error").hidden = true;
      state.pollError = false;
    }
  } catch (error) {
    showError(t("pollError", { error: error.message }));
    state.pollError = true;
  } finally {
    if (state.jobs.some(running) || state.pollError) schedulePoll();
  }
}

async function selectJob(id) {
  const job = await request(`/api/jobs/${encodeURIComponent(id)}`);
  state.selectedJob = job;
  state.artifactSignature = "";
  upsertJob(job);
  renderJob(job);
  if (busy()) schedulePoll();
}

async function refreshBootstrap(fillInputs = true) {
  const result = await request("/api/bootstrap");
  const firstLoad = !state.example;
  state.config = result.config;
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
  if (busy()) schedulePoll();
  return result;
}

$("language").addEventListener("change", async (event) => {
  const language = event.target.value;
  try { await loadLanguage(language); }
  catch (error) {
    $("language").value = state.language;
    showError(t("languageError", { error: error.message }));
  }
});

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

$("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("save-config").disabled = true;
  clearNotices();
  try {
    const payload = Object.fromEntries([...new FormData(event.target)].filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]));
    payload.persist = $("persist-config").checked;
    state.config = await post("/api/config", payload);
    for (const id of ["rh-key", "apimart-key", "llm-key"]) $(id).value = "";
    renderConfig(true);
    showMessage(t(payload.persist ? "configSavedDisk" : "configSaved"));
  } catch (error) { showError(error); }
  finally { $("save-config").disabled = false; }
});

$("refresh-config").addEventListener("click", async () => {
  $("refresh-config").disabled = true;
  try { await refreshBootstrap(); showMessage(t("configRefreshed")); }
  catch (error) { showError(error); }
  finally { $("refresh-config").disabled = false; }
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
  if (state.submitting || state.uploading || busy()) return;
  clearNotices();
  try {
    const operation = event.submitter?.id === "start-frames" ? "frames" : "pipeline";
    const payload = { kind: state.kind, demo: state.demo, output_dir: $("output-dir").value.trim(), operation };
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
    state.selectedJob = job;
    state.artifactSignature = "";
    upsertJob(job);
    renderJob(job);
    schedulePoll();
  } catch (error) { showError(error); }
  finally { state.submitting = false; renderMode(); }
});

async function runJobAction(url, payload) {
  if (state.submitting || busy()) return;
  state.submitting = true;
  renderMode();
  clearNotices();
  try {
    const job = await post(url, payload);
    state.selectedJob = job;
    upsertJob(job);
    renderJob(job);
    schedulePoll();
  } catch (error) { showError(error); }
  finally { state.submitting = false; renderMode(); }
}

$("continue-job").addEventListener("click", async () => {
  if (state.selectedJob?.can_continue) await runJobAction(`/api/jobs/${encodeURIComponent(state.selectedJob.id)}/continue`, {});
});

$("resume-job").addEventListener("click", async () => {
  if (!state.selectedJob || state.submitting || busy()) return;
  state.submitting = true;
  renderMode();
  $("resume-job").textContent = t("resuming");
  clearNotices();
  try {
    const job = await post(`/api/jobs/${encodeURIComponent(state.selectedJob.id)}/resume`, {});
    state.selectedJob = job;
    upsertJob(job);
    renderJob(job);
    schedulePoll();
  } catch (error) { showError(error); }
  finally {
    state.submitting = false;
    $("resume-job").textContent = t("resumeJob");
    renderMode();
  }
});

$("refresh-history").addEventListener("click", async () => {
  $("refresh-history").disabled = true;
  try {
    await refreshBootstrap(false);
    if (state.selectedJob) await selectJob(state.selectedJob.id);
    clearNotices();
  } catch (error) { showError(error); }
  finally { $("refresh-history").disabled = false; }
});

async function initialize() {
  try {
    await loadLanguage(state.language);
    await refreshBootstrap();
    const initialJob = state.jobs.find(running) || state.jobs[0];
    if (initialJob) await selectJob(initialJob.id);
  } catch (error) { showError(error); }
}

initialize();
