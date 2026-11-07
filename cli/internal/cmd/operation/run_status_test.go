// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

const testRunID = "6f1f3a52-1c0e-4c53-9d0b-0a3b2f9d7e11"

func runBody(t *testing.T, status string, result map[string]any, errText *string) []byte {
	t.Helper()
	m := map[string]any{
		"run_id":              testRunID,
		"status":              status,
		"origin":              "call",
		"connector_id":        "vault-1.x",
		"op_id":               "vault.kv.read",
		"target_name":         "t1",
		"approval_request_id": nil,
		"result":              result,
		"error":               errText,
		"created_at":          "2026-11-07T10:00:00Z",
		"started_at":          "2026-11-07T10:00:01Z",
		"ended_at":            nil,
	}
	raw, err := json.Marshal(m)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return raw
}

func runResp(t *testing.T, code int, body []byte) *api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse {
	t.Helper()
	r := &api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		HTTPResponse: makeHTTPResp(code),
		Body:         body,
	}
	if code == http.StatusOK {
		var v api.OperationRunStatusResponse
		if err := json.Unmarshal(body, &v); err != nil {
			t.Fatalf("decode run: %v", err)
		}
		r.JSON200 = &v
	}
	return r
}

func execRunStatus(t *testing.T, f *fakeOperationsClient, args ...string) (string, string, error) {
	t.Helper()
	withFakeClient(t, f)
	prev := runCheckPauseFor
	runCheckPauseFor = 0
	t.Cleanup(func() { runCheckPauseFor = prev })
	cmd := newRunStatusCmd()
	var out, errOut bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errOut)
	cmd.SetArgs(append([]string{testRunID, "--backplane", "https://x"}, args...))
	err := cmd.Execute()
	return out.String(), errOut.String(), err
}

func TestCallAsyncSendsFlagAndPrintsRunID(t *testing.T) {
	f := &fakeOperationsClient{callResponses: []*api.PostCallApiV1OperationsCallPostResponse{{
		HTTPResponse: makeHTTPResp(http.StatusAccepted),
		Body:         []byte(`{"run_id":"` + testRunID + `","status":"pending","async":true}`),
	}}}
	withFakeClient(t, f)
	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--async", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if f.lastCallBody == nil || f.lastCallBody.Async == nil || !*f.lastCallBody.Async {
		t.Fatalf("async: true not sent: %+v", f.lastCallBody)
	}
	want := "Started. Run id: " + testRunID + ". Check it with `meho operation run-status " + testRunID + "`."
	if !strings.Contains(out.String(), want) {
		t.Errorf("output %q lacks %q", out.String(), want)
	}
}

func TestCallAsyncJSONPrintsAcceptedAnswer(t *testing.T) {
	f := &fakeOperationsClient{callResponses: []*api.PostCallApiV1OperationsCallPostResponse{{
		HTTPResponse: makeHTTPResp(http.StatusAccepted),
		Body:         []byte(`{"run_id":"` + testRunID + `","status":"pending","async":true}`),
	}}}
	withFakeClient(t, f)
	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--async", "--json", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("not JSON: %v: %s", err, out.String())
	}
	if got["run_id"] != testRunID || got["status"] != "pending" || got["async"] != true {
		t.Errorf("unexpected answer: %v", got)
	}
}

func TestCallWithoutAsyncSendsNoAsyncField(t *testing.T) {
	response := callDeliveryResponse(t, map[string]any{"status": "ok", "op_id": "vault.kv.read"})
	if _, err := executeDeliveryCall(t, response, false); err != nil {
		t.Fatalf("execute: %v", err)
	}
}

func TestCallWithoutAsyncRejects202(t *testing.T) {
	f := &fakeOperationsClient{callResponses: []*api.PostCallApiV1OperationsCallPostResponse{{
		HTTPResponse: makeHTTPResp(http.StatusAccepted),
		Body:         []byte(`{"run_id":"x"}`),
	}}}
	withFakeClient(t, f)
	cmd := newCallCmd()
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--backplane", "https://x"})
	if err := cmd.Execute(); err == nil {
		t.Fatal("a 202 without --async must stay an error")
	}
	if f.lastCallBody.Async != nil {
		t.Errorf("async field must be absent without the flag, got %v", *f.lastCallBody.Async)
	}
}

func TestCallAsyncOldServerAnswers200(t *testing.T) {
	response := callDeliveryResponse(t, map[string]any{"status": "ok", "op_id": "vault.kv.read"})
	f := &fakeOperationsClient{callResponses: []*api.PostCallApiV1OperationsCallPostResponse{response}}
	withFakeClient(t, f)
	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	cmd.SetArgs([]string{"vault-1.x", "vault.kv.read", "--async", "--backplane", "https://x"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(out.String(), "status=ok") {
		t.Errorf("a 200 answer must render as a normal result, got %q", out.String())
	}
}

func TestRunStatusRunning(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "running", nil, nil)),
	}}
	out, _, err := execRunStatus(t, f)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	for _, want := range []string{"state=running", "still going", "2026-11-07T10:00:01Z"} {
		if !strings.Contains(out, want) {
			t.Errorf("output %q lacks %q", out, want)
		}
	}
	if f.lastRunParams[0].WaitSeconds != nil {
		t.Error("a plain check must not ask the server to wait")
	}
}

func TestRunStatusSucceededPrintsResult(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "succeeded",
			map[string]any{"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"k": "v"}}, nil)),
	}}
	out, _, err := execRunStatus(t, f)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(out, "vault-1.x vault.kv.read — status=ok") || !strings.Contains(out, `"k": "v"`) {
		t.Errorf("result not printed like a call: %q", out)
	}
}

func TestRunStatusSucceededWithErrorResultPrintsErrorAndExits1(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "succeeded",
			map[string]any{"status": "error", "op_id": "vault.kv.read", "error": "boom"}, nil)),
	}}
	out, _, err := execRunStatus(t, f)
	if !errors.Is(err, errOpError) {
		t.Fatalf("want errOpError, got %v", err)
	}
	if !strings.Contains(out, "status=error") || !strings.Contains(out, "boom") {
		t.Errorf("error not printed: %q", out)
	}
}

func TestRunStatusFailedShowsReasonAndIsNotSuccess(t *testing.T) {
	reason := "worker lost after a restart"
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "failed", nil, &reason)),
	}}
	out, _, err := execRunStatus(t, f)
	if !errors.Is(err, errOpError) {
		t.Fatalf("a failed run must exit non-zero, got %v", err)
	}
	if !strings.Contains(out, "The run failed. It has no result.") || !strings.Contains(out, "reason: "+reason) {
		t.Errorf("failed run not explained: %q", out)
	}
	if strings.Contains(out, "status=ok") {
		t.Errorf("failed run shown as success: %q", out)
	}
}

func TestRunStatusUncertainAndCancelled(t *testing.T) {
	for state, want := range map[string]string{
		"uncertain": "Check the target before you start it again.",
		"cancelled": "The run was cancelled.",
	} {
		f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
			runResp(t, 200, runBody(t, state, nil, nil)),
		}}
		out, _, err := execRunStatus(t, f)
		if !errors.Is(err, errOpError) {
			t.Errorf("%s: want errOpError, got %v", state, err)
		}
		if !strings.Contains(out, want) {
			t.Errorf("%s: output %q lacks %q", state, out, want)
		}
	}
}

func TestRunStatusSucceededWithoutVisibleResult(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "succeeded", nil, nil)),
	}}
	out, _, err := execRunStatus(t, f)
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(out, "Only the person who started the run can read its result.") {
		t.Errorf("hidden result not explained: %q", out)
	}
}

func TestRunStatusWaitChecksUntilEnd(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "pending", nil, nil)),
		runResp(t, 200, runBody(t, "running", nil, nil)),
		runResp(t, 200, runBody(t, "succeeded",
			map[string]any{"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"k": "v"}}, nil)),
	}}
	out, _, err := execRunStatus(t, f, "--wait")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if len(f.lastRunParams) != 3 {
		t.Fatalf("want 3 checks, got %d", len(f.lastRunParams))
	}
	for _, p := range f.lastRunParams {
		if p.WaitSeconds == nil || *p.WaitSeconds != runCheckWaitSeconds {
			t.Errorf("each check must ask for a short wait, got %v", p.WaitSeconds)
		}
	}
	if strings.Contains(out, "still going") || !strings.Contains(out, `"k": "v"`) {
		t.Errorf("only the final result should print: %q", out)
	}
}

func TestRunStatusUnknownRun(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 404, []byte(`{"detail":"operation_run_not_found"}`)),
	}}
	_, errOut, err := execRunStatus(t, f)
	if err == nil {
		t.Fatal("unknown run must be an error")
	}
	if !strings.Contains(errOut, "no operation run "+testRunID) {
		t.Errorf("unclear error: %q", errOut)
	}
}

func TestRunStatusInvalidID(t *testing.T) {
	withFakeClient(t, &fakeOperationsClient{})
	cmd := newRunStatusCmd()
	var errOut bytes.Buffer
	cmd.SetOut(&bytes.Buffer{})
	cmd.SetErr(&errOut)
	cmd.SetArgs([]string{"not-a-uuid", "--backplane", "https://x"})
	if err := cmd.Execute(); err == nil || !strings.Contains(errOut.String(), "invalid <run_id>") {
		t.Fatalf("want invalid id error, got %v / %q", err, errOut.String())
	}
}

func TestRunStatusJSONPrintsRun(t *testing.T) {
	f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
		runResp(t, 200, runBody(t, "running", nil, nil)),
	}}
	out, _, err := execRunStatus(t, f, "--json")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(out), &got); err != nil || got["status"] != "running" {
		t.Errorf("bad JSON %q: %v", out, err)
	}
}

// A stored result with an integer above 2^53 must keep every digit,
// in the human render and under --json, like `meho operation call`.
func TestRunStatusKeepsLargeIntegers(t *testing.T) {
	const big = "9007199254740993"
	body := runBody(t, "succeeded",
		map[string]any{"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"id": int64(9007199254740993)}}, nil)
	if !bytes.Contains(body, []byte(big)) {
		t.Fatalf("test body lost the number: %s", body)
	}
	for _, args := range [][]string{nil, {"--json"}} {
		f := &fakeOperationsClient{runResponses: []*api.GetOperationRunStatusApiV1OperationsRunsHandleGetResponse{
			runResp(t, 200, body),
		}}
		out, _, err := execRunStatus(t, f, args...)
		if err != nil {
			t.Fatalf("execute %v: %v", args, err)
		}
		if !strings.Contains(out, big) {
			t.Errorf("args %v: number changed, want %s in %q", args, big, out)
		}
	}
}
