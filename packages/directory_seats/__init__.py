"""Google Workspace / Cloud Identity Free 50-seat generator and Admin SDK Directory client."""
from .client import DirectoryApiError, DirectoryClient
from .generator import (
    ADMIN_CSV_HEADERS,
    CLOUD_IDENTITY_FREE_CAP,
    export_admin_csv,
    generate_domain_seats,
    make_seat,
    redact_seat,
)

__all__ = [
    "ADMIN_CSV_HEADERS",
    "CLOUD_IDENTITY_FREE_CAP",
    "DirectoryApiError",
    "DirectoryClient",
    "export_admin_csv",
    "generate_domain_seats",
    "make_seat",
    "redact_seat",
]
