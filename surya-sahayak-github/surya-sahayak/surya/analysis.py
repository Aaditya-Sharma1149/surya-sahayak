"""One call that turns (bill numbers + a location) into the whole report.

real weather history  x  plan engine  x  subsidy  x  per-panel economics  x  break-even  x  pollution avoided.
Everything here is deterministic Python. The LLM only reads the bill and explains the result.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from . import economics, engine, impact, policy, states, weather
from .bill import to_number

log = logging.getLogger("surya.analysis")

MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DEFAULT_FLAT_TARIFF = 7.0           # ₹/unit when a non-Delhi bill doesn't reveal its rate


def ym_label(ym: str) -> str:
    return f"{MONTH_ABBR[int(ym[5:7]) - 1]} {ym[:4]}"


def derive_flat_tariff(bill: dict | None) -> tuple[float | None, str | None]:
    """Average energy rate from the bill (energy charges ÷ units). None if it looks wrong."""
    bill = bill or {}
    e, u = to_number(bill.get("energy_charges")), to_number(bill.get("units_consumed"))
    if e and u and u > 0:
        r = e / u
        if 2.0 <= r <= 15.0:
            return round(r, 2), f"₹{r:.2f}/unit, taken from your bill (energy charges ÷ units)"
    return None, None


def _safe_history(lat, lon, today):
    try:
        return weather.history(lat, lon, today=today), None
    except Exception as e:
        log.warning("weather history unavailable: %s", e)
        return None, f"{type(e).__name__}: {e}"


def _month_of_year_means(hist: dict) -> list[float]:
    buckets: list[list[float]] = [[] for _ in range(12)]
    for m in hist["months"]:
        buckets[int(m["ym"][5:7]) - 1].append(m["kwh_per_kw"])
    return [sum(b) / len(b) for b in buckets]


def _agg(rows: list[dict]) -> dict:
    return {"kwh": round(sum(r["kwh"] for r in rows)), "inr": sum(r["inr"] for r in rows)}


def analyze(*, monthly_units: float, location: dict, bill: dict | None = None, state: str | None = None,
            sanctioned_load_kw: float | None = None, roof_area_m2: float | None = None,
            kw_override: float | None = None, flat_tariff: float | None = None,
            today: date | None = None, include_now: bool = True) -> dict:
    if not monthly_units or monthly_units <= 0:
        raise ValueError("monthly_units is required")
    lat, lon = float(location["lat"]), float(location["lon"])
    bill = bill or {}
    today = today or date.today()
    state = (states.state_code(state) or states.state_code(location.get("state_code"))
             or states.state_code(bill.get("state")) or states.state_from_discom(bill.get("discom")))
    sanctioned_load_kw = sanctioned_load_kw or to_number(bill.get("sanctioned_load_kw"))
    warnings: list[dict] = []

    # ---- fetch everything slow in parallel
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_pv = ex.submit(engine.pvgis_yield, lat, lon, 1.0)
        f_hist = ex.submit(_safe_history, lat, lon, today)
        f_now = ex.submit(weather.now, lat, lon) if include_now else None
        pv = f_pv.result()
        hist, hist_err = f_hist.result()
        live = f_now.result() if f_now else None
    pv_ok = pv["source"].startswith("PVGIS")

    # ---- typical-year yield + calibration of the weather record
    scale, calibration = 1.0, {"applied": False}
    if hist:
        years = len(hist["months"]) / 12
        weather_annual = sum(m["kwh_per_kw"] for m in hist["months"]) / years
        if pv_ok:
            raw = pv["annual_kwh"] / weather_annual
            scale = min(max(raw, 0.80), 1.10)
            calibration = {"applied": True, "scale": round(scale, 3), "raw_ratio": round(raw, 3),
                           "pvgis_kwh_per_kw": round(pv["annual_kwh"]), "weather_kwh_per_kw": round(weather_annual)}
            if abs(raw - 1) > 0.20:
                warnings.append({"code": "weather_vs_pvgis_gap",
                                 "text": f"Weather record and PVGIS differ by {abs(raw - 1):.0%}; results are anchored to PVGIS."})
            per_kw = pv
        else:
            per_kw = {"monthly_kwh": _month_of_year_means(hist), "annual_kwh": weather_annual,
                      "source": hist["source"] + ", 5-year average"}
            calibration = {"applied": False, "weather_kwh_per_kw": round(weather_annual)}
            warnings.append({"code": "pvgis_down", "text": "PVGIS was unreachable; using the 5-year weather average for yield."})
    else:
        per_kw = pv
        warnings.append({"code": "weather_down",
                         "text": "Live weather history could not be fetched, so the past-1-year and past-5-year views are "
                                 "unavailable. Showing a typical year instead."})
        if not pv_ok:
            warnings.append({"code": "no_solar_data",
                             "text": "No live solar data at all: numbers use a generic North-India profile and are rough."})
    lo_band, hi_band = policy.SYSTEM["yield_sanity_band"]
    if not lo_band <= per_kw["annual_kwh"] <= hi_band:
        warnings.append({"code": "yield_out_of_band",
                         "text": f"Yield of {per_kw['annual_kwh']:.0f} kWh/kW/yr is outside the usual {lo_band}-{hi_band} range for India."})

    # ---- tariff
    is_delhi = state == "DL"
    derived, tariff_note = derive_flat_tariff(bill)
    flat = flat_tariff or (None if is_delhi else (derived or DEFAULT_FLAT_TARIFF))
    if not is_delhi and not flat_tariff and not derived:
        warnings.append({"code": "tariff_assumed", "text": f"Your bill's rate wasn't readable; assumed ₹{DEFAULT_FLAT_TARIFF:g}/unit."})
        tariff_note = f"₹{DEFAULT_FLAT_TARIFF:g}/unit assumed"
    if not state:
        warnings.append({"code": "no_state", "text": "State unknown: only the central subsidy is counted."})

    # ---- plan: engine's size limit first, then stop where the extra panels stop paying back
    def make_plan(kw=None):
        return engine.build_plan(monthly_units=monthly_units, lat=lat, lon=lon, roof_area_m2=roof_area_m2,
                                 state=state or "", sanctioned_load_kw=sanctioned_load_kw, flat_tariff=flat,
                                 kw_override=kw, per_kw=per_kw)

    plan = make_plan(kw_override)
    if plan.kw <= 0:
        return {"ok": False, "reason": "roof_too_small", "plan": {**plan.to_dict(), "headline_en": "", "headline_hi": ""},
                "warnings": warnings}
    rate = None if (is_delhi and not flat_tariff) else (flat or DEFAULT_FLAT_TARIFF)
    kw_max = plan.kw
    sizes = economics.ladder_sizes(kw_max)
    if kw_override:
        sizes = sorted(set(sizes) | {float(kw_override)})
    ladder = economics.ladder(sizes=sizes, monthly_units=monthly_units, per_kw_monthly=per_kw["monthly_kwh"],
                              flat_rate=rate, state=state, recommended_kw=kw_max)
    if kw_override is None and kw_max > 1:
        kw_eco = economics.economic_size([r for r in ladder if r["kw"] <= kw_max])
        if kw_eco < kw_max:
            plan = make_plan(kw_eco)
            plan.sizing["binding_limit"] = "payback"
            plan.sizing["max_useful_kw"] = kw_max
            for r in ladder:
                r["recommended"] = abs(r["kw"] - kw_eco) < 1e-9
    elif kw_override:
        plan.sizing["user_chosen"] = True
    kw = plan.kw
    plan_d = plan.to_dict()
    plan_d["headline_en"] = engine.headline(plan, "en")
    plan_d["headline_hi"] = engine.headline(plan, "hi")
    annual_kwh, annual_saving = plan.generation["annual_kwh"], sum(r["saving"] for r in plan.monthly_table)

    # ---- what the roof WOULD have produced (real weather, valued at today's tariff)
    life = policy.PANEL["life_years"]
    deg = policy.PANEL["degradation_per_year"]
    lifetime_kwh = sum(annual_kwh * (1 - deg) ** y for y in range(life))
    if hist:
        rows = []
        for m in hist["months"]:
            gen = m["kwh_per_kw"] * scale * kw
            rows.append({"ym": m["ym"], "kwh": round(gen, 1), "inr": engine.monthly_savings(monthly_units, gen, rate)["saving"]})
        last12 = rows[-12:]
        per_year = [{"label": f"{ym_label(rows[i]['ym'])} – {ym_label(rows[i + 11]['ym'])}", **_agg(rows[i:i + 12])}
                    for i in range(0, len(rows) - 11, 12)]
        best = max(last12, key=lambda r: r["kwh"])
        worst = min(last12, key=lambda r: r["kwh"])
        l12, l60 = _agg(last12), _agg(rows)
        history = {
            "mode": "weather", "source": hist["source"], "calibration": calibration,
            "start": hist["start"], "end": hist["end"], "tilt": hist["tilt"], "azimuth": hist["azimuth"],
            "kw": kw, "months": rows,
            "last12": {**l12, "vs_typical_pct": round((l12["kwh"] / annual_kwh - 1) * 100, 1) if annual_kwh else None,
                       "best_month": best, "worst_month": worst, "impact": impact.impact(l12["kwh"])},
            "last60": {**l60, "years": per_year, "avg_per_year_inr": round(l60["inr"] / (len(rows) / 12)),
                       "impact": impact.impact(l60["kwh"])},
            "valued_at": "today's tariff and your average monthly use",
        }
    else:
        rows = [{"ym": None, "label": r["month"], "kwh": r["generation"], "inr": r["saving"]} for r in plan.monthly_table]
        history = {"mode": "typical_year", "source": per_kw["source"], "calibration": calibration, "kw": kw,
                   "months": rows, "error": hist_err,
                   "last12": {**_agg(rows), "vs_typical_pct": 0.0, "impact": impact.impact(annual_kwh)},
                   "last60": None, "valued_at": "today's tariff and your average monthly use"}

    # ---- per panel, ladder, break-even, subsidy
    sub = engine.subsidies(kw, state, monthly_units)
    subsidy_total = plan.central_subsidy + plan.state_subsidy
    panel = economics.per_panel(kw=kw, annual_kwh=annual_kwh, annual_saving=annual_saving,
                                gross=plan.gross_cost, subsidy=subsidy_total, net=plan.net_cost)
    breakeven = economics.breakeven(plan.upfront_cost, annual_saving, kw)

    cash_now = 0 if plan.delhi_free_scheme else plan.gross_cost - plan.state_subsidy
    subsidy = {
        "as_of": policy.CENTRAL["as_of"], "central": plan.central_subsidy, "state": plan.state_subsidy,
        "state_label": sub["state_label"], "total": subsidy_total, "gross": plan.gross_cost, "net": plan.net_cost,
        "free_scheme": plan.delhi_free_scheme, "cash_at_install": max(cash_now, 0),
        "refund_later": 0 if plan.delhi_free_scheme else plan.central_subsidy,
        "refund_note": "Central subsidy is credited to your bank account by DBT roughly 30–45 days after commissioning.",
        "ceiling_note": "Central subsidy stops growing at 3 kW: every kW above that is unsubsidised.",
        "by_size": [{"kw": r["kw"], "central": r["central"], "state": r["state"]} for r in ladder],
        "conditions": policy.CENTRAL["conditions"],
        "conditions_hi": policy.CENTRAL["conditions_hi"],
        "portals": [policy.CENTRAL["source"]] + ([policy.DELHI["source"]] if is_delhi else []),
        "free_scheme_limit_units": policy.DELHI["free_scheme_max_units"] if is_delhi else None,
        "state_note": policy.DELHI["status_note"] if is_delhi else None,
        "possible_extra": [] if is_delhi or state not in policy.STATE_TOPUPS["items"] else [
            {**policy.STATE_TOPUPS["items"][state], "state_code": state, "as_of": policy.STATE_TOPUPS["as_of"],
             "source": policy.STATE_TOPUPS["source"], "counted": False}],
    }

    imp = {"per_year": impact.impact(annual_kwh), "lifetime": impact.impact(lifetime_kwh), "method": impact.describe()}
    imp["last12"], imp["last60"] = history["last12"]["impact"], (history["last60"] or {}).get("impact")

    now = None
    if live:
        now = {**live, "system_watts": round(live["watts_per_kw"] * kw),
               "today_kwh_so_far": round(live["today_kwh_per_kw_so_far"] * kw, 1),
               "today_kwh_forecast": round(live["today_kwh_per_kw_forecast"] * kw, 1)}

    peak_payback = breakeven["payback_years"]
    chat_context = {
        "city": location.get("city"), "state": state, "kw": kw, "panels": panel["panels"],
        "gross_cost": plan.gross_cost, "central_subsidy": plan.central_subsidy, "state_subsidy": plan.state_subsidy,
        "net_cost": plan.net_cost, "upfront_cost": plan.upfront_cost, "emi": plan.emi,
        "avg_bill_before": plan.avg_bill_before, "avg_bill_after": plan.avg_bill_after,
        "avg_monthly_saving": plan.avg_monthly_saving, "month_one_net": plan.month_one_net,
        "delhi_free_scheme": plan.delhi_free_scheme, "binding_limit": plan.sizing["binding_limit"],
        "annual_kwh": annual_kwh, "payback_years": peak_payback, "free_after_years": breakeven["free_after_years"],
        "last_12_months": {"kwh": history["last12"]["kwh"], "inr": history["last12"]["inr"]},
        "last_5_years": None if not history["last60"] else {"kwh": history["last60"]["kwh"], "inr": history["last60"]["inr"]},
        "per_panel_year": {"kwh": panel["kwh_per_panel_year"], "inr": panel["inr_per_panel_year"]},
        "impact_per_year": {k: imp["per_year"][k] for k in ("co2_kg", "coal_kg", "petrol_l", "so2_kg", "nox_kg", "trees_year")},
        "headline_en": plan_d["headline_en"], "headline_hi": plan_d["headline_hi"],
        "rules_as_of": policy.CENTRAL["as_of"],
    }

    return {
        "ok": True, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "location": location, "state": state, "state_name": states.state_name(state),
        "inputs": {"monthly_units": round(monthly_units, 1), "sanctioned_load_kw": sanctioned_load_kw,
                   "roof_area_m2": roof_area_m2, "flat_tariff": flat, "tariff_note": tariff_note,
                   "discom": bill.get("discom")},
        "plan": plan_d, "history": history, "per_panel": panel, "ladder": ladder, "breakeven": breakeven,
        "subsidy": subsidy, "impact": imp, "now": now,
        "roof": {"known": roof_area_m2 is not None, "area_m2": roof_area_m2,
                 "needed_m2": plan.sizing["roof_needed_m2"], "panel_area_m2": panel["total_area_m2"]},
        "warnings": warnings, "chat_context": chat_context,
        "credits": policy.DATA_CREDITS, "rules_as_of": policy.CENTRAL["as_of"],
    }
