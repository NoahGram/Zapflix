"""
New-episode monitoring — a Sonarr-style watchlist.

Zapflix keeps its own ledger of grabbed episodes per monitored show in
data/monitored.json (it delivers everything itself, so no NAS filesystem
access is needed — aria2's RPC can't browse folders anyway). A background
checker compares Cinemeta's episode list against the ledger and downloads
anything new through the normal pipeline.

Key subtlety: Cinemeta lists announced-but-unaired episodes for ongoing
shows. Seeding and checking therefore only consider episodes whose
`released` date is in the past — otherwise unaired episodes would be
"known" from day one and never grabbed when they actually air.
"""

import json
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from backend import (DATA_DIR, load_config, process_download_task,
                     resume_pending_deliveries, sweep_aria2_errors)
from cinemeta import get_series_metadata

MONITORED_FILE = DATA_DIR / "monitored.json"
_monitored_lock = threading.Lock()
_check_lock = threading.Lock()  # one check at a time (loop vs. "Check now")


def _ep_aired(released: str) -> bool:
    """True if the episode's air date is in the past (or unknown)."""
    if not released:
        return True
    try:
        dt = datetime.fromisoformat(released.replace("Z", "+00:00"))
        return dt <= datetime.now(timezone.utc)
    except ValueError:
        return True


def _read() -> list[dict]:
    try:
        with open(MONITORED_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _write(items: list[dict]):
    MONITORED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MONITORED_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(items, f, indent=1)
    tmp.replace(MONITORED_FILE)


def list_monitored() -> list[dict]:
    with _monitored_lock:
        return _read()


def add_monitored(imdb_id: str, folder: Optional[str] = None) -> tuple[bool, str]:
    """Start monitoring a series. Seeds the ledger with all already-aired
    episodes so only *future* episodes are auto-grabbed. `folder` is the
    library subfolder new episodes are delivered to (None = series default)."""
    with _monitored_lock:
        items = _read()
        if any(it["imdb_id"] == imdb_id for it in items):
            return True, "Already monitored"

    series = get_series_metadata(imdb_id)
    if not series or not series.seasons:
        return False, f"No series metadata for {imdb_id}"

    known = []
    unaired = 0
    for s in sorted(series.seasons):
        for ep in series.seasons[s]:
            if _ep_aired(ep.released):
                known.append(f"{ep.season}:{ep.episode}")
            else:
                unaired += 1

    with _monitored_lock:
        items = _read()
        if any(it["imdb_id"] == imdb_id for it in items):  # raced
            return True, "Already monitored"
        items.append({
            "imdb_id": imdb_id,
            "name": series.name,
            "folder": folder,
            "added": int(time.time()),
            "last_check": 0,
            "last_grab": "",
            "known": known,
        })
        _write(items)
    return True, (f"Monitoring {series.name} — watching for new episodes "
                  f"({len(known)} aired episodes ignored, {unaired} upcoming)")


def remove_monitored(imdb_id: str) -> bool:
    with _monitored_lock:
        items = _read()
        kept = [it for it in items if it["imdb_id"] != imdb_id]
        _write(kept)
        return len(kept) < len(items)


def note_grabbed(imdb_id: str, grabbed_keys: list[str]):
    """Record episodes delivered outside the checker (manual downloads),
    so the next check doesn't re-grab them."""
    if not grabbed_keys:
        return
    with _monitored_lock:
        items = _read()
        for it in items:
            if it["imdb_id"] == imdb_id:
                merged = set(it.get("known", [])) | set(grabbed_keys)
                it["known"] = sorted(merged, key=lambda k: tuple(map(int, k.split(":"))))
                _write(items)
                break


def check_all(log_callback: Callable[[str], None] = lambda m: None) -> Optional[dict]:
    """One monitoring pass over all shows. Returns {show: new_count} or None
    if a check is already running."""
    if not _check_lock.acquire(blocking=False):
        log_callback("⏳ Monitoring check already running — skipped.")
        return None
    try:
        # First: finish anything a crash/reboot interrupted, and surface
        # aria2 downloads that errored since we queued them.
        try:
            resume_pending_deliveries(log_callback)
            sweep_aria2_errors(log_callback)
        except Exception as e:
            log_callback(f"⚠️ Delivery reconcile failed: {e}")

        shows = list_monitored()
        if not shows:
            return {}
        log_callback(f"🔔 Checking {len(shows)} monitored show(s) for new episodes...")
        results: dict[str, int] = {}

        for show in shows:
            imdb_id = show["imdb_id"]
            try:
                series = get_series_metadata(imdb_id)
            except Exception as e:
                log_callback(f"⚠️ Metadata failed for {show['name']}: {e}")
                continue
            if not series or not series.seasons:
                continue

            known = set(show.get("known", []))
            new_eps = []
            for s in sorted(series.seasons):
                for ep in series.seasons[s]:
                    key = f"{ep.season}:{ep.episode}"
                    if key not in known and _ep_aired(ep.released):
                        new_eps.append(ep)

            if not new_eps:
                _update_show(imdb_id, last_check=int(time.time()))
                continue

            labels = ", ".join(ep.label for ep in new_eps[:8])
            if len(new_eps) > 8:
                labels += f" (+{len(new_eps) - 8} more)"
            log_callback(f"🆕 {series.name}: {len(new_eps)} new episode(s) — {labels}")

            # Group into the selection format the download task understands
            by_season: dict[int, list[int]] = {}
            for ep in new_eps:
                by_season.setdefault(ep.season, []).append(ep.episode)
            selection = [{"season": s, "episodes": eps} for s, eps in sorted(by_season.items())]

            result = process_download_task(imdb_id, "series", log_callback, selection,
                                           folder=show.get("folder"))
            grabbed = (result or {}).get("grabbed", [])
            if grabbed:
                note_grabbed(imdb_id, grabbed)
                last = max(grabbed, key=lambda k: tuple(map(int, k.split(":"))))
                s, e = last.split(":")
                _update_show(imdb_id, last_check=int(time.time()),
                             last_grab=f"S{int(s):02d}E{int(e):02d} · {time.strftime('%d %b %H:%M')}")
            else:
                _update_show(imdb_id, last_check=int(time.time()))
            results[series.name] = len(grabbed)

        grabbed_total = sum(results.values())
        if grabbed_total:
            log_callback(f"🔔 Monitoring done — grabbed {grabbed_total} episode(s).")
        else:
            log_callback("🔔 Monitoring done — nothing new.")
        return results
    finally:
        _check_lock.release()


def _update_show(imdb_id: str, **fields):
    with _monitored_lock:
        items = _read()
        for it in items:
            if it["imdb_id"] == imdb_id:
                it.update(fields)
                _write(items)
                break


def monitor_loop(log_callback: Callable[[str], None] = lambda m: None):
    """Daemon-thread loop: periodic checks, interval from config."""
    time.sleep(90)  # let the server settle before the first pass
    while True:
        try:
            check_all(log_callback)
        except Exception as e:
            log_callback(f"❌ Monitoring loop error: {e}")
        hours = float(load_config().get("monitoring", {}).get("check_interval_hours", 6))
        time.sleep(max(hours, 0.5) * 3600)
