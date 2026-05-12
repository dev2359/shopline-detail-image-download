#!/usr/bin/env python3
"""Download product page image HTML and image files from a Shopline site.

Usage:
  python shopline_image_downloader.py --base https://www.celladix.hk --out output
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable

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

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


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


def parse_sitemap_loose(xml_text: str) -> list[str]:
    urls: list[str] = []
    for loc in re.findall(r"<loc>(.*?)</loc>", xml_text, re.IGNORECASE | re.DOTALL):
        loc = loc.strip()
        if not loc:
            continue
        loc = (
            loc.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", "\"")
            .replace("&#39;", "'")
        )
        urls.append(loc)
    return urls


def parse_sitemap(url: str, seen: set[str] | None = None) -> list[str]:
    if seen is None:
        seen = set()
    if url in seen:
        return []
    seen.add(url)

    xml_text = fetch_text_force_utf8(url)
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return parse_sitemap_loose(xml_text)
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


def discover_sitemaps(base: str) -> list[str]:
    robots_url = f"{base.rstrip('/')}/robots.txt"
    try:
        text = fetch_text_force_utf8(robots_url)
    except Exception:
        return []
    sitemaps: list[str] = []
    for line in text.splitlines():
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url:
                sitemaps.append(url)
    return sitemaps


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
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            # Fallback to less strict load conditions
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
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


def extract_product_id_from_rel(html: str) -> str:
    # Example: <div id="product_id" rel="9c019a5baad2"></div>
    patterns = [
        r'id=["\']product_id["\'][^>]*\brel=["\']([^"\']+)["\']',
        r'\brel=["\']([^"\']+)["\'][^>]*\bid=["\']product_id["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


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


def safe_folder_name(url: str, product_id: str | None = None) -> str:
    if product_id:
        return str(product_id)
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

def process_product_url(
    url: str,
    base: str,
    out_dir: str,
    debug: bool = False,
    external_base: str = "",
    external_folder: str = "",
    external_prefix: str = "",
    thumbs_only: bool = False,
) -> tuple[str, list[str], list[str]]:
    html = fetch_text_force_utf8(url)
    product = extract_product_json(html) or {}
    rel_id = extract_product_id_from_rel(html)
    product_id = rel_id or str(product.get("id") or "").strip() or None

    # Extract detail images only from ProductDetail-description block (rendered)
    detail_candidates: list[str] = []
    if not thumbs_only:
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

    detail_urls = [] if thumbs_only else [detail_best[i] for i in detail_order if i in detail_best]
    thumb_urls = [thumb_best[i] for i in thumb_order if i in thumb_best]

    if debug:
        print(f"  Detail candidates (.ProductDetail-description): {len(detail_candidates)}")
        if not HAS_PLAYWRIGHT:
            print("  Playwright not available; detail images may be empty.")
        print(f"  Thumb candidates (matched by product ids): {len(thumb_candidates)}")
        print(f"  Pattern matches: {pattern_count}")
        print(f"  Final detail urls: {len(detail_urls)}")
        print(f"  Final thumb urls: {len(thumb_urls)}")

    product_dir = os.path.join(out_dir, safe_folder_name(url, product_id))
    folder_id = os.path.basename(product_dir)
    os.makedirs(product_dir, exist_ok=True)

    # Save image HTML (separate)
    external_base = external_base.rstrip("/")
    external_folder = external_folder.strip().strip("/")
    external_prefix = external_prefix.strip()
    id_for_path = product_id or os.path.basename(product_dir)
    detail_html: list[str] = []
    thumb_html: list[str] = []
    base_path = ""
    if external_base:
        if external_folder:
            base_path = f"{external_base}/{external_folder}/{id_for_path}"
        elif not thumbs_only:
            base_path = external_base

    if thumbs_only:
        if base_path and thumb_urls:
            thumb_ext = os.path.splitext(urllib.parse.urlparse(thumb_urls[0]).path)[1] or ".jpg"
            thumb_url = f"{base_path}/thumb{thumb_ext}"
        else:
            thumb_url = thumb_urls[0] if thumb_urls else ""
        if thumb_url:
            detail_html = [f'<img src="{thumb_url}" style="display:block;" />']
    elif external_base:
        for idx, src in enumerate(detail_urls):
            if idx > 99:
                break
            ext = os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".jpg"
            name = f"{external_prefix}{idx}{ext}"
            url = f"{base_path}/{name}"
            detail_html.append(f'<img src="{url}" style="display:block;" />')
        # Only images_detail.html is needed; skip generating thumbnail HTML
    else:
        detail_html = [f'<img src="{u}" style="display:block;" />' for u in detail_urls]
        # Only images_detail.html is needed; skip generating thumbnail HTML
    if not thumbs_only:
        with open(os.path.join(product_dir, "images_detail.html"), "w", encoding="utf-8") as f:
            f.write("\n".join(detail_html))
    # images_thumb.html is intentionally omitted

    # Save source URL
    if not thumbs_only:
        with open(os.path.join(product_dir, "product_url.txt"), "w", encoding="utf-8") as f:
            f.write(url)

    # Download images into separate folders
    if not thumbs_only:
        for idx, img_url in enumerate(detail_urls):
            if idx > 99:
                break
            parsed = urllib.parse.urlparse(img_url)
            ext = os.path.splitext(parsed.path)[1] or ".jpg"
            prefix = external_prefix or ""
            img_name = f"{prefix}{idx}{ext}"
            img_path = os.path.join(product_dir, img_name)
            try:
                download_file(img_url, img_path)
            except Exception as exc:
                print(f"  Failed image: {img_url} ({exc})")

    if thumb_urls:
        first_thumb = thumb_urls[0]
        thumb_name = "thumb.jpg"
        thumb_path = os.path.join(product_dir, thumb_name)
        try:
            download_file(first_thumb, thumb_path)
        except Exception as exc:
            print(f"  Failed image: {first_thumb} ({exc})")

    return product_dir, detail_urls, thumb_urls


def run_for_base(
    base: str,
    out_dir: str,
    sitemap_url: str,
    delay: float,
    max_products: int,
    debug: bool,
    progress_cb: Callable[[int, int, str], None] | None = None,
    external_base: str = "",
    external_folder: str = "",
    external_prefix: str = "",
    thumbs_only: bool = False,
) -> list[str]:
    # Prefer www. sitemap if available to avoid non-www redirects/HTML
    parsed_base = urllib.parse.urlparse(base)
    if parsed_base.netloc and not parsed_base.netloc.startswith("www."):
        www_base = f"{parsed_base.scheme}://www.{parsed_base.netloc}"
        www_sitemap = f"{www_base}/sitemap.xml"
        try:
            www_urls = parse_sitemap(www_sitemap)
        except Exception:
            www_urls = []
        if www_urls:
            base = www_base
            sitemap_url = www_sitemap

    try:
        all_urls = parse_sitemap(sitemap_url)
    except Exception as exc:
        fallback = f"{base}/sitemap_index.xml"
        if sitemap_url != fallback:
            try:
                all_urls = parse_sitemap(fallback)
                sitemap_url = fallback
            except Exception as exc2:
                print(f"Failed to read sitemap: {exc}")
                print(f"Also failed sitemap_index: {exc2}")
                return []
        else:
            print(f"Failed to read sitemap: {exc}")
            return []

    product_urls = [u for u in all_urls if is_product_url(u)]
    product_urls = list(dict.fromkeys(product_urls))

    if max_products > 0:
        product_urls = product_urls[: max_products]

    if not product_urls:
        # Try robots.txt-discovered sitemaps
        extra_sitemaps = discover_sitemaps(base)
        for sm in extra_sitemaps:
            try:
                more_urls = parse_sitemap(sm)
            except Exception:
                continue
            for u in more_urls:
                if is_product_url(u):
                    product_urls.append(u)
        product_urls = list(dict.fromkeys(product_urls))

    if not product_urls:
        # Try www. variant for base if not already
        parsed = urllib.parse.urlparse(base)
        if parsed.netloc and not parsed.netloc.startswith("www."):
            www_base = f"{parsed.scheme}://www.{parsed.netloc}"
            www_sitemap = f"{www_base}/sitemap.xml"
            try:
                all_urls = parse_sitemap(www_sitemap)
            except Exception:
                all_urls = []
            if not all_urls:
                for sm in discover_sitemaps(www_base):
                    try:
                        all_urls.extend(parse_sitemap(sm))
                    except Exception:
                        continue
            product_urls = [u for u in all_urls if is_product_url(u)]
            product_urls = list(dict.fromkeys(product_urls))

    if not product_urls:
        print("No product URLs found in sitemap. Provide a sitemap URL or add a crawler.")
        return []

    total = len(product_urls)
    print(f"Found {total} product URLs")

    product_dirs: list[str] = []
    for idx, url in enumerate(product_urls, start=1):
        print(f"[{idx}/{total}] {url}")
        try:
            product_dir, _, _ = process_product_url(
                url,
                base,
                out_dir,
                debug=debug,
                external_base=external_base,
                external_folder=external_folder,
                external_prefix=external_prefix,
                thumbs_only=thumbs_only,
            )
            product_dirs.append(product_dir)
        except Exception as exc:
            print(f"  Failed product: {url} ({exc})")

        if progress_cb:
            progress_cb(idx, total, url)

        time.sleep(delay)

    return product_dirs


def run_for_product(
    product_url: str,
    out_dir: str,
    debug: bool,
    progress_cb: Callable[[int, int, str], None] | None = None,
    external_base: str = "",
    external_folder: str = "",
    external_prefix: str = "",
    thumbs_only: bool = False,
) -> list[str]:
    parsed = urllib.parse.urlparse(product_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    product_dir, _, _ = process_product_url(
        product_url,
        base,
        out_dir,
        debug=debug,
        external_base=external_base,
        external_folder=external_folder,
        external_prefix=external_prefix,
        thumbs_only=thumbs_only,
    )
    if progress_cb:
        progress_cb(1, 1, product_url)
    return [product_dir]


def run_for_products(
    product_urls: list[str],
    out_dir: str,
    debug: bool,
    progress_cb: Callable[[int, int, str], None] | None = None,
    external_base: str = "",
    external_folder: str = "",
    external_prefix: str = "",
    thumbs_only: bool = False,
) -> list[str]:
    product_dirs: list[str] = []
    total = len(product_urls)
    for idx, product_url in enumerate(product_urls, start=1):
        parsed = urllib.parse.urlparse(product_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        product_dir, _, _ = process_product_url(
            product_url,
            base,
            out_dir,
            debug=debug,
            external_base=external_base,
            external_folder=external_folder,
            external_prefix=external_prefix,
            thumbs_only=thumbs_only,
        )
        product_dirs.append(product_dir)
        if progress_cb:
            progress_cb(idx, total, product_url)
    return product_dirs


def zip_directory(src_dir: str, zip_path: str) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(src_dir):
            for name in files:
                full_path = os.path.join(root, name)
                rel_path = os.path.relpath(full_path, src_dir)
                zf.write(full_path, rel_path)


def _set_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id, {})
        job.update(fields)
        JOBS[job_id] = job


def _get_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return dict(job)


def _run_job(job_id: str, params: dict[str, Any]) -> None:
    try:
        base = params.get("base") or ""
        sitemap = params.get("sitemap") or ""
        product_urls = params.get("product_url") or []
        delay = float(params.get("delay") or 0.5)
        max_products = int(params.get("max_products") or 0)
        output_root = params.get("output_root") or os.getcwd()
        external_base = (params.get("external_base") or "").strip()
        external_folder = (params.get("external_folder") or "").strip()
        external_prefix = (params.get("external_prefix") or "").strip()
        thumbs_only = bool(params.get("thumbs_only"))

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = os.path.join(output_root, f"web_output_{stamp}_{job_id[:8]}")
        os.makedirs(out_dir, exist_ok=True)

        def progress(done: int, total: int, current: str) -> None:
            _set_job(job_id, done=done, total=total, current=current)

        if product_urls:
            _set_job(job_id, mode="product")
            run_for_products(
                product_urls,
                out_dir,
                debug=False,
                progress_cb=progress,
                external_base=external_base,
                external_folder=external_folder,
                external_prefix=external_prefix,
                thumbs_only=thumbs_only,
            )
        else:
            if not base:
                raise ValueError("base is required")
            base = base.rstrip("/")
            sitemap_url = sitemap or f"{base}/sitemap.xml"
            _set_job(job_id, mode="base")
            run_for_base(
                base,
                out_dir,
                sitemap_url,
                delay,
                max_products,
                debug=False,
                progress_cb=progress,
                external_base=external_base,
                external_folder=external_folder,
                external_prefix=external_prefix,
                thumbs_only=thumbs_only,
            )

        zip_path = f"{out_dir}.zip"
        zip_directory(out_dir, zip_path)
        _set_job(job_id, status="done", zip_path=zip_path, out_dir=out_dir, message="done")
    except Exception as exc:
        _set_job(job_id, status="error", message=str(exc))


class DownloadHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ShoplineDownloader/1.0"

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/status":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = (query.get("id", [""])[0] or "").strip()
            job = _get_job(job_id)
            if not job:
                self.send_error(404, "Job not found")
                return
            payload = json.dumps(job, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/open":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = (query.get("id", [""])[0] or "").strip()
            job = _get_job(job_id)
            if not job or job.get("status") != "done":
                self.send_error(404, "Job not ready")
                return
            out_dir = job.get("out_dir")
            if not out_dir or not os.path.exists(out_dir):
                self.send_error(404, "Folder not found")
                return
            try:
                os.startfile(out_dir)
            except Exception:
                pass
            self._send_html("<pre>opened</pre>")
            return

        if parsed.path == "/download":
            query = urllib.parse.parse_qs(parsed.query)
            job_id = (query.get("id", [""])[0] or "").strip()
            job = _get_job(job_id)
            if not job or job.get("status") != "done":
                self.send_error(404, "Job not ready")
                return
            zip_path = job.get("zip_path")
            if not zip_path or not os.path.exists(zip_path):
                self.send_error(404, "File not found")
                return
            with open(zip_path, "rb") as f:
                payload = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", "attachment; filename=shopline_download.zip")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path != "/":
            self.send_error(404, "Not Found")
            return
        body = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Shopline Image Downloader</title>
    <style>
      :root {
        --bg1: #0b1220;
        --bg2: #0f172a;
        --card: #0b1526;
        --accent: #5ea2ff;
        --text: #e5e7eb;
        --muted: #9aa4b2;
        --danger: #f87171;
        --ok: #34d399;
        --border: #2c3a52;
      }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: "Noto Sans KR", "Segoe UI", Arial, sans-serif;
        color: var(--text);
        background: radial-gradient(1200px 800px at 20% 0%, #111c2e, var(--bg2));
      }
      .wrap {
        max-width: 880px;
        margin: 0 auto;
        padding: 36px 24px 60px;
      }
      .card {
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 24px;
        box-shadow: 0 12px 30px rgba(15, 23, 42, 0.12);
      }
      h1 { margin: 0 0 8px; font-size: 26px; }
      p { margin: 6px 0 16px; color: var(--muted); }
      .grid { display: grid; grid-template-columns: 1fr; gap: 14px; }
      .row-inline { display: flex; gap: 8px; align-items: center; }
      .row-inline input { flex: 1; }
      .row-inline button { height: 40px; padding: 0 12px; }
      .toggle {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 13px;
        color: var(--muted);
        cursor: pointer;
        user-select: none;
      }
      .toggle input { width: auto; }
      .section { padding: 8px 0; border-top: 1px solid #1f2937; }
      .section:first-child { border-top: none; }
      label { display: block; margin: 10px 0 6px; font-size: 14px; color: var(--muted); }
      input {
        width: 100%;
        box-sizing: border-box;
        padding: 10px 12px;
        border-radius: 10px;
        border: 1px solid var(--border);
        background: #0a1424;
        color: var(--text);
        outline: none;
        box-shadow: inset 0 0 0 1px rgba(94, 162, 255, 0.15);
      }
      input:focus {
        border-color: var(--accent);
        box-shadow: 0 0 0 2px rgba(94, 162, 255, 0.25);
      }
      .actions { margin-top: 16px; display: flex; gap: 10px; align-items: center; }
      button {
        padding: 10px 14px;
        border-radius: 10px;
        border: none;
        background: var(--accent);
        color: #0b1220;
        font-weight: 600;
        cursor: pointer;
      }
      button.secondary { background: #1b2842; color: var(--text); border: 1px solid var(--border); }
      .progress-wrap {
        margin-top: 18px;
        padding: 12px;
        border-radius: 12px;
        border: 1px dashed var(--border);
        background: #0a1424;
      }
      .bar {
        width: 100%;
        height: 10px;
        background: #0b1629;
        border-radius: 999px;
        overflow: hidden;
        border: 1px solid var(--border);
      }
      .bar > span {
        display: block;
        height: 100%;
        width: 0%;
        background: linear-gradient(90deg, #38bdf8, #22c55e);
      }
      .meta { font-size: 12px; color: var(--muted); margin-top: 6px; }
      .status { font-size: 14px; margin-top: 10px; }
      .status.ok { color: var(--ok); }
      .status.err { color: var(--danger); }
      .links { margin-top: 10px; display: flex; gap: 10px; flex-wrap: wrap; }
      .links a {
        padding: 8px 12px;
        border-radius: 10px;
        background: #1b2842;
        color: var(--text);
        text-decoration: none;
        border: 1px solid var(--border);
        font-size: 13px;
      }
      .footer { margin-top: 18px; font-size: 12px; color: var(--muted); }
      @media (max-width: 820px) {
        .grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div class="wrap">
      <div class="card">
        <h1>Shopline Image Downloader</h1>
        <p>메인 도메인 전체 다운로드 또는 특정 상품 URL 단건 다운로드를 지원합니다.</p>
        <form id="downloadForm">
          <div class="grid">
            <div class="section">
              <label>메인 도메인</label>
              <input type="text" name="base" placeholder="https://www.celladix.hk" />
            </div>
            <div class="section">
              <label>특정 상품 상세 URL</label>
              <div id="productList"></div>
              <button type="button" class="secondary" id="addProductBtn" style="width:100%; margin-top:6px;">+ URL 추가</button>
            </div>
            <div class="section">
              <label class="toggle">
                <input type="checkbox" id="toggleAdvanced" />
                사이트맵/최대상품수 열기
              </label>
              <div id="advancedFields" style="display:none; margin-top:8px;">
                <label>사이트맵 URL (선택)</label>
                <input type="text" name="sitemap" placeholder="https://www.celladix.hk/sitemap.xml" />
                <label>최대 상품 수 (선택)</label>
                <input type="number" name="max_products" placeholder="0 = 전체" />
              </div>
            </div>
            <div class="section">
              <label>요청 간 딜레이(초)</label>
              <input type="number" name="delay" step="0.1" value="0.5" />
            </div>
            <div class="section">
              <label>외부 서버 Base URL (선택)</label>
              <input type="text" name="external_base" value="https://objectstorage.ap-chuncheon-1.oraclecloud.com/n/axgvkldvr9i4/b/isamogu-media-bucket/o" />
            </div>
            <div class="section">
              <label>폴더명 (선택)</label>
              <input type="text" name="external_folder" placeholder="Celladix_HK" />
            </div>
            <div class="section">
              <label>파일 접두어 (선택)</label>
              <input type="text" name="external_prefix" placeholder="Celladix_" />
            </div>
            <div class="section">
              <label class="toggle">
                <input type="checkbox" name="thumbs_only" />
                썸네일만 다운로드
              </label>
            </div>
          </div>
          <div class="actions">
            <button type="submit">다운로드 시작</button>
            <button type="button" class="secondary" id="resetBtn">초기화</button>
          </div>
        </form>
        <div class="progress-wrap" id="progressWrap" style="display:none;">
          <div class="bar"><span id="barFill"></span></div>
          <div class="meta" id="metaText">대기 중...</div>
          <div class="status" id="statusText"></div>
          <div class="links" id="resultLinks" style="display:none;">
            <a id="downloadLink" href="#">ZIP 다운로드</a>
            <a id="openFolderLink" href="#">폴더 열기</a>
          </div>
        </div>
        <div class="footer">대량 다운로드는 시간이 오래 걸릴 수 있습니다.</div>
      </div>
    </div>
    <script>
      const form = document.getElementById('downloadForm');
      const resetBtn = document.getElementById('resetBtn');
      const progressWrap = document.getElementById('progressWrap');
      const barFill = document.getElementById('barFill');
      const metaText = document.getElementById('metaText');
      const statusText = document.getElementById('statusText');
      const productList = document.getElementById('productList');
      const addProductBtn = document.getElementById('addProductBtn');
      const resultLinks = document.getElementById('resultLinks');
      const downloadLink = document.getElementById('downloadLink');
      const openFolderLink = document.getElementById('openFolderLink');
      const toggleAdvanced = document.getElementById('toggleAdvanced');
      const advancedFields = document.getElementById('advancedFields');

      function setStatus(msg, ok) {
        statusText.textContent = msg;
        statusText.className = 'status ' + (ok ? 'ok' : 'err');
      }

      function updateProgress(done, total, current) {
        const pct = total ? Math.round((done / total) * 100) : 0;
        barFill.style.width = pct + '%';
        metaText.textContent = `진행률 ${done}/${total} (${pct}%) - ${current || ''}`;
      }

      function poll(jobId) {
        fetch(`/status?id=${jobId}`).then(r => r.json()).then(data => {
          const done = data.done || 0;
          const total = data.total || 0;
          updateProgress(done, total, data.current || '');
          if (data.status === 'done') {
            setStatus('완료됨: 아래 링크를 사용하세요.', true);
            downloadLink.href = `/download?id=${jobId}`;
            openFolderLink.href = `/open?id=${jobId}`;
            resultLinks.style.display = 'flex';
            return;
          }
          if (data.status === 'error') {
            setStatus('오류: ' + (data.message || '실패'), false);
            return;
          }
          setStatus('처리 중...', true);
          setTimeout(() => poll(jobId), 1000);
        }).catch(() => {
          setStatus('상태 확인 실패', false);
          setTimeout(() => poll(jobId), 1500);
        });
      }

      function addProductRow(value) {
        const row = document.createElement('div');
        row.className = 'row-inline';
        const input = document.createElement('input');
        input.type = 'text';
        input.name = 'product_url';
        input.placeholder = 'https://www.celladix.hk/products/...';
        if (value) input.value = value;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'secondary';
        btn.textContent = '- 제거';
        btn.addEventListener('click', () => {
          row.remove();
        });
        row.appendChild(input);
        row.appendChild(btn);
        productList.appendChild(row);
      }

      addProductBtn.addEventListener('click', () => addProductRow(''));
      addProductRow('');

      toggleAdvanced.addEventListener('change', () => {
        advancedFields.style.display = toggleAdvanced.checked ? 'block' : 'none';
      });

      form.addEventListener('submit', (e) => {
        e.preventDefault();
        const formData = new FormData(form);
        const body = new URLSearchParams(formData);
        progressWrap.style.display = 'block';
        barFill.style.width = '0%';
        metaText.textContent = '요청 전송 중...';
        statusText.textContent = '';
        resultLinks.style.display = 'none';
        fetch('/start', { method: 'POST', body }).then(r => r.json()).then(data => {
          if (!data.job_id) {
            setStatus('요청 실패', false);
            return;
          }
          setStatus('처리 시작', true);
          poll(data.job_id);
        }).catch(() => {
          setStatus('요청 실패', false);
        });
      });

      resetBtn.addEventListener('click', () => {
        form.reset();
        progressWrap.style.display = 'none';
      });
    </script>
  </body>
</html>
"""
        self._send_html(body)

    def do_POST(self) -> None:
        if self.path != "/start":
            self.send_error(404, "Not Found")
            return
        # Avoid hanging forever on incomplete POST bodies
        try:
            self.connection.settimeout(10)
        except Exception:
            pass
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length).decode("utf-8")
        params = urllib.parse.parse_qs(data)
        base = (params.get("base", [""])[0] or "").strip()
        sitemap = (params.get("sitemap", [""])[0] or "").strip()
        product_urls = [p.strip() for p in params.get("product_url", []) if p.strip()]
        delay_raw = (params.get("delay", ["0.5"])[0] or "0.5").strip()
        max_raw = (params.get("max_products", ["0"])[0] or "0").strip()
        external_base = (params.get("external_base", [""])[0] or "").strip()
        external_folder = (params.get("external_folder", [""])[0] or "").strip()
        external_prefix = (params.get("external_prefix", [""])[0] or "").strip()

        try:
            delay = float(delay_raw)
        except ValueError:
            delay = 0.5
        try:
            max_products = int(max_raw)
        except ValueError:
            max_products = 0

        output_root = getattr(self.server, "output_root", os.getcwd())
        job_id = uuid.uuid4().hex
        _set_job(job_id, status="running", done=0, total=0, current="")
        job_params = {
            "base": base,
            "sitemap": sitemap,
            "product_url": product_urls,
            "delay": delay,
            "max_products": max_products,
            "output_root": output_root,
            "external_base": external_base,
            "external_folder": external_folder,
            "external_prefix": external_prefix,
            "thumbs_only": bool(params.get("thumbs_only")),
        }
        thread = threading.Thread(target=_run_job, args=(job_id, job_params), daemon=True)
        thread.start()

        payload = json.dumps({"job_id": job_id}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def run_web_server(host: str, port: int, output_root: str) -> int:
    server = http.server.ThreadingHTTPServer((host, port), DownloadHandler)
    setattr(server, "output_root", output_root)
    print(f"Web server running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Shopline product image downloader")
    parser.add_argument("--base", required=False, help="Base site URL (e.g., https://www.celladix.hk)")
    parser.add_argument("--out", required=False, help="Output directory")
    parser.add_argument("--sitemap", default="", help="Sitemap URL (default: {base}/sitemap.xml)")
    parser.add_argument("--delay", type=float, default=0.5, help="Delay between requests in seconds")
    parser.add_argument("--max-products", type=int, default=0, help="Limit number of products (0 = no limit)")
    parser.add_argument("--debug", action="store_true", help="Print debug info for image extraction")
    parser.add_argument("--product-url", default="", help="Single product detail URL")
    parser.add_argument("--thumbs-only", action="store_true", help="Download thumbnails only")
    parser.add_argument("--web", action="store_true", help="Run as a local web server")
    parser.add_argument("--host", default="127.0.0.1", help="Web server host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Web server port (default: 8000)")

    args = parser.parse_args()

    if args.web:
        output_root = os.path.abspath(args.out or os.getcwd())
        return run_web_server(args.host, args.port, output_root)

    if not args.out:
        print("--out is required unless --web is used.")
        return 2

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)

    if args.product_url:
        try:
            run_for_product(args.product_url, out_dir, debug=args.debug, thumbs_only=args.thumbs_only)
        except Exception as exc:
            print(f"Failed product: {exc}")
            return 1
        print("Done")
        return 0

    if not args.base:
        print("--base is required unless --product-url is used.")
        return 2

    base = args.base.rstrip("/")
    sitemap_url = args.sitemap or f"{base}/sitemap.xml"

    run_for_base(
        base,
        out_dir,
        sitemap_url,
        args.delay,
        args.max_products,
        args.debug,
        thumbs_only=args.thumbs_only,
    )

    print("Done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
