package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"net/url"
	"strings"
)

type dbCreator struct {
	daemonURL string
}

func (d *dbCreator) Init() {
	d.daemonURL = daemonURLs[0] // pick first one since it always exists
}

// addAuthHeader adds Basic authentication header to the request if credentials are provided
func (d *dbCreator) addAuthHeader(req *http.Request) {
	if username != "" && password != "" {
		credentials := username + ":" + password
		encoded := base64.StdEncoding.EncodeToString([]byte(credentials))
		req.Header.Set("Authorization", "Basic "+encoded)
	}
}

func (d *dbCreator) DBExists(dbName string) bool {
	dbs, err := d.listDatabases()
	if err != nil {
		log.Fatal(err)
	}

	for _, db := range dbs {
		if db == dbName {
			return true
		}
	}
	return false
}

func (d *dbCreator) executeSQL(sql string) ([]byte, error) {
	form := url.Values{}
	form.Set("sql", sql)
	req, err := http.NewRequest(
		http.MethodPost,
		strings.TrimRight(d.daemonURL, "/")+"/v1/sql",
		strings.NewReader(form.Encode()),
	)
	if err != nil {
		return nil, fmt.Errorf("create SQL request: %w", err)
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	d.addAuthHeader(req)

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("execute SQL %q: %w", sql, err)
	}
	defer resp.Body.Close()

	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("read response for SQL %q: %w", sql, err)
	}
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf(
			"execute SQL %q returned %s: %s",
			sql,
			resp.Status,
			strings.TrimSpace(string(body)),
		)
	}
	return body, nil
}

func (d *dbCreator) listDatabases() ([]string, error) {
	body, err := d.executeSQL("SHOW DATABASES")
	if err != nil {
		return nil, fmt.Errorf("list databases: %w", err)
	}

	type listingType struct {
		Output []struct {
			Records struct {
				Rows [][]string `json:"rows"`
			} `json:"records"`
		} `json:"output"`
	}
	var listing listingType
	if err := json.Unmarshal(body, &listing); err != nil {
		return nil, fmt.Errorf("decode database listing: %w", err)
	}
	if len(listing.Output) == 0 {
		return nil, fmt.Errorf("decode database listing: response contains no output")
	}

	databaseNames := make([]string, 0, len(listing.Output[0].Records.Rows))
	for i, row := range listing.Output[0].Records.Rows {
		if len(row) == 0 {
			return nil, fmt.Errorf("decode database listing: row %d contains no database name", i)
		}
		databaseNames = append(databaseNames, row[0])
	}
	return databaseNames, nil
}

func (d *dbCreator) RemoveOldDB(dbName string) error {
	_, err := d.executeSQL(fmt.Sprintf("DROP DATABASE %s", dbName))
	return err
}

func (d *dbCreator) CreateDB(dbName string) error {
	_, err := d.executeSQL(fmt.Sprintf("CREATE DATABASE %s", dbName))
	return err
}
