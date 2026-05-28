// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package audit

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// twoLevelReplay returns a small `api.AuditReplayResult` with two
// chronological roots; the first carries one child that itself
// carries a grandchild. Used by the tree-render and max-depth tests
// so they share one fixture and the expected line shapes stay in
// one place.
func twoLevelReplay(t *testing.T) api.AuditReplayResult {
	t.Helper()
	dur120 := "120.5"
	dur40 := "40"
	dur8 := "8"
	grandchild := api.ReplayNode{
		Id:           mustUUID(t, "44444444-4444-4444-4444-444444444444"),
		Ts:           mustTS(t, "2026-05-13T10:00:02Z"),
		PrincipalSub: "damir",
		Method:       "GET",
		Path:         "/x/task",
		StatusCode:   500,
		OpId:         "vsphere.task.wait",
		OpClass:      "read",
		ResultStatus: "error",
		DurationMs:   nil,
		Depth:        2,
	}
	grandKids := []api.ReplayNode{grandchild}
	child := api.ReplayNode{
		Id:           mustUUID(t, "33333333-3333-3333-3333-333333333333"),
		Ts:           mustTS(t, "2026-05-13T10:00:01Z"),
		PrincipalSub: "damir",
		Method:       "POST",
		Path:         "/x/power",
		StatusCode:   200,
		OpId:         "vsphere.vm.power_off",
		OpClass:      "write",
		ResultStatus: "ok",
		DurationMs:   &dur40,
		Depth:        1,
		Children:     &grandKids,
	}
	childKids := []api.ReplayNode{child}
	root1 := api.ReplayNode{
		Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:           mustTS(t, "2026-05-13T10:00:00Z"),
		PrincipalSub: "damir",
		Method:       "POST",
		Path:         "/x/migrate",
		StatusCode:   200,
		OpId:         "vsphere.vm.migrate",
		OpClass:      "write",
		ResultStatus: "ok",
		DurationMs:   &dur120,
		Depth:        0,
		Children:     &childKids,
	}
	root2 := api.ReplayNode{
		Id:           mustUUID(t, "22222222-2222-2222-2222-222222222222"),
		Ts:           mustTS(t, "2026-05-13T10:05:00Z"),
		PrincipalSub: "damir",
		Method:       "GET",
		Path:         "/x/list",
		StatusCode:   200,
		OpId:         "vsphere.vm.list",
		OpClass:      "read",
		ResultStatus: "ok",
		DurationMs:   &dur8,
		Depth:        0,
	}
	return api.AuditReplayResult{
		SessionId: mustUUID(t, "55555555-5555-5555-5555-555555555555"),
		TenantId:  mustUUID(t, "66666666-6666-6666-6666-666666666666"),
		RowCount:  4,
		Root:      []api.ReplayNode{root1, root2},
	}
}

// TestRunReplayRejectsNonUUID — AC: a non-UUID <session-id> is
// rejected client-side with a clear message, before any network
// round-trip. The typed-client path parameter is
// `openapi_types.UUID`; parsing at the verb edge keeps the bad-
// input error a clean output.Unexpected.
func TestRunReplayRejectsNonUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{SessionID: "not-a-uuid"})
	if err == nil {
		t.Fatalf("expected error for non-UUID session-id")
	}
	if !strings.Contains(stderr.String(), "not a UUID") {
		t.Errorf("stderr missing UUID-rejection hint: %s", stderr.String())
	}
}

// TestRunReplayRejectsNegativeMaxDepth — a negative --max-depth is a
// nonsensical render bound; surface it before the round-trip.
func TestRunReplayRejectsNegativeMaxDepth(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID: "11111111-1111-1111-1111-111111111111",
		MaxDepth:  -1,
	})
	if err == nil {
		t.Fatalf("expected error for negative --max-depth")
	}
	if !strings.Contains(stderr.String(), "max-depth must be") {
		t.Errorf("stderr missing max-depth hint: %s", stderr.String())
	}
}

// TestRunReplayHappyPathTree — AC1: a multi-level session renders an
// ASCII tree with chronological roots, indented children, and the
// documented per-line shape `<ts> <op_id> [<status>] (<ms>ms)`.
func TestRunReplayHappyPathTree(t *testing.T) {
	fixture := twoLevelReplay(t)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("method: got %s; want GET", r.Method)
		}
		expected := "/api/v1/audit/sessions/11111111-1111-1111-1111-111111111111/replay"
		if r.URL.Path != expected {
			t.Errorf("path: got %q; want %q", r.URL.Path, expected)
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         "11111111-1111-1111-1111-111111111111",
		MaxDepth:          defaultReplayMaxDepth,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReplay: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()

	// Per-line shape and the null-duration dash rendering.
	for _, want := range []string{
		"2026-05-13T10:00:00Z vsphere.vm.migrate [ok] (120.5ms)",
		"2026-05-13T10:00:01Z vsphere.vm.power_off [ok] (40ms)",
		"2026-05-13T10:00:02Z vsphere.task.wait [error] (-ms)",
		"2026-05-13T10:05:00Z vsphere.vm.list [ok] (8ms)",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("tree missing line %q in:\n%s", want, out)
		}
	}

	// Roots emitted in chronological order: migrate's subtree before
	// the later list root.
	migrateIdx := strings.Index(out, "vsphere.vm.migrate")
	listIdx := strings.Index(out, "vsphere.vm.list")
	if migrateIdx < 0 || listIdx < 0 || migrateIdx > listIdx {
		t.Errorf("roots not chronological (migrate before list):\n%s", out)
	}

	// Children are indented under parents: the grandchild line carries
	// deeper indentation than the child, which is deeper than the root.
	rootLine := lineContaining(t, out, "vsphere.vm.migrate")
	childLine := lineContaining(t, out, "vsphere.vm.power_off")
	grandLine := lineContaining(t, out, "vsphere.task.wait")
	if indentOf(childLine) <= indentOf(rootLine) {
		t.Errorf("child not indented under root:\nroot=%q\nchild=%q", rootLine, childLine)
	}
	if indentOf(grandLine) <= indentOf(childLine) {
		t.Errorf("grandchild not indented under child:\nchild=%q\ngrand=%q", childLine, grandLine)
	}
}

// TestRunReplayJSONVerbatim — AC2: --json emits the raw
// AuditReplayResult JSON; nesting is parseable and matches the tree.
func TestRunReplayJSONVerbatim(t *testing.T) {
	fixture := twoLevelReplay(t)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         "11111111-1111-1111-1111-111111111111",
		JSONOut:           true,
		MaxDepth:          defaultReplayMaxDepth,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReplay --json: %v; stderr=%s", err, stderr.String())
	}
	var decoded api.AuditReplayResult
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("--json output not parseable: %v\n%s", err, stdout.String())
	}
	if len(decoded.Root) != 2 {
		t.Fatalf("--json root count: got %d; want 2", len(decoded.Root))
	}
	// Nesting survives: root[0] → child → grandchild.
	if decoded.Root[0].Children == nil ||
		len(*decoded.Root[0].Children) != 1 ||
		(*decoded.Root[0].Children)[0].Children == nil ||
		len(*(*decoded.Root[0].Children)[0].Children) != 1 {
		t.Errorf("--json nesting lost: %+v", decoded.Root[0])
	}
	if (*(*decoded.Root[0].Children)[0].Children)[0].OpId != "vsphere.task.wait" {
		t.Errorf("--json grandchild op_id wrong: %+v",
			(*(*decoded.Root[0].Children)[0].Children)[0])
	}
	// row_count echoed verbatim.
	if decoded.RowCount != 4 {
		t.Errorf("--json row_count: got %d; want 4", decoded.RowCount)
	}
}

// TestRunReplayMaxDepthTruncates — AC3: --max-depth 1 truncates
// rendering below depth 1. The grandchild (depth 2) must be folded
// into a "more node(s)" marker, not printed as its own line.
func TestRunReplayMaxDepthTruncates(t *testing.T) {
	fixture := twoLevelReplay(t)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fixture)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         "11111111-1111-1111-1111-111111111111",
		MaxDepth:          1,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReplay --max-depth 1: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	// Depth 0 and 1 render; depth 2 is folded.
	if !strings.Contains(out, "vsphere.vm.migrate") {
		t.Errorf("depth-0 root missing: %s", out)
	}
	if !strings.Contains(out, "vsphere.vm.power_off") {
		t.Errorf("depth-1 child missing: %s", out)
	}
	if strings.Contains(out, "vsphere.task.wait") {
		t.Errorf("depth-2 grandchild should be folded at --max-depth 1: %s", out)
	}
	if !strings.Contains(out, "more node(s) below depth 1") {
		t.Errorf("fold marker missing: %s", out)
	}
}

// TestRunReplay413Redirects — AC4: a 413 session_too_large prints the
// `meho audit query --session-id <id>` redirect (with the row count
// and cap) and returns a non-nil error so the process exits non-zero.
func TestRunReplay413Redirects(t *testing.T) {
	sessionID := "11111111-1111-1111-1111-111111111111"
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusRequestEntityTooLarge)
		// FastAPI wraps the route's HTTPException dict under `detail`.
		_, _ = w.Write([]byte(
			`{"detail":{"detail":"session_too_large","row_count":12345}}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         sessionID,
		MaxDepth:          defaultReplayMaxDepth,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected non-nil error on 413 (non-zero exit)")
	}
	got := stderr.String()
	for _, want := range []string{
		"has 12345 rows",
		"cap 10000",
		"meho audit query --session-id " + sessionID,
	} {
		if !strings.Contains(got, want) {
			t.Errorf("413 redirect missing %q in: %s", want, got)
		}
	}
}

// TestRunReplayEmptySession — an unknown / foreign / empty session
// returns root=[] / row_count=0 (never 404). The verb renders a clear
// "no rows" line and exits zero — the same non-leakage posture `show`
// takes.
func TestRunReplayEmptySession(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(
			`{"root":[],"session_id":"11111111-1111-1111-1111-111111111111",` +
				`"tenant_id":"22222222-2222-2222-2222-222222222222","row_count":0}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         "11111111-1111-1111-1111-111111111111",
		MaxDepth:          defaultReplayMaxDepth,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runReplay empty session: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "no audit rows in this session") {
		t.Errorf("empty session missing helper line: %s", stdout.String())
	}
}

// TestRunReplay403SurfacesInsufficientRole — a read_only operator is
// below the audit gate; the backend returns 403 and the verb surfaces
// the shared insufficient_role category.
func TestRunReplay403SurfacesInsufficientRole(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/audit/sessions/", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusForbidden)
		_, _ = w.Write([]byte(`{"detail":"requires operator role"}`))
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runReplay(cmd, replayOptions{
		SessionID:         "11111111-1111-1111-1111-111111111111",
		MaxDepth:          defaultReplayMaxDepth,
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatalf("expected error on 403")
	}
	if !strings.Contains(stderr.String(), "requires operator role") {
		t.Errorf("stderr missing role detail: %s", stderr.String())
	}
}

// TestPrintReplayTreeEmpty — the empty forest renders the helper line
// (covered indirectly above, pinned here directly for the renderer).
func TestPrintReplayTreeEmpty(t *testing.T) {
	var buf bytes.Buffer
	printReplayTree(&buf, &api.AuditReplayResult{Root: []api.ReplayNode{}}, defaultReplayMaxDepth)
	if !strings.Contains(buf.String(), "no audit rows in this session") {
		t.Errorf("empty tree missing helper line: %s", buf.String())
	}
}

// TestFormatReplayNodeNullDuration — a node with a null duration_ms
// renders `(-ms)` so the column stays present and grep-friendly.
func TestFormatReplayNodeNullDuration(t *testing.T) {
	got := formatReplayNode(&api.ReplayNode{
		Id:           mustUUID(t, "11111111-1111-1111-1111-111111111111"),
		Ts:           mustTS(t, "2026-05-13T10:00:00Z"),
		PrincipalSub: "damir",
		Method:       "GET",
		Path:         "/x",
		StatusCode:   200,
		OpId:         "x.y",
		OpClass:      "read",
		ResultStatus: "ok",
		DurationMs:   nil,
	})
	if !strings.Contains(got, "(-ms)") {
		t.Errorf("null duration not rendered as dash: %q", got)
	}
}

// TestCountDescendantsNilChildren pins the helper's nil-children
// arm. The generated `ReplayNode.Children` is `*[]ReplayNode`; a
// nil pointer must round-trip as zero descendants.
func TestCountDescendantsNilChildren(t *testing.T) {
	node := &api.ReplayNode{
		Id: mustUUID(t, "11111111-1111-1111-1111-111111111111"),
	}
	if got := countDescendants(node); got != 0 {
		t.Errorf("nil-children descendants: got %d; want 0", got)
	}
}

// TestDecodeSessionTooLargeRowCount — the 413 helper pulls the nested
// row_count out of FastAPI's double-wrapped detail body, and falls
// back to "?" on an unexpected shape so the redirect stays useful.
func TestDecodeSessionTooLargeRowCount(t *testing.T) {
	body := `{"detail":{"detail":"session_too_large","row_count":98765}}`
	if got := decodeSessionTooLargeRowCount(body); got != "98765" {
		t.Errorf("decodeSessionTooLargeRowCount: got %q; want 98765", got)
	}
	if got := decodeSessionTooLargeRowCount("Service Unavailable"); got != "?" {
		t.Errorf("decodeSessionTooLargeRowCount fallback: got %q; want ?", got)
	}
}

// lineContaining returns the first line of s that contains sub, failing
// the test when none does. Used by the indentation assertions.
func lineContaining(t *testing.T, s, sub string) string {
	t.Helper()
	for _, line := range strings.Split(s, "\n") {
		if strings.Contains(line, sub) {
			return line
		}
	}
	t.Fatalf("no line containing %q in:\n%s", sub, s)
	return ""
}

// indentOf returns the count of leading runes before the `├`/`└` tree
// connector — a proxy for a node's render depth.
func indentOf(line string) int {
	idx := strings.IndexAny(line, "├└")
	if idx < 0 {
		return 0
	}
	return len([]rune(line[:idx]))
}
