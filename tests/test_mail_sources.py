"""Offline IMAP connector contract tests."""
import imaplib

import pytest


RAW = b'To: relay@example.test\r\nX-Original-To: unit42@team.example.test\r\nFrom: sender@example.test\r\nSubject: Verification\r\n\r\nCode 123456\r\n'
SETTINGS = dict(provider="imap", host="imap.example.test", username_env="MAIL_USER", password_env="MAIL_PASSWORD", mailbox="Archive", port=993)


class FakeIMAP:
    def __init__(self, host, port, **kwargs):
        self.calls: list[tuple] = [("connect", host, port, kwargs)]
    def login(self, username, password):
        self.calls.append(("login", username, password))
        return "OK", []
    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [b"4"]
    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"11 12 13 14"]
        return "OK", [(b"1 (BODY[] {100}", RAW), b")"]
    def response(self, name):
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [b"101"]
    def logout(self):
        self.calls.append(("logout",))
        return "BYE", []


def test_imap_fetches_bounded_latest_uids_readonly_preserving_recipient():
    from catchall_mail.sources import ImapSource
    transport = FakeIMAP("unused", 0)
    source = ImapSource(SETTINGS, environ={"MAIL_USER": "mailbox-user", "MAIL_PASSWORD": "private-password"}, transport_factory=lambda *a, **kw: transport)
    messages = source.pull(limit=2)
    assert len(messages) == 2
    assert messages[0].recipient == "unit42@team.example.test"
    assert messages[0].otp_codes == ["123456"]
    assert ("select", "Archive", True) in transport.calls
    assert [call for call in transport.calls if call[0] == "fetch"] == [("fetch", b"13", "(BODY.PEEK[])"), ("fetch", b"14", "(BODY.PEEK[])")]
    assert transport.calls[-1] == ("logout",)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, 1001])
def test_imap_rejects_unbounded_or_invalid_limits_before_connecting(limit):
    from catchall_mail.sources import ImapSource
    source = ImapSource(SETTINGS, environ={"MAIL_USER": "u", "MAIL_PASSWORD": "p"}, transport_factory=lambda *a, **kw: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="limit"):
        source.pull(limit)


def test_imap_missing_environment_secret_is_actionable_before_connection():
    from catchall_mail.sources import ImapSource
    source = ImapSource(SETTINGS, environ={}, transport_factory=lambda *a, **kw: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="username_env"):
        source.pull()


@pytest.mark.parametrize("stage", ["login", "select", "search", "fetch"])
def test_imap_protocol_failure_is_sanitized_and_logs_out(stage):
    from catchall_mail.sources import ImapSource
    transport = FakeIMAP("unused", 0)
    if stage in ("login", "select"):
        setattr(transport, stage, lambda *a, **kw: ("NO", [b"secret-password"]))
    else:
        original = transport.uid
        transport.uid = lambda command, *a: ("NO", [b"secret-password"]) if command == stage else original(command, *a)
    source = ImapSource(SETTINGS, environ={"MAIL_USER": "u", "MAIL_PASSWORD": "p"}, transport_factory=lambda *a, **kw: transport)
    with pytest.raises(ValueError, match="IMAP") as error:
        source.pull()
    assert "secret-password" not in str(error.value)
    assert transport.calls[-1] == ("logout",)


def test_imap_empty_mailbox_uses_ssl_and_logs_out(monkeypatch):
    from catchall_mail.sources import ImapSource
    transport = FakeIMAP("unused", 0)
    transport.uid = lambda *a: ("OK", [b""])
    seen = []
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: seen.append((a, kw)) or transport)
    assert ImapSource(SETTINGS, environ={"MAIL_USER": "u", "MAIL_PASSWORD": "p"}).pull() == []
    assert seen == [(("imap.example.test", 993), {"timeout": 30})]
    assert transport.calls[-1] == ("logout",)


def test_imap_transport_exception_does_not_expose_credentials():
    from catchall_mail.sources import ImapSource
    def fail(*args, **kwargs):
        raise imaplib.IMAP4.error("server echoed secret-password")
    with pytest.raises(ValueError, match="IMAP") as error:
        ImapSource(SETTINGS, environ={"MAIL_USER": "u", "MAIL_PASSWORD": "p"}, transport_factory=fail).pull()
    assert "secret-password" not in str(error.value)

