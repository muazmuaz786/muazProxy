from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
import asyncio, httpx, json as _json, os, re, subprocess, tempfile, base64, logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE_DIR   = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

YOUTUBE_API_KEY    = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_executor = ThreadPoolExecutor(max_workers=4)


# ── Cookies ────────────────────────────────────────────────────
def _setup_cookies() -> Optional[str]:
    src = os.getenv("YTDLP_COOKIES_PATH")
    if src and Path(src).exists():
        try:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
            tmp.write(Path(src).read_bytes()); tmp.close()
            log.info("Cookies copied to %s", tmp.name)
            return tmp.name
        except Exception as e:
            log.warning("Cookie copy failed: %s", e)
    return None

COOKIES = _setup_cookies()


# ── Helpers ────────────────────────────────────────────────────
def extract_video_id(url: str) -> Optional[str]:
    for p in [
        r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]:
        m = re.search(p, url.strip())
        if m: return m.group(1)
    return None


def _ytdlp_base() -> list:
    cmd = ["yt-dlp", "--no-playlist", "--geo-bypass", "--force-ipv4", "--no-check-certificates", "--extractor-args", "youtube:player_client=web,default"]
    if COOKIES:
        cmd += ["--cookies", COOKIES]
    return cmd


def _get_meta(video_id: str) -> dict:
    cmd = _ytdlp_base() + ["--dump-json", "--skip-download", "--no-warnings",
                            f"https://www.youtube.com/watch?v={video_id}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise HTTPException(500, f"yt-dlp meta error: {r.stderr[:400]}")
    d = _json.loads(r.stdout.strip())
    return {"title": d.get("title", video_id), "thumbnail": d.get("thumbnail", "")}


def _download(video_id: str) -> Path:
    out = tempfile.mktemp(suffix=".mp4", prefix=f"yt_{video_id}_")
    cmd = _ytdlp_base() + [
        "-f", "18/best[ext=mp4][height<=480]/best[ext=mp4]/best",
        "--no-warnings",
        "--hls-prefer-native",
        "--no-part",
        "-o", out,
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    log.info("Downloading %s", video_id)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    log.info("yt-dlp returncode=%s stderr=%s", r.returncode, r.stderr[:200])
    
    p = Path(out)
    if not p.exists() or p.stat().st_size < 1000:
        # try without format restriction
        cmd2 = _ytdlp_base() + ["--no-warnings", "-o", out,
                                  f"https://www.youtube.com/watch?v={video_id}"]
        r2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=180)
        if not p.exists() or p.stat().st_size < 1000:
            raise HTTPException(500, f"Download failed: {r.stderr[:300] or r2.stderr[:300]}")
    
    log.info("Downloaded %s → %s bytes", video_id, p.stat().st_size)
    return p


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
@app.get("/")
async def serve_index():
    p = STATIC_DIR / "index.html"
    if not p.exists(): raise HTTPException(404, "index.html not found")
    return FileResponse(p)

@app.get("/api/thumbnail/{video_id}")
async def thumbnail(video_id: str):
    async with httpx.AsyncClient() as c:
        r = await c.get(f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg", timeout=5)
    if r.status_code != 200: raise HTTPException(404)
    return Response(r.content, media_type="image/jpeg")

@app.get("/api/stream-info/{video_id}", response_model=StreamInfo)
async def stream_info(video_id: str):
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(_executor, _get_meta, video_id)
    return StreamInfo(video_id=video_id, **meta)

@app.get("/api/stream/{video_id}")
async def stream(video_id: str):
    """Download full video then serve as FileResponse (handles Range automatically)."""
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(_executor, _download, video_id)
    
    # FileResponse handles Range requests natively — no manual chunking needed
    return FileResponse(
        path,
        media_type="video/mp4",
        filename=f"{video_id}.mp4",
        background=None,
    )

@app.get("/api/search", response_model=SearchResponse)
async def search(q: str = Query(...), kind: str = Query("video"), page_token: Optional[str] = None):
    if not YOUTUBE_API_KEY:
        raise HTTPException(500, "YOUTUBE_API_KEY not set")
    params = {
        "part": "snippet", "type": "video", "maxResults": 12,
        "q": f"{q} #Shorts" if kind == "short" else q,
        "videoDuration": "short" if kind == "short" else "any",
        "key": YOUTUBE_API_KEY,
    }
    if page_token: params["pageToken"] = page_token
    async with httpx.AsyncClient() as c:
        r = await c.get(YOUTUBE_SEARCH_URL, params=params, timeout=8)
    if r.status_code != 200: raise HTTPException(r.status_code, "YouTube API error")
    body = r.json()
    return SearchResponse(
        results=[SearchResult(video_id=i["id"]["videoId"],
                              title=i["snippet"]["title"],
                              channel=i["snippet"]["channelTitle"])
                 for i in body.get("items", [])],
        next_page_token=body.get("nextPageToken"),
    )

@app.post("/api/parse")
async def parse(body: VideoRequest):
    vid = extract_video_id(body.url)
    if not vid: raise HTTPException(422, "Invalid YouTube URL or ID")
    return {"video_id": vid}

@app.get("/api/health")
async def health():
    return {"status": "ok", "cookies": bool(COOKIES), "api_key": bool(YOUTUBE_API_KEY)}
