package main

import (
	"fmt"
	"html/template"
	"math"
	"strconv"
	"strings"
	"time"
)

// funcs are the template helpers. Stored timestamps are UTC; the dashboard
// shows them in the server's zone.
func (s *Server) funcs() template.FuncMap {
	return template.FuncMap{
		"minutes":     formatMinutes,
		"localTime":   s.localTime,
		"day":         formatDay,
		"shortDate":   func(t time.Time) string { return t.Format("Mon 2 Jan") },
		"days":        formatDays,
		"bars":        sparkBars,
		"sparkWidth":  func(h []DayMetric) float64 { return float64(len(h)) * sparkStep },
		"sparkHeight": func() float64 { return sparkHeight },
		"join":        func(list []string) string { return strings.Join(list, ", ") },
		"plural":      plural,
		"asset":       func(name string) string { return s.assets[name] },
	}
}

// formatMinutes renders a duration in minutes as "2h 05m", "45m" or "<1m".
func formatMinutes(m float64) string {
	if m <= 0 {
		return "0m"
	}
	if m < 1 {
		return "<1m"
	}
	total := int(math.Round(m))
	if total < 60 {
		return fmt.Sprintf("%dm", total)
	}
	return fmt.Sprintf("%dh %02dm", total/60, total%60)
}

// localTime renders a stored UTC timestamp (string or *string) in the
// server's zone; an absent or unparseable value renders as-is or a dash.
func (s *Server) localTime(v any) string {
	var raw string
	switch t := v.(type) {
	case string:
		raw = t
	case *string:
		if t == nil {
			return "-"
		}
		raw = *t
	}
	if raw == "" {
		return "-"
	}
	ts, err := time.Parse(time.RFC3339Nano, raw)
	if err != nil {
		return raw
	}
	return ts.In(s.loc).Format("Mon 2 Jan 2006 15:04")
}

// formatDay renders a metrics date (YYYY-MM-DD) with its weekday.
func formatDay(date string) string {
	d, err := time.Parse(dateLayout, date)
	if err != nil {
		return date
	}
	return d.Format("Mon 2 Jan 2006")
}

// formatDays renders a number of days, keeping a half day from a median.
func formatDays(n float64) string {
	s := strconv.FormatFloat(n, 'f', -1, 64)
	if n == 1 {
		return s + " day"
	}
	return s + " days"
}

// bar is one day of the sparkline, in SVG user units.
type bar struct {
	X, Y, W, H float64
	Label      string
}

const (
	sparkHeight = 40.0
	sparkStep   = 4.0
)

// sparkBars lays out one bar per day, scaled to the busiest day. A day with
// any usage gets at least a sliver so it stays visible.
func sparkBars(history []DayMetric, max float64) []bar {
	out := make([]bar, 0, len(history))
	for i, d := range history {
		h := 0.0
		if max > 0 && d.TotalMinutes > 0 {
			h = math.Max(1, d.TotalMinutes/max*sparkHeight)
		}
		out = append(out, bar{
			X: float64(i) * sparkStep, Y: sparkHeight - h, W: sparkStep - 1, H: h,
			Label: formatDay(d.Date) + ": " + formatMinutes(d.TotalMinutes),
		})
	}
	return out
}

// plural renders a count with its noun, adding "s" unless the count is one.
func plural(n int64, noun string) string {
	if n == 1 {
		return "1 " + noun
	}
	return strconv.FormatInt(n, 10) + " " + noun + "s"
}
