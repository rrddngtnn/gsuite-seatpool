from directory_seats.generator import generate_domain_seats
from seat_allocator.store import open_db, upsert_tenant_domain, add_seat, counts, set_tenant_status
from seat_allocator.allocator import allocate_round_robin


def test_staged_inventory_is_not_allocatable():
    db = open_db(':memory:')
    tid = upsert_tenant_domain(db, 'example.test', 'unused')
    add_seat(db, tid, generate_domain_seats('example.test', 1)[0])
    assert allocate_round_robin(db, 'internal', 1).allocated == []
    assert counts(db)['seats_ready'] == 0


def test_dns_publication_is_pending_not_google_verified():
    db = open_db(':memory:')
    upsert_tenant_domain(db, 'example.test', 'unused')
    set_tenant_status(db, 'example.test', 'dns_pending')
    assert db.execute('SELECT status FROM tenants').fetchone()[0] == 'dns_pending'
