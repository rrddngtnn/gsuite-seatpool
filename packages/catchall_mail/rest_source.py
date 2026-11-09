"""Read-only REST adapter for an explicit JSON-to-RFC5322 contract."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from urllib.parse import urljoin, urlsplit

import requests

from .router import ParsedMail, parse_mime_message


@dataclass(frozen=True)
class RestMessage:
    account_id: str
    id: str
    message: ParsedMail


class RestSource:
    """Pull from a server implementing this contract, not arbitrary mail APIs.

    Config: ``url`` is an HTTPS mailbox endpoint (no userinfo or fragment);
    optional ``bearer_env`` names an environment variable holding a bearer token;
    ``max_pages`` defaults to 10 and must be an integer from 1 through 100.
    The URL must identify one stable mailbox, even across token rotation.

    GET sends ``limit=<remaining>`` and expects HTTP 200 JSON of the form
    ``{"messages": [{"id": "stable opaque id", "raw": "RFC5322 MIME string"}],
    "next": "relative-or-absolute-url"}``. ``next`` may be absent, null, or empty
    to end pagination; otherwise it must stay on the original HTTPS origin.
    IDs must be nonblank strings, unique and stable within that endpoint.
    Unknown JSON fields are ignored; there is no vendor-specific field mapping.

    Pulls return at most ``limit`` messages (1..1000, default 50), in server order,
    and stop at ``max_pages`` even if more pages exist. Repeated pulls may repeat
    IDs; there is no persisted cursor or implicit deduplication. Metadata uses
    SHA256 of the exact configured URL as account_id, never the bearer token.
    REST IDs are not IMAP UIDs: do not pass these records to IMAP checkpoints.

    ``transport`` is a requests.get-compatible callable for offline injection.
    Redirects are disabled, each request has a 30-second timeout, and failures
    raise sanitized ValueError messages without response bodies or credentials.
    """
    def __init__(self, config, *, environ=None, transport=None):
        self.config = dict(config)
        self.environ = os.environ if environ is None else environ
        self.transport = transport or requests.get

    def pull(self, limit: int = 50) -> list[ParsedMail]:
        return [item.message for item in self.pull_with_metadata(limit)]

    def pull_with_metadata(self, limit: int = 50) -> list[RestMessage]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("REST limit must be an integer between 1 and 1000")
        max_pages = self.config.get("max_pages", 10)
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError("REST max_pages must be an integer between 1 and 100")
        url = self.config.get("url")
        origin = self._origin(url)
        account_id = hashlib.sha256(url.encode("utf-8")).hexdigest()
        headers = {"Accept": "application/json"}
        if "bearer_env" in self.config:
            token = self.environ.get(self.config["bearer_env"])
            if (not isinstance(token, str) or not token
                    or any(ord(char) <= 32 or ord(char) >= 127 for char in token)):
                raise ValueError("REST bearer environment variable is missing or invalid")
            headers["Authorization"] = "Bearer " + token
        messages = []
        for _ in range(max_pages):
            try:
                response = self.transport(
                    url, params={"limit": limit - len(messages)},
                    headers=headers, timeout=30, allow_redirects=False,
                )
            except (requests.RequestException, OSError, ValueError):
                raise ValueError("REST request failed; check endpoint and credentials") from None
            try:
                if response.status_code != 200:
                    raise ValueError("REST request failed; expected HTTP 200")
                try:
                    payload = response.json()
                    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
                        raise ValueError
                    next_link = payload.get("next")
                    if next_link is not None and (
                        not isinstance(next_link, str)
                        or any(ord(char) <= 32 for char in next_link)
                    ):
                        raise ValueError
                    for item in payload["messages"]:
                        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                                or not item["id"].strip() or not isinstance(item.get("raw"), str)):
                            raise ValueError
                    messages.extend(RestMessage(account_id, item["id"], parse_mime_message(item["raw"]))
                                    for item in payload["messages"][:limit - len(messages)])
                except (ValueError, TypeError, KeyError, LookupError):
                    raise ValueError("REST response does not match the MIME contract") from None
            finally:
                response.close()
            if len(messages) >= limit or not payload.get("next"):
                break
            url = urljoin(url, payload["next"])
            if self._origin(url) != origin:
                raise ValueError("REST pagination must stay on the configured HTTPS origin")
        return messages

    @staticmethod
    def _origin(url):
        try:
            if not isinstance(url, str) or any(ord(char) <= 32 for char in url):
                raise ValueError
            parts = urlsplit(url)
            if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.fragment):
                raise ValueError
            return parts.hostname.lower(), parts.port or 443
        except ValueError:
            raise ValueError("REST URL must be HTTPS without credentials or fragments") from None
