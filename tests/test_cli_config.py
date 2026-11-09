"""Configuration precedence and resumable CLI lifecycle regression tests."""
import json
import sqlite3

import pytest

from gsuite_seatpool import cli, config


def test_configuration_precedence(tmp_path):
    path = tmp_path / "seatpool.toml"
    path.write_text('schema_version = 1\ndb = "file.db"\n[dns]\nprovider = "manual"\nbase_url = "https://dns.file"\n[directory]\nbase_url = "https://directory.file"\ntoken = "file-token"\n')
    env = {"SEATPOOL_DB": "env.db", "REGISTRAR_BASE_URL": "https://dns.env", "DIRECTORY_BASE_URL": "https://directory.env", "DIRECTORY_TOKEN": "env-token", "DNS_PROVIDER": "registrar"}
    resolved = config.load_config(path, environ=env, overrides={"db": "cli.db", "dns": {"provider": "cloudflare"}, "directory": {"token": "cli-token"}})
    assert resolved["db"] == "cli.db"
    assert resolved["dns"]["provider"] == "cloudflare"
    assert resolved["dns"]["base_url"] == "https://dns.env"
    assert resolved["directory"] == {"base_url": "https://directory.env", "token": "cli-token"}
    assert config.load_config(path, environ={})["dns"]["provider"] == "manual"
    assert config.load_config(environ={})["db"] == "gsuite_seatpool.db"


@pytest.mark.parametrize("text", ['schema_version = 2', 'schema_version = true', 'db = 123', '[dns]\nprovider = ""', '[directory]\nbase_url = 123'])
def test_invalid_configuration_rejected(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        config.load_config(path, environ={})


def test_cli_resolves_config_env_and_explicit_options(tmp_path, monkeypatch):
    path = tmp_path / "cli.toml"
    path.write_text('schema_version = 1\ndb = "file.db"\n[directory]\nbase_url = "https://file"\ntoken = "file-token"\n')
    monkeypatch.setenv("DIRECTORY_TOKEN", "env-token")
    captured = []
    monkeypatch.setattr(cli, "cmd_sync_directory", lambda args: captured.append(args) or 0)
    assert cli.main(["--config", str(path), "--db", "explicit.db", "sync-directory", "--fqdn", "example.test", "--base-url", "https://cli"]) == 0
    assert captured[0].db == "explicit.db"
    assert captured[0].base_url == "https://cli"
    assert captured[0].token == "env-token"
    assert captured[0].settings["directory"]["base_url"] == "https://cli"


@pytest.fixture
def staged(tmp_path, capsys):
    path = str(tmp_path / "state.db")
    cli.main(["--db", path, "init-tenant", "--fqdn", "example.test", "--seats", "3"])
    capsys.readouterr()
    return path


def seat_states(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT email,status FROM seats ORDER BY seat_index").fetchall()


def test_sync_persists_success_before_later_failure_and_resumes(staged, monkeypatch, capsys):
    remote = []
    calls = []
    class Directory:
        def __init__(self, **kwargs):
            pass
        def list_users(self, domain):
            calls.append(domain)
            return list(remote)
        def insert_user(self, seat):
            if len(remote) == 1:
                raise RuntimeError("interrupted")
            user = {"primaryEmail": seat["email"]}
            remote.append(user)
            return user
    monkeypatch.setattr(cli, "DirectoryClient", Directory)
    args = ["--db", staged, "sync-directory", "--fqdn", "example.test"]
    with pytest.raises(RuntimeError, match="interrupted"):
        cli.main(args)
    assert [s for _, s in seat_states(staged)] == ["provisioned", "ready", "ready"]
    # A remote success not saved locally must be reconciled rather than recreated.
    remote.append({"primaryEmail": seat_states(staged)[1][0].upper()})
    cli.main(args)
    assert [s for _, s in seat_states(staged)] == ["provisioned"] * 3
    assert calls == ["example.test", "example.test"]
    assert len(remote) == 3


def test_status_is_one_json_object_and_does_not_claim_staged_provisioned(staged, capsys):
    cli.main(["--db", staged, "status"])
    result = json.loads(capsys.readouterr().out)
    assert result["seats_ready"] == 0
    assert result["seats_provisioned"] == 0
    assert result["tenants_detail"][0]["status"] == "staged"


def test_dns_verification_only_marks_pending(staged, monkeypatch):
    import dns_zone.client as dns
    class Provider:
        def ensure_google_verification(self, fqdn, token):
            return {"action": "created", "record": {"type": "TXT", "name": "@", "value": token}}
    monkeypatch.setattr(dns, "create_dns_provider", lambda settings: Provider(), raising=False)
    cli.main(["--db", staged, "dns-verify", "--fqdn", "example.test", "--token", "verification-value"])
    with sqlite3.connect(staged) as db:
        assert db.execute("SELECT status FROM tenants").fetchone()[0] == "dns_pending"


def test_plan_is_local_read_only_and_redacts_passwords(staged, capsys, monkeypatch):
    monkeypatch.setattr(cli, "DirectoryClient", lambda **kwargs: pytest.fail("plan must not contact remote"))
    before = seat_states(staged)
    cli.main(["--db", staged, "plan", "--fqdn", "example.test"])
    result = json.loads(capsys.readouterr().out)
    assert result["fqdn"] == "example.test"
    assert len(result["pending"]) == 3
    assert "password" not in json.dumps(result)
    assert seat_states(staged) == before


def test_reconcile_reads_remote_once_only_updates_matching_local_seats(staged, monkeypatch, capsys):
    emails = [email for email, _ in seat_states(staged)]
    calls = []
    class Directory:
        def __init__(self, **kwargs):
            pass
        def list_users(self, domain):
            calls.append(domain)
            return [{"primaryEmail": emails[0].upper()}, {"primaryEmail": "unrelated@example.test"}]
        def insert_user(self, seat):
            pytest.fail("reconcile must never create remote users")
    monkeypatch.setattr(cli, "DirectoryClient", Directory)
    cli.main(["--db", staged, "reconcile", "--fqdn", "example.test"])
    result = json.loads(capsys.readouterr().out)
    assert result["matched"] == 1
    assert calls == ["example.test"]
    assert [status for _, status in seat_states(staged)] == ["provisioned", "ready", "ready"]


def test_allocate_redacts_password_by_default(staged, capsys):
    with sqlite3.connect(staged) as db:
        db.execute("UPDATE seats SET status='provisioned'")
        db.execute("UPDATE tenants SET status='active'")
    cli.main(["--db", staged, "allocate", "--target", "test"])
    result = json.loads(capsys.readouterr().out)
    assert result["allocated_count"] == 1
    assert "password" not in result["allocated"][0]


def test_dns_provider_and_catchall_env_are_wired(staged, monkeypatch):
    import dns_zone.client as dns
    monkeypatch.setenv("CATCHALL_TARGET", "mail@example.test")
    captured = []
    class Provider:
        def ensure_catchall(self, fqdn, mode, target):
            captured.append(target)
            return []
    monkeypatch.setattr(dns, "create_dns_provider", lambda settings: captured.append(settings) or Provider(), raising=False)
    cli.main(["--db", staged, "dns-catchall", "--fqdn", "example.test", "--provider", "manual"])
    assert captured[0]["provider"] == "manual"
    assert captured[1] == "mail@example.test"


def test_directory_auth_factory_wired(staged, monkeypatch, capsys):
    import directory_seats.auth as auth
    marker = object()
    seen = []
    monkeypatch.setattr(auth, "build_directory_auth", lambda **kwargs: seen.append(kwargs) or {"credentials": marker})
    class Directory:
        def __init__(self, **kwargs):
            assert kwargs["credentials"] is marker
        def list_users(self, domain):
            return []
    monkeypatch.setattr(cli, "DirectoryClient", Directory)
    cli.main(["--db", staged, "reconcile", "--fqdn", "example.test", "--token", "explicit-token"])
    assert seen[0]["bearer_token"] == "explicit-token"


def test_configured_credentials_are_not_masked_by_mock_token(staged, tmp_path, monkeypatch):
    import directory_seats.auth as auth
    path = tmp_path / "credentials.toml"
    path.write_text('schema_version = 1\n[directory]\nbase_url = "https://admin.googleapis.com"\nservice_account_file = "service.json"\ndelegated_admin = "admin@example.test"\n')
    seen = []
    monkeypatch.setattr(auth, "build_directory_auth", lambda **kwargs: seen.append(kwargs) or {"bearer_token": "test"})
    class Directory:
        def __init__(self, **kwargs):
            pass
        def list_users(self, domain):
            return []
    monkeypatch.setattr(cli, "DirectoryClient", Directory)
    cli.main(["--config", str(path), "--db", staged, "reconcile", "--fqdn", "example.test"])
    assert seen[0]["bearer_token"] is None
    assert seen[0]["service_account_file"] == "service.json"
    assert seen[0]["delegated_admin"] == "admin@example.test"


def test_google_token_environment_overrides_file_token(tmp_path):
    path = tmp_path / "auth.toml"
    path.write_text('schema_version = 1\n[directory]\ntoken = "file-token"\n')
    result = config.load_config(path, environ={"GOOGLE_DIRECTORY_TOKEN": "google-env-token"})
    assert result["directory"]["token"] == "google-env-token"


def test_manual_catchall_does_not_claim_dns_ready(staged, capsys):
    cli.main(["--db", staged, "dns-catchall", "--fqdn", "example.test", "--provider", "manual", "--target", "mail@example.test"])
    with sqlite3.connect(staged) as db:
        assert db.execute("SELECT status FROM tenants").fetchone()[0] == "dns_pending"
        assert db.execute("SELECT COUNT(*) FROM dns_records").fetchone()[0] == 0


def test_sync_empty_or_zero_limit_does_not_activate_staged_tenant(staged, monkeypatch):
    class Directory:
        def __init__(self, **kwargs):
            pass
        def list_users(self, domain):
            return []
    monkeypatch.setattr(cli, "DirectoryClient", Directory)
    cli.main(["--db", staged, "sync-directory", "--fqdn", "example.test", "--limit", "0"])
    with sqlite3.connect(staged) as db:
        assert db.execute("SELECT status FROM tenants").fetchone()[0] == "staged"


def test_registrar_username_env_compatibility(staged, tmp_path, monkeypatch):
    import dns_zone.client as dns
    path = tmp_path / "dns.toml"
    path.write_text('schema_version = 1\n[dns]\nusername_env = "REGISTRAR_USER"\npassword_env = "REGISTRAR_PASSWORD"\n')
    captured = []
    class Provider:
        def ensure_google_verification(self, fqdn, token):
            return {"action": "planned", "record": {"type": "TXT", "name": "@", "value": token}}
    monkeypatch.setattr(dns, "create_dns_provider", lambda settings: captured.append(settings) or Provider())
    cli.main(["--config", str(path), "--db", staged, "dns-verify", "--fqdn", "example.test", "--token", "verification"])
    assert captured[0]["email_env"] == "REGISTRAR_USER"


def test_config_table_shape_errors_are_actionable(tmp_path):
    path = tmp_path / "invalid.toml"
    path.write_text('dns = "not-a-table"\n')
    with pytest.raises(ValueError, match="dns must be a table"):
        config.load_config(path, environ={"REGISTRAR_BASE_URL": "https://env"})
