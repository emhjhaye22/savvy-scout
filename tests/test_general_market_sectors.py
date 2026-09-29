"""2026-09-21: CPV-division fallback sectors for notices no Trifork sector
keyword matches (config_cpv_division_sectors,
sector_classifier.classify_general_market_sector, Gate 1)."""

from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all
from savvy_scout.models.notice import Status
from savvy_scout.triage.gates import gate1_sector_owner, triage_notice
from savvy_scout.triage.sector_classifier import classify_general_market_sector
from savvy_scout.workflow.approvals import retriage_all_unmatched

UNRELATED_TEXT = "resurfacing of the high street car park and footpaths"


def _insert_new_notice(conn, ref, cpv_primary, buyer="Anytown Parish Works Dept", text=UNRELATED_TEXT):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO notices (
            ref, title, buyer, source, uk_stage, status, cpv_primary, text_blob,
            tender_status, raw_json, first_seen_at, last_swept_at, created_at, updated_at
        ) VALUES (?, ?, ?, 'Find a Tender', 'UK3', 'NEW', ?, ?, 'active', '{}', ?, ?, ?, ?)
        """,
        (ref, f"Title {ref}", buyer, cpv_primary, text, now, now, now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_cpv_division_table_is_seeded(conn):
    count = conn.execute("SELECT COUNT(*) FROM config_cpv_division_sectors WHERE enabled = 1").fetchone()[0]
    assert count >= 40
    assert classify_general_market_sector(conn, "45233141") == "Construction"
    assert classify_general_market_sector(conn, "80000000") == "Education and Training"


def test_classifier_handles_missing_unknown_and_disabled_cpv(conn):
    assert classify_general_market_sector(conn, None) is None
    assert classify_general_market_sector(conn, "") is None
    assert classify_general_market_sector(conn, "99000000") is None
    conn.execute("UPDATE config_cpv_division_sectors SET enabled = 0 WHERE cpv_prefix = '45'")
    assert classify_general_market_sector(conn, "45000000") is None


def test_gate1_cpv_fallback_without_owner_fails_with_sector_persisted(conn):
    result = gate1_sector_owner(conn, "Anytown Parish Works Dept", UNRELATED_TEXT, "45233141")
    assert result.outcome == "FAIL"
    assert result.extra == {"sector": "Construction"}


def test_gate1_keyword_match_still_wins_over_cpv(conn):
    result = gate1_sector_owner(conn, "National Grid", "smart grid monitoring for energy distribution", "45233141")
    assert result.outcome == "PASS"
    assert result.extra["sector"] == "Energy"


def test_gate1_no_keyword_and_no_cpv_behaves_as_before(conn):
    result = gate1_sector_owner(conn, "Unrelated Buyer", "totally unrelated procurement text", None)
    assert result.outcome == "FAIL"
    assert result.extra == {}


def test_gate1_cpv_sector_with_owner_passes(conn):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO config_owner_map (sector, owner, notes, updated_at, updated_by) VALUES (?, ?, NULL, ?, 'test')",
        ("Construction", "Mark", now),
    )
    result = gate1_sector_owner(conn, "Anytown Parish Works Dept", UNRELATED_TEXT, "45233141")
    assert result.outcome == "PASS"
    assert result.extra == {"sector": "Construction", "owner": "Mark"}


def test_triage_auto_rejects_unowned_cpv_sector_but_keeps_the_label(conn):
    notice_id = _insert_new_notice(conn, "REF-CONSTRUCTION", "45233141")
    triage_notice(conn, notice_id)
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["sector"] == "Construction"
    assert row["owner"] is None
    assert row["status"] == Status.REJECTED.value
    assert row["auto_rejected_unowned"] == 1


def test_retriage_all_unmatched_backfills_legacy_rejected_notices(conn):
    """The existing production backlog: auto-rejected unowned with
    sector=NULL, from before the CPV fallback existed."""
    notice_id = _insert_new_notice(conn, "REF-LEGACY", "80000000")
    conn.execute(
        "UPDATE notices SET status = 'REJECTED', auto_rejected_unowned = 1, sector = NULL WHERE id = ?",
        (notice_id,),
    )
    conn.commit()

    counts = retriage_all_unmatched(conn)

    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert counts["now_matched"] == 1
    assert row["sector"] == "Education and Training"
    assert row["status"] == Status.REJECTED.value
    assert row["auto_rejected_unowned"] == 1


def test_admin_retriage_unmatched_route_and_config_table(tmp_path):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    trifork_id = setup_conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at, client_id) "
        "VALUES ('adminowner', ?, 'Admin Owner', 1, ?, ?)",
        (generate_password_hash("testpass"), datetime.now(timezone.utc).isoformat(), trifork_id),
    )
    notice_id = _insert_new_notice(setup_conn, "REF-ROUTE", "85100000")
    setup_conn.execute(
        "UPDATE notices SET status = 'REJECTED', auto_rejected_unowned = 1 WHERE id = ?", (notice_id,)
    )
    setup_conn.commit()
    setup_conn.close()

    app = create_app(Settings(
        db_path=db_path, lookback_days=7, find_a_tender_base_url="", contracts_finder_base_url="",
        flask_secret_key="test-key", ms_graph_tenant_id=None, ms_graph_client_id=None,
        ms_graph_client_secret=None, ms_graph_sender_upn=None,
    ))
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    client = app.test_client()
    client.post("/login", data={"username": "adminowner", "password": "testpass"})

    page = client.get("/admin/").get_data(as_text=True)
    assert "General Market Sectors (CPV)" in page
    assert "Retriage unclassified notices" in page

    resp = client.post("/admin/retriage-unmatched", follow_redirects=True)
    assert resp.status_code == 200
    assert "now have a sector" in resp.get_data(as_text=True)

    check = get_connection(db_path)
    assert check.execute("SELECT sector FROM notices WHERE id = ?", (notice_id,)).fetchone()["sector"] == (
        "Health and Social Care"
    )


def test_retriage_routes_cpv_sector_into_pipeline_once_it_gains_an_owner(conn):
    """The 'feeds into the real pipeline' promise: a CPV-labelled, unowned,
    auto-rejected notice must still be picked up by retriage_all_unmatched
    after Admin adds an owner for that sector."""
    notice_id = _insert_new_notice(conn, "REF-ONBOARD", "45233141")
    triage_notice(conn, notice_id)
    assert conn.execute("SELECT sector FROM notices WHERE id = ?", (notice_id,)).fetchone()["sector"] == "Construction"

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO config_owner_map (sector, owner, notes, updated_at, updated_by) VALUES (?, ?, NULL, ?, 'test')",
        ("Construction", "Mark", now),
    )
    conn.commit()

    counts = retriage_all_unmatched(conn)

    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert counts["checked"] == 1
    assert row["owner"] == "Mark"
    assert row["status"] != Status.REJECTED.value
    assert row["auto_rejected_unowned"] == 0
