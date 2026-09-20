"""Background jobs for a production deploy: the daily sweep and a daily
backup, run in-process inside the web service.

Why in-process rather than a separate Render Cron Job: the sweep and the
web dashboard both need the SAME SQLite file, and Render persistent disks
can only be attached to one service at a time -- a separate Cron Job
service couldn't share the web service's disk. Running it on a timer
inside the same process (same pattern as the sibling app, see
new-app/scheduler.py) sidesteps that entirely, at the cost of needing
start_scheduler() called exactly once per running process -- see wsgi.py.

Locally, sweeping still runs via `python -m savvy_scout.cli sweep`
(e.g. from Windows Task Scheduler); this only matters for the deployed
copy, which has no OS-level scheduler to lean on.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from savvy_scout.config import load_settings
from savvy_scout.db.backup import backup_database
from savvy_scout.db.connection import get_approver_email, get_connection, init_db
from savvy_scout.db.seed_config import seed_all
from savvy_scout.graph.drive import upload_file
from savvy_scout.notifications import (
    APPROACHING_DAYS,
    NotificationError,
    send_email_with_attachment,
    send_victoria_reminder_digest_email,
)
from savvy_scout.reporting.reports import generate_monthly_report, generate_weekly_report, most_recent_monday
from savvy_scout.sweep.runner import run_sweep

logger = logging.getLogger(__name__)

# Reports are prepared for Trifork's leadership team (UK-based), so their
# schedule runs on UK time rather than the team's own Manila time above.
LONDON = ZoneInfo("Europe/London")

# 2026-08-10: the team works out of the Philippines -- 8am Manila time was
# chosen over an earlier 6am proposal specifically because it falls just
# after UK midnight (UK is 7-8 hours behind PH depending on BST/GMT), so the
# full previous UK calendar day's notices are already published by the time
# this runs. 6am PH would land an hour BEFORE UK midnight instead, risking
# missing anything published in the UK's last hour of that day. Explicit
# tzinfo rather than relying on the container's local time (previously
# unset, so hour=6 silently meant 6am UTC/7am BST -- not the "8am UK time"
# the Overview's own display text claimed, see home.py's sweep_next_run).
MANILA = ZoneInfo("Asia/Manila")


def run_daily_sweep() -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    try:
        init_db(conn)
        seed_all(conn)
        stats = run_sweep(conn, settings, triggered_by="scheduler")
        logger.info(
            "Scheduled sweep: pulled %s notices, %s expiring leads, %s triaged",
            stats["pulled"], stats["expiring_leads"], stats["triaged"],
        )
    finally:
        conn.close()


def run_daily_backup() -> None:
    settings = load_settings()
    try:
        path = backup_database(settings.db_path)
        logger.info("Scheduled backup written to %s", path)
    except FileNotFoundError:
        logger.warning("Scheduled backup skipped: no database file yet at %s", settings.db_path)
        return

    # Off-box copy (2026-09-17 audit finding): the local backup above still
    # lives on the same disk as the database it protects. Uploaded via the
    # same Azure AD app registration already used for escalation email --
    # needs Files.ReadWrite.All added to that app's application permissions
    # in Azure Portal (an external setup step; see graph/drive.py's
    # docstring). Failure here is logged, never fatal: the local backup
    # above already succeeded, and a Graph outage shouldn't be treated as a
    # reason to consider today's backup missing.
    if settings.graph_configured:
        try:
            folder = os.environ.get("MS_GRAPH_BACKUP_FOLDER", "TenderSight-Backups")
            url = upload_file(
                path, folder, settings.ms_graph_sender_upn,
                settings.ms_graph_tenant_id, settings.ms_graph_client_id, settings.ms_graph_client_secret,
            )
            logger.info("Backup uploaded to OneDrive: %s", url)
        except Exception:
            logger.exception("OneDrive backup upload failed; local backup at %s is still intact", path)
    else:
        logger.info("OneDrive backup skipped: Microsoft Graph is not configured (see .env.example)")


def _report_recipients(conn, settings) -> list[str]:
    """Explicit request (2026-08-21): Trifork's Account Approver was never
    on the Weekly/Monthly report distribution, only whoever
    REPORT_RECIPIENT_EMAIL points at (the Admin). She gets every individual
    escalation email already (_notify_victoria_of_escalation), but not this
    digest-level report."""
    recipients = []
    if settings.report_recipient_email:
        recipients.append(settings.report_recipient_email)
    trifork = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()
    approver_email = get_approver_email(conn, trifork["id"]) if trifork else None
    if approver_email:
        recipients.append(approver_email)
    return list(dict.fromkeys(recipients))


def _email_report(conn, path: str, subject: str) -> None:
    settings = load_settings()
    recipients = _report_recipients(conn, settings)
    if not recipients:
        logger.warning("Report generated at %s but no recipients configured; not emailed.", path)
        return
    for recipient in recipients:
        try:
            send_email_with_attachment(
                recipient,
                subject,
                "Attached: the latest auto-generated Trifork Scouting report from TenderSight™.",
                path,
            )
            logger.info("Emailed %s to %s", path, recipient)
        except NotificationError:
            logger.exception("Failed to email report %s to %s", path, recipient)


def run_weekly_report_job() -> None:
    """SPEC.md C6 Friday EOW report -- covers the current week (Monday
    through today) so Friday afternoon's send reflects the whole working
    week, not last week's."""
    settings = load_settings()
    conn = get_connection(settings.db_path)
    try:
        week_start = most_recent_monday()
        path = generate_weekly_report(conn, week_start, settings.reports_output_dir)
        _email_report(conn, path, f"Trifork Scouting Weekly Report {week_start.isoformat()}")
    finally:
        conn.close()


def run_monthly_report_job() -> None:
    """Runs on the 1st of the month, so it reports the month that just
    finished rather than the (nearly empty) one just starting."""
    settings = load_settings()
    conn = get_connection(settings.db_path)
    try:
        today = date.today()
        if today.month == 1:
            month_start = date(today.year - 1, 12, 1)
        else:
            month_start = date(today.year, today.month - 1, 1)
        path = generate_monthly_report(conn, month_start, settings.reports_output_dir)
        _email_report(conn, path, f"Trifork Scouting Monthly Report {month_start.strftime('%Y-%m')}")
    finally:
        conn.close()


def _pending_victoria_escalations(conn) -> list:
    rows = conn.execute(
        """
        SELECT n.id, n.ref, n.title, n.buyer, n.owner, n.deadline, n.indicative_value,
               p.overall_rating, p.overall_reasoning,
               p.capability_fit_rating, p.capability_fit_reasoning
        FROM notices n
        LEFT JOIN phase2_assessments p ON p.id = (
            SELECT id FROM phase2_assessments WHERE notice_id = n.id ORDER BY id DESC LIMIT 1
        )
        WHERE n.status = 'ESCALATED_TO_VICTORIA'
        """
    ).fetchall()
    escalated_at_by_id = {
        row["notice_id"]: row["changed_at"]
        for row in conn.execute(
            "SELECT notice_id, MAX(changed_at) AS changed_at FROM status_history "
            "WHERE to_status = 'ESCALATED_TO_VICTORIA' GROUP BY notice_id"
        ).fetchall()
    }
    return rows, escalated_at_by_id


def run_victoria_reminder_job() -> None:
    """Explicit request (2026-08-21): a daily digest so an escalation
    doesn't just sit awaiting Victoria's decision until she happens to
    reopen the app -- split into deadline-driven urgency and a distinct
    "this one's genuinely strong, don't let it lapse" reason, since the two
    call for different levels of attention. Skips sending entirely when
    nothing currently qualifies, so this stays a signal, not daily noise."""
    settings = load_settings()
    conn = get_connection(settings.db_path)
    try:
        trifork = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()
        approver_email = get_approver_email(conn, trifork["id"]) if trifork else None
        if not approver_email:
            logger.debug("No email on file for the Account Approver; skipping reminder digest.")
            return

        rows, escalated_at_by_id = _pending_victoria_escalations(conn)
        app_url = (os.environ.get("SAVVY_SCOUT_APP_BASE_URL") or "").rstrip("/")
        now = datetime.now(timezone.utc)
        urgent, high_value = [], []
        for row in rows:
            item = {
                "notice_id": row["id"], "ref": row["ref"], "title": row["title"], "buyer": row["buyer"],
                "owner": row["owner"], "deadline": row["deadline"], "value": row["indicative_value"],
                "escalated_at": escalated_at_by_id.get(row["id"]),
            }
            days_left = None
            if row["deadline"]:
                try:
                    deadline_dt = datetime.fromisoformat(row["deadline"])
                    if deadline_dt.tzinfo is None:
                        deadline_dt = deadline_dt.replace(tzinfo=timezone.utc)
                    days_left = (deadline_dt - now).days
                except ValueError:
                    days_left = None
            is_urgent = days_left is not None and 0 <= days_left <= APPROACHING_DAYS
            is_high_value = row["overall_rating"] == "PURSUE" or (
                row["overall_rating"] == "FLAG" and row["capability_fit_rating"] == "HIGH"
            )
            if is_urgent:
                item["why"] = f"{days_left} day(s) left before the deadline."
                urgent.append(item)
            elif is_high_value:
                reason = row["overall_reasoning"] or row["capability_fit_reasoning"] or "Strong capability fit."
                item["why"] = f"{row['overall_rating']} overall, {row['capability_fit_rating']} capability fit. {reason}"
                high_value.append(item)

        if not urgent and not high_value:
            logger.debug("No outstanding Victoria escalations qualify for a reminder today.")
            return
        try:
            send_victoria_reminder_digest_email(approver_email, urgent, high_value, app_url)
            logger.info("Sent Victoria reminder digest: %d urgent, %d high-value", len(urgent), len(high_value))
        except NotificationError:
            logger.exception("Failed to send Victoria reminder digest")
    finally:
        conn.close()


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_daily_sweep, "cron", hour=8, minute=0, timezone=MANILA, id="daily_sweep")
    scheduler.add_job(run_daily_backup, "cron", hour=5, minute=45, id="daily_backup")
    scheduler.add_job(
        run_weekly_report_job, "cron", day_of_week="fri", hour=16, minute=0, timezone=LONDON, id="weekly_report"
    )
    scheduler.add_job(
        run_monthly_report_job, "cron", day=1, hour=8, minute=0, timezone=LONDON, id="monthly_report"
    )
    scheduler.add_job(
        run_victoria_reminder_job, "cron", day_of_week="mon-fri", hour=8, minute=30,
        timezone=LONDON, id="victoria_reminder_digest",
    )
    scheduler.start()
    return scheduler
