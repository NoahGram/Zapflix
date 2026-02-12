"""
Quality Profiles - predefined and custom media quality profiles for optimal downloads.

Each profile defines preferred codecs, resolution, audio, size limits, and scoring
weights tailored for specific use cases (e.g., NAS storage efficiency, maximum
quality, mobile devices, etc.).

Profiles are used by the Torrentio stream scorer to rank and auto-select the best
available stream for each episode.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QualityProfile:
    """Defines quality preferences and constraints for stream selection."""

    name: str
    description: str

    # Resolution preferences (in priority order)
    preferred_resolution: list[str] = field(default_factory=lambda: ["1080p"])
    accept_resolution: list[str] = field(default_factory=lambda: ["1080p", "720p"])

    # Codec preferences (in priority order)
    preferred_codecs: list[str] = field(default_factory=lambda: ["x265", "hevc"])
    accept_codecs: list[str] = field(
        default_factory=lambda: ["x265", "hevc", "x264", "h264", "h.264", "h.265"]
    )

    # Audio preferences (in priority order)
    preferred_audio: list[str] = field(default_factory=lambda: ["aac", "ac3", "eac3"])
    accept_audio: list[str] = field(
        default_factory=lambda: ["aac", "ac3", "eac3", "dd5.1", "dd2.0", "dts"]
    )

    # Release type preferences
    preferred_release: list[str] = field(
        default_factory=lambda: ["web-dl", "webdl", "webrip", "bluray"]
    )

    # Size constraints (in GB, per episode for series)
    min_size_gb: float = 0.1
    max_size_gb: float = 15.0
    ideal_size_gb: tuple[float, float] = (0.3, 4.0)  # (min_ideal, max_ideal)

    # Hard exclusions — streams matching these are never picked
    exclude_keywords: list[str] = field(
        default_factory=lambda: [
            "cam", "hdcam", "telesync", "telecine", "ts", "hdts",
            "screener", "dvdscr",
        ]
    )

    # Minimum seeders required
    min_seeders: int = 1

    # Scoring weights (how much each factor matters)
    weight_resolution: float = 40.0
    weight_codec: float = 25.0
    weight_audio: float = 10.0
    weight_release: float = 10.0
    weight_seeders: float = 10.0
    weight_size: float = 5.0

    def score_stream(self, title: str, quality: str, seeders: int, size_str: str) -> float:
        """Score a stream against this profile. Higher = better match."""
        title_lower = title.lower()
        score = 0.0

        # ── Hard exclusions ──
        for kw in self.exclude_keywords:
            if kw in title_lower:
                return -1.0

        # ── Resolution score ──
        if quality in self.preferred_resolution:
            idx = self.preferred_resolution.index(quality)
            score += self.weight_resolution * (1.0 - idx * 0.2)
        elif quality in self.accept_resolution:
            score += self.weight_resolution * 0.3
        # else: 0 points for resolution

        # ── Codec score ──
        codec_found = False
        for i, codec in enumerate(self.preferred_codecs):
            if codec in title_lower:
                score += self.weight_codec * (1.0 - i * 0.15)
                codec_found = True
                break
        if not codec_found:
            for codec in self.accept_codecs:
                if codec in title_lower:
                    score += self.weight_codec * 0.3
                    break

        # ── Audio score ──
        audio_found = False
        for i, audio in enumerate(self.preferred_audio):
            if audio in title_lower:
                score += self.weight_audio * (1.0 - i * 0.15)
                audio_found = True
                break
        if not audio_found:
            for audio in self.accept_audio:
                if audio in title_lower:
                    score += self.weight_audio * 0.3
                    break

        # ── Release type score ──
        for i, rel in enumerate(self.preferred_release):
            if rel in title_lower:
                score += self.weight_release * (1.0 - i * 0.15)
                break

        # ── Seeders score (logarithmic, capped) ──
        if seeders >= self.min_seeders:
            import math
            seeder_score = min(math.log10(max(seeders, 1) + 1) / 3.0, 1.0)
            score += self.weight_seeders * seeder_score
        else:
            score -= 20.0  # Penalty for too few seeders

        # ── Size score ──
        size_gb = _parse_size_to_gb(size_str)
        if size_gb is not None:
            if size_gb < self.min_size_gb or size_gb > self.max_size_gb:
                score -= 15.0  # Outside acceptable range
            elif self.ideal_size_gb[0] <= size_gb <= self.ideal_size_gb[1]:
                score += self.weight_size  # Perfect range
            else:
                # Acceptable but not ideal
                score += self.weight_size * 0.5

        return score


def _parse_size_to_gb(size_str: str) -> Optional[float]:
    """Parse a size string like '1.5 GB', '300 MB' to GB."""
    if not size_str or size_str == "?":
        return None
    match = re.match(r"([\d.]+)\s*(GB|MB|TB|KB)", size_str.strip(), re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit == "TB":
        return value * 1024
    elif unit == "GB":
        return value
    elif unit == "MB":
        return value / 1024
    elif unit == "KB":
        return value / (1024 * 1024)
    return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Built-in Profiles
# ═══════════════════════════════════════════════════════════════════════════════

PROFILES: dict[str, QualityProfile] = {}


def _register(p: QualityProfile):
    PROFILES[p.name] = p
    return p


# ── Balanced (default) — best for NAS + Jellyfin, optimized for DXP2800/N100 ──
_register(QualityProfile(
    name="balanced",
    description="1080p x265 — best balance of quality, size, and NAS compatibility",
    preferred_resolution=["1080p"],
    accept_resolution=["1080p", "720p"],
    preferred_codecs=["x265", "hevc"],
    accept_codecs=["x265", "hevc", "x264", "h264", "h.264", "h.265", "av1"],
    preferred_audio=["aac", "ac3", "eac3"],
    accept_audio=["aac", "ac3", "eac3", "dd5.1", "dd2.0", "dts"],
    preferred_release=["web-dl", "webdl", "webrip", "bluray"],
    min_size_gb=0.1,
    max_size_gb=8.0,
    ideal_size_gb=(0.3, 3.0),
    min_seeders=2,
))

# ── Quality — maximum visual/audio quality within reason ──
_register(QualityProfile(
    name="quality",
    description="4K/1080p best quality — larger files, best picture",
    preferred_resolution=["2160p", "1080p"],
    accept_resolution=["2160p", "1080p", "720p"],
    preferred_codecs=["x265", "hevc"],
    accept_codecs=["x265", "hevc", "av1", "x264", "h264"],
    preferred_audio=["eac3", "dts", "ac3", "aac"],
    accept_audio=["eac3", "dts", "ac3", "truehd", "atmos", "aac", "dd5.1"],
    preferred_release=["bluray", "web-dl", "webdl", "remux"],
    min_size_gb=0.5,
    max_size_gb=25.0,
    ideal_size_gb=(2.0, 12.0),
    min_seeders=2,
    weight_resolution=45.0,
    weight_codec=20.0,
    weight_audio=15.0,
    weight_size=5.0,
))

# ── Compact — small files, save storage ──
_register(QualityProfile(
    name="compact",
    description="720p/1080p x265 — smallest files, saves NAS storage",
    preferred_resolution=["720p", "1080p"],
    accept_resolution=["720p", "1080p", "480p"],
    preferred_codecs=["x265", "hevc", "av1"],
    accept_codecs=["x265", "hevc", "av1", "x264", "h264"],
    preferred_audio=["aac", "ac3"],
    accept_audio=["aac", "ac3", "dd2.0", "eac3"],
    preferred_release=["web-dl", "webdl", "webrip"],
    min_size_gb=0.05,
    max_size_gb=3.0,
    ideal_size_gb=(0.1, 1.0),
    min_seeders=1,
    weight_resolution=20.0,
    weight_codec=20.0,
    weight_size=30.0,  # Size matters most
    weight_seeders=15.0,
))

# ── 4K — for high-end setups with HDR displays ──
_register(QualityProfile(
    name="4k",
    description="4K x265 HDR — premium quality, needs storage + HDR display",
    preferred_resolution=["2160p"],
    accept_resolution=["2160p", "1080p"],
    preferred_codecs=["x265", "hevc"],
    accept_codecs=["x265", "hevc", "av1"],
    preferred_audio=["eac3", "dts", "truehd", "atmos"],
    accept_audio=["eac3", "dts", "truehd", "atmos", "ac3", "aac"],
    preferred_release=["web-dl", "webdl", "bluray", "remux"],
    min_size_gb=1.0,
    max_size_gb=50.0,
    ideal_size_gb=(4.0, 20.0),
    min_seeders=2,
    weight_resolution=50.0,
    weight_codec=15.0,
    weight_audio=15.0,
))

# ── Jellyfin-optimized — direct play friendly for Intel QSV NAS ──
_register(QualityProfile(
    name="jellyfin",
    description="Direct-play optimized for Jellyfin on Intel N100 NAS (HEVC/AV1 + AAC)",
    preferred_resolution=["1080p"],
    accept_resolution=["1080p", "720p"],
    preferred_codecs=["x265", "hevc", "av1"],  # N100 HW decodes all of these
    accept_codecs=["x265", "hevc", "av1", "x264", "h264"],
    preferred_audio=["aac", "ac3", "eac3"],  # Direct play audio, no transcode
    accept_audio=["aac", "ac3", "eac3", "dd5.1", "dd2.0"],
    # Explicitly avoid DTS/TrueHD which may force audio transcode
    exclude_keywords=[
        "cam", "hdcam", "telesync", "telecine", "ts", "hdts",
        "screener", "dvdscr",
        "dts-hd", "truehd", "atmos",  # These often force audio transcoding
    ],
    preferred_release=["web-dl", "webdl", "webrip", "bluray"],
    min_size_gb=0.1,
    max_size_gb=6.0,
    ideal_size_gb=(0.3, 2.5),
    min_seeders=2,
    weight_resolution=35.0,
    weight_codec=25.0,
    weight_audio=15.0,  # Audio compatibility matters more for direct play
    weight_release=10.0,
    weight_seeders=10.0,
    weight_size=5.0,
))


def get_profile(name: str) -> QualityProfile:
    """Get a profile by name. Raises KeyError if not found."""
    if name not in PROFILES:
        available = ", ".join(PROFILES.keys())
        raise KeyError(f"Unknown profile '{name}'. Available: {available}")
    return PROFILES[name]


def list_profiles() -> list[tuple[str, str]]:
    """Return list of (name, description) for all profiles."""
    return [(name, p.description) for name, p in PROFILES.items()]


def load_custom_profile(data: dict) -> QualityProfile:
    """Load a custom profile from a config dict."""
    return QualityProfile(
        name=data.get("name", "custom"),
        description=data.get("description", "Custom profile"),
        preferred_resolution=data.get("preferred_resolution", ["1080p"]),
        accept_resolution=data.get("accept_resolution", ["1080p", "720p"]),
        preferred_codecs=data.get("preferred_codecs", ["x265", "hevc"]),
        accept_codecs=data.get("accept_codecs", ["x265", "hevc", "x264"]),
        preferred_audio=data.get("preferred_audio", ["aac", "ac3"]),
        accept_audio=data.get("accept_audio", ["aac", "ac3", "eac3"]),
        preferred_release=data.get("preferred_release", ["web-dl", "webrip", "bluray"]),
        min_size_gb=data.get("min_size_gb", 0.1),
        max_size_gb=data.get("max_size_gb", 15.0),
        ideal_size_gb=tuple(data.get("ideal_size_gb", [0.3, 4.0])),
        exclude_keywords=data.get("exclude_keywords", [
            "cam", "hdcam", "telesync", "telecine",
        ]),
        min_seeders=data.get("min_seeders", 1),
        weight_resolution=data.get("weight_resolution", 40.0),
        weight_codec=data.get("weight_codec", 25.0),
        weight_audio=data.get("weight_audio", 10.0),
        weight_release=data.get("weight_release", 10.0),
        weight_seeders=data.get("weight_seeders", 10.0),
        weight_size=data.get("weight_size", 5.0),
    )
