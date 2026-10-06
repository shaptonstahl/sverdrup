// Command sverdrup-server serves the Sverdrup dashboard and JSON API from the
// SQLite database the collector and processor maintain. It only reads.
package main

import (
	"errors"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

const defaultDBPath = "/data/sverdrup.db"

func main() {
	dbPath := envOr("DB_PATH", defaultDBPath)
	port := envOr("DASHBOARD_PORT", "8080")

	store, err := OpenStore(dbPath)
	if err != nil {
		log.Fatalf("open database: %v", err)
	}
	defer store.Close()
	srv, err := NewServer(store)
	if err != nil {
		log.Fatalf("load templates: %v", err)
	}

	httpServer := &http.Server{
		Addr:              ":" + port,
		Handler:           srv.Handler(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}
	log.Printf("Sverdrup dashboard listening on port %s, reading %s (time zone %s)", port, dbPath, time.Local)
	if err := httpServer.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatalf("server: %v", err)
	}
}

// envOr returns the trimmed environment value, or fallback when it is empty.
func envOr(key, fallback string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return fallback
}
