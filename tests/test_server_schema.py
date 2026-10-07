"""The Go dashboard's test fixture must use the schema the pipeline writes."""

import sqlite3
from pathlib import Path

from sverdrup.db import SCHEMA

GO_SCHEMA = (
    Path(__file__).resolve().parent.parent / "server" / "testdata" / "schema.sql"
)


def _structure(script):
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(script)
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        structure = {}
        for table in tables:
            indexes = {}
            for _, name, unique, origin, partial in conn.execute(
                f"PRAGMA index_list('{table}')"
            ):
                key = name if origin == "c" else origin
                columns = [
                    row[2] for row in conn.execute(f"PRAGMA index_info('{name}')")
                ]
                indexes[key, tuple(columns)] = (unique, partial)
            structure[table] = {
                "columns": conn.execute(f"PRAGMA table_info('{table}')").fetchall(),
                "indexes": indexes,
                "foreign_keys": conn.execute(
                    f"PRAGMA foreign_key_list('{table}')"
                ).fetchall(),
            }
        return structure
    finally:
        conn.close()


def test_go_test_schema_matches_pipeline_schema():
    assert _structure(GO_SCHEMA.read_text()) == _structure(
        SCHEMA
    ), f"{GO_SCHEMA} is out of date; copy SCHEMA from sverdrup/db.py into it"
