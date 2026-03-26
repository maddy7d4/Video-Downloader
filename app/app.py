import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from datetime import date
from pathlib import Path

import requests as http_client
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, unquote_to_bytes
from flask import Flask, Response, after_this_request, jsonify, render_template, request, send_file, stream_with_context, url_for
from yt_dlp import YoutubeDL


BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_DIR = BASE_DIR / "temp"

TEMP_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)


def get_base_ydl_opts() -> dict:
    # Extractor defaults tuned for better site compatibility.
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "web", "tv_embedded"],
            }
        },
    }


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


def get_info_ydl_opts() -> dict:
    """Tighter timeouts and fewer retries so /api/info finishes before proxy/worker limits."""
    opts = {**get_base_ydl_opts(), "skip_download": True}
    opts["socket_timeout"] = 25
    opts["retries"] = 2
    opts["fragment_retries"] = 2
    return opts


def try_oembed_metadata(url: str) -> dict | None:
    """
    Public oEmbed APIs often work from cloud hosts when full extraction is blocked or slow.
    Duration may be missing for some providers.
    """
    try:
        netloc = urlsplit(url).netloc.lower()
    except Exception:
        return None

    candidates: list[tuple[str, dict]] = []
    if any(x in netloc for x in ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        candidates.append(
            ("https://www.youtube.com/oembed", {"url": url, "format": "json"}),
        )
    if "vimeo.com" in netloc:
        candidates.append(("https://vimeo.com/api/oembed.json", {"url": url}))
    if "sketchfab.com" in netloc:
        candidates.append(("https://sketchfab.com/oembed", {"url": url}))

    headers = {"User-Agent": "ClipFetchStudio/1.0 (metadata preview)"}
    for api, params in candidates:
        try:
            resp = http_client.get(api, params=params, timeout=(5, 14), headers=headers)
            if not resp.ok:
                continue
            data = resp.json()
            thumb = data.get("thumbnail_url") or data.get("thumbnail_url_with_play_button")
            duration = data.get("duration")
            if duration is not None and not isinstance(duration, (int, float)):
                duration = None
            return {
                "title": data.get("title"),
                "duration": int(duration) if duration is not None else None,
                "thumbnail": thumb,
                "uploader": data.get("author_name"),
            }
        except Exception:
            continue
    return None


def get_video_info(url: str) -> dict:
    core: dict | None = None
    try:
        with YoutubeDL(get_info_ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        thumbs = info.get("thumbnails") or []
        thumb = info.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
        core = {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "thumbnail": thumb,
            "uploader": info.get("uploader") or info.get("channel"),
        }
    except Exception:
        pass

    if core and core.get("title"):
        return core

    oembed = try_oembed_metadata(url)
    if oembed and (oembed.get("title") or oembed.get("thumbnail")):
        return {
            "title": oembed.get("title") or (core or {}).get("title"),
            "duration": (core or {}).get("duration") if core else oembed.get("duration"),
            "thumbnail": oembed.get("thumbnail") or (core or {}).get("thumbnail"),
            "uploader": oembed.get("uploader") or (core or {}).get("uploader"),
        }

    if core:
        return core

    raise ValueError("Could not fetch media metadata for this URL.")


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


def sanitize_filename_part(value: str, max_len: int = 80) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("._-")
    return cleaned[:max_len]


def download_source(
    url: str,
    mode: str,
    request_id: str,
    quality: str,
    download_subtitles: bool = False,
    download_thumbnail: bool = False,
) -> tuple[Path, list[Path], str]:
    out_dir = TEMP_DIR / request_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(out_dir / "%(title).120s.%(ext)s")

    ydl_opts = {
        **get_base_ydl_opts(),
        "outtmpl": out_template,
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
        media_title = (info.get("title") or file_path.stem or "download").strip()

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

    return file_path, side_files, media_title


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


@app.get("/health")
def health():
    return "", 204


@app.route("/")
def index():
    base_url = request.url_root.rstrip("/")
    canonical_url = f"{base_url}{url_for('index')}"
    logo_url = f"{base_url}{url_for('static', filename='logo.svg')}"
    seo = {
        "title": "ClipFetch Studio - Universal Video and Audio Downloader",
        "description": (
            "Download videos and audio from supported websites with quality options, trimming, "
            "format conversion, subtitle support, and mobile-friendly saving."
        ),
        "canonical_url": canonical_url,
        "og_image": logo_url,
        "site_name": "ClipFetch Studio",
    }
    return render_template("index.html", seo=seo)


@app.get("/robots.txt")
def robots_txt():
    base_url = request.url_root.rstrip("/")
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        f"Sitemap: {base_url}/sitemap.xml\n"
    )
    return app.response_class(content, mimetype="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    base_url = request.url_root.rstrip("/")
    today = date.today().isoformat()
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{base_url}/</loc>\n"
        f"    <lastmod>{today}</lastmod>\n"
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return app.response_class(content, mimetype="application/xml")


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
        return jsonify({"error": "Failed to fetch media info from this URL. Try another supported link."}), 400


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
    filename_prefix = sanitize_filename_part(payload["filename_prefix"], max_len=40)

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
        source_file, side_files, media_title = download_source(
            url,
            mode,
            request_id,
            quality,
            download_subtitles=include_subtitles,
            download_thumbnail=include_thumbnail,
        )

        title_part = sanitize_filename_part(media_title, max_len=90) or "download"
        short_id = request_id[:8]
        file_base = f"{title_part}_{short_id}"
        if filename_prefix:
            file_base = f"{filename_prefix}_{file_base}"
        if mode == "audio":
            output_name = f"{file_base}.{output_format}"
            audio_bitrate = get_audio_bitrate(quality)
        else:
            output_name = f"{file_base}.{output_format}"
            audio_bitrate = "192"
        temp_request_dir = TEMP_DIR / request_id
        output_file = temp_request_dir / output_name

        export_media(source_file, output_file, mode, output_format, start, end, audio_bitrate)

        response_file = output_file
        response_name = output_file.name
        response_mime = "audio/mpeg" if mode == "audio" else "video/mp4"
        if include_subtitles or include_thumbnail:
            bundle_name = f"{file_base}_bundle.zip"
            bundle_path = temp_request_dir / bundle_name
            with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(output_file, arcname=output_file.name)
                for side_file in side_files:
                    archive.write(side_file, arcname=side_file.name)
            response_file = bundle_path
            response_name = bundle_name
            response_mime = "application/zip"

        @after_this_request
        def _cleanup_temp_files(response):
            shutil.rmtree(temp_request_dir, ignore_errors=True)
            return response

        return send_file(
            response_file,
            as_attachment=True,
            download_name=response_name,
            mimetype=response_mime,
        )
    except subprocess.CalledProcessError:
        return jsonify({"error": "ffmpeg processing failed. Check trim values and try again."}), 500
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": "Download failed for this URL. Please try another supported website or try again later."}), 400


_MEDIA_EXTENSIONS = {
    # Images
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "svg": "image", "bmp": "image", "ico": "image",
    "tiff": "image", "tif": "image", "avif": "image", "heic": "image", "heif": "image",
    # Video
    "mp4": "video", "webm": "video", "avi": "video", "mov": "video",
    "mkv": "video", "flv": "video", "m4v": "video", "ogv": "video", "ts": "video",
    # Audio
    "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio",
    "flac": "audio", "aac": "audio", "opus": "audio",
    # Documents
    "pdf": "document", "doc": "document", "docx": "document",
    "xls": "document", "xlsx": "document", "ppt": "document", "pptx": "document",
    # CAD / 3D
    "dwg": "cad", "dxf": "cad",
    "step": "cad", "stp": "cad", "iges": "cad", "igs": "cad",
    "stl": "cad", "obj": "cad", "fbx": "cad",
    "gltf": "cad", "glb": "cad",
    "usdz": "cad", "usd": "cad",
    "3ds": "cad", "blend": "cad",
    "skp": "cad", "c4d": "cad",
    "ma": "cad", "mb": "cad",
    "3mf": "cad", "ply": "cad", "dae": "cad",
    # Sketchfab streamed geometry (not always a single downloadable GLB in HTML)
    "binz": "cad",
    # Archives
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive",
    "gz": "archive", "bz2": "archive", "xz": "archive",
}

# Regex to pull media URLs out of raw script / JSON content.
# Does NOT exclude backslash so it survives JSON-escaped \/ sequences
# (caller should unescape \/ → / before running this).
_MEDIA_EXT_PATTERN = re.compile(
    r'https?://[^\s"\'<>]+\.(' +
    '|'.join(re.escape(e) for e in _MEDIA_EXTENSIONS) +
    r')(?:[?#][^\s"\'<>]*)?',
    re.IGNORECASE,
)

# Sketchfab embed JSON often contains media.sketchfab.com URLs without a traditional file extension in <a href>.
_SKETCHFAB_CDN_PATTERN = re.compile(
    r"https://media\.sketchfab\.com/[^\s\"'<>]+",
    re.IGNORECASE,
)


def extract_sketchfab_model_uid(page_url: str) -> str | None:
    """32-char hex model id from /3d-models/...-UID or /models/UID."""
    m = re.search(r"/models/([0-9a-f]{32})(?:/|$|\?)", page_url, re.I)
    if m:
        return m.group(1)
    m = re.search(r"-([0-9a-f]{32})(?:/|$|\?)", page_url, re.I)
    if m:
        return m.group(1)
    return None


def sketchfab_cdn_assets_from_embed(model_uid: str, referer: str, headers: dict) -> list[dict]:
    """Fetch the embed page; 3D assets are injected in JSON (not plain <video> tags)."""
    embed_url = f"https://sketchfab.com/models/{model_uid}/embed"
    out: list[dict] = []
    seen: set[str] = set()
    try:
        r = http_client.get(embed_url, timeout=18, headers={**headers, "Referer": referer})
        if not r.ok:
            return out
        text = r.text.replace("\\/", "/").replace("&#34;", '"').replace("&quot;", '"')
        for m in _SKETCHFAB_CDN_PATTERN.finditer(text):
            raw = m.group(0)
            u = raw.rstrip('",;)}]\\')
            if u in seen or len(u) < 40:
                continue
            seen.add(u)
            ext = u.split("?")[0].split("#")[0].lower().rsplit(".", 1)[-1] if "." in u else ""
            media_type = _MEDIA_EXTENSIONS.get(ext)
            if not media_type:
                continue
            name = u.split("/")[-1].split("?")[0] or "file"
            out.append({"url": u, "type": media_type, "name": name, "referer": embed_url})
    except Exception:
        pass
    return out


# Prefer these sources when rewriting a viewer-only stream (e.g. .binz) to a real mesh for CAD export.
_MESH_CONVERT_SOURCE_EXTS: tuple[str, ...] = (
    "glb",
    "gltf",
    "obj",
    "stl",
    "ply",
    "dae",
    "x3d",
    "3ds",
    "fbx",
    "3mf",
)

_MESH_URL_IN_HTML_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+\.("
    + "|".join(re.escape(e) for e in _MESH_CONVERT_SOURCE_EXTS)
    + r")(?:[?#][^\s\"'<>]*)?",
    re.IGNORECASE,
)


def _sketchfab_uid_from_media_url(url: str) -> str | None:
    m = re.search(r"media\.sketchfab\.com/models/([0-9a-f]{32})/", url, re.I)
    if m:
        return m.group(1)
    return None


def _mesh_candidate_from_url(url: str) -> tuple[int, str, str] | None:
    """Score a downloadable URL if it looks like a mesh asset (any host)."""
    if not url.startswith(("http://", "https://")):
        return None
    low_full = url.lower()
    for junk in ("favicon", "/sprite", "/icons/", "apple-touch", "/widget", "/ads/"):
        if junk in low_full:
            return None
    parts = urlsplit(url)
    path = parts.path.lower()
    host_l = parts.netloc.lower()
    if "sketchfab.com" in host_l:
        for noise in (
            "/textures/",
            "/thumbnails/",
            "/avatars/",
            "/backgrounds/",
            "/environments/",
            "/matcaps/",
        ):
            if noise in path:
                return None
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
    if ext not in _MESH_CONVERT_SOURCE_EXTS:
        return None
    name = url.split("/")[-1].split("?")[0] or f"model.{ext}"
    return (_MESH_CONVERT_SOURCE_EXTS.index(ext), url, name)


def _collect_mesh_urls_from_json(obj) -> list[tuple[int, str, str]]:
    found: list[tuple[int, str, str]] = []
    if isinstance(obj, dict):
        for v in obj.values():
            found.extend(_collect_mesh_urls_from_json(v))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_collect_mesh_urls_from_json(v))
    elif isinstance(obj, str):
        hit = _mesh_candidate_from_url(obj)
        if hit:
            found.append(hit)
    return found


def _pick_best_mesh_candidate(raw: list[tuple[int, str, str]]) -> tuple[str, str] | None:
    if not raw:
        return None
    by_url: dict[str, tuple[int, str, str]] = {}
    for pri, u, name in raw:
        if u not in by_url or pri < by_url[u][0]:
            by_url[u] = (pri, u, name)
    merged = list(by_url.values())
    merged.sort(key=lambda t: (t[0], len(t[1])))
    _, best_url, best_name = merged[0]
    return (best_url, best_name)


def _sketchfab_binz_mesh_candidates(file_url: str, referer: str, headers: dict) -> list[tuple[int, str, str]]:
    """Sketchfab embed + /i/models JSON (only when we can resolve a model uid)."""
    uid = _sketchfab_uid_from_media_url(file_url) or extract_sketchfab_model_uid(referer or "")
    if not uid:
        return []
    base_sf_headers = {
        "User-Agent": headers["User-Agent"],
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer if referer and "sketchfab.com" in referer.lower() else "https://sketchfab.com/",
    }
    candidates: list[tuple[int, str, str]] = []
    embed_url = f"https://sketchfab.com/models/{uid}/embed"
    try:
        r = http_client.get(
            embed_url,
            timeout=18,
            headers={
                **base_sf_headers,
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            },
        )
        if r.ok:
            text = r.text.replace("\\/", "/").replace("&#34;", '"').replace("&quot;", '"')
            for m in _SKETCHFAB_CDN_PATTERN.finditer(text):
                u = m.group(0).rstrip('",;)}]\\')
                hit = _mesh_candidate_from_url(u)
                if hit:
                    candidates.append(hit)
    except Exception:
        pass

    try:
        ir = http_client.get(
            f"https://sketchfab.com/i/models/{uid}",
            timeout=14,
            headers={**base_sf_headers, "Accept": "application/json"},
        )
        if ir.ok:
            candidates.extend(_collect_mesh_urls_from_json(ir.json()))
    except Exception:
        pass
    return candidates


def discover_mesh_urls_from_referer_page(referer: str, headers: dict) -> list[tuple[int, str, str]]:
    """Scan any referer HTML/JSON page for mesh asset URLs (works for arbitrary sites)."""
    out: list[tuple[int, str, str]] = []
    try:
        rh = {
            "User-Agent": headers["User-Agent"],
            "Accept-Language": headers.get("Accept-Language", "en-US,en;q=0.9"),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Referer": referer,
        }
        r = http_client.get(referer, timeout=18, headers=rh)
        if not r.ok:
            return out
        text = r.text.replace("\\/", "/").replace("&#34;", '"').replace("&quot;", '"')
        ct = r.headers.get("Content-Type", "").lower()
        snippet = text.lstrip()[:8000]
        if "json" in ct or snippet.startswith("{") or snippet.startswith("["):
            try:
                out.extend(_collect_mesh_urls_from_json(r.json()))
            except Exception:
                pass
        if "html" in ct or "<html" in text[:3000].lower() or "doctype html" in text[:3000].lower():
            for m in _MESH_URL_IN_HTML_PATTERN.finditer(text):
                u = m.group(0).rstrip('",;)}]\\')
                hit = _mesh_candidate_from_url(u)
                if hit:
                    out.append(hit)
            try:
                soup = BeautifulSoup(text, "html.parser")
                for tag in soup.find_all(["a", "link", "source", "iframe"]):
                    for attr in ("href", "src"):
                        v = tag.get(attr)
                        if v and isinstance(v, str) and v.startswith("http"):
                            hit = _mesh_candidate_from_url(urljoin(referer, v.strip()))
                            if hit:
                                out.append(hit)
            except Exception:
                pass
    except Exception:
        pass
    return out


def resolve_binz_stream_to_mesh_url(file_url: str, referer: str, headers: dict) -> tuple[str, str] | None:
    """
    For viewer .binz streams, try to find a real mesh URL: Sketchfab APIs if applicable,
    plus the referer page for any host (HTML links, JSON config blobs).
    """
    path = urlsplit(file_url).path.lower()
    if not path.endswith(".binz"):
        return None
    cands: list[tuple[int, str, str]] = []
    cands.extend(_sketchfab_binz_mesh_candidates(file_url, referer, headers))
    ref = (referer or "").strip()
    if ref.startswith(("http://", "https://")):
        cands.extend(discover_mesh_urls_from_referer_page(ref, headers))
    return _pick_best_mesh_candidate(cands)


@app.post("/api/scrape")
def api_scrape():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required."}), 400
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = http_client.get(url, timeout=15, headers=headers)
        resp.raise_for_status()
    except http_client.exceptions.Timeout:
        return jsonify({"error": "Request timed out. The page took too long to respond."}), 400
    except http_client.exceptions.ConnectionError:
        return jsonify({"error": "Could not connect to that URL. Check the address and try again."}), 400
    except Exception as exc:
        return jsonify({"error": f"Failed to fetch page: {exc}"}), 400

    soup = BeautifulSoup(resp.text, "html.parser")
    found = {}

    def _ext(href):
        clean = href.split("?")[0].split("#")[0].lower()
        return clean.rsplit(".", 1)[-1] if "." in clean else ""

    def add(href, forced_type=None):
        if not href or href.startswith(("data:", "javascript:", "mailto:", "#")):
            return
        full = urljoin(url, href)
        if full in found:
            return
        media_type = forced_type or _MEDIA_EXTENSIONS.get(_ext(full))
        if media_type:
            name = full.split("/")[-1].split("?")[0] or "file"
            found[full] = {"url": full, "type": media_type, "name": name, "referer": url}

    for tag in soup.find_all("img"):
        add(tag.get("src"), "image")
        for part in (tag.get("srcset") or "").split(","):
            p = part.strip().split()
            if p:
                add(p[0], "image")

    for tag in soup.find_all("video"):
        add(tag.get("src"), "video")
        for s in tag.find_all("source"):
            add(s.get("src"), "video")

    for tag in soup.find_all("audio"):
        add(tag.get("src"), "audio")
        for s in tag.find_all("source"):
            add(s.get("src"), "audio")

    for tag in soup.find_all("a", href=True):
        add(tag["href"])

    # Scan inline <script> / JSON blobs for media URLs.
    # JSON encodes forward slashes as \/ — unescape before matching so
    # https:\/\/cdn.example.com\/model.glb becomes a valid URL.
    for script in soup.find_all("script"):
        content = script.string
        if not content:
            continue
        unescaped = content.replace("\\/", "/")
        for match in _MEDIA_EXT_PATTERN.finditer(unescaped):
            add(match.group(0))

    # Scan data-* attributes on any element (common in lazy-load and 3D viewer setups)
    for tag in soup.find_all(True):
        for attr, val in tag.attrs.items():
            if attr.startswith("data-") and isinstance(val, str) and val.startswith("http"):
                add(val)

    # Sketchfab: model files load from CDN inside the embed document, not the public model page HTML.
    if "sketchfab.com" in urlsplit(url).netloc.lower():
        uid = extract_sketchfab_model_uid(url)
        if not uid:
            try:
                oe = http_client.get(
                    "https://sketchfab.com/oembed",
                    params={"url": url},
                    timeout=12,
                    headers=headers,
                )
                if oe.ok:
                    html = oe.json().get("html") or ""
                    m = re.search(
                        r"sketchfab\.com/models/([0-9a-f]{32})/embed",
                        html,
                        re.I,
                    )
                    if m:
                        uid = m.group(1)
            except Exception:
                pass
        if uid:
            for item in sketchfab_cdn_assets_from_embed(uid, url, headers):
                if item["url"] not in found:
                    found[item["url"]] = item

    return jsonify({"media": list(found.values()), "count": len(found)})


def _encode_url(url: str) -> str:
    """Normalize URL path encoding.

    Handles three cases:
    1. Raw non-ASCII chars in path  → percent-encode as UTF-8
    2. Correct %E6%BC%86 encoding   → preserved unchanged
    3. Mojibake %C3%A6%C2%BC%C2%86 → fixed back to %E6%BC%86
       (happens when UTF-8 bytes are misread as Latin-1 code points and re-encoded)
    """
    try:
        parts = urlsplit(url)
        # Decode all %XX sequences to raw bytes (non-%XX chars are UTF-8 encoded first)
        raw = unquote_to_bytes(parts.path)
        try:
            decoded = raw.decode("utf-8")
            non_ascii = [c for c in decoded if not c.isascii()]
            # If every non-ASCII char fits in a single byte it's likely mojibake:
            # UTF-8 bytes were treated as Latin-1 code points then re-encoded as UTF-8.
            if non_ascii and all(ord(c) < 0x100 for c in non_ascii):
                raw = decoded.encode("latin-1")  # recover original UTF-8 bytes
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        safe_path = quote(raw, safe=b"/:@!$&'()*+,;=~-._")
        return urlunsplit((parts.scheme, parts.netloc, safe_path, parts.query, parts.fragment))
    except Exception:
        return url


# (suffix, mimetype, trimesh file_type)
CAD_EXPORT_FORMATS: dict[str, tuple[str, str, str]] = {
    "dxf": (".dxf", "application/dxf", "dxf"),
    "step": (".step", "application/STEP", "step"),
    "stl": (".stl", "model/stl", "stl"),
    "obj": (".obj", "model/obj", "obj"),
    "glb": (".glb", "model/gltf-binary", "glb"),
}

MAX_CAD_INPUT_BYTES = 120 * 1024 * 1024


def _run_universal_cad_export(src: str, dest: str, file_type: str) -> None:
    """Best-effort: trimesh, meshio (VTU/VTK/MED/etc.), ZIP of glTF/OBJ, then export to the target format."""
    import json

    s_src = json.dumps(os.path.abspath(src))
    s_dest = json.dumps(os.path.abspath(dest))
    s_ft = json.dumps(file_type)
    script = f"""
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import numpy as np
import trimesh


def scene_to_mesh(loaded):
    if loaded is None:
        return None
    if isinstance(loaded, trimesh.Scene):
        parts = [g for g in loaded.geometry.values() if isinstance(g, trimesh.Trimesh)]
        if not parts:
            return None
        return trimesh.util.concatenate(parts) if len(parts) > 1 else parts[0]
    if isinstance(loaded, trimesh.Trimesh):
        return loaded
    return None


def try_trimesh_load(path: Path):
    for force in (None, "mesh", "scene"):
        try:
            if force is None:
                loaded = trimesh.load(str(path), process=True)
            else:
                loaded = trimesh.load(str(path), force=force, process=True)
        except (NotImplementedError, OSError, ValueError, AttributeError, TypeError):
            loaded = None
        except Exception:
            loaded = None
        mesh = scene_to_mesh(loaded)
        if mesh is not None and not mesh.is_empty:
            return mesh
    return None


def tet_boundary_triangles(tets):
    f = np.vstack(
        [
            tets[:, [0, 1, 2]],
            tets[:, [0, 1, 3]],
            tets[:, [0, 2, 3]],
            tets[:, [1, 2, 3]],
        ]
    )
    fs = np.sort(f, axis=1)
    u, counts = np.unique(fs, axis=0, return_counts=True)
    return u[counts == 1]


def mesh_from_meshio(path: Path):
    import meshio

    m = meshio.read(str(path))
    pts = np.asarray(m.points, dtype=np.float64)
    if pts.size == 0:
        return None
    cd = m.cells_dict
    faces = None
    if "triangle" in cd:
        faces = np.asarray(cd["triangle"], dtype=np.int64)
    elif "triangle6" in cd:
        t6 = np.asarray(cd["triangle6"], dtype=np.int64)
        faces = t6[:, :3]
    elif "quad" in cd:
        q = np.asarray(cd["quad"], dtype=np.int64)
        faces = np.vstack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])
    elif "quad8" in cd:
        q = np.asarray(cd["quad8"], dtype=np.int64)
        faces = np.vstack([q[:, [0, 1, 2]], q[:, [0, 2, 3]]])
    elif "tetra" in cd:
        tets = np.asarray(cd["tetra"], dtype=np.int64)
        faces = tet_boundary_triangles(tets)
    elif "tetra10" in cd:
        tets = np.asarray(cd["tetra10"], dtype=np.int64)
        faces = tet_boundary_triangles(tets[:, :4])
    if faces is None or faces.size == 0:
        return None
    mesh = trimesh.Trimesh(vertices=pts, faces=faces, process=True)
    return None if mesh.is_empty else mesh


def prepare_work_path(raw: Path):
    if raw.suffix.lower() != ".zip":
        return raw, None
    td = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(raw) as z:
            z.extractall(td)
    except Exception:
        shutil.rmtree(td, ignore_errors=True)
        return None, None
    order = [
        ".glb",
        ".gltf",
        ".obj",
        ".stl",
        ".ply",
        ".dae",
        ".x3d",
        ".3ds",
        ".vtk",
        ".vtu",
    ]
    best = []
    for p in Path(td).rglob("*"):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in order:
            best.append((order.index(suf), -p.stat().st_size, p))
    if not best:
        shutil.rmtree(td, ignore_errors=True)
        return None, None
    best.sort(key=lambda x: (x[0], x[1]))
    return best[0][2], td


src = Path({s_src})
dest = Path({s_dest})
ft = {s_ft}
tmpdir = None
try:
    work, tmpdir = prepare_work_path(src)
    if work is None:
        sys.exit(2)
    mesh = try_trimesh_load(work)
    if mesh is None:
        try:
            mesh = mesh_from_meshio(work)
        except Exception:
            mesh = None
    if mesh is None and work != src:
        mesh = try_trimesh_load(src)
    if mesh is None:
        try:
            mesh = mesh_from_meshio(src)
        except Exception:
            mesh = None
    if mesh is None or mesh.is_empty:
        sys.exit(2)
    mesh.export(str(dest), file_type=ft)
finally:
    if tmpdir is not None:
        shutil.rmtree(tmpdir, ignore_errors=True)
"""
    subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        timeout=240,
    )


@app.get("/api/proxy-download")
def proxy_download():
    file_url = request.args.get("url", "").strip()
    filename = request.args.get("name", "").strip()
    referer = request.args.get("referer", "").strip()
    cad_format = (request.args.get("cad_format") or "").strip().lower()
    inline = request.args.get("inline") == "1"
    if not file_url:
        return jsonify({"error": "URL required."}), 400

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if referer:
        headers["Referer"] = referer
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(referer)
        headers["Origin"] = f"{parsed.scheme}://{parsed.netloc}"

    try:
        if cad_format and cad_format not in CAD_EXPORT_FORMATS:
            return jsonify({"error": "Invalid cad_format. Use dxf, step, stl, obj, or glb."}), 400

        eff_url = file_url
        cad_effective = cad_format if cad_format in CAD_EXPORT_FORMATS else ""
        binz_cad_no_mesh_source = False
        if cad_effective and not inline:
            path_low = urlsplit(file_url).path.lower()
            name_suf = Path(filename).suffix.lower() if filename else ""
            if path_low.endswith(".binz") or name_suf == ".binz":
                alt = resolve_binz_stream_to_mesh_url(file_url, referer, headers)
                if alt:
                    eff_url, mesh_name = alt
                    filename = mesh_name
                else:
                    # No mesh URL on embed/API/referer: serve original .binz (conversion not possible here).
                    cad_effective = ""
                    binz_cad_no_mesh_source = True

        r = http_client.get(
            _encode_url(eff_url),
            stream=True,
            timeout=30,
            headers=headers,
        )
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        if not filename:
            filename = eff_url.split("/")[-1].split("?")[0] or "download"

        if (not cad_effective) or inline:

            def generate():
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        yield chunk

            response = Response(stream_with_context(generate()), content_type=content_type)
            if not inline:
                response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
                if binz_cad_no_mesh_source:
                    response.headers["X-ClipFetch-CAD"] = (
                        "skipped: no GLB/glTF/OBJ/etc. found for this .binz (check referer page)"
                    )
            return response

        suffix, mime_out, tri_type = CAD_EXPORT_FORMATS[cad_effective]
        dl_name = Path(filename).stem + suffix
        src_suffix = Path(filename).suffix.lower()

        tmp_in = None
        try:
            tmp_in = tempfile.NamedTemporaryFile(delete=False, suffix=src_suffix or ".bin")
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_CAD_INPUT_BYTES:
                    tmp_in.close()
                    os.unlink(tmp_in.name)
                    return jsonify({"error": "File too large for CAD conversion."}), 400
                tmp_in.write(chunk)
            tmp_in.close()
            src_path = tmp_in.name

            if src_suffix == suffix:
                resp = send_file(
                    src_path,
                    as_attachment=True,
                    download_name=Path(filename).name,
                    mimetype=mime_out,
                )

                @after_this_request
                def _clean_same(_resp):
                    try:
                        os.unlink(src_path)
                    except OSError:
                        pass
                    return _resp

                return resp

            out_fd, out_path = tempfile.mkstemp(suffix=suffix)
            os.close(out_fd)
            try:
                _run_universal_cad_export(os.path.abspath(src_path), os.path.abspath(out_path), tri_type)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                resp = send_file(
                    src_path,
                    as_attachment=True,
                    download_name=filename,
                    mimetype=content_type,
                )

                @after_this_request
                def _clean_fb(_resp):
                    try:
                        os.unlink(src_path)
                    except OSError:
                        pass
                    return _resp

                return resp
            try:
                os.unlink(src_path)
            except OSError:
                pass

            resp = send_file(
                out_path,
                as_attachment=True,
                download_name=dl_name,
                mimetype=mime_out,
            )

            @after_this_request
            def _clean_out(_resp):
                try:
                    os.unlink(out_path)
                except OSError:
                    pass
                return _resp

            return resp
        except Exception:
            if tmp_in is not None:
                try:
                    if not tmp_in.closed:
                        tmp_in.close()
                except Exception:
                    pass
                try:
                    os.unlink(tmp_in.name)
                except Exception:
                    pass
            return jsonify({"error": "Failed to download or convert file."}), 400
    except Exception:
        return jsonify({"error": "Failed to download file."}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
