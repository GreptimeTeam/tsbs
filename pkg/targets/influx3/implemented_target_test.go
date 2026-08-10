package influx3

import (
	"bytes"
	"testing"
	"time"

	"github.com/timescale/tsbs/pkg/data"
	"github.com/timescale/tsbs/pkg/targets/constants"
)

func TestTarget(t *testing.T) {
	target := NewTarget()
	if got := target.TargetName(); got != constants.FormatInflux3 {
		t.Fatalf("unexpected target name: got %q", got)
	}

	p := data.NewPoint()
	p.SetMeasurementName([]byte("cpu"))
	p.AppendTag([]byte("hostname"), "host_0")
	p.AppendField([]byte("usage_user"), int64(42))
	timestamp := time.Unix(0, 123).UTC()
	p.SetTimestamp(&timestamp)

	var buf bytes.Buffer
	if err := target.Serializer().Serialize(p, &buf); err != nil {
		t.Fatal(err)
	}
	if got, want := buf.String(), "cpu,hostname=host_0 usage_user=42i 123\n"; got != want {
		t.Fatalf("unexpected serialization: got %q want %q", got, want)
	}
}
