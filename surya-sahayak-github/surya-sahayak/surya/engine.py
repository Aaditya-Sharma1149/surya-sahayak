"""Plan engine: sizing, generation (PVGIS), subsidy stack, bills, EMI, payback.

Pure functions, no AWS dependency — unit-test and demo these first.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict, field
from functools import lru_cache

import requests

from . import policy

PVGIS_URL = "https://re.jrc.ec.europa.eu/api/v5_3/PVcalc"
M2_PER_KW = 10.0          # MNRE rule of thumb: ~10 m² shadow-free per kW
USABLE_FRACTION = 0.75    # setbacks, water tank, staircase head-room
# Fallback Delhi profile (kWh per kWp per month, ~1,450/yr) if PVGIS is down.
DELHI_FALLBACK_MONTHLY = [95, 108, 135, 148, 152, 138, 118, 116, 124, 128, 107, 96]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------- generation
def pvgis_yield(lat: float, lon: float, kw: float = 1.0, tilt: float | None = None,
                loss: float = 14.0, timeout: float = 15.0) -> dict:
    """Monthly + annual kWh for `kw` peak power. Falls back to a Delhi profile."""
    # Round so nearby clicks on the map hit the cache (≈1 km grid).
    # Only successful PVGIS calls are cached; failures retry next time.
    try:
        monthly, annual = _pvgis_cached(round(lat, 2), round(lon, 2), kw, tilt, loss, timeout)
        return {"monthly_kwh": list(monthly), "annual_kwh": annual, "source": "PVGIS v5.3 (EU JRC)"}
    except Exception as e:  # network, quota, schema change
        monthly = [m * kw for m in DELHI_FALLBACK_MONTHLY]
        return {"monthly_kwh": monthly, "annual_kwh": sum(monthly),
                "source": f"fallback Delhi profile (PVGIS unavailable: {type(e).__name__})"}


@lru_cache(maxsize=256)
def _pvgis_cached(lat, lon, kw, tilt, loss, timeout):
    params = {"lat": lat, "lon": lon, "peakpower": kw, "loss": loss,
              "aspect": 0, "outputformat": "json"}
    if tilt is None:
        params["optimalangles"] = 1
    else:
        params["angle"] = tilt
    r = requests.get(PVGIS_URL, params=params, timeout=timeout)
    r.raise_for_status()
    out = r.json()["outputs"]
    return tuple(m["E_m"] for m in out["monthly"]["fixed"]), out["totals"]["fixed"]["E_y"]


# -------------------------------------------------------------------- sizing
def roof_capacity_kw(area_m2: float) -> float:
    return max(0.0, area_m2 * USABLE_FRACTION / M2_PER_KW)


def recommend_kw(monthly_units: float, roof_area_m2: float | None, yield_per_kw_annual: float,
                 sanctioned_load_kw: float | None = None, max_kw: float | None = None) -> dict:
    """Largest sensible system: the smallest of consumption, roof (if known), sanctioned load and a 10 kW cap."""
    max_kw = max_kw or policy.SYSTEM["max_residential_kw"]
    need = monthly_units * 12 / yield_per_kw_annual
    limits = {"consumption": need, "system_cap": max_kw}
    roof = None
    if roof_area_m2 is not None:                      # unknown roof = no roof limit (auto mode)
        roof = roof_capacity_kw(roof_area_m2)
        limits["roof"] = roof
    if sanctioned_load_kw:
        limits["sanctioned_load"] = sanctioned_load_kw
    kw = math.floor(min(limits.values()) * 2) / 2     # 0.5 kW steps, round down
    kw = max(kw, 1.0) if (roof is None or roof >= 1.0) else 0.0
    binding = min(limits, key=limits.get)
    return {"kw": kw, "limits_kw": {k: round(v, 2) for k, v in limits.items()},
            "binding_limit": binding, "roof_known": roof_area_m2 is not None,
            "roof_needed_m2": round(kw * M2_PER_KW / USABLE_FRACTION, 1)}


def price_per_kw(kw: float) -> float:
    """Benchmark ₹/kW (midpoint of the fair range), interpolated between the published sizes."""
    pts = sorted((k, (lo + hi) / 2) for k, (lo, hi) in policy.BENCHMARK_PER_KW.items())
    if kw <= pts[0][0]:
        return pts[0][1]
    if kw >= pts[-1][0]:
        return pts[-1][1]
    for (k0, p0), (k1, p1) in zip(pts, pts[1:]):
        if k0 <= kw <= k1:
            return p0 + (p1 - p0) * (kw - k0) / (k1 - k0)
    return pts[-1][1]


def gross_cost(kw: float) -> int:
    return int(round(kw * price_per_kw(kw)))


# ------------------------------------------------------------------ subsidies
def central_subsidy(kw: float) -> int:
    total, left = 0.0, kw
    for size, rate in policy.CENTRAL["slabs"]:
        take = min(left, size)
        total += take * rate
        left -= take
        if left <= 0:
            break
    return int(min(total, policy.CENTRAL["cap"]))


def delhi_subsidy(kw: float) -> int:
    d = policy.DELHI
    if kw >= d["max_kw"]:
        return d["capital_subsidy_3kw"]
    return int(kw * d["capital_subsidy_per_kw_assumed"])


def delhi_free_eligible(avg_monthly_units: float, kw: float) -> bool:
    return avg_monthly_units <= policy.DELHI["free_scheme_max_units"] and kw <= policy.DELHI["max_kw"]


# -------------------------------------------------------------------- tariffs
def delhi_bill(units: float, apply_govt_subsidy: bool = True) -> float:
    """Energy charge for `units` in a month, after Delhi's consumption subsidy."""
    t = policy.DELHI_TARIFF
    left, charge = max(units, 0), 0.0
    for size, rate in t["slabs"]:
        take = min(left, size)
        charge += take * rate
        left -= take
        if left <= 0:
            break
    if apply_govt_subsidy:
        if units <= t["zero_bill_upto"]:
            return 0.0
        if units <= t["half_subsidy_upto"]:
            return charge - min(charge * 0.5, t["half_subsidy_cap"])
    return charge


def monthly_savings(units: float, gen: float, flat_rate: float | None = None,
                    export_rate: float = policy.EXPORT_RATE_ASSUMED) -> dict:
    """Saving for one month. Uses Delhi slabs unless a flat ₹/unit is given."""
    net = units - gen
    if flat_rate is not None:
        before = units * flat_rate
        after = max(net, 0) * flat_rate
    else:
        before, after = delhi_bill(units), delhi_bill(max(net, 0))
    export_credit = max(-net, 0) * export_rate
    return {"bill_before": round(before), "bill_after": round(after),
            "export_credit": round(export_credit),
            "saving": round(before - after + export_credit)}


def subsidies(kw: float, state: str | None, monthly_units: float) -> dict:
    """Central + state money for a system of `kw` at a home using `monthly_units`."""
    is_delhi = (state or "").upper() in ("DL", "DELHI")
    return {"central": central_subsidy(kw), "state": delhi_subsidy(kw) if is_delhi else 0,
            "state_label": "Delhi Government" if is_delhi else None, "is_delhi": is_delhi,
            "free_scheme": bool(is_delhi and delhi_free_eligible(monthly_units, kw))}


def year_table(monthly_units: float, per_kw_monthly: list, kw: float, flat_rate: float | None) -> list:
    """12 typical months: generation, bill before/after, export credit and saving."""
    rows = []
    for i, per in enumerate(per_kw_monthly):
        g = per * kw
        rows.append({"month": MONTHS[i], "generation": round(g), **monthly_savings(monthly_units, g, flat_rate)})
    return rows


# ----------------------------------------------------------------------- loan
def emi(principal: float, annual_rate: float, months: int) -> float:
    if principal <= 0:
        return 0.0
    r = annual_rate / 12
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


# ----------------------------------------------------------------------- plan
@dataclass
class Plan:
    kw: float
    sizing: dict
    generation: dict
    gross_cost: int
    central_subsidy: int
    state_subsidy: int
    net_cost: int
    delhi_free_scheme: bool
    upfront_cost: int
    loan_principal: int
    emi: int
    avg_bill_before: int
    avg_bill_after: int
    avg_monthly_saving: int
    month_one_net: int                 # saving - EMI (the headline)
    payback_years: float | None
    lifetime_saving_25y: int
    monthly_table: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    assumptions_hi: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


def build_plan(*, monthly_units: float, lat: float, lon: float, roof_area_m2: float | None = None,
               state: str = "DL", sanctioned_load_kw: float | None = None,
               flat_tariff: float | None = None, quoted_cost: int | None = None,
               kw_override: float | None = None, use_loan: bool = True,
               degradation: float | None = None, tariff_escalation: float | None = None,
               per_kw: dict | None = None) -> Plan:
    """Size the system and compute subsidies, bills, EMI and payback for a typical year.

    `per_kw` lets the caller pass a yield it already fetched (same shape as pvgis_yield()).
    `roof_area_m2=None` means "roof unknown": size by consumption / sanctioned load only.
    """
    degradation = policy.PANEL["degradation_per_year"] if degradation is None else degradation
    tariff_escalation = policy.SYSTEM["tariff_escalation"] if tariff_escalation is None else tariff_escalation
    per_kw = per_kw or pvgis_yield(lat, lon, 1.0)
    sizing = recommend_kw(monthly_units, roof_area_m2, per_kw["annual_kwh"], sanctioned_load_kw)
    kw = kw_override or sizing["kw"]

    price = price_per_kw(kw)
    gross = int(quoted_cost or gross_cost(kw))
    sub = subsidies(kw, state, monthly_units)
    is_delhi, cfa, sfa, free = sub["is_delhi"], sub["central"], sub["state"], sub["free_scheme"]
    net = max(gross - cfa - sfa, 0)
    upfront = 0 if free else net

    rate = None if (is_delhi and flat_tariff is None) else (flat_tariff or 7.0)
    table = year_table(monthly_units, per_kw["monthly_kwh"], kw, rate)
    gen_monthly = [r["generation"] for r in table]
    avg_save = sum(r["saving"] for r in table) / 12
    avg_before = sum(r["bill_before"] for r in table) / 12
    avg_after = sum(r["bill_after"] for r in table) / 12

    L = policy.LOAN
    principal = 0 if (free or not use_loan) else net * (1 - L["margin_fraction"])
    m_emi = emi(principal, L["annual_rate"], L["tenure_months"])

    payback = None
    if upfront > 0 and avg_save > 0:
        payback = round(upfront / (avg_save * 12), 1)
    elif upfront == 0:
        payback = 0.0

    lifetime = sum(avg_save * 12 * (1 - degradation) ** y * (1 + tariff_escalation) ** y
                   for y in range(policy.PANEL["life_years"]))

    assumptions = [
        f"Generation: {per_kw['source']}, {policy.PANEL['system_loss']:.0%} system losses, south-facing, optimal tilt",
        f"Gross cost: {'your quote' if quoted_cost else f'benchmark ≈₹{round(price):,}/kW for this size'}",
        f"Central subsidy per {policy.CENTRAL['name']} (as of {policy.CENTRAL['as_of']})",
    ]
    if is_delhi:
        assumptions += [f"Delhi subsidy per {policy.DELHI['name']} (as of {policy.DELHI['as_of']})",
                        f"Bills use DERC slabs + Delhi consumption subsidy ({policy.DELHI_TARIFF['as_of']})",
                        f"Surplus export credited at ₹{policy.EXPORT_RATE_ASSUMED}/unit (assumed)"]
        if not free:
            assumptions.append("VERIFY: Delhi ₹78,000 subsidy assumed to apply above 400 units/month "
                               "(reported by installers, not yet in official guidelines)")
    else:
        assumptions.append(f"Tariff: ₹{rate:g}/unit flat; surplus exported at ₹{policy.EXPORT_RATE_ASSUMED}/unit (assumed)")
    if principal:
        assumptions.append(f"Loan: {L['annual_rate']:.0%} p.a., {L['tenure_months']//12} yrs, "
                           f"{L['margin_fraction']:.0%} down, subsidy used to prepay principal")
    assumptions.append("Savings shown as estimates; actual bills vary with usage and tariff orders")

    hi = [
        f"उत्पादन: {per_kw['source']}, {policy.PANEL['system_loss']:.0%} सिस्टम हानि, दक्षिणमुखी, सबसे अच्छा झुकाव",
        f"कुल लागत: {'आपका कोटेशन' if quoted_cost else f'इस साइज़ के लिए मानक दर ≈₹{round(price):,}/kW'}",
        f"केंद्र सब्सिडी: PM सूर्य घर: मुफ़्त बिजली योजना के अनुसार ({policy.CENTRAL['as_of']} तक)",
    ]
    if is_delhi:
        hi += [f"दिल्ली सब्सिडी: दिल्ली सौर ऊर्जा नीति 2023, दूसरा संशोधन ({policy.DELHI['as_of']} तक)",
               "बिल DERC स्लैब और दिल्ली की बिजली सब्सिडी से (नवीनतम DERC आदेश से जाँचें)",
               f"अतिरिक्त बिजली ग्रिड को ₹{policy.EXPORT_RATE_ASSUMED}/यूनिट पर (माना गया)"]
        if not free:
            hi.append("जाँचें: दिल्ली की ₹78,000 सब्सिडी 400 यूनिट/माह से ऊपर भी मानी गई है "
                      "(इंस्टॉलरों के अनुसार, अभी आधिकारिक दिशानिर्देशों में नहीं)")
    else:
        hi.append(f"टैरिफ़: ₹{rate:g}/यूनिट एक समान; अतिरिक्त बिजली ₹{policy.EXPORT_RATE_ASSUMED}/यूनिट पर (माना गया)")
    if principal:
        hi.append(f"लोन: {L['annual_rate']:.0%} सालाना, {L['tenure_months']//12} साल, "
                  f"{L['margin_fraction']:.0%} डाउन पेमेंट, सब्सिडी से मूलधन का पूर्व-भुगतान")
    hi.append("बचत अनुमान है; असली बिल खपत और टैरिफ़ आदेशों के साथ बदलते हैं")

    return Plan(kw=kw, sizing=sizing,
                generation={"annual_kwh": round(sum(gen_monthly)), "avg_monthly_kwh": round(sum(gen_monthly) / 12),
                            "kwh_per_kw_year": round(per_kw["annual_kwh"]),
                            "monthly_per_kw": [round(x, 1) for x in per_kw["monthly_kwh"]],
                            "source": per_kw["source"]},
                gross_cost=gross, central_subsidy=cfa, state_subsidy=sfa, net_cost=net,
                delhi_free_scheme=free, upfront_cost=upfront, loan_principal=round(principal),
                emi=round(m_emi), avg_bill_before=round(avg_before), avg_bill_after=round(avg_after),
                avg_monthly_saving=round(avg_save), month_one_net=round(avg_save - m_emi),
                payback_years=payback, lifetime_saving_25y=round(lifetime),
                monthly_table=table, assumptions=assumptions, assumptions_hi=hi)


def headline(plan: Plan, lang: str = "en") -> str:
    p = plan
    if p.delhi_free_scheme:
        if lang == "hi":
            return (f"आप दिल्ली की मुफ़्त सोलर योजना के पात्र हैं: {p.kw:g} kW सिस्टम, ₹0 अग्रिम लागत। "
                    f"आपका बिल ₹{p.avg_bill_before:,} से घटकर लगभग ₹{p.avg_bill_after:,} होगा — "
                    f"हर महीने करीब ₹{p.avg_monthly_saving:,} की बचत।")
        return (f"You qualify for Delhi's free rooftop solar: a {p.kw:g} kW system at ₹0 upfront. "
                f"Your bill drops from ₹{p.avg_bill_before:,} to about ₹{p.avg_bill_after:,} — "
                f"roughly ₹{p.avg_monthly_saving:,} a month back in your pocket.")
    sign = "ahead" if p.month_one_net >= 0 else "behind"
    if lang == "hi":
        word = "आगे" if p.month_one_net >= 0 else "पीछे"
        return (f"आपका बिल ₹{p.avg_bill_before:,} है। लोन EMI ₹{p.emi:,} होगी। "
                f"पहले महीने से आप लगभग ₹{abs(p.month_one_net):,} {word} रहेंगे।")
    return (f"Your bill is ₹{p.avg_bill_before:,}. The loan EMI is ₹{p.emi:,}. "
            f"You come out about ₹{abs(p.month_one_net):,} {sign} from month one.")


# Alias kept for compatibility with earlier snippets.
def central_subsidy_estimate(kw: float) -> int:
    if kw < 0:
        raise ValueError("system_kw cannot be negative")
    return central_subsidy(kw)
