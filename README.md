# ClipFetch Studio

ClipFetch Studio lets you:
- paste a YouTube link
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
