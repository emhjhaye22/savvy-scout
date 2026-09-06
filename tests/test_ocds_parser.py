from savvy_scout.sources.ocds_parser import (
    _find_award_supplier,
    _find_supplier_name,
    parse_release,
    parse_release_package,
)


def test_parses_real_sample_notice(sample_ocds_package):
    parsed_list = parse_release_package(sample_ocds_package, source="Find a Tender")
    assert len(parsed_list) == 1
    parsed = parsed_list[0]
    notice = parsed.notice

    assert notice.ref == "066188-2026"
    assert notice.ocid == "ocds-h6vhtk-06cac6"
    assert notice.title == "Legacy Infrastructure Support"
    assert notice.buyer == "Transport for London"
    assert notice.notice_type == "UK2"
    assert notice.uk_stage == "UK2"
    assert notice.indicative_value is None

    # This notice has no tender.classification or item classification, only
    # additionalClassifications, so the primary CPV is inferred from those.
    assert notice.cpv_primary == "48800000"
    assert notice.cpv_primary_inferred is True
    assert notice.cpv_additional == ["48800000", "50300000", "72100000"]

    assert parsed.tender_status == "planning"
    assert parsed.lot_statuses == ["planning"]
    assert parsed.tender_period_end is None
    assert parsed.pme_due_date == "2026-08-07T23:59:59+01:00"
    assert notice.deadline == "2026-08-07T23:59:59+01:00"
    assert parsed.future_notice_date == "2026-09-08T23:59:59+01:00"
    assert parsed.contract_end_date == "2031-07-12T23:59:59+01:00"
    assert parsed.is_award is False

    assert "legacy infrastructure support" in parsed.text_blob
    assert notice.raw_json  # full release JSON retained as an evidence snapshot


def _batched_release():
    """Mimics the real live bug (2026-09-06): a Public Contracts Scotland
    release batches many unrelated awards' parties into one shared list --
    "GR Taxis" happens to be first with role "supplier" here, even though
    this specific release's own award was won by someone else entirely."""
    return {
        "id": "rls-1-TEST",
        "ocid": "ocds-test-0001",
        "tag": ["award"],
        "date": "2026-08-10T12:00:00Z",
        "parties": [
            {"id": "org-1", "name": "East Lothian Council", "roles": ["buyer"]},
            {"id": "org-2", "name": "GR Taxis", "roles": ["supplier"],
             "address": {"streetAddress": "Bankhead House", "locality": "Tranent"}},
            {"id": "org-61", "name": "Harvey Nash Limited", "roles": ["supplier"],
             "address": {"streetAddress": "1 Recruitment Row", "locality": "Edinburgh"},
             "contactPoint": {"name": "Ria Newham", "email": "ria.newham@harveynash.com", "telephone": "+44 131 555 0100"}},
        ],
        "buyer": {"name": "Care Inspectorate", "id": "org-1"},
        "tender": {"id": "tender-1", "title": "Interim HR Business Partner", "description": ""},
        "awards": [
            {"id": "awd-1", "suppliers": [{"name": "Harvey Nash Limited", "id": "org-61"}]},
        ],
    }


def test_find_supplier_name_prefers_award_scoped_supplier_over_batched_parties():
    release = _batched_release()
    assert _find_supplier_name(release) == "Harvey Nash Limited"


def test_find_award_supplier_returns_matching_party_for_address():
    release = _batched_release()
    name, party = _find_award_supplier(release)
    assert name == "Harvey Nash Limited"
    assert party["id"] == "org-61"
    assert party["address"]["locality"] == "Edinburgh"


def test_find_supplier_name_falls_back_to_party_role_scan_when_no_award_yet():
    """A pre-award tender/planning release has no awards[] at all -- this
    must still fall back to the old release-wide role scan rather than
    returning None outright."""
    release = {
        "id": "rls-2-TEST",
        "tender": {"id": "tender-1", "title": "A future tender"},
        "parties": [
            {"id": "org-1", "name": "East Lothian Council", "roles": ["buyer"]},
            {"id": "org-2", "name": "A Tenderer Ltd", "roles": ["tenderer"]},
        ],
        "awards": [],
    }
    assert _find_supplier_name(release) == "A Tenderer Ltd"


def test_parse_release_uses_award_scoped_supplier_end_to_end():
    release = _batched_release()
    parsed = parse_release(release, source="Public Contracts Scotland")
    assert parsed.notice.supplier_name == "Harvey Nash Limited"
    assert "Edinburgh" in (parsed.notice.supplier_address or "")


def test_parse_release_extracts_supplier_contact_details():
    """2026-09-06: real data already published as part of the official
    award notice -- not scraped, not fabricated."""
    release = _batched_release()
    parsed = parse_release(release, source="Public Contracts Scotland")
    assert parsed.notice.supplier_contact_name == "Ria Newham"
    assert parsed.notice.supplier_contact_email == "ria.newham@harveynash.com"
    assert parsed.notice.supplier_contact_phone == "+44 131 555 0100"


def test_parse_release_supplier_contact_fields_none_when_absent():
    release = {
        "id": "rls-3-TEST",
        "tender": {"id": "tender-1", "title": "A tender"},
        "parties": [{"id": "org-1", "name": "A Supplier Ltd", "roles": ["supplier"]}],
        "awards": [{"id": "awd-1", "suppliers": [{"name": "A Supplier Ltd", "id": "org-1"}]}],
    }
    parsed = parse_release(release, source="Find a Tender")
    assert parsed.notice.supplier_contact_name is None
    assert parsed.notice.supplier_contact_email is None
    assert parsed.notice.supplier_contact_phone is None
