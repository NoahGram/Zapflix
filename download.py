#!/usr/bin/env python3
"""
TorrentDownloader - Bulk download series from Stremio/Torrentio.

Automates the process of finding and downloading entire series by:
1. Searching for a series via Cinemeta (Stremio's metadata provider)
2. Fetching torrent streams from Torrentio for each episode
3. Sending them to qBittorrent (or Real-Debrid) for downloading

Usage:
    python download.py                          # Interactive mode
    python download.py --imdb tt0903747         # Download by IMDB ID
    python download.py --search "Breaking Bad"  # Search and download
    python download.py --imdb tt0903747 --season 1        # Only season 1
    python download.py --imdb tt0903747 --season 1-3      # Seasons 1 to 3
    python download.py --imdb tt0903747 --dry-run         # Preview without downloading
"""

import argparse
import json
import sys
import time
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt, IntPrompt, Confirm
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.panel import Panel
from rich.text import Text

from cinemeta import search_series, get_series_metadata, Episode, Series
from torrentio import TorrentioClient, TorrentStream
from clients import QBittorrentClient, RealDebridClient, MagnetFileSaver, AddResult

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


def display_streams(streams: list[TorrentStream], episode_label: str):
    """Display available streams for an episode."""
    if not streams:
        console.print(f"  [red]✗ No streams found for {episode_label}[/red]")
        return

    table = Table(show_header=True, header_style="bold", title=f"Streams for {episode_label}")
    table.add_column("#", style="dim", width=3)
    table.add_column("Quality", style="cyan", width=8)
    table.add_column("Size", style="green", width=10)
    table.add_column("Seeders", style="yellow", width=8)
    table.add_column("Source", style="dim", max_width=50)

    for i, s in enumerate(streams, 1):
        table.add_row(str(i), s.quality, s.size, str(s.seeders), s.source[:50])

    console.print(table)


def interactive_search() -> str:
    """Interactive series search. Returns IMDB ID."""
    query = Prompt.ask("\n[bold]🔍 Search for a series[/bold]")
    console.print(f"[dim]Searching for '{query}'...[/dim]")

    results = search_series(query)
    if not results:
        console.print("[red]No results found.[/red]")
        sys.exit(1)

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=3)
    table.add_column("Name", style="cyan")
    table.add_column("Year", style="green", width=12)
    table.add_column("IMDB ID", style="dim")

    for i, r in enumerate(results[:10], 1):
        table.add_row(str(i), r["name"], str(r["year"]), r["imdb_id"])

    console.print(table)

    choice = IntPrompt.ask(
        "Select a series",
        choices=[str(i) for i in range(1, min(len(results), 10) + 1)],
    )
    return results[choice - 1]["imdb_id"]


def run_download(
    series: Series,
    episodes: list[Episode],
    torrentio: TorrentioClient,
    config: dict,
    dry_run: bool = False,
    auto_select: bool = True,
):
    """Main download loop - fetch streams and send to download client."""

    # Initialize download client
    rd_config = config.get("real_debrid", {})
    qb_config = config.get("qbittorrent", {})
    dl_config = config.get("download", {})

    use_rd = rd_config.get("enabled", False) and rd_config.get("api_key")

    if use_rd:
        client = RealDebridClient(api_key=rd_config["api_key"])
        if not client.test_connection():
            console.print("[red]Real-Debrid connection failed. Falling back to qBittorrent.[/red]")
            use_rd = False

    if not use_rd:
        client = QBittorrentClient(
            host=qb_config.get("host", "http://localhost"),
            port=qb_config.get("port", 8080),
            username=qb_config.get("username", "admin"),
            password=qb_config.get("password", "adminadmin"),
            save_path=qb_config.get("save_path", ""),
            category=qb_config.get("category", "stremio-series"),
            sequential=qb_config.get("sequential_download", True),
            first_last_priority=qb_config.get("first_last_piece_priority", True),
        )
        if not dry_run and not client.login():
            console.print("[red]Cannot connect to qBittorrent. Check config.json.[/red]")
            if Confirm.ask("Save magnet links to files instead?", default=True):
                client = MagnetFileSaver(output_dir=f"magnets/{series.name}")
            else:
                sys.exit(1)

    # Stats
    success = 0
    failed = 0
    skipped = 0
    already_have = 0  # Episodes covered by an already-added season pack

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
            if i > 0:
                time.sleep(dl_config.get("delay_between_seasons", 5))

        # Fetch streams
        console.print(f"\n  [bold]{ep.label}[/bold] - {ep.name}")
        streams = torrentio.get_streams(ep)

        if not streams:
            console.print(f"  [red]✗ No streams found[/red]")
            failed += 1
            continue

        # Select stream
        if auto_select:
            selected = torrentio.select_best_stream(streams)
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

        # Download
        if dry_run:
            console.print(f"  [dim]Would download: {selected}[/dim]")
            success += 1
        else:
            subfolder = f"{safe_series_name}/Season {ep.season:02d}"

            if isinstance(client, QBittorrentClient):
                result = client.add_torrent(
                    selected,
                    subfolder=subfolder,
                    tags=f"{series.name},S{ep.season:02d}",
                )
                if result == AddResult.SUCCESS:
                    console.print(f"  [green]✓ Added to downloads[/green]")
                    success += 1
                elif result == AddResult.ALREADY_EXISTS:
                    console.print(
                        f"  [cyan]↳ Already covered by season pack (same torrent)[/cyan]"
                    )
                    already_have += 1
                else:
                    console.print(f"  [red]✗ Failed to add[/red]")
                    failed += 1
            elif isinstance(client, RealDebridClient):
                torrent_id = client.add_magnet(selected)
                if torrent_id is not None:
                    console.print(f"  [green]✓ Added to downloads[/green]")
                    success += 1
                else:
                    console.print(f"  [red]✗ Failed to add[/red]")
                    failed += 1
            elif isinstance(client, MagnetFileSaver):
                path = client.save(selected, series.name, ep.label)
                console.print(f"  [dim]Saved to {path}[/dim]")
                success += 1
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

    # Cleanup
    if isinstance(client, QBittorrentClient):
        client.logout()


def main():
    parser = argparse.ArgumentParser(
        description="Bulk download series from Stremio/Torrentio",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                                  Interactive mode
  %(prog)s --search "Breaking Bad"          Search by name
  %(prog)s --imdb tt0903747                 Download by IMDB ID
  %(prog)s --imdb tt0903747 --season 1      Only season 1
  %(prog)s --imdb tt0903747 --season 2-4    Seasons 2 through 4
  %(prog)s --imdb tt0903747 --dry-run       Preview without downloading
  %(prog)s --imdb tt0903747 --manual        Manually pick each stream
  %(prog)s --imdb tt0903747 --quality 720p  Prefer 720p quality
        """,
    )

    parser.add_argument("--imdb", help="IMDB ID of the series (e.g., tt0903747)")
    parser.add_argument("--search", help="Search for a series by name")
    parser.add_argument(
        "--season",
        default="all",
        help="Season(s) to download: '1', '2-5', or 'all' (default: all)",
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

    args = parser.parse_args()

    # Banner
    console.print(
        Panel(
            "[bold cyan]TorrentDownloader[/bold cyan]\n"
            "[dim]Bulk download series from Stremio/Torrentio[/dim]",
            border_style="cyan",
        )
    )

    # Load config
    if args.config:
        global CONFIG_FILE
        CONFIG_FILE = Path(args.config)
    config = load_config()

    # Get IMDB ID
    if args.imdb:
        imdb_id = args.imdb
    elif args.search:
        console.print(f"[dim]Searching for '{args.search}'...[/dim]")
        results = search_series(args.search)
        if not results:
            console.print("[red]No results found.[/red]")
            sys.exit(1)

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("#", style="dim", width=3)
        table.add_column("Name", style="cyan")
        table.add_column("Year", style="green", width=12)
        table.add_column("IMDB ID", style="dim")

        for i, r in enumerate(results[:10], 1):
            table.add_row(str(i), r["name"], str(r["year"]), r["imdb_id"])
        console.print(table)

        choice = IntPrompt.ask(
            "Select a series",
            choices=[str(i) for i in range(1, min(len(results), 10) + 1)],
        )
        imdb_id = results[choice - 1]["imdb_id"]
    else:
        imdb_id = interactive_search()

    # Fetch series metadata
    console.print(f"\n[dim]Fetching metadata for {imdb_id}...[/dim]")
    try:
        series = get_series_metadata(imdb_id)
    except Exception as e:
        console.print(f"[red]Failed to fetch series metadata: {e}[/red]")
        sys.exit(1)

    display_series_info(series)

    # Parse season range
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

    # Confirm
    dry_run = args.dry_run or config.get("download", {}).get("dry_run", False)
    if not dry_run:
        if not Confirm.ask(f"\nProceed with downloading {len(episodes)} episodes?", default=True):
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(0)

    # Initialize Torrentio client
    torrentio_cfg = config.get("torrentio", {})
    torrentio = TorrentioClient(
        base_url=torrentio_cfg.get("base_url", "https://torrentio.strem.fun"),
        config=args.torrentio_config or torrentio_cfg.get("config", ""),
        preferred_quality=args.quality or torrentio_cfg.get("preferred_quality", ["1080p", "720p"]),
        exclude_keywords=torrentio_cfg.get("exclude_keywords", []),
        max_results=torrentio_cfg.get("max_results_per_episode", 5),
    )

    # Override to magnet saver if requested
    if args.save_magnets:
        config["qbittorrent"] = {}  # Will trigger fallback to MagnetFileSaver
        config["real_debrid"] = {"enabled": False}

    # Run
    auto_select = not args.manual and config.get("download", {}).get("auto_select_best", True)
    run_download(
        series=series,
        episodes=episodes,
        torrentio=torrentio,
        config=config,
        dry_run=dry_run,
        auto_select=auto_select,
    )


if __name__ == "__main__":
    main()
