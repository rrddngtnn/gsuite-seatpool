"""Offline contract tests for the explicitly defined REST MIME adapter."""
import importlib

import pytest


RAW = "To: seat@example.test\r\nFrom: sender@example.test\r\nSubject: Code\r\n\r\nYour code is 123456"


class Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.closed = False

    def json(self):
        return self.payload

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *payloads):
        self.responses = [Response(payload) for payload in payloads]
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[len(self.calls) - 1]


def source(config=None, **kwargs):
    module = importlib.import_module("catchall_mail.rest_source")
    return module.RestSource(config or {"url": "https://mail.example.test/messages"}, **kwargs)


def test_pull_parses_explicit_raw_mime_contract_without_network():
    transport = Transport({"messages": [{"id": "opaque-1", "raw": RAW}]})
    messages = source(transport=transport).pull(limit=3)
    assert messages[0].recipient == "seat@example.test"
    assert messages[0].otp_codes == ["123456"]
    assert transport.calls == [("https://mail.example.test/messages", {
        "params": {"limit": 3}, "headers": {"Accept": "application/json"},
        "timeout": 30, "allow_redirects": False,
    })]
    assert transport.responses[0].closed


def test_pagination_follows_relative_next_and_caps_total_messages():
    transport = Transport(
        {"messages": [{"id": "one", "raw": RAW}], "next": "?cursor=opaque"},
        {"messages": [{"id": "two", "raw": RAW}, {"id": "three", "raw": RAW}],
         "next": "?cursor=unused"},
    )
    messages = source(transport=transport).pull(limit=2)
    assert len(messages) == 2
    assert len(transport.calls) == 2
    assert transport.calls[1][0] == "https://mail.example.test/messages?cursor=opaque"
    assert transport.calls[1][1]["params"] == {"limit": 1}
    assert all(response.closed for response in transport.responses)


@pytest.mark.parametrize("url", ["http://mail.example.test/messages", "https://user:secret@mail.example.test/messages", "https://mail.example.test/messages#secret", "not-a-url", "https://mail.example.test:bad/", "https://mail.example.test/\nsecret"])
def test_invalid_endpoint_is_rejected_before_transport(url):
    transport = Transport()
    with pytest.raises(ValueError, match="REST"):
        source({"url": url}, transport=transport).pull()
    assert transport.calls == []


@pytest.mark.parametrize("next_link", ["https://evil.example/messages", "//evil.example/messages", "http://mail.example.test/messages", "https://user:secret@mail.example.test/messages", "https://mail.example.test:444/messages"])
def test_unsafe_next_link_is_rejected_without_forwarding_auth(next_link):
    transport = Transport({"messages": [], "next": next_link})
    with pytest.raises(ValueError, match="REST"):
        source(transport=transport).pull()
    assert len(transport.calls) == 1


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "2", None])
def test_invalid_limit_is_rejected_before_transport(limit):
    transport = Transport()
    with pytest.raises(ValueError, match="REST limit"):
        source(transport=transport).pull(limit)
    assert transport.calls == []


@pytest.mark.parametrize("max_pages", [0, 101, True, "2"])
def test_invalid_page_bound_is_rejected_before_transport(max_pages):
    transport = Transport()
    with pytest.raises(ValueError, match="REST max_pages"):
        source({"url": "https://mail.example.test/messages", "max_pages": max_pages}, transport=transport).pull()
    assert transport.calls == []


def test_bearer_is_read_from_environment_only(capsys):
    transport = Transport({"messages": []})
    adapter = source({"url": "https://mail.example.test/messages", "bearer_env": "MAIL_TOKEN"},
                     environ={"MAIL_TOKEN": "private-token"}, transport=transport)
    adapter.pull()
    assert transport.calls[0][1]["headers"]["Authorization"] == "Bearer private-token"
    assert "private-token" not in repr(adapter)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", [None, "", "  ", "token\r\ninjected: value"])
def test_missing_or_unsafe_bearer_is_rejected_without_request(value):
    transport = Transport()
    with pytest.raises(ValueError, match="REST bearer") as caught:
        source({"url": "https://mail.example.test/messages", "bearer_env": "MAIL_TOKEN"},
               environ={"MAIL_TOKEN": value}, transport=transport).pull()
    assert transport.calls == []
    assert "injected" not in str(caught.value)


@pytest.mark.parametrize("payload", [None, [], {}, {"messages": {}}, {"messages": [None]}, {"messages": [{"raw": RAW}]}, {"messages": [{"id": 1, "raw": RAW}]}, {"messages": [{"id": "", "raw": RAW}]}, {"messages": [{"id": "x", "raw": None}]}, {"messages": [], "next": 1}])
def test_malformed_contract_is_sanitized(payload):
    transport = Transport(payload)
    with pytest.raises(ValueError, match="REST response"):
        source(transport=transport).pull()
    assert transport.responses[0].closed


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_non_success_status_is_sanitized_and_not_followed(status):
    transport = Transport({"messages": []})
    transport.responses[0].status_code = status
    with pytest.raises(ValueError, match="REST request"):
        source(transport=transport).pull()
    assert len(transport.calls) == 1
    assert transport.responses[0].closed


def test_transport_errors_do_not_expose_credentials_or_url(capsys):
    import requests
    def broken(*args, **kwargs):
        raise requests.ConnectionError("private-token https://secret.example/?key=private")
    with pytest.raises(ValueError, match="REST request") as caught:
        source(transport=broken).pull()
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__
    assert capsys.readouterr().out == ""


def test_invalid_json_is_sanitized_and_response_closed():
    transport = Transport(None)
    def broken():
        raise ValueError("private response body")
    transport.responses[0].json = broken
    with pytest.raises(ValueError, match="REST response") as caught:
        source(transport=transport).pull()
    assert "private" not in str(caught.value)
    assert transport.responses[0].closed


def test_metadata_preserves_stable_opaque_ids_without_faking_imap_uids():
    from dataclasses import FrozenInstanceError
    transport = Transport({"messages": [{"id": "opaque:/ 001", "raw": RAW}]})
    adapter = source(transport=transport)
    item = adapter.pull_with_metadata()[0]
    assert item.id == "opaque:/ 001"
    assert item.message.recipient == "seat@example.test"
    assert len(item.account_id) == 64
    assert "mail.example" not in item.account_id
    with pytest.raises(FrozenInstanceError):
        item.id = "changed"
    same = source(transport=Transport({"messages": [{"id": item.id, "raw": RAW}]})).pull_with_metadata()[0]
    other = source({"url": "https://other.example/messages"},
                   transport=Transport({"messages": [{"id": item.id, "raw": RAW}]})).pull_with_metadata()[0]
    assert same.account_id == item.account_id
    assert other.account_id != item.account_id
    assert not hasattr(item, "uidvalidity")


def test_control_characters_in_next_are_rejected_before_urljoin_normalizes_them():
    transport = Transport({"messages": [], "next": "\n?cursor=secret"})
    with pytest.raises(ValueError, match="REST"):
        source(transport=transport).pull()
    assert len(transport.calls) == 1


def test_pagination_has_a_configurable_hard_page_bound():
    transport = Transport({"messages": [], "next": "?page=2"})
    assert source({"url": "https://mail.example.test/messages", "max_pages": 1},
                  transport=transport).pull() == []
    assert len(transport.calls) == 1

