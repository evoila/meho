// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
)

// Path mode of `meho operation result-query` (#4888): the --path read of a
// document handle, its refusal hint, and its human render. The flag, the
// request and the result struct live in resultquery.go.

// documentHandleMessage returns the backplane's message when err is the
// `reason=invalid_query` refusal of paging (or querying) a document handle:
// the handle holds one document, not rows. The server has no separate
// reason for this case, so the message text is the only marker; it is the
// `ResultRecordKindError` text of the result handle store.
func documentHandleMessage(err error) (string, bool) {
	detail, ok := refusalDetail(err)
	if !ok || detail.Reason != "invalid_query" ||
		!strings.Contains(detail.Message, "holds one document, not rows") {
		return "", false
	}
	return detail.Message, true
}

// documentHandleHint is the one line the CLI adds after the server's
// document-handle refusal: the read that works, in CLI form.
func documentHandleHint(handleID string) string {
	return fmt.Sprintf("Read it with `meho operation result-query %s --path \"\"` "+
		"(the whole document) or `--path /<key>` (one top-level field).", handleID)
}

// apiRefusalDetail is the object `detail` of a backplane 422 refusal.
type apiRefusalDetail struct {
	Message string `json:"message"`
	Reason  string `json:"reason"`
}

// refusalDetail decodes the object `detail` out of an *apiResponseError
// body. It reports false for a non-API error and for a body whose `detail`
// is not an object with a reason (the validation-error list, for example).
func refusalDetail(err error) (apiRefusalDetail, bool) {
	var apiErr *apiResponseError
	if !errors.As(err, &apiErr) {
		return apiRefusalDetail{}, false
	}
	var body struct {
		Detail apiRefusalDetail `json:"detail"`
	}
	if json.Unmarshal([]byte(apiErr.Body), &body) != nil || body.Detail.Reason == "" {
		return apiRefusalDetail{}, false
	}
	return body.Detail, true
}

// printPathReadResult renders a path read: one header line with the handle
// id, the pointer and the JSON type of the value, then the value itself. An
// array value comes paged in Rows, so the header also states the window and
// the total and the items print like rows. Any other value is one node of
// the document, pretty-printed.
func printPathReadResult(w io.Writer, r *ResultQueryResult) {
	if r.ValueType == "array" {
		end := r.Offset + r.ReturnedRows
		fmt.Fprintf(w, "%s — path %q (array) — items %d..%d of %d, returned %d\n",
			r.HandleID, *r.Path, r.Offset, end, r.TotalRows, r.ReturnedRows)
		if r.ReturnedRows == 0 {
			fmt.Fprintln(w, "  (empty window — the offset is past the end of the array)")
			return
		}
		printRows(w, r.Rows)
		return
	}
	fmt.Fprintf(w, "%s — path %q (%s)\n", r.HandleID, *r.Path, r.ValueType)
	if len(r.Value) == 0 {
		return
	}
	printIndentedJSON(w, r.Value)
}

// printRows pretty-prints each row on its own.
func printRows(w io.Writer, rows []json.RawMessage) {
	for _, row := range rows {
		printIndentedJSON(w, row)
	}
}

// printIndentedJSON re-indents the raw bytes without decoding them, so a
// number keeps every digit the backplane stored (decoding into `any` would
// turn an integer above 2^53 into a rounded float64). Bytes that are not
// valid JSON print as they are.
func printIndentedJSON(w io.Writer, raw json.RawMessage) {
	var buf bytes.Buffer
	if err := json.Indent(&buf, raw, "", "  "); err != nil {
		fmt.Fprintln(w, string(raw))
		return
	}
	fmt.Fprintln(w, buf.String())
}
