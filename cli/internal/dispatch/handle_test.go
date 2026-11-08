// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

const reducedHandleID = "5b2c8f4e-1d3a-4c6b-9e7f-0a1b2c3d4e5f"

// reducedReply is a reply the backplane sends for a large set-shaped
// result: a short summary in result, and the full result behind the
// top-level handle (the shape of the backend OperationResult model).
func reducedReply(delivery string) string {
	return `{"status":"ok","op_id":"k8s.logs",` +
		`"result":{"row_count":3,"total":3,"sample_rows_returned":1,"sample_bytes":2820,"source_key":"lines"},` +
		`"error":null,"duration_ms":42,` +
		`"handle":{"handle_id":"` + reducedHandleID + `","summary_md":"3 rows","schema_":{},` +
		`"total_rows":3,"sample_rows":[{"line":"first"}],"ttl_seconds":900,"fetch_more":null},` +
		`"audit_id":"11111111-2222-3333-4444-555555555555",` +
		`"delivery":"` + delivery + `","extras":{}}`
}

// callReduced runs Call against a fake backplane that answers body, so
// the test goes through the same decode a vendor verb uses.
func callReduced(t *testing.T, body string) *CallResult {
	t.Helper()
	withFake(t, &fakeOperationsClient{
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: []byte(body)},
		},
	})
	r, err := New("k8s-1.x").Call(context.Background(), "https://bp", "k8s.logs", "test-target", nil)
	if err != nil {
		t.Fatalf("Call: %v", err)
	}
	return r
}

func renderTo(t *testing.T, r *CallResult, jsonOut bool, printer func(io.Writer, *CallResult)) (string, error) {
	t.Helper()
	cmd := &cobra.Command{}
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	err := New("k8s-1.x").Render(cmd, "k8s.logs", r, jsonOut, printer)
	return out.String(), err
}

// TestRenderReducedReplyShowsHandle — a reduced result shows the handle
// id and the result-query command in the human render, with the generic
// render and with a verb's own printer (#4659).
func TestRenderReducedReplyShowsHandle(t *testing.T) {
	lineCount := func(w io.Writer, _ *CallResult) {
		fmt.Fprintln(w, "3 lines")
	}
	for name, printer := range map[string]func(io.Writer, *CallResult){
		"generic":        nil,
		"custom printer": lineCount,
	} {
		t.Run(name, func(t *testing.T) {
			r := callReduced(t, reducedReply("complete"))
			out, err := renderTo(t, r, false, printer)
			if err != nil {
				t.Fatalf("Render: %v", err)
			}
			for _, want := range []string{
				"result handle: " + reducedHandleID,
				"meho operation result-query " + reducedHandleID,
			} {
				if !strings.Contains(out, want) {
					t.Errorf("human render misses %q:\n%s", want, out)
				}
			}
			if strings.Contains(out, "delivery incomplete") {
				t.Errorf("complete delivery must not warn:\n%s", out)
			}
		})
	}
}

// TestRenderReducedReplyJSONKeepsEveryKey — --json prints the full
// reply: handle (with its handle_id), audit_id and delivery stay.
func TestRenderReducedReplyJSONKeepsEveryKey(t *testing.T) {
	r := callReduced(t, reducedReply("complete"))
	out, err := renderTo(t, r, true, nil)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("decode --json output: %v; output=%q", err, out)
	}
	var want map[string]any
	if err := json.Unmarshal([]byte(reducedReply("complete")), &want); err != nil {
		t.Fatalf("decode fixture: %v", err)
	}
	for key := range want {
		if _, ok := got[key]; !ok {
			t.Errorf("--json dropped the %q key: %s", key, out)
		}
	}
	handle, _ := got["handle"].(map[string]any)
	if handle["handle_id"] != reducedHandleID {
		t.Errorf("handle_id: got %#v want %q", handle["handle_id"], reducedHandleID)
	}
	if got["delivery"] != "complete" {
		t.Errorf("delivery: got %#v", got["delivery"])
	}
}

// TestRenderPartialDeliveryWarnsNotToRerun — when the backplane could
// not hand over the whole result, the human render says the operation
// already ran, and still names the handle.
func TestRenderPartialDeliveryWarnsNotToRerun(t *testing.T) {
	for _, delivery := range []string{"partial", "unavailable"} {
		t.Run(delivery, func(t *testing.T) {
			r := callReduced(t, reducedReply(delivery))
			out, err := renderTo(t, r, false, nil)
			if err != nil {
				t.Fatalf("Render: %v", err)
			}
			for _, want := range []string{
				"delivery: " + delivery,
				"do not re-invoke it",
				"result handle: " + reducedHandleID,
			} {
				if !strings.Contains(out, want) {
					t.Errorf("human render misses %q:\n%s", want, out)
				}
			}
		})
	}
}

// TestRenderSmallResultPrintsNoHandleLine — a small result has a null
// handle; the human render adds no handle line and --json keeps the
// null handle as the backplane sent it.
func TestRenderSmallResultPrintsNoHandleLine(t *testing.T) {
	body := `{"status":"ok","op_id":"k8s.logs","result":{"lines":["a"]},` +
		`"error":null,"duration_ms":1,"handle":null,"audit_id":null,"delivery":null,"extras":{}}`
	r := callReduced(t, body)
	out, err := renderTo(t, r, false, nil)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if strings.Contains(out, "result handle") || strings.Contains(out, "delivery") {
		t.Errorf("small result must print no handle or delivery line:\n%s", out)
	}
	jsonOut, err := renderTo(t, r, true, nil)
	if err != nil {
		t.Fatalf("Render --json: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(jsonOut), &got); err != nil {
		t.Fatalf("decode --json output: %v", err)
	}
	for _, key := range []string{"handle", "audit_id", "delivery"} {
		v, ok := got[key]
		if !ok || v != nil {
			t.Errorf("--json %q: got %#v (present=%v), want null", key, v, ok)
		}
	}
}

// TestRenderFailedCallPrintsNoHandleLine — a failed call keeps its
// error trailer and gets no handle line, even if a handle came along.
func TestRenderFailedCallPrintsNoHandleLine(t *testing.T) {
	msg := "connection refused"
	r := &CallResult{
		Status: "error", OpID: "k8s.logs", Error: &msg,
		Handle: json.RawMessage(`{"handle_id":"` + reducedHandleID + `"}`),
	}
	out, err := renderTo(t, r, false, nil)
	if !errors.Is(err, ErrOpError) {
		t.Fatalf("status=error must exit 1: %v", err)
	}
	if strings.Contains(out, "result handle") {
		t.Errorf("failed call must print no handle line:\n%s", out)
	}
	if !strings.Contains(out, msg) {
		t.Errorf("error trailer missing:\n%s", out)
	}
}

// TestResultHandleIDEdgeInputs — a missing, null, odd or id-less handle
// yields "" and never panics.
func TestResultHandleIDEdgeInputs(t *testing.T) {
	for name, tc := range map[string]struct {
		raw  json.RawMessage
		want string
	}{
		"nil":          {nil, ""},
		"null":         {json.RawMessage(`null`), ""},
		"not object":   {json.RawMessage(`["x"]`), ""},
		"broken json":  {json.RawMessage(`{"handle_id":`), ""},
		"no handle_id": {json.RawMessage(`{"summary_md":"x"}`), ""},
		"wrong type":   {json.RawMessage(`{"handle_id":7}`), ""},
		"present":      {json.RawMessage(`{"handle_id":"` + reducedHandleID + `"}`), reducedHandleID},
	} {
		t.Run(name, func(t *testing.T) {
			if got := ResultHandleID(tc.raw); got != tc.want {
				t.Errorf("got %q want %q", got, tc.want)
			}
		})
	}
}

// TestWriteResultHandleWritesNothingWithoutHandle — no handle and a
// complete or unset delivery write no line at all.
func TestWriteResultHandleWritesNothingWithoutHandle(t *testing.T) {
	complete := "complete"
	for name, d := range map[string]*string{"nil": nil, "complete": &complete} {
		t.Run(name, func(t *testing.T) {
			var b bytes.Buffer
			WriteResultHandle(&b, nil, d)
			if b.Len() != 0 {
				t.Errorf("want no output, got %q", b.String())
			}
		})
	}
}
