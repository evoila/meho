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

func callDeliveryResponse(t *testing.T, body any) *api.PostCallApiV1OperationsCallPostResponse {
	t.Helper()
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal response: %v", err)
	}
	return &api.PostCallApiV1OperationsCallPostResponse{
		HTTPResponse: makeHTTPResp(http.StatusOK),
		Body:         raw,
	}
}

func executeDeliveryCall(t *testing.T, response *api.PostCallApiV1OperationsCallPostResponse, jsonOut bool) (string, error) {
	t.Helper()
	withFakeClient(t, &fakeOperationsClient{callResponses: []*api.PostCallApiV1OperationsCallPostResponse{response}})

	cmd := newCallCmd()
	var out bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&bytes.Buffer{})
	args := []string{"vault-1.x", "vault.kv.read", "--backplane", "https://x"}
	if jsonOut {
		args = append(args, "--json")
	}
	cmd.SetArgs(args)
	err := cmd.Execute()
	return out.String(), err
}

// Older backplanes do not send the additive receipt/delivery fields. Their
// success envelope must remain a successful command, rather than becoming a
// compatibility or exit-code regression.
func TestCallDeliveryOldServerEnvelopeRemainsExitZero(t *testing.T) {
	out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
		"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"version": 3},
	}), false)
	if err != nil {
		t.Fatalf("old server success must exit 0: %v", err)
	}
	if !strings.Contains(out, "status=ok") || !strings.Contains(out, `"version": 3`) {
		t.Fatalf("old server result was not rendered: %q", out)
	}
	for _, forbidden := range []string{"audit receipt:", "delivery:", "do not re-invoke"} {
		if strings.Contains(out, forbidden) {
			t.Errorf("old server response unexpectedly rendered %q: %q", forbidden, out)
		}
	}
}

func TestCallDeliveryCompleteRendersReceiptAndDelivery(t *testing.T) {
	const auditID = "11111111-1111-1111-1111-111111111111"
	out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
		"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"version": 4},
		"audit_id": auditID, "delivery": "complete",
	}), false)
	if err != nil {
		t.Fatalf("complete delivery must exit 0: %v", err)
	}
	for _, want := range []string{"audit receipt: " + auditID, "delivery: complete"} {
		if !strings.Contains(out, want) {
			t.Errorf("complete render missing %q: %q", want, out)
		}
	}
	if strings.Contains(out, "do not re-invoke") {
		t.Errorf("complete delivery must not show incomplete-delivery remediation: %q", out)
	}
}

func TestCallDeliveryIncompleteRendersNoReinvokeRemediation(t *testing.T) {
	const auditID = "22222222-2222-2222-2222-222222222222"
	for _, delivery := range []string{"partial", "unavailable"} {
		t.Run(delivery, func(t *testing.T) {
			out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
				"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"version": 5},
				"audit_id": auditID, "delivery": delivery,
			}), false)
			if err != nil {
				t.Fatalf("%s delivery must still exit 0: %v", delivery, err)
			}
			for _, want := range []string{
				"audit receipt: " + auditID,
				"delivery: " + delivery,
				"operation already executed; do not re-invoke it.",
				"inspect the audit receipt or operation status.",
			} {
				if !strings.Contains(out, want) {
					t.Errorf("%s render missing %q: %q", delivery, want, out)
				}
			}
		})
	}
}

func TestCallDeliveryUnavailableNullReceiptNamesStatusRemediation(t *testing.T) {
	out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
		"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"version": 6},
		"audit_id": nil, "delivery": "unavailable",
	}), false)
	if err != nil {
		t.Fatalf("unavailable delivery without receipt must still exit 0: %v", err)
	}
	for _, want := range []string{
		"delivery: unavailable",
		"operation already executed; do not re-invoke it.",
		"inspect operation status; no committed audit receipt is available.",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("null-receipt render missing %q: %q", want, out)
		}
	}
	if strings.Contains(out, "audit receipt:") {
		t.Errorf("null receipt must not invent an audit receipt: %q", out)
	}
}

func TestCallDeliveryJSONPreservesNullableFields(t *testing.T) {
	for _, tc := range []struct {
		name     string
		auditID  any
		delivery any
	}{
		{name: "explicit nulls", auditID: nil, delivery: nil},
		{name: "present values", auditID: "33333333-3333-3333-3333-333333333333", delivery: "complete"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
				"status": "ok", "op_id": "vault.kv.read", "result": map[string]any{"version": 7},
				"audit_id": tc.auditID, "delivery": tc.delivery,
			}), true)
			if err != nil {
				t.Fatalf("json success must exit 0: %v", err)
			}
			var envelope map[string]any
			if err := json.Unmarshal([]byte(out), &envelope); err != nil {
				t.Fatalf("decode --json output: %v; output=%q", err, out)
			}
			if got := envelope["audit_id"]; got != tc.auditID {
				t.Errorf("audit_id: got %#v want %#v", got, tc.auditID)
			}
			if got := envelope["delivery"]; got != tc.delivery {
				t.Errorf("delivery: got %#v want %#v", got, tc.delivery)
			}
		})
	}
}

func TestCallDeliveryDoesNotChangeStructuredFailureExit(t *testing.T) {
	for _, status := range []string{"error", "denied"} {
		t.Run(status, func(t *testing.T) {
			out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
				"status": status, "op_id": "vault.kv.read", "error": "policy refused",
				"audit_id": "44444444-4444-4444-4444-444444444444", "delivery": "complete",
			}), true)
			if !errors.Is(err, errOpError) {
				t.Fatalf("status=%s must retain structured-failure exit: %v", status, err)
			}
			var envelope map[string]any
			if err := json.Unmarshal([]byte(out), &envelope); err != nil {
				t.Fatalf("decode --json output: %v; output=%q", err, out)
			}
			if envelope["status"] != status {
				t.Errorf("json status: got %#v want %q", envelope["status"], status)
			}
		})
	}
}

// A response too large to deliver comes back as delivery=partial with a
// handle; the render points the operator at result-query for that handle.
func TestCallDeliveryPartialPointsAtResultQuery(t *testing.T) {
	const handle = "55555555-5555-5555-5555-555555555555"
	out, err := executeDeliveryCall(t, callDeliveryResponse(t, map[string]any{
		"status": "ok", "op_id": "vault.kv.read", "result": nil,
		"handle":   map[string]any{"handle_id": handle},
		"audit_id": "66666666-6666-6666-6666-666666666666", "delivery": "partial",
	}), false)
	if err != nil {
		t.Fatalf("partial delivery must exit 0: %v", err)
	}
	for _, want := range []string{
		"delivery: partial",
		"do not re-invoke it.",
		"meho operation result-query " + handle,
	} {
		if !strings.Contains(out, want) {
			t.Errorf("partial render missing %q: %q", want, out)
		}
	}
}

// A result-query read over the delivery limit is a 422 with
// reason=output_too_large; the CLI names it and shows the server message.
func TestResultQueryOutputTooLargeRendersDeliveryMessage(t *testing.T) {
	body := `{"detail":{"message":"the response is 3000000 bytes on the wire, over the 2097152-byte delivery limit, so it was not delivered (delivery unavailable). Read a smaller window: lower ` + "`limit`" + ` and retry.","reason":"output_too_large","delivery":"unavailable"}}`
	withFakeClient(t, &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(http.StatusUnprocessableEntity), Body: []byte(body)},
		},
	})
	cmd := newResultQueryCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{"11111111-1111-1111-1111-111111111111", "--backplane", "https://x"})
	_ = cmd.Execute()
	for _, want := range []string{"result too large to deliver:", "2097152-byte delivery limit", "lower `limit`"} {
		if !strings.Contains(errBuf.String(), want) {
			t.Errorf("output_too_large render missing %q: %q", want, errBuf.String())
		}
	}
}

func TestOutputTooLargeMessageIgnoresOtherReasons(t *testing.T) {
	err := &apiResponseError{StatusCode: 404, Body: `{"detail":{"message":"gone","reason":"handle_not_found"}}`}
	if _, ok := outputTooLargeMessage(err); ok {
		t.Fatal("handle_not_found must not be treated as output_too_large")
	}
	if _, ok := outputTooLargeMessage(errors.New("dial tcp: refused")); ok {
		t.Fatal("a transport error must not be treated as output_too_large")
	}
}
