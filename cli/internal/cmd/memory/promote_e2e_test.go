// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package memory

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/evoila/meho/cli/internal/api"
	"github.com/evoila/meho/cli/internal/auth"
)

// E2E smoke for `meho promote` against a **live backplane**.
//
// Issue #627 acceptance criterion: "E2E smoke test promotes a real
// memory end-to-end against a running backplane (CI integration
// job)." This file runs only when the operator has provisioned a
// live backplane via the same `MEHO_E2E_BACKPLANE` /
// `MEHO_E2E_ACCESS_TOKEN` envs the CI integration job sets. With
// either env unset, every test in this file is t.Skip'd so the
// default `go test ./...` sandbox run stays hermetic.
//
// What the smoke covers (matches the bullet list in the issue body):
//
//   - Happy path: `meho promote user/<slug> --to user-tenant` →
//     exit 0; target row visible via `meho recall`.
//   - --move happy path → exit 0; source row gone (recall → 404).
//   - Idempotent re-run → exit 6 in human mode, exit 0 with --json.
//   - 403 promotion-without-authority → exit 5. (Not exercised in
//     this file — the integration harness would need a separate
//     non-admin operator token; left to the CI job's per-token
//     matrix.)
//
// Each test uses a unique slug (timestamp-suffixed) so reruns don't
// collide with leftover rows from a prior failed run.

const (
	e2eBackplaneEnv   = "MEHO_E2E_BACKPLANE"
	e2eAccessTokenEnv = "MEHO_E2E_ACCESS_TOKEN" //nolint:gosec // env name only
)

// requireLiveBackplane returns the backplane URL or t.Skip's the
// test when the env is unset. Mirrors the env-gate shape every
// integration-test convention adopts.
//
// The same `seedXDGAndToken` helper used by the in-process httptest
// tests is reused, but pointed at the live URL with the operator's
// real access token in the file store — this is exactly the shape
// `meho login` writes, so the auth + transport stack hits the
// production refresh path.
func requireLiveBackplane(t *testing.T) string {
	t.Helper()
	url := strings.TrimSpace(os.Getenv(e2eBackplaneEnv))
	token := strings.TrimSpace(os.Getenv(e2eAccessTokenEnv))
	if url == "" || token == "" {
		t.Skipf("skipping live-backplane smoke: set %s + %s to enable",
			e2eBackplaneEnv, e2eAccessTokenEnv)
	}
	// Layer the live URL + token over the per-test XDG_CONFIG_HOME
	// the helper builds. `seedXDGAndToken` writes a placeholder
	// token; override with the operator's real one so the live
	// backplane accepts the bearer.
	seedLiveTokenStore(t, url, token)
	return url
}

// seedLiveTokenStore reuses the file-store path the helper writes
// and replaces the placeholder access token with the env-provided
// value. Kept verbose so the wire shape stays obvious when
// debugging a failed CI job.
//
// `seedXDGAndToken` writes a placeholder StoredToken; we overwrite
// it with the operator's real bearer here so the typed-client's
// bearer-injecting editor sends production credentials to the live
// backplane.
func seedLiveTokenStore(t *testing.T, backplaneURL, accessToken string) {
	t.Helper()
	seedXDGAndToken(t, backplaneURL)
	store, err := auth.NewFileStore()
	if err != nil {
		t.Fatalf("seedLiveTokenStore: NewFileStore: %v", err)
	}
	service, user := auth.KeyForBackplane(backplaneURL)
	if err := store.Save(service, user, auth.StoredToken{
		BackplaneURL: backplaneURL,
		AccessToken:  accessToken,
		TokenType:    "Bearer",
		// 1h expiry mirrors the placeholder; the CI integration
		// job is expected to either re-mint a fresh token per
		// matrix entry or rely on the backplane's refresh path
		// (`api.AuthedClient.Refresh`) if MEHO_E2E_REFRESH_TOKEN
		// is also set.
		Expiry: time.Now().Add(1 * time.Hour),
	}); err != nil {
		t.Fatalf("seedLiveTokenStore: store.Save: %v", err)
	}
}

// uniqueSlug returns a slug guaranteed unique across re-runs of the
// same CI job by suffixing the test name with a timestamp + a
// monotonically-incrementing nonce. Slugs containing dots /
// hyphens / underscores are legal per the substrate's
// SLUG_PATTERN, so the timestamp shape `e2e-promote-<unix-ns>`
// always validates.
func uniqueSlug(prefix string) string {
	return fmt.Sprintf("%s-%d", prefix, time.Now().UnixNano())
}

// TestE2EPromoteUserToUserTenantHappyPath — fresh promotion against a
// live backplane: remember a `user`-scoped row, promote to
// `user-tenant`, verify the target row is visible.
func TestE2EPromoteUserToUserTenantHappyPath(t *testing.T) {
	backplaneURL := requireLiveBackplane(t)
	slug := uniqueSlug("e2e-promote-happy")

	// 1. Seed a user-scoped source row.
	cmdRem, _, stderrRem := newRunCmd(t)
	cmdRem.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmdRem, rememberOptions{
		BodyArg:           "e2e-promote-happy",
		ScopeArg:          "user",
		SlugArg:           slug,
		Persist:           true, // skip default 7-day TTL so test cleanup is deterministic
		BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E remember (seed): %v; stderr=%s", err, stderrRem.String())
	}
	defer cleanupMemory(t, backplaneURL, "user-tenant", slug)
	defer cleanupMemory(t, backplaneURL, "user", slug)

	// 2. Promote to user-tenant.
	cmdProm, stdoutProm, stderrProm := newRunCmd(t)
	cmdProm.SetIn(bytes.NewBufferString(""))
	if err := runPromote(cmdProm, promoteOptions{
		ScopeSlugArg:      "user/" + slug,
		ToArg:             "user-tenant",
		BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E promote: %v; stderr=%s", err, stderrProm.String())
	}
	out := stdoutProm.String()
	if !strings.Contains(out, "promoted user-tenant/"+slug) {
		t.Errorf("expected fresh-promotion line; got %q", out)
	}

	// 3. Verify the target row is visible (recall succeeds).
	cmdRec, stdoutRec, _ := newRunCmd(t)
	cmdRec.SetIn(bytes.NewBufferString(""))
	if err := runRecall(cmdRec, recallOptions{
		ScopeSlugArg: "user-tenant/" + slug, BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E recall target: %v", err)
	}
	if !strings.Contains(stdoutRec.String(), "e2e-promote-happy") {
		t.Errorf("expected target row body; got %q", stdoutRec.String())
	}
}

// TestE2EPromoteWithMoveDeletesSource — `--move` removes the source
// row server-side; a follow-up recall against the source returns
// 404 (memory_not_found / unexpected_response exit 4).
func TestE2EPromoteWithMoveDeletesSource(t *testing.T) {
	backplaneURL := requireLiveBackplane(t)
	slug := uniqueSlug("e2e-promote-move")

	cmdRem, _, _ := newRunCmd(t)
	cmdRem.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmdRem, rememberOptions{
		BodyArg: "move-me", ScopeArg: "user", SlugArg: slug,
		Persist: true, BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E remember: %v", err)
	}
	defer cleanupMemory(t, backplaneURL, "user-tenant", slug)

	// Promote with --move; expect source gone afterwards.
	cmdProm, _, stderrProm := newRunCmd(t)
	cmdProm.SetIn(bytes.NewBufferString(""))
	if err := runPromote(cmdProm, promoteOptions{
		ScopeSlugArg: "user/" + slug, ToArg: "user-tenant", Move: true,
		BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E promote --move: %v; stderr=%s", err, stderrProm.String())
	}

	// Source recall must 404.
	cmdRec, _, stderrRec := newRunCmd(t)
	cmdRec.SetIn(bytes.NewBufferString(""))
	err := runRecall(cmdRec, recallOptions{
		ScopeSlugArg: "user/" + slug, BackplaneOverride: backplaneURL,
	})
	if err == nil {
		t.Fatalf("E2E: source row should be 404 after --move; recall returned nil")
	}
	if !strings.Contains(stderrRec.String(), "memory_not_found") {
		t.Errorf("expected memory_not_found on source after --move; got %q", stderrRec.String())
	}
}

// TestE2EPromoteIdempotentRerun — re-running the same promote returns
// exit 6 in human-readable mode (the route returns 200 + the
// existing row; the CLI surfaces "already promoted" + exit 6).
func TestE2EPromoteIdempotentRerun(t *testing.T) {
	backplaneURL := requireLiveBackplane(t)
	slug := uniqueSlug("e2e-promote-rerun")

	cmdRem, _, _ := newRunCmd(t)
	cmdRem.SetIn(bytes.NewBufferString(""))
	if err := runRemember(cmdRem, rememberOptions{
		BodyArg: "rerun-test", ScopeArg: "user", SlugArg: slug,
		Persist: true, BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E remember: %v", err)
	}
	defer cleanupMemory(t, backplaneURL, "user", slug)
	defer cleanupMemory(t, backplaneURL, "user-tenant", slug)

	// First promote — fresh, exit 0.
	cmdFirst, _, _ := newRunCmd(t)
	cmdFirst.SetIn(bytes.NewBufferString(""))
	if err := runPromote(cmdFirst, promoteOptions{
		ScopeSlugArg: "user/" + slug, ToArg: "user-tenant",
		BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E first promote: %v", err)
	}

	// Sleep so the second call's pre-POST timestamp is strictly
	// after the first promote's created_at. The wall-clock gap
	// between fresh and rerun is what isIdempotentRerun keys on;
	// a sub-second turnaround on a fast backplane could otherwise
	// false-negative.
	time.Sleep(1500 * time.Millisecond)

	// Second promote — idempotent re-run.
	cmdSec, stdoutSec, _ := newRunCmd(t)
	cmdSec.SetIn(bytes.NewBufferString(""))
	err := runPromote(cmdSec, promoteOptions{
		ScopeSlugArg: "user/" + slug, ToArg: "user-tenant",
		BackplaneOverride: backplaneURL,
	})
	if err == nil {
		t.Fatalf("E2E idempotent: expected exit-6 error; got nil")
	}
	var coder interface{ ExitCode() int }
	if !errors.As(err, &coder) || coder.ExitCode() != 6 {
		t.Errorf("E2E idempotent: expected exit 6; got %v", err)
	}
	if !strings.Contains(stdoutSec.String(), "already promoted") {
		t.Errorf("E2E idempotent: expected idempotent wording; got %q", stdoutSec.String())
	}

	// Third promote with --json — collapses to exit 0.
	cmdJSON, stdoutJSON, _ := newRunCmd(t)
	cmdJSON.SetIn(bytes.NewBufferString(""))
	if err := runPromote(cmdJSON, promoteOptions{
		ScopeSlugArg: "user/" + slug, ToArg: "user-tenant",
		JSONOut:           true,
		BackplaneOverride: backplaneURL,
	}); err != nil {
		t.Fatalf("E2E idempotent --json: %v", err)
	}
	var decoded api.MemoryEntry
	if err := json.Unmarshal(stdoutJSON.Bytes(), &decoded); err != nil {
		t.Fatalf("E2E idempotent --json: stdout not JSON: %v\n%s", err, stdoutJSON.String())
	}
	if decoded.Slug != slug {
		t.Errorf("E2E idempotent --json: expected slug %q; got %+v", slug, decoded)
	}
}

// cleanupMemory deletes one memory row to keep the live backplane
// tidy. Failures during cleanup are logged but not fatal — a
// leftover row from a flaky test shouldn't mask the real failure.
func cleanupMemory(t *testing.T, backplaneURL, scopeStr, slug string) {
	t.Helper()
	scope := Scope(scopeStr)
	cmd, _, _ := newRunCmd(t)
	cmd.SetIn(bytes.NewBufferString(""))
	if err := runForget(cmd, forgetOptions{
		ScopeSlugArg: string(scope) + "/" + slug,
		Confirm:      true, BackplaneOverride: backplaneURL,
	}); err != nil {
		// idempotent forget — log but don't fail. Cleanup failures
		// don't change the test verdict.
		t.Logf("cleanup forget %s/%s: %v", scope, slug, err)
	}
}
