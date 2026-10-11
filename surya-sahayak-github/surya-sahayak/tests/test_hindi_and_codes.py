"""The page translates backend findings by code, and every English list has a Hindi twin of the same length."""
from fastapi.testclient import TestClient

import api
from surya import analysis, economics, quote


def test_health_says_whether_uploads_are_kept():
    j = TestClient(api.app).get("/api/health").json()
    assert j["ok"] is True and j["stores_uploads"] is False and j["max_upload_mb"] == 10


def test_every_quote_flag_has_a_code_the_page_can_translate():
    known = {"price_high", "price_low", "no_size_price", "no_dcr", "no_vendor_reg", "missing_item", "size_mismatch", "gst_excluded"}
    for text in (open("samples/quote_overpriced.txt").read(), open("samples/quote_fair.txt").read(), "nothing useful here"):
        q = quote.audit_quote(text, recommended_kw=5)
        assert q["red_flags"] or q["looks_good"]
        for f in q["red_flags"]:
            assert f["code"] in known and isinstance(f["p"], dict)
        assert len(q["looks_good_codes"]) == len(q["looks_good"])


def test_hindi_lists_match_the_english_ones():
    r = analysis.analyze(monthly_units=512, location={"lat": 28.55, "lon": 77.2, "city": "New Delhi", "state_code": "DL"},
                         bill={"discom": "BSES Rajdhani", "state": "DL", "units_consumed": 512, "energy_charges": 2890})
    assert len(r["plan"]["assumptions_hi"]) == len(r["plan"]["assumptions"])
    assert len(r["subsidy"]["conditions_hi"]) == len(r["subsidy"]["conditions"])
    b = economics.breakeven(50_000, 20_000, 3)
    assert len(b["assumptions_hi"]) == len(b["assumptions"])
