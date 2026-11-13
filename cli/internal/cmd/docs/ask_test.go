// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package docs

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/output"
)

const askTestToken = "eyJ.secret.askToken"

// askServer serves /api/v1/ask_docs with the given status, headers and body
// and returns the server plus a pointer to the last decoded request body.
func askServer(t *testing.T, status int, headers map[string]string, body string) (*httptest.Server, *map[string]any) {
	t.Helper()
	got := map[string]any{}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/ask_docs", func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(raw, &got)
		w.Header().Set("Content-Type", "application/json")
		for k, v := range headers {
			w.Header().Set(k, v)
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(body))
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL, askTestToken)
	return srv, &got
}

const askOKBody = `{"answer":"Release 1 adds a feature [1].\nSecond line [2].",` +
	`"citations":[` +
	`{"chunk_id":"c1","title":"Release notes","source_url":"https://docs.example/rn",` +
	`"link":{"href":"https://docs.example/rn","label":"docs.example","kind":"web","clickable":true}},` +
	`{"chunk_id":"c2","title":null,"source_url":"gs://bucket/x",` +
	`"link":{"href":null,"label":"Guide x","kind":"other","clickable":false}}],` +
	`"answer_source":"upstream"}`

func TestRunAskRejectsBadArguments(t *testing.T) {
	cases := map[string]askOptions{
		"empty question":  {Question: "", Collections: []string{"vmware"}},
		"blank question":  {Question: "  ", Collections: []string{"vmware"}},
		"no collection":   {Question: "q"},
		"two collections": {Question: "q", Collections: []string{"a", "b"}},
		"all":             {Question: "q", Collections: []string{"all"}},
		"limit zero":      {Question: "q", Collections: []string{"a"}, Limit: 0, Changed: true},
		"limit 51":        {Question: "q", Collections: []string{"a"}, Limit: 51, Changed: true},
	}
	for name, opts := range cases {
		t.Run(name, func(t *testing.T) {
			cmd, _, stderr := newRunCmd(t)
			opts.BackplaneOverride = "http://127.0.0.1:1" // a call would fail with exit 3
			err := runAsk(cmd, opts)
			if got := exitCodeOf(t, err); got != output.ExitUnexpected {
				t.Errorf("expected exit %d; got %d (stderr=%q)", output.ExitUnexpected, got, stderr.String())
			}
		})
	}
}

func TestRunAskHappyPath(t *testing.T) {
	srv, got := askServer(t, http.StatusOK, nil, askOKBody)
	cmd, stdout, stderr := newRunCmd(t)
	err := runAsk(cmd, askOptions{
		Question: "what is new?", Collections: []string{"vmware"}, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	if (*got)["collection"] != "vmware" || (*got)["query"] != "what is new?" {
		t.Errorf("unexpected wire body: %v", *got)
	}
	want := "Answer (source: upstream)\n" +
		"Release 1 adds a feature [1].\nSecond line [2].\n\n" +
		"Sources\n" +
		"[1] Release notes  https://docs.example/rn\n" +
		"[2] Guide x\n"
	if stdout.String() != want {
		t.Errorf("stdout mismatch.\ngot:  %q\nwant: %q", stdout.String(), want)
	}
	assertNoToken(t, stdout.String(), stderr.String())
}

func TestRunAskOmitsAbsentRefinements(t *testing.T) {
	srv, got := askServer(t, http.StatusOK, nil, askOKBody)
	cmd, _, stderr := newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	for _, key := range []string{"product", "version", "limit", "target"} {
		if v, ok := (*got)[key]; ok {
			t.Errorf("expected key %s absent from the wire body; got %v", key, v)
		}
	}
}

func TestRunAskSendsRefinementsWhenSet(t *testing.T) {
	srv, got := askServer(t, http.StatusOK, nil, askOKBody)
	cmd, _, stderr := newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, Product: "nsx", Version: "9.0",
		Limit: 5, Changed: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	if (*got)["product"] != "nsx" || (*got)["version"] != "9.0" || (*got)["limit"] != float64(5) {
		t.Errorf("unexpected wire body: %v", *got)
	}
}

func TestRunAskJSONShape(t *testing.T) {
	srv, _ := askServer(t, http.StatusOK, nil, askOKBody)
	cmd, stdout, stderr := newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, JSONOut: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	var out map[string]any
	readJSONBodyOf(t, stdout.Bytes(), &out)
	if out["answer_source"] != "upstream" {
		t.Errorf("answer_source = %v", out["answer_source"])
	}
	if !strings.HasPrefix(out["answer"].(string), "Release 1") {
		t.Errorf("answer = %v", out["answer"])
	}
	cites, ok := out["citations"].([]any)
	if !ok || len(cites) != 2 {
		t.Fatalf("citations = %v", out["citations"])
	}
	assertNoToken(t, stdout.String(), stderr.String())
}

func TestRunAskNoGroundedAnswerExitsZero(t *testing.T) {
	srv, _ := askServer(t, http.StatusOK, nil,
		`{"answer":"No grounded answer: nothing found.","citations":[],"answer_source":"local"}`)
	cmd, stdout, stderr := newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	if !strings.Contains(stdout.String(), "No grounded answer") || strings.Contains(stdout.String(), "Sources") {
		t.Errorf("unexpected stdout %q", stdout.String())
	}
}

func TestRunAskSanitizesControlCharacters(t *testing.T) {
	body := `{"answer":"line one\u001b[31mred\nline two","citations":[` +
		`{"chunk_id":"c1","title":"T\u001b[2Jitle","link":{"href":"https://x/\u001b]0;pwn\u0007","label":"l"}}],` +
		`"answer_source":"local"}`
	srv, _ := askServer(t, http.StatusOK, nil, body)

	cmd, stdout, stderr := newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	if strings.ContainsRune(out, 0x1b) || strings.ContainsRune(out, 0x07) {
		t.Errorf("control character reached the text output: %q", out)
	}
	if !strings.Contains(out, "line one�[31mred\nline two") {
		t.Errorf("answer line break lost or text altered: %q", out)
	}

	cmd, stdout, _ = newRunCmd(t)
	if err := runAsk(cmd, askOptions{
		Question: "q", Collections: []string{"vmware"}, JSONOut: true, BackplaneOverride: srv.URL,
	}); err != nil {
		t.Fatalf("runAsk json: %v", err)
	}
	var parsed struct {
		Answer string `json:"answer"`
	}
	readJSONBodyOf(t, stdout.Bytes(), &parsed)
	if parsed.Answer != "line one\x1b[31mred\nline two" {
		t.Errorf("--json changed the answer: %q", parsed.Answer)
	}
}

func TestRunAskErrorMapping(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		headers map[string]string
		body    string
		exit    int
		want    []string
	}{
		{"401", 401, nil, `{"detail":"no"}`, output.ExitAuthExpired, []string{"meho login"}},
		{"403 not_entitled", 403, nil,
			`{"detail":{"error":"not_entitled","collection":"vmware","message":"missing right"}}`,
			output.ExitInsufficientRole, []string{"not_entitled"}},
		{"403 disabled", 403, nil,
			`{"detail":{"error":"collection_disabled","collection":"vmware","retryable":false}}`,
			output.ExitUnexpected, []string{"collection is disabled"}},
		{"409", 409, nil, `{"detail":"collection is not ready"}`,
			output.ExitUnexpected, []string{"not ready"}},
		{"422", 422, nil, `{"detail":"bad scope"}`,
			output.ExitUnexpected, []string{"invalid request"}},
		{"502 upstream_rejected", 502, nil,
			`{"detail":{"detail":"x","leg":"corpus_unavailable","cause":"upstream_rejected","message":"backend refused","upstream_status":400}}`,
			output.ExitUnexpected, []string{"corpus_unavailable", "upstream_rejected", "backend refused", "HTTP 400"}},
		{"503 retry-after", 503, map[string]string{"Retry-After": "30"},
			`{"detail":{"detail":"x","leg":"model_unavailable","cause":"upstream_rate_limited","message":"slow down","retry_after":30}}`,
			output.ExitUnexpected, []string{"model_unavailable", "try again in 30 seconds"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv, _ := askServer(t, tc.status, tc.headers, tc.body)
			cmd, stdout, stderr := newRunCmd(t)
			err := runAsk(cmd, askOptions{
				Question: "q", Collections: []string{"vmware"}, BackplaneOverride: srv.URL,
			})
			if got := exitCodeOf(t, err); got != tc.exit {
				t.Errorf("exit = %d; want %d (stderr=%q)", got, tc.exit, stderr.String())
			}
			for _, w := range tc.want {
				if !strings.Contains(stderr.String(), w) {
					t.Errorf("stderr missing %q: %q", w, stderr.String())
				}
			}
			if strings.Contains(stderr.String(), `{"detail"`) && tc.status >= 502 {
				t.Errorf("raw JSON leaked into the error: %q", stderr.String())
			}
			assertNoToken(t, stdout.String(), stderr.String())
		})
	}
}

func TestAskCmdRequiresExactlyOneArg(t *testing.T) {
	cmd := newAskCmd()
	cmd.SetArgs([]string{})
	cmd.SetOut(io.Discard)
	cmd.SetErr(io.Discard)
	if err := cmd.Execute(); err == nil {
		t.Fatal("expected an error without a question")
	}
}

func assertNoToken(t *testing.T, outputs ...string) {
	t.Helper()
	for _, o := range outputs {
		if strings.Contains(o, askTestToken) || strings.Contains(o, "Bearer") {
			t.Errorf("token leaked into output: %q", o)
		}
	}
}
