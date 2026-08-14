package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"testing"

	"github.com/timescale/tsbs/pkg/query"
)

func testQuery(sql string) *query.HTTP {
	values := url.Values{}
	values.Set("sql", sql)
	q := query.NewHTTP()
	q.Method = []byte(http.MethodPost)
	q.Path = []byte("/v1/sql?" + values.Encode())
	q.RawQuery = []byte(sql)
	return q
}

func TestHTTPClientExplainAnalyzeVerbose(t *testing.T) {
	var receivedSQL string
	var receivedDatabase string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedSQL = r.URL.Query().Get("sql")
		receivedDatabase = r.URL.Query().Get("db")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"output":[{"records":{"rows":[["plan"]]}}],"execution_time_ms":12}`))
	}))
	defer server.Close()

	resultsDir := t.TempDir()
	client := &HTTPClient{client: server.Client(), Host: []byte(server.URL)}
	q := testQuery("SELECT 1")
	q.SetID(0)
	_, err := client.Do(q, &HTTPClientDoOptions{
		database:              "benchmark",
		explainAnalyzeVerbose: true,
		explainResults:        newExplainResultWriter(resultsDir),
	})
	if err != nil {
		t.Fatal(err)
	}

	if got, want := receivedSQL, "EXPLAIN ANALYZE VERBOSE SELECT 1"; got != want {
		t.Fatalf("executed SQL = %q, want %q", got, want)
	}
	if receivedDatabase != "benchmark" {
		t.Fatalf("database = %q, want benchmark", receivedDatabase)
	}
	encoded, err := os.ReadFile(filepath.Join(resultsDir, "cold.json"))
	if err != nil {
		t.Fatal(err)
	}
	var result explainResult
	if err := json.Unmarshal(encoded, &result); err != nil {
		t.Fatal(err)
	}
	if result.Phase != "cold" || result.QueryIndex != 0 || result.SQL != "SELECT 1" || result.ExecutedSQL != receivedSQL {
		t.Fatalf("unexpected explain result: %+v", result)
	}
	if !json.Valid(result.Response) {
		t.Fatalf("saved response is not valid JSON: %s", result.Response)
	}
}

func TestHTTPClientNormalQueryIsUnchanged(t *testing.T) {
	var receivedSQL string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		receivedSQL = r.URL.Query().Get("sql")
		_, _ = w.Write([]byte(`{"output":[]}`))
	}))
	defer server.Close()

	client := &HTTPClient{client: server.Client(), Host: []byte(server.URL)}
	q := testQuery("SELECT 1")
	if _, err := client.Do(q, &HTTPClientDoOptions{database: "benchmark"}); err != nil {
		t.Fatal(err)
	}
	if receivedSQL != "SELECT 1" {
		t.Fatalf("executed SQL = %q, want SELECT 1", receivedSQL)
	}
}

func TestExplainResultWriterUsesHotSequenceNamesAndRefusesOverwrite(t *testing.T) {
	directory := t.TempDir()
	writer := newExplainResultWriter(directory)
	if err := writer.write(2, "SELECT 2", explainPrefix+"SELECT 2", []byte(`{"output":[]}`)); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(directory, "hot-002.json")
	if _, err := os.Stat(path); err != nil {
		t.Fatal(err)
	}
	if err := writer.write(2, "SELECT 2", explainPrefix+"SELECT 2", []byte(`{"output":[]}`)); err == nil {
		t.Fatal("expected duplicate explain result to fail")
	}
}

func TestExplainResultWriterRejectsGreptimeError(t *testing.T) {
	directory := t.TempDir()
	writer := newExplainResultWriter(directory)
	if err := writer.write(0, "SELECT bad", explainPrefix+"SELECT bad", []byte(`{"code":1004,"error":"bad query"}`)); err == nil {
		t.Fatal("expected GreptimeDB error response to fail")
	}
	if _, err := os.Stat(filepath.Join(directory, "cold.json")); !os.IsNotExist(err) {
		t.Fatalf("error response should not create cold.json: %v", err)
	}
}
