from datetime import datetime, timedelta, timezone
import re

from werkzeug.security import generate_password_hash

from savvy_scout.config import Settings
from savvy_scout.dashboard import create_app
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all


def _insert_notice(
    conn, ref: str, first_seen_at: str, sector: str | None,
    uk_stage: str = "UK3", cpv_primary: str | None = "72200000",
) -> None:
    """Defaults to an in-scope notice (UK3 stage, CPV 72200000, which is
    within every sector's config_sector_cpv_scope) so callers only need to
    override uk_stage/cpv_primary/sector to test the out-of-scope cases."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO notices (
            ref, ocid, title, buyer, source, notice_type, uk_stage, status, sector, owner,
            indicative_value, cpv_primary, cpv_primary_inferred, cpv_additional, deadline,
            text_blob, tender_status, lot_statuses, tender_period_end, pme_due_date,
            future_notice_date, contract_end_date, is_award, raw_json, first_seen_at,
            last_swept_at, created_at, updated_at
        ) VALUES (
            ?, NULL, ?, ?, ?, NULL, ?, 'NEW', ?, NULL,
            NULL, ?, 0, NULL, NULL,
            '', NULL, NULL, NULL, NULL,
            NULL, NULL, 0, '{}', ?,
            ?, ?, ?
        )
        """,
        (
            ref,
            f"Title {ref}",
            f"Buyer {ref}",
            "Find a Tender",
            uk_stage,
            sector,
            cpv_primary,
            first_seen_at,
            now,
            now,
            now,
        ),
    )


def _insert_notice_with_publish_dates(
    conn, ref: str, first_seen_at: str, first_published_at: str | None, published_at: str | None,
    publish_date_unknown: int = 0,
) -> None:
    """Same in-scope defaults as _insert_notice, but lets a test control
    first_published_at/published_at/publish_date_unknown independently --
    for proving Sector Performance/Notices by Source date by
    first_published_at, not the source's last-updated published_at
    (2026-08-10 finding: an old notice amended today was inflating "today"'s
    count), and exclude publish_date_unknown notices entirely (2026-08-10
    finding #2: a notice first discovered via an award/update release has
    no reliable publish date at all)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO notices (
            ref, ocid, title, buyer, source, notice_type, uk_stage, status, sector, owner,
            indicative_value, cpv_primary, cpv_primary_inferred, cpv_additional, deadline,
            text_blob, tender_status, lot_statuses, tender_period_end, pme_due_date,
            future_notice_date, contract_end_date, is_award, raw_json, published_at,
            first_published_at, publish_date_unknown, first_seen_at, last_swept_at, created_at, updated_at
        ) VALUES (
            ?, NULL, ?, ?, ?, NULL, 'UK3', 'NEW', 'Fintech', NULL,
            NULL, '72200000', 0, NULL, NULL,
            '', NULL, NULL, NULL, NULL,
            NULL, NULL, 0, '{}', ?,
            ?, ?, ?, ?, ?, ?
        )
        """,
        (
            ref, f"Title {ref}", f"Buyer {ref}", "Find a Tender",
            published_at, first_published_at, publish_date_unknown, first_seen_at, now, now, now,
        ),
    )


def _insert_triage_run(conn, ref: str, outcome: str) -> None:
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()[0]
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO triage_runs (
            notice_id, headline_gate, headline_outcome, headline_reason, evaluated_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (notice_id, "GATE", outcome, f"{outcome} outcome", now),
    )


def _logged_in_client(app, username):
    client = app.test_client()
    client.post("/login", data={"username": username, "password": "testpass"})
    return client


def _db(app):
    return get_connection(app.config["SAVVY_SCOUT_DB_PATH"])


def test_overview_shows_scouting_report(tmp_path):
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    for username, display_name in [("victoria", "Victoria"), ("mark", "Mark")]:
        setup_conn.execute(
            "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                username,
                generate_password_hash("testpass"),
                display_name,
                int(display_name == "Victoria"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    setup_conn.commit()

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=(now.weekday() - 5) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, microsecond=0)

    # In-scope notices (real sector, UK1-4 stage, CPV within that sector's
    # configured scope) -- these should all count towards the Overview.
    notices = [
        ("REF-TODAY", today_start + timedelta(hours=2), "Fintech"),
        ("REF-WEEK", week_start + timedelta(hours=3), "Energy"),
        ("REF-MONTH", month_start + timedelta(days=3, hours=1), "Rail and Transport"),
        ("REF-YTD", year_start + timedelta(days=10, hours=1), "NHS and Healthcare"),
        ("REF-LASTYEAR", now - timedelta(days=400), "Central and Local Government"),
    ]
    for ref, dt, sector in notices:
        _insert_notice(setup_conn, ref, dt.isoformat(), sector)
    for ref, outcome in [
        ("REF-TODAY", "PASS"),
        ("REF-WEEK", "FLAG"),
        ("REF-MONTH", "MAYBE"),  # legacy outcome, must fold into FLAG below, never shown as its own state
        ("REF-YTD", "FAIL"),
    ]:
        _insert_triage_run(setup_conn, ref, outcome)

    # Out-of-scope notices (2026-07-30 filter): none of these should ever
    # show up in the Overview's counts or sector breakdown.
    _insert_notice(setup_conn, "REF-NO-SECTOR", today_start.isoformat(), None)
    _insert_notice(setup_conn, "REF-WRONG-CPV", today_start.isoformat(), "Fintech", cpv_primary="45000000")
    _insert_notice(setup_conn, "REF-UK5", today_start.isoformat(), "Fintech", uk_stage="UK5")
    setup_conn.commit()
    setup_conn.close()

    settings = Settings(
        db_path=db_path,
        lookback_days=7,
        find_a_tender_base_url="",
        contracts_finder_base_url="",
        flask_secret_key="test-key",
        ms_graph_tenant_id=None,
        ms_graph_client_id=None,
        ms_graph_client_secret=None,
        ms_graph_sender_upn=None,
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    client = _logged_in_client(app, "mark")

    response = client.get("/")
    html = response.get_data(as_text=True)

    expected_ytd = sum(1 for _, dt, _ in notices if dt >= year_start)
    expected_month = sum(1 for _, dt, _ in notices if dt >= month_start)
    expected_week = sum(1 for _, dt, _ in notices if dt >= week_start)
    expected_today = sum(1 for _, dt, _ in notices if dt >= today_start)

    assert "Scouting report" in html
    assert re.search(rf'<div class="stat-value">{len(notices)}</div>\s*<div class="stat-label">Total scouted</div>', html)
    assert re.search(rf'<div class="stat-value">{expected_ytd}</div>\s*<div class="stat-label">Scouted YTD</div>', html)
    assert re.search(rf'<div class="stat-value">{expected_month}</div>\s*<div class="stat-label">This Month</div>', html)
    assert re.search(rf'<div class="stat-value">{expected_week}</div>\s*<div class="stat-label">This Week</div>', html)
    assert re.search(rf'<div class="stat-value">{expected_today}</div>\s*<div class="stat-label">Today</div>', html)
    assert "Fintech" in html
    assert "Energy" in html
    assert "Rail and Transport" in html
    assert "NHS and Healthcare" in html
    # Out-of-scope notices (no sector, wrong CPV, UK5 stage) must never
    # inflate "Total scouted" -- proven by expected_* above only counting
    # the 5 in-scope `notices`, not the 3 extra out-of-scope ones inserted.
    # No-sector notices no longer get their own "UNVERIFIED" row at all: 5
    # real sectors + "In Sector (total)" + "Total Swept (all sources)" = 7
    # Sector Performance rows, plus Notices by Source's 1 source ("Find a
    # Tender", every _insert_notice call here) + its "Total" row = 2 more
    # (2026-08-09: both tables share the same .sector-cell row-label markup).
    assert html.count('class="sector-cell"') == 9
    assert "PASS" in html
    assert "FLAG" in html
    assert "MAYBE" not in html
    assert "FAIL" in html
    # REF-WEEK (FLAG) + REF-MONTH (legacy MAYBE, folded in) = 2 in the FLAG bucket.
    assert re.search(r'<span class="legend-label">FLAG</span>\s*<span class="legend-value">2</span>', html)


def test_amended_old_notice_does_not_inflate_todays_count(tmp_path):
    """2026-08-10 finding: a notice first published weeks ago that gets
    amended/awarded/cancelled today has published_at (the source's
    last-updated timestamp) bumped to today on every re-sweep, but
    first_published_at is set once and never touched -- so _report_date
    must bucket it under its real, original publish date, not today.
    Checks _build_sector_performance/_build_source_performance directly
    (not via rendered HTML) since which weekday column "today" lands in
    depends on what day the test happens to run."""
    from zoneinfo import ZoneInfo

    from savvy_scout.dashboard.routes.home import _build_sector_performance, _build_source_performance

    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)

    now_uk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    today_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)
    three_weeks_ago = (now_uk - timedelta(days=21)).isoformat()

    # First published 3 weeks ago, then amended today: published_at moved to
    # today, first_published_at stayed pinned to the original date.
    _insert_notice_with_publish_dates(
        setup_conn, "REF-AMENDED",
        first_seen_at=three_weeks_ago,
        first_published_at=three_weeks_ago,
        published_at=today_start.isoformat(),
    )
    # A genuinely new notice, published today for the first time.
    _insert_notice_with_publish_dates(
        setup_conn, "REF-NEW-TODAY",
        first_seen_at=today_start.isoformat(),
        first_published_at=today_start.isoformat(),
        published_at=today_start.isoformat(),
    )
    setup_conn.commit()

    sector_perf = _build_sector_performance(setup_conn, now_uk)
    source_perf = _build_source_performance(setup_conn, now_uk)
    setup_conn.close()

    # "week" (Sat-Fri of the current week) is deterministic regardless of
    # what day the test runs on: 3 weeks ago is never in it, today always
    # is. If first_published_at weren't pinned, REF-AMENDED's published_at
    # (bumped to today by the simulated amendment) would put it here too,
    # making this 2 instead of 1.
    swept_total_week = next(r for r in sector_perf["rows"] if r["sector"] == "Total Swept (all sources)")["week"]
    source_week = next(r for r in source_perf["rows"] if r["sector"] == "Find a Tender")["week"]
    assert swept_total_week == 1
    assert source_week == 1


def test_weekend_published_notice_gets_its_own_day_column(tmp_path):
    """2026-08-10 finding #4: a notice genuinely published on a Sunday was
    invisible in every day column of Sector Performance/Notices by Source,
    since those tables only had Mon-Fri columns even though the daily sweep
    has no day_of_week restriction and sources do publish on weekends. Now
    a full Sat-Fri week, so the notice's real publish day shows up."""
    from zoneinfo import ZoneInfo

    from savvy_scout.dashboard.routes.home import _build_sector_performance, _build_source_performance, _perf_windows

    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)

    now_uk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    weekdays, week_start, _, _, _, _, _ = _perf_windows(now_uk)
    sunday = weekdays[1]

    _insert_notice_with_publish_dates(
        setup_conn, "REF-SUNDAY",
        first_seen_at=now_uk.isoformat(),
        first_published_at=datetime.combine(sunday, datetime.min.time(), tzinfo=now_uk.tzinfo).isoformat(),
        published_at=datetime.combine(sunday, datetime.min.time(), tzinfo=now_uk.tzinfo).isoformat(),
    )
    setup_conn.commit()

    sector_perf = _build_sector_performance(setup_conn, now_uk)
    source_perf = _build_source_performance(setup_conn, now_uk)
    setup_conn.close()

    assert [d["label"] for d in sector_perf["day_headers"]] == ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]

    swept_total_row = next(r for r in sector_perf["rows"] if r["sector"] == "Total Swept (all sources)")
    source_row = next(r for r in source_perf["rows"] if r["sector"] == "Find a Tender")
    assert swept_total_row["days"][1] == 1  # Sunday column
    assert swept_total_row["days"][:1] + swept_total_row["days"][2:] == [0, 0, 0, 0, 0, 0]
    assert source_row["days"][1] == 1
    assert swept_total_row["week"] == 1


def test_last_week_total_column(tmp_path):
    """Explicit request (2026-08-19): a "Last week" total at the start of
    Sector/Source Performance, so last week's figure stays visible for
    comparison instead of only this week's in-progress total."""
    from zoneinfo import ZoneInfo

    from savvy_scout.dashboard.routes.home import _build_sector_performance, _build_source_performance, _perf_windows

    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)

    now_uk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    _, week_start, _, _, _, last_week_start, last_week_end = _perf_windows(now_uk)

    def _at(day, hour=12):
        return datetime.combine(day, datetime.min.time(), tzinfo=now_uk.tzinfo) + timedelta(hours=hour)

    # Two notices last week, one this week, one three weeks ago -- only the
    # two from last week should land in "last_week".
    _insert_notice_with_publish_dates(
        setup_conn, "REF-LASTWEEK-1",
        first_seen_at=_at(last_week_start).isoformat(),
        first_published_at=_at(last_week_start).isoformat(),
        published_at=_at(last_week_start).isoformat(),
    )
    _insert_notice_with_publish_dates(
        setup_conn, "REF-LASTWEEK-2",
        first_seen_at=_at(last_week_end).isoformat(),
        first_published_at=_at(last_week_end).isoformat(),
        published_at=_at(last_week_end).isoformat(),
    )
    _insert_notice_with_publish_dates(
        setup_conn, "REF-THISWEEK",
        first_seen_at=_at(week_start).isoformat(),
        first_published_at=_at(week_start).isoformat(),
        published_at=_at(week_start).isoformat(),
    )
    _insert_notice_with_publish_dates(
        setup_conn, "REF-OLD",
        first_seen_at=_at(last_week_start - timedelta(days=14)).isoformat(),
        first_published_at=_at(last_week_start - timedelta(days=14)).isoformat(),
        published_at=_at(last_week_start - timedelta(days=14)).isoformat(),
    )
    setup_conn.commit()

    sector_perf = _build_sector_performance(setup_conn, now_uk)
    source_perf = _build_source_performance(setup_conn, now_uk)
    setup_conn.close()

    swept_total_row = next(r for r in sector_perf["rows"] if r["sector"] == "Total Swept (all sources)")
    source_row = next(r for r in source_perf["rows"] if r["sector"] == "Find a Tender")
    assert swept_total_row["last_week"] == 2
    assert swept_total_row["week"] == 1
    assert source_row["last_week"] == 2


def _set_status_via_history(conn, ref: str, from_status: str | None, to_status: str, changed_by: str) -> None:
    """Directly inserts the status_history row a real transition would
    write, so victoria_sourced_reject_sql's EXISTS check (keyed off the
    notice's MOST RECENT status_history row) sees exactly what a real
    reject_notice()/mark_victoria_decision() call would have left behind."""
    now = datetime.now(timezone.utc).isoformat()
    notice_id = conn.execute("SELECT id FROM notices WHERE ref = ?", (ref,)).fetchone()["id"]
    conn.execute("UPDATE notices SET status = ? WHERE id = ?", (to_status, notice_id))
    conn.execute(
        "INSERT INTO status_history (notice_id, from_status, to_status, changed_by, changed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (notice_id, from_status, to_status, changed_by, now),
    )


def test_approval_rate_counts_approved_and_victoria_sourced_rejections_only(tmp_path):
    """2026-08-10: replaces the removed Contract Expiry Radar panel.
    APPROVED and every stage further along the happy path (CAPTURE_BRIEF_
    DRAFTED, DOCS_DOWNLOADED, CALENDARED, ACTIVE) all count as approved --
    they passed through an APPROVED decision on the way, which is only ever
    Victoria's own call (see workflow.approvals.approve_phase2's docstring).
    Rejected is deliberately scoped the same way (explicit request:
    "rejection and approval should [be the] same") -- only a REJECTED
    reached via ESCALATED_TO_VICTORIA counts; an owner's own earlier reject
    (straight from TO_REVIEW, never reaching her) does not. Notices still
    awaiting a decision (TO_REVIEW here) don't count either way."""
    from savvy_scout.dashboard.routes.home import _build_approval_rate
    from savvy_scout.dashboard.scope_filter import in_scope_filter_sql

    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    seed_all(conn)

    now = datetime.now(timezone.utc).isoformat()
    for ref in [
        "REF-APPROVED", "REF-CALENDARED", "REF-ACTIVE",
        "REF-VICTORIA-REJECTED", "REF-OWNER-REJECTED", "REF-STILL-DECIDING",
    ]:
        _insert_notice(conn, ref, now, "Fintech")
    conn.commit()

    _set_status_via_history(conn, "REF-APPROVED", "ESCALATED_TO_VICTORIA", "APPROVED", "Victoria")
    _set_status_via_history(conn, "REF-CALENDARED", "ESCALATED_TO_VICTORIA", "APPROVED", "Victoria")
    conn.execute("UPDATE notices SET status = 'CALENDARED' WHERE ref = 'REF-CALENDARED'")
    _set_status_via_history(conn, "REF-ACTIVE", "ESCALATED_TO_VICTORIA", "APPROVED", "Victoria")
    conn.execute("UPDATE notices SET status = 'ACTIVE' WHERE ref = 'REF-ACTIVE'")
    _set_status_via_history(conn, "REF-VICTORIA-REJECTED", "ESCALATED_TO_VICTORIA", "REJECTED", "Victoria")
    # An owner's own early reject, straight from TO_REVIEW -- never reached Victoria.
    _set_status_via_history(conn, "REF-OWNER-REJECTED", "TO_REVIEW", "REJECTED", "Mark")
    conn.commit()

    in_scope_where, in_scope_params = in_scope_filter_sql(conn)
    result = _build_approval_rate(conn, in_scope_where, in_scope_params)
    conn.close()

    assert result["approved"] == 3
    assert result["rejected"] == 1
    assert result["total"] == 4
    assert result["approved_pct"] == 75.0
    assert result["rejected_pct"] == 25.0


def test_approval_rate_by_owner_breaks_down_per_owner(tmp_path):
    """2026-08-10 explicit request: not just one aggregate figure, per
    owner too -- Mark's and Kanvesh's notices must be counted separately,
    each against the same Victoria-sourced-rejection definition."""
    from savvy_scout.dashboard.routes.home import _build_approval_rate_by_owner
    from savvy_scout.dashboard.scope_filter import in_scope_filter_sql

    db_path = str(tmp_path / "test.db")
    conn = get_connection(db_path)
    init_db(conn)
    seed_all(conn)

    now = datetime.now(timezone.utc).isoformat()
    for ref, owner in [
        ("REF-MARK-1", "Mark"), ("REF-MARK-2", "Mark"), ("REF-KANVESH-1", "Kanvesh"),
    ]:
        _insert_notice(conn, ref, now, "Fintech")
        conn.execute("UPDATE notices SET owner = ? WHERE ref = ?", (owner, ref))
    conn.commit()

    _set_status_via_history(conn, "REF-MARK-1", "ESCALATED_TO_VICTORIA", "APPROVED", "Victoria")
    _set_status_via_history(conn, "REF-MARK-2", "ESCALATED_TO_VICTORIA", "REJECTED", "Victoria")
    _set_status_via_history(conn, "REF-KANVESH-1", "ESCALATED_TO_VICTORIA", "APPROVED", "Victoria")
    conn.commit()

    in_scope_where, in_scope_params = in_scope_filter_sql(conn)
    rows = _build_approval_rate_by_owner(conn, in_scope_where, in_scope_params)
    conn.close()

    by_owner = {r["owner"]: r for r in rows}
    assert by_owner["Mark"]["approved"] == 1
    assert by_owner["Mark"]["rejected"] == 1
    assert by_owner["Mark"]["approved_pct"] == 50.0
    assert by_owner["Kanvesh"]["approved"] == 1
    assert by_owner["Kanvesh"]["rejected"] == 0
    assert by_owner["Kanvesh"]["approved_pct"] == 100.0


def test_sector_with_only_a_publish_date_unknown_notice_still_gets_a_row(tmp_path):
    """2026-08-10 finding #5, found live: Fintech's only in-scope notice had
    publish_date_unknown=1 (discovered via an award/update release), and the
    whole "Fintech" row vanished from Sector Performance entirely -- not
    just its date columns -- even though it correctly still counted in
    Sector mix/Total scouted (a separate, date-independent count). A
    sector's row must exist as soon as it has any in-scope notice at all,
    with zeroed date columns for the ones with no confirmed publish date,
    not disappear."""
    from zoneinfo import ZoneInfo

    from savvy_scout.dashboard.routes.home import _build_sector_performance, _build_source_performance

    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)

    now_uk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))

    _insert_notice_with_publish_dates(
        setup_conn, "REF-FINTECH-UNKNOWN",
        first_seen_at=now_uk.isoformat(),
        first_published_at=None,
        published_at=now_uk.isoformat(),
        publish_date_unknown=1,
    )
    setup_conn.commit()

    sector_perf = _build_sector_performance(setup_conn, now_uk)
    source_perf = _build_source_performance(setup_conn, now_uk)
    setup_conn.close()

    fintech_row = next((r for r in sector_perf["rows"] if r["sector"] == "Fintech"), None)
    assert fintech_row is not None, "Fintech's row must exist even though its only notice has no confirmed date"
    assert sum(fintech_row["days"]) == 0
    assert fintech_row["week"] == 0
    assert fintech_row["month"] == 0
    assert fintech_row["ytd"] == 0

    source_row = next((r for r in source_perf["rows"] if r["sector"] == "Find a Tender"), None)
    assert source_row is not None, "the source's row must exist even if every notice has no confirmed date"
    assert sum(source_row["days"]) == 0


def test_award_only_discovery_excluded_from_every_date_bucket(tmp_path):
    """2026-08-10 finding #2: a notice first discovered via an award/
    contract/amendment/termination release (publish_date_unknown=1) has no
    reliable publish date anywhere in that release's payload -- it must be
    excluded from Sector Performance/Notices by Source date buckets
    entirely (not counted under "today" via the published_at/first_seen_at
    fallback, which would repeat finding #1's mistake through a different
    path), while a genuinely new notice published today still counts."""
    from zoneinfo import ZoneInfo

    from savvy_scout.dashboard.routes.home import _build_sector_performance, _build_source_performance

    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)

    now_uk = datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/London"))
    today_start = now_uk.replace(hour=0, minute=0, second=0, microsecond=0)

    # Discovered today via an award-only release: published_at and
    # first_seen_at are both "today", but publish_date_unknown says don't
    # trust either for date-based reporting.
    _insert_notice_with_publish_dates(
        setup_conn, "REF-AWARD-ONLY",
        first_seen_at=today_start.isoformat(),
        first_published_at=None,
        published_at=today_start.isoformat(),
        publish_date_unknown=1,
    )
    # A genuinely new notice, published today for the first time.
    _insert_notice_with_publish_dates(
        setup_conn, "REF-NEW-TODAY",
        first_seen_at=today_start.isoformat(),
        first_published_at=today_start.isoformat(),
        published_at=today_start.isoformat(),
    )
    setup_conn.commit()

    sector_perf = _build_sector_performance(setup_conn, now_uk)
    source_perf = _build_source_performance(setup_conn, now_uk)
    setup_conn.close()

    swept_total_week = next(r for r in sector_perf["rows"] if r["sector"] == "Total Swept (all sources)")["week"]
    source_week = next(r for r in source_perf["rows"] if r["sector"] == "Find a Tender")["week"]
    assert swept_total_week == 1
    assert source_week == 1


def test_overview_shows_cross_feature_tiles(tmp_path):
    """2026-09-05 UI alignment: Renewals due, New signals this week,
    Competitors watched, and Items shortlisted are cheap single-table counts
    surfaced on the Overview so Signals, Competitor Intel, and Shortlists
    each get a one-glance summary without leaving the landing page."""
    db_path = str(tmp_path / "test.db")
    setup_conn = get_connection(db_path)
    init_db(setup_conn)
    seed_all(setup_conn)
    setup_conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("mark", generate_password_hash("testpass"), "Mark", 0, datetime.now(timezone.utc).isoformat()),
    )

    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=(now.weekday() - 5) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)

    # Due within 90 days AND created this week -- counts toward both tiles.
    setup_conn.execute(
        "INSERT INTO contract_expiry (notice_ref, buyer, title, end_date, review_date, source_ref, created_at) "
        "VALUES ('REF-A', 'Buyer', 'Renewal A', ?, ?, 'REF-A', ?)",
        ((now + timedelta(days=30)).isoformat(), (now - timedelta(days=1)).isoformat(), (week_start + timedelta(hours=1)).isoformat()),
    )
    # Beyond the 90-day window and created last month -- must not count
    # towards either tile, proving the query doesn't just count every row.
    setup_conn.execute(
        "INSERT INTO contract_expiry (notice_ref, buyer, title, end_date, review_date, source_ref, created_at) "
        "VALUES ('REF-B', 'Buyer', 'Renewal B', ?, ?, 'REF-B', ?)",
        ((now + timedelta(days=400)).isoformat(), (now + timedelta(days=200)).isoformat(), (now - timedelta(days=40)).isoformat()),
    )
    setup_conn.execute(
        "INSERT INTO watched_competitors (supplier_name, watched_by, watched_at) VALUES ('Acme Ltd', 'Mark', ?)",
        (now.isoformat(),),
    )
    _insert_notice(setup_conn, "REF-SHORTLIST", now.isoformat(), "Fintech")
    shortlisted_notice_id = setup_conn.execute(
        "SELECT id FROM notices WHERE ref = 'REF-SHORTLIST'"
    ).fetchone()["id"]
    setup_conn.execute(
        "INSERT INTO shortlisted_notices (notice_id, added_by, added_at) VALUES (?, 'Mark', ?)",
        (shortlisted_notice_id, now.isoformat()),
    )
    setup_conn.commit()
    setup_conn.close()

    settings = Settings(
        db_path=db_path,
        lookback_days=7,
        find_a_tender_base_url="",
        contracts_finder_base_url="",
        flask_secret_key="test-key",
        ms_graph_tenant_id=None,
        ms_graph_client_id=None,
        ms_graph_client_secret=None,
        ms_graph_sender_upn=None,
    )
    app = create_app(settings)
    app.config["TESTING"] = True
    client = _logged_in_client(app, "mark")

    html = client.get("/").get_data(as_text=True)

    assert re.search(r'<div class="stat-value">1</div>\s*<div class="stat-label">Renewals due \(90d\)</div>', html)
    assert re.search(r'<div class="stat-value">1</div>\s*<div class="stat-label">New signals this week</div>', html)
    assert re.search(r'<div class="stat-value">1</div>\s*<div class="stat-label">Competitors watched</div>', html)
    assert re.search(r'<div class="stat-value">1</div>\s*<div class="stat-label">Items shortlisted</div>', html)
