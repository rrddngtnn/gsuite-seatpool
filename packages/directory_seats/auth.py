"""Explicit Directory authentication configuration; no implicit ADC or login flow."""
from __future__ import annotations

import os
from collections.abc import Mapping


DIRECTORY_SCOPES = ("https://www.googleapis.com/auth/admin.directory.user",)


def build_directory_auth(
    *,
    bearer_token: str | None = None,
    authorized_user_file: str | None = None,
    service_account_file: str | None = None,
    delegated_admin: str | None = None,
    environ: Mapping[str, str] | None = None,
    scopes=DIRECTORY_SCOPES,
) -> dict:
    """Return DirectoryClient kwargs without API requests or implicit ADC discovery.

    Precedence: bearer token, authorized-user file, delegated service account.
    Environment fallbacks are GOOGLE_DIRECTORY_TOKEN,
    GOOGLE_DIRECTORY_CREDENTIALS_FILE, GOOGLE_DIRECTORY_SERVICE_ACCOUNT_FILE,
    and GOOGLE_DIRECTORY_DELEGATED_ADMIN. Credential modes import google-auth
    lazily; the caller must install google-auth and requests separately.
    """
    env = os.environ if environ is None else environ
    token = bearer_token or env.get("GOOGLE_DIRECTORY_TOKEN")
    if token:
        return {"bearer_token": token}
    filename = authorized_user_file or env.get("GOOGLE_DIRECTORY_CREDENTIALS_FILE")
    if filename:
        try:
            from google.oauth2 import credentials
        except ImportError as exc:
            raise RuntimeError("Install google-auth and requests for Google credential authentication") from exc
        return {"credentials": credentials.Credentials.from_authorized_user_file(filename, scopes=scopes)}
    service_file = service_account_file or env.get("GOOGLE_DIRECTORY_SERVICE_ACCOUNT_FILE")
    if service_file:
        subject = delegated_admin or env.get("GOOGLE_DIRECTORY_DELEGATED_ADMIN")
        if not subject:
            raise ValueError("Service-account authentication requires delegated_admin")
        try:
            from google.oauth2 import service_account
        except ImportError as exc:
            raise RuntimeError("Install google-auth and requests for Google credential authentication") from exc
        credentials = service_account.Credentials.from_service_account_file(service_file, scopes=scopes)
        return {"credentials": credentials.with_subject(subject)}
    raise ValueError("Directory authentication must be explicitly configured")
