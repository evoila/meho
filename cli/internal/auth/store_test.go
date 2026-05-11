// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 evoila Group

package auth

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

// TestFileStoreRoundTrip is the load-bearing happy-path for the
// file-fallback backend. It exercises Save → Load → Delete in a
// tmpdir so the test never touches the operator's real
// $XDG_CONFIG_HOME. Failures here mean a regression in the
// serialisation layer the CLI depends on when the OS keyring is
// unavailable (every CI run + every headless host).
func TestFileStoreRoundTrip(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "credentials.json")
	store := NewFileStoreAt(path)

	want := StoredToken{
		BackplaneURL: "https://meho.example.com",
		Issuer:       "https://kc.example.com/realms/meho",
		ClientID:     "meho-cli",
		AccessToken:  "access-token-value",
		RefreshToken: "refresh-token-value",
		IDToken:      "id-token-value",
		TokenType:    "Bearer",
		// Truncate to seconds so JSON RFC3339 round-trip is exact —
		// time.Time keeps monotonic clock data that serialisation
		// strips, which produces a spurious mismatch on Equal.
		Expiry: time.Now().UTC().Truncate(time.Second),
	}

	if err := store.Save(DefaultService, want.BackplaneURL, want); err != nil {
		t.Fatalf("save: %v", err)
	}

	got, err := store.Load(DefaultService, want.BackplaneURL)
	if err != nil {
		t.Fatalf("load: %v", err)
	}
	if got.AccessToken != want.AccessToken {
		t.Errorf("access token: got %q, want %q", got.AccessToken, want.AccessToken)
	}
	if got.RefreshToken != want.RefreshToken {
		t.Errorf("refresh token: got %q, want %q", got.RefreshToken, want.RefreshToken)
	}
	if got.IDToken != want.IDToken {
		t.Errorf("id token: got %q, want %q", got.IDToken, want.IDToken)
	}
	if got.Issuer != want.Issuer {
		t.Errorf("issuer: got %q, want %q", got.Issuer, want.Issuer)
	}
	if got.ClientID != want.ClientID {
		t.Errorf("client id: got %q, want %q", got.ClientID, want.ClientID)
	}
	if !got.Expiry.Equal(want.Expiry) {
		t.Errorf("expiry: got %v, want %v", got.Expiry, want.Expiry)
	}

	if err := store.Delete(DefaultService, want.BackplaneURL); err != nil {
		t.Fatalf("delete: %v", err)
	}
	if _, err := store.Load(DefaultService, want.BackplaneURL); !errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("post-delete load: got %v, want ErrTokenNotFound", err)
	}
}

// TestFileStoreEnforcesZeroSixHundred locks the security property
// that the credentials file is created mode 0600 and the directory
// 0700 — anything looser would leak the access token to other UIDs
// on a shared host.
//
// Skipped on Windows because POSIX file modes don't map cleanly
// there; the file backend on Windows is documented as best-effort
// and operators are expected to rely on the OS keyring backend.
func TestFileStoreEnforcesZeroSixHundred(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX mode bits not enforced on Windows")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "credentials.json")
	store := NewFileStoreAt(path)

	if err := store.Save(DefaultService, "user", StoredToken{AccessToken: "secret"}); err != nil {
		t.Fatalf("save: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat file: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0o600 {
		t.Errorf("file perms: got %o, want 600", perm)
	}

	dirInfo, err := os.Stat(filepath.Dir(path))
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if perm := dirInfo.Mode().Perm(); perm != 0o700 {
		t.Errorf("dir perms: got %o, want 700", perm)
	}
}

// TestFileStoreLoadMissingReturnsSentinel confirms that "no file
// yet" maps to ErrTokenNotFound rather than a raw "no such file"
// error. This is the path every meho status invocation takes on a
// brand-new host before login has run; misclassifying it as a real
// error would produce confusing "have you tried logging in?" prompts
// that already happened.
func TestFileStoreLoadMissingReturnsSentinel(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))
	if _, err := store.Load(DefaultService, "anything"); !errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("expected ErrTokenNotFound, got %v", err)
	}
}

// TestFileStoreLoadMalformedReturnsError defends against a truncated
// or hand-edited credentials file: load must surface a real error
// (not the sentinel) so the operator sees the corruption rather than
// silently logging in again over the rubble.
func TestFileStoreLoadMalformedReturnsError(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "credentials.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o600); err != nil {
		t.Fatalf("seed file: %v", err)
	}
	store := NewFileStoreAt(path)
	_, err := store.Load(DefaultService, "user")
	if err == nil {
		t.Fatalf("expected parse error, got nil")
	}
	if errors.Is(err, ErrTokenNotFound) {
		t.Fatalf("malformed file shouldn't surface as ErrTokenNotFound: %v", err)
	}
}

// TestFileStoreSupportsMultipleEntries shows that the on-disk shape
// holds entries for several (service, user) pairs simultaneously —
// the seam every multi-backplane future-CLI release will use.
func TestFileStoreSupportsMultipleEntries(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))

	one := StoredToken{BackplaneURL: "https://a.example", AccessToken: "tok-a"}
	two := StoredToken{BackplaneURL: "https://b.example", AccessToken: "tok-b"}

	if err := store.Save(DefaultService, one.BackplaneURL, one); err != nil {
		t.Fatalf("save one: %v", err)
	}
	if err := store.Save(DefaultService, two.BackplaneURL, two); err != nil {
		t.Fatalf("save two: %v", err)
	}

	gotOne, err := store.Load(DefaultService, one.BackplaneURL)
	if err != nil {
		t.Fatalf("load one: %v", err)
	}
	gotTwo, err := store.Load(DefaultService, two.BackplaneURL)
	if err != nil {
		t.Fatalf("load two: %v", err)
	}
	if gotOne.AccessToken != "tok-a" || gotTwo.AccessToken != "tok-b" {
		t.Fatalf("entries cross-talked: one=%q two=%q", gotOne.AccessToken, gotTwo.AccessToken)
	}
}

// TestFileStoreDeleteAbsentIsNoop matches the documented
// idempotency contract: deleting a non-existent entry returns nil
// rather than a sentinel — callers don't have to special-case
// first-run.
func TestFileStoreDeleteAbsentIsNoop(t *testing.T) {
	dir := t.TempDir()
	store := NewFileStoreAt(filepath.Join(dir, "credentials.json"))
	if err := store.Delete(DefaultService, "never-saved"); err != nil {
		t.Fatalf("delete absent: %v", err)
	}
}

// TestFileStoreDescribeIncludesPath verifies the operator-facing
// label contains the path so a confused operator can find the file.
func TestFileStoreDescribeIncludesPath(t *testing.T) {
	path := "/tmp/meho-test/credentials.json"
	store := NewFileStoreAt(path)
	desc := store.Describe()
	if want := path; !contains(desc, want) {
		t.Errorf("describe should contain %q, got %q", want, desc)
	}
}

// TestKeyForBackplaneNormalisesTrailingSlash guarantees that
// `meho login https://x/` and `meho login https://x` collide on the
// same store key — otherwise an operator's second invocation would
// silently store a duplicate.
func TestKeyForBackplaneNormalisesTrailingSlash(t *testing.T) {
	_, userA := KeyForBackplane("https://meho.example.com")
	_, userB := KeyForBackplane("https://meho.example.com/")
	if userA != userB {
		t.Errorf("trailing slash should normalise: %q vs %q", userA, userB)
	}
}

// TestStoredTokenJSONShape pins the wire shape — adding or renaming
// JSON fields here is a forward-compat break for tokens persisted
// by earlier CLI versions. The test deliberately writes the exact
// keys; a rename in the struct without updating this test is the
// signal that you've broken the on-disk schema.
func TestStoredTokenJSONShape(t *testing.T) {
	tok := StoredToken{
		BackplaneURL: "https://x",
		Issuer:       "https://kc",
		ClientID:     "id",
		AccessToken:  "at",
		RefreshToken: "rt",
		IDToken:      "idt",
		TokenType:    "Bearer",
	}
	data, err := json.Marshal(tok)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, key := range []string{
		`"backplane_url"`,
		`"issuer"`,
		`"client_id"`,
		`"access_token"`,
		`"refresh_token"`,
		`"id_token"`,
		`"token_type"`,
	} {
		if !contains(string(data), key) {
			t.Errorf("JSON missing %s in %s", key, string(data))
		}
	}
}

// contains is a tiny strings.Contains shim kept inline so this test
// file stays free of the strings import (cuts down on the import
// block, no functional value otherwise).
func contains(haystack, needle string) bool {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return true
		}
	}
	return false
}
