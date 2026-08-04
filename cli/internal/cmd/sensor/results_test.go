// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// fakeSensorResult builds a minimal api.SensorResultRead fixture keyed off
// the stub sensor id.
func fakeSensorResult(t *testing.T) api.SensorResultRead {
	t.Helper()
	reason := "dispatch_error"
	evidence := map[string]interface{}{"reason": reason}
	return api.SensorResultRead{
		SensorId:    parseStubUUID(t, stubSensorID),
		EvaluatedAt: time.Date(2026, 8, 1, 12, 0, 0, 0, time.UTC),
		State:       api.SensorResultReadStateCritical,
		Value:       42,
		Evidence:    &evidence,
		Reason:      &reason,
	}
}

func TestRunResultsHappyPath(t *testing.T) {
	cursor := "Y3Vyc29yLXRva2Vu"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/results",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodGet {
				t.Errorf("expected GET; got %s", r.Method)
			}
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(api.SensorResultListResponse{
				Items:      []api.SensorResultRead{fakeSensorResult(t)},
				NextCursor: &cursor,
			})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{
		SensorID:          stubSensorID,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runResults: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if !strings.Contains(out, "critical") {
		t.Errorf("expected state in stdout; got %q", out)
	}
	if !strings.Contains(out, "dispatch_error") {
		t.Errorf("expected reason in stdout; got %q", out)
	}
	if !strings.Contains(out, "--cursor="+cursor) {
		t.Errorf("expected next-cursor hint in stdout; got %q", out)
	}
}

func TestRunResultsEmptyResponse(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/results",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(api.SensorResultListResponse{Items: nil})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{
		SensorID:          stubSensorID,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runResults: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no evidence rows") {
		t.Errorf("expected empty-list message; got %q", stdout.String())
	}
}

func TestRunResultsForwardsFilters(t *testing.T) {
	var captured url.Values
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/results",
		func(w http.ResponseWriter, r *http.Request) {
			captured = r.URL.Query()
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(api.SensorResultListResponse{})
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{
		SensorID:          stubSensorID,
		From:              "2026-08-01T00:00:00Z",
		To:                "2026-08-02T00:00:00Z",
		State:             "degraded",
		Limit:             25,
		Cursor:            "abc123",
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runResults: %v; stderr=%s", err, stderr.String())
	}
	checks := map[string]string{
		"from":   "2026-08-01T00:00:00Z",
		"to":     "2026-08-02T00:00:00Z",
		"state":  "degraded",
		"limit":  "25",
		"cursor": "abc123",
	}
	for key, want := range checks {
		if got := captured.Get(key); got != want {
			t.Errorf("query %q = %q; want %q", key, got, want)
		}
	}
}

func TestRunResultsInvalidStateFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{SensorID: stubSensorID, State: "bogus"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--state must be one of") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunResultsInvalidFromFailsFast(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{SensorID: stubSensorID, From: "not-a-timestamp"}); err == nil {
		t.Fatalf("expected validation error")
	}
	if !strings.Contains(stderr.String(), "--from is not a valid RFC 3339 timestamp") {
		t.Errorf("expected validation message; got %q", stderr.String())
	}
}

func TestRunResults404SurfacesNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/results",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"sensor_not_found"}`))
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	if err := runResults(cmd, resultsOptions{
		SensorID:          stubSensorID,
		BackplaneOverride: srv.URL,
	}); err == nil {
		t.Fatalf("expected 404 to surface as error")
	}
	if !strings.Contains(stderr.String(), "sensor_not_found") {
		t.Errorf("expected backend detail; got %q", stderr.String())
	}
}
