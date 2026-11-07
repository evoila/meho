// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dashboard

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// runEditCmd runs `meho dashboard edit` through cobra, so the "flag was
// set" detection is the real one.
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

const stubSecondSensor = "55555555-5555-5555-5555-555555555555"

func TestRunEditSendsMembersAndSettings(t *testing.T) {
	var raw map[string]any
	var method string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/checks/dashboards/"+stubDashboardID, func(w http.ResponseWriter, r *http.Request) {
		method = r.Method
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &raw); err != nil {
			t.Fatalf("decode body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fakeDashboardDetail(t))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	stdout, stderr, err := runEditCmd(t, stubDashboardID,
		"--add-sensor", stubSensorID, "--add-sensor", stubSecondSensor,
		"--remove-sensor", stubOtherTenant,
		"--notify-min-state", "degraded",
		"--backplane", srv.URL)
	if err != nil {
		t.Fatalf("edit: %v; stderr=%s", err, stderr)
	}
	if method != http.MethodPatch {
		t.Errorf("expected PATCH; got %s", method)
	}
	added, _ := raw["add_sensor_ids"].([]any)
	if len(added) != 2 || added[0] != stubSensorID || added[1] != stubSecondSensor {
		t.Errorf("add_sensor_ids wrong: %v", raw["add_sensor_ids"])
	}
	removed, _ := raw["remove_sensor_ids"].([]any)
	if len(removed) != 1 || removed[0] != stubOtherTenant {
		t.Errorf("remove_sensor_ids wrong: %v", raw["remove_sensor_ids"])
	}
	if raw["notify_min_state"] != "degraded" {
		t.Errorf("notify_min_state wrong: %v", raw["notify_min_state"])
	}
	if raw["name"] != nil || raw["notify_email"] != nil {
		t.Errorf("unset fields sent with a value: %v", raw)
	}
	if !strings.Contains(stdout, "updated dashboard") {
		t.Errorf("expected updated-prose in stdout; got %q", stdout)
	}
}

func TestRunEditNotifyOffSendsEmptyEmail(t *testing.T) {
	var raw map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(body, &raw)
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fakeDashboardDetail(t))
	}))
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	if _, stderr, err := runEditCmd(t, stubDashboardID, "--notify-off", "--backplane", srv.URL); err != nil {
		t.Fatalf("edit: %v; stderr=%s", err, stderr)
	}
	if v, ok := raw["notify_email"]; !ok || v != "" {
		t.Errorf("expected notify_email \"\"; got %v (present=%v)", v, ok)
	}
}

func TestRunEditRejectsBadInputLocally(t *testing.T) {
	cases := [][]string{
		{"not-a-uuid", "--name", "x"},
		{stubDashboardID},
		{stubDashboardID, "--add-sensor", "nope"},
		{stubDashboardID, "--remove-sensor", "nope"},
		{stubDashboardID, "--notify-min-state", "ok"},
		{stubDashboardID, "--notify-email", "a@example.com", "--notify-off"},
		{stubDashboardID, "--name", "x", "--tenant", "nope"},
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
		{http.StatusUnprocessableEntity, `{"detail":"sensor_not_found"}`, "sensor_not_found"},
		{http.StatusNotFound, `{"detail":"dashboard_not_found"}`, "dashboard_not_found"},
		{http.StatusConflict, `{"detail":"dashboard_name_conflict"}`, "dashboard_name_conflict"},
	} {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(tc.status)
			_, _ = w.Write([]byte(tc.body))
		}))
		seedXDGAndToken(t, srv.URL)
		_, stderr, err := runEditCmd(t, stubDashboardID, "--name", "x", "--backplane", srv.URL)
		srv.Close()
		if err == nil {
			t.Errorf("HTTP %d: expected an error", tc.status)
		}
		if !strings.Contains(stderr, tc.want) {
			t.Errorf("HTTP %d: expected %q in stderr; got %q", tc.status, tc.want, stderr)
		}
	}
}
