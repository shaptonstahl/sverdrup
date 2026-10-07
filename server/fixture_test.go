package main

import (
	"database/sql"
	_ "embed"
	"path/filepath"
	"testing"
	"time"
)

//go:embed testdata/schema.sql
var schemaSQL string

// fixtureNow is Wednesday 2026-10-07: the week starts Monday 2026-10-05 and
// the month on 2026-10-01.
var fixtureNow = time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)

// Fixture service IDs.
const (
	netflixID = 1
	spotifyID = 2
	youtubeID = 3
)

// fixtureSQL is a small household. Netflix is used on 2026-09-30, 10-02 and
// 10-06 (gaps of 2 and 4 days, median 3); Spotify once this week; YouTube
// never. Two accounts each have a device named "Living Room TV", and one
// device name contains a colon.
const fixtureSQL = `
INSERT INTO services (id, service_code, service_name, domains) VALUES
  (1, 'netflix', 'Netflix', '["netflix.com"]'),
  (2, 'spotify', 'Spotify', '["spotify.com"]'),
  (3, 'youtube', 'YouTube', '["youtube.com"]');

INSERT INTO metrics (service_id, account, profile_id, date, total_minutes, session_count, last_seen) VALUES
  (1, 'home', 'p1', '2026-09-30', 20, 1, '2026-09-30T20:20:00.000Z'),
  (1, 'home', 'p1', '2026-10-02', 45, 1, '2026-10-02T19:45:00.000Z'),
  (1, 'home', 'p1', '2026-10-06', 60, 2, '2026-10-06T21:00:00.000Z'),
  (1, 'home', 'p2', '2026-10-06', 30, 1, '2026-10-06T22:00:00.000Z'),
  (2, 'family', 'p3', '2026-10-07', 10, 1, '2026-10-07T08:10:00.000Z');

INSERT INTO device_metrics (service_id, account, device_name, date, total_minutes, session_count, last_seen) VALUES
  (1, 'home', 'Living Room TV', '2026-10-02', 45, 1, '2026-10-02T19:45:00.000Z'),
  (1, 'home', 'Living Room TV', '2026-10-06', 60, 1, '2026-10-06T21:00:00.000Z'),
  (1, 'home', 'phone:a', '2026-10-06', 40, 2, '2026-10-06T22:00:00.000Z'),
  (2, 'family', 'Living Room TV', '2026-10-07', 10, 1, '2026-10-07T08:10:00.000Z');

INSERT INTO sessions (id, service_id, account, profile_ids, device_names, start_time, end_time, session_length_minutes) VALUES
  (1, 1, 'home', '["p1"]', '["Living Room TV"]', '2026-09-30T20:00:00.000Z', '2026-09-30T20:20:00.000Z', 20),
  (2, 1, 'home', '["p1"]', '["Living Room TV"]', '2026-10-02T19:00:00.000Z', '2026-10-02T19:45:00.000Z', 45),
  (3, 1, 'home', '["p1"]', '["Living Room TV"]', '2026-10-06T20:00:00.000Z', '2026-10-06T21:00:00.000Z', 60),
  (4, 1, 'home', '["p1","p2"]', '["Living Room TV","phone:a"]', '2026-10-06T21:30:00.000Z', '2026-10-06T22:00:00.000Z', 30),
  (5, 2, 'family', '["p3"]', '["<b>kid</b>"]', '2026-10-07T08:00:00.000Z', '2026-10-07T08:10:00.000Z', 10);
`

// newFixtureDB writes the pipeline schema and fixture rows to a new database
// in WAL mode, as the Python jobs leave it, and returns its path.
func newFixtureDB(t *testing.T, extra ...string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "sverdrup.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, stmt := range append([]string{"PRAGMA journal_mode=WAL", schemaSQL, fixtureSQL}, extra...) {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("fixture: %v", err)
		}
	}
	return path
}

// openFixture opens a read-only store on a fresh fixture database.
func openFixture(t *testing.T, extra ...string) *Store {
	t.Helper()
	store, err := OpenStore(newFixtureDB(t, extra...))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { store.Close() })
	return store
}
