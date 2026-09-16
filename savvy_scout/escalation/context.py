import json
import re
import sqlite3
from datetime import datetime, timezone

MISSING = "—"
OWNER_NAMES = ("Mark", "Kanvesh", "Hammad")


def _json_value(value, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _display(value):
    return value if value not in (None, "") else MISSING


def _iso_date(value):
    return str(value)[:10] if value else MISSING


def derive_urgency(deadline, gate_outcomes, now=None):
    if any(gate["result"] == "FAIL" for gate in gate_outcomes):
        return "🔴 URGENT"
    if not deadline:
        return "🟢 Open"
    try:
        parsed = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        days = (parsed.date() - current.date()).days
        if days <= 7:
            return "🔴 URGENT"
        if days <= 14:
            return "🟡 Approaching"
    except ValueError:
        pass
    return "🟢 Open"


def _latest_gate_outcomes(conn, notice_id):
    run = conn.execute(
        "SELECT id FROM triage_runs WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()
    if not run:
        return []
    return [
        {"gate_name": row["gate_name"], "result": row["outcome"], "reason": _display(row["reason"])}
        for row in conn.execute(
            "SELECT gate_name, outcome, reason FROM gate_results WHERE triage_run_id = ? "
            "ORDER BY gate_number",
            (run["id"],),
        ).fetchall()
    ]


def _decision_context(conn, notice_id):
    owner_placeholders = ",".join("?" for _ in OWNER_NAMES)
    owner_decision = conn.execute(
        f"SELECT * FROM status_history WHERE notice_id = ? "
        f"AND from_status = 'AWAITING_PHASE2_APPROVAL' "
        f"AND to_status IN ('ESCALATED_TO_VICTORIA', 'REJECTED') "
        f"AND changed_by IN ({owner_placeholders}) ORDER BY id DESC LIMIT 1",
        (notice_id, *OWNER_NAMES),
    ).fetchone()
    victoria_decision = conn.execute(
        "SELECT * FROM status_history WHERE notice_id = ? "
        "AND from_status = 'ESCALATED_TO_VICTORIA' "
        "AND to_status IN ('APPROVED', 'REJECTED', 'PARKED') "
        "ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()

    if victoria_decision:
        stage = {"APPROVED": "GO", "REJECTED": "NO-GO", "PARKED": "Parked"}[victoria_decision["to_status"]]
    elif owner_decision and owner_decision["to_status"] == "REJECTED":
        stage = "NO-GO"
    else:
        stage = "Escalated"
    return owner_decision, victoria_decision, stage


def _value_and_currency(notice):
    text = notice["indicative_value"]
    if text in (None, "", "0", "0.0"):
        return MISSING, MISSING
    currency_match = re.search(r"\b(GBP|EUR|USD)\b", str(text).upper())
    currency = currency_match.group(1) if currency_match else "GBP"
    number_match = re.search(r"\d[\d,]*(?:\.\d+)?", str(text))
    value = number_match.group(0).replace(",", "") if number_match else str(text)
    return value, currency


def build_context(conn: sqlite3.Connection, notice_id: int) -> dict:
    notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if notice is None:
        raise ValueError(f"No notice with id {notice_id}")
    assessment = conn.execute(
        "SELECT * FROM phase2_assessments WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()
    gates = _latest_gate_outcomes(conn, notice_id)
    owner_decision, victoria_decision, stage = _decision_context(conn, notice_id)
    briefs = {
        row["brief_type"]: row["docx_path"]
        for row in conn.execute(
            "SELECT brief_type, docx_path FROM escalation_briefs WHERE notice_id = ? ORDER BY id",
            (notice_id,),
        ).fetchall()
    }
    value, currency = _value_and_currency(notice)
    cpv_codes = [notice["cpv_primary"]] + _json_value(notice["cpv_additional"], [])
    cpv_codes = list(dict.fromkeys(code for code in cpv_codes if code))

    if assessment:
        ai_read = {
            "capability_fit": _display(assessment["capability_fit_rating"]),
            "competitor_position": _display(assessment["competitor_position_rating"]),
            "right_to_win": _display(assessment["right_to_win_rating"]),
            "overall": _display(assessment["overall_rating"]),
            "per_field_reasoning": {
                "capability_fit": _display(assessment["capability_fit_reasoning"]),
                "competitor_position": _display(assessment["competitor_position_reasoning"]),
                "right_to_win": _display(assessment["right_to_win_reasoning"]),
                "overall": _display(assessment["overall_reasoning"]),
            },
            "open_questions": _json_value(assessment["open_questions"], []),
        }
        blockers = _json_value(assessment["blockers"], [])
        asks = _json_value(assessment["asks"], [])
        recommendation = _json_value(assessment["recommendation"], {})
        capability_mapping = _json_value(assessment["capability_mapping"], [])
        positioning_points = _json_value(assessment["positioning_points"], [])
        executive_summary = _json_value(assessment["executive_summary"], {})
        key_terms = _json_value(assessment["key_terms"], [])
        scope_of_requirement = _json_value(assessment["scope_of_requirement"], {})
        engagement_model = _json_value(assessment["engagement_model"], {})
        procurement_timetable = _json_value(assessment["procurement_timetable"], [])
        decision_framework = _json_value(assessment["decision_framework"], [])
        solo_or_partner = _json_value(assessment["solo_or_partner_recommendation"], {})
        med_dual_reading = _json_value(assessment["med_dual_reading"], {})
    else:
        ai_read = {
            "capability_fit": MISSING,
            "competitor_position": MISSING,
            "right_to_win": MISSING,
            "overall": MISSING,
            "per_field_reasoning": {},
            "open_questions": [],
            "status": "No AI read on file",
        }
        blockers, asks, recommendation = [], [], {}
        capability_mapping, positioning_points = [], []
        executive_summary, key_terms, scope_of_requirement, engagement_model = {}, [], {}, {}
        procurement_timetable, decision_framework = [], []
        solo_or_partner = {}
        med_dual_reading = {}

    framework_gate = next((gate for gate in gates if "framework" in gate["gate_name"].casefold()), None)
    generated_at = datetime.now(timezone.utc).isoformat()
    return {
        "notice_id": notice_id,
        "title": _display(notice["title"]),
        "buyer": _display(notice["buyer"]),
        "buyer_org_type": _display(notice["buyer_org_type"]),
        "source_portal": _display(notice["source"]),
        "notice_type": _display(notice["notice_type"]),
        "uk_stage": _display(notice["uk_stage"]),
        "notice_reference": _display(notice["ref"]),
        "notice_url": _display(notice["notice_url"]),
        "notice_text": _display(notice["notice_description"] or notice["text_blob"]),
        "sector": _display(notice["sector"]),
        "cpv_codes": cpv_codes,
        "value_estimate": value,
        "currency": currency,
        "route_to_market": _display(notice["procurement_method_details"] or notice["procurement_method"]),
        "framework_status": framework_gate["reason"] if framework_gate else MISSING,
        "buyer_contact": _display(notice["buyer_contact_email"]),
        "date_spotted": _iso_date(notice["first_seen_at"]),
        "published_date": _iso_date(notice["published_at"]),
        "clarification_deadline": _iso_date(notice["enquiry_period_end"]),
        "submission_deadline": _iso_date(notice["deadline"]),
        "victoria_decision_date": _iso_date(victoria_decision["changed_at"] if victoria_decision else None),
        "gate_outcomes": gates,
        "ai_read": ai_read,
        "blockers_risks": blockers,
        "direct_asks": asks,
        "capability_mapping": capability_mapping,
        "positioning_points": positioning_points,
        "immediate_actions": recommendation.get("immediate_actions", []),
        "recommendation_rationale": _display(recommendation.get("rationale")),
        "executive_summary_ai": executive_summary,
        "key_terms": key_terms,
        "scope_of_requirement": scope_of_requirement,
        "engagement_model": engagement_model,
        "procurement_timetable_ai": procurement_timetable,
        "decision_framework": decision_framework,
        "solo_or_partner_recommendation": solo_or_partner,
        "med_dual_reading": med_dual_reading,
        "recommended_next_action": _display(
            recommendation.get("decision") or (owner_decision["reason"] if owner_decision else None)
        ),
        "urgency": derive_urgency(notice["deadline"], gates),
        "owner_name": _display(notice["owner"]),
        "escalated_by": _display(owner_decision["changed_by"] if owner_decision else None),
        "escalated_at": _display(owner_decision["changed_at"] if owner_decision else None),
        "stage": stage,
        "addendum_link": briefs.get("INTERNAL_ADDENDUM", MISSING),
        "internal_brief_link": briefs.get("INTERNAL_BRIEF", MISSING),
        "capture_brief_link": briefs.get("CAPTURE_BRIEF", ""),
        "generated_at": generated_at,
    }