// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// requesterText holds the control characters a requester could put in a
// subject or summary: ESC (a cursor move and a line erase), CR, BEL and a
// bidi override.
const (
	rawSummary = "Power off web-01\x1b[1A\x1b[2K\r  Risk level:   safe\a"
	rawSubject = "web-01\u202e10-bew"
)

func viewWithRequesterText(t *testing.T) api.ApprovalRequestView {
	t.Helper()
	d := newApprovalView(t, "pending", "")
	d.ProposedEffect = map[string]interface{}{"safety_level": "destructive", "name": "a\u202eb"}
	summary, subject := rawSummary, rawSubject
	d.ReviewerContext = &api.ReviewerContextView{Summary: &summary, Subject: &subject}
	return d
}

func assertNoRawControls(t *testing.T, out string) {
	t.Helper()
	for _, raw := range []string{"\x1b", "\r", "\a", "\u202e"} {
		if strings.Contains(out, raw) {
			t.Errorf("output holds the raw character %q:\n%q", raw, out)
		}
	}
}

func TestShowRequestEscapesRequesterText(t *testing.T) {
	d := viewWithRequesterText(t)
	var buf bytes.Buffer
	showRequest(&buf, "Request 1 of 1:", &d)
	out := buf.String()

	assertNoRawControls(t, out)
	for _, want := range []string{
		`  What it does: Power off web-01\x1b[1A\x1b[2K\x0d  Risk level:   safe\x07`,
		`  Subject:      web-01\u202e10-bew`,
		"  Risk level:   destructive\n",
		`"name": "a\u202eb"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("showRequest output misses %q in:\n%s", want, out)
		}
	}
}

func TestPrintDetailEscapesRequesterText(t *testing.T) {
	d := viewWithRequesterText(t)
	cmd, stdout, _ := newCapturingCmd(t)
	printDetail(cmd, &d)
	out := stdout.String()

	assertNoRawControls(t, out)
	for _, want := range []string{
		`Summary:      Power off web-01\x1b[1A\x1b[2K\x0d  Risk level:   safe\x07`,
		`Subject:      web-01\u202e10-bew`,
		`"name": "a\u202eb"`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("printDetail output misses %q in:\n%s", want, out)
		}
	}
}

func TestListTableEscapesServerText(t *testing.T) {
	d := newApprovalView(t, "pending", "")
	d.OpId = "op\x1b[2K"
	name := "alice\r"
	d.PrincipalName = &name
	var buf bytes.Buffer
	printListTable(&buf, []api.ApprovalRequestView{d}, "pending", "", 0)
	out := buf.String()

	assertNoRawControls(t, out)
	if !strings.Contains(out, `op\x1b[2K`) || !strings.Contains(out, `alice\x0d`) {
		t.Errorf("list table misses the escaped text:\n%s", out)
	}
}

func TestWithdrawalEscapesReason(t *testing.T) {
	d := newApprovalView(t, "withdrawn", "")
	reason := "done\x1b]0;title\a"
	d.WithdrawReason = &reason
	var buf bytes.Buffer
	printWithdrawal(&buf, stubID, &d)

	assertNoRawControls(t, buf.String())
	if !strings.Contains(buf.String(), `reason:       done\x1b]0;title\x07`) {
		t.Errorf("withdraw output misses the escaped reason:\n%s", buf.String())
	}
}

func TestHTTPErrorEscapesServerBody(t *testing.T) {
	cmd, _, stderr := newCapturingCmd(t)
	_ = renderHTTPStatus(cmd, "https://meho.example", 409, []byte("conflict\x1b[2K\rfine"), false)

	assertNoRawControls(t, stderr.String())
	if !strings.Contains(stderr.String(), `conflict\x1b[2K\x0dfine`) {
		t.Errorf("error output misses the escaped body:\n%s", stderr.String())
	}
}
