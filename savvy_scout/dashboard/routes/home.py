"""Landing dashboard: a pipeline-health overview, separate from the
Approval Queue (SPEC.md B1's action list, now at /queue). Nothing here
changes state -- purely read-only counts and charts. "Needs attention" and
"Recent activity" now live as topbar notification dropdowns (see
dashboard/notifications.py + the context processor in dashboard/__init__.py)
rather than as panels on this page, so this view is scoped the same way the
queue is: sector owners see their own patch, Victoria sees everything.

2026-07-30: the scouting/sector numbers on this page are additionally scoped
to "clean" notices only -- a real sector match, within that sector's
configured CPV scope (config_sector_cpv_scope), and UK1-UK4 stage. This
matches what actually reaches an owner (everything else auto-rejects or
FLAGs as an open question, see triage.gates/workflow.approvals), so the
Overview reads as "what we're actually pursuing," not raw sweep volume."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.charts import bar_chart_series
from savvy_scout.dashboard.notifications import victoria_sourced_reject_sql
from savvy_scout.dashboard.routes.competitor_intel import _competitors, _parse_gbp
from savvy_scout.dashboard.scope_filter import IN_SCOPE_UK_STAGES, in_scope_filter_sql
from savvy_scout.sweep.runner import run_sweep

home_bp = Blueprint("home", __name__)

LONDON = ZoneInfo("Europe/London")
# The team works out of the Philippines -- the sweep schedule (see
# scheduler.py) and its Last/Next sweep display below are in Manila time.
# Deliberately separate from LONDON, which stays UK time for Sector
# Performance/Notices by Source -- those bucket by the UK sources' own
# calendar day, not by when the team happens to be looking at the page.
MANILA = ZoneInfo("Asia/Manila")
# Full week, not just Mon-Fri (2026-08-10): the daily sweep has no
# day_of_week restriction and already runs every day (see scheduler.py),
# and government sources do publish notices over the weekend -- confirmed
# live the same day when a Sunday-published notice was invisible in every
# day column despite correctly counting in This Week/This Month/YTD.
WEEKDAY_LABELS = ["Sat", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri"]

# Shared colour language for the Overview page's pie/donut charts -- the
# same colours are used for the legend swatches and the CSS conic-gradient
# background, so keeping them here (rather than duplicated in Jinja) is the
# single source of truth for both.
SECTOR_PALETTE = ["#2563EB", "#7C3AED", "#10B981", "#F59E0B", "#EC4899", "#14B8A6", "#64748B"]
TRIAGE_COLORS = {"PASS": "#10B981", "FLAG": "#F59E0B", "FAIL": "#DC2626"}


def _conic_gradient(shares: list[tuple[str, float]]) -> str:
    """Build a CSS conic-gradient() string from a list of (color, pct) slices
    (pct in 0..100). Used to render donut/pie charts with plain CSS -- no
    charting library or JS dependency needed."""
    stops = []
    acc = 0.0
    for color, pct in shares:
        if pct <= 0:
            continue
        start, acc = acc, acc + pct
        stops.append(f"{color} {start:.2f}% {acc:.2f}%")
    if not stops:
        return "conic-gradient(#E5E7EB 0% 100%)"
    return "conic-gradient(" + ", ".join(stops) + ")"


def _count(conn, query: str, params: tuple) -> int:
    return conn.execute(query, params).fetchone()[0]


def _pretty_date(value: datetime) -> str:
    return f"{value.day} {value:%b %Y}"


def _pretty_datetime(value: datetime) -> str:
    return value.strftime("%d %b %Y, %I:%M %p %Z")


def _pretty_manila_datetime(value: datetime) -> str:
    """Same shape as _pretty_datetime, but spells out "Philippines time"
    instead of relying on %Z -- Asia/Manila's own abbreviation is "PST"
    (Philippine Standard Time), easily misread as US Pacific time."""
    return value.strftime("%d %b %Y, %I:%M %p") + " Philippines time"


def _to_london_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LONDON)


def _report_date(row):
    """The date a notice counts under for Sector Performance: first_published_at
    (set once, on first insert, never touched again) if we have it, else the
    older published_at, else first_seen_at as a last-resort fallback for the
    rare notice a source published without a usable date. Deliberately NOT
    published_at first -- that's the source release's own "date" field, its
    LAST-UPDATED timestamp, which gets overwritten on every re-sweep. Without
    first_published_at, a 3-week-old notice amended/awarded/cancelled today
    would silently look newly published today (2026-08-10 finding). Never
    first_seen_at by default -- that's when OUR sweep found it, not when the
    buyer actually published it.

    publish_date_unknown (2026-08-10, same day, second finding) short-
    circuits all of that to None -- excluded from every date bucket
    entirely. Set when we first discovered this notice via an award,
    contract, amendment, or termination release rather than an actual
    tender/planning notice: that release carries no reliable publish date
    anywhere in its payload, so published_at/first_seen_at would both just
    silently be "today" (the day WE happened to first see it), same failure
    as the first finding just via a different path -- see sweep.dedupe and
    sources.ocds_parser.ParsedNotice.is_publish_event."""
    if row["publish_date_unknown"]:
        return None
    dt = (
        _to_london_datetime(row["first_published_at"])
        or _to_london_datetime(row["published_at"])
        or _to_london_datetime(row["first_seen_at"])
    )
    return dt.date() if dt else None


def _build_scope_predicate(conn):
    """Same "in scope" rule as scope_filter.in_scope_filter_sql (sector set,
    UK1-4 stage, within that sector's configured CPV scope), just as a
    Python predicate -- Sector Performance buckets by calendar day in
    Europe/London from a stored ISO timestamp with a mix of offsets, which
    isn't reliable to do in raw SQL, so the whole report is built in Python
    from one bulk fetch instead of one query per cell."""
    rows = conn.execute(
        "SELECT sector, allowed_cpv_prefixes FROM config_sector_cpv_scope WHERE enabled = 1"
    ).fetchall()
    if not rows:
        def predicate(sector, cpv_primary, uk_stage):
            return sector is not None and uk_stage in IN_SCOPE_UK_STAGES
        return predicate

    scope_map = {row["sector"]: json.loads(row["allowed_cpv_prefixes"]) for row in rows}

    def predicate(sector, cpv_primary, uk_stage):
        if uk_stage not in IN_SCOPE_UK_STAGES:
            return False
        prefixes = scope_map.get(sector)
        if prefixes is None:
            return False
        return bool(cpv_primary) and any(cpv_primary.startswith(p) for p in prefixes)

    return predicate


def _new_perf_bucket(weekdays):
    return {"days": {d: 0 for d in weekdays}, "last_week": 0, "week": 0, "month": 0, "ytd": 0}


def _accumulate_perf(
    bucket, report_date, weekdays, week_start, week_end, month_start, year_start,
    last_week_start=None, last_week_end=None,
):
    if report_date in bucket["days"]:
        bucket["days"][report_date] += 1
    if last_week_start is not None and last_week_start <= report_date <= last_week_end:
        bucket["last_week"] += 1
    if week_start <= report_date <= week_end:
        bucket["week"] += 1
    if report_date >= month_start:
        bucket["month"] += 1
    if report_date >= year_start:
        bucket["ytd"] += 1


def _perf_row(label, bucket, weekdays, **extra):
    return {
        "sector": label,
        "days": [bucket["days"][d] for d in weekdays],
        "last_week": bucket["last_week"],
        "week": bucket["week"],
        "month": bucket["month"],
        "ytd": bucket["ytd"],
        **extra,
    }


def _perf_windows(now_uk: datetime):
    """Shared Sat-Fri-this-week + week/month/YTD window boundaries for every
    Overview performance table (Sector, Source, ...), so they all report
    against the exact same date ranges. Full 7-day week, not just Mon-Fri
    (2026-08-10) -- the sweep runs every day and sources do publish notices
    on weekends, so a Sat/Sun notice needs its own day column too instead of
    only counting in the week/month/YTD rollups.

    Week starts Saturday, not Monday (2026-08-19, explicit request): the
    scouting work week ends Friday, so a Monday-Sunday week was hiding the
    weekend that had JUST passed the moment Monday's row reset -- Mark
    could no longer see Saturday/Sunday's own figures at all once a new
    week began. Starting the week on Saturday keeps the most recent
    weekend visible at the front of the row through the following week."""
    today = now_uk.date()
    week_start = today - timedelta(days=(now_uk.weekday() - 5) % 7)
    week_end = week_start + timedelta(days=6)
    weekdays = [week_start + timedelta(days=i) for i in range(7)]
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    return weekdays, week_start, week_end, month_start, year_start, last_week_start, last_week_end


def _day_headers(weekdays):
    return [{"label": label, "date": _pretty_date(d)} for label, d in zip(WEEKDAY_LABELS, weekdays)]


def _build_sector_performance(conn, now_uk: datetime) -> dict:
    """Sector Performance (2026-08-09): per-sector opportunity counts for
    Mon-Sun of the CURRENT week, then This Week/This Month/YTD, dated by
    publication date (not sweep date). Two grand-total rows are appended:
    "In Sector" (sum of the per-sector rows above, i.e. what in_scope_filter_sql
    also counts) and "Total Swept" (every notice pulled, matched to a sector
    or not) -- comparing the two shows how much of the raw sweep volume
    actually lands in-scope."""
    weekdays, week_start, week_end, month_start, year_start, last_week_start, last_week_end = _perf_windows(now_uk)

    predicate = _build_scope_predicate(conn)
    rows = conn.execute(
        "SELECT sector, cpv_primary, uk_stage, first_published_at, published_at, "
        "first_seen_at, publish_date_unknown FROM notices"
    ).fetchall()

    sector_buckets: dict[str, dict] = {}
    in_scope_total = _new_perf_bucket(weekdays)
    swept_total = _new_perf_bucket(weekdays)

    for row in rows:
        # Seed the sector's row as soon as it's in scope at all (2026-08-10
        # fix), regardless of whether THIS notice has a usable report_date --
        # otherwise a sector whose only in-scope notice has
        # publish_date_unknown=1 (discovered via an award/update release, no
        # confirmed publish date) never gets a row here at all, even though
        # it's correctly counted in Sector mix/Total scouted (found live:
        # Fintech's only in-scope notice was exactly this case, and the
        # sector silently vanished from this table entirely, not just its
        # date columns).
        is_in_scope = predicate(row["sector"], row["cpv_primary"], row["uk_stage"])
        if is_in_scope:
            sector_buckets.setdefault(row["sector"], _new_perf_bucket(weekdays))

        report_date = _report_date(row)
        if report_date is None:
            continue
        _accumulate_perf(
            swept_total, report_date, weekdays, week_start, week_end, month_start, year_start,
            last_week_start, last_week_end,
        )

        if is_in_scope:
            _accumulate_perf(
                in_scope_total, report_date, weekdays, week_start, week_end, month_start, year_start,
                last_week_start, last_week_end,
            )
            bucket = sector_buckets[row["sector"]]
            _accumulate_perf(
                bucket, report_date, weekdays, week_start, week_end, month_start, year_start,
                last_week_start, last_week_end,
            )

    perf_rows = [
        _perf_row(sector, bucket, weekdays)
        for sector, bucket in sorted(sector_buckets.items(), key=lambda kv: kv[1]["ytd"], reverse=True)
    ]
    perf_rows.append(_perf_row("In Sector (total)", in_scope_total, weekdays, is_total=True))
    perf_rows.append(_perf_row("Total Swept (all sources)", swept_total, weekdays, is_total=True, is_grand_total=True))

    return {"rows": perf_rows, "day_headers": _day_headers(weekdays)}


#: Approved (2026-08-10): APPROVED or anything further along the happy path
#: (CAPTURE_BRIEF_DRAFTED, DOCS_DOWNLOADED, CALENDARED, ACTIVE) -- all passed
#: through an APPROVED decision on the way, and APPROVED is only ever reached
#: via victoria_decision('approve') (see workflow.approvals.approve_phase2's
#: docstring -- an owner's own "approval" at Phase 2 only escalates to
#: Victoria, it never sets APPROVED itself), so this is inherently Victoria's
#: call alone already, with no extra filter needed.
_APPROVED_STATUSES = ("APPROVED", "CAPTURE_BRIEF_DRAFTED", "DOCS_DOWNLOADED", "CALENDARED", "ACTIVE")


def _approval_rate_sql(in_scope_where: str) -> str:
    """Shared SELECT for both the aggregate and per-owner Approved vs
    Rejected breakdowns -- rejected is deliberately scoped to
    victoria_sourced_reject_sql (2026-08-10, explicit request: "rejection
    and approval should [be the] same"), matching approved already being
    Victoria-only by construction. An owner's own earlier reject (from
    TO_REVIEW or AWAITING_PHASE2_APPROVAL, before ever reaching her) is a
    real, valid decision -- see that helper's own docstring -- it just isn't
    counted in THIS specific "how does Victoria's own call rate compare"
    panel."""
    approved_case = "n.status IN (" + ", ".join(f"'{s}'" for s in _APPROVED_STATUSES) + ")"
    return f"""
        SELECT
            n.owner AS owner,
            SUM(CASE WHEN {approved_case} THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN n.status = 'REJECTED' AND {victoria_sourced_reject_sql('n')} THEN 1 ELSE 0 END) AS rejected
        FROM notices n
        WHERE {in_scope_where}
    """


def _approval_rate_from_counts(approved: int, rejected: int) -> dict:
    total = approved + rejected
    return {
        "approved": approved,
        "rejected": rejected,
        "total": total,
        "approved_pct": round(approved / total * 100, 1) if total else 0,
        "rejected_pct": round(rejected / total * 100, 1) if total else 0,
    }


def _build_approval_rate(conn, in_scope_where, in_scope_params) -> dict:
    """Approved vs Rejected (2026-08-10, replacing the Contract Expiry Radar
    panel -- removing it was a deliberate call: its own future re-procurement
    lead will still show up normally once the buyer actually publishes the
    new tender, since that falls within the regular sweep's lookback window
    at that time; the panel only bought advance warning, which the team
    decided wasn't worth it). A win-rate signal at Victoria's own decision
    level specifically -- see _approval_rate_sql. Anything still awaiting a
    decision, parked/monitoring, or rejected before ever reaching her isn't
    counted either way here."""
    row = conn.execute(_approval_rate_sql(in_scope_where), tuple(in_scope_params)).fetchone()
    return _approval_rate_from_counts(row["approved"] or 0, row["rejected"] or 0)


def _build_approval_rate_by_owner(conn, in_scope_where, in_scope_params) -> list[dict]:
    """Same Approved vs Rejected definition as _build_approval_rate, broken
    out per owner (2026-08-10 explicit request) -- lets each sector owner
    (and Victoria, looking across everyone) see whose escalated notices
    tend to land with her vs get turned down, not just one aggregate
    figure."""
    rows = conn.execute(
        _approval_rate_sql(in_scope_where) + " AND n.owner IS NOT NULL GROUP BY n.owner ORDER BY n.owner",
        tuple(in_scope_params),
    ).fetchall()
    result = [
        {"owner": r["owner"], **_approval_rate_from_counts(r["approved"] or 0, r["rejected"] or 0)}
        for r in rows
    ]
    return [r for r in result if r["total"] > 0]


def _build_top_buyers(conn, in_scope_where, in_scope_params, limit=8) -> list[dict]:
    rows = conn.execute(
        f"""
        SELECT buyer, COUNT(*) AS cnt
        FROM notices
        WHERE {in_scope_where} AND buyer IS NOT NULL
        GROUP BY buyer
        ORDER BY cnt DESC, buyer ASC
        LIMIT ?
        """,
        (*in_scope_params, limit),
    ).fetchall()
    max_count = rows[0]["cnt"] if rows else 1
    return [{"buyer": r["buyer"], "count": r["cnt"], "pct": round(r["cnt"] / max_count * 100, 1)} for r in rows]


def _build_top_competitors(conn, limit=8) -> list[dict]:
    """Mirrors _build_top_buyers, but for Competitor Intel's own aggregation
    (2026-09-06) rather than a fresh query -- reuses the same relevance
    filter (Gate 2, not just sector match) so this doesn't reintroduce the
    "taxi firm outranking real competitors" noise Competitor Intel itself
    already fixed. Deliberately NOT scoped by in_scope_filter_sql: award
    notices are UK5, which that filter excludes by design, same reasoning
    as Competitor Intel's own screen."""
    relevant = [c for c in _competitors(conn) if c["relevant"]][:limit]
    max_count = relevant[0]["award_count"] if relevant else 1
    return [
        {"supplier_name": c["supplier_name"], "count": c["award_count"], "pct": round(c["award_count"] / max_count * 100, 1)}
        for c in relevant
    ]


def _build_sector_spend(conn) -> list[dict]:
    """Market-size-by-sector panel named in the UI alignment build's
    Dashboard spec (2026-09-06): aggregated award value per sector, across
    every configured sector (config_owner_map, the same source of truth
    admin.py uses -- not hardcoded, so it stays correct if a sector is ever
    added/renamed) regardless of whether each has any priced awards yet --
    a sector with no data shows a real zero bar, not an omitted one.
    Deliberately NOT using in_scope_filter_sql: that excludes UK5
    (awarded/closed) by design, and award notices are UK5 by definition,
    same reasoning as Competitor Intel's aggregation."""
    sector_order = [
        r["sector"] for r in conn.execute("SELECT sector FROM config_owner_map ORDER BY sector").fetchall()
    ]
    rows = conn.execute(
        "SELECT sector, indicative_value FROM notices WHERE is_award = 1 AND sector IS NOT NULL"
    ).fetchall()
    totals = {sector: 0.0 for sector in sector_order}
    for row in rows:
        parsed = _parse_gbp(row["indicative_value"])
        if parsed is not None and row["sector"] in totals:
            totals[row["sector"]] += parsed
    buckets = [(sector, totals[sector]) for sector in sector_order]
    return bar_chart_series(buckets)


def _build_source_performance(conn, now_uk: datetime) -> dict:
    """Notices by Source (2026-08-09): where each swept notice actually came
    from (Find a Tender, Contracts Finder, Public Contracts Scotland,
    Sell2Wales, eTendersNI -- see sources/ and config_sources), same
    Mon-Sun/week/month/YTD shape as Sector Performance, dated by publication
    date. Unfiltered by sector/CPV scope -- this is about sweep coverage per
    source, not what's in scope, so it should total to the same "Total
    Swept" figure Sector Performance shows."""
    weekdays, week_start, week_end, month_start, year_start, last_week_start, last_week_end = _perf_windows(now_uk)

    rows = conn.execute(
        "SELECT source, first_published_at, published_at, first_seen_at, publish_date_unknown FROM notices"
    ).fetchall()

    source_buckets: dict[str, dict] = {}
    grand_total = _new_perf_bucket(weekdays)

    for row in rows:
        # Seed the source's row unconditionally (2026-08-10 fix, same as
        # Sector Performance above) -- a source whose every notice happens
        # to have publish_date_unknown=1 must still show its own zeroed row
        # here, not vanish entirely.
        bucket = source_buckets.setdefault(row["source"] or "Unknown", _new_perf_bucket(weekdays))

        report_date = _report_date(row)
        if report_date is None:
            continue
        _accumulate_perf(
            grand_total, report_date, weekdays, week_start, week_end, month_start, year_start,
            last_week_start, last_week_end,
        )
        _accumulate_perf(
            bucket, report_date, weekdays, week_start, week_end, month_start, year_start,
            last_week_start, last_week_end,
        )

    perf_rows = [
        _perf_row(source, bucket, weekdays)
        for source, bucket in sorted(source_buckets.items(), key=lambda kv: kv[1]["ytd"], reverse=True)
    ]
    perf_rows.append(_perf_row("Total", grand_total, weekdays, is_total=True, is_grand_total=True))

    return {"rows": perf_rows, "day_headers": _day_headers(weekdays)}


@home_bp.route("/")
@login_required
def index():
    conn = get_db()
    now = datetime.now(timezone.utc)
    uk_now = now.astimezone(LONDON)
    manila_now = now.astimezone(MANILA)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    tomorrow_start = today_start + timedelta(days=1)
    # Saturday-start week (2026-08-19), matching _perf_windows below --
    # otherwise this KPI tile's "This Week" figure would disagree with the
    # Sector/Source Performance tables on what "this week" even means.
    week_start = (now - timedelta(days=(now.weekday() - 5) % 7)).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    in_scope_where, in_scope_params = in_scope_filter_sql(conn)

    scouting_total = _count(conn, f"SELECT COUNT(*) FROM notices WHERE {in_scope_where}", tuple(in_scope_params))
    scouting_ytd = _count(
        conn, f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ?",
        (*in_scope_params, year_start.isoformat()),
    )
    scouting_month = _count(
        conn, f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ?",
        (*in_scope_params, month_start.isoformat()),
    )
    scouting_week = _count(
        conn, f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ?",
        (*in_scope_params, week_start.isoformat()),
    )
    scouting_today = _count(
        conn, f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ?",
        (*in_scope_params, today_start.isoformat()),
    )
    scouting_yesterday = _count(
        conn,
        f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ? AND first_seen_at < ?",
        (*in_scope_params, yesterday_start.isoformat(), today_start.isoformat()),
    )
    swept_today = _count(
        conn,
        f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND last_swept_at >= ? AND last_swept_at < ?",
        (*in_scope_params, today_start.isoformat(), tomorrow_start.isoformat()),
    )
    new_today = _count(
        conn,
        f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND first_seen_at >= ? AND first_seen_at < ?",
        (*in_scope_params, today_start.isoformat(), tomorrow_start.isoformat()),
    )
    updated_today = _count(
        conn,
        f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} "
        "AND last_swept_at >= ? AND last_swept_at < ? AND first_seen_at < ?",
        (*in_scope_params, today_start.isoformat(), tomorrow_start.isoformat(), today_start.isoformat()),
    )
    swept_yesterday = _count(
        conn,
        f"SELECT COUNT(*) FROM notices WHERE {in_scope_where} AND last_swept_at >= ? AND last_swept_at < ?",
        (*in_scope_params, yesterday_start.isoformat(), today_start.isoformat()),
    )

    last_swept_row = conn.execute(
        "SELECT MAX(last_swept_at) AS last_swept_at FROM notices WHERE last_swept_at IS NOT NULL"
    ).fetchone()
    sweep_last_run = None
    sweep_next_run = None
    try:
        next_manila_run = manila_now.replace(hour=8, minute=0, second=0, microsecond=0)
        if manila_now >= next_manila_run:
            next_manila_run += timedelta(days=1)
        sweep_next_run = _pretty_manila_datetime(next_manila_run)
    except Exception:
        sweep_next_run = "8:00 AM Philippines time daily"

    last_swept_at = last_swept_row["last_swept_at"] if last_swept_row else None
    if last_swept_at:
        try:
            last_swept_dt = datetime.fromisoformat(last_swept_at)
            sweep_last_run = _pretty_manila_datetime(last_swept_dt.astimezone(MANILA))
        except Exception:
            sweep_last_run = last_swept_at

    sector_rows = conn.execute(
        f"""
        SELECT sector, COUNT(*) AS cnt
        FROM notices
        WHERE {in_scope_where}
        GROUP BY sector
        ORDER BY cnt DESC, sector ASC
        """,
        tuple(in_scope_params),
    ).fetchall()
    sector_split = [
        {
            "sector": row["sector"],
            "count": row["cnt"],
            "share": round((row["cnt"] / scouting_total * 100) if scouting_total else 0, 1),
        }
        for row in sector_rows
    ]
    sector_pie_gradient = _conic_gradient(
        [(SECTOR_PALETTE[i % len(SECTOR_PALETTE)], row["share"]) for i, row in enumerate(sector_split)]
    )

    # Sector Performance (2026-08-09): dated by publication date, not sweep
    # date -- see _build_sector_performance. Built once here rather than as
    # several more SQL queries, since bucketing by Europe/London calendar day
    # from a stored ISO timestamp with mixed UTC offsets isn't reliable to do
    # in raw SQL.
    sector_performance = _build_sector_performance(conn, uk_now)
    source_performance = _build_source_performance(conn, uk_now)
    approval_rate = _build_approval_rate(conn, in_scope_where, in_scope_params)
    approval_rate_by_owner = _build_approval_rate_by_owner(conn, in_scope_where, in_scope_params)
    top_buyers = _build_top_buyers(conn, in_scope_where, in_scope_params)
    top_competitors = _build_top_competitors(conn)
    sector_spend = _build_sector_spend(conn)

    # Cross-feature tiles (2026-09-05 UI alignment): Signals, Competitor
    # Intel, and Shortlists are separate screens, but a one-glance count of
    # each belongs on the landing page too. All four are cheap single-table
    # counts, no scope_filter involved -- none of contract_expiry,
    # watched_competitors, or shortlisted_notices have sector/CPV columns of
    # their own to filter by.
    renewals_due_90d = _count(
        conn,
        "SELECT COUNT(*) FROM contract_expiry WHERE end_date <= ?",
        ((now + timedelta(days=90)).isoformat(),),
    )
    new_signals_week = _count(
        conn,
        "SELECT COUNT(*) FROM contract_expiry WHERE created_at >= ?",
        (week_start.isoformat(),),
    )
    competitors_watched = _count(conn, "SELECT COUNT(*) FROM watched_competitors", ())
    items_shortlisted = _count(conn, "SELECT COUNT(*) FROM shortlisted_notices", ())

    latest_triage_rows = conn.execute(
        f"""
        WITH latest AS (
            SELECT notice_id, MAX(id) AS max_id
            FROM triage_runs
            GROUP BY notice_id
        )
        SELECT tr.headline_outcome AS outcome, COUNT(*) AS cnt
        FROM triage_runs tr
        JOIN latest l ON l.max_id = tr.id
        JOIN notices n ON n.id = tr.notice_id
        WHERE {in_scope_where}
        GROUP BY tr.headline_outcome
        """,
        tuple(in_scope_params),
    ).fetchall()
    triage_totals = {row["outcome"]: row["cnt"] for row in latest_triage_rows}
    # Legacy MAYBE headline outcomes (recorded before Gate 3/5's 2026-08-15
    # rewrite, see gates.py's headline_outcome) fold into FLAG here -- no
    # gate has produced MAYBE since, and it's not a distinct outcome.
    triage_totals["FLAG"] = triage_totals.get("FLAG", 0) + triage_totals.pop("MAYBE", 0)
    triage_order = ["PASS", "FLAG", "FAIL"]
    triage_total = sum(triage_totals.values())
    triage_outcomes = [
        {
            "label": label,
            "value": triage_totals.get(label, 0),
            "share": round((triage_totals.get(label, 0) / triage_total * 100) if triage_total else 0, 1),
        }
        for label in triage_order
    ]
    triage_pie_gradient = _conic_gradient(
        [(TRIAGE_COLORS.get(item["label"], "#64748B"), item["share"]) for item in triage_outcomes]
    )

    scouting_report = {
        "total": scouting_total,
        "tiles": [
            {"label": "Scouted YTD", "value": scouting_ytd, "hint": f"Since {_pretty_date(year_start)}"},
            {"label": "This Month", "value": scouting_month, "hint": f"Since {_pretty_date(month_start)}"},
            {"label": "This Week", "value": scouting_week, "hint": f"Since {_pretty_date(week_start)}"},
            {"label": "Today", "value": scouting_today, "hint": f"Since {_pretty_date(today_start)}"},
        ],
        "daily": {
            "yesterday_seen": scouting_yesterday,
            "today_seen": scouting_today,
            "yesterday_swept": swept_yesterday,
            "today_swept": swept_today,
            "today_new": new_today,
            "today_updated": updated_today,
        },
        "sector_split": sector_split,
        "sector_pie_gradient": sector_pie_gradient,
        "triage_outcomes": triage_outcomes,
        "triage_pie_gradient": triage_pie_gradient,
    }

    return render_template(
        "home.html",
        scouting_report=scouting_report,
        sector_performance=sector_performance,
        source_performance=source_performance,
        approval_rate=approval_rate,
        approval_rate_by_owner=approval_rate_by_owner,
        top_buyers=top_buyers,
        top_competitors=top_competitors,
        sector_spend=sector_spend,
        renewals_due_90d=renewals_due_90d,
        new_signals_week=new_signals_week,
        competitors_watched=competitors_watched,
        items_shortlisted=items_shortlisted,
        sweep_note={"last_run": sweep_last_run, "next_run": sweep_next_run},
        sector_palette=SECTOR_PALETTE,
        triage_colors=TRIAGE_COLORS,
    )


@home_bp.route("/sweep-now", methods=["POST"])
@login_required
def sweep_now():
    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    conn = get_db()
    stats = run_sweep(conn, settings, triggered_by=current_user.display_name)
    flash(
        f"Sweep complete: pulled {stats['pulled']} notices, surfaced {stats['expiring_leads']} expiring leads, triaged {stats['triaged']} new notices.",
        "success",
    )
    return redirect(url_for("home.index"))


@home_bp.route("/generate-reports-now", methods=["POST"])
@login_required
def generate_reports_now():
    # Lazy import: savvy_scout.scheduler pulls in reporting.reports, which
    # imports savvy_scout.dashboard.scope_filter -- importing that at module
    # load time here would re-enter this still-initializing dashboard
    # package and circular-import.
    from savvy_scout.scheduler import run_monthly_report_job, run_weekly_report_job

    settings = current_app.config["SAVVY_SCOUT_SETTINGS"]
    run_weekly_report_job()
    run_monthly_report_job()
    if settings.report_recipient_email:
        flash("Weekly and Monthly Trifork reports generated and emailed.", "success")
    else:
        flash(
            "Weekly and Monthly Trifork reports generated in the reports folder. "
            "Set REPORT_RECIPIENT_EMAIL to have them emailed automatically.",
            "success",
        )
    return redirect(url_for("home.index"))
