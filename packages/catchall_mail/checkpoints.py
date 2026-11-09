"""Atomic inbox insertion and persistent mailbox/UID identity checkpoints."""
from __future__ import annotations

import datetime
import json


def ingest_imap_messages(db, messages) -> int:
    """Commit a complete fetched batch or roll it back, including its checkpoints."""
    db.execute("""CREATE TABLE IF NOT EXISTS imap_checkpoints (
        account_id TEXT NOT NULL,
        uidvalidity TEXT NOT NULL,
        uid TEXT NOT NULL,
        PRIMARY KEY (account_id, uidvalidity, uid)
    )""")
    ingested = 0
    with db:
        for item in messages:
            inserted = db.execute(
                "INSERT INTO imap_checkpoints(account_id, uidvalidity, uid) VALUES (?, ?, ?) "
                "ON CONFLICT(account_id, uidvalidity, uid) DO NOTHING",
                (item.account_id, item.uidvalidity, item.uid),
            )
            if not inserted.rowcount:
                continue
            message = item.message
            domain = message.recipient.split("@", 1)[1] if "@" in message.recipient else ""
            db.execute(
                "INSERT INTO inbox_messages(recipient, domain, sender, subject, body_text, "
                "otp_json, urls_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message.recipient, domain, message.sender, message.subject, message.body_text,
                 json.dumps(message.otp_codes), json.dumps(message.urls),
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            ingested += 1
    return ingested


def ingest_rest_messages(db, messages) -> int:
    """Atomically ingest REST IDs in a namespace separate from IMAP UIDs."""
    db.execute("""CREATE TABLE IF NOT EXISTS rest_checkpoints (
        account_id TEXT NOT NULL,
        id TEXT NOT NULL,
        PRIMARY KEY (account_id, id)
    )""")
    ingested = 0
    with db:
        for item in messages:
            inserted = db.execute(
                "INSERT INTO rest_checkpoints(account_id, id) VALUES (?, ?) "
                "ON CONFLICT(account_id, id) DO NOTHING",
                (item.account_id, item.id),
            )
            if not inserted.rowcount:
                continue
            message = item.message
            domain = message.recipient.split("@", 1)[1] if "@" in message.recipient else ""
            db.execute(
                "INSERT INTO inbox_messages(recipient, domain, sender, subject, body_text, "
                "otp_json, urls_json, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message.recipient, domain, message.sender, message.subject, message.body_text,
                 json.dumps(message.otp_codes), json.dumps(message.urls),
                 datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
            ingested += 1
    return ingested
