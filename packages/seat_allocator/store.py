"""SQLite database layer for tenants, DNS records, Cloud Identity seats, allocations, and catch-all mail."""
from __future__ import annotations

import datetime
import json
import sqlite3

from catchall_mail.router import ParsedMail

TENANT_STATUSES = ("staged", "dns_ready", "verified", "active", "suspended", "dead")
SEAT_STATUSES = ("ready", "provisioned", "suspended", "dead")

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
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.row_factory = sqlite3.Row
    return db


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
    if max_seats < 1 or max_seats > 50:
        raise ValueError("max_seats must be between 1 and 50 (Cloud Identity Free cap)")
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
    db.commit()
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
        "seats_ready": q("SELECT COUNT(*) FROM seats WHERE status IN ('ready','provisioned')"),
        "allocations": q("SELECT COUNT(*) FROM allocations"),
        "inbox_messages": q("SELECT COUNT(*) FROM inbox_messages"),
    }
