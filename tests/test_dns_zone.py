"""Tests for registrar DNS zone CRUD, TXT verification, and catch-all MX/SPF presets."""
from __future__ import annotations

import pytest

from dns_zone.client import DnsError, DnsZoneClient


def test_dns_login_and_empty_zone(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    user = c.login("owner@example.test", "Secret123!")
    assert user["email"] == "owner@example.test"
    assert c.list_records("alpha.example.test") == []


def test_dns_login_rejects_short_password(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    with pytest.raises(DnsError) as exc:
        c.login("owner@example.test", "short")
    assert exc.value.status == 400


def test_dns_add_and_delete_txt_record(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    rec = c.add_record("alpha.example.test", "TXT", "_probe", "v=1", ttl=300)
    assert rec["type"] == "TXT"
    assert rec["name"] == "_probe"
    assert rec["value"] == "v=1"
    assert len(c.list_records("alpha.example.test")) == 1

    res = c.delete_record("alpha.example.test", rec["id"])
    assert res["ok"] is True
    assert c.list_records("alpha.example.test") == []


def test_dns_validation_errors(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    with pytest.raises(DnsError) as e_type:
        c.add_record("alpha.example.test", "SRV", "@", "1 2 3 target")
    assert e_type.value.payload["error"] == "unsupported_record_type"

    with pytest.raises(DnsError) as e_val:
        c.add_record("alpha.example.test", "TXT", "@", "")
    assert e_val.value.payload["error"] == "empty_record_value"

    with pytest.raises(DnsError) as e_name:
        c.add_record("alpha.example.test", "TXT", "!!!bad!!!", "v=1")
    assert e_name.value.payload["error"] == "invalid_record_name"

    with pytest.raises(DnsError) as e_ip:
        c.add_record("alpha.example.test", "A", "@", "not-an-ip")
    assert e_ip.value.payload["error"] == "invalid_record_value"


def test_dns_ensure_google_verification_idempotent(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    r1 = c.ensure_google_verification("alpha.example.test", "abc123token")
    assert r1["action"] == "created"
    assert r1["record"]["value"] == "google-site-verification=abc123token"

    r2 = c.ensure_google_verification("alpha.example.test", "google-site-verification=abc123token")
    assert r2["action"] == "unchanged"
    assert len(c.list_records("alpha.example.test")) == 1


def test_dns_ensure_catchall_presets_idempotent(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    first = c.ensure_catchall("alpha.example.test", "forwardemail", "inbox@master.test")
    assert len(first) == 4
    assert all(x["action"] == "created" for x in first)

    second = c.ensure_catchall("alpha.example.test", "forwardemail", "inbox@master.test")
    assert all(x["action"] == "unchanged" for x in second)
    assert len(c.list_records("alpha.example.test")) == 4


def test_dns_nameserver_update(session_adapter):
    c = DnsZoneClient("http://mock.local", session=session_adapter)
    with pytest.raises(DnsError) as exc:
        c.set_nameservers("alpha.example.test", ["ns1.cloudflare.com"])
    assert exc.value.payload["error"] == "invalid_nameservers"

    ok = c.set_nameservers(
        "alpha.example.test",
        ["anna.ns.cloudflare.com", "bob.ns.cloudflare.com"],
    )
    assert ok["ok"] is True
