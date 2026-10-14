"""
Schnapper Tracker — PC Component Deal Finder (eBay DE + Kleinanzeigen)
======================================================================
Backend: Flask + requests + BeautifulSoup4 + re
Run:
    pip install flask requests beautifulsoup4
    python app.py
Then open http://127.0.0.1:5000
"""

import re
import time
import random
import logging
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("schnapper")

app = Flask(__name__, static_folder=".", static_url_path="")

# ---------------------------------------------------------------------------
# 1. HTTP session / anti-block headers
# ---------------------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer": "https://www.google.de/",
        "Cache-Control": "max-age=0",
    })
    return s


def fetch(url: str, timeout: int = 15) -> str | None:
    """Fetch a URL with rotated UA. Returns HTML text or None on failure."""
    try:
        s = make_session()
        # small random delay to look human
        time.sleep(random.uniform(0.4, 1.2))
        r = s.get(url, timeout=timeout)
        if r.status_code != 200:
            log.warning("HTTP %s for %s", r.status_code, url)
            return None
        # eBay/Kleinanzeigen sometimes return captcha/block pages
        low = r.text[:4000].lower()
        if any(k in low for k in ("captcha", "robot check", "bist du ein mensch", "are you a human", "zugriff verweigert", "access denied")):
            # still try to parse; log it
            log.warning("Possible bot-check page for %s", url)
        return r.text
    except Exception as e:  # noqa: BLE001 - anti-crash: never kill a scan
        log.error("fetch failed %s: %s", url, e)
        return None

# ---------------------------------------------------------------------------
# 2. Price parsing (critical)
# ---------------------------------------------------------------------------

# Matches: 1.299 €, 1.299,00 €, 299,99€, € 299, 299 VB, 299,- , "VB 250€",
# "Zu verschenken" / "Gratis" -> 0.0, "Tausch" / "Gesuch" -> None
PRICE_RE = re.compile(r"(\d[\d\s\.\']*\d|\d)\s*(?:[,\.]\s*\d{1,2})?")
FREE_RE = re.compile(r"(zu\s*verschenken|gratis|kostenlos|geschenkt)", re.I)
NO_PRICE_RE = re.compile(r"(tausch|gesuch|suche|anfrage|preis\s*auf|vb\s*\?|unbekannt)", re.I)


def parse_price(raw: str | None) -> float | None:
    """
    Advanced price parser for German listings.
    Handles: '1.299 €', '1.299,00 €', '299,99€', '€ 299', '299 VB', '299,-', 'VB 250'.
    Returns float euros, 0.0 for free, None if unparseable / no concrete price.
    """
    if not raw:
        return None
    t = raw.strip()
    # normalize whitespace & nbsp
    t = t.replace("\xa0", " ").replace("\u202f", " ")
    if FREE_RE.search(t):
        return 0.0
    # "Tausch", "Gesuch" etc. -> no buy price
    # but "VB" alone means Verhandlungsbasis -> still a price, keep going
    tmp = re.sub(r"\bVB\b", "", t, flags=re.I).strip()
    if NO_PRICE_RE.search(tmp):
        return None
    # strip currency symbols / words
    tmp = re.sub(r"(€|EUR|Euro)", " ", tmp, flags=re.I)
    tmp = tmp.replace("-", " ").strip()
    m = PRICE_RE.search(tmp)
    if not m:
        return None
    num = m.group(0)
    num = num.replace(" ", "").replace("'", "").replace("\u2019", "")
    # German format decision:
    # - if both '.' and ',' present: '.' = thousands, ',' = decimal  -> "1.299,99" => 1299.99
    # - if only ',' present: ',' = decimal                             -> "299,99"  => 299.99
    # - if only '.' present: decide by trailing group length:
    #     "1.299" (3 digits after dot) => thousands => 1299
    #     "299.99" (2 digits)          => decimal  => 299.99
    #     "1.299.499"                  => thousands => 1299499
    try:
        if "." in num and "," in num:
            num = num.replace(".", "").replace(",", ".")
        elif "," in num:
            num = num.replace(".", "").replace(",", ".")
        else:
            # only dots (or none)
            parts = num.split(".")
            if len(parts) > 1 and all(len(p) == 3 for p in parts[1:]):
                num = "".join(parts)  # thousands separators
            elif len(parts) == 2 and len(parts[1]) in (1, 2):
                pass  # already decimal, keep dot
            else:
                num = "".join(parts)
        price = float(num)
        if price <= 0:
            return 0.0
        if price > 100_000:  # sanity: typo / phone number matched
            return None
        return round(price, 2)
    except ValueError:
        return None


def clean_text(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s

# ---------------------------------------------------------------------------
# 3. Benchmark dictionary (baseline: Ryzen 5 5600X ≈ 100 / RTX 3060 ≈ 100)
#    Scores are a blended relative-performance index (gaming + compute).
#    Lookup is fuzzy: normalized substring match.
# ---------------------------------------------------------------------------

BENCHMARKS: dict[str, float] = {
    # ---- NVIDIA GPUs ----
    "rtx 5090": 285, "rtx 5080": 225, "rtx 5070 ti": 195, "rtx 5070": 175,
    "rtx 5060 ti": 145, "rtx 5060": 130,
    "rtx 4090": 240, "rtx 4080 super": 205, "rtx 4080": 195, "rtx 4070 ti super": 185,
    "rtx 4070 ti": 175, "rtx 4070 super": 160, "rtx 4070": 145, "rtx 4060 ti": 120,
    "rtx 4060": 105,
    "rtx 3090 ti": 175, "rtx 3090": 165, "rtx 3080 ti": 155, "rtx 3080": 145,
    "rtx 3070 ti": 130, "rtx 3070": 120, "rtx 3060 ti": 110, "rtx 3060": 100,
    "rtx 3050": 75,
    "rtx 2080 ti": 115, "rtx 2080 super": 105, "rtx 2080": 100, "rtx 2070 super": 95,
    "rtx 2070": 88, "rtx 2060 super": 82, "rtx 2060": 75, "gtx 1660 super": 65,
    "gtx 1660 ti": 66, "gtx 1660": 60, "gtx 1650 super": 52, "gtx 1650": 45,
    "gtx 1080 ti": 85, "gtx 1080": 78, "gtx 1070 ti": 72, "gtx 1070": 68, "gtx 1060": 55,
    # ---- AMD GPUs ----
    "rx 7900 xtx": 190, "rx 7900 xt": 170, "rx 7900 gre": 155, "rx 7800 xt": 150,
    "rx 7700 xt": 130, "rx 7600 xt": 105, "rx 7600": 95,
    "rx 6950 xt": 160, "rx 6900 xt": 155, "rx 6800 xt": 145, "rx 6800": 135,
    "rx 6750 xt": 125, "rx 6700 xt": 118, "rx 6700": 110, "rx 6650 xt": 100, "rx 6600 xt": 92,
    "rx 6600": 85, "rx 6500 xt": 55, "rx 6400": 42,
    "rx 5700 xt": 88, "rx 5700": 80, "rx 5600 xt": 70, "rx 5500 xt": 58,
    "rx  Vega 64": 65, "rx vega 56": 60, "rx 590": 52, "rx 580": 48, "rx 570": 42,
    # ---- Intel GPUs ----
    "arc a770": 85, "arc a750": 78, "arc a580": 68, "arc b580": 92, "arc b570": 85,
    # ---- AMD CPUs ----
    "ryzen 9 9950x": 185, "ryzen 9 9900x": 170, "ryzen 7 9800x3d": 175, "ryzen 7 9700x": 150,
    "ryzen 5 9600x": 130, "ryzen 9 7950x3d": 175, "ryzen 9 7950x": 170, "ryzen 9 7900x": 160,
    "ryzen 7 7800x3d": 160, "ryzen 7 7700x": 145, "ryzen 5 7600x": 130, "ryzen 5 7600": 125,
    "ryzen 9 5950x": 150, "ryzen 9 5900x": 140, "ryzen 7 5800x3d": 140, "ryzen 7 5800x": 120,
    "ryzen 5 5700x": 112, "ryzen 5 5600x": 100, "ryzen 5 5600": 95, "ryzen 5 5500": 85,
    "ryzen 5 3600": 72, "ryzen 7 3700x": 85, "ryzen 5 2600": 58,
    # ---- Intel CPUs ----
    "i9-14900k": 180, "i9-14900kf": 180, "i7-14700k": 165, "i7-14700kf": 165,
    "i5-14600k": 145, "i5-14600kf": 145, "i5-14400f": 125, "i5-14400": 125,
    "i9-13900k": 172, "i9-13900kf": 172, "i7-13700k": 158, "i7-13700kf": 158,
    "i5-13600k": 140, "i5-13600kf": 140, "i5-13400f": 118, "i5-13400": 118,
    "i9-12900k": 155, "i9-12900kf": 155, "i7-12700k": 140, "i7-12700kf": 140,
    "i5-12600k": 125, "i5-12600kf": 125, "i5-12400f": 105, "i5-12400": 105,
    "i3-12100f": 80, "i3-12100": 80,
    "i9-11900k": 105, "i7-11700k": 100, "i5-11600k": 90, "i5-11400f": 82,
    "i7-10700k": 90, "i5-10400f": 72,
    # ---- Memory / Storage / Misc (capacity-based pseudo score) ----
    "ddr5 32gb": 100, "ddr4 32gb": 90, "ddr5 16gb": 60, "ddr4 16gb": 55,
    "2tb nvme": 100, "1tb nvme": 60, "500gb nvme": 35, "4tb nvme": 170,
    "2tb ssd": 90, "1tb ssd": 55,
}


def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9äöüß\. ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def lookup_benchmark(query_name: str) -> tuple[float | None, str | None]:
    """
    Fuzzy match query against BENCHMARKS keys.
    Returns (score, matched_key). Longest-key-first so '4070 ti super'
    wins over '4070'.
    """
    q = _norm(query_name)
    # exact / substring, longest keys first
    for key in sorted(BENCHMARKS, key=len, reverse=True):
        nk = _norm(key)
        if nk and nk in q:
            return BENCHMARKS[key], key
    return None, None

# ---------------------------------------------------------------------------
# 4. URL builders
# ---------------------------------------------------------------------------

def ebay_url(term: str) -> str:
    # _sop=1 = price + shipping lowest first; LH_Complete not used (active listings)
    return "https://www.ebay.de/sch/i.html?_nkw=" + urllib.parse.quote_plus(term) + "&_sop=12&_ipg=60"


def kleinanzeigen_url(term: str) -> str:
    slug = re.sub(r"\s+", "-", term.strip().lower())
    slug = urllib.parse.quote(slug)
    return f"https://www.kleinanzeigen.de/s-{slug}/k0"

# ---------------------------------------------------------------------------
# 5. Scrapers (each fully exception-guarded)
# ---------------------------------------------------------------------------

def scrape_ebay(term: str, max_budget: float, limit: int = 30) -> list[dict]:
    out: list[dict] = []
    url = ebay_url(term)
    html = fetch(url)
    if not html:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")
        # eBay DE search results: li.s-card / div.s-card__info, older: li.s-item
        cards = soup.select("li.s-card, li.s-item, div.s-card")
        if not cards:
            cards = soup.select("[class*='s-item'], [class*='s-card']")
        for c in cards[: limit * 2]:  # oversample, filter below
            try:
                # title
                t_el = c.select_one(".s-card__title, .s-item__title, [class*='title']")
                title = clean_text(t_el.get_text(" ") if t_el else "")
                if not title or len(title) < 3:
                    continue
                if title.lower() in ("shop on ebay",):
                    continue
                # link
                a_el = c.select_one("a.s-card__link, a.s-item__link, a[href*='/itm/']")
                href = (a_el.get("href") if a_el else "") or ""
                href = clean_text(href.split("?")[0]) if href else ""
                if href and href.startswith("/"):
                    href = "https://www.ebay.de" + href
                if not href or "/itm/" not in href:
                    # fallback: any anchor with itm
                    continue
                # price
                p_el = c.select_one(".s-card__price, .s-item__price, [class*='price']")
                price = parse_price(p_el.get_text(" ") if p_el else "")
                if price is None:
                    continue
                if price > max_budget:  # discard immediately to reduce noise
                    continue
                # shipping / location
                ship_el = c.select_one(".s-card__shipping, .s-item__shipping, .s-item__logisticsCost")
                loc_el = c.select_one(".s-card__location, .s-item__location")
                shipping = clean_text(ship_el.get_text(" ") if ship_el else "")
                location = clean_text(loc_el.get_text(" ") if loc_el else "")
                meta = " | ".join(x for x in [shipping, location] if x)
                out.append({
                    "title": title,
                    "price": price,
                    "url": href,
                    "platform": "eBay",
                    "location": meta or "Versand möglich",
                    "query": term,
                })
                if len(out) >= limit:
                    break
            except Exception as e:  # per-card guard
                log.debug("ebay card skip: %s", e)
                continue
    except Exception as e:
        log.error("scrape_ebay failed for '%s': %s", term, e)
    return out


def scrape_kleinanzeigen(term: str, max_budget: float, limit: int = 30) -> list[dict]:
    out: list[dict] = []
    url = kleinanzeigen_url(term)
    html = fetch(url)
    if not html:
        return out
    try:
        soup = BeautifulSoup(html, "html.parser")
        # Kleinanzeigen SERP: article.aditem, li.ad-listitem
        cards = soup.select("article.aditem, li.ad-listitem, article[data-adid], ul#srchrslt-adtable article")
        if not cards:
            # fallback: any anchor to /s-anzeige/
            cards = soup.select("a[href*='/s-anzeige/']")
            items = []
            for a in cards:
                items.append({"anchor_only": a})
            cards = items
        for c in cards:
            try:
                if "anchor_only" in c:
                    a_el = c["anchor_only"]
                    title = clean_text(a_el.get_text(" "))
                    href = a_el.get("href", "")
                    # price/location unknown in fallback -> skip (can't verify budget)
                    if not title or len(title) < 4:
                        continue
                    if href.startswith("/"):
                        href = "https://www.kleinanzeigen.de" + href
                    # try sibling price
                    parent = a_el.find_parent(["article", "li", "div"])
                    ptxt = parent.get_text(" ") if parent else ""
                    m = re.search(r"(\d[\d\.\s]*\s*(?:[,\.]\d{1,2})?\s*€?)", ptxt)
                    price = parse_price(m.group(0) if m else "")
                    if price is None or price > max_budget:
                        continue
                    out.append({
                        "title": title[:140], "price": price, "url": href.split("?")[0],
                        "platform": "Kleinanzeigen", "location": "Kleinanzeigen", "query": term,
                    })
                    if len(out) >= limit:
                        break
                    continue
                # standard card
                a_el = c.select_one("a.ellipsis, a[href*='/s-anzeige/']")
                title = clean_text(a_el.get_text(" ") if a_el else c.select_one(".text-module-begin").get_text(" ") if c.select_one(".text-module-begin") else "")
                if not title or len(title) < 4:
                    continue
                href = (a_el.get("href") if a_el else "") or ""
                if href.startswith("/"):
                    href = "https://www.kleinanzeigen.de" + href
                href = href.split("?")[0].split("#")[0]
                if not href:
                    continue
                p_el = c.select_one(".aditem-main--middle--price, .aditem-price, [class*='price']")
                price = parse_price(p_el.get_text(" ") if p_el else c.get_text(" "))
                # kleinanzeigen price element sometimes nested oddly; fallback regex on card text
                if price is None:
                    m = re.search(r"(\d[\d\.\s]*\s*(?:[,\.]\d{1,2})?\s*€)", c.get_text(" "))
                    price = parse_price(m.group(0) if m else "")
                if price is None:
                    continue
                if price > max_budget:
                    continue
                loc_el = c.select_one(".aditem-main--top--left, .aditem-main--top, [class*='location']")
                location = clean_text(loc_el.get_text(" ") if loc_el else "")
                out.append({
                    "title": title, "price": price, "url": href,
                    "platform": "Kleinanzeigen", "location": location or "Abholung / Versand",
                    "query": term,
                })
                if len(out) >= limit:
                    break
            except Exception as e:
                log.debug("kleinanzeigen card skip: %s", e)
                continue
    except Exception as e:
        log.error("scrape_kleinanzeigen failed for '%s': %s", term, e)
    return out

# ---------------------------------------------------------------------------
# 6. Scan orchestration + Schnapper logic
# ---------------------------------------------------------------------------

SCHNAPPER_THRESHOLD = 0.15  # >15% below average => Schnapper


def scan_component(item: dict) -> dict:
    name = str(item.get("name", "")).strip()
    try:
        max_budget = float(item.get("max_budget", 0))
    except (TypeError, ValueError):
        max_budget = 0
    if not name or max_budget <= 0:
        return {"query": name, "max_budget": max_budget, "listings": [], "error": "invalid name/max_budget"}

    listings: list[dict] = []
    errors: list[str] = []

    # Run both platforms in parallel, each guarded
    with ThreadPoolExecutor(max_workers=2) as ex:
        fut = {
            ex.submit(scrape_ebay, name, max_budget): "eBay",
            ex.submit(scrape_kleinanzeigen, name, max_budget): "Kleinanzeigen",
        }
        for f in as_completed(fut):
            platform = fut[f]
            try:
                listings.extend(f.result())
            except Exception as e:  # anti-crash: one platform never kills the scan
                msg = f"{platform} failed: {e}"
                log.error(msg)
                errors.append(msg)

    # Benchmark + value score
    bench_score, bench_key = lookup_benchmark(name)
    for l in listings:
        l["benchmark"] = bench_score
        l["benchmark_key"] = bench_key
        l["value_score"] = round(bench_score / l["price"], 4) if bench_score and l["price"] else 0.0

    # Schnapper detection: >15% below component average AND within budget
    prices = [l["price"] for l in listings if l["price"] > 0]
    avg = round(sum(prices) / len(prices), 2) if prices else 0.0
    for l in listings:
        discount = round((avg - l["price"]) / avg, 4) if avg else 0.0
        l["avg_price"] = avg
        l["discount_vs_avg"] = discount
        l["is_schnapper"] = bool(avg and discount >= SCHNAPPER_THRESHOLD and l["price"] <= max_budget)

    # default sort: value score desc
    listings.sort(key=lambda x: (x["is_schnapper"], x["value_score"]), reverse=True)

    return {
        "query": name,
        "max_budget": max_budget,
        "benchmark": bench_score,
        "benchmark_key": bench_key,
        "avg_price": avg,
        "count": len(listings),
        "schnapper_count": sum(1 for l in listings if l["is_schnapper"]),
        "listings": listings,
        "errors": errors,
        "urls": {"ebay": ebay_url(name), "kleinanzeigen": kleinanzeigen_url(name)},
    }

# ---------------------------------------------------------------------------
# 7. Flask routes
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(".", "index.html")


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "benchmarks": len(BENCHMARKS)})


@app.get("/api/benchmarks")
def benchmarks():
    return jsonify(BENCHMARKS)


@app.post("/api/scan")
def scan():
    t0 = time.time()
    try:
        data = request.get_json(force=True, silent=True)
    except Exception:
        data = None
    # accept: array directly, or {items:[...]}, or {components:[...]}
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("items") or data.get("components") or data.get("targets") or []
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Send JSON array like [{\"name\":\"RTX 3070\",\"max_budget\":300}]"}), 400
    if len(items) > 20:
        return jsonify({"error": "Max 20 components per scan."}), 400

    results = []
    # scan components in parallel (bounded)
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
        fut = {ex.submit(scan_component, it): it for it in items}
        for f in as_completed(fut):
            try:
                results.append(f.result())
            except Exception as e:
                it = fut[f]
                log.error("component scan crashed: %s (%s)", it, e)
                results.append({"query": str(it), "listings": [], "error": str(e)})

    # keep original order
    order = [str(i.get("name", "")) for i in items if isinstance(i, dict)]
    results.sort(key=lambda r: order.index(r["query"]) if r["query"] in order else 999)

    all_listings = [l for r in results for l in r.get("listings", [])]
    elapsed = round(time.time() - t0, 2)
    log.info("scan done: %d components, %d listings in %ss", len(results), len(all_listings), elapsed)
    return jsonify({
        "results": results,
        "total": len(all_listings),
        "schnapper_total": sum(1 for l in all_listings if l.get("is_schnapper")),
        "elapsed_s": elapsed,
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
