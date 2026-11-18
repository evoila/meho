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

// TestGetSearchSendsIncludeRaw — --include-raw maps to include_raw=true,
// and without it the parameter is left out.
func TestGetSearchSendsIncludeRaw(t *testing.T) {
	for _, includeRaw := range []bool{false, true} {
		body, _ := json.Marshal(SearchResponse{Hits: nil})
		f := &fakeOperationsClient{
			searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
				{HTTPResponse: makeHTTPResp(200), Body: body},
			},
		}
		opts := searchOptions{ConnectorID: "vault-1.x", Query: "secret", IncludeRaw: includeRaw}
		if _, err := getSearch(context.Background(), f, opts); err != nil {
			t.Fatalf("getSearch: %v", err)
		}
		p := f.lastSearchParams.IncludeRaw
		if includeRaw && (p == nil || !*p) {
			t.Fatalf("--include-raw should send include_raw=true; got %+v", p)
		}
		if !includeRaw && p != nil {
			t.Fatalf("without --include-raw no include_raw should be sent; got %v", *p)
		}
	}
}

// TestGetGroupsSendsIncludeRaw — the groups verb sends the same flag.
func TestGetGroupsSendsIncludeRaw(t *testing.T) {
	body, _ := json.Marshal(GroupsResponse{ConnectorID: "vault-1.x"})
	f := &fakeOperationsClient{
		groupsResponses: []*api.GetGroupsApiV1OperationsGroupsGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: body},
		},
	}
	if _, err := getGroups(context.Background(), f, "vault-1.x", groupsQuery{includeRaw: true}); err != nil {
		t.Fatalf("getGroups: %v", err)
	}
	p := f.lastGroupsParams.IncludeRaw
	if p == nil || !*p {
		t.Fatalf("--include-raw should send include_raw=true; got %+v", p)
	}
}

// TestRawOperationsNoteIsPrinted — the table shows the note of
// raw_operations, also when no hit and no group came back.
func TestRawOperationsNoteIsPrinted(t *testing.T) {
	var answer SearchResponse
	raw := `{"hits": [], "query_duration_ms": 3,
		"raw_operations": {"included": false, "hidden_matches": 2,
		"note": "2 raw operation(s) also match."}}`
	if err := json.Unmarshal([]byte(raw), &answer); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if answer.RawOperations == nil || answer.RawOperations.HiddenMatches != 2 {
		t.Fatalf("raw_operations not decoded: %+v", answer.RawOperations)
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "vault-1.x", "tag", &answer, false)
	if !strings.Contains(buf.String(), "note: 2 raw operation(s) also match.") {
		t.Errorf("search table should print the note; got:\n%s", buf.String())
	}

	note := "1 group(s) hold only raw operations."
	buf.Reset()
	printGroupsTable(&buf, &GroupsResponse{
		ConnectorID:   "vault-1.x",
		RawOperations: &RawOperations{HiddenGroups: 1, Note: &note},
	})
	if !strings.Contains(buf.String(), "note: "+note) {
		t.Errorf("groups table should print the note; got:\n%s", buf.String())
	}
}

// TestNoRawOperationsNoNote — an answer without raw_operations (the
// switch is off) prints no note line.
func TestNoRawOperationsNoNote(t *testing.T) {
	var buf bytes.Buffer
	printSearchTable(&buf, "vault-1.x", "tag", &SearchResponse{}, false)
	printGroupsTable(&buf, &GroupsResponse{ConnectorID: "vault-1.x"})
	if strings.Contains(buf.String(), "note:") {
		t.Errorf("no note expected; got:\n%s", buf.String())
	}
}
