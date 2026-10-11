"""Per-panel earnings, the size ladder (what each extra kW buys) and the break-even year.

Pure functions on top of engine.py. All rupee inputs come from policy.py.
"""
from __future__ import annotations

import math

from . import engine, policy


def n_panels(kw: float) -> int:
    return max(1, round(kw * 1000 / policy.PANEL["watt_peak"]))


# ------------------------------------------------------------------ cash flow
def cashflow(upfront: float, saving_y1: float, kw: float, *, escalation: float,
             degradation: float | None = None, years: int | None = None) -> list[int]:
    """Cumulative cash position at the end of each year (index 0 = the day you pay).

    Includes the one-off inverter replacement and yearly cleaning/O&M after the free-service years.
    """
    P, S = policy.PANEL, policy.SYSTEM
    degradation = P["degradation_per_year"] if degradation is None else degradation
    years = years or P["life_years"]
    cum = [-float(upfront)]
    for y in range(1, years + 1):
        gain = saving_y1 * (1 - degradation) ** (y - 1) * (1 + escalation) ** (y - 1)
        cost = 0.0
        if y == S["inverter_replacement_year"]:
            cost += kw * S["inverter_per_kw"]
        if y > S["free_om_years"]:
            cost += kw * S["om_per_kw_year"]
        cum.append(cum[-1] + gain - cost)
    return [round(c) for c in cum]


def _payback_raw(cum: list[int]) -> float | None:
    if cum[0] >= 0:
        return 0.0
    for y in range(1, len(cum)):
        if cum[y] >= 0:
            prev, cur = cum[y - 1], cum[y]
            return (y - 1) + (-prev) / (cur - prev)
    return None


def payback_years(cum: list[int]) -> float | None:
    """First moment the cumulative position reaches zero (linear inside the year), 1 decimal."""
    raw = _payback_raw(cum)
    return None if raw is None else round(raw, 1)


def breakeven(upfront: float, annual_saving: float, kw: float) -> dict:
    """When does the roof start paying for itself, and when is the electricity 'free'?

    Two honest cases: tariffs stay flat (conservative) and tariffs grow ~3%/yr (typical).
    """
    S, P = policy.SYSTEM, policy.PANEL
    flat = cashflow(upfront, annual_saving, kw, escalation=0.0)
    grow = cashflow(upfront, annual_saving, kw, escalation=S["tariff_escalation"])
    raw_flat, raw_grow = _payback_raw(flat), _payback_raw(grow)
    pb_flat, pb_grow = payback_years(flat), payback_years(grow)
    lo = raw_grow if raw_grow is not None else raw_flat
    hi = raw_flat if raw_flat is not None else raw_grow
    free_after = None if hi is None else [math.ceil(round(lo, 6)), math.ceil(round(hi, 6))]
    return {
        "upfront": round(upfront),
        "payback_years": {"optimistic": pb_grow, "conservative": pb_flat},
        "free_after_years": free_after,                      # whole years, e.g. [5, 6]
        "free_years_left": None if hi is None else max(P["life_years"] - math.ceil(round(hi, 6)), 0),
        "from_day_one": upfront <= 0,
        "net_gain_25y": {"conservative": flat[-1], "optimistic": grow[-1]},
        "cumulative": {"years": list(range(len(flat))), "conservative": flat, "optimistic": grow},
        "assumptions": [
            f"Panels lose {P['degradation_per_year']:.1%} output a year",
            f"Inverter replaced in year {S['inverter_replacement_year']} (≈₹{S['inverter_per_kw']:,}/kW, assumed)",
            f"Cleaning/O&M ≈₹{S['om_per_kw_year']}/kW a year after year {S['free_om_years']}",
            "Conservative = today's tariff forever; optimistic = tariff grows "
            f"{S['tariff_escalation']:.0%} a year",
        ],
        "assumptions_hi": [
            f"पैनल हर साल {P['degradation_per_year']:.1%} उत्पादन खोते हैं",
            f"साल {S['inverter_replacement_year']} में इन्वर्टर बदला जाता है (≈₹{S['inverter_per_kw']:,}/kW, माना गया)",
            f"साल {S['free_om_years']} के बाद सफ़ाई/रखरखाव ≈₹{S['om_per_kw_year']}/kW सालाना",
            f"कम अनुमान = आज का टैरिफ़ हमेशा; ज़्यादा अनुमान = टैरिफ़ हर साल {S['tariff_escalation']:.0%} बढ़े",
        ],
    }


# ------------------------------------------------------------------ per panel
def per_panel(*, kw: float, annual_kwh: float, annual_saving: float, gross: float,
              subsidy: float, net: float) -> dict:
    n = n_panels(kw)
    area = n * policy.PANEL["area_m2"]
    return {
        "watt_peak": policy.PANEL["watt_peak"], "panels": n,
        "area_each_m2": policy.PANEL["area_m2"], "total_area_m2": round(area, 1),
        "kwh_per_panel_year": round(annual_kwh / n), "kwh_per_m2_year": round(annual_kwh / area),
        "inr_per_panel_year": round(annual_saving / n), "inr_per_panel_month": round(annual_saving / n / 12),
        "inr_per_panel_day": round(annual_saving / n / 365, 1),
        "inr_per_m2_year": round(annual_saving / area), "inr_per_kw_year": round(annual_saving / kw),
        "gross_per_panel": round(gross / n), "subsidy_per_panel": round(subsidy / n),
        "net_per_panel": round(net / n),
    }


# ------------------------------------------------------------------ size ladder
def ladder_sizes(recommended_kw: float, max_kw: float | None = None) -> list[float]:
    max_kw = max_kw or policy.SYSTEM["max_residential_kw"]
    if recommended_kw <= 0:
        return []
    top = min(max_kw, math.ceil(recommended_kw) + 2)
    sizes = {float(k) for k in range(1, int(top) + 1)}
    sizes.add(float(recommended_kw))
    return sorted(sizes)


def ladder(*, sizes: list[float], monthly_units: float, per_kw_monthly: list, flat_rate: float | None,
           state: str | None, recommended_kw: float) -> list[dict]:
    """One row per system size, including what the *extra* panels over the previous row cost and earn."""
    rows: list[dict] = []
    prev = None
    for kw in sizes:
        sub = engine.subsidies(kw, state, monthly_units)
        gross = engine.gross_cost(kw)
        subsidy = sub["central"] + sub["state"]
        net = max(gross - subsidy, 0)
        upfront = 0 if sub["free_scheme"] else net
        table = engine.year_table(monthly_units, per_kw_monthly, kw, flat_rate)
        saving = sum(r["saving"] for r in table)
        kwh = sum(r["generation"] for r in table)
        row = {"kw": kw, "panels": n_panels(kw), "gross": gross, "central": sub["central"], "state": sub["state"],
               "subsidy": subsidy, "net": net, "upfront": upfront, "free_scheme": sub["free_scheme"],
               "annual_kwh": kwh, "annual_saving": saving,
               "payback_years": 0.0 if upfront == 0 else (round(upfront / saving, 1) if saving > 0 else None),
               "recommended": abs(kw - recommended_kw) < 1e-9}
        base = prev or {"upfront": 0, "annual_saving": 0, "subsidy": 0, "panels": 0}
        d_cost, d_save = row["upfront"] - base["upfront"], row["annual_saving"] - base["annual_saving"]
        row["extra"] = {"panels": row["panels"] - base["panels"], "upfront": d_cost, "subsidy": row["subsidy"] - base["subsidy"],
                        "annual_saving": d_save,
                        "payback_years": 0.0 if d_cost <= 0 else (round(d_cost / d_save, 1) if d_save > 0 else None)}
        rows.append(row)
        prev = row
    return rows


def economic_size(rows: list[dict], max_years: float | None = None) -> float:
    """Largest size whose *extra* panels still pay back within `max_years` (the first kW is always kept)."""
    max_years = max_years or policy.SYSTEM["max_marginal_payback_years"]
    best = rows[0]["kw"] if rows else 0.0
    for i, r in enumerate(rows):
        if i == 0:
            continue
        pb = r["extra"]["payback_years"]
        if pb is not None and pb <= max_years:
            best = r["kw"]
        else:
            break
    return best
