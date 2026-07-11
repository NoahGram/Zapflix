from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Union, List
import logging
import uvicorn
import collections
import time

from cinemeta import search_all, get_meta
from backend import process_download_task, load_config, VERSION

app = FastAPI(title="TorrentDownloader Web")

# Simple in-memory log storage for frontend polling
logs = collections.deque(maxlen=200)

def distinct_logs(msg: str):
    """Avoid spamming logs with duplicates."""
    try:
        timestamp = time.strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] {msg}"
        if logs and logs[-1] == full_msg:
            return
        logs.append(full_msg)
    except Exception as e:
        print(f"Log error: {e}")

templates = Jinja2Templates(directory="templates")

class DownloadRequest(BaseModel):
    imdb_id: str
    type: str
    # None/"all" => everything; or [{"season": int, "episodes": [int,...]}, ...]
    selection: Optional[Union[str, List[dict]]] = None

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    # Modern Starlette signature: request first, then template name.
    return templates.TemplateResponse(request, "index.html")

@app.get("/api/search")
async def search(q: str):
    if not q: return []
    try:
        return search_all(q)
    except Exception as e:
        return [{"name": f"Error: {e}", "imdb_id": "error", "type": "error", "year": ""}]

@app.get("/api/meta/{content_type}/{imdb_id}")
async def meta(content_type: str, imdb_id: str):
    try:
        data = get_meta(content_type, imdb_id)
        if not data:
            return JSONResponse({"error": "not found"}, status_code=404)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    distinct_logs(f"Received download request for {req.imdb_id}")
    background_tasks.add_task(
        process_download_task, req.imdb_id, req.type, distinct_logs, req.selection
    )
    return {"message": f"Started download for {req.imdb_id}", "status": "queued"}

@app.get("/api/logs")
async def get_logs():
    return list(logs)

@app.get("/api/status")
async def status():
    config = load_config()
    return {
        "status": "online",
        "version": VERSION,
        "config_found": bool(config),
        "clients": {
            "rd": bool(config.get("real_debrid", {}).get("enabled") and config.get("real_debrid", {}).get("api_key")),
            "aria2": bool(config.get("aria2", {}).get("enabled")),
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
