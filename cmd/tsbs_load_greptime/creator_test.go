package main

import (
	"encoding/base64"
	"fmt"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
)

func TestListDatabases(t *testing.T) {
	oldUsername, oldPassword := username, password
	username, password = "greptime", "secret"
	defer func() {
		username, password = oldUsername, oldPassword
	}()

	wantAuthorization := "Basic " + base64.StdEncoding.EncodeToString([]byte("greptime:secret"))
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("unexpected method: got %s want %s", r.Method, http.MethodPost)
		}
		if got := r.Header.Get("Content-Type"); got != "application/x-www-form-urlencoded" {
			t.Errorf("unexpected content type: got %q", got)
		}
		if got := r.Header.Get("Authorization"); got != wantAuthorization {
			t.Errorf("unexpected authorization: got %q want %q", got, wantAuthorization)
		}
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse request form: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		if got := r.Form.Get("sql"); got != "SHOW DATABASES" {
			t.Errorf("unexpected SQL: got %q", got)
		}

		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, `{"output":[{"records":{"schema":{"column_schemas":[{"name":"Database","data_type":"String"}]},"rows":[["greptime_private"],["information_schema"],["public"],["benchmark"]],"total_rows":4}}],"execution_time_ms":2}`)
	}))
	defer server.Close()

	creator := &dbCreator{daemonURL: server.URL + "/"}
	want := []string{"greptime_private", "information_schema", "public", "benchmark"}
	got, err := creator.listDatabases()
	if err != nil {
		t.Fatalf("list databases: %v", err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("unexpected databases: got %v want %v", got, want)
	}
	if !creator.DBExists("benchmark") {
		t.Error("expected benchmark database to exist")
	}
	if creator.DBExists("missing") {
		t.Error("did not expect missing database to exist")
	}
}

func TestListDatabasesRejectsMalformedResponses(t *testing.T) {
	tests := []struct {
		name    string
		body    string
		wantErr string
	}{
		{name: "invalid JSON", body: `{`, wantErr: "decode database listing"},
		{name: "missing output", body: `{}`, wantErr: "response contains no output"},
		{name: "empty row", body: `{"output":[{"records":{"rows":[[]]}}]}`, wantErr: "row 0 contains no database name"},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				fmt.Fprint(w, test.body)
			}))
			defer server.Close()

			creator := &dbCreator{daemonURL: server.URL}
			_, err := creator.listDatabases()
			if err == nil || !strings.Contains(err.Error(), test.wantErr) {
				t.Fatalf("unexpected error: got %v want substring %q", err, test.wantErr)
			}
		})
	}
}

func TestCreateAndRemoveDatabase(t *testing.T) {
	var statements []string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if err := r.ParseForm(); err != nil {
			t.Errorf("parse request form: %v", err)
			w.WriteHeader(http.StatusBadRequest)
			return
		}
		statements = append(statements, r.Form.Get("sql"))
		fmt.Fprint(w, `{"output":[{"affectedrows":1}],"execution_time_ms":1}`)
	}))
	defer server.Close()

	creator := &dbCreator{daemonURL: server.URL}
	if err := creator.CreateDB("benchmark"); err != nil {
		t.Fatalf("create database: %v", err)
	}
	if err := creator.RemoveOldDB("benchmark"); err != nil {
		t.Fatalf("remove database: %v", err)
	}

	want := []string{"CREATE DATABASE benchmark", "DROP DATABASE benchmark"}
	if !reflect.DeepEqual(statements, want) {
		t.Fatalf("unexpected SQL statements: got %v want %v", statements, want)
	}
}

func TestCreateDatabaseReportsServerError(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"error":"Database 'benchmark' already exists"}`)
	}))
	defer server.Close()

	creator := &dbCreator{daemonURL: server.URL}
	err := creator.CreateDB("benchmark")
	if err == nil {
		t.Fatal("expected create database to fail")
	}
	for _, want := range []string{"400 Bad Request", "Database 'benchmark' already exists"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error %q does not contain %q", err, want)
		}
	}
}
