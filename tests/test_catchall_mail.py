"""Tests for catch-all RFC5322 MIME parsing, OTP/URL extraction, and inbox polling."""
from __future__ import annotations

from catchall_mail.router import (
    CatchallMailbox,
    extract_google_site_token,
    extract_otp_codes,
    extract_verification_urls,
    parse_mime_message,
)

SAMPLE_MIME = """From: Google Workspace <no-reply@google.com>
To: unit01@alpha.example.test
Subject: Your verification code is 849201
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="b1"

--b1
Content-Type: text/plain; charset="utf-8"

Use code 849201 to verify your account, or visit:
https://admin.google.com/ac/signup/verify?token=XYZ987654321
DNS token: google-site-verification=rAtMaN4080_VeRiFy_ToKeN_1234567890
--b1--
"""


def test_parse_mime_and_extract_tokens():
    parsed = parse_mime_message(SAMPLE_MIME)
    assert parsed.recipient == "unit01@alpha.example.test"
    assert parsed.sender == "no-reply@google.com"
    assert "849201" in parsed.otp_codes
    assert "https://admin.google.com/ac/signup/verify?token=XYZ987654321" in parsed.urls
    assert (
        extract_google_site_token(parsed.body_text)
        == "google-site-verification=rAtMaN4080_VeRiFy_ToKeN_1234567890"
    )


def test_mailbox_sqlite_persistence_and_wait(db):
    mb = CatchallMailbox(db=db)
    mb.ingest_raw(SAMPLE_MIME)
    msgs = mb.messages_for(address="unit01@alpha.example.test")
    assert len(msgs) == 1
    assert msgs[0].otp_codes == ["849201"]

    code, msg = mb.wait_for_pattern("unit01@alpha.example.test", r"\b(\d{6})\b", timeout=0.2)
    assert code == "849201"
    assert msg is not None


def test_mailbox_wait_timeout_when_empty(db):
    mb = CatchallMailbox(db=db)
    code, msg = mb.wait_for_pattern("missing@alpha.example.test", timeout=0.1, poll=0.02)
    assert code is None and msg is None


def test_extract_helpers_deduplicate():
    assert extract_otp_codes("Code 123456 and again 123456") == ["123456"]
    assert extract_verification_urls("Click https://a.test/v?x=1.") == ["https://a.test/v?x=1"]
