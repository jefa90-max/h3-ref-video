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

function renderVideoSegments(job) {
  $("segments-section").hidden = job.kind !== "film";
  if (job.kind !== "film") return;
  const segments = job.segments || [];
  const pages = Math.max(1, Math.ceil(segments.length / state.segmentPageSize));
  state.segmentPage = Math.min(state.segmentPage, pages);
  $("segments-summary").textContent = t("segmentsSummary", { ready: segments.filter((segment) => segment.url).length, total: segments.length });
  $("segments-page-label").textContent = t("segmentPageLabel", { page: state.segmentPage, pages, count: segments.length });
  $("segments-previous").disabled = state.segmentPage <= 1;
  $("segments-next").disabled = state.segmentPage >= pages;
  $("assembly-notice").hidden = !job.pending_video && !job.pending_assembly;
  $("assembly-notice").textContent = t(job.pending_video ? "segmentsNeedFrames" : "assemblyPending");
  $("assembly-hint").textContent = t(job.pending_video ? "segmentsNeedFrames" : job.assembly?.ready ? "assemblyHint" : "assemblyMissing");
  $("assemble-job").disabled = state.submitting || running(job) || !job.can_assemble;
  if (state.resultTab !== "segments") return;
  const visible = segments.slice((state.segmentPage - 1) * state.segmentPageSize, state.segmentPage * state.segmentPageSize);
  const numbers = new Set(visible.map((segment) => segment.number));
  for (const [number, card] of state.segmentCards) {
    if (!numbers.has(number)) { removeSegmentCard(card); state.segmentCards.delete(number); }
  }
  for (const segment of visible) {
    const signature = JSON.stringify([segment.url, state.language]);
    let card = state.segmentCards.get(segment.number);
    if (card && card.signature !== signature) {
      removeSegmentCard(card);
      state.segmentCards.delete(segment.number);
      card = null;
    }
    if (!card) {
      const article = element("article", "segment-card");
      const heading = element("div", "label-heading");
      const status = element("span", "tag");
      heading.append(element("h3", "", t("segmentTitle", { number: segment.number })), status);
      article.append(heading, element("p", "hint", t("segmentDetails", { start: segment.start, end: segment.end, seconds: segment.seconds, count: segment.shots })));
      let video = null;
      if (segment.url) {
        const preview = element("div", "artifact video");
        video = element("video");
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.src = segment.url;
        video.setAttribute("aria-label", t("segmentTitle", { number: segment.number }));
        preview.append(video);
        article.append(preview);
      } else article.append(element("p", "segment-placeholder hint", t("segmentWaiting")));
      const actions = element("div", "segment-actions");
      if (segment.url) {
        const download = element("a", "button secondary", t("downloadSegment"));
        download.href = segment.url;
        download.download = `seg${segment.number}.mp4`;
        actions.append(download);
      }
      const regenerate = element("button", "button secondary regenerate-segment", t(job.demo ? "regenerateSegmentDemo" : "regenerateSegmentLive"));
      regenerate.type = "button";
      regenerate.addEventListener("click", async () => {
        const current = state.selectedJob?.segments?.find((item) => item.number === segment.number);
        if (!current?.can_regenerate) return;
        await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/segments/${segment.number}/regenerate`, { version: current.version });
      });
      actions.append(regenerate);
      article.append(actions, element("p", "hint", t(job.demo ? "segmentDemoHint" : "segmentLiveHint")));
      if (segment.chained_next) article.append(element("p", "hint segment-seam", t("segmentSeamHint")));
      $("segment-cards").append(article);
      card = { element: article, video, player: video ? createVideoPlayer(video) : null, status, regenerate, signature };
      if (video) video.addEventListener("play", () => {
        state.player?.pause();
        for (const other of state.segmentCards.values()) if (other.video !== video) other.video?.pause();
      });
      state.segmentCards.set(segment.number, card);
    }
    card.regenerate.dataset.enabled = String(segment.can_regenerate);
    card.regenerate.disabled = state.submitting || running(job) || !segment.can_regenerate;
    card.status.textContent = t(segment.url ? "segmentReady" : "segmentWaiting");
    // Keep existing media elements in place during polling; insert only newly created cards in order.
    const position = visible.findIndex((item) => item.number === segment.number);
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
  for (const [id, direction] of [["segments-previous", -1], ["segments-next", 1]]) {
    $(id).addEventListener("click", () => {
      state.segmentPage += direction;
      renderVideoSegments(state.selectedJob);
      $("segments-section").scrollIntoView({ block: "start" });
    });
  }
  $("assemble-job").addEventListener("click", async () => {
    const job = state.selectedJob;
    if (job?.can_assemble) await runJobAction(`/api/jobs/${encodeURIComponent(job.id)}/assemble`, { version: job.assembly.version });
  });
}
