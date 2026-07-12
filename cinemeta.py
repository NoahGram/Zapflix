"""
Cinemeta client - fetches series and movie metadata from Stremio's Cinemeta addon.
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
    released: str = ""  # ISO timestamp from Cinemeta; "" = unknown (assume aired)

    @property
    def label(self) -> str:
        return f"S{self.season:02d}E{self.episode:02d}"

    def __str__(self) -> str:
        return f"{self.label} - {self.name}"


@dataclass
class Movie:
    imdb_id: str
    name: str
    year: str
    description: str = ""
    runtime: str = ""
    genres: list[str] = None

    def __post_init__(self):
        if self.genres is None:
            self.genres = []

    def __str__(self) -> str:
        return f"{self.name} ({self.year})"


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
    """Search for a series by name. Returns list of {imdb_id, name, year, type}."""
    url = f"{CINEMETA_BASE}/catalog/series/top/search={requests.utils.quote(query)}.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    results = []
    for meta in data.get("metas", []):
        results.append(
            {
                "imdb_id": meta.get("imdb_id") or meta.get("id"),
                "name": meta.get("name", "Unknown"),
                "year": meta.get("releaseInfo", meta.get("year", "?")),
                "type": "series",
                "poster": meta.get("poster", ""),
                "background": meta.get("background", ""),
            }
        )
    return results


def search_movies(query: str) -> list[dict]:
    """Search for a movie by name. Returns list of {imdb_id, name, year, type}."""
    url = f"{CINEMETA_BASE}/catalog/movie/top/search={requests.utils.quote(query)}.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    results = []
    for meta in data.get("metas", []):
        results.append(
            {
                "imdb_id": meta.get("imdb_id") or meta.get("id"),
                "name": meta.get("name", "Unknown"),
                "year": meta.get("releaseInfo", meta.get("year", "?")),
                "type": "movie",
                "poster": meta.get("poster", ""),
                "background": meta.get("background", ""),
            }
        )
    return results


def search_all(query: str) -> list[dict]:
    """Search both movies and series. Returns combined results sorted by relevance."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_movies = pool.submit(search_movies, query)
        future_series = pool.submit(search_series, query)
        movies = future_movies.result()
        series = future_series.result()

    # Interleave results: movies first (since user likely wants movies if typing a title),
    # then series. Deduplicate by IMDB ID.
    seen = set()
    combined = []
    # Alternate: movie, series, movie, series... to give balanced results
    mi, si = 0, 0
    while mi < len(movies) or si < len(series):
        if mi < len(movies):
            m = movies[mi]
            mi += 1
            if m["imdb_id"] not in seen:
                seen.add(m["imdb_id"])
                combined.append(m)
        if si < len(series):
            s = series[si]
            si += 1
            if s["imdb_id"] not in seen:
                seen.add(s["imdb_id"])
                combined.append(s)
    return combined


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
            released=video.get("released", "") or "",
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


def get_meta(content_type: str, imdb_id: str) -> Optional[dict]:
    """Fetch rich display metadata for the detail view.

    Returns a JSON-friendly dict with poster/background/plot/rating/genres and,
    for series, the season -> episode structure. Returns None on failure.
    """
    ctype = "series" if content_type == "series" else "movie"
    url = f"{CINEMETA_BASE}/meta/{ctype}/{imdb_id}.json"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        meta = resp.json().get("meta", {})
    except Exception:
        return None

    result = {
        "imdb_id": imdb_id,
        "type": ctype,
        "name": meta.get("name", "Unknown"),
        "year": meta.get("releaseInfo", meta.get("year", "?")),
        "poster": meta.get("poster", ""),
        "background": meta.get("background", ""),
        "description": meta.get("description", ""),
        "rating": meta.get("imdbRating", ""),
        "genres": meta.get("genres") or meta.get("genre") or [],
        "runtime": meta.get("runtime", ""),
    }

    if ctype == "series":
        seasons: dict[int, list[dict]] = {}
        for video in meta.get("videos", []):
            season = video.get("season")
            episode_num = video.get("episode") or video.get("number")
            if season is None or episode_num is None or season == 0:
                continue
            seasons.setdefault(int(season), []).append({
                "season": int(season),
                "episode": int(episode_num),
                "name": video.get("name") or video.get("title", f"Episode {episode_num}"),
                "overview": video.get("overview", ""),
            })
        for s in seasons:
            seasons[s].sort(key=lambda e: e["episode"])
        # Emit as a sorted list of {season, episodes:[...]}
        result["seasons"] = [
            {"season": s, "episodes": seasons[s]} for s in sorted(seasons.keys())
        ]

    return result


def get_movie_metadata(imdb_id: str) -> Movie:
    """Fetch movie metadata."""
    url = f"{CINEMETA_BASE}/meta/movie/{imdb_id}.json"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    meta = data.get("meta", {})
    return Movie(
        imdb_id=imdb_id,
        name=meta.get("name", "Unknown"),
        year=meta.get("releaseInfo", meta.get("year", "?")),
        description=meta.get("description", ""),
        runtime=meta.get("runtime", ""),
        genres=meta.get("genres", []),
    )
