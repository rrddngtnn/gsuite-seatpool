"""Offline REST CLI/configuration and durable ingestion contracts."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from gsuite_seatpool import cli, config
from seat_allocator.store import open_db
from test_rest_source import RAW, Response


SETTINGS = {"provider": "rest", "url": "https://mail.example.test/messages",
            "bearer_env": "REST_MAIL_TOKEN", "max_pages": 2}


def test_rest_config_and_cli_dispatch_persist_dedup(tmp_path, monkeypatch, capsys):
    path = tmp_path / "rest.toml"
    path.write_text('[mail]\nprovider="rest"\nurl="https://mail.example.test/messages"\nbearer_env="REST_MAIL_TOKEN"\nmax_pages=2\n')
    assert config.load_config(path, environ={})["mail"] == SETTINGS
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response({"messages": [{"id": "opaque:/ 001", "raw": RAW}]})

    monkeypatch.setattr("catchall_mail.rest_source.requests.get", get)
    monkeypatch.setenv("REST_MAIL_TOKEN", "private-token")
    db_path = str(tmp_path / "rest.db")
    args = ["--config", str(path), "--db", db_path, "inbox-pull", "--limit", "3"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ingested": 1}
    monkeypatch.setenv("REST_MAIL_TOKEN", "rotated-secret")
    assert cli.main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ingested": 0}
    assert captured.err == ""
    assert len(calls) == 2
    assert calls[0][1]["params"] == {"limit": 3}
    assert calls[1][1]["headers"]["Authorization"] == "Bearer rotated-secret"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 1
        rows = db.execute("SELECT account_id,id FROM rest_checkpoints").fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "opaque:/ 001"
        assert "private-token" not in repr(rows) + captured.out
        assert "rotated-secret" not in repr(rows) + captured.out


@pytest.mark.parametrize("changes", [
    {"url": "http://mail.example.test/messages"},
    {"url": "https://user:private-token@mail.example.test/messages"},
    {"url": "https://mail.example.test/messages#private-token"},
    {"url": "https://mail.example.test:bad/messages"},
    {"url": ""}, {"url": None}, {"max_pages": 0}, {"max_pages": 101},
    {"max_pages": True}, {"max_pages": "2"}, {"bearer_env": ""},
    {"bearer_env": 123}, {"bearer": "private-token"},
    {"host": "imap.example.test"}, {"provider": "vendor"},
])
def test_rest_config_validation_sanitizes_errors(changes):
    with pytest.raises(ValueError) as caught:
        config.load_config(environ={}, overrides={"mail": dict(SETTINGS, **changes)})
    assert "private-token" not in str(caught.value)


def setup_pull(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("REST_MAIL_TOKEN", "private-token")
    monkeypatch.setattr("catchall_mail.rest_source.requests.get", lambda *a, **kw: Response(payload))
    return SimpleNamespace(db=str(tmp_path / "mail.db"), limit=10,
                           settings={"mail": dict(SETTINGS)})


def test_rest_checkpoint_namespace_preserves_ids_and_distinct_accounts(tmp_path, monkeypatch, capsys):
    args = setup_pull(tmp_path, monkeypatch, {"messages": [
        {"id": "001", "raw": RAW}, {"id": "1", "raw": RAW},
        {"id": "001", "raw": RAW},
    ]})
    assert cli.cmd_inbox_pull(args) == 0
    assert json.loads(capsys.readouterr().out) == {"ingested": 2}
    args.settings["mail"]["url"] = "https://mail.example.test/other-mailbox"
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 2}
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 0}
    with sqlite3.connect(args.db) as db:
        assert db.execute("SELECT COUNT(*) FROM rest_checkpoints").fetchone()[0] == 4
        assert db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 4
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='imap_checkpoints'").fetchall()


def test_rest_failed_insert_rolls_back_whole_batch_then_retry_dedups(tmp_path, monkeypatch, capsys):
    args = setup_pull(tmp_path, monkeypatch, {"messages": [
        {"id": "one", "raw": RAW}, {"id": "two", "raw": RAW},
    ]})
    with open_db(args.db) as db:
        db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON inbox_messages "
                   "WHEN (SELECT COUNT(*) FROM inbox_messages) = 1 "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        cli.cmd_inbox_pull(args)
    assert capsys.readouterr().out == ""
    with sqlite3.connect(args.db) as db:
        assert db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM rest_checkpoints").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_second")
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 2}
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 0}


def test_rest_page_failure_preserves_previous_state_and_retry_ingests_once(tmp_path, monkeypatch, capsys):
    import requests
    args = setup_pull(tmp_path, monkeypatch, {"messages": [{"id": "one", "raw": RAW}]})
    cli.cmd_inbox_pull(args)
    capsys.readouterr()

    def failing_pages(url, **kwargs):
        if "page=2" in url:
            raise requests.ConnectionError("private-token")
        return Response({"messages": [{"id": "two", "raw": RAW}], "next": "?page=2"})

    monkeypatch.setattr("catchall_mail.rest_source.requests.get", failing_pages)
    with pytest.raises(ValueError, match="REST request") as caught:
        cli.cmd_inbox_pull(args)
    assert "private-token" not in str(caught.value)
    assert capsys.readouterr().out == ""
    with sqlite3.connect(args.db) as db:
        assert db.execute("SELECT id FROM rest_checkpoints").fetchall() == [("one",)]
        assert db.execute("SELECT COUNT(*) FROM inbox_messages").fetchone()[0] == 1
    monkeypatch.setattr("catchall_mail.rest_source.requests.get", lambda *a, **kw: Response({
        "messages": [{"id": "one", "raw": RAW}, {"id": "two", "raw": RAW}],
    }))
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 1}
    cli.cmd_inbox_pull(args)
    assert json.loads(capsys.readouterr().out) == {"ingested": 0}
