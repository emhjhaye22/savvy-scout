"""Bar chart data prep (2026-09-06 UI alignment, researching Stotles/
Tussell/Contracts Advance turned up one pattern all three share that Savvy
Scout had none of: real charts, not just numbers-in-boxes). Plain CSS bars,
no JS or SVG charting library -- consistent with the conic-gradient donut
and meter-fill bar already in home.html. This module only computes
normalized heights; the template renders a flex row of divs."""


MONTH_LABELS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def bar_chart_series(buckets: list[tuple[str, float]]) -> list[dict]:
    """buckets: [(label, value), ...] in display order. Returns
    [{"label", "value", "pct"}, ...] where pct is that bucket's value as a
    share of the largest bucket (0-100), so bar heights can be set with
    plain CSS. All buckets get pct=0 if every value is zero, rather than
    dividing by zero."""
    max_value = max((value for _, value in buckets), default=0)
    return [
        {
            "label": label,
            "value": value,
            "pct": (value / max_value * 100) if max_value else 0,
        }
        for label, value in buckets
    ]
