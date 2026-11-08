"""Catch-all RFC5322 MIME parser and OTP / verification link extractor."""
from .router import (
    CatchallMailbox,
    ParsedMail,
    extract_otp_codes,
    extract_verification_urls,
    parse_mime_message,
)

__all__ = [
    "CatchallMailbox",
    "ParsedMail",
    "extract_otp_codes",
    "extract_verification_urls",
    "parse_mime_message",
]
