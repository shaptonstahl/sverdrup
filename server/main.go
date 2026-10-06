package main

import (
	"fmt"
	"net/http"
	"os"
)

func main() {
	port := os.Getenv("DASHBOARD_PORT")
	if port == "" {
		port = "8080"
	}

	// TODO: Implement dashboard routes and API endpoints
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, "Sverdrup Dashboard - Coming soon\n")
	})

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"status":"ok"}`)
	})

	fmt.Printf("Server listening on port %s\n", port)
	if err := http.ListenAndServe(":"+port, nil); err != nil {
		fmt.Fprintf(os.Stderr, "Server error: %v\n", err)
		os.Exit(1)
	}
}
