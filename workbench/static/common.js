"use strict";

const $ = (id) => document.getElementById(id);
const savedLanguage = localStorage.getItem("h3-workbench-language");
const state = {
  language: ["zh-CN", "en"].includes(savedLanguage) ? savedLanguage : "zh-CN",
  messages: {}, config: null, jobs: [], ready: false, pollError: false,
};
let renderPage = () => {};
let llmProvider = null;
let llmDrafts = {};

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
  const activeKeyNames = config.llm_providers?.[config.LLM_PROVIDER]?.key_names
    || (config.LLM_PROVIDER === "gemini" ? ["GEMINI_API_KEY"] : ["LLM_API_KEY", "APIMART_API_KEY"]);
  const apimartFallback = (config.llm_providers?.apimart?.key_names || ["LLM_API_KEY", "APIMART_API_KEY"]).includes("APIMART_API_KEY");
  const textReady = activeKeyNames.some((name) => keys[name]);
  const complete = keys.RUNNINGHUB_API_KEY && keys.APIMART_API_KEY && textReady && config.ffmpeg && config.ffprobe;
  $("config-summary").textContent = t(complete ? "configReady" : "configIncomplete");
  $("rh-key-status").textContent = t(keys.RUNNINGHUB_API_KEY ? "keyPresent" : "keyMissing");
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
    $("llm-provider").value = config.LLM_PROVIDER;
  }
  renderLlmProvider(fillInputs);
}

$("llm-provider").addEventListener("change", () => {
  if (!state.config) return;
  llmDrafts[llmProvider] = {composition_model: $("llm-composition-model").value,
    rewrite_model: $("llm-rewrite-model").value, base_url: $("llm-base").value};
  renderLlmProvider(true);
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
    payload.persist = $("persist-config").checked;
    state.config = await post("/api/config", payload);
    for (const id of ["rh-key", "apimart-key", "llm-key", "gemini-key"]) $(id).value = "";
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
