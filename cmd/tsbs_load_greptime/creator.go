package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io/ioutil"
	"log"
	"net/http"
	"time"
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
		if db == loader.DatabaseName() {
			return true
		}
	}
	return false
}

func (d *dbCreator) listDatabases() ([]string, error) {
	u := fmt.Sprintf("%s/v1/sql?sql=show%%20databases", d.daemonURL)
	req, err := http.NewRequest("GET", u, nil)
	if err != nil {
		return nil, fmt.Errorf("listDatabases error: %s", err.Error())
	}
	d.addAuthHeader(req)

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("listDatabases error: %s", err.Error())
	}
	defer resp.Body.Close()

	body, err := ioutil.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	// Do ad-hoc parsing to find existing database names:
	// {"code":0,"output":[{"records":{"schema":{"column_schemas":[{"name":"Schemas","data_type":"String"}]},"rows":[["public"]]}}],"execution_time_ms":0}
	type listingType struct {
		Output []struct {
			Rows [][]string
		}
	}
	var listing listingType
	err = json.Unmarshal(body, &listing)
	if err != nil {
		return nil, err
	}

	ret := []string{}
	for _, nestedName := range listing.Output[0].Rows {
		name := nestedName[0]
		// the _internal database is skipped:
		if name == "_internal" {
			continue
		}
		ret = append(ret, name)
	}
	return ret, nil
}

func (d *dbCreator) RemoveOldDB(dbName string) error {
	u := fmt.Sprintf("%s/v1/sql?sql=drop+database+%s", d.daemonURL, dbName)
	req, err := http.NewRequest("POST", u, nil)
	if err != nil {
		return fmt.Errorf("drop db error: %s", err.Error())
	}
	req.Header.Set("Content-Type", "text/plain")
	d.addAuthHeader(req)

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("drop db error: %s", err.Error())
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("drop db returned non-200 code: %d", resp.StatusCode)
	}
	time.Sleep(time.Second)
	return nil
}

func (d *dbCreator) CreateDB(dbName string) error {
	u := fmt.Sprintf("%s/v1/sql?sql=create%%20database%%20%s", d.daemonURL, dbName)
	req, err := http.NewRequest("GET", u, nil)
	if err != nil {
		return fmt.Errorf("create db error: %s", err.Error())
	}
	d.addAuthHeader(req)

	client := &http.Client{}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("create db error: %s", err.Error())
	}
	defer resp.Body.Close()

	// does the body need to be read into the void?

	if resp.StatusCode != 200 {
		return fmt.Errorf("bad db create")
	}

	time.Sleep(time.Second)
	return nil
}
