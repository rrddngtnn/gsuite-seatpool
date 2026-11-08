"""CLI entrypoint for gsuite-seatpool."""
from __future__ import annotations

import argparse
import json
import os
import sys

from catchall_mail.router import CatchallMailbox
from directory_seats.client import DirectoryClient
from directory_seats.generator import (
    CLOUD_IDENTITY_FREE_CAP,
    export_admin_csv,
    generate_domain_seats,
    seat_password,
)
from dns_zone.client import CATCHALL_PRESETS, DnsZoneClient
from seat_allocator.allocator import allocate_round_robin
from seat_allocator.store import (
    add_dns_record,
    add_seat,
    counts,
    open_db,
    set_tenant_status,
    upsert_tenant_domain,
)


def cmd_doctor(args) -> int:
    try:
        import curl_cffi  # noqa: F401
        cf_ok = True
    except ImportError:
        cf_ok = False
    try:
        import fastapi  # noqa: F401
        fa_ok = True
    except ImportError:
        fa_ok = False
    print(json.dumps({
        "curl_cffi": cf_ok,
        "fastapi_mock": fa_ok,
        "cloud_identity_free_cap": CLOUD_IDENTITY_FREE_CAP,
        "catchall_presets": list(CATCHALL_PRESETS),
    }, indent=2))
    return 0


def cmd_mock(args) -> int:
    import uvicorn
    from .mock_server import app
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_init_tenant(args) -> int:
    db = open_db(args.db)
    summary = []
    for fqdn in args.fqdn:
        admin_pw = seat_password(fqdn, 0)
        tid = upsert_tenant_domain(db, fqdn, admin_pw, max_seats=args.seats)
        seats = generate_domain_seats(fqdn, count=args.seats, prefix=args.prefix)
        for s in seats:
            add_seat(db, tid, s)
        summary.append({
            "fqdn": fqdn,
            "admin_email": f"admin@{fqdn}",
            "seats": len(seats),
        })
    print(json.dumps({"initialized": summary}, indent=2))
    return 0


def cmd_export_csv(args) -> int:
    db = open_db(args.db)
    os.makedirs(args.out_dir, exist_ok=True)
    rows = db.execute("SELECT id, fqdn FROM tenants ORDER BY id").fetchall()
    written = []
    for t in rows:
        if args.fqdn and t["fqdn"] != args.fqdn:
            continue
        seat_rows = [
            dict(r, change_password_at_next_login=False)
            for r in db.execute(
                "SELECT fqdn, seat_index, email, password, first_name, last_name, org_unit "
                "FROM seats WHERE tenant_id=? ORDER BY seat_index",
                (t["id"],),
            ).fetchall()
        ]
        out_path = os.path.join(args.out_dir, f"{t['fqdn']}.csv")
        export_admin_csv(seat_rows, out_path)
        written.append(out_path)
    print(json.dumps({"csv_files": written}, indent=2))
    return 0


def cmd_dns_verify(args) -> int:
    db = open_db(args.db)
    client = DnsZoneClient(args.base_url)
    res = client.ensure_google_verification(args.fqdn, args.token)
    rec = res["record"]
    if res["action"] == "created":
        add_dns_record(
            db,
            args.fqdn,
            rec["type"],
            rec["name"],
            rec["value"],
            ttl=rec.get("ttl", 300),
            remote_id=rec.get("id"),
        )
    set_tenant_status(db, args.fqdn, "verified", args.token[:32])
    print(json.dumps(res, indent=2))
    return 0


def cmd_dns_catchall(args) -> int:
    db = open_db(args.db)
    client = DnsZoneClient(args.base_url)
    changes = client.ensure_catchall(args.fqdn, args.mode, args.target)
    for item in changes:
        if item["action"] == "created":
            rec = item["record"]
            add_dns_record(
                db,
                args.fqdn,
                rec["type"],
                rec["name"],
                rec["value"],
                ttl=rec.get("ttl", 300),
                priority=rec.get("priority"),
                remote_id=rec.get("id"),
            )
    set_tenant_status(db, args.fqdn, "dns_ready", f"mode={args.mode}")
    print(json.dumps(changes, indent=2))
    return 0


def cmd_sync_directory(args) -> int:
    db = open_db(args.db)
    client = DirectoryClient(base_url=args.base_url, bearer_token=args.token)
    seats = db.execute(
        "SELECT id, fqdn, seat_index, email, password, first_name, last_name, org_unit "
        "FROM seats WHERE fqdn=? AND status='ready' ORDER BY seat_index LIMIT ?",
        (args.fqdn, args.limit),
    ).fetchall()
    provisioned = []
    for s in seats:
        user_obj = client.insert_user(dict(s))
        db.execute("UPDATE seats SET status='provisioned' WHERE id=?", (s["id"],))
        provisioned.append(user_obj["primaryEmail"])
    db.commit()
    set_tenant_status(db, args.fqdn, "active", f"provisioned={len(provisioned)}")
    print(json.dumps({"fqdn": args.fqdn, "provisioned": len(provisioned)}, indent=2))
    return 0


def cmd_allocate(args) -> int:
    db = open_db(args.db)
    res = allocate_round_robin(
        db,
        target=args.target,
        count=args.count,
        max_per_domain_day=args.max_per_domain_day,
    )
    print(json.dumps({
        "target": res.target,
        "requested": res.requested,
        "allocated_count": len(res.allocated),
        "allocated": res.allocated,
        "skipped_domains": res.skipped_domains,
    }, indent=2))
    return 0 if res.allocated else 2


def cmd_inbox_ingest(args) -> int:
    db = open_db(args.db)
    mb = CatchallMailbox(db=db)
    raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    parsed = mb.ingest_raw(raw, envelope_rcpt=args.recipient)
    print(json.dumps({
        "recipient": parsed.recipient,
        "sender": parsed.sender,
        "subject": parsed.subject,
        "otp_codes": parsed.otp_codes,
        "urls": parsed.urls,
    }, indent=2))
    return 0


def cmd_status(args) -> int:
    db = open_db(args.db)
    print(json.dumps(counts(db), indent=2))
    for r in db.execute(
        "SELECT t.fqdn, t.status, t.max_seats, "
        "(SELECT COUNT(*) FROM seats s WHERE s.tenant_id=t.id) AS total_seats, "
        "(SELECT COUNT(*) FROM allocations a WHERE a.fqdn=t.fqdn) AS total_allocs "
        "FROM tenants t ORDER BY t.id"
    ):
        print(f"  {r['fqdn']:28} status={r['status']:10} seats={r['total_seats']:2}/{r['max_seats']:2} allocs={r['total_allocs']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsuite-seatpool")
    p.add_argument("--db", default="gsuite_seatpool.db")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)

    mk = sub.add_parser("mock")
    mk.add_argument("--host", default="127.0.0.1")
    mk.add_argument("--port", type=int, default=8788)
    mk.set_defaults(fn=cmd_mock)

    it = sub.add_parser("init-tenant")
    it.add_argument("--fqdn", action="append", required=True)
    it.add_argument("--seats", type=int, default=CLOUD_IDENTITY_FREE_CAP)
    it.add_argument("--prefix", default="unit")
    it.set_defaults(fn=cmd_init_tenant)

    ec = sub.add_parser("export-csv")
    ec.add_argument("--out-dir", default="./csv_out")
    ec.add_argument("--fqdn", default=None)
    ec.set_defaults(fn=cmd_export_csv)

    dv = sub.add_parser("dns-verify")
    dv.add_argument("--base-url", default="http://127.0.0.1:8788")
    dv.add_argument("--fqdn", required=True)
    dv.add_argument("--token", required=True)
    dv.set_defaults(fn=cmd_dns_verify)

    dc = sub.add_parser("dns-catchall")
    dc.add_argument("--base-url", default="http://127.0.0.1:8788")
    dc.add_argument("--fqdn", required=True)
    dc.add_argument("--mode", choices=list(CATCHALL_PRESETS), default="forwardemail")
    dc.add_argument("--target", required=True)
    dc.set_defaults(fn=cmd_dns_catchall)

    sd = sub.add_parser("sync-directory")
    sd.add_argument("--base-url", default="http://127.0.0.1:8788")
    sd.add_argument("--token", default="mock-token")
    sd.add_argument("--fqdn", required=True)
    sd.add_argument("--limit", type=int, default=CLOUD_IDENTITY_FREE_CAP)
    sd.set_defaults(fn=cmd_sync_directory)

    al = sub.add_parser("allocate")
    al.add_argument("--target", required=True)
    al.add_argument("--count", type=int, default=1)
    al.add_argument("--max-per-domain-day", type=int, default=3)
    al.set_defaults(fn=cmd_allocate)

    ii = sub.add_parser("inbox-ingest")
    ii.add_argument("--file", default="-")
    ii.add_argument("--recipient", default=None)
    ii.set_defaults(fn=cmd_inbox_ingest)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
