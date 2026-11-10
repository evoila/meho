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

func TestBuildModelPriceBody(t *testing.T) {
	tests := []struct {
		name     string
		model    string
		price    *modelPricesOptions
		wantJSON string
	}{
		{
			name:     "set sends both prices as strings",
			model:    "openai:my-model",
			price:    &modelPricesOptions{input: "0.40", output: "1.60"},
			wantJSON: `{"model":"openai:my-model","price":{"input_per_million":"0.40","output_per_million":"1.60"}}`,
		},
		{
			name:     "remove sends a null price",
			model:    "my-model",
			wantJSON: `{"model":"my-model","price":null}`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			body, err := buildModelPriceBody(tc.model, tc.price)
			if err != nil {
				t.Fatalf("buildModelPriceBody: %v", err)
			}
			got, err := json.Marshal(body)
			if err != nil {
				t.Fatalf("marshal: %v", err)
			}
			if string(got) != tc.wantJSON {
				t.Fatalf("body = %s, want %s", got, tc.wantJSON)
			}
		})
	}
}

func TestBuildModelPriceBodyRejects(t *testing.T) {
	tests := []struct {
		name  string
		model string
		price *modelPricesOptions
	}{
		{"empty model", "", nil},
		{"missing output", "m", &modelPricesOptions{input: "1"}},
		{"missing input", "m", &modelPricesOptions{output: "1"}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := buildModelPriceBody(tc.model, tc.price); err == nil {
				t.Fatalf("expected an error")
			}
		})
	}
}

func TestPrintModelPrices(t *testing.T) {
	tenant := uuid.MustParse("11111111-1111-1111-1111-111111111111")
	var buf bytes.Buffer
	printModelPrices(&buf, &api.TenantModelPrices{
		TenantId: tenant,
		Prices: []api.ModelPriceEntry{
			{Model: "openai:my-model", InputPerMillion: "0.40", OutputPerMillion: "1.60"},
		},
	})
	for _, want := range []string{"MODEL", "openai:my-model", "0.40", "1.60"} {
		if !strings.Contains(buf.String(), want) {
			t.Errorf("table missing %q in %q", want, buf.String())
		}
	}

	buf.Reset()
	printModelPrices(&buf, &api.TenantModelPrices{TenantId: tenant})
	if !strings.Contains(buf.String(), "no model prices set") {
		t.Errorf("empty table = %q", buf.String())
	}
}
