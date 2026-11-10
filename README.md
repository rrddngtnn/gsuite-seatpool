# gsuite-seatpool

Configurable DNS provisioning, Google Directory user management, MIME ingestion, and local inventory allocation. Directory identities and mail delivery are separate: this tool does not provide Gmail mailboxes or create Google organizations.

## Install

```bash
python3 -m pip install -e '.[dev,google]'
cp seatpool.example.toml seatpool.toml
```

Python 3.10+ is supported. The full 331-test suite has been exercised locally on Python 3.10, 3.11, and 3.12 in separate environments; CI covers those versions. Python 3.10 uses the conditional `tomli` dependency. Google credential support is an optional `google` extra.

## Configuration

Resolution order: explicit CLI flags > environment > TOML > defaults. Environment variables must be exported; `.env` is not automatically loaded. See [seatpool.example.toml](seatpool.example.toml).

```bash
export SEATPOOL_DB=./inventory.sqlite
export GOOGLE_DIRECTORY_TOKEN='<your-short-lived-token>'
gsuite-seatpool --config seatpool.toml doctor
```

Environment settings include `SEATPOOL_DB`, `DNS_PROVIDER`, `REGISTRAR_BASE_URL`, `DIRECTORY_BASE_URL`, `GOOGLE_DIRECTORY_TOKEN`, `DIRECTORY_TOKEN`, `CATCHALL_TARGET`, `GOOGLE_DIRECTORY_CREDENTIALS_FILE`, `GOOGLE_DIRECTORY_SERVICE_ACCOUNT_FILE`, and `GOOGLE_DIRECTORY_DELEGATED_ADMIN`.

DNS providers:

- `manual`: emits planned records without claiming remote changes.
- `registrar`: implements the specific REST contract in [architecture](docs/architecture.md), with optional `email_env` and `password_env` authentication. Changing its URL does not translate arbitrary APIs.
- `cloudflare`: Cloudflare v4 DNS records with `zone_id` and `token_env`. Nameserver delegation is outside this adapter.

The Python factory and CLI support installed third-party entry points in `gsuite_seatpool.dns`. Unknown providers fail when selected for a DNS operation.

Google authentication supports a bearer token, an authorized-user credential file with refresh, or a service-account file with delegated administrator. API permissions and domain-wide delegation must already be configured. Never commit credential files.

## Inventory workflow

```bash
# Local inventory only; does not create Google users or organizations.
gsuite-seatpool --config seatpool.toml init-tenant --fqdn team.example.test --seats 5

# Local read-only plan; no remote calls.
gsuite-seatpool --config seatpool.toml plan --fqdn team.example.test

# Read Google Directory and update matching local records.
gsuite-seatpool --config seatpool.toml reconcile --fqdn team.example.test

# Apply pending creations and commit each successful local result.
gsuite-seatpool --config seatpool.toml sync-directory --fqdn team.example.test --limit 5

gsuite-seatpool --config seatpool.toml status
```

Sync lists remote users before creating missing primary addresses. It does not verify that an existing user's password matches local inventory. Reconcile never writes to Google. It marks remote suspended/archived users locally suspended and previously provisioned users absent from a complete listing as missing; neither is allocatable. It does not delete or recreate users.

Only `provisioned` seats are allocatable. Normal allocation output omits passwords. CSV export explicitly writes credentials; protect it and the plaintext SQLite inventory.

```bash
gsuite-seatpool --config seatpool.toml export-csv --out-dir ./private/csv
gsuite-seatpool --config seatpool.toml allocate --target internal-project --count 2
```

## DNS and mail

```bash
gsuite-seatpool --config seatpool.toml dns-verify --fqdn team.example.test --token google-site-verification=example-token
gsuite-seatpool --config seatpool.toml dns-catchall --fqdn team.example.test --mode custom_mx --target mx.example.test
gsuite-seatpool --config seatpool.toml inbox-ingest --file message.eml
```

TXT submission is `dns_pending`, not proof of Google verification. Catch-all presets generate DNS records only: forwarding rules, provider eligibility, and mail delivery must be checked separately. MIME files/stdin and read-only IMAP SSL ingestion are supported. An explicit REST MIME connector is also available (contract below); it is not an automatic translator for arbitrary mailbox APIs. IMAP fetches a bounded latest batch using UID and BODY.PEEK. Persistent checkpoints identify account/mailbox, UIDVALIDITY and UID; inbox rows and checkpoints commit atomically so repeat pulls do not duplicate messages. It still fetches only the latest bounded batch, not an unbounded historical backfill.

## Shared organization inventory

```bash
gsuite-seatpool --config seatpool.toml org-init --key company --capacity 50
gsuite-seatpool --config seatpool.toml org-link --key company --fqdn team.example.test
gsuite-seatpool --config seatpool.toml org-status --key company
```

Capacity is operator-declared planned inventory, not a license request. All linked non-dead seats reserve capacity across domains; over-capacity mutations roll back.

## IMAP ingestion

Add this table to your private configuration, export the referenced credentials, then run `gsuite-seatpool --config seatpool.toml inbox-pull --limit 50`:

```toml
[mail]
provider = "imap"
host = "imap.example.test"
port = 993
mailbox = "INBOX"
username_env = "MAIL_USERNAME"
password_env = "MAIL_PASSWORD"
```

IMAP opens the mailbox read-only. Command output contains an ingestion count rather than message contents or credentials.

## REST MIME ingestion

Use a private configuration with an endpoint implementing this explicit contract:

```toml
[mail]
provider = "rest"
url = "https://mail.example.test/messages"
bearer_env = "MAIL_API_TOKEN"
max_pages = 10
```

Run `inbox-pull --limit 50` with your configuration. The endpoint receives GET requests with a remaining `limit` and returns:

```json
{"messages": [{"id": "stable-opaque-id", "raw": "To: user@example.test\r\n\r\nMessage body"}], "next": null}
```

`next` can be a relative or same-origin HTTPS pagination link. Redirects and cross-origin pagination are rejected. Persistent `(account_id, id)` checkpoints and inbox records commit atomically. Account identity is derived from the exact endpoint URL: keep it stable and do not embed credentials in it. Token rotation through the same environment reference does not duplicate messages. Limits and page bounds prevent unbounded traversal; this contract does not match every mailbox vendor without an external adapter.

## TMail v1 ingestion

This optional HTTP adapter implements the mailbox-scoped v1 contract; it has no
TMail source-tree, import, or deployment dependency. It does not provision mailboxes.
Use a mailbox-scoped bearer token, not an administrator token:

```toml
[mail]
provider = "tmail_v1"
url = "https://mail.example.test"
mailbox = "seat@example.test"
bearer_env = "MAIL_API_TOKEN"
max_pages = 10
```

Export the referenced token and run
`gsuite-seatpool --config seatpool.toml inbox-pull --limit 50`. The URL must be
an HTTPS origin without credentials, query, fragment, or a path prefix.
GET `/v1/messages` returns at most 100 metadata records per page:

```json
{"messages": [{"id": "00000000-0000-4000-8000-000000000001", "size": 128, "envelope": {"from": "sender@example.test", "to": "seat@example.test", "received": "2026-01-01T00:00:00Z"}}], "cursor": null}
```

A non-null opaque cursor is sent as a query parameter on the same endpoint.
GET `/v1/messages/{id}/raw` must return the exact RFC5322 bytes, matching `size`.
IDs must be lowercase UUIDv4. Envelope recipient must match the configured
mailbox (case-insensitive); missing or mismatched metadata fails the page before
any raw fetch. The trusted envelope recipient overrides MIME `To`, `Delivered-To`,
and `X-Original-To`. The sender and subject remain MIME content, not authenticated
claims. The envelope's UTC received timestamp is stored with the inbox row.

Requests disable redirects, use TLS verification, and have a 30-second socket
timeout plus an elapsed-time check while streaming. List bodies are capped at
1 MiB and raw bodies at 25 MiB. Errors omit URLs, tokens and response bodies.
MIME is parsed as bytes, preserving declared non-UTF8 charsets. Invalid bytes in
known charsets use replacement characters; unknown/unsupported charsets fail
explicitly without advancing that scan segment.

`--limit` bounds newly ingested messages (1..1000). `max_pages` bounds scan
segments per invocation (1..100, default 10), including a resumed partial page;
there are at most `max_pages` list requests and `limit` raw fetches. Persistent
SQLite state stores pending page metadata and the next cursor together with
inbox rows and deduplication IDs. Small limits resume the unconsumed part of a
page rather than skipping it; already ingested IDs do not consume the message
limit. This advances through old pages across bounded calls instead of repeatedly
starting at the first page. After the final page is consumed, the next invocation
starts a new sweep to discover later arrivals.

Each segment commits atomically. A failed segment is retryable; earlier successful
pages remain committed even if the command later fails and emits no success count.
Checkpoints are scoped by normalized HTTPS host/port and mailbox, independently
of the bearer token, so token rotation does not duplicate messages. Run only one
pull per mailbox/database at a time. Expired server cursors and permanently invalid
MIME require operator intervention; they are not silently skipped.

## Packages

| Package | Responsibility |
|---|---|
| `dns_zone` | DNS adapters, factory, record presets |
| `directory_seats` | Local generation, CSV export, Google auth and Directory API |
| `catchall_mail` | MIME parsing, local ingestion, pattern extraction |
| `seat_allocator` | SQLite inventory and provisioned-seat allocation |
| `gsuite_seatpool` | TOML configuration, CLI, offline mock server |

## Current limits

- Generation defaults to 50 but accepts larger positive counts; this is not a Google licensing entitlement. The offline mock retains a simulated 50-user limit for quota-error tests.
- Explicit organization/domain linkage supports operator-declared shared planned capacity. This is local reservation accounting, not live Google license discovery. Legacy domains remain unlinked until explicitly associated.
- New passwords use cryptographic randomness; reinitializing inventory preserves existing credentials. New database and CSV files are private (0600); CSV exports refuse existing destinations. Existing database permissions are preserved. SQLite/CSV credentials remain plaintext.
- No-config endpoints are loopback mocks. Real Google use requires the production URL and credentials.
- Safe Directory reads retry transient HTTP responses. User creation is not blindly retried.

## Verification

```bash
python3 -m pytest
python3 scripts/public-hygiene-gate.py --repo-root .
git diff --check
```

Tests run offline with temporary databases and provider doubles. They do not certify live Google, Cloudflare, or registrar access. Before publishing, audit tracked files and reachable history; the hygiene gate checks known patterns, not every possible secret. See [SECURITY.md](SECURITY.md).
