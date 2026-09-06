"""The notice state machine (SPEC.md v1.5 A1). Main line:
NEW -> PHASE1_TRIAGED -> PHASE2_SCOPED -> AWAITING_PHASE2_APPROVAL ->
ESCALATED_TO_VICTORIA -> APPROVED -> CAPTURE_BRIEF_DRAFTED ->
DOCS_DOWNLOADED -> CALENDARED -> ACTIVE, plus TO_REVIEW, HANDOFF,
MONITORING, REJECTED and PARKED.

SPEC.md doesn't specify the side-branch edges (when REJECTED/PARKED/MONITOR
are reachable, or whether they can return to the main line), only that no
status can be skipped. The graph below is Phase A's working assumption for
those edges, extended in Phase B where the real workflow needed it: B3
escalates "any FLAG at any gate," and Gate 1/2/5 FLAGs are recorded at Phase 1
triage time, before Phase 2 scoping exists.
AWAITING_PHASE1_APPROVAL -> ESCALATED_TO_VICTORIA used to be a direct edge,
skipping Phase 2 entirely -- removed 2026-07-21: too many notices were
reaching Victoria straight off a Phase 1 gate flag, before the automated
Phase 2 scope read ever ran. Victoria escalation is now only reachable from
AWAITING_PHASE2_APPROVAL, so a notice always gets the Phase 2 AI read first;
mark_victoria_decision (workflow.approvals) also enforces this explicitly,
not just this transition table.
AWAITING_PHASE1_APPROVAL -> MONITOR is also a direct edge, needed for
re-triage: a notice re-evaluated after a config correction (e.g. a sector
keyword fix) can discover a new headline outcome of MONITOR that wasn't
visible on its first pass, without ever having left AWAITING_PHASE1_APPROVAL."""


from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    NEW = "NEW"
    PHASE1_TRIAGED = "PHASE1_TRIAGED"
    TO_REVIEW = "TO_REVIEW"
    HANDOFF = "HANDOFF"
    PHASE2_SCOPED = "PHASE2_SCOPED"
    AWAITING_PHASE2_APPROVAL = "AWAITING_PHASE2_APPROVAL"
    ESCALATED_TO_VICTORIA = "ESCALATED_TO_VICTORIA"
    APPROVED = "APPROVED"
    CAPTURE_BRIEF_DRAFTED = "CAPTURE_BRIEF_DRAFTED"
    DOCS_DOWNLOADED = "DOCS_DOWNLOADED"
    CALENDARED = "CALENDARED"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    PARKED = "PARKED"
    MONITORING = "MONITORING"

    # Backward-compatible aliases retained during migration.
    AWAITING_PHASE1_APPROVAL = "TO_REVIEW"
    MONITOR = "MONITORING"


class UKStage(str, Enum):
    UK1 = "UK1"
    UK2 = "UK2"
    UK3 = "UK3"
    UK4 = "UK4"
    UK5 = "UK5"
    UNVERIFIED = "UNVERIFIED"


ALLOWED_TRANSITIONS: dict[Status, set[Status]] = {
    Status.NEW: {Status.PHASE1_TRIAGED},
    Status.PHASE1_TRIAGED: {
        Status.TO_REVIEW,
        Status.PHASE2_SCOPED,
        Status.HANDOFF,
        Status.MONITORING,
    },
    Status.TO_REVIEW: {
        Status.PHASE2_SCOPED,
        Status.MONITORING,
        Status.REJECTED,
        Status.PARKED,
    },
    Status.HANDOFF: {Status.PHASE1_TRIAGED, Status.TO_REVIEW},
    Status.PHASE2_SCOPED: {
        Status.AWAITING_PHASE2_APPROVAL,
        Status.TO_REVIEW,
    },
    Status.AWAITING_PHASE2_APPROVAL: {
        Status.ESCALATED_TO_VICTORIA,
        Status.APPROVED,
        Status.REJECTED,
        Status.PARKED,
    },
    Status.ESCALATED_TO_VICTORIA: {
        Status.APPROVED,
        Status.REJECTED,
        Status.PARKED,
        Status.PHASE2_SCOPED,
    },
    Status.APPROVED: {Status.CAPTURE_BRIEF_DRAFTED, Status.DOCS_DOWNLOADED},
    Status.CAPTURE_BRIEF_DRAFTED: {Status.DOCS_DOWNLOADED},
    Status.DOCS_DOWNLOADED: {Status.CALENDARED},
    Status.CALENDARED: {Status.ACTIVE},
    Status.ACTIVE: set(),
    Status.MONITORING: {Status.PHASE1_TRIAGED, Status.TO_REVIEW},
    # REJECTED -> TO_REVIEW exists solely for retriage recovery of a notice
    # this app auto-rejected for having no sector/owner (see
    # notices.auto_rejected_unowned, workflow.approvals.retriage_and_route).
    # A human's own REJECTED decision is otherwise still final in practice --
    # nothing else in the app ever attempts this transition.
    Status.REJECTED: {Status.TO_REVIEW},
    Status.PARKED: {Status.TO_REVIEW, Status.AWAITING_PHASE2_APPROVAL},
}


class InvalidTransition(ValueError):
    pass


def validate_transition(from_status: Status, to_status: Status) -> None:
    allowed = ALLOWED_TRANSITIONS.get(from_status, set())
    if to_status not in allowed:
        raise InvalidTransition(
            f"Cannot move a notice from {from_status.value} to {to_status.value}; "
            f"allowed: {sorted(s.value for s in allowed)}"
        )


@dataclass
class Notice:
    ref: str
    title: str
    buyer: str | None
    source: str
    notice_type: str | None
    uk_stage: str
    raw_json: str
    ocid: str | None = None
    sector: str | None = None
    owner: str | None = None
    indicative_value: str | None = None
    cpv_primary: str | None = None
    cpv_primary_inferred: bool = False
    cpv_additional: list[str] | None = None
    deadline: str | None = None
    cpv_primary_description: str | None = None
    supplier_name: str | None = None
    supplier_address: str | None = None
    # 2026-09-06: the winning supplier's own contactPoint, when the buyer's
    # award notice actually included one -- real data already published as
    # part of the official award notice (not scraped, not fabricated). No
    # website field exists in the source data at all, so there's no
    # supplier_website to add alongside these.
    supplier_contact_name: str | None = None
    supplier_contact_email: str | None = None
    supplier_contact_phone: str | None = None
    buyer_address: str | None = None
    buyer_contact_email: str | None = None
    buyer_region: str | None = None
    procurement_method: str | None = None
    procurement_method_details: str | None = None
    notice_url: str | None = None
    value_amount_gross: str | None = None
    above_threshold: bool | None = None
    main_procurement_category: str | None = None
    enquiry_period_end: str | None = None
    award_period_end: str | None = None
    submission_method_details: str | None = None
    submission_languages: str | None = None
    electronic_submission_policy: str | None = None
    procedure_features: str | None = None
    award_criteria_summary: str | None = None
    contract_start_date: str | None = None
    contract_max_extent_date: str | None = None
    renewal_description: str | None = None
    buyer_ppon: str | None = None
    buyer_website: str | None = None
    buyer_org_type: str | None = None
    conflicts_assessment: str | None = None
    bid_documents_json: str | None = None
    # The OCDS release's own publish timestamp (release["date"]) -- when the
    # buyer/portal actually published this notice, distinct from
    # notices.first_seen_at (when OUR sweep first pulled it in). Needed
    # (2026-08-09) for date-based reporting ("opportunities per day") that
    # reflects real publication activity, not our sweep cadence.
    published_at: str | None = None
