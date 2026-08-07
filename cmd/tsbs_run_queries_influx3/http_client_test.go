package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/timescale/tsbs/pkg/query"
)

func testQuery() *query.HTTP {
	q := query.NewHTTP()
	q.HumanLabel = []byte("test query")
	q.HumanDescription = []byte("test query description")
	q.Method = []byte(http.MethodPost)
	q.Path = []byte("/api/v3/query_sql")
	q.RawQuery = []byte("SELECT * FROM cpu")
	return q
}

func TestHTTPClientDo(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/v3/query_sql" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer query-token" {
			t.Errorf("unexpected authorization: %q", got)
		}
		var request queryRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Database != "benchmark" || request.Query != "SELECT * FROM cpu" || request.Format != "json" {
			t.Errorf("unexpected request body: %+v", request)
		}
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `[{"usage_user":42}]`)
	}))
	defer server.Close()

	q := testQuery()
	defer q.Release()
	lag, err := NewHTTPClient(server.URL).Do(q, &HTTPClientDoOptions{Database: "benchmark", Token: "query-token"})
	if err != nil {
		t.Fatal(err)
	}
	if lag <= 0 {
		t.Fatalf("unexpected latency: %f", lag)
	}
}

func TestHTTPClientDoError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "bad SQL", http.StatusBadRequest)
	}))
	defer server.Close()

	q := testQuery()
	defer q.Release()
	_, err := NewHTTPClient(server.URL).Do(q, &HTTPClientDoOptions{Database: "benchmark"})
	if err == nil || !strings.Contains(err.Error(), "bad SQL") || !strings.Contains(err.Error(), "400") {
		t.Fatalf("unexpected error: %v", err)
	}
}
