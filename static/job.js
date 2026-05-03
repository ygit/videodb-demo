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
  indexing: "indexing transcript",
  reeling: "generating reels",
  done: "done",
  completed_with_errors: "done with errors",
  error: "failed",
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

function renderJob(container, payload) {
  const stateEl = container.querySelector("[data-job-state]");
  const metaEl = container.querySelector("[data-job-meta]");
  const errorEl = container.querySelector("[data-job-error]");
  const reelsEl = container.querySelector("[data-reels]");

  renderProgress(container, payload);

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
  }
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
