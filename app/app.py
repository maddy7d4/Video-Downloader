import re
import shutil
import subprocess
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
    "jpg": "image", "jpeg": "image", "png": "image", "gif": "image",
    "webp": "image", "svg": "image", "bmp": "image", "ico": "image",
    "tiff": "image", "avif": "image",
    "mp4": "video", "webm": "video", "avi": "video", "mov": "video",
    "mkv": "video", "flv": "video", "m4v": "video", "ogv": "video",
    "mp3": "audio", "wav": "audio", "m4a": "audio", "ogg": "audio",
    "flac": "audio", "aac": "audio",
    "pdf": "document",
    "dwg": "cad", "dxf": "cad", "step": "cad", "stp": "cad",
    "stl": "cad", "obj": "cad", "fbx": "cad", "iges": "cad", "igs": "cad",
    "zip": "archive", "rar": "archive", "7z": "archive", "tar": "archive", "gz": "archive",
}


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


@app.get("/api/proxy-download")
def proxy_download():
    file_url = request.args.get("url", "").strip()
    filename = request.args.get("name", "").strip()
    referer = request.args.get("referer", "").strip()
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
        r = http_client.get(
            _encode_url(file_url),
            stream=True,
            timeout=30,
            headers=headers,
        )
        r.raise_for_status()
        content_type = r.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
        if not filename:
            filename = file_url.split("/")[-1].split("?")[0] or "download"

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        response = Response(stream_with_context(generate()), content_type=content_type)
        if request.args.get("inline") != "1":
            response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
    except Exception:
        return jsonify({"error": "Failed to download file."}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
