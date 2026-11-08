"""Offline pytest fixtures for gsuite-seatpool."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))


class TestClientSessionAdapter:
    """Adapt FastAPI TestClient to the session interface expected by DnsZoneClient and DirectoryClient."""

    def __init__(self, tc):
        self.tc = tc

    @staticmethod
    def _path(url: str) -> str:
        if "://" in url:
            return "/" + url.split("/", 3)[-1]
        return url

    def get(self, url: str, params=None, headers=None, timeout=30):
        return self.tc.get(self._path(url), params=params, headers=headers)

    def post(self, url: str, data=None, json=None, headers=None, timeout=30):
        if data is not None:
            return self.tc.post(self._path(url), content=data, headers=headers)
        return self.tc.post(self._path(url), json=json, headers=headers)

    def put(self, url: str, data=None, json=None, headers=None, timeout=30):
        if data is not None:
            return self.tc.put(self._path(url), content=data, headers=headers)
        return self.tc.put(self._path(url), json=json, headers=headers)

    def delete(self, url: str, headers=None, timeout=30):
        return self.tc.delete(self._path(url), headers=headers)


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from gsuite_seatpool import mock_server

    mock_server.reset()
    return TestClient(mock_server.app)


@pytest.fixture()
def session_adapter(client):
    return TestClientSessionAdapter(client)


@pytest.fixture()
def db(tmp_path):
    from seat_allocator.store import open_db

    return open_db(str(tmp_path / "test.db"))
