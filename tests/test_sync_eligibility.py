"""Sync respects the eligibility of users already present remotely."""
import json
import sqlite3

import pytest

from gsuite_seatpool import cli


@pytest.mark.parametrize("flags", [{"suspended": True}, {"archived": True}, {"suspended": True, "archived": True}])
def test_sync_existing_ineligible_user_stays_unallocatable(tmp_path, monkeypatch, capsys, flags):
    path = str(tmp_path / "state.db")
    cli.main(["--db", path, "init-tenant", "--fqdn", "example.test", "--seats", "1"])
    with sqlite3.connect(path) as db:
        email = db.execute("SELECT email FROM seats").fetchone()[0]

    class Directory:
        def list_users(self, fqdn):
            return [{"primaryEmail": email.upper(), **flags}]

        def insert_user(self, seat):
            pytest.fail("existing remote user must not be recreated")

    monkeypatch.setattr(cli, "_directory_client", lambda args: Directory())
    capsys.readouterr()
    args = ["--db", path, "sync-directory", "--fqdn", "example.test"]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["provisioned"] == 0
    assert cli.main(args) == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM seats").fetchone()[0] == "suspended"
        assert db.execute("SELECT status FROM tenants").fetchone()[0] == "staged"
