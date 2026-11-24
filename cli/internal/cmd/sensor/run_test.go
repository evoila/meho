// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRunSensorNowHappyPath(t *testing.T) {
	var capturedPath, capturedQuery string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/run",
		func(w http.ResponseWriter, r *http.Request) {
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			capturedPath = r.URL.Path
			capturedQuery = r.URL.RawQuery
			w.Header().Set("Content-Type", "application/json")
			w.Header().Set("Meho-Audit-Id", "99999999-8888-7777-6666-555555555555")
			w.WriteHeader(http.StatusAccepted)
			_, _ = w.Write([]byte(`{"sensor_id":"` + stubSensorID + `","next_fire_at":"2026-11-19T10:00:00Z"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runSensorNow(cmd, runNowOptions{
		SensorID:          stubSensorID,
		Tenant:            stubOtherTenant,
		BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runSensorNow: %v; stderr=%s", err, stderr.String())
	}
	if capturedPath != "/api/v1/sensors/"+stubSensorID+"/run" {
		t.Errorf("unexpected path: %q", capturedPath)
	}
	if !strings.Contains(capturedQuery, "tenant_filter="+stubOtherTenant) {
		t.Errorf("expected tenant_filter; got %q", capturedQuery)
	}
	if !strings.Contains(stdout.String(), "runs at the next runner tick") {
		t.Errorf("expected run prose; got %q", stdout.String())
	}
	if !strings.Contains(stdout.String(), "audit receipt: 99999999-8888-7777-6666-555555555555\n") {
		t.Errorf("expected audit receipt line; got %q", stdout.String())
	}
}

func TestRunSensorNowPausedIsRefused(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID+"/run",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusConflict)
			_, _ = w.Write([]byte(`{"detail":"sensor_paused: the sensor is paused; resume it first (meho sensor resume ` + stubSensorID + `)"}`))
		},
	)
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runSensorNow(cmd, runNowOptions{SensorID: stubSensorID, BackplaneOverride: srv.URL})
	if err == nil {
		t.Fatalf("expected an error for a paused sensor")
	}
	if !strings.Contains(stderr.String(), "resume it first") {
		t.Errorf("expected the resume hint; got %q", stderr.String())
	}
}

func TestRunSensorNowBadID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	if err := runSensorNow(cmd, runNowOptions{SensorID: "not-a-uuid"}); err == nil {
		t.Fatalf("expected an error")
	}
	if !strings.Contains(stderr.String(), "not a valid UUID") {
		t.Errorf("unexpected stderr: %q", stderr.String())
	}
}
