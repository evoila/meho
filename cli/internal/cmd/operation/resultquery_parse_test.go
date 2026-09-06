// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package operation

import (
	"reflect"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// These table-driven tests cover the result-query flag parser (#3366,
// PR #3377): the pure functions that shape --where / --select / --group-by
// / --aggregate / --order-by / --query-limit into an *api.ResultQuerySpec
// before the POST. client_test.go already exercises postResultQuery's
// paging transport with spec=nil; here we drive the parse helpers directly.

func TestBuildResultQuerySpecPagingVsQueryMode(t *testing.T) {
	// Each case validates the (*api.ResultQuerySpec, error) return against
	// its own assertion closure — the shapes differ too much for a single
	// expected-struct comparison to read cleanly.
	tests := []struct {
		name   string
		opts   resultQueryOptions
		verify func(t *testing.T, spec *api.ResultQuerySpec, err error)
	}{
		{
			name: "no flags and query-limit unset yields paging mode",
			opts: resultQueryOptions{},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec != nil {
					t.Fatalf("paging mode must return a nil spec, got %+v", spec)
				}
			},
		},
		{
			name: "a where term populates the filter",
			opts: resultQueryOptions{WhereTerms: []string{"state = active"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.Filter == nil {
					t.Fatalf("expected a populated Filter, got %+v", spec)
				}
				if got := len(*spec.Filter); got != 1 {
					t.Fatalf("Filter length = %d, want 1", got)
				}
			},
		},
		{
			name: "select columns populate the projection",
			opts: resultQueryOptions{SelectCols: []string{"name", "id"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.Select == nil {
					t.Fatalf("expected a populated Select, got %+v", spec)
				}
				if !reflect.DeepEqual(*spec.Select, []string{"name", "id"}) {
					t.Fatalf("Select = %v, want [name id]", *spec.Select)
				}
			},
		},
		{
			name: "group-by columns populate the grouping",
			opts: resultQueryOptions{GroupByCols: []string{"power_state"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.GroupBy == nil {
					t.Fatalf("expected a populated GroupBy, got %+v", spec)
				}
				if !reflect.DeepEqual(*spec.GroupBy, []string{"power_state"}) {
					t.Fatalf("GroupBy = %v, want [power_state]", *spec.GroupBy)
				}
			},
		},
		{
			name: "an aggregate term populates the aggregate list",
			opts: resultQueryOptions{AggregateTerms: []string{"COUNT"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.Aggregate == nil {
					t.Fatalf("expected a populated Aggregate, got %+v", spec)
				}
				if got := len(*spec.Aggregate); got != 1 {
					t.Fatalf("Aggregate length = %d, want 1", got)
				}
			},
		},
		{
			name: "an order-by term populates the sort list",
			opts: resultQueryOptions{OrderByTerms: []string{"name asc"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.OrderBy == nil {
					t.Fatalf("expected a populated OrderBy, got %+v", spec)
				}
				if got := len(*spec.OrderBy); got != 1 {
					t.Fatalf("OrderBy length = %d, want 1", got)
				}
			},
		},
		{
			name: "query-limit set to a positive value populates Limit",
			opts: resultQueryOptions{QueryLimit: 25, QueryLimitSet: true},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.Limit == nil {
					t.Fatalf("expected a populated Limit, got %+v", spec)
				}
				if *spec.Limit != 25 {
					t.Fatalf("Limit = %d, want 25", *spec.Limit)
				}
			},
		},
		{
			// The paging-vs-query branch keys off QueryLimitSet, not the
			// value: --query-limit 0 explicitly set is query mode with
			// Limit=0, NOT paging mode.
			name: "query-limit explicitly set to zero is query mode with Limit 0",
			opts: resultQueryOptions{QueryLimit: 0, QueryLimitSet: true},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				if spec == nil || spec.Limit == nil {
					t.Fatalf("query-limit=0 set must produce a non-nil Limit, got %+v", spec)
				}
				if *spec.Limit != 0 {
					t.Fatalf("Limit = %d, want 0", *spec.Limit)
				}
			},
		},
		{
			name: "a bad where sub-term propagates the parse error",
			opts: resultQueryOptions{WhereTerms: []string{"onlyonefield"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err == nil {
					t.Fatalf("expected an error for a malformed where term, got spec %+v", spec)
				}
				if spec != nil {
					t.Fatalf("expected a nil spec on error, got %+v", spec)
				}
			},
		},
		{
			name: "a bad aggregate sub-term propagates the parse error",
			opts: resultQueryOptions{AggregateTerms: []string{"MEDIAN latency"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err == nil {
					t.Fatalf("expected an error for an unsupported aggregate func, got spec %+v", spec)
				}
				if spec != nil {
					t.Fatalf("expected a nil spec on error, got %+v", spec)
				}
			},
		},
		{
			name: "a bad order-by sub-term propagates the parse error",
			opts: resultQueryOptions{OrderByTerms: []string{"name sideways"}},
			verify: func(t *testing.T, spec *api.ResultQuerySpec, err error) {
				if err == nil {
					t.Fatalf("expected an error for a bad order-by direction, got spec %+v", spec)
				}
				if spec != nil {
					t.Fatalf("expected a nil spec on error, got %+v", spec)
				}
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			spec, err := buildResultQuerySpec(tc.opts)
			tc.verify(t, spec, err)
		})
	}
}

func TestParseWherePredicateHappyPaths(t *testing.T) {
	// wantValue is compared with reflect.DeepEqual, which treats a nil
	// interface (IS NULL leaves Value unset) as equal to a nil want.
	tests := []struct {
		name      string
		raw       string
		wantField string
		wantOp    api.FilterPredicateOp
		wantValue any
	}{
		{"equals with a bare-word string", "state = active", "state", api.FilterPredicateOpEqual, "active"},
		{"equals with a JSON number binds float64", "id = 5", "id", api.FilterPredicateOpEqual, float64(5)},
		{"equals with a JSON bool binds bool", "ok = true", "ok", api.FilterPredicateOpEqual, true},
		{"not-equals maps to the Empty op token", "age != 5", "age", api.FilterPredicateOpEmpty, float64(5)},
		{"less than", "age < 5", "age", api.FilterPredicateOpLessThan, float64(5)},
		{"less than or equal", "age <= 5", "age", api.FilterPredicateOpLessThanEqual, float64(5)},
		{"greater than", "age > 5", "age", api.FilterPredicateOpGreaterThan, float64(5)},
		{"greater than or equal", "age >= 5", "age", api.FilterPredicateOpGreaterThanEqual, float64(5)},
		{"IN splits a comma list into elements", "tags IN a,b,c", "tags", api.FilterPredicateOpIN, []any{"a", "b", "c"}},
		{"IS NULL uppercase takes no value", "status IS NULL", "status", api.FilterPredicateOpISNULL, nil},
		{"IS NULL is case-insensitive", "status is null", "status", api.FilterPredicateOpISNULL, nil},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			pred, err := parseWherePredicate(tc.raw)
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if pred.Field != tc.wantField {
				t.Errorf("Field = %q, want %q", pred.Field, tc.wantField)
			}
			if pred.Op != tc.wantOp {
				t.Errorf("Op = %q, want %q", pred.Op, tc.wantOp)
			}
			if !reflect.DeepEqual(pred.Value, tc.wantValue) {
				t.Errorf("Value = %#v, want %#v", pred.Value, tc.wantValue)
			}
		})
	}
}

func TestParseWherePredicateErrors(t *testing.T) {
	tests := []struct {
		name string
		raw  string
	}{
		{"single field is too few tokens", "onlyfield"},
		{"field and op without a value is too few tokens", "field ="},
		{"unknown operator", "field ?? value"},
		{"empty field before IS NULL", "IS NULL"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := parseWherePredicate(tc.raw); err == nil {
				t.Fatalf("parseWherePredicate(%q) = nil error, want an error", tc.raw)
			}
		})
	}
}

func TestParseAggregate(t *testing.T) {
	tests := []struct {
		name      string
		raw       string
		wantFunc  api.AggregateFunc
		wantField *string
		wantErr   bool
	}{
		{"COUNT without a field compiles to COUNT(*)", "COUNT", api.AggregateFuncCOUNT, nil, false},
		{"COUNT with a field", "COUNT id", api.AggregateFuncCOUNT, strptr("id"), false},
		{"SUM with a field", "SUM bytes", api.AggregateFuncSUM, strptr("bytes"), false},
		{"MIN with a field", "MIN age", api.AggregateFuncMIN, strptr("age"), false},
		{"MAX with a field", "MAX age", api.AggregateFuncMAX, strptr("age"), false},
		{"AVG with a field", "AVG latency", api.AggregateFuncAVG, strptr("latency"), false},
		{"lowercase func is upcased", "count", api.AggregateFuncCOUNT, nil, false},
		{"unsupported function errors", "MEDIAN latency", "", nil, true},
		{"empty input errors", "", "", nil, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			agg, err := parseAggregate(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseAggregate(%q) = nil error, want an error", tc.raw)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if agg.Func != tc.wantFunc {
				t.Errorf("Func = %q, want %q", agg.Func, tc.wantFunc)
			}
			if !reflect.DeepEqual(agg.Field, tc.wantField) {
				t.Errorf("Field = %v, want %v", derefStr(agg.Field), derefStr(tc.wantField))
			}
		})
	}
}

func TestParseOrderBy(t *testing.T) {
	asc := api.OrderByDirectionAsc
	desc := api.OrderByDirectionDesc
	tests := []struct {
		name      string
		raw       string
		wantField string
		wantDir   *api.OrderByDirection
		wantErr   bool
	}{
		{"explicit asc", "name asc", "name", &asc, false},
		{"explicit desc", "created desc", "created", &desc, false},
		{"asc is case-insensitive", "name ASC", "name", &asc, false},
		{"desc is case-insensitive", "name DESC", "name", &desc, false},
		{"no direction defaults to unset", "name", "name", nil, false},
		{"bad direction errors", "name sideways", "", nil, true},
		{"empty input errors", "", "", nil, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			term, err := parseOrderBy(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("parseOrderBy(%q) = nil error, want an error", tc.raw)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if term.Field != tc.wantField {
				t.Errorf("Field = %q, want %q", term.Field, tc.wantField)
			}
			if !reflect.DeepEqual(term.Direction, tc.wantDir) {
				t.Errorf("Direction = %v, want %v", term.Direction, tc.wantDir)
			}
		})
	}
}

func strptr(s string) *string { return &s }

func derefStr(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}
