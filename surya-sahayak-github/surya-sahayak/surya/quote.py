"""Quote auditor: vendor quote (text or extracted from PDF/photo) -> red flags.

Rule-based checks are deterministic so the demo can't hallucinate a verdict;
the LLM is only used upstream to pull text/fields out of a photo or PDF.
"""
from __future__ import annotations

import re

from . import policy

QUOTE_EXTRACT_PROMPT = """Read this rooftop-solar vendor quotation. Return ONLY JSON:
{"vendor_name": str|null, "system_kw": number|null, "total_price": number|null,
 "gst_included": bool|null, "panel_make": str|null, "panel_wattage_wp": number|null,
 "panel_dcr": bool|null, "inverter_make": str|null, "empanelment_id": str|null,
 "line_items": [str], "full_text": str}
Numbers as plain numbers. null if not stated. Never guess. Include the full visible text in full_text."""


def _has(text: str, needles: list[str]) -> bool:
    t = text.lower()
    return any(n in t for n in needles)


def _guess_kw(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*k\s*w(?!h)", text, re.I)
    return float(m.group(1)) if m else None


def _guess_price(text: str) -> float | None:
    cands = re.findall(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d+)?)", text, re.I)
    vals = [float(c.replace(",", "")) for c in cands]
    vals = [v for v in vals if v >= 20_000]
    return max(vals) if vals else None


def audit_quote(text: str, system_kw: float | None = None, total_price: float | None = None,
                recommended_kw: float | None = None, panel_dcr: bool | None = None,
                empanelment_id: str | None = None) -> dict:
    kw = system_kw or _guess_kw(text)
    price = total_price or _guess_price(text)
    flags, ok, ok_codes = [], [], []

    # 1. Price vs benchmark
    verdict = "unknown"
    if kw and price:
        per_kw = price / kw
        bucket = min(policy.BENCHMARK_PER_KW, key=lambda b: abs(b - kw))
        lo, hi = policy.BENCHMARK_PER_KW[bucket]
        if per_kw > hi * 1.10:
            verdict = "overpriced"
            flags.append({"severity": "high", "issue": f"Price ₹{per_kw:,.0f}/kW is above the fair range "
                          f"₹{lo:,}–₹{hi:,}/kW for ~{bucket} kW systems",
                          "ask_vendor": f"Ask for a revised quote near ₹{hi * kw:,.0f} or itemised costs",
                          "potential_overpay": round(price - hi * kw),
                          "code": "price_high", "p": {"per_kw": round(per_kw), "lo": lo, "hi": hi, "bucket": bucket,
                                                      "target": round(hi * kw)}})
        elif per_kw < lo * 0.85:
            verdict = "suspiciously_low"
            flags.append({"severity": "medium", "issue": f"Price ₹{per_kw:,.0f}/kW is well below market — "
                          "check for imported (non-DCR) panels, a thin structure or missing net meter",
                          "ask_vendor": "Ask for panel make, DCR certificate and structure spec in writing",
                          "code": "price_low", "p": {"per_kw": round(per_kw), "lo": lo, "hi": hi}})
        else:
            verdict = "fair"
            ok.append(f"Price ₹{per_kw:,.0f}/kW is within the fair range ₹{lo:,}–₹{hi:,}/kW")
            ok_codes.append({"code": "price_fair", "p": {"per_kw": round(per_kw), "lo": lo, "hi": hi}})
    else:
        flags.append({"severity": "medium", "issue": "Could not find system size and total price on the quote",
                      "ask_vendor": "Ask for kW capacity and an all-inclusive price (with GST) in writing",
                      "code": "no_size_price", "p": {}})

    # 2. Subsidy compliance (losing the subsidy is the costliest mistake)
    dcr = panel_dcr if panel_dcr is not None else _has(text, policy.REQUIRED_QUOTE_ITEMS["dcr_panels"])
    if not dcr:
        flags.append({"severity": "high", "issue": "Quote does not say the panels are DCR (domestically manufactured). "
                      "Non-DCR panels make you ineligible for the central subsidy (₹78,000 on 3 kW)",
                      "ask_vendor": "Get 'DCR modules' and the panel make/model written on the quote",
                      "code": "no_dcr", "p": {}, "subsidy": True})
    else:
        ok.append("Panels stated as DCR / domestic content")
        ok_codes.append({"code": "dcr_ok", "p": {}})
    if not (empanelment_id or _has(text, ["empanel", "registered vendor", "mnre", "pm surya ghar", "pmsg"])):
        flags.append({"severity": "high", "issue": "No sign the vendor is registered on the PM Surya Ghar portal",
                      "ask_vendor": "Ask for their registration ID and check it on pmsuryaghar.gov.in",
                      "code": "no_vendor_reg", "p": {}, "subsidy": True})
    else:
        ok.append("Vendor registration / scheme mentioned")
        ok_codes.append({"code": "vendor_ok", "p": {}})

    # 3. Missing line items
    missing = [k for k, needles in policy.REQUIRED_QUOTE_ITEMS.items()
               if k != "dcr_panels" and not _has(text, needles)]
    for k in missing:
        flags.append({"severity": "medium" if k in ("net_meter", "earthing_and_lightning_arrestor",
                                                      "warranty") else "low",
                      "issue": f"Not mentioned: {k.replace('_', ' ')}",
                      "ask_vendor": f"Confirm {k.replace('_', ' ')} is included at no extra cost",
                      "code": "missing_item", "p": {"item": k}})

    # 4. Size sanity vs our plan
    if kw and recommended_kw and abs(kw - recommended_kw) >= 1:
        flags.append({"severity": "medium", "issue": f"Quote is {kw:g} kW; your bill and roof suggest {recommended_kw:g} kW",
                      "ask_vendor": "Ask why this size; above 3 kW there is no extra central subsidy",
                      "code": "size_mismatch", "p": {"kw": kw, "rec": recommended_kw}})

    order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: order[f["severity"]])
    return {"system_kw": kw, "total_price": price,
            "price_per_kw": round(price / kw) if kw and price else None,
            "price_verdict": verdict, "red_flags": flags, "looks_good": ok, "looks_good_codes": ok_codes,
            "subsidy_at_risk": any("subsidy" in f["issue"] for f in flags if f["severity"] == "high"),
            "rules_as_of": policy.CENTRAL["as_of"]}


# ------------------------------------------------------------ file -> audit
def _audit_from_fields(fields: dict, recommended_kw: float | None) -> dict:
    from .bill import to_number
    text = fields.get("full_text") or ""
    text += "\n" + "\n".join(fields.get("line_items") or [])
    for k in ("panel_make", "inverter_make", "empanelment_id"):
        if fields.get(k):
            text += f"\n{k}: {fields[k]}"
    result = audit_quote(text,
                         system_kw=to_number(fields.get("system_kw")),
                         total_price=to_number(fields.get("total_price")),
                         recommended_kw=recommended_kw,
                         panel_dcr=fields.get("panel_dcr") if isinstance(fields.get("panel_dcr"), bool) else None,
                         empanelment_id=fields.get("empanelment_id"))
    result["vendor_name"] = fields.get("vendor_name")
    if fields.get("gst_included") is False:
        result["red_flags"].append({"severity": "medium", "issue": "Price may exclude GST",
                                    "ask_vendor": "Ask for the final price including GST",
                                    "code": "gst_excluded", "p": {}})
    return result


def audit_quote_bytes(data: bytes, filename: str, recommended_kw: float | None = None,
                      model_id: str | None = None) -> dict:
    """Photo/PDF/text of a quote -> audit. Text files skip the LLM entirely."""
    if filename.lower().endswith(".txt"):
        return audit_quote(data.decode("utf-8", "ignore"), recommended_kw=recommended_kw)
    from . import llm
    fields = llm.ask_vision_json(data, filename, QUOTE_EXTRACT_PROMPT, model_id=model_id, max_tokens=3000)
    result = _audit_from_fields(fields, recommended_kw)
    result["extracted"] = {k: v for k, v in fields.items() if k != "full_text"}
    return result
