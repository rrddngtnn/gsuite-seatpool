"""SQLite database layer for tenants, DNS records, Cloud Identity seats, allocations, and catch-all mail."""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat

from catchall_mail.router import ParsedMail

TENANT_STATUSES = ("staged", "dns_pending", "dns_ready", "verified", "active", "suspended", "dead")
SEAT_STATUSES = ("ready", "provisioned", "suspended", "missing", "dead")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
  id                 INTEGER PRIMARY KEY,
  fqdn               TEXT UNIQUE NOT NULL,
  registrar_account  TEXT,
  admin_email        TEXT NOT NULL,
  admin_password     TEXT NOT NULL,
  verification_token TEXT,
  catchall_mode      TEXT,
  catchall_target    TEXT,
  max_seats          INTEGER NOT NULL DEFAULT 50,
  status             TEXT NOT NULL DEFAULT 'staged',
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL
);

-- Organization linkage is explicit: legacy inventory remains unlinked.
-- Capacity is operator-planned inventory, not a statement of Google licensing.
CREATE TABLE IF NOT EXISTS organizations (
  organization_key TEXT PRIMARY KEY NOT NULL,
  capacity INTEGER NOT NULL CHECK(capacity > 0)
);

CREATE TABLE IF NOT EXISTS organization_domains (
  tenant_id INTEGER PRIMARY KEY REFERENCES tenants(id),
  organization_key TEXT NOT NULL REFERENCES organizations(organization_key)
);
CREATE INDEX IF NOT EXISTS ix_organization_domains_key
  ON organization_domains(organization_key);

CREATE TABLE IF NOT EXISTS dns_records (
  id          INTEGER PRIMARY KEY,
  fqdn        TEXT NOT NULL,
  remote_id   INTEGER,
  record_type TEXT NOT NULL,
  name        TEXT NOT NULL,
  value       TEXT NOT NULL,
  ttl         INTEGER NOT NULL DEFAULT 300,
  priority    INTEGER,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seats (
  id              INTEGER PRIMARY KEY,
  tenant_id       INTEGER NOT NULL REFERENCES tenants(id),
  fqdn            TEXT NOT NULL,
  seat_index      INTEGER NOT NULL,
  email           TEXT UNIQUE NOT NULL,
  password        TEXT NOT NULL,
  first_name      TEXT NOT NULL,
  last_name       TEXT NOT NULL,
  org_unit        TEXT NOT NULL DEFAULT '/',
  status          TEXT NOT NULL DEFAULT 'ready',
  allocated_count INTEGER NOT NULL DEFAULT 0,
  last_used_at    TEXT,
  created_at      TEXT NOT NULL,
  UNIQUE(fqdn, seat_index)
);

CREATE TABLE IF NOT EXISTS allocations (
  id           INTEGER PRIMARY KEY,
  seat_id      INTEGER NOT NULL REFERENCES seats(id),
  fqdn         TEXT NOT NULL,
  email        TEXT NOT NULL,
  target       TEXT NOT NULL,
  allocated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox_messages (
  id          INTEGER PRIMARY KEY,
  recipient   TEXT NOT NULL,
  domain      TEXT NOT NULL,
  sender      TEXT NOT NULL,
  subject     TEXT NOT NULL,
  body_text   TEXT NOT NULL,
  otp_json    TEXT NOT NULL,
  urls_json   TEXT NOT NULL,
  received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY,
  ts        TEXT NOT NULL,
  fqdn      TEXT,
  kind      TEXT NOT NULL,
  detail    TEXT
);

CREATE INDEX IF NOT EXISTS ix_seats_fqdn_status ON seats(fqdn, status);
CREATE INDEX IF NOT EXISTS ix_alloc_dom_target_ts ON allocations(fqdn, target, allocated_at);
CREATE INDEX IF NOT EXISTS ix_inbox_rcpt ON inbox_messages(recipient);
CREATE INDEX IF NOT EXISTS ix_inbox_dom ON inbox_messages(domain);
"""


def utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path: str = "gsuite_seatpool.db") -> sqlite3.Connection:
    path = os.fspath(path)
    if path == ":memory:":
        db = sqlite3.connect(path)
    else:
        # Exclusive creation sets permissions before SQLite can write credentials.
        # O_NOFOLLOW also rejects dangling links; never chmod an existing file.
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        created = False
        try:
            fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = os.open(path, flags)
        try:
            if created:
                os.fchmod(fd, 0o600)
            original = os.fstat(fd)
            if not stat.S_ISREG(original.st_mode):
                raise ValueError("Database path must be a regular file")
            uri = Path(os.path.abspath(path)).as_uri() + "?mode=rw"
            db = sqlite3.connect(uri, uri=True)
            current = os.stat(path, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                db.close()
                raise ValueError("Database path changed while opening")
        finally:
            os.close(fd)
    try:
        db.executescript(SCHEMA)
        db.row_factory = sqlite3.Row
        return db
    except Exception:
        db.close()
        raise


def organization_capacity(db: sqlite3.Connection, organization_key: str) -> dict:
    """Return local planned capacity; this does not verify licensing or provisioning."""
    row = db.execute(
        "SELECT capacity FROM organizations WHERE organization_key=?", (organization_key,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Unknown organization: {organization_key}")
    domains = [row[0] for row in db.execute(
        "SELECT t.fqdn FROM tenants t JOIN organization_domains d ON d.tenant_id=t.id "
        "WHERE d.organization_key=? ORDER BY t.fqdn", (organization_key,)
    )]
    reserved = db.execute(
        "SELECT COUNT(*) FROM seats s JOIN organization_domains d ON d.tenant_id=s.tenant_id "
        "WHERE d.organization_key=? AND s.status!='dead'", (organization_key,)
    ).fetchone()[0]
    return {"organization_key": organization_key, "capacity": row[0],
            "reserved": reserved, "available": row[0] - reserved, "domains": domains}


def upsert_organization(db: sqlite3.Connection, organization_key: str, capacity: int) -> None:
    """Declare an operator-managed organization and its planned seat capacity."""
    if not isinstance(organization_key, str) or not organization_key.strip():
        raise ValueError("organization_key must be non-empty")
    if type(capacity) is not int or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    with db:
        db.execute(
            "INSERT INTO organizations(organization_key, capacity) VALUES(?, ?) "
            "ON CONFLICT(organization_key) DO UPDATE SET capacity=excluded.capacity",
            (organization_key, capacity),
        )
        if organization_capacity(db, organization_key)["available"] < 0:
            raise ValueError("Organization planned capacity exceeded")


def link_domain(db: sqlite3.Connection, organization_key: str, fqdn: str) -> None:
    """Explicitly associate an existing inventory domain with an organization."""
    organization_capacity(db, organization_key)
    tenant = db.execute("SELECT id FROM tenants WHERE fqdn=?", (fqdn.strip().lower(),)).fetchone()
    if tenant is None:
        raise ValueError(f"Unknown domain: {fqdn}")
    with db:
        db.execute(
            "INSERT INTO organization_domains(tenant_id, organization_key) VALUES(?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET organization_key=excluded.organization_key",
            (tenant[0], organization_key),
        )
        if organization_capacity(db, organization_key)["available"] < 0:
            raise ValueError("Organization planned capacity exceeded")


def log_event(db: sqlite3.Connection, fqdn: str | None, kind: str, detail: str = "") -> None:
    db.execute(
        "INSERT INTO events(ts, fqdn, kind, detail) VALUES(?, ?, ?, ?)",
        (utcnow(), fqdn, kind, str(detail)[:2000]),
    )
    db.commit()


def upsert_tenant_domain(
    db: sqlite3.Connection,
    fqdn: str,
    admin_password: str,
    registrar_account: str | None = None,
    max_seats: int = 50,
) -> int:
    if type(max_seats) is not int or max_seats < 1:
        raise ValueError("max_seats must be a positive integer")
    fqdn = fqdn.strip().lower()
    now = utcnow()
    db.execute(
        "INSERT INTO tenants(fqdn, registrar_account, admin_email, admin_password, max_seats, status, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, 'staged', ?, ?) "
        "ON CONFLICT(fqdn) DO UPDATE SET "
        "registrar_account=COALESCE(excluded.registrar_account, tenants.registrar_account), "
        "max_seats=excluded.max_seats, updated_at=excluded.updated_at",
        (fqdn, registrar_account, f"admin@{fqdn}", admin_password, max_seats, now, now),
    )
    db.commit()
    row = db.execute("SELECT id FROM tenants WHERE fqdn=?", (fqdn,)).fetchone()
    log_event(db, fqdn, "tenant_upserted", f"max_seats={max_seats}")
    return int(row["id"])


def set_tenant_status(db: sqlite3.Connection, fqdn: str, status: str, note: str = "") -> None:
    if status not in TENANT_STATUSES:
        raise ValueError(f"Unknown tenant status: {status}")
    db.execute(
        "UPDATE tenants SET status=?, updated_at=? WHERE fqdn=?",
        (status, utcnow(), fqdn),
    )
    db.commit()
    log_event(db, fqdn, "tenant_status", f"{status} {note}".strip())


def add_dns_record(
    db: sqlite3.Connection,
    fqdn: str,
    record_type: str,
    name: str,
    value: str,
    ttl: int = 300,
    priority: int | None = None,
    remote_id: int | None = None,
) -> int:
    cur = db.execute(
        "INSERT INTO dns_records(fqdn, remote_id, record_type, name, value, ttl, priority, created_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (fqdn, remote_id, record_type.upper(), name, value, int(ttl), priority, utcnow()),
    )
    db.commit()
    return int(cur.lastrowid)


def add_seat(db: sqlite3.Connection, tenant_id: int, seat: dict, status: str = "ready") -> int:
    if status not in SEAT_STATUSES:
        raise ValueError(f"Unknown seat status: {status}")
    with db:
        tenant = db.execute("SELECT fqdn FROM tenants WHERE id=?", (tenant_id,)).fetchone()
        if tenant is None or tenant[0] != seat["fqdn"]:
            raise ValueError("Seat domain must match an existing tenant")
        cur = db.execute(
            "INSERT OR IGNORE INTO seats"
            "(tenant_id, fqdn, seat_index, email, password, first_name, last_name, org_unit, status, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                seat["fqdn"],
                seat["seat_index"],
                seat["email"],
                seat["password"],
                seat["first_name"],
                seat["last_name"],
                seat.get("org_unit", "/"),
                status,
                utcnow(),
            ),
        )
        linked = db.execute(
            "SELECT organization_key FROM organization_domains WHERE tenant_id=?", (tenant_id,)
        ).fetchone()
        if linked and organization_capacity(db, linked[0])["available"] < 0:
            raise ValueError("Organization planned capacity exceeded")
    row = db.execute("SELECT id FROM seats WHERE email=?", (seat["email"],)).fetchone()
    return int(row["id"])


def store_inbox_message(db: sqlite3.Connection, parsed: ParsedMail) -> int:
    domain = parsed.recipient.split("@", 1)[1] if "@" in parsed.recipient else ""
    cur = db.execute(
        "INSERT INTO inbox_messages(recipient, domain, sender, subject, body_text, otp_json, urls_json, received_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
        (
            parsed.recipient,
            domain,
            parsed.sender,
            parsed.subject,
            parsed.body_text,
            json.dumps(parsed.otp_codes),
            json.dumps(parsed.urls),
            utcnow(),
        ),
    )
    db.commit()
    return int(cur.lastrowid)


def list_inbox_messages(
    db: sqlite3.Connection,
    recipient: str | None = None,
    domain: str | None = None,
) -> list[ParsedMail]:
    clauses = []
    params = []
    if recipient:
        clauses.append("recipient = ?")
        params.append(recipient.strip().lower())
    if domain:
        clauses.append("domain = ?")
        params.append(domain.strip().lower())
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = db.execute(
        f"SELECT recipient, sender, subject, body_text, otp_json, urls_json "
        f"FROM inbox_messages {where} ORDER BY id DESC",
        tuple(params),
    ).fetchall()
    return [
        ParsedMail(
            recipient=r["recipient"],
            sender=r["sender"],
            subject=r["subject"],
            body_text=r["body_text"],
            otp_codes=json.loads(r["otp_json"]),
            urls=json.loads(r["urls_json"]),
        )
        for r in rows
    ]


def counts(db: sqlite3.Connection) -> dict[str, int]:
    q = lambda sql: int(db.execute(sql).fetchone()[0])
    return {
        "tenants": q("SELECT COUNT(*) FROM tenants"),
        "dns_records": q("SELECT COUNT(*) FROM dns_records"),
        "seats": q("SELECT COUNT(*) FROM seats"),
        "seats_ready": q("SELECT COUNT(*) FROM seats WHERE status='provisioned'"),
        "allocations": q("SELECT COUNT(*) FROM allocations"),
        "inbox_messages": q("SELECT COUNT(*) FROM inbox_messages"),
    }
