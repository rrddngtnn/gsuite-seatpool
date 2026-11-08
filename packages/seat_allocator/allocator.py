"""Round-robin seat allocation across tenant domains with per-domain daily target budgets."""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import sqlite3

from .store import log_event, utcnow


@dataclass
class AllocationResult:
    target: str
    requested: int
    max_per_domain_day: int
    allocated: list[dict] = field(default_factory=list)
    skipped_domains: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return len(self.allocated) == self.requested


def domain_daily_usage(
    db: sqlite3.Connection,
    fqdn: str,
    target: str,
    window_hours: int = 24,
) -> int:
    cutoff = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(hours=window_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = db.execute(
        "SELECT COUNT(*) FROM allocations "
        "WHERE fqdn=? AND target=? AND allocated_at >= ?",
        (fqdn, target, cutoff),
    ).fetchone()
    return int(row[0])


def allocate_round_robin(
    db: sqlite3.Connection,
    target: str,
    count: int = 1,
    max_per_domain_day: int = 3,
    window_hours: int = 24,
) -> AllocationResult:
    """Select seats across active/staged tenant domains in strict round-robin order."""
    res = AllocationResult(
        target=target,
        requested=count,
        max_per_domain_day=max_per_domain_day,
    )
    if count <= 0:
        return res

    domains = [
        r["fqdn"]
        for r in db.execute(
            "SELECT fqdn FROM tenants WHERE status NOT IN ('suspended', 'dead') ORDER BY id"
        ).fetchall()
    ]
    if not domains:
        return res

    exhausted: set[str] = set()
    cursor = 0

    while len(res.allocated) < count and len(exhausted) < len(domains):
        fqdn = domains[cursor % len(domains)]
        cursor += 1
        if fqdn in exhausted:
            continue

        used = domain_daily_usage(db, fqdn, target, window_hours=window_hours)
        if used >= max_per_domain_day:
            exhausted.add(fqdn)
            res.skipped_domains[fqdn] = f"daily_budget_exhausted ({used}/{max_per_domain_day})"
            log_event(
                db,
                fqdn,
                "domain_budget_guard",
                f"target={target} used={used}/{max_per_domain_day}",
            )
            continue

        seat = db.execute(
            "SELECT s.id, s.fqdn, s.seat_index, s.email, s.password, s.first_name, s.last_name "
            "FROM seats s "
            "WHERE s.fqdn=? AND s.status IN ('ready', 'provisioned') "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM allocations a WHERE a.seat_id = s.id AND a.target = ?"
            ") "
            "ORDER BY s.allocated_count ASC, s.seat_index ASC LIMIT 1",
            (fqdn, target),
        ).fetchone()

        if seat is None:
            exhausted.add(fqdn)
            res.skipped_domains[fqdn] = "no_unallocated_seat_for_target"
            continue

        now = utcnow()
        db.execute(
            "INSERT INTO allocations(seat_id, fqdn, email, target, allocated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (seat["id"], seat["fqdn"], seat["email"], target, now),
        )
        db.execute(
            "UPDATE seats SET allocated_count = allocated_count + 1, last_used_at = ? WHERE id = ?",
            (now, seat["id"]),
        )
        db.commit()
        item = {
            "seat_id": int(seat["id"]),
            "fqdn": seat["fqdn"],
            "seat_index": int(seat["seat_index"]),
            "email": seat["email"],
            "password": seat["password"],
            "first_name": seat["first_name"],
            "last_name": seat["last_name"],
            "target": target,
            "allocated_at": now,
        }
        res.allocated.append(item)
        log_event(db, fqdn, "seat_allocated", f"{seat['email']} -> {target}")

    return res
