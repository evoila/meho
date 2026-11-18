// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestGetGroupsSendsGroupAndCursor — --group and --cursor reach the query.
func TestGetGroupsSendsGroupAndCursor(t *testing.T) {
	body, _ := json.Marshal(GroupsResponse{ConnectorID: "vault-1.x"})
	f := &fakeOperationsClient{
		groupsResponses: []*api.GetGroupsApiV1OperationsGroupsGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	query := groupsQuery{group: "kv", cursor: "auth"}
	if _, err := getGroups(context.Background(), f, "vault-1.x", query); err != nil {
		t.Fatalf("getGroups: %v", err)
	}
	p := f.lastGroupsParams
	if p.Group == nil || *p.Group != "kv" || p.Cursor == nil || *p.Cursor != "auth" {
		t.Fatalf("group and cursor not sent: %+v", p)
	}
	if _, err := getGroups(context.Background(), f, "vault-1.x", groupsQuery{}); err != nil {
		t.Fatalf("getGroups: %v", err)
	}
	if f.lastGroupsParams.Group != nil || f.lastGroupsParams.Cursor != nil {
		t.Fatalf("without the flags nothing should be sent: %+v", f.lastGroupsParams)
	}
}

// TestGroupOperationsArePrinted — the table shows each operation line, the
// count left out, the next page and the note.
func TestGroupOperationsArePrinted(t *testing.T) {
	raw := `{"connector_id": "vault-1.x", "next_cursor": "kv", "note": "no group",
		"groups": [{"group_key": "kv", "name": "KV", "when_to_use": "Secrets.",
		"operation_count": 3, "operations_more": 1, "operations": [
		{"op_id": "vault.kv.read", "summary": "Read a secret.", "safety_level": "safe", "requires_approval": false},
		{"op_id": "vault.kv.delete", "summary": null, "safety_level": "destructive", "requires_approval": true}]}]}`
	var answer GroupsResponse
	if err := json.Unmarshal([]byte(raw), &answer); err != nil {
		t.Fatalf("decode: %v", err)
	}
	var buf bytes.Buffer
	printGroupsTable(&buf, &answer)
	out := buf.String()
	for _, want := range []string{
		"vault.kv.read (safe) Read a secret.",
		"vault.kv.delete (destructive, needs approval)",
		"… 1 more: use meho operation search with --group kv",
		"more groups: run again with --cursor kv",
		"note: no group",
	} {
		if !strings.Contains(out, want) {
			t.Fatalf("output misses %q:\n%s", want, out)
		}
	}
}

// TestGroupsWithoutOperationsPrintAsBefore — an answer without operation
// lines (the switch off) prints no extra line.
func TestGroupsWithoutOperationsPrintAsBefore(t *testing.T) {
	answer := GroupsResponse{
		ConnectorID: "vault-1.x",
		Groups:      []GroupSummary{{GroupKey: "kv", Name: "KV", WhenToUse: "Secrets.", OperationCount: 3}},
	}
	var buf bytes.Buffer
	printGroupsTable(&buf, &answer)
	lines := strings.Split(strings.TrimSpace(buf.String()), "\n")
	if len(lines) != 3 {
		t.Fatalf("want header, column line and one group row; got:\n%s", buf.String())
	}
}
