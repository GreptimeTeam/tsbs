package influx3

import (
	"fmt"
	"strings"
	"time"

	"github.com/timescale/tsbs/cmd/tsbs_generate_queries/databases"
	"github.com/timescale/tsbs/cmd/tsbs_generate_queries/uses/devops"
	"github.com/timescale/tsbs/pkg/query"
)

const databaseLabel = "InfluxDB 3"

var cpuTagColumns = []string{
	"hostname",
	"region",
	"datacenter",
	"rack",
	"os",
	"arch",
	"team",
	"service",
	"service_version",
	"service_environment",
}

// Devops produces InfluxDB 3 SQL queries for the TSBS devops query types.
type Devops struct {
	*BaseGenerator
	*devops.Core
}

func (d *Devops) getHostWhereWithHostnames(hostnames []string) string {
	quoted := make([]string, len(hostnames))
	for i, hostname := range hostnames {
		quoted[i] = fmt.Sprintf("'%s'", strings.ReplaceAll(hostname, "'", "''"))
	}
	return fmt.Sprintf("hostname IN (%s)", strings.Join(quoted, ", "))
}

func (d *Devops) getHostWhereString(nHosts int) string {
	hostnames, err := d.GetRandomHosts(nHosts)
	databases.PanicIfErr(err)
	return d.getHostWhereWithHostnames(hostnames)
}

func getSelectClausesAggMetrics(agg string, metrics []string) []string {
	clauses := make([]string, len(metrics))
	for i, metric := range metrics {
		clauses[i] = fmt.Sprintf("%[1]s(%[2]s) AS %[1]s_%[2]s", agg, metric)
	}
	return clauses
}

func (d *Devops) GroupByTime(qi query.Query, nHosts, numMetrics int, timeRange time.Duration) {
	interval := d.Interval.MustRandWindow(timeRange)
	metrics, err := devops.GetCPUMetricsSlice(numMetrics)
	databases.PanicIfErr(err)

	sql := fmt.Sprintf(`SELECT date_bin(INTERVAL '1 minute', time) AS minute,
       %s
FROM cpu
WHERE %s AND time >= '%s' AND time < '%s'
GROUP BY 1
ORDER BY 1 ASC`,
		strings.Join(getSelectClausesAggMetrics("max", metrics), ", "),
		d.getHostWhereString(nHosts), interval.StartString(), interval.EndString())

	humanLabel := fmt.Sprintf("%s %d cpu metric(s), random %4d hosts, random %s by 1m", databaseLabel, numMetrics, nHosts, timeRange)
	humanDesc := fmt.Sprintf("%s: %s", humanLabel, interval.StartString())
	d.fillInQuery(qi, humanLabel, humanDesc, sql)
}

func (d *Devops) GroupByOrderByLimit(qi query.Query) {
	interval := d.Interval.MustRandWindow(time.Hour)
	sql := fmt.Sprintf(`SELECT date_bin(INTERVAL '1 minute', time) AS minute,
       max(usage_user) AS max_usage_user
FROM cpu
WHERE time < '%s'
GROUP BY 1
ORDER BY 1 DESC
LIMIT 5`, interval.EndString())

	humanLabel := databaseLabel + " max cpu over last 5 min-intervals (random end)"
	humanDesc := fmt.Sprintf("%s: %s", humanLabel, interval.EndString())
	d.fillInQuery(qi, humanLabel, humanDesc, sql)
}

func (d *Devops) GroupByTimeAndPrimaryTag(qi query.Query, numMetrics int) {
	interval := d.Interval.MustRandWindow(devops.DoubleGroupByDuration)
	metrics, err := devops.GetCPUMetricsSlice(numMetrics)
	databases.PanicIfErr(err)

	sql := fmt.Sprintf(`SELECT date_bin(INTERVAL '1 hour', time) AS hour,
       hostname,
       %s
FROM cpu
WHERE time >= '%s' AND time < '%s'
GROUP BY 1, hostname
ORDER BY 1 ASC, hostname ASC`,
		strings.Join(getSelectClausesAggMetrics("avg", metrics), ", "),
		interval.StartString(), interval.EndString())

	humanLabel := devops.GetDoubleGroupByLabel(databaseLabel, numMetrics)
	humanDesc := fmt.Sprintf("%s: %s", humanLabel, interval.StartString())
	d.fillInQuery(qi, humanLabel, humanDesc, sql)
}

func (d *Devops) MaxAllCPU(qi query.Query, nHosts int, duration time.Duration) {
	interval := d.Interval.MustRandWindow(duration)
	sql := fmt.Sprintf(`SELECT date_bin(INTERVAL '1 hour', time) AS hour,
       %s
FROM cpu
WHERE %s AND time >= '%s' AND time < '%s'
GROUP BY 1
ORDER BY 1 ASC`,
		strings.Join(getSelectClausesAggMetrics("max", devops.GetAllCPUMetrics()), ", "),
		d.getHostWhereString(nHosts), interval.StartString(), interval.EndString())

	humanLabel := devops.GetMaxAllLabel(databaseLabel, nHosts)
	humanDesc := fmt.Sprintf("%s: %s", humanLabel, interval.StartString())
	d.fillInQuery(qi, humanLabel, humanDesc, sql)
}

func (d *Devops) LastPointPerHost(qi query.Query) {
	columns := append([]string{}, cpuTagColumns...)
	columns = append(columns, devops.GetAllCPUMetrics()...)

	selectClauses := []string{"hostname", "last_value(time ORDER BY time) AS time"}
	for _, column := range columns[1:] {
		selectClauses = append(selectClauses, fmt.Sprintf("last_value(%[1]s ORDER BY time) AS %[1]s", column))
	}

	sql := fmt.Sprintf("SELECT %s\nFROM cpu\nGROUP BY hostname\nORDER BY hostname ASC", strings.Join(selectClauses, ",\n       "))
	humanLabel := databaseLabel + " last row per host"
	d.fillInQuery(qi, humanLabel, humanLabel+": cpu", sql)
}

func (d *Devops) HighCPUForHosts(qi query.Query, nHosts int) {
	interval := d.Interval.MustRandWindow(devops.HighCPUDuration)
	predicates := []string{
		"usage_user > 90.0",
		fmt.Sprintf("time >= '%s'", interval.StartString()),
		fmt.Sprintf("time < '%s'", interval.EndString()),
	}
	if nHosts > 0 {
		predicates = append(predicates, d.getHostWhereString(nHosts))
	}

	sql := fmt.Sprintf("SELECT *\nFROM cpu\nWHERE %s\nORDER BY time ASC, hostname ASC", strings.Join(predicates, " AND "))
	humanLabel, err := devops.GetHighCPULabel(databaseLabel, nHosts)
	databases.PanicIfErr(err)
	humanDesc := fmt.Sprintf("%s: %s", humanLabel, interval.StartString())
	d.fillInQuery(qi, humanLabel, humanDesc, sql)
}
