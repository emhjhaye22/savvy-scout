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

from flask import Blueprint, abort, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.charts import MONTH_LABELS, bar_chart_series

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


def _competitors(conn: sqlite3.Connection):
    rows = conn.execute(
        """
        SELECT supplier_name, sector, indicative_value,
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
             "priced_total": 0.0, "priced_count": 0, "last_win_date": None},
        )
        entry["award_count"] += 1
        entry["sectors"].add(r["sector"])
        parsed = _parse_gbp(r["indicative_value"])
        if parsed is not None:
            entry["priced_total"] += parsed
            entry["priced_count"] += 1
        if r["win_date"] and (entry["last_win_date"] is None or r["win_date"] > entry["last_win_date"]):
            entry["last_win_date"] = r["win_date"]

    watched = _watched_names(conn)
    out = []
    for entry in by_supplier.values():
        entry["sectors"] = sorted(entry["sectors"])
        entry["watched"] = entry["supplier_name"] in watched
        out.append(entry)
    out.sort(key=lambda e: e["award_count"], reverse=True)
    return out


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

    for r in rows:
        parsed = _parse_gbp(r["indicative_value"])
        contracts.append(
            {
                "ref": r["ref"], "title": r["title"], "buyer": r["buyer"],
                "sector": r["sector"], "indicative_value": r["indicative_value"],
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
    }


def _buyers(conn: sqlite3.Connection):
    rows = conn.execute(
        """
        SELECT buyer, sector, COALESCE(published_at, first_seen_at) AS activity_date
        FROM notices
        WHERE buyer IS NOT NULL AND buyer != '' AND sector IS NOT NULL
        """
    ).fetchall()

    by_buyer: dict[str, dict] = {}
    for r in rows:
        entry = by_buyer.setdefault(
            r["buyer"],
            {"buyer": r["buyer"], "notice_count": 0, "sectors": set(), "last_activity": None},
        )
        entry["notice_count"] += 1
        entry["sectors"].add(r["sector"])
        if r["activity_date"] and (entry["last_activity"] is None or r["activity_date"] > entry["last_activity"]):
            entry["last_activity"] = r["activity_date"]

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
    competitors = _competitors(conn) if tab != "buyers" else []
    buyers = _buyers(conn) if tab == "buyers" else []
    return render_template(
        "competitor_intel.html", tab=tab, competitors=competitors, buyers=buyers,
    )


@competitor_intel_bp.route("/competitor-intel/detail")
@login_required
def detail():
    conn = get_db()
    supplier_name = request.args.get("name", "")
    detail_data = _competitor_detail(conn, supplier_name)
    if detail_data is None:
        abort(404)
    return render_template("competitor_detail.html", **detail_data)


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
