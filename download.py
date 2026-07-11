#!/usr/bin/env python3
"""
TorrentDownloader - Bulk download movies and series from Stremio/Torrentio.

Automates the process of finding and downloading entire series or movies by:
1. Searching via Cinemeta (Stremio's metadata provider)
2. Fetching torrent streams from Torrentio for each item
3. Sending them to qBittorrent (or Real-Debrid) for downloading

Usage:
    python download.py                          # Interactive mode
    python download.py --search "Pacific Rim"    # Search movies + series
    python download.py --imdb tt1663662          # Download by IMDB ID (auto-detects type)
    python download.py --imdb tt0903747 --season 1        # Series season 1
    python download.py --imdb tt0903747 --season 1-3      # Seasons 1 to 3
    python download.py --search "Deadpool" --dry-run      # Preview without downloading
"""

import argparse
import json
import re
import sys
import time
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.panel import Panel

from cinemeta import (
    search_all,
    get_series_metadata, get_movie_metadata,
    Episode, Series, Movie,
)
from torrentio import TorrentioClient, TorrentStream
from clients import RealDebridClient, MagnetFileSaver, Aria2Client, AddResult
from profiles import QualityProfile, get_profile, list_profiles, load_custom_profile, PROFILES

console = Console()

CONFIG_FILE = Path(__file__).parent / "config.json"


def load_config() -> dict:
    """Load configuration from config.json."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    console.print("[yellow]⚠ config.json not found, using defaults[/yellow]")
    return {}


def parse_season_range(season_arg: str) -> tuple[int | None, int | None]:
    """Parse season argument like '1', '1-3', 'all'."""
    if not season_arg or season_arg.lower() == "all":
        return None, None
    if "-" in season_arg:
        parts = season_arg.split("-", 1)
        return int(parts[0]), int(parts[1])
    s = int(season_arg)
    return s, s


def display_series_info(series: Series):
    """Display series information in a nice table."""
    console.print()
    console.print(
        Panel(
            f"[bold cyan]{series.name}[/bold cyan] ({series.year})\n"
            f"IMDB: [dim]{series.imdb_id}[/dim]\n"
            f"Seasons: [green]{len(series.seasons)}[/green] | "
            f"Episodes: [green]{series.total_episodes}[/green]",
            title="📺 Series Info",
        )
    )

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Season", style="cyan", width=8)
    table.add_column("Episodes", style="green", width=10)
    table.add_column("Range", style="dim")

    for s_num in sorted(series.seasons.keys()):
        eps = series.seasons[s_num]
        ep_range = f"E{eps[0].episode:02d} - E{eps[-1].episode:02d}"
        table.add_row(f"S{s_num:02d}", str(len(eps)), ep_range)

    console.print(table)


def display_streams(streams: list[TorrentStream], label: str):
    """Display available streams for an episode or movie."""
    if not streams:
        console.print(f"  [red]✗ No streams found for {label}[/red]")
        return

    table = Table(show_header=True, header_style="bold", title=f"Streams for {label}")
    table.add_column("#", style="dim", width=3)
    table.add_column("Quality", style="cyan", width=8)
    table.add_column("Size", style="green", width=10)
    table.add_column("Seeders", style="yellow", width=8)
    table.add_column("Source", style="dim", max_width=50)

    for i, s in enumerate(streams, 1):
        table.add_row(str(i), s.quality, s.size, str(s.seeders), s.source[:50])

    console.print(table)


def interactive_search() -> tuple[str, str]:
    """Interactive search for movies and series. Returns (imdb_id, type)."""
    query = Prompt.ask("\n[bold]🔍 Search for a movie or series[/bold]")
    console.print(f"[dim]Searching for '{query}'...[/dim]")

    results = search_all(query)
    if not results:
        console.print("[red]No results found.[/red]")
        sys.exit(1)

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Type", style="yellow", width=8)
    table.add_column("Name", style="cyan")
    table.add_column("Year", style="green", width=12)
    table.add_column("IMDB ID", style="dim")

    for i, r in enumerate(results[:15], 1):
        type_icon = "🎬" if r["type"] == "movie" else "📺"
        table.add_row(str(i), f"{type_icon} {r['type']}", r["name"], str(r["year"]), r["imdb_id"])

    console.print(table)

    choice = IntPrompt.ask(
        "Select",
        choices=[str(i) for i in range(1, min(len(results), 15) + 1)],
    )
    selected = results[choice - 1]
    return selected["imdb_id"], selected["type"]


def _init_download_client(config: dict, dry_run: bool, fallback_name: str = "media"):
    """Initialize the download client based on config. Returns the client.

    Real-Debrid only; falls back to saving magnet files if RD is unavailable.
    """
    rd_config = config.get("real_debrid", {})

    if rd_config.get("enabled", False) and rd_config.get("api_key"):
        client = RealDebridClient(api_key=rd_config["api_key"])
        if dry_run or client.test_connection():
            return client
        console.print("[red]Real-Debrid connection failed.[/red]")

    if Confirm.ask("Save magnet links to files instead?", default=True):
        return MagnetFileSaver(output_dir=f"magnets/{fallback_name}")
    sys.exit(1)


def _init_aria2_client(config: dict):
    """Initialize aria2 client if configured. Returns Aria2Client or None."""
    aria2_config = config.get("aria2", {})
    if not aria2_config.get("enabled", False):
        return None

    client = Aria2Client(
        host=aria2_config.get("host", "http://localhost"),
        port=aria2_config.get("port", 6800),
        secret=aria2_config.get("secret", ""),
        download_dir=aria2_config.get("download_dir", ""),
    )

    if client.test_connection():
        return client

    console.print("[yellow]⚠ aria2 configured but not reachable — skipping NAS download[/yellow]")
    return None


def _sync_rd_to_aria2(
    rd_client: RealDebridClient,
    aria2: Aria2Client,
    content_name: str,
    content_type: str,
    base_dir: str = "",
    timeout: int = 600,
    poll_interval: int = 5,
):
    """Wait for RD torrents to complete and send download links to aria2.

    Polls Real-Debrid until torrents finish processing, unrestricts the
    download links, and sends them to aria2 on the NAS with a Jellyfin-
    friendly folder structure.
    """
    torrent_ids = rd_client.get_pending_torrents()
    if not torrent_ids:
        return

    console.print(
        f"\n[bold]📥 Sending {len(torrent_ids)} torrent(s) to aria2 for NAS download...[/bold]"
    )

    total_files = 0
    total_sent = 0
    total_failed = 0

    for tid in torrent_ids:
        # Poll until RD has the torrent ready for download
        start_time = time.time()
        info = None

        while time.time() - start_time < timeout:
            info = rd_client.get_torrent_info(tid)
            if not info:
                break

            status = info.get("status")
            if status == "downloaded":
                break
            elif status in ("error", "virus", "dead"):
                console.print(
                    f"  [red]✗ RD torrent failed: {info.get('filename', '?')} ({status})[/red]"
                )
                info = None
                break
            else:
                progress = info.get("progress", 0)
                console.print(
                    f"\r  [dim]⏳ {info.get('filename', '?')[:60]}: "
                    f"{status} ({progress}%)[/dim]   ",
                    end="",
                )
                time.sleep(poll_interval)
        else:
            console.print(
                f"\n  [yellow]⏳ Torrent still processing after {timeout}s — "
                f"will continue downloading in background on RD[/yellow]"
            )
            continue

        if not info:
            continue

        links = info.get("links", [])
        if not links:
            console.print(f"  [yellow]⚠ No files in torrent: {info.get('filename', '?')}[/yellow]")
            continue

        console.print(f"\n  [bold]Unrestricting {len(links)} file(s):[/bold] {info.get('filename', '?')[:70]}")
        total_files += len(links)

        for link in links:
            unrestricted = rd_client.unrestrict_link(link)
            if not unrestricted or not unrestricted.get("download"):
                console.print(f"    [red]✗ Failed to unrestrict link[/red]")
                total_failed += 1
                continue

            url = unrestricted["download"]
            filename = unrestricted.get("filename", "unknown")

            # Build Jellyfin-friendly subdirectory
            if content_type == "movie":
                subdir = os.path.join(base_dir, content_name) if base_dir else ""
            else:
                # Detect season number from filename for proper organization
                season_match = re.search(r'[Ss](\d{1,2})', filename)
                if season_match:
                    season_num = int(season_match.group(1))
                    subdir = (
                        os.path.join(base_dir, content_name, f"Season {season_num:02d}")
                        if base_dir else ""
                    )
                else:
                    subdir = os.path.join(base_dir, content_name) if base_dir else ""

            gid = aria2.add_download(url, directory=subdir, filename=filename)
            if gid:
                console.print(f"    [green]✓[/green] {filename}")
                total_sent += 1
            else:
                console.print(f"    [red]✗[/red] {filename}")
                total_failed += 1

            time.sleep(0.5)  # Small delay between unrestrict calls

    # Summary
    parts = [f"[green]✓ Sent: {total_sent}[/green]"]
    if total_failed > 0:
        parts.append(f"[red]✗ Failed: {total_failed}[/red]")
    parts.append(f"Total files: {total_files}")

    console.print(
        Panel(" | ".join(parts), title="📥 NAS Download via aria2")
    )


def run_movie_download(
    movie: Movie,
    torrentio: TorrentioClient,
    config: dict,
    dry_run: bool = False,
    auto_select: bool = True,
    profile: QualityProfile | None = None,
):
    """Download a single movie."""

    client = _init_download_client(config, dry_run, fallback_name=movie.name)

    safe_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in movie.name
    )

    console.print()
    console.print(
        f"[bold]{'🔍 DRY RUN - ' if dry_run else ''}Fetching streams for {movie.name}...[/bold]"
    )

    streams = torrentio.get_movie_streams(movie.imdb_id)

    if not streams:
        console.print("[red]✗ No streams found for this movie.[/red]")
        return

    # Select stream
    if auto_select:
        selected = torrentio.select_best_stream(streams, profile=profile)
        if selected:
            console.print(f"  [green]→ Auto-selected:[/green] {selected}")
        else:
            console.print("[red]✗ No suitable stream found with current profile.[/red]")
            return
    else:
        display_streams(streams, movie.name)
        choice = Prompt.ask(
            "Select stream (number, 'q' to quit)",
            default="1",
        )
        if choice.lower() == "q":
            return
        try:
            selected = streams[int(choice) - 1]
        except (ValueError, IndexError):
            console.print("[yellow]Invalid choice.[/yellow]")
            return

    # Download
    if dry_run:
        console.print(f"  [dim]Would download: {selected}[/dim]")
        console.print(
            Panel("[green]✓ Would add: 1[/green] | Total: 1", title="📊 Download Summary")
        )
    else:
        ok = False
        if isinstance(client, RealDebridClient):
            result = client.add_magnet(selected)
            if result in (AddResult.SUCCESS, AddResult.ALREADY_EXISTS):
                console.print(f"  [green]✓ Added to Real-Debrid[/green]")
                ok = True
            else:
                console.print(f"  [red]✗ Failed to add[/red]")
        elif isinstance(client, MagnetFileSaver):
            path = client.save(selected, movie.name, "movie")
            console.print(f"  [dim]Saved to {path}[/dim]")
            ok = True

        status = "[green]✓ Added: 1[/green]" if ok else "[red]✗ Failed: 1[/red]"
        console.print(Panel(f"{status} | Total: 1", title="📊 Download Summary"))

        # Sync Real-Debrid downloads to NAS via aria2
        if ok and isinstance(client, RealDebridClient) and not dry_run:
            aria2 = _init_aria2_client(config)
            if aria2:
                # Use first year for Jellyfin folder name
                year = movie.year.split("-")[0].split("–")[0].strip()
                content_name = f"{movie.name} ({year})"
                _sync_rd_to_aria2(
                    rd_client=client,
                    aria2=aria2,
                    content_name=content_name,
                    content_type="movie",
                    base_dir=config.get("aria2", {}).get("download_dir", ""),
                )


def _parse_season_coverage(source: str) -> set[int]:
    """Parse which seasons a torrent pack covers from its source/title.

    Returns a set of season numbers covered, or empty set if not a multi-season pack.
    Common patterns: "S01-S08", "Season 1-8", "Complete Series".
    """
    source_lower = source.lower()

    # "Complete Series" / "Complete.Series"
    if "complete series" in source_lower or "complete.series" in source_lower:
        return set(range(1, 100))  # Covers all practical seasons

    # "S01-S08", "S01-08", "s1-s8"
    match = re.search(r's(\d{1,2})\s*[-–]\s*s?(\d{1,2})', source_lower)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if end > start:
            return set(range(start, end + 1))

    # "Season 1-8", "Seasons 1-8", "Season.1-8"
    match = re.search(r'seasons?\s*\.?\s*(\d{1,2})\s*[-–]\s*(\d{1,2})', source_lower)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        if end > start:
            return set(range(start, end + 1))

    return set()


def run_download(
    series: Series,
    episodes: list[Episode],
    torrentio: TorrentioClient,
    config: dict,
    dry_run: bool = False,
    auto_select: bool = True,
    profile: QualityProfile | None = None,
):
    """Main download loop - fetch streams and send to download client."""

    client = _init_download_client(config, dry_run, fallback_name=series.name)
    dl_config = config.get("download", {})

    # Stats
    success = 0
    failed = 0
    skipped = 0
    already_have = 0  # Episodes covered by an already-added season pack

    # Pack tracking — avoid duplicate downloads and redundant API calls
    covered_hashes: set[str] = set()    # Info hashes already sent to client
    covered_seasons: set[int] = set()   # Seasons fully covered by a detected pack

    safe_series_name = "".join(
        c if c.isalnum() or c in " -_" else "_" for c in series.name
    )

    console.print()
    console.print(
        f"[bold]{'🔍 DRY RUN - ' if dry_run else ''}Processing {len(episodes)} episodes...[/bold]"
    )
    console.print()

    current_season = None

    for i, ep in enumerate(episodes):
        # Season header
        if ep.season != current_season:
            current_season = ep.season
            console.print(f"\n[bold cyan]═══ Season {current_season} ═══[/bold cyan]")
            if ep.season in covered_seasons:
                season_ep_count = sum(1 for e in episodes if e.season == ep.season)
                console.print(f"  [cyan]↳ All {season_ep_count} episodes covered by season pack[/cyan]")
            elif i > 0:
                time.sleep(dl_config.get("delay_between_seasons", 5))

        # Skip episodes already covered by a season pack
        if ep.season in covered_seasons:
            already_have += 1
            continue

        # Fetch streams
        console.print(f"\n  [bold]{ep.label}[/bold] - {ep.name}")
        streams = torrentio.get_streams(ep)

        if not streams:
            console.print(f"  [red]✗ No streams found[/red]")
            failed += 1
            continue

        # Select stream
        if auto_select:
            selected = torrentio.select_best_stream(streams, profile=profile)
            if selected:
                console.print(f"  [green]→ Auto-selected:[/green] {selected}")
            else:
                console.print(f"  [red]✗ No suitable stream found[/red]")
                failed += 1
                continue
        else:
            display_streams(streams, ep.label)
            choice = Prompt.ask(
                "  Select stream (number, 's' to skip, 'q' to quit)",
                default="1",
            )
            if choice.lower() == "q":
                break
            if choice.lower() == "s":
                skipped += 1
                continue
            try:
                selected = streams[int(choice) - 1]
            except (ValueError, IndexError):
                console.print("  [yellow]Invalid choice, skipping[/yellow]")
                skipped += 1
                continue

        # Check if this stream's hash was already added (same pack reused)
        if selected.info_hash.lower() in covered_hashes:
            pack_seasons = _parse_season_coverage(selected.source)
            if pack_seasons:
                new_covered = pack_seasons - covered_seasons
                if new_covered:
                    covered_seasons.update(pack_seasons)
            console.print(f"  [cyan]↳ Already covered by season pack (same torrent)[/cyan]")
            already_have += 1
            continue

        # Download
        if dry_run:
            console.print(f"  [dim]Would download: {selected}[/dim]")
            success += 1
        else:
            subfolder = f"{safe_series_name}/Season {ep.season:02d}"

            result = AddResult.FAILED

            if isinstance(client, RealDebridClient):
                result = client.add_magnet(selected)
            elif isinstance(client, MagnetFileSaver):
                path = client.save(selected, series.name, ep.label)
                console.print(f"  [dim]Saved to {path}[/dim]")
                result = AddResult.SUCCESS

            # Handle result and detect season pack coverage
            if result == AddResult.SUCCESS:
                if not isinstance(client, MagnetFileSaver):
                    console.print(f"  [green]✓ Added to downloads[/green]")
                success += 1
                covered_hashes.add(selected.info_hash.lower())
                pack_seasons = _parse_season_coverage(selected.source)
                if pack_seasons:
                    new_covered = pack_seasons - covered_seasons
                    if new_covered:
                        covered_seasons.update(pack_seasons)
                        season_list = sorted(pack_seasons)
                        if season_list[-1] - season_list[0] + 1 == len(season_list):
                            range_str = f"Seasons {season_list[0]}-{season_list[-1]}"
                        else:
                            range_str = f"Seasons {', '.join(str(s) for s in season_list)}"
                        remaining = sum(
                            1 for e in episodes[i + 1:]
                            if e.season in pack_seasons
                        )
                        if remaining > 0:
                            console.print(
                                f"  [cyan]📦 Season pack — covers {range_str}"
                                f" ({remaining} remaining episodes will be skipped)[/cyan]"
                            )
            elif result == AddResult.ALREADY_EXISTS:
                console.print(
                    f"  [cyan]↳ Already covered by season pack (same torrent)[/cyan]"
                )
                covered_hashes.add(selected.info_hash.lower())
                pack_seasons = _parse_season_coverage(selected.source)
                if pack_seasons:
                    covered_seasons.update(pack_seasons)
                already_have += 1
            else:
                console.print(f"  [red]✗ Failed to add[/red]")
                failed += 1

        # Rate limiting
        if i < len(episodes) - 1:
            time.sleep(dl_config.get("delay_between_episodes", 2))

    # Summary
    console.print()
    parts = [
        f"[green]✓ Added: {success}[/green]",
        f"[red]✗ Failed: {failed}[/red]",
    ]
    if already_have > 0:
        parts.append(f"[cyan]↳ Season pack: {already_have}[/cyan]")
    if skipped > 0:
        parts.append(f"[yellow]⊘ Skipped: {skipped}[/yellow]")
    parts.append(f"Total: {len(episodes)}")

    console.print(
        Panel(
            " | ".join(parts),
            title="📊 Download Summary",
        )
    )

    # Sync Real-Debrid downloads to NAS via aria2
    if isinstance(client, RealDebridClient) and success > 0 and not dry_run:
        aria2 = _init_aria2_client(config)
        if aria2:
            year = series.year.split("-")[0].split("–")[0].strip()
            content_name = f"{series.name} ({year})"
            _sync_rd_to_aria2(
                rd_client=client,
                aria2=aria2,
                content_name=content_name,
                content_type="series",
                base_dir=config.get("aria2", {}).get("download_dir", ""),
            )


def main():
    parser = argparse.ArgumentParser(
        description="Bulk download movies & series from Stremio/Torrentio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  Interactive mode
  %(prog)s --search "Breaking Bad"          Search by name
  %(prog)s --search "Pacific Rim"           Search for a movie
  %(prog)s --imdb tt0903747                 Download by IMDB ID (series)
  %(prog)s --imdb tt1663662 --type movie    Download a movie by IMDB ID
  %(prog)s --imdb tt0903747 --season 1      Only season 1
  %(prog)s --imdb tt0903747 --season 2-4    Seasons 2 through 4
  %(prog)s --imdb tt0903747 --dry-run       Preview without downloading
  %(prog)s --imdb tt0903747 --manual        Manually pick each stream
  %(prog)s --imdb tt0903747 --quality 720p  Prefer 720p quality
        """,
    )

    parser.add_argument("--imdb", help="IMDB ID (e.g., tt0903747)")
    parser.add_argument("--search", help="Search for a movie or series by name")
    parser.add_argument(
        "--type",
        choices=["movie", "series"],
        help="Content type (auto-detected if omitted)",
    )
    parser.add_argument(
        "--season",
        default="all",
        help="Season(s) to download: '1', '2-5', or 'all' (default: all, series only)",
    )
    parser.add_argument(
        "--quality",
        nargs="+",
        help="Preferred quality in order (default: 1080p 720p)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be downloaded without actually doing it",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Manually select stream for each episode instead of auto-selecting",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: ./config.json)",
    )
    parser.add_argument(
        "--torrentio-config",
        help="Torrentio config string (from the URL after torrentio.strem.fun/)",
    )
    parser.add_argument(
        "--save-magnets",
        action="store_true",
        help="Save magnet links to files instead of sending to a client",
    )
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        help="Quality profile: " + ", ".join(
            f"{n} ({p.description})" for n, p in PROFILES.items()
        ),
    )
    parser.add_argument(
        "--list-profiles",
        action="store_true",
        help="Show all available quality profiles and exit",
    )

    args = parser.parse_args()

    # List profiles and exit
    if args.list_profiles:
        table = Table(show_header=True, header_style="bold magenta", title="Quality Profiles")
        table.add_column("Name", style="cyan", width=12)
        table.add_column("Description", style="green")
        table.add_column("Resolution", style="yellow")
        table.add_column("Codec", style="dim")
        table.add_column("Max Size", style="dim", width=10)
        for name, p in PROFILES.items():
            table.add_row(
                name,
                p.description,
                " > ".join(p.preferred_resolution),
                " > ".join(p.preferred_codecs[:2]),
                f"{p.max_size_gb} GB",
            )
        console.print(table)
        sys.exit(0)

    # Banner
    console.print(
        Panel(
            "[bold cyan]TorrentDownloader[/bold cyan]\n"
            "[dim]Bulk download movies & series from Stremio/Torrentio[/dim]",
            border_style="cyan",
        )
    )

    # Load config
    if args.config:
        global CONFIG_FILE
        CONFIG_FILE = Path(args.config)
    config = load_config()

    # Determine IMDB ID and content type
    content_type = args.type  # may be None (auto-detect)

    if args.imdb:
        imdb_id = args.imdb
    elif args.search:
        console.print(f"[dim]Searching for '{args.search}'...[/dim]")
        results = search_all(args.search)
        if not results:
            console.print("[red]No results found.[/red]")
            sys.exit(1)

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=3)
        table.add_column("Type", style="yellow", width=8)
        table.add_column("Name", style="cyan")
        table.add_column("Year", style="green", width=12)
        table.add_column("IMDB ID", style="dim")

        for i, r in enumerate(results[:15], 1):
            type_icon = "🎬" if r["type"] == "movie" else "📺"
            table.add_row(str(i), f"{type_icon} {r['type']}", r["name"], str(r["year"]), r["imdb_id"])
        console.print(table)

        choice = IntPrompt.ask(
            "Select",
            choices=[str(i) for i in range(1, min(len(results), 15) + 1)],
        )
        selected = results[choice - 1]
        imdb_id = selected["imdb_id"]
        content_type = selected["type"]
    else:
        imdb_id, content_type = interactive_search()

    # Auto-detect type if not set (e.g. --imdb without --type)
    if not content_type:
        console.print(f"[dim]Detecting content type for {imdb_id}...[/dim]")
        try:
            series_meta = get_series_metadata(imdb_id)
            # Valid series must have at least one episode
            if series_meta.total_episodes > 0:
                content_type = "series"
                console.print(f"[dim]Detected: series ({series_meta.name})[/dim]")
            else:
                raise ValueError("No episodes")
        except Exception:
            try:
                movie_meta = get_movie_metadata(imdb_id)
                content_type = "movie"
                console.print(f"[dim]Detected: movie ({movie_meta.name})[/dim]")
            except Exception:
                console.print("[red]Could not find this IMDB ID as either movie or series.[/red]")
                sys.exit(1)

    # Initialize Torrentio client
    torrentio_cfg = config.get("torrentio", {})

    # Resolve quality profile
    profile = None
    profile_name = args.profile or config.get("download", {}).get("profile", None)
    if profile_name:
        try:
            custom = config.get("profiles", {}).get(profile_name)
            if custom:
                profile = load_custom_profile(custom)
            else:
                profile = get_profile(profile_name)
            console.print(
                f"\n[bold]📋 Profile:[/bold] [cyan]{profile.name}[/cyan] — {profile.description}"
            )
        except KeyError as e:
            console.print(f"[red]{e}[/red]")
            sys.exit(1)

    torrentio = TorrentioClient(
        base_url=torrentio_cfg.get("base_url", "https://torrentio.strem.fun"),
        config=args.torrentio_config or torrentio_cfg.get("config", ""),
        preferred_quality=args.quality or torrentio_cfg.get("preferred_quality", ["1080p", "720p"]),
        exclude_keywords=torrentio_cfg.get("exclude_keywords", []),
        max_results=torrentio_cfg.get("max_results_per_episode", 5),
    )

    # Override to magnet saver if requested
    if args.save_magnets:
        config["real_debrid"] = {"enabled": False}

    auto_select = not args.manual and config.get("download", {}).get("auto_select_best", True)
    dry_run = args.dry_run or config.get("download", {}).get("dry_run", False)

    if content_type == "movie":
        # --- Movie flow ---
        console.print(f"\n[dim]Fetching metadata for {imdb_id}...[/dim]")
        try:
            movie = get_movie_metadata(imdb_id)
        except Exception as e:
            console.print(f"[red]Failed to fetch movie metadata: {e}[/red]")
            sys.exit(1)

        console.print(
            Panel(
                f"[bold]{movie.name}[/bold] ({movie.year})\n"
                f"[dim]{movie.description or 'No description'}[/dim]\n"
                f"Runtime: {movie.runtime or '?'} | Genres: {', '.join(movie.genres) if movie.genres else '?'}",
                title="🎬 Movie Info",
                border_style="cyan",
            )
        )

        if not dry_run:
            if not Confirm.ask(f"\nDownload [cyan]{movie.name}[/cyan]?", default=True):
                console.print("[yellow]Aborted.[/yellow]")
                sys.exit(0)

        run_movie_download(
            movie=movie,
            torrentio=torrentio,
            config=config,
            dry_run=dry_run,
            auto_select=auto_select,
            profile=profile,
        )
    else:
        # --- Series flow ---
        console.print(f"\n[dim]Fetching metadata for {imdb_id}...[/dim]")
        try:
            series = get_series_metadata(imdb_id)
        except Exception as e:
            console.print(f"[red]Failed to fetch series metadata: {e}[/red]")
            sys.exit(1)

        display_series_info(series)

        season_from, season_to = parse_season_range(args.season)
        episodes = series.get_episodes(season_from=season_from, season_to=season_to)

        if not episodes:
            console.print("[red]No episodes found for the specified range.[/red]")
            sys.exit(1)

        console.print(
            f"\n[bold]Selected [cyan]{len(episodes)}[/cyan] episodes "
            f"(S{episodes[0].season:02d}E{episodes[0].episode:02d} → "
            f"S{episodes[-1].season:02d}E{episodes[-1].episode:02d})[/bold]"
        )

        if not dry_run:
            if not Confirm.ask(f"\nProceed with downloading {len(episodes)} episodes?", default=True):
                console.print("[yellow]Aborted.[/yellow]")
                sys.exit(0)

        run_download(
            series=series,
            episodes=episodes,
            torrentio=torrentio,
            config=config,
            dry_run=dry_run,
            auto_select=auto_select,
            profile=profile,
        )


if __name__ == "__main__":
    main()
