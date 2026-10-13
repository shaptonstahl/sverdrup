package main

import (
	"context"
	"net/http"
	"strings"
	"testing"
)

// groupsSQL adds named profiles and two groups to the fixture. HapLan is
// every profile except Julie (p2); Others is Julie alone. HapLan's sessions
// leave out p2, so on 2026-10-06 it has p1's two sessions (60 minutes) and
// Others has p2's own (30 minutes).
const groupsSQL = `
INSERT INTO profiles (profile_id, account, name, updated_at) VALUES
  ('p1', 'home', 'Stephen', '2026-10-07T00:00:00.000Z'),
  ('p2', 'home', 'Julie', '2026-10-07T00:00:00.000Z'),
  ('p3', 'family', 'Den', '2026-10-07T00:00:00.000Z');

INSERT INTO profile_groups (name, position, rule, entries) VALUES
  ('Others', 1, 'include', '["Julie"]'),
  ('HapLan', 0, 'exclude', '["Julie"]');

INSERT INTO group_members (group_name, profile_id) VALUES
  ('HapLan', 'p1'), ('HapLan', 'p3'), ('Others', 'p2');

INSERT INTO group_metrics (service_id, group_name, date, total_minutes, session_count, last_seen) VALUES
  (1, 'HapLan', '2026-09-30', 20, 1, '2026-09-30T20:20:00.000Z'),
  (1, 'HapLan', '2026-10-02', 45, 1, '2026-10-02T19:45:00.000Z'),
  (1, 'HapLan', '2026-10-06', 60, 2, '2026-10-06T21:00:00.000Z'),
  (2, 'HapLan', '2026-10-07', 10, 1, '2026-10-07T08:10:00.000Z'),
  (1, 'Others', '2026-10-06', 30, 1, '2026-10-06T22:00:00.000Z');

INSERT INTO group_sessions (id, service_id, group_name, profile_ids, device_names, start_time, end_time, session_length_minutes) VALUES
  (1, 1, 'HapLan', '["p1"]', '["Living Room TV"]', '2026-10-02T19:00:00.000Z', '2026-10-02T19:45:00.000Z', 45),
  (2, 1, 'HapLan', '["p1","p3"]', '["Living Room TV"]', '2026-10-06T20:00:00.000Z', '2026-10-06T21:00:00.000Z', 60),
  (3, 1, 'Others', '["p2"]', '["phone:a"]', '2026-10-06T21:30:00.000Z', '2026-10-06T22:00:00.000Z', 30),
  (4, 2, 'HapLan', '["p3"]', '["<b>kid</b>"]', '2026-10-07T08:00:00.000Z', '2026-10-07T08:10:00.000Z', 10);
`

func TestGroupsInConfiguredOrderWithNamedMembers(t *testing.T) {
	groups, err := openFixture(t, groupsSQL).Groups(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(groups) != 2 || groups[0].Name != "HapLan" || groups[1].Name != "Others" {
		t.Fatalf("groups = %+v", groups)
	}
	h := groups[0]
	if h.Rule != "exclude" || strings.Join(h.Entries, ",") != "Julie" || len(h.Profiles) != 2 ||
		h.Profiles[0] != (Profile{ID: "p3", Name: "Den", Account: "family"}) || h.Profiles[1].Name != "Stephen" {
		t.Errorf("HapLan = %+v", h)
	}
}

func TestStoreScopedToAGroup(t *testing.T) {
	store := openFixture(t, groupsSQL)
	ctx := context.Background()
	haplan := Scope{Group: "HapLan"}

	got, err := store.ServiceSummaries(ctx, PeriodAt(fixtureNow), haplan)
	if err != nil {
		t.Fatal(err)
	}
	if n := got[0]; n.Code != "netflix" || n.TotalMinutes != 125 || n.SessionCount != 4 || n.WeekMinutes != 60 || n.MonthMinutes != 105 {
		t.Errorf("HapLan netflix = %+v", n)
	}

	days, err := store.DailyMetrics(ctx, netflixID, date("2026-10-06"), date("2026-10-06"), Scope{Group: "Others"})
	if err != nil {
		t.Fatal(err)
	}
	if days[0].TotalMinutes != 30 || days[0].SessionCount != 1 {
		t.Errorf("Others 10-06 = %+v", days[0])
	}

	// Others used Netflix on one day only.
	if _, ok, err := store.MedianDaysBetweenUse(ctx, netflixID, Scope{Group: "Others"}); err != nil || ok {
		t.Errorf("Others median ok=%v err=%v", ok, err)
	}
	if gap, ok, err := store.MedianDaysBetweenUse(ctx, netflixID, haplan); err != nil || !ok || gap != 3 {
		t.Errorf("HapLan median = %v, %v, %v", gap, ok, err)
	}

	sessions, err := store.RecentSessions(ctx, nil, 50, "HapLan")
	if err != nil {
		t.Fatal(err)
	}
	if len(sessions) != 3 || sessions[1].Group != "HapLan" || sessions[1].Account != "" ||
		strings.Join(sessions[1].ProfileNames, ",") != "Stephen,Den" {
		t.Errorf("HapLan sessions = %+v", sessions)
	}
}

func TestProfileSummariesNameEveryProfile(t *testing.T) {
	store := openFixture(t, groupsSQL, `INSERT INTO profiles (profile_id, account, name, updated_at)
		VALUES ('q9', 'home', NULL, '2026-10-07T00:00:00.000Z')`)
	got, err := store.ProfileSummaries(context.Background(), PeriodAt(fixtureNow))
	if err != nil {
		t.Fatal(err)
	}
	var names []string
	for _, p := range got {
		names = append(names, p.Name)
	}
	// By minutes this month; a profile without metrics is still listed, by ID.
	if strings.Join(names, ",") != "Stephen,Julie,Den,q9" || got[0].MonthMinutes != 105 || got[3].TotalMinutes != 0 {
		t.Errorf("profiles = %+v", got)
	}
}

func TestDatabaseFromBeforeGroups(t *testing.T) {
	store := openFixture(t, "DROP TABLE profiles; DROP TABLE group_members; DROP TABLE profile_groups")
	ctx := context.Background()
	if groups, err := store.Groups(ctx); err != nil || groups != nil {
		t.Errorf("groups = %v, %v", groups, err)
	}
	profiles, err := store.ProfileSummaries(ctx, PeriodAt(fixtureNow))
	if err != nil || len(profiles) != 3 || profiles[0].Name != "p1" || profiles[0].Account != "home" {
		t.Errorf("profiles = %+v, %v", profiles, err)
	}
	sessions, err := store.RecentSessions(ctx, nil, 1, "")
	if err != nil || strings.Join(sessions[0].ProfileNames, ",") != "p3" {
		t.Errorf("sessions = %+v, %v", sessions, err)
	}
	h := serverFor(t, store)
	wantStatus(t, get(t, h, "/"), http.StatusOK)
	wantStatus(t, get(t, h, "/services/1"), http.StatusOK)
}

func TestOverviewDefaultsToTheFirstGroup(t *testing.T) {
	h := serverFor(t, openFixture(t, groupsSQL))
	rec := get(t, h, "/")
	wantStatus(t, rec, http.StatusOK)
	wantContains(t, rec.Body.String(),
		`<option value="group:HapLan" selected>HapLan</option>`,
		`<option value="group:Others">Others</option>`,
		`<option value="all">All profiles</option>`,
		`HapLan <span class="muted">profile group</span>`,
		"1h 10m", // HapLan this week: 60 + 10 minutes
		`<a href="/services/1?group=HapLan">Netflix</a>`,
		"every profile except Julie",
		"<td>Den <span class=\"muted\">p3 family</span></td>",
	)

	all := get(t, h, "/?view=all")
	wantContains(t, all.Body.String(), `<option value="all" selected>`, "1h 40m", `<a href="/services/1?group=all">Netflix</a>`)

	others := get(t, h, "/?group=others", "HX-Request", "true") // names match without regard to case
	wantStatus(t, others, http.StatusOK)
	wantContains(t, others.Body.String(), "Others", "30m")

	wantStatus(t, get(t, h, "/?group=Nobody"), http.StatusBadRequest)
}

func TestServicePageForAGroup(t *testing.T) {
	h := serverFor(t, openFixture(t, groupsSQL))
	rec := get(t, h, "/services/1")
	wantStatus(t, rec, http.StatusOK)
	body := rec.Body.String()
	wantContains(t, body,
		`<a href="/services/1?group=HapLan" aria-current="true">HapLan</a>`,
		"2h 05m",       // HapLan all time: 125 minutes
		"Stephen, Den", // a session's profiles, by name
		`href="/services/1?days=90&amp;group=HapLan"`,
		"Devices in every profile",
	)
	if strings.Contains(body, "Julie") {
		t.Error("HapLan's sessions include Julie's")
	}

	all := get(t, h, "/services/1?group=all&days=90")
	wantStatus(t, all, http.StatusOK)
	wantContains(t, all.Body.String(), `<a href="/services/1?group=all" aria-current="true">All profiles</a>`,
		"2h 35m", "home: Stephen, Julie")

	wantStatus(t, get(t, h, "/services/1?device=home:phone:a"), http.StatusBadRequest)
	wantStatus(t, get(t, h, "/services/1?group=Nobody"), http.StatusBadRequest)
}

func TestAPIForGroups(t *testing.T) {
	h := serverFor(t, openFixture(t, groupsSQL))

	def := decode[apiServicesResponse](t, get(t, h, "/api/services"))
	if def.Scope != "group" || def.Group == nil || *def.Group != "HapLan" || def.Services[0].TotalMinutes != 125 {
		t.Errorf("default services = %+v", def)
	}
	all := decode[apiServicesResponse](t, get(t, h, "/api/services?group=all"))
	if all.Scope != "all" || all.Group != nil || all.Services[0].TotalMinutes != 155 {
		t.Errorf("all services = %+v", all)
	}

	m := decode[apiMetricsResponse](t, get(t, h, "/api/services/1/metrics?start=2026-10-06&end=2026-10-06&group=Others"))
	if *m.Group != "Others" || m.Metrics[0].TotalMinutes != 30 {
		t.Errorf("Others metrics = %+v", m)
	}

	type sessionsResp struct {
		Group    *string   `json:"group"`
		Sessions []Session `json:"sessions"`
	}
	s := decode[sessionsResp](t, get(t, h, "/api/sessions?service=1"))
	if s.Group == nil || *s.Group != "HapLan" || len(s.Sessions) != 2 || s.Sessions[0].Group != "HapLan" {
		t.Errorf("HapLan sessions = %+v", s)
	}
	if raw := get(t, h, "/api/sessions?limit=1").Body.String(); strings.Contains(raw, `"account"`) {
		t.Errorf("group session JSON has an account: %s", raw)
	}
	wantStatus(t, get(t, h, "/api/sessions?device=home:phone:a"), http.StatusBadRequest)

	type groupsResp struct {
		Default *string `json:"default"`
		Groups  []Group `json:"groups"`
	}
	g := decode[groupsResp](t, get(t, h, "/api/groups"))
	if g.Default == nil || *g.Default != "HapLan" || len(g.Groups) != 2 || len(g.Groups[0].Profiles) != 2 {
		t.Errorf("groups = %+v", g)
	}

	type profilesResp struct {
		Profiles []ProfileSummary `json:"profiles"`
	}
	p := decode[profilesResp](t, get(t, h, "/api/profiles"))
	if len(p.Profiles) != 3 || p.Profiles[1].Name != "Julie" || strings.Join(p.Profiles[1].Groups, ",") != "Others" ||
		strings.Join(p.Profiles[0].Groups, ",") != "HapLan" {
		t.Errorf("profiles = %+v", p.Profiles)
	}
}

func TestAPIGroupsWithoutGroups(t *testing.T) {
	rec := get(t, newTestServer(t), "/api/groups")
	wantStatus(t, rec, http.StatusOK)
	if got := strings.TrimSpace(rec.Body.String()); got != `{"default":null,"groups":[]}` {
		t.Errorf("groups = %s", got)
	}
}
