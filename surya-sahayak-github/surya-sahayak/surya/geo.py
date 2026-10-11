"""Where is this home?  GPS -> city, bill PIN code -> coordinates, network address as a last resort.

Privacy: only a 6-digit PIN code or a city name is ever sent to a map service — never the
customer's name or street address. IP addresses are used for one lookup and are not stored.
All services are free and keyless: Nominatim (OpenStreetMap), Open-Meteo geocoding,
BigDataCloud (reverse fallback), ipwho.is / ip-api.com (IP fallback).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time

import requests

from . import states

log = logging.getLogger("surya.geo")

HEADERS = {"User-Agent": "SuryaSahayak/1.0 (AWS hackathon demo)"}   # Nominatim policy: identify the app
NOMINATIM = "https://nominatim.openstreetmap.org"
OM_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
BIGDATACLOUD = "https://api.bigdatacloud.net/data/reverse-geocode-client"
NOMINATIM_MIN_INTERVAL = 1.1          # seconds between Nominatim calls (their usage policy: max 1/s)

_nom_lock = threading.Lock()
_nom_last = 0.0
_rev_cache: dict = {}
_fwd_cache: dict = {}


# ------------------------------------------------------------------ helpers
def in_india_box(lat, lon) -> bool:
    try:
        return 6.0 <= float(lat) <= 37.6 and 68.0 <= float(lon) <= 97.6
    except (TypeError, ValueError):
        return False


def extract_pincode(text) -> str | None:
    """First Indian PIN code (6 digits, not starting with 0) in free text."""
    m = re.search(r"(?<!\d)([1-9]\d{2}\s?\d{3})(?!\d)", str(text or ""))
    return re.sub(r"\s", "", m.group(1)) if m else None


def _get(url: str, params: dict | None = None, timeout: float = 8.0):
    r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _nominatim(path: str, params: dict):
    global _nom_last
    with _nom_lock:                                        # serialise + throttle
        wait = NOMINATIM_MIN_INTERVAL - (time.monotonic() - _nom_last)
        if wait > 0:
            time.sleep(wait)
        try:
            return _get(f"{NOMINATIM}/{path}", params)
        finally:
            _nom_last = time.monotonic()


def _is_public(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _first(d: dict, keys) -> str | None:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return None


def _label(loc: dict) -> str:
    """'Hauz Khas, New Delhi' / 'Shivajinagar, Pune, Maharashtra'. The state is skipped when the city already says it."""
    parts = []
    for p in (loc.get("locality"), loc.get("city"), loc.get("state")):
        if p and p not in parts:
            parts.append(p)
    if len(parts) >= 2 and loc.get("state") and parts[-1] == loc["state"]:
        if any(loc["state"].lower() in q.lower() for q in parts[:-1]):
            parts.pop()
    return ", ".join(parts) or "Your location"


def _from_nominatim_address(a: dict) -> dict:
    code = states.state_code(a.get("ISO3166-2-lvl4")) or states.state_code(a.get("state"))
    return {
        "city": _first(a, ("city", "town", "municipality", "village", "city_district", "county", "state_district")),
        "locality": _first(a, ("suburb", "neighbourhood", "quarter", "city_district", "hamlet")),
        "district": _first(a, ("state_district", "county")),
        "state": states.state_name(code) or a.get("state"),
        "state_code": code,
        "pincode": (a.get("postcode") or "").replace(" ", "") or None,
        "country_code": (a.get("country_code") or "").upper() or None,
    }


# ------------------------------------------------------------------ reverse: coordinates -> city
def reverse(lat: float, lon: float) -> dict | None:
    """GPS point -> {city, locality, district, state, state_code, pincode, country_code}. None if all services fail."""
    lat, lon = round(lat, 3), round(lon, 3)            # ~110 m: enough for city and state, not a street address
    key = (lat, lon)
    if key in _rev_cache:
        return _rev_cache[key]
    out = None
    try:
        j = _nominatim("reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 14,
                                   "addressdetails": 1, "accept-language": "en"})
        a = (j or {}).get("address")
        if a:
            out = _from_nominatim_address(a)
            out["via"] = "OpenStreetMap Nominatim"
    except Exception as e:
        log.info("Nominatim reverse failed: %s", e)
    if not out or not out.get("city"):
        try:
            j = _get(BIGDATACLOUD, {"latitude": lat, "longitude": lon, "localityLanguage": "en"})
            code = states.state_code(j.get("principalSubdivisionCode")) or states.state_code(j.get("principalSubdivision"))
            cand = {
                "city": j.get("city") or j.get("locality"),
                "locality": j.get("locality") if j.get("locality") != j.get("city") else None,
                "district": None,
                "state": states.state_name(code) or j.get("principalSubdivision"),
                "state_code": code,
                "pincode": (j.get("postcode") or "").replace(" ", "") or None,
                "country_code": (j.get("countryCode") or "").upper() or None,
                "via": "BigDataCloud",
            }
            if cand["city"] or cand["state"]:
                out = {**(out or {}), **{k: v for k, v in cand.items() if v}}
        except Exception as e:
            log.info("BigDataCloud reverse failed: %s", e)
    if out:
        if len(_rev_cache) > 500:
            _rev_cache.clear()
        _rev_cache[key] = out
    return out


# ------------------------------------------------------------------ forward: PIN code / city -> coordinates
def _from_open_meteo(res: dict) -> dict:
    code = states.state_code(res.get("admin1"))
    pcs = res.get("postcodes") or []
    return {"lat": float(res["latitude"]), "lon": float(res["longitude"]),
            "city": res.get("name"), "locality": None, "district": res.get("admin2"),
            "state": states.state_name(code) or res.get("admin1"), "state_code": code,
            "pincode": pcs[0] if pcs else None,
            "country_code": (res.get("country_code") or "IN").upper(), "via": "Open-Meteo geocoding"}


def forward(*, pincode: str | None = None, city: str | None = None) -> dict | None:
    """PIN code (preferred) or a city name -> {lat, lon, city, state, state_code, ...}. India only."""
    pin = extract_pincode(pincode) if pincode else None
    city = (city or "").strip() or None
    ck = (pin, (city or "").lower())
    if ck in _fwd_cache:
        return _fwd_cache[ck]
    out = None
    if pin:
        try:
            j = _nominatim("search", {"postalcode": pin, "countrycodes": "in", "format": "jsonv2",
                                      "addressdetails": 1, "limit": 1, "accept-language": "en"})
            if j:
                out = {"lat": float(j[0]["lat"]), "lon": float(j[0]["lon"]), "via": "OpenStreetMap Nominatim",
                       **_from_nominatim_address(j[0].get("address") or {})}
                out["pincode"] = out.get("pincode") or pin
        except Exception as e:
            log.info("Nominatim postcode search failed: %s", e)
    for q in ([pin] if pin and not out else []) + ([city] if city and not out else []):
        try:
            j = _get(OM_GEOCODE, {"name": q, "count": 5, "language": "en", "format": "json", "countryCode": "IN"})
            res = [r for r in (j.get("results") or []) if (r.get("country_code") or "").upper() == "IN"]
            if res:
                out = _from_open_meteo(res[0])
                if pin and not out.get("pincode"):
                    out["pincode"] = pin
        except Exception as e:
            log.info("Open-Meteo geocoding failed for %r: %s", q, e)
        if out:
            break
    if not out and city:
        try:
            j = _nominatim("search", {"q": f"{city}, India", "countrycodes": "in", "format": "jsonv2",
                                      "addressdetails": 1, "limit": 1, "accept-language": "en"})
            if j:
                out = {"lat": float(j[0]["lat"]), "lon": float(j[0]["lon"]), "via": "OpenStreetMap Nominatim",
                       **_from_nominatim_address(j[0].get("address") or {})}
        except Exception as e:
            log.info("Nominatim city search failed: %s", e)
    if out and in_india_box(out["lat"], out["lon"]):
        _fwd_cache[ck] = out
        return out
    return None


# ------------------------------------------------------------------ IP address (last resort)
def from_ip(ip: str | None) -> dict | None:
    """Rough city from a public IP. Accurate to the ISP's city at best (~10-50 km)."""
    if not ip or not _is_public(ip):
        return None
    try:
        j = _get(f"https://ipwho.is/{ip}", {"fields": "success,city,region,region_code,country_code,latitude,longitude,postal"}, timeout=5)
        if j.get("success") is not False and j.get("latitude") is not None:
            code = states.state_code(j.get("region_code")) or states.state_code(j.get("region"))
            return {"lat": float(j["latitude"]), "lon": float(j["longitude"]), "city": j.get("city"),
                    "locality": None, "district": None, "state": states.state_name(code) or j.get("region"),
                    "state_code": code, "pincode": j.get("postal") or None,
                    "country_code": (j.get("country_code") or "").upper(), "via": "ipwho.is"}
    except Exception as e:
        log.info("ipwho.is failed: %s", e)
    try:
        j = _get(f"http://ip-api.com/json/{ip}", {"fields": "status,city,regionName,region,countryCode,lat,lon,zip"}, timeout=5)
        if j.get("status") == "success":
            code = states.state_code(j.get("regionName")) or states.state_code(j.get("region"))
            return {"lat": float(j["lat"]), "lon": float(j["lon"]), "city": j.get("city"),
                    "locality": None, "district": None, "state": states.state_name(code) or j.get("regionName"),
                    "state_code": code, "pincode": j.get("zip") or None,
                    "country_code": (j.get("countryCode") or "").upper(), "via": "ip-api.com"}
    except Exception as e:
        log.info("ip-api.com failed: %s", e)
    return None


# ------------------------------------------------------------------ orchestration
def _done(loc: dict, source: str, notes: list, alternatives: list | None = None, accuracy_m=None) -> dict:
    loc = dict(loc)
    loc.update({"source": source, "label": _label(loc), "notes": notes,
                "alternatives": alternatives or [], "accuracy_m": accuracy_m, "needs_manual": False})
    loc["lat"], loc["lon"] = round(float(loc["lat"]), 5), round(float(loc["lon"]), 5)
    return loc


def locate(*, lat=None, lon=None, accuracy_m=None, bill: dict | None = None, ip: str | None = None,
           query: str | None = None, prefer: str | None = None) -> dict:
    """Pick the best location automatically.

    Order: typed search > GPS > PIN code / city on the bill > network address > state capital.
    If GPS and the bill are in different states, the bill wins (the roof is where the connection is),
    unless the caller passes prefer="gps". Everything we skipped is reported in `notes` / `alternatives`.
    """
    bill = bill or {}
    notes: list[str] = []
    bill_state = states.state_code(bill.get("state")) or states.state_from_discom(bill.get("discom"))
    bill_pin = extract_pincode(bill.get("pincode")) or extract_pincode(bill.get("service_address"))
    bill_city = (bill.get("city") or "").strip() or None

    def from_bill():
        loc = forward(pincode=bill_pin, city=bill_city) if (bill_pin or bill_city) else None
        if loc and bill_state and loc.get("state_code") and loc["state_code"] != bill_state:
            notes.append("bill_place_state_mismatch")
        return loc

    # 1. user typed a place
    if query and query.strip():
        loc = forward(pincode=extract_pincode(query), city=None if extract_pincode(query) else query.strip())
        if loc:
            return _done(loc, "manual", notes)
        notes.append("search_not_found")

    # 2. GPS
    gps = None
    if lat is not None and lon is not None and in_india_box(lat, lon):
        rev = reverse(float(lat), float(lon)) or {}
        if rev.get("country_code") in (None, "IN"):
            gps = {**rev, "lat": float(lat), "lon": float(lon)}
        else:
            notes.append("gps_outside_india")
    elif lat is not None and lon is not None:
        notes.append("gps_outside_india")

    if gps:
        gps_state = gps.get("state_code")
        mismatch = bool(bill_state and gps_state and gps_state != bill_state)
        if mismatch and prefer != "gps":
            alt = _done(gps, "gps", [], accuracy_m=accuracy_m)
            loc = from_bill()
            if not loc and bill_state in states.CAPITALS:
                la, lo = states.CAPITALS[bill_state]
                loc = {"lat": la, "lon": lo, "city": states.state_name(bill_state), "locality": None,
                       "state": states.state_name(bill_state), "state_code": bill_state, "pincode": None}
                notes.append("state_capital_used")
            if loc:
                notes.append("gps_differs_from_bill")
                return _done(loc, "bill" if "state_capital_used" not in notes else "state_capital",
                             notes, alternatives=[alt])
        return _done(gps, "gps", notes, accuracy_m=accuracy_m)

    # 3. address on the bill
    loc = from_bill()
    if loc:
        return _done(loc, "bill", notes)

    # 4. network address (only if it says India)
    ipl = from_ip(ip)
    if ipl and ipl.get("country_code") == "IN" and in_india_box(ipl["lat"], ipl["lon"]):
        notes.append("approximate_from_network")
        return _done(ipl, "ip", notes, accuracy_m=25_000)
    if ipl:
        notes.append("network_outside_india")

    # 5. state capital from the discom
    if bill_state in states.CAPITALS:
        la, lo = states.CAPITALS[bill_state]
        notes.append("state_capital_used")
        return _done({"lat": la, "lon": lo, "city": states.state_name(bill_state), "locality": None,
                      "state": states.state_name(bill_state), "state_code": bill_state, "pincode": None},
                     "state_capital", notes, accuracy_m=100_000)

    return {"lat": None, "lon": None, "city": None, "state": None, "state_code": None, "source": None,
            "label": None, "notes": notes + ["location_unknown"], "alternatives": [], "needs_manual": True}
