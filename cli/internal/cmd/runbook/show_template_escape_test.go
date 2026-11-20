// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bytes"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
)

// TestPrintTemplateBlockEscapesVerifyOpID checks that the human view
// shows control characters in a verify op_id as visible escapes, for a
// manual step and for an operation_call step.
func TestPrintTemplateBlockEscapesVerifyOpID(t *testing.T) {
	const rawOpID = "vault.pki.cert\x1b[2J\r"
	const shownOpID = `vault.pki.cert\x1b[2J\x0d`

	var manualVerify api.ManualStep_Verify
	if err := manualVerify.FromOperationCallVerify(api.OperationCallVerify{
		Type: "operation_call", OpId: rawOpID + "m",
	}); err != nil {
		t.Fatalf("manual verify: %v", err)
	}
	var manual api.ShowTemplateResponse_Steps_Item
	if err := manual.FromManualStep(api.ManualStep{
		Id: "check", Title: "Check", Body: "Look.", Type: "manual", Verify: manualVerify,
	}); err != nil {
		t.Fatalf("manual step: %v", err)
	}

	var opVerify api.OperationCallStep_Verify
	if err := opVerify.FromOperationCallVerify(api.OperationCallVerify{
		Type: "operation_call", OpId: rawOpID + "o",
	}); err != nil {
		t.Fatalf("op verify: %v", err)
	}
	var opStep api.ShowTemplateResponse_Steps_Item
	if err := opStep.FromOperationCallStep(api.OperationCallStep{
		Id: "issue", Title: "Issue", Body: "Run.", Type: "operation_call",
		OpId: "vault.pki.issue", Verify: opVerify,
	}); err != nil {
		t.Fatalf("op step: %v", err)
	}

	r := &api.ShowTemplateResponse{
		Slug: "s\x1b[2Jlug", Version: 1, Title: "T", Status: api.ShowTemplateResponseStatusPublished,
		Steps:     []api.ShowTemplateResponse_Steps_Item{manual, opStep},
		CreatedBy: "alice\x1b[1A", CreatedAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
		EditedBy: "bob\r", EditedAt: time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC),
	}

	var buf bytes.Buffer
	if err := printTemplateBlock(&buf, r); err != nil {
		t.Fatalf("printTemplateBlock: %v", err)
	}
	out := buf.String()
	for _, bad := range []string{"\x1b", "\r"} {
		if strings.Contains(out, bad) {
			t.Errorf("output holds raw %q:\n%s", bad, out)
		}
	}
	for _, want := range []string{
		"verify: operation_call op_id=" + shownOpID + "m",
		"verify: operation_call op_id=" + shownOpID + "o",
		`Template: s\x1b[2Jlug@1`,
		`Created by:  alice\x1b[1A`,
		`Edited by:   bob\x0d`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected output to contain %q; got:\n%s", want, out)
		}
	}
}
