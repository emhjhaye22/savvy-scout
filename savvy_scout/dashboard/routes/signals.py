"""Signals: pre-tender and renewal alerts. Only the renewal half is real
today -- built entirely from the existing expiry radar (sweep.expiry_radar,
SPEC.md A2), which already logs every award notice's contract_expiry row
with no new data source required. Pre-tender detection (forward plans,
committee minutes, advance pipeline listings) has no data source in this
app yet; this screen does not fake it with placeholder rows."""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, render_template
from flask_login import login_required

from savvy_scout.dashboard.auth import get_db
from savvy_scout.dashboard.charts import bar_chart_series

signals_bp = Blueprint("signals", __name__)

MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _renewals_by_month(signals: list[dict], months_ahead: int = 12) -> list[dict]:
    """Buckets renewals by the month their contract ends, starting this
    calendar month -- the same "expiring contracts by month" shape found in
    all three competitor tools researched for this build (Stotles, Tussell,
    Contracts Advance each show some version of it). Renewals with no
    end_date, or one outside the window, aren't counted -- this chart is
    honest about only covering what's dated and near-term, not a stand-in
    for the full signals list above it."""
    now = datetime.now(timezone.utc)
    bucket_starts = []
    year, month = now.year, now.month
    for _ in range(months_ahead):
        bucket_starts.append((year, month))
        month += 1
        if month > 12:
            month = 1
            year += 1

    counts = {ym: 0 for ym in bucket_starts}
    for s in signals:
        try:
            end_date = datetime.fromisoformat(s["end_date"])
        except (TypeError, ValueError):
            continue
        key = (end_date.year, end_date.month)
        if key in counts:
            counts[key] += 1

    buckets = [(f"{MONTH_LABELS[m - 1]} {y}", counts[(y, m)]) for y, m in bucket_starts]
    return bar_chart_series(buckets)


def _urgency(review_date_str: str, end_date_str: str) -> str:
    now = datetime.now(timezone.utc)
    try:
        review_date = datetime.fromisoformat(review_date_str)
        if review_date.tzinfo is None:
            review_date = review_date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return "upcoming"
    try:
        end_date = datetime.fromisoformat(end_date_str)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        end_date = None

    if review_date <= now:
        return "due_now"
    if end_date and end_date <= now + timedelta(days=90):
        return "due_soon"
    return "upcoming"


@signals_bp.route("/signals")
@login_required
def index():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT ce.id, ce.notice_ref, ce.buyer, ce.title, ce.end_date, ce.review_date,
               n.id AS notice_id, n.sector, n.cpv_primary, n.value_amount_gross, n.owner
        FROM contract_expiry ce
        LEFT JOIN notices n ON n.ref = ce.notice_ref
        ORDER BY ce.review_date ASC
        """
    ).fetchall()

    signals = []
    for row in rows:
        signals.append(
            {
                "id": row["id"],
                "notice_id": row["notice_id"],
                "notice_ref": row["notice_ref"],
                "buyer": row["buyer"],
                "title": row["title"],
                "sector": row["sector"],
                "cpv_primary": row["cpv_primary"],
                "value_amount_gross": row["value_amount_gross"],
                "end_date": row["end_date"],
                "review_date": row["review_date"],
                "urgency": _urgency(row["review_date"], row["end_date"]),
            }
        )

    counts = {"due_now": 0, "due_soon": 0, "upcoming": 0}
    for s in signals:
        counts[s["urgency"]] += 1

    renewals_chart = _renewals_by_month(signals)

    return render_template("signals.html", signals=signals, counts=counts, renewals_chart=renewals_chart)
