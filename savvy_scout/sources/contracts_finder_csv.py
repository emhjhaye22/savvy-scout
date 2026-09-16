"""Contracts Finder's own CSV export (the "Download CSV" button on its search
results page) -- a broader dataset than its public OCDS API.

Confirmed live 2026-08-10: 4 genuine, live ("Open") opportunities -- ordinary
tender/PME notices, nothing exotic -- were completely absent from Contracts
Finder's OCDS feed regardless of lookback window or how long after publish we
checked (a real structural gap, not a timing lag; one of the four is
syndicated via a third-party portal per its own description, suggesting this
is a syndication/indexing gap on Contracts Finder's side). The same 4 are all
present in this CSV export. This module exists to close that gap; it doesn't
replace sweep_contracts_finder (OCDS) since the CSV carries far fewer
structured fields (no CPV classification objects, no OCID, no lots) -- both
run, and sweep.dedupe's fuzzy title+buyer match merges the ones both feeds see
rather than duplicating them (see the confirmed-live UK stage note in
_CSV_NOTICE_TYPE_TO_UK_STAGE below for the one known tradeoff of running both).

No stateless single-request API for this: the search results page sets the
search criteria in a server-side session (cookie-based), and GetCsvFile reads
from that same session -- so this always does a GET .../Search/Results first
to set it, then GET .../Search/GetCsvFile on the same requests.Session.

lookback_days is accepted (matching every other source's sweep signature) but
NOT applied as a date filter: the site's real search form POSTs to /Search
with split published_from[day]/[month]/[year] fields plus several other
required fields, confirmed live 2026-08-10 that a GET to /Search/Results with
those same field names is silently ignored (results were identical whether a
1-day or unfiltered range was requested) and a direct POST reproduction
without the full field set the real form sends returns a generic site error.
Rather than pass parameters that look like a filter but silently aren't, this
sweeps the same bare, unfiltered "everything currently live" search every
time (confirmed stable at 689 rows) and relies on sweep.dedupe to make that
cheap: an already-seen ref/fuzzy-match just updates its existing row.

All rows returned came back Status=Open only when queried without an explicit
status filter (confirmed live 2026-08-10, 689/689 rows) -- i.e. this export
already excludes awarded/closed notices for us, unlike the OCDS feed's award/
contract/update releases that sweep.dedupe.ParsedNotice.is_publish_event has
to filter out. Every row here is therefore treated as a genuine, reliable
publish event."""

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime

import requests

from savvy_scout.models.notice import Notice
from savvy_scout.sources.ocds_parser import ParsedNotice

SOURCE_LABEL = "Contracts Finder"
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# Contracts Finder's CSV export has no native UK1-5 stage tag the way Find a
# Tender's OCDS documents do (see ocds_parser._find_notice_type) -- this is
# our own inferred mapping from the only 3 "Notice Type" values seen live
# (2026-08-10, 689-row sample). Known tradeoff: sweep.dedupe fuzzy-matches
# this against an already-OCDS-captured row for the same real notice and
# OVERWRITES that row's real OCDS-derived uk_stage with this inferred one
# (dedupe.upsert_notice only preserves the old stage when the NEW notice_type
# is None -- CSV rows always have one). Accepted as a simplification rather
# than building full field-level source-precedence tracking for this.
_CSV_NOTICE_TYPE_TO_UK_STAGE = {
    "PreProcurement": "UK2",  # early market engagement, same shape as a UK2 PIN
    "Pipeline": "UK3",  # forward-look/planned procurement notice
    "Contract": "UK4",  # live tender notice
}


def _parse_value(low: str | None, high: str | None) -> str | None:
    low = (low or "").strip()
    high = (high or "").strip()
    if low and high:
        return f"GBP {low} to {high}"
    if low:
        return f"GBP {low}"
    if high:
        return f"GBP up to {high}"
    return None


def _parse_deadline(closing_date: str | None, closing_time: str | None) -> str | None:
    """Closing Date is DD/MM/YYYY, Closing Time (if present) is HH:MM --
    combined into one ISO datetime string, same shape as every other
    source's deadline field."""
    closing_date = (closing_date or "").strip()
    if not closing_date:
        return None
    parts = closing_date.split("/")
    if len(parts) != 3:
        return None
    day, month, year = parts
    time_part = (closing_time or "").strip() or "00:00"
    try:
        return datetime.strptime(f"{year}-{month}-{day} {time_part}", "%Y-%m-%d %H:%M").isoformat()
    except ValueError:
        return None


def _parse_cpv_codes(raw: str | None) -> tuple[str | None, list[str]]:
    codes = [c.strip() for c in (raw or "").replace("\n", " ").split(" ") if c.strip()]
    if not codes:
        return None, []
    return codes[0], codes[1:]


def _parse_row(row: dict, source: str) -> ParsedNotice:
    ref = (row.get("Notice Identifier") or "").strip()
    if not ref:
        raise ValueError("CSV row has no Notice Identifier, cannot build a stable reference")

    title = (row.get("Title") or "UNVERIFIED").strip() or "UNVERIFIED"
    description = (row.get("Description") or "").strip()
    buyer = (row.get("Organisation Name") or "").strip() or None
    notice_type = (row.get("Notice Type") or "").strip() or None
    uk_stage = _CSV_NOTICE_TYPE_TO_UK_STAGE.get(notice_type, "UNVERIFIED")
    primary_cpv, additional_cpvs = _parse_cpv_codes(row.get("Cpv Codes"))
    published_at = (row.get("Published Date") or "").strip() or None
    deadline = _parse_deadline(row.get("Closing Date"), row.get("Closing Time"))
    buyer_region = (row.get("Region") or "").strip() or None
    buyer_contact_email = (row.get("Contact Email") or "").strip() or None
    buyer_website = (row.get("Contact Website") or "").strip() or None
    indicative_value = _parse_value(row.get("Value Low"), row.get("Value High"))

    notice = Notice(
        ref=ref,
        title=title,
        buyer=buyer,
        source=source,
        notice_type=notice_type,
        uk_stage=uk_stage,
        raw_json=json.dumps(row),
        indicative_value=indicative_value,
        cpv_primary=primary_cpv,
        cpv_primary_inferred=False,
        cpv_additional=additional_cpvs,
        deadline=deadline,
        buyer_region=buyer_region,
        buyer_contact_email=buyer_contact_email,
        buyer_website=buyer_website,
        published_at=published_at,
        notice_description=description or None,
    )

    text_blob = "\n".join([title, description]).lower()

    return ParsedNotice(
        notice=notice,
        text_blob=text_blob,
        tender_status=(row.get("Status") or None) or None,
        is_award=False,
        # Every row is a live "Open" listing (see module docstring), never an
        # award/contract/update artifact -- unlike OCDS releases, there is no
        # unreliable-date case to guard against here.
        is_publish_event=True,
    )


def sweep_contracts_finder_csv(base_url: str, lookback_days: int) -> Iterator[ParsedNotice]:
    # lookback_days is accepted for signature parity with every other
    # source (see runner.SOURCE_REGISTRY) but unused -- see module
    # docstring for why a date filter isn't applied here.
    del lookback_days

    session = requests.Session()
    session.headers.update(REQUEST_HEADERS)

    search_params = {"Sort": "10"}
    search_response = session.get(
        f"{base_url.rstrip('/')}/Search/Results", params=search_params, timeout=REQUEST_TIMEOUT_SECONDS
    )
    search_response.raise_for_status()

    csv_response = session.get(f"{base_url.rstrip('/')}/Search/GetCsvFile", timeout=REQUEST_TIMEOUT_SECONDS)
    csv_response.raise_for_status()

    reader = csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig")))
    for row in reader:
        try:
            yield _parse_row(row, SOURCE_LABEL)
        except ValueError:
            # A row with no usable Notice Identifier -- skip rather than
            # abort the whole source's remaining rows over one bad record.
            continue
