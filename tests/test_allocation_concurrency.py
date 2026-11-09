"""Allocation safety across independent SQLite connections (no live services)."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from directory_seats.generator import generate_domain_seats
from seat_allocator.allocator import allocate_round_robin
from seat_allocator.store import add_seat, open_db, upsert_tenant_domain


@pytest.mark.parametrize("seat_count,budget", [(2, 1), (1, 3)])
def test_concurrent_same_target_cannot_exceed_budget_or_reuse_seat(tmp_path, seat_count, budget):
    path = tmp_path / "allocation.db"
    seed = open_db(path)
    tenant = upsert_tenant_domain(seed, "race.example.test", "test-only", max_seats=seat_count)
    for seat in generate_domain_seats("race.example.test", count=seat_count):
        add_seat(seed, tenant, seat, status="provisioned")
    seed.close()

    start = threading.Barrier(2, timeout=5)
    first_selected = threading.Event()
    second_contending = threading.Event()
    first_finished = threading.Event()
    results = [None, None]
    errors = []

    class CoordinatedCursor(sqlite3.Cursor):
        def fetchone(self):
            row = super().fetchone()
            # Drain the statement before pausing, so the test itself never holds
            # a SQLite reader lock that would block the first writer's commit.
            super().fetchall()
            if self.connection.worker == 0:
                first_selected.set()
                assert second_contending.wait(5), "second allocator did not contend"
            else:
                second_contending.set()
                assert first_finished.wait(5), "first allocator did not finish"
            return row

    class CoordinatedConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if self.worker == 1 and sql.strip().upper() == "BEGIN IMMEDIATE":
                # Signal *before* acquiring the lock: a barrier inside the
                # serialized critical section would deadlock the fixed code.
                second_contending.set()
            if sql.startswith("SELECT s.id, s.fqdn"):
                return self.cursor(CoordinatedCursor).execute(sql, parameters)
            return super().execute(sql, parameters)

    def worker(index):
        connection = None
        try:
            connection = sqlite3.connect(path, factory=CoordinatedConnection, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.worker = index
            start.wait()
            if index == 1:
                assert first_selected.wait(5), "first allocator did not select"
            results[index] = allocate_round_robin(
                connection, "same-target", max_per_domain_day=budget,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            if connection is not None:
                connection.close()
            if index == 0:
                first_finished.set()

    threads = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads), "allocation threads deadlocked"
    assert not errors, errors
    assert sum(result.ok for result in results) == 1
    with sqlite3.connect(path) as check:
        allocations = check.execute("SELECT seat_id FROM allocations").fetchall()
        assert len(allocations) == 1
        assert check.execute("SELECT SUM(allocated_count) FROM seats").fetchone()[0] == 1
        assert check.execute("SELECT COUNT(*) FROM events WHERE kind='seat_allocated'").fetchone()[0] == 1


@pytest.mark.parametrize("first_domain_exhausted", [False, True])
def test_allocation_and_events_roll_back_together_on_failure(tmp_path, first_domain_exhausted):
    connection = open_db(tmp_path / "rollback.db")
    try:
        for fqdn in ("first.example.test", "second.example.test"):
            tenant = upsert_tenant_domain(connection, fqdn, "test-only", max_seats=2)
            for seat in generate_domain_seats(fqdn, count=2):
                add_seat(connection, tenant, seat, status="provisioned")
        if first_domain_exhausted:
            connection.execute("UPDATE seats SET status='suspended' WHERE fqdn='second.example.test'")
            connection.commit()
            allocate_round_robin(connection, "rollback-target", max_per_domain_day=1)
            connection.execute("UPDATE seats SET status='provisioned'")
            connection.commit()

        allocations_before = connection.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]
        events_before = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        counts_before = connection.execute("SELECT SUM(allocated_count) FROM seats").fetchone()[0]
        # Failure after an earlier seat_allocated or domain_budget_guard event
        # proves neither path can call the auto-committing store.log_event.
        connection.executescript("""
            CREATE TEMP TRIGGER reject_second_domain_event
            BEFORE INSERT ON events
            WHEN NEW.kind='seat_allocated' AND NEW.fqdn='second.example.test'
            BEGIN SELECT RAISE(ABORT, 'injected event failure'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="injected event failure"):
            allocate_round_robin(connection, "rollback-target", count=2, max_per_domain_day=1)
        assert not connection.in_transaction
        assert connection.execute("SELECT COUNT(*) FROM allocations").fetchone()[0] == allocations_before
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == events_before
        assert connection.execute("SELECT SUM(allocated_count) FROM seats").fetchone()[0] == counts_before
        connection.execute("DROP TRIGGER reject_second_domain_event")
        assert allocate_round_robin(connection, "recovery-target", max_per_domain_day=1).ok
    finally:
        connection.close()
