# TorrentDownloader

Bulk download entire TV series from Stremio/Torrentio — no more manually selecting sources episode by episode.

## How It Works

1. **Search** for a series by name or IMDB ID (uses Stremio's Cinemeta metadata)
2. **Fetch** available torrent streams from Torrentio for every episode
3. **Send** the best matching magnet links to **qBittorrent** (or Real-Debrid)
4. **Sit back** while everything downloads automatically

## Setup

### 1. Install dependencies

```bash
cd TorrentDownloader
pip install -r requirements.txt
```

### 2. Configure

Edit `config.json`:

```jsonc
{
    "torrentio": {
        "base_url": "https://torrentio.strem.fun",
        "config": "",                    // Your Torrentio config string (see below)
        "preferred_quality": ["1080p", "720p"],
        "exclude_keywords": ["cam", "hdcam", "telesync"],
        "max_results_per_episode": 5
    },
    "qbittorrent": {
        "host": "http://localhost",
        "port": 8080,
        "username": "admin",
        "password": "adminadmin",       // Change this!
        "save_path": "/path/to/downloads",  // Leave empty for qBittorrent default
        "category": "stremio-series",
        "sequential_download": true,
        "first_last_piece_priority": true
    },
    "real_debrid": {
        "enabled": false,               // Set to true to use Real-Debrid
        "api_key": ""                   // Get from https://real-debrid.com/apitoken
    },
    "download": {
        "delay_between_episodes": 2,    // Seconds between API requests
        "delay_between_seasons": 5,
        "auto_select_best": true,
        "dry_run": false
    }
}
```

### 3. qBittorrent Setup

Make sure qBittorrent's Web UI is enabled:
- **Tools → Options → Web UI**
- Check "Web User Interface (Remote control)"
- Set port (default 8080) and credentials
- Update `config.json` with matching values

### 4. Torrentio Config String (Optional)

If you use custom Torrentio filters:
1. Go to [torrentio.strem.fun](https://torrentio.strem.fun)
2. Configure your preferred settings (providers, quality, etc.)
3. Copy the config string from the URL (the part after `torrentio.strem.fun/`)
4. Paste it into `config.json` under `torrentio.config`

Example: if your URL is `https://torrentio.strem.fun/sort=qualitysize|qualityfilter=480p,scr,cam/manifest.json`,
then your config string is `sort=qualitysize|qualityfilter=480p,scr,cam`.

## Usage


### Enter Python Virtual Environment
```bash
source /home/noah/Repositories/TorrentDownloader/.venv/bin/activate
```

### Interactive mode (recommended for first use)

```bash
python download.py
```

### Search by name

```bash
python download.py --search "Breaking Bad"
```

### Download by IMDB ID

```bash
# Find the IMDB ID from https://www.imdb.com (it's in the URL)
python download.py --imdb tt0903747
```

### Download specific seasons

```bash
# Single season
python download.py --imdb tt0903747 --season 1

# Season range
python download.py --imdb tt0903747 --season 2-4

# All seasons (default)
python download.py --imdb tt0903747 --season all
```

### Preview without downloading (dry run)

```bash
python download.py --imdb tt0903747 --dry-run
```

### Manually pick each stream

```bash
python download.py --imdb tt0903747 --manual
```

### Prefer a specific quality

```bash
python download.py --imdb tt0903747 --quality 720p 480p
```

### Save magnet links to files (no torrent client needed)

```bash
python download.py --imdb tt0903747 --save-magnets
```

### Use Real-Debrid

Set `real_debrid.enabled` to `true` and add your API key in `config.json`, then run normally.
The script will add magnets to your Real-Debrid cloud instead of qBittorrent.

## All CLI Options

| Flag | Description |
|------|-------------|
| `--imdb ID` | IMDB ID of the series (e.g., `tt0903747`) |
| `--search QUERY` | Search for a series by name |
| `--season RANGE` | Season(s): `1`, `2-5`, or `all` |
| `--quality Q [Q...]` | Preferred quality order (e.g., `1080p 720p`) |
| `--dry-run` | Preview without downloading |
| `--manual` | Manually select stream per episode |
| `--save-magnets` | Save .magnet files instead of using a client |
| `--torrentio-config` | Override Torrentio config string |
| `--config PATH` | Path to alternate config.json |

## File Structure

```
TorrentDownloader/
├── download.py        # Main CLI entry point
├── cinemeta.py        # Series metadata fetcher (Stremio Cinemeta API)
├── torrentio.py       # Torrent stream fetcher (Torrentio addon API)
├── clients.py         # Download clients (qBittorrent, Real-Debrid, magnet saver)
├── config.json        # Configuration
├── requirements.txt   # Python dependencies
└── README.md
```

## Tips

- **First run**: Use `--dry-run` to preview what would be downloaded
- **Old/niche series**: If auto-select picks bad sources, use `--manual` mode
- **Rate limiting**: The script waits between requests to avoid getting blocked. You can adjust `delay_between_episodes` in config
- **Sequential download**: Enabled by default in qBittorrent so you can start watching sooner
- **Resume**: If interrupted, just run again — qBittorrent will skip torrents it already has
