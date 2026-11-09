"""Read-only IMAP SSL ingestion using environment-referenced credentials."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import imaplib
import json
import os

from .router import ParsedMail, parse_mime_message


@dataclass(frozen=True)
class ImapMessage:
    account_id: str
    uidvalidity: str
    uid: str
    message: ParsedMail


class ImapSource:
    """Fetch the latest bounded UID batch without marking messages as seen.

    Repeated pulls may return the same messages; no cursor or deduplication is
    implied. Transport injection allows offline testing without credentials.
    """

    def __init__(self, config, *, environ=None, transport_factory=None):
        self.config = dict(config)
        self.environ = os.environ if environ is None else environ
        self.transport_factory = transport_factory or imaplib.IMAP4_SSL

    def pull(self, limit: int = 50) -> list[ParsedMail]:
        return self._pull(limit, metadata=False)

    def pull_with_metadata(self, limit: int = 50) -> list[ImapMessage]:
        """Return mailbox-scoped UID identities; require server UIDVALIDITY."""
        return self._pull(limit, metadata=True)

    def _pull(self, limit, *, metadata):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("IMAP limit must be an integer between 1 and 1000")
        credentials = []
        for field in ("username_env", "password_env"):
            value = self.environ.get(self.config.get(field, ""))
            if not value or not value.strip():
                raise ValueError(f"IMAP credential environment variable for {field} is missing or empty")
            credentials.append(value)
        client = None
        try:
            client = self.transport_factory(self.config["host"], self.config.get("port", 993), timeout=30)
            self._checked(client.login(*credentials), "login")
            self._checked(client.select(self.config.get("mailbox", "INBOX"), readonly=True), "select")
            if metadata:
                try:
                    validity = client.response("UIDVALIDITY")[1]
                    uidvalidity = self._identity_number(validity[0], "UIDVALIDITY")
                except (AttributeError, IndexError, TypeError):
                    raise ValueError("IMAP UIDVALIDITY is missing or invalid") from None
                mailbox = self.config.get("mailbox", "INBOX")
                if mailbox.casefold() == "inbox":
                    mailbox = "INBOX"
                account_id = hashlib.sha256(json.dumps([
                    self.config["host"].lower(), self.config.get("port", 993),
                    credentials[0], mailbox,
                ], ensure_ascii=True).encode("utf-8")).hexdigest()
            data = self._checked(client.uid("search", None, "ALL"), "search")
            messages = []
            for uid in (data[0] or b"").split()[-limit:]:
                if metadata:
                    self._identity_number(uid, "UID")
                fetched = self._checked(client.uid("fetch", uid, "(BODY.PEEK[])"), "fetch")
                for item in fetched:
                    if isinstance(item, tuple) and isinstance(item[1], bytes):
                        parsed = parse_mime_message(item[1])
                        messages.append(ImapMessage(account_id, uidvalidity, uid.decode("ascii"), parsed)
                                        if metadata else parsed)
                        break
                else:
                    if metadata:
                        raise ValueError("IMAP fetch returned no message body; retry the pull")
            return messages
        except (imaplib.IMAP4.error, OSError):
            raise ValueError("IMAP connection or authentication failed; check mail settings and credentials") from None
        finally:
            if client is not None:
                try:
                    client.logout()
                except (imaplib.IMAP4.error, OSError):
                    pass

    @staticmethod
    def _identity_number(value, field):
        if (not isinstance(value, bytes) or not value.isdigit() or len(value) > 10
                or not 1 <= int(value) <= 4294967295):
            raise ValueError(f"IMAP {field} is missing or invalid")
        return str(int(value))

    @staticmethod
    def _checked(response, operation):
        status, data = response
        if status != "OK":
            raise ValueError(f"IMAP {operation} failed; check mail settings and permissions")
        return data
