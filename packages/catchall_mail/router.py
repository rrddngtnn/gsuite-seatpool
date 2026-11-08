"""RFC5322 MIME parsing, OTP/token extraction, and catch-all inbox polling."""
from __future__ import annotations

from dataclasses import dataclass, field
import email
from email.header import decode_header
from email.utils import parseaddr
import re
import time


@dataclass
class ParsedMail:
    recipient: str
    sender: str
    subject: str
    body_text: str
    otp_codes: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)


OTP_PATTERN = re.compile(r"\b(\d{6})\b")
URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+")
GOOGLE_VERIFY_TOKEN_PATTERN = re.compile(r"(google-site-verification=[A-Za-z0-9_-]{20,})")


def _decode_str(val: str | None) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(chunk))
    return "".join(decoded)


def extract_otp_codes(text: str) -> list[str]:
    seen = []
    for m in OTP_PATTERN.findall(text or ""):
        if m not in seen:
            seen.append(m)
    return seen


def extract_verification_urls(text: str) -> list[str]:
    seen = []
    for u in URL_PATTERN.findall(text or ""):
        cleaned = u.rstrip(".,;)")
        if cleaned not in seen:
            seen.append(cleaned)
    return seen


def extract_google_site_token(text: str) -> str | None:
    m = GOOGLE_VERIFY_TOKEN_PATTERN.search(text or "")
    return m.group(1) if m else None


def parse_mime_message(raw: str | bytes, envelope_rcpt: str | None = None) -> ParsedMail:
    msg = (
        email.message_from_bytes(raw)
        if isinstance(raw, bytes)
        else email.message_from_string(raw)
    )
    rcpt = (
        envelope_rcpt
        or msg.get("X-Original-To")
        or msg.get("Delivered-To")
        or parseaddr(msg.get("To", ""))[1]
    )
    rcpt = (rcpt or "").strip().lower()
    sender = parseaddr(msg.get("From", ""))[1].strip().lower()
    subject = _decode_str(msg.get("Subject", ""))

    body_parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if ctype in ("text/plain", "text/html") and "attachment" not in disp:
                payload = part.get_payload(decode=True)
                if payload is not None:
                    charset = part.get_content_charset() or "utf-8"
                    body_parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            body_parts.append(payload.decode(charset, errors="replace"))
        else:
            body_parts.append(str(msg.get_payload() or ""))

    combined = "\n".join(body_parts)
    search_blob = f"{subject}\n{combined}"
    return ParsedMail(
        recipient=rcpt,
        sender=sender,
        subject=subject,
        body_text=combined,
        otp_codes=extract_otp_codes(search_blob),
        urls=extract_verification_urls(search_blob),
    )


class CatchallMailbox:
    """In-memory and SQLite-backed catch-all inbox router without post-signup timestamp truncation."""

    def __init__(self, db=None):
        self.db = db
        self._mem: list[ParsedMail] = []

    def ingest_raw(self, raw: str | bytes, envelope_rcpt: str | None = None) -> ParsedMail:
        parsed = parse_mime_message(raw, envelope_rcpt=envelope_rcpt)
        self._mem.append(parsed)
        if self.db is not None:
            from seat_allocator.store import store_inbox_message
            store_inbox_message(self.db, parsed)
        return parsed

    def messages_for(self, address: str | None = None, domain: str | None = None) -> list[ParsedMail]:
        addr = address.strip().lower() if address else None
        dom = domain.strip().lower() if domain else None
        if self.db is not None:
            from seat_allocator.store import list_inbox_messages
            return list_inbox_messages(self.db, recipient=addr, domain=dom)
        out = []
        for m in self._mem:
            if addr and m.recipient != addr:
                continue
            if dom and not m.recipient.endswith("@" + dom):
                continue
            out.append(m)
        return out

    def wait_for_pattern(
        self,
        address: str,
        pattern: str = r"\b(\d{6})\b",
        timeout: float = 5.0,
        poll: float = 0.05,
        sleep=None,
    ) -> tuple[str | None, ParsedMail | None]:
        """Poll all messages for recipient without a post-signup timestamp cutoff."""
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while True:
            for msg in self.messages_for(address=address):
                blob = f"{msg.subject}\n{msg.body_text}"
                m = rx.search(blob)
                if m:
                    return (m.group(1) if m.groups() else m.group(0)), msg
            if time.monotonic() >= deadline:
                return None, None
            (sleep or time.sleep)(poll)
