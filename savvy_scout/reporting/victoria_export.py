import sqlite3
import re
from datetime import date
from pathlib import Path

from savvy_scout.escalation.brief import (
    build_original_notice_pdf,
    record_brief,
)
from savvy_scout.escalation.context import owner_display_names
from savvy_scout.escalation.word_documents import (
    build_capture_brief_docx,
    build_internal_addendum_docx,
)
from savvy_scout.export.trifork_pipeline import update_trifork_pipeline
from savvy_scout.logging_util import log_audit
from savvy_scout.reporting.reports import generate_monthly_report, generate_weekly_report, most_recent_monday


def _owner_escalated_notice_ids(conn: sqlite3.Connection, owner: str | None = None) -> list[int]:
    # owner, if given, restricts the Addendum/Brief package to that owner's
    # own escalations only -- explicit request (2026-08-16): one owner's
    # package must not include another owner's opportunities.
    names = (owner,) if owner else owner_display_names(conn)
    placeholders = ",".join("?" for _ in names)
    rows = conn.execute(
        f"SELECT notice_id, MAX(id) AS latest_id FROM status_history "
        f"WHERE from_status = 'AWAITING_PHASE2_APPROVAL' "
        f"AND to_status = 'ESCALATED_TO_VICTORIA' "
        f"AND changed_by IN ({placeholders}) "
        f"AND EXISTS (SELECT 1 FROM phase2_assessments p WHERE p.notice_id = status_history.notice_id) "
        f"GROUP BY notice_id ORDER BY latest_id",
        names,
    ).fetchall()
    return [row["notice_id"] for row in rows]


def _safe_title(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "-", value).strip(" .") or "Untitled Opportunity"


def _file_stem(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "Untitled_Opportunity"


def _move_generated(source_path: str, destination: Path) -> str:
    source = Path(source_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        source.replace(destination)
    return str(destination)


def export_victoria_package(
    conn: sqlite3.Connection, output_root: str, reference_date: date | None = None,
    owner: str | None = None,
) -> dict:
    """owner, if given (e.g. "Mark"), scopes the whole package -- artifacts,
    tracker, and both reports -- to that owner's own sectors only. Explicit
    request (2026-08-16): Mark's package must never include Kanvesh's or
    Hammad's opportunities."""
    root = Path(output_root)
    artifacts_root = root / "Addendum and Brief per Phase 2 Pass & Flag Opportunities"
    tracker_dir = root / "Pipeline Tracker"
    weekly_dir = root / "Weekly Report"
    monthly_dir = root / "Monthly Report"
    for directory in (artifacts_root, tracker_dir, weekly_dir, monthly_dir):
        directory.mkdir(parents=True, exist_ok=True)

    artifact_count = 0
    for index, notice_id in enumerate(_owner_escalated_notice_ids(conn, owner), start=1):
        notice = conn.execute("SELECT ref, title FROM notices WHERE id = ?", (notice_id,)).fetchone()
        if not notice:
            continue
        safe_title = _safe_title(notice["title"])
        stem = _file_stem(notice["title"])
        notice_dir = artifacts_root / f"{index:02d}. {safe_title}"
        reason = "owner_phase2_approved_victoria_package"
        addendum_path = _move_generated(
            build_internal_addendum_docx(conn, notice_id, str(notice_dir)),
            notice_dir / f"{stem}_Internal_Addendum.docx",
        )
        capture_path = _move_generated(
            build_capture_brief_docx(conn, notice_id, str(notice_dir)),
            notice_dir / f"{stem}_Capture_Brief.docx",
        )
        original_path = _move_generated(
            build_original_notice_pdf(conn, notice_id, str(notice_dir)),
            notice_dir / f"{safe_title}.pdf",
        )
        paths = (
            ("INTERNAL_ADDENDUM", addendum_path),
            ("CAPTURE_BRIEF", capture_path),
            ("ORIGINAL_NOTICE", original_path),
        )
        for brief_type, path in paths:
            record_brief(conn, notice_id, reason, path, "victoria_package_export", brief_type)
            log_audit(
                conn, "notice", str(notice_id), "artifact_generated",
                "victoria_package_export", reason,
                {"brief_type": brief_type, "path": path},
            )
            artifact_count += 1

    today = reference_date or date.today()
    tracker = update_trifork_pipeline(
        conn, str(tracker_dir / f"My_Trifork_Pipeline_Tracker - Updated {today.isoformat()}.xlsx"), owner,
    )
    week_start = most_recent_monday(today)
    weekly_path = generate_weekly_report(conn, week_start, str(weekly_dir), owner=owner)
    # Previous complete month, matching run_monthly_report_job's own logic
    # (2026-08-16 fix): the current calendar month isn't finished yet, so a
    # report for it is premature -- confirmed live, this produced a "Monthly
    # Report 2026-08" on 16 August, days before the month even ended.
    if today.month == 1:
        month_start = date(today.year - 1, 12, 1)
    else:
        month_start = date(today.year, today.month - 1, 1)
    monthly_path = generate_monthly_report(conn, month_start, str(monthly_dir), owner=owner)
    return {
        "opportunities_with_artifacts": artifact_count // 3,
        "artifacts": artifact_count,
        "tracker": tracker,
        "weekly_report": weekly_path,
        "monthly_report": monthly_path,
    }