// Shared rendering for a job's status payload.
// Used by templates/job.html (single job) and templates/batch.html (M jobs).

const TERMINAL_STATES = new Set(["done", "error", "completed_with_errors"]);

function el(tag, opts = {}) {
  const node = document.createElement(tag);
  if (opts.text != null) node.textContent = String(opts.text);
  if (opts.className) node.className = opts.className;
  return node;
}

function renderReel(li, reel) {
  while (li.firstChild) li.removeChild(li.firstChild);

  const header = el("div", { className: "reel-header" });

  const title = el("div");
  const strong = el("strong", { text: reel.query == null ? "" : reel.query });
  title.appendChild(strong);
  header.appendChild(title);

  const status = el("div");
  status.appendChild(document.createTextNode("state: "));
  status.appendChild(el("code", { text: reel.state || "unknown" }));
  if (typeof reel.start === "number" && typeof reel.end === "number") {
    status.appendChild(document.createTextNode(
      ` — ${reel.start.toFixed(1)}s → ${reel.end.toFixed(1)}s`
    ));
  }
  if (reel.error) {
    status.appendChild(document.createTextNode(" — "));
    status.appendChild(el("span", { className: "error", text: reel.error }));
  }
  header.appendChild(status);

  if (reel.matched_text) {
    const matched = el("div", { className: "muted" });
    matched.textContent = `matched: “${reel.matched_text}”`;
    header.appendChild(matched);
  }

  li.appendChild(header);

  if (reel.stream_url) {
    const video = el("video");
    video.controls = true;
    video.preload = "metadata";
    video.src = reel.stream_url;
    li.appendChild(video);

    const links = el("div", { className: "links" });
    const playerLink = el("a", { text: "open in VideoDB player" });
    playerLink.href = reel.player_url;
    playerLink.target = "_blank";
    playerLink.rel = "noopener";
    links.appendChild(playerLink);
    links.appendChild(document.createTextNode(" · "));
    const streamLink = el("a", { text: "stream URL" });
    streamLink.href = reel.stream_url;
    streamLink.target = "_blank";
    streamLink.rel = "noopener";
    links.appendChild(streamLink);
    li.appendChild(links);
  }
}

const STATE_PRETTY = {
  queued: "queued",
  uploading: "uploading",
  indexing: "indexing",
  reeling: "generating reels",
  done: "done",
  completed_with_errors: "done with errors",
  error: "failed",
  matching: "searching",
  reframing: "reframing",
  captioning: "burning subtitles",
  transcoding: "transcoding",
  ready: "ready",
  skipped: "skipped",
  pending: "pending",
};

function renderProgress(container, payload) {
  const fill = container.querySelector("[data-progress-fill]");
  const stepsEl = container.querySelector("[data-progress-steps]");
  const progress = payload.progress;
  const state = payload.state;
  if (!progress) return;

  if (fill) {
    fill.style.width = `${Math.max(0, Math.min(100, progress.percent))}%`;
    fill.classList.toggle("is-error", state === "error");
    fill.classList.toggle("is-warning", state === "completed_with_errors");
    fill.classList.toggle("is-active", !["done", "completed_with_errors", "error"].includes(state));
  }

  if (stepsEl) {
    while (stepsEl.firstChild) stepsEl.removeChild(stepsEl.firstChild);
    for (const step of progress.steps || []) {
      const li = el("li", { className: `step step-${step.state}` });
      const dot = el("span", { className: "step-dot" });
      let glyph = "";
      if (step.state === "done") glyph = "✓";
      else if (step.state === "active") glyph = "●";
      else if (step.state === "error") glyph = "✗";
      else glyph = "○";
      dot.textContent = glyph;
      li.appendChild(dot);
      li.appendChild(el("span", { className: "step-label", text: step.label }));
      stepsEl.appendChild(li);
    }
  }
}

function renderVideoInfo(container, payload) {
  const wrap = container.querySelector("[data-video-info]");
  if (!wrap) return;

  const meta = payload.video_meta;
  const hasMeta = meta && (meta.name || meta.thumbnail_url || meta.length_pretty);
  const hasTranscript = !!payload.transcript_preview;

  if (!hasMeta && !hasTranscript) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  while (wrap.firstChild) wrap.removeChild(wrap.firstChild);

  const heading = el("h3", { className: "video-info-title", text: "Source video" });
  wrap.appendChild(heading);

  if (hasMeta) {
    const row = el("div", { className: "video-info-row" });

    if (meta.thumbnail_url) {
      const thumb = el("img", { className: "video-info-thumb" });
      thumb.src = meta.thumbnail_url;
      thumb.alt = "";
      thumb.loading = "lazy";
      row.appendChild(thumb);
    }

    const body = el("div", { className: "video-info-body" });

    const titleLine = el("div", { className: "video-info-name" });
    if (meta.player_url) {
      const a = el("a", { text: meta.name || payload.video_url || "(unnamed video)" });
      a.href = meta.player_url;
      a.target = "_blank";
      a.rel = "noopener";
      titleLine.appendChild(a);
    } else {
      titleLine.textContent = meta.name || payload.video_url || "(unnamed video)";
    }
    body.appendChild(titleLine);

    const chips = el("div", { className: "video-info-chips" });
    const chipBits = [];
    if (meta.length_pretty) chipBits.push(meta.length_pretty);
    if (payload.config && payload.config.language_display) chipBits.push(payload.config.language_display);
    if (payload.transcript_word_count) chipBits.push(`${payload.transcript_word_count.toLocaleString()} words in transcript`);
    chipBits.forEach((bit, i) => {
      if (i > 0) chips.appendChild(document.createTextNode(" · "));
      chips.appendChild(el("span", { className: "muted", text: bit }));
    });
    if (chipBits.length) body.appendChild(chips);

    row.appendChild(body);
    wrap.appendChild(row);
  }

  if (hasTranscript) {
    const det = el("details", { className: "transcript-details" });
    const sum = el("summary", { text: "Transcript preview" });
    det.appendChild(sum);

    const preview = el("p", { className: "transcript-preview" });
    preview.textContent = payload.transcript_preview;
    det.appendChild(preview);

    if (payload.transcript_available) {
      const fullWrap = el("div", { className: "transcript-actions" });
      const btn = el("button", { className: "secondary", text: "Show full transcript" });
      btn.type = "button";
      const fullEl = el("pre", { className: "transcript-full" });
      fullEl.hidden = true;

      btn.addEventListener("click", async () => {
        if (!fullEl.hidden) {
          fullEl.hidden = true;
          btn.textContent = "Show full transcript";
          return;
        }
        if (!fullEl.dataset.loaded) {
          btn.textContent = "Loading…";
          btn.disabled = true;
          try {
            const r = await fetch(`/jobs/${encodeURIComponent(payload.id)}/transcript`);
            if (!r.ok) throw new Error(`HTTP ${r.status}`);
            fullEl.textContent = await r.text();
            fullEl.dataset.loaded = "1";
          } catch (e) {
            fullEl.textContent = "(failed to load transcript)";
          } finally {
            btn.disabled = false;
          }
        }
        fullEl.hidden = false;
        btn.textContent = "Hide full transcript";
      });

      fullWrap.appendChild(btn);
      det.appendChild(fullWrap);
      det.appendChild(fullEl);
    }

    wrap.appendChild(det);
  }
}

function renderJob(container, payload) {
  const stateEl = container.querySelector("[data-job-state]");
  const metaEl = container.querySelector("[data-job-meta]");
  const errorEl = container.querySelector("[data-job-error]");
  const reelsEl = container.querySelector("[data-reels]");

  renderProgress(container, payload);
  renderVideoInfo(container, payload);

  if (stateEl) stateEl.textContent = STATE_PRETTY[payload.state] || payload.state;

  if (metaEl) {
    const cfg = payload.config || {};
    metaEl.textContent = (payload.video_id
      ? `video.id ${payload.video_id} · `
      : ""
    ) + `${cfg.aspect_label || "?"} · ${cfg.mode_label || "?"} · ${cfg.language_display || "?"} · ${cfg.length || "?"}s per reel`;
  }

  if (errorEl) {
    if (payload.error) {
      errorEl.textContent = payload.error;
      errorEl.hidden = false;
    } else {
      errorEl.hidden = true;
    }
  }

  if (reelsEl) {
    const reels = Array.isArray(payload.reels) ? payload.reels : [];
    while (reelsEl.children.length < reels.length) {
      reelsEl.appendChild(el("li", { className: "reel" }));
    }
    while (reelsEl.children.length > reels.length) {
      reelsEl.removeChild(reelsEl.lastChild);
    }
    reels.forEach((reel, i) => {
      try {
        renderReel(reelsEl.children[i], reel);
      } catch (e) {
        console.error("reel render failed", i, e);
      }
    });
    renderNoMatchTips(container, reels, payload.state);
  }
}

const NO_REEL_STATES = new Set(["skipped", "error"]);

function renderNoMatchTips(container, reels, jobState) {
  const reelsEl = container.querySelector("[data-reels]");
  if (!reelsEl) return;
  const finished = ["done", "completed_with_errors", "error"].includes(jobState);
  const allMissing = reels.length > 0 && reels.every(r => NO_REEL_STATES.has(r.state));
  let banner = container.querySelector("[data-no-match-tips]");

  if (!finished || !allMissing) {
    if (banner) banner.remove();
    return;
  }
  if (banner) return;

  banner = el("aside", { className: "tips-banner" });
  banner.dataset.noMatchTips = "true";

  const heading = el("h3", { text: "No matches — try smaller queries" });
  banner.appendChild(heading);

  const intro = el("p");
  intro.textContent =
    "Every reel was skipped, which usually means the query didn't semantically match anything in the transcript. A few things that help:";
  banner.appendChild(intro);

  const list = el("ul");
  for (const item of [
    "Use one short topic per line (e.g. “transfer rumour”), not a full sentence.",
    "Match your query language to the spoken language — if the audio is Hindi/Hinglish, switch the language picker.",
    "Lean on the speaker's vocabulary. If commentators say “best moments”, that hits better than “laughing moments”.",
    "Sometimes the video has very little speech — try a shorter video or one with clearer audio.",
  ]) {
    const li = el("li");
    li.textContent = item;
    list.appendChild(li);
  }
  banner.appendChild(list);

  const action = el("p", { className: "tips-actions" });
  const link = el("a", { text: "← edit queries and try again" });
  link.href = "/";
  action.appendChild(link);
  banner.appendChild(action);

  reelsEl.parentNode.insertBefore(banner, reelsEl);
}

async function pollJob(jobId, container) {
  try {
    const r = await fetch(`/jobs/${encodeURIComponent(jobId)}/status`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const payload = await r.json();
    renderJob(container, payload);
    if (!TERMINAL_STATES.has(payload.state)) {
      setTimeout(() => pollJob(jobId, container), 1500);
    }
  } catch (e) {
    console.warn("status poll failed; retrying", jobId, e);
    setTimeout(() => pollJob(jobId, container), 2000);
  }
}

async function pollBatch(batchId, sectionsByJobId) {
  try {
    const r = await fetch(`/batches/${encodeURIComponent(batchId)}/status`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const payload = await r.json();
    let allTerminal = true;
    for (const job of payload.jobs || []) {
      const container = sectionsByJobId.get(job.id);
      if (container) renderJob(container, job);
      if (!TERMINAL_STATES.has(job.state)) allTerminal = false;
    }
    if (!allTerminal) setTimeout(() => pollBatch(batchId, sectionsByJobId), 1500);
  } catch (e) {
    console.warn("batch poll failed; retrying", batchId, e);
    setTimeout(() => pollBatch(batchId, sectionsByJobId), 2000);
  }
}
