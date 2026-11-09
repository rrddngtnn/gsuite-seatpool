"""Offline persistence contracts for UID-scoped IMAP ingestion."""
import json
import sqlite3
from types import SimpleNamespace

import imaplib
import pytest

from catchall_mail.sources import ImapSource
from gsuite_seatpool.cli import cmd_inbox_pull
from seat_allocator.store import open_db
from test_mail_sources import FakeIMAP, SETTINGS


class IdentityIMAP(FakeIMAP):
    validity = b"101"

    def response(self, code):
        assert code == "UIDVALIDITY"
        return code, [self.validity]


def setup_pull(tmp_path, monkeypatch, transport=None, **settings):
    transport = transport or IdentityIMAP("unused", 0)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: transport)
    monkeypatch.setenv("MAIL_USER", "private-mailbox-user")
    monkeypatch.setenv("MAIL_PASSWORD", "private-password")
    args = SimpleNamespace(db=str(tmp_path / "mail.db"), limit=2,
                           settings={"mail": dict(SETTINGS, **settings)})
    return args, transport


def count_messages(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0]


def test_repeat_cli_pull_is_persistently_deduplicated(tmp_path, monkeypatch, capsys):
    args, transport = setup_pull(tmp_path, monkeypatch)
    assert cmd_inbox_pull(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ingested": 2}
    assert cmd_inbox_pull(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ingested": 0}
    assert count_messages(args.db) == 2
    assert "private-password" not in captured.out + captured.err
    assert "private-mailbox-user" not in captured.out + captured.err
    assert transport.calls[-1] == ("logout",)


@pytest.mark.parametrize("validity", [None, b"", b"0", b"not-a-uid", b"4294967296"])
def test_metadata_requires_valid_uidvalidity_without_secret_errors(tmp_path, monkeypatch, validity):
    args, transport = setup_pull(tmp_path, monkeypatch)
    transport.validity = validity
    with pytest.raises(ValueError, match="UIDVALIDITY") as error:
        cmd_inbox_pull(args)
    assert "private" not in str(error.value)
    assert not (tmp_path / "mail.db").exists()
    assert transport.calls[-1] == ("logout",)

@pytest.mark.parametrize("change", ["validity", "username", "mailbox", "host", "port"])
def test_identity_namespaces_do_not_hide_distinct_mail(tmp_path, monkeypatch, capsys, change):
    args, transport = setup_pull(tmp_path, monkeypatch)
    cmd_inbox_pull(args)
    if change == "validity":
        transport.validity = b"102"
    elif change == "username":
        monkeypatch.setenv("MAIL_USER", "another-account")
    else:
        args.settings["mail"][change] = {"mailbox": "Other", "host": "other.example.test", "port": 1993}[change]
    cmd_inbox_pull(args)
    assert count_messages(args.db) == 4
    cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"ingested": 0}


def test_password_rotation_keeps_account_identity(tmp_path, monkeypatch, capsys):
    args, _ = setup_pull(tmp_path, monkeypatch)
    cmd_inbox_pull(args)
    monkeypatch.setenv("MAIL_PASSWORD", "rotated-password")
    cmd_inbox_pull(args)
    assert count_messages(args.db) == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {"ingested": 0}
    with sqlite3.connect(args.db) as db:
        values = repr(db.execute("SELECT * FROM imap_checkpoints").fetchall())
        assert "private" not in values
        assert "rotated-password" not in values


def test_database_failure_rolls_back_inbox_and_checkpoints_then_resumes(tmp_path, monkeypatch):
    args, _ = setup_pull(tmp_path, monkeypatch)
    with open_db(args.db) as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON inbox_messages "
                   "WHEN (SELECT COUNT(*) FROM inbox_messages) = 1 "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        cmd_inbox_pull(args)
    with sqlite3.connect(args.db) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM imap_checkpoints").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_second")
    cmd_inbox_pull(args)
    assert count_messages(args.db) == 2
    cmd_inbox_pull(args)
    assert count_messages(args.db) == 2


def test_fetch_failure_keeps_prior_checkpoint_and_retry_ingests_once(tmp_path, monkeypatch):
    args, transport = setup_pull(tmp_path, monkeypatch)
    args.limit = 1
    cmd_inbox_pull(args)
    args.limit = 2
    original = transport.uid
    def fail(command, *values):
        if command == "fetch" and values[0] == b"14":
            raise imaplib.IMAP4.error("private-password")
        return original(command, *values)
    transport.uid = fail
    with pytest.raises(ValueError) as error:
        cmd_inbox_pull(args)
    assert "private-password" not in str(error.value)
    assert count_messages(args.db) == 1
    transport.uid = original
    cmd_inbox_pull(args)
    assert count_messages(args.db) == 2


def test_incomplete_fetch_fails_without_checkpointing_batch(tmp_path, monkeypatch):
    args, transport = setup_pull(tmp_path, monkeypatch)
    original = transport.uid
    transport.uid = lambda command, *values: ("OK", [None]) if command == "fetch" else original(command, *values)
    with pytest.raises(ValueError, match="fetch"):
        cmd_inbox_pull(args)
    assert not (tmp_path / "mail.db").exists()


@pytest.mark.parametrize("uid", [b"0", b"private-password", b"4294967296"])
def test_invalid_search_uid_is_rejected_before_ingestion(tmp_path, monkeypatch, uid):
    args, transport = setup_pull(tmp_path, monkeypatch)
    original = transport.uid
    transport.uid = lambda command, *values: ("OK", [uid]) if command == "search" else original(command, *values)
    with pytest.raises(ValueError, match="UID") as error:
        cmd_inbox_pull(args)
    assert "private-password" not in str(error.value)
