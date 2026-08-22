"""
Episode filename parsing and canonical naming.

Release groups name episode files in wildly different ways. Western scene
releases carry an explicit `S01E05`; anime releases usually carry only an
*absolute* episode number (`[Group] Show - 05 (1080p) [A1B2C3D4].mkv`) and
sometimes nothing recognisable at all. Zapflix used to require `SxxEyy` and
silently discarded everything else as "bonus content" — which threw away
entire 81-file anime season packs.

This module turns a filename (ideally the full path inside the torrent, for
directory context) into a (season, episode) pair, so files can be filed and
renamed consistently no matter which source they came from.

Every rule here was checked against ~1000 real Torrentio filenames; see
test_naming.py for the regression cases that came out of that corpus.
"""

import os
import re
from dataclasses import dataclass
from typing import Optional

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mov", ".wmv",
              ".webm", ".mpg", ".mpeg", ".m2ts", ".ogm", ".rmvb"}

# Bonus material. Checked BEFORE any number parsing — "NCOP 01" and
# "Shirtless Montage 1.0" both carry numbers that would otherwise be read as
# episode numbers and overwrite real episodes.
_EXTRA_KEYWORDS = (
    "ncop", "nced", "nc op", "nc ed", "creditless", "textless",
    "clean opening", "clean ending", "opening credits", "ending credits",
    "non-credit", "menu", "preview", "trailer", "teaser", "promo", "pv ",
    "gag reel", "blooper", "outtake", "interview", "making of", "making-of",
    "behind the scenes", "featurette", "breakdown", "deleted", "montage",
    "sneak peek", "commentary", "sponsor", "web card", "recap of", "bd cm",
)

# Spin-offs / side content bundled into a season pack. Without this, a pack
# containing "Tensura Nikki - 01" alongside the main show would map that file
# onto S01E01 and overwrite the real episode.
_SPECIAL_MARKERS = re.compile(
    r"(?i)(?<![a-z])(ova|oad|ona|specials?|extras?|bonus|nikki|shorts?|"
    r"mini[- ]?series|picture drama|recap)(?![a-z])"
)

# Noise stripped before hunting for a bare episode number. Order matters:
# bracketed groups and CRC hashes go first, then technical tags.
_NOISE_PATTERNS = (
    r"\[[0-9a-fA-F]{8}\]",                       # CRC32 hash
    r"\b\d{3,4}\s?x\s?\d{3,4}\b",                # 1280x720
    r"\b\d{3,4}[pi]\b",                          # 1080p / 480i
    r"\b[248]k\b",                               # 4K
    r"\b(?:x|h)\.?26[45]\b",                     # x265 / h.264
    r"\bhevc\b|\bavc\b|\bav1\b|\bxvid\b|\bdivx\b",
    r"\b\d{1,2}\s?bits?\b",                      # 10bit
    r"\b(?:aac|ac3|eac3|dd[p+]?|dts(?:[- ]?hd)?|truehd|atmos|flac|opus|mp3|pcm)\b",
    r"\b\d\.\d\b",                               # 5.1 / 2.0 channels
    r"\bdual[- ]?audio\b|\bmulti(?:[- ]?audio|[- ]?sub)?\b",
    r"\b(?:blu-?ray|bd(?:rip|mux|remux)?|web-?dl|web-?rip|hdtv|dvd(?:rip)?|remux|uhd|hdr\d*|dolby\s?vision|dv|sdr)\b",
    r"\b(?:repack|proper|uncensored|censored|subbed|dubbed|raw|batch|complete)\b",
    r"\b(?:dub|subs?|softsubs?|hardsubs?|audio)\b",
    r"\b(?:eng|jpn|jap|ita|spa|ger|fre|rus|por|pol|tur|nld?|multi|vf|vo|vostfr|"
    r"fr|de|es|nl|pl|tr|kor|korean|french|spanish|italian|german|russian)"
    r"(?:[- ]?subs?|[- ]?dub)?\b",
    r"\b(?:hd|fhd|sd|hq|web|tv|dlmux|dl)\b",
    r"\b(?:bs\d{1,2}|at-?x|tokyo\s?mx|nhk\w*|tvs|mbs|ytv)\b",  # JP TV stations
    r"(?:(?<=\d)|\b)v\d\b",                      # v2 release version, incl. "05v2"
    r"\b(?:19|20)\d{2}\b",                       # year
    r"\bai\s?upscale\b|\bupscaled?\b",
)
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)

# ── Explicit markers ──
_SxxEyy = re.compile(r"(?<![a-z0-9])s(\d{1,3})[\s._-]*e(\d{1,4})(?![0-9])", re.IGNORECASE)
# Lookbehind excludes letters/digits but NOT dots, so dotted scene names like
# "Teen.Wolf.4x02.117" still match. Resolutions ("1280x720") are already gone
# by the time this runs, and a leading digit is blocked by the lookbehind.
_NNxNN = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})\s?x\s?(\d{2,4})(?![0-9p])", re.IGNORECASE)
_SEASON_ONLY = re.compile(r"(?<![a-z])(?:season|series|s)\s*(\d{1,2})(?![0-9a-z])", re.IGNORECASE)
_SEASON_ORDINAL = re.compile(r"(?<![a-z])(\d{1,2})(?:st|nd|rd|th)\s+season", re.IGNORECASE)

# ── Bare episode-number candidates ──
# "END"/"FIN" often trails the number on final episodes ("- 12 END [1080p]").
_END = r"(?:\s*(?:end|fin(?:al)?|完))?"
_EP_LABELLED = re.compile(
    r"(?<![a-z])(?:ep?|episode|epis[oó]dio|folge|cap(?:[íi]tulo)?)\.?\s*[-#]?\s*"
    rf"(\d{{1,4}})(?:\.(\d))?(?![0-9])", re.IGNORECASE)
_EP_AFTER_DASH = re.compile(rf"[-–—]\s*(\d{{1,4}})(?:\.(\d))?{_END}\s*(?:$|[-–—(\[{{])", re.IGNORECASE)
_EP_BRACKETED = re.compile(r"[\[(]\s*(\d{1,4})(?:\.(\d))?\s*[\])]")
_EP_TRAILING = re.compile(rf"(?<![A-Za-z0-9])(\d{{1,4}})(?:\.(\d))?{_END}\s*$", re.IGNORECASE)
# "One Piece 001 - Yo soy Luffy.mkv" — number before the episode title.
_EP_BEFORE_DASH = re.compile(r"(?<![A-Za-z0-9])(\d{1,4})(?:\.(\d))?\s*[-–—]\s+\D")
# "01. Show Name.mkv" / "05 - Title.mkv"
_EP_LEADING = re.compile(r"^\s*(\d{1,4})(?:\.(\d))?\s*[-.\s]")

# Trailing metadata groups: "[Shichibukai]", "(wolfmax4k.com)", "[DUB]"
_TRAILING_GROUP = re.compile(r"\s*[\[({][^\[\]({})]{0,40}[\])}]\s*$")


@dataclass
class ParsedFile:
    """Outcome of parsing one file from a torrent."""
    kind: str                       # "episode" | "extra" | "unknown"
    season: Optional[int] = None
    episode: Optional[int] = None
    how: str = ""                   # which layer decided (for logs)
    note: str = ""

    @property
    def is_episode(self) -> bool:
        return self.kind == "episode" and self.season is not None and self.episode is not None


def _strip_noise(stem: str) -> str:
    """Remove technical tags and bracketed groups, keeping episode numbers."""
    # Release-group prefix: "[Anime Time] Show - 05" -> "Show - 05"
    s = re.sub(r"^\s*[\[(][^\])]{1,40}[\])]\s*", " ", stem)
    # Underscores are separators, never decimal points: "[Cleo]Show_-_01_(BD)"
    s = s.replace("_", " ")
    s = _NOISE_RE.sub(" ", s)
    # Now-empty brackets/parens left behind by noise removal
    s = re.sub(r"[\[({]\s*[\])}]", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" ._-")


def _strip_trailing_groups(text: str) -> str:
    """Drop trailing metadata groups so the episode number ends up last.

    "One Piece 001 [Shichibukai]" -> "One Piece 001"
    """
    prev = None
    while prev != text:
        prev = text
        text = _TRAILING_GROUP.sub("", text).strip(" ._-")
    return text


def _episode_candidates(cleaned: str, tail: str) -> list[tuple[int, Optional[int], str, tuple[int, int]]]:
    """(number, decimal, how, span) candidates, strongest signal first.

    Order matters: a show whose title contains a number ("86 - Eighty Six - 07")
    must resolve via the dash-delimited episode, not the title, so positional
    patterns are tried before the loose leading/before-dash fallbacks.
    """
    out = []
    sources = ((_EP_LABELLED, "labelled", cleaned),
               (_EP_AFTER_DASH, "after-dash", cleaned),
               (_EP_BRACKETED, "bracketed", cleaned),
               (_EP_TRAILING, "trailing", tail),
               (_EP_BEFORE_DASH, "before-dash", cleaned),
               (_EP_LEADING, "leading", cleaned))
    for regex, how, text in sources:
        for m in regex.finditer(text):
            num = int(m.group(1))
            if num < 1:  # "Gundam 00" is a title, not episode zero
                continue
            dec = int(m.group(2)) if m.lastindex and m.group(2) else None
            out.append((num, dec, how, m.span(1)))
    return out


def absolute_to_season_episode(absolute: int, season_counts: dict[int, int]
                               ) -> Optional[tuple[int, int]]:
    """Map an absolute episode number onto (season, episode).

    Anime releases number continuously across seasons: with 24-episode
    seasons, absolute 25 is S02E01. Specials (season 0) are excluded from the
    running total. Returns None when the number is out of range, which is the
    signal that the "absolute" reading was wrong.
    """
    if absolute < 1:
        return None
    running = 0
    for season in sorted(s for s in season_counts if s > 0):
        count = season_counts[season]
        if absolute <= running + count:
            return season, absolute - running
        running += count
    return None


def _compact_to_season_episode(num: int, season_counts: dict[int, int]
                               ) -> Optional[tuple[int, int]]:
    """Read a 3-4 digit number as concatenated season+episode.

    "514 Ozymandias.mkv" is Breaking Bad S05E14. Only accepted when the split
    lands on an episode that actually exists, so real absolute numbers
    (One Piece 382) are not mangled.
    """
    if not (100 <= num <= 9999):
        return None
    for season_digits in (1, 2):
        s, e = divmod(num, 100) if season_digits == 1 else (num // 100, num % 100)
        if season_digits == 2:
            s, e = num // 100, num % 100
        if s in season_counts and 1 <= e <= season_counts[s]:
            return s, e
    return None


def _valid_episode(season: int, episode: int, season_counts: dict[int, int]) -> bool:
    """Does this season/episode actually exist according to the metadata?

    A season the metadata does not know about counts as invalid: a file
    claiming "S02E020" of a one-season show is far more likely to be
    continuous numbering than a real second season.
    """
    if not season_counts or season == 0:
        return True
    return season in season_counts and episode <= season_counts[season]


def parse_media_filename(path: str,
                         season_counts: Optional[dict[int, int]] = None,
                         pack_seasons: Optional[set[int]] = None,
                         ) -> ParsedFile:
    """Work out which episode a file inside a torrent holds.

    path:           filename or full path inside the torrent (a path gives
                    directory context, which catches bundled spin-offs).
    season_counts:  {season: episode_count} from Cinemeta, enabling absolute
                    numbering and sanity checks.
    pack_seasons:   seasons this torrent is known to cover. A single-season
                    pack numbers its files per season ("- 05" = E05 of that
                    season); a multi-season pack numbers them absolutely.
    """
    fname = os.path.basename(path)
    stem, ext = os.path.splitext(fname)
    if ext.lower() not in VIDEO_EXTS:
        return ParsedFile("extra", note=f"not a video file ({ext or 'no extension'})")

    haystack = path.lower()
    for kw in _EXTRA_KEYWORDS:
        if kw in haystack:
            return ParsedFile("extra", note=f"bonus material ('{kw.strip()}')")

    season_counts = season_counts or {}

    # ── Layer 1: explicit SxxEyy anywhere in the path ──
    m = _SxxEyy.search(stem) or _SxxEyy.search(path)
    if m:
        s_, e_ = int(m.group(1)), int(m.group(2))
        if _valid_episode(s_, e_, season_counts):
            return ParsedFile("episode", s_, e_, how="explicit SxxEyy")
        # Releases label continuous numbering as SxxEyy too ("S02E29" of a
        # 23-episode season, "S01E0382" of One Piece). Re-read as absolute.
        mapped = absolute_to_season_episode(e_, season_counts)
        if mapped:
            return ParsedFile("episode", mapped[0], mapped[1],
                              how=f"S{s_:02d}E{e_:02d} out of range → absolute {e_}")
        return ParsedFile("episode", s_, e_, how="explicit SxxEyy (unverified)")

    cleaned = _strip_noise(stem)
    tail = _strip_trailing_groups(cleaned)

    # ── Layer 2: NNxNN (4x02) ──
    m = _NNxNN.search(cleaned)
    if m:
        return ParsedFile("episode", int(m.group(1)), int(m.group(2)), how="explicit NNxNN")

    candidates = _episode_candidates(cleaned, tail)

    # ── Spin-off / OVA content bundled in a pack -> specials, never main seasons ──
    if _SPECIAL_MARKERS.search(path):
        num = candidates[0][0] if candidates else None
        return ParsedFile("episode", 0, num, how="special/OVA marker",
                          note="filed as specials") if num else \
            ParsedFile("extra", note="side content without a number")

    if not candidates:
        return ParsedFile("unknown", note=f"no episode number found in '{fname}'")

    # ── Layer 3: season from the name ──
    # Ordinal form first: in "Show 2nd Season 25" the season is 2 and 25 is the
    # episode — matching "Season 25" would read the episode as the season.
    explicit_season = None
    om = _SEASON_ORDINAL.search(cleaned)
    if om:
        explicit_season = int(om.group(1))
    else:
        sm = _SEASON_ONLY.search(cleaned)
        # Ignore a "Season N" whose number is itself an episode candidate.
        if sm and not any(sm.span(1) == span for *_, span in candidates):
            explicit_season = int(sm.group(1))

    # ── Layer 4: decide how to read the bare number ──
    single_pack_season = next(iter(pack_seasons)) if pack_seasons and len(pack_seasons) == 1 else None
    target_season = explicit_season if explicit_season is not None else single_pack_season

    for num, dec, how, _span in candidates:
        if dec:  # "05.5" — a half episode is special content
            return ParsedFile("episode", 0, num, how=f"{how} (decimal .{dec})",
                              note="half-episode filed as special")

        # Per-season reading, when we know which season to expect
        if target_season is not None and _valid_episode(target_season, num, season_counts):
            return ParsedFile("episode", target_season, num,
                              how=f"{how} + season {target_season}")

        if season_counts:
            # Absolute reading (anime convention), validated against metadata
            mapped = absolute_to_season_episode(num, season_counts)
            if mapped:
                return ParsedFile("episode", mapped[0], mapped[1],
                                  how=f"{how} (absolute {num})")
            # Concatenated season+episode ("514" = S05E14)
            compact = _compact_to_season_episode(num, season_counts)
            if compact:
                return ParsedFile("episode", compact[0], compact[1],
                                  how=f"{how} (compact {num})")
        else:
            # No metadata to check against: assume season 1 rather than drop it
            return ParsedFile("episode", target_season or 1, num, how=f"{how} (assumed S01)")

    return ParsedFile("unknown", note=f"episode number out of range in '{fname}'")


def canonical_name(series_name: str, parsed: ParsedFile, original: str) -> str:
    """Jellyfin-friendly filename: 'Series - S01E05.mkv'.

    Consistent across sources, so the same show pulled from two release groups
    lands as two neatly matching files instead of
    'BLACK TORCH - S01E06 - Turn Up - 2160p HDR Ai Upscale -Mesc.mkv' next to
    '[YE] BLACK TORCH - 05 (BS11 1280x720 x265 10bit AAC).mkv'.
    """
    if not parsed.is_episode:
        return os.path.basename(original)
    # Specials keep their original names. A pack's OVAs and its bundled
    # spin-off both start numbering at 1, so renaming them to S00E01… would
    # make two different files claim the same slot and overwrite each other.
    if parsed.season == 0:
        return os.path.basename(original)
    ext = os.path.splitext(original)[1] or ".mkv"
    safe = re.sub(r'[<>:"/\\|?*]', "", series_name).strip()
    return f"{safe} - S{parsed.season:02d}E{parsed.episode:02d}{ext}"


def season_folder(parsed: ParsedFile) -> str:
    """Season directory for a parsed file ('' when unknown)."""
    if parsed.season is None:
        return ""
    return f"Season {parsed.season:02d}"
