"""Phase 1 triage gates (SPEC.md A3), five gates plus Filter 3.

Authoritative gate order (SavvyScout_Gate_Logic_Final.md, 2026-08-15,
supersedes every earlier gate-order document -- three different orders were
in use before this one; this is the one to trust):

  Gate 1  Buyer in scope     -- contracting authority against sector buyer lists
  Gate 2  CPV codes          -- notice CPV codes against verified/inferred/disqualifier lists
  Gate 3  Framework status   -- open procurement, call-off, or establishment
  Gate 4  Window             -- is the bidding/engagement window still open
  Gate 5  Sector boundary    -- boundary rules where a buyer sits between two desks

- Gate 1 owner map: Energy = Mark. Ambiguous or contested sector = FLAG to
  Victoria (Maddy routing from the references is retired, Maddy left the
  team 2026-07-15). Rail and Transport = Mark. A bare industry keyword with
  no product/capability coupling term (2026-07-21 update) is out of sector
  and FAILs, no owner assigned -- superseding the earlier "never fail if
  unsure" FLAG-to-Victoria call for this specific case. Still gets a human
  double-check like every other FAIL (2026-07-28 clarification: the gates
  can misclassify, so no FAIL skips owner review, this one included).
- Gate 2, CPV codes (2026-08-15, final correction -- explicitly does NOT
  adopt a default-PASS loosening here, unlike Gate 3 below): CPV alone is
  not reliable enough to carry a blanket PASS. FAIL only on a DISQUALIFIERS
  list match (33xxxx/32xxxx/85xxxx). PASS on a VERIFIED match, PASS+INFERRED
  on an INFERRED match. 48xxxxxx packaged-software codes PASS only against a
  named Trifork product (Corax, Tiris Messenger), otherwise FLAG. FLAG, do
  not rate, on any CPV that matches no list at all -- never default to PASS
  on an unresolved CPV; that discards a real signal that the requirement
  doesn't sit cleanly in the account's mapped categories.
- Gate 3, Framework status (2026-08-15, Mark's correction, load-bearing):
  default is PASS. Most notices don't narrate their own procurement
  mechanics; that silence is normal, not evidence of a blocking framework.
  Only FAILs on a confirmed, named, inaccessible framework call-off.
- Gate 5, Sector boundary: boundary rules for a notice whose type of work is
  unclear from its own text -- FAILs on a bare industry keyword with no
  digital/capability coupling, PASSes on an unconditional-pass term or a
  properly-coupled generic term, FLAGs (routes to a human) when neither is
  present. Never defaults to PASS on unclear text alone.
- All gates always run and record a result; no short-circuit on first FAIL.
  The first non-PASS gate result, in gate order, is the headline outcome.
- Filter 3 (scale/SI-prime dominance) is a separately agreed rule (15 June
  2026), distinct from the "no minimum value floor" non-negotiable, and is
  config-driven with an enabled/disabled toggle.
"""

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from savvy_scout.logging_util import log_audit, log_status_change
from savvy_scout.models.notice import Status, validate_transition
from savvy_scout.notifications import (
    NotificationError,
    send_new_opportunity_email,
    send_new_opportunity_teams_message,
)
from savvy_scout.triage.sector_classifier import (
    classify_general_market_sector,
    classify_sector,
    contains_keyword,
    is_contested,
    uncoupled_candidate_sectors,
)

logger = logging.getLogger(__name__)

GATE_ORDER = ["gate1", "gate2", "gate3", "gate4", "gate5", "filter3"]

GATE_NAMES = {
    "gate1": "Buyer in scope",
    "gate2": "CPV codes",
    "gate3": "Framework status",
    "gate4": "Window",
    "gate5": "Sector boundary",
    "filter3": "Scale and incumbents",
}

AIRPORT_TERMS = [
    "airport",
    "air traffic control",
    "national air traffic",
    "defence aviation",
    "military aviation",
    "raf base",
]


@dataclass
class GateResult:
    outcome: str
    reason: str
    extra: dict = field(default_factory=dict)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _extract_numeric_value(indicative_value: str | None) -> float | None:
    if not indicative_value:
        return None
    numbers = re.findall(r"[\d.]+", indicative_value.replace(",", ""))
    if not numbers:
        return None
    return max(float(n) for n in numbers)


def _is_airport_or_defence_aviation(buyer: str | None, text_blob: str) -> bool:
    haystack = f"{(buyer or '').lower()}\n{text_blob.lower()}"
    return any(contains_keyword(haystack, term) for term in AIRPORT_TERMS)


def gate1_sector_owner(
    conn: sqlite3.Connection, buyer: str | None, text_blob: str, cpv_primary: str | None = None
) -> GateResult:
    if _is_airport_or_defence_aviation(buyer, text_blob):
        return GateResult(
            "FAIL",
            "Airport-side, air traffic control or defence aviation; Trifork's aviation "
            "scope is airlines only.",
        )

    if is_contested(conn, buyer, text_blob):
        return GateResult(
            "FLAG",
            "Buyer/notice text matches more than one configured sector's keywords; "
            "ambiguous or contested, escalate to Victoria.",
        )

    sector = classify_sector(conn, buyer, text_blob)
    is_general_market = False
    if sector is None:
        candidates = uncoupled_candidate_sectors(conn, buyer, text_blob)
        if candidates:
            return GateResult(
                "FAIL",
                f"Buyer/notice mentions the {', '.join(sorted(candidates))} industry, but no "
                "digital/software product or capability signal was found alongside it; "
                "out of sector. Still routed to TO_REVIEW like any other FAIL, since this "
                "gate's keyword match can be wrong -- see triage_notice.",
            )
        # 2026-09-21: no Trifork sector keyword matched at all -- fall back to
        # a CPV-division-based general-market sector name (see
        # sector_classifier.classify_general_market_sector) so the
        # general/admin market view has a real sector label instead of
        # NULL/"Unclassified". This never overrides a real keyword match.
        sector = classify_general_market_sector(conn, cpv_primary)
        is_general_market = sector is not None
        if sector is None:
            return GateResult(
                "FAIL",
                "No configured sector keyword matched this buyer or notice text; "
                "obviously out of scope (none of the four confirmed sectors, NHS/healthcare, "
                "or Central/Local Government).",
            )

    owner_row = conn.execute(
        "SELECT owner FROM config_owner_map WHERE sector = ?", (sector,)
    ).fetchone()
    owner = owner_row["owner"] if owner_row else None
    if not owner:
        if is_general_market:
            # A CPV-derived general-market sector with no owner is the
            # routine, expected case (nobody's bought into this sector yet) --
            # not a Trifork config gap. Auto-close cheaply here (real sector
            # persisted via extra, no owner) rather than following the FLAG
            # path below, which routes to PHASE2_SCOPED with no reachable UI
            # to ever process or auto-reject it once there.
            return GateResult(
                "FAIL",
                f"General-market sector '{sector}' identified via CPV code -- no client "
                "currently owns this sector, recorded for visibility and auto-closed rather "
                "than left for a review nobody's assigned to do.",
                extra={"sector": sector},
            )
        return GateResult(
            "FLAG",
            f"Sector '{sector}' matched but has no owner configured; escalate to Victoria.",
        )

    return GateResult(
        "PASS",
        f"Sector: {sector}. Owner: {owner}.",
        extra={"sector": sector, "owner": owner},
    )


def _sector_cpv_scope(conn: sqlite3.Connection, sector: str | None) -> list[str] | None:
    """Returns the allowed CPV prefixes for a sector with an enabled
    config_sector_cpv_scope row, or None if that sector isn't scoped (should
    fall back to the global config_cpv_lists lookup)."""
    if not sector:
        return None
    row = conn.execute(
        "SELECT allowed_cpv_prefixes FROM config_sector_cpv_scope WHERE sector = ? AND enabled = 1",
        (sector,),
    ).fetchone()
    if not row:
        return None
    return json.loads(row["allowed_cpv_prefixes"])


def gate2_type_of_work(
    conn: sqlite3.Connection,
    text_blob: str,
    cpv_primary: str | None = None,
    cpv_primary_inferred: bool = False,
    cpv_additional: list[str] | None = None,
    sector: str | None = None,
) -> GateResult:
    """2026-08-15, Mark's correction (Trifork scouting skill v0.3, Section 3
    Gate 2, load-bearing): default is PASS. A 111-notice triage run produced
    zero clean Phase 1 passes because this gate required exact matching
    language (an unconditional-pass term, or a generic term specifically
    paired with a coupling term) before granting PASS -- "evidence required
    to pass" logic, backwards from the account principle that only three
    things fail an opportunity: wrong type of work, an inaccessible
    framework, a closed window. Now: any digital/software/data/platform
    signal at all (an unconditional-pass term, a generic term on its own,
    or a capability/product coupling term on its own) is sufficient for
    PASS. Only a genuinely non-digital notice -- no text signal AND no
    supporting CPV evidence -- gets FLAGged for a human type-of-work call.
    A CPV DISQUALIFIER match (33xxx/32xxx) is still the only CPV-based FAIL;
    a sector-CPV-scope mismatch alone no longer fails, it's corroboration
    only, same tier as an unclassified CPV."""
    if cpv_additional is None:
        cpv_additional = []

    terms = conn.execute("SELECT term, category FROM config_gate2_terms").fetchall()

    fail_matches = [
        t["term"] for t in terms if t["category"] == "fail" and contains_keyword(text_blob, t["term"])
    ]
    if fail_matches:
        return GateResult("FAIL", f"Matched fail term(s): {', '.join(fail_matches)} -- wrong type of work.")

    pass_matches = [
        t["term"] for t in terms
        if t["category"] == "unconditional_pass" and contains_keyword(text_blob, t["term"])
    ]
    generic_matches = [
        t["term"] for t in terms
        if t["category"] == "generic_needs_coupling" and contains_keyword(text_blob, t["term"])
    ]
    coupling_rows = conn.execute("SELECT term FROM config_coupling_terms").fetchall()
    coupled_terms = [r["term"] for r in coupling_rows if contains_keyword(text_blob, r["term"])]
    has_text_signal = bool(pass_matches or generic_matches or coupled_terms)
    signal_reason = (
        f"Digital/software signal: unconditional pass term(s) {pass_matches}, generic term(s) "
        f"{generic_matches}, capability/product term(s) {coupled_terms}."
        if has_text_signal
        else "No digital, software, data, or platform signal found in the notice text."
    )

    # Compatibility path for older callers/tests that only exercise the text
    # heuristic. Real notice triage passes CPV evidence in, so this preserves
    # the current app path while keeping the legacy helper surface working.
    if cpv_primary is None and cpv_primary_inferred is False and not cpv_additional:
        return GateResult("PASS" if has_text_signal else "FLAG", signal_reason)

    # CPV evidence: the global verified/inferred/disqualifier/48xxx-product
    # check (_lookup_cpv) is always authoritative for a CPV-based FAIL.
    # Sector CPV scope is corroboration only -- it can support a PASS but
    # never itself produce a FAIL (Mark's correction above).
    cpv_disqualified = False
    cpv_corroborates_pass = False
    cpv_reason = "No CPV code present on this notice."
    if cpv_primary:
        cpv_outcome, cpv_reason = _lookup_cpv(conn, cpv_primary, text_blob)
        if cpv_primary_inferred:
            cpv_reason += " (primary CPV inferred)"
        if cpv_outcome == "FAIL":
            cpv_disqualified = True
        elif cpv_outcome in ("PASS", "INFERRED_FIT"):
            cpv_corroborates_pass = True
        elif not _cpv_has_explicit_entry(conn, cpv_primary):
            # Sector-scope corroboration only applies to a genuinely
            # unclassified CPV -- never overrides a FLAG that
            # config_cpv_lists deliberately assigned for a specific reason
            # (e.g. bare 48xxxxxx with no named Trifork product match,
            # which must stay FLAG, never a blanket pass).
            scoped_prefixes = _sector_cpv_scope(conn, sector)
            if scoped_prefixes is not None:
                allowed_display = ", ".join(f"{p}xxx" for p in scoped_prefixes)
                if any(cpv_primary.startswith(p) for p in scoped_prefixes):
                    cpv_corroborates_pass = True
                    cpv_reason += f" (in {sector}'s configured range: {allowed_display})"
                else:
                    cpv_reason += (
                        f" (outside {sector}'s configured range {allowed_display}; not itself "
                        "disqualifying, per Mark's 15 August 2026 correction)"
                    )

    if cpv_disqualified:
        return GateResult("FAIL", f"CPV disqualifier: {cpv_reason}")

    if has_text_signal or cpv_corroborates_pass:
        return GateResult("PASS", f"{signal_reason} CPV evidence: {cpv_reason}")

    return GateResult(
        "FLAG",
        f"{signal_reason} No supporting CPV match either; type of work unclear. CPV evidence: {cpv_reason}",
    )


def gate5_sector_boundary(conn: sqlite3.Connection, text_blob: str) -> GateResult:
    """Gate 5, Sector boundary (SavvyScout_Gate_Logic_Final.md, 2026-08-15):
    boundary rules for a notice whose type of work is unclear from its own
    text. Deliberately does NOT default to PASS on unclear text -- that
    loosening was only agreed for Gate 3 (Framework status), never here;
    see the module docstring.

    FAIL on a bare industry keyword with no digital/capability coupling, or
    an explicit fail term. PASS on an unconditional-pass term, or a generic
    term properly coupled to a sector/capability term. FLAG (route to a
    human) when neither is present -- type of work genuinely unclear."""
    if not text_blob:
        return GateResult("FLAG", "No notice text to evaluate; type of work unclear.")

    terms = conn.execute("SELECT term, category FROM config_gate2_terms").fetchall()

    fail_matches = [t["term"] for t in terms if t["category"] == "fail" and contains_keyword(text_blob, t["term"])]
    if fail_matches:
        return GateResult("FAIL", f"Matched fail term(s): {', '.join(fail_matches)} -- wrong type of work.")

    pass_matches = [
        t["term"] for t in terms
        if t["category"] == "unconditional_pass" and contains_keyword(text_blob, t["term"])
    ]
    if pass_matches:
        return GateResult("PASS", f"Matched unconditional pass term(s): {', '.join(pass_matches)}.")

    generic_matches = [
        t["term"] for t in terms
        if t["category"] == "generic_needs_coupling" and contains_keyword(text_blob, t["term"])
    ]
    if generic_matches:
        coupling_rows = conn.execute("SELECT term FROM config_coupling_terms").fetchall()
        coupled_terms = [r["term"] for r in coupling_rows if contains_keyword(text_blob, r["term"])]
        if coupled_terms:
            return GateResult(
                "PASS",
                f"Generic term(s) {generic_matches} coupled with sector/capability "
                f"term(s): {coupled_terms}.",
            )
        return GateResult(
            "FLAG",
            f"Generic term(s) {generic_matches} present with no sector or capability "
            "coupling; not auto-passed on generic language alone.",
        )

    return GateResult(
        "FLAG",
        "No Gate 5 keyword matched (pass, fail, or generic); type of work unclear, do not rate.",
    )


def gate3_notice_stage(uk_stage: str) -> GateResult:
    if uk_stage in ("UK1", "UK2", "UK3", "UK4"):
        return GateResult("PASS", f"Live pre-award stage ({uk_stage}).")
    if uk_stage == "UK5":
        return GateResult("FAIL", "Award stage (UK5); route to closed/awarded handling.")
    return GateResult("FLAG", "UK stage is UNVERIFIED or not recognized; carry as open question to Phase 2.")


def gate5_framework(conn: sqlite3.Connection, text_blob: str, uk_stage: str) -> GateResult:
    """2026-08-15, Mark's correction (Trifork scouting skill v0.3, Section 3
    Gate 3, load-bearing): default is PASS. Most notices do not narrate
    their own procurement mechanics; that silence is normal, not evidence
    of a blocking framework -- the previous MAYBE (UK1/UK2)/FLAG (otherwise)
    default on no framework language was "evidence required to pass" logic,
    backwards from the account principle that only a confirmed, named,
    inaccessible framework fails this gate. uk_stage is kept as a parameter
    for backward compatibility with callers, but no longer changes the
    outcome here."""
    keywords = conn.execute("SELECT term, category FROM config_framework_keywords").fetchall()

    call_off_matches = [
        k["term"] for k in keywords if k["category"] == "call_off" and contains_keyword(text_blob, k["term"])
    ]
    establishment_matches = [
        k["term"] for k in keywords
        if k["category"] == "establishment" and contains_keyword(text_blob, k["term"])
    ]
    direct_matches = [
        k["term"] for k in keywords if k["category"] == "direct" and contains_keyword(text_blob, k["term"])
    ]

    if call_off_matches:
        frameworks = conn.execute("SELECT framework_name FROM config_trifork_frameworks").fetchall()
        on_framework = any(contains_keyword(text_blob, f["framework_name"]) for f in frameworks)
        if on_framework:
            return GateResult(
                "PASS",
                f"Framework call-off detected ({', '.join(call_off_matches)}), Trifork is "
                "confirmed on the named framework.",
            )
        return GateResult(
            "FAIL",
            f"Framework call-off detected ({', '.join(call_off_matches)}); Trifork not "
            "yet on framework.",
        )

    if establishment_matches:
        return GateResult(
            "PASS", f"Framework establishment bid detected ({', '.join(establishment_matches)})."
        )

    if direct_matches:
        return GateResult("PASS", f"Direct open procurement detected ({', '.join(direct_matches)}).")

    return GateResult(
        "PASS",
        "No framework or direct-procurement language detected; treated as open by default "
        "per Mark's 15 August 2026 correction -- absence of procurement-route jargon is not "
        "evidence of a blocking framework.",
    )


def gate3_framework(conn: sqlite3.Connection, text_blob: str, uk_stage: str) -> GateResult:
    """Backward-compatible alias for older tests and callers."""
    return gate5_framework(conn, text_blob, uk_stage)


def gate5_cpv(
    conn: sqlite3.Connection,
    cpv_primary: str | None,
    cpv_primary_inferred: bool,
    cpv_additional: list[str],
    text_blob: str,
) -> GateResult:
    """Backward-compatible CPV helper used by older tests and callers.

    The current app folds CPV evidence into Gate 2, but this helper still
    returns the CPV-only verdict so older callers keep working.
    """
    if not cpv_primary:
        return GateResult("FLAG", "No CPV code present on this notice; unlisted, do not rate.")

    primary_outcome, primary_reason = _lookup_cpv(conn, cpv_primary, text_blob)
    if cpv_primary_inferred:
        primary_reason += " (primary CPV inferred)"

    conflicting_additional: list[str] = []
    for code in cpv_additional:
        if code == cpv_primary:
            continue
        additional_outcome, _additional_reason = _lookup_cpv(conn, code, text_blob)
        if additional_outcome != primary_outcome:
            conflicting_additional.append(f"{code} ({additional_outcome})")

    if conflicting_additional:
        return GateResult(
            "FLAG",
            f"Primary CPV {cpv_primary}: {primary_reason}; conflicting additional CPV(s): "
            f"{', '.join(conflicting_additional)}.",
        )

    return GateResult(primary_outcome, primary_reason)


def gate4_window(
    tender_status: str | None,
    lot_statuses: list[str],
    tender_period_end: str | None,
    pme_due_date: str | None,
    future_notice_date: str | None,
) -> GateResult:
    now = datetime.now(timezone.utc)
    closed_statuses = {"complete", "cancelled", "withdrawn", "unsuccessful"}

    future_dt = _parse_date(future_notice_date)
    stage_still_ahead = bool(future_dt and future_dt.astimezone(timezone.utc) > now)

    is_closed = tender_status in closed_statuses or any(s in closed_statuses for s in lot_statuses)
    if is_closed:
        if stage_still_ahead:
            return GateResult(
                "MONITOR",
                f"Closed or awarded, but a further notice is expected ({future_notice_date}); "
                "tender stage still ahead.",
            )
        return GateResult("FAIL", f"Closed or awarded (status: {tender_status or 'UNVERIFIED'}).")

    deadline = tender_period_end or pme_due_date
    deadline_dt = _parse_date(deadline)
    if deadline_dt and deadline_dt.astimezone(timezone.utc) < now:
        if stage_still_ahead:
            return GateResult(
                "MONITOR",
                f"Deadline {deadline} has passed but tender stage still ahead per "
                f"{future_notice_date}.",
            )
        return GateResult(
            "FLAG",
            f"Deadline {deadline} has passed and no further stage is stated; status "
            "unclear, do not guess.",
        )

    return GateResult(
        "PASS", f"Open (status: {tender_status or 'UNVERIFIED'}, deadline: {deadline or 'none stated'})."
    )


def _cpv_has_explicit_entry(conn: sqlite3.Connection, code: str) -> bool:
    """True if config_cpv_lists has any row (exact code or 2-digit prefix)
    for this CPV -- distinguishes a genuinely unclassified CPV from one
    _lookup_cpv deliberately returned FLAG for (e.g. bare 48xxxxxx with no
    named product match), which must never be overridden by sector-scope
    corroboration alone."""
    row = conn.execute(
        "SELECT 1 FROM config_cpv_lists WHERE cpv_code = ? OR cpv_code = ? LIMIT 1",
        (code, code[:2]),
    ).fetchone()
    return row is not None


def _lookup_cpv(conn: sqlite3.Connection, code: str, text_blob: str) -> tuple[str, str]:
    exact_rows = conn.execute(
        "SELECT list_type, condition_keyword, notes FROM config_cpv_lists WHERE cpv_code = ?",
        (code,),
    ).fetchall()
    for row in exact_rows:
        if row["condition_keyword"]:
            if contains_keyword(text_blob, row["condition_keyword"]):
                return row["list_type"], (row["notes"] or "exact CPV match, condition met")
            continue
        return row["list_type"], (row["notes"] or "exact CPV list match")

    # Ordered so a conditioned row (e.g. 48xxxxxx + "corax") is checked
    # before the bare fallback row for the same prefix (e.g. plain 48xxxxxx
    # FLAG) -- condition_keyword IS NULL sorts last regardless of insertion
    # order, matching the exact-code loop's condition-first behaviour above.
    prefix_rows = conn.execute(
        "SELECT list_type, condition_keyword, notes FROM config_cpv_lists WHERE cpv_code = ? "
        "ORDER BY condition_keyword IS NULL",
        (code[:2],),
    ).fetchall()
    for row in prefix_rows:
        if row["condition_keyword"]:
            if contains_keyword(text_blob, row["condition_keyword"]):
                return row["list_type"], (row["notes"] or "prefix bucket match, condition met")
            continue
        return row["list_type"], (row["notes"] or "prefix bucket match")

    return "FLAG", "CPV code not in any documented list; unlisted, do not rate."


def filter3_scale(conn: sqlite3.Connection, indicative_value: str | None, text_blob: str) -> GateResult:
    config = conn.execute("SELECT * FROM config_scale_filter ORDER BY id DESC LIMIT 1").fetchone()
    if not config or not config["enabled"]:
        return GateResult("PASS", "Filter 3 is disabled.")

    amount = _extract_numeric_value(indicative_value)
    if amount is None or amount <= config["value_threshold"]:
        return GateResult(
            "PASS",
            f"Value not over Filter 3's £{config['value_threshold']:,.0f} threshold "
            f"(indicative value: {indicative_value or 'UNVERIFIED'}).",
        )

    si_primes = json.loads(config["si_prime_suppliers"])
    matched_primes = [p for p in si_primes if contains_keyword(text_blob, p)]
    if matched_primes:
        return GateResult(
            "FAIL",
            f"Over £{config['value_threshold']:,.0f} with a dominant global SI prime pool "
            f"mentioned ({', '.join(matched_primes)}). Filter 3, agreed {config['agreed_date']}.",
        )
    return GateResult(
        "PASS",
        f"Over £{config['value_threshold']:,.0f} but no global SI prime named; Filter 3 "
        "requires both conditions.",
    )


def run_gates(conn: sqlite3.Connection, notice_row: sqlite3.Row) -> dict[str, GateResult]:
    """Runs all five gates plus Filter 3, always, regardless of earlier
    outcomes (no short-circuit, per your decision)."""
    text_blob = notice_row["text_blob"] or ""
    cpv_additional = json.loads(notice_row["cpv_additional"]) if notice_row["cpv_additional"] else []
    lot_statuses = json.loads(notice_row["lot_statuses"]) if notice_row["lot_statuses"] else []

    results: dict[str, GateResult] = {}
    results["gate1"] = gate1_sector_owner(conn, notice_row["buyer"], text_blob, notice_row["cpv_primary"])
    results["gate2"] = gate5_cpv(
        conn,
        notice_row["cpv_primary"],
        bool(notice_row["cpv_primary_inferred"]),
        cpv_additional,
        text_blob,
    )
    results["gate3"] = gate5_framework(conn, text_blob, notice_row["uk_stage"])
    results["gate4"] = gate4_window(
        notice_row["tender_status"],
        lot_statuses,
        notice_row["tender_period_end"],
        notice_row["pme_due_date"],
        notice_row["future_notice_date"],
    )
    results["gate5"] = gate5_sector_boundary(conn, text_blob)
    results["filter3"] = filter3_scale(conn, notice_row["indicative_value"], text_blob)
    return results


def headline_outcome(results: dict[str, GateResult]) -> tuple[str, str, str]:
    """A FAIL anywhere is the headline, first FAIL in gate order if several --
    it's a stronger, more conclusive signal than a FLAG/MONITOR, so it must
    win even if an earlier gate in GATE_ORDER only flagged. Absent any FAIL,
    the first non-PASS in gate order is the headline; legacy MAYBE values are
    normalized to FLAG so the system has no separate pursue state. All PASS
    means the notice passes Phase 1 clean."""
    normalized = {}
    for gate_key, result in results.items():
        outcome = result.outcome
        if outcome == "MAYBE":
            outcome = "FLAG"
        normalized[gate_key] = GateResult(outcome, result.reason, result.extra)

    for gate_key in GATE_ORDER:
        if gate_key not in normalized:
            continue
        if normalized[gate_key].outcome == "FAIL":
            return gate_key, "FAIL", normalized[gate_key].reason
    for gate_key in GATE_ORDER:
        if gate_key not in normalized:
            continue
        result = normalized[gate_key]
        if result.outcome != "PASS":
            return gate_key, result.outcome, result.reason
    return GATE_ORDER[0], "PASS", "All gates passed."


def _evaluate_and_record(
    conn: sqlite3.Connection, notice_id: int, notice_row: sqlite3.Row, actor: str, audit_action: str
) -> tuple[str, str, str, dict[str, GateResult]]:
    """Runs all gates, records a new triage_runs + gate_results set, and
    updates sector/owner from Gate 1 if it produced one. Shared by
    triage_notice (first pass) and retriage_notice (re-evaluation after a
    config correction). Does not touch notice status. Returns
    (headline_gate, headline_outcome, headline_reason, results) -- results is
    the full per-gate dict, so callers can inspect e.g. gate2's
    cpv_scope_fail marker without a second gates run."""
    results = run_gates(conn, notice_row)
    headline_gate, headline_out, headline_reason = headline_outcome(results)
    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        "INSERT INTO triage_runs (notice_id, headline_gate, headline_outcome, headline_reason, evaluated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (notice_id, headline_gate, headline_out, headline_reason, now),
    )
    triage_run_id = cursor.lastrowid

    for gate_key in GATE_ORDER:
        result = results[gate_key]
        conn.execute(
            "INSERT INTO gate_results "
            "(triage_run_id, notice_id, gate_number, gate_name, outcome, reason, evaluated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (triage_run_id, notice_id, gate_key, GATE_NAMES[gate_key], result.outcome, result.reason, now),
        )

    # Always set sector/owner to exactly what this evaluation found (never
    # COALESCE-preserve a stale value): on first triage sector/owner start
    # NULL so this is equivalent either way, but on a retriage after a
    # classification fix, a notice that no longer matches any sector must
    # actually clear back to NULL, not keep an outdated PASS.
    gate1_extra = results["gate1"].extra
    sector = gate1_extra.get("sector")
    owner = gate1_extra.get("owner")
    conn.execute(
        "UPDATE notices SET sector = ?, owner = ?, updated_at = ? WHERE id = ?",
        (sector, owner, now, notice_id),
    )

    conn.commit()
    log_audit(
        conn,
        "notice",
        str(notice_id),
        audit_action,
        actor,
        headline_reason,
        {"headline_gate": headline_gate, "headline_outcome": headline_out},
    )
    return headline_gate, headline_out, headline_reason, results


def _notify_owner_of_new_opportunity(conn: sqlite3.Connection, notice_id: int) -> None:
    """Best-effort email + Teams alert to the assigned sector owner
    (2026-08-09) -- fired once, from triage_notice's first pass only (never
    retriage_notice, so a config-correction re-evaluation doesn't re-notify
    an owner about a notice they've already seen). Never raises: a missing
    owner email/webhook, or a down SMTP server/Teams webhook, must not stop
    the sweep from triaging the rest of the batch."""
    notice = conn.execute(
        "SELECT ref, title, buyer, deadline, owner FROM notices WHERE id = ?", (notice_id,)
    ).fetchone()
    if not notice or not notice["owner"]:
        return
    user = conn.execute(
        "SELECT display_name, email, teams_webhook_url FROM users WHERE display_name = ?", (notice["owner"],)
    ).fetchone()
    if not user:
        return
    app_url = (os.environ.get("SAVVY_SCOUT_APP_BASE_URL") or "").rstrip("/")

    if user["email"]:
        try:
            send_new_opportunity_email(
                user["email"], user["display_name"], notice["ref"], notice["title"],
                notice["buyer"], notice["deadline"], app_url, notice_id,
            )
        except NotificationError as exc:
            logger.warning("Could not send new-opportunity email for notice %s: %s", notice_id, exc)
    else:
        logger.debug("No email on file for owner %r; skipping new-opportunity email.", notice["owner"])

    if user["teams_webhook_url"]:
        try:
            send_new_opportunity_teams_message(
                user["teams_webhook_url"], user["display_name"], notice["ref"], notice["title"],
                notice["buyer"], notice["deadline"], app_url, notice_id,
            )
        except NotificationError as exc:
            logger.warning("Could not post new-opportunity Teams message for notice %s: %s", notice_id, exc)
    else:
        logger.debug("No Teams webhook on file for owner %r; skipping new-opportunity Teams message.", notice["owner"])


def triage_notice(conn: sqlite3.Connection, notice_id: int, actor: str = "system_triage") -> str:
    """Runs Phase 1 triage on one notice, records every gate result, moves
    the notice from NEW to PHASE1_TRIAGED, then routes based on headline outcome:
    - PASS: skip owner review, go straight to PHASE2_SCOPED for the AI scope read
    - FLAG: also go to PHASE2_SCOPED, to gather more data before an
      escalation decision, rather than auto-escalating on the Phase 1 result alone
    - FAIL: go to TO_REVIEW for owner double-check -- UNLESS the FAIL needs no
      human judgment call: no sector/owner assigned at all (nobody to review
      it anyway), a Gate 3 UK5/award-stage FAIL (unambiguous), or a Gate 2
      sector-scoped CPV mismatch (a plain number comparison). Any of those
      auto-rejects instead (2026-07-28 decisions). A fuzzy FAIL with an owner
      still assigned (a Gate 2 text-based fail term, or the non-scoped global
      CPV list) still gets a real double-check -- those calls can be wrong.
    - MONITOR: go to MONITORING status
    Returns the headline outcome."""
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if notice_row is None:
        raise ValueError(f"No notice with id {notice_id}")

    headline_gate, headline_out, headline_reason, gate_results = _evaluate_and_record(
        conn, notice_id, notice_row, actor, "triage_run"
    )
    now = datetime.now(timezone.utc).isoformat()

    current_status = Status(notice_row["status"])
    if current_status == Status.NEW:
        validate_transition(current_status, Status.PHASE1_TRIAGED)
        conn.execute(
            "UPDATE notices SET status = ?, updated_at = ? WHERE id = ?",
            (Status.PHASE1_TRIAGED.value, now, notice_id),
        )
        conn.commit()
        log_status_change(conn, notice_id, current_status.value, Status.PHASE1_TRIAGED.value, actor, "Phase 1 triage complete")
        current_status = Status.PHASE1_TRIAGED

    # Route based on headline outcome per SPEC B1 + user policy:
    # - PASS: Phase 2 scope read
    # - FLAG: Phase 2 scope read (gather more data before escalating)
    # - FAIL: Owner double-check review (TO_REVIEW)
    # - MONITOR: Monitoring status (MONITORING)
    if headline_out == "PASS":
        next_status = Status.PHASE2_SCOPED
        reason = "Phase 1 passed all gates, proceeding to Phase 2 scope read"
    elif headline_out == "FLAG":
        next_status = Status.PHASE2_SCOPED
        reason = f"Phase 1 {headline_out} - proceeding to Phase 2 scope read for deeper assessment before escalation decision"
    elif headline_out == "FAIL":
        next_status = Status.TO_REVIEW
        reason = headline_reason  # Owner reviews the FAIL as double-check
    elif headline_out == "MONITOR":
        next_status = Status.MONITORING
        reason = headline_reason
    else:
        next_status = Status.TO_REVIEW
        reason = headline_reason

    validate_transition(current_status, next_status)
    conn.execute(
        "UPDATE notices SET status = ?, updated_at = ? WHERE id = ?",
        (next_status.value, now, notice_id),
    )
    conn.commit()
    log_status_change(conn, notice_id, current_status.value, next_status.value, actor, reason)

    # Auto-close a TO_REVIEW FAIL instead of waiting on a human, but only for
    # cases with no real judgment call involved:
    # - no sector/owner assigned: no queue filters by owner=NULL, so it would
    #   sit there invisibly forever otherwise.
    # - Gate 2 FAIL: pure CPV disqualifier list match (33xxx/32xxx/85xxx) --
    #   a deterministic list lookup, never a text heuristic (Gate 2 no
    #   longer looks at notice text at all per the final gate-order doc).
    # - Gate 3 FAIL: a named framework call-off Trifork isn't a member of --
    #   unambiguous.
    # - Gate 4 FAIL: only ever an already closed/awarded tender (complete,
    #   cancelled, withdrawn, unsuccessful, with no further stage expected)
    #   -- also unambiguous, nothing left to bid on.
    # A fuzzy FAIL still gets a real owner's double-check as before -- only
    # Gate 5 (Sector boundary) can FAIL on a text heuristic, and those calls
    # can be wrong.
    # Checked across ALL gates, not just whichever one won the headline --
    # a notice can independently FAIL two gates at once, and headline_outcome
    # only surfaces the first one in gate order. If ANY gate is a
    # deterministic FAIL, auto-reject regardless of which gate is headlined.
    refreshed = conn.execute("SELECT owner FROM notices WHERE id = ?", (notice_id,)).fetchone()
    is_deterministic_fail = (
        gate_results["gate2"].outcome == "FAIL"
        or gate_results["gate3"].outcome == "FAIL"
        or gate_results["gate4"].outcome == "FAIL"
    )
    if next_status == Status.TO_REVIEW and (refreshed["owner"] is None or is_deterministic_fail):
        validate_transition(Status.TO_REVIEW, Status.REJECTED)
        now2 = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE notices SET status = ?, auto_rejected_unowned = 1, updated_at = ? WHERE id = ?",
            (Status.REJECTED.value, now2, notice_id),
        )
        conn.commit()
        log_status_change(
            conn, notice_id, Status.TO_REVIEW.value, Status.REJECTED.value, actor,
            "Auto-rejected: no human judgment call needed here. " + headline_reason,
        )

    # No auto-escalation at Phase 1 - FLAGs get Phase 2 assessment first

    final_status = conn.execute("SELECT status FROM notices WHERE id = ?", (notice_id,)).fetchone()["status"]
    if final_status != Status.REJECTED.value:
        _notify_owner_of_new_opportunity(conn, notice_id)

    return headline_out


def retriage_notice(conn: sqlite3.Connection, notice_id: int, actor: str = "system_retriage") -> str:
    """Re-evaluates all gates for a notice after a config correction (e.g. a
    sector keyword fix), without moving its status. Deliberately does not
    touch status at all: routing a re-triaged notice onward (to MONITOR or
    ESCALATED_TO_VICTORIA, generating a fresh brief if warranted) is workflow
    orchestration, not gate logic, so it lives in
    workflow.approvals.retriage_and_route, not here.

    Only sane to call on a notice still in AWAITING_PHASE1_APPROVAL that no
    human has acted on yet; callers are responsible for that check (see
    workflow.approvals.retriage_and_route, which enforces it). Returns the
    new headline outcome."""
    notice_row = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if notice_row is None:
        raise ValueError(f"No notice with id {notice_id}")

    _, headline_out, _, _ = _evaluate_and_record(conn, notice_id, notice_row, actor, "retriage_run")
    return headline_out
