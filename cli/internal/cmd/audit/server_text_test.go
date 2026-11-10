// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// pathWithControlCodes is a stored request path with control codes
// (ESC [2K, CR) followed by text that looks like another field.
const pathWithControlCodes = "/api/v1/x\x1b[2K\rprincipal_sub:     someone-else"

// assertNoControlCodes fails when out holds a raw ESC or CR, and checks
// that the escape is shown as visible text instead.
func assertNoControlCodes(t *testing.T, out string) {
	t.Helper()
	if strings.ContainsAny(out, "\x1b\r") {
		t.Errorf("output holds a raw control code: %q", out)
	}
	if !strings.Contains(out, `\x1b[2K\x0d`) {
		t.Errorf("output does not show the control codes as visible escapes: %q", out)
	}
}

func TestPrintEntrySummaryEscapesControlCodes(t *testing.T) {
	name := "bob\x1b]0;title\x07"
	var buf bytes.Buffer
	printEntrySummary(&buf, &api.AuditEntry{
		Id:            mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:            mustTS(t, "2026-05-13T00:00:00Z"),
		PrincipalSub:  "reader",
		PrincipalName: &name,
		Method:        "GET",
		Path:          pathWithControlCodes,
		OpId:          "x.y",
		ResultStatus:  "denied",
		Payload:       map[string]any{"note": "a\x1b[2K\rb"},
	})
	out := buf.String()
	assertNoControlCodes(t, out)
	if strings.Contains(out, "\x07") {
		t.Errorf("output holds a raw BEL: %q", out)
	}
	if !strings.Contains(out, "path:              /api/v1/x\\x1b[2K\\x0dprincipal_sub:") {
		t.Errorf("path is not on its own line with visible escapes: %q", out)
	}
	if got := strings.Count(out, "\nprincipal_sub:"); got != 1 {
		t.Errorf("want one principal_sub line, got %d: %q", got, out)
	}
}

func TestPrintQueryTableEscapesControlCodes(t *testing.T) {
	target := "web\x1b[2K\rother"
	var buf bytes.Buffer
	printQueryTable(&buf, &api.AuditQueryResult{Rows: []api.AuditEntry{{
		Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:           mustTS(t, "2026-05-13T00:00:00Z"),
		PrincipalSub: "reader\x1b[2K\rother",
		TargetName:   &target,
		OpId:         "x.y",
		ResultStatus: "ok",
	}}})
	assertNoControlCodes(t, buf.String())
}

func TestFormatReplayNodeEscapesControlCodes(t *testing.T) {
	actor := "agent\x1b[2K\rother"
	got := formatReplayNode(&api.ReplayNode{
		Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:           mustTS(t, "2026-05-13T10:00:00Z"),
		PrincipalSub: "reader",
		Method:       "GET",
		Path:         pathWithControlCodes,
		OpId:         "x.y",
		ResultStatus: "ok",
		ActorSub:     &actor,
	})
	assertNoControlCodes(t, got)
}

func TestRunTraceShowEscapesControlCodes(t *testing.T) {
	body := strings.ReplaceAll(traceWithTwoSpans,
		`"url":"https://vc.example.test/api/vcenter/vm"`,
		`"url":"https://vc.example.test/x\u001b[2K\rother"`)
	srv := serveTrace(t, 200, body)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runTraceShow(cmd, traceShowOptions{AuditID: traceShowAuditID, BackplaneOverride: srv.URL}); err != nil {
		t.Fatalf("runTraceShow: %v; stderr=%s", err, stderr.String())
	}
	assertNoControlCodes(t, stdout.String())
}
