package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"math"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"modernc.org/sqlite"
)

// dateLayout is the form of the metrics tables' local calendar dates.
const dateLayout = "2006-01-02"

var (
	// ErrNotFound means the requested service does not exist.
	ErrNotFound = errors.New("not found")
	// ErrNoData means the database or its tables do not exist yet: the
	// collector and processor create them on their first run.
	ErrNoData = errors.New("no data yet")
)

// Store reads the SQLite database the Python collector and processor write.
// It never writes: the connection is opened read-only and query-only.
type Store struct {
	db   *sql.DB
	path string
}

// OpenStore opens the database at path read-only. The file need not exist
// yet; queries return ErrNoData until it does.
func OpenStore(path string) (*Store, error) {
	dsn, err := readOnlyDSN(path)
	if err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(4)
	db.SetConnMaxIdleTime(5 * time.Minute)
	return &Store{db: db, path: path}, nil
}

// Close releases the database handle.
func (s *Store) Close() error {
	return s.db.Close()
}

// readOnlyDSN builds a SQLite URI that opens path read-only (mode=ro, so a
// missing file is not created) and refuses writes on the connection
// (query_only), with a busy timeout so reads wait out the processor's writes.
func readOnlyDSN(path string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", fmt.Errorf("resolve DB_PATH %q: %w", path, err)
	}
	q := url.Values{}
	q.Set("mode", "ro")
	q.Add("_pragma", "busy_timeout(5000)")
	q.Add("_pragma", "query_only(1)")
	// The driver hands a "file:" name to SQLite whole, so SQLite applies mode=ro;
	// the URL encoding escapes any '?', '#' or '%' in the path.
	u := url.URL{Scheme: "file", Path: filepath.ToSlash(abs), RawQuery: q.Encode()}
	return u.String(), nil
}

// classify turns errors that mean "the pipeline has not run yet" into
// ErrNoData, so handlers can say so instead of reporting a failure.
func (s *Store) classify(err error) error {
	if err == nil {
		return nil
	}
	var se *sqlite.Error
	// SQLITE_CANTOPEN (14) is "no data yet" only when the file is missing;
	// an existing file that cannot be opened (permissions, or a WAL database
	// in a directory where SQLite cannot create its -shm file) is a fault.
	if errors.As(err, &se) && se.Code()&0xff == 14 {
		if _, statErr := os.Stat(s.path); errors.Is(statErr, fs.ErrNotExist) {
			return fmt.Errorf("%w: %v", ErrNoData, err)
		}
		return fmt.Errorf("open %s: %w", s.path, err)
	}
	if strings.Contains(err.Error(), "no such table") {
		return fmt.Errorf("%w: %v", ErrNoData, err)
	}
	return err
}

// Period is the local calendar window the dashboard summarizes: the week
// starts on Monday and the month on the 1st, both up to and including today.
type Period struct {
	Today      time.Time
	WeekStart  time.Time
	MonthStart time.Time
}

// PeriodAt returns the period containing now, in now's location (the server's
// TZ, which should match the processor's TZ that defines metrics dates).
func PeriodAt(now time.Time) Period {
	y, m, d := now.Date()
	today := time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
	offset := (int(today.Weekday()) + 6) % 7 // days since Monday
	return Period{
		Today:      today,
		WeekStart:  today.AddDate(0, 0, -offset),
		MonthStart: time.Date(y, m, 1, 0, 0, 0, 0, time.UTC),
	}
}

// DeviceKey names a NextDNS device within an account, as the device_metrics
// and device_sessions tables key it.
type DeviceKey struct {
	Account string
	Name    string
}

// String is the "account:device" form the dashboard and API accept.
func (k DeviceKey) String() string {
	return k.Account + ":" + k.Name
}

// ParseDeviceKey parses "account:device". Account names cannot contain a
// colon, so the first colon separates them and the device name may hold more.
func ParseDeviceKey(s string) (DeviceKey, error) {
	account, name, ok := strings.Cut(s, ":")
	if !ok || account == "" || name == "" {
		return DeviceKey{}, fmt.Errorf("device must be account:device_name, got %q", s)
	}
	return DeviceKey{Account: account, Name: name}, nil
}

// AllProfiles is the group parameter value that selects every profile.
const AllProfiles = "all"

// Scope is whose usage a view shows: one profile group (Group), one NextDNS
// device (Device), or, with neither, every profile.
type Scope struct {
	Group  string
	Device *DeviceKey
}

// metricsSource returns the daily-metrics table for the scope and the
// filter (starting with AND) and arguments that restrict it.
func (sc Scope) metricsSource() (table, filter string, args []any) {
	switch {
	case sc.Device != nil:
		return "device_metrics", "AND m.account = ? AND m.device_name = ?", []any{sc.Device.Account, sc.Device.Name}
	case sc.Group != "":
		return "group_metrics", "AND m.group_name = ?", []any{sc.Group}
	}
	return "metrics", "", nil
}

// Profile is a NextDNS profile the collector collects.
type Profile struct {
	ID      string `json:"id"`
	Name    string `json:"name"` // NextDNS name, else the ID
	Account string `json:"account"`
}

// Group is a profile group from SVERDRUP_GROUPS as the processor last
// resolved it: its rule ("include" or "exclude"), its configured entries
// (profile IDs or names), and the profiles it covers.
type Group struct {
	Name     string    `json:"name"`
	Rule     string    `json:"rule"`
	Entries  []string  `json:"entries"`
	Profiles []Profile `json:"profiles"`
}

// isMissingTable reports whether err is SQLite's "no such table". A
// database written before profile groups existed lacks their tables until
// the collector or processor next runs.
func isMissingTable(err error) bool {
	return err != nil && strings.Contains(err.Error(), "no such table")
}

// Groups returns the configured groups in SVERDRUP_GROUPS order, each with
// its member profiles by name. The first is the dashboard's default.
func (s *Store) Groups(ctx context.Context) ([]Group, error) {
	rows, err := s.db.QueryContext(ctx, "SELECT name, rule, entries FROM profile_groups ORDER BY position")
	if isMissingTable(err) {
		return nil, nil
	}
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	var out []Group
	index := map[string]int{}
	for rows.Next() {
		var g Group
		var entries string
		if err := rows.Scan(&g.Name, &g.Rule, &entries); err != nil {
			return nil, err
		}
		if g.Entries, err = jsonList(entries); err != nil {
			return nil, fmt.Errorf("group %s entries: %w", g.Name, err)
		}
		g.Profiles = []Profile{}
		index[g.Name] = len(out)
		out = append(out, g)
	}
	if err := rows.Err(); err != nil {
		return nil, s.classify(err)
	}
	members, err := s.db.QueryContext(ctx, `
		SELECT gm.group_name, gm.profile_id, COALESCE(p.name, gm.profile_id), COALESCE(p.account, '')
		FROM group_members gm LEFT JOIN profiles p ON p.profile_id = gm.profile_id
		ORDER BY COALESCE(p.name, gm.profile_id), gm.profile_id`)
	if err != nil {
		return nil, s.classify(err)
	}
	defer members.Close()
	for members.Next() {
		var group string
		var p Profile
		if err := members.Scan(&group, &p.ID, &p.Name, &p.Account); err != nil {
			return nil, err
		}
		if i, ok := index[group]; ok {
			out[i].Profiles = append(out[i].Profiles, p)
		}
	}
	return out, s.classify(members.Err())
}

// ProfileNames maps each profile ID the collector has named to its NextDNS
// name.
func (s *Store) ProfileNames(ctx context.Context) (map[string]string, error) {
	names := map[string]string{}
	rows, err := s.db.QueryContext(ctx, "SELECT profile_id, name FROM profiles WHERE name IS NOT NULL")
	if isMissingTable(err) {
		return names, nil
	}
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	for rows.Next() {
		var id, name string
		if err := rows.Scan(&id, &name); err != nil {
			return nil, err
		}
		names[id] = name
	}
	return names, s.classify(rows.Err())
}

// ProfileSummary is a profile with its usage from the per-profile metrics:
// its share of each account-wide session, so concurrent viewing in two
// profiles splits rather than doubles.
type ProfileSummary struct {
	Profile
	Groups        []string `json:"groups"`
	TotalMinutes  float64  `json:"total_minutes"`
	SessionCount  int64    `json:"session_count"`
	LastSeen      *string  `json:"last_seen"`
	WeekMinutes   float64  `json:"week_minutes"`
	WeekSessions  int64    `json:"week_sessions"`
	MonthMinutes  float64  `json:"month_minutes"`
	MonthSessions int64    `json:"month_sessions"`
}

// ProfileSummaries returns every collected profile, and every profile with
// metrics, ordered by minutes this month, then all-time minutes, then name.
// Groups is left for the caller to fill.
func (s *Store) ProfileSummaries(ctx context.Context, p Period) ([]ProfileSummary, error) {
	ids := "SELECT profile_id FROM metrics"
	join := ""
	name, account := "ids.profile_id", "COALESCE(MAX(m.account), '')"
	if err := s.db.QueryRowContext(ctx, "SELECT 1 FROM profiles LIMIT 1").Scan(new(int)); err == nil || errors.Is(err, sql.ErrNoRows) {
		ids += " UNION SELECT profile_id FROM profiles"
		join = "LEFT JOIN profiles p ON p.profile_id = ids.profile_id"
		name, account = "COALESCE(p.name, ids.profile_id)", "COALESCE(p.account, MAX(m.account), '')"
	} else if !isMissingTable(err) {
		return nil, s.classify(err)
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT ids.profile_id, `+name+` AS profile_name, `+account+`,
		       COALESCE(SUM(m.total_minutes), 0) AS all_minutes,
		       COALESCE(SUM(m.session_count), 0), MAX(m.last_seen),
		       `+periodColumns+`
		FROM (`+ids+`) ids `+join+`
		LEFT JOIN metrics m ON m.profile_id = ids.profile_id
		GROUP BY ids.profile_id
		ORDER BY month_minutes DESC, all_minutes DESC, profile_name, ids.profile_id`, periodArgs(p)...)
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	var out []ProfileSummary
	for rows.Next() {
		var r ProfileSummary
		if err := rows.Scan(&r.ID, &r.Name, &r.Account, &r.TotalMinutes, &r.SessionCount, &r.LastSeen,
			&r.WeekMinutes, &r.WeekSessions, &r.MonthMinutes, &r.MonthSessions); err != nil {
			return nil, err
		}
		r.Groups = []string{}
		out = append(out, r)
	}
	return out, s.classify(rows.Err())
}

// Service is one row of the services table.
type Service struct {
	ID   int64  `json:"id"`
	Code string `json:"code"`
	Name string `json:"name"`
}

// ServiceSummary is a service with its usage aggregated from the daily metrics.
type ServiceSummary struct {
	Service
	TotalMinutes  float64 `json:"total_minutes"`
	SessionCount  int64   `json:"session_count"`
	ActiveDays    int64   `json:"active_days"`
	FirstDate     *string `json:"first_date"`
	LastDate      *string `json:"last_date"`
	LastSeen      *string `json:"last_seen"`
	WeekMinutes   float64 `json:"week_minutes"`
	WeekSessions  int64   `json:"week_sessions"`
	MonthMinutes  float64 `json:"month_minutes"`
	MonthSessions int64   `json:"month_sessions"`
}

// ServiceSummaries returns every service with its usage in the scope: every
// profile (from metrics), one group (group_metrics) or one device
// (device_metrics). Services without usage are included with zeros. They
// are ordered by minutes this month, then all-time minutes, then name.
func (s *Store) ServiceSummaries(ctx context.Context, p Period, sc Scope) ([]ServiceSummary, error) {
	table, filter, extra := sc.metricsSource()
	join := "LEFT JOIN " + table + " m ON m.service_id = sv.id " + filter
	args := append(periodArgs(p), extra...) // ON follows the SELECT list
	rows, err := s.db.QueryContext(ctx, `
		SELECT sv.id, sv.service_code, sv.service_name,
		       COALESCE(SUM(m.total_minutes), 0) AS all_minutes,
		       COALESCE(SUM(m.session_count), 0),
		       COUNT(DISTINCT CASE WHEN m.total_minutes > 0 OR m.session_count > 0 THEN m.date END),
		       MIN(m.date), MAX(m.date), MAX(m.last_seen),
		       `+periodColumns+`
		FROM services sv `+join+`
		GROUP BY sv.id
		ORDER BY month_minutes DESC, all_minutes DESC, sv.service_name, sv.id`, args...)
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	var out []ServiceSummary
	for rows.Next() {
		var r ServiceSummary
		if err := rows.Scan(&r.ID, &r.Code, &r.Name, &r.TotalMinutes, &r.SessionCount,
			&r.ActiveDays, &r.FirstDate, &r.LastDate, &r.LastSeen,
			&r.WeekMinutes, &r.WeekSessions, &r.MonthMinutes, &r.MonthSessions); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, s.classify(rows.Err())
}

// periodColumns sums minutes and sessions for this week and this month; it
// takes periodArgs. Dates are fixed-width text, so text comparison is date order.
const periodColumns = `
		COALESCE(SUM(CASE WHEN m.date BETWEEN ? AND ? THEN m.total_minutes END), 0) AS week_minutes,
		COALESCE(SUM(CASE WHEN m.date BETWEEN ? AND ? THEN m.session_count END), 0) AS week_sessions,
		COALESCE(SUM(CASE WHEN m.date BETWEEN ? AND ? THEN m.total_minutes END), 0) AS month_minutes,
		COALESCE(SUM(CASE WHEN m.date BETWEEN ? AND ? THEN m.session_count END), 0) AS month_sessions`

func periodArgs(p Period) []any {
	week, month, today := p.WeekStart.Format(dateLayout), p.MonthStart.Format(dateLayout), p.Today.Format(dateLayout)
	return []any{week, today, week, today, month, today, month, today}
}

// Service returns one service by ID, or ErrNotFound.
func (s *Store) Service(ctx context.Context, id int64) (Service, error) {
	var sv Service
	err := s.db.QueryRowContext(ctx,
		"SELECT id, service_code, service_name FROM services WHERE id = ?", id,
	).Scan(&sv.ID, &sv.Code, &sv.Name)
	if errors.Is(err, sql.ErrNoRows) {
		return Service{}, ErrNotFound
	}
	return sv, s.classify(err)
}

// DeviceSummary is a NextDNS device with its usage aggregated from
// device_metrics.
type DeviceSummary struct {
	Account       string  `json:"account"`
	DeviceName    string  `json:"device_name"`
	Key           string  `json:"key"`
	TotalMinutes  float64 `json:"total_minutes"`
	SessionCount  int64   `json:"session_count"`
	LastSeen      *string `json:"last_seen"`
	WeekMinutes   float64 `json:"week_minutes"`
	WeekSessions  int64   `json:"week_sessions"`
	MonthMinutes  float64 `json:"month_minutes"`
	MonthSessions int64   `json:"month_sessions"`
}

// DeviceSummaries returns every device seen in device_metrics, optionally
// limited to one service, ordered by minutes this month, then all-time
// minutes, then account and name.
func (s *Store) DeviceSummaries(ctx context.Context, p Period, serviceID *int64) ([]DeviceSummary, error) {
	where := ""
	args := periodArgs(p)
	if serviceID != nil {
		where = "WHERE m.service_id = ?"
		args = append(args, *serviceID)
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT m.account, m.device_name,
		       SUM(m.total_minutes) AS all_minutes, SUM(m.session_count), MAX(m.last_seen),
		       `+periodColumns+`
		FROM device_metrics m `+where+`
		GROUP BY m.account, m.device_name
		ORDER BY month_minutes DESC, all_minutes DESC, m.account, m.device_name`, args...)
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	var out []DeviceSummary
	for rows.Next() {
		var r DeviceSummary
		if err := rows.Scan(&r.Account, &r.DeviceName, &r.TotalMinutes, &r.SessionCount, &r.LastSeen,
			&r.WeekMinutes, &r.WeekSessions, &r.MonthMinutes, &r.MonthSessions); err != nil {
			return nil, err
		}
		r.Key = DeviceKey{Account: r.Account, Name: r.DeviceName}.String()
		out = append(out, r)
	}
	return out, s.classify(rows.Err())
}

// DayMetric is one local calendar day of a service's usage in a scope.
type DayMetric struct {
	Date         string  `json:"date"`
	TotalMinutes float64 `json:"total_minutes"`
	SessionCount int64   `json:"session_count"`
	LastSeen     *string `json:"last_seen"`
}

// DailyMetrics returns one entry per day from start to end inclusive, with
// zeros for days without usage, in the scope.
func (s *Store) DailyMetrics(ctx context.Context, serviceID int64, start, end time.Time, sc Scope) ([]DayMetric, error) {
	table, filter, extra := sc.metricsSource()
	args := append([]any{serviceID, start.Format(dateLayout), end.Format(dateLayout)}, extra...)
	rows, err := s.db.QueryContext(ctx, `
		SELECT m.date, SUM(m.total_minutes), SUM(m.session_count), MAX(m.last_seen)
		FROM `+table+` m
		WHERE m.service_id = ? AND m.date BETWEEN ? AND ? `+filter+`
		GROUP BY m.date`, args...)
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	byDate := map[string]DayMetric{}
	for rows.Next() {
		var d DayMetric
		if err := rows.Scan(&d.Date, &d.TotalMinutes, &d.SessionCount, &d.LastSeen); err != nil {
			return nil, err
		}
		byDate[d.Date] = d
	}
	if err := rows.Err(); err != nil {
		return nil, s.classify(err)
	}
	var out []DayMetric
	for day := start; !day.After(end); day = day.AddDate(0, 0, 1) {
		date := day.Format(dateLayout)
		d, ok := byDate[date]
		if !ok {
			d = DayMetric{Date: date}
		}
		out = append(out, d)
	}
	return out, nil
}

// MedianDaysBetweenUse returns the median gap, in days, between consecutive
// local days on which the service was used in the scope, and false when it
// was used on fewer than two days.
func (s *Store) MedianDaysBetweenUse(ctx context.Context, serviceID int64, sc Scope) (float64, bool, error) {
	table, filter, extra := sc.metricsSource()
	rows, err := s.db.QueryContext(ctx, `
		SELECT m.date FROM `+table+` m WHERE m.service_id = ? `+filter+`
		GROUP BY m.date
		HAVING SUM(m.total_minutes) > 0 OR SUM(m.session_count) > 0
		ORDER BY m.date`, append([]any{serviceID}, extra...)...)
	if err != nil {
		return 0, false, s.classify(err)
	}
	defer rows.Close()
	var days []time.Time
	for rows.Next() {
		var date string
		if err := rows.Scan(&date); err != nil {
			return 0, false, err
		}
		day, err := time.Parse(dateLayout, date)
		if err != nil {
			return 0, false, fmt.Errorf("metrics date %q: %w", date, err)
		}
		days = append(days, day)
	}
	if err := rows.Err(); err != nil {
		return 0, false, s.classify(err)
	}
	if len(days) < 2 {
		return 0, false, nil
	}
	gaps := make([]float64, 0, len(days)-1)
	for i := 1; i < len(days); i++ {
		gaps = append(gaps, math.Round(days[i].Sub(days[i-1]).Hours()/24))
	}
	sort.Float64s(gaps)
	mid := len(gaps) / 2
	if len(gaps)%2 == 1 {
		return gaps[mid], true, nil
	}
	return (gaps[mid-1] + gaps[mid]) / 2, true, nil
}

// Session is one viewing session: account-wide (Account set) or within a
// profile group (Group set).
type Session struct {
	ID           int64    `json:"id"`
	ServiceID    int64    `json:"service_id"`
	ServiceCode  string   `json:"service_code"`
	ServiceName  string   `json:"service_name"`
	Account      string   `json:"account,omitempty"`
	Group        string   `json:"group,omitempty"`
	ProfileIDs   []string `json:"profile_ids"`
	ProfileNames []string `json:"profile_names"` // NextDNS names, else IDs, in ProfileIDs order
	DeviceNames  []string `json:"device_names"`
	StartTime    string   `json:"start_time"`
	EndTime      string   `json:"end_time"`
	Minutes      float64  `json:"session_length_minutes"`
}

// RecentSessions returns the latest sessions, newest first, for one service
// or for all of them when serviceID is nil: account-wide sessions, or one
// group's when group is set.
func (s *Store) RecentSessions(ctx context.Context, serviceID *int64, limit int, group string) ([]Session, error) {
	from, owner := "sessions", "se.account, ''"
	where := "WHERE 1 = 1"
	args := []any{}
	if group != "" {
		from, owner = "group_sessions", "'', se.group_name"
		where += " AND se.group_name = ?"
		args = append(args, group)
	}
	if serviceID != nil {
		where += " AND se.service_id = ?"
		args = append(args, *serviceID)
	}
	args = append(args, limit)
	names, err := s.ProfileNames(ctx)
	if err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT se.id, se.service_id, sv.service_code, sv.service_name, `+owner+`,
		       se.profile_ids, se.device_names, se.start_time, se.end_time,
		       se.session_length_minutes
		FROM `+from+` se JOIN services sv ON sv.id = se.service_id
		`+where+`
		ORDER BY se.start_time DESC, se.id DESC
		LIMIT ?`, args...)
	if err != nil {
		return nil, s.classify(err)
	}
	defer rows.Close()
	out := []Session{}
	for rows.Next() {
		var r Session
		var profiles, devices string
		if err := rows.Scan(&r.ID, &r.ServiceID, &r.ServiceCode, &r.ServiceName, &r.Account, &r.Group,
			&profiles, &devices, &r.StartTime, &r.EndTime, &r.Minutes); err != nil {
			return nil, err
		}
		if r.ProfileIDs, err = jsonList(profiles); err != nil {
			return nil, fmt.Errorf("session %d profile_ids: %w", r.ID, err)
		}
		r.ProfileNames = make([]string, len(r.ProfileIDs))
		for i, id := range r.ProfileIDs {
			r.ProfileNames[i] = id
			if name, ok := names[id]; ok {
				r.ProfileNames[i] = name
			}
		}
		if r.DeviceNames, err = jsonList(devices); err != nil {
			return nil, fmt.Errorf("session %d device_names: %w", r.ID, err)
		}
		out = append(out, r)
	}
	return out, s.classify(rows.Err())
}

// jsonList decodes a JSON array of strings, as the processor stores the
// profiles and devices a session touched.
func jsonList(s string) ([]string, error) {
	out := []string{}
	if err := json.Unmarshal([]byte(s), &out); err != nil {
		return nil, err
	}
	if out == nil {
		out = []string{}
	}
	return out, nil
}
