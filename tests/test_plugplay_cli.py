"""Plug-and-play configuration and offline CLI integration."""
import json
import sqlite3

import pytest

from gsuite_seatpool import cli, config


def test_dns_plugin_name_passes_configuration_and_cli(tmp_path, monkeypatch):
    assert config.load_config(environ={}, overrides={"dns": {"provider": "installed-plugin"}})["dns"]["provider"] == "installed-plugin"
    seen = []
    monkeypatch.setattr(cli, "cmd_dns_verify", lambda args: seen.append(args.settings["dns"]["provider"]) or 0)
    assert cli.main(["--db", str(tmp_path / "unused.db"), "dns-verify", "--provider", "installed-plugin", "--fqdn", "example.test", "--token", "verification"]) == 0
    assert seen == ["installed-plugin"]


@pytest.mark.parametrize("provider", ["", "   "])
def test_empty_dns_provider_rejected(provider):
    with pytest.raises(ValueError, match="non-empty"):
        config.load_config(environ={}, overrides={"dns": {"provider": provider}})


def test_installed_dns_entrypoint_runs_through_cli(tmp_path, monkeypatch, capsys):
    from importlib import metadata
    from dns_zone.adapters import ManualDnsProvider
    seen = []
    class Entry:
        name = "installed-plugin"
        def load(self):
            return lambda settings: seen.append(settings) or ManualDnsProvider()
    monkeypatch.setattr(metadata, "entry_points", lambda **kwargs: [Entry()])
    assert cli.main(["--db", str(tmp_path / "plugin.db"), "dns-verify", "--provider", "installed-plugin", "--fqdn", "example.test", "--token", "verification"]) == 0
    assert seen[0]["provider"] == "installed-plugin"
    assert json.loads(capsys.readouterr().out)["action"] == "planned"


def test_unknown_provider_factory_failure_is_clean(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--db", str(tmp_path / "unused.db"), "dns-verify", "--provider", "not-installed", "--fqdn", "example.test", "--token", "verification"])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert "Unknown DNS provider" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_mail_table_configuration(tmp_path):
    path = tmp_path / "mail.toml"
    path.write_text('[mail]\nprovider="imap"\nhost="imap.example.test"\nusername_env="MAIL_USER"\npassword_env="MAIL_PASSWORD"\nmailbox="Archive"\nport=1993\n')
    settings = config.load_config(path, environ={})["mail"]
    assert settings == dict(provider="imap", host="imap.example.test", username_env="MAIL_USER", password_env="MAIL_PASSWORD", mailbox="Archive", port=1993)


@pytest.mark.parametrize("text", ['mail="bad"', '[mail]\nport="993"', '[mail]\nport=true', '[mail]\nport=0', '[mail]\nprovider="unsupported"', '[mail]\nhost=123', '[mail]\npassword="plaintext"'])
def test_invalid_mail_table_rejected(tmp_path, text):
    path = tmp_path / "bad.toml"
    path.write_text(text)
    with pytest.raises(ValueError, match="mail"):
        config.load_config(path, environ={})


def test_inbox_pull_persists_parsed_messages_without_stdout_secrets(tmp_path, monkeypatch, capsys):
    import imaplib
    from test_mail_sources import FakeIMAP
    transport = FakeIMAP("unused", 0)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda *a, **kw: transport)
    monkeypatch.setenv("MAIL_USER", "mailbox-user")
    monkeypatch.setenv("MAIL_PASSWORD", "private-password")
    path = tmp_path / "mail.toml"
    path.write_text('[mail]\nprovider="imap"\nhost="imap.example.test"\nusername_env="MAIL_USER"\npassword_env="MAIL_PASSWORD"\n')
    db = tmp_path / "inbox.db"
    assert cli.main(["--config", str(path), "--db", str(db), "inbox-pull", "--limit", "1"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"ingested": 1}
    assert "private-password" not in captured.out + captured.err
    assert "123456" not in captured.out
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT recipient FROM inbox_messages").fetchall() == [("unit42@team.example.test",)]


def test_inbox_pull_missing_settings_is_clean(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        cli.main(["--db", str(tmp_path / "unused.db"), "inbox-pull"])
    assert error.value.code == 2
    assert "mail" in capsys.readouterr().err


def test_organization_cli_uses_store_api(tmp_path, capsys):
    db = str(tmp_path / "organization.db")
    assert cli.main(["--db", db, "org-init", "--key", "org-one", "--capacity", "80"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["capacity"] == 80
    assert cli.main(["--db", db, "init-tenant", "--fqdn", "team.example.test", "--seats", "3"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db, "org-link", "--key", "org-one", "--fqdn", "team.example.test"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reserved"] == 3
    assert cli.main(["--db", db, "org-status", "--key", "org-one"]) == 0
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize("arguments,match", [
    (["org-init", "--key", "", "--capacity", "10"], "key"),
    (["org-init", "--key", "org", "--capacity", "0"], "capacity"),
    (["org-status", "--key", "unknown"], "Unknown organization"),
])
def test_organization_errors_are_actionable(tmp_path, capsys, arguments, match):
    with pytest.raises(SystemExit) as error:
        cli.main(["--db", str(tmp_path / "org.db"), *arguments])
    assert error.value.code == 2
    captured = capsys.readouterr()
    assert match in captured.err
    assert "Traceback" not in captured.err


def test_nested_override_shape_is_actionable():
    with pytest.raises(ValueError, match="dns must be a table"):
        config.load_config(environ={}, overrides={"dns": "bad"})
    with pytest.raises(ValueError, match="db"):
        config.load_config(environ={}, overrides={"db": {"bad": "value"}})
