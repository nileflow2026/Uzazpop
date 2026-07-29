"""
services/medicine_sync.py
──────────────────────────
Imports medicine data from the Kenya Pharmacy and Poisons Board (PPB)
public register, to pre-populate the product catalogue with medicines
actually registered for use in Kenya — instead of manual data entry.

DATA SOURCE:
  PPB Regulatory Information Management System (PRIMS) public register
    https://prims.pharmacyboardkenya.org/pharma_register_public/
  This is not an official published API — it's the internal AJAX/DataTables
  endpoint the public register page itself calls to load results. It can
  change or break without notice since it isn't a documented, versioned
  API. That's why every fetch failure here is surfaced clearly (see
  PPBFetchError) instead of ever being silently treated as "done" —
  the exact bug that was previously found and fixed in the old OpenFDA
  importer this replaces.

STRATEGY:
  1. GET the public register page first, to pick up whatever session
     cookie the server sets (the AJAX endpoint may expect one).
  2. GET the AJAX endpoint in DataTables-style pages (start/length),
     with headers that mimic the page's own JS fetching it.
  3. Each row is a plain array (not an object) — see _extract_ppb_fields
     for the column mapping.
  4. Idempotent — skips anything already in the catalogue by name or
     PPB registration number. Safe to re-run; already-imported medicines
     are never re-imported, so this never needs to run again after the
     catalogue is populated (new stock is added via Add Medicine / Edit,
     not by re-syncing).
"""

import logging
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from config import get_settings
from models.orm import Medicine

logger = logging.getLogger(__name__)
settings = get_settings()

PPB_BASE_URL = "https://prims.pharmacyboardkenya.org"
PPB_REGISTER_PAGE = f"{PPB_BASE_URL}/pharma_register_public/"
PPB_AJAX_URL = f"{PPB_BASE_URL}/ajax/public"

# Column order in each PPB result row (a plain array, not an object).
# Determined from the endpoint's own DataTables output.
_COL_REG_NO = 0
_COL_TRADE_NAME = 1
_COL_GENERIC_NAME = 2
_COL_STRENGTH = 3
_COL_PACK_SIZE = 4
_COL_CATEGORY = 5          # e.g. "GENERIC/BIOSIMILARS"
_COL_ASSESSMENT_TYPE = 6   # not stored — not useful day-to-day
_COL_ORIGIN = 7            # "LOCAL" / "FOREIGN"
_COL_REG_DATE = 8          # not stored — not useful day-to-day
_COL_DISTRIBUTOR = 9
_COL_MANUFACTURER = 10
_COL_STATUS = 11           # e.g. "Registered<span>...</span>" — HTML-tagged


class PPBFetchError(Exception):
    """Raised when a fetch to the PPB register fails outright (network,
    timeout, bad status, or an unexpected non-JSON response). Distinct
    from a normal empty page, which just means we've reached the end
    of the register. Callers must NOT treat these the same, or a
    connection/format problem gets silently misreported as success."""
    pass


async def _fetch_ppb_page(client: httpx.AsyncClient, start: int, length: int) -> Dict[str, Any]:
    """
    Fetch one page of the PPB public register (DataTables-style
    pagination). Returns the parsed JSON body
    ({"recordsTotal", "recordsFiltered", "data": [...]}).

    Raises PPBFetchError on any real failure — including the response
    not actually being JSON, which is what happens if PPB serves back
    its normal HTML page instead of the AJAX data (e.g. because a
    session/header requirement changed).
    """
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": PPB_REGISTER_PAGE,
    }
    params = {
        "fetch": "all_ph_ectd",
        "draw": 1,
        "start": start,
        "length": length,
    }
    try:
        resp = await client.get(PPB_AJAX_URL, params=params, headers=headers)
    except httpx.HTTPError as exc:
        raise PPBFetchError(
            f"Could not reach the PPB register (prims.pharmacyboardkenya.org): {exc}. "
            f"Check this server's internet connection."
        ) from exc

    if resp.status_code != 200:
        raise PPBFetchError(f"PPB register returned HTTP {resp.status_code} at start={start}")

    try:
        return resp.json()
    except ValueError as exc:
        # The endpoint served back HTML (or something else) instead of JSON —
        # most likely the site changed, or it needs a session this request
        # didn't have. Fail loudly rather than pretending this means "done".
        raise PPBFetchError(
            "PPB register did not return JSON — it may have changed, or require "
            "a browser session this backend doesn't have. "
            f"Response started with: {resp.text[:200]!r}"
        ) from exc


def _strip_html(value: str) -> str:
    """PPB's status column comes back with HTML like 'Registered</span>' —
    strip any tags, keep just the text."""
    import re
    return re.sub(r"<[^>]*>", "", value or "").strip()


def _extract_ppb_fields(row: List[str]) -> Optional[Dict[str, Any]]:
    """Map one PPB result row (a plain array) onto our Medicine fields.
    Returns None if the row is unusable (no trade name)."""
    if len(row) <= _COL_TRADE_NAME:
        return None

    name = (row[_COL_TRADE_NAME] or "").strip()
    if not name:
        return None

    def col(i: int) -> Optional[str]:
        return row[i].strip() if len(row) > i and row[i] else None

    return {
        "name": name[:300],
        "generic_name": col(_COL_GENERIC_NAME),
        "strength": col(_COL_STRENGTH),
        "ppb_registration_no": col(_COL_REG_NO),
        "ppb_pack_size": col(_COL_PACK_SIZE),
        "ppb_origin": col(_COL_ORIGIN),
        "ppb_distributor": col(_COL_DISTRIBUTOR),
        "manufacturer": col(_COL_MANUFACTURER),
        "ppb_status": _strip_html(row[_COL_STATUS]) if len(row) > _COL_STATUS else None,
    }


async def sync_ppb_bulk(
    db: Session,
    target_count: int = 500,
    start_skip: int = 0,
    max_pages: int = 50,
) -> Dict[str, Any]:
    """
    Bulk-import medicines from the Kenya PPB public register.

    Paginates through results starting at `start_skip`, until
    `target_count` new medicines are imported or `max_pages` is reached
    (safety cap — each page is a network call, and one HTTP
    request/response should not run forever).

    Idempotent — skips anything already in the catalogue by name or
    PPB registration number. Safe to re-run; once the catalogue is
    populated there's no need to sync again — new stock is added via
    the normal Add Medicine / Edit flows, not by re-running this.

    Resumable: pass back the returned `next_skip` on the next call to
    continue where this one left off (the frontend's "Import All"
    button does this automatically until `exhausted=true`).

    Returns {"imported", "skipped", "errors", "pages_fetched",
    "next_skip", "exhausted", "last_name", "fetch_error", "records_total"}.
    """
    imported = skipped = errors = 0
    start = start_skip
    page_size = 100
    pages_fetched = 0
    exhausted = False
    last_name = None
    fetch_error = None
    records_total = None

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0), follow_redirects=True) as client:
        try:
            # Visit the public register page first so any session cookie
            # the server sets is picked up before we call the AJAX endpoint.
            await client.get(PPB_REGISTER_PAGE)
        except httpx.HTTPError as exc:
            return {
                "imported": 0, "skipped": 0, "errors": 0, "pages_fetched": 0,
                "next_skip": start_skip, "exhausted": False, "last_name": None,
                "fetch_error": f"Could not reach the PPB register page: {exc}",
                "records_total": None,
            }

        while imported < target_count and pages_fetched < max_pages:
            try:
                body = await _fetch_ppb_page(client, start, page_size)
            except PPBFetchError as exc:
                fetch_error = str(exc)
                break  # Stop — do NOT mark this as exhausted, it's a real failure

            pages_fetched += 1
            records_total = body.get("recordsTotal", records_total)
            rows = body.get("data") or []
            if not rows:
                exhausted = True
                break  # No more results — genuine end of the register

            for row in rows:
                if imported >= target_count:
                    break
                try:
                    fields = _extract_ppb_fields(row)
                    if not fields:
                        skipped += 1
                        continue

                    # ── Idempotency: by name or by PPB registration number ──
                    filters = [Medicine.name == fields["name"]]
                    if fields["ppb_registration_no"]:
                        filters.append(Medicine.ppb_registration_no == fields["ppb_registration_no"])
                    existing = db.query(Medicine).filter(or_(*filters)).first()
                    if existing:
                        skipped += 1
                        continue

                    med = Medicine(
                        name=fields["name"],
                        generic_name=fields["generic_name"],
                        strength=fields["strength"],
                        manufacturer=fields["manufacturer"],
                        ppb_registration_no=fields["ppb_registration_no"],
                        ppb_pack_size=fields["ppb_pack_size"],
                        ppb_origin=fields["ppb_origin"],
                        ppb_distributor=fields["ppb_distributor"],
                        ppb_status=fields["ppb_status"],
                        source="ppb",
                        unit_price=0,      # Pharmacy sets their own selling price
                        reorder_level=10,
                        # PPB's register doesn't indicate OTC vs prescription-only,
                        # so this defaults to OTC — review/edit per medicine as needed.
                        requires_prescription=False,
                    )
                    db.add(med)
                    db.commit()
                    imported += 1
                    last_name = fields["name"]

                except Exception as exc:
                    db.rollback()
                    errors += 1
                    logger.error("Error importing PPB row: %s", exc)

            start += page_size

    logger.info(
        "PPB bulk sync complete – imported=%d skipped=%d errors=%d pages=%d next_start=%d fetch_error=%s",
        imported, skipped, errors, pages_fetched, start, fetch_error,
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "pages_fetched": pages_fetched,
        "next_skip": start,
        "exhausted": exhausted,
        "last_name": last_name,
        "fetch_error": fetch_error,
        "records_total": records_total,
    }
