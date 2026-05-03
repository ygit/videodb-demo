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
