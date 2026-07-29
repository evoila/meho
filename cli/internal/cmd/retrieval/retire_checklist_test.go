// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	openapi_types "github.com/oapi-codegen/runtime/types"

	"github.com/evoila/meho/cli/internal/api"
)

// mustParseUUID parses a canonical UUID string into the
// openapi-runtime UUID wrapper, failing the test loudly on malformed
// input. Used by table-render tests that need to seed a non-nil
// `TenantId` pointer field on the generated `api.RetireChecklistReport`.
func mustParseUUID(t *testing.T, s string) openapi_types.UUID {
	t.Helper()
	var u openapi_types.UUID
	if err := u.UnmarshalText([]byte(s)); err != nil {
		t.Fatalf("parse UUID %q: %v", s, err)
	}
	return u
}

// TestSurfacesFromLabelsKnowledgeBucketsKb — a `knowledge` label
// buckets to the kb surface per T7's runbook scheme.
func TestSurfacesFromLabelsKnowledgeBucketsKb(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "knowledge"},
	})
	if len(got) != 1 || got[0] != "kb" {
		t.Fatalf("expected [kb]; got %v", got)
	}
}

// TestSurfacesFromLabelsConnectorBucketsOperations — a `connector`
// label buckets to the operations surface per T7's runbook scheme.
func TestSurfacesFromLabelsConnectorBucketsOperations(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "connector"},
	})
	if len(got) != 1 || got[0] != "operations" {
		t.Fatalf("expected [operations]; got %v", got)
	}
}

// TestSurfacesFromLabelsMemoryBucketsMemory — a `memory` label
// buckets to the memory surface.
func TestSurfacesFromLabelsMemoryBucketsMemory(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "memory"},
	})
	if len(got) != 1 || got[0] != "memory" {
		t.Fatalf("expected [memory]; got %v", got)
	}
}

// TestSurfacesFromLabelsMultipleSurfaces — an issue spanning multiple
// surfaces (e.g. a connector blocker that also touches kb docs)
// buckets against each surface exactly once.
func TestSurfacesFromLabelsMultipleSurfaces(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "knowledge"},
		{Name: "memory"},
	})
	if len(got) != 2 {
		t.Fatalf("expected 2 surfaces; got %v", got)
	}
	want := map[string]bool{"kb": true, "memory": true}
	for _, surface := range got {
		if !want[surface] {
			t.Fatalf("unexpected surface %q in %v", surface, got)
		}
	}
}

// TestSurfacesFromLabelsDuplicateLabelDoesNotDouble — a label seen
// twice on the same issue (rare but possible during label-migration
// PRs) only buckets the surface once.
func TestSurfacesFromLabelsDuplicateLabelDoesNotDouble(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "knowledge"},
		{Name: "knowledge"},
	})
	if len(got) != 1 || got[0] != "kb" {
		t.Fatalf("expected single [kb] bucket; got %v", got)
	}
}

// TestSurfacesFromLabelsNoMarker — an issue without any surface marker
// returns the empty slice (caller treats as generic blocker).
func TestSurfacesFromLabelsNoMarker(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "priority:high"},
	})
	if len(got) != 0 {
		t.Fatalf("expected empty slice for marker-less issue; got %v", got)
	}
}

// TestSurfacesFromLabelsIgnoresUnknownLabel — a label outside the
// known surface scheme doesn't crash and doesn't bucket anywhere.
func TestSurfacesFromLabelsIgnoresUnknownLabel(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "needs-design"},
	})
	if len(got) != 0 {
		t.Fatalf("expected empty slice for unknown label; got %v", got)
	}
}

// TestPrintRetireTableRendersOverallAndPerSurface — the human table
// includes the overall verdict, each surface verdict, and each
// criterion line.
func TestPrintRetireTableRendersOverallAndPerSurface(t *testing.T) {
	tenantUUID := mustParseUUID(t, "11111111-1111-1111-1111-111111111111")
	report := &api.RetireChecklistReport{
		RanAt:          time.Date(2026, 5, 14, 12, 0, 0, 0, time.UTC),
		TenantId:       &tenantUUID,
		Since:          time.Date(2026, 2, 13, 12, 0, 0, 0, time.UTC),
		Until:          time.Date(2026, 5, 14, 12, 0, 0, 0, time.UTC),
		OverallVerdict: api.RetireChecklistReportOverallVerdictREADYTORETIRE,
		Surfaces: []api.SurfaceChecklist{
			{
				Surface: api.SurfaceChecklistSurface("kb"),
				Verdict: api.SurfaceChecklistVerdictREADYTORETIRE,
				Criteria: []api.CriterionResult{
					{
						Name:             api.CriterionResultNameDailyUseDuration,
						Verdict:          api.CriterionResultVerdictGreen,
						ObservedValue:    "40 days since first use",
						ThresholdSummary: ">= 30 days",
					},
					{
						Name:             api.CriterionResultNameOpenBlockers,
						Verdict:          api.CriterionResultVerdictGreen,
						ObservedValue:    "0 open",
						ThresholdSummary: "== 0 open blockers",
					},
				},
			},
		},
	}
	var buf bytes.Buffer
	printRetireTable(&buf, report)
	out := buf.String()
	if !strings.Contains(out, "overall: READY TO RETIRE") {
		t.Fatalf("missing overall verdict: %s", out)
	}
	if !strings.Contains(out, "kb — READY TO RETIRE") {
		t.Fatalf("missing surface verdict: %s", out)
	}
	if !strings.Contains(out, "daily_use_duration") {
		t.Fatalf("missing criterion line: %s", out)
	}
	if !strings.Contains(out, "tenant: 11111111-1111-1111-1111-111111111111") {
		t.Fatalf("missing tenant id: %s", out)
	}
}

// TestPrintRetireTableHandlesNotesAndNilTenant — notes render in
// parentheses; a nil tenant id is omitted entirely.
func TestPrintRetireTableHandlesNotesAndNilTenant(t *testing.T) {
	noteCriterion := "no baseline corpus configured for this surface in v0.2"
	report := &api.RetireChecklistReport{
		RanAt:          time.Date(2026, 5, 14, 12, 0, 0, 0, time.UTC),
		OverallVerdict: api.RetireChecklistReportOverallVerdictREVIEWMANUALLY,
		Surfaces: []api.SurfaceChecklist{
			{
				Surface: api.SurfaceChecklistSurface("memory"),
				Verdict: api.SurfaceChecklistVerdictREVIEWMANUALLY,
				Criteria: []api.CriterionResult{
					{
						Name:             api.CriterionResultNameMehoVsBaseline,
						Verdict:          api.CriterionResultVerdictYellow,
						ObservedValue:    "baseline did not run",
						ThresholdSummary: "every metric >= baseline",
						Notes:            &noteCriterion,
					},
				},
			},
		},
	}
	var buf bytes.Buffer
	printRetireTable(&buf, report)
	out := buf.String()
	if strings.Contains(out, "tenant:") {
		t.Fatalf("tenant line should be omitted for nil TenantID: %s", out)
	}
	if !strings.Contains(out, "("+noteCriterion+")") {
		t.Fatalf("note should appear in parens: %s", out)
	}
}

// TestRetireRequestBodySendsNullBlockerCountsWhenNil — sending a nil
// pointer serialises as JSON `null`. The generated
// `api.RetireChecklistRequest.BlockerCounts` has no `omitempty` on
// its struct tag (the FastAPI schema marks the field as nullable,
// not optional), so the wire carries the explicit `null`. The
// backend's `RetireChecklistRequest` pydantic model treats
// `null` and absent identically for the `blocker_counts` field
// (criterion 5 reports `REVIEW MANUALLY` in both cases per the
// schema doc-string), so the wire change from the pre-migration
// "absent" shape to the post-migration "null" shape is
// behaviour-preserving on the server.
func TestRetireRequestBodySendsNullBlockerCountsWhenNil(t *testing.T) {
	body := retireRequestBody(retireOptions{Surface: "kb"}, nil, nil)
	got := mustMarshal(t, body)
	if !strings.Contains(got, `"blocker_counts":null`) {
		t.Fatalf("blocker_counts should serialise as null when nil: %s", got)
	}
}

// TestRetireRequestBodyIncludesEmptyBlockerCountsMap — an empty
// map is serialised as `{}` and the backend treats it as "every
// surface has zero blockers" (caller intent). Distinct from the
// null-pointer case above, which means "unknown" (yellow).
//
// The nil-vs-empty distinction is load-bearing for criterion 5:
// passing `{}` means "the lookup ran and found zero open blockers
// on every surface" (green); passing `null` means "the lookup
// didn't run" (yellow / REVIEW MANUALLY). The generated
// `*map[string]int` shape preserves both halves.
func TestRetireRequestBodyIncludesEmptyBlockerCountsMap(t *testing.T) {
	empty := map[string]int{}
	body := retireRequestBody(retireOptions{Surface: "all"}, &empty, nil)
	got := mustMarshal(t, body)
	if !strings.Contains(got, `"blocker_counts":{}`) {
		t.Fatalf("expected empty map serialised; got %s", got)
	}
}

// TestRetireRequestBodyIncludesSurfaceCounts — a non-empty map
// round-trips key/value pairs as expected.
func TestRetireRequestBodyIncludesSurfaceCounts(t *testing.T) {
	counts := map[string]int{"kb": 0, "memory": 2}
	body := retireRequestBody(retireOptions{Surface: "all"}, &counts, nil)
	got := mustMarshal(t, body)
	if !strings.Contains(got, `"kb":0`) || !strings.Contains(got, `"memory":2`) {
		t.Fatalf("surface counts not in payload: %s", got)
	}
}

// TestLookupBlockerCountsFailsGracefullyWhenGHMissing — pointing the
// lookup at a non-existent binary surfaces the wrapped exec error
// rather than crashing or returning silent zeros.
func TestLookupBlockerCountsFailsGracefullyWhenGHMissing(t *testing.T) {
	// We can't fake the binary lookup easily; instead, drive the
	// real failure path by setting PATH to an empty value for this
	// single test process. The lookup uses exec.CommandContext("gh",
	// ...) which goes through PATH resolution.
	t.Setenv("PATH", "")
	counts, err := lookupBlockerCounts(context.Background(), "evoila/meho")
	if err == nil {
		t.Fatalf("expected error when gh is unreachable; got counts=%v", counts)
	}
}

// TestRetireRequestBodyIncludesBaselineOverrides — a non-empty
// override map round-trips into the wire body as the backend's
// `RetireChecklistRequest.baseline_overrides` field expects.
func TestRetireRequestBodyIncludesBaselineOverrides(t *testing.T) {
	kind := "grep"
	overrides := map[string]api.BaselineMetricsOverride{
		"kb": {PrecisionAt5: 0.7, Mrr: 0.5, Coverage: 0.85, Kind: &kind},
	}
	body := retireRequestBody(retireOptions{Surface: "kb"}, nil, &overrides)
	got := mustMarshal(t, body)
	if !strings.Contains(got, `"baseline_overrides"`) {
		t.Fatalf("baseline_overrides should appear in payload: %s", got)
	}
	if !strings.Contains(got, `"precision_at_5":0.7`) {
		t.Fatalf("baseline precision missing: %s", got)
	}
	if !strings.Contains(got, `"kind":"grep"`) {
		t.Fatalf("baseline kind missing: %s", got)
	}
}

// TestRetireRequestBodySendsNullBaselineOverridesWhenNil — a nil
// pointer serialises as JSON `null`. Pydantic treats absent and
// null equivalently for the `baseline_overrides` field; criterion
// 4 reports yellow (REVIEW MANUALLY) when the value is missing or
// null. Behaviour-preserving wire change from the pre-migration
// `omitempty` shape (which dropped the field entirely).
func TestRetireRequestBodySendsNullBaselineOverridesWhenNil(t *testing.T) {
	body := retireRequestBody(retireOptions{Surface: "kb"}, nil, nil)
	got := mustMarshal(t, body)
	if !strings.Contains(got, `"baseline_overrides":null`) {
		t.Fatalf("baseline_overrides should serialise as null when nil: %s", got)
	}
}

// TestLoadBaselineOverridesExtractsKbBaselineMetrics — a saved
// `eval --baseline grep` result yields the expected per-surface
// baseline triple ready to ship as criterion 4 override.
func TestLoadBaselineOverridesExtractsKbBaselineMetrics(t *testing.T) {
	kind := "grep"
	prec := float32(0.65)
	mrr := float32(0.45)
	cov := float32(0.80)
	result := &api.EvalResult{
		RanAt:          time.Date(2026, 5, 14, 12, 0, 0, 0, time.UTC),
		OverallVerdict: api.EvalResultOverallVerdictYellow,
		Surfaces: []api.SurfaceResult{
			{
				Surface:              api.SurfaceResultSurface("kb"),
				QueryCount:           10,
				PrecisionAt5:         0.85,
				Mrr:                  0.60,
				Coverage:             0.95,
				Verdict:              api.SurfaceResultVerdictGreen,
				BaselineKind:         &kind,
				BaselinePrecisionAt5: &prec,
				BaselineMrr:          &mrr,
				BaselineCoverage:     &cov,
			},
		},
	}
	dir := t.TempDir()
	path := dir + "/baseline.json"
	if err := writeBaseline(result, path); err != nil {
		t.Fatalf("writeBaseline: %v", err)
	}

	got, err := loadBaselineOverrides(path)
	if err != nil {
		t.Fatalf("loadBaselineOverrides: %v", err)
	}
	kb, ok := got["kb"]
	if !ok {
		t.Fatalf("kb override missing; got map=%v", got)
	}
	if kb.PrecisionAt5 != prec || kb.Mrr != mrr || kb.Coverage != cov {
		t.Fatalf("kb override metrics not preserved: %+v", kb)
	}
	if kb.Kind == nil || *kb.Kind != kind {
		t.Fatalf("kb override kind not preserved: %v", kb.Kind)
	}
}

// TestLoadBaselineOverridesSkipsSurfacesWithoutBaseline — a saved
// run where memory + operations had no baseline (the v0.2 default)
// emits only the kb entry; criterion 4 stays yellow for the others
// rather than silently flipping red on zero-metric data.
func TestLoadBaselineOverridesSkipsSurfacesWithoutBaseline(t *testing.T) {
	kind := "grep"
	prec := float32(0.65)
	mrr := float32(0.45)
	cov := float32(0.80)
	result := &api.EvalResult{
		RanAt:          time.Date(2026, 5, 14, 12, 0, 0, 0, time.UTC),
		OverallVerdict: api.EvalResultOverallVerdictYellow,
		Surfaces: []api.SurfaceResult{
			{
				Surface:              api.SurfaceResultSurface("kb"),
				QueryCount:           10,
				BaselineKind:         &kind,
				BaselinePrecisionAt5: &prec,
				BaselineMrr:          &mrr,
				BaselineCoverage:     &cov,
			},
			// memory + operations: no baseline (the v0.2 shape).
			{Surface: api.SurfaceResultSurface("memory"), QueryCount: 10},
			{Surface: api.SurfaceResultSurface("operations"), QueryCount: 10},
		},
	}
	dir := t.TempDir()
	path := dir + "/baseline.json"
	if err := writeBaseline(result, path); err != nil {
		t.Fatalf("writeBaseline: %v", err)
	}

	got, err := loadBaselineOverrides(path)
	if err != nil {
		t.Fatalf("loadBaselineOverrides: %v", err)
	}
	if len(got) != 1 {
		t.Fatalf("expected only kb override; got %v", got)
	}
	if _, ok := got["kb"]; !ok {
		t.Fatalf("kb override missing; got %v", got)
	}
}

// TestLoadBaselineOverridesRejectsMissingFile — a non-existent path
// surfaces a wrapped error so the caller's "warn + fall back to
// yellow" branch can fire instead of crashing.
func TestLoadBaselineOverridesRejectsMissingFile(t *testing.T) {
	_, err := loadBaselineOverrides("/no/such/baseline.json")
	if err == nil {
		t.Fatalf("expected error for missing baseline file")
	}
	if !strings.Contains(err.Error(), "read baseline file") {
		t.Fatalf("error should wrap with 'read baseline file'; got %v", err)
	}
}

// --- helpers ---

func mustMarshal(t *testing.T, v interface{}) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(raw)
}
