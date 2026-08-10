package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"

	"github.com/timescale/tsbs/pkg/query"
)

type HTTPClient struct {
	client *http.Client
	host   string
}

type HTTPClientDoOptions struct {
	Debug          int
	PrintResponses bool
	Database       string
	Token          string
}

type queryRequest struct {
	Database string                 `json:"db"`
	Query    string                 `json:"q"`
	Format   string                 `json:"format"`
	Params   map[string]interface{} `json:"params,omitempty"`
}

func NewHTTPClient(host string) *HTTPClient {
	transport := &http.Transport{MaxIdleConnsPerHost: 1024}
	return &HTTPClient{client: &http.Client{Transport: transport}, host: host}
}

func (c *HTTPClient) Do(q *query.HTTP, opts *HTTPClientDoOptions) (float64, error) {
	payload, err := json.Marshal(queryRequest{
		Database: opts.Database,
		Query:    string(q.RawQuery),
		Format:   "json",
	})
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequest(http.MethodPost, c.host+string(q.Path), bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if opts.Token != "" {
		req.Header.Set("Authorization", "Bearer "+opts.Token)
	}

	if opts.Debug > 0 {
		fmt.Fprintf(os.Stderr, "debug: %s -- %s\n", q.HumanLabel, q.HumanDescription)
		if opts.Debug >= 3 {
			fmt.Fprintf(os.Stderr, "debug:   SQL: %s\n", q.RawQuery)
		}
	}

	start := time.Now()
	resp, err := c.client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	body, readErr := io.ReadAll(resp.Body)
	lag := float64(time.Since(start).Nanoseconds()) / 1e6
	if readErr != nil {
		return lag, readErr
	}
	if resp.StatusCode != http.StatusOK {
		return lag, fmt.Errorf("InfluxDB 3 query failed (status %d): %s", resp.StatusCode, body)
	}

	if opts.Debug > 0 {
		fmt.Fprintf(os.Stderr, "debug: %s in %7.2fms\n", q.HumanLabel, lag)
		if opts.Debug >= 4 {
			fmt.Fprintf(os.Stderr, "debug:   response: %s\n", body)
		}
	}
	if opts.PrintResponses {
		var pretty bytes.Buffer
		if err := json.Indent(&pretty, body, "", "  "); err != nil {
			fmt.Printf("ID %d SQL: %s\n%s\n", q.GetID(), q.RawQuery, body)
		} else {
			fmt.Printf("ID %d SQL: %s\n%s\n", q.GetID(), q.RawQuery, pretty.Bytes())
		}
	}
	return lag, nil
}
