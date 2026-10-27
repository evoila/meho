// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"

	openapi_types "github.com/oapi-codegen/runtime/types"
)

// postResultQueryWireBody runs the generated client against a test server
// and returns the JSON object the client put on the wire.
func postResultQueryWireBody(t *testing.T, body ResultQueryBody) map[string]json.RawMessage {
	t.Helper()
	var got map[string]json.RawMessage
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, err := io.ReadAll(r.Body)
		if err != nil {
			t.Errorf("read request body: %v", err)
		}
		if err := json.Unmarshal(raw, &got); err != nil {
			t.Errorf("request body is not a JSON object: %v (%q)", err, raw)
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	client, err := NewClient(srv.URL)
	if err != nil {
		t.Fatalf("NewClient: %v", err)
	}
	resp, err := client.PostResultQueryApiV1OperationsResultQueryPost(
		context.Background(), &PostResultQueryApiV1OperationsResultQueryPostParams{}, body)
	if err != nil {
		t.Fatalf("post: %v", err)
	}
	_, _ = io.Copy(io.Discard, resp.Body)
	_ = resp.Body.Close()
	return got
}

// An unset limit must be left out of the request body. A backplane from
// before #4994 refuses `"limit": null` with a 422, so sending the key
// would break every plain row read when the CLI is upgraded first.
func TestResultQueryBodyLeavesUnsetLimitOut(t *testing.T) {
	offset := 0
	got := postResultQueryWireBody(t, ResultQueryBody{
		HandleId: openapi_types.UUID{},
		Offset:   &offset,
	})
	if _, present := got["limit"]; present {
		t.Fatalf("an unset limit must not be sent; body has limit=%s", got["limit"])
	}
	if string(got["offset"]) != "0" {
		t.Fatalf("offset must be sent; got %s", got["offset"])
	}
}

func TestResultQueryBodySendsLimitWhenSet(t *testing.T) {
	offset, limit := 0, 7
	got := postResultQueryWireBody(t, ResultQueryBody{
		HandleId: openapi_types.UUID{},
		Offset:   &offset,
		Limit:    &limit,
	})
	if string(got["limit"]) != "7" {
		t.Fatalf("limit=7 must be sent; got %s", got["limit"])
	}
}
