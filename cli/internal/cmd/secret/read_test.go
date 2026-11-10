// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package secret

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/dispatch"
)

// withStdoutTTY overrides the stdoutIsTTY seam for the duration of the
// test and restores it afterwards. The default (false, "piped") is what
// every test but the TTY-refuse test wants, so tests that need the
// happy path don't have to touch the seam — but the production default
// interrogates the real os.Stdout fd, which under `go test` is a pipe,
// so it already reports false. We pin it explicitly for determinism.
func withStdoutTTY(t *testing.T, isTTY bool) {
	t.Helper()
	prev := stdoutIsTTY
	stdoutIsTTY = func() bool { return isTTY }
	t.Cleanup(func() { stdoutIsTTY = prev })
}

// ---------- command-tree shape ----------

// TestNewRootCmdHasRead — the root command must expose the `read` verb
// alongside `move` (acceptance criterion: wired into NewRootCmd()).
func TestNewRootCmdHasRead(t *testing.T) {
	root := NewRootCmd()
	got := map[string]bool{}
	for _, sub := range root.Commands() {
		got[sub.Name()] = true
	}
	if !got["read"] {
		t.Errorf("secret root is missing the read sub-verb; has %v", got)
	}
}

// TestReadHelpListsFlags — `meho secret read --help` must surface
// --field and --target (acceptance criterion: --help exits 0 and shows
// the flags). The TTY seam is irrelevant here — cobra prints help
// before RunE runs.
func TestReadHelpListsFlags(t *testing.T) {
	cmd := newReadCmd()
	var buf bytes.Buffer
	cmd.SetOut(&buf)
	cmd.SetErr(&buf)
	cmd.SetArgs([]string{"--help"})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("--help execute: %v", err)
	}
	out := buf.String()
	for _, want := range []string{"--field", "--target"} {
		if !strings.Contains(out, want) {
			t.Errorf("read --help output missing %q\n%s", want, out)
		}
	}
}

// TestReadFieldRequired — --field is a required flag; the command exposes
// no inline-value flag that would land a secret in argv.
func TestReadFieldRequired(t *testing.T) {
	cmd := newReadCmd()
	flag := cmd.Flags().Lookup("field")
	if flag == nil {
		t.Fatalf("read is missing the --field flag")
	}
	if ann := flag.Annotations[cobraRequiredAnnotation]; len(ann) == 0 || ann[0] != "true" {
		t.Errorf("--field should be marked required; annotations=%v", flag.Annotations)
	}
	for _, banned := range []string{"value", "secret", "password"} {
		if cmd.Flags().Lookup(banned) != nil {
			t.Errorf("read must NOT expose an inline --%s flag", banned)
		}
	}
}

// ---------- raw-value-only contract ----------

// TestSecretRead — the load-bearing acceptance criterion. With a mock
// backplane returning status=ok and result.data.<field>=<v>, stdout must
// equal EXACTLY <v>: no key name, no envelope, no quoting, no trailing
// newline.
func TestSecretRead(t *testing.T) {
	const field = "password"
	const value = "s3cr3t-p@ss"

	var captured callRequestBody
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			if err := json.NewDecoder(r.Body).Decode(&captured); err != nil {
				t.Errorf("decode body: %v", err)
				w.WriteHeader(400)
				return
			}
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opKVRead, DurationMs: 4,
				Result: json.RawMessage(`{"data":{"password":"s3cr3t-p@ss","other":"x"},"version":3}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "rdc-vault", "--field", field,
		"--backplane", srv.URL, "secret", "app/db",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v (stderr=%s)", err, errBuf.String())
	}

	// Exact-match: no added bytes whatsoever.
	if got := out.String(); got != value {
		t.Errorf("stdout must equal exactly %q with no decoration; got %q", value, got)
	}
	// The verb dispatches vault.kv.read against vault-1.x, not the broker.
	if captured.ConnectorID != vaultConnectorID {
		t.Errorf("connector_id: got %q want %q", captured.ConnectorID, vaultConnectorID)
	}
	if captured.OpID != opKVRead {
		t.Errorf("op_id: got %q want %q", captured.OpID, opKVRead)
	}
	// The target slug rides in the standard {"name": …} dict.
	if captured.Target == nil || captured.Target["name"] != "rdc-vault" {
		t.Errorf("target should carry name=rdc-vault; got %v", captured.Target)
	}
	// mount + path cross the wire; --field is client-side only.
	if captured.Params["mount"] != "secret" || captured.Params["path"] != "app/db" {
		t.Errorf("params should carry mount+path; got %v", captured.Params)
	}
	// reveal_secret opts out of the default credential_read response scrub
	// (#2467); without it the piped value would be the redaction sentinel.
	if captured.Params["reveal_secret"] != true {
		t.Errorf("params should carry reveal_secret=true (#2467); got %v", captured.Params)
	}
	if _, leaked := captured.Params["field"]; leaked {
		t.Errorf("field must NOT cross the wire (client-side extraction); got %v", captured.Params)
	}
}

// TestSecretReadNumericField — a numeric field renders as its exact
// source text (no float64 round-trip, no trailing newline).
func TestSecretReadNumericField(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"port":5432},"version":1}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "port", "--backplane", srv.URL, "secret", "p",
	})
	if err := cmd.Execute(); err != nil {
		t.Fatalf("execute: %v (stderr=%s)", err, errBuf.String())
	}
	if got := out.String(); got != "5432" {
		t.Errorf("numeric field: got %q want %q", got, "5432")
	}
}

// ---------- TTY-refuse guardrail ----------

// TestSecretReadRefusesTTY — with stdout a real terminal, the verb writes
// NOTHING to stdout, writes a refusal to stderr, and exits non-zero. It
// must refuse BEFORE any dispatch (no credential is even fetched).
func TestSecretReadRefusesTTY(t *testing.T) {
	dispatched := false
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			dispatched = true
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"password":"leak"},"version":1}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, true) // stdout is a terminal → must refuse

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "password", "--backplane", srv.URL, "secret", "p",
	})
	err := cmd.Execute()
	if err == nil {
		t.Fatalf("expected a non-nil (non-zero exit) error on a TTY refusal")
	}
	if out.Len() != 0 {
		t.Errorf("stdout must be empty on TTY refusal; got %q", out.String())
	}
	if !strings.Contains(errBuf.String(), "pipe-only") {
		t.Errorf("stderr should carry the pipe-only refusal; got %q", errBuf.String())
	}
	if dispatched {
		t.Errorf("the verb must refuse BEFORE dispatching — no credential should be fetched")
	}
}

// ---------- error isolation ----------

// TestSecretReadMissingField — a field absent from result.data writes
// NOTHING to stdout, a structured error to stderr, and exits non-zero.
func TestSecretReadMissingField(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"username":"admin"},"version":1}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "password", "--backplane", srv.URL, "secret", "p",
	})
	if err := cmd.Execute(); err == nil {
		t.Fatalf("a missing field must be a non-zero exit")
	}
	if out.Len() != 0 {
		t.Errorf("stdout must be empty when the field is missing; got %q", out.String())
	}
	if !strings.Contains(errBuf.String(), "password") {
		t.Errorf("stderr should name the missing field; got %q", errBuf.String())
	}
}

// TestSecretReadDispatchError — a status=error dispatch writes NOTHING to
// stdout, the connector error to stderr, and exits non-zero. A piped
// consumer must never receive the error string as a secret.
func TestSecretReadDispatchError(t *testing.T) {
	errMsg := "vault: permission denied"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "error", OpID: opKVRead, Error: &errMsg,
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "password", "--backplane", srv.URL, "secret", "p",
	})
	if err := cmd.Execute(); err == nil {
		t.Fatalf("a status=error dispatch must be a non-zero exit")
	}
	if out.Len() != 0 {
		t.Errorf("stdout must be empty on a dispatch error; got %q", out.String())
	}
	if !strings.Contains(errBuf.String(), "permission denied") {
		t.Errorf("stderr should carry the connector error; got %q", errBuf.String())
	}
}

// TestSecretReadObjectFieldRejected — a field whose value is a JSON
// object/array is not a single credential; the verb rejects it with an
// empty stdout and a stderr error rather than stringifying it.
func TestSecretReadObjectFieldRejected(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: "ok", OpID: opKVRead,
				Result: json.RawMessage(`{"data":{"nested":{"k":"v"}},"version":1}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "nested", "--backplane", srv.URL, "secret", "p",
	})
	if err := cmd.Execute(); err == nil {
		t.Fatalf("a non-scalar field must be a non-zero exit")
	}
	if out.Len() != 0 {
		t.Errorf("stdout must be empty for a non-scalar field; got %q", out.String())
	}
	if !strings.Contains(errBuf.String(), "scalar") {
		t.Errorf("stderr should explain the non-scalar rejection; got %q", errBuf.String())
	}
}

// ---------- unit: extractField / scalarToString directly ----------

// TestExtractFieldScalars pins the field-extraction helper's scalar
// rendering across string / int / bool without the cobra + HTTP scaffold.
func TestExtractFieldScalars(t *testing.T) {
	cases := []struct {
		name  string
		env   string
		field string
		want  string
	}{
		{"string", `{"data":{"k":"hunter2"}}`, "k", "hunter2"},
		{"empty-string", `{"data":{"k":""}}`, "k", ""},
		{"int", `{"data":{"k":42}}`, "k", "42"},
		{"bigint", `{"data":{"k":12345678901234567}}`, "k", "12345678901234567"},
		{"bool-true", `{"data":{"k":true}}`, "k", "true"},
		{"bool-false", `{"data":{"k":false}}`, "k", "false"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := extractField(json.RawMessage(tc.env), tc.field)
			if err != nil {
				t.Fatalf("extractField: %v", err)
			}
			if got != tc.want {
				t.Errorf("got %q want %q", got, tc.want)
			}
		})
	}
}

// TestExtractFieldErrors pins the failure modes: missing field, null
// value, non-scalar value, empty envelope.
func TestExtractFieldErrors(t *testing.T) {
	cases := []struct {
		name  string
		env   string
		field string
	}{
		{"missing", `{"data":{"a":"b"}}`, "k"},
		{"null", `{"data":{"k":null}}`, "k"},
		{"object", `{"data":{"k":{"x":1}}}`, "k"},
		{"array", `{"data":{"k":[1,2]}}`, "k"},
		{"no-data", `{"version":1}`, "k"},
		{"empty", `null`, "k"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if _, err := extractField(json.RawMessage(tc.env), tc.field); err == nil {
				t.Errorf("expected an error for %s; got nil", tc.name)
			}
		})
	}
}

// TestVaultConnectorFrozen pins the read verb's connector_id to the KV
// read op's registered connector — a drift here would dispatch against
// the wrong (or a non-existent) connector.
func TestVaultConnectorFrozen(t *testing.T) {
	if vaultConnectorID != "vault-1.x" {
		t.Fatalf("vaultConnectorID drifted: got %q want vault-1.x", vaultConnectorID)
	}
	if opKVRead != "vault.kv.read" {
		t.Fatalf("opKVRead drifted: got %q want vault.kv.read", opKVRead)
	}
}

// TestSecretReadNotFoundExitsOne — a status=not_found dispatch (#3591,
// #4986) is a known failed read: the envelope goes to stderr, stdout
// stays empty, and the exit is 1 via the shared sentinel (not exit 4).
func TestSecretReadNotFoundExitsOne(t *testing.T) {
	errMsg := "not_found: secret/p does not exist"
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: dispatch.StatusNotFound, OpID: opKVRead, Error: &errMsg,
				Extras: json.RawMessage(`{"error_code":"not_found","resource_ids":["secret/p"]}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs([]string{
		"--target", "v", "--field", "password", "--backplane", srv.URL, "secret", "p",
	})
	err := cmd.Execute()
	if !errors.Is(err, dispatch.ErrOpError) {
		t.Fatalf("not_found must exit 1 via dispatch.ErrOpError; got %v", err)
	}
	if out.Len() != 0 {
		t.Errorf("stdout must be empty on not_found; got %q", out.String())
	}
	for _, want := range []string{"not found: secret/p", "does not exist"} {
		if !strings.Contains(errBuf.String(), want) {
			t.Errorf("stderr missing %q; got %q", want, errBuf.String())
		}
	}
	if strings.Contains(errBuf.String(), "upgrade") {
		t.Errorf("not_found must not ask for a CLI upgrade; got %q", errBuf.String())
	}
}

// ---------- approved reads (--approval) ----------

const approvalStubID = "3f2b9c1e-1111-4000-8000-000000000042"

// exitCodeOf returns the exit code a rendered error carries, or -1.
func exitCodeOf(err error) int {
	var coded interface{ ExitCode() int }
	if errors.As(err, &coded) {
		return coded.ExitCode()
	}
	return -1
}

// runReadCmd runs `meho secret read` with args and returns stdout,
// stderr and the error.
func runReadCmd(t *testing.T, args ...string) (string, string, error) {
	t.Helper()
	cmd := newReadCmd()
	var out, errBuf bytes.Buffer
	cmd.SetOut(&out)
	cmd.SetErr(&errBuf)
	cmd.SetArgs(args)
	err := cmd.Execute()
	return out.String(), errBuf.String(), err
}

// TestSecretReadParkedNamesTheApprovalCommand — a read that waits for
// approval writes no value and tells the person how to get it later.
func TestSecretReadParkedNamesTheApprovalCommand(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/operations/call": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, dispatch.CallResult{
				Status: dispatch.StatusAwaitingApproval, OpID: opKVRead,
				Extras: json.RawMessage(`{"approval_request_id":"` + approvalStubID + `"}`),
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, errOut, err := runReadCmd(t,
		"--field", "password", "--backplane", srv.URL, "secret", "app/db")
	if err == nil {
		t.Fatalf("a waiting read must exit non-zero")
	}
	if out != "" {
		t.Errorf("stdout must be empty while the read waits; got %q", out)
	}
	want := "meho secret read --approval " + approvalStubID + " --field <name>"
	if !strings.Contains(errOut, want) {
		t.Errorf("stderr must name %q; got %q", want, errOut)
	}
}

// TestSecretReadApprovedWritesTheValue — after the approval, --approval
// runs the approved read once through the reveal route and writes only
// the field value. It sends no new read.
func TestSecretReadApprovedWritesTheValue(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + approvalStubID + "/reveal": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"approval_request_id": approvalStubID,
				"status":              "ok",
				"op_id":               opKVRead,
				"error":               nil,
				"audit_id":            nil,
				"result": map[string]any{
					"data":    map[string]any{"password": "s3cr3t-p@ss", "user": "root"},
					"version": 2,
				},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, errOut, err := runReadCmd(t,
		"--approval", approvalStubID, "--field", "password", "--backplane", srv.URL)
	if err != nil {
		t.Fatalf("approved read: %v (stderr %q)", err, errOut)
	}
	if out != "s3cr3t-p@ss" {
		t.Errorf("stdout = %q, want exactly the value", out)
	}
}

// TestSecretReadApprovedStillWaiting — not approved yet: the route
// refuses (409), no value, the reason on stderr, exit 4.
func TestSecretReadApprovedStillWaiting(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + approvalStubID + "/reveal": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 409, map[string]any{
				"detail": "reveal_not_approved: The request is pending. " +
					"The value can be fetched only after the approval.",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, errOut, err := runReadCmd(t,
		"--approval", approvalStubID, "--field", "password", "--backplane", srv.URL)
	if exitCodeOf(err) != 4 {
		t.Fatalf("a request that still waits must exit 4; got %v", err)
	}
	if out != "" {
		t.Errorf("stdout must be empty; got %q", out)
	}
	if !strings.Contains(errOut, "no value: reveal_not_approved: The request is pending") {
		t.Errorf("stderr must say the request still waits; got %q", errOut)
	}
}

// TestSecretReadApprovedUsedOnce — the second fetch is refused with a
// clear reason and no value: the approval works once.
func TestSecretReadApprovedUsedOnce(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + approvalStubID + "/reveal": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 409, map[string]any{
				"detail": "reveal_already_used: The value of this request was already fetched. " +
					"Send a new read to see it again.",
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, errOut, err := runReadCmd(t,
		"--approval", approvalStubID, "--field", "password", "--backplane", srv.URL)
	if exitCodeOf(err) != 4 {
		t.Fatalf("a used approval must exit 4; got %v", err)
	}
	if out != "" {
		t.Errorf("stdout must be empty; got %q", out)
	}
	if !strings.Contains(errOut, "already fetched") {
		t.Errorf("stderr must say the value was already fetched; got %q", errOut)
	}
}

// TestSecretReadApprovedNotOwner — another person gets exit 5, no value.
func TestSecretReadApprovedNotOwner(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + approvalStubID + "/reveal": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 403, map[string]any{"detail": "not_request_owner"})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, _, err := runReadCmd(t,
		"--approval", approvalStubID, "--field", "password", "--backplane", srv.URL)
	if exitCodeOf(err) != 5 {
		t.Fatalf("not the sender must exit 5; got %v", err)
	}
	if out != "" {
		t.Errorf("stdout must be empty; got %q", out)
	}
}

// TestSecretReadApprovedRejectsOtherOps — the result of another
// operation is not written as a secret value.
func TestSecretReadApprovedRejectsOtherOps(t *testing.T) {
	srv := mockBackplane(t, map[string]mockHandler{
		"POST /api/v1/approvals/" + approvalStubID + "/reveal": func(w http.ResponseWriter, r *http.Request) {
			writeJSON(t, w, 200, map[string]any{
				"approval_request_id": approvalStubID,
				"status":              "ok",
				"op_id":               "vault.kv.put",
				"result":              map[string]any{"data": map[string]any{"password": "x"}},
			})
		},
	})
	defer srv.Close()
	primeToken(t, srv.URL)
	withStdoutTTY(t, false)

	out, errOut, err := runReadCmd(t,
		"--approval", approvalStubID, "--field", "password", "--backplane", srv.URL)
	if err == nil || out != "" {
		t.Fatalf("another op's result must not be written; out=%q err=%v", out, err)
	}
	if !strings.Contains(errOut, "is not a secret read") {
		t.Errorf("stderr must say why; got %q", errOut)
	}
}

// TestSecretReadApprovalTakesNoArgs — with --approval the mount and path
// come from the request, so extra arguments are refused.
func TestSecretReadApprovalTakesNoArgs(t *testing.T) {
	withStdoutTTY(t, false)
	_, _, err := runReadCmd(t, "--approval", approvalStubID, "--field", "password", "secret", "app/db")
	if err == nil {
		t.Fatalf("--approval with mount and path must be refused")
	}
}

// TestSecretReadApprovedRefusesTTY — the pipe-only guard holds for an
// approved read too.
func TestSecretReadApprovedRefusesTTY(t *testing.T) {
	withStdoutTTY(t, true)
	out, _, err := runReadCmd(t, "--approval", approvalStubID, "--field", "password")
	if err == nil || out != "" {
		t.Fatalf("a terminal must get a refusal and no value; out=%q err=%v", out, err)
	}
}
