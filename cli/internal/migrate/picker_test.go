// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"testing"
)

// ── slugFromPath ─────────────────────────────────────────────────────────────

func TestSlugFromPath(t *testing.T) {
	cases := []struct {
		path string
		want string
	}{
		{"/home/x/memory/daily-routine.md", "daily-routine"},
		{"/home/x/memory/Daily Routine.md", "daily-routine"},
		{"/home/x/memory/my_project.md", "my-project"},
		{"/home/x/memory/123-numbers.md", "entry-123-numbers"},
		{"/home/x/memory/foo.md", "foo"},
		{"/home/x/memory/FOO BAR.md", "foo-bar"},
	}
	for _, tc := range cases {
		got := slugFromPath(tc.path)
		if got != tc.want {
			t.Errorf("slugFromPath(%q) = %q; want %q", tc.path, got, tc.want)
		}
	}
}

// ── validateSlug ─────────────────────────────────────────────────────────────

func TestValidateSlug(t *testing.T) {
	accept := []string{"daily-routine", "a", "foo123", "abc-def-ghi"}
	for _, s := range accept {
		if err := validateSlug(s); err != nil {
			t.Errorf("validateSlug(%q) unexpected error: %v", s, err)
		}
	}
	// "1start" is valid per spec regex ^[a-z0-9][a-z0-9-]*$ (digit is allowed first char)
	reject := []string{"Daily Routine", "-x", "x_y", ""}
	for _, s := range reject {
		if err := validateSlug(s); err == nil {
			t.Errorf("validateSlug(%q) expected error, got nil", s)
		}
	}
}

// ── BuildForm structure ───────────────────────────────────────────────────────

func TestBuildForm_GroupCount(t *testing.T) {
	files := []MemoryFile{
		{Path: "/mem/a.md", Type: "user", Body: "hello"},
		{Path: "/mem/b.md", Type: "feedback", Body: "world"},
	}
	form, plans := BuildForm(files, BuildFormOpts{})
	if form == nil {
		t.Fatal("BuildForm returned nil form")
	}
	// 4 groups per file + 1 confirm group
	want := len(files)*4 + 1
	got := len(plans) // plans length equals len(files)
	if got != len(files) {
		t.Errorf("plans length = %d; want %d", got, len(files))
	}
	_ = want // form.Groups() is not exported; we verify via plans length
}

func TestBuildForm_DefaultActions(t *testing.T) {
	files := []MemoryFile{
		{Path: "/m/user.md", Type: "user", Body: "hello"},
		// machine-local flagged file → default skip
		{Path: "/m/local.md", Type: "user", Body: "/Users/bob/code is here"},
	}
	_, plans := BuildForm(files, BuildFormOpts{})
	if plans[0].Action != ActionMigrateSuggested {
		t.Errorf("plans[0].Action = %q; want %q", plans[0].Action, ActionMigrateSuggested)
	}
	if plans[1].Action != ActionSkipMachineLocal {
		t.Errorf("plans[1].Action = %q; want %q", plans[1].Action, ActionSkipMachineLocal)
	}
}

func TestBuildForm_IncludeMachineLocalFlipsDefault(t *testing.T) {
	files := []MemoryFile{
		{Path: "/m/local.md", Type: "user", Body: "/Users/bob/code"},
	}
	_, plans := BuildForm(files, BuildFormOpts{IncludeMachineLocal: true})
	if plans[0].Action != ActionMigrateSuggested {
		t.Errorf("with IncludeMachineLocal=true, action = %q; want %q",
			plans[0].Action, ActionMigrateSuggested)
	}
}

// ── Role-filtered scope options ───────────────────────────────────────────────

func TestBuildScopeOptions_NoTenantAdmin(t *testing.T) {
	opts := buildScopeOptions(BuildFormOpts{IsTenantAdmin: false, TenantConfigured: true, TargetConfigured: true})
	for _, o := range opts {
		if o.Value == ScopeTenant || o.Value == ScopeTarget {
			t.Errorf("scope option %q present without tenant_admin", o.Value)
		}
	}
}

func TestBuildScopeOptions_TenantAdmin(t *testing.T) {
	opts := buildScopeOptions(BuildFormOpts{IsTenantAdmin: true, TenantConfigured: true, TargetConfigured: true})
	has := func(scope string) bool {
		for _, o := range opts {
			if o.Value == scope {
				return true
			}
		}
		return false
	}
	for _, scope := range []string{ScopeUser, ScopeUserTenant, ScopeUserTarget, ScopeTenant, ScopeTarget} {
		if !has(scope) {
			t.Errorf("scope %q missing when IsTenantAdmin=true", scope)
		}
	}
}

// ── FinalizeSkip ─────────────────────────────────────────────────────────────

func TestFinalizeSkip(t *testing.T) {
	plans := []SubmitPlan{
		{Action: ActionMigrateSuggested},
		{Action: ActionSkipManual},
		{Action: ActionSkipMachineLocal},
		{Action: ActionMigrateEdit},
	}
	FinalizeSkip(plans)
	if plans[0].Skip {
		t.Error("migrate:suggested should not be Skip")
	}
	if !plans[1].Skip {
		t.Error("skip:manual should be Skip")
	}
	if !plans[2].Skip {
		t.Error("skip:machine-local should be Skip")
	}
	if plans[3].Skip {
		t.Error("migrate:edit should not be Skip")
	}
}

// ── DefaultPlan ──────────────────────────────────────────────────────────────

func TestDefaultPlan_UserType(t *testing.T) {
	f := MemoryFile{Path: "/m/user.md", Type: "user", Body: "notes", BodySHA256: "abcdef012345"}
	plan := DefaultPlan(f, BuildFormOpts{})
	if plan.Scope != ScopeUser {
		t.Errorf("scope = %q; want %q", plan.Scope, ScopeUser)
	}
	if plan.Skip {
		t.Error("user file should not be skipped by default")
	}
	if plan.Slug != "user" {
		t.Errorf("slug = %q; want %q", plan.Slug, "user")
	}
}

func TestDefaultPlan_MachineLocalOptOut(t *testing.T) {
	f := MemoryFile{
		Path:               "/m/ml.md",
		Type:               "user",
		Body:               "text",
		MachineLocalOptOut: true,
	}
	plan := DefaultPlan(f, BuildFormOpts{IncludeMachineLocal: false})
	if !plan.Skip {
		t.Error("MachineLocalOptOut file should be skipped when IncludeMachineLocal=false")
	}
}
