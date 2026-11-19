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
  const videoOperation = ["pipeline", "segment", "assemble", "rewrite_segment", "rewrite_prompts"].includes(job.operation);
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
  state.segmentRewrites ??= new Map();
  for (const segment of job.segments || []) {
    const submitted = state.segmentSubmissions.get(segment.number);
    if (submitted?.revision && segment.applied_revision === submitted.revision) {
      const active = submitted.prompt_format === "source" ? segment.source_prompt : segment.h3_prompt;
      if (active === submitted.prompt) {
        const entry = state.segmentDrafts.get(segment.number);
        if (entry?.drafts[submitted.prompt_format]?.text === submitted.prompt) {
          delete entry.drafts[submitted.prompt_format];
          const card = state.segmentCards.get(segment.number);
          if (card?.editorFormat === submitted.prompt_format) card.editorBaseline = { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
        }
        state.segmentSubmissions.delete(segment.number);
      }
    }
    const rewritePending = state.segmentRewrites.get(segment.number);
    if (rewritePending && ["failed", "interrupted"].includes(job.status)) {
      state.segmentRewrites.delete(segment.number);
    } else if (rewritePending && job.status === "succeeded") {
      if (segment.h3_prompt && segment.source_prompt === rewritePending.prompt && !segment.source_rewrite_required) {
        const entry = segmentDraftEntry(segment);
        if (entry.drafts.source?.text?.trim() === rewritePending.prompt) {
          delete entry.drafts.source;
        }
        if (!entry.drafts.h3) {
          entry.selectedFormat = "h3";
          const card = state.segmentCards.get(segment.number);
          if (card) {
            card.editorFormat = null;
            card.editorBaseline = { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
          }
        }
      }
      state.segmentRewrites.delete(segment.number);
    }
    if (job.status === "succeeded" && ["rewrite_segment", "rewrite_prompts"].includes(job.operation) && segment.h3_prompt && !segment.source_rewrite_required) {
      const entry = state.segmentDrafts.get(segment.number);
      if (entry?.drafts.source?.text?.trim() === segment.source_prompt) {
        delete entry.drafts.source;
        const card = state.segmentCards.get(segment.number);
        if (card?.editorFormat === "source") {
          card.editorBaseline = { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
        }
      }
    }
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
    const hasClipSnapshot = Boolean(segment.url && segment.prompts_unapplied_to_clip && (segment.clip_source_prompt != null || segment.clip_h3_prompt != null));
    const shownSource = hasClipSnapshot ? segment.clip_source_prompt : segment.source_prompt;
    const shownH3 = hasClipSnapshot ? segment.clip_h3_prompt : segment.h3_prompt;
    card.activeLabel.textContent = t(
      !segment.url ? "segmentSavedPrompts" : (segment.prompts_unapplied_to_clip && !hasClipSnapshot) ? "segmentSavedPromptsPendingVideo" : "segmentActivePrompts",
    );
    card.activeSource.textContent = shownSource || t("segmentNoActiveSource");
    card.activeH3.textContent = shownH3 || t("segmentNoActiveH3");
  }
  const baseline = draft || (card.editorFormat === format && card.editorBaseline) || { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
  const conflict = baseline.basePromptVersion !== segment.prompt_version || baseline.baseClipVersion !== segment.version;
  const locked = state.submitting || running(job) || segment.task_pending || segment.submission_unknown || card.composing;
  const enabled = Boolean(segment.can_regenerate && !locked && text.trim() && !conflict);
  card.regenerate.dataset.enabled = String(enabled);
  card.regenerate.disabled = !enabled;
  card.regenerate.textContent = t(segment.terminal_failed ? "segmentRetryCurrent" : job.demo ? "regenerateSegmentDemo" : "regenerateSegmentLive");
  card.cost.textContent = t(job.demo ? "segmentDemoHint" : format === "source" && (text !== segment.source_prompt || !segment.h3_prompt || segment.source_rewrite_required) ? "segmentSourceCost" : "segmentVideoCost");
  if (card.editor) {
    const sourceDraft = state.segmentDrafts.get(segment.number)?.drafts.source;
    const sourceText = format === "source" ? text : (sourceDraft?.text ?? segmentSavedText(segment, "source"));
    const sourceBaseline = (format === "source" ? draft : sourceDraft) || (card.editorFormat === "source" && card.editorBaseline) || { basePromptVersion: segment.prompt_version, baseClipVersion: segment.version };
    const sourceConflict = sourceBaseline.basePromptVersion !== segment.prompt_version || sourceBaseline.baseClipVersion !== segment.version;
    const shownConflict = conflict || (format === "h3" && Boolean(sourceDraft) && sourceConflict);
    card.conflict.textContent = t(conflict ? "segmentDraftConflict" : "segmentSourceDraftConflict");
    card.conflict.hidden = !shownConflict;
    card.rebase.textContent = t("segmentRebaseDraft");
    card.rebase.hidden = !shownConflict;
    for (const control of [card.editor, card.format, card.rebase]) {
      control.dataset.enabled = String(Boolean(segment.editable));
      control.disabled = (locked || !segment.editable) && !(control === card.editor && card.composing);
    }
    const hasUnsavedH3Draft = format === "h3" && Boolean(state.segmentDrafts.get(segment.number)?.drafts.h3);
    const canRewrite = Boolean((segment.can_rewrite ?? segment.editable) && !locked && sourceText.trim() && !sourceConflict && !hasUnsavedH3Draft);
    card.rewriteH3.textContent = t(job.demo ? "segmentRewriteH3Demo" : "segmentRewriteH3Live");
    card.rewriteH3.dataset.enabled = String(canRewrite);
    card.rewriteH3.disabled = !canRewrite;
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
  const editorActions = element("div", "segment-editor-actions");
  card.rewriteH3 = element("button", "button secondary segment-rewrite-h3 segment-control"); card.rewriteH3.type = "button";
  editorActions.append(card.rewriteH3);
  const active = element("details", "segment-active-prompts"); card.activeLabel = element("summary");
  card.activeSource = element("pre"); card.activeH3 = element("pre"); active.append(card.activeLabel, card.activeSource, card.activeH3);
  card.references = element("div", "segment-references hint");
  card.details.append(card.formatLabel, card.format, card.promptLabel, card.editor, card.draftNote, card.otherDraft, card.conflict, card.rebase, editorActions, active, card.references);
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
    const current = card.segment, format = segmentFormat(current), drafts = segmentDraftEntry(current).drafts;
    if (drafts[format]) {
      drafts[format] = { text: card.editor.value, basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    } else {
      card.editor.value = segmentSavedText(current, format);
      if (card.editorFormat === format) {
        card.editorBaseline = { basePromptVersion: current.prompt_version, baseClipVersion: current.version };
      }
    }
    if (format === "h3" && drafts.source) {
      drafts.source = { ...drafts.source, basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    }
    refreshSegmentControls(card, state.selectedJob);
  });
  card.rewriteH3.addEventListener("click", async () => {
    if (card.rewriteH3.disabled) return;
    state.segmentRewrites ??= new Map();
    const current = card.segment;
    const sourceDraft = state.segmentDrafts.get(current.number)?.drafts.source;
    const sourceText = sourceDraft?.text ?? segmentSavedText(current, "source");
    const trimmedSource = sourceText.trim();
    if (!trimmedSource) return;
    const baseline = sourceDraft || (card.editorFormat === "source" && card.editorBaseline) || { basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    const payload = { prompt: trimmedSource, prompt_format: "source", prompt_version: baseline.basePromptVersion };
    const accepted = await runJobAction(`/api/jobs/${encodeURIComponent(state.selectedJob.id)}/segments/${current.number}/rewrite`, payload);
    if (accepted) {
      const updated = accepted.segments?.find(item => item.number === current.number);
      if (updated?.prompt_version) {
        if (sourceDraft && sourceDraft.text.trim() === trimmedSource) {
          sourceDraft.basePromptVersion = updated.prompt_version;
          sourceDraft.baseClipVersion = updated.version;
        }
        if (card.editorFormat === "source") {
          card.editorBaseline = { basePromptVersion: updated.prompt_version, baseClipVersion: updated.version };
        }
      }
      state.segmentRewrites.set(current.number, {
        prompt: trimmedSource,
        revision: updated?.attempt_revision || updated?.applied_revision || null,
      });
      renderVideoSegments(accepted);
    }
  });
  refreshSegmentControls(card, state.selectedJob, true);
}

function segmentPlaceholderText(segment) {
  if (running(state.selectedJob) && segment.live_progress && typeof formatLiveProgressParts === "function") {
    const { label, meta } = formatLiveProgressParts(segment.live_progress);
    return meta ? `${label} · ${meta}` : label;
  }
  return t("segmentWaiting");
}

function updateSegmentMedia(card, segment) {
  const placeholder = segment.url ? "" : segmentPlaceholderText(segment);
  if (card.mediaUrl === segment.url && card.mediaLanguage === state.language && card.placeholderText === placeholder) return;
  if (card.mediaUrl !== segment.url) {
    card.video?.pause(); card.player?.destroy(); card.media.replaceChildren(); card.video = null; card.player = null;
    if (segment.url) {
      card.video = element("video"); card.video.controls = true; card.video.preload = "metadata"; card.video.playsInline = true; card.video.src = segment.url;
      card.media.append(card.video); card.player = createVideoPlayer(card.video);
      card.video.addEventListener("play", () => { state.player?.pause(); for (const other of state.segmentCards.values()) if (other !== card) other.video?.pause(); });
    } else card.media.append(element("p", "segment-placeholder hint", placeholder));
  } else if (!card.video) {
    card.media.children[0].textContent = placeholder;
  } else {
    // Update player translations without replacing the prompt editor or its focus.
    const currentTime = card.video.currentTime || 0;
    card.video.pause(); card.player?.destroy(); card.media.replaceChildren();
    card.video = element("video"); card.video.controls = true; card.video.preload = "metadata"; card.video.playsInline = true; card.video.src = segment.url;
    if (currentTime > 0) card.video.addEventListener("loadedmetadata", () => { card.video.currentTime = currentTime; }, { once: true });
    card.media.append(card.video); card.player = createVideoPlayer(card.video);
    card.video.addEventListener("play", () => { state.player?.pause(); for (const other of state.segmentCards.values()) if (other !== card) other.video?.pause(); });
  }
  card.mediaUrl = segment.url; card.mediaLanguage = state.language; card.placeholderText = placeholder;
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
    const sourceDraft = state.segmentDrafts.get(current.number)?.drafts.source;
    const rawText = draft?.text ?? segmentSavedText(current, format);
    const preservePairedSource = format === "h3" && !draft && !sourceDraft && Boolean(current.source_prompt) && Boolean(current.h3_prompt) && rawText === current.h3_prompt && !current.source_rewrite_required;
    const submitFormat = preservePairedSource ? "source" : format;
    const text = preservePairedSource ? current.source_prompt : rawText;
    const baseline = draft || card.editorBaseline || { basePromptVersion: current.prompt_version, baseClipVersion: current.version };
    const payload = { prompt: text, prompt_format: submitFormat, prompt_version: baseline.basePromptVersion, version: baseline.baseClipVersion };
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
  state.segmentRewrites ??= new Map();
  if (state.segmentJobId !== job.id) {
    for (const card of state.segmentCards.values()) removeSegmentCard(card);
    state.segmentCards.clear(); state.segmentDrafts.clear(); state.expandedSegments.clear(); state.segmentSubmissions.clear(); state.segmentRewrites.clear();
    state.segmentPage = 1; state.segmentJobId = job.id;
  }
  if (job.kind !== "film") return;
  reconcileSegmentSubmissions(job);
  const segments = job.segments || [], pages = Math.max(1, Math.ceil(segments.length / state.segmentPageSize));
  state.segmentPage = Math.max(1, Math.min(state.segmentPage, pages));
  $("segments-summary").textContent = t("segmentsSummary", { ready: segments.filter(s => s.url).length, total: segments.length });
  $("segments-page-label").textContent = t("segmentPageLabel", { page: state.segmentPage, pages, count: segments.length });
  renderPagination("segments", state.segmentPage, pages);
  const canBatchRewrite = Boolean(job.can_rewrite_prompts && !state.submitting && !running(job));
  const missingCount = Number(job.missing_h3_count || 0);
  const missingKey = job.demo
    ? (missingCount > 0 ? "rewriteMissingPromptsCount" : "rewriteMissingPrompts")
    : (missingCount > 0 ? "rewriteMissingPromptsLiveCount" : "rewriteMissingPromptsLive");
  $("rewrite-missing-prompts").textContent = t(missingKey, { count: missingCount });
  $("rewrite-missing-prompts").disabled = !canBatchRewrite || missingCount <= 0;
  $("rewrite-all-prompts").textContent = t(job.demo ? "rewriteAllPrompts" : "rewriteAllPromptsLive");
  $("rewrite-all-prompts").disabled = !canBatchRewrite || !segments.length;
  $("segments-rewrite-toolbar").className = job.demo ? "segments-rewrite-toolbar" : "segments-rewrite-toolbar live-paid";
  $("segments-rewrite-hint").textContent = t(job.demo ? "segmentsRewriteDemoHint" : "segmentsRewriteHint");
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
    const note = segment.submission_unknown ? "segmentSubmissionUnknown" : segment.task_pending ? "segmentTaskPending" : segment.terminal_failed ? "segmentTerminalFailed" : segment.error ? "segmentFailedAttempt" : (segment.url && segment.prompts_unapplied_to_clip) ? "segmentPromptsSavedNotInVideo" : "";
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
  $("rewrite-missing-prompts").addEventListener("click", async () => {
    const job = state.selectedJob;
    if (!$("rewrite-missing-prompts").disabled && job?.can_rewrite_prompts) {
      await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/segments/rewrite`, { scope: "missing" });
    }
  });
  $("rewrite-all-prompts").addEventListener("click", async () => {
    const job = state.selectedJob;
    if (!$("rewrite-all-prompts").disabled && job?.can_rewrite_prompts) {
      await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/segments/rewrite`, { scope: "all" });
    }
  });
}
