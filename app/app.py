import re
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_DIR = BASE_DIR / "temp"
DOWNLOADS_DIR = BASE_DIR / "downloads"

TEMP_DIR.mkdir(parents=True, exist_ok=True)
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def parse_time_to_seconds(raw_value: str) -> float | None:
    if raw_value is None:
        return None
    value = raw_value.strip()
    if not value:
        return None

    if re.fullmatch(r"\d+(\.\d+)?", value):
        return float(value)

    parts = value.split(":")
    if not all(part.isdigit() for part in parts):
        raise ValueError("Time must be seconds or HH:MM:SS.")

    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + int(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + int(seconds)

    raise ValueError("Time must be seconds or HH:MM:SS.")


def format_seconds_for_ffmpeg(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    whole = int(seconds)
    milliseconds = int(round((seconds - whole) * 1000))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def ensure_ffmpeg_exists() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed or not in PATH.")


def get_video_info(url: str) -> dict:
    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
    }
    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
    }


def run_ffmpeg_trim(
    input_file: Path,
    output_file: Path,
    start: float | None,
    end: float | None,
    mode: str,
    audio_bitrate: str = "192",
) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", format_seconds_for_ffmpeg(start)]
    cmd += ["-i", str(input_file)]
    if end is not None:
        cmd += ["-to", format_seconds_for_ffmpeg(end)]

    if mode == "audio":
        cmd += ["-vn", "-codec:a", "libmp3lame", "-b:a", f"{audio_bitrate}k", str(output_file)]
    else:
        cmd += ["-codec:v", "libx264", "-preset", "veryfast", "-codec:a", "aac", str(output_file)]

    subprocess.run(cmd, check=True)


def get_video_format_for_quality(quality: str) -> str:
    quality_map = {
        "best": "bestvideo+bestaudio/best",
        "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
        "720": "bestvideo[height<=720]+bestaudio/best[height<=720]",
        "480": "bestvideo[height<=480]+bestaudio/best[height<=480]",
        "360": "bestvideo[height<=360]+bestaudio/best[height<=360]",
    }
    return quality_map.get(quality, quality_map["best"])


def get_audio_bitrate(quality: str) -> str:
    allowed = {"320", "256", "192", "128"}
    return quality if quality in allowed else "192"


def sanitize_filename_prefix(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    return cleaned[:60]


def download_source(
    url: str,
    mode: str,
    request_id: str,
    quality: str,
    download_subtitles: bool = False,
    download_thumbnail: bool = False,
) -> tuple[Path, list[Path]]:
    out_dir = TEMP_DIR / request_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(title).120s.%(ext)s")

    ydl_opts = {
        "outtmpl": out_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if download_subtitles:
        ydl_opts["writesubtitles"] = True
        ydl_opts["writeautomaticsub"] = True
        ydl_opts["subtitleslangs"] = ["en", "en-US", "en-GB"]
    if download_thumbnail:
        ydl_opts["writethumbnail"] = True

    if mode == "audio":
        ydl_opts["format"] = "bestaudio/best"
    else:
        ydl_opts["format"] = get_video_format_for_quality(quality)
        ydl_opts["merge_output_format"] = "mp4"

    with YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        file_path = Path(ydl.prepare_filename(info))

    if mode == "video" and file_path.suffix.lower() != ".mp4":
        mp4_guess = file_path.with_suffix(".mp4")
        if mp4_guess.exists():
            file_path = mp4_guess

    side_files = []
    if download_subtitles:
        side_files.extend(out_dir.glob("*.vtt"))
        side_files.extend(out_dir.glob("*.srt"))
    if download_thumbnail:
        side_files.extend(out_dir.glob("*.jpg"))
        side_files.extend(out_dir.glob("*.jpeg"))
        side_files.extend(out_dir.glob("*.png"))
        side_files.extend(out_dir.glob("*.webp"))

    return file_path, side_files


def export_media(
    input_file: Path,
    output_file: Path,
    mode: str,
    output_format: str,
    start: float | None,
    end: float | None,
    audio_bitrate: str,
) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if start is not None:
        cmd += ["-ss", format_seconds_for_ffmpeg(start)]
    cmd += ["-i", str(input_file)]
    if end is not None:
        cmd += ["-to", format_seconds_for_ffmpeg(end)]

    if mode == "audio":
        if output_format == "m4a":
            cmd += ["-vn", "-codec:a", "aac", "-b:a", f"{audio_bitrate}k", str(output_file)]
        elif output_format == "wav":
            cmd += ["-vn", "-codec:a", "pcm_s16le", str(output_file)]
        else:
            cmd += ["-vn", "-codec:a", "libmp3lame", "-b:a", f"{audio_bitrate}k", str(output_file)]
    else:
        if output_format == "webm":
            cmd += ["-codec:v", "libvpx-vp9", "-b:v", "2M", "-codec:a", "libopus", str(output_file)]
        else:
            cmd += ["-codec:v", "libx264", "-preset", "veryfast", "-codec:a", "aac", str(output_file)]
    subprocess.run(cmd, check=True)


def get_recent_downloads(limit: int = 20) -> list[dict]:
    items = []
    for path in DOWNLOADS_DIR.glob("*"):
        if path.is_file():
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_ts": int(stat.st_mtime),
                }
            )
    items.sort(key=lambda x: x["modified_ts"], reverse=True)
    return items[:limit]


@app.route("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
def api_info():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required."}), 400
    try:
        info = get_video_info(url)
        info["video_qualities"] = ["best", "1080", "720", "480", "360"]
        info["audio_qualities"] = ["320", "256", "192", "128"]
        info["video_formats"] = ["mp4", "webm"]
        info["audio_formats"] = ["mp3", "m4a", "wav"]
        return jsonify({"info": info})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Failed to fetch info: {exc}"}), 400


def _extract_download_payload() -> dict:
    if request.method == "GET":
        source = request.args
        return {
            "url": (source.get("url") or "").strip(),
            "mode": (source.get("mode") or "video").strip().lower(),
            "start": source.get("start", ""),
            "end": source.get("end", ""),
            "quality": (source.get("quality") or "").strip().lower(),
            "output_format": (source.get("output_format") or "").strip().lower(),
            "include_subtitles": (source.get("include_subtitles") or "false").strip().lower(),
            "include_thumbnail": (source.get("include_thumbnail") or "false").strip().lower(),
            "filename_prefix": (source.get("filename_prefix") or "").strip(),
        }
    data = request.get_json(force=True)
    return {
        "url": (data.get("url") or "").strip(),
        "mode": (data.get("mode") or "video").strip().lower(),
        "start": data.get("start", ""),
        "end": data.get("end", ""),
        "quality": (data.get("quality") or "").strip().lower(),
        "output_format": (data.get("output_format") or "").strip().lower(),
        "include_subtitles": str(data.get("include_subtitles", "false")).strip().lower(),
        "include_thumbnail": str(data.get("include_thumbnail", "false")).strip().lower(),
        "filename_prefix": (data.get("filename_prefix") or "").strip(),
    }


@app.get("/api/history")
def api_history():
    return jsonify({"items": get_recent_downloads()})


@app.route("/api/download", methods=["GET", "POST"])
def api_download():
    payload = _extract_download_payload()
    url = payload["url"]
    mode = payload["mode"]
    raw_start = payload["start"]
    raw_end = payload["end"]
    quality = payload["quality"] or ("best" if mode == "video" else "192")
    output_format = payload["output_format"] or ("mp4" if mode == "video" else "mp3")
    include_subtitles = payload["include_subtitles"] == "true"
    include_thumbnail = payload["include_thumbnail"] == "true"
    filename_prefix = sanitize_filename_prefix(payload["filename_prefix"])

    if mode not in {"video", "audio"}:
        return jsonify({"error": "Mode must be video or audio."}), 400
    if not url:
        return jsonify({"error": "URL is required."}), 400
    if mode == "video" and output_format not in {"mp4", "webm"}:
        return jsonify({"error": "Invalid video format."}), 400
    if mode == "audio" and output_format not in {"mp3", "m4a", "wav"}:
        return jsonify({"error": "Invalid audio format."}), 400

    try:
        start = parse_time_to_seconds(raw_start)
        end = parse_time_to_seconds(raw_end)
        if start is not None and start < 0:
            return jsonify({"error": "Start time must be >= 0."}), 400
        if end is not None and end < 0:
            return jsonify({"error": "End time must be >= 0."}), 400
        if start is not None and end is not None and end <= start:
            return jsonify({"error": "End time must be greater than start time."}), 400

        ensure_ffmpeg_exists()
        request_id = uuid.uuid4().hex
        source_file, side_files = download_source(
            url,
            mode,
            request_id,
            quality,
            download_subtitles=include_subtitles,
            download_thumbnail=include_thumbnail,
        )

        file_base = f"{mode}_{request_id}"
        if filename_prefix:
            file_base = f"{filename_prefix}_{file_base}"
        if mode == "audio":
            output_name = f"{file_base}.{output_format}"
            audio_bitrate = get_audio_bitrate(quality)
        else:
            output_name = f"{file_base}.{output_format}"
            audio_bitrate = "192"
        output_file = DOWNLOADS_DIR / output_name

        export_media(source_file, output_file, mode, output_format, start, end, audio_bitrate)

        response_file = output_file
        response_name = output_file.name
        response_mime = "audio/mpeg" if mode == "audio" else "video/mp4"
        if include_subtitles or include_thumbnail:
            bundle_name = f"{file_base}_bundle.zip"
            bundle_path = DOWNLOADS_DIR / bundle_name
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(output_file, arcname=output_file.name)
                for side_file in side_files:
                    archive.write(side_file, arcname=side_file.name)
            response_file = bundle_path
            response_name = bundle_name
            response_mime = "application/zip"

        temp_request_dir = TEMP_DIR / request_id
        shutil.rmtree(temp_request_dir, ignore_errors=True)

        return send_file(
            response_file,
            as_attachment=True,
            download_name=response_name,
            mimetype=response_mime,
        )
    except subprocess.CalledProcessError:
        return jsonify({"error": "ffmpeg processing failed. Check trim values and try again."}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"Download failed: {exc}"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
