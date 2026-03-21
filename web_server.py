from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import logging
import uvicorn
import collections
import os
import time

from cinemeta import search_all
from backend import process_download_task, load_config

app = FastAPI(title="TorrentDownloader Web")

# Simple in-memory log storage for frontend polling
logs = collections.deque(maxlen=100)

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

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/search")
async def search(q: str):
    if not q: return []
    try:
        results = search_all(q)
        return results
    except Exception as e:
        return [{"name": f"Error: {e}", "imdb_id": "error", "type": "error", "year": ""}]

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    distinct_logs(f"Received download request for {req.imdb_id}")
    background_tasks.add_task(process_download_task, req.imdb_id, req.type, distinct_logs)
    return {"message": f"Started download for {req.imdb_id}", "status": "queued"}

@app.get("/api/logs")
async def get_logs():
    return list(logs)

@app.get("/api/status")
async def status():
    config = load_config()
    return {
        "status": "online",
        "config_found": bool(config),
        "clients": {
            "rd": bool(config.get("real_debrid", {}).get("enabled")),
            "qb": bool(config.get("qbittorrent", {}).get("host")),
            "aria2": bool(config.get("aria2", {}).get("enabled"))
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
