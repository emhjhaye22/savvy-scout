"""Companies House lookup for Competitor Intel (2026-09-06): registered
company name, company number, registered address, and status -- the only
things a competitor's company entry can legitimately be enriched with. No
public procurement data or the Companies House register publishes personal
contact details (email, phone, named contact) for company staff, so this
deliberately stops at what's real rather than fabricating anything further.

Free API, register at
https://developer.company-information.service.gov.uk/. Uses HTTP Basic
Auth with the API key as the username and a blank password, per Companies
House's own documented auth scheme."""

import sqlite3
from datetime import datetime, timezone

import requests

SEARCH_URL = "https://api.company-information.service.gov.uk/search/companies"
COMPANY_URL_TEMPLATE = "https://find-and-update.company-information.service.gov.uk/company/{number}"
REQUEST_TIMEOUT_SECONDS = 10


def _search_companies_house(supplier_name: str, api_key: str) -> dict | None:
    """Returns the top search match's fields, or None if nothing matched.
    Raises requests.RequestException/ValueError on a genuine call failure
    (network error, bad response) -- callers must not cache those as
    "not found", only a call that actually completed with zero results."""
    response = requests.get(
        SEARCH_URL,
        params={"q": supplier_name, "items_per_page": 1},
        auth=(api_key, ""),
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    data = response.json()

    items = data.get("items") or []
    if not items:
        return None
    top = items[0]
    return {
        "company_name": top.get("title"),
        "company_number": top.get("company_number"),
        "address": top.get("address_snippet"),
        "status": top.get("company_status"),
    }


def get_company_info(conn: sqlite3.Connection, supplier_name: str, api_key: str | None) -> dict | None:
    """Cache-first lookup. Returns None if no API key is configured, if the
    cached (or fresh) search found nothing, or if the live call itself
    failed -- callers can't distinguish these cases from the return value
    alone, which is fine here since the template only needs to know
    whether there's anything to show."""
    if not api_key:
        return None

    cached = conn.execute(
        "SELECT * FROM company_lookups WHERE supplier_name = ?", (supplier_name,)
    ).fetchone()
    if cached is not None:
        if not cached["found"]:
            return None
        return {
            "company_name": cached["company_name"],
            "company_number": cached["company_number"],
            "address": cached["address"],
            "status": cached["status"],
        }

    try:
        result = _search_companies_house(supplier_name, api_key)
    except (requests.RequestException, ValueError):
        return None

    now = datetime.now(timezone.utc).isoformat()
    if result is None:
        conn.execute(
            "INSERT INTO company_lookups (supplier_name, found, looked_up_at) VALUES (?, 0, ?)",
            (supplier_name, now),
        )
    else:
        conn.execute(
            "INSERT INTO company_lookups "
            "(supplier_name, found, company_name, company_number, address, status, looked_up_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?)",
            (
                supplier_name, result["company_name"], result["company_number"],
                result["address"], result["status"], now,
            ),
        )
    conn.commit()
    return result
