// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package migrate

import (
	"testing"
)

// TestSuggestScope exercises the full mapping table including the
// tenantConfigured branch for type "project" and the unknown-type
// fallback.
func TestSuggestScope(t *testing.T) {
	cases := []struct {
		name             string
		memType          string
		tenantConfigured bool
		want             string
	}{
		// type: user → ScopeUser regardless of tenant.
		{name: "user no tenant", memType: "user", tenantConfigured: false, want: ScopeUser},
		{name: "user with tenant", memType: "user", tenantConfigured: true, want: ScopeUser},

		// type: feedback → ScopeUser.
		{name: "feedback no tenant", memType: "feedback", tenantConfigured: false, want: ScopeUser},
		{name: "feedback with tenant", memType: "feedback", tenantConfigured: true, want: ScopeUser},

		// type: project → ScopeUserTenant when tenant configured, ScopeUser otherwise.
		{name: "project no tenant", memType: "project", tenantConfigured: false, want: ScopeUser},
		{name: "project with tenant", memType: "project", tenantConfigured: true, want: ScopeUserTenant},

		// type: reference → ScopeUser.
		{name: "reference no tenant", memType: "reference", tenantConfigured: false, want: ScopeUser},
		{name: "reference with tenant", memType: "reference", tenantConfigured: true, want: ScopeUser},

		// unknown type → ScopeUser (safe default).
		{name: "unknown empty", memType: "", tenantConfigured: false, want: ScopeUser},
		{name: "unknown string", memType: "something-else", tenantConfigured: false, want: ScopeUser},
		{name: "unknown with tenant", memType: "custom", tenantConfigured: true, want: ScopeUser},
	}

	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			mf := MemoryFile{Type: tc.memType}
			got := SuggestScope(mf, tc.tenantConfigured)
			if got != tc.want {
				t.Errorf("SuggestScope(type=%q, tenant=%v): got %q want %q",
					tc.memType, tc.tenantConfigured, got, tc.want)
			}
		})
	}
}

// TestScopeConstantsAreDistinct guards against accidental equal values.
func TestScopeConstantsAreDistinct(t *testing.T) {
	all := []string{ScopeUser, ScopeUserTenant, ScopeUserTarget, ScopeTenant, ScopeTarget}
	seen := map[string]bool{}
	for _, s := range all {
		if seen[s] {
			t.Errorf("duplicate scope constant value: %q", s)
		}
		seen[s] = true
	}
}
