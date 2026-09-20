import json
from pathlib import Path

import pytest

from savvy_scout.models.notice import Notice, Status
from savvy_scout.sources.ocds_parser import ParsedNotice
from savvy_scout.sweep.dedupe import upsert_notice
from savvy_scout.triage.gates import triage_notice
from savvy_scout.workflow import approvals

VALID_ASSESSMENT = {
    "capability_fit": {"rating": "MED", "reasoning": "Transferable engineering fit."},
    "competitor_position": {"rating": "UNKNOWN", "reasoning": "No incumbent named."},
    "right_to_win": {"rating": "MED", "reasoning": "Plausible given the profile."},
    "overall": {"rating": "PURSUE", "reasoning": "Worth pursuing."},
    "open_questions": ["Confirm the deadline with the buyer."],
}


class FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, content_text):
        self.content = [FakeTextBlock(content_text)]
        self.stop_reason = "end_turn"


class FakeMessages:
    def create(self, **kwargs):
        return FakeResponse(json.dumps(VALID_ASSESSMENT))


class FakeClient:
    def __init__(self):
        self.messages = FakeMessages()


def _make_pass_notice(conn, ref="REF-APPR-PASS", title="Real Time Payments Platform", buyer="Some Bank"):
    notice = Notice(
        ref=ref,
        title=title,
        buyer=buyer,
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
        cpv_primary="72200000",
    )
    parsed = ParsedNotice(
        notice=notice,
        text_blob="bespoke build of a real-time payments platform, a direct award open tender",
        tender_status="active",
    )
    notice_id = upsert_notice(conn, parsed)
    triage_notice(conn, notice_id)  # clean PASS, owner = Mark -> straight to PHASE2_SCOPED
    return notice_id


def _make_flagged_notice(conn, ref="REF-APPR-FLAG"):
    # Matches both Fintech ("bank" + "payments platform" coupling) and Energy
    # ("energy" + "smart grid" coupling) -> contested -> Gate 1 FLAG.
    notice = Notice(
        ref=ref,
        title="Contested Sector Notice",
        buyer="Some Bank",
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
    )
    parsed = ParsedNotice(
        notice=notice,
        text_blob="real-time payments platform integration with smart grid billing systems "
        "for our energy trading desk",
        tender_status="active",
    )
    notice_id = upsert_notice(conn, parsed)
    triage_notice(conn, notice_id)  # gate1 FLAG (contested), no owner -> also straight to PHASE2_SCOPED
    return notice_id


def _make_fail_notice(conn, ref="REF-APPR-FAIL"):
    # Gate 1 passes cleanly (Energy/Mark) but Gate 2 fails on hardware/resale
    # language, which is the headline -> AWAITING_PHASE1_APPROVAL for Mark's
    # Phase 1 double-check (the one outcome that still stops in that queue).
    notice = Notice(
        ref=ref,
        title="Server Hardware Resale",
        buyer="National Grid",
        source="Find a Tender",
        notice_type="UK3",
        uk_stage="UK3",
        raw_json="{}",
    )
    parsed = ParsedNotice(
        notice=notice,
        text_blob="hardware resale of legacy servers for the energy network",
        tender_status="active",
    )
    notice_id = upsert_notice(conn, parsed)
    triage_notice(conn, notice_id)
    return notice_id


def _advance_to_phase2_queue(conn, notice_id):
    """Runs the automated Phase 2 scope read, exactly as the dashboard does
    on every queue page load, so a PASS/FLAG/MAYBE notice reaches
    AWAITING_PHASE2_APPROVAL -- the queue an owner can actually act from."""
    approvals.process_pending_phase2_scope_reads(conn, FakeClient())


def test_reject_notice_requires_reason(conn):
    notice_id = _make_pass_notice(conn)
    with pytest.raises(ValueError):
        approvals.reject_notice(conn, notice_id, "Mark", "account_user", "")


def test_reject_notice_enforces_ownership(conn):
    notice_id = _make_pass_notice(conn)  # owner is Mark
    with pytest.raises(approvals.NotAuthorized):
        approvals.reject_notice(conn, notice_id, "Kanvesh", "account_user", "Not my patch")


def test_reject_notice_victoria_can_always_act(conn):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)
    approvals.reject_notice(conn, notice_id, "Victoria", "account_approver", "Overridden")
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.REJECTED.value


def test_park_notice_requires_reason(conn):
    notice_id = _make_pass_notice(conn)
    with pytest.raises(ValueError):
        approvals.park_notice(conn, notice_id, "Mark", "account_user", "   ")


def test_flagged_notice_reaches_phase2_queue_for_owner_review(conn):
    # FLAG/MAYBE no longer auto-escalates the instant Phase 1 triage
    # finishes: it gets the same automated Phase 2 scope read as a PASS, so
    # the owner sees the Gate 1 flag alongside the AI read together and
    # decides for themselves whether to mark it for Victoria.
    notice_id = _make_flagged_notice(conn)
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.PHASE2_SCOPED.value

    _advance_to_phase2_queue(conn, notice_id)
    row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    # This one's a contested sector (matches both Fintech and Energy), so it
    # never got an owner -- nobody could review it in AWAITING_PHASE2_APPROVAL,
    # so it's auto-rejected instead, recoverable via retriage if the contest
    # is ever resolved (e.g. a config fix removes the ambiguity).
    assert row["status"] == Status.REJECTED.value
    assert row["auto_rejected_unowned"] == 1


def test_mark_victoria_decision_enforces_ownership(conn):
    notice_id = _make_pass_notice(conn)  # owner Mark
    with pytest.raises(approvals.NotAuthorized):
        approvals.mark_victoria_decision(conn, notice_id, "Kanvesh", "Not sure about this one")


def test_mark_victoria_decision_by_owner_escalates(conn, tmp_path):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)
    path = approvals.mark_victoria_decision(
        conn, notice_id, "Mark", "Want a second opinion", briefs_dir=str(tmp_path / "briefs")
    )
    assert path
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.ESCALATED_TO_VICTORIA.value


def test_mark_victoria_decision_refuses_before_phase2_scope_read(conn):
    # Owner should not be able to skip Phase 2 and send a raw Phase 1
    # notice straight to Victoria (2026-07-21 policy: too many un-scoped
    # leads were reaching her this way).
    notice_id = _make_fail_notice(conn)  # sits in TO_REVIEW, owner Mark
    with pytest.raises(ValueError):
        approvals.mark_victoria_decision(conn, notice_id, "Mark", "Second opinion")
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.TO_REVIEW.value


def test_mark_victoria_decision_refuses_at_phase2_scoped_not_yet_awaiting_approval(conn):
    # PHASE2_SCOPED means the scope read hasn't run yet (still queued);
    # escalation must wait for AWAITING_PHASE2_APPROVAL.
    notice_id = _make_pass_notice(conn)  # PHASE2_SCOPED, not yet advanced
    with pytest.raises(ValueError):
        approvals.mark_victoria_decision(conn, notice_id, "Mark", "Second opinion")


def test_advance_phase2_without_scope_read_moves_to_awaiting_approval(conn):
    notice_id = _make_pass_notice(conn)  # PHASE2_SCOPED, no AI read run
    approvals.advance_phase2_without_scope_read(conn, notice_id, "Mark")
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.AWAITING_PHASE2_APPROVAL.value
    # No AI assessment was created -- this is expected, not a data gap.
    assessment = conn.execute(
        "SELECT * FROM phase2_assessments WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    assert assessment is None


def test_advance_phase2_without_scope_read_refuses_wrong_status(conn):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)  # now AWAITING_PHASE2_APPROVAL
    with pytest.raises(ValueError):
        approvals.advance_phase2_without_scope_read(conn, notice_id, "Mark")


def test_advance_pending_phase2_without_scope_read_bulk(conn):
    id1 = _make_pass_notice(conn, ref="REF-BULK-1", title="Real Time Payments Platform", buyer="Some Bank")
    id2 = _make_pass_notice(conn, ref="REF-BULK-2", title="Cloud Migration Programme", buyer="Another Buyer")
    count = approvals.advance_pending_phase2_without_scope_read(conn, "system_phase2_manual")
    assert count == 2
    for notice_id in (id1, id2):
        row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
        assert row["status"] == Status.AWAITING_PHASE2_APPROVAL.value


def test_victoria_decision_requires_escalated_status(conn):
    notice_id = _make_pass_notice(conn)  # still PHASE2_SCOPED, not escalated
    with pytest.raises(ValueError):
        approvals.victoria_decision(conn, notice_id, "Victoria", "approve")


def test_victoria_decision_approve(conn, tmp_path):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)
    approvals.mark_victoria_decision(
        conn, notice_id, "Mark", "Want a second opinion", briefs_dir=str(tmp_path / "briefs")
    )
    artifacts = conn.execute(
        "SELECT brief_type, docx_path FROM escalation_briefs WHERE notice_id = ? ORDER BY brief_type",
        (notice_id,),
    ).fetchall()
    assert {row["brief_type"] for row in artifacts} == {
        "CAPTURE_BRIEF", "INTERNAL_ADDENDUM", "ORIGINAL_NOTICE"
    }
    assert all(Path(row["docx_path"]).exists() for row in artifacts)

    approvals.victoria_decision(conn, notice_id, "Victoria", "approve", "Looks fine")
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.CAPTURE_BRIEF_DRAFTED.value
    capture_count = conn.execute(
        "SELECT COUNT(*) FROM escalation_briefs WHERE notice_id = ? AND brief_type = 'CAPTURE_BRIEF'",
        (notice_id,),
    ).fetchone()[0]
    assert capture_count == 1


def test_victoria_decision_reject_requires_reason(conn, tmp_path):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)
    approvals.mark_victoria_decision(
        conn, notice_id, "Mark", "Want a second opinion", briefs_dir=str(tmp_path / "briefs")
    )
    with pytest.raises(ValueError):
        approvals.victoria_decision(conn, notice_id, "Victoria", "reject", None)


def test_approve_phase1_runs_scope_read_and_advances_status(conn):
    notice_id = _make_fail_notice(conn)
    fake_client = FakeClient()
    approvals.approve_phase1(conn, notice_id, "Mark", "account_user", fake_client)

    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.AWAITING_PHASE2_APPROVAL.value

    assessment_row = conn.execute(
        "SELECT * FROM phase2_assessments WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    assert assessment_row["overall_rating"] == "PURSUE"


def test_approve_phase1_enforces_ownership(conn):
    notice_id = _make_fail_notice(conn)  # owner Mark
    with pytest.raises(approvals.NotAuthorized):
        approvals.approve_phase1(conn, notice_id, "Hammad", "account_user", FakeClient())


def test_correct_pre_routing_fix_backlog_moves_stale_escalation_to_phase2(conn):
    notice_id = _make_flagged_notice(conn)  # today's code puts this at PHASE2_SCOPED
    # Force it back to ESCALATED_TO_VICTORIA to simulate a notice the retired
    # auto_escalate_if_flagged behaviour shipped straight to Victoria yesterday,
    # with no Victoria decision ever recorded against it.
    conn.execute("UPDATE notices SET status = 'ESCALATED_TO_VICTORIA' WHERE id = ?", (notice_id,))
    conn.commit()

    counts = approvals.correct_pre_routing_fix_backlog(conn)

    assert counts == {"checked": 1, "moved_to_phase2": 1, "unchanged": 0}
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.PHASE2_SCOPED.value


def test_correct_pre_routing_fix_backlog_leaves_correctly_routed_fail_alone(conn):
    notice_id = _make_fail_notice(conn)  # already correctly at AWAITING_PHASE1_APPROVAL

    counts = approvals.correct_pre_routing_fix_backlog(conn)

    assert counts == {"checked": 1, "moved_to_phase2": 0, "unchanged": 1}
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.AWAITING_PHASE1_APPROVAL.value


def test_correct_pre_routing_fix_backlog_ignores_notices_victoria_already_decided(conn):
    notice_id = _make_pass_notice(conn)
    _advance_to_phase2_queue(conn, notice_id)
    approvals.mark_victoria_decision(conn, notice_id, "Mark", "Second opinion please")
    approvals.victoria_decision(conn, notice_id, "Victoria", "approve", "Fine")

    counts = approvals.correct_pre_routing_fix_backlog(conn)

    assert counts == {"checked": 0, "moved_to_phase2": 0, "unchanged": 0}
    row = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()
    assert row["status"] == Status.CAPTURE_BRIEF_DRAFTED.value
