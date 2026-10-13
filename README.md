# JioTV EPG Builder (TiviMate XMLTV)

This package includes:
- `build_epg.py`: runs on Windows/macOS/Linux and writes `jiotv.xml` plus `jiotv.xml.gz`.
- `.github/workflows/update-epg.yml`: rebuilds both XMLTV files every two hours and commits changes.
- `cloudflare-worker/`: a JavaScript Cloudflare Worker that gradually refreshes batches and serves `/epg.xml`.

It uses the channel IDs from:
`https://raw.githubusercontent.com/Varad-S-K/Channel-Json/refs/heads/main/JioTv_MOB.json`

and calls:
`https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get?offset=N&channel_id=ID&langId=6`

## Option A — build a file on Windows

1. Install Python 3.10+ from https://www.python.org/downloads/windows/ and enable "Add Python to PATH".
2. Extract this ZIP.
3. Open Command Prompt in the extracted folder.
4. Run:
   ```bat
   py build_epg.py
   ```
5. It writes `jiotv.xml` and `jiotv.xml.gz`. Upload `jiotv.xml` to a host that provides a stable public HTTPS URL, or keep regenerating the file and upload it when needed.
6. To fetch more/less offsets:
   ```bat
   py build_epg.py --days 3 --workers 5
   ```
   Offset interpretation is based on the endpoint's behavior; this requests offsets 0, 1, and 2.
7. If some requests fail, see `jiotv.xml.errors.txt` and rerun. The previous completed output is preserved if a run returns no programme rows.

## Automatic GitHub Actions updates

The `Update JioTV EPG` workflow runs every two hours (UTC) and can also be started from the repository's **Actions** tab. It builds an indented `jiotv.xml` and its compressed `jiotv.xml.gz`, then commits them only when their contents change. Scheduled runs use `--fail-on-errors`, so a partial API failure leaves the previous XML files untouched and fails the workflow; the request details are written to `jiotv.xml.errors.txt`. Ensure GitHub Actions is enabled and the workflow's repository token has permission to write contents.

## Option B — automatic URL on Cloudflare Workers

1. Create a Workers KV namespace named `EPG_KV` in Cloudflare.
2. Copy its namespace ID into `cloudflare-worker/wrangler.toml`, replacing the placeholder.
3. Install Node.js from https://nodejs.org/.
4. Open a terminal in `cloudflare-worker` and run:
   ```bash
   npm install -g wrangler
   wrangler login
   wrangler deploy
   ```
5. Check `https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev/status`.
6. Once all batches are refreshed, add `https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev/epg.xml` to TiviMate as an XMLTV EPG URL.

The Worker refreshes 10 channels per 10-minute cron run, with offsets 0, 1, 2, and serves a two-space-indented XMLTV document from `/epg.xml`. For a channel list with N channels, one full pass takes about `ceil(N/10) * 10` minutes. A full pass is needed before all channels have been populated. After that, it refreshes each batch in rotation.

Cloudflare Workers typically run JavaScript/TypeScript; this is why the Cloudflare version is JavaScript, while the local version is Python. Free-plan quotas can change, so check current Cloudflare Workers and KV limits before deploying.

## TiviMate channel matching

For channel `144`, your playlist should use `tvg-id="144"` if that is the ID in your JSON. The XMLTV `<channel id="144">` needs to match that exact ID. If your M3U uses different `tvg-id` values, change those values or modify the XMLTV IDs accordingly.

## Troubleshooting

- `No channel IDs found`: inspect the JSON and add its actual ID/name field names to `normalize_channels()` (Python) or `first()` (Worker).
- `EPG JSON has no 'epg' array`: the API response schema changed; inspect one successful response and update the parser.
- 404/403/429: endpoint availability, headers, channel ID, or rate limiting may be involved. Reduce concurrency and retry later.
- The EPG does not provide video streams; it only supplies programme guide metadata.
- This uses an unofficial, undocumented Jio API endpoint. It may change or restrict requests.
