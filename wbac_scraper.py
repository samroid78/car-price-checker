"""
WBAC UK scraper — full 3-step headless browser flow:
  1. Submit reg + mileage
  2. Fill contact form (OFCOM test number + example.com email)
  3. Extract vehicle details AND the valuation price from the result page
"""
import re
from playwright.sync_api import sync_playwright, TimeoutError as PTE

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

BODY_TYPES = [
    "CONVERTIBLE", "CABRIOLET", "ROADSTER", "TARGA", "SPIDER",
    "ESTATE", "SHOOTING BRAKE",
    "COUPE", "FASTBACK",
    "HATCHBACK",
    "SALOON", "SEDAN",
    "SUV", "CROSSOVER",
    "MPV", "PEOPLE CARRIER",
    "VAN", "PICKUP",
    "LIMOUSINE",
]

_TEST_PHONE    = "07863239691"
_TEST_EMAIL    = "samroid78@gmail.com"
_TEST_POSTCODE = "E181BT"


def get_wbac_data(reg: str, mileage: int) -> dict:
    """
    Full WBAC UK valuation flow via stealth headless Chromium.
    Returns: wbac_description, variant, body_type, transmission,
             wbac_year, wbac_colour, valuation_price, wbac_url.
    """
    result = {
        "wbac_url": (
            f"https://www.webuyanycar.com/car-valuation/"
            f"?vrm={reg.upper().replace(' ', '')}&mileage={mileage}"
        )
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=STEALTH_ARGS)
            ctx = browser.new_context(
                locale="en-GB",
                timezone_id="Europe/London",
                user_agent=UA,
                extra_http_headers={"Accept-Language": "en-GB,en;q=0.9"},
                viewport={"width": 1366, "height": 768},
            )
            page = ctx.new_page()
            page.add_init_script(
                'Object.defineProperty(navigator,"webdriver",{get:()=>undefined})'
            )

            # ── Step 1: submit reg + mileage ──────────────────────────────
            page.goto("https://www.webuyanycar.com/car-valuation/", timeout=40000)
            page.wait_for_selector('input[name="registrationNumber"]', timeout=20000)

            for btn in ["#onetrust-accept-btn-handler", "#onetrust-reject-all-handler"]:
                try:
                    page.click(btn, timeout=3000)
                    page.wait_for_timeout(800)
                    break
                except PTE:
                    pass

            page.fill('input[name="registrationNumber"]', reg.upper().replace(" ", ""))
            page.fill('input[name="mileage"]', str(mileage))
            page.click('button[type="submit"]', timeout=10000)

            # ── Step 2: vehicle details page — extract description ─────────
            page.wait_for_url("**/vehicle/details**", timeout=25000)
            page.wait_for_selector("#EmailAddress", timeout=15000)

            body_text = page.inner_text("body")
            result.update(_parse_vehicle_text(body_text))

            # ── Step 3: contact form → valuation result page ───────────────
            for sel, val in [
                ("#EmailAddress",    _TEST_EMAIL),
                ("#Postcode",        _TEST_POSTCODE),
                ("#TelephoneNumber", _TEST_PHONE),
            ]:
                el = page.locator(sel).first
                el.click()
                page.wait_for_timeout(300)
                el.type(val)
                page.wait_for_timeout(300)

            page.locator('button:has-text("Get my valuation")').first.click(timeout=10000)
            page.wait_for_url("**/valuation/view**", timeout=25000)
            page.wait_for_timeout(2000)

            val_text = page.inner_text("body")
            result.update(_extract_valuation(val_text))
            # Keep the clean URL (not the session-specific result page which expires)
            # result["wbac_url"] already set to the clean valuation URL above

            browser.close()

    except Exception as e:
        print(f"[WBAC scraper] {e}")

    return result


# ── Parsers ────────────────────────────────────────────────────────────────


def _parse_vehicle_text(text: str) -> dict:
    """
    Parse the WBAC vehicle description line, e.g.:
      PORSCHE 911 [991] CARRERA CABRIOLET - S 2dr PDK • 2013 • Silver • Automatic
    """
    data = {}
    for line in text.split("\n"):
        line = line.strip()
        if "•" in line and re.search(r"\b(19|20)\d{2}\b", line):
            data["wbac_description"] = line
            break

    desc = data.get("wbac_description", "")
    if not desc:
        return data

    parts = [p.strip() for p in desc.split("•")]

    if parts:
        full_model = parts[0].strip()
        data["variant"] = full_model
        upper = full_model.upper()
        for bt in BODY_TYPES:
            if bt in upper:
                data["body_type"] = bt.title()
                break
        if not data.get("transmission"):
            _detect_transmission(full_model, data)

    if len(parts) > 1:
        m = re.search(r"\b(19|20)\d{2}\b", parts[1])
        if m:
            data["wbac_year"] = m.group(0)

    if len(parts) > 2:
        data["wbac_colour"] = parts[2].strip().title()

    if len(parts) > 3:
        _detect_transmission(parts[3].strip(), data, override=True)

    return data


def _extract_valuation(text: str) -> dict:
    """Extract the headline valuation price from the WBAC result page."""
    data = {}

    # The page shows "Your valuation\n£41,845" — find the first £ amount
    # that is clearly a car price (4-6 digits, possibly with commas)
    prices = re.findall(r"£\s*([\d]{2,3}(?:,\d{3})+|[\d]{4,6})", text)
    if prices:
        # The first match on the valuation page is the headline price
        raw = prices[0].replace(",", "")
        try:
            amount = int(raw)
            if amount >= 100:   # ignore tiny amounts (transaction fees etc.)
                data["valuation_price"] = f"£{amount:,}"
        except ValueError:
            pass

    return data


def extract_wbac_specs(model: str, wbac_description: str) -> dict:
    """
    Extract ALL exact vehicle specifications from a WBAC description string.
    Returns a dict used to match listings on AutoTrader with full precision.

    WBAC format example:
      'PORSCHE 911 [991] CARRERA CABRIOLET - S 2dr PDK • 2013 • Silver • Automatic'

    Returns:
      {
        "base_model":    "911",            # base model for the URL
        "variant":       "Carrera S",      # EXACT variant phrase — MUST appear in AT title
        "full_model":    "911 Carrera S",  # for display
        "year":          "2013",
        "body_type":     "Convertible",
        "transmission":  "Automatic",
      }
    """
    import re as _re

    result = {
        "base_model":   model,
        "variant":      "",
        "full_model":   model,
        "year":         "",
        "body_type":    "",
        "transmission": "",
    }

    if not wbac_description:
        return result

    desc = wbac_description.upper()

    # ── Step 1: Extract year from bullet-separated parts ──────────────────
    parts = [p.strip() for p in wbac_description.split("•")]
    if len(parts) >= 2:
        ym = _re.search(r"\b(19|20)\d{2}\b", parts[1])
        if ym:
            result["year"] = ym.group(0)
    if len(parts) >= 4:
        trans_part = parts[3].strip().lower()
        if "automatic" in trans_part or "auto" in trans_part:
            result["transmission"] = "Automatic"
        elif "manual" in trans_part:
            result["transmission"] = "Manual"

    # ── Step 2: Body type ────────────────────────────────────────────────
    for bt_wbac, bt_at in [
        ("CABRIOLET",     "Convertible"),
        ("CONVERTIBLE",   "Convertible"),
        ("ROADSTER",      "Convertible"),
        ("TARGA",         "Convertible"),
        ("COUPE",         "Coupe"),
        ("FASTBACK",      "Coupe"),
        ("HATCHBACK",     "Hatchback"),
        ("SALOON",        "Saloon"),
        ("ESTATE",        "Estate"),
        ("SUV",           "SUV"),
    ]:
        if bt_wbac in desc:
            result["body_type"] = bt_at
            break

    # ── Step 3: Transmission from description ────────────────────────────
    if not result["transmission"]:
        for kw, label in [("PDK", "Automatic"), ("DSG", "Automatic"), ("TIPTRONIC", "Automatic"),
                           ("AUTOMATIC", "Automatic"), ("MANUAL", "Manual")]:
            if kw in desc:
                result["transmission"] = label
                break

    # ── Step 4: Extract EXACT variant phrase ─────────────────────────────
    # WBAC format: "CARRERA BODY - SUFFIX" means "Carrera SUFFIX"
    # e.g. "CARRERA CABRIOLET - S" → "Carrera S"  (NOT "Carrera 4S", NOT base "Carrera")
    suf_match = _re.search(r"CARRERA\s+\w+\s*-\s*(4 GTS|4S|GTS|S)\b", desc)
    if suf_match:
        suffix = suf_match.group(1).strip()
        variant_map = {"S": "Carrera S", "4S": "Carrera 4S", "GTS": "Carrera GTS", "4 GTS": "Carrera 4 GTS"}
        result["variant"] = variant_map.get(suffix, f"Carrera {suffix}")
    else:
        # Full variant patterns — longest/most specific first
        VARIANTS = [
            ("TURBO S",       "Turbo S"),
            ("TURBO",         "Turbo"),
            ("GT3 RS",        "GT3 RS"),
            ("GT3",           "GT3"),
            ("GT2 RS",        "GT2 RS"),
            ("GT2",           "GT2"),
            ("CARRERA 4 GTS", "Carrera 4 GTS"),
            ("CARRERA 4S",    "Carrera 4S"),
            ("CARRERA 4",     "Carrera 4"),
            ("GTS",           "GTS"),
            ("CARRERA",       "Carrera"),
        ]
        for pattern, label in VARIANTS:
            if pattern in desc:
                result["variant"] = label
                break

    result["base_model"] = model
    result["full_model"] = f"{model} {result['variant']}".strip() if result["variant"] else model
    return result


def extract_autotrader_model(make: str, model: str, wbac_description: str) -> str:
    """Legacy wrapper — returns the full model string for the AutoTrader search."""
    specs = extract_wbac_specs(model, wbac_description)
    return specs["full_model"]


def _detect_transmission(text: str, data: dict, override: bool = False) -> None:
    if data.get("transmission") and not override:
        return
    tl = text.lower()
    if any(k in tl for k in ("automatic", "auto", "pdk", "dsg", "cvt", "tiptronic", "s-tronic")):
        data["transmission"] = "Automatic"
    elif any(k in tl for k in ("manual", " man ")):
        data["transmission"] = "Manual"
    elif text.strip() and override:
        data["transmission"] = text.strip().title()
