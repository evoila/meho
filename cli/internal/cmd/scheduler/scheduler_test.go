// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package scheduler

import (
	"strings"
	"testing"
)

func TestNewRootCmd_Subcommands(t *testing.T) {
	cmd := NewRootCmd()
	if cmd.Use != "scheduler" {
		t.Fatalf("expected Use=scheduler, got %q", cmd.Use)
	}
	want := map[string]bool{"list": true, "create": true, "cancel <trigger_id>": true}
	for _, sub := range cmd.Commands() {
		if !want[sub.Use] {
			t.Errorf("unexpected subcommand %q", sub.Use)
		}
		delete(want, sub.Use)
	}
	if len(want) != 0 {
		t.Errorf("missing subcommands: %v", want)
	}
}

func TestBuildListPath(t *testing.T) {
	tests := []struct {
		name string
		opts listOptions
		// substrings the path should contain (order-independent)
		contains []string
		// substrings the path must NOT contain
		notContains []string
	}{
		{
			name:        "no_filters",
			opts:        listOptions{},
			contains:    []string{"/api/v1/scheduler/triggers"},
			notContains: []string{"?"},
		},
		{
			name:     "kind_and_status",
			opts:     listOptions{Kind: "cron", Status: "active"},
			contains: []string{"kind=cron", "status=active"},
		},
		{
			name:     "tenant_filter",
			opts:     listOptions{Tenant: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
			contains: []string{"tenant_filter=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
		},
		{
			name:     "limit_and_offset",
			opts:     listOptions{Limit: 25, Offset: 50},
			contains: []string{"limit=25", "offset=50"},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := buildListPath(tc.opts)
			for _, sub := range tc.contains {
				if !strings.Contains(got, sub) {
					t.Errorf("expected %q to contain %q", got, sub)
				}
			}
			for _, sub := range tc.notContains {
				if strings.Contains(got, sub) {
					t.Errorf("expected %q NOT to contain %q", got, sub)
				}
			}
		})
	}
}

func TestBuildCancelPath(t *testing.T) {
	tests := []struct {
		name string
		opts cancelOptions
		want string
	}{
		{
			name: "no_tenant",
			opts: cancelOptions{TriggerID: "abc-123"},
			want: "/api/v1/scheduler/triggers/abc-123",
		},
		{
			name: "with_tenant",
			opts: cancelOptions{
				TriggerID: "abc-123",
				Tenant:    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
			},
			want: "/api/v1/scheduler/triggers/abc-123?tenant_filter=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := buildCancelPath(tc.opts); got != tc.want {
				t.Errorf("got %q want %q", got, tc.want)
			}
		})
	}
}

func TestValidKinds(t *testing.T) {
	for _, k := range []string{"cron", "one_off", "event"} {
		if !validKinds[k] {
			t.Errorf("expected %q to be a valid kind", k)
		}
	}
	if validKinds["bogus"] {
		t.Errorf("expected 'bogus' to not be a valid kind")
	}
}

func TestValidStatuses(t *testing.T) {
	for _, s := range []string{"active", "paused", "cancelled", "fired"} {
		if !validStatuses[s] {
			t.Errorf("expected %q to be a valid status", s)
		}
	}
}

func TestValidInFlightPolicies(t *testing.T) {
	for _, p := range []string{"fail_into_audit", "resume"} {
		if !validInFlightPolicies[p] {
			t.Errorf("expected %q to be a valid in-flight policy", p)
		}
	}
}
