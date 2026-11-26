#!/usr/bin/env python3
"""Aggregate domestic fast IPTV sources + iptv-org CN/HK/TW, probe streams, and output TVBox playlists."""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent
API_BASE = "https://iptv-org.github.io/api"

DOMESTIC_M3U_SOURCES = (
    ("vbskycn_v4", "https://live.zbds.top/tv/iptv4.m3u"),
    ("china_iptv", "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/cnTV_AutoUpdate.m3u8"),
    ("ccsh_live", "https://raw.githubusercontent.com/CCSH/IPTV/main/live.m3u"),
    ("suxuang_v4", "https://raw.githubusercontent.com/suxuang/myIPTV/main/ipv4.m3u"),
)

GROUP_ORDER = ("央视频道", "卫视频道", "中国香港", "中国台湾", "Nickelodeon", "地方与特色")

CCTV_LABELS = {
    "1": "CCTV-1 综合",
    "2": "CCTV-2 财经",
    "3": "CCTV-3 综艺",
    "4": "CCTV-4 中文国际",
    "5": "CCTV-5 体育",
    "5+": "CCTV-5+ 体育赛事",
    "6": "CCTV-6 电影",
    "7": "CCTV-7 国防军事",
    "8": "CCTV-8 电视剧",
    "9": "CCTV-9 纪录",
    "10": "CCTV-10 科教",
    "11": "CCTV-11 戏曲",
    "12": "CCTV-12 社会与法",
    "13": "CCTV-13 新闻",
    "14": "CCTV-14 少儿",
    "15": "CCTV-15 音乐",
    "16": "CCTV-16 奥林匹克",
    "17": "CCTV-17 农业农村",
    "4K": "CCTV-4K 超高清",
    "8K": "CCTV-8K 超高清",
}

HK_KEYWORDS = (
    "香港", "翡翠", "明珠", "无线", "無綫", "hoy", "viutv", "rthk",
    "凤凰", "鳳凰", "phoenix", "亚视", "亞視", "now", "有线", "有線", "j2",
    "美亚", "美亞", "寰亚", "astro", "千禧", "星河", "澳视", "澳廣視", "莲花", "蓮花",
    "港台電視", "港台电视",
)
TW_KEYWORDS = (
    "台湾", "台灣", "taiwan", "中天", "民视", "民視", "东森", "東森", "台视", "台視",
    "中视", "中視", "华视", "華視", "三立", "tvbs", "年代", "壹电视", "壹電視",
    "公视", "公視", "大爱", "大愛", "纬来", "緯來", "八大", "非凡", "寰宇",
    "镜新闻", "鏡新聞", "好消息", "人间卫视", "人間衛視", "原住民", "客家",
    "momo", "animax", "axn", "cinemax", "hbo", "cgntv", "ntdtv", "et mall",
    "genius go", "霹雳", "龍華", "龙华", "靖天", "博斯", "ttv", "set news",
)

BAD_NAME_KEYWORDS = ("更新时间", "公告", "支持作者", "公众号", "微信", "qq群", "免责", "测试")
BAD_URL_PATTERNS = ("youtube.com/", "youtu.be/", ".swf", ".html", "player.cntv.cn/")

SATELLITE_RE = re.compile(
    r"(湖南|浙江|江苏|江蘇|东方|東方|北京|广东|廣東|深圳|安徽|山东|山東|湖北|四川|"
    r"黑龙江|黑龍江|辽宁|遼寧|天津|东南|東南|厦门|廈門|海峡|海峽|海南|广西|廣西|"
    r"重庆|重慶|河南|河北|山西|陕西|陝西|江西|吉林|云南|雲南|贵州|貴州|甘肃|甘肅|"
    r"宁夏|寧夏|青海|新疆|西藏|内蒙古|內蒙古|兵团|兵團|延边|延邊|三沙|农林|農林|"
    r"卡酷|金鹰|金鷹|炫动|炫動|优漫|優漫|嘉佳|康巴|安多|黄河|黃河)卫视"
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
QUALITY_NOISE_RE = re.compile(
    r"(\[[^\]]*\]|\([^\)]*\)|\b(1080p|720p|576p|480p|576i|480i|hd|fhd|sd|uhd|4k|8k|hevc|h265)\b|"
    r"高清|超清|标清|流畅|备\d*|备用\d*|测试|\s+)",
    re.IGNORECASE,
)


def fetch_text(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def clean_stream_url(raw_url: str) -> list[str]:
    """Handle occasional '$label' or ';http' packed entries in community M3Us."""
    url = raw_url.strip().split("$", 1)[0].strip()
    parts = [p.strip() for p in url.split(";") if p.strip().startswith(("http://", "https://", "rtmp://"))]
    return parts if parts else ([url] if url else [])


def normalize_channel_name(raw_name: str) -> str:
    name = raw_name.strip()
    # Clean common suffixes like "-1080P", "_HD", "(720p)"
    no_noise = QUALITY_NOISE_RE.sub("", name)
    upper = no_noise.upper()

    # Match CCTV-1 .. CCTV-17, CCTV-5+, CCTV-4K, CCTV-8K
    m = re.match(r"^CCTV[-_ ]?(5\+|4K|8K|1[0-7]|[1-9])(?:[^\d+].*)?$", upper)
    if m:
        key = m.group(1)
        return CCTV_LABELS.get(key, f"CCTV-{key}")
    if upper.startswith("CGTN"):
        return name.split("(", 1)[0].split("[", 1)[0].strip()
    if upper.startswith("CETV"):
        m_cetv = re.match(r"^CETV[-_ ]?([1-4])", upper)
        if m_cetv:
            return f"CETV-{m_cetv.group(1)}"

    # Normalize satellite TV names (e.g. "湖南卫视高清" -> "湖南卫视")
    m_sat = SATELLITE_RE.search(no_noise)
    if m_sat:
        return m_sat.group(0)

    # Strip bracketed resolution/status noise for cleaner grouping
    cleaned = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", name).strip()
    cleaned = re.sub(r"[-_ ]?(HD|FHD|SD|1080P|720P|4K)$", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or name


def classify_group(name: str, src_group: str, region_hint: str | None) -> str:
    low_name = name.lower()
    low_grp = src_group.lower()

    if region_hint == "中国台湾" or any(k in low_name for k in TW_KEYWORDS) or "台湾" in low_grp or "台灣" in low_grp:
        return "中国台湾"
    if (
        region_hint == "中国香港"
        or any(k in low_name for k in HK_KEYWORDS)
        or bool(re.search(r"\btvb\b|tvb[\u4e00-\u9fff]|tvb(?:pearl|plus)", low_name))
        or "香港" in low_grp
    ):
        return "中国香港"
    if "卫视" in name or "衛視" in name:
        return "卫视频道"
    if name.startswith(("CCTV-", "CETV-", "CGTN")) or "cctv" in low_name or "央视" in low_grp:
        return "央视频道"
    if "卫视" in low_grp:
        return "卫视频道"
    if "港澳" in low_grp:
        return "中国香港"
    return "地方与特色"


def parse_m3u_entries(text: str, source_name: str) -> list[dict]:
    entries = []
    cur: dict | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            grp_m = re.search(r'group-title="([^"]*)"', line)
            logo_m = re.search(r'tvg-logo="([^"]*)"', line)
            id_m = re.search(r'tvg-id="([^"]*)"', line)
            raw_name = line.split(",", 1)[-1].strip()
            cur = {
                "raw_name": raw_name,
                "src_group": grp_m.group(1) if grp_m else "",
                "logo": logo_m.group(1) if logo_m else "",
                "tvg_id": id_m.group(1) if id_m else "",
                "user_agent": "",
                "referrer": "",
                "source": source_name,
            }
        elif line.startswith("#EXTVLCOPT:http-user-agent=") and cur:
            cur["user_agent"] = line.split("=", 1)[1].strip()
        elif line.startswith("#EXTVLCOPT:http-referrer=") and cur:
            cur["referrer"] = line.split("=", 1)[1].strip()
        elif not line.startswith("#") and cur:
            for url in clean_stream_url(line):
                item = dict(cur)
                item["url"] = url
                entries.append(item)
            cur = None
    return entries


def load_iptv_org_entries() -> tuple[list[dict], list[dict]]:
    channels = json.loads(fetch_text(f"{API_BASE}/channels.json"))
    streams = json.loads(fetch_text(f"{API_BASE}/streams.json"))
    feeds = json.loads(fetch_text(f"{API_BASE}/feeds.json"))
    logos = json.loads(fetch_text(f"{API_BASE}/logos.json"))

    ch_map = {c["id"]: c for c in channels if c.get("id")}
    feed_map = {(f["channel"], f["id"]): f for f in feeds if f.get("channel")}
    logo_map: dict[tuple[str, str | None], str] = {}
    for logo in logos:
        cid, lurl = logo.get("channel"), logo.get("url")
        if cid and lurl:
            key = (cid, logo.get("feed"))
            if key not in logo_map or logo.get("in_use"):
                logo_map[key] = lurl

    nick_cids = {
        c["id"]
        for c in channels
        if "nickelodeon"
        in " ".join(
            [
                c.get("id") or "",
                c.get("name") or "",
                c.get("network") or "",
                " ".join(c.get("alt_names") or []),
                " ".join(c.get("owners") or []),
            ]
        ).lower()
    }

    cn_hk_tw_entries = []
    nick_entries = []
    for s in streams:
        cid = s.get("channel")
        url = (s.get("url") or "").strip()
        if not url:
            continue
        ch = ch_map.get(cid) if cid else None
        if ch and ch.get("is_nsfw"):
            continue

        stream_feed = s.get("feed")
        ua = (s.get("user_agent") or "").strip()
        if ua.startswith("#EXTVLCOPT:http-user-agent="):
            ua = ua[len("#EXTVLCOPT:http-user-agent=") :]
        ref = (s.get("referrer") or "").strip()
        if ref.startswith("#EXTVLCOPT:http-referrer="):
            ref = ref[len("#EXTVLCOPT:http-referrer=") :]
        logo = (logo_map.get((cid, stream_feed)) or logo_map.get((cid, None)) or "") if cid else ""
        tvg_id = (f"{cid}@{stream_feed}" if stream_feed else cid) if cid else ""

        title_lower = (s.get("title") or "").lower()
        if (cid and cid in nick_cids) or ("nickelodeon" in title_lower):
            base_title = (s.get("title") or (ch.get("name") if ch else "") or "Nickelodeon").strip()
            cc = (ch.get("country") if ch else "") or ""
            quality = (s.get("quality") or "").strip()
            nick_name = f"{base_title} [{cc}]" if cc else base_title
            if quality:
                nick_name = f"{nick_name} ({quality})"
            nick_entries.append(
                {
                    "raw_name": nick_name,
                    "name": nick_name,
                    "country": cc,
                    "src_group": "Nickelodeon",
                    "region_hint": "Nickelodeon",
                    "logo": logo,
                    "tvg_id": tvg_id,
                    "user_agent": ua,
                    "referrer": ref,
                    "url": url,
                    "source": "iptv_org_nick",
                }
            )

        if not ch:
            continue
        ba = feed_map.get((cid, stream_feed), {}).get("broadcast_area") or []
        cc = ch.get("country")

        region_hint = None
        if cc == "HK" or any(a.startswith(("c/HK", "s/HK-")) for a in ba):
            region_hint = "中国香港"
        elif cc == "TW" or any(a.startswith(("c/TW", "s/TW-")) for a in ba):
            region_hint = "中国台湾"
        elif cc == "CN" or any(a.startswith(("c/CN", "s/CN-")) for a in ba):
            region_hint = "中国大陆"
        if not region_hint:
            continue

        raw_name = next((a.strip() for a in (ch.get("alt_names") or []) if CJK_RE.search(a)), (ch.get("name") or "").strip())
        cn_hk_tw_entries.append(
            {
                "raw_name": raw_name,
                "src_group": "",
                "region_hint": region_hint,
                "logo": logo,
                "tvg_id": tvg_id,
                "user_agent": ua,
                "referrer": ref,
                "url": url,
                "source": "iptv_org",
            }
        )
    return cn_hk_tw_entries, nick_entries


def probe_entry(entry: dict, timeout: float = 2.5) -> tuple[bool, int]:
    """Verify URL responds cleanly and (for HLS) serves a real playlist + reachable first segment."""
    url = entry["url"]
    low_url = url.lower()
    if not url.startswith(("http://", "https://")):
        return False, 9999
    if "://[" in url or "/audio/" in low_url or any(p in low_url for p in BAD_URL_PATTERNS):
        return False, 9999

    headers = {"User-Agent": entry["user_agent"] or "okhttp/3.15"}
    if entry["referrer"]:
        headers["Referer"] = entry["referrer"]

    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype or "shockwave-flash" in ctype:
                return False, 9999
            final_url = resp.geturl()
            data = resp.read(32768)
        ms = int((time.time() - t0) * 1000)
        if not data or b"<html" in data[:512].lower() or b"<!doctype" in data[:512].lower():
            return False, 9999

        if b"#EXTM3U" in data:
            text = data.decode("utf-8", errors="ignore")
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if not lines:
                return False, 9999
            seg_url = urllib.parse.urljoin(final_url, lines[0])
            t1 = time.time()
            seg_req = urllib.request.Request(seg_url, headers=headers)
            with urllib.request.urlopen(seg_req, timeout=timeout) as seg_resp:
                seg_ctype = (seg_resp.headers.get("Content-Type") or "").lower()
                if "text/html" in seg_ctype:
                    return False, 9999
                seg_data = seg_resp.read(8192)
            ms += int((time.time() - t1) * 1000)
            if len(seg_data) < 64:
                return False, 9999
            return True, ms

        # Direct FLV / MPEG-TS stream (check FLV magic or MPEG-TS 0x47 sync byte)
        if data[:3] == b"FLV" or (len(data) >= 4096 and (data[0] == 0x47 or "video/" in ctype or "octet-stream" in ctype)):
            return True, ms
        return False, 9999
    except Exception:
        return False, 9999


def cctv_sort_key(name: str) -> tuple[int, int, str]:
    m = re.match(r"^CCTV-(\d+)", name)
    if m:
        return (0, int(m.group(1)), name)
    if name.startswith("CCTV-4K"):
        return (1, 0, name)
    if name.startswith("CCTV-8K"):
        return (1, 1, name)
    if name.startswith("CETV-"):
        return (2, 0, name)
    return (3, 0, name)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build fast combined CN/HK/TW TVBox playlists.")
    parser.add_argument("--base-url", default="https://auroragzyx.github.io/tvbox-iptv")
    parser.add_argument("--speed-test", action="store_true", help="Probe every stream locally and rank by speed.")
    parser.add_argument("--max-per-channel", type=int, default=5, help="Keep top N fastest lines per channel.")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    raw_entries: list[dict] = []
    for src_name, src_url in DOMESTIC_M3U_SOURCES:
        try:
            text = fetch_text(src_url)
            parsed = parse_m3u_entries(text, src_name)
            print(f"Fetched {src_name}: {len(parsed)} entries")
            raw_entries.extend(parsed)
        except Exception as exc:
            print(f"WARN: failed {src_name}: {exc}")

    nick_entries: list[dict] = []
    try:
        org_entries, nick_entries = load_iptv_org_entries()
        print(f"Fetched iptv_org: {len(org_entries)} entries, Nickelodeon: {len(nick_entries)} entries")
        raw_entries.extend(org_entries)
    except Exception as exc:
        print(f"WARN: failed iptv_org: {exc}")

    # Deduplicate by URL and normalize metadata
    dedup: list[dict] = []
    seen_urls: set[str] = set()
    logo_by_name: dict[str, str] = {}

    for e in raw_entries:
        url = e["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        norm_name = normalize_channel_name(e["raw_name"])
        if not norm_name or any(k in norm_name.lower() for k in BAD_NAME_KEYWORDS):
            continue
        group = classify_group(norm_name, e.get("src_group", ""), e.get("region_hint"))
        e["name"] = norm_name
        e["group"] = group
        if e.get("logo") and norm_name not in logo_by_name:
            logo_by_name[norm_name] = e["logo"]
        dedup.append(e)

    # Deduplicate Nickelodeon entries separately (keep original country/quality tags)
    nick_dedup: list[dict] = []
    nick_seen: set[str] = set()
    for e in nick_entries:
        url = e["url"]
        if url in nick_seen:
            continue
        nick_seen.add(url)
        e["group"] = "Nickelodeon"
        nick_dedup.append(e)

    print(f"Unique candidate URLs: {len(dedup)} (CN/HK/TW) + {len(nick_dedup)} (Nickelodeon)")

    if args.speed_test:
        print("Running real stream + first-segment speed test...")
        with ThreadPoolExecutor(max_workers=64) as ex:
            results = list(ex.map(probe_entry, dedup))
            nick_results = list(ex.map(probe_entry, nick_dedup))
        verified: list[dict] = []
        for entry, (ok, ms) in zip(dedup, results):
            if ok:
                penalty = 800 if "raw.githubusercontent.com" in entry["url"] else 0
                entry["latency_ms"] = ms
                entry["rank_score"] = ms + penalty
                verified.append(entry)
        nick_verified: list[dict] = []
        for entry, (ok, ms) in zip(nick_dedup, nick_results):
            if ok:
                entry["latency_ms"] = ms
                entry["rank_score"] = ms
                nick_verified.append(entry)
        print(f"Verified working streams: {len(verified)} / {len(dedup)} (CN/HK/TW), {len(nick_verified)} / {len(nick_dedup)} (Nickelodeon)")
        candidates = verified
        nick_candidates = nick_verified
    else:
        for idx, entry in enumerate(dedup):
            entry["latency_ms"] = 0
            entry["rank_score"] = idx
        for idx, entry in enumerate(nick_dedup):
            entry["latency_ms"] = 0
            entry["rank_score"] = idx
        candidates = dedup
        nick_candidates = nick_dedup

    # Prioritize English/Asia Nickelodeon feeds first, then sort by latency
    en_countries = {"SG", "US", "GB", "AU", "CA", ""}
    nick_candidates.sort(
        key=lambda x: (0 if x.get("country", "") in en_countries else 1, x["rank_score"], x["name"])
    )

    # Group by (group, channel_name), keep top N fastest per channel
    by_channel: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for entry in candidates:
        if not entry.get("logo") and entry["name"] in logo_by_name:
            entry["logo"] = logo_by_name[entry["name"]]
        by_channel[(entry["group"], entry["name"])].append(entry)

    grouped_output: dict[str, list[dict]] = {g: [] for g in GROUP_ORDER}
    for (grp, cname), items in by_channel.items():
        items.sort(key=lambda x: (x["rank_score"], x["url"]))
        grouped_output[grp].extend(items[: args.max_per_channel])
    grouped_output["Nickelodeon"] = list(nick_candidates)

    def render_playlists(group_list: list[str], data_map: dict[str, list[dict]]) -> tuple[list[str], list[str]]:
        m3u_out = ["#EXTM3U"]
        txt_out: list[str] = []
        for grp in group_list:
            items = data_map.get(grp, [])
            if not items:
                continue
            if grp == "央视频道":
                items.sort(key=lambda x: (cctv_sort_key(x["name"]), x["rank_score"]))
            elif grp != "Nickelodeon":
                items.sort(key=lambda x: (x["name"], x["rank_score"]))

            txt_out.append(f"{grp},#genre#")
            for item in items:
                attrs = []
                if item.get("tvg_id"):
                    attrs.append(f'tvg-id="{item["tvg_id"]}"')
                attrs.append(f'tvg-name="{item["name"]}"')
                if item.get("logo"):
                    attrs.append(f'tvg-logo="{item["logo"]}"')
                attrs.append(f'group-title="{grp}"')

                m3u_out.append(f"#EXTINF:-1 {' '.join(attrs)},{item['name']}")
                if item.get("user_agent"):
                    m3u_out.append(f"#EXTVLCOPT:http-user-agent={item['user_agent']}")
                if item.get("referrer"):
                    m3u_out.append(f"#EXTVLCOPT:http-referrer={item['referrer']}")
                m3u_out.append(item["url"])

                txt_name = item["name"].replace(",", " ")
                txt_out.append(f"{txt_name},{item['url']}")
            txt_out.append("")
        return m3u_out, txt_out

    m3u_lines, txt_lines = render_playlists(list(GROUP_ORDER), grouped_output)
    nick_m3u_lines, nick_txt_lines = render_playlists(["Nickelodeon"], grouped_output)

    m3u_path = OUTPUT_DIR / "cn_hk_tw.m3u"
    txt_path = OUTPUT_DIR / "cn_hk_tw.txt"
    nick_m3u_path = OUTPUT_DIR / "nickelodeon.m3u"
    nick_txt_path = OUTPUT_DIR / "nickelodeon.txt"
    json_path = OUTPUT_DIR / "tvbox_live.json"

    m3u_path.write_text("\n".join(m3u_lines) + "\n", encoding="utf-8")
    txt_path.write_text("\n".join(txt_lines), encoding="utf-8")
    nick_m3u_path.write_text("\n".join(nick_m3u_lines) + "\n", encoding="utf-8")
    nick_txt_path.write_text("\n".join(nick_txt_lines), encoding="utf-8")

    tvbox_cfg = {
        "lives": [
            {
                "name": "央视·卫视·港澳台·Nickelodeon (极速M3U)",
                "type": 1,
                "url": f"{base_url}/cn_hk_tw.m3u",
                "playerType": 1,
                "ua": "okhttp/3.15",
            },
            {
                "name": "央视·卫视·港澳台·Nickelodeon (极速TXT)",
                "type": 0,
                "url": f"{base_url}/cn_hk_tw.txt",
                "playerType": 1,
            },
            {
                "name": "Nickelodeon 合集 (M3U)",
                "type": 1,
                "url": f"{base_url}/nickelodeon.m3u",
                "playerType": 1,
            },
        ]
    }
    json_path.write_text(json.dumps(tvbox_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total_streams = sum(len(grouped_output[g]) for g in GROUP_ORDER)
    print(f"Generated {total_streams} streams:")
    for grp in GROUP_ORDER:
        ch_cnt = len({item["name"] for item in grouped_output[grp]})
        print(f"  {grp}: {ch_cnt} channels, {len(grouped_output[grp])} streams")


if __name__ == "__main__":
    main()
