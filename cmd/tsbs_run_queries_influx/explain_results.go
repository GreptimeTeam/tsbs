package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

const explainPrefix = "EXPLAIN ANALYZE VERBOSE "

type explainResult struct {
	Phase       string          `json:"phase"`
	QueryIndex  uint64          `json:"query_index"`
	SQL         string          `json:"sql"`
	ExecutedSQL string          `json:"executed_sql"`
	Response    json.RawMessage `json:"response"`
}

type explainResultWriter struct {
	directory string
	mu        sync.Mutex
}

type greptimeResponse struct {
	Code  *int   `json:"code"`
	Error string `json:"error"`
}

func newExplainResultWriter(directory string) *explainResultWriter {
	return &explainResultWriter{directory: directory}
}

func (w *explainResultWriter) write(queryIndex uint64, sql string, executedSQL string, response []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()

	if !json.Valid(response) {
		return fmt.Errorf("GreptimeDB explain response for query %d is not valid JSON", queryIndex)
	}
	var envelope greptimeResponse
	if err := json.Unmarshal(response, &envelope); err != nil {
		return err
	}
	if envelope.Code != nil && *envelope.Code != 0 {
		return fmt.Errorf("GreptimeDB explain query %d failed with code %d: %s", queryIndex, *envelope.Code, envelope.Error)
	}
	if envelope.Error != "" {
		return fmt.Errorf("GreptimeDB explain query %d failed: %s", queryIndex, envelope.Error)
	}
	if err := os.MkdirAll(w.directory, 0755); err != nil {
		return err
	}

	phase := "cold"
	filename := "cold.json"
	if queryIndex > 0 {
		phase = "hot"
		filename = fmt.Sprintf("hot-%03d.json", queryIndex)
	}
	destination := filepath.Join(w.directory, filename)
	if _, err := os.Stat(destination); err == nil {
		return fmt.Errorf("explain result already exists: %s", destination)
	} else if !os.IsNotExist(err) {
		return err
	}

	encoded, err := json.MarshalIndent(explainResult{
		Phase:       phase,
		QueryIndex:  queryIndex,
		SQL:         sql,
		ExecutedSQL: executedSQL,
		Response:    json.RawMessage(response),
	}, "", "  ")
	if err != nil {
		return err
	}
	encoded = append(encoded, '\n')

	temporary, err := os.CreateTemp(w.directory, ".explain-result-*")
	if err != nil {
		return err
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err := temporary.Write(encoded); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Chmod(0644); err != nil {
		temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryPath, destination)
}
