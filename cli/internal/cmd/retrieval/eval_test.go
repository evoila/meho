// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package retrieval

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
	"github.com/evoila/meho/cli/internal/backplane"
)

// TestDiffEvalResultsNoRegression — identical metrics on every
// surface returns an empty regression list (the CI gate's success
// path).
func TestDiffEvalResultsNoRegression(t *testing.T) {
	today := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	baseline := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	got := diffEvalResults(today, baseline, defaultEpsilon)
	if len(got) != 0 {
		t.Fatalf("expected no regressions; got %d: %v", len(got), got)
	}
}

// TestDiffEvalResultsDetectsPrecisionRegression — a precision drop
// > epsilon shows up as a regression entry naming the metric.
func TestDiffEvalResultsDetectsPrecisionRegression(t *testing.T) {
	today := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.50, Mrr: 0.60, Coverage: 0.95},
		},
	}
	baseline := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	got := diffEvalResults(today, baseline, defaultEpsilon)
	if len(got) != 1 {
		t.Fatalf("expected 1 regression; got %d: %v", len(got), got)
	}
	if !strings.Contains(got[0], "kb.precision_at_5") {
		t.Fatalf("regression line should name kb.precision_at_5; got %q", got[0])
	}
}

// TestDiffEvalResultsWithinEpsilonNotFlagged — a tiny drop within
// the epsilon noise floor does NOT trip the gate.
func TestDiffEvalResultsWithinEpsilonNotFlagged(t *testing.T) {
	today := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.84, Mrr: 0.60, Coverage: 0.95},
		},
	}
	baseline := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	got := diffEvalResults(today, baseline, defaultEpsilon)
	if len(got) != 0 {
		t.Fatalf("expected no regressions for 0.01 drop within 0.02 epsilon; got %v", got)
	}
}

// TestDiffEvalResultsSkipsZeroQuerySurfaces — a surface with zero
// queries on either side is not compared (mirrors backend semantics).
func TestDiffEvalResultsSkipsZeroQuerySurfaces(t *testing.T) {
	today := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 0, PrecisionAt5: 0.0, Mrr: 0.0, Coverage: 0.0},
		},
	}
	baseline := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	got := diffEvalResults(today, baseline, defaultEpsilon)
	if len(got) != 0 {
		t.Fatalf("expected zero-corpus surface to be skipped; got %v", got)
	}
}

// TestDiffEvalResultsIgnoresSurfacesOnlyOnOneSide — adding a new
// surface in today is not a regression on the existing surfaces.
func TestDiffEvalResultsIgnoresSurfacesOnlyOnOneSide(t *testing.T) {
	today := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
			{Surface: "memory", QueryCount: 10, PrecisionAt5: 0.10, Mrr: 0.05, Coverage: 0.20},
		},
	}
	baseline := &api.EvalResult{
		Surfaces: []api.SurfaceResult{
			{Surface: "kb", QueryCount: 10, PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95},
		},
	}
	got := diffEvalResults(today, baseline, defaultEpsilon)
	if len(got) != 0 {
		t.Fatalf("expected new surface in today to be skipped; got %v", got)
	}
}

// TestWriteBaselineRoundTripPreservesShape — writing then reading
// yields the same struct (the persistence shape used by the CI
// gate workflow).
func TestWriteBaselineRoundTripPreservesShape(t *testing.T) {
	ranAt, _ := time.Parse(time.RFC3339, "2026-05-14T18:00:00Z")
	original := &api.EvalResult{
		RanAt:          ranAt,
		OverallVerdict: api.EvalResultOverallVerdictGreen,
		Surfaces: []api.SurfaceResult{
			{
				Surface: "kb", QueryCount: 10,
				PrecisionAt5: 0.85, Mrr: 0.60, Coverage: 0.95,
				Verdict: api.SurfaceResultVerdictGreen,
			},
		},
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "baseline.json")
	if err := writeBaseline(original, path); err != nil {
		t.Fatalf("writeBaseline: %v", err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	if !strings.HasSuffix(string(raw), "\n") {
		t.Fatalf("baseline file should end with newline; got %q", string(raw)[len(raw)-2:])
	}
	var loaded api.EvalResult
	if err := json.Unmarshal(raw, &loaded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if loaded.OverallVerdict != api.EvalResultOverallVerdictGreen {
		t.Fatalf("verdict not preserved: %q", loaded.OverallVerdict)
	}
	if len(loaded.Surfaces) != 1 || loaded.Surfaces[0].Surface != "kb" {
		t.Fatalf("surfaces not preserved: %+v", loaded.Surfaces)
	}
}

// TestMaybeCompareBaselineEmptyPathSkipsRead — empty path → no
// disk read, no error.
func TestMaybeCompareBaselineEmptyPathSkipsRead(t *testing.T) {
	today := &api.EvalResult{}
	got, err := maybeCompareBaseline(today, "")
	if err != nil {
		t.Fatalf("expected no error for empty path; got %v", err)
	}
	if got != nil {
		t.Fatalf("expected nil regressions for empty path; got %v", got)
	}
}

// TestMaybeCompareBaselineMissingFileReturnsError — a non-empty
// path that points at nothing surfaces a wrapped error.
func TestMaybeCompareBaselineMissingFileReturnsError(t *testing.T) {
	today := &api.EvalResult{}
	_, err := maybeCompareBaseline(today, "/no/such/baseline.json")
	if err == nil {
		t.Fatalf("expected error for missing baseline; got nil")
	}
	if !strings.Contains(err.Error(), "read baseline") {
		t.Fatalf("error should be wrapped with 'read baseline'; got %v", err)
	}
}

// TestNormaliseURLStripsTrailingSlash — backplane URLs are
// canonicalised so the same URL maps to the same store key.
func TestNormaliseURLStripsTrailingSlash(t *testing.T) {
	got, err := backplane.NormaliseURL("https://meho.test/")
	if err != nil {
		t.Fatalf("normaliseURL: %v", err)
	}
	if got != "https://meho.test" {
		t.Fatalf("expected trailing slash stripped; got %q", got)
	}
}

// TestNormaliseURLRejectsEmpty — empty URL returns an error
// (operator forgot to pass --backplane).
func TestNormaliseURLRejectsEmpty(t *testing.T) {
	_, err := backplane.NormaliseURL("   ")
	if err == nil || !strings.Contains(err.Error(), "empty") {
		t.Fatalf("expected 'empty' error; got %v", err)
	}
}

// TestNormaliseURLRejectsHostless — URL without a host (e.g. just a
// path) is rejected at the boundary.
func TestNormaliseURLRejectsHostless(t *testing.T) {
	_, err := backplane.NormaliseURL("/no-host")
	if err == nil {
		t.Fatalf("expected error for hostless URL; got nil")
	}
}

// TestErrEvalGateIsSentinel — verifies the sentinel exists + is
// distinct, so callers can switch on it.
func TestErrEvalGateIsSentinel(t *testing.T) {
	if errEvalGate == nil {
		t.Fatalf("errEvalGate should be a non-nil sentinel")
	}
	if errors.Is(errEvalGate, errors.New("other")) {
		t.Fatalf("errEvalGate should not match arbitrary errors")
	}
}

// TestClassifyBackplaneErrorRoutesByCause — ErrConfigNotFound (or
// any error wrapping it) maps to AuthExpired (exit 2 = the operator
// needs to run `meho login`). Every other error maps to Unexpected
// (exit 4 = the cause is operator argv or a corrupt config). The
// pre-fix shape mapped everything to AuthExpired which sent
// operators down the `meho login` path even for a typo in
// `--backplane http:/example`.
func TestClassifyBackplaneErrorRoutesByCause(t *testing.T) {
	wrappedNotFound := &backplane.NotConfiguredError{Inner: auth.ErrConfigNotFound}
	se := backplane.ClassifyError(wrappedNotFound)
	if se == nil || se.Code != "auth_expired" {
		t.Fatalf("ErrConfigNotFound wrapper should classify as auth_expired; got %+v", se)
	}

	parseFailure := errors.New("invalid backplane URL")
	se = backplane.ClassifyError(parseFailure)
	if se == nil || se.Code != "unexpected_response" {
		t.Fatalf("parse failure should classify as unexpected; got %+v", se)
	}
}

// TestEvalRequestBodyOmitsEmptySurfaceAndBaseline — the wire shape
// for a bare `meho retrieval eval` keeps Surface + Baseline at their
// pointer-nil zero so the backend's defaults apply.
func TestEvalRequestBodyOmitsEmptySurfaceAndBaseline(t *testing.T) {
	body := evalRequestBody(evalOptions{Surface: "", Baseline: ""})
	if body.Surface != nil {
		t.Errorf("Surface should be nil for empty input; got %v", *body.Surface)
	}
	if body.Baseline != nil {
		t.Errorf("Baseline should be nil for empty input; got %v", *body.Baseline)
	}
}

// TestEvalRequestBodySendsSurface — operator-supplied --surface kb
// shows up on the wire as the typed enum value.
func TestEvalRequestBodySendsSurface(t *testing.T) {
	body := evalRequestBody(evalOptions{Surface: "kb"})
	if body.Surface == nil || *body.Surface != api.EvalRequestSurfaceKb {
		t.Errorf("Surface should be kb; got %v", body.Surface)
	}
}

// TestEvalRequestBodyMarshalsToWire — round-trips through the JSON
// encoder to assert the wire keys line up with the backend's
// extra="forbid" schema. A typo (`surfaces` instead of `surface`)
// would fail 422 at the framework boundary.
func TestEvalRequestBodyMarshalsToWire(t *testing.T) {
	body := evalRequestBody(evalOptions{Surface: "kb", Baseline: "grep"})
	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	wire := string(raw)
	if !strings.Contains(wire, `"surface":"kb"`) {
		t.Errorf("expected surface=kb on wire; got %s", wire)
	}
	if !strings.Contains(wire, `"baseline":"grep"`) {
		t.Errorf("expected baseline=grep on wire; got %s", wire)
	}
}
