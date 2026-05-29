#!/usr/bin/env python3
"""Fetch daily AI news from RSS/Atom feeds and push a digest to Feishu."""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import hmac
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES_FILE = ROOT / "sources.json"
DEFAULT_STATE_FILE = ROOT / ".state" / "sent_links.json"

AI_KEYWORDS = (
    "openai",
    "gpt",
    "chatgpt",
    "agent",
    "ai agent",
    "llm",
    "large language model",
    "multimodal",
    "reasoning",
    "inference",
    "benchmark",
    "model",
    "transformer",
    "anthropic",
    "claude",
    "gemini",
    "deepmind",
    "nvidia",
    "microsoft copilot",
    "hugging face",
    "sora",
    "机器人",
    "人工智能",
    "大模型",
    "智能体",
)


@dataclass(frozen=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_sources(path: Path) -> list[Source]:
    if not path.exists():
        raise FileNotFoundError(f"sources file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources = []
    for item in raw:
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if name and url:
            sources.append(Source(name=name, url=url))
    if not sources:
        raise ValueError("sources file contains no valid source entries")
    return sources


def fetch_text(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-ai-news-feishu/1.0 (+https://open.feishu.cn)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def clean_text(value: str | None, limit: int = 180) -> str:
    if not value:
        return ""
    text = html.unescape(re.sub(r"<[^>]+>", " ", value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def first_text(element: ET.Element, names: Iterable[str]) -> str:
    for name in names:
        found = element.find(name)
        if found is not None and found.text:
            return found.text.strip()
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in names and child.text:
            return child.text.strip()
    return ""


def first_link(element: ET.Element) -> str:
    link_text = first_text(element, ("link",))
    if link_text:
        return link_text
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag == "link":
            href = child.attrib.get("href")
            rel = child.attrib.get("rel", "alternate")
            if href and rel in ("alternate", ""):
                return href.strip()
    return ""


def parse_feed(xml_text: str, source: Source) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    root_tag = root.tag.rsplit("}", 1)[-1].lower()
    entries = []

    if root_tag == "rss":
        channel = root.find("channel")
        nodes = channel.findall("item") if channel is not None else []
    else:
        nodes = [
            node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1].lower() in ("entry", "item")
        ]

    for node in nodes:
        title = clean_text(first_text(node, ("title",)), limit=140)
        link = first_link(node)
        published_raw = first_text(node, ("published", "updated", "pubDate", "date"))
        summary = clean_text(
            first_text(node, ("summary", "description", "content", "encoded")),
            limit=220,
        )
        if title and link:
            entries.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source.name,
                    published=parse_date(published_raw),
                    summary=summary,
                )
            )
    return entries


def canonical_link(link: str) -> str:
    parsed = urllib.parse.urlsplit(link)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [
        (key, value)
        for key, value in query
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
    ]
    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            urllib.parse.urlencode(query),
            "",
        )
    )


def load_sent_links(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    if isinstance(raw, list):
        return {str(item) for item in raw}
    return set()


def save_sent_links(path: Path, links: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = sorted(links)[-1000:]
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")


def score_item(item: NewsItem) -> tuple[int, float]:
    text = f"{item.title} {item.summary}".lower()
    keyword_score = sum(3 if " " in keyword else 1 for keyword in AI_KEYWORDS if keyword in text)
    timestamp = item.published.timestamp() if item.published else 0
    return (keyword_score, timestamp)


def collect_news(sources: list[Source], lookback_hours: int, max_items: int, sent_links: set[str]) -> tuple[list[NewsItem], list[str]]:
    cutoff = utc_now() - timedelta(hours=lookback_hours)
    by_link: dict[str, NewsItem] = {}
    errors = []

    for source in sources:
        try:
            feed = fetch_text(source.url)
            for item in parse_feed(feed, source):
                key = canonical_link(item.link)
                if key in sent_links:
                    continue
                if item.published and item.published < cutoff:
                    continue
                existing = by_link.get(key)
                if existing is None or score_item(item) > score_item(existing):
                    by_link[key] = item
        except (ET.ParseError, urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            errors.append(f"{source.name}: {exc}")

    ranked = sorted(by_link.values(), key=score_item, reverse=True)
    return ranked[:max_items], errors


def local_date_label() -> str:
    local = datetime.now().astimezone()
    return local.strftime("%Y-%m-%d")


def build_digest_text(items: list[NewsItem], errors: list[str]) -> str:
    lines = [f"每日 AI 资讯｜{local_date_label()}", ""]
    if not items:
        lines.append("今天没有抓取到新的 AI 资讯。")
    for index, item in enumerate(items, start=1):
        date = item.published.astimezone().strftime("%m-%d %H:%M") if item.published else "时间未知"
        lines.append(f"{index}. {item.title}")
        lines.append(f"   来源：{item.source}｜{date}")
        if item.summary:
            lines.append(f"   摘要：{item.summary}")
        lines.append(f"   链接：{item.link}")
        lines.append("")
    if errors:
        lines.append("部分来源抓取失败：")
        lines.extend(f"- {error}" for error in errors[:5])
    return "\n".join(lines).strip()


def build_feishu_post(items: list[NewsItem], errors: list[str]) -> dict:
    content = []
    if not items:
        content.append([{"tag": "text", "text": "今天没有抓取到新的 AI 资讯。"}])
    for index, item in enumerate(items, start=1):
        date = item.published.astimezone().strftime("%m-%d %H:%M") if item.published else "时间未知"
        line = [
            {"tag": "text", "text": f"{index}. "},
            {"tag": "a", "text": item.title, "href": item.link},
            {"tag": "text", "text": f"\n来源：{item.source}｜{date}"},
        ]
        content.append(line)
        if item.summary:
            content.append([{"tag": "text", "text": f"摘要：{item.summary}"}])
    if errors:
        content.append([{"tag": "text", "text": "部分来源抓取失败：" + "；".join(errors[:5])}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"每日 AI 资讯｜{local_date_label()}",
                    "content": content,
                }
            }
        },
    }


def sign_payload(payload: dict, secret: str | None) -> dict:
    if not secret:
        return payload
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(string_to_sign, b"", digestmod=hashlib.sha256).digest()
    signed = dict(payload)
    signed["timestamp"] = timestamp
    signed["sign"] = base64.b64encode(digest).decode("utf-8")
    return signed


def post_to_feishu(webhook_url: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        text = response.read().decode("utf-8", errors="replace")
        if response.status >= 400:
            raise RuntimeError(f"Feishu webhook failed: HTTP {response.status} {text}")
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            result = {}
        if result.get("code", 0) not in (0, None):
            raise RuntimeError(f"Feishu webhook failed: {text}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Push daily AI news digest to Feishu.")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without sending.")
    parser.add_argument("--sources", default=os.getenv("AI_NEWS_SOURCES_FILE", str(DEFAULT_SOURCES_FILE)))
    parser.add_argument("--state", default=os.getenv("AI_NEWS_STATE_FILE", str(DEFAULT_STATE_FILE)))
    parser.add_argument("--max-items", type=int, default=int(os.getenv("AI_NEWS_MAX_ITEMS", "12")))
    parser.add_argument("--lookback-hours", type=int, default=int(os.getenv("AI_NEWS_LOOKBACK_HOURS", "30")))
    parser.add_argument("--ignore-state", action="store_true", help="Do not filter previously sent links.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sources = load_sources(Path(args.sources))
    state_file = Path(args.state)
    sent_links = set() if args.ignore_state else load_sent_links(state_file)
    items, errors = collect_news(sources, args.lookback_hours, args.max_items, sent_links)

    if args.dry_run:
        print(build_digest_text(items, errors))
        return 0

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook_url:
        print("FEISHU_WEBHOOK_URL is required. Run with --dry-run to preview without sending.", file=sys.stderr)
        return 2

    payload = build_feishu_post(items, errors)
    payload = sign_payload(payload, os.getenv("FEISHU_WEBHOOK_SECRET"))
    post_to_feishu(webhook_url, payload)

    for item in items:
        sent_links.add(canonical_link(item.link))
    save_sent_links(state_file, sent_links)
    print(f"Sent {len(items)} AI news item(s) to Feishu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

