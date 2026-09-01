import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema)
    _apply_migrations(conn)
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    # v1.5 status rename migration
    status_map = {
        "AWAITING_PHASE1_APPROVAL": "TO_REVIEW",
        "MONITOR": "MONITORING",
    }
    for old_status, new_status in status_map.items():
        conn.execute("UPDATE notices SET status = ? WHERE status = ?", (new_status, old_status))
        conn.execute("UPDATE status_history SET from_status = ? WHERE from_status = ?", (new_status, old_status))
        conn.execute("UPDATE status_history SET to_status = ? WHERE to_status = ?", (new_status, old_status))

    # v1.5 escalation docs: internal addendum vs capture brief marker
    cols = [r[1] for r in conn.execute("PRAGMA table_info(escalation_briefs)").fetchall()]
    if "brief_type" not in cols:
        conn.execute("ALTER TABLE escalation_briefs ADD COLUMN brief_type TEXT NOT NULL DEFAULT 'INTERNAL_ADDENDUM'")

    # Full-detail notice fields: supplier, contracting authority contact/
    # address, procurement method/regime, human-readable CPV description --
    # previously only buried in raw_json, not queryable or displayable.
    notice_cols = [r[1] for r in conn.execute("PRAGMA table_info(notices)").fetchall()]
    new_notice_cols = [
        "cpv_primary_description",
        "supplier_name",
        "supplier_address",
        "buyer_address",
        "buyer_contact_email",
        "buyer_region",
        "procurement_method",
        "procurement_method_details",
    ]
    for col in new_notice_cols:
        if col not in notice_cols:
            conn.execute(f"ALTER TABLE notices ADD COLUMN {col} TEXT")

    # Direct link to the published notice (Find a Tender / Contracts Finder),
    # so reviewers can check the source directly from the Capture Brief.
    # Backfilled from each notice's already-stored raw_json rather than
    # constructed/guessed, since the URL is only trustworthy if the source
    # actually published it.
    if "notice_url" not in notice_cols:
        conn.execute("ALTER TABLE notices ADD COLUMN notice_url TEXT")

    if "auto_rejected_unowned" not in notice_cols:
        conn.execute("ALTER TABLE notices ADD COLUMN auto_rejected_unowned INTEGER NOT NULL DEFAULT 0")

    rows_missing_url = conn.execute(
        "SELECT id, raw_json FROM notices WHERE notice_url IS NULL AND raw_json IS NOT NULL AND raw_json != ''"
    ).fetchall()
    if rows_missing_url:
        import json as _json

        from savvy_scout.sources.ocds_parser import _find_notice_url

        for row in rows_missing_url:
            try:
                release = _json.loads(row["raw_json"])
            except ValueError:
                continue
            url = _find_notice_url(release)
            if url:
                conn.execute("UPDATE notices SET notice_url = ? WHERE id = ?", (url, row["id"]))

    # Additional OCDS fields (award criteria, submission instructions,
    # contract start/extension dates, buyer PPON/website/org type, etc.),
    # 2026-07-30: present in raw_json all along, never parsed out before.
    # Backfilled from each notice's already-stored raw_json, not just
    # applied to future sweeps, so existing notices get this too.
    additional_cols = [
        "value_amount_gross", "above_threshold", "main_procurement_category",
        "enquiry_period_end", "award_period_end", "submission_method_details",
        "submission_languages", "electronic_submission_policy", "procedure_features",
        "award_criteria_summary", "contract_start_date", "contract_max_extent_date",
        "renewal_description", "buyer_ppon", "buyer_website", "buyer_org_type",
        "conflicts_assessment", "bid_documents_json",
    ]
    missing_additional_cols = [c for c in additional_cols if c not in notice_cols]
    for col in missing_additional_cols:
        col_type = "INTEGER" if col == "above_threshold" else "TEXT"
        conn.execute(f"ALTER TABLE notices ADD COLUMN {col} {col_type}")

    # Account management (2026-08-08): email column for login-by-email and
    # invite emails, is_admin for the new account-management screen (Mark,
    # deliberately separate from is_victoria's existing rule-correction
    # authority). Existing seeded accounts keep username login working since
    # email is nullable; 'mark' is granted is_admin on the migration that
    # actually adds the column, not unconditionally, so a superadmin flag set
    # by hand later isn't clobbered by a later boot.
    user_cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "email" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    if "is_admin" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE users SET is_admin = 1 WHERE username = 'mark'")

    # Teams notifications (2026-08-09): a per-user Microsoft Teams incoming
    # webhook URL, so a new-opportunity notification can be posted straight
    # into that owner's Teams alongside the email -- no Azure AD app
    # registration needed (the Graph app-registration for this app was never
    # completed, see notifications.py), just a webhook connector the owner
    # adds to a channel/chat of their choosing and pastes the URL for here.
    if "teams_webhook_url" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN teams_webhook_url TEXT")

    # published_at (2026-08-09): the OCDS release's own publish timestamp,
    # for date-based reporting (Sector Performance) that reflects real
    # publication activity instead of our sweep cadence -- see
    # models.notice.Notice.published_at. Backfilled from each notice's
    # already-stored raw_json, same pattern as the other backfills above.
    if "published_at" not in notice_cols:
        import json as _json

        conn.execute("ALTER TABLE notices ADD COLUMN published_at TEXT")
        rows_missing_published = conn.execute(
            "SELECT id, raw_json FROM notices WHERE raw_json IS NOT NULL AND raw_json != ''"
        ).fetchall()
        for row in rows_missing_published:
            try:
                release = _json.loads(row["raw_json"])
            except ValueError:
                continue
            published_at = release.get("date")
            if published_at:
                conn.execute("UPDATE notices SET published_at = ? WHERE id = ?", (published_at, row["id"]))

    # first_published_at (2026-08-10): published_at gets overwritten on
    # every re-sweep with the source release's latest "date" (see
    # sweep.dedupe.upsert_notice), so an old notice amended/awarded/
    # cancelled today silently looks newly published today in Sector
    # Performance/Notices by Source. first_published_at is set once on
    # insert and never touched again -- see schema.sql's comment on it.
    # Backfilled from each row's current published_at, the closest
    # approximation available for notices that already existed before this
    # column did; going forward it's set correctly at first-seen time.
    notice_cols_now = [r[1] for r in conn.execute("PRAGMA table_info(notices)").fetchall()]
    if "first_published_at" not in notice_cols_now:
        conn.execute("ALTER TABLE notices ADD COLUMN first_published_at TEXT")
        conn.execute("UPDATE notices SET first_published_at = published_at WHERE first_published_at IS NULL")

    # publish_date_unknown (2026-08-10): a second finding the same day as
    # first_published_at above -- a notice we discover for the very first
    # time via an award/contract/amendment/termination release (rather than
    # an actual tender/planning notice) has no reliable publish date
    # anywhere in that release's payload, so first_published_at was still
    # wrongly getting stamped with "today" for those. Re-parses each
    # existing row's already-stored raw_json (same backfill pattern as
    # published_at above) to find its real OCDS tag and correct any rows
    # already wrongly stamped by the two sweeps that ran between deploying
    # first_published_at and this fix.
    notice_cols_now = [r[1] for r in conn.execute("PRAGMA table_info(notices)").fetchall()]
    if "publish_date_unknown" not in notice_cols_now:
        import json as _json

        conn.execute("ALTER TABLE notices ADD COLUMN publish_date_unknown INTEGER NOT NULL DEFAULT 0")
        rows_to_check = conn.execute(
            "SELECT id, raw_json FROM notices WHERE raw_json IS NOT NULL AND raw_json != ''"
        ).fetchall()
        for row in rows_to_check:
            try:
                release = _json.loads(row["raw_json"])
            except ValueError:
                continue
            tags = release.get("tag", []) or []
            if not (set(tags) & {"tender", "planning"}):
                conn.execute(
                    "UPDATE notices SET publish_date_unknown = 1, first_published_at = NULL WHERE id = ?",
                    (row["id"],),
                )

    # Contracts Finder (CSV) source (2026-08-10) -- see seed_sources for the
    # full rationale; seed_sources only inserts config_sources rows on an
    # empty table, so an already-seeded production DB needs this added
    # explicitly rather than picking it up from seed_sources on next boot.
    # Guarded on the table being non-empty (already seeded) -- a genuinely
    # fresh database must go through seed_sources for ALL rows (this one
    # included, already added there), or inserting just this one row here
    # first would make the table look non-empty and seed_sources would then
    # skip seeding the other 5 default sources entirely.
    config_sources_seeded = conn.execute("SELECT 1 FROM config_sources LIMIT 1").fetchone() is not None
    existing_csv_source = conn.execute(
        "SELECT 1 FROM config_sources WHERE source_type = 'contracts_finder_csv'"
    ).fetchone()
    if config_sources_seeded and existing_csv_source is None:
        from datetime import datetime, timezone

        conn.execute(
            "INSERT INTO config_sources (name, source_type, base_url, enabled, notes, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "Contracts Finder (CSV)",
                "contracts_finder_csv",
                "https://www.contractsfinder.service.gov.uk",
                1,
                "Same underlying site as 'Contracts Finder' above, via its own CSV "
                "export instead of the OCDS API -- confirmed live 2026-08-10 that "
                "several genuine, live opportunities (incl. ones syndicated through "
                "third-party portals) are structurally absent from the OCDS feed "
                "but present here. Runs alongside the OCDS sweep, not instead of "
                "it; writes the same source name (\"Contracts Finder\") into "
                "notices.source so both feeds appear as one row on the Overview's "
                "Notices by Source table.",
                datetime.now(timezone.utc).isoformat(),
                "system_migration",
            ),
        )

    # National government departments/agencies + housing associations added
    # to Central and Local Government, and Sizewell C added to Energy
    # (2026-08-10, explicit request after a manual-vs-app audit) -- see
    # SECTOR_KEYWORDS' comment on the same rows; that only seeds a fresh DB,
    # so an already-seeded production DB needs these inserted explicitly.
    # Guarded on the table being non-empty already, same reason as the
    # config_sources fix above: inserting into a genuinely EMPTY table here
    # first would make seed_keywords' "if not _table_empty: return" guard
    # skip seeding every other base keyword (council, nhs, energy, ...).
    sector_keywords_seeded = conn.execute("SELECT 1 FROM config_sector_keywords LIMIT 1").fetchone() is not None
    new_keyword_rows = [] if not sector_keywords_seeded else [
        ("Central and Local Government", "foreign, commonwealth and development office", "identity"),
        ("Central and Local Government", "house of commons", "identity"),
        ("Central and Local Government", "chief constable", "identity"),
        ("Central and Local Government", "police and crime commissioner", "identity"),
        ("Central and Local Government", "pension protection fund", "identity"),
        ("Central and Local Government", "natural england", "identity"),
        ("Central and Local Government", "forestry and land scotland", "identity"),
        ("Central and Local Government", "commonalty and citizens of the city of london", "identity"),
        ("Central and Local Government", "combined authority", "identity"),
        ("Central and Local Government", "development corporation", "identity"),
        ("Central and Local Government", "housing association", "identity"),
        ("Central and Local Government", "housing group", "identity"),
        ("Central and Local Government", "framework housing association", "identity"),
        ("Central and Local Government", "great places housing group", "identity"),
        ("Central and Local Government", "salvation army housing association", "identity"),
        ("Central and Local Government", "southdown housing association", "identity"),
        ("Central and Local Government", "valleys to coast housing", "identity"),
        ("Central and Local Government", "your housing group", "identity"),
        ("Central and Local Government", "riverside group", "identity"),
        ("Central and Local Government", "be one homes", "identity"),
        ("Central and Local Government", "office for legal complaints", "identity"),
        ("Energy", "sizewell c", "identity"),
    ]
    for sector, keyword, category in new_keyword_rows:
        exists = conn.execute(
            "SELECT 1 FROM config_sector_keywords WHERE sector = ? AND keyword = ?", (sector, keyword)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO config_sector_keywords (sector, keyword, category) VALUES (?, ?, ?)",
                (sector, keyword, category),
            )

    # Bare "nationwide" removed (2026-08-10) -- see seed_config.py's comment
    # on it (same collision risk as bare "visa", confirmed live: matched
    # the ordinary adverb "available nationwide", not Nationwide Building
    # Society). Only meaningful on an already-seeded DB; harmless no-op
    # otherwise since the row wouldn't exist yet either way.
    conn.execute("DELETE FROM config_sector_keywords WHERE sector = 'Fintech' AND keyword = 'nationwide'")

    # "General Medical Council" exclusion for Central and Local Government
    # (2026-08-10) -- see seed_exclusion_terms' comment on it. Guarded the
    # same way as the keyword rows above: only backfill an already-seeded
    # DB, never insert first into a genuinely empty table.
    exclusion_terms_seeded = conn.execute("SELECT 1 FROM config_exclusion_terms LIMIT 1").fetchone() is not None
    if exclusion_terms_seeded:
        existing_gmc_exclusion = conn.execute(
            "SELECT 1 FROM config_exclusion_terms WHERE sector = 'Central and Local Government' "
            "AND term = 'general medical council'"
        ).fetchone()
        if existing_gmc_exclusion is None:
            from datetime import datetime, timezone

            conn.execute(
                "INSERT INTO config_exclusion_terms (sector, term, notes, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "Central and Local Government",
                    "general medical council",
                    "Healthcare professional regulator (GMC), not local government -- "
                    "matches 'council' but has nothing to do with it.",
                    datetime.now(timezone.utc).isoformat(),
                    "system_migration",
                ),
            )

    # "health"/"hospital" no longer unconditional identity matches
    # (2026-08-10) -- see seed_config.SECTOR_KEYWORDS's comment on this.
    # A plain UPDATE, safe to run on every boot: a no-op once already
    # applied, and a no-op on a genuinely fresh DB where seed_sources
    # inserts the corrected category directly and this WHERE simply
    # matches nothing yet.
    conn.execute(
        "UPDATE config_sector_keywords SET category = 'generic_needs_coupling' "
        "WHERE sector = 'NHS and Healthcare' AND keyword IN ('health', 'hospital') "
        "AND category = 'identity'"
    )

    # Internal Addendum sections C-F (2026-08-09) -- see schema.sql's comment
    # on phase2_assessments. All nullable; existing assessments just don't
    # have them until re-run.
    assessment_cols = [r[1] for r in conn.execute("PRAGMA table_info(phase2_assessments)").fetchall()]
    for col in ("capability_mapping", "positioning_points", "blockers", "asks", "recommendation"):
        if col not in assessment_cols:
            conn.execute(f"ALTER TABLE phase2_assessments ADD COLUMN {col} TEXT")

    # Rich narrative fields for the Internal Addendum / Capture Brief
    # (2026-08-16) -- see schema.sql's comment on phase2_assessments. All
    # nullable; existing assessments just don't have them until re-run.
    for col in (
        "executive_summary", "key_terms", "scope_of_requirement", "engagement_model",
        "procurement_timetable", "decision_framework", "solo_or_partner_recommendation",
        "med_dual_reading",
    ):
        if col not in assessment_cols:
            conn.execute(f"ALTER TABLE phase2_assessments ADD COLUMN {col} TEXT")

    # Blockers/capability profile prompt correction (2026-08-12, Victoria
    # Milan's ruling of 11 August 2026): the previously-seeded capability
    # profile framed UK track record/references/clearance/staff scale as
    # "known capability gaps" the model should weigh against a rating --
    # those are positioning points, never blockers. seed_capability_profile
    # only seeds an empty table, so an already-seeded production DB needs
    # its existing row updated directly. Matched on the exact old heading
    # so this is a no-op once already applied (idempotent on every boot),
    # and never touches a row someone has since hand-edited to something
    # else entirely.
    old_profile_marker = "Known capability gaps (load-bearing, check before any HIGH or MED rating):"
    current_profile_row = conn.execute(
        "SELECT id, profile_text FROM config_capability_profile ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if current_profile_row is not None and old_profile_marker in current_profile_row["profile_text"]:
        from savvy_scout.db.seed_config import CAPABILITY_PROFILE_TEXT

        conn.execute(
            "UPDATE config_capability_profile SET profile_text = ? WHERE id = ?",
            (CAPABILITY_PROFILE_TEXT, current_profile_row["id"]),
        )

    # NHS and Healthcare owner transferred from Hammad to Mark (2026-08-11,
    # Trifork scouting skill v2, Rule 2.4, confirmed with Mark). seed_owner_map
    # only seeds an empty table, so an already-seeded production DB needs its
    # existing row updated directly. This only changes the CONFIG (which owner
    # new/re-triaged notices get); existing notices.owner rows already
    # assigned to Hammad are reassigned separately, by an explicit one-off
    # script, not by this migration.
    conn.execute(
        "UPDATE config_owner_map SET owner = 'Mark', "
        "notes = 'Transferred from Hammad to Mark, 11 August 2026 (Trifork scouting skill v2, "
        "Rule 2.4). Scouting consolidated to Mark alone on 2026-09-01.' "
        "WHERE sector = 'NHS and Healthcare' AND owner = 'Hammad'"
    )

    # Scouting consolidated to a single desk, Mark, on 2026-09-01 -- Kanvesh
    # and Hammad no longer own any sector. Central and Local Government was
    # the last sector still on Kanvesh; same idempotent pattern as the NHS
    # migration above (existing notices.owner rows reassigned separately, by
    # an explicit one-off script, not by this migration).
    conn.execute(
        "UPDATE config_owner_map SET owner = 'Mark', "
        "notes = 'Transferred from Kanvesh to Mark, 2026-09-01 -- scouting consolidated to a "
        "single desk.' "
        "WHERE sector = 'Central and Local Government' AND owner = 'Kanvesh'"
    )

    # Trifork scouting skill v2, Section 3a NHS keywords + exclusions
    # (2026-08-11) -- see SECTOR_KEYWORDS/seed_exclusion_terms' comments on
    # these same rows. Guarded the same way as the earlier keyword/exclusion
    # backfills above: only insert into an already-seeded table, never into
    # a genuinely empty one (that would trip the _table_empty seeding trap).
    sector_keywords_seeded_v2 = conn.execute(
        "SELECT 1 FROM config_sector_keywords LIMIT 1"
    ).fetchone() is not None
    nhs_v2_keyword_rows = [] if not sector_keywords_seeded_v2 else [
        ("NHS and Healthcare", "electronic patient record", "identity"),
        ("NHS and Healthcare", "epr", "identity"),
        ("NHS and Healthcare", "clinical data platform", "identity"),
        ("NHS and Healthcare", "patient portal", "identity"),
        ("NHS and Healthcare", "digital health platform", "identity"),
        ("NHS and Healthcare", "clinical decision support", "identity"),
        ("NHS and Healthcare", "clinical ai", "identity"),
        ("NHS and Healthcare", "ai diagnostics", "identity"),
        ("NHS and Healthcare", "digital pathology", "identity"),
        ("NHS and Healthcare", "pathology informatics", "identity"),
        ("NHS and Healthcare", "population health data", "identity"),
        ("NHS and Healthcare", "health data platform", "identity"),
        ("NHS and Healthcare", "interoperability", "generic_needs_coupling"),
        ("NHS and Healthcare", "fhir", "identity"),
        ("NHS and Healthcare", "hl7", "identity"),
        ("NHS and Healthcare", "patient administration", "identity"),
        ("NHS and Healthcare", "clinical safety", "identity"),
    ]
    for sector, keyword, category in nhs_v2_keyword_rows:
        exists = conn.execute(
            "SELECT 1 FROM config_sector_keywords WHERE sector = ? AND keyword = ?", (sector, keyword)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO config_sector_keywords (sector, keyword, category) VALUES (?, ?, ?)",
                (sector, keyword, category),
            )

    exclusion_terms_seeded_v2 = conn.execute(
        "SELECT 1 FROM config_exclusion_terms LIMIT 1"
    ).fetchone() is not None
    nhs_v2_exclusion_rows = [] if not exclusion_terms_seeded_v2 else [
        ("NHS and Healthcare", "medical devices", "Goods procurement, not software."),
        ("NHS and Healthcare", "medical equipment supply", "Goods procurement, not software."),
        ("NHS and Healthcare", "estates and facilities", "Facilities management, not software."),
        ("NHS and Healthcare", "clinical staffing and agency", "Staffing/agency services, not software."),
        ("NHS and Healthcare", "training and education delivery", "Training delivery, not software."),
        ("NHS and Healthcare", "ppe", "Goods procurement, not software."),
        ("NHS and Healthcare", "pharmaceuticals", "Goods procurement, not software."),
        ("NHS and Healthcare", "catering", "Facilities/catering services, not software."),
        ("NHS and Healthcare", "cleaning", "Facilities/cleaning services, not software."),
        ("NHS and Healthcare", "patient transport", "Physical transport services, not software."),
    ]
    if nhs_v2_exclusion_rows:
        from datetime import datetime, timezone as _tz

        now_v2 = datetime.now(_tz.utc).isoformat()
        for sector, term, notes in nhs_v2_exclusion_rows:
            exists = conn.execute(
                "SELECT 1 FROM config_exclusion_terms WHERE sector = ? AND term = ?", (sector, term)
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO config_exclusion_terms (sector, term, notes, updated_at, updated_by) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (sector, term, notes, now_v2, "system_migration"),
                )

    # 48xxxxxx Corax/Tiris Messenger conditional PASS (2026-08-11) -- see
    # seed_cpv_lists' comment on these rows. seed_cpv_lists only seeds an
    # empty table, so an already-seeded production DB needs the two
    # condition rows inserted explicitly; the bare 48/FLAG fallback row
    # already exists from the original seed and is left alone.
    cpv_lists_seeded = conn.execute("SELECT 1 FROM config_cpv_lists LIMIT 1").fetchone() is not None
    if cpv_lists_seeded:
        cpv_48_condition_rows = [
            ("48", "PASS", "corax", "Maps to Corax (AI analytics, decision support, clinical/AI data)."),
            ("48", "PASS", "tiris", "Maps to Tiris Messenger (secure operational/safety-critical messaging)."),
        ]
        for cpv_code, list_type, condition_keyword, notes in cpv_48_condition_rows:
            exists = conn.execute(
                "SELECT 1 FROM config_cpv_lists WHERE cpv_code = ? AND condition_keyword = ?",
                (cpv_code, condition_keyword),
            ).fetchone()
            if exists is None:
                conn.execute(
                    "INSERT INTO config_cpv_lists (cpv_code, list_type, condition_keyword, notes) "
                    "VALUES (?, ?, ?, ?)",
                    (cpv_code, list_type, condition_keyword, notes),
                )

    if missing_additional_cols:
        import json as _json

        from savvy_scout.sources.ocds_parser import extract_additional_fields

        rows_to_backfill = conn.execute(
            "SELECT id, raw_json FROM notices WHERE raw_json IS NOT NULL AND raw_json != ''"
        ).fetchall()
        for row in rows_to_backfill:
            try:
                release = _json.loads(row["raw_json"])
            except ValueError:
                continue
            fields = extract_additional_fields(release)
            conn.execute(
                "UPDATE notices SET value_amount_gross = ?, above_threshold = ?, "
                "main_procurement_category = ?, enquiry_period_end = ?, award_period_end = ?, "
                "submission_method_details = ?, submission_languages = ?, "
                "electronic_submission_policy = ?, procedure_features = ?, "
                "award_criteria_summary = ?, contract_start_date = ?, contract_max_extent_date = ?, "
                "renewal_description = ?, buyer_ppon = ?, buyer_website = ?, buyer_org_type = ?, "
                "conflicts_assessment = ?, bid_documents_json = ? WHERE id = ?",
                (
                    fields["value_amount_gross"], fields["above_threshold"],
                    fields["main_procurement_category"], fields["enquiry_period_end"],
                    fields["award_period_end"], fields["submission_method_details"],
                    fields["submission_languages"], fields["electronic_submission_policy"],
                    fields["procedure_features"], fields["award_criteria_summary"],
                    fields["contract_start_date"], fields["contract_max_extent_date"],
                    fields["renewal_description"], fields["buyer_ppon"], fields["buyer_website"],
                    fields["buyer_org_type"], fields["conflicts_assessment"],
                    fields["bid_documents_json"], row["id"],
                ),
            )
