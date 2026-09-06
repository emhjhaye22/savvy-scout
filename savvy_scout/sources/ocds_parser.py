"""OCDS release package parser, built directly against 066188-2026_ocds.json's
real structure: a release package with a single (or repeated) release, a
tender block that carries title/description/items/lots even at planning
stage, and noticeType living on planning.documents[] / tender.documents[]
rather than on the release itself.

Non-negotiable: never invent a value. Any field absent from the source is
left as None (persisted as NULL / UNVERIFIED), never guessed or defaulted."""

import json
import re
from dataclasses import dataclass, field

from savvy_scout.models.notice import Notice

UK_STAGE_PATTERN = re.compile(r"^UK[1-5]$")


#: OCDS release tags that actually represent an opportunity being published
#: (a new tender, or its earlier planning notice) -- as opposed to award,
#: contract, amendment, or termination releases, which record something
#: happening to an ALREADY-published tender and carry no reliable trace of
#: when that tender was first published (2026-08-10 finding: an award-only
#: release has no tenderPeriod or notice documents at all, so there is no
#: usable original-publish date anywhere in its payload).
_PUBLISH_EVENT_TAGS = frozenset({"tender", "planning"})


@dataclass
class ParsedNotice:
    notice: Notice
    text_blob: str
    tender_status: str | None
    lot_statuses: list[str] = field(default_factory=list)
    tender_period_end: str | None = None
    pme_due_date: str | None = None
    future_notice_date: str | None = None
    contract_end_date: str | None = None
    is_award: bool = False
    is_publish_event: bool = False


def _find_buyer_name(release: dict) -> str | None:
    buyer = release.get("buyer") or {}
    if buyer.get("name"):
        return buyer["name"]
    for party in release.get("parties", []):
        if "buyer" in party.get("roles", []):
            return party.get("name")
    return None


def _find_party_by_role(release: dict, role: str) -> dict | None:
    for party in release.get("parties", []) or []:
        if role in (party.get("roles") or []):
            return party
    return None


def _format_address(party: dict | None) -> str | None:
    if not party:
        return None
    address = party.get("address") or {}
    parts = [
        address.get("streetAddress"),
        address.get("locality"),
        address.get("postalCode"),
        address.get("countryName") or address.get("country"),
    ]
    parts = [p for p in parts if p]
    return ", ".join(parts) if parts else None


def _find_award_supplier(release: dict) -> tuple[str | None, dict | None]:
    """Returns (name, matching party-or-None) for the actual winning
    supplier of THIS award. award.suppliers[] is scoped to the specific
    award this release represents -- release-level parties[] is not: some
    sources (2026-09-06 finding, Public Contracts Scotland) batch many
    unrelated awards into one release, sharing a single combined parties
    list across all of them. Picking "the first party tagged supplier" out
    of that shared list attributes the wrong company to notices that have
    nothing to do with it (confirmed live: dozens of Scottish Government
    research/staffing awards were all mis-attributed to "GR Taxis", a real
    East Lothian taxi firm that happened to be first in that batch's
    parties array). award.suppliers[] doesn't have this problem -- it's
    only ever populated with the supplier(s) of that specific award.
    Falls back to a release-wide role scan only when there's no award-level
    supplier at all (e.g. a pre-award tender/planning release, where there
    is no award yet to be scoped to)."""
    for award in release.get("awards", []) or []:
        for supplier in award.get("suppliers", []) or []:
            name = supplier.get("name")
            if name:
                party = next(
                    (p for p in release.get("parties", []) or [] if p.get("id") == supplier.get("id")),
                    None,
                )
                return name, party
    party = _find_party_by_role(release, "supplier") or _find_party_by_role(release, "tenderer")
    return (party.get("name") if party else None), party


def _find_supplier_name(release: dict) -> str | None:
    name, _ = _find_award_supplier(release)
    return name


def _find_cpv_description(tender: dict, cpv_code: str | None) -> str | None:
    if not cpv_code:
        return None
    classification = tender.get("classification") or {}
    if classification.get("scheme") == "CPV" and classification.get("id") == cpv_code:
        return classification.get("description")
    for item in tender.get("items", []) or []:
        item_classification = item.get("classification") or {}
        if item_classification.get("scheme") == "CPV" and item_classification.get("id") == cpv_code:
            return item_classification.get("description")
        for extra in item.get("additionalClassifications", []) or []:
            if extra.get("scheme") == "CPV" and extra.get("id") == cpv_code:
                return extra.get("description")
    return None


def _find_notice_type(release: dict) -> str | None:
    planning = release.get("planning", {}) or {}
    tender = release.get("tender", {}) or {}
    documents = list(planning.get("documents", []) or []) + list(tender.get("documents", []) or [])
    for doc in documents:
        notice_type = doc.get("noticeType")
        if notice_type:
            return notice_type
    return None


def _find_notice_url(release: dict) -> str | None:
    """Direct link to the published notice (e.g. on Find a Tender or
    Contracts Finder), taken from the document's own 'url' field. Never
    constructed/guessed from the ref -- only used if the source actually
    published it."""
    planning = release.get("planning", {}) or {}
    tender = release.get("tender", {}) or {}
    documents = list(planning.get("documents", []) or []) + list(tender.get("documents", []) or [])
    for doc in documents:
        if doc.get("url"):
            return doc["url"]
    return None


def _derive_uk_stage(notice_type: str | None) -> str:
    if notice_type and UK_STAGE_PATTERN.match(notice_type):
        return notice_type
    return "UNVERIFIED"


def _collect_cpvs(tender: dict) -> tuple[str | None, bool, list[str]]:
    """Returns (primary_cpv, primary_is_inferred, additional_cpvs).

    Primary comes from tender.classification if present (CPV scheme), else
    the first item's classification, else falls back to the first
    additionalClassifications CPV entry with primary_is_inferred=True, since
    this OCDS profile doesn't always carry a distinct primary classification.
    additional_cpvs is every CPV code found in items[].additionalClassifications,
    deduplicated, for reference regardless of what became primary."""
    items = tender.get("items", []) or []

    def cpv_id(classification: dict | None) -> str | None:
        if classification and classification.get("scheme") == "CPV":
            return classification.get("id")
        return None

    primary = cpv_id(tender.get("classification"))
    inferred = False

    if not primary and items:
        primary = cpv_id(items[0].get("classification"))

    additional: list[str] = []
    for item in items:
        for extra in item.get("additionalClassifications", []) or []:
            if extra.get("scheme") == "CPV":
                code = extra.get("id")
                if code and code not in additional:
                    additional.append(code)

    if not primary and additional:
        primary = additional[0]
        inferred = True

    return primary, inferred, additional


def _extract_value(tender: dict) -> str | None:
    value = tender.get("value") or {}
    if value.get("amount") is not None:
        currency = value.get("currency", "")
        return f"{value['amount']} {currency}".strip()
    min_value = (tender.get("minValue") or {}).get("amount")
    max_value = (tender.get("maxValue") or {}).get("amount")
    if min_value is not None or max_value is not None:
        currency = (tender.get("minValue") or tender.get("maxValue") or {}).get("currency", "")
        return f"{min_value or '?'}-{max_value or '?'} {currency}".strip()
    return None


def _extract_pme_due_date(planning: dict) -> str | None:
    for milestone in planning.get("milestones", []) or []:
        if milestone.get("type") == "engagement":
            return milestone.get("dueDate")
    return None


def _extract_value_gross(tender: dict) -> str | None:
    value = tender.get("value") or {}
    amount_gross = value.get("amountGross")
    if amount_gross is None:
        return None
    currency = value.get("currency", "")
    return f"{amount_gross} {currency}".strip()


def _extract_award_criteria_summary(lot: dict) -> str | None:
    award_criteria = lot.get("awardCriteria") or {}
    weighting_description = award_criteria.get("weightingDescription")
    if weighting_description:
        return weighting_description
    criteria = award_criteria.get("criteria") or []
    if not criteria:
        return None
    return "; ".join(
        f"{c.get('name', c.get('type', 'Criterion'))}: {c.get('description', '')}".strip(": ")
        for c in criteria
    )


def _extract_renewal_description(lot: dict) -> str | None:
    renewal_desc = (lot.get("renewal") or {}).get("description")
    if renewal_desc:
        return renewal_desc
    return (lot.get("options") or {}).get("description")


def _extract_conflicts_assessment(tender: dict) -> str | None:
    for doc in tender.get("documents", []) or []:
        if doc.get("documentType") == "conflictOfInterest":
            return doc.get("description") or "Prepared"
    return None


# documentType values that are actual bid documents (ITT pack, PQQ/selection
# criteria, specs, clarifications) rather than procedural notice pages
# (tenderNotice, awardNotice, contractNotice, etc. -- those are just the HTML
# notice itself, already surfaced via notice_url) or conflictOfInterest
# (already its own field via _extract_conflicts_assessment). 2026-07-30: found
# via a full scan of every notice's raw_json -- 2,769 of 7,320 notices
# actually carry real bidding documents that were never being extracted.
BID_DOCUMENT_TYPES = {
    "biddingDocuments",
    "technicalSpecifications",
    "technicalSelectionCriteria",
    "economicSelectionCriteria",
    "eligibilityCriteria",
    "evaluationCriteria",
    "clarifications",
    "submissionDocuments",
    "procurementPlan",
    "marketStudies",
    "contractSummary",
}


def extract_bid_documents(release: dict) -> list[dict]:
    """Real, directly-downloadable bid documents (ITT/PQQ/spec/clarifications)
    found in tender.documents[] / planning.documents[] -- never a
    login-gated e-tendering portal link (those live in
    submission_method_details instead, since they're not documents, just a
    URL to register on). Never invents a URL: only documents the source
    itself published with a url are included."""
    tender = release.get("tender", {}) or {}
    planning = release.get("planning", {}) or {}
    documents = list(planning.get("documents", []) or []) + list(tender.get("documents", []) or [])
    seen_urls = set()
    result = []
    for doc in documents:
        url = doc.get("url")
        doc_type = doc.get("documentType")
        if not url or doc_type not in BID_DOCUMENT_TYPES or url in seen_urls:
            continue
        seen_urls.add(url)
        result.append({
            "documentType": doc_type,
            "title": doc.get("title"),
            "description": doc.get("description"),
            "format": doc.get("format"),
            "url": url,
            "datePublished": doc.get("datePublished"),
        })
    return result


def extract_additional_fields(release: dict) -> dict:
    """Extra OCDS fields beyond parse_release's original scope (2026-07-30):
    award criteria/weighting, submission instructions, enquiry/award dates,
    contract start/extension details, buyer PPON/website/org type,
    conflicts-of-interest status. All present in the source release all
    along -- just never parsed out into a queryable/displayable field
    before. Shared by parse_release (new sweeps) and the connection.py
    migration (backfilling existing raw_json)."""
    tender = release.get("tender", {}) or {}
    buyer_party = _find_party_by_role(release, "buyer")
    buyer_details = (buyer_party or {}).get("details") or {}
    buyer_identifier = (buyer_party or {}).get("identifier") or {}
    org_classifications = buyer_details.get("classifications") or []
    org_type = org_classifications[0].get("description") if org_classifications else None

    lots = tender.get("lots", []) or []
    award_criteria_summary = None
    contract_start_date = None
    contract_max_extent_date = None
    renewal_description = None
    for lot in lots:
        if award_criteria_summary is None:
            award_criteria_summary = _extract_award_criteria_summary(lot)
        contract_period = lot.get("contractPeriod") or {}
        if contract_start_date is None:
            contract_start_date = contract_period.get("startDate")
        if contract_max_extent_date is None:
            contract_max_extent_date = contract_period.get("maxExtentDate")
        if renewal_description is None:
            renewal_description = _extract_renewal_description(lot)

    submission_terms = tender.get("submissionTerms") or {}
    languages = submission_terms.get("languages") or []

    bid_documents = extract_bid_documents(release)

    return {
        "value_amount_gross": _extract_value_gross(tender),
        "above_threshold": tender.get("aboveThreshold"),
        "main_procurement_category": tender.get("mainProcurementCategory"),
        "enquiry_period_end": (tender.get("enquiryPeriod") or {}).get("endDate"),
        "award_period_end": (tender.get("awardPeriod") or {}).get("endDate"),
        "submission_method_details": tender.get("submissionMethodDetails"),
        "submission_languages": ", ".join(languages) if languages else None,
        "electronic_submission_policy": submission_terms.get("electronicSubmissionPolicy"),
        "procedure_features": (tender.get("procedure") or {}).get("features"),
        "award_criteria_summary": award_criteria_summary,
        "contract_start_date": contract_start_date,
        "contract_max_extent_date": contract_max_extent_date,
        "renewal_description": renewal_description,
        "buyer_ppon": buyer_identifier.get("id") if buyer_identifier.get("scheme") == "GB-PPON" else None,
        "buyer_website": buyer_details.get("url"),
        "buyer_org_type": org_type,
        "conflicts_assessment": _extract_conflicts_assessment(tender),
        "bid_documents_json": json.dumps(bid_documents) if bid_documents else None,
    }


def parse_release(release: dict, source: str) -> ParsedNotice:
    tender = release.get("tender", {}) or {}
    planning = release.get("planning", {}) or {}

    ref = release.get("id") or release.get("ocid")
    if not ref:
        raise ValueError("Release has neither id nor ocid, cannot build a stable reference")

    title = tender.get("title") or "UNVERIFIED"
    description = tender.get("description") or ""
    buyer = _find_buyer_name(release)
    notice_type = _find_notice_type(release)
    uk_stage = _derive_uk_stage(notice_type)

    primary_cpv, cpv_inferred, additional_cpvs = _collect_cpvs(tender)
    indicative_value = _extract_value(tender)

    tender_period_end = (tender.get("tenderPeriod") or {}).get("endDate")
    pme_due_date = _extract_pme_due_date(planning)
    deadline = tender_period_end or pme_due_date

    buyer_party = _find_party_by_role(release, "buyer")
    cpv_primary_description = _find_cpv_description(tender, primary_cpv)
    additional_fields = extract_additional_fields(release)
    supplier_name, supplier_party = _find_award_supplier(release)

    notice = Notice(
        ref=ref,
        title=title,
        buyer=buyer,
        source=source,
        notice_type=notice_type,
        uk_stage=uk_stage,
        raw_json="",  # filled in by the caller with the full release JSON text
        ocid=release.get("ocid"),
        indicative_value=indicative_value,
        cpv_primary=primary_cpv,
        cpv_primary_inferred=cpv_inferred,
        cpv_additional=additional_cpvs,
        deadline=deadline,
        cpv_primary_description=cpv_primary_description,
        supplier_name=supplier_name,
        supplier_address=_format_address(supplier_party),
        supplier_contact_name=((supplier_party or {}).get("contactPoint") or {}).get("name"),
        supplier_contact_email=((supplier_party or {}).get("contactPoint") or {}).get("email"),
        supplier_contact_phone=((supplier_party or {}).get("contactPoint") or {}).get("telephone"),
        buyer_address=_format_address(buyer_party),
        buyer_contact_email=((buyer_party or {}).get("contactPoint") or {}).get("email"),
        buyer_region=((buyer_party or {}).get("address") or {}).get("region"),
        procurement_method=tender.get("procurementMethod"),
        procurement_method_details=tender.get("procurementMethodDetails"),
        notice_url=_find_notice_url(release),
        published_at=release.get("date"),
        **additional_fields,
    )

    lots = tender.get("lots", []) or []
    contract_end_date = None
    for lot in lots:
        end_date = (lot.get("contractPeriod") or {}).get("endDate")
        if end_date:
            contract_end_date = end_date
            break

    text_blob_parts = [title, description]
    if tender.get("procurementMethodDetails"):
        text_blob_parts.append(tender["procurementMethodDetails"])
    if cpv_primary_description:
        text_blob_parts.append(cpv_primary_description)
    text_blob = "\n".join(text_blob_parts).lower()
    tags = release.get("tag", []) or []

    return ParsedNotice(
        notice=notice,
        text_blob=text_blob,
        tender_status=tender.get("status"),
        lot_statuses=[lot.get("status") for lot in lots if lot.get("status")],
        tender_period_end=tender_period_end,
        pme_due_date=pme_due_date,
        future_notice_date=(tender.get("communication") or {}).get("futureNoticeDate"),
        contract_end_date=contract_end_date,
        is_award=("award" in tags) or bool(release.get("awards")),
        is_publish_event=bool(set(tags) & _PUBLISH_EVENT_TAGS),
    )


def parse_release_package(package: dict, source: str) -> list[ParsedNotice]:
    parsed = []
    for release in package.get("releases", []):
        result = parse_release(release, source)
        result.notice.raw_json = _release_to_json(release)
        parsed.append(result)
    return parsed


def _release_to_json(release: dict) -> str:
    import json

    return json.dumps(release, ensure_ascii=False)
