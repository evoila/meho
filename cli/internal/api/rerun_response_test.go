// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"io"
	"net/http"
	"strings"
	"testing"
)

// POST /api/v1/approvals/{id}/rerun answers 422 with a string detail when
// MEHO no longer holds the params of a credential write (#5417). The
// generated parser must keep that text (see validation_error.go, #5151),
// so a CLI verb can show the recovery step instead of a decode error.
func TestParseRerunApprovalRequestResponseKeepsStringDetailOn422(t *testing.T) {
	const detail = "approval_rerun_params_required: approval request 0f1e has no stored params " +
		"(a credential write keeps them only until the first run). Send the original params again"
	rsp := &http.Response{
		StatusCode: http.StatusUnprocessableEntity,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body:       io.NopCloser(strings.NewReader(`{"detail":"` + detail + `"}`)),
	}

	got, err := ParseRerunApprovalRequestApiV1ApprovalsRequestIdRerunPostResponse(rsp)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got.StatusCode() != http.StatusUnprocessableEntity {
		t.Fatalf("status: got %d", got.StatusCode())
	}
	if got.JSON422 == nil || got.JSON422.Detail == nil || len(*got.JSON422.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.JSON422)
	}
	if msg := (*got.JSON422.Detail)[0].Msg; msg != detail {
		t.Errorf("msg: got %q", msg)
	}
}

// A body validation error on the same route still decodes as the list
// FastAPI sends.
func TestParseRerunApprovalRequestResponseKeepsListDetailOn422(t *testing.T) {
	rsp := &http.Response{
		StatusCode: http.StatusUnprocessableEntity,
		Header:     http.Header{"Content-Type": []string{"application/json"}},
		Body: io.NopCloser(strings.NewReader(
			`{"detail":[{"loc":["body","extra"],"msg":"Extra inputs are not permitted","type":"extra_forbidden"}]}`,
		)),
	}

	got, err := ParseRerunApprovalRequestApiV1ApprovalsRequestIdRerunPostResponse(rsp)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if got.JSON422 == nil || got.JSON422.Detail == nil || len(*got.JSON422.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.JSON422)
	}
	if d := (*got.JSON422.Detail)[0]; d.Type != "extra_forbidden" || len(d.Loc) != 2 {
		t.Errorf("list entry decoded wrong: %+v", d)
	}
}
