// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	"github.com/evoila/meho/cli/internal/api"
)

func ptrStr(s string) *string { return &s }
func ptrBool(b bool) *bool    { return &b }

// TestRenderHumanHealth pins the human-formatter contract. The
// install.sh smoke test reads the first line to grep for "Logged
// in as"; renaming that label is a wire-compat break.
func TestRenderHumanHealth(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{
			Sub:   "alice-sub-id",
			Email: ptrStr("alice@example.com"),
		},
		Vault: api.VaultStatus{Reachable: true, ReadOk: true, Detail: ptrStr("version=42")},
		Db:    api.DbStatus{Migrated: ptrBool(true)},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	got := buf.String()
	for _, want := range []string{
		"Logged in as alice@example.com (sub: alice-sub-id)",
		"Vault: reachable, read OK (version=42)",
		"DB:    migrated",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in human output:\n%s", want, got)
		}
	}
}

// TestRenderHumanHealth_SandboxMode: one extra line when the backplane
// says the instance is in sandbox mode (#4622); nothing new when it is
// off, and nothing new when an older backplane sends no field.
func TestRenderHumanHealth_SandboxMode(t *testing.T) {
	base := func(mode *string) *api.HealthResponse {
		return &api.HealthResponse{
			Operator:    api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:       api.VaultStatus{Reachable: true, ReadOk: true},
			Db:          api.DbStatus{Migrated: ptrBool(true)},
			SandboxMode: mode,
		}
	}
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}
	on := render(base(ptrStr("on")))
	if !strings.Contains(on, "Sandbox mode: on") {
		t.Errorf("expected the sandbox line when the mode is on, got:\n%s", on)
	}
	off := render(base(ptrStr("off")))
	missing := render(base(nil))
	if off != missing {
		t.Errorf("mode off and no field must print the same:\n%s\n---\n%s", off, missing)
	}
	if strings.Contains(off, "Sandbox mode") {
		t.Errorf("no sandbox line when the mode is off, got:\n%s", off)
	}
	if strings.Count(on, "\n") != strings.Count(off, "\n")+1 {
		t.Errorf("the mode on adds exactly one line:\n%s\n---\n%s", on, off)
	}
}

// TestRenderHumanHealth_ConnectorPackages: `meho status` lists the
// connector packages the image carries (#5036), prints "none" for the
// plain public image, and prints nothing when an older backplane sends
// no field.
func TestRenderHumanHealth_ConnectorPackages(t *testing.T) {
	base := func(pkgs *[]api.ConnectorPackage) *api.HealthResponse {
		return &api.HealthResponse{
			Operator:          api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:             api.VaultStatus{Reachable: true, ReadOk: true},
			Db:                api.DbStatus{Migrated: ptrBool(true)},
			ConnectorPackages: pkgs,
		}
	}
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}
	digest := "sha256:" + strings.Repeat("a", 64)
	listed := render(base(&[]api.ConnectorPackage{
		{Name: "example-connectors", Version: "1.2.0", WheelDigest: digest, Folders: []string{"example_widget"}},
	}))
	if !strings.Contains(listed, "  Connector packages:\n    example-connectors 1.2.0 ("+digest+")\n") {
		t.Errorf("expected the package line, got:\n%s", listed)
	}
	none := render(base(&[]api.ConnectorPackage{}))
	if !strings.Contains(none, "  Connector packages: none\n") {
		t.Errorf("expected none for the plain image, got:\n%s", none)
	}
	if older := render(base(nil)); strings.Contains(older, "Connector packages") {
		t.Errorf("no line when the backplane sends no field, got:\n%s", older)
	}
}

// TestFormatDB pins the DB line for a database at the expected
// revision, behind it, ahead of it (normal after a rollback), with no
// revision, and for the failure and old-backplane shapes (#4830).
func TestFormatDB(t *testing.T) {
	cases := []struct {
		name string
		db   api.DbStatus
		want string
	}{
		{"unknown", api.DbStatus{}, "unknown"},
		{"old backplane migrated", api.DbStatus{Migrated: ptrBool(true)}, "migrated"},
		{"old backplane not migrated", api.DbStatus{Migrated: ptrBool(false)}, "not migrated"},
		{
			"at head",
			api.DbStatus{Migrated: ptrBool(true), Revision: ptrStr("0115"), Head: ptrStr("0115"), Detail: ptrStr("revision=0115")},
			"migrated, revision 0115",
		},
		{
			"behind",
			api.DbStatus{Migrated: ptrBool(false), Revision: ptrStr("0114"), Head: ptrStr("0115"), Detail: ptrStr("current=0114 head=0115")},
			"not migrated (database at 0114, this version expects 0115)",
		},
		{
			"ahead after rollback",
			api.DbStatus{Migrated: ptrBool(true), Revision: ptrStr("0116"), Head: ptrStr("0115"), Detail: ptrStr("current=0116 head=0115 db_ahead=true")},
			"migrated, revision 0116 (newer than this version, which expects 0115; normal after a rollback)",
		},
		{
			"fresh database",
			api.DbStatus{Migrated: ptrBool(false), Head: ptrStr("0115"), Detail: ptrStr("current=None head=0115")},
			"not migrated (database has no revision, this version expects 0115)",
		},
		{
			"database unreachable",
			api.DbStatus{Migrated: ptrBool(false), Head: ptrStr("0115"), Detail: ptrStr("check_failed: OperationalError")},
			"not migrated (check_failed: OperationalError)",
		},
		{
			"vector extension missing",
			api.DbStatus{Migrated: ptrBool(false), Revision: ptrStr("0115"), Head: ptrStr("0115"), Detail: ptrStr("revision=0115 pgvector=missing")},
			"not migrated (revision=0115 pgvector=missing)",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := formatDB(tc.db); got != tc.want {
				t.Errorf("formatDB() = %q, want %q", got, tc.want)
			}
		})
	}
}

// TestRenderHumanHealth_UpgradeDetails pins the readiness, approvals
// and features lines (#4830), and that an older backplane's response
// without those fields prints exactly as before.
func TestRenderHumanHealth_UpgradeDetails(t *testing.T) {
	base := func() *api.HealthResponse {
		return &api.HealthResponse{
			Operator: api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:    api.VaultStatus{Reachable: true, ReadOk: true},
			Db:       api.DbStatus{Migrated: ptrBool(true), Revision: ptrStr("0115"), Head: ptrStr("0115")},
		}
	}
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}

	old := render(base())
	for _, label := range []string{"Readiness", "Approvals", "Features"} {
		if strings.Contains(old, label) {
			t.Errorf("an old response must not print a %s line:\n%s", label, old)
		}
	}
	if strings.Count(old, "\n") != 3 {
		t.Errorf("an old response prints three lines:\n%s", old)
	}

	ready := base()
	ready.Readiness = &api.ReadinessStatus{Ready: true, Checks: &[]api.ReadinessCheck{
		{Name: "keycloak", Ok: true}, {Name: "vault", Ok: true}, {Name: "db", Ok: true, Detail: ptrStr("revision=0115")},
	}}
	ready.Approvals = &api.ApprovalsStatus{
		Custody:                  "configured",
		EffectivePosture:         "four_eyes_enforced",
		DelegatedDecisionPosture: "production_own_hand_only",
	}
	ready.Features = &[]api.FeatureStatus{
		{Name: "agent_runtime", Configured: true, MissingEnv: &[]string{}},
		{Name: "mcp", Configured: true},
	}
	got := render(ready)
	for _, want := range []string{
		"  DB:    migrated, revision 0115\n",
		"  Readiness: ready (keycloak, vault, db ok)\n",
		"  Approvals: a second person must approve; decisions on someone else's instruction are refused; approval key configured\n",
		"  Features: all configured\n",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}

	notReady := base()
	notReady.Readiness = &api.ReadinessStatus{Ready: false, Checks: &[]api.ReadinessCheck{
		{Name: "keycloak", Ok: true},
		{Name: "vault", Ok: false, Detail: ptrStr("unreachable: ConnectError")},
		{Name: "db", Ok: true},
	}}
	notReady.Approvals = &api.ApprovalsStatus{
		Custody:                  "missing",
		EffectivePosture:         "single_operator_break_glass",
		DelegatedDecisionPosture: "lab_class_delegation_allowed",
	}
	notReady.Features = &[]api.FeatureStatus{
		{Name: "agent_runtime", Configured: false, MissingEnv: &[]string{"KEYCLOAK_ADMIN_CLIENT_ID", "KEYCLOAK_ADMIN_CLIENT_SECRET"}},
		{Name: "approval_custody", Configured: false, MissingEnv: &[]string{"APPROVAL_HANDOFF_ENCRYPTION_KEY"}},
		{Name: "mcp", Configured: true},
	}
	got = render(notReady)
	for _, want := range []string{
		"  Readiness: not ready\n",
		"    vault: failed (unreachable: ConnectError)\n",
		"    ok: keycloak, db\n",
		"  Approvals: one operator may approve their own request (break-glass); decisions on someone else's instruction are allowed; approval key missing\n",
		"  Features: not configured: agent_runtime (needs KEYCLOAK_ADMIN_CLIENT_ID, KEYCLOAK_ADMIN_CLIENT_SECRET), approval_custody (needs APPROVAL_HANDOFF_ENCRYPTION_KEY)\n",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in:\n%s", want, got)
		}
	}

	empty := base()
	empty.Readiness = &api.ReadinessStatus{Ready: false}
	if got := render(empty); !strings.Contains(got, "  Readiness: not ready (no checks reported)\n") {
		t.Errorf("empty check list must say so:\n%s", got)
	}
}

// TestRenderHumanHealth_NoEmail confirms the fallback path (no
// email → name → sub) renders the sub alone without the trailing
// "(sub: ...)" tail.
func TestRenderHumanHealth_NoEmail(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "service-account-1"},
		Vault:    api.VaultStatus{Reachable: false, Detail: ptrStr("login_failed: ConnectionRefused")},
		Db:       api.DbStatus{Migrated: ptrBool(false)},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	got := buf.String()
	if !strings.Contains(got, "Logged in as service-account-1\n") {
		t.Errorf("expected bare-sub identity line, got:\n%s", got)
	}
	if !strings.Contains(got, "Vault: unreachable (login_failed: ConnectionRefused)") {
		t.Errorf("expected unreachable rendering, got:\n%s", got)
	}
	if !strings.Contains(got, "DB:    not migrated") {
		t.Errorf("expected not-migrated rendering, got:\n%s", got)
	}
}

// TestPrintJSON confirms the JSON path emits a parseable document
// terminated by a newline. The install.sh smoke test pipes
// `meho status --json | jq .` — a missing trailing newline merges
// the JSON with the next shell prompt and breaks the pipe.
func TestPrintJSON(t *testing.T) {
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "x"},
		Vault:    api.VaultStatus{Reachable: true, ReadOk: true},
		Db:       api.DbStatus{Migrated: ptrBool(true)},
	}
	var buf bytes.Buffer
	if err := PrintJSON(&buf, resp); err != nil {
		t.Fatalf("PrintJSON: %v", err)
	}
	out := buf.String()
	if !strings.HasSuffix(out, "\n") {
		t.Errorf("expected trailing newline, got: %q", out)
	}
	var got api.HealthResponse
	if err := json.Unmarshal([]byte(strings.TrimSpace(out)), &got); err != nil {
		t.Fatalf("output is not valid JSON: %v\noutput:\n%s", err, out)
	}
	if got.Operator.Sub != "x" {
		t.Errorf("operator sub roundtrip failed; got %+v", got)
	}
}

// TestRenderError_JSONPath confirms the JSON error envelope: shape
// + stderr-only emission + silent error to suppress cobra's
// default printer + exit-code propagation via ExitCoder.
func TestRenderError_JSONPath(t *testing.T) {
	var stderr bytes.Buffer
	err := RenderError(&stderr, AuthExpired("no creds for backplane"), true)
	if err == nil {
		t.Fatal("expected non-nil error")
	}
	if err.Error() != "" {
		t.Errorf("JSON path should return a silentError with no message; got %q", err.Error())
	}
	var coder ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != ExitAuthExpired {
		t.Errorf("expected exit %d, got %d", ExitAuthExpired, coder.ExitCode())
	}
	var envelope map[string]any
	if jerr := json.Unmarshal(bytes.TrimSpace(stderr.Bytes()), &envelope); jerr != nil {
		t.Fatalf("stderr is not valid JSON: %v\nstderr:\n%s", jerr, stderr.String())
	}
	if envelope["error"] != "auth_expired" {
		t.Errorf("expected error=auth_expired, got %v", envelope["error"])
	}
	if envelope["detail"] != "no creds for backplane" {
		t.Errorf("expected detail prose, got %v", envelope["detail"])
	}
	if envelope["exit_code"] != float64(ExitAuthExpired) {
		t.Errorf("expected exit_code=%d, got %v", ExitAuthExpired, envelope["exit_code"])
	}
}

// TestRenderError_HumanPath confirms the human error path: the
// one-line "meho: <code>: <detail>" rendering goes to stderr, the
// returned error is silent (so cobra's default printer doesn't
// double-render), and ExitCoder propagates the exit code.
func TestRenderError_HumanPath(t *testing.T) {
	var stderr bytes.Buffer
	err := RenderError(&stderr, Unreachable("dial tcp: connect: refused"), false)
	got := stderr.String()
	for _, want := range []string{"unreachable", "dial tcp"} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in stderr:\n%s", want, got)
		}
	}
	if err.Error() != "" {
		t.Errorf("human path should return silentError (empty .Error()), got %q", err.Error())
	}
	var coder ExitCoder
	if !errors.As(err, &coder) {
		t.Fatalf("expected ExitCoder, got %T", err)
	}
	if coder.ExitCode() != ExitUnreachable {
		t.Errorf("expected exit %d, got %d", ExitUnreachable, coder.ExitCode())
	}
}

// TestExitCodes pins the v0.1 exit-code contract. install.sh
// branches on these values; renumbering is a wire-compat break.
func TestExitCodes(t *testing.T) {
	cases := []struct {
		name string
		err  *StructuredError
		want int
	}{
		{"auth_expired", AuthExpired(""), 2},
		{"unreachable", Unreachable(""), 3},
		{"unexpected", Unexpected(""), 4},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if c.err.ExitCode() != c.want {
				t.Errorf("ExitCode() = %d, want %d", c.err.ExitCode(), c.want)
			}
		})
	}
}
