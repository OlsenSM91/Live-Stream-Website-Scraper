# toast.py
import hashlib, json, re, time, contextlib, logging, sys, asyncio
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import urlparse, urljoin
from contextlib import asynccontextmanager

import aiohttp
from aiohttp import ClientTimeout
from bs4 import BeautifulSoup
from fastapi import FastAPI, Response, Query, Body
from pydantic import BaseModel
from dateutil import tz

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ace-monitor")

# -----------------------------
# Config – put only URLs you’re allowed to crawl
# -----------------------------
START_URLS = [
    "https://v1.gostreameast.link/#streams"
    # "https://example.com/your-listing-page"
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
SCRAPE_INTERVAL_SECONDS = 90
TIMEZONE = tz.gettz("America/Los_Angeles")

# Use Selenium only (more reliable on Windows than Playwright here)
USE_HEADLESS = True

# -----------------------------
# Data models (public metadata only)
# -----------------------------
class Evidence(BaseModel):
    page_url: str
    fetched_at_utc: str
    status: int
    server_ip: Optional[str] = None
    response_headers: Dict[str, str] = {}
    listing_html_sha256: str
    notes: Optional[str] = None

class HeadSnapshot(BaseModel):
    url: Optional[str] = None
    status: Optional[int] = None
    server_ip: Optional[str] = None
    headers: Dict[str, str] = {}

class RequestObservables(BaseModel):
    scheme: Optional[str] = None
    authority: Optional[str] = None
    path: Optional[str] = None
    origin_candidate: Optional[str] = None
    referrer_candidate: Optional[str] = None

class Event(BaseModel):
    id: str
    league: Optional[str] = None
    title: str
    status: str                     # "live" | "upcoming" | "unknown"
    start_time_epoch: Optional[int] = None
    page_url: str                   # listing page
    event_url: Optional[str] = None
    iframe_src_observable: Optional[str] = None   # LIVE only (after click reveal)
    iframe_head: Optional[HeadSnapshot] = None    # HEAD of iframe src (LIVE only)
    request_observables: Optional[RequestObservables] = None  # URL-derived (LIVE only)
    evidence: Evidence

class State(BaseModel):
    last_run_utc: Optional[str] = None
    events: List[Event] = []

STATE = State(events=[])

# -----------------------------
# Helpers
# -----------------------------
def sha256_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()

def now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def classify_status(card: BeautifulSoup) -> Tuple[str, Optional[int]]:
    live_badge = card.select_one(".match-status-info .live-status-badge")
    if live_badge and "live" in (live_badge.get_text(strip=True) or "").lower():
        return "live", None
    t_badge = card.select_one(".match-status-info .today-status-badge")
    if t_badge:
        ts = t_badge.get("data-starttime")
        return "upcoming", int(ts) if ts and ts.isdigit() else None
    return "unknown", None

def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def build_event_id(league: str, title: str, event_url: str) -> str:
    base = f"{league}|{title}|{event_url}"
    return sha256_str(base)[:16]

async def head_only(url: Optional[str]) -> Optional[HeadSnapshot]:
    """Legitimate HEAD to capture status/IP/headers (no spoofing)."""
    if not url:
        return None
    meta = HeadSnapshot(url=url, status=None, server_ip=None, headers={})
    try:
        timeout = ClientTimeout(total=REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
            async with session.head(url, allow_redirects=True) as r:
                meta.status = r.status
                meta.headers = {k: v for k, v in r.headers.items()}
                ip = None
                if r.connection and r.connection.transport:
                    peer = r.connection.transport.get_extra_info("peername")
                    if peer and isinstance(peer, tuple):
                        ip = peer[0]
                meta.server_ip = ip
    except Exception as e:
        log.warning(f"[HEAD] {url} -> {type(e).__name__}: {e}")
    return meta

def pretty(obj: Any) -> Response:
    return Response(content=json.dumps(obj, indent=2, ensure_ascii=False), media_type="application/json")

def build_request_observables(iframe_url: Optional[str], referrer_url: Optional[str]) -> RequestObservables:
    if not iframe_url:
        return RequestObservables(referrer_candidate=referrer_url)
    u = urlparse(iframe_url)
    authority = u.netloc
    scheme = u.scheme
    path = u.path or ""
    origin = f"{scheme}://{authority}" if scheme and authority else None
    return RequestObservables(
        scheme=scheme or None,
        authority=authority or None,
        path=path or None,
        origin_candidate=origin,
        referrer_candidate=referrer_url
    )

# -----------------------------
# HTTP fetch for listing page
# -----------------------------
async def fetch_listing_html(url: str) -> str:
    timeout = ClientTimeout(total=REQUEST_TIMEOUT)
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}, timeout=timeout) as session:
        async with session.get(url) as r:
            log.info(f"[http] listing {url} -> {r.status}")
            return await r.text(errors="ignore")

# -----------------------------
# Selenium helpers (Windows-friendly)
# -----------------------------
CLICK_SELECTORS = [
    'button[aria-label*="play" i]',
    '.vjs-big-play-button',
    '.jw-icon-playback',
    'button.play',
    '[role="button"][aria-label*="play" i]',
    '.plyr__control--overlaid',
    '.start-button, .start, .btn-play',
    'div[class*="play"]',
]

def _selenium_reveal_iframe_src(event_url: str, user_agent: str, headless: bool) -> Optional[str]:
    """Selenium flow to click minimal 'play' controls and then read iframe src."""
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.chrome.service import Service as ChromeService

        opts = Options()
        if headless:
            opts.add_argument("--headless=new")
        opts.add_argument(f"--user-agent={user_agent}")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--window-size=1366,860")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-extensions")
        opts.add_argument("--autoplay-policy=no-user-gesture-required")

        # First try Selenium Manager (bundled with Selenium 4+)
        driver = None
        try:
            driver = webdriver.Chrome(options=opts)
        except Exception:
            # Fallback to webdriver-manager pinned to your Chrome-for-Testing
            from webdriver_manager.chrome import ChromeDriverManager
            path = ChromeDriverManager().install()
            driver = webdriver.Chrome(service=ChromeService(path), options=opts)

        driver.get(event_url)
        driver.set_page_load_timeout(30)

        # Wait for DOM readiness
        try:
            WebDriverWait(driver, 10).until(lambda d: d.execute_script("return document.readyState") in ("interactive", "complete"))
        except Exception:
            pass

        # If an iframe is already present
        try:
            els = driver.find_elements(By.CSS_SELECTOR, "iframe#iframe, iframe[src]")
            if els:
                src = els[0].get_attribute("src")
                driver.quit()
                return urljoin(event_url, src) if src else None
        except Exception:
            pass

        # Try clicking common 'play' controls
        for sel in CLICK_SELECTORS:
            try:
                elems = driver.find_elements(By.CSS_SELECTOR, sel)
                if not elems:
                    continue
                el = elems[0]
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                except Exception:
                    pass
                try:
                    el.click()
                except Exception:
                    driver.execute_script("arguments[0].click();", el)

                # wait briefly for iframe to attach
                try:
                    WebDriverWait(driver, 6).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#iframe, iframe[src]"))
                    )
                except Exception:
                    pass

                els = driver.find_elements(By.CSS_SELECTOR, "iframe#iframe, iframe[src]")
                if els:
                    src = els[0].get_attribute("src")
                    driver.quit()
                    return urljoin(event_url, src) if src else None
            except Exception:
                continue

        # Last try: send a Space key to active element
        try:
            driver.execute_script("""
                (function(){
                  const e = new KeyboardEvent('keydown', {key:' ', keyCode:32, which:32, bubbles:true});
                  (document.activeElement || document.body).dispatchEvent(e);
                })();
            """)
            WebDriverWait(driver, 4).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "iframe#iframe, iframe[src]"))
            )
            els = driver.find_elements(By.CSS_SELECTOR, "iframe#iframe, iframe[src]")
            if els:
                src = els[0].get_attribute("src")
                driver.quit()
                return urljoin(event_url, src) if src else None
        except Exception:
            pass

        driver.quit()
        return None
    except Exception as e:
        log.warning(f"[selenium] {type(e).__name__}: {e}")
        return None

async def fetch_iframe_src(event_url: str) -> Optional[str]:
    """Run Selenium in a thread so we don't block the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _selenium_reveal_iframe_src, event_url, USER_AGENT, USE_HEADLESS)

# -----------------------------
# Core scraper
# -----------------------------
async def scrape_once():
    global STATE
    new_events: List[Event] = []

    for url in START_URLS:
        try:
            html = await fetch_listing_html(url)
            soup = BeautifulSoup(html, "lxml")

            container = soup.select_one(".sports-matches-list#matchesList")
            log.info(f"[parse] matchesList present: {bool(container)} for {url}")

            listing_hash = sha256_str(html)
            cats = soup.select(".sports-category-area")
            log.info(f"[parse] categories found: {len(cats)}")

            for cat in cats:
                league_el = cat.select_one(".category-title-header h2")
                league = normalize_text(league_el.get_text()) if league_el else None

                cards = cat.select(".match-card")
                log.info(f"[parse] league={league or 'UNKNOWN'} cards={len(cards)}")

                for card in cards:
                    a = card.select_one("a.match-title-link")
                    if not a:
                        continue
                    title = normalize_text(a.get_text())
                    href = a.get("href") or ""
                    event_url = urljoin(url, href) if href else None
                    status_str, start_ts = classify_status(card)

                    iframe_src = None
                    iframe_head = None
                    req_obs = None

                    # Only attempt to reveal iframe on LIVE events
                    if status_str == "live" and event_url:
                        log.info(f"[live] revealing iframe for {event_url}")
                        iframe_src = await fetch_iframe_src(event_url)
                        if iframe_src:
                            iframe_head = await head_only(iframe_src)
                            req_obs = build_request_observables(iframe_src, event_url)

                    ev = Event(
                        id=build_event_id(league or "", title, event_url or url),
                        league=league,
                        title=title,
                        status=status_str,
                        start_time_epoch=start_ts,
                        page_url=url,
                        event_url=event_url,
                        iframe_src_observable=iframe_src,
                        iframe_head=iframe_head,
                        request_observables=req_obs,
                        evidence=Evidence(
                            page_url=url,
                            fetched_at_utc=now_utc_str(),
                            status=200,
                            server_ip=None,
                            response_headers={},
                            listing_html_sha256=listing_hash,
                            notes="Parsed listing; for LIVE events, simulated minimal user action to reveal iframe src (no spoofing)."
                        )
                    )
                    new_events.append(ev)

        except Exception as e:
            log.exception(f"[scrape] error: {e}")
            new_events.append(Event(
                id=sha256_str(f"FETCH-ERROR|{url}|{time.time()}")[:16],
                league=None,
                title=f"Fetch error for {url}: {type(e).__name__}",
                status="unknown",
                start_time_epoch=None,
                page_url=url,
                event_url=None,
                iframe_src_observable=None,
                iframe_head=None,
                request_observables=None,
                evidence=Evidence(
                    page_url=url,
                    fetched_at_utc=now_utc_str(),
                    status=0,
                    server_ip=None,
                    response_headers={},
                    listing_html_sha256="",
                    notes="Page fetch failed; see logs."
                )
            ))

    STATE = State(last_run_utc=now_utc_str(), events=new_events)

async def scheduler():
    while True:
        await scrape_once()
        await asyncio.sleep(SCRAPE_INTERVAL_SECONDS)

# -----------------------------
# FastAPI app (lifespan pattern)
# -----------------------------
app = FastAPI(title="ACE Safe Monitor", version="1.0")

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

app.router.lifespan_context = lifespan

# -----------------------------
# Endpoints
# -----------------------------
@app.get("/")
def dashboard():
    rows = []
    for e in STATE.events:
        ro = e.request_observables or RequestObservables()
        tr_class = "live" if e.status == "live" else ("upcoming" if e.status == "upcoming" else "")
        rows.append(f"""
        <tr class="{tr_class}">
            <td>{e.league or ''}</td>
            <td>{e.title or ''}</td>
            <td>{e.status or ''}</td>
            <td>{e.start_time_epoch or ''}</td>
            <td><a href="{e.page_url or '#'}" target="_blank">listing</a></td>
            <td><a href="{e.event_url or '#'}" target="_blank">{'event' if e.event_url else ''}</a></td>
            <td><a href="{e.iframe_src_observable or '#'}" target="_blank">{'iframe' if e.iframe_src_observable else ''}</a></td>
            <td>{ro.scheme or ''}</td>
            <td>{ro.authority or ''}</td>
            <td style="word-break:break-all">{ro.path or ''}</td>
            <td>{ro.origin_candidate or ''}</td>
            <td><span title="Embedding page">{ro.referrer_candidate or ''}</span></td>
            <td>{(e.iframe_head.status if e.iframe_head else '')}</td>
        </tr>
        """)
    html = f"""
    <html><head><title>ACE Safe Monitor</title>
    <style>
    body {{ font-family: sans-serif; padding: 16px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 6px; font-size: 12px; }}
    th {{ background: #f3f3f3; position: sticky; top: 0; }}
    tr.live {{ background:#e9fff1; }}
    tr.upcoming {{ background:#fffbe9; }}
    </style></head>
    <body>
      <h1>ACE Safe Monitor</h1>
      <p>Last run (UTC): {STATE.last_run_utc or '-'}</p>
      <p><a href="/report.json" target="_blank">report.json</a> · <a href="/epg.xml" target="_blank">epg.xml</a> · <a href="/healthz" target="_blank">health</a></p>
      <table>
        <thead>
          <tr>
            <th>League</th><th>Title</th><th>Status</th><th>Start</th>
            <th>Listing</th><th>Event</th><th>Iframe (LIVE)</th>
            <th>Scheme</th><th>Authority</th><th>Path</th><th>Origin*</th><th>Referrer*</th>
            <th>Iframe HEAD</th>
          </tr>
        </thead>
        <tbody>{"".join(rows) if rows else '<tr><td colspan="13">No events yet</td></tr>'}</tbody>
      </table>
      <p style="margin-top:8px;font-size:12px">* Origin/Referrer are URL-derived candidates (not captured headers).</p>
    </body></html>
    """
    return Response(content=html, media_type="text/html")

@app.get("/report.json")
def report_json():
    return pretty(STATE.model_dump())

@app.get("/epg.xml", response_class=Response)
def epg():
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="ACE Safe Monitor">']
    leagues = sorted({e.league for e in STATE.events if e.league})
    for lg in leagues:
        chan_id = re.sub(r'[^A-Za-z0-9]+', '_', lg or "UNKNOWN")
        lines.append(f'  <channel id="{chan_id}"><display-name>{lg}</display-name></channel>')

    def ts_fmt(epoch: Optional[int]) -> str:
        if not epoch:
            return ""
        dt = datetime.fromtimestamp(epoch, tz=TIMEZONE)
        return dt.strftime("%Y%m%d%H%M%S %z")

    for e in STATE.events:
        if not e.league:
            continue
        chan_id = re.sub(r'[^A-Za-z0-9]+', '_', e.league)
        start_attr = ts_fmt(e.start_time_epoch) if e.start_time_epoch else ""
        start_attr = f' start="{start_attr}"' if start_attr else ""
        title = (e.title or "Unknown").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        link = (e.event_url or e.page_url or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        iframe_txt = (e.iframe_src_observable or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        status_txt = e.status.upper()

        lines.append(f'  <programme channel="{chan_id}"{start_attr}>')
        lines.append(f'    <title>{title}</title>')
        desc_body = f"Status: {status_txt}. Link: {link}"
        if iframe_txt:
            desc_body += f" | Iframe src: {iframe_txt}"
        lines.append(f'    <desc>{desc_body}</desc>')
        lines.append(f'  </programme>')
    lines.append('</tv>')
    xml = "\n".join(lines)
    return Response(content=xml, media_type="application/xml")

@app.get("/healthz")
def healthz():
    return pretty({"ok": True, "last_run_utc": STATE.last_run_utc, "tracked": len(STATE.events)})

@app.get("/debug-scan")
async def debug_scan(url: str = Query(..., description="Listing URL to parse")):
    html = await fetch_listing_html(url)
    soup = BeautifulSoup(html, "lxml")
    items = []
    for cat in soup.select(".sports-category-area"):
        league_el = cat.select_one(".category-title-header h2")
        league = normalize_text(league_el.get_text()) if league_el else None
        for card in cat.select(".match-card"):
            a = card.select_one("a.match-title-link")
            if not a:
                continue
            title = normalize_text(a.get_text())
            href = a.get("href") or ""
            event_url = urljoin(url, href) if href else None
            status_str, start_ts = classify_status(card)

            iframe_src = None
            iframe_head = None
            req_obs = None
            if status_str == "live" and event_url:
                iframe_src = await fetch_iframe_src(event_url)
                if iframe_src:
                    iframe_head = await head_only(iframe_src)
                    req_obs = build_request_observables(iframe_src, event_url)

            items.append({
                "league": league,
                "title": title,
                "status": status_str,
                "start_time_epoch": start_ts,
                "event_url": event_url,
                "iframe_src_observable": iframe_src,
                "request_observables": (req_obs.model_dump() if req_obs else None),
                "iframe_head": (iframe_head.model_dump() if iframe_head else None),
            })
    return pretty({"found": len(items), "items": items})

# -----------------------------
# Offline M3U8 parser (no network)
# -----------------------------
class M3U8ParseResult(BaseModel):
    target_duration: Optional[float] = None
    media_sequence: Optional[int] = None
    discontinuity_sequence: Optional[int] = None
    key_uris: List[str] = []
    segment_urls: List[str] = []
    segment_hosts: List[str] = []

def parse_m3u8_text(text: str) -> M3U8ParseResult:
    key_uris, seg_urls = [], []
    target_duration = None
    media_seq = None
    disc_seq = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try: target_duration = float(line.split(":", 1)[1])
            except Exception: pass
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try: media_seq = int(line.split(":", 1)[1])
            except Exception: pass
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            try: disc_seq = int(line.split(":", 1)[1])
            except Exception: pass
        elif line.startswith("#EXT-X-KEY:"):
            m = re.search(r'URI="([^"]+)"', line)
            if m: key_uris.append(m.group(1))
        elif not line.startswith("#"):
            seg_urls.append(line)
    hosts = []
    for u in seg_urls + key_uris:
        try: hosts.append(urlparse(u).netloc)
        except Exception: pass
    return M3U8ParseResult(
        target_duration=target_duration,
        media_sequence=media_seq,
        discontinuity_sequence=disc_seq,
        key_uris=key_uris,
        segment_urls=seg_urls,
        segment_hosts=sorted({h for h in hosts if h})
    )

@app.post("/parse-m3u8")
def parse_m3u8(m3u8_text: str = Body(..., embed=True, description="Paste the full M3U8 text here")):
    res = parse_m3u8_text(m3u8_text)
    return pretty(res.model_dump())
