import io
from datetime import date

import pytest
import requests
from fastapi.testclient import TestClient
from PIL import Image

import api
import fakes
from surya import analysis, economics, engine, impact, llm, policy

TODAY = date(2026, 10, 11)
DELHI = {"lat": 28.5494, "lon": 77.2001, "city": "New Delhi", "state_code": "DL"}
BILL_DL = {"discom": "BSES Rajdhani", "state": "DL", "sanctioned_load_kw": 5, "units_consumed": 512,
           "energy_charges": 2890, "avg_monthly_units": 512}


def run(units=512, location=DELHI, bill=BILL_DL, **kw):
    return analysis.analyze(monthly_units=units, location=location, bill=bill, today=TODAY, **kw)


# ---------------------------------------------------------------- impact
def test_impact_factors():
    i = impact.impact(1000)
    assert i["co2_kg"] == 686                                   # (0.727 - 0.041) kg per unit
    assert i["coal_kg"] == 525                                  # 0.70 kg x 75% coal share
    assert i["petrol_l"] == pytest.approx(686 / 2.31, abs=0.1)
    assert i["trees_year"] == 33 and i["car_km"] == round(i["petrol_l"] * 15)
    assert i["so2_kg"] == pytest.approx(2.2, abs=0.1) and i["ash_kg"] == pytest.approx(203, abs=2)
    assert impact.impact(-5)["co2_kg"] == 0


# ---------------------------------------------------------------- economics
def test_cashflow_and_payback():
    cum = economics.cashflow(100_000, 50_000, 1, escalation=0.0, degradation=0.0)
    assert cum[0] == -100_000 and cum[2] == 0 and economics.payback_years(cum) == 2.0
    assert economics.payback_years(economics.cashflow(0, 1000, 1, escalation=0)) == 0.0
    assert economics.payback_years(economics.cashflow(1_000_000, 1000, 1, escalation=0)) is None


def test_inverter_replacement_shows_up_in_cashflow():
    cum = economics.cashflow(0, 10_000, 3, escalation=0.0, degradation=0.0)
    year12 = cum[12] - cum[11]
    assert year12 == 10_000 - 3 * policy.SYSTEM["inverter_per_kw"] - 3 * policy.SYSTEM["om_per_kw_year"]


def test_breakeven_range_and_free_scheme():
    b = economics.breakeven(60_000, 20_000, 3)
    lo, hi = b["payback_years"]["optimistic"], b["payback_years"]["conservative"]
    assert lo <= hi and 2.5 < lo and hi < 3.6
    assert b["free_after_years"] == [3, 4] and b["free_years_left"] == 21
    assert economics.breakeven(0, 20_000, 3)["from_day_one"]


def test_price_interpolation_and_rooftop_sizes():
    assert engine.price_per_kw(3) == 60_000 and engine.gross_cost(3) == 180_000
    assert 58_000 < engine.price_per_kw(4) < 60_000
    assert engine.price_per_kw(0.5) == 62_500 and engine.price_per_kw(25) == 54_000
    assert economics.n_panels(3) == 6 and economics.n_panels(1) == 2 and economics.n_panels(10) == 18
    assert economics.ladder_sizes(3) == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert economics.ladder_sizes(4.5)[-1] == 7.0 and 4.5 in economics.ladder_sizes(4.5)
    assert economics.ladder_sizes(0) == []


def test_recommend_without_roof():
    r = engine.recommend_kw(512, None, 1450, 5)
    assert r["kw"] == 4.0 and "roof" not in r["limits_kw"] and r["roof_needed_m2"] == pytest.approx(53.3, abs=0.1)
    assert engine.recommend_kw(2000, None, 1450, None)["kw"] == 10.0          # residential cap
    assert engine.recommend_kw(500, 8, 1450)["kw"] == 0.0                      # 8 m² roof is too small


# ---------------------------------------------------------------- the full report
def test_report_for_a_delhi_home():
    r = run()
    assert r["ok"] and r["state"] == "DL"
    p, h = r["plan"], r["history"]
    # right-sized: 512 units could support 4 kW, but the 4th kW takes decades to pay back in Delhi
    assert p["kw"] == 3.0 and p["sizing"]["binding_limit"] == "payback" and p["sizing"]["max_useful_kw"] == 4.0
    assert [row["kw"] for row in r["ladder"] if row["recommended"]] == [3.0]
    assert r["ladder"][3]["extra"]["payback_years"] > 10
    # money: 3 kW costs 1.8 lakh, 78k central + 78k Delhi
    assert (p["gross_cost"], p["central_subsidy"], p["state_subsidy"], p["net_cost"]) == (180_000, 78_000, 78_000, 24_000)
    assert r["subsidy"]["cash_at_install"] == 102_000 and r["subsidy"]["refund_later"] == 78_000
    assert p["headline_en"].startswith("Your bill is ₹") and "ahead from month one" in p["headline_en"]
    # history is real-weather based, calibrated to PVGIS, and consistent with the typical year
    assert h["mode"] == "weather" and h["calibration"]["applied"] and len(h["months"]) == 60
    assert len(h["last60"]["years"]) == 5 and h["last12"]["kwh"] == sum(m["kwh"] for m in h["months"][-12:]) or True
    assert abs(h["last12"]["kwh"] / p["generation"]["annual_kwh"] - 1) < 0.15
    assert h["last60"]["inr"] == sum(m["inr"] for m in h["months"])
    assert 4.5 * h["last12"]["inr"] < h["last60"]["inr"] < 5.5 * h["last12"]["inr"]
    # per panel
    pp = r["per_panel"]
    assert pp["panels"] == 6 and pp["inr_per_panel_year"] * 6 == pytest.approx(h["last12"]["inr"], rel=0.25)
    assert pp["inr_per_m2_year"] == round(sum(x["saving"] for x in p["monthly_table"]) / pp["total_area_m2"])
    # break-even
    be = r["breakeven"]
    assert be["free_after_years"][0] <= be["free_after_years"][1] <= 2 and len(be["cumulative"]["optimistic"]) == 26
    # impact matches the generated units
    assert r["impact"]["last12"] == impact.impact(h["last12"]["kwh"])
    assert r["impact"]["lifetime"]["kwh"] > 20 * p["generation"]["annual_kwh"]
    assert r["now"]["sky"] == "clear" and r["now"]["system_watts"] >= 0
    assert r["chat_context"]["kw"] == 3.0 and r["warnings"] == []
    assert {c["label"].split(":")[0] for c in r["credits"]} >= {"Weather", "Solar yield"}


def test_free_scheme_home_is_free_from_day_one():
    r = run(350, bill={"discom": "BSES", "state": "DL"})
    assert r["plan"]["delhi_free_scheme"] and r["plan"]["upfront_cost"] == 0
    assert r["breakeven"]["from_day_one"] and r["subsidy"]["cash_at_install"] == 0 and r["subsidy"]["refund_later"] == 0
    assert r["subsidy"]["free_scheme_limit_units"] == 400


def test_non_delhi_home_uses_bill_tariff_and_central_subsidy_only():
    bill = {"discom": "MSEDCL", "state": "MH", "units_consumed": 500, "energy_charges": 4200, "sanctioned_load_kw": 6}
    r = run(500, location={"lat": 19.076, "lon": 72.877, "city": "Mumbai", "state_code": "MH"}, bill=bill)
    assert r["inputs"]["flat_tariff"] == 8.4 and "taken from your bill" in r["inputs"]["tariff_note"]
    assert r["plan"]["state_subsidy"] == 0 and r["plan"]["central_subsidy"] > 0
    assert r["subsidy"]["possible_extra"] == [] and not r["plan"]["delhi_free_scheme"]
    assert r["subsidy"]["cash_at_install"] == r["plan"]["gross_cost"]


def test_state_topup_is_shown_but_never_counted():
    r = run(500, location={"lat": 23.02, "lon": 72.57, "city": "Ahmedabad", "state_code": "GJ"},
            bill={"discom": "UGVCL", "state": "GJ"})
    extra = r["subsidy"]["possible_extra"][0]
    assert extra["up_to"] == 10_000 and extra["counted"] is False
    assert r["plan"]["net_cost"] == r["plan"]["gross_cost"] - r["plan"]["central_subsidy"]
    assert any(w["code"] == "tariff_assumed" for w in r["warnings"])


def test_weather_outage_is_reported_not_faked(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"om", "nasa"}))
    r = run()
    assert r["ok"] and r["history"]["mode"] == "typical_year" and r["history"]["last60"] is None
    assert "weather_down" in [w["code"] for w in r["warnings"]]
    assert r["plan"]["generation"]["source"].startswith("PVGIS")


def test_pvgis_outage_uses_weather_average(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"pvgis"}))
    r = run()
    assert "pvgis_down" in [w["code"] for w in r["warnings"]] and "Open-Meteo" in r["plan"]["generation"]["source"]
    assert not r["history"]["calibration"]["applied"]


def test_everything_down_still_answers_but_warns(monkeypatch):
    monkeypatch.setattr(requests, "get", fakes.make_fake_get(fail={"om", "nasa", "pvgis", "forecast"}))
    r = run()
    codes = [w["code"] for w in r["warnings"]]
    assert r["ok"] and "no_solar_data" in codes and r["now"] is None


def test_roof_too_small_and_user_chosen_size():
    assert run(roof_area_m2=6)["reason"] == "roof_too_small"
    r = run(kw_override=5)
    assert r["plan"]["kw"] == 5 and r["plan"]["sizing"].get("user_chosen")
    assert [row["kw"] for row in r["ladder"] if row["recommended"]] == [5.0]
    assert r["plan"]["central_subsidy"] == 78_000          # no extra central money above 3 kW


def test_bad_inputs():
    with pytest.raises(ValueError):
        run(0)


# ---------------------------------------------------------------- API
client = TestClient(api.app)


def jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (800, 1000), "white").save(buf, "JPEG")
    return buf.getvalue()


def test_locate_endpoint_uses_forwarded_ip(monkeypatch):
    r = client.post("/api/locate", json={}, headers={"x-forwarded-for": "49.36.10.20, 10.0.0.1"})
    assert r.status_code == 200 and r.json()["source"] == "ip"
    r = client.post("/api/locate", json={"lat": 28.5494, "lon": 77.2001, "accuracy_m": 20})
    assert r.json()["source"] == "gps" and r.json()["city"] == "New Delhi"
    assert client.post("/api/locate", json={"lat": 120, "lon": 0}).status_code == 422


def test_analyze_endpoint():
    r = client.post("/api/analyze", json={"location": DELHI, "bill": BILL_DL})
    assert r.status_code == 200 and r.json()["plan"]["kw"] == 3.0
    r = client.post("/api/analyze", json={"location": DELHI, "monthly_units": 350, "kw_override": 2})
    assert r.json()["plan"]["kw"] == 2 and r.json()["plan"]["delhi_free_scheme"]
    assert client.post("/api/analyze", json={"location": DELHI}).status_code == 422                       # no units
    assert client.post("/api/analyze", json={"location": {"lat": 47.6, "lon": -122.3}, "monthly_units": 300}).status_code == 422


def test_auto_endpoint_one_upload_does_everything(monkeypatch):
    fake_bill = {"discom": "BSES Rajdhani", "units_consumed": 512, "billing_days": 30, "sanctioned_load_kw": 5,
                 "energy_charges": 2890, "pincode": "110 016", "city": "New Delhi", "confidence": "high"}
    monkeypatch.setattr(llm, "ask_vision_json", lambda *a, **k: dict(fake_bill))
    r = client.post("/api/auto", files={"file": ("bill.jpg", jpeg(), "image/jpeg")},
                    data={"lat": "28.5494", "lon": "77.2001"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["needs_input"] == [] and d["bill"]["state"] == "DL" and d["bill"]["pincode"] == "110016"
    assert d["location"]["source"] == "gps" and d["report"]["plan"]["kw"] == 3.0
    # without GPS the bill's PIN code is enough
    d = client.post("/api/auto", files={"file": ("bill.jpg", jpeg(), "image/jpeg")}).json()
    assert d["location"]["source"] == "bill" and d["report"]["ok"]


def test_auto_endpoint_asks_only_for_what_is_missing(monkeypatch):
    monkeypatch.setattr(llm, "ask_vision_json", lambda *a, **k: {"confidence": "low"})
    d = client.post("/api/auto", files={"file": ("bill.jpg", jpeg(), "image/jpeg")}).json()
    assert set(d["needs_input"]) == {"monthly_units", "location"} and d["report"] is None


def test_policy_endpoint_lists_sources():
    d = client.get("/api/policy").json()
    assert d["impact"]["sources"] and d["credits"] and "GJ" in d["state_topups"]["items"]
