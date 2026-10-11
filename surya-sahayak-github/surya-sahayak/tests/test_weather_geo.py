from datetime import date

import pytest
import requests

import fakes
from surya import geo, states, weather


# ---------------------------------------------------------------- weather
def test_window_ends_on_last_full_month_with_data():
    w = weather.window(date(2026, 10, 11))
    assert w["months"][0] == "2021-10" and w["months"][-1] == "2026-09" and len(w["months"]) == 60
    assert w["start"] == date(2021, 10, 1) and w["end"] == date(2026, 9, 30)
    assert weather.window(date(2026, 10, 6))["months"][-1] == "2026-09"      # Sep 30 is >= 6 days old
    assert weather.window(date(2026, 10, 5))["months"][-1] == "2026-08"      # not yet
    assert weather.window(date(2027, 1, 3))["months"][-1] == "2026-11"
    assert weather.window(date(2027, 1, 7))["months"][-1] == "2026-12"       # December rolls the year


def test_optimal_tilt_delhi():
    assert 25.0 <= weather.optimal_tilt(28.6) <= 27.0


def test_pv_hour_physics():
    # 1000 W/m² at 25 °C air: cell ≈ 58.75 °C -> -11.8% temperature loss, then 14% system loss
    assert weather.pv_hour_kwh_per_kw(1000, 25) == pytest.approx(0.7585, abs=0.002)
    assert weather.pv_hour_kwh_per_kw(0, 30) == 0 and weather.pv_hour_kwh_per_kw(None, 30) == 0
    assert weather.pv_hour_kwh_per_kw(500, 45) < weather.pv_hour_kwh_per_kw(500, 15)   # hotter = less


def test_history_from_open_meteo(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(calls=calls))
    h = weather.history(28.55, 77.2, today=date(2026, 10, 11))
    assert "Open-Meteo" in h["source"] and len(h["months"]) == 60
    assert all(m["kwh_per_kw"] > 40 for m in h["months"])
    annual = sum(m["kwh_per_kw"] for m in h["months"]) / 5
    assert 1000 < annual < 1900
    assert len(calls) == 6                              # one request per calendar year touched
    first = calls[0][1]
    assert first["tilt"] == weather.optimal_tilt(28.55) and first["azimuth"] == 0
    # a second call is served from the disk cache
    calls.clear()
    weather.history(28.55, 77.2, today=date(2026, 10, 11))
    assert calls == []


def test_history_falls_back_to_nasa(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"om"}))
    h = weather.history(28.55, 77.2, today=date(2026, 10, 11))
    assert "NASA" in h["source"] and len(h["months"]) == 60
    assert 1000 < sum(m["kwh_per_kw"] for m in h["months"]) / 5 < 2000


def test_history_raises_when_everything_is_down(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"om", "nasa"}))
    with pytest.raises(weather.WeatherUnavailable):
        weather.history(28.55, 77.2, today=date(2026, 10, 11))


def test_history_rejects_a_response_full_of_gaps(monkeypatch):
    base = fakes.make_fake_get()

    def gappy(url, params=None, **kw):
        r = base(url, params, **kw)
        if "archive-api" in url:
            r._payload["hourly"]["global_tilted_irradiance"] = [None] * len(r._payload["hourly"]["time"])
        return r
    monkeypatch.setattr(requests, "get", gappy)
    assert "NASA" in weather.history(28.55, 77.2, today=date(2026, 10, 11))["source"]


def test_now_reports_watts_and_never_raises(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"forecast"}))
    assert weather.now(28.55, 77.2) is None
    monkeypatch.setattr(requests, "get", fakes.make_fake_get())
    n = weather.now(28.55, 77.2)
    assert n["sky"] == "clear" and n["temp_c"] == 31.4 and n["today_kwh_per_kw_forecast"] > 2


# ---------------------------------------------------------------- states
def test_state_codes():
    assert states.state_code("DL") == "DL" and states.state_code("IN-DL") == "DL"
    assert states.state_code("Delhi") == "DL" and states.state_code("NCT of Delhi") == "DL"
    assert states.state_code("Orissa") == "OD" and states.state_code("TS") == "TG"
    assert states.state_code("Jammu & Kashmir") == "JK"
    assert states.state_code("Atlantis") is None and states.state_code(None) is None
    assert states.state_from_discom("Tata Power-DDL") == "DL"
    assert states.state_from_discom("Tata Power Mumbai") == "MH"
    assert states.state_from_discom("BESCOM") == "KA"


# ---------------------------------------------------------------- geo
def test_extract_pincode():
    assert geo.extract_pincode("H-12, Hauz Khas, New Delhi - 110016") == "110016"
    assert geo.extract_pincode("PIN 110 016") == "110016"
    assert geo.extract_pincode("CA no 1234567890") is None            # 10 digits is not a PIN
    assert geo.extract_pincode("012345") is None and geo.extract_pincode(None) is None


def test_reverse_geocode_delhi():
    r = geo.reverse(28.5494, 77.2001)
    assert r["city"] == "New Delhi" and r["locality"] == "Hauz Khas"
    assert r["state_code"] == "DL" and r["pincode"] == "110016" and r["country_code"] == "IN"


def test_reverse_falls_back_to_bigdatacloud(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"nominatim"}))
    r = geo.reverse(28.5494, 77.2001)
    assert r["city"] == "New Delhi" and r["state_code"] == "DL" and r["via"] == "BigDataCloud"


def test_reverse_returns_none_when_all_services_fail(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"nominatim", "bigdata"}))
    assert geo.reverse(28.5494, 77.2001) is None


def test_forward_by_pincode_and_city(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(search=fakes.NOMINATIM_GURUGRAM))
    r = geo.forward(pincode="122001")
    assert r["state_code"] == "HR" and abs(r["lat"] - 28.4595) < 1e-3
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"nominatim"}))
    r = geo.forward(city="Gurugram")                                   # Open-Meteo geocoding
    assert r["city"] == "Gurugram" and r["state_code"] == "HR"


def test_forward_ignores_places_outside_india(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(search=fakes.NOMINATIM_SEATTLE, calls=calls,
                                                              fail={"omgeo"}))
    assert geo.forward(pincode="999999") is None


def test_locate_gps_only():
    loc = geo.locate(lat=28.5494, lon=77.2001, accuracy_m=30)
    assert loc["source"] == "gps" and loc["state_code"] == "DL" and loc["label"] == "Hauz Khas, New Delhi"
    assert loc["accuracy_m"] == 30 and not loc["needs_manual"]


def test_locate_gps_outside_india_uses_the_bill():
    loc = geo.locate(lat=47.6, lon=-122.3, bill={"pincode": "110016", "state": "DL"})
    assert loc["source"] == "bill" and "gps_outside_india" in loc["notes"]


def test_locate_bill_beats_gps_when_states_differ(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(reverse=fakes.NOMINATIM_GURUGRAM, search=fakes.NOMINATIM_DELHI))
    bill = {"state": "DL", "discom": "BSES Rajdhani", "pincode": "110016"}
    loc = geo.locate(lat=28.4595, lon=77.0266, bill=bill)
    assert loc["source"] == "bill" and loc["state_code"] == "DL"
    assert "gps_differs_from_bill" in loc["notes"] and loc["alternatives"][0]["state_code"] == "HR"
    again = geo.locate(lat=28.4595, lon=77.0266, bill=bill, prefer="gps")
    assert again["source"] == "gps" and again["state_code"] == "HR"


def test_locate_same_state_keeps_gps():
    loc = geo.locate(lat=28.5494, lon=77.2001, bill={"state": "DL", "pincode": "110016"})
    assert loc["source"] == "gps" and loc["alternatives"] == []


def test_locate_bill_pincode_then_ip_then_capital(monkeypatch):
    assert geo.locate(bill={"pincode": "110016"})["source"] == "bill"
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"nominatim", "omgeo"}))
    loc = geo.locate(bill={"discom": "BSES"}, ip="49.36.10.20")
    assert loc["source"] == "ip" and "approximate_from_network" in loc["notes"]
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"nominatim", "omgeo", "ipwho", "ipapi"}))
    loc = geo.locate(bill={"discom": "BSES"}, ip="49.36.10.20")
    assert loc["source"] == "state_capital" and loc["state_code"] == "DL"


def test_locate_private_ip_and_no_clues_needs_manual():
    loc = geo.locate(ip="10.0.0.5")
    assert loc["needs_manual"] and loc["lat"] is None


def test_locate_typed_query_wins(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(search=fakes.NOMINATIM_GURUGRAM))
    loc = geo.locate(lat=28.5494, lon=77.2001, query="122001")
    assert loc["source"] == "manual" and loc["state_code"] == "HR"


def test_label_does_not_repeat_the_state():
    assert geo._label({"locality": "Hauz Khas", "city": "New Delhi", "state": "Delhi"}) == "Hauz Khas, New Delhi"
    assert geo._label({"locality": "Shivajinagar", "city": "Pune", "state": "Maharashtra"}) == "Shivajinagar, Pune, Maharashtra"
    assert geo._label({"city": "Delhi", "state": "Delhi"}) == "Delhi"
    assert geo._label({}) == "Your location"
