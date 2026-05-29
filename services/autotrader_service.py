"""
AutoTrader UK — direct Playwright browser access using the user's installed Chrome.
Uses real Chrome (not Playwright's Chromium) which bypasses Cloudflare much better.

Strategy:
1. Build a pre-filled search URL from the WBAC vehicle details.
2. Launch real Chrome (channel="chrome"), navigate, wait for CF challenge to resolve.
3. Dismiss cookie banner, scroll to load listings.
4. Take a full-width screenshot of the search results page.
5. Also attempt to extract structured listing data from the rendered HTML.
6. Return screenshot (base64 JPEG) + structured listings + search URL.
"""
import os
import re
import base64
import logging
import urllib.parse
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PTE
from firecrawl import FirecrawlApp
from dotenv import load_dotenv
load_dotenv()
_FC_KEY = os.getenv("FIRECRAWL_API_KEY", "fc-4f9e85b2a341424ab18f4bb7a50e5b11")

from .vehicle_match_service import (
    compute_confidence, normalize_mileage, normalize_year,
    normalize_price, price_stats,
)

log = logging.getLogger(__name__)

_MILE_TOLERANCE = 10_000    # ±10,000 miles as requested

# Comprehensive stealth init script
_STEALTH_JS = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-GB','en-US','en'] });
    Object.defineProperty(navigator, 'platform',  { get: () => 'Win32' });
    window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (params) =>
      (params.name === 'notifications')
        ? Promise.resolve({ state: Notification.permission })
        : origQuery(params);
"""


# Body type mapping: WBAC/Carwow names → AutoTrader URL values
_BODY_TYPE_MAP = {
    "cabriolet":      "Convertible",
    "convertible":    "Convertible",
    "roadster":       "Convertible",
    "spider":         "Convertible",
    "targa":          "Convertible",
    "coupe":          "Coupe",
    "fastback":       "Coupe",
    "hatchback":      "Hatchback",
    "saloon":         "Saloon",
    "sedan":          "Saloon",
    "estate":         "Estate",
    "shooting brake": "Estate",
    "suv":            "SUV",
    "crossover":      "SUV",
    "mpv":            "MPV",
    "people carrier": "MPV",
    "van":            "Other",
    "pickup":         "Other",
}

_FUEL_MAP = {
    "petrol":       "Petrol",
    "diesel":       "Diesel",
    "electric":     "Electric",
    "hybrid":       "Hybrid",
    "plug-in hybrid": "Plug-in Hybrid",
}

_TRANS_MAP = {
    "automatic":  "Automatic",
    "auto":       "Automatic",
    "pdk":        "Automatic",
    "dsg":        "Automatic",
    "cvt":        "Automatic",
    "tiptronic":  "Automatic",
    "s-tronic":   "Automatic",
    "manual":     "Manual",
}


def _split_base_and_variant(make: str, model: str) -> tuple[str, str]:
    """
    Split a refined model string into base model + variant keywords.

    Examples:
      "911 Carrera S"  →  ("911", "Carrera S")
      "911 Turbo S"    →  ("911", "Turbo S")
      "911"            →  ("911", "")
      "3 Series 320d"  →  ("3 Series", "320d")
    """
    if not model:
        return model, ""

    # Known base models for common makes (extend as needed)
    KNOWN_BASES = {
        "PORSCHE": ["911", "CAYENNE", "MACAN", "PANAMERA", "TAYCAN", "BOXSTER", "CAYMAN"],
        "BMW":     ["1 SERIES", "2 SERIES", "3 SERIES", "4 SERIES", "5 SERIES",
                    "6 SERIES", "7 SERIES", "8 SERIES", "X1", "X2", "X3", "X4", "X5", "X6", "X7", "M3", "M4", "M5"],
        "MERCEDES": ["A CLASS", "B CLASS", "C CLASS", "E CLASS", "S CLASS",
                     "GLA", "GLB", "GLC", "GLE", "GLS", "AMG"],
        "AUDI":    ["A1", "A2", "A3", "A4", "A5", "A6", "A7", "A8", "Q2", "Q3", "Q5", "Q7", "Q8", "TT", "R8"],
    }

    model_upper = model.upper()
    make_upper  = make.upper()

    bases = KNOWN_BASES.get(make_upper, [])
    for base in bases:
        if model_upper.startswith(base):
            remainder = model[len(base):].strip()
            return base.title() if base == make_upper else base, remainder

    # Generic fallback: first word is base model, rest is variant
    parts = model.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return model, ""


def build_motors_url(make: str, model: str) -> str:
    """Motors.co.uk path-based URL — used as Cloudflare fallback."""
    ms = make.lower().replace(" ", "-")
    mo = model.lower().replace(" ", "-")
    return f"https://www.motors.co.uk/used-cars/{ms}/{mo}/"


def _try_firecrawl(fc: FirecrawlApp, url: str, label: str, result: dict,
                   proxy: str = "stealth") -> list:
    """Scrape a URL with Firecrawl JSON extraction; return list of listing dicts."""
    log.info("[%s] Firecrawl scrape: %s", label, url)
    try:
        scraped = fc.scrape(
            url,
            formats=[{
                "type": "json",
                "prompt": (
                    "Extract every individual used-car listing on this page. "
                    "For each listing return: title (full name with make/model/trim), "
                    "price as a plain integer in GBP, year as 4-digit integer, "
                    "mileage as integer miles, fuel_type, transmission, dealer name."
                ),
                "schema": {
                    "type": "object",
                    "properties": {
                        "listings": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title":        {"type": "string"},
                                    "price":        {"type": "number"},
                                    "year":         {"type": "number"},
                                    "mileage":      {"type": "number"},
                                    "fuel_type":    {"type": "string"},
                                    "transmission": {"type": "string"},
                                    "dealer":       {"type": "string"},
                                },
                            },
                        },
                    },
                },
            }],
            wait_for=8000,
            proxy=proxy,
        )
        raw = getattr(scraped, "json", None) or {}
        lst = raw.get("listings") or raw.get("carListings") or []

        seen, out = set(), []
        for l in lst:
            price = normalize_price(l.get("price", 0))
            if price < 500:
                continue
            key = f"{l.get('title', '')}_{price}"
            if key in seen:
                continue
            seen.add(key)
            l["price"]       = f"£{price:,}"
            l["price_num"]   = price
            l["year"]        = normalize_year(l.get("year"))
            l["mileage"]     = normalize_mileage(l.get("mileage"))
            l["mileage_fmt"] = f"{l['mileage']:,} mi" if l["mileage"] else None
            out.append(l)
        log.info("[%s] Returned %d listings", label, len(out))
        return out
    except Exception as e:
        log.warning("[%s] Firecrawl error: %s", label, e)
        return []


def build_search_url(
    make: str, model: str, year: int, mileage: int,
    body_type: str = None, transmission: str = None, fuel_type: str = None,
) -> str:
    """
    Build an AutoTrader UK search URL.
    Year is EXACT (year-from == year-to) as required.
    Body type, transmission, and fuel type are included for precise matching.
    Mileage tolerance is ±10,000 miles.
    """
    p = {
        "make":     make.upper(),
        "model":    model.upper(),
        "postcode": "SW1A1AA",
        "radius":   "1500",
        "sort":     "relevance",
    }

    # Exact year — no range
    if year:
        p["year-from"] = str(year)
        p["year-to"]   = str(year)

    # Mileage ±10k
    if mileage:
        p["mileage-from"] = str(max(0, mileage - _MILE_TOLERANCE))
        p["mileage-to"]   = str(mileage + _MILE_TOLERANCE)

    # Fuel type
    if fuel_type:
        mapped = _FUEL_MAP.get(fuel_type.lower().strip())
        if mapped:
            p["fuel-type"] = mapped

    # Transmission
    if transmission:
        mapped = _TRANS_MAP.get(transmission.lower().strip())
        if mapped:
            p["transmission"] = mapped

    # Body type
    if body_type:
        mapped = _BODY_TYPE_MAP.get(body_type.lower().strip())
        if mapped:
            p["body-type"] = mapped

    return "https://www.autotrader.co.uk/car-search?" + urllib.parse.urlencode(p)


def search_autotrader(
    make: str,
    model: str,           # may be "911 Carrera S" — we strip to base model for URL
    year: str,
    mileage: int,
    body_type: str = None,
    fuel_type: str = None,
    transmission: str = None,
) -> dict:
    """
    Navigate AutoTrader UK directly with real Chrome, take a screenshot,
    and extract structured listing data where possible.

    Variant handling:
    - AutoTrader's model URL parameter only accepts the base model (e.g. "911"),
      not the full variant ("911 Carrera S").
    - We extract the variant keywords from the model string, search with the
      base model, then FILTER Python-side to only return listings that contain
      the variant in their title.
    - This gives precise results: e.g. "Carrera S" not "all 911s".
    """
    target_year = normalize_year(year)

    # Split base model from variant: "911 Carrera S" → base="911", variant="Carrera S"
    base_model, variant_keywords = _split_base_and_variant(make, model)

    url = build_search_url(make, base_model, target_year, mileage,
                           body_type, transmission, fuel_type)
    log.info("[AutoTrader] Search: %s %s (variant filter: '%s')",
             make, base_model, variant_keywords or "none")

    result = {
        "listings":         [],
        "averagePrice":     None,
        "lowestPrice":      None,
        "highestPrice":     None,
        "count":            0,
        "relaxedMatchUsed": False,
        "filter_label":     f"Year ±1 · Mileage ±{_MILE_TOLERANCE:,} mi",
        "search_url":       url,
        "source_used":      "AutoTrader UK",
        "screenshot":       None,   # base64 JPEG of the results page
        "screenshot_note":  None,
        "cloudflare_hit":   False,
        "warnings":         [],
        "scraped_at":       datetime.now(timezone.utc).isoformat(),
    }

    target = {
        "make": make, "model": base_model, "year": target_year,
        "mileage": mileage, "body_type": body_type,
        "fuel_type": fuel_type, "transmission": transmission,
        "variant_keywords": variant_keywords,   # exact variant PHRASE e.g. "Carrera S"
    }

    try:
        _run_playwright(url, target, result)
    except Exception as e:
        msg = f"AutoTrader error: {e}"
        log.error(msg)
        result["warnings"].append(msg)

    return result


# ── Playwright runner ────────────────────────────────────────────────────────


def _run_playwright(url: str, target: dict, result: dict) -> None:
    log.info("[AutoTrader] Launching browser for: %s", url)

    with sync_playwright() as p:
        # Try real Chrome first (if installed locally), fall back to bundled Chromium
        # On Railway/cloud: only bundled Chromium is available
        browser = None
        for attempt in [
            dict(channel="chrome",   headless=True),   # real Chrome (local dev)
            dict(channel=None,       headless=True,    # bundled Chromium (cloud/Railway)
                 args=["--disable-blink-features=AutomationControlled",
                        "--no-sandbox", "--disable-dev-shm-usage",
                        "--disable-gpu", "--window-size=1440,900"]),
        ]:
            try:
                ch = attempt.pop("channel", None)
                if ch:
                    browser = p.chromium.launch(channel=ch, **attempt)
                else:
                    browser = p.chromium.launch(**attempt)
                log.info("[AutoTrader] Browser launched (channel=%s headless=%s)",
                         ch, attempt.get("headless"))
                break
            except Exception as e:
                log.warning("[AutoTrader] Browser launch failed (%s): %s", ch, e)

        if not browser:
            result["warnings"].append("Could not launch any browser for AutoTrader.")
            return

        ctx = browser.new_context(
            locale="en-GB",
            timezone_id="Europe/London",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={"Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8"},
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script(_STEALTH_JS)

        try:
            # ── Load AutoTrader search page ────────────────────────────
            page.goto(url, timeout=35000, wait_until="domcontentloaded")

            # ── Wait out Cloudflare challenge (up to ~12 s) ─────────────
            cf_resolved = False
            for _ in range(12):
                page.wait_for_timeout(1000)
                title = page.title().lower()
                if "just a moment" not in title and "cloudflare" not in title:
                    cf_resolved = True
                    break
                log.info("[AutoTrader] Waiting for Cloudflare challenge…")

            if not cf_resolved:
                result["cloudflare_hit"] = True
                result["screenshot_note"] = (
                    "Cloudflare security check blocked this request — this happens after repeated "
                    "automated requests from the same IP. Click 'Open on AutoTrader' to view live results in your browser."
                )
                result["warnings"].append(
                    "AutoTrader Cloudflare: click 'View all results on AutoTrader →' to see live listings in your browser."
                )
                # Try Firecrawl as a fallback for listings data when Cloudflare blocks Playwright
                log.info("[AutoTrader] Cloudflare blocked — trying Firecrawl fallback for listings")
                fc = FirecrawlApp(api_key=_FC_KEY)
                fallback_url = build_motors_url(
                    target.get("make", ""),
                    target.get("model", "")
                )
                fb = _try_firecrawl(fc, fallback_url, "Motors.co.uk fallback", result, proxy="stealth")
                if fb:
                    result["source_used"] = "Motors.co.uk (Cloudflare fallback)"
                    result["search_url"] = fallback_url
                    _score_and_filter(fb, target, result)
            else:
                log.info("[AutoTrader] Past Cloudflare. Title: %s", page.title())

                # Give async scripts (incl. SourcePoint consent) time to load
                # AutoTrader's SP script typically fires 5-15s after page render
                page.wait_for_timeout(6000)

                # ── Try to click dismiss, then JS-nuke anything remaining ──
                _dismiss_autotrader_cookies(page)

                # Let listings render
                page.wait_for_timeout(2000)

                # Try to parse structured listing data from the rendered HTML
                html = page.content()
                raw_listings = _parse_at_html(html)
                if raw_listings:
                    _score_and_filter(raw_listings, target, result)

            # ── Force-remove any remaining overlays before screenshotting ──
            # AutoTrader's cookie banner loads up to 15s after page render.
            # We try to click it, then nuke any leftover overlay with JS.
            _force_remove_overlays(page)
            page.wait_for_timeout(500)

            # ── Screenshot ────────────────────────────────────────────────
            ss_bytes = page.screenshot(
                type="jpeg",
                quality=82,
                clip={"x": 0, "y": 0, "width": 1440, "height": 900},
            )
            result["screenshot"] = base64.b64encode(ss_bytes).decode("ascii")
            log.info("[AutoTrader] Screenshot captured (%d bytes)", len(ss_bytes))

        except PTE as e:
            result["warnings"].append(f"AutoTrader Playwright timeout: {e}")
            # Still attempt screenshot on timeout
            try:
                ss = page.screenshot(type="jpeg", quality=75,
                                     clip={"x":0,"y":0,"width":1440,"height":900})
                result["screenshot"] = base64.b64encode(ss).decode("ascii")
            except Exception:
                pass
        finally:
            browser.close()



def _force_remove_overlays(page) -> None:
    """
    Forcefully remove any cookie/consent overlay from the DOM using JavaScript.
    This is a last-resort clean-up that runs just before the screenshot to
    guarantee a clean image regardless of timing.
    """
    try:
        page.evaluate("""
            () => {
                const remove = (sel) => {
                    document.querySelectorAll(sel).forEach(el => {
                        el.style.display = 'none';
                        // Also try remove() in case display:none leaves a gap
                        try { el.remove(); } catch(_) {}
                    });
                };
                // Target cookie/consent dialogs by common patterns
                remove('[class*="CookieConsent"]');
                remove('[class*="cookie-banner"]');
                remove('[class*="cookie-policy"]');
                remove('[class*="cookieBanner"]');
                remove('[class*="cookie-notice"]');
                remove('[class*="consent-modal"]');
                remove('[class*="consent-banner"]');
                remove('[class*="gdpr"]');
                remove('[id*="cookie"]');
                remove('[id*="consent"]');
                remove('[id*="gdpr"]');
                // AutoTrader-specific: dialog with "We use cookies" heading
                document.querySelectorAll('div[role="dialog"]').forEach(el => {
                    if (el.innerText && el.innerText.toLowerCase().includes('cookie')) {
                        el.style.display = 'none';
                        try { el.remove(); } catch(_) {}
                    }
                });
                // Remove body scroll-lock that dialogs sometimes add
                document.body.style.overflow = 'auto';
                document.documentElement.style.overflow = 'auto';
            }
        """)
        log.info("[AutoTrader] Overlay removal JS executed")
    except Exception as e:
        log.warning("[AutoTrader] Overlay removal failed: %s", e)


def _dismiss_autotrader_cookies(page) -> None:
    """
    Dismiss AutoTrader's cookie consent overlay.

    AutoTrader uses SourcePoint (same as Carwow) — the consent buttons live
    inside an iframe with title 'SP Consent Message', NOT on the main page.
    We must use frame_locator() to reach them.
    """
    # Wait for the SourcePoint container to appear
    sp_container = 'div[id^="sp_message_container"]'
    try:
        page.wait_for_selector(sp_container, state="visible", timeout=12000)
        log.info("[AutoTrader] SourcePoint cookie banner detected")
    except PTE:
        log.info("[AutoTrader] No cookie banner detected — proceeding")
        return

    # The buttons are inside a SourcePoint iframe
    sp_iframe = page.frame_locator('iframe[title="SP Consent Message"]')
    clicked = False
    for btn_text in ["Accept All", "Accept all", "Accept All Cookies"]:
        try:
            sp_iframe.locator(f'button:has-text("{btn_text}")').first.click(timeout=4000)
            clicked = True
            log.info("[AutoTrader] SourcePoint cookie dismissed via iframe: '%s'", btn_text)
            break
        except PTE:
            continue

    if not clicked:
        # Fallback: try main-page buttons (some SourcePoint configs don't use iframe)
        for btn_text in ["Accept All", "Essential Cookies Only", "Reject All"]:
            try:
                page.locator(f'button:has-text("{btn_text}")').first.click(timeout=3000)
                clicked = True
                log.info("[AutoTrader] Cookie dismissed via main page: '%s'", btn_text)
                break
            except PTE:
                continue

    # Wait for the SourcePoint container to disappear
    try:
        page.wait_for_selector(sp_container, state="hidden", timeout=5000)
        log.info("[AutoTrader] Cookie banner hidden")
    except PTE:
        pass

    page.wait_for_timeout(800)


# ── HTML parser ─────────────────────────────────────────────────────────────


def _parse_at_html(html: str) -> list:
    """
    Parse AutoTrader search result HTML for listing cards.
    AutoTrader's HTML changes; we try multiple structural strategies.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: <article> with data-testid
    cards = soup.find_all("article", {"data-testid": True})

    # Strategy 2: <li> containing a price
    if not cards:
        cards = [li for li in soup.find_all("li")
                 if li.find(string=re.compile(r"£[\d,]+"))]

    # Strategy 3: divs with "listing" in class
    if not cards:
        cards = [d for d in soup.find_all(True)
                 if d.name in ("div","section")
                 and any("listing" in c for c in (d.get("class") or []))]

    results, seen = [], set()
    for card in cards[:30]:
        item = _extract_card(card)
        if not item:
            continue
        key = f"{item.get('title','')}_{item.get('price_num',0)}"
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


_NOISE = {"loading", "toggle pictures", "save advert", "reserved online",
          "great price", "good price", "fair price", "lower price",
          "reserve online", "private seller", "part exchange", "finance available"}

def _extract_card(card) -> dict | None:
    text = card.get_text(" ", strip=True)
    pm = re.search(r"£\s*([\d,]+)", text)
    if not pm:
        return None
    price_num = normalize_price(pm.group(1))
    if price_num < 500:
        return None

    ym = re.search(r"\b(19[89]\d|20[012]\d)\b", text)
    year = int(ym.group(0)) if ym else None

    mm = re.search(r"([\d,]+)\s*miles?", text, re.I)
    mileage = normalize_mileage(mm.group(1)) if mm else None

    # ── Smart title extraction ─────────────────────────────────────────────
    # Strategy 1: look for heading/title HTML elements
    title = ""
    for sel in ["h2", "h3", "h4",
                lambda t: t.get("class") and
                any("title" in c.lower() or "name" in c.lower() or "heading" in c.lower()
                    for c in (t.get("class") or []))]:
        try:
            el = card.find(sel)
            if el:
                t = el.get_text(" ", strip=True)
                if t and len(t) > 8 and "loading" not in t.lower():
                    title = t[:100]
                    break
        except Exception:
            pass

    # Strategy 2: first non-noise line of text
    if not title:
        lines = [l.strip() for l in text.split() if len(l.strip()) > 2]
        # Rebuild meaningful phrases — skip noise words
        clean_lines = []
        full_text = " ".join(lines)
        for line in re.split(r"  +|\n", card.get_text("  ", strip=True)):
            line = line.strip()
            if not line or len(line) < 5:
                continue
            if any(noise in line.lower() for noise in _NOISE):
                continue
            clean_lines.append(line)
        if clean_lines:
            title = clean_lines[0][:100]
        else:
            title = text[:80]

    fuel = next((f for f in ("Petrol","Diesel","Electric","Hybrid")
                 if f.lower() in text.lower()), None)

    trans = None
    if any(k in text.lower() for k in ("automatic","auto","pdk","dsg","cvt")):
        trans = "Automatic"
    elif "manual" in text.lower():
        trans = "Manual"

    link_el = card.find("a", href=re.compile(r"/car-details/"))
    link = ("https://www.autotrader.co.uk" + link_el["href"]
            if link_el else None)

    img_el = card.find("img", src=True)
    image = img_el["src"] if img_el else None

    return {
        "title":        title,
        "full_text":    text,          # full card text for variant phrase matching
        "price":        f"£{price_num:,}",
        "price_num":    price_num,
        "year":         year,
        "mileage":      mileage,
        "mileage_fmt":  f"{mileage:,} mi" if mileage else None,
        "fuel_type":    fuel,
        "transmission": trans,
        "link":         link,
        "image":        image,
    }


# ── Filter + score ────────────────────────────────────────────────────────


def _score_and_filter(raw: list, target: dict, result: dict) -> None:
    target_year      = target.get("year") or 0
    mileage          = target.get("mileage") or 0
    # ── Variant phrase from WBAC (exact phrase match required) ───────────────
    # The variant is a PHRASE e.g. "Carrera S" — ALL words must appear
    # consecutively in the listing title.  "Carrera 4S" or base "Carrera" won't match.
    variant_phrase = (target.get("variant_keywords") or "").strip().upper()

    def _title_matches_variant(title: str, full_text: str = "") -> bool:
        """
        PHRASE match — variant phrase must appear in the listing title OR full card text.
        Searching full text catches cases where the React title says 'Loading...'
        but the variant name appears elsewhere in the card (e.g. spec rows).
        """
        if not variant_phrase:
            return True
        return variant_phrase in title.upper() or variant_phrase in full_text.upper()

    def _filter(listings, yr_spread, mi_tol, require_variant=True):
        """Filter by year, mileage, and (optionally) exact variant phrase."""
        out, relaxed = [], False
        for l in listings:
            ly = l.get("year") or 0
            lm = l.get("mileage") or 0
            # Strict variant phrase check — also searches full card text
            if require_variant and variant_phrase:
                if not _title_matches_variant(l.get("title", ""), l.get("full_text", "")):
                    continue
            if lm and mileage and abs(lm - mileage) > mi_tol:
                continue
            if target_year and ly:
                diff = abs(ly - target_year)
                if diff > yr_spread:
                    continue
                if diff > 0:
                    relaxed = True
            out.append(l)
        return out, relaxed

    vl = f" · {variant_phrase.title()}" if variant_phrase else ""

    # Tier 1: exact year + exact variant phrase + ±10k miles
    candidates, relaxed = _filter(raw, 0, _MILE_TOLERANCE, require_variant=True)
    label = f"Exact year {target_year} · Exact variant{vl} · Mileage ±{_MILE_TOLERANCE:,} mi"

    if len(candidates) < 2:
        # Tier 2: ±1 year + exact variant
        candidates, relaxed = _filter(raw, 1, _MILE_TOLERANCE, require_variant=True)
        label = f"Year ±1 · Exact variant{vl} · Mileage ±{_MILE_TOLERANCE:,} mi"

    if len(candidates) < 2:
        # Tier 3: ±2 years + exact variant + wider mileage
        candidates, relaxed = _filter(raw, 2, _MILE_TOLERANCE * 2, require_variant=True)
        label = f"Year ±2 · Exact variant{vl} · Mileage ±20,000 mi"

    if not candidates and variant_phrase:
        # Tier 4: RELAX variant — show all year/mileage matches regardless of variant
        # This is a last resort — clearly labelled as relaxed
        candidates, relaxed = _filter(raw, 1, _MILE_TOLERANCE, require_variant=False)
        label = f"⚠️ No exact {variant_phrase.title()} found — showing all 911 (year ±1 · mileage ±{_MILE_TOLERANCE:,} mi)"

    if not candidates:
        candidates, relaxed = raw, True
        label = "No matching listings found for this spec"

    scored = []
    for l in candidates:
        m = compute_confidence(target, l)
        vm = _title_matches_variant(l.get("title", ""), l.get("full_text", ""))
        l.update(
            matchConfidence  = m["confidence"],
            matchScore       = m["score"],
            matchReasons     = m["reasons"],
            relaxedYear      = m["relaxed_year"],
            variantMatch     = vm,
            variantMatchLabel= "✅ Exact match" if vm else "⚠️ Variant not confirmed",
        )
        scored.append(l)
    scored.sort(key=lambda x: x["matchScore"], reverse=True)

    prices = [l["price_num"] for l in scored if l.get("price_num")]
    stats  = price_stats(prices)
    result.update(
        listings=scored, count=len(scored),
        averagePrice=stats.get("average"),
        lowestPrice=stats.get("lowest"),
        highestPrice=stats.get("highest"),
        relaxedMatchUsed=relaxed,
        filter_label=label,
    )
