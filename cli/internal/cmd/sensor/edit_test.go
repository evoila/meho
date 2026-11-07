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
)

// runEditCmd runs `meho sensor edit` through cobra, so the "flag was set"
// detection is the real one.
func runEditCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd, stdout, stderr := newRunCmd(t)
	root := NewRootCmd()
	root.SetOut(stdout)
	root.SetErr(stderr)
	root.SetArgs(append([]string{"edit"}, args...))
	err := root.ExecuteContext(cmd.Context())
	return stdout.String(), stderr.String(), err
}

func TestRunEditSendsOnlySetFields(t *testing.T) {
	var raw map[string]any
	var method, query string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors/"+stubSensorID, func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		query = r.URL.RawQuery
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &raw); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fakeSensor(t, "interval"))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runEditCmd(t, stubSensorID,
		"--connector-id", "vmware-rest-9.1",
		"--params", `{"folder":"<folder-id>"}`,
		"--severity", "degraded",
		"--tenant", stubOtherTenant,
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("edit: %v; stderr=%s", err, stderr)
	}
	if method != http.MethodPatch {
		t.Errorf("expected PATCH; got %s", method)
	}
	if !strings.Contains(query, "tenant_filter="+stubOtherTenant) {
		t.Errorf("expected tenant_filter in query; got %q", query)
	}
	if raw["connector_id"] != "vmware-rest-9.1" || raw["severity"] != "degraded" {
		t.Errorf("set fields missing from body: %v", raw)
	}
	if params, ok := raw["params"].(map[string]any); !ok || params["folder"] != "<folder-id>" {
		t.Errorf("params missing from body: %v", raw)
	}
	for _, field := range []string{"name", "op_id", "interval_seconds", "cron_expr", "timezone"} {
		if v, ok := raw[field]; ok && v != nil {
			t.Errorf("unset field %q sent with value %v", field, v)
		}
	}
	if _, ok := raw["assertion"]; ok {
		t.Errorf("unset assertion sent: %v", raw["assertion"])
	}
	if !strings.Contains(stdout, "updated sensor") {
		t.Errorf("expected updated-prose in stdout; got %q", stdout)
	}
}

func TestRunEditWithoutFlagsSendsNothing(t *testing.T) {
	called := false
	srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		called = true
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	_, stderr, err := runEditCmd(t, stubSensorID, "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("expected an error for an edit with no field")
	}
	if !strings.Contains(stderr, "at least one field") {
		t.Errorf("expected a clear message; got %q", stderr)
	}
	if called {
		t.Errorf("no request may be sent")
	}
}

func TestRunEditRejectsBadInputLocally(t *testing.T) {
	cases := [][]string{
		{"not-a-uuid", "--name", "x"},
		{stubSensorID, "--severity", "warning"},
		{stubSensorID, "--cadence-kind", "hourly"},
		{stubSensorID, "--interval-seconds", "0"},
		{stubSensorID, "--params", "null"},
		{stubSensorID, "--assertion", "[1]"},
		{stubSensorID, "--name", "x", "--tenant", "nope"},
	}
	for _, args := range cases {
		called := false
		srv := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
			called = true
		}))
		seedXDGAndToken(t, srv.URL)
		_, _, err := runEditCmd(t, append(args, "--backplane", srv.URL)...)
		srv.Close()
		if err == nil {
			t.Errorf("%v: expected an error", args)
		}
		if called {
			t.Errorf("%v: no request may be sent", args)
		}
	}
}

func TestRunEditSurfacesServerErrors(t *testing.T) {
	for _, tc := range []struct {
		status int
		body   string
		want   string
	}{
		{http.StatusConflict, `{"detail":"sensor_name_conflict"}`, "sensor_name_conflict"},
		{http.StatusNotFound, `{"detail":"sensor_not_found"}`, "sensor_not_found"},
		{http.StatusUnprocessableEntity, `{"detail":"sensor_edit_invalid: cron cadence requires cron_expr"}`, "sensor_edit_invalid"},
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		seedXDGAndToken(t, srv.URL)
		_, stderr, err := runEditCmd(t, stubSensorID, "--name", "taken", "--backplane", srv.URL)
		srv.Close()
		if err == nil {
			t.Errorf("HTTP %d: expected an error", tc.status)
		}
		if !strings.Contains(stderr, tc.want) {
			t.Errorf("HTTP %d: expected %q in stderr; got %q", tc.status, tc.want, stderr)
		}
	}
}
