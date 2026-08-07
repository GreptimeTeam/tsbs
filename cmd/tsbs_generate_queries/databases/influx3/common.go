package influx3

import (
	"time"

	"github.com/timescale/tsbs/cmd/tsbs_generate_queries/uses/devops"
	"github.com/timescale/tsbs/cmd/tsbs_generate_queries/utils"
	"github.com/timescale/tsbs/pkg/query"
)

const queryPath = "/api/v3/query_sql"

// BaseGenerator contains settings specific to InfluxDB 3.
type BaseGenerator struct{}

func (g *BaseGenerator) GenerateEmptyQuery() query.Query {
	return query.NewHTTP()
}

func (g *BaseGenerator) fillInQuery(qi query.Query, humanLabel, humanDesc, sql string) {
	q := qi.(*query.HTTP)
	q.HumanLabel = []byte(humanLabel)
	q.HumanDescription = []byte(humanDesc)
	q.Method = []byte("POST")
	q.Path = []byte(queryPath)
	q.Body = nil
	q.RawQuery = []byte(sql)
}

func (g *BaseGenerator) NewDevops(start, end time.Time, scale int) (utils.QueryGenerator, error) {
	core, err := devops.NewCore(start, end, scale)
	if err != nil {
		return nil, err
	}
	return &Devops{BaseGenerator: g, Core: core}, nil
}
