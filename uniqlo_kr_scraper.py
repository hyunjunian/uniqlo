#!/usr/bin/env python3
"""
Collect product information from the public UNIQLO KR website.

The site server-renders a Redux-style `window.__PRELOADED_STATE__` object that
contains product search/listing entities. This tool crawls public category,
feature, and product pages, extracts those entities, and optionally enriches
them from product detail pages.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import ssl
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://www.uniqlo.com/kr/ko/"
API_BASE_URL = "https://www.uniqlo.com/kr/api/commerce/v5/ko/"
DEFAULT_SEEDS = [
    BASE_URL,
    urljoin(BASE_URL, "men"),
    urljoin(BASE_URL, "kids"),
    urljoin(BASE_URL, "baby"),
    urljoin(BASE_URL, "feature/sale/women"),
    urljoin(BASE_URL, "feature/sale/men"),
    urljoin(BASE_URL, "feature/sale/kids"),
    urljoin(BASE_URL, "feature/sale/baby"),
    urljoin(BASE_URL, "feature/new/women"),
    urljoin(BASE_URL, "feature/new/men"),
    urljoin(BASE_URL, "feature/new/kids"),
    urljoin(BASE_URL, "feature/new/baby"),
]

PRODUCT_RE = re.compile(r"/kr/ko/products/(?P<product>E\d{6}-\d{3})/(?P<price_group>\d{2})")
STATE_RE = re.compile(
    r"window\.__PRELOADED_STATE__\s*=\s*(?P<state>\{.*?\})\s*</script>",
    re.DOTALL,
)


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.links.add(html.unescape(value))


@dataclass
class CrawlResult:
    products: dict[str, dict[str, Any]]
    pages_seen: int
    product_pages_seen: int
    api_requests_seen: int = 0
    category_paths_seen: int = 0


def fetch(url: str, timeout: int, retries: int, delay: float, insecure: bool) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.7",
    }
    last_error: Exception | None = None
    context = ssl._create_unverified_context() if insecure else None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout, context=context) as response:
                data = response.read()
            if delay:
                time.sleep(delay)
            return data.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(max(delay, 0.5) * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def fetch_json(url: str, timeout: int, retries: int, delay: float, insecure: bool) -> dict[str, Any]:
    return json.loads(fetch(url, timeout, retries, delay, insecure))


def normalize_url(href: str, current_url: str) -> str | None:
    if href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return None
    url = urldefrag(urljoin(current_url, href))[0]
    parsed = urlparse(url)
    if parsed.netloc != "www.uniqlo.com":
        return None
    if not parsed.path.startswith("/kr/ko/"):
        return None
    return url


def extract_links(document: str, current_url: str) -> set[str]:
    parser = LinkParser()
    parser.feed(document)
    links: set[str] = set()
    for href in parser.links:
        url = normalize_url(href, current_url)
        if url:
            links.add(url)
    return links


def extract_state(document: str) -> dict[str, Any] | None:
    match = STATE_RE.search(document.replace("\x00", ""))
    if not match:
        return None
    raw = html.unescape(match.group("state"))
    return json.loads(raw)


def walk_dict(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_dict(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_dict(child)


def compact_color(color: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": color.get("code"),
        "display_code": color.get("displayCode"),
        "name": color.get("name"),
        "filter_code": color.get("filterCode"),
    }


def price_from_flags(flags: dict[str, Any] | None) -> int | None:
    if not flags:
        return None
    for flag in flags.get("priceFlags", []) or []:
        wording = flag.get("nameWording") or {}
        value = wording.get("value")
        if isinstance(value, int):
            return value
    return None


def price_from_prices(prices: dict[str, Any] | None) -> int | None:
    if not isinstance(prices, dict):
        return None
    for key in ("promo", "base"):
        value = prices.get(key)
        if isinstance(value, dict) and isinstance(value.get("value"), int):
            return value["value"]
    return None


def price_values(prices: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not isinstance(prices, dict):
        return None, None
    base = prices.get("base")
    promo = prices.get("promo")
    base_value = base.get("value") if isinstance(base, dict) else None
    promo_value = promo.get("value") if isinstance(promo, dict) else None
    return base_value if isinstance(base_value, int) else None, promo_value if isinstance(promo_value, int) else None


def price_flags(flags: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(flags, dict):
        return []
    output = []
    for flag in flags.get("priceFlags", []) or []:
        if not isinstance(flag, dict):
            continue
        output.append(
            {
                "code": flag.get("code"),
                "name": flag.get("name"),
                "type": flag.get("type"),
                "flag_color": flag.get("flagColor"),
            }
        )
    return output


def availability(product: dict[str, Any], representative: dict[str, Any], sizes: list[str]) -> tuple[str, str]:
    sales = product.get("sales")
    if sales is None:
        sales = representative.get("sales")
    if sales is False:
        return "not_available", "품절 또는 판매중지"
    if sales is True and sizes:
        return "available", "판매중"
    if sales is True:
        return "unknown", "판매중 여부 확인 필요"
    return "unknown", "확인 필요"


def image_from_product(product: dict[str, Any], color_display_code: str | None) -> str | None:
    images = product.get("images") or {}
    main = images.get("main") or {}
    if color_display_code and isinstance(main.get(color_display_code), dict):
        return main[color_display_code].get("image")
    for item in main.values():
        if isinstance(item, dict) and item.get("image"):
            return item["image"]
    return None


def summarize_entity(entity_key: str, entity: dict[str, Any]) -> dict[str, Any] | None:
    product = entity.get("product") if isinstance(entity.get("product"), dict) else entity
    if not isinstance(product, dict):
        return None
    product_id = product.get("productId") or entity_key.split("-")[0] + "-000"
    l1_id = product.get("l1Id") or re.sub(r"\D", "", str(product_id))[:6]
    name = product.get("name")
    if not product_id or not l1_id or not name:
        return None

    representative = product.get("representative") or {}
    color = representative.get("color") if isinstance(representative, dict) else None
    if not color and product.get("colors"):
        color = product["colors"][0]
    color_display_code = color.get("displayCode") if isinstance(color, dict) else None
    price_group = product.get("priceGroup") or entity_key.rsplit("-", 1)[-1]
    detail_url = urljoin(BASE_URL, f"products/{product_id}/{price_group}")
    if color_display_code:
        detail_url = f"{detail_url}?colorDisplayCode={color_display_code}"

    sizes = []
    for size in (entity.get("sizes") or product.get("sizes") or []):
        if isinstance(size, dict):
            sizes.append(size.get("name") or size.get("displayCode"))
    first_size = entity.get("size")
    if isinstance(first_size, dict):
        sizes.insert(0, first_size.get("name") or first_size.get("displayCode"))
    sizes = sorted({str(size) for size in sizes if size})
    availability_status, availability_label = availability(product, representative, sizes)

    colors = [compact_color(item) for item in product.get("colors", []) or [] if isinstance(item, dict)]
    base_price, promo_price = price_values(product.get("prices"))
    price = (
        price_from_prices(product.get("prices"))
        or price_from_flags(product.get("flags"))
        or price_from_flags(representative.get("flags"))
    )
    rating = product.get("rating") or representative.get("rating") or {}
    flags = price_flags(product.get("flags")) or price_flags(representative.get("flags"))

    return {
        "product_id": product_id,
        "l1_id": l1_id,
        "name": name,
        "gender_category": product.get("genderCategory"),
        "size_gender": product.get("sizeGender"),
        "price_group": price_group,
        "price_krw": price,
        "base_price_krw": base_price,
        "promo_price_krw": promo_price,
        "price_flags": flags,
        "representative_color": compact_color(color) if isinstance(color, dict) else None,
        "colors": colors,
        "sizes": sizes,
        "available_sizes_count": len(sizes),
        "availability_status": availability_status,
        "availability_label": availability_label,
        "rating_average": rating.get("average"),
        "rating_count": rating.get("count"),
        "image_url": image_from_product(product, color_display_code),
        "detail_url": detail_url,
        "sales": entity.get("sales") if entity.get("sales") is not None else representative.get("sales"),
        "store_stock_only": (
            entity.get("storeStockOnly")
            if entity.get("storeStockOnly") is not None
            else product.get("storeStockOnly")
        ),
    }


def add_category_context(product: dict[str, Any], category_path: dict[str, Any]) -> dict[str, Any]:
    categories = product.setdefault("categories", [])
    category_key = category_path.get("path")
    if category_key and all(item.get("path") != category_key for item in categories):
        categories.append(category_path)
    return product


def extract_products_from_state(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for node in walk_dict(state.get("entity", {})):
        if "product" not in node:
            continue
        summary = summarize_entity("", node)
        if summary:
            products[summary["product_id"]] = summary
    return products


def extract_products_from_api_result(
    result: dict[str, Any],
    category_path: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    products: dict[str, dict[str, Any]] = {}
    for item in result.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        summary = summarize_entity("", item)
        if not summary:
            continue
        if category_path:
            summary = add_category_context(summary, category_path)
        products[summary["product_id"]] = summary
    return products


def taxonomy_paths(taxonomies: dict[str, Any], depth: str) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    if depth == "gender":
        for gender in taxonomies.get("genders", []) or []:
            paths.append(
                {
                    "path": f"{gender['id']},,,",
                    "gender_id": gender["id"],
                    "gender_name": gender.get("name"),
                }
            )
        return paths

    if depth == "class":
        source = taxonomies.get("classes", []) or []
    else:
        source = taxonomies.get("categories", []) or []

    for node in source:
        parents = node.get("parents", []) or []
        gender = parents[0] if len(parents) >= 1 else {}
        class_node = parents[1] if len(parents) >= 2 else node
        category_id = node.get("id") if depth == "category" else ""
        class_id = class_node.get("id") if depth == "category" else node.get("id")
        if not gender.get("id") or not class_id:
            continue
        path = f"{gender['id']},{class_id},{category_id or ''},"
        paths.append(
            {
                "path": path,
                "gender_id": gender.get("id"),
                "gender_name": gender.get("name"),
                "class_id": class_id,
                "class_name": class_node.get("name"),
                "category_id": category_id or None,
                "category_name": node.get("name") if depth == "category" else None,
            }
        )
    return paths


def flatten_taxonomies(taxonomies: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for level in ("genders", "classes", "categories", "subcategories"):
        singular = {
            "genders": "gender",
            "classes": "class",
            "categories": "category",
            "subcategories": "subcategory",
        }[level]
        for node in taxonomies.get(level, []) or []:
            parents = node.get("parents", []) or []
            gender = parents[0] if len(parents) >= 1 else (node if level == "genders" else {})
            class_node = parents[1] if len(parents) >= 2 else (node if level == "classes" else {})
            category = parents[2] if len(parents) >= 3 else (node if level == "categories" else {})
            if level == "genders":
                path = f"{node.get('id')},,,"
            elif level == "classes":
                path = f"{gender.get('id')},{node.get('id')},,"
            elif level == "categories":
                path = f"{gender.get('id')},{class_node.get('id')},{node.get('id')},"
            else:
                path = f"{gender.get('id')},{class_node.get('id')},{category.get('id')},{node.get('id')}"
            rows.append(
                {
                    "level": singular,
                    "id": node.get("id"),
                    "name": node.get("name"),
                    "key": node.get("key") or node.get("genderKey"),
                    "path": path,
                    "gender_id": gender.get("id"),
                    "gender_name": gender.get("name"),
                    "class_id": class_node.get("id"),
                    "class_name": class_node.get("name"),
                    "category_id": category.get("id"),
                    "category_name": category.get("name"),
                    "parent_ids": [parent.get("id") for parent in parents],
                    "parent_names": [parent.get("name") for parent in parents],
                    "raw": node,
                }
            )
    return rows


def fetch_categories(args: argparse.Namespace) -> list[dict[str, Any]]:
    taxonomy_url = f"{API_BASE_URL}products/taxonomies"
    taxonomies = fetch_json(taxonomy_url, args.timeout, args.retries, args.delay, args.insecure)
    return flatten_taxonomies(taxonomies.get("result", {}))


def products_api_url(
    path: str,
    limit: int,
    offset: int,
    sort: int,
    flag_codes: list[str] | None = None,
) -> str:
    query_params: dict[str, Any] = {"path": path, "limit": limit, "offset": offset, "sort": sort}
    if flag_codes:
        query_params["flagCodes"] = ",".join(flag_codes)
    query = urlencode(query_params)
    return f"{API_BASE_URL}products?{query}"


def crawl_api(args: argparse.Namespace) -> CrawlResult:
    products: dict[str, dict[str, Any]] = {}
    api_requests_seen = 0
    processed_category_paths = 0

    if args.api_path:
        category_paths = [{"path": path} for path in args.api_path]
    else:
        taxonomy_url = f"{API_BASE_URL}products/taxonomies"
        taxonomies = fetch_json(taxonomy_url, args.timeout, args.retries, args.delay, args.insecure)
        api_requests_seen += 1
        category_paths = taxonomy_paths(taxonomies.get("result", {}), args.taxonomy_depth)

    for index, category_path in enumerate(category_paths, 1):
        path = category_path["path"]
        offset = 0
        total: int | None = None
        processed_category_paths += 1
        if args.verbose:
            label = category_path.get("category_name") or category_path.get("class_name") or path
            print(f"[api category {index:04d}/{len(category_paths):04d}] {label} ({path})", file=sys.stderr)
        while total is None or offset < total:
            flag_codes = ["discount"] if args.discount_only else args.flag_code
            url = products_api_url(path, args.api_limit, offset, args.sort, flag_codes)
            try:
                response = fetch_json(url, args.timeout, args.retries, args.delay, args.insecure)
            except RuntimeError as exc:
                print(f"warn: {exc}", file=sys.stderr)
                break
            api_requests_seen += 1
            result = response.get("result") or {}
            pagination = result.get("pagination") or {}
            total = pagination.get("total", 0)
            count = pagination.get("count", 0)
            if args.verbose:
                print(
                    f"  [api page] offset={offset} limit={args.api_limit} count={count} total={total}",
                    file=sys.stderr,
                )
            for product_id, product in extract_products_from_api_result(result, category_path).items():
                if product_id in products and category_path:
                    product = add_category_context({**products[product_id]}, category_path)
                products[product_id] = {**products.get(product_id, {}), **product}
            if not count:
                break
            offset += count
            if args.max_api_pages and api_requests_seen >= args.max_api_pages:
                break
        if args.max_api_pages and api_requests_seen >= args.max_api_pages:
            break

    if args.details:
        detail_urls = [p.get("detail_url") for p in products.values() if p.get("detail_url")]
        for index, detail_url in enumerate(sorted(set(detail_urls)), 1):
            if args.max_details and index > args.max_details:
                break
            if args.verbose:
                print(f"[detail {index:04d}] {detail_url}", file=sys.stderr)
            try:
                document = fetch(detail_url, args.timeout, args.retries, args.delay, args.insecure)
            except RuntimeError as exc:
                print(f"warn: {exc}", file=sys.stderr)
                continue
            detail = extract_detail(document, detail_url)
            product_id = detail.get("product_id")
            if product_id:
                products[product_id] = {**products.get(product_id, {}), **detail}

    return CrawlResult(
        products=products,
        pages_seen=0,
        product_pages_seen=0,
        api_requests_seen=api_requests_seen,
        category_paths_seen=processed_category_paths,
    )


def extract_detail(document: str, url: str) -> dict[str, Any]:
    state = extract_state(document)
    details: dict[str, Any] = {"detail_url": url}
    if state:
        pdp_entity = ((state.get("entity") or {}).get("pdpEntity") or {})
        product = pdp_entity.get("product") if isinstance(pdp_entity, dict) else None
        if isinstance(product, dict):
            details.update(
                {
                    "product_id": product.get("productId") or details.get("product_id"),
                    "name": product.get("name"),
                    "gender_category": product.get("genderCategory"),
                    "colors": [compact_color(c) for c in product.get("colors", []) or []],
                    "image_url": image_from_product(product, None),
                }
            )
    product_no = re.search(r"제품 번호:\s*(\d{6})", document)
    if product_no:
        details["product_number"] = product_no.group(1)
    return {k: v for k, v in details.items() if v not in (None, "", [])}


def should_follow(url: str) -> bool:
    path = urlparse(url).path
    if PRODUCT_RE.search(path):
        return True
    allowed_parts = (
        "/kr/ko/women",
        "/kr/ko/men",
        "/kr/ko/kids",
        "/kr/ko/baby",
        "/kr/ko/feature/",
        "/kr/ko/special-feature/",
    )
    return path.startswith(allowed_parts)


def crawl(args: argparse.Namespace) -> CrawlResult:
    queue = deque(args.seed or DEFAULT_SEEDS)
    seen: set[str] = set()
    products: dict[str, dict[str, Any]] = {}
    product_pages_seen = 0

    while queue and len(seen) < args.max_pages:
        url = queue.popleft()
        if url in seen or not should_follow(url):
            continue
        seen.add(url)
        if args.verbose:
            print(f"[{len(seen):04d}] {url}", file=sys.stderr)
        try:
            document = fetch(url, args.timeout, args.retries, args.delay, args.insecure)
        except RuntimeError as exc:
            print(f"warn: {exc}", file=sys.stderr)
            continue

        state = extract_state(document)
        if state:
            for product_id, product in extract_products_from_state(state).items():
                products[product_id] = {**products.get(product_id, {}), **product}

        if PRODUCT_RE.search(urlparse(url).path):
            product_pages_seen += 1
            detail = extract_detail(document, url)
            product_id = detail.get("product_id")
            if not product_id:
                match = PRODUCT_RE.search(urlparse(url).path)
                product_id = match.group("product") if match else None
            if product_id:
                products[product_id] = {**products.get(product_id, {}), **detail}

        for link in extract_links(document, url):
            if link not in seen and should_follow(link):
                queue.append(link)

    if args.details:
        detail_urls = [p.get("detail_url") for p in products.values() if p.get("detail_url")]
        for index, detail_url in enumerate(sorted(set(detail_urls)), 1):
            if args.max_details and index > args.max_details:
                break
            if args.verbose:
                print(f"[detail {index:04d}] {detail_url}", file=sys.stderr)
            try:
                document = fetch(detail_url, args.timeout, args.retries, args.delay, args.insecure)
            except RuntimeError as exc:
                print(f"warn: {exc}", file=sys.stderr)
                continue
            detail = extract_detail(document, detail_url)
            product_id = detail.get("product_id")
            if product_id:
                products[product_id] = {**products.get(product_id, {}), **detail}

    return CrawlResult(products=products, pages_seen=len(seen), product_pages_seen=product_pages_seen)


def write_json(path: Path, result: CrawlResult) -> None:
    payload = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": API_BASE_URL,
        "pages_seen": result.pages_seen,
        "product_pages_seen": result.product_pages_seen,
        "api_requests_seen": result.api_requests_seen,
        "category_paths_seen": result.category_paths_seen,
        "product_count": len(result.products),
        "products": sorted(result.products.values(), key=lambda item: item.get("product_id", "")),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, products: dict[str, dict[str, Any]]) -> None:
    fields = [
        "product_id",
        "product_number",
        "name",
        "gender_category",
        "size_gender",
        "price_group",
        "price_krw",
        "base_price_krw",
        "promo_price_krw",
        "price_flags",
        "available_sizes_count",
        "availability_status",
        "availability_label",
        "rating_average",
        "rating_count",
        "image_url",
        "detail_url",
        "sales",
        "store_stock_only",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for product in sorted(products.values(), key=lambda item: item.get("product_id", "")):
            row = {field: product.get(field) for field in fields}
            row["price_flags"] = json.dumps(row["price_flags"], ensure_ascii=False)
            writer.writerow(row)


def write_products_html(path: Path, products: dict[str, dict[str, Any]], title: str) -> None:
    product_list = sorted(products.values(), key=lambda item: (item.get("gender_category") or "", item.get("name") or ""))
    cards = []
    for product in product_list:
        name = html.escape(str(product.get("name") or ""))
        product_id = html.escape(str(product.get("product_id") or ""))
        gender = html.escape(str(product.get("gender_category") or ""))
        image = html.escape(str(product.get("image_url") or ""))
        detail = html.escape(str(product.get("detail_url") or "#"))
        price = product.get("price_krw")
        base_price = product.get("base_price_krw")
        flags = ", ".join(flag.get("name") or flag.get("code") or "" for flag in product.get("price_flags", []))
        flags_text = html.escape(flags)
        price_text = f"{price:,}원" if isinstance(price, int) else ""
        base_text = f"{base_price:,}원" if isinstance(base_price, int) and base_price != price else ""
        cards.append(
            f"""
      <article class="card" data-name="{name.lower()}" data-gender="{gender}">
        <a href="{detail}" target="_blank" rel="noreferrer">
          <div class="media">{f'<img src="{image}" alt="{name}" loading="lazy">' if image else ''}</div>
          <div class="body">
            <div class="meta">{gender}</div>
            <h2>{name}</h2>
            <div class="price">{price_text}{f'<span>{base_text}</span>' if base_text else ''}</div>
            <div class="flag">{flags_text}</div>
            <div class="id">{product_id}</div>
          </div>
        </a>
      </article>"""
        )

    payload = json.dumps(
        {
            "title": title,
            "count": len(product_list),
            "genders": sorted({p.get("gender_category") for p in product_list if p.get("gender_category")}),
        },
        ensure_ascii=False,
    )
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: Arial, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif; color: #111; background: #f6f6f6; }}
    header {{ position: sticky; top: 0; z-index: 2; background: #fff; border-bottom: 1px solid #ddd; padding: 18px 24px; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    .controls {{ display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }}
    input, select {{ height: 36px; border: 1px solid #bbb; border-radius: 4px; padding: 0 10px; background: #fff; font-size: 14px; }}
    input {{ width: min(210px, 100%); }}
    .count {{ color: #555; font-size: 14px; }}
    main {{ padding: 24px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 16px; }}
    .card {{ background: #fff; border: 1px solid #ddd; border-radius: 6px; overflow: hidden; }}
    .card a {{ color: inherit; text-decoration: none; display: block; height: 100%; }}
    .media {{ aspect-ratio: 3 / 4; background: #eee; display: grid; place-items: center; }}
    .media img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
    .body {{ padding: 12px; }}
    .meta, .id, .flag {{ color: #666; font-size: 12px; line-height: 1.35; }}
    h2 {{ font-size: 15px; line-height: 1.35; min-height: 40px; margin: 6px 0 10px; }}
    .price {{ font-weight: 700; font-size: 16px; color: #e60012; margin-bottom: 6px; }}
    .price span {{ margin-left: 8px; color: #777; font-weight: 400; font-size: 12px; text-decoration: line-through; }}
    .hidden {{ display: none; }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <div class="controls">
      <input id="q" type="search" placeholder="상품명 또는 제품번호 검색">
      <select id="gender"><option value="">성별 전체</option></select>
      <span class="count" id="count"></span>
    </div>
  </header>
  <main><section class="grid" id="grid">{''.join(cards)}
  </section></main>
  <script>
    const meta = {payload};
    const q = document.getElementById('q');
    const gender = document.getElementById('gender');
    const count = document.getElementById('count');
    const cards = [...document.querySelectorAll('.card')];
    meta.genders.forEach(g => {{
      const option = document.createElement('option');
      option.value = g;
      option.textContent = g;
      gender.appendChild(option);
    }});
    function render() {{
      const query = q.value.trim().toLowerCase();
      const selectedGender = gender.value;
      let visible = 0;
      cards.forEach(card => {{
        const okQuery = !query || card.dataset.name.includes(query) || card.textContent.toLowerCase().includes(query);
        const okGender = !selectedGender || card.dataset.gender === selectedGender;
        const ok = okQuery && okGender;
        card.classList.toggle('hidden', !ok);
        if (ok) visible += 1;
      }});
      count.textContent = `${{visible.toLocaleString()}} / ${{meta.count.toLocaleString()}}개`;
    }}
    q.addEventListener('input', render);
    gender.addEventListener('change', render);
    render();
  </script>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")


def write_categories_json(path: Path, categories: list[dict[str, Any]]) -> None:
    payload = {
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{API_BASE_URL}products/taxonomies",
        "category_count": len(categories),
        "categories": categories,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_categories_csv(path: Path, categories: list[dict[str, Any]]) -> None:
    fields = [
        "level",
        "id",
        "name",
        "key",
        "path",
        "gender_id",
        "gender_name",
        "class_id",
        "class_name",
        "category_id",
        "category_name",
        "parent_ids",
        "parent_names",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in categories:
            output = {field: row.get(field) for field in fields}
            output["parent_ids"] = json.dumps(output["parent_ids"], ensure_ascii=False)
            output["parent_names"] = json.dumps(output["parent_names"], ensure_ascii=False)
            writer.writerow(output)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scrape public UNIQLO KR product information.")
    parser.add_argument("--mode", choices=("api", "html", "categories", "discounts"), default="api", help="Collection mode.")
    parser.add_argument("--seed", action="append", help="Seed URL. Can be passed multiple times.")
    parser.add_argument("--api-path", action="append", help="API category path, e.g. '57892,57959,,'.")
    parser.add_argument("--api-limit", type=int, default=100, help="API products limit per request. UNIQLO currently accepts up to 100.")
    parser.add_argument("--max-api-pages", type=int, default=0, help="Limit API requests; 0 means no limit.")
    parser.add_argument("--sort", type=int, default=0, help="UNIQLO API sort value.")
    parser.add_argument("--flag-code", action="append", help="Filter products by API flag code. Can be passed multiple times.")
    parser.add_argument("--discount-only", action="store_true", help="Shortcut for --flag-code discount.")
    parser.add_argument("--html", default="", help="Optional product browser HTML output path.")
    parser.add_argument(
        "--taxonomy-depth",
        choices=("gender", "class", "category"),
        default="class",
        help="Taxonomy level to request from the products API.",
    )
    parser.add_argument("--out", default="data/uniqlo_kr_products.json", help="JSON output path.")
    parser.add_argument("--csv", default="", help="Optional CSV output path.")
    parser.add_argument("--max-pages", type=int, default=300, help="Maximum listing/category pages to crawl.")
    parser.add_argument("--details", action="store_true", help="Fetch product detail pages for enrichment.")
    parser.add_argument("--max-details", type=int, default=0, help="Limit detail pages; 0 means no limit.")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between requests in seconds.")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout in seconds.")
    parser.add_argument("--retries", type=int, default=2, help="Retries per request.")
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for local CA issues.")
    parser.add_argument("--verbose", action="store_true", help="Print crawl progress to stderr.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.mode == "categories":
        categories = fetch_categories(args)
        write_categories_json(Path(args.out), categories)
        if args.csv:
            write_categories_csv(Path(args.csv), categories)
        print(f"saved {len(categories)} categories to {args.out}")
        if args.csv:
            print(f"saved csv to {args.csv}")
        return 0

    if args.mode == "discounts":
        args.discount_only = True
        if not args.out or args.out == "data/uniqlo_kr_products.json":
            args.out = "data/uniqlo_kr_discount_items.json"
        if not args.csv:
            args.csv = "data/uniqlo_kr_discount_items.csv"
        if not args.html:
            args.html = "data/uniqlo_kr_discount_items.html"
        result = crawl_api(args)
        write_json(Path(args.out), result)
        write_csv(Path(args.csv), result.products)
        write_products_html(Path(args.html), result.products, "UNIQLO KR 할인 상품")
        print(
            f"saved {len(result.products)} discount products from "
            f"{result.category_paths_seen} category paths and {result.api_requests_seen} API requests"
        )
        print(f"saved json to {args.out}")
        print(f"saved csv to {args.csv}")
        print(f"saved html to {args.html}")
        return 0

    result = crawl_api(args) if args.mode == "api" else crawl(args)
    write_json(Path(args.out), result)
    if args.csv:
        write_csv(Path(args.csv), result.products)
    if args.html:
        write_products_html(Path(args.html), result.products, "UNIQLO KR 상품")
    if args.mode == "api":
        print(
            f"saved {len(result.products)} products from "
            f"{result.category_paths_seen} category paths and {result.api_requests_seen} API requests to {args.out}"
        )
    else:
        print(f"saved {len(result.products)} products from {result.pages_seen} pages to {args.out}")
    if args.csv:
        print(f"saved csv to {args.csv}")
    if args.html:
        print(f"saved html to {args.html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
