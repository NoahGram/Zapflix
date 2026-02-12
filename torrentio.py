"""
Torrentio client - fetches torrent streams from the Torrentio Stremio addon.
"""

import re
import time
import requests
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from cinemeta import Episode

if TYPE_CHECKING:
    from profiles import QualityProfile


@dataclass
class TorrentStream:
    """Represents a single torrent stream result from Torrentio."""

    title: str
    info_hash: str
    file_idx: Optional[int]
    seeders: int
    size: str
    source: str  # tracker/indexer name
    quality: str  # extracted quality (1080p, 720p, etc.)

    @property
    def magnet_link(self) -> str:
        trackers = [
            "udp://tracker.opentrackr.org:1337/announce",
            "udp://open.stealth.si:80/announce",
            "udp://tracker.torrent.eu.org:451/announce",
            "udp://tracker.bittor.pw:1337/announce",
            "udp://public.popcorn-tracker.org:6969/announce",
            "udp://tracker.dler.org:6969/announce",
            "udp://exodus.desync.com:6969",
            "udp://open.demonii.com:1337/announce",
        ]
        tracker_params = "&".join(f"tr={t}" for t in trackers)
        name = requests.utils.quote(self.title.split("\n")[0])
        return f"magnet:?xt=urn:btih:{self.info_hash}&dn={name}&{tracker_params}"

    def __str__(self) -> str:
        return f"[{self.quality}] {self.source} | {self.size} | {self.seeders} seeders"


def _extract_quality(title: str) -> str:
    """Extract quality tag from stream title."""
    title_lower = title.lower()
    for q in ["2160p", "4k", "1080p", "720p", "480p", "360p"]:
        if q in title_lower:
            return "2160p" if q == "4k" else q
    return "unknown"


def _extract_size(title: str) -> str:
    """Extract file size from stream title."""
    match = re.search(r"💾\s*([\d.]+\s*[GKMT]B)", title, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"([\d.]+\s*[GKMT]B)", title, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "?"


def _extract_seeders(title: str) -> int:
    """Extract seeder count from stream title."""
    match = re.search(r"👤\s*(\d+)", title)
    if match:
        return int(match.group(1))
    return 0


def _extract_source(title: str) -> str:
    """Extract the source/tracker name (first line of title)."""
    lines = title.strip().split("\n")
    if lines:
        # First line usually has the source like "[Torrentio] tracker⚡"
        source = lines[0].strip()
        # Clean up emoji and formatting
        source = re.sub(r"[⚡🔒💾👤]", "", source).strip()
        return source if source else "Unknown"
    return "Unknown"


class TorrentioClient:
    """Client for querying the Torrentio Stremio addon API."""

    def __init__(
        self,
        base_url: str = "https://torrentio.strem.fun",
        config: str = "",
        preferred_quality: Optional[list[str]] = None,
        exclude_keywords: Optional[list[str]] = None,
        max_results: int = 5,
    ):
        self.base_url = base_url.rstrip("/")
        self.config = config.strip("/") if config else ""
        self.preferred_quality = preferred_quality or ["1080p", "720p"]
        self.exclude_keywords = [k.lower() for k in (exclude_keywords or [])]
        self.max_results = max_results
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "TorrentDownloader/1.0",
                "Accept": "application/json",
            }
        )

    def _build_url(self, imdb_id: str, season: int, episode: int) -> str:
        """Build the Torrentio stream API URL for a series episode."""
        video_id = f"{imdb_id}:{season}:{episode}"
        if self.config:
            return f"{self.base_url}/{self.config}/stream/series/{video_id}.json"
        return f"{self.base_url}/stream/series/{video_id}.json"

    def _build_movie_url(self, imdb_id: str) -> str:
        """Build the Torrentio stream API URL for a movie."""
        if self.config:
            return f"{self.base_url}/{self.config}/stream/movie/{imdb_id}.json"
        return f"{self.base_url}/stream/movie/{imdb_id}.json"

    def get_streams(self, episode: Episode) -> list[TorrentStream]:
        """Fetch available torrent streams for an episode."""
        url = self._build_url(episode.imdb_id, episode.season, episode.episode)

        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠ Failed to fetch streams for {episode.label}: {e}")
            return []

        streams = []
        for s in data.get("streams", []):
            title = s.get("title", s.get("name", ""))
            info_hash = None

            # Extract info_hash from various possible fields
            if "infoHash" in s:
                info_hash = s["infoHash"]
            elif "url" in s:
                # Sometimes it's in a magnet URL
                match = re.search(r"btih:([a-fA-F0-9]{40})", s["url"])
                if match:
                    info_hash = match.group(1)

            if not info_hash:
                continue

            quality = _extract_quality(title)
            stream = TorrentStream(
                title=title,
                info_hash=info_hash.lower(),
                file_idx=s.get("fileIdx"),
                seeders=_extract_seeders(title),
                size=_extract_size(title),
                source=_extract_source(title),
                quality=quality,
            )

            # Filter out excluded keywords
            title_lower = title.lower()
            if any(kw in title_lower for kw in self.exclude_keywords):
                continue

            streams.append(stream)

        return streams

    def get_movie_streams(self, imdb_id: str) -> list[TorrentStream]:
        """Fetch available torrent streams for a movie."""
        url = self._build_movie_url(imdb_id)

        try:
            resp = self.session.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"  ⚠ Failed to fetch streams: {e}")
            return []

        streams = []
        for s in data.get("streams", []):
            title = s.get("title", s.get("name", ""))
            info_hash = None

            if "infoHash" in s:
                info_hash = s["infoHash"]
            elif "url" in s:
                match = re.search(r"btih:([a-fA-F0-9]{40})", s["url"])
                if match:
                    info_hash = match.group(1)

            if not info_hash:
                continue

            quality = _extract_quality(title)
            stream = TorrentStream(
                title=title,
                info_hash=info_hash.lower(),
                file_idx=s.get("fileIdx"),
                seeders=_extract_seeders(title),
                size=_extract_size(title),
                source=_extract_source(title),
                quality=quality,
            )

            title_lower = title.lower()
            if any(kw in title_lower for kw in self.exclude_keywords):
                continue

            streams.append(stream)

        return streams

    def select_best_stream(self, streams: list[TorrentStream],
                           profile: Optional["QualityProfile"] = None,
                           ) -> Optional[TorrentStream]:
        """Select the best stream.

        If a profile is provided, uses the profile's multi-factor scoring.
        Otherwise falls back to simple quality + seeders ranking.
        """
        if not streams:
            return None

        if profile is not None:
            scored = []
            for s in streams:
                sc = profile.score_stream(s.title, s.quality, s.seeders, s.size)
                if sc >= 0:  # -1 means hard-excluded
                    scored.append((sc, s))
            if not scored:
                return None
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[0][1]

        # Fallback: simple quality + seeders
        def score(s: TorrentStream) -> tuple:
            quality_score = 0
            for i, q in enumerate(self.preferred_quality):
                if s.quality == q:
                    quality_score = len(self.preferred_quality) - i
                    break
            return (quality_score, s.seeders)

        streams_sorted = sorted(streams, key=score, reverse=True)
        return streams_sorted[0]

    def get_streams_batch(
        self, episodes: list[Episode], delay: float = 2.0
    ) -> dict[str, list[TorrentStream]]:
        """Fetch streams for multiple episodes with rate limiting."""
        results = {}
        for i, ep in enumerate(episodes):
            results[ep.label] = self.get_streams(ep)
            if i < len(episodes) - 1:
                time.sleep(delay)
        return results
