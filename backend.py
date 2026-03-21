import json
import logging
import os
import re
import time
import traceback
from pathlib import Path
from typing import Optional, List, Callable

from cinemeta import search_all, get_series_metadata, get_movie_metadata, Episode, Series, Movie
from torrentio import TorrentioClient, TorrentStream
from clients import QBittorrentClient, RealDebridClient, MagnetFileSaver, Aria2Client, AddResult
from profiles import QualityProfile

# Setup logging
logger = logging.getLogger("TorrentDownloader")
logger.setLevel(logging.INFO)

CONFIG_FILE = Path(__file__).parent / "config.json"

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def get_clients(config: dict):
    """Initialize download clients based on config."""
    rd_config = config.get("real_debrid", {})
    qb_config = config.get("qbittorrent", {})
    aria2_config = config.get("aria2", {})

    # Real-Debrid
    rd_client = None
    if rd_config.get("enabled") and rd_config.get("api_key"):
        try:
            rd_client = RealDebridClient(api_key=rd_config["api_key"])
            if not rd_client.test_connection():
                logger.warning("Real-Debrid connection failed.")
                rd_client = None
        except Exception:
            rd_client = None

    # qBittorrent (fallback if RD disabled/failed)
    qb_client = None
    if not rd_client:
        try:
            qb_client = QBittorrentClient(
                host=qb_config.get("host", "http://localhost"),
                port=qb_config.get("port", 8080),
                username=qb_config.get("username", "admin"),
                password=qb_config.get("password", "adminadmin"),
                save_path=qb_config.get("save_path", ""),
                category=qb_config.get("category", "stremio-series"),
            )
            if not qb_client.login():
                logger.warning("qBittorrent connection failed.")
                qb_client = None
        except Exception:
            qb_client = None

    # Aria2 (for RD downloads)
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

    return rd_client, qb_client, aria2_client

def process_download_task(
    imdb_id: str, 
    type: str, 
    log_callback: Callable[[str], None] = lambda msg: None
):
    """Background task to handle the full download process."""
    try:
        log_callback(f"🚀 Starting background download for {imdb_id} ({type})...")
        
        config = load_config()
        if not config:
            log_callback(f"⚠️ Config not found or empty at {CONFIG_FILE}")

        rd_client, qb_client, aria2_client = get_clients(config)
        if not rd_client and not qb_client:
            log_callback("❌ No download client available (RD or qBittorrent). Check config.json.")
            return

        t_cfg = config.get("torrentio", {})
        torrentio = TorrentioClient(
            base_url=t_cfg.get("base_url", "https://torrentio.strem.fun"),
            config=t_cfg.get("config", ""),
            preferred_quality=t_cfg.get("preferred_quality", ["1080p", "720p"]),
            exclude_keywords=t_cfg.get("exclude_keywords", []),
        )

        # specific profile or default
        exclude_kw = config.get("torrentio", {}).get("exclude_keywords", [])
        preferred_res = config.get("torrentio", {}).get("preferred_quality", ["1080p"])

        profile = QualityProfile(
            name="custom",
            description="Web generated profile",
            preferred_resolution=preferred_res,
            exclude_keywords=exclude_kw
        )

        download_client = rd_client if rd_client else qb_client
        download_client_name = "Real-Debrid" if rd_client else "qBittorrent"
        log_callback(f"🔌 Connected to {download_client_name}")

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
                
            log_callback(f"✅ Selected: {selected.quality} | {selected.size}")
            
            if rd_client:
                log_callback("📥 Adding to Real-Debrid...")
                rd_client.add_magnet(selected)
                if aria2_client:
                    log_callback("⏳ Waiting for RD to cache...")
                    sync_rd_to_aria2(rd_client, aria2_client, f"{movie.name} ({movie.year})", "movie", config, log_callback)
            else:
                log_callback("📥 Sending to qBittorrent...")
                qb_client.add_torrent(selected, subfolder=movie.name, tags=movie.name)

        elif type == "series":
            series = get_series_metadata(imdb_id)
            if not series:
                log_callback(f"❌ Could not find metadata for {imdb_id}")
                return

            log_callback(f"📺 Processing Series: {series.name} - {len(series.seasons)} Seasons")
            
            episodes_flat = []
            for s_num in sorted(series.seasons.keys()):
                episodes_flat.extend(series.seasons[s_num])
            
            log_callback(f"📋 Found {len(episodes_flat)} episodes. Checking streams...")
            
            covered_hashes = set()
            
            for ep in episodes_flat:
                streams = torrentio.get_streams(ep)
                selected = torrentio.select_best_stream(streams, profile)
                
                if not selected:
                    log_callback(f"⚠️ No stream for {ep.label}")
                    continue
                    
                if selected.info_hash in covered_hashes:
                    continue
                    
                log_callback(f"⬇️ {ep.label}: Adding {selected.quality} ({selected.size})")
                
                if rd_client:
                    rd_client.add_magnet(selected)
                    covered_hashes.add(selected.info_hash)
                else:
                    qb_client.add_torrent(selected, subfolder=f"{series.name}/Season {ep.season}", tags=series.name)
                    covered_hashes.add(selected.info_hash)

            if rd_client and aria2_client:
                log_callback("⏳ Waiting for RD Cache & Syncing to Aria2...")
                sync_rd_to_aria2(rd_client, aria2_client, series.name, "series", config, log_callback)

        log_callback("✨ Task completed!")
    except Exception as e:
        log_callback(f"❌ CRITICAL ERROR: {str(e)}")
        log_callback(traceback.format_exc())

def sync_rd_to_aria2(rd_client, aria2_client, content_name, start_type, config, log_callback):
    """Simplified sync version for backend."""
    try:
        base_dir = config.get("aria2", {}).get("download_dir", "")
        timeout = 600
        start = time.time()
        
        pending = rd_client.get_pending_torrents()
        if not pending:
            log_callback("ℹ️ No pending RD torrents found.")
            return

        log_callback(f"🔄 Monitoring {len(pending)} torrents on RD...")
        
        while time.time() - start < timeout:
            all_done = True
            for tid in pending:
                info = rd_client.get_torrent_info(tid)
                if not info: continue
                
                status = info.get('status')
                if status == 'downloaded':
                    for link in info.get('links', []):
                        item = rd_client.unrestrict_link(link)
                        if item and item.get('download'):
                            fname = item.get('filename')
                            subdir = ""
                            if start_type == "movie":
                                subdir = os.path.join(base_dir, content_name)
                            else:
                                s_match = re.search(r'[Ss](\d{1,2})', fname)
                                s_dir = f"Season {int(s_match.group(1)):02d}" if s_match else ""
                                subdir = os.path.join(base_dir, content_name, s_dir)
                                
                            aria2_client.add_download(item['download'], directory=subdir, filename=fname)
                            log_callback(f"🚀 Sent to Aria2: {fname}")
                elif status in ['magnet_conversion', 'waiting_files_selection', 'downloading']:
                    all_done = False
                elif status in ['virus', 'error', 'dead']:
                    log_callback(f"❌ RD Error for {info.get('filename')}: {status}")

            if all_done:
                break
            time.sleep(5)
    except Exception as e:
         log_callback(f"❌ Sync Error: {str(e)}")
