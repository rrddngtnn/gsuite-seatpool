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
