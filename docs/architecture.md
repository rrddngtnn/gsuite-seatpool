# Architecture and operational contracts

## Boundaries

Google Directory manages Google identities. DNS adapters publish records. External mail services deliver messages; the MIME ingester only processes supplied files/stdin. None of these operations alone proves the others succeeded.

## Configuration and providers

The CLI resolves flags over exported environment over versioned TOML over defaults. DNS uses a provider factory with manual, registrar REST, and Cloudflare implementations. Python integrations and CLI configuration can load factories from `gsuite_seatpool.dns` entry points.

The registrar adapter implements this specific contract:

```text
POST   /api/auth/login                   -> 200 {user: ...}
GET    /api/domains/<fqdn>/dns            -> 200 {records: [...]}
POST   /api/domains/<fqdn>/dns            -> 201 {record: ...}
DELETE /api/domains/<fqdn>/dns/<id>       -> 200 {...}
PUT    /api/domains/<fqdn>/nameservers    -> 200 {...}
```

This is not a universal registrar API. New APIs need adapters.

Google authentication is injected into the Directory client. Authorized-user and delegated service-account credentials refresh through google-auth. Bearer-token mode leaves token lifecycle to the caller. Paginated reads retry bounded transient HTTP failures; creates are not blindly retried.

## Lifecycle

- `init-tenant` stages local inventory; it does not create an organization.
- `plan` reads local pending inventory without network or database writes.
- DNS verification TXT submission sets `dns_pending`, never `verified`.
- Manual DNS emits `planned` operations without claiming remote readiness.
- Sync lists existing users first, creates missing primary addresses, and commits each successful local update independently.
- Reconcile reads remote users and updates matching local staged seats. It marks suspended/archived remote users suspended locally and previously provisioned absent users missing after a complete listing. It does not delete or recreate remote users or reset passwords.
- Allocation selects only locally `provisioned` seats. CLI output removes passwords; explicit CSV export retains them.

## Remaining model limitations

The legacy `tenants` table represents domain inventories. Explicit `organizations` and `organization_domains` tables associate inventories and enforce shared operator-planned reservations. Legacy domains remain unlinked on migration. Capacity is not a live Google licensing entitlement. Generation has no hard 50-seat ceiling; 50 is a local default. The mock retains a simulated quota for error tests.

New passwords use cryptographic randomness; existing credentials survive reinitialization. New DB/CSV files use private permissions and reject symlink destinations. Credentials remain plaintext. Read-only IMAP SSL ingestion uses persistent account/mailbox + UIDVALIDITY + UID checkpoints with atomic batch ingestion. Latest-batch fetching is not full historical backfill. REST MIME ingestion uses an explicit HTTPS JSON contract with bounded same-origin pagination and atomic account/message-ID checkpoints. No live license discovery is claimed.

## Verification boundary

Automated tests use local temporary databases and provider doubles. Passing these checks demonstrates tested behavior, not live provider eligibility, Google verification, or email deliverability. Provider integration checks must be explicitly scoped before contacting real services.
