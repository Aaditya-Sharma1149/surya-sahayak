"""Probe every outside service Surya Sahayak depends on, from THIS machine, then run one full report.

    cd ~/surya-sahayak && source .venv/bin/activate && python check_live.py        # about 20-40 seconds

Nothing is faked here. Each line says OK or FAIL, what came back, and what the app does if that service
stays down. Paste the whole output back if anything shows FAIL.
"""
from __future__ import annotations

import sys
import time
from datetime import date, timedelta

import requests

UA = {"User-Agent": "SuryaSahayak/1.0 (AWS hackathon demo)"}
DELHI = (28.5494, 77.2001)                       # Hauz Khas
RESULTS: list[tuple[str, bool]] = []


def probe(name: str, fn, if_down: str):
    t0 = time.time()
    try:
        detail = fn()
        RESULTS.append((name, True))
        print(f"  OK    {name:<32} {detail}  [{time.time() - t0:.1f}s]")
    except Exception as e:                       # noqa: BLE001 - a probe must never stop the others
        RESULTS.append((name, False))
        print(f"  FAIL  {name:<32} {type(e).__name__}: {str(e)[:320]}")
        print(f"          if it stays down: {if_down}")


def get(url, params=None, timeout=25):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- raw probes
def p_archive():
    end = date.today() - timedelta(days=10)
    j = get("https://archive-api.open-meteo.com/v1/archive", {
        "latitude": 28.55, "longitude": 77.2, "start_date": (end - timedelta(days=20)).isoformat(),
        "end_date": end.isoformat(), "hourly": "global_tilted_irradiance,temperature_2m",
        "tilt": 26, "azimuth": 0, "timezone": "GMT"})
    h = j["hourly"]
    g = [x for x in h["global_tilted_irradiance"] if x is not None]
    assert len(h["time"]) == len(h["global_tilted_irradiance"]) and g, "no tilted-irradiance series in the reply"
    return f"{len(g)} hourly values, peak {max(g):.0f} W/m² on the panel plane"


def p_forecast():
    j = get("https://api.open-meteo.com/v1/forecast", {
        "latitude": 28.55, "longitude": 77.2, "current": "temperature_2m,cloud_cover,is_day",
        "hourly": "global_tilted_irradiance", "tilt": 26, "azimuth": 0, "forecast_days": 1, "timezone": "auto"})
    c = j["current"]
    assert "temperature_2m" in c and j["hourly"]["global_tilted_irradiance"], "unexpected forecast shape"
    return f"{c['temperature_2m']}°C, cloud {c.get('cloud_cover')}%, is_day={c.get('is_day')}"


def p_nasa():
    end = date.today() - timedelta(days=10)
    j = get("https://power.larc.nasa.gov/api/temporal/daily/point", {
        "parameters": "ALLSKY_SFC_SW_DWN,T2M", "community": "RE", "format": "JSON", "latitude": 28.55,
        "longitude": 77.2, "start": (end - timedelta(days=9)).strftime("%Y%m%d"), "end": end.strftime("%Y%m%d")}, timeout=60)
    vals = [v for v in j["properties"]["parameter"]["ALLSKY_SFC_SW_DWN"].values() if v is not None and v >= 0]
    assert len(vals) >= 7, f"only {len(vals)} valid days"
    return f"{len(vals)} days, mean {sum(vals) / len(vals):.2f} kWh/m²/day"


def p_pvgis():
    from surya import engine
    engine._pvgis_cached.cache_clear()
    r = engine.pvgis_yield(*DELHI, 1.0)
    assert r["source"].startswith("PVGIS"), f"fell back to: {r['source']}"
    assert 1200 <= r["annual_kwh"] <= 1800, f"{r['annual_kwh']} kWh/kW/yr is outside the usual Delhi range"
    return f"{r['annual_kwh']:.0f} kWh per kW per year (typical year)"


def p_nominatim_rev():
    j = get("https://nominatim.openstreetmap.org/reverse", {"lat": DELHI[0], "lon": DELHI[1], "format": "jsonv2",
                                                              "zoom": 14, "addressdetails": 1, "accept-language": "en"})
    a = j["address"]
    assert a.get("state") == "Delhi" or a.get("ISO3166-2-lvl4") == "IN-DL", f"state came back as {a.get('state')!r}"
    return f"{a.get('suburb') or a.get('neighbourhood') or '?'}, {a.get('city') or a.get('state_district')}, {a.get('state')}"


def p_nominatim_pin():
    time.sleep(1.1)                                  # Nominatim allows 1 request per second
    j = get("https://nominatim.openstreetmap.org/search", {"postalcode": "110016", "countrycodes": "in", "format": "jsonv2",
                                                             "addressdetails": 1, "limit": 1, "accept-language": "en"})
    assert j, "no result for PIN 110016"
    return f"PIN 110016 -> {j[0]['lat'][:7]}, {j[0]['lon'][:7]} ({j[0]['address'].get('state')})"


def p_bigdata():
    j = get("https://api.bigdatacloud.net/data/reverse-geocode-client", {"latitude": DELHI[0], "longitude": DELHI[1], "localityLanguage": "en"})
    assert j.get("countryCode") == "IN", f"country {j.get('countryCode')!r}"
    return f"{j.get('city') or j.get('locality')}, {j.get('principalSubdivision')}"


def p_om_geocode():
    j = get("https://geocoding-api.open-meteo.com/v1/search", {"name": "Pune", "count": 3, "language": "en", "format": "json", "countryCode": "IN"})
    r = j["results"][0]
    return f"Pune -> {r['latitude']:.2f}, {r['longitude']:.2f} ({r.get('admin1')})"


def p_ipwho():
    j = get("https://ipwho.is/", {"fields": "success,city,region,country_code"}, timeout=8)
    assert j.get("success") is not False, str(j)[:120]
    return f"this server looks like {j.get('city')}, {j.get('country_code')} (expected: not India)"


def p_ipapi():
    j = get("http://ip-api.com/json/", {"fields": "status,city,countryCode"}, timeout=8)
    assert j.get("status") == "success", str(j)[:120]
    return f"this server looks like {j.get('city')}, {j.get('countryCode')}"


# ---------------------------------------------------------------- the app's own code paths
def p_history():
    from surya import weather
    h = weather.history(*DELHI)
    ms = h["months"]
    mean = sum(m["kwh_per_kw"] for m in ms) / (len(ms) / 12)
    return f"{len(ms)} months {h['start']}..{h['end']}, mean {mean:.0f} kWh/kW/yr via {h['source'].split(',')[0]}"


def p_geo():
    from surya import geo
    gps = geo.locate(lat=DELHI[0], lon=DELHI[1], accuracy_m=30)
    pin = geo.locate(bill={"pincode": "411005", "state": "MH"})
    assert gps.get("source") == "gps" and gps.get("state_code") == "DL", (
        f"GPS point in Delhi came back as {gps.get('label')!r}, state {gps.get('state_code')!r}. "
        "Both place-name services (Nominatim, BigDataCloud) failed above, so the app would use the PIN or city on the bill")
    assert pin.get("source") == "bill" and pin.get("state_code") == "MH", (
        f"PIN 411005 came back as {pin.get('label')!r}, state {pin.get('state_code')!r}, source {pin.get('source')!r}. "
        "The PIN lookups (Nominatim, Open-Meteo geocoding) failed above, so the app would ask for a city")
    return f"GPS -> {gps['label']} | PIN 411005 -> {pin['label']}"


def p_now():
    from surya import weather
    n = weather.now(*DELHI)
    assert n, "weather.now returned nothing"
    return f"{n['temp_c']}°C, {n['watts_per_kw']} W per kW right now, today so far {n['today_kwh_per_kw_so_far']} kWh/kW"


def p_report():
    from surya import analysis
    r = analysis.analyze(monthly_units=512, location={"lat": DELHI[0], "lon": DELHI[1], "city": "New Delhi", "state_code": "DL"},
                         bill={"discom": "BSES Rajdhani", "state": "DL", "units_consumed": 512, "energy_charges": 2890, "sanctioned_load_kw": 5})
    h, b, i = r["history"], r["breakeven"], r["impact"]
    assert h["mode"] == "weather", "report fell back to a typical year: " + "; ".join(w["code"] for w in r["warnings"])
    l12, l60 = h["last12"], h["last60"]
    print(f"          {r['plan']['kw']} kW / {r['per_panel']['panels']} panels | last 12 months {l12['kwh']:,} units = ₹{l12['inr']:,} | "
          f"last 5 years {l60['kwh']:,} units = ₹{l60['inr']:,}")
    print(f"          per panel ₹{r['per_panel']['inr_per_panel_year']:,}/yr | free after {b['free_after_years']} years | "
          f"coal not burned {i['per_year']['coal_kg']:,} kg/yr | calibration {h['calibration']}")
    if r["warnings"]:
        print("          warnings:", ", ".join(w["code"] for w in r["warnings"]))
    ratio = (h["calibration"] or {}).get("raw_ratio")
    if ratio and not 0.85 <= ratio <= 1.15:
        print(f"          NOTE: weather record / PVGIS = {ratio} (outside 0.85-1.15); results are anchored to PVGIS")
    return "full report built from live data"


def main() -> int:
    print("Surya Sahayak: live service check\n")
    print("Weather and solar data")
    probe("Open-Meteo history (archive)", p_archive, "5-year view falls back to NASA POWER, then to a typical year with a warning")
    probe("Open-Meteo live forecast", p_forecast, "the 'right now' strip is simply left out")
    probe("NASA POWER daily", p_nasa, "only matters if Open-Meteo history is also down")
    probe("PVGIS typical-year yield", p_pvgis, "yield comes from the 5-year weather average instead")
    print("\nPlace lookup")
    probe("Nominatim: GPS -> city", p_nominatim_rev, "BigDataCloud is used next")
    probe("Nominatim: PIN -> place", p_nominatim_pin, "Open-Meteo geocoding is used next")
    probe("BigDataCloud: GPS -> city", p_bigdata, "only a backup for Nominatim")
    probe("Open-Meteo geocoding: city", p_om_geocode, "typed cities fall back to Nominatim")
    probe("ipwho.is (network address)", p_ipwho, "ip-api.com is used next")
    probe("ip-api.com (network address)", p_ipapi, "the state capital from the bill is used, or the user is asked")
    print("\nThe app's own code, end to end")
    probe("5-year weather history", p_history, "report shows a typical year plus a clear warning")
    probe("Location from GPS and from a PIN", p_geo, "the user is asked to type a city or PIN")
    probe("Live 'right now' numbers", p_now, "the strip is left out")
    probe("Whole report, Delhi, 512 units", p_report, "see the lines above")

    bad = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(bad)} of {len(RESULTS)} checks OK.")
    if bad:
        print("Needs attention:", "; ".join(bad))
        print("The app still runs when a backup-covered service is down, but paste this output back so the real cause can be fixed.")
    else:
        print("Everything the app depends on is reachable from this machine.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
