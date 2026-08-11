package query

import (
	"encoding/json"
	"testing"
	"time"
)

func TestGetTotalsMapIncludesStructuredStats(t *testing.T) {
	limit := uint64(10)
	allQueries := newStatGroup(limit)
	allQueries.push(2)
	allQueries.push(4)
	sp := &defaultStatProcessor{
		args:      &statProcessorArgs{limit: &limit},
		startTime: time.Now().Add(-time.Second),
		statMapping: map[string]*statGroup{
			labelAllQueries: allQueries,
		},
	}

	totals := sp.GetTotalsMap()
	stats := totals["overallStats"].(map[string]queryStatSummary)
	all := stats["all_queries"]
	if all.Count != 2 || all.MeanMilliseconds != 3 {
		t.Fatalf("unexpected all-query stats: %#v", all)
	}
	encoded, err := json.Marshal(totals)
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]interface{}
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	overall := decoded["overallStats"].(map[string]interface{})
	allJSON := overall["all_queries"].(map[string]interface{})
	if allJSON["count"] != float64(2) || allJSON["meanMilliseconds"] != float64(3) {
		t.Fatalf("unexpected JSON stats: %#v", allJSON)
	}
	if BenchmarkTestResultVersion != "0.2" {
		t.Fatalf("unexpected result version %q", BenchmarkTestResultVersion)
	}
}

func TestStatProcessorSend(t *testing.T) {
	s := GetStat()
	s.isWarm = true
	statPool.Put(s)
	s = GetStat()
	if s.isWarm {
		t.Errorf("initial stat came back warm unexpectedly")
	}
	s.value = 10.1
	sp := &defaultStatProcessor{}
	sp.c = make(chan *Stat, 2)
	sp.send([]*Stat{s, s})
	r := <-sp.c
	if r.value != s.value {
		t.Errorf("sent a stat and got a different one back")
	}
	if r.isWarm {
		t.Errorf("received stat is warm unexpectedly")
	}

	// 2nd value too
	r = <-sp.c
	if r.value != s.value {
		t.Errorf("sent a stat and got a different one back (2)")
	}
	if r.isWarm {
		t.Errorf("received stat is warm unexpectedly (2)")
	}

	// should not send anything
	wantLen := len(sp.c)
	sp.send(nil)
	time.Sleep(25 * time.Millisecond)
	if got := len(sp.c); got != wantLen {
		t.Errorf("empty stat array changed channel length: got %d want %d", got, wantLen)
	}
}

func TestStatProcessorSendWarm(t *testing.T) {
	s := GetStat()
	if s.isWarm {
		t.Errorf("initial stat came back warm unexpectedly")
	}
	s.value = 10.1
	sp := &defaultStatProcessor{}
	sp.c = make(chan *Stat, 2)
	sp.sendWarm([]*Stat{s, s})
	r := <-sp.c
	if r.value != s.value {
		t.Errorf("sent a stat and got a different one back")
	}
	if !r.isWarm {
		t.Errorf("received stat is NOT warm unexpectedly")
	}

	// 2nd value too
	r = <-sp.c
	if r.value != s.value {
		t.Errorf("sent a stat and got a different one back (2)")
	}
	if !r.isWarm {
		t.Errorf("received stat is NOT warm unexpectedly (2)")
	}

	// should not send anything
	wantLen := len(sp.c)
	sp.sendWarm(nil)
	time.Sleep(25 * time.Millisecond)
	if got := len(sp.c); got != wantLen {
		t.Errorf("empty stat array changed channel length: got %d want %d", got, wantLen)
	}
}
