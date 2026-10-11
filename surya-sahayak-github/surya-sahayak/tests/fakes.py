"""Test doubles for the outside world (Open-Meteo, NASA POWER, PVGIS, map services).

USED ONLY BY TESTS. Nothing here ships as app data: in production every number comes from the real
services, and when they fail the app says so instead of inventing anything.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import requests

PVGIS_SHARE = [0.066, 0.075, 0.093, 0.102, 0.105, 0.095, 0.081, 0.080, 0.086, 0.088, 0.074, 0.055]  # sums to 1.0

NOMINATIM_DELHI = {"place_id": 1, "lat": "28.5494", "lon": "77.2001", "address": {
    "suburb": "Hauz Khas", "city": "New Delhi", "state_district": "South Delhi", "state": "Delhi",
    "ISO3166-2-lvl4": "IN-DL", "postcode": "110016", "country": "India", "country_code": "in"}}
NOMINATIM_GURUGRAM = {"place_id": 2, "lat": "28.4595", "lon": "77.0266", "address": {
    "city": "Gurugram", "state_district": "Gurugram", "state": "Haryana",
    "ISO3166-2-lvl4": "IN-HR", "postcode": "122001", "country": "India", "country_code": "in"}}
NOMINATIM_SEATTLE = {"place_id": 3, "lat": "47.6", "lon": "-122.3", "address": {
    "city": "Seattle", "state": "Washington", "ISO3166-2-lvl4": "US-WA", "country_code": "us"}}


OM_PLACES = {
    "gurugram": {"name": "Gurugram", "latitude": 28.4595, "longitude": 77.0266, "country_code": "IN",
                 "admin1": "Haryana", "admin2": "Gurugram", "postcodes": ["122001"]},
    "pune": {"name": "Pune", "latitude": 18.5204, "longitude": 73.8567, "country_code": "IN",
             "admin1": "Maharashtra", "admin2": "Pune", "postcodes": ["411001"]},
    "new delhi": {"name": "New Delhi", "latitude": 28.6139, "longitude": 77.209, "country_code": "IN",
                  "admin1": "Delhi", "admin2": "New Delhi", "postcodes": ["110001"]},
}


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def hourly_series(start: date, end: date, peak: float = 680.0, local: bool = False):
    """Synthetic but physically-shaped hourly GTI (W/m²) + temperature, in GMT hours."""
    times, gti, temp = [], [], []
    d = start
    while d <= end:
        doy = d.timetuple().tm_yday
        season = 0.80 + 0.20 * math.cos((doy - 110) / 365 * 2 * math.pi)
        t = round(25 + 9 * math.sin((doy - 90) / 365 * 2 * math.pi), 1)
        for h in range(24):
            if local:                                    # timezone=auto: the sun is up 06:00-18:00 local
                sun = math.sin(math.pi * (h - 6) / 12) if 6 <= h <= 18 else 0.0
            else:                                        # archive is GMT: ≈ 06:00-18:00 IST is 00:30-12:30
                sun = math.sin(math.pi * (h - 0.5) / 12) if 0.5 <= h <= 12.5 else 0.0
            times.append(f"{d.isoformat()}T{h:02d}:00")
            gti.append(round(peak * max(sun, 0.0) * season, 1))
            temp.append(t)
        d += timedelta(days=1)
    return times, gti, temp


def make_fake_get(*, fail=(), pvgis_annual=1450.0, reverse=NOMINATIM_DELHI, search=NOMINATIM_DELHI,
                  peak=680.0, calls=None):
    """Return a replacement for requests.get. `fail` names providers that should raise ConnectionError:
    om, nasa, pvgis, forecast, nominatim, bigdata, omgeo, ipwho, ipapi."""
    fail = set(fail)

    def get(url, params=None, headers=None, timeout=None, **kw):
        params = params or {}
        if calls is not None:
            calls.append((url, dict(params)))

        def boom(name):
            if name in fail:
                raise requests.ConnectionError(f"{name} unreachable (test)")

        if "archive-api.open-meteo.com" in url:
            boom("om")
            assert params["hourly"] == "global_tilted_irradiance,temperature_2m" and "tilt" in params
            s, e = date.fromisoformat(params["start_date"]), date.fromisoformat(params["end_date"])
            times, gti, temp = hourly_series(s, e, peak)
            return FakeResp({"latitude": params["latitude"], "longitude": params["longitude"], "hourly": {
                "time": times, "global_tilted_irradiance": gti, "temperature_2m": temp}})
        if "power.larc.nasa.gov" in url:
            boom("nasa")
            s, e = date.fromisoformat(params["start"][:4] + "-" + params["start"][4:6] + "-" + params["start"][6:]), \
                date.fromisoformat(params["end"][:4] + "-" + params["end"][4:6] + "-" + params["end"][6:])
            ghi, t2m, d = {}, {}, s
            while d <= e:
                doy = d.timetuple().tm_yday
                ghi[d.strftime("%Y%m%d")] = round(5.3 + 1.2 * math.cos((doy - 110) / 365 * 2 * math.pi), 2)
                t2m[d.strftime("%Y%m%d")] = round(25 + 9 * math.sin((doy - 90) / 365 * 2 * math.pi), 1)
                d += timedelta(days=1)
            return FakeResp({"properties": {"parameter": {"ALLSKY_SFC_SW_DWN": ghi, "T2M": t2m}}})
        if "re.jrc.ec.europa.eu" in url:
            boom("pvgis")
            return FakeResp({"outputs": {
                "monthly": {"fixed": [{"month": i + 1, "E_m": round(pvgis_annual * s, 2)} for i, s in enumerate(PVGIS_SHARE)]},
                "totals": {"fixed": {"E_y": pvgis_annual}}}})
        if "api.open-meteo.com/v1/forecast" in url:
            boom("forecast")
            times, gti, temp = hourly_series(date(2026, 10, 11), date(2026, 10, 11), peak, local=True)
            return FakeResp({"current": {"time": "2026-10-11T14:15", "temperature_2m": 31.4, "cloud_cover": 12, "is_day": 1},
                             "hourly": {"time": [t.replace("T", "T") for t in times],
                                        "global_tilted_irradiance": gti, "temperature_2m": temp}})
        if "nominatim.openstreetmap.org/reverse" in url:
            boom("nominatim")
            return FakeResp(reverse)
        if "nominatim.openstreetmap.org/search" in url:
            boom("nominatim")
            return FakeResp([search] if search else [])
        if "bigdatacloud" in url:
            boom("bigdata")
            return FakeResp({"city": "New Delhi", "locality": "Hauz Khas", "principalSubdivision": "Delhi",
                             "principalSubdivisionCode": "IN-DL", "countryCode": "IN", "postcode": "110016"})
        if "geocoding-api.open-meteo.com" in url:
            boom("omgeo")
            name = str(params.get("name", "")).strip().lower()
            place = OM_PLACES.get(name, OM_PLACES["gurugram"])          # unknown names: legacy default
            return FakeResp({"results": [place]})
        if "ipwho.is" in url:
            boom("ipwho")
            return FakeResp({"success": True, "city": "Delhi", "region": "Delhi", "region_code": "DL",
                             "country_code": "IN", "latitude": 28.65, "longitude": 77.23, "postal": "110001"})
        if "ip-api.com" in url:
            boom("ipapi")
            return FakeResp({"status": "success", "city": "Delhi", "regionName": "Delhi", "region": "DL",
                             "countryCode": "IN", "lat": 28.65, "lon": 77.23, "zip": "110001"})
        raise AssertionError(f"unexpected outbound call in test: {url}")

    return get
