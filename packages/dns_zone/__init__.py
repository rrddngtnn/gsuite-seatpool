"""Registrar DNS zone record and catch-all MX/TXT provisioning client."""
from .client import (
    CATCHALL_PRESETS,
    SUPPORTED_RECORD_TYPES,
    DnsError,
    DnsZoneClient,
    build_http_session,
)

__all__ = [
    "CATCHALL_PRESETS",
    "SUPPORTED_RECORD_TYPES",
    "DnsError",
    "DnsZoneClient",
    "build_http_session",
]
