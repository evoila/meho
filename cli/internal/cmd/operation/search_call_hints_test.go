// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// searchJSON runs getSearch on body and returns the hits as the
// --json output prints them.
func searchJSON(t *testing.T, body string) string {
	t.Helper()
	f := &fakeOperationsClient{
		searchResponses: []*api.GetSearchApiV1OperationsSearchGetResponse{
			{HTTPResponse: makeHTTPResp(200), Body: []byte(body)},
		},
	}
	got, err := getSearch(context.Background(), f, searchOptions{ConnectorID: "vault-1.x", Query: "secret"})
	if err != nil {
		t.Fatalf("getSearch: %v", err)
	}
	out, err := json.Marshal(got.Hits)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(out)
}

// TestSearchJSONKeepsCallHints — with the backend switch on, the
// --json output keeps required_params (also an empty list) and
// target_required (also false).
func TestSearchJSONKeepsCallHints(t *testing.T) {
	out := searchJSON(t, `{"hits":[
		{"op_id":"vault.kv.read","summary":null,"description":null,"group_key":null,
		 "safety_level":"safe","requires_approval":false,
		 "required_params":["path","mount"],"target_required":true},
		{"op_id":"vault.sys.health","summary":null,"description":null,"group_key":null,
		 "safety_level":"safe","requires_approval":false,
		 "required_params":[],"target_required":false}
	],"query_duration_ms":1}`)
	for _, want := range []string{
		`"required_params":["path","mount"],"target_required":true`,
		`"required_params":[],"target_required":false`,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("--json output should keep %s; got %s", want, out)
		}
	}
}

// TestSearchJSONWithoutCallHints — with the backend switch off the
// fields are not sent, and the --json output does not invent them.
func TestSearchJSONWithoutCallHints(t *testing.T) {
	out := searchJSON(t, `{"hits":[
		{"op_id":"vault.kv.read","summary":null,"description":null,"group_key":null,
		 "safety_level":"safe","requires_approval":false}
	],"query_duration_ms":1}`)
	if strings.Contains(out, "required_params") || strings.Contains(out, "target_required") {
		t.Errorf("--json output should not add fields the backend did not send; got %s", out)
	}
}
