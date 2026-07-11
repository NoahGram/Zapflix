import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Optional, List, Callable, Union

import requests
from dotenv import load_dotenv

from cinemeta import search_all, get_series_metadata, get_movie_metadata, Episode, Series, Movie
from torrentio import TorrentioClient, TorrentStream
from clients import RealDebridClient, Aria2Client, AddResult
from profiles import QualityProfile

# Setup logging
logger = logging.getLogger("TorrentDownloader")
logger.setLevel(logging.INFO)

CONFIG_FILE = Path(__file__).parent / "config.json"

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


def _is_season_pack(stream: TorrentStream, season: int) -> bool:
    """Heuristic: does this stream cover a whole season (not one episode)?"""
    text = f"{stream.title} {stream.filename}".lower()
    if re.search(rf"s0*{season}\s*e\d+\s*[-–~]\s*e?\d+", text):
        return True
    if re.search(r"e\d+\s*[-–~]\s*e?\d+", text):
        return True
    if "complete" in text or "full season" in text:
        return True
    if re.search(rf"season\s*0*{season}\b", text):
        return True
    if re.search(rf"s0*{season}\b", text) and not re.search(rf"s0*{season}e\d+", text):
        return True
    return False


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


def _target_subdir(base_dir: str, content_name: str, start_type: str, fname: str) -> str:
    """Build the Jellyfin-friendly NAS subdirectory for a file."""
    if start_type == "movie":
        return os.path.join(base_dir, content_name)
    s_match = re.search(r"[Ss](\d{1,2})", fname)
    s_dir = f"Season {int(s_match.group(1)):02d}" if s_match else ""
    return os.path.join(base_dir, content_name, s_dir)


def _deliver_cached(stream, aria2_client, base_dir, content_name, start_type, log_callback) -> bool:
    """Fast path for cached streams: resolve the direct link and push to aria2.

    Returns True on success. No RD polling, no unrestrict — instant & reliable.
    """
    direct = resolve_direct_link(stream.resolve_url)
    if not direct:
        return False
    fname = stream.filename or content_name
    subdir = _target_subdir(base_dir, content_name, start_type, fname)
    gid = aria2_client.add_download(direct, directory=subdir, filename=fname)
    if gid:
        log_callback(f"🚀 Sent cached file to aria2: {fname}")
        return True
    return False


def process_download_task(
    imdb_id: str,
    type: str,
    log_callback: Callable[[str], None] = lambda msg: None,
    selection: Union[str, list, None] = None,
):
    """Background task to handle the full download process."""
    try:
        log_callback(f"🚀 Starting background download for {imdb_id} ({type})...")

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
        base_dir = config.get("aria2", {}).get("download_dir", "")

        log_callback("🔌 Connected to Real-Debrid"
                     + (" + aria2" if aria2_client else " (no aria2 — files stay in RD cloud)"))

        if type == "movie":
            movie = get_movie_metadata(imdb_id)
            if not movie:
                log_callback(f"❌ Could not find metadata for {imdb_id}")
                return

            log_callback(f"🎬 Processing Movie: {movie.name} ({movie.year})")
            streams = torrentio.get_movie_streams(imdb_id)
            selected = torrentio.select_best_stream(streams, profile)

            if not selected:
                log_callback("❌ No suitable stream found.")
                return

            cache_tag = "⚡RD+ " if selected.cached else ""
            log_callback(f"✅ Selected: {cache_tag}{selected.quality} | {selected.size}")

            year = str(movie.year).split("-")[0].split("–")[0].strip()
            content_name = f"{movie.name} ({year})"

            # Fast path: cached stream + aria2 -> instant direct link
            if aria2_client and selected.cached and selected.resolve_url:
                if _deliver_cached(selected, aria2_client, base_dir, content_name, "movie", log_callback):
                    log_callback("✨ Task completed!")
                    return
                log_callback("↳ Cached fast-path failed, falling back to RD download...")

            # Fallback: add magnet, let RD download, then sync to aria2
            log_callback("📥 Adding to Real-Debrid...")
            rd_client.add_magnet(selected)
            if aria2_client:
                log_callback("⏳ Waiting for RD to cache...")
                sync_rd_to_aria2(rd_client, aria2_client, content_name, "movie", config, log_callback)

        elif type == "series":
            series = get_series_metadata(imdb_id)
            if not series:
                log_callback(f"❌ Could not find metadata for {imdb_id}")
                return

            episodes = _filter_episodes(series, selection)
            log_callback(f"📺 Processing Series: {series.name} — {len(episodes)} episode(s) selected")

            covered_hashes: set[str] = set()
            covered_seasons: set[int] = set()
            cached_sent = 0
            uncached_added = 0

            for i, ep in enumerate(episodes):
                if ep.season in covered_seasons:
                    continue

                streams = torrentio.get_streams(ep)
                selected = torrentio.select_best_stream(streams, profile)

                if i < len(episodes) - 1:
                    time.sleep(delay)  # rate-limit Torrentio

                if not selected:
                    log_callback(f"⚠️ No stream for {ep.label}")
                    continue

                is_pack = _is_season_pack(selected, ep.season)

                if selected.info_hash in covered_hashes:
                    if is_pack:
                        covered_seasons.add(ep.season)
                    continue
                covered_hashes.add(selected.info_hash)
                if is_pack:
                    covered_seasons.add(ep.season)

                cache_tag = "⚡RD+ " if selected.cached else ""
                pack_tag = " [season pack]" if is_pack else ""

                # Fast path: cached + aria2 -> instant direct link, no RD polling
                if aria2_client and selected.cached and selected.resolve_url:
                    if _deliver_cached(selected, aria2_client, base_dir, series.name, "series", log_callback):
                        log_callback(f"⬇️ {ep.label}: {cache_tag}{selected.quality} ({selected.size}){pack_tag}")
                        cached_sent += 1
                        if is_pack:
                            log_callback(f"📦 Season {ep.season} covered by pack — skipping remaining queries")
                        continue

                # Fallback: uncached (or resolve failed) -> add magnet for RD to download
                log_callback(f"⬇️ {ep.label}: adding {cache_tag}{selected.quality} ({selected.size}){pack_tag} to RD")
                rd_client.add_magnet(selected)
                uncached_added += 1
                if is_pack:
                    log_callback(f"📦 Season {ep.season} covered by pack — skipping remaining queries")

            log_callback(f"📋 Cached sent to aria2: {cached_sent} | Queued on RD: {uncached_added}")

            # Only the uncached (add_magnet'd) torrents need RD polling + sync
            if aria2_client and uncached_added:
                log_callback("⏳ Waiting for RD to download uncached torrents & syncing to aria2...")
                sync_rd_to_aria2(rd_client, aria2_client, series.name, "series", config, log_callback)

        log_callback("✨ Task completed!")
    except Exception as e:
        log_callback(f"❌ CRITICAL ERROR: {str(e)}")
        log_callback(traceback.format_exc())


def sync_rd_to_aria2(rd_client, aria2_client, content_name, start_type, config, log_callback):
    """Poll RD to completion, then push each file to aria2 exactly once.

    Used for uncached torrents that RD must download first. Each RD file link is
    unrestricted and forwarded a single time (tracked in `sent_links`), with a
    retry on failure and a per-torrent timeout. (Cached content is delivered
    directly via resolve_direct_link and never reaches this path.)
    """
    try:
        base_dir = config.get("aria2", {}).get("download_dir", "")
        timeout = int(config.get("aria2", {}).get("poll_timeout", 900))
        poll_interval = 5

        pending = rd_client.get_pending_torrents()
        if not pending:
            log_callback("ℹ️ No pending RD torrents to sync.")
            return

        log_callback(f"🔄 Monitoring {len(pending)} torrent(s) on RD...")

        sent_links: set[str] = set()
        total_sent = 0
        total_failed = 0

        for tid in pending:
            start = time.time()
            info = None
            while time.time() - start < timeout:
                info = rd_client.get_torrent_info(tid)
                if not info:
                    break
                status = info.get("status")
                if status == "downloaded":
                    break
                if status in ("error", "virus", "dead"):
                    log_callback(f"❌ RD error for {info.get('filename', '?')}: {status}")
                    info = None
                    break
                time.sleep(poll_interval)
            else:
                log_callback(f"⏳ Torrent still processing after {timeout}s — "
                             f"RD keeps downloading in the background: {info.get('filename', '?') if info else tid}")
                continue

            if not info:
                continue

            links = info.get("links", [])
            if not links:
                log_callback(f"⚠️ No files in torrent: {info.get('filename', '?')}")
                continue

            for link in links:
                if link in sent_links:
                    continue

                item = rd_client.unrestrict_link(link)
                if not item or not item.get("download"):
                    time.sleep(2)  # transient / expired-link retry
                    item = rd_client.unrestrict_link(link)
                if not item or not item.get("download"):
                    log_callback("❌ Failed to unrestrict a link (skipped)")
                    total_failed += 1
                    continue

                fname = item.get("filename", "unknown")
                subdir = _target_subdir(base_dir, content_name, start_type, fname)

                gid = aria2_client.add_download(item["download"], directory=subdir, filename=fname)
                if gid:
                    sent_links.add(link)
                    total_sent += 1
                    log_callback(f"🚀 Sent to aria2: {fname}")
                else:
                    total_failed += 1
                    log_callback(f"❌ aria2 failed for: {fname}")

                time.sleep(0.5)

        summary = f"✅ Sent {total_sent} file(s) to aria2"
        if total_failed:
            summary += f" | ❌ {total_failed} failed"
        log_callback(summary)
    except Exception as e:
        log_callback(f"❌ Sync Error: {str(e)}")
