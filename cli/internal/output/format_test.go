// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package output

import (
	"bytes"
	"encoding/json"
	"errors"
	"strings"
	"testing"

	openapi_types "github.com/oapi-codegen/runtime/types"

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

// TestRenderHumanHealth_OperationCatalog: one extra line when another
// release wrote the operation rows (#5019); nothing new when this
// release wrote them, none is recorded, or the field is missing.
func TestRenderHumanHealth_OperationCatalog(t *testing.T) {
	base := func(c *api.OperationCatalogStatus) *api.HealthResponse {
		return &api.HealthResponse{
			Operator:         api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:            api.VaultStatus{Reachable: true, ReadOk: true},
			Db:               api.DbStatus{Migrated: ptrBool(true)},
			OperationCatalog: c,
		}
	}
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}
	other := render(base(&api.OperationCatalogStatus{
		State:            "other_release",
		ThisRelease:      "aaa111",
		WrittenByRelease: ptrStr("bbb222"),
	}))
	if !strings.Contains(other, "Operation settings: written by release bbb222, this server runs aaa111") {
		t.Errorf("expected the operation settings line, got:\n%s", other)
	}
	missing := render(base(nil))
	for _, state := range []string{"same_release", "not_recorded"} {
		got := render(base(&api.OperationCatalogStatus{State: state, ThisRelease: "aaa111"}))
		if got != missing {
			t.Errorf("state %s must print like no field:\n%s\n---\n%s", state, got, missing)
		}
	}
	if strings.Count(other, "\n") != strings.Count(missing, "\n")+1 {
		t.Errorf("another release adds exactly one line:\n%s\n---\n%s", other, missing)
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
		"  Approvals: who may approve depends on your group in the current tenant (see Who may approve in the approvals guide); decisions on someone else's instruction are refused; approval key configured\n",
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
		"  Approvals: break-glass is on: a person who may run changes may approve their own `safe` or `caution` change; a change of rights is not covered; a few operations need the Admin role, also with break-glass on; the group rules still apply; decisions on someone else's instruction are allowed; approval key missing\n",
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

// TestFormatApprovals_ByRole pins the whole approvals line for each role
// in the current tenant, with the emergency switch off and on. The texts
// follow the table "Who may approve" in the approvals guide.
func TestFormatApprovals_ByRole(t *testing.T) {
	tenant := openapi_types.UUID{1}
	const tail = "; decisions on someone else's instruction are refused; approval key configured"
	withRole := func(r api.TenantRole) api.OperatorIdentity {
		return api.OperatorIdentity{Sub: "s", TenantId: &tenant, TenantRole: r}
	}
	const neutral = "who may approve depends on your group in the current tenant (see Who may approve in the approvals guide)"
	const bgOther = "break-glass is on: a person who may run changes may approve their own `safe` or `caution` change; a change of rights is not covered; a few operations need the Admin role, also with break-glass on; the group rules still apply"
	cases := []struct {
		name    string
		op      api.OperatorIdentity
		posture string
		want    string
	}{
		{"admin", withRole(api.TenantRoleTenantAdmin), "four_eyes_enforced",
			"you may approve your own changes (a change of rights only with a click in the web console; in MEHO's own login realm a change of rights needs a platform admin, one you sent with a token needs a second person who is a platform admin, and so does one that can change the approval sign-in, which never runs alone, also not for a platform admin); MEHO refuses a change that resets or sets the password of your own login"},
		{"operator", withRole(api.TenantRoleOperator), "four_eyes_enforced",
			"a second person approves your changes; you may approve your own `caution` change that waited because you sent it with a token, with a click in the web console; a change of rights is not covered; a few operations, for example registering a target, need the Admin role: you cannot run them or approve them"},
		{"reader", withRole(api.TenantRoleReadOnly), "four_eyes_enforced",
			"you cannot run changes"},
		{"unknown role", withRole(api.TenantRole("auditor")), "four_eyes_enforced", neutral},
		{"empty role", withRole(api.TenantRole("")), "four_eyes_enforced", neutral},
		{"no current tenant", api.OperatorIdentity{Sub: "s", TenantRole: api.TenantRoleTenantAdmin}, "four_eyes_enforced", neutral},
		{"admin, break-glass", withRole(api.TenantRoleTenantAdmin), "single_operator_break_glass",
			"you may approve your own changes (a change of rights only with a click in the web console; in MEHO's own login realm a change of rights needs a platform admin, one you sent with a token needs a second person who is a platform admin, and so does one that can change the approval sign-in, which never runs alone, also not for a platform admin); MEHO refuses a change that resets or sets the password of your own login; break-glass is on: anyone may approve their own `safe` or `caution` change; the switch does not cover a change of rights or an operation that needs the Admin role"},
		{"operator, break-glass", withRole(api.TenantRoleOperator), "single_operator_break_glass",
			"break-glass is on: you may approve your own `safe` or `caution` change; `dangerous` and `destructive` changes still need a second person; a change of rights is not covered; a few operations, for example registering a target, need the Admin role: you cannot run them or approve them, also with break-glass on"},
		{"reader, break-glass", withRole(api.TenantRoleReadOnly), "single_operator_break_glass",
			"you cannot run changes"},
		{"unknown role, break-glass", withRole(api.TenantRole("auditor")), "single_operator_break_glass", bgOther},
		{"no current tenant, break-glass", api.OperatorIdentity{Sub: "s"}, "single_operator_break_glass", bgOther},
		{"new posture value", withRole(api.TenantRoleTenantAdmin), "something_new", "approval posture something_new"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			a := api.ApprovalsStatus{
				Custody:                  "configured",
				EffectivePosture:         tc.posture,
				DelegatedDecisionPosture: "production_own_hand_only",
			}
			if got, want := formatApprovals(a, tc.op), tc.want+tail; got != want {
				t.Errorf("formatApprovals() = %q, want %q", got, want)
			}
		})
	}
}

// TestRenderHumanHealth_ApprovalsUseCurrentTenantRole checks that the
// status line takes the role of the current tenant from the answer.
func TestRenderHumanHealth_ApprovalsUseCurrentTenantRole(t *testing.T) {
	tenant := openapi_types.UUID{2}
	resp := &api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "s", TenantId: &tenant, TenantRole: api.TenantRoleReadOnly},
		Vault:    api.VaultStatus{Reachable: true, ReadOk: true},
		Db:       api.DbStatus{Migrated: ptrBool(true)},
		Approvals: &api.ApprovalsStatus{
			Custody:                  "configured",
			EffectivePosture:         "four_eyes_enforced",
			DelegatedDecisionPosture: "production_own_hand_only",
		},
	}
	var buf bytes.Buffer
	if err := PrintHealth(&buf, resp); err != nil {
		t.Fatalf("PrintHealth: %v", err)
	}
	want := "  Approvals: you cannot run changes; decisions on someone else's instruction are refused; approval key configured\n"
	if !strings.Contains(buf.String(), want) {
		t.Errorf("missing %q in:\n%s", want, buf.String())
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

// TestRenderHumanHealth_Tenants: the person's tenants are listed with
// the role in each, and a login with several tenants that chose none
// gets the list, a hint, the DB line and the neutral approvals line only
// (#5239). The answer carries no approval settings in that case.
func TestRenderHumanHealth_Tenants(t *testing.T) {
	tenantA := api.TenantEntry{Name: "lab-a", TenantRole: api.TenantRoleReadOnly}
	tenantB := api.TenantEntry{Name: "lab-b", TenantRole: api.TenantRoleOperator}
	render := func(resp *api.HealthResponse) string {
		var buf bytes.Buffer
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}

	unchosen := render(&api.HealthResponse{
		Operator: api.OperatorIdentity{Sub: "alice", Tenants: &[]api.TenantEntry{tenantA, tenantB}},
		Vault:    api.VaultStatus{Detail: ptrStr("tenant_not_chosen")},
		Db:       api.DbStatus{Migrated: ptrBool(true)},
	})
	for _, want := range []string{
		"  Tenants:\n    lab-a  read_only\n    lab-b  operator\n",
		"No tenant chosen. Choose one with `meho tenants use <name>`.",
		"DB:    migrated",
		"  Approvals: who may approve depends on your group in the current tenant (see Who may approve in the approvals guide)\n",
	} {
		if !strings.Contains(unchosen, want) {
			t.Errorf("missing %q in output:\n%s", want, unchosen)
		}
	}
	if strings.Contains(unchosen, "Vault:") {
		t.Errorf("unchosen output must not show the Vault line:\n%s", unchosen)
	}

	tenantB.Current = true
	chosen := render(&api.HealthResponse{
		Operator: api.OperatorIdentity{
			Sub:      "alice",
			TenantId: &tenantB.TenantId,
			Tenants:  &[]api.TenantEntry{tenantA, tenantB},
		},
		Vault: api.VaultStatus{Reachable: true, ReadOk: true},
		Db:    api.DbStatus{Migrated: ptrBool(true)},
	})
	if !strings.Contains(chosen, "    lab-b  operator  (current)\n") {
		t.Errorf("current tenant not marked:\n%s", chosen)
	}
	if !strings.Contains(chosen, "Vault: reachable") || strings.Contains(chosen, "No tenant chosen") {
		t.Errorf("chosen output wrong:\n%s", chosen)
	}
}

// TestRenderHumanHealth_AgentModelSpend: one warning line when MEHO has
// no price for the default agent model (#5771); nothing new when it has
// one, and nothing new when an older backplane sends no field.
func TestRenderHumanHealth_AgentModelSpend(t *testing.T) {
	render := func(spend *api.AgentModelSpend) string {
		var buf bytes.Buffer
		resp := &api.HealthResponse{
			Operator:        api.OperatorIdentity{Sub: "alice-sub-id"},
			Vault:           api.VaultStatus{Reachable: true, ReadOk: true},
			Db:              api.DbStatus{Migrated: ptrBool(true)},
			AgentModelSpend: spend,
		}
		if err := PrintHealth(&buf, resp); err != nil {
			t.Fatalf("PrintHealth: %v", err)
		}
		return buf.String()
	}
	unpriced := render(&api.AgentModelSpend{Model: "anthropic:claude-new-model", SpendCounted: false})
	want := "Agent model: anthropic:claude-new-model has no price in MEHO, so spend limits do not count its runs"
	if !strings.Contains(unpriced, want) {
		t.Errorf("expected the warning line, got:\n%s", unpriced)
	}
	priced := render(&api.AgentModelSpend{Model: "anthropic:claude-sonnet-5-5", SpendCounted: true})
	missing := render(nil)
	if priced != missing {
		t.Errorf("a priced model and no field must print the same:\n%s\n---\n%s", priced, missing)
	}
	if strings.Contains(priced, "Agent model") {
		t.Errorf("no line when the spend is counted, got:\n%s", priced)
	}
	if strings.Count(unpriced, "\n") != strings.Count(priced, "\n")+1 {
		t.Errorf("an unpriced model adds exactly one line:\n%s\n---\n%s", unpriced, priced)
	}
}
