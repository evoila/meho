// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// makeCurrentStepBody synthesises a wire StartRun / AdvanceRun 200 /
// 201 body shaped as the `kind=current_step` variant of the
// discriminated union NextStepResponse. Helper keeps the test
// bodies focused on the assertion they care about rather than the
// envelope ceremony.
//
// `extraStepIDs` is a deliberate test hook -- the opacity test
// (#1319 AC + test #5) needs to inject foreign step-id strings into
// the wire response and assert that the CLI renderer never picks
// them up. We splice them in alongside the legitimate fields via a
// nested injection map so the resulting JSON contains both.
func makeCurrentStepBody(
	runID, slug string,
	version, n, total int,
	step stepBodyDTO,
	extraStepIDs []string,
) []byte {
	payload := map[string]any{
		"kind":             "current_step",
		"run_id":           runID,
		"template_slug":    slug,
		"template_version": version,
		"position":         map[string]any{"n": n, "total": total},
		"current_step": map[string]any{
			"id":     step.ID,
			"title":  step.Title,
			"body":   step.Body,
			"type":   step.Type,
			"op_id":  step.OpID,
			"params": step.Params,
			"verify": step.Verify,
		},
	}
	if len(extraStepIDs) > 0 {
		// Out-of-contract: simulate a backend bug that leaked
		// other step IDs into the envelope. The CLI's
		// startResponseView struct doesn't have field-paths for
		// these, so encoding/json will discard them on the read
		// side and the renderer won't display them. The test
		// reads the on-wire body for a string-match assertion to
		// prove the leak was present in the wire JSON, and the
		// rendered output for the negative-presence assertion.
		future := make([]map[string]any, 0, len(extraStepIDs))
		for _, id := range extraStepIDs {
			future = append(future, map[string]any{
				"id":    id,
				"title": "LEAKED_FUTURE_STEP_" + id,
				"body":  "LEAKED_BODY_" + id,
			})
		}
		payload["future_steps"] = future
		payload["all_steps"] = future
	}
	raw, _ := json.Marshal(payload)
	return raw
}

// confirmStepBody produces a stepBodyDTO with a confirm-typed
// verify. The prompt is the operator-facing text from the
// substrate's substituted Verify shape.
func confirmStepBody(id, title, body, prompt string) stepBodyDTO {
	p := prompt
	return stepBodyDTO{
		ID:    id,
		Title: title,
		Body:  body,
		Type:  "manual",
		Verify: &stepBodyVerifyDTO{
			Type:   "confirm",
			Prompt: &p,
		},
	}
}

// operationCallStepBody produces a stepBodyDTO with an
// operation_call-typed verify, including the dispatched op_id.
func operationCallStepBody(id, title, body, opID, verifyOpID string) stepBodyDTO {
	op := opID
	vo := verifyOpID
	return stepBodyDTO{
		ID:    id,
		Title: title,
		Body:  body,
		Type:  "operation_call",
		OpID:  &op,
		Params: &map[string]any{
			"target": "host-1",
		},
		Verify: &stepBodyVerifyDTO{
			Type: "operation_call",
			OpID: &vo,
		},
	}
}

// TestRunStartHappyPath — POST hits the right path with the right
// body shape; the rendered output carries the run_id, template
// coords, step body, and verify summary.
func TestRunStartHappyPath(t *testing.T) {
	var seenBody api.StartRunRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("expected POST; got %s", r.Method)
		}
		raw, _ := io.ReadAll(r.Body)
		readJSONBodyOf(t, raw, &seenBody)
		step := confirmStepBody("step-1", "Quiesce host", "Drain ${run.target}.", "Is host drained?")
		body := makeCurrentStepBody("11111111-1111-4111-8111-111111111111",
			"vmware-host-quiesce", 2, 1, 3, step, nil)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(body)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug:              "vmware-host-quiesce",
		Target:            "esx-01",
		Params:            []string{"hours=2", "reason=upgrade"},
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runStartRun: %v; stderr=%s", err, stderr.String())
	}
	if seenBody.TemplateSlug != "vmware-host-quiesce" {
		t.Errorf("template_slug on wire: got %q", seenBody.TemplateSlug)
	}
	if seenBody.Target != "esx-01" {
		t.Errorf("target on wire: got %q", seenBody.Target)
	}
	if seenBody.Params == nil {
		t.Fatalf("params nil on wire; expected map with hours/reason")
	}
	if (*seenBody.Params)["hours"] != "2" || (*seenBody.Params)["reason"] != "upgrade" {
		t.Errorf("params on wire: got %+v", *seenBody.Params)
	}
	out := stdout.String()
	for _, want := range []string{
		"Run ID:      11111111-1111-4111-8111-111111111111",
		"Template:    vmware-host-quiesce@2",
		"Step 1/3: Quiesce host",
		"(id: step-1)",
		"Drain ${run.target}.", // body is rendered verbatim (substitution is server-side)
		"Verify type: confirm",
		"Prompt: Is host drained?",
		"yes|no|escalate",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected stdout to contain %q; got:\n%s", want, out)
		}
	}
}

// TestRunStartJSONHappyPath — --json emits the typed envelope.
func TestRunStartJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		step := confirmStepBody("s1", "Title", "Body", "p?")
		body := makeCurrentStepBody("22222222-2222-4222-8222-222222222222",
			"slug", 1, 1, 1, step, nil)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(body)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "slug", Target: "t", JSONOut: true, BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runStartRun --json: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded["template_slug"] != "slug" {
		t.Errorf("envelope: %+v", decoded)
	}
}

// TestRunStartOpacityRendering — LOAD-BEARING per issue #1319 AC.
// The substrate may (correctly) emit only the current step in its
// CurrentStepResponse envelope; this test simulates a hypothetical
// future backend bug that ALSO leaks other step IDs / bodies into
// the response. The CLI's stepBodyDTO has no field-path for those
// leaked fields, so even when present in the JSON they MUST NOT
// appear in rendered output.
//
// The assertion split:
//  1. The on-wire body MUST contain the leaked id (the test
//     genuinely simulated a leak).
//  2. The rendered stdout MUST NOT contain the leaked id, the
//     leaked title, or the leaked body — the CLI rendered only
//     the current_step's field paths.
//
// Same posture as the substrate's
// `test_step_body_omits_future_step_fields` regression test,
// flipped to the human surface.
func TestRunStartOpacityRendering(t *testing.T) {
	const (
		currentID   = "current-step-uid"
		leakedID1   = "future-step-uid-a"
		leakedID2   = "future-step-uid-b"
		leakedBody1 = "LEAKED_BODY_future-step-uid-a"
		leakedBody2 = "LEAKED_BODY_future-step-uid-b"
	)
	wireBody := makeCurrentStepBody(
		"33333333-3333-4333-8333-333333333333",
		"opacity-slug", 1, 1, 5,
		confirmStepBody(currentID, "Current step title", "Current step body.", "Done?"),
		[]string{leakedID1, leakedID2},
	)
	// Belt-and-suspenders: assert the test setup actually leaked
	// the IDs into the wire JSON. Without this, a refactor to
	// makeCurrentStepBody that quietly stopped emitting the future
	// steps would make the negative assertions trivially pass.
	for _, want := range []string{leakedID1, leakedID2, leakedBody1, leakedBody2} {
		if !strings.Contains(string(wireBody), want) {
			t.Fatalf("test setup bug: leaked field %q not in wire JSON", want)
		}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(wireBody)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "opacity-slug", Target: "t", BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runStartRun: %v; stderr=%s", err, stderr.String())
	}
	out := stdout.String()
	// Positive: the current step IS rendered.
	if !strings.Contains(out, currentID) {
		t.Errorf("expected current step id %q in output; got:\n%s", currentID, out)
	}
	if !strings.Contains(out, "Current step body.") {
		t.Errorf("expected current step body in output; got:\n%s", out)
	}
	if !strings.Contains(out, "Step 1/5") {
		t.Errorf("expected position marker in output; got:\n%s", out)
	}
	// Negative: the future step IDs / bodies MUST NOT leak through.
	for _, leaked := range []string{leakedID1, leakedID2, leakedBody1, leakedBody2,
		"LEAKED_FUTURE_STEP_" + leakedID1, "LEAKED_FUTURE_STEP_" + leakedID2} {
		if strings.Contains(out, leaked) {
			t.Errorf("OPACITY VIOLATION: leaked field %q rendered to stdout:\n%s",
				leaked, out)
		}
	}
}

// TestRunStartShowsOperationCallVerify — the rendered block surfaces
// the will-dispatch op_id so the operator knows what the substrate
// will call when they next.
func TestRunStartShowsOperationCallVerify(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		step := operationCallStepBody("s1", "Maintenance mode",
			"Switch host to maintenance.", "vmware.host.enter_maintenance",
			"vmware.host.is_in_maintenance")
		body := makeCurrentStepBody("44444444-4444-4444-8444-444444444444",
			"vmware-maint", 1, 1, 2, step, nil)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = w.Write(body)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "vmware-maint", Target: "esx", BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runStartRun op_call: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"Step kind:   operation_call (op_id: vmware.host.enter_maintenance)",
		"Verify type: operation_call",
		"Will dispatch op_id: vmware.host.is_in_maintenance",
		"(substrate dispatches the verify call)",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got:\n%s", want, out)
		}
	}
}

// TestRunStartRejectsEmptySlug — args[0] empty fails fast.
func TestRunStartRejectsEmptySlug(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{Slug: "", Target: "x"})
	if err == nil {
		t.Fatal("expected error for empty slug")
	}
	if !strings.Contains(stderr.String(), "non-empty <slug>") {
		t.Errorf("expected slug hint; got %q", stderr.String())
	}
}

// TestRunStartRejectsEmptyTarget — --target is required.
func TestRunStartRejectsEmptyTarget(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{Slug: "x", Target: ""})
	if err == nil {
		t.Fatal("expected error for empty target")
	}
	if !strings.Contains(stderr.String(), "--target") {
		t.Errorf("expected --target hint; got %q", stderr.String())
	}
}

// TestRunStartRejectsMalformedParam — --param without `=` is bad.
func TestRunStartRejectsMalformedParam(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "x", Target: "y", Params: []string{"goodkey=goodval", "BADKEY_NO_EQUALS"},
	})
	if err == nil {
		t.Fatal("expected error for malformed --param")
	}
	if !strings.Contains(stderr.String(), "k=v") {
		t.Errorf("expected k=v hint; got %q", stderr.String())
	}
}

// TestRunStart404SurfacesSlugNotFound — the substrate emits
// `TemplateNotFoundError` -> 404 with a detail string for an
// unknown slug.
func TestRunStart404SurfacesSlugNotFound(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, `{"detail":"slug_not_found"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "missing", Target: "t", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on 404")
	}
	if !strings.Contains(stderr.String(), "slug_not_found") {
		t.Errorf("expected slug_not_found in stderr; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 4 {
		t.Errorf("expected ExitCode 4; got %v", err)
	}
}

// TestRunStart400DeprecatedTemplate — every version deprecated → 400.
func TestRunStart400DeprecatedTemplate(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadRequest)
		fmt.Fprint(w, `{"detail":"all_versions_deprecated"}`)
	})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "stale-slug", Target: "t", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on 400")
	}
	if !strings.Contains(stderr.String(), "all_versions_deprecated") {
		t.Errorf("expected deprecated hint; got %q", stderr.String())
	}
}

// TestRunStartNetworkError — closed server → exit 3 unreachable.
func TestRunStartNetworkError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	err := runStartRun(cmd, startRunOptions{
		Slug: "x", Target: "t", BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on closed server")
	}
	if !strings.Contains(stderr.String(), "unreachable") {
		t.Errorf("expected unreachable; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 3 {
		t.Errorf("expected ExitCode 3; got %v", err)
	}
}

// TestParseParamFlagsHappyPath — well-formed k=v inputs parse.
func TestParseParamFlagsHappyPath(t *testing.T) {
	out, err := parseParamFlags([]string{"a=1", "b=2", "c=hello world"})
	if err != nil {
		t.Fatalf("parseParamFlags: %v", err)
	}
	if out["a"] != "1" || out["b"] != "2" || out["c"] != "hello world" {
		t.Errorf("params: %+v", out)
	}
}

// TestParseParamFlagsEmptyIsNil — nil input returns nil so the JSON
// body omits the params key (backend defaults to {}).
func TestParseParamFlagsEmptyIsNil(t *testing.T) {
	out, err := parseParamFlags(nil)
	if err != nil {
		t.Fatalf("parseParamFlags(nil): %v", err)
	}
	if out != nil {
		t.Errorf("expected nil; got %+v", out)
	}
}

// TestParseParamFlagsValueContainingEquals — first `=` is the
// separator so `--param token=a=b=c` yields {token: "a=b=c"}.
func TestParseParamFlagsValueContainingEquals(t *testing.T) {
	out, err := parseParamFlags([]string{"token=a=b=c"})
	if err != nil {
		t.Fatalf("parseParamFlags: %v", err)
	}
	if out["token"] != "a=b=c" {
		t.Errorf("expected token=a=b=c; got %q", out["token"])
	}
}

// TestParseParamFlagsRejectsEmptyKey — leading `=` is invalid.
func TestParseParamFlagsRejectsEmptyKey(t *testing.T) {
	_, err := parseParamFlags([]string{"=v"})
	if err == nil {
		t.Fatal("expected error for empty key")
	}
}

// TestDecodeNextStepResponseCurrentStep — kind discriminator routes
// to the current_step view.
func TestDecodeNextStepResponseCurrentStep(t *testing.T) {
	body := makeCurrentStepBody("u1", "s", 1, 1, 1,
		confirmStepBody("id1", "t", "b", "p"), nil)
	current, completed, err := decodeNextStepResponse(body)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if completed != nil {
		t.Errorf("expected current_step only; got completed: %+v", completed)
	}
	if current == nil || current.RunID != "u1" || current.CurrentStep.ID != "id1" {
		t.Errorf("decoded current: %+v", current)
	}
}

// TestDecodeNextStepResponseCompleted — kind=completed routes to the
// completion view.
func TestDecodeNextStepResponseCompleted(t *testing.T) {
	body := []byte(`{"kind":"completed","run_id":"u2","state":"completed","completed_at":"2026-05-30T12:00:00Z"}`)
	current, completed, err := decodeNextStepResponse(body)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	if current != nil {
		t.Errorf("expected completed only; got current: %+v", current)
	}
	if completed == nil || completed.RunID != "u2" || completed.State != "completed" {
		t.Errorf("decoded completed: %+v", completed)
	}
}

// TestDecodeNextStepResponseUnknownKind — a third kind surfaces as
// an error (no-third-response-shape contract).
func TestDecodeNextStepResponseUnknownKind(t *testing.T) {
	body := []byte(`{"kind":"surprise","run_id":"u3"}`)
	_, _, err := decodeNextStepResponse(body)
	if err == nil {
		t.Fatal("expected error for unknown kind")
	}
}
