# ClipFetch Studio

ClipFetch Studio lets you:
- paste a supported media page link
- choose **Video (MP4)** or **Audio only (MP3)**
- choose quality:
  - Video: best / 1080p / 720p / 480p / 360p
  - Audio: 320 / 256 / 192 / 128 kbps
- choose output format:
  - Video: MP4 or WEBM
  - Audio: MP3, M4A, or WAV
- optionally trim with start/end range sliders
- include subtitles and/or thumbnail in a ZIP bundle
- add a custom filename prefix
- light/dark theme toggle (saved preference)
- download the final processed file

## Requirements

- Python 3.10+
- `ffmpeg` installed and available in PATH

Ubuntu install:

```bash
sudo apt update
sudo apt install -y ffmpeg python3-pip
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python run.py
```

Then open:

[http://localhost:5000](http://localhost:5000)

## Notes

- Trimming is optional. Keep full range selected to download full media.
- On mobile browsers, download is triggered as a direct file response so it can be saved to Files/Downloads.
- Server output files are temporary and auto-deleted after the response is sent.

## Deploy (Render)

This repo is ready for Docker deploy on Render.

1. Push this project to GitHub.
2. In Render, click **New +** -> **Blueprint**.
3. Select your GitHub repo (Render will detect `render.yaml`).
4. Deploy.

Render will build from `Dockerfile`, install `ffmpeg`, and run Gunicorn with a **180s worker timeout** so media metadata and downloads are less likely to be cut off (the default 30s often breaks “fetch info” on cloud hosts).

### If “Fetch” / preview fails on Render

- **Timeouts:** Free-tier edge limits still apply; very slow sites may fail even with a longer Gunicorn timeout.
- **Cloud IP blocking:** Some sites block datacenter IPs. The app uses a public **oEmbed** fallback when full metadata extraction fails, so preview may still work for common hosts (title/thumbnail; duration may be missing until download).
- **Logs:** In the Render dashboard, open your service → **Logs** to see worker crashes or 502s.
