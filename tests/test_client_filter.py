import json
from datetime import datetime, timezone

from savvy_scout.triage.client_filter import (
    evaluate_client_filter,
    run_client_triage,
    run_client_triage_for_notice,
)


def _insert_notice(
    conn, ref, cpv_primary=None, buyer="A Buyer", text_blob="", uk_stage="UK3",
    buyer_region=None, indicative_value=None,
):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, cpv_primary, buyer_region, "
        "indicative_value, text_blob, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', ?, 'Find a Tender', ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?)",
        (ref, buyer, uk_stage, cpv_primary, buyer_region, indicative_value, text_blob, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT * FROM notices WHERE ref = ?", (ref,)).fetchone()


def _make_client(conn, name="Acme Construction", **filter_kwargs):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES (?, 1, ?, 'test')",
        (name, now),
    )
    client_id = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]
    defaults = {
        "cpv_prefixes": None, "keywords": None, "notice_types": None,
        "regions": None, "min_value": None, "max_value": None,
    }
    defaults.update(filter_kwargs)
    conn.execute(
        "INSERT INTO client_filters (client_id, cpv_prefixes, keywords, notice_types, regions, "
        "min_value, max_value, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'test')",
        (
            client_id, defaults["cpv_prefixes"], defaults["keywords"], defaults["notice_types"],
            defaults["regions"], defaults["min_value"], defaults["max_value"], now,
        ),
    )
    conn.commit()
    return client_id


def _get_filter(conn, client_id):
    return conn.execute("SELECT * FROM client_filters WHERE client_id = ?", (client_id,)).fetchone()


def test_trifork_is_seeded_as_a_client(conn):
    row = conn.execute("SELECT * FROM clients WHERE name = 'Trifork'").fetchone()
    assert row is not None
    assert row["is_active"] == 1


def test_no_constraints_configured_always_passes(conn):
    client_id = _make_client(conn)
    notice = _insert_notice(conn, "REF-A")
    outcome, reason = evaluate_client_filter(_get_filter(conn, client_id), notice)
    assert outcome == "PASS"


def test_cpv_prefix_match(conn):
    client_id = _make_client(conn, cpv_prefixes=json.dumps(["45", "71"]))
    matching = _insert_notice(conn, "REF-A", cpv_primary="45200000")
    non_matching = _insert_notice(conn, "REF-B", cpv_primary="72500000")
    no_cpv = _insert_notice(conn, "REF-C", cpv_primary=None)

    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), matching)
    assert outcome == "PASS"
    outcome, reason = evaluate_client_filter(_get_filter(conn, client_id), non_matching)
    assert outcome == "FAIL"
    assert "72500000" in reason
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), no_cpv)
    assert outcome == "FAIL"


def test_keyword_match(conn):
    client_id = _make_client(conn, keywords=json.dumps(["construction", "building works"]))
    matching = _insert_notice(conn, "REF-A", text_blob="major construction project in Leeds")
    non_matching = _insert_notice(conn, "REF-B", text_blob="cloud software platform")

    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), matching)
    assert outcome == "PASS"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), non_matching)
    assert outcome == "FAIL"


def test_notice_type_match(conn):
    client_id = _make_client(conn, notice_types=json.dumps(["UK3", "UK4"]))
    matching = _insert_notice(conn, "REF-A", uk_stage="UK4")
    non_matching = _insert_notice(conn, "REF-B", uk_stage="UK5")

    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), matching)
    assert outcome == "PASS"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), non_matching)
    assert outcome == "FAIL"


def test_region_match(conn):
    client_id = _make_client(conn, regions=json.dumps(["London", "North West"]))
    matching = _insert_notice(conn, "REF-A", buyer_region="London")
    non_matching = _insert_notice(conn, "REF-B", buyer_region="Scotland")
    no_region = _insert_notice(conn, "REF-C", buyer_region=None)

    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), matching)
    assert outcome == "PASS"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), non_matching)
    assert outcome == "FAIL"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), no_region)
    assert outcome == "FAIL"


def test_value_range_match(conn):
    client_id = _make_client(conn, min_value=100000, max_value=500000)
    in_range = _insert_notice(conn, "REF-A", indicative_value="250000 GBP")
    too_low = _insert_notice(conn, "REF-B", indicative_value="50000 GBP")
    too_high = _insert_notice(conn, "REF-C", indicative_value="900000 GBP")
    no_value = _insert_notice(conn, "REF-D", indicative_value=None)

    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), in_range)
    assert outcome == "PASS"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), too_low)
    assert outcome == "FAIL"
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), too_high)
    assert outcome == "FAIL"
    # No stated value yet -- must not be excluded just because a value
    # filter is configured; there's nothing to compare against.
    outcome, _ = evaluate_client_filter(_get_filter(conn, client_id), no_value)
    assert outcome == "PASS"


def test_all_configured_fields_must_match_and_not_or(conn):
    client_id = _make_client(
        conn, cpv_prefixes=json.dumps(["45"]), regions=json.dumps(["London"]),
    )
    cpv_only = _insert_notice(conn, "REF-A", cpv_primary="45200000", buyer_region="Scotland")
    region_only = _insert_notice(conn, "REF-B", cpv_primary="72500000", buyer_region="London")
    both = _insert_notice(conn, "REF-C", cpv_primary="45200000", buyer_region="London")

    assert evaluate_client_filter(_get_filter(conn, client_id), cpv_only)[0] == "FAIL"
    assert evaluate_client_filter(_get_filter(conn, client_id), region_only)[0] == "FAIL"
    assert evaluate_client_filter(_get_filter(conn, client_id), both)[0] == "PASS"


def test_run_client_triage_evaluates_every_notice(conn):
    client_id = _make_client(conn, cpv_prefixes=json.dumps(["45"]))
    _insert_notice(conn, "REF-A", cpv_primary="45200000")
    _insert_notice(conn, "REF-B", cpv_primary="72500000")

    count = run_client_triage(conn, client_id)
    assert count == 2

    results = {
        r["notice_id"]: r["outcome"]
        for r in conn.execute("SELECT * FROM client_triage_results WHERE client_id = ?", (client_id,)).fetchall()
    }
    assert len(results) == 2
    outcomes = set(results.values())
    assert outcomes == {"PASS", "FAIL"}


def test_run_client_triage_is_idempotent_on_rerun(conn):
    """Re-running after a filter change updates the existing row rather
    than duplicating it (ON CONFLICT upsert)."""
    client_id = _make_client(conn, cpv_prefixes=json.dumps(["45"]))
    _insert_notice(conn, "REF-A", cpv_primary="72500000")

    run_client_triage(conn, client_id)
    row = conn.execute("SELECT outcome FROM client_triage_results WHERE client_id = ?", (client_id,)).fetchone()
    assert row["outcome"] == "FAIL"

    conn.execute("UPDATE client_filters SET cpv_prefixes = ? WHERE client_id = ?", (json.dumps(["72"]), client_id))
    conn.commit()
    run_client_triage(conn, client_id)

    rows = conn.execute("SELECT outcome FROM client_triage_results WHERE client_id = ?", (client_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["outcome"] == "PASS"


def test_run_client_triage_for_notice_skips_trifork(conn):
    """Trifork must never get a client_triage_results row -- its notices
    are only ever evaluated by gates.py's own triage_notice()."""
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    _make_client(conn, name="Acme Construction", cpv_prefixes=json.dumps(["45"]))
    notice = _insert_notice(conn, "REF-A", cpv_primary="45200000")

    run_client_triage_for_notice(conn, notice)

    trifork_result = conn.execute(
        "SELECT * FROM client_triage_results WHERE client_id = ?", (trifork_id,)
    ).fetchone()
    assert trifork_result is None

    other_result = conn.execute(
        "SELECT * FROM client_triage_results WHERE notice_id = ?", (notice["id"],)
    ).fetchall()
    assert len(other_result) == 1
    assert other_result[0]["outcome"] == "PASS"


def test_run_client_triage_for_notice_skips_inactive_clients(conn):
    client_id = _make_client(conn, cpv_prefixes=json.dumps(["45"]))
    conn.execute("UPDATE clients SET is_active = 0 WHERE id = ?", (client_id,))
    conn.commit()
    notice = _insert_notice(conn, "REF-A", cpv_primary="45200000")

    run_client_triage_for_notice(conn, notice)

    result = conn.execute(
        "SELECT * FROM client_triage_results WHERE client_id = ?", (client_id,)
    ).fetchone()
    assert result is None
