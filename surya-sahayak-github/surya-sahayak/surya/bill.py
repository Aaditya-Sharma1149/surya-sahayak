"""Bill reader: photo/PDF of an Indian electricity bill -> structured, validated fields.

Flow:  bytes -> Bedrock vision (JSON) -> clean numbers -> sanity checks
       -> average monthly units -> list of fields the user must confirm.
The UI should always show the result in editable inputs: never dead-end.
"""
from __future__ import annotations

import re

from . import geo, llm, states

BILL_PROMPT = """You are reading a photo of an Indian residential electricity bill \
(e.g. BSES Rajdhani, BSES Yamuna, TPDDL, NDMC, or any state discom).

Return ONLY a JSON object, no prose, with exactly these keys:
{
  "discom": string | null,
  "state": string | null,                  // 2-letter code, e.g. "DL"
  "consumer_name": string | null,
  "ca_number": string | null,              // CA / consumer / account number
  "bill_month": string | null,             // "YYYY-MM"
  "units_consumed": number | null,         // kWh billed for THIS period
  "billing_days": number | null,
  "sanctioned_load_kw": number | null,
  "service_address": string | null,        // supply / premises address as printed
  "pincode": string | null,                // 6-digit PIN code of the supply address
  "city": string | null,                   // city or town of the supply address
  "tariff_category": string | null,        // e.g. "Domestic"
  "energy_charges": number | null,
  "fixed_charges": number | null,
  "govt_subsidy": number | null,           // positive number if shown
  "total_payable": number | null,
  "consumption_history": [{"month": "YYYY-MM", "units": number}],
  "confidence": "high" | "medium" | "low",
  "notes": string
}

Rules:
- Numbers must be plain numbers (no ₹, commas or units). 1,234.50 -> 1234.5
- If sanctioned load is in kVA, use it as kW.
- Never guess. If a field is not visible or not legible, use null and say so in notes.
- If the billing period is two months, report the units printed and set billing_days.
- Read Hindi labels too ("खपत" = consumption, "स्वीकृत भार" = sanctioned load).
"""

NUMERIC = ["units_consumed", "billing_days", "sanctioned_load_kw", "energy_charges",
           "fixed_charges", "govt_subsidy", "total_payable"]

def to_number(v):
    """'1,234.50' / '₹ 1234' / 345 / None -> float | None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d[\d,]*\.?\d*", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def clean(raw: dict) -> dict:
    bill = dict(raw)
    for k in NUMERIC:
        bill[k] = to_number(bill.get(k))
    hist = []
    for h in bill.get("consumption_history") or []:
        u = to_number(h.get("units")) if isinstance(h, dict) else None
        if u is not None and 0 < u < 20_000:
            hist.append({"month": h.get("month"), "units": u})
    bill["consumption_history"] = hist
    bill["state"] = states.state_code(bill.get("state")) or states.state_from_discom(bill.get("discom"))
    bill["pincode"] = geo.extract_pincode(bill.get("pincode")) or geo.extract_pincode(bill.get("service_address"))
    for k in ("service_address", "city"):
        v = bill.get(k)
        bill[k] = v.strip() if isinstance(v, str) and v.strip() else None
    return bill


def monthly_average(bill: dict) -> float | None:
    """Best estimate of average monthly units (12-month history beats one bill)."""
    hist = [h["units"] for h in bill.get("consumption_history") or []]
    if len(hist) >= 3:
        days = bill.get("billing_days")
        avg = sum(hist) / len(hist)
        # Two-monthly bills print two-month totals in their history too.
        return round(avg / 2 if days and days > 45 else avg, 1)
    u, days = bill.get("units_consumed"), bill.get("billing_days")
    if u is None:
        return None
    return round(u * 30 / days, 1) if days and days >= 20 else u


def validate(bill: dict) -> list[str]:
    """Plausibility checks. Returns warnings; anything here goes to needs_review."""
    w = []
    u = bill.get("units_consumed")
    if u is not None and not (5 <= u <= 5000):
        w.append(f"units_consumed={u} looks implausible for a home")
    sl = bill.get("sanctioned_load_kw")
    if sl is not None and not (0.5 <= sl <= 50):
        w.append(f"sanctioned_load_kw={sl} looks implausible")
    days = bill.get("billing_days")
    if days is not None and not (20 <= days <= 70):
        w.append(f"billing_days={days} looks implausible")
    if u and bill.get("energy_charges") and bill["energy_charges"] / u > 15:
        w.append("energy charge per unit > ₹15 — units may be misread")
    return w


def read_bill_bytes(data: bytes, filename: str = "bill.jpg", model_id: str | None = None) -> dict:
    raw = llm.ask_vision_json(data, filename, BILL_PROMPT, model_id=model_id, max_tokens=1500)
    bill = clean(raw)
    bill["avg_monthly_units"] = monthly_average(bill)
    warnings = validate(bill)
    needs = [k for k in ("units_consumed", "sanctioned_load_kw", "discom") if bill.get(k) is None]
    if bill["avg_monthly_units"] is None:
        needs.append("avg_monthly_units")
    if bill.get("confidence") == "low" or warnings:
        needs.append("units_consumed")
    bill["warnings"] = warnings
    bill["needs_review"] = sorted(set(needs))
    return bill


def read_bill(image_path: str, model_id: str | None = None) -> dict:
    with open(image_path, "rb") as f:
        return read_bill_bytes(f.read(), image_path, model_id)
