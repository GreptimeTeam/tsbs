package main

import (
	"bytes"
	"sync"
	"testing"

	"github.com/timescale/tsbs/pkg/data"
)

func TestBatchAppend(t *testing.T) {
	bufPool = sync.Pool{New: func() interface{} { return &bytes.Buffer{} }}
	b := (&factory{}).New().(*batch)
	b.Append(data.NewLoadedPoint([]byte("cpu,hostname=host_0 usage_user=1i,usage_idle=2i 123")))
	if b.rows != 1 || b.metrics != 2 {
		t.Fatalf("unexpected counts: rows=%d metrics=%d", b.rows, b.metrics)
	}
	if got := b.buf.String(); got != "cpu,hostname=host_0 usage_user=1i,usage_idle=2i 123\n" {
		t.Fatalf("unexpected batch: %q", got)
	}
}
