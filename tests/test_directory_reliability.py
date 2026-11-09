"""Offline Directory API reliability and authentication contracts."""
from __future__ import annotations

import pytest

from directory_seats.client import DirectoryApiError, DirectoryClient


class Response:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self.body = {} if body is None else body

    def json(self):
        return self.body


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def _call(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)

    def delete(self, url, **kwargs):
        return self._call("DELETE", url, **kwargs)


def test_list_users_follows_page_tokens_including_empty_pages():
    session = Session(
        Response(body={"users": [{"id": "1"}], "nextPageToken": "a+b/="}),
        Response(body={"nextPageToken": "second"}),
        Response(body={"users": [{"id": "2"}]}),
    )
    client = DirectoryClient("https://directory.example.test/", "explicit", session)
    assert client.list_users("example.test") == [{"id": "1"}, {"id": "2"}]
    assert [call[2]["params"] for call in session.calls] == [
        {"domain": "example.test"},
        {"domain": "example.test", "pageToken": "a+b/="},
        {"domain": "example.test", "pageToken": "second"},
    ]
    assert all(call[2]["headers"]["Authorization"] == "Bearer explicit" for call in session.calls)


@pytest.mark.parametrize("method", ["get_user", "delete_user"])
def test_user_keys_are_quoted_as_one_path_segment(method):
    session = Session(Response(body={"id": "user"}))
    client = DirectoryClient(session=session)
    key = "a+b/example@test.invalid?#%"
    result = getattr(client, method)(key)
    assert session.calls[0][1].endswith("/users/a%2Bb%2Fexample%40test.invalid%3F%23%25")
    assert result == ({"id": "user"} if method == "get_user" else {"deleted": key})


def test_get_user_preserves_api_errors():
    client = DirectoryClient(session=Session(Response(404, {"error": {"message": "missing"}})))
    with pytest.raises(DirectoryApiError, match="missing") as error:
        client.get_user("missing")
    assert error.value.status == 404

@pytest.mark.parametrize("method", ["list_users", "get_user"])
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_safe_reads_retry_transient_responses(method, status):
    session = Session(Response(status), Response(body={"users": [], "id": "ok"}))
    waits = []
    client = DirectoryClient(session=session, sleep=waits.append)
    getattr(client, method)("example.test")
    assert len(session.calls) == 2
    assert waits == [1.0]


def test_read_retries_are_bounded():
    session = Session(*(Response(503) for _ in range(4)))
    waits = []
    client = DirectoryClient(session=session, max_retries=2, sleep=waits.append)
    with pytest.raises(DirectoryApiError) as error:
        client.list_users("example.test")
    assert error.value.status == 503
    assert len(session.calls) == 3
    assert waits == [1.0, 2.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409])
def test_permanent_read_errors_are_not_retried(status):
    session = Session(Response(status))
    client = DirectoryClient(session=session)
    with pytest.raises(DirectoryApiError) as error:
        client.get_user("user")
    assert error.value.status == status
    assert len(session.calls) == 1


@pytest.mark.parametrize("method", ["insert_user", "delete_user"])
def test_writes_are_not_retried(method):
    session = Session(Response(503))
    client = DirectoryClient(session=session)
    value = {"email": "a@example.test", "first_name": "A", "last_name": "B", "password": "test-only"} if method == "insert_user" else "user"
    with pytest.raises(DirectoryApiError):
        getattr(client, method)(value)
    assert len(session.calls) == 1

@pytest.mark.parametrize("method", ["list_users", "get_user", "insert_user", "delete_user"])
def test_credentials_before_request_refreshes_each_request(method):
    session = Session(Response(body={"users": []}), Response(body={"users": []}))
    transport = object()

    class Credentials:
        def __init__(self):
            self.calls = []

        def before_request(self, request, verb, url, headers):
            assert request is transport
            self.calls.append((verb, url))
            headers["Authorization"] = f"Bearer refreshed-{len(self.calls)}"

    credentials = Credentials()
    client = DirectoryClient(session=session, credentials=credentials, auth_request=transport)
    value = {"email": "a@example.test", "first_name": "A", "last_name": "B", "password": "test-only"} if method == "insert_user" else "example.test"
    getattr(client, method)(value)
    getattr(client, method)(value)
    assert [call[2]["headers"]["Authorization"] for call in session.calls] == ["Bearer refreshed-1", "Bearer refreshed-2"]
    assert credentials.calls == [(call[0], call[1]) for call in session.calls]


def test_token_provider_is_called_again_on_retry():
    tokens = iter(["first", "second"])
    session = Session(Response(429), Response(body={"users": []}))
    client = DirectoryClient(session=session, token_provider=lambda: next(tokens), sleep=lambda _: None)
    assert client.list_users("example.test") == []
    assert [call[2]["headers"]["Authorization"] for call in session.calls] == ["Bearer first", "Bearer second"]


def test_auth_factory_explicit_token_precedes_environment_without_google_auth(monkeypatch):
    import builtins
    from directory_seats import auth
    real_import = builtins.__import__

    def no_google(name, *args, **kwargs):
        if name.startswith("google"):
            raise AssertionError("token authentication must not import google-auth")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_google)
    assert auth.build_directory_auth(bearer_token="explicit", environ={"GOOGLE_DIRECTORY_TOKEN": "env"}) == {"bearer_token": "explicit"}
    assert auth.build_directory_auth(environ={"GOOGLE_DIRECTORY_TOKEN": "env"}) == {"bearer_token": "env"}
    with pytest.raises(ValueError, match="authentication"):
        auth.build_directory_auth(environ={})

@pytest.mark.parametrize("from_env", [False, True])
def test_auth_factory_loads_authorized_user_file_lazily(monkeypatch, from_env):
    import sys
    from types import ModuleType, SimpleNamespace
    from directory_seats.auth import build_directory_auth
    calls = []
    credentials = object()

    class Credentials:
        @staticmethod
        def from_authorized_user_file(filename, scopes):
            calls.append((filename, scopes))
            return credentials

    monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", SimpleNamespace(credentials=SimpleNamespace(Credentials=Credentials)))
    options = {"environ": {"GOOGLE_DIRECTORY_CREDENTIALS_FILE": "oauth.json"}} if from_env else {"authorized_user_file": "oauth.json", "environ": {}}
    assert build_directory_auth(**options) == {"credentials": credentials}
    assert calls == [("oauth.json", ("https://www.googleapis.com/auth/admin.directory.user",))]

@pytest.mark.parametrize("from_env", [False, True])
def test_auth_factory_delegates_service_account_with_scopes(monkeypatch, from_env):
    import sys
    from types import ModuleType, SimpleNamespace
    from directory_seats.auth import build_directory_auth
    calls = []
    delegated = object()

    class Credentials:
        @staticmethod
        def from_service_account_file(filename, scopes):
            calls.append((filename, scopes))
            return Credentials()

        def with_subject(self, subject):
            calls.append(subject)
            return delegated

    monkeypatch.setitem(sys.modules, "google", ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.oauth2", SimpleNamespace(service_account=SimpleNamespace(Credentials=Credentials)))
    options = {"environ": {"GOOGLE_DIRECTORY_SERVICE_ACCOUNT_FILE": "service.json", "GOOGLE_DIRECTORY_DELEGATED_ADMIN": "admin@example.test"}} if from_env else {"service_account_file": "service.json", "delegated_admin": "admin@example.test", "environ": {}}
    assert build_directory_auth(**options, scopes=("custom-scope",)) == {"credentials": delegated}
    assert calls == [("service.json", ("custom-scope",)), "admin@example.test"]


def test_service_account_requires_explicit_delegated_admin():
    from directory_seats.auth import build_directory_auth
    with pytest.raises(ValueError, match="delegated_admin"):
        build_directory_auth(service_account_file="service.json", environ={})

@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_retry_budget_rejected(value):
    with pytest.raises(ValueError, match="max_retries"):
        DirectoryClient(session=Session(), max_retries=value)


@pytest.mark.parametrize("mode", ["oauth", "service_account"])
def test_optional_auth_dependency_error_is_actionable(monkeypatch, mode):
    import builtins
    from directory_seats.auth import build_directory_auth
    real_import = builtins.__import__

    def missing_google(name, *args, **kwargs):
        if name.startswith("google"):
            raise ImportError("missing google")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_google)
    with pytest.raises(RuntimeError, match="google-auth"):
        options = {"authorized_user_file": "oauth.json"} if mode == "oauth" else {"service_account_file": "service.json", "delegated_admin": "admin@example.test"}
        build_directory_auth(**options, environ={})


def test_default_auth_transport_is_lazy_and_reused(monkeypatch):
    import sys
    from types import SimpleNamespace
    transport = object()
    created = []

    def request():
        created.append(True)
        return transport

    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", SimpleNamespace(Request=request))

    class Credentials:
        def before_request(self, request, method, url, headers):
            assert request is transport
            headers["Authorization"] = "Bearer refreshed"

    client = DirectoryClient(credentials=Credentials(), session=Session(Response(), Response()))
    assert created == []
    client.get_user("one")
    client.get_user("two")
    assert created == [True]
