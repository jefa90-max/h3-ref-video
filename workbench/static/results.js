"use strict";

Object.assign(state, {
  jobId: $("results-page").dataset.jobId, selectedJob: null, submitting: false,
  page: 1, pageSize: 12, frameDrafts: new Map(), expandedFrames: new Set(),
  storyboardView: "list", frameIndex: 0,
  frameReferenceDrafts: new Map(), frameSubmissions: new Map(),
  storyboardSignature: "", planSignature: "", linksSignature: "", videoSignature: "",
  player: null, pollTimer: null, jobRevision: 0,
  resultTab: null, lastVideoOperation: null, segmentPage: 1, segmentPageSize: 6, segmentCards: new Map(),
});

function canGenerateVideo(job) {
  if (!job?.can_continue || job.status !== "succeeded" || !job.review?.version) return false;
  const frames = job.storyboard || [];
  return job.review.kind === "script" || (frames.length === job.review.total && frames.every((frame) => frame.url));
}

function renderResultActions() {
  const job = state.selectedJob;
  if (!job) return;
  const locked = state.submitting || running(job);
  $("resume-job").disabled = locked;
  $("continue-job").disabled = locked || !canGenerateVideo(job);
  $("assemble-job").disabled = locked || !job.can_assemble;
  document.querySelectorAll(".regenerate-segment").forEach((button) => {
    button.disabled = locked || button.dataset.enabled !== "true";
  });
  document.querySelectorAll(".regenerate-frame").forEach((button) => {
    button.disabled = locked || button.dataset.enabled !== "true";
  });
  document.querySelectorAll(".frame-reference-action").forEach((control) => {
    control.disabled = locked || control.dataset.enabled !== "true";
  });
  document.querySelectorAll(".frame-prompt").forEach((editor) => { editor.disabled = locked; });
}

function renderStoryboard(job) {
  const frames = job.storyboard || [];
  for (const frame of frames) {
    const submitted = state.frameSubmissions.get(frame.key);
    if (!submitted || job.status !== "succeeded" || frame.version === submitted.version) continue;
    const paths = (frame.references || []).filter((reference) => reference.kind === "character").map((reference) => reference.path);
    if (frame.prompt !== submitted.prompt || JSON.stringify(paths) !== JSON.stringify(submitted.character_reference_paths)) continue;
    if (state.frameDrafts.get(frame.key)?.trim() === submitted.prompt) state.frameDrafts.delete(frame.key);
    if (JSON.stringify(frameCharacterReferences(frame).map((reference) => reference.path)) === JSON.stringify(paths)) state.frameReferenceDrafts.delete(frame.key);
    state.frameSubmissions.delete(frame.key);
  }
  $("storyboard-section").hidden = job.review?.kind !== "frames";
  const pages = Math.max(1, Math.ceil(frames.length / state.pageSize));
  state.page = Math.max(1, Math.min(state.page, pages));
  state.frameIndex = Math.max(0, Math.min(state.frameIndex, frames.length - 1));
  const carousel = state.storyboardView === "carousel";
  const visible = carousel ? frames.slice(state.frameIndex, state.frameIndex + 1)
    : frames.slice((state.page - 1) * state.pageSize, state.page * state.pageSize);
  for (const view of ["list", "carousel"]) {
    $(`storyboard-view-${view}`).setAttribute("aria-pressed", String(state.storyboardView === view));
  }
  $("review-page-hint").textContent = t(carousel ? "reviewCarouselHint" : "reviewPageHint");
  $("storyboard-cards").className = carousel ? "storyboard-carousel" : "storyboard-grid";
  $("page-pagination").hidden = carousel;
  $("page-label").textContent = t("reviewPageLabel", { page: state.page, pages, count: frames.length });
  renderPagination("page", state.page, pages);
  $("frame-navigation").hidden = !carousel || !frames.length;
  $("frame-position").textContent = t("framePosition", { index: frames.length ? state.frameIndex + 1 : 0, count: frames.length });
  $("frame-previous").disabled = state.frameIndex <= 0;
  $("frame-next").disabled = state.frameIndex >= frames.length - 1;
  $("review-empty").hidden = visible.length > 0;
  $("review-empty").textContent = t("framesNotReady");
  const signature = JSON.stringify([visible, job.character_references, job.status, state.language, state.page, state.storyboardView, state.frameIndex]);
  if (state.storyboardSignature === signature) return;
  state.storyboardSignature = signature;
  $("storyboard-cards").replaceChildren();
  for (const frame of visible) $("storyboard-cards").append(createStoryboardCard(frame, job, carousel));
}

function createStoryboardCard(frame, job, carousel) {
  const card = element("article", "storyboard-card");
  const heading = element("div", "label-heading");
  const name = t("frameTitle", { segment: frame.segment, shot: frame.shot });
  heading.append(element("h3", "", name));
  const body = element("div", "storyboard-card-body");
  const media = element("div", "storyboard-media");
  const information = element("div", "storyboard-info");
  body.append(media, information);
  card.append(heading, body);
  if (frame.url) {
    const link = element("a", "storyboard-preview");
    link.href = frame.url;
    link.target = "_blank";
    link.rel = "noopener";
    const image = element("img");
    image.src = frame.url;
    image.loading = carousel ? "eager" : "lazy";
    image.alt = name;
    link.append(image);
    media.append(link);
  } else media.append(element("p", "frame-placeholder hint", t("frameWaiting")));
  if (frame.error) {
    const failureMessage = t(frame.submission_unknown ? "frameSubmissionUnknown" : frame.task_pending ? "frameTaskPending" : frame.moderation_failed ? "frameModerationFailed" : "frameGenerationFailed");
    if (!frame.url) media.replaceChildren(element("p", "frame-placeholder hint", failureMessage));
    information.append(element("p", "hint", failureMessage),
      element("p", "hint", t("frameGenerationReason", { reason: frame.error })));
  }
  const draftNote = element("p", "hint frame-draft-note", t("frameDraftUnapplied"));
  function updateDraftNote() {
    draftNote.hidden = (!state.frameDrafts.has(frame.key) || state.frameDrafts.get(frame.key).trim() === (frame.prompt || "").trim()) && !frameReferencesChanged(frame);
  }
  updateDraftNote();
  const details = element(carousel ? "section" : "details", "frame-editor");
  if (!carousel) details.append(element("summary", "", t("editFramePrompt")));
  let editor;
  function expandEditor() {
    if (editor) return;
    const label = element("label");
    label.append(element("span", "", t("framePromptLabel")));
    editor = element("textarea", "frame-prompt");
    editor.rows = carousel ? 10 : 6;
    editor.value = state.frameDrafts.get(frame.key) ?? frame.prompt;
    editor.addEventListener("input", () => {
      state.frameDrafts.set(frame.key, editor.value);
      updateReferences();
      renderResultActions();
    });
    label.append(editor);
    details.append(label);
    const regenerate = element("button", "button secondary regenerate-frame", t(job.demo ? "regenerateDemo" : "regenerateLive"));
    regenerate.type = "button";
    function updateReferences() {
      regenerate.dataset.enabled = String(frame.references_editable && !running(job) && frameGenerationReferences(frame).every((reference) => reference.url)
        && (!frame.moderation_failed || editor.value.trim() !== (frame.prompt || "").trim()));
      updateDraftNote();
    }
    details.append(createFrameReferenceEditor(frame, updateReferences));
    updateReferences();
    regenerate.addEventListener("click", async () => {
      if (regenerate.disabled) return;
      if (!editor.value.trim()) { showError(t("framePromptRequired")); return; }
      const payload = { prompt: editor.value.trim(), character_reference_paths: frameCharacterReferences(frame).map((reference) => reference.path) };
      state.frameSubmissions.set(frame.key, { ...payload, version: frame.version });
      await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/frames/${encodeURIComponent(frame.key)}`, payload);
    });
    details.append(regenerate, element("p", "hint", t(job.demo ? "regenerateDemoHint" : "regenerateLiveHint")));
    renderResultActions();
  }
  if (carousel) expandEditor();
  else {
    details.addEventListener("toggle", () => {
      if (details.open) { state.expandedFrames.add(frame.key); expandEditor(); }
      else state.expandedFrames.delete(frame.key);
    });
    if (state.expandedFrames.has(frame.key)) { details.open = true; expandEditor(); }
  }
  information.append(details, draftNote);
  return card;
}

function renderScriptPlan(job) {
  const review = job.review || {};
  $("script-section").hidden = review.kind !== "script";
  const plan = review.plan;
  const signature = JSON.stringify([plan, review.version, state.language]);
  if (signature === state.planSignature) return;
  state.planSignature = signature;
  $("script-plan").replaceChildren();
  if (!plan) return;
  $("script-plan").append(element("h3", "", plan.title), element("p", "hint", t("scriptPlanSummary", { seconds: plan.seconds, count: plan.segments.length })));
  for (const segment of plan.segments) {
    const details = element("details", "script-segment");
    details.append(element("summary", "", t("scriptSegment", { index: segment.idx + 1, start: segment.start, end: segment.end, seconds: segment.seconds, count: segment.shots.length })));
    let expanded = false;
    details.addEventListener("toggle", () => {
      if (!details.open || expanded) return;
      expanded = true;
      for (const shot of segment.shots) {
        const item = element("div", "script-shot");
        item.append(element("strong", "", t("scriptShotTime", { start: shot.start, end: shot.end })));
        for (const field of ["action", "camera", "line"]) {
          if (shot[field]) item.append(element("p", "", t(`script_${field}`, { value: shot[field] })));
        }
        details.append(item);
      }
    });
    $("script-plan").append(details);
  }
}

function renderArtifacts(job) {
  const artifacts = job.artifacts || [];
  const final = artifacts.find((file) => file.kind === "video" && file.role === "final");
  $("final-video-section").hidden = !final;
  $("final-video-version").textContent = t(job.pending_video || job.pending_assembly ? "videoOlderVersion" : "videoCurrentVersion");
  const download = $("download-final-video");
  download.hidden = !final;
  if (final) {
    const url = new URL(final.url, window.location.href);
    url.searchParams.set("download", "1");
    download.href = url.href;
    download.download = final.name;
    download.textContent = t(job.pending_video || job.pending_assembly ? "downloadPreviousFinalVideo" : "downloadFinalVideo");
  } else download.removeAttribute("href");
  const videoSignature = JSON.stringify([final?.url, state.language]);
  if (state.videoSignature !== videoSignature) {
    state.videoSignature = videoSignature;
    state.player?.destroy();
    state.player = null;
    $("final-video").replaceChildren();
    if (final) {
      const video = element("video");
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.src = final.url;
      $("final-video").append(video);
      state.player = createVideoPlayer(video);
      video.addEventListener("play", () => {
        for (const card of state.segmentCards.values()) card.video?.pause();
      });
    }
  }
  const framePaths = new Set((job.storyboard || []).map((frame) => frame.path));
  const downloads = artifacts.filter((file) => !framePaths.has(file.path));
  $("artifact-section").hidden = !downloads.length;
  const signature = JSON.stringify(downloads);
  if (state.linksSignature === signature) return;
  state.linksSignature = signature;
  $("artifact-links").replaceChildren();
  for (const file of downloads) {
    const link = element("a", "artifact-link", file.name);
    link.href = file.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.title = file.path;
    $("artifact-links").append(link);
  }
}

function renderJob(job) {
  $("job-loading").hidden = Boolean(job);
  $("job-detail").hidden = !job;
  if (!job) return;
  renderResultTabs(job);
  const review = job.review || {};
  $("job-status").textContent = t(`status_${job.status}`);
  $("job-status").className = `status-badge ${job.status}`;
  $("job-kind").textContent = t(job.kind === "one" ? "oneTitle" : "filmTitle");
  $("job-mode").textContent = t(job.demo ? "demoMode" : "liveMode");
  $("job-operation").textContent = t({ frames: "operationFrames", frame: "operationFrame", segment: "operationSegment", assemble: "operationAssemble" }[job.operation] || "operationPipeline");
  $("job-created").textContent = dateLabel(job.created_at);
  const failed = ["failed", "interrupted"].includes(job.status);
  const activeStage = { pipeline: "stageGeneratingVideo", segment: "stageRegeneratingSegment", assemble: "stageAssembling" }[job.operation] || "stageGeneratingFrames";
  const stage = failed ? "stageFailed" : running(job) ? activeStage : job.frame_failures ? "stageFramesIncomplete" : job.review_needed ? "stageReview" : job.pending_assembly ? "stageAwaitingAssembly" : "stageFinished";
  $("stage-title").textContent = t(stage);
  $("stage-hint").textContent = t(stage === "stageReview" && review.kind === "script" ? "stageScriptReviewHint" : `${stage}Hint`);
  $("job-error").hidden = !job.error;
  $("job-error").textContent = job.error || "";
  $("frames-count").textContent = job.progress?.frames ?? 0;
  $("clips-count").textContent = job.progress?.clips ?? 0;
  $("files-count").textContent = job.artifacts?.length ?? 0;
  $("resume-job").hidden = !failed && !job.can_resume_frames;
  $("resume-job").textContent = t(job.can_resume_frames ? "resumeFrames" : "resumeJob");
  $("resume-hint").hidden = !failed && !job.can_resume_frames;
  $("review-section").hidden = job.kind !== "film";
  $("review-progress").textContent = t(review.kind === "script" ? "scriptPlanForReview" : "reviewSummary", { total: review.total || 0 });
  $("review-badge").textContent = t(review.complete ? "reviewComplete" : "reviewPending");
  $("review-badge").className = `status-badge ${review.complete ? "succeeded" : "idle"}`;
  $("video-stale").hidden = !job.pending_video;
  $("continue-section").hidden = !job.review_needed;
  $("continue-job").textContent = t(review.kind === "script" ? (job.demo ? "confirmScriptDemo" : "confirmScriptLive") : (job.demo ? "confirmFramesDemo" : "confirmFramesLive"));
  $("continue-hint").textContent = t(review.kind === "script" ? "scriptBeforeVideo" : "reviewBeforeVideo");
  $("progress-note").textContent = t(running(job) ? "progressRunning" : job.demo && job.status === "succeeded" ? "progressDemo" : "progressDone");
  $("job-output").textContent = job.output_dir;
  const logs = $("job-logs");
  const atBottom = logs.scrollHeight - logs.scrollTop - logs.clientHeight < 35;
  if (logs.textContent !== (job.logs || t("logsEmpty"))) {
    logs.textContent = job.logs || t("logsEmpty");
    if (atBottom) logs.scrollTop = logs.scrollHeight;
  }
  $("log-live").hidden = !running(job);
  $("report-section").hidden = !job.report;
  $("job-report").textContent = job.report || "";
  renderStoryboard(job);
  renderScriptPlan(job);
  renderArtifacts(job);
  renderVideoSegments(job);
  renderResultActions();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  state.pollTimer = setTimeout(refreshJob, 1400);
}

async function refreshJob() {
  if (state.submitting) return;
  const revision = ++state.jobRevision;
  try {
    const job = await request(`/api/jobs/${encodeURIComponent(state.jobId)}`);
    if (revision !== state.jobRevision) return;
    state.selectedJob = job;
    renderJob(job);
    if (state.pollError) clearNotices();
    if (running(job)) schedulePoll();
  } catch (error) {
    if (revision !== state.jobRevision) return;
    showError(t("pollError", { error: error.message }));
    state.pollError = true;
    schedulePoll();
  }
}

async function runJobAction(url, payload) {
  if (state.submitting || running(state.selectedJob)) return;
  clearTimeout(state.pollTimer);
  state.jobRevision += 1;
  state.submitting = true;
  clearNotices();
  renderResultActions();
  let failed = false;
  try {
    state.selectedJob = await post(url, payload);
    renderJob(state.selectedJob);
    if (running(state.selectedJob)) schedulePoll();
  } catch (error) { failed = true; showError(error); }
  finally { state.submitting = false; renderResultActions(); }
  if (failed) await refreshJob();
}

initializePagination("page", (page) => {
  state.page = page;
  renderStoryboard(state.selectedJob);
  renderResultActions();
  $("storyboard-section").scrollIntoView({ block: "start" });
});
for (const view of ["list", "carousel"]) {
  $(`storyboard-view-${view}`).addEventListener("click", () => {
    if (!state.selectedJob || state.storyboardView === view) return;
    if (view === "list") state.page = Math.floor(state.frameIndex / state.pageSize) + 1;
    else if (Math.floor(state.frameIndex / state.pageSize) + 1 !== state.page) {
      state.frameIndex = (state.page - 1) * state.pageSize;
    }
    state.storyboardView = view;
    renderStoryboard(state.selectedJob);
    renderResultActions();
  });
}
for (const [id, direction] of [["frame-previous", -1], ["frame-next", 1]]) {
  $(id).addEventListener("click", () => {
    if ($(id).disabled || !state.selectedJob) return;
    state.frameIndex += direction;
    renderStoryboard(state.selectedJob);
    renderResultActions();
  });
}
document.addEventListener("keydown", (event) => {
  if (!["ArrowLeft", "ArrowRight"].includes(event.key) || event.defaultPrevented || event.isComposing
    || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
  if (state.storyboardView !== "carousel" || $("storyboard-panel").hidden || $("storyboard-section").hidden) return;
  if (event.target.isContentEditable || event.target.closest("input, textarea, select")) return;
  const button = $(event.key === "ArrowLeft" ? "frame-previous" : "frame-next");
  if (button.disabled) return;
  event.preventDefault();
  button.click();
});
$("continue-job").addEventListener("click", async () => {
  const job = state.selectedJob;
  if (canGenerateVideo(job)) await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/continue`, { review_version: job.review.version, approved: true });
});
$("resume-job").addEventListener("click", async () => {
  if (state.selectedJob) await runJobAction(`/api/jobs/${encodeURIComponent(state.jobId)}/resume`, {});
});
renderPage = () => renderJob(state.selectedJob);

async function initializeResults() {
  try {
    await loadLanguage(state.language);
    state.config = (await request("/api/bootstrap")).config;
    renderConfig(true);
    await refreshJob();
  } catch (error) { showError(error); }
}
initializeVideoSegments();
initializeResults();
