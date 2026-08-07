package main

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestHTTPWriterRequest(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v3/write_lp" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		wantParams := url.Values{
			"db":             {"benchmark"},
			"precision":      {"nanosecond"},
			"accept_partial": {"false"},
			"no_sync":        {"false"},
		}
		if got := r.URL.Query(); got.Encode() != wantParams.Encode() {
			t.Errorf("unexpected query params: got %s want %s", got.Encode(), wantParams.Encode())
		}
		if got := r.Header.Get("Authorization"); got != "Bearer write-token" {
			t.Errorf("unexpected authorization: %q", got)
		}
		if got := r.Header.Get("Content-Encoding"); got != "gzip" {
			t.Errorf("unexpected content encoding: %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	writer := NewHTTPWriter(HTTPWriterConfig{
		Host: server.URL, Database: "benchmark", Token: "write-token",
	})
	latency, err := writer.WriteLineProtocol([]byte("compressed"), true)
	if err != nil {
		t.Fatal(err)
	}
	if latency <= 0 {
		t.Fatalf("unexpected latency: %d", latency)
	}
}

func TestHTTPWriterErrors(t *testing.T) {
	tests := []struct {
		name      string
		status    int
		retryable bool
	}{
		{name: "too many requests", status: http.StatusTooManyRequests, retryable: true},
		{name: "unavailable", status: http.StatusServiceUnavailable, retryable: true},
		{name: "bad request", status: http.StatusBadRequest},
		{name: "server error", status: http.StatusInternalServerError},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Retry-After", "2")
				w.WriteHeader(tt.status)
				fmt.Fprint(w, "write failed")
			}))
			defer server.Close()

			writer := NewHTTPWriter(HTTPWriterConfig{Host: server.URL, Database: "benchmark"})
			_, err := writer.WriteLineProtocol([]byte("cpu usage=1i"), false)
			if err == nil || !strings.Contains(err.Error(), "write failed") {
				t.Fatalf("unexpected error: %v", err)
			}
			retryErr, ok := err.(*retryableWriteError)
			if ok != tt.retryable {
				t.Fatalf("retryable=%v want %v", ok, tt.retryable)
			}
			if ok && retryErr.retryAfter != 2*time.Second {
				t.Fatalf("retry after=%s", retryErr.retryAfter)
			}
		})
	}
}

func TestParseRetryAfter(t *testing.T) {
	now := time.Date(2026, 8, 7, 0, 0, 0, 0, time.UTC)
	if got := parseRetryAfter("3", now); got != 3*time.Second {
		t.Fatalf("seconds retry-after: got %s", got)
	}
	if got := parseRetryAfter(now.Add(5*time.Second).Format(http.TimeFormat), now); got != 5*time.Second {
		t.Fatalf("date retry-after: got %s", got)
	}
}
