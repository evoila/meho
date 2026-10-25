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

// Path mode of `meho operation result-query` (#4888). Every test runs the
// cobra command over the fake client, with made-up ids and values, and
// checks both the request the CLI sends (`query.path`, `offset`, `limit`)
// and what the operator sees.

const pathTestHandle = "22222222-2222-2222-2222-222222222222"

// pathReadResponse is the server's answer to a path read whose value is not
// an array: the value rides whole in `value` (_read_document_path).
func pathReadResponse(path, valueType, value string) []byte {
	return []byte(`{"handle_id":"` + pathTestHandle + `","path":"` + path + `","value_type":"` + valueType +
		`","value":` + value + `,"rows":[],"offset":0,"limit":50,"returned_rows":1,"total_rows":1,` +
		`"stored_rows":1,"truncated":false,"coverage":"complete","coverage_note":null}`)
}

func executeResultQuery(t *testing.T, f *fakeOperationsClient, args ...string) (stdout, stderr string, err error) {
	t.Helper()
	withFakeClient(t, f)
	cmd := newResultQueryCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(append([]string{pathTestHandle, "--backplane", "https://x"}, args...))
	err = cmd.Execute()
	return out.String(), errBuf.String(), err
}

func fakeWithResultQueryResponse(status int, body []byte) *fakeOperationsClient {
	return &fakeOperationsClient{
		resultQueryResponses: []*api.PostResultQueryApiV1OperationsResultQueryPostResponse{
			{HTTPResponse: makeHTTPResp(status), Body: body},
		},
	}
}

func TestResultQueryPathSendsPointerAndRendersOneValue(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK, pathReadResponse("/name", "string", `"vm-01"`))
	out, errOut, err := executeResultQuery(t, f, "--path", "/name")
	if err != nil {
		t.Fatalf("execute: %v (stderr %q)", err, errOut)
	}
	body := f.lastResultQueryBody
	if body == nil || body.Query == nil || body.Query.Path == nil || *body.Query.Path != "/name" {
		t.Fatalf("request must carry query.path=/name; got %+v", body)
	}
	if body.Query.Filter != nil || body.Query.Select != nil || body.Query.Limit != nil {
		t.Fatalf("a path read must carry no relational field; got %+v", body.Query)
	}
	if body.Offset == nil || *body.Offset != 0 || body.Limit == nil || *body.Limit != 50 {
		t.Fatalf("offset / limit must still be sent for array paging; got %+v", body)
	}
	for _, want := range []string{pathTestHandle + ` — path "/name" (string)`, `"vm-01"`} {
		if !strings.Contains(out, want) {
			t.Errorf("render missing %q: %q", want, out)
		}
	}
}

func TestResultQueryPathPrettyPrintsAnObject(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK,
		pathReadResponse("/hardware", "object", `{"cpu":2,"memory_mb":4096}`))
	out, _, err := executeResultQuery(t, f, "--path", "/hardware")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	for _, want := range []string{`path "/hardware" (object)`, "\n  \"cpu\": 2,\n  \"memory_mb\": 4096\n"} {
		if !strings.Contains(out, want) {
			t.Errorf("render missing %q: %q", want, out)
		}
	}
}

// A number above 2^53 must print with every digit. Decoding the value into
// `any` would round 9007199254740993 to 9007199254740992.
func TestResultQueryPathKeepsLargeIntegerDigits(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK, pathReadResponse("/id", "number", `9007199254740993`))
	out, _, err := executeResultQuery(t, f, "--path", "/id")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(out, "9007199254740993") {
		t.Fatalf("large integer must keep its digits: %q", out)
	}
}

func TestResultQueryPathKeepsLargeIntegerDigitsInsideAnObject(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK,
		pathReadResponse("/stats", "object", `{"bytes":9007199254740993,"ratio":1.5}`))
	out, _, err := executeResultQuery(t, f, "--path", "/stats")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	for _, want := range []string{"\n  \"bytes\": 9007199254740993,\n  \"ratio\": 1.5\n"} {
		if !strings.Contains(out, want) {
			t.Errorf("object render missing %q: %q", want, out)
		}
	}
	if strings.Contains(out, "9007199254740992") {
		t.Errorf("large integer was rounded: %q", out)
	}
}

func TestResultQueryPathKeepsLargeIntegerDigitsInArrayItems(t *testing.T) {
	body := []byte(`{"handle_id":"` + pathTestHandle + `","path":"/ids","value_type":"array",` +
		`"rows":[{"id":9007199254740993}],"offset":0,"limit":50,"returned_rows":1,"total_rows":1,` +
		`"stored_rows":1,"truncated":false,"coverage":"complete","coverage_note":null}`)
	f := fakeWithResultQueryResponse(http.StatusOK, body)
	out, _, err := executeResultQuery(t, f, "--path", "/ids")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if !strings.Contains(out, `"id": 9007199254740993`) {
		t.Fatalf("array item must keep the large integer digits: %q", out)
	}
}

func TestResultQueryPathEmptyPointerReadsTheWholeDocument(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK, pathReadResponse("", "object", `{"name":"vm-01"}`))
	out, _, err := executeResultQuery(t, f, "--path", "")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	body := f.lastResultQueryBody
	if body == nil || body.Query == nil || body.Query.Path == nil || *body.Query.Path != "" {
		t.Fatalf(`--path "" must send query.path=""; got %+v`, body)
	}
	if !strings.Contains(out, `path "" (object)`) || !strings.Contains(out, `"name": "vm-01"`) {
		t.Fatalf("whole-document render wrong: %q", out)
	}
}

func TestResultQueryPathPagesAnArray(t *testing.T) {
	body := []byte(`{"handle_id":"` + pathTestHandle + `","path":"/devices","value_type":"array",` +
		`"rows":[{"key":2002},{"key":2003}],"offset":2,"limit":2,"returned_rows":2,"total_rows":5,` +
		`"stored_rows":5,"truncated":false,"coverage":"complete","coverage_note":null}`)
	f := fakeWithResultQueryResponse(http.StatusOK, body)
	out, _, err := executeResultQuery(t, f, "--path", "/devices", "--offset", "2", "--limit", "2")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	req := f.lastResultQueryBody
	if req.Offset == nil || *req.Offset != 2 || req.Limit == nil || *req.Limit != 2 {
		t.Fatalf("array paging must send offset=2 limit=2; got %+v", req)
	}
	for _, want := range []string{`path "/devices" (array) — items 2..4 of 5, returned 2`, `"key": 2002`, `"key": 2003`} {
		if !strings.Contains(out, want) {
			t.Errorf("array render missing %q: %q", want, out)
		}
	}
}

// A rows handle keeps the top-level fields its inline summary left out
// (`omitted_scalar_count`); --path /<field> reads one of them back.
func TestResultQueryPathReadsLeftOutFieldOfRowsHandle(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK,
		pathReadResponse("/stderr", "string", `"warning: 3 items skipped"`))
	out, _, err := executeResultQuery(t, f, "--path", "/stderr")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	if *f.lastResultQueryBody.Query.Path != "/stderr" {
		t.Fatalf("query.path = %q, want /stderr", *f.lastResultQueryBody.Query.Path)
	}
	if !strings.Contains(out, `path "/stderr" (string)`) || !strings.Contains(out, "3 items skipped") {
		t.Fatalf("left-out field render wrong: %q", out)
	}
}

func TestResultQueryPathJSONCarriesPathValueTypeAndValue(t *testing.T) {
	f := fakeWithResultQueryResponse(http.StatusOK, pathReadResponse("/name", "string", `"vm-01"`))
	out, _, err := executeResultQuery(t, f, "--path", "/name", "--json")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	var got map[string]any
	if err := json.Unmarshal([]byte(out), &got); err != nil {
		t.Fatalf("--json output is not JSON: %v: %q", err, out)
	}
	if got["path"] != "/name" || got["value_type"] != "string" || got["value"] != "vm-01" {
		t.Fatalf("--json must carry path, value_type and value; got %v", got)
	}
}

// Without --path the --json envelope keeps its shape: no path-read keys.
func TestResultQueryJSONWithoutPathOmitsPathKeys(t *testing.T) {
	rows := []byte(`{"handle_id":"` + pathTestHandle + `","rows":[{"i":1}],"offset":0,"limit":50,` +
		`"returned_rows":1,"total_rows":1,"stored_rows":1,"truncated":false}`)
	f := fakeWithResultQueryResponse(http.StatusOK, rows)
	out, _, err := executeResultQuery(t, f, "--json")
	if err != nil {
		t.Fatalf("execute: %v", err)
	}
	for _, key := range []string{`"path"`, `"value_type"`, `"value"`} {
		if strings.Contains(out, key) {
			t.Errorf("paging --json must not carry %s: %q", key, out)
		}
	}
}

func TestResultQueryPathRefusesQueryFlagsBeforeAnyCall(t *testing.T) {
	cases := [][]string{
		{"--path", "/a", "--select", "name"},
		{"--path", "/a", "--where", "state = active"},
		{"--path", "/a", "--group-by", "state"},
		{"--path", "/a", "--aggregate", "COUNT"},
		{"--path", "/a", "--order-by", "name"},
		{"--path", "/a", "--query-limit", "10"},
	}
	for _, args := range cases {
		t.Run(strings.Join(args[2:], " "), func(t *testing.T) {
			f := &fakeOperationsClient{}
			_, errOut, err := executeResultQuery(t, f, args...)
			if err == nil {
				t.Fatal("mixing --path with a query flag must fail")
			}
			if !strings.Contains(errOut, "--path cannot be combined with") {
				t.Fatalf("refusal message missing: %q", errOut)
			}
			if f.lastResultQueryBody != nil {
				t.Fatalf("the refusal must happen before any call; got %+v", f.lastResultQueryBody)
			}
		})
	}
}

// Paging a document handle without --path is a 422 from the server. The CLI
// shows the server's message and adds the CLI form of the read that works.
func TestResultQueryPagingDocumentHandleAddsPathHint(t *testing.T) {
	refusal := `{"detail":{"message":"handle ` + pathTestHandle + ` cannot be read this way: it holds one ` +
		`document, not rows. Read it with ` + "`query={\\\"path\\\": \\\"<JSON Pointer>\\\"}`" +
		`, for example ` + "`\\\"/name\\\"`" + `.","reason":"invalid_query"}}`
	f := fakeWithResultQueryResponse(http.StatusUnprocessableEntity, []byte(refusal))
	_, errOut, err := executeResultQuery(t, f)
	if err == nil {
		t.Fatal("a 422 must be a command error")
	}
	for _, want := range []string{
		"cannot be read this way: it holds one document, not rows",
		"Read it with `meho operation result-query " + pathTestHandle + ` --path ""` + "`",
		"`--path /<key>`",
	} {
		if !strings.Contains(errOut, want) {
			t.Errorf("document-handle refusal missing %q: %q", want, errOut)
		}
	}
}

// A --path read that is too large is the same refusal as a too-large page.
func TestResultQueryPathTooLargeShowsServerMessage(t *testing.T) {
	refusal := `{"detail":{"message":"the value is 300000 bytes, over the 262144-byte output budget. ` +
		`Point ` + "`path`" + ` at a smaller part of the document.","reason":"output_too_large","delivery":"unavailable"}}`
	f := fakeWithResultQueryResponse(http.StatusUnprocessableEntity, []byte(refusal))
	_, errOut, err := executeResultQuery(t, f, "--path", "")
	if err == nil {
		t.Fatal("a 422 must be a command error")
	}
	if !strings.Contains(errOut, "result too large to deliver:") || !strings.Contains(errOut, "smaller part of the document") {
		t.Fatalf("output_too_large render wrong: %q", errOut)
	}
}

func TestDocumentHandleMessageMatchesOnlyTheDocumentRefusal(t *testing.T) {
	other := &apiResponseError{StatusCode: 422,
		Body: `{"detail":{"message":"pointer /nope does not resolve","reason":"invalid_query"}}`}
	if _, ok := documentHandleMessage(other); ok {
		t.Fatal("another invalid_query refusal must not get the document hint")
	}
	notFound := &apiResponseError{StatusCode: 404,
		Body: `{"detail":{"reason":"handle_not_found","handle_id":"x"}}`}
	if _, ok := documentHandleMessage(notFound); ok {
		t.Fatal("handle_not_found must not get the document hint")
	}
	if _, ok := documentHandleMessage(errors.New("dial tcp: refused")); ok {
		t.Fatal("a transport error must not get the document hint")
	}
	doc := &apiResponseError{StatusCode: 422,
		Body: `{"detail":{"message":"handle x cannot be read this way: it holds one document, not rows.","reason":"invalid_query"}}`}
	if msg, ok := documentHandleMessage(doc); !ok || !strings.Contains(msg, "holds one document") {
		t.Fatalf("document refusal not recognised: ok=%v msg=%q", ok, msg)
	}
}
