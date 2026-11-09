"""Offline organization capacity, migration, and database file safety tests."""
import os
import stat
import sqlite3

import pytest

from seat_allocator import store


def test_explicit_organization_links_share_planned_capacity(db):
    first = store.upsert_tenant_domain(db, "first.test", "unused")
    store.upsert_tenant_domain(db, "second.test", "unused")
    store.upsert_organization(db, "customer-a", 3)
    store.link_domain(db, "customer-a", " FIRST.test ")
    store.link_domain(db, "customer-a", "second.test")
    store.link_domain(db, "customer-a", "first.test")
    details = store.organization_capacity(db, "customer-a")
    assert details == {
        "organization_key": "customer-a", "capacity": 3,
        "reserved": 0, "available": 3,
        "domains": ["first.test", "second.test"],
    }
    assert db.execute("SELECT tenant_id FROM organization_domains ORDER BY tenant_id").fetchone()[0] == first
    assert db.execute("SELECT status FROM tenants LIMIT 1").fetchone()[0] == "staged"


def seat(fqdn, index):
    return {"fqdn": fqdn, "seat_index": index, "email": f"user{index}@{fqdn}",
            "password": "unused", "first_name": "Test", "last_name": "User"}


def test_link_over_capacity_rolls_back_and_preserves_existing_link(db):
    first = store.upsert_tenant_domain(db, "first.test", "unused")
    second = store.upsert_tenant_domain(db, "second.test", "unused")
    store.add_seat(db, first, seat("first.test", 1))
    store.add_seat(db, second, seat("second.test", 1), status="suspended")
    store.upsert_organization(db, "shared", 1)
    store.upsert_organization(db, "previous", 2)
    store.link_domain(db, "shared", "first.test")
    store.link_domain(db, "previous", "second.test")
    with pytest.raises(ValueError, match="capacity"):
        store.link_domain(db, "shared", "second.test")
    assert store.organization_capacity(db, "shared")["domains"] == ["first.test"]
    assert store.organization_capacity(db, "previous")["domains"] == ["second.test"]
    assert db.execute("SELECT COUNT(*) FROM seats").fetchone()[0] == 2
    assert not db.in_transaction


@pytest.mark.parametrize("status", ["ready", "provisioned", "suspended"])
def test_add_seat_enforces_shared_reservations_and_allows_dead(db, status):
    first = store.upsert_tenant_domain(db, "first.test", "unused")
    second = store.upsert_tenant_domain(db, "second.test", "unused")
    store.upsert_organization(db, "shared", 1)
    for domain in ["first.test", "second.test"]:
        store.link_domain(db, "shared", domain)
    original = store.add_seat(db, first, seat("first.test", 1), status=status)
    assert store.add_seat(db, first, seat("first.test", 1)) == original
    store.add_seat(db, second, seat("second.test", 1), status="dead")
    with pytest.raises(ValueError, match="capacity"):
        store.add_seat(db, second, seat("second.test", 2), status=status)
    assert store.organization_capacity(db, "shared")["reserved"] == 1
    assert db.execute("SELECT COUNT(*) FROM seats").fetchone()[0] == 2
    assert not db.in_transaction


def test_capacity_cannot_shrink_below_reservations(db):
    tid = store.upsert_tenant_domain(db, "first.test", "unused")
    store.upsert_organization(db, "shared", 2)
    store.link_domain(db, "shared", "first.test")
    store.add_seat(db, tid, seat("first.test", 1))
    store.add_seat(db, tid, seat("first.test", 2))
    with pytest.raises(ValueError, match="capacity"):
        store.upsert_organization(db, "shared", 1)
    assert store.organization_capacity(db, "shared")["capacity"] == 2


@pytest.mark.parametrize("capacity", [0, -1, 1.5, True, "3"])
def test_organization_capacity_requires_positive_integer(db, capacity):
    with pytest.raises(ValueError, match="positive integer"):
        store.upsert_organization(db, "customer", capacity)
    assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0


@pytest.mark.parametrize("key", ["", "  ", None])
def test_organization_key_cannot_be_empty(db, key):
    with pytest.raises(ValueError, match="organization_key"):
        store.upsert_organization(db, key, 1)


def test_tenant_capacity_is_positive_without_license_assumptions(db):
    store.upsert_tenant_domain(db, "large.test", "unused", max_seats=200)
    assert db.execute("SELECT max_seats FROM tenants").fetchone()[0] == 200
    with pytest.raises(ValueError, match="positive"):
        store.upsert_tenant_domain(db, "large.test", "unused", max_seats=0)
    assert db.execute("SELECT max_seats FROM tenants").fetchone()[0] == 200


@pytest.mark.parametrize("mask", [0, 0o777])
def test_new_database_is_private_regardless_of_umask(tmp_path, mask):
    path = tmp_path / "private.db"
    previous = os.umask(mask)
    try:
        store.open_db(path).close()
    finally:
        os.umask(previous)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("dangling", [False, True])
def test_database_symlinks_are_rejected_without_touching_target(tmp_path, dangling):
    target = tmp_path / "target.db"
    if not dangling:
        target.write_bytes(b"do not modify")
    link = tmp_path / "link.db"
    link.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        store.open_db(link)
    assert link.is_symlink()
    if dangling:
        assert not target.exists()
    else:
        assert target.read_bytes() == b"do not modify"


def test_existing_database_permissions_are_preserved(tmp_path):
    path = tmp_path / "existing.db"
    sqlite3.connect(path).close()
    path.chmod(0o640)
    store.open_db(path).close()
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_seat_cannot_bypass_organization_using_another_domain(db):
    first = store.upsert_tenant_domain(db, "first.test", "unused")
    second = store.upsert_tenant_domain(db, "second.test", "unused")
    store.upsert_organization(db, "shared", 1)
    store.link_domain(db, "shared", "first.test")
    store.add_seat(db, first, seat("first.test", 1))
    with pytest.raises(ValueError, match="tenant"):
        store.add_seat(db, second, seat("first.test", 2))
    with pytest.raises(ValueError, match="tenant"):
        store.add_seat(db, 99999, seat("unknown.test", 1))
    assert db.execute("SELECT COUNT(*) FROM seats").fetchone()[0] == 1


def test_links_and_capacity_survive_reopen_and_failed_writes(tmp_path):
    path = tmp_path / "reopen.db"
    db = store.open_db(path)
    first = store.upsert_tenant_domain(db, "first.test", "unused")
    second = store.upsert_tenant_domain(db, "second.test", "unused")
    store.upsert_organization(db, "shared", 2)
    store.link_domain(db, "shared", "first.test")
    store.link_domain(db, "shared", "second.test")
    store.add_seat(db, first, seat("first.test", 1))
    store.add_seat(db, second, seat("second.test", 1), status="suspended")
    with pytest.raises(ValueError, match="capacity"):
        store.add_seat(db, second, seat("second.test", 2))
    before = store.organization_capacity(db, "shared")
    db.close()
    db = store.open_db(path)
    assert store.organization_capacity(db, "shared") == before
    assert before["reserved"] == 2
    assert before["available"] == 0
    assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(ValueError, match="Unknown organization"):
        store.link_domain(db, "missing", "first.test")
    with pytest.raises(ValueError, match="Unknown domain"):
        store.link_domain(db, "shared", "missing.test")
    assert store.organization_capacity(db, "shared") == before
    db.close()


def test_legacy_inventory_reopens_without_inventing_organizations(tmp_path):
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(store.SCHEMA)
    legacy.execute("DROP TABLE IF EXISTS organization_domains")
    legacy.execute("DROP TABLE IF EXISTS organizations")
    legacy.execute(
        "INSERT INTO tenants(fqdn, admin_email, admin_password, created_at, updated_at) "
        "VALUES('old.test', 'admin@old.test', 'unchanged', 'then', 'then')"
    )
    legacy.commit()
    legacy.close()

    for _ in range(2):
        db = store.open_db(path)
        assert db.execute("SELECT COUNT(*) FROM organizations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM organization_domains").fetchone()[0] == 0
        tenant = db.execute("SELECT * FROM tenants").fetchone()
        assert tenant["fqdn"] == "old.test"
        assert tenant["admin_password"] == "unchanged"
        assert tenant["max_seats"] == 50
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        db.close()
