"""Google Admin SDK Directory API (directory_v1) HTTP client."""
from __future__ import annotations

import json
import time
from urllib.parse import quote

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

    def __init__(
        self,
        base_url: str = "https://admin.googleapis.com",
        bearer_token: str = "mock-token",
        session=None,
        *,
        credentials=None,
        token_provider=None,
        auth_request=None,
        max_retries: int = 2,
        sleep=time.sleep,
    ):
        """Keep legacy bearer auth; credentials or a zero-argument provider may replace it.

        Credentials receive before_request on every attempt. Only GET responses
        with HTTP 429 or 5xx are retried, at most max_retries times.
        """
        if type(max_retries) is not int or max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        self.credentials = credentials
        self.token_provider = token_provider
        self.auth_request = auth_request
        self.max_retries = max_retries
        self.sleep = sleep
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.session = session or build_http_session(self.base_url)

    def _headers(self, method: str = "GET", url: str = "") -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.credentials is not None:
            if self.auth_request is None:
                from google.auth.transport.requests import Request
                self.auth_request = Request()
            self.credentials.before_request(self.auth_request, method, url, headers)
        else:
            token = self.token_provider() if self.token_provider is not None else self.bearer_token
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _get(self, url: str, **kwargs):
        attempt = 0
        while True:
            response = self.session.get(url, headers=self._headers("GET", url), timeout=30, **kwargs)
            transient = response.status_code == 429 or 500 <= response.status_code <= 599
            if not transient or attempt == self.max_retries:
                return response
            self.sleep(min(2.0 ** attempt, 30.0))
            attempt += 1

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
            headers=self._headers("POST", f"{self.base_url}/admin/directory/v1/users"),
            timeout=30,
        )
        body = _parse(r)
        if r.status_code in (200, 201) and isinstance(body, dict):
            return body
        raise DirectoryApiError(r.status_code, body)

    def list_users(self, domain: str) -> list[dict]:
        users = []
        params = {"domain": domain}
        seen_tokens = set()
        while True:
            r = self._get(
                f"{self.base_url}/admin/directory/v1/users",
                params=dict(params),
            )
            body = _parse(r)
            if r.status_code != 200 or not isinstance(body, dict):
                raise DirectoryApiError(r.status_code, body)
            users.extend(body.get("users", []))
            token = body.get("nextPageToken")
            if not token:
                return users
            if not isinstance(token, str) or token in seen_tokens:
                raise DirectoryApiError(502, {"error": {"message": "Invalid repeated pagination token"}})
            seen_tokens.add(token)
            params["pageToken"] = token

    def get_user(self, user_key: str) -> dict:
        r = self._get(
            f"{self.base_url}/admin/directory/v1/users/{quote(user_key, safe='')}",
        )
        body = _parse(r)
        if r.status_code == 200 and isinstance(body, dict):
            return body
        raise DirectoryApiError(r.status_code, body)

    def delete_user(self, user_key: str) -> dict:
        r = self.session.delete(
            f"{self.base_url}/admin/directory/v1/users/{quote(user_key, safe='')}",
            headers=self._headers("DELETE", f"{self.base_url}/admin/directory/v1/users/{quote(user_key, safe='')}"),
            timeout=30,
        )
        if r.status_code in (200, 204):
            return {"deleted": user_key}
        raise DirectoryApiError(r.status_code, _parse(r))
