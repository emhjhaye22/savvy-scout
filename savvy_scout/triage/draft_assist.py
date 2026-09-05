"""Draft assist (UI alignment build, 2026-09-05): AI-drafted first answers to
selection-questionnaire / bid-response questions, using the same
Anthropic/OpenAI client already wired for Phase 2 scope reads (see
scope_read.py) and the same Trifork capability profile as its grounding
content -- this app has no other source of "what should we say about
ourselves" to draft from, and no stored questionnaire text of its own, so
Mark pastes the actual question in per opportunity.

Every draft is PROVISIONAL until a human reviews it, per SPEC.md
non-negotiable 5 -- same guardrail as every other AI output in this app.
This module only produces the draft text; the review/used-or-rejected state
lives in dashboard/routes/draft_assist.py against the draft_assist_items
table."""

import sqlite3

import anthropic

from savvy_scout.triage.scope_read import _build_notice_context, get_capability_profile

MODEL = "claude-sonnet-5"
OPENAI_MODEL = "gpt-4o"

SYSTEM_INSTRUCTIONS = (
    "You are drafting a first-pass answer to a UK public-sector selection "
    "questionnaire or bid-response question, on behalf of Trifork, using "
    "Trifork's own capability profile as the only source of what to claim "
    "about the company. Write in first person plural (\"we\"), UK English, "
    "concrete and specific rather than generic marketing language -- ground "
    "every claim in the capability profile or the notice's own stated "
    "requirement, never invent a client name, certification, or number that "
    "isn't in the profile. If the profile doesn't support a strong answer to "
    "this question, say so plainly in the draft rather than papering over "
    "the gap. This is a first draft for a human to review and edit, not a "
    "final submission."
)


def _build_prompt(conn: sqlite3.Connection, notice_row: sqlite3.Row, question_text: str) -> str:
    notice_context = _build_notice_context(conn, notice_row)
    return (
        f"{notice_context}\n\n"
        f"Selection questionnaire / bid response question to answer:\n{question_text}"
    )


def run_draft_answer(client: anthropic.Anthropic, conn: sqlite3.Connection, notice_row: sqlite3.Row, question_text: str) -> str:
    capability_profile = get_capability_profile(conn)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1200,
        system=[
            {"type": "text", "text": SYSTEM_INSTRUCTIONS},
            {
                "type": "text",
                "text": f"Trifork capability profile:\n\n{capability_profile}",
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[{"role": "user", "content": _build_prompt(conn, notice_row, question_text)}],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to draft an answer for notice {notice_row['ref']}.")
    return next(block.text for block in response.content if block.type == "text")


def run_draft_answer_openai(client, conn: sqlite3.Connection, notice_row: sqlite3.Row, question_text: str) -> str:
    """Same draft as run_draft_answer, against OpenAI -- mirrors
    scope_read.py's run_scope_read_openai fallback for when Anthropic credit
    runs out."""
    capability_profile = get_capability_profile(conn)
    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        max_tokens=1200,
        messages=[
            {"role": "system", "content": SYSTEM_INSTRUCTIONS},
            {"role": "system", "content": f"Trifork capability profile:\n\n{capability_profile}"},
            {"role": "user", "content": _build_prompt(conn, notice_row, question_text)},
        ],
    )
    choice = response.choices[0]
    if choice.finish_reason == "content_filter":
        raise RuntimeError(f"OpenAI declined to draft an answer for notice {notice_row['ref']}.")
    return choice.message.content


def get_draft_assist_client(settings):
    """Returns (client, draft_fn, model_name) for whichever provider
    settings.scope_read_provider selects -- same provider choice Phase 2
    scope reads use, since this app only ever configures one AI provider at
    a time. Raises RuntimeError if that provider's key isn't set."""
    if settings.scope_read_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("SCOPE_READ_PROVIDER=openai but OPENAI_API_KEY is not set.")
        import openai

        return openai.OpenAI(api_key=settings.openai_api_key), run_draft_answer_openai, OPENAI_MODEL

    if not settings.anthropic_api_key:
        raise RuntimeError("SCOPE_READ_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set.")
    return anthropic.Anthropic(api_key=settings.anthropic_api_key), run_draft_answer, MODEL
