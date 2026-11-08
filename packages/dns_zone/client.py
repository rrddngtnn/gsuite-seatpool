"""HTTP client for registrar DNS zone records and nameserver delegation."""
from __future__ import annotations

import json

SUPPORTED_RECORD_TYPES = ("A", "AAAA", "CNAME", "TXT", "MX", "NS")

CATCHALL_PRESETS = {
    "forwardemail": lambda target: [
        {"type": "MX", "name": "@", "value": "mx1.forwardemail.net", "priority": 10, "ttl": 300},
        {"type": "MX", "name": "@", "value": "mx2.forwardemail.net", "priority": 10, "ttl": 300},
        {"type": "TXT", "name": "@", "value": f"forward-email={target}", "priority": None, "ttl": 300},
        {"type": "TXT", "name": "@", "value": "v=spf1 a mx include:spf.forwardemail.net -all", "priority": None, "ttl": 300},
    ],
    "improvmx": lambda target: [
        {"type": "MX", "name": "@", "value": "mx1.improvmx.com", "priority": 10, "ttl": 300},
        {"type": "MX", "name": "@", "value": "mx2.improvmx.com", "priority": 20, "ttl": 300},
        {"type": "TXT", "name": "@", "value": "v=spf1 include:spf.improvmx.com ~all", "priority": None, "ttl": 300},
    ],
    "custom_mx": lambda target: [
        {"type": "MX", "name": "@", "value": target, "priority": 10, "ttl": 300},
    ],
}


class DnsError(Exception):
    def __init__(self, status: int, payload: dict | str):
        self.status = status
        self.payload = payload
        code = payload.get("error") if isinstance(payload, dict) else str(payload)[:120]
        super().__init__(f"HTTP {status}: {code}")


def build_http_session(base_url: str, proxy: str | None = None, impersonate: str = "chrome131"):
    """Create a curl_cffi session forcing HTTP/1.1 on cleartext http:// targets."""
    from curl_cffi import requests as cf_requests

    http_ver = "v1" if base_url.startswith("http://") else None
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return cf_requests.Session(
        impersonate=impersonate,
        http_version=http_ver,
        proxies=proxies,
    )


def _json_post(session, url: str, payload: dict, timeout: int = 30):
    body = json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload
    return session.post(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )


def _json_put(session, url: str, payload: dict, timeout: int = 30):
    body = json.dumps(payload) if not isinstance(payload, (str, bytes)) else payload
    return session.put(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )


def _parse_body(resp) -> dict | str:
    try:
        return resp.json()
    except Exception:
        return getattr(resp, "text", "")


class DnsZoneClient:
    """Manage domain zone records and nameserver delegation against a registrar REST API."""

    def __init__(self, base_url: str, session=None, proxy: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.session = session or build_http_session(self.base_url, proxy=proxy)

    def login(self, email: str, password: str) -> dict:
        r = _json_post(
            self.session,
            f"{self.base_url}/api/auth/login",
            {"email": email, "password": password},
        )
        body = _parse_body(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body.get("user", body)
        raise DnsError(r.status_code, body)

    def list_records(self, fqdn: str) -> list[dict]:
        r = self.session.get(f"{self.base_url}/api/domains/{fqdn}/dns", timeout=30)
        body = _parse_body(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body.get("records", [])
        raise DnsError(r.status_code, body)

    def add_record(
        self,
        fqdn: str,
        record_type: str,
        name: str,
        value: str,
        ttl: int = 300,
        priority: int | None = None,
    ) -> dict:
        rtype = (record_type or "").upper()
        payload = {
            "type": rtype,
            "name": name,
            "value": value,
            "ttl": int(ttl),
        }
        if priority is not None:
            payload["priority"] = int(priority)
        r = _json_post(self.session, f"{self.base_url}/api/domains/{fqdn}/dns", payload)
        body = _parse_body(r)
        if r.status_code == 201 and isinstance(body, dict):
            return body["record"]
        raise DnsError(r.status_code, body)

    def delete_record(self, fqdn: str, record_id: int) -> dict:
        r = self.session.delete(
            f"{self.base_url}/api/domains/{fqdn}/dns/{record_id}",
            timeout=30,
        )
        body = _parse_body(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body
        raise DnsError(r.status_code, body)

    def set_nameservers(self, fqdn: str, nameservers: list[str]) -> dict:
        r = _json_put(
            self.session,
            f"{self.base_url}/api/domains/{fqdn}/nameservers",
            {"nameservers": list(nameservers)},
        )
        body = _parse_body(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body
        raise DnsError(r.status_code, body)

    def ensure_google_verification(self, fqdn: str, token: str, ttl: int = 300) -> dict:
        txt_val = (
            token
            if token.startswith("google-site-verification=")
            else f"google-site-verification={token}"
        )
        existing = self.list_records(fqdn)
        for rec in existing:
            if rec.get("type") == "TXT" and rec.get("name") == "@" and rec.get("value") == txt_val:
                return {"action": "unchanged", "record": rec}
        created = self.add_record(fqdn, "TXT", "@", txt_val, ttl=ttl)
        return {"action": "created", "record": created}

    def ensure_catchall(self, fqdn: str, mode: str, target: str) -> list[dict]:
        if mode not in CATCHALL_PRESETS:
            raise ValueError(f"Unsupported catch-all preset: {mode}")
        specs = CATCHALL_PRESETS[mode](target)
        existing = self.list_records(fqdn)
        results = []
        for spec in specs:
            match = next(
                (
                    r
                    for r in existing
                    if r.get("type") == spec["type"]
                    and r.get("name") == spec["name"]
                    and r.get("value") == spec["value"]
                ),
                None,
            )
            if match:
                results.append({"action": "unchanged", "record": match})
            else:
                rec = self.add_record(
                    fqdn,
                    spec["type"],
                    spec["name"],
                    spec["value"],
                    ttl=spec["ttl"],
                    priority=spec["priority"],
                )
                results.append({"action": "created", "record": rec})
        return results
