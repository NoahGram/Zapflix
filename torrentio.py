"""
Torrentio client - fetches torrent streams from the Torrentio Stremio addon.

When Torrentio is configured with a Real-Debrid API key (via the `config`
string, generated at torrentio.strem.fun/configure), each stream is annotated
with a cache marker:
    [RD+]        -> already cached on Real-Debrid (instant, reliable)
    [RD download] -> not cached; RD would have to download it first

Real-Debrid killed its own /instantAvailability endpoint (Nov 2024), so this
marker is now the only reliable way to know what RD has cached. We parse it into
`TorrentStream.cached` and strongly prefer cached streams during selection.
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
    cached: bool = False  # True when Torrentio marks it [RD+] (cached on RD)
    filename: str = ""  # real filename from behaviorHints (better than title)
    resolve_url: str = ""  # Torrentio RD resolve URL (redirects to a direct link)

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
        tag = "RD+ " if self.cached else ""
        return f"[{tag}{self.quality}] {self.source} | {self.size} | {self.seeders} seeders"


def _extract_quality(text: str) -> str:
    """Extract quality tag from a stream name/title/filename."""
    text_lower = text.lower()
    for q in ["2160p", "4k", "1080p", "720p", "480p", "360p"]:
        if q in text_lower:
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


def _is_cached(name: str, title: str) -> bool:
    """Detect the Torrentio [RD+] cache marker."""
    blob = f"{name} {title}".lower()
    # "[rd+]" means cached; "[rd download]" means NOT cached.
    return "rd+" in blob or "[rd+]" in blob


def _parse_stream(s: dict, exclude_keywords: list[str]) -> Optional["TorrentStream"]:
    """Parse a single Torrentio stream dict into a TorrentStream, or None."""
    name = s.get("name", "")
    title = s.get("title", name)

    # When Torrentio is configured with a debrid key it returns a `url`
    # (a /resolve/... redirect to a direct link) and NO infoHash. Otherwise
    # it returns infoHash directly. Support both shapes.
    resolve_url = ""
    info_hash = None
    if "infoHash" in s:
        info_hash = s["infoHash"]
    elif "url" in s:
        url = s["url"]
        if "/resolve/" in url:
            resolve_url = url
        match = re.search(r"btih:([a-fA-F0-9]{40})", url)
        if not match:
            # Resolve URLs carry the info hash as a 40-hex path segment.
            match = re.search(r"/([a-fA-F0-9]{40})(?:/|$)", url)
        if match:
            info_hash = match.group(1)
    if not info_hash:
        return None

    behavior = s.get("behaviorHints") or {}
    filename = behavior.get("filename", "") or ""

    # Quality: name is cleanest ("1080p", "4k HDR"), then filename, then title.
    quality = _extract_quality(name)
    if quality == "unknown":
        quality = _extract_quality(filename or title)

    stream = TorrentStream(
        title=title,
        info_hash=info_hash.lower(),
        file_idx=s.get("fileIdx"),
        seeders=_extract_seeders(title),
        size=_extract_size(title),
        source=_extract_source(title),
        quality=quality,
        cached=_is_cached(name, title),
        filename=filename,
        resolve_url=resolve_url,
    )

    # Filter out excluded keywords (check title + filename)
    haystack = f"{title} {filename}".lower()
    if any(kw in haystack for kw in exclude_keywords):
        return None

    return stream


class TorrentioClient:
    """Client for querying the Torrentio Stremio addon API."""

    def __init__(
        self,
        base_url: str = "https://torrentio.strem.fun",
        config: str = "",
        preferred_quality: Optional[list[str]] = None,
        exclude_keywords: Optional[list[str]] = None,
        max_results: int = 5,
        max_retries: int = 3,
    ):
        self.base_url = base_url.rstrip("/")
        self.config = config.strip("/") if config else ""
        self.preferred_quality = preferred_quality or ["1080p", "720p"]
        self.exclude_keywords = [k.lower() for k in (exclude_keywords or [])]
        self.max_results = max_results
        self.max_retries = max_retries
        self.session = requests.Session()
        # A browser-like User-Agent is required: Cloudflare 403s non-browser
        # UAs on Real-Debrid-configured requests (a common cause of failures).
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json",
            }
        )

    @property
    def rd_configured(self) -> bool:
        """True when a Real-Debrid key is baked into the Torrentio config."""
        return "realdebrid=" in self.config.lower()

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

    def _fetch(self, url: str, label: str = "") -> list[dict]:
        """GET a Torrentio stream URL with retry/backoff. Returns raw streams."""
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                # Retry on rate-limit / transient server errors
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.RequestException(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json().get("streams", [])
            except (requests.RequestException, ValueError) as e:
                if attempt >= self.max_retries:
                    print(f"  ⚠ Failed to fetch streams{f' for {label}' if label else ''}: {e}")
                    return []
                # Exponential backoff: 2s, 4s, 8s...
                time.sleep(2 ** attempt)
        return []

    def get_streams(self, episode: Episode) -> list[TorrentStream]:
        """Fetch available torrent streams for an episode."""
        url = self._build_url(episode.imdb_id, episode.season, episode.episode)
        raw = self._fetch(url, label=episode.label)
        streams = []
        for s in raw:
            parsed = _parse_stream(s, self.exclude_keywords)
            if parsed:
                streams.append(parsed)
        return streams

    def get_movie_streams(self, imdb_id: str) -> list[TorrentStream]:
        """Fetch available torrent streams for a movie."""
        url = self._build_movie_url(imdb_id)
        raw = self._fetch(url, label=imdb_id)
        streams = []
        for s in raw:
            parsed = _parse_stream(s, self.exclude_keywords)
            if parsed:
                streams.append(parsed)
        return streams

    def select_best_stream(self, streams: list[TorrentStream],
                           profile: Optional["QualityProfile"] = None,
                           ) -> Optional[TorrentStream]:
        """Select the best stream.

        Cached ([RD+]) streams are strongly preferred: any cached stream that
        passes the profile's hard exclusions beats every uncached one. Among
        streams of the same cache status, the profile's multi-factor score (or a
        simple quality+seeders fallback) decides. This gives "prefer cached,
        fall back to best" — reliable instant downloads when RD has the content,
        and the best available torrent otherwise.
        """
        if not streams:
            return None

        if profile is not None:
            scored = []
            for s in streams:
                sc = profile.score_stream(s.title, s.quality, s.seeders, s.size)
                if sc >= 0:  # -1 means hard-excluded
                    scored.append((s.cached, sc, s))
            if not scored:
                return None
            # Sort by (cached first, then score) — cached always wins.
            scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
            return scored[0][2]

        # Fallback: simple cached + quality + seeders
        def score(s: TorrentStream) -> tuple:
            quality_score = 0
            for i, q in enumerate(self.preferred_quality):
                if s.quality == q:
                    quality_score = len(self.preferred_quality) - i
                    break
            return (s.cached, quality_score, s.seeders)

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
