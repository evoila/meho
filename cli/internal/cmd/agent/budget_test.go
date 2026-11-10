// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func ptrInt(v int) *int           { return &v }
func ptrFloat(v float32) *float32 { return &v }

func TestBuildBudgetSetBodyHappy(t *testing.T) {
	body, err := buildBudgetSetBody("agent:pod-reader", "daily", nil, ptrFloat(5), ptrInt(200))
	if err != nil {
		t.Fatalf("buildBudgetSetBody: %v", err)
	}
	if body.Window != api.BudgetWindowKindDaily || body.PrincipalSub != "agent:pod-reader" {
		t.Errorf("unexpected body: %+v", body)
	}
	if body.TokenLimit != nil || *body.CostLimit != 5 || *body.RequestLimit != 200 {
		t.Errorf("limits not carried: %+v", body)
	}
}

func TestBuildBudgetSetBodyKeepsZeroLimit(t *testing.T) {
	body, err := buildBudgetSetBody("agent:a", "weekly", nil, nil, ptrInt(0))
	if err != nil {
		t.Fatalf("buildBudgetSetBody: %v", err)
	}
	if body.RequestLimit == nil || *body.RequestLimit != 0 {
		t.Errorf("a limit of 0 must be sent, got %+v", body.RequestLimit)
	}
}

func TestBuildBudgetSetBodyRefusesBadInput(t *testing.T) {
	cases := []struct {
		name   string
		window string
		tokens *int
		cost   *float32
		reqs   *int
	}{
		{"no limit", "daily", nil, nil, nil},
		{"bad window", "hourly", nil, nil, ptrInt(1)},
		{"empty window", "", nil, nil, ptrInt(1)},
		{"negative request limit", "daily", nil, nil, ptrInt(-1)},
		{"negative cost limit", "daily", nil, ptrFloat(-0.5), nil},
		{"negative token limit", "daily", ptrInt(-5), nil, nil},
	}
	for _, c := range cases {
		if _, err := buildBudgetSetBody("agent:a", c.window, c.tokens, c.cost, c.reqs); err == nil {
			t.Errorf("%s: expected an error", c.name)
		}
	}
}

func TestPrintBudgetTableShowsNoCap(t *testing.T) {
	limit := 1
	var buf bytes.Buffer
	printBudgetTable(&buf, []api.AgentBudgetRead{
		{Window: api.BudgetWindowKindDaily, RequestsConsumed: 1, RequestLimit: &limit},
		{Window: api.BudgetWindowKindMonthly},
	})
	out := buf.String()
	if !strings.Contains(out, "1 / 1") || !strings.Contains(out, "0 / no cap") {
		t.Errorf("table missing used/cap text:\n%s", out)
	}
}

// budgetServer fakes /api/v1/agent-budgets. It records the method, query and
// body of the last call and answers with one daily entry.
func budgetServer(t *testing.T, status int, seen *http.Request, seenBody *[]byte) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agent-budgets", func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") == "" {
			t.Errorf("missing Authorization header")
		}
		*seen = *r.Clone(r.Context())
		*seenBody, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		if status != http.StatusOK {
			w.WriteHeader(status)
			fmt.Fprint(w, `{"detail":"insufficient_role"}`)
			return
		}
		limit := 200
		entry := api.AgentBudgetRead{
			PrincipalSub: "agent:pod-reader", Window: api.BudgetWindowKindDaily,
			RequestLimit: &limit, RequestsConsumed: 3,
		}
		if r.Method == http.MethodGet {
			_ = json.NewEncoder(w).Encode(api.AgentBudgetListResponse{Budgets: []api.AgentBudgetRead{entry}})
			return
		}
		_ = json.NewEncoder(w).Encode(entry)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	seedXDGAndToken(t, srv.URL)
	return srv
}

func runBudgetCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd := NewBudgetRootCmd()
	var stdout, stderr bytes.Buffer
	cmd.SetOut(&stdout)
	cmd.SetErr(&stderr)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return stdout.String(), stderr.String(), err
}

func TestBudgetSetCommandSendsTheLimits(t *testing.T) {
	var seen http.Request
	var body []byte
	srv := budgetServer(t, http.StatusOK, &seen, &body)

	out, stderr, err := runBudgetCmd(t, "set", "agent:pod-reader", "--window", "daily",
		"--cost-limit", "5", "--request-limit", "200", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("set: %v; stderr=%s", err, stderr)
	}
	if seen.Method != http.MethodPut {
		t.Errorf("method = %s, want PUT", seen.Method)
	}
	var sent map[string]any
	if err := json.Unmarshal(body, &sent); err != nil {
		t.Fatalf("body is not JSON: %s", body)
	}
	if sent["principal_sub"] != "agent:pod-reader" || sent["window"] != "daily" ||
		sent["cost_limit"] != 5.0 || sent["request_limit"] != 200.0 {
		t.Errorf("unexpected body: %s", body)
	}
	// null means "keep the current limit" on the server, the same as left out.
	if sent["token_limit"] != nil {
		t.Errorf("a limit that was not given must not be set: %s", body)
	}
	if !strings.Contains(out, "set daily cap for agent:pod-reader") || !strings.Contains(out, "3 / 200") {
		t.Errorf("unexpected output:\n%s", out)
	}
}

func TestBudgetShowCommandAsksForThePrincipal(t *testing.T) {
	var seen http.Request
	var body []byte
	srv := budgetServer(t, http.StatusOK, &seen, &body)

	out, stderr, err := runBudgetCmd(t, "show", "agent:pod-reader", "--window", "daily", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("show: %v; stderr=%s", err, stderr)
	}
	q := seen.URL.Query()
	if seen.Method != http.MethodGet || q.Get("principal_sub") != "agent:pod-reader" || q.Get("window") != "daily" {
		t.Errorf("unexpected request: %s %s", seen.Method, seen.URL)
	}
	if !strings.Contains(out, "WINDOW") || !strings.Contains(out, "3 / 200") {
		t.Errorf("unexpected output:\n%s", out)
	}
}

func TestBudgetClearCommandSendsTheWindow(t *testing.T) {
	var seen http.Request
	var body []byte
	srv := budgetServer(t, http.StatusOK, &seen, &body)

	out, stderr, err := runBudgetCmd(t, "clear", "agent:pod-reader", "--window", "daily", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("clear: %v; stderr=%s", err, stderr)
	}
	q := seen.URL.Query()
	if seen.Method != http.MethodDelete || q.Get("principal_sub") != "agent:pod-reader" || q.Get("window") != "daily" {
		t.Errorf("unexpected request: %s %s", seen.Method, seen.URL)
	}
	if !strings.Contains(out, "cleared daily cap for agent:pod-reader") {
		t.Errorf("unexpected output:\n%s", out)
	}
}

func TestBudgetCommandsShowTheServerRefusal(t *testing.T) {
	var seen http.Request
	var body []byte
	srv := budgetServer(t, http.StatusForbidden, &seen, &body)

	_, stderr, err := runBudgetCmd(t, "set", "agent:pod-reader", "--window", "daily",
		"--request-limit", "1", "--backplane", srv.URL)
	if err == nil {
		t.Fatalf("expected an error on 403")
	}
	if !strings.Contains(stderr, "insufficient_role") {
		t.Errorf("stderr missing insufficient_role: %q", stderr)
	}
}

func TestBudgetSetCommandRefusesWithoutLimitBeforeCallingTheServer(t *testing.T) {
	var seen http.Request
	var body []byte
	srv := budgetServer(t, http.StatusOK, &seen, &body)

	if _, _, err := runBudgetCmd(t, "set", "agent:pod-reader", "--window", "daily", "--backplane", srv.URL); err == nil {
		t.Fatalf("expected an error without a limit")
	}
	if seen.Method != "" {
		t.Errorf("the server must not be called, got %s", seen.Method)
	}
}
