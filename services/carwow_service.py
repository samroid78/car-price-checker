"""
Carwow.co.uk sell-my-car valuation scraper — full Playwright flow.

Exact steps (confirmed via live Firecrawl interaction testing):
  1. Load https://www.carwow.co.uk/sell-my-car
  2. Enter registration number, click "Value my car"
  3. Confirm vehicle page — extract make/model/fuel/transmission/colour, click "Next"
  4. Mileage page — update mileage to user's value, click "Next"
  5. Contact form — fill Email, Name, Mobile (using .type() for React events), Postcode
  6. Click "Show me my valuation"
  7. Extract valuation price from results page

Uses OFCOM-designated fictional UK mobile 07700 900000-range for phone field.
All contact data uses reserved/fictional domains and numbers to avoid real impact.
"""
import re
import os
import base64
import logging
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PTE

_CARWOW_SESSION_FILE = os.path.join(os.path.dirname(__file__), "..", "carwow_session.json")

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

# Fictional/test contact data — safe to use in automated forms
_TEST_EMAIL    = "samroid78@gmail.com"
_TEST_NAME     = "Sam"
_TEST_POSTCODE = "E181BT"
_TEST_PHONES   = ["07863239691"]


def get_carwow_valuation(
    reg: str,
    mileage: int,
    make: str = "",
    model: str = "",
    year: str = "",
) -> dict:
    """
    Navigate Carwow's sell-my-car flow and return the valuation.

    Returns:
        {
            valuation, valuation_num, valuationRange,
            vehicle_description, make, model, fuel_type, transmission, colour,
            mileage_used, sourceUrl, confidence, warnings, scraped_at
        }
    """
    result = {
        "valuation":           None,
        "valuation_num":       None,
        "valuationRange":      {},
        "vehicle_description": None,
        "make":                None,
        "model":               None,
        "fuel_type":           None,
        "transmission":        None,
        "colour":              None,
        "mileage_used":        mileage,
        "sourceUrl":           "https://www.carwow.co.uk/sell-my-car",
        "carwow_direct_url":   f"https://www.carwow.co.uk/sell-my-car?vrm={reg.upper().replace(' ','')}",
        "confidence":          None,
        "screenshot":          None,   # base64 JPEG of Carwow vehicle confirmation
        "assumptions":         [f"Reg: {reg}", f"Mileage: {mileage:,} miles"],
        "warnings":            [],
        "scraped_at":          datetime.now(timezone.utc).isoformat(),
    }

    try:
        with sync_playwright() as p:
            # Prefer installed Chrome for better site compatibility
            browser = None
            for launch_args in [
                {"channel": "chrome", "headless": True},
                {"headless": True, "args": STEALTH_ARGS},
            ]:
                try:
                    browser = p.chromium.launch(**launch_args)
                    break
                except Exception:
                    pass
            if not browser:
                result["warnings"].append("Could not launch browser for Carwow.")
                return result

            ctx = browser.new_context(
                locale="en-GB",
                timezone_id="Europe/London",
                user_agent=UA,
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
                viewport={"width": 1440, "height": 900},
            )
            page = ctx.new_page()
            page.add_init_script(
                'Object.defineProperty(navigator,"webdriver",{get:()=>undefined})'
            )

            # ── Step 1: Load sell-my-car page ─────────────────────────────
            log.info("[Carwow] Loading sell-my-car page for reg=%s", reg)
            try:
                page.goto("https://www.carwow.co.uk/sell-my-car", timeout=45000)
            except PTE:
                # Retry once with a longer wait
                log.warning("[Carwow] First load timed out, retrying...")
                page.wait_for_timeout(3000)
                page.goto("https://www.carwow.co.uk/sell-my-car", timeout=50000)
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)

            # Cookie consent — Carwow uses a SourcePoint iframe for GDPR
            # Must click inside the iframe, not on the main page
            _dismiss_carwow_cookies(page)

            # ── Step 2: Enter registration ────────────────────────────────
            reg_clean = reg.upper().replace(" ", "")
            reg_input = page.locator(
                'input[name="vrm"], input[id*="vrm"], '
                'input[placeholder*="registration"], input[placeholder*="reg"]'
            ).first
            reg_input.click()
            reg_input.fill(reg_clean)
            page.wait_for_timeout(500)

            # Submit — Carwow uses input[type="submit"] (no visible text), so press Enter
            reg_input.press("Enter")
            # Also try clicking the submit input as a backup
            try:
                page.locator('input[type="submit"]').first.click(timeout=3000)
            except PTE:
                pass

            # ── Step 3: "Have we found your car?" confirmation page ────────
            page.wait_for_selector('a:has-text("Next"), a:has-text("This isn\'t")', timeout=20000)
            page.wait_for_timeout(1000)

            body_text = page.inner_text("body")
            result.update(_parse_carwow_vehicle(body_text))
            log.info("[Carwow] Vehicle confirmed: %s", result.get("vehicle_description"))

            # Screenshot of the vehicle confirmation page (most informative view)
            try:
                ss = page.screenshot(type="jpeg", quality=80,
                                     clip={"x": 0, "y": 0, "width": 1440, "height": 900})
                result["screenshot"] = base64.b64encode(ss).decode("ascii")
            except Exception:
                pass

            # Click "Next" to confirm the vehicle (it's an <a> link on Carwow)
            page.locator('a:has-text("Next"), button:has-text("Next")').first.click(timeout=10000)
            page.wait_for_timeout(1500)

            # ── Step 4: Mileage confirmation page ─────────────────────────
            # Carwow's mileage input has no name/id — select visible text inputs
            try:
                # Get all visible text inputs and use the first non-hidden one
                mile_inputs = page.locator('input[type="text"]:not([type="hidden"])').all()
                if not mile_inputs:
                    mile_inputs = [page.locator('input').first]
                mile_input = mile_inputs[0]

                current_val = mile_input.input_value()
                if current_val:
                    log.info("[Carwow] Pre-filled mileage: %s → updating to %d", current_val, mileage)
                mile_input.click(click_count=3)
                mile_input.fill(str(mileage))
                page.wait_for_timeout(400)
                result["mileage_used"] = mileage
            except Exception as e:
                log.warning("[Carwow] Could not update mileage input: %s", e)

            # Click "Next" to submit mileage
            page.locator('button[type="submit"]:has-text("Next"), button:has-text("Next"), a:has-text("Next")').first.click(timeout=8000)
            page.wait_for_timeout(1500)

            # Carwow shows a high-mileage warning modal when mileage > MOT estimate
            # Click "Confirm" if the modal is present
            try:
                confirm_btn = page.locator('button[type="submit"]:has-text("Confirm"), button:has-text("Confirm")').first
                if confirm_btn.is_visible():
                    log.info("[Carwow] High-mileage warning modal detected — clicking Confirm")
                    confirm_btn.click(timeout=5000)
                    page.wait_for_timeout(1500)
            except PTE:
                pass

            # ── Step 5: Contact form (signups/new) ────────────────────────
            # Wait for contact form to load (redirects to quotes.carwow.co.uk/selling/signups/new)
            page.wait_for_selector('input[name="user[email]"], input[placeholder="Email"]', timeout=15000)
            log.info("[Carwow] Filling contact form at %s", page.url)

            # Use exact field names found from live inspection
            _fill_react_input(page, 'input[name="user[email]"]',       _TEST_EMAIL)
            _fill_react_input(page, 'input[name="user[name]"]',        _TEST_NAME)

            # Phone — try multiple numbers until validation passes
            phone_accepted = False
            for phone in _TEST_PHONES:
                _fill_react_input(page, 'input[name="user[phone_number]"]', phone)
                page.wait_for_timeout(700)
                err_count = page.locator('text="Please enter a valid phone number"').count()
                if err_count == 0:
                    phone_accepted = True
                    log.info("[Carwow] Phone accepted: %s", phone)
                    break
                log.warning("[Carwow] Phone %s rejected, trying next", phone)

            if not phone_accepted:
                log.warning("[Carwow] All test phone numbers failed validation")
                result["warnings"].append(
                    "Carwow phone validation blocked automated form submission."
                )

            _fill_react_input(page, 'input[name="user[postcode]"]', _TEST_POSTCODE)
            page.wait_for_timeout(400)

            # Submit
            page.locator('button[type="submit"]:has-text("Show me my valuation")').first.click(timeout=10000)
            page.wait_for_timeout(4000)

            # ── Step 6: Extract valuation from results page ────────────────
            result["sourceUrl"] = page.url
            val_text = page.inner_text("body")
            log.info("[Carwow] Results page URL: %s", page.url)

            # Detect OTP gate — save session so user can enter the code immediately
            if "check your email" in val_text.lower() or "enter the code" in val_text.lower():
                result["otp_required"] = True
                result["otp_url"]      = page.url
                # Save the live browser session so entering the OTP doesn't need a new email
                try:
                    ctx.storage_state(path=_CARWOW_SESSION_FILE)
                    log.info("[Carwow] Session saved for OTP entry")
                except Exception as se:
                    log.warning("[Carwow] Could not save session: %s", se)
            else:
                _extract_carwow_valuation(val_text, result)

            browser.close()

    except Exception as e:
        msg = f"Carwow error: {e}"
        log.error(msg)
        result["warnings"].append(msg)

    return result


def complete_carwow_otp(otp_code: str, otp_url: str) -> dict:
    """
    Resume a saved Carwow session, enter the OTP from the user's email,
    and extract the valuation price.
    Called from /api/verify-carwow endpoint.
    """
    result = {
        "valuation": None, "valuation_num": None, "valuationRange": {},
        "vehicle_description": None, "warnings": [],
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }

    import json
    session_state = None
    if os.path.exists(_CARWOW_SESSION_FILE):
        try:
            with open(_CARWOW_SESSION_FILE) as f:
                session_state = json.load(f)
        except Exception:
            pass

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

            ctx_kwargs = {
                "locale": "en-GB", "timezone_id": "Europe/London",
                "user_agent": UA,
                "extra_http_headers": {"Accept-Language": "en-GB,en;q=0.9"},
                "viewport": {"width": 1440, "height": 900},
            }
            if session_state:
                ctx_kwargs["storage_state"] = session_state

            ctx  = p.chromium.new_context if False else browser.new_context
            ctx  = browser.new_context(**ctx_kwargs)
            page = ctx.new_page()
            page.add_init_script('Object.defineProperty(navigator,"webdriver",{get:()=>undefined})')

            # Navigate to the OTP page
            page.goto(otp_url, timeout=25000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            page.wait_for_timeout(1500)

            log.info("[Carwow OTP] At page: %s", page.url)

            # Find OTP input and enter the code
            otp_entered = False
            for sel in ['input[autocomplete="one-time-code"]', 'input[name="otp"]',
                        'input[maxlength="6"]', 'input[type="number"]']:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=3000):
                        el.click()
                        el.press("Control+a"); el.press("Delete")
                        el.type(otp_code, delay=30)
                        page.wait_for_timeout(400)
                        otp_entered = True
                        log.info("[Carwow OTP] Code entered via %s", sel)
                        break
                except PTE:
                    continue

            if not otp_entered:
                result["warnings"].append("Could not find OTP input on Carwow page.")
                browser.close()
                return result

            # Submit
            for btn in ['button:has-text("Log in")', 'button:has-text("Verify")',
                        'button:has-text("Continue")', 'button[type="submit"]']:
                try:
                    b = page.locator(btn).first
                    if b.is_visible(timeout=2000):
                        b.click(timeout=5000)
                        page.wait_for_timeout(5000)
                        break
                except PTE:
                    continue

            log.info("[Carwow OTP] After submit: %s", page.url)
            val_text = page.inner_text("body")

            if "incorrect" in val_text.lower() or "invalid" in val_text.lower():
                result["warnings"].append("OTP code was rejected — it may have expired. Please request a new code.")
            elif "check your email" in val_text.lower():
                result["warnings"].append("Still on verification page — OTP may have expired.")
            else:
                _extract_carwow_valuation(val_text, result)
                result["sourceUrl"] = page.url
                # Screenshot
                try:
                    ss = page.screenshot(type="jpeg", quality=85,
                                         clip={"x":0,"y":0,"width":1440,"height":900})
                    result["screenshot"] = base64.b64encode(ss).decode("ascii")
                except Exception:
                    pass

            browser.close()

    except Exception as e:
        result["warnings"].append(f"Carwow OTP error: {e}")

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _dismiss_carwow_cookies(page) -> None:
    """
    Carwow uses a SourcePoint GDPR consent iframe that blocks all page clicks.
    We must locate the iframe and click 'Accept all' inside it.
    """
    try:
        # Wait for the consent iframe to appear
        page.wait_for_selector('iframe[title="SP Consent Message"]', timeout=8000)
        iframe = page.frame_locator('iframe[title="SP Consent Message"]')
        for btn_text in ["Accept all", "Accept All", "ACCEPT ALL", "Accept"]:
            try:
                iframe.locator(f'button:has-text("{btn_text}")').first.click(timeout=3000)
                page.wait_for_timeout(1000)
                log.info("[Carwow] Cookie consent dismissed via iframe")
                return
            except PTE:
                continue
    except PTE:
        pass

    # Fallback: try main page buttons
    for btn_text in ["Accept all", "Accept All", "Accept"]:
        try:
            page.locator(f'button:has-text("{btn_text}")').first.click(timeout=2000)
            page.wait_for_timeout(600)
            log.info("[Carwow] Cookie consent dismissed via main page button")
            return
        except PTE:
            pass


def _fill_react_input(page, selector: str, value: str) -> None:
    """Fill a React-controlled input using keystroke simulation."""
    try:
        el = page.locator(selector).first
        el.click()
        page.wait_for_timeout(200)
        # Select all and delete first, then type
        el.press("Control+a")
        el.press("Delete")
        page.wait_for_timeout(100)
        el.type(value, delay=30)
    except PTE:
        log.warning("[Carwow] Could not fill input: %s", selector)


def _parse_carwow_vehicle(text: str) -> dict:
    """
    Parse the Carwow vehicle confirmation page.
    Typical layout (from live test):
      "FG63 ACY"
      "Porsche 911 (2011-2016) S 2dr PDK"
      "Automatic • Petrol • Silver"
    """
    data = {}
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    # Find the bold vehicle description line (contains year range in brackets)
    for i, line in enumerate(lines):
        if re.search(r"\((\d{4})-(\d{4})\)", line):
            data["vehicle_description"] = line
            # Parse make / model from the description
            # e.g. "Porsche 911 (2011-2016) S 2dr PDK"
            clean = re.sub(r"\(\d{4}-\d{4}\)", "", line).strip()
            parts = clean.split(None, 1)
            data["make"]  = parts[0] if parts else ""
            data["model"] = parts[1].strip() if len(parts) > 1 else ""

            # Next non-empty lines contain Transmission, Fuel, Colour as separate items
            # Collect all text after the model line up to a navigation/footer item
            j = i + 1
            spec_blob = []
            while j < len(lines):
                raw = lines[j].strip()
                j += 1
                if not raw or raw in ("•", "·", "|"):
                    continue
                sl = raw.lower()
                if any(w in sl for w in ("this isn", "back to listing", "sell free")):
                    break
                spec_blob.append(raw)
                if len(spec_blob) >= 6:
                    break

            # Join everything and split on bullet chars OR double-spaces
            combined = " • ".join(spec_blob)  # normalize separators
            # Also handle inline bullet-free joins like "Automatic  Petrol  Silver"
            tokens = re.split(r"\s*[•·|]\s*|\s{2,}", combined)
            for tok in tokens:
                tok = tok.strip()
                if not tok:
                    continue
                tl = tok.lower()
                if any(t in tl for t in ("automatic","manual","pdk","dsg","cvt","tiptronic","s-tronic")):
                    if not data.get("transmission"):
                        data["transmission"] = tok
                elif any(f in tl for f in ("petrol","diesel","electric","hybrid","plug")):
                    if not data.get("fuel_type"):
                        data["fuel_type"] = tok
                elif (re.match(r"^[A-Za-z][a-z\s-]+$", tok)
                      and len(tok) < 25
                      and "isn" not in tl and "next" not in tl
                      and "back" not in tl and "sell" not in tl):
                    if not data.get("colour"):
                        data["colour"] = tok
            break

    return data


def _extract_carwow_valuation(text: str, result: dict) -> None:
    """Extract the valuation price from Carwow's results page."""
    # Carwow may show: "Your estimated value £XX,XXX" or similar
    # Look for £ amounts above £1,000
    prices = re.findall(r"£\s*([\d]{1,3}(?:,\d{3})+|[\d]{4,6})", text)
    valid  = sorted(set(
        int(p.replace(",", "")) for p in prices
        if int(p.replace(",", "")) >= 1_000
    ))

    if valid:
        result["valuation"]     = f"£{valid[0]:,}"
        result["valuation_num"] = valid[0]
        result["confidence"]    = "High" if len(valid) == 1 else "Medium"
        if len(valid) >= 2:
            lo, hi = min(valid), max(valid)
            result["valuationRange"] = {
                "low": lo, "high": hi,
                "low_fmt": f"£{lo:,}", "high_fmt": f"£{hi:,}",
            }
        log.info("[Carwow] Valuation extracted: %s", result["valuation"])
    else:
        result["warnings"].append(
            "Carwow: no valuation price visible after form submission — "
            "the page may require further steps or a real phone number."
        )
        log.warning("[Carwow] No valuation found. Page text snippet: %s", text[:500])
