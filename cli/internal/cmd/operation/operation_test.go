// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// TestTruncateAsciiNoTrim — string within budget passes through.
func TestTruncateAsciiNoTrim(t *testing.T) {
	if got := truncate("hello", 10); got != "hello" {
		t.Fatalf("truncate within budget: got %q; want %q", got, "hello")
	}
}

// TestTruncateAsciiTrimAppendsEllipsis — over-budget ASCII keeps
// the first maxLen-1 chars and appends U+2026.
func TestTruncateAsciiTrimAppendsEllipsis(t *testing.T) {
	if got := truncate("hello world", 5); got != "hell…" {
		t.Fatalf("truncate over-budget ascii: got %q; want %q", got, "hell…")
	}
}

// TestTruncateMultiByteRuneSafe — multi-byte UTF-8 must not produce
// invalid byte cuts. The rune-aware shape is the load-bearing one;
// a byte-slice cut on "café" at byte 3 would split the é codepoint.
func TestTruncateMultiByteRuneSafe(t *testing.T) {
	if got := truncate("café world", 5); got != "café…" {
		t.Fatalf("truncate multi-byte: got %q; want %q", got, "café…")
	}
}

// TestTruncateZeroBudget — degenerate maxLen=0 returns empty rather
// than panicking on a negative slice index.
func TestTruncateZeroBudget(t *testing.T) {
	if got := truncate("anything", 0); got != "" {
		t.Fatalf("truncate maxLen=0: got %q; want %q", got, "")
	}
}

// TestStrDerefNilEmptyOtherwiseValue — covers the Optional[str]
// shape coming back from the backend Pydantic models.
func TestStrDerefNilEmptyOtherwiseValue(t *testing.T) {
	if got := strDeref(nil); got != "" {
		t.Fatalf("strDeref(nil): got %q; want %q", got, "")
	}
	v := "hello"
	if got := strDeref(&v); got != "hello" {
		t.Fatalf("strDeref(&v): got %q; want %q", got, "hello")
	}
}

// TestPrintGroupsTableHumanFormat — happy-path render with 2 groups.
func TestPrintGroupsTableHumanFormat(t *testing.T) {
	r := &GroupsResponse{
		ConnectorID: "vault-1.x",
		Groups: []GroupSummary{
			{GroupKey: "kv", Name: "Key-Value", WhenToUse: "Read or write secrets", OperationCount: 2},
			{GroupKey: "sys", Name: "System", WhenToUse: "Vault health + metrics", OperationCount: 1},
		},
	}
	var buf bytes.Buffer
	printGroupsTable(&buf, r)
	out := buf.String()
	for _, want := range []string{"vault-1.x", "2 enabled group(s)", "kv", "Key-Value", "Read or write secrets", "sys"} {
		if !strings.Contains(out, want) {
			t.Errorf("printGroupsTable missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintGroupsTableEmpty — zero-group connector renders the
// no-groups line (no header, no rows).
func TestPrintGroupsTableEmpty(t *testing.T) {
	r := &GroupsResponse{ConnectorID: "vmware-rest-9.0", Groups: nil}
	var buf bytes.Buffer
	printGroupsTable(&buf, r)
	out := buf.String()
	if !strings.Contains(out, "0 enabled groups") {
		t.Errorf("empty groups response should announce 0 enabled groups; got:\n%s", out)
	}
	if strings.Contains(out, "group_key") {
		t.Errorf("empty groups response should not render the header row; got:\n%s", out)
	}
}

// TestPrintSearchTableHumanFormat — happy-path render with 2 hits.
func TestPrintSearchTableHumanFormat(t *testing.T) {
	summary := "Read a Vault KV secret"
	r := &SearchResponse{
		Hits: []SearchHit{
			{OpID: "vault.kv.read", Summary: &summary, SafetyLevel: "safe", FusedScore: 0.987},
			{OpID: "vault.kv.list", Summary: nil, SafetyLevel: "safe", FusedScore: 0.413},
		},
		QueryDurationMs: 42.7,
	}
	var buf bytes.Buffer
	printSearchTable(&buf, "vault-1.x", "kv read", r)
	out := buf.String()
	for _, want := range []string{"vault.kv.read", "vault.kv.list", "Read a Vault KV secret", "2 hit(s)", "0.987"} {
		if !strings.Contains(out, want) {
			t.Errorf("printSearchTable missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintSearchTableEmpty — zero hits renders the header line
// without the table body.
func TestPrintSearchTableEmpty(t *testing.T) {
	r := &SearchResponse{Hits: nil, QueryDurationMs: 12.0}
	var buf bytes.Buffer
	printSearchTable(&buf, "vault-1.x", "nonexistent", r)
	out := buf.String()
	if !strings.Contains(out, "0 hit(s)") {
		t.Errorf("empty search should announce 0 hits; got:\n%s", out)
	}
	if strings.Contains(out, "op_id") {
		t.Errorf("empty search should skip the table header; got:\n%s", out)
	}
}

// TestPrintCallResultOkRendersResult — status=ok with a result body
// pretty-prints the result JSON. The exit-non-zero branch is
// covered separately at the runCall level (smoke).
func TestPrintCallResultOkRendersResult(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "vault.kv.read",
		Result:     json.RawMessage(`{"value":"secret"}`),
		DurationMs: 23.4,
	}
	var buf bytes.Buffer
	printCallResult(&buf, "vault-1.x", "vault.kv.read", r)
	out := buf.String()
	for _, want := range []string{"status=ok", "vault.kv.read", `"value"`, `"secret"`} {
		if !strings.Contains(out, want) {
			t.Errorf("printCallResult ok missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintCallResultErrorRendersErrorString — status=error with a
// non-nil Error pointer surfaces the error string and (when present)
// the extras envelope.
func TestPrintCallResultErrorRendersErrorString(t *testing.T) {
	errMsg := "unknown_op: vault.bogus"
	r := &CallResult{
		Status:     "error",
		OpID:       "vault.bogus",
		Error:      &errMsg,
		Extras:     json.RawMessage(`{"known_op_count":7}`),
		DurationMs: 5.0,
	}
	var buf bytes.Buffer
	printCallResult(&buf, "vault-1.x", "vault.bogus", r)
	out := buf.String()
	for _, want := range []string{"status=error", "unknown_op: vault.bogus", "extras:", "known_op_count"} {
		if !strings.Contains(out, want) {
			t.Errorf("printCallResult error missing %q in output:\n%s", want, out)
		}
	}
}

// TestPrintCallResultOkNullResult — status=ok with a null result
// renders the header line and stops (no JSON dump). Typed ops
// returning None on the Python side land here.
func TestPrintCallResultOkNullResult(t *testing.T) {
	r := &CallResult{
		Status:     "ok",
		OpID:       "k8s.about",
		Result:     json.RawMessage(`null`),
		DurationMs: 12.0,
	}
	var buf bytes.Buffer
	printCallResult(&buf, "k8s-1.x", "k8s.about", r)
	out := buf.String()
	if !strings.Contains(out, "status=ok") {
		t.Errorf("null-result ok render missing status header; got:\n%s", out)
	}
	// Heuristic: the body should be only the header line, no extra
	// JSON dump.
	if strings.Count(out, "\n") > 1 {
		t.Errorf("null-result ok should produce one line; got:\n%s", out)
	}
}

// TestLoadParamsFlagEmpty — empty flag value returns (nil, nil) so
// runCall can omit the params key from the body.
func TestLoadParamsFlagEmpty(t *testing.T) {
	got, err := loadParamsFlag("")
	if err != nil {
		t.Fatalf("loadParamsFlag(\"\"): %v", err)
	}
	if got != nil {
		t.Fatalf("loadParamsFlag(\"\") should be nil; got %v", got)
	}
}

// TestLoadParamsFlagInlineJSON — happy-path inline JSON object.
func TestLoadParamsFlagInlineJSON(t *testing.T) {
	got, err := loadParamsFlag(`{"path":"secret/foo","key":"v"}`)
	if err != nil {
		t.Fatalf("loadParamsFlag: %v", err)
	}
	if got["path"] != "secret/foo" || got["key"] != "v" {
		t.Fatalf("inline JSON params not parsed; got %v", got)
	}
}

// TestLoadParamsFlagFileReference — `@<file>` form reads + parses.
func TestLoadParamsFlagFileReference(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "params.json")
	if err := os.WriteFile(path, []byte(`{"namespace":"default"}`), 0o644); err != nil {
		t.Fatalf("setup write: %v", err)
	}
	got, err := loadParamsFlag("@" + path)
	if err != nil {
		t.Fatalf("loadParamsFlag @file: %v", err)
	}
	if got["namespace"] != "default" {
		t.Fatalf("file params not parsed; got %v", got)
	}
}

// TestLoadParamsFlagInvalidJSONReportsError — malformed JSON surfaces
// a `parse params JSON` error string the runner can pass to
// output.Unexpected.
func TestLoadParamsFlagInvalidJSONReportsError(t *testing.T) {
	_, err := loadParamsFlag(`{not json`)
	if err == nil {
		t.Fatalf("expected parse error; got nil")
	}
	if !strings.Contains(err.Error(), "parse params JSON") {
		t.Fatalf("error should name parse failure; got %v", err)
	}
}

// TestLoadParamsFlagMissingFileReportsError — `@` prefix on a
// nonexistent path reports a read failure with the path in the
// message (operator-friendly).
func TestLoadParamsFlagMissingFileReportsError(t *testing.T) {
	_, err := loadParamsFlag("@/nonexistent/path/params.json")
	if err == nil {
		t.Fatalf("expected read error; got nil")
	}
	if !strings.Contains(err.Error(), "read params file") {
		t.Fatalf("error should name read failure; got %v", err)
	}
}

// TestNormaliseURLStripsTrailingSlash — same contract as the
// retrieval sibling; trailing-slash trimming is the v0.2 convention.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
}

// TestNormaliseURLRejectsEmpty — empty input returns the "empty"
// error string callers depend on.
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	_, err := backplane.NormaliseURL("   ")
	if err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("expected 'empty' error; got %v", err)
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired; everything else maps
// to Unexpected. Same routing ladder as the retrieval sibling.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrappedNotFound := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrappedNotFound)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("ErrConfigNotFound wrapper should classify as auth_expired; got %+v", se)
	}
	parseFailure := errors.New("invalid backplane URL")
	se = backplane.ClassifyError(parseFailure)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected; got %+v", se)
	}
}

// TestErrOpErrorIsSentinel — verifies the sentinel exists + is
// distinct, so callers (and main) can switch on it. Mirrors the
// retrieval sibling's errEvalGate test.
func TestErrOpErrorIsSentinel(t *testing.T) {
	if errOpError == nil {
		t.Fatalf("errOpError should be a non-nil sentinel")
	}
	if errors.Is(errOpError, errors.New("other")) {
		t.Fatalf("errOpError should not match arbitrary errors")
	}
}

// TestPrettyJSONRoundTrip — ensures pretty-printer indents +
// preserves keys for a representative result envelope.
func TestPrettyJSONRoundTrip(t *testing.T) {
	raw := json.RawMessage(`{"a":1,"b":[2,3]}`)
	got, err := prettyJSON(raw)
	if err != nil {
		t.Fatalf("prettyJSON: %v", err)
	}
	if !strings.Contains(got, "\n") {
		t.Errorf("prettyJSON should include newlines; got %q", got)
	}
	if !strings.Contains(got, `"a"`) || !strings.Contains(got, `"b"`) {
		t.Errorf("prettyJSON should preserve keys; got %q", got)
	}
}

// TestPrettyJSONRejectsInvalid — invalid JSON returns the unmarshal
// error so the caller can fall back to raw-bytes rendering.
func TestPrettyJSONRejectsInvalid(t *testing.T) {
	if _, err := prettyJSON(json.RawMessage(`{not json`)); err == nil {
		t.Fatalf("expected unmarshal error; got nil")
	}
}
