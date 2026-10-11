"""What does a unit of rooftop-solar electricity NOT burn?  Coal, petrol-equivalent and pollution.

Every factor lives in policy.IMPACT with its source and an "approximate" label. These are
grid-average estimates (±20-30%): a unit saved at home is a unit a mostly-coal grid did not generate.
"""
from __future__ import annotations

from . import policy


def factors() -> dict:
    """Per kWh of grid electricity avoided."""
    I = policy.IMPACT
    c, share = I["per_coal_kwh"], I["coal_share"]
    return {
        "co2_gross_kg": I["grid_kg_co2_per_kwh"],
        "co2_lifecycle_kg": I["solar_lifecycle_kg_co2_per_kwh"],
        "co2_net_kg": I["grid_kg_co2_per_kwh"] - I["solar_lifecycle_kg_co2_per_kwh"],
        "coal_kg": c["coal_kg"] * share,
        "so2_g": c["so2_g"] * share,
        "nox_g": c["nox_g"] * share,
        "pm25_g": c["pm25_g"] * share,
        "ash_kg": c["ash_kg"] * share,
        "water_l": c["water_l"] * share,
    }


def impact(kwh: float) -> dict:
    """Environmental effect of generating `kwh` units from the roof instead of drawing them from the grid."""
    I, f = policy.IMPACT, factors()
    kwh = max(float(kwh), 0.0)
    co2 = kwh * f["co2_net_kg"]
    petrol_l = co2 / I["petrol_kg_co2_per_litre"]
    return {
        "kwh": round(kwh),
        "co2_kg": round(co2),
        "coal_kg": round(kwh * f["coal_kg"]),
        "petrol_l": round(petrol_l, 1),                              # litres of petrol that emit the same CO₂
        "car_km": round(petrol_l * I["car_km_per_litre"]),           # …or this many km in a petrol car
        "two_wheeler_km": round(petrol_l * I["two_wheeler_km_per_litre"]),
        "trees_year": round(co2 / I["tree_kg_co2_per_year"]),        # trees needed to absorb it for a year
        "so2_kg": round(kwh * f["so2_g"] / 1000, 1),
        "nox_kg": round(kwh * f["nox_g"] / 1000, 1),
        "pm25_kg": round(kwh * f["pm25_g"] / 1000, 2),
        "ash_kg": round(kwh * f["ash_kg"]),
        "water_l": round(kwh * f["water_l"]),
    }


def describe() -> dict:
    """The factors and their sources, for the 'how we calculated this' panel and /api/policy."""
    I, f = policy.IMPACT, factors()
    return {"as_of": I["as_of"], "label": I["label"], "per_kwh": {k: round(v, 4) for k, v in f.items()},
            "petrol_kg_co2_per_litre": I["petrol_kg_co2_per_litre"], "car_km_per_litre": I["car_km_per_litre"],
            "two_wheeler_km_per_litre": I["two_wheeler_km_per_litre"], "tree_kg_co2_per_year": I["tree_kg_co2_per_year"],
            "sources": I["sources"]}
