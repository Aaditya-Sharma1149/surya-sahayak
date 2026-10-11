"""Surya Sahayak — Strands agent that ties bill reader, plan engine and quote auditor together.

The agent never computes numbers itself: every ₹ figure comes from a tool.
CLI:  python -m surya.agent bill.jpg --lat 28.55 --lon 77.20 --roof 45 [--quote q.txt] [--hi]
"""
from __future__ import annotations

import argparse
import json
import os
import uuid

from strands import Agent, tool
from strands.models import BedrockModel

from . import analysis, bill as bill_mod, engine, llm, policy, quote as quote_mod

AGENT_MODEL = os.getenv("AGENT_MODEL_ID", "apac.amazon.nova-pro-v1:0")


@tool
def read_electricity_bill(image_path: str) -> dict:
    """Extract units consumed, sanctioned load, discom and charges from a photo or PDF of an Indian electricity bill.

    Args:
        image_path: Local path to the bill photo (jpg/png) or PDF.
    """
    return bill_mod.read_bill(image_path)


@tool
def make_solar_plan(monthly_units: float, lat: float, lon: float, roof_area_m2: float,
                    state: str = "DL", sanctioned_load_kw: float | None = None,
                    quoted_cost: float | None = None) -> dict:
    """Size a rooftop solar system and compute generation (PVGIS), subsidy stack, EMI, bill savings and payback.

    Args:
        monthly_units: Average monthly consumption in kWh (units).
        lat: Roof latitude.
        lon: Roof longitude.
        roof_area_m2: Shadow-free roof area in square metres.
        state: Two-letter state code, e.g. DL.
        sanctioned_load_kw: Sanctioned load from the bill, if known.
        quoted_cost: Vendor's total price in rupees, if the user has a quote.
    """
    p = engine.build_plan(monthly_units=monthly_units, lat=lat, lon=lon, roof_area_m2=roof_area_m2,
                          state=state, sanctioned_load_kw=sanctioned_load_kw,
                          quoted_cost=int(quoted_cost) if quoted_cost else None)
    d = p.to_dict()
    d.pop("monthly_table")          # keep the agent's context small; the UI renders it
    d["headline_en"] = engine.headline(p, "en")
    d["headline_hi"] = engine.headline(p, "hi")
    return d


@tool
def what_if_system_size(monthly_units: float, lat: float, lon: float, kw: float, state: str = "DL",
                        sanctioned_load_kw: float | None = None) -> dict:
    """Re-run the full analysis for a different system size ("what if I install 5 kW?").

    Returns cost, subsidy, EMI, saving, payback, the last 12 months / 5 years of generation in rupees
    (from real weather) and the coal / petrol / pollution avoided for that size.

    Args:
        monthly_units: Average monthly consumption in kWh (units).
        lat: Home latitude.
        lon: Home longitude.
        kw: System size to evaluate, 1 to 10 kW.
        state: Two-letter state code, e.g. DL.
        sanctioned_load_kw: Sanctioned load from the bill, if known.
    """
    r = analysis.analyze(monthly_units=monthly_units, location={"lat": lat, "lon": lon}, state=state,
                         sanctioned_load_kw=sanctioned_load_kw, kw_override=kw, include_now=False)
    return r["chat_context"] if r.get("ok") else {"error": r.get("reason", "analysis failed")}


@tool
def audit_vendor_quote(quote_text: str, system_kw: float | None = None, total_price: float | None = None,
                       recommended_kw: float | None = None) -> dict:
    """Check a vendor's rooftop-solar quote for overpricing, missing items and subsidy-compliance risks.

    Args:
        quote_text: Full text of the quotation.
        system_kw: Quoted capacity in kW, if known.
        total_price: Quoted all-inclusive price in rupees, if known.
        recommended_kw: System size from make_solar_plan, to compare against.
    """
    return quote_mod.audit_quote(quote_text, system_kw, total_price, recommended_kw)


@tool
def application_checklist() -> dict:
    """Documents needed to apply for the PM Surya Ghar subsidy (English + Hindi), plus the official portals and conditions."""
    return {"documents": [{"en": en, "hi": hi} for en, hi in policy.DOCUMENT_CHECKLIST],
            "portals": [policy.CENTRAL["source"], policy.DELHI["source"]],
            "conditions": policy.CENTRAL["conditions"], "as_of": policy.CENTRAL["as_of"]}


TOOLS = [read_electricity_bill, make_solar_plan, what_if_system_size, audit_vendor_quote, application_checklist]

SYSTEM_PROMPT = f"""You are Surya Sahayak (सूर्य सहायक), a rooftop-solar advisor for Indian households.

Workflow:
1. If given a bill photo path, call read_electricity_bill. Use avg_monthly_units; if it is null, ask the user for their monthly units.
2. Call make_solar_plan with the units, location and roof area.
3. If the user shares a vendor quote, call audit_vendor_quote with recommended_kw from the plan.
4. When the user asks how to apply, call application_checklist.
5. For "what if I install N kW?" call what_if_system_size.
If a CONTEXT block with plan/bill/quote JSON is given, answer from it; only call tools for new calculations.
The CONTEXT may include: last_12_months and last_5_years (kWh and ₹ the system WOULD have produced, from real
weather, valued at today's tariff), per_panel_year (kWh and ₹ per panel), payback_years and free_after_years
(when the system has paid for itself; a range because tariffs may or may not rise), and impact_per_year
(CO₂ kg, coal kg, petrol-equivalent litres, SO₂/NOx kg, trees). binding_limit "payback" means we stopped at the
size where extra panels stop paying back within 10 years; max_useful_kw is the bigger size the bill could support.

How to answer:
- Lead with cash flow, using the plan's headline. If delhi_free_scheme is true, say that first: it is the biggest news.
- Then: system size and why (binding_limit), subsidy stack (central + state), upfront cost, EMI, payback.
- For quotes: lead with high-severity red flags, especially anything that puts the subsidy at risk.
- Use ONLY numbers that tools or the CONTEXT returned. Never invent or recompute a figure. Say "about" for savings.
- Pollution and coal numbers are grid-average estimates (±20-30%): say "roughly" and never present them as exact.
- Petrol is a CO₂ equivalent ("the same CO₂ as burning N litres of petrol"), not petrol the household would have burned.
- Never claim a vendor is fraudulent; say what to ask them and to verify on the portal.
- End with: "Rules as of {policy.CENTRAL['as_of']}. Confirm on pmsuryaghar.gov.in and solar.delhi.gov.in."
- Reply in Hindi (Devanagari) if the user writes in Hindi or asks for Hindi; otherwise English.
- Be brief (under 150 words unless asked): a household, not an engineer, is reading.
"""


def make_agent() -> Agent:
    model = BedrockModel(model_id=AGENT_MODEL, region_name=llm.REGION, temperature=0.2, max_tokens=1200)
    return Agent(model=model, system_prompt=SYSTEM_PROMPT, tools=TOOLS, callback_handler=None)


# In-memory sessions: fine for one EC2 box / a demo. Use DynamoDB if you scale out.
_SESSIONS: dict[str, Agent] = {}
_MAX_SESSIONS = 200


def chat(message: str, session_id: str | None = None, context: dict | None = None,
         lang: str = "en") -> tuple[str, str]:
    sid = session_id if session_id in _SESSIONS else (session_id or uuid.uuid4().hex)
    if sid not in _SESSIONS:
        if len(_SESSIONS) >= _MAX_SESSIONS:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        _SESSIONS[sid] = make_agent()
    msg = message
    if context:
        msg = f"CONTEXT (from the app, trusted numbers):\n{json.dumps(context, ensure_ascii=False)[:12000]}\n\nUSER: {message}"
    if lang == "hi":
        msg += "\n(कृपया हिंदी में जवाब दें।)"
    result = _SESSIONS[sid](msg)
    return sid, str(result).strip()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("bill")
    ap.add_argument("--lat", type=float, required=True)
    ap.add_argument("--lon", type=float, required=True)
    ap.add_argument("--roof", type=float, required=True, help="roof area in m²")
    ap.add_argument("--quote", help="path to a text file with the vendor quote")
    ap.add_argument("--hi", action="store_true", help="answer in Hindi")
    a = ap.parse_args()

    m = (f"Here is my electricity bill: {a.bill}. My roof is at lat {a.lat}, lon {a.lon}, "
         f"and the usable area is {a.roof} m².")
    if a.quote:
        m += f"\nI also got this vendor quote:\n{open(a.quote).read()}"
    _, reply = chat(m, lang="hi" if a.hi else "en")
    print(reply)
