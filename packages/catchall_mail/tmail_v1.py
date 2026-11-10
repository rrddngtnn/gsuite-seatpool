"""Independent, read-only TMail v1 mailbox HTTP adapter."""
from dataclasses import dataclass
import hashlib
import json
import os
import re
import time
import datetime
from urllib.parse import urlsplit

import requests

from .checkpoints import ingest_tmail_page
from .router import ParsedMail, parse_mime_message


@dataclass(frozen=True)
class TMailMessage:
    account_id: str
    id: str
    message: ParsedMail
    received: str


class TMailV1Source:
    """GET /v1/messages and /v1/messages/{uuid}/raw with a scoped token.

    Requires url (HTTPS origin), mailbox (envelope address), and bearer_env.
    ingest() returns the newly inserted count, bounded by limit (1..1000) and
    max_pages scan segments (1..100, default 10). Each segment commits inbox
    rows, IDs, next cursor and pending metadata together. A partial page keeps
    its remainder; a completed sweep resets to the first page on the next call.
    No remote writes or TMail package/runtime dependency are used.
    """
    def __init__(self, config, *, environ=None, transport=None):
        self.config = dict(config)
        self.environ = os.environ if environ is None else environ
        self.transport = transport or requests.get

    @staticmethod
    def validate_config(config):
        try:
            url = config['url']
            if not isinstance(url, str) or any(ord(c) <= 32 or ord(c) >= 127 for c in url):
                raise ValueError
            parts = urlsplit(url)
            if (parts.scheme != 'https' or not parts.hostname or parts.username is not None
                    or parts.password is not None or parts.query or parts.fragment
                    or parts.path not in ('', '/') or parts.port == 0 or '\\' in url):
                raise ValueError
            if not re.fullmatch(r'[^@\s]+@[^@\s]+', config['mailbox']):
                raise ValueError
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', config['bearer_env']):
                raise ValueError
            pages = config.get('max_pages', 10)
            if type(pages) is not int or not 1 <= pages <= 100:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ValueError('TMail requires an HTTPS origin, mailbox, bearer_env and max_pages 1..100') from None

    def _page(self, cursor):
        try:
            payload = json.loads(self._read('/v1/messages', {'cursor': cursor} if cursor else {}))
            if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list) or len(payload['messages']) > 100:
                raise ValueError
            next_cursor = payload['cursor']
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096 or any(ord(c) < 32 for c in next_cursor)):
                raise ValueError
            if cursor is not None and next_cursor == cursor:
                raise ValueError
            ids = set()
            for item in payload['messages']:
                if not isinstance(item, dict) or not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}', item['id']):
                    raise ValueError
                if item['id'] in ids or type(item['size']) is not int or not 0 <= item['size'] <= 26214400:
                    raise ValueError
                ids.add(item['id'])
                envelope = item['envelope']
                if not isinstance(envelope, dict) or not isinstance(envelope.get('to'), str) or envelope['to'].lower() != self.config['mailbox'].lower():
                    raise ValueError
                if not isinstance(envelope['from'], str) or any(ord(c) < 32 for c in envelope['from']):
                    raise ValueError
                received = envelope['received']
                if not isinstance(received, str) or not received.endswith('Z'):
                    raise ValueError
                datetime.datetime.fromisoformat(received[:-1] + '+00:00')
            return payload
        except (KeyError, ValueError, TypeError):
            raise ValueError('TMail response does not match the scoped v1 contract') from None

    def _read(self, path, params=None):
        response = None
        started = time.monotonic()
        try:
            response = self.transport(self.config['url'].rstrip('/') + path, params=params,
                                      headers={'Authorization': 'Bearer ' + self.environ[self.config['bearer_env']]},
                                      timeout=30, allow_redirects=False, stream=True)
            if response.status_code != 200:
                raise ValueError
            bound = 1048576 if path == '/v1/messages' else 26214400
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=65536):
                size += len(chunk)
                if size > bound or time.monotonic() - started > 30:
                    raise ValueError
                chunks.append(chunk)
            return b''.join(chunks)
        except (requests.RequestException, OSError, ValueError):
            raise ValueError('TMail request failed or exceeded response bounds') from None
        finally:
            if response is not None:
                response.close()

    def ingest(self, db, limit=50):
        self.validate_config(self.config)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('TMail limit must be an integer from 1 to 1000')
        token = self.environ.get(self.config['bearer_env'])
        if not isinstance(token, str) or not token or any(ord(c) <= 32 or ord(c) >= 127 for c in token):
            raise ValueError('TMail bearer environment variable is missing or invalid')
        origin = urlsplit(self.config['url'])
        account = hashlib.sha256(json.dumps([origin.hostname.lower(), origin.port or 443, self.config['mailbox'].lower()]).encode()).hexdigest()
        db.execute('CREATE TABLE IF NOT EXISTS tmail_scans (account_id TEXT PRIMARY KEY, cursor TEXT, pending TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS tmail_checkpoints (account_id TEXT, id TEXT, PRIMARY KEY(account_id, id))')
        total = 0
        for _ in range(self.config.get('max_pages', 10)):
            row = db.execute('SELECT cursor, pending FROM tmail_scans WHERE account_id=?', (account,)).fetchone()
            cursor, pending = (row[0], json.loads(row[1])) if row else (None, [])
            if not pending:
                payload = self._page(cursor)
                pending, cursor = payload['messages'], payload['cursor']
            messages = []
            consumed = 0
            for item in pending:
                consumed += 1
                if db.execute('SELECT 1 FROM tmail_checkpoints WHERE account_id=? AND id=?', (account, item['id'])).fetchone():
                    continue
                raw = self._read('/v1/messages/' + item['id'] + '/raw')
                if len(raw) != item['size']:
                    raise ValueError('TMail raw message size mismatch')
                try:
                    parsed = parse_mime_message(raw, envelope_rcpt=item['envelope']['to'])
                except (LookupError, ValueError, TypeError):
                    raise ValueError('TMail MIME decoding failed; page remains retryable') from None
                messages.append(TMailMessage(account, item['id'], parsed, item['envelope']['received']))
                if total + len(messages) >= limit:
                    break
            remaining = pending[consumed:]
            ingest_tmail_page(db, account, messages, cursor, remaining)
            total += len(messages)
            if total >= limit or (not remaining and cursor is None):
                break
        return total
