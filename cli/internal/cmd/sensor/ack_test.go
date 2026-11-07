// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

func runAckCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd, stdout, stderr := newRunCmd(t)
	root := NewRootCmd()
	root.SetOut(stdout)
	root.SetErr(stderr)
	root.SetArgs(append([]string{"ack"}, args...))
	err := root.ExecuteContext(cmd.Context())
	return stdout.String(), stderr.String(), err
}

func fakeAck(until string) map[string]any {
	return map[string]any{
		"id":             "00000000-0000-0000-0000-0000000000a1",
		"sensor_id":      stubSensorID,
		"acked_state":    "degraded",
		"reason":         "fix_planned",
		"note":           "Disk swap on Friday",
		"created_by_sub": "op-alice",
		"created_at":     "2026-11-07T10:00:00Z",
		"expires_at":     until,
		"ended_at":       nil,
		"end_reason":     nil,
	}
}

func TestRunAckSendsUntilReasonNote(t *testing.T) {
	var raw map[string]any
	var method string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/acknowledgement", func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &raw); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(fakeAck("2026-12-01T18:00:00Z"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runAckCmd(t, stubSensorID,
		"--until", "2026-12-01T18:00:00Z",
		"--reason", "fix_planned",
		"--note", "Disk swap on Friday",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("ack: %v; stderr=%s", err, stderr)
	}
	if method != http.MethodPost {
		t.Errorf("expected POST; got %s", method)
	}
	if raw["expires_at"] != "2026-12-01T18:00:00Z" || raw["reason"] != "fix_planned" ||
		raw["note"] != "Disk swap on Friday" {
		t.Errorf("unexpected body: %v", raw)
	}
	for _, want := range []string{"acknowledged sensor", "by:     op-alice", "until:  2026-12-01T18:00:00Z", "reason: fix_planned"} {
		if !strings.Contains(stdout, want) {
			t.Errorf("expected %q in stdout; got %q", want, stdout)
		}
	}
}

func TestRunAckRefusesMissingOrBadFlagsWithoutACall(t *testing.T) {
	called := false
	mux := http.NewServeMux()
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { called = true })
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cases := map[string][]string{
		"no until":     {"--reason", "fix_planned", "--note", "x"},
		"bad until":    {"--until", "tomorrow", "--reason", "fix_planned", "--note", "x"},
		"negative":     {"--until", "-2h", "--reason", "fix_planned", "--note", "x"},
		"bad reason":   {"--until", "4h", "--reason", "because", "--note", "x"},
		"blank note":   {"--until", "4h", "--reason", "false_alarm", "--note", "  "},
		"no note flag": {"--until", "4h", "--reason", "false_alarm"},
	}
	for name, flags := range cases {
		t.Run(name, func(t *testing.T) {
			args := append([]string{stubSensorID}, flags...)
			args = append(args, "--backplane", srv.URL)
			if _, _, err := runAckCmd(t, args...); err == nil {
				t.Fatalf("expected an error for %s", name)
			}
		})
	}
	if called {
		t.Errorf("no request may be sent for a bad flag")
	}
}

func TestParseUntilDurationIsFromNow(t *testing.T) {
	now := time.Date(2026, 11, 7, 10, 0, 0, 0, time.UTC)
	got, err := parseUntil("72h", now)
	if err != nil {
		t.Fatalf("parseUntil: %v", err)
	}
	if !got.Equal(now.Add(72 * time.Hour)) {
		t.Errorf("got %v", got)
	}
}

func TestRunAckShowsServerRefusal(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/acknowledgement", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"sensor_ack_already_open"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	_, stderr, err := runAckCmd(t, stubSensorID,
		"--until", "4h", "--reason", "fix_planned", "--note", "x", "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("expected an error for HTTP 409")
	}
	if !strings.Contains(stderr, "sensor_ack_already_open") {
		t.Errorf("expected the server reason in stderr; got %q", stderr)
	}
}
