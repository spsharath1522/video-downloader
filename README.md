# Media Downloader

Paste any media link — YouTube, **Spotify**, **Apple Music**, Vimeo, Twitter, etc. — see formats, then download (video+audio or audio only).

## Supported links

- **YouTube**, Vimeo, Twitter/X, and 1000+ sites (via yt-dlp)
- **Adult sites** — Pornhub, XVideos, RedTube, and other sites with yt-dlp extractors (no age filtering; use at your own discretion)
- **Spotify** — tracks, albums, playlists (audio via spotdl)
- **Apple Music** — via yt-dlp where supported

## Requirements

- **Python 3.10+**
- **ffmpeg** (for merging video + audio, and for Spotify). Install:
  - Ubuntu/Debian: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: download from [ffmpeg.org](https://ffmpeg.org/download.html)
- **aria2** (optional, for faster downloads):
  - Ubuntu/Debian: `sudo apt install aria2`
  - macOS: `brew install aria2`
- **Deno or Node.js** (optional, for full YouTube support): if you see “No supported JavaScript runtime”, install one so all formats work. The app will use it if found in PATH.
  - Deno: `curl -fsSL https://deno.land/install.sh | sh`
  - Node: `sudo apt install nodejs` or [nodejs.org](https://nodejs.org)

### More speed

- The app already uses **64 parallel fragment downloads** (yt-dlp) for DASH/HLS, which gives most of the speed gain.
- The **Debian/Ubuntu aria2 package** limits `--max-connection-per-server` to **16**; the app uses 16. To use more connections per server (e.g. 32), build aria2 from source: [aria2/aria2](https://github.com/aria2/aria2) (then the app will still pass `-x 16`; you’d need to change that in code or use an aria2 config with a higher value if your build allows it).

## Setup

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

If you get **Cloudflare 403** on some links, the app uses yt-dlp impersonation (requires `curl_cffi`). Reinstall with: `pip install -r requirements.txt` so the `yt-dlp[curl-cffi]` extra is installed.

## Run

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 — paste a URL, fetch formats, pick one, and download.

## Usage

1. Paste a link (YouTube, Spotify, Apple Music, or any supported site).
2. Click **Fetch formats** to load available qualities.
3. Choose a format (video + audio, or audio only for music).
4. Click **Download** to save the file.
