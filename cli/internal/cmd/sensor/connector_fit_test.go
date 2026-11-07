// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package sensor

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func strPtr(s string) *string { return &s }

const fitWarningText = "The target runs vsphere 8.0.3. Connector vmware-rest-9.0 works only " +
	"with versions >=9.0,<10.0. Connector vmware-rest-8.0 fits this target."

func TestPrintListTableShowsConnectorTargetAndWarning(t *testing.T) {
	bad := fakeSensor(t, "interval")
	bad.ConnectorFit = &api.SensorConnectorFit{
		TargetProduct:       strPtr("vsphere"),
		TargetVersion:       strPtr("8.0.3"),
		Warning:             strPtr(fitWarningText),
		MatchingConnectorId: strPtr("vmware-rest-8.0"),
	}
	good := fakeSensor(t, "interval")
	good.Name = "no-target"

	var out bytes.Buffer
	printListTable(&out, &api.SensorListResponse{Items: []api.SensorRead{bad, good}})
	text := out.String()
	for _, want := range []string{"CONNECTOR", "TARGET", "vmware-rest-9.0", "vsphere 8.0.3"} {
		if !strings.Contains(text, want) {
			t.Errorf("expected %q in table; got %q", want, text)
		}
	}
	if got := strings.Count(text, "warning: sensor"); got != 1 {
		t.Errorf("expected one warning line; got %d in %q", got, text)
	}
	if !strings.Contains(text, "warning: sensor disk-space: "+fitWarningText) {
		t.Errorf("expected the warning for disk-space; got %q", text)
	}
}

func TestTargetLabelWithoutFit(t *testing.T) {
	if got := targetLabel(nil); got != "-" {
		t.Errorf("targetLabel(nil) = %q; want -", got)
	}
	if got := targetLabel(&api.SensorConnectorFit{TargetProduct: strPtr("vsphere")}); got != "vsphere" {
		t.Errorf("targetLabel(product only) = %q; want vsphere", got)
	}
}

func TestPrintSensorSummaryShowsWarning(t *testing.T) {
	s := fakeSensor(t, "interval")
	s.ConnectorFit = &api.SensorConnectorFit{
		TargetProduct: strPtr("vsphere"),
		TargetVersion: strPtr("8.0.3"),
		Warning:       strPtr(fitWarningText),
	}
	var out bytes.Buffer
	printSensorSummary(&out, &s)
	if !strings.Contains(out.String(), "vsphere 8.0.3") || !strings.Contains(out.String(), fitWarningText) {
		t.Errorf("expected target version and warning; got %q", out.String())
	}
}

func TestRunCreateMismatchShowsMessageAndForceIsSent(t *testing.T) {
	var forces []any
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/sensors", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		forces = append(forces, body["force"])
		w.Header().Set("Content-Type", "application/json")
		if body["force"] != true {
			w.WriteHeader(http.StatusUnprocessableEntity)
			_ = json.NewEncoder(w).Encode(map[string]any{"detail": []map[string]any{{
				"loc":  []string{"body", "connector_id"},
				"msg":  fitWarningText + " Set force to create the Sensor anyway.",
				"type": "sensor_connector_mismatch",
			}}})
			return
		}
		created := fakeSensor(t, "interval")
		created.ConnectorFit = &api.SensorConnectorFit{Warning: strPtr(fitWarningText)}
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(created)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	opts := createOptions{
		Name: "disk-space", ConnectorID: "vmware-rest-9.0", OpID: "vmware.vm.list",
		AssertionArg: stubAssertion, CadenceKind: "interval", IntervalSeconds: 60,
		TargetArg: `{"name":"<target-name>"}`, BackplaneOverride: srv.URL,
	}
	cmd, _, stderr := newRunCmd(t)
	if err := runCreate(cmd, opts); err == nil {
		t.Fatalf("expected the mismatch to fail the create")
	}
	if !strings.Contains(stderr.String(), "invalid request: "+fitWarningText) {
		t.Errorf("expected the plain warning; got %q", stderr.String())
	}

	opts.Force = true
	cmd, stdout, stderr := newRunCmd(t)
	if err := runCreate(cmd, opts); err != nil {
		t.Fatalf("runCreate --force: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), fitWarningText) {
		t.Errorf("expected the warning in the summary; got %q", stdout.String())
	}
	if len(forces) != 2 || forces[0] != nil || forces[1] != true {
		t.Errorf("expected force omitted then true; got %v", forces)
	}
}
