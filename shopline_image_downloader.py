#!/usr/bin/env python3
"""Download product page image HTML and image files from a Shopline site.

Usage:
  python shopline_image_downloader.py --base https://www.celladix.hk --out output
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import sys
from typing import Any

try:
    from playwright.sync_api import sync_playwright  # type: ignore
    HAS_PLAYWRIGHT = True
except Exception:
    HAS_PLAYWRIGHT = False

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

PRODUCT_PATH_PATTERNS = [
    re.compile(r"/products/", re.IGNORECASE),
    re.compile(r"/product/", re.IGNORECASE),
]

IMG_ATTR_CANDIDATES = [
    "src",
    "data-src",
    "data-original",
    "data-lazy",
    "data-zoom-image",
    "data-large_image",
]

JSON_IMAGE_KEYS = [
    "original_image_url",
    "detail_image_url",
    "default_image_url",
    "thumb_image_url",
]

NOISE_HINTS = [
    "logo",
    "icon",
    "favicon",
    "payment",
    "visa",
    "master",
    "paypal",
    "alipay",
    "wechat",
    "apple_pay",
    "google_pay",
    "unionpay",
    "diner",
    "american_express",
    "amex",
    "line",
    "facebook",
    "instagram",
    "tiktok",
    "youtube",
    "twitter",
    "pinterest",
    "badge",
]


class ImageHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        self.images.append(attr_map)


class ProductDescriptionImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_desc = False
        self.desc_depth = 0
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "div":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            class_attr = attr_map.get("class", "")
            if "ProductDetail-description" in class_attr:
                self.in_desc = True
                self.desc_depth = 1
            elif self.in_desc:
                self.desc_depth += 1
        if tag.lower() == "img" and self.in_desc:
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            self.images.append(attr_map)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "div" and self.in_desc:
            self.desc_depth -= 1
            if self.desc_depth <= 0:
                self.in_desc = False
                self.desc_depth = 0


def fetch_bytes(url: str, timeout: int = 20) -> bytes:
    safe_url = encode_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str, timeout: int = 20) -> str:
    data = fetch_bytes(url, timeout=timeout)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def fetch_text_force_utf8(url: str, timeout: int = 20) -> str:
    safe_url = encode_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def strip_ns(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def parse_sitemap(url: str, seen: set[str] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    if url in seen:
        return []
    seen.add(url)

    xml_text = fetch_text(url)
    root = ET.fromstring(xml_text)
    tag = strip_ns(root.tag).lower()

    urls: list[str] = []
    if tag == "sitemapindex":
        for child in root:
            if strip_ns(child.tag).lower() != "sitemap":
                continue
            loc = child.find("{*}loc")
            if loc is not None and loc.text:
                urls.extend(parse_sitemap(loc.text.strip(), seen))
    elif tag == "urlset":
        for child in root:
            if strip_ns(child.tag).lower() != "url":
                continue
            loc = child.find("{*}loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    return urls


def is_product_url(url: str) -> bool:
    path = urllib.parse.urlparse(url).path
    return any(p.search(path) for p in PRODUCT_PATH_PATTERNS)


def normalize_url(url: str, base_url: str) -> str:
    return urllib.parse.urljoin(base_url, url)


def encode_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parsed.path, safe="/:%")
    query = urllib.parse.quote_plus(parsed.query, safe="=&")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, query, parsed.fragment))


def scan_shopline_urls(html: str) -> list[str]:
    prefixes = [
        "https://img.shoplineapp.com/media/image_clips/",
        "https://shoplineimg.com/",
    ]
    results: list[str] = []
    stop_chars = set(['"', "'", " ", "\\", ")", ">"])

    for prefix in prefixes:
        start = 0
        while True:
            idx = html.find(prefix, start)
            if idx == -1:
                break
            end = idx
            while end < len(html) and html[end] not in stop_chars:
                end += 1
            url = html[idx:end]
            url = url.replace("\\u0026", "&").replace("&amp;", "&")
            results.append(url)
            start = end
    return results


def normalize_image_key(url: str) -> str:
    # Remove query and normalize size segment if present
    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    # Shopline size segment like /2000x.jpg or /800x.png
    path = re.sub(r"/\\d+x(?=\\.[a-zA-Z0-9]+$)", "/{size}", path)
    return f"{parsed.netloc}{path}"


def classify_image(url: str) -> str:
    # Returns "detail" or "thumb"
    lower = url.lower()
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path).lower()

    # Heuristics: large/original => detail
    if "original" in filename:
        return "detail"
    if re.search(r"/(1[2-9]\\d{2,3}|2000|2400|3000)x\\.", lower):
        return "detail"
    if re.search(r"/(800|900|1000|1080|1200|1296|1512)x\\.", lower):
        return "detail"
    return "thumb"


def size_score(url: str) -> int:
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path).lower()
    if "original" in filename:
        return 10_000
    m = re.search(r"/(\\d+)x(?=\\.[a-zA-Z0-9]+$)", parsed.path)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return 0
    return 0


def is_detail_candidate(url: str) -> bool:
    # Keep only true detail images for detail HTML
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path).lower()
    if "original" in filename:
        return True
    return size_score(url) >= 1200


def extract_detail_images_with_playwright(url: str) -> list[str]:
    if not HAS_PLAYWRIGHT:
        return []
    results: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        try:
            page.wait_for_selector(".ProductDetail-description", timeout=10000)
        except Exception:
            pass
        img_attrs: list[dict[str, Any]] = page.eval_on_selector_all(
            ".ProductDetail-description img",
            """els => els.map(img => ({
                src: img.getAttribute('src') || '',
                dataSrc: img.getAttribute('data-src') || '',
                dataOriginal: img.getAttribute('data-original') || '',
                srcset: img.getAttribute('srcset') || ''
            }))""",
        )
        browser.close()

    for attrs in img_attrs:
        src = attrs.get("dataOriginal") or attrs.get("dataSrc") or attrs.get("src") or ""
        srcset = attrs.get("srcset") or ""
        if srcset:
            candidates = [c.strip().split(" ")[0] for c in srcset.split(",") if c.strip()]
            if candidates:
                src = candidates[-1]
        if not src or src.startswith("data:"):
            continue
        results.append(src)
    return results


def extract_product_json(html: str) -> dict[str, Any] | None:
    marker = "app.value('product', JSON.parse('"
    start = html.find(marker)
    if start == -1:
        return None
    i = start + len(marker)
    buf: list[str] = []
    while i < len(html):
        ch = html[i]
        if ch == "\\":
            if i + 1 < len(html):
                buf.append(html[i + 1])
                i += 2
                continue
        if ch == "'":
            break
        buf.append(ch)
        i += 1
    blob = "".join(buf)
    blob = blob.replace("\\u0026", "&").replace("\\/", "/").replace("\\\"", "\"")
    try:
        return json.loads(blob)
    except Exception:
        return None


def extract_gallery_thumbs(html: str) -> list[str]:
    product = extract_product_json(html)
    if not product:
        return []
    media = product.get("media") or []
    results: list[str] = []
    for item in media:
        if not isinstance(item, dict):
            continue
        # Prefer largest available for the gallery
        url = item.get("detail_image_url") or ""
        if not url:
            images = item.get("images") or {}
            original = (images.get("original") or {}).get("url") if isinstance(images, dict) else ""
            url = original or ""
        if not url:
            url = item.get("default_image_url") or item.get("thumb_image_url") or ""
        if url:
            results.append(url)
    return results


def extract_image_id(url: str) -> str:
    # image_clips/<id>/...
    m = re.search(r"/image_clips/([a-f0-9]+)/", url, re.IGNORECASE)
    if m:
        return m.group(1)
    # shoplineimg.com/<owner_id>/<image_id>/...
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return parts[1]
    return ""


def extract_image_urls(html: str, base_url: str) -> tuple[list[str], list[str]]:
    img_urls: list[str] = []
    img_html: list[str] = []

    # Primary: product media JSON
    json_urls = extract_product_image_urls_from_json(html)
    for u in json_urls:
        if is_likely_product_image(u):
            img_urls.append(u)
            img_html.append(f'<img src="{u}" />')

    # Secondary: scan for Shopline CDN image URLs in the full HTML
    url_pattern = re.compile(
        r'https?://(?:img\\.shoplineapp\\.com/media/image_clips|shoplineimg\\.com)/[^\"\\s]+'
    )
    for u in url_pattern.findall(html):
        if is_likely_product_image(u):
            img_urls.append(u)
            img_html.append(f'<img src="{u}" />')

    # De-duplicate while preserving order
    img_urls = list(dict.fromkeys(img_urls))
    img_html = list(dict.fromkeys(img_html))
    return img_urls, img_html


def extract_product_media_urls(html: str) -> tuple[list[str], list[str]]:
    # Focus on the first product media array only (current product)
    match = re.search(r'cover_media_array', html)
    if not match:
        return [], []

    snippet = html[match.start() : match.start() + 200000]
    unescaped = (
        snippet.replace("\\u0026", "&")
        .replace("\\/", "/")
        .replace("\\\\\"", "\"")
    )

    url_pattern = re.compile(
        r'https?://(?:img\\.shoplineapp\\.com/media/image_clips|shoplineimg\\.com)/[^\"\\s]+'
    )
    urls = url_pattern.findall(unescaped)

    detail: list[str] = []
    thumbs: list[str] = []
    for u in urls:
        if not is_likely_product_image(u):
            continue
        if classify_image(u) == "detail":
            detail.append(u)
        else:
            thumbs.append(u)

    return detail, thumbs


def is_likely_product_image(url: str) -> bool:
    lower = url.lower()
    parsed = urllib.parse.urlparse(url)
    filename = os.path.basename(parsed.path).lower()

    # Strong allowlist for Shopline product media
    if "image_clips" in lower:
        return True
    if "/products/" in lower or "/product/" in lower:
        return True
    if "shoplineimg.com" in lower or "img.shoplineapp.com" in lower:
        # Filter out obvious header/footer/payment icons by filename only
        if any(h in filename for h in NOISE_HINTS):
            return False
        return True
    return False


def safe_folder_name(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    slug = path.split("/")[-1] or "product"
    if not re.match(r"^[a-zA-Z0-9._-]+$", slug):
        slug = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return slug


def download_file(url: str, out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    data = fetch_bytes(url)
    with open(out_path, "wb") as f:
        f.write(data)


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Shopline product image downloader")
    parser.add_argument("--base", required=True, help="Base site URL (e.g., https://www.celladix.hk)")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument("--sitemap", default="", help="Sitemap URL (default: {base}/sitemap.xml)")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests in seconds")
    parser.add_argument("--max-products", type=int, default=0, help="Limit number of products (0 = no limit)")
    parser.add_argument("--debug", action="store_true", help="Print debug info for image extraction")

    args = parser.parse_args()

    base = args.base.rstrip("/")
    out_dir = args.out
    sitemap_url = args.sitemap or f"{base}/sitemap.xml"

    os.makedirs(out_dir, exist_ok=True)

    try:
        all_urls = parse_sitemap(sitemap_url)
    except Exception as exc:
        fallback = f"{base}/sitemap_index.xml"
        if not args.sitemap and sitemap_url != fallback:
            try:
                all_urls = parse_sitemap(fallback)
                sitemap_url = fallback
            except Exception as exc2:
                print(f"Failed to read sitemap: {exc}")
                print(f"Also failed sitemap_index: {exc2}")
                return 1
        else:
            print(f"Failed to read sitemap: {exc}")
            return 1

    product_urls = [u for u in all_urls if is_product_url(u)]
    product_urls = list(dict.fromkeys(product_urls))

    if args.max_products > 0:
        product_urls = product_urls[: args.max_products]

    if not product_urls:
        print("No product URLs found in sitemap. Provide a sitemap URL or add a crawler.")
        return 1

    print(f"Found {len(product_urls)} product URLs")

    for idx, url in enumerate(product_urls, start=1):
        print(f"[{idx}/{len(product_urls)}] {url}")
        try:
            html = fetch_text_force_utf8(url)

            # Extract detail images only from ProductDetail-description block (rendered)
            detail_candidates: list[str] = []
            rendered_urls = extract_detail_images_with_playwright(url)
            for src in rendered_urls:
                abs_url = normalize_url(src, base)
                if is_likely_product_image(abs_url) and is_detail_candidate(abs_url):
                    detail_candidates.append(abs_url)

            pattern_count = 0

            # Thumbnails: use product gallery JSON only
            thumb_candidates = [u for u in extract_gallery_thumbs(html) if is_likely_product_image(u)]

            # De-duplicate within each bucket and keep only largest per image id
            detail_best: dict[str, str] = {}
            detail_order: list[str] = []
            for u in dict.fromkeys(detail_candidates):
                img_id = extract_image_id(u) or normalize_image_key(u)
                if img_id not in detail_order:
                    detail_order.append(img_id)
                if img_id not in detail_best or size_score(u) > size_score(detail_best[img_id]):
                    detail_best[img_id] = u

            thumb_best: dict[str, str] = {}
            thumb_order: list[str] = []
            for u in dict.fromkeys(thumb_candidates):
                img_id = extract_image_id(u) or normalize_image_key(u)
                if img_id not in thumb_order:
                    thumb_order.append(img_id)
                if img_id not in thumb_best or size_score(u) > size_score(thumb_best[img_id]):
                    thumb_best[img_id] = u

            detail_urls = [detail_best[i] for i in detail_order if i in detail_best]
            thumb_urls = [thumb_best[i] for i in thumb_order if i in thumb_best]

            if args.debug:
                print(f"  Detail candidates (.ProductDetail-description): {len(detail_candidates)}")
                if not HAS_PLAYWRIGHT:
                    print("  Playwright not available; detail images may be empty.")
                print(f"  Thumb candidates (matched by product ids): {len(thumb_candidates)}")
                print(f"  Pattern matches: {pattern_count}")
                print(f"  Final detail urls: {len(detail_urls)}")
                print(f"  Final thumb urls: {len(thumb_urls)}")

            product_dir = os.path.join(out_dir, safe_folder_name(url))
            os.makedirs(product_dir, exist_ok=True)

            # Save image HTML (separate)
            detail_html = [f'<img src="{u}" style="display:block;" />' for u in detail_urls]
            thumb_html = [f'<img src="{u}" />' for u in thumb_urls]
            with open(os.path.join(product_dir, "images_detail.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(detail_html))
            with open(os.path.join(product_dir, "images_thumb.html"), "w", encoding="utf-8") as f:
                f.write("\n".join(thumb_html))

            # Save source URL
            with open(os.path.join(product_dir, "product_url.txt"), "w", encoding="utf-8") as f:
                f.write(url)

            # Download images into separate folders
            for img_url in detail_urls:
                img_name = os.path.basename(urllib.parse.urlparse(img_url).path) or "image"
                img_path = os.path.join(product_dir, "detail", img_name)
                if os.path.exists(img_path):
                    stem, ext = os.path.splitext(img_name)
                    suffix = 1
                    while True:
                        candidate = os.path.join(product_dir, "detail", f"{stem}_{suffix}{ext}")
                        if not os.path.exists(candidate):
                            img_path = candidate
                            break
                        suffix += 1
                try:
                    download_file(img_url, img_path)
                except Exception as exc:
                    print(f"  Failed image: {img_url} ({exc})")

            for img_url in thumb_urls:
                img_name = os.path.basename(urllib.parse.urlparse(img_url).path) or "image"
                img_path = os.path.join(product_dir, "thumbs", img_name)
                if os.path.exists(img_path):
                    stem, ext = os.path.splitext(img_name)
                    suffix = 1
                    while True:
                        candidate = os.path.join(product_dir, "thumbs", f"{stem}_{suffix}{ext}")
                        if not os.path.exists(candidate):
                            img_path = candidate
                            break
                        suffix += 1
                try:
                    download_file(img_url, img_path)
                except Exception as exc:
                    print(f"  Failed image: {img_url} ({exc})")

        except Exception as exc:
            print(f"  Failed product: {url} ({exc})")

        time.sleep(args.delay)

    print("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
