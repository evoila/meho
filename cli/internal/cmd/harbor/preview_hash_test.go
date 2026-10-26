// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package harbor

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

func runHarborCmd(cmd *cobra.Command, args ...string) (string, error) {
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return out.String() + errBuf.String(), err
}

func decodeBody(t *testing.T, r *http.Request) map[string]any {
	t.Helper()
	var body map[string]any
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		t.Errorf("decode body: %v", err)
	}
	return body
}

// TestRobotDeletePreviewsThenSendsTheHash — `harbor robot delete` previews
// the exact call first and sends its preview_hash, so the delete waits for
// approval instead of being denied with preview_binding_required.
func TestRobotDeletePreviewsThenSendsTheHash(t *testing.T) {
	var previewBody, callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, r *http.Request) {
			previewBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{
				"status": "ok", "op_id": "harbor.robot.delete", "preview_hash": "hash-9",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			callBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{
				"status": "awaiting_approval", "op_id": "harbor.robot.delete",
				"extras": map[string]any{"approval_request_id": "req-1"},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, err := runHarborCmd(newRobotDeleteCmd(),
		"--project", "myproject", "--id", "42", "--target", "harbor", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("robot delete should exit 0 while it waits for approval; got %v\n%s", err, out)
	}
	if callBody["preview_hash"] != "hash-9" {
		t.Fatalf("preview_hash not sent: %v", callBody["preview_hash"])
	}
	for _, b := range []map[string]any{previewBody, callBody} {
		params, _ := b["params"].(map[string]any)
		if b["connector_id"] != ConnectorID || b["op_id"] != "harbor.robot.delete" ||
			params["project"] != "myproject" || params["id"] != float64(42) {
			t.Errorf("preview and call must carry the same op and params; got %v", b)
		}
	}
	if !strings.Contains(out, "awaiting_approval") {
		t.Errorf("output should show awaiting_approval:\n%s", out)
	}
}

// TestRobotDeleteStopsWhenPreviewFails — a failed preview stops the
// command with the reason, and no call is sent.
func TestRobotDeleteStopsWhenPreviewFails(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/preview": func(w http.ResponseWriter, _ *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"status": "error", "op_id": "harbor.robot.delete", "error": "robot 42 not found",
			})
		},
		"POST /api/v1/operations/call": func(w http.ResponseWriter, _ *http.Request) {
			t.Errorf("no call may be sent after a failed preview")
			writeJSON(t, w, 200, map[string]any{"status": "ok"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, err := runHarborCmd(newRobotDeleteCmd(),
		"--project", "myproject", "--id", "42", "--target", "harbor", "--backplane", srv.URL)
	if !errors.Is(err, errOpError) {
		t.Fatalf("a failed preview should exit 1; got %v", err)
	}
	if !strings.Contains(out, "robot 42 not found") || !strings.Contains(out, "was not sent") {
		t.Errorf("output should show the preview error:\n%s", out)
	}
}

// TestOperationCallSendsPreviewHash — `harbor operation call` passes
// --preview-hash through, so a destructive op can wait for approval.
func TestOperationCallSendsPreviewHash(t *testing.T) {
	var callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			callBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{"status": "awaiting_approval", "op_id": "harbor.robot.delete"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	out, err := runHarborCmd(newOperationCallCmd(), "harbor.robot.delete",
		"--target", "harbor", "--params", `{"project":"p","id":1}`,
		"--preview-hash", "hash-3", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("operation call: %v\n%s", err, out)
	}
	if callBody["preview_hash"] != "hash-3" {
		t.Fatalf("preview_hash not sent: %v", callBody["preview_hash"])
	}
}

// TestOperationCallWithoutPreviewHashOmitsIt — without the flag the wire
// shape stays as before.
func TestOperationCallWithoutPreviewHashOmitsIt(t *testing.T) {
	var callBody map[string]any
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			callBody = decodeBody(t, r)
			writeJSON(t, w, 200, map[string]any{"status": "ok", "op_id": "harbor.about"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)

	if _, err := runHarborCmd(newOperationCallCmd(), "harbor.about",
		"--target", "harbor", "--backplane", srv.URL); err != nil {
		t.Fatalf("operation call: %v", err)
	}
	if v, ok := callBody["preview_hash"]; ok && v != nil {
		t.Fatalf("no preview_hash expected; got %v", v)
	}
}
