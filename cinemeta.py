"""
Cinemeta client - fetches series metadata (seasons, episodes) from Stremio's Cinemeta addon.
"""

import requests
from typing import Optional
from dataclasses import dataclass


CINEMETA_BASE = "https://v3-cinemeta.strem.io"


@dataclass
class Episode:
    season: int
    episode: int
    name: str
    imdb_id: str  # series IMDB ID (used for torrentio queries)
    overview: str = ""

    @property
    def label(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    def __str__(self) -> str:
        return f"{self.label} - {self.name}"


@dataclass
class Series:
    imdb_id: str
    name: str
    year: str
    seasons: dict[int, list[Episode]]  # season_number -> episodes
    total_episodes: int

    def get_episodes(
        self,
        season_from: Optional[int] = None,
        season_to: Optional[int] = None,
        episode_from: Optional[int] = None,
        episode_to: Optional[int] = None,
    ) -> list[Episode]:
        """Get filtered list of episodes based on season/episode ranges."""
        episodes = []
        for season_num in sorted(self.seasons.keys()):
            if season_from is not None and season_num < season_from:
                continue
            if season_to is not None and season_num > season_to:
                continue
            for ep in self.seasons[season_num]:
                if season_from is not None and season_num == season_from:
                    if episode_from is not None and ep.episode < episode_from:
                        continue
                if season_to is not None and season_num == season_to:
                    if episode_to is not None and ep.episode > episode_to:
                        continue
                episodes.append(ep)
        return episodes


def search_series(query: str) -> list[dict]:
    """Search for a series by name. Returns list of {imdb_id, name, year}."""
    url = f"{CINEMETA_BASE}/catalog/series/top/search={requests.utils.quote(query)}.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    results = []
    for meta in data.get("metas", []):
        results.append(
            {
                "imdb_id": meta.get("imdb_id") or meta.get("id"),
                "name": meta.get("name", "Unknown"),
                "year": meta.get("releaseInfo", meta.get("year", "?")),
            }
        )
    return results


def get_series_metadata(imdb_id: str) -> Series:
    """Fetch full series metadata including all seasons and episodes."""
    url = f"{CINEMETA_BASE}/meta/series/{imdb_id}.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    meta = data.get("meta", {})
    name = meta.get("name", "Unknown")
    year = meta.get("releaseInfo", meta.get("year", "?"))

    seasons: dict[int, list[Episode]] = {}
    total = 0

    for video in meta.get("videos", []):
        season = video.get("season")
        episode_num = video.get("episode") or video.get("number")

        # Skip specials (season 0) and entries without proper numbering
        if season is None or episode_num is None or season == 0:
            continue

        ep = Episode(
            season=int(season),
            episode=int(episode_num),
            name=video.get("name") or video.get("title", f"Episode {episode_num}"),
            imdb_id=imdb_id,
            overview=video.get("overview", ""),
        )

        seasons.setdefault(ep.season, []).append(ep)
        total += 1

    # Sort episodes within each season
    for s in seasons:
        seasons[s].sort(key=lambda e: e.episode)

    return Series(
        imdb_id=imdb_id,
        name=name,
        year=year,
        seasons=seasons,
        total_episodes=total,
    )
