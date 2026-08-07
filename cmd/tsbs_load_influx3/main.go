// tsbs_load_influx3 loads line protocol into InfluxDB 3 Core or Enterprise.
package main

import (
	"bufio"
	"bytes"
	"fmt"
	"log"
	"strings"
	"sync"
	"time"

	"github.com/blagojts/viper"
	"github.com/spf13/pflag"
	"github.com/timescale/tsbs/internal/utils"
	"github.com/timescale/tsbs/load"
	"github.com/timescale/tsbs/pkg/targets"
	"github.com/timescale/tsbs/pkg/targets/constants"
	"github.com/timescale/tsbs/pkg/targets/initializers"
)

var (
	daemonURLs    []string
	backoff       time.Duration
	useGzip       bool
	authToken     string
	adminToken    string
	acceptPartial bool
	noSync        bool
)

var (
	loader  load.BenchmarkRunner
	config  load.BenchmarkRunnerConfig
	bufPool sync.Pool
	target  targets.ImplementedTarget
)

var fatal = log.Fatalf

func init() {
	target = initializers.GetTarget(constants.FormatInflux3)
	config = load.BenchmarkRunnerConfig{}
	config.AddToFlagSet(pflag.CommandLine)
	target.TargetSpecificFlags("", pflag.CommandLine)
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

	backoff = viper.GetDuration("backoff")
	useGzip = viper.GetBool("gzip")
	authToken = viper.GetString("auth-token")
	adminToken = viper.GetString("admin-token")
	if adminToken == "" {
		adminToken = authToken
	}
	acceptPartial = viper.GetBool("accept-partial")
	noSync = viper.GetBool("no-sync")

	config.HashWorkers = false
	loader = load.GetBenchmarkRunner(config)
}

type benchmark struct{}

func (b *benchmark) GetDataSource() targets.DataSource {
	return &fileDataSource{scanner: bufio.NewScanner(load.GetBufferedReader(config.FileName))}
}

func (b *benchmark) GetBatchFactory() targets.BatchFactory { return &factory{} }

func (b *benchmark) GetPointIndexer(_ uint) targets.PointIndexer {
	return &targets.ConstantIndexer{}
}

func (b *benchmark) GetProcessor() targets.Processor { return &processor{} }

func (b *benchmark) GetDBCreator() targets.DBCreator {
	if !config.DoCreateDB && !config.DoAbortOnExist {
		return nil
	}
	return &dbCreator{}
}

func main() {
	bufPool = sync.Pool{New: func() interface{} {
		return bytes.NewBuffer(make([]byte, 0, 4*1024*1024))
	}}
	loader.RunBenchmark(&benchmark{})
}
