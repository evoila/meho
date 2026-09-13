// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package tenants

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

// TestBuildBodySparseTriState is the load-bearing CLI test: the PATCH body must
// carry ONLY the fields the operator set (absent = leave unchanged), and an
// inherit / clear must serialize to an explicit JSON null (clear), never be
// dropped. This is the null-vs-absent distinction the generated struct (no
// omitempty on its pointers) would get wrong.
func TestBuildBodySparseTriState(t *testing.T) {
	tests := []struct {
		name     string
		opts     setOptions
		wantJSON string
	}{
		{
			name:     "enabled only omits the untouched fields",
			opts:     setOptions{enabled: true, enabledSet: true},
			wantJSON: `{"flight_recorder_enabled":true}`,
		},
		{
			name:     "agent-readable inherit clears to explicit null",
			opts:     setOptions{agentReadable: "inherit", agentReadableSet: true},
			wantJSON: `{"flight_recorder_agent_readable":null}`,
		},
		{
			name:     "agent-readable false forces off",
			opts:     setOptions{agentReadable: "false", agentReadableSet: true},
			wantJSON: `{"flight_recorder_agent_readable":false}`,
		},
		{
			name:     "retention set",
			opts:     setOptions{retentionDays: 14, retentionDaysSet: true},
			wantJSON: `{"flight_recorder_retention_days":14}`,
		},
		{
			name:     "clear-retention clears to explicit null",
			opts:     setOptions{clearRetention: true},
			wantJSON: `{"flight_recorder_retention_days":null}`,
		},
		{
			name: "all three at once",
			opts: setOptions{
				enabled: true, enabledSet: true,
				agentReadable: "true", agentReadableSet: true,
				retentionDays: 7, retentionDaysSet: true,
			},
			wantJSON: `{"flight_recorder_agent_readable":true,"flight_recorder_enabled":true,` +
				`"flight_recorder_retention_days":7}`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			body, err := buildBody(tc.opts)
			if err != nil {
				t.Fatalf("buildBody: unexpected error: %v", err)
			}
			got, err := json.Marshal(body)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			if string(got) != tc.wantJSON {
				t.Fatalf("body JSON = %s, want %s", got, tc.wantJSON)
			}
		})
	}
}

func TestPrintPolicyReadSummaryShowsEffectiveAndRawValues(t *testing.T) {
	retention := 14
	var buf bytes.Buffer
	printPolicyReadSummary(&buf, &api.TenantFlightRecorderPolicyRead{
		Effective: api.EffectiveTenantFlightRecorderPolicy{
			TenantId:                    uuid.MustParse("11111111-1111-1111-1111-111111111111"),
			FlightRecorderEnabled:       true,
			FlightRecorderAgentReadable: true,
			FlightRecorderRetentionDays: 7,
		},
		Raw: api.TenantFlightRecorderPolicy{
			TenantId:                    uuid.MustParse("11111111-1111-1111-1111-111111111111"),
			FlightRecorderEnabled:       true,
			FlightRecorderRetentionDays: &retention,
		},
	})
	out := buf.String()
	for _, want := range []string{
		"effective flight-recorder policy", "agent-readable:  true", "retention:       7 days",
		"raw tenant values", "inherit (follows capture default)", "14 days",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("policy read summary missing %q in %q", want, out)
		}
	}
}

func TestBuildBodyRejects(t *testing.T) {
	tests := []struct {
		name string
		opts setOptions
	}{
		{"nothing set", setOptions{}},
		{
			"agent-readable invalid",
			setOptions{agentReadable: "maybe", agentReadableSet: true},
		},
		{
			"retention out of bounds low",
			setOptions{retentionDays: 0, retentionDaysSet: true},
		},
		{
			"retention out of bounds high",
			setOptions{retentionDays: 366, retentionDaysSet: true},
		},
		{
			"retention + clear contradictory",
			setOptions{retentionDays: 14, retentionDaysSet: true, clearRetention: true},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildBody(tc.opts); err == nil {
				t.Fatalf("buildBody(%+v): expected an error, got nil", tc.opts)
			}
		})
	}
}
