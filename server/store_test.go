package main

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func date(s string) time.Time {
	t, err := time.Parse(dateLayout, s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestPeriodAt(t *testing.T) {
	cases := []struct {
		now                     time.Time
		today, week, monthStart string
	}{
		{fixtureNow, "2026-10-07", "2026-10-05", "2026-10-01"},
		{time.Date(2026, 10, 5, 0, 0, 0, 0, time.UTC), "2026-10-05", "2026-10-05", "2026-10-01"},                         // Monday
		{time.Date(2026, 10, 4, 23, 59, 0, 0, time.UTC), "2026-10-04", "2026-09-28", "2026-10-01"},                       // Sunday, week began last month
		{time.Date(2026, 3, 1, 9, 0, 0, 0, time.UTC), "2026-03-01", "2026-02-23", "2026-03-01"},                          // month start
		{time.Date(2026, 10, 7, 1, 0, 0, 0, time.FixedZone("UTC-5", -5*3600)), "2026-10-07", "2026-10-05", "2026-10-01"}, // local date, not UTC
	}
	for _, c := range cases {
		p := PeriodAt(c.now)
		got := []string{p.Today.Format(dateLayout), p.WeekStart.Format(dateLayout), p.MonthStart.Format(dateLayout)}
		want := []string{c.today, c.week, c.monthStart}
		if strings.Join(got, " ") != strings.Join(want, " ") {
			t.Errorf("PeriodAt(%v) = %v, want %v", c.now, got, want)
		}
	}
}

func TestServiceSummariesHousehold(t *testing.T) {
	store := openFixture(t)
	got, err := store.ServiceSummaries(context.Background(), PeriodAt(fixtureNow), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("got %d services, want 3", len(got))
	}
	// Ordered by minutes this month.
	if got[0].Code != "netflix" || got[1].Code != "spotify" || got[2].Code != "youtube" {
		t.Fatalf("order = %s, %s, %s", got[0].Code, got[1].Code, got[2].Code)
	}
	n := got[0]
	if n.TotalMinutes != 155 || n.SessionCount != 5 || n.ActiveDays != 3 {
		t.Errorf("netflix totals = %v min, %d sessions, %d days", n.TotalMinutes, n.SessionCount, n.ActiveDays)
	}
	if n.WeekMinutes != 90 || n.WeekSessions != 3 || n.MonthMinutes != 135 || n.MonthSessions != 4 {
		t.Errorf("netflix week/month = %v/%d, %v/%d", n.WeekMinutes, n.WeekSessions, n.MonthMinutes, n.MonthSessions)
	}
	if *n.FirstDate != "2026-09-30" || *n.LastDate != "2026-10-06" || *n.LastSeen != "2026-10-06T22:00:00.000Z" {
		t.Errorf("netflix dates = %s, %s, %s", *n.FirstDate, *n.LastDate, *n.LastSeen)
	}
	y := got[2]
	if y.TotalMinutes != 0 || y.SessionCount != 0 || y.FirstDate != nil || y.LastSeen != nil {
		t.Errorf("unused youtube = %+v", y)
	}
}

func TestServiceSummariesForDevice(t *testing.T) {
	store := openFixture(t)
	p := PeriodAt(fixtureNow)
	got, err := store.ServiceSummaries(context.Background(), p, &DeviceKey{Account: "home", Name: "Living Room TV"})
	if err != nil {
		t.Fatal(err)
	}
	byCode := map[string]ServiceSummary{}
	for _, s := range got {
		byCode[s.Code] = s
	}
	if n := byCode["netflix"]; n.TotalMinutes != 105 || n.WeekMinutes != 60 || n.MonthMinutes != 105 {
		t.Errorf("netflix on home TV = %+v", n)
	}
	// The family account's TV of the same name is a different device.
	if s := byCode["spotify"]; s.TotalMinutes != 0 {
		t.Errorf("spotify on home TV = %v minutes, want 0", s.TotalMinutes)
	}
	if len(got) != 3 {
		t.Errorf("got %d services, want all 3", len(got))
	}
}

func TestDeviceSummaries(t *testing.T) {
	store := openFixture(t)
	p := PeriodAt(fixtureNow)
	all, err := store.DeviceSummaries(context.Background(), p, nil)
	if err != nil {
		t.Fatal(err)
	}
	var keys []string
	for _, d := range all {
		keys = append(keys, d.Key)
	}
	want := "home:Living Room TV,home:phone:a,family:Living Room TV"
	if strings.Join(keys, ",") != want {
		t.Errorf("devices = %v, want %s", keys, want)
	}
	id := int64(spotifyID)
	one, err := store.DeviceSummaries(context.Background(), p, &id)
	if err != nil {
		t.Fatal(err)
	}
	if len(one) != 1 || one[0].Key != "family:Living Room TV" || one[0].WeekMinutes != 10 {
		t.Errorf("spotify devices = %+v", one)
	}
}

func TestDailyMetricsFillsEveryDay(t *testing.T) {
	store := openFixture(t)
	got, err := store.DailyMetrics(context.Background(), netflixID, date("2026-10-01"), date("2026-10-07"), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 7 || got[0].Date != "2026-10-01" || got[6].Date != "2026-10-07" {
		t.Fatalf("got %d days from %s", len(got), got[0].Date)
	}
	if got[1].TotalMinutes != 45 || got[5].TotalMinutes != 90 || got[5].SessionCount != 3 {
		t.Errorf("10-02 = %+v, 10-06 = %+v", got[1], got[5])
	}
	if got[5].LastSeen == nil || *got[5].LastSeen != "2026-10-06T22:00:00.000Z" {
		t.Errorf("10-06 last_seen = %v", got[5].LastSeen)
	}
	if got[0].TotalMinutes != 0 || got[0].LastSeen != nil {
		t.Errorf("empty day = %+v", got[0])
	}

	dev, err := store.DailyMetrics(context.Background(), netflixID, date("2026-10-06"), date("2026-10-06"),
		&DeviceKey{Account: "home", Name: "phone:a"})
	if err != nil {
		t.Fatal(err)
	}
	if len(dev) != 1 || dev[0].TotalMinutes != 40 || dev[0].SessionCount != 2 {
		t.Errorf("device day = %+v", dev)
	}
}

func TestMedianDaysBetweenUse(t *testing.T) {
	store := openFixture(t)
	ctx := context.Background()
	if gap, ok, err := store.MedianDaysBetweenUse(ctx, netflixID); err != nil || !ok || gap != 3 {
		t.Errorf("netflix median = %v, %v, %v; want 3", gap, ok, err)
	}
	if _, ok, err := store.MedianDaysBetweenUse(ctx, spotifyID); err != nil || ok {
		t.Errorf("one day of use gave a median (ok=%v, err=%v)", ok, err)
	}

	odd := openFixture(t, `INSERT INTO metrics (service_id, account, profile_id, date, total_minutes, session_count, last_seen)
		VALUES (1, 'home', 'p1', '2026-10-07', 5, 1, '2026-10-07T09:00:00.000Z'),
		       (1, 'home', 'p1', '2026-10-05', 0, 0, '2026-10-05T09:00:00.000Z')`)
	// Gaps 2, 4, 1 (a zero-usage day does not count as use): median 2.
	if gap, ok, err := odd.MedianDaysBetweenUse(ctx, netflixID); err != nil || !ok || gap != 2 {
		t.Errorf("odd median = %v, %v, %v; want 2", gap, ok, err)
	}
}

func TestRecentSessions(t *testing.T) {
	store := openFixture(t)
	ctx := context.Background()
	all, err := store.RecentSessions(ctx, nil, 50)
	if err != nil {
		t.Fatal(err)
	}
	var ids []int64
	for _, s := range all {
		ids = append(ids, s.ID)
	}
	if len(ids) != 5 || ids[0] != 5 || ids[4] != 1 {
		t.Errorf("session order = %v, want newest first", ids)
	}
	if all[1].ServiceName != "Netflix" || strings.Join(all[1].ProfileIDs, ",") != "p1,p2" ||
		strings.Join(all[1].DeviceNames, ",") != "Living Room TV,phone:a" {
		t.Errorf("session 4 = %+v", all[1])
	}
	id := int64(netflixID)
	two, err := store.RecentSessions(ctx, &id, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(two) != 2 || two[0].ID != 4 || two[1].ID != 3 {
		t.Errorf("netflix limit 2 = %+v", two)
	}
}

func TestStoreIsReadOnly(t *testing.T) {
	store := openFixture(t)
	_, err := store.db.Exec("DELETE FROM sessions")
	if err == nil {
		t.Fatal("delete through the dashboard's connection succeeded")
	}
	if !strings.Contains(strings.ToLower(err.Error()), "readonly") && !strings.Contains(strings.ToLower(err.Error()), "read-only") {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestMissingDatabaseIsNoDataAndNotCreated(t *testing.T) {
	path := filepath.Join(t.TempDir(), "absent.db")
	store, err := OpenStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	_, err = store.ServiceSummaries(context.Background(), PeriodAt(fixtureNow), nil)
	if !errors.Is(err, ErrNoData) {
		t.Fatalf("err = %v, want ErrNoData", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("the dashboard created %s", path)
	}
}

func TestEmptyDatabaseIsNoData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "empty.db")
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("PRAGMA user_version = 0; CREATE TABLE other (x)"); err != nil {
		t.Fatal(err)
	}
	db.Close()
	store, err := OpenStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.RecentSessions(context.Background(), nil, 5); !errors.Is(err, ErrNoData) {
		t.Errorf("err = %v, want ErrNoData", err)
	}
}

func TestReadsWhileProcessorWrites(t *testing.T) {
	path := newFixtureDB(t)
	writer, err := sql.Open("sqlite", path+"?_pragma=busy_timeout(5000)")
	if err != nil {
		t.Fatal(err)
	}
	defer writer.Close()
	tx, err := writer.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec("DELETE FROM sessions"); err != nil {
		t.Fatal(err)
	}
	store, err := OpenStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	// WAL lets the reader see the last committed state during the write.
	got, err := store.RecentSessions(context.Background(), nil, 50)
	if err != nil || len(got) != 5 {
		t.Fatalf("during write: %d sessions, err %v", len(got), err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	got, err = store.RecentSessions(context.Background(), nil, 50)
	if err != nil || len(got) != 0 {
		t.Fatalf("after commit: %d sessions, err %v", len(got), err)
	}
}

func TestReadOnlyDSNEscapesPath(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "odd ?#% dir")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	src := newFixtureDB(t)
	data, err := os.ReadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "sverdrup.db")
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
	store, err := OpenStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	if _, err := store.Service(context.Background(), netflixID); err != nil {
		t.Errorf("open %q: %v", path, err)
	}
}

func TestServiceNotFound(t *testing.T) {
	store := openFixture(t)
	if _, err := store.Service(context.Background(), 99); !errors.Is(err, ErrNotFound) {
		t.Errorf("err = %v, want ErrNotFound", err)
	}
}

func TestParseDeviceKey(t *testing.T) {
	k, err := ParseDeviceKey("home:phone:a")
	if err != nil || k.Account != "home" || k.Name != "phone:a" {
		t.Errorf("ParseDeviceKey = %+v, %v", k, err)
	}
	for _, bad := range []string{"home", ":tv", "home:", ""} {
		if _, err := ParseDeviceKey(bad); err == nil {
			t.Errorf("ParseDeviceKey(%q) accepted", bad)
		}
	}
}

func TestUnopenableDatabaseIsAFault(t *testing.T) {
	// A path that exists but is not a database file must not read as "no data yet".
	store, err := OpenStore(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	_, err = store.ServiceSummaries(context.Background(), PeriodAt(fixtureNow), nil)
	if err == nil || errors.Is(err, ErrNoData) {
		t.Fatalf("err = %v, want a fault other than ErrNoData", err)
	}
}
