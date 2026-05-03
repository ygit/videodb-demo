"""Flask UI for generating short reels from a long-form video via VideoDB.

Run:
    flask --app app run

Then open http://localhost:5000.
"""

from __future__ import annotations

import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(".env")

import videodb
from flask import Flask, jsonify, redirect, render_template, request, url_for
from videodb import ReframeMode
from videodb.exceptions import InvalidRequestError, VideodbError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(threadName)s] %(message)s",
)
log = logging.getLogger("reel-builder")

app = Flask(__name__)

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
PLAYER_PREFIX = "https://console.videodb.io/player?url="
MAX_REEL_CONCURRENCY = 5


def _parse_queries(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


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


def _run_reel(job_id: str, video: "videodb.Video", idx: int) -> None:
    with _jobs_lock:
        cfg = dict(_jobs[job_id]["config"])
        query = _jobs[job_id]["reels"][idx]["query"]
    _set_reel(job_id, idx, state="matching")

    try:
        results = video.search(query)
        shots = results.get_shots()
    except InvalidRequestError as exc:
        if "No results found" in str(exc):
            _set_reel(job_id, idx, state="skipped", error="no match")
            return
        log.exception("search failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error="search failed")
        return
    except VideodbError:
        log.exception("search failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error="search failed")
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
        reel = video.reframe(start=start, end=end, target=cfg["aspect"], mode=cfg["mode"])
    except VideodbError:
        log.exception("reframe failed (job=%s idx=%s)", job_id, idx)
        _set_reel(job_id, idx, state="error", error="reframe failed")
        return

    stream_url = reel.stream_url
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
            language_code = _jobs[job_id]["config"]["language_code"]
            n = len(_jobs[job_id]["reels"])

        _set(job_id, state="uploading")
        video = _get_or_upload(job_video_url)
        _set(job_id, video_id=video.id)

        _set(job_id, state="indexing")
        video.index_spoken_words(force=True, language_code=language_code)

        _set(job_id, state="reeling")
        with ThreadPoolExecutor(max_workers=max(1, min(n, MAX_REEL_CONCURRENCY))) as ex:
            futures = [ex.submit(_run_reel, job_id, video, i) for i in range(n)]
            for f in futures:
                f.result()

        _set(job_id, state=_terminal_state(job_id))
    except Exception:
        log.exception("job failed: %s", job_id)
        _set(job_id, state="error", error="job failed; check server logs")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/generate")
def generate():
    video_url = (request.form.get("video_url") or "").strip()
    queries = _parse_queries(request.form.get("queries", ""))
    length_raw = (request.form.get("length") or "60").strip()
    aspect_label = request.form.get("aspect", "9:16")
    mode_label = request.form.get("mode", "smart")
    language_label = request.form.get("language", "english")

    errors: list[str] = []
    if not video_url:
        errors.append("video URL is required")
    if not queries:
        errors.append("at least one query is required")
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

    if errors:
        return render_template(
            "index.html",
            errors=errors,
            form={"video_url": video_url, "queries": request.form.get("queries", ""),
                  "length": length_raw, "aspect": aspect_label, "mode": mode_label,
                  "language": language_label},
        ), 400

    language_display, language_code = language_entry
    job_id = secrets.token_urlsafe(8)
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "state": "queued",
            "video_id": None,
            "video_url": video_url,
            "config": {"length": length, "aspect": aspect, "mode": mode,
                       "aspect_label": aspect_label, "mode_label": mode_label,
                       "language_label": language_label,
                       "language_display": language_display,
                       "language_code": language_code},
            "reels": [
                {"query": q, "state": "pending", "start": None, "end": None,
                 "stream_url": None, "player_url": None, "error": None,
                 "matched_text": None}
                for q in queries
            ],
            "error": None,
        }

    threading.Thread(target=run_job, args=(job_id,), daemon=True,
                     name=f"job-{job_id}").start()
    return redirect(url_for("job_page", job_id=job_id))


@app.get("/jobs/<job_id>")
def job_page(job_id: str):
    with _jobs_lock:
        if job_id not in _jobs:
            return ("job not found", 404)
    return render_template("job.html", job_id=job_id)


@app.get("/jobs/<job_id>/status")
def job_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return jsonify({"error": "not found"}), 404
        snapshot = {
            "id": job["id"],
            "state": job["state"],
            "video_id": job["video_id"],
            "video_url": job["video_url"],
            "config": {
                "length": job["config"]["length"],
                "aspect_label": job["config"]["aspect_label"],
                "mode_label": job["config"]["mode_label"],
                "language_display": job["config"]["language_display"],
            },
            "reels": [dict(r) for r in job["reels"]],
            "error": job["error"],
        }
    return jsonify(snapshot)
