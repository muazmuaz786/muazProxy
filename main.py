from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.background import BackgroundTask
from pydantic import BaseModel
from pathlib import Path
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor
import asyncio
import httpx
import json as _json
import os
import re
import subprocess
import tempfile

app = FastAPI(title="YouTube Embedded Viewer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "AIzaSyCkdN2Ru90k5DBzG5n7JjM7e6049UMtob4")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
_executor = ThreadPoolExecutor(max_workers=4)

# Optional: allow passing cookies via env to bypass YouTube bot checks.
COOKIES_PATH = None
if os.getenv("YTDLP_COOKIES"):
    # Write env content (Netscape cookie format) to a temp file.
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix="yt_cookies_", suffix=".txt")
    tmp.write(os.getenv("YTDLP_COOKIES").encode("utf-8"))
    tmp.flush()
    tmp.close()
    COOKIES_PATH = tmp.name

# Helpers

def extract_video_id(url: str) -> Optional[str]:
    patterns = [
        r"(?:youtube\.com/watch\?(?:.*&)?v=)([A-Za-z0-9_-]{11})",
        r"(?:youtu\.be/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/embed/)([A-Za-z0-9_-]{11})",
        r"(?:youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1)
    return None


def _get_meta(video_id: str) -> dict:
    """Get title + thumbnail via yt-dlp without downloading the file."""
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--dump-json",
        "--no-warnings",
        "--geo-bypass",
        "--extractor-args",
        "youtube:player_client=android",
        "--skip-download",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    if COOKIES_PATH:
        cmd += ["--cookies", COOKIES_PATH]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail="yt-dlp error: " + (result.stderr[:400] or "unknown error"),
        )
    data = _json.loads(result.stdout.strip())
    return {"title": data.get("title", video_id), "thumbnail": data.get("thumbnail", "")}


def _resolve_stream(video_id: str) -> tuple[str, dict]:
    """Resolve direct media URL and required headers using yt-dlp JSON output."""
    # Try a preferred mp4 <=720p, then fall back to "best" if unavailable.
    format_candidates = [
        "best[ext=mp4][height<=720]/best[height<=720]/best",
        "best",
    ]
    last_err = ""
    for fmt in format_candidates:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f",
            fmt,
            "--dump-json",
            "--no-warnings",
            "--geo-bypass",
            "--extractor-args",
            "youtube:player_client=android",
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        if COOKIES_PATH:
            cmd += ["--cookies", COOKIES_PATH]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = _json.loads(result.stdout.strip())
            url = data.get("url")
            headers = data.get("http_headers", {})
            if url:
                return url, headers
        last_err = (result.stderr or "")[:400]

    raise HTTPException(
        status_code=502,
        detail="yt-dlp failed to fetch stream URL: " + (last_err or "unknown error"),
    )


# Schemas

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


# Routes

@app.get("/", response_class=FileResponse)
async def serve_index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found. Expected static/index.html.")
    return FileResponse(index_path)


@app.get("/api/thumbnail/{video_id}")
async def proxy_thumbnail(video_id: str):
    url = f"https://img.youtube.com/vi/{video_id}/mqdefault.jpg"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=5)
    if resp.status_code != 200:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return Response(content=resp.content, media_type="image/jpeg")


@app.get("/api/stream-info/{video_id}", response_model=StreamInfo)
async def stream_info(video_id: str):
    """Return title + thumbnail. Actual video is streamed via /api/stream/{video_id}."""
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(_executor, _get_meta, video_id)
    return StreamInfo(video_id=video_id, **meta)


@app.get("/api/stream/{video_id}")
async def stream_video(video_id: str, request: Request):
    """Proxy the upstream video with Range support so the HTML5 player can play."""
    media_url, upstream_headers = _resolve_stream(video_id)

    headers = {k: v for k, v in upstream_headers.items()}
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]

    client = httpx.AsyncClient(follow_redirects=True, timeout=None)
    upstream_cm = client.stream("GET", media_url, headers=headers)
    upstream = await upstream_cm.__aenter__()

    if upstream.status_code not in (200, 206):
        err_body = await upstream.aread()
        await upstream_cm.__aexit__(None, None, None)
        await client.aclose()
        raise HTTPException(status_code=upstream.status_code, detail=err_body.decode(errors="ignore")[:400])

    async def close_upstream():
        await upstream_cm.__aexit__(None, None, None)
        await client.aclose()

    resp = StreamingResponse(
        upstream.aiter_bytes(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("Content-Type", "video/mp4"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(close_upstream),
    )
    for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if h in upstream.headers:
            resp.headers[h] = upstream.headers[h]
    return resp


@app.get("/api/search", response_model=SearchResponse)
async def search_videos(
    q: str = Query(..., min_length=1),
    kind: str = Query("video", pattern="^(video|short)$"),
    page_token: Optional[str] = Query(None),
):
    if not YOUTUBE_API_KEY:
        raise HTTPException(status_code=500, detail="YOUTUBE_API_KEY is not configured.")

    search_q = f"{q} #Shorts" if kind == "short" else q
    params = {
        "part": "snippet",
        "q": search_q,
        "type": "video",
        "maxResults": 12,
        "videoDuration": "short" if kind == "short" else "any",
        "key": YOUTUBE_API_KEY,
    }
    if page_token:
        params["pageToken"] = page_token

    async with httpx.AsyncClient() as client:
        resp = await client.get(YOUTUBE_SEARCH_URL, params=params, timeout=8)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="YouTube API error.")

    body = resp.json()
    results = [
        SearchResult(
            video_id=item["id"]["videoId"],
            title=item["snippet"]["title"],
            channel=item["snippet"]["channelTitle"],
        )
        for item in body.get("items", [])
    ]
    return SearchResponse(results=results, next_page_token=body.get("nextPageToken"))


@app.post("/api/parse")
async def parse_video(body: VideoRequest):
    video_id = extract_video_id(body.url)
    if not video_id:
        raise HTTPException(status_code=422, detail="Please provide a valid YouTube URL or video ID.")
    return {"video_id": video_id}


@app.get("/api/health")
async def health():
    return {"status": "ok"}
