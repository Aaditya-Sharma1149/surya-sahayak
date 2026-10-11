"""Boot the REAL app (api.py + web/index.html) with every outside service faked, for browser tests.

USED ONLY BY TESTS. In production none of this exists: the app talks to Bedrock, Open-Meteo, NASA POWER,
PVGIS and the map services, and says so plainly when one of them is down.
"""
from __future__ import annotations

import copy
import io
import pathlib
import shutil
import socket
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import requests  # noqa: E402
import uvicorn  # noqa: E402

import fakes  # noqa: E402

NOMINATIM_PUNE = {"place_id": 4, "lat": "18.5204", "lon": "73.8567", "address": {
    "suburb": "Shivajinagar", "city": "Pune", "state_district": "Pune", "state": "Maharashtra",
    "ISO3166-2-lvl4": "IN-MH", "postcode": "411005", "country": "India", "country_code": "in"}}

BILL_DELHI = {"discom": "BSES Rajdhani", "state": "DL", "consumer_name": "TEST USER", "ca_number": "100000000",
              "bill_month": "2026-09", "units_consumed": 512, "billing_days": 30, "sanctioned_load_kw": 5,
              "service_address": "B-12, Hauz Khas, New Delhi 110016", "pincode": "110016", "city": "New Delhi",
              "tariff_category": "Domestic", "energy_charges": 2890, "fixed_charges": 250, "govt_subsidy": 0,
              "total_payable": 3430, "consumption_history": [], "confidence": "high", "notes": ""}
BILL_PUNE = {**BILL_DELHI, "discom": "MSEDCL", "state": "MH", "service_address": "Shivajinagar, Pune 411005",
             "pincode": "411005", "city": "Pune", "units_consumed": 380, "energy_charges": 3420, "sanctioned_load_kw": 4}
BILL_NO_UNITS = {**BILL_DELHI, "units_consumed": None, "consumption_history": [], "confidence": "low"}
BILL_NO_PLACE = {**BILL_DELHI, "discom": None, "state": None, "service_address": None, "pincode": None, "city": None}

QUOTE_FIELDS = {"vendor_name": "Test Solar", "system_kw": 3, "total_price": 285000, "gst_included": True,
                "panel_make": "Waaree", "panel_wattage_wp": 545, "panel_dcr": None, "inverter_make": "Growatt",
                "empanelment_id": None, "line_items": ["3 kW on-grid system"],
                "full_text": "Supply and installation of 3 kW On-Grid Rooftop Solar. Total Rs. 2,85,000 inclusive of GST."}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def jpeg_bill_image() -> bytes:
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (900, 1200), "#fbfaf4")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 900, 120], fill="#0b4a8f")
    d.text((30, 40), "BSES RAJDHANI POWER LTD  -  ELECTRICITY BILL (SAMPLE FOR TESTING)", fill="white")
    rows = ["Consumer: TEST USER", "CA No: 100000000", "Units consumed: 512 kWh", "Sanctioned load: 5 kW",
            "Energy charges: Rs 2,890", "Total payable: Rs 3,430"]
    for i, r in enumerate(rows):
        d.text((40, 180 + i * 60), r, fill="#222")
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return buf.getvalue()


class World:
    """Holds the fake outside world. `set(...)` swaps scenarios between browser tests."""

    def __init__(self):
        self.cache = pathlib.Path(tempfile.mkdtemp(prefix="surya_e2e_"))
        import os
        os.environ["SURYA_CACHE_DIR"] = str(self.cache / "cache")
        from surya import geo, llm
        geo.NOMINATIM_MIN_INTERVAL = 0
        self.geo, self.llm = geo, llm
        self.calls: list = []
        self.chat_log: list = []
        self.llm_calls = 0
        self.scenario: dict = {}
        llm.ask_vision_json = self._vision
        self.set()

    def _vision(self, data, filename, prompt, model_id=None, max_tokens=2000):
        self.llm_calls += 1
        if "quotation" in prompt.lower() or "quote" in prompt.lower()[:200]:
            return copy.deepcopy(QUOTE_FIELDS)
        if self.scenario.get("unreadable"):
            raise ValueError("No JSON in model output: 'I cannot read this image'")
        return copy.deepcopy(self.scenario.get("bill", BILL_DELHI))

    def set(self, **scenario):
        from surya import engine
        self.scenario = scenario
        shutil.rmtree(self.cache / "cache", ignore_errors=True)
        engine._pvgis_cached.cache_clear()
        self.geo._rev_cache.clear()
        self.geo._fwd_cache.clear()
        self.calls.clear()
        inner = fakes.make_fake_get(
            fail=scenario.get("fail", ()), reverse=scenario.get("reverse", fakes.NOMINATIM_DELHI),
            search=scenario.get("search", fakes.NOMINATIM_DELHI), pvgis_annual=scenario.get("pvgis", 1450.0),
            calls=self.calls)
        delay = float(scenario.get("delay", 0))

        def slow_get(url, *a, **kw):
            if delay and "archive-api.open-meteo.com" in url:
                time.sleep(delay)
            return inner(url, *a, **kw)
        requests.get = slow_get
        return self

    def fake_chat(self):
        """Replace the Bedrock agent with a canned reply that proves the report context arrived."""
        from surya import agent

        def chat(message, session_id=None, context=None, lang="en"):
            self.chat_log.append({"message": message, "lang": lang, "context": context})
            plan = (context or {}).get("plan") or {}
            return (session_id or "sess-1"), f"[test reply] kw={plan.get('kw')} city={plan.get('city')} lang={lang}"
        agent.chat = chat


def start(port: int | None = None):
    """Start uvicorn in a daemon thread. Returns (base_url, world)."""
    import api
    from surya import quote as quote_mod  # noqa: F401  (imported so the module is loaded before patching)
    world = World()
    try:
        world.fake_chat()
    except Exception as e:          # strands missing in this environment: chat test is skipped
        print("chat faking skipped:", e)
    port = port or free_port()
    server = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    import urllib.request                                  # health check bypasses `requests`, which is faked
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError("server did not start")
    return f"http://127.0.0.1:{port}", world
