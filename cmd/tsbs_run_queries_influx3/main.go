// tsbs_run_queries_influx3 benchmarks InfluxDB 3 SQL requests read from stdin.
package main

import (
	"fmt"
	"log"
	"strings"

	"github.com/blagojts/viper"
	"github.com/spf13/pflag"
	"github.com/timescale/tsbs/internal/utils"
	"github.com/timescale/tsbs/pkg/query"
)

var (
	daemonURLs []string
	authToken  string
	runner     *query.BenchmarkRunner
)

func init() {
	var config query.BenchmarkRunnerConfig
	config.AddToFlagSet(pflag.CommandLine)
	pflag.String("urls", "http://localhost:8181", "InfluxDB 3 URLs, comma-separated. Will be used in a round-robin fashion.")
	pflag.String("auth-token", "", "InfluxDB 3 token used for queries.")
	pflag.Parse()

	if err := utils.SetupConfigFile(); err != nil {
		panic(fmt.Errorf("fatal error config file: %s", err))
	}
	if err := viper.Unmarshal(&config); err != nil {
		panic(fmt.Errorf("unable to decode config: %s", err))
	}

	for _, daemonURL := range strings.Split(viper.GetString("urls"), ",") {
		daemonURL = strings.TrimRight(strings.TrimSpace(daemonURL), "/")
		if daemonURL != "" {
			daemonURLs = append(daemonURLs, daemonURL)
		}
	}
	if len(daemonURLs) == 0 {
		log.Fatal("missing 'urls' flag")
	}
	authToken = viper.GetString("auth-token")
	runner = query.NewBenchmarkRunner(config)
}

func main() { runner.Run(&query.HTTPPool, newProcessor) }

type processor struct {
	client *HTTPClient
	opts   *HTTPClientDoOptions
}

func newProcessor() query.Processor { return &processor{} }

func (p *processor) Init(worker int) {
	p.opts = &HTTPClientDoOptions{
		Debug:          runner.DebugLevel(),
		PrintResponses: runner.DoPrintResponses(),
		Database:       runner.DatabaseName(),
		Token:          authToken,
	}
	p.client = NewHTTPClient(daemonURLs[worker%len(daemonURLs)])
}

func (p *processor) ProcessQuery(q query.Query, _ bool) ([]*query.Stat, error) {
	lag, err := p.client.Do(q.(*query.HTTP), p.opts)
	if err != nil {
		return nil, err
	}
	stat := query.GetStat()
	stat.Init(q.HumanLabelName(), lag)
	return []*query.Stat{stat}, nil
}
