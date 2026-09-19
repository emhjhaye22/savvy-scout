"""Command-line entry points for Savvy Scout.

    python -m savvy_scout.cli init-db
    python -m savvy_scout.cli sweep
    python -m savvy_scout.cli export --output tracker.xlsx
    python -m savvy_scout.cli regression-test --baseline baseline.xlsx --output diff.xlsx
    python -m savvy_scout.cli backup
    python -m savvy_scout.cli create-user --username mark --display-name Mark
    python -m savvy_scout.cli retriage-unmatched
    python -m savvy_scout.cli dashboard --port 5000
"""

import argparse
import getpass
import sys
from datetime import datetime, timezone

from savvy_scout.config import load_settings
from savvy_scout.db.backup import DEFAULT_BACKUPS_DIR, backup_database
from savvy_scout.db.connection import get_connection, init_db
from savvy_scout.db.seed_config import seed_all
from savvy_scout.export.excel_tracker import export_tracker
from savvy_scout.export.trifork_pipeline import update_trifork_pipeline
from savvy_scout.regression.baseline_compare import run_regression_test
from savvy_scout.reporting.victoria_export import export_victoria_package
from savvy_scout.sweep.runner import run_sweep, triage_pending
from savvy_scout.workflow.approvals import (
    correct_pre_routing_fix_backlog,
    reclassify_phase2_scoped_backlog,
    retriage_all_unmatched,
)

DASHBOARD_DISPLAY_NAMES = {"Mark", "Kanvesh", "Hammad", "Victoria"}


def cmd_init_db(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    init_db(conn)
    seed_all(conn)
    print(f"Database initialised and config seeded at {settings.db_path}")


def cmd_sweep(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    init_db(conn)
    seed_all(conn)
    stats = run_sweep(conn, settings, triggered_by="cli")
    print(
        f"Pulled {stats['pulled']} notices, surfaced {stats['expiring_leads']} expiring-contract "
        f"leads, triaged {stats['triaged']} new notices."
    )


def cmd_triage(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    count = triage_pending(conn)
    print(f"Triaged {count} pending notices.")


def cmd_export(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    path = export_tracker(conn, args.output)
    print(f"Tracker exported to {path}")


def cmd_sync_trifork_tracker(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    result = update_trifork_pipeline(conn, args.output)
    print(
        f"Trifork tracker updated at {result['output_path']}: "
        f"{result['inserted']} inserted, {result['updated']} updated, {result['skipped']} skipped."
    )


def cmd_export_victoria_package(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    result = export_victoria_package(conn, args.output_root, owner=args.owner)
    print(
        f"Victoria package exported to {args.output_root}: "
        f"{result['opportunities_with_artifacts']} opportunities, "
        f"{result['artifacts']} PDFs, {result['tracker']['total']} tracker rows."
    )


def cmd_regression_test(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    result = run_regression_test(conn, args.baseline, args.output)
    print(
        f"Regression test complete: {result['agreements']}/{result['total']} agree, "
        f"{result['disagreements']} disagreement(s). Report: {result['output_path']}"
    )
    if result["disagreements"]:
        sys.exit(1)


def cmd_backup(args: argparse.Namespace) -> None:
    settings = load_settings()
    path = backup_database(settings.db_path, args.backup_dir)
    print(f"Backup written to {path}")


def cmd_create_user(args: argparse.Namespace) -> None:
    from werkzeug.security import generate_password_hash

    if args.display_name not in DASHBOARD_DISPLAY_NAMES:
        print(
            f"Warning: '{args.display_name}' is not one of {sorted(DASHBOARD_DISPLAY_NAMES)}; "
            "the dashboard's admin/Victoria checks match on exact display name."
        )

    settings = load_settings()
    conn = get_connection(settings.db_path)
    init_db(conn)
    password = getpass.getpass(f"Password for {args.username} ({args.display_name}): ")
    if not password:
        print("Aborted: password cannot be empty.")
        return

    # Joins Trifork's own account (2026-09-18 tenancy fix) -- this CLI has
    # no client argument yet, since every current user of the app is
    # Trifork staff.
    trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, is_victoria, created_at, client_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            args.username,
            generate_password_hash(password),
            args.display_name,
            int(args.display_name == "Victoria"),
            datetime.now(timezone.utc).isoformat(),
            trifork_id,
        ),
    )
    conn.commit()
    print(f"Created dashboard user '{args.username}' ({args.display_name}).")


def cmd_retriage_unmatched(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    counts = retriage_all_unmatched(conn)
    print(
        f"Re-checked {counts['checked']} notices with no sector match: "
        f"{counts['now_matched']} now matched a sector, {counts['still_unmatched']} still don't. "
        f"Of the newly matched: {counts['sent_to_phase2']} sent to the Phase 2 scope read queue, "
        f"{counts['monitoring']} moved to MONITORING."
    )


def cmd_correct_backlog(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    counts = correct_pre_routing_fix_backlog(conn)
    print(
        f"Checked {counts['checked']} notices still in TO_REVIEW or "
        f"ESCALATED_TO_VICTORIA: {counts['moved_to_phase2']} moved to PHASE2_SCOPED "
        f"(auto-escalated under the retired routing, no Victoria decision recorded), "
        f"{counts['unchanged']} unchanged."
    )


def cmd_reclassify_backlog(args: argparse.Namespace) -> None:
    settings = load_settings()
    conn = get_connection(settings.db_path)
    counts = reclassify_phase2_scoped_backlog(conn)
    print(
        f"Checked {counts['checked']} notices in PHASE2_SCOPED: {counts['moved_out_of_scope']} "
        f"no longer match a confirmed sector after the Gate 1 coupling fix and moved back to "
        f"TO_REVIEW, {counts['unchanged']} still correctly in scope."
    )


def cmd_dashboard(args: argparse.Namespace) -> None:
    from savvy_scout.dashboard import create_app

    settings = load_settings()
    if not settings.flask_secret_key:
        print(
            "Warning: FLASK_SECRET_KEY is not set in .env; using an insecure development key. "
            "Do not run this way outside local development."
        )
    app = create_app(settings)
    app.run(host=args.host, port=args.port, debug=args.debug)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="savvy-scout")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Create the database and seed config tables").set_defaults(
        func=cmd_init_db
    )

    subparsers.add_parser(
        "sweep", help="Pull new notices from Find a Tender and Contracts Finder, dedupe and triage"
    ).set_defaults(func=cmd_sweep)

    subparsers.add_parser(
        "triage", help="Run Phase 1 triage on any notice still in NEW status, without sweeping"
    ).set_defaults(func=cmd_triage)

    export_parser = subparsers.add_parser("export", help="Export the multi-sheet Excel tracker")
    export_parser.add_argument("--output", default="tracker.xlsx")
    export_parser.set_defaults(func=cmd_export)

    trifork_parser = subparsers.add_parser(
        "sync-trifork-tracker",
        help="Update the formatted Trifork pipeline workbook from genuine owner Phase 2 decisions",
    )
    trifork_parser.add_argument("--output", required=True)
    trifork_parser.set_defaults(func=cmd_sync_trifork_tracker)

    victoria_parser = subparsers.add_parser(
        "export-victoria-package",
        help="Generate the complete local Victoria reports package",
    )
    victoria_parser.add_argument("--output-root", required=True)
    victoria_parser.add_argument(
        "--owner", default=None,
        help="Restrict the package to one owner's sectors only (e.g. Mark). Omit for all owners.",
    )
    victoria_parser.set_defaults(func=cmd_export_victoria_package)

    regression_parser = subparsers.add_parser(
        "regression-test", help="Compare machine outcomes against a baseline tracker Excel file"
    )
    regression_parser.add_argument("--baseline", required=True)
    regression_parser.add_argument("--output", default="regression_diff.xlsx")
    regression_parser.set_defaults(func=cmd_regression_test)

    backup_parser = subparsers.add_parser("backup", help="Back up the SQLite database file")
    backup_parser.add_argument("--backup-dir", default=DEFAULT_BACKUPS_DIR)
    backup_parser.set_defaults(func=cmd_backup)

    create_user_parser = subparsers.add_parser(
        "create-user", help="Create a dashboard login (Mark, Kanvesh, Hammad or Victoria)"
    )
    create_user_parser.add_argument("--username", required=True)
    create_user_parser.add_argument("--display-name", required=True)
    create_user_parser.set_defaults(func=cmd_create_user)

    subparsers.add_parser(
        "retriage-unmatched",
        help="Re-check every notice with no sector match against the current keyword lists, "
        "e.g. after a config correction. Only touches notices no human has acted on yet.",
    ).set_defaults(func=cmd_retriage_unmatched)

    subparsers.add_parser(
        "correct-backlog",
        help="One-time correction: move notices auto-escalated by the retired "
        "auto_escalate_if_flagged routing (no Victoria decision recorded) into PHASE2_SCOPED",
    ).set_defaults(func=cmd_correct_backlog)

    subparsers.add_parser(
        "reclassify-backlog",
        help="One-time correction: re-check Gate 1 sector classification for every notice "
        "still in PHASE2_SCOPED after the bare-keyword coupling fix",
    ).set_defaults(func=cmd_reclassify_backlog)

    dashboard_parser = subparsers.add_parser("dashboard", help="Run the Flask approval dashboard")
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=int, default=5000)
    dashboard_parser.add_argument("--debug", action="store_true")
    dashboard_parser.set_defaults(func=cmd_dashboard)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
