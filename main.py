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

YOUTUBE_API_KEY    = "AIzaSyCkdN2Ru90k5DBzG5n7JjM7e6049UMtob4"
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

COOKIES_PATH = _load_cookies()


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
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise HTTPException(500, "yt-dlp meta error: " + r.stderr[:300])
    data = _json.loads(r.stdout.strip())
    return {"title": data.get("title", video_id), "thumbnail": data.get("thumbnail", "")}


def _stream_video(video_id: str):
    """
    Stream directly from yt-dlp stdout — tries formats from most to least compatible.
    '18' = 360p mp4 (always a single file, no merge needed) — most reliable on servers.
    """
    format_order = [
        "18",                                          # 360p mp4 combined — most reliable
        "best[ext=mp4][height<=480]",
        "best[ext=mp4][height<=720]",
        "best[ext=mp4]",
        "best",
    ]

    for fmt in format_order:
        cmd = _base_cmd(video_id)
        cmd[-1:] = ["-f", fmt, "-o", "-", cmd[-1]]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # peek first 8KB — if we get data, format works
        first = proc.stdout.read(8192)
        if first and proc.poll() is None:
            def gen(proc=proc, first=first):
                yield first
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk: break
                    yield chunk
                proc.stdout.close(); proc.wait()
            return gen()
        proc.stdout.close(); proc.stderr.close(); proc.wait()

    raise HTTPException(500, "No playable format found for this video.")


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
async def stream_video(video_id: str):
    loop = asyncio.get_event_loop()
    gen  = await loop.run_in_executor(_executor, _stream_video, video_id)
    return StreamingResponse(gen, media_type="video/mp4",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

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

@app.get("/api/health")
async def health():
    return {"status": "ok", "cookies": bool(COOKIES_PATH)}

