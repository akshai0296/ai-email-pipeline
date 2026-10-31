"""Test setup: local/ modules import without the backend, a model or the network.

The real ``bootstrap`` loads backend/, patches its database and storage, and starts the
egress guard. The modules under test only need three things from it, so a stand-in
provides them, with storage in a temporary folder that is emptied before each test.
"""

from __future__ import annotations

import shutil
import socket
import sys
import tempfile
import types
from pathlib import Path

import pytest

LOCAL_DIR = Path(__file__).resolve().parents[1] / "local"
STORAGE_DIR = Path(tempfile.mkdtemp(prefix="email-pipeline-tests-"))
FILES_URL = "http://127.0.0.1:8765/files"


class LocalStorageAdapter:
    """The two StoragePort methods local/ calls, writing under STORAGE_DIR."""

    def upload_file_object(self, file_key: str, file_obj, content_type=None, *a, **k):
        path = STORAGE_DIR / file_key.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(file_obj.read() if hasattr(file_obj, "read") else bytes(file_obj))
        return file_key

    def url(self, key: str) -> str:
        return f"{FILES_URL}/{key.lstrip('/')}"


bootstrap = types.ModuleType("bootstrap")
bootstrap.STORAGE_DIR = STORAGE_DIR
bootstrap.FILES_URL = FILES_URL
bootstrap.ALLOWED_HOSTS = set()
bootstrap.LocalStorageAdapter = LocalStorageAdapter
sys.modules["bootstrap"] = bootstrap
sys.path.insert(0, str(LOCAL_DIR))


@pytest.fixture(autouse=True)
def storage() -> Path:
    for child in STORAGE_DIR.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    return STORAGE_DIR


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any test that tries to open a connection fails, so a missed mock can't reach a real service."""

    def refuse(*a, **k):
        raise RuntimeError("tests must not use the network")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
