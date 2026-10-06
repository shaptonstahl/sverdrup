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

// ServiceSummaries returns every service with its usage, household-wide
// (from metrics) or for one device (from device_metrics). Services without
// usage are included with zeros. They are ordered by minutes this month,
// then all-time minutes, then name.
func (s *Store) ServiceSummaries(ctx context.Context, p Period, device *DeviceKey) ([]ServiceSummary, error) {
	join := "LEFT JOIN metrics m ON m.service_id = sv.id"
	args := periodArgs(p)
	if device != nil {
		join = "LEFT JOIN device_metrics m ON m.service_id = sv.id AND m.account = ? AND m.device_name = ?"
		args = append(args, device.Account, device.Name) // ON follows the SELECT list
	}
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

// DayMetric is one local calendar day of a service's usage, summed across
// accounts and profiles (or for one device).
type DayMetric struct {
	Date         string  `json:"date"`
	TotalMinutes float64 `json:"total_minutes"`
	SessionCount int64   `json:"session_count"`
	LastSeen     *string `json:"last_seen"`
}

// DailyMetrics returns one entry per day from start to end inclusive, with
// zeros for days without usage, household-wide or for one device.
func (s *Store) DailyMetrics(ctx context.Context, serviceID int64, start, end time.Time, device *DeviceKey) ([]DayMetric, error) {
	table, filter := "metrics", ""
	args := []any{serviceID, start.Format(dateLayout), end.Format(dateLayout)}
	if device != nil {
		table, filter = "device_metrics", "AND account = ? AND device_name = ?"
		args = append(args, device.Account, device.Name)
	}
	rows, err := s.db.QueryContext(ctx, `
		SELECT date, SUM(total_minutes), SUM(session_count), MAX(last_seen)
		FROM `+table+`
		WHERE service_id = ? AND date BETWEEN ? AND ? `+filter+`
		GROUP BY date`, args...)
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
// local days on which the service was used, and false when it was used on
// fewer than two days.
func (s *Store) MedianDaysBetweenUse(ctx context.Context, serviceID int64) (float64, bool, error) {
	rows, err := s.db.QueryContext(ctx, `
		SELECT date FROM metrics WHERE service_id = ?
		GROUP BY date
		HAVING SUM(total_minutes) > 0 OR SUM(session_count) > 0
		ORDER BY date`, serviceID)
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

// Session is one household viewing session.
type Session struct {
	ID          int64    `json:"id"`
	ServiceID   int64    `json:"service_id"`
	ServiceCode string   `json:"service_code"`
	ServiceName string   `json:"service_name"`
	Account     string   `json:"account"`
	ProfileIDs  []string `json:"profile_ids"`
	DeviceNames []string `json:"device_names"`
	StartTime   string   `json:"start_time"`
	EndTime     string   `json:"end_time"`
	Minutes     float64  `json:"session_length_minutes"`
}

// RecentSessions returns the latest sessions, newest first, for one service
// or for all of them when serviceID is nil.
func (s *Store) RecentSessions(ctx context.Context, serviceID *int64, limit int) ([]Session, error) {
	where := ""
	args := []any{}
	if serviceID != nil {
		where = "WHERE se.service_id = ?"
		args = append(args, *serviceID)
	}
	args = append(args, limit)
	rows, err := s.db.QueryContext(ctx, `
		SELECT se.id, se.service_id, sv.service_code, sv.service_name, se.account,
		       se.profile_ids, se.device_names, se.start_time, se.end_time,
		       se.session_length_minutes
		FROM sessions se JOIN services sv ON sv.id = se.service_id
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
		if err := rows.Scan(&r.ID, &r.ServiceID, &r.ServiceCode, &r.ServiceName, &r.Account,
			&profiles, &devices, &r.StartTime, &r.EndTime, &r.Minutes); err != nil {
			return nil, err
		}
		if r.ProfileIDs, err = jsonList(profiles); err != nil {
			return nil, fmt.Errorf("session %d profile_ids: %w", r.ID, err)
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
