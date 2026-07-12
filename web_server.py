from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional, Union, List
import logging
import uvicorn
import collections
import time

import os

from cinemeta import search_all, get_meta
import threading

from backend import (process_download_task, load_config, VERSION,
                     list_failures, remove_failure, retry_failure)
from monitor import (list_monitored, add_monitored, remove_monitored,
                     note_grabbed, check_all, monitor_loop)
from clients import Aria2Client

app = FastAPI(title="Zapflix Web")

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
def search(q: str):
    if not q: return []
    try:
        return search_all(q)
    except Exception as e:
        return [{"name": f"Error: {e}", "imdb_id": "error", "type": "error", "year": ""}]

@app.get("/api/meta/{content_type}/{imdb_id}")
def meta(content_type: str, imdb_id: str):
    try:
        data = get_meta(content_type, imdb_id)
        if not data:
            return JSONResponse({"error": "not found"}, status_code=404)
        return data
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

def _download_and_note(imdb_id: str, type: str, selection):
    """Run a download and record grabbed episodes in the monitor ledger, so
    manual downloads of a monitored show aren't re-grabbed by the checker."""
    result = process_download_task(imdb_id, type, distinct_logs, selection)
    if result and result.get("type") == "series":
        note_grabbed(imdb_id, result.get("grabbed", []))

@app.post("/api/download")
async def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    distinct_logs(f"Received download request for {req.imdb_id}")
    background_tasks.add_task(_download_and_note, req.imdb_id, req.type, req.selection)
    return {"message": f"Started download for {req.imdb_id}", "status": "queued"}

@app.get("/api/logs")
async def get_logs():
    return list(logs)

@app.get("/api/downloads")
def downloads():
    """Live aria2 download list for the UI's Downloads panel."""
    config = load_config()
    a_cfg = config.get("aria2", {})
    if not a_cfg.get("enabled"):
        return []
    client = Aria2Client(
        host=a_cfg.get("host", "http://localhost"),
        port=a_cfg.get("port", 6800),
        secret=a_cfg.get("secret", ""),
    )
    out = []
    for d in client.list_downloads():
        total = int(d.get("totalLength", 0) or 0)
        done = int(d.get("completedLength", 0) or 0)
        speed = int(d.get("downloadSpeed", 0) or 0)
        files = d.get("files") or []
        path = files[0].get("path", "") if files else ""
        out.append({
            "gid": d.get("gid"),
            "name": os.path.basename(path) or d.get("gid"),
            "status": d.get("status"),
            "total": total,
            "done": done,
            "progress": round(done * 100 / total, 1) if total else 0,
            "speed": speed,
            "eta": round((total - done) / speed) if speed > 0 and total > done else None,
            "error": d.get("errorMessage") or None,
        })
    # Active first, then queued, then finished/failed
    order = {"active": 0, "waiting": 1, "paused": 2, "error": 3, "complete": 4, "removed": 5}
    out.sort(key=lambda x: order.get(x["status"], 9))
    return out

@app.get("/api/failures")
def failures():
    """Failed file deliveries (persisted) — the UI's Failed files panel."""
    return sorted(list_failures(), key=lambda x: x.get("ts", 0), reverse=True)

@app.post("/api/failures/{fid}/retry")
def failure_retry(fid: str):
    ok, message = retry_failure(fid, distinct_logs)
    if not ok:
        distinct_logs(f"❌ Retry failed: {message}")
    return {"ok": ok, "message": message}

@app.delete("/api/failures/{fid}")
def failure_dismiss(fid: str):
    return {"ok": remove_failure(fid)}

@app.delete("/api/failures")
def failures_clear():
    remove_failure(None)
    return {"ok": True}

@app.get("/api/monitored")
def monitored():
    items = list_monitored()
    # known lists can be large (One Piece: 1000+) — send counts, not contents
    return [{k: v for k, v in it.items() if k != "known"} | {"known_count": len(it.get("known", []))}
            for it in items]

@app.post("/api/monitored/{imdb_id}")
def monitor_add(imdb_id: str):
    ok, message = add_monitored(imdb_id)
    distinct_logs(("🔔 " if ok else "❌ ") + message)
    return {"ok": ok, "message": message}

@app.delete("/api/monitored/{imdb_id}")
def monitor_remove(imdb_id: str):
    return {"ok": remove_monitored(imdb_id)}

@app.post("/api/monitored-check")
async def monitor_check(background_tasks: BackgroundTasks):
    background_tasks.add_task(check_all, distinct_logs)
    return {"ok": True, "message": "Check started"}

@app.on_event("startup")
async def start_monitor_thread():
    threading.Thread(target=monitor_loop, args=(distinct_logs,), daemon=True).start()

@app.get("/api/status")
def status():
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
