"""Competitor Intel: who's winning what, and who's buying. Built entirely
from data already captured off award notices during the OCDS parse
(notices.supplier_name, notices.is_award) and from buyer names on every
notice -- no external data source or partnership required for this MVP.

Deliberately NOT using scope_filter.in_scope_filter_sql here: that filter
excludes UK5 (awarded/closed) by design, since it's built for "what's still
live to pursue" views. Award notices are UK5 by definition, so applying it
here would filter out almost everything this screen exists to show. A plain
sector match is the right scope for historical intelligence instead.

Data-honesty note: notices.value_amount_gross is never populated on award
notices in practice (checked against the live database, 0 of ~9,500).
The real award value lives in the free-text indicative_value field
("833156.96 GBP"), and only on a minority of awards. Every value figure
below is explicit about how many awards it's actually based on, rather
than presenting a total as more complete than it is."""

import re
import sqlite3
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.charts import MONTH_LABELS, bar_chart_series
from savvy_scout.dashboard.companies_house import COMPANY_URL_TEMPLATE, get_company_info
from savvy_scout.triage.gates import _lookup_cpv
from savvy_scout.triage.sector_classifier import contains_keyword

competitor_intel_bp = Blueprint("competitor_intel", __name__)

_VALUE_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*GBP\s*$", re.IGNORECASE)


def _parse_gbp(value: str | None) -> float | None:
    if not value:
        return None
    m = _VALUE_RE.match(value)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _watched_names(conn: sqlite3.Connection) -> set[str]:
    return {r["supplier_name"] for r in conn.execute("SELECT supplier_name FROM watched_competitors").fetchall()}


def _is_relevant_award(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Purpose-built relevance check for historical award data (2026-09-06,
    tightened same day) -- deliberately NOT a call to the real
    gate2_type_of_work, even though it started as one. That function is
    correctly lenient for its real job (Phase 1 triage of live tenders,
    where a human reviews the result afterward): a bare "coupling term"
    match, on its own, is enough for PASS -- Mark's 15 August 2026
    correction removed the requirement that it be paired with real
    generic/digital language. Reusing that exact leniency here produced a
    live false positive: SKYLINE TAXIS's actual notices are literally
    "provision of transport services" for home-to-school taxi runs (CPV
    60120000, Taxi services) -- gate2_type_of_work PASSed them purely
    because the word "transport" is a coupling term (added for the Rail
    and Transport sector), with no real digital/software signal anywhere
    in the text. That leniency assumes a human catches the false positive
    afterward; this filter has no such human-review step, so it needs a
    stricter standard than the real Gate 2 does:
      - a fail-term match, or a CPV disqualifier, still fails outright
        (same as gate2_type_of_work).
      - PASS requires either a real unconditional_pass/generic_needs_coupling
        term match (actual digital/software language, not just a sector
        product-name coupling term on its own), or a genuine
        _lookup_cpv PASS/INFERRED_FIT (an explicit, vetted CPV list entry)
        -- not gate2_type_of_work's weaker "CPV unclassified but happens to
        fall within the sector's broad configured range" fallback, which
        is corroboration-grade evidence, not proof, for an unsupervised
        filter with nothing else checking its work.

    Cached by ref (2026-09-06 urgent perf fix): a full evaluation per
    award, computed fresh on every page load, took 50+ seconds against the
    live database's ~9,500 award notices. A notice's own text/CPV/sector
    never change once swept, so the result never changes either -- this
    turns every load after the first into a cheap indexed cache read.
    Deliberately does NOT commit here -- committing once per uncached row
    (potentially thousands, on a first/cold load) turned out to be the
    larger share of that 50+ seconds, each commit a separate disk sync on
    Render's persistent disk. Callers commit once after their whole loop
    finishes instead."""
    cached = conn.execute(
        "SELECT relevant FROM award_relevance_cache WHERE ref = ?", (row["ref"],)
    ).fetchone()
    if cached is not None:
        return bool(cached["relevant"])

    text_blob = row["text_blob"] or ""
    cpv_primary = row["cpv_primary"]

    terms = conn.execute("SELECT term, category FROM config_gate2_terms").fetchall()
    has_fail_term = any(t["category"] == "fail" and contains_keyword(text_blob, t["term"]) for t in terms)
    has_strong_text_signal = any(
        t["category"] in ("unconditional_pass", "generic_needs_coupling") and contains_keyword(text_blob, t["term"])
        for t in terms
    )

    cpv_disqualified = False
    cpv_strong_pass = False
    if cpv_primary:
        cpv_outcome, _ = _lookup_cpv(conn, cpv_primary, text_blob)
        if cpv_outcome == "FAIL":
            cpv_disqualified = True
        elif cpv_outcome in ("PASS", "INFERRED_FIT"):
            cpv_strong_pass = True

    relevant = not has_fail_term and not cpv_disqualified and (has_strong_text_signal or cpv_strong_pass)
    conn.execute(
        "INSERT OR REPLACE INTO award_relevance_cache (ref, relevant) VALUES (?, ?)",
        (row["ref"], int(relevant)),
    )
    return relevant


def _competitors(conn: sqlite3.Connection):
    rows = conn.execute(
        """
        SELECT ref, supplier_name, sector, indicative_value, text_blob, cpv_primary,
               cpv_primary_inferred, cpv_additional,
               COALESCE(published_at, first_seen_at) AS win_date
        FROM notices
        WHERE is_award = 1 AND supplier_name IS NOT NULL AND supplier_name != '' AND sector IS NOT NULL
        """
    ).fetchall()

    by_supplier: dict[str, dict] = {}
    for r in rows:
        entry = by_supplier.setdefault(
            r["supplier_name"],
            {"supplier_name": r["supplier_name"], "award_count": 0, "sectors": set(),
             "priced_total": 0.0, "priced_count": 0, "last_win_date": None, "relevant": False},
        )
        entry["award_count"] += 1
        entry["sectors"].add(r["sector"])
        parsed = _parse_gbp(r["indicative_value"])
        if parsed is not None:
            entry["priced_total"] += parsed
            entry["priced_count"] += 1
        if r["win_date"] and (entry["last_win_date"] is None or r["win_date"] > entry["last_win_date"]):
            entry["last_win_date"] = r["win_date"]
        if not entry["relevant"] and _is_relevant_award(conn, r):
            entry["relevant"] = True
    conn.commit()  # one commit for the whole batch of relevance-cache writes, not one per row

    watched = _watched_names(conn)
    out = []
    for entry in by_supplier.values():
        entry["sectors"] = sorted(entry["sectors"])
        entry["watched"] = entry["supplier_name"] in watched
        out.append(entry)
    out.sort(key=lambda e: e["award_count"], reverse=True)
    return out


def possible_competitors_for_notice(conn: sqlite3.Connection, sector: str | None, buyer: str | None) -> list[dict]:
    """For a live opportunity (2026-09-06), ranks who's most likely to also
    bid on it: suppliers who've previously won a *relevant* award from this
    exact buyer rank first (the strongest real signal -- an incumbent or
    known relationship), then anyone who's won relevant work in the same
    sector more broadly. Reuses the same Gate 2 relevance check as
    Competitor Intel's default filter, for the same reason: a same-sector
    but wrong-type-of-work supplier (a taxi firm at an NHS trust) isn't a
    real bidding threat on a software tender just because the sector
    matches. Returns at most 10, same-buyer matches first, then by award
    count -- there's no attempt to estimate a probability, just a ranked
    "who to watch for" list from real history."""
    if not sector:
        return []
    rows = conn.execute(
        "SELECT ref, supplier_name, buyer, sector, indicative_value, text_blob, cpv_primary, "
        "cpv_primary_inferred, cpv_additional, COALESCE(published_at, first_seen_at) AS win_date "
        "FROM notices WHERE is_award = 1 AND supplier_name IS NOT NULL AND supplier_name != '' AND sector = ?",
        (sector,),
    ).fetchall()

    by_supplier: dict[str, dict] = {}
    for r in rows:
        if not _is_relevant_award(conn, r):
            continue
        entry = by_supplier.setdefault(
            r["supplier_name"],
            {"supplier_name": r["supplier_name"], "award_count": 0, "last_win_date": None, "same_buyer": False},
        )
        entry["award_count"] += 1
        if buyer and r["buyer"] == buyer:
            entry["same_buyer"] = True
        if r["win_date"] and (entry["last_win_date"] is None or r["win_date"] > entry["last_win_date"]):
            entry["last_win_date"] = r["win_date"]
    conn.commit()  # one commit for the whole batch of relevance-cache writes, not one per row

    out = list(by_supplier.values())
    out.sort(key=lambda e: (not e["same_buyer"], -e["award_count"]))
    return out[:10]


def _competitor_detail(conn: sqlite3.Connection, supplier_name: str) -> dict | None:
    """Per-competitor drill-down (2026-09-06 UI alignment, modeled on
    Contracts Advance's competitor detail page): the same award rows the
    main grid already aggregates, cut three ways -- by buyer, by sector,
    and by month -- plus a real chart instead of just totals. Returns None
    if this supplier has no award notices, so the route can 404 rather
    than render an empty page for a typo'd or stale name."""
    rows = conn.execute(
        """
        SELECT ref, title, buyer, sector, indicative_value,
               supplier_contact_name, supplier_contact_email, supplier_contact_phone,
               COALESCE(published_at, first_seen_at) AS win_date
        FROM notices
        WHERE is_award = 1 AND supplier_name = ?
        ORDER BY win_date DESC
        """,
        (supplier_name,),
    ).fetchall()
    if not rows:
        return None

    contracts = []
    by_buyer: dict[str, dict] = {}
    by_sector: dict[str, dict] = {}
    monthly: dict[tuple[int, int], float] = {}
    priced_total = 0.0
    priced_count = 0
    # Most recent non-null contact across all this supplier's awards (rows
    # are already ordered newest-first) -- shown once at the top as "best
    # known contact," alongside every award's own contact in the table
    # below, since different awards can carry different contacts over time.
    latest_contact = None

    for r in rows:
        parsed = _parse_gbp(r["indicative_value"])
        if latest_contact is None and (r["supplier_contact_name"] or r["supplier_contact_email"] or r["supplier_contact_phone"]):
            latest_contact = {
                "name": r["supplier_contact_name"],
                "email": r["supplier_contact_email"],
                "phone": r["supplier_contact_phone"],
            }
        contracts.append(
            {
                "ref": r["ref"], "title": r["title"], "buyer": r["buyer"],
                "sector": r["sector"], "indicative_value": r["indicative_value"],
                "contact_name": r["supplier_contact_name"],
                "contact_email": r["supplier_contact_email"],
                "contact_phone": r["supplier_contact_phone"],
                "win_date": r["win_date"],
            }
        )
        if parsed is not None:
            priced_total += parsed
            priced_count += 1

        if r["buyer"]:
            b = by_buyer.setdefault(r["buyer"], {"buyer": r["buyer"], "count": 0, "priced_total": 0.0})
            b["count"] += 1
            b["priced_total"] += parsed or 0.0
        if r["sector"]:
            s = by_sector.setdefault(r["sector"], {"sector": r["sector"], "count": 0, "priced_total": 0.0})
            s["count"] += 1
            s["priced_total"] += parsed or 0.0
        if r["win_date"]:
            try:
                d = datetime.fromisoformat(r["win_date"])
                key = (d.year, d.month)
                monthly[key] = monthly.get(key, 0.0) + (parsed or 0.0)
            except ValueError:
                pass

    chart = []
    if monthly:
        chart = bar_chart_series(
            [(f"{MONTH_LABELS[month - 1]} {year}", monthly[(year, month)]) for year, month in sorted(monthly)]
        )

    return {
        "supplier_name": supplier_name,
        "contracts": contracts,
        "award_count": len(rows),
        "priced_total": priced_total,
        "priced_count": priced_count,
        "by_buyer": sorted(by_buyer.values(), key=lambda x: x["count"], reverse=True),
        "by_sector": sorted(by_sector.values(), key=lambda x: x["count"], reverse=True),
        "chart": chart,
        "watched": supplier_name in _watched_names(conn),
        "latest_contact": latest_contact,
    }


def _buyers(conn: sqlite3.Connection):
    """Same relevance principle as _competitors() (2026-09-06): a buyer is
    only "relevant" if at least one of their notices -- award or not, since
    a buyer who's only ever published genuinely digital/software tenders
    but hasn't awarded one yet is still a real prospect -- is itself
    Trifork's type of work. Without this, the tab listed every buyer who's
    ever published anything in a tracked sector, the same noise problem
    Competitors had before this fix."""
    rows = conn.execute(
        """
        SELECT ref, buyer, sector, cpv_primary, cpv_primary_inferred, cpv_additional, text_blob,
               COALESCE(published_at, first_seen_at) AS activity_date
        FROM notices
        WHERE buyer IS NOT NULL AND buyer != '' AND sector IS NOT NULL
        """
    ).fetchall()

    by_buyer: dict[str, dict] = {}
    for r in rows:
        entry = by_buyer.setdefault(
            r["buyer"],
            {"buyer": r["buyer"], "notice_count": 0, "sectors": set(), "last_activity": None, "relevant": False},
        )
        entry["notice_count"] += 1
        entry["sectors"].add(r["sector"])
        if r["activity_date"] and (entry["last_activity"] is None or r["activity_date"] > entry["last_activity"]):
            entry["last_activity"] = r["activity_date"]
        if not entry["relevant"] and _is_relevant_award(conn, r):
            entry["relevant"] = True
    conn.commit()  # one commit for the whole batch of relevance-cache writes, not one per row

    out = []
    for entry in by_buyer.values():
        entry["sectors"] = sorted(entry["sectors"])
        out.append(entry)
    out.sort(key=lambda e: e["notice_count"], reverse=True)
    return out


@competitor_intel_bp.route("/competitor-intel")
@login_required
def index():
    conn = get_db()
    tab = request.args.get("tab", "competitors")
    show_all = request.args.get("show") == "all"

    all_competitors = _competitors(conn) if tab != "buyers" else []
    irrelevant_count = sum(1 for c in all_competitors if not c["relevant"])
    competitors = all_competitors if show_all else [c for c in all_competitors if c["relevant"]]

    all_buyers = _buyers(conn) if tab == "buyers" else []
    irrelevant_buyer_count = sum(1 for b in all_buyers if not b["relevant"])
    buyers = all_buyers if show_all else [b for b in all_buyers if b["relevant"]]

    return render_template(
        "competitor_intel.html", tab=tab, competitors=competitors, buyers=buyers,
        show_all=show_all, irrelevant_count=irrelevant_count,
        irrelevant_buyer_count=irrelevant_buyer_count,
    )


@competitor_intel_bp.route("/competitor-intel/detail")
@login_required
def detail():
    conn = get_db()
    supplier_name = request.args.get("name", "")
    detail_data = _competitor_detail(conn, supplier_name)
    if detail_data is None:
        abort(404)

    # Company enrichment only runs here, not on the main list (2026-09-06):
    # a live/cached lookup per row would slow down a page listing dozens of
    # competitors, and the drill-down is where "who exactly is this" is
    # actually being asked.
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    company_info = get_company_info(conn, supplier_name, settings.companies_house_api_key)
    company_url = COMPANY_URL_TEMPLATE.format(number=company_info["company_number"]) if company_info else None

    return render_template(
        "competitor_detail.html", **detail_data,
        company_info=company_info, company_url=company_url,
        companies_house_configured=bool(settings.companies_house_api_key),
    )


@competitor_intel_bp.route("/competitor-intel/watch", methods=["POST"])
@login_required
def toggle_watch():
    conn = get_db()
    supplier_name = request.form.get("supplier_name", "").strip()
    if not supplier_name:
        return redirect(url_for("competitor_intel.index"))

    existing = conn.execute(
        "SELECT id FROM watched_competitors WHERE supplier_name = ?", (supplier_name,)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM watched_competitors WHERE id = ?", (existing["id"],))
    else:
        conn.execute(
            "INSERT INTO watched_competitors (supplier_name, watched_by, watched_at) VALUES (?, ?, ?)",
            (supplier_name, current_user.display_name, datetime.now(timezone.utc).isoformat()),
        )
    conn.commit()
    return redirect(request.form.get("next") or url_for("competitor_intel.index", tab="competitors"))
