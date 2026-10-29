// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func subopsOK(t *testing.T) *api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse {
	t.Helper()
	write := "write"
	reason := "a delete step can never carry a standing grant"
	r := api.GovernedSubopsResponse{
		OpId:        "vmware.composite.vm.create",
		ConnectorId: "vmware-rest-8.0",
		GovernedSubops: []api.GovernedSubop{
			{OpId: "vmware.vm.create", SafetyLevel: &write, Grantable: true},
			{OpId: "vmware.vm.power_on", Grantable: true},
			{OpId: "vmware.vm.delete", Grantable: false, UngrantableReason: &reason},
		},
	}
	body, err := json.Marshal(r)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return &api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{
		HTTPResponse: makeHTTPResp(200), Body: body, JSON200: &r,
	}
}

func runSubopsCmd(t *testing.T, f *fakeOperationsClient, args ...string) (string, string, error) {
	t.Helper()
	withFakeClient(t, f)
	cmd := newSubopsCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(append(args, "--backplane", "https://x"))
	err := cmd.Execute()
	return out.String(), errBuf.String(), err
}

// TestSubopsListsEachStep — a normal answer prints one row per step with
// the risk level and whether a grant may cover it, and --connector
// lands on the typed connector_id param.
func TestSubopsListsEachStep(t *testing.T) {
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{subopsOK(t)},
	}
	out, errOut, err := runSubopsCmd(t, f, "vmware.composite.vm.create", "--connector", "vmware-rest-8.0")
	if err != nil {
		t.Fatalf("execute: %v; stderr=%s", err, errOut)
	}
	p := f.lastSubopsParams
	if p == nil || p.OpId != "vmware.composite.vm.create" || p.ConnectorId == nil || *p.ConnectorId != "vmware-rest-8.0" {
		t.Fatalf("typed params not set; got %+v", p)
	}
	for _, want := range []string{
		"vmware.composite.vm.create (connector vmware-rest-8.0): 3 child step(s)",
		"OP_ID", "RISK", "GRANT",
		"vmware.vm.create", "write",
		"unknown",
		"no: a delete step can never carry a standing grant",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}

// TestSubopsWithoutConnectorSendsNoConnectorID — without --connector the
// backplane picks the composite's own connector.
func TestSubopsWithoutConnectorSendsNoConnectorID(t *testing.T) {
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{subopsOK(t)},
	}
	if _, errOut, err := runSubopsCmd(t, f, "vmware.composite.vm.create"); err != nil {
		t.Fatalf("execute: %v; stderr=%s", err, errOut)
	}
	if f.lastSubopsParams.ConnectorId != nil {
		t.Fatalf("connector_id should be unset; got %q", *f.lastSubopsParams.ConnectorId)
	}
}

// TestSubopsJSONPrintsAnswerAsItCame — --json writes the server bytes.
func TestSubopsJSONPrintsAnswerAsItCame(t *testing.T) {
	resp := subopsOK(t)
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{resp},
	}
	out, errOut, err := runSubopsCmd(t, f, "vmware.composite.vm.create", "--json")
	if err != nil {
		t.Fatalf("execute: %v; stderr=%s", err, errOut)
	}
	if out != string(resp.Body)+"\n" {
		t.Fatalf("--json should print the body as it came; got %q", out)
	}
}

// TestSubopsNoChildStepsIsAPlainMessage — the route's 404
// no_governed_subops answer prints a plain note and exits 0.
func TestSubopsNoChildStepsIsAPlainMessage(t *testing.T) {
	body := []byte(`{"detail": {"message": "no governed child-op surface registered for op 'vault.kv.read'",` +
		` "reason": "no_governed_subops", "op_id": "vault.kv.read", "next_step": "..."}}`)
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{
			{HTTPResponse: makeHTTPResp(404), Body: body},
		},
	}
	out, errOut, err := runSubopsCmd(t, f, "vault.kv.read")
	if err != nil {
		t.Fatalf("no child steps must not be an error; got %v; stderr=%s", err, errOut)
	}
	if out != "vault.kv.read has no child steps that need their own grant.\n" {
		t.Fatalf("unexpected output %q", out)
	}
	if errOut != "" {
		t.Fatalf("stderr should be empty; got %q", errOut)
	}
}

// TestSubopsOther404IsAnError — a 404 without the no_governed_subops
// reason (for example a backplane without the route) stays an error.
func TestSubopsOther404IsAnError(t *testing.T) {
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{
			{HTTPResponse: makeHTTPResp(404), Body: []byte(`{"detail":"Not Found"}`)},
		},
	}
	_, errOut, _ := runSubopsCmd(t, f, "vault.kv.read")
	if !strings.Contains(errOut, "HTTP 404") {
		t.Fatalf("expected an HTTP 404 error on stderr; got %q", errOut)
	}
}

// TestSubopsRefreshesOn401 — one refresh, then the retried call.
func TestSubopsRefreshesOn401(t *testing.T) {
	f := &fakeOperationsClient{
		subopsResponses: []*api.GetGovernedSubopsApiV1OperationsGovernedSubopsGetResponse{
			{HTTPResponse: makeHTTPResp(401), Body: []byte(`{"detail":"token expired"}`)},
			subopsOK(t),
		},
	}
	if _, errOut, err := runSubopsCmd(t, f, "vmware.composite.vm.create"); err != nil {
		t.Fatalf("execute: %v; stderr=%s", err, errOut)
	}
	if f.refreshCount != 1 {
		t.Fatalf("expected one Refresh; got %d", f.refreshCount)
	}
}
