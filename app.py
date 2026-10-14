#!/usr/bin/env python3
"""
PC Parts Tracker - backend

Scrapes eBay.de and Kleinanzeigen.de for the components listed in targets.json,
filters by max_budget and scores every listing by benchmark points per euro.

Run:  python app.py   ->   http://127.0.0.1:5000
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_from_directory

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TARGETS_FILE = os.path.join(BASE_DIR, "targets.json")

EBAY_BASE = os.environ.get("EBAY_BASE", "https://www.ebay.de")
KA_BASE = "https://www.kleinanzeigen.de"

CACHE_TTL = int(os.environ.get("CACHE_TTL", "300"))        # seconds
BROWSER_MODE = os.environ.get("SCRAPER_BROWSER", "off")    # off | fallback | always
DEBUG_DUMP = os.environ.get("DEBUG_DUMP", "") == "1"       # save fetched HTML to ./debug
MIN_PRICE_RATIO = 0.15   # listings below 15 % of max_budget are treated as bait / accessories

app = Flask(__name__, static_folder=None)
POOL = ThreadPoolExecutor(max_workers=4)

# --------------------------------------------------------------------------- #
# Hardcoded benchmark tables (approximate, relative scores - edit freely)
# GPUs: roughly PassMark G3D Mark.  CPUs: roughly PassMark CPU Mark.
# The scale differs between GPUs and CPUs, so compare value scores within one
# component type only.
# --------------------------------------------------------------------------- #
GPU_BENCHMARKS = {
    "GTX 1060": 10000, "GTX 1070": 14000, "GTX 1070 Ti": 15000, "GTX 1080": 15800,
    "GTX 1080 Ti": 18400, "GTX 1650": 7800, "GTX 1650 Super": 10400,
    "GTX 1660": 11500, "GTX 1660 Super": 12500, "GTX 1660 Ti": 12800,
    "RTX 2060": 13900, "RTX 2060 Super": 16300, "RTX 2070": 16000,
    "RTX 2070 Super": 18200, "RTX 2080": 19000, "RTX 2080 Super": 19800,
    "RTX 2080 Ti": 21500,
    "RTX 3050": 12700, "RTX 3060": 17000, "RTX 3060 Ti": 20200, "RTX 3070": 22000,
    "RTX 3070 Ti": 23600, "RTX 3080": 25000, "RTX 3080 Ti": 27000,
    "RTX 3090": 26900, "RTX 3090 Ti": 29000,
    "RTX 4060": 19700, "RTX 4060 Ti": 22500, "RTX 4070": 26700,
    "RTX 4070 Super": 30300, "RTX 4070 Ti": 31500, "RTX 4070 Ti Super": 34000,
    "RTX 4080": 34800, "RTX 4080 Super": 35500, "RTX 4090": 38500,
    "RTX 5060": 21000, "RTX 5060 Ti": 24500, "RTX 5070": 29500,
    "RTX 5070 Ti": 36500, "RTX 5080": 39500, "RTX 5090": 46500,
    "RX 5500 XT": 9800, "RX 5600 XT": 14000, "RX 5700": 16000, "RX 5700 XT": 17800,
    "RX 6500 XT": 8900, "RX 6600": 14900, "RX 6600 XT": 16700, "RX 6650 XT": 17200,
    "RX 6700 XT": 19600, "RX 6750 XT": 20700, "RX 6800": 22300, "RX 6800 XT": 25000,
    "RX 6900 XT": 26500, "RX 6950 XT": 27500,
    "RX 7600": 17800, "RX 7600 XT": 18800, "RX 7700 XT": 24000, "RX 7800 XT": 27700,
    "RX 7900 GRE": 30000, "RX 7900 XT": 31500, "RX 7900 XTX": 35000,
    "RX 9060 XT": 24500, "RX 9070": 33500, "RX 9070 XT": 37500,
    "Arc A380": 6300, "Arc A750": 15500, "Arc A770": 17000,
    "Arc B570": 18500, "Arc B580": 20500,
}

CPU_BENCHMARKS = {
    "Ryzen 5 2600": 13000, "Ryzen 7 2700X": 16500,
    "Ryzen 5 3600": 17800, "Ryzen 5 3600X": 18500, "Ryzen 7 3700X": 22500,
    "Ryzen 7 3800X": 23500, "Ryzen 9 3900X": 32000,
    "Ryzen 5 5500": 19400, "Ryzen 5 5600": 21900, "Ryzen 5 5600X": 22300,
    "Ryzen 5 5600G": 19800, "Ryzen 5 5600GT": 21500, "Ryzen 7 5700X": 24500,
    "Ryzen 7 5700X3D": 25000, "Ryzen 7 5800X": 27500, "Ryzen 7 5800X3D": 28000,
    "Ryzen 9 5900X": 39500, "Ryzen 9 5950X": 46000,
    "Ryzen 5 7600": 28500, "Ryzen 5 7600X": 29500, "Ryzen 7 7700": 34000,
    "Ryzen 7 7700X": 35500, "Ryzen 7 7800X3D": 34500, "Ryzen 9 7900X": 52000,
    "Ryzen 9 7950X": 63000, "Ryzen 5 9600X": 30500, "Ryzen 7 9700X": 36500,
    "Ryzen 7 9800X3D": 38500, "Ryzen 9 9900X": 52500, "Ryzen 9 9950X": 66000,
    "Core i5-10400F": 12500, "Core i5-11400F": 17000, "Core i7-10700K": 17500,
    "Core i5-12400": 19700, "Core i5-12400F": 19500, "Core i5-12600K": 27700,
    "Core i7-12700": 31500, "Core i7-12700K": 34000, "Core i9-12900K": 41000,
    "Core i5-13400F": 24900, "Core i5-13600K": 38300, "Core i7-13700K": 46000,
    "Core i9-13900K": 59000, "Core i5-14400F": 25500, "Core i5-14600K": 39500,
    "Core i7-14700K": 53000, "Core i9-14900K": 60000,
    "Core Ultra 5 245K": 42000, "Core Ultra 7 265K": 59000, "Core Ultra 9 285K": 67000,
}

_STRIP_WORDS = re.compile(
    r"\b(nvidia|geforce|amd|radeon|intel|core|graphics|grafikkarte|prozessor|processor|cpu|gpu)\b"
)


def norm(text: str) -> str:
    """'GeForce RTX 3070' -> 'rtx3070', 'Intel Core i5-12400F' -> 'i512400f'."""
    t = _STRIP_WORDS.sub(" ", (text or "").lower())
    return re.sub(r"[^a-z0-9]", "", t)


BENCH: dict[str, dict] = {}
for _kind, _table in (("gpu", GPU_BENCHMARKS), ("cpu", CPU_BENCHMARKS)):
    for _name, _score in _table.items():
        BENCH[norm(_name)] = {"name": _name, "score": _score, "kind": _kind}


def detect_key(text: str) -> str | None:
    """Most specific known model contained in text ('RTX 3070 Ti' beats 'RTX 3070')."""
    n = norm(text)
    best = None
    for key in BENCH:
        if key in n and (best is None or len(key) > len(best)):
            best = key
    return best


# --------------------------------------------------------------------------- #
# Fetching (polite, browser-like requests with optional Playwright fallback)
# --------------------------------------------------------------------------- #
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]
BLOCK_MARKERS = (
    "captcha", "pardon our interruption", "access denied", "robot check",
    "verify you are a human", "unusual traffic", "zugriff verweigert",
)


class ScrapeError(Exception):
    pass


_local = threading.local()
_browser_lock = threading.Semaphore(1)


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
            "Accept-Encoding": "gzip, deflate",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "DNT": "1",
        })
        _local.session = s
    return s


def fetch_requests(url: str, referer: str | None = None, attempts: int = 3) -> str:
    session = _session()
    last = "Request failed"
    for i in range(attempts):
        time.sleep(random.uniform(0.4, 1.3) if i == 0 else (2 ** i) + random.random())
        try:
            r = session.get(url, timeout=20, headers={"Referer": referer} if referer else None)
        except requests.RequestException as exc:
            last = f"Network error ({exc.__class__.__name__})"
            continue
        if r.status_code == 200:
            return r.text
        last = f"HTTP {r.status_code}"
        if r.status_code not in (403, 429, 503):
            break
    raise ScrapeError(last)


def fetch_browser(url: str) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("Playwright is not installed (pip install playwright && playwright install chromium)") from exc
    with _browser_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    locale="de-DE", user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1366, "height": 900},
                )
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                for selector in ("#gdpr-banner-accept", "#onetrust-accept-btn-handler", "button#gdpr-banner-accept"):
                    try:
                        page.click(selector, timeout=1500)
                        break
                    except Exception:
                        pass
                page.wait_for_timeout(1500)
                return page.content()
            finally:
                browser.close()


def looks_blocked(html: str) -> bool:
    head = html[:6000].lower()
    return any(m in head for m in BLOCK_MARKERS)


def fetch_html(url: str, referer: str | None = None) -> str:
    if BROWSER_MODE == "always":
        return fetch_browser(url)
    try:
        return fetch_requests(url, referer)
    except ScrapeError:
        if BROWSER_MODE == "fallback":
            return fetch_browser(url)
        raise


def dump_debug(name: str, html: str) -> None:
    if not DEBUG_DUMP:
        return
    folder = os.path.join(BASE_DIR, "debug")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{name}.html"), "w", encoding="utf-8") as fh:
        fh.write(html)


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
_PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?")
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "mär": 3, "apr": 4, "mai": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12}


def parse_price(text: str | None) -> float | None:
    """German price formats: 'EUR 1.299,00', '300 € VB', 'EUR 20,00 bis EUR 30,00' (first value)."""
    if not text or re.search(r"[$£]|\busd\b|\bgbp\b", text.lower()):
        return None
    m = _PRICE_RE.search(text)
    if not m:
        return None
    whole = m.group(1).replace(".", "")
    cents = (m.group(2) or "0").ljust(2, "0")
    return float(f"{whole}.{cents}")


def clean_text(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_ka_date(label: str) -> float | None:
    s = label.lower()
    mt = re.search(r"(\d{1,2}):(\d{2})", s)
    hour, minute = (int(mt.group(1)), int(mt.group(2))) if mt else (0, 0)
    now = datetime.now()
    if "heute" in s:
        d = now.date()
    elif "gestern" in s:
        d = (now - timedelta(days=1)).date()
    else:
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
        if not m:
            return None
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    return datetime(d.year, d.month, d.day, hour, minute).timestamp()


def parse_ebay_sold_date(label: str) -> float | None:
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zäÄ]+)\.?\s*(\d{4})", label)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower()[:3])
    if not month:
        return None
    return datetime(int(m.group(3)), month, int(m.group(1))).timestamp()


_EBAY_PREFIX = re.compile(r"^(neues angebot|new listing)\s*", re.I)
_EBAY_SUFFIX = re.compile(r"\s*(wird in neuem fenster oder tab geöffnet|opens in a new window or tab)\s*$", re.I)


def clean_ebay_title(text: str) -> str:
    t = clean_text(text)
    t = _EBAY_PREFIX.sub("", t)
    return _EBAY_SUFFIX.sub("", t).strip()


# --------------------------------------------------------------------------- #
# eBay
# --------------------------------------------------------------------------- #
def ebay_url(name: str, max_budget: float, mode: str) -> str:
    params = {"_nkw": name, "_sacat": 0, "_ipg": 60, "_udhi": int(max_budget), "rt": "nc"}
    if mode == "sold":
        params.update({"LH_Sold": 1, "LH_Complete": 1, "_sop": 13})   # recently ended first
    else:
        params["_sop"] = 10                                           # newly listed first
    return f"{EBAY_BASE}/sch/i.html?{urlencode(params)}"


def parse_ebay(html: str, mode: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for rank, card in enumerate(soup.select("li.s-item, li.s-card")):
        link_el = card.select_one("a.s-item__link, a.s-card__link, a[href*='/itm/']")
        title_el = card.select_one(".s-item__title, .s-card__title")
        price_el = card.select_one(".s-item__price, .s-card__price")
        if not (link_el and title_el and price_el):
            continue
        title = clean_ebay_title(title_el.get_text(" ", strip=True))
        if not title or title.lower() in ("shop on ebay", "shop auf ebay"):
            continue
        price = parse_price(price_el.get_text(" ", strip=True))
        href = link_el.get("href", "")
        if price is None or "/itm/" not in href:
            continue

        lines = [clean_text(x) for x in card.get_text("\n", strip=True).split("\n") if x.strip()]
        ship_cost, ship_label, location, posted_label, auction = None, "Unknown", "", "", False
        for line in lines:
            low = line.lower()
            if re.search(r"kostenlos(er)?\s+(versand|lieferung)|free (shipping|delivery)", low):
                ship_cost, ship_label = 0.0, "Free shipping"
            else:
                m = re.search(r"\+\s*(?:eur\s*)?(\d[\d.]*,\d{2})\s*(?:eur)?\s*(versand|lieferung|shipping|delivery)", low)
                if m:
                    ship_cost = parse_price(m.group(1))
                    ship_label = f"+€{ship_cost:.2f}"
                elif "abholung" in low and ship_cost is None:
                    ship_label = "Pickup only"
            if not location and re.match(r"^(aus|from)\s+\S", line, re.I):
                location = re.sub(r"^(aus|from)\s+", "", line, flags=re.I)
            if not posted_label and low.startswith("verkauft"):
                posted_label = line
            if re.search(r"\b\d+\s+gebote?\b|\b\d+\s+bids?\b", low):
                auction = True

        out.append({
            "platform": "ebay", "title": title, "link": href.split("?")[0], "price": price,
            "shipping_cost": ship_cost, "shipping_label": ship_label, "location": location,
            "posted_label": posted_label,
            "posted_ts": parse_ebay_sold_date(posted_label) if mode == "sold" else None,
            "auction": auction, "negotiable": False, "rank": rank,
        })
    return out


def scrape_ebay(name: str, max_budget: float, mode: str) -> list[dict]:
    url = ebay_url(name, max_budget, mode)
    html = fetch_html(url, referer=EBAY_BASE + "/")
    dump_debug(f"ebay_{norm(name)}_{mode}", html)
    items = parse_ebay(html, mode)
    if not items and looks_blocked(html):
        raise ScrapeError("eBay served a bot-check page. Wait a few minutes or set SCRAPER_BROWSER=fallback.")
    return items


# --------------------------------------------------------------------------- #
# Kleinanzeigen
# --------------------------------------------------------------------------- #
def ka_urls(name: str, max_budget: float) -> list[str]:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return [
        f"{KA_BASE}/s-preis:0:{int(max_budget)}/{slug}/k0",
        f"{KA_BASE}/s-{slug}/k0",
    ]


def parse_kleinanzeigen(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for rank, card in enumerate(soup.select("article.aditem, article[data-adid]")):
        link_el = card.select_one("a.ellipsis, h2 a, a[href*='/s-anzeige/']")
        href = card.get("data-href") or (link_el.get("href") if link_el else "")
        title_el = card.select_one("a.ellipsis, h2 a, h2")
        price_el = card.select_one("[class*='price-shipping--price'], p[class*='price']")
        if not (href and title_el):
            continue
        price_text = clean_text(price_el.get_text(" ", strip=True)) if price_el else ""
        if "verschenken" in price_text.lower():
            price = 0.0
        else:
            price = parse_price(price_text)
        if price is None:
            continue

        loc_el = card.select_one("[class*='main--top--left']")
        date_el = card.select_one("[class*='main--top--right']")
        card_text = clean_text(card.get_text(" ", strip=True))
        posted_label = clean_text(date_el.get_text(" ", strip=True)) if date_el else ""

        ship_cost, ship_label = None, "Pickup only"
        m = re.search(r"versand ab\s*(\d[\d.]*,\d{2})", card_text.lower())
        if m:
            ship_cost = parse_price(m.group(1))
            ship_label = f"Shipping from €{ship_cost:.2f}"
            ship_cost = None          # lowest tier only; keep total honest, do not guess
        elif "versand möglich" in card_text.lower():
            ship_label = "Shipping possible"

        out.append({
            "platform": "kleinanzeigen", "title": clean_text(title_el.get_text(" ", strip=True)),
            "link": urljoin(KA_BASE, href), "price": price,
            "shipping_cost": ship_cost, "shipping_label": ship_label,
            "location": clean_text(loc_el.get_text(" ", strip=True)) if loc_el else "",
            "posted_label": posted_label, "posted_ts": parse_ka_date(posted_label),
            "auction": False, "negotiable": bool(re.search(r"\bVB\b", price_text)), "rank": rank,
        })
    return out


def scrape_kleinanzeigen(name: str, max_budget: float) -> list[dict]:
    last_error = None
    for i, url in enumerate(ka_urls(name, max_budget)):
        try:
            html = fetch_html(url, referer=KA_BASE + "/")
        except ScrapeError as exc:
            last_error = exc
            continue
        dump_debug(f"ka_{norm(name)}_{i}", html)
        items = parse_kleinanzeigen(html)
        if items:
            return items
        if looks_blocked(html):
            raise ScrapeError("Kleinanzeigen served a bot-check page. Wait a few minutes or set SCRAPER_BROWSER=fallback.")
    if last_error:
        raise last_error
    return []


# --------------------------------------------------------------------------- #
# Filtering and scoring
# --------------------------------------------------------------------------- #
DEFECT_RE = re.compile(r"\b(defekt\w*|kaputt\w*|bastler\w*|ersatzteil\w*|for parts|artefakte?|no post|ohne funktion)\b", re.I)
JUNK_RE = re.compile(
    r"\b(suche|gesucht|ankauf|wir kaufen|tausche|tausch|wasserkühler|backplate|lüfter|halterung|"
    r"adapter|kabel|poster|aufkleber|nur karton|nur ovp|leerkarton|komplett[- ]?pc|gaming[- ]?pc|"
    r"pc komplett|laptop|notebook|bundle|mainboard|motherboard|platine)\b", re.I)


def build_listings(raws: list[dict], name: str, max_budget: float, include_defective: bool):
    target_key = norm(name) if norm(name) in BENCH else None
    name_tokens = [norm(t) for t in re.split(r"[\s\-]+", name) if norm(t)]
    bench = BENCH.get(target_key) if target_key else None

    listings, seen, hidden = [], set(), 0
    for r in raws:
        title = r["title"]
        if JUNK_RE.search(title):
            continue
        if not include_defective and DEFECT_RE.search(title):
            continue
        if target_key:
            if detect_key(title) != target_key:       # wrong variant (Ti / XT / Super ...) or model missing
                continue
        else:
            n_title = norm(title)
            if not all(tok in n_title for tok in name_tokens):
                continue
        if r["price"] < max_budget * MIN_PRICE_RATIO:
            continue

        total = r["price"] + (r["shipping_cost"] or 0.0)
        if total > max_budget:
            hidden += 1
            continue
        if r["link"] in seen:
            continue
        seen.add(r["link"])

        value = round(bench["score"] / total, 1) if bench and total > 0 else None
        listings.append({
            **r,
            "id": hashlib.md5(r["link"].encode()).hexdigest()[:12],
            "target": name,
            "total": round(total, 2),
            "bench_name": bench["name"] if bench else None,
            "bench_score": bench["score"] if bench else None,
            "value": value,
        })
    return listings, hidden


# --------------------------------------------------------------------------- #
# Cache + orchestration
# --------------------------------------------------------------------------- #
_cache: dict[tuple, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def get_raw(platform: str, name: str, max_budget: float, mode: str, force: bool):
    key = (platform, name.lower(), int(max_budget), mode)
    if not force:
        with _cache_lock:
            hit = _cache.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1], True
    if platform == "ebay":
        raws = scrape_ebay(name, max_budget, mode)
    else:
        raws = scrape_kleinanzeigen(name, max_budget)
    with _cache_lock:
        _cache[key] = (time.time(), raws)
    return raws, False


def search_target(name, max_budget, platforms, mode, include_defective, force):
    futures = {p: POOL.submit(get_raw, p, name, max_budget, mode, force) for p in platforms}
    listings, errors, hidden, cached = [], [], 0, True
    for platform, fut in futures.items():
        try:
            raws, was_cached = fut.result(timeout=120)
        except ScrapeError as exc:
            errors.append({"platform": platform, "message": str(exc)})
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append({"platform": platform, "message": f"Unexpected error: {exc.__class__.__name__}: {exc}"})
            continue
        cached = cached and was_cached
        items, h = build_listings(raws, name, max_budget, include_defective)
        listings.extend(items)
        hidden += h
    return {"listings": listings, "errors": errors, "hidden_over_budget": hidden, "cached": cached}


# --------------------------------------------------------------------------- #
# Targets file
# --------------------------------------------------------------------------- #
def validate_targets(data) -> list[dict]:
    if not isinstance(data, list) or not data:
        raise ValueError("JSON must be a non-empty array of {\"name\", \"max_budget\"} objects.")
    if len(data) > 50:
        raise ValueError("At most 50 components are supported.")
    out = []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Entry {i} is not an object.")
        name, budget = item.get("name"), item.get("max_budget")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"Entry {i}: \"name\" must be a non-empty string.")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
            raise ValueError(f"Entry {i} ({name}): \"max_budget\" must be a number greater than 0.")
        out.append({"name": name.strip(), "max_budget": int(budget) if float(budget).is_integer() else float(budget)})
    return out


def annotate(targets: list[dict]) -> list[dict]:
    return [{**t, "benchmark": (BENCH[norm(t["name"])]["name"] if norm(t["name"]) in BENCH else None)} for t in targets]


def load_targets() -> list[dict]:
    try:
        with open(TARGETS_FILE, encoding="utf-8") as fh:
            return validate_targets(json.load(fh))
    except (OSError, ValueError):
        return []


def save_targets(targets: list[dict]) -> None:
    with open(TARGETS_FILE, "w", encoding="utf-8") as fh:
        json.dump(targets, fh, indent=2, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/api/targets")
def api_get_targets():
    return jsonify({"targets": annotate(load_targets())})


@app.put("/api/targets")
def api_put_targets():
    try:
        targets = validate_targets(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    save_targets(targets)
    return jsonify({"targets": annotate(targets)})


@app.get("/api/benchmarks")
def api_benchmarks():
    return jsonify({"benchmarks": sorted(BENCH.values(), key=lambda b: (b["kind"], b["name"]))})


@app.post("/api/search")
def api_search():
    data = request.get_json(silent=True) or {}
    try:
        (target,) = validate_targets([{"name": data.get("name"), "max_budget": data.get("max_budget")}])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    mode = "sold" if data.get("mode") == "sold" else "active"
    platforms = [p for p in data.get("platforms", ["ebay", "kleinanzeigen"]) if p in ("ebay", "kleinanzeigen")]
    if mode == "sold":
        platforms = [p for p in platforms if p == "ebay"]       # Kleinanzeigen has no sold history
    if not platforms:
        return jsonify({"error": "No supported platform selected."}), 400
    result = search_target(
        target["name"], target["max_budget"], platforms, mode,
        bool(data.get("include_defective")), bool(data.get("force")),
    )
    return jsonify(result)


if __name__ == "__main__":
    if not os.path.exists(TARGETS_FILE):
        save_targets([{"name": "RTX 3070", "max_budget": 300}, {"name": "Ryzen 5 5600X", "max_budget": 110}])
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False, threaded=True)
