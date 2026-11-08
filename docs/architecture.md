# Architecture & Operational Contracts

## 1. Split-Brain Identity and Mail Routing

Google Workspace Business Starter charges per mailbox. Cloud Identity Free provides the same Google Account OAuth identity (`accounts.google.com`) and Admin Console (`admin.google.com`) directory management for up to 50 users per domain at zero cost, without Gmail mailbox provisioning.

By keeping `MX` records pointed at a catch-all mail relay (`forwardemail`, `improvmx`, or a self-hosted SMTP listener) while verifying the domain in Google Admin Console via a `TXT` record (`google-site-verification=...`):

1. Directory users (`unit01@domain.tld` .. `unit50@domain.tld`) authenticate via Google OAuth (`Sign in with Google`) without requiring SMS verification during creation.
2. Inbound verification emails and OTP codes sent to `unitXX@domain.tld` route through the catch-all `MX` into a single consolidated mailbox or SQLite table (`inbox_messages`).

## 2. Registrar DNS API Contract

Verified endpoint contract for domain zone management:

```text
GET    /api/domains/<fqdn>/dns                 -> 200 {"records": [...], "supported_types": [...]}
POST   /api/domains/<fqdn>/dns                 -> 201 {"record": {"id", "type", "name", "value", "ttl", "priority"}}
DELETE /api/domains/<fqdn>/dns/<record_id>     -> 200 {"ok": true}
PUT    /api/domains/<fqdn>/nameservers         -> 200 {"ok": true}
```

Validation errors returned by the mock handler:
- `400 unsupported_record_type`: `type` missing or outside `A`, `AAAA`, `CNAME`, `TXT`, `MX`, `NS`.
- `400 empty_record_value`: `value` empty.
- `400 invalid_record_name`: `name` contains illegal hostname characters.
- `400 invalid_record_value`: `A` value is not IPv4 or `MX` value is not a valid FQDN.
- `400 invalid_nameservers`: fewer than 2 nameservers supplied to `PUT /nameservers`.

## 3. Round-Robin Seat Allocation

Registering consecutive accounts from a single custom domain (`unit01@domainA`, `unit02@domainA`, ...) triggers domain-level clustering heuristics on target platforms. `seat_allocator.allocate_round_robin()` enforces two invariants:

1. **Cross-Domain Interleaving**: For `D` active domains, allocation `i` selects from domain `i mod D` before advancing to seat index `2` on any domain.
2. **Per-Domain Daily Target Ceiling**: Each `(fqdn, target)` pair is capped at `max_per_domain_day` allocations within a rolling 24-hour window.
