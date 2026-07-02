#!/usr/bin/env python3
"""Google Trends gaming/meme trend monitor with Feishu notifications."""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from pytrends.request import TrendReq

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
FEISHU_WEBHOOK = os.environ.get("FEISHU_WEBHOOK", "")
GEO = os.environ.get("GEO", "US")
HL = os.environ.get("HL", "en-US")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
TRENDS_RSS_URL = f"https://trends.google.com/trending/rss?geo={GEO}"
SEEN_TERMS_FILE = "seen_terms.json"

TARGET_CATEGORIES = {"game", "game_meme", "meme"}

CATEGORY_EMOJI = {"game": "🎮", "game_meme": "🔥", "meme": "🤣"}
CATEGORY_LABEL = {"game": "Game", "game_meme": "Game+Meme", "meme": "Meme"}


def fetch_rss_trends() -> list[dict]:
    resp = requests.get(TRENDS_RSS_URL, timeout=30)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    ns = {"ht": "https://trends.google.com/trending/rss"}

    # Extract the RSS publication date to identify batch freshness
    channel_pub = root.findtext(".//pubDate", "")
    batch_date = _parse_rss_date(channel_pub)

    trends = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        title = title_el.text.strip() if title_el is not None and title_el.text else ""

        traffic_el = item.find("ht:approx_traffic", ns)
        traffic = (
            traffic_el.text.strip()
            if traffic_el is not None and traffic_el.text
            else "unknown"
        )

        pub_el = item.find("pubDate")
        pub_raw = pub_el.text.strip() if pub_el is not None and pub_el.text else ""

        news_items = item.findall("ht:news_item", ns)
        news_titles = []
        news_sources = []
        for ni in news_items[:5]:
            nit = ni.find("ht:news_item_title", ns)
            if nit is not None and nit.text:
                news_titles.append(nit.text.strip())
            nis = ni.find("ht:news_item_source", ns)
            if nis is not None and nis.text:
                news_sources.append(nis.text.strip())

        if not title:
            continue

        trends.append(
            {
                "title": title,
                "traffic": traffic,
                "pub_date": pub_raw,
                "batch_date": batch_date.isoformat() if batch_date else "",
                "news_titles": news_titles,
                "news_sources": news_sources,
                "source": "rss",
            }
        )

    return trends


def _parse_rss_date(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%a, %d %b %Y %H:%M:%S %z")
    except ValueError:
        return None


def fetch_pytrends_realtime() -> list[dict]:
    try:
        pytrends = TrendReq(hl=HL, tz=0, timeout=(10, 25), retries=1)
        df = pytrends.realtime_trending_searches(pn=GEO)
    except Exception as e:
        print(f"  pytrends request failed: {e}")
        return []

    if df is None or df.empty:
        return []

    trends = []
    now = datetime.now(timezone.utc).isoformat()
    for _, row in df.iterrows():
        title = str(row.get("title", "")).strip()
        if not title:
            continue
        # Some versions return entityNames or related fields
        related = ""
        entity_col = row.get("entityNames", "")
        if entity_col and hasattr(entity_col, "__iter__") and len(entity_col) > 0:
            related = str(entity_col[0])
        trends.append(
            {
                "title": title,
                "traffic": "trending",
                "pub_date": now,
                "batch_date": now,
                "news_titles": [related] if related else [],
                "news_sources": [],
                "source": "pytrends",
            }
        )

    return trends


def merge_trends(rss: list[dict], pt: list[dict]) -> list[dict]:
    """Merge RSS and pytrends results, deduplicating by normalized title."""
    seen = {}
    for t in rss + pt:
        key = t["title"].lower().strip()
        if key in seen:
            existing = seen[key]
            # Merge news sources
            existing["news_titles"] = list(
                set(existing.get("news_titles", []) + t.get("news_titles", []))
            )
            existing["news_sources"] = list(
                set(existing.get("news_sources", []) + t.get("news_sources", []))
            )
            existing["source"] = f"{existing['source']}+{t['source']}"
            # RSS traffic beats pytrends' "trending" placeholder
            if existing.get("traffic") == "trending":
                existing["traffic"] = t.get("traffic", "trending")
        else:
            seen[key] = dict(t)
    return list(seen.values())


def classify_with_deepseek(trends: list[dict]) -> list[dict]:
    if not DEEPSEEK_API_KEY:
        print("  DEEPSEEK_API_KEY not set, skipping classification")
        for t in trends:
            t["category"] = "unknown"
            t["reason"] = ""
        return trends

    if not trends:
        return trends

    # Build the prompt
    term_lines = []
    for i, t in enumerate(trends):
        context_parts = []
        if t.get("news_titles"):
            context_parts.extend(t["news_titles"][:3])
        if t.get("news_sources"):
            context_parts.extend(t["news_sources"][:2])
        ctx = " | ".join(context_parts) if context_parts else "no context"
        term_lines.append(f'{i + 1}. "{t["title"]}" | ctx: {ctx}')

    prompt = """Classify each trending search term. Return a JSON object with a "results" array.

Categories:
- "game_meme": a viral internet meme that ALSO has a playable game version (examples: sprunki, crazy cattle 3d, italian brainrot, skibidi toilet game). These are INTERACTIVE games born from memes.
- "game": a known video game title, game franchise, mobile game, or game platform (examples: Fortnite, Minecraft, Elden Ring, Roblox, Steam). NOT just a gaming news topic.
- "meme": internet meme or viral trend that does NOT have a playable game (examples: a catchphrase, a reaction image, a social media challenge without a game).
- "other": not game or meme related. Normal news, celebrity, sports, politics, etc.

Key judgment: "game_meme" means someone could BUILD a simple web game for this keyword and get SEO traffic. It's a meme that has or could have a game version.

Return ONLY valid JSON:
{"results": [{"index": 1, "category": "game_meme", "reason": "brief reason in Chinese"}, ...]}

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
                "content": "You are a content classifier. Always output valid JSON only, no markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 4096,
    }

    try:
        resp = requests.post(
            DEEPSEEK_API_URL, headers=headers, json=payload, timeout=90
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"  DeepSeek API error: {e}")
        for t in trends:
            t["category"] = "unknown"
            t["reason"] = ""
        return trends

    data = resp.json()
    content = data["choices"][0]["message"]["content"].strip()

    # Parse response
    try:
        # Strip markdown code fences if present
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
            t["reason"] = ""
        return trends

    # Normalize the parsed structure
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
            t["reason"] = class_map[i].get("reason", "")
        else:
            t["category"] = "other"
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

    # Prune old entries (>7 days)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    existing = {k: v for k, v in existing.items() if v > cutoff}

    with open(SEEN_TERMS_FILE, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)


def send_to_feishu(trends: list[dict], dry_run: bool = False) -> bool:
    if not FEISHU_WEBHOOK:
        print("  FEISHU_WEBHOOK not set, skipping notification")
        return True

    matched = [t for t in trends if t.get("category") in TARGET_CATEGORIES]

    if not matched:
        print("  No game/meme trends to send")
        return True

    if dry_run:
        print(f"  [DRY RUN] Would send {len(matched)} items:")
        for t in matched:
            print(f"    [{t.get('category')}] {t['title']} ({t.get('traffic')})")
        return True

    # Sort: game_meme first, then game, then meme
    priority = {"game_meme": 0, "game": 1, "meme": 2}
    ordered = sorted(
        matched,
        key=lambda x: (
            priority.get(x.get("category", "other"), 99),
            -_traffic_to_num(x.get("traffic", "0")),
        ),
    )

    elements: list[dict] = []
    for t in ordered:
        emoji = CATEGORY_EMOJI.get(t.get("category", ""), "")
        label = CATEGORY_LABEL.get(t.get("category", ""), "")

        # Build a compact info line
        reason_line = ""
        if t.get("reason"):
            reason_line = f"\n{t['reason']}"

        sources_line = ""
        if t.get("news_sources"):
            unique_sources = list(dict.fromkeys(t["news_sources"]))[:3]
            sources_line = " | " + " · ".join(unique_sources)

        elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"{emoji} **{t['title']}** `{t.get('traffic', '?')}` {label}{reason_line}{sources_line}",
                },
            }
        )

    now_str = datetime.now().strftime("%m-%d %H:%M")

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🔥 游戏/Meme 热搜 | {now_str} ({len(matched)}条)",
                },
                "template": "turquoise",
            },
            "elements": elements,
            "note": {
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"Geo: {GEO} | Google Trends · RSS + pytrends",
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
            print(f"  Sent {len(matched)} trends to Feishu OK")
            return True
        print(f"  Feishu error: {resp.status_code} {body}")
        return False
    except requests.RequestException as e:
        print(f"  Feishu request failed: {e}")
        return False


def _traffic_to_num(traffic: str) -> int:
    """Convert traffic string like '500+' or '2,000+' to a comparable number."""
    s = traffic.replace("+", "").replace(",", "").strip()
    try:
        return int(s)
    except ValueError:
        return 0


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"=== Trends Monitor [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ===")
    print(f"  Region: {GEO}")
    print(f"  Dry run: {dry_run}")

    # 1. Fetch trends
    rss = fetch_rss_trends()
    print(f"  RSS: {len(rss)} trends")

    pt = fetch_pytrends_realtime()
    print(f"  pytrends: {len(pt)} trends")

    # 2. Merge & dedup
    trends = merge_trends(rss, pt)
    print(f"  Merged unique: {len(trends)}")

    if not trends:
        print("  No trends found, aborting.")
        return

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

    # Print classification summary
    cat_counts = {}
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
    print(f"\n  === Result: {len(matched)} game/meme trends ===")
    for t in matched:
        print(f"    [{t.get('category')}] {t['title']} ({t.get('traffic')})")
    print()


if __name__ == "__main__":
    main()
