"""
Download clients - handles sending torrents to qBittorrent or Real-Debrid.
"""

import os
import json
import time
import requests
from enum import Enum
from typing import Optional
from dataclasses import dataclass

from torrentio import TorrentStream


class AddResult(Enum):
    """Result of attempting to add a torrent."""
    SUCCESS = "success"          # Newly added
    ALREADY_EXISTS = "exists"    # Same hash already in client (season pack)
    FAILED = "failed"            # Actual failure


# ─── qBittorrent Client ────────────────────────────────────────────────────────


class QBittorrentClient:
    """Manages downloads via qBittorrent Web API."""

    def __init__(
        self,
        host: str = "http://localhost",
        port: int = 8080,
        username: str = "admin",
        password: str = "adminadmin",
        save_path: str = "",
        category: str = "stremio-series",
        sequential: bool = True,
        first_last_priority: bool = True,
    ):
        self.base_url = f"{host}:{port}"
        self.username = username
        self.password = password
        self.save_path = save_path
        self.category = category
        self.sequential = sequential
        self.first_last_priority = first_last_priority
        self.session = requests.Session()
        self._authenticated = False
        self._added_hashes: set[str] = set()   # Track hashes added this session
        self._existing_hashes: set[str] = set()  # Hashes already in qBittorrent

    def login(self) -> bool:
        """Authenticate with qBittorrent Web API."""
        try:
            resp = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=10,
            )
            if resp.text == "Ok.":
                self._authenticated = True
                # Ensure category exists
                self._ensure_category()
                # Load existing torrent hashes to detect duplicates
                self._load_existing_hashes()
                return True
            else:
                print(f"  ✗ qBittorrent login failed: {resp.text}")
                return False
        except requests.RequestException as e:
            print(f"  ✗ Cannot connect to qBittorrent at {self.base_url}: {e}")
            return False

    def _ensure_category(self):
        """Create the download category if it doesn't exist."""
        try:
            data = {"category": self.category}
            if self.save_path:
                data["savePath"] = self.save_path
            self.session.post(
                f"{self.base_url}/api/v2/torrents/createCategory",
                data=data,
                timeout=10,
            )
        except Exception:
            pass  # Category may already exist

    def _load_existing_hashes(self):
        """Load info hashes of torrents already in qBittorrent."""
        try:
            resp = self.session.get(
                f"{self.base_url}/api/v2/torrents/info",
                timeout=10,
            )
            if resp.status_code == 200:
                for t in resp.json():
                    h = t.get("hash", "").lower()
                    if h:
                        self._existing_hashes.add(h)
        except Exception:
            pass

    def add_torrent(
        self,
        stream: TorrentStream,
        subfolder: str = "",
        tags: str = "",
    ) -> AddResult:
        """Add a torrent to qBittorrent via magnet link.

        Returns AddResult.ALREADY_EXISTS if the same info hash was already
        added (common with season packs where every episode shares one torrent).
        """
        info_hash = stream.info_hash.lower()

        # Check if this exact hash was already added this session or exists in qBittorrent
        # Do this BEFORE login attempt so it works even when testing without qBittorrent
        if info_hash in self._added_hashes or info_hash in self._existing_hashes:
            return AddResult.ALREADY_EXISTS

        if not self._authenticated:
            if not self.login():
                return AddResult.FAILED
            return AddResult.ALREADY_EXISTS

        magnet = stream.magnet_link

        data = {
            "urls": magnet,
            "category": self.category,
            "sequentialDownload": str(self.sequential).lower(),
            "firstLastPiecePrio": str(self.first_last_priority).lower(),
        }

        if subfolder and self.save_path:
            data["savepath"] = os.path.join(self.save_path, subfolder)
        elif self.save_path:
            data["savepath"] = self.save_path

        if tags:
            data["tags"] = tags

        try:
            resp = self.session.post(
                f"{self.base_url}/api/v2/torrents/add",
                data=data,
                timeout=15,
            )
            if resp.text == "Ok.":
                self._added_hashes.add(info_hash)
                return AddResult.SUCCESS
            elif resp.text == "Fails.":
                # qBittorrent returns "Fails." for duplicates — re-check
                self._existing_hashes.add(info_hash)
                return AddResult.ALREADY_EXISTS
            else:
                return AddResult.FAILED
        except requests.RequestException as e:
            print(f"  ✗ Error adding torrent: {e}")
            return AddResult.FAILED

    def get_torrent_list(self, category: Optional[str] = None) -> list[dict]:
        """Get list of torrents, optionally filtered by category."""
        if not self._authenticated:
            if not self.login():
                return []

        params = {}
        if category:
            params["category"] = category

        try:
            resp = self.session.get(
                f"{self.base_url}/api/v2/torrents/info",
                params=params,
                timeout=10,
            )
            return resp.json()
        except Exception:
            return []

    def logout(self):
        """Logout from qBittorrent."""
        try:
            self.session.post(f"{self.base_url}/api/v2/auth/logout", timeout=5)
        except Exception:
            pass


# ─── Real-Debrid Client ────────────────────────────────────────────────────────


class RealDebridClient:
    """Manages downloads via Real-Debrid API (adds magnets for cloud downloading)."""

    API_BASE = "https://api.real-debrid.com/rest/1.0"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
            }
        )
        self._added_hashes: set[str] = set()  # Track hashes added this session
        self._torrent_ids: dict[str, str] = {}  # hash -> torrent_id

    def test_connection(self) -> bool:
        """Test if the API key is valid."""
        try:
            resp = self.session.get(f"{self.API_BASE}/user", timeout=10)
            if resp.status_code == 200:
                user = resp.json()
                print(f"  ✓ Real-Debrid connected as: {user.get('username', '?')}")
                return True
            else:
                print(f"  ✗ Real-Debrid auth failed: {resp.status_code}")
                return False
        except requests.RequestException as e:
            print(f"  ✗ Cannot connect to Real-Debrid: {e}")
            return False

    def add_magnet(self, stream: TorrentStream) -> AddResult:
        """Add a magnet link to Real-Debrid.

        Returns AddResult.ALREADY_EXISTS if the same info hash was already
        added this session (e.g. season pack covering multiple episodes).
        """
        info_hash = stream.info_hash.lower()

        # Deduplicate: skip if this exact hash was already added
        if info_hash in self._added_hashes:
            return AddResult.ALREADY_EXISTS

        try:
            resp = self.session.post(
                f"{self.API_BASE}/torrents/addMagnet",
                data={"magnet": stream.magnet_link},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            torrent_id = data.get("id")

            if torrent_id:
                # Select all files
                self.session.post(
                    f"{self.API_BASE}/torrents/selectFiles/{torrent_id}",
                    data={"files": "all"},
                    timeout=15,
                )
                self._added_hashes.add(info_hash)
                self._torrent_ids[info_hash] = torrent_id
                return AddResult.SUCCESS
        except requests.RequestException as e:
            print(f"  ✗ Real-Debrid error: {e}")
        return AddResult.FAILED

    def get_pending_torrents(self) -> list[str]:
        """Get unique torrent IDs added this session."""
        return list(set(self._torrent_ids.values()))

    def get_torrent_info(self, torrent_id: str) -> Optional[dict]:
        """Get info about a torrent on Real-Debrid."""
        try:
            resp = self.session.get(
                f"{self.API_BASE}/torrents/info/{torrent_id}",
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"  ✗ Error getting torrent info: {e}")
            return None

    def unrestrict_link(self, link: str) -> Optional[dict]:
        """Unrestrict a link to get a direct download URL.

        Returns dict with 'download' (URL) and 'filename' keys, or None.
        """
        try:
            resp = self.session.post(
                f"{self.API_BASE}/unrestrict/link",
                data={"link": link},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            print(f"  ✗ Error unrestricting link: {e}")
            return None


# ─── Magnet File Saver (fallback) ──────────────────────────────────────────────


class MagnetFileSaver:
    """Saves magnet links to .magnet files for manual importing."""

    def __init__(self, output_dir: str = "magnets"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    def save(self, stream: TorrentStream, series_name: str, episode_label: str) -> str:
        """Save a magnet link to a file. Returns the file path."""
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in series_name)
        filename = f"{safe_name}_{episode_label}.magnet"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w") as f:
            f.write(stream.magnet_link)

        return filepath


# ─── aria2 Client ──────────────────────────────────────────────────────────────


class Aria2Client:
    """Manages downloads via aria2 JSON-RPC interface.

    aria2 runs on the NAS and downloads Real-Debrid direct links to the
    media library so Jellyfin can pick them up automatically.
    """

    def __init__(
        self,
        host: str = "http://localhost",
        port: int = 6800,
        secret: str = "",
        download_dir: str = "",
    ):
        self.rpc_url = f"{host}:{port}/jsonrpc"
        self.secret = secret
        self.download_dir = download_dir
        self.session = requests.Session()

    def _call(self, method: str, params: Optional[list] = None):
        """Send a JSON-RPC call to aria2."""
        if params is None:
            params = []
        if self.secret:
            params = [f"token:{self.secret}"] + params

        payload = {
            "jsonrpc": "2.0",
            "id": "torrentdownloader",
            "method": method,
            "params": params,
        }
        resp = self.session.post(self.rpc_url, json=payload, timeout=10)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(result["error"].get("message", "Unknown aria2 error"))
        return result.get("result")

    def test_connection(self) -> bool:
        """Test if aria2 RPC is reachable."""
        try:
            version = self._call("aria2.getVersion")
            if version:
                print(f"  ✓ aria2 connected (v{version.get('version', '?')})")
                return True
            return False
        except Exception as e:
            print(f"  ✗ Cannot connect to aria2: {e}")
            return False

    def add_download(
        self,
        url: str,
        directory: str = "",
        filename: str = "",
    ) -> Optional[str]:
        """Add a download URL to aria2. Returns the GID or None."""
        options: dict[str, str] = {}
        target_dir = directory or self.download_dir
        if target_dir:
            options["dir"] = target_dir
        if filename:
            options["out"] = filename

        try:
            gid = self._call("aria2.addUri", [[url], options])
            return gid
        except Exception as e:
            print(f"  ✗ Error adding to aria2: {e}")
            return None
