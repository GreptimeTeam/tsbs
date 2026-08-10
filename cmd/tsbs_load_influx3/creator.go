package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
)

type dbCreator struct {
	daemonURL string
	client    *http.Client
}

func (d *dbCreator) Init() {
	d.daemonURL = daemonURLs[0]
	d.client = &http.Client{}
}

func (d *dbCreator) newRequest(method, endpoint string, body io.Reader) (*http.Request, error) {
	req, err := http.NewRequest(method, d.daemonURL+endpoint, body)
	if err != nil {
		return nil, err
	}
	if adminToken != "" {
		req.Header.Set("Authorization", "Bearer "+adminToken)
	}
	return req, nil
}

func responseError(action string, resp *http.Response, body []byte) error {
	return fmt.Errorf("InfluxDB 3 %s failed (status %d): %s", action, resp.StatusCode, body)
}

func (d *dbCreator) DBExists(dbName string) bool {
	req, err := d.newRequest(http.MethodGet, "/api/v3/configure/database?format=json", nil)
	if err != nil {
		fatal("could not create InfluxDB 3 database-list request: %v", err)
		return false
	}
	resp, err := d.client.Do(req)
	if err != nil {
		fatal("could not list InfluxDB 3 databases: %v", err)
		return false
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		fatal("could not read InfluxDB 3 database list: %v", err)
		return false
	}
	if resp.StatusCode != http.StatusOK {
		fatal("%v", responseError("database list", resp, body))
		return false
	}

	var rows []map[string]interface{}
	if err := json.Unmarshal(body, &rows); err != nil {
		fatal("could not decode InfluxDB 3 database list: %v", err)
		return false
	}
	for _, row := range rows {
		if name, ok := row["iox::database"].(string); ok && name == dbName {
			return true
		}
	}
	return false
}

func (d *dbCreator) RemoveOldDB(dbName string) error {
	params := url.Values{}
	params.Set("db", dbName)
	params.Set("hard_delete_at", "now")
	req, err := d.newRequest(http.MethodDelete, "/api/v3/configure/database?"+params.Encode(), nil)
	if err != nil {
		return err
	}
	resp, err := d.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return responseError("database deletion", resp, body)
	}
	return nil
}

func (d *dbCreator) CreateDB(dbName string) error {
	body, err := json.Marshal(map[string]string{"db": dbName})
	if err != nil {
		return err
	}
	req, err := d.newRequest(http.MethodPost, "/api/v3/configure/database", bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := d.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return responseError("database creation", resp, responseBody)
	}
	return nil
}
