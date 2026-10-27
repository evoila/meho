// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"encoding/json"
	"testing"
)

func TestHTTPValidationErrorDecodesStringDetail(t *testing.T) {
	var got HTTPValidationError
	if err := json.Unmarshal([]byte(`{"detail":"delegated_reason_invalid: add a blank line"}`), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Detail == nil || len(*got.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.Detail)
	}
	if msg := (*got.Detail)[0].Msg; msg != "delegated_reason_invalid: add a blank line" {
		t.Errorf("msg: got %q", msg)
	}
}

func TestHTTPValidationErrorDecodesListDetail(t *testing.T) {
	var got HTTPValidationError
	body := `{"detail":[{"loc":["body","decision"],"msg":"field required","type":"missing"}]}`
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Detail == nil || len(*got.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.Detail)
	}
	d := (*got.Detail)[0]
	if d.Msg != "field required" || d.Type != "missing" || len(d.Loc) != 2 {
		t.Errorf("list entry decoded wrong: %+v", d)
	}
}

func TestHTTPValidationErrorDecodesMissingDetail(t *testing.T) {
	for _, body := range []string{`{}`, `{"detail":null}`} {
		var got HTTPValidationError
		if err := json.Unmarshal([]byte(body), &got); err != nil {
			t.Fatalf("unmarshal %s: %v", body, err)
		}
		if got.Detail != nil {
			t.Errorf("%s: want nil detail; got %+v", body, got.Detail)
		}
	}
}

func TestHTTPValidationErrorRejectsOtherShapes(t *testing.T) {
	var got HTTPValidationError
	if err := json.Unmarshal([]byte(`{"detail":42}`), &got); err == nil {
		t.Errorf("want an error for a number detail; got %+v", got.Detail)
	}
}
