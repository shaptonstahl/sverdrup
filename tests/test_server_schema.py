"""The Go dashboard's test fixture must use the schema the pipeline writes."""

from pathlib import Path

from sverdrup.db import SCHEMA

GO_SCHEMA = (
    Path(__file__).resolve().parent.parent / "server" / "testdata" / "schema.sql"
)


def test_go_test_schema_matches_pipeline_schema():
    lines = GO_SCHEMA.read_text().splitlines()
    body = "\n".join(
        line
        for line in lines
        if not line.startswith("-- Copy of") and not line.startswith("-- tests/")
    )
    assert (
        body.strip() == SCHEMA.strip()
    ), f"{GO_SCHEMA} is out of date; copy SCHEMA from sverdrup/db.py into it"
