// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package runbook

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

// TestRunNextWithExplicitVerifyResponseYes — issue test #6. With
// `--verify-response yes`, the verb POSTs once with the answer
// embedded; no prompt.
func TestRunNextWithExplicitVerifyResponseYes(t *testing.T) {
	calls := int32(0)
	var seen api.NextStepRequest
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/11111111-1111-4111-8111-111111111111/next",
		func(w http.ResponseWriter, r *http.Request) {
			atomic.AddInt32(&calls, 1)
			if r.Method != http.MethodPost {
				t.Errorf("expected POST; got %s", r.Method)
			}
			raw, _ := io.ReadAll(r.Body)
			readJSONBodyOf(t, raw, &seen)
			step := confirmStepBody("step-2", "Second step", "Do thing.", "Done?")
			body := makeCurrentStepBody("11111111-1111-4111-8111-111111111111",
				"slug", 1, 2, 5, step, nil)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	// EOF stdin to prove the prompt path isn't entered.
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "11111111-1111-4111-8111-111111111111",
		VerifyResponse:    "yes",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v; stderr=%s", err, stderr.String())
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Errorf("expected exactly 1 POST; got %d", got)
	}
	if !seen.LastVerified {
		t.Errorf("expected last_verified=true on a supplied --verify-response")
	}
	if seen.VerifyResponse == nil {
		t.Fatalf("expected verify_response in body; got nil")
	}
	confirm, cerr := seen.VerifyResponse.AsConfirmVerifyResponse()
	if cerr != nil {
		t.Fatalf("AsConfirmVerifyResponse: %v", cerr)
	}
	if string(confirm.Answer) != "yes" {
		t.Errorf("expected answer=yes; got %q", confirm.Answer)
	}
	out := stdout.String()
	if !strings.Contains(out, "Step 2/5: Second step") {
		t.Errorf("expected new step rendered; got:\n%s", out)
	}
}

// TestRunNextInteractiveConfirmPrompt — issue test #7. No
// --verify-response; substrate returns 422
// VerifyResponseRequiredError; CLI prompts; on `yes` input, POSTs
// again with the answer and renders the next step.
func TestRunNextInteractiveConfirmPrompt(t *testing.T) {
	calls := int32(0)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/22222222-2222-4222-8222-222222222222/next",
		func(w http.ResponseWriter, r *http.Request) {
			n := atomic.AddInt32(&calls, 1)
			raw, _ := io.ReadAll(r.Body)
			var req api.NextStepRequest
			readJSONBodyOf(t, raw, &req)
			if n == 1 {
				// First call: operator didn't supply an answer; we
				// expect last_verified=false and no verify_response.
				if req.LastVerified {
					t.Errorf("first call: expected last_verified=false")
				}
				// The codegen union's `union` field is unexported;
				// we can't inspect the raw bytes here. Instead pin
				// the discriminator-absent shape: a non-nil
				// VerifyResponse would surface a discriminator on
				// AsConfirmVerifyResponse and we'd assert against
				// that; nil VerifyResponse is the intended first-
				// call shape.
				if req.VerifyResponse != nil {
					if _, derr := req.VerifyResponse.Discriminator(); derr == nil {
						t.Errorf("first call: expected no verify_response; got one with a discriminator")
					}
				}
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnprocessableEntity)
				fmt.Fprint(w, `{"detail":"VerifyResponseRequiredError: step-2 needs a confirm response"}`)
				return
			}
			// Second call: the prompt path supplied "yes".
			if !req.LastVerified {
				t.Errorf("second call: expected last_verified=true")
			}
			if req.VerifyResponse == nil {
				t.Errorf("second call: expected verify_response set")
			}
			step := confirmStepBody("step-3", "Third", "Body.", "p?")
			body := makeCurrentStepBody("22222222-2222-4222-8222-222222222222",
				"slug", 1, 3, 5, step, nil)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("yes\n"))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "22222222-2222-4222-8222-222222222222",
		VerifyResponse:    "", // explicitly empty → enter prompt path
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v; stderr=%s", err, stderr.String())
	}
	if got := atomic.LoadInt32(&calls); got != 2 {
		t.Errorf("expected 2 POSTs (initial probe + retry); got %d", got)
	}
	out := stdout.String()
	if !strings.Contains(out, "Verify required") {
		t.Errorf("expected prompt preface; got:\n%s", out)
	}
	if !strings.Contains(out, "Answer [yes/no/escalate]") {
		t.Errorf("expected prompt text; got:\n%s", out)
	}
	if !strings.Contains(out, "Step 3/5: Third") {
		t.Errorf("expected post-prompt step render; got:\n%s", out)
	}
}

// TestRunNextInteractiveInvalidAnswer — issue test #8. Operator
// enters `maybe` → CLI re-prompts.
func TestRunNextInteractiveInvalidAnswer(t *testing.T) {
	calls := int32(0)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/33333333-3333-4333-8333-333333333333/next",
		func(w http.ResponseWriter, r *http.Request) {
			n := atomic.AddInt32(&calls, 1)
			_ = r
			if n == 1 {
				w.WriteHeader(http.StatusUnprocessableEntity)
				fmt.Fprint(w, `{"detail":"VerifyResponseRequiredError"}`)
				return
			}
			step := confirmStepBody("s4", "Step 4", "B.", "p?")
			body := makeCurrentStepBody("33333333-3333-4333-8333-333333333333",
				"slug", 1, 4, 5, step, nil)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("maybe\nyes\n"))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "33333333-3333-4333-8333-333333333333",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, `invalid answer "maybe"`) {
		t.Errorf("expected re-prompt on invalid answer; got:\n%s", out)
	}
	if !strings.Contains(out, "Step 4/5") {
		t.Errorf("expected post-retry render; got:\n%s", out)
	}
}

// TestRunNextInteractiveEscalate — issue test #9. The CLI accepts
// `escalate` and POSTs it.
func TestRunNextInteractiveEscalate(t *testing.T) {
	var secondAnswer string
	calls := int32(0)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/44444444-4444-4444-8444-444444444444/next",
		func(w http.ResponseWriter, r *http.Request) {
			n := atomic.AddInt32(&calls, 1)
			raw, _ := io.ReadAll(r.Body)
			var req api.NextStepRequest
			readJSONBodyOf(t, raw, &req)
			if n == 1 {
				w.WriteHeader(http.StatusUnprocessableEntity)
				fmt.Fprint(w, `{"detail":"VerifyResponseRequiredError"}`)
				return
			}
			confirm, _ := req.VerifyResponse.AsConfirmVerifyResponse()
			secondAnswer = string(confirm.Answer)
			// Escalate transitions the step to failed; the substrate
			// would surface PreviousStepFailedError on the NEXT call.
			// For this test we render a completion banner directly
			// since the test is about the answer reaching the wire.
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"kind":"completed","run_id":"44444444-4444-4444-8444-444444444444","state":"abandoned","completed_at":"2026-05-30T12:00:00Z"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("escalate\n"))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "44444444-4444-4444-8444-444444444444",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v", err)
	}
	if secondAnswer != "escalate" {
		t.Errorf("expected escalate on wire; got %q", secondAnswer)
	}
	if !strings.Contains(stdout.String(), "Run complete.") {
		t.Errorf("expected completion banner; got:\n%s", stdout.String())
	}
}

// TestRunNextOpacityRendering — LOAD-BEARING per issue #1319 AC and
// test #5. The substrate's 200 response can in theory carry future
// step bodies (a backend bug); the CLI renderer must only display
// the current_step's field paths.
//
// Mirrors the start-side opacity test verbatim except this is on the
// `next` route (which is the verb operators call most often during
// an in-flight run).
func TestRunNextOpacityRendering(t *testing.T) {
	const (
		currentID = "real-current-step"
		leakedID  = "leaked-future-step"
	)
	wireBody := makeCurrentStepBody(
		"55555555-5555-4555-8555-555555555555",
		"opacity-next-slug", 1, 4, 5,
		confirmStepBody(currentID, "Real title", "Real body.", "p?"),
		[]string{leakedID},
	)
	if !strings.Contains(string(wireBody), leakedID) {
		t.Fatalf("test setup: leaked id %q not in wire JSON", leakedID)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/55555555-5555-4555-8555-555555555555/next",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(wireBody)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "55555555-5555-4555-8555-555555555555",
		VerifyResponse:    "yes",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v", err)
	}
	out := stdout.String()
	if !strings.Contains(out, currentID) {
		t.Errorf("expected current step id in output; got:\n%s", out)
	}
	if !strings.Contains(out, "Real body.") {
		t.Errorf("expected real body in output; got:\n%s", out)
	}
	if strings.Contains(out, leakedID) {
		t.Errorf("OPACITY VIOLATION: leaked id %q rendered to stdout:\n%s", leakedID, out)
	}
	if strings.Contains(out, "LEAKED_BODY_") {
		t.Errorf("OPACITY VIOLATION: leaked body rendered; got:\n%s", out)
	}
}

// TestRunNextOperationCallVerifyPass — issue test #10. The substrate
// dispatches an operation_call verify, matches, and the next step
// body lands; the CLI renders the next step. The match/mismatch
// verdict is operator-visible via the rendered step body's
// `Verify type:` block (the next step's verify, which may itself
// be confirm or operation_call).
func TestRunNextOperationCallVerifyPass(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/66666666-6666-4666-8666-666666666666/next",
		func(w http.ResponseWriter, _ *http.Request) {
			step := confirmStepBody("step-after-opcall", "Post-opcall step",
				"Operation verified; continue.", "ready?")
			body := makeCurrentStepBody("66666666-6666-4666-8666-666666666666",
				"slug", 1, 3, 4, step, nil)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "66666666-6666-4666-8666-666666666666",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun: %v", err)
	}
	if !strings.Contains(stdout.String(), "Step 3/4: Post-opcall step") {
		t.Errorf("expected post-opcall step render; got:\n%s", stdout.String())
	}
}

// TestRunNextOperationCallVerifyFail — issue test #11. The
// substrate's verify dispatch mismatched; backend returns 422
// VerifyResponseMismatchError. The CLI surfaces the detail.
func TestRunNextOperationCallVerifyFail(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/77777777-7777-4777-8777-777777777777/next",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusUnprocessableEntity)
			fmt.Fprint(w, `{"detail":"VerifyResponseMismatchError: expected powered_on=true, got powered_on=false"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "77777777-7777-4777-8777-777777777777",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on verify mismatch")
	}
	out := stderr.String()
	if !strings.Contains(out, "VerifyResponseMismatchError") {
		t.Errorf("expected mismatch detail in stderr; got:\n%s", out)
	}
	if !strings.Contains(out, "powered_on=false") {
		t.Errorf("expected actual-vs-expected detail in stderr; got:\n%s", out)
	}
}

// TestRunNextRunCompleted — issue test #12. RunCompletedResponse
// → CLI prints "Run complete." and exits 0.
func TestRunNextRunCompleted(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/88888888-8888-4888-8888-888888888888/next",
		func(w http.ResponseWriter, _ *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"kind":"completed","run_id":"88888888-8888-4888-8888-888888888888","state":"completed","completed_at":"2026-05-30T12:34:56Z"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "88888888-8888-4888-8888-888888888888",
		VerifyResponse:    "yes",
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun completed: %v", err)
	}
	out := stdout.String()
	for _, want := range []string{
		"Run complete.",
		"run_id=88888888-8888-4888-8888-888888888888",
		"state=completed",
		"2026-05-30T12:34:56Z",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("expected %q in output; got:\n%s", want, out)
		}
	}
}

// TestRunNextRejectsBadUUID — args[0] non-UUID fails fast.
func TestRunNextRejectsBadUUID(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runNextRun(cmd, nextRunOptions{RunID: "not-a-uuid"})
	if err == nil {
		t.Fatal("expected error for bad UUID")
	}
	if !strings.Contains(stderr.String(), "invalid run_id") {
		t.Errorf("expected uuid hint; got %q", stderr.String())
	}
}

// TestRunNextRejectsBadVerifyResponse — typo in --verify-response.
func TestRunNextRejectsBadVerifyResponse(t *testing.T) {
	cmd, _, stderr := newRunCmd(t)
	err := runNextRun(cmd, nextRunOptions{
		RunID:          "99999999-9999-4999-8999-999999999999",
		VerifyResponse: "perhaps",
	})
	if err == nil {
		t.Fatal("expected error for bad --verify-response")
	}
	if !strings.Contains(stderr.String(), "yes, no, escalate") {
		t.Errorf("expected enum hint; got %q", stderr.String())
	}
}

// TestRunNext403SurfacesNotRunAssignee — single-assignee discipline:
// the route returns 403 NotRunAssigneeError if the caller isn't the
// assignee (or admin via reassign first).
func TestRunNext403SurfacesNotRunAssignee(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/next",
		func(w http.ResponseWriter, _ *http.Request) {
			w.WriteHeader(http.StatusForbidden)
			fmt.Fprint(w, `{"detail":"NotRunAssigneeError: caller is not the run's assignee"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
		VerifyResponse:    "yes",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected 403 error")
	}
	if !strings.Contains(stderr.String(), "NotRunAssigneeError") {
		t.Errorf("expected assignee detail; got %q", stderr.String())
	}
	type ec interface{ ExitCode() int }
	if x, ok := err.(ec); !ok || x.ExitCode() != 5 {
		t.Errorf("expected ExitCode 5; got %v", err)
	}
}

// TestRunNext422NonRequiredErrorDoesNotPrompt — a 422 that is NOT a
// VerifyResponseRequiredError (e.g. VerifyResponseMismatchError on a
// supplied answer) must surface as an error WITHOUT entering the
// interactive prompt loop. This pins the verifyResponseRequired
// probe's discriminator: only the "missing verify response" 422
// triggers the prompt.
func TestRunNext422NonRequiredErrorDoesNotPrompt(t *testing.T) {
	calls := int32(0)
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb/next",
		func(w http.ResponseWriter, _ *http.Request) {
			atomic.AddInt32(&calls, 1)
			w.WriteHeader(http.StatusUnprocessableEntity)
			fmt.Fprint(w, `{"detail":"VerifyResponseMismatchError: shape mismatch"}`)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, _, stderr := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString("yes\n")) // would be consumed if prompt fired
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
		VerifyResponse:    "",
		BackplaneOverride: srv.URL,
	})
	if err == nil {
		t.Fatal("expected error on 422 mismatch")
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Errorf("expected exactly 1 POST (no retry); got %d", got)
	}
	if !strings.Contains(stderr.String(), "VerifyResponseMismatchError") {
		t.Errorf("expected mismatch detail; got %q", stderr.String())
	}
}

// TestRunNextJSONHappyPath — --json emits the typed envelope.
func TestRunNextJSONHappyPath(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/runbooks/runs/cccccccc-cccc-4ccc-8ccc-cccccccccccc/next",
		func(w http.ResponseWriter, _ *http.Request) {
			step := confirmStepBody("s5", "Title", "Body", "p?")
			body := makeCurrentStepBody("cccccccc-cccc-4ccc-8ccc-cccccccccccc",
				"slug", 1, 5, 5, step, nil)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(body)
		})
	srv := httptest.NewServer(mux)
	defer srv.Close()
	seedXDGAndToken(t, srv.URL)

	cmd, stdout, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	err := runNextRun(cmd, nextRunOptions{
		RunID:             "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
		VerifyResponse:    "yes",
		JSONOut:           true,
		BackplaneOverride: srv.URL,
	})
	if err != nil {
		t.Fatalf("runNextRun --json: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(stdout.Bytes(), &decoded); err != nil {
		t.Fatalf("stdout not JSON: %v; %q", err, stdout.String())
	}
	if decoded["run_id"] != "cccccccc-cccc-4ccc-8ccc-cccccccccccc" {
		t.Errorf("envelope: %+v", decoded)
	}
}
