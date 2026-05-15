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

// TestSurfacesFromLabelsSingleSurface — a single `surface:kb` marker
// buckets to one surface.
func TestSurfacesFromLabelsSingleSurface(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "surface:kb"},
	})
	if len(got) != 1 || got[0] != "kb" {
		t.Fatalf("expected [kb]; got %v", got)
	}
}

// TestSurfacesFromLabelsMultipleSurfaces — multi-surface markers
// produce multi-bucket counts.
func TestSurfacesFromLabelsMultipleSurfaces(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "surface:kb"},
		{Name: "surface:memory"},
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

// TestSurfacesFromLabelsNoMarker — an issue without a `surface:`
// marker returns the empty slice (caller treats as generic blocker).
func TestSurfacesFromLabelsNoMarker(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "retrieval-migration-blocker"},
		{Name: "priority:high"},
	})
	if len(got) != 0 {
		t.Fatalf("expected empty slice for marker-less issue; got %v", got)
	}
}

// TestSurfacesFromLabelsIgnoresUnknownSurface — a typo'd
// `surface:bogus` marker doesn't crash and doesn't bucket anywhere.
func TestSurfacesFromLabelsIgnoresUnknownSurface(t *testing.T) {
	got := surfacesFromLabels([]ghIssueLabel{
		{Name: "surface:bogus"},
	})
	if len(got) != 0 {
		t.Fatalf("expected empty slice for unknown surface; got %v", got)
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

// --- helpers ---

func mustMarshal(t *testing.T, v interface{}) string {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return string(raw)
}
