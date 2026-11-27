"use strict";

const $ = (id) => document.getElementById(id);
const savedLanguage = localStorage.getItem("h3-workbench-language");
const state = {
  language: ["zh-CN", "en"].includes(savedLanguage) ? savedLanguage : "zh-CN",
  messages: {}, config: null, jobs: [], ready: false, pollError: false,
  runtimeStatus: null, runtimeBusy: false,
  runtimeAuthSlot: "primary", runtimeStopSlot: "primary", watchingSecondaryStart: false,
};
let renderPage = () => {};
let llmProvider = null;
let llmDrafts = {};
let runtimePollTimer = null;
let runtimeStatusSeq = 0;
let runtimeRefreshInFlight = null;
let runtimeRefreshInFlightSeq = 0;
let cliAuthCancelling = false;

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
  const items = Array.isArray(rawUrl)
    ? rawUrl
    : String(rawUrl).split(/[\r\n,]+/).map((s) => s.trim()).filter(Boolean);
  for (const item of items) {
    try {
      const parsed = new URL(item);
      const host = (parsed.hostname || "").toLowerCase();
      if (["127.0.0.1", "localhost", "::1", "[::1]"].includes(host)) continue;
      if (parsed.protocol === "https:") return true;
    } catch {
      return true;
    }
  }
  return false;
}

function clearRuntimePoll() {
  if (runtimePollTimer && typeof clearTimeout === "function") {
    clearTimeout(runtimePollTimer);
  }
  runtimePollTimer = null;
}

const RUNTIME_TRANSITIONAL = ["awaiting_cli_auth", "verifying_cli_auth", "provisioning", "awaiting_drive_auth", "starting_comfy", "stopping"];

function isTransitionalState(st) {
  return RUNTIME_TRANSITIONAL.includes(st);
}

function getManagedNode(statusObj, slot) {
  const list = Array.isArray(statusObj?.managed_nodes) ? statusObj.managed_nodes : [];
  return list.find((item) => item && item.slot === slot) || null;
}

function scheduleRuntimePoll(statusObj) {
  clearRuntimePoll();
  if (!statusObj) return;
  const managedTransitional = Array.isArray(statusObj.managed_nodes)
    && statusObj.managed_nodes.some((m) => m && isTransitionalState(m.state));
  if (!isTransitionalState(statusObj.state) && !managedTransitional && !isTransitionalState(statusObj.cli_auth?.state)) {
    return;
  }
  if (typeof setTimeout === "function") {
    runtimePollTimer = setTimeout(() => {
      refreshRuntimeStatus().catch(() => {
        if ((state.config?.VIDEO_PROVIDER || "runninghub") === "comfyui") {
          scheduleRuntimePoll(state.runtimeStatus);
        }
      });
    }, 2500);
  }
}

function renderRuntimeNodes(nodes, secondary, gpu, primary = null) {
  const list = $("runtime-nodes");
  if (!list) return;
  const secActive = Boolean(
    secondary
    && (secondary.online || isTransitionalState(secondary.state) || secondary.has_session || secondary.state === "error")
  );
  const rawNodes = Array.isArray(nodes) ? nodes : [];
  if (rawNodes.length <= 1 && !secActive) {
    list.hidden = true;
    if (typeof list.replaceChildren === "function") list.replaceChildren();
    return;
  }
  const items = rawNodes.map((n) => ({
    ...n,
    _isSecondary: Boolean(secondary?.url && n.url === secondary.url),
    _isPrimary: Boolean(primary?.url && n.url === primary.url),
  }));
  if (secActive && !items.some((n) => n._isSecondary)) {
    items.push({
      url: secondary.url || "",
      label: secondary.label || secondary.session || t("runtimeNodeSecondaryTag"),
      online: Boolean(secondary.online),
      auth_failed: Boolean(secondary.auth_failed),
      devices: secondary.devices || [],
      queue_running: secondary.queue_running || 0,
      queue_pending: secondary.queue_pending || 0,
      _isSecondary: true,
      _isPrimary: false,
    });
  }
  const rows = items.map((node) => {
    const li = element("li", "runtime-node-item");
    const isSec = Boolean(node._isSecondary);
    const managed = isSec ? secondary : (node._isPrimary ? primary : null);
    let badgeText = "";
    let badgeClass = "status-badge";
    if (managed && isTransitionalState(managed.state)) {
      badgeText = t(`runtimeState_${managed.state}`, { gpu });
      badgeClass = "status-badge running";
    } else if (managed && managed.state === "error") {
      badgeText = t("runtimeState_error");
      badgeClass = "status-badge failed";
    } else if (node.online) {
      badgeText = t("runtimeNodeOnline");
      badgeClass = "status-badge succeeded";
    } else if (node.auth_failed) {
      badgeText = t("runtimeNodeAuthFailed");
      badgeClass = "status-badge failed";
    } else {
      badgeText = t("runtimeNodeOffline");
    }
    const label = node.label || node.url || "";
    const devName = node.devices?.[0]?.name || "";
    const queueTotal = (Number(node.queue_running) || 0) + (Number(node.queue_pending) || 0);
    const metaParts = [];
    if (isSec) metaParts.push(t("runtimeNodeSecondaryTag"));
    if (devName) metaParts.push(devName);
    if (node.online) metaParts.push(t("runtimeNodeQueue", { count: queueTotal }));
    if (managed?.state === "error" && managed.error) metaParts.push(managed.error);
    const children = [
      element("span", badgeClass, badgeText),
      element("code", "runtime-node-url", label),
      ...(metaParts.length ? [element("span", "hint runtime-node-meta", metaParts.join(" · "))] : []),
    ];
    if (isSec && secondary && (secondary.online || secondary.has_session || isTransitionalState(secondary.state))) {
      const stopBtn = element("button", "button text-button runtime-node-action", t("runtimeStopNode"));
      stopBtn.type = "button";
      stopBtn.disabled = state.runtimeBusy || secondary.state === "stopping";
      stopBtn.addEventListener("click", () => submitRuntimeStop(false, "secondary"));
      children.push(stopBtn);
    }
    li.append(...children);
    return li;
  });
  list.replaceChildren(...rows);
  list.hidden = false;
}

function syncConfigFromRuntimeStatus(statusObj) {
  if (!state.config || !statusObj || !Array.isArray(statusObj.comfyui_urls)) return;
  const joined = statusObj.comfyui_urls.join(",");
  const urlsChanged = joined !== (state.config.COMFYUI_URL || "");
  if (urlsChanged) {
    state.config.COMFYUI_URL = joined;
    state.config.COMFYUI_URLS = statusObj.comfyui_urls.slice();
    const urlInput = $("comfy-url");
    if (urlInput && (!document.activeElement || document.activeElement !== urlInput)) {
      urlInput.value = joined;
    }
  }
  if (Number.isFinite(statusObj.effective_comfyui_concurrency)) {
    state.config.effective_comfyui_concurrency = statusObj.effective_comfyui_concurrency;
  }
}

function renderRuntimeStatus() {
  const statusObj = state.runtimeStatus;
  if (!statusObj) return;
  syncConfigFromRuntimeStatus(statusObj);
  const st = statusObj.state || "offline";
  const gpu = statusObj.gpu || state.config?.COLAB_GPU || "G4";
  const primary = getManagedNode(statusObj, "primary");
  const secondary = getManagedNode(statusObj, "secondary");
  const primaryState = primary?.state || st;
  const badge = $("runtime-badge");
  badge.textContent = t(`runtimeState_${st}`, { gpu });
  badge.className = "status-badge";
  if (st === "online") badge.classList.add("succeeded");
  else if (isTransitionalState(st)) badge.classList.add("running");
  else if (["error", "orphan_warning", "cli_auth_required"].includes(st)) badge.classList.add("failed");
  $("runtime-start").textContent = t("runtimeStartGpu", { gpu });

  const configuredUrl = statusObj.comfyui_url || state.config?.COMFYUI_URL || "";
  const nodes = Array.isArray(statusObj.nodes) ? statusObj.nodes : [];
  const totalCount = Number.isFinite(statusObj.total_count) ? statusObj.total_count : nodes.length;
  const onlineCount = Number.isFinite(statusObj.online_count) ? statusObj.online_count : nodes.filter((n) => n.online).length;
  const isMultiNode = totalCount > 1;
  const cliAuth = statusObj.cli_auth || {};
  const cliAuthState = cliAuth.state || "idle";
  const cliAuthPending = st === "awaiting_cli_auth" || st === "verifying_cli_auth" || ["awaiting_cli_auth", "verifying_cli_auth"].includes(cliAuthState);
  const cliAuthRequired = st === "cli_auth_required" || cliAuthState === "required";
  if (st === "online") {
    if (isMultiNode) {
      const concurrency = state.config?.effective_comfyui_concurrency || onlineCount;
      $("runtime-detail").textContent = t("runtimeDetailMultiOnline", {
        online: onlineCount,
        total: totalCount,
        concurrency: Math.min(concurrency, Math.max(1, onlineCount)),
      });
    } else {
      const device = statusObj.devices?.[0]?.name || t("runtimeDefaultDevice");
      $("runtime-detail").textContent = t("runtimeDetailOnline", { url: configuredUrl, device });
    }
  } else if (st === "offline") {
    if (isMultiNode) {
      $("runtime-detail").textContent = t("runtimeDetailMultiOffline", { total: totalCount, gpu });
    } else {
      $("runtime-detail").textContent = t(configuredUrl ? "runtimeDetailOffline" : "runtimeDetailNoUrl", { gpu });
    }
  } else if (st === "cli_unavailable") {
    $("runtime-detail").textContent = t("runtimeDetailCliUnavailable");
  } else if (st === "cli_auth_required") {
    $("runtime-detail").textContent = cliAuth.error
      ? t("runtimeDetailError", { error: cliAuth.error })
      : t("runtimeDetailCliAuthRequired");
  } else if (st === "awaiting_cli_auth") {
    $("runtime-detail").textContent = t("runtimeDetailCliAuthPending");
  } else if (st === "verifying_cli_auth") {
    $("runtime-detail").textContent = t("runtimeDetailCliAuthVerifying");
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
  renderRuntimeNodes(nodes, secondary, gpu, primary);

  if (state.watchingSecondaryStart && secondary?.online && secondary.url) {
    state.watchingSecondaryStart = false;
    showMessage(t("runtimeSecondaryReadyNotice", { url: secondary.url }));
  } else if (state.watchingSecondaryStart && secondary && ["error", "offline"].includes(secondary.state)) {
    state.watchingSecondaryStart = false;
  }

  const cliAuthUrl = cliAuth.auth_url || (st === "awaiting_cli_auth" ? statusObj.auth_url : null) || "";
  if ($("runtime-cli-auth-start")) {
    $("runtime-cli-auth-start").hidden = !(Boolean(statusObj.cli_available) && cliAuthRequired && !cliAuthPending);
    $("runtime-cli-auth-start").disabled = state.runtimeBusy;
  }
  if ($("runtime-cli-auth-box")) {
    $("runtime-cli-auth-box").hidden = !cliAuthPending;
    if ($("runtime-cli-auth-text")) {
      $("runtime-cli-auth-text").textContent = t(
        st === "verifying_cli_auth" || cliAuthState === "verifying_cli_auth"
          ? "runtimeDetailCliAuthVerifying"
          : "runtimeDetailCliAuthPending",
      );
    }
    if ($("runtime-cli-auth-url")) {
      if ($("runtime-cli-auth-url").value !== cliAuthUrl) $("runtime-cli-auth-url").value = cliAuthUrl;
      $("runtime-cli-auth-url").hidden = !cliAuthUrl;
    }
    if ($("runtime-cli-auth-link")) {
      $("runtime-cli-auth-link").href = cliAuthUrl || "#";
      $("runtime-cli-auth-link").hidden = !cliAuthUrl;
    }
    if ($("runtime-cli-auth-copy")) {
      $("runtime-cli-auth-copy").hidden = !cliAuthUrl;
      $("runtime-cli-auth-copy").disabled = state.runtimeBusy || !cliAuthUrl;
    }
    const verifying = st === "verifying_cli_auth" || cliAuthState === "verifying_cli_auth";
    if ($("runtime-cli-auth-refresh")) $("runtime-cli-auth-refresh").disabled = state.runtimeBusy || verifying;
    if ($("runtime-cli-auth-code")) $("runtime-cli-auth-code").disabled = state.runtimeBusy || verifying;
    if ($("runtime-cli-auth-submit")) $("runtime-cli-auth-submit").disabled = state.runtimeBusy || verifying || !cliAuthUrl;
    if ($("runtime-cli-auth-cancel")) $("runtime-cli-auth-cancel").disabled = cliAuthCancelling;
  }

  let authSlot = null;
  let authUrl = null;
  let authSession = "";
  if (st === "awaiting_drive_auth" || primaryState === "awaiting_drive_auth") {
    authSlot = "primary";
    authUrl = primary?.auth_url || statusObj.auth_url || null;
    authSession = primary?.session || statusObj.session || "";
  } else if (secondary?.state === "awaiting_drive_auth") {
    authSlot = "secondary";
    authUrl = secondary.auth_url || null;
    authSession = secondary.session || "";
  }
  const awaitingAuth = Boolean(authSlot);
  state.runtimeAuthSlot = authSlot || "primary";
  $("runtime-drive-box").hidden = !awaitingAuth;
  if ($("runtime-drive-text")) {
    $("runtime-drive-text").textContent = authSlot === "secondary" && authSession
      ? t("runtimeDetailDriveAuthNode", { session: authSession })
      : t("runtimeDetailDriveAuth");
  }
  if (awaitingAuth && authUrl) {
    $("runtime-drive-link").href = authUrl;
    $("runtime-drive-link").hidden = false;
    if ($("runtime-drive-url")) {
      if ($("runtime-drive-url").value !== authUrl) $("runtime-drive-url").value = authUrl;
      $("runtime-drive-url").hidden = false;
    }
    if ($("runtime-drive-copy")) {
      $("runtime-drive-copy").hidden = false;
      $("runtime-drive-copy").disabled = state.runtimeBusy;
    }
  } else {
    $("runtime-drive-link").hidden = true;
    if ($("runtime-drive-url")) {
      $("runtime-drive-url").value = "";
      $("runtime-drive-url").hidden = true;
    }
    if ($("runtime-drive-copy")) $("runtime-drive-copy").hidden = true;
  }
  $("runtime-drive-ack").disabled = state.runtimeBusy || !awaitingAuth;

  const primaryTransitional = isTransitionalState(primaryState);
  const secondaryTransitional = Boolean(secondary && isTransitionalState(secondary.state));
  const cliReady = Boolean(statusObj.cli_available) && !cliAuthRequired && !cliAuthPending;
  const primaryOnline = primary ? Boolean(primary.online) : st === "online";
  const primaryUrl = primary ? (primary.url || "") : configuredUrl;
  $("runtime-refresh").disabled = state.runtimeBusy;
  $("runtime-start").disabled = state.runtimeBusy || !cliReady || primaryOnline || primaryTransitional || !primaryUrl;
  $("runtime-stop").disabled = state.runtimeBusy || !cliReady || primaryState === "stopping" || (!primaryOnline && !primaryTransitional && !(primary?.has_session ?? statusObj.has_session));
  if ($("runtime-confirm-stop")) $("runtime-confirm-stop").disabled = state.runtimeBusy;

  const startSecBtn = $("runtime-start-secondary");
  if (startSecBtn) {
    const showSecondaryStart = cliReady && (!secondary || (!secondary.online && !secondaryTransitional));
    startSecBtn.hidden = !showSecondaryStart;
    startSecBtn.textContent = t("runtimeStartSecondaryGpu", { gpu });
    startSecBtn.disabled = state.runtimeBusy || !cliReady || secondaryTransitional || !state.config?.keys?.COMFYUI_API_KEY;
  }

  const secLogs = Array.isArray(secondary?.logs) ? secondary.logs : [];
  const logs = (secondaryTransitional || secondary?.state === "error") && secLogs.length
    ? secLogs
    : (statusObj.logs || secLogs);
  $("runtime-logs-section").hidden = logs.length === 0;
  $("runtime-logs").textContent = logs.join("\n");
}

async function refreshRuntimeStatus() {
  if ((state.config?.VIDEO_PROVIDER || "runninghub") !== "comfyui") return;
  if (runtimeRefreshInFlight && runtimeRefreshInFlightSeq === runtimeStatusSeq) {
    return runtimeRefreshInFlight;
  }
  const seq = runtimeStatusSeq;
  const current = (async () => {
    const statusObj = await request("/api/runtime/status");
    if (seq !== runtimeStatusSeq) return;
    state.runtimeStatus = statusObj;
    renderRuntimeStatus();
    scheduleRuntimePoll(statusObj);
  })();
  runtimeRefreshInFlight = current;
  runtimeRefreshInFlightSeq = seq;
  try {
    await current;
  } finally {
    if (runtimeRefreshInFlight === current) {
      runtimeRefreshInFlight = null;
    }
  }
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
    ["comfy-concurrency-field", "comfy-concurrency"],
    ["comfy-preset-field", "comfy-preset"],
    ["comfy-resolution-field", "comfy-resolution"],
    ["comfy-upscale-field", "comfy-upscale"],
    ["colab-gpu-field", "colab-gpu"],
    ["colab-auto-start-field", "colab-auto-start"],
  ]) {
    if ($(fieldId)) $(fieldId).hidden = !comfy;
    if ($(inputId)) $(inputId).disabled = !comfy;
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
    if ($("comfy-concurrency")) $("comfy-concurrency").value = config.COMFYUI_CONCURRENCY || "";
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
  const seq = runtimeStatusSeq;
  state.runtimeBusy = true;
  renderRuntimeStatus();
  try { await refreshRuntimeStatus(); }
  catch (error) {
    if (seq === runtimeStatusSeq) showError(error);
  }
  finally {
    if (seq === runtimeStatusSeq && !cliAuthCancelling) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
});

async function submitRuntimeStart(slot = "primary") {
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  state.runtimeBusy = true;
  $("runtime-confirm").hidden = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const payload = slot === "primary" ? {} : { slot };
    const statusObj = await post("/api/runtime/start", payload);
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    state.runtimeStatus = statusObj;
    if (slot === "secondary") state.watchingSecondaryStart = true;
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
    const gpu = state.runtimeStatus?.gpu || state.config?.COLAB_GPU || "G4";
    showMessage(t(slot === "secondary" ? "runtimeSecondaryStartedNotice" : "runtimeStartedNotice", { gpu }));
  } catch (error) {
    if (slot === "secondary") state.watchingSecondaryStart = false;
    if (seq === runtimeStatusSeq) {
      showError(error);
      scheduleRuntimePoll(state.runtimeStatus);
    }
  } finally {
    if (seq === runtimeStatusSeq) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

$("runtime-start").addEventListener("click", async () => {
  await submitRuntimeStart("primary");
});

if ($("runtime-start-secondary")) {
  $("runtime-start-secondary").addEventListener("click", async () => {
    await submitRuntimeStart("secondary");
  });
}

async function copyAuthLink(url, inputEl) {
  if (!url) return;
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(url);
      showMessage(t("runtimeAuthLinkCopied"));
      return;
    }
  } catch {
    // Fall through to input selection fallback
  }
  if (inputEl && typeof inputEl.select === "function") {
    inputEl.select();
    try {
      if (typeof document.execCommand === "function" && document.execCommand("copy")) {
        showMessage(t("runtimeAuthLinkCopied"));
        return;
      }
    } catch {
      // Fall through to manual copy notice
    }
  }
  showError(t("runtimeAuthLinkCopyFailed"));
}

async function startCliAuthFlow(restart = false) {
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  if (restart) {
    if ($("runtime-cli-auth-code")) $("runtime-cli-auth-code").value = "";
    if (state.runtimeStatus?.cli_auth) state.runtimeStatus.cli_auth.auth_id = null;
  }
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const res = await post("/api/runtime/cli-auth/start", restart ? { restart: true } : {});
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    if (res.status) state.runtimeStatus = res.status;
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
    if (res.already_authenticated) {
      if (res.keyring_warning) showError(res.keyring_warning);
      else showMessage(t("runtimeCliAuthAlreadyReady"));
    }
  } catch (error) {
    if (seq === runtimeStatusSeq) {
      showError(error);
      await refreshRuntimeStatus().catch(() => {
        if (seq === runtimeStatusSeq) scheduleRuntimePoll(state.runtimeStatus);
      });
    }
  } finally {
    if (seq === runtimeStatusSeq && !cliAuthCancelling) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

async function submitCliAuthCode() {
  const input = $("runtime-cli-auth-code");
  const code = (input?.value || "").trim();
  if (!code) {
    showError(t("runtimeCliAuthCodeEmpty"));
    return;
  }
  const authId = state.runtimeStatus?.cli_auth?.auth_id || "";
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const payload = authId ? { code, auth_id: authId } : { code };
    const res = await post("/api/runtime/cli-auth/submit", payload);
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    if (input) input.value = "";
    if (res.status) state.runtimeStatus = res.status;
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
    if (res.authenticated) {
      if (res.keyring_warning) showError(res.keyring_warning);
      else showMessage(t("runtimeCliAuthSuccess"));
    } else {
      showError(res.error || t("runtimeDetailError", { error: "cli_auth_failed" }));
    }
  } catch (error) {
    if (seq === runtimeStatusSeq) {
      if (input) input.value = "";
      showError(error);
      await refreshRuntimeStatus().catch(() => {
        if (seq === runtimeStatusSeq) scheduleRuntimePoll(state.runtimeStatus);
      });
    }
  } finally {
    if (seq === runtimeStatusSeq && !cliAuthCancelling) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

async function cancelCliAuthFlow() {
  if (cliAuthCancelling) return;
  const authId = state.runtimeStatus?.cli_auth?.auth_id || "";
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  cliAuthCancelling = true;
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const payload = authId ? { auth_id: authId } : {};
    const res = await post("/api/runtime/cli-auth/cancel", payload);
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    if ($("runtime-cli-auth-code")) $("runtime-cli-auth-code").value = "";
    if (res.status) state.runtimeStatus = res.status;
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
    if (!res.stale) showMessage(t("runtimeCliAuthCancelled"));
  } catch (error) {
    if (seq === runtimeStatusSeq) {
      showError(error);
      scheduleRuntimePoll(state.runtimeStatus);
    }
  } finally {
    cliAuthCancelling = false;
    if (seq === runtimeStatusSeq) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

if ($("runtime-cli-auth-start")) {
  $("runtime-cli-auth-start").addEventListener("click", async () => {
    await startCliAuthFlow(false);
  });
}

if ($("runtime-cli-auth-refresh")) {
  $("runtime-cli-auth-refresh").addEventListener("click", async () => {
    await startCliAuthFlow(true);
  });
}

if ($("runtime-cli-auth-code")) {
  $("runtime-cli-auth-code").addEventListener("keydown", async (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      if (!$("runtime-cli-auth-submit")?.disabled) await submitCliAuthCode();
    }
  });
}

if ($("runtime-cli-auth-submit")) {
  $("runtime-cli-auth-submit").addEventListener("click", async () => {
    await submitCliAuthCode();
  });
}

if ($("runtime-cli-auth-cancel")) {
  $("runtime-cli-auth-cancel").addEventListener("click", async () => {
    await cancelCliAuthFlow();
  });
}

if ($("runtime-cli-auth-copy")) {
  $("runtime-cli-auth-copy").addEventListener("click", async () => {
    const url = $("runtime-cli-auth-url")?.value || "";
    await copyAuthLink(url, $("runtime-cli-auth-url"));
  });
}

if ($("runtime-drive-copy")) {
  $("runtime-drive-copy").addEventListener("click", async () => {
    const url = $("runtime-drive-url")?.value || $("runtime-drive-link")?.href || "";
    await copyAuthLink(url, $("runtime-drive-url"));
  });
}

$("runtime-drive-ack").addEventListener("click", async () => {
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  state.runtimeBusy = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const payload = state.runtimeAuthSlot === "secondary" ? { slot: "secondary" } : {};
    const res = await post("/api/runtime/drive-ack", payload);
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    if (res.status) state.runtimeStatus = res.status;
    if (res.accepted === false) {
      showError(t("runtimeDetailError", { error: res.state || "drive_ack_rejected" }));
    }
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
  } catch (error) {
    if (seq === runtimeStatusSeq) {
      showError(error);
      scheduleRuntimePoll(state.runtimeStatus);
    }
  } finally {
    if (seq === runtimeStatusSeq) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
});

async function submitRuntimeStop(force, slot = "primary") {
  let seq = ++runtimeStatusSeq;
  clearRuntimePoll();
  state.runtimeBusy = true;
  $("runtime-confirm").hidden = true;
  clearNotices();
  renderRuntimeStatus();
  try {
    const payload = slot === "primary" ? { force } : { force, slot };
    const res = await post("/api/runtime/stop", payload);
    if (seq !== runtimeStatusSeq) return;
    seq = ++runtimeStatusSeq;
    if (res.status) state.runtimeStatus = res.status;
    if (res.confirmation_required) {
      state.runtimeStopSlot = slot;
      $("runtime-confirm-text").textContent = t("runtimeStopConfirmWarning", {
        local: t(res.local_busy ? "runtimeBoolYes" : "runtimeBoolNo"),
        queue: res.queue_unknown ? t("runtimeQueueUnknown") : (res.remote_queue ?? 0),
      });
      $("runtime-confirm").hidden = false;
    } else {
      $("runtime-confirm").hidden = true;
      showMessage(t("runtimeStoppedNotice"));
    }
    renderRuntimeStatus();
    scheduleRuntimePoll(state.runtimeStatus);
  } catch (error) {
    if (seq === runtimeStatusSeq) {
      showError(error);
      await refreshRuntimeStatus().catch(() => {
        if (seq === runtimeStatusSeq) scheduleRuntimePoll(state.runtimeStatus);
      });
    }
  } finally {
    if (seq === runtimeStatusSeq) state.runtimeBusy = false;
    renderRuntimeStatus();
  }
}

$("runtime-stop").addEventListener("click", async () => {
  await submitRuntimeStop(false, "primary");
});

$("runtime-confirm-stop").addEventListener("click", async () => {
  await submitRuntimeStop(true, state.runtimeStopSlot || "primary");
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
    if ($("comfy-concurrency") && !$("comfy-concurrency").disabled && !$("comfy-concurrency").value.trim()) {
      payload.COMFYUI_CONCURRENCY = "";
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
