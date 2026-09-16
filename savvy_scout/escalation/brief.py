"""Escalation documents (SPEC v1.5 B3).

- Internal Addendum: generated on owner-marked Victoria escalation. PDF,
  branded to match the rest of the app (red #AF1F23 header language),
  table-based sections -- see build_internal_addendum.
- Capture Brief: generated after owner Phase 2 approval. Also PDF, same
    visual language -- see build_capture_brief.
- Original Notice: a PDF snapshot of the complete source text captured by
    Savvy Scout, generated alongside the two review documents.

2026-08-09: switched both from python-docx to reportlab-generated PDF
(explicit request -- match a supplied reference document's exact design:
red section-table headers, a bordered "internal use only" callout box,
notice link never dropped). reportlab is pure-Python (no LibreOffice/MS
Word needed), so this works unchanged on Render's Linux container.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import HRFlowable, Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from savvy_scout.escalation.context import MISSING, build_context

GATE_ORDER = ["gate1", "gate2", "gate3", "gate4", "gate5", "filter3"]

# SAVVY_SCOUT_BRIEFS_DIR lets a production deploy (Render's persistent disk,
# e.g. /var/data/briefs) point generated files somewhere that actually
# survives a redeploy -- the default keeps local dev unchanged.
DEFAULT_BRIEFS_DIR = os.environ.get("SAVVY_SCOUT_BRIEFS_DIR", "briefs")

# Same brand red used throughout the dashboard's CSS (base.html --primary).
BRAND_RED = colors.HexColor("#AE1E23")
CALLOUT_BG = colors.white
CALLOUT_BORDER = colors.HexColor("#B8BCC2")
ROW_ALT_BG = colors.HexColor("#F9FAFB")
GRID_COLOR = colors.HexColor("#E5E7EB")
MUTED_TEXT = colors.HexColor("#6B7280")
PAGE_WIDTH = 16 * cm
LOGO_PATH = Path(__file__).resolve().parent.parent / "static" / "design" / "logos" / "TenderSight.png"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _esc(value) -> str:
    """Escapes &, < and > in dynamic content (notice/AI text) before it goes
    into a reportlab Paragraph -- Paragraph parses its string as a small XML
    dialect, so an unescaped "&" (e.g. a buyer or case study named "&Money")
    silently corrupts or drops that part of the text instead of raising.
    Never call this on the static markup strings this module writes itself
    (those intentionally use &nbsp;/&middot;/&ldquo;/etc as real entities)."""
    if value is None or value == "":
        return ""
    return _xml_escape(str(value))


def _pdf_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        "BriefBanner", parent=styles["Normal"], fontSize=9, textColor=BRAND_RED,
        fontName="Helvetica-Bold", spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        "BriefTitle", parent=styles["Title"], fontSize=17, alignment=TA_LEFT,
        textColor=colors.HexColor("#111827"), spaceAfter=4,
    ))
    styles.add(ParagraphStyle(
        "BriefMeta", parent=styles["Normal"], fontSize=9.5,
        textColor=colors.HexColor("#4B5563"), spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        "BriefSectionHeading", parent=styles["Heading2"], fontSize=12.5,
        textColor=BRAND_RED, spaceBefore=16, spaceAfter=6,
    ))
    styles.add(ParagraphStyle("BriefCell", parent=styles["Normal"], fontSize=9.5, leading=13))
    styles.add(ParagraphStyle(
        "BriefCellHeader", parent=styles["Normal"], fontSize=9.5, leading=13,
        textColor=colors.white, fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle("BriefFooter", parent=styles["Normal"], fontSize=8, textColor=MUTED_TEXT))
    return styles


def _section_table(col_headers: list[str], rows: list[tuple], col_widths: list[float], styles) -> Table:
    """A section's data table: red header row (brand colour), alternating
    row shading, full grid -- the visual language every section below uses,
    matching the reference document's tables."""
    header = [Paragraph(h, styles["BriefCellHeader"]) for h in col_headers]
    data = [header]
    for row in rows:
        data.append([Paragraph(_esc(cell) or "&mdash;", styles["BriefCell"]) for cell in row])
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND_RED),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_ALT_BG]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table


def _callout_box(text: str, styles) -> Table:
    """White fill, thin grey border, thick red left accent bar -- the
    house-style callout box per BidSavvy_Brand_Guidelines.md, not a
    uniform colour block."""
    box = Table([[Paragraph(text, styles["BriefCell"])]], colWidths=[PAGE_WIDTH])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CALLOUT_BG),
        ("BOX", (0, 0), (-1, -1), 0.5, CALLOUT_BORDER),
        ("LINEBEFORE", (0, 0), (0, -1), 3, BRAND_RED),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return box


def _header_block(notice: sqlite3.Row, banner_text: str, styles) -> list:
    meta_bits = [_esc(notice["buyer"]) or "UNVERIFIED", f"Reference: {_esc(notice['ref'])}"]
    if notice["sector"]:
        meta_bits.append(f"Sector: {_esc(notice['sector'])}")
    return [
        Paragraph(banner_text, styles["BriefBanner"]),
        Paragraph(_esc(notice["title"]) or "UNVERIFIED", styles["BriefTitle"]),
        Paragraph(" &middot; ".join(meta_bits), styles["BriefMeta"]),
        Paragraph(
            f"Auto-generated by TenderSight™ &middot; {datetime.now(timezone.utc).strftime('%d %B %Y')}",
            styles["BriefMeta"],
        ),
        Spacer(1, 10),
    ]


def _footer_block(text: str, styles) -> list:
    return [
        Spacer(1, 16),
        HRFlowable(width="100%", color=GRID_COLOR),
        Spacer(1, 6),
        Paragraph(text, styles["BriefFooter"]),
    ]


def _build_pdf(output_path: str, story: list, footer_context: dict | None = None) -> str:
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=2 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )

    def draw_footer(canvas, _doc):
        if not footer_context:
            return
        canvas.saveState()
        canvas.setStrokeColor(GRID_COLOR)
        canvas.line(1.5 * cm, 1.35 * cm, A4[0] - 1.5 * cm, 1.35 * cm)
        canvas.setFillColor(MUTED_TEXT)
        canvas.setFont("Helvetica", 7.5)
        generated = str(footer_context["generated_at"])[:19].replace("T", " ")
        left = "Smarter Bids. Real Results. | © 2026 Bid Savvy Solutions Ltd"
        right = f"{footer_context['notice_reference']} | {generated} | Page {canvas.getPageNumber()}"
        canvas.drawString(1.5 * cm, 0.9 * cm, left)
        canvas.drawRightString(A4[0] - 1.5 * cm, 0.9 * cm, right)
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return output_path


def _context_header(context: dict, label: str, styles) -> list:
    urgency_text = context["urgency"].split(" ", 1)[-1]
    urgency_colour = {
        "URGENT": colors.HexColor("#DC2626"),
        "Approaching": colors.HexColor("#D97706"),
        "Open": colors.HexColor("#059669"),
    }.get(urgency_text, MUTED_TEXT)
    logo = Image(str(LOGO_PATH), width=3.3 * cm, height=1.15 * cm)
    urgency = Table([[Paragraph(urgency_text, styles["BriefCellHeader"])]], colWidths=[3 * cm])
    urgency.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), urgency_colour),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    band = Table([[logo, Paragraph(label.upper(), styles["BriefBanner"]), urgency]], colWidths=[4 * cm, 9 * cm, 3 * cm])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F9FAFB")),
        ("BOX", (0, 0), (-1, -1), 0.8, GRID_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return [
        band,
        Spacer(1, 10),
        Paragraph(_esc(context["title"]), styles["BriefTitle"]),
        Paragraph(
            f"{_esc(context['buyer'])} &middot; {_esc(context['sector'])} &middot; "
            f"Reference: {_esc(context['notice_reference'])}",
            styles["BriefMeta"],
        ),
        Paragraph(
            f"Generated: {_esc(context['generated_at'])}", styles["BriefMeta"]
        ),
        Spacer(1, 10),
    ]


def _provisional_banner(styles) -> Table:
    return _callout_box(
        "<b>PROVISIONAL — FOR VALIDATION</b><br/>AI-derived ratings and reasoning require human validation.",
        styles,
    )


def _dict_text(value, preferred_keys) -> str:
    if isinstance(value, dict):
        parts = [str(value[key]) for key in preferred_keys if value.get(key)]
        return " — ".join(parts) or MISSING
    return str(value or MISSING)


def _bullet_rows(values, preferred_keys) -> list[tuple]:
    return [(f"• {_dict_text(value, preferred_keys)}",) for value in values] or [(MISSING,)]


def _latest_gate_results(conn: sqlite3.Connection, notice_id: int) -> list[sqlite3.Row]:
    run = conn.execute(
        "SELECT id FROM triage_runs WHERE notice_id = ? ORDER BY id DESC LIMIT 1", (notice_id,)
    ).fetchone()
    if not run:
        return []
    rows = conn.execute(
        "SELECT gate_number, gate_name, outcome, reason FROM gate_results WHERE triage_run_id = ?",
        (run["id"],),
    ).fetchall()
    by_gate = {r["gate_number"]: r for r in rows}
    return [by_gate[g] for g in GATE_ORDER if g in by_gate]


def _latest_assessment(conn: sqlite3.Connection, notice_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM phase2_assessments WHERE notice_id = ? ORDER BY id DESC LIMIT 1",
        (notice_id,),
    ).fetchone()


def build_internal_addendum(conn: sqlite3.Connection, notice_id: int, output_dir: str = DEFAULT_BRIEFS_DIR) -> str:
    context = build_context(conn, notice_id)
    styles = _pdf_styles()
    story = _context_header(context, "Internal Addendum", styles)
    story.append(_callout_box(
        "<b>INTERNAL USE ONLY — NOT FOR CLIENT DISTRIBUTION.</b><br/>"
        "Prepared for Victoria Milan to make a GO / NO-GO / Park decision.",
        styles,
    ))
    story.append(Paragraph("1. TRIAGE SUMMARY", styles["BriefSectionHeading"]))
    surfaced = ", ".join(
        f"{gate['gate_name']}: {gate['result']}" for gate in context["gate_outcomes"] if gate["result"] != "PASS"
    ) or "all recorded gates passed"
    story.append(Paragraph(
        f"{_esc(context['title'])} is a {_esc(context['sector'])} opportunity from "
        f"{_esc(context['buyer'])}. It surfaced for owner review because {_esc(surfaced)}.",
        styles["BriefCell"],
    ))
    story.append(Paragraph(
        f"Source notice: {_esc(context['notice_url'])}", styles["BriefCell"]
    ))

    story.append(Paragraph("2. CAPABILITY MAPPING", styles["BriefSectionHeading"]))
    story.append(_section_table(
        ["Gate", "Result", "Reason"],
        [(gate["gate_name"], gate["result"], gate["reason"]) for gate in context["gate_outcomes"]] or [(MISSING, MISSING, MISSING)],
        [4 * cm, 2.5 * cm, 9.5 * cm], styles,
    ))
    story.append(Spacer(1, 8))
    story.append(_provisional_banner(styles))
    reasoning = context["ai_read"].get("per_field_reasoning", {})
    story.append(_section_table(
        ["AI dimension", "Rating", "Reasoning"],
        [
            ("Capability fit", context["ai_read"]["capability_fit"], reasoning.get("capability_fit", MISSING)),
            ("Competitor position", context["ai_read"]["competitor_position"], reasoning.get("competitor_position", MISSING)),
            ("Right to win", context["ai_read"]["right_to_win"], reasoning.get("right_to_win", MISSING)),
            ("Overall", context["ai_read"]["overall"], reasoning.get("overall", MISSING)),
        ],
        [4 * cm, 2.5 * cm, 9.5 * cm], styles,
    ))

    story.append(Paragraph("3. BLOCKERS & RISKS", styles["BriefSectionHeading"]))
    story.append(_section_table(["Material blockers and risks"], _bullet_rows(
        context["blockers_risks"], ("blocker", "assessment")
    ), [16 * cm], styles))

    story.append(Paragraph("4. DIRECT ASKS", styles["BriefSectionHeading"]))
    asks = context["direct_asks"] or context["ai_read"].get("open_questions", [])
    story.append(_section_table(["Decision or answer required from Victoria"], _bullet_rows(
        asks, ("ask", "why_it_matters")
    ), [16 * cm], styles))

    story.append(Paragraph("5. RECOMMENDATION", styles["BriefSectionHeading"]))
    story.append(_callout_box(
        f"Owner recommendation: <b>{_esc(context['recommended_next_action'])}</b><br/>"
        f"Recommended by: {_esc(context['owner_name'])}", styles,
    ))

    safe_ref = context["notice_reference"].replace("/", "-")
    output_path = str(Path(output_dir) / f"{safe_ref}_internal_addendum.pdf")
    return _build_pdf(output_path, story, context)


def build_original_notice_pdf(
    conn: sqlite3.Connection, notice_id: int, output_dir: str = DEFAULT_BRIEFS_DIR
) -> str:
    notice = conn.execute("SELECT * FROM notices WHERE id = ?", (notice_id,)).fetchone()
    if notice is None:
        raise ValueError(f"No notice with id {notice_id}")

    styles = _pdf_styles()
    story = _header_block(notice, "ORIGINAL NOTICE &nbsp;|&nbsp; SOURCE SNAPSHOT", styles)
    story.append(_section_table(
        ["Field", "Detail"],
        [
            ("Reference", notice["ref"]),
            ("Source", notice["source"]),
            ("Buyer", notice["buyer"] or "UNVERIFIED"),
            ("Published", notice["published_at"] or "UNVERIFIED"),
            ("Source URL", notice["notice_url"] or "Not published by source"),
        ],
        [4 * cm, 12 * cm], styles,
    ))
    story.append(Paragraph("FULL NOTICE TEXT", styles["BriefSectionHeading"]))
    notice_text = notice["notice_description"] or notice["text_blob"] or "UNVERIFIED"
    text_lines = notice_text.splitlines() or ["UNVERIFIED"]
    for line in text_lines:
        story.append(Paragraph(_esc(line) or "&nbsp;", styles["BriefCell"]))
        story.append(Spacer(1, 4))
    story.extend(_footer_block(
        "Auto-generated by TenderSight™ &middot; Source snapshot for internal review",
        styles,
    ))

    safe_ref = notice["ref"].replace("/", "-")
    output_path = str(Path(output_dir) / f"{safe_ref}_original_notice.pdf")
    return _build_pdf(output_path, story)


def build_capture_brief(conn: sqlite3.Connection, notice_id: int, output_dir: str = DEFAULT_BRIEFS_DIR) -> str:
    context = build_context(conn, notice_id)
    styles = _pdf_styles()
    story = _context_header(context, "Capture Brief", styles)
    sections = [
        ("1. OPPORTUNITY SUMMARY", context["notice_text"]),
        ("2. BUYER", context["buyer"]),
        ("3. VALUE", f"{context['value_estimate']} {context['currency']}"),
        ("4. ROUTE TO MARKET", f"{context['route_to_market']} | Framework: {context['framework_status']}"),
    ]
    for heading, text in sections:
        story.append(Paragraph(heading, styles["BriefSectionHeading"]))
        story.append(Paragraph(_esc(text), styles["BriefCell"]))

    story.append(Paragraph("5. GATE OUTCOMES", styles["BriefSectionHeading"]))
    story.append(_section_table(
        ["Gate", "Result", "Reason"],
        [(gate["gate_name"], gate["result"], gate["reason"]) for gate in context["gate_outcomes"]] or [(MISSING, MISSING, MISSING)],
        [4 * cm, 2.5 * cm, 9.5 * cm], styles,
    ))

    story.append(Paragraph("6. PROVISIONAL RATINGS WITH REASONING", styles["BriefSectionHeading"]))
    story.append(_provisional_banner(styles))
    reasoning = context["ai_read"].get("per_field_reasoning", {})
    story.append(_section_table(
        ["Dimension", "Rating", "Reasoning"],
        [
            ("Capability fit", context["ai_read"]["capability_fit"], reasoning.get("capability_fit", MISSING)),
            ("Competitor position", context["ai_read"]["competitor_position"], reasoning.get("competitor_position", MISSING)),
            ("Right to win", context["ai_read"]["right_to_win"], reasoning.get("right_to_win", MISSING)),
            ("Overall", context["ai_read"]["overall"], reasoning.get("overall", MISSING)),
        ], [4 * cm, 2.5 * cm, 9.5 * cm], styles,
    ))

    story.append(Paragraph("7. COMPETITOR PICTURE", styles["BriefSectionHeading"]))
    story.append(Paragraph(_esc(reasoning.get("competitor_position", MISSING)), styles["BriefCell"]))
    story.append(Paragraph("8. RISKS", styles["BriefSectionHeading"]))
    story.append(_section_table(["Risk"], _bullet_rows(context["blockers_risks"], ("blocker", "assessment")), [16 * cm], styles))
    story.append(Paragraph("9. OPEN QUESTIONS", styles["BriefSectionHeading"]))
    story.append(_section_table(["Question"], _bullet_rows(context["ai_read"].get("open_questions", []), ()), [16 * cm], styles))
    story.append(Paragraph("10. RECOMMENDED NEXT ACTION", styles["BriefSectionHeading"]))
    story.append(_callout_box(_esc(context["recommended_next_action"]), styles))

    safe_ref = context["notice_reference"].replace("/", "-")
    output_path = str(Path(output_dir) / f"{safe_ref}_capture_brief.pdf")
    return _build_pdf(output_path, story, context)


def build_brief(conn: sqlite3.Connection, notice_id: int, output_dir: str = DEFAULT_BRIEFS_DIR) -> str:
    # Backward-compatible alias used by older callers.
    return build_internal_addendum(conn, notice_id, output_dir)


def record_brief(
    conn: sqlite3.Connection,
    notice_id: int,
    trigger_reason: str,
    docx_path: str,
    created_by: str,
    brief_type: str = "INTERNAL_ADDENDUM",
) -> int:
    now = _now()
    existing = conn.execute(
        "SELECT id FROM escalation_briefs WHERE notice_id = ? AND brief_type = ? "
        "ORDER BY id DESC LIMIT 1",
        (notice_id, brief_type),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE escalation_briefs SET trigger_reason = ?, docx_path = ?, created_by = ?, "
            "created_at = ? WHERE id = ?",
            (trigger_reason, docx_path, created_by, now, existing["id"]),
        )
        conn.commit()
        return existing["id"]
    cursor = conn.execute(
        "INSERT INTO escalation_briefs (notice_id, trigger_reason, docx_path, created_by, created_at, brief_type) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (notice_id, trigger_reason, docx_path, created_by, now, brief_type),
    )
    conn.commit()
    return cursor.lastrowid


def mark_emailed(conn: sqlite3.Connection, brief_id: int, recipient: str) -> None:
    conn.execute(
        "UPDATE escalation_briefs SET emailed_to = ?, emailed_at = ? WHERE id = ?",
        (recipient, _now(), brief_id),
    )
    conn.commit()
