// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bytes"
	"strings"
	"testing"

	"github.com/spf13/cobra"
)

// assertNoRawControl fails when out holds a control byte other than the
// newline that ends each line.
func assertNoRawControl(t *testing.T, out string) {
	t.Helper()
	for i, r := range out {
		if r == '\n' {
			continue
		}
		if r < 0x20 || r == 0x7f || (r >= 0x80 && r < 0xa0) {
			t.Fatalf("raw control character %U at byte %d in output:\n%q", r, i, out)
		}
	}
}

// TestRenderCurrentStepEscapesControlCharacters checks that the human view
// of `meho runbook start` and `meho runbook next` shows control characters
// in the template slug, the step title, the body, the op_id and the verify
// prompt as visible escapes, and keeps the line breaks of the body.
func TestRenderCurrentStepEscapesControlCharacters(t *testing.T) {
	prompt := "Is it up?\x1b]0;title\x07"
	opID := "vault.pki.cert\x1b[2J"
	view := &startResponseView{
		RunID:           "6f1c3a52-0000-4000-8000-000000000001",
		TemplateSlug:    "rotate\x1b[31m",
		TemplateVersion: 2,
		Position:        stepPositionDTO{N: 1, Total: 2},
		CurrentStep: stepBodyDTO{
			ID:     "check\r",
			Title:  "Check\x1b[2K",
			Body:   "First line\r\nSecond\tline\x1b[1A\r\n",
			Type:   "operation_call",
			OpID:   &opID,
			Verify: &stepBodyVerifyDTO{Type: "confirm", Prompt: &prompt},
		},
	}
	var buf bytes.Buffer
	renderStartHeader(&buf, view)
	renderCurrentStep(&buf, view)
	out := buf.String()

	assertNoRawControl(t, out)
	for _, want := range []string{
		`Template:    rotate\x1b[31m@2`,
		`Step 1/2: Check\x1b[2K  (id: check\x0d)`,
		"First line\nSecond\\x09line\\x1b[1A\n",
		`op_id: vault.pki.cert\x1b[2J`,
		`Prompt: Is it up?\x1b]0;title\x07`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
}

// TestRenderCurrentStepEscapesVerifyOpID checks the verify op_id line of an
// operation_call verify.
func TestRenderCurrentStepEscapesVerifyOpID(t *testing.T) {
	opID := "k8s.pod.get\x1b[2J\r"
	view := &startResponseView{
		RunID:       "6f1c3a52-0000-4000-8000-000000000002",
		Position:    stepPositionDTO{N: 1, Total: 1},
		CurrentStep: stepBodyDTO{ID: "a", Title: "A", Body: "b", Type: "manual\x07", Verify: &stepBodyVerifyDTO{Type: "operation_call", OpID: &opID}},
	}
	var buf bytes.Buffer
	renderCurrentStep(&buf, view)
	out := buf.String()

	assertNoRawControl(t, out)
	for _, want := range []string{`Step kind:   manual\x07`, `Will dispatch op_id: k8s.pod.get\x1b[2J\x0d`} {
		if !strings.Contains(out, want) {
			t.Errorf("output lacks %q:\n%s", want, out)
		}
	}
}

// TestRenderNextResponseEscapesCompletedRun checks the "Run complete" lines.
func TestRenderNextResponseEscapesCompletedRun(t *testing.T) {
	body := []byte(`{"kind":"completed","run_id":"r1","state":"completed\u001b[2K","completed_at":"2026-11-18\r"}`)
	cmd := &cobra.Command{}
	var out, errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	if err := renderNextResponse(cmd, "http://backplane.test", body, nextRunOptions{}); err != nil {
		t.Fatalf("render: %v (stderr %q)", err, errOut.String())
	}
	got := out.String()
	assertNoRawControl(t, got)
	for _, want := range []string{`state=completed\x1b[2K`, `Completed at: 2026-11-18\x0d`} {
		if !strings.Contains(got, want) {
			t.Errorf("output lacks %q:\n%s", want, got)
		}
	}
}
