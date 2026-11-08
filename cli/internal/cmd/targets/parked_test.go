// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package targets

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

const parkedReply = `{"status":"awaiting_approval","op_id":"%s",` +
	`"approval_request_id":"00000000-0000-0000-0000-0000000000b2","message":"waits"}`

func parkedFor(opID string) string {
	return fmt.Sprintf(parkedReply, opID)
}

func assertParkedOutput(t *testing.T, stdout *bytes.Buffer, opID string) {
	t.Helper()
	out := stdout.String()
	for _, want := range []string{
		opID + " — status=awaiting_approval",
		"approval id: 00000000-0000-0000-0000-0000000000b2",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout misses %q:\n%s", want, out)
		}
	}
}

// TestAddWaitingForApprovalExitsZero: a 202 is a parked write, not a
// created target and not an error.
func TestAddWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusAccepted, parkedFor("targets.register"))
	stdout, stderr, err := runAddCmd(t, url, "new-system", "--product", "ssh", "--host", "new-system.example")
	if err != nil {
		t.Fatalf("add: %v\nstderr=%s", err, stderr.String())
	}
	assertParkedOutput(t, stdout, "targets.register")
}

func TestUpdateWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusAccepted, parkedFor("targets.update"))
	stdout, stderr, err := runUpdateCmd(t, url, "a-system", "--host", "other.example")
	if err != nil {
		t.Fatalf("update: %v\nstderr=%s", err, stderr.String())
	}
	assertParkedOutput(t, stdout, "targets.update")
}

func TestDeleteWaitingForApprovalExitsZero(t *testing.T) {
	// No t.Parallel(): seedXDGAndToken calls t.Setenv.
	var f fakeDeleteServer
	url := f.start(t, http.StatusAccepted, parkedFor("targets.remove"))
	stdout, stderr, err := runDeleteCmd(t, url, "", "a-system", "--confirm")
	if err != nil {
		t.Fatalf("delete: %v\nstderr=%s", err, stderr.String())
	}
	assertParkedOutput(t, stdout, "targets.remove")
	if strings.Contains(stdout.String(), "deleted target") {
		t.Errorf("a waiting delete must not say it deleted:\n%s", stdout.String())
	}
}

// TestImportRecordsWritesThatWait: each write that waits keeps its
// approval id, and the summary does not count it as done.
func TestImportRecordsWritesThatWait(t *testing.T) {
	t.Parallel()
	doer := func(_ context.Context, method, _ string, _ []byte) ([]byte, error) {
		if method == http.MethodPost {
			return []byte(parkedFor("targets.register")), nil
		}
		return []byte(`{"name":"b"}`), nil
	}
	p := &plan{
		Create: []planEntry{{Name: "a", Action: actionCreate, Body: map[string]any{"name": "a"}}},
		Update: []planEntry{{Name: "b", Action: actionUpdate, Body: map[string]any{"notes": "x"}}},
	}
	if err := executePlan(context.Background(), doer, p); err != nil {
		t.Fatalf("executePlan: %v", err)
	}
	if p.Create[0].ApprovalRequestID != "00000000-0000-0000-0000-0000000000b2" {
		t.Errorf("create entry should keep the approval id: %+v", p.Create[0])
	}
	if p.Update[0].ApprovalRequestID != "" {
		t.Errorf("a write that ran must have no approval id: %+v", p.Update[0])
	}
	cmd := &cobra.Command{}
	var stdout bytes.Buffer
	cmd.SetOut(&stdout)
	if err := renderApplyResult(cmd, p, false); err != nil {
		t.Fatalf("renderApplyResult: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"Applied: 0 created, 1 updated, 1 waiting for approval.",
		`CREATE "a": approval id 00000000-0000-0000-0000-0000000000b2`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("stdout misses %q:\n%s", want, out)
		}
	}
}
