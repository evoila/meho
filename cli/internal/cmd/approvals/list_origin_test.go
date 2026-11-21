// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package approvals

import (
	"bytes"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

func TestPrintListTableShowsWorkRefAndSession(t *testing.T) {
	item := newApprovalView(t, "pending", "")
	ref := "CHG0012345"
	session := uuid.MustParse("abcdef12-2222-4333-8444-555555555555")
	item.WorkRef = &ref
	item.AgentSessionId = &session

	var buf bytes.Buffer
	printListTable(&buf, []api.ApprovalRequestView{item}, "", "", 0)
	lines := strings.Split(buf.String(), "\n")
	if !strings.Contains(lines[0], "WORK-REF") || !strings.Contains(lines[0], "SESSION") {
		t.Fatalf("header has no WORK-REF and SESSION columns: %q", lines[0])
	}
	fields := strings.Fields(lines[1])
	n := len(fields)
	// The last three fields are WORK-REF, SESSION and CREATED.
	if fields[n-3] != "CHG0012345" || fields[n-2] != "abcdef12" {
		t.Errorf("row = %q; want work ref CHG0012345 and session abcdef12", lines[1])
	}
}

func TestPrintListTableShowsDashWithoutWorkRefAndSession(t *testing.T) {
	item := newApprovalView(t, "pending", "")

	var buf bytes.Buffer
	printListTable(&buf, []api.ApprovalRequestView{item}, "", "", 0)
	fields := strings.Fields(strings.Split(buf.String(), "\n")[1])
	n := len(fields)
	if fields[n-3] != "-" || fields[n-2] != "-" {
		t.Errorf("row fields = %q; want \"-\" for work ref and session", fields)
	}
}
