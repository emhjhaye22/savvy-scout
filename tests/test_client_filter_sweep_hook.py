import json
from datetime import datetime, timezone

from savvy_scout.sweep.runner import triage_pending


def _insert_notice(conn, ref, cpv_primary="45200000", text_blob="construction of a new bridge"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, status, cpv_primary, text_blob, "
        "raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, 'A notice', 'A Buyer', 'Find a Tender', 'UK3', 'NEW', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, cpv_primary, text_blob, now, now, now, now),
    )
    conn.commit()
    return conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]


def _make_client(conn, name, cpv_prefixes):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, is_active, created_at, created_by) VALUES (?, 1, ?, 'test')",
        (name, now),
    )
    client_id = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]
    conn.execute(
        "INSERT INTO client_filters (client_id, cpv_prefixes, updated_at, updated_by) VALUES (?, ?, ?, 'test')",
        (client_id, json.dumps(cpv_prefixes), now),
    )
    conn.commit()
    return client_id


def test_triage_pending_also_evaluates_active_clients(conn):
    client_id = _make_client(conn, "Acme Construction", ["45"])
    notice_id = _insert_notice(conn, "REF-A", cpv_primary="45200000")

    triaged = triage_pending(conn)

    assert triaged == 1
    result = conn.execute(
        "SELECT * FROM client_triage_results WHERE client_id = ? AND notice_id = ?",
        (client_id, notice_id),
    ).fetchone()
    assert result is not None
    assert result["outcome"] == "PASS"


def test_triage_pending_does_not_create_a_trifork_client_triage_row(conn):
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    notice_id = _insert_notice(conn, "REF-A")

    triage_pending(conn)

    result = conn.execute(
        "SELECT * FROM client_triage_results WHERE client_id = ? AND notice_id = ?",
        (trifork_id, notice_id),
    ).fetchone()
    assert result is None
