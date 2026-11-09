"""Tests for local seat generation, CSV formatting, and Admin SDK Directory API."""
from __future__ import annotations

import csv

import pytest

from directory_seats.client import DirectoryApiError, DirectoryClient
from directory_seats.generator import (
    ADMIN_CSV_HEADERS,
    CLOUD_IDENTITY_FREE_CAP,
    export_admin_csv,
    generate_domain_seats,
    make_seat,
    redact_seat,
)


def test_seat_generation_has_stable_identity_but_fresh_secrets():
    s1 = make_seat("alpha.example.test", 1)
    s2 = make_seat("alpha.example.test", 1)
    assert redact_seat(s1) == redact_seat(s2)
    assert s1["email"] == "unit01@alpha.example.test"
    assert s1["password"] != s2["password"]
    assert s1["change_password_at_next_login"] is False


def test_seat_password_has_at_least_24_characters():
    assert len(make_seat("alpha.example.test", 1)["password"]) >= 24


def test_seat_password_policy():
    s = make_seat("alpha.example.test", 7)
    pw = s["password"]
    assert len(pw) >= 10
    assert any(c.isupper() for c in pw)
    assert any(c.islower() for c in pw)
    assert any(c.isdigit() for c in pw)
    assert any(not c.isalnum() for c in pw)


def test_legacy_constant_preserves_local_default_not_a_ceiling():
    assert CLOUD_IDENTITY_FREE_CAP == 50
    seats = generate_domain_seats("alpha.example.test")
    assert len(seats) == CLOUD_IDENTITY_FREE_CAP
    assert make_seat("alpha.example.test", 51)["email"] == "unit51@alpha.example.test"


def test_redact_seat_strips_password():
    s = make_seat("alpha.example.test", 1)
    r = redact_seat(s)
    assert "password" not in r
    assert r["email"] == s["email"]


def test_export_admin_csv_format(tmp_path):
    seats = generate_domain_seats("alpha.example.test", count=3)
    out = export_admin_csv(seats, str(tmp_path / "alpha.csv"))
    with open(out, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ADMIN_CSV_HEADERS
    assert len(rows) == 4
    assert rows[1][2] == "unit01@alpha.example.test"
    assert rows[1][5] == "False"


def test_directory_api_insert_list_delete(session_adapter):
    dc = DirectoryClient(base_url="http://mock.local", session=session_adapter)
    seat = make_seat("alpha.example.test", 1)
    created = dc.insert_user(seat)
    assert created["primaryEmail"] == "unit01@alpha.example.test"
    assert created["changePasswordAtNextLogin"] is False

    # Duplicate rejected with 409
    with pytest.raises(DirectoryApiError) as dup:
        dc.insert_user(seat)
    assert dup.value.status == 409

    users = dc.list_users("alpha.example.test")
    assert len(users) == 1

    dc.delete_user("unit01@alpha.example.test")
    assert dc.list_users("alpha.example.test") == []


def test_directory_api_50_user_cap(session_adapter):
    dc = DirectoryClient(base_url="http://mock.local", session=session_adapter)
    for seat in generate_domain_seats("cap.example.test", count=50):
        dc.insert_user(seat)
    assert len(dc.list_users("cap.example.test")) == 50

    extra = {
        "email": "unit51@cap.example.test",
        "password": "ValidPassword123!",
        "first_name": "Extra",
        "last_name": "Seat",
    }
    with pytest.raises(DirectoryApiError) as exc:
        dc.insert_user(extra)
    assert exc.value.status == 403
