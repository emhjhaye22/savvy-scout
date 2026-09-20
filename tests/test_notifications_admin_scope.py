from datetime import datetime, timezone

from savvy_scout.dashboard.notifications import (
    STAGE_GROUPS,
    VICTORIA_STAGE_SLUGS,
    get_notification_context,
    get_sidebar_stage_counts,
)


def _insert_notice(conn, ref, status, sector="Fintech", owner="Mark", cpv_primary="72200000", uk_stage="UK3"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO notices (ref, title, buyer, sector, owner, status, source, uk_stage, "
        "cpv_primary, raw_json, first_seen_at, last_swept_at, created_at, updated_at) "
        "VALUES (?, ?, 'A Buyer', ?, ?, ?, 'Find a Tender', ?, ?, '{}', ?, ?, ?, ?)",
        (ref, f"Title {ref}", sector, owner, status, uk_stage, cpv_primary, now, now, now, now),
    )
    conn.commit()


def test_sidebar_stage_counts_admin_sees_every_stage_and_out_of_scope_notice(conn):
    """2026-09-20 explicit request ("no filters here"): Admin must see
    every Workflow Stages row (not restricted to VICTORIA_STAGE_SLUGS like
    Victoria's own narrower view) and every trade/sector, including a
    notice with no sector at all owned by someone else entirely."""
    _insert_notice(conn, "REF-UNCLASSIFIED", "TO_REVIEW", sector=None, owner=None)
    _insert_notice(conn, "REF-OTHER-OWNER", "PHASE2_SCOPED", owner="Someone Else")

    counts = get_sidebar_stage_counts(conn, owner="Admin Display Name", is_approver=0, is_admin=True)
    by_slug = {c["slug"]: c["count"] for c in counts}

    assert set(by_slug) == {slug for slug, *_ in STAGE_GROUPS}  # every stage, not just VICTORIA_STAGE_SLUGS
    assert by_slug["to_review"] == 1
    assert by_slug["phase2_scoped"] == 1


def test_sidebar_stage_counts_non_admin_stays_scoped(conn):
    """The widening above is Admin-only -- a regular sector owner (and
    Account Approver, unchanged) must keep seeing only Trifork's
    configured sectors, exactly as before."""
    _insert_notice(conn, "REF-UNCLASSIFIED", "TO_REVIEW", sector=None, owner=None)

    counts = get_sidebar_stage_counts(conn, owner="Mark", is_approver=0, is_admin=False)
    by_slug = {c["slug"]: c["count"] for c in counts}
    assert by_slug["to_review"] == 0

    approver_counts = get_sidebar_stage_counts(conn, owner="Victoria", is_approver=1, is_admin=False)
    approver_slugs = {c["slug"] for c in approver_counts}
    assert approver_slugs == VICTORIA_STAGE_SLUGS


def test_notification_context_admin_sees_every_owner_and_out_of_scope_notice(conn):
    _insert_notice(conn, "REF-UNCLASSIFIED", "TO_REVIEW", sector=None, owner=None)
    _insert_notice(conn, "REF-OTHER-OWNER", "AWAITING_PHASE2_APPROVAL", owner="Someone Else")

    ctx = get_notification_context(conn, owner="Admin Display Name", is_approver=0, is_admin=True)
    assert ctx["attention_count"] == 2

    non_admin_ctx = get_notification_context(conn, owner="Mark", is_approver=0, is_admin=False)
    assert non_admin_ctx["attention_count"] == 0
