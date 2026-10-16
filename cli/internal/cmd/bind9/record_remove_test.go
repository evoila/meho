// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package bind9

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"
)

func decodeBody(t *testing.T, r *http.Request) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		t.Errorf("decode body: %v", err)
	}
	return body
}

// TestRecordRemovePreviewsThenSendsTheHash — `bind9 record remove`
// previews the exact call first and sends its preview_hash, so the removal
// waits for approval instead of being denied with preview_binding_required.
func TestRecordRemovePreviewsThenSendsTheHash(t *testing.T) {
	var previewBody, callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			previewBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{
				"status": "ok", "op_id": "bind9.record.remove", "preview_hash": "hash-5",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			callBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{
				"status": "awaiting_approval", "op_id": "bind9.record.remove",
				"extras": map[string]any{"approval_request_id": "req-2"},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var out bytes.Buffer
	cmd := newRecordRemoveCmd()
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"web.example.test", "--zone", "example.test", "--target", "dns",
		"--backplane", srv.URL})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("record remove should exit 0 while it waits for approval; got %v\n%s", err, out.String())
	}
	if callBody["preview_hash"] != "hash-5" {
		t.Fatalf("preview_hash not sent: %v", callBody["preview_hash"])
	}
	for _, b := range []map[string]any{previewBody, callBody} {
		params, _ := b["params"].(map[string]any)
		if b["connector_id"] != ConnectorID || b["op_id"] != "bind9.record.remove" ||
			params["fqdn"] != "web.example.test" || params["zone"] != "example.test" {
			t.Errorf("preview and call must carry the same op and params; got %v", b)
		}
	}
	if !strings.Contains(out.String(), "awaiting_approval") {
		t.Errorf("output should show awaiting_approval:\n%s", out.String())
	}
}

// TestRecordRemoveStopsWhenPreviewFails — a failed preview stops the
// command with the reason, and no call is sent.
func TestRecordRemoveStopsWhenPreviewFails(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"status": "unavailable", "op_id": "bind9.record.remove", "error": "zone is not managed",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			t.Errorf("no call may be sent after a failed preview")
			writeJSON(t, w, 200, map[string]any{"status": "ok"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	var out bytes.Buffer
	cmd := newRecordRemoveCmd()
	cmd.SetOut(&out)
	cmd.SetErr(&out)
	cmd.SetArgs([]string{"web.example.test", "--target", "dns", "--backplane", srv.URL})
	if err := cmd.Execute(); !errors.Is(err, errOpError) {
		t.Fatalf("a failed preview should exit 1; got %v", err)
	}
	if !strings.Contains(out.String(), "zone is not managed") {
		t.Errorf("output should show the preview error:\n%s", out.String())
	}
}
