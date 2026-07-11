# TorrentDownloader

Search for a movie or series, click download, and it lands on your NAS — via
**Real-Debrid** (cloud caching) + **aria2** (NAS downloader), ready for Jellyfin.

## How It Works

1. **Search** a title (Stremio's Cinemeta metadata → posters, plot, rating).
2. **Pick** it — for series, choose which seasons/episodes you want.
3. **Torrentio** (configured with your Real-Debrid key) returns streams, marking
   which are already **cached** on Real-Debrid (`[RD+]`).
4. **Deliver**:
   - *Cached* streams resolve straight to a Real-Debrid direct link → **aria2**
     pulls them to the NAS instantly (no waiting, no failures).
   - *Uncached* streams are added to Real-Debrid to download, then synced to
     aria2 when ready.

Real-Debrid is the only supported client (qBittorrent was removed).

> 📖 **New install?** Follow the step-by-step [INSTALL.md](INSTALL.md) — covers
> PC, generic servers, and a full UGREEN NAS (Docker) walkthrough incl. aria2.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Secrets — `.env`

Copy `.env.example` to `.env` and fill it in (`.env` is gitignored):

```ini
RD_API_KEY=your_real_debrid_key            # https://real-debrid.com/apitoken
TORRENTIO_CONFIG=sort=qualitysize|realdebrid=your_real_debrid_key
ARIA2_HOST=http://YOUR_NAS_IP
ARIA2_PORT=6800
ARIA2_SECRET=your_aria2_rpc_secret
ARIA2_DOWNLOAD_DIR=/downloads
```

Environment variables override `config.json`, so secrets stay out of committed
files. Non-secret settings (quality, delays, profile) live in `config.json`
(copy from `config.example.json`).

### 3. Torrentio config string (important)

The `[RD+]` cached detection and instant links **require** your Real-Debrid key
baked into the Torrentio config:

1. Go to [torrentio.strem.fun/configure](https://torrentio.strem.fun/configure),
   pick **Real-Debrid** and enter your key.
2. Copy the path segment between the host and `/manifest.json`.
3. Put it in `TORRENTIO_CONFIG` (e.g. `sort=qualitysize|realdebrid=<key>`).

> Note: requests use a browser User-Agent — Cloudflare blocks non-browser
> User-Agents on debrid-configured Torrentio requests.

## Run

### Web UI (recommended)

```bash
python web_server.py           # or: uvicorn web_server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` — search, open a title, pick episodes, download.

### Docker

```bash
docker compose up --build -d
```

`docker-compose.yml` reads `.env` and mounts `config.json`.

### CLI

```bash
python download.py --imdb tt0903747            # by IMDB id
python download.py --search "Breaking Bad"     # search
python download.py --imdb tt0903747 --dry-run  # preview
python download.py --imdb tt0903747 --season 2-4
```

## Web API

| Endpoint | Description |
|----------|-------------|
| `GET /api/search?q=` | Search movies + series (posters, year, type) |
| `GET /api/meta/{type}/{imdb_id}` | Full metadata + season/episode list |
| `POST /api/download` | `{imdb_id, type, selection?}` — queue a download |
| `GET /api/logs` | Recent server logs (UI polls this) |
| `GET /api/status` | Config + client (RD / aria2) status |

`selection` is `"all"` (or omitted) for everything, or
`[{"season": 1, "episodes": [1,2,3]}, ...]` for specific episodes.

## File Structure

```
TorrentDownloader/
├── web_server.py      # FastAPI web UI + REST API
├── backend.py         # Orchestration: Torrentio → RD → aria2 (+ env config)
├── download.py        # CLI entry point
├── cinemeta.py        # Metadata (Cinemeta): search, get_meta
├── torrentio.py       # Torrentio streams + [RD+] cached parsing + selection
├── clients.py         # RealDebridClient, Aria2Client, MagnetFileSaver
├── profiles.py        # Quality profiles / scoring
├── templates/         # Web UI (poster catalog + detail + episode picker)
├── config.json        # Non-secret config (gitignored)
├── .env               # Secrets (gitignored)
└── requirements.txt
```

## Tips

- **Prefer cached**: selection strongly prefers `[RD+]` streams — instant and
  reliable — and only falls back to uncached torrents when nothing is cached.
- **Long series**: for a whole season, a detected season pack covers the season
  in one torrent, and Torrentio queries are rate-limited to avoid throttling.
- **aria2**: files are foldered as `<Title>/Season NN/` for Jellyfin.
```
