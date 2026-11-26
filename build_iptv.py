#!/usr/bin/env python3
"""Fetch iptv-org API data and build combined CN + HK + TW playlists for TVBox."""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path

API_BASE = "https://iptv-org.github.io/api"
OUTPUT_DIR = Path(__file__).resolve().parent

REGION_ORDER = ("中国大陆", "中国香港", "中国台湾")
COUNTRY_TO_REGION = {
    "CN": "中国大陆",
    "HK": "中国香港",
    "TW": "中国台湾",
}
AREA_PREFIX_TO_REGION = (
    (("c/CN", "s/CN-"), "中国大陆"),
    (("c/HK", "s/HK-"), "中国香港"),
    (("c/TW", "s/TW-"), "中国台湾"),
)
CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def fetch_json(endpoint: str) -> list[dict]:
    url = f"{API_BASE}/{endpoint}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def pick_region(country: str | None, broadcast_area: list[str]) -> str | None:
    if country in COUNTRY_TO_REGION:
        return COUNTRY_TO_REGION[country]
    for area in broadcast_area:
        for prefixes, region in AREA_PREFIX_TO_REGION:
            if area in prefixes or area.startswith(prefixes):
                return region
    return None


def pick_display_name(channel: dict) -> str:
    """Use a single canonical name per channel (CJK alt_name preferred) so TVBox merges backup lines."""
    alt_names = channel.get("alt_names") or []
    for alt in alt_names:
        if CJK_RE.search(alt):
            return alt.strip()
    return (channel.get("name") or "").strip()


def build_logo_lookup(logos: list[dict]) -> dict[tuple[str, str | None], str]:
    lookup: dict[tuple[str, str | None], str] = {}
    for logo in logos:
        cid = logo.get("channel")
        url = logo.get("url")
        if not cid or not url:
            continue
        key = (cid, logo.get("feed"))
        if key not in lookup or logo.get("in_use"):
            lookup[key] = url
    return lookup


def sanitize_vlcopt(value: str, prefix: str) -> str:
    cleaned = value.strip()
    if cleaned.startswith(prefix):
        cleaned = cleaned[len(prefix) :]
    return cleaned


def main() -> None:
    parser = argparse.ArgumentParser(description="Build combined CN/HK/TW TVBox playlists.")
    parser.add_argument(
        "--base-url",
        default=".",
        help="Base URL prefix used in tvbox_live.json (e.g. https://auroragzyx.github.io/tvbox-iptv)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    channels = fetch_json("channels.json")
    streams = fetch_json("streams.json")
    feeds = fetch_json("feeds.json")
    logos = fetch_json("logos.json")

    ch_map = {c["id"]: c for c in channels if c.get("id")}
    feed_map = {(f["channel"], f["id"]): f for f in feeds if f.get("channel")}
    logo_map = build_logo_lookup(logos)

    grouped: dict[str, list[dict]] = {region: [] for region in REGION_ORDER}
    seen_urls: set[str] = set()

    for stream in streams:
        cid = stream.get("channel")
        url = (stream.get("url") or "").strip()
        if not cid or not url or url in seen_urls:
            continue
        channel = ch_map.get(cid)
        if not channel or channel.get("is_nsfw"):
            continue

        feed_id = stream.get("feed")
        feed = feed_map.get((cid, feed_id), {})
        region = pick_region(channel.get("country"), feed.get("broadcast_area") or [])
        if not region:
            continue

        seen_urls.add(url)
        name = pick_display_name(channel)
        quality = (stream.get("quality") or "").strip()
        labels = [lbl.strip() for lbl in (stream.get("labels") or []) if lbl.strip()]
        logo_url = logo_map.get((cid, feed_id)) or logo_map.get((cid, None)) or ""
        tvg_id = f"{cid}@{feed_id}" if feed_id else cid

        grouped[region].append(
            {
                "region": region,
                "name": name,
                "quality": quality,
                "labels": labels,
                "url": url,
                "tvg_id": tvg_id,
                "logo": logo_url,
                "user_agent": stream.get("user_agent") or "",
                "referrer": stream.get("referrer") or "",
            }
        )

    for region in REGION_ORDER:
        grouped[region].sort(key=lambda item: (item["name"].lower(), item["quality"], item["url"]))

    m3u_lines = ["#EXTM3U"]
    txt_lines: list[str] = []

    for region in REGION_ORDER:
        items = grouped[region]
        if not items:
            continue
        txt_lines.append(f"{region},#genre#")
        for item in items:
            suffix_parts = []
            if item["quality"]:
                suffix_parts.append(f"({item['quality']})")
            for lbl in item["labels"]:
                suffix_parts.append(f"[{lbl}]")
            m3u_title = f"{item['name']} {' '.join(suffix_parts)}".strip()

            attrs = [f'tvg-id="{item["tvg_id"]}"', f'tvg-name="{item["name"]}"']
            if item["logo"]:
                attrs.append(f'tvg-logo="{item["logo"]}"')
            attrs.append(f'group-title="{region}"')

            m3u_lines.append(f"#EXTINF:-1 {' '.join(attrs)},{m3u_title}")
            if item["user_agent"]:
                ua = sanitize_vlcopt(item["user_agent"], "#EXTVLCOPT:http-user-agent=")
                m3u_lines.append(f"#EXTVLCOPT:http-user-agent={ua}")
            if item["referrer"]:
                ref = sanitize_vlcopt(item["referrer"], "#EXTVLCOPT:http-referrer=")
                m3u_lines.append(f"#EXTVLCOPT:http-referrer={ref}")
            m3u_lines.append(item["url"])

            # TVBox TXT uses exact channel name so multiple URLs merge into switchable lines.
            txt_name = item["name"].replace(",", " ")
            txt_lines.append(f"{txt_name},{item['url']}")
        txt_lines.append("")

    m3u_path = OUTPUT_DIR / "cn_hk_tw.m3u"
    txt_path = OUTPUT_DIR / "cn_hk_tw.txt"
    json_path = OUTPUT_DIR / "tvbox_live.json"

    m3u_path.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")

    tvbox_cfg = {
        "lives": [
            {
                "name": "中国大陆·香港·台湾 (M3U)",
                "type": 1,
                "url": f"{base_url}/cn_hk_tw.m3u",
                "playerType": 1,
                "ua": "okhttp/3.15",
            },
            {
                "name": "中国大陆·香港·台湾 (TXT)",
                "type": 0,
                "url": f"{base_url}/cn_hk_tw.txt",
                "playerType": 1,
            },
        ]
    }
    json_path.write_text(json.dumps(tvbox_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = sum(len(grouped[r]) for r in REGION_ORDER)
    print(f"Generated {total} streams:")
    for region in REGION_ORDER:
        print(f"  {region}: {len(grouped[region])}")
    print(f"Wrote: {m3u_path}")
    print(f"Wrote: {txt_path}")
    print(f"Wrote: {json_path}")


if __name__ == "__main__":
    main()
