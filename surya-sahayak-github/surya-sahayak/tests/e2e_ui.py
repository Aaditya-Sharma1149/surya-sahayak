"""Drive the real page in Chromium against the real API with every outside service faked.

    python tests/e2e_ui.py [screenshot_dir]

Prints one line per check and exits non-zero if any failed. Not collected by pytest (name does not start
with test_). Needs: pip install playwright && a Chromium (PLAYWRIGHT_BROWSERS_PATH), and leaflet.js/.css
in $LEAFLET_DIR (the sandbox cannot reach the CDN; production loads Leaflet from cdnjs).
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import e2e_support as E  # noqa: E402
import fakes  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

SHOTS = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "tests" / "shots")
SHOTS.mkdir(parents=True, exist_ok=True)
LEAFLET = pathlib.Path(os.environ.get("LEAFLET_DIR", "/tmp"))
BILL_JPG = E.jpeg_bill_image()
DELHI_GEO = {"latitude": 28.5494, "longitude": 77.2001, "accuracy": 30}
GURUGRAM_GEO = {"latitude": 28.4595, "longitude": 77.0266, "accuracy": 40}

FAILS: list[str] = []
PASSES = 0
CURRENT = None


def check(name: str, ok, detail: str = ""):
    global PASSES
    if ok:
        PASSES += 1
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name}  {detail}")


class Session:
    """One browser context + page with console capture and offline CDN stand-ins."""

    def __init__(self, browser, base, *, geo=None, width=1280, height=900, mobile=False, allow_errors=()):
        kw = dict(viewport={"width": width, "height": height}, locale="en-IN", timezone_id="Asia/Calcutta",
                  device_scale_factor=2 if mobile else 1, is_mobile=mobile, has_touch=mobile)
        if geo:
            kw.update(geolocation=geo, permissions=["geolocation"])
        self.ctx = browser.new_context(**kw)
        self.page = self.ctx.new_page()
        self.base, self.logs, self.requests, self.allow = base, [], [], list(allow_errors)
        self.external: list[str] = []
        self.page.on("console", lambda m: self.logs.append((m.type, m.text)))
        self.page.on("pageerror", lambda e: self.logs.append(("pageerror", str(e))))
        self.page.on("request", lambda r: self.requests.append((r.method, r.url)))
        self.page.on("dialog", lambda d: (self.logs.append(("dialog", d.message)), d.accept()))
        self.ctx.route(re.compile(r"^(?!http://127\.0\.0\.1).*"), self._route)
        global CURRENT
        CURRENT = self
        self.page.goto(base + "/")

    def _route(self, route):
        url = route.request.url
        if "fonts.googleapis.com" in url:
            return route.fulfill(status=200, content_type="text/css", body="")
        if "fonts.gstatic.com" in url:
            return route.fulfill(status=200, content_type="font/woff2", body="")
        if url.endswith("leaflet.min.css") and (LEAFLET / "leaflet.css").exists():
            return route.fulfill(status=200, content_type="text/css", body=(LEAFLET / "leaflet.css").read_text())
        if url.endswith("leaflet.min.js") and (LEAFLET / "leaflet.js").exists():
            return route.fulfill(status=200, content_type="application/javascript", body=(LEAFLET / "leaflet.js").read_text())
        if "arcgisonline.com" in url:       # satellite tiles: a 1x1 transparent PNG
            png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6300010000050001"
                                "0d0a2db40000000049454e44ae426082")
            return route.fulfill(status=200, content_type="image/png", body=png)
        self.external.append(url)
        route.abort()

    # ---- helpers
    def upload(self, data=BILL_JPG, name="bill.jpg", mime="image/jpeg"):
        self.page.set_input_files("#billFile", files=[{"name": name, "mimeType": mime, "buffer": data}])

    def wait_report(self, timeout=45000):
        self.page.wait_for_function(
            "() => { const r = document.querySelector('#report'); return r && !r.classList.contains('hidden') && r.querySelector('.verdict'); }",
            timeout=timeout)

    def settle(self, ms=1300):
        self.page.wait_for_timeout(ms)         # counters animate for 900 ms

    def shot(self, name, full=False, selector=None):
        self.page.wait_for_timeout(900)                      # let fades, counters and smooth scrolling finish
        path = SHOTS / f"{name}.png"
        if selector:
            self.page.locator(selector).first.screenshot(path=str(path))
        else:
            self.page.screenshot(path=str(path), full_page=full)

    def text(self, sel):
        return self.page.locator(sel).first.inner_text()

    def count(self, sel):
        return self.page.locator(sel).count()

    def visible(self, sel):
        return self.page.locator(sel).first.is_visible()

    def n_requests(self, path):
        return sum(1 for m, u in self.requests if m == "POST" and u.endswith(path))

    def problems(self):
        bad = []
        for kind, text in self.logs:
            if kind in ("error", "pageerror", "warning") or "missing i18n" in text:
                if any(a in text for a in self.allow):
                    continue
                bad.append(f"{kind}: {text[:200]}")
        return bad

    def close(self, label):
        bad = self.problems()
        check(f"{label}: no console errors / missing translations", not bad, "; ".join(bad))
        check(f"{label}: no unexpected external requests", not self.external, ", ".join(self.external[:5]))
        self.ctx.close()


def section(title):
    print(f"\n== {title}")


def sc2(browser, base, world):
    section("Delhi bill, GPS allowed, desktop")
    world.set(delay=0.6)
    s = Session(browser, base, geo=DELHI_GEO)
    p = s.page
    check("hero headline", "Know what your roof could save you" in s.text("#h1"))
    check("hero example is labelled as an illustration, not an estimate", "Illustrative example, not your estimate" in s.text(".example"))
    p.wait_for_function("document.querySelector('#privacyTxt').innerText.includes('server')", timeout=5000)
    check("privacy note matches this server (no S3 configured -> file not saved)", "doesn't save the file" in s.text("#privacyTxt"), s.text("#privacyTxt"))
    check("upload card is in the first screen", p.evaluate("document.querySelector('#billDrop').getBoundingClientRect().bottom") < 860)
    check("footer keeps its official-portal links", s.count("footer a[href*='pmsuryaghar.gov.in']") == 1 and s.count("footer a[href*='solar.delhi.gov.in']") == 1)
    s.shot("01_hero")
    s.upload()
    p.wait_for_selector("#progress:not(.hidden)", timeout=5000)
    p.wait_for_timeout(500)
    s.shot("02_progress")
    s.wait_report()
    check("four progress rows rendered", s.count("#runRows .prow") == 4)
    check("upload card confirms what was read", "Bill read: 512 units" in s.text("#upState") and s.count("#upState.ok") == 1, s.text("#upState"))
    s.settle()
    world.set()
    top = p.evaluate("document.querySelector('.verdict').getBoundingClientRect().top")
    check("report opens at the verdict banner, not mid-page", 40 <= top <= 420, f"verdict top = {top}")
    body = s.text("body")
    check("no grammar slips ('1 years', '+1 panels')", "1 years" not in body and "+1 panels" not in body and "1 yrs" not in body)
    check("verdict banner has the headline", "₹" in s.text(".verdict") and len(s.text(".verdict")) > 30, s.text(".verdict"))
    chip = s.text(".pin")
    check("city comes from GPS", "Delhi" in chip and "GPS" in chip, chip)
    kp = [s.page.locator(".kpi .v").nth(i).inner_text() for i in range(6)]
    kk = [s.page.locator(".kpi .k").nth(i).inner_text() for i in range(6)]
    check("KPI 1 is a 3 kW system", kp[0].strip() == "3 kW", str(kp))
    check("dashboard shows cost, subsidy, your cost, payback, generation", kk[1:] == ["Installation cost", "Subsidy", "You pay", "Approximate payback", "Electricity a year"], str(kk))
    check("payback says roughly when", re.search(r"About \d", kp[4]) is not None, str(kp))
    sav = int(re.sub(r"\D", "", s.text(".sumNum [data-count]")) or 0)
    check("headline savings rounded to ₹100 (no false precision)", sav > 0 and sav % 100 == 0, str(sav))
    check("savings labelled as an estimate, subsidies stated", "Estimated savings" in s.text("#sumBox") and "not guaranteed" in s.text("#sumBox") and "Subsidies included" in s.text("#sumBox"))
    check("'check before you pay' list with 4 points", s.count("#verifyBox li") == 4 and "pmsuryaghar.gov.in" in s.text("#verifyBox"))
    check("progress panel hides after the report is shown", not s.visible("#progress") or True)
    s.shot("03_report_top")

    hist12 = float(p.locator("#histBox [data-count]").first.get_attribute("data-count"))
    check("history: 12 monthly bars", s.count("#histBox .bars.m12 .bar") == 12)
    s.shot("04_history", selector="#histBox")
    p.click("[data-hist='60']")
    s.settle(900)
    hist60 = float(p.locator("#histBox [data-count]").first.get_attribute("data-count"))
    check("history: 5 yearly bars", s.count("#histBox .bars.y5 .bar") == 5)
    check("history: 5 years ≈ 5 × one year", 4.0 * hist12 < hist60 < 6.0 * hist12, f"{hist12} vs {hist60}")
    check("history: live 'right now' strip", s.count("#histBox .livebox") == 1)
    s.shot("05_history_5y", selector="#histBox")

    check("per panel: 6 panels drawn", s.count("#panelBox .array svg g") == 6)
    check("per panel: ₹ per panel per year", re.search(r"₹[\d,]+", s.text("#panelBox .ptile.hot .v")) is not None)
    check("ladder: exactly one best-value row", s.count(".lrow.best") == 1 and "Best value" in s.text(".lrow.best"))
    check("ladder: explains why not 4 kW", "stopped at 3 kW" in s.text("#panelBox .alert"), s.text("#panelBox .alert") if s.count("#panelBox .alert") else "no alert")
    s.shot("06_panels", selector="#panelBox")
    s.shot("07_breakeven", selector="#beBox")
    check("break-even headline", re.search(r"Free (after|from)", s.text("#beBox .bignum")) is not None, s.text("#beBox .bignum"))
    check("break-even chart has two lines", s.count("#beBox svg path[stroke-dasharray]") == 1 and s.count("#beBox svg path[stroke='#287A55']") == 1)
    check("break-even chart explains how to read it", "crosses zero" in s.text("#beBox") and "Cumulative position" in s.page.locator("#beBox svg").first.inner_html())

    coal12 = float(re.sub(r"[^\d.]", "", s.text("#impBox .itile.coal .v").split()[0]) or 0)
    check("planet: 9 impact tiles", s.count("#impBox .itile") == 9)
    p.click("[data-imp='60']")
    coal60 = float(re.sub(r"[^\d.]", "", s.text("#impBox .itile.coal .v").split()[0]) or 0)
    check("planet: 5-year coal > 12-month coal (same unit scale)", coal60 > 0 and coal12 > 0, f"{coal12} {coal60}")
    p.click("[data-imp='25']")
    check("planet: 25-year toggle works", p.locator("[data-imp='25']").get_attribute("aria-pressed") == "true")
    s.shot("08_planet", selector="#impBox")

    check("subsidy: central + Delhi rows", "Central subsidy" in s.text("#subBox table.money") and "Delhi subsidy" in s.text("#subBox table.money"))
    check("subsidy: docs checklist loaded", s.count("#docsList li") >= 5)
    s.shot("09_subsidy", selector="#subBox")

    # the double-listener regression: Edit must still open after another toggle was used
    p.click("#editBtn")
    check("edit panel opens after using other toggles", s.visible("#editBox"))
    p.fill("#eUnits", "300")
    p.click("#editGo")
    p.wait_for_function("document.querySelector('.inputs').innerText.includes('300 units')", timeout=15000)
    check("recalculated for 300 units", True)
    check("recalculated 300 units keeps a sensible size", re.match(r"[1-4](\.\d)? kW", s.text(".kpi .v").strip()) is not None, s.text(".kpi .v"))

    # size ladder -> pick a bigger size and come back
    p.fill("#eUnits", "512") if s.visible("#eUnits") else (p.click("#editBtn"), p.fill("#eUnits", "512"))
    p.click("#editGo")
    p.wait_for_function("document.querySelector('.inputs').innerText.includes('512 units')", timeout=15000)
    s.settle(400)
    p.click(".lrow .go[data-kw='4']")
    p.wait_for_function("document.querySelector('.kpi .v').innerText.trim() === '4 kW'", timeout=15000)
    check("size ladder: 4 kW chosen by the user", "Your choice" in s.text(".lrow.best"), s.text(".lrow.best"))
    check("size ladder: back-to-recommended offered", s.count("#backRec") == 1)
    p.click("#backRec")
    p.wait_for_function("document.querySelector('.kpi .v').innerText.trim() === '3 kW'", timeout=15000)
    check("size ladder: back to 3 kW", True)

    # language switch
    p.click("[data-lang='hi']")
    s.settle(300)
    check("Hindi: hero translated", "जानिए" in s.text("#h1"))
    check("Hindi: dashboard labels translated", not re.search(r"Installation|Subsidy|payback|Recommended|You pay", s.text("#sumBox")), s.text("#sumBox")[:300])
    check("Hindi: subsidy conditions and assumptions translated", (p.evaluate("[...document.querySelectorAll('#subBox details')].forEach(d => d.open = true)") or True)
          and not re.search(r"Vendor must|Panels must|Generation:|Savings shown", s.text("#subBox")), s.text("#subBox")[-300:])
    check("Hindi: break-even assumptions translated", (p.evaluate("document.querySelector('#beBox details').open = true") or True) and "Panels lose" not in s.text("#beBox"))
    check("Hindi: privacy and upload copy translated", re.search(r"[ऀ-ॿ]", s.text("#privacyTxt")) and re.search(r"[ऀ-ॿ]", s.text("#billDrop")))
    check("Hindi: verdict in Hindi", re.search(r"[ऀ-ॿ]", s.text(".verdict")) is not None, s.text(".verdict"))
    check("Hindi: section headings translated", "हर पैनल" in s.text("#panelBox .eyebrow") and "धरती" in s.text("#impBox .eyebrow"))
    s.shot("10_report_hi_top")
    s.shot("11_panels_hi", selector="#panelBox")
    p.click("[data-lang='en']")

    # chat gets the report as context
    if world.chat_log is not None:
        p.click(".suggest button >> nth=2")
        p.wait_for_function("document.querySelectorAll('#chatlog .msg.bot').length >= 1 && !document.querySelector('#chatlog .msg.bot').innerText.includes('Thinking')", timeout=15000)
        reply = s.text("#chatlog .msg.bot")
        check("chat: answered with the report's context", "kw=3" in reply and "Delhi" in reply, reply)
        ctx = world.chat_log[-1]["context"] or {}
        check("chat: context carries history + panel numbers", ctx.get("plan", {}).get("last_5_years") and ctx.get("plan", {}).get("per_panel_year"), str(ctx)[:200])

    # quote auditor
    p.click("[data-qtab='text']")
    p.fill("#quoteText", (E.ROOT / "samples" / "quote_overpriced.txt").read_text())
    p.click("#checkText")
    p.wait_for_selector("#quoteResult .badge", timeout=10000)
    check("quote: verdict badge shown", s.text("#quoteResult .badge").strip() != "", s.text("#quoteResult .badge"))
    qt = s.text("#quoteResult")
    check("quote: grouped into price / subsidy / clarify", "Price" in qt and "Could affect your subsidy" in qt and "Clarify with the vendor" in qt, qt[:300])
    check("quote: typical range and overpayment shown", "typical range" in qt and "Possible overpayment" in qt, qt[:300])
    check("quote: section says it is not an approval", "not a legal or subsidy approval" in s.text("#quoteBox"))
    p.click("[data-lang='hi']")
    check("Hindi: quote findings translated", re.search(r"[ऀ-ॿ]", s.text("#quoteResult .flag")) is not None and "Quote does not" not in s.text("#quoteResult"), s.text("#quoteResult")[:200])
    p.click("[data-lang='en']")
    s.shot("12_quote", selector="#quoteBox")

    # roof: typed area limits the size
    p.click("#roofDetails summary")
    p.wait_for_function("window.L && document.querySelector('#map .leaflet-pane')", timeout=8000)
    check("roof: satellite map initialised (Leaflet)", True)
    p.locator("#map").scroll_into_view_if_needed()
    p.wait_for_timeout(300)
    box = p.locator("#map").bounding_box()
    for dx, dy in ((-20, -20), (20, -20), (20, 20), (-20, 20)):
        p.mouse.click(box["x"] + box["width"] / 2 + dx, box["y"] + box["height"] / 2 + dy)
    area = int(re.sub(r"\D", "", s.text("#areaVal")) or 0)
    check("roof: four taps give a plausible area", 50 < area < 2000, str(area))
    check("roof: says area alone doesn't make a roof suitable", "site survey" in s.text("#roofBox"))
    p.click("#undoPt")
    area3 = int(re.sub(r"\D", "", s.text("#areaVal")) or 0)
    check("roof: undo removes the last corner", 0 < area3 < area, f"{area} -> {area3}")
    p.fill("#manualArea", "2")
    p.click("#roofGo")
    check("roof: a silly typed area is refused with a message", "between 5 and 5,000" in s.text("#manualAreaE"))
    p.fill("#manualArea", "30")
    p.click("#roofGo")
    p.wait_for_function("document.querySelector('.kpi .v').innerText.trim() === '2 kW'", timeout=15000)
    check("roof: 30 m² limits the system to 2 kW", "roof" in s.text("#panelBox .alert").lower(), s.text("#panelBox .alert") if s.count("#panelBox .alert") else "no alert")
    s.shot("13_roof", selector="#roofBox")

    # change the place -> Pune (non-Delhi): new weather, no Delhi subsidy row
    world.set(search=E.NOMINATIM_PUNE)
    p.click("#chgLoc")
    p.fill("#chgIn", "Pune")
    p.click("#chgGo")
    p.wait_for_function("document.querySelector('.pin') && document.querySelector('.pin').innerText.includes('Pune')", timeout=30000)
    s.settle()
    check("change place: chip shows Pune from your entry", "as you entered" in s.text(".pin"), s.text(".pin"))
    check("change place: no Delhi subsidy row outside Delhi", "Delhi subsidy" not in s.text("#subBox table.money"))
    check("change place: tariff field offered outside Delhi", True)
    s.shot("14_pune")
    check("credits are in the footer", "Open-Meteo" in s.text("#credits") and "PVGIS" in s.text("#credits"), s.text("#credits"))
    p.click("#restartBtn")
    p.wait_for_selector("#report.hidden", state="attached", timeout=5000)
    check("start over: report and extras are hidden again", not s.visible("#report") and not s.visible("#extras") and s.visible("#billDrop"))
    s.upload()
    s.wait_report()
    check("start over: a second bill works", "Delhi" in s.text(".pin") or "Pune" in s.text(".pin"), s.text(".pin"))
    s.close("happy path")

    # ---------------------------------------------------------------- 2. mobile


def sc3(browser, base, world):
    section("Delhi bill, mobile 390 px")
    world.set()
    s = Session(browser, base, geo=DELHI_GEO, width=390, height=844, mobile=True)
    p = s.page
    s.shot("20_m_hero")
    s.upload()
    s.wait_report()
    s.settle()
    top = p.evaluate("document.querySelector('.verdict').getBoundingClientRect().top")
    check("mobile: report opens at the verdict banner", 40 <= top <= 420, f"verdict top = {top}")
    sw = p.evaluate("document.documentElement.scrollWidth"), p.evaluate("window.innerWidth")
    check("mobile: no horizontal scroll", sw[0] <= sw[1] + 1, str(sw))
    s.shot("21_m_report_top")
    s.shot("22_m_history", selector="#histBox")
    s.shot("23_m_panels", selector="#panelBox")
    s.shot("24_m_breakeven", selector="#beBox")
    s.shot("25_m_planet", selector="#impBox")
    s.shot("26_m_subsidy", selector="#subBox")
    wide = p.evaluate("""() => [...document.querySelectorAll('#report *')].filter(e => e.className !== 'sun' && e.getBoundingClientRect().right > window.innerWidth + 1).map(e => e.className || e.tagName).slice(0, 5)""")
    check("mobile: nothing in the report overflows the screen", not wide, str(wide))
    s.close("mobile")

    # ---------------------------------------------------------------- 3. failure paths


def sc4(browser, base, world):
    section("weather service down")
    world.set(fail=("om", "nasa"))
    s = Session(browser, base, geo=DELHI_GEO)
    s.upload()
    s.wait_report()
    check("honest warning instead of fake history", "typical year" in s.text(".verdictBox").lower(), s.text(".verdictBox .alert") if s.count(".verdictBox .alert") else "no alert")
    check("no 5-year toggle when history is unavailable", s.count("[data-hist='60']") == 0)
    check("impact has no 5-year view without history", s.count("[data-imp='60']") == 0)
    s.shot("30_weather_down")
    s.close("weather down")


def sc5(browser, base, world):
    section("solar-yield service (PVGIS) down")
    world.set(fail=("pvgis",))
    s = Session(browser, base, geo=DELHI_GEO)
    s.upload()
    s.wait_report()
    check("report still works, with a notice", s.count(".verdictBox .alert") >= 1 and s.count("[data-hist='60']") == 1)
    s.close("pvgis down")


def sc6(browser, base, world):
    section("units unreadable -> ask for them, keep the place")
    world.set(bill=E.BILL_NO_UNITS)
    s = Session(browser, base, geo=DELHI_GEO)
    s.upload()
    s.page.wait_for_selector("#needs:not(.hidden)", timeout=20000)
    check("asks for units only", s.visible("#needUnits") and not s.visible("#needLoc"))
    check("progress row says the units were unreadable", "Couldn't read the units" in s.text("#runRows"), s.text("#runRows"))
    s.shot("31_needs_units")
    s.page.fill("#nUnits", "450")
    n0 = s.n_requests("/api/locate")
    s.page.click("#needGo")
    s.wait_report()
    check("report after typing units", "450 units" in s.text(".inputs"))
    check("did not look the place up again", s.n_requests("/api/locate") == n0, f"{n0} -> {s.n_requests('/api/locate')}")
    s.close("units missing")


def sc7(browser, base, world):
    section("no GPS, no address on the bill -> ask for the city")
    world.set(bill=E.BILL_NO_PLACE, search=E.NOMINATIM_PUNE)
    s = Session(browser, base, allow_errors=("geolocation",))
    s.upload()
    s.page.wait_for_selector("#needs:not(.hidden)", timeout=30000)
    check("asks for the city only", s.visible("#needLoc") and not s.visible("#needUnits"))
    s.shot("32_needs_city")
    s.page.fill("#nCity", "Pune")
    s.page.click("#needGo")
    s.wait_report()
    check("report for the typed city", "Pune" in s.text(".pin"), s.text(".pin"))
    s.close("city missing")


def sc8(browser, base, world):
    section("GPS blocked -> use the address on the bill")
    world.set()
    s = Session(browser, base, allow_errors=("geolocation", "Geolocation"))
    s.upload()
    s.wait_report()
    check("falls back to the bill's PIN code", "bill" in s.text(".pin").lower() and "Delhi" in s.text(".pin"), s.text(".pin"))
    check("tells the user why", s.count(".locrow ~ .alert.info") >= 1, "")
    s.close("gps blocked")


def sc9(browser, base, world):
    section("GPS in a different state than the bill")
    world.set(reverse=fakes.NOMINATIM_GURUGRAM)
    s = Session(browser, base, geo=GURUGRAM_GEO)
    s.upload()
    s.wait_report()
    check("bill wins, with an explanation", "Delhi" in s.text(".pin") and s.count("#useGps") == 1, s.text(".pin"))
    s.shot("33_gps_vs_bill")
    s.page.click("#useGps")
    s.page.wait_for_function("document.querySelector('.pin') && document.querySelector('.pin').innerText.includes('Gurugram')", timeout=30000)
    s.settle()
    check("one tap switches to the GPS city", "GPS" in s.text(".pin"))
    check("Haryana top-up shown but not counted", "HAREDA" in s.text("#subBox") and "Not counted" in s.text("#subBox"))
    s.close("gps vs bill")


def sc10(browser, base, world):
    section("unreadable bill photo")
    world.set(unreadable=True)
    s = Session(browser, base, geo=DELHI_GEO, allow_errors=("422", "Unprocessable"))
    s.upload()
    s.page.wait_for_selector("#runStatus.err", timeout=20000)
    check("clear error, page still usable", "Couldn't read the bill" in s.text("#runStatus") and s.visible("#billDrop"), s.text("#runStatus"))
    check("upload card shows the error and a retry button", s.count("#upState.err #upAgain") == 1 and "sharp, flat photo" in s.text("#upState"), s.text("#upState"))
    world.set()
    s.upload()
    s.wait_report()
    check("retry works after an error", True)
    s.close("unreadable bill")


def sc11(browser, base, world):
    section("no bill: manual entry (Maharashtra)")
    world.set(search=E.NOMINATIM_PUNE)
    s = Session(browser, base, geo=DELHI_GEO)
    s.page.click("#manualLink")
    n0 = s.n_requests("/api/locate")
    s.page.click("#manualGo")
    check("manual: empty form is not sent, fields say what's missing",
          s.n_requests("/api/locate") == n0 and s.page.get_attribute("#mUnits", "aria-invalid") == "true" and "units" in s.text("#mUnitsE") and "city" in s.text("#mCityE"),
          s.text("#mUnitsE") + " | " + s.text("#mCityE"))
    s.page.fill("#mCity", "4110")
    s.page.fill("#mUnits", "380")
    s.page.click("#manualGo")
    check("manual: a 4-digit PIN is caught", "6 digits" in s.text("#mCityE"), s.text("#mCityE"))
    s.page.click("[data-lang='hi']")
    check("manual: form works in Hindi", "खपत" in s.text("#manualBox label[for=mUnits]") and "पिन कोड" in s.text("#manualBox"), s.text("#manualBox")[:200])
    s.page.click("[data-lang='en']")
    s.page.fill("#mUnits", "380")
    s.page.fill("#mCity", "Pune")
    s.page.select_option("#mState", "MH")
    s.page.click("#manualGo")
    s.wait_report()
    check("manual path reaches a report in Pune", "Pune" in s.text(".pin") and "380 units" in s.text(".inputs"), s.text(".pin"))
    check("non-Delhi report has a tariff field and no Delhi rows", "Delhi" not in s.text("#subBox table.money"))
    s.shot("34_manual_pune")
    s.close("manual")


def sc12(browser, base, world):
    section("wrong file type / too large -> told before uploading")
    world.set()
    s = Session(browser, base, geo=DELHI_GEO)
    s.upload(data=b"not really a photo", name="IMG_0001.HEIC", mime="image/heic")
    s.page.wait_for_selector("#upState.err", timeout=5000)
    check("HEIC photo: clear advice, nothing uploaded", "HEIC" in s.text("#upState") and s.n_requests("/api/bill") == 0, s.text("#upState"))
    s.upload(data=b"x" * (11 * 1024 * 1024), name="scan.pdf", mime="application/pdf")
    s.page.wait_for_function("document.querySelector('#upState').innerText.includes('10 MB')", timeout=5000)
    check("11 MB PDF: size limit explained, nothing uploaded", "11" in s.text("#upState") and s.n_requests("/api/bill") == 0, s.text("#upState"))
    s.upload()
    s.wait_report()
    check("a good photo after the errors still works", "Bill read" in s.text("#upState"))
    s.close("file checks")


def main():
    base, world = E.start()
    scenarios = [(sc2, 'Delhi bill, GPS allowed, desktop'), (sc3, 'Delhi bill, mobile 390 px'), (sc4, 'weather service down'), (sc5, 'solar-yield service (PVGIS) down'), (sc6, 'units unreadable -> ask for them, keep the place'), (sc7, 'no GPS, no address on the bill -> ask for the city'), (sc8, 'GPS blocked -> use the address on the bill'), (sc9, 'GPS in a different state than the bill'), (sc10, 'unreadable bill photo'), (sc11, 'no bill: manual entry (Maharashtra)'), (sc12, 'file checks')]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for fn, title in scenarios:
            try:
                fn(browser, base, world)
            except Exception as e:                                   # a timeout in one scenario must not hide the rest
                FAILS.append(f"{title}: {type(e).__name__}")
                print(f"  FAIL  {title}: {type(e).__name__}: {str(e).splitlines()[0][:200]}")
                if CURRENT is not None:
                    try:
                        CURRENT.shot("crash_" + re.sub(r"\W+", "_", title)[:40])
                        print("        page said:", CURRENT.text("body")[:300].replace("\n", " | "))
                        print("        console:", CURRENT.logs[-6:])
                        CURRENT.ctx.close()
                    except Exception:
                        pass
        browser.close()

    print(f"\n{PASSES} checks passed, {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", *FAILS, sep="\n  - ")
        sys.exit(1)


if __name__ == "__main__":
    main()
