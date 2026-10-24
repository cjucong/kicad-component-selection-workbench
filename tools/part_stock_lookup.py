# -*- coding: utf-8 -*-
"""Semi-real-time LCSC stock lookup for the local part-selection web UI."""

from __future__ import annotations

import json
import difflib
import os
import re
import shutil
import subprocess
import threading
import time
import base64
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, FIRST_COMPLETED, as_completed, wait
from copy import deepcopy
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


LCSC_QUERY_URL = "https://wmsc.lcsc.com/ftps/wm/product/query/list"
SZLCSC_SEARCH_URL = "https://so.szlcsc.com/global.html?k={c_code}"
SZLCSC_TIP_URL = "https://so.szlcsc.com/search/searchTip?keyword={c_code}"
SZLCSC_ITEM_URL = "https://item.szlcsc.com/{product_id}.html"
SZLCSC_PARTNER_DETAIL_URL = "https://item.szlcsc.com/details/{vendor_code}/{provider_code}.html"
SZLCSC_PARTNER_QUERY_URL = "https://overseas.szlcsc.com/overseas/global/search"
DEFAULT_USD_TO_CNY_RATE = 6.1053
PACKAGE_TOKENS = {"0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512"}
_STOCK_CACHE_LOCKS: dict[Path, threading.Lock] = {}
_STOCK_CACHE_LOCKS_LOCK = threading.Lock()
_DOMESTIC_BROWSER_LOCK = threading.Lock()
_SEARCH_SOURCE_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lcsc-source")


class _FetchCache:
    """Short-lived LRU with shared, bounded in-flight source requests."""

    def __init__(self, max_entries: int = 32, ttl: float = 60, max_inflight: int = 4) -> None:
        self._max_entries = max_entries
        self._ttl = ttl
        self._max_inflight = max_inflight
        self._entries: OrderedDict[tuple[str, int], tuple[float, Any]] = OrderedDict()
        self._inflight: dict[tuple[str, int], Future] = {}
        self._condition = threading.Condition()

    def get(self, key: tuple[str, int], loader: Callable[[], Any], force: bool = False) -> Any:
        with self._condition:
            while True:
                cached = self._entries.get(key)
                if cached is not None and not force:
                    expires, value = cached
                    if time.monotonic() < expires:
                        self._entries.move_to_end(key)
                        return deepcopy(value)
                    del self._entries[key]
                pending = self._inflight.get(key)
                if pending is not None:
                    owner = False
                    break
                if len(self._inflight) < self._max_inflight:
                    pending = Future()
                    self._inflight[key] = pending
                    owner = True
                    break
                self._condition.wait()

        if not owner:
            return deepcopy(pending.result())
        try:
            value = deepcopy(loader())
            with self._condition:
                # Do not retain empty/error results: a later search should retry.
                if value:
                    self._entries[key] = (time.monotonic() + self._ttl, value)
                    while len(self._entries) > self._max_entries:
                        self._entries.popitem(last=False)
                else:
                    self._entries.pop(key, None)
            pending.set_result(value)
            return deepcopy(value)
        except BaseException as exc:
            pending.set_exception(exc)
            raise
        finally:
            with self._condition:
                del self._inflight[key]
                self._condition.notify_all()


_DOMESTIC_PAGE_CACHE = _FetchCache()
_PARTNER_PRODUCT_CACHE = _FetchCache()


OVERSEAS_PROVIDER_LABELS = {
    "digikey": "Digikey",
    "mouser": "Mouser",
    "tti": "TTi-US",
    "element": "E",
}


def stock_cache_lock(path: Path) -> threading.Lock:
    resolved = Path(path).resolve()
    with _STOCK_CACHE_LOCKS_LOCK:
        if resolved not in _STOCK_CACHE_LOCKS:
            _STOCK_CACHE_LOCKS[resolved] = threading.Lock()
        return _STOCK_CACHE_LOCKS[resolved]


def normalize_c_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text or text == "NC":
        return ""
    compact = re.sub(r"\bC\s+(\d+)\b", r"C\1", text)
    match = re.search(r"\bC\d+\b", compact)
    return match.group(0) if match else ""


def text_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def clean_space(value: object) -> str:
    return re.sub(r"\s+", " ", text_value(value).strip())


def strip_search_highlights(value: object) -> str:
    """Remove LCSC search markup before storing electrical part data."""
    return unescape(re.sub(r"</?span\b[^>]*>", "", text_value(value), flags=re.I))


def exact_c_code_keyword(value: object) -> str:
    text = re.sub(r"\s+", "", text_value(value).upper())
    return text if re.fullmatch(r"C\d+", text) else ""


def parse_szlcsc_next_data(page_html: str) -> list[dict[str, Any]]:
    """Convert domestic LCSC Next.js records to the existing WMSC product schema."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        page_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    try:
        data = json.loads(unescape(match.group(1)))
        records = data["props"]["pageProps"]["soData"]["searchResult"]["productRecordList"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []

    products: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        product = record.get("productVO") or record
        if not isinstance(product, dict):
            continue
        params = record.get("paramLinkedMap") or {}
        price_list = []
        for price in product.get("productPriceList") or []:
            if not isinstance(price, dict):
                continue
            price_list.append(
                {
                    "ladder": price.get("startPurchasedNumber") or price.get("spNumber") or price.get("ladder"),
                    "productPrice": price.get("productPrice") or price.get("thePrice"),
                }
            )
        total_stock = record.get("totalStockNumber")
        if total_stock is None:
            total_stock = product.get("stockNumber")
        gd_stock = record.get("gdWarehouseStockNumber")
        if gd_stock is None:
            gd_stock = product.get("gdWarehouseStockNumber")
        js_stock = record.get("jsWarehouseStockNumber")
        if js_stock is None:
            js_stock = product.get("jsWarehouseStockNumber")
        products.append(
            {
                "productId": product.get("productId"),
                "productCode": product.get("productCode"),
                "productModel": product.get("productModel"),
                "brandNameEn": product.get("productGradePlateName") or record.get("lightBrandName"),
                "encapStandard": product.get("encapsulationModel") or record.get("lightStandard"),
                "stockNumber": total_stock,
                "stockSz": gd_stock,
                "stockJs": js_stock,
                "wmStockHk": product.get("hkStockNumber"),
                "minBuyNumber": product.get("minBuyNumber"),
                "minPacketNumber": product.get("productMinEncapsulationNumber"),
                "cnyProductPriceList": price_list,
                "paramVOList": [
                    {"paramName": name, "paramValue": value}
                    for name, value in params.items()
                    if clean_space(name) and clean_space(value)
                ],
                "catalogName": record.get("lightCatalogName") or product.get("productType"),
                "productImageUrl": product.get("breviaryImageUrl"),
                "productImageUrlBig": product.get("bigImageUrl"),
                "productArrange": product.get("productMinEncapsulationUnit"),
                "deliveryTimeWayDays": product.get("deliveryTimeWayDays") or record.get("deliveryTimeWayDays"),
                "hasStockNow": product.get("hasStockNow") if product.get("hasStockNow") is not None else record.get("hasStockNow"),
                "validStockNumber": product.get("validStockNumber"),
                "lcBookProductVO": product.get("lcBookProductVO"),
                "productHkDollerPriceList": product.get("productHkDollerPriceList"),
                "domesticStockVO": record.get("domesticStockVO") or product.get("domesticStockVO") or {},
                "source": "so.szlcsc.com/domestic",
            }
        )
    return products


class _LCSCOverseasPageParser(HTMLParser):
    """Extract the rendered overseas quote cards from the LCSC search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.card_depth = 0
        self.card: dict[str, Any] | None = None
        self.active_text: tuple[str, list[str]] | None = None
        self.current_dl: list[tuple[str, str]] | None = None
        self.void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if self.card is None:
            custom = attributes.get("data-custom-data") or ""
            if tag == "section" and '"productType":"oversea"' in custom:
                try:
                    metadata = json.loads(unescape(custom))
                except json.JSONDecodeError:
                    metadata = {}
                self.card = {
                    "provider": clean_space(metadata.get("provider")),
                    "provider_model": clean_space(metadata.get("productCodeManufacturer")),
                    "source_part_id": clean_space(metadata.get("sourcePartId")),
                    "text": [],
                    "links": [],
                    "dls": [],
                }
                self.card_depth = 1
                return
        if self.card is None:
            return

        if tag not in self.void_tags:
            self.card_depth += 1
        if tag == "a":
            href = clean_space(attributes.get("href"))
            if href and "item.szlcsc.com/details/" in href:
                self.card["links"].append(href)
        if tag == "dl":
            self.current_dl = []
        if tag in {"dt", "dd"}:
            self.active_text = (tag, [])

    def handle_endtag(self, tag: str) -> None:
        if self.card is None:
            return
        if self.active_text and tag == self.active_text[0]:
            kind, parts = self.active_text
            value = clean_space(" ".join(parts))
            if self.current_dl is not None and value:
                self.current_dl.append((kind, value))
            self.active_text = None
        if tag == "dl" and self.current_dl is not None:
            self.card["dls"].append(self.current_dl)
            self.current_dl = None
        if tag == "section" and self.card_depth == 1:
            self.card["text"] = [clean_space(value) for value in self.card["text"] if clean_space(value)]
            self.cards.append(self.card)
            self.card = None
            self.card_depth = 0
            return
        if tag not in self.void_tags:
            self.card_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.card is None:
            return
        value = clean_space(data)
        if value:
            self.card["text"].append(value)
            if self.active_text:
                self.active_text[1].append(value)

    @property
    def cards(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_cards"):
            self._cards: list[dict[str, Any]] = []
        return self._cards


def _overseas_card_field(card: dict[str, Any], label: str) -> str:
    for dl in card.get("dls") or []:
        values = {kind: value for kind, value in dl if kind in {"dt", "dd"}}
        if values.get("dt") == label:
            return values.get("dd", "")
    return ""


def _overseas_card_text(card: dict[str, Any]) -> str:
    return " ".join(card.get("text") or [])


def _overseas_price_ladder(card: dict[str, Any]) -> str:
    text = _overseas_card_text(card)
    matches = re.findall(r"(\d+)\s*\+\s*[￥¥]\s*([0-9]+(?:\.[0-9]+)?)", text)
    return "; ".join(f"{quantity}+: ¥{price}" for quantity, price in matches[:6])


def _overseas_first_price_quantity(card: dict[str, Any]) -> str:
    text = _overseas_card_text(card)
    match = re.search(r"(\d+)\s*\+\s*[￥¥]", text)
    return match.group(1) if match else ""


def _overseas_delivery(card: dict[str, Any]) -> str:
    text = _overseas_card_text(card)
    match = re.search(r"\d+\s*-\s*\d+\s*个工作日|\d+\s*个工作日", text)
    return re.sub(r"\s+", "", match.group(0)) if match else ""


def parse_szlcsc_overseas_channels(page_html: str, c_code: str = "") -> list[dict[str, str]]:
    """Parse Digikey/Mouser/TTi-US/E cards from rendered LCSC HTML."""
    parser = _LCSCOverseasPageParser()
    parser.feed(page_html or "")
    result: list[dict[str, str]] = []
    for card in parser.cards:
        provider_key = clean_space(card.get("provider")).lower()
        supplier = OVERSEAS_PROVIDER_LABELS.get(provider_key, clean_space(card.get("provider")))
        if not supplier:
            continue
        dls = card.get("dls") or []
        model = clean_space(card.get("provider_model"))
        if not model:
            for dl in dls:
                model = next((value for kind, value in dl if kind == "dd"), "")
                if model:
                    break
        channel = {
            "name": "海外订货",
            "channel_type": "overseas",
            "supplier": supplier,
            "supplier_sku": _overseas_card_field(card, "供应商编号"),
            "vendor_code": _overseas_card_field(card, "供应商编号"),
            "provider_model": model,
            "model": model,
            "c_code": "",
            "related_lcsc_code": exact_c_code_keyword(c_code),
            "brand": _overseas_card_field(card, "品牌"),
            "package": _overseas_card_field(card, "封装"),
            "param": _overseas_card_field(card, "参数"),
            "description": _overseas_card_field(card, "描述"),
            "packaging": _overseas_card_field(card, "包装"),
            "stock": _overseas_card_field(card, "库存"),
            "lead_time": _overseas_delivery(card),
            "moq": _overseas_first_price_quantity(card),
            "mpq": _overseas_card_field(card, "最小包装"),
            "increment": _overseas_card_field(card, "增量"),
            "price": _overseas_price_ladder(card),
            "batch_number": "",
            "url": (card.get("links") or [""])[0],
        }
        if channel["url"] or channel["supplier_sku"] or channel["provider_model"]:
            result.append(channel)
    return result


def parse_szlcsc_partner_data(page_html: str) -> list[dict[str, Any]]:
    """Convert LCSC partner inventory records to the existing product schema."""
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        page_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []
    try:
        data = json.loads(unescape(match.group(1)))
        records = data["props"]["pageProps"]["partnerResultRaw"]["lcbookLandingPageProductVOS"]
    except (KeyError, TypeError, json.JSONDecodeError):
        return []

    products: list[dict[str, Any]] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        price_list = [
            {"ladder": price.get("countStart"), "productPrice": price.get("rmbPrice") or price.get("price")}
            for price in record.get("tieredPrices") or []
            if isinstance(price, dict)
        ]
        params = []
        for param in record.get("selfParams") or []:
            if not isinstance(param, dict):
                continue
            name = clean_space(param.get("parameterName"))
            value = clean_space(param.get("parameterDetailValue"))
            if name and value:
                params.append({"paramName": name, "paramValue": value})
        products.append(
            {
                "productCode": record.get("productCode"),
                "productModel": record.get("productCodeManufacturer"),
                "brandNameEn": record.get("brandName"),
                "encapStandard": record.get("selfStandard"),
                "stockNumber": record.get("stockNumber"),
                "minBuyNumber": record.get("minBuyNumber"),
                "minPacketNumber": record.get("productMinEncapsulationNumber"),
                "cnyProductPriceList": price_list,
                "tieredPrices": record.get("tieredPrices"),
                "deliveryTimeWayDays": record.get("deliveryTimeWayDays"),
                "paramVOList": params,
                "productKeyAttributes": record.get("selfProductIntro"),
                "catalogName": record.get("lcCatalogName"),
                "productImageUrl": record.get("breviaryImageUrl"),
                "productImageUrlBig": record.get("bigImageUrl"),
                "productArrange": record.get("unit"),
                "provider": record.get("provider"),
                "vendorCode": record.get("vendorCode"),
                "vendorCodeEncode": record.get("vendorCodeEncode"),
                "productCodeProvider": record.get("productCodeProvider"),
                "productCodeManufacturer": record.get("productCodeManufacturer"),
                "batchNumber": record.get("batchNumber"),
                "source": "szlcsc.com/partner",
            }
        )
    return products


def parse_szlcsc_partner_response(data: object) -> list[dict[str, Any]]:
    """Flatten every orderable quote returned by the partner inventory API."""
    if not isinstance(data, dict) or data.get("code") != 200:
        return []
    groups = data.get("result") or []
    if not isinstance(groups, list):
        return []

    products: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        provider = clean_space(group.get("provider"))
        for raw_product in group.get("products") or []:
            if not isinstance(raw_product, dict):
                continue
            product = dict(raw_product)
            if provider and not clean_space(product.get("provider")):
                product["provider"] = provider
            orderable = any(
                clean_space(product.get(key))
                for key in ("provider", "vendorCode", "vendorCodeEncode", "productCodeProvider")
            )
            if group.get("isLcscBookShow") or orderable:
                product["source"] = "szlcsc.com/partner-api"
                products.append(product)
    return products


def find_chrome_executable() -> str:
    candidates = [
        shutil.which("chrome"),
        shutil.which("msedge"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    return next((path for path in candidates if path and Path(path).is_file()), "")


def fetch_szlcsc_page(c_code: str, timeout: int = 10, force_refresh: bool = False) -> str:
    keyword = clean_space(c_code)
    if not keyword:
        return ""
    return _DOMESTIC_PAGE_CACHE.get(
        (keyword.casefold(), timeout), lambda: _fetch_szlcsc_page(keyword, timeout), force=force_refresh
    )


def _fetch_szlcsc_page(keyword: str, timeout: int) -> str:
    with _DOMESTIC_BROWSER_LOCK:
        chrome = find_chrome_executable()
        if not chrome:
            return ""
        command = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--virtual-time-budget=8000",
            "--dump-dom",
            SZLCSC_SEARCH_URL.format(c_code=urllib.parse.quote(keyword)),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=max(3, timeout),
                creationflags=creationflags,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return completed.stdout.decode("utf-8", errors="replace")


def fetch_szlcsc_domestic_products(c_code: str, timeout: int = 10, force_refresh: bool = False) -> list[dict[str, Any]]:
    keyword = clean_space(c_code)
    if not keyword:
        return []
    page_html = fetch_szlcsc_page(keyword, timeout, force_refresh=force_refresh)
    if not page_html:
        return []
    exact_code = exact_c_code_keyword(keyword)
    return [
        product
        for product in parse_szlcsc_next_data(page_html)
        if not exact_code or normalize_c_code(product.get("productCode")) == exact_code
    ]


def fetch_szlcsc_overseas_channels(c_code: str, timeout: int = 10) -> list[dict[str, str]]:
    code = exact_c_code_keyword(c_code)
    if not code:
        return []
    page_html = fetch_szlcsc_page(code, timeout)
    return parse_szlcsc_overseas_channels(page_html, code) if page_html else []


def fetch_szlcsc_partner_products(keyword: str, timeout: int = 10, force_refresh: bool = False,
                                 strict_errors: bool = False) -> list[dict[str, Any]]:
    keyword = clean_space(keyword)
    if not keyword:
        return []
    try:
        return _PARTNER_PRODUCT_CACHE.get(
            (keyword.casefold(), timeout), lambda: _fetch_szlcsc_partner_products(keyword, timeout), force=force_refresh
        )
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        if strict_errors:
            raise
        return []


def _fetch_szlcsc_partner_products(keyword: str, timeout: int) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
        {
            "keyword": keyword,
            "pageNumber": 1,
            "noShowSelf": "false",
            "from": "",
            "brandLevel": "",
        }
    )
    request = urllib.request.Request(
        f"{SZLCSC_PARTNER_QUERY_URL}?{query}",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "Origin": "https://so.szlcsc.com",
            "Referer": "https://so.szlcsc.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(data, dict) or data.get("code") != 200 or not isinstance(data.get("result", []), list):
        raise ValueError("Partner inventory returned an invalid or unsuccessful response")
    return parse_szlcsc_partner_response(data)


def product_parameters(product: dict[str, Any]) -> list[dict[str, str]]:
    """Keep every vendor parameter as a pair, without splitting values on punctuation."""
    items = []
    seen = set()
    aliases = {
        "temperature coefficient": "温度系数", "temperature coefficient of resistance": "温度系数",
        "tcr": "温度系数", "电阻温度系数": "温度系数", "温度系数": "温度系数",
        "working voltage": "工作电压", "operating voltage": "工作电压", "工作电压": "工作电压",
    }
    for key in ("paramVOList", "paramPOList", "selfParams"):
        for item in product.get(key) or []:
            if not isinstance(item, dict):
                continue
            name = strip_search_highlights(item.get("paramName") or item.get("paramNameEn")
                                           or item.get("name") or item.get("parameterName")).strip()
            raw_value = next((item[key] for key in ("paramValue", "paramValueEn", "value", "parameterDetailValue")
                              if item.get(key) is not None), "")
            value = strip_search_highlights(raw_value).strip()
            if not name:
                continue
            label = re.sub(r"\s*[(（][^()（）]*[)）]\s*$", "", name)
            canonical = aliases.get(clean_space(label).casefold())
            if canonical:
                name = canonical + name[len(label):]
            pair = (name, value)
            if pair not in seen:
                seen.add(pair)
                items.append({"name": name, "value": value})
    return items


def product_param_items(product: dict[str, Any]) -> list[str]:
    return [f"{item['name']}: {item['value']}" for item in product_parameters(product) if item["value"]]


def product_param_summary(product: dict[str, Any]) -> str:
    for key in ("productKeyAttributes", "productDescEn", "productIntroEn", "productNameEn"):
        value = clean_space(strip_search_highlights(product.get(key)))
        if value:
            return value
    items = product_param_items(product)
    return "; ".join(items[:8])


def product_param_detail(product: dict[str, Any]) -> str:
    return "; ".join(product_param_items(product))


def preserve_candidate_parameters(item: dict[str, Any], cached: dict[str, Any] | None) -> dict[str, Any]:
    """A new search summary is not evidence that cached electrical ratings disappeared."""
    if not cached or normalize_c_code(item.get("c_code")) != normalize_c_code(cached.get("c_code")):
        return item
    if not normalize_model(item.get("model")) or normalize_model(item.get("model")) != normalize_model(cached.get("model")):
        return item
    if item.get("detail_parameter_status") == "ready":
        return item

    def pairs(source):
        if isinstance(source.get("parameters"), list) and source["parameters"]:
            return [dict(pair) for pair in source["parameters"] if isinstance(pair, dict) and pair.get("name")]
        result = []
        for segment in re.split(r"[;；\n]\s*(?=[^:：;；\n]{1,64}[:：])", str(source.get("param_detail") or "")):
            match = re.fullmatch(r"\s*([^:：]{1,64})[:：]\s*(.*)", segment, re.S)
            if match:
                result.append({"name": match[1].strip(), "value": match[2].strip()})
        return result

    merged = OrderedDict()
    def known(value):
        value = text_value(value).strip()
        return bool(value) and not re.fullmatch(
            r"[-—–]+|未提供|待核实|待确认|未知|暂无|无|n/?a|unknown|not specified|null|undefined", value, re.I)

    for source in (cached, item):
        for pair in pairs(source):
            key = re.sub(r"\s*[(（][^()（）]*[)）]\s*$", "", str(pair["name"])).casefold().strip()
            key = {"最大工作电压": "工作电压", "maximum working voltage": "工作电压",
                   "working voltage": "工作电压", "temperature coefficient": "温度系数"}.get(key, key)
            if key not in merged or known(pair.get("value")) or not known(merged[key].get("value")):
                merged[key] = pair
    if not merged:
        return item
    result = {**item, "parameters": list(merged.values())}
    result["param_detail"] = "; ".join(f"{pair['name']}: {pair.get('value', '')}" for pair in merged.values())
    result["parameter_provenance"] = "cached-and-search"
    return result


def int_value(value: Any) -> int:
    try:
        return int(float(text_value(value).replace(",", "")))
    except ValueError:
        return 0


def format_ladder_price(price_list: list[Any], symbol: str) -> str:
    if not price_list:
        return ""
    first_price = price_list[0] or {}
    amount = text_value(
        first_price.get("productPrice")
        or first_price.get("price")
        or first_price.get("currencyPrice")
        or first_price.get("cnyProductPrice")
    )
    ladder = text_value(first_price.get("ladder"))
    return f"{symbol}{amount}@{ladder}" if amount and ladder else f"{symbol}{amount}" if amount else ""


def format_usd_ladder_as_rmb(price_list: list[Any], rate: float = DEFAULT_USD_TO_CNY_RATE) -> str:
    if not price_list:
        return ""
    first_price = price_list[0] or {}
    symbol = text_value(first_price.get("currencySymbol"))
    if symbol != "$":
        return ""
    amount_text = text_value(first_price.get("productPrice") or first_price.get("usdPrice") or first_price.get("currencyPrice"))
    ladder = text_value(first_price.get("ladder"))
    try:
        amount = float(amount_text)
    except ValueError:
        return ""
    rmb = f"{amount * rate:.4f}".rstrip("0").rstrip(".")
    return f"¥{rmb}@{ladder}" if ladder else f"¥{rmb}"


def format_price_ladder(product: dict[str, Any], limit: int = 2) -> str:
    price_list = product.get("cnyProductPriceList") or product.get("cnyLadderPrice") or product.get("productPriceList") or []
    result = []
    for item in price_list[:limit]:
        if not isinstance(item, dict):
            continue
        ladder = text_value(item.get("ladder"))
        if item.get("currencySymbol") == "$":
            price = format_usd_ladder_as_rmb([item])
        else:
            price = format_ladder_price([item], "¥")
        if price:
            if "@" in price:
                amount, qty = price.split("@", 1)
                result.append(f"{qty}+: {amount}")
            elif ladder:
                result.append(f"{ladder}+: {price}")
            else:
                result.append(price)
    return "; ".join(result)


def encode_lcbook_vendor_code(value: object) -> str:
    text = clean_space(value)
    if not text:
        return ""
    return base64.b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def double_quote_path_segment(value: object) -> str:
    return urllib.parse.quote(urllib.parse.quote(clean_space(value), safe=""), safe="")


def lcbook_order_url(item: dict[str, Any]) -> str:
    provider_code = (
        clean_space(item.get("productCodeProvider"))
        or clean_space(item.get("productCodeManufacturer"))
        or clean_space(item.get("productModel"))
    )
    if not provider_code:
        return ""
    vendor_code = clean_space(item.get("vendorCodeEncode"))
    if not vendor_code:
        vendor_code = encode_lcbook_vendor_code(item.get("vendorCode"))
    if not vendor_code:
        vendor_code = clean_space(item.get("provider"))
    if not vendor_code:
        return ""
    return SZLCSC_PARTNER_DETAIL_URL.format(
        vendor_code=urllib.parse.quote(vendor_code, safe=""),
        provider_code=double_quote_path_segment(provider_code),
    )


def format_partner_price_ladder(item: dict[str, Any], limit: int = 3) -> str:
    prices = item.get("tieredPrices") or []
    if not isinstance(prices, list):
        prices = []
    result = []
    for price in prices[:limit]:
        if not isinstance(price, dict):
            continue
        ladder = text_value(price.get("countStart"))
        amount = text_value(price.get("rmbPrice") or price.get("price"))
        if ladder and amount:
            result.append(f"{ladder}+: ¥{amount}")
        elif amount:
            result.append(f"¥{amount}")
    return "; ".join(result)


def order_channel_from_lcbook(item: dict[str, Any], product: dict[str, Any] | None = None) -> dict[str, Any]:
    product = product or {}
    supplier = clean_space(item.get("provider"))
    model = clean_space(item.get("productCodeManufacturer") or product.get("productModel"))
    stock_source = item.get("stockNumber") if item.get("stockNumber") is not None else product.get("stockNumber")
    return {
        "name": "合作库存" if supplier else "订货渠道",
        "supplier": supplier,
        "vendor_code": clean_space(item.get("vendorCode")),
        "provider_model": clean_space(item.get("productCodeProvider")),
        "model": model,
        "c_code": normalize_c_code(item.get("productCode") or product.get("productCode")),
        "brand": clean_space(item.get("brandName") or item.get("productGradePlateName") or product.get("brandNameEn")),
        "package": clean_space(item.get("selfStandard") or item.get("encapsulationModel") or product.get("encapStandard")),
        "param": clean_space(item.get("selfProductIntro") or item.get("productDesc") or item.get("productKeyAttributes")),
        "param_detail": product_param_detail(item),
        "parameters": product_parameters(item),
        "category": text_value(item.get("catalogName") or item.get("lcCatalogName") or item.get("wmCatalogNameEn")),
        "batch_number": clean_space(item.get("batchNumber")),
        "stock": clean_space(stock_source),
        "lead_time": clean_space(item.get("deliveryTimeWayDays") or product.get("deliveryTimeWayDays")),
        "moq": clean_space(item.get("minBuyNumber") if item.get("minBuyNumber") is not None else product.get("minBuyNumber")),
        "mpq": clean_space(
            item.get("productMinEncapsulationNumber")
            if item.get("productMinEncapsulationNumber") is not None
            else product.get("minPacketNumber")
        ),
        "increment": clean_space(
            item.get("incrementalQuantity")
            if item.get("incrementalQuantity") is not None
            else item.get("theRatio")
        ),
        "price": format_partner_price_ladder(item) or format_partner_price_ladder(product) or format_price_ladder(product),
        "url": lcbook_order_url(item),
    }


def product_order_channels(product: dict[str, Any]) -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    lcbook = product.get("lcBookProductVO")
    if isinstance(lcbook, dict):
        channel = order_channel_from_lcbook(lcbook, product)
        if channel.get("url") or channel.get("supplier") or channel.get("provider_model"):
            channels.append(channel)
    if product.get("provider") or product.get("productCodeProvider") or product.get("vendorCodeEncode"):
        channel = order_channel_from_lcbook(product, product)
        if channel.get("url") or channel.get("supplier") or channel.get("provider_model"):
            channels.append(channel)

    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for channel in channels:
        key = channel.get("url") or "|".join(
            channel.get(part, "") for part in ("supplier", "vendor_code", "provider_model")
        )
        if key and key not in seen:
            seen.add(key)
            deduped.append(channel)
    return deduped[:8]


def product_dispatch_stock(product: dict[str, Any]) -> tuple[str, str]:
    domestic = product.get("domesticStockVO")
    domestic = domestic if isinstance(domestic, dict) else {}
    counts = [text_value(domestic.get(key)).replace(",", "") for key in ("shipImmediately", "ship3Days")]
    counts = [value if re.fullmatch(r"\d+", value) else "" for value in counts]
    if text_value(product.get("hasStockNow")).lower() in {"no", "false", "0"}:
        counts[0] = "0"
    return counts[0], counts[1]


def product_delivery(product: dict[str, Any]) -> str:
    # Neither total quantity nor warehouse location establishes dispatch readiness.
    lead_time = clean_space(product.get("deliveryTimeWayDays"))
    immediate, three_days = product_dispatch_stock(product)
    parts = [lead_time] if lead_time else []
    if int_value(immediate) > 0:
        parts.append(f"可立即发货 {immediate}")
    if int_value(three_days) > 0:
        parts.append(f"预计3天发货 {three_days}")
    if parts:
        return "；".join(parts)
    if product.get("isForeignOnsale"):
        return "订货/海外代购，交期以立创页面为准"
    return "发货时间待核实"


def product_channel(product: dict[str, Any]) -> str:
    if product_order_channels(product):
        return "合作库存/订货"
    if int_value(product.get("stockNumber")) > 0:
        return "商城库存"
    if product.get("isForeignOnsale"):
        return "海外代购/订货"
    return "暂无库存" if text_value(product.get("stockNumber")) else "库存待核实"


def normalize_model(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", text_value(value).upper())


def extract_model_tokens(keyword: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9#._+-]*", keyword or ""):
        compact = normalize_model(token)
        if len(compact) < 6 or compact in PACKAGE_TOKENS or re.fullmatch(r"\d+[RKMUNPFV]*", compact):
            continue
        if not (re.search(r"[A-Z]", compact) and re.search(r"\d", compact)):
            continue
        tokens.append(token.strip())
    return tokens


def candidate_match(keyword: str, product: dict[str, Any]) -> tuple[str, int]:
    exact_code = exact_c_code_keyword(keyword)
    if exact_code and normalize_c_code(product.get("productCode")) == exact_code:
        return ("C编码精确", 0)
    model = normalize_model(product.get("productModel"))
    for token in extract_model_tokens(keyword):
        token_norm = normalize_model(token)
        if model == token_norm:
            return ("型号精确", 0)
        if token_norm and (token_norm in model or model in token_norm):
            return ("型号相关", 1)
        if len(token_norm) >= 10 and difflib.SequenceMatcher(None, token_norm, model).ratio() >= 0.88:
            return ("型号近似", 1)
    return ("参数替代", 2)


def parse_szlcsc_item_price(page_html: str) -> str:
    text = unescape(page_html or "")
    text = re.sub(r"\s+", " ", text)
    match = re.search(r"(\d+)\s*\+\s*[：:]\s*[￥¥]\s*([0-9]+(?:\.[0-9]+)?)", text)
    if match:
        return f"¥{match.group(2)}@{match.group(1)}"
    match = re.search(r"价格\s*[￥¥]\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    return f"¥{match.group(1)}" if match else ""


def fetch_szlcsc_item_price(product: dict[str, Any], c_code: str, timeout: int = 4) -> str:
    product_id = text_value(product.get("productId"))
    if not product_id:
        return ""
    request = urllib.request.Request(
        SZLCSC_ITEM_URL.format(product_id=product_id),
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1",
            "Referer": SZLCSC_SEARCH_URL.format(c_code=c_code),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            page_html = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""
    return parse_szlcsc_item_price(page_html)


def parse_lcsc_product(c_code: str, product: dict[str, Any]) -> dict[str, Any]:
    cny_price_list = product.get("cnyProductPriceList") or product.get("cnyLadderPrice") or []
    price = format_ladder_price(cny_price_list, "¥")
    if not price:
        price = text_value(product.get("cnyProductPrice") or product.get("productConsultPrice"))
        if price:
            price = f"¥{price}"
    if not price:
        price = format_usd_ladder_as_rmb(product.get("productPriceList") or [])
    if not price:
        price = "无人民币价"

    return {
        "ok": True,
        "status": "OK",
        "c_code": c_code,
        "product_id": text_value(product.get("productId")) if re.fullmatch(r"\d+", text_value(product.get("productId"))) else "",
        "stock": text_value(product.get("stockNumber")),
        "stock_sz": text_value(product.get("stockSz")),
        "stock_js": text_value(product.get("stockJs")),
        "stock_hk": text_value(product.get("wmStockHk")),
        "immediate_stock": product_dispatch_stock(product)[0],
        "three_day_stock": product_dispatch_stock(product)[1],
        "delivery_schema": 1,
        "moq": text_value(product.get("minBuyNumber")),
        "mpq": text_value(product.get("minPacketNumber")),
        "price": price,
        "model": text_value(product.get("productModel")),
        "brand": text_value(product.get("brandNameEn")),
        "package": text_value(product.get("encapStandard")),
        "param": product_param_summary(product),
        "param_detail": product_param_detail(product),
        "parameters": product_parameters(product),
        "price_ladder": format_price_ladder(product),
        "delivery": product_delivery(product),
        "channel": product_channel(product),
        "arrange": text_value(product.get("productArrange")),
        "category": text_value(product.get("catalogName") or product.get("wmCatalogNameEn")),
        "image_url": text_value(product.get("productImageUrl")),
        "image_url_big": text_value(product.get("productImageUrlBig")),
        "url": SZLCSC_SEARCH_URL.format(c_code=c_code),
        "order_channels": product_order_channels(product),
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "so.szlcsc.com+wmsc",
        "error": "",
    }


def stock_status(c_code: str, status: str, error: str = "") -> dict[str, Any]:
    if status in {"NC", "NeedCCode"} and not c_code:
        status = "请提供料号"
        error = error or "请提供料号"
    return {
        "ok": False,
        "status": status,
        "c_code": c_code,
        "stock": "",
        "stock_sz": "",
        "stock_js": "",
        "stock_hk": "",
        "moq": "",
        "mpq": "",
        "price": "",
        "model": "",
        "brand": "",
        "package": "",
        "param": "",
        "param_detail": "",
        "price_ladder": "",
        "delivery": "",
        "channel": "",
        "arrange": "",
        "category": "",
        "image_url": "",
        "image_url_big": "",
        "url": "",
        "order_channels": [],
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source": "so.szlcsc.com+wmsc",
        "error": error,
    }


def probe_szlcsc_search(c_code: str, timeout: int = 4) -> str:
    request = urllib.request.Request(
        SZLCSC_TIP_URL.format(c_code=c_code),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Referer": SZLCSC_SEARCH_URL.format(c_code=c_code),
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return f"so.szlcsc probe failed: {exc}"
    if data.get("code") != 200:
        return f"so.szlcsc probe failed: {data.get('msg')}"
    return ""


def query_lcsc_stock(
    c_code: str,
    timeout: int = 8,
    domestic_fetcher: Callable[[str, int], list[dict[str, Any]]] = fetch_szlcsc_domestic_products,
) -> dict[str, str | bool]:
    c_code = normalize_c_code(c_code)
    if not c_code:
        return stock_status("", "NeedCCode")

    # The domestic search page is the authoritative fallback for exact codes;
    # the old tip probe adds another blocked request and only delays a lookup.
    probe_error = ""
    payload = {
        "keyword": c_code,
        "globalKeyword": c_code,
        "catalogIdList": [],
        "brandIdList": [],
        "encapValueList": [],
        "isStock": False,
        "isOtherSuppliers": False,
        "isAsianBrand": False,
        "isDeals": False,
        "isEnvironment": False,
        "paramNameValueMap": {},
        "currentPage": 1,
        "pageSize": 25,
    }
    request = urllib.request.Request(
        LCSC_QUERY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.lcsc.com",
            "Referer": f"https://www.lcsc.com/products?keywords={c_code}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        for product in domestic_fetcher(c_code, timeout):
            if normalize_c_code(product.get("productCode")) == c_code:
                result = parse_lcsc_product(c_code, product)
                result["source"] = "so.szlcsc.com/domestic"
                result["error"] = ""
                return result
        return stock_status(c_code, "QueryFailed", str(exc))

    if data.get("code") != 200:
        for product in domestic_fetcher(c_code, timeout):
            if normalize_c_code(product.get("productCode")) == c_code:
                result = parse_lcsc_product(c_code, product)
                result["source"] = "so.szlcsc.com/domestic"
                result["error"] = ""
                return result
        return stock_status(c_code, "QueryFailed", text_value(data.get("msg")))

    products = ((data.get("result") or {}).get("dataList")) or []
    for product in products:
        if normalize_c_code(product.get("productCode")) == c_code:
            result = parse_lcsc_product(c_code, product)
            if result.get("price") == "无人民币价":
                rmb_price = fetch_szlcsc_item_price(product, c_code)
                if rmb_price:
                    result["price"] = rmb_price
                    result["source"] = f"{result['source']}+item_price"
            result["error"] = probe_error
            return result
    for product in domestic_fetcher(c_code, timeout):
        if normalize_c_code(product.get("productCode")) != c_code:
            continue
        result = parse_lcsc_product(c_code, product)
        result["source"] = "so.szlcsc.com/domestic"
        result["error"] = ""
        return result
    return stock_status(c_code, "NotFound")


def build_candidate_item(keyword: str, product: dict[str, Any]) -> dict[str, Any]:
    code = normalize_c_code(product.get("productCode"))
    stock = parse_lcsc_product(code, product)
    match_type, match_rank = candidate_match(keyword, product)
    return {
        "c_code": code,
        "model": stock["model"],
        "brand": stock["brand"],
        "package": stock["package"],
        "product_id": stock.get("product_id", ""),
        "param": stock["param"],
        "param_detail": stock["param_detail"],
        "parameters": stock["parameters"],
        "stock": stock["stock"],
        "stock_sz": stock["stock_sz"],
        "stock_js": stock["stock_js"],
        "stock_hk": stock["stock_hk"],
        "immediate_stock": stock["immediate_stock"],
        "three_day_stock": stock["three_day_stock"],
        "delivery_schema": stock["delivery_schema"],
        "moq": stock["moq"],
        "mpq": stock["mpq"],
        "price": stock["price"],
        "price_ladder": stock["price_ladder"],
        "delivery": stock["delivery"],
        "channel": stock["channel"],
        "arrange": stock["arrange"],
        "category": stock["category"],
        "image_url": stock["image_url"],
        "image_url_big": stock["image_url_big"],
        "url": stock["url"],
        "order_channels": stock["order_channels"],
        "match_type": match_type,
        "match_rank": match_rank,
        "source": text_value(product.get("source")) or text_value(stock.get("source")),
        "fetched_at": text_value(stock.get("fetched_at")),
    }


def candidate_stock_result(item: dict[str, Any]) -> dict[str, Any]:
    """Return the cache-compatible stock record represented by a candidate row."""
    result = dict(item)
    result.update({"ok": True, "status": "OK", "error": ""})
    for key in ("match_type", "match_rank"):
        result.pop(key, None)
    return result


def stock_to_candidate_item(keyword: str, stock: dict[str, Any]) -> dict[str, Any]:
    """Turn a cached stock record into the candidate shape used by the modal."""
    item = dict(stock)
    item["match_type"] = "C编码精确" if exact_c_code_keyword(keyword) else "缓存结果"
    item["match_rank"] = 0
    return item


def domestic_exact_candidate_items(
    keyword: str,
    timeout: int,
    domestic_fetcher: Callable[[str, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    code = exact_c_code_keyword(keyword)
    if not code:
        return []
    return [
        build_candidate_item(keyword, product)
        for product in domestic_fetcher(code, timeout)
        if normalize_c_code(product.get("productCode")) == code
    ]


def enrich_candidate_order_channels(
    items: list[dict[str, Any]],
    partner_products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach partner quotes without changing the primary product fields."""
    for item in items:
        code = normalize_c_code(item.get("c_code"))
        model = normalize_model(item.get("model"))
        matched = [
            product
            for product in partner_products
            if (code and normalize_c_code(product.get("productCode")) == code)
            or (
                model
                and normalize_model(product.get("productCodeManufacturer") or product.get("productModel")) == model
            )
        ]
        channels = list(item.get("order_channels") or [])
        channels.extend(order_channel_from_lcbook(product, product) for product in matched)

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for channel in channels:
            key = channel.get("url") or "|".join(
                channel.get(part, "") for part in ("supplier", "vendor_code", "provider_model")
            )
            if key and key not in seen:
                seen.add(key)
                deduped.append(channel)
        item["order_channels"] = deduped
    return items


def enrich_candidate_overseas_channels(
    items: list[dict[str, Any]],
    channels: list[dict[str, str]],
    c_code: str,
) -> list[dict[str, Any]]:
    """Attach the official overseas cards to the exact domestic result."""
    exact = exact_c_code_keyword(c_code)
    if not exact or not channels:
        return items
    for item in items:
        if normalize_c_code(item.get("c_code")) != exact:
            continue
        current = list(item.get("order_channels") or [])
        current.extend(channels)
        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for channel in current:
            key = channel.get("url") or "|".join(
                channel.get(part, "") for part in ("supplier", "vendor_code", "provider_model")
            )
            if key and key not in seen:
                seen.add(key)
                deduped.append(channel)
        item["order_channels"] = deduped
        break
    return items


def search_lcsc_candidates(
    keyword: str,
    limit: int = 10,
    timeout: int = 8,
    include_substitutes: bool = True,
    domestic_fetcher: Callable[[str, int], list[dict[str, Any]]] = fetch_szlcsc_domestic_products,
    partner_fetcher: Callable[[str, int], list[dict[str, Any]]] = fetch_szlcsc_partner_products,
    overseas_fetcher: Callable[[str, int], list[dict[str, str]]] | None = None,
    on_partial: Callable[[dict[str, Any]], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    def check_cancelled():
        if cancelled and cancelled():
            raise CancelledError()

    check_cancelled()
    keyword = str(keyword or "").strip()
    if not keyword:
        return {"ok": False, "error": "keyword is required", "items": []}
    model_tokens = extract_model_tokens(keyword)
    exact_code = exact_c_code_keyword(keyword)
    partner_keyword = exact_c_code_keyword(keyword) or (model_tokens[0] if model_tokens else keyword)

    sources = {"domestic": "querying", "partner": "skipped" if exact_code else "querying"}
    if exact_code and overseas_fetcher:
        sources["overseas"] = "querying"

    def fetch_products(fetcher, query):
        check_cancelled()
        try:
            products = (fetcher(query, timeout, strict_errors=True)
                        if fetcher is fetch_szlcsc_partner_products else fetcher(query, timeout))
            return products, "ready" if products else "empty"
        except CancelledError:
            raise
        except Exception:
            return [], "failed"

    def candidate_items():
        items = [build_candidate_item(keyword, product) for product in domestic_products[:limit]]
        enrich_candidate_order_channels(items, partner_products)
        exact_items = [item for item in items if int_value(item.get("match_rank")) <= 1]
        return exact_items if not include_substitutes and exact_items else items

    def publish():
        check_cancelled()
        if on_partial:
            on_partial({"ok": True, "items": candidate_items(), "sources": dict(sources), "mode": "partial"})

    def overseas_channels():
        check_cancelled()
        try:
            channels = run_source(lambda: overseas_fetcher(exact_code, timeout))
            sources["overseas"] = "ready" if channels else "empty"
            return channels
        except CancelledError:
            raise
        except Exception:
            sources["overseas"] = "failed"
            return []

    def run_source(loader):
        def load():
            check_cancelled()
            return loader()
        future = _SEARCH_SOURCE_POOL.submit(load)
        try:
            while not future.done():
                check_cancelled()
                wait([future], timeout=.1)
            check_cancelled()
            return future.result()
        finally:
            future.cancel()

    domestic_products = []
    partner_products = []

    # Leave active upstream calls in a bounded shared pool when canceled; the
    # orchestration worker is then free to start the user's replacement query.
    futures = {_SEARCH_SOURCE_POOL.submit(fetch_products, domestic_fetcher, keyword): "domestic"}
    if not exact_code:
        futures[_SEARCH_SOURCE_POOL.submit(fetch_products, partner_fetcher, partner_keyword)] = "partner"
    pending = set(futures)
    try:
        while pending:
            check_cancelled()
            completed, pending = wait(pending, timeout=.1, return_when=FIRST_COMPLETED)
            for future in completed:
                source = futures[future]
                products, sources[source] = future.result()
                if source == "partner":
                    partner_products = products
                else:
                    domestic_products = products
                publish()
    finally:
        for future in pending:
            future.cancel()
    check_cancelled()
    if domestic_products:
        # Keep the structured list aligned with the visible domestic商城 search.
        items = candidate_items()
        if exact_code and overseas_fetcher:
            overseas = overseas_channels()
            enrich_candidate_overseas_channels(items, overseas, exact_code)
        enrich_candidate_order_channels(items, partner_products)
        exact_items = [item for item in items if int_value(item.get("match_rank")) <= 1]
        if not include_substitutes and exact_items:
            items = exact_items
        return {
            "ok": True,
            "error": "",
            "items": items,
            "mode": "domestic",
            "model_tokens": model_tokens,
            "sources": sources,
        }
    check_cancelled()
    sources["fallback"] = "querying"
    publish()
    payload = {
        "keyword": keyword,
        "globalKeyword": keyword,
        "catalogIdList": [],
        "brandIdList": [],
        "encapValueList": [],
        "isStock": False,
        "isOtherSuppliers": False,
        "isAsianBrand": False,
        "isDeals": False,
        "isEnvironment": False,
        "paramNameValueMap": {},
        "currentPage": 1,
        "pageSize": max(1, min(limit, 25)),
    }
    request = urllib.request.Request(
        LCSC_QUERY_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.lcsc.com",
            "Referer": f"https://www.lcsc.com/products?keywords={urllib.parse.quote(keyword)}",
        },
        method="POST",
    )
    def load_fallback():
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    try:
        data = run_source(load_fallback)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        check_cancelled()
        fallback_items = run_source(lambda: domestic_exact_candidate_items(keyword, timeout, domestic_fetcher))
        sources["fallback"] = "ready" if fallback_items else "failed"
        if fallback_items:
            enrich_candidate_order_channels(fallback_items, partner_products)
            if exact_c_code_keyword(keyword) and overseas_fetcher:
                overseas = overseas_channels()
                enrich_candidate_overseas_channels(fallback_items, overseas, keyword)
            return {"ok": True, "error": "", "items": fallback_items, "mode": "exact", "model_tokens": model_tokens, "sources": sources}
        return {"ok": False, "error": str(exc), "items": [], "sources": sources}
    check_cancelled()
    if data.get("code") != 200:
        fallback_items = run_source(lambda: domestic_exact_candidate_items(keyword, timeout, domestic_fetcher))
        sources["fallback"] = "ready" if fallback_items else "failed"
        if fallback_items:
            enrich_candidate_order_channels(fallback_items, partner_products)
            if exact_c_code_keyword(keyword) and overseas_fetcher:
                overseas = overseas_channels()
                enrich_candidate_overseas_channels(fallback_items, overseas, keyword)
            return {"ok": True, "error": "", "items": fallback_items, "mode": "exact", "model_tokens": model_tokens, "sources": sources}
        return {"ok": False, "error": text_value(data.get("msg")), "items": [], "sources": sources}
    products = ((data.get("result") or {}).get("dataList")) or []
    items = []
    for product in products[:limit]:
        code = normalize_c_code(product.get("productCode"))
        if not code:
            continue
        items.append(build_candidate_item(keyword, product))
    if exact_code:
        exact_index = next((idx for idx, item in enumerate(items) if item["c_code"] == exact_code), -1)
        if exact_index < 0:
            items = run_source(lambda: domestic_exact_candidate_items(keyword, timeout, domestic_fetcher)) + items
        elif not items[exact_index].get("order_channels"):
            domestic_items = run_source(lambda: domestic_exact_candidate_items(keyword, timeout, domestic_fetcher))
            if domestic_items:
                items[exact_index] = domestic_items[0]
        if overseas_fetcher:
            overseas = overseas_channels()
            enrich_candidate_overseas_channels(items, overseas, exact_code)
    enrich_candidate_order_channels(items, partner_products)
    items.sort(key=lambda item: (item["match_rank"], 0 if int_value(item.get("stock")) > 0 else 1))
    exact_items = [item for item in items if int_value(item["match_rank"]) <= 1]
    if not include_substitutes and exact_items:
        items = exact_items
    sources["fallback"] = "ready" if items else "empty"
    return {
        "ok": True,
        "error": "",
        "items": items,
        "mode": "exact" if (not include_substitutes and exact_items) else "all",
        "model_tokens": model_tokens,
        "sources": sources,
    }


def load_stock_cache(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        code = normalize_c_code(key)
        if not code or not isinstance(value, dict):
            continue
        item = dict(value)
        if item.get("channel") == "现货":
            item["channel"] = "商城库存"
        elif item.get("channel") == "无现货":
            item["channel"] = "暂无库存"
        # Older releases synthesized this promise from total stock, not a shipping quote.
        if item.get("delivery_schema") != 1 and clean_space(item.get("delivery")) in {"现货最快4小时发货", "4小时发货"}:
            item["delivery"] = "发货时间待核实"
        if text_value(item.get("price")).startswith("$"):
            item["price"] = "无人民币价"
        result[code] = item
    return result


def save_stock_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    tmp_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def lookup_stock_with_cache(
    cache_path: Path,
    c_code: str,
    fetcher: Callable[[str], dict[str, Any]] = query_lcsc_stock,
) -> dict[str, Any]:
    code = normalize_c_code(c_code)
    if not code:
        return stock_status("", "NeedCCode")

    try:
        result = fetcher(code)
    except Exception as exc:
        result = stock_status(code, "QueryFailed", str(exc))

    with stock_cache_lock(cache_path):
        cache = load_stock_cache(cache_path)
        if not result.get("ok") and code in cache and cache[code].get("ok"):
            cached = dict(cache[code])
            cached["status"] = f"{result.get('status')}/Cached"
            cached["error"] = text_value(result.get("error"))
            result = cached
        elif result.get("ok") and code in cache:
            result = preserve_candidate_parameters(result, cache[code])
            cached_price = text_value(cache[code].get("price"))
            result_price = text_value(result.get("price"))
            if cached_price.startswith("¥") and result_price == "无人民币价":
                result["price"] = cached_price

        if result.get("ok") or code not in cache or not cache[code].get("ok"):
            cache[code] = result
        save_stock_cache(cache_path, cache)
    return result


def unique_codes_from_rows(rows: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    for row in rows:
        code = normalize_c_code(
            row.get("stock_c_code") or row.get("effective_c_code") or row.get("accepted_c_code") or row.get("lcsc_match_c_code")
        )
        if code and code not in codes:
            codes.append(code)
    return codes


def enrich_rows_with_cached_stock(rows: list[dict[str, Any]], cache_path: Path) -> list[dict[str, Any]]:
    cache = load_stock_cache(cache_path)
    for row in rows:
        code = normalize_c_code(
            row.get("stock_c_code") or row.get("effective_c_code") or row.get("accepted_c_code") or row.get("lcsc_match_c_code")
        )
        if code and code in cache:
            row["stock"] = dict(cache[code])
        elif not code:
            row["stock"] = stock_status("", "NeedCCode")
    return rows


def enrich_rows_with_stock(
    rows: list[dict[str, Any]],
    cache_path: Path,
    fetcher: Callable[[str], dict[str, Any]] = query_lcsc_stock,
    max_workers: int = 8,
) -> list[dict[str, Any]]:
    codes = unique_codes_from_rows(rows)
    cache = load_stock_cache(cache_path)
    fetched: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(codes) or 1))) as executor:
        future_to_code = {executor.submit(lookup_stock_with_cache, cache_path, code, fetcher): code for code in codes}
        for future in as_completed(future_to_code):
            code = future_to_code[future]
            try:
                result = future.result()
            except Exception as exc:
                result = stock_status(code, "QueryFailed", str(exc))
            fetched[code] = result

    for row in rows:
        code = normalize_c_code(
            row.get("stock_c_code") or row.get("effective_c_code") or row.get("accepted_c_code") or row.get("lcsc_match_c_code")
        )
        row["stock"] = fetched.get(code) if code else stock_status("", "NeedCCode")
    return rows
