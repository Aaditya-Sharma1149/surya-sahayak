import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

from surya import bill, llm, quote  # noqa: E402
import api  # noqa: E402

SAMPLES = pathlib.Path(__file__).resolve().parents[1] / "samples"
client = TestClient(api.app)

FAKE_BILL = {"discom": "BSES Rajdhani", "state": None, "units_consumed": "512", "billing_days": 30,
             "sanctioned_load_kw": "5 kW", "energy_charges": "₹ 2,890.50", "total_payable": 3120,
             "consumption_history": [{"month": "2026-07", "units": 610}, {"month": "2026-08", "units": "580"},
                                     {"month": "2026-09", "units": 512}, {"month": "x", "units": None}],
             "confidence": "high", "notes": ""}


def jpeg_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (3000, 4000), "white").save(buf, "JPEG")
    return buf.getvalue()


# ---------- bill parsing
def test_parse_json_handles_fences_and_prose():
    assert llm.parse_json('Sure!\n```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}


def test_to_number():
    assert bill.to_number("₹ 1,234.50") == 1234.5
    assert bill.to_number("5 kW") == 5.0
    assert bill.to_number(None) is None


def test_clean_and_average():
    b = bill.clean(FAKE_BILL)
    assert b["state"] == "DL" and b["units_consumed"] == 512 and b["sanctioned_load_kw"] == 5
    assert len(b["consumption_history"]) == 3
    assert bill.monthly_average(b) == round((610 + 580 + 512) / 3, 1)


def test_two_monthly_bill_average():
    b = bill.clean({"units_consumed": 800, "billing_days": 60})
    assert bill.monthly_average(b) == 400


def test_image_is_shrunk_for_bedrock():
    data, fmt = llm.prepare_image(jpeg_bytes())
    assert fmt == "jpeg" and len(data) < llm.MAX_IMAGE_BYTES
    assert max(Image.open(io.BytesIO(data)).size) <= llm.MAX_SIDE_PX


# ---------- API
def test_health():
    assert client.get("/api/health").json()["ok"]


def test_plan_endpoint():
    r = client.post("/api/plan", json={"monthly_units": 650, "lat": 28.55, "lon": 77.2, "roof_area_m2": 60})
    assert r.status_code == 200
    d = r.json()
    assert d["emi"] > 0 and d["headline_en"].startswith("Your bill is")
    assert len(d["monthly_table"]) == 12


def test_plan_rejects_bad_input():
    r = client.post("/api/plan", json={"monthly_units": -5, "lat": 28.55, "lon": 77.2, "roof_area_m2": 60})
    assert r.status_code == 422


def test_bill_endpoint_with_mocked_bedrock(monkeypatch):
    monkeypatch.setattr(llm, "ask_vision_json", lambda *a, **k: dict(FAKE_BILL))
    r = client.post("/api/bill", files={"file": ("bill.jpg", jpeg_bytes(), "image/jpeg")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["units_consumed"] == 512 and d["avg_monthly_units"] > 500 and d["needs_review"] == []


def test_bill_endpoint_flags_missing_units(monkeypatch):
    monkeypatch.setattr(llm, "ask_vision_json", lambda *a, **k: {"discom": "TPDDL", "confidence": "low"})
    d = client.post("/api/bill", files={"file": ("b.png", jpeg_bytes(), "image/png")}).json()
    assert "units_consumed" in d["needs_review"] and "avg_monthly_units" in d["needs_review"]


def test_bill_rejects_wrong_type():
    r = client.post("/api/bill", files={"file": ("bill.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 415


def test_quote_text_endpoint():
    r = client.post("/api/quote", data={"text": (SAMPLES / "quote_overpriced.txt").read_text(),
                                        "recommended_kw": "3"})
    d = r.json()
    assert d["price_verdict"] == "overpriced" and d["subsidy_at_risk"]


def test_quote_file_txt_skips_llm():
    r = client.post("/api/quote", files={"file": ("q.txt", (SAMPLES / "quote_fair.txt").read_bytes(), "text/plain")})
    assert r.json()["price_verdict"] == "fair"


def test_quote_photo_with_mocked_bedrock(monkeypatch):
    fields = {"vendor_name": "X", "system_kw": 3, "total_price": "2,85,000", "panel_dcr": False,
              "line_items": ["panels", "inverter"], "full_text": "3 kW system Rs 2,85,000", "gst_included": False}
    monkeypatch.setattr(llm, "ask_vision_json", lambda *a, **k: fields)
    d = client.post("/api/quote", files={"file": ("q.jpg", jpeg_bytes(), "image/jpeg")}).json()
    assert d["price_verdict"] == "overpriced" and d["subsidy_at_risk"]
    assert any("GST" in f["issue"] for f in d["red_flags"])


def test_checklist():
    assert len(client.get("/api/checklist").json()["documents"]) >= 5
