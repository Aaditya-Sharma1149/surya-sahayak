"""Surya Sahayak HTTP API (FastAPI).

Run on EC2:   uvicorn api:app --host 127.0.0.1 --port 8000      (nginx proxies /api)
Docs:         http://<server>/api/docs
"""
from __future__ import annotations

import ipaddress
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from surya import analysis, bill as bill_mod, engine, geo, impact, policy, quote as quote_mod
from surya.schemas import (AnalyzeRequest, ChatRequest, ChatResponse, LocateRequest, PlanRequest,
                           QuoteTextRequest)

log = logging.getLogger("surya")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MAX_UPLOAD = 10 * 1024 * 1024
ALLOWED = (".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt")
S3_BUCKET = os.getenv("S3_BUCKET")            # optional: keep uploads in S3
WEB_DIR = Path(__file__).parent / "web"

app = FastAPI(title="Surya Sahayak API", version="2.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
                   allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- helpers
async def read_upload(file: UploadFile) -> bytes:
    name = (file.filename or "").lower()
    if not name.endswith(ALLOWED):
        raise HTTPException(415, f"Upload a photo (jpg/png/webp), PDF or .txt — got '{file.filename}'")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 10 MB)")
    return data


def save_to_s3(data: bytes, kind: str, filename: str) -> Optional[str]:
    if not S3_BUCKET:
        return None
    try:
        import boto3
        key = f"{kind}/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}-{Path(filename).name}"
        boto3.client("s3").put_object(Bucket=S3_BUCKET, Key=key, Body=data)
        return f"s3://{S3_BUCKET}/{key}"
    except Exception as e:                     # never fail the request because of S3
        log.warning("S3 upload failed: %s", e)
        return None


def client_ip(request: Request) -> Optional[str]:
    """Real visitor IP behind nginx / CloudFront: first public address in X-Forwarded-For."""
    candidates = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if request.client:
        candidates.append(request.client.host)
    for c in candidates:
        try:
            if ipaddress.ip_address(c).is_global:
                return c
        except ValueError:
            continue
    return None


def bedrock_error(e: Exception) -> HTTPException:
    """Turn AWS errors into messages a human can act on."""
    code = getattr(e, "response", {}).get("Error", {}).get("Code", type(e).__name__)
    hints = {
        "AccessDeniedException": "Bedrock access denied: enable model access in the Bedrock console "
                                 "and give the EC2 role bedrock:InvokeModel / Converse permissions.",
        "ValidationException": "Bedrock rejected the request: check BILL_MODEL_ID/AGENT_MODEL_ID for your region "
                               "(run python check_bedrock.py).",
        "ThrottlingException": "Bedrock is throttling requests: wait a few seconds and try again.",
        "ResourceNotFoundException": "Model not found in this region: run python check_bedrock.py.",
        "NoCredentialsError": "No AWS credentials: attach an IAM role to the EC2 instance or run aws configure.",
    }
    log.exception("AI call failed")
    return HTTPException(502, hints.get(code, f"AI service error ({code}): {e}"))


# ---------------------------------------------------------------- routes
@app.get("/api/health")
def health():
    return {"ok": True, "rules_as_of": policy.CENTRAL["as_of"],
            "stores_uploads": bool(S3_BUCKET), "max_upload_mb": MAX_UPLOAD // (1024 * 1024)}


@app.get("/api/policy")
def get_policy():
    """What the numbers are based on — show this in the UI footer."""
    return {"central": policy.CENTRAL, "delhi": policy.DELHI,
            "loan": policy.LOAN, "benchmark_per_kw": policy.BENCHMARK_PER_KW,
            "export_rate_assumed": policy.EXPORT_RATE_ASSUMED,
            "panel": policy.PANEL, "system": policy.SYSTEM, "impact": impact.describe(),
            "state_topups": policy.STATE_TOPUPS, "credits": policy.DATA_CREDITS}


@app.get("/api/checklist")
def checklist():
    return {"documents": [{"en": en, "hi": hi} for en, hi in policy.DOCUMENT_CHECKLIST],
            "portals": [policy.CENTRAL["source"], policy.DELHI["source"]],
            "conditions": policy.CENTRAL["conditions"], "as_of": policy.CENTRAL["as_of"]}


@app.post("/api/bill")
async def read_bill(file: UploadFile = File(...)):
    """Bill photo/PDF -> extracted fields. Fields in `needs_review` must be confirmed by the user."""
    data = await read_upload(file)
    s3 = save_to_s3(data, "bills", file.filename)
    try:
        result = await run_in_threadpool(bill_mod.read_bill_bytes, data, file.filename)
    except ValueError as e:                   # model returned non-JSON
        raise HTTPException(422, f"Couldn't read the bill — try a sharper, flatter photo. ({e})")
    except Exception as e:
        raise bedrock_error(e)
    result["stored_at"] = s3
    return result


@app.post("/api/plan")
def make_plan(req: PlanRequest):
    """Pure calculation — no AI. Works even if Bedrock is down."""
    p = engine.build_plan(**req.model_dump())
    out = p.to_dict()
    out["headline_en"] = engine.headline(p, "en")
    out["headline_hi"] = engine.headline(p, "hi")
    out["rules_as_of"] = policy.CENTRAL["as_of"]
    return out


@app.post("/api/quote")
async def audit_quote(file: Optional[UploadFile] = File(None), text: Optional[str] = Form(None),
                      recommended_kw: Optional[float] = Form(None)):
    """Upload a quote (photo/PDF/txt) OR paste its text."""
    if text and text.strip():
        return quote_mod.audit_quote(text, recommended_kw=recommended_kw)
    if not file:
        raise HTTPException(400, "Send a quote file or paste the quote text")
    data = await read_upload(file)
    save_to_s3(data, "quotes", file.filename)
    try:
        return await run_in_threadpool(quote_mod.audit_quote_bytes, data, file.filename, recommended_kw)
    except ValueError as e:
        raise HTTPException(422, f"Couldn't read the quote — paste the text instead. ({e})")
    except Exception as e:
        raise bedrock_error(e)


@app.post("/api/quote/text")
def audit_quote_text(req: QuoteTextRequest):
    return quote_mod.audit_quote(req.text, recommended_kw=req.recommended_kw)


@app.post("/api/locate")
def locate(req: LocateRequest, request: Request):
    """Best location, automatically: GPS city > PIN/city on the bill > network address > state capital."""
    return geo.locate(lat=req.lat, lon=req.lon, accuracy_m=req.accuracy_m, bill=req.bill,
                      ip=client_ip(request), query=req.query, prefer=req.prefer)


def _run_analysis(*, location: dict, bill: Optional[dict], monthly_units: Optional[float], **kw) -> dict:
    lat, lon = location.get("lat"), location.get("lon")
    if lat is None or lon is None or not geo.in_india_box(lat, lon):
        raise HTTPException(422, "A location inside India is needed (lat/lon).")
    units = monthly_units or (bill or {}).get("avg_monthly_units") or (bill or {}).get("units_consumed")
    if not units:
        raise HTTPException(422, "Monthly units are needed: read them from the bill or enter them.")
    try:
        return analysis.analyze(monthly_units=float(units), location=location, bill=bill, **kw)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    """Whole report: sizing, last-1-year / last-5-year generation from real weather in ₹, per-panel
    earnings, break-even year, subsidy cash-flow and coal / petrol / pollution avoided. No AI."""
    return _run_analysis(location=req.location, bill=req.bill, monthly_units=req.monthly_units,
                         roof_area_m2=req.roof_area_m2, kw_override=req.kw_override,
                         sanctioned_load_kw=req.sanctioned_load_kw, flat_tariff=req.flat_tariff)


@app.post("/api/auto")
async def auto(request: Request, file: UploadFile = File(...), lat: Optional[float] = Form(None),
               lon: Optional[float] = Form(None), accuracy_m: Optional[float] = Form(None),
               prefer: Optional[str] = Form(None), query: Optional[str] = Form(None),
               roof_area_m2: Optional[float] = Form(None)):
    """Upload ONE bill photo; everything else is automatic (bill -> location -> weather -> report)."""
    data = await read_upload(file)
    save_to_s3(data, "bills", file.filename)
    try:
        bill = await run_in_threadpool(bill_mod.read_bill_bytes, data, file.filename)
    except ValueError as e:
        raise HTTPException(422, f"Couldn't read the bill — try a sharper, flatter photo. ({e})")
    except Exception as e:
        raise bedrock_error(e)
    loc = await run_in_threadpool(lambda: geo.locate(lat=lat, lon=lon, accuracy_m=accuracy_m, bill=bill,
                                                     ip=client_ip(request), query=query,
                                                     prefer=prefer if prefer in ("gps", "bill") else None))
    out = {"bill": bill, "location": loc, "report": None, "needs_input": []}
    if not bill.get("avg_monthly_units"):
        out["needs_input"].append("monthly_units")
    if loc.get("needs_manual"):
        out["needs_input"].append("location")
    if out["needs_input"]:
        return out
    out["report"] = await run_in_threadpool(
        lambda: _run_analysis(location=loc, bill=bill, monthly_units=None, roof_area_m2=roof_area_m2))
    return out


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from surya import agent                    # lazy: API starts even if strands has an issue
    try:
        sid, reply = agent.chat(req.message, req.session_id, req.context, req.lang)
    except Exception as e:
        raise bedrock_error(e)
    return ChatResponse(session_id=sid, reply=reply)


# ---------------------------------------------------------------- frontend
@app.get("/", include_in_schema=False)
def index():
    page = WEB_DIR / "index.html"
    if page.exists():
        return FileResponse(page)
    return JSONResponse({"app": "Surya Sahayak API", "docs": "/api/docs"})
