"""Offline TMail v1 contract and durable scan tests."""
import importlib
import json

import pytest

CONFIG = {"provider": "tmail_v1", "url": "https://mail.example.test", "mailbox": "seat@example.test", "bearer_env": "MAIL_TOKEN", "max_pages": 1}
RAW = b'To: victim@example.test\r\nX-Original-To: victim@example.test\r\nDelivered-To: victim@example.test\r\nFrom: sender@example.test\r\nSubject: Code\r\nContent-Type: text/plain; charset=iso-8859-1\r\n\r\ncaf\xe9 123456'


def ident(n):
    return f'00000000-0000-4000-8000-{n:012d}'


def item(n, raw=RAW, recipient="seat@example.test"):
    return {"id": ident(n), "size": len(raw), "envelope": {"from": "sender@example.test", "to": recipient, "received": "2026-01-01T00:00:00Z"}}


class Response:
    def __init__(self, body, status=200):
        self.body = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def close(self):
        self.closed = True


class Transport:
    def __init__(self, *responses):
        self.responses = [r if isinstance(r, Response) else Response(r) for r in responses]
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[len(self.calls) - 1]


def source(transport, **overrides):
    cls = getattr(importlib.import_module('catchall_mail.tmail_v1'), 'TMailV1Source')
    return cls(dict(CONFIG, **overrides), environ={"MAIL_TOKEN": "private-token"}, transport=transport)


def test_ingest_uses_bytes_and_trusted_envelope_not_spoofed_headers(db):
    transport = Transport({"messages": [item(1)], "cursor": None}, RAW)
    assert source(transport).ingest(db, limit=1) == 1
    row = db.execute('SELECT * FROM inbox_messages').fetchone()
    assert row['recipient'] == 'seat@example.test'
    assert row['body_text'] == 'café 123456'
    assert json.loads(row['otp_json']) == ['123456']
    assert [c[0] for c in transport.calls] == ['https://mail.example.test/v1/messages', f'https://mail.example.test/v1/messages/{ident(1)}/raw']
    for _, kwargs in transport.calls:
        assert kwargs['allow_redirects'] is False
        assert kwargs['timeout'] == 30
        assert kwargs['stream'] is True
        assert kwargs['headers']['Authorization'] == 'Bearer private-token'
    assert all(r.closed for r in transport.responses)


def test_bounded_pulls_resume_pages_skip_seen_ids_and_reset_sweep(db):
    first = Transport({'messages': [item(1)], 'cursor': 'opaque+/='}, RAW)
    assert source(first).ingest(db, limit=1) == 1
    second = Transport({'messages': [item(1), item(2)], 'cursor': 'last'}, RAW)
    assert source(second).ingest(db, limit=1) == 1
    assert second.calls[0][1]['params'] == {'cursor': 'opaque+/='}
    third = Transport({'messages': [item(3)], 'cursor': None}, RAW)
    assert source(third).ingest(db, limit=1) == 1
    assert third.calls[0][1]['params'] == {'cursor': 'last'}
    again = Transport({'messages': [item(1)], 'cursor': None})
    assert source(again).ingest(db, limit=1) == 0
    assert again.calls[0][1]['params'] == {}
    assert len(again.calls) == 1
    assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 3


def test_partial_page_retains_pending_records_across_new_source_instances(db):
    first = Transport({'messages': [item(1), item(2)], 'cursor': 'next'}, RAW)
    assert source(first).ingest(db, limit=1) == 1
    second = Transport(RAW)
    assert source(second).ingest(db, limit=1) == 1
    assert second.calls[0][0].endswith(ident(2) + '/raw')
    third = Transport({'messages': [], 'cursor': None})
    assert source(third).ingest(db, limit=1) == 0
    assert third.calls[0][1]['params'] == {'cursor': 'next'}


def test_scan_survives_database_reopen_and_token_rotation(tmp_path):
    from seat_allocator.store import open_db
    path = str(tmp_path / 'resume.db')
    db = open_db(path)
    source(Transport({'messages': [item(1), item(2)], 'cursor': 'next'}, RAW)).ingest(db, limit=1)
    db.close()
    db = open_db(path)
    try:
        adapter = source(Transport(RAW), url='https://MAIL.example.test/', mailbox='SEAT@example.test')
        adapter.environ['MAIL_TOKEN'] = 'rotated-private-token'
        assert adapter.ingest(db, limit=1) == 1
        assert db.execute('SELECT COUNT(*) FROM tmail_scans').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 2
    finally:
        db.close()


def test_raw_body_bound_closes_response_and_leaves_page_retryable(db):
    raw = Response(b'x' * (26214400 + 1))
    with pytest.raises(ValueError, match='TMail'):
        source(Transport({'messages': [item(1)], 'cursor': None}, raw)).ingest(db)
    assert raw.closed
    assert db.execute('SELECT COUNT(*) FROM tmail_checkpoints').fetchone()[0] == 0



@pytest.mark.parametrize('updates', [
    {'url': 'http://mail.example.test'}, {'url': 'https://user:private@mail.example.test'},
    {'url': 'https://mail.example.test?private=1'}, {'url': 'https://mail.example.test/#private'},
    {'url': 'https://mail.example.test/\\nprivate'}, {'mailbox': ''}, {'mailbox': 'bad'},
    {'bearer_env': ''}, {'max_pages': True}, {'max_pages': 0},
])
def test_invalid_config_rejected_before_request(db, updates):
    transport = Transport()
    with pytest.raises(ValueError, match='TMail'):
        source(transport, **updates).ingest(db)
    assert not transport.calls


@pytest.mark.parametrize('limit', [0, -1, 1001, True, '1'])
def test_invalid_limit_rejected_before_request(db, limit):
    transport = Transport()
    with pytest.raises(ValueError, match='TMail'):
        source(transport).ingest(db, limit)
    assert not transport.calls


@pytest.mark.parametrize('token', [None, '', 'private\r\nInjected: secret', 'not ascii é'])
def test_unsafe_bearer_rejected(db, token):
    transport = Transport()
    adapter = source(transport)
    adapter.environ['MAIL_TOKEN'] = token
    with pytest.raises(ValueError, match='TMail') as caught:
        adapter.ingest(db)
    assert 'private' not in str(caught.value)
    assert not transport.calls


@pytest.mark.parametrize('response', [
    Response({'messages': [], 'cursor': None}, 302),
    Response({'messages': [], 'cursor': None}, 401),
    Response(b'private-token not JSON'),
    Response({'messages': [], 'cursor': 123}),
    Response({'messages': [], 'cursor': ''}),
    Response({'messages': []}), Response({'messages': [None], 'cursor': None}),
    Response({'messages': [dict(item(1), id='../private')], 'cursor': None}),
    Response({'messages': [dict(item(1), size=True)], 'cursor': None}),
    Response({'messages': [item(1), item(1)], 'cursor': None}),
    Response({'messages': [item(1, recipient='victim@example.test')], 'cursor': None}),
    Response({'messages': [dict(item(1), envelope={'to': None})], 'cursor': None}),
])
def test_malformed_or_unscoped_list_is_secret_safe_and_never_fetches_raw(db, response):
    transport = Transport(response)
    with pytest.raises(ValueError, match='TMail') as caught:
        source(transport).ingest(db)
    assert 'private' not in str(caught.value)
    assert len(transport.calls) == 1
    assert response.closed
    assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 0


def test_oversized_list_stops_stream_and_closes_response(db):
    response = Response(b'x' * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match='TMail'):
        source(Transport(response)).ingest(db)
    assert response.closed


def test_request_failure_is_sanitized(db):
    import requests
    def broken(*args, **kwargs):
        raise requests.ConnectionError('private-token secret URL')
    with pytest.raises(ValueError, match='TMail') as caught:
        source(broken).ingest(db)
    assert 'private' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_page_database_failure_rolls_back_ids_and_cursor_then_retry_progresses(db):
    import sqlite3
    db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON inbox_messages WHEN (SELECT COUNT(*) FROM inbox_messages)=1 BEGIN SELECT RAISE(ABORT, 'failure'); END")
    page = {'messages': [item(1), item(2)], 'cursor': 'next'}
    with pytest.raises(sqlite3.IntegrityError):
        source(Transport(page, RAW, RAW)).ingest(db, limit=2)
    assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM tmail_checkpoints').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM tmail_scans').fetchone()[0] == 0
    db.execute('DROP TRIGGER fail_second')
    assert source(Transport(page, RAW, RAW)).ingest(db, limit=2) == 2
    next_page = Transport({'messages': [item(3)], 'cursor': None}, RAW)
    assert source(next_page).ingest(db, limit=1) == 1
    assert next_page.calls[0][1]['params'] == {'cursor': 'next'}


def test_later_page_transport_failure_preserves_committed_page_and_resume(db):
    import requests
    first = Transport({'messages': [item(1)], 'cursor': 'next'}, RAW)
    def fail_next(url, **kwargs):
        if kwargs.get('params') == {'cursor': 'next'}:
            raise requests.Timeout('private-token')
        return first(url, **kwargs)
    with pytest.raises(ValueError, match='TMail'):
        source(fail_next, max_pages=2).ingest(db, limit=3)
    assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 1
    retry = Transport({'messages': [item(2)], 'cursor': None}, RAW)
    assert source(retry).ingest(db) == 1
    assert retry.calls[0][1]['params'] == {'cursor': 'next'}


@pytest.mark.parametrize('raw', [b'Content-Type: text/plain; charset=x-private-unknown\r\n\r\nbody', b'Subject: =?x-private-unknown?b?YQ==?=\r\n\r\nbody'])
def test_unknown_mime_charset_is_explicit_sanitized_failure_and_retryable(db, raw):
    page = {'messages': [item(1, raw)], 'cursor': 'next'}
    with pytest.raises(ValueError, match='TMail MIME') as caught:
        source(Transport(page, raw)).ingest(db)
    assert 'private' not in str(caught.value)
    assert db.execute('SELECT COUNT(*) FROM tmail_checkpoints').fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM tmail_scans').fetchone()[0] == 0


def test_raw_size_mismatch_rejected_without_checkpoint(db):
    with pytest.raises(ValueError, match='TMail'):
        source(Transport({'messages': [item(1)], 'cursor': None}, b'short')).ingest(db)
    assert db.execute('SELECT COUNT(*) FROM inbox_messages').fetchone()[0] == 0


def test_received_timestamp_from_envelope_is_persisted(db):
    source(Transport({'messages': [item(1)], 'cursor': None}, RAW)).ingest(db)
    assert db.execute('SELECT received_at FROM inbox_messages').fetchone()[0] == '2026-01-01T00:00:00Z'


@pytest.mark.parametrize('envelope', [
    {'to': 'seat@example.test'},
    {'to': 'seat@example.test', 'from': [], 'received': '2026-01-01T00:00:00Z'},
    {'to': 'seat@example.test', 'from': 'sender@example.test', 'received': 'private'},
    {'to': 'seat@example.test', 'from': 'sender@example.test', 'received': None},
])
def test_missing_or_invalid_envelope_metadata_is_sanitized_before_raw_fetch(db, envelope):
    transport = Transport({'messages': [dict(item(1), envelope=envelope)], 'cursor': None})
    with pytest.raises(ValueError, match='TMail response'):
        source(transport).ingest(db)
    assert len(transport.calls) == 1


def test_stream_read_timeout_is_secret_safe_and_closed(db):
    import requests
    response = Response(b'')
    def broken(chunk_size):
        raise requests.Timeout('private-token')
        yield b''
    response.iter_content = broken
    with pytest.raises(ValueError, match='TMail') as caught:
        source(Transport(response)).ingest(db)
    assert 'private' not in str(caught.value)
    assert response.closed


def test_total_response_deadline_stops_slow_drip_stream(db, monkeypatch):
    ticks = iter([0, 31])
    monkeypatch.setattr('catchall_mail.tmail_v1.time.monotonic', lambda: next(ticks))
    response = Response({'messages': [], 'cursor': None})
    with pytest.raises(ValueError, match='TMail'):
        source(Transport(response)).ingest(db)
    assert response.closed


def test_repeated_cursor_rejected_without_advancing_checkpoint(db):
    source(Transport({'messages': [], 'cursor': 'same'})).ingest(db)
    with pytest.raises(ValueError, match='TMail response'):
        source(Transport({'messages': [], 'cursor': 'same'})).ingest(db)
    assert db.execute('SELECT cursor FROM tmail_scans').fetchone()[0] == 'same'



def test_optional_config_and_cli_tmail_provider(tmp_path, monkeypatch, capsys):
    from gsuite_seatpool.config import load_config
    from gsuite_seatpool import cli
    from types import SimpleNamespace
    path = tmp_path / 'seatpool.toml'
    path.write_text('[mail]\nprovider="tmail_v1"\nurl="https://mail.example.test"\nmailbox="seat@example.test"\nbearer_env="MAIL_TOKEN"\nmax_pages=1\n')
    settings = load_config(path, environ={})
    transport = Transport({'messages': [item(1)], 'cursor': None}, RAW)
    monkeypatch.setenv('MAIL_TOKEN', 'private-token')
    monkeypatch.setattr('catchall_mail.tmail_v1.requests.get', transport)
    args = SimpleNamespace(settings=settings, db=str(tmp_path / 'inbox.db'), limit=1)
    assert cli.cmd_inbox_pull(args) == 0
    assert json.loads(capsys.readouterr().out) == {'ingested': 1}


@pytest.mark.parametrize('updates', [{'url': 'http://bad.test'}, {'mailbox': 'bad'}, {'bearer_env': ''}, {'token': 'private-token'}])
def test_tmail_config_invalid_values_fail_early(updates):
    from gsuite_seatpool.config import load_config
    with pytest.raises(ValueError):
        load_config(environ={}, overrides={'mail': dict(CONFIG, **updates)})
