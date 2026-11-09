"""Tests for SQLite state store, multi-domain round-robin seat allocation, and CLI commands."""
from __future__ import annotations

import json

import pytest

from directory_seats.generator import generate_domain_seats
from gsuite_seatpool.cli import main as cli_main
from seat_allocator.allocator import allocate_round_robin, domain_daily_usage
from seat_allocator.store import (
    add_seat,
    counts,
    set_tenant_status,
    upsert_tenant_domain,
)


def _seed_domains(db, domains: list[str], seats_per_domain: int = 5):
    for fqdn in domains:
        tid = upsert_tenant_domain(db, fqdn, "AdminPass123!", max_seats=seats_per_domain)
        for s in generate_domain_seats(fqdn, count=seats_per_domain):
            add_seat(db, tid, s, status="provisioned")


def test_round_robin_interleaves_domains(db):
    domains = ["d1.example.test", "d2.example.test", "d3.example.test"]
    _seed_domains(db, domains, seats_per_domain=4)

    res = allocate_round_robin(db, target="github", count=6, max_per_domain_day=3)
    assert res.ok
    picked_domains = [a["fqdn"] for a in res.allocated]
    assert picked_domains == [
        "d1.example.test",
        "d2.example.test",
        "d3.example.test",
        "d1.example.test",
        "d2.example.test",
        "d3.example.test",
    ]
    assert [a["seat_index"] for a in res.allocated] == [1, 1, 1, 2, 2, 2]


def test_per_domain_daily_budget_guard(db):
    domains = ["d1.example.test", "d2.example.test"]
    _seed_domains(db, domains, seats_per_domain=5)

    res = allocate_round_robin(db, target="codebuddy", count=6, max_per_domain_day=2)
    assert not res.ok
    assert len(res.allocated) == 4
    assert domain_daily_usage(db, "d1.example.test", "codebuddy") == 2
    assert domain_daily_usage(db, "d2.example.test", "codebuddy") == 2
    assert "d1.example.test" in res.skipped_domains
    assert "d2.example.test" in res.skipped_domains


def test_seat_reusable_across_different_targets(db):
    _seed_domains(db, ["d1.example.test"], seats_per_domain=1)
    r_gh = allocate_round_robin(db, target="github", count=1, max_per_domain_day=3)
    r_cb = allocate_round_robin(db, target="codebuddy", count=1, max_per_domain_day=3)
    assert r_gh.ok and r_cb.ok
    assert r_gh.allocated[0]["email"] == r_cb.allocated[0]["email"]


def test_suspended_tenant_skipped(db):
    _seed_domains(db, ["active.example.test", "suspended.example.test"], seats_per_domain=2)
    set_tenant_status(db, "suspended.example.test", "suspended", "hold")
    res = allocate_round_robin(db, target="github", count=2, max_per_domain_day=2)
    assert res.ok
    assert all(a["fqdn"] == "active.example.test" for a in res.allocated)


def test_invalid_status_rejected(db):
    _seed_domains(db, ["d1.example.test"], seats_per_domain=1)
    with pytest.raises(ValueError):
        set_tenant_status(db, "d1.example.test", "unknown_state")


def test_cli_end_to_end(tmp_path, capsys):
    db_path = str(tmp_path / "cli.db")
    csv_dir = str(tmp_path / "csv")

    assert cli_main([
        "--db", db_path,
        "init-tenant",
        "--fqdn", "alpha.example.test",
        "--fqdn", "beta.example.test",
        "--seats", "5",
    ]) == 0
    capsys.readouterr()

    assert cli_main([
        "--db", db_path,
        "export-csv",
        "--out-dir", csv_dir,
    ]) == 0
    out_csv = json.loads(capsys.readouterr().out)
    assert len(out_csv["csv_files"]) == 2

    from seat_allocator.store import open_db
    connection = open_db(db_path)
    connection.execute("UPDATE seats SET status='provisioned'")
    connection.commit()
    connection.close()

    assert cli_main([
        "--db", db_path,
        "allocate",
        "--target", "github",
        "--count", "4",
        "--max-per-domain-day", "2",
    ]) == 0
    out_alloc = json.loads(capsys.readouterr().out)
    assert out_alloc["allocated_count"] == 4
