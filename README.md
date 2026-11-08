# gsuite-seatpool

Multi-domain DNS zone provisioner, Cloud Identity Free 50-seat directory generator, catch-all RFC5322 OTP router, and round-robin seat allocator.

`gsuite-seatpool` decouples Google OAuth identity provisioning from paid mailbox licensing by pairing **Cloud Identity Free** directory seats (`50` seats/domain ceiling) with **registrar-level catch-all MX/TXT routing**. Run the pre-publish hygiene gate (`python3 scripts/public-hygiene-gate.py --repo-root .`) before any public push.

1. **`dns_zone`**: Automate `TXT` (`google-site-verification=...`), `MX`, `SPF`, and nameserver delegation against registrar zone APIs.
2. **`directory_seats`**: Deterministically generate up to 50 user seats per domain, export bulk CSVs matching the Google Admin Console schema (`Change Password at Next Sign-In = False`), or provision directly via the Admin SDK Directory v1 API (`POST /admin/directory/v1/users`).
3. **`catchall_mail`**: Parse inbound RFC5322 MIME messages across catch-all domains (`*@domain.tld`) and extract 6-digit OTPs and verification URLs without post-signup timestamp truncation.
4. **`seat_allocator`**: Allocate seats across domains in strict round-robin order while enforcing a per-domain, per-target daily budget (`max_per_domain_day`).

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       gsuite-seatpool CLI / Runner                      │
└─────────┬───────────────────┬─────────────────────┬─────────────────────┘
          │                   │                     │
          ▼                   ▼                     ▼
┌───────────────────┐ ┌───────────────────┐ ┌─────────────────────────────┐
│     dns_zone      │ │  directory_seats  │ │        catchall_mail        │
│ TXT verify + MX   │ │ 50-seat CSV / API │ │ RFC5322 MIME + OTP/URL poll │
└─────────┬─────────┘ └─────────┬─────────┘ └──────────────┬──────────────┘
          │                     │                          │
          └─────────────────────┼──────────────────────────┘
                                ▼
                  ┌───────────────────────────┐
                  │      seat_allocator       │
                  │ Round-Robin + SQLite WAL  │
                  └───────────────────────────┘
```

## Monorepo Layout

| Package | Path | Responsibility |
|---|---|---|
| `dns_zone` | `packages/dns_zone` | Registrar DNS zone record CRUD (`A`, `AAAA`, `CNAME`, `TXT`, `MX`, `NS`) and catch-all presets (`forwardemail`, `improvmx`, `custom_mx`) |
| `directory_seats` | `packages/directory_seats` | Deterministic seat seeding (`1..50`), Admin Console CSV exporter, and Google Admin SDK `directory_v1` client |
| `catchall_mail` | `packages/catchall_mail` | Multipart MIME parser, OTP/URL regex extraction, and SQLite/in-memory inbox polling |
| `seat_allocator` | `packages/seat_allocator` | SQLite persistence (`tenants`, `dns_records`, `seats`, `allocations`, `inbox_messages`, `events`) and round-robin allocator |
| `gsuite_seatpool` | `packages/gsuite_seatpool` | Unified CLI and offline FastAPI mock server for registrar DNS and Google Directory APIs |

## Installation

```bash
git clone https://github.com/rrddngtnn/gsuite-seatpool
cd gsuite-seatpool
python3 -m pip install -e ".[dev]"
```

Or run directly from source:

```bash
export PYTHONPATH=packages
```

## Quickstart

```bash
# Initialize two tenant domains (50 seats each = 100 seats)
make init

# Export Google Admin Console bulk-upload CSVs
make export-csv

# Allocate 4 seats for target "github" in round-robin order (max 2/domain/day)
make allocate N=4 CAP=2

# Run offline test suite
make test
```

Sample `allocate` output:

```json
{
  "target": "github",
  "requested": 4,
  "allocated_count": 4,
  "allocated": [
    {
      "seat_id": 1,
      "fqdn": "alpha.example.test",
      "seat_index": 1,
      "email": "unit01@alpha.example.test",
      "target": "github"
    },
    {
      "seat_id": 51,
      "fqdn": "beta.example.test",
      "seat_index": 1,
      "email": "unit01@beta.example.test",
      "target": "github"
    }
  ],
  "skipped_domains": {}
}
```

## Catch-All Routing Presets

| Preset | DNS Records Provisioned | External Account Required |
|---|---|---|
| `forwardemail` | `MX 10 mx1.forwardemail.net`, `MX 10 mx2.forwardemail.net`, `TXT forward-email=<target>`, `TXT v=spf1 ...` | No (configured via DNS TXT) |
| `improvmx` | `MX 10 mx1.improvmx.com`, `MX 20 mx2.improvmx.com`, `TXT v=spf1 include:spf.improvmx.com ~all` | Yes (domain registered in ImprovMX) |
| `custom_mx` | `MX 10 <target>` | Self-hosted SMTP / LMTP listener |

## CLI Reference

```bash
gsuite-seatpool doctor
gsuite-seatpool mock --host 127.0.0.1 --port 8788
gsuite-seatpool init-tenant --fqdn alpha.example.test --seats 50
gsuite-seatpool dns-verify --fqdn alpha.example.test --token google-site-verification=abc123
gsuite-seatpool dns-catchall --fqdn alpha.example.test --mode forwardemail --target master@example.test
gsuite-seatpool export-csv --out-dir ./csv_out
gsuite-seatpool sync-directory --fqdn alpha.example.test --limit 50
gsuite-seatpool allocate --target github --count 8 --max-per-domain-day 3
gsuite-seatpool inbox-ingest --file message.eml
gsuite-seatpool status
```

## Operational Limits

- **Cloud Identity Free Cap**: Hard ceiling of `50` user seats per domain. `generate_domain_seats()` and the mock Directory API reject requests above `50`.
- **Per-Domain Daily Target Guard**: `allocate_round_robin()` tracks allocations in a rolling 24-hour window (`allocations.allocated_at`) and skips any domain that reaches `max_per_domain_day` for a given target service.
- **Cleartext HTTP/1.1 Negotiation**: `build_http_session()` forces `http_version="v1"` on `http://` endpoints so `curl_cffi` TLS impersonation profiles do not emit `Upgrade: h2c` frames against local ASGI mock servers.

## Test Suite

```bash
python3 -m pytest
```

Offline unit and integration tests covering:
- Registrar DNS zone CRUD, idempotent TXT verification, and catch-all MX/SPF presets
- Deterministic seat generation, password policy compliance, 50-seat ceiling enforcement, and Admin Console CSV formatting
- Google Admin SDK Directory v1 user provisioning, duplicate detection (`409`), and 50-seat quota exhaustion (`403`)
- RFC5322 multipart MIME parsing, 6-digit OTP extraction, and verification URL polling
- Multi-domain round-robin seat allocation and per-domain daily target budget enforcement
