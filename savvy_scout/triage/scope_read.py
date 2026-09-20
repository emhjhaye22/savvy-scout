"""Phase 2 AI scope reads (SPEC.md B2). On Phase 1 approval, calls the Claude
API with the full notice text and the Trifork capability profile from config,
producing a structured provisional assessment. Single call, structured
output (not tool-use or an agentic loop): this is extraction/judgement
against one document, not multi-step work.

Model: claude-sonnet-5, per your cost/quality call (near-Opus quality on this
kind of structured judgement task, at roughly half the per-notice cost of
Opus 4.8, since this runs on every Phase 1 approval).

The "PROVISIONAL, FOR VALIDATION" label is applied by the application at
display/export time (dashboard templates, escalation brief), never trusted
from the model's own output, per SPEC.md non-negotiable 5."""

import json
import sqlite3
from datetime import datetime, timezone

import anthropic

MODEL = "claude-sonnet-5"
OPENAI_MODEL = "gpt-4o"
# 2026-09-20 fix: every other outbound network call in this app (tender
# source fetches, Microsoft Graph, SMTP) has an explicit 15-30s timeout --
# these two AI client constructions were the one exception, left at each
# SDK's own default (several minutes). A slow/unreachable provider would
# tie up one of Waitress's limited worker threads for that whole duration,
# backing up every other request behind it -- a real production incident,
# not a hypothetical.
AI_REQUEST_TIMEOUT_SECONDS = 90

SCOPE_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "capability_fit": {
            "type": "object",
            "properties": {
                "rating": {"type": "string", "enum": ["HIGH", "MED", "LOW"]},
                "reasoning": {"type": "string"},
            },
            "required": ["rating", "reasoning"],
            "additionalProperties": False,
        },
        "competitor_position": {
            "type": "object",
            "properties": {
                "rating": {"type": "string", "enum": ["STRONG", "CONTESTED", "WEAK", "UNKNOWN"]},
                "reasoning": {"type": "string"},
            },
            "required": ["rating", "reasoning"],
            "additionalProperties": False,
        },
        "right_to_win": {
            "type": "object",
            "properties": {
                "rating": {"type": "string", "enum": ["HIGH", "MED", "LOW"]},
                "reasoning": {"type": "string"},
            },
            "required": ["rating", "reasoning"],
            "additionalProperties": False,
        },
        "overall": {
            "type": "object",
            "properties": {
                "rating": {"type": "string", "enum": ["PURSUE", "FLAG", "DECLINE"]},
                "reasoning": {"type": "string"},
            },
            "required": ["rating", "reasoning"],
            "additionalProperties": False,
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
        # Written narrative for the Internal Addendum / Capture Brief,
        # addressed to Victoria as the person deciding GO / NO-GO / Park --
        # proper prose, not a copy of notice text. opening states who the
        # buyer is and what they want in one sentence; scope_summary
        # synthesises what's actually being asked for (not a line-by-line
        # dump of the notice); executive_view gives Victoria the read that
        # actually helps her decide.
        "executive_summary": {
            "type": "object",
            "properties": {
                "opening": {"type": "string"},
                "scope_summary": {"type": "string"},
                "executive_view": {"type": "string"},
            },
            "required": ["opening", "scope_summary", "executive_view"],
            "additionalProperties": False,
        },
        # A short glossary of jargon, acronyms or scheme names that actually
        # appear in the notice and that Victoria (not a procurement
        # specialist) would need explained to follow the brief -- e.g. LCCC,
        # CfD, PME, a named framework. Only terms genuinely present in the
        # notice text; do not invent a glossary where the notice is plain
        # English. An empty list is a valid result.
        "key_terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "meaning": {"type": "string"},
                },
                "required": ["term", "meaning"],
                "additionalProperties": False,
            },
        },
        # Section 5 breakdown ("Scope of Requirement"): distinct requirement
        # areas actually stated in the notice, each a short synthesised
        # phrase (e.g. "Settlement calculations, covering an increasing
        # scale and volume of transactions"), never a raw sentence lifted
        # verbatim from the notice text. what_buyer_is_seeking is one
        # sentence naming the underlying reason the buyer is going to
        # market (e.g. testing feasibility, exploring the market ahead of a
        # future procurement).
        "scope_of_requirement": {
            "type": "object",
            "properties": {
                "requirement_areas": {"type": "array", "items": {"type": "string"}},
                "what_buyer_is_seeking": {"type": "string"},
            },
            "required": ["requirement_areas", "what_buyer_is_seeking"],
            "additionalProperties": False,
        },
        # How a bidder actually engages with this procurement -- the
        # submission portal/route, response format, and any named contact
        # or supplier-engagement stage the notice describes. This is NOT the
        # opportunity's internal workflow stage (escalated/approved/etc) --
        # it is what the notice itself says about how to respond. Say
        # UNVERIFIED where the notice doesn't state it.
        "engagement_model": {
            "type": "object",
            "properties": {
                "model": {"type": "string"},
                "how_to_respond": {"type": "string"},
            },
            "required": ["model", "how_to_respond"],
            "additionalProperties": False,
        },
        # Concrete dated milestones stated in the notice (clarification
        # deadline, submission deadline, envisaged contract start, etc.),
        # beyond the dates already captured structurally. Use UNVERIFIED for
        # the date where the notice names the milestone but not a date. An
        # empty list is valid if the notice states nothing beyond the
        # structured deadline fields.
        "procurement_timetable": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "milestone": {"type": "string"},
                    "date": {"type": "string"},
                },
                "required": ["milestone", "date"],
                "additionalProperties": False,
            },
        },
        # Section 9 ("Solo or Partner Recommendation"): a genuine call on
        # whether Trifork should respond alone or with a delivery partner at
        # this stage, and why -- never a restatement of the overall PROCEED/
        # PARK/DECLINE recommendation or a "prepared by" administrative line.
        "solo_or_partner_recommendation": {
            "type": "object",
            "properties": {
                "recommendation": {"type": "string"},
                "rationale": {"type": "string"},
            },
            "required": ["recommendation", "rationale"],
            "additionalProperties": False,
        },
        # Section C / Section 6's dual reading for a MED capability_fit:
        # per Bid Savvy house style, a MED rating is genuinely ambiguous
        # between a build and a packaged-product purchase and must be
        # presented as two separate readings, not one averaged claim.
        # Populate all three properties always; when capability_fit is HIGH
        # or LOW there is no genuine ambiguity, so leave build_signals and
        # product_signals empty and honest_position empty too -- the
        # document only renders this section for a MED rating.
        "med_dual_reading": {
            "type": "object",
            "properties": {
                "build_signals": {"type": "array", "items": {"type": "string"}},
                "product_signals": {"type": "array", "items": {"type": "string"}},
                "honest_position": {"type": "string"},
            },
            "required": ["build_signals", "product_signals", "honest_position"],
            "additionalProperties": False,
        },
        # Genuine GO/NO-GO decision points for Victoria, each framed as a
        # question with what a yes/no answer implies for the recommendation
        # -- not a restatement of asks (which are questions FOR Trifork/the
        # buyer); these are questions Victoria herself needs to weigh.
        "decision_framework": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "implication": {"type": "string"},
                },
                "required": ["question", "implication"],
                "additionalProperties": False,
            },
        },
        # Internal Addendum Section C ("Why this is a high fit"): each
        # buyer-side problem paired with the specific Trifork capability/case
        # study that maps to it -- name real case studies from the profile
        # (ALai, &Money, Nordjyllandsfonden, AI Mail Assist, etc.), never a
        # generic capability-area label alone.
        "capability_mapping": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "problem": {"type": "string"},
                    "capability_mapping": {"type": "string"},
                },
                "required": ["problem", "capability_mapping"],
                "additionalProperties": False,
            },
        },
        # Where UK-newness genuinely matters to winning (reference-building,
        # partnering options, presenting European proof points to a UK
        # buyer) -- rendered as its own Internal Addendum section, between
        # capability mapping and blockers. NEVER treated as a blocker or
        # used to lower a rating; see rule 5 and rule 2's exclusion list.
        "positioning_points": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "point": {"type": "string"},
                    "how_to_address": {"type": "string"},
                },
                "required": ["point", "how_to_address"],
                "additionalProperties": False,
            },
        },
        # Section D blockers: ONLY three things, per the Trifork scouting
        # skill v2, Rule 1.1 (11 August 2026, Victoria Milan): wrong type of
        # work, a NAMED framework call-off Trifork is not a member of, or a
        # closed/awarded window. Nothing else counts, including a required
        # product Trifork lacks or a pass/fail certification -- narrower
        # than earlier drafts of this rule.
        # Do NOT include: UK track record, UK references, delivery capacity, staff
        # scale, turnover, or unconfirmed security clearance. Per Victoria Milan's
        # ruling of 11 August 2026 those are positioning points, not blockers.
        # An empty blockers array is a valid result.
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "blocker": {"type": "string"},
                    "assessment": {"type": "string"},
                },
                "required": ["blocker", "assessment"],
                "additionalProperties": False,
            },
        },
        # Section E: each open question paired with why it matters to the
        # decision, not just a bare list.
        "asks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ask": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                },
                "required": ["ask", "why_it_matters"],
                "additionalProperties": False,
            },
        },
        # Section F: an explicit recommendation with concrete next actions,
        # not just a rating.
        "recommendation": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["PROCEED", "DO_NOT_PROCEED", "PARK_FOR_MORE_INFO"]},
                "immediate_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "owner_and_deadline": {"type": "string"},
                        },
                        "required": ["action", "owner_and_deadline"],
                        "additionalProperties": False,
                    },
                },
                "rationale": {"type": "string"},
            },
            "required": ["decision", "immediate_actions", "rationale"],
            "additionalProperties": False,
        },
    },
    "required": [
        "capability_fit", "competitor_position", "right_to_win", "overall", "open_questions",
        "capability_mapping", "positioning_points", "blockers", "asks", "recommendation",
        "executive_summary", "key_terms", "scope_of_requirement", "engagement_model",
        "procurement_timetable", "decision_framework", "solo_or_partner_recommendation",
        "med_dual_reading",
    ],
    "additionalProperties": False,
}

SYSTEM_INSTRUCTIONS = """You are assisting Bid Savvy Solutions Ltd's Phase 2 scoping for Trifork UK
(Erlang Solutions Ltd). You will be given a UK public procurement notice's full text, its Phase 1
gate results, and Trifork's capability profile. Produce a structured provisional assessment only.

Context to hold throughout: Trifork is new to the UK market. Bid Savvy was engaged specifically to
build Trifork's UK presence from a standing start. Trifork having no UK track record is the premise
of the engagement, not a finding against an opportunity.

Rules:

1. Never invent a figure, contact, deadline or fact not present in the notice text given to you.
   Where a field is absent, say UNVERIFIED.

2. Rate capability_fit on engineering fit between the requirement and Trifork's capability. Do NOT
   reduce any rating because Trifork is new to the UK. The following must never lower
   capability_fit, right_to_win or the recommendation, and must never appear in blockers:
   - no UK delivery reference, no UK public sector reference, no sector reference
   - UK security clearance population not confirmed
   - absence of UK framework access in general, where the notice names no specific framework
     call-off
   - company size, staff count, turnover or scale
   - no prior relationship with this buyer
   Where any of these genuinely matters to winning, put it in positioning_points, never in
   blockers.

3. capability_fit uses the solution-open test. Cite the notice wording that drives the rating.
   HIGH: the notice leaves solution, vendor and delivery approach genuinely open, and the
   requirement maps to Trifork build capability.
   MED: real build content is present but so are packaged-product signals, for example a
   48xxxxxx CPV, "procure and implement", "provide, implement and support", a supplier
   demonstration stage, a licence-based user model, or language about evaluating what the market
   already offers. State both readings rather than averaging them into one claim.
   LOW: the notice contains too little to assess, or nothing maps.

4. blockers: only three things count as a blocker. Nothing else, ever:
   - wrong type of work: the buyer wants hardware, a packaged product, resale, or a managed
     service on a system Trifork did not build
   - a NAMED framework call-off Trifork is not a member of
   - a closed or awarded window
   If none of these apply, return an empty list. Do not manufacture blockers to appear thorough,
   and do not add a required product, a certification, or unpublished requirements as a blocker --
   those are not one of the three. An empty blockers list is a valid and useful result.

5. positioning_points: what a bid writer must handle to win. This is where UK-newness belongs:
   reference-building, partnering options, and how to present European proof points to a UK
   buyer. Frame each as something to address, never as a reason for doubt.

6. capability_mapping: pair each real buyer-side requirement stated in the notice with the
   SPECIFIC named Trifork case study or product that maps to it. Only four verified case studies
   exist, and no others: &Money (AI financial advisory platform used by Nykredit, AL Sydbank and
   BEC), Nordjyllandsfonden (AI document and application processing for grant administration),
   ALai (secure internal AI assistant for Arbejdernes Landsbank and AL Sydbank), AI Mail Assist
   (for OK a.m.b.a., energy sector). Every one of these is Danish, private or third-sector, in
   the roughly GBP 100,000 to GBP 300,000 range; never imply a UK public sector engagement or a
   larger value for any of them. NEVER name Vocalink, Visa, Danske Bank or any other case study --
   these have been used in error before and corrected, and are not verified proof points. The
   products that may be named instead: Corax (AI analytics and decision support, clinical and
   AI data), Tiris Messenger (secure operational and safety-critical messaging), iFly4 (airline
   operations), Trifork PIM (planning, optimisation, scenario analysis), LOFTHome, Synq. Never a
   generic label alone. If nothing in the profile genuinely maps to a requirement, say so plainly
   in that row rather than stretching a weak analogy.

7. competitor_position: assess only from what the notice reveals. Use UNKNOWN where no incumbent
   or competitive field is identifiable. Do not assume a strong competitive field exists in the
   absence of evidence.

8. right_to_win: base this on the type-of-work question, whether a named framework blocks
   access, and whether the requirement matches Trifork capability. Do not reduce it for UK
   newness.

9. open_questions: genuinely unresolved matters, chiefly whether the requirement is a build or a
   product purchase where the notice does not settle it. Not rhetorical questions.

10. asks: concrete questions FOR Trifork, routed via Victoria, each paired with why it matters to
    the decision.

11. recommendation:
    PROCEED where the type of work fits Trifork capability and the window is open.
    PARK_FOR_MORE_INFO where the notice is genuinely too early-stage or too thin to determine the
    type of work.
    DO_NOT_PROCEED only where the type of work is definitively wrong, a named framework blocks
    access, or the window is closed.
    Never recommend against, and never hedge, on the basis of UK newness, company size,
    references or clearance. Do not use the phrases "suggests reluctance", "may be at a
    disadvantage", "could weaken Trifork's bid", or any similar formulation where the stated
    reason is track record, size, references or clearance.

12. immediate_actions: concrete next steps, each paired with owner_and_deadline naming WHO does it
    and BY WHEN. owner_and_deadline is a role, never a person's own name -- "Trifork commercial
    lead with Bid Savvy support", "Trifork engineering and product leads", "Bid Savvy" -- paired
    with a real date where the notice gives one (e.g. the submission/engagement deadline), or a
    short relative window ("This week") where it doesn't. Never leave owner_and_deadline as just
    a name with no timeframe, and never assign a Trifork-side commercial or technical action to
    the Bid Savvy scout personally -- Bid Savvy coordinates and scouts, Trifork commits capability
    and makes commercial decisions.

13. executive_summary, key_terms, engagement_model, procurement_timetable, decision_framework:
    you are writing directly for Victoria Milan to make a GO / NO-GO / Park call. Write like you
    are briefing her in person, not like you are filling in a form:
    - executive_summary: real prose in full sentences. opening names the buyer and what they
      actually want, in plain English. scope_summary synthesises the requirement -- what problem
      the buyer is solving and what they're asking a supplier to do -- never a line-by-line copy
      or paraphrase-in-order of the notice text. executive_view is your own judgement of whether
      this is worth pursuing and why, referencing the capability_fit and right_to_win ratings.
    - key_terms: only acronyms, scheme names or jargon that genuinely appear in the notice and
      that someone outside procurement would stumble on (e.g. LCCC, CfD, PME, a named framework).
      Do not pad this with generic procurement terms everyone already knows. Empty list if the
      notice is plain English.
    - scope_of_requirement: requirement_areas is a short list of distinct requirement areas,
      each your own synthesised phrase (a handful of words to one short sentence), never a
      sentence lifted verbatim from the notice. what_buyer_is_seeking is one sentence on why the
      buyer is going to market now (e.g. testing feasibility ahead of a future procurement,
      replacing an incumbent, meeting a compliance deadline).
    - engagement_model: describes how a bidder actually engages with THIS procurement -- the
      submission route or portal named in the notice, the response format, any market-engagement
      or clarification stage described. Never describe our own internal workflow status
      (escalated, approved, flagged) here -- that is a different concept entirely and does not
      belong in this field under any circumstance. If the notice doesn't say, use UNVERIFIED for
      both fields.
    - procurement_timetable: only genuine dated (or explicitly-undated-but-named) milestones from
      the notice text itself, beyond the structured deadline fields you're separately given.
    - solo_or_partner_recommendation: a genuine call on whether Trifork should respond alone or
      with a delivery partner at THIS stage, and why (e.g. solo at an early market-engagement
      stage to establish a direct relationship, versus needing a partner for scale, an existing
      UK framework place, or specific delivery capacity). This is never a restatement of the
      overall PROCEED/PARK/DECLINE recommendation and never an administrative "prepared by"
      line -- it answers the specific question "alone or with someone else, and why".
      recommendation itself is short, natural, human-written phrasing -- "Solo", "Partner",
      "Solo at this stage" -- never a code-style token such as SOLO_ONLY or WITH_PARTNER.
    - decision_framework: 2-4 questions Victoria herself would weigh to decide GO/NO-GO/Park (for
      example, whether a stated risk is acceptable, whether the timeline allows a competitive
      response, whether the positioning points can realistically be addressed in time), each
      paired with what a yes or a no means for the recommendation. These are decisions FOR
      Victoria, distinct from asks (which are questions routed TO Trifork/the buyer).
    - med_dual_reading: only meaningful when capability_fit is MED. build_signals lists the
      specific notice wording that points to real build content; product_signals lists the
      specific notice wording that points to a packaged-product purchase (a 48xxxxxx CPV,
      "procure and implement", a supplier demonstration stage, a licence-based user model, an
      evaluation of what the market already offers). honest_position is one paragraph stating
      plainly that this is genuinely unclear between a build and a packaged-product purchase,
      not an averaged guess. When capability_fit is HIGH or LOW there is no genuine ambiguity to
      present two ways: leave build_signals and product_signals as empty lists and
      honest_position as an empty string.

14. House style, non-negotiable for every text field you write:
    - UK English. No em dash or en dash character anywhere, including inside a sentence where
      you might otherwise use one for an aside: use a comma, colon, semicolon or pipe instead.
    - Never write: delve, tapestry, navigate the complexities, robust, seamless, leverage (as a
      verb), world class, cutting edge, best in class. Write plainly instead.
    - Never invent a fact: no figure, date, contact, CPV code or requirement not present in the
      notice text you were given. Where a field is genuinely absent, say UNVERIFIED or
      "Not stated" rather than estimating or guessing.
    - Use the buyer's exact published opportunity title where you reference it; never shorten or
      tidy it, since a shortened title cannot be searched for on the source portal.

This is a provisional, machine-generated read for a human to validate, not a bid decision."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_capability_profile(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT profile_text FROM config_capability_profile ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise RuntimeError("config_capability_profile is empty; run db init/seed first")
    return row["profile_text"]


def _build_notice_context(conn: sqlite3.Connection, notice_row: sqlite3.Row) -> str:
    gate_rows = conn.execute(
        "SELECT gate_name, outcome, reason FROM gate_results WHERE notice_id = ? "
        "ORDER BY id DESC LIMIT 6",
        (notice_row["id"],),
    ).fetchall()
    gate_summary = "\n".join(f"- {g['gate_name']}: {g['outcome']} ({g['reason']})" for g in gate_rows)

    return (
        f"Notice reference: {notice_row['ref']}\n"
        f"Title: {notice_row['title']}\n"
        f"Buyer: {notice_row['buyer'] or 'UNVERIFIED'}\n"
        f"Sector: {notice_row['sector'] or 'UNVERIFIED'}\n"
        f"Indicative value: {notice_row['indicative_value'] or 'UNVERIFIED'}\n"
        f"UK stage: {notice_row['uk_stage']}\n\n"
        f"Phase 1 gate results:\n{gate_summary}\n\n"
        f"Full notice text:\n{notice_row['text_blob']}"
    )


def run_scope_read(
    client: anthropic.Anthropic, conn: sqlite3.Connection, notice_row: sqlite3.Row
) -> dict:
    """Calls Claude Sonnet 5 with structured output and returns the parsed
    assessment dict. Raises RuntimeError if the model refuses."""
    capability_profile = get_capability_profile(conn)
    notice_context = _build_notice_context(conn, notice_row)

    response = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=[
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {
                "type": "text",
                "text": f"Trifork capability profile:\n\n{capability_profile}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": notice_context}],
        output_config={"format": {"type": "json_schema", "schema": SCOPE_READ_SCHEMA}},
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(
            f"Claude declined the scope read for notice {notice_row['ref']}; "
            "route to the owner for a manual Phase 2 read."
        )

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def run_scope_read_openai(client, conn: sqlite3.Connection, notice_row: sqlite3.Row) -> dict:
    """Same scope read as run_scope_read, against OpenAI instead of Claude
    (2026-07-30, added as a fallback when Anthropic credit ran out). Same
    system instructions, same structured schema -- only the API call shape
    differs, so both providers produce assessments in the same format."""
    capability_profile = get_capability_profile(conn)
    notice_context = _build_notice_context(conn, notice_row)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=1500,
        messages=[
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "system", "content": f"Trifork capability profile:\n\n{capability_profile}"},
            {"role": "user", "content": notice_context},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "scope_read", "schema": SCOPE_READ_SCHEMA, "strict": True},
        },
    )

    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        raise RuntimeError(
            f"OpenAI declined the scope read for notice {notice_row['ref']}; "
            "route to the owner for a manual Phase 2 read."
        )

    return json.loads(choice.message.content)


def get_scope_read_client(settings):
    """Returns (client, scope_read_fn) for whichever provider
    settings.scope_read_provider selects -- the one place that decides which
    API a Phase 2 scope read actually calls. Raises RuntimeError if that
    provider's key isn't configured (callers should check
    settings.scope_read_ready first to show a friendlier UI message)."""
    if settings.scope_read_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("SCOPE_READ_PROVIDER=openai but OPENAI_API_KEY is not set.")
        import openai

        return openai.OpenAI(api_key=settings.openai_api_key, timeout=AI_REQUEST_TIMEOUT_SECONDS), run_scope_read_openai

    if not settings.anthropic_api_key:
        raise RuntimeError("SCOPE_READ_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
    return (
        anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=AI_REQUEST_TIMEOUT_SECONDS),
        run_scope_read,
    )


def save_scope_read(
    conn: sqlite3.Connection, notice_id: int, assessment: dict, model_used: str = MODEL
) -> int:
    now = _now()
    cursor = conn.execute(
        "INSERT INTO phase2_assessments ("
        "notice_id, capability_fit_rating, capability_fit_reasoning, "
        "competitor_position_rating, competitor_position_reasoning, "
        "right_to_win_rating, right_to_win_reasoning, "
        "overall_rating, overall_reasoning, open_questions, "
        "capability_mapping, positioning_points, blockers, asks, recommendation, "
        "executive_summary, key_terms, scope_of_requirement, engagement_model, "
        "procurement_timetable, decision_framework, solo_or_partner_recommendation, "
        "med_dual_reading, model_used, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id,
            assessment["capability_fit"]["rating"],
            assessment["capability_fit"]["reasoning"],
            assessment["competitor_position"]["rating"],
            assessment["competitor_position"]["reasoning"],
            assessment["right_to_win"]["rating"],
            assessment["right_to_win"]["reasoning"],
            assessment["overall"]["rating"],
            assessment["overall"]["reasoning"],
            json.dumps(assessment["open_questions"]),
            json.dumps(assessment["capability_mapping"]) if assessment.get("capability_mapping") else None,
            json.dumps(assessment["positioning_points"]) if assessment.get("positioning_points") else None,
            json.dumps(assessment["blockers"]) if assessment.get("blockers") else None,
            json.dumps(assessment["asks"]) if assessment.get("asks") else None,
            json.dumps(assessment["recommendation"]) if assessment.get("recommendation") else None,
            json.dumps(assessment["executive_summary"]) if assessment.get("executive_summary") else None,
            json.dumps(assessment["key_terms"]) if assessment.get("key_terms") else None,
            json.dumps(assessment["scope_of_requirement"]) if assessment.get("scope_of_requirement") else None,
            json.dumps(assessment["engagement_model"]) if assessment.get("engagement_model") else None,
            json.dumps(assessment["procurement_timetable"]) if assessment.get("procurement_timetable") else None,
            json.dumps(assessment["decision_framework"]) if assessment.get("decision_framework") else None,
            json.dumps(assessment["solo_or_partner_recommendation"]) if assessment.get("solo_or_partner_recommendation") else None,
            json.dumps(assessment["med_dual_reading"]) if assessment.get("med_dual_reading") else None,
            model_used,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid
