// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/spf13/cobra"

	"github.com/evoila/meho/cli/internal/api"
)

// TestPreviewAndCallSendsThePreviewHash — the call carries the hash the
// preview returned, and the preview and call carry the same op, target
// and params.
func TestPreviewAndCallSendsThePreviewHash(t *testing.T) {
	f := withFake(t, &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: []byte(`{"status":"ok","preview_hash":"h-7"}`)},
		},
		callResponses: []*api.PostCallApiV1OperationsCallPostResponse{
			{HTTPResponse: makeHTTPResp(200), Body: []byte(`{"status":"awaiting_approval","op_id":"x.delete"}`)},
		},
	})
	params := map[string]any{"id": 7}
	r, p, err := New("c").PreviewAndCall(context.Background(), "https://bp", "x.delete", "t", params)
	if err != nil {
		t.Fatalf("PreviewAndCall: %v", err)
	}
	if r == nil || r.Status != "awaiting_approval" || p.PreviewHash != "h-7" {
		t.Fatalf("unexpected results: call=%+v preview=%+v", r, p)
	}
	if f.lastCallBody.PreviewHash == nil || *f.lastCallBody.PreviewHash != "h-7" {
		t.Fatalf("preview_hash not sent on the call: %+v", f.lastCallBody.PreviewHash)
	}
	if f.lastPreviewBody.OpId != "x.delete" || f.lastCallBody.OpId != "x.delete" {
		t.Fatalf("op_id differs: preview=%q call=%q", f.lastPreviewBody.OpId, f.lastCallBody.OpId)
	}
	if (*f.lastPreviewBody.Params)["id"] != 7 || (*f.lastCallBody.Params)["id"] != 7 {
		t.Fatalf("params differ: preview=%+v call=%+v", f.lastPreviewBody.Params, f.lastCallBody.Params)
	}
}

// TestPreviewAndCallStopsWhenPreviewFails — a preview that is not ok, or
// that has no hash, sends no call.
func TestPreviewAndCallStopsWhenPreviewFails(t *testing.T) {
	for _, body := range []string{
		`{"status":"error","error":"robot not found"}`,
		`{"status":"unavailable"}`,
		`{"status":"ok"}`,
	} {
		f := withFake(t, &fakeOperationsClient{
			previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
				{HTTPResponse: makeHTTPResp(200), Body: []byte(body)},
			},
		})
		r, p, err := New("c").PreviewAndCall(context.Background(), "https://bp", "x.delete", "t", nil)
		if err != nil {
			t.Fatalf("%s: PreviewAndCall: %v", body, err)
		}
		if r != nil || p == nil {
			t.Fatalf("%s: expected no call result and the preview; got call=%+v preview=%+v", body, r, p)
		}
		if f.lastCallBody != nil {
			t.Fatalf("%s: no call may be sent; got %+v", body, f.lastCallBody)
		}
	}
}

// TestPreviewAndCallPropagatesPreviewError — a transport or HTTP error on
// the preview comes back as the error, and no call is sent.
func TestPreviewAndCallPropagatesPreviewError(t *testing.T) {
	f := withFake(t, &fakeOperationsClient{
		previewResponses: []*api.PostPreviewApiV1OperationsPreviewPostResponse{
			{HTTPResponse: makeHTTPResp(403), Body: []byte(`{"detail":"forbidden"}`)},
		},
	})
	_, _, err := New("c").PreviewAndCall(context.Background(), "https://bp", "x.delete", "t", nil)
	var apiErr *APIResponseError
	if !errors.As(err, &apiErr) || apiErr.StatusCode != 403 {
		t.Fatalf("expected *APIResponseError{403}; got %v", err)
	}
	if f.lastCallBody != nil {
		t.Fatalf("no call may be sent after a failed preview")
	}
}

func TestRenderPreviewFailureShowsTheReason(t *testing.T) {
	msg := "robot not found"
	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&out)
	err := New("c").RenderPreviewFailure(cmd, "x.delete",
		&PreviewResult{Status: "error", Error: &msg}, false)
	if !errors.Is(err, ErrOpError) {
		t.Fatalf("expected ErrOpError; got %v", err)
	}
	for _, want := range []string{"preview status=error", "The call was not sent", "robot not found"} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output misses %q:\n%s", want, out.String())
		}
	}
}

func TestRenderPreviewFailureJSONPrintsTheEnvelope(t *testing.T) {
	var out bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&out)
	raw := []byte(`{"status":"unavailable","op_id":"x.delete"}`)
	err := New("c").RenderPreviewFailure(cmd, "x.delete",
		&PreviewResult{Status: "unavailable", Raw: raw}, true)
	if !errors.Is(err, ErrOpError) {
		t.Fatalf("expected ErrOpError; got %v", err)
	}
	if !strings.Contains(out.String(), `"unavailable"`) {
		t.Errorf("JSON output should carry the preview envelope; got %s", out.String())
	}
}

func TestRenderPreviewFailureRejectsUnknownStatus(t *testing.T) {
	var out, errBuf bytes.Buffer
	cmd := &cobra.Command{}
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	err := New("c").RenderPreviewFailure(cmd, "x.delete", &PreviewResult{Status: "what"}, false)
	if err == nil || errors.Is(err, ErrOpError) {
		t.Fatalf("an unknown status should be an unexpected-response error; got %v", err)
	}
}
