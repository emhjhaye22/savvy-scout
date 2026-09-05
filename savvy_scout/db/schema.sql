-- Savvy Scout Phase A schema. SQLite.

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref TEXT NOT NULL UNIQUE,
    ocid TEXT,
    title TEXT NOT NULL,
    buyer TEXT,
    source TEXT NOT NULL,
    notice_type TEXT,
    uk_stage TEXT NOT NULL DEFAULT 'UNVERIFIED',
    status TEXT NOT NULL DEFAULT 'NEW',
    -- Set when this notice was auto-rejected by the app itself -- either no
    -- sector/owner matched (nobody to review it), or the FAIL was a
    -- deterministic call with no real judgment involved (Gate 3 UK5, or a
    -- sector-scoped CPV mismatch) -- as opposed to a human's own REJECTED
    -- decision. Lets retriage tooling tell the two apart: a keyword/CPV/
    -- config fix can still recover an auto-rejected notice, never a
    -- human one.
    auto_rejected_unowned INTEGER NOT NULL DEFAULT 0,
    sector TEXT,
    owner TEXT,
    indicative_value TEXT,
    cpv_primary TEXT,
    cpv_primary_inferred INTEGER NOT NULL DEFAULT 0,
    cpv_additional TEXT,
    cpv_primary_description TEXT,
    deadline TEXT,
    supplier_name TEXT,
    supplier_address TEXT,
    buyer_address TEXT,
    buyer_contact_email TEXT,
    buyer_region TEXT,
    procurement_method TEXT,
    procurement_method_details TEXT,
    notice_url TEXT,
    -- Additional OCDS fields captured 2026-07-30 -- previously present in
    -- raw_json but never parsed out into a queryable/displayable field.
    value_amount_gross TEXT,
    above_threshold INTEGER,
    main_procurement_category TEXT,
    enquiry_period_end TEXT,
    award_period_end TEXT,
    submission_method_details TEXT,
    submission_languages TEXT,
    electronic_submission_policy TEXT,
    procedure_features TEXT,
    award_criteria_summary TEXT,
    contract_start_date TEXT,
    contract_max_extent_date TEXT,
    renewal_description TEXT,
    buyer_ppon TEXT,
    buyer_website TEXT,
    buyer_org_type TEXT,
    conflicts_assessment TEXT,
    bid_documents_json TEXT,
    text_blob TEXT NOT NULL DEFAULT '',
    tender_status TEXT,
    lot_statuses TEXT,
    tender_period_end TEXT,
    pme_due_date TEXT,
    future_notice_date TEXT,
    contract_end_date TEXT,
    is_award INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    published_at TEXT,
    -- Set once, on first insert, and never touched again -- unlike
    -- published_at (the source release's own "date" field, which is its
    -- LAST-UPDATED timestamp and gets overwritten on every re-sweep, e.g.
    -- when an old notice is amended, awarded, or cancelled).
    first_published_at TEXT,
    -- True when no reliable original publication date was present.
    publish_date_unknown INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_swept_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS triage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    headline_gate TEXT,
    headline_outcome TEXT NOT NULL,
    headline_reason TEXT,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    triage_run_id INTEGER NOT NULL REFERENCES triage_runs(id),
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    gate_number TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    evaluated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    action TEXT NOT NULL,
    user TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    reason TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS contract_expiry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_ref TEXT NOT NULL,
    buyer TEXT,
    title TEXT,
    end_date TEXT NOT NULL,
    review_date TEXT NOT NULL,
    source_ref TEXT,
    created_at TEXT NOT NULL
);

-- Competitor Intel's watch toggle. supplier_name is the natural key (the
-- same free-text name notices.supplier_name carries off award notices --
-- no separate competitor entity exists anywhere else in the app).
CREATE TABLE IF NOT EXISTS watched_competitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_name TEXT NOT NULL UNIQUE,
    watched_by TEXT NOT NULL,
    watched_at TEXT NOT NULL
);

-- Shortlists: one flat save-list per the single-owner design (2026-09-05
-- clarification -- no named/multiple lists, just a star toggle on a notice
-- and one page listing everything saved). notice_id is UNIQUE because a
-- notice is either on the list or it isn't, not on it more than once.
CREATE TABLE IF NOT EXISTS shortlisted_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL UNIQUE REFERENCES notices(id),
    added_by TEXT NOT NULL,
    added_at TEXT NOT NULL
);

-- Draft assist: PROVISIONAL AI-drafted answers to selection-questionnaire /
-- bid-response questions Mark pastes in per opportunity (2026-09-05: no
-- questionnaire text is captured anywhere else in the app, so there is
-- nothing to draft against without this). Reuses the same Anthropic/OpenAI
-- client already wired for Phase 2 scope reads -- status tracks the same
-- "PROVISIONAL until a human reviews it" guardrail used everywhere else AI
-- output appears in this app.
CREATE TABLE IF NOT EXISTS draft_assist_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    question_text TEXT NOT NULL,
    draft_answer TEXT,
    status TEXT NOT NULL DEFAULT 'DRAFTED',
    model_used TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    reviewed_at TEXT
);

-- Config: buyer sector -> owner. Energy = Mark per Victoria's verbal sector
-- confirmation (references), overriding the original SPEC.md draft (Kanvesh).
-- See README "Open questions for Victoria" for the formal-confirmation ask.
CREATE TABLE IF NOT EXISTS config_owner_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL UNIQUE,
    owner TEXT NOT NULL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Config: keyword -> sector, used to classify a buyer/notice into a sector
-- before the owner map above applies. Heuristic pending a confirmed buyer
-- list (SPEC.md open question 2).
-- category: 'identity' (a buyer/company name or specific-enough term, matches
-- the sector on its own) or 'generic_needs_coupling' (bare industry
-- vocabulary -- energy, bank, rail -- that also matches unrelated non-IT
-- work from the same buyer/industry, so it only counts alongside a real
-- capability or product-coupling term from config_coupling_terms). Same
-- pattern Gate 2 already uses for platform/digital/data.
CREATE TABLE IF NOT EXISTS config_sector_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL,
    keyword TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'identity',
    notes TEXT
);

-- Config: Gate 2 type-of-work terms.
-- category: 'fail' (unconditional fail, unchanged from SPEC.md),
--           'unconditional_pass' (specific enough to pass alone),
--           'generic_needs_coupling' (platform/digital/data: PASS only if a
--           sector or capability/product coupling term is also present,
--           otherwise FLAG, per your Gate 2 decision).
CREATE TABLE IF NOT EXISTS config_gate2_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    category TEXT NOT NULL,
    notes TEXT
);

-- Config: coupling evidence terms for Gate 2 (sector terms and capability/
-- product terms). A generic term is "coupled" if one of these also appears.
CREATE TABLE IF NOT EXISTS config_coupling_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    kind TEXT NOT NULL, -- 'sector' or 'capability'
    sector TEXT,
    notes TEXT
);

-- Config: exclusion terms for Gate 1. If one of these appears alongside an
-- otherwise-matching sector, that sector is excluded from the match entirely
-- (e.g. Aviation matches "airline operations software" but excludes "ground
-- equipment", so a ground-handling-equipment notice never counts as
-- Aviation even though it mentions an airline). 2026-07-28 addition.
CREATE TABLE IF NOT EXISTS config_exclusion_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL,
    term TEXT NOT NULL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Config: per-sector CPV restriction for Gate 2's CPV evidence check.
-- allowed_cpv_prefixes is a JSON list of 2-digit prefixes (e.g. ["72","48"]).
-- When a sector has an enabled row here, its CPV evidence outcome is PASS
-- only if cpv_primary starts with one of these prefixes (FAIL otherwise,
-- FLAG if no CPV at all) -- this entirely overrides the sector-agnostic
-- config_cpv_lists for that sector. A sector with no row here (or a
-- disabled one) keeps using the global config_cpv_lists as before.
-- 2026-07-28 addition, for Mark's sectors (Fintech, Aviation, Rail and
-- Transport, Energy): only IT-services (72xxx) and software-package
-- (48xxx) CPV codes count as in-scope for these sectors.
CREATE TABLE IF NOT EXISTS config_sector_cpv_scope (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sector TEXT NOT NULL UNIQUE,
    allowed_cpv_prefixes TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Config: framework call-off keywords vs establishment/direct keywords, for
-- Gate 3. Trifork's confirmed framework memberships (currently none, G-Cloud
-- 15 application in progress per the references) live in
-- config_trifork_frameworks; any detected call-off against a framework not
-- in that table fails.
CREATE TABLE IF NOT EXISTS config_framework_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    term TEXT NOT NULL,
    category TEXT NOT NULL, -- 'call_off', 'establishment', 'direct'
    notes TEXT
);

CREATE TABLE IF NOT EXISTS config_trifork_frameworks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    framework_name TEXT NOT NULL UNIQUE,
    confirmed_date TEXT,
    notes TEXT
);

-- Config: CPV lists for Gate 5. cpv_code may be an exact 8-digit code or a
-- 2-digit prefix bucket ('72xx', '73xx', '48xx' etc). list_type: PASS,
-- INFERRED_FIT, FLAG, FAIL. condition_keyword, when set, means the FAIL only
-- applies when that keyword also appears in the notice text (SPEC.md's
-- "when bundled with secure testing" / "when bundled with testing" rules).
-- Sourced from the Kanvesh scouting skill Section 4, verified against Home
-- Office SCBP notice 039639-2026 (see README).
CREATE TABLE IF NOT EXISTS config_cpv_lists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cpv_code TEXT NOT NULL,
    list_type TEXT NOT NULL,
    condition_keyword TEXT,
    notes TEXT
);

-- Config: Filter 3, scale and incumbents. A separately agreed rule (dated
-- 15 June 2026), distinct from the "no minimum value floor" non-negotiable.
-- Config-driven with an on/off toggle.
CREATE TABLE IF NOT EXISTS config_scale_filter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled INTEGER NOT NULL DEFAULT 1,
    value_threshold REAL NOT NULL DEFAULT 500000000,
    si_prime_suppliers TEXT NOT NULL,
    agreed_date TEXT NOT NULL,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Sweep sources: which procurement portals the daily sweep pulls from.
-- source_type is the dispatch key runner.py uses to pick the client
-- function (see savvy_scout/sources/); an unrecognised source_type is
-- skipped with a logged warning rather than failing the whole sweep.
-- enabled lets admin toggle a source off without deleting its config row.
CREATE TABLE IF NOT EXISTS config_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    base_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Phase B: dashboard accounts, individual logins, no shared accounts
-- (SPEC.md B1). is_victoria carries the existing rule-correction authority
-- (Victoria/Kanvesh, see dashboard/routes/admin.py); is_admin is a separate,
-- narrower authority for the account-management screen itself (2026-08-08,
-- deliberately Mark, not Victoria -- see dashboard/routes/admin.py
-- _is_super_admin). email is nullable so the four original seeded accounts
-- (mark/kanvesh/hammad/victoria) keep working via username login without
-- needing a real address backfilled.
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL UNIQUE, -- Mark, Kanvesh, Hammad, Victoria
    email TEXT,
    is_victoria INTEGER NOT NULL DEFAULT 0,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    teams_webhook_url TEXT
);

-- Config: Trifork capability profile fed to the Claude API scope read (B2).
-- Singleton row, editable via the admin tab (B4) without a code change.
CREATE TABLE IF NOT EXISTS config_capability_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_text TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

-- Phase B: AI scope reads (B2). One row per Phase 2 assessment. Always
-- displayed/exported with an application-level "PROVISIONAL, FOR
-- VALIDATION" label, never trusted from the model's own output.
CREATE TABLE IF NOT EXISTS phase2_assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    capability_fit_rating TEXT NOT NULL,
    capability_fit_reasoning TEXT NOT NULL,
    competitor_position_rating TEXT NOT NULL,
    competitor_position_reasoning TEXT NOT NULL,
    right_to_win_rating TEXT NOT NULL,
    right_to_win_reasoning TEXT NOT NULL,
    overall_rating TEXT NOT NULL,
    overall_reasoning TEXT NOT NULL,
    open_questions TEXT NOT NULL, -- JSON array
    model_used TEXT NOT NULL,
    created_at TEXT NOT NULL,
    -- Internal Addendum sections C-F (2026-08-09, matches the reference
    -- document's exact table shapes): capability_mapping is a JSON array of
    -- {problem, capability} pairs (Section C, "Why this is a high fit");
    -- blockers is a JSON array of {blocker, assessment} pairs; asks is a
    -- JSON array of {ask, why_it_matters} pairs; recommendation is a JSON
    -- object {decision, immediate_actions: [str], rationale}.
    capability_mapping TEXT,
    -- UK-newness, evidence positioning and partnering points belong here,
    -- rather than being represented as hard blockers.
    positioning_points TEXT,
    blockers TEXT,
    asks TEXT,
    recommendation TEXT,
    -- Rich narrative content for the Internal Addendum / Capture Brief
    -- (2026-08-16), written directly by the Phase 2 AI from the full notice
    -- text rather than assembled by regex-splitting raw text at document-build
    -- time. executive_summary is a JSON object {opening, scope_summary,
    -- executive_view}; key_terms and procurement_timetable and
    -- decision_framework are JSON arrays of {term, meaning} /
    -- {milestone, date} / {question, implication}; engagement_model is a JSON
    -- object {model, how_to_respond}. All nullable; existing assessments just
    -- don't have them until re-run.
    executive_summary TEXT,
    key_terms TEXT,
    scope_of_requirement TEXT,
    engagement_model TEXT,
    procurement_timetable TEXT,
    decision_framework TEXT,
    solo_or_partner_recommendation TEXT,
    -- MED capability_fit's dual reading (2026-08-19, house style spec):
    -- {build_signals: [str], product_signals: [str], honest_position: str}.
    med_dual_reading TEXT
);

-- Phase B: escalation briefs (B3). One row per auto-generated brief.
CREATE TABLE IF NOT EXISTS escalation_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL REFERENCES notices(id),
    trigger_reason TEXT NOT NULL, -- e.g. 'gate_flag:gate1' or 'owner_marked_victoria_decision'
    docx_path TEXT NOT NULL,
    emailed_to TEXT,
    emailed_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    brief_type TEXT NOT NULL DEFAULT 'INTERNAL_ADDENDUM'
);

-- Phase B: learning loop (B4). Every Victoria ruling and rule correction,
-- version-log style, distinct from the generic audit_log (which already
-- captures every settings change). Restricted to Victoria and Kanvesh.
CREATE TABLE IF NOT EXISTS rule_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entered_by TEXT NOT NULL,
    entered_at TEXT NOT NULL,
    table_affected TEXT NOT NULL,
    description TEXT NOT NULL,
    reason TEXT NOT NULL,
    source TEXT
);

-- Durable history of every sweep and each source touched in that run.
CREATE TABLE IF NOT EXISTS sweep_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    triggered_by TEXT NOT NULL,
    total_pulled INTEGER NOT NULL DEFAULT 0,
    total_triaged INTEGER NOT NULL DEFAULT 0,
    total_expiring_leads INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sweep_run_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sweep_run_id INTEGER NOT NULL REFERENCES sweep_runs(id),
    source_name TEXT NOT NULL,
    status TEXT NOT NULL,
    pulled INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_notices_ref ON notices(ref);
CREATE INDEX IF NOT EXISTS idx_notices_status ON notices(status);
CREATE INDEX IF NOT EXISTS idx_gate_results_notice ON gate_results(notice_id);
CREATE INDEX IF NOT EXISTS idx_triage_runs_notice ON triage_runs(notice_id);
CREATE INDEX IF NOT EXISTS idx_phase2_assessments_notice ON phase2_assessments(notice_id);
CREATE INDEX IF NOT EXISTS idx_escalation_briefs_notice ON escalation_briefs(notice_id);
CREATE INDEX IF NOT EXISTS idx_sweep_run_sources_run ON sweep_run_sources(sweep_run_id);
