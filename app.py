"""Flask UI for generating short reels from a long-form video via VideoDB.

Run:
    flask --app app run

Then open http://localhost:5000.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote, urlparse

from dotenv import load_dotenv

load_dotenv(".env")

import re

import videodb
from flask import Flask, jsonify, redirect, render_template, request, url_for
from videodb import (
    AudioConfig,
    IndexType,
    ReframeMode,
    SceneExtractionType,
    SearchType,
    TranscodeMode,
    VideoConfig,
)
from videodb.exceptions import InvalidRequestError, VideodbError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("reel-builder")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

try:
    _conn = videodb.connect()
    _coll = _conn.get_collection()
except Exception as exc:
    raise SystemExit(
        f"VideoDB startup failed: {exc}. "
        "Check that VIDEO_DB_API_KEY is set in .env or in your shell environment."
    ) from exc

_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_batches: dict[str, dict[str, Any]] = {}
_batches_lock = threading.Lock()

_video_cache: dict[str, str] = {}
_video_cache_lock = threading.Lock()
_url_locks: dict[str, threading.Lock] = {}
_url_locks_guard = threading.Lock()

ASPECT_MAP = {"9:16": "vertical", "1:1": "square", "16:9": "landscape"}
REFRAME_MODES = {"smart": ReframeMode.smart, "simple": ReframeMode.simple}
LANGUAGES = {
    "english": ("English", "en_us"),
    "hindi": ("Hindi", "hi"),
    "hinglish": ("Hinglish", "hi"),
}
INDEX_TYPES = {"transcript", "scene"}

# Output-quality presets map to (TranscodeMode, CRF). "standard" skips
# transcoding entirely so the reel is served as VideoDB's reframe output.
QUALITY_PRESETS: dict[str, tuple[str, int] | None] = {
    "standard": None,
    "fast": (TranscodeMode.lightning, 28),
    "high": (TranscodeMode.economy, 18),
}
PLAYER_PREFIX = "https://console.videodb.io/player?url="
MAX_REEL_CONCURRENCY = 5
TRANSCODE_POLL_TIMEOUT_S = 240
TRANSCODE_POLL_INTERVAL_S = 2.5
TRANSCODE_CALLBACK_PLACEHOLDER = "https://example.com/no-callback"


def _parse_queries(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


_SENTENCE_OPENERS = (
    "give me", "show me", "find me", "i want", "i need",
    "make me", "make a", "create a", "generate", "list ",
)


def _looks_like_compound_sentence(qtext: str) -> bool:
    """True if the user wrote one long sentence instead of one topic per line."""
    lines = [line.strip() for line in (qtext or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    line = lines[0]
    word_count = len(line.split())
    if word_count > 12:
        return True
    lower = line.lower()
    if any(lower.startswith(opener) for opener in _SENTENCE_OPENERS):
        return True
    has_separator = (
        "," in line
        or ";" in line
        or " & " in line
        or " and " in f" {lower} "
    )
    return has_separator and word_count > 5


def _widen(shot_start: float, shot_end: float, length: float, video_length: float | None) -> tuple[float, float]:
    mid = (shot_start + shot_end) / 2
    half = length / 2
    s = max(0.0, mid - half)
    if video_length:
        e = min(video_length, s + length)
        s = max(0.0, e - length)
    else:
        e = s + length
    return s, e


def _set(job_id: str, **fields: Any) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _set_reel(job_id: str, idx: int, **fields: Any) -> None:
    with _jobs_lock:
        _jobs[job_id]["reels"][idx].update(fields)


def _lock_for_url(url: str) -> threading.Lock:
    with _url_locks_guard:
        lock = _url_locks.get(url)
        if lock is None:
            lock = threading.Lock()
            _url_locks[url] = lock
        return lock


def _looks_like_not_found(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "not found" in msg or "does not exist" in msg or "no such" in msg


def _get_or_upload(url: str) -> "videodb.Video":
    with _lock_for_url(url):
        with _video_cache_lock:
            cached_id = _video_cache.get(url)
        if cached_id:
            try:
                return _coll.get_video(cached_id)
            except VideodbError as exc:
                if not _looks_like_not_found(exc):
                    log.warning("cached video lookup failed (will not re-upload): %s", exc)
                    raise
                log.info("cached video id %s is stale; re-uploading", cached_id)
        video = _coll.upload(url=url)
        with _video_cache_lock:
            _video_cache[url] = video.id
        return video


def _reframe_with_retry(video: "videodb.Video", start: float, end: float,
                        aspect: str, mode: Any, job_id: str, idx: int):
    """Try reframe once; if the SDK gives up with 'Stuck on processing status',
    retry one more time. The retry is cheap (the source video is already
    uploaded and indexed) and frequently succeeds on a fresh worker."""
    try:
        return video.reframe(start=start, end=end, target=aspect, mode=mode)
    except Exception as exc:
        if not _is_processing_timeout(exc):
            raise
        log.warning("reframe stuck on processing, retrying once (job=%s idx=%s)",
                    job_id, idx)
        return video.reframe(start=start, end=end, target=aspect, mode=mode)


def _ensure_scene_index(video: "videodb.Video") -> str:
    """Build a scene index, or recover the existing one if already indexed.
    `index_scenes()` lacks a `force` flag and raises an error containing the
    existing id; we parse the id out instead of failing."""
    try:
        return video.index_scenes(
            extraction_type=SceneExtractionType.shot_based,
            prompt="Describe the visual content in this scene.",
        )
    except Exception as exc:
        match = re.search(r"id\s+([a-f0-9-]+)", str(exc))
        if match:
            log.info("scene index already exists, reusing id=%s", match.group(1))
            return match.group(1)
        raise


def _add_subtitle_with_retry(reel_video, job_id: str, idx: int) -> str:
    """Burn captions into the reframed reel. Same retry-on-stuck pattern as
    reframe — it's another long-running server-side render."""
    try:
        return reel_video.add_subtitle()
    except Exception as exc:
        if not _is_processing_timeout(exc):
            raise
        log.warning("add_subtitle stuck on processing, retrying once (job=%s idx=%s)",
                    job_id, idx)
        return reel_video.add_subtitle()


def _wants_transcode(cfg: dict[str, Any]) -> bool:
    return QUALITY_PRESETS.get(cfg.get("quality_preset") or "standard") is not None


def _transcode_and_wait(source_url: str, cfg: dict[str, Any]) -> str:
    """Submit a transcode job and poll until it finishes. Returns the new
    stream URL. The SDK requires `callback_url`, but we don't run a webhook
    server — we pass a placeholder and rely on `get_transcode_details(job_id)`
    polling for completion."""
    preset = QUALITY_PRESETS[cfg["quality_preset"]]
    if preset is None:
        return source_url
    mode_name, crf = preset
    video_config = VideoConfig(quality=crf)
    audio_config = AudioConfig()

    job_id = _conn.transcode(
        source=source_url,
        callback_url=TRANSCODE_CALLBACK_PLACEHOLDER,
        mode=mode_name,
        video_config=video_config,
        audio_config=audio_config,
    )
    deadline = time.time() + TRANSCODE_POLL_TIMEOUT_S
    last_status = None
    while time.time() < deadline:
        details = _conn.get_transcode_details(job_id)
        last_status = (details.get("status") or "").lower()
        if last_status in ("completed", "success", "done", "successful"):
            return (details.get("output_url")
                    or details.get("stream_url")
                    or details.get("url")
                    or source_url)
        if last_status in ("failed", "error", "errored"):
            raise RuntimeError(f"transcode failed: {details}")
        time.sleep(TRANSCODE_POLL_INTERVAL_S)
    raise TimeoutError(f"transcode did not complete in {TRANSCODE_POLL_TIMEOUT_S}s "
                       f"(last status: {last_status!r})")


def _run_reel(job_id: str, video: "videodb.Video", idx: int) -> None:
    with _jobs_lock:
        cfg = dict(_jobs[job_id]["config"])
        query = _jobs[job_id]["reels"][idx]["query"]
        scene_index_id = _jobs[job_id].get("scene_index_id")
    _set_reel(job_id, idx, state="matching")

    score_threshold = cfg.get("score_threshold") or 0.0
    try:
        if cfg["index_type"] == "scene":
            results = video.search(
                query=query,
                search_type=SearchType.semantic,
                index_type=IndexType.scene,
                scene_index_id=scene_index_id,
                score_threshold=score_threshold,
            )
        else:
            results = video.search(query, score_threshold=score_threshold)
        shots = results.get_shots()
    except InvalidRequestError as exc:
        if "No results found" in str(exc):
            _set_reel(job_id, idx, state="skipped", error="no match")
            return
        log.exception("search failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error=_clean_sdk_error(str(exc)))
        return
    except VideodbError as exc:
        log.exception("search failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error=_clean_sdk_error(str(exc)))
        return

    if not shots:
        _set_reel(job_id, idx, state="skipped", error="empty shots")
        return

    top = shots[0]
    video_length = getattr(video, "length", None)
    start, end = _widen(top.start, top.end, cfg["length"], video_length)
    _set_reel(job_id, idx, state="reframing",
              start=round(start, 2), end=round(end, 2),
              matched_text=getattr(top, "text", None))

    try:
        reel = _reframe_with_retry(video, start, end, cfg["aspect"], cfg["mode"], job_id, idx)
    except VideodbError as exc:
        log.exception("reframe failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error=_clean_sdk_error(str(exc)))
        return
    except Exception as exc:
        log.exception("reframe failed unexpectedly (job=%s idx=%s)", job_id, idx)
        if _is_processing_timeout(exc):
            _set_reel(job_id, idx, state="error",
                      error=_processing_timeout_msg("reeling"))
        else:
            _set_reel(job_id, idx, state="error",
                      error="reframe failed unexpectedly; check server logs")
        return

    stream_url = reel.stream_url

    if cfg.get("subtitles"):
        _set_reel(job_id, idx, state="captioning")
        try:
            stream_url = _add_subtitle_with_retry(reel, job_id, idx)
        except Exception:
            log.exception("subtitle burn-in failed (job=%s idx=%s)", job_id, idx)
            # Non-fatal: continue with the un-subtitled stream URL.

    if _wants_transcode(cfg):
        _set_reel(job_id, idx, state="transcoding")
        try:
            stream_url = _transcode_and_wait(stream_url, cfg)
        except Exception:
            log.exception("transcode failed (job=%s idx=%s)", job_id, idx)
            # Non-fatal: keep the pre-transcode URL.

    _set_reel(
        job_id,
        idx,
        state="ready",
        stream_url=stream_url,
        player_url=f"{PLAYER_PREFIX}{quote(stream_url, safe='')}",
    )


def _terminal_state(job_id: str) -> str:
    with _jobs_lock:
        states = {r["state"] for r in _jobs[job_id]["reels"]}
    if "ready" in states and "error" not in states:
        return "done"
    if "ready" in states:
        return "completed_with_errors"
    return "completed_with_errors"


def run_job(job_id: str) -> None:
    try:
        with _jobs_lock:
            job_video_url = _jobs[job_id]["video_url"]
            cfg = dict(_jobs[job_id]["config"])
            n = len(_jobs[job_id]["reels"])

        _set(job_id, state="uploading")
        video = _get_or_upload(job_video_url)
        with _jobs_lock:
            existing_meta = dict(_jobs[job_id].get("video_meta") or {})
        merged_meta = _merge_video_meta(existing_meta, _video_meta(video))
        _set(job_id, video_id=video.id, video_meta=merged_meta)

        _set(job_id, state="indexing")
        if cfg["index_type"] == "scene":
            scene_index_id = _ensure_scene_index(video)
            _set(job_id, scene_index_id=scene_index_id)
        else:
            video.index_spoken_words(force=True, language_code=cfg["language_code"])
            try:
                transcript = video.get_transcript_text()
            except Exception:
                log.exception("get_transcript_text failed (job=%s)", job_id)
                transcript = None
            _set(job_id, **_build_transcript_fields(transcript))

        _set(job_id, state="reeling")
        with ThreadPoolExecutor(max_workers=max(1, min(n, MAX_REEL_CONCURRENCY))) as ex:
            futures = [ex.submit(_run_reel, job_id, video, i) for i in range(n)]
            for f in futures:
                f.result()

        _set(job_id, state=_terminal_state(job_id))
    except (InvalidRequestError, VideodbError) as exc:
        with _jobs_lock:
            last_phase = _jobs[job_id]["state"]
        log.exception("job failed: %s (phase=%s)", job_id, last_phase)
        _set(job_id, state="error", failed_phase=last_phase,
             error=_clean_sdk_error(str(exc)))
    except Exception as exc:
        with _jobs_lock:
            last_phase = _jobs[job_id]["state"]
        log.exception("job failed: %s (phase=%s)", job_id, last_phase)
        if _is_processing_timeout(exc):
            msg = _processing_timeout_msg(last_phase)
        else:
            msg = "job failed unexpectedly; check server logs"
        _set(job_id, state="error", failed_phase=last_phase, error=msg)


def _clean_sdk_error(msg: str) -> str:
    """Strip noisy prefixes/whitespace from SDK error strings before showing to users."""
    msg = msg.strip()
    for prefix in ("Invalid request: ", "Invalid Request: "):
        if msg.startswith(prefix):
            msg = msg[len(prefix):]
            break
    return msg.rstrip(". ").strip()


def _is_processing_timeout(exc: BaseException) -> bool:
    """The SDK raises a bare Exception('Stuck on processing status') after its
    internal poll/backoff gives up. Detect that case so we can react usefully."""
    return "Stuck on processing" in str(exc)


_PHASE_NOUN = {
    "uploading": "upload",
    "indexing": "transcript indexing",
    "reeling": "reel render",
}


def _processing_timeout_msg(phase: str | None) -> str:
    noun = _PHASE_NOUN.get(phase or "", "operation")
    return (
        f"VideoDB took too long to finish the {noun}. This usually happens on "
        "very long source videos — try a shorter clip, or email "
        "contact@videodb.io to raise your processing limits."
    )


_STATE_PRETTY = {
    "queued": "queued",
    "uploading": "uploading",
    "indexing": "indexing",
    "reeling": "reeling",
    "done": "done",
    "completed_with_errors": "done with errors",
    "error": "failed",
}


def _format_relative(ts: float | None) -> str:
    if not ts:
        return ""
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return "just now"
    if delta < 3600:
        n = int(delta // 60)
        return f"{n}m ago" if n != 1 else "1m ago"
    if delta < 86400:
        n = int(delta // 3600)
        return f"{n}h ago" if n != 1 else "1h ago"
    n = int(delta // 86400)
    return f"{n}d ago" if n != 1 else "1d ago"


def _recent_jobs(limit: int = 25) -> list[dict[str, Any]]:
    with _jobs_lock:
        snapshot = list(_jobs.values())
    snapshot.sort(key=lambda j: j.get("created_at") or 0.0, reverse=True)
    snapshot = snapshot[:limit]

    out = []
    for job in snapshot:
        meta = job.get("video_meta") or {}
        reels = job["reels"]
        ready = sum(1 for r in reels if r["state"] == "ready")
        total = len(reels)
        if job["state"] == "done":
            reel_summary = f"{ready} of {total} reel{'s' if total != 1 else ''}"
        elif job["state"] == "completed_with_errors":
            reel_summary = f"{ready}/{total} reels (some failed)"
        elif job["state"] == "error":
            reel_summary = "failed"
        else:
            reel_summary = f"{total} reel{'s' if total != 1 else ''}"

        if job.get("batch_id"):
            link = url_for("batch_page", batch_id=job["batch_id"])
        else:
            link = url_for("job_page", job_id=job["id"])

        out.append({
            "id": job["id"],
            "batch_id": job.get("batch_id"),
            "video_url": job["video_url"],
            "video_name": (meta.get("name") if meta else None) or None,
            "thumbnail_url": (meta.get("thumbnail_url") if meta else None) or None,
            "state": job["state"],
            "state_pretty": _STATE_PRETTY.get(job["state"], job["state"]),
            "reel_summary": reel_summary,
            "created_relative": _format_relative(job.get("created_at")),
            "link_url": link,
        })
    return out


@app.get("/")
def index():
    return render_template("index.html", recent_jobs=_recent_jobs())


_YOUTUBE_PATTERNS = [
    re.compile(r"youtube\.com/watch\?[^#]*v=([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/live/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtube\.com/embed/([a-zA-Z0-9_-]{11})"),
    re.compile(r"youtu\.be/([a-zA-Z0-9_-]{11})"),
]


def _extract_youtube_id(url: str) -> str | None:
    for pattern in _YOUTUBE_PATTERNS:
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def _provisional_video_meta(url: str) -> dict[str, Any]:
    """Best-effort metadata derivable from the URL alone — used to populate the
    Source-video panel before upload completes. Real fields overwrite these
    once VideoDB returns metadata."""
    yt_id = _extract_youtube_id(url)
    thumbnail = (f"https://i.ytimg.com/vi/{yt_id}/hqdefault.jpg"
                 if yt_id else None)
    try:
        host = (urlparse(url).hostname or "").lower() or None
    except Exception:
        host = None
    if host and host.startswith("www."):
        host = host[4:]
    return {
        "name": None,
        "description": None,
        "length": None,
        "thumbnail_url": thumbnail,
        "stream_url": None,
        "player_url": None,
        "host": host,
        "source_url": url,
        "youtube_id": yt_id,
    }


def _video_meta(video: "videodb.Video") -> dict[str, Any]:
    """Collect user-visible metadata using getattr so SDK schema drift doesn't crash us."""
    def s(name: str) -> Any:
        v = getattr(video, name, None)
        return v if v else None
    return {
        "name": s("name"),
        "description": s("description"),
        "length": getattr(video, "length", None),
        "thumbnail_url": s("thumbnail_url"),
        "stream_url": s("stream_url"),
        "player_url": s("player_url"),
    }


def _merge_video_meta(provisional: dict[str, Any] | None,
                      real: dict[str, Any]) -> dict[str, Any]:
    """Real fields win where present; provisional fills the rest."""
    merged = dict(provisional or {})
    for k, v in real.items():
        if v is not None and v != "":
            merged[k] = v
    return merged


def _format_duration(seconds: float | None) -> str | None:
    if not seconds and seconds != 0:
        return None
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


_TRANSCRIPT_PREVIEW_CHARS = 800


def _build_transcript_fields(text: str | None) -> dict[str, Any]:
    if not text:
        return {"transcript_preview": None, "transcript_word_count": None,
                "transcript_full": None}
    text = text.strip()
    preview = text[:_TRANSCRIPT_PREVIEW_CHARS]
    if len(text) > _TRANSCRIPT_PREVIEW_CHARS:
        preview = preview.rsplit(" ", 1)[0] + "…"
    return {
        "transcript_preview": preview,
        "transcript_word_count": len(text.split()),
        "transcript_full": text,
    }


def _make_job(video_url: str, queries: list[str], cfg: dict[str, Any]) -> str:
    job_id = secrets.token_urlsafe(8)
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "state": "queued",
            "video_id": None,
            "video_url": video_url,
            "config": dict(cfg),
            "reels": [
                {"query": q, "state": "pending", "start": None, "end": None,
                 "stream_url": None, "player_url": None, "error": None,
                 "matched_text": None}
                for q in queries
            ],
            "error": None,
            "failed_phase": None,
            "video_meta": _provisional_video_meta(video_url),
            "transcript_preview": None,
            "transcript_word_count": None,
            "transcript_full": None,
            "created_at": time.time(),
            "batch_id": None,
            "scene_index_id": None,
        }
    threading.Thread(target=run_job, args=(job_id,), daemon=True,
                     name=f"job-{job_id}").start()
    return job_id


@app.post("/generate")
def generate():
    video_urls = [u.strip() for u in request.form.getlist("video_url")]
    queries_blocks = request.form.getlist("queries")
    length_raw = (request.form.get("length") or "60").strip()
    aspect_label = request.form.get("aspect", "9:16")
    mode_label = request.form.get("mode", "smart")
    language_label = request.form.get("language", "english")
    index_type = (request.form.get("index_type") or "transcript").strip()
    quality_preset = (request.form.get("quality_preset") or "standard").strip()
    subtitles = bool(request.form.get("subtitles"))
    score_raw = (request.form.get("score_threshold") or "0.3").strip()

    if len(video_urls) != len(queries_blocks):
        return render_template(
            "index.html",
            errors=["malformed form: mismatched video/query count"],
        ), 400

    errors: list[str] = []
    if not video_urls or all(not u for u in video_urls):
        errors.append("at least one video URL is required")

    parsed_videos: list[tuple[str, list[str]]] = []
    for i, (url, qtext) in enumerate(zip(video_urls, queries_blocks), 1):
        if not url:
            errors.append(f"video {i}: URL is required")
            continue
        qs = _parse_queries(qtext)
        if not qs:
            errors.append(f"video {i}: at least one query is required")
            continue
        if _looks_like_compound_sentence(qtext):
            errors.append(
                f"video {i}: that looks like one long sentence — split it into "
                "multiple lines (one short topic per line, e.g. \"key highlights\")."
            )
            continue
        parsed_videos.append((url, qs))

    try:
        length = float(length_raw)
    except ValueError:
        length = None
        errors.append(f"length must be a number, got {length_raw!r}")
    if length is not None and not (5 <= length <= 300):
        errors.append("length must be between 5 and 300 seconds")
    aspect = ASPECT_MAP.get(aspect_label)
    if not aspect:
        errors.append(f"unknown aspect ratio: {aspect_label}")
    mode = REFRAME_MODES.get(mode_label)
    if mode is None:
        errors.append(f"unknown reframe mode: {mode_label}")
    language_entry = LANGUAGES.get(language_label)
    if language_entry is None:
        errors.append(f"unknown language: {language_label}")
    if index_type not in INDEX_TYPES:
        errors.append(f"unknown search type: {index_type}")
    if quality_preset not in QUALITY_PRESETS:
        errors.append(f"unknown output quality: {quality_preset}")
    try:
        score_threshold = float(score_raw)
    except ValueError:
        score_threshold = None
        errors.append(f"score threshold must be a number, got {score_raw!r}")
    if score_threshold is not None and not (0.0 <= score_threshold <= 1.0):
        errors.append("score threshold must be between 0 and 1")

    if errors or not parsed_videos:
        return render_template(
            "index.html",
            errors=errors or ["no valid videos"],
            form={
                "videos": [{"url": u, "queries": q}
                           for u, q in zip(video_urls, queries_blocks)],
                "length": length_raw, "aspect": aspect_label,
                "mode": mode_label, "language": language_label,
                "index_type": index_type, "quality_preset": quality_preset,
                "subtitles": subtitles, "score_threshold": score_raw,
            },
            recent_jobs=_recent_jobs(),
        ), 400

    language_display, language_code = language_entry  # type: ignore[misc]
    cfg = {"length": length, "aspect": aspect, "mode": mode,
           "aspect_label": aspect_label, "mode_label": mode_label,
           "language_label": language_label,
           "language_display": language_display,
           "language_code": language_code,
           "index_type": index_type,
           "score_threshold": score_threshold,
           "subtitles": subtitles,
           "quality_preset": quality_preset}

    job_ids = [_make_job(url, qs, cfg) for url, qs in parsed_videos]

    if len(job_ids) == 1:
        return redirect(url_for("job_page", job_id=job_ids[0]))

    batch_id = secrets.token_urlsafe(8)
    with _batches_lock:
        _batches[batch_id] = {"id": batch_id, "job_ids": list(job_ids), "config": dict(cfg),
                              "created_at": time.time()}
    with _jobs_lock:
        for jid in job_ids:
            if jid in _jobs:
                _jobs[jid]["batch_id"] = batch_id
    return redirect(url_for("batch_page", batch_id=batch_id))


@app.get("/jobs/<job_id>")
def job_page(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return ("job not found", 404)
    return render_template("job.html", job_id=job_id)


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    reels = [dict(r) for r in job["reels"]]
    meta = job.get("video_meta") or {}
    return {
        "id": job["id"],
        "state": job["state"],
        "video_id": job["video_id"],
        "video_url": job["video_url"],
        "config": {
            "length": job["config"]["length"],
            "aspect_label": job["config"]["aspect_label"],
            "mode_label": job["config"]["mode_label"],
            "language_display": job["config"]["language_display"],
            "index_type": job["config"].get("index_type", "transcript"),
            "subtitles": job["config"].get("subtitles", False),
            "quality_preset": job["config"].get("quality_preset", "standard"),
        },
        "reels": reels,
        "progress": _compute_progress(job["state"], reels, job.get("failed_phase")),
        "error": job["error"],
        "failed_phase": job.get("failed_phase"),
        "video_meta": {
            "name": meta.get("name"),
            "thumbnail_url": meta.get("thumbnail_url"),
            "player_url": meta.get("player_url"),
            "length": meta.get("length"),
            "length_pretty": _format_duration(meta.get("length")),
            "host": meta.get("host"),
            "source_url": meta.get("source_url"),
            "youtube_id": meta.get("youtube_id"),
        } if meta else None,
        "transcript_preview": job.get("transcript_preview"),
        "transcript_word_count": job.get("transcript_word_count"),
        "transcript_available": bool(job.get("transcript_full")),
    }


PHASE_ORDER = ["queued", "uploading", "indexing", "reeling", "done"]
PHASE_LABELS = {
    "queued": "Queued",
    "uploading": "Uploading",
    "indexing": "Indexing transcript",
    "reeling": "Generating reels",
    "done": "Done",
}
PHASE_PERCENT = {"queued": 5, "uploading": 12, "indexing": 30, "reeling": 70, "done": 100}


def _compute_progress(
    job_state: str,
    reels: list[dict[str, Any]],
    failed_phase: str | None = None,
) -> dict[str, Any]:
    """Return a percent (0-100) and a list of milestone steps with state."""
    n = max(len(reels), 1)
    finished_reels = sum(1 for r in reels if r["state"] in ("ready", "skipped", "error"))

    if job_state == "queued":
        percent = 2
    elif job_state == "uploading":
        percent = 12
    elif job_state == "indexing":
        percent = 30
    elif job_state == "reeling":
        percent = 40 + int(55 * (finished_reels / n))
    elif job_state in ("done", "completed_with_errors"):
        percent = 100
    elif job_state == "error":
        percent = PHASE_PERCENT.get(failed_phase or "", 100)
    else:
        percent = 0

    error_idx = (
        PHASE_ORDER.index(failed_phase)
        if job_state == "error" and failed_phase in PHASE_ORDER
        else None
    )

    def step_state(phase: str) -> str:
        idx = PHASE_ORDER.index(phase)
        if error_idx is not None:
            if idx < error_idx:
                return "done"
            if idx == error_idx:
                return "error"
            return "pending"
        cur_idx = PHASE_ORDER.index(job_state) if job_state in PHASE_ORDER else 0
        if job_state in ("done", "completed_with_errors"):
            cur_idx = len(PHASE_ORDER) - 1
        if idx < cur_idx:
            return "done"
        if idx == cur_idx:
            if phase == "done" or phase == "queued":
                return "done"
            return "active"
        return "pending"

    steps = []
    for phase in PHASE_ORDER:
        s = step_state(phase)
        label = PHASE_LABELS[phase]
        if phase == "reeling" and (
            job_state in ("reeling", "done", "completed_with_errors")
            or (job_state == "error" and failed_phase == "reeling")
        ):
            label = f"{PHASE_LABELS[phase]} ({finished_reels}/{n})"
        steps.append({"phase": phase, "state": s, "label": label})

    return {"percent": percent, "steps": steps,
            "finished_reels": finished_reels, "total_reels": len(reels)}


@app.get("/jobs/<job_id>/status")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        snapshot = _job_snapshot(job)
    return jsonify(snapshot)


@app.get("/jobs/<job_id>/transcript")
def job_transcript(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return ("job not found", 404)
        text = job.get("transcript_full")
    if not text:
        return ("transcript not available yet", 404)
    return text, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/batches/<batch_id>")
def batch_page(batch_id: str):
    with _batches_lock:
        batch = _batches.get(batch_id)
        if not batch:
            return ("batch not found", 404)
        job_ids = list(batch["job_ids"])
    with _jobs_lock:
        jobs = [{"id": jid, "video_url": _jobs[jid]["video_url"], "state": _jobs[jid]["state"]}
                for jid in job_ids if jid in _jobs]
    return render_template("batch.html", batch_id=batch_id, jobs=jobs)


@app.get("/batches/<batch_id>/status")
def batch_status(batch_id: str):
    with _batches_lock:
        batch = _batches.get(batch_id)
        if not batch:
            return jsonify({"error": "not found"}), 404
        job_ids = list(batch["job_ids"])
    with _jobs_lock:
        snapshots = [_job_snapshot(_jobs[jid]) for jid in job_ids if jid in _jobs]
    return jsonify({"id": batch_id, "jobs": snapshots})
