"""
Download clients - handles sending torrents to Real-Debrid and forwarding the
resulting direct links to aria2 on the NAS.

qBittorrent support was removed — the workflow is Real-Debrid → aria2 only.
"""

import os
import time
import requests
from enum import Enum
from typing import Optional

from torrentio import TorrentStream


class AddResult(Enum):
    """Result of attempting to add a torrent."""
    SUCCESS = "success"          # Newly added
    ALREADY_EXISTS = "exists"    # Same hash already added this session (season pack)
    FAILED = "failed"            # Actual failure


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

    def torrent_id(self, info_hash: str) -> Optional[str]:
        """RD torrent id for a hash added this session (for the delivery journal)."""
        return self._torrent_ids.get(info_hash.lower())

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
            "id": "zapflix",
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
        retries: int = 3,
    ) -> Optional[str]:
        """Add a download URL to aria2 with retry/backoff. Returns the GID or None."""
        # Overwrite on re-download (retries, monitor re-grabs) instead of
        # aria2's default of creating "name.1.mkv" duplicates.
        options: dict[str, str] = {
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
        }
        target_dir = directory or self.download_dir
        if target_dir:
            options["dir"] = target_dir
        if filename:
            options["out"] = filename

        last_err = None
        for attempt in range(1, retries + 1):
            try:
                gid = self._call("aria2.addUri", [[url], options])
                if gid:
                    return gid
            except Exception as e:
                last_err = e
                if attempt < retries:
                    time.sleep(2 ** attempt)  # 2s, 4s, 8s
        print(f"  ✗ Error adding to aria2 after {retries} attempts: {last_err}")
        return None

    def tell_status(self, gid: str) -> Optional[dict]:
        """Return aria2 status for a GID (status, errorMessage, files...)."""
        try:
            return self._call(
                "aria2.tellStatus",
                [gid, ["gid", "status", "errorCode", "errorMessage",
                       "completedLength", "totalLength", "files"]],
            )
        except Exception as e:
            print(f"  ✗ Error getting aria2 status: {e}")
            return None

    def list_downloads(self, max_stopped: int = 40) -> list[dict]:
        """List active, waiting, and recently finished downloads.

        Feeds the web UI's Downloads panel. Returns [] if aria2 is unreachable.
        """
        keys = ["gid", "status", "completedLength", "totalLength",
                "downloadSpeed", "errorMessage", "files"]
        items: list[dict] = []
        try:
            items += self._call("aria2.tellActive", [keys]) or []
            items += self._call("aria2.tellWaiting", [0, 200, keys]) or []
            items += self._call("aria2.tellStopped", [0, max_stopped, keys]) or []
        except Exception as e:
            print(f"  ✗ Error listing aria2 downloads: {e}")
        return items
