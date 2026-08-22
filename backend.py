import json
import logging
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Optional, List, Callable, Union

import requests
from dotenv import load_dotenv

from cinemeta import search_all, get_series_metadata, get_movie_metadata, Episode, Series, Movie
from torrentio import TorrentioClient, TorrentStream
from clients import RealDebridClient, Aria2Client, AddResult
from profiles import QualityProfile
from naming import (ParsedFile, parse_media_filename, canonical_name,
                    season_folder)

# Setup logging
logger = logging.getLogger("Zapflix")
logger.setLevel(logging.INFO)

# Bump on every release — shown in the UI header, /api/status, and task logs
# so a stale Docker image is immediately obvious.
VERSION = "1.11.0"

CONFIG_FILE = Path(__file__).parent / "config.json"

# Failed deliveries live in data/ so a Docker rebuild doesn't wipe them
# (docker-compose mounts ./data). Each entry keeps the RD link — direct
# download URLs expire, but re-unrestricting the RD link mints a fresh one.
DATA_DIR = Path(__file__).parent / "data"
FAILED_FILE = DATA_DIR / "failed.json"
_failed_lock = threading.Lock()

# Delivery journal: RD torrents we added (until every file is sent to aria2)
# and every link already sent. Survives reboots/rebuilds so a container that
# dies mid-delivery can pick up where it left off instead of losing files.
DELIVERIES_FILE = DATA_DIR / "deliveries.json"
_deliveries_lock = threading.Lock()
RECONCILE_WINDOW_DAYS = 14

# Load .env once at import so env vars are available to load_config().
load_dotenv(Path(__file__).parent / ".env")

# Browser-like session for following Torrentio /resolve/ redirects (Cloudflare
# 403s non-browser User-Agents on debrid requests).
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_resolve_session = requests.Session()
_resolve_session.headers.update({"User-Agent": _BROWSER_UA})


def _env_overlay(config: dict) -> dict:
    """Overlay secret/env values on top of config.json.

    Environment variables win over config.json so secrets can live in .env
    (or the container environment) instead of a committed file.
    """
    rd = config.setdefault("real_debrid", {})
    if os.getenv("RD_API_KEY"):
        rd["api_key"] = os.getenv("RD_API_KEY")
        rd["enabled"] = True

    t = config.setdefault("torrentio", {})
    if os.getenv("TORRENTIO_CONFIG"):
        t["config"] = os.getenv("TORRENTIO_CONFIG")

    aria2 = config.setdefault("aria2", {})
    if os.getenv("ARIA2_HOST"):
        aria2["host"] = os.getenv("ARIA2_HOST")
        aria2["enabled"] = True
    if os.getenv("ARIA2_PORT"):
        aria2["port"] = int(os.getenv("ARIA2_PORT"))
    if os.getenv("ARIA2_SECRET"):
        aria2["secret"] = os.getenv("ARIA2_SECRET")
    if os.getenv("ARIA2_DOWNLOAD_DIR"):
        aria2["download_dir"] = os.getenv("ARIA2_DOWNLOAD_DIR")

    jf = config.setdefault("jellyfin", {})
    if os.getenv("JELLYFIN_HOST"):
        jf["host"] = os.getenv("JELLYFIN_HOST")
    if os.getenv("JELLYFIN_API_KEY"):
        jf["api_key"] = os.getenv("JELLYFIN_API_KEY")

    return config


def load_config() -> dict:
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            config = json.load(f)
    return _env_overlay(config)


def get_clients(config: dict):
    """Initialize clients based on config. Returns (rd_client, aria2_client).

    Real-Debrid is required; aria2 is optional (without it, files stay in the
    RD cloud instead of being pushed to the NAS).
    """
    rd_config = config.get("real_debrid", {})
    aria2_config = config.get("aria2", {})

    rd_client = None
    if rd_config.get("enabled") and rd_config.get("api_key"):
        try:
            rd_client = RealDebridClient(api_key=rd_config["api_key"])
            if not rd_client.test_connection():
                logger.warning("Real-Debrid connection failed.")
                rd_client = None
        except Exception:
            rd_client = None

    aria2_client = None
    if aria2_config.get("enabled"):
        try:
            aria2_client = Aria2Client(
                host=aria2_config.get("host", "http://localhost"),
                port=aria2_config.get("port", 6800),
                secret=aria2_config.get("secret", ""),
                download_dir=aria2_config.get("download_dir", ""),
            )
        except Exception:
            aria2_client = None

    return rd_client, aria2_client


def resolve_direct_link(resolve_url: str, timeout: int = 60) -> Optional[str]:
    """Follow a Torrentio /resolve/ redirect to a direct download link.

    For a cached ([RD+]) stream this returns instantly with the Real-Debrid
    direct URL (the 302 Location) without downloading anything. Returns None
    if the stream is not ready / not cached.
    """
    try:
        r = _resolve_session.get(resolve_url, allow_redirects=False, timeout=timeout)
        if r.status_code in (301, 302, 303, 307, 308):
            loc = r.headers.get("Location", "")
            # A real direct link points off Torrentio (to the debrid host).
            if loc and "torrentio" not in loc:
                return loc
        # Some resolvers 200 straight to the file
        if r.status_code == 200 and "real-debrid" in r.url:
            return r.url
    except Exception as e:
        print(f"  ✗ resolve error: {e}")
    return None


def _pack_seasons(stream: TorrentStream, season: int) -> set[int]:
    """Which whole seasons does this stream's torrent cover? Empty = single episode.

    Only the torrent name (first line of the Torrentio title) is inspected —
    the filename always names a single episode file even inside packs, and
    episode *titles* can be numeric (e.g. "S04E02 - 117") which fooled looser
    heuristics.
    """
    name = stream.title.split("\n")[0].lower()
    seasons: set[int] = set()

    # Multi-season ranges: "s01-s06", "season 1-6", "seasons 1 to 6"
    for m in re.finditer(r"s(\d{1,2})\s*[-–~]\s*s(\d{1,2})", name):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 60:
            seasons.update(range(a, b + 1))
    for m in re.finditer(r"seasons?\s*(\d{1,2})\s*(?:[-–~]|to)\s*(\d{1,2})", name):
        a, b = int(m.group(1)), int(m.group(2))
        if 0 < a <= b <= 60:
            seasons.update(range(a, b + 1))

    # Additive lists: "(Season 01 + Season 02 + OVA)" / "S01+S02"
    if not re.search(r"s0*\d+\s*e\d+", name):
        mentions = {int(m.group(1)) for m in
                    re.finditer(r"(?:seasons?\s*|s)0*(\d{1,2})\b", name)}
        mentions = {s for s in mentions if 0 < s <= 60}
        if len(mentions) >= 2:
            seasons |= mentions

    # Episode-range pack within this season: "S02E01-E12" (require the E on
    # both sides — "S04E02 - 117" must NOT match)
    if re.search(rf"s0*{season}\s*e\d+\s*[-–~]\s*e\d+", name):
        seasons.add(season)
    # Whole-season pack: "Season 2" / "S02" with no single-episode marker
    elif re.search(rf"(?:seasons?\s*0*{season}|s0*{season})\b", name) and \
            not re.search(rf"s0*{season}\s*e\d+", name):
        seasons.add(season)

    # "complete"/"full season" with no parsable numbers: cover at least this
    # season (later seasons re-match the same hash and get covered then)
    if not seasons and ("complete" in name or "full season" in name):
        seasons.add(season)

    return seasons


def _filter_episodes(series: Series, selection) -> List[Episode]:
    """Return the episodes to download based on an optional selection.

    selection: None or "all" -> everything.
               [{"season": int, "episodes": [int, ...] | []/None}, ...]
               (empty/absent episodes list = whole season)
    """
    if not selection or selection == "all":
        return [ep for s in sorted(series.seasons) for ep in series.seasons[s]]

    wanted: dict[int, Optional[set]] = {}
    for item in selection:
        try:
            s = int(item["season"])
        except (KeyError, TypeError, ValueError):
            continue
        eps = item.get("episodes")
        wanted[s] = set(int(e) for e in eps) if eps else None

    result: List[Episode] = []
    for s in sorted(series.seasons):
        if s not in wanted:
            continue
        for ep in series.seasons[s]:
            if wanted[s] is None or ep.episode in wanted[s]:
                result.append(ep)
    return result


def _target_subdir(base_dir: str, content_name: str, start_type: str, fname: str,
                   parsed: Optional[ParsedFile] = None) -> str:
    """Build the Jellyfin-friendly NAS subdirectory for a file.

    The season comes from the filename parser, not a loose regex — the old
    `[Ss](\\d{1,2})` matched the "S11" inside the TV station "BS11" and filed
    an episode under "Season 11".
    """
    if start_type == "movie":
        return os.path.join(base_dir, content_name)
    if parsed is None:
        parsed = parse_media_filename(fname)
    return os.path.join(base_dir, content_name, season_folder(parsed))


# ─── Failure auto-retry & delivery history ─────────────────────────────────────

def auto_retry_failures(log_callback: Callable[[str], None] = lambda m: None,
                        max_attempts: int = 5, min_age: int = 1800):
    """Retry pending failures automatically (called each monitoring pass).

    Only entries that failed ≥ min_age seconds ago and have fewer than
    max_attempts auto-retries are touched — transient errors self-heal,
    persistent ones stop burning API calls and wait for the manual button.
    """
    now = time.time()
    pending = [f for f in list_failures()
               if f.get("attempts", 0) < max_attempts and now - f.get("ts", 0) >= min_age]
    if not pending:
        return
    log_callback(f"🔁 Auto-retrying {len(pending)} failed file(s)...")
    recovered = 0
    for f in pending:
        ok, _ = retry_failure(f["id"], log_callback)
        if ok:
            recovered += 1
        time.sleep(1)
    log_callback(f"🔁 Auto-retry done — {recovered}/{len(pending)} recovered.")


def get_history(limit: int = 200) -> list[dict]:
    """Chronological list of everything delivered, from the journal."""
    with _deliveries_lock:
        sent = _read_deliveries().get("sent", {})
    items = [{"ts": r.get("ts", 0), "fname": r.get("fname", ""),
              "content_name": r.get("content_name", ""), "type": r.get("type", ""),
              "folder": r.get("folder")}
             for r in sent.values() if r.get("gid")]
    return sorted(items, key=lambda x: -x["ts"])[:limit]


# ─── Jellyfin library scan ─────────────────────────────────────────────────────

_last_jellyfin_scan = 0.0


def trigger_jellyfin_scan(config: dict, log_callback: Callable[[str], None] = lambda m: None):
    """Ask Jellyfin to rescan its libraries so new files appear immediately.

    No-op unless jellyfin.host + api_key are configured (JELLYFIN_HOST /
    JELLYFIN_API_KEY in .env). Debounced to once a minute — deliveries come
    in bursts and one scan covers them all.
    """
    global _last_jellyfin_scan
    jf = config.get("jellyfin", {})
    host = (jf.get("host") or "").rstrip("/")
    key = jf.get("api_key") or ""
    if not host or not key:
        return
    if time.time() - _last_jellyfin_scan < 60:
        return
    try:
        r = requests.post(f"{host}/Library/Refresh",
                          headers={"X-Emby-Token": key}, timeout=10)
        if r.status_code in (200, 204):
            _last_jellyfin_scan = time.time()
            log_callback("🪼 Jellyfin library scan triggered")
        else:
            log_callback(f"⚠️ Jellyfin scan failed: HTTP {r.status_code}")
    except Exception as e:
        log_callback(f"⚠️ Jellyfin scan failed: {e}")


# ─── Library index (what Zapflix has delivered) ────────────────────────────────

def build_library_index() -> dict:
    """What Zapflix has delivered, from the journal's sent records.

    Returns {"movies": [names...], "series": {name: ["s:e", ...]}} with
    lowercase keys for tolerant matching in the UI. Covers everything
    journaled (cached fast-path files included as of v1.9).
    """
    with _deliveries_lock:
        sent = _read_deliveries().get("sent", {})
    movies: set[str] = set()
    series: dict[str, set[str]] = {}
    for rec in sent.values():
        if not rec.get("gid"):  # skipped extras are journaled with gid=""
            continue
        name = (rec.get("content_name") or "").lower().strip()
        if not name:
            continue
        if rec.get("type") == "movie":
            movies.add(name)
        else:
            m = re.search(r"[Ss](\d{1,2})\s*[Ee](\d{1,3})", rec.get("fname", ""))
            if not m:
                m = re.search(r"\b(\d{1,2})x(\d{2,3})\b", rec.get("fname", ""))
            if m:
                series.setdefault(name, set()).add(f"{int(m.group(1))}:{int(m.group(2))}")
    return {"movies": sorted(movies),
            "series": {k: sorted(v, key=lambda s: tuple(map(int, s.split(":"))))
                       for k, v in series.items()}}


# ─── UI config editor ──────────────────────────────────────────────────────────

# Only these config.json fields are editable from the web UI. Secrets stay in
# .env and are never read or written here.
EDITABLE_CONFIG: dict[str, dict[str, type]] = {
    "torrentio": {"preferred_quality": list, "exclude_keywords": list},
    "aria2": {"library_folders": list, "default_movie_folder": str,
              "default_series_folder": str, "poll_timeout": float},
    "download": {"delay_between_episodes": float},
    "monitoring": {"check_interval_hours": float},
    "jellyfin": {"host": str},  # API key stays in .env (JELLYFIN_API_KEY)
}


def get_editable_config() -> dict:
    cfg = load_config()
    return {sec: {k: cfg.get(sec, {}).get(k) for k in keys}
            for sec, keys in EDITABLE_CONFIG.items()}


def update_editable_config(data: dict) -> tuple[bool, str]:
    """Merge whitelisted fields into config.json and write it back.

    Written IN PLACE (truncate + write), not tmp+rename: config.json is
    bind-mounted as a single file in Docker, and replacing it would detach
    the file from the host copy.
    """
    raw = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            raw = json.load(f)

    changed = []
    for sec, keys in EDITABLE_CONFIG.items():
        incoming = data.get(sec)
        if not isinstance(incoming, dict):
            continue
        for key, want_type in keys.items():
            if key not in incoming:
                continue
            val = incoming[key]
            if want_type is list:
                if not isinstance(val, list):
                    return False, f"{sec}.{key} must be a list"
                val = [str(v).strip() for v in val if str(v).strip()]
            elif want_type is float:
                try:
                    val = float(val)
                except (TypeError, ValueError):
                    return False, f"{sec}.{key} must be a number"
                if val <= 0:
                    return False, f"{sec}.{key} must be positive"
                if val == int(val):
                    val = int(val)
            else:
                val = str(val).strip()
            raw.setdefault(sec, {})[key] = val
            changed.append(f"{sec}.{key}")

    if not changed:
        return False, "Nothing to update"
    with open(CONFIG_FILE, "w") as f:
        json.dump(raw, f, indent=4)
    return True, f"Saved: {', '.join(changed)}"


# ─── Running-task registry (cancellation) ──────────────────────────────────────

class TaskCancelled(Exception):
    """Raised inside a download task when the user hits Cancel."""


_tasks_lock = threading.Lock()
ACTIVE_TASKS: dict[str, dict] = {}


def register_task(name: str, task_type: str) -> tuple[str, threading.Event]:
    """Register a running task; returns (task_id, cancel_event)."""
    tid = uuid.uuid4().hex[:8]
    ev = threading.Event()
    with _tasks_lock:
        ACTIVE_TASKS[tid] = {"id": tid, "name": name, "type": task_type,
                             "started": int(time.time()), "cancel": ev}
    return tid, ev


def update_task_name(tid: Optional[str], name: str):
    if not tid:
        return
    with _tasks_lock:
        if tid in ACTIVE_TASKS:
            ACTIVE_TASKS[tid]["name"] = name


def finish_task(tid: str):
    with _tasks_lock:
        ACTIVE_TASKS.pop(tid, None)


def cancel_task(tid: str) -> bool:
    with _tasks_lock:
        task = ACTIVE_TASKS.get(tid)
        if task:
            task["cancel"].set()
            return True
    return False


def list_tasks() -> list[dict]:
    with _tasks_lock:
        return [{k: v for k, v in t.items() if k != "cancel"}
                | {"cancelling": t["cancel"].is_set()}
                for t in ACTIVE_TASKS.values()]


def _check_cancel(cancel: Optional[threading.Event]):
    """Cooperative cancellation point — threads can't be killed, so the
    download/sync loops call this between steps."""
    if cancel is not None and cancel.is_set():
        raise TaskCancelled()


# ─── Library folder routing ────────────────────────────────────────────────────

def _content_root(config: dict, folder: Optional[str], start_type: str) -> str:
    """Base directory for a download: aria2 download_dir + library subfolder.

    folder=None -> the configured default for the content type ('' = root,
    which is the pre-1.7 behavior). The folder is as *aria2* sees it, same
    as download_dir itself.
    """
    a_cfg = config.get("aria2", {})
    base = a_cfg.get("download_dir", "")
    if folder is None:
        key = "default_movie_folder" if start_type == "movie" else "default_series_folder"
        folder = a_cfg.get(key, "")
    return os.path.join(base, folder) if folder else base


# ─── Delivery journal ──────────────────────────────────────────────────────────

def _read_deliveries() -> dict:
    try:
        with open(DELIVERIES_FILE) as f:
            return json.load(f)
    except Exception:
        return {"torrents": {}, "sent": {}}


def _write_deliveries(data: dict):
    DELIVERIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DELIVERIES_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    tmp.replace(DELIVERIES_FILE)


def record_torrent(tid: str, content_name: str, start_type: str, folder: Optional[str],
                   season_counts: Optional[dict] = None,
                   pack_seasons: Optional[set] = None):
    """Journal an RD torrent we added, until all its files reach aria2.

    The naming context is stored alongside it so the reconciler can name files
    correctly after a restart without re-fetching metadata.
    """
    if not tid:
        return
    with _deliveries_lock:
        d = _read_deliveries()
        d["torrents"].setdefault(tid, {
            "content_name": content_name, "type": start_type,
            "folder": folder, "ts": int(time.time()), "done": False,
            "season_counts": {str(k): v for k, v in (season_counts or {}).items()},
            "pack_seasons": sorted(pack_seasons or []),
        })
        _write_deliveries(d)


def mark_torrent_done(tid: str):
    with _deliveries_lock:
        d = _read_deliveries()
        if tid in d["torrents"]:
            d["torrents"][tid]["done"] = True
            _write_deliveries(d)


def _torrent_context(tid: str) -> tuple[dict, set]:
    """Journaled naming context for a torrent: (season_counts, pack_seasons)."""
    with _deliveries_lock:
        t = _read_deliveries().get("torrents", {}).get(tid) or {}
    return ({int(k): v for k, v in (t.get("season_counts") or {}).items()},
            set(t.get("pack_seasons") or []))


def link_sent(link: str) -> bool:
    with _deliveries_lock:
        return link in _read_deliveries()["sent"]


def record_sent(link: str, gid: str, fname: str, content_name: str,
                start_type: str, folder: Optional[str]):
    with _deliveries_lock:
        d = _read_deliveries()
        d["sent"][link] = {"gid": gid, "fname": fname, "ts": int(time.time()),
                           "content_name": content_name, "type": start_type,
                           "folder": folder, "swept": False}
        _write_deliveries(d)


# ─── Failed-delivery store ─────────────────────────────────────────────────────

def _read_failures() -> list[dict]:
    try:
        with open(FAILED_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _write_failures(items: list[dict]):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = FAILED_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(items, f, indent=1)
    tmp.replace(FAILED_FILE)


def list_failures() -> list[dict]:
    with _failed_lock:
        return _read_failures()


def record_failure(content_name: str, start_type: str, rd_link: str,
                   filename: str, reason: str, folder: Optional[str] = None):
    """Persist a failed file delivery so the UI can offer a retry."""
    with _failed_lock:
        items = _read_failures()
        for it in items:
            if it.get("rd_link") == rd_link:  # same file failing again
                it.update(ts=int(time.time()), reason=reason,
                          attempts=it.get("attempts", 0) + 1)
                break
        else:
            items.append({
                "id": uuid.uuid4().hex[:12],
                "ts": int(time.time()),
                "content_name": content_name,
                "type": start_type,
                "rd_link": rd_link,
                "filename": filename,
                "reason": reason,
                "folder": folder,
            })
        _write_failures(items)


def remove_failure(fid: Optional[str]) -> bool:
    """Remove one entry by id, or all entries when fid is None."""
    with _failed_lock:
        items = _read_failures()
        kept = [] if fid is None else [it for it in items if it.get("id") != fid]
        _write_failures(kept)
        return len(kept) < len(items)


def retry_failure(fid: str, log_callback: Callable[[str], None] = lambda m: None) -> tuple[bool, str]:
    """Re-unrestrict a failed file's RD link and hand it to aria2 again."""
    entry = next((it for it in list_failures() if it.get("id") == fid), None)
    if not entry:
        return False, "Unknown failure id"

    config = load_config()
    rd_client, aria2_client = get_clients(config)
    if not rd_client or not aria2_client:
        return False, "Real-Debrid or aria2 unavailable — check config"

    item, error = rd_client.unrestrict_link(entry["rd_link"])
    if not item or not item.get("download"):
        record_failure(entry["content_name"], entry["type"], entry["rd_link"],
                       entry.get("filename", "?"), error, entry.get("folder"))
        return False, f"Still failing — {error}"

    fname = item.get("filename") or entry.get("filename") or "unknown"

    parsed = parse_media_filename(fname) if entry["type"] == "series" else None
    if parsed is not None and not parsed.is_episode:
        remove_failure(fid)
        return True, f"Skipped ({parsed.note}): {fname}"

    base_dir = _content_root(config, entry.get("folder"), entry["type"])
    out_name = canonical_name(entry["content_name"], parsed, fname) if parsed else fname
    subdir = _target_subdir(base_dir, entry["content_name"], entry["type"], fname, parsed)
    gid = aria2_client.add_download(item["download"], directory=subdir, filename=out_name)
    if gid:
        remove_failure(fid)
        record_sent(entry["rd_link"], gid, out_name, entry["content_name"],
                    entry["type"], entry.get("folder"))
        log_callback(f"🔁 Retry OK — sent to aria2: {out_name}")
        trigger_jellyfin_scan(config, log_callback)
        return True, f"Sent to aria2: {out_name}"

    record_failure(entry["content_name"], entry["type"], entry["rd_link"],
                   out_name, "aria2 add failed (retried)", entry.get("folder"))
    return False, "aria2 rejected the download again"


def _deliver_cached(stream, aria2_client, base_dir, content_name, start_type,
                    log_callback, folder=None, episode: Optional[Episode] = None) -> bool:
    """Fast path for cached streams: resolve the direct link and push to aria2.

    Returns True on success. No RD polling, no unrestrict — instant & reliable.
    Journaled via record_sent (keyed by the stable resolve URL) so the library
    index sees it and the aria2 error sweep covers it.
    """
    direct = resolve_direct_link(stream.resolve_url)
    if not direct:
        return False

    fname = stream.filename or content_name
    out_name, subdir = fname, _target_subdir(base_dir, content_name, start_type, fname)

    if start_type == "series":
        # We asked Torrentio for one specific episode, so this file's identity
        # is known — no parsing guesswork, whatever the release group called it.
        parsed = (ParsedFile("episode", episode.season, episode.episode, how="requested")
                  if episode is not None else parse_media_filename(fname))
        out_name = canonical_name(content_name, parsed, fname)
        subdir = _target_subdir(base_dir, content_name, start_type, fname, parsed)

    gid = aria2_client.add_download(direct, directory=subdir, filename=out_name)
    if gid:
        record_sent(stream.resolve_url, gid, out_name, content_name, start_type, folder)
        log_callback(f"🚀 Sent cached file to aria2: {out_name}")
        return True
    return False


def process_download_task(
    imdb_id: str,
    type: str,
    log_callback: Callable[[str], None] = lambda msg: None,
    selection: Union[str, list, None] = None,
    folder: Optional[str] = None,  # library subfolder (None = type default)
    cancel: Optional[threading.Event] = None,
    task_id: Optional[str] = None,
):
    """Background task to handle the full download process."""
    try:
        log_callback(f"🚀 Starting background download for {imdb_id} ({type})... [v{VERSION}]")

        config = load_config()
        if not config:
            log_callback(f"⚠️ Config not found or empty at {CONFIG_FILE}")

        rd_client, aria2_client = get_clients(config)
        if not rd_client:
            log_callback("❌ Real-Debrid is required but not available. Set RD_API_KEY / config.json.")
            return

        t_cfg = config.get("torrentio", {})
        torrentio = TorrentioClient(
            base_url=t_cfg.get("base_url", "https://torrentio.strem.fun"),
            config=t_cfg.get("config", ""),
            preferred_quality=t_cfg.get("preferred_quality", ["1080p", "720p"]),
            exclude_keywords=t_cfg.get("exclude_keywords", []),
        )
        if not torrentio.rd_configured:
            log_callback("⚠️ Torrentio has no Real-Debrid key in its config string — "
                         "cached ([RD+]) detection & instant links are disabled. Generate one at "
                         "torrentio.strem.fun/configure and set TORRENTIO_CONFIG.")

        profile = QualityProfile(
            name="custom",
            description="Web generated profile",
            preferred_resolution=t_cfg.get("preferred_quality", ["1080p"]),
            exclude_keywords=t_cfg.get("exclude_keywords", []),
        )

        delay = float(config.get("download", {}).get("delay_between_episodes", 1.5))
        base_dir = _content_root(config, folder, type)

        log_callback("🔌 Connected to Real-Debrid"
                     + (" + aria2" if aria2_client else " (no aria2 — files stay in RD cloud)"))

        if type == "movie":
            movie = get_movie_metadata(imdb_id)
            if not movie:
                log_callback(f"❌ Could not find metadata for {imdb_id}")
                return

            update_task_name(task_id, movie.name)
            log_callback(f"🎬 Processing Movie: {movie.name} ({movie.year})")
            _check_cancel(cancel)
            streams = torrentio.get_movie_streams(imdb_id)
            selected = torrentio.select_best_stream(streams, profile)

            if not selected:
                log_callback("❌ No suitable stream found.")
                return {"type": "movie", "delivered": False}

            cache_tag = "⚡RD+ " if selected.cached else ""
            log_callback(f"✅ Selected: {cache_tag}{selected.quality} | {selected.size}")

            year = str(movie.year).split("-")[0].split("–")[0].strip()
            content_name = f"{movie.name} ({year})"

            _check_cancel(cancel)
            # Fast path: cached stream + aria2 -> instant direct link
            if aria2_client and selected.cached and selected.resolve_url:
                if _deliver_cached(selected, aria2_client, base_dir, content_name, "movie", log_callback, folder):
                    trigger_jellyfin_scan(config, log_callback)
                    log_callback("✨ Task completed!")
                    return {"type": "movie", "delivered": True}
                log_callback("↳ Cached fast-path failed, falling back to RD download...")

            # Fallback: add magnet, let RD download, then sync to aria2
            log_callback("📥 Adding to Real-Debrid...")
            rd_client.add_magnet(selected)
            record_torrent(rd_client.torrent_id(selected.info_hash), content_name, "movie", folder)
            if aria2_client:
                log_callback("⏳ Waiting for RD to cache...")
                sync_rd_to_aria2(rd_client, aria2_client, content_name, "movie", config, log_callback, base_dir, folder, cancel)
                trigger_jellyfin_scan(config, log_callback)
            log_callback("✨ Task completed!")
            return {"type": "movie", "delivered": True}

        elif type == "series":
            series = get_series_metadata(imdb_id)
            if not series:
                log_callback(f"❌ Could not find metadata for {imdb_id}")
                return

            episodes = _filter_episodes(series, selection)
            update_task_name(task_id, series.name)
            log_callback(f"📺 Processing Series: {series.name} — {len(episodes)} episode(s) selected")

            # Episode counts per season drive absolute-number parsing inside
            # packs (anime releases number continuously across seasons).
            season_counts = {n: len(v) for n, v in series.seasons.items()}

            covered_hashes: set[str] = set()
            covered_seasons: set[int] = set()
            sent_resolves: set[str] = set()
            no_stream_keys: set[str] = set()  # "season:episode" with no stream found
            cached_sent = 0
            rd_queued = 0
            pack_covered = 0
            no_stream = 0

            for i, ep in enumerate(episodes):
                _check_cancel(cancel)
                if ep.season in covered_seasons:
                    pack_covered += 1
                    continue

                streams = torrentio.get_streams(ep)
                ranked = torrentio.rank_streams(streams, profile)
                selected = ranked[0] if ranked else None

                if i < len(episodes) - 1:
                    time.sleep(delay)  # rate-limit Torrentio

                if not selected:
                    log_callback(f"⚠️ No stream for {ep.label}")
                    no_stream += 1
                    no_stream_keys.add(f"{ep.season}:{ep.episode}")
                    continue

                pack_seasons = _pack_seasons(selected, ep.season)
                cache_tag = "⚡RD+ " if selected.cached else ""

                if selected.info_hash in covered_hashes:
                    # This torrent is already queued on RD (a pack) — the sync
                    # stage will deliver its files, so skip the covered seasons.
                    covered_seasons |= pack_seasons
                    pack_covered += 1
                    continue

                if pack_seasons:
                    # Multi-episode pack: the resolve fast-path would fetch only
                    # ONE file, so always go through RD (instant when cached) —
                    # the sync stage delivers every file in the pack.
                    label = ", ".join(f"S{s:02d}" for s in sorted(pack_seasons))
                    log_callback(f"📦 {ep.label}: {cache_tag}pack covers {label} "
                                 f"({selected.quality}, {selected.size}) — adding to RD")
                    rd_client.add_magnet(selected)
                    record_torrent(rd_client.torrent_id(selected.info_hash), series.name,
                                   "series", folder, season_counts, pack_seasons)
                    covered_hashes.add(selected.info_hash)
                    covered_seasons |= pack_seasons
                    rd_queued += 1
                    continue

                # Single-episode torrent, cached: instant direct link to aria2.
                # A resolve can fail on a stale cache entry, so try the next
                # cached candidate before falling back to a slow RD download.
                if aria2_client and selected.cached and selected.resolve_url:
                    handled, tried = False, 0
                    for cand in ranked:
                        if tried >= 3:
                            break
                        if not cand.cached or not cand.resolve_url or _pack_seasons(cand, ep.season):
                            continue
                        if cand.resolve_url in sent_resolves:
                            handled = True
                            break
                        tried += 1
                        if _deliver_cached(cand, aria2_client, base_dir, series.name,
                                           "series", log_callback, folder, ep):
                            alt = " (alternative source)" if tried > 1 else ""
                            log_callback(f"⬇️ {ep.label}: ⚡RD+ {cand.quality} ({cand.size}){alt}")
                            sent_resolves.add(cand.resolve_url)
                            cached_sent += 1
                            handled = True
                            break
                    if handled:
                        continue
                    log_callback(f"↳ {ep.label}: cached link(s) would not resolve — falling back to RD")

                # Uncached (or resolve failed): add magnet for RD to download
                log_callback(f"⬇️ {ep.label}: adding {cache_tag}{selected.quality} ({selected.size}) to RD")
                rd_client.add_magnet(selected)
                record_torrent(rd_client.torrent_id(selected.info_hash), series.name,
                               "series", folder, season_counts, pack_seasons)
                covered_hashes.add(selected.info_hash)
                rd_queued += 1

            summary = (f"📋 {len(episodes)} episode(s): {cached_sent} sent directly to aria2 | "
                       f"{rd_queued} torrent(s) queued on RD | {pack_covered} covered by packs")
            if no_stream:
                summary += f" | ⚠️ {no_stream} with no stream"
            log_callback(summary)

            # Packs and uncached torrents are delivered by the sync stage
            if aria2_client and rd_queued:
                log_callback("⏳ Syncing RD torrents to aria2 (instant for cached packs)...")
                sync_rd_to_aria2(rd_client, aria2_client, series.name, "series", config,
                                 log_callback, base_dir, folder, cancel, season_counts)

            # ── Gap check ──
            # A pack can be added successfully and still not deliver an episode
            # (dead cache entry, a season the pack turned out not to contain).
            # Re-check what actually reached aria2 and rescue the gaps from a
            # different source rather than reporting success.
            if aria2_client and episodes:
                _check_cancel(cancel)
                rescued = _rescue_missing_episodes(
                    series, episodes, no_stream_keys, covered_hashes, torrentio,
                    profile, rd_client, aria2_client, base_dir, folder,
                    season_counts, delay, log_callback, cancel)
                cached_sent += rescued

            if cached_sent or rd_queued:
                trigger_jellyfin_scan(config, log_callback)
            log_callback("✨ Task completed!")
            # Outcome per selected episode — the monitor uses this to update
            # its ledger (grabbed = reached any delivery path; no-stream ones
            # are retried on the next monitoring check).
            grabbed = [f"{ep.season}:{ep.episode}" for ep in episodes
                       if f"{ep.season}:{ep.episode}" not in no_stream_keys]
            return {"type": "series", "grabbed": grabbed,
                    "no_stream": sorted(no_stream_keys)}

        log_callback("✨ Task completed!")
    except TaskCancelled:
        log_callback("🛑 Task cancelled. Files already handed to aria2 keep downloading "
                     "(cancel them in the Downloads panel if needed).")
    except Exception as e:
        log_callback(f"❌ CRITICAL ERROR: {str(e)}")
        log_callback(traceback.format_exc())
    return None


MAX_RESCUE_EPISODES = 30


def _rescue_missing_episodes(series, episodes, no_stream_keys, tried_hashes,
                             torrentio, profile, rd_client, aria2_client,
                             base_dir, folder, season_counts, delay,
                             log_callback, cancel=None) -> int:
    """Re-fetch episodes that never actually reached aria2.

    Adding a torrent successfully is not the same as delivering an episode: a
    pack can fail to unrestrict, or turn out not to contain the season its
    name advertised. This compares the selection against what the delivery
    journal says was really sent and retries the gaps from a different source.
    """
    delivered = set(build_library_index()["series"].get(series.name.lower().strip(), []))

    # Torrents still working on RD will be finished by the reconciler — don't
    # duplicate their episodes by fetching alternatives now.
    with _deliveries_lock:
        torrents = _read_deliveries().get("torrents", {})
    if any(not t.get("done") and t.get("content_name") == series.name
           for t in torrents.values()):
        return 0

    missing = [ep for ep in episodes
               if f"{ep.season}:{ep.episode}" not in delivered
               and f"{ep.season}:{ep.episode}" not in no_stream_keys]
    if not missing:
        return 0

    capped = missing[:MAX_RESCUE_EPISODES]
    log_callback(f"🔎 {len(missing)} selected episode(s) never reached aria2 — "
                 f"retrying {len(capped)} from other sources...")
    if len(capped) < len(missing):
        log_callback(f"⚠️ Only the first {MAX_RESCUE_EPISODES} are retried this run; "
                     "run the download again to continue.")

    rescued = 0
    for i, ep in enumerate(capped):
        _check_cancel(cancel)
        ranked = torrentio.rank_streams(torrentio.get_streams(ep), profile)
        if i < len(capped) - 1:
            time.sleep(delay)
        for cand in ranked[:6]:
            # Skip the torrents that already let us down, and packs (a pack
            # would just repeat the same bulk failure).
            if cand.info_hash in tried_hashes or _pack_seasons(cand, ep.season):
                continue
            if not (cand.cached and cand.resolve_url):
                continue
            if _deliver_cached(cand, aria2_client, base_dir, series.name,
                               "series", log_callback, folder, ep):
                log_callback(f"🩹 Recovered {ep.label}: ⚡RD+ {cand.quality} ({cand.size})")
                tried_hashes.add(cand.info_hash)
                rescued += 1
                break
        else:
            log_callback(f"⚠️ {ep.label}: no working alternative source found")
    return rescued


def sync_rd_to_aria2(rd_client, aria2_client, content_name, start_type, config,
                     log_callback, base_dir=None, folder=None, cancel=None,
                     season_counts=None, pack_seasons=None):
    """Poll RD to completion, then push each file to aria2 exactly once.

    Used for uncached torrents that RD must download first. Sent links are
    tracked in the persistent delivery journal, so an interrupted sync is
    finished later by resume_pending_deliveries(). (Cached single episodes are
    delivered directly via resolve_direct_link and never reach this path.)
    """
    try:
        if base_dir is None:
            base_dir = config.get("aria2", {}).get("download_dir", "")
        timeout = int(config.get("aria2", {}).get("poll_timeout", 900))
        poll_interval = 5

        pending = rd_client.get_pending_torrents()
        if not pending:
            log_callback("ℹ️ No pending RD torrents to sync.")
            return

        log_callback(f"🔄 Monitoring {len(pending)} torrent(s) on RD...")

        total_sent = 0
        total_failed = 0
        skipped_extras = 0

        for tid in pending:
            _check_cancel(cancel)
            start = time.time()
            info = None
            while time.time() - start < timeout:
                _check_cancel(cancel)
                info = rd_client.get_torrent_info(tid)
                if not info:
                    break
                status = info.get("status")
                if status == "downloaded":
                    break
                if status in ("error", "virus", "dead"):
                    log_callback(f"❌ RD error for {info.get('filename', '?')}: {status}")
                    mark_torrent_done(tid)
                    info = None
                    break
                time.sleep(poll_interval)
            else:
                log_callback(f"⏳ Torrent still processing after {timeout}s — the delivery "
                             f"journal will finish it later: {info.get('filename', '?') if info else tid}")
                continue

            if not info:
                continue

            # Each torrent carries its own naming context (a pack covering
            # S01+S02 numbers files differently from a single-season pack).
            t_counts, t_packs = _torrent_context(tid)
            sent, failed, skipped = _deliver_rd_links(
                info, content_name, start_type, base_dir, folder,
                rd_client, aria2_client, log_callback, cancel,
                t_counts or season_counts, t_packs or pack_seasons)
            total_sent += sent
            total_failed += failed
            skipped_extras += skipped
            if not failed:
                mark_torrent_done(tid)

        summary = f"✅ Sent {total_sent} file(s) to aria2"
        if skipped_extras:
            summary += f" | ⏭️ {skipped_extras} extra/bonus file(s) skipped"
        if total_failed:
            summary += f" | ❌ {total_failed} failed (retry from the Failed files panel)"
        log_callback(summary)
    except TaskCancelled:
        # A cancelled task must not be silently finished by the reconciler —
        # retire its remaining journaled torrents.
        for tid in rd_client.get_pending_torrents():
            mark_torrent_done(tid)
        raise
    except Exception as e:
        log_callback(f"❌ Sync Error: {str(e)}")


def _deliver_rd_links(info, content_name, start_type, base_dir, folder,
                      rd_client, aria2_client, log_callback, cancel=None,
                      season_counts=None, pack_seasons=None) -> tuple[int, int, int]:
    """Send every not-yet-sent file of a downloaded RD torrent to aria2.

    Files are classified from their path *before* unrestricting, so bonus
    material never costs an API call — which also keeps us well clear of RD's
    rate limits on big packs. Episodes are renamed canonically so releases
    from different groups land consistently.

    Consults/updates the persistent delivery journal, so it is safe to call
    again after a crash or reboot — already-sent links are skipped.
    Returns (sent, failed, skipped).
    """
    sent = failed = skipped = 0
    unrecognised: list[str] = []
    # Guards against a pack that ships two copies of an episode (e.g. 1080p
    # and 720p): both would rename to the same target and silently overwrite.
    claimed: set[str] = set()

    links = info.get("links", [])
    selected = [f for f in info.get("files", []) if f.get("selected")]
    # RD returns exactly one link per selected file, in file order. Pairing
    # them gives us the full in-torrent path (directory context included)
    # before we spend an unrestrict call. If the counts ever disagree, fall
    # back to parsing the unrestricted filename rather than risk mis-pairing.
    paired = selected if len(selected) == len(links) else None
    if paired is None and links:
        log_callback(f"⚠️ RD returned {len(links)} link(s) for {len(selected)} selected "
                     "file(s) — classifying after unrestrict instead")

    for idx, link in enumerate(links):
        _check_cancel(cancel)
        if link_sent(link):
            continue

        path = paired[idx].get("path", "") if paired else ""
        parsed = None
        if start_type == "series" and path:
            parsed = parse_media_filename(path, season_counts, pack_seasons)
            if not parsed.is_episode:
                skipped += 1
                if parsed.kind == "extra":
                    record_sent(link, "", os.path.basename(path), content_name,
                                start_type, folder)  # settled: never reconsider
                else:
                    unrecognised.append(os.path.basename(path))
                continue

        item, error = rd_client.unrestrict_link(link)
        if not item or not item.get("download"):
            label = os.path.basename(path) or f"a file from '{info.get('filename', '?')}'"
            log_callback(f"❌ Could not unrestrict {label} — {error}")
            record_failure(content_name, start_type, link, label, error, folder)
            failed += 1
            continue

        fname = item.get("filename", "unknown")
        out_name, subdir = fname, _target_subdir(base_dir, content_name, start_type, fname)

        if start_type == "series":
            if parsed is None:  # no path context — classify the real filename
                parsed = parse_media_filename(fname, season_counts, pack_seasons)
                if not parsed.is_episode:
                    skipped += 1
                    if parsed.kind == "extra":
                        record_sent(link, "", fname, content_name, start_type, folder)
                    else:
                        unrecognised.append(fname)
                    continue
            out_name = canonical_name(content_name, parsed, fname)
            subdir = _target_subdir(base_dir, content_name, start_type, fname, parsed)

            target = os.path.join(subdir, out_name)
            if target in claimed:
                log_callback(f"⏭️ Skipping duplicate of {out_name} ({fname})")
                record_sent(link, "", fname, content_name, start_type, folder)
                skipped += 1
                continue
            claimed.add(target)

        gid = aria2_client.add_download(item["download"], directory=subdir, filename=out_name)
        if gid:
            record_sent(link, gid, out_name, content_name, start_type, folder)
            sent += 1
            log_callback(f"🚀 Sent to aria2: {out_name}")
        else:
            failed += 1
            log_callback(f"❌ aria2 rejected {out_name} — saved for retry")
            record_failure(content_name, start_type, link, out_name, "aria2 add failed", folder)

        time.sleep(0.5)

    if unrecognised:
        preview = ", ".join(unrecognised[:3]) + (" …" if len(unrecognised) > 3 else "")
        log_callback(f"❓ {len(unrecognised)} file(s) had no recognisable episode number "
                     f"and were left alone: {preview}")
    return sent, failed, skipped


def resume_pending_deliveries(log_callback: Callable[[str], None] = lambda m: None):
    """Finish deliveries interrupted by a crash/reboot.

    Walks journaled RD torrents that never completed delivery: if RD has
    finished downloading them meanwhile, their unsent files are pushed to
    aria2 now. Runs at startup and before every monitoring pass.
    """
    now = int(time.time())
    with _deliveries_lock:
        d = _read_deliveries()
    pending = {tid: t for tid, t in d.get("torrents", {}).items()
               if not t.get("done") and now - t.get("ts", 0) < RECONCILE_WINDOW_DAYS * 86400}
    if not pending:
        return

    config = load_config()
    rd_client, aria2_client = get_clients(config)
    if not rd_client or not aria2_client:
        return

    log_callback(f"🧷 Resuming {len(pending)} unfinished RD deliver{'y' if len(pending) == 1 else 'ies'}...")
    for tid, t in pending.items():
        info = rd_client.get_torrent_info(tid)
        if not info:
            mark_torrent_done(tid)  # gone from RD — nothing to resume
            continue
        status = info.get("status")
        if status in ("error", "virus", "dead"):
            log_callback(f"❌ RD gave up on '{info.get('filename', '?')}' ({status})")
            mark_torrent_done(tid)
            continue
        if status != "downloaded":
            continue  # RD still working — try again next pass

        base_dir = _content_root(config, t.get("folder"), t.get("type", "series"))
        counts = {int(k): v for k, v in (t.get("season_counts") or {}).items()}
        sent, failed_n, _ = _deliver_rd_links(
            info, t.get("content_name", "?"), t.get("type", "series"),
            base_dir, t.get("folder"), rd_client, aria2_client, log_callback,
            None, counts, set(t.get("pack_seasons") or []))
        if not failed_n:
            mark_torrent_done(tid)
        if sent:
            log_callback(f"🧷 Recovered {sent} file(s) from '{t.get('content_name', '?')}'")
            trigger_jellyfin_scan(config, log_callback)


def sweep_aria2_errors(log_callback: Callable[[str], None] = lambda m: None):
    """Move aria2 downloads that errored after being queued (e.g. RD link
    expired across a reboot) into the retryable Failed files queue."""
    now = int(time.time())
    with _deliveries_lock:
        d = _read_deliveries()
    candidates = {link: s for link, s in d.get("sent", {}).items()
                  if s.get("gid") and not s.get("swept")
                  and now - s.get("ts", 0) < RECONCILE_WINDOW_DAYS * 86400}
    if not candidates:
        return

    config = load_config()
    _, aria2_client = get_clients(config)
    if not aria2_client:
        return

    def _mark_swept(link):
        with _deliveries_lock:
            data = _read_deliveries()
            if link in data["sent"]:
                data["sent"][link]["swept"] = True
                _write_deliveries(data)

    for link, s in candidates.items():
        st = aria2_client.tell_status(s["gid"])
        if not st:
            break  # aria2 unreachable (or gid purged) — try next sweep
        status = st.get("status")
        if status == "error":
            log_callback(f"❌ aria2 download errored: {s.get('fname', '?')} — saved for retry")
            record_failure(s.get("content_name", "?"), s.get("type", "series"),
                           link, s.get("fname", "?"),
                           f"aria2 error: {st.get('errorMessage', '?')[:80]}", s.get("folder"))
            _mark_swept(link)
        elif status in ("complete", "removed"):
            _mark_swept(link)
        # active/waiting/paused: leave for a later sweep
