#!/usr/bin/env python3
"""Google Trends gaming/meme trend monitor — multi-region with Feishu notifications."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import requests

# Monkey-patch for pytrends compatibility with urllib3 >= 2.0
import urllib3.util.retry as _retry_mod

_OriginalRetry = _retry_mod.Retry


class _PatchedRetry(_OriginalRetry):
    def __init__(self, *args, method_whitelist=None, **kwargs):
        if method_whitelist is not None:
            kwargs.setdefault("allowed_methods", method_whitelist)
        super().__init__(*args, **kwargs)


_retry_mod.Retry = _PatchedRetry

from pytrends.request import TrendReq

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
SEEN_TERMS_FILE = "seen_terms.json"

REGIONS = ["US", "MX", "BR", "JP", "KR", "GB"]
REGION_EMOJI = {
    "US": "🇺🇸",
    "MX": "🇲🇽",
    "BR": "🇧🇷",
    "JP": "🇯🇵",
    "KR": "🇰🇷",
    "GB": "🇬🇧",
}
REGION_NAME = {
    "US": "美国",
    "MX": "墨西哥",
    "BR": "巴西",
    "JP": "日本",
    "KR": "韩国",
    "GB": "英国",
}

TARGET_CATEGORIES = {"game", "game_meme", "meme"}
CATEGORY_PRIORITY = {"game_meme": 0, "game": 1, "meme": 2}
CATEGORY_EMOJI = {"game_meme": "🔥", "game": "🎮", "meme": "🤣"}

TOPIC_EMOJI = {
    "财经": "📈",
    "体育": "⚽",
    "影视": "🎬",
    "科技": "💻",
    "娱乐": "🎭",
    "政治": "🏛️",
    "社会": "📰",
    "人物": "👤",
    "游戏": "🎮",
    "游戏+梗": "🔥",
    "流行梗": "🤣",
    "健康": "🏥",
    "教育": "📚",
    "天气": "🌤️",
    "美食": "🍽️",
}


def fetch_rss_trends(geo: str) -> tuple[str, list[dict]]:
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    ns = {"ht": "https://trends.google.com/trending/rss"}

    channel_pub = root.findtext(".//pubDate", "")
    batch_date = _parse_rss_date(channel_pub)

    trends = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        if not title:
            continue

        traffic_el = item.find("ht:approx_traffic", ns)
        traffic = (
            traffic_el.text.strip()
            if traffic_el is not None and traffic_el.text
            else "?"
        )

        news_items = item.findall("ht:news_item", ns)
        news_titles, news_sources = [], []
        for ni in news_items[:3]:
            nit = ni.find("ht:news_item_title", ns)
            if nit is not None and nit.text:
                news_titles.append(nit.text.strip())
            nis = ni.find("ht:news_item_source", ns)
            if nis is not None and nis.text:
                news_sources.append(nis.text.strip())

        trends.append(
            {
                "title": title,
                "traffic": traffic,
                "regions": [geo],
                "batch_date": batch_date.isoformat() if batch_date else "",
                "news_titles": news_titles,
                "news_sources": news_sources,
            }
        )

    return geo, trends


def _parse_rss_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


def fetch_all_regions() -> list[dict]:
    all_trends: list[dict] = []
    for geo, trends in map(fetch_rss_trends, REGIONS):
        print(f"    {geo}: {len(trends)} trends")
        all_trends.extend(trends)
    return all_trends


def merge_cross_region(trends: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for t in trends:
        key = t["title"].lower().strip()
        if key in seen:
            ex = seen[key]
            ex["regions"] = list(set(ex.get("regions", []) + t.get("regions", [])))
            ex["news_titles"] = list(
                set(ex.get("news_titles", []) + t.get("news_titles", []))
            )
            ex["news_sources"] = list(
                set(ex.get("news_sources", []) + t.get("news_sources", []))
            )
            if _traffic_to_num(ex.get("traffic", "0")) < _traffic_to_num(
                t.get("traffic", "0")
            ):
                ex["traffic"] = t["traffic"]
        else:
            seen[key] = dict(t)
    return list(seen.values())


def classify_with_deepseek(trends: list[dict]) -> list[dict]:
    if not DEEPSEEK_API_KEY:
        for t in trends:
            t["category"] = "unknown"
            t["topic"] = "未知"
            t["reason"] = ""
        return trends

    if not trends:
        return trends

    term_lines = []
    for i, t in enumerate(trends):
        ctx = []
        if t.get("news_titles"):
            ctx.extend(t["news_titles"][:2])
        if t.get("news_sources"):
            ctx.extend(t["news_sources"][:1])
        ctx_str = " | ".join(ctx) if ctx else "no context"
        regions_str = ",".join(t.get("regions", []))
        term_lines.append(
            f'{i + 1}. "{t["title"]}" [regions: {regions_str}] ctx: {ctx_str}'
        )

    prompt = """Classify each trending search term. Return a JSON object with a "results" array.
Each result: {"index": N, "category": "...", "topic": "...", "reason": "..."}

Categories:
- "game_meme": viral internet meme that also has a playable game (e.g. sprunki, crazy cattle 3d, italian brainrot)
- "game": a video game title, franchise, or gaming platform (e.g. Fortnite, Minecraft, Elden Ring, Roblox)
- "meme": internet meme/viral trend without a playable game
- "other": anything else (news, celebrity, sports, politics, finance, etc.)

"topic" field — short Chinese category label:
- For "game_meme": "游戏+梗"
- For "game": "游戏"
- For "meme": "流行梗"
- For "other": choose from 财经/体育/影视/科技/娱乐/政治/社会/人物/健康/教育/天气/美食 or a fitting single-word Chinese label

"reason" field — 1-sentence explanation in Chinese.

Return ONLY valid JSON:
{"results": [{"index": 1, "category": "other", "topic": "财经", "reason": "特斯拉股价相关"}, ...]}

Terms:
""" + "\n".join(term_lines)

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {
                "role": "system",
                "content": "You are a content classifier. Output valid JSON only, no markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 8192,
    }

    try:
        resp = requests.post(
            DEEPSEEK_API_URL, headers=headers, json=payload, timeout=120
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  DeepSeek API error: {e}")
        for t in trends:
            t["category"] = "unknown"
            t["topic"] = "未知"
            t["reason"] = ""
        return trends

    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()

    try:
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(
                lines[1:-1] if lines[-1].startswith("```") else lines[1:]
            )
        parsed = json.loads(content)
    except json.JSONDecodeError:
        print(f"  Failed to parse DeepSeek JSON. Raw: {content[:500]}")
        for t in trends:
            t["category"] = "unknown"
            t["topic"] = "未知"
            t["reason"] = ""
        return trends

    results_list = parsed if isinstance(parsed, list) else parsed.get("results", [])
    if not results_list and isinstance(parsed, list):
        results_list = parsed

    class_map: dict[int, dict] = {}
    for c in results_list:
        idx = int(c.get("index", -1)) - 1
        if 0 <= idx < len(trends):
            class_map[idx] = c

    for i, t in enumerate(trends):
        if i in class_map:
            t["category"] = class_map[i].get("category", "other")
            t["topic"] = class_map[i].get("topic", "其他")
            t["reason"] = class_map[i].get("reason", "")
        else:
            t["category"] = "other"
            t["topic"] = "其他"
            t["reason"] = ""

    tokens = data.get("usage", {}).get("total_tokens", "?")
    print(f"  DeepSeek classified {len(trends)} terms (tokens: {tokens})")
    return trends


def load_seen_terms() -> set[str]:
    try:
        with open(SEEN_TERMS_FILE, "r") as f:
            data = json.load(f)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        return {t for t, d in data.items() if d > cutoff}
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen_terms(trends: list[dict]):
    existing = {}
    try:
        with open(SEEN_TERMS_FILE, "r") as f:
            existing = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    now = datetime.now(timezone.utc).isoformat()
    for t in trends:
        key = t["title"].lower().strip()
        existing[key] = now

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    existing = {k: v for k, v in existing.items() if v > cutoff}

    with open(SEEN_TERMS_FILE, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def send_to_feishu(trends: list[dict], dry_run: bool = False) -> bool:
    if not FEISHU_WEBHOOK:
        print("  FEISHU_WEBHOOK not set, skipping notification")
        return True

    # Split into high-priority (game/meme) and the rest
    highlights = [t for t in trends if t.get("category") in TARGET_CATEGORIES]
    others = [t for t in trends if t.get("category") not in TARGET_CATEGORIES]

    if dry_run:
        print(
            f"  [DRY RUN] Would send {len(highlights)} highlights + {len(others)} others"
        )
        for t in highlights:
            print(
                f"    [{t.get('category')}] {t['title']} ({t.get('traffic')}) [{t.get('topic')}]"
            )
        for t in others[:5]:
            print(f"    [{t.get('topic')}] {t['title']} ({t.get('traffic')})")
        if len(others) > 5:
            print(f"    ... and {len(others) - 5} more")
        return True

    elements: list[dict] = []

    # --- Highlights section ---
    if highlights:
        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**🎮 游戏 / Meme 热点 ({len(highlights)}条)**",
                },
            }
        )

        ordered = sorted(
            highlights,
            key=lambda x: (
                CATEGORY_PRIORITY.get(x.get("category", "other"), 99),
                -_traffic_to_num(x.get("traffic", "0")),
            ),
        )
        for t in ordered:
            emoji = CATEGORY_EMOJI.get(t.get("category", ""), "")
            topic = t.get("topic", "")
            region_flags = "".join(
                str(REGION_EMOJI.get(r, r)) for r in t.get("regions", [])
            )
            reason_line = f"\n  {t['reason']}" if t.get("reason") else ""
            elements.append(
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"{emoji} **{t['title']}**  `{t.get('traffic', '?')}`   {topic}  {region_flags}{reason_line}",
                    },
                }
            )

        elements.append({"tag": "hr"})

    # --- All trends section ---
    total = len(trends)
    region_counts = {r: 0 for r in REGIONS}
    for t in trends:
        for r in t.get("regions", []):
            if r in region_counts:
                region_counts[r] += 1
    region_summary = " · ".join(
        f"{REGION_EMOJI.get(r, r)} {region_counts[r]}" for r in REGIONS
    )

    elements.append(
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📋 全部热搜 ({total}条)**  {region_summary}",
            },
        }
    )

    # Sort: highlights first, then by traffic > regions count
    all_ordered = sorted(
        trends,
        key=lambda x: (
            CATEGORY_PRIORITY.get(x.get("category", "other"), 99),
            -_traffic_to_num(x.get("traffic", "0")),
            -len(x.get("regions", [])),
        ),
    )

    for t in all_ordered:
        cat = t.get("category", "other")
        if cat in CATEGORY_EMOJI:
            emoji = CATEGORY_EMOJI[cat]
        else:
            topic = t.get("topic", "")
            emoji = TOPIC_EMOJI.get(topic, "📌")

        topic = t.get("topic", "")
        region_flags = "".join(
            str(REGION_EMOJI.get(r, r)) for r in t.get("regions", [])
        )

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{emoji} **{t['title']}**  `{t.get('traffic', '?')}`  {topic}  {region_flags}",
                },
            }
        )

    # --- Footer ---
    now_str = datetime.now().strftime("%m-%d %H:%M")
    hit_count = len(highlights)
    title = f"🌍 全球热搜 | {now_str}"
    if hit_count > 0:
        title += f" (🎮 {hit_count}条)"

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "elements": elements,
            "note": {
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"Google Trends RSS · DeepSeek 分类 · {len(REGIONS)}个地区",
                    }
                ],
            },
        },
    }

    try:
        resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=30)
        body = resp.json()
        if resp.status_code == 200 and (
            body.get("code") == 0 or body.get("StatusCode") == 0
        ):
            print(f"  Sent to Feishu OK ({len(highlights)} highlights, {total} total)")
            return True
        print(f"  Feishu error: {resp.status_code} {body}")
        return False
    except requests.RequestException as e:
        print(f"  Feishu request failed: {e}")
        return False


def _traffic_to_num(traffic: str) -> int:
    s = traffic.replace("+", "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return 0


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"=== Trends Monitor [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ===")
    print(f"  Regions: {', '.join(REGIONS)}")
    print(f"  Dry run: {dry_run}")

    # 1. Fetch from all regions
    print("  Fetching RSS...")
    all_raw = fetch_all_regions()
    total_raw = len(all_raw)
    print(f"  Total raw: {total_raw}")

    if not all_raw:
        print("  No trends found, aborting.")
        return

    # 2. Cross-region dedup
    trends = merge_cross_region(all_raw)
    print(f"  After cross-region dedup: {len(trends)} unique")

    # 3. Filter seen
    seen = load_seen_terms()
    new_trends = [t for t in trends if t["title"].lower().strip() not in seen]
    print(f"  New (unseen): {len(new_trends)} / {len(trends)}")

    if not new_trends:
        print("  All trends already seen, done.")
        return

    # 4. Classify
    print("  Classifying with DeepSeek...")
    classified = classify_with_deepseek(new_trends)

    cat_counts: dict[str, int] = {}
    for t in classified:
        c = t.get("category", "other")
        cat_counts[c] = cat_counts.get(c, 0) + 1
    print(f"  Classification: {cat_counts}")

    # 5. Send to Feishu
    success = send_to_feishu(classified, dry_run=dry_run)

    # 6. Save seen terms
    if success or dry_run:
        save_seen_terms(classified)
        if not dry_run:
            print("  Saved seen terms.")

    # Final summary
    matched = [t for t in classified if t.get("category") in TARGET_CATEGORIES]
    print(f"\n  === Result: {len(matched)} game/meme out of {len(classified)} ===")
    for t in matched:
        print(
            f"    [{t.get('category')}] {t['title']} ({t.get('traffic')}) [{t.get('topic')}]"
        )
    print()


if __name__ == "__main__":
    main()
