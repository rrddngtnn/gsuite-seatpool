"""Read remote directory state and reconcile local eligibility without remote writes."""
from __future__ import annotations


def reconcile_inventory(db, client, fqdn: str) -> dict:
    users = client.list_users(fqdn)
    remote = {user['primaryEmail'].casefold(): user for user in users}
    report = {'fqdn': fqdn, 'matched': 0, 'suspended': 0, 'missing': 0}
    seats = db.execute(
        "SELECT id,email,status FROM seats WHERE fqdn=? AND status!='dead'", (fqdn,)
    ).fetchall()
    with db:
        for seat in seats:
            user = remote.get(seat['email'].casefold())
            if user is None:
                if seat['status'] in ('provisioned', 'suspended', 'missing'):
                    db.execute("UPDATE seats SET status='missing' WHERE id=?", (seat['id'],))
                    report['missing'] += 1
                continue
            report['matched'] += 1
            status = 'suspended' if user.get('suspended') or user.get('archived') else 'provisioned'
            report['suspended'] += status == 'suspended'
            db.execute('UPDATE seats SET status=? WHERE id=?', (status, seat['id']))
    return report
