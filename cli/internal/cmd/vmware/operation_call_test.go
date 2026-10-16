// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package vmware

import (
	"bytes"
	"encoding/json"
	"net/http"
	"testing"
)

// TestOperationCallSendsPreviewHash — `vmware operation call` passes
// --preview-hash through, so a destructive op (for example
// vmware.composite.vm.destroy) can wait for approval.
func TestOperationCallSendsPreviewHash(t *testing.T) {
	var callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&callBody); err != nil {
				t.Errorf("decode body: %v", err)
			}
			writeJSON(t, w, 200, map[string]any{
				"status": "awaiting_approval", "op_id": "vmware.composite.vm.destroy",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var out bytes.Buffer
	cmd := newOperationCallCmd()
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"vmware.composite.vm.destroy", "--target", "vc",
		"--params", `{"vm":"vm-101"}`, "--preview-hash", "hash-2", "--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("operation call: %v\n%s", err, out.String())
	}
	if callBody["preview_hash"] != "hash-2" {
		t.Fatalf("preview_hash not sent: %v", callBody["preview_hash"])
	}
	if callBody["connector_id"] != ConnectorID {
		t.Fatalf("connector_id: got %v", callBody["connector_id"])
	}
}
