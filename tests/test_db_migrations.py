from datetime import datetime, timezone

from werkzeug.security import generate_password_hash

from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def test_fresh_db_seeds_all_six_sources_including_csv(tmp_path):
    """2026-08-10 regression: the contracts_finder_csv migration in
    _apply_migrations originally ran unconditionally, inserting its row
    into config_sources before seed_sources ever got a chance to run --
    which made the table look non-empty, so seed_sources's
    "if not _table_empty(...): return" guard skipped seeding the other 5
    default sources entirely on a genuinely fresh database. The migration
    must only backfill an ALREADY-seeded database; a fresh one goes
    through seed_sources for every row, that one included."""
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    seed_all(conn)
    conn.close()

    # Re-open and re-run init_db, same as every real app boot does.
    conn2 = get_connection(db_path)
    init_db(conn2)
    source_types = {r["source_type"] for r in conn2.execute("SELECT source_type FROM config_sources").fetchall()}
    conn2.close()

    assert source_types == {
        "find_a_tender", "contracts_finder", "contracts_finder_csv",
        "public_contracts_scotland", "sell2wales", "etendersni",
    }


def test_already_seeded_db_backfills_missing_csv_source(tmp_path):
    """A production DB seeded before this migration existed (missing only
    the contracts_finder_csv row) must get it backfilled on next boot,
    without duplicating or disturbing the other already-seeded rows."""
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    seed_all(conn)
    conn.execute("DELETE FROM config_sources WHERE source_type = 'contracts_finder_csv'")
    conn.commit()
    conn.close()

    conn2 = get_connection(db_path)
    init_db(conn2)
    rows = conn2.execute("SELECT source_type, COUNT(*) AS n FROM config_sources GROUP BY source_type").fetchall()
    conn2.close()

    counts = {r["source_type"]: r["n"] for r in rows}
    assert counts.get("contracts_finder_csv") == 1
    assert counts.get("find_a_tender") == 1
    assert counts.get("contracts_finder") == 1
    assert sum(counts.values()) == 6


def test_stranded_null_client_id_is_healed_on_next_boot(tmp_path):
    """2026-09-19 regression: the users.client_id backfill only ran the
    FIRST time the column was added, gated on "column doesn't exist yet".
    SQLite's ALTER TABLE ADD COLUMN is durable independent of any later
    commit(), while the backfill UPDATE right after it is not -- init_db()
    commits only once, at the very end of _apply_migrations(). A process
    killed between that ALTER and the final commit (a deploy restart, an
    interrupted first boot against months of real data) would leave every
    existing user's client_id permanently NULL, since the gating condition
    ("client_id column missing") is false on every subsequent boot and
    nothing else ever revisits it. This simulates exactly that stranded
    state directly (client_id column present, value NULL) regardless of
    how it got there, and confirms the self-healing backfill fixes it."""
    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    seed_all(conn)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, is_admin, created_at, client_id) "
        "VALUES ('mark', ?, 'Mark', 0, 1, ?, NULL)",
        (generate_password_hash("testpass"), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    conn2 = get_connection(db_path)
    init_db(conn2)
    trifork_id = conn2.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    row = conn2.execute("SELECT client_id FROM users WHERE username = 'mark'").fetchone()
    conn2.close()

    assert row["client_id"] == trifork_id
