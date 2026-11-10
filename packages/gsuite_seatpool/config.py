"""Versioned TOML configuration; explicit options override environment and file."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULTS = {
    "schema_version": 1,
    "db": "gsuite_seatpool.db",
    "dns": {"provider": "registrar", "base_url": "http://127.0.0.1:8788"},
    "directory": {"base_url": "http://127.0.0.1:8788", "token": ""},
}


def _merge(target, source):
    for key, value in source.items():
        if isinstance(value, dict):
            if key in target and not isinstance(target[key], dict):
                raise ValueError(f"{key} has an invalid table value")
            _merge(target.setdefault(key, {}), value)
        else:
            target[key] = value


def load_config(path=None, *, environ=None, overrides=None):
    """Resolve defaults < TOML < environment < explicit CLI values."""
    import copy
    result = copy.deepcopy(DEFAULTS)
    if path is not None:
        try:
            import tomllib
        except ImportError:  # Python 3.10
            import tomli as tomllib
        with Path(path).open("rb") as stream:
            _merge(result, tomllib.load(stream))
    for section in ("dns", "directory"):
        if not isinstance(result[section], dict):
            raise ValueError(f"{section} must be a table")
    env = os.environ if environ is None else environ
    for variable, section, key in (
        ("SEATPOOL_DB", None, "db"),
        ("DNS_PROVIDER", "dns", "provider"),
        ("REGISTRAR_BASE_URL", "dns", "base_url"),
        ("DIRECTORY_BASE_URL", "directory", "base_url"),
        ("GOOGLE_DIRECTORY_TOKEN", "directory", "token"),
        ("DIRECTORY_TOKEN", "directory", "token"),
        ("GOOGLE_DIRECTORY_CREDENTIALS_FILE", "directory", "authorized_user_file"),
        ("GOOGLE_DIRECTORY_SERVICE_ACCOUNT_FILE", "directory", "service_account_file"),
        ("GOOGLE_DIRECTORY_DELEGATED_ADMIN", "directory", "delegated_admin"),
        ("CATCHALL_TARGET", None, "catchall_target"),
    ):
        if variable in env:
            (result[section] if section else result)[key] = env[variable]
    _merge(result, overrides or {})
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("schema_version must be 1")
    if not isinstance(result["db"], str) or not result["db"]:
        raise ValueError("db must be a non-empty string")
    for section in ("dns", "directory"):
        if not isinstance(result[section], dict):
            raise ValueError(f"{section} must be a table")
        for key, value in result[section].items():
            if not isinstance(value, str):
                raise ValueError(f"{section}.{key} must be a string")
    if not result["dns"]["provider"].strip():
        raise ValueError("dns.provider must be a non-empty string")
    if "mail" in result:
        mail = result["mail"]
        if not isinstance(mail, dict):
            raise ValueError("mail must be a table")
        provider = mail.get("provider", "imap")
        if provider not in ("imap", "rest", "tmail_v1"):
            raise ValueError("mail.provider must be imap, rest or tmail_v1")
        allowed = ({"provider", "url", "bearer_env", "max_pages", "mailbox"} if provider == "tmail_v1" else
                   {"provider", "url", "bearer_env", "max_pages"} if provider == "rest" else
                   {"provider", "host", "username_env", "password_env", "mailbox", "port"})
        if mail.keys() - allowed:
            raise ValueError("mail has unsupported fields; use environment references for credentials")
        for key, value in mail.items():
            if key == "port":
                if type(value) is not int or not 1 <= value <= 65535:
                    raise ValueError("mail.port must be an integer between 1 and 65535")
            elif key == "max_pages":
                if type(value) is not int or not 1 <= value <= 100:
                    raise ValueError("mail.max_pages must be an integer between 1 and 100")
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"mail.{key} must be a non-empty string")
        if provider == "tmail_v1":
            from catchall_mail.tmail_v1 import TMailV1Source
            TMailV1Source.validate_config(mail)
        if provider == "rest":
            from catchall_mail.rest_source import RestSource
            RestSource._origin(mail.get("url"))
    directory = result["directory"]
    if not directory["token"] and not any(directory.get(key) for key in ("authorized_user_file", "service_account_file")):
        if directory["base_url"] == DEFAULTS["directory"]["base_url"]:
            directory["token"] = "mock-token"
    return result
