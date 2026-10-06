from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from collector.collect import NextDNSClient
from sverdrup.db import connect, init_db
from tests.nextdns_mock import API_KEY, PROFILE_ID, MockNextDNS


@pytest.fixture
def nextdns() -> Iterator[MockNextDNS]:
    server = MockNextDNS().start()
    yield server
    server.stop()


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "sverdrup.db")


@pytest.fixture
def conn(db_path: str) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(nextdns: MockNextDNS, sleeps: list[float]) -> NextDNSClient:
    return NextDNSClient(nextdns.url, API_KEY, page_size=10, sleep=sleeps.append)
