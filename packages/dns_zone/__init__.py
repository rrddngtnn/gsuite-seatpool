"""Registrar DNS zone record and catch-all MX/TXT provisioning client."""
from .client import (
    CATCHALL_PRESETS,
    SUPPORTED_RECORD_TYPES,
    DnsError,
    DnsZoneClient,
    build_http_session,
)

from .adapters import (
    CloudflareDnsProvider,
    ManualDnsProvider,
    RegistrarDnsProvider,
    create_dns_provider,
)

__all__ = [
    "CloudflareDnsProvider",
    "RegistrarDnsProvider",
    "ManualDnsProvider",
    "create_dns_provider",
    "CATCHALL_PRESETS",
    "SUPPORTED_RECORD_TYPES",
    "DnsError",
    "DnsZoneClient",
    "build_http_session",
]
