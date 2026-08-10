package main

import (
	"bytes"
	"errors"
	"fmt"
	"time"

	"github.com/timescale/tsbs/pkg/targets"
	"github.com/valyala/fasthttp"
)

const backingOffChanCap = 100

var printFn = fmt.Printf

type processor struct {
	backingOffChan chan bool
	backingOffDone chan struct{}
	httpWriter     *HTTPWriter
}

func (p *processor) Init(worker int, _, _ bool) {
	daemonURL := daemonURLs[worker%len(daemonURLs)]
	p.initWithHTTPWriter(worker, NewHTTPWriter(HTTPWriterConfig{
		Host:          daemonURL,
		Database:      loader.DatabaseName(),
		Token:         authToken,
		AcceptPartial: acceptPartial,
		NoSync:        noSync,
		DebugInfo:     fmt.Sprintf("worker #%d, dest url: %s", worker, daemonURL),
	}))
}

func (p *processor) initWithHTTPWriter(worker int, writer *HTTPWriter) {
	p.backingOffChan = make(chan bool, backingOffChanCap)
	p.backingOffDone = make(chan struct{})
	p.httpWriter = writer
	go p.processBackoffMessages(worker)
}

func (p *processor) Close(_ bool) {
	close(p.backingOffChan)
	<-p.backingOffDone
}

func (p *processor) ProcessBatch(input targets.Batch, doLoad bool) (uint64, uint64) {
	b := input.(*batch)
	if doLoad {
		for {
			var err error
			if useGzip {
				compressed := bufPool.Get().(*bytes.Buffer)
				fasthttp.WriteGzip(compressed, b.buf.Bytes())
				_, err = p.httpWriter.WriteLineProtocol(compressed.Bytes(), true)
				compressed.Reset()
				bufPool.Put(compressed)
			} else {
				_, err = p.httpWriter.WriteLineProtocol(b.buf.Bytes(), false)
			}

			var retryable *retryableWriteError
			if errors.As(err, &retryable) {
				p.backingOffChan <- true
				delay := retryable.retryAfter
				if delay <= 0 {
					delay = backoff
				}
				time.Sleep(delay)
				continue
			}
			p.backingOffChan <- false
			if err != nil {
				fatal("Error writing: %s\n", err.Error())
			}
			break
		}
	}

	metricCount, rowCount := b.metrics, uint64(b.rows)
	b.buf.Reset()
	bufPool.Put(b.buf)
	return metricCount, rowCount
}

func (p *processor) processBackoffMessages(worker int) {
	var total time.Duration
	var started time.Time
	backingOff := false
	for current := range p.backingOffChan {
		if current && !backingOff {
			started = time.Now()
			backingOff = true
		} else if !current && backingOff {
			duration := time.Since(started)
			printFn("[worker %d] backoff took %.02fsec\n", worker, duration.Seconds())
			total += duration
			backingOff = false
		}
	}
	if backingOff {
		total += time.Since(started)
	}
	printFn("[worker %d] backoffs took a total of %fsec of runtime\n", worker, total.Seconds())
	p.backingOffDone <- struct{}{}
}
