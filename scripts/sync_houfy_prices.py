"""
Houfy Price Sync — Playwright browser automation.

Pulls the nightly rate calendar from PriceLabs and syncs prices to Houfy
by simulating clicks on the pricing calendar UI.

Free Houfy accounts support iCal calendar sync but no price-sync API,
so this script automates the manual UI instead.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Setup (first time):
    pip install playwright httpx python-dotenv
    playwright install chromium

Add to .env:
    HOUFY_EMAIL=your@email.com
    HOUFY_PASSWORD=yourpassword

    # One entry per property (Houfy listing ID from your dashboard URL)
    HOUFY_LISTING_ID_NIDRY_HILLS=123456
    HOUFY_LISTING_ID_BOAT_VILLA=789012
    HOUFY_LISTING_ID_ADAMAN_NICOLETA=345678
    HOUFY_LISTING_ID_ADAMAN_MARIA=901234

    # PriceLabs listing IDs (from app.pricelabs.co → Listings)
    PRICELABS_PROPERTY_ID_NIDRY_HILLS=pl-nidry-id
    PRICELABS_PROPERTY_ID_BOAT_VILLA=pl-boat-id
    PRICELABS_PROPERTY_ID_ADAMAN_NICOLETA=pl-adaman-n-id
    PRICELABS_PROPERTY_ID_ADAMAN_MARIA=pl-adaman-m-id
    # Falls back to PRICELABS_PROPERTY_ID if per-property keys are not set.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Usage:
    # Headless, 60 days, real sync
    python scripts/sync_houfy_prices.py

    # Preview only — no browser opened
    python scripts/sync_houfy_prices.py --dry-run

    # Show browser window (useful first time to watch the automation)
    python scripts/sync_houfy_prices.py --visible

    # Extend forecast window
    python scripts/sync_houfy_prices.py --days 90

    # Pause browser after login to inspect selectors in DevTools
    python scripts/sync_houfy_prices.py --visible --inspect

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Houfy selector notes:
    If Houfy updates their frontend, update the _SEL_* constants below.
    Run with --visible --inspect to pause after login and use DevTools
    to find the correct selectors for date cells and price inputs.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from playwright.async_api import async_playwright, Page
except ImportError:
    print(
        "ERROR: playwright not installed.\n"
        "Run: pip install playwright && playwright install chromium"
    )
    sys.exit(1)


# ── Property mappings ─────────────────────────────────────────────────────────
# Each entry maps one property across PriceLabs, HostHub, and Houfy.
# hosthub_rate_plan_id — HostHub default rate plan ID (GET /rate-plans/{id}/rates).
#   HostHub receives dynamic prices from PriceLabs via the SyncBnb integration,
#   making this the primary working rate source without needing PriceLabs IAPI creds.
# pricelabs_id         — PriceLabs listing ID (used when IAPI credentials are set).
# houfy_listing_id     — numeric Houfy listing ID from the listing management URL.
# Leave houfy_listing_id AND both rate sources empty to skip a property.

PROPERTY_MAPPINGS: list[dict] = [
    {
        "name":                 "Nidry Hills",
        "pricelabs_id":         os.getenv("PRICELABS_PROPERTY_ID_NIDRY_HILLS",     ""),
        "hosthub_rate_plan_id": os.getenv("HOSTHUB_RATE_PLAN_NIDRI",               ""),
        "houfy_listing_id":     os.getenv("HOUFY_LISTING_ID_NIDRY_HILLS",          ""),
    },
    {
        "name":                 "Boat Villa",
        "pricelabs_id":         os.getenv("PRICELABS_PROPERTY_ID_BOAT_VILLA",      ""),
        "hosthub_rate_plan_id": os.getenv("HOSTHUB_RATE_PLAN_BOAT",                ""),
        "houfy_listing_id":     os.getenv("HOUFY_LISTING_ID_BOAT_VILLA",           ""),
    },
    {
        "name":                 "Adaman Nicoleta",
        "pricelabs_id":         os.getenv("PRICELABS_PROPERTY_ID_ADAMAN_NICOLETA", ""),
        "hosthub_rate_plan_id": os.getenv("HOSTHUB_RATE_PLAN_ADAMAN_NICOLETA",     ""),
        "houfy_listing_id":     os.getenv("HOUFY_LISTING_ID_ADAMAN_NICOLETA",      ""),
    },
    {
        "name":                 "Adaman Maria",
        "pricelabs_id":         os.getenv("PRICELABS_PROPERTY_ID_ADAMAN_MARIA",    ""),
        "hosthub_rate_plan_id": os.getenv("HOSTHUB_RATE_PLAN_ADAMAN_MARIA",        ""),
        "houfy_listing_id":     os.getenv("HOUFY_LISTING_ID_ADAMAN_MARIA",         ""),
    },
]


# ── PriceLabs ─────────────────────────────────────────────────────────────────
# Public API key — used for listing metadata only.
# Daily price data requires PriceLabs IAPI partner credentials:
#   PRICELABS_INTEGRATION_TOKEN, PRICELABS_INTEGRATION_NAME, PRICELABS_USER_TOKEN
# Contact support@pricelabs.co to obtain them.

_PL_API_KEY  = os.getenv("PRICELABS_API_KEY", "")

# ── HostHub (SyncBnb) — primary rate source ───────────────────────────────────
# HostHub receives PriceLabs dynamic prices via the SyncBnb PMS integration.
# This gives us daily rates without requiring PriceLabs IAPI partner credentials.

_HH_API_KEY  = os.getenv("HOSTHUB_API_KEY", "")
_HH_API_BASE = os.getenv("HOSTHUB_API_BASE", "https://app.hosthub.com/api/2019-03-01")


def generate_flat_rates(price: float, days: int = 60) -> list[dict]:
    """
    Return a list of {"date": "YYYY-MM-DD", "price": float} for the next N days,
    all priced at the given flat rate. Used when PriceLabs IDs are not yet configured.
    """
    today = date.today()
    return [
        {"date": (today + timedelta(days=i)).isoformat(), "price": float(price)}
        for i in range(days)
    ]


async def fetch_pricelabs_rates(pricelabs_id: str, days: int = 60) -> list[dict]:
    """
    Fetch nightly rates from PriceLabs IAPI v2 for the next N days.

    Requires PriceLabs IAPI partner credentials — contact support@pricelabs.co.
    Set in .env: PRICELABS_INTEGRATION_TOKEN, PRICELABS_INTEGRATION_NAME,
    PRICELABS_USER_TOKEN (owner's PriceLabs email).

    Uses GET /v2/integration/api/calendar (read calendar pushed by PMS)
    or POST /v2/integration/api/get_prices (dynamic prices, requires sync enabled).

    Returns a list of {"date": "YYYY-MM-DD", "price": float} dicts sorted by date.
    """
    integration_token = os.getenv("PRICELABS_INTEGRATION_TOKEN", "")
    integration_name  = os.getenv("PRICELABS_INTEGRATION_NAME",  "")
    user_token        = os.getenv("PRICELABS_USER_TOKEN",         "")

    if not integration_token or not integration_name or not user_token:
        raise RuntimeError(
            "PriceLabs IAPI credentials not configured. "
            "Set PRICELABS_INTEGRATION_TOKEN, PRICELABS_INTEGRATION_NAME, "
            "and PRICELABS_USER_TOKEN in .env. "
            "Contact support@pricelabs.co to obtain partner credentials."
        )
    if not pricelabs_id:
        raise RuntimeError("pricelabs_id is empty — check PROPERTY_MAPPINGS or .env")

    today     = date.today()
    date_from = today.isoformat()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            "https://api.pricelabs.co/v2/integration/api/calendar",
            headers={
                "X-INTEGRATION-TOKEN": integration_token,
                "X-INTEGRATION-NAME":  integration_name,
            },
            params={
                "user_token": user_token,
                "listing_id": pricelabs_id,
                "start_date": date_from,
            },
        )
        r.raise_for_status()
        data = r.json()

    raw = data.get("data") or []
    rates: list[dict] = []
    for item in raw:
        d = item.get("date") or item.get("stay_date")
        p = item.get("price") or item.get("rate") or item.get("nightly_price")
        if d and p:
            rates.append({"date": str(d), "price": float(p)})

    rates.sort(key=lambda x: x["date"])
    return rates[:days]


async def fetch_hosthub_rates(rate_plan_id: str, days: int = 60) -> list[dict]:
    """
    Fetch nightly rates from HostHub (SyncBnb) for the next N days.

    HostHub receives dynamic prices from PriceLabs via the SyncBnb integration,
    making this the primary working rate source without PriceLabs IAPI credentials.
    Endpoint: GET /rate-plans/{rate_plan_id}/rates
    Price is returned in cents — divided by 100 to get EUR.

    Returns a list of {"date": "YYYY-MM-DD", "price": float} dicts sorted by date.
    """
    if not _HH_API_KEY:
        raise RuntimeError("HOSTHUB_API_KEY not set in environment")
    if not rate_plan_id:
        raise RuntimeError("rate_plan_id is empty — check PROPERTY_MAPPINGS or .env")

    today     = date.today()
    date_from = today.isoformat()
    date_to   = (today + timedelta(days=days)).isoformat()

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{_HH_API_BASE}/rate-plans/{rate_plan_id}/rates",
            headers={"Authorization": _HH_API_KEY, "Accept": "application/json"},
            params={"start_date": date_from, "end_date": date_to},
        )
        r.raise_for_status()
        data = r.json()

    rates: list[dict] = []
    for item in data.get("data", []):
        d     = item.get("date")
        cents = (item.get("amount") or {}).get("cents")
        if d and cents is not None:
            rates.append({"date": str(d), "price": float(cents) / 100})

    rates.sort(key=lambda x: x["date"])
    return rates[:days]


async def fetch_hosthub_blocked_dates(
    rental_id: str,
    days: int = 60,
) -> set[str]:
    """
    Return the set of YYYY-MM-DD strings that are booked or blocked
    in HostHub for the given rental over the next `days` days.

    Uses GET /rentals/{rental_id}/calendar-events (same endpoint as
    import_hosthub.py).  Only Booking and Block event types are treated
    as unavailable; availability events are ignored.

    Pre-building this set lets sync_property skip entire booking spans
    without touching the browser, cutting runtime proportionally to the
    share of booked nights in the window.
    """
    if not _HH_API_KEY or not rental_id:
        return set()

    today     = date.today()
    date_from = today.isoformat()
    date_to   = (today + timedelta(days=days)).isoformat()

    events: list[dict] = []
    url: str | None = (
        f"{_HH_API_BASE}/rentals/{rental_id}/calendar-events"
    )
    params: dict = {
        "is_visible": "true",
        # Look back 90 days so any in-progress booking that checked in before today
        # is still returned by the API (its date_from is before today).
        # Without this, HostHub would omit bookings already underway, causing the
        # sync loop to hit each day individually in the browser instead of skipping.
        "start_date": (date.today() - timedelta(days=90)).isoformat(),
        "end_date":   date_to,
    }
    _max_pages = 20  # safety cap — at ~50 events/page that's 1 000 events max

    async with httpx.AsyncClient(timeout=15) as client:
        for _ in range(_max_pages):
            if not url:
                break
            r = await client.get(
                url, headers={"Authorization": _HH_API_KEY, "Accept": "application/json"},
                params=params, timeout=15,
            )
            r.raise_for_status()
            d = r.json()
            page_events = d.get("data", [])
            events.extend(page_events)
            nxt = d.get("navigation", {}).get("next")
            url = (
                nxt if nxt and nxt.startswith("http")
                else (f"https://app.hosthub.com{nxt}" if nxt else None)
            )
            params = {}  # only used for the first request
            # Early-exit: if every event on this page starts after our window, stop.
            if page_events and all(
                (ev.get("date_from") or "") > date_to for ev in page_events
            ):
                break

    blocked: set[str] = set()
    for ev in events:
        ev_type = (ev.get("type") or "").lower()
        if ev_type not in ("booking", "block", "owner block", "maintenance", "hold"):
            continue
        if ev.get("cancelled_at"):
            continue
        start = ev.get("date_from")
        end   = ev.get("date_to")
        if not start or not end:
            continue
        # Expand the range: date_from is check-in, date_to is check-out.
        # The check-out day itself is available for a new arrival, so exclude it.
        cur = date.fromisoformat(start)
        checkout = date.fromisoformat(end)
        while cur < checkout:
            s = cur.isoformat()
            if date_from <= s <= date_to:
                blocked.add(s)
            cur += timedelta(days=1)

    return blocked


# ── Houfy selectors ───────────────────────────────────────────────────────────
# UPDATE THESE if Houfy changes their frontend.
# Use --visible --inspect to pause after login and verify in DevTools.

_HOUFY_BASE  = "https://www.houfy.com"
_HOUFY_LOGIN = f"{_HOUFY_BASE}/signin?next=home"

# Calendar management URL — replace {listing_id} at runtime.
_HOUFY_CAL = f"{_HOUFY_BASE}/edit-listing/{{listing_id}}/reservations/calendar"

# Login form
_SEL_EMAIL    = 'input[type="email"], input[name="email"]'
_SEL_PASSWORD = 'input[type="password"], input[name="password"]'
_SEL_SUBMIT   = 'button:has-text("Sign In"), button[type="submit"]'

# CSS classes that indicate a blocked / unavailable date.
# Available dates have class: CalendarGrid_calGridCell__AbKu0 only.
# Blocked dates are expected to carry one of these additional class fragments.
# Fallback: if no class matches, a missing editor after click also means blocked.
_BLOCKED_CLASSES = ("blocked", "unavailable", "disabled", "closed", "reserved", "booked")

# Date cell — confirmed from live DOM.
# Each day renders as: <div data-date="YYYY-MM-DD" class="CalendarGrid_calGridCell__AbKu0 ...">
_SEL_DATE_CELL = '[data-date="{date}"]'

# Month select dropdown — confirmed from live DOM.
# Value format: MMYYYY zero-padded, e.g. "042026" for April 2026.
# Using this is far more reliable than clicking prev/next arrow buttons.
_SEL_MONTH_SELECT = 'select.bg-transparent'

# Editor panel header — the sidebar that appears after clicking a date.
# Used to verify the panel is open before interacting with inputs.
_SEL_EDITOR_HEADER = 'h2:has-text("Selected Date")'

# Close (X) button of the editor panel.
# Used to dismiss the panel after saving, before clicking the next date.
_SEL_EDITOR_CLOSE = 'h2:has-text("Selected Date") ~ button, button:has(.lucide-x)'

# Price-per-night input — confirmed from live DOM.
# The editor has 4 type=number inputs; price-per-night is identified by:
#   - placeholder="0"
#   - NO min attribute  (min-stay has min="1", discount has min="0")
# This uniquely identifies the price field without relying on labels.
_SEL_PRICE_INPUT = 'input[type="number"][placeholder="0"]:not([min])'

# "Save Pricing" button — confirmed from live DOM: exact text match.
_SEL_SAVE_BTN = 'button:has-text("Save Pricing")'
async def houfy_login(page: Page, email: str, password: str) -> None:
    """Log in to Houfy. Raises on failure."""
    await page.goto(_HOUFY_LOGIN, wait_until="networkidle")
    await page.fill(_SEL_EMAIL, email)
    await page.fill(_SEL_PASSWORD, password)
    await page.click(_SEL_SUBMIT)
    # Wait until redirected away from the sign-in page
    await page.wait_for_url(lambda url: "/signin" not in url, timeout=20_000)
    print(f"  Logged in as {email}")


async def _navigate_to_month(page: Page, year: int, month: int) -> None:
    """
    Navigate the calendar to the given month/year using the month select dropdown.

    Houfy renders a <select> with option values in MMYYYY format (e.g. "042026").
    Selecting the correct option is instant and does not require clicking arrows.
    Falls back to checking that a date cell for the target month is visible.
    """
    value = f"{month:02d}{year}"  # e.g. "042026" for April 2026
    select = page.locator(_SEL_MONTH_SELECT).first
    try:
        await select.wait_for(state="visible", timeout=3_000)
        await select.select_option(value=value)
        # Wait for the first cell of the target month to appear.
        # Houfy lazy-loads each month's pricing data from the server;
        # a fixed sleep is too short and causes the "30s between dates" symptom.
        first_day = f"{year:04d}-{month:02d}-01"
        await page.locator(f'[data-date="{first_day}"]').first.wait_for(
            state="visible", timeout=35_000
        )
    except Exception:
        await page.wait_for_timeout(500)  # fallback if select or cell not found


async def _set_single_date_price(
    page: Page, rate_date: str, price: float, debug: bool = False
) -> bool:
    """
    Click one date cell and set its nightly price.

    Flow:
      1. Locate the date cell by data-date attribute.
      2. Skip if invisible or has a blocked CSS class.
      3. Click the cell — the editor sidebar opens ("Selected Date" heading).
      4. Fill the price-per-night input (placeholder="0", no min attr).
      5. Click "Save Pricing" button.
      6. Close the editor panel so it's clean for the next date click.

    Returns True on success, False if skipped.
    """
    import time as _time
    def _t(label: str, t0: float) -> float:
        t1 = _time.monotonic()
        if debug:
            print(f"      [{label}] {t1 - t0:.2f}s", flush=True)
        return t1

    t = _time.monotonic()

    # Close any editor panel that may be open from a previous date
    close_btn = page.locator(_SEL_EDITOR_CLOSE).first
    try:
        if await close_btn.is_visible(timeout=50):
            await close_btn.click(timeout=500)
            await page.wait_for_timeout(80)
    except Exception:
        pass
    t = _t("pre-close", t)

    cell_sel = _SEL_DATE_CELL.format(date=rate_date)
    cell = page.locator(cell_sel).first

    if not await cell.is_visible(timeout=2_000):
        return False
    t = _t("cell-visible", t)

    # Skip dates that carry a blocked/reserved CSS class
    classes = await cell.get_attribute("class") or ""
    if any(cls in classes for cls in _BLOCKED_CLASSES):
        return False

    await cell.click()
    t = _t("cell-click", t)

    # Wait for the editor panel to open ("Selected Date" h2)
    editor = page.locator(_SEL_EDITOR_HEADER).first
    try:
        await editor.wait_for(state="visible", timeout=5_000)
    except Exception:
        return False  # editor did not open — date is not editable
    t = _t("editor-open", t)

    # Fill the price-per-night input
    price_input = page.locator(_SEL_PRICE_INPUT).first
    try:
        await price_input.wait_for(state="visible", timeout=3_000)
    except Exception:
        return False
    t = _t("input-visible", t)

    await price_input.click(click_count=3)   # select all existing value
    await price_input.fill(str(int(price)))
    t = _t("fill", t)

    # Click "Save Pricing"
    save_btn = page.locator(_SEL_SAVE_BTN).first
    try:
        await save_btn.wait_for(state="visible", timeout=3_000)
        await save_btn.click()
    except Exception:
        await price_input.press("Enter")
    t = _t("save-click", t)

    await page.wait_for_timeout(250)  # let React commit the save
    t = _t("post-save-sleep", t)

    # Close the editor panel so the next date click opens cleanly
    close_btn = page.locator(_SEL_EDITOR_CLOSE).first
    try:
        if await close_btn.is_visible(timeout=50):
            await close_btn.click(timeout=500)
            await page.wait_for_timeout(80)
    except Exception:
        pass  # editor may have auto-closed after save — that's fine
    _t("post-close", t)

    return True


async def sync_property(
    page:          Page,
    listing_id:    str,
    rates:         list[dict],
    inspect_mode:  bool,
    blocked_dates: set[str] | None = None,
    debug:         bool = False,
) -> dict:
    """
    Navigate to the Houfy pricing calendar for one listing and sync all rates.

    blocked_dates — set of YYYY-MM-DD strings pre-fetched from HostHub that are
    already booked/blocked.  Dates in this set are skipped before opening the
    browser, which avoids a per-date CSS-class check and collapses entire booking
    spans into a single log line.

    Returns {"set": int, "skipped": int, "errors": int}.
    """
    cal_url = _HOUFY_CAL.format(listing_id=listing_id)
    print(f"  → {cal_url}")
    await page.goto(cal_url, wait_until="networkidle")

    if inspect_mode:
        print(
            "  [INSPECT MODE] Browser paused.\n"
            "  Use DevTools to verify _SEL_DATE_CELL, _SEL_PRICE_INPUT, _SEL_SAVE_BTN.\n"
            "  Close the Playwright inspector to continue."
        )
        await page.pause()

    _blocked = blocked_dates or set()
    stats    = {"set": 0, "skipped": 0, "errors": 0}
    current_month: tuple[int, int] | None = None  # (year, month) last navigated to

    # For cleaner logging, collapse consecutive booked dates into range summaries.
    booked_run_start: str | None = None
    booked_run_count: int = 0

    def _flush_booked_run() -> None:
        nonlocal booked_run_start, booked_run_count
        if booked_run_start and booked_run_count:
            end_d = (date.fromisoformat(booked_run_start) +
                     timedelta(days=booked_run_count - 1)).isoformat()
            label = booked_run_start if booked_run_count == 1 else f"{booked_run_start} → {end_d}"
            print(f"    ~ {label}  skipped ({booked_run_count}d booked)")
        booked_run_start = None
        booked_run_count = 0

    for entry in rates:
        rate_date = entry["date"]
        price     = entry["price"]

        # ── Pre-flight skip: date is booked in HostHub ────────────────────────
        if rate_date in _blocked:
            if booked_run_start is None:
                booked_run_start = rate_date
            booked_run_count += 1
            stats["skipped"] += 1
            continue

        # Flush any preceding booked run before processing an available date
        _flush_booked_run()

        try:
            year, month_num, _ = (int(x) for x in rate_date.split("-"))

            # Navigate to the correct month only when it changes
            if current_month != (year, month_num):
                await _navigate_to_month(page, year, month_num)
                current_month = (year, month_num)

            ok = await _set_single_date_price(page, rate_date, price, debug=debug)
            if ok:
                print(f"    ✓ {rate_date}  €{price:.0f}")
                stats["set"] += 1
            else:
                print(f"    ~ {rate_date}  skipped (unavailable in Houfy)")
                stats["skipped"] += 1

        except Exception as exc:
            msg = str(exc)
            print(f"    ✗ {rate_date}  ERROR: {msg}")
            stats["errors"] += 1
            if "browser has been closed" in msg or "Target page" in msg:
                print("  [ABORT] Browser or page closed — stopping this property.")
                break

    _flush_booked_run()  # flush any trailing booked run
    return stats


# ── Public entry point (importable by ARQ cron) ───────────────────────────────

async def run_sync(
    days:       int   = 60,
    dry_run:    bool  = False,
    visible:    bool  = False,
    inspect:    bool  = False,
    flat_rate:  float | None = None,
    property:   str   = "",
    debug:      bool  = False,
) -> None:
    """
    Main sync routine. Called by the CLI and by the ARQ cron job.

    dry_run=True  — fetch rates and print them; no browser opened.
    visible=True  — show the Chromium browser window.
    inspect=True  — pause after login for selector debugging (requires visible=True).
    flat_rate=N   — use a fixed €N/night instead of fetching from PriceLabs.
                    Use this when PriceLabs IDs are not yet configured.
    property      — case-insensitive substring filter on property name.
    """
    email    = os.getenv("HOUFY_EMAIL", "")
    password = os.getenv("HOUFY_PASSWORD", "")

    active_props = [
        p for p in PROPERTY_MAPPINGS
        if p.get("houfy_listing_id") and (
            p.get("hosthub_rate_plan_id") or p.get("pricelabs_id") or flat_rate is not None
        )
        and (not property or property.lower() in p["name"].lower())
    ]

    if not active_props:
        print(
            "No properties configured.\n"
            "Set HOUFY_LISTING_ID_* and HOSTHUB_RATE_PLAN_ID_* in .env\n"
            "(or PRICELABS_PROPERTY_ID_* with IAPI credentials, or pass --flat-rate N)."
        )
        return

    if dry_run:
        print(f"=== Houfy Price Sync — DRY RUN ({days} days"
              f"{f', flat €{flat_rate:.0f}' if flat_rate else ''}) ===\n")
        for prop in active_props:
            print(f"Property: {prop['name']}  (Houfy ID: {prop['houfy_listing_id']})")
            try:
                if flat_rate is not None:
                    rates = generate_flat_rates(flat_rate, days)
                    print(f"  {len(rates)} dates at flat €{flat_rate:.0f}/night")
                elif prop.get("hosthub_rate_plan_id"):
                    rates = await fetch_hosthub_rates(prop["hosthub_rate_plan_id"], days)
                    print(f"  {len(rates)} rates from HostHub (PriceLabs-synced)")
                else:
                    rates = await fetch_pricelabs_rates(prop["pricelabs_id"], days)
                    print(f"  {len(rates)} rates fetched from PriceLabs IAPI")
                for r in rates[:5]:
                    print(f"  [DRY RUN] {r['date']}  €{r['price']:.0f}")
                if len(rates) > 5:
                    print(f"  ... and {len(rates) - 5} more")
            except Exception as exc:
                print(f"  ERROR: {exc}")
            print()
        return

    if not email or not password:
        print("ERROR: HOUFY_EMAIL and HOUFY_PASSWORD must be set in .env")
        return

    print(
        f"=== Houfy Price Sync ({days} days, "
        f"{'visible' if visible else 'headless'}) ===\n"
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not visible)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page    = await context.new_page()

        print("Logging in to Houfy...")
        await houfy_login(page, email, password)

        for prop in active_props:
            print(f"\nProperty: {prop['name']}  (Houfy ID: {prop['houfy_listing_id']})")
            try:
                if flat_rate is not None:
                    rates = generate_flat_rates(flat_rate, days)
                    print(f"  {len(rates)} dates at flat €{flat_rate:.0f}/night")
                elif prop.get("hosthub_rate_plan_id"):
                    rates = await fetch_hosthub_rates(prop["hosthub_rate_plan_id"], days)
                    print(f"  {len(rates)} rates from HostHub (PriceLabs-synced)")
                else:
                    rates = await fetch_pricelabs_rates(prop["pricelabs_id"], days)
                    print(f"  {len(rates)} rates from PriceLabs IAPI")
            except Exception as exc:
                print(f"  ERROR fetching rates: {exc}")
                continue

            # Pre-fetch booked dates so the browser loop can skip them in bulk
            blocked: set[str] = set()
            rental_id = prop.get("pricelabs_id") or prop.get("hosthub_rate_plan_id", "")
            # hosthub rental IDs match pricelabs_id (SyncBnb shares the same IDs)
            hh_rental_id = prop.get("pricelabs_id", "")
            if hh_rental_id and _HH_API_KEY:
                try:
                    blocked = await fetch_hosthub_blocked_dates(hh_rental_id, days)
                    if blocked:
                        print(f"  {len(blocked)} booked/blocked dates pre-fetched (will skip)")
                except Exception:
                    pass  # non-fatal — fall back to browser-side detection

            stats = await sync_property(
                page, prop["houfy_listing_id"], rates, inspect,
                blocked_dates=blocked, debug=debug,
            )
            print(
                f"  Summary: {stats['set']} set, "
                f"{stats['skipped']} skipped, "
                f"{stats['errors']} errors"
            )

        await browser.close()

    print("\nDone.")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync PriceLabs nightly rates to Houfy pricing calendar"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch rates and print them — no browser opened"
    )
    parser.add_argument(
        "--visible", action="store_true",
        help="Show the browser window (default: headless)"
    )
    parser.add_argument(
        "--days", type=int, default=60,
        help="Days ahead to sync (default: 60)"
    )
    parser.add_argument(
        "--flat-rate", type=float, default=None, metavar="PRICE",
        help="Use a fixed price (EUR/night) instead of fetching from PriceLabs"
    )
    parser.add_argument(
        "--inspect",  action="store_true",
        help="Pause after login to verify selectors in DevTools (requires --visible)"
    )
    parser.add_argument(
        "--property", type=str, default="", metavar="NAME",
        help="Case-insensitive substring filter — sync only matching properties"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Print per-step timing for each date (identifies which step is slow)"
    )
    args = parser.parse_args()
    asyncio.run(run_sync(
        days=args.days,
        dry_run=args.dry_run,
        visible=args.visible,
        flat_rate=args.flat_rate,
        inspect=args.inspect,
        property=args.property,
        debug=args.debug,
    ))
