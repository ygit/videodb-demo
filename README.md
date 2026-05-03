# videodb-demo

Experimentation playground for the [VideoDB](https://videodb.io) Python SDK.

## Setup

```bash
# Create virtualenv (recommended)
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure API key (free key at https://console.videodb.io)
cp .env.example .env
# then edit .env and set VIDEO_DB_API_KEY
```

## Run

```bash
python main.py
```

Connects to your default VideoDB collection and lists the videos in it.

## Reel builder UI

A Flask app that turns a long-form video into short reels driven by topic queries.

```bash
flask --app app run
```

Then open <http://localhost:5000>. Paste a video URL, write one query per line (each query → one reel), tweak length / aspect ratio / reframe mode, and submit. The status page shows per-reel progress and embeds each reel as soon as it's ready.

Pipeline (server-side):

1. Upload the URL (cached per-URL in memory; reruns of the same URL skip re-upload).
2. `video.index_spoken_words(force=True)` — idempotent.
3. For each query: semantic-search the transcript, widen the top match to the requested length, `video.reframe()` to the chosen aspect. All queries reframe in parallel via a thread pool.

The job store is in-memory, so a Flask process restart loses jobs. That's fine for a local playground; don't deploy as-is.
