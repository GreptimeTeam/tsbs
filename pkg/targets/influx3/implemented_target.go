package influx3

import (
	"time"

	"github.com/blagojts/viper"
	"github.com/spf13/pflag"
	"github.com/timescale/tsbs/pkg/data/serialize"
	"github.com/timescale/tsbs/pkg/data/source"
	"github.com/timescale/tsbs/pkg/targets"
	"github.com/timescale/tsbs/pkg/targets/constants"
	"github.com/timescale/tsbs/pkg/targets/influx"
)

// NewTarget returns the InfluxDB 3 target.
func NewTarget() targets.ImplementedTarget {
	return &influx3Target{}
}

type influx3Target struct{}

func (t *influx3Target) TargetSpecificFlags(flagPrefix string, flagSet *pflag.FlagSet) {
	flagSet.String(flagPrefix+"urls", "http://localhost:8181", "InfluxDB 3 URLs, comma-separated. Will be used in a round-robin fashion.")
	flagSet.Duration(flagPrefix+"backoff", time.Second, "Time to wait before retrying a transient write failure.")
	flagSet.Bool(flagPrefix+"gzip", true, "Whether to gzip write requests.")
	flagSet.String(flagPrefix+"auth-token", "", "InfluxDB 3 token used for writes.")
	flagSet.String(flagPrefix+"admin-token", "", "InfluxDB 3 admin token used for database lifecycle operations. Defaults to auth-token.")
	flagSet.Bool(flagPrefix+"accept-partial", false, "Whether InfluxDB 3 may accept valid lines from an otherwise invalid batch.")
	flagSet.Bool(flagPrefix+"no-sync", false, "Acknowledge writes before the WAL is persisted. This reduces durability.")
}

func (t *influx3Target) TargetName() string {
	return constants.FormatInflux3
}

func (t *influx3Target) Serializer() serialize.PointSerializer {
	return &influx.Serializer{}
}

func (t *influx3Target) Benchmark(string, *source.DataSourceConfig, *viper.Viper) (targets.Benchmark, error) {
	panic("not implemented")
}
