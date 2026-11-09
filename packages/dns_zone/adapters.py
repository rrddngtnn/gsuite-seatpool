"""DNS provider adapters sharing the DnsZoneClient record interface."""
import os

from .client import DnsError, DnsZoneClient


class ManualDnsProvider(DnsZoneClient):
    """Collect instructions only; never report records as remotely provisioned."""

    def __init__(self):
        self.planned_records = []

    def list_records(self, fqdn):
        return []

    def add_record(self, fqdn, record_type, name, value, ttl=300, priority=None):
        record = dict(type=record_type.upper(), name=name, value=value,
                      ttl=int(ttl), priority=priority)
        self.planned_records.append({"fqdn": fqdn, **record})
        return record

    def ensure_google_verification(self, fqdn, token, ttl=300):
        result = super().ensure_google_verification(fqdn, token, ttl)
        return {**result, "action": "planned"}

    def ensure_catchall(self, fqdn, mode, target):
        return [{**result, "action": "planned"}
                for result in super().ensure_catchall(fqdn, mode, target)]

    def delete_record(self, fqdn, record_id):
        raise NotImplementedError("Manual DNS cannot delete remote records")

    def set_nameservers(self, fqdn, nameservers):
        raise NotImplementedError("Manual DNS cannot change nameserver delegation")


def _required(config, field):
    value = config.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"DNS configuration requires {field}")
    return value


def _environment(config, field):
    reference = _required(config, field)
    value = os.environ.get(reference)
    if not value or not value.strip():
        raise ValueError(f"DNS credential environment variable for {field} is missing or empty")
    return value


class RegistrarDnsProvider(DnsZoneClient):
    """Legacy registrar client with optional environment-based login."""

    def __init__(self, config, *, session=None):
        base_url = _required(config, "base_url")
        credentials = None
        if "email_env" in config or "password_env" in config:
            credentials = (_environment(config, "email_env"), _environment(config, "password_env"))
        super().__init__(base_url, session=session, proxy=config.get("proxy"))
        if credentials:
            try:
                self.login(*credentials)
            except Exception:
                raise DnsError(401, {"error": "registrar_login_failed"}) from None


class CloudflareDnsProvider(DnsZoneClient):
    """Cloudflare v4 DNS records normalized to the legacy record schema."""

    def __init__(self, config, *, session=None):
        from urllib.parse import quote

        zone_id = _required(config, "zone_id")
        self._token = _environment(config, "token_env")
        super().__init__("https://api.cloudflare.com/client/v4", session=session)
        self._records_url = f"{self.base_url}/zones/{quote(zone_id, safe='')}/dns_records"

    def _request(self, method, url, **kwargs):
        try:
            response = getattr(self.session, method)(
                url, headers={"Authorization": f"Bearer {self._token}",
                              "Content-Type": "application/json"}, timeout=30, **kwargs)
            body = response.json()
        except Exception:
            raise DnsError(502, {"error": "cloudflare_transport_error"}) from None
        if not 200 <= response.status_code < 300 or not isinstance(body, dict) or body.get("success") is not True:
            raise DnsError(response.status_code, {"error": "cloudflare_request_failed"})
        return body

    @staticmethod
    def _normalize(record, fqdn):
        name = record["name"].rstrip(".").lower()
        domain = fqdn.rstrip(".").lower()
        if name == domain:
            name = "@"
        elif name.endswith("." + domain):
            name = name[:-(len(domain) + 1)]
        return dict(id=record["id"], type=record["type"].upper(), name=name,
                    value=record["content"], ttl=record["ttl"], priority=record.get("priority"))

    def list_records(self, fqdn):
        records = []
        page = 1
        while True:
            body = self._request("get", self._records_url, params={"page": page, "per_page": 100})
            records.extend(self._normalize(record, fqdn) for record in body["result"])
            if page >= body.get("result_info", {}).get("total_pages", 1):
                return records
            page += 1


    def add_record(self, fqdn, record_type, name, value, ttl=300, priority=None):
        import json

        domain = fqdn.rstrip(".")
        owner = name.rstrip(".")
        if owner == "@":
            owner = domain
        elif owner.lower() != domain.lower() and not owner.lower().endswith("." + domain.lower()):
            owner = f"{owner}.{domain}"
        payload = dict(type=record_type.upper(), name=owner, content=value, ttl=int(ttl))
        if priority is not None:
            payload["priority"] = int(priority)
        body = self._request("post", self._records_url, data=json.dumps(payload))
        return self._normalize(body["result"], fqdn)

    def delete_record(self, fqdn, record_id):
        from urllib.parse import quote

        self._request("delete", f"{self._records_url}/{quote(str(record_id), safe='')}")
        return {"ok": True, "id": record_id}

    def set_nameservers(self, fqdn, nameservers):
        raise NotImplementedError("Cloudflare DNS does not manage registrar delegation")


def create_dns_provider(config, *, session=None):
    """Construct a DNS provider; session injection supports offline transports."""
    provider = _required(config, "provider")
    if provider == "manual":
        return ManualDnsProvider()
    if provider == "registrar":
        return RegistrarDnsProvider(config, session=session)
    if provider == "cloudflare":
        return CloudflareDnsProvider(config, session=session)
    from importlib import metadata

    matches = [entry for entry in metadata.entry_points(group="gsuite_seatpool.dns")
               if entry.name == provider]
    if len(matches) > 1:
        raise ValueError("Ambiguous DNS provider entry points")
    if matches:
        return matches[0].load()(dict(config))
    raise ValueError("Unknown DNS provider")
