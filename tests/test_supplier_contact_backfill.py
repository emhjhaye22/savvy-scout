import json
from datetime import datetime, timezone

from savvy_scout.db.connection import get_connection, init_db


def _release_json(supplier_name, contact):
    return json.dumps({
        "id": "rls-1-TEST",
        "parties": [
            {"id": "org-1", "name": "A Buyer", "roles": ["buyer"]},
            {"id": "org-2", "name": supplier_name, "roles": ["supplier"], "contactPoint": contact},
        ],
        "tender": {"id": "tender-1", "title": "A tender"},
        "awards": [{"id": "awd-1", "suppliers": [{"name": supplier_name, "id": "org-2"}]}],
    })


def test_migration_backfills_supplier_contact_from_raw_json_on_a_pre_existing_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)  # normal boot: schema.sql already has the new columns

    # Simulate a database from before this migration existed.
    conn.execute("ALTER TABLE notices DROP COLUMN supplier_contact_name")
    conn.execute("ALTER TABLE notices DROP COLUMN supplier_contact_email")
    conn.execute("ALTER TABLE notices DROP COLUMN supplier_contact_phone")

    now = datetime.now(timezone.utc).isoformat()
    contact = {"name": "Ria Newham", "email": "ria.newham@harveynash.com", "telephone": "+44 131 555 0100"}
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, source, uk_stage, raw_json, first_seen_at, "
        "last_swept_at, created_at, updated_at, supplier_name) "
        "VALUES ('REF-A', 'A tender', 'A Buyer', 'Find a Tender', 'UK5', ?, ?, ?, ?, ?, 'Wrong Name')",
        (_release_json("Harvey Nash Limited", contact), now, now, now, now),
    )
    conn.commit()

    cols_before = [r[1] for r in conn.execute("PRAGMA table_info(notices)").fetchall()]
    assert "supplier_contact_name" not in cols_before

    # Re-running init_db (as every app boot does) must add the columns back
    # and backfill this row from its already-stored raw_json.
    init_db(conn)

    row = conn.execute("SELECT * FROM notices WHERE ref = 'REF-A'").fetchone()
    assert row["supplier_name"] == "Harvey Nash Limited"  # also self-heals the GR-Taxis-style bug
    assert row["supplier_contact_name"] == "Ria Newham"
    assert row["supplier_contact_email"] == "ria.newham@harveynash.com"
    assert row["supplier_contact_phone"] == "+44 131 555 0100"

    conn.close()


def test_migration_is_a_no_op_on_a_database_that_already_has_the_columns(tmp_path):
    """Running init_db again (every app boot) after the columns already
    exist must not error or re-scan every notice unnecessarily."""
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    init_db(conn)  # second boot -- must not raise
    conn.close()
