package main

import (
	"bufio"
	"bytes"
	"strings"

	"github.com/timescale/tsbs/pkg/data"
	"github.com/timescale/tsbs/pkg/data/usecases/common"
	"github.com/timescale/tsbs/pkg/targets"
)

const errNotThreeTuplesFmt = "parse error: line does not have 3 tuples, has %d"

var newLine = []byte("\n")

type fileDataSource struct{ scanner *bufio.Scanner }

func (d *fileDataSource) NextItem() data.LoadedPoint {
	ok := d.scanner.Scan()
	if !ok && d.scanner.Err() == nil {
		return data.LoadedPoint{}
	}
	if !ok {
		fatal("scan error: %v", d.scanner.Err())
		return data.LoadedPoint{}
	}
	return data.NewLoadedPoint(d.scanner.Bytes())
}

func (d *fileDataSource) Headers() *common.GeneratedDataHeaders { return nil }

type batch struct {
	buf     *bytes.Buffer
	rows    uint
	metrics uint64
}

func (b *batch) Len() uint { return b.rows }

func (b *batch) Append(item data.LoadedPoint) {
	line := item.Data.([]byte)
	parts := strings.Split(string(line), " ")
	if len(parts) != 3 {
		fatal(errNotThreeTuplesFmt, len(parts))
		return
	}
	b.rows++
	b.metrics += uint64(len(strings.Split(parts[1], ",")))
	b.buf.Write(line)
	b.buf.Write(newLine)
}

type factory struct{}

func (f *factory) New() targets.Batch { return &batch{buf: bufPool.Get().(*bytes.Buffer)} }
