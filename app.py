import os
import re
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

from wbac_scraper import get_wbac_data
from services.autotrader_service  import search_autotrader
from services.motorway_service    import get_motorway_valuation
from services.carwow_service      import get_carwow_valuation
from services.vehicle_match_service import normalize_price

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)

GOVUK_VE_BASE = "https://vehicleenquiry.service.gov.uk"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}


# ── Helpers ────────────────────────────────────────────────────────────────


def clean_reg(reg: str) -> str:
    return reg.upper().replace(" ", "").replace("-", "").strip()


def decode_year_from_reg(reg: str) -> str:
    reg = clean_reg(reg)
    if re.match(r"^[A-Z]{2}\d{2}[A-Z]{3}$", reg):
        code = int(reg[2:4])
        return str(2000 + code) if code < 50 else str(2000 + code - 50)
    prefix = {
        "A": "1983", "B": "1984", "C": "1985", "D": "1986", "E": "1987",
        "F": "1988", "G": "1989", "H": "1990", "J": "1991", "K": "1992",
        "L": "1993", "M": "1994", "N": "1995", "P": "1996", "R": "1997",
        "S": "1998", "T": "1999", "V": "1999", "W": "2000", "X": "2000",
        "Y": "2001",
    }
    if re.match(r"^[A-Y]\d{1,3}[A-Z]{3}$", reg) and reg[0] in prefix:
        return prefix[reg[0]]
    return ""


# ── DVLA vehicle enquiry (3-step form) ────────────────────────────────────


def get_vehicle_info(reg: str) -> dict:
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        r1 = session.get(f"{GOVUK_VE_BASE}/", timeout=15)
        csrf1 = (
            BeautifulSoup(r1.text, "html.parser")
            .find("meta", {"name": "csrf-token"})
            .get("content", "")
        )
        r2 = session.post(
            f"{GOVUK_VE_BASE}/vehicle-enquiry/save?locale=en",
            data={"wizard_vehicle_enquiry_capture_vrn[vrn]": reg,
                  "authenticity_token": csrf1},
            headers={"Referer": f"{GOVUK_VE_BASE}/", "X-CSRF-Token": csrf1},
            allow_redirects=True, timeout=15,
        )
        if "VehicleNotFound" in r2.url:
            return {}

        soup2 = BeautifulSoup(r2.text, "html.parser")
        vehicle = {}
        for row in soup2.find_all(class_="govuk-summary-list__row"):
            k = (row.find(class_="govuk-summary-list__key") or {}).get_text(strip=True).lower()
            v = (row.find(class_="govuk-summary-list__value") or {}).get_text(strip=True)
            if "make" in k:
                vehicle["make"] = v.title()
            elif "colour" in k or "color" in k:
                vehicle["colour"] = v.title()
        if not vehicle.get("make"):
            return {}

        confirm_form = soup2.find("form", {"action": lambda a: a and "vehicle-enquiry/save" in a})
        csrf2 = (
            confirm_form.find("input", {"name": "authenticity_token"}).get("value", "")
            if confirm_form else csrf1
        )
        r3 = session.post(
            f"{GOVUK_VE_BASE}/vehicle-enquiry/save?locale=en",
            data={"wizard_vehicle_enquiry_capture_confirm_vehicle[confirmed]": "Yes",
                  "authenticity_token": csrf2},
            headers={"Referer": r2.url, "X-CSRF-Token": csrf2},
            allow_redirects=True, timeout=15,
        )
        soup3 = BeautifulSoup(r3.text, "html.parser")

        DT_MAP = {
            "fuel type":                  ("fuel_type",   True),
            "cylinder capacity":          ("engine_size", False),
            "co2":                        ("co2",         False),
            "year of manufacture":        ("year",        False),
            "date of first registration": ("first_reg",   False),
            "vehicle colour":             ("colour",      True),
        }
        for dt in soup3.find_all("dt"):
            dd = dt.find_next_sibling("dd")
            if not dd:
                continue
            key = dt.get_text(strip=True).lower()
            val = dd.get_text(strip=True)
            if not val or val.lower() in ("not available", "n/a"):
                continue
            for pattern, (field, titlecase) in DT_MAP.items():
                if pattern in key:
                    if field == "first_reg":
                        ym = re.search(r"\b(19|20)\d{2}\b", val)
                        if ym and not vehicle.get("year"):
                            vehicle["year"] = ym.group(0)
                    elif not vehicle.get(field):
                        vehicle[field] = val.title() if titlecase else val
                    break

        for panel in soup3.find_all(class_=lambda c: c and "govuk-panel" in c):
            parts = [p.strip() for p in panel.get_text("\n").split("\n") if p.strip()]
            txt = " ".join(parts)
            tl  = txt.lower()
            if "taxed" in tl and "tax_status" not in vehicle:
                vehicle["tax_status"] = "Taxed"
            elif "sorn" in tl and "tax_status" not in vehicle:
                vehicle["tax_status"] = "SORN"
            elif "untaxed" in tl and "tax_status" not in vehicle:
                vehicle["tax_status"] = "Untaxed"
            if "tax due" in tl and "tax_due" not in vehicle:
                m = re.search(r"Tax due[:\s]+(\d{1,2}\s+\w+\s+\d{4})", txt, re.IGNORECASE)
                if m:
                    vehicle["tax_due"] = m.group(1).strip()
            if "mot" in tl and "mot_status" not in vehicle:
                if "no details" in tl or "no mot" in tl:
                    vehicle["mot_status"] = "No details held by DVLA"
                elif "valid" in tl or "passed" in tl:
                    vehicle["mot_status"] = "Valid"
                elif "fail" in tl:
                    vehicle["mot_status"] = "Failed"
            if "expir" in tl and "mot_expiry" not in vehicle:
                m = re.search(r"Expir\w+[:\s]+(\d{1,2}\s+\w+\s+\d{4})", txt, re.IGNORECASE)
                if m:
                    vehicle["mot_expiry"] = m.group(1).strip()

        vehicle["year"]   = vehicle.get("year") or decode_year_from_reg(reg)
        vehicle["source"] = "DVLA (gov.uk)"
        return vehicle

    except Exception as e:
        log.error("[DVLA] %s", e)

    year = decode_year_from_reg(reg)
    return {"year": year, "source": "reg plate decode"} if year else {}


# ── Price insight ──────────────────────────────────────────────────────────


def _build_insight(wbac_data: dict, at_data: dict, motorway_data: dict,
                   carwow_data: dict) -> dict:
    warnings = []

    wbac_num   = normalize_price(wbac_data.get("valuation_price", ""))
    at_avg     = at_data.get("averagePrice") or 0
    at_low     = at_data.get("lowestPrice")  or 0
    at_high    = at_data.get("highestPrice") or 0
    mwy_num    = motorway_data.get("valuation_num") or 0
    cw_num     = carwow_data.get("valuation_num")   or 0

    # Estimated trade range (WBAC + Motorway as trade buyers)
    trade_values = [v for v in [wbac_num, mwy_num] if v > 0]
    trade_range  = {
        "low":  min(trade_values) if trade_values else None,
        "high": max(trade_values) if trade_values else None,
    }

    # Estimated retail range (AutoTrader listings)
    retail_range = {
        "low":  at_low  or None,
        "high": at_high or None,
    }

    # Price gap (trade vs retail)
    price_gap = None
    if wbac_num and at_avg:
        price_gap = at_avg - wbac_num

    # Narrative summary
    summary_parts = []
    if wbac_num and at_avg:
        pct = round((price_gap / at_avg) * 100) if at_avg else 0
        if price_gap and price_gap > 0:
            summary_parts.append(
                f"WBAC's offer of £{wbac_num:,} is ~{pct}% below the AutoTrader average "
                f"(£{at_avg:,}), which is typical — trade buyers offer less than retail."
            )
        elif price_gap and price_gap <= 0:
            summary_parts.append(
                "WBAC's offer is at or above the AutoTrader comparable average — "
                "this is a strong trade offer."
            )
    elif wbac_num:
        summary_parts.append(f"WBAC valuation: £{wbac_num:,}. AutoTrader data not available.")
    else:
        summary_parts.append("No WBAC valuation retrieved — comparison is partial.")

    at_count = at_data.get("count", 0)
    if at_count:
        summary_parts.append(f"{at_count} comparable AutoTrader listing{'s' if at_count != 1 else ''} found.")
    else:
        warnings.append("No AutoTrader listings found — prices may be unavailable.")

    if at_data.get("warnings"):
        warnings.extend(at_data["warnings"])
    if motorway_data.get("warnings"):
        warnings.extend(motorway_data["warnings"])
    if carwow_data.get("warnings"):
        warnings.extend(carwow_data["warnings"])

    return {
        "summary":     " ".join(summary_parts),
        "tradeRange":  trade_range,
        "retailRange": retail_range,
        "priceGap":    price_gap,
        "priceGapFmt": f"£{abs(price_gap):,}" if price_gap is not None else None,
        "warnings":    warnings,
    }


# ── Flask routes ────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/check", methods=["POST"])
def check_car():
    body = request.get_json(silent=True) or {}

    raw_reg = body.get("registration", "").strip()
    if not raw_reg:
        return jsonify({"error": "Please enter a registration number."}), 400
    reg = clean_reg(raw_reg)
    if not reg or len(reg) < 2 or len(reg) > 8:
        return jsonify({"error": "Invalid UK registration number."}), 400

    user_make  = body.get("make",  "").strip().title()
    user_model = body.get("model", "").strip()
    try:
        mileage = int(str(body.get("mileage", "0")).replace(",", "").replace(" ", ""))
    except (ValueError, TypeError):
        mileage = 0

    # ── Phase 1: DVLA lookup (fast, ~3 s) ─────────────────────────────────
    log.info("[API] Starting check for reg=%s", reg)
    vehicle = get_vehicle_info(reg)
    if not vehicle:
        return jsonify({
            "error": f"No vehicle found for '{reg}'. Check the registration and try again."
        }), 404

    if user_make:
        vehicle["make"] = user_make
    if user_model:
        vehicle["model"] = user_model
    if mileage:
        vehicle["mileage_num"] = mileage
        vehicle["mileage"]     = f"{mileage:,} miles"

    make  = vehicle.get("make", "")
    model = user_model or vehicle.get("model", "")
    year  = vehicle.get("year", "")

    # ── Phase 2: WBAC first (serial) — Playwright; keep it isolated ──────
    # Running WBAC alongside other Playwright-based scrapers simultaneously
    # can cause resource contention.  Run it alone, then parallelize the rest.
    log.info("[API] Phase 2: WBAC scrape")
    wbac_data = {}
    if mileage:
        try:
            wbac_data = get_wbac_data(reg, mileage)
        except Exception as e:
            log.error("[WBAC] %s", e)
            wbac_data = {"warnings": [str(e)]}

    # Promote WBAC fields to vehicle early so market scrapers can use them
    for field in ("body_type", "transmission", "wbac_colour", "variant"):
        if wbac_data.get(field) and not vehicle.get(field):
            vehicle[field] = wbac_data[field]

    # ── Phase 3: Market scrapers in parallel ───────────────────────────────
    log.info("[API] Phase 3: parallel market scrapes")

    def run_autotrader():
        if not (make and model):
            return {"warnings": ["Make/model required for AutoTrader search."]}
        # Use exact year from WBAC — vehicle["year"] is set from WBAC/DVLA
        exact_year = vehicle.get("year", year)
        return search_autotrader(
            make, model, exact_year, mileage,
            body_type    = vehicle.get("body_type")    or wbac_data.get("body_type"),
            fuel_type    = vehicle.get("fuel_type")    or wbac_data.get("fuel_type"),
            transmission = vehicle.get("transmission") or wbac_data.get("transmission"),
        )

    def run_motorway():
        if not mileage:
            return {"warnings": ["Mileage required for Motorway valuation."]}
        return get_motorway_valuation(reg, mileage)

    def run_carwow():
        if not mileage:
            return {"warnings": ["Mileage required for Carwow valuation."]}
        # Carwow uses the registration directly — no need for make/model
        return get_carwow_valuation(reg, mileage, make=make, model=model, year=year)

    tasks = {
        "autotrader": run_autotrader,
        "motorway":   run_motorway,
        "carwow":     run_carwow,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=3) as exe:
        future_map = {exe.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(future_map):
            name = future_map[future]
            try:
                results[name] = future.result(timeout=120)
            except Exception as e:
                log.error("[%s] task failed: %s", name, e)
                results[name] = {"error": str(e), "warnings": [str(e)]}

    at_data       = results.get("autotrader", {})
    motorway_data = results.get("motorway",   {})
    carwow_data   = results.get("carwow",     {})

    # Also promote any Motorway fields (e.g. full model name) to vehicle
    for field in ("model", "body_type", "fuel_type", "colour"):
        mwy_val = motorway_data.get(field)
        if mwy_val and not vehicle.get(field):
            vehicle[field] = mwy_val

    insight = _build_insight(wbac_data, at_data, motorway_data, carwow_data)

    log.info("[API] Done: WBAC=%s AT=%d listings MWY=%s CW=%s",
             wbac_data.get("valuation_price"),
             at_data.get("count", 0),
             motorway_data.get("valuation"),
             carwow_data.get("valuation"))

    return jsonify({
        "registration": reg,
        "vehicle":      vehicle,
        "wbac":         wbac_data,
        "autotrader":   at_data,
        "motorway":     motorway_data,
        "carwow":       carwow_data,
        "insight":      insight,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV", "production") == "development"
    print(f"\n  Car Price Checker -> http://localhost:{port}\n")
    app.run(debug=debug, port=port, host="0.0.0.0")
