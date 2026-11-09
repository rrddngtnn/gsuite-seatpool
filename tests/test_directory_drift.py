from seat_allocator.store import open_db, upsert_tenant_domain, add_seat
from directory_seats.generator import make_seat


def test_remote_suspended_and_missing_stop_allocation():
    from directory_seats.reconcile import reconcile_inventory
    db = open_db(':memory:')
    tid = upsert_tenant_domain(db, 'example.test', 'unused')
    for index in (1, 2, 3):
        add_seat(db, tid, make_seat('example.test', index), status='provisioned')
    class Directory:
        def list_users(self, domain):
            return [{'primaryEmail': 'unit01@example.test', 'suspended': True},
                    {'primaryEmail': 'unit03@example.test', 'suspended': False}]
    report = reconcile_inventory(db, Directory(), 'example.test')
    assert report == {'fqdn': 'example.test', 'matched': 2, 'suspended': 1, 'missing': 1}
    rows = db.execute('SELECT status FROM seats ORDER BY seat_index').fetchall()
    assert [r[0] for r in rows] == ['suspended', 'missing', 'provisioned']


def test_failed_remote_read_does_not_change_inventory():
    import pytest
    from directory_seats.reconcile import reconcile_inventory
    db = open_db(':memory:')
    tid = upsert_tenant_domain(db, 'example.test', 'unused')
    add_seat(db, tid, make_seat('example.test', 1), status='provisioned')
    class Directory:
        def list_users(self, domain):
            raise RuntimeError('incomplete remote listing')
    with pytest.raises(RuntimeError):
        reconcile_inventory(db, Directory(), 'example.test')
    assert db.execute('SELECT status FROM seats').fetchone()[0] == 'provisioned'
