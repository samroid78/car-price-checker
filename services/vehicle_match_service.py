"""
Vehicle matching + confidence scoring.
Used by all scraper services to assess how closely a listing matches the target vehicle.
"""
import re
import logging

log = logging.getLogger(__name__)


# ── Normalisation helpers ──────────────────────────────────────────────────


def normalize_price(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(re.sub(r"[^\d]", "", str(value or "")) or 0)


def normalize_mileage(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return int(re.sub(r"[^\d]", "", str(value or "")) or 0)


def normalize_year(value) -> int:
    if isinstance(value, int):
        return value
    m = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return int(m.group(0)) if m else 0


def _tokens(text: str) -> set:
    """Split a string into uppercase tokens, stripping noise."""
    return set(re.split(r"[\s\-/\[\](),.]+", (text or "").upper())) - {"", "-"}


def fuzzy_variant_score(target: str, candidate: str) -> float:
    """
    Word-overlap similarity 0–1.
    0.0  = no overlap
    0.5+ = partial match
    1.0  = identical token sets
    """
    if not target or not candidate:
        return 0.0
    t, c = _tokens(target), _tokens(candidate)
    if not t or not c:
        return 0.0
    return len(t & c) / max(len(t | c), 1)


# ── Confidence scoring ─────────────────────────────────────────────────────


def compute_confidence(target: dict, candidate: dict) -> dict:
    """
    Score how well *candidate* matches *target* vehicle.

    target / candidate keys (all optional):
        make, model, year, mileage, body_type,
        fuel_type, transmission, variant, title

    Returns:
        {
            confidence: "High" | "Medium" | "Low",
            score: int (0-100),
            reasons: [str],
            relaxed_year: bool
        }
    """
    score = 0
    reasons: list[str] = []
    relaxed_year = False

    # ── Make ──────────────────────────────────────────────────────────────
    tm = (target.get("make") or "").upper()
    cm = (candidate.get("make") or candidate.get("title") or "").upper()
    if tm and (tm in cm or cm.startswith(tm)):
        score += 20
        reasons.append("Same make")

    # ── Model ─────────────────────────────────────────────────────────────
    tmo = (target.get("model") or "").upper()
    cmo = (candidate.get("model") or candidate.get("title") or "").upper()
    if tmo and (tmo in cmo or cmo.startswith(tmo)):
        score += 20
        reasons.append("Same model")

    # ── Year ──────────────────────────────────────────────────────────────
    ty = normalize_year(target.get("year"))
    cy = normalize_year(candidate.get("year"))
    if ty and cy:
        diff = abs(ty - cy)
        if diff == 0:
            score += 20
            reasons.append("Same year")
        elif diff == 1:
            score += 10
            reasons.append("Year within ±1")
            relaxed_year = True

    # ── Mileage ───────────────────────────────────────────────────────────
    tmi = normalize_mileage(target.get("mileage"))
    cmi = normalize_mileage(candidate.get("mileage"))
    if tmi and cmi:
        diff = abs(tmi - cmi)
        if diff <= 5_000:
            score += 20
            reasons.append("Mileage within 5,000 miles")
        elif diff <= 10_000:
            score += 10
            reasons.append("Mileage within 10,000 miles")
        elif diff <= 20_000:
            score += 5
            reasons.append("Mileage within 20,000 miles")

    # ── Variant (fuzzy) ───────────────────────────────────────────────────
    tv = target.get("variant") or ""
    cv = candidate.get("variant") or candidate.get("title") or ""
    vs = fuzzy_variant_score(tv, cv)
    if vs >= 0.55:
        score += 10
        reasons.append("Variant matched")
    elif vs >= 0.25:
        score += 4
        reasons.append("Closest variant match")

    # ── Body type ─────────────────────────────────────────────────────────
    tb = (target.get("body_type") or "").upper()
    cb = (candidate.get("body_type") or candidate.get("title") or "").upper()
    if tb and tb in cb:
        score += 5
        reasons.append("Body type matched")

    # ── Fuel type ─────────────────────────────────────────────────────────
    tf = (target.get("fuel_type") or "").upper()
    cf = (candidate.get("fuel_type") or "").upper()
    if tf and cf and tf == cf:
        score += 3
        reasons.append("Fuel type matched")

    # ── Transmission ──────────────────────────────────────────────────────
    tt = (target.get("transmission") or "").upper()
    ct = (candidate.get("transmission") or "").upper()
    if tt and ct and tt[:4] == ct[:4]:  # "AUTO" or "MANU"
        score += 2
        reasons.append("Transmission matched")

    # ── Confidence band ───────────────────────────────────────────────────
    # Relaxed year match caps at Medium regardless of score
    confidence = "Low"
    if score >= 75 and not relaxed_year:
        confidence = "High"
    elif score >= 45:
        confidence = "Medium"

    return {
        "confidence": confidence,
        "score": score,
        "reasons": reasons,
        "relaxed_year": relaxed_year,
    }


# ── Price stats helper ─────────────────────────────────────────────────────


def price_stats(prices: list[int]) -> dict:
    if not prices:
        return {}
    return {
        "count":   len(prices),
        "lowest":  min(prices),
        "highest": max(prices),
        "average": round(sum(prices) / len(prices)),
    }


# ── Unit tests (run with `python -m pytest services/vehicle_match_service.py`) ──


if __name__ == "__main__":
    # Quick smoke-test
    target = {
        "make": "Porsche", "model": "911", "year": "2013",
        "mileage": 42000, "body_type": "Cabriolet",
        "fuel_type": "Petrol", "transmission": "Automatic",
        "variant": "PORSCHE 911 [991] CARRERA CABRIOLET",
    }

    tests = [
        ({"make": "Porsche", "model": "911", "year": 2013, "mileage": 40000,
          "body_type": "Cabriolet", "fuel_type": "Petrol", "transmission": "Automatic",
          "variant": "Porsche 911 Carrera Cabriolet"},  "High"),
        ({"make": "Porsche", "model": "911", "year": 2014, "mileage": 50000,
          "fuel_type": "Petrol", "variant": "Porsche 911 Carrera"},  "Medium"),
        ({"make": "Porsche", "model": "911", "year": 2010, "mileage": 80000}, "Low"),
    ]

    for cand, expected in tests:
        r = compute_confidence(target, cand)
        status = "OK" if r["confidence"] == expected else "FAIL"
        print(f"[{status}] score={r['score']:>3} conf={r['confidence']:<6} "
              f"expected={expected}  reasons={r['reasons']}")
