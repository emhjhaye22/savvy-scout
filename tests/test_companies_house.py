import pytest
import requests

from savvy_scout.dashboard.companies_house import get_company_info


class FakeResponse:
    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json_data


def test_returns_none_without_api_key(conn):
    assert get_company_info(conn, "Acme Ltd", None) is None


def test_returns_and_caches_a_match(conn, monkeypatch):
    calls = []

    def fake_get(url, params, auth, timeout):
        calls.append(params)
        return FakeResponse({
            "items": [{
                "title": "ACME LIMITED", "company_number": "01234567",
                "address_snippet": "1 High Street, London", "company_status": "active",
            }]
        })

    monkeypatch.setattr("savvy_scout.dashboard.companies_house.requests.get", fake_get)

    result = get_company_info(conn, "Acme Ltd", "test-key")
    assert result == {
        "company_name": "ACME LIMITED", "company_number": "01234567",
        "address": "1 High Street, London", "status": "active",
    }
    assert len(calls) == 1

    # Second call must hit the cache, not the API again.
    result2 = get_company_info(conn, "Acme Ltd", "test-key")
    assert result2 == result
    assert len(calls) == 1

    row = conn.execute("SELECT * FROM company_lookups WHERE supplier_name = 'Acme Ltd'").fetchone()
    assert row["found"] == 1
    assert row["company_number"] == "01234567"


def test_no_match_is_cached_as_not_found(conn, monkeypatch):
    calls = []

    def fake_get(url, params, auth, timeout):
        calls.append(params)
        return FakeResponse({"items": []})

    monkeypatch.setattr("savvy_scout.dashboard.companies_house.requests.get", fake_get)

    assert get_company_info(conn, "Nobody Ever Registered This", "test-key") is None
    assert get_company_info(conn, "Nobody Ever Registered This", "test-key") is None
    assert len(calls) == 1

    row = conn.execute(
        "SELECT * FROM company_lookups WHERE supplier_name = 'Nobody Ever Registered This'"
    ).fetchone()
    assert row["found"] == 0


def test_network_failure_is_not_cached_and_returns_none(conn, monkeypatch):
    calls = []

    def fake_get(url, params, auth, timeout):
        calls.append(params)
        raise requests.ConnectionError("boom")

    monkeypatch.setattr("savvy_scout.dashboard.companies_house.requests.get", fake_get)

    assert get_company_info(conn, "Acme Ltd", "test-key") is None
    assert get_company_info(conn, "Acme Ltd", "test-key") is None
    # Not cached -- both calls hit the (failing) API again.
    assert len(calls) == 2

    row = conn.execute("SELECT * FROM company_lookups WHERE supplier_name = 'Acme Ltd'").fetchone()
    assert row is None
