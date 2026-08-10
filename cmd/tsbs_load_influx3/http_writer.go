package main

import (
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"

	"github.com/valyala/fasthttp"
)

const (
	httpClientName        = "tsbs_load_influx3"
	headerContentEncoding = "Content-Encoding"
	headerGzip            = "gzip"
	headerAuthorization   = "Authorization"
)

type HTTPWriterConfig struct {
	Host          string
	Database      string
	Token         string
	AcceptPartial bool
	NoSync        bool
	DebugInfo     string
}

type HTTPWriter struct {
	client fasthttp.Client
	c      HTTPWriterConfig
	url    []byte
}

type retryableWriteError struct {
	status     int
	retryAfter time.Duration
	body       string
}

func (e *retryableWriteError) Error() string {
	return fmt.Sprintf("transient InfluxDB 3 write response (status %d): %s", e.status, e.body)
}

func NewHTTPWriter(c HTTPWriterConfig) *HTTPWriter {
	params := url.Values{}
	params.Set("db", c.Database)
	params.Set("precision", "nanosecond")
	params.Set("accept_partial", strconv.FormatBool(c.AcceptPartial))
	params.Set("no_sync", strconv.FormatBool(c.NoSync))
	return &HTTPWriter{
		client: fasthttp.Client{Name: httpClientName},
		c:      c,
		url:    []byte(c.Host + "/api/v3/write_lp?" + params.Encode()),
	}
}

func (w *HTTPWriter) initializeReq(req *fasthttp.Request, body []byte, isGzip bool) {
	req.Header.SetContentType("text/plain")
	req.Header.SetMethod(fasthttp.MethodPost)
	req.Header.SetRequestURIBytes(w.url)
	if isGzip {
		req.Header.Set(headerContentEncoding, headerGzip)
	}
	if w.c.Token != "" {
		req.Header.Set(headerAuthorization, "Bearer "+w.c.Token)
	}
	req.SetBody(body)
}

func parseRetryAfter(value string, now time.Time) time.Duration {
	if seconds, err := strconv.Atoi(value); err == nil && seconds >= 0 {
		return time.Duration(seconds) * time.Second
	}
	if retryAt, err := http.ParseTime(value); err == nil && retryAt.After(now) {
		return retryAt.Sub(now)
	}
	return 0
}

func (w *HTTPWriter) executeReq(req *fasthttp.Request, resp *fasthttp.Response) (int64, error) {
	start := time.Now()
	err := w.client.Do(req, resp)
	latency := time.Since(start).Nanoseconds()
	if err != nil {
		return latency, err
	}

	status := resp.StatusCode()
	if status == fasthttp.StatusNoContent {
		return latency, nil
	}
	if status == fasthttp.StatusTooManyRequests || status == fasthttp.StatusServiceUnavailable {
		return latency, &retryableWriteError{
			status:     status,
			retryAfter: parseRetryAfter(string(resp.Header.Peek("Retry-After")), time.Now()),
			body:       string(resp.Body()),
		}
	}
	return latency, fmt.Errorf("[DebugInfo: %s] invalid InfluxDB 3 write response (status %d): %s", w.c.DebugInfo, status, resp.Body())
}

func (w *HTTPWriter) WriteLineProtocol(body []byte, isGzip bool) (int64, error) {
	req := fasthttp.AcquireRequest()
	defer fasthttp.ReleaseRequest(req)
	w.initializeReq(req, body, isGzip)

	resp := fasthttp.AcquireResponse()
	defer fasthttp.ReleaseResponse(resp)
	return w.executeReq(req, resp)
}
