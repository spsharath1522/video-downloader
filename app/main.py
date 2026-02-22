"""
Media Downloader API: fetch formats and download with video+audio.
Supports YouTube, Spotify, Apple Music, and other yt-dlp sites.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, HttpUrl

# In-memory store for download job progress: job_id -> { status, progress, path?, filename?, error? }
DOWNLOAD_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()

# aria2: Debian/Ubuntu package allows max 16 per server. Build from source to use more (e.g. 32).
ARIA2_MAX_CONNECTIONS = 16
# Splits per file (audio / single-URL downloads)
ARIA2_SPLITS = 5000
# Speed: buffer 4 MiB; chunk 100 MiB (under 1000 MB)
DOWNLOAD_BUFFER_SIZE = 4 * 1024 * 1024    # 4 MiB
HTTP_CHUNK_SIZE = 100 * 1024 * 1024      # 100 MiB
# Parallel fragment downloads (video/DASH)
CONCURRENT_FRAGMENT_DOWNLOADS = 5000

# URL patterns for special handling
SPOTIFY_DOMAINS = ("open.spotify.com", "spotify.com")
APPLE_MUSIC_DOMAINS = ("music.apple.com", "itunes.apple.com")

# Cloudflare / impersonation: use any available browser target (requires curl_cffi)
IMPERSONATE_TARGET = ""  # "" = any; or "chrome" / "safari" / "chrome:windows-10"

app = FastAPI(title="Media Downloader")


@app.on_event("startup")
def _log_impersonation_status():
    if _use_impersonation():
        return
    import logging
    logging.getLogger("uvicorn.error").warning(
        "Cloudflare/impersonation not available (install: pip install curl_cffi). "
        "Some sites (e.g. youx.xxx) may return 403."
    )


def _impersonation_available() -> bool:
    """True if curl_cffi is installed and yt-dlp can use it (for Cloudflare sites). We don't test via YoutubeDL(impersonate=...) because that triggers an AssertionError in some yt-dlp versions."""
    try:
        from yt_dlp.networking import _curlcffi  # noqa: F401
        return True
    except Exception:
        return False


# Check once at startup so we only add impersonate opts when curl_cffi is available
_IMPERSONATION_AVAILABLE: bool | None = None


def _use_impersonation() -> bool:
    global _IMPERSONATION_AVAILABLE
    if _IMPERSONATION_AVAILABLE is None:
        _IMPERSONATION_AVAILABLE = _impersonation_available()
    return _IMPERSONATION_AVAILABLE

# Where to store downloads (temporary; served then can be cleaned)
DOWNLOADS_DIR = Path(tempfile.gettempdir()) / "media-downloader"
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


class UrlInput(BaseModel):
    url: HttpUrl


class FormatChoice(BaseModel):
    id: str
    label: str
    format_spec: str
    note: str


class DownloadRequest(BaseModel):
    url: HttpUrl
    format_spec: str | None = None


def _sanitize_filename(name: str, max_length: int = 200) -> str:
    """Make a string safe for use as a filename (keep one extension handled by caller)."""
    if not name or not name.strip():
        return "media"
    # Remove/replace chars that are invalid in filenames on common OSes
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.strip().strip(".")
    return name[:max_length] if name else "media"


def _ffmpeg_available() -> bool:
    """Return True if ffmpeg is installed (needed for merging video+audio)."""
    return shutil.which("ffmpeg") is not None


def _aria2c_available() -> bool:
    """Return True if aria2c is installed (faster parallel downloads)."""
    return shutil.which("aria2c") is not None


def _is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _apply_youtube_cookies(url: str, ydl_opts: dict) -> None:
    """Use browser cookies for YouTube to avoid 'Sign in to confirm you're not a bot'."""
    if not _is_youtube_url(url):
        return
    # Use Chrome cookies; if you use Firefox, change to "firefox"
    ydl_opts["cookiesfrombrowser"] = "chrome"


def _apply_cloudflare_opts(url: str, ydl_opts: dict) -> None:
    """Add generic extractor impersonation and Referer for Cloudflare sites. Don't set top-level impersonate (triggers AssertionError in yt-dlp init); generic extractor uses extractor_args."""
    ydl_opts["extractor_args"] = {"generic": {"impersonate": ["chrome"]}}
    if "/embed/" in url:
        ydl_opts["force_generic_extractor"] = True
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        if p.netloc:
            ydl_opts.setdefault("add_headers", {})["Referer"] = f"{p.scheme or 'https'}://{p.netloc}/"
    except Exception:
        pass


def _get_js_runtimes() -> list[str]:
    """Return list of JS runtime names to pass to yt-dlp (avoids YouTube 'no JS runtime' warning)."""
    runtimes = []
    if shutil.which("deno"):
        runtimes.append("deno")
    if shutil.which("node") and "node" not in runtimes:
        runtimes.append("node")
    return runtimes


def _is_spotify_url(url: str) -> bool:
    return any(d in url for d in SPOTIFY_DOMAINS)


def _is_apple_music_url(url: str) -> bool:
    return any(d in url for d in APPLE_MUSIC_DOMAINS)


def _spotdl_available() -> bool:
    try:
        import spotdl  # noqa: F401
        return True
    except ImportError:
        return False


def _get_spotify_track_title(url: str) -> str:
    """Get track title from Spotify URL via spotdl save (metadata only)."""
    if not _spotdl_available():
        return "Spotify track"
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    try:
        subprocess.run(
            [sys.executable, "-m", "spotdl", "save", url, "--save-file", tmp.name],
            capture_output=True,
            timeout=30,
            cwd=str(DOWNLOADS_DIR),
        )
        with open(tmp.name, encoding="utf-8") as f:
            data = json.load(f)
        # spotdl save can return a list (playlist) or single object
        items = data if isinstance(data, list) else [data]
        if items and isinstance(items[0], dict):
            first = items[0]
            name = first.get("name") or first.get("title") or first.get("song")
            artists = first.get("artists") or first.get("artist")
            if name:
                if isinstance(artists, list) and artists:
                    a = artists[0]
                    artist = a.get("name", a) if isinstance(a, dict) else str(a)
                    return f"{name} - {artist}" if artist else name
                if isinstance(artists, str):
                    return f"{name} - {artists}"
                return str(name)
    except (Exception, FileNotFoundError):
        pass
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return "Spotify track"


def _download_spotify(url: str) -> tuple[str, str]:
    """
    Download Spotify track/album/playlist via spotdl. Returns (file_path, display_filename).
    For single track returns one file; for playlist/album returns first file (or we could zip).
    """
    if not _spotdl_available():
        raise HTTPException(
            status_code=400,
            detail="Spotify downloads require spotdl. Install with: pip install spotdl",
        )
    subdir = DOWNLOADS_DIR / uuid.uuid4().hex
    subdir.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "spotdl", "download", url, "--output", "{title}.{output-ext}"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(subdir),
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()
            raise HTTPException(status_code=400, detail=f"Spotify download failed: {err or 'Unknown error'}")
        # Find downloaded file(s) – single track usually one file
        files = [f for f in subdir.iterdir() if f.is_file() and f.suffix.lower() in (".mp3", ".m4a", ".opus", ".ogg")]
        if not files:
            raise HTTPException(status_code=500, detail="Spotify download produced no audio file")
        path = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[0]
        filename = path.name
        return str(path), filename
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=400, detail="Spotify download timed out")


def _sanitize(info: dict) -> dict:
    """Make yt_dlp info JSON-serializable."""
    if info is None:
        return {}
    # Remove non-serializable entries
    return {
        k: v
        for k, v in info.items()
        if not k.startswith("_") and v is not None
    }


def _build_merged_format_options(info: dict) -> list[dict]:
    """
    Build format options (video+audio). When ffmpeg is missing, use only
    single-file formats to avoid merge (no ffmpeg required).
    """
    formats = info.get("formats") or []
    has_video = any(f.get("vcodec") and f.get("vcodec") != "none" for f in formats)
    has_audio = any(f.get("acodec") and f.get("acodec") != "none" for f in formats)
    can_merge = _ffmpeg_available()

    options = []

    if has_video and has_audio:
        heights = sorted(
            {f.get("height") for f in formats if f.get("height") and isinstance(f.get("height"), int)},
            reverse=True,
        )
        if can_merge:
            # Merge best video + best audio per resolution (needs ffmpeg)
            for h in heights[:8]:
                options.append({
                    "id": f"height_{h}",
                    "label": f"{h}p (video + audio)",
                    "format_spec": f"bestvideo[height<={h}]+bestaudio/best[height<={h}]",
                    "note": f"Up to {h}p, merged",
                })
            options.append({
                "id": "best",
                "label": "Best (video + audio)",
                "format_spec": "bestvideo+bestaudio/best",
                "note": "Best available quality",
            })
            # Audio only (music)
            options.append({
                "id": "audio_only",
                "label": "Audio only (music)",
                "format_spec": "bestaudio/best",
                "note": "Best audio quality, no video",
            })
        else:
            # No ffmpeg: only single-file formats (pre-merged by site)
            for h in heights[:8]:
                options.append({
                    "id": f"height_{h}",
                    "label": f"{h}p (video + audio)",
                    "format_spec": f"best[height<={h}]",
                    "note": f"Up to {h}p (no ffmpeg)",
                })
            options.append({
                "id": "best",
                "label": "Best (video + audio)",
                "format_spec": "best",
                "note": "Best single file (install ffmpeg for more options)",
            })
        # Audio only (music) – always offer when both video and audio exist
        options.append({
            "id": "audio_only",
            "label": "Audio only (music)",
            "format_spec": "bestaudio/best",
            "note": "Best audio quality, no video",
        })
    elif has_audio and not has_video:
        options.append({
            "id": "best_audio",
            "label": "Best audio",
            "format_spec": "bestaudio/best",
            "note": "Audio only",
        })
    else:
        options.append({
            "id": "best",
            "label": "Best",
            "format_spec": "best",
            "note": "Single stream",
        })

    return options


def _run_download_job(job_id: str, url: str, format_spec: str) -> None:
    """Run in a thread: download media and update DOWNLOAD_JOBS[job_id] with progress."""
    with _JOBS_LOCK:
        DOWNLOAD_JOBS[job_id] = {"status": "downloading", "progress": 0, "path": None, "filename": None, "error": None}

    def set_progress(progress: int, status: str = "downloading") -> None:
        with _JOBS_LOCK:
            if job_id in DOWNLOAD_JOBS:
                # Only ever increase progress (yt-dlp may report per-file for merge: video 0-100, then audio 0-100)
                prev = DOWNLOAD_JOBS[job_id].get("progress") or 0
                DOWNLOAD_JOBS[job_id]["progress"] = min(100, max(prev, progress))
                DOWNLOAD_JOBS[job_id]["status"] = status

    try:
        if _is_spotify_url(url):
            set_progress(10)
            path, filename = _download_spotify(url)
            with _JOBS_LOCK:
                DOWNLOAD_JOBS[job_id] = {"status": "done", "progress": 100, "path": path, "filename": filename, "error": None}
            return

        format_spec = format_spec or ("bestvideo+bestaudio/best" if _ffmpeg_available() else "best")
        out_template = str(DOWNLOADS_DIR / f"%(id)s_%(title).100s.%(ext)s")

        def progress_hook(d: dict) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total and total > 0:
                    pct = int(100 * (d.get("downloaded_bytes") or 0) / total)
                    set_progress(pct)
                else:
                    set_progress(50)  # unknown total
            elif d.get("status") == "finished":
                set_progress(100)

        ydl_opts = {
            "format": format_spec,
            "outtmpl": out_template,
            "quiet": False,
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
            "buffersize": DOWNLOAD_BUFFER_SIZE,
            "http_chunk_size": HTTP_CHUNK_SIZE,
        }
        _apply_cloudflare_opts(url, ydl_opts)
        _apply_youtube_cookies(url, ydl_opts)
        js = _get_js_runtimes()
        if js:
            ydl_opts["js_runtimes"] = js
        if "+" in format_spec and _ffmpeg_available():
            ydl_opts["merge_output_format"] = "mp4"  # remux only, original quality
        if _aria2c_available():
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = {"aria2c": ["-x", str(ARIA2_MAX_CONNECTIONS), "-s", str(ARIA2_SPLITS), "-k", "1M", "-j", str(ARIA2_MAX_CONNECTIONS), "--min-split-size=1M"]}

        info = None
        path = None
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                path = ydl.prepare_filename(info)

        if not info or not path or not os.path.isfile(path):
            req = info.get("requested_downloads") or [] if info else []
            path = req[0].get("filepath") if req else None
        if not path or not os.path.isfile(path):
            with _JOBS_LOCK:
                DOWNLOAD_JOBS[job_id] = {"status": "error", "progress": 0, "path": None, "filename": None, "error": "Downloaded file not found"}
            return

        ext = os.path.splitext(path)[1] or ".mp4"
        title = info.get("title") or info.get("fulltitle") or info.get("track")
        if not title or not str(title).strip():
            stem = os.path.splitext(os.path.basename(path))[0]
            vid = info.get("id")
            if vid and stem.startswith(str(vid) + "_"):
                title = stem[len(str(vid)) + 1 :].strip()
            else:
                title = stem or "media"
        filename = _sanitize_filename(str(title).strip()) + ext
        if not filename or filename == ".mp4":
            filename = os.path.basename(path)

        with _JOBS_LOCK:
            DOWNLOAD_JOBS[job_id] = {"status": "done", "progress": 100, "path": path, "filename": filename, "error": None}
    except Exception as e:
        err = str(e)
        if "impersonat" in err.lower() and ("not available" in err.lower() or "Cloudflare" in err):
            err = "Cloudflare/impersonation needs curl_cffi. Run: pip install curl_cffi  then restart the app."
        with _JOBS_LOCK:
            DOWNLOAD_JOBS[job_id] = {"status": "error", "progress": 0, "path": None, "filename": None, "error": err}


@app.get("/api/impersonation-status")
def impersonation_status():
    """Check if curl_cffi/impersonation is available in this process (for Cloudflare sites)."""
    try:
        import curl_cffi
        curl_ok = True
    except ImportError:
        curl_ok = False
    ydl_ok = _impersonation_available()
    return {
        "curl_cffi_installed": curl_ok,
        "yt_dlp_impersonation_available": ydl_ok,
        "hint": "Start the app with the same env where you installed curl_cffi: ./venv/bin/uvicorn app.main:app --reload",
    }


@app.post("/api/download/start", response_model=dict)
def download_start(body: DownloadRequest):
    """Start a download in the background. Returns job_id for polling status and fetching the file."""
    job_id = uuid.uuid4().hex
    thread = threading.Thread(
        target=_run_download_job,
        args=(job_id, str(body.url), body.format_spec or ""),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/api/download/status/{job_id}")
def download_status(job_id: str):
    """Return current status and progress (0-100) for a download job."""
    with _JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job["status"],
        "progress": job["progress"],
        "filename": job.get("filename"),
        "error": job.get("error"),
    }


@app.get("/api/download/file/{job_id}")
def download_file(job_id: str):
    """Return the downloaded file when status is done."""
    with _JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "done" or not job.get("path") or not os.path.isfile(job["path"]):
        raise HTTPException(status_code=400, detail="File not ready or missing")
    filename = job.get("filename") or os.path.basename(job["path"])
    return FileResponse(
        job["path"],
        media_type="application/octet-stream",
        filename=filename,
    )


@app.post("/api/formats", response_model=dict)
def get_formats(body: UrlInput):
    """Extract available formats for the given URL. Returns title and format options."""
    url = str(body.url)

    # Spotify: use spotdl for metadata, offer audio-only
    if _is_spotify_url(url):
        if not _spotdl_available():
            raise HTTPException(
                status_code=400,
                detail="Spotify links require spotdl. Install with: pip install spotdl",
            )
        title = _get_spotify_track_title(url)
        return {
            "title": title,
            "url": url,
            "formats": [
                {
                    "id": "audio_only",
                    "label": "Audio only (music)",
                    "format_spec": "spotify_audio",
                    "note": "Download as audio via YouTube match",
                },
            ],
        }

    # Apple Music and others: try yt-dlp (impersonate for Cloudflare sites e.g. youx.xxx)
    ydl_opts = {"quiet": True, "no_warnings": True, "extract_flat": False}
    _apply_cloudflare_opts(url, ydl_opts)
    _apply_youtube_cookies(url, ydl_opts)
    js = _get_js_runtimes()
    if js:
        ydl_opts["js_runtimes"] = js
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        err_msg = str(e)
        if "impersonat" in err_msg.lower() and ("not available" in err_msg.lower() or "Cloudflare" in err_msg):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Cloudflare/impersonation needs curl_cffi. In terminal run:  pip install curl_cffi   then restart the app."
                ),
            )
        if "Cloudflare" in err_msg:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Site blocked by Cloudflare. Install:  pip install curl_cffi   then restart the app."
                ),
            )
        raise HTTPException(status_code=400, detail=f"Could not fetch formats: {err_msg}")

    if not info:
        raise HTTPException(status_code=400, detail="No info returned for this URL")

    title = info.get("title") or "Unknown"
    options = _build_merged_format_options(info)

    return {
        "title": title,
        "url": url,
        "formats": options,
    }


@app.post("/api/download")
def download_media(body: DownloadRequest):
    """
    Download media with the selected format. Supports YouTube, Spotify, Apple Music, etc.
    """
    url = str(body.url)
    format_spec = body.format_spec or ""

    # Spotify: use spotdl (format_spec can be "spotify_audio" or any)
    if _is_spotify_url(url):
        path, filename = _download_spotify(url)
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=filename,
        )

    # yt-dlp for all other URLs (YouTube, Apple Music, etc.)
    format_spec = format_spec or ("bestvideo+bestaudio/best" if _ffmpeg_available() else "best")

    out_template = str(DOWNLOADS_DIR / f"%(id)s_%(title).100s.%(ext)s")
    ydl_opts = {
        "format": format_spec,
        "outtmpl": out_template,
        "quiet": False,
        "concurrent_fragment_downloads": CONCURRENT_FRAGMENT_DOWNLOADS,
        "buffersize": DOWNLOAD_BUFFER_SIZE,
        "http_chunk_size": HTTP_CHUNK_SIZE,
    }
    _apply_cloudflare_opts(url, ydl_opts)
    _apply_youtube_cookies(url, ydl_opts)
    js = _get_js_runtimes()
    if js:
        ydl_opts["js_runtimes"] = js
    if "+" in format_spec and _ffmpeg_available():
        ydl_opts["merge_output_format"] = "mp4"  # remux only, original quality
    # aria2c: max throughput
    if _aria2c_available():
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = {
            "aria2c": ["-x", str(ARIA2_MAX_CONNECTIONS), "-s", str(ARIA2_SPLITS), "-k", "1M", "-j", str(ARIA2_MAX_CONNECTIONS), "--min-split-size=1M"],
        }

    path = None
    info = None
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info:
                path = ydl.prepare_filename(info)
    except Exception as e:
        err = str(e)
        if "impersonat" in err.lower() and ("not available" in err.lower() or "Cloudflare" in err):
            err = "Cloudflare/impersonation needs curl_cffi. Run: pip install curl_cffi  then restart the app."
        raise HTTPException(status_code=400, detail=f"Download failed: {err}")

    if not info:
        raise HTTPException(status_code=400, detail="Download produced no file")

    if not path or not os.path.isfile(path):
        req = info.get("requested_downloads") or []
        path = req[0].get("filepath") if req else None
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=500, detail="Downloaded file not found")

    ext = os.path.splitext(path)[1] or ".mp4"
    # Prefer title from metadata; fallback to filename yt-dlp wrote (our template: id_title.ext)
    title = (
        info.get("title")
        or info.get("fulltitle")
        or info.get("track")
    )
    if not title or not str(title).strip():
        stem = os.path.splitext(os.path.basename(path))[0]
        vid = info.get("id")
        if vid and stem.startswith(str(vid) + "_"):
            title = stem[len(str(vid)) + 1 :].strip()
        else:
            title = stem or "media"
    filename = _sanitize_filename(str(title).strip()) + ext
    if not filename or filename == ".mp4":
        filename = os.path.basename(path)

    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
    )


# Serve frontend
frontend_path = Path(__file__).resolve().parent.parent / "static"
if frontend_path.is_dir():
    app.mount("/", StaticFiles(directory=str(frontend_path), html=True), name="static")
