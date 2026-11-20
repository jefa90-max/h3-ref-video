"use strict";

const $ = (id) => document.getElementById(id);
const savedLanguage = localStorage.getItem("h3-workbench-language");
const state = {
  language: ["zh-CN", "en"].includes(savedLanguage) ? savedLanguage : "zh-CN",
  messages: {}, config: null, jobs: [], ready: false, pollError: false,
  runtimeStatus: null, runtimeBusy: false,
};
let renderPage = () => {};
let llmProvider = null;
let llmDrafts = {};
let runtimePollTimer = null;

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
  if (!response.ok) {
    if ((state.config?.VIDEO_PROVIDER || "runninghub") === "comfyui" && !String(url).startsWith("/api/runtime/")) {
      refreshRuntimeStatus().catch(() => {});
    }
    const error = new Error(body.error || t("httpError", { status: response.status }));
    error.status = response.status;
    throw error;
  }
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

function comfyRequiresKey(rawUrl) {
  if (!rawUrl) return false;
  try {
    const parsed = new URL(rawUrl);
    const host = (parsed.hostname || "").toLowerCase();
    if (["127.0.0.1", "localhost", "::1", "[::1]"].includes(host)) return false;
    return parsed.protocol === "https:";
  } catch {
    return true;
  }
}

function clearRuntimePoll() {
  if (runtimePollTimer && typeof clearTimeout === "function") {
    clearTimeout(runtimePollTimer);
  }
  runtimePollTimer = null;
}

function scheduleRuntimePoll(statusObj) {
  clearRuntimePoll();
  if (!statusObj || !["provisioning", "awaiting_drive_auth", "starting_comfy", "stopping"].includes(statusObj.state)) {
    return;
  }
  if (typeof setTimeout === "function") {
    runtimePollTimer = setTimeout(() => {
      refreshRuntimeStatus().catch(() => {});
    }, 2500);
  }
}

function renderRuntimeStatus() {
  const statusObj = state.runtimeStatus;
  if (!statusObj) return;
  const st = statusObj.state || "offline";
  const gpu = statusObj.gpu || state.config?.COLAB_GPU || "G4";
  const badge = $("runtime-badge");
  badge.textContent = t(`runtimeState_${st}`, { gpu });
  badge.className = "status-badge";
  if (st === "online") badge.classList.add("succeeded");
  else if (["provisioning", "awaiting_drive_auth", "starting_comfy", "stopping"].includes(st)) badge.classList.add("running");
  else if (["error", "orphan_warning"].includes(st)) badge.classList.add("failed");
  $("runtime-start").textContent = t("runtimeStartGpu", { gpu });

  const configuredUrl = statusObj.comfyui_url || state.config?.COMFYUI_URL || "";
  if (st === "online") {
    const device = statusObj.devices?.[0]?.name || t("runtimeDefaultDevice");
    $("runtime-detail").textContent = t("runtimeDetailOnline", { url: configuredUrl, device });
  } else if (st === "offline") {
    $("runtime-detail").textContent = t(configuredUrl ? "runtimeDetailOffline" : "runtimeDetailNoUrl", { gpu });
  } else if (st === "cli_unavailable") {
    $("runtime-detail").textContent = t("runtimeDetailCliUnavailable");
  } else if (st === "provisioning") {
    $("runtime-detail").textContent = t("runtimeDetailProvisioning", { gpu });
  } else if (st === "awaiting_drive_auth") {
    $("runtime-detail").textContent = t("runtimeDetailDriveAuth");
  } else if (st === "starting_comfy") {
    $("runtime-detail").textContent = t("runtimeDetailStartingComfy");
  } else if (st === "stopping") {
    $("runtime-detail").textContent = t("runtimeDetailStopping");
  } else if (st === "orphan_warning") {
    const sessions = (statusObj.orphan_sessions || []).map((item) => item.endpoint).join(", ");
    $("runtime-detail").textContent = t("runtimeDetailOrphan", { sessions, gpu });
  } else {
    $("runtime-detail").textContent = t("runtimeDetailError", { error: statusObj.error || st });
  }

  const awaitingAuth = st === "awaiting_drive_auth";
  $("runtime-drive-box").hidden = !awaitingAuth;
  if (awaitingAuth && statusObj.auth_url) {
    $("runtime-drive-link").href = statusObj.auth_url;
    $("runtime-drive-link").hidden = false;
  } else {
    $("runtime-drive-link").hidden = true;
  }
  $("runtime-drive-ack").disabled = state.runtimeBusy || !awaitingAuth;

  const transitional = ["provisioning", "awaiting_drive_auth", "starting_comfy", "stopping"].includes(st);
  const cliReady = Boolean(statusObj.cli_available);
  $("runtime-refresh").disabled = state.runtimeBusy;
  $("runtime-start").disabled = state.runtimeBusy || !cliReady || st === "online" || transitional || !configuredUrl;
  $("runtime-stop").disabled = state.runtimeBusy || !cliReady || st === "stopping" || (st === "offline" && !statusObj.has_session);

  const logs = statusObj.logs || [];
  $("runtime-logs-section").hidden = logs.length === 0;
  $("runtime-logs").textContent = logs.join("\n");
}

async function refreshRuntimeStatus() {
  if ((state.config?.VIDEO_PROVIDER || "runninghub") !== "comfyui") return;
  const statusObj = await request("/api/runtime/status");
  state.runtimeStatus = statusObj;
  renderRuntimeStatus();
  scheduleRuntimePoll(statusObj);
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
  localStorage.setItem("h3-workbench-language", language);
  renderConfig();
  renderPage(previousSample);
}

function renderVideoProvider() {
  const provider = $("video-provider").value || state.config?.VIDEO_PROVIDER || "runninghub";
  const comfy = provider === "comfyui";
  $("rh-key-field").hidden = comfy;
  $("rh-key").disabled = comfy;
  for (const [fieldId, inputId] of [
    ["comfy-url-field", "comfy-url"],
    ["comfy-key-field", "comfy-key"],
    ["comfy-preset-field", "comfy-preset"],
    ["comfy-resolution-field", "comfy-resolution"],
    ["comfy-upscale-field", "comfy-upscale"],
    ["colab-gpu-field", "colab-gpu"],
    ["colab-auto-start-field", "colab-auto-start"],
  ]) {
    $(fieldId).hidden = !comfy;
    $(inputId).disabled = !comfy;
  }
}

function renderLlmProvider(fillInputs = false) {
  const provider = $("llm-provider").value;
  const gemini = provider === "gemini";
  const prefix = gemini ? "GEMINI" : "LLM";
  $("llm-key-field").hidden = gemini;
  $("llm-key").disabled = gemini;
  $("gemini-key-field").hidden = !gemini;
  $("gemini-key").disabled = !gemini;
  $("llm-base").disabled = gemini;
  $("llm-composition-model").name = `${prefix}_COMPOSITION_MODEL`;
  $("llm-rewrite-model").name = `${prefix}_REWRITE_MODEL`;
  if (fillInputs) {
    const profile = llmDrafts[provider] || state.config.llm_providers[provider];
    $("llm-composition-model").value = profile.composition_model;
    $("llm-rewrite-model").value = profile.rewrite_model;
    $("llm-base").value = profile.base_url;
  }
  llmProvider = provider;
}

function renderConfig(fillInputs = false) {
  const config = state.config;
  if (!config) return;
  const keys = config.keys || {};
  const cfgVideoProvider = config.VIDEO_PROVIDER || "runninghub";
  const activeKeyNames = config.llm_providers?.[config.LLM_PROVIDER]?.key_names
    || (config.LLM_PROVIDER === "gemini" ? ["GEMINI_API_KEY"] : ["LLM_API_KEY", "APIMART_API_KEY"]);
  const apimartFallback = (config.llm_providers?.apimart?.key_names || ["LLM_API_KEY", "APIMART_API_KEY"]).includes("APIMART_API_KEY");
  const textReady = activeKeyNames.some((name) => keys[name]);
  const needsComfyKey = config.comfy_requires_key ?? comfyRequiresKey(config.COMFYUI_URL);
  const videoReady = cfgVideoProvider === "comfyui"
    ? Boolean(config.COMFYUI_URL) && (!needsComfyKey || Boolean(keys.COMFYUI_API_KEY))
    : Boolean(keys.RUNNINGHUB_API_KEY);
  const complete = videoReady && keys.APIMART_API_KEY && textReady && config.ffmpeg && config.ffprobe;
  $("config-summary").textContent = t(complete ? "configReady" : "configIncomplete");
  $("rh-key-status").textContent = t(keys.RUNNINGHUB_API_KEY ? "keyPresent" : "keyMissing");
  $("comfy-key-status").textContent = t(keys.COMFYUI_API_KEY ? "keyPresent" : (config.COMFYUI_URL && !needsComfyKey ? "keyOptionalLocal" : "keyMissing"));
  $("apimart-key-status").textContent = t(keys.APIMART_API_KEY ? "keyPresent" : "keyMissing");
  $("llm-key-status").textContent = t(keys.LLM_API_KEY ? "keyPresent" : apimartFallback ? "llmFallback" : "keyMissing");
  $("gemini-key-status").textContent = t(keys.GEMINI_API_KEY ? "keyPresent" : "keyMissing");
  $("config-storage").textContent = t(config.saved_to_env ? "configOnDisk" : "configInMemory");
  const environment = $("environment");
  environment.replaceChildren(element("span", "ok", t("pythonVersion", { version: config.python })));
  for (const name of ["ffmpeg", "ffprobe"]) {
    environment.append(element("span", config[name] ? "ok" : "missing", t(config[name] ? "environmentPresent" : "environmentMissing", { name })));
  }
  if (fillInputs) {
    llmDrafts = {};
    $("video-provider").value = cfgVideoProvider;
    $("comfy-url").value = config.COMFYUI_URL || "";
    $("comfy-preset").value = config.COMFYUI_PRESET || "full20";
    $("comfy-resolution").value = config.COMFYUI_RESOLUTION || "768p";
    $("comfy-upscale").value = config.COMFYUI_UPSCALE ?? "0";
    $("colab-gpu").value = config.COLAB_GPU || "G4";
    $("colab-auto-start").value = config.COLAB_AUTO_START || "0";
    $("llm-provider").value = config.LLM_PROVIDER;
  }
  renderVideoProvider();
  renderLlmProvider(fillInputs);

  const showRuntime = cfgVideoProvider === "comfyui";
  $("runtime-card").hidden = !showRuntime;
  if (showRuntime) {
    if (fillInputs || !state.runtimeStatus) {
      refreshRuntimeStatus().catch(() => {});
    } else {
      renderRuntimeStatus();
    }
  } else {
    clearRuntimePoll();
    $("runtime-confirm").hidden = true;
  }
}

$("video-provider").addEventListener("change", () => {
  renderVideoProvider();
});

$("llm-provider").addEventListener("change", () => {
  if (!state.config) return;
  llmDrafts[llmProvider] = {composition_model: $("llm-composition-model").value,
    rewrite_model: $("llm-rewrite-model").value, base_url: $("llm-base").value};
  renderLlmProvider(true);
});

$("runtime-refresh").addEventListener("click", async () => {
  state.runtimeBusy = true;
  renderRuntimeStatus();
  try { await refreshRuntimeStatus(); }
  catch (error) { showError(error); }
  finally { state.runtimeBusy = false; renderRuntimeStatus(); }
});

$("runtime-start").addEventListener("click", async () => {
  state.runtimeBusy = true;
  $("runtime-confirm").hidden = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    state.runtimeStatus = await post("/api/runtime/start", {});
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
    const gpu = state.runtimeStatus?.gpu || state.config?.COLAB_GPU || "G4";
    showMessage(t("runtimeStartedNotice", { gpu }));
  } catch (error) {
    showError(error);
  } finally {
    state.runtimeBusy = false;
    renderRuntimeStatus();
  }
});

$("runtime-drive-ack").addEventListener("click", async () => {
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const res = await post("/api/runtime/drive-ack", {});
    if (res.status) state.runtimeStatus = res.status;
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
  } catch (error) {
    showError(error);
  } finally {
    state.runtimeBusy = false;
    renderRuntimeStatus();
  }
});

async function submitRuntimeStop(force) {
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const res = await post("/api/runtime/stop", { force });
    if (res.status) state.runtimeStatus = res.status;
    if (res.confirmation_required) {
      $("runtime-confirm-text").textContent = t("runtimeStopConfirmWarning", {
        local: t(res.local_busy ? "runtimeBoolYes" : "runtimeBoolNo"),
        queue: res.remote_queue ?? 0,
      });
      $("runtime-confirm").hidden = false;
    } else {
      $("runtime-confirm").hidden = true;
      showMessage(t("runtimeStoppedNotice"));
    }
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
  } catch (error) {
    showError(error);
  } finally {
    state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

$("runtime-stop").addEventListener("click", async () => {
  await submitRuntimeStop(false);
});

$("runtime-confirm-stop").addEventListener("click", async () => {
  await submitRuntimeStop(true);
});

$("runtime-cancel-stop").addEventListener("click", () => {
  $("runtime-confirm").hidden = true;
});

$("language").addEventListener("change", async (event) => {
  const language = event.target.value;
  try { await loadLanguage(language); }
  catch (error) {
    $("language").value = state.language;
    showError(t("languageError", { error: error.message }));
  }
});

$("config-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("save-config").disabled = true;
  clearNotices();
  try {
    const payload = Object.fromEntries([...new FormData(event.target)].filter(([, value]) => value.trim()).map(([key, value]) => [key, value.trim()]));
    if (!$("comfy-url").disabled && !$("comfy-url").value.trim()) {
      payload.COMFYUI_URL = "";
    }
    payload.persist = $("persist-config").checked;
    state.config = await post("/api/config", payload);
    for (const id of ["rh-key", "comfy-key", "apimart-key", "llm-key", "gemini-key"]) $(id).value = "";
    renderConfig(true);
    showMessage(t(payload.persist ? "configSavedDisk" : "configSaved"));
  } catch (error) { showError(error); }
  finally { $("save-config").disabled = false; }
});

$("refresh-config").addEventListener("click", async () => {
  $("refresh-config").disabled = true;
  try { state.config = (await request("/api/bootstrap")).config; renderConfig(true); showMessage(t("configRefreshed")); }
  catch (error) { showError(error); }
  finally { $("refresh-config").disabled = false; }
});
