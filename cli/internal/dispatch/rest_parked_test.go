// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package dispatch

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

const parkedBody = `{"status":"awaiting_approval","op_id":"targets.register",` +
	`"approval_request_id":"00000000-0000-0000-0000-0000000000a1","message":"waits"}`

func TestParseRESTParked(t *testing.T) {
	t.Parallel()
	cases := map[string]bool{
		parkedBody:                                  true,
		`{"status":"awaiting_approval"}`:            false, // no approval id
		`{"status":"ok","approval_request_id":"x"}`: false,
		`{"name":"a-target","product":"ssh"}`:       false,
		``:                                          false,
		`not json`:                                  false,
	}
	for body, want := range cases {
		if _, got := ParseRESTParked([]byte(body)); got != want {
			t.Errorf("ParseRESTParked(%q) = %v; want %v", body, got, want)
		}
	}
}

func TestRenderRESTParkedHuman(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	parked, err := RenderRESTParked(&out, []byte(parkedBody), false)
	if err != nil || !parked {
		t.Fatalf("RenderRESTParked = %v, %v; want true, nil", parked, err)
	}
	for _, want := range []string{
		"targets.register — status=awaiting_approval",
		ParkedHint,
		"meho approvals show 00000000-0000-0000-0000-0000000000a1",
	} {
		if !strings.Contains(out.String(), want) {
			t.Errorf("output misses %q:\n%s", want, out.String())
		}
	}
}

func TestRenderRESTParkedJSON(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	if parked, err := RenderRESTParked(&out, []byte(parkedBody), true); err != nil || !parked {
		t.Fatalf("RenderRESTParked = %v, %v; want true, nil", parked, err)
	}
	var got RESTParked
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatalf("output is not JSON: %v\n%s", err, out.String())
	}
	if got.ApprovalRequestID != "00000000-0000-0000-0000-0000000000a1" || got.OpID != "targets.register" {
		t.Errorf("unexpected JSON: %+v", got)
	}
}

func TestRenderRESTParkedLeavesOtherAnswersAlone(t *testing.T) {
	t.Parallel()
	var out bytes.Buffer
	parked, err := RenderRESTParked(&out, []byte(`{"name":"a-target"}`), false)
	if parked || err != nil || out.Len() != 0 {
		t.Errorf("RenderRESTParked = %v, %v, %q; want false, nil, empty", parked, err, out.String())
	}
}
