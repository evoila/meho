// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package connector

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

const testAuditID = "0f0e0d0c-0b0a-0908-0706-050403020100"

// TestEditGroupAndEnablePrintAuditReceipt — #4976: edit-group and
// enable answer 204 No Content, so the audit id comes back in the
// Meho-Audit-Id header. The human output prints `audit receipt: <id>`
// and the --json output carries `audit_id`.
func TestEditGroupAndEnablePrintAuditReceipt(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"PATCH /api/v1/connectors/vmware-rest-9.0/groups/tagging": func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Meho-Audit-Id", testAuditID)
			w.WriteHeader(http.StatusNoContent)
		},
		"POST /api/v1/connectors/vmware-rest-9.0/enable": func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Meho-Audit-Id", testAuditID)
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	run := func(args ...string) string {
		t.Helper()
		var cmdArgs []string
		cmdArgs = append(cmdArgs, args...)
		cmdArgs = append(cmdArgs, "--backplane", srv.URL)
		cmd := NewRootCmd()
		var out, errOut bytes.Buffer
		cmd.SetOut(&out)
		cmd.SetErr(&errOut)
		cmd.SetArgs(cmdArgs)
		if err := cmd.ExecuteContext(context.Background()); err != nil {
			t.Fatalf("%v: %v; stderr=%s", args, err, errOut.String())
		}
		return out.String()
	}

	for _, args := range [][]string{
		{"edit-group", "vmware-rest-9.0", "tagging", "--when-to-use", "Use for tags."},
		{"enable", "vmware-rest-9.0", "--confirm"},
	} {
		human := run(args...)
		if !strings.HasSuffix(human, "audit receipt: "+testAuditID+"\n") {
			t.Errorf("%v: human output does not end with the audit receipt:\n%s", args, human)
		}
		var got map[string]any
		jsonOut := run(append(args, "--json")...)
		if err := json.Unmarshal([]byte(jsonOut), &got); err != nil {
			t.Fatalf("%v --json: not JSON: %v\n%s", args, err, jsonOut)
		}
		if got["audit_id"] != testAuditID {
			t.Errorf("%v --json: audit_id = %v, want %s", args, got["audit_id"], testAuditID)
		}
	}
}

// TestTransitionJSONAuditIDNullWithoutHeader — an older backplane sends
// no Meho-Audit-Id header: the human output has no receipt line and the
// --json audit_id is null.
func TestTransitionJSONAuditIDNullWithoutHeader(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/connectors/vmware-rest-9.0/disable": func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusNoContent)
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	cmd := NewRootCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"disable", "vmware-rest-9.0", "--confirm", "--json", "--backplane", srv.URL})
	if err := cmd.ExecuteContext(context.Background()); err != nil {
		t.Fatalf("disable: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("not JSON: %v\n%s", err, out.String())
	}
	if v, ok := got["audit_id"]; !ok || v != nil {
		t.Errorf("audit_id should be present and null; got %+v", got)
	}
}
