package main

import (
	"strings"
	"testing"

	"github.com/timescale/tsbs/pkg/query"
)

func TestValidateExplainOptions(t *testing.T) {
	tests := []struct {
		name       string
		config     query.BenchmarkRunnerConfig
		explain    bool
		resultsDir string
		wantError  string
	}{
		{
			name:       "results require explain",
			config:     query.BenchmarkRunnerConfig{Workers: 1},
			resultsDir: "results",
			wantError:  "--explain-results-dir requires --explain-analyze-verbose",
		},
		{
			name:       "results reject prewarming",
			config:     query.BenchmarkRunnerConfig{Workers: 1, PrewarmQueries: true},
			explain:    true,
			resultsDir: "results",
			wantError:  "--explain-results-dir cannot be combined with --prewarm-queries",
		},
		{
			name:      "explain requires one worker",
			config:    query.BenchmarkRunnerConfig{Workers: 2},
			explain:   true,
			wantError: "--explain-analyze-verbose requires --workers=1",
		},
		{
			name:       "explain results",
			config:     query.BenchmarkRunnerConfig{Workers: 1},
			explain:    true,
			resultsDir: "results",
		},
		{
			name:    "prewarmed explain without results",
			config:  query.BenchmarkRunnerConfig{Workers: 1, PrewarmQueries: true},
			explain: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateExplainOptions(tt.config, tt.explain, tt.resultsDir)
			if tt.wantError == "" {
				if err != nil {
					t.Fatalf("validateExplainOptions() error = %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), tt.wantError) {
				t.Fatalf("validateExplainOptions() error = %v, want containing %q", err, tt.wantError)
			}
		})
	}
}
