// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"bytes"
	"context"
	"encoding/json"
	"strings"
	"testing"
)

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
	tenant := "11111111-1111-1111-1111-111111111111"
	report := &RetireChecklistReport{
		RanAt:          "2026-05-14T12:00:00+00:00",
		TenantID:       &tenant,
		Since:          "2026-02-13T12:00:00+00:00",
		Until:          "2026-05-14T12:00:00+00:00",
		OverallVerdict: "READY TO RETIRE",
		Surfaces: []RetireSurfaceChecklist{
			{
				Surface: "kb",
				Verdict: "READY TO RETIRE",
				Criteria: []RetireCriterionResult{
					{
						Name:             "daily_use_duration",
						Verdict:          "green",
						ObservedValue:    "40 days since first use",
						ThresholdSummary: ">= 30 days",
					},
					{
						Name:             "open_blockers",
						Verdict:          "green",
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
	if !strings.Contains(out, "tenant: "+tenant) {
		t.Fatalf("missing tenant id: %s", out)
	}
}

// TestPrintRetireTableHandlesNotesAndNilTenant — notes render in
// parentheses; a nil tenant id is omitted entirely.
func TestPrintRetireTableHandlesNotesAndNilTenant(t *testing.T) {
	noteCriterion := "no baseline corpus configured for this surface in v0.2"
	report := &RetireChecklistReport{
		RanAt:          "2026-05-14T12:00:00+00:00",
		OverallVerdict: "REVIEW MANUALLY",
		Surfaces: []RetireSurfaceChecklist{
			{
				Surface: "memory",
				Verdict: "REVIEW MANUALLY",
				Criteria: []RetireCriterionResult{
					{
						Name:             "meho_vs_baseline",
						Verdict:          "yellow",
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

// TestRetireRequestMarshalOmitsBlockerCountsWhenNil — sending a nil
// pointer drops the field entirely (Pydantic treats absence + null
// the same way, but the wire stays compact).
func TestRetireRequestMarshalOmitsBlockerCountsWhenNil(t *testing.T) {
	req := retireRequest{Surface: "kb"}
	got := mustMarshal(t, req)
	if strings.Contains(got, "blocker_counts") {
		t.Fatalf("blocker_counts should be omitted when nil: %s", got)
	}
}

// TestRetireRequestMarshalIncludesEmptyBlockerCountsMap — an empty
// map is serialised as `{}` and the backend treats it as "every
// surface has zero blockers" (caller intent). Distinct from the nil
// case above.
func TestRetireRequestMarshalIncludesEmptyBlockerCountsMap(t *testing.T) {
	empty := map[string]int{}
	req := retireRequest{Surface: "all", BlockerCounts: &empty}
	got := mustMarshal(t, req)
	if !strings.Contains(got, `"blocker_counts":{}`) {
		t.Fatalf("expected empty map serialised; got %s", got)
	}
}

// TestRetireRequestMarshalIncludesSurfaceCounts — a non-empty map
// round-trips key/value pairs as expected.
func TestRetireRequestMarshalIncludesSurfaceCounts(t *testing.T) {
	counts := map[string]int{"kb": 0, "memory": 2}
	req := retireRequest{Surface: "all", BlockerCounts: &counts}
	got := mustMarshal(t, req)
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

// TestRetireRequestMarshalIncludesBaselineOverrides — a non-empty
// override map round-trips into the wire body as the backend's
// `RetireChecklistRequest.baseline_overrides` field expects.
func TestRetireRequestMarshalIncludesBaselineOverrides(t *testing.T) {
	overrides := map[string]baselineMetricsOverride{
		"kb": {PrecisionAt5: 0.7, MRR: 0.5, Coverage: 0.85, Kind: "grep"},
	}
	req := retireRequest{Surface: "kb", BaselineOverrides: &overrides}
	got := mustMarshal(t, req)
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

// TestRetireRequestMarshalOmitsBaselineOverridesWhenNil — a nil
// pointer drops the field (Pydantic treats absent and null
// equivalently; wire stays compact).
func TestRetireRequestMarshalOmitsBaselineOverridesWhenNil(t *testing.T) {
	req := retireRequest{Surface: "kb"}
	got := mustMarshal(t, req)
	if strings.Contains(got, "baseline_overrides") {
		t.Fatalf("baseline_overrides should be omitted when nil: %s", got)
	}
}

// TestLoadBaselineOverridesExtractsKbBaselineMetrics — a saved
// `eval --baseline grep` result yields the expected per-surface
// baseline triple ready to ship as criterion 4 override.
func TestLoadBaselineOverridesExtractsKbBaselineMetrics(t *testing.T) {
	kind := "grep"
	prec := 0.65
	mrr := 0.45
	cov := 0.80
	result := &EvalResult{
		RanAt:          "2026-05-14T12:00:00+00:00",
		OverallVerdict: "yellow",
		Surfaces: []EvalSurfaceResult{
			{
				Surface:              "kb",
				QueryCount:           10,
				PrecisionAt5:         0.85,
				MRR:                  0.60,
				Coverage:             0.95,
				Verdict:              "green",
				BaselineKind:         &kind,
				BaselinePrecisionAt5: &prec,
				BaselineMRR:          &mrr,
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
	if kb.PrecisionAt5 != prec || kb.MRR != mrr || kb.Coverage != cov {
		t.Fatalf("kb override metrics not preserved: %+v", kb)
	}
	if kb.Kind != kind {
		t.Fatalf("kb override kind not preserved: %q", kb.Kind)
	}
}

// TestLoadBaselineOverridesSkipsSurfacesWithoutBaseline — a saved
// run where memory + operations had no baseline (the v0.2 default)
// emits only the kb entry; criterion 4 stays yellow for the others
// rather than silently flipping red on zero-metric data.
func TestLoadBaselineOverridesSkipsSurfacesWithoutBaseline(t *testing.T) {
	kind := "grep"
	prec := 0.65
	mrr := 0.45
	cov := 0.80
	result := &EvalResult{
		RanAt:          "2026-05-14T12:00:00+00:00",
		OverallVerdict: "yellow",
		Surfaces: []EvalSurfaceResult{
			{
				Surface:              "kb",
				QueryCount:           10,
				BaselineKind:         &kind,
				BaselinePrecisionAt5: &prec,
				BaselineMRR:          &mrr,
				BaselineCoverage:     &cov,
			},
			// memory + operations: no baseline (the v0.2 shape).
			{Surface: "memory", QueryCount: 10},
			{Surface: "operations", QueryCount: 10},
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
