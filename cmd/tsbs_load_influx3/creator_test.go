package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestDBCreatorLifecycle(t *testing.T) {
	var deleted, created bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer admin-token" {
			t.Errorf("unexpected authorization: %q", got)
		}
		switch {
		case r.Method == http.MethodGet && r.URL.Path == "/api/v3/configure/database":
			fmt.Fprint(w, `[{"iox::database":"benchmark"}]`)
		case r.Method == http.MethodDelete && r.URL.Path == "/api/v3/configure/database":
			if r.URL.Query().Get("db") != "benchmark" || r.URL.Query().Get("hard_delete_at") != "now" {
				t.Errorf("unexpected delete query: %s", r.URL.RawQuery)
			}
			deleted = true
		case r.Method == http.MethodPost && r.URL.Path == "/api/v3/configure/database":
			var payload map[string]string
			if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
				t.Fatal(err)
			}
			if payload["db"] != "benchmark" {
				t.Errorf("unexpected create payload: %+v", payload)
			}
			created = true
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	oldURLs, oldToken := daemonURLs, adminToken
	defer func() { daemonURLs, adminToken = oldURLs, oldToken }()
	daemonURLs = []string{server.URL}
	adminToken = "admin-token"
	creator := &dbCreator{}
	creator.Init()
	if !creator.DBExists("benchmark") {
		t.Fatal("expected database to exist")
	}
	if err := creator.RemoveOldDB("benchmark"); err != nil {
		t.Fatal(err)
	}
	if err := creator.CreateDB("benchmark"); err != nil {
		t.Fatal(err)
	}
	if !deleted || !created {
		t.Fatalf("lifecycle incomplete: deleted=%v created=%v", deleted, created)
	}
}

func TestDBCreatorBypass(t *testing.T) {
	oldConfig := config
	defer func() { config = oldConfig }()
	config.DoCreateDB = false
	config.DoAbortOnExist = false
	if got := (&benchmark{}).GetDBCreator(); got != nil {
		t.Fatalf("expected lifecycle bypass, got %T", got)
	}
}
