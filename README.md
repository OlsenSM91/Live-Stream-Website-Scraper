# ACE Safe Monitor

> A focused, *evidence-first* scraper for ACE-style monitoring. It catalogs listings (live & upcoming) from permitted pages, and—**for LIVE items only**—performs a minimal user interaction to reveal a visible `<iframe src>` (no spoofing, no media fetching). It then records request observables for chain-of-custody style reporting.

Think of it as a field notebook, not a wiretap. 📓⚖️

---

## What it does (and what it deliberately does **not** do)

### ✅ Does

* Scrapes configured listing pages you’re allowed to crawl (e.g., “Live Games” pages).
* Classifies entries as **live** or **upcoming** using badge selectors.
* For **LIVE** events:

  * Opens the event page with **Selenium** (headless by default).
  * Simulates minimal user actions (click typical “play” controls) to let the site reveal a visible `<iframe ... src="...">`.
  * Captures:

    * **iframe src** (if present)
    * **URL-derived** request observables (scheme, authority, path, origin/referrer *candidates*)
    * A **legitimate `HEAD`** snapshot of the iframe URL (status, headers, observed server IP)
* Emits:

  * A compact dashboard at `/`
  * **Pretty** JSON at `/report.json`
  * XMLTV-ish **EPG** at `/epg.xml`
  * A `/debug-scan?url=...` endpoint to quickly test a single listing page
  * An offline `/parse-m3u8` endpoint you can POST a raw manifest to (no network requests inside)

### 🚫 Does **not**

* Emulate/forge `Referer`, `Origin`, or session tokens.
* Fetch media segments, keys, or any protected content.
* Circumvent paywalls, captchas, or access controls.

**Bottom line:** it collects *visible, user-level page state* and derives safe metadata for reporting. Perfect for a SOC/IR evidence pipeline; useless for freeloading pirates. That’s exactly the point.

---

## High-level flow

1. **Listing crawl** (aiohttp): fetch permitted `START_URLS`, parse the DOM:

   * `.sports-category-area` → league name from `.category-title-header h2`
   * `.match-card a.match-title-link` → title + event URL
   * Badges:

     * `.live-status-badge` → `live`
     * `.today-status-badge[data-starttime]` → `upcoming` (epoch from `data-starttime`)
2. **LIVE event enrichment** (Selenium, headless):

   * Open `event_url`, try common “play” selectors, wait briefly for an `iframe#iframe` or any `iframe[src]`.
   * If found: record `iframe src`, build URL-derived observables, and do a **HEAD** to capture response headers/status.
3. **Output & schedule**:

   * The app schedules itself every `SCRAPE_INTERVAL_SECONDS` (default **90s**).
   * Results are served at the endpoints below.

---

## Quick start

### Requirements

* **Python 3.9+** (3.11 recommended)
* **Google Chrome** or **Chromium** (Selenium will use Selenium Manager or download a matching driver via `webdriver-manager`)
* Windows, macOS, or Linux

### Install

```bash
pip install fastapi uvicorn aiohttp beautifulsoup4 lxml python-dateutil selenium webdriver-manager
```

### Configure

Open `toast.py` and set **only URLs you’re allowed to crawl**:

```python
START_URLS = [
    "https://v1.gostreameast.link/#streams"
]
```

Other useful knobs:

```python
USE_HEADLESS = True                 # headless Selenium (set False for debugging)
SCRAPE_INTERVAL_SECONDS = 90        # re-scan cadence
USER_AGENT = "Mozilla/5.0 ..."      # UA string for both HTTP and Selenium
```

### Run

```bash
uvicorn toast:app --host 127.0.0.1 --port 8000 --reload
```

Open: [http://127.0.0.1:8000/](http://127.0.0.1:8000/)

---

## Endpoints

* `GET /` — Minimal dashboard (table):

  * League, Title, Status, Start time (epoch if provided)
  * Links: listing page, event page, iframe (when LIVE + detected)
  * Observables: scheme, authority, path, origin\*, referrer\* candidates
  * `HEAD` status for iframe URL
    *\* URL-derived, not captured headers.*

* `GET /report.json` — Pretty JSON containing all events and evidence.

* `GET /epg.xml` — Barebones XMLTV-ish output:

  * One synthetic channel per league
  * `programme` entries with status & evidence links
  * Includes `iframe src` in description for LIVE items

* `GET /healthz` — Simple health output (`ok`, last run, count).

* `GET /debug-scan?url=<listing>` — One-off parser for a single listing URL.

  * Behaves like the main scraper: **only** reveals iframe for LIVE items.

* `POST /parse-m3u8` — Offline manifest parser (no network).
  Example:

  ```bash
  curl -X POST http://127.0.0.1:8000/parse-m3u8 \
       -H "Content-Type: application/json" \
       -d '{"m3u8_text":"#EXTM3U\n#EXT-X-VERSION:4\n..."}'
  ```

  Returns keys, segment URLs, and distinct segment hosts for reporting.

---

## Data model (selected fields)

### `Event`

```json
{
  "id": "sha256-short",
  "league": "NFL",
  "title": "Team A - Team B",
  "status": "live | upcoming | unknown",
  "start_time_epoch": 1757264400,
  "page_url": "https://listing/page",
  "event_url": "https://event/detail",
  "iframe_src_observable": "https://player.example/…",        // LIVE only, if found
  "request_observables": {
    "scheme": "https",
    "authority": "player.example",
    "path": "/embed/…/playlist.m3u8",
    "origin_candidate": "https://player.example",
    "referrer_candidate": "https://event/detail"
  },
  "iframe_head": {
    "url": "https://player.example/…",
    "status": 200,
    "server_ip": "203.0.113.7",
    "headers": { "content-type": "application/vnd.apple.mpegurl", "..." : "..." }
  },
  "evidence": {
    "page_url": "https://listing/page",
    "fetched_at_utc": "2025-09-07T06:06:25Z",
    "status": 200,
    "listing_html_sha256": "…",
    "notes": "Parsed listing; for LIVE events, simulated minimal user action to reveal iframe src (no spoofing)."
  }
}
```

---

## How it classifies & reveals

* **Classification**
  Looks for:

  * `.match-status-info .live-status-badge` → **live**
  * `.match-status-info .today-status-badge[data-starttime]` → **upcoming**

* **Reveal (LIVE only)**
  Tries common “play” elements:

  ```python
  CLICK_SELECTORS = [
    'button[aria-label*="play" i]',
    '.vjs-big-play-button', '.jw-icon-playback', 'button.play',
    '[role="button"][aria-label*="play" i]',
    '.plyr__control--overlaid',
    '.start-button, .start, .btn-play',
    'div[class*="play"]',
  ]
  ```

  Then looks for `iframe#iframe` or any `iframe[src]`. If found, records it.

> If a specific site uses custom controls, add a selector here (or ping your future self to add a per-domain override map).

---

## Usage tips

* **Debug why a LIVE iframe isn’t showing up**

  1. Set `USE_HEADLESS = False`, rerun, and watch the browser.
  2. Inspect the event page’s DOM; add a site-specific selector to `CLICK_SELECTORS`.
  3. Some sites guard the player with consent/captcha; we intentionally don’t bypass these.

* **Pretty JSON**
  `/report.json` is indented for readability—perfect for piping into a dashboard.

* **Offline EPG & Manifest analysis**

  * `/epg.xml` is intentionally minimal (evidence, not entertainment).
  * Use `/parse-m3u8` to extract **segment hosts** from a pasted manifest for DMCA notices.

---

## Troubleshooting

* **Chrome/driver mismatch**
  We rely on Selenium Manager first; if that fails, `webdriver-manager` will download a matching ChromeDriver automatically. Corporate proxies can block this. If so, preinstall a driver and point Selenium to it.

* **Firewall/AV blocks**
  Headless browsers sometimes spook AV. If launches fail, try non-headless or whitelist the binary.

* **Selectors changed**
  If the listing page changes its CSS classes, update:

  * `.sports-category-area`, `.category-title-header h2`
  * `.match-card a.match-title-link`
  * Status badges (`.live-status-badge`, `.today-status-badge`)

* **Performance**
  Selenium opens a browser only for **LIVE** cards. If a listing contains hundreds of lives (rare), consider rate limits or add per-league caps.

---

## Ethics & legal

* **Use only on pages you are authorized to crawl.**
* This tool captures *what a regular user would see* and records URL-derived metadata. It **does not** fetch protected media or forge headers.
* Your organization is responsible for compliance with applicable laws, ToS, and ACE/MPA reporting workflows.
* If your goal is anything except **copyright enforcement** or **security research** with permission—don’t use this. Wrong tool for the wrong job.

> “Because you *can* doesn’t mean you *should*.” This repo chooses **should**.

---

## Appendix: Example curl

Pretty JSON:

```bash
curl http://127.0.0.1:8000/report.json
```

Debug a single listing page:

```bash
curl "http://127.0.0.1:8000/debug-scan?url=https://v1.gostreameast.link/#streams"
```

EPG preview:

```bash
curl http://127.0.0.1:8000/epg.xml
```

Parse a pasted M3U8 (offline):

```bash
curl -X POST http://127.0.0.1:8000/parse-m3u8 \
  -H "Content-Type: application/json" \
  -d @manifest.json
# where manifest.json contains:
# { "m3u8_text": "#EXTM3U\n#EXT-X-VERSION:4\n..." }
```

## Adding ability to resolve playlist.m3u8 HLS streaming files Once a live URL is detected the reports.json includes the following:
The request observables will pull in something like:
```json
       "request_observables": {
        "scheme": "https",
        "authority": "embedsports.top",
        "path": "/embed-seast/alpha/uc-davis-vs-washington/2",
        "origin_candidate": "https://embedsports.top",
        "referrer_candidate": "https://streameast.ps/cfb/uc-davis-aggies-washington-huskies/"
```

Example CURL:
curl -H "Referer: https://embedsports.top/" -H "Origin: https://embedsports.top/" -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36" https://zd.strmd.top/secure/pMgSaYLMuKeJfpiVtFSUbdMIjDTXfEwV/alpha/stream/san-diego-state-vs-washington-state/2/playlist.m3u8
