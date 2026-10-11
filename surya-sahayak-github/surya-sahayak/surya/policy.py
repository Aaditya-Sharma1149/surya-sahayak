"""Subsidy, tariff and benchmark rules — the ONLY place policy numbers live.

Every block carries an `as_of` date and a `source`. Anything marked
VERIFY is a best-available figure that should be checked on the official
portal before you show it as fact in the demo:
  - National portal: https://pmsuryaghar.gov.in
  - Delhi portal:    https://solar.delhi.gov.in
"""

CENTRAL = {
    "name": "PM Surya Ghar: Muft Bijli Yojana (Central Financial Assistance)",
    "as_of": "2026-10-10",
    "source": "https://pmsuryaghar.gov.in",
    # ₹30,000/kW for kW 1-2, ₹18,000 for kW 3, nothing beyond. Cap ₹78,000.
    "slabs": [(2.0, 30_000), (1.0, 18_000)],
    "cap": 78_000,
    "conditions": [
        "Vendor must be registered/empanelled on the national portal",
        "Panels must be domestically manufactured (DCR modules); imported modules void the subsidy",
        "One central subsidy per household (grid-connected residential connection)",
        "Paid by DBT to your bank account ~30-45 days after commissioning",
    ],
    "conditions_hi": [
        "विक्रेता राष्ट्रीय पोर्टल पर पंजीकृत/सूचीबद्ध होना चाहिए",
        "पैनल देश में बने (DCR मॉड्यूल) होने चाहिए; आयातित मॉड्यूल पर सब्सिडी नहीं मिलती",
        "हर घर को एक ही केंद्र सब्सिडी (ग्रिड से जुड़ा घरेलू कनेक्शन)",
        "चालू होने के लगभग 30-45 दिन बाद DBT से आपके बैंक खाते में",
    ],
}

DELHI = {
    "name": "Delhi Solar Energy Policy 2023 — 2nd amendment (Cabinet, 2 Sep 2026)",
    "as_of": "2026-10-10",
    "source": "https://solar.delhi.gov.in",
    # Raised on 2 Sep 2026 to match central: ₹78,000 for 3 kW, paid upfront
    # (replaces the old ₹10k/kW capital subsidy + ₹3/unit GBI over 5 years).
    "capital_subsidy_3kw": 78_000,
    # VERIFY: per-kW scaling below 3 kW is not published; we pro-rate.
    "capital_subsidy_per_kw_assumed": 26_000,
    "max_kw": 3.0,
    # Households averaging <= 400 units/month (last 12 bills) get up to 3 kW
    # at zero upfront cost; Delhi tops up any gap between subsidies and the
    # IPGCL-selected vendor's price. Free O&M for 5 years.
    "free_scheme_max_units": 400,
    "free_scheme_note": "Zero upfront cost via IPGCL-selected vendors, subject to technical feasibility. Target: 2.3 lakh homes by March 2027.",
    "status_note": "Scheme guidelines and vendor list were still being notified in Sep 2026 — check the portal.",
}

# DERC domestic energy-charge slabs (₹/unit), monthly. VERIFY current order.
# Excludes fixed charges, PPAC surcharge, electricity duty — these are
# extracted from the bill photo where available.
DELHI_TARIFF = {
    "as_of": "2026-10-10 (VERIFY against latest DERC order)",
    "slabs": [(200, 3.00), (200, 4.50), (400, 6.50), (400, 7.00), (float("inf"), 8.00)],
    # Delhi govt consumption subsidy: 0 bill up to 200 units; 50% off (max ₹800) 201-400.
    "zero_bill_upto": 200,
    "half_subsidy_upto": 400,
    "half_subsidy_cap": 800,
}

# Surplus exported under net metering is settled at the discom's average
# power purchase cost. VERIFY — not published in the amendment coverage.
EXPORT_RATE_ASSUMED = 3.50

LOAN = {
    # Collateral-free scheme loans, ~repo + 0.5%. VERIFY with current repo rate.
    "annual_rate": 0.07,
    "tenure_months": 120,
    "margin_fraction": 0.10,   # typical 10% down payment
}

# Fair gross price ranges (₹, fully installed, on-grid, DCR panels), 2026.
BENCHMARK_PER_KW = {
    1: (55_000, 70_000),
    2: (55_000, 70_000),
    3: (53_000, 67_000),
    5: (52_000, 64_000),
    10: (48_000, 60_000),
}

REQUIRED_QUOTE_ITEMS = {
    "panels": ["panel", "module", "pv"],
    "panel_make_and_wattage": ["wp", "watt", "make", "brand"],
    "dcr_panels": ["dcr", "domestic content", "made in india", "domestic"],
    "inverter": ["inverter"],
    "mounting_structure": ["structure", "mounting", "gi ", "galvan"],
    "cabling_and_acdb_dcdb": ["cable", "acdb", "dcdb", "wiring"],
    "earthing_and_lightning_arrestor": ["earthing", "lightning", "arrestor", "la "],
    "net_meter": ["net meter", "net-meter", "bidirectional", "bi-directional"],
    "installation_and_commissioning": ["installation", "commissioning"],
    "warranty": ["warranty"],
    "subsidy_paperwork": ["subsidy", "portal", "documentation", "paperwork"],
}

DOCUMENT_CHECKLIST = [
    ("Latest electricity bill (name + CA/consumer number must match applicant)", "बिजली का नवीनतम बिल (नाम और CA नंबर आवेदक से मेल खाए)"),
    ("Aadhaar card", "आधार कार्ड"),
    ("Bank account linked to Aadhaar (for DBT), cancelled cheque or passbook", "आधार से जुड़ा बैंक खाता (DBT के लिए), कैंसिल चेक या पासबुक"),
    ("Proof of ownership / NOC for the roof", "छत का स्वामित्व प्रमाण / NOC"),
    ("Mobile number and email", "मोबाइल नंबर और ईमेल"),
    ("Passport-size photo", "पासपोर्ट साइज़ फोटो"),
    ("Vendor's empanelment ID and DCR panel certificate (from the vendor)", "विक्रेता की पंजीकरण ID और DCR पैनल प्रमाणपत्र (विक्रेता से)"),
]


# ---------------------------------------------------------------- system assumptions
# Not policy, but they drive every ₹ figure — kept here so they are easy to audit.
PANEL = {
    "watt_peak": 545,          # typical DCR module in 2026 quotes. VERIFY against your vendor's datasheet.
    "area_m2": 2.6,            # 2.28 m x 1.13 m
    "system_loss": 0.14,       # cables, inverter, dirt (PVGIS default)
    "temp_coeff_per_c": -0.0035,
    "cell_temp_rise_per_wm2": 0.03375,   # NOCT 47 °C: (47-20)/800
    "degradation_per_year": 0.005,
    "life_years": 25,
}
SYSTEM = {
    "tariff_escalation": 0.03,       # long-run grid tariff growth used for the optimistic payback case
    "inverter_replacement_year": 12,
    "inverter_per_kw": 8_000,        # VERIFY: rough 2026 price of a string inverter per kW
    "free_om_years": 5,              # Delhi scheme: 5 years free O&M (assumed for all)
    "om_per_kw_year": 300,           # cleaning + inspection after the free period
    "max_residential_kw": 10.0,
    "max_marginal_payback_years": 10,   # stop adding panels once the *extra* kW takes longer than this to pay back
    "yield_sanity_band": (850, 2100),  # kWh per kW per year plausible for India
}

# ---------------------------------------------------------------- pollution avoided
# Per kWh of grid electricity NOT drawn. Order-of-magnitude estimates (±20-30%): coal plants differ.
IMPACT = {
    "as_of": "2026-10-11",
    "label": "Estimates for the average Indian grid unit. Real plants differ by ±20–30%.",
    # CEA CO₂ Baseline Database, weighted average, FY 2023-24 (0.727 tCO₂/MWh). VERIFY: a newer
    # version (22.0, Sep 2026) exists on cea.nic.in.
    "grid_kg_co2_per_kwh": 0.727,
    # IPCC AR5 median life-cycle emissions of rooftop PV (41 gCO₂e/kWh) — subtracted so we do not over-claim.
    "solar_lifecycle_kg_co2_per_kwh": 0.041,
    "coal_share": 0.75,        # coal + lignite ≈ three-quarters of Indian generation (approx., VERIFY)
    # Per kWh generated by a coal plant (Guttikunda & Jawahar 2014, India 2010-11; SO₂/NOx/PM
    # derived from their national totals ÷ coal burned). Older data: newer plants may emit less.
    "per_coal_kwh": {"coal_kg": 0.70, "so2_g": 3.0, "nox_g": 2.9, "pm25_g": 0.84,
                     "ash_kg": 0.27,    # ≈38% ash in Indian coal. VERIFY with CEA fly-ash reports
                     "water_l": 3.5},   # MoEFCC 2015 norm for plants with cooling towers. VERIFY
    "petrol_kg_co2_per_litre": 2.31,
    "car_km_per_litre": 15,
    "two_wheeler_km_per_litre": 45,
    "tree_kg_co2_per_year": 21,
    "sources": [
        {"label": "Grid factor 0.727 tCO₂/MWh (FY 2023-24, CEA CO₂ Baseline Database) as reported by SolarQuarter",
         "url": "https://solarquarter.com/2025/01/06/co2-baseline-database-highlights-indias-progress-in-renewable-energy-and-emission-reduction-cea/"},
        {"label": "CEA CO₂ Baseline Database — User Guide v22.0 (newer edition, check its factor)",
         "url": "https://cea.nic.in/wp-content/uploads/baseline/2026/09/User_Guide__Version_22.0.pdf"},
        {"label": "Guttikunda & Jawahar (2014), Atmospheric emissions and pollution from the coal-fired thermal power plants in India",
         "url": "https://urbanemissions.info/wp-content/uploads/docs/2014-08-AE-Emissions-Health-Coal-PPs-India.pdf"},
        {"label": "IPCC AR5 WG3 Annex III (life-cycle emissions of PV)",
         "url": "https://www.ipcc.ch/report/ar5/wg3/"},
    ],
}

# ---------------------------------------------------------------- data credits (shown in the UI footer)
DATA_CREDITS = [
    {"label": "Weather: Open-Meteo.com (CC BY 4.0), ERA5 © ECMWF / Copernicus", "url": "https://open-meteo.com/"},
    {"label": "Solar yield: PVGIS © European Union, JRC", "url": "https://re.jrc.ec.europa.eu/pvg_tools/en/"},
    {"label": "Fallback weather: NASA POWER", "url": "https://power.larc.nasa.gov/"},
    {"label": "Maps & places: © OpenStreetMap contributors (ODbL), Esri World Imagery",
     "url": "https://www.openstreetmap.org/copyright"},
]

# ---------------------------------------------------------------- state top-ups (NOT counted in headline numbers)
# Reported by an installer-guide site (quickestimate.co, "as of mid-2026"); not confirmed with the
# state agencies. Shown to the user as "possible extra money — verify", never added to the plan.
STATE_TOPUPS = {
    "as_of": "mid-2026 (unverified)",
    "source": "https://quickestimate.co/blog/state-top-up-subsidies-pm-surya-ghar",
    "items": {
        "GJ": {"agency": "GEDA", "up_to": 10_000, "note": "Flat top-up for 1–10 kW homes; separate GEDA application.", "note_hi": "1–10 kW घरों के लिए एक तय राशि; GEDA में अलग आवेदन।"},
        "RJ": {"agency": "RRECL", "up_to": 15_000, "note": "₹10,000 (1–2 kW) or ₹15,000 (3 kW), first-come within the annual budget.", "note_hi": "₹10,000 (1–2 kW) या ₹15,000 (3 kW), सालाना बजट में पहले आओ-पहले पाओ।"},
        "HR": {"agency": "HAREDA", "up_to": 10_000, "note": "₹5,000 general, ₹10,000 for lower-income households; apply within 45 days of commissioning.", "note_hi": "₹5,000 सामान्य, कम आय वाले घरों के लिए ₹10,000; चालू होने के 45 दिन के भीतर आवेदन।"},
        "MP": {"agency": "MPUVN", "up_to": 12_000, "note": "₹8,000–12,000, urban, income-linked, intermittent funding.", "note_hi": "₹8,000–12,000, शहरी, आय से जुड़ी, फ़ंड बीच-बीच में।"},
    },
}
