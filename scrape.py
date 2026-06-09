#!/usr/bin/env python3
"""
scrape.py — fetch competitor prices for the Price Reconciliation Dashboard.

Reads  urls.json   (category -> product -> competitor -> {url, note})
Writes prices.json  (category -> product -> competitor -> price)

WHY PLAYWRIGHT: almost every marketplace here (G2G, G2A, Kinguin, Eneba,
Codashop, SEAGM, MooGold, itemku ...) renders prices with JavaScript and/or
sits behind bot protection, so plain HTTP requests return empty shells. We
drive a real headless Chromium instead.

PER-SITE ADAPTERS: each marketplace lays its price out differently, so every
domain gets its own small adapter function that knows where to read the price.
`generic_extract` is the last-resort fallback. The two concrete adapters below
(MooGold, SEAGM) are STARTING POINTS — selectors WILL need to be verified
against the live pages. Treat them as a template for the rest.

LOCAL RUN:
    pip install playwright
    python -m playwright install chromium
    python scrape.py                                   # all categories
    python scrape.py --categories "Spotify" --headful  # watch one category
    python scrape.py --categories "Spotify" --limit 15 --debug   # diagnose

--debug writes a ./debug/ folder with a screenshot per URL and a findings.json
listing, for each page, its title, whether it looks blocked, and every
price-looking number found. Use it to write/verify adapter selectors. Share
debug/findings.json + a couple of screenshots and exact selectors can be added.

Anti-bot reality: expect ~5-7 of the 10 sites to work reliably. Some need
stealth tweaks, slower pacing, or proxies; a few may resist entirely. Be
respectful: low concurrency, modest cadence (the workflow runs every 6h).
"""

import argparse
import asyncio
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
URLS_FILE = ROOT / "urls.json"
OUT_FILE = ROOT / "prices.json"
REPORT_FILE = ROOT / "scrape_report.json"
DEBUG_DIR = ROOT / "debug"

# --- tuning knobs -----------------------------------------------------------
CONCURRENCY = 4            # parallel pages; keep low to stay polite / unblocked
NAV_TIMEOUT_MS = 25_000    # per-page navigation timeout
RETRIES = 1                # extra attempts on failure
PER_REQUEST_DELAY = 0.4    # seconds of jitter between starts (politeness)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BLOCK_HINTS = ("just a moment", "captcha", "access denied", "cloudflare",
               "are you a human", "verify you are", "unusual traffic",
               "请稍候", "enable javascript")

debug_records = []  # populated when --debug

# --- price text parsing -----------------------------------------------------
# Matches "$12.34", "US$ 12.34", "12.34 USD", "RM 50", "€10,00", "S$ 9.90" etc.
_PRICE_RE = re.compile(
    r"(?:US\$|USD|RM|S\$|SGD|EUR|€|£|\$)\s*([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{1,2})?)"
    r"|([0-9]{1,3}(?:[.,][0-9]{3})*(?:[.,][0-9]{1,2})?)\s*(?:USD|EUR|SGD|MYR|RM)",
    re.IGNORECASE,
)


def _to_float(raw: str):
    """Normalise a price token to float, handling 1,234.56 and 1.234,56."""
    s = raw.strip()
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if re.search(r",\d{2}$", s):
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    try:
        v = float(s)
        return v if 0 < v < 100_000 else None
    except ValueError:
        return None


def prices_in_text(text: str):
    out = []
    for m in _PRICE_RE.finditer(text or ""):
        tok = m.group(1) or m.group(2)
        v = _to_float(tok)
        if v is not None:
            out.append(v)
    return out


async def generic_extract(page, url, note):
    """Last-resort: scan visible text and return the lowest plausible price.
    A guess (good for listing pages) — prefer a site-specific adapter."""
    try:
        body = await page.inner_text("body")
    except Exception:
        return None
    vals = prices_in_text(body)
    return min(vals) if vals else None


# --- site-specific adapters (TEMPLATES — verify selectors live) -------------
async def adapter_moogold(page, url, note):
    for sel in [".woocommerce-Price-amount bdi", ".woocommerce-Price-amount",
                "[data-price]", ".price ins .amount", ".price .amount"]:
        try:
            el = await page.query_selector(sel)
            if el:
                vals = prices_in_text(await el.inner_text())
                if vals:
                    return min(vals)
                dp = await el.get_attribute("data-price")
                if dp and _to_float(dp):
                    return _to_float(dp)
        except Exception:
            pass
    return await generic_extract(page, url, note)


async def adapter_seagm(page, url, note):
    for sel in [".denomination .price", ".product-price", ".price-now",
                "[class*=price]"]:
        try:
            el = await page.query_selector(sel)
            if el:
                vals = prices_in_text(await el.inner_text())
                if vals:
                    return min(vals)
        except Exception:
            pass
    return await generic_extract(page, url, note)


# domain -> adapter. Unlisted domains use generic_extract.
ADAPTERS = {
    "moogold.com": adapter_moogold,
    "seagm.com": adapter_seagm,
    # TODO add/verify with debug output: g2g.com, g2a.com, kinguin.net,
    # eneba.com, codashop.com, joytify.com (LapakGaming), itemku.com,
    # unipin.com, offgamers.com
}


def adapter_for(url):
    host = urlparse(url).netloc.replace("www.", "").lower()
    for dom, fn in ADAPTERS.items():
        if host.endswith(dom):
            return fn
    return generic_extract


def _safe(s, n=60):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s))[:n]


async def collect_debug(page, task):
    """Save a screenshot + record what the page actually contains."""
    rec = {"comp": task["comp"], "url": task["url"], "category": task["sheet"],
           "product": task["pname"], "title": None, "blocked": False,
           "candidates": [], "adapter": adapter_for(task["url"]).__name__}
    try:
        rec["title"] = await page.title()
    except Exception:
        pass
    try:
        body = await page.inner_text("body")
        low = (body or "").lower()
        rec["blocked"] = any(h in low for h in BLOCK_HINTS)
        rec["candidates"] = sorted(set(prices_in_text(body)))[:15]
    except Exception:
        pass
    shot = DEBUG_DIR / f"{_safe(task['sheet'])}__{_safe(task['pname'],40)}__{_safe(task['comp'])}.png"
    try:
        await page.screenshot(path=str(shot), full_page=False)
        rec["screenshot"] = shot.name
    except Exception:
        pass
    debug_records.append(rec)


# --- fetching ---------------------------------------------------------------
async def fetch_one(context, task, results, report):
    url, comp = task["url"], task["comp"]
    fn = adapter_for(url)
    for attempt in range(RETRIES + 1):
        page = await context.new_page()
        try:
            await page.route(
                "**/*",
                lambda r: r.abort()
                if r.request.resource_type in ("image", "media", "font")
                else r.continue_(),
            )
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            await page.wait_for_timeout(1500)  # let JS prices render
            if ARGS.debug:
                await collect_debug(page, task)
            price = await fn(page, url, task.get("note"))
            await page.close()
            if price is not None:
                results.setdefault(task["sheet"], {}).setdefault(task["pname"], {})[comp] = round(price, 2)
                report["ok"] += 1
                return
        except Exception as e:
            report["errors"].append(f'{comp} {url[:60]} :: {type(e).__name__}')
            try:
                await page.close()
            except Exception:
                pass
        await asyncio.sleep(0.8 * (attempt + 1))
    report["failed"] += 1


async def run(tasks):
    results, report = {}, {"ok": 0, "failed": 0, "errors": []}
    sem = asyncio.Semaphore(1 if ARGS.debug else CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not ARGS.headful)
        context = await browser.new_context(user_agent=USER_AGENT, locale="en-US")

        async def worker(t):
            async with sem:
                await asyncio.sleep(PER_REQUEST_DELAY)
                await fetch_one(context, t, results, report)

        done = 0
        for fut in asyncio.as_completed([worker(t) for t in tasks]):
            await fut
            done += 1
            if done % 25 == 0 or done == len(tasks):
                print(f"  {done}/{len(tasks)} fetched "
                      f"(ok={report['ok']} fail={report['failed']})", flush=True)
        await browser.close()
    return results, report


def build_tasks(data, only_categories, limit):
    tasks = []
    for cat, block in data["categories"].items():
        if only_categories and cat not in only_categories:
            continue
        for product in block["products"]:
            for comp, info in product["urls"].items():
                tasks.append({"sheet": cat, "pname": product["name"],
                              "comp": comp, "url": info["url"], "note": info.get("note")})
    return tasks[:limit] if limit else tasks


def main():
    if not URLS_FILE.exists():
        sys.exit(f"Missing {URLS_FILE} — generate it from the products spreadsheet first.")
    data = json.loads(URLS_FILE.read_text(encoding="utf-8"))

    only = set(c.strip() for c in ARGS.categories.split(",")) if ARGS.categories else None
    tasks = build_tasks(data, only, ARGS.limit)
    if ARGS.debug:
        DEBUG_DIR.mkdir(exist_ok=True)
    print(f"Scraping {len(tasks)} URLs across "
          f"{len(only) if only else len(data['categories'])} categories"
          f"{' [DEBUG]' if ARGS.debug else ''}…", flush=True)

    t0 = time.time()
    results, report = asyncio.run(run(tasks))

    payload = {"generated": datetime.now(timezone.utc).isoformat(),
               "source": "scrape.py", "prices": results}
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    report["elapsed_sec"] = round(time.time() - t0, 1)
    report["errors"] = report["errors"][:50]
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if ARGS.debug:
        (DEBUG_DIR / "findings.json").write_text(
            json.dumps(debug_records, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n--- DEBUG SUMMARY (per URL) ---")
        for r in debug_records:
            flag = "BLOCKED" if r["blocked"] else ("HIT" if r["candidates"] else "empty")
            print(f"  [{flag:7}] {r['comp']:12} cands={r['candidates'][:5]}  {r['url'][:55]}")
        print(f"\nScreenshots + findings.json in: {DEBUG_DIR}")

    print(f"\nDone in {report['elapsed_sec']}s — ok={report['ok']} "
          f"failed={report['failed']}. Wrote {OUT_FILE.name}.", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="", help="comma-separated subset, e.g. 'Steam,Spotify'")
    ap.add_argument("--limit", type=int, default=0, help="cap total fetches (smoke test)")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--debug", action="store_true", help="save screenshots + findings.json per URL")
    ARGS = ap.parse_args()
    main()
