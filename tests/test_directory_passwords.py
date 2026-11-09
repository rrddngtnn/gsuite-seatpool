"""Offline credential generation, persistence, and export security regressions."""
from __future__ import annotations

import os
import stat

import pytest

from directory_seats.generator import export_admin_csv, make_seat


def test_reinitializing_tenant_preserves_stored_credentials(tmp_path, capsys):
    from gsuite_seatpool.cli import main
    from seat_allocator.store import open_db

    path = str(tmp_path / "state.db")
    args = ["--db", path, "init-tenant", "--fqdn", "alpha.example.test", "--seats", "2"]
    assert main(args) == 0
    with open_db(path) as db:
        # Existing passwords, including legacy or manually changed values, are authoritative.
        db.execute("UPDATE tenants SET admin_password = ?", ("existing-admin-password",))
        db.execute("UPDATE seats SET password = ? WHERE seat_index = 1", ("existing-seat-password",))
        before_admin = db.execute("SELECT admin_password FROM tenants").fetchone()[0]
        before_seats = [dict(row) for row in db.execute("SELECT * FROM seats ORDER BY seat_index")]

    assert main(args) == 0
    with open_db(path) as db:
        assert db.execute("SELECT admin_password FROM tenants").fetchone()[0] == before_admin
        assert [dict(row) for row in db.execute("SELECT * FROM seats ORDER BY seat_index")] == before_seats
    assert "existing-admin-password" not in capsys.readouterr().out


def test_credential_csv_is_private_even_with_permissive_umask(tmp_path):
    path = tmp_path / "credentials.csv"
    old_umask = os.umask(0)
    try:
        export_admin_csv([make_seat("alpha.example.test", 1)], str(path))
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("existing_kind", ["regular", "symlink", "dangling_symlink"])
def test_credential_csv_refuses_existing_destination(tmp_path, existing_kind):
    path = tmp_path / "credentials.csv"
    target = tmp_path / "target.csv"
    if existing_kind == "regular":
        path.write_text("existing data", encoding="utf-8")
    else:
        if existing_kind == "symlink":
            target.write_text("existing data", encoding="utf-8")
        path.symlink_to(target)

    with pytest.raises(FileExistsError):
        export_admin_csv([make_seat("alpha.example.test", 1)], str(path))

    if existing_kind == "regular":
        assert path.read_text(encoding="utf-8") == "existing data"
    else:
        assert path.is_symlink()
        if existing_kind == "symlink":
            assert target.read_text(encoding="utf-8") == "existing data"
        else:
            assert not target.exists()
