package influx3

import (
	"math/rand"
	"strings"
	"testing"
	"time"

	"github.com/timescale/tsbs/cmd/tsbs_generate_queries/uses/devops"
	"github.com/timescale/tsbs/pkg/query"
)

func newTestDevops(t *testing.T) *Devops {
	t.Helper()
	rand.Seed(123)
	start := time.Date(2016, 1, 1, 0, 0, 0, 0, time.UTC)
	generator, err := (&BaseGenerator{}).NewDevops(start, start.Add(72*time.Hour), 100)
	if err != nil {
		t.Fatal(err)
	}
	return generator.(*Devops)
}

func assertHTTPQuery(t *testing.T, q query.Query, fragments ...string) {
	t.Helper()
	hq := q.(*query.HTTP)
	if got := string(hq.Method); got != "POST" {
		t.Fatalf("unexpected method: %q", got)
	}
	if got := string(hq.Path); got != queryPath {
		t.Fatalf("unexpected path: %q", got)
	}
	for _, fragment := range fragments {
		if !strings.Contains(string(hq.RawQuery), fragment) {
			t.Errorf("query does not contain %q:\n%s", fragment, hq.RawQuery)
		}
	}
	q.Release()
}

func TestHostFilter(t *testing.T) {
	d := newTestDevops(t)
	if got, want := d.getHostWhereWithHostnames([]string{"host_1", "host_2"}), "hostname IN ('host_1', 'host_2')"; got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestCPUQueries(t *testing.T) {
	t.Run("single group by", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.GroupByTime(q, 2, 5, time.Hour)
		assertHTTPQuery(t, q,
			"date_bin(INTERVAL '1 minute', time)",
			"max(usage_iowait) AS max_usage_iowait",
			"hostname IN (",
			"GROUP BY 1",
			"ORDER BY 1 ASC",
		)
	})

	t.Run("order by limit", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.GroupByOrderByLimit(q)
		assertHTTPQuery(t, q, "WHERE time < '", "ORDER BY 1 DESC", "LIMIT 5")
	})

	t.Run("double group by", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.GroupByTimeAndPrimaryTag(q, 5)
		assertHTTPQuery(t, q,
			"date_bin(INTERVAL '1 hour', time)",
			"avg(usage_iowait) AS avg_usage_iowait",
			"GROUP BY 1, hostname",
			"ORDER BY 1 ASC, hostname ASC",
		)
	})

	t.Run("max all", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.MaxAllCPU(q, 8, devops.MaxAllDuration)
		assertHTTPQuery(t, q,
			"max(usage_user) AS max_usage_user",
			"max(usage_guest_nice) AS max_usage_guest_nice",
			"hostname IN (",
		)
	})

	t.Run("last point", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.LastPointPerHost(q)
		assertHTTPQuery(t, q,
			"last_value(time ORDER BY time) AS time",
			"last_value(region ORDER BY time) AS region",
			"last_value(usage_guest_nice ORDER BY time) AS usage_guest_nice",
			"GROUP BY hostname",
			"ORDER BY hostname ASC",
		)
	})

	t.Run("high CPU all hosts", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.HighCPUForHosts(q, 0)
		if strings.Contains(string(q.(*query.HTTP).RawQuery), "hostname IN") {
			t.Fatal("all-host query unexpectedly contains a host filter")
		}
		assertHTTPQuery(t, q, "usage_user > 90.0", "time >= '", "time < '", "ORDER BY time ASC, hostname ASC")
	})

	t.Run("high CPU one host", func(t *testing.T) {
		d := newTestDevops(t)
		q := d.GenerateEmptyQuery()
		d.HighCPUForHosts(q, 1)
		assertHTTPQuery(t, q, "usage_user > 90.0", "hostname IN ('host_")
	})
}
