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

func TestHTTPValidationErrorDecodesObjectDetail(t *testing.T) {
	// The shape the backplane sends when an ingest label is refused (#4728).
	body := `{"detail":{"kind":"uncovered_version_label","product":"networker","version":"19.12",` +
		`"impl_id":"networker-rest","registered_classes":[{"class_name":"X","version":"19.14",` +
		`"impl_id":"networker-rest","supported_version_range":">=19.14,<20.0"}],` +
		`"message":"version='19.12' is not covered"}}`
	var got HTTPValidationError
	if err := json.Unmarshal([]byte(body), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Detail == nil || len(*got.Detail) != 1 {
		t.Fatalf("want one detail entry; got %+v", got.Detail)
	}
	d := (*got.Detail)[0]
	if d.Msg != "version='19.12' is not covered" || d.Type != "uncovered_version_label" {
		t.Errorf("object detail decoded wrong: %+v", d)
	}
}

func TestHTTPValidationErrorObjectDetailWithoutMessageKeepsText(t *testing.T) {
	var got HTTPValidationError
	if err := json.Unmarshal([]byte(`{"detail":{"error":"output_too_large","limit":5}}`), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	d := (*got.Detail)[0]
	if d.Type != "output_too_large" || d.Msg != `{"error":"output_too_large","limit":5}` {
		t.Errorf("want type and raw text kept; got %+v", d)
	}
}
