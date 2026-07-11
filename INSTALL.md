# Installation Guide

Get TorrentDownloader running on a PC, any server, or a NAS (UGREEN example
included). The flow is always the same:

> **download codebase → put it on the machine → add your API keys → set up aria2 → done**

## How the pieces fit

```
You (browser) ──► TorrentDownloader web UI (port 8000)
                        │
                        ├─► Cinemeta ........ search & metadata (posters, episodes)
                        ├─► Torrentio ....... finds torrents, marks [RD+] cached
                        ├─► Real-Debrid ..... turns torrents into direct HTTPS links
                        └─► aria2 (RPC) ..... downloads those links to your disk
                                │
                                └─► /downloads   →  your media folder (Jellyfin etc.)
```

TorrentDownloader itself stores nothing — aria2 does the actual downloading,
so aria2 must run **on the machine that owns the storage** (your NAS/server).
The web UI can run on the same machine or a different one.

---

## Step 0 — What you need first

1. **A Real-Debrid account + API key** — get the key at
   <https://real-debrid.com/apitoken>.
2. **Your Torrentio config string** (this is what enables instant cached
   downloads):
   - Go to <https://torrentio.strem.fun/configure>
   - Under *Debrid provider* pick **Real-Debrid** and paste your API key
   - Click **Install** / copy the link — it looks like
     `https://torrentio.strem.fun/sort=qualitysize%7Crealdebrid=YOURKEY/manifest.json`
   - Your config string is the part **between the host and `/manifest.json`**,
     URL-decoded, e.g. `sort=qualitysize|realdebrid=YOURKEY`
3. **aria2** running somewhere with its RPC port reachable (Step 2 below).

---

## Step 1 — Get the codebase & add your keys

```bash
git clone https://github.com/NoahGram/TorrentDownloader.git
cd TorrentDownloader
cp .env.example .env
cp config.example.json config.json
```

Edit **`.env`** (this is the only file with secrets — it is gitignored):

```ini
RD_API_KEY=your_real_debrid_api_key
TORRENTIO_CONFIG=sort=qualitysize|realdebrid=your_real_debrid_api_key
ARIA2_HOST=http://IP_OF_THE_MACHINE_RUNNING_ARIA2
ARIA2_PORT=6800
ARIA2_SECRET=your_aria2_rpc_secret
ARIA2_DOWNLOAD_DIR=/downloads
```

> ⚠️ **`ARIA2_DOWNLOAD_DIR` is the folder as *aria2* sees it**, not as this app
> sees it. If aria2 runs in Docker with `-v /volume1/Theater:/downloads`, the
> correct value is `/downloads` and the files really land in
> `/volume1/Theater`.

`config.json` holds the non-secret preferences (quality, delays, profile) —
the defaults are sensible, tune later if you like.

---

## Step 2 — Set up aria2

### On a NAS / server (Docker — recommended)

Use the excellent [`p3terx/aria2-pro`](https://hub.docker.com/r/p3terx/aria2-pro) image:

```yaml
# aria2-compose.yml
services:
  aria2:
    image: p3terx/aria2-pro:latest
    container_name: aria2
    restart: unless-stopped
    environment:
      - RPC_SECRET=pick_a_secret        # same value as ARIA2_SECRET in .env
      - RPC_PORT=6800
    ports:
      - "6800:6800"
    volumes:
      - /path/to/your/media/folder:/downloads   # e.g. your Theater folder
```

```bash
docker compose -f aria2-compose.yml up -d
```

### On a PC (bare metal)

```bash
# Debian/Ubuntu:  sudo apt install aria2
# Fedora:         sudo dnf install aria2
# macOS:          brew install aria2

aria2c --enable-rpc --rpc-listen-all --rpc-secret=pick_a_secret \
       --dir=/path/to/your/media/folder
```

Then in `.env`: `ARIA2_HOST=http://127.0.0.1`, `ARIA2_DOWNLOAD_DIR=` the same
`--dir` you passed (here the app and aria2 see the same real path).

---

## Step 3 — Run TorrentDownloader

### Option A — Docker (server / NAS)

```bash
docker compose up --build -d
```

That's it. The compose file reads `.env` automatically and mounts
`config.json`. Open `http://<machine-ip>:8000`.

### Option B — Directly with Python (PC)

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python web_server.py
```

Open `http://localhost:8000`.

---

## UGREEN NAS walkthrough (Docker UI)

This matches a real working setup — two containers on the NAS:

**1. aria2 container** (`p3terx/aria2-pro`)
- Volume: `Personal folder/Theater` → `/downloads` (Read/write)
- Port: `6800` (or another, e.g. `6801`) → `6800`
- Environment: `RPC_SECRET=<your secret>`

**2. TorrentDownloader** — created as a Docker **Project** (compose):
- Put the whole codebase in a shared folder, e.g.
  `Shared folder/docker/TorrentDownloader/`
- Create `.env` and `config.json` in that same folder (Step 1)
- In `.env` point at the NAS itself:
  ```ini
  ARIA2_HOST=http://<NAS-LAN-IP>     # e.g. http://192.168.178.35
  ARIA2_PORT=6801                    # the *NAS* port you mapped for aria2
  ARIA2_DOWNLOAD_DIR=/downloads      # aria2's container path = Theater folder
  ```
- In the UGREEN Docker app: **Project → Create → pick the folder** (it uses the
  repo's `docker-compose.yml`) → Deploy
- Open `http://<NAS-IP>:8000`

### Upgrading an existing UGREEN install

The container bakes the code in at build time, so after pulling new code you
must **rebuild**, not just restart:

1. Replace the codebase files in `Shared folder/docker/TorrentDownloader/`
   with the new version (or `git pull` over SSH)
2. Make sure `.env` exists next to `docker-compose.yml` (new requirement —
   older versions kept everything in `config.json`)
3. Rebuild the project: in the UGREEN Docker app rebuild/redeploy the project,
   or over SSH:
   ```bash
   cd /volume1/docker/TorrentDownloader
   docker compose up --build -d
   ```

---

## Checklist — "is it working?"

| Check | How |
|-------|-----|
| Web UI up | `http://<ip>:8000` loads, header shows **RD Active** and **aria2 → NAS** |
| RD key valid | `/api/status` returns `"rd": true` |
| aria2 reachable | Start any small movie download — log shows `🚀 Sent cached file to aria2` |
| Files land right | Look in your media folder: `Movie (Year)/file.mkv` or `Series/Season 01/...` |

## Troubleshooting

- **Search works but downloads fail / no streams** — your `TORRENTIO_CONFIG`
  is missing or has no `realdebrid=` part. The log warns about this at the
  start of every download.
- **`❌ aria2 failed`** — check `ARIA2_HOST`/`ARIA2_PORT`/`ARIA2_SECRET`
  match the aria2 container, and that the port is actually published. From the
  NAS: `curl http://<host>:<port>/jsonrpc` should answer (400/405 is fine —
  it means aria2 is there).
- **Files land in the wrong folder** — remember `ARIA2_DOWNLOAD_DIR` is
  aria2's *container* path. Change the aria2 volume mapping, not the app.
- **Everything queues but nothing is instant** — the title simply isn't cached
  on Real-Debrid; the app adds it to RD and syncs when RD finishes.
- **HTTP 403 from Torrentio** — you're likely running an old version; current
  code sends a browser User-Agent (Cloudflare blocks non-browser clients).
