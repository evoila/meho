// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package agent

import (
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"

	"github.com/evoila/meho/cli/internal/api"
)

func TestBuildGrantCreateBodyMinimal(t *testing.T) {
	body, err := buildGrantCreateBody("agent:scout", "*", "auto-execute", "", "")
	if err != nil {
		t.Fatalf("buildGrantCreateBody: %v", err)
	}
	if body.PrincipalSub != "agent:scout" || body.OpPattern != "*" {
		t.Errorf("scalar fields: %+v", body)
	}
	if string(body.Verdict) != "auto-execute" {
		t.Errorf("verdict: got %q", body.Verdict)
	}
	if body.ExpiresAt != nil {
		t.Errorf("ExpiresAt should be nil for permanent grant; got %+v", body.ExpiresAt)
	}
	if body.TargetScope != nil {
		t.Errorf("TargetScope should be nil when --target absent; got %+v", body.TargetScope)
	}
}

func TestBuildGrantCreateBodyWithTargetAndExpires(t *testing.T) {
	body, err := buildGrantCreateBody(
		"agent:scout", "vault.kv.*", "needs-approval",
		"00000000-0000-0000-0000-000000000001",
		"2026-06-01T00:00:00Z",
	)
	if err != nil {
		t.Fatalf("buildGrantCreateBody: %v", err)
	}
	if body.TargetScope == nil || *body.TargetScope != "00000000-0000-0000-0000-000000000001" {
		t.Errorf("TargetScope: got %+v", body.TargetScope)
	}
	if body.ExpiresAt == nil {
		t.Fatalf("ExpiresAt should be set; got nil")
	}
	if !body.ExpiresAt.Equal(time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("ExpiresAt: got %s", body.ExpiresAt)
	}
}

func TestBuildGrantCreateBodyRejectsBadExpires(t *testing.T) {
	_, err := buildGrantCreateBody("agent:scout", "*", "deny", "", "tomorrow")
	if err == nil {
		t.Fatalf("expected error for non-RFC3339 --expires")
	}
}

func TestBuildGrantElevateBodyRequiresExpires(t *testing.T) {
	_, err := buildGrantElevateBody("agent:scout", "*", "auto-execute", "", "")
	if err == nil {
		t.Fatalf("expected error for missing --expires on elevation")
	}
}

func TestBuildGrantElevateBodyHappy(t *testing.T) {
	body, err := buildGrantElevateBody(
		"agent:scout", "vault.kv.*", "auto-execute", "*",
		"2026-06-01T00:00:00Z",
	)
	if err != nil {
		t.Fatalf("buildGrantElevateBody: %v", err)
	}
	if body.ExpiresAt.IsZero() {
		t.Errorf("ExpiresAt should be set; got zero")
	}
	if body.TargetScope == nil || *body.TargetScope != "*" {
		t.Errorf("TargetScope: got %+v", body.TargetScope)
	}
	if string(body.Verdict) != "auto-execute" {
		t.Errorf("Verdict: got %q", body.Verdict)
	}
}

func TestGrantListParamsBuild(t *testing.T) {
	params := grantListParams("agent:scout", true, 25, 0)
	if params.PrincipalSub == nil || *params.PrincipalSub != "agent:scout" {
		t.Errorf("PrincipalSub: got %+v", params.PrincipalSub)
	}
	if params.IncludeExpired == nil || !*params.IncludeExpired {
		t.Errorf("IncludeExpired: got %+v", params.IncludeExpired)
	}
	if params.Limit == nil || *params.Limit != 25 {
		t.Errorf("Limit: got %+v", params.Limit)
	}
	if params.Offset != nil {
		t.Errorf("Offset should be nil when zero; got %+v", params.Offset)
	}
}

func TestPrintGrantListTableEmpty(t *testing.T) {
	var sb strings.Builder
	printGrantListTable(&sb, nil)
	if !strings.Contains(sb.String(), "no permission grants") {
		t.Errorf("empty render missing hint; got %q", sb.String())
	}
}

func TestPrintGrantListTableRows(t *testing.T) {
	var sb strings.Builder
	expires := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	grants := []api.AgentGrantRead{
		{
			Id:           uuid.MustParse("11111111-1111-1111-1111-111111111111"),
			PrincipalSub: "agent:scout",
			OpPattern:    "vault.kv.*",
			Verdict:      "auto-execute",
			ExpiresAt:    &expires,
			CreatedBySub: "op-admin",
			CreatedAt:    expires,
		},
	}
	printGrantListTable(&sb, grants)
	for _, want := range []string{"agent:scout", "vault.kv.*", "auto-execute", "2026-06-01T00:00:00Z"} {
		if !strings.Contains(sb.String(), want) {
			t.Errorf("printGrantListTable missing %q in %q", want, sb.String())
		}
	}
}

func TestRenderGrantEntryHumanLine(t *testing.T) {
	var sb strings.Builder
	expires := time.Date(2026, 6, 1, 12, 0, 0, 0, time.UTC)
	entry := &api.AgentGrantRead{
		Id:           uuid.MustParse("22222222-2222-2222-2222-222222222222"),
		PrincipalSub: "agent:scout",
		OpPattern:    "*",
		Verdict:      "auto-execute",
		ExpiresAt:    &expires,
	}
	if err := renderGrantEntry(&sb, entry, false, "created"); err != nil {
		t.Fatalf("renderGrantEntry: %v", err)
	}
	for _, want := range []string{"created grant", "agent:scout", "expires 2026-06-01T12:00:00Z"} {
		if !strings.Contains(sb.String(), want) {
			t.Errorf("human render missing %q in %q", want, sb.String())
		}
	}
}

func TestRenderGrantEntryPermanent(t *testing.T) {
	var sb strings.Builder
	entry := &api.AgentGrantRead{
		Id:           uuid.MustParse("22222222-2222-2222-2222-222222222222"),
		PrincipalSub: "agent:scout",
		OpPattern:    "*",
		Verdict:      "auto-execute",
	}
	if err := renderGrantEntry(&sb, entry, false, "created"); err != nil {
		t.Fatalf("renderGrantEntry: %v", err)
	}
	if !strings.Contains(sb.String(), "permanent") {
		t.Errorf("nil ExpiresAt should render \"permanent\"; got %q", sb.String())
	}
}

func TestGrantRevokeRejectsInvalidUUID(t *testing.T) {
	// The revoke verb is wired inline in newGrantRevokeCmd, so exercise
	// it via the command tree to assert the uuid.Parse error path.
	root := NewGrantRootCmd()
	root.SetArgs([]string{"revoke", "not-a-uuid"})
	cmd, _, stderr := newTestCmd(t)
	root.SetOut(cmd.OutOrStdout())
	root.SetErr(stderr)
	root.SetContext(cmd.Context())
	if err := root.Execute(); err == nil {
		t.Fatalf("expected error for invalid grant-id UUID")
	}
	if !strings.Contains(stderr.String(), "invalid <grant-id>") {
		t.Errorf("stderr missing parse-error hint; got %q", stderr.String())
	}
}
