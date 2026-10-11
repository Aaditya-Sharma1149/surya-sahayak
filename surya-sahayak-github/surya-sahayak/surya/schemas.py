"""Request/response models for the API (pydantic v2)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PlanRequest(BaseModel):
    monthly_units: float = Field(..., gt=0, lt=20_000, description="Average monthly units (kWh)")
    lat: float = Field(..., ge=6, le=37, description="Roof latitude (India)")
    lon: float = Field(..., ge=68, le=98, description="Roof longitude (India)")
    roof_area_m2: Optional[float] = Field(None, gt=0, lt=5_000, description="Area drawn on the map, m². Omit if unknown")
    state: str = Field("DL", min_length=2, max_length=2)
    sanctioned_load_kw: Optional[float] = Field(None, gt=0, lt=100)
    flat_tariff: Optional[float] = Field(None, gt=0, lt=20, description="₹/unit outside Delhi")
    quoted_cost: Optional[int] = Field(None, gt=0)
    kw_override: Optional[float] = Field(None, gt=0, le=10)
    use_loan: bool = True


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None
    lang: str = Field("en", pattern="^(en|hi)$")
    context: Optional[dict] = Field(None, description="Current plan/bill/quote JSON from the UI")


class ChatResponse(BaseModel):
    session_id: str
    reply: str


class QuoteTextRequest(BaseModel):
    text: str = Field(..., min_length=10)
    recommended_kw: Optional[float] = None


class LocateRequest(BaseModel):
    lat: Optional[float] = Field(None, ge=-90, le=90, description="Browser GPS latitude")
    lon: Optional[float] = Field(None, ge=-180, le=180, description="Browser GPS longitude")
    accuracy_m: Optional[float] = Field(None, ge=0)
    query: Optional[str] = Field(None, max_length=120, description="A city or 6-digit PIN code typed by the user")
    prefer: Optional[str] = Field(None, pattern="^(gps|bill)$")
    bill: Optional[dict] = Field(None, description="Output of /api/bill (state, discom, pincode, city)")


class AnalyzeRequest(BaseModel):
    location: dict = Field(..., description="Output of /api/locate (needs lat, lon)")
    monthly_units: Optional[float] = Field(None, gt=0, lt=20_000, description="Overrides the bill's average")
    bill: Optional[dict] = None
    roof_area_m2: Optional[float] = Field(None, gt=0, lt=5_000)
    kw_override: Optional[float] = Field(None, gt=0, le=10)
    sanctioned_load_kw: Optional[float] = Field(None, gt=0, lt=100)
    flat_tariff: Optional[float] = Field(None, gt=0, lt=20)
