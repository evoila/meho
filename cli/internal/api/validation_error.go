// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package api

import (
	"bytes"
	"encoding/json"
)

// UnmarshalJSON lets a 422 body decode when its `detail` is a plain
// string, not only the list the OpenAPI spec declares. Some backplane
// routes answer 422 with `{"detail": "<text>"}`. Without this method the
// generated parsers fail on that body and return a JSON decode error
// instead of the response, so the server's message is lost (#5134).
//
// A string detail becomes one ValidationError whose Msg is the text.
// An object detail (for example the structured ingest refusals) becomes
// one ValidationError whose Msg is the object's `message` and whose Type
// is its error name (`kind`, `error` or `code`). An object with no
// `message` keeps its JSON text as Msg, so the reason is never lost
// (#4728). A list detail decodes as before. Any other shape is still an
// error.
func (e *HTTPValidationError) UnmarshalJSON(data []byte) error {
	var raw struct {
		Detail json.RawMessage `json:"detail"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return err
	}
	e.Detail = nil
	if len(raw.Detail) == 0 || bytes.Equal(raw.Detail, []byte("null")) {
		return nil
	}
	var text string
	if err := json.Unmarshal(raw.Detail, &text); err == nil {
		e.Detail = &[]ValidationError{{Msg: text}}
		return nil
	}
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(raw.Detail, &obj); err == nil && obj != nil {
		e.Detail = &[]ValidationError{objectDetail(raw.Detail, obj)}
		return nil
	}
	var list []ValidationError
	if err := json.Unmarshal(raw.Detail, &list); err != nil {
		return err
	}
	e.Detail = &list
	return nil
}

// objectDetail turns an object `detail` into one ValidationError.
func objectDetail(whole json.RawMessage, obj map[string]json.RawMessage) ValidationError {
	var v ValidationError
	if err := json.Unmarshal(obj["message"], &v.Msg); err != nil || v.Msg == "" {
		v.Msg = string(whole)
	}
	for _, key := range []string{"kind", "error", "code"} {
		var name string
		if err := json.Unmarshal(obj[key], &name); err == nil && name != "" {
			v.Type = name
			break
		}
	}
	return v
}
