package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// newTestServer serves a fixture database with the clock at fixtureNow, in UTC.
func newTestServer(t *testing.T) http.Handler {
	t.Helper()
	return serverFor(t, openFixture(t))
}

func serverFor(t *testing.T, store *Store) http.Handler {
	t.Helper()
	srv, err := NewServer(store)
	if err != nil {
		t.Fatal(err)
	}
	srv.now = func() time.Time { return fixtureNow }
	srv.loc = time.UTC
	return srv.Handler()
}

func get(t *testing.T, h http.Handler, target string, headers ...string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, nil)
	for i := 0; i+1 < len(headers); i += 2 {
		req.Header.Set(headers[i], headers[i+1])
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decode[T any](t *testing.T, rec *httptest.ResponseRecorder) T {
	t.Helper()
	if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
		t.Fatalf("Content-Type = %q", ct)
	}
	var v T
	if err := json.Unmarshal(rec.Body.Bytes(), &v); err != nil {
		t.Fatalf("decode %s: %v", rec.Body, err)
	}
	return v
}

func wantStatus(t *testing.T, rec *httptest.ResponseRecorder, status int) {
	t.Helper()
	if rec.Code != status {
		t.Fatalf("status = %d, want %d; body: %s", rec.Code, status, rec.Body)
	}
}

func wantContains(t *testing.T, body string, parts ...string) {
	t.Helper()
	for _, p := range parts {
		if !strings.Contains(body, p) {
			t.Errorf("body lacks %q", p)
		}
	}
}

func TestOverviewPage(t *testing.T) {
	rec := get(t, newTestServer(t), "/")
	wantStatus(t, rec, http.StatusOK)
	body := rec.Body.String()
	wantContains(t, body,
		"<!doctype html>", `src="/static/htmx.min.js?v=`, `href="/static/style.css?v=`, "Whole household",
		`<a href="/services/1">Netflix</a>`, `<a href="/services/3">YouTube</a>`,
		"1h 40m",         // household this week: 90 + 10 minutes
		"2h 25m",         // household this month: 135 + 10 minutes
		"from Mon 5 Oct", // week start
		`<option value="home:phone:a">phone:a (home)</option>`,
		`href="/?device=home%3aLiving%20Room%20TV"`,
		"Tue 6 Oct 2026 22:00", // Netflix last seen, in the server zone
	)
	if !strings.Contains(rec.Header().Get("Vary"), "HX-Request") {
		t.Error("overview does not vary on HX-Request")
	}
}

func TestOverviewDeviceFilter(t *testing.T) {
	h := newTestServer(t)
	rec := get(t, h, "/?device=home:Living+Room+TV")
	wantStatus(t, rec, http.StatusOK)
	wantContains(t, rec.Body.String(),
		`<option value="home:Living Room TV" selected>`,
		"1h 45m", // Netflix on that TV this month: 45 + 60
	)

	frag := get(t, h, "/?device=home:phone:a", "HX-Request", "true")
	wantStatus(t, frag, http.StatusOK)
	body := frag.Body.String()
	if strings.Contains(body, "<html") || !strings.HasPrefix(strings.TrimSpace(body), `<section id="services-panel">`) {
		t.Errorf("htmx request got more than the panel: %.120s", body)
	}
	wantContains(t, body, "phone:a", "40m")

	restore := get(t, h, "/?device=home:phone:a", "HX-Request", "true", "HX-History-Restore-Request", "true")
	wantStatus(t, restore, http.StatusOK)
	wantContains(t, restore.Body.String(), "<!doctype html>", `<option value="home:phone:a" selected>`, `<section id="services-panel">`)

	wantStatus(t, get(t, h, "/?device=nocolon"), http.StatusBadRequest)
}

func TestOverviewEscapesDeviceNames(t *testing.T) {
	store := openFixture(t, `INSERT INTO device_metrics (service_id, account, device_name, date, total_minutes, session_count, last_seen)
		VALUES (1, 'home', '<script>alert(1)</script>', '2026-10-06', 5, 1, '2026-10-06T21:00:00.000Z')`)
	rec := get(t, serverFor(t, store), "/")
	wantStatus(t, rec, http.StatusOK)
	if strings.Contains(rec.Body.String(), "<script>alert") {
		t.Error("device name rendered unescaped")
	}
	wantContains(t, rec.Body.String(), "&lt;script&gt;alert(1)&lt;/script&gt;")
}

func TestServicePage(t *testing.T) {
	h := newTestServer(t)
	rec := get(t, h, "/services/1")
	wantStatus(t, rec, http.StatusOK)
	body := rec.Body.String()
	wantContains(t, body,
		"<h1>Netflix</h1>", "Daily viewing", "3 days", // median days between use
		"2h 35m", // all time: 155 minutes
		`<svg class="spark" viewBox="0 0 120 40"`, // 30 days of 4 units
		"Tue 6 Oct 2026", "1h 30m", // daily row for 10-06
		"Living Room TV, phone:a", "home: p1, p2", // recent session devices and profiles
		`aria-current="true"`,
	)
	if strings.Contains(body, "Spotify") {
		t.Error("service page lists another service's sessions")
	}

	frag := get(t, h, "/services/1?days=90", "HX-Request", "true")
	wantStatus(t, frag, http.StatusOK)
	if !strings.HasPrefix(strings.TrimSpace(frag.Body.String()), `<section id="history-panel">`) {
		t.Errorf("htmx history request got: %.120s", frag.Body)
	}
	wantContains(t, frag.Body.String(), `viewBox="0 0 360 40"`, "Wed 30 Sep 2026")

	restore := get(t, h, "/services/1?days=90", "HX-Request", "true", "HX-History-Restore-Request", "true")
	wantStatus(t, restore, http.StatusOK)
	wantContains(t, restore.Body.String(), "<!doctype html>", "<h1>Netflix</h1>", `<section id="history-panel">`)

	wantStatus(t, get(t, h, "/services/1?days=7"), http.StatusBadRequest)
	wantStatus(t, get(t, h, "/services/99"), http.StatusNotFound)
	wantStatus(t, get(t, h, "/services/abc"), http.StatusNotFound)
}

func TestServicePageEscapesSessionDevices(t *testing.T) {
	rec := get(t, newTestServer(t), "/services/2")
	wantStatus(t, rec, http.StatusOK)
	wantContains(t, rec.Body.String(), "&lt;b&gt;kid&lt;/b&gt;")
	if strings.Contains(rec.Body.String(), "<b>kid</b>") {
		t.Error("session device name rendered unescaped")
	}
}

func TestAPIServices(t *testing.T) {
	h := newTestServer(t)
	rec := get(t, h, "/api/services")
	wantStatus(t, rec, http.StatusOK)
	resp := decode[apiServicesResponse](t, rec)
	if resp.Today != "2026-10-07" || resp.WeekStart != "2026-10-05" || resp.MonthStart != "2026-10-01" || resp.Device != nil {
		t.Errorf("period = %+v", resp)
	}
	if len(resp.Services) != 3 || resp.Services[0].Code != "netflix" || resp.Services[0].TotalMinutes != 155 {
		t.Errorf("services = %+v", resp.Services)
	}

	// Unused services encode null dates, not empty strings.
	var raw struct {
		Services []map[string]any `json:"services"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &raw); err != nil {
		t.Fatal(err)
	}
	if v, ok := raw.Services[2]["last_seen"]; !ok || v != nil {
		t.Errorf("youtube last_seen = %v (present %v), want null", v, ok)
	}

	dev := decode[apiServicesResponse](t, get(t, h, "/api/services?device=family:Living%20Room%20TV"))
	if dev.Device == nil || *dev.Device != "family:Living Room TV" || dev.Services[0].Code != "spotify" {
		t.Errorf("device services = %+v", dev)
	}
	wantStatus(t, get(t, h, "/api/services?device=bad"), http.StatusBadRequest)
}

func TestAPIMetrics(t *testing.T) {
	h := newTestServer(t)

	def := decode[apiMetricsResponse](t, get(t, h, "/api/services/1/metrics"))
	if def.Start != "2026-09-08" || def.End != "2026-10-07" || len(def.Metrics) != 30 {
		t.Errorf("default range = %s..%s, %d days", def.Start, def.End, len(def.Metrics))
	}
	if def.Service.Code != "netflix" {
		t.Errorf("service = %+v", def.Service)
	}

	rec := get(t, h, "/api/services/1/metrics?start=2026-10-05&end=2026-10-06")
	wantStatus(t, rec, http.StatusOK)
	got := decode[apiMetricsResponse](t, rec)
	if len(got.Metrics) != 2 || got.Metrics[1].Date != "2026-10-06" || got.Metrics[1].TotalMinutes != 90 || got.Metrics[0].TotalMinutes != 0 {
		t.Errorf("metrics = %+v", got.Metrics)
	}

	onlyStart := decode[apiMetricsResponse](t, get(t, h, "/api/services/1/metrics?start=2026-10-01"))
	if onlyStart.End != "2026-10-07" || len(onlyStart.Metrics) != 7 {
		t.Errorf("start only = %s..%s", onlyStart.Start, onlyStart.End)
	}

	dev := decode[apiMetricsResponse](t, get(t, h, "/api/services/1/metrics?start=2026-10-06&end=2026-10-06&device=home:phone:a"))
	if len(dev.Metrics) != 1 || dev.Metrics[0].TotalMinutes != 40 {
		t.Errorf("device metrics = %+v", dev.Metrics)
	}

	for target, status := range map[string]int{
		"/api/services/1/metrics?start=2026-13-01":                http.StatusBadRequest,
		"/api/services/1/metrics?end=yesterday":                   http.StatusBadRequest,
		"/api/services/1/metrics?start=2026-10-07&end=2026-10-01": http.StatusBadRequest,
		"/api/services/1/metrics?start=2000-01-01&end=2026-10-01": http.StatusBadRequest,
		"/api/services/1/metrics?device=x":                        http.StatusBadRequest,
		"/api/services/99/metrics":                                http.StatusNotFound,
		"/api/services/0/metrics":                                 http.StatusNotFound,
		"/api/services/1/metrics?start=2016-10-12&end=2026-10-07": http.StatusOK, // 3648 days
	} {
		rec := get(t, h, target)
		if rec.Code != status {
			t.Errorf("%s: status %d, want %d (%s)", target, rec.Code, status, rec.Body)
		}
		if status != http.StatusOK {
			if e := decode[map[string]string](t, rec); e["error"] == "" {
				t.Errorf("%s: no error message", target)
			}
		}
	}
}

func TestAPISessions(t *testing.T) {
	h := newTestServer(t)
	type resp struct {
		Sessions []Session `json:"sessions"`
	}
	all := decode[resp](t, get(t, h, "/api/sessions"))
	if len(all.Sessions) != 5 || all.Sessions[0].ServiceCode != "spotify" {
		t.Errorf("sessions = %+v", all.Sessions)
	}
	nf := decode[resp](t, get(t, h, "/api/sessions?service=1&limit=2"))
	if len(nf.Sessions) != 2 || nf.Sessions[0].ID != 4 || nf.Sessions[0].Minutes != 30 ||
		nf.Sessions[0].StartTime != "2026-10-06T21:30:00.000Z" {
		t.Errorf("netflix sessions = %+v", nf.Sessions)
	}
	none := get(t, h, "/api/sessions?service=3")
	wantStatus(t, none, http.StatusOK)
	if !strings.Contains(none.Body.String(), `"sessions":[]`) {
		t.Errorf("empty sessions = %s", none.Body)
	}
	for target, status := range map[string]int{
		"/api/sessions?limit=0":    http.StatusBadRequest,
		"/api/sessions?limit=501":  http.StatusBadRequest,
		"/api/sessions?limit=ten":  http.StatusBadRequest,
		"/api/sessions?service=x":  http.StatusBadRequest,
		"/api/sessions?service=99": http.StatusNotFound,
		"/api/sessions?limit=500":  http.StatusOK,
	} {
		if rec := get(t, h, target); rec.Code != status {
			t.Errorf("%s: status %d, want %d", target, rec.Code, status)
		}
	}
}

func TestAPIDevices(t *testing.T) {
	type resp struct {
		Devices []DeviceSummary `json:"devices"`
	}
	got := decode[resp](t, get(t, newTestServer(t), "/api/devices"))
	if len(got.Devices) != 3 || got.Devices[0].Key != "home:Living Room TV" || got.Devices[0].MonthMinutes != 105 {
		t.Errorf("devices = %+v", got.Devices)
	}
}

func TestNoDataYet(t *testing.T) {
	store, err := OpenStore(filepath.Join(t.TempDir(), "missing.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	h := serverFor(t, store)
	page := get(t, h, "/")
	wantStatus(t, page, http.StatusServiceUnavailable)
	wantContains(t, page.Body.String(), "No data yet")
	for _, target := range []string{"/api/services", "/api/sessions", "/api/services/1/metrics", "/api/devices"} {
		rec := get(t, h, target)
		wantStatus(t, rec, http.StatusServiceUnavailable)
		if e := decode[map[string]string](t, rec); !strings.HasPrefix(e["error"], "no data yet") {
			t.Errorf("%s: error = %q", target, e["error"])
		}
	}
	wantStatus(t, get(t, h, "/health"), http.StatusOK)
}

func TestRoutingAndHeaders(t *testing.T) {
	h := newTestServer(t)

	health := get(t, h, "/health")
	wantStatus(t, health, http.StatusOK)
	if decode[map[string]string](t, health)["status"] != "ok" {
		t.Errorf("health = %s", health.Body)
	}

	js := get(t, h, "/static/htmx.min.js")
	wantStatus(t, js, http.StatusOK)
	if ct := js.Header().Get("Content-Type"); !strings.Contains(ct, "javascript") {
		t.Errorf("htmx Content-Type = %q", ct)
	}
	wantContains(t, js.Body.String(), `version:"2.0.11"`)
	wantStatus(t, get(t, h, "/static/style.css"), http.StatusOK)

	missing := get(t, h, "/nope")
	wantStatus(t, missing, http.StatusNotFound)
	wantContains(t, missing.Body.String(), "Page not found")

	req := httptest.NewRequest(http.MethodPost, "/api/services", nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	wantStatus(t, rec, http.StatusMethodNotAllowed)

	csp := health.Header().Get("Content-Security-Policy")
	if !strings.Contains(csp, "default-src 'self'") || !strings.Contains(csp, "frame-ancestors 'none'") {
		t.Errorf("CSP = %q", csp)
	}
	if health.Header().Get("X-Content-Type-Options") != "nosniff" {
		t.Error("missing nosniff")
	}
}

func TestFormatMinutes(t *testing.T) {
	for in, want := range map[float64]string{
		0: "0m", -1: "0m", 0.4: "<1m", 1: "1m", 59.4: "59m", 59.6: "1h 00m", 61: "1h 01m", 155: "2h 35m", 600: "10h 00m",
	} {
		if got := formatMinutes(in); got != want {
			t.Errorf("formatMinutes(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestSparkBars(t *testing.T) {
	bars := sparkBars([]DayMetric{
		{Date: "2026-10-01", TotalMinutes: 0},
		{Date: "2026-10-02", TotalMinutes: 100},
		{Date: "2026-10-03", TotalMinutes: 0.1},
	}, 100)
	if len(bars) != 3 || bars[0].H != 0 || bars[1].H != sparkHeight || bars[1].Y != 0 || bars[1].X != sparkStep {
		t.Errorf("bars = %+v", bars)
	}
	if bars[2].H != 1 {
		t.Errorf("a tiny day should still show: %+v", bars[2])
	}
	if bars[1].Label != "Fri 2 Oct 2026: 1h 40m" {
		t.Errorf("label = %q", bars[1].Label)
	}
}

func TestPlural(t *testing.T) {
	if got := plural(1, "session"); got != "1 session" {
		t.Errorf("plural(1) = %q", got)
	}
	if got := plural(0, "day"); got != "0 days" {
		t.Errorf("plural(0) = %q", got)
	}
}
