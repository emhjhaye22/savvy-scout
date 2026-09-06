from savvy_scout.dashboard.charts import bar_chart_series


def test_bar_chart_series_normalizes_to_max():
    result = bar_chart_series([("Jan", 50), ("Feb", 100), ("Mar", 25)])
    assert result == [
        {"label": "Jan", "value": 50, "pct": 50.0},
        {"label": "Feb", "value": 100, "pct": 100.0},
        {"label": "Mar", "value": 25, "pct": 25.0},
    ]


def test_bar_chart_series_all_zero_returns_zero_pct_not_divide_by_zero():
    result = bar_chart_series([("Jan", 0), ("Feb", 0)])
    assert result == [
        {"label": "Jan", "value": 0, "pct": 0},
        {"label": "Feb", "value": 0, "pct": 0},
    ]


def test_bar_chart_series_empty_list():
    assert bar_chart_series([]) == []


def test_bar_chart_series_preserves_order():
    result = bar_chart_series([("C", 1), ("A", 3), ("B", 2)])
    assert [r["label"] for r in result] == ["C", "A", "B"]
