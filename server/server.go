package main

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"html/template"
	"io/fs"
	"log"
	"math"
	"net/http"
	"slices"
	"strconv"
	"time"
)

//go:embed templates/*.html
var templateFS embed.FS

//go:embed static
var staticFS embed.FS

const (
	defaultSessionLimit = 50
	maxSessionLimit     = 500
	// defaultMetricsDays is the API's default range, ending today.
	defaultMetricsDays = 30
	// maxMetricsDays bounds one time-series request (about ten years).
	maxMetricsDays = 3660
)

// historyRanges are the service page's daily-history choices, in days.
var historyRanges = []int{30, 90, 365}

// Server serves the dashboard pages and the JSON API.
type Server struct {
	store *Store
	// now and loc are the clock and the zone that define "today" and render
	// timestamps; tests replace them.
	now   func() time.Time
	loc   *time.Location
	pages map[string]*template.Template
	// assets maps a static file name to its URL with a content hash, so a
	// new build's files are fetched despite the long cache lifetime.
	assets map[string]string
}

// NewServer parses the embedded templates and returns a server using the
// system clock and local time zone (TZ).
func NewServer(store *Store) (*Server, error) {
	s := &Server{store: store, now: time.Now, loc: time.Local, pages: map[string]*template.Template{}, assets: map[string]string{}}
	entries, err := fs.ReadDir(staticFS, "static")
	if err != nil {
		return nil, err
	}
	for _, e := range entries {
		b, err := staticFS.ReadFile("static/" + e.Name())
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256(b)
		s.assets[e.Name()] = "/static/" + e.Name() + "?v=" + hex.EncodeToString(sum[:6])
	}
	for _, page := range []string{"overview", "service", "error"} {
		t, err := template.New("layout.html").Funcs(s.funcs()).
			ParseFS(templateFS, "templates/layout.html", "templates/"+page+".html")
		if err != nil {
			return nil, err
		}
		s.pages[page] = t
	}
	return s, nil
}

// Handler returns the routes, wrapped with the security headers.
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /{$}", s.handleOverview)
	mux.HandleFunc("GET /services/{id}", s.handleService)
	mux.HandleFunc("GET /api/services", s.handleAPIServices)
	mux.HandleFunc("GET /api/services/{id}/metrics", s.handleAPIMetrics)
	mux.HandleFunc("GET /api/sessions", s.handleAPISessions)
	mux.HandleFunc("GET /api/devices", s.handleAPIDevices)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("GET /favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNoContent) // no icon; keeps browsers from logging a 404
	})
	static, _ := fs.Sub(staticFS, "static")
	files := http.StripPrefix("/static/", http.FileServerFS(static))
	mux.Handle("GET /static/", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "public, max-age=86400")
		files.ServeHTTP(w, r)
	}))
	mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		s.htmlError(w, r, http.StatusNotFound, "Page not found.")
	})
	return securityHeaders(mux)
}

// securityHeaders allows only same-origin scripts and styles (htmx and the
// stylesheet are served from /static) and forbids framing.
func securityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("Content-Security-Policy",
			"default-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		next.ServeHTTP(w, r)
	})
}

// overviewData is the overview page and its htmx panel.
type overviewData struct {
	Period    Period
	Device    *DeviceKey
	DeviceSel string
	Services  []ServiceSummary
	Devices   []DeviceSummary
	Totals    ServiceSummary
	UsedMonth int
}

func (s *Server) handleOverview(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	device, err := deviceParam(r)
	if err != nil {
		s.htmlError(w, r, http.StatusBadRequest, err.Error())
		return
	}
	p := PeriodAt(s.now().In(s.loc))
	services, err := s.store.ServiceSummaries(ctx, p, device)
	if err != nil {
		s.storeError(w, r, err)
		return
	}
	devices, err := s.store.DeviceSummaries(ctx, p, nil)
	if err != nil {
		s.storeError(w, r, err)
		return
	}
	data := overviewData{Period: p, Device: device, Services: services, Devices: devices}
	if device != nil {
		data.DeviceSel = device.String()
	}
	for _, sv := range services {
		data.Totals.WeekMinutes += sv.WeekMinutes
		data.Totals.WeekSessions += sv.WeekSessions
		data.Totals.MonthMinutes += sv.MonthMinutes
		data.Totals.MonthSessions += sv.MonthSessions
		data.Totals.TotalMinutes += sv.TotalMinutes
		data.Totals.SessionCount += sv.SessionCount
		if sv.MonthMinutes > 0 || sv.MonthSessions > 0 {
			data.UsedMonth++
		}
	}
	s.render(w, r, "overview", "services-panel", data)
}

// serviceData is the service detail page and its htmx history panel.
type serviceData struct {
	Period    Period
	Summary   ServiceSummary
	MedianGap float64
	HasMedian bool
	Days      int
	Ranges    []int
	History   []DayMetric
	Active    []DayMetric // History's days with usage, newest first
	MaxDay    float64
	Devices   []DeviceSummary
	Sessions  []Session
}

func (s *Server) handleService(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id, ok := pathID(r)
	if !ok {
		s.htmlError(w, r, http.StatusNotFound, "No such service.")
		return
	}
	days := historyRanges[0]
	if v := r.URL.Query().Get("days"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || !slices.Contains(historyRanges, n) {
			s.htmlError(w, r, http.StatusBadRequest, "days must be 30, 90 or 365.")
			return
		}
		days = n
	}
	if _, err := s.store.Service(ctx, id); err != nil {
		s.storeError(w, r, err)
		return
	}
	p := PeriodAt(s.now().In(s.loc))
	data := serviceData{Period: p, Days: days, Ranges: historyRanges}
	summaries, err := s.store.ServiceSummaries(ctx, p, nil)
	if err != nil {
		s.storeError(w, r, err)
		return
	}
	for _, sv := range summaries {
		if sv.ID == id {
			data.Summary = sv
		}
	}
	data.History, err = s.store.DailyMetrics(ctx, id, p.Today.AddDate(0, 0, 1-days), p.Today, nil)
	if err != nil {
		s.storeError(w, r, err)
		return
	}
	for i := len(data.History) - 1; i >= 0; i-- {
		d := data.History[i]
		data.MaxDay = math.Max(data.MaxDay, d.TotalMinutes)
		if d.TotalMinutes > 0 || d.SessionCount > 0 {
			data.Active = append(data.Active, d)
		}
	}
	if data.MedianGap, data.HasMedian, err = s.store.MedianDaysBetweenUse(ctx, id); err != nil {
		s.storeError(w, r, err)
		return
	}
	if data.Devices, err = s.store.DeviceSummaries(ctx, p, &id); err != nil {
		s.storeError(w, r, err)
		return
	}
	if data.Sessions, err = s.store.RecentSessions(ctx, &id, defaultSessionLimit); err != nil {
		s.storeError(w, r, err)
		return
	}
	s.render(w, r, "service", "history-panel", data)
}

// apiServicesResponse is GET /api/services.
type apiServicesResponse struct {
	Today      string           `json:"today"`
	WeekStart  string           `json:"week_start"`
	MonthStart string           `json:"month_start"`
	Device     *string          `json:"device"`
	Services   []ServiceSummary `json:"services"`
}

func (s *Server) handleAPIServices(w http.ResponseWriter, r *http.Request) {
	device, err := deviceParam(r)
	if err != nil {
		apiError(w, http.StatusBadRequest, err.Error())
		return
	}
	p := PeriodAt(s.now().In(s.loc))
	services, err := s.store.ServiceSummaries(r.Context(), p, device)
	if err != nil {
		apiStoreError(w, err)
		return
	}
	for i := range services {
		roundSummary(&services[i])
	}
	resp := apiServicesResponse{
		Today:      p.Today.Format(dateLayout),
		WeekStart:  p.WeekStart.Format(dateLayout),
		MonthStart: p.MonthStart.Format(dateLayout),
		Services:   nonNil(services),
	}
	if device != nil {
		k := device.String()
		resp.Device = &k
	}
	writeJSON(w, http.StatusOK, resp)
}

// apiMetricsResponse is GET /api/services/{id}/metrics.
type apiMetricsResponse struct {
	Service Service     `json:"service"`
	Start   string      `json:"start"`
	End     string      `json:"end"`
	Device  *string     `json:"device"`
	Metrics []DayMetric `json:"metrics"`
}

func (s *Server) handleAPIMetrics(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	id, ok := pathID(r)
	if !ok {
		apiError(w, http.StatusNotFound, "no such service")
		return
	}
	q := r.URL.Query()
	end := PeriodAt(s.now().In(s.loc)).Today
	if v := q.Get("end"); v != "" {
		t, err := time.Parse(dateLayout, v)
		if err != nil {
			apiError(w, http.StatusBadRequest, "end must be a date, YYYY-MM-DD")
			return
		}
		end = t
	}
	start := end.AddDate(0, 0, 1-defaultMetricsDays)
	if v := q.Get("start"); v != "" {
		t, err := time.Parse(dateLayout, v)
		if err != nil {
			apiError(w, http.StatusBadRequest, "start must be a date, YYYY-MM-DD")
			return
		}
		start = t
	}
	if start.After(end) {
		apiError(w, http.StatusBadRequest, "start must not be after end")
		return
	}
	if end.Sub(start).Hours()/24 >= maxMetricsDays {
		apiError(w, http.StatusBadRequest, "range must be at most "+strconv.Itoa(maxMetricsDays)+" days")
		return
	}
	device, err := deviceParam(r)
	if err != nil {
		apiError(w, http.StatusBadRequest, err.Error())
		return
	}
	service, err := s.store.Service(ctx, id)
	if err != nil {
		apiStoreError(w, err)
		return
	}
	metrics, err := s.store.DailyMetrics(ctx, id, start, end, device)
	if err != nil {
		apiStoreError(w, err)
		return
	}
	for i := range metrics {
		metrics[i].TotalMinutes = roundMinutes(metrics[i].TotalMinutes)
	}
	resp := apiMetricsResponse{
		Service: service, Start: start.Format(dateLayout), End: end.Format(dateLayout), Metrics: metrics,
	}
	if device != nil {
		k := device.String()
		resp.Device = &k
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *Server) handleAPISessions(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	q := r.URL.Query()
	limit := defaultSessionLimit
	if v := q.Get("limit"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < 1 || n > maxSessionLimit {
			apiError(w, http.StatusBadRequest, "limit must be a whole number from 1 to "+strconv.Itoa(maxSessionLimit))
			return
		}
		limit = n
	}
	var serviceID *int64
	if v := q.Get("service"); v != "" {
		id, err := strconv.ParseInt(v, 10, 64)
		if err != nil || id < 1 {
			apiError(w, http.StatusBadRequest, "service must be a service id")
			return
		}
		if _, err := s.store.Service(ctx, id); err != nil {
			apiStoreError(w, err)
			return
		}
		serviceID = &id
	}
	sessions, err := s.store.RecentSessions(ctx, serviceID, limit)
	if err != nil {
		apiStoreError(w, err)
		return
	}
	for i := range sessions {
		sessions[i].Minutes = roundMinutes(sessions[i].Minutes)
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

func (s *Server) handleAPIDevices(w http.ResponseWriter, r *http.Request) {
	p := PeriodAt(s.now().In(s.loc))
	devices, err := s.store.DeviceSummaries(r.Context(), p, nil)
	if err != nil {
		apiStoreError(w, err)
		return
	}
	for i := range devices {
		d := &devices[i]
		d.TotalMinutes, d.WeekMinutes, d.MonthMinutes = roundMinutes(d.TotalMinutes), roundMinutes(d.WeekMinutes), roundMinutes(d.MonthMinutes)
	}
	writeJSON(w, http.StatusOK, map[string]any{"devices": nonNil(devices)})
}

// render writes a full page, or only its fragment for an htmx request so
// the swap replaces just that panel.
func (s *Server) render(w http.ResponseWriter, r *http.Request, page, fragment string, data any) {
	name := "layout.html"
	if r.Header.Get("HX-Request") == "true" && fragment != "" {
		name = fragment
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Add("Vary", "HX-Request")
	s.execute(w, http.StatusOK, page, name, data)
}

func (s *Server) execute(w http.ResponseWriter, status int, page, name string, data any) {
	// Render into a buffer first so a template error cannot leave half a page.
	var buf bytes.Buffer
	if err := s.pages[page].ExecuteTemplate(&buf, name, data); err != nil {
		log.Printf("render %s/%s: %v", page, name, err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.WriteHeader(status)
	_, _ = buf.WriteTo(w)
}

func (s *Server) htmlError(w http.ResponseWriter, r *http.Request, status int, msg string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	s.execute(w, status, "error", "layout.html", map[string]any{
		"Status": status, "Title": http.StatusText(status), "Message": msg,
	})
}

func (s *Server) storeError(w http.ResponseWriter, r *http.Request, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		s.htmlError(w, r, http.StatusNotFound, "No such service.")
	case errors.Is(err, ErrNoData):
		s.htmlError(w, r, http.StatusServiceUnavailable,
			"No data yet. The collector and processor create the database on their first scheduled run.")
	default:
		log.Printf("%s %s: %v", r.Method, r.URL.Path, err)
		s.htmlError(w, r, http.StatusInternalServerError, "The database could not be read.")
	}
}

func apiStoreError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrNotFound):
		apiError(w, http.StatusNotFound, "no such service")
	case errors.Is(err, ErrNoData):
		apiError(w, http.StatusServiceUnavailable, "no data yet: the collector and processor have not created the database")
	default:
		log.Printf("api: %v", err)
		apiError(w, http.StatusInternalServerError, "the database could not be read")
	}
}

func apiError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		log.Printf("encode json: %v", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(append(b, '\n'))
}

// deviceParam reads the optional ?device=account:device_name filter.
func deviceParam(r *http.Request) (*DeviceKey, error) {
	v := r.URL.Query().Get("device")
	if v == "" {
		return nil, nil
	}
	k, err := ParseDeviceKey(v)
	if err != nil {
		return nil, err
	}
	return &k, nil
}

// pathID reads a positive {id} path segment.
func pathID(r *http.Request) (int64, bool) {
	id, err := strconv.ParseInt(r.PathValue("id"), 10, 64)
	return id, err == nil && id > 0
}

// roundMinutes trims float noise from summed minutes for the API, to
// a thousandth of a minute (60 ms, about the precision of the timestamps).
func roundMinutes(m float64) float64 {
	return math.Round(m*1000) / 1000
}

func roundSummary(sv *ServiceSummary) {
	sv.TotalMinutes = roundMinutes(sv.TotalMinutes)
	sv.WeekMinutes = roundMinutes(sv.WeekMinutes)
	sv.MonthMinutes = roundMinutes(sv.MonthMinutes)
}

// nonNil makes an empty result encode as [] rather than null.
func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}
