// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package topology

import (
	"bytes"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const parkedReply = `{"status":"awaiting_approval","op_id":"%s",` +
	`"approval_request_id":"00000000-0000-0000-0000-0000000000c3","message":"waits"}`

// parkedServer answers every request with 202 and a parked body.
func parkedServer(t *testing.T, opID string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusAccepted)
		_, _ = fmt.Fprintf(w, parkedReply, opID)
	}))
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv.URL
}

func assertParked(t *testing.T, stdout *bytes.Buffer, opID string) {
	t.Helper()
	for _, want := range []string{
		opID + " — status=awaiting_approval",
		"approval id: 00000000-0000-0000-0000-0000000000c3",
	} {
		if !strings.Contains(stdout.String(), want) {
			t.Errorf("stdout misses %q:\n%s", want, stdout.String())
		}
	}
}

func TestAnnotateWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	url := parkedServer(t, "topology.annotate")
	cmd, stdout, stderr := newRunCmd(t)
	if err := runAnnotate(cmd, annotateOptions{
		From: "service-x", Kind: "depends-on", To: "database-y", BackplaneOverride: url,
	}); err != nil {
		t.Fatalf("runAnnotate: %v; stderr=%s", err, stderr.String())
	}
	assertParked(t, stdout, "topology.annotate")
}

func TestUnannotateWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	url := parkedServer(t, "topology.unannotate")
	cmd, stdout, stderr := newRunCmd(t)
	if err := runUnannotate(cmd, unannotateOptions{
		EdgeID: "11111111-2222-3333-4444-555555555555", BackplaneOverride: url,
	}); err != nil {
		t.Fatalf("runUnannotate: %v; stderr=%s", err, stderr.String())
	}
	assertParked(t, stdout, "topology.unannotate")
	if strings.Contains(stdout.String(), "deleted edge") {
		t.Errorf("a waiting delete must not say it deleted:\n%s", stdout.String())
	}
}

func TestBulkImportWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	file := filepath.Join(t.TempDir(), "edges.yaml")
	if err := os.WriteFile(file, []byte("edges:\n  - from: a\n    kind: depends-on\n    to: b\n"), 0o600); err != nil {
		t.Fatalf("write file: %v", err)
	}
	url := parkedServer(t, "topology.bulk_import")
	cmd, stdout, stderr := newRunCmd(t)
	if err := runBulkImport(cmd, bulkImportOptions{File: file, BackplaneOverride: url}); err != nil {
		t.Fatalf("runBulkImport: %v; stderr=%s", err, stderr.String())
	}
	assertParked(t, stdout, "topology.bulk_import")
}
