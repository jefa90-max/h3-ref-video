"use strict";

function createVideoPlayer(video) {
  if (typeof Plyr !== "function") { showError(t("playerUnavailable")); return null; }
  return new Plyr(video, {
    iconUrl: "/static/vendor/plyr/plyr.svg", loadSprite: true,
    blankVideo: "/static/vendor/plyr/blank.mp4", storage: { enabled: false }, i18n: state.messages.player,
    controls: ["play-large", "play", "progress", "current-time", "mute", "volume", "settings", "pip", "fullscreen"],
    settings: ["speed"],
  });
}

function selectResultTab(tab, focus = false) {
  state.resultTab = tab;
  for (const name of ["storyboard", "segments"]) {
    const selected = name === tab;
    const button = $(`tab-${name}`);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
    $(`${name}-panel`).hidden = !selected;
    if (selected && focus) button.focus();
  }
  if (tab !== "segments") {
    state.player?.pause();
    for (const card of state.segmentCards.values()) card.video?.pause();
  }
}

function renderResultTabs(job) {
  const film = job.kind === "film";
  $("result-tabs").hidden = !film;
  if (!film) {
    $("storyboard-panel").hidden = true;
    $("segments-panel").hidden = false;
    return;
  }
  const videoOperation = ["pipeline", "segment", "assemble"].includes(job.operation);
  const operation = `${job.operation}:${job.operation_id || job.created_at}`;
  if (state.resultTab === null) {
    const hasVideo = (job.segments || []).some((segment) => segment.url);
    selectResultTab(videoOperation || hasVideo ? "segments" : "storyboard");
  } else if (videoOperation && operation !== state.lastVideoOperation) {
    selectResultTab("segments");
  }
  state.lastVideoOperation = operation;
}

function removeSegmentCard(card) {
  card.video?.pause();
  card.player?.destroy();
  card.element.remove();
}

function segmentFormat(segment) {
  return state.segmentDrafts.get(segment.number)?.selectedFormat || segment.prompt_format || "source";
}

function segmentSavedText(segment, format) {
  if (format === segment.prompt_format) return segment.prompt || "";
  return format === "source" ? segment.source_prompt || segment.automatic_source_prompt || "" : segment.h3_prompt || "";
}

function segmentDraftEntry(segment) {
  if (!state.segmentDrafts.has(segment.number)) state.segmentDrafts.set(segment.number, { selectedFormat: segmentFormat(segment), drafts: {} });
  return state.segmentDrafts.get(segment.number);
}

function reconcileSegmentSubmissions(job) {
  for (const segment of job.segments || []) {
    const submitted = state.segmentSubmissions.get(segment.number);
    if (!submitted?.revision || segment.applied_revision !== submitted.revision) continue;
    const active = submitted.prompt_format === "source" ? segment.source_prompt : segment.h3_prompt;
    if (active !== submitted.prompt) continue;
    const entry = state.segmentDrafts.get(segment.number);
    if (entry?.drafts[submitted.prompt_format]?.text === submitted.prompt) {
      delete entry.drafts[submitted.prompt_format];
      const card = state.segmentCards.get(segment.number);
      if (card?.editorFormat === submitted.prompt_format) card.editorBaseline = { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
    }
    state.segmentSubmissions.delete(segment.number);
  }
}

function refreshSegmentControls(card, job, syncText = false) {
  const segment = card.segment;
  const format = segmentFormat(segment);
  const draft = state.segmentDrafts.get(segment.number)?.drafts[format];
  const text = draft?.text ?? segmentSavedText(segment, format);
  if (card.editor) {
    const changingFormat = card.editorFormat !== format;
    if (changingFormat || (syncText && document.activeElement !== card.editor && !card.composing)) {
      if (card.editor.value !== text) card.editor.value = text;
      card.editorBaseline = draft || { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
    }
    card.editorFormat = format;
    card.format.value = format;
    card.promptLabel.textContent = t(format === "source" ? "segmentPromptLabelSource" : "segmentPromptLabelH3");
    card.formatLabel.textContent = t("segmentFormatLabel");
    card.format.children[0].textContent = t("segmentFormatSource");
    card.format.children[1].textContent = t("segmentFormatH3");
    card.draftNote.textContent = t("segmentDraftUnapplied");
    card.draftNote.hidden = !draft;
    card.otherDraft.textContent = t("segmentOtherFormatDraft");
    card.otherDraft.hidden = !state.segmentDrafts.get(segment.number)?.drafts[format === "source" ? "h3" : "source"];
    card.activeLabel.textContent = t("segmentActivePrompts");
    card.activeSource.textContent = segment.source_prompt || t("segmentNoActiveSource");
    card.activeH3.textContent = segment.h3_prompt || t("segmentNoActiveH3");
  }
  const baseline = draft || (card.editorFormat === format && card.editorBaseline) || { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
  const conflict = baseline.basePromptVersion !== segment.prompt_version || baseline.baseClipVersion !== segment.version;
  const locked = state.submitting || running(job) || segment.task_pending || segment.submission_unknown || card.composing;
  const enabled = Boolean(segment.can_regenerate && !locked && text.trim() && !conflict);
  card.regenerate.dataset.enabled = String(enabled);
  card.regenerate.disabled = !enabled;
  card.regenerate.textContent = t(segment.terminal_failed ? "segmentRetryCurrent" : job.demo ? "regenerateSegmentDemo" : "regenerateSegmentLive");
  card.cost.textContent = t(job.demo ? "segmentDemoHint" : format === "source" && (text !== segment.source_prompt || !segment.h3_prompt || segment.source_changed) ? "segmentSourceCost" : "segmentVideoCost");
  if (card.editor) {
    card.conflict.textContent = t("segmentDraftConflict");
    card.conflict.hidden = !conflict;
    card.rebase.textContent = t("segmentRebaseDraft");
    card.rebase.hidden = !conflict;
    for (const control of [card.editor, card.format, card.rebase]) {
      control.dataset.enabled = String(Boolean(segment.editable));
      control.disabled = (locked || !segment.editable) && !(control === card.editor && card.composing);
    }
  }
}

function expandSegmentEditor(card) {
  if (card.editor) return;
  const segment = card.segment, prefix = `segment-${segment.number}`;
  const entry = segmentDraftEntry(segment);
  card.formatLabel = element("label"); card.formatLabel.htmlFor = `${prefix}-format`;
  card.format = element("select", "segment-format segment-control"); card.format.id = `${prefix}-format`;
  for (const format of ["source", "h3"]) { const option = element("option"); option.value = format; card.format.append(option); }
  card.promptLabel = element("label"); card.promptLabel.htmlFor = `${prefix}-prompt`;
  card.editor = element("textarea", "segment-prompt segment-control"); card.editor.id = `${prefix}-prompt`; card.editor.rows = 8;
  card.editor.setAttribute("aria-describedby", `${prefix}-draft ${prefix}-conflict ${prefix}-error`);
  card.draftNote = element("p", "hint segment-draft-note"); card.draftNote.id = `${prefix}-draft`;
  card.otherDraft = element("p", "hint");
  card.conflict = element("p", "hint segment-conflict"); card.conflict.id = `${prefix}-conflict`;
  card.rebase = element("button", "button secondary segment-rebase segment-control"); card.rebase.type = "button";
  const active = element("details", "segment-active-prompts"); card.activeLabel = element("summary");
  card.activeSource = element("pre"); card.activeH3 = element("pre"); active.append(card.activeLabel, card.activeSource, card.activeH3);
  card.references = element("div", "segment-references hint");
  card.details.append(card.formatLabel, card.format, card.promptLabel, card.editor, card.draftNote, card.otherDraft, card.conflict, card.rebase, active, card.references);
  card.editor.addEventListener("input", () => {
    const current = card.segment, format = segmentFormat(current), drafts = segmentDraftEntry(current).drafts;
    drafts[format] = { ...(drafts[format] || card.editorBaseline), text: card.editor.value };
    refreshSegmentControls(card, state.selectedJob);
  });
  card.editor.addEventListener("compositionstart", () => { card.composing = true; card.editor.dataset.composing = "true"; refreshSegmentControls(card, state.selectedJob); });
  card.editor.addEventListener("compositionend", () => { card.composing = false; card.editor.dataset.composing = "false"; refreshSegmentControls(card, state.selectedJob); });
  card.format.addEventListener("change", () => {
    entry.selectedFormat = card.format.value;
    if (entry.selectedFormat === "source" && !card.segment.source_prompt && card.segment.prompt_format === "h3" && !entry.drafts.source) {
      entry.drafts.source = { text: card.segment.automatic_source_prompt || "", basePromptVersion: card.segment.prompt_version, baseClipVersion: card.segment.version };
    }
    refreshSegmentControls(card, state.selectedJob, true);
  });
  card.rebase.addEventListener("click", () => {
    if (card.rebase.disabled) return;
    const current = card.segment, format = segmentFormat(current);
    segmentDraftEntry(current).drafts[format] = { text: card.editor.value, basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    refreshSegmentControls(card, state.selectedJob);
  });
  refreshSegmentControls(card, state.selectedJob, true);
}

function updateSegmentMedia(card, segment) {
  if (card.mediaUrl === segment.url && card.mediaLanguage === state.language) return;
  if (card.mediaUrl !== segment.url) {
    card.video?.pause(); card.player?.destroy(); card.media.replaceChildren(); card.video = null; card.player = null;
    if (segment.url) {
      card.video = element("video"); card.video.controls = true; card.video.preload = "metadata"; card.video.playsInline = true; card.video.src = segment.url;
      card.media.append(card.video); card.player = createVideoPlayer(card.video);
      card.video.addEventListener("play", () => { state.player?.pause(); for (const other of state.segmentCards.values()) if (other !== card) other.video?.pause(); });
    } else card.media.append(element("p", "segment-placeholder hint", t("segmentWaiting")));
  } else if (!card.video) {
    card.media.children[0].textContent = t("segmentWaiting");
  } else {
    // Update player translations without replacing the prompt editor or its focus.
    card.player?.destroy(); card.player = createVideoPlayer(card.video);
  }
  card.mediaUrl = segment.url; card.mediaLanguage = state.language;
}

function createSegmentCard(segment) {
  const article = element("article", "segment-card"), heading = element("div", "label-heading");
  const card = { element: article, segment, title: element("h3"), status: element("span", "tag"), description: element("p", "hint"), media: element("div", "artifact video"),
    attemptNote: element("p", "hint segment-attempt"), error: element("p", "hint segment-error"), sourceWarning: element("p", "hint segment-source-warning"), details: element("details", "segment-editor"),
    summary: element("summary"), download: element("a", "button secondary"), regenerate: element("button", "button secondary regenerate-segment"), cost: element("p", "hint"), seam: element("p", "hint segment-seam") };
  heading.append(card.title, card.status); card.attemptNote.setAttribute("aria-live", "polite"); card.attemptNote.setAttribute("role", "status");
  card.error.id = `segment-${segment.number}-error`; card.details.append(card.summary);
  card.details.addEventListener("toggle", () => {
    if (card.details.open) { state.expandedSegments.add(segment.number); expandSegmentEditor(card); renderVideoSegments(state.selectedJob); }
    else state.expandedSegments.delete(segment.number);
  });
  card.regenerate.type = "button";
  card.regenerate.addEventListener("click", async () => {
    if (card.regenerate.disabled) return;
    const current = card.segment, format = segmentFormat(current), draft = state.segmentDrafts.get(current.number)?.drafts[format];
    const text = draft?.text ?? segmentSavedText(current, format);
    const baseline = draft || card.editorBaseline || { basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    const payload = { prompt: text, prompt_format: format, prompt_version: baseline.basePromptVersion, version: baseline.baseClipVersion };
    const submission = { ...payload, revision: null }; state.segmentSubmissions.set(current.number, submission);
    const accepted = await runJobAction(`/api/jobs/${encodeURIComponent(state.selectedJob.id)}/segments/${current.number}/regenerate`, payload);
    if (accepted) {
      const updated = accepted.segments?.find(item => item.number === current.number);
      submission.revision = updated?.attempt_revision || updated?.applied_revision || null;
      renderVideoSegments(accepted);
    }
  });
  const actions = element("div", "segment-actions"); actions.append(card.download, card.regenerate);
  article.append(heading, card.description, card.media, card.attemptNote, card.error, card.sourceWarning, card.details, actions, card.cost, card.seam);
  return card;
}

function renderVideoSegments(job) {
  $("segments-section").hidden = job.kind !== "film";
  if (state.segmentJobId !== job.id) {
    for (const card of state.segmentCards.values()) removeSegmentCard(card);
    state.segmentCards.clear(); state.segmentDrafts.clear(); state.expandedSegments.clear(); state.segmentSubmissions.clear();
    state.segmentPage = 1; state.segmentJobId = job.id;
  }
  if (job.kind !== "film") return;
  reconcileSegmentSubmissions(job);
  const segments = job.segments || [], pages = Math.max(1, Math.ceil(segments.length / state.segmentPageSize));
  state.segmentPage = Math.max(1, Math.min(state.segmentPage, pages));
  $("segments-summary").textContent = t("segmentsSummary", { ready: segments.filter(s => s.url).length, total: segments.length });
  $("segments-page-label").textContent = t("segmentPageLabel", { page: state.segmentPage, pages, count: segments.length });
  renderPagination("segments", state.segmentPage, pages);
  $("assembly-notice").hidden = !job.pending_video && !job.pending_assembly;
  $("assembly-notice").textContent = t(job.pending_video ? "segmentsNeedFrames" : "assemblyPending");
  $("assembly-hint").textContent = t(job.pending_video ? "segmentsNeedFrames" : job.assembly?.ready ? "assemblyHint" : "assemblyMissing");
  $("assemble-job").disabled = state.submitting || running(job) || !job.can_assemble;
  if (state.resultTab !== "segments") return;
  const visible = segments.slice((state.segmentPage - 1) * state.segmentPageSize, state.segmentPage * state.segmentPageSize);
  const numbers = new Set(visible.map(s => s.number));
  for (const [number, card] of state.segmentCards) if (!numbers.has(number)) { removeSegmentCard(card); state.segmentCards.delete(number); }
  for (const [position, segment] of visible.entries()) {
    let card = state.segmentCards.get(segment.number);
    if (!card) { card = createSegmentCard(segment); state.segmentCards.set(segment.number, card); }
    card.segment = segment;
    updateSegmentMedia(card, segment);
    card.title.textContent = t("segmentTitle", { number: segment.number });
    card.description.textContent = t("segmentDetails", { start: segment.start, end: segment.end, seconds: segment.seconds, count: segment.shots });
    card.status.textContent = t(segment.url ? "segmentReady" : "segmentWaiting");
    card.summary.textContent = t("segmentEditPrompt");
    const note = segment.submission_unknown ? "segmentSubmissionUnknown" : segment.task_pending ? "segmentTaskPending" : segment.terminal_failed ? "segmentTerminalFailed" : segment.error ? "segmentFailedAttempt" : "";
    const text = note ? t(note) : ""; if (card.attemptNote.textContent !== text) card.attemptNote.textContent = text;
    card.attemptNote.hidden = !text; card.error.textContent = segment.error || ""; card.error.hidden = !segment.error;
    card.sourceWarning.textContent = t("segmentSourceChanged"); card.sourceWarning.hidden = !segment.source_changed;
    card.download.textContent = t("downloadSegment"); card.download.hidden = !segment.url; card.download.href = segment.url || ""; card.download.download = `seg${segment.number}.mp4`;
    card.seam.textContent = t("segmentSeamHint"); card.seam.hidden = !segment.chained_next;
    card.video?.setAttribute("aria-label", t("segmentTitle", { number: segment.number }));
    if (state.expandedSegments.has(segment.number)) { card.details.open = true; expandSegmentEditor(card); }
    refreshSegmentControls(card, job, true);
    if (card.references) {
      const signature = JSON.stringify([segment.reference_images, segment.board_omitted, segment.omitted_frames, segment.reference_history_known, state.language]);
      if (signature !== card.referenceSignature) {
        card.referenceSignature = signature; card.references.replaceChildren(element("p", "", t("segmentReferencesHeading")));
        if (!segment.reference_history_known) card.references.append(element("p", "", t("segmentReferenceHistoryUnknown")));
        for (const reference of segment.reference_images || []) card.references.append(element("p", "", t(`segmentReference_${reference.kind}`, reference) + (reference.available ? "" : t("segmentReferenceMissing"))));
        if (segment.board_omitted) card.references.append(element("p", "", t("segmentBoardOmitted")));
        if (segment.omitted_frames) card.references.append(element("p", "", t("segmentFramesOmitted", { count: segment.omitted_frames })));
      }
    }
    if ($("segment-cards").children[position] !== card.element) $("segment-cards").insertBefore(card.element, $("segment-cards").children[position] || null);
  }
}

function initializeVideoSegments() {
  for (const name of ["storyboard", "segments"]) {
    $(`tab-${name}`).addEventListener("click", () => {
      selectResultTab(name);
      renderVideoSegments(state.selectedJob);
    });
    $(`tab-${name}`).addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const target = event.key === "Home" ? "storyboard" : event.key === "End" ? "segments" : name === "storyboard" ? "segments" : "storyboard";
      selectResultTab(target, true);
      renderVideoSegments(state.selectedJob);
    });
  }
  initializePagination("segments", (page) => {
    state.segmentPage = page;
    renderVideoSegments(state.selectedJob);
    $("segments-section").scrollIntoView({ block: "start" });
  });
  $("assemble-job").addEventListener("click", async () => {
    const job = state.selectedJob;
    if (job?.can_assemble) await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/assemble`, { version: job.assembly.version });
  });
}
