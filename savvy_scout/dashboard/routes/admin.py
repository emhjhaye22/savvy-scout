"""Admin tab: config table editing + the B4 learning loop's rule-correction
log. Restricted to Victoria and Mark, the sole scouting desk since Kanvesh
and Hammad were consolidated out on 2026-09-01. A bare-bones version now;
SPEC.md C5 (source tier management, email whitelist) completes it later."""

import json
import secrets
from datetime import datetime, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.security import generate_password_hash

from savvy_scout.dashboard.auth import get_db
from savvy_scout.db.connection import unique_client_slug
from savvy_scout.logging_util import log_audit
from savvy_scout.triage.client_filter import (
    client_filter_is_empty,
    get_client_match_status_counts,
    get_client_matches,
    record_client_notice_status,
    run_client_triage,
)
from savvy_scout.workflow.approvals import bring_back_escalated_for_gate_retriage
from savvy_scout.notifications import NotificationError, send_account_invite_email

admin_bp = Blueprint("admin", __name__)

EDITABLE_TABLES = [
    "config_owner_map",
    "config_sector_keywords",
    "config_gate2_terms",
    "config_coupling_terms",
    "config_exclusion_terms",
    "config_framework_keywords",
    "config_trifork_frameworks",
    "config_cpv_lists",
    "config_sector_cpv_scope",
    "config_scale_filter",
    "config_capability_profile",
    "config_sources",
]

# Groups the flat EDITABLE_TABLES list into related sections for the admin
# page's nav + layout: (anchor slug, section label, table names in it).
TABLE_GROUPS = [
    ("sectors", "Sectors & Owners", ["config_owner_map", "config_sector_keywords", "config_exclusion_terms"]),
    ("gate2", "Type of Work (Gate 2)", ["config_gate2_terms", "config_coupling_terms"]),
    ("frameworks", "Framework Rules", ["config_framework_keywords", "config_trifork_frameworks"]),
    ("cpv", "CPV & Scale", ["config_cpv_lists", "config_sector_cpv_scope", "config_scale_filter"]),
    ("capability", "Capability Profile", ["config_capability_profile"]),
    ("sources", "Sweep Sources", ["config_sources"]),
]

# Columns the app manages itself (autoincrement PK, or audit timestamps/actor
# stamped server-side) -- never rendered as editable inputs, never taken from
# submitted form data.
AUTO_MANAGED_COLUMNS = {"id", "updated_at", "updated_by", "created_at"}


def _has_correction_authority() -> bool:
    """Rule-correction authority: Admin or Account Approver (2026-09-20,
    generalized from the literal "Victoria or Mark" check -- see
    _is_super_admin below for why these stay two separate checks rather
    than one combined role)."""
    return current_user.is_admin or current_user.is_account_approver


def _is_super_admin() -> bool:
    """Account-management authority: deliberately Admin only, separate from
    Account Approver's rule-correction authority above (2026-08-08,
    explicit request -- the two roles are not the same person)."""
    return bool(current_user.is_admin)


def _table_schema(conn, table_name: str) -> list[dict]:
    """User-editable columns for a table: name, whether it's NOT NULL with
    no default (so a blank submission must be rejected, not silently
    inserted as an empty string)."""
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [
        {
            "name": row["name"],
            "required": bool(row["notnull"]) and row["dflt_value"] is None,
        }
        for row in rows
        if row["name"] not in AUTO_MANAGED_COLUMNS
    ]


def _record_correction(conn, table_name: str, description: str, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO rule_corrections (entered_by, entered_at, table_affected, description, reason, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (current_user.display_name, now, table_name, description, reason, None),
    )
    conn.commit()
    log_audit(conn, "config", table_name, "settings_change", current_user.display_name, reason)


def _build_admin_context(conn, has_correction, is_admin) -> dict:
    """Every piece of context admin.html needs to render, for a given pair
    of authorities. Extracted (2026-09-20) so a failed add_row/update_row/
    add_client/update_client_filter submission can re-render the full page
    in place with the offending values still filled in, instead of
    redirecting to a fresh GET of index() and discarding everything the
    user just typed -- previously the only thing these four handlers ever
    computed for themselves was the one table/client they were posting to,
    not the ~8-query page context index() builds, so a redirect was the
    only viable failure path they had."""
    # Skip querying every config table entirely for an is_admin-only visitor
    # (Mark, with no correction authority) -- those sections aren't rendered
    # for them at all, so fetching every row of every config table on each
    # load was pure wasted work.
    if has_correction:
        tables = {name: conn.execute(f"SELECT * FROM {name}").fetchall() for name in EDITABLE_TABLES}
        editable_columns = {name: _table_schema(conn, name) for name in EDITABLE_TABLES}
        corrections = conn.execute(
            "SELECT * FROM rule_corrections ORDER BY id DESC LIMIT 50"
        ).fetchall()
    else:
        tables, editable_columns, corrections = {}, {}, []
    users = conn.execute(
        "SELECT u.*, c.name AS client_name FROM users u LEFT JOIN clients c ON c.id = u.client_id "
        "ORDER BY u.display_name"
    ).fetchall() if is_admin else []
    # Every active client, Trifork first, for the "Add a teammate" client
    # picker (2026-09-19) -- separate from the Trifork-excluded `clients`
    # list below, which is the client-onboarding/filter-management section.
    all_clients = (
        conn.execute("SELECT * FROM clients WHERE is_active = 1 ORDER BY (name != 'Trifork'), name").fetchall()
        if is_admin else []
    )
    # Clients (2026-09-06): multi-client onboarding, is_admin-only (Mark),
    # deliberately not has_correction_authority -- Victoria's remit is
    # Trifork's own rule-correction, not which clients exist on the
    # platform. Trifork itself has a row (seeded) but no filter to edit
    # here; its triage is gates.py, untouched by this section.
    clients = []
    if is_admin:
        client_rows = conn.execute("SELECT * FROM clients WHERE name != 'Trifork' ORDER BY name").fetchall()
        for c in client_rows:
            filter_row = conn.execute(
                "SELECT * FROM client_filters WHERE client_id = ?", (c["id"],)
            ).fetchone()
            match_count = conn.execute(
                "SELECT COUNT(*) FROM client_triage_results WHERE client_id = ? AND outcome = 'PASS'",
                (c["id"],),
            ).fetchone()[0]
            clients.append({"client": c, "filter": filter_row, "match_count": match_count})
    # Sectors & Owners row (2026-08-09): the owner picker needs every
    # existing user's name regardless of is_admin -- assigning an *existing*
    # person as a sector's owner is a correction-authority action, only
    # *creating a brand new* person inline is account-management (gated by
    # can_create_users below).
    owner_choices = (
        [row["display_name"] for row in conn.execute("SELECT display_name FROM users ORDER BY display_name").fetchall()]
        if has_correction else []
    )
    # Contact fields (email/Teams webhook/Bid Director) shown inline on every
    # Sectors & Owners row (2026-08-09), keyed by display_name, so editing an
    # existing owner's contact info no longer requires a separate trip to
    # Manage Users -- one save updates the sector assignment and the
    # person's account together.
    owner_contacts = (
        {
            row["display_name"]: {
                "email": row["email"] or "",
                "teams_webhook_url": row["teams_webhook_url"] or "",
                "role": row["role"],
            }
            for row in conn.execute("SELECT display_name, email, teams_webhook_url, role FROM users").fetchall()
        }
        if has_correction else {}
    )
    return {
        "tables": tables,
        "editable_columns": editable_columns,
        "corrections": corrections,
        "groups": TABLE_GROUPS,
        "users": users,
        "has_correction_authority": has_correction,
        "is_super_admin": is_admin,
        "owner_choices": owner_choices,
        "owner_contacts": owner_contacts,
        "can_create_users": is_admin,
        "clients": clients,
        "all_clients": all_clients,
    }


@admin_bp.route("/")
@login_required
def index():
    # Merged page (2026-08-09): Config & Rules and Manage Users used to be
    # two separate pages gated by two separate authorities, which read as
    # "nothing changed" to anyone who only ever looked at the one they had
    # access to. Now anyone with either authority can land on this one page;
    # each section below still only renders/acts for the authority that
    # actually owns it -- Sectors & Rules for Victoria/Mark, Manage Users
    # for Mark (is_admin) -- so the underlying permission split is unchanged,
    # just physically co-located.
    if not (_has_correction_authority() or _is_super_admin()):
        flash("Only Victoria or the admin account can access this page.", "error")
        return redirect(url_for("queues.index"))
    conn = get_db()
    context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
    return render_template("admin.html", **context)


def _row_form_error(table_name: str, row_id: int | None = None) -> dict:
    """What admin.html needs to redisplay a specific row's just-submitted
    (but rejected) values in place, instead of the row reverting to its
    last-saved values on a redirect (2026-09-20) -- e.g. forgetting the
    required reason no longer wipes every other field on that row.
    row_id=None means this is the "add a new row" form for table_name,
    not an edit of an existing row."""
    return {
        "table_name": table_name,
        "row_id": row_id,
        # "fields", not "values" -- a plain dict already has a built-in
        # .values() method, which Jinja's dot-attribute lookup resolves to
        # BEFORE falling back to item access, silently shadowing a
        # same-named dict key instead of raising.
        "fields": {k: v for k, v in request.form.items() if k != "csrf_token"},
    }


@admin_bp.route("/config/<table_name>/<int:row_id>/update", methods=["POST"])
@login_required
def update_row(table_name, row_id):
    if not _has_correction_authority():
        flash("Only Victoria or Mark can make rule corrections.", "error")
        return redirect(url_for("queues.index"))
    if table_name not in EDITABLE_TABLES:
        flash("Unknown config table.", "error")
        return redirect(url_for("admin.index"))

    conn = get_db()

    reason = request.form.get("reason", "")
    if not reason.strip():
        flash("A reason is required for every rule correction.", "error")
        context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
        return render_template("admin.html", **context, row_form_error=_row_form_error(table_name, row_id))

    editable_names = {col["name"] for col in _table_schema(conn, table_name)}
    columns = [c for c in request.form if c != "reason" and c in editable_names]
    # A sector's name is its identity everywhere else (config_sector_keywords,
    # config_sector_cpv_scope, notices.sector, and the ownership-transfer
    # match below) -- renaming it here in place would silently desync all of
    # those instead of actually renaming a sector, so it's locked to
    # add/delete only, never an in-place edit (2026-08-09, enforced
    # server-side too since the read-only rendering in admin.html is just UI).
    if table_name == "config_owner_map" and "sector" in columns:
        columns.remove("sector")
    if not columns:
        flash("No recognised fields submitted.", "error")
        context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
        return render_template("admin.html", **context, row_form_error=_row_form_error(table_name, row_id))

    # Ownership transfer (2026-08-09): the person owning a sector may change,
    # but its existing notices shouldn't silently strand under the old
    # owner's name -- capture the sector/old owner before the update so they
    # can be reassigned to whoever the sector's new owner is, in one action.
    previous_row = None
    if table_name == "config_owner_map" and "owner" in columns:
        previous_row = conn.execute(
            "SELECT sector, owner FROM config_owner_map WHERE id = ?", (row_id,)
        ).fetchone()

    all_column_names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    set_clause_parts = [f"{c} = ?" for c in columns]
    values = [request.form[c] for c in columns]
    if "updated_at" in all_column_names:
        set_clause_parts.append("updated_at = ?")
        values.append(datetime.now(timezone.utc).isoformat())
    if "updated_by" in all_column_names:
        set_clause_parts.append("updated_by = ?")
        values.append(current_user.display_name)
    conn.execute(
        f"UPDATE {table_name} SET {', '.join(set_clause_parts)} WHERE id = ?", (*values, row_id)
    )
    conn.commit()

    transferred = 0
    if previous_row and previous_row["owner"]:
        new_owner = request.form["owner"].strip()
        old_owner = previous_row["owner"]
        if new_owner and new_owner != old_owner:
            cursor = conn.execute(
                "UPDATE notices SET owner = ? WHERE sector = ? AND owner = ?",
                (new_owner, previous_row["sector"], old_owner),
            )
            conn.commit()
            transferred = cursor.rowcount
            if transferred:
                log_audit(
                    conn, "notices", previous_row["sector"], "owner_transferred",
                    current_user.display_name,
                    f"Reassigned {transferred} notice(s) in {previous_row['sector']} from {old_owner} to {new_owner}",
                )

    _record_correction(conn, table_name, f"Updated row {row_id}, fields: {', '.join(columns)}", reason)
    if transferred:
        flash(f"Rule correction saved. {transferred} existing notice(s) transferred to the new owner.")
    else:
        flash("Rule correction saved.")
    return redirect(url_for("admin.index"))


@admin_bp.route("/config/owner-map/<int:row_id>/assign-owner", methods=["POST"])
@login_required
def assign_owner(row_id):
    """Sectors & Owners' single combined action (2026-08-09): pick an
    existing person as a sector's owner, or -- in the same save -- type a
    brand new person's name/email/Teams webhook to both create their account
    (sending them the invite email/temp password, same as Manage Users'
    "Add a teammate") and assign them as owner, instead of those being two
    separate screens/actions. Reassigning to an existing owner is a
    correction-authority action; typing a genuinely new person is account-
    management authority, since it creates a login."""
    if not (_has_correction_authority() or _is_super_admin()):
        flash("Only Victoria or the admin account can assign sector owners.", "error")
        return redirect(url_for("admin.index") + "#group-sectors")

    reason = request.form.get("reason", "")
    if not reason.strip():
        flash("A reason is required for every rule correction.", "error")
        return redirect(url_for("admin.index") + "#group-sectors")

    conn = get_db()
    previous_row = conn.execute(
        "SELECT sector, owner FROM config_owner_map WHERE id = ?", (row_id,)
    ).fetchone()
    if previous_row is None:
        flash("Sector not found.", "error")
        return redirect(url_for("admin.index") + "#group-sectors")

    owner_choice = request.form.get("owner", "").strip()
    notes = request.form.get("notes", "").strip()
    # Same field names whether picking an existing owner or typing a new one
    # -- the template only needs one set of contact inputs, pre-filled by JS
    # per selection, rather than two separate sets for the new/existing cases.
    owner_email = request.form.get("owner_email", "").strip().lower()
    owner_teams = request.form.get("owner_teams_webhook_url", "").strip()
    # Trifork-internal only (no tenant-client equivalent of a sector owner),
    # so no "admin" option here -- just the two roles the account can hold.
    owner_role = request.form.get("owner_role", "account_user")
    if owner_role not in ("account_approver", "account_user"):
        owner_role = "account_user"
    if owner_email and "@" not in owner_email:
        flash(f"'{owner_email}' doesn't look like a valid email address.", "error")
        return redirect(url_for("admin.index") + "#group-sectors")

    if owner_choice == "__new__":
        if not _is_super_admin():
            flash("Only the admin account can create a new teammate.", "error")
            return redirect(url_for("admin.index") + "#group-sectors")
        new_name = request.form.get("new_display_name", "").strip()
        if not new_name or not owner_email:
            flash("A new owner needs at least a display name and email.", "error")
            return redirect(url_for("admin.index") + "#group-sectors")
        username = owner_email.split("@", 1)[0]
        existing = conn.execute(
            "SELECT 1 FROM users WHERE email = ? OR username = ? OR display_name = ?",
            (owner_email, username, new_name),
        ).fetchone()
        if existing:
            flash("A user with that email, username, or display name already exists.", "error")
            return redirect(url_for("admin.index") + "#group-sectors")
        temp_password, (message, category) = _invite_or_reset(owner_email, new_name, username)
        # Joins Trifork's own account (2026-09-18 tenancy fix) -- see
        # add_user()'s identical comment above.
        trifork_id = conn.execute("SELECT id FROM clients WHERE name = 'Trifork'").fetchone()["id"]
        conn.execute(
            "INSERT INTO users (username, password_hash, display_name, email, teams_webhook_url, role, created_at, client_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                username, generate_password_hash(temp_password), new_name, owner_email,
                owner_teams or None, owner_role, datetime.now(timezone.utc).isoformat(), trifork_id,
            ),
        )
        conn.commit()
        log_audit(
            conn, "user", owner_email, "account_created", current_user.display_name,
            f"Added {new_name} ({owner_email}) via sector owner assignment",
        )
        new_owner_name = new_name
        flash(message, category)
    else:
        if not owner_choice:
            flash("Pick an owner, or add a new one.", "error")
            return redirect(url_for("admin.index") + "#group-sectors")
        new_owner_name = owner_choice
        # Contact info (2026-08-09): editable inline for an *existing* owner
        # too, not just when creating a new one -- one save updates their
        # email/Teams webhook/role together with the sector assignment,
        # instead of a separate trip to Manage Users.
        existing_user = conn.execute(
            "SELECT id, role FROM users WHERE display_name = ?", (new_owner_name,)
        ).fetchone()
        if existing_user:
            # This form only ever offers account_approver/account_user (no
            # admin option, see owner_role above) -- if the person picked as
            # a sector owner happens to already be the Admin, don't demote
            # them just because this unrelated field defaulted to
            # account_user; leave admin status untouched either way.
            new_role = existing_user["role"] if existing_user["role"] == "admin" else owner_role
            conn.execute(
                "UPDATE users SET email = ?, teams_webhook_url = ?, role = ? WHERE id = ?",
                (owner_email or None, owner_teams or None, new_role, existing_user["id"]),
            )
            conn.commit()

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE config_owner_map SET owner = ?, notes = ?, updated_at = ?, updated_by = ? WHERE id = ?",
        (new_owner_name, notes or None, now, current_user.display_name, row_id),
    )
    conn.commit()

    transferred = 0
    old_owner = previous_row["owner"]
    if old_owner and new_owner_name != old_owner:
        cursor = conn.execute(
            "UPDATE notices SET owner = ? WHERE sector = ? AND owner = ?",
            (new_owner_name, previous_row["sector"], old_owner),
        )
        conn.commit()
        transferred = cursor.rowcount
        if transferred:
            log_audit(
                conn, "notices", previous_row["sector"], "owner_transferred",
                current_user.display_name,
                f"Reassigned {transferred} notice(s) in {previous_row['sector']} from {old_owner} to {new_owner_name}",
            )

    _record_correction(conn, "config_owner_map", f"Set {previous_row['sector']} owner to {new_owner_name}", reason)
    if transferred:
        flash(f"Owner updated. {transferred} existing notice(s) transferred to {new_owner_name}.")
    else:
        flash("Owner updated.")
    return redirect(url_for("admin.index") + "#group-sectors")


@admin_bp.route("/config/<table_name>/add", methods=["POST"])
@login_required
def add_row(table_name):
    if not _has_correction_authority():
        flash("Only Victoria or Mark can make rule corrections.", "error")
        return redirect(url_for("queues.index"))
    if table_name not in EDITABLE_TABLES:
        flash("Unknown config table.", "error")
        return redirect(url_for("admin.index"))

    conn = get_db()

    reason = request.form.get("reason", "")
    if not reason.strip():
        flash("A reason is required for every rule correction.", "error")
        context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
        return render_template("admin.html", **context, row_form_error=_row_form_error(table_name))

    schema = _table_schema(conn, table_name)

    values_by_column = {}
    missing_required = []
    for col in schema:
        value = request.form.get(col["name"], "").strip()
        if value:
            values_by_column[col["name"]] = value
        elif col["required"]:
            missing_required.append(col["name"])

    if missing_required:
        flash(f"Missing required field(s): {', '.join(missing_required)}.", "error")
        context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
        return render_template("admin.html", **context, row_form_error=_row_form_error(table_name))
    if not values_by_column:
        flash("Enter at least one field to add a new row.", "error")
        context = _build_admin_context(conn, _has_correction_authority(), _is_super_admin())
        return render_template("admin.html", **context, row_form_error=_row_form_error(table_name))

    all_column_names = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    now = datetime.now(timezone.utc).isoformat()
    if "updated_at" in all_column_names:
        values_by_column["updated_at"] = now
    if "updated_by" in all_column_names:
        values_by_column["updated_by"] = current_user.display_name
    if "created_at" in all_column_names:
        values_by_column["created_at"] = now

    columns = list(values_by_column.keys())
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT INTO {table_name} ({', '.join(columns)}) VALUES ({placeholders})",
        [values_by_column[c] for c in columns],
    )
    conn.commit()

    description = ", ".join(f"{k}={v}" for k, v in values_by_column.items() if k not in AUTO_MANAGED_COLUMNS)
    _record_correction(conn, table_name, f"Added row: {description}", reason)
    flash("New row added.")
    return redirect(url_for("admin.index"))


@admin_bp.route("/config/<table_name>/<int:row_id>/delete", methods=["POST"])
@login_required
def delete_row(table_name, row_id):
    if not _has_correction_authority():
        flash("Only Victoria or Mark can make rule corrections.", "error")
        return redirect(url_for("queues.index"))
    if table_name not in EDITABLE_TABLES:
        flash("Unknown config table.", "error")
        return redirect(url_for("admin.index"))

    reason = request.form.get("reason", "")
    if not reason.strip():
        flash("A reason is required for every rule correction.", "error")
        return redirect(url_for("admin.index"))

    conn = get_db()
    row = conn.execute(f"SELECT * FROM {table_name} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        flash("Row not found.", "error")
        return redirect(url_for("admin.index"))

    description = ", ".join(
        f"{k}={row[k]}" for k in row.keys() if k not in AUTO_MANAGED_COLUMNS and k != "id"
    )
    conn.execute(f"DELETE FROM {table_name} WHERE id = ?", (row_id,))
    conn.commit()

    _record_correction(conn, table_name, f"Deleted row {row_id}: {description}", reason)
    flash("Row deleted.")
    return redirect(url_for("admin.index"))


def _app_url() -> str:
    return (current_app.config.get("SAVVY_SCOUT_APP_BASE_URL") or request.host_url).rstrip("/")


def _invite_or_reset(email: str, display_name: str, username: str) -> tuple[str, str]:
    """Generates a temp password, sends the invite/reset email, and returns
    (flash_message, flash_category) -- SMTP isn't configured in every
    environment yet, so a send failure still leaves the account usable and
    surfaces the temp password for the admin to hand over manually instead
    of silently failing the whole action."""
    temp_password = secrets.token_urlsafe(9)
    app_url = _app_url()
    try:
        send_account_invite_email(email, display_name, app_url, email, temp_password)
        message = f"Invited {display_name} at {email} -- they'll receive the app link and a temporary password by email."
        category = "success"
    except NotificationError as exc:
        message = (
            f"Account saved, but the invite email couldn't be sent ({exc}). "
            f"Share this manually -- link: {app_url}, email: {email}, temporary password: {temp_password}"
        )
        category = "error"
    return temp_password, (message, category)


@admin_bp.route("/users/add", methods=["POST"])
@login_required
def add_user():
    """Standalone "add a teammate" for the admin (is_admin), independent of
    Sectors & Owners' inline "+ New person..." creation -- that path only
    renders for Victoria/Mark (has_correction_authority), so an is_admin-
    only Mark had no way to create an account at all without it (2026-08-09
    fix: the Manage Users card told him to use Sectors & Owners, but he
    can't see that section)."""
    if not _is_super_admin():
        flash("Only the admin account can manage users.", "error")
        return redirect(url_for("queues.index"))

    display_name = request.form.get("display_name", "").strip()
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "account_user")
    if role not in ("admin", "account_approver", "account_user"):
        role = "account_user"
    client_id = request.form.get("client_id", type=int)

    if not display_name or not email:
        flash("Display name and email are both required.", "error")
        return redirect(url_for("admin.users_index"))
    if "@" not in email:
        flash(f"'{email}' doesn't look like a valid email address.", "error")
        return redirect(url_for("admin.users_index"))

    # username is the login fallback for the four original accounts; new
    # accounts log in by email, but every row still needs a unique username
    # (schema constraint) -- derive one from the email's local part.
    username = email.split("@", 1)[0]

    conn = get_db()
    trifork_id = current_app.config["TRIFORK_CLIENT_ID"]
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone() if client_id else None
    if client is None:
        # 2026-09-19 client-portal build: this form now lets Mark pick any
        # active client, not just Trifork -- but a missing/invalid client_id
        # (an old cached form, a stray form field) must never silently fall
        # back to Trifork, which would put a stranger's login in Trifork's
        # own workspace. Fall back only for genuinely no client_id at all
        # (backward compatibility with the pre-picker form shape).
        client = conn.execute("SELECT * FROM clients WHERE id = ?", (trifork_id,)).fetchone()
    client_id = client["id"]
    # admin is the platform owner (2026-09-20, generalized from the earlier
    # is_victoria-only guard) -- exclusively Trifork's own account, never
    # attachable to a tenant client regardless of what the form posts.
    if client_id != trifork_id and role == "admin":
        role = "account_user"

    existing = conn.execute(
        "SELECT 1 FROM users WHERE email = ? OR username = ? OR display_name = ?",
        (email, username, display_name),
    ).fetchone()
    if existing:
        flash("A user with that email, username, or display name already exists.", "error")
        return redirect(url_for("admin.users_index"))

    temp_password, (message, category) = _invite_or_reset(email, display_name, username)
    conn.execute(
        "INSERT INTO users (username, password_hash, display_name, email, role, "
        "created_at, client_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            username,
            generate_password_hash(temp_password),
            display_name,
            email,
            role,
            datetime.now(timezone.utc).isoformat(),
            client_id,
        ),
    )
    conn.commit()
    log_audit(conn, "user", email, "account_created", current_user.display_name, f"Added {display_name} ({email})")

    flash(message, category)
    return redirect(url_for("admin.users_index"))


@admin_bp.route("/users/<int:user_id>/update-contact", methods=["POST"])
@login_required
def update_user_contact(user_id):
    """Backfills email/Teams webhook for the four originally-seeded accounts
    (mark, kanvesh, hammad, victoria), which have neither on file since they
    were created before email/Teams notifications existed (2026-08-09) --
    lets an owner actually receive new-opportunity alerts without needing a
    full account re-creation. If this is the first time an email is set on
    the account (it was empty before), also issues a fresh temporary
    password and emails the person their actual username + password
    (2026-08-09: these are genuinely first logins for them in practice --
    they never had a reason to know their seeded username/password before
    getting an email at all -- so "log in with your usual username/
    password" was useless; give them real, working credentials instead)."""
    if not _is_super_admin():
        flash("Only the admin account can manage users.", "error")
        return redirect(url_for("queues.index"))

    email = request.form.get("email", "").strip().lower()
    if email and "@" not in email:
        flash(f"'{email}' doesn't look like a valid email address.", "error")
        return redirect(url_for("admin.users_index"))

    teams_webhook_url = request.form.get("teams_webhook_url", "").strip()
    if teams_webhook_url and not teams_webhook_url.startswith("https://"):
        flash("Teams webhook URL must start with https://.", "error")
        return redirect(url_for("admin.users_index"))

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.users_index"))

    if email:
        existing = conn.execute(
            "SELECT 1 FROM users WHERE email = ? AND id != ?", (email, user_id)
        ).fetchone()
        if existing:
            flash(f"Another user already has the email '{email}'.", "error")
            return redirect(url_for("admin.users_index"))

    is_new_email = bool(email) and not row["email"]

    conn.execute(
        "UPDATE users SET email = ?, teams_webhook_url = ? WHERE id = ?",
        (email or None, teams_webhook_url or None, user_id),
    )
    conn.commit()

    if is_new_email:
        temp_password, (message, category) = _invite_or_reset(email, row["display_name"], row["username"])
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(temp_password), user_id)
        )
        conn.commit()
        log_audit(
            conn, "user", email, "contact_updated", current_user.display_name,
            f"Set email to {email} for {row['display_name']} (credentials emailed)",
        )
        flash(message, category)
        return redirect(url_for("admin.users_index"))

    log_audit(
        conn, "user", email or row["username"], "contact_updated", current_user.display_name,
        f"Set email to {email or '(cleared)'} and Teams webhook to "
        f"{'(set)' if teams_webhook_url else '(cleared)'} for {row['display_name']}",
    )

    flash(f"Updated {row['display_name']}'s contact details.")
    return redirect(url_for("admin.users_index"))


@admin_bp.route("/users")
@login_required
def users_index():
    """Manage Users now lives merged into admin.index (2026-08-09) -- this
    route is kept only so old links/bookmarks still land somewhere sane."""
    return redirect(url_for("admin.index") + "#group-users")


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
def reset_user_password(user_id):
    if not _is_super_admin():
        flash("Only the admin account can manage users.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.users_index"))
    if not row["email"]:
        flash(f"{row['display_name']} has no email on file -- reset the password directly in the database instead.", "error")
        return redirect(url_for("admin.users_index"))

    temp_password, (message, category) = _invite_or_reset(row["email"], row["display_name"], row["username"])
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(temp_password), user_id)
    )
    conn.commit()
    log_audit(conn, "user", row["email"], "password_reset", current_user.display_name, f"Reset password for {row['display_name']}")

    flash(message, category)
    return redirect(url_for("admin.users_index"))


@admin_bp.route("/users/<int:user_id>/send-invite", methods=["POST"])
@login_required
def send_invite(user_id):
    """One-click resend for "the invite never arrived" (2026-08-09) -- same
    underlying action as Reset Password (fresh temp password + full
    username/password emailed), just a friendlier label/entry point for
    onboarding someone rather than a security-flavoured "reset" action."""
    if not _is_super_admin():
        flash("Only the admin account can manage users.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.users_index"))
    if not row["email"]:
        flash(f"{row['display_name']} has no email on file yet -- set one first.", "error")
        return redirect(url_for("admin.users_index"))

    temp_password, (message, category) = _invite_or_reset(row["email"], row["display_name"], row["username"])
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(temp_password), user_id)
    )
    conn.commit()
    log_audit(conn, "user", row["email"], "invite_resent", current_user.display_name, f"Resent invite to {row['display_name']}")

    flash(message, category)
    return redirect(url_for("admin.users_index"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    if not _is_super_admin():
        flash("Only the admin account can manage users.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        flash("User not found.", "error")
        return redirect(url_for("admin.users_index"))
    if str(row["id"]) == current_user.id:
        flash("You can't delete your own account while logged in as it.", "error")
        return redirect(url_for("admin.users_index"))

    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    log_audit(conn, "user", row["email"] or row["username"], "account_deleted", current_user.display_name, f"Removed {row['display_name']}")

    flash(f"Removed {row['display_name']}'s account.")
    return redirect(url_for("admin.users_index"))


def _client_filter_from_form() -> dict:
    """Comma-separated free-text fields, not dynamic chip-style inputs --
    same "keep it simple" principle as the filter design itself. Notice
    types come from a fixed checkbox list (UK1-UK5), the only field with a
    real fixed vocabulary."""
    def _csv_list(field: str) -> list[str]:
        raw = request.form.get(field, "")
        return [item.strip() for item in raw.split(",") if item.strip()]

    return {
        "cpv_prefixes": _csv_list("cpv_prefixes"),
        "keywords": _csv_list("keywords"),
        "notice_types": request.form.getlist("notice_types"),
        "regions": _csv_list("regions"),
        "min_value": request.form.get("min_value") or None,
        "max_value": request.form.get("max_value") or None,
    }


def _client_form_error(name: str, f: dict) -> dict:
    """Reshapes _client_filter_from_form()'s output (lists, for matching)
    back into what the Add-a-client form's text inputs need to redisplay
    what was just typed on a failed submit (2026-09-20) -- previously
    every failure branch below redirected to a blank form, discarding all
    7 fields on any single mistake (a duplicate name, an empty filter)."""
    return {
        "name": name,
        "cpv_prefixes": ", ".join(f["cpv_prefixes"]),
        "keywords": ", ".join(f["keywords"]),
        "regions": ", ".join(f["regions"]),
        "notice_types": f["notice_types"],
        "min_value": f["min_value"],
        "max_value": f["max_value"],
    }


@admin_bp.route("/clients/new")
@login_required
def new_client():
    """Dedicated "Add a client" page (2026-09-20) -- previously a
    permanently-open form card at the top of the Clients section on
    admin.html, competing for space with the client list itself."""
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))
    return render_template("admin_client_form.html", client=None, filter=None, client_form_error=None)


@admin_bp.route("/clients/<int:client_id>/edit")
@login_required
def edit_client(client_id):
    """Dedicated "Configure this client" page (2026-09-20) -- previously
    an entire edit form (3 text fields, 5 checkboxes, 2 number fields)
    squeezed into one cell of the Clients table, for every client, all at
    once, with no click-through of its own (unlike viewing a client's
    matches, which already had one)."""
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))
    conn = get_db()
    client = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND name != 'Trifork'", (client_id,)
    ).fetchone()
    if client is None:
        flash("Client not found.", "error")
        return redirect(url_for("admin.index") + "#group-clients")
    filter_row = conn.execute("SELECT * FROM client_filters WHERE client_id = ?", (client_id,)).fetchone()
    return render_template("admin_client_form.html", client=client, filter=filter_row, client_form_error=None)


@admin_bp.route("/clients/add", methods=["POST"])
@login_required
def add_client():
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))

    name = request.form.get("name", "").strip()
    f = _client_filter_from_form()
    conn = get_db()

    if not name:
        flash("Client name is required.", "error")
        return render_template("admin_client_form.html", client=None, filter=None, client_form_error=_client_form_error(name, f))
    if name == "Trifork":
        flash('"Trifork" is reserved for the existing account.', "error")
        return render_template("admin_client_form.html", client=None, filter=None, client_form_error=_client_form_error(name, f))

    if client_filter_is_empty(f):
        flash(
            "At least one filter field (CPV prefix, keyword, notice type, region, or value) "
            "is required -- an empty filter would match every notice in the backlog.",
            "error",
        )
        return render_template("admin_client_form.html", client=None, filter=None, client_form_error=_client_form_error(name, f))

    existing = conn.execute("SELECT 1 FROM clients WHERE name = ?", (name,)).fetchone()
    if existing:
        flash(f'A client named "{name}" already exists.', "error")
        return render_template("admin_client_form.html", client=None, filter=None, client_form_error=_client_form_error(name, f))

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO clients (name, slug, is_active, created_at, created_by) VALUES (?, ?, 1, ?, ?)",
        (name, unique_client_slug(conn, name), now, current_user.display_name),
    )
    client_id = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()["id"]

    conn.execute(
        "INSERT INTO client_filters (client_id, cpv_prefixes, keywords, notice_types, regions, "
        "min_value, max_value, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            client_id, json.dumps(f["cpv_prefixes"]), json.dumps(f["keywords"]),
            json.dumps(f["notice_types"]), json.dumps(f["regions"]),
            f["min_value"], f["max_value"], now, current_user.display_name,
        ),
    )
    conn.commit()

    matched = run_client_triage(conn, client_id)
    flash(f'Added "{name}" and evaluated {matched} existing notices against its filter.')
    return redirect(url_for("admin.index") + "#group-clients")


@admin_bp.route("/clients/<int:client_id>/update-filter", methods=["POST"])
@login_required
def update_client_filter(client_id):
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None or client["name"] == "Trifork":
        flash("Client not found.", "error")
        return redirect(url_for("admin.index") + "#group-clients")

    f = _client_filter_from_form()
    if client_filter_is_empty(f):
        # No client_form_error/re-render here (2026-09-20): unlike add_row/
        # update_row/add_client, this route's only failure mode is "every
        # field was left blank" -- there's no partially-correct submission
        # to lose, since any single non-blank field passes. Redirecting
        # back to edit_client() re-fetches and shows the real, unchanged,
        # non-empty filter still in the database; showing the just-
        # submitted (all-blank) values instead would incorrectly suggest
        # the filter had been cleared.
        flash(
            "At least one filter field (CPV prefix, keyword, notice type, region, or value) "
            "is required -- an empty filter would match every notice in the backlog. "
            "The previous filter was left unchanged.",
            "error",
        )
        return redirect(url_for("admin.edit_client", client_id=client_id))

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE client_filters SET cpv_prefixes = ?, keywords = ?, notice_types = ?, regions = ?, "
        "min_value = ?, max_value = ?, updated_at = ?, updated_by = ? WHERE client_id = ?",
        (
            json.dumps(f["cpv_prefixes"]), json.dumps(f["keywords"]), json.dumps(f["notice_types"]),
            json.dumps(f["regions"]), f["min_value"], f["max_value"], now, current_user.display_name,
            client_id,
        ),
    )
    conn.commit()

    matched = run_client_triage(conn, client_id)
    flash(f'Updated "{client["name"]}"\'s filter and re-evaluated every notice -- {matched} matched or checked.')
    return redirect(url_for("admin.index") + "#group-clients")


@admin_bp.route("/clients/<int:client_id>/toggle-active", methods=["POST"])
@login_required
def toggle_client_active(client_id):
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None or client["name"] == "Trifork":
        flash("Client not found.", "error")
        return redirect(url_for("admin.index") + "#group-clients")

    conn.execute("UPDATE clients SET is_active = ? WHERE id = ?", (0 if client["is_active"] else 1, client_id))
    conn.commit()
    flash(f'{"Paused" if client["is_active"] else "Reactivated"} "{client["name"]}".')
    return redirect(url_for("admin.index") + "#group-clients")


@admin_bp.route("/clients/<int:client_id>/matches")
@login_required
def client_matches(client_id):
    """A minimal working triage for a client -- not Trifork's full 5-gate
    workflow (no Phase 2 AI, no escalation, no reports), just enough for
    Mark to actually act on matches day-to-day: mark a notice Shortlisted
    or Rejected with a note, same operating model as Trifork (Mark
    triages on the client's behalf; no separate client login exists).
    status defaults to NEW until Mark records a decision."""
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))

    conn = get_db()
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if client is None or client["name"] == "Trifork":
        flash("Client not found.", "error")
        return redirect(url_for("admin.index") + "#group-clients")

    status_filter = request.args.get("status", "")
    matches = get_client_matches(conn, client_id, status_filter or None)
    status_counts = get_client_match_status_counts(conn, client_id)

    return render_template(
        "admin_client_matches.html", client=client, matches=matches,
        status_filter=status_filter, status_counts=status_counts,
    )


@admin_bp.route("/clients/<int:client_id>/notices/<int:notice_id>/set-status", methods=["POST"])
@login_required
def set_client_notice_status(client_id, notice_id):
    if not _is_super_admin():
        flash("Only the admin account can manage clients.", "error")
        return redirect(url_for("queues.index"))

    status = request.form.get("status", "")
    if status not in ("NEW", "SHORTLISTED", "REJECTED"):
        flash("Invalid status.", "error")
        return redirect(url_for("admin.client_matches", client_id=client_id))

    note = request.form.get("note", "").strip() or None
    conn = get_db()
    record_client_notice_status(conn, client_id, notice_id, status, note, current_user.display_name)

    keep_filter = request.form.get("status_filter", "")
    return redirect(url_for("admin.client_matches", client_id=client_id, status=keep_filter or None))


@admin_bp.route("/retriage-escalated", methods=["POST"])
@login_required
def retriage_escalated():
    """One-time-use bulk action for a gate/config correction: sends every
    ESCALATED_TO_VICTORIA notice back to PHASE2_SCOPED with its Phase 1
    gates freshly re-evaluated, so owners review the updated result before
    anything reaches Victoria again."""
    if not (_has_correction_authority() or _is_super_admin()):
        flash("Only Victoria or the admin account can do this.", "error")
        return redirect(url_for("queues.index"))
    conn = get_db()
    counts = bring_back_escalated_for_gate_retriage(conn, actor=current_user.display_name)
    flash(
        f"Re-evaluated Phase 1 gates and sent {counts['sent_to_phase2']} of "
        f"{counts['checked']} escalated notice(s) back to Phase 2 for owner review.",
    )
    return redirect(url_for("admin.index"))


@admin_bp.route("/notices/<int:notice_id>/delete", methods=["POST"])
@login_required
def delete_notice(notice_id):
    """Permanently removes a notice and every row referencing it (gate
    results, triage runs, Phase 2 assessment, escalation briefs, status
    history, audit log) -- for cleaning up test/duplicate entries, not for
    real triage decisions (use Reject for those)."""
    if not (_has_correction_authority() or _is_super_admin()):
        flash("Only Victoria or the admin account can do this.", "error")
        return redirect(url_for("queues.index"))
    conn = get_db()
    row = conn.execute("SELECT ref, title FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if row is None:
        flash("Notice not found.", "error")
        return redirect(url_for("admin.index"))

    conn.execute("DELETE FROM gate_results WHERE notice_id = ?", (notice_id,))
    conn.execute("DELETE FROM triage_runs WHERE notice_id = ?", (notice_id,))
    conn.execute("DELETE FROM phase2_assessments WHERE notice_id = ?", (notice_id,))
    conn.execute("DELETE FROM escalation_briefs WHERE notice_id = ?", (notice_id,))
    conn.execute("DELETE FROM status_history WHERE notice_id = ?", (notice_id,))
    conn.execute("DELETE FROM audit_log WHERE entity_type = 'notice' AND entity_id = ?", (str(notice_id),))
    conn.execute("DELETE FROM notices WHERE id = ?", (notice_id,))
    conn.commit()
    log_audit(conn, "notice", str(notice_id), "notice_deleted", current_user.display_name, f"Deleted {row['ref']} -- {row['title']}")

    flash(f"Deleted notice {row['ref']} -- {row['title']}.")
    return redirect(url_for("admin.index"))
