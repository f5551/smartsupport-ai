"""All behavioral tests use independent files in pytest's temporary directory."""

from pathlib import Path

import pytest

import database
import daily_cost


@pytest.fixture(autouse=True)
def isolate_daily_ledger(monkeypatch, tmp_path):
    monkeypatch.setattr(daily_cost, "DEFAULT_PATH", tmp_path / "daily-api-cost.sqlite3")


@pytest.fixture(autouse=True)
def protect_demo_database(monkeypatch):
    original = database.connect

    def test_connection(db_path=database.DEFAULT_DB_PATH, **kwargs):
        if Path(db_path).resolve() == database.DEFAULT_DB_PATH.resolve():
            raise AssertionError("Tests must never connect to the demo database")
        return original(db_path, **kwargs)

    monkeypatch.setattr(database, "connect", test_connection)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "isolated-test.sqlite3"
    database.init_database(path)
    database.seed_demo_data(path)
    return path
