"""Google Admin SDK Directory API (directory_v1) HTTP client."""
from __future__ import annotations

import json

from dns_zone.client import build_http_session


class DirectoryApiError(Exception):
    def __init__(self, status: int, payload: dict | str):
        self.status = status
        self.payload = payload
        msg = payload.get("error", {}).get("message") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else str(payload)[:120]
        super().__init__(f"HTTP {status}: {msg}")


def _parse(resp) -> dict | str:
    try:
        return resp.json()
    except Exception:
        return getattr(resp, "text", "")


class DirectoryClient:
    """Provision and manage directory users against Google Admin SDK Directory v1 endpoints."""

    def __init__(self, base_url: str = "https://admin.googleapis.com", bearer_token: str = "mock-token", session=None):
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.session = session or build_http_session(self.base_url)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        }

    def insert_user(self, seat: dict) -> dict:
        payload = {
            "primaryEmail": seat["email"],
            "name": {
                "givenName": seat["first_name"],
                "familyName": seat["last_name"],
            },
            "password": seat["password"],
            "orgUnitPath": seat.get("org_unit", "/"),
            "changePasswordAtNextLogin": bool(seat.get("change_password_at_next_login", False)),
        }
        r = self.session.post(
            f"{self.base_url}/admin/directory/v1/users",
            data=json.dumps(payload),
            headers=self._headers(),
            timeout=30,
        )
        body = _parse(r)
        if r.status_code in (200, 201) and isinstance(body, dict):
            return body
        raise DirectoryApiError(r.status_code, body)

    def list_users(self, domain: str) -> list[dict]:
        r = self.session.get(
            f"{self.base_url}/admin/directory/v1/users",
            params={"domain": domain},
            headers=self._headers(),
            timeout=30,
        )
        body = _parse(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body.get("users", [])
        raise DirectoryApiError(r.status_code, body)

    def delete_user(self, user_key: str) -> dict:
        r = self.session.delete(
            f"{self.base_url}/admin/directory/v1/users/{user_key}",
            headers=self._headers(),
            timeout=30,
        )
        if r.status_code in (200, 204):
            return {"deleted": user_key}
        raise DirectoryApiError(r.status_code, _parse(r))
