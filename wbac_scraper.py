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


def extract_autotrader_model(make: str, model: str, wbac_description: str) -> str:
    """
    Refine the AutoTrader model string by appending the exact variant/trim
    extracted from the WBAC description.

    Examples:
      'PORSCHE 911 [991] CARRERA CABRIOLET - S 2dr PDK'  -> '911 Carrera S'
      'PORSCHE 911 [991] CARRERA CABRIOLET - 4S 2dr PDK' -> '911 Carrera 4S'
      'PORSCHE 911 [991] TURBO S CABRIOLET 2dr PDK'      -> '911 Turbo S'
      'BMW 3 SERIES 320D M SPORT ...'                     -> '3 Series 320d M Sport'
    """
    if not wbac_description:
        return model

    desc = wbac_description.upper()

    # ── Variant patterns — order matters (longer/more specific first) ──
    VARIANT_PATTERNS = [
        # Porsche 911
        ("TURBO S",          "Turbo S"),
        ("TURBO",            "Turbo"),
        ("GT3 RS",           "GT3 RS"),
        ("GT3",              "GT3"),
        ("GT2 RS",           "GT2 RS"),
        ("GT2",              "GT2"),
        ("GTS",              "GTS"),
        ("CARRERA 4 GTS",    "Carrera 4 GTS"),
        ("CARRERA 4S",       "Carrera 4S"),
        ("CARRERA 4",        "Carrera 4"),
        ("CARRERA S",        "Carrera S"),
        ("CARRERA",          "Carrera"),
        # BMW
        ("M3",  "M3"), ("M4",  "M4"), ("M5",  "M5"),
        # Mercedes
        ("AMG",  "AMG"),
        # Audi
        ("RS",  "RS"), ("S LINE", "S Line"),
    ]

    # Special WBAC format: "CARRERA CABRIOLET - S" means Carrera S
    import re as _re
    suf = _re.search(r"CARRERA\s+\w+\s*-\s*(4S|S|GTS|4 GTS)", desc)
    if suf:
        suffix = suf.group(1).strip()
        if suffix == "S":
            return f"{model} Carrera S"
        elif suffix == "4S":
            return f"{model} Carrera 4S"
        elif suffix == "GTS":
            return f"{model} Carrera GTS"
        elif suffix == "4 GTS":
            return f"{model} Carrera 4 GTS"

    for pattern, label in VARIANT_PATTERNS:
        if pattern in desc:
            return f"{model} {label}"

    return model   # fallback: no variant refinement found


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
