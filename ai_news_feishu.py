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
    "鏈哄櫒浜?",
    "浜哄伐鏅鸿兘",
    "澶фā鍨?",
    "鏅鸿兘浣?",
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
    image_url: str = ""


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


def tag_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def absolute_url(url: str, base_url: str) -> str:
    if not url:
        return ""
    return urllib.parse.urljoin(base_url, html.unescape(url.strip()))


def first_image_url(element: ET.Element, base_url: str) -> str:
    for child in element.iter():
        tag = tag_name(child)
        attrs = {key.rsplit("}", 1)[-1].lower(): value for key, value in child.attrib.items()}
        if tag in {"content", "thumbnail"} and attrs.get("url"):
            return absolute_url(attrs["url"], base_url)
        if tag == "enclosure" and attrs.get("url", "").lower().startswith(("http://", "https://")):
            if attrs.get("type", "").lower().startswith("image/"):
                return absolute_url(attrs["url"], base_url)
    text = ET.tostring(element, encoding="unicode", method="xml")
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', text, flags=re.IGNORECASE)
    if match:
        return absolute_url(match.group(1), base_url)
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
        image_url = first_image_url(node, link)
        if title and link:
            entries.append(
                NewsItem(
                    title=title,
                    link=link,
                    source=source.name,
                    published=parse_date(published_raw),
                    summary=summary,
                    image_url=image_url,
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


def extract_meta_image(html_text: str, base_url: str) -> str:
    patterns = (
        r'<meta[^>]+(?:property|name)=["\']og:image(?::secure_url)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image(?::secure_url)?["\']',
        r'<meta[^>]+(?:property|name)=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']twitter:image(?::src)?["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return absolute_url(match.group(1), base_url)
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, flags=re.IGNORECASE)
    if match:
        return absolute_url(match.group(1), base_url)
    return ""


def enrich_news_images(items: list[NewsItem]) -> list[NewsItem]:
    enriched = []
    for item in items:
        image_url = item.image_url
        if not image_url:
            try:
                image_url = extract_meta_image(fetch_text(item.link, timeout=12), item.link)
            except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
                print(f"Image lookup skipped for {item.link}: {exc}", file=sys.stderr)
        enriched.append(
            NewsItem(
                title=item.title,
                link=item.link,
                source=item.source,
                published=item.published,
                summary=item.summary,
                image_url=image_url,
            )
        )
    return enriched


def is_probably_chinese(text: str) -> bool:
    if not text:
        return False
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return chinese_chars >= 4


def extract_response_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [extract_response_text(item) for item in value]
        return "".join(part for part in parts if part)
    if isinstance(value, dict):
        if isinstance(value.get("output_text"), str):
            return value["output_text"]
        if value.get("type") == "output_text" and isinstance(value.get("text"), str):
            return value["text"]
        for key in ("text", "content", "output"):
            extracted = extract_response_text(value.get(key))
            if extracted:
                return extracted
    return ""


def translate_summaries_with_openai(items: list[NewsItem]) -> list[NewsItem]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return items

    targets = [
        (index, item)
        for index, item in enumerate(items)
        if item.summary and not is_probably_chinese(item.summary)
    ]
    if not targets:
        return items

    payload_items = [
        {
            "index": index,
            "title": item.title,
            "summary": item.summary,
        }
        for index, item in targets
    ]
    prompt = (
        "鎶婁笅闈? AI 璧勮鎽樿缈昏瘧鎴愮畝浣撲腑鏂囥?傝姹傦細蹇犲疄銆佺畝娲併?侀?傚悎椋炰功鏃ユ姤锛?"
        "淇濈暀浜у搧鍚嶃?佸叕鍙稿悕鍜屾ā鍨嬪悕锛涗笉瑕佹坊鍔犲師鏂囨病鏈夌殑淇℃伅銆?"
        "鍙繑鍥? JSON 鏁扮粍锛屾瘡椤规牸寮忎负 {\"index\": 鏁板瓧, \"summary_zh\": \"涓枃鎽樿\"}銆俓n\n"
        + json.dumps(payload_items, ensure_ascii=False)
    )
    model = os.getenv("OPENAI_TRANSLATION_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    body = {
        "model": model,
        "input": prompt,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            result = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"OpenAI summary translation skipped: {exc}", file=sys.stderr)
        return items

    raw_text = extract_response_text(result).strip()
    raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.IGNORECASE | re.DOTALL)
    try:
        translations = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"OpenAI summary translation returned non-JSON text: {exc}", file=sys.stderr)
        return items

    by_index = {}
    if isinstance(translations, list):
        for entry in translations:
            if isinstance(entry, dict) and isinstance(entry.get("summary_zh"), str):
                by_index[int(entry.get("index", -1))] = clean_text(entry["summary_zh"], limit=220)

    translated = []
    for index, item in enumerate(items):
        summary = by_index.get(index, item.summary)
        translated.append(
            NewsItem(
                title=item.title,
                link=item.link,
                source=item.source,
                published=item.published,
                summary=summary,
                image_url=item.image_url,
            )
        )
    return translated


def local_date_label() -> str:
    local = datetime.now().astimezone()
    return local.strftime("%Y-%m-%d")


def build_digest_text(items: list[NewsItem], errors: list[str]) -> str:
    lines = [f"姣忔棩 AI 璧勮锝渰local_date_label()}", ""]
    if not items:
        lines.append("浠婂ぉ娌℃湁鎶撳彇鍒版柊鐨? AI 璧勮銆?")
    for index, item in enumerate(items, start=1):
        date = item.published.astimezone().strftime("%m-%d %H:%M") if item.published else "鏃堕棿鏈煡"
        lines.append(f"{index}. {item.title}")
        lines.append(f"   鏉ユ簮锛歿item.source}锝渰date}")
        if item.summary:
            lines.append(f"   鎽樿锛歿item.summary}")
        if item.image_url:
            lines.append(f"   閰嶅浘锛歿item.image_url}")
        lines.append(f"   閾炬帴锛歿item.link}")
        lines.append("")
    if errors:
        lines.append("閮ㄥ垎鏉ユ簮鎶撳彇澶辫触锛?")
        lines.extend(f"- {error}" for error in errors[:5])
    return "\n".join(lines).strip()


def build_feishu_post(items: list[NewsItem], errors: list[str], image_keys: dict[str, str] | None = None) -> dict:
    image_keys = image_keys or {}
    content = []
    if not items:
        content.append([{"tag": "text", "text": "浠婂ぉ娌℃湁鎶撳彇鍒版柊鐨? AI 璧勮銆?"}])
    for index, item in enumerate(items, start=1):
        date = item.published.astimezone().strftime("%m-%d %H:%M") if item.published else "鏃堕棿鏈煡"
        line = [
            {"tag": "text", "text": f"{index}. "},
            {"tag": "a", "text": item.title, "href": item.link},
            {"tag": "text", "text": f"\n鏉ユ簮锛歿item.source}锝渰date}"},
        ]
        content.append(line)
        if item.summary:
            content.append([{"tag": "text", "text": f"鎽樿锛歿item.summary}"}])
        image_key = image_keys.get(item.link)
        if image_key:
            content.append([{"tag": "img", "image_key": image_key}])
        elif item.image_url:
            content.append(
                [
                    {"tag": "text", "text": "閰嶅浘锛?"},
                    {"tag": "a", "text": "鎵撳紑鍥剧墖", "href": item.image_url},
                ]
            )
    if errors:
        content.append([{"tag": "text", "text": "閮ㄥ垎鏉ユ簮鎶撳彇澶辫触锛?" + "锛?".join(errors[:5])}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"姣忔棩 AI 璧勮锝渰local_date_label()}",
                    "content": content,
                }
            }
        },
    }


def fetch_binary(url: str, timeout: int = 20, max_bytes: int = 10 * 1024 * 1024) -> tuple[bytes, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "daily-ai-news-feishu/1.0 (+https://open.feishu.cn)",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
        data = response.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError(f"image is larger than {max_bytes} bytes")
        return data, content_type


def get_feishu_tenant_access_token(app_id: str, app_secret: str) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8", errors="replace"))
    token = result.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"failed to get Feishu tenant token: {result}")
    return token


def multipart_form_data(fields: dict[str, str], files: dict[str, tuple[str, str, bytes]]) -> tuple[bytes, str]:
    boundary = f"----daily-ai-news-{int(time.time() * 1000)}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(value.encode("utf-8"))
        chunks.append(b"\r\n")
    for name, (filename, content_type, data) in files.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_feishu_image(token: str, image_url: str) -> str:
    image_data, content_type = fetch_binary(image_url)
    extension = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
    }.get(content_type, "jpg")
    body, content_type_header = multipart_form_data(
        {"image_type": "message"},
        {"image": (f"news-image.{extension}", content_type, image_data)},
    )
    request = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/images",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type_header,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8", errors="replace"))
    image_key = result.get("data", {}).get("image_key")
    if not image_key:
        raise RuntimeError(f"failed to upload Feishu image: {result}")
    return image_key


def upload_news_images_to_feishu(items: list[NewsItem]) -> dict[str, str]:
    app_id = os.getenv("FEISHU_APP_ID")
    app_secret = os.getenv("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        return {}

    try:
        token = get_feishu_tenant_access_token(app_id, app_secret)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Feishu image upload disabled: {exc}", file=sys.stderr)
        return {}

    image_keys = {}
    for item in items:
        if not item.image_url:
            continue
        try:
            image_keys[item.link] = upload_feishu_image(token, item.image_url)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
            print(f"Feishu image upload skipped for {item.image_url}: {exc}", file=sys.stderr)
    return image_keys


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
    items = enrich_news_images(items)
    items = translate_summaries_with_openai(items)

    if args.dry_run:
        print(build_digest_text(items, errors))
        return 0

    webhook_url = os.getenv("FEISHU_WEBHOOK_URL")
    if not webhook_url:
        print("FEISHU_WEBHOOK_URL is required. Run with --dry-run to preview without sending.", file=sys.stderr)
        return 2

    image_keys = upload_news_images_to_feishu(items)
    payload = build_feishu_post(items, errors, image_keys)
    payload = sign_payload(payload, os.getenv("FEISHU_WEBHOOK_SECRET"))
    post_to_feishu(webhook_url, payload)

    for item in items:
        sent_links.add(canonical_link(item.link))
    save_sent_links(state_file, sent_links)
    print(f"Sent {len(items)} AI news item(s) to Feishu.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
