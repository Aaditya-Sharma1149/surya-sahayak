import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from surya import engine, quote  # noqa: E402

SAMPLES = pathlib.Path(__file__).resolve().parents[1] / "samples"


def test_central_subsidy_slabs():
    assert engine.central_subsidy(1) == 30_000
    assert engine.central_subsidy(2) == 60_000
    assert engine.central_subsidy(2.5) == 69_000
    assert engine.central_subsidy(3) == 78_000
    assert engine.central_subsidy(10) == 78_000


def test_delhi_subsidy():
    assert engine.delhi_subsidy(3) == 78_000
    assert engine.delhi_subsidy(5) == 78_000


def test_delhi_bill_slabs_and_subsidy():
    assert engine.delhi_bill(150) == 0                    # free up to 200
    assert engine.delhi_bill(300) == 1050 - 525           # 600+450, 50% off
    assert engine.delhi_bill(500) == 600 + 900 + 650      # no subsidy above 400


def test_emi():
    assert round(engine.emi(100_000, 0.07, 120)) == 1161
    assert engine.emi(0, 0.07, 120) == 0


def test_free_scheme_plan():
    p = engine.build_plan(monthly_units=350, lat=28.55, lon=77.2, roof_area_m2=45)
    assert p.delhi_free_scheme and p.upfront_cost == 0 and p.emi == 0
    assert p.avg_monthly_saving > 0


def test_above_400_plan_has_emi():
    p = engine.build_plan(monthly_units=650, lat=28.55, lon=77.2, roof_area_m2=60)
    assert not p.delhi_free_scheme
    assert p.kw >= 3
    assert p.emi > 0 and p.upfront_cost > 0


def test_quote_auditor_catches_overpriced():
    r = quote.audit_quote((SAMPLES / "quote_overpriced.txt").read_text())
    assert r["price_verdict"] == "overpriced"
    assert r["subsidy_at_risk"]
    assert r["red_flags"][0]["severity"] == "high"


def test_quote_auditor_passes_fair():
    r = quote.audit_quote((SAMPLES / "quote_fair.txt").read_text())
    assert r["price_verdict"] == "fair"
    assert not r["subsidy_at_risk"]
    assert not [f for f in r["red_flags"] if f["severity"] == "high"]
