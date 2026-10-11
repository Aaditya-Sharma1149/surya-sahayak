"""Real historical weather -> what a rooftop system *would have* produced.

Primary   Open-Meteo Historical Weather API (ERA5 reanalysis): hourly irradiance already
          projected on the tilted panel plane (`global_tilted_irradiance`) + 2 m air temperature.
Fallback  NASA POWER daily all-sky irradiance + temperature, with a fixed tilt gain.

Output is kWh per kW of panels for every calendar month. Nothing here knows about money.
No fake data: if both services fail we raise WeatherUnavailable and the caller says so.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import requests

from . import policy

log = logging.getLogger("surya.weather")

OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
NASA_POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
HEADERS = {"User-Agent": "SuryaSahayak/1.0 (AWS hackathon demo)"}   # never put personal data here

LAG_DAYS = 6              # ERA5 is ~5 days behind real time
NASA_TILT_GAIN = 1.08     # tilted-plane vs horizontal for ~26° south-facing in India (fallback only)
MODEL_VERSION = "1"       # bump to invalidate the disk cache when the physics changes


class WeatherUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ geometry / time
def optimal_tilt(lat: float) -> float:
    """Jacobson (2018) fit for the best fixed tilt of a south-facing panel (≈26° at Delhi)."""
    p = abs(lat)
    t = 1.3793 + p * (1.2011 + p * (-0.014404 + p * 0.000080509))
    return round(min(max(t, 5.0), 40.0), 1)


def _prev_month(y: int, m: int) -> tuple[int, int]:
    return (y - 1, 12) if m == 1 else (y, m - 1)


def window(today: date | None = None, months: int = 60) -> dict:
    """The last `months` FULL calendar months for which ERA5 data is already available."""
    today = today or date.today()
    cutoff = today - timedelta(days=LAG_DAYS)
    next_first = (cutoff.replace(day=1) + timedelta(days=32)).replace(day=1)
    last_day = next_first - timedelta(days=1)
    y, m = (cutoff.year, cutoff.month) if cutoff >= last_day else _prev_month(cutoff.year, cutoff.month)
    yms = []
    for _ in range(months):
        yms.append(f"{y:04d}-{m:02d}")
        y, m = _prev_month(y, m)
    yms.reverse()
    sy, sm = int(yms[0][:4]), int(yms[0][5:])
    ey, em = int(yms[-1][:4]), int(yms[-1][5:])
    end = date(ey + (em == 12), em % 12 + 1, 1) - timedelta(days=1)
    return {"start": date(sy, sm, 1), "end": end, "months": yms}


def _chunks(win: dict) -> list[tuple[date, date]]:
    out, y = [], win["start"].year
    while y <= win["end"].year:
        out.append((max(win["start"], date(y, 1, 1)), min(win["end"], date(y, 12, 31))))
        y += 1
    return out


# ------------------------------------------------------------------ physics
def pv_hour_kwh_per_kw(g_w_m2: float, temp_c: float | None) -> float:
    """kWh produced in one hour by 1 kWp, given plane-of-array irradiance (W/m²) and air temperature."""
    p = policy.PANEL
    if g_w_m2 is None or g_w_m2 <= 0:
        return 0.0
    ta = 25.0 if temp_c is None else temp_c
    cell = ta + p["cell_temp_rise_per_wm2"] * g_w_m2
    return (g_w_m2 / 1000.0) * (1 + p["temp_coeff_per_c"] * (cell - 25.0)) * (1 - p["system_loss"])


def _monthly_from_hourly(times: list, gti: list, temp: list) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for i, t in enumerate(times):
        g = gti[i] if i < len(gti) else None
        if g is None:
            continue
        ta = temp[i] if i < len(temp) else None
        out[str(t)[:7]] += pv_hour_kwh_per_kw(g, ta)
    return out


# ------------------------------------------------------------------ cache + http
def _cache_dir() -> Path:
    p = Path(os.getenv("SURYA_CACHE_DIR") or Path(__file__).resolve().parent.parent / ".cache")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cache_get(key: str, ttl: float):
    f = _cache_dir() / f"{key}.json"
    try:
        if f.exists() and time.time() - f.stat().st_mtime < ttl:
            return json.loads(f.read_text())
    except Exception:
        pass
    return None


def _cache_put(key: str, value) -> None:
    try:
        (_cache_dir() / f"{key}.json").write_text(json.dumps(value))
    except Exception as e:                       # a read-only disk must never break a request
        log.warning("weather cache write failed: %s", e)


def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in (MODEL_VERSION, *parts)).encode()).hexdigest()


def _get_json(url: str, params: dict, timeout: float, tries: int = 3):
    last: Exception | None = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        except requests.RequestException as e:
            last = e
            time.sleep(0.8 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(1.5 * (i + 1))
            continue
        if r.status_code >= 400:
            reason = ""
            try:
                reason = str(r.json().get("reason", ""))
            except Exception:
                pass
            raise RuntimeError(f"HTTP {r.status_code} {reason}".strip())
        return r.json()
    raise last or RuntimeError("request failed")


def _ttl_for(chunk_end: date, today: date) -> float:
    """Finished periods never change much: keep ~6 months. Recent ones: 12 h."""
    return 180 * 86400 if chunk_end < today - timedelta(days=40) else 12 * 3600


# ------------------------------------------------------------------ providers
def _open_meteo_chunk(lat: float, lon: float, tilt: float, az: float, s: date, e: date,
                      today: date) -> dict[str, float]:
    key = _key("om", f"{lat:.2f}", f"{lon:.2f}", tilt, az, s, e)
    hit = _cache_get(key, _ttl_for(e, today))
    if hit is not None:
        return hit
    j = _get_json(OPEN_METEO_ARCHIVE, {
        "latitude": round(lat, 2), "longitude": round(lon, 2),      # ~1 km: all the weather grid needs, and not a street address
        "start_date": s.isoformat(), "end_date": e.isoformat(),
        "hourly": "global_tilted_irradiance,temperature_2m",
        "tilt": tilt, "azimuth": az, "timezone": "GMT"}, timeout=45)
    h = j.get("hourly") or {}
    times, gti, temp = h.get("time") or [], h.get("global_tilted_irradiance") or [], h.get("temperature_2m") or []
    if not times or len(gti) != len(times):
        raise ValueError("Open-Meteo response has no tilted-irradiance series")
    need = ((e - s).days + 1) * 24
    valid = sum(1 for g in gti if g is not None)
    if valid < 0.9 * need:
        raise ValueError(f"Open-Meteo returned {valid}/{need} hours")
    monthly = {k: round(v, 3) for k, v in _monthly_from_hourly(times, gti, temp).items()}
    _cache_put(key, monthly)
    return monthly


def _nasa_chunk(lat: float, lon: float, s: date, e: date, today: date) -> dict[str, float]:
    key = _key("nasa", f"{lat:.2f}", f"{lon:.2f}", s, e)
    hit = _cache_get(key, _ttl_for(e, today))
    if hit is not None:
        return hit
    j = _get_json(NASA_POWER, {
        "parameters": "ALLSKY_SFC_SW_DWN,T2M", "community": "RE", "format": "JSON",
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "start": s.strftime("%Y%m%d"), "end": e.strftime("%Y%m%d")}, timeout=60)
    par = (j.get("properties") or {}).get("parameter") or {}
    ghi, t2m = par.get("ALLSKY_SFC_SW_DWN") or {}, par.get("T2M") or {}
    p = policy.PANEL
    monthly: dict[str, float] = defaultdict(float)
    n = 0
    for d, g in ghi.items():
        if g is None or g < 0:                    # NASA uses -999 for "no data"
            continue
        ta = t2m.get(d)
        ta = 25.0 if (ta is None or ta < -90) else ta
        cell = ta + 3 + p["cell_temp_rise_per_wm2"] * (g * 1000 / 8)   # ≈8 effective sun-hours
        monthly[f"{d[:4]}-{d[4:6]}"] += g * NASA_TILT_GAIN * (1 + p["temp_coeff_per_c"] * (cell - 25)) * (1 - p["system_loss"])
        n += 1
    if n < 0.9 * ((e - s).days + 1):
        raise ValueError(f"NASA POWER returned {n} days")
    monthly = {k: round(v, 3) for k, v in monthly.items()}
    _cache_put(key, monthly)
    return monthly


def _collect(fn, chunks) -> dict[str, float]:
    with ThreadPoolExecutor(max_workers=min(6, len(chunks))) as ex:
        parts = list(ex.map(fn, chunks))
    merged: dict[str, float] = {}
    for p in parts:
        merged.update(p)
    return merged


# ------------------------------------------------------------------ public API
def history(lat: float, lon: float, *, months: int = 60, tilt: float | None = None,
            azimuth: float = 0.0, today: date | None = None) -> dict:
    """kWh per kW of panels for each of the last `months` full calendar months (uncalibrated)."""
    today = today or date.today()
    tilt = optimal_tilt(lat) if tilt is None else tilt
    azimuth = azimuth if lat >= 0 else 180.0
    win = window(today, months)
    chunks = _chunks(win)
    errors = []
    monthly, source = None, None
    try:
        monthly = _collect(lambda c: _open_meteo_chunk(lat, lon, tilt, azimuth, c[0], c[1], today), chunks)
        source = "Open-Meteo Historical Weather (ERA5 reanalysis), hourly irradiance on the panel plane"
    except Exception as e:
        errors.append(f"Open-Meteo: {type(e).__name__}: {e}")
        log.warning("Open-Meteo history failed: %s", e)
    if monthly is not None and any(ym not in monthly for ym in win["months"]):
        errors.append("Open-Meteo: months missing")
        monthly = None
    if monthly is None:
        try:
            monthly = _collect(lambda c: _nasa_chunk(lat, lon, c[0], c[1], today), chunks)
            source = "NASA POWER daily satellite/reanalysis irradiance"
        except Exception as e:
            errors.append(f"NASA POWER: {type(e).__name__}: {e}")
            log.warning("NASA POWER history failed: %s", e)
    if monthly is None or any(ym not in monthly for ym in win["months"]):
        raise WeatherUnavailable("; ".join(errors) or "no weather data")
    return {"source": source, "tilt": tilt, "azimuth": azimuth,
            "start": win["start"].isoformat(), "end": win["end"].isoformat(),
            "months": [{"ym": ym, "kwh_per_kw": monthly[ym]} for ym in win["months"]]}


def now(lat: float, lon: float, tilt: float | None = None, azimuth: float = 0.0) -> dict | None:
    """What 1 kW of panels is doing at this moment, plus today's forecast. Never raises."""
    try:
        tilt = optimal_tilt(lat) if tilt is None else tilt
        j = _get_json(OPEN_METEO_FORECAST, {
            "latitude": round(lat, 2), "longitude": round(lon, 2),
            "current": "temperature_2m,cloud_cover,is_day",
            "hourly": "global_tilted_irradiance,temperature_2m",
            "tilt": tilt, "azimuth": azimuth, "forecast_days": 1, "timezone": "auto"}, timeout=12, tries=2)
        cur, h = j.get("current") or {}, j.get("hourly") or {}
        times, gti, temp = h.get("time") or [], h.get("global_tilted_irradiance") or [], h.get("temperature_2m") or []
        if not times or len(gti) != len(times):
            return None
        stamp = str(cur.get("time") or "")[:13]
        idx = next((i for i, t in enumerate(times) if str(t)[:13] == stamp), None)
        if idx is None:
            idx = max(0, min(len(times) - 1, int(stamp[11:13] or 12))) if len(stamp) >= 13 else 12
        g = gti[idx] or 0.0
        ta = cur.get("temperature_2m", temp[idx] if idx < len(temp) else None)
        watts = round(pv_hour_kwh_per_kw(g, ta) * 1000)
        today_total = sum(pv_hour_kwh_per_kw(gti[i] or 0.0, temp[i] if i < len(temp) else None) for i in range(len(times)))
        so_far = sum(pv_hour_kwh_per_kw(gti[i] or 0.0, temp[i] if i < len(temp) else None) for i in range(idx + 1))
        cloud = cur.get("cloud_cover")
        sky = None if cloud is None else ("clear" if cloud < 20 else "partly_cloudy" if cloud < 60 else "cloudy")
        return {"time": cur.get("time"), "temp_c": ta, "cloud_pct": cloud, "sky": sky,
                "is_day": bool(cur.get("is_day", 1)), "irradiance_w_m2": round(g),
                "watts_per_kw": watts, "today_kwh_per_kw_so_far": round(so_far, 2),
                "today_kwh_per_kw_forecast": round(today_total, 2), "source": "Open-Meteo forecast"}
    except Exception as e:
        log.info("live weather unavailable: %s", e)
        return None
