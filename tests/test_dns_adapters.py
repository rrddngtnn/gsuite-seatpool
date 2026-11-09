"""Offline contract tests for pluggable DNS adapters."""
import pytest

import dns_zone


def test_factory_is_also_available_from_legacy_client_module():
    from dns_zone import client

    assert callable(getattr(client, "create_dns_provider", None))
    assert isinstance(client.create_dns_provider({"provider": "manual"}), dns_zone.ManualDnsProvider)


def test_registrar_login_failure_redacts_server_and_transport_secrets(monkeypatch):
    monkeypatch.setenv("DNS_EMAIL", "owner@example.test")
    monkeypatch.setenv("DNS_PASSWORD", "private-password")
    for response in [OfflineResponse({"error": "private-password"}, 401), RuntimeError("private-password")]:
        with pytest.raises(dns_zone.DnsError) as exc:
            dns_zone.create_dns_provider({"provider": "registrar", "base_url": "http://mock.local",
                "email_env": "DNS_EMAIL", "password_env": "DNS_PASSWORD"}, session=OfflineSession(response))
        assert "private-password" not in str(exc.value)
        assert "private-password" not in repr(exc.value.payload)


def test_entrypoint_plugin_receives_configuration(monkeypatch):
    from importlib import metadata

    received = []
    sentinel = object()

    class Plugin:
        name = "custom"

        def load(self):
            def factory(config):
                received.append(config)
                return sentinel
            return factory

    def entry_points(**kwargs):
        assert kwargs == {"group": "gsuite_seatpool.dns"}
        return [Plugin()]

    monkeypatch.setattr(metadata, "entry_points", entry_points)
    config = {"provider": "custom", "custom_setting": "value"}
    assert dns_zone.create_dns_provider(config) is sentinel
    assert received == [config]


def test_duplicate_entrypoint_is_rejected(monkeypatch):
    from importlib import metadata
    from types import SimpleNamespace

    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [
        SimpleNamespace(name="duplicate"), SimpleNamespace(name="duplicate")])
    with pytest.raises(ValueError, match="[Aa]mbiguous"):
        dns_zone.create_dns_provider({"provider": "duplicate"})


def test_cloudflare_errors_do_not_expose_credentials(monkeypatch):
    monkeypatch.setenv("CF_TOKEN", "private-token")
    for response in [OfflineResponse({"success": False, "errors": ["private-token"]}, 403),
                     RuntimeError("private-token")]:
        provider = dns_zone.create_dns_provider(
            {"provider": "cloudflare", "token_env": "CF_TOKEN", "zone_id": "zone123"},
            session=OfflineSession(response))
        with pytest.raises(dns_zone.DnsError) as exc:
            provider.list_records("example.test")
        assert "private-token" not in str(exc.value)
        assert "private-token" not in repr(exc.value.payload)
        assert exc.value.__cause__ is None


def test_cloudflare_add_and_delete_use_standard_api(monkeypatch):
    import json

    monkeypatch.setenv("CF_TOKEN", "private-token")
    session = OfflineSession(
        OfflineResponse({"success": True, "result": {"id": "r1", "type": "MX", "name": "example.test", "content": "mx.test", "ttl": 300, "priority": 10}}),
        OfflineResponse({"success": True, "result": {"id": "r1"}}),
        OfflineResponse({"success": True, "result": {"id": "r2", "type": "TXT", "name": "_proof.example.test", "content": "proof", "ttl": 300}}),
    )
    provider = dns_zone.create_dns_provider(
        {"provider": "cloudflare", "token_env": "CF_TOKEN", "zone_id": "zone123"}, session=session)
    record = provider.add_record("example.test", "mx", "@", "mx.test", priority=10)
    assert record["id"] == "r1" and record["name"] == "@"
    assert json.loads(session.calls[0][2]["data"]) == {
        "type": "MX", "name": "example.test", "content": "mx.test", "ttl": 300, "priority": 10,
    }
    assert provider.delete_record("example.test", "r1") == {"ok": True, "id": "r1"}
    assert session.calls[1][0:2] == ("DELETE", "https://api.cloudflare.com/client/v4/zones/zone123/dns_records/r1")
    provider.add_record("example.test", "TXT", "_proof", "proof")
    assert json.loads(session.calls[2][2]["data"])["name"] == "_proof.example.test"
    assert "priority" not in json.loads(session.calls[2][2]["data"])
    with pytest.raises(NotImplementedError):
        provider.set_nameservers("example.test", ["ns1.test", "ns2.test"])


# All HTTP interactions below use deterministic offline responses.


class OfflineResponse:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


class OfflineSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        assert self.responses, "Unexpected HTTP request"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._request("DELETE", url, **kwargs)


def test_cloudflare_paginates_normalizes_and_reuses_existing_records(monkeypatch):
    monkeypatch.setenv("CF_TOKEN", "private-token")
    pages = [
        OfflineResponse({"success": True, "result": [
            {"id": "one", "type": "TXT", "name": "example.test", "content": "google-site-verification=abc", "ttl": 1}],
            "result_info": {"page": 1, "total_pages": 2}}),
        OfflineResponse({"success": True, "result": [
            {"id": "two", "type": "MX", "name": "mail.example.test", "content": "mx.test", "ttl": 300, "priority": 10}],
            "result_info": {"page": 2, "total_pages": 2}}),
    ]
    session = OfflineSession(*pages, *pages)
    provider = dns_zone.create_dns_provider(
        {"provider": "cloudflare", "token_env": "CF_TOKEN", "zone_id": "zone123"}, session=session)
    records = provider.list_records("example.test")
    assert records == [
        {"id": "one", "type": "TXT", "name": "@", "value": "google-site-verification=abc", "ttl": 1, "priority": None},
        {"id": "two", "type": "MX", "name": "mail", "value": "mx.test", "ttl": 300, "priority": 10},
    ]
    assert session.calls[0][1] == "https://api.cloudflare.com/client/v4/zones/zone123/dns_records"
    assert session.calls[0][2]["headers"]["Authorization"] == "Bearer private-token"
    assert [c[2]["params"]["page"] for c in session.calls] == [1, 2]
    assert provider.ensure_google_verification("example.test", "abc")["action"] == "unchanged"
    assert "private-token" not in repr(provider)


@pytest.mark.parametrize("config", [
    {"provider": "cloudflare"},
    {"provider": "cloudflare", "zone_id": "zone123"},
    {"provider": "cloudflare", "zone_id": "zone123", "token_env": "MISSING_CF_TOKEN"},
])
def test_cloudflare_requires_zone_and_environment_token(config):
    with pytest.raises(ValueError):
        dns_zone.create_dns_provider(config)


def test_registrar_optional_environment_login(monkeypatch, session_adapter):
    config = {"provider": "registrar", "base_url": "http://mock.local"}
    provider = dns_zone.create_dns_provider(config, session=session_adapter)
    assert isinstance(provider, dns_zone.DnsZoneClient)
    assert provider.list_records("example.test") == []
    monkeypatch.setenv("DNS_EMAIL", "owner@example.test")
    monkeypatch.setenv("DNS_PASSWORD", "Secret123!")
    provider = dns_zone.create_dns_provider(
        {**config, "email_env": "DNS_EMAIL", "password_env": "DNS_PASSWORD"},
        session=session_adapter,
    )
    assert provider.ensure_google_verification("example.test", "abc")["action"] == "created"


@pytest.mark.parametrize("config", [
    {}, {"provider": "unknown"}, {"provider": "registrar"},
    {"provider": "registrar", "base_url": "http://mock.local", "email_env": "ABSENT"},
    {"provider": "registrar", "base_url": "http://mock.local", "email_env": "ABSENT", "password_env": "ABSENT"},
])
def test_factory_rejects_invalid_registrar_configuration(config):
    with pytest.raises(ValueError):
        dns_zone.create_dns_provider(config)


def test_manual_factory_emits_plans_without_claiming_remote_state():
    provider = dns_zone.create_dns_provider({"provider": "manual"})
    result = provider.ensure_google_verification("example.test", "abc")
    assert result["action"] == "planned"
    assert result["record"] == {
        "type": "TXT", "name": "@", "value": "google-site-verification=abc",
        "ttl": 300, "priority": None,
    }
    assert provider.list_records("example.test") == []
    assert provider.planned_records == [{"fqdn": "example.test", **result["record"]}]
    assert all(r["action"] == "planned" for r in provider.ensure_catchall(
        "example.test", "improvmx", "inbox@example.test"))
    with pytest.raises(NotImplementedError):
        provider.delete_record("example.test", "id")
    with pytest.raises(NotImplementedError):
        provider.set_nameservers("example.test", ["ns1.test", "ns2.test"])
