"""
Motorway.co.uk valuation scraper — full Playwright flow.

Discovered flow (from live testing with FG63ACY):
  1. Navigate to /car-value-tracker/{REG}?mileage={MILEAGE}
     → Motorway immediately redirects to auth.motorway.co.uk/seller/ with
       the vehicle details pre-loaded (skips the mileage page for known cars)
  2. auth.motorway.co.uk/seller/ — two-step sign-up:
     a. Email input + Continue
     b. First name, Last name, Mobile, Postcode + "See valuation"
  3. Redirected back to motorway.co.uk with the valuation shown
"""
import re
import base64
import logging
import json
import os
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PTE

_SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "motorway_session.json")

log = logging.getLogger(__name__)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_EMAIL     = "samroid78@gmail.com"
_FIRSTNAME = "Sam"
_LASTNAME  = "Sharma"
_MOBILE    = "07863239691"
_POSTCODE  = "E181BT"


def get_motorway_valuation(reg: str, mileage: int) -> dict:
    result = {
        "valuation":      None,
        "valuation_num":  None,
        "valuationRange": {},
        "description":    None,
        "make":           None,
        "model":          None,
        "body_type":      None,
        "fuel_type":      None,
        "colour":         None,
        "year":           None,
        "transmission":   None,
        "sourceUrl":      "https://motorway.co.uk/car-value-tracker",
        "screenshot":     None,
        "assumptions":    [f"Reg: {reg}", f"Mileage: {mileage:,} miles"],
        "warnings":       [],
        "scraped_at":     datetime.now(timezone.utc).isoformat(),
    }

    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in [
                {"channel": "chrome", "headless": True},     # local dev (Chrome installed)
                {"headless": True, "args": STEALTH_ARGS},    # Railway/cloud (bundled Chromium)
            ]:
                try:
                    browser = p.chromium.launch(**kwargs)
                    break
                except Exception:
                    pass
            if not browser:
                result["warnings"].append("Could not launch browser.")
                return result

            # Try to reuse a saved session (avoids the full sign-up flow)
            saved_session = _load_session()
            ctx_kwargs = {
                "locale": "en-GB",
                "timezone_id": "Europe/London",
                "user_agent": UA,
                "extra_http_headers": {"Accept-Language": "en-GB,en;q=0.9"},
                "viewport": {"width": 1440, "height": 900},
            }
            if saved_session:
                ctx_kwargs["storage_state"] = saved_session
                log.info("[Motorway] Reusing saved session")

            ctx = browser.new_context(**ctx_kwargs)

            def _new_page():
                p = ctx.new_page()
                p.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')
                return p

            # ── Fast path: if we have a saved session, go direct to valuation URL ──
            if saved_session:
                reg_clean_fp = reg.upper().replace(" ", "")
                direct_val_url = f"https://motorway.co.uk/car-value-tracker/{reg_clean_fp}?mileage={mileage}"
                log.info("[Motorway] Trying direct URL with saved session: %s", direct_val_url)
                fp = _new_page()
                try:
                    fp.goto(direct_val_url, timeout=30000)
                    fp.wait_for_load_state("domcontentloaded", timeout=15000)
                    fp.wait_for_timeout(3000)
                    fast_text = fp.inner_text("body")
                    log.info("[Motorway] Fast path URL: %s", fp.url)
                    if "valuation" in fast_text.lower() and "/car-value-tracker/" in fp.url:
                        result.update(_parse_vehicle_text(fast_text))
                        _extract_valuation(fast_text, result)
                        result["sourceUrl"] = fp.url
                        if result.get("valuation"):
                            log.info("[Motorway] Fast path succeeded: %s", result["valuation"])
                            try:
                                ss = fp.screenshot(type="jpeg", quality=85,
                                                   clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                                result["screenshot"] = base64.b64encode(ss).decode("ascii")
                            except Exception:
                                pass
                            browser.close()
                            return result
                        log.info("[Motorway] Fast path: page loaded but no valuation — full flow needed")
                    else:
                        log.info("[Motorway] Fast path: not on valuation page — full flow needed. Clearing stale session.")
                        _clear_session()
                except Exception as e:
                    log.warning("[Motorway] Fast path error: %s — falling through to full flow", e)
                finally:
                    fp.close()

            # ── Main flow: homepage → reg → mileage page → sign-up → valuation ──
            page = _new_page()
            log.info("[Motorway] Loading homepage for reg=%s", reg)
            page.goto("https://motorway.co.uk/car-value-tracker", timeout=35000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2500)

            # Cookie consent
            for btn in ["Accept all", "Accept"]:
                try:
                    page.locator(f'button:has-text("{btn}")').first.click(timeout=2000)
                    page.wait_for_timeout(500)
                    break
                except PTE:
                    pass

            # Enter reg and submit — try multiple approaches for reliability
            reg_clean = reg.upper().replace(" ", "")
            reg_inp = page.locator('[name="vrm-input"], input[placeholder*="reg"], input[placeholder*="Reg"]').first
            reg_inp.fill(reg_clean)
            page.wait_for_timeout(600)

            # Try clicking the submit button first, then fall back to Enter
            submitted = False
            for sub_sel in ['input[type="submit"]', 'button[type="submit"]']:
                try:
                    sub = page.locator(sub_sel).first
                    if sub.is_visible(timeout=2000):
                        sub.click()
                        submitted = True
                        log.info("[Motorway] Submitted via %s", sub_sel)
                        break
                except PTE:
                    pass
            if not submitted:
                reg_inp.press("Enter")
                log.info("[Motorway] Submitted via Enter key")

            log.info("[Motorway] Reg submitted — waiting for mileage page (up to 18s)...")

            # Wait for the "Confirm mileage" button — Motorway's SPA typically takes 5-15s
            mileage_page_loaded = False
            try:
                page.wait_for_selector('button:has-text("Confirm mileage")', timeout=18000)
                page.wait_for_timeout(600)
                mileage_page_loaded = True
                log.info("[Motorway] Mileage page ready at: %s", page.url)
            except PTE:
                log.warning("[Motorway] Mileage page not loaded. URL: %s", page.url)
                result["warnings"].append(
                    "Motorway: vehicle lookup page did not load — "
                    "this can happen due to bot detection after repeated requests. "
                    "Try again in a few minutes with a different registration."
                )

            # Only extract vehicle data if we're on the mileage page (not the homepage)
            # The homepage has nav items like 'Authors', 'Guides', '2017' which cause wrong results
            if mileage_page_loaded and "/mileage" in page.url:
                page_text = page.inner_text("body")
                result.update(_parse_vehicle_text(page_text))
                log.info("[Motorway] Vehicle: %s", result.get("description"))

            # ── Step 2: update mileage and confirm (only if mileage page loaded) ────
            if not mileage_page_loaded:
                # Take screenshot of whatever page we're on for debugging
                try:
                    ss = page.screenshot(type="jpeg", quality=75,
                                         clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                    result["screenshot"] = base64.b64encode(ss).decode("ascii")
                except Exception:
                    pass
                browser.close()
                return result

            try:
                mile_inp = page.locator("input").first
                mile_inp.click(click_count=3)
                mile_inp.fill(str(mileage))
                page.wait_for_timeout(400)
                log.info("[Motorway] Mileage updated to %d", mileage)
            except PTE:
                log.warning("[Motorway] Could not update mileage input")

            page.locator('button:has-text("Confirm mileage")').first.click(timeout=8000)
            page.wait_for_timeout(2000)
            log.info("[Motorway] Clicked Confirm mileage — checking for modal at %s...", page.url)

            # Motorway shows a high-mileage warning modal: "Continue" or "Cancel"
            try:
                page.wait_for_selector('button:has-text("Continue")', timeout=5000)
                page.locator('button:has-text("Continue")').first.click(timeout=5000)
                log.info("[Motorway] High-mileage modal confirmed via Continue")
                page.wait_for_timeout(2000)
            except PTE:
                log.info("[Motorway] No high-mileage modal — proceeding")

            log.info("[Motorway] After mileage confirm: %s", page.url)

            # ── Step 3: sign-up form (auth.motorway.co.uk) ────────────────
            # Motorway redirects to auth.motorway.co.uk/seller/?...
            # Form is two-step:
            #   3a. Email input + Continue
            #   3b. First name, Last name, Mobile, Postcode + "See valuation"

            def _type(sel, val, timeout=5000):
                """Fill input, waiting for it to appear first."""
                try:
                    el = page.locator(sel).first
                    el.wait_for(state="visible", timeout=timeout)
                    el.click()
                    page.wait_for_timeout(80)
                    el.press("Control+a")
                    el.press("Delete")
                    el.type(val, delay=20)
                    page.wait_for_timeout(120)
                    return True
                except PTE:
                    return False

            # Step 3a: wait explicitly for the email input, then fill it
            log.info("[Motorway] Waiting for email input at %s...", page.url)
            try:
                page.wait_for_selector('[type="email"], input[placeholder*="Email"]', timeout=15000)
                page.wait_for_timeout(500)
            except PTE:
                log.warning("[Motorway] Email input not found at %s", page.url)

            email_filled = _type('[type="email"], input[placeholder*="Email"]', _EMAIL, timeout=5000)
            log.info("[Motorway] Email filled: %s at %s", email_filled, page.url)

            if email_filled:
                page.wait_for_timeout(300)
                page.locator('button:has-text("Continue")').first.click(timeout=5000)
                page.wait_for_timeout(3000)
                log.info("[Motorway] Email Continue → %s", page.url)

            # Step 3b: full details form — fields use LABELS not placeholders.
            # Use get_by_role with accessible name (most reliable for React forms).
            def _fill_by_label(label_text, value):
                """Fill input found by its label text."""
                try:
                    el = page.get_by_role("textbox", name=label_text)
                    if el.count() > 0 and el.first.is_visible(timeout=3000):
                        el.first.click()
                        page.wait_for_timeout(80)
                        el.first.press("Control+a")
                        el.first.press("Delete")
                        el.first.type(value, delay=20)
                        page.wait_for_timeout(120)
                        log.info("[Motorway] Filled '%s'", label_text)
                        return True
                except PTE:
                    pass
                return False

            _fill_by_label("First name",    _FIRSTNAME)
            _fill_by_label("Last name",     _LASTNAME)
            _fill_by_label("Mobile number", _MOBILE)    # also try tel input
            _type('[type="tel"]', _MOBILE)               # backup
            _fill_by_label("Postcode",      _POSTCODE)
            page.wait_for_timeout(500)

            submitted = False
            for btn_text in ["See valuation", "Get valuation"]:
                try:
                    b = page.locator(f'button:has-text("{btn_text}")').first
                    b.wait_for(state="visible", timeout=3000)
                    b.click(timeout=6000)
                    log.info("[Motorway] Submitted via '%s'", btn_text)
                    page.wait_for_timeout(8000)
                    submitted = True
                    break
                except PTE:
                    continue

            if not submitted:
                log.warning("[Motorway] Could not find See/Get valuation button")

            # ── Step 3: extract valuation ─────────────────────────────────
            result["sourceUrl"] = page.url
            val_text = page.inner_text("body")
            log.info("[Motorway] Result page: %s | snippet: %s", page.url, val_text[:200])

            _extract_valuation(val_text, result)

            # Save session cookies after successful valuation for future reuse
            if result.get("valuation"):
                try:
                    ctx.storage_state(path=_SESSION_FILE)
                    log.info("[Motorway] Session saved for future reuse")
                except Exception as e:
                    log.warning("[Motorway] Could not save session: %s", e)

            # Screenshot
            try:
                ss = page.screenshot(type="jpeg", quality=85,
                                     clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                result["screenshot"] = base64.b64encode(ss).decode("ascii")
                log.info("[Motorway] Screenshot captured (%d bytes)", len(ss))
            except Exception as e:
                log.warning("[Motorway] Screenshot failed: %s", e)

            browser.close()

    except Exception as e:
        msg = f"Motorway error: {e}"
        log.error(msg)
        result["warnings"].append(msg)

    return result


def complete_motorway_magic_link(magic_link_url: str) -> dict:
    """
    Follow a Motorway magic link from the sign-in email.
    The link signs the user in and redirects to the car valuation page.
    Called from /api/verify-motorway endpoint.
    """
    result = {
        "valuation": None, "valuation_num": None, "valuationRange": {},
        "description": None, "make": None, "model": None,
        "body_type": None, "fuel_type": None, "colour": None, "year": None,
        "sourceUrl": magic_link_url, "screenshot": None, "warnings": [],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        with sync_playwright() as p:
            browser = None
            for kwargs in [
                {"channel": "chrome", "headless": True},
                {"headless": True, "args": STEALTH_ARGS},
            ]:
                try:
                    browser = p.chromium.launch(**kwargs)
                    break
                except Exception:
                    pass
            if not browser:
                result["warnings"].append("Could not launch browser.")
                return result

            ctx  = browser.new_context(
                locale="en-GB", timezone_id="Europe/London",
                user_agent=UA,
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            page.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')

            log.info("[Motorway magic link] Navigating to: %s", magic_link_url[:80])
            page.goto(magic_link_url, timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(4000)

            log.info("[Motorway magic link] Landed at: %s", page.url)
            val_text = page.inner_text("body")

            # Extract valuation
            result.update(_parse_vehicle_text(val_text))
            _extract_valuation(val_text, result)
            result["sourceUrl"] = page.url

            if result.get("valuation"):
                log.info("[Motorway magic link] Valuation: %s", result["valuation"])
                # Save session for future fast-path reuse
                try:
                    ctx.storage_state(path=_SESSION_FILE)
                    log.info("[Motorway magic link] Session saved")
                except Exception:
                    pass
            else:
                # Check if still on sign-in page
                if "sign in" in val_text.lower() or "log in" in val_text.lower():
                    result["warnings"].append(
                        "Magic link did not sign in — it may have expired. "
                        "Request a new link from Motorway."
                    )
                else:
                    result["warnings"].append(
                        "Signed in but valuation not found on this page."
                    )

            try:
                ss = page.screenshot(type="jpeg", quality=85,
                                     clip={"x":0,"y":0,"width":1440,"height":900})
                result["screenshot"] = base64.b64encode(ss).decode("ascii")
            except Exception:
                pass

            browser.close()

    except Exception as e:
        result["warnings"].append(f"Motorway magic link error: {e}")

    return result


# ── Session helpers ──────────────────────────────────────────────────────────


def _load_session() -> dict | None:
    """Load saved Motorway session cookies if available."""
    try:
        if os.path.exists(_SESSION_FILE):
            with open(_SESSION_FILE) as f:
                data = json.load(f)
            # Check cookies are not empty
            if data.get("cookies"):
                log.info("[Motorway] Loaded saved session (%d cookies)", len(data["cookies"]))
                return data
    except Exception as e:
        log.warning("[Motorway] Could not load saved session: %s", e)
    return None


def _clear_session():
    """Remove saved session (call if session appears stale)."""
    try:
        if os.path.exists(_SESSION_FILE):
            os.remove(_SESSION_FILE)
            log.info("[Motorway] Cleared stale session")
    except Exception:
        pass


# ── Parsers ──────────────────────────────────────────────────────────────────


def _parse_vehicle_text(text: str) -> dict:
    """
    Parse vehicle details shown on the Motorway sign-up page.
    Motorway shows the model as a heading, e.g. 'Porsche 911 Carrera S S-A',
    then: FG63 ACY · 2013 · 38,469 miles · Silver · Convertible · Petrol
    """
    data = {}
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Find the vehicle model heading — a meaningful multi-word line containing a make
    known_makes = {"Porsche", "BMW", "Mercedes", "Audi", "Volkswagen", "Ford", "Toyota",
                   "Honda", "Nissan", "Vauxhall", "Peugeot", "Renault", "Citroën",
                   "Kia", "Hyundai", "Mazda", "Lexus", "Jaguar", "Land Rover",
                   "Range Rover", "Volvo", "Skoda", "Seat", "Tesla", "Fiat", "Mini"}

    for i, line in enumerate(lines):
        first_word = line.split()[0] if line.split() else ""
        if first_word in known_makes and len(line) > len(first_word) + 2:
            data["description"] = line
            parts = line.split(None, 1)
            data["make"]  = parts[0]
            data["model"] = parts[1].strip() if len(parts) > 1 else ""
            break

    # Parse spec items from surrounding lines
    spec_text = " ".join(lines)
    # Year
    ym = re.search(r"\b(19[89]\d|20[012]\d)\b", spec_text)
    if ym:
        data["year"] = ym.group(0)
    # Fuel
    for f in ("Petrol", "Diesel", "Electric", "Hybrid"):
        if f.lower() in spec_text.lower():
            data["fuel_type"] = f
            break
    # Body type
    for b in ("Convertible", "Cabriolet", "Coupe", "Hatchback", "Saloon", "Estate", "SUV"):
        if b.lower() in spec_text.lower():
            data["body_type"] = b
            break
    # Colour — look for capitalised single word between bullets or on its own line
    for line in lines:
        if re.match(r"^[A-Z][a-z]{2,14}$", line):
            if line not in ("Petrol", "Diesel", "Electric", "Hybrid", "Manual",
                            "Automatic", "Convertible", "Cabriolet", "Coupe",
                            "Hatchback", "Saloon", "Estate", "Motorway", "Porsche",
                            "Value", "Your", "Help", "Tools", "More", "Silver",
                            "Black", "White", "Blue", "Red", "Grey", "Green"):
                if not data.get("colour"):
                    data["colour"] = line
            elif line in ("Silver", "Black", "White", "Blue", "Red", "Grey", "Green"):
                data["colour"] = line

    return data


def _extract_valuation(text: str, result: dict) -> None:
    """
    Extract the HEADLINE valuation price from the Motorway results page.

    The page shows e.g.:
      "Your latest valuation   £52,900"
      "2 year change  -£7,361 (-16.16%)"  ← this is NOT the valuation

    Strategy: search for £ amount that immediately follows valuation-related text.
    If not found, fall back to the LARGEST price >= £5,000 (the change is smaller).
    """
    # Strategy 1: price immediately after "valuation" keyword
    m = re.search(r"(?:latest\s+valuation|your\s+valuation)\s*[^\d£]*£\s*([\d,]+)",
                  text, re.IGNORECASE)
    if m:
        price = int(m.group(1).replace(",", ""))
        if price >= 1_000:
            result["valuation"]     = f"£{price:,}"
            result["valuation_num"] = price
            result["confidence"]    = "High"
            log.info("[Motorway] Valuation (keyword match): £%s", f"{price:,}")
            return

    # Strategy 2: all prices >= £5,000 sorted desc — the largest is most likely the valuation
    prices = re.findall(r"£\s*([\d]{1,3}(?:,\d{3})+|[\d]{4,6})", text)
    candidates = sorted(set(
        int(p.replace(",", "")) for p in prices
        if int(p.replace(",", "")) >= 5_000
    ), reverse=True)

    if candidates:
        price = candidates[0]
        result["valuation"]     = f"£{price:,}"
        result["valuation_num"] = price
        result["confidence"]    = "High"
        log.info("[Motorway] Valuation (largest price): £%s", f"{price:,}")
        # If there are also chart axis prices (multiples like £32,000, £40,000),
        # try to find a non-round number as the actual estimate
        non_round = [p for p in candidates if p % 1000 != 0]
        if non_round:
            price = non_round[0]
            result["valuation"]     = f"£{price:,}"
            result["valuation_num"] = price
            log.info("[Motorway] Valuation (non-round preferred): £%s", f"{price:,}")
        return

    result["warnings"].append(
        "Motorway valuation not visible on result page."
    )
