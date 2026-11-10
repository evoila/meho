// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"bytes"
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
