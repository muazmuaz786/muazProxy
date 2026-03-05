from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
import asyncio, httpx, json as _json, os, re, subprocess, tempfile, base64, logging

app = FastAPI(title="YouTube Embedded Viewer")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

YOUTUBE_API_KEY    = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_executor = ThreadPoolExecutor(max_workers=4)


# ── Cookies ────────────────────────────────────────────────────
def _load_cookies() -> Optional[str]:
    path = os.getenv("YTDLP_COOKIES_PATH")
    if path and Path(path).exists():
        return path
    cookie_text = os.getenv("YTDLP_COOKIES")
    if not cookie_text:
        b64 = os.getenv("YTDLP_COOKIES_B64")
        if b64:
            try: cookie_text = base64.b64decode(b64).decode("utf-8")
            except: return None
    if not cookie_text:
        return None
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="yt_cookies_", suffix=".txt")
    tmp.write(cookie_text.encode()); tmp.flush(); tmp.close()
    return tmp.name


# Copy cookies to writable temp location so yt-dlp can update them
def _ensure_writable_cookies(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix='yt_cookies_rw_', suffix='.txt')
        tmp.write(Path(path).read_bytes())
        tmp.flush(); tmp.close()
        logging.info('Copied cookies to writable path: %s', tmp.name)
        return tmp.name
    except Exception as e:
        logging.warning('Could not copy cookies: %s', e)
        return path

COOKIES_PATH = _ensure_writable_cookies(_load_cookies())



# ── Helpers ────────────────────────────────────────────────────
def extract_video_id(url: str) -> Optional[str]:
    for p in [
        r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]:
        m = re.search(p, url.strip())
        if m: return m.group(1)
    return None


def _base_cmd(video_id: str) -> list:
    """Base yt-dlp command with all flags that help on server environments."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--geo-bypass",
        "--force-ipv4",
        # web client works better with cookies than android
        "--extractor-args", "youtube:player_client=web",
    ]
    if COOKIES_PATH:
        cmd += ["--cookies", COOKIES_PATH]
    cmd.append(f"https://www.youtube.com/watch?v={video_id}")
    return cmd


def _get_meta(video_id: str) -> dict:
    cmd = _base_cmd(video_id)
    # insert before URL
    cmd[-1:] = ["--dump-json", "--skip-download", cmd[-1]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise HTTPException(500, "yt-dlp meta error: " + (r.stderr or r.stdout)[:800])
    data = _json.loads(r.stdout.strip())
    return {"title": data.get("title", video_id), "thumbnail": data.get("thumbnail", "")}


def _download_video(video_id: str) -> Path:
    """Download video to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.mp4', prefix=f'yt_{video_id}_')
    tmp.close()
    out_path = tmp.name

    cmd = _base_cmd(video_id)
    cmd[-1:] = ["-f", "18/best[ext=mp4]/best", "-o", out_path, cmd[-1]]

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or not Path(out_path).exists():
        raise HTTPException(500, "Download failed: " + r.stderr[:300])
    return Path(out_path)


# ── Schemas ────────────────────────────────────────────────────
class VideoRequest(BaseModel):
    url: str

class StreamInfo(BaseModel):
    video_id: str
    title: str
    thumbnail: str

class SearchResult(BaseModel):
    video_id: str
    title: str
    channel: str

class SearchResponse(BaseModel):
    results: List[SearchResult]
    next_page_token: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────
@app.get("/", response_class=FileResponse)
async def serve_index():
    p = STATIC_DIR / "index.html"
    if not p.exists(): raise HTTPException(404, "Frontend not found.")
    return FileResponse(p)

@app.get("/api/thumbnail/{video_id}")
async def proxy_thumbnail(video_id: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg", timeout=5)
    if r.status_code != 200: raise HTTPException(404, "Thumbnail not found")
    return Response(content=r.content, media_type="image/jpeg")

@app.get("/api/stream-info/{video_id}", response_model=StreamInfo)
async def stream_info(video_id: str):
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(_executor, _get_meta, video_id)
    return StreamInfo(video_id=video_id, **meta)

@app.get("/api/stream/{video_id}")
async def stream_video(video_id: str, request: Request):
    """Download to temp file first, then serve with proper Range support."""
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_executor, _download_video, video_id)

    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    def cleanup():
        try: path.unlink()
        except: pass

    if range_header:
        # Parse range
        start, end = range_header.replace("bytes=", "").split("-")
        start = int(start)
        end = int(end) if end else file_size - 1
        chunk_size = end - start + 1

        def ranged_gen():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = chunk_size
                while remaining > 0:
                    data = f.read(min(65536, remaining))
                    if not data: break
                    remaining -= len(data)
                    yield data
            cleanup()

        return StreamingResponse(
            ranged_gen(),
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(chunk_size),
            }
        )
    else:
        def full_gen():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk: break
                    yield chunk
            cleanup()

        return StreamingResponse(
            full_gen(),
            media_type="video/mp4",
            headers={
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-cache",
            }
        )

@app.get("/api/search", response_model=SearchResponse)
async def search_videos(
    q: str = Query(..., min_length=1),
    kind: str = Query("video", pattern="^(video|short)$"),
    page_token: Optional[str] = Query(None),
):
    if not YOUTUBE_API_KEY:
        raise HTTPException(500, "YOUTUBE_API_KEY not set.")
    params = {
        "part": "snippet", "q": f"{q} #Shorts" if kind == "short" else q,
        "type": "video", "maxResults": 12,
        "videoDuration": "short" if kind == "short" else "any",
        "key": YOUTUBE_API_KEY,
    }
    if page_token: params["pageToken"] = page_token
    async with httpx.AsyncClient() as c:
        r = await c.get(YOUTUBE_SEARCH_URL, params=params, timeout=8)
    if r.status_code != 200: raise HTTPException(r.status_code, "YouTube API error.")
    body = r.json()
    return SearchResponse(
        results=[SearchResult(video_id=i["id"]["videoId"],
                              title=i["snippet"]["title"],
                              channel=i["snippet"]["channelTitle"])
                 for i in body.get("items", [])],
        next_page_token=body.get("nextPageToken"),
    )

@app.post("/api/parse")
async def parse_video(body: VideoRequest):
    vid = extract_video_id(body.url)
    if not vid: raise HTTPException(422, "Invalid YouTube URL or ID.")
    return {"video_id": vid}


@app.get("/api/debug/{video_id}")
async def debug_ytdlp(video_id: str):
    """Temporary debug endpoint — remove after fixing."""
    import shutil
    cmd = _base_cmd(video_id)
    cmd[-1:] = ["--dump-json", "--skip-download", cmd[-1]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return {
        "returncode": r.returncode,
        "stdout_snippet": r.stdout[:500],
        "stderr_full": r.stderr,
        "yt_dlp_path": shutil.which("yt-dlp"),
        "cookies_path": COOKIES_PATH,
        "cookies_exists": Path(COOKIES_PATH).exists() if COOKIES_PATH else False,
        "cmd": cmd,
    }


@app.get("/api/formats/{video_id}")
async def list_formats(video_id: str):
    cmd = _base_cmd(video_id)
    cmd[-1:] = ["--list-formats", cmd[-1]]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return Response(content=r.stdout + r.stderr, media_type="text/plain")

@app.get("/api/health")
async def health():
    return {"status": "ok", "cookies": bool(COOKIES_PATH)}
