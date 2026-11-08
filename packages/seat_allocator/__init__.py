"""SQLite state persistence and multi-domain round-robin seat allocator."""
from .allocator import AllocationResult, allocate_round_robin, domain_daily_usage
from .store import (
    TENANT_STATUSES,
    add_dns_record,
    add_seat,
    counts,
    list_inbox_messages,
    log_event,
    open_db,
    set_tenant_status,
    store_inbox_message,
    upsert_tenant_domain,
)

__all__ = [
    "TENANT_STATUSES",
    "AllocationResult",
    "add_dns_record",
    "add_seat",
    "allocate_round_robin",
    "counts",
    "domain_daily_usage",
    "list_inbox_messages",
    "log_event",
    "open_db",
    "set_tenant_status",
    "store_inbox_message",
    "upsert_tenant_domain",
]
