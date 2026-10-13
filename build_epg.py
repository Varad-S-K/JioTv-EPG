#!/usr/bin/env python3
"""
Fetch JioTV EPG for every channel in JioTv_MOB.json and write XMLTV.

Usage:
  python build_epg.py
  python build_epg.py --days 3 --workers 6
  python build_epg.py --input channels.json --output jiotv.xml

No third-party Python packages are required.
"""
import argparse
import concurrent.futures
import gzip
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

CHANNELS_URL = (
    "https://raw.githubusercontent.com/Varad-S-K/Channel-Json/"
    "refs/heads/main/JioTv_MOB.json"
)
EPG_URL = "https://jiotv.data.cdn.jio.com/apis/v1.3/getepg/get"


def request_json(url, retries=3):
    last_error = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                  "AppleWebKit/537.36 Chrome/129 Safari/537.36",
                    "Referer": "https://www.jiotv.com/",
                },
            )
            with urllib.request.urlopen(req, timeout=25) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                return json.loads(response.read().decode("utf-8-sig"))
        except Exception as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep((1.5 ** attempt) + random.random() * 0.4)
    raise RuntimeError(f"Request failed after {retries} attempts: {url}: {last_error}")


def first_value(obj, keys, default=None):
    if not isinstance(obj, dict):
        return default
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return default


def find_channel_array(data):
    """Accept a JSON array or common wrappers containing an array."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("channels", "data", "result", "results", "channelList", "channel_list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        # Some APIs wrap arrays several levels down.
        for value in data.values():
            if isinstance(value, (list, dict)):
                try:
                    found = find_channel_array(value)
                    if found:
                        return found
                except ValueError:
                    pass
    raise ValueError("Could not find a channel array in the JSON. Inspect the input JSON structure.")


def normalize_channels(data):
    raw_channels = find_channel_array(data)
    channels, seen = [], set()
    for item in raw_channels:
        if not isinstance(item, dict):
            continue
        cid = first_value(item, (
            "id", "channel_id", "channelId", "channelid", "channelID", "channel_id_int"
        ))
        name = first_value(item, (
            "name", "channel_name", "channelName", "channelname", "channel"
        ))
        if cid is None:
            continue
        cid = str(cid).strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        channels.append({
            "id": cid,
            "name": str(name or cid).strip(),
            "logo": str(first_value(item, ("logo", "logoUrl", "logo_url", "channel_logo"), "")).strip(),
        })
    if not channels:
        raise ValueError(
            "No channels with recognized IDs were found. Expected keys such as id/channel_id "
            "and name/channel_name in JioTv_MOB.json."
        )
    return channels


def epoch_seconds(value):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 1e12:  # milliseconds
            n /= 1000
        return int(n)
    text = str(value).strip()
    if text.isdigit():
        n = int(text)
        return n // 1000 if n > 1e12 else n
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def normalize_epg_response(data, fallback_channel_id):
    if isinstance(data, dict):
        rows = data.get("epg")
        if not isinstance(rows, list):
            # Do not silently accept an unexpected API schema.
            raise ValueError("JioTV EPG JSON has no 'epg' array")
    elif isinstance(data, list):
        rows = data
    else:
        raise ValueError("Unexpected JioTV EPG response format")

    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = epoch_seconds(first_value(row, ("startEpoch", "start_epoch", "start", "startTime")))
        stop = epoch_seconds(first_value(row, ("endEpoch", "end_epoch", "end", "endTime")))
        title = first_value(row, ("showname", "title", "programName", "program_name"))
        if start is None or stop is None or stop <= start or not title:
            continue
        result.append({
            "channel_id": str(first_value(row, ("channel_id", "channelId"), fallback_channel_id)),
            "start": start,
            "stop": stop,
            "title": str(title).strip(),
            "description": str(first_value(row, ("description", "desc"), "")).strip(),
            "category": str(first_value(row, ("showCategory", "category"), "")).strip(),
            "icon": str(first_value(row, ("episodeThumbnail", "episodePoster", "icon"), "")).strip(),
        })
    return result


def fetch_one(args):
    channel, offset = args
    query = urllib.parse.urlencode({
        "offset": offset,
        "channel_id": channel["id"],
        "langId": 6,
    })
    url = f"{EPG_URL}?{query}"
    try:
        data = request_json(url)
        rows = normalize_epg_response(data, channel["id"])
        return channel["id"], offset, rows, None
    except Exception as exc:
        return channel["id"], offset, [], str(exc)


def xmltv_timestamp(seconds):
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def add_text(parent, tag, text, **attrs):
    node = SubElement(parent, tag, attrs)
    node.text = str(text)
    return node


def write_xmltv(channels, programmes, output_path):
    tv = Element("tv", {"generator-info-name": "JioTV EPG Builder"})
    for channel in channels:
        node = SubElement(tv, "channel", {"id": channel["id"]})
        add_text(node, "display-name", channel["name"])
        if channel.get("logo", "").startswith(("https://", "http://")):
            SubElement(node, "icon", {"src": channel["logo"]})

    seen = set()
    for p in sorted(programmes, key=lambda x: (x["channel_id"], x["start"], x["stop"])):
        key = (p["channel_id"], p["start"], p["stop"], p["title"])
        if key in seen:
            continue
        seen.add(key)
        node = SubElement(tv, "programme", {
            "start": xmltv_timestamp(p["start"]),
            "stop": xmltv_timestamp(p["stop"]),
            "channel": p["channel_id"],
        })
        add_text(node, "title", p["title"], lang="en")
        if p["description"]:
            add_text(node, "desc", p["description"], lang="en")
        if p["category"]:
            add_text(node, "category", p["category"], lang="en")
        if p["icon"].startswith(("https://", "http://")):
            SubElement(node, "icon", {"src": p["icon"]})

    # Write to a temporary file first so a failed run doesn't destroy the previous guide.
    temp_path = output_path + ".tmp"
    indent(tv, space="  ")
    ElementTree(tv).write(temp_path, encoding="utf-8", xml_declaration=True)
    os.replace(temp_path, output_path)

    gz_path = output_path + ".gz"
    with open(output_path, "rb") as source, gzip.open(gz_path + ".tmp", "wb", compresslevel=6) as target:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            target.write(block)
    os.replace(gz_path + ".tmp", gz_path)
    return len(seen), gz_path


def main():
    parser = argparse.ArgumentParser(description="Build an all-channel JioTV XMLTV EPG.")
    parser.add_argument("--input", default=CHANNELS_URL, help="Channel JSON URL or local JSON file")
    parser.add_argument("--output", default="jiotv.xml", help="Output XMLTV filename")
    parser.add_argument("--days", type=int, default=3, choices=range(1, 8),
                        help="Number of offsets to fetch (1-7; default 3)")
    parser.add_argument("--workers", type=int, default=5, choices=range(1, 13),
                        help="Concurrent requests (default 5; keep modest)")
    parser.add_argument("--fail-on-errors", action="store_true",
                        help="Do not replace output files if any channel request fails")
    args = parser.parse_args()

    print(f"Loading channels from {args.input}", flush=True)
    if args.input.startswith(("https://", "http://")):
        raw = request_json(args.input)
    else:
        with open(args.input, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)

    channels = normalize_channels(raw)
    print(f"Found {len(channels)} channels.", flush=True)
    jobs = [(channel, offset) for channel in channels for offset in range(args.days)]
    programmes, errors = [], []
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(fetch_one, job) for job in jobs]
        for future in concurrent.futures.as_completed(futures):
            cid, offset, rows, error = future.result()
            done += 1
            programmes.extend(rows)
            if error:
                errors.append((cid, offset, error))
            if done % 25 == 0 or done == len(jobs):
                print(f"Fetched {done}/{len(jobs)} channel-offset requests; "
                      f"{len(programmes)} programme rows so far.", flush=True)

    error_path = args.output + ".errors.txt"
    if errors and args.fail_on_errors:
        with open(error_path, "w", encoding="utf-8") as f:
            for cid, offset, error in errors:
                f.write(f"channel={cid} offset={offset}: {error}\n")
        print(
            f"ERROR: {len(errors)} of {len(jobs)} requests failed; output was not updated. "
            f"See {os.path.abspath(error_path)}.",
            file=sys.stderr,
        )
        for cid, offset, error in errors[:10]:
            print(f"  channel={cid} offset={offset}: {error}", file=sys.stderr)
        sys.exit(3)

    if not programmes:
        print("ERROR: No programme rows were returned. No output was written.", file=sys.stderr)
        if errors:
            with open(error_path, "w", encoding="utf-8") as f:
                for cid, offset, error in errors:
                    f.write(f"channel={cid} offset={offset}: {error}\n")
            print(f"Error log: {os.path.abspath(error_path)}", file=sys.stderr)
            print("First errors:", file=sys.stderr)
            for e in errors[:10]:
                print(f"  channel={e[0]} offset={e[1]}: {e[2]}", file=sys.stderr)
        sys.exit(2)

    count, gz_path = write_xmltv(channels, programmes, args.output)
    print(f"\nDone: {count} unique programme entries written.")
    print(f"XMLTV: {os.path.abspath(args.output)}")
    print(f"GZIP:  {os.path.abspath(gz_path)}")
    print(f"Failed requests: {len(errors)} of {len(jobs)}")
    if errors:
        with open(error_path, "w", encoding="utf-8") as f:
            for cid, offset, error in errors:
                f.write(f"channel={cid} offset={offset}: {error}\n")
        print(f"Error log: {os.path.abspath(error_path)}")
        print("Some channels/offsets failed; check the error log and rerun later.")
    elif os.path.exists(error_path):
        os.remove(error_path)


if __name__ == "__main__":
    main()
