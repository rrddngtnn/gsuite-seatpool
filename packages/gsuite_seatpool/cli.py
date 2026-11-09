"""CLI entrypoint for gsuite-seatpool."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .config import load_config
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
        "default_seat_count": CLOUD_IDENTITY_FREE_CAP,
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


def _dns_client(args):
    from dns_zone import client as dns
    factory = getattr(dns, "create_dns_provider", None)
    if factory is not None:
        settings = dict(args.settings["dns"])
        if "username_env" in settings and "email_env" not in settings:
            settings["email_env"] = settings["username_env"]
        return factory(settings)
    if args.settings["dns"]["provider"] != "registrar":
        raise ValueError("DNS provider factory is unavailable")
    return DnsZoneClient(args.base_url)


def cmd_dns_verify(args) -> int:
    db = open_db(args.db)
    client = _dns_client(args)
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
    set_tenant_status(db, args.fqdn, "dns_pending", "verification TXT submitted")
    print(json.dumps(res, indent=2))
    return 0


def cmd_dns_catchall(args) -> int:
    db = open_db(args.db)
    client = _dns_client(args)
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
    status = "dns_pending" if any(item["action"] == "planned" for item in changes) else "dns_ready"
    set_tenant_status(db, args.fqdn, status, f"mode={args.mode}")
    print(json.dumps(changes, indent=2))
    return 0


def _directory_client(args):
    from directory_seats.auth import build_directory_auth
    settings = args.settings["directory"]
    auth = build_directory_auth(
        bearer_token=settings.get("token") or None,
        authorized_user_file=settings.get("authorized_user_file"),
        service_account_file=settings.get("service_account_file"),
        delegated_admin=settings.get("delegated_admin"),
    )
    return DirectoryClient(base_url=args.base_url, **auth)


def cmd_sync_directory(args) -> int:
    db = open_db(args.db)
    client = _directory_client(args)
    seats = db.execute(
        "SELECT id, fqdn, seat_index, email, password, first_name, last_name, org_unit "
        "FROM seats WHERE fqdn=? AND status='ready' ORDER BY seat_index LIMIT ?",
        (args.fqdn, args.limit),
    ).fetchall()
    existing = {user["primaryEmail"].casefold(): user for user in client.list_users(args.fqdn)}
    provisioned = []
    for s in seats:
        key = s["email"].casefold()
        if key not in existing:
            existing[key] = client.insert_user(dict(s))
        user = existing[key]
        status = "suspended" if user.get("suspended") or user.get("archived") else "provisioned"
        db.execute("UPDATE seats SET status=? WHERE id=?", (status, s["id"]))
        db.commit()
        if status == "provisioned":
            provisioned.append(s["email"])
    if provisioned:
        set_tenant_status(db, args.fqdn, "active", f"provisioned={len(provisioned)}")
    print(json.dumps({"fqdn": args.fqdn, "provisioned": len(provisioned)}, indent=2))
    return 0


def cmd_plan(args) -> int:
    import sqlite3
    from pathlib import Path
    # Do not initialize a database or run schema DDL during a dry plan.
    with sqlite3.connect(Path(args.db).resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        pending = [dict(row) for row in db.execute(
            "SELECT email,status FROM seats WHERE fqdn=? AND status='ready' ORDER BY seat_index",
            (args.fqdn,),
        )]
    print(json.dumps({"fqdn": args.fqdn, "pending": pending}, indent=2))
    return 0


def cmd_reconcile(args) -> int:
    db = open_db(args.db)
    client = _directory_client(args)
    from directory_seats.reconcile import reconcile_inventory
    report = reconcile_inventory(db, client, args.fqdn)
    print(json.dumps(report, indent=2))
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
        "allocated": [{k: v for k, v in seat.items() if k != "password"} for seat in res.allocated],
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


def cmd_inbox_pull(args) -> int:
    from catchall_mail.sources import ImapSource
    from catchall_mail.rest_source import RestSource
    from catchall_mail.checkpoints import ingest_imap_messages, ingest_rest_messages

    settings = args.settings.get("mail", {})
    provider = settings.get("provider")
    if provider == "rest":
        source, ingest = RestSource(settings), ingest_rest_messages
    elif provider == "imap":
        for field in ("host", "username_env", "password_env"):
            if not settings.get(field):
                raise ValueError(f"inbox-pull requires mail.{field}")
        source, ingest = ImapSource(settings), ingest_imap_messages
    else:
        raise ValueError("inbox-pull requires mail.provider imap or rest")
    messages = source.pull_with_metadata(limit=args.limit)
    with open_db(args.db) as db:
        ingested = ingest(db, messages)
    print(json.dumps({"ingested": ingested}))
    return 0


def cmd_organization(args) -> int:
    from seat_allocator.store import upsert_organization, link_domain, organization_capacity

    if not args.key.strip():
        raise ValueError("organization key must be non-empty")
    if args.cmd == "org-init" and args.capacity < 1:
        raise ValueError("organization capacity must be a positive integer")
    with open_db(args.db) as db:
        if args.cmd == "org-init":
            upsert_organization(db, args.key, args.capacity)
        elif args.cmd == "org-link":
            link_domain(db, args.key, args.fqdn)
        result = organization_capacity(db, args.key)
    print(json.dumps(result, indent=2))
    return 0


def cmd_status(args) -> int:
    db = open_db(args.db)
    result = counts(db)
    result["seats_provisioned"] = db.execute("SELECT COUNT(*) FROM seats WHERE status='provisioned'").fetchone()[0]
    result["tenants_detail"] = [dict(r) for r in db.execute(
        "SELECT t.fqdn, t.status, t.max_seats, "
        "(SELECT COUNT(*) FROM seats s WHERE s.tenant_id=t.id) AS total_seats, "
        "(SELECT COUNT(*) FROM allocations a WHERE a.fqdn=t.fqdn) AS total_allocs "
        "FROM tenants t ORDER BY t.id"
    )]
    print(json.dumps(result, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gsuite-seatpool")
    p.add_argument("--config")
    p.add_argument("--db", default=None)
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
    dv.add_argument("--base-url", default=None)
    dv.add_argument("--provider", default=None)
    dv.add_argument("--fqdn", required=True)
    dv.add_argument("--token", required=True)
    dv.set_defaults(fn=cmd_dns_verify)

    dc = sub.add_parser("dns-catchall")
    dc.add_argument("--base-url", default=None)
    dc.add_argument("--provider", default=None)
    dc.add_argument("--fqdn", required=True)
    dc.add_argument("--mode", choices=list(CATCHALL_PRESETS), default="forwardemail")
    dc.add_argument("--target", default=None)
    dc.set_defaults(fn=cmd_dns_catchall)

    sd = sub.add_parser("sync-directory")
    sd.add_argument("--base-url", default=None)
    sd.add_argument("--token", default=None)
    sd.add_argument("--fqdn", required=True)
    sd.add_argument("--limit", type=int, default=CLOUD_IDENTITY_FREE_CAP)
    sd.set_defaults(fn=cmd_sync_directory)

    plan = sub.add_parser("plan")
    plan.add_argument("--fqdn", required=True)
    plan.set_defaults(fn=cmd_plan)
    reconcile = sub.add_parser("reconcile")
    reconcile.add_argument("--fqdn", required=True)
    reconcile.add_argument("--base-url", default=None)
    reconcile.add_argument("--token", default=None)
    reconcile.set_defaults(fn=cmd_reconcile)

    al = sub.add_parser("allocate")
    al.add_argument("--target", required=True)
    al.add_argument("--count", type=int, default=1)
    al.add_argument("--max-per-domain-day", type=int, default=3)
    al.set_defaults(fn=cmd_allocate)

    ii = sub.add_parser("inbox-ingest")
    ii.add_argument("--file", default="-")
    ii.add_argument("--recipient", default=None)
    ii.set_defaults(fn=cmd_inbox_ingest)

    pull = sub.add_parser("inbox-pull")
    pull.add_argument("--limit", type=int, default=50)
    pull.set_defaults(fn=cmd_inbox_pull)

    for name in ("org-init", "org-link", "org-status"):
        organization = sub.add_parser(name)
        organization.add_argument("--key", required=True)
        if name == "org-init":
            organization.add_argument("--capacity", type=int, required=True)
        elif name == "org-link":
            organization.add_argument("--fqdn", required=True)
        organization.set_defaults(fn=cmd_organization)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    overrides = {}
    if args.db is not None:
        overrides["db"] = args.db
    section = "dns" if args.cmd.startswith("dns-") else "directory"
    for key in ("base_url", "provider"):
        value = getattr(args, key, None)
        if value is not None:
            overrides.setdefault(section, {})[key] = value
    if args.cmd in ("sync-directory", "reconcile") and args.token is not None:
        overrides.setdefault("directory", {})["token"] = args.token
    try:
        args.settings = load_config(args.config, overrides=overrides)
    except (ValueError, OSError, ImportError) as exc:
        parser.error(str(exc))
    args.db = args.settings["db"]
    if hasattr(args, "base_url"):
        args.base_url = args.settings[section]["base_url"]
    if args.cmd in ("sync-directory", "reconcile"):
        args.token = args.settings["directory"]["token"]
    if args.cmd == "dns-catchall":
        args.target = args.target or args.settings.get("catchall_target")
        if not args.target:
            parser.error("dns-catchall requires --target or CATCHALL_TARGET")
    try:
        return args.fn(args)
    except (ValueError, OSError, ImportError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
